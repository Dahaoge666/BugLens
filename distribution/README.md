# BugLens distribution

`distribution/` 是安装和发布层，和 Python 后端、React 前端保持独立。它只引用已发布制品或用户显式提供的镜像，不导入任何 Agent Graph 代码。

## 一键安装

在 Windows PowerShell：

```powershell
.\distribution\buglensctl.ps1 install -Mode backend
.\distribution\buglensctl.ps1 install -Mode full
.\distribution\buglensctl.ps1 update
.\distribution\buglensctl.ps1 doctor
```

在 macOS/Linux：

```bash
bash ./distribution/buglensctl.sh install --mode backend
bash ./distribution/buglensctl.sh install --mode full
bash ./distribution/buglensctl.sh update
bash ./distribution/buglensctl.sh doctor
```

脚本要求 Docker Desktop（含 Compose v2）。首次运行会在 `distribution/.runtime/` 创建 `.env` 和数据目录；
`OPENAI_API_KEY` 可以在 `.env` 中设置，也可以预先通过环境变量注入。backend 模式只暴露 HTTP/SSE API，
full 模式另外暴露静态前端（默认 `http://localhost:8080`）。

生产环境请把 `.env` 中的镜像改为固定 digest，并将 `BUGLENS_DATA_DIR` 指向持久磁盘。更新前脚本会备份
配置和 SQLite 文件，Compose 健康检查通过后才切换版本；已有 run 的 config snapshot 不会被覆盖。

## 开发构建

```bash
docker build -t buglens/backend:dev -f distribution/Dockerfile.backend .
docker build -t buglens/frontend:dev frontend
BUGLENS_BACKEND_IMAGE=buglens/backend:dev BUGLENS_FRONTEND_IMAGE=buglens/frontend:dev \
  bash ./distribution/buglensctl.sh install --mode full
```

后端镜像只安装 Python package 和 `web` extra；前端镜像只包含 `frontend/` 构建出的静态资源和反向代理。
