# BugLens 前后端对接契约

本文是 `frontend/` 与 `backend/` 之间的稳定边界。前端只消费本文定义的 HTTP JSON、HTTP Command 和 SSE Event，不读取 Python 源码、YAML、环境变量、SQLite 或 SDK Session，也不复制 Graph 路由、重试和评测逻辑。

## 服务边界

```text
frontend/ ── HTTP JSON / HTTP Command + SSE Event ──> backend/
                                                   ├─ ApplicationService
                                                   ├─ DiagnosisRuntime
                                                   └─ Agent Graph
```

`backend/` 是业务状态和生命周期的唯一事实来源。网络断开、SSE 关闭或前端刷新不等于诊断失败；前端应重新读取 Run 快照并按 `sequence` 追赶事件。

## 诊断 Command

所有 Command 使用 JSON，并包含以下公共字段：

```json
{
  "protocol_version": "2",
  "command_id": "cmd_123",
  "run_id": "diag_123",
  "expected_revision": 3,
  "submitted_at": "2026-09-07T12:00:00Z",
  "command_type": "start_diagnosis"
}
```

### 创建诊断

`POST /v1/runs`，请求体的 `command_type` 必须是 `start_diagnosis`：

```json
{
  "protocol_version": "2",
  "command_id": "cmd_start_123",
  "run_id": "client_run_123",
  "command_type": "start_diagnosis",
  "question": "订单接口在生产环境持续超时",
  "context": {"environment": "production", "service": "order-api"},
  "evidence": [
    {"source": "log", "content": "timeout waiting for database connection", "reference": "trace_1001"}
  ],
  "profile": "default",
  "target": {
    "mode": "infer",
    "primary_service_id": "order-api"
  }
}
```

`question` 长度为 1–12,000，`evidence` 最多 100 项，`profile` 由后端选择和校验。后端会在进入 Agent 前脱敏输入。

### 继续、跳过、恢复或取消

- `POST /v1/runs/{run_id}/commands`：提交 `submit_user_answers`，请求体包含 `request_id` 和 1–3 个 `answers`；答案必须引用当前 `input_required` 事件中的问题 ID。
- `POST /v1/runs/{run_id}/commands`：提交 `skip_user_interaction`，请求体包含当前 `request_id` 和 `reason`；不伪造答案，运行以信息不可用继续。
- `POST /v1/runs/{run_id}/commands`：提交 `resume_diagnosis`，只允许快照 `available_actions` 包含 `resume` 的运行。
- `POST /v1/runs/{run_id}/commands`：提交 `confirm_diagnosis_target`，携带当前 `target_confirmation_required.request_id`、候选环境 ID、可选主服务 ID 和 `expected_revision`；确认成功后后端保存不可变无凭据环境快照。
- `POST /v1/runs/{run_id}/commands`：`approve_tool` / `reject_tool` 处理已注册只读工具的 SDK 审批中断；必须携带当前 `request_id` 和 `expected_revision`。默认 profile 不暴露需要审批的工具。
- `POST /v1/runs/{run_id}/cancel`：提交 `cancel_diagnosis`，请求体包含非空 `reason`。

Command 使用 `command_id` 实现幂等。`expected_revision` 不匹配时后端拒绝变更并返回冲突，前端应重新获取 Run 快照后再让用户确认。

## SSE Event

创建、继续和事件追赶接口返回 `text/event-stream`。每个 frame 使用事件序号作为 SSE `id`，JSON 事件放在 `data`：

```text
id: 4
data: {"protocol_version":"2","event_id":"evt_123","run_id":"diag_123","sequence":4,"revision":3,"occurred_at":"2026-09-07T12:00:01Z","event_type":"node_attempt_started","specversion":"1.0","id":"evt_123","source":"buglens","subject":"runs/diag_123","type":"com.buglens.node_attempt_started","runid":"diag_123","data":{}}

```

事件公共字段：

| 字段 | 约束 |
| --- | --- |
| `protocol_version` | 新客户端使用字符串 `"2"`；服务端迁移窗口可读取 `"1"` |
| `event_id` | 事件唯一标识，前端按它去重 |
| `run_id` | 所属诊断运行 |
| `sequence` | 从 1 开始递增，前端按它排序和去重 |
| `revision` | 对应 Run 状态版本 |
| `occurred_at` | UTC 时间 |
| `event_type` | 事件类型及其专属字段 |

当前事件类型包括 `run_started`、`target_confirmation_required`、`target_confirmed`、`node_attempt_started`、`node_retry_scheduled`、`node_attempt_failed`、`node_completed`、`tool_call_started`、`tool_call_completed`、`tool_call_failed`、`tool_approval_required`、`tool_approval_resolved`、`input_required`、`input_skipped`、`run_waiting`、`run_resume_available`、`run_resumed`、`run_cancel_requested`、`user_input_submitted`、`run_completed`、`run_failed` 和 `run_canceled`。目标确认前 `lifecycle_status` 为 `waiting_for_target_confirmation`，确认前不暴露环境工具；确认后 Run 快照提供 `target` 和 `environment_snapshot_id`。Graph 固定经过 `analyze`、`investigate`、`evaluate`、`summarize`；前端只展示事件，不自行推进节点。

`input_required` 携带结构化 `request`，前端展示 `explanation`、`questions`、`answer_type` 和 `options`，提交答案时保留 `request_id`。`run_completed` 的 `outcome` 可能是 `confirmed` 或 `inconclusive`；`run_failed` 只表示基础设施或执行失败，不应被渲染成已确认根因。

## Run 查询与断线恢复

- `GET /v1/runs/{run_id}`：获取当前 Run 快照；
- `GET /v1/runs/{run_id}/events?after={sequence}`：获取指定序号之后的已提交事件；
- SSE 断线后，前端先读取快照，再使用最后一个已确认的 `sequence` 追赶事件；
- 合法但没有新事件的追赶请求返回空的 200 SSE 流；Run 不存在返回 404。

Run 快照中的 `lifecycle_status`、`outcome`、`current_node`、`pending_interaction`、`pending_approval`、`revision` 和 `available_actions` 由后端决定。`pending_approval` 只包含脱敏的工具展示参数和请求 ID，不包含 SDK RunState；前端不得根据状态名称自行推断“可重试”“可取消”或 Graph 下一节点。

## Admin JSON API

管理 API 使用独立的 JSON 查询/变更 DTO，不进入 AgentCommand/Event 流：

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v1/admin/bootstrap/status` | 初始化状态 |
| GET | `/v1/admin/health` | 组件健康状态 |
| GET | `/v1/admin/version` | 后端版本和协议版本 |
| GET | `/v1/admin/capabilities` | 能力与可用操作 |
| GET | `/v1/admin/config` | 当前非敏感配置 |
| GET | `/v1/environments` | 可选择环境摘要和目录 revision；不返回凭据 |
| GET | `/v1/admin/plugins` | entry point 发现的插件、版本、能力、Schema 和实例状态 |
| GET | `/v1/admin/environment-config` | 非敏感环境目录；凭据只返回 `is_set` |
| POST | `/v1/admin/environment-config/validate` | 校验目录和插件配置，不落盘 |
| PUT/PATCH | `/v1/admin/environment-config` | If-Match/revision 原子更新目录 |
| POST | `/v1/admin/plugin-instances/{id}/check` | 检查插件实例健康 |
| POST | `/v1/admin/config/validate` | 校验配置但不落盘 |
| PUT/PATCH | `/v1/admin/config` | 按 revision 原子更新配置 |
| GET | `/v1/admin/runs` | 分页查询运行记录；损坏行会被跳过并通过 `degraded_count` 标记 |
| GET | `/v1/admin/sessions` | 查询 Session 元数据 |
| GET | `/v1/admin/runs/{run_id}/sessions` | 查询指定 Run 的 Session |
| GET | `/v1/admin/runs/{run_id}/executions` | 查询节点尝试审计 |
| GET | `/v1/admin/runs/{run_id}/tools` | 查询脱敏工具调用审计 |

配置更新必须发送 `expected_revision`。后端返回 409 时，前端重新读取配置并显示差异；API key 等 secret 只允许写入或清除，永远不会在响应中返回原值。SDK 审批检查点只在后端使用 `BUGLENS_RUN_STATE_KEY` 加密保存，HTTP/SSE 不返回序列化 RunState。

环境目录更新同样使用 opaque `expected_revision`/`If-Match`。`secret_updates` 明确声明 `username`、`password` 或 `token` 的 `set`/`clear`；遗漏表示保留。已创建 Run 使用目标确认时的无凭据环境快照，凭据轮换对下一次插件调用生效。

当设置 `BUGLENS_ADMIN_TOKEN` 时，Admin 请求必须发送 `Authorization: Bearer <token>`；未设置时按部署环境决定是否允许可信内网访问。独立域名部署必须配置精确的 `BUGLENS_CORS_ORIGIN`。

## 部署要求

- 同域部署可由反向代理把 `/` 指向前端静态资源，把 `/v1/` 指向后端；
- 跨域部署必须配置精确 CORS origin，不使用通配符凭据策略；
- SSE 代理必须保留 `text/event-stream`、关闭响应缓冲并设置合理 idle timeout；
- 删除或不构建 `frontend/` 时，后端 CLI、HTTP/SSE API 和 backend-only 原生安装仍必须可用；
- 前端构建不得成为后端 Python 包构建步骤，后端也不得负责打包前端静态资源。
