# Trends and hotspots

[Contents](index.md) · [简体中文](../zh/trends.md)

## Open the page

The home **每日趋势** module gives a short overview. Select **查看趋势分析**, or open **趋势雷达**, for the full page. Login and meaningful interest directions are required.

The current analysis centers on new submissions in the **latest arXiv announcement**, interpreted against your interests and relevant research context. It is more than a restatement of collection counts. An arXiv announcement is not guaranteed for every calendar day.

![Trends and hotspots](../images/trends.jpg)

## Page sections

| Section | Content |
| --- | --- |
| 当日热点 | Hotspots among confirmed topics; at least three new papers in a topic are needed |
| 方向发展概述 | Problems, approaches, results, and significance by interest direction |
| 每日概览 | An overall account of the announcement's research focus |
| 收藏作者的新动向 | New work from followed authors within the last seven days |

The home page uses short summaries; the trends page provides fuller analysis. Direction overviews emphasize developments without placing a paper-link list beneath the narrative.

## Get relevant analysis

1. Add long-term interests, active temporary interests, or approved topics under **设置 → 我的兴趣**.
2. Save and wait for the interest update.
3. Open the trends page and check its arXiv announcement date.
4. If generation is pending, allow the **方向趋势简述** background task to finish.

Make broad directions more specific where useful. For example, describe “combinatorics” as “list constraints and extremal structure in graph coloring” to distinguish relevant problems and results.

## Interpret the status

- **Incomplete topic statistics:** some papers still need classification or topic approval. Hotspots cover the confirmed subset.
- **No hotspots:** papers may exist without meeting the display threshold.
- **Waiting / updating:** announcement synchronization, classification, or report generation is in progress.
- **No matching development:** the current material is insufficient for this direction; this does not establish that the entire field made no progress.

Announcement dates, public paper dates, ingestion dates, and report-generation times are different. Follow the date basis displayed on the page.

## Administrator adjustments

Open Admin → **模型配置 → Prompts**. `trend_report` controls the trend instructions and `trend_rewrite` controls expression refinement. State the desired scientific problems, methodological changes, evidence, and length while preserving the required output structure.

Assign the model under **功能分配 → 方向趋势简述**. The task with that name prepares announcements and generates reports. **趋势聚合** computes topic statistics and is a separate task.

[Previous: Research assistant and Skills](assistant.md) · [Next: Administration](admin.md)
