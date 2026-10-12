# Frequently asked questions

[Contents](index.md) · [简体中文](../zh/faq.md)

## The website does not open. What should I check?

Run `docker compose ps` in the deployment directory. Check Docker, `app`, and port conflicts, then inspect `docker compose logs --tail=100 app worker`. The default is `http://localhost:8000`; use the configured port if changed. Other LAN devices must use the deployment host's IP, not their own localhost.

## Why is the feed empty after installation?

A fresh installation has no registered ingestion sources. Add categories under **分类与来源**, allow ingestion, and run the arXiv or conference task. Guests also need guest-default sources. Signed-in users should check interest and date filters. Model configuration alone does not populate papers.

## Is there a default administrator account?

No. The first registered account becomes an administrator; subsequent accounts are ordinary users by default. Save the random administration entry shown after initial registration.

## What if I forget the administration entry?

Ask another administrator with access to check **基本设置**, or use an already-open console. Creating another account does not repeat first-user privileges. Without access, the deployment maintainer should recover the entry from configuration or backups rather than delete the database.

## I entered an API key. Why does a task still need configuration?

Connections and assignments are separate. After saving the connection, select the connection and model for each feature under **功能分配**. Classification needs both a classification model and embeddings. Follow the missing-feature message.

## Why can a model chat but not create vectors?

Embeddings use a different model and endpoint. Select an embedding model and supported dimensions. If a service only provides chat, add another embedding service or use an Ollama embedding model.

## Why cannot Docker reach host-native Ollama?

Container localhost points to the container. Host-native Ollama normally uses `http://host.docker.internal:11434`; Compose Ollama uses `http://ollama:11434`. Check its listening interface, port access, and installed models.

## Ingestion keeps running. Is it stuck?

Check whether the processed count increases and inspect detailed source logs. arXiv pages through unsynchronized date windows. Many sources, a large initial gap, or throttling can extend the run. Initial `FETCH_LIMIT` is not a regular synchronization cap. Conference work normally targets the latest edition but can wait on slow sources. Use Stop if needed.

## Papers exist, but translated titles and briefs are missing.

Ingestion saves original metadata. Run **论文速读** to generate translations and both brief sections. Check its assigned model and failed items.

## Why are no topics shown in the tree?

Catalog entries are not approved topics. Approve proposals or add topics under **主题管理**, then verify classification. Ingestion alone does not assign topics. Disabling a topic also removes paper labels.

## Does renaming a topic update paper labels?

Yes. Papers reference a topic ID and show the current name after reloading. A change in scientific meaning may require reclassification; a name correction does not require regenerating each label.

## Does deleting a topic delete its papers? What about a category?

Topic disabling/deletion retains papers and adjusts topic references. Source-category deletion can delete papers exclusive to those sources, as shown in the impact preview. Papers with another retained source remain. Review the confirmation because the operations have different effects.

## Why do recommendations look unchanged after editing interests?

Check that the background interest update has completed. The form save precedes vector generation. Confirm that sources contain papers, topics are approved, and temporary interests are active. Similar candidate papers can also produce similar recommendations.

## Why does “today viewed” not increase immediately?

A home-feed card needs about five continuous visible seconds. Merely loading or quickly swiping past it does not count. Statistics use the current account and site timezone.

## Why is reading queued? Does closing the page cancel it?

Shared reading concurrency is limited. The count shows waiting jobs ahead in the same resource queue. Closing the page does not cancel accepted work. Requests for one paper reuse its task and cache. Waiting jobs survive worker restarts; interrupted active model calls require retry.

## Why is there no thinking percentage?

Providers do not supply a reliable total reasoning workload. The interface shows status, elapsed time, and generated questions. Hidden reasoning does not mean the worker is idle.

## Why did an old reading card not change after editing its Prompt?

Existing caches are not automatically regenerated. Validate the new instruction on a new call, then have an administrator reprocess selected briefs or cards. Reprocessing consumes model resources.

## Why are there no hotspots or no analysis of my direction?

Check active interests, matching new submissions, classification, and approvals. A hotspot needs at least three new papers in one topic. **趋势聚合** produces statistics; **方向趋势简述** produces the narrative.

## Why does reading continue after disabling the schedule?

Schedules, pipeline switches, and the user reading queue are separate mechanisms. The worker continues accepted reading requests. Disabling scheduled jobs does not stop every background queue.

## Does 30-day log retention mean I must re-enter keys monthly?

No. It applies to logs. Encrypted credentials remain usable with the original encryption key unless the provider invalidates them. Back up `data/` together with `.secrets/`.

## Can GitHub run the whole system without a server?

GitHub Pages hosts documentation but cannot run this application's Python API, database, and persistent worker. Run the application locally through Docker and read or publish its bilingual manual on GitHub. See [documentation maintenance and publishing](publishing.md).

[Previous: Maintenance](maintenance.md) · [Next: Documentation maintenance and publishing](publishing.md)
