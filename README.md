# BugLens

BugLens 是一个基于 OpenAI Agents SDK 的可恢复故障定位项目。CLI 和 HTTP/SSE Web Adapter 共享同一个 Application Service、Runtime 和确定性 Agent Graph，依次使用分析、定位、评测和总结四个 Agent。

Runtime 将每次短执行持久化为 checkpoint 和 Event；需要用户补充信息时进入 waiting_user，进程可以退出，之后用新的 Command 恢复。

## 安装

需要 Python 3.11 或 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:OPENAI_API_KEY = "你的密钥"
```

启动 HTTP/SSE 后端（前端可独立部署）：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
.\.venv\Scripts\buglens-web.exe --host 127.0.0.1 --port 8000
```

需要前后端一键安装时，使用独立的 [`distribution/`](distribution/README.md) 安装层；它通过 Docker
Compose 选择 backend-only 或 full 模式，不会把前端依赖写入 Python 工程。

## 使用

直接提交问题：

```powershell
.\.venv\Scripts\buglens.exe "生产环境订单接口从 10:20 开始超时"
```

附加上下文和证据：

```powershell
.\.venv\Scripts\buglens.exe `
  "订单接口大量超时" `
  --context environment=production `
  --tenant example-team `
  --evidence log="timeout waiting for connection"
```

如果 Agent 需要更多信息，CLI 会说明原因并在终端逐项提问。使用 `--json` 可以输出完整结构化结果；运行 `buglens --help` 查看所有选项。

固定策略只能通过 profile 选择，不能由普通请求逐项覆盖：

```powershell
.\.venv\Scripts\buglens.exe "订单接口大量超时" --profile default --config .\config\buglens.example.yaml
```

也支持 `--resume RUN_ID`、`--status RUN_ID` 和 `--cancel RUN_ID`。远程 CLI 使用 `--remote URL`，渲染方式与本地 CLI 相同。

## SDK 使用方式

- 四个节点使用 SDK `Agent` 和 Pydantic `output_type`。
- 所有执行通过 SDK `Runner.run()`，并限制 `max_turns`。
- 每个诊断节点使用独立的 SDK `SQLiteSession`。同一节点的澄清和重试自动延续会话，不同节点不共享历史。
- 一次完整诊断使用 SDK tracing 归组，`run_id` 作为 `group_id`。
- 当前不使用 handoff：评测失败后的确定性循环仍由 Graph 控制。
- 当前没有证据查询工具；`tools=[]` 保证第一阶段无外部副作用。
- `ApplicationService`、`DiagnosisRuntime` 和 `SQLiteCheckpointStore` 负责统一 Command/Event、生命周期、revision、幂等和配置快照。
- `AdminApplicationService` 只负责控制面健康、版本、配置校验/原子更新和运行/Session 元数据查询，不进入 Graph。

当前与目标架构见[架构文档](docs/architecture.md)，SDK 边界见
[SDK 能力采用规范](docs/sdk-capability-spec.md)。目标设计拆分为：

- [Runtime 与 Adapter](docs/agent-runtime-adapter-spec.md)
- [Command/Event 协议](docs/agent-protocol-spec.md)
- [生命周期恢复](docs/lifecycle-resume-spec.md)
- [运行配置与快照](docs/runtime-config-spec.md)
- [Admin 控制面](docs/admin-control-plane-spec.md)

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无 | 真实运行必需 |
| `BUGLENS_SESSION_DB` | `data/buglens.db` | SDK SQLite Session 数据库 |
| `BUGLENS_CONFIG` | 无 | 运行 profile YAML |
| `BUGLENS_PROFILE` | `default` | 默认运行 profile |
| `BUGLENS_TRACING` | `true` | 是否启用 SDK tracing |
| `BUGLENS_PROMPT_CONFIG` | 无 | 可选租户提示词 YAML |
| `BUGLENS_CORS_ORIGIN` | 无 | 可选前端来源；独立域名部署时设置为精确 origin |
| `BUGLENS_ADMIN_TOKEN` | 无 | 可选管理 API Bearer Token；公网部署建议设置 |

租户配置模板位于 `config/tenant_prompts.example.yaml`。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
```

构建产物位于 `dist/`。当前实现支持用户输入的文本证据，Web Adapter 提供 HTTP Command + SSE Event；尚未接入日志、指标或 Trace 等只读工具。

## 端到端手工测试

以下命令适用于 PowerShell。先在一个新的终端进入项目并创建独立环境：

```powershell
cd D:\Project\BugLens
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\buglens.exe --help
```

设置 API Key。输入内容只保存在当前 PowerShell 进程的环境变量中：

```powershell
$env:OPENAI_API_KEY = Read-Host "OPENAI_API_KEY"
$env:BUGLENS_SESSION_DB = "data/manual-test.db"
$env:BUGLENS_TRACING = "true"
```

运行一次包含问题、环境、日志、指标和变更记录的 Bug 检查：

```powershell
.\.venv\Scripts\buglens.exe `
  "2026-09-06 10:20 至 10:45，生产环境 order-api 的 POST /orders P99 从 350ms 升至 8s，并出现超时" `
  --context environment=production `
  --context service=order-api `
  --context time_window="2026-09-06T10:20:00+08:00/2026-09-06T10:45:00+08:00" `
  --evidence log="10:31:08 timeout waiting for database connection; trace_id=tr_1001" `
  --evidence metric="db_pool_active=100, db_pool_max=100, http_p99=8s at 10:31" `
  --evidence change="order-api 2.4.1 deployed at 10:15; database pool max unchanged" `
  --output .\data\manual-result.json
```

最大定位次数、澄清轮数、评测门槛和节点 max turns 均来自 profile，并在创建 run 时保存为不可变运行配置快照。

如果 CLI 提问，直接在 `>` 后输入对应信息并回车。每个问题会同时显示提问原因；最多进行两轮澄清。当前退出码 `0` 表示评测通过，`2` 表示证据不足或评测未通过。Runtime 使用独立的 lifecycle/outcome 字段，等待态和诊断未决不会混用同一状态：

```powershell
$LASTEXITCODE
Get-Content .\data\manual-result.json
$result = Get-Content .\data\manual-result.json -Raw | ConvertFrom-Json
$result | Select-Object run_id, status, attempt, clarification_round
$result.report | Format-List
$result.evaluation | Format-List
```

确认 SDK Session 已创建。正常完成一次流程时应看到同一个 `run_id` 下的 `analyze`、`investigate`、`evaluate` 和 `summarize` 节点会话：

```powershell
Test-Path .\data\manual-test.db
.\.venv\Scripts\python.exe -c "import sqlite3; db=sqlite3.connect(r'data/manual-test.db'); print(*[row[0] for row in db.execute('select session_id from agent_sessions order by session_id')], sep='\n')"
```

也可以要求完整 JSON 直接输出到终端：

```powershell
.\.venv\Scripts\buglens.exe `
  "测试环境 checkout-api 返回 500，日志显示 KeyError: currency" `
  --context environment=test `
  --evidence log="KeyError: currency; trace_id=tr_2001" `
  --json
```

最后运行代码检查、测试和打包，确认本地安装与发布包都可用：

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
.\.venv\Scripts\python.exe -m pytest -q
Remove-Item .\dist\* -Force -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -m build
.\.venv\Scripts\python.exe -m pip install --force-reinstall .\dist\buglens-0.1.0-py3-none-any.whl
.\.venv\Scripts\buglens.exe --help
```

完成后清除当前终端中的 Key，并按需删除手工测试文件：

```powershell
Remove-Item Env:OPENAI_API_KEY
Remove-Item .\data\manual-result.json -ErrorAction SilentlyContinue
Remove-Item .\data\manual-test.db -ErrorAction SilentlyContinue
```
