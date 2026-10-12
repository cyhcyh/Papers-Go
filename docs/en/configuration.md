# Configuration

[Contents](index.md) · [简体中文](../zh/configuration.md)

## Where to configure each feature

| Setting | Recommended location | Application |
| --- | --- | --- |
| Name, icons, description, administration entry | Admin → 基本设置 | Save the site settings |
| Connections, API keys, feature models | Admin → 模型配置 | New calls or newly started tasks use the configuration |
| Prompts and skill instructions | Admin → 模型配置 → Prompts / Skills | Used by new calls; cached outputs require separate regeneration |
| Sources, ingestion scope, guest defaults | Admin → 分类与来源 | Subsequent ingestion and browsing use the current registry |
| Task schedules and advanced parameters | Admin → 任务中心 | The worker refreshes saved schedules |
| Startup and timezone parameters | Deployment `.env` | Recreate Docker services after changes |

Refer to the root `.env.example` for runtime parameters. Model selections come from console feature assignments. Upgraded installations may retain earlier settings; a fresh installation does not automatically assign models or populate ingestion sources.

## Common environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TZ` | `Asia/Shanghai` | Scheduler and daily statistics timezone |
| `REQUIRE_INVITE_CODE` | `false` | Require invitation codes after the first account |
| `JWT_SECRET` | Empty | Generate and persist a secret when empty; otherwise managed by the deployer |
| `SCHEDULER_ENABLED` | `true` | Enable scheduled jobs; the worker still handles other background queues |
| `BOOTSTRAP_ENABLED` | `true` | Enable startup initialization, subject to source and model readiness |
| `PIPELINE_MODE` | Host-native `process`; Docker `external` | Worker process management |
| `LLM_TIMEOUT` | `120` | General model timeout; reading has separate long-task budgets |
| `READING_CONTEXT_TOKENS` | `0` | Reading context capacity; zero selects a model-dependent value |
| `READING_CLOUD_CONCURRENCY` | `2` | Initial concurrent cloud / Codex reading cards |
| `READING_LOCAL_CONCURRENCY` | `1` | Initial concurrent local reading cards |
| `CLASSIFY_CLOUD_CONCURRENCY` | `4` | Initial cloud classification concurrency |
| `BRIEF_CLOUD_CONCURRENCY` | `4` | Initial cloud brief concurrency |
| `EMBEDDING_DIM` | `1024` | Initial vector dimension; must match the embedding service |
| `SQLITE_MMAP_MB` | `1024` | Maximum SQLite file mapping range, not reserved physical memory |

The console can persist settings such as concurrency; saved application settings govern their features. `ARXIV_CATEGORIES` and `CONFERENCE_SOURCES` are compatibility parameters and do not replace source registration on a fresh installation.

`FETCH_LIMIT=400` limits the initial trial fetch only. Regular complete incremental synchronization does not use it as a per-run paper cap.

## Persistent data

The business database defaults to `data/papers.sqlite3`. Vector indexes use separate SQLite files under the same data directory. Full-text material, skill files, and site assets also live there. Back up the complete `data/` directory.

Model credentials are encrypted. Docker mounts host `.secrets/` at `/secrets` and sets `MODEL_KEY_FILE` to `/secrets/model-keys.key`. The host-native default is `.secrets/model-keys.key`. If you supply your own `MODEL_ENCRYPTION_KEY`, restoration requires the same key.

## Changes and activation

Console configuration is stored in the database. It does not rewrite default `prompts.toml`, bundled skill files, or `.env`. Running model tasks use configuration snapshots so models and instructions do not change halfway through processing.

After changing deployment environment variables, use the original Compose file arguments to recreate services:

```bash
docker compose up -d --force-recreate app worker
```

A simple `restart` does not inject changed Compose environment variables into an existing container.

[Previous: Local development](development.md) · [Next: User guide](user-guide.md)
