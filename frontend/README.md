# BugLens Frontend

本目录用于 BugLens 的独立 Web 前端。前端源码、测试、构建配置和前端文档都只放在
`frontend/` 下，不向仓库根目录或 `backend/` 写入前端依赖与生成文件。

当前目录包含 React/Vite 静态 SPA。前端文档只维护当前架构与交互：

- [产品与前端架构](docs/product-and-architecture.md)
- [配置与管理交互](docs/operations-and-management-ux.md)
- 多环境选择、目标确认和插件管理的稳定字段见根目录的[前后端对接契约](../frontend-backend-contract.md)及后端的[多环境插件规范](../backend/docs/environment-plugin-spec.md)。

本地运行：

```bash
pnpm install --frozen-lockfile
pnpm dev                 # http://localhost:5173，/v1 由 Vite 代理到 8000
pnpm typecheck
pnpm test                # 配置表单数据转换与隔离回归，不调用模型
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

每个内置插件的主表单支持驱动、MCP、CLI 接入；其他连接器可通过“自定义 MCP / CLI 连接器”创建。接入说明包含协议、适用场景、使用步骤和调用示例，按实例保存并提供给定位节点。CLI 程序需实现 BugLens JSON 协议，MCP 服务需提供对应只读工具；完整说明见[环境与插件交互](docs/operations-and-management-ux.md)。

源码按页面、API 和配置表单职责拆分，保持扁平布局。环境与插件页按“选择环境 → 选择子服务 → 添加插件 → 选择类别 → 选择并配置”操作，支持创建环境和子服务、重复接入与单项配置管理。数据库类别包含 SQLite/PostgreSQL，日志类别包含文件日志/SSH 日志，主机诊断类别包含 SSH 固定检查。常用操作无需编辑 JSON；完整目录维护保留在高级视图。`pnpm typecheck` 同时检查未使用的变量和参数，`pnpm test` 使用 Node 原生测试执行配置转换、服务绑定与隔离回归。其余前端测试待办见根目录 [TODO](../TODO.md)。
