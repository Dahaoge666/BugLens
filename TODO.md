# TODO

本轮遗留项已完成。核心修复（changelog 0001 的 #1–#8）、生命周期实现、只读工具审批恢复和发布前验证均已落地；后续新增事项再在此记录。

## 测试

- [x] **兼容网关契约测试**：新增基于 `httpx.MockTransport` 的真实 Agents SDK 路径测试，覆盖自定义端点的 Chat Completions、流式请求、网关忽略 `response_format` 时的结构化输出解析和 JSON coercion。
- [x] **`_schema_hint` / `_install_json_coercion` 单测**：
  - coercion 表驱动：` ```json{...}``` `、首尾带解释文本、合法 JSON 不被破坏、无 JSON 时安全降级。
  - `_schema_hint` 对四个 `output_type` 均生成非空 JSON Schema 片段且包含 `properties`。
  - 幂等性：多次导入 `app.agents` 不会重复包装 `validate_json`。
- [x] **`streaming` 开关单测**：锁定 `Runner.run_streamed` / `Runner.run` 的分流行为，并保留 SDK Session 回归覆盖。
- [x] **多模型解耦路径单测**：覆盖专用 `OpenAIChatCompletionsModel` 的端点、凭据、超时、缓存复用和 per-model streaming 覆盖。
- [x] **admin API key 脱敏 / 回填单测**：覆盖公开响应脱敏、空/掩码值回填和新值覆盖。

## 后端健壮性

- [x] **列表端点不应因单个损坏 run 而 400**：列表序列化现在跳过损坏行并返回 `degraded_count`，不会因单条非法状态拖垮 Admin API；同时保留状态机校验和回归测试。

## 可观测性

- [x] **`reasoning_content` 纳入可追溯性**：节点审计保存 provider 明确释放的、脱敏且有界的 reasoning summary；不保存 raw/encrypted content，并有回归测试确认隐藏推理内容不会落库。

## 发布前检查

- [x] 后端完整测试、ruff、格式检查和可构建性验证。
- [x] 前端 typecheck 和 production build。
- [x] 安装入口、CLI 帮助和副本 SQLite 数据库迁移/恢复演练。
