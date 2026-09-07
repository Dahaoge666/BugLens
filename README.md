# BugLens

BugLens 是一个基于 OpenAI Agents SDK 的故障定位项目。当前版本提供交互式 CLI，依次使用分析、定位、评测和总结四个 Agent，并在当前终端进程中完成必要澄清。

目标架构将增加共享 Agent Runtime、可恢复检查点和 Web/API Adapter；CLI 与 Web 将作为同一 Runtime 的薄入口。下文“使用”和“手工测试”描述当前已实现版本，不代表目标能力已经完成。

## 安装

需要 Python 3.11 或 3.12。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:OPENAI_API_KEY = "你的密钥"
```

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

## SDK 使用方式

- 四个节点使用 SDK `Agent` 和 Pydantic `output_type`。
- 所有执行通过 SDK `Runner.run()`，并限制 `max_turns`。
- 每个诊断节点使用独立的 SDK `SQLiteSession`。同一节点的澄清和重试自动延续会话，不同节点不共享历史。
- 一次完整诊断使用 SDK tracing 归组，`run_id` 作为 `group_id`。
- 当前不使用 handoff：评测失败后的确定性循环仍由 Graph 控制。
- 当前没有证据查询工具；`tools=[]` 保证第一阶段无外部副作用。

当前与目标架构见[架构文档](docs/architecture.md)，SDK 边界见
[SDK 能力采用规范](docs/sdk-capability-spec.md)。目标设计拆分为：

- [Runtime 与 Adapter](docs/agent-runtime-adapter-spec.md)
- [Command/Event 协议](docs/agent-protocol-spec.md)
- [生命周期恢复](docs/lifecycle-resume-spec.md)
- [运行配置与快照](docs/runtime-config-spec.md)

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无 | 真实运行必需 |
| `BUGLENS_SESSION_DB` | `data/buglens.db` | SDK SQLite Session 数据库 |
| `BUGLENS_TRACING` | `true` | 是否启用 SDK tracing |
| `BUGLENS_PROMPT_CONFIG` | 无 | 可选租户提示词 YAML |

租户配置模板位于 `config/tenant_prompts.example.yaml`。

## 测试与打包

```powershell
.\.venv\Scripts\python.exe -m ruff check app tests
.\.venv\Scripts\python.exe -m ruff format --check app tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m build
```

构建产物位于 `dist/`。当前实现支持用户输入的文本证据，尚未实现共享 Runtime、Web Adapter、业务检查点，也未接入日志、指标或 Trace 等只读工具。

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

当前 CLI 仍兼容 `--max-attempts` 和 `--max-clarifications`，但它们属于待废弃参数；默认值已经是两次定位和两轮澄清。目标版本由配置 profile 决定固定策略，并把解析结果保存为运行配置快照。

当前版本如果 CLI 提问，直接在 `>` 后输入对应信息并回车。每个问题会同时显示提问原因；最多进行两轮澄清。当前退出码 `0` 表示评测通过，`2` 表示证据不足或评测未通过。目标 Runtime 实现后将使用独立 lifecycle/outcome 字段，等待态和诊断未决不再混用同一状态：

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
