# TODO

本轮遗留项已完成。核心修复（changelog 0001 的 #1–#8）、生命周期实现、只读工具审批恢复和发布前验证均已落地；后续新增事项再在此记录。

## 测试

- [x] **兼容网关契约测试**：新增基于 `httpx.MockTransport` 的真实 Agents SDK 路径测试，覆盖自定义端点的 Chat Completions、流式请求、网关忽略 `response_format` 时的结构化输出解析和 JSON coercion。
- [x] **`_schema_hint` / `_install_json_coercion` 单测**：
  - coercion 表驱动：` ```json{...}``` `、首尾带解释文本、合法 JSON 不被破坏、无 JSON 时安全降级。
  - `_schema_hint` 对四个 `output_type` 均生成非空 JSON Schema 片段且包含 `properties`。
  - 幂等性：多次导入 `app.agents` 不会重复包装 `validate_json`。
- [x] **`streaming` 开关单测**：锁定 `Runner.run_streamed` / `Runner.run` 的分流行为，并保留 SDK Session 回归覆盖。
- [x] **多模型解耦路径单测**：覆盖专用 `OpenAIChatCompletionsModel` 的端点、凭据、超时、缓存复用和 per-model streaming 覆盖。
- [x] **admin API key 脱敏 / 回填单测**：覆盖公开响应脱敏、空/掩码值回填和新值覆盖。

## 后端健壮性

- [x] **列表端点不应因单个损坏 run 而 400**：列表序列化现在跳过损坏行并返回 `degraded_count`，不会因单条非法状态拖垮 Admin API；同时保留状态机校验和回归测试。

## 可观测性

- [x] **`reasoning_content` 纳入可追溯性**：节点审计保存 provider 明确释放的、脱敏且有界的 reasoning summary；不保存 raw/encrypted content，并有回归测试确认隐藏推理内容不会落库。

## 发布前检查

- [x] 后端完整测试、ruff、格式检查和可构建性验证。
- [x] 前端 typecheck 和 production build。
- [x] 安装入口、CLI 帮助和副本 SQLite 数据库迁移/恢复演练。

## 真实模型端到端验证（待执行）

以下步骤需要人工使用真实模型执行，不纳入 CI，也不应使用生产数据。每次测试使用脱敏问题、专用 API Key、独立 SQLite 文件和唯一 `run_id`；默认 profile 的只读工具为关闭状态，不会执行修复、发布、重启或其他副作用操作。

### 1. 准备运行环境

在 PowerShell 中执行：

```powershell
Set-Location backend
uv sync --locked --extra dev --extra web

$env:OPENAI_API_KEY = "<专用测试 Key>"
$env:BUGLENS_SESSION_DB = "data/e2e-real-model-<日期>.db"
$env:BUGLENS_PROFILE = "default"
# 使用兼容网关时再设置；OpenAI 官方端点可留空
# $env:OPENAI_BASE_URL = "https://<gateway>/v1"

uv run buglens --help
```

确认模型/网关支持 Agents SDK 所需的结构化输出、工具调用和 handoff。测试结束后检查输出文件、终端日志和 SQLite 中没有 API Key、完整模型消息或其他 secret。

### 2. CLI 基本链路：分诊 → 类别定位 → 独立评测 → 总结

使用包含可追溯证据、但不含敏感信息的问题：

```powershell
uv run buglens `
  "生产环境 order-api 的 POST /orders 从 350ms 升至 8s，部分请求超时" `
  --context environment=production `
  --context service=order-api `
  --evidence log="timeout waiting for database connection; trace_id=e2e-1001" `
  --evidence metric="db_pool_wait_seconds p95=7.8" `
  --json `
  --output data/e2e-basic.json

$state = Get-Content data/e2e-basic.json -Raw | ConvertFrom-Json
$state | Select-Object run_id,lifecycle_status,outcome,current_node,review_count,active_agent
```

验收：

- `lifecycle_status=completed`，`current_node=done`，`outcome` 只能是 `confirmed` 或 `inconclusive`；两者都必须有 `report`。
- `confirmed` 必须包含独立评测和证据引用；若评测不通过或证据不足，必须是 `inconclusive`，不能把假设写成已确认根因。
- `report.evidence_ids` 只能引用输入或只读工具返回的证据 ID；`limitations` 和 `next_actions` 应能说明仍需验证的内容。
- CLI 返回码：确认结果为 `0`，信息不足的已完成结果为 `2`；后者不是基础设施失败。

至少换两类问题重复一次（例如数据库连接池、网络/依赖服务），记录模型是否把问题交给正确的类别 Investigator，以及 `active_agent`/`review_count` 是否符合预期。

### 3. HTTP/SSE、断线追赶和澄清恢复

在第二个终端使用同一组环境变量启动服务：

```powershell
uv run buglens-web --host 127.0.0.1 --port 8000
```

在第一个终端创建一个故意缺少关键环境信息的问题，保留唯一的 `run_id`：

```powershell
$runId = "e2e-http-$(Get-Date -Format yyyyMMddHHmmss)"
$body = @{
  protocol_version = "2"
  command_id = "cmd-$runId"
  run_id = $runId
  command_type = "start_diagnosis"
  question = "订单服务偶发超时，尚未确认发生在哪个依赖"
  context = @{ environment = "staging"; service = "order-api" }
  evidence = @(@{ source = "log"; content = "request timeout after 5s"; reference = "e2e-2001" })
  profile = "default"
} | ConvertTo-Json -Depth 10

curl.exe -N -H "Content-Type: application/json" `
  -d $body "http://127.0.0.1:8000/v1/runs"

$view = Invoke-RestMethod "http://127.0.0.1:8000/v1/runs/$runId"
$view | ConvertTo-Json -Depth 30
```

若快照为 `waiting_user`：

1. 保存 `pending_interaction.request_id`、`revision` 和每个问题的 `id`，不要猜测 ID。
2. 关闭 SSE 连接后，用 `GET /v1/runs/{run_id}` 重新读取快照；状态应仍为 `waiting_user`。
3. 使用快照中的 request/revision 提交 `submit_user_answers`，答案中的 `request_id` 和 `question_id` 必须逐一匹配：

```powershell
$request = $view.pending_interaction
$answerBody = @{
  protocol_version = "2"
  command_id = "answer-$runId"
  run_id = $runId
  expected_revision = $view.revision
  command_type = "submit_user_answers"
  request_id = $request.request_id
  answers = @(@{
    request_id = $request.request_id
    question_id = $request.questions[0].id
    answer = "发生在 staging，依赖为 PostgreSQL；可提供连接池使用率和慢查询统计"
  })
} | ConvertTo-Json -Depth 10

curl.exe -N -H "Content-Type: application/json" `
  -d $answerBody "http://127.0.0.1:8000/v1/runs/$runId/commands"
```

验收：

- 回答后进入 `running` 并最终完成；澄清来源为 Evaluate 时，`resume_node` 必须为 `investigate`。
- 前后两次快照的 SDK Session ID 应保持为 `{run_id}:diagnosis`；澄清只追加业务输入，不复制或手工回放模型消息。
- 用 `GET /v1/runs/{run_id}/events?after=<最后确认的 sequence>` 追赶断线事件，`sequence` 严格递增且不重复；重复提交同一个 `command_id` 不应重复推进运行。
- 若模型没有提出澄清，记录为“未触发该分支”，不要为了通过测试伪造 `waiting_user` 状态。

### 4. 原生 loop 与审计检查

对已完成的 `run_id` 查询：

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/v1/admin/runs/$runId/executions" | ConvertTo-Json -Depth 30
Invoke-RestMethod "http://127.0.0.1:8000/v1/admin/runs/$runId/sessions" | ConvertTo-Json -Depth 30
```

确认：

- 运行中可观察到 Triage/Analyze → 对应类别 Investigator → `review_diagnosis` 独立 Evaluator 的结果；`review_count` 至少为 1，最多不超过 profile 上限。
- 生产原生路径通常在一个 `analyze` 或 `investigate` 应用层 turn 内完成 handoff 和评测，不能要求出现四个独立 SDK Session；唯一原生 Session 是 `{run_id}:diagnosis`，Evaluator 的 `as_tool()` 调用边界通过结果和审计体现。
- execution/session 记录包含脱敏的输入摘要、配置快照 ID、状态和时间；不包含 API Key、SDK RunState、完整对话或隐藏推理内容。

### 5. 可选失败、Resume 与工具审批

- 将测试网关临时切换为不可达地址或使用受控短超时，确认运行进入 `failed` 且 `resume_available=true`；恢复前重新读取快照，执行 `resume_diagnosis`，确认沿用原 `run_id`、配置快照和 Session。
- 只有在显式启用只读工具、配置 `BUGLENS_RUN_STATE_KEY` 后，才测试 `waiting_approval` → `approve_tool`/`reject_tool`；检查客户端看不到 SDK RunState，批准/拒绝后不会重复工具调用。

### 6. 记录结果

- [ ] E2E-01 基本链路（问题类别：____，run_id：____，outcome：____）
- [ ] E2E-02 HTTP/SSE 断线追赶与幂等（run_id：____）
- [ ] E2E-03 Analyze/Investigate 澄清恢复（run_id：____）
- [ ] E2E-04 评测不通过时的 `inconclusive` 门禁（run_id：____）
- [ ] E2E-05 失败后 Resume（run_id：____）
- [ ] E2E-06 工具审批恢复（仅启用审批工具时，run_id：____）

记录执行日期、provider/model、配置版本、运行 ID、返回码、最终状态、事件序号范围和发现的问题；不要把 API Key、原始敏感日志或完整模型消息提交到仓库。
