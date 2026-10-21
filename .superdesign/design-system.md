# RED PANDA · Web visual redesign

## Product truth

RED PANDA is a personal space for a small team of persistent Bots. People talk to named Bots privately or in group chats, assign goals, give permission, and redirect work. Coding projects are optional resources. A Bot is a colleague, not a disposable run. The implemented product supports a mixed roster of Bots and group chats, browser-local navigation folders, group membership, inline mentions, direct conversations, individual authorization, model settings, and optional code projects.

Private conversations are the primary interaction surface. Group chats primarily support reading complete reports, confirmations and handoffs from several Bots, often after the person has been away.

## User direction

The existing visual system may be replaced. Design and preview locally using the current agent; do not invoke Superdesign's cloud, login, upload source, or consume its generation credits. Superdesign's discovery and design-before-implementation workflow is retained. The previous Design Report template request was replaced by the explicit Superdesign request.

Recorded preferences, scoped to this product: ordinary group chats, folders and individual Bots drive navigation; mentions are optional. Inline `@name` uses a readable colored background with a stable color per Bot, so references remain easy to find in long messages. Never center navigation around mentions.

The sidebar starts with a labeled New Bot action. Omit the redundant brand header and personal-workspace row; give the recovered height to the conversation list. Keep group and folder creation beside the conversation heading.

The user prefers direction A and wants its dark counterpart. A supports light and dark palettes within the same layout, reading density, identity colors and interaction state. The footer theme action switches without clearing conversation drafts. Its dark palette uses neutral charcoal surfaces, softer off-white text and brighter identity colors; it does not inherit direction B's rail or compact typography.

## Interaction structure

- Keep one obvious place to write. Enter sends, Shift+Enter inserts a newline; Chinese IME composition must never send prematurely.
- Mixed navigation folders contain both groups and Bots. Search and conversation-type filters operate on the roster.
- Bot bubbles align left; human bubbles align right, with their avatar outside the right edge. Body text remains left aligned inside both. Shallow tinted surfaces have no outline or shadow. Short bubbles fit their contents; all long bubbles on both sides cap at the prose measure plus padding, with an opposite-side gutter. Never widen the entire bubble because a later section contains code or diff, or constrain paragraphs separately inside a wider surface. Each author stays visible, with extra separation when the sender changes.
- Prose stays within a comfortable reading measure; headings, paragraphs and lists have distinct rhythm. Code and diff use the available bubble width, with horizontal scrolling inside the code area only.
- Opening a group resumes at its unread divider. History reading never follows incoming messages; only a count appears. Reaching the bottom or explicitly choosing latest restores following. Each conversation retains its reading anchor, draft and pending whole-message quote while navigating.
- Quotes show the original author and a short preview. They link to the source message and provide a return to the prior reading position. Quoting never inserts a mention or creates a thread.
- Group composers start compact and expand for long input, newlines or a quote. Private conversations keep a larger composer. A private conversation entered from a group provides a return to that group's saved context.
- Member details are secondary and collapsible. Selecting a member opens that Bot's own conversation, where permissions belong.
- Project selection never blocks starting a conversation. Do not introduce an organization / project / task hierarchy above people and chats.
- Use textual status with color, and labeled icon buttons. Empty search results, empty conversations, focus states and narrow screens are designed.

## Direction A · Studio / 轻盈协作

Selected direction. A single organized sidebar, a quiet chat surface, and member details closed until requested. Private Bot conversations come first in the roster. Chrome is pale cool gray; terracotta is restricted to deliberate actions. Stable identity colors distinguish Atlas (blue), Nova (violet), Echo (green), and Mori (amber). Authors, quotes and paragraphs establish rhythm; shallow message backgrounds keep boundaries readable.

- Background #f4f5f7; surface #ffffff; sidebar #f7f8fa; text #242a35; secondary #667080.
- Accent #c24d32; light accent #fae9e3; primary buttons dark #27313f.
- Chinese body 14–15px with 1.85 line-height; UI 13px; metadata 12px; titles 18px.
- Sidebar 258px, optional details 264px; conversation bounded at 1040px, prose at 72ch, code and diff scroll within the bubble. Composer bounded at 820px.
- Corners: 6px controls, 10px panels and composer. The composer uses the approved rectangular 66×30px “发送 ↑” button; group and private conversations share its visual structure. No gradients, dashboard stat cards, oversized hero headings, or decorative taglines.

## Direction B · Focus / 深色专注

A different navigation composition: a narrow brand rail, a dedicated conversation list, and a dense blue-black conversation surface. Member details open as an overlay instead of permanently consuming reading width. Useful for a small screen or several active conversations. The same identity colors are adjusted for dark-background contrast.

- Background #13181f; surface #1b222c; sidebar #171d25; text #e1e6ee; secondary #9aa8ba.
- Accent #ef9b79; selected conversation #29323f; fine divider #303b49.
- Body 14px with 1.8 line-height; compact author/message spacing.
- Rail 64px + list 246px. Single primary reading pane; details drawer 292px.

## Assets and implementation

Use the actual repository brand image `assets/brand/red-panda-mark-pixel.png`; never replace the mark with an invented logo. Typography uses locally installed Segoe UI / Microsoft YaHei UI / PingFang SC; no external font request. Outline interface icons are inline SVG. The previews are dependency-free HTML/CSS/JavaScript and make no external requests. Example conversations are labeled as sample data and do not claim real Bot work.

The existing React / Mantine / Redux application remains the production integration point after the user selects a direction. These previews are design artifacts, not a second production app or a new backend architecture.
