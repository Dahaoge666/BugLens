# BugLens Agent Command/Event 协议规范

## 范围

本文只定义 Adapter、Application Service 与 Runtime 的稳定协议。Command 是请求，Event 是已提交事实，二者不得混用。HTTP DTO 和 CLI 参数必须先转为协议对象，Runtime 返回 `AsyncIterator[AgentEvent]`。

管理控制面 `/v1/admin/*` 使用独立的 JSON 查询/变更 DTO，不属于 AgentCommand/Event 流；它只能访问
确定性的配置和运维元数据，不能绕过 Application Service 调用 Graph。具体字段见
[Admin 控制面规范](admin-control-plane-spec.md)。

## 信封与类型

```python
class AgentCommand(StrictModel):
    protocol_version: Literal["1"] = "1"
    command_id: str
    run_id: str
    expected_revision: int | None
    submitted_at: datetime

class AgentEvent(StrictModel):
    protocol_version: Literal["1"] = "1"
    event_id: str
    run_id: str
    sequence: int
    revision: int
    occurred_at: datetime
```

首期 Command：`StartDiagnosis(question, context, evidence, profile)`、`SubmitUserAnswers(request_id, answers)`、`CancelDiagnosis(reason)`。未来增加 `SubmitToolResult` 和 `DecideApproval`。固定策略参数不属于 StartDiagnosis。

首期持久化 Event：`RunStarted`、`NodeStarted`、`NodeCompleted`、`InputRequired`、`RunWaiting`、`RunCompleted`、`RunFailed`、`RunCanceled`。未来增加工具和审批事件。

`AssistantDelta`/`NodeProgress` 是可选瞬时事件，可以丢失或关闭，不得参与恢复判断。持久化 Event 必须与状态转换同事务提交。

## 规则

- command ID 全局幂等；非创建 Command 校验 run、revision、等待态和 pending ID；
- 同一 run 的 Event sequence 严格递增，客户端按 event ID 去重并可从任意 sequence 续传；
- Event 只描述已提交事实，不携带 SDK 内部对象、完整 RunState 或 secret；
- InputRequired 包含结构化问题；RunWaiting 包含等待种类和 revision；RunCompleted 包含 outcome 和摘要；
- RunFailed 只公开稳定 code、脱敏信息和 retryable；
- 协议模型 `extra="forbid"`，持久化 protocol version。

错误码至少包括 `run_not_found`、`revision_conflict`、`invalid_run_status`、`pending_request_mismatch` 和 `validation_failed`。HTTP status 与 CLI exit code 由 Adapter 映射。

同一 major version 只增加可选字段/事件；删除字段或改变语义需要 major version。客户端忽略未知可选 Event，服务端拒绝未知 Command。

## 验收标准

- CLI/Web 对相同输入生成同 Schema Command；本地/远程 Client 消费同 Schema Event；
- Command/Event 重放确定且可测试；Event Stream 可断点续传；
- 协议中不存在 Graph、SDK Session 或 HTTP 框架对象。
