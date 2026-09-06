# 构建与部署指南

## 产物与用途

| 产物 | 用途 | 构建命令 |
| --- | --- | --- |
| Wheel 与源码包 | 本机、虚拟机、内部 Python 制品库 | `python -m build` |
| Docker 镜像 | 测试和服务化部署 | `docker build -t buglens:0.1.0 .` |

版本以 `pyproject.toml` 的 `project.version` 为唯一来源。CI 会执行格式检查、静态检查、测试和 Python 包构建，并保存构建产物。

## 环境配置

复制 `.env.example` 作为本地配置。`.env` 已被 Git 忽略。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无 | 真实模型调用必需 |
| `BUGLENS_HOST` | `127.0.0.1` | 监听地址；容器内为 `0.0.0.0` |
| `BUGLENS_PORT` | `8000` | 监听端口，范围 1–65535 |
| `BUGLENS_LOG_LEVEL` | `info` | 服务日志级别 |
| `BUGLENS_STATE_DIR` | `data/runs` | 需要写权限的状态目录 |
| `BUGLENS_PROMPT_CONFIG` | 无 | 可选租户提示词 YAML 路径 |

租户提示词可从 `config/tenant_prompts.example.yaml` 复制。生产部署应通过只读挂载提供配置，不应将租户配置或密钥打入镜像。

## Python 包

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m build
```

输出位于 `dist/`。验证 wheel 时，在干净虚拟环境安装 `dist/buglens-*.whl`，运行 `buglens`，并访问 `/health`。

## Docker

```powershell
docker build -t buglens:0.1.0 .
docker run --rm -p 8000:8000 `
  -e OPENAI_API_KEY=$env:OPENAI_API_KEY `
  -v buglens-data:/var/lib/buglens `
  buglens:0.1.0
```

本地编排可复制 `.env.example` 为 `.env`，填写 API Key 后运行 `docker compose up --build`。镜像使用非 root 用户，包含 `/health` 健康检查；命名卷保存运行状态，本地 `config/` 以只读方式挂载。

启用租户提示词时，将配置保存为 `config/tenant_prompts.yaml`，并设置 `BUGLENS_PROMPT_CONFIG=/etc/buglens/tenant_prompts.yaml`。

## API 生命周期

- `POST /diagnoses` 创建任务。
- `GET /diagnoses/{run_id}` 查询任务。
- `POST /diagnoses/{run_id}/answers` 恢复等待澄清的任务。
- `POST /diagnoses/{run_id}/cancel` 将等待澄清的任务结束为未决。

默认最多进行两次定位尝试和两轮澄清。未通过评测的假设不会作为已确认主结论返回。

## 发布检查

发布前确认测试和代码检查通过、Python 包成功构建、wheel 可在干净环境导入、Docker 健康检查正常、版本号与 Git/镜像标签一致，且产物不包含 `.env`、运行数据和开发缓存。
