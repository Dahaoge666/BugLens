# Runtime、协议与恢复

本文定义后端内部的 Command/Event 语义、生命周期、恢复和持久化边界。公开 HTTP/SSE 路径与 JSON 字段以根目录 `frontend-backend-contract.md` 为准；组件职责见 `architecture.md`。

## 三类状态必须分离

| 类型 | 内容 | 持久化位置 |
| --- | --- | --- |
| 业务状态 | `DiagnosisState`、当前节点、attempt、pending、outcome | CheckpointStore |
| 业务事实 | 已接受 Command、已提交 Event、revision、sequence | CheckpointStore |
| 模型上下文 | 单个节点的模型消息历史 | SDK `SQLiteSession` |

SDK Session 不能决定 Graph 从哪里恢复，`DiagnosisState` 也不能保存或回放模型消息。

## Command 与 Event

Command 是请求，Event 是已提交事实。CLI 参数和 HTTP DTO 必须先转换为同一组严格 Pydantic 模型；Runtime 返回统一的 `AsyncIterator[AgentEvent]`。

Command 公共字段为 `protocol_version`、`command_id`、`run_id`、`expected_revision` 和 `submitted_at`。当前命令：

- `StartDiagnosis`：问题、上下文、证据和 profile；固定策略参数不属于请求。
- `SubmitUserAnswers`：当前 pending request ID 和结构化答案。
- `CancelDiagnosis`：取消原因。

Event 公共字段为 `protocol_version`、`event_id`、`run_id`、`sequence`、`revision` 和 `occurred_at`。当前持久化事件：

- `RunStarted`、`NodeStarted`、`NodeCompleted`；
- `InputRequired`、`RunWaiting`；
- `RunCompleted`、`RunFailed`、`RunCanceled`。

瞬时进度事件可以丢失，不能参与恢复判断。持久化 Event 必须与对应状态转换在同一事务提交，且不得包含 SDK 对象、完整模型消息或 secret。

## 生命周期与结果

生命周期：

```text
created → running ─┬→ waiting_user ─→ running
                  ├→ waiting_tool ─→ running
                  ├→ waiting_approval → running
                  ├→ completed
                  ├→ failed
                  └→ canceled
```

`completed`、`failed`、`canceled` 是终态。诊断结果单独使用 `confirmed | inconclusive`：

- 评测通过：`completed/confirmed`；
- 达到定位上限仍未通过：`completed/inconclusive`；
- 基础设施或节点执行失败：`failed`，没有诊断 outcome。

执行游标为 `analyze | investigate | evaluate | summarize | done`。任意时刻最多有一个 `pending_*`；等待态必须有对应 pending，running 和终态不得有 pending；`completed` 必须有 outcome；`done` 只用于终态。

## 单步推进

Graph 每次只计算一个确定性转换：

1. Analyze 需要澄清则等待用户，否则进入 Investigate。
2. Investigate 开始新定位时增加 attempt；澄清恢复不增加 attempt。
3. Evaluate 通过后设置 confirmed；未通过且有剩余次数则回到 Investigate；达到上限后设置 inconclusive。
4. Summarize 只转写结构化结果，随后进入 `done/completed`。

Runtime 在每一步前加载检查点和不可变配置快照，在每一步后原子提交新状态与 Event。Graph 不执行 I/O，不直接提交数据。

## 暂停与恢复

节点返回 `UserInteractionRequest` 时，Runtime：

1. 保存 pending request；
2. 设置 `waiting_user`；
3. 提交 `InputRequired` 和 `RunWaiting`；
4. 结束当前短执行。

回答 Command 必须匹配 run revision、request ID 和问题 ID。答案经严格校验与脱敏后，Runtime 清除 pending、增加澄清轮数，并把 `ClarificationInput` 交回同一来源节点和同一 SDK Session。

断线恢复先读取 Run 快照，再从最后确认的 sequence 追赶 Event。事件读取是有界历史查询，返回当前已有事件后关闭；空结果不是失败。

## 幂等、并发与事务

- `command_id` 全局幂等，重复 Command 返回原结果，不重复推进。
- 非创建 Command 校验 run、revision、生命周期和 pending ID。
- 同一 run 的 Event sequence 严格递增，客户端使用 event ID/sequence 去重。
- 状态、Command 幂等记录和 Event 同事务提交。
- revision 提供乐观锁；短租约防止两个执行者同时推进同一 run。
- 每次 run 保存配置 snapshot ID；恢复不读取已变化的当前配置。

稳定错误至少包括 `run_not_found`、`revision_conflict`、`invalid_run_status`、`pending_request_mismatch` 和 `validation_failed`。`RunFailed` 只公开稳定 code、脱敏 message、node、retryable 和时间。

## 兼容与测试

同一 major 协议版本只能增加可选字段或事件；删除字段或改变语义需要 major version。客户端忽略未知可选 Event，服务端拒绝未知 Command。

测试至少覆盖：

- Command 重放不重复执行；错误 revision/request ID 被拒绝；
- 暂停后可由新进程恢复，且不重做已提交节点；
- 两个执行者不能并发推进同一 run；
- Evaluate 重试复用原 Investigate Session；
- 评测不足进入 `completed/inconclusive` 而非 `failed`；
- Graph 测试不启动 HTTP，也不调用真实模型。
