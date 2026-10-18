# 布局与外观

明暗主题、侧栏宽度、窄屏 Agent 名册、欢迎页与右侧内容页。来源：`web/src/app/App.tsx`、`web/src/main.tsx`、`ColorSchemeSwitcher.tsx`、`sidebarWidth.ts`、`ConversationWelcome.tsx`、`web/src/features/conversation/ContentPage.tsx`、`Conversation.tsx`。

## Sub-features

- `color-scheme`：侧栏底部图标按钮 `外观` 打开菜单（标签 `外观`），只有 `深色`、`亮色` 两项，当前项带勾；默认深色（`main.tsx` 的 `defaultColorScheme="dark"`），没有「跟随系统」。
- `sidebar-resize`：侧栏右边缘分隔条（`role="separator"`，名称 `调节侧栏宽度`，带 `aria-valuemin`/`aria-valuemax`/`aria-valuenow`），只支持指针拖动，无键盘调节；宽度夹在 220–520，默认 288，松手时写入 `localStorage` 的 `redpanda.sidebarWidth`。
- `mobile-sidebar`：视口窄于 `sm` 时侧栏收进 `打开 Agent 名册`。
- `welcome`：欢迎区（图 alt `折纸小熊猫`、标题 `今天想一起完成什么？`）出现在空会话（含刚打开的草稿会话）和没有工作区时的首页。
- `content-page`：子 Agent、文件改动、错误详情在右侧内容页打开，标题分别为 `子 Agent`、`文件改动`、`<标题> · 错误详情`；关闭按钮 `关闭内容页`；分隔条 `调节内容页宽度`（指针拖动或聚焦后左右方向键，最小 320，存于 `localStorage` 的 `redpanda.contentPageWidth`）。

## How to get to it (user POV)

- 侧栏底部 `外观`；侧栏右边缘拖拽；把浏览器窗口缩窄；在会话里打开子 Agent 或文件改动。

## Driving it with the browser

- **主题**：首次打开（清空 `localStorage`）后切到 `亮色` 并刷新，再切回 `深色`。
  - (a) 首次打开为深色；切换后 `localStorage` 保存了主题，刷新后保持所选主题。键名按 Mantine 默认 `localStorageColorSchemeManager` 推断为 `mantine-color-scheme-value`（本仓库未自定义，未在已安装的库源码里核对），以实际读到的键为准。
  - (b) 菜单只有 `深色`、`亮色`，所选项带勾；`<meta name="theme-color">` 亮色为 `#faf6f0`、深色为 `#171412`。
  - (c) 服务端文件不变，不发任何 API 请求。
  - (d) 手动把该键改为非法值后刷新 → 页面仍能渲染（回到默认深色或 Mantine 的处理结果），记下实际行为。
- **侧栏宽度**：拖宽后刷新。
  - (a) `localStorage.redpanda.sidebarWidth` 为松手时的宽度，刷新后宽度保留。
  - (b) 分隔条的 `aria-valuenow` 随拖动变化。
  - (c) 主区域内容随之变窄/变宽，不被遮挡。
  - (d) 拖到极小/极大 → 被夹在 220 和 520；把该键手动改成非数字后刷新 → 回到 288。
- **窄屏**：视口 390×844。
  - (b) 出现 `打开 Agent 名册`，点开能切换 Agent，选完自动收起。
  - (c) 刷新后仍正常收起，不丢会话。
  - (d) 打开子 Agent 内容页（标题 `子 Agent`）后，输入框仍可用，`关闭内容页` 可点并关闭；内容页宽度被限制在可用宽度内。
- **欢迎区**：(b) 刚打开的草稿会话与没有工作区的首页都显示 `折纸小熊猫` 与 `今天想一起完成什么？`；发出首条消息后欢迎区消失。

## Gotchas

- 纯前端行为，不需要模型；可作为每次 UI 改动的冒烟。
- 主题存储键是根据 Mantine 默认值推断的，不是本仓库源码里写出的常量；核对时以浏览器实际 `localStorage` 为准并记下键名。
- 子 Agent 不再是独立侧面板，而是通用内容页 `ContentPage`；旧判据里的「子 Agent 面板」指它。
- 历史 live 记录：未跑（2026-10-12，main@1a4790a）：纯界面，UI 未验。2026-10-18 的修订只做源码核对，未 live 复跑。
