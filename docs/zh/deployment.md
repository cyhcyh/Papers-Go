# 环境部署

[目录](index.md) · [English](../en/deployment.md)

## 选择部署方式

| 方式 | 适用情况 | 文件 |
| --- | --- | --- |
| 应用与 worker | 使用云端模型或已有 Ollama | `docker-compose.yml` |
| 应用、worker 与 Ollama | 希望一起管理本地模型服务 | 再叠加 `docker-compose.ollama.yml` |
| 上述服务与 NVIDIA GPU | Docker 已具备 GPU 支持 | 再叠加 `docker-compose.gpu.yml` |
| Conda 源码运行 | 调试、开发或查看源码行为 | 见[本地开发](development.md) |

部署前确认 Docker 运行于 Linux 容器模式。发布镜像为 `linux/amd64`。Compose 需 2.30 或更新版本；Linux 的 NVIDIA GPU 运行方式需主机安装 NVIDIA Container Toolkit，Docker Desktop 则需先满足其 GPU 使用条件。

## 基础部署

下载仓库后，在仓库根目录执行：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

访问 `http://localhost:8000`。这组配置直接使用发布镜像，修改本机源码后执行该命令不会自动把源码打包进镜像。

需要变更网站端口时，在您自己的部署配置中将 `8000:8000` 改为例如 `8080:8000`。前一个端口供浏览器访问，后一个保持容器内 API 端口。保存后重新执行 `docker compose up -d`。

## 同时启动 Ollama

CPU 或已有其他加速方式的基础配置：

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml pull
docker compose -f docker-compose.yml -f docker-compose.ollama.yml up -d
```

NVIDIA GPU 配置：

```bash
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.gpu.yml pull
docker compose -f docker-compose.yml -f docker-compose.ollama.yml -f docker-compose.gpu.yml up -d
```

配置只启动模型服务，不会预设或自动下载模型。由您准备所需生成模型和向量模型，再在后台获取模型目录并分配。

后续查看日志、更新或停止服务时，应继续使用安装时同一组 `-f` 参数。关于 Ollama 模型和 GPU 的准备，参阅 [Ollama Docker 文档](https://docs.ollama.com/docker)。

## 填写模型连接地址

| 模型服务位置 | 后台填写的示例地址 |
| --- | --- |
| 同一 Compose 项目的 Ollama | `http://ollama:11434` |
| Docker 宿主机上独立运行的 Ollama | `http://host.docker.internal:11434` |
| 同一局域网其他设备上的 Ollama | `http://设备地址:11434` |
| 本机 Conda 后端访问本机 Ollama | `http://localhost:11434` |
| 云端兼容服务 | 服务商提供的完整 Base URL，例如以 `/v1` 结尾的兼容地址 |

容器中的 `localhost` 指容器自身。使用宿主机 Ollama 时，服务需要允许来自容器的连接；请根据本机网络环境配置监听与防火墙。

Ollama 默认宿主机绑定地址为 `127.0.0.1`，端口为 `11434`。该端口已被占用时，可在自己的 `.env` 中调整 `OLLAMA_PORT`。同一 Compose 网络内仍用 `http://ollama:11434`。

## 持久化目录

| 宿主机位置 | 容器位置 | 用途 |
| --- | --- | --- |
| `data/` | `/data` | 数据库、向量文件、全文和精读缓存、技能、站点资源 |
| `.secrets/` | `/secrets` | 模型凭据加密密钥 |
| Ollama 命名卷 | `/root/.ollama` | 下载的模型 |

保留项目名 `shualunwen` 和这些挂载，才能继续使用已有数据。不要同时启动多个 worker 操作同一份数据。

## 检查启动状态

```bash
docker compose ps
docker compose logs --tail=100 app worker
```

访问 `http://localhost:8000/api/health` 检查 API 健康状态。该检查不表示模型配置、论文来源或某个后台任务已经成功。

## 局域网或公网访问

同一局域网可使用主机 IP 加网站端口访问，并允许对应防火墙入站。公开访问时配置 HTTPS 反向代理；智能助手使用 SSE，代理需允许长连接并关闭流式响应缓冲。PWA 安装需要安全上下文，localhost 可作本机开发例外。

第一次启动成功后的操作见[快速开始](quick-start.md)。升级和恢复数据见[运行维护](maintenance.md)。

[上一页：快速开始](quick-start.md) · [下一页：本地开发](development.md)
