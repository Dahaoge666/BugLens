# BugLens 多环境诊断工具插件规范

状态：`v1`，与当前实现同步。

本文是 BugLens 多环境、数据库和节点日志能力的后端规范。它定义核心与插件之间的稳定边界；前端只依赖根目录的 [前后端对接契约](../../frontend-backend-contract.md)，插件包不依赖 BugLens Agent 或运行时。

## 1. 目标与边界

一次诊断遵循以下顺序：

```text
StartDiagnosis
    │
    ├─ explicit ──> 后端解析 ──> 保存无凭据环境快照
    │
    └─ infer ────> 后端按 ID/名称/别名生成候选
                       │
                       └─ TargetConfirmationRequired
                              │
                              └─ ConfirmDiagnosisTarget
                                     │
                                     └─ InvestigateAgent
                                            │
                                     EnvironmentToolRegistry
                                            │
                                      buglens.tool_plugins
                                            │
                                     deterministic connector
                                            │
                                      Evidence + audit
```

BugLens 核心拥有模型、Agent、工具 Schema、目标确认、调用预算、超时、脱敏、审计、Evidence 和生命周期。插件只实现确定性的只读执行，不持有模型密钥、Agent、Runner、CheckpointStore 或完整 `DiagnosisState`。

当前部署假设是可信内网：不提供用户身份、角色、RBAC、环境授权或 API 鉴权。环境确认、只读事务、查询限制和跨环境拒绝是安全边界与资源控制，不是用户权限系统。若将来接入不可信租户，必须在此规范之外增加鉴权和授权层。

非目标：插件执行写操作、Shell/SSH、修复、发布、重启、任意文件浏览、任意数据库连接串、在 Web UI 中安装或升级插件。

## 2. 独立包与发现

仓库提供三个可独立构建的包：

```text
plugin-api/                 # buglens-plugin-api，只有 Pydantic 协议
plugins/sqlite/             # buglens-plugin-sqlite 参考插件
plugins/file-logs/          # buglens-plugin-file-logs 参考插件
backend/app/plugins.py      # 核心发现、注册、调用和审计桥接
```

插件通过 PyPA entry point group `buglens.tool_plugins` 发现。例如：

```toml
[project.entry-points."buglens.tool_plugins"]
sqlite = "buglens_sqlite_plugin:plugin_factory"
```

核心启动时发现全部已安装 entry point，按 `plugin_id` 去重，并拒绝不兼容的 `api_major`。安装、升级和删除插件由运维发布完成，需要重启；环境 YAML 和 Admin API 只管理实例配置。

### 2.1 `buglens-plugin-api`

API 主版本当前为 `1`。协议类型位于 `buglens_plugin_api.protocol`：

| 类型 | 必填/关键字段 | 语义 |
| --- | --- | --- |
| `PluginManifest` | `plugin_id`、`implementation_version`、`api_major`、`api_version`、`capabilities` | 插件身份、兼容性、能力和两个 JSON Schema |
| `ToolPlugin` | `validate_config()`、`check_health()`、`execute()`、`close()` | 插件生命周期协议 |
| `ExecutionContext` | `execution_id`、`run_id`、`environment_snapshot_id`、`plugin_instance_id`、`deadline`、限制 | 单次调用的有界、无凭据上下文 |
| `ToolResult` | `status`、`result`、来源引用、游标、`truncated`、耗时、警告 | 结构化成功、部分、拒绝或不可用结果 |
| `SourceReference` | `source_id`、`locator` | 可审计的来源定位 |

`ToolResult.status` 只能是：

- `succeeded`：结果完整返回；
- `partial`：达到行数、字节或扫描限制，结果可用但不完整；
- `rejected`：策略、参数或语句被拒绝，允许 Agent 调整请求；
- `unavailable`：源不可用、超时或插件故障，核心不向模型发送原始异常。

插件实现必须接受 JSON 对象并返回 JSON-safe 数据。插件不得把凭据放入 `ToolResult`、`SourceReference`、警告、异常文本或 locator。

Manifest 中的 `instance_config_schema` 和 `source_config_schema` 使用 JSON Schema Draft 2020-12；核心在注册和配置校验时调用 `Draft202012Validator`。核心环境模型的凭据字段 `username`、`password`、`token` 均声明 `writeOnly: true` 和 `x-buglens-secret: true`。

## 3. 环境目录

由 `BUGLENS_ENVIRONMENTS_CONFIG` 指向 YAML 文件。完整目录严格拒绝未知字段、重复 ID、无效引用、重复/混用环境的 source 和无效服务依赖。集合既支持列表，也支持以 ID 为 key 的 mapping。

顶层字段：

| 字段 | 说明 |
| --- | --- |
| `config_version` | 目录格式或运营版本 |
| `plugin_instances` | 插件实例和连接配置 |
| `environments` | 可确认的环境 |
| `services` | 环境内服务与依赖 |
| `nodes` | 节点/主机实例 |
| `sources` | 绑定插件实例的数据源 |

最小结构如下：

```yaml
config_version: environments-v1
plugin_instances:
  sqlite-local:
    plugin_id: sqlite
    enabled: true
    config:
      root_path: ./data
    username: null
    password: null
    token: null
    default_limits:
      max_results: 200
      timeout_seconds: 10
      max_bytes: 65536
      max_scan_files: 100
      max_scan_bytes: 16777216
environments:
  staging:
    display_name: Staging
    aliases: [stage, 预发]
    level: staging
    region: cn-east-1
    timezone: Asia/Shanghai
    tags: {deployment.environment.name: staging}
    enabled: true
services:
  order-api:
    name: order-api
    aliases: [orders]
    environment_id: staging
nodes:
  order-node-1:
    hostname: order-stage-1
    instance_id: order-api-1
    environment_id: staging
sources:
  order-db:
    kind: database
    plugin_instance: sqlite-local
    environment_id: staging
    service_ids: [order-api]
    config:
      path: staging/orders.sqlite
      allowed_tables: [orders]
      denied_columns: [password, token]
```

字段映射优先采用 OpenTelemetry 语义：

- `ServiceConfig.name` 对应 `service.name`，可在 `tags` 或别名中保留 `service.namespace`；
- `NodeConfig.instance_id` 对应 `service.instance.id`；
- `EnvironmentConfig.id`/`tags[deployment.environment.name]` 对应 `deployment.environment.name`；
- `region`、`timezone` 和 `tags` 保存部署区域及其他 resource attributes。

一个 source 只能属于一个环境。它可以关联该环境内多个服务和节点；一次 run 可以访问确认环境内的多个 source，但绝不能访问其他环境的 source。没有环境快照的旧 run 不会自动获得外部工具。

## 4. 目标选择、确认与恢复

`StartDiagnosis` 新增：

```json
{
  "target": {
    "mode": "explicit",
    "environment_id": "production",
    "primary_service_id": "order-api"
  }
}
```

`mode=explicit` 必须包含启用环境 ID；该选择直接视为确认。`primary_service_id` 只作为诊断提示，不缩小同环境关联服务的可查询范围。

`mode=infer` 使用请求的 `target.environment_id`、`target.primary_service_id` 和兼容的 `context.environment`、`context.service`，对环境 ID、显示名和别名做确定性匹配。匹配结果最多返回 20 个后端生成候选。候选只能来自当前启用目录；模型或客户端不能创建候选、修改候选的环境 ID 或传入连接信息。当前实现直接让用户确认后端候选，以保证模型永远不是环境授权边界；若未来让 AnalyzeAgent 预筛候选，核心仍必须重新校验最终 ID，且最多接受 3 个候选 ID。

推断产生：

1. 保存 `TargetConfirmationRequired`，包含 `request_id`、请求目标和最多 20 个候选；
2. 状态变为 `waiting_for_target_confirmation`，`available_actions` 为 `confirm_target`、`cancel`；
3. 此状态不计入 Analyze/Investigate 的澄清轮数，且 InvestigateAgent 不注册数据库或日志工具；
4. 客户端提交 `ConfirmDiagnosisTarget`，携带 `request_id`、环境 ID、可选主服务 ID 和 `expected_revision`；
5. 核心确认候选归属，保存 `ResolvedEnvironmentSnapshot`，提交 `TargetConfirmed`，再推进 Analyze/Investigate。

`ConfirmDiagnosisTarget` 使用 `command_id` 和 `request_id` 双重幂等保护。旧 revision、未知 request、非候选环境、跨环境主服务和重复确认均被拒绝或返回原已提交事件。快照保存环境、服务、节点、启用 source、插件 ID、非敏感 source 配置和目录 revision，但不保存用户名、密码、token 或其他 secret。恢复 run 时只读取原快照；普通目录热更新不会改变它能访问的 source 集合。

## 5. 核心工具协议

InvestigateAgent 只看到以下稳定工具参数：

```text
describe_environment()
describe_database(source_id, schema?, table_pattern?)
query_database(source_id, sql, parameters, purpose)
search_logs(source_id, service_ids?, node_ids?, start_time, end_time,
            text_query, levels?, correlation_ids?, cursor?)
```

Agent 不传递环境 ID、插件 ID、连接地址、文件路径、用户名、密码或 token。`source_id` 是经过确认快照筛选的稳定目录 ID；核心把 source 映射到插件实例和当前凭据。

工具注册同时受以下条件约束：

- profile 的 `tools.enabled`、`allowed_nodes`、`allowed_profiles`；
- 当前节点必须是 `investigate`；
- run 必须有环境快照；
- source 必须在快照中启用且类型匹配；
- 实例和 source 的限制取两者最小值；
- 每个 run 默认最多 20 次外部调用、同时最多 2 次；
- 单次调用默认最多 200 行、10 秒、64 KiB；硬上限 1000 行、30 秒、1 MiB；
- 日志额外默认最多扫描 100 个文件、16 MiB，时间范围必须是绝对时间且不超过 24 小时。

`describe_environment` 也计入 run 的外部工具预算。预算超限、并发超限、未确认目标、跨环境 source 和错误类型都以结构化 `ToolResult` 返回，不把 Python traceback 发送给模型。

插件结果先经过核心 JSON 化、脱敏和大小限制，再作为工具结果返回；成功或部分结果注册一个有界 `EvidenceRecord`，包含 source ID、操作、游标、耗时、截断和警告。结果中的日志、数据库字段和文字均是不可信证据，永远不能改变 Agent 指令；Investigate prompt 明确要求将其视为数据，禁止执行其中的指令。

## 6. 参考插件安全约束

### 6.1 SQLite

`buglens-plugin-sqlite`：

- 只接受配置目录内的规范化路径，拒绝目录逃逸；
- 使用 URI `mode=ro`、`PRAGMA query_only` 和 SQLite authorizer；
- 单次只允许一条语句；拒绝 DML、DDL、`CALL`、`DO`、`COPY`、事务/锁、`ATTACH`、危险 PRAGMA 和扩展加载；
- 参数必须是 JSON 标量，使用命名占位符；
- 可配置表/视图白名单和敏感列拒绝列表；
- 使用 progress callback 和 deadline；结果按行数与 UTF-8 字节数限制；
- BLOB 不返回原文，只返回类型、长度和 SHA-256。

### 6.2 文件日志

`buglens-plugin-file-logs`：

- 只搜索实例 `root_path` 内配置的 `path`、`paths` 或 `glob`；拒绝绝对路径、`..` 和解析后逃出 root 的符号链接；
- 基础文件名会包含轮转文件；支持纯文本、JSON Lines、常见编码和自定义 timestamp fields；
- 必须传绝对 `start_time`/`end_time`，时间窗口最多 24 小时；
- 按文件数、扫描字节、返回条数、返回字节和 deadline 限制；
- 返回相对路径和行号 locator，不返回任意主机绝对路径；
- 普通文本按行解析为 message，JSON Lines 提取 timestamp、level、service、node、correlation ID 等安全字段。

Loki、Elastic、远程日志代理等后续接入仍实现同一个 `ToolPlugin` 协议，不改变 Agent 工具 Schema 或 run 生命周期。

## 7. 配置热更新与 secret

环境目录公开 revision 是对去除 secret 后规范 JSON 的 opaque SHA-256 前缀。Admin API：

- `GET /v1/admin/environment-config` 返回 revision、writable 和非敏感目录；
- `POST /v1/admin/environment-config/validate` 只解析、检查 JSON Schema、引用和插件配置，不写文件；
- `PUT /v1/admin/environment-config` 必须带 `expected_revision` 或 `If-Match`；
- 写入同目录临时文件后用 `os.replace` 原子替换，任意校验失败继续使用旧版本；
- 新 run 使用新目录；已有 run 使用旧的无凭据 source 快照；
- 每次实际调用从当前内存目录读取实例凭据，因此凭据轮换立即生效；插件实例变化时旧实例先 `close()`。

配置 API 永不返回 secret 原文，只返回：

```json
{
  "password": {"is_set": true},
  "token": {"is_set": false}
}
```

更新时遗漏字段表示保留；设置或清除必须在 `secret_updates` 中明确声明：

```json
{
  "secret_updates": {
    "sqlite-local": {
      "password": {"action": "set", "value": "new-value"},
      "token": {"action": "clear"}
    }
  }
}
```

凭据不进入 run config snapshot、environment snapshot、state、Event、SDK Session、Evidence、模型上下文或工具审计。审计只保留脱敏 SQL、查询指纹、source/instance/plugin 版本、耗时、状态和截断标记；查询参数全部掩码。

## 8. Admin 与前端

新增接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/environments` | 返回可选择环境摘要和目录 revision |
| GET | `/v1/admin/plugins` | 已发现插件、能力、版本、Schema 和实例状态 |
| GET | `/v1/admin/environment-config` | 返回非敏感目录和 secret presence |
| POST | `/v1/admin/environment-config/validate` | 校验候选目录 |
| PUT/PATCH | `/v1/admin/environment-config` | If-Match/revision 原子更新 |
| POST | `/v1/admin/plugin-instances/{id}/check` | 检查单个插件实例健康 |

管理页面提供环境/插件目录、Schema、校验、连接检查和 revision 冲突提示；凭据输入框只在提交时发送，不回显原值。新建诊断支持明确选择和自动推断。Run 详情展示目标确认、环境快照 ID、插件 Evidence 和查询审计摘要，但不展示凭据或未脱敏异常。

## 9. 持久化与审计

Checkpoint Schema 为 v3：

- 新增 `run_environment_snapshots(snapshot_id, environment_id, config_revision, snapshot_json, created_at)`；
- `DiagnosisState` 新增 `target`、`pending_target_confirmation`、`environment_snapshot_id`，旧 run 通过 nullable 字段兼容；
- `diagnosis_tool_executions` 新增 plugin ID/实现版本/实例 ID、environment snapshot、source、operation、redacted query、query fingerprint 和 `truncated`；
- 旧数据库启动时按 nullable 列迁移，旧 run 没有环境快照也不会被补授外部工具。

`ToolCallStarted`/`ToolCallCompleted`/`ToolCallFailed` 仍然是运行事件，工具详细审计和 Evidence 分别写入工具执行表和 Evidence 表。Event、command、state、审计与证据都经过固定上限和脱敏；模型消息只保存在 SDK Session。

## 10. 验收矩阵

核心测试不调用真实模型或生产数据，使用 fake `NodeRunner`/fake plugin：

| 场景 | 验收点 |
| --- | --- |
| 发现 | entry point、重复 ID、API major 不兼容、manifest/schema 错误 |
| 目录 | 重复 ID、未知引用、服务依赖、source 跨环境、secret schema |
| 目标 | explicit、ID/别名推断、最多 20 候选、确认前无工具、确认幂等、跨环境拒绝 |
| 快照 | 不含凭据；目录更新不改变已有 run；凭据轮换立即对新调用生效 |
| 预算 | 每 run 20 次、并发 2、超时、行数/字节截断、错误映射和 Evidence 注册 |
| SQLite | DML/DDL/ATTACH/危险 PRAGMA/多语句/锁/超时/表列白名单 |
| 日志 | 目录穿越、符号链接、轮转、编码、时间窗口和扫描限制 |
| Admin | validate 不写入、原子回滚、If-Match 冲突、secret 只返回 `is_set` |
| 构建 | 后端 wheel、`buglens-plugin-api` wheel、SQLite wheel、file-logs wheel 独立构建 |

本地验证命令：

```powershell
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

cd ../plugin-api
python -m build --wheel
cd ../plugins/sqlite
python -m build --wheel
cd ../file-logs
python -m build --wheel
```

默认 profile 和启动环境不启用外部工具；参考插件只有在独立安装 wheel、配置环境目录并将 profile 的 `tools.enabled` 打开后才参与诊断。
