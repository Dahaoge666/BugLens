# 0011 — 分类定位指南与入口相似问题确认

- 日期：2026-09-13
- 类型：Added / Changed / Security / Docs
- 状态：已落地

## 概述

现象明确且有证据支持的 Memory 自动生成分类定位指南，管理员也可手工导入和维护。Web/CLI 新问题先查找相似指南，确认相似后直接提供指南；无匹配或明确拒绝候选时再进入原诊断流程。

## 详细说明

新增指南领域模型、应用服务、业务 SQLite schema v5 和已有 Memory 的一次性回填。自动指南与诊断终态、Memory 原子提交，稳定来源 ID 避免重复，人工编辑或停用不会被重新生成覆盖。沿用七种问题分类，指南展示现象、适用条件、定位步骤、预期观察、历史结论、未验证原因和限制。

公开入口 JSON 查询不创建 Run、不调用模型；范围内确定性词项检索最多返回三个候选，由提问者确认相似。手工指南可在当前租户内声明通用环境/服务范围，自动指南保持严格范围，已知版本差异被排除。

Admin API 提供分类列表、原子导入及 revision 更新，复用现有管理鉴权，内容脱敏。Web 支持表单与 JSON 文本/文件导入、编辑和启用/停用；CLI 支持 --find-guides、--guide、--diagnose 和 --import-guides。搜索或提交失败保留问题草稿，真实 Run 展示以回查的后端快照为准。

## 关联文件

- `backend/app/guides.py`、`infra.py`、`application.py`、`web.py`、`client.py`、`cli.py`
- `frontend/src/NewDiagnosis.tsx`、`GuidesPage.tsx`、`GuideDetail.tsx`、`guideEntry.ts`、`App.tsx`、`api.ts`
- `backend/tests/test_guides.py`、`test_memory.py`、`frontend/tests/guide-entry.test.mjs`
- [定位指南说明](../backend/docs/diagnosis-guides.md)、[公共契约](../frontend-backend-contract.md)、[JSON 样例](../backend/config/diagnosis-guides.example.json)

## 后续

第一版匹配范围内最近更新的 200 份指南，采用词项重合；语义检索、反馈驱动的指南质量优化与用户角色按后续需求扩展。历史内容不确认本次根因，不自动执行任何排查步骤或修复。

## 验证

后端 locked 依赖同步、Ruff 与格式检查、216 项测试、sdist/wheel 构建通过；前端 frozen-lockfile 安装、类型检查、17 项测试及生产构建通过。实际 CLI 使用临时数据库完成样例导入、查询和指南选用，确认 0 个诊断 Run、0 次节点执行。未调用真实模型。

浏览器连接不可用，页面操作和视觉检查未完成；入口分流通过前端测试及后端/CLI 集成测试验证。
