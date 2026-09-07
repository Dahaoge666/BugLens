# BugLens 安装、配置与任务管理交互设计

> 实现状态（MVP）：前端已落地总览、任务、Session、配置与系统页；后端新增独立 `AdminApplicationService` 和 `/v1/admin/*` 适配端点；`distribution/` 提供不依赖 Docker 的 backend/full 原生安装和进程管理。

## 1. 设计结论

BugLens 应分成两个逻辑平面：

```text
诊断数据面                              管理控制面
───────────────────────────────        ───────────────────────────────
创建任务、提交澄清、取消任务            初始化、配置、健康检查、版本更新
查询 Run、消费 Event                    任务列表、Session 元数据、审计
        │                                        │
Agent Application Service                Admin Application Service
        │                                        │
        └────────── 共用认证与审计边界 ──────────┘
```

前端只调用公开 API，不读取或改写 YAML、环境变量、SQLite 和 SDK Session 表。管理控制面不调用或复制
Agent Graph；它通过独立的 `AdminApplicationService` 完成配置校验、原子应用、健康查询和更新编排。

用户从界面看到三个稳定概念：

- **实例**：一套正在运行的 BugLens 后端，包含版本、健康状态与能力。
- **诊断任务**：现有协议中的一个 `run_id`，具有生命周期、结果、进度和配置快照。
- **节点会话**：一个任务在 Analyze、Investigate、Evaluate、Summarize 节点使用的 SDK Session；只展示
  脱敏元数据，不展示或编辑 SDK 消息历史。

“登录会话”属于未来认证功能，必须使用不同名称，不与节点会话混在一起。

## 2. 用户角色与权限

首期至少提供三种权限，即使本地单机模式由同一个人使用：

| 角色 | 可查看任务 | 可创建/恢复/取消任务 | 可改配置 | 可更新系统 |
| --- | --- | --- | --- | --- |
| Viewer | 是 | 否 | 否 | 否 |
| Operator | 是 | 是 | 否 | 否 |
| Admin | 是 | 是 | 是 | 是 |

首次安装产生一次性 Setup Token，只能用于初始化管理员。完成初始化后立即失效。管理 API 默认不允许匿名
访问；公网部署必须启用 TLS。密钥字段为 write-only：用户可以覆盖或删除，但前端永远不能读取原值。

当前 MVP 先面向本机/可信内网：未设置 `BUGLENS_ADMIN_TOKEN` 时管理端点可直接访问；设置该环境变量后立即要求
Bearer Token。一次性 Setup Token、角色和多租户授权属于后续认证层，不写入 Agent Runtime。

## 3. 安装与更新

### 3.1 交付形态

推荐同时提供两个可独立发布的制品和一个很小的安装管理器：

```text
buglens-<version>.whl             Python API + CLI，不依赖前端
buglens-frontend-<version>.tar.gz 静态页面，不包含 Agent 代码
buglensctl                        安装、启动、更新、备份、诊断
```

用户只需要选择安装模式，不选择底层组件：

```powershell
# 后端独立安装
buglensctl install --mode backend

# 前后端完整安装
buglensctl install --mode full

# 更新当前安装模式
buglensctl update
```

首期使用 uv 作为跨平台环境管理底座。`buglensctl` 由 uv 使用 Python 3.11 运行，并在安装目录创建隔离虚拟环境、配置、数据、日志
和 PID 文件，负责选择 backend/full 模式、安装固定版本制品、检查健康状态并打印访问地址。full 模式的
轻量同源服务器托管静态资源并代理 `/v1/*`；正式服务器可替换为现有 systemd 与 Nginx/Caddy。

目录保持三个独立工程面：

```text
BugLens/
├── backend/                            # 后端源码、测试、配置与后端文档
├── frontend/                           # 前端源码、测试与前端文档
└── distribution/                       # 原生安装器、进程管理与升级入口
```

正式发行的 `distribution/` 只依赖已发布制品。仓库内安装器允许从本地源码安装后端，并在缺少预构建
前端时调用 pnpm；后端包、前端静态包、安装器仍分别构建和发布。

### 3.2 首次安装流程

命令行安装保持一条主路径：

```text
选择模式 → 检查 Python → 创建隔离环境与数据目录 → 安装制品
        → 启动后端 → 健康检查 → 输出 Setup Token/访问地址
```

终端只显示当前步骤和可恢复错误，完整服务日志写入 `.runtime/logs/`。失败时保留已下载制品和配置草稿，重复执行
同一命令从安全检查点继续。

full 模式启动后，浏览器进入 `/setup`。backend 模式则输出等价的：

```text
buglensctl init
buglensctl config edit
buglensctl doctor
```

这样不安装前端也能完成全部初始化，后端独立使用不受影响。

### 3.3 更新流程

更新必须是可审计、可回退的状态机：

```text
检查更新 → 显示变更与兼容性 → 预检 → 备份
        → 等待运行中任务结束 → 拉取并切换 → 健康验证
        ├── 成功：完成
        └── 失败：自动回滚并恢复旧版本
```

当前单机安装器在停止服务后备份配置和 SQLite，再更新隔离环境与静态制品并执行健康检查。未来增加
长任务或数据库迁移后，应扩展为 drain 状态机，并展示目标版本、迁移要求、预计影响和回滚能力。

管理界面可以触发同一更新编排，但不在浏览器中执行 shell 命令。前端调用本机受保护的 Control API，
Control API 再驱动 `buglensctl`。多节点或托管环境默认隐藏“网页更新”按钮，只显示运维命令。

## 4. 首次配置向导

### 4.1 路由与恢复

未初始化实例访问任何管理页时重定向至 `/setup`。向导状态保存在后端临时配置草稿中，刷新页面可恢复；
浏览器只保存不含敏感信息的 `draft_id`。

```text
1 连接实例 → 2 模型凭据 → 3 数据存储 → 4 诊断策略 → 5 验证 → 6 完成
```

### 4.2 各步骤

#### 连接实例

- 显示后端地址、版本、协议版本和健康状态；
- 输入一次性 Setup Token；
- 对“地址不可达”“TLS 不可信”“版本不兼容”给出不同错误，不统一显示“连接失败”。

#### 模型凭据

- 输入 `OPENAI_API_KEY`，提供显示/隐藏按钮；
- 只发送到 write-only secret API，不写入 localStorage、URL、日志或诊断 Event；
- 保存后只显示“已配置 · 更新于某时”，不返回掩码后的原始密钥；
- 提供“测试连接”，只返回成功、稳定错误码和建议，不回显上游响应详情。

#### 数据存储

- 展示数据库类型与位置，首期固定 SQLite；
- 原生安装默认使用安装器管理的 `.runtime/data/`，生产部署使用权限受限的固定持久目录；
- 展示可写性、可用空间、备份目录和最近一次备份；
- 修改数据库位置需要重启时，在提交前明确标识。

#### 诊断策略

- 先选择内置 preset：稳健、标准、快速；高级模式才展示节点配置；
- 允许设置默认 profile、每节点模型、`max_turns`、澄清轮数和评测阈值；
- 根据后端 Schema 动态限制范围，例如最多两次 Investigate；
- 清楚标识“只影响新任务”。运行中和已完成任务继续引用原不可变快照。

#### 验证

按顺序执行配置 Schema、目录权限、数据库连接、模型凭据和最小模型请求检查。每项显示通过/警告/失败，
失败项可以就地返回编辑；不得为了验证而创建正式诊断任务。

#### 完成

原子提交配置，显示配置版本和审计 ID。向导完成后进入系统概览，不自动创建示例任务。

## 5. 主导航与页面

安装和初始化完成后的主导航：

```text
BugLens
├── 概览             /dashboard
├── 诊断任务         /tasks
├── 节点会话         /sessions
├── 配置
│   ├── 基础设置     /settings/general
│   ├── 诊断策略     /settings/profiles
│   ├── 租户提示词   /settings/prompts
│   └── 密钥         /settings/secrets
└── 系统
    ├── 健康状态     /system/health
    └── 版本与更新   /system/update
```

“新建诊断”是全局主操作，始终位于顶部；配置和系统入口只对 Admin 显示。移动端使用抽屉导航，但任务
状态和待补充数量仍固定在首屏。

## 6. 系统概览

首页不是营销页面，首屏直接回答四个问题：系统是否健康、有没有待处理任务、正在运行什么、最近结果如何。

```text
┌───────────────────────────────────────────────────────────────────┐
│ 实例正常 · v0.1.0 · API v1                [＋ 新建诊断]          │
├──────────────┬──────────────┬──────────────┬──────────────────────┤
│ 运行中  3    │ 等待输入  2  │ 今日完成 18  │ 未决结果 4           │
├──────────────┴──────────────┴──────────────┴──────────────────────┤
│ 需要处理                         │ 最近任务                      │
│ 2 个任务等待补充信息             │ 状态 / 阶段 / 更新时间         │
├──────────────────────────────────┴────────────────────────────────┤
│ 系统健康：模型 / 数据库 / Event Store / 磁盘 / 版本              │
└───────────────────────────────────────────────────────────────────┘
```

数字卡片均可点击进入已带筛选条件的任务列表。健康警告不与任务失败混用颜色和文案。

## 7. 诊断任务管理

### 7.1 列表

任务列表采用服务端分页，默认按更新时间倒序。支持以下最小筛选：

- 生命周期：running、waiting、completed、failed、canceled；
- 结果：confirmed、inconclusive；
- 当前节点；
- profile；
- 创建时间；
- question/run ID 关键字。

每行显示：问题摘要、状态、当前阶段、结果、profile、创建者、更新时间和待办数量。`completed` 与
`confirmed` 必须分两列或两个标签显示，避免把流程完成误解为结论确认。

批量操作首期只提供“导出元数据”和“归档”。取消任务、清理数据等破坏性操作必须逐个确认，不允许在
默认列表中一键全选删除。

### 7.2 任务详情

沿用现有诊断工作台，增加管理信息：

- 顶部：run ID、生命周期、结果、当前阶段、revision、配置快照；
- 主区：问题、证据、假设、评测、报告、待补充问题；
- 侧栏：负责人、创建时间、最近活动、连接状态；
- 底部：Event 审计时间线；
- “执行会话”页签：四个节点会话的只读元数据。

允许动作由后端 `available_actions` 返回，前端不根据状态自行推断：

| 动作 | 可用条件 | 交互 |
| --- | --- | --- |
| 提交补充 | `waiting_user` 且 request/revision 匹配 | 原位结构化表单 |
| 取消 | 非终态且后端允许 | 确认原因后提交 |
| 重试基础设施失败 | `failed` 且 `retryable` | 创建恢复 Command，不复制任务 |
| 归档 | 终态 | 从默认列表隐藏，不删除证据 |
| 导出 | 有读取权限 | 生成脱敏 JSON/报告 |

## 8. Session 可视化管理

### 8.1 展示边界

SDK Session 的职责是保存节点模型上下文，不能成为第二套业务状态。前端只展示：

- session ID 的短标识；
- 关联 run ID 和节点；
- 创建/最近活动时间；
- 状态：未创建、活跃、等待、已结束、异常；
- SDK/provider 类型和健康状态；
- 可选的 turn 数与存储大小统计；
- 使用的 prompt/config snapshot 版本。

前端不展示内部思维、不允许编辑消息、不允许把一个节点 Session 挂到其他任务，也不自行重放历史。

### 8.2 任务内视图

```text
Analyze Session ── 已结束 ─┐
Investigate Session ─ 活跃 ├─ 关联任务 diag_xxx
Evaluate Session ── 未创建 ┤
Summarize Session ─ 未创建 ┘
```

当前节点使用强调色；等待用户时标注“等待业务输入”，而不是把 Session 显示为“挂起进程”。点击会话只
打开元数据抽屉和关联 Event，不加载完整消息内容。

### 8.3 全局 Session 页

主要用于运维排错和容量观察，默认仅 Admin 可见。支持按节点、健康状态、最近活动和关联任务筛选。
首期只读；清理通过统一保留策略执行，不能直接删除 SDK 数据库行。只有终态任务超过保留期限且 SDK
提供安全删除能力时，后台清理作业才能清除会话，并记录审计事件。

## 9. 后端配置修改

### 9.1 编辑模型

配置页面采用“已生效版本 + 草稿”模型：

```text
加载 active revision
        ↓
创建 draft ─ 编辑 ─ 实时本地校验
        ↓
后端 validate（不应用）
        ↓
显示结构化 diff 与影响范围
        ↓
确认 apply(expected_revision)
        ├── 成功：产生新 config version
        └── 冲突：保留草稿并显示三方差异
```

保存按钮不得直接覆写 active 配置。应用后只影响新创建任务；旧任务仍使用其持久化的
`config_snapshot_id`。需要重启的设置进入 `pending_restart`，界面显示“已保存，尚未生效”。

### 9.2 页面组织

- **基础设置**：默认 profile、Tracing 开关、数据保留策略、只读部署信息；
- **诊断策略**：Graph、Evaluation、四个节点策略，以 profile 为单位管理；
- **租户提示词**：按 tenant/category 编辑，显示版本与预览；
- **密钥**：OpenAI Key 等 write-only secret，只显示配置状态；
- **原始配置**：只读 YAML/JSON 预览和下载，首期不提供自由文本直接应用。

高风险字段提供简短影响说明，例如提高 `max_turns` 会增加耗时和成本。模型下拉选项应来自后端能力
查询，不在前端硬编码当前模型列表。

## 10. 管理 API 需求

现有 `/v1/runs` API 保持不变。建议新增独立 `/v1/admin` 和 `/v1/control` 命名空间：

### 10.1 初始化与健康

```text
GET  /v1/admin/bootstrap/status
POST /v1/admin/bootstrap
GET  /v1/admin/health
GET  /v1/admin/version
GET  /v1/admin/capabilities
```

### 10.2 配置

```text
GET  /v1/admin/config
POST /v1/admin/config/drafts
PUT  /v1/admin/config/drafts/{draft_id}
POST /v1/admin/config/drafts/{draft_id}/validate
POST /v1/admin/config/drafts/{draft_id}/apply
PUT  /v1/admin/secrets/{secret_name}
DELETE /v1/admin/secrets/{secret_name}
```

所有修改请求带 `command_id` 和 `expected_revision`；验证和应用返回字段级错误、警告、结构化 diff、
是否需要重启以及审计 ID。密钥接口永不返回 value。

### 10.3 任务与 Session 查询

```text
GET /v1/admin/runs?status=&outcome=&node=&profile=&cursor=&limit=
GET /v1/admin/runs/{run_id}/available-actions
GET /v1/admin/runs/{run_id}/sessions
GET /v1/admin/sessions?node=&health=&cursor=&limit=
GET /v1/admin/sessions/{session_id}
```

Session 响应只包含允许的元数据，不包含 SDK 消息数组或 provider secret。

### 10.4 更新控制

```text
GET  /v1/control/updates
POST /v1/control/updates/check
POST /v1/control/updates/{version}/stage
POST /v1/control/updates/{version}/apply
GET  /v1/control/operations/{operation_id}
POST /v1/control/operations/{operation_id}/cancel
```

这些接口只在受支持的单机完整安装中启用。它们返回持久化 Operation 状态，浏览器刷新后仍可继续查看，
不能依赖一个长期挂起的 HTTP 请求。

## 11. 状态与反馈规范

任务生命周期统一映射：

| 后端状态 | 用户文案 | 颜色语义 | 是否需要操作 |
| --- | --- | --- | --- |
| `created` | 已创建 | 中性 | 否 |
| `running` | 正在诊断 | 蓝/青 | 否 |
| `waiting_user` | 等待补充 | 琥珀 | 是 |
| `waiting_tool` | 等待数据 | 紫 | 视情况 |
| `waiting_approval` | 等待批准 | 琥珀 | 是 |
| `completed` | 流程已完成 | 绿色 | 查看 outcome |
| `failed` | 执行失败 | 红色 | 按 retryable 决定 |
| `canceled` | 已取消 | 灰色 | 否 |

状态变化通过 `aria-live="polite"` 宣布，但不强制移动焦点。需要用户立刻决定的批准和高风险确认使用
对话框并管理焦点。离线、SSE 重连和后端失败使用不同文案，不能把网络状态写成任务状态。

## 12. 实施顺序

### Phase A：只读管理

- 新增健康、版本、任务列表和 Session 元数据查询；
- 前端增加概览、任务列表、系统健康和任务内 Session 页签；
- 不改变现有诊断协议和 Graph。

### Phase B：安全配置

- 新增初始化、配置草稿、校验、diff、应用和 secret API；
- 前端实现 `/setup` 与配置中心；
- 配置变更审计、乐观锁和运行快照回归测试。

### Phase C：一键交付

- 独立 `distribution/` 工程；
- backend/full 安装模式、健康检查、备份、drain、升级与回滚；
- full 模式网页更新控制；backend 模式保持 CLI 等价操作。

### Phase D：运维增强

- Session 容量趋势、保留策略和合规清理；
- 多节点部署时对接外部编排平台，只读展示更新状态。

## 13. 验收标准

- backend-only 安装和更新完全不要求 Node.js 或前端制品；
- full 安装用一条命令完成，并能通过 `/setup` 初始化；
- 浏览器无法读取已有 secret、任意文件或 SDK 消息历史；
- 配置先验证和展示 diff，再原子应用；并发修改不会静默覆盖；
- 配置变更只影响新任务，恢复任务继续使用原快照；
- 任务列表的 lifecycle 与 outcome 分开展示；
- Session 状态不参与 Graph 路由，前端不保存或回放模型消息；
- 更新前自动停止服务并备份，更新后执行健康验证；
- 前端、后端、distribution 的构建与发布相互独立。
