# BugLens Agent Runtime 与 Adapter 架构规范

## 范围与现状

本文定义组件职责和依赖方向。生命周期、协议和配置分别见同目录对应 spec。

当前实现已经落地共享的 `ApplicationService`、`DiagnosisRuntime`、CLI LocalClient 和 HTTP/SSE
Web Adapter。`app.cli` 只负责参数、输入和渲染；Web Adapter 只负责 HTTP/SSE 映射。两者都不直接
构造或推进 `DiagnosisGraph`。管理控制面由独立的 `AdminApplicationService` 提供，详见
[控制面规范](admin-control-plane-spec.md)。

## 目标架构

```text
CLI ── LocalAgentClient ─┐
                        ├─ Application Service ─ Agent Runtime ─ Agent Graph
Web ─────────────────────┘                         ├─ NodeRunner
Remote CLI ─ HTTP/SSE ─ Web                       ├─ Checkpoint/Event Store
                                                  └─ SDK Session
```

CLI 和 Web 不得直接调用 Graph。Graph 可替换，只要 Runtime 与 Agent Protocol 保持兼容。

## 职责

- Domain：实体、状态、节点结构化模型、错误和不变量，不依赖 transport、数据库或 SDK Adapter。
- Graph：确定性路由、评测、重试和中断决定；接收状态、返回转换。
- Runtime：接收 Command，加载配置/checkpoint，处理幂等与租约，推进 Graph，调用 NodeRunner，原子保存状态并发布 Event，恢复 SDK Session/RunState。
- Application Service：身份、权限、run 创建、profile 选择、配额、审计主体和查询；不实现 Graph 路由。
- Infra：Store、Session factory、Config repository 和未来 queue 的实现。
- Adapter：transport 与协议互转、事件渲染；不保存 checkpoint、不重试 Graph、不拼接模型消息。

核心接口：

```python
class AgentRuntime(Protocol):
    def run(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...

class AgentClient(Protocol):
    def send(self, command: AgentCommand) -> AsyncIterator[AgentEvent]: ...
    async def get_run(self, run_id: str) -> RunView: ...
```

LocalAgentClient 调本地 Service；RemoteAgentClient 用 HTTP 提交 Command、SSE 消费 Event。CLI renderer 不感知执行位置。

## Web 与 CLI

Web 首选 HTTP Command + SSE Event：

```text
POST /v1/runs
POST /v1/runs/{id}/commands
POST /v1/runs/{id}/cancel
GET  /v1/runs/{id}
GET  /v1/runs/{id}/events?after={sequence}
```

HTTP 不跨越长等待；到达等待态即结束短执行。SSE 支持 sequence/Last-Event-ID 续传。只有高频双向控制才增加 WebSocket。

CLI 支持 `run/resume/status/cancel`。收到 InputRequired 可立即采集并发送新 Command，也可退出后恢复。CLI 默认仅接受问题、上下文、证据、run ID、profile 和展示参数，不暴露固定工作流参数。

前端是独立的静态工程，位于 `frontend/`，只消费公开的 Command/Event 和 `/v1/admin/*` JSON
契约。它不导入 Python、Graph、Runtime 或 SQLite；删除 `frontend/` 不改变后端安装和 CLI/API
行为。`distribution/` 只负责引用后端和前端制品，不把两套依赖合并成一个工程。

## 管理控制面边界

Admin Adapter 只读写确定性的配置和运维元数据，不触发诊断节点，也不改变 Graph 路由：

```text
HTTP /v1/admin/* ── AdminApplicationService ── ConfigRepository / CheckpointStore
HTTP /v1/runs/*   ── ApplicationService ─────── DiagnosisRuntime ── Graph
```

当前实现提供健康、版本、能力、配置读取/校验/应用、运行列表和 SDK Session 元数据查询。配置应用
使用 `expected_revision` 乐观锁，并先校验全部 profile，再通过同目录临时文件和 `os.replace` 原子
替换 YAML。API 不返回 SDK 模型消息或 secret；运行中的任务继续使用创建时保存的配置快照。

## 推荐目录

```text
app/
├── domain/{models,state,errors}.py
├── protocol/{commands,events}.py
├── agent/{graph,runtime,nodes,runner}.py
├── application/agent_service.py
├── adapters/{cli,web}.py
├── infra/{checkpoints,events,sessions,config}.py
└── bootstrap.py
```

保持适度分层，不为每个类建文件；`bootstrap.py` 是唯一依赖组装点。依赖方向为 Adapter → Application → Runtime → Graph/Domain，Infra 实现领域端口，禁止反向导入 Adapter。

本仓库当前实现将控制面和可选 HTTP 入口分别放在 `app/admin.py` 与 `app/server.py`；不要求为了
控制面拆出额外的 transport 包。前端源码和文档只能放在 `frontend/`，后端 docs 只描述协议和边界。

## 交付与验收

依次提取 Protocol/配置、改造无交互 Graph、实现 Runtime/持久化、迁移 CLI 到 LocalClient，再增加 HTTP/SSE 和 RemoteClient。

- CLI/Web 不导入或构造 DiagnosisGraph；
- 相同 Command 序列产生等价状态和领域 Event；
- Graph 测试不启动 transport，Adapter 测试使用 fake Client/Service；
- 本地/远程 CLI 共用 renderer；Web 断线不取消或丢失 run。
- backend-only 安装不需要 Node.js；full 安装通过 `distribution/` 启动两个独立制品。

`AGENTS.md` 已同步采用本规范：保留 CLI，同时新增共享 Runtime 的 Web/API Adapter，不允许两套 Agent 实现。
