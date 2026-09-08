# BugLens 安装与运行管理

`distribution/` 只负责统一编排和运行管理：它启动后端、托管前端静态文件、代理 `/v1/*`，并保存运行时数据、日志和 PID。后端和前端的安装动作分别位于 `backend/` 与 `frontend/`。

## 推荐入口

在仓库根目录执行：

```powershell
.\install.ps1
```

macOS/Linux：

```bash
bash ./install.sh
```

默认执行 full 模式：同步后端生产环境、安装并构建前端、启动两个进程、等待健康检查通过，并打印访问地址。只安装后端时使用：

```powershell
.\install.ps1 -Mode backend
```

```bash
bash ./install.sh backend
```

源码检出需要：

- `uv`；它会按 `backend/uv.lock` 准备 Python 3.11+ 环境；
- Node.js 20+ 与 Corepack/pnpm；full 模式在没有 `frontend/dist/` 时用于构建前端。

如果正式发布包已经附带预构建的 `frontend-static`，full 模式可以只使用 `uv`，不需要用户安装 Node.js。

## 组件级入口

需要单独安装时，直接进入对应目录：

```powershell
.\backend\install.ps1
.\frontend\install.ps1
```

```bash
bash ./backend/install.sh
bash ./frontend/install.sh
```

后端脚本把隔离环境放在指定 runtime 目录；前端脚本执行 `pnpm install --frozen-lockfile`、`pnpm build`，并把静态产物复制到指定输出目录。根目录脚本会自动按正确顺序调用它们，普通用户不需要分别处理依赖、构建和启动。

## 高级生命周期命令

根目录脚本适合首次安装。需要状态、更新或停止服务时使用统一管理器：

```powershell
.\distribution\buglensctl.ps1 status
.\distribution\buglensctl.ps1 stop
.\distribution\buglensctl.ps1 start
.\distribution\buglensctl.ps1 restart
.\distribution\buglensctl.ps1 doctor
.\distribution\buglensctl.ps1 update
.\distribution\buglensctl.ps1 uninstall
```

Bash 使用等价命令：

```bash
bash ./distribution/buglensctl.sh status
bash ./distribution/buglensctl.sh stop
bash ./distribution/buglensctl.sh start
bash ./distribution/buglensctl.sh restart
bash ./distribution/buglensctl.sh doctor
bash ./distribution/buglensctl.sh update
bash ./distribution/buglensctl.sh uninstall
```

`update` 会先备份 `.env`、配置和 SQLite 数据，再重新同步后端/前端并启动；`uninstall` 只删除安装环境、前端副本、日志和 PID，保留配置与数据。

## 运行时目录

```text
distribution/.runtime/
├── .env                  # 从 backend/.env.example 初始化，不提交版本库
├── config.yaml           # 从 backend/config/buglens.example.yaml 初始化
├── data/buglens.db      # Checkpoint 与 SDK Session
├── frontend/             # 已安装的前端静态制品
├── logs/                 # backend/frontend 日志
├── pids/                 # 原生进程 PID
└── venv/                 # 后端隔离 Python 环境
```

常用运行配置包括 `OPENAI_API_KEY`、`BUGLENS_HOST`、`BUGLENS_PORT`、`BUGLENS_WEB_HOST`、`BUGLENS_WEB_PORT` 和可选的 `BUGLENS_ADMIN_TOKEN`。如果部署显式注册了需要审批的只读工具，还必须设置 `BUGLENS_RUN_STATE_KEY`，并在所有可能接手恢复的进程中保持一致。编辑 `.runtime/.env` 后执行 `restart` 即可生效。

## 生产环境

内置静态代理适合本机、开发和单机内网。正式 Linux 部署建议把 `buglens-web` 交给 systemd，把前端静态文件和 `/v1/*` 交给现有 Nginx/Caddy，并将 `.env`、配置和 SQLite 放到受限的持久目录。当前 SQLite Store 适合单实例，不要在没有共享持久化的情况下水平启动多个后端实例。

发布前可在隔离 runtime 目录执行 `backend/install.py --runtime-root <临时目录>`，确认锁定依赖可安装；随后启动 backend-only 服务并访问 `/v1/admin/health`。更新前先使用 `buglensctl.py update` 的备份流程，验证配置、数据库和加密审批密钥均未被打包进制品。
