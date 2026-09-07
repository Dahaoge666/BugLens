# BugLens 测试报告（真实模型端到端）

测试日期：2026-09-07
测试环境：Python 3.14.4 + openai-agents 0.22.0 + openai 3.8.0
模型后端：OpenAI 兼容 MaaS 网关（litellm，GLM 5.2 reasoning 模型，仅支持流式）

## 1. 测试方法

1. 克隆仓库并安装依赖（`pip install -e ".[dev,web]"`）。
2. 先验证项目自身：`ruff check` / `ruff format --check` / `pytest -q`。
3. 用 pi 已配置的 OpenAI 兼容端点（`OPENAI_BASE_URL` + `OPENAI_API_KEY`）作为 BugLens 的模型后端。
4. 逐层探测 SDK 与网关的兼容性，定位阻塞点并做最小适配补丁。
5. 运行真实诊断，校验结构化输出质量、Session/Event 持久化与 Admin 控制面。

## 2. 自身健康度（无需真实模型）

| 检查项 | 结果 |
| --- | --- |
| `ruff check app tests` | All checks passed |
| `ruff format --check` | 21 files already formatted |
| `pytest -q` | 20 passed |
| `buglens --help` | 正常 |
| 包构建（editable） | 成功 |

确定性 Graph 路由、Session 隔离、评测重算、配置快照等单测全部通过。

## 3. 真实模型集成：发现的阻塞点与适配

MaaS 网关行为探测结果：

| 能力 | 结论 |
| --- | --- |
| `GET /v1/models` | ✅ 可用 |
| `POST /v1/chat/completions` `stream:true` | ✅ 可用，但每个模型都带 `reasoning_content` |
| `stream:false`（非流式） | ❌ litellm `InternalServerError`，非流式聚合坏，请求挂起 |
| `POST /v1/responses`（Responses API） | ❌ 400，网关不支持 |
| `response_format: json_schema` 强制 | ❌ 流式下**不强制**，模型仍输出纯文本 |

而 openai-agents SDK 默认：使用 **Responses API**、**非流式** (`stream=False`)、结构化输出**依赖 `response_format`**。三者在本网关上全部失败。

为此做了最小适配补丁（`app/agents.py` + `app/bootstrap.py`，+119/-10，20 单测仍绿）：

- `bootstrap.configure_openai_provider()`：检测到 `OPENAI_BASE_URL`/`OPENAI_API_KEY` 时，`set_default_openai_client` + `set_default_openai_api("chat_completions")`，并返回"自定义端点"标志。
- `OpenAINodeRunner(streaming=...)`：自定义端点时改用 `Runner.run_streamed`（流式），默认仍 `Runner.run`（非流式，OpenAI 官方端点不变）。
- `_schema_hint()`：把 `output_type.model_json_schema()` 显式注入 instructions，不依赖 `response_format` 强制。
- `_install_json_coercion()`：解析前剥离 ` ```json ` 包裹与多余文本，容错模型输出。

## 4. 端到端诊断结果

场景：测试环境 `checkout-api` POST /checkout 返回 500，日志 `KeyError: 'currency'`，1.8.0 新增多币种分支，部分历史订单缺 currency 字段。

```
运行 ID：1b674191b970405f93b6fdf3c2f6566e
生命周期：completed   结果：confirmed   attempt: 1   clarification_round: 0
退出码：0
```

- **analyze**：category=`application_error`，confidence=0.92，准确提取症状/影响/环境/证据（2 条）。
- **investigate**：3 个按 rank 排序假设（置信度 0.9/0.7/0.4），均带 `supporting_evidence`、可执行 `verification_steps`、低风险 `remediation_direction`；`primary_conclusion` 等于假设 1 的 cause。
- **evaluate**：`passed=true, score=88`（coverage 18 / traceability 20 / consistency 18 / executability 18 / uncertainty 14），客观指出 `reference` 字段为 null 的追溯缺陷。
- **summarize**：报告完整，明确"需获取实际代码片段与订单数据以完全确认"，不虚构结论。

诊断质量高，体现了证据约束、不虚构、验证可执行的安全原则。

## 5. 持久化与控制面验证

- **SDK Session**：`agent_sessions` 表正确生成 4 条独立会话（`run_id:analyze/investigate/evaluate/summarize`），符合"每节点独立 Session"设计。
- **Checkpoint/Event**：`diagnosis_runs`（rev=4）+ 10 条有序事件（`run_started`→4×`node_started/completed`→`run_completed`）。
- **Admin 控制面**：`health=ok`（checkpoint/configuration/model_credentials 三组件 ok）、version/capabilities/config、runs 列表（1 条 confirmed）、sessions 列表（4 条 completed，每条 3 消息）均正确。

## 6. 需要改进的点

已识别的问题与本次适配已从本报告迁出，作为单一事实来源登记在仓库文档中，避免重复维护：

- **阻塞兼容问题 + 代码/设计问题（合并）**：见 [`changelog/0001-provider-compat-and-structured-output-hardening.md`](changelog/0001-provider-compat-and-structured-output-hardening.md)。其中 1–3 为已落地适配（待正式化与补测试），4–8 为待修复问题，含原因、影响与建议修复方向。
- **测试与可观测性补遗**：见 [`TODO.md`](TODO.md)，包含兼容网关契约测试、`_schema_hint`/`_install_json_coercion` 单测、`streaming` 开关单测与 `reasoning_content` 可追溯性。

## 7. 复现命令

```bash
cd BugLens
python -m venv .venv
# 引导 pip（若 ensurepip 缺失）
.venv/bin/python /tmp/get-pip.py -i http://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
.venv/bin/python -m pip install -e ".[dev,web]"

export OPENAI_BASE_URL="http://100.85.253.225:50040/v1"
export OPENAI_API_KEY="sk-1234"
export OPENAI_DEFAULT_MODEL="maas-glm-5.2-volcengine-codeagent"
export BUGLENS_TRACING=false
export BUGLENS_SESSION_DB="data/test-maas.db"

.venv/bin/buglens "测试环境 checkout-api POST /checkout 返回 500，KeyError: currency" \
  --context environment=test --context service=checkout-api \
  --evidence log="KeyError: 'currency' at checkout.py:42; trace_id=tr_2001" \
  --evidence change="1.8.0 新增多币种分支，部分历史订单缺 currency 字段" \
  --output data/manual-result.json --json
```
