# BugLens Backend

`backend/` 是 BugLens 的独立 Python 工程，包含 CLI、HTTP/SSE Adapter、Application Service、Agent Runtime、确定性 Graph、Admin 控制面和后端测试。它不依赖 Node.js，也不包含前端源码。

## 安装

uv 会按 `uv.lock` 选择可用的 Python 3.11+ 解释器并创建、同步隔离环境：

```powershell
uv sync --locked --extra dev --extra web
```

只安装给原生运行管理器使用的生产后端环境时，执行：

```powershell
.\install.ps1
```

```bash
bash ./install.sh
```

也可以传入自定义 runtime 目录。该脚本只同步后端，不安装 Node.js、不构建前端，也不启动进程；根目录的 `install.ps1`/`install.sh` 会负责按顺序调用后端和前端安装，再启动完整服务。

安装 Web 依赖并启动 HTTP/SSE API：

```powershell
$env:OPENAI_API_KEY = "你的密钥"
uv run buglens-web --host 127.0.0.1 --port 8000
```

## CLI

```powershell
uv run buglens `
  "生产环境订单接口大量超时" `
  --context environment=production `
  --evidence log="timeout waiting for database connection" `
  --json
```

CLI 和 Web 都只调用同一个 `ApplicationService` 与 `DiagnosisRuntime`。一次执行在完成或进入等待用户输入时结束；恢复通过新的 Command 完成，不维持长期挂起的进程或 HTTP 请求。

## 配置

- `config/buglens.example.yaml`：运行 profile 示例；
- `config/tenant_prompts.example.yaml`：租户提示词示例；
- `config/environments.example.yaml`：两个环境、SQLite 和文件日志 source 示例；
- `.env.example`：环境变量示例。

原生 Agents SDK loop 的评测上限、rubric 阈值和各 Agent `max_turns` 来自版本化 profile，并在创建 run 时保存不可变配置快照。生产路径由 Triage/Analyze 通过 handoff 选择类别 Investigator，再以 `Agent.as_tool()` 调用独立 Evaluator；旧确定性 Graph 仍保留兼容测试。

默认工具关闭且不需要审批。接入显式 `needs_approval=True` 的只读工具时，请设置 `BUGLENS_RUN_STATE_KEY`；它用于加密 SDK 恢复状态，必须在跨进程恢复审批的服务实例间一致。

多环境插件规范见 [environment-plugin-spec.md](docs/environment-plugin-spec.md)。参考插件分别位于 `../plugins/sqlite` 和 `../plugins/file-logs`，需要独立构建/安装 wheel；设置 `BUGLENS_ENVIRONMENTS_CONFIG` 后，配置目录仍默认不向 Agent 暴露，必须在 profile 中显式打开只读工具。

## 文档

- [架构与 SDK 边界](docs/architecture.md)：组件职责、原生 loop、handoff/as_tool 与 SDK 能力边界；
- [Runtime、协议与恢复](docs/runtime-protocol.md)：Command/Event、状态机、检查点、幂等与并发；
- [配置、Admin 与运维](docs/configuration-and-operations.md)：配置分层、快照、控制面和部署安全；
- [多环境插件规范](docs/environment-plugin-spec.md)：插件 API、环境目录、目标确认、只读查询、热更新和验收矩阵；
- [前后端对接契约](../frontend-backend-contract.md)：前端可依赖的 HTTP/SSE 与 Admin 公共接口。

## 测试与打包

```powershell
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
uv build
```

测试使用假的 `NodeRunner`，不会调用真实模型。
