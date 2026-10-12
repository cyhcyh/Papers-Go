# 快速开始

[目录](index.md) · [English](../en/quick-start.md)

本章带您完成一次完整安装：启动网站 → 注册管理员 → 配置模型 → 添加分类 → 抓取与处理论文 → 审批主题 → 设置个人兴趣 → 使用推荐、对话和精读。

只想先使用起来，请按顺序完成本章。后续章节用于说明各项设置和日常维护。

## 准备工作

| 项目 | 要求 |
| --- | --- |
| Docker | Linux 容器模式，发布镜像支持 `linux/amd64` |
| Docker Compose | 2.30 或更新版本 |
| 模型服务 | 可访问的 Ollama、兼容云端 API 或 Codex 账户授权；使用前在后台分配模型 |
| 网络 | 能访问所选论文来源、镜像仓库和模型服务 |

这一方式不需要在宿主机安装 Python、Node.js 或数据库服务。

## 第一步：启动网站

新建部署目录：

```bash
mkdir papers-go
cd papers-go
```

在这个目录中新建 `docker-compose.yml`，完整复制以下内容：

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

在同一目录执行：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

浏览器打开 **http://localhost:8000**。`app` 提供网页和 API，`worker` 执行抓取、分析、队列等后台工作。两个服务均正常运行后再进行配置。

此配置可以连接云端 API、Codex 或您已有的 Ollama，无需下载源码。`.env` 文件可选，首次可不创建。需要一起部署 Ollama 或启用 GPU 时，见[环境部署](deployment.md)。

## 第二步：注册管理员并设置网站

1. 点击“登录 / 注册”，注册第一个账号，密码至少 8 位。
2. 第一个账号自动成为管理员，无需输入默认管理员密码。
3. 注册成功后保存提示的随机管理入口。以后访问该地址进入后台。
4. 在“基本设置”中确认站点名称、图标和管理入口。

第一次使用只需填写站点名称、描述并保存，其余缓存和推荐参数先保留默认值。模型配置和分类都在后台左侧菜单中。

> 管理入口只在首次注册成功时主动提醒。更改入口后请更新浏览器书签。其他被授予管理员角色的用户也通过同一站点管理入口访问。

## 第三步：连接模型并分配功能

### 添加连接

打开“模型配置 → 模型连接 → 添加连接”。先选一种已有可用服务：

| 您准备使用什么 | 连接类型 | 要填写什么 |
| --- | --- | --- |
| 云端 API | 云端 | 连接名称、服务商的兼容 Base URL、API Key |
| 本机已安装的 Ollama | 本地 Ollama | 名称、`http://host.docker.internal:11434` |
| Compose 中的 Ollama | 本地 Ollama | 名称、`http://ollama:11434` |
| ChatGPT 账户的 Codex 服务 | Codex | 连接名称；保存后通过设备码登录 |

例如，名称可以填“我的云端模型”；Base URL 填服务商提供的兼容接口地址，不填模型介绍页或聊天网页。Ollama 需先准备好模型，后台不会替您下载。

云端 API 和 Ollama 连接保存后，使用连接卡片提供的测试和更新可用模型操作。Codex 连接先按下面的步骤授权，再配置功能。

### 使用 Codex 设备码登录

1. 添加连接时选择“Codex”，填写连接名称，例如“我的 Codex”。
2. 服务地址使用默认的“Codex 服务（设备码流程）”，兼容版本保持“自动检测官方稳定版本”，OAuth Client ID 保留默认值，保存连接。
3. 在连接卡片上点击“登录 Codex”，页面会显示设备码和“授权页面”链接。
4. 打开授权页面，登录自己的 ChatGPT 账户，输入设备码并完成授权。
5. 返回模型配置页，页面会自动更新授权状态并加载可用模型。随后到“功能分配”选择这条连接和需要的模型。

这种方式无需填写 API Key，也无需安装 Codex CLI。可用模型和额度由所授权账户的权限决定。设备码过期时，重新点击“登录 Codex”获取新码。

Codex 连接用于文本生成；“向量生成”仍需单独配置云端或 Ollama 的 embedding 模型。

### 分配模型

打开“功能分配”，逐项打开配置，选择连接、选择或填写模型名并保存：

| 功能 | 应选择的模型 | 本章用处 |
| --- | --- | --- |
| 向量生成 | embedding 向量模型 | 论文检索和兴趣推荐；主题分类也依赖它 |
| 主题分类 | 支持结构化输出的文本模型 | 给论文选择研究主题 |
| 质量与新颖性评估 | 文本模型 | 生成质量参考信息 |
| 中文标题与论文速读 | 文本模型 | 翻译标题，生成两项速读 |
| 智能助手 | 支持工具调用的对话模型 | 前台研究对话 |
| 论文精读 | 能处理长文本的生成模型 | 生成六问精读卡 |
| 方向趋势简述 | 文本模型 | 生成个人趋势概述 |

同一文本模型可以分配给多项功能，向量生成要单独选 embedding 模型。云端连接若只有聊天模型，需要再添加提供 embedding 的连接，或使用本地向量模型。

思考模式、推理强度、并发和其他高级参数第一次先保留自动或默认值。暂时不使用的深度解析、画像反思等功能可以以后再配置。

详细设置见[模型配置](models-prompts.md)。

## 第四步：添加论文分类与来源

打开“分类与来源”，点击新增，选择需要的 arXiv 分类或会议并保存。勾选自动抓取；希望游客看到的来源同时设为游客默认。

例如，关注组合数学可添加 `math.CO`，关注机器学习可添加 `cs.LG`，会议可选择 ICML、AAAI、NeurIPS 或 ICLR。这里只是配置示例，系统不会替您启用这些来源。

## 第五步：抓取并处理论文

1. 打开“任务中心”，点击“运行完整流水线”。
2. 系统按顺序执行已启用的步骤，自动完成论文抓取、主题分类、向量生成、质量评估和论文速读，无需逐个启动。
3. 在任务卡片中查看运行状态和已处理数量。提示“待配置”时，回模型配置补齐对应功能；出现失败时查看任务错误和日志。
4. 打开“分类浏览”，选择已添加的分类，即可查看收录和处理后的论文。

完成的论文会逐篇保存，不必等所有论文处理完才浏览。精读全文按需生成，论文速读完成不代表已生成精读卡。

流水线启动前会检查所有启用步骤的模型配置，禁用的步骤会跳过。暂时不需要的“Top 5 预读”等步骤可先禁用。后续可设置计划，让流水线定时运行，详见[任务中心](pipeline.md)。

## 第六步：批准或添加主题

打开“主题管理”，切换到待审核提议。查看主题名称、研究方向说明、所属分类及提议来源，合适的点击批准。不合适的可以编辑或拒绝。

如果希望自己创建主题，使用新增入口，填写名称、学科、说明和来源关联，再保存。普通用户只能提交提议，正式发布由管理员批准。

主题目录是候选方向，批准后才成为正式主题。每篇论文只选一个最相关主题，不需要一开始手工建立全部主题。

若论文尚未匹配到主题，可以在批准后补跑分类任务。

## 第七步：在前台设置个人兴趣

点击后台“回到首页”，打开“设置 → 我的兴趣”。管理员同样可以设置自己的个人兴趣；其他注册用户也在这里设置。

1. 研究方向描述：写自己的背景，例如“我研究图着色与极值图论”。
2. 长期兴趣：添加具体方向，例如“图着色”，选择关注程度。
3. 近期关注：按需填写当前问题和结束日期。
4. 关注的分类与主题：选择 `math.CO` 或自己配置的来源，进一步选择感兴趣的正式主题。
5. 保存，等待“推荐正在后台更新”状态完成，再返回首页。

勾选主分类会同时勾选其下主题；不想全部关注时再取消具体项。修改兴趣后的语义推荐需要兴趣向量更新完成，保存按钮成功不等于后台模型已经处理完。

## 第八步：验证推荐、助手和精读

| 功能 | 如何验证 |
| --- | --- |
| 信息流与推荐 | 刷几篇论文，尝试喜欢、没兴趣；查看“猜你想看” |
| 个人书架 | 收藏一篇论文，进入“我的书架 → 收藏”确认 |
| 智能助手 | 点击论文“问 AI”，询问“这篇论文研究什么问题？” |
| 精读 | 打开“展开 → 生成精读卡”，确认出现排队或生成状态；稍后回来查看六问结果 |
| 趋势 | 运行“方向趋势简述”后查看首页简版和趋势页；本期需有与兴趣匹配的新稿 |

基础流程跑通后，再扩展来源、设置任务计划、调整 Prompts、启用预读或配置通知。不要用第一次抓取全库的等待时间判断所有日常操作速度。

## 快速排错

| 现象 | 先检查 |
| --- | --- |
| 页面打不开 | `docker compose ps`，端口是否占用，Docker 是否运行 |
| 网站有了但没论文 | 来源是否添加、自动抓取是否开启、抓取任务是否成功 |
| 分类任务说缺少模型 | 主题分类和向量生成是否都已分配 |
| 卡片没有中文速读 | “论文速读”任务是否完成 |
| 主题树没有主题 | 提议是否已批准，相关论文是否已分类 |
| 兴趣保存后没有变化 | 后台兴趣更新是否完成，所选来源中是否有论文 |
| 任务提示 worker 未就绪 | `docker compose logs --tail=100 worker` |

若网站可打开但无论文，先检查来源是否配置、是否允许抓取及抓取任务是否成功，再查看[常见问题](faq.md)。

[上一页：介绍](index.md) · [下一页：环境部署](deployment.md)
