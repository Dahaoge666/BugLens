# SSH 主机诊断插件

独立包 `buglens-ssh-plugin`，插件 ID `ssh`，主机诊断类别。依赖公共插件 API 和 Paramiko，不导入 BugLens 后端。

在环境中选择子服务 → 添加插件 → 主机诊断 → SSH，填写远程主机和连接账号。端口默认 22；可使用后端服务器上的私钥文件、SSH Agent 或当前实例的密码。私钥和 known_hosts 路径指向运行后端的服务器；应由管理员事先核对并加入目标主机指纹。插件采用 RejectPolicy，未知指纹不会自动信任。

只执行固定 Linux 检查：`system`（uname -a）、`uptime`、`disk`（df -Pk）、`memory`（/proc/meminfo）、`processes`（ps 的有限字段）。默认启用前四项，数据源可限制检查集合；Agent 只能指定 source ID 和检查名称，不能提供任意 shell 命令。每次独立连接，不申请 PTY，输出受行数、字节和 deadline 限制，结束后关闭通道与连接。

此驱动与已有 `transport.type=ssh` 的固定 JSON 探针连接器用途不同：主机诊断无需部署探针。健康检查仅验证 SSH 认证与连接；命令可用性在实际检查时判断。

仓库安装：`uv sync --project backend --extra web --extra plugins`。独立构建：在本目录执行 `uv build`。配置示例见 [远程环境示例](../../backend/config/environments.remote.example.yaml)。
