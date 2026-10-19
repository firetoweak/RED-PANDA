# Agent 名册

左侧按项目列出 Agent 与群聊，可在项目内自定义分组、混放条目并折叠，支持新建、切换及状态展示。不列普通会话，也不提供 Agent 改名或归档。来源：`web/src/features/sessions/AgentSidebar.tsx`、`WorkspaceRoster.tsx`、`CreateAgentModal.tsx`、`WorkspaceHeader.tsx`、`AgentHome.tsx`、`app.py` 的 `/api/agents`。

文件审阅是会话页的 `文件改动`，不在这一条。名册工具 `list_colleagues` / `declare_direction` 见 [colleague-tools.md](./colleague-tools.md)。

## Sub-features

- `create-agent`：从工作区的 `在 <名称> 新建 Agent`，或工作台对应项目内的 `新建 Agent` / `添加伙伴` 创建；侧栏顶部不再有全局创建按钮。弹窗标题 `新建 Agent`，项目按入口预选，有多个项目时可选 `项目`，再填 `名字`（说明 `项目内唯一，区分大小写，不能带首尾空白`），按钮 `创建`。名字去掉空白后为空时 `创建` 不可点。成功后地址变为 `/sessions/<id>`。
- `switch`：点 Agent 行（可访问名称为 Agent 名字）→ `/sessions/<id>`。`POST /api/sessions/{id}/select` 由进入会话页后的 `Conversation.tsx` 发出；创建接口自己也会先选中这条会话。
- `status`：每行头像角标取值 `干活`、`等授权`、`等人回话`、`空闲`。名字下方显示负责方向，未设置时为 `打开对话，开始协作`。同一状态也显示在会话身份栏和工作台卡片上。侧栏底部 `已连接` / `正在连接…`。
- `search`：`搜索 Agent、群聊或工作区` 按名称、方向或工作区过滤；搜索时展开匹配项目，清空搜索后恢复此前折叠偏好。无结果显示 `没有找到匹配的 Agent、群聊或工作区`。
- `workbench`：侧栏 `工作台` 打开首页，项目下展示 Agent 卡片、状态和方向；项目页只显示该项目的 Agent 与群聊。从项目中新建 Agent 绑定该项目。
- `group-collapse`：点项目标题折叠/展开；折叠记在 `localStorage` 的 `redpanda.collapsedWorkspaces`。在折叠的项目里新建会先展开。
- `custom-sections`：工作区内 `在 <名称> 新建分组` 建立分类；Bot 和群聊行的右键或 `移动 <名字> 到分组` 菜单可移入已有分类、放回 `未分组`，或新建分类并自动归入。点 `分组 <名称>` 折叠/展开，标题显示条目数。`<名称> 分组操作` 可重命名或删除；删除只把条目放回未分组，不移除 Agent 或群聊。名称不能为空、不能与同工作区分组重复，`未分组` 为保留名称。
- `section-preferences`：分类、归属和折叠保存在当前浏览器 `redpanda.sidebarGroups.<workspace_id>`，按工作区隔离；同源标签页同步，不写入项目或群成员。搜索临时展开匹配分组，清空后恢复保存的折叠状态。
- `home`：`/` 与 `/workspaces/:workspaceId` 不自动建会话。没有项目时主区域是欢迎图和 `创建工作区`；已有项目时文字是 `从左侧选择一个 Agent，或在项目里新建一个。`

## How to get to it (user POV)

- 侧栏工作区右侧 `在 <名称> 新建 Agent`。点 Agent 名字进入它的会话。点项目名折叠分组。

## Driving it with the browser

项目目录必须是已有首个提交的 Git 工作树根。验证前在 `$SCRATCH_WORKSPACE` 做一次 `git init` 和首个提交，再把它登记为工作区。

- **创建**：点对应工作区的 `在 <名称> 新建 Agent`，名字填 `验证熊猫`，点 `创建`。
  - (a) `GET /api/agents` 出现该 Agent，`name` 为 `验证熊猫`，`workspace_id` 等于该项目，响应里没有 `worktree` 和 `branch`；`$REDPANDA_HOME/state/coding_bot/` 下有它的工作树。
  - (b) `POST /api/agents` 返回 201；地址变为 `/sessions/<session_id>`；侧栏该项目下出现 `验证熊猫`。
  - (c) 人自己检出的项目目录没有被改成 Agent 工作树；`note.txt` 仍是 `verify`。
  - (d) 同一项目再创建同名 → 弹窗 `无法创建`，正文含 `已有 Agent`，`POST` 为 409。名字写成 ` 验证熊猫` → 422。项目不是 Git 仓库时 422，正文说明要先初始化并创建首个提交。请求体带 `worktree` 或 `branch` → 422，不创建。
- **状态**：创建后尚未打开、Worker 没在跑时活动点为 `空闲` 或进入会话后变为 `干活`（创建事实会请求一次决策）。停在等下一条外部输入时为 `等人回话`；有待授权或待确认时为 `等授权`，打开会话也不把它清掉。
  - (a) 对照 `GET /api/agents` 的 `status`：`working` / `authorization` / `waiting` / `idle`。
- **切换与折叠**：(a) 两个 Agent 来回点，每次消息与 `GET /api/sessions/<id>` 一致。(b) 折叠后 `localStorage.redpanda.collapsedWorkspaces` 含该项目 id 为 `true`，刷新后仍折叠；折叠、展开不发 API。(d) 把该值改成非 JSON 后刷新 → 各组展开，不报错。
- **自定义分组**：从工作区分组行创建分类，把一位 Agent 和一个群聊移入同组，折叠并刷新。(a) 当前浏览器保存的归属和折叠不变；`GET /api/agents` 与 `GET /api/groups` 的身份、工作区和群成员不变。(b) 搜索组内 Agent 会临时展开该组；清空恢复折叠。(c) 改名后条目保留；删除分类后所有条目回到未分组，仍能打开原会话。(d) 空名称、重复名称和 `未分组` 禁止保存；不同工作区的分组独立。

## Gotchas

- 名册不提供改名、归档，也不再有 `新建会话` 或 `new agent` 草稿行。
- 状态不是一套新的处境判断：干活来自 Worker 或仍在进行的子任务，等授权来自待授权或待确认，等人回话来自停在等外部事实或当前失败，其余为空闲。

- 2026-10-20 PC live：工作台创建两位 Agent、切换、搜索无结果与清除通过；状态与 GET /api/agents 对照一致。

2026-10-20 增量验收：移除侧栏全局创建入口，保留工作区内创建。Chrome 实测工作区加号可打开新建 Agent 表单（未提交创建）；12 项相关测试及构建通过。证据 `.tools/verification/workspace-create-entry/`。

2026-10-20 自定义分组增量验收：Chrome 1366×956，创建分类、右键移动 Ada、把群聊放入同组、为 Lin 新建另一分类并自动归入、折叠刷新、搜索与清空、重命名、删除后 Lin 回到未分组均通过。145 项前端测试及构建通过；跨工作区隔离有组件测试，跨标签页同步本轮未实测。证据 `.tools/verification/sidebar-sections/`，按用户要求保留 8765 服务。
