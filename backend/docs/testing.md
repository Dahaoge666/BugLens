# 测试与端到端验证

本文维护验证步骤。组件职责见[架构](architecture.md)，恢复语义见[Runtime 协议](runtime-protocol.md)，公开字段与端点见[前后端契约](../../frontend-backend-contract.md)。

## 默认验证

从 `backend/` 执行：

```powershell
uv sync --locked --extra dev --extra web
uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
uv run pytest -q
uv build
```

测试不调用真实模型。Graph 和 Adapter 使用 fake runner/service；SDK 的严格 Schema、Session 参数、网关兼容、handoff、独立评测和审批恢复使用 mock 或受控本地连接器验证。默认测试不读取生产日志或现有演示数据库。

前端独立在 `frontend/` 执行 `pnpm install --frozen-lockfile`、`pnpm typecheck` 和 `pnpm build`。类型检查同时拒绝未使用的局部变量和参数。

## 两套环境自动化验证

`tests/e2e/test_two_environment_diagnosis.py` 使用确定性 `ScenarioNodeRunner`，通过真实 SQLite/file-logs 连接器读取 pytest 临时目录中的隔离数据。它覆盖兼容四节点 Graph、Application Service、Runtime、环境快照、工具预算、Evidence、审计和最终报告。

| 环境 | 服务 | 日志 / 数据库 source | 预置故障 |
| --- | --- | --- | --- |
| `staging` | `order-api-staging` | `staging-order-logs` / `staging-orders-db` | 沙箱支付回调超时，订单停留在 `pending` |
| `production` | `order-api-production` | `production-order-logs` / `production-orders-db` | 数据库连接池耗尽，订单请求批量超时 |

```powershell
uv run pytest -q tests/e2e/test_two_environment_diagnosis.py
```

验收重点：两个场景均完成并通过独立评测，报告同时引用日志和数据库 Evidence；全部 source 与环境快照归属一致；双向跨环境查询被拒绝且不产生 Evidence；敏感列、凭据和另一环境的隔离标记不进入公开状态、事件或审计。

需要手工检查数据或运行真实模型时，生成独立 fixture：

```powershell
uv run python scripts/prepare_two_environment_e2e.py --output .e2e
```

脚本生成 `catalog.yaml`、`profile.yaml`、`scenario.json`、两个环境各自的日志/数据库及结果目录。日志时间以生成时的 UTC 时间为基准，绝对窗口写入 `scenario.json`。已有 fixture 默认不覆盖；重复运行使用新目录，或确认覆盖后显式传入 `--force`。生成物已由 `.gitignore` 排除。

## 真实模型冒烟

这部分尚需在自己的测试凭据下显式执行，不进入 pytest 或每次提交的 CI。使用脱敏问题、专用 API Key、独立 SQLite 文件和唯一 run ID。不要自动重试到偶然通过。

在 `backend/` 设置凭据和生成 fixture：

```powershell
$env:OPENAI_API_KEY = "<专用测试 Key>"
$env:BUGLENS_CONFIG = ".e2e/profile.yaml"
$env:BUGLENS_ENVIRONMENTS_CONFIG = ".e2e/catalog.yaml"
$env:BUGLENS_SESSION_DB = ".e2e/buglens.db"
$scenario = Get-Content .e2e/scenario.json -Raw | ConvertFrom-Json
```

安装参考插件，让真实进程能发现两个独立插件包：

```powershell
uv pip install --python .venv/Scripts/python.exe --editable ../plugins/sqlite --editable ../plugins/file-logs
```

macOS/Linux 将解释器路径换为 `.venv/bin/python`。后续 `uv run` 添加 `--no-sync`，保留本次临时安装的参考插件。

两次运行显式指定目标，复用生成的时间窗口：

```powershell
uv run --no-sync buglens $scenario.environments.staging.question `
  --environment staging --service order-api-staging `
  --context "window_start=$($scenario.window_start)" `
  --context "window_end=$($scenario.window_end)" `
  --json --output .e2e/results/staging.json

uv run --no-sync buglens $scenario.environments.production.question `
  --environment production --service order-api-production `
  --context "window_start=$($scenario.window_start)" `
  --context "window_end=$($scenario.window_end)" `
  --json --output .e2e/results/production.json
```

先校验目录和四个插件实例健康，再运行诊断。两套环境的预置数据足以定位；若请求额外输入或只引用一种 source，应记录为未通过。真实生产编排使用原生 loop，不能把四个独立 SDK 节点或 Session 当作验收条件。

除两个定位场景外，还需验证：

| 场景 | 验收重点 |
| --- | --- |
| 基本 CLI 诊断 | 最终报告区分已支持结论、假设和信息不足；`confirmed` 有独立评测和可追溯 Evidence |
| HTTP/SSE 断线追赶 | 断线后回查快照，按 sequence 追赶；同一个 command ID 重放不重复推进 |
| 澄清与 Skip | 使用快照中的 request/question ID 和 revision；Evaluate 来源回答回到 Investigator；Skip 不伪造答案 |
| 失败 Resume | 受控执行故障提供恢复操作；恢复沿用 run、配置快照和 Session，不重做已提交步骤 |
| 工具审批 | 仅对显式启用审批的只读工具测试；设置一致的 `BUGLENS_RUN_STATE_KEY`，跨进程批准/拒绝不重复调用 |
| 审计和 Session | 原生 Session 为 `{run_id}:diagnosis`，兼容 Graph 为 `{run_id}:{node}`；Admin 只返回脱敏元数据 |

业务证据不足的 `completed/inconclusive` 与基础设施 `failed` 分开记录。未触发澄清或审批分支时写明“未触发”，不能伪造等待状态。待执行结果统一记录在根目录 [TODO](../../TODO.md)。
