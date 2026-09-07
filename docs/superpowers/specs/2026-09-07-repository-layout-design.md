# BugLens 前后端目录与文档分离设计

## 目标

将仓库明确拆分为独立的 `frontend/` 和 `backend/` 两个工程边界：前端源码、依赖、构建配置和前端文档只位于 `frontend/`；Python 后端源码、测试、运行配置、打包配置和后端实现文档只位于 `backend/`。根目录只保留仓库级公共内容，包括安装、启动、使用说明、跨前后端契约、发布编排、变更记录和协作约束。

本次整理不改变 BugLens 的运行时行为、HTTP/SSE 协议、CLI 入口或 Agent Graph；只调整文件归属、构建入口和文档导航。

## 当前问题

- Python 后端的 `app/`、`tests/`、`config/` 和 `pyproject.toml` 与前端工程并列在仓库根目录，工程边界不够直观。
- 根级 `docs/` 混合了跨边界协议、后端运行时规范和管理控制面规范。
- 构建、Docker、CI 和 README 中存在基于旧路径的命令与链接，需要在迁移后统一更新。
- `TESTING_REPORT.md` 是一次性真实模型测试记录，不属于长期公共使用文档；`TODO.md` 是仍需保留的项目待办，不删除。

## 目标目录

```text
BugLens/
├── README.md                         # 公共安装、启动、使用和开发入口
├── LICENSE
├── AGENTS.md
├── TODO.md                           # 保留的项目待办
├── .gitignore
├── .github/                          # 仓库级 CI 与自动化
├── docs/                             # 跨前后端公共架构与对接契约
│   ├── architecture.md
│   ├── agent-protocol-spec.md
│   ├── agent-runtime-adapter-spec.md
│   └── frontend-backend-contract.md
├── frontend/                         # React/Vite 前端独立工程
│   ├── src/
│   ├── docs/
│   ├── package.json
│   └── ...
├── backend/                          # Python 后端独立工程
│   ├── app/
│   ├── tests/
│   ├── config/
│   ├── docs/
│   ├── pyproject.toml
│   ├── README.md
│   └── .env.example
├── distribution/                     # 公共安装、发布与 Compose 编排
└── changelog/                        # 仓库级变更记录
```

## 文件归属与迁移

### 前端

现有 `frontend/` 保持目录名和内部工程结构不变。它继续包含 React/Vite 源码、TypeScript 配置、Node 依赖锁定文件、前端 Dockerfile、Nginx 配置和 `frontend/docs/` 下的前端产品、运维交互及 Admin API 使用文档。

### 后端

以下内容迁移到 `backend/`：

- `app/`：CLI、Web Adapter、Application Service、Runtime、Graph、Agent 和协议实现；
- `tests/`：后端测试；
- `config/`：后端运行配置示例；
- `pyproject.toml`：Python 包元数据、依赖、入口和测试工具配置；
- `.env.example`：后端环境变量示例；
- 根级 `docs/admin-control-plane-spec.md`、`docs/lifecycle-resume-spec.md`、`docs/runtime-config-spec.md`、`docs/sdk-capability-spec.md`：后端实现与运行时规范；
- 新增 `backend/README.md`：后端独立安装、开发、测试和打包说明。

后端包名、Python 模块名和入口保持不变：`app.cli:main`、`app.server:main`，因此 CLI 命令和导入路径不因目录迁移而改变。

### 根级公共内容

根目录保留仓库级 `README.md`、`LICENSE`、`AGENTS.md`、`.gitignore`、`TODO.md`、`.github/`、`distribution/` 和 `changelog/`。根级 `README.md` 只描述整体产品、前后端安装方式、启动方式、用户使用方式和指向各工程文档的入口，不重复后端或前端内部实现细节。

根级 `docs/` 保留 `architecture.md`、`agent-protocol-spec.md` 和 `agent-runtime-adapter-spec.md`，并新增 `frontend-backend-contract.md`。新增契约文档定义：

- HTTP Command 的请求方法、路径、JSON 信封、幂等字段和错误语义；
- SSE Event 的事件信封、`event_id`、`sequence`、`Last-Event-ID` 续传和终态规则；
- Run 快照与 `available_actions` 的前端展示边界；
- `/v1/admin/*` 公共管理查询/变更接口的鉴权、版本和冲突处理；
- CORS、反向代理、SSE 缓冲和独立部署要求；
- 哪些状态由后端负责判定，哪些内容前端不得复制或推断。

## 构建与路径调整

- 后端开发命令以 `backend/` 为工作目录执行，例如 `python -m pip install -e ".[dev]"`、`python -m pytest -q`、`python -m build`。
- 根级 README 提供从仓库根目录执行的等价命令，例如 `python -m pip install -e "backend[dev]"` 不作为默认假设；由于 editable 安装和 PEP 517 构建需要以后端目录为项目根，文档明确使用 `Set-Location backend` 或 `python -m build backend` 的可验证方式。
- `.github/workflows/ci.yml` 进入 `backend/` 工作目录，Ruff、pytest 和 build 只扫描后端目录；前端 job 进入 `frontend/` 执行锁定依赖安装、typecheck 和 build。
- `distribution/Dockerfile.backend` 以仓库根为构建上下文时从 `backend/` 复制 `pyproject.toml`、后端 README、许可证、`app/` 和 `config/`，并在镜像内以 `/opt/buglens` 构建后端包。
- `frontend/Dockerfile` 仍以 `frontend/` 为构建上下文，Compose 的服务名和镜像名保持兼容。
- 根 README、后端 README、前端 README、变更记录和所有 Markdown 内的旧路径链接全部同步更新。

## 删除范围

删除根级 `TESTING_REPORT.md`，因为它是一次性环境与真实模型联调记录，不是产品使用、安装或长期架构文档。相关长期约束以代码、测试、`changelog/` 和后端规范为准。

明确保留根级 `TODO.md`，不把待办误判为废弃文档。

除 `TESTING_REPORT.md` 外，不依据文件名批量删除文档；只有确认内容已被新的公共契约或工程专属文档完整覆盖时，才允许在迁移中移除重复内容。

## 运行时与兼容性约束

- 不复制或重写 Graph、Application Service、Runtime、Command/Event 协议逻辑。
- `frontend/` 仍通过 HTTP JSON、HTTP Command 和 SSE Event 访问后端，不导入 Python 代码。
- `backend/` 不依赖 Node.js；后端测试不调用真实模型。
- `distribution/` 仍是公共发布层，只引用后端和前端制品，不把两套依赖合并。
- 迁移后 `buglens`、`buglens-web`、backend-only Compose、full Compose 和前端独立构建均保持可用。

## 验收标准

1. 根目录不再直接包含 `app/`、`tests/`、`config/` 或 Python `pyproject.toml`；前端仍只存在于 `frontend/`。
2. 根 README、`backend/README.md`、`frontend/README.md` 和根级契约文档能分别说明公共使用、后端开发、前端开发和前后端边界。
3. `rg` 检查不存在指向已迁移旧路径的失效文档、Docker 或 CI 引用。
4. 后端 Ruff、pytest、Python package build 和前端 typecheck/build 全部通过。
5. `TESTING_REPORT.md` 被删除，`TODO.md` 保留，其他有效架构、协议和变更记录可通过文档入口访问。
