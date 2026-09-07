# BugLens 开发指南

## 业务目标

BugLens 用于把故障现象和用户提供的证据整理成可审计的诊断报告。用户可通过 CLI 或 Web 提交问题、环境信息与日志等证据；系统依次完成问题分析、原因假设、独立评测和结果总结。输出必须区分“证据支持的结论”“仍需验证的假设”和“信息不足”，不能把推测写成事实，也不执行修复、发布或重启。

一次诊断是可恢复的短执行：运行到完成，或在需要用户补充信息时保存检查点并结束。之后由新 Command 恢复，不长期挂起 CLI 进程或 HTTP 请求。

## 技术选型与目录

| 范围 | 技术与职责 | 目录 |
| --- | --- | --- |
| 后端 | Python 3.11+、Pydantic、OpenAI Agents SDK、SQLite、原生 ASGI/Uvicorn；承载 CLI、API、Application Service、Runtime 和确定性 Graph | `backend/` |
| 前端 | React 19、TypeScript、Vite、原生 `fetch`/`ReadableStream` SSE、pnpm；构建为可独立部署的静态 SPA | `frontend/` |
| 发布 | uv 管理的 Python 环境、静态前端、PowerShell/Bash 安装入口；生产环境可接入 systemd 与现有反向代理 | `distribution/` |

前后端依赖和构建必须独立。后端不需要 Node.js，前端不导入 Python，也不复制 Agent Graph、重试或状态判断逻辑。

## 前后端交互

交互分为两个平面：

- 诊断数据面：前端或远程 CLI 通过 HTTP 提交 `AgentCommand`，通过 SSE 消费 `AgentEvent`，断线后按 `sequence` 追赶；本地 CLI 通过 `LocalAgentClient` 使用相同协议。
- 管理控制面：前端通过 `/v1/admin/*` JSON API 查询健康、版本、能力、配置、运行列表和 Session 元数据。它不进入 Graph，也不返回 secret 或 SDK 消息。

后端 Run 快照是业务状态的唯一事实来源。网络断开不等于运行失败，SSE 关闭不等于诊断完成；前端必须回查快照，且只使用后端返回的状态和可用操作。公开字段、端点、鉴权和恢复规则以根目录的 `frontend-backend-contract.md` 为准。

## 核心实现约束

流程固定为：

```text
AnalyzeAgent → InvestigateAgent → EvaluateAgent
                      ↑                │
                      └── 评测失败 ────┘
                                       │
                                  SummarizeAgent
```

- 四个节点均使用 Agents SDK `Agent` 和 Pydantic `output_type`；通过 `Runner.run()` 执行并设置 `max_turns`。
- Graph 只做确定性状态转换，不读取 stdin/HTTP、不管理数据库；分类路由、最多两次定位、最多两轮澄清和最终状态由代码控制。
- CLI/Web Adapter 必须经过同一个 `ApplicationService` 和 `DiagnosisRuntime`，不得直接调用或复制 Graph。
- 每个 `run_id + node_name` 使用独立 `SQLiteSession`；同一节点澄清/重试复用 Session，不同节点不共享完整模型历史。
- SDK Session 只保存模型消息；`DiagnosisState`、Command、Event、revision 和配置快照由 CheckpointStore 持久化，两者不得混用。
- 每个 run 保存无 secret 的不可变配置快照；恢复时不重新读取当前 profile。
- `completed/inconclusive` 表示业务证据不足，`failed` 只表示基础设施或执行失败。
- 输入进入模型前脱敏。当前 Agent 不注册有副作用的工具；未来工具必须只读、最小权限、有界返回并提供可引用 ID。

## 文档导航（按需阅读）

先读与当前任务直接相关的一份文档：

| 任务 | 文档 |
| --- | --- |
| 安装、启动、基本使用 | `README.md` |
| 修改 HTTP/SSE、Admin API 或前端对接 | `frontend-backend-contract.md` |
| 修改模块职责、Graph、Agents SDK 或依赖方向 | `backend/docs/architecture.md` |
| 修改 Command/Event、生命周期、恢复、并发或持久化 | `backend/docs/runtime-protocol.md` |
| 修改 profile、配置快照、Admin 配置或运维安全 | `backend/docs/configuration-and-operations.md` |
| 后端开发、测试、打包 | `backend/README.md` |
| 前端页面、状态模型和交互 | `frontend/README.md` 及 `frontend/docs/` |
| 原生安装、进程管理与生产发布 | `distribution/README.md` |
| 未完成事项与历史变更 | `TODO.md`、`changelog/` |

不要在多个文档重复定义同一契约：跨端字段归根级契约，后端内部语义归 `backend/docs/`，前端展示与交互归 `frontend/docs/`。

## 验证

测试不得调用真实模型；使用 fake `NodeRunner` 验证路由，并单独验证 SDK Session 参数和严格 Schema。

```bash
cd backend
uv sync --locked --extra dev --extra web
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
uv build

cd ../frontend
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
```
