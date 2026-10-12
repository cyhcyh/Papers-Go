# Interests and account settings

[Contents](index.md) · [简体中文](../zh/interests.md)

Open **设置** after login. The current tabs are **基本设置**, **我的兴趣**, **通知管理**, and **我的技能**.

## Basic settings

Your username is read-only here; administrators manage username changes. Choose light, dark, or system appearance.

To change your password, enter the old password, a new password, and the new password again. New passwords must contain 8–128 characters and both entries must match. Sign in again after a successful change.

## My interests

The interest profile is a form; Markdown editing is unnecessary.

| Field | Purpose | Example |
| --- | --- | --- |
| 研究方向描述 | Background, experience, and problems for the assistant | Studying structure and coloring in sparse graphs |
| 长期兴趣 | Persistent recommendation and trend directions | Extremal graph theory; graph coloring |
| 近期关注 | Temporary priorities, with an optional end date | New list-coloring bounds this month |
| 关注程度 | General, sustained, or priority attention | Priority attention |
| Exclusions | Directions you want to avoid | Applications outside your current research |
| Categories and topics | Paper sources and approved topic scope | `math.CO` and selected approved topics |

The research description primarily provides context for the assistant. To use a direction for recommendations and trends, also add it as a long-term or temporary interest, or select a topic.

### Save your interests

1. Describe your background.
2. Add long-term interests, temporary priorities, and exclusions.
3. Select categories and topics. Selecting a parent selects the currently displayed topics beneath it; adjust individual topics as needed.
4. Save the form.
5. If the page reports that recommendations are updating in the background, wait for completion before returning to the feed.

Saving the form and generating interest vectors are separate stages. If the model service is unavailable, the update may wait or fail. Check its status instead of repeatedly submitting the same content.

The profile retains the current version and at most **three previous versions**. Version numbers continue increasing. Feedback and weekly profile reflection also contribute to interest updates.

## Topic proposals

In the proposal section of **我的兴趣**, search the research-direction catalog, select an entry and its source associations, add a reason, and submit.

Ordinary users submit proposals for administrator approval. The current implementation uses the site's research-direction catalog; catalog entries and approved topics are distinct. See [categories, sources, and topics](sources-topics.md).

## Watch list

Add keyword, benchmark, author, or topic watches. Use specific values rather than overly broad terms.

Watches produce notifications through background work such as alert evaluation. They do not continuously monitor every external website. Removing a watch prevents future alerts based on that item.

## Notifications

The page groups settings by notification method. Control in-app and Telegram notifications and enter your Telegram Chat ID. The administrator must also configure the server's Telegram bot credential; a Chat ID alone is insufficient.

Use **通知中心** to read notifications and manage their read state. New notifications depend on your switches, matching papers, and background task execution.

## My skills

Use **我的技能** for personal skills and shared proposals. See [research assistant and Skills](assistant.md) for usage and approval.

[Previous: User guide](user-guide.md) · [Next: Paper reading](reading.md)
