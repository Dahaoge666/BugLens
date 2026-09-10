# 配置、Admin 与运维边界

本文定义运行配置、不可变快照、Admin 控制面和部署安全。公开 Admin API 的 HTTP 字段与前端行为以根目录 `frontend-backend-contract.md` 为准；本文只描述后端规则。

## 配置分层

| 类型 | 示例 | 生效时机 |
| --- | --- | --- |
| 启动配置 | 数据库、监听地址、日志、默认 profile、CORS、Admin token | 进程启动 |
| 运行策略 | loop/兼容 Graph 上限、评测阈值、Agent model/max turns/prompt、工具限制 | 创建 run 时快照 |
| 请求输入 | 问题、上下文、证据、允许的 profile | 每个 Command |
| 展示选项 | JSON、输出文件、颜色、远程地址 | Adapter 本地 |
| 环境目录 | 连接器实例、环境、服务、节点和可扩展 source（数据库/日志/知识库/流量） | 目录 API 热更新；run 确认时生成环境快照 |

普通 CLI/Web 请求只能选择 profile，不能逐项覆盖固定策略。优先级为代码安全上限 > profile > 环境变量选择的默认 profile > 内置默认值。

## Profile 与模型

版本化 profile 至少包含：

```yaml
profiles:
  default:
    config_version: default-v1
    prompt_config_version: tenant-prompts-v1
    models:
      default: {model: gpt-4.1-mini, timeout: 60}
    graph:
      max_investigation_attempts: 2
      max_clarification_rounds: 2
    evaluation:
      passing_score: 75
      min_evidence_traceability: 15
      min_verification_executability: 15
      rubric_version: rubric-v1
    nodes:
      analyze: {model: default, max_turns: 6, prompt_version: analyzer-v1}
      investigate: {model: default, max_turns: 6, prompt_version: investigator-v1}
      evaluate: {model: default, max_turns: 6, prompt_version: rubric-v1}
      summarize: {model: default, max_turns: 6, prompt_version: summary-v1}
    tools: {enabled: false, allowed_nodes: [investigate], max_results: 20, max_result_rows: 200, timeout_seconds: 30, max_result_bytes: 65536}
    retry: {max_retries: 5, initial_delay_seconds: 1, max_delay_seconds: 16, multiplier: 2, jitter: true}
    sessions: {history_item_limit: 100}
    lease_seconds: 60
    lease_renewal_seconds: 20
```

`ConfigRepository` 使用严格 Pydantic Schema 解析全部 profile。未知字段、越界值、缺失节点或重复 config version 必须在启动、校验或创建 run 前失败。节点的 `model` 优先引用 `models` 中的命名端点；为兼容旧配置，不匹配注册表时按直接模型名解析。

如果注册的只读工具设置 `needs_approval: true`，运行进程必须配置 `BUGLENS_RUN_STATE_KEY`。该密钥需要在可能接手审批恢复的进程间保持一致；它只用于加密 SDK RunState，不会写入 profile、run 快照或 Admin 响应。

多环境工具 profile 另有 `max_result_rows`（默认 200，硬上限 1000）；`max_results` 仍表示一次向 Agent 暴露的工具数。环境目录由 `BUGLENS_ENVIRONMENTS_CONFIG` 指定，参考 `config/environments.example.yaml`。外部工具默认关闭，只有目标确认完成且 profile `tools.enabled=true` 时才向 InvestigateAgent 暴露。

模型端点与节点策略分离：节点只引用命名模型；endpoint、model ID、timeout、streaming 和凭据回退由模型配置解析。API key 等 secret 不属于 profile 的公开视图，也不进入诊断状态、Event 或配置快照。

## 不可变运行快照

Application Service 在创建 run 时解析 profile，生成规范化、无 secret 的 `ResolvedRunConfig`，并在首次事务中保存快照。`DiagnosisState` 只保存 snapshot ID、profile 和 config version。

快照规则：

- snapshot ID 可由规范化配置 hash 生成，相同配置可复用；
- 已被 run 引用的快照不得删除或覆盖；
- 恢复只读取原快照，不重新解析当前 YAML；
- 配置更新只影响之后创建的 run；
- trace/Event 记录 snapshot ID 和版本，不记录完整 prompt 或 secret。
- 目标确认后的 `ResolvedEnvironmentSnapshot` 只保存环境元数据和非敏感 source 配置；凭据不进入 run state、SQLite、Event、Session、Evidence 或工具审计。每次插件调用读取当前凭据，所以显式轮换立即生效。
- state history、node execution、tool execution 和 evidence 记录与状态 revision 关联；工具只在允许的 node/profile/tenant/capability 范围内暴露。
- `retry.max_retries` 表示首次调用后的重试次数，默认 5；底层 OpenAI client 使用 `max_retries=0`，由 Agents SDK runner-managed retry 负责模型传输重试。

## Admin 控制面

Admin 控制面只访问 `ConfigRepository`、CheckpointStore 和安装能力信息，不调用 Graph，不触发诊断节点，也不复放 SDK 消息。

职责分组：

| 分组 | 能力 |
| --- | --- |
| 启动与健康 | 初始化状态、组件健康、版本、能力开关 |
| 配置 | 读取公开配置、校验候选 profile、按 revision 原子应用 |
| 环境与插件 | 列出环境、发现插件、校验目录、原子应用目录、检查实例健康 |
| 运维查询 | 分页查询 run、SDK Session、节点尝试和脱敏工具审计 |

健康检查至少区分 checkpoint store、配置解析和模型凭据状态；只返回状态与说明，不返回凭据。能力开关控制前端是否展示操作；浏览器不能执行 shell、容器更新或其他未实现的运维动作。Admin 列表遇到单条损坏快照时跳过该行，并通过 `degraded_count` 暴露降级数量。

运行列表必须把 lifecycle 与 outcome 分开。Session 查询只返回 session ID、run/node、状态、时间、消息数量和 config snapshot，不返回 `agent_messages` 内容。

## 配置校验与原子应用

校验只解析候选值，不修改 active 配置。应用配置时：

1. 在进程内写锁下比较 `expected_revision`；
2. 校验目标 profile 和全部现有 profile；
3. 在配置文件同目录写临时 YAML；
4. 使用 `os.replace` 原子替换；
5. 更新内存副本并返回新 revision。

revision 冲突返回 `config_revision_conflict`；没有配置文件路径时，内置默认配置只读并返回 `config_not_writable`。任一步失败都不能留下部分写入。

## 安全与部署

环境配置 API 返回 opaque revision；PUT/PATCH 使用 `If-Match` 或 `expected_revision`。配置先写同目录临时文件并原子替换，失败继续使用旧目录。secret 使用 `username`、`password`、`token` 独立字段，响应只返回 `is_set`，更新通过 `secret_updates` 的 `set`/`clear` 明确执行。驱动插件代码仍由运维安装和重启发布；MCP/CLI/SSH 连接器只需更新目录配置。

- 设置 `BUGLENS_ADMIN_TOKEN` 后，所有 Admin 请求必须使用 Bearer token；比较使用常量时间。
- token、API key 和其他 secret 不写入业务状态、Event、日志或前端可读响应。若私有 profile 配置了 `api_key`，配置文件必须按凭据文件保护，Admin 视图只能返回掩码值；空值或掩码值更新会保留原 key。
- 独立域名部署使用精确 `BUGLENS_CORS_ORIGIN`，允许 `content-type`、`accept`、`authorization`；预检返回 204。
- 生产环境使用 TLS；SSE 代理保留 `text/event-stream`、关闭缓冲并设置合理 idle timeout。
- 根目录一键安装器分别调用 `backend/install.*` 和 `frontend/install.*`；`distribution/` 只负责运行管理与静态代理。backend 模式不需要 Node.js，使用预构建制品的 full 模式同样不需要 Node.js。
- 前端不可用时，本地 CLI、远程 CLI、HTTP/SSE API 和 backend-only 部署仍可使用。
- 审批状态只在后端数据库保存加密的 SDK RunState；公开 `pending_approval`、`tool_approval_required` 仅用于展示和 request ID 关联。

## 验收

- 相同 profile 解析确定，非法配置快速失败。
- 修改当前配置不影响已有 run 的恢复。
- 配置冲突和校验失败不会部分落盘。
- Admin 请求不进入 Graph，查询有界且不泄露模型消息或 secret。
- 配置快照、trace 和 Event 可关联到版本，但都不包含凭据。
