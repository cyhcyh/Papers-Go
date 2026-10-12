# 本地开发

[目录](index.md) · [English](../en/development.md)

## 准备环境

需要 Git、Conda 和 Node.js 22。仓库的 `environment.yml` 使用 Python 3.12，并安装后端及测试依赖。

在仓库根目录执行：

```bash
conda env create --prefix ./.conda-env -f environment.yml
conda activate ./.conda-env
```

首次配置时复制环境变量示例。PowerShell：

```powershell
Copy-Item .env.example .env
```

Bash：

```bash
cp .env.example .env
```

已有 `.env` 时保留自己的配置，不要用示例覆盖凭据。模型连接和所用模型在后台配置；本机 Ollama 地址通常为 `http://localhost:11434`。

## 运行后端

在激活 Conda 的项目根目录终端运行：

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000 --reload
```

默认 `PIPELINE_MODE=process`，API 生命周期会启动独立 worker 子进程。不要再手工启动第二个 worker。

## 运行前端

另开一个终端：

```bash
cd frontend
npm ci
npm run dev
```

访问 Vite 输出的地址，通常为 `http://127.0.0.1:5173`。`frontend/vite.config.ts` 将 `/api` 请求转发到 `127.0.0.1:8000`。若改变后端端口，也需同步自己的开发代理配置。

## 工作进程模式

| `PIPELINE_MODE` | 行为 |
| --- | --- |
| `process` | 默认本机模式，API 自动启动独立 worker |
| `external` | API 不启动 worker，由部署系统单独启动；仓库 Docker 使用此模式 |
| `inline` | 测试使用，在 API 进程中运行后台逻辑 |

确实需要手工管理 worker 时，先让 API 使用 `external`，再从同一项目根目录、同一 Conda 环境启动：

```bash
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
```

另一个 PowerShell 终端中：

```powershell
$env:PYTHONPATH = (Resolve-Path backend).Path
python -m app.worker
```

两个进程须使用相同的 `DATA_DIR`、加密密钥和运行配置。

## 检查与构建

后端测试，在项目根目录执行：

```bash
python -m pytest backend/tests
```

前端检查，在 `frontend/` 执行：

```bash
npm run check:math
npm run check:chat
npm run check:dwell
npm run check:auth
npm run build
```

如需将自己修改的源码制作成 Docker 镜像：

```bash
docker build -t papers-go:local .
```

构建完成后，在自己的部署配置中将 `app` 与 `worker` 都指向该本地镜像，再重新创建服务。仓库 Compose 默认使用 Docker Hub 发布镜像。

[上一页：环境部署](deployment.md) · [下一页：配置说明](configuration.md)
