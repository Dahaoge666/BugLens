# BugLens Admin 控制面规范

## 目的与边界

Admin 控制面用于初始化后的运维观察和安全配置，不是第二套诊断执行入口。它只访问确定性的
`ConfigRepository`、`SQLiteCheckpointStore` 和安装能力信息；不会调用 `DiagnosisGraph`、复放
Agents SDK 消息，也不会改变正在运行的任务。

```text
frontend/ (静态 SPA) ── HTTP JSON ──> /v1/admin/*
                                         │
                                  AdminApplicationService
                                    │                 │
                         ConfigRepository      CheckpointStore
```

诊断数据面仍由 `/v1/runs/*`、`ApplicationService` 和 `DiagnosisRuntime` 负责。前端删除、单独构建或
单独部署时，CLI 和 HTTP/SSE 诊断 API 必须继续可用。前端源码、依赖和前端文档只能位于 `frontend/`；
本文件只描述后端契约。

## 版本与能力

```text
GET /v1/admin/bootstrap/status
GET /v1/admin/health
GET /v1/admin/version
GET /v1/admin/capabilities
```

`bootstrap/status` 返回 `initialized`、`setup_required`、版本和健康视图。健康视图至少包含：

- `checkpoint_store`：SQLite 是否可执行只读探针；
- `configuration`：`default` profile 是否能严格解析；
- `model_credentials`：是否检测到 `OPENAI_API_KEY`（只返回状态和说明，不返回值）。

能力视图用布尔开关控制界面可见操作。当前 `one_click_update` 为 `false`：安装和更新由
`distribution/buglensctl.*` 编排，浏览器不能直接执行 shell 或容器命令。

## 配置契约

```text
GET  /v1/admin/config
POST /v1/admin/config/validate
PUT  /v1/admin/config
```

读取响应：

```json
{
  "revision": "a8d1c29e4ff2f9aa1d4ab392",
  "writable": true,
  "active_profile": "default",
  "profiles": {"default": {"config_version": "default-v1"}}
}
```

校验和应用请求使用同一结构：

```json
{
  "profile": "default",
  "config": {
    "config_version": "default-v2",
    "prompt_config_version": "tenant-prompts-v1",
    "graph": {"max_investigation_attempts": 2, "max_clarification_rounds": 2},
    "evaluation": {"passing_score": 75},
    "nodes": {
      "analyze": {"max_turns": 6, "prompt_version": "analyzer-v1"},
      "investigate": {"max_turns": 6, "prompt_version": "tenant-prompts-v1"},
      "evaluate": {"max_turns": 6, "prompt_version": "rubric-v1"},
      "summarize": {"max_turns": 6, "prompt_version": "summary-v1"}
    },
    "tools": {"enabled": false, "max_results": 20, "timeout_seconds": 30}
  },
  "expected_revision": "a8d1c29e4ff2f9aa1d4ab392"
}
```

`validate` 只解析候选值，返回 `valid`、`errors`、`warnings`、`config_version` 和预测的
`snapshot_id`，不会修改 active 配置。`PUT` 在进程内写锁下执行以下事务：

1. 比较 `expected_revision`；
2. 校验目标 profile 和全部 profile（四个节点齐全、字段范围合法、版本唯一）；
3. 写入目标文件同目录的临时 YAML；
4. 使用 `os.replace` 原子替换，并更新内存副本。

成功响应是新的配置视图。并发修改返回 HTTP `409` 和 `config_revision_conflict`；没有设置
`BUGLENS_CONFIG` 时配置来自内置默认值，写入返回 HTTP `409` 和 `config_not_writable`。配置变更只
影响之后创建的 run，已有 run 继续读取其不可变 `config_snapshot_id`。任何 API key 或其他 secret
都不属于 profile，不能通过这些端点读取或写入。

## 运行列表与 Session 元数据

```text
GET /v1/admin/runs
GET /v1/admin/sessions
GET /v1/admin/runs/{run_id}/sessions
```

运行列表支持服务端分页和以下查询参数：

```text
lifecycle_status, outcome, node, profile, q, offset, limit (limit 最大 100)
```

每个运行条目返回 `run_id`、问题摘要、生命周期、独立 outcome、当前节点、profile、config version/
snapshot、attempt、clarification_round、是否有待输入、时间和稳定的 `last_error` 信息。生命周期和
结论是两个字段：`completed` 不代表 `confirmed`。

Session 列表支持 `run_id`、`node`、`status`、`offset`、`limit`。每项只返回 `session_id`、关联
run/node、状态、创建/更新时间、消息数量和 config snapshot。SDK `agent_messages` 的内容永远不会
出现在响应中；新安装尚未建立 SDK 表时返回空列表而不是健康错误。

## 认证与跨域

默认不设置 `BUGLENS_ADMIN_TOKEN` 时，管理 API 面向本机/可信内网使用。设置该环境变量后，所有
`/v1/admin/*` 请求必须携带：

```text
Authorization: Bearer <token>
```

缺少或不匹配返回 HTTP `401`、`admin_auth_required` 和 `WWW-Authenticate: Bearer`。比较使用常量时间
比较；token 不写入 checkpoint、配置 profile 或日志。静态前端注入的 `VITE_BUGLENS_ADMIN_TOKEN` 只
适用于单用户/内网，公网多用户部署应在反向代理或认证层终止用户身份和租户授权。

独立域名部署时设置 `BUGLENS_CORS_ORIGIN` 为精确 origin。响应会附带允许的 HTTP 方法和
`content-type, accept, authorization` 请求头；OPTIONS 预检返回 204。同源或 full Compose 部署无需
配置 CORS。生产环境仍应使用 TLS，并由代理为 SSE 关闭缓冲和设置 idle timeout。

## 入口与验收

安装 `web` extra 后可使用 `buglens-web` 启动可选 Uvicorn 入口：

```powershell
python -m pip install -e ".[web]"
buglens-web --host 127.0.0.1 --port 8000
```

`distribution/` 提供 backend-only 和 full 两种 Compose manifest 及跨平台 `buglensctl` 脚本；后端
镜像不包含 Node.js，前端镜像不包含 Python 代码。

验收要求：

- Admin 请求不进入 Graph，诊断 Command/Event 协议保持兼容；
- 校验失败或 revision 冲突不会部分写入配置；
- 运行列表和 Session 列表有界分页，不泄露模型消息和 secret；
- 前端不可用时 CLI、后端 HTTP/SSE 和 backend-only 安装仍可独立工作；
- 后端测试使用 fake runner，不调用真实模型；前端构建从 `frontend/` 独立执行。
