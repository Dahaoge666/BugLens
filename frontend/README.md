# BugLens Frontend

本目录用于 BugLens 的独立 Web 前端。前端源码、测试、构建配置和前端文档都只放在
`frontend/` 下，不向仓库根目录或 `backend/` 写入前端依赖与生成文件。

当前目录包含可执行的 React/Vite 前端 MVP，以及与后端隔离的前端设计文档：

- [产品与前端架构设计](docs/product-and-architecture.md)
- [安装、配置与任务管理交互设计](docs/operations-and-management-ux.md)
- [Admin API 对接契约](docs/admin-api-contract.md)

本地运行：

```bash
pnpm install
pnpm dev                 # http://localhost:5173，/v1 由 Vite 代理到 8000
pnpm typecheck
pnpm build
```

生产安装可以直接执行本目录的安装器：

```powershell
.\install.ps1
```

```bash
bash ./install.sh
```

安装器会在没有现成 `dist/index.html` 时自动执行锁文件安装和生产构建，再把静态产物复制到输出目录。它只负责前端，不启动后端；根目录的一键脚本会自动调用它并启动前后端代理。

生产静态构建可直接部署 `dist/`。原生 full 安装由 `distribution/spa_server.py` 托管静态文件，
并将同源 `/v1/` 请求转发到独立后端；正式服务器也可使用已有 Nginx/Caddy 完成相同职责。

当前实现遵守以下边界：

- 前端只依赖公开的 HTTP Command / SSE Event 协议；
- 对 `waiting_approval` 只展示脱敏的工具名/参数，并通过 `approve_tool` 或 `reject_tool` Command 操作；不读取 SDK RunState；
- 不导入 Python 模块，不复制 Agent Graph 或 Runtime 逻辑；
- 前端可独立构建为静态资源，后端无需 Node.js；
- 后端继续支持 CLI 和其他 API 客户端独立使用；
- 前端本地开发、测试、构建和文档命令均从本目录执行。

前后端 HTTP/SSE 与 Admin JSON 的稳定边界见根目录的[前后端对接契约](../frontend-backend-contract.md)。

后续拆分组件时保持以下目录边界：

```text
frontend/
├── docs/                 # 仅前端产品、交互和工程文档
├── public/               # 静态资源
├── src/
│   ├── app/              # 路由、Provider、全局错误边界
│   ├── features/         # 按诊断、澄清、报告等业务切片
│   ├── components/       # 无业务含义的通用组件
│   ├── api/              # HTTP/SSE 客户端和协议 Schema
│   ├── styles/           # Token 与全局样式
│   └── test/             # 测试基础设施
├── e2e/                  # Playwright 端到端测试
├── package.json
├── vite.config.ts
└── tsconfig.json
```
