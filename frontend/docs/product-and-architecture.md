# BugLens 产品与前端架构设计

> 当前实现：`src/App.tsx` 已提供总览、诊断任务、Agent Session、配置初始化和系统更新五个管理视图；`src/api.ts` 对接 HTTP Command/SSE 与 Admin API。未连接后端时显示明确的演示数据，不会伪造写入成功。
>
> MVP 先使用原生 `fetch` 和轻量 hash 导航，保持零运行时依赖、便于独立静态部署；当多租户鉴权和复杂缓存稳定后，再按下表评估 TanStack Query / React Router，不改变 HTTP/SSE 边界。

## 1. 结论

BugLens 前端应定位为**证据驱动的诊断工作台**，而不是通用聊天界面。用户需要持续看见：
问题是什么、当前进行到哪一步、结论依据是什么、还缺什么信息，以及结论是否通过独立评测。

推荐以纯静态 SPA 交付：

| 层面 | 推荐方案 | 原因 |
| --- | --- | --- |
| 应用 | React + TypeScript + Vite | 适合交互密集的独立 API 客户端；产物可放任意静态托管 |
| 路由 | React Router | URL 可直接定位某次诊断，刷新和分享不丢上下文 |
| 服务端状态 | TanStack Query | 管理 Run 快照、命令 mutation、失效与重取 |
| 实时事件 | 自有轻量 SSE transport | 支持 POST 响应流、sequence 去重和断线追赶 |
| 表单 | React Hook Form + Zod | 动态澄清问题和复杂证据输入需要运行时校验 |
| UI 基础 | Tailwind CSS + Radix Primitives | 快速建立一致视觉，同时保留无障碍语义和焦点管理 |
| 单元/组件测试 | Vitest + Testing Library + MSW | 在不运行真实后端和模型的情况下验证状态与协议 |
| 端到端测试 | Playwright + axe-core | 覆盖流式事件、恢复、键盘操作和常见无障碍问题 |

不建议首期使用 Next.js。BugLens 已有明确的 Python Application Service 和 HTTP/SSE 边界，SSR、
Server Actions 与 Node 服务端不会改善诊断主流程，反而会增加部署单元和模糊前后端职责。若未来出现公开、
需 SEO 的知识库，可作为另一个静态文档站处理，不改变诊断 SPA。

## 2. 产品原则

1. **结论必须可追溯**：任何原因假设都同时展示支持证据、反证、置信度和验证步骤。
2. **生命周期与结论分离**：`completed` 表示流程结束，`confirmed/inconclusive` 才表示诊断结果。
3. **等待不是卡死**：`waiting_user` 应转换为明确的待办表单，并解释为何需要这些信息。
4. **进度真实而克制**：只渲染已提交 Event，不伪造百分比、不展示模型“思考过程”。
5. **恢复优先**：刷新、短暂断网或关闭页面后，可以用 run ID 和 sequence 恢复。
6. **协议是唯一边界**：前端不推断 Graph 路由，不自行决定重试 Investigate 或跳过 Evaluate。

## 3. 信息架构

首期只需要三个主路由：

```text
/
├── /new                    新建诊断
└── /runs/:runId            诊断工作台
    ├── ?view=overview      概览与阶段进度
    ├── ?view=evidence      输入证据与提取结果
    ├── ?view=hypotheses    原因假设与验证步骤
    ├── ?view=evaluation    独立评测
    └── ?view=events        事件审计记录
```

本次控制台 MVP 在同一静态入口下增加 `#dashboard`、`#tasks`、`#sessions`、`#settings` 和 `#system` hash
视图；它们只改变前端展示，不改变后端协议。后续引入 React Router 时可无缝映射为上述 URL 路由。

`/` 展示产品说明、“开始诊断”和“打开已有 run”入口。当前 MVP 通过 Admin API 的分页运行列表展示团队
任务；仍不得将完整诊断、证据或答案写入 localStorage。多租户部署应在认证层增加租户隔离后再开放历史列表。

## 4. 核心页面

### 4.1 新建诊断

采用单页渐进式表单，避免让用户理解内部 Agent 节点：

- **问题描述**：必填；提示用户写清症状、影响、时间和环境。
- **上下文**：环境、服务、版本、时间窗口；允许增加自定义键值。
- **证据**：日志、指标、Trace、变更记录、其他；每条包含内容、引用和观察时间。
- **执行配置**：只允许选择后端公开的 `profile`，不暴露重试次数或评测阈值。
- **提交前检查**：显示将发送的信息，提醒敏感数据处理要求。

提交后立即导航到 `/runs/:runId`。run ID 和 command ID 由浏览器生成，命令重试必须复用原
command ID，以利用后端幂等语义。

### 4.2 诊断工作台

桌面端布局：

```text
┌────────────────────────────────────────────────────────────────────┐
│ BugLens   DIAG-…   [运行中/等待输入/已完成]  连接状态      [取消] │
├────────────────────────────────────────────────────────────────────┤
│ 分析问题 ── 定位原因 ── 独立评测 ── 生成报告                      │
├──────────────────────────────────────────────┬─────────────────────┤
│ 主内容                                       │ 诊断上下文          │
│                                              │                     │
│ 当前阶段说明 / 待补充问题                    │ 环境、时间、影响    │
│ 原因假设卡片                                 │ 原始证据引用        │
│  支持证据 · 反证 · 置信度 · 验证步骤        │ 缺失信息            │
│                                              │                     │
│ 最终摘要 / 下一步                            │                     │
├──────────────────────────────────────────────┴─────────────────────┤
│ 活动记录：按 sequence 展示已提交事件，默认折叠技术细节            │
└────────────────────────────────────────────────────────────────────┘
```

移动端按“状态 → 待办 → 结论 → 证据 → 事件”纵向排列；上下文侧栏变为抽屉。

阶段条只是 `current_node` 与事件的可视化，不允许点击跳步。节点文案面向用户：

| 协议节点 | 用户文案 | 说明 |
| --- | --- | --- |
| `analyze` | 理解问题 | 归纳症状、影响、时间和环境 |
| `investigate` | 定位原因 | 基于证据形成可验证假设 |
| `evaluate` | 检查结论 | 独立检查证据和推理质量 |
| `summarize` | 生成报告 | 转写已通过或未决的结果 |

### 4.3 澄清面板

收到 `input_required` 后，在主内容顶部显示高优先级面板，而不是临时 toast：

- 展示 `explanation` 和每个问题的 `rationale`；
- 按 `answer_type` 渲染文本、单选、多选或证据输入；
- 清楚区分必填与选填，最多同时展示三个问题；
- 提交时携带当前 `revision`、`request_id` 以及各问题 ID；
- `revision_conflict` 时先重新获取 Run，不静默覆盖用户答案；
- 提交成功后将焦点移到状态消息，并通过 `aria-live="polite"` 宣布执行已继续。

输入草稿可放 sessionStorage，并以 run ID/request ID 为作用域；提交成功后删除。草稿中可能包含敏感
证据，因此默认不跨浏览器会话持久化。

### 4.4 结果呈现

`confirmed` 与 `inconclusive` 使用不同的信息层级，而不是只换标签颜色：

- **confirmed**：首屏显示主要结论、执行摘要、1–3 个排序假设和下一步验证/处置方向。
- **inconclusive**：首屏显示“证据不足，未确认主结论”，优先列出 evidence gaps、limitations 和
  next data to collect；不得把 `primary_conclusion` 视觉包装为已确认事实。
- **failed**：展示稳定错误码、可重试性和恢复建议，不暴露异常堆栈。
- **canceled**：保留已产生的只读结果与事件记录，禁止继续提交答案。

评测页使用五项水平条展示 `criteria_scores`，总分只是摘要；证据可追溯和验证可执行性必须单独可见，
避免用户把一个总分误解为诊断可信度。

## 5. 前端状态模型

后端 Run 快照是唯一业务真相；TanStack Query 保存查询缓存，React 组件只保存短期 UI 状态。

```text
POST Command 的 SSE 响应 ─┐
                          ├─ 解析 AgentEvent ─ 去重(sequence/event_id)
GET /events?after=N ──────┘                       │
                                                  ├─ 更新事件列表
                                                  └─ invalidate Run 快照
                                                           │
                                                GET /v1/runs/:id
```

不建议首期引入 Redux/Zustand。只有在出现跨页面、无法从 URL/Run 快照派生的复杂客户端状态后再评估。

前端必须显式区分三种状态：

- **领域状态**：`lifecycle_status`、`outcome`、`current_node`、`revision`，全部来自 Run 快照；
- **连接状态**：`connecting/live/recovering/offline/closed`，只描述浏览器连接；
- **命令状态**：`idle/submitting/accepted/error`，只描述一次用户动作。

例如网络中断不等于 run 失败，SSE 关闭也不等于诊断完成。

## 6. HTTP 与 SSE 集成

### 6.1 命令流

当前 API 的 `POST /v1/runs` 和 `POST /v1/runs/:id/commands|cancel` 直接返回
`text/event-stream`。浏览器原生 `EventSource` 不能发送 POST body，因此需使用 `fetch()` 读取
`ReadableStream`，按空行切分 SSE frame，解析 `id:` 与 `data:`。

命令流处理规则：

1. 发送严格的 version 1 Command；
2. 仅在尚未收到任何 HTTP 响应且结果不确定时，允许使用相同 command ID 重试；
3. 每个事件先按 `event_id` 去重，再保证 sequence 单调写入；
4. 收到终态或等待态后关闭本次命令流；
5. 随后刷新 Run 快照，校准前端派生状态。

### 6.2 历史追赶与重连

页面加载或流意外断开时：

1. `GET /v1/runs/:id` 获取最新快照；
2. 使用已持久化在内存中的最大 sequence 请求
   `GET /v1/runs/:id/events?after={sequence}`；
3. 合并、去重并再次刷新快照；
4. 若后端未来把该 GET 端点升级为长连接，可直接使用 EventSource；当前实现返回已有事件后即关闭，
   所以不应做无上限快速重连；
5. 只有 `running` 状态才有限频率退避轮询，等待态和终态不轮询。

推荐退避为 1s、2s、5s、10s，之后固定 15s；页面不可见时暂停，重新可见和浏览器 `online` 时立即
追赶。界面始终显示“连接正在恢复”，不得把连接异常映射为 `RunFailed`。

### 6.3 错误映射

| 后端 code | 前端行为 |
| --- | --- |
| `run_not_found` | 显示不存在/无权限通用页，不泄露资源是否属于其他租户 |
| `revision_conflict` | 保留草稿，刷新 Run，要求用户确认后重提 |
| `invalid_run_status` | 刷新 Run，禁用已过期动作 |
| `pending_request_mismatch` | 刷新待办，保留但不自动提交旧答案 |
| `validation_failed` | 关联到表单；无法定位字段时显示页面级错误 |
| `run_busy` | 提示正在由另一个执行者处理，并按退避策略刷新 |

## 7. 部署与隔离

推荐部署为两个完全独立的制品：

```text
frontend source ── pnpm install --frozen-lockfile && pnpm build ── static dist/
backend source  ── python -m build ────────────── BugLens API + CLI
```

生产入口可采用以下任一方式：

- 推荐：同一域名的反向代理，`/` 指向静态前端，`/api/` 代理到 BugLens API；
- 或者：不同域名部署，后端显式配置允许的 origin、方法和请求头。

前端通过 `VITE_BUGLENS_API_BASE_URL` 注入 API 地址。前端构建不得成为 Python 包构建步骤，后端也
不得负责提供前端静态文件。反向代理只是部署拓扑，不属于 BugLens Application Service。

SSE 代理需要关闭响应缓冲、保留 `text/event-stream`、设置合理的 idle timeout。生产环境建议使用同站点
HttpOnly Secure Cookie；当前实现提供可选 `BUGLENS_ADMIN_TOKEN` Bearer 校验和精确
`BUGLENS_CORS_ORIGIN`，静态前端 token 只适合内网/单用户场景，多用户部署应在反向代理或认证层完成。

## 8. 独立目录约束

实施后的目录应保持：

```text
BugLens/
├── backend/               # Python 后端、测试、配置和后端文档
│   ├── app/
│   ├── config/
│   ├── tests/
│   └── pyproject.toml
├── data/
├── backend/docs/          # 后端实现、架构与协议文档
├── distribution/          # 只引用已发布制品的安装层
└── frontend/              # 前端唯一根目录
    ├── docs/
    ├── public/
    ├── src/
    ├── e2e/
    ├── package.json
    ├── pnpm-lock.yaml
    ├── tsconfig.json
    ├── vite.config.ts
    └── .gitignore
```

CI 使用两个独立 job 和各自的工作目录。前端 job 只缓存 pnpm 依赖并运行 lint、typecheck、unit、build、
e2e；后端 job 保持现有 Ruff、pytest 和 build，不添加 Node 依赖。

## 9. 当前 API 差距与处理顺序

这些差距不阻止完成设计，但会影响生产级前端：

| 优先级 | 差距 | 首期处理 |
| --- | --- | --- |
| P0 | 尚无多用户角色授权和租户隔离 | 已支持可选管理 Bearer Token；多用户公网部署仍需反向代理/认证层 |
| P0 | CORS/OPTIONS | Web Adapter 已支持精确 `BUGLENS_CORS_ORIGIN`；优先采用同域反向代理 |
| P0 | SSE 错误在响应开始后难以用 HTTP status 表达 | 已在发送首帧前捕获初始错误；前端仍需兼容断流并回查 Run |
| P1 | GET events 当前是历史读取，不是持续订阅 | 首期退避追赶；后续可升级为带心跳的长连接 |
| P1 | 多租户审计和复杂分页 | MVP 已提供 Admin 运行列表、状态/节点/profile/关键词过滤 |
| P1 | 无机器可消费的 OpenAPI/JSON Schema | 当前以 `src/types.ts` 的 TypeScript 视图和后端 Pydantic 契约为边界；后续由后端发布契约并做差异测试 |
| P2 | `evidence_upload` 尚无二进制上传协议 | 首期作为结构化 Evidence 文本/引用输入，不显示文件上传假象 |

前端实现不应通过复制 Python 业务逻辑“填平”这些缺口。

## 10. 分阶段交付

### Phase 1：可用 MVP

本次提交已覆盖：总览/任务/Session/配置/系统五个 hash 视图、Admin API 数据同步、断线演示回退、取消与澄清
Command、事件按 `event_id/sequence` 去重、配置 revision 校验，以及 backend/full 独立部署清单。复杂角色授权、
文件证据和真实更新控制仍按后续阶段处理。

- 新建诊断表单；
- Run 工作台与四阶段进度；
- POST SSE 流解析、快照刷新和事件追赶；
- 动态澄清表单与乐观锁冲突处理；
- confirmed/inconclusive/failed/canceled 完整结果页；
- 响应式布局、键盘操作、基础无障碍；
- 后续补充 fake API/MSW 测试，不调用真实模型（当前仓库只提供 typecheck/build 验证）。

### Phase 2：团队使用

- 后端鉴权、租户与 run 列表接口落地后的历史诊断页；
- 搜索、过滤、标签和共享链接；
- 证据引用跳转、报告打印/导出；
- 更完整的审计视图和遥测。

### Phase 3：只读工具接入后

- 日志、指标、Trace 证据查看器；
- tool/approval 等待态；
- 数据采集进度和引用来源健康状态。

## 11. 验收标准

- 删除或不构建 `frontend/` 时，Python CLI/API 的安装、测试和运行完全不受影响；
- 前端不导入后端源码，也不包含 Graph、节点重试或评测通过逻辑；
- 同一 Command 重试复用 command ID，事件按 event ID/sequence 去重；
- 刷新页面能从 Run 快照和 `after` sequence 恢复；
- `waiting_user` 可完成全部 answer type 的输入，且冲突时不丢草稿；
- inconclusive 不呈现为已确认根因；
- 网络离线、流断开和后端 failed 在视觉和语义上相互独立；
- 核心流程通过键盘完成，动态状态可被辅助技术获知；
- 前端测试不调用真实模型，后端测试不需要安装 Node.js。
