# OpenAI Agents SDK 能力采用规范

## 目的

本文规定 BugLens 哪些基础能力直接使用 OpenAI Agents SDK，哪些逻辑仍由项目代码负责。目标是减少重复实现，同时保持诊断流程可解释、可审计、可控重试和节点隔离。

参考官方文档：

- [Running agents](https://developers.openai.com/api/docs/guides/agents/running-agents)
- [Orchestration and handoffs](https://developers.openai.com/api/docs/guides/agents/orchestration)
- [Guardrails and human review](https://developers.openai.com/api/docs/guides/agents/guardrails-approvals)
- [Results and state](https://developers.openai.com/api/docs/guides/agents/results)

## 当前采用

| SDK 能力 | 项目中的用途 | 决策 |
| --- | --- | --- |
| `Agent` | 定义 Analyze、Investigate、Evaluate、Summarize 四个节点 | 保留 |
| Pydantic `output_type` | 直接得到经过 Schema 校验的节点结果 | 保留 |
| `Runner.run()` | 执行模型调用、工具循环和最终输出处理 | 保留 |
| `SQLiteSession` | 保存同一节点的多轮消息，不自行拼接历史 | 保留 |
| `RunContextWrapper` 的本地 context | 向当前运行提供 run ID、节点名和配置版本 | 保留 |
| `trace()` | 用 `run_id` 把四个节点归入同一次诊断 | 保留 |
| `max_turns` | 限制单个节点的运行轮数 | 保留 |

SDK Session 是共享 Agent Runtime 的模型会话层：无论入口来自本地 CLI、远程 CLI 还是 Web，都由 Runtime 使用相同 Session 规则。一个会话只选择一种连续性策略；本项目不同时混用 Session、`previous_response_id` 和 Conversations API。

## 可继续替换的能力

### 流式运行

可用 `Runner.run_streamed()` 消费 SDK 运行事件，并由 Runtime 映射成协议层瞬时 Event。SDK 事件不得直接暴露给 Adapter，也不得替代与检查点同事务提交的持久化领域 Event。

### 生命周期 hooks

节点耗时、模型调用、工具调用和错误日志应通过 SDK hooks 收集。Graph 只记录业务状态、分类、评测分数和重试次数，避免另写一套模型运行观察器。

### Guardrails

接入外部证据后使用 SDK guardrails：

- Analyze 的 input guardrail 检查输入范围、敏感信息和明显注入内容；
- Summarize 的 output guardrail 检查未决结论是否被写成已确认事实；
- 每个函数工具使用 tool guardrail 校验参数、返回值和数据范围。

Pydantic 字段长度、枚举、评测阈值和节点路由仍由普通代码校验。这些是确定性数据契约，不需要再调用一个模型。

### Function tools 与 MCP

日志、指标、Trace、配置快照和变更记录应注册为 SDK function tools 或 MCP 工具。工具必须只读、最小权限、限制时间范围和返回数量，并提供可引用 ID。SDK 负责参数 Schema、工具调用循环和结果回传，项目只实现具体只读数据适配器。

### 工具审批与 `RunState`

如果未来加入需要人工确认的敏感工具，应使用 `needs_approval`、`interruptions` 和可序列化 `RunState`。任一 Adapter 提交审批 Command 后，由 Runtime 使用同一 state 恢复，不自行重建待执行工具调用。

这项能力不替代语义澄清。语义澄清由 `UserInteractionRequest`、回答 Command 和 Graph 次数上限管理；工具审批只表示批准或拒绝已经生成的工具调用。

### 长会话压缩

当单节点历史明显增长时，可在现有 Session 上增加 SDK 会话压缩能力。V1 每个节点最多少量轮次，暂不启用，避免增加一次额外模型调用和调试复杂度。

## 不用于替代 Graph 的能力

| 能力 | 不采用为主流程的原因 |
| --- | --- |
| handoff | 控制权会转交给专家 Agent；当前流程要求 Graph 固定执行评测门禁、重试上限和统一总结。 |
| `Agent.as_tool()` | 适合由一个 manager Agent 自主决定何时调用专家；当前类别路由和评测循环必须由代码确定。 |
| `result.to_input_list()` | Session 已负责历史加载和保存，手工回放会造成重复上下文。 |
| `previous_response_id` / Conversations API | 与本地 SQLite Session 属于不同的会话连续性策略，不应混用。 |
| SDK HITL 作为问题澄清 | 它恢复被工具审批暂停的同一次运行，不能表达用户对结构化诊断问题的答案。 |

Application Service/Runtime 负责创建和持久化 `DiagnosisState`。Graph 负责按类别选择定位提示词、执行评测阈值、控制最多两次定位和两轮澄清，并计算诊断 outcome。生命周期由 Runtime 提交：评测通过为 `completed/confirmed`，未通过为 `completed/inconclusive`。

## 目标能力顺序

1. 保持现有四节点、结构化输出、SQLite Session 和 tracing。
2. 引入共享 Runtime、业务检查点和 Command/Event 协议，CLI 迁移到 LocalAgentClient。
3. 增加 HTTP/SSE Adapter 和 RemoteAgentClient。
4. 使用流式运行和 hooks 改善所有 Adapter 的进度与观测。
5. 为日志、指标和 Trace 增加只读工具，并按需启用审批与 `RunState`。
6. 用离线故障集评测不同配置快照。

## 验收条件

- 四个节点分别使用独立 Session ID，任一节点不能读取其他节点的消息历史。
- 同一节点的澄清或评测重试由 SDK Session 维护历史，项目不调用 `to_input_list()`。
- 节点输出必须通过 Pydantic Schema；评测总分和门槛由代码重新计算。
- Graph 的路由在相同结构化输出下完全确定。
- 工具保持只读，参数和返回值通过 SDK tool guardrail 后才进入模型上下文。
- 未通过评测或超过澄清次数时生命周期为 `completed`、outcome 为 `inconclusive`，报告不保留已确认主结论。
