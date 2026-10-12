# User guide

[Contents](index.md) · [简体中文](../zh/user-guide.md)

## Paper feed

Open **刷论文** on the home page. Guests can browse the administrator's public source selection. Personal interests and saved feedback require login.

On desktop, the page frame stays in place while the center feed scrolls independently. On mobile, swipe vertically between papers. The feed loads in batches, starting with 20 papers and fetching more as you continue.

![Desktop feed](../images/feed-desktop.jpg)

### Reading a card

| Element | Meaning |
| --- | --- |
| Main title | Original paper title |
| Translated title | Chinese title produced by the brief task |
| Authors | Up to two lines, followed by “等” when names do not fit |
| Source label | arXiv code or conference name, such as `math.CO` or ICML |
| Topic | Approved research topic, with long names confined to a fixed region |
| Recommendation score | Composite ranking score, not an acceptance probability or formal academic rating |
| 研究问题 | The research question |
| 主要贡献和结果 | A short account of the approach, contribution, and results |

Translations, briefs, or topics may be missing before processing is complete. Supported text areas render LaTeX mathematical notation.

### Card actions

- **原文:** open the paper's source page.
- **展开:** open details, the abstract, and reading-card generation.
- **问 AI:** open the assistant with paper context.
- **喜欢:** record positive interest and add the paper to the liked section of your library.
- **收藏:** save the paper to your library.
- **没兴趣:** record negative feedback to reduce related recommendations.

Likes and saves can be removed. A home-feed card needs approximately **five continuous seconds** of visible dwell time to count as viewed. Loading a card or swiping past it immediately does not count. This is a browsing metric, not proof that the full text was read.

## Browse by category

Open **分类浏览**:

1. Select an arXiv category or conference in the tree and expand its approved topics.
2. Search by title, abstract, or author.
3. Select a date basis and time range.
4. Sort by recommendation score or date.
5. Use the previous/next controls below the results. Category browsing uses 30-paper pages.

The category tree scrolls independently. On mobile, open the category drawer, select a filter, and return to the list. Category pagination is separate from the continuous home feed.

![Category browsing](../images/browse-desktop.jpg)

“最近收录” refers to when a paper entered this installation. Paper dates refer to available public paper dates. A newly imported old paper is not newly published. Broaden the time range or topic filter if no results appear.

## Your library

After login, open **我的书架**. Switch between **喜欢** (liked), **收藏** (saved), and **浏览记录** (browsing history). Search by title, abstract, or author.

Removing a save affects your record, not the shared paper. Papers may become unavailable after source deletion or an expiry cleanup. A save is not a permanent archive.

## Home sidebar

| Module | Purpose |
| --- | --- |
| 每一次发现，都算数 | Today's reading, likes, and saves |
| 猜你想看 | Up to five current recommendations |
| 每日趋势 | A short research overview linking to the full trends page |

Guests see a login prompt for trends. The sidebar adapts or collapses on narrow and mobile layouts.

Continue with [interest settings](interests.md) to make recommendations more relevant.

[Previous: Configuration](configuration.md) · [Next: Interests and account settings](interests.md)
