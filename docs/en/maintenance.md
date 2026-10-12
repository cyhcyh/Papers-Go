# Maintenance

[Contents](index.md) · [简体中文](../zh/maintenance.md)

## Routine checks

Review task success times, failures, pending topics, and cloud usage in the console. From the deployment directory:

```bash
docker compose ps
docker compose logs --tail=100 app worker
```

For Ollama / GPU deployments, retain the same `-f` file arguments used during installation for all commands below.

## Back up

Stop writes before copying so business data, vectors, and files share a consistent point in time.

### 1. Stop the application and worker

```bash
docker compose stop worker app
```

### 2. Copy complete persistent data

Copy the following to a safe location **outside the deployment directory**:

| Content | Reason |
| --- | --- |
| Entire `data/` | Business and vector databases, reading/full-text caches, skills, and site assets |
| Entire `.secrets/` | Decrypt stored model credentials |
| Active Compose files | Restore mounts, ports, and services |
| `.env`, if used | Restore runtime settings; also preserve any separately supplied encryption key |

Do not copy only the main `papers.sqlite3` file. The full directory can include WAL files and multiple vector databases.

PowerShell example from the deployment directory; choose your own backup location:

```powershell
$backupDir = Join-Path 'D:\PapersGoBackups' (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Path $backupDir | Out-Null
Copy-Item -LiteralPath data,.secrets,docker-compose.yml -Destination $backupDir -Recurse
if (Test-Path .env) { Copy-Item -LiteralPath .env -Destination $backupDir }
```

Include the Ollama / GPU overlay files if used. The Ollama named volume is outside these directories; back it up separately or prepare models again after restoration.

### 3. Start and check

```bash
docker compose start app worker
docker compose ps
```

## Update the published image

1. Complete a backup.
2. Read the target version's change notes.
3. Pull and recreate the application and worker:

```bash
docker compose pull app worker
docker compose up -d --no-deps app worker
```

4. Open the feed and console and inspect models and task state.

`latest` follows the newest published release. To pin a release, set both `app` and `worker` to the same published Docker Hub tag. Keep their versions consistent.

Compose normally uses the release image. To run source modifications, build the appropriate image first; restarting a published image does not apply local source edits.

## Restore

1. Stop the application and worker.
2. Preserve the current installation for rollback, then restore `data/`, `.secrets/`, and configuration from the same backup.
3. Check mount locations, the Compose project name, and the encryption key.
4. Run `docker compose up -d` and verify logs and model access.

An older application may not understand upgraded data structures. Pair an application rollback with a pre-upgrade data backup rather than changing only its image tag.

Do not use `docker compose down -v` for ordinary updates or restoration; it deletes the named Ollama model volume.

## Retention and cleanup

| Data | Management |
| --- | --- |
| Operational logs | Configurable age and count under basic settings; default 30 days |
| Full-text cache | Configurable age and size; separate from reading-card results |
| Interest profiles | Current version plus at most three older versions |
| Expired papers | Batched removal by the expiry task |
| Task, usage, and notification history | Separate bounded policies; details are not all permanent |

Initial paper lifetimes and social extensions are configurable in expiry settings. Archive original papers separately when permanent retention matters. A personal save does not guarantee that a shared paper will never expire.

## Growth

Start with existing shared caches, batching, controlled concurrency, and incremental ingestion. New users mainly add personal interest vectors and ranking work; they do not cause all briefs and reading cards to regenerate.

Add sources gradually and observe backlog, throttling, disk, memory, and request latency. The current implementation uses SQLite and one worker. A planned PostgreSQL migration is not an existing capability.

[Previous: Task center](pipeline.md) · [Next: Frequently asked questions](faq.md)
