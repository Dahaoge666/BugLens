# 前端 Admin API 入口

前端使用的完整 HTTP JSON、鉴权、revision 冲突、secret 字段和 CORS 契约统一维护在根目录的[前后端对接契约](../../frontend-backend-contract.md)。本文只保留前端侧约束：

- 请求通过 `src/api.ts` 发出，不直接读取后端配置文件、环境变量或 SQLite；
- Admin 请求失败必须区分网络不可达、鉴权失败、校验失败和 revision 冲突；
- API key 等 secret 只作为 write-only 输入，前端不写入 localStorage、URL、诊断 Event 或日志；
- 能力和 `available_actions` 以服务端响应为准，前端不根据生命周期状态自行推断操作。
