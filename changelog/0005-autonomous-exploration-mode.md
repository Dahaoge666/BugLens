# 0005 — 自主探索模式（auto-skip 澄清 + 只读探索工具）

- 日期：2026-09-09
- 变更类型：`Added`
- 范围：确定性 Graph、短执行 Runtime、内置只读工具、配置 Profile

## 概述

新增「自主探索模式」：以 `context.attributes["auto_explore"]=true` 启动的诊断不再向用户请求澄清，而是自动跳过澄清并继续推进 Graph；同时注册一个内置只读探索工具 `http_get`，让 agent 在 enabled 的 profile 下自主获取公开诊断上下文。

## 已落地

- 新增 `app/builtin_tools.py`：内置只读工具 `http_get(url)`，用 `httpx2`（OpenAI SDK 的传递依赖）执行 `GET`，`timeout=15s`、`follow_redirects=True`，响应体截断到 8KB 并经 `sanitize_data` 脱敏后返回；`needs_approval=False`，自主模式自动执行。工厂 `build_builtin_tool_registry()` 创建 `ToolRegistry(enabled=True, allowed_nodes={analyze,investigate}, max_results=20, timeout_seconds=30)` 并注册 `http_get`。每个执行由 `_instrument_tool` 生成稳定 `tool_execution_id`，满足「可引用 ID」约束。
- `app/bootstrap.py`：`build_local_service` 创建 `OpenAINodeRunner` 时注入 `tool_registry=build_builtin_tool_registry()`。所有 profile 都注册了 `http_get`，但只对 `tools.enabled=true` 的 profile 实际暴露给 agent（由 `_tools_for` → `ToolRegistry.is_enabled` 按 per-node `tools_enabled` 过滤），保持默认安全。
- `app/runtime.py` `_drive`：在 node 完成、`new_state` 为 `WAITING_USER` 且 `pending_interaction` 非空的分支前插入自主探索拦截。当 `bool(new_state.context.attributes.get("auto_explore"))` 为真时，调用 `DiagnosisGraph.skip_interaction(...)` 得到 `RUNNING` 态（`pending_interaction=None`、`current_node=resume_node`、澄清轮次 +1），重新置 `revision=state.revision+1`，构造并 `commit` `InputSkipped` 事件，随后**不 break**，继续 while 循环进入下一 `prepare_step`。skip 递增澄清轮次，受 `max_clarification_rounds` 约束，预算耗尽由 Graph 自动转 `SUMMARIZE/INCONCLUSIVE`，不会死循环。非自主模式保持原 `InputRequired + RunWaiting + break` 行为。
- `config/buglens.maas.yaml`：default profile 的 `tools` 改为 `enabled: true`，并加 `allowed_nodes: [analyze, investigate]`。
- 严格遵守 AGENTS.md：工具只读、最小权限、有界返回、提供可引用 ID；Graph 仍只做确定性状态转换，不读 stdin/HTTP；CLI/Web 仍经同一 `ApplicationService` + `DiagnosisRuntime`。

## 验证

- 后端：新增 `tests/test_autonomous_explore.py` 4 个测试覆盖（auto-skip 跳过澄清并推进到 investigate、澄清预算耗尽转 INCONCLUSIVE、`http_get` 有界返回与脱敏、registry 注册与门控）；连同原有测试共 63 个通过；`ruff check` 与 `ruff format --check` 通过。
- 测试不调用真实模型，使用 fake `NodeRunner` 验证路由；`http_get` 用 monkeypatch 替换 `httpx2.AsyncClient` 验证有界与脱敏。

## 关联文件

- `app/builtin_tools.py`（新增）
- `app/bootstrap.py`
- `app/runtime.py`
- `config/buglens.maas.yaml`
- `tests/test_autonomous_explore.py`（新增）

## Follow-up

- 自主模式下若 agent 通过 `http_get` 仍频繁触发澄清并被自动跳过，可考虑在 skip reason 中累积 tool evidence 命中情况，便于 Summarize 解释证据缺口来源。
- 后续如新增更多只读探索工具，应统一经 `build_builtin_tool_registry` 注册，并保持 `needs_approval=False` + 有界返回契约。
