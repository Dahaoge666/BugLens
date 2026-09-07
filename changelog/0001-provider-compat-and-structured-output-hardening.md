# 0001 — OpenAI 兼容端点支持、结构化输出加固与配套健壮性修复

- 日期：2026-09-07（初稿），2026-09-08（#4–#8 落地）
- 来源：真实模型端到端测试（见 [`../TESTING_REPORT.md`](../TESTING_REPORT.md) 第 3、6 节）
- 变更类型：`Added` / `Changed` / `Docs`
- 关联文件：`app/bootstrap.py`、`app/agents.py`、`app/application.py`、`app/config.py`、`app/cli.py`、`app/graph.py`
- 测试：29 个单测全绿（新增 9 个回归测试），`ruff` 通过；真实端到端诊断 `completed/confirmed` 与 `completed/inconclusive` 均已验证

## 背景

真实模型端到端测试使用一个 OpenAI 兼容的 MaaS 网关（litellm，GLM 5.2 reasoning 模型）作为 BugLens 后端，暴露出 openai-agents SDK 默认行为与自建网关的三重不兼容：

1. SDK 默认使用 **Responses API**（`/v1/responses`），网关返回 400 不支持。
2. SDK 非流式路径用 `stream=False`，而该网关非流式聚合损坏，请求挂起/失败。
3. SDK 结构化输出依赖 `response_format` 强制 JSON，但该网关在流式下**不强制** `response_format`，模型仍输出纯文本（偶带 ` ```json ` 包裹），导致 SDK `Invalid JSON when parsing model output`。

三者叠加会使任意 OpenAI 兼容网关无法跑通 BugLens 诊断流程。

## 已落地

### 1. 无自定义 OpenAI 兼容端点配置入口 — `Added`（`app/bootstrap.py`、`app/config.py`）

- 问题：SDK 默认连接 OpenAI 官方端点，自建网关（vLLM / litellm / Azure 兼容等）无法开箱使用。
- 修复：`configure_openai_provider(*, base_url, api_key, timeout) -> bool` 接收显式参数（不再读裸环境变量），检测到自定义端点时调用 `set_default_openai_client`（`use_for_tracing=False`）+ `set_default_openai_api("chat_completions")`，返回"是否自定义端点"标志供 runner 决定是否启用流式。端点凭据正式纳入 `Settings.openai_base_url` / `openai_api_key` / `openai_timeout`（对应环境变量 `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `BUGLENS_OPENAI_TIMEOUT`），由 `build_local_service` 传入。
- 测试：`test_settings_read_openai_provider_options`、`test_configure_openai_provider_returns_custom_flag`。

### 2. 仅支持流式的网关无法运行 — `Changed`（`app/agents.py`）

- 问题：`Runner.run`（非流式，`stream=False`）在只支持流式的网关上挂起或返回错误。
- 修复：`OpenAINodeRunner.__init__` 增加 `streaming: bool = False`；`run()` 在 `streaming=True` 时改用 `Runner.run_streamed` 并消费 `stream_events()`，否则维持 `Runner.run`（OpenAI 官方端点与既有单测不受影响）。`build_local_service` 用 `configure_openai_provider()` 的返回值决定 `streaming`。
- 测试：真实端到端诊断（流式路径）+ 既有 `test_sdk_runner_uses_same_session_*`（非流式路径回归）。

### 3. 结构化输出强依赖 `response_format` — `Added`（`app/agents.py`）

- 问题：很多网关不强制 `response_format`，模型输出纯文本或带 Markdown 包裹，SDK 的 `validate_json` 解析失败。
- 修复：
  - `_schema_hint(output_type)`：把 `output_type.model_json_schema()` 显式注入 instructions 末尾，要求模型只输出匹配 schema 的合法 JSON，不依赖 `response_format` 强制。
  - `_install_json_coercion()`：在导入时对 `agents.util._json.validate_json` 做一次性、幂等包装，解析前剥离 ` ```json ` 包裹与首尾多余文本（取首个 `{` 到末个 `}`），合法 JSON 不被破坏。
- 测试：真实端到端诊断（4 节点结构化输出全部解析成功）。

### 4. `NodePolicy.model` 是死代码 — `Changed`（`app/graph.py`、`app/agents.py`）

- 问题：`config.py` 定义了 `NodePolicy.model`（默认 `gpt-4.1-mini`），但 `agents.py` 的 `_agent` 从未把它传给 `Agent`，模型名实际由 SDK 默认值（`OPENAI_DEFAULT_MODEL` 环境变量）决定。配置文件改 `model` 不生效，违反 AGENTS.md "固定工作流参数来自版本化配置 profile 并持久化快照"的承诺。
- 修复：
  - `NodeRuntimeContext` 新增 `model: str | None = None` 字段。
  - `DiagnosisGraph._runtime_for_config` 把 `node_policy.model` 放入 context。
  - `OpenAINodeRunner._agent` 接收 `model` 参数并构造 `Agent(model=model)`，agent 缓存 key 含 `model` 以避免不同模型的 agent 混用。
- 验证：真实诊断下用配置文件指定 `maas-glm-5.2-volcengine-codeagent` 后，`Agent.model` 正确传入（此前默认 `gpt-4.1-mini` 会被 MaaS 网关拒绝）。
- 测试：`test_node_runtime_context_carries_model_field`、`test_node_policy_model_is_passed_to_agent`。
- 注意：使用自定义网关时，必须通过配置文件（`BUGLENS_CONFIG`）显式指定各节点 `model`，否则 `NodePolicy.model` 默认 `gpt-4.1-mini` 会被传给网关而报错——这是修复"死代码"后的预期行为（配置现在真正生效）。

### 5. `OPENAI_API_KEY` 缺失报错不清晰 — `Changed`（`app/application.py`、`app/cli.py`）

- 问题：health 将 `model_credentials` 标为 `degraded`，但真正运行到 `Runner` 才失败，且错误被 `NodeExecutionError` 包成 `"execution failed"`，丢失"未配置 API Key"这一根因，排障困难。
- 修复：
  - 新增 `ModelCredentialsError(code="model_credentials_not_configured")`。
  - `ApplicationService.__init__` 接收 `require_model_credentials: bool` 与 `openai_base_url` / `openai_api_key`，在 `send(StartDiagnosis)` 前调用 `_check_model_credentials()` 快速失败。逻辑：自定义端点（有 `base_url`）可能自带鉴权，不强制要求 `api_key`；仅默认 OpenAI 路径要求 `api_key`。
  - `build_local_service` 启用该校验并注入凭据。
  - `cli.main` 捕获 `ModelCredentialsError` 转为退出码 `66` + 中文提示，而非 traceback。
- 验证：真实环境下 `OPENAI_API_KEY` 未设时，CLI 直接报"模型凭据未就绪：OPENAI_API_KEY is not configured..."并退出 66（修复前是被包成 `node_execution_failed` 在 analyze 节点失败，退出 2）。
- 测试：`test_application_service_requires_model_credentials`、`test_application_service_custom_endpoint_needs_no_api_key`。

### 6. CLI 澄清交互在非 TTY 环境直接退出 130 — `Changed`（`app/cli.py`）

- 问题：`ConsoleClarifier.ask` 遇 `EOFError` 抛出后被 `main` 捕获成"已取消"并退出 130。远程 CLI / CI / 管道等非交互场景体验差，用户无法区分"被取消"与"环境不支持交互"。
- 修复：
  - 新增 `NonInteractiveClarifierError(code="non_interactive_clarifier")`。
  - `ConsoleClarifier` 接收可注入的 `stream`（默认 `sys.stdin`），`ask()` 开头检测 `isatty()`，非 TTY 时抛 `NonInteractiveClarifierError`，提示改用 `--resume RUN_ID` 或 Web Adapter。
  - `main` 捕获该异常转为退出码 `64`，且**保留 run 处于 `waiting_user` 状态**以便后续 `--resume` 续跑（而非像 130 那样暗示取消）。
- 验证：真实环境下管道输入（`</dev/null`）触发澄清时，输出"需要补充信息但当前环境不支持交互输入..."并退出 64（修复前是退出 130 "已取消"）。
- 测试：`test_console_clarifier_rejects_non_tty`。

### 7. tracing 与自定义端点冲突 — `Changed`（`app/bootstrap.py`）

- 问题：`BUGLENS_TRACING=true`（默认）时 SDK tracing 可能向自定义 `base_url` 上报失败。此前 `configure_openai_provider` 已设 `use_for_tracing=False` 规避，但运行时 `tracing_enabled` 仍为 True，业务 tracing 路径仍可能误用自定义端点。
- 修复：`build_local_service` 计算 `tracing_enabled = settings.tracing_enabled and not custom_endpoint`，自定义端点时**默认禁用**全部 tracing（LLM tracing 与业务 tracing 一并），操作者若需对自定义端点开 tracing 须显式设置并理解风险。该值同时传给 `DiagnosisGraph` 与 `DiagnosisRuntime`。
- 测试：`test_build_local_service_disables_tracing_for_custom_endpoint`（断言 `BUGLENS_TRACING=true` + 自定义端点下 `service.runtime.tracing_enabled is False`）。

### 8. 硬编码超时 — `Changed`（`app/config.py`、`app/bootstrap.py`）

- 问题：`AsyncOpenAI(timeout=60.0)` 不可配；reasoning 模型或慢节点可能超时。
- 修复：`Settings.openai_timeout: float = Field(default=60.0, gt=0, le=600)`，对应环境变量 `BUGLENS_OPENAI_TIMEOUT`。`configure_openai_provider` 接收 `timeout` 参数构造 `AsyncOpenAI(timeout=timeout)`。范围约束 `0 < t ≤ 600` 秒防止误配过大。
- 测试：`test_settings_read_openai_provider_options`（读 120）、`test_settings_reject_invalid_timeout`（拒绝 0）。

## 状态与后续

- 1–8 全部已在代码中落地并通过真实诊断验证 + 单测回归。
- 测试与可观测性补遗（流式路径单测、JSON coercion 表驱动单测、端到端 e2e、OpenTelemetry trace 断言等）见 [`../TODO.md`](../TODO.md)。
