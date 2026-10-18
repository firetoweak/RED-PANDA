# Agent 名册

左侧按项目列出 Agent，支持新建、切换、折叠分组，并显示每个 Agent 的状态。不列普通会话，也不提供改名或归档。来源：`web/src/features/sessions/SessionSidebar.tsx`、`CreateAgentModal.tsx`、`WorkspaceHeader.tsx`、`AgentHome.tsx`、`app.py` 的 `/api/agents`。

文件审阅仍是会话变更，不在这一条。

## Sub-features

- `create-agent`：侧栏 `新建 Agent`，或各组的 `在 <名称> 新建 Agent`。弹窗标题 `新建 Agent`，有多个项目时先选 `项目`，再填 `名字`（说明 `项目内唯一，区分大小写，不能带首尾空白`），按钮 `创建`。名字去掉空白后为空时 `创建` 不可点。成功后地址变为 `/sessions/<id>`。
- `switch`：点 Agent 行（可访问名称为 Agent 名字）→ `/sessions/<id>`。`POST /api/sessions/{id}/select` 由进入会话页后的 `Conversation.tsx` 发出；创建接口自己也会先选中这条会话。
- `status`：每行左侧活动点取值 `干活`、`等授权`、`等人回话`、`空闲`。有负责方向时名字下方显示方向。侧栏底部 `已连接` / `正在连接…`。
- `group-collapse`：点项目标题折叠/展开；折叠记在 `localStorage` 的 `redpanda.collapsedWorkspaces`。在折叠的项目里新建会先展开。
- `home`：`/` 与 `/workspaces/:workspaceId` 不自动建会话。没有项目时主区域是欢迎图和 `创建工作区`；已有项目时文字是 `从左侧选择一个 Agent，或在项目里新建一个。`

## How to get to it (user POV)

- 侧栏 `新建 Agent`。点 Agent 名字进入它的会话。点项目名折叠分组。

## Driving it with the browser

项目目录必须是已有首个提交的 Git 工作树根。验证前在 `$SCRATCH_WORKSPACE` 做一次 `git init` 和首个提交，再把它登记为工作区。

- **创建**：点 `新建 Agent`，名字填 `验证熊猫`，点 `创建`。
  - (a) `GET /api/agents` 出现该 Agent，`name` 为 `验证熊猫`，`workspace_id` 等于该项目，响应里没有 `worktree` 和 `branch`；`$REDPANDA_HOME/state/coding_bot/` 下有它的工作树。
  - (b) `POST /api/agents` 返回 201；地址变为 `/sessions/<session_id>`；侧栏该项目下出现 `验证熊猫`。
  - (c) 人自己检出的项目目录没有被改成 Agent 工作树；`note.txt` 仍是 `verify`。
  - (d) 同一项目再创建同名 → 弹窗 `无法创建`，正文含 `已有 Agent`，`POST` 为 409。名字写成 ` 验证熊猫` → 422。项目不是 Git 仓库时 422，正文说明要先初始化并创建首个提交。请求体带 `worktree` 或 `branch` → 422，不创建。
- **状态**：创建后尚未打开、Worker 没在跑时活动点为 `空闲` 或进入会话后变为 `干活`（创建事实会请求一次决策）。停在等下一条外部输入时为 `等人回话`；有待授权或待确认时为 `等授权`，打开会话也不把它清掉。
  - (a) 对照 `GET /api/agents` 的 `status`：`working` / `authorization` / `waiting` / `idle`。
- **切换与折叠**：(a) 两个 Agent 来回点，每次消息与 `GET /api/sessions/<id>` 一致。(b) 折叠后 `localStorage.redpanda.collapsedWorkspaces` 含该项目 id 为 `true`，刷新后仍折叠；折叠、展开不发 API。(d) 把该值改成非 JSON 后刷新 → 各组展开，不报错。

## Gotchas

- 名册不提供改名、归档，也不再有 `新建会话` 或 `new agent` 草稿行。
- 状态不是一套新的处境判断：干活来自 Worker 或仍在进行的子任务，等授权来自待授权或待确认，等人回话来自停在等外部事实或当前失败，其余为空闲。
