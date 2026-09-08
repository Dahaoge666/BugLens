# Graph 生命周期、上下文与审计实现计划

状态：核心生命周期与只读工具审批恢复已实现，发布演练完成
适用范围：后端 Graph、DiagnosisRuntime、Agents SDK 集成、SQLite 持久化、Command/Event 协议，以及对应的 CLI/Web 展示
设计基线：OpenAI Agents SDK 0.22.x、Python 3.11+、Pydantic、SQLite；安装器让 uv 选择可用的兼容解释器。

## 1. 目的

本计划用于把 BugLens 的诊断流程改造成可恢复、可审计的短执行工作流，并为问题分析阶段接入多个只读工具预留稳定扩展点。

实现完成后，一条诊断 Command 应当持续执行到以下任一边界：

- 需要用户补充信息；
- 可恢复故障耗尽自动重试，需要人工 Resume；
- 诊断完成；
- 收到取消请求，并在当前模型或工具调用完成后的安全边界终止。

系统必须能够回答：某个节点为什么执行、实际看到了什么上下文、调用了哪些工具、取得了哪些证据、为何重试或等待，以及如何从检查点恢复。

本计划不改变 BugLens 的业务目标：系统只生成证据约束的诊断报告，不执行修复、部署、重启或其他有副作用的操作。

## 2. 已确认的设计决策

1. 主流程保持确定性：`Analyze → Investigate → Evaluate → Summarize`。Evaluate 未通过时回到 Investigate，最多执行配置允许的定位轮数。
2. 一次请求持续执行到等待态或终止边界，不在每个节点后要求客户端重新发 Command。
3. 暂不新增生命周期枚举；`waiting_approval` 已接入 SDK interruption，`waiting_tool` 继续作为未来异步工具作业的保留状态。
4. Analyze、Investigate、Evaluate 均可发起澄清，但职责不同，澄清次数按节点分别计算，默认每节点最多两轮。
5. Evaluate 发起的用户澄清恢复到 Investigate，由 Investigate 消化新证据后重新评测。
6. 用户可以显式 Skip 当前澄清；Skip 表示在信息不足的前提下继续，不伪造答案。
7. 模型调用、工具调用或网络调用超时属于可恢复执行故障；`waiting_user` 本身不设置 TTL。
8. 自动重试定义为“首次调用失败后最多重试五次”，因此单个重试周期最多调用六次。
9. 只自动重试可恢复故障。配置、契约、鉴权、数据库一致性等确定性故障不盲目重试。
10. 自动重试耗尽后显示 Resume。Resume 沿用原 `run_id`、原节点 SDK Session 和不可变配置快照，并开启一个新的五次重试周期。
11. Cancel 不强制中断正在进行的模型或工具调用；调用结束后，在下一安全边界转为 `canceled`。
12. Graph 的生命周期和路由由 BugLens 控制；单个 Agent 的模型循环、工具执行、模型重试、Session、审批断点、hooks 和 tracing 尽量复用 Agents SDK。

## 3. 当前实现基线与缺口

### 3.1 当前已有能力

- `DiagnosisRuntime` 接收统一 Command，并驱动 `DiagnosisGraph.step()`。
- `diagnosis_runs` 保存最新 `DiagnosisState`。
- `diagnosis_commands` 保存 Command 幂等结果。
- `diagnosis_events` 保存带 `sequence` 和 `revision` 的领域事件。
- `run_config_snapshots` 保存每个 run 的配置快照。
- `run_leases` 用于避免多个执行者同时推进同一个 run。
- 每个 `{run_id}:{node_name}` 使用独立的 SDK `SQLiteSession`。
- 四个节点使用 SDK `Agent`、Pydantic `output_type` 和 `Runner.run()`/`run_streamed()`。
- 输入进入 Graph 前执行脱敏。

### 3.2 发布前检查结果

核心实现、审批恢复、兼容性回归、Admin 降级列表和前端审批入口已完成。第 19 节的锁文件检查、后端/前端构建、SQLite 迁移/备份恢复和隔离 backend-only health 检查均已通过；真实模型与外部工具仍由部署方在自己的凭据和只读连接器下验证。

## 4. 目标职责边界

### 4.1 BugLens 负责

- Graph 节点顺序、路由和评测门禁；
- 生命周期、等待、Resume、Skip、协作式 Cancel；
- 每节点澄清预算和定位轮数上限；
- Command 幂等、revision、lease、checkpoint 和 Event；
- 业务上下文组装、证据注册、证据引用和审计账本；
- 可恢复错误分类，以及 SDK 未覆盖的节点/工具重试；
- 对 CLI/Web 暴露稳定协议，不暴露 SDK 内部对象。

### 4.2 Agents SDK 负责

- `Agent` 和 Pydantic `output_type` 的结构化输出；
- `Runner.run()`/`run_streamed()` 内的模型—工具循环和 `max_turns`；
- `ModelSettings.timeout` 与 `ModelRetrySettings` 的模型调用重试；
- `SQLiteSession` 的模型消息历史；
- `@function_tool` 的参数 Schema、校验、上下文注入和工具超时；
- tool input/output guardrails；
- `RunHooks`、`ToolContext.tool_call_id`、token usage 和 tracing；
- 工具审批产生的 `interruptions`，以及 `RunState` 序列化、批准/拒绝和恢复。

### 4.3 明确不采用的 SDK 编排能力

主流程不使用 handoff 或 `Agent.as_tool()` 编排四个节点。它们会把关键路由交给模型，无法保证评测门禁、定位次数、澄清次数和恢复位置完全确定。

普通用户澄清也不使用 SDK `RunState`。普通澄清发生在节点已经产生合法业务输出之后，应通过 BugLens Command 和 Checkpoint 恢复。`RunState` 只用于真正的工具审批中断。

## 5. 目标生命周期

不新增状态枚举，状态语义如下：

- `created`：可选的极短初始化状态；如果 Start 在一个事务内直接建立 running 快照，也可以不对外观察到。
- `running`：Runtime 正在或可以继续推进 Graph。
- `waiting_user`：已持久化一个 `pending_interaction`，等待 Submit 或 Skip。
- `waiting_tool`：预留给未来无法在一次 Runner 调用内完成的异步工具任务。
- `waiting_approval`：工具触发 SDK interruption，等待批准或拒绝。
- `completed`：业务完成，`outcome` 为 `confirmed` 或 `inconclusive`。
- `failed`：执行停止。若 `last_error.retryable=true` 且重试已耗尽，则允许 Resume；否则不可恢复。
- `canceled`：取消已在安全边界生效。

`completed` 和 `canceled` 不可恢复。`failed` 是否可恢复由 `last_error.retryable`、`resume_available` 和兼容性检查共同决定，不能只看状态字符串。

客户端不得自行推断操作，Run 快照必须返回 `available_actions`：

- `waiting_user`：`submit_answers`、`skip_input`、`cancel`；
- 可恢复 `failed`：`resume`；
- `running`：`cancel`；
- `waiting_approval`：`approve`、`reject`、`cancel`；
- 其余状态：空列表。

## 6. 单条 Command 的执行语义

### 6.1 Start、Submit、Skip 和 Resume

Runtime 在校验并接受 Command 后执行以下循环：

1. 重新读取已提交状态和不可变配置快照。
2. 检查取消控制标记和 lease fencing token。
3. 由纯 Graph 计算下一步的节点调用计划或立即状态转换。
4. 先提交节点执行开始记录和开始事件。
5. 在事务外执行 `Runner.run()`，其中可以完成多轮模型和同步工具调用。
6. 校验节点输出，提交执行结果、状态新 revision、状态历史和领域事件。
7. 若状态仍为 `running`，继续下一轮；否则结束本次 Command。

网络连接断开不代表执行失败。服务端继续执行已接受的 Command；客户端重连后先读快照，再从最后确认的 `sequence` 追赶事件。

### 6.2 安全提交边界

外部模型/工具调用不得放在 SQLite 长事务中。建议使用两个短事务：

- 开始事务：新增 `node_execution(status=running)` 和 `NodeAttemptStarted`；
- 完成事务：更新 node execution，提交新的 run 快照、state history、Command 幂等结果和完成/等待/失败事件。

进程崩溃可能留下 `node_execution.status=running`。恢复时将其标记为 `interrupted`，根据错误分类进入自动重试或可恢复 failed，不能假装该调用从未发生。

## 7. Graph 与节点执行接口

将 Graph 拆为两个纯操作，避免 Graph 在模型调用后才补发开始事件：

```python
plan = graph.prepare_step(state, config)
new_state, transition = graph.apply_result(state, plan, node_result)
```

`prepare_step()` 返回序列化安全的 `NodeExecutionPlan`：

- `execution_id`
- `node`
- `input_model`
- `session_id`
- `investigation_attempt`
- `clarification_round`
- `retry_cycle`
- `config_snapshot_id`

`apply_result()` 只接受已验证的节点输出或明确的执行错误，不读数据库、不调用工具、不发送 Event。

删除或废弃重复的 `DiagnosisGraph.run()`；所有路由测试通过与生产一致的 `prepare_step/apply_result` 接口验证。

每次状态修改后使用不可变 `model_copy(update=...)`，或在 Store commit 前强制执行：

```python
DiagnosisState.model_validate(state.model_dump(mode="python"))
```

推荐同时给 `StrictModel` 开启 `validate_assignment=True`，但提交前完整校验仍需保留。

## 8. 节点上下文流转

### 8.1 通用上下文模型

将自由字典逐步收敛为可扩展的 `DiagnosisContext`：

- `environment`：prod、staging、dev 等；
- `system`、`service`、`component`；
- `version`、`deployment_id`；
- `region`、`host`、`runtime`；
- `incident_started_at`、`timezone`；
- `expected_behavior`、`actual_behavior`；
- `impact_scope`；
- `constraints`；
- `attributes`：保留受限的扩展键值。

所有字段必须有长度、数量和允许类型上限。未知上下文先进入 `attributes`，不能静默丢弃。

### 8.2 证据注册

所有用户附件、日志片段和工具结果先规范化为 `EvidenceRecord`：

- 稳定 `evidence_id`；
- 来源类型和来源引用；
- 内容 hash；
- 脱敏后的有界内容或外部存储引用；
- `observed_at` 和 `collected_at`；
- 产生该证据的 `tool_execution_id`，如果存在；
- 数据分类和保留策略。

模型输出中的 supporting/contradicting evidence 必须引用 `evidence_id`，不能只保存自由文本描述。

### 8.3 各节点实际输入

Analyze：

- 原始问题；
- 完整的规范化 DiagnosisContext；
- 当前证据索引及预算内内容；
- Analyze 本节点历史澄清答案；
- 本节点允许使用的只读工具。

Investigate：

- 最新 ProblemAnalysis；
- 所有相关用户证据和工具证据；
- 用户回答历史；
- 上一轮 Investigation 和 Evaluation；
- Evaluate 的 retry guidance；
- 当前定位轮次和 prompt config version。

Evaluate：

- 最新 Analysis 和 Investigation；
- 被引用证据的索引、hash 和来源；
- 上一轮 Evaluation；
- 结构化进展记录；
- rubric version 和阈值。

Summarize：

- 最终 Analysis、Investigation、Evaluation；
- outcome 和定位轮数；
- 证据索引；
- 被跳过或无法补足的信息；
- 限制条件和失败过的工具摘要。

建立唯一的 `ContextAssembler` 负责这些显式业务输入。SDK `SQLiteSession` 只补充同节点模型历史；使用 `SessionSettings(limit=...)` 和 `session_input_callback` 控制历史合并，不另写模型消息重放系统。

## 9. 澄清、Skip 与无进展规则

### 9.1 数据模型

把全局 `clarification_round` 改为按节点计数，例如：

```python
clarification_rounds = {
    "analyze": 0,
    "investigate": 0,
    "evaluate": 0,
}
```

`UserInteractionRequest.source_node` 扩展为 Analyze、Investigate、Evaluate；`resume_node` 独立校验，允许 Evaluate 指向 Investigate。

Evaluate 增加可选 `interaction_request`，并扩展 reason：

- `tool_unavailable`
- `tool_failed`
- `insufficient_evidence`
- `user_explanation_required`
- `no_progress`

### 9.2 节点职责

- Analyze 只询问问题范围、环境、影响、时间窗口等问题定义信息。
- Investigate 只询问验证假设所需的日志、指标、配置和复现信息。
- Evaluate 仅在工具不足、需要用户解释或连续两轮无进展时提问；回答交回 Investigate。

### 9.3 Skip

新增 `SkipUserInteraction` Command，必须匹配 `run_id`、`expected_revision` 和 `request_id`。执行后：

- 记录哪些必填问题被跳过及原因；
- 增加来源节点澄清轮数；
- 清除 pending；
- 以 `information_unavailable=true` 的结构化输入继续；
- 最终报告必须把相关结论标为假设或信息不足。

### 9.4 连续两轮无进展

不让模型自行决定“是否有进展”。Investigation 输出增加结构化 delta：

- `new_evidence_ids`
- `resolved_gap_ids`
- `changed_hypothesis_ids`
- `discarded_hypothesis_ids`

只有至少一个 ID 通过代码校验且确实相对上一轮新增或改变，才算有进展。连续两个 Evaluate 周期没有有效 delta 时，Evaluate 可以发起一次 `no_progress` 澄清；澄清预算耗尽或用户 Skip 后，按现有最大定位次数规则进入 `inconclusive`。

## 10. 重试与 Resume

### 10.1 SDK 模型重试

节点 Agent 使用：

```python
ModelSettings(
    timeout=node_timeout,
    retry=ModelRetrySettings(
        max_retries=5,
        backoff={
            "initial_delay": 1.0,
            "max_delay": 16.0,
            "multiplier": 2.0,
            "jitter": True,
        },
        policy=retry_policies.any(
            retry_policies.provider_suggested(),
            retry_policies.retry_after(),
            retry_policies.network_error(),
            retry_policies.http_status([408, 409, 429, 500, 502, 503, 504]),
        ),
    ),
)
```

SDK 重试只负责单次模型请求，并利用 replay-safety 判断是否可安全重放。不要再用 `AsyncOpenAI.max_retries` 叠加另一层相同网络重试，否则最坏请求数会相乘。底层 OpenAI client 应显式设为 `max_retries=0`，由 Agents SDK 的 runner-managed retry 作为唯一模型传输重试来源。

### 10.2 BugLens 补充重试

统一 `ExecutionRetryPolicy` 只处理 SDK 未覆盖的边界：

- `ModelBehaviorError`，例如无效结构化输出；
- 可恢复的只读工具连接器故障；
- 进程级临时资源错误。

每次尝试都写入 `node_executions` 或 `tool_executions`，不能用一个字段覆盖整个周期。

不可自动重试：

- 配置或 prompt version 缺失；
- 鉴权/权限拒绝；
- GraphContractError；
- Pydantic 业务契约校验错误；
- revision、lease 或数据库一致性错误；
- 已产生不可安全重放副作用的调用。当前工具只读，因此不应出现最后一种情况。

### 10.3 Resume

新增 `ResumeDiagnosis` Command：

- 仅允许 `failed` 且 `resume_available=true`；
- 必须匹配最新 revision；
- 复用原 run、当前 node、SDK Session 和 config snapshot；
- 递增 `retry_cycle`，本周期 retry index 归零；
- 追加 `RunResumed` 事件；
- 从未成功提交的节点重新执行，不重做已完成节点。

Resume 前检查 agent definition version、SDK major/minor compatibility和 Session 是否存在。不兼容时返回稳定错误，不能用当前配置静默重建旧执行。

## 11. 只读工具扩展设计

### 11.1 注册方式

第一阶段仅给 Analyze 开放工具，但 ToolRegistry 必须支持按节点、profile、tenant 和能力动态过滤。工具使用 SDK `@function_tool`，不要手写 FunctionTool JSON Schema。

通过 `RunContextWrapper[NodeRuntimeContext]` 注入：

- tenant 和授权后的能力集合；
- evidence writer；
- execution/event observer；
- 只读连接器；
- 配置快照和大小限制。

使用 `is_enabled` 隐藏当前 run 无权使用的工具；这只控制暴露范围，工具实现和 input guardrail 仍必须再次校验资源范围。

### 11.2 工具契约

每个工具必须：

- 只读、最小权限；
- 参数使用严格 Pydantic 类型；
- 配置 SDK `timeout`；
- 输出数量、单项长度和总字节数有上限；
- 在入库和返回模型前脱敏；
- 返回稳定 source ID，并将有效结果注册为 EvidenceRecord；
- 不把 secret、内部异常堆栈或无限日志返回模型；
- 支持 fake connector 单元测试，不访问真实外部系统。

SDK tool input/output guardrails 用于资源范围、参数和输出边界。工具超时默认作为可恢复错误返回模型；需要终止节点时使用 `timeout_behavior="raise_exception"` 并交给统一错误分类。

### 11.3 工具审批与异步工具

当前只读工具默认无需审批。显式注册 `needs_approval=True` 的只读工具使用 SDK interruption：

1. Runner 返回 `interruptions`；
2. 将 `result.to_state().to_string()` 作为不透明 SDK 状态，用 `BUGLENS_RUN_STATE_KEY` 加密持久化；
3. Graph 进入 `waiting_approval`；
4. Approve/Reject Command 修改 RunState；
5. 使用原 Agent、原 Session 和原 RunState 恢复；决定、resolved 时间和节点审计状态原子记录。

不要自行设计 SDK 工具调用恢复格式。

普通同步工具在同一次 Runner 调用内完成，不进入 `waiting_tool`。只有未来真正的外部异步作业才使用 `waiting_tool`，届时需另行补充 job ID、回调幂等和超时协议。

## 12. 数据库审计模型

现有 `diagnosis_runs`、`diagnosis_commands`、`diagnosis_events`、`run_config_snapshots`、`run_leases` 和 SDK Session 表继续保留。新增以下表。

### 12.1 `diagnosis_state_history`

每次成功提交 revision 时追加，不覆盖：

- `run_id`
- `revision`
- `state_json`
- `cause_event_id`
- `created_at`
- 主键 `(run_id, revision)`

用途是还原 Graph 状态变化。`diagnosis_runs.state_json` 仍是读取当前状态的快速快照。

### 12.2 `diagnosis_node_executions`

每次节点尝试一条记录：

- `execution_id` 主键；
- `run_id`、`node`、`session_id`；
- `investigation_attempt`、`clarification_round`；
- `retry_cycle`、`retry_index`；
- `status`：running、succeeded、failed、interrupted；
- `input_context_json`：本次模型实际可见的脱敏业务输入；
- `input_hash`；
- `output_json`：验证后的 Pydantic 输出；
- `error_code`、`error_message`、`retryable`；
- `reasoning_summary_json`：仅 provider 明确释放的、脱敏且有界的 reasoning summary；不保存 raw/encrypted content；
- `trace_id`；
- `config_snapshot_id`、`agent_definition_version`；
- `started_at`、`completed_at`。

大型证据不复制进 `input_context_json`，只保存 evidence ID、hash、摘要和外部引用。

### 12.3 `diagnosis_tool_executions`

通过 SDK `RunHooks` 和 `ToolContext.tool_call_id` 采集：

- `tool_execution_id` 主键；
- `node_execution_id` 外键；
- `sdk_tool_call_id`；
- `tool_name`、`tool_version`；
- `retry_index`；
- 脱敏后的 `arguments_json` 和 hash；
- `status`、`error_code`、`retryable`；
- 有界 `result_summary_json`；
- 产生的 `evidence_ids_json`；
- `started_at`、`completed_at`、`duration_ms`。

SDK Session 中的工具消息用于模型续接；该表用于审计和管理查询，二者不能互相替代。

### 12.4 `diagnosis_evidence`

- `evidence_id` 主键；
- `run_id`；
- `source_type`、`source_reference`；
- `content_hash`；
- 有界脱敏内容或 `storage_reference`；
- `observed_at`、`collected_at`；
- `tool_execution_id`；
- `metadata_json`。

### 12.5 `diagnosis_sdk_run_states`

仅用于 `waiting_approval`：

- `run_id`、`request_id`；
- 加密的 SDK RunState；
- `sdk_version`、`agent_definition_version`；
- `created_at`、`resolved_at`；
- `decision`、`decision_reason`。

`encrypted_state` 使用 `BUGLENS_RUN_STATE_KEY` 加密，密钥本身不进入 SQLite。不得把序列化 RunState 塞入 `DiagnosisState` 或 Event。

### 12.6 `diagnosis_run_control`

用于不打断活动节点地接收 Cancel：

- `run_id` 主键；
- `cancel_requested_at`；
- `cancel_command_id`；
- `reason`；
- `processed_at`。

Cancel Command 独立原子写入控制表，不抢占活动节点的状态 revision。Runtime 在模型/工具调用结束和下一节点开始前检查该表，然后提交 `canceled`。

## 13. Event 与公开协议 v2

由于 `failed` 的可恢复语义、澄清来源和 Command 集合发生变化，应升级协议 major 版本，而不是悄悄改变 v1。

v2 Event envelope 采用 CloudEvents 1.0 的字段语义，并保留 BugLens 的有序扩展字段：

- `specversion`
- `id`
- `source`
- `subject`
- `type`
- `time`
- `datacontenttype`
- `data`
- 扩展：`runid`、`sequence`、`revision`

领域 Event 至少包括：

- RunStarted
- NodeAttemptStarted
- NodeRetryScheduled
- NodeAttemptFailed
- NodeCompleted
- ToolCallStarted/Completed/Failed（公开内容必须脱敏且有界）
- ToolApprovalRequired/Resolved（只公开 pending 展示字段和决定）
- InputRequired
- InputSkipped
- RunWaiting
- RunResumeAvailable
- RunResumed
- RunCancelRequested
- RunCompleted
- RunFailed
- RunCanceled

数据库 Event 与状态转换同事务提交。SDK streaming token、内部推理或高频进度只能作为可丢失瞬时事件，不能参与恢复判断，也不能持久化隐藏推理内容。

快照响应增加：

- `available_actions`
- `resume_available`
- `active_execution_id`
- 各节点 clarification rounds
- 当前 retry cycle/index 的安全摘要

前端和 CLI 只依赖这些字段，不读取 SDK Session 或 RunState。

## 14. Lease、并发和恢复

- 每次 Command 使用唯一 lease owner，不使用 Runtime 进程级固定 owner。
- lease 增加 fencing token；提交节点结果时必须验证 token 仍为当前值。
- 模型或工具调用期间定期续租，续租不修改 run revision。
- 同一 run 同时只允许一个 Graph 推进者；读取快照和事件不受影响。
- Cancel 走独立 run control 通道，因此可以在活动调用期间被接受。
- 进程启动或获取 lease 时扫描过期 running execution，将其标记 interrupted，再按恢复策略处理。
- Command 幂等记录必须覆盖 Start、Submit、Skip、Resume、Cancel、Approve 和 Reject。

## 15. 安全、隐私和保留

- 修改配置快照序列化，使 `api_key` 等 secret 从实际 `config_json` 中删除，而不只是从 snapshot hash 中删除。
- 对已经落库的旧配置快照执行一次迁移清理；迁移前备份数据库，并验证目标数据库路径。
- `DiagnosisState`、state history、node/tool execution 和 Event 入库前统一脱敏。
- tracing 默认设置 `trace_include_sensitive_data=False`；非 OpenAI 网关且没有 tracing 凭证时按配置禁用导出。
- 不记录模型隐藏推理；只记录 provider 明确释放、脱敏且有界的 reasoning summary 和结构化业务结论。
- 工具参数和结果分别设置字段长度、记录数和总字节上限。
- 大型原始证据使用独立存储引用和内容 hash，数据库只保存受控内容。
- RunState 可能包含上下文和 SDK 元数据，必须加密、限制访问并设置保留期限。
- 使用审批工具前必须配置 `BUGLENS_RUN_STATE_KEY`；没有密钥时 interruption 以稳定的 `sdk_run_state_unavailable` 失败，不把不可恢复的审批状态伪装成 waiting。

## 16. 配置调整

建议新增或调整以下配置：

```yaml
runtime:
  lease_seconds: 60
  lease_renewal_seconds: 20
  retry:
    max_retries: 5
    initial_delay_seconds: 1
    max_delay_seconds: 16
    multiplier: 2
    jitter: true

graph:
  max_investigation_attempts: 2
  max_clarification_rounds_per_node: 2

tools:
  enabled: false
  allowed_nodes: [analyze]
  timeout_seconds: 30
  max_results: 20
  max_result_bytes: 65536

sessions:
  history_item_limit: 100
```

所有影响运行语义的值进入无 secret 的不可变 config snapshot。Resume 不重新解析当前 profile。

将 `openai-agents` 依赖收紧到已验证的 0.22 minor 范围，并更新 lockfile，例如 `>=0.22,<0.23`。升级 SDK 时单独执行兼容性测试，尤其验证 RunState 序列化、retry policy、Session 和 hooks。

## 17. 实施阶段

### 阶段 0：契约冻结

修改：

- `backend/docs/runtime-protocol.md`
- `backend/docs/architecture.md`
- 根目录 `frontend-backend-contract.md`
- 相关 frontend docs

工作：

1. 固化 v2 Command、Event、状态语义和 `available_actions`。
2. 固化每节点澄清、Skip、Resume 和协作式 Cancel 规则。
3. 固化 NodeExecution、ToolExecution、EvidenceRecord Schema。

验收：文档之间没有重复或冲突定义；v1/v2 兼容策略明确。

### 阶段 1：持久化与安全迁移

主要修改：`backend/app/infra.py`、`backend/app/config.py`、迁移测试。

工作：

1. 新增 state history、node execution、tool execution、evidence、SDK RunState 和 run control 表。
2. 给 schema 引入显式版本和幂等迁移器。
3. 修复 config snapshot secret 泄漏。
4. 增加按 run、node、execution、tool_call_id 查询接口。

验收：迁移可重复执行；失败回滚；旧数据库可读；任何公开查询不返回 secret 或完整 SDK 消息。

### 阶段 2：领域模型与纯 Graph

主要修改：`backend/app/models.py`、`backend/app/graph.py`。

工作：

1. 增加 DiagnosisContext、EvidenceRecord、per-node clarification rounds、progress delta。
2. Evaluate 支持 interaction request，允许 source Evaluate/resume Investigate。
3. 实现 Skip 后的确定性路由。
4. 拆分 `prepare_step/apply_result`，删除重复 run 路径。
5. 提交前完整验证状态不变量。

验收：纯 Graph 测试覆盖每条边，不访问数据库、网络或真实模型。

### 阶段 3：Agents SDK 集成

主要修改：`backend/app/agents.py`，新增 tool registry、hooks 和 retry policy 模块。

工作：

1. 升级并收紧 Agents SDK 版本。
2. 使用 SDK `ModelRetrySettings` 和 `ModelSettings.timeout`，关闭底层重复 client retry。
3. 保持每节点独立 SQLiteSession，加入 SessionSettings 和 session input callback。
4. 接入 RunHooks/ToolContext，写入工具审计。
5. 建立只读 `@function_tool` 注册和 guardrail 基础设施，先用 fake 工具验证。
6. 为显式 `needs_approval=True` 的只读工具实现 RunState 加密存取适配器，默认工具仍不启用审批。

验收：模型网络错误由 SDK 重试五次；请求次数不发生嵌套相乘；工具调用具有稳定 call ID 和 evidence ID。

### 阶段 4：Runtime、Resume、Cancel 与并发

主要修改：`backend/app/runtime.py`、`backend/app/application.py`、Store 接口。

工作：

1. 实现一次 Command 驱动到等待或终止边界。
2. 正确地在外部调用前提交 NodeAttemptStarted。
3. 实现统一错误分类、retry cycle 和 ResumeDiagnosis。
4. 实现 SkipUserInteraction。
5. 实现 run control 和协作式 Cancel。
6. 增加 lease 唯一 owner、续租和 fencing token。
7. 实现崩溃后 interrupted execution 恢复。

验收：新进程可从等待/失败检查点恢复，不重做已提交节点；两个执行者不能同时提交；Cancel 不截断当前调用。

### 阶段 5：HTTP、SSE、CLI 和前端

主要修改：protocol、`backend/app/web.py`、`server.py`、`client.py`、`cli.py`、根契约和前端状态层。

工作：

1. 增加 v2 Command/Event DTO。
2. 快照返回 available actions 和执行摘要。
3. UI 支持 Resume、Skip、分类型澄清输入和取消已请求状态。
4. SSE 断线后先回查快照，再按 sequence 追赶。
5. 管理页面展示节点尝试和脱敏工具审计，不展示 SDK 原始消息。

验收：CLI 与 Web 只经 ApplicationService；前端不复制 Graph、重试和状态判断。

### 阶段 6：回归、故障注入和发布

工作：

1. 增加 fake NodeRunner 和 fake tool 的确定性故障注入。
2. 覆盖超时、429/5xx、无效结构化输出、工具失败、崩溃、断线、并发 Resume 和重复 Command。
3. 验证审计记录能完整重建执行时间线。
4. 更新 README、TODO 和 changelog。
5. 在副本数据库演练迁移和回滚，再发布。

## 18. 必须覆盖的测试场景

### Graph

- Analyze 正常进入 Investigate。
- Analyze、Investigate、Evaluate 分别发起澄清。
- Evaluate 澄清回答恢复到 Investigate。
- 每节点澄清计数互不影响。
- 达到澄清上限或 Skip 后继续，并在报告中保留信息不足。
- Evaluate 通过、失败回环、达到定位上限 inconclusive。
- 连续两轮无有效 delta 时触发 no-progress 规则。

### Retry 与 Resume

- 初始调用加五次 SDK 重试，总调用次数最多六次。
- Retry-After 优先于本地 backoff。
- 已开始流式响应时不执行不安全重放。
- 结构化输出错误进入统一节点重试。
- 重试耗尽生成 resume available。
- Resume 复用原 session/config/current node，已成功节点不重做。
- 非 retryable failed 不提供 Resume。

### Session 和上下文

- `{run_id}:{node}` 隔离。
- 同节点 Submit、Skip 后续调用和 Resume 复用 Session。
- 不同节点不共享完整模型消息。
- ContextAssembler 给各节点的字段和 evidence ID 完整、确定。
- Session history limit 与 callback 不重复持久化旧消息。

### 工具

- 工具只在允许节点/profile/tenant 中可见。
- 参数 guardrail、超时、结果上限和脱敏生效。
- 每次工具尝试都有 tool execution 记录。
- 工具结果注册 evidence ID，并可被结论引用。
- fake 工具验证所有路径，不调用真实系统。
- approval interruption 可序列化、跨进程批准/拒绝并恢复。

### 持久化、并发和协议

- 状态、state history、领域 Event 和完成记录原子一致。
- 崩溃留下的 running execution 被标为 interrupted。
- Command 重放不重复执行。
- revision、request ID 和 lease fencing 冲突被拒绝。
- 活动节点期间 Cancel 可被接受，并在安全边界生效。
- SSE 断线不改变运行结果，sequence 追赶无重复。
- v1 数据迁移后仍可读取；v2 客户端忽略未知可选 Event。
- 数据库、Event、API 和 trace 中不出现测试 secret。

## 19. 验证命令

后端：

```bash
cd backend
uv sync --locked --extra dev --extra web
uv run ruff check app tests
uv run ruff format --check app tests
uv run pytest -q
uv build
```

前端：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm typecheck
pnpm build
```

测试不得调用真实模型或真实外部工具。对 SQLite 迁移、SDK Session 参数、严格 Schema、RunState 版本兼容和 retry 次数分别编写测试。

## 20. 完成标准

以下条件全部满足才算完成：

- 任意 run 都能从数据库说明每个 Graph 节点、每次尝试、实际上下文摘要和工具调用结果。
- 一条 Command 能稳定执行到等待或终止边界。
- 用户澄清、Skip、五次自动重试、Resume 和协作式 Cancel 均可跨进程恢复。
- SDK 通用能力被直接复用，项目没有重复实现模型循环、模型传输重试、Session、工具 Schema、工具审批状态或 tracing。
- Graph、业务 checkpoint 与 SDK Session 的职责没有混用。
- CLI/Web 使用同一协议和 ApplicationService，前端不包含 Graph 规则。
- 所有结论可引用证据 ID；未验证假设和信息不足在最终报告中明确区分。
- 全量后端与前端验证通过，数据库和公开接口中无 secret。

## 21. Agents SDK 官方实现参考

后续实现应以锁定版本的 API 和以下官方文档为准，不根据模型记忆猜测 SDK 行为：

- Agents 与 Runner：https://openai.github.io/openai-agents-python/agents/
- Runner 生命周期：https://openai.github.io/openai-agents-python/running_agents/
- 模型 timeout 与 runner-managed retries：https://openai.github.io/openai-agents-python/models/#runner-managed-retries
- Sessions、`session_input_callback` 与 history limit：https://openai.github.io/openai-agents-python/sessions/
- Function tools 与工具 timeout：https://openai.github.io/openai-agents-python/tools/#function-tools
- Guardrails：https://openai.github.io/openai-agents-python/guardrails/
- RunHooks：https://openai.github.io/openai-agents-python/ref/lifecycle/
- 本地 RunContext 与 ToolContext：https://openai.github.io/openai-agents-python/context/
- 工具审批与持久化 RunState：https://openai.github.io/openai-agents-python/human_in_the_loop/
- Tracing 与敏感数据设置：https://openai.github.io/openai-agents-python/tracing/

开始实现前先用当前 `uv.lock` 环境执行 API 签名检查，并将关键行为固化为测试。SDK 升级必须显式更新依赖范围、lockfile、兼容测试和本计划中的版本基线。
