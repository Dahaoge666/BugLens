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

Command 公共字段为 `protocol_version`、`command_id`、`run_id`、`expected_revision` 和 `submitted_at`。当前新命令使用协议 v2；服务端在迁移窗口仍可解析 v1：

- `StartDiagnosis`：问题、上下文、证据、profile 和 `target={mode: explicit|infer, environment_id?, primary_service_id?}`；固定策略参数不属于请求。
- `ConfirmDiagnosisTarget`：目标推断后的确认命令，携带 `request_id`、环境 ID、可选主服务 ID 和期望 state revision；不占用澄清轮次。
- `SubmitUserAnswers`：当前 pending request ID 和结构化答案。
- `SkipUserInteraction`：当前 pending request ID、跳过原因和信息不可用标记。
- `ResumeDiagnosis`：从可恢复的 failed 检查点开启新的 retry cycle。
- `CancelDiagnosis`：取消原因；活动节点期间只写入 run control。
- `ApproveTool` / `RejectTool`：处理已注册只读工具的 SDK tool interruption；审批检查点由后端加密保存，客户端只看到脱敏的 pending 展示数据。

Event v2 在上述字段之外提供 CloudEvents 1.0 envelope（`specversion`、`id`、`source`、`subject`、`type`、`time`、`datacontenttype`、`data`、`runid`）。当前持久化事件：

- `RunStarted`、`NodeAttemptStarted`、`NodeRetryScheduled`、`NodeAttemptFailed`、`NodeCompleted`；
- `ToolCallStarted` / `ToolCallCompleted` / `ToolCallFailed`；
- `ToolApprovalRequired` / `ToolApprovalResolved`；
- `InputRequired`、`InputSkipped`、`RunWaiting`、`RunResumeAvailable`、`RunResumed`；
- `TargetConfirmationRequired`、`TargetConfirmed`；
- `RunCancelRequested`、`UserInputSubmitted`、`RunCompleted`、`RunFailed`、`RunCanceled`。

瞬时进度事件可以丢失，不能参与恢复判断。持久化 Event 必须与对应状态转换在同一事务提交，且不得包含 SDK 对象、完整模型消息或 secret。

## 生命周期与结果

生命周期：

```text
created → running ─┬→ waiting_user ─→ running
                  ├→ waiting_tool ─→ running
                   ├→ waiting_approval → running
                   ├→ waiting_for_target_confirmation → running
                  ├→ completed
                  ├→ failed
                  └→ canceled
```

`completed`、`failed`、`canceled` 是终态。诊断结果单独使用 `confirmed | inconclusive`：

- 评测通过：`completed/confirmed`；
- 达到定位上限仍未通过：`completed/inconclusive`；
- 基础设施或节点执行失败：`failed`，没有诊断 outcome。

执行游标仍为 `analyze | investigate | evaluate | summarize | done`，用于兼容事件和恢复；生产原生 loop 通常在一个 `analyze` 或 `investigate` 游标内完成分诊、handoff 和评测。任意时刻最多有一个 `pending_*`；等待态必须有对应 pending，running 和终态不得有 pending；`completed` 必须有 outcome；`done` 只用于终态。

目标确认是独立的等待态：推断只使用后端目录候选，确认成功才保存无凭据 `ResolvedEnvironmentSnapshot` 并向 InvestigateAgent 注册环境工具。确认前工具列表为空；恢复时读取原快照，不读取新目录的 source 集合。

## 应用层 turn

生产路径每个短 Command 至多推进一次原生 Agents SDK loop：

1. `NativeDiagnosisGraph.prepare_step` 从快照组装一个 `NativeDiagnosisInput`，并固定本次 run 的配置、Session ID 和评测 rubric。
2. `NativeDiagnosisRunner` 由 Triage/Analyze 开始；信息不足时返回 `needs_input`，否则用 `handoff` 把 `InvestigationBrief` 交给类别 Investigator。
3. Investigator 可调用只读工具，并以 `Agent.as_tool()` 调用独立 Evaluator；最多两轮评测，代码检查至少一次评测和证据引用。
4. 结构化结果交回 `NativeDiagnosisGraph.apply_result`。通过门禁才是 `confirmed`；评测不通过、未执行或信息耗尽均为 `completed/inconclusive`。

旧 `DiagnosisGraph` 仍按四节点规则推进，供迁移期间的旧 profile 和兼容测试使用。Runtime 在每个应用层 turn 前加载检查点和不可变配置快照，在 turn 后原子提交状态与 Event；Graph 不执行 I/O，不直接提交数据。

## 暂停与恢复

节点返回 `UserInteractionRequest` 时，Runtime：

1. 保存 pending request；
2. 设置 `waiting_user`；
3. 提交 `InputRequired` 和 `RunWaiting`；
4. 结束当前短执行。

回答或 Skip Command 必须匹配 run revision、request ID 和问题 ID。答案经严格校验与脱敏后，Runtime 清除 pending、增加来源节点澄清轮数，并把 `ClarificationInput` 写入下一次 `NativeDiagnosisInput`，再次调用同一个 `{run_id}:diagnosis` SDK Session；Evaluate 来源的回答交回 Investigator。旧 Graph 仍把它交回对应节点 Session。Skip 不伪造答案，而是以 `information_unavailable=true` 继续，并在总结限制中保留记录。

工具审批 Command 必须匹配 run revision 和 pending approval request ID。Runtime 使用 SDK `RunState.from_string()` 恢复加密检查点，批准或拒绝全部待处理 interruption 后，复用原 Agent、节点 Session 和 active execution。决定提交与 `ToolApprovalResolved` Event 原子落库；节点完成后才标记 SDK 检查点 resolved。若进程在决定提交后退出，重复同一 `command_id` 会继续未完成的 SDK 状态，不重复创建节点执行记录。

可恢复的节点/模型/工具错误在单个 retry cycle 内最多自动重试五次（首次调用加五次重试）。耗尽后状态为 `failed` 并提供 `resume`；Resume 沿用原节点 Session 和配置快照。Cancel 不强制终止活动外部调用，只在调用完成后的安全边界提交 `canceled`。

断线恢复先读取 Run 快照，再从最后确认的 sequence 追赶 Event。事件读取是有界历史查询，返回当前已有事件后关闭；空结果不是失败。

## 幂等、并发与事务

- `command_id` 全局幂等，重复 Command 返回原结果，不重复推进。
- 非创建 Command 校验 run、revision、生命周期和 pending ID。
- 同一 run 的 Event sequence 严格递增，客户端使用 event ID/sequence 去重。
- 状态、Command 幂等记录和 Event 同事务提交。
- revision 提供乐观锁；短租约防止两个执行者同时推进同一 run。
- 每次 run 保存配置 snapshot ID；恢复不读取已变化的当前配置。
- 目录确认后的 run 额外保存 environment snapshot ID；当前目录热更新不改变已有 run 的环境边界，凭据则按调用从当前实例配置读取以支持轮换。

稳定错误至少包括 `run_not_found`、`revision_conflict`、`invalid_run_status`、`pending_request_mismatch` 和 `validation_failed`。`RunFailed` 只公开稳定 code、脱敏 message、node、retryable 和时间。

## 兼容与测试

同一 major 协议版本只能增加可选字段或事件；删除字段或改变语义需要 major version。客户端忽略未知可选 Event，服务端拒绝未知 Command。

测试至少覆盖：

- Command 重放不重复执行；错误 revision/request ID 被拒绝；
- 暂停后可由新进程恢复，且不重做已提交节点；
- 两个执行者不能并发推进同一 run；
- 原生 loop 的 Evaluator 通过 `as_tool()` 保持独立调用边界；澄清后 Investigator 复用 `{run_id}:diagnosis` Session；
- 评测不足进入 `completed/inconclusive` 而非 `failed`；
- Graph 测试不启动 HTTP，也不调用真实模型。
