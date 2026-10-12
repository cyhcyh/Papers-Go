# Quick start

[Contents](index.md) · [简体中文](../zh/quick-start.md)

Follow this chapter in order: start the site, register the administrator, connect models, add sources, process papers, approve topics, set personal interests, and check recommendations, chat, and reading.

## Prerequisites

| Component | Requirement |
| --- | --- |
| Docker | Linux containers; the published image supports `linux/amd64` |
| Docker Compose | Version 2.30 or newer |
| Model service | Reachable Ollama, compatible cloud API, or Codex account authorization, explicitly assigned in the console |
| Network | Access to the selected paper sources, image registry, and model services |

This deployment does not require host installations of Python, Node.js, or a database server.

## Step 1: Start the site

Create a deployment directory:

```bash
mkdir papers-go
cd papers-go
```

Create `docker-compose.yml` there and copy the complete configuration:

```yaml
name: shualunwen
services:
  app:
    image: roger1001/papers-go:latest
    ports:
      - "8000:8000"
    env_file:
      - path: .env
        required: false
    environment:
      DATA_DIR: /data
      FRONTEND_DIR: /app/frontend/dist
      MODEL_KEY_FILE: /secrets/model-keys.key
      OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:-http://ollama:11434}
      TZ: ${TZ:-Asia/Shanghai}
      PIPELINE_MODE: external
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./data:/data
      - ./.secrets:/secrets
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
  worker:
    image: roger1001/papers-go:latest
    command: ["python", "-m", "app.worker"]
    env_file:
      - path: .env
        required: false
    environment:
      DATA_DIR: /data
      FRONTEND_DIR: /app/frontend/dist
      MODEL_KEY_FILE: /secrets/model-keys.key
      OLLAMA_BASE_URL: ${OLLAMA_BASE_URL:-http://ollama:11434}
      TZ: ${TZ:-Asia/Shanghai}
      PIPELINE_MODE: external
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - ./data:/data
      - ./.secrets:/secrets
    depends_on:
      app:
        condition: service_healthy
    restart: unless-stopped
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
```

Run from that directory:

```bash
docker compose pull
docker compose up -d
docker compose ps
```

Open **http://localhost:8000**. `app` serves the interface and API. `worker` performs ingestion, analysis, and queued work. Check that both services are running before configuring the installation.

This configuration connects to a cloud API, Codex, or your existing Ollama service without downloading source code. An `.env` file is optional and can be omitted initially. See [deployment](deployment.md) to include Ollama or GPU access.

## Step 2: Register the administrator and set up the site

1. Select **登录 / 注册** and create the first account with a password of at least eight characters.
2. The first account automatically becomes an administrator. There is no default administrator password.
3. Save the random administration entry displayed after registration.
4. Visit that entry and review the site name, icons, and entry address under **基本设置**.

Initially, set the site name and description, save, and keep cache and ranking defaults. Locate **模型配置**, **分类与来源**, and **任务中心** in the left menu.

> The entry reminder appears after the first registration. Update your bookmark whenever you change the entry. Other users granted the administrator role use the same site-wide entry.

## Step 3: Connect models and assign features

### Add a connection

Open **模型配置 → 模型连接 → 添加连接** and use an available service:

| Service | Type | Enter |
| --- | --- | --- |
| Cloud API | 云端 | Name, provider compatibility Base URL, API key |
| Existing Ollama on the Docker host | 本地 Ollama | Name, `http://host.docker.internal:11434` |
| Ollama in Compose | 本地 Ollama | Name, `http://ollama:11434` |
| Codex through a ChatGPT account | Codex | Connection name; sign in with a device code after saving |

For example, name a connection “My cloud models.” Use the provider's API endpoint, not a model description or chat website. Prepare Ollama models separately; the console does not download them.

For cloud APIs and Ollama, save, test the connection, and update its available-model list. For Codex, authorize the connection as described below before assigning features.

### Sign in to Codex with a device code

1. Select **Codex** when adding a connection and enter a name, such as “My Codex.”
2. Keep the default **Codex 服务（设备码流程）** service address, automatic stable-version detection, and default OAuth Client ID. Save the connection.
3. Select **登录 Codex** on its card. A device code and **授权页面** link appear.
4. Open the authorization page, sign in to your ChatGPT account, enter the device code, and complete authorization.
5. Return to model configuration. The page automatically updates authorization status and loads available models. Open **功能分配** to select this connection and the model for each feature.

No API key or Codex CLI installation is required. Available models and limits depend on the authorized account. If the device code expires, select **登录 Codex** again to obtain a new one.

The Codex connection provides text generation. Assign a separate cloud or Ollama embedding model to **向量生成**.

### Assign features

Open **功能分配**, configure each needed feature, select a connection and model, and save:

| Feature | Model type | Purpose |
| --- | --- | --- |
| 向量生成 | Embedding model | Retrieval, interest recommendations, and classification dependencies |
| 主题分类 | Structured-output text model | Choose research topics |
| 质量与新颖性评估 | Text model | Assess reference quality |
| 中文标题与论文速读 | Text model | Chinese titles and two-part briefs |
| 智能助手 | Tool-capable conversation model | Research chat |
| 论文精读 | Long-context generation model | Six-question reading cards |
| 方向趋势简述 | Text model | Personal research summaries |

One text model can serve multiple features. Embeddings require an embedding model. If your cloud connection only offers chat models, add another embedding provider or a local embedding model.

Keep thinking, reasoning effort, concurrency, and other advanced options at automatic/default values initially. Configure optional deep analysis, reflection, and other features later.

See [model configuration](models-prompts.md) for detailed settings.

## Step 4: Add paper categories and sources

Open **分类与来源**, use the addition control, select the arXiv categories or conferences you want, and save. Allow automatic ingestion and select the guest-default option for sources that guests should see.

For example, use `math.CO` for combinatorics, `cs.LG` for machine learning, or select ICML, AAAI, NeurIPS, or ICLR. These are examples, not automatically enabled sources.

## Step 5: Ingest and process papers

1. Open **任务中心** and select **运行完整流水线**.
2. The system runs enabled stages in order, automatically ingesting papers, assigning topics, generating vectors, assessing quality, and creating briefs. You do not need to start each stage separately.
3. Check status and processed counts on the task cards. If configuration is missing, assign the indicated feature under model configuration. For failures, inspect the task error and logs.
4. Open **分类浏览** and select an added category to view the ingested and processed papers.

Completed results are saved paper by paper; you can browse before the entire collection finishes. Paper briefs and full-text reading cards are separate results.

Before starting, the pipeline checks model requirements for every enabled stage. Disabled stages are skipped. Disable optional stages such as **Top 5 预读** if you do not need them. You can also schedule the pipeline to run automatically; see [task center](pipeline.md).

## Step 6: Approve or add topics

Open **主题管理** and review pending proposals. Check the name, description, source association, and proposal origin before approving. Edit or reject unsuitable proposals.

To create your own topic, use the addition control and enter its name, discipline, description, and source association. Ordinary users submit proposals; administrators publish them.

Catalog entries are candidates, not already approved topics. Each paper gets one best-matching topic. You do not need to create every topic before ingestion.

Rerun classification after approval if papers still need matching.

## Step 7: Set personal interests

Use **回到首页**, then open **设置 → 我的兴趣**. Administrators and ordinary registered users both set their own preferences here.

1. Describe your background, such as graph coloring and extremal graph theory.
2. Add a long-term interest, such as graph coloring, and choose its priority.
3. Add temporary interests and end dates if needed.
4. Select `math.CO` or your configured source and the approved topics you want.
5. Save, wait for the background recommendation update, and return to the feed.

Selecting a category selects its topics; deselect individual topics if needed. Semantic recommendations need the background vector update, not just a successful form save.

## Step 8: Check recommendations, chat, and reading

| Feature | Check |
| --- | --- |
| Feed and recommendations | Browse, try a like or negative feedback, and inspect **猜你想看** |
| Library | Save a paper and find it under **我的书架 → 收藏** |
| Assistant | Select **问 AI** and ask what problem the paper addresses |
| Reading | Select **展开 → 生成精读卡**, observe queue or generation status, and return for the six-question result |
| Trends | Run **方向趋势简述**, then check home and full summaries; matching new submissions are needed |

After the basic workflow works, add sources, schedule tasks, adjust Prompts, enable prereading, or configure notifications. Initial collection processing is different from ordinary daily use.

## Quick troubleshooting

| Symptom | First check |
| --- | --- |
| Site does not open | `docker compose ps`, port conflicts, Docker status |
| Site opens but no papers | Sources added, ingestion allowed, ingestion task succeeded |
| Classification needs configuration | Both classification and embeddings assigned |
| No Chinese brief | **论文速读** completed |
| No topics in the tree | Proposals approved and papers classified |
| Interests appear unchanged | Background update complete and matching papers exist |
| Worker not ready | `docker compose logs --tail=100 worker` |

If the site opens but no papers appear, check source configuration, ingestion permission, and task results first. See [frequently asked questions](faq.md).

[Previous: Introduction](index.md) · [Next: Deployment](deployment.md)
