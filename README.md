# BugLens

基于 OpenAI Agents SDK 的故障定位工具。四个独立 Agent 负责分析、定位、评测和总结，
由 Python 状态机控制流转。支持结构化澄清、有限重试和 JSON 状态持久化。

## 快速开始

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:OPENAI_API_KEY = "你的密钥"
.\.venv\Scripts\buglens.exe
```

服务默认监听 `http://127.0.0.1:8000`。访问 `/docs` 调试 API，访问 `/health` 检查服务状态。

创建定位任务：

```powershell
$body = @{
  question = "生产环境订单接口从 10:20 开始超时"
  context = @{ environment = "production"; tenant_id = "example-team" }
  evidence = @(@{ source = "log"; content = "timeout waiting for connection" })
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/diagnoses `
  -ContentType application/json -Body $body
```

当状态为 `awaiting_user_input` 时，把响应中的 `request_id` 和问题 ID 提交到 `POST /diagnoses/{run_id}/answers`。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
```

测试模拟模型返回值，同时检查实际 SDK 的严格输出 Schema，不需要 API 密钥。完整配置、Python 包、Docker 和发布流程见[构建与部署指南](docs/build-and-deploy.md)。设计约束见 [AGENTS.md](AGENTS.md)。

## 接口与行为

- `POST /diagnoses`：提交 `question`、可选 `context` 和 `evidence`。
- `GET /diagnoses/{run_id}`：查询已保存状态。
- `POST /diagnoses/{run_id}/answers`：提交 `request_id` 与 `answers`，每项包含 `question_id`、`answer` 和可选 `attachments`。
- `POST /diagnoses/{run_id}/cancel`：取消等待澄清的任务。

默认最多两轮定位和两轮澄清。澄清恢复使用新的 Agent 运行，继续当前定位轮次。
多选题的 `answer` 为选项 JSON 数组字符串；单选题直接填写选项。
未确认根因时主结论为 null；节点执行失败返回 502 和可查询的 run ID。

状态默认保存在 `data/runs/`，运行参数见 `.env.example`，租户提示词模板见 `config/tenant_prompts.example.yaml`。

当前是本地 Phase 1 原型，使用单进程，同一任务依次提交答案。
生产鉴权、并发控制、完整审计、只读数据源接入和真实故障质量评测尚未实现。
