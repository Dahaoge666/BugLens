# TODO

测试与可观测性相关的补遗。来源：[`TESTING_REPORT.md`](TESTING_REPORT.md) 第 6.3 节。核心修复（changelog 0001 的 #1–#8）已落地，以下为尚未覆盖的测试与可观测性增强项。

## 测试

- [ ] **兼容网关契约测试**：当前单测用 `FakeRunner`，不覆盖流式 / `response_format` / JSON coercion 的真实路径。增加一个基于本地 fake OpenAI server（或 `MockOpenAIClient`）的集成测试，锁定：自定义 `OPENAI_BASE_URL` 时走 `chat_completions` + 流式、`response_format` 不被强制时仍能解析结构化输出、coercion 行为稳定。
- [ ] **`_schema_hint` / `_install_json_coercion` 单测**：
  - coercion 表驱动：` ```json{...}``` `、首尾带解释文本、合法 JSON 不被破坏、无 JSON 时安全降级。
  - `_schema_hint` 对四个 `output_type` 均生成非空 JSON Schema 片段且包含 `properties`。
  - 幂等性：多次导入 `app.agents` 不会重复包装 `validate_json`。
- [ ] **`streaming` 开关单测**：`OpenAINodeRunner(streaming=True)` 使用 `Runner.run_streamed`，`streaming=False` 使用 `Runner.run`（既有 `test_sdk_runner_uses_same_session_per_node_and_separate_node_sessions` 等用例需保持绿）。
- [ ] **多模型解耦路径单测**：`NodeRuntimeContext.model_config` 携带 `ModelConfig` 时，`_model_instance` 构造的 `OpenAIChatCompletionsModel` 绑定了对应的 `base_url/api_key/timeout`，且按 `(model, base_url, timeout, api_key)` 缓存复用；`model_config=None` 时退回裸名 + 全局客户端；per-model `streaming` 覆盖 `default_streaming`。
- [ ] **admin API key 脱敏 / 回填单测**：`AdminApplicationService.config()` 对 `models.*.api_key` 做脱敏；`apply_config` 收到空或含 `****` 的 api_key 时回填原值，收到新值时覆盖。

## 可观测性

- [ ] **`reasoning_content` 纳入可追溯性**：网关返回的思维链被 SDK 放入 `ResponseReasoningItem`，但 `DiagnosisState` 未保留推理过程，"为何得出此根因"无法回溯。评估在节点结果或 trace 中保留（脱敏后的）推理摘要，增强诊断可解释性，同时不违背"业务状态不保存 SDK 消息"的边界（推理摘要是结构化派生数据，非原始消息回放）。
