# BugLens

基于 OpenAI Agents SDK 的故障定位工具。四个独立 Agent 负责分析、定位、评测和总结，
由 Python 状态机控制流转。支持结构化澄清、有限重试和 JSON 状态持久化。

## 结构

项目采用平铺模块，按职责划分对象：

| 模块 | 职责 |
| --- | --- |
| `app/api.py` | 应用工厂、HTTP 路由、异常映射和响应格式 |
| `app/service.py` | `DiagnosisService` 协调创建、查询、回答、取消和失败记录 |
| `app/graph.py` | `DiagnosisGraph` 负责节点编排、暂停恢复和重试 |
| `app/models.py` | 类型化数据契约、状态创建、澄清校验和评分规则 |
| `app/agents.py` | `OpenAINodeRunner` 封装独立 SDK 调用和格式重试 |
| `app/prompts.py` | 类别提示词及 `PromptRegistry` 配置读取 |
| `app/security.py` | 共用输入脱敏函数 |
| `app/storage.py` | `JsonStateStore` 保存和加载运行状态 |
| `tests/test_diagnosis.py` | 流程、领域规则、API 和 SDK 适配回归测试 |

依赖方向：HTTP → Service → Graph → SDK / Storage；业务校验归对应模型所有。
`create_app(service=...)` 支持注入独立服务，测试不需要修改模块全局对象。
设计约束见 [AGENTS.md](AGENTS.md)。

## 启动与测试

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:OPENAI_API_KEY = "你的密钥"
.\.venv\Scripts\python.exe -m uvicorn app.api:app --reload
```

打开 `http://127.0.0.1:8000/docs` 交互调用接口。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

测试模拟模型返回值，同时检查实际 SDK 的严格输出 Schema，不需要 API 密钥。

## 接口与行为

- `POST /diagnoses`：提交 `question`、可选 `context` 和 `evidence`。
- `GET /diagnoses/{run_id}`：查询已保存状态。
- `POST /diagnoses/{run_id}/answers`：提交 `request_id` 与 `answers`，每项包含 `question_id`、`answer` 和可选 `attachments`。
- `POST /diagnoses/{run_id}/cancel`：取消等待澄清的任务。

默认最多两轮定位和两轮澄清。澄清恢复使用新的 Agent 运行，继续当前定位轮次。
多选题的 `answer` 为选项 JSON 数组字符串；单选题直接填写选项。
未确认根因时主结论为 null；节点执行失败返回 502 和可查询的 run ID。

状态保存在 `data/runs/`。可选创建 `config/tenant_prompts.yaml`，结构为：

```yaml
version: team-v1
tenants:
  example-team:
    categories:
      performance: 优先关注 PostgreSQL 连接池。
```

当前是本地 Phase 1 原型，使用单进程，同一任务依次提交答案。
生产鉴权、并发控制、完整审计、只读数据源接入和真实故障质量评测尚未实现。
