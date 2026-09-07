# Admin API 对接契约

前端只依赖以下 HTTP JSON 端点；诊断执行仍使用原有的 Command + SSE Event，不在前端重建 Graph。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v1/admin/bootstrap/status` | 首次安装向导需要的初始化、健康和版本状态 |
| GET | `/v1/admin/health` | 后端组件健康检查 |
| GET | `/v1/admin/version` | 后端版本和协议版本 |
| GET | `/v1/admin/capabilities` | 能力开关，前端据此隐藏不可用操作 |
| GET | `/v1/admin/config` | 读取公开运行 profile；不返回密钥 |
| POST | `/v1/admin/config/validate` | 不落盘校验 profile |
| PUT | `/v1/admin/config` | 带 revision 乐观锁的原子配置更新 |
| GET | `/v1/admin/runs` | 分页、状态/节点/profile/关键词过滤的运行列表 |
| GET | `/v1/admin/sessions` | 节点 Session 元数据列表，不返回模型消息 |
| GET | `/v1/admin/runs/{run_id}/sessions` | 某个运行的 Session 元数据 |

配置更新请求示例：

```json
{
  "profile": "default",
  "config": {
    "config_version": "default-v2",
    "prompt_config_version": "tenant-prompts-v1",
    "graph": {"max_investigation_attempts": 2, "max_clarification_rounds": 2},
    "evaluation": {"passing_score": 75, "min_evidence_traceability": 15, "min_verification_executability": 15, "rubric_version": "rubric-v1"},
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

`expected_revision` 不匹配时后端返回 `409 config_revision_conflict`，前端必须重新读取配置并让用户确认差异。
`BUGLENS_CONFIG` 未设置时配置是内置只读默认值，界面应显示“先设置配置文件路径”。API Key 等凭据只允许
通过部署环境或 Secret 管理器注入，前端永远不读取、不写入诊断状态。

若前端部署在独立域名，后端设置 `BUGLENS_CORS_ORIGIN` 为精确的前端 origin（例如
`https://buglens.example.com`）；同源部署或 full Compose 模式不需要 CORS。

公网部署时设置 `BUGLENS_ADMIN_TOKEN`，管理端点要求
`Authorization: Bearer <token>`；前端构建时可在 `VITE_BUGLENS_ADMIN_TOKEN` 注入对应值。
静态前端中的 token 只适合内网或单用户部署，生产多用户场景应在反向代理或未来认证层完成用户鉴权。
