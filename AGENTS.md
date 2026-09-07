# BugLens 实现约束

## 产品形态

BugLens 提供交互式 CLI 和 Web/API 两种薄 Adapter，两者必须通过同一个 Application Service 和 Agent Runtime 执行诊断，不得直接调用或复制 Graph 逻辑。普通 Web 通信使用 HTTP Command + SSE Event；CLI 可使用本地或远程 AgentClient。

一次短执行持续到完成或必须等待外部输入为止。等待前持久化检查点并结束执行，之后由新的 Command 恢复，不维持长期挂起的 CLI 进程或 HTTP 请求。

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

Graph 只接收业务状态并产生确定性转换，不读取 stdin、HTTP，不直接管理数据库。Agent Runtime 负责加载检查点、应用 Command、推进 Graph、持久化状态和发布 Event。

## SDK 优先原则

- 使用 `Agent` 定义节点，使用 Pydantic `output_type` 约束结果。
- 使用 `Runner.run()` 执行节点并设置 `max_turns`。
- 使用 SDK `SQLiteSession` 管理模型会话，不自行保存或回放消息。
- 每个 `run_id + node_name` 使用独立 Session。同一节点的澄清和重试复用 Session，不同节点不共享完整会话。
- 使用 SDK tracing 包裹完整诊断，以 `run_id` 作为 `group_id`。
- 只有确定性业务状态保存在 `DiagnosisState`，并由独立 CheckpointStore 持久化；不得在业务状态中保存或回放模型消息。
- 每次运行保存不可变配置快照，恢复时继续使用原快照，不重新读取当前配置文件。
- SDK 已有能力可以满足需求时，不重复实现 Session、消息历史、response chaining、工具循环或结构化输出解析。

## 节点职责

- `analyze`：提取症状、影响、时间和环境并分类，不输出根因。
- `investigate`：按类别生成 1–3 条有证据支持且可验证的原因假设。
- `evaluate`：独立检查覆盖度、证据、推理、验证步骤和不确定性。
- `summarize`：只转写结构化结果，不重新推理或改变评测结论。

澄清由 Analyze 或 Investigate 输出 `UserInteractionRequest`。Runtime 保存 pending request、进入 `waiting_user` 并发布 `InputRequired`；CLI 或 Web 提交回答 Command 后，Runtime 将 `ClarificationInput` 发送到同一节点 Session。评测失败时，将 `RetryInput` 发送到同一 Investigate Session。

## 安全与边界

- 只依据用户提供或只读工具返回的证据，不虚构事实。
- 当前 Agent 不注册工具，不执行修复、发布、重启或其他副作用操作。
- 输入和澄清答案进入模型前脱敏。
- 生命周期与诊断结果分离。未通过评测时以 `completed/inconclusive` 结束，不得输出已确认主结论；基础设施失败使用 `failed`。
- 后续工具必须只读、最小权限、限制结果数量并返回可引用标识。

## 工程要求

- Python 3.11+，依赖和构建配置集中在 `pyproject.toml`。
- CLI 入口为 `buglens = app.cli:main`；Web 入口不得包含 Agent 核心逻辑。
- CLI 和 Web 使用严格的统一 `AgentCommand` / `AgentEvent` 协议。
- 固定工作流参数来自版本化配置 profile 并持久化快照，不作为普通 CLI/Web 请求参数。
- 测试不得调用真实模型；使用假的 `NodeRunner` 验证路由，并单独验证 SDK Session 参数和严格输出 Schema。
- 提交前运行 Ruff、pytest 和 `python -m build`。
