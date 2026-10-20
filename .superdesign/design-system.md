# RED PANDA · Web visual redesign

## Product truth

RED PANDA is a personal space for a small team of persistent Bots. People talk to named Bots privately or in group chats, assign goals, give permission, and redirect work. Coding projects are optional resources. A Bot is a colleague, not a disposable run. The implemented product supports a mixed roster of Bots and group chats, browser-local navigation folders, group membership, inline mentions, direct conversations, individual authorization, model settings, and optional code projects.

## User direction

The existing visual system may be replaced. Design and preview locally using the current agent; do not invoke Superdesign's cloud, login, upload source, or consume its generation credits. Superdesign's discovery and design-before-implementation workflow is retained. The previous Design Report template request was replaced by the explicit Superdesign request.

Recorded preferences, scoped to this product: ordinary group chats, folders and individual Bots drive navigation; mentions are optional. Inline `@name` uses a readable colored background with a stable color per Bot, so references remain easy to find in long messages. Never center navigation around mentions.

## Interaction structure

- Keep one obvious place to write. Enter sends, Shift+Enter inserts a newline; Chinese IME composition must never send prematurely.
- Mixed navigation folders contain both groups and Bots. Search and conversation-type filters operate on the roster.
- Group messages use a shared left edge and named authors. Human messages are identifiable without making the rest of the discussion disappear into bubbles.
- Member details are secondary and collapsible. Selecting a member opens that Bot's own conversation, where permissions belong.
- Project selection never blocks starting a conversation. Do not introduce an organization / project / task hierarchy above people and chats.
- Use textual status with color, and labeled icon buttons. Empty search results, empty conversations, focus states and narrow screens are designed.

## Direction A · Studio / 轻盈协作

Recommended starting point. A single organized sidebar, a white editorial chat surface, and a quiet member panel. Chrome is pale cool gray; brand terracotta is restricted to deliberate actions. Stable identity colors distinguish Atlas (blue), Nova (violet), Echo (green). Authors, quotes and paragraphs establish rhythm; restrained borders keep long conversations readable.

- Background #f4f5f7; surface #ffffff; sidebar #f7f8fa; text #242a35; secondary #667080.
- Accent #c24d32; light accent #fae9e3; primary buttons dark #27313f.
- Chinese body 14–15px with 1.85 line-height; UI 13px; metadata 12px; titles 18px.
- Sidebar 258px, details 264px; conversational content bounded at 820px.
- Corners: 6px controls, 10px panels, 12px composer. No gradients, dashboard stat cards, oversized hero headings, or decorative taglines.

## Direction B · Focus / 深色专注

A different navigation composition: a narrow brand rail, a dedicated conversation list, and a dense blue-black conversation surface. Member details open as an overlay instead of permanently consuming reading width. Useful for a small screen or several active conversations. The same identity colors are adjusted for dark-background contrast.

- Background #13181f; surface #1b222c; sidebar #171d25; text #e1e6ee; secondary #9aa8ba.
- Accent #ef9b79; selected conversation #29323f; fine divider #303b49.
- Body 14px with 1.8 line-height; compact author/message spacing.
- Rail 64px + list 246px. Single primary reading pane; details drawer 292px.

## Assets and implementation

Use the actual repository brand image `assets/brand/red-panda-mark-pixel.png`; never replace the mark with an invented logo. Typography uses locally installed Segoe UI / Microsoft YaHei UI / PingFang SC; no external font request. Outline interface icons are inline SVG. The previews are dependency-free HTML/CSS/JavaScript and make no external requests. Example conversations are labeled as sample data and do not claim real Bot work.

The existing React / Mantine / Redux application remains the production integration point after the user selects a direction. These previews are design artifacts, not a second production app or a new backend architecture.
