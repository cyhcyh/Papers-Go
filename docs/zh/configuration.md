# 配置说明

[目录](index.md) · [English](../en/configuration.md)

## 配置在哪里

| 配置类型 | 推荐入口 | 生效方式 |
| --- | --- | --- |
| 网站名称、图标、描述、管理入口 | 后台 → 基本设置 | 保存站点设置 |
| 模型连接、API Key、各功能模型 | 后台 → 模型配置 | 新调用或新启动任务使用新配置 |
| Prompt 与技能指令 | 后台 → 模型配置 → Prompts / Skills | 新调用使用保存的指令；已缓存结果需另行重做 |
| 分类来源、抓取范围、游客默认来源 | 后台 → 分类与来源 | 后续抓取和浏览使用当前来源设置 |
| 任务计划和高级参数 | 后台 → 任务中心 | 保存计划后由 worker 更新调度 |
| 进程启动、时区等参数 | 部署目录 `.env` | Docker 重新创建服务后生效 |

运行参数参考根目录 `.env.example`；模型选择以后台功能分配为准。已有站点迁移可能保留原配置，新安装不会自动分配模型或创建抓取来源。

## 常用环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | 调度和日统计使用的时区 |
| `REQUIRE_INVITE_CODE` | `false` | 首个账号之后的注册是否要求邀请码 |
| `JWT_SECRET` | 空 | 空值时生成并持久化；固定值由部署者管理 |
| `SCHEDULER_ENABLED` | `true` | 是否启动定时调度；worker 仍负责其他后台队列 |
| `BOOTSTRAP_ENABLED` | `true` | 启动初始化流程开关；仍需满足模型和来源配置 |
| `PIPELINE_MODE` | 本机 `process`；Docker `external` | 工作进程管理方式 |
| `LLM_TIMEOUT` | `120` | 一般模型调用超时，精读另有长任务预算 |
| `READING_CONTEXT_TOKENS` | `0` | 精读上下文容量；0 表示按模型选择 |
| `READING_CLOUD_CONCURRENCY` | `2` | 云端 / Codex 精读默认同时生成篇数 |
| `READING_LOCAL_CONCURRENCY` | `1` | 本地精读默认同时生成篇数 |
| `CLASSIFY_CLOUD_CONCURRENCY` | `4` | 云端主题分类默认并发 |
| `BRIEF_CLOUD_CONCURRENCY` | `4` | 云端论文速读默认并发 |
| `EMBEDDING_DIM` | `1024` | 初始向量维度；必须与所选向量服务相容 |
| `SQLITE_MMAP_MB` | `1024` | SQLite 最大文件映射范围，非预留物理内存 |

并发等设置可在模型配置中保存；保存后的应用配置优先用于该功能。`ARXIV_CATEGORIES`、`CONFERENCE_SOURCES` 是兼容运行参数，不代替新安装的后台来源登记。

`FETCH_LIMIT=400` 只用于首次试运行的数量限制，日常完整增量同步不以此值作为每次上限。

## 数据保存位置

默认业务数据库为 `data/papers.sqlite3`。向量索引使用同一数据目录下的独立 SQLite 文件；全文、技能文件、站点资源等也保存在该目录。应备份整个 `data/`。

模型凭据使用密钥加密保存。Docker 将宿主机 `.secrets/` 挂载到 `/secrets`，`MODEL_KEY_FILE` 指向 `/secrets/model-keys.key`；本机默认路径为 `.secrets/model-keys.key`。若使用 `MODEL_ENCRYPTION_KEY` 自行提供密钥，恢复时也必须提供同一密钥。

## 修改与生效

后台配置保存在数据库中，不改写默认 `prompts.toml`、技能源文件或 `.env`。运行中的模型任务会使用配置快照，避免半途更换模型或指令。

修改部署环境变量后使用原来的 Compose 文件参数执行：

```bash
docker compose up -d --force-recreate app worker
```

单纯 `restart` 不会把修改后的 Compose 环境变量重新注入已有容器。

[上一页：本地开发](development.md) · [下一页：用户手册](user-guide.md)
