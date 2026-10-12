# Task center

[Contents](index.md) · [简体中文](../zh/pipeline.md)

Open Admin → **任务中心**. This page controls ingestion and processing. Browsing does not require the entire pipeline to finish.

## First run

After configuring sources and models as described in [quick start](quick-start.md), select **运行完整流水线** at the top of the page. The system runs all enabled stages in order without starting each one manually.

Before starting, the pipeline checks model assignments required by enabled stages. If configuration is missing, complete it under **模型配置 → 功能分配**. Disable stages you do not need; they will be skipped.

Track task status and processed counts while it runs. Completed papers are available to browse. Review new topic proposals from classification under **主题管理**.

The full pipeline does not include the independent tasks below. To rerun one stage or investigate a problem, select **现在运行** on its task card.

## Pipeline stages

| Name / identifier | Purpose | Implementation |
| --- | --- | --- |
| arXiv / `fetch_arxiv` | Ingest new and updated papers | Official Atom API, category and date-window pagination, ID/version deduplication, persisted synchronization state |
| 顶会论文 / `fetch_conf` | Ingest the latest conference edition | papers.cool feeds, edition checks, reconciliation and caching, real titles and conference labels |
| 社区信号 / `fetch_community` | Refresh community attention signals | Hugging Face upvotes and related GitHub stars with cached requests |
| 主题分类 / `classify` | Assign one most relevant topic | Candidate-direction retrieval and model decisions; new topics need approval |
| 建立向量 / `build_vectors` | Embed papers and interests | Embedding service, consistent vector space, retrieval indexes |
| 论文质量评估 / `assess_quality` | Compute reference quality | Skill-based assessment of abstracts or cached excerpts, combined with contribution, venue, author, and community signals |
| 论文速读 / `tldr_gen` | Translate titles and produce two-part briefs | Structured model output, validation, per-paper persistence |
| Top 5 预读 / `preread` | Prepare reading cards | Request cards for the five highest-quality papers in the collection, reusing existing cards |
| 趋势聚合 / `trend_stats` | Aggregate topic statistics | Counts and average quality by topic and date; no model call |
| 预警评估 / `alert_eval` | Generate matching alerts | Batched checks against watches and research directions, with incremental state |
| 每日指标汇总 / `metrics` | Aggregate reading activity | Views, likes, saves, skips, and expansions |

Vectors, briefs, and reading cards are shared paper-processing results, not generated from scratch for every user. Personal ranking uses each user's interests and feedback.

## Independent tasks

| Name / identifier | Purpose and implementation |
| --- | --- |
| 作者影响力 / `author_impact` | Identify authors through OpenAlex and cache highly cited signals for quality and ranking; preserve progress when external quotas interrupt work |
| 到期论文清退 / `paper_expiry` | Purge expired papers and related records in batches, deferring queued or generating papers |
| 方向趋势简述 / `trend_report` | Prepare the latest arXiv announcement and needed classification, generate direction summaries for recently active users, reuse caches |
| 画像反思 / `reflect` | Reflect on the last seven days of behavior and update profiles; skip when no new behavior exists |
| 分类抽样评估 / `audit` | Model-assisted classification sampling and reporting, with existing rules for related unclassified papers |

Each has its own schedule and manual control. The trend-report task can synchronize announcements and classify papers before generating text.

## Run, stop, and disable

| Control | Effect |
| --- | --- |
| 现在运行 | Submit the enabled task when the worker is ready |
| 停止 / 取消排队 | Stop the selected running or queued task |
| 停止当前任务 | Stop the current pipeline group |
| 禁用 | Skip the stage in subsequent execution and block manual starts |
| 启用 | Allow subsequent execution without immediately starting it |

Use Stop to end current work. A switch disabling future execution is not a substitute for stopping an active call. Successfully completed paper results remain.

## Schedules

1. Select **计划任务设置**.
2. Enable the pipeline or independent task schedule.
3. Choose daily or weekly execution, time, and weekdays.
4. Save and check the displayed next run and timezone.

Defaults are daily pipeline 10:00, author impact 06:20, expiry 11:00, and direction summaries 10:50; weekly reflection Sunday 04:00 and classification audit Sunday 06:00. The default timezone is `Asia/Shanghai`. Your console's saved schedule is authoritative.

Scheduler settings, task switches, and configuration checks all affect execution. Services must be running at the scheduled time.

## Reprocess existing results

Classification, vector building, quality assessment, briefs, and prereading support reprocessing. Open the task's reprocessing control, select the scope in its dialog, and confirm.

Use this after changing models or instructions, correcting classification, or refreshing old outputs. Ordinary new-paper processing does not require a full redo.

Replacement is **per paper and per processing item**. Successful items keep their new result; a failed or stopped item retains its own old result. Successful work across the whole run is not rolled back. Inspect completed, failed, and pending items before continuing or retrying.

A complete embedding-model change also requires vector-space rebuilding and activation. Use the dedicated [model configuration](models-prompts.md) workflow.

## Advanced settings and troubleshooting

Community signals expose GitHub credentials, lookback, and caching. Author impact exposes OpenAlex credentials, batches, and caching. Audits expose sample size; expiry exposes batches and retention. Start with defaults.

For missing configuration, assign the indicated feature. For worker readiness, check the process. For paper failures, inspect retry status and logs. Ingestion time depends on source count, unsynchronized windows, and external throttling; it has no fixed duration.

[Previous: Model configuration](models-prompts.md) · [Next: Maintenance](maintenance.md)
