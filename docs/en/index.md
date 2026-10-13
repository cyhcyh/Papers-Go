# Papers-Go User Manual

[简体中文](../zh/index.md) · [Documentation home](../index.md)

Papers-Go brings paper ingestion, personal recommendations, assisted reading, and research trends into a self-hosted web application. Use it from a desktop or mobile browser, with local models, cloud services, or a mixture of both.

This manual describes repository version **1.1.1** and follows the workflows of installers, researchers, and administrators.

## Start here

To deploy your own site, follow [quick start](quick-start.md): copy the Docker Compose configuration, register the administrator, configure the installation, run the pipeline, and set personal interests.

To use an existing installation, start with the [user guide](user-guide.md) and [interests and account settings](interests.md).

## Full contents

### 1. Getting started and deployment

- [Quick start](quick-start.md): start the site, configure it, run the pipeline, and use personal recommendations.
- [Deployment](deployment.md): Docker, Ollama and GPU setup, access addresses, and persistent directories.
- [Configuration](configuration.md): environment variables, data locations, and applying configuration changes.

### 2. Administration and setup

- [Administration](admin.md): the administration entry, overview, site settings, users, and logs.
- [Model configuration](models-prompts.md): cloud APIs, Ollama, Codex, feature assignments, and Prompts.
- [Categories, sources, and topics](sources-topics.md): add arXiv categories and conferences, approve topics, edit records, and use bulk actions.
- [Task center](pipeline.md): run the full pipeline, configure schedules, enable or disable stages, stop tasks, and reprocess results.

### 3. Using the site

- [User guide](user-guide.md): the paper feed, category browsing, paper card actions, and personal library.
- [Interests and account settings](interests.md): account and appearance settings, interests, topic proposals, watchlists, and notifications.
- [Paper reading](reading.md): generate six-question reading cards, check queue status, and use shared cached results.
- [Research assistant and Skills](assistant.md): paper conversations, session management, context and memory, and skill use and administration.
- [Trends and hotspots](trends.md): research hotspots, developments in your interests, and trend generation status.

### 4. Maintenance and troubleshooting

- [Maintenance](maintenance.md): routine checks, updates, backup and restoration, and data cleanup.
- [Frequently asked questions](faq.md): solutions for deployment, model connections, pipelines, recommendations, and reading.

### 5. Development and documentation

- [Local development](development.md): run the source with Conda and start the backend, frontend, and worker.
- [Documentation maintenance and publishing](publishing.md): edit both languages and publish a documentation site with VitePress and GitHub Pages.

## Roles

| Role | Main capabilities |
| --- | --- |
| Guest | Browse the configured public feed, categories, and paper information; personal features request a login |
| Registered user | Set interests, like and save papers, generate reading cards, chat, and propose topics or shared skills |
| Administrator | Manage site settings, models, sources, topics, users, background work, logs, and usage |

The first registered account becomes an administrator. Its random administration entry is shown after initial registration; administrators can subsequently view or change it in the console.

## Main features

- **Paper feed:** desktop browsing and mobile swiping, with paper briefs, authors, source labels, and recommendation scores.
- **Reading cards:** six-question full-text analysis with mathematics, tables, and evidence excerpts; background queueing and a shared result cache.
- **Recommendations:** source and topic selections, long-term and temporary interests, and reading feedback influence ranking.
- **Trends:** research developments and hotspots organized around the latest arXiv announcement and the user's interests.
- **Research assistant:** paper search, reading requests, interest-aware conversation, and personal or shared skills.
- **Administration:** background tasks, sources, topics, accounts, models, instructions, and operational logs.

![Desktop paper feed](../images/feed-desktop.jpg)

## Conventions

“Admin → 模型配置 → 功能分配” means to open those controls in order. Commands specify their working directory. `localhost:8000` is the default Docker address; substitute your configured port where necessary.

Chinese and English chapters share the same file names. Screenshots show the current Chinese application interface, and this manual includes Chinese menu labels for reference. Documentation languages do not change the application's interface language.

[Next: Quick start](quick-start.md)
