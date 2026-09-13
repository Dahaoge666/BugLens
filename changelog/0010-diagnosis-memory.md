# 0010 — 诊断案例 Memory

- 日期：2026-09-13
- 类型：Added / Security / Docs
- 状态：已落地

## 概述

已完成诊断自动整理成结构化案例，后续诊断在同租户/环境/服务内检索相似问题，获得可追溯的排查方向与验证参考。

## 详细说明

新增确定性整理器、词项检索、SQLite schema v4 及已有完成报告的一次性回填。案例随终态原子提交，每个来源 run 一份；检索引用在节点开始检查点固定，恢复不重新读取案例库。原生 loop 与兼容四节点路径均接入。

主结论和待验证假设分别保存，证据不足案例保留缺口。文本有界并脱敏，历史内容不进入系统指令，历史证据不进入当前证据集合，Graph 拒绝将历史 ID 冒充本次证据。此能力不新增模型调用、工具写操作或诊断路由。

## 关联文件

- `backend/app/memory.py`、`models.py`、`infra.py`、`runtime.py`、`graph.py`、`context.py`、`prompts.py`
- `backend/tests/test_memory.py`
- [诊断 Memory 说明](../backend/docs/diagnosis-memory.md)、[公共契约](../frontend-backend-contract.md)

## 后续

当前使用范围内最近 200 个案例的词项匹配。跨服务推广、语义检索、人工反馈和前端案例库管理页可按后续需求扩展。

## 验证

后端 locked 依赖同步、Ruff 检查及格式检查、199 项 fake/local 测试、sdist/wheel 构建通过；前端 frozen-lockfile 依赖安装、类型检查及构建通过。后端验证使用独立临时虚拟环境，未调用真实模型。
