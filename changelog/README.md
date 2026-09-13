# Changelog

本目录记录 BugLens 的显著变更与已识别问题。每个文件为一个条目，编号递增。

## 条目

- [0011 — 分类定位指南与入口确认](0011-diagnosis-guides.md)

- [0010 — 诊断案例 Memory](0010-diagnosis-memory.md)
- [0009 — 清理未使用代码](0009-unused-code-cleanup.md)
- [0008 — 连接方式与节点使用说明](0008-connector-transports-and-usage.md)
- [0007 — 按子服务分类接入插件](0007-service-scoped-plugin-integrations.md)
- [0006 — 代码与文档清理](0006-code-and-documentation-cleanup.md)
- [0005 — 自主探索模式](0005-autonomous-exploration-mode.md)
- [0004 — 生命周期与审批恢复](0004-graph-lifecycle-and-approval-recovery.md)
- [0003 — 前端管理请求隔离与服务绑定](0003-frontend-admin-request-isolation-and-server-binding.md)
- [0002 — 多模型策略解耦](0002-model-strategy-decoupling.md)
- [0001 — 网关兼容与结构化输出](0001-provider-compat-and-structured-output-hardening.md)

## 约定

- 文件名：`NNNN-简短英文-slug.md`（如 `0001-provider-compat-and-structured-output-hardening.md`）。
- 每个条目含：日期、变更类型（`Added` / `Changed` / `Fixed` / `Deprecated` / `Removed` / `Security` / `Docs`）、概述、详细说明、关联文件、状态与后续。
- 已落地但尚未正式化的适配标注为 `已适配（待正式化）`；尚未修改的问题标注为 `待修复`，并明确建议的修复方向。
- 测试与可观测性相关的补遗统一登记到仓库根目录的 [`TODO.md`](../TODO.md)，并在本目录对应条目里交叉引用。
