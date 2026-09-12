# 配置与管理交互

管理页面通过 `src/api.ts` 使用公开 Admin JSON API，诊断操作通过 Command/Event 协议。管理控制面不进入 Agent Graph。端点、鉴权、secret 字段和 revision 规则以[根目录契约](../../frontend-backend-contract.md)为准；安装和进程管理以[发布说明](../../distribution/README.md)为准。

## 实例与任务

总览和系统页展示后端返回的健康组件、版本与能力；请求失败区分网络不可达、鉴权失败、校验失败和 revision 冲突。健康异常不改写诊断生命周期。

任务列表展示问题、状态、当前阶段、profile 和更新时间。打开任务后读取快照和审计；提交澄清、Skip、Resume、Cancel、目标确认或审批时，使用该快照提供的操作与待办标识。

全局 Session 页只展示关联 run、节点/编排类型、创建/更新时间、消息数和派生状态。原生生产 loop 使用一个诊断 Session；兼容 Graph 使用隔离的节点 Session。Session 不是业务状态来源，页面不能编辑消息、跨任务挂载 Session、直接删数据库行或手工回放模型历史。

## 配置编辑

配置页提供模型端点、凭据、超时、streaming，以及诊断策略和节点设置。用户编辑的是本地草稿，后端负责严格校验与原子保存。

1. 加载公开配置及 revision。
2. 编辑草稿，使用 validate 检查错误和警告。
3. 保存携带原 revision；冲突时保留草稿并刷新配置。
4. 展示已生效版本及“只影响新任务”的说明。

已有 run 继续使用不可变配置快照。前端不读取配置 YAML、环境变量、SQLite 或 SDK RunState，也不自行重算后端策略。

API Key 作为 write-only 输入发送；已配置状态或掩码用于展示，不当作新凭据回传。不把凭据存入 localStorage、URL、诊断事件或日志。

## 环境与插件

环境页包含目录概览、插件目录和配置编辑。概览展示环境、service/node/source 归属与 source 能力；插件页展示后端发现的插件及实例健康，不允许模型或浏览器指定任意工具、连接地址或文件路径。

目录编辑器只编辑非敏感 JSON。实例凭据使用后端公开的 `is_set` 状态，轮换通过 `secret_updates` 的 `set`/`clear` 提交，并携带目录 revision。健康检查只验证实例连接，不能触发诊断或修复。

目标有歧义时，在诊断工作台展示后端生成的候选，再提交确认 Command。目录热更新不改变已有 run 的环境快照；环境边界和只读查询限制见[插件规范](../../backend/docs/environment-plugin-spec.md)。

## 安装与更新展示

根目录安装入口提供 full/backend 模式，`distribution/buglensctl` 负责状态、启动、停止、更新和备份；前后端依赖与制品独立。系统页展示安装命令与版本说明，当前没有网页更新 Control API。

未配置管理 Token 时，当前 MVP 面向本机或可信内网；配置后使用 Bearer Token。Setup Token、用户角色、多租户认证、后端配置草稿服务、网页更新、任务归档和 Session 保留作业尚未实现，页面不得把这些设计设想展示为已可用能力。

静态前端 Token 只适合受控场景，多用户部署的认证由反向代理或独立认证层完成。SSE 代理、CORS 和运行时目录设置见后端[配置与运维](../../backend/docs/configuration-and-operations.md)。
