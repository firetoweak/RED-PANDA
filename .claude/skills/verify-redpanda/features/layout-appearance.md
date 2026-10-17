# 布局与外观

明暗主题、侧栏宽度、窄屏会话列表。来源：`web/src/app/App.tsx`、`ColorSchemeSwitcher.tsx`、`sidebarWidth.ts`、`ConversationWelcome.tsx`、`SubagentPanel.tsx`。

## Sub-features

- `color-scheme`：`外观` 菜单切换明/暗/跟随系统。
- `sidebar-resize`：拖动 `调节侧栏宽度`。
- `mobile-sidebar`：视口窄于 `sm` 时侧栏收进 `打开会话列表`。
- `welcome`：空会话显示 `折纸小熊猫` 欢迎图。

## How to get to it (user POV)

- 侧栏底部 `外观`；侧栏右边缘拖拽；把浏览器窗口缩窄。

## Driving it with the browser

- **主题**：切到暗色并刷新。(a) `localStorage` 中保存了主题（Mantine color scheme 键），刷新后仍为暗色。(c) 服务端文件不变。
- **侧栏宽度**：拖宽后刷新，(a) 宽度保留（`sidebarWidth.ts` 的存储键）。(d) 拖到极小/极大应被夹在上下限内，主区域不被挤没。
- **窄屏**：视口 390×844。(b) 出现 `打开会话列表`，点开能切换会话，选完自动收起。(d) 子 Agent 面板在窄屏下不遮挡输入框到不可用。

## Gotchas

- 纯前端行为，不需要模型；可作为每次 UI 改动的冒烟。

## Live status
- 未跑（2026-10-12，main@1a4790a）：纯界面，UI 未验
