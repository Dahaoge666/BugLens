# BugLens

BugLens 是一个基于 OpenAI Agents SDK 的可恢复故障定位项目。`backend/` 提供 CLI、HTTP/SSE API、Application Service、Runtime 和原生 Agents SDK loop（保留兼容 Graph）；`frontend/` 是独立的 React/Vite Web 客户端。两者只通过公开的 Command/Event 与 Admin JSON 契约通信。

## 目录

- `backend/`：Python 后端、CLI、Web API、测试、运行配置和后端规范。
- `frontend/`：React/Vite 前端源码、构建配置和前端文档。
- `backend/docs/`：后端实现、架构、协议和运行规范。
- `frontend-backend-contract.md`：前后端公共对接契约。
- `distribution/`：统一运行管理、静态代理和高级生命周期命令。
- `changelog/`：已落地变更记录；`TODO.md`：项目待办。

## 一键安装并启动

在仓库根目录执行一条命令即可安装、构建、部署并启动前后端。默认安装 full 模式：

```powershell
.\install.ps1
```

macOS/Linux：

```bash
bash ./install.sh
```

脚本会自动创建运行目录、同步后端生产依赖、安装前端依赖、构建 SPA、启动后端和前端代理，并执行健康检查。
只安装后端时使用 `.\install.ps1 -Mode backend` 或 `bash ./install.sh backend`。

源码检出需要预装 `uv`、Node.js 20+ 和 Corepack/pnpm；正式发布如果附带预构建的前端静态制品，full 模式只需要 `uv`。

## 安装后端

后端也可以独立安装。使用 `uv` 选择 Python 3.11+ 环境并按 `backend/uv.lock` 安装依赖：

```powershell
uv sync --project backend --locked --extra dev --extra web
```

真实模型运行前设置 `OPENAI_API_KEY`。后端可独立运行，不需要 Node.js：

```powershell
$env:OPENAI_API_KEY = "你的密钥"
uv run --project backend buglens-web --host 127.0.0.1 --port 8000
```

也可以直接执行 `backend/install.ps1` 或 `bash backend/install.sh`。更完整的后端开发、测试和打包说明见 [`backend/README.md`](backend/README.md)。

## 安装前端

前端使用 Node.js、Corepack 和 pnpm，依赖与构建完全位于 `frontend/`：

```powershell
Push-Location frontend
corepack enable
pnpm install --frozen-lockfile
pnpm dev
Pop-Location
```

生产安装由根目录一键脚本自动调用 `frontend/install.ps1` 或 `frontend/install.sh`，不需要用户分别理解构建和复制静态文件。

Vite 默认在 `http://localhost:5173` 提供页面，并将 `/v1` 请求代理到本地后端 `http://127.0.0.1:8000`。前端也可以单独构建为静态资源：

```powershell
Push-Location frontend
pnpm typecheck
pnpm build
Pop-Location
```

## 一键部署

根目录 `install.ps1`/`install.sh` 默认安装并启动 full 模式；它们会调用 `backend/` 与 `frontend/` 中的组件安装器。
需要状态、停止、更新或卸载时，再使用 [`distribution/README.md`](distribution/README.md) 中的 `buglensctl`。

正式发布提供预构建前端制品时，使用者也不需要安装 Node.js。

## 使用 CLI

```powershell
uv run --project backend buglens "生产环境 order-api 的 POST /orders 从 350ms 升至 8s"
```

可以附加环境、租户和只读证据：

```powershell
uv run --project backend buglens `
  "订单接口大量超时" `
  --context environment=production `
  --tenant example-team `
  --evidence log="timeout waiting for database connection"
```

CLI、Web 和远程客户端都使用同一个 Application Service 与 Runtime；CLI 不复制 Web 或 Graph 逻辑。使用 `buglens --help` 查看全部选项，使用 `--json` 获取结构化结果。

## 配置

后端通过环境变量读取运行配置；密钥不写入诊断状态、配置快照或事件：

| 变量 | 说明 |
| --- | --- |
| `OPENAI_API_KEY` | 真实模型运行必需的凭据 |
| `BUGLENS_SESSION_DB` | SDK SQLite Session 数据库路径 |
| `BUGLENS_CONFIG` | 运行 profile YAML 路径 |
| `BUGLENS_PROFILE` | 默认运行 profile，默认 `default` |
| `BUGLENS_TRACING` | 是否启用 SDK tracing |
| `BUGLENS_PROMPT_CONFIG` | 可选租户提示词配置路径 |
| `BUGLENS_CORS_ORIGIN` | 独立前端域名部署时允许的精确 origin |
| `BUGLENS_ADMIN_TOKEN` | 可选 Admin API Bearer Token |
| `BUGLENS_RUN_STATE_KEY` | 需要审批的只读工具使用的加密恢复密钥；必须在接手恢复的进程间一致 |

后端配置模板位于 [`backend/config/`](backend/config/)，环境变量模板位于 [`backend/.env.example`](backend/.env.example)。

## 文档

- [前后端对接契约](frontend-backend-contract.md)
- [后端架构与 SDK 边界](backend/docs/architecture.md)
- [Runtime、协议与恢复](backend/docs/runtime-protocol.md)
- [配置、Admin 与运维](backend/docs/configuration-and-operations.md)
- [后端开发与测试](backend/README.md)
- [前端开发与交互文档](frontend/README.md)
- [安装与发布层](distribution/README.md)

## 验证

后端和前端分别验证，互不要求安装对方的依赖：

```powershell
uv run --project backend ruff check app tests
uv run --project backend ruff format --check app tests
uv run --project backend pytest -q
uv build --project backend

Push-Location frontend
pnpm typecheck
pnpm build
Pop-Location
```
