# 文档维护与发布

[目录](index.md) · [English](../en/publishing.md)

## 直接在 GitHub 阅读

文档位于同一仓库的 `docs/`，上传后打开 `docs/index.md` 即可选择语言，不需要服务器或安装任何工具。

| 文件 | 用途 |
| --- | --- |
| 根目录 `README.md` | 原中文项目说明 |
| 根目录 `README.en.md` | 对应的英文项目说明 |
| `docs/index.md` | 双语文档入口 |
| `docs/zh/` | 中文完整手册 |
| `docs/en/` | 英文完整手册 |
| `docs/images/` | 两种语言共用的界面截图 |

每章顶部有对应语言链接和目录入口。首次安装从 `quick-start.md` 开始，不必从头读完所有章节。

## 修改文档

在 GitHub 打开对应 Markdown 文件，点击编辑按钮修改文字，再提交。新增章节时同步更新两种语言的目录与相互跳转链接；保留相同文件名便于对应。

截图使用仓库中的相对路径。不要把 API Key、管理入口、真实用户数据或本机 `.env` 截入公开文档。界面截图可以沿用中文，英文说明中标明对应按钮。

## 制作 VitePress 文档站

Markdown 内容可以作为 VitePress 文档源，制作成带侧栏、页内目录、搜索和语言菜单的网站。GitHub Pages 提供静态托管，无需自建服务器。[VitePress 部署说明](https://vitepress.dev/guide/deploy#github-pages)

当前文档以 Markdown 形式提供，可直接在 GitHub 阅读。要启用独立文档站，还需配置 VitePress 和 Pages 工作流。以下步骤说明如何完成这些设置。

### 1. 独立安装文档工具

在 `docs/` 中建立独立的文档依赖，避免修改前端应用的依赖：

```bash
cd docs
npm init -y
npm install --save-dev vitepress@1.6.4
```

为文档 package 配置开发、构建和预览命令，分别执行 `vitepress dev .`、`vitepress build .`、`vitepress preview .`。文档构建只读取文档内容，不需要启动 FastAPI、数据库或 Ollama。

### 2. 配置目录与语言

在 `docs/.vitepress/` 创建文档配置，按以下内容组织：

- 标题为 Papers-Go，首页使用现有 `docs/index.md`。
- 中文语言路径为 `/zh/`，英文为 `/en/`。
- 顶部导航提供快速开始、用户手册、后台手册和 GitHub。
- 侧栏按“入门”“用户手册”“后台手册”“维护与帮助”分组。
- 开启本地全文搜索，不必配置外部搜索账号。
- 右侧目录显示当前文章的二、三级标题。

使用 VitePress 内置 `locales` 设置语言及各自导航。页面文件已经按对应目录准备，详见[官方多语言说明](https://vitepress.dev/guide/i18n)。

如果站点部署到 `https://cyhcyh.github.io/Papers-Go/`，`base` 应为 `/Papers-Go/`。仓库更名、使用账号主页仓库或自定义域名时，要按实际网址调整，不能沿用错误路径。

### 3. 本地预览

在 `docs/` 执行文档开发或构建 / 预览命令，检查：

- 首次使用八个步骤能连续阅读。
- 中英文切换及章节跳转正确。
- Compose、表格、截图和代码块正常显示。
- 手机端导航能展开，页内目录和搜索可用。

文档构建生成的缓存、依赖和产物应忽略，不提交 `node_modules/` 或 `.vitepress/dist/`。

### 4. 开启 GitHub Pages

1. 将文档及后续文档工具配置提交到目标 GitHub 仓库。
2. 打开仓库 Settings → Pages。
3. 在 Build and deployment 中选择 GitHub Actions。
4. 配置一个仅构建 `docs/` 的发布工作流：安装文档依赖、构建、上传 `.vitepress/dist/`、部署 Pages。
5. 工作流分支应使用该 GitHub 仓库实际默认分支，不要直接照搬本机开发分支名。
6. 运行成功后，以 Pages 页面显示的地址为准。

公开仓库可使用 GitHub Free 的 Pages；私有仓库需要核对账号方案。流程参考 [GitHub Pages 自定义工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

### 5. 以后怎样更新

自动发布配置完成后，只需修改 Markdown 并提交到触发分支，GitHub Actions 会重新生成文档站。发布文档不需要把 Papers-Go 应用本身部署到 GitHub Pages。

## 普通用户应从哪里阅读

在独立站点发布前，给用户提供 GitHub 中的 `docs/zh/quick-start.md` 或 `docs/en/quick-start.md` 链接。站点发布后再分享对应的快速开始页面；英文 README 中的相对文档链接仍可继续使用。

[上一页：常见问题](faq.md) · [返回目录](index.md)
