# Documentation maintenance and publishing

[Contents](index.md) · [简体中文](../zh/publishing.md)

## Read on GitHub

Documentation lives in the project's `docs/` directory. Open `docs/index.md` on GitHub and choose a language. No server or local tools are required.

| File | Purpose |
| --- | --- |
| Root `README.md` | Existing Chinese project introduction |
| Root `README.en.md` | Corresponding English introduction |
| `docs/index.md` | Bilingual entry |
| `docs/zh/` | Complete Chinese manual |
| `docs/en/` | Complete English manual |
| `docs/images/` | Shared screenshots |

Each chapter links to the matching language and contents. First-time installers should start with `quick-start.md`, not read every reference chapter first.

## Edit the manual

Open a Markdown file on GitHub, select Edit, change the text, and commit. Update both language indexes and cross-language links when adding chapters. Keep matching file names.

Use repository-relative screenshot paths. Keep keys, private administration entries, real user records, and local `.env` content out of public documentation. Chinese screenshots can be shared between languages with control names explained in English.

## Publish with VitePress

The Markdown files can become a VitePress site with sidebars, page outlines, search, and a language menu. GitHub Pages provides static hosting without your own server. See the [VitePress deployment guide](https://vitepress.dev/guide/deploy#github-pages).

The manual currently consists of Markdown pages readable on GitHub. A separate site also requires VitePress configuration and a Pages workflow. The following steps explain that setup.

### 1. Install documentation tooling separately

Keep document dependencies in `docs/`, independent of the application frontend:

```bash
cd docs
npm init -y
npm install --save-dev vitepress@1.6.4
```

Add documentation scripts for `vitepress dev .`, `vitepress build .`, and `vitepress preview .`. A documentation build does not require FastAPI, a database, or Ollama.

### 2. Configure navigation and languages

Create the documentation configuration under `docs/.vitepress/`:

- Use Papers-Go as the title and the existing `docs/index.md` as the entry.
- Use `/zh/` for Chinese and `/en/` for English.
- Add top links for quick start, user documentation, administration, and GitHub.
- Group the sidebar into getting started, user guide, administration, and maintenance/help.
- Enable local full-text search without an external search account.
- Show second- and third-level headings in the page outline.

Use VitePress `locales` for languages and their navigation. The source files already have corresponding language directories. See [internationalization](https://vitepress.dev/guide/i18n).

For `https://cyhcyh.github.io/Papers-Go/`, set `base` to `/Papers-Go/`. Adjust this for a renamed repository, account-homepage repository, or custom domain rather than retaining the wrong path.

### 3. Preview locally

Run document development or build/preview commands in `docs/` and check:

- The eight initial setup steps form a continuous workflow.
- Language and chapter links work.
- Compose examples, tables, images, and code blocks render.
- Mobile navigation, page outlines, and search work.

Ignore generated dependencies, caches, and outputs. Do not commit document `node_modules/` or `.vitepress/dist/`.

### 4. Enable GitHub Pages

1. Commit the documentation and future tooling configuration to the target GitHub repository.
2. Open Settings → Pages.
3. Under Build and deployment, select GitHub Actions.
4. Configure a document-only workflow to install dependencies, build, upload `.vitepress/dist/`, and deploy Pages.
5. Use the GitHub repository's actual default branch, not a local development branch copied from another setup.
6. After success, use the URL displayed by Pages.

Public repositories can use Pages on GitHub Free. Check plan eligibility for private repositories. See [GitHub Pages custom workflows](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages).

### 5. Maintain the site

Once automatic publishing is configured, edit Markdown and push to the workflow's trigger branch. Actions rebuilds the site. This publishes documentation; it does not deploy the Papers-Go application to Pages.

## Share the right entry

Before the separate site is published, share the GitHub link to `docs/en/quick-start.md` or `docs/zh/quick-start.md`. After publishing, share the corresponding quick-start page. Relative documentation links in the English README remain useful on GitHub.

[Previous: Frequently asked questions](faq.md) · [Return to contents](index.md)
