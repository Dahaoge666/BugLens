# BugLens 实现约束

## 产品形态

BugLens 是交互式 CLI，不提供 Web API。一次命令完成问题输入、必要澄清、定位、评测和报告输出。

## Agent Graph

流程固定为：

```text
AnalyzeAgent → InvestigateAgent → EvaluateAgent
                      ↑                │
                      └── 评测失败 ────┘
                              │
                         SummarizeAgent
```

四个节点都是独立的 OpenAI Agents SDK `Agent`，Graph 是确定性的 Python 编排器。Graph 控制分类路由、最多两次定位、最多两轮澄清和最终状态；模型不得自由改变流程。

## SDK 优先原则

- 使用 `Agent` 定义节点，使用 Pydantic `output_type` 约束结果。
- 使用 `Runner.run()` 执行节点并设置 `max_turns`。
- 使用 SDK `SQLiteSession` 管理模型会话，不自行保存或回放消息。
- 每个 `run_id + node_name` 使用独立 Session。同一节点的澄清和重试复用 Session，不同节点不共享完整会话。
- 使用 SDK tracing 包裹完整诊断，以 `run_id` 作为 `group_id`。
- 只有确定性业务状态保存在 `DiagnosisState`；当前 CLI 生命周期内使用，不另建会话存储层。
- SDK 已有能力可以满足需求时，不重复实现 Session、消息历史、response chaining、工具循环或结构化输出解析。

## 节点职责

- `analyze`：提取症状、影响、时间和环境并分类，不输出根因。
- `investigate`：按类别生成 1–3 条有证据支持且可验证的原因假设。
- `evaluate`：独立检查覆盖度、证据、推理、验证步骤和不确定性。
- `summarize`：只转写结构化结果，不重新推理或改变评测结论。

澄清由 Analyze 或 Investigate 输出 `UserInteractionRequest`，CLI 收集答案后，将 `ClarificationInput` 发送到同一节点 Session。评测失败时，将 `RetryInput` 发送到同一 Investigate Session。

## 安全与边界

- 只依据用户提供或只读工具返回的证据，不虚构事实。
- 当前 Agent 不注册工具，不执行修复、发布、重启或其他副作用操作。
- 输入和澄清答案进入模型前脱敏。
- 未通过评测时状态必须为 `inconclusive`，不得输出已确认主结论。
- 后续工具必须只读、最小权限、限制结果数量并返回可引用标识。

## 工程要求

- Python 3.11+，依赖和构建配置集中在 `pyproject.toml`。
- CLI 入口为 `buglens = app.cli:main`。
- 测试不得调用真实模型；使用假的 `NodeRunner` 验证路由，并单独验证 SDK Session 参数和严格输出 Schema。
- 提交前运行 Ruff、pytest 和 `python -m build`。
