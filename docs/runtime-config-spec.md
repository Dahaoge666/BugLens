# BugLens 运行配置与快照规范

## 范围与现状

本文定义固定参数的来源、解析和持久化快照。当前实现已将 Graph 上限、评测门槛、节点模型/turns/
prompt 版本收敛到版本化 profile；普通 CLI/Web 请求只能选择 profile。`ConfigRepository` 负责严格解析、
revision 计算和配置写入，Runtime 在创建 run 时保存快照。

## 配置分类

- 启动配置：数据库、监听地址、日志、默认 profile、Web 开关和事件保留期；随部署变化。
- 运行策略：Graph 上限、评测阈值、节点 max turns/model/prompt version、工具限制；创建 run 时快照。
- 请求输入：问题、上下文、证据和允许的 profile；属于 Command。
- 展示选项：JSON、输出文件、颜色、远程地址；只属于 Adapter。

示例：

```yaml
profiles:
  default:
    graph: {max_investigation_attempts: 2, max_clarification_rounds: 2}
    evaluation:
      passing_score: 75
      min_evidence_traceability: 15
      min_verification_executability: 15
      rubric_version: rubric-v1
    nodes:
      analyze: {max_turns: 6, prompt_version: analyzer-v1}
      investigate: {max_turns: 6}
      evaluate: {max_turns: 6}
      summarize: {max_turns: 6, prompt_version: summary-v1}
    tools: {enabled: false, max_results: 20, timeout_seconds: 30}
```

优先级为代码安全上限 > profile > 环境变量选择默认 profile > 内置默认值。普通 CLI/Web 请求不得逐项覆盖策略；管理员通过新 profile/version 修改。

## 模型与快照

使用严格 Pydantic `RuntimePolicy` 和 `ResolvedRunConfig(snapshot_id, profile, config_version, prompt_config_version, policy, resolved_at)`。代码保留不可突破的安全上限。

StartDiagnosis 只选择 profile。Application Service 解析配置，Runtime 在首次事务中保存无 secret 的完整快照并将 snapshot ID 写入 DiagnosisState。恢复只读取快照，不重新解析当前文件；配置更新只影响新 run。

新增 `run_config_snapshots(snapshot_id, profile, config_version, prompt_config_version, config_json, created_at)`。可用规范化配置 hash 作为 ID，多 run 复用；仍被引用的快照不可删除。API Key/凭据只由 Infra 执行时注入，不进入快照。

## 管理 API 与原子更新

`AdminApplicationService` 通过 HTTP Adapter 暴露以下最小控制面：

```text
GET  /v1/admin/config
POST /v1/admin/config/validate
PUT  /v1/admin/config
```

读取结果包含公开 profile、当前 `revision` 和 `writable` 标志，不包含凭据。校验只解析候选 profile，
不会改变内存中的 active 配置。应用请求携带 `profile`、候选 `config` 和 `expected_revision`：

1. 在进程内写锁下比较 revision；
2. 校验目标 profile 和所有现有 profile，确保节点策略、prompt 版本及 config version 合法且唯一；
3. 写入同目录临时 YAML；
4. 使用 `os.replace` 原子替换目标文件，再更新内存副本。

revision 冲突返回稳定的 `config_revision_conflict`（HTTP 409）；未设置 `BUGLENS_CONFIG` 时内置默认配置
是只读的，写入返回 `config_not_writable`（HTTP 409）。配置变更只影响新 run，恢复中的 run 继续读取其
`config_snapshot_id`。

## 参数简化与迁移

CLI 只保留 `--profile`、本地管理员用 `--config`、连接用 `--remote` 和展示参数。移除普通用户的 `--max-attempts`、`--max-clarifications`。

迁移时先引入与现有行为一致的 default profile；旧 flags 短期 deprecated 并映射为临时 profile，下一兼容版本移除。启动时校验全部 profile，未知字段、越界值、缺失节点或重复版本应快速失败。

trace/Event 至少携带 snapshot ID、profile、config version 和 prompt version，但不打印完整 prompt 或 secret。

## 验收标准

- CLI/Web 不逐项传固定 Graph 参数；相同 profile 解析确定且 Schema 严格；
- 非法配置在启动或创建前失败；创建后修改配置不影响恢复；
- checkpoint、trace、Event 可定位到快照且快照无 secret；
- default profile 与现有两次定位、两轮澄清和评测门禁一致。
- 前端可以删除或单独部署；它只能通过 Admin API 修改 profile，不能读取 YAML、环境变量或 SQLite。
