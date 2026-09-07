# BugLens 可暂停与可恢复生命周期规范

## 范围

本文只定义诊断运行的生命周期、检查点和恢复语义。相关边界见 [Runtime/Adapter 规范](agent-runtime-adapter-spec.md)、[Command/Event 协议](agent-protocol-spec.md)和[运行配置规范](runtime-config-spec.md)。固定 Graph 保持 `Analyze → Investigate → Evaluate → Summarize`；总定位尝试最多两次，因此评测失败后最多重试 Investigate 一次。

## 当前实现

`DiagnosisRuntime` 已将每次短执行拆成可提交的 checkpoint：Runtime 在等待用户、完成、失败或取消
前保存 `DiagnosisState` 和对应 Event，然后结束本次调用；不会在 CLI 进程或 HTTP 请求中长期阻塞。
`SQLiteCheckpointStore` 持久化运行状态、Command 幂等记录、Event、租约和配置快照。SDK
`SQLiteSession` 仍只保存节点模型消息，不能决定业务 Graph 从哪里恢复。

`GET /v1/runs/{id}/events?after=N` 是有界历史读取：按 sequence 返回已提交 Event 后关闭；不存在的
run 返回 `run_not_found`，合法但没有新事件的追赶请求返回空 SSE 流。客户端断线应先读取 Run 快照，再
从最大 sequence 追赶，不把断流误判为 failed。

## 状态模型

生命周期为 `created`、`running`、`waiting_user`、`waiting_tool`、`waiting_approval`、`completed`、`failed`、`canceled`。三个等待态只可恢复到 `running`；后三者为终态。

诊断结果单独建模为 `confirmed | inconclusive`。评测达到上限仍未通过属于 `completed/inconclusive`，不是执行失败，且报告不得保留已确认主结论。

执行游标为 `analyze | investigate | evaluate | summarize | done`。`DiagnosisState` 增加：

```python
lifecycle_status: LifecycleStatus
outcome: DiagnosisOutcome | None
current_node: GraphNode
pending_interaction: UserInteractionRequest | None
pending_tool: PendingToolRequest | None
pending_approval: PendingApproval | None
revision: int
schema_version: int
config_snapshot_id: str
created_at: datetime
updated_at: datetime
last_error: FailureRecord | None
```

任意时刻最多存在一个 `pending_*`；等待态必须具有对应 pending 对象；running 和终态不得有 pending；completed 必须有 outcome；`done` 只用于终态。

## 推进规则

Graph 每次只计算一个确定性转换，不读取 stdin/HTTP，也不直接写数据库。

- Analyze 需要澄清时进入 `waiting_user`，否则转 Investigate。
- Investigate 新尝试增加 attempt；澄清恢复不增加；完成后转 Evaluate。
- Evaluate 通过后设置 confirmed 并转 Summarize；未通过且可重试则回到 Investigate；达到上限设置 inconclusive 后转 Summarize。
- Summarize 只转写结构化结果，完成后设置 `done/completed`。

## 暂停恢复

Agent 返回 `UserInteractionRequest` 后，Runtime 必须原子保存检查点和 `InputRequired` 事件并结束短执行，不得调用 `input()`。回答 Command 必须匹配 revision 和 request ID，经校验脱敏后清除 pending、增加澄清轮数，并用 `ClarificationInput` 恢复来源节点。

恢复继续使用 `{run_id}:{node_name}` SDK Session，不自行回放消息。未来 `waiting_approval` 保存 SDK `RunState`；业务澄清不得伪装成工具审批。未来工具必须使用稳定幂等键。

## 持久化与并发

首期 SQLite 使用独立的 `diagnosis_runs`、`diagnosis_commands`、`diagnosis_events` 表。状态更新、Command 幂等记录和 Event 必须同事务提交。revision 提供乐观锁，短租约避免同一 run 并发推进。

基础设施重试耗尽进入 failed；业务评测未通过进入 completed/inconclusive。错误记录只能包含稳定 code、脱敏 message、node、retryable 和时间。

## 验收标准

- 暂停后可关闭进程并由新进程恢复，正常恢复不重做首次节点调用；
- Command 重放不重复推进，错误 revision/request ID 被拒绝；
- 两个执行者不能并发推进同一 run；
- Evaluate 重试继续使用原 Investigate Session；
- Graph 不依赖 transport；测试不调用真实模型；Ruff、pytest、build 通过。

`AGENTS.md` 已同步采用本规范：允许持久化确定性业务检查点，但 SDK Session 仍独占模型消息历史。
