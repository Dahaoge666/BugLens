# 0004 — Graph 生命周期、证据边界与工具审批恢复

- 日期：2026-09-08
- 变更类型：`Added` / `Changed` / `Fixed` / `Docs`
- 范围：确定性 Graph、短执行 Runtime、SQLite 检查点、Agents SDK、Command/Event 协议、Admin API 和前端

## 已落地

- 将诊断流程收敛为 `Analyze → Investigate → Evaluate → Summarize`，并把节点输入、澄清、Skip、评测回环、无进展和最终 outcome 的判断留在纯 Graph 中。
- 统一一次 Command 执行到等待或终止边界；支持恢复、取消、幂等 Command、revision、lease/fencing 和崩溃后的 interrupted execution 处理。
- 为每个 `run_id + node` 使用独立 `SQLiteSession`；业务 `DiagnosisState`、Event、revision、配置快照和 SDK 状态分别持久化。
- 接入只读 `function_tool` 注册、参数/结果边界、工具审计和 evidence ID；显式 `needs_approval=True` 的工具可触发 `waiting_approval`。
- 使用 Agents SDK `RunState` 加密检查点实现跨进程 Approve/Reject 恢复；公开快照只返回脱敏的工具名、参数和 request ID，不返回 SDK 状态或 secret。
- 将 provider 明确释放的 reasoning summary 作为脱敏、有界的审计派生字段保存，不保存原始消息或隐藏推理内容。
- Admin 列表遇到损坏 run 时返回 `degraded_count` 并跳过坏行；前端独立处理 Admin 请求并展示审批卡片。
- 增加兼容网关的 `httpx.MockTransport` 集成回归，覆盖 Chat Completions、流式、忽略 `response_format` 和 JSON coercion。

## 验证

- 后端：59 个测试通过；ruff 检查和格式检查通过。
- 前端：TypeScript 类型检查和 production build 通过。
- 发布入口：backend/frontend 安装脚本、CLI 帮助、制品构建及副本 SQLite 迁移/恢复检查通过。
