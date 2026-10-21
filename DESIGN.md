---
name: RED PANDA
description: 轻盈协作：以清楚的身份与克制的明暗界面承托长期对话。
colors:
  canvas-light: '#ffffff'
  sidebar-light: '#f7f8fa'
  surface-light: '#f5f6f8'
  bubble-light: '#edf1f8'
  tile-light: '#e9edf3'
  border-light: '#e8ebef'
  border-strong-light: '#dce1e8'
  hover-light: '#eef0f4'
  placeholder-light: '#697588'
  text-light: '#242a35'
  dimmed-light: '#667080'
  accent-text-light: '#963419'
  accent-ring-light: rgba(185, 62, 29, 0.48)
  identity-blue-light: '#365fad'
  identity-blue-bg-light: '#e8eefb'
  identity-purple-light: '#7750ac'
  identity-purple-bg-light: '#f0e9fb'
  identity-green-light: '#29765f'
  identity-green-bg-light: '#e4f1eb'
  identity-amber-light: '#90630c'
  identity-amber-bg-light: '#fff1cf'
  canvas-dark: '#202227'
  sidebar-dark: '#191b20'
  surface-dark: '#282b32'
  bubble-dark: '#2b3545'
  tile-dark: '#30343d'
  border-dark: '#32353e'
  border-strong-dark: '#444955'
  hover-dark: '#292c33'
  placeholder-dark: '#a0a6b2'
  text-dark: '#e4e5e9'
  dimmed-dark: '#a0a6b2'
  accent-text-dark: '#ffa078'
  accent-ring-dark: rgba(247, 123, 76, 0.5)
  identity-blue-dark: '#acc7ff'
  identity-blue-bg-dark: '#2d3c59'
  identity-purple-dark: '#d4baff'
  identity-purple-bg-dark: '#3e3154'
  identity-green-dark: '#a0d8c1'
  identity-green-bg-dark: '#263e35'
  identity-amber-dark: '#efd18e'
  identity-amber-bg-dark: '#443a29'
  composer-dark: '#25282e'
  panel-dark: '#1c1e23'
  composer-focus-dark: '#8896ad'
  button-dark: '#dce1e9'
  button-text-dark: '#20242b'
  human-dark: '#3a352f'
  human-text-dark: '#ded0be'
  composer-light: '#ffffff'
  panel-light: '#fafbfc'
  composer-focus-light: '#9ca8b9'
  button-light: '#27313f'
  button-text-light: '#ffffff'
  human-light: '#eeeae5'
  human-text-light: '#786c61'
typography:
  title:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 18px
    fontWeight: 650
  title-mobile:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 16px
    fontWeight: 650
  body:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 15px
    fontWeight: 400
    lineHeight: 1.85
  body-mobile:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.85
  label:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 13px
    fontWeight: 500
  author:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 12px
    fontWeight: 600
    lineHeight: 1.5
  metadata:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 11px
    fontWeight: 400
    lineHeight: 1.5
  new-bot:
    fontFamily: '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 13px
    fontWeight: 600
    lineHeight: 1
  code:
    fontFamily: '"JetBrains Mono", "Cascadia Code", Consolas, monospace'
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.65
rounded:
  xs: 4px
  sm: 6px
  md: 10px
  lg: 14px
  xl: 20px
  search: 6px
  avatar: 9px
  avatar-mobile: 7px
  attachment: 12px
spacing:
  step-4: 4px
  step-6: 6px
  step-8: 8px
  step-10: 10px
  step-12: 12px
  step-14: 14px
  step-16: 16px
  step-20: 20px
  step-24: 24px
  step-26: 26px
  step-28: 28px
  step-32: 32px
  step-44: 44px
  step-56: 56px
components:
  button-new-bot-light:
    backgroundColor: '{colors.canvas-light}'
    textColor: '{colors.text-light}'
    typography: '{typography.new-bot}'
    rounded: '{rounded.md}'
    padding: 0 12px
    height: 40px
    width: 100%
  button-send-light:
    backgroundColor: '{colors.text-light}'
    textColor: '{colors.canvas-light}'
    rounded: '{rounded.xl}'
    size: 30px
  input-search-light:
    backgroundColor: '{colors.canvas-light}'
    textColor: '{colors.text-light}'
    rounded: '{rounded.search}'
    height: 36px
  navigation-row-light:
    backgroundColor: '{colors.sidebar-light}'
    textColor: '{colors.text-light}'
    rounded: '{rounded.md}'
    padding: 8px
  navigation-row-selected-light:
    backgroundColor: '{colors.tile-light}'
    textColor: '{colors.text-light}'
    rounded: '{rounded.md}'
    padding: 8px
  mention-atlas-light:
    backgroundColor: '{colors.identity-blue-bg-light}'
    textColor: '{colors.identity-blue-light}'
    rounded: '{rounded.xs}'
    padding: 3px 5px
  message-bot-light:
    backgroundColor: '{colors.surface-light}'
    textColor: '{colors.text-light}'
    typography: '{typography.body}'
    rounded: '{rounded.md}'
    padding: 12px 16px 15px
  message-human-light:
    backgroundColor: '{colors.bubble-light}'
    textColor: '{colors.text-light}'
    typography: '{typography.body}'
    rounded: '{rounded.md}'
    padding: 12px 16px 15px
  composer-group-light:
    backgroundColor: '{colors.canvas-light}'
    textColor: '{colors.text-light}'
    rounded: '{rounded.md}'
    padding: 0px
  button-new-bot-dark:
    backgroundColor: '{colors.canvas-dark}'
    textColor: '{colors.text-dark}'
    typography: '{typography.new-bot}'
    rounded: '{rounded.md}'
    padding: 0 12px
    height: 40px
    width: 100%
  button-send-dark:
    backgroundColor: '{colors.text-dark}'
    textColor: '{colors.canvas-dark}'
    rounded: '{rounded.xl}'
    size: 30px
  input-search-dark:
    backgroundColor: '{colors.canvas-dark}'
    textColor: '{colors.text-dark}'
    rounded: '{rounded.search}'
    height: 36px
  navigation-row-dark:
    backgroundColor: '{colors.sidebar-dark}'
    textColor: '{colors.text-dark}'
    rounded: '{rounded.md}'
    padding: 8px
  navigation-row-selected-dark:
    backgroundColor: '{colors.tile-dark}'
    textColor: '{colors.text-dark}'
    rounded: '{rounded.md}'
    padding: 8px
  mention-atlas-dark:
    backgroundColor: '{colors.identity-blue-bg-dark}'
    textColor: '{colors.identity-blue-dark}'
    rounded: '{rounded.xs}'
    padding: 3px 5px
  message-bot-dark:
    backgroundColor: '{colors.surface-dark}'
    textColor: '{colors.text-dark}'
    typography: '{typography.body}'
    rounded: '{rounded.md}'
    padding: 12px 16px 15px
  message-human-dark:
    backgroundColor: '{colors.bubble-dark}'
    textColor: '{colors.text-dark}'
    typography: '{typography.body}'
    rounded: '{rounded.md}'
    padding: 12px 16px 15px
  composer-group-dark:
    backgroundColor: '{colors.canvas-dark}'
    textColor: '{colors.text-dark}'
    rounded: '{rounded.md}'
    padding: 0px
---

# Design System: RED PANDA

## Overview

**Creative North Star: "轻盈协作"**

以用户已选定的「轻盈协作」为视觉北极星：白色与中性炭灰承托对话，浅气泡区分消息，身份色帮助在长报告中找到同事。界面克制、清楚，保留正文的阅读节奏。

当前实现使用本机中文无衬线字体、细线 SVG 图标、小圆角与轻边界。明暗主题共用布局和身份关系；陶土色出现在部分操作与定位状态，蓝色承担全局键盘焦点和文字选择。

本文件记录已落地的 React / Mantine 界面；具体页面的构图与操作模式保留在对应 surface brief。品牌图片沿用仓库已有的 assets/brand/red-panda-mark-pixel.png（favicon / 回退头像）与 red-panda-full.png（空私聊欢迎）；其已有来源随原资产保留，本次没有生成或替换图片，侧栏不展示品牌图。

**Key Characteristics:**
- 明暗共用的灰阶聊天工作面
- 头像、署名与点名保持同一身份色
- 浅气泡、完整正文、气泡内代码滚动
- 系统中文字体与宽松正文行距

## Colors

前置 tokens 以 light / dark 后缀记录同一语义的两套实际值；运行时由根节点的主题属性切换 CSS 变量。颜色不承担唯一的信息表达，状态仍有文字或图标。

### Primary

陶土操作色来自现有 ember 色阶：用于新建图标、部分辅助操作、未读与引用定位。发送按钮采用正文色与画布色反转，不把整个页面铺成品牌色。焦点与文字选择使用身份蓝。

### Secondary

四组身份前景与浅底在明暗中保持稳定对应。

**The Identity Continuity Rule.** 同一 Bot 的头像、署名与正文点名使用同一身份色；Atlas 蓝、Nova 紫、Echo 绿、Mori 琥珀不随主题交换。

### Neutral

聊天底色、侧栏、Bot 气泡、人类气泡与选中底色分别对应 canvas、sidebar、surface、bubble、tile。正文与次要文字分层；边界与悬停使用来源中的半透明色，不另造不透明近似。占位文字独立于 disabled 样式；浅色和深色采用 placeholder tokens，最终评审记录的对应底色对比度约为 4.67:1 和 6.51:1。

## Typography

**Body Font:** 前置 tokens 中的本机 Segoe UI Variable Text / Segoe UI / Microsoft YaHei UI / PingFang SC 无衬线栈；用户明确批准系统字体，不引入外部字体请求。

**Label/Mono Font:** 标签沿用正文栈；代码使用 JetBrains Mono / Cascadia Code / Consolas 等宽栈，均使用已有本机字体及回退。

### Hierarchy

- **Title:** 聊天标题使用 title，小屏使用 title-mobile；正文中标题维持正文字号、较重字重（650），行高（1.5），上下节奏（1.6em / .7em）。
- **Body:** 正文使用 body，小屏使用 body-mobile；段落与列表行高（1.85）。长气泡桌面上限为 min(calc(100% - 84px), calc(72ch + 32px))，正文和底色受同一宽度限制。
- **Label / Author / Metadata:** 普通导航标签、作者和时间分别使用 label、author、metadata；时间使用等宽数字。新建 Bot 使用独立的 new-bot 角色。
- **Code:** pre 使用 code；保留空白、不强行折行，在自身容器内横向滚动。正文图片及 SVG 限制在容器宽度内。

**The One Bubble Measure Rule.** 长消息的整块气泡受正文行宽与可用空间共同限制；不要让代码或 diff 扩大整块底色。

## Layout

应用填满视口高度（100dvh），页面本身不纵向滚动；对话、名册与资料区各自滚动。最小页面宽度（320px）。桌面侧栏默认（258px），可拖动到（220–520px），内边距（20px 16px 15px）。顶部先新建 Bot，再搜索、对话过滤和混合名册；底部保留模型配置与主题切换。

消息列表与输入区共用居中的读写宽列，上限（1040px），空间不足时两侧合计留白（48px）；Bot 靠左、人的消息靠右，气泡继续限制正文行宽。群消息间隔（26px），私聊 turn 底部间距（26px），turn 内部间隔（24px）。正文之后是署名与时间，其下常显状态、引用和复制，头像靠消息末尾对齐。思考与执行入口并排紧凑排列，展开内容才使用详情宽度。气泡与头像间距（12px）。写入区与消息列表共享宽度上限（1040px）；群聊与私聊共用 A 原稿的框体、文字和工具栏。群聊收起时文本区（34px）、padding（9px 14px 0），工具栏 padding（0 10px 5px），整框约（70.6px）；长于90字符、换行或引用时展开。私聊及展开态文本区至少（76px）、padding（14px 16px 5px），工具栏 padding（5px 10px 9px），最多自动扩至七行。右下角统一为（66×30px）「发送 ↑」按钮，6px 圆角；私聊保留附件、授权、模型与停止/继续。

头部桌面最小高度（77px）、内边距（0 29px）。资料区为可选次级面板，实际桌面宽（288px），80em 以下为（256px）；64em 以下隐藏桌面资料并以群资料抽屉（300px）等窄屏入口呈现。文件查看也在 64em 以下占满剩余主区域。

48em 以下折叠侧栏并显示左上菜单；头部最小高度（69px），内边距（12px 14px 12px 58px）。消息容器宽度改为 calc(100% - 28px)，头像（27px）、间距（8px），气泡上限 calc(100% - 51px)、内边距（10px 12px 12px）；写入区左右留白（14px）。消息操作在所有屏幕持续可见。spacing tokens 记录实际复用步长，不要求所有空间都落在单一倍数网格。

## Elevation & Depth

主要依靠明度、浅底与细边界区分层次。消息气泡没有阴影与描边；两套输入区沿用 A 原稿的轻投影（0 3px 12px #202e4510），聚焦使用专用边线色与轻状态环（0 0 0 2px #8b9bb00c）。成员点名菜单使用（0 6px 24px var(--rp-shadow)），回到最新提示使用（0 3px 12px var(--rp-shadow)）；编辑消息保留现有（0 0 0 1px var(--rp-accent-glow)）状态环。确切阴影来源与动效记录在 sidecar。

**The Flat Reading Surface Rule.** 静态气泡保持无描边、无投影；阴影按实际组件逐项记录，不推导成所有容器通用的浮卡样式。

## Shapes

Mantine 的半径尺度为 xs / sm / md / lg / xl，默认 md。实际气泡与输入区边框均为 md（10px），没有沿用原型的 12px composer。搜索、新建 Bot 与过滤容器采用 sm（6px），常规身份头像采用 avatar（9px），小屏消息头像采用 avatar-mobile（7px），资料头像采用 attachment（12px）。点名小底色采用 xs（4px）。引用使用左侧细线与（0 5px 5px 0）圆角；按钮依组件使用小圆角或主题 xl。不要将所有组件统一为胶囊。

## Components

### Buttons

新建 Bot 为全宽 40px、13px 字、6px 圆角按钮。发送沿用 A 原稿的文字加上箭头按钮，默认使用专用前景和背景，disabled 保留同色并降低至 .4；不使用圆形纯图标按钮。次级操作为细线 SVG 图标和弱悬停底色，保留可读标签。

### Chips

正文 @ 点名是 inline 小底色标签，使用身份前景与底色，字号（.93em），不换行；padding 与半径见 tokens。它只表达文中点名，不把引用自动转换为点名。

### Cards / Containers

浅气泡随内容收缩，长气泡统一限制宽度；人的整条消息反向排列，头像在右侧外边，Bot 在左侧。两者正文左对齐。执行与思考入口保持透明，细线与弱底用于实际子区域；不把它们变成厚重浮卡。

### Inputs / Fields

搜索是画布底、细边界、search 半径，支持 Ctrl/Cmd K 聚焦。群聊与私聊输入均使用 13px / 1.7 文本，仅最少行数不同。两套输入均显式设置占位文字和 opacity: 1；正常输入不借用 disabled 的淡化。引用预览在 textarea 上方，作者保持身份色，预览单行截断，可打开原消息或移除。Enter 发送、Shift+Enter 换行，中文输入法组合期间不发送。

### Navigation

名册行最小高度（58px）、内边距（10px 11px），默认透明，hover / focus-within 加薄层，选中使用 tile。常规溢出操作在 hover / focus-within 显示，触摸环境直接显示。搜索、过滤、Bot 与群聊共享这一列；侧栏不恢复品牌块与个人工作区标题。

### Browser Interaction

全局文字选择为身份蓝的前景与底色；键盘 focus-visible 为（2px）身份蓝外框，偏移（3px）。部分已有链接的更具体规则保留陶土定位环；引用目标同样使用陶土环。textarea 的 caret 使用身份蓝。所有原生滚动条声明 thin、强调边界色拇指与透明轨道，虚拟时间线另保留 stable gutter；Mantine ScrollArea 名册为 hover 显示。侧栏拖动时使用 col-resize 且暂时禁止选字。

主界面切换过渡（180ms），拖动侧栏时关闭；轻量背景过渡（150ms ease-out）、操作显隐（120ms ease）、活动指示旋转（0.8s linear infinite）。prefers-reduced-motion 关闭该旋转和卡片过渡，并强制 scroll-behavior: auto，不额外添加动画。

## Do's and Don'ts

### Do:
- Do 让身份色贯穿同一 Bot 的头像、署名和点名。
- Do 让整个人类气泡靠右、Bot 气泡靠左，气泡里的正文保持左对齐。
- Do 保持两套主题的占位文字显式着色且 opacity 为 1。
- Do 让代码与表格在气泡内部横向滚动，并保留明显的键盘焦点。

### Don't:
- Don't 把人类消息改成只有文字右对齐，或把长代码撑出的宽度当作正文宽度。
- Don't 在侧栏顶部恢复品牌块或个人工作区标题。
- Don't 为此界面加载外部字体或用新造标识替换现有品牌图片。
- Don't 将未使用的原型色值、12px 原型输入框圆角或欢迎页装饰文字升格为当前组件规范。

未规范化项：空私聊欢迎页仍有英文装饰性 welcome-brand 文案，只记录现状，不作为未来页面可继承的 kicker / eyebrow。finish-verdict 已解决占位文字对比度，仅在已评审范围内为 ship，不扩张为全产品验证结论。
