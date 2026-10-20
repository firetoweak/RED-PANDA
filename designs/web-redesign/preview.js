/* Local design preview. All data and actions stay in this page. */
"use strict";
const direction = document.documentElement.dataset.direction;
const iconPaths = {
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z"/><path d="M8 11h8m-8 4h5"/>',
  group: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/><circle cx="9" cy="7" r="4"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  down: '<path d="m7 10 5 5 5-5"/>',
  right: '<path d="m9 5 7 7-7 7"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  info: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M15 4v16m-8-11h4m-4 4h4"/>',
  at: '<circle cx="12" cy="12" r="4"/><path d="M16 8v5a3 3 0 0 0 6 0v-1a10 10 0 1 0-4 8"/>',
  send: '<path d="M12 19V5m-5 5 5-5 5 5"/>',
  reply: '<path d="m9 10-5 5 5 5M4 15h9a6 6 0 0 0 6-6V4"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 13h8m-8 4h6"/>',
  folder: '<path d="M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"/>',
  settings: '<path d="M4 7h16M4 17h16M8 4v6m8 4v6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  bulb: '<path d="M9 18h6m-6 3h6M9 15a6 6 0 1 1 6 0v3H9v-3Z"/>',
  shield: '<path d="M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7L12 3Z"/><path d="m8 12 3 3 5-6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  layout: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M9 3v18M9 9h12"/>',
  dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name]}</svg>`;
const esc = (value) => String(value).replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const escapeRegex = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const bots = [
  { id: "atlas", name: "Atlas", role: "架构与后端", status: "空闲", color: "atlas", initial: "A" },
  { id: "nova", name: "Nova", role: "界面与交互", status: "工作中", color: "nova", initial: "N" },
  { id: "echo", name: "Echo", role: "测试与质量", status: "等待授权", color: "echo", initial: "E" },
  { id: "mori", name: "Mori", role: "资料研究", status: "空闲", color: "mori", initial: "M" },
];
const folders = [
  { id: "development", name: "产品研发", collapsed: false },
  { id: "ideas", name: "想法与探索", collapsed: false },
];
const conversations = [
  { id: "dev", kind: "group", name: "研发讨论", subtitle: "Nova：先把阅读体验做好", folder: "development", time: "10:42", unread: true, members: ["atlas","nova","echo"] },
  { id: "atlas", kind: "bot", name: "Atlas", subtitle: "架构与后端", folder: "development", time: "10:38" },
  { id: "nova", kind: "bot", name: "Nova", subtitle: "界面与交互", folder: "development", time: "10:41" },
  { id: "echo", kind: "bot", name: "Echo", subtitle: "有一项操作需要你授权", folder: "development", time: "10:42", unread: true },
  { id: "ideas", kind: "group", name: "灵感收集站", subtitle: "让想法先发生", folder: "ideas", members: ["nova","mori"], time: "昨天" },
  { id: "mori", kind: "bot", name: "Mori", subtitle: "资料研究", folder: "ideas", time: "昨天" },
];
const messages = {
  dev: [
    { id: "g1", sender: "human", time: "10:32", body: '<p>这次先把 Web 端的协作体验做好。不要像一个管理后台，我希望它就是一个可以随时找大家聊事情的地方。</p>' },
    { id: "g2", sender: "atlas", time: "10:35", body: '<p>赞同。入口应该围绕<strong>人和对话</strong>组织：找一个 Bot 私聊，或者把几位 Bot 拉进群里一起讨论。代码项目只在需要的时候出现。</p><p>@Nova 视觉上可以把群聊放在最自然的位置；每位 Bot 的名字和状态要让人一眼分清。</p>' },
    { id: "g3", sender: "nova", time: "10:39", body: '<p>我想把它做成一个安静、好用的协作空间。左边找对话，中间专注读写，右边按需查看成员，不再用一堆卡片解释系统。</p><p>长消息用清楚的段落节奏，<strong>点名直接留在正文里</strong>，每个名字有稳定的身份色。比如 @Atlas 和 @Echo，扫一眼就能找到。</p>', file: true },
    { id: "g4", sender: "echo", time: "10:42", quote: { name: "Nova", text: "点名直接留在正文里，每个名字有稳定的身份色。" }, body: '<p>我来检查阅读和输入体验。重点看长消息、中文输入法，以及窄窗口下的布局。@Nova 成员资料收起来之后，聊天区应该自然变宽。</p><p>需要执行的验证命令，我会在私聊里请你授权。</p>' },
  ],
  atlas: [
    { id: "a1", sender: "atlas", time: "10:28", body: '<p>我在。我们可以先聊清楚目标，再决定要不要关联代码项目。</p><p>当前我主要负责架构和后端，也可以和 @Nova 一起推敲交互边界。需要协作时，我们会把讨论放回群聊里。</p>' },
    { id: "a2", sender: "human", time: "10:30", body: '<p>前端这次可以重做视觉，但群聊和每个 Bot 的身份要保留。</p>' },
    { id: "a3", sender: "atlas", time: "10:31", body: '<p>明白。视觉可以换，协作关系保持清楚。群聊负责共享讨论，私聊负责单独沟通和处理我的授权请求。</p><p>项目是我执行编码任务时使用的资源，<strong>不会变成聊天的前置条件</strong>。</p>' },
  ],
  nova: [
    { id: "n1", sender: "nova", time: "10:36", body: '<p>新版想解决的第一件事，是让你愿意一直待在这个聊天窗口里。</p><p>我会把界面分成三个节奏：轻一点的导航、清楚一点的消息、克制一点的辅助信息。群聊和单个 Bot 用同一套查找方式，不需要先挑项目。</p>' },
    { id: "n2", sender: "human", time: "10:37", body: '<p>对，@ 不是百分百用，不要围绕它设计。但是正文里的点名要醒目。</p>' },
    { id: "n3", sender: "nova", time: "10:41", body: '<p>记住了。@Atlas 是蓝色，@Echo 是绿色，我用紫色；头像和点名用同一套身份色。</p><p>颜色帮助定位信息，正文仍然保持舒适的对比度。你也可以先看看这份布局草案。</p>', file: true },
  ],
  echo: [
    { id: "e1", sender: "echo", time: "10:40", body: '<p>我准备检查前端的现有测试，确认重新设计时不会影响基本交互。</p><p>下面这项操作需要你授权。你可以只允许这一次，或者拒绝并告诉我换一种验证方式。</p>', permission: true },
  ],
  ideas: [
    { id: "i1", sender: "human", time: "昨天 17:20", body: '<p>这里先放还不成熟的想法，不急着变成功能。</p>' },
    { id: "i2", sender: "mori", time: "昨天 17:24", body: '<p>好。资料、参考和问题都可以先留在这里。等到想法清楚了，再请 @Nova 一起讨论它应该长什么样。</p>' },
  ],
  mori: [{ id: "m1", sender: "mori", time: "昨天 17:18", body: '<p>你好，我是 Mori，主要帮你整理资料和研究问题。</p><p>把想了解的主题或手头的材料发给我就好，不需要先关联代码项目。</p>' }],
};
let activeId = "dev";
let filter = "all";
let rosterQuery = "";
let detailsOpened = direction === "studio" && window.innerWidth > 1180;
let replyTo = null;
let mentionIndex = 0;
let mentionDismissed = false;
let composing = false;
let serial = 0;
let toastTimer;
let searchOpened = false;
let chatQuery = "";
const drafts = new Map();
const getConversation = () => conversations.find((item) => item.id === activeId);
const findBot = (id) => bots.find((bot) => bot.id === id);
const getMembers = () => getConversation().kind === "group" ? getConversation().members.map(findBot) : bots;
const statusClass = (status) => status === "工作中" ? "" : status === "等待授权" ? "attention" : "idle";
const avatar = (id, extra = "") => {
  if (id === "human") return `<span class="entry-avatar identity-human ${extra}" aria-hidden="true">你</span>`;
  const bot = findBot(id);
  return `<span class="entry-avatar identity-${bot?.color || "atlas"} ${extra}" aria-hidden="true">${esc(bot?.initial || "B")}</span>`;
};
const iconButton = (action, name, symbol, extra = "") => `<button type="button" class="icon-button ${extra}" data-action="${action}" aria-label="${esc(name)}" title="${esc(name)}">${icon(symbol)}</button>`;
function renderMentions(html) {
  const names = [...bots].sort((a,b) => b.name.length - a.name.length).map((bot) => escapeRegex(esc(bot.name))).join("|");
  return html.replace(new RegExp(`@(${names})(?![\\p{L}\\p{N}_-])`, "giu"), (full, name) => {
    const bot = bots.find((item) => esc(item.name).toLocaleLowerCase() === name.toLocaleLowerCase());
    return bot ? `<button type="button" class="mention identity-${bot.color}" data-open="${esc(bot.id)}" aria-label="打开 ${esc(bot.name)} 的私聊">@${esc(bot.name)}</button>` : full;
  });
}
document.getElementById("app").innerHTML = `
  <div class="review-bar" aria-label="视觉初稿预览">
    <span class="review-label">${icon("layout")}视觉初稿</span>
    <nav class="direction-links" aria-label="设计方向">
      <a href="./index.html" class="${direction === "studio" ? "active" : ""}" ${direction === "studio" ? 'aria-current="page"' : ""}>A 轻盈协作</a>
      <a href="./focus.html" class="${direction === "focus" ? "active" : ""}" ${direction === "focus" ? 'aria-current="page"' : ""}>B 深色专注</a>
    </nav>
    <span class="review-note">示例内容 · 操作仅在本地演示</span>
  </div>
  <div class="workspace" id="workspace">
    <nav class="app-rail" aria-label="快捷入口">
      <img src="../../assets/brand/red-panda-mark-pixel.png" alt="RED PANDA">
      ${iconButton("all-chats", "所有对话", "chat", "rail-active")}
      ${iconButton("new-bot", "新建 Bot", "plus")}
      ${iconButton("new-group", "新建群聊", "group")}
      <div class="rail-bottom">${iconButton("settings", "模型配置", "settings")}</div>
    </nav>
    <button type="button" class="sidebar-backdrop" data-action="close-sidebar" aria-label="关闭侧栏"></button>
    <aside class="sidebar" aria-label="Bot 与群聊名册">
      <div class="brand"><img src="../../assets/brand/red-panda-mark-pixel.png" alt=""><div><div class="brand-name">RED PANDA</div><div class="brand-caption">长期 Bot 协作</div></div></div>
      <div class="space-switch"><span class="space-symbol">我</span><span>${direction === "focus" ? "RED PANDA" : "我的协作空间"}</span></div>
      <label class="search-box">${icon("search")}<input id="roster-search" type="search" placeholder="搜索 Bot 或群聊" aria-label="搜索 Bot 或群聊" autocomplete="off"><kbd>Ctrl K</kbd></label>
      <div class="sidebar-topline"><h2>对话</h2><div style="display:flex;gap:2px">${iconButton("new-folder", "新建分组", "folder")}${iconButton("new-group", "新建群聊", "group")}${iconButton("new-bot", "新建 Bot", "plus")}</div></div>
      <div class="filter-bar" role="tablist" aria-label="会话类型"><button class="filter" role="tab" data-filter="all" aria-selected="true">全部</button><button class="filter" role="tab" data-filter="group" aria-selected="false">群聊</button><button class="filter" role="tab" data-filter="bot" aria-selected="false">Bot</button></div>
      <nav class="roster" id="roster" aria-label="会话列表"></nav>
      <footer class="sidebar-footer"><button class="settings-button" data-action="settings">${icon("settings")}模型配置</button><div class="connection"><span class="status-dot"></span>本地预览<span class="local-label">个人空间</span></div></footer>
    </aside>
    <main class="conversation-shell" aria-label="聊天">
      <header class="chat-header" id="chat-header"></header>
      <div id="chat-search" class="chat-search" hidden>${icon("search")}<input type="search" id="message-search" placeholder="搜索这段对话" aria-label="搜索当前对话"><span id="search-count"></span>${iconButton("close-search", "关闭消息搜索", "close")}</div>
      <div class="transcript" id="transcript" role="region" aria-label="聊天消息" tabindex="0"><div class="transcript-inner" id="messages"></div></div>
      <section class="composer-dock" aria-label="撰写消息">
        <div class="typing-status" id="typing-status"></div>
        <div class="composer-wrap"><div class="mention-popup" id="mention-popup" hidden></div>
          <form class="composer" id="composer-form"><div class="reply-preview" id="reply-preview" hidden></div>
            <textarea id="message-input" rows="2" aria-label="消息正文" placeholder="发送到研发讨论，输入 @ 点名成员…" autocomplete="off"></textarea>
            <div class="composer-toolbar">${iconButton("mention", "点名成员", "at")}<span class="recipient" id="recipient">未点名 · 通知全体成员</span><button class="send-button" type="submit" id="send-button" disabled>发送${icon("send")}</button></div>
          </form>
        </div>
        <div class="composer-footnote"><span><kbd>Enter</kbd> 发送 · <kbd>Shift + Enter</kbd> 换行</span><span class="lock-note">${icon("shield")}每位 Bot 的授权在私聊中处理</span></div>
      </section>
    </main>
    <aside class="details" id="details" aria-label="会话资料"></aside>
  </div>
  <dialog id="modal" aria-labelledby="dialog-title"></dialog>
  <div class="toast" id="toast" role="status" hidden></div>`;

function renderRoster() {
  const visible = conversations.filter((item) => (filter === "all" || item.kind === filter) && `${item.name} ${item.subtitle}`.toLocaleLowerCase().includes(rosterQuery.toLocaleLowerCase()));
  document.getElementById("roster").innerHTML = visible.length === 0 ? '<div class="empty-roster">没有找到匹配的 Bot 或群聊。<br>试试名字或负责方向。</div>' : folders.map((folder) => {
    const items = visible.filter((item) => item.folder === folder.id);
    if (!items.length && (rosterQuery || filter !== "all")) return "";
    const collapsed = folder.collapsed && !rosterQuery;
    return `<section class="folder" aria-label="${esc(folder.name)}分组"><div class="folder-head"><button class="folder-toggle" data-folder="${folder.id}" aria-expanded="${!collapsed}">${icon(collapsed ? "right" : "down")}${esc(folder.name)}<span class="count">${items.length}</span></button></div><div ${collapsed ? "hidden" : ""}>${items.length ? items.map((item) => {
      const bot = item.kind === "bot" ? findBot(item.id) : null;
      const figure = bot ? `<span class="entry-avatar identity-${bot.color}" aria-hidden="true">${esc(bot.initial)}<i class="status-dot ${statusClass(bot.status)}"></i></span>` : `<span class="entry-avatar group-avatar" aria-hidden="true">${icon("group")}</span>`;
      return `<div class="entry-wrap"><button class="conversation-entry ${item.id === activeId ? "active" : ""}" data-open="${esc(item.id)}" ${item.id === activeId ? 'aria-current="page"' : ""} aria-label="${esc(item.kind === "group" ? "群聊 " : "私聊 ")}${esc(item.name)}">${figure}<span class="entry-copy"><span class="entry-name">${esc(item.name)}</span><span class="entry-sub">${esc(item.subtitle)}</span></span><span class="entry-side"><span class="entry-time">${esc(item.time || "")}</span>${item.unread ? '<i class="entry-dot" aria-label="有新动态"></i>' : ""}</span></button><button class="entry-menu icon-button" data-move="${esc(item.id)}" aria-label="移动 ${esc(item.name)} 到分组" title="移动到分组">${icon("dots")}</button></div>`;
    }).join("") : '<div class="empty-roster">从对话右侧菜单移入 Bot 或群聊。</div>'}</div></section>`;
  }).join("");
  document.querySelectorAll("[data-filter]").forEach((button) => { const selected = button.dataset.filter === filter; button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1; });
}
function renderHeader() {
  const current = getConversation();
  const isGroup = current.kind === "group";
  const bot = isGroup ? null : findBot(activeId);
  document.getElementById("chat-header").innerHTML = `${iconButton("sidebar", "打开会话名册", "menu", "mobile-toggle")}<span class="entry-avatar header-avatar ${isGroup ? "" : `identity-${bot.color}`}" aria-hidden="true">${isGroup ? icon("group") : esc(bot.initial)}</span><div class="header-copy"><h1>${esc(current.name)}</h1><p class="header-subtitle">${isGroup ? `${current.members.length} 位 Bot · 群聊` : `<span class="status-dot ${statusClass(bot.status)}"></span>${esc(bot.status)} · ${esc(bot.role)}`}</p></div><div class="header-actions">${isGroup ? `<div class="avatar-stack" aria-hidden="true">${current.members.map((id) => avatar(id)).join("")}</div>` : ""}${iconButton("search-chat", "搜索当前对话", "search")}<span class="divider"></span><button type="button" class="icon-button" data-action="details" aria-label="${detailsOpened ? "收起" : "查看"}${isGroup ? "群资料" : "Bot 资料"}" title="${detailsOpened ? "收起资料" : "查看资料"}" aria-expanded="${detailsOpened}">${icon("info")}</button></div>`;
}
function renderDetails() {
  const current = getConversation();
  const isGroup = current.kind === "group";
  const members = isGroup ? current.members.map(findBot) : [findBot(activeId)];
  document.getElementById("details").hidden = !detailsOpened;
  document.getElementById("workspace").classList.toggle("details-closed", !detailsOpened);
  document.getElementById("details").innerHTML = `<div class="details-heading"><h2>${isGroup ? "群资料" : "Bot 资料"}</h2>${iconButton("details", "收起资料", "close")}</div><div class="details-profile">${isGroup ? `<span class="entry-avatar" aria-hidden="true">${icon("group")}</span>` : avatar(activeId)}<h3>${esc(current.name)}</h3><p>${isGroup ? "把想法放在一起，把事情向前推进。" : esc(members[0].role)}</p></div><div class="detail-section"><div class="section-label"><span>${isGroup ? "群成员" : "当前状态"}</span><span>${isGroup ? members.length + " 位 Bot" : ""}</span></div>${members.map((bot) => `<button class="member" data-open="${esc(bot.id)}" aria-label="查看 ${esc(bot.name)} 的私聊">${avatar(bot.id)}<span class="entry-copy"><span class="entry-name">${esc(bot.name)}</span><span class="entry-sub">${esc(bot.role)}</span></span><span class="member-status"><i class="status-dot ${statusClass(bot.status)}"></i>${esc(bot.status)}</span></button>`).join("")}</div><div class="detail-rule"></div><div class="detail-section"><div class="section-label">${isGroup ? "关于这个群" : "协作信息"}</div>${isGroup ? `<div class="detail-info"><span>消息</span><span>${messages[activeId].length} 条</span></div><div class="detail-info"><span>成员构成</span><span>${members.length} 位 Bot 和你</span></div>` : `<div class="detail-info"><span>身份</span><span>长期 Bot</span></div><div class="detail-info"><span>代码项目</span><span>未关联</span></div><div class="detail-info"><span>参与群聊</span><span>${conversations.filter((item) => item.kind === "group" && item.members.includes(activeId)).length} 个</span></div>`}</div><div class="hint-box"><h3>${icon("bulb")}${isGroup ? "像平常一样聊天" : "直接开始交流"}</h3><p>${isGroup ? "发消息和大家一起讨论。需要某位成员回应时，在正文里 @ 对方就好。" : "讨论想法、处理任务，或让 Bot 找同事一起协作。无需先创建代码项目。"}</p></div>`;
}
function messageText(message) { return message.body.replace(/<[^>]*>/g, ""); }
function renderMessages() {
  const current = getConversation();
  const visible = messages[activeId].filter((message) => `${messageText(message)} ${findBot(message.sender)?.name || "你"}`.toLocaleLowerCase().includes(chatQuery.toLocaleLowerCase()));
  document.getElementById("search-count").textContent = chatQuery ? `${visible.length} 条消息` : "";
  document.getElementById("messages").innerHTML = `<div class="day-divider">${activeId === "ideas" || activeId === "mori" ? "昨天 · 10月20日" : "今天 · 10月21日"}</div>` + (visible.length === 0 ? `<div class="empty-state">${icon("chat")}<h2>${chatQuery ? "没有找到这条消息" : `和${esc(current.name)}开始交流`}</h2><p>${chatQuery ? "换个词试试，或关闭搜索查看全部消息。" : "先说说你的想法，或者一起讨论接下来要做的事。"}</p></div>` : visible.map((message) => {
    const human = message.sender === "human";
    const bot = human ? null : findBot(message.sender);
    const name = human ? "你" : bot.name;
    return `<article class="message ${human ? "human" : ""}" id="message-${message.id}" aria-label="${esc(name)}的消息">${avatar(message.sender)}<div class="message-content"><div class="message-meta"><button class="author" ${human ? 'data-action="human-profile"' : `data-open="${esc(bot.id)}"`}>${esc(name)}</button>${human ? "" : `<span class="author-role">${esc(bot.role)}</span>`}<time>${esc(message.time)}</time></div>${message.quote ? `<div class="quote"><b>${esc(message.quote.name)}</b>${esc(message.quote.text)}</div>` : ""}<div class="message-body">${renderMentions(message.body)}</div>${message.file ? `<button class="shared-file" data-action="view-file" aria-label="打开界面布局草案"><span class="file-icon">${icon("file")}</span><span class="file-copy"><strong>界面布局草案.md</strong><small>布局与交互说明 · 示例附件</small></span>${icon("right")}</button>` : ""}${message.permission ? renderPermission() : ""}</div><div class="message-actions"><button class="icon-button" data-reply="${message.id}" aria-label="回复 ${esc(name)} 的消息" title="回复">${icon("reply")}</button><button class="icon-button" data-copy="${message.id}" aria-label="复制 ${esc(name)} 的消息" title="复制">${icon("copy")}</button></div></article>`;
  }).join(""));
}
function renderPermission() {
  return `<div class="permission"><div class="permission-title">${icon("shield")}需要你授权</div><p>在前端目录执行测试，读取测试结果。</p><div class="code-block"><div class="code-label"><span>执行命令</span><span>web/</span></div><code>npm run test</code></div><div class="permission-actions"><button class="plain-button" data-action="deny">拒绝</button><button class="primary-button" data-action="approve">允许这一次</button></div></div>`;
}
function updateTyping() {
  const current = getConversation();
  document.getElementById("typing-status").innerHTML = current.id === "dev" ? `<span class="typing-dots" aria-hidden="true"><i></i><i></i><i></i></span><span>Nova 正在整理下一条消息</span>` : current.id === "echo" && findBot("echo").status === "等待授权" ? `${icon("shield")}<span>Echo 在等你处理上面的授权请求</span>` : `<span>${current.kind === "group" ? "和大家一起，把想法聊清楚" : `直接和 ${esc(current.name)} 交流`}</span>`;
}
function renderConversation() {
  renderRoster(); renderHeader(); renderDetails(); renderMessages(); updateTyping();
  const input = document.getElementById("message-input");
  input.placeholder = getConversation().kind === "group" ? `发送到${getConversation().name}，输入 @ 点名成员…` : `发送给 ${getConversation().name}…`;
  input.value = drafts.get(activeId) || "";
  replyTo = null; renderReply(); updateComposer();
}
function openConversation(id) {
  if (!conversations.some((item) => item.id === id)) return;
  if (id === activeId) { document.getElementById("workspace").classList.remove("sidebar-open"); return; }
  drafts.set(activeId, document.getElementById("message-input").value);
  activeId = id; chatQuery = "";
  document.getElementById("message-search").value = "";
  getConversation().unread = false;
  document.getElementById("workspace").classList.remove("sidebar-open");
  renderConversation();
  document.getElementById("transcript").scrollTop = 0;
}
function renderReply() {
  const element = document.getElementById("reply-preview");
  element.hidden = !replyTo;
  element.innerHTML = replyTo ? `${icon("reply")}<span>回复 ${esc(replyTo.name)}：${esc(replyTo.text)}</span>${iconButton("cancel-reply", "取消回复", "close")}` : "";
}
function mentionMatch() {
  const input = document.getElementById("message-input");
  return input.value.slice(0, input.selectionStart).match(/(^|\s)@([^\s@]*)$/u);
}
function mentionCandidates() {
  const match = mentionMatch();
  if (!match || mentionDismissed || composing) return [];
  return getMembers().filter((bot) => bot.name.toLocaleLowerCase().startsWith(match[2].toLocaleLowerCase()));
}
function updateComposer() {
  const input = document.getElementById("message-input");
  const names = getMembers().filter((bot) => new RegExp(`@${escapeRegex(bot.name)}(?=$|[\\s，。！？、,:;!?）)])`, "iu").test(input.value));
  document.getElementById("send-button").disabled = !input.value.trim();
  document.getElementById("recipient").textContent = getConversation().kind === "bot" ? `发送给 ${getConversation().name}` : names.length ? `点名 ${names.map((bot) => "@" + bot.name).join("、")}` : "未点名 · 通知全体成员";
  const candidates = mentionCandidates();
  mentionIndex = Math.min(mentionIndex, Math.max(0, candidates.length - 1));
  const popup = document.getElementById("mention-popup");
  popup.hidden = !candidates.length;
  if (candidates.length) {
    popup.innerHTML = `<div class="mention-heading">点名${getConversation().kind === "group" ? "群成员" : "同事"}</div><div role="listbox" id="mention-list" aria-label="点名候选">${candidates.map((bot,index) => `<button class="mention-option" type="button" role="option" id="mention-${bot.id}" aria-selected="${index === mentionIndex}" data-mention="${bot.id}">${avatar(bot.id)}${esc(bot.name)}<small>${esc(bot.role)}</small></button>`).join("")}</div>`;
    input.setAttribute("aria-autocomplete", "list"); input.setAttribute("aria-controls", "mention-list"); input.setAttribute("aria-activedescendant", `mention-${candidates[mentionIndex].id}`);
  } else { input.removeAttribute("aria-activedescendant"); input.removeAttribute("aria-controls"); }
}
function chooseMention(id) {
  const input = document.getElementById("message-input");
  const match = mentionMatch();
  if (!match) return;
  const bot = findBot(id);
  const start = input.selectionStart - match[2].length - 1;
  const insertion = `@${bot.name} `;
  input.setRangeText(insertion, start, input.selectionEnd, "end");
  input.focus(); mentionDismissed = true; updateComposer();
}
function sendMessage() {
  const input = document.getElementById("message-input");
  const text = input.value.trim();
  if (!text || composing) return;
  const now = new Date();
  const message = { id: `local-${++serial}`, sender: "human", time: now.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }), body: `<p>${esc(text).replace(/\n/g, "<br>")}</p>` };
  if (replyTo) message.quote = { name: replyTo.name, text: replyTo.text.slice(0,100) };
  messages[activeId].push(message);
  const current = getConversation(); current.subtitle = "你：" + text.replace(/\n/g," "); current.time = message.time;
  input.value = ""; drafts.delete(activeId); replyTo = null; mentionDismissed = false;
  renderReply(); updateComposer(); renderMessages(); renderRoster(); renderDetails();
  document.getElementById("typing-status").textContent = "消息已添加到本地示例";
  document.getElementById("transcript").scrollTop = document.getElementById("transcript").scrollHeight;
  input.focus();
}
function toast(text) {
  clearTimeout(toastTimer); const element = document.getElementById("toast");
  element.textContent = text; element.hidden = false;
  toastTimer = setTimeout(() => { element.hidden = true; }, 2600);
}
function showDialog(title, body) {
  const dialog = document.getElementById("modal");
  dialog.innerHTML = `<div class="dialog-header"><h2 id="dialog-title">${esc(title)}</h2>${iconButton("close-dialog", "关闭弹窗", "close")}</div><div class="dialog-body">${body}</div>`;
  dialog.showModal();
}
function createDialog(kind) {
  const title = { bot:"新建 Bot", group:"新建群聊", folder:"新建分组" }[kind];
  showDialog(title, `<form id="create-form" data-kind="${kind}"><p class="dialog-description">${kind === "bot" ? "给新同事一个名字，就可以开始交流。代码项目可以暂不关联。" : kind === "group" ? "让几位 Bot 在同一个地方，一起讨论和协作。" : "把相关的 Bot 和群聊放在一起，按你的习惯整理。"}</p><label class="field"><span>${kind === "bot" ? "Bot 名字" : kind === "group" ? "群聊名称" : "分组名称"}</span><input name="name" aria-label="${title}名称" placeholder="${kind === "bot" ? "例如：Luna" : kind === "group" ? "例如：产品讨论" : "例如：资料研究"}" required maxlength="40" autofocus autocomplete="off"></label>${kind === "group" ? `<label class="field"><span>选择成员 <small style="display:inline;font-weight:400">2–6 位 Bot</small></span></label>${bots.map((bot) => `<label class="member-check"><input type="checkbox" name="members" value="${esc(bot.id)}">${avatar(bot.id)}${esc(bot.name)}<small>${esc(bot.role)}</small></label>`).join("")}` : ""}${kind === "bot" ? '<label class="field"><span>代码项目 <small style="display:inline;font-weight:400">可选</small></span><select aria-label="代码项目"><option>暂不关联</option></select><small>Bot 有自己的工作目录，不需要先准备代码仓库。</small></label>' : ""}<p class="form-error" id="form-error" role="alert"></p><div class="dialog-actions"><button class="plain-button" type="button" data-action="close-dialog">取消</button><button class="primary-button" type="submit">${kind === "bot" ? "创建 Bot" : kind === "group" ? "创建群聊" : "创建分组"}</button></div></form>`);
}
function createItem(form) {
  const data = new FormData(form); const kind = form.dataset.kind;
  const name = String(data.get("name")).trim();
  const error = document.getElementById("form-error");
  if (!name) { error.textContent = "请填写名称。"; return; }
  if ((kind === "folder" ? folders : conversations).some((item) => item.name === name)) { error.textContent = "这个名称已经存在，换一个试试。"; return; }
  const members = data.getAll("members");
  if (kind === "group" && (members.length < 2 || members.length > 6)) { error.textContent = "请选择 2–6 位 Bot。"; return; }
  const id = `created-${++serial}`;
  if (kind === "folder") {
    folders.push({id,name,collapsed:false});
    filter = "all"; rosterQuery = ""; document.getElementById("roster-search").value = "";
    renderRoster();
  } else {
    if (kind === "bot") bots.push({id,name,role:"尚未设置负责方向",status:"空闲",color:"mori",initial:Array.from(name)[0]});
    const item = {id,kind,name,subtitle:kind === "group" ? "一起开始新的讨论" : "打开对话，开始协作",folder:"ideas",time:"刚刚"};
    if (kind === "group") item.members = members;
    conversations.push(item); messages[id] = []; filter = "all"; rosterQuery = "";
    document.getElementById("roster-search").value = ""; openConversation(id);
  }
  document.getElementById("modal").close(); toast(`${name} 已加入本地预览`);
}
const actions = {
  "new-bot": () => createDialog("bot"), "new-group": () => createDialog("group"), "new-folder": () => createDialog("folder"),
  "details": () => { detailsOpened = !detailsOpened; renderDetails(); renderHeader(); },
  "sidebar": () => document.getElementById("workspace").classList.toggle("sidebar-open"),
  "close-sidebar": () => document.getElementById("workspace").classList.remove("sidebar-open"),
  "all-chats": () => { filter = "all"; rosterQuery = ""; document.getElementById("roster-search").value = ""; renderRoster(); },
  "cancel-reply": () => { replyTo = null; renderReply(); },
  "close-dialog": () => document.getElementById("modal").close(),
  "human-profile": () => toast("你是这个协作空间的主人"),
  "mention": () => { const input = document.getElementById("message-input"); const before = input.value.slice(0,input.selectionStart); input.setRangeText(`${before && !/\s$/.test(before) ? " " : ""}@`,input.selectionStart,input.selectionEnd,"end"); mentionDismissed = false; input.focus(); updateComposer(); },
  "search-chat": () => { searchOpened = !searchOpened; document.getElementById("chat-search").hidden = !searchOpened; if (searchOpened) document.getElementById("message-search").focus(); else { chatQuery = ""; document.getElementById("message-search").value = ""; renderMessages(); } },
  "close-search": () => { searchOpened = false; chatQuery = ""; document.getElementById("chat-search").hidden = true; document.getElementById("message-search").value = ""; renderMessages(); },
  "view-file": () => showDialog("界面布局草案.md", '<div class="document-body"><p>示例附件 · 用于体验阅读与交付入口</p><h3>把聊天放在正中央</h3><p>左侧按照个人分组组织群聊和 Bot，聊天正文使用统一的阅读起点。身份色在头像和正文点名之间保持一致。</p><h3>信息按需出现</h3><p>成员资料可以收起；授权操作留在对应 Bot 的私聊。项目是可选资源，不占据主导航。</p><h3>长消息也要容易读</h3><p>适当的段落间距、清楚的作者和引用关系，比给每段文字套卡片更有效。窄屏时优先保证正文与输入区的空间。</p></div>'),
  "settings": () => showDialog("模型配置", '<p class="dialog-description">这是视觉示意。实际连接设置将在正式前端接入后沿用。</p><label class="field"><span>默认模型</span><select aria-label="默认模型"><option>尚未配置</option></select><small>配置模型后，Bot 才能执行真实任务。此预览不会读取凭据或发起模型请求。</small></label><div class="dialog-actions"><button class="primary-button" data-action="close-dialog">知道了</button></div>'),
  "approve": () => handlePermission(true), "deny": () => handlePermission(false),
};
function handlePermission(approved) {
  const message = messages.echo.find((item) => item.permission);
  if (!message) return;
  message.permission = false;
  const text = approved ? "你已允许这一次操作。此处仅演示授权状态，不执行命令。" : "你已拒绝这次操作。可以继续和 Echo 讨论其他验证方式。";
  messages.echo.push({id:`permission-${++serial}`,sender:"human",time:"刚刚",body:`<p>${text}</p>`});
  findBot("echo").status = "空闲";
  conversations.find((item) => item.id === "echo").subtitle = approved ? "本次操作已允许" : "本次操作已拒绝";
  renderConversation(); toast(approved ? "已演示单次授权，未执行命令" : "已拒绝示例操作");
}
document.addEventListener("click", async (event) => {
  const actionTarget = event.target.closest("[data-action]");
  if (actionTarget) { actions[actionTarget.dataset.action]?.(); return; }
  const openTarget = event.target.closest("[data-open]");
  if (openTarget) { openConversation(openTarget.dataset.open); return; }
  const moveTarget = event.target.closest("[data-move]");
  if (moveTarget) {
    const item = conversations.find((entry) => entry.id === moveTarget.dataset.move);
    showDialog(`整理 ${item.name}`, `<form id="move-form" data-entry="${esc(item.id)}"><p class="dialog-description">分组只整理你的对话列表，不改变 Bot 身份或群成员。</p><label class="field"><span>移动到分组</span><select name="folder" aria-label="目标分组">${folders.map((folder) => `<option value="${esc(folder.id)}" ${folder.id === item.folder ? "selected" : ""}>${esc(folder.name)}</option>`).join("")}</select></label><div class="dialog-actions"><button class="plain-button" type="button" data-action="close-dialog">取消</button><button class="primary-button" type="submit">移动</button></div></form>`);
    return;
  }
  const folderTarget = event.target.closest("[data-folder]");
  if (folderTarget) { const folder = folders.find((item) => item.id === folderTarget.dataset.folder); folder.collapsed = !folder.collapsed; renderRoster(); return; }
  const filterTarget = event.target.closest("[data-filter]");
  if (filterTarget) { filter = filterTarget.dataset.filter; renderRoster(); return; }
  const mentionTarget = event.target.closest("[data-mention]");
  if (mentionTarget) { chooseMention(mentionTarget.dataset.mention); return; }
  const replyTarget = event.target.closest("[data-reply]");
  if (replyTarget) { const message = messages[activeId].find((item) => item.id === replyTarget.dataset.reply); replyTo = { name: findBot(message.sender)?.name || "你", text: messageText(message) }; renderReply(); document.getElementById("message-input").focus(); return; }
  const copyTarget = event.target.closest("[data-copy]");
  if (copyTarget) { const message = messages[activeId].find((item) => item.id === copyTarget.dataset.copy); try { await navigator.clipboard.writeText(messageText(message)); toast("消息已复制"); } catch { toast("浏览器未允许复制，请直接选中文字复制"); } }
});
document.addEventListener("mousedown", (event) => { if (event.target.closest("[data-mention]")) event.preventDefault(); });
document.addEventListener("submit", (event) => {
  if (event.target.id === "composer-form") { event.preventDefault(); sendMessage(); }
  else if (event.target.id === "create-form") { event.preventDefault(); createItem(event.target); }
  else if (event.target.id === "move-form") {
    event.preventDefault(); const item = conversations.find((entry) => entry.id === event.target.dataset.entry);
    item.folder = new FormData(event.target).get("folder"); folders.find((folder) => folder.id === item.folder).collapsed = false;
    renderRoster(); document.getElementById("modal").close(); toast("已移动到所选分组");
  }
});
document.getElementById("roster-search").addEventListener("input", (event) => { rosterQuery = event.target.value.trim(); renderRoster(); });
document.getElementById("message-search").addEventListener("input", (event) => { chatQuery = event.target.value.trim(); renderMessages(); });
const textarea = document.getElementById("message-input");
textarea.addEventListener("input", () => { mentionDismissed = false; mentionIndex = 0; updateComposer(); });
textarea.addEventListener("click", () => { mentionDismissed = false; updateComposer(); });
textarea.addEventListener("compositionstart", () => { composing = true; updateComposer(); });
textarea.addEventListener("compositionend", () => { composing = false; updateComposer(); });
textarea.addEventListener("keydown", (event) => {
  if (event.isComposing || composing || event.keyCode === 229) return;
  const candidates = mentionCandidates();
  if (candidates.length && ["ArrowDown","ArrowUp","Enter","Tab","Escape"].includes(event.key) && !event.shiftKey) {
    event.preventDefault();
    if (event.key === "Escape") mentionDismissed = true;
    else if (event.key === "ArrowDown") mentionIndex = (mentionIndex + 1) % candidates.length;
    else if (event.key === "ArrowUp") mentionIndex = (mentionIndex - 1 + candidates.length) % candidates.length;
    else { chooseMention(candidates[mentionIndex].id); return; }
    updateComposer();
  } else if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); }
});
document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === "k") { event.preventDefault(); if (window.innerWidth <= 800) document.getElementById("workspace").classList.add("sidebar-open"); document.getElementById("roster-search").focus(); }
  if (event.key === "Escape") { document.getElementById("workspace").classList.remove("sidebar-open"); document.getElementById("mention-popup").hidden = true; }
});
document.querySelector(".filter-bar").addEventListener("keydown", (event) => {
  if (!["ArrowLeft","ArrowRight"].includes(event.key)) return;
  event.preventDefault(); const tabs = [...document.querySelectorAll("[data-filter]")];
  const index = tabs.indexOf(event.target); const next = tabs[(index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
  filter = next.dataset.filter; renderRoster(); next.focus();
});
renderConversation();
