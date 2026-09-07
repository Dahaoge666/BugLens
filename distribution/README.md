# BugLens 原生安装与运行

`distribution/` 提供不依赖 Docker 的跨平台安装层。它使用 uv 管理 Python 和隔离环境，并在 `.runtime/` 中保存配置、SQLite 数据、日志和 PID 文件，不修改系统 Python 环境。

## 前置条件

- [uv](https://docs.astral.sh/uv/getting-started/installation/)；uv 会按需获取 Python 3.11，不要求系统预装 Python 或 `python3-venv`；
- backend 模式不需要 Node.js；
- full 模式优先使用预构建的前端静态制品。源码目录没有 `frontend/dist/` 时，才需要 Node.js、Corepack 和 pnpm 进行一次构建。

正式发布时应同时提供 Python wheel 和 `frontend-static` 制品，使最终用户只需安装 uv，不需要 Docker、Node.js 或手工管理 Python 环境。

## 初始化配置

Windows PowerShell：

```powershell
.\distribution\buglensctl.ps1 init
```

macOS/Linux：

```bash
bash ./distribution/buglensctl.sh init
```

首次执行会生成 `distribution/.runtime/.env` 和 `config.yaml`。在 `.env` 中设置 `OPENAI_API_KEY` 或 `OPENAI_BASE_URL`；数据默认保存在 `.runtime/data/buglens.db`。

## 安装

只安装并启动 API：

```powershell
.\distribution\buglensctl.ps1 install backend
```

安装完整 Web：

```powershell
.\distribution\buglensctl.ps1 install full
```

Bash 使用等价命令：

```bash
bash ./distribution/buglensctl.sh install --mode backend
bash ./distribution/buglensctl.sh install --mode full
```

backend 默认地址为 `http://127.0.0.1:8000`。full 默认地址为 `http://127.0.0.1:8080`，原生静态服务器会将同源 `/v1/*` 请求转发到后端，包含 SSE 流。

## 生命周期管理

```powershell
.\distribution\buglensctl.ps1 status
.\distribution\buglensctl.ps1 stop
.\distribution\buglensctl.ps1 start
.\distribution\buglensctl.ps1 restart
.\distribution\buglensctl.ps1 doctor
.\distribution\buglensctl.ps1 update
.\distribution\buglensctl.ps1 uninstall
```

Bash 的命令名相同。`update` 会先停止进程并备份 `.env`、配置和 SQLite 数据，再更新独立环境并启动；`uninstall` 删除程序环境、前端副本、日志和 PID 文件，但保留配置与数据。

## 目录边界

```text
distribution/.runtime/
├── .env                  # 启动配置和凭据，不提交版本库
├── config.yaml           # 可写运行 profile
├── data/buglens.db       # Checkpoint 与 SDK Session
├── frontend/             # 已安装的静态制品，仅 full 模式
├── logs/                 # backend/frontend 日志
├── pids/                 # 原生进程 PID
└── venv/                 # 隔离的 Python 环境
```

## 生产部署

内置进程管理器面向本机、开发和单机内网使用。正式 Linux 服务器建议使用同一 wheel 和静态制品，但将：

- `buglens-web` 交给 systemd 管理；
- 前端静态文件和 `/v1/*` 反向代理交给现有 Nginx/Caddy；
- `.env`、配置和 SQLite 放到受限的持久目录；
- TLS、日志轮转、备份和进程用户交给操作系统设施。

这仍然不需要 Docker。当前 SQLite Store 适合单实例；在切换共享持久化之前，不要水平启动多个后端实例。
