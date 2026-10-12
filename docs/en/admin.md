# Administration

[Contents](index.md) · [简体中文](../zh/admin.md)

## Sign in

Administrators use the site's private administration entry. It is randomly generated and displayed after the first registration, then available under **基本设置**. The public navigation has no administration link.

Open the entry and sign in with an administrator account. Knowing the address alone does not grant access; the entry does not replace authentication.

## Navigation

| Menu | Purpose |
| --- | --- |
| 回到首页 | Return to the public application |
| 概览 | Site size, processing coverage, behavior, and model usage |
| 基本设置 | Branding, entry address, cache, logs, recommendation settings |
| 任务中心 | Execution, stopping, switches, schedules, and reprocessing |
| 分类与来源 | arXiv categories, conferences, and ingestion settings |
| 主题管理 | Approve, add, edit, disable, or delete topics |
| 用户管理 | Accounts, roles, states, and batch actions |
| 模型配置 | Connections, assignments, Prompts, and Skills |
| 日志 | Interface, model, source, and task diagnostics |

Use the left menu on desktop and the expandable menu on mobile. Model configuration uses horizontal tabs within its own page.

## Overview

![Administration overview](../images/admin-overview.jpg)

Charts and totals cover papers, topics, users, processing, and cloud token usage. Input and output usage depends on recorded calls and provider-reported data. Missing provider values are not an accurate billing record. Check the provider for actual charges and limits.

For operational issues, start with processing coverage and task state, then open the task or logs.

## Basic settings

1. Set the site name and description, and upload the site logo and favicon.
2. Set the administration entry: `/` followed by 6–80 letters, digits, underscores, or hyphens. Reserved application routes cannot be used.
3. Adjust full-text cache age and size for the available disk space.
4. Set log retention days and maximum entries.
5. Adjust personal and guest recommendation weights where provided. Each group must total 100%.

Each icon upload is limited to 2 MB. Check the new entry and update your bookmark after saving.

The default 30-day log retention controls log records. It does not expire API keys every 30 days.

## Users

Find and select users to change usernames, reset passwords, grant or revoke administrator roles, and enable or disable accounts. Sensitive changes such as passwords and roles invalidate previous login state.

Use multi-selection for batch management. Verify the selected accounts and the effect before destructive operations.

| Action | Effect |
| --- | --- |
| Disable | Block account use while keeping personal data |
| Reset password | Replace the password; the previous password no longer works |
| Grant administrator | Give site-management access; supply the site entry to that person |
| Delete | Remove the user's conversations, profiles, interactions, watches, notifications, personal skills, and related records |

The application prevents deleting, disabling, or demoting the currently signed-in administrator and requires at least one active administrator. Deleting a user does not delete the site's shared paper collection.

## Logs

Use the category, level, and search controls on the log page:

- For ingestion errors, inspect source and task events.
- For generation failures, inspect the feature, model, elapsed time, and error.
- For administration failures, inspect the operation path and status.
- For worker readiness problems, also inspect Docker worker logs.

API keys, Authorization values, and similar fields are redacted. Logs are not a database backup, and retention removes older records.

[Previous: Trends and hotspots](trends.md) · [Next: Categories, sources, and topics](sources-topics.md)
