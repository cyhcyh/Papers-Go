# 运行维护

[目录](index.md) · [English](../en/maintenance.md)

## 日常检查

先在后台查看任务成功时间、失败记录、待批准主题和云端用量。需要检查容器时，在部署目录执行：

```bash
docker compose ps
docker compose logs --tail=100 app worker
```

更新和恢复时保留当前项目名、Compose 配置和全部挂载，不要用新安装示例覆盖它们。如果安装时使用了 `-p` 或 `COMPOSE_PROJECT_NAME`，下面的命令也要沿用同一设置。

使用 Ollama / GPU 叠加配置的部署，以下所有命令都需保留安装时的同一组 `-f` 参数。

## 备份

备份前停止写入，使数据库、向量和文件保持同一时点。

### 1. 停止网站与工作进程

```bash
docker compose stop worker app
```

### 2. 复制完整数据

将下列内容复制到**部署目录以外**的安全备份位置：

| 内容 | 为什么需要 |
| --- | --- |
| 整个 `data/` | 业务库、独立向量库、精读与全文缓存、技能、站点资源等 |
| 整个 `.secrets/` | 解密数据库内的模型凭据 |
| 使用中的 Compose 文件 | 恢复相同挂载、端口与服务 |
| `.env`，如有 | 恢复运行参数；自行提供的加密密钥也须妥善备份 |

不要只复制 `papers.sqlite3` 主文件。完整目录可能包含 WAL 等配套文件与多个向量数据库。

PowerShell 示例，在部署目录执行，并把目标改成自己的备份位置：

```powershell
$backupDir = Join-Path 'D:\PapersGoBackups' (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Path $backupDir | Out-Null
Copy-Item -LiteralPath data,.secrets,docker-compose.yml -Destination $backupDir -Recurse
if (Test-Path .env) { Copy-Item -LiteralPath .env -Destination $backupDir }
```

若使用拆分 Ollama / GPU 配置，一并复制对应文件。Ollama 命名卷不在上述目录中，需要单独备份或在恢复后重新准备模型。

### 3. 启动并检查

```bash
docker compose start app worker
docker compose ps
```

## 更新发布镜像

1. 按上述流程完成备份。
2. 查看要更新版本的变更说明。
3. 拉取并重新创建应用和 worker：

```bash
docker compose pull app worker
docker compose up -d --no-deps app worker
```

4. 打开首页、后台，检查模型连接和任务状态。

`latest` 跟随最新发布。如需固定版本，把 `app` 与 `worker` 同时改为 Docker Hub 已发布的同一标签。不要让两个服务使用不一致的版本。

Compose 默认拉取发布镜像；若运行自己的源码修改版，要先构建对应镜像，不能仅重启发布镜像期待源码生效。

## 恢复备份

1. 停止网站和 worker。
2. 保存当前目录以备回退，再将同一份备份中的 `data/`、`.secrets/` 和配置文件还原。
3. 确认挂载路径、Compose 项目名和模型密钥一致。
4. 执行 `docker compose up -d`，检查日志和模型连接。

旧版本程序可能不支持更新后的数据结构。回退应用版本时，应配合升级前的数据备份，而不是只换镜像标签。

不要使用 `docker compose down -v` 来完成普通更新或恢复，它会删除命名卷中的 Ollama 模型。

## 数据保留与清退

| 对象 | 管理方式 |
| --- | --- |
| 运行日志 | 基本设置中配置保留天数和条数，默认 30 天 |
| 全文缓存 | 基本设置中配置容量和时间；缓存不等于精读结果 |
| 兴趣画像 | 当前版本加最多 3 个旧版本 |
| 到期论文 | “到期论文清退”任务按到期规则分批删除 |
| 任务、用量、通知等历史 | 系统按各自策略整理，不保证永久保留全部明细 |

论文初始和社交延长期限可在清退高级参数中设置。需要长期保存的原文应另行归档，收藏是个人引用，不保证永久阻止共享论文清退。

## 用户和分类规模扩大

先利用已有共享缓存、批量任务、受控并发和增量同步。新用户主要增加个人兴趣向量及推荐开销，不会把所有论文的速读和精读重复生成一遍。

扩展来源时逐步增加，观察任务积压、API 限流、磁盘、内存与响应速度。当前使用 SQLite 和单 worker，文档不把计划中的 PostgreSQL 迁移描述为已实现能力。

[上一页：任务中心](pipeline.md) · [下一页：常见问题](faq.md)
