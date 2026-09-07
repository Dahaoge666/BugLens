# BugLens Agent Runtime 与 Adapter 架构规范

## 范围与现状

本文定义组件职责和依赖方向。生命周期、协议和配置分别见同目录对应 spec。

当前 `app.cli` 直接构造 NodeRunner 和 Graph，并负责输入、生命周期和渲染；尚无 Application Service、Runtime、Web Adapter 或 AgentClient，因此不符合薄 Adapter 目标。

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

## 交付与验收

依次提取 Protocol/配置、改造无交互 Graph、实现 Runtime/持久化、迁移 CLI 到 LocalClient，再增加 HTTP/SSE 和 RemoteClient。

- CLI/Web 不导入或构造 DiagnosisGraph；
- 相同 Command 序列产生等价状态和领域 Event；
- Graph 测试不启动 transport，Adapter 测试使用 fake Client/Service；
- 本地/远程 CLI 共用 renderer；Web 断线不取消或丢失 run。

`AGENTS.md` 已同步采用本规范：保留 CLI，同时新增共享 Runtime 的 Web/API Adapter，不允许两套 Agent 实现。
