# 会话文件变更审阅

Agent 会话（名册里的会话）在输入框上方显示尚未落地的 Git 差异；点击摘要在右侧打开标题为 `文件改动` 的内容页（`ContentPage`，不是抽屉），选择文件查看旧、新行号和文本差异。接口也支持委派子任务，但其观察页尚无独立差异入口。其他会话没有变化查看，接口返回 503。来源：`web/src/features/conversation/FileChangesPanel.tsx`、`Conversation.tsx`、`ContentPage.tsx`、`web/src/api/fileChanges.ts`、`redpandaApi.ts`、`web/src/realtime/eventBridge.ts`、`redpanda/assistant/conversations.py`（`file_changes`）、`redpanda/assistant/agent_git.py`（`review`）、`redpanda/channels/web/app.py`。

## Sub-features

- `summary`：输入框上方按钮，文字为 `N 个文件已更改`（加载中 `读取文件变更`，摘要请求失败 `文件变更暂不可用`），后接 `+a`、`−r`。二进制文件不计行数。按钮旁图标按钮 aria-label `刷新文件变更`。
- `content-page`：内容页 `section` 的 aria-label 与标题都是 `文件改动`；关闭按钮 `关闭内容页`；工具栏左侧显示所选文件路径（无选择时显示 `当前会话仍保留的净变更`），右侧为所选文件的 `+a −r`、第二个 `刷新文件变更` 图标按钮、`文件列表` 开关（`aria-expanded`）。
- `file-list`：侧栏 aria-label `改动文件列表`，标题 `改动文件 · N`；按目录分组（`details` 可折叠）。每个文件是按钮，名称 `查看 <path> 的差异`，`aria-pressed` 标示当前选中；按钮内显示状态字母 `A`/`D`/`M`（`新增`/`删除`/`修改` 只出现在字母的 aria-label 和按钮 title `<path> · <状态>` 里）与 `+a −r`。打开内容页时默认选中第一个文件。
- `line-diff`：差异区 aria-label `<path> 的差异`；展示旧、新行号、`+`/`−` 和正文。Git 差异整体视为该 Agent 的工作，不区分会话外修改；二进制文件只列变化，显示「二进制或非 UTF-8 内容，无法显示文本差异。」；权限变化单独一行（`权限：<旧> → <新>`）。
- `pagination`：每页 200 个展示行（含 hunk 标题与上下文），`上一页`/`第 N 页`/`下一页`，首页 `上一页` 禁用、`next_offset=null` 时 `下一页` 禁用；分页不改变完整差异的统计。文件 revision（由两端对象身份算出）变化时差异区重建，回到第一页。
- `refresh`：SSE 事件触发 `FileChanges` 缓存失效后自动刷新（`eventBridge.ts`）；两个 `刷新文件变更` 图标按钮手动刷新；差异读取失败时 Alert 内的 `刷新列表` 同时刷新摘要和该文件差异。
- `scope`：Agent 的变化是它工作树当前状态（含已提交、未提交与未跟踪，不含被忽略文件）相对它与集成分支分叉点的差异；已提交并成功 `land_branch` 的成果不再显示，未提交残留仍显示。委派子任务的只读接口以父 Agent 的分支为基准，`merge_subagent` 后已合入成果不再显示。查询借临时索引收集未跟踪文件，不改动 Agent 自己的索引与 `git status`。

## How to get to it (user POV)

- 从名册打开一个 Agent 会话，在输入框上方找到 `N 个文件已更改`，点击后打开 `文件改动` 内容页；默认已选中第一个文件，可在 `改动文件 · N` 列表里点其他文件。
- 无变更时仍有 `0 个文件已更改` 入口，内容页显示 `当前没有保留的会话变更。`。
- 打开内容页 `子 Agent` 可观察任务过程与交回结论（见 [subagents.md](./subagents.md)），当前没有子任务的文件差异入口。只读 changes 接口能核对子任务差异，但不能替代这个 UI 入口的验证。
- 先按 `../SKILL.md` 检查隔离实例。通过编码回合准备变更时 `live_model_ready` 必须为 true；不满足就记录未能准备真实成果，不用直接 POST 或伪造会话冒充页面证明。

## Driving it with the browser

前置：隔离工作区是有首个提交的 Git 仓库（`git init && git add note.txt && git commit -m init`），已在名册里用它创建 Agent A。Agent 工作树位于 `$REDPANDA_HOME/state/coding_bot/<项目>/<Agent>/`。涉及工具写入时明确授权。证据保存到 `$EVIDENCE_DIR/session-file-changes/`，不同状态用独立子目录保留截图、快照、只读响应和 `git status` 输出，不能互相覆盖。

- **定位控件**：页面上有两个 aria-label 同为 `刷新文件变更` 的按钮（摘要旁一个、内容页工具栏一个）。按名称点击时要限定容器（内容页在 `section[aria-label="文件改动"]` 内），否则会命中第一个或报重名。
- **摘要、列表与正文**：在 `消息` 中发送：`请用 write_file 新建 review.txt，UTF-8 内容为 first 和 second 两行，每行末尾都有换行；不要提交，也不要修改其他文件。完成后停止。` 等文件操作完成。
  - (a) Agent 工作树里 `review.txt` 为 `first\nsecond\n`；`GET /api/sessions/<A>/changes` 仅一个文件，`status=added`、`added=2`、`removed=0`。
  - (b) 页面无需手动刷新显示 `1 个文件已更改`、`+2`；点击摘要出现 `文件改动` 内容页，`review.txt` 已被选中（`查看 review.txt 的差异` 的 `aria-pressed=true`），状态字母 `A`（aria-label `新增`），新行号 1/2 及对应正文。只读 `GET .../changes?path=review.txt&revision=<摘要中该文件的 revision>&offset=0` 与页面一致。
  - (c) Agent 工作树的 `git status --porcelain` 在查询前后相同（未跟踪的 `review.txt` 仍是 `??`）；读列表、点文件和翻页不改磁盘文件，也不新增模型决策或工具调用。人自己的项目目录不出现 `review.txt`。
  - (d) 若显示 `文件变更暂不可用`（摘要请求失败）或内容页显示「无法读取文件变更，请刷新重试。」，先 Doctor，再读 `GET .../changes` 的 503 原文；确认实例正常后按提示刷新，不将警告当成零变更。
  - 临时差异读取失败后点击 `刷新列表`，应重新请求摘要及该文件差异；即使文件 revision 未变也应能恢复，不得一直显示缓存的错误。
- **提交与落地**：让 A 提交 `review.txt`，再调用 `land_branch`。落地、同步、冲突与挡住的判据在 [land-and-sync.md](./land-and-sync.md)；这里只核对变化查看。
  - (a) 提交后列表仍含 `review.txt`（已提交但未落地的工作也算）；落地成功后 `files=[]`。
  - (b) 摘要最终为 `0 个文件已更改`，内容页显示 `当前没有保留的会话变更。`。
  - (c) 人自己的项目目录与检出分支不变。
  - (d) 让 A 修改文件后又改回原值（不提交）→ 该文件不出现在列表里；差异以工作树当前状态为准，不累加工具调用次数。
- **非 Agent 会话**：对 TUI 或旧普通会话调用 `GET /api/sessions/<id>/changes` → 503（`只有 Agent 和它委派的任务提供文件变化查看。`），页面显示 `文件变更暂不可用`。
- **分页与旧结果失效**：让 A 创建 `review.txt`，包含编号 1 至 230，每行一个编号并带换行。点文件后用 `下一页`、`上一页`。
  - (a) 摘要始终新增 230 行；各页读取相同 `revision`，按 `next_offset` 续读覆盖全部正文。每页最多 200 个展示行，包含 hunk 标题与上下文，不能将页大小当作新增行数。
  - (b) 页码 `第 1 页` → `第 2 页`，正文变化；最后一页 `下一页` 禁用，首页 `上一页` 禁用。
  - (c) 分页与摘要统计分离；文件更新后使用新 revision 并回到 `第 1 页`。
  - (d) 保存旧 revision，让 A 再修改文件，随后对旧 revision 发只读差异请求 → 409（`文件变化已更新，请刷新后查看。`）；页面提示 `文件变化已更新或暂时无法读取。` 与 `刷新列表`，点击后可继续。不存在或已无变化的文件 → 404（`该文件已没有当前会话的变更，请刷新列表。`）；`path=../outside.txt` → 422（`文件路径必须是工作区内的相对路径。`），不读工作区外文件；不带 `path` 却带 `offset>0` 或 `revision` → 422（`续读时必须指定文件。`）。
- **自动同步**：按 [realtime-events.md](./realtime-events.md) 的变更同步步骤，用两个标签页观察同一会话；保留工具完成后、无需手动刷新的摘要与 diff 证据。

## Gotchas

- 入口名称：内容页标题是 `文件改动`，不是 `会话变更`；`会话变更` 只出现在空状态文字 `当前没有保留的会话变更。` 里。没有名为 `刷新` 的按钮，手动刷新是图标按钮 `刷新文件变更`（两处同名），`刷新列表` 只在差异读取失败的 Alert 中出现。
- 文件状态在界面上是字母 `A`/`D`/`M`；按可见文字找 `新增` 找不到，要用 aria-label 或 title。
- 同步进来的同事成果已在集成分支上，不出现在差异里；被忽略的文件不出现。
- 外部编辑没有自动监听，需手动刷新。
- 本入口只读，不提供接受、撤销或回退按钮；撤销由 Agent 或人用 Git 完成。
- 已实测确认的入口缺口（2026-10-20）：`SubagentPanel.tsx` 没有接入文件差异，不能按旧地图声称子任务观察页可查看差异。

- 2026-10-18 改写：文件回退移除后，本条只验证 Agent 与其委派任务的 Git 差异；旧的会话操作证据、`get_changes` 与来源标记判据已删除。未 live 复跑。
- 2026-10-18 源码核对（未 live）：修正入口名为 `文件改动` 内容页、刷新控件为两个同名 `刷新文件变更`、状态字母 A/D/M、默认选中第一个文件、分页控件与各错误文案。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：从摘要打开文件改动，note.txt 的 verify→edited、行号和禁用分页与 GET 一致；子 Agent 观察页实测没有差异入口。大文件分页与版本冲突未重验。
