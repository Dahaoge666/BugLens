# BugLens Frontend

本目录用于 BugLens 的独立 Web 前端。前端源码、测试、构建配置和前端文档都只放在
`frontend/` 下，不向仓库根目录或 Python 后端目录写入前端依赖与生成文件。

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

生产静态构建可直接部署 `dist/`；使用 `frontend/Dockerfile` 时，Nginx 会把 `/v1/` 反向代理到
`buglens-backend:8000`，前端镜像不包含 Python 代码。

当前实现遵守以下边界：

- 前端只依赖公开的 HTTP Command / SSE Event 协议；
- 不导入 Python 模块，不复制 Agent Graph 或 Runtime 逻辑；
- 前端可独立构建为静态资源，后端无需 Node.js；
- 后端继续支持 CLI 和其他 API 客户端独立使用；
- 前端本地开发、测试、构建和文档命令均从本目录执行。

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
