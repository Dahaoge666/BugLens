# 两套环境端到端验证计划

## 1. 验证目标

验证 BugLens 能在两个完全隔离的环境中，分别读取该环境自己的日志和数据库，形成可追溯的根因结论，并拒绝跨环境数据访问。

本计划的“定位成功”必须同时满足：

- Run 最终为 `lifecycle_status=completed`、`outcome=confirmed`；
- `Analyze → Investigate → Evaluate → Summarize` 全链路完成，评测通过；
- Investigate 在同一个 Run 内至少成功调用一次 `search_logs` 和一次 `query_database`；
- 报告引用至少一条日志 Evidence 和一条数据库 Evidence；
- 根因与预置故障一致，且没有把另一环境的故障或数据写入报告；
- 工具审计、环境快照、事件和报告均不包含凭据或敏感列。

## 2. 验证拓扑

每个环境使用独立的插件实例、根目录、日志文件和 SQLite 数据库。即使连接器实现相同，也不共享实例配置或数据目录。

| 环境 | 服务 / 节点 | 日志实例与 source | 数据库实例与 source | 预置故障 |
| --- | --- | --- | --- | --- |
| `staging` | `order-api-staging` / `order-staging-1` | `staging-file-logs` / `staging-order-logs` | `staging-sqlite` / `staging-orders-db` | 沙箱支付回调超时，订单长时间停留在 `pending` |
| `production` | `order-api-production` / `order-production-1` | `production-file-logs` / `production-order-logs` | `production-sqlite` / `production-orders-db` | 数据库连接池耗尽，订单请求批量超时 |

建议的临时数据目录如下，所有生成物均不提交 Git：

```text
backend/.e2e/
├── catalog.yaml
├── profile.yaml
├── scenario.json
├── staging/
│   ├── db/orders.sqlite
│   └── logs/order-api.jsonl
├── production/
│   ├── db/orders.sqlite
│   └── logs/order-api.jsonl
└── results/
```

`catalog.yaml` 配置四个插件实例。数据库 source 显式声明 `database.describe.v1`、`database.query.v1`，日志 source 显式声明 `logs.search.v1`。两个环境的 source、service 和 node 不得交叉引用。

## 3. 场景数据

准备脚本在每次执行时生成一个基准时间 `T0 = 当前 UTC 时间 - 15 分钟`，所有日志和数据库记录落在 `[T0, T0 + 10 分钟]` 内，并将实际时间窗写入 `scenario.json`。这样日志查询始终使用绝对时间且不超过 24 小时。

### 3.1 Staging：支付回调超时

问题描述：

> staging 的订单 `ord_stg_1002` 支付后超过 5 分钟仍为 pending，请定位原因。

数据库准备：

- `orders` 中 `ord_stg_1002.status=pending`；
- `order_events` 中依次存在 `checkout_received`、`payment_requested`；
- 最后一条事件为 `payment_callback_timeout`，不存在 `payment_confirmed`；
- `payment_token` 保留为拒绝读取的敏感列，用于验证列级保护。

日志准备：

- 使用 `service_id=order-api-staging`、`node_id=order-staging-1`；
- 使用唯一关联 ID `stg-trace-1002`；
- 关键消息包含 `payment callback deadline exceeded`、`provider=sandbox-pay` 和 `order_id=ord_stg_1002`；
- 写入隔离标记 `STAGING_ONLY_PAYMENT_CALLBACK`。

预期结论：沙箱支付渠道回调超过等待期限，订单状态机未收到确认事件，因此订单停留在 `pending`。结论不得表述为数据库连接池耗尽。

### 3.2 Production：数据库连接池耗尽

问题描述：

> production 的订单接口在给定时间窗内出现批量超时，请定位原因。

数据库准备：

- `orders` 中 `ord_prod_7802`、`ord_prod_7803` 为 `timeout`，同窗口保留至少一条 `paid` 对照记录；
- `order_events` 中两笔异常订单均有 `checkout_timeout`，时间与日志告警一致；
- 数据允许使用时间范围和状态做聚合查询，禁止读取 `payment_token`。

日志准备：

- 使用 `service_id=order-api-production`、`node_id=order-production-1`；
- 使用唯一关联 ID `prod-trace-7802`、`prod-trace-7803`；
- 关键消息包含 `db pool acquire timeout`、`active=20`、`idle=0`、`waiters=37`；
- 写入隔离标记 `PRODUCTION_ONLY_DB_POOL_EXHAUSTED`。

预期结论：数据库连接池被占满且存在等待队列，连接获取超时导致订单请求失败。结论不得表述为支付回调异常。

## 4. 分层验证

### 4.1 第 0 层：启动前检查

1. 在 backend 虚拟环境中安装 `plugin-api`、SQLite 和 file-logs 参考插件；
2. 使用独立的临时 checkpoint/session 数据库，避免历史 Run 污染断言；
3. 校验 profile 已开启工具，且只允许 `investigate` 节点使用；
4. 调用环境目录 validate API，要求无 Schema、引用或 capability 错误；
5. 对四个插件实例逐一执行 health check，结果必须全部健康；
6. `GET /v1/environments` 必须只返回本次启用的 `staging` 和 `production`。

任一前置检查失败时停止验证，不进入诊断，以免把连接问题误判为定位失败。

### 4.2 第 1 层：确定性自动化 E2E

该层进入默认 pytest，可在 CI 中重复执行，但不得调用真实模型。

使用 `ScenarioNodeRunner` 返回严格 Pydantic 输出：

1. Analyze 根据场景输入形成问题分析；
2. Investigate 通过真实 `EnvironmentToolRegistry` 调用真实 file-logs/SQLite 插件；
3. runner 根据真实工具结果构造包含 Evidence ID 的 `InvestigationResult`；
4. Evaluate 返回满足当前 rubric 阈值的 `EvaluationResult`；
5. Summarize 生成最终 `DiagnosisReport`。

这层覆盖 Application Service、Runtime、目标快照、Graph、能力网关、真实连接器、Evidence、审计和最终状态。Agents SDK 的 Session 参数与严格 Schema 继续由现有独立测试验证。

建议新增：

```text
backend/scripts/prepare_two_environment_e2e.py
backend/tests/e2e/test_two_environment_diagnosis.py
```

测试必须使用 pytest 临时目录生成四份数据源，不读取或修改 `backend/data/` 中的演示数据。

### 4.3 第 2 层：真实模型冒烟

真实模型冒烟只通过显式命令手工或受控流水线执行，不进入 pytest，也不作为每次提交的强制 CI 项。它验证模型能根据问题描述主动选择正确日志和数据库工具。

两次运行都使用 explicit target，避免把环境选择交给模型。问题文本和查询时间窗直接复用生成脚本写入的 `scenario.json`，避免模型猜测时间范围：

```powershell
$env:BUGLENS_CONFIG = ".e2e/profile.yaml"
$env:BUGLENS_ENVIRONMENTS_CONFIG = ".e2e/catalog.yaml"
$env:BUGLENS_SESSION_DB = ".e2e/buglens.db"
$scenario = Get-Content .e2e/scenario.json -Raw | ConvertFrom-Json

uv run buglens $scenario.environments.staging.question `
  --environment staging `
  --service order-api-staging `
  --context "window_start=$($scenario.window_start)" `
  --context "window_end=$($scenario.window_end)" `
  --json `
  --output .e2e/results/staging.json

uv run buglens $scenario.environments.production.question `
  --environment production `
  --service order-api-production `
  --context "window_start=$($scenario.window_start)" `
  --context "window_end=$($scenario.window_end)" `
  --json `
  --output .e2e/results/production.json
```

准备脚本应把实际的绝对开始/结束时间写入问题文本或附加 context，避免模型猜测查询窗口。若出现 `input_required`，本场景判失败：已准备的数据应足以完成定位，不依赖人工补充。

## 5. 自动断言

### 5.1 Run 与状态机

每个场景均断言：

- `target.environment_id` 与请求一致；
- `environment_snapshot_id` 非空，快照只包含本环境的两个 source；
- 事件序号连续且 revision 单调递增；
- 四个诊断节点均成功完成；
- 没有 `run_failed`、`input_required` 或未解决的 approval；
- `evaluation.passed=true`、总分不低于 75、证据可追溯和验证可执行两项均不低于 15；
- 最终状态为 `completed/confirmed`。

### 5.2 工具、Evidence 与审计

通过 `GET /v1/admin/runs/{run_id}/tools` 和 Run 快照断言：

- 工具只在 `investigate` 节点调用；
- 至少一条 `search_logs` 和一条 `query_database` 的状态为 `succeeded`；
- staging 的全部审计记录只引用 `staging-*` instance/source；
- production 的全部审计记录只引用 `production-*` instance/source；
- 报告 `evidence_ids` 至少关联一条日志 Evidence 和一条数据库 Evidence；
- Evidence 的 `source_reference` 可定位到日志相对路径/行号或数据库 source；
- SQL 为单条只读 `SELECT`，审计中参数已掩码且存在 query fingerprint；
- `payment_token`、连接路径、凭据和 Python traceback 不出现在状态、事件、审计或报告中。

### 5.3 场景语义

语义判断使用小型允许词组集合，不要求模型逐字匹配：

| 场景 | 必须表达 | 禁止作为主因 |
| --- | --- | --- |
| staging | 支付回调超时 / 未收到支付确认 / 订单状态停留 pending | 数据库连接池耗尽 |
| production | 数据库连接池耗尽 / 无空闲连接 / 获取连接超时 | 支付回调超时 |

报告必须同时引用场景中的订单状态或事件记录，以及对应日志告警；只复述用户问题、只使用一种 source，或将相关性写成无证据事实均判失败。

### 5.4 跨环境隔离

除两个成功场景外，再执行两个网关级负例：

- 持有 staging 快照调用 `production-order-logs` 或 `production-orders-db`；
- 持有 production 快照调用 `staging-order-logs` 或 `staging-orders-db`。

两者都必须返回稳定的 `source_not_allowed`/等价拒绝结果，不产生 Evidence。随后扫描两个成功 Run 的状态、事件、审计和报告：staging 中不得出现 `PRODUCTION_ONLY_DB_POOL_EXHAUSTED`，production 中不得出现 `STAGING_ONLY_PAYMENT_CALLBACK`。

## 6. 执行顺序与通过门槛

```text
生成临时数据
  → 校验目录与四个实例健康
  → 执行 staging 成功场景
  → 执行 production 成功场景
  → 执行双向跨环境拒绝
  → 校验 Evidence / audit / 敏感信息
  → 保存结果摘要
```

最终门槛为四个 Gate 全部通过：

| Gate | 通过条件 |
| --- | --- |
| G0 配置与连接 | 目录校验通过，四个实例健康，source 归属正确 |
| G1 Staging 定位 | 使用自己的日志和数据库，输出支付回调超时的 confirmed 结论 |
| G2 Production 定位 | 使用自己的日志和数据库，输出连接池耗尽的 confirmed 结论 |
| G3 安全与隔离 | 双向跨环境访问被拒绝，无敏感信息或隔离标记泄漏 |

结果摘要至少记录 run ID、环境快照 ID、最终状态、主结论、评测分数、工具调用 source/operation/status、Evidence ID 和失败原因。原始凭据及未脱敏工具参数不得写入结果文件。

## 7. 完成后的标准命令

实现本计划后，默认自动化验证入口应保持为：

```powershell
cd backend
uv run pytest -q tests/e2e/test_two_environment_diagnosis.py
uv run pytest -q
```

真实模型冒烟使用独立脚本或显式 CLI 命令，并要求调用者预先设置 `OPENAI_API_KEY`。它的失败需要保留 Run 和脱敏审计用于分析，但不得自动重试到“碰巧成功”。
