# PostgreSQL 数据库插件

独立包 `buglens-postgresql-plugin`，插件 ID `postgresql`，数据库类别。依赖公共插件 API、Psycopg 3 和 pglast，不导入 BugLens 后端。

在环境中选择子服务 → 添加插件 → 数据库 → PostgreSQL，填写主机、数据库和只读账号。默认端口 5432、SSL `require`、Schema `public`；表白名单、敏感列拒绝列表和证书选项收在高级设置。账号与密码保存在当前实例的凭据字段；连接配置不使用模型提供的 DSN。

每次调用创建新连接，首条语句前启用只读事务，设置 statement/lock timeout 与固定 search_path，结束后回滚并关闭。SQL 经 PostgreSQL AST 校验，只接受单条 SELECT（可含只读 CTE），限制 Schema、表、列，拒绝写入 CTE、SELECT INTO、锁、任意函数和外部访问函数。允许的内置函数固定到 pg_catalog。`:name` 或 `%(name)s` 使用原生参数绑定；结果使用服务端游标并限制行数、字节与 deadline。表结构查询也执行表/列筛选。

数据库账号仍须只授予所需表的 SELECT 权限；视图、自定义类型、操作符与数据库端规则由数据库管理员管理。插件不创建账号、不修改权限。连接检查验证数据库可读，实际表权限在查询时验证。

仓库安装：`uv sync --project backend --extra web --extra plugins`。独立构建：在本目录执行 `uv build`。配置示例见 [远程环境示例](../../backend/config/environments.remote.example.yaml)。
