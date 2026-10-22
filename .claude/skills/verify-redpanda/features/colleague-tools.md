# 名册工具

全部长期 Bot 用 `declare_direction` 声明负责方向，用 `list_colleagues` 查询同事。查询给出当前快照，不含自己，也不含 Session、项目坐标或分支名。方向写入声明者自己的时间线，名册与侧栏只显示它自己的最新一次声明。同事加入、以及别人声明方向，会作为不请求决策的事实投进其他长期 Bot 的时间线。没有单独的声明按钮。来源：`redpanda/assistant/agent_messages.py`、`redpanda/assistant/host/agent_messages.py`、`redpanda/assistant/agents.py`、`GET /api/agents`、`web/src/features/sessions/BotRoster.tsx`。

## Sub-features

- `declare`：`declare_direction` 的参数只有 `direction`。侧栏在名字下方显示这句；没有声明时显示 `打开对话，开始协作`。
- `list`：`list_colleagues` 不接受参数。每位同事有 `name`、`direction`、`directory`、`projects`、`status`、`since`。
- `roster-facts`：第二位 Agent 创建后，已在的同事收到 `bot.colleague_joined`；对方声明方向后收到 `bot.direction_declared`。两条都不请求决策。
- `shared-memory`：所有长期 Bot 用 `read_shared_memory` 查阅、用 `update_shared_memory` 修改同一份正文，最多500字符，含标点、空白和换行，可以为空。修改需要已读的 `version` 和完整 `content`；沿用工具授权。状态在 `$REDPANDA_HOME/state/shared_memory.sqlite`，修改者与来源一起保存。首次工作、内容变化和压缩后，完整正文通过 `bot.shared_memory` / `<shared_memory>` 呈现，不单独唤醒空闲同事。

## How to get to it (user POV)

- 不关联项目也可以建两个 Bot，例如 `Ada` 与 `Lin`。
- 打开 `Ada` 的会话，在 `消息` 里请它声明方向、再查询同事。
- 侧栏 `Ada` 的名字下方看方向。负责方向不会出现在 `/inbox`。

## Driving it with the browser

需要 `live_model_ready`。工具结果的 `code` 在该会话 journal（`$REDPANDA_HOME/runtime_sessions/<sha256(session_id)>/journal.sqlite`）的命令结果里。`GET /api/sessions/<id>` 的 `tools` 只有名字和状态：`ok: false` 的已知结果也显示徽标 `完成`。

- **声明方向**：请 `Ada` 调用 `declare_direction`，方向写 `负责验证地图`。
  - (a) `GET /api/agents` 里 `Ada` 的 `direction` 为这句；journal 有 `bot.direction_declared`，`name` 是 `Ada`。
  - (b) 侧栏 `Ada` 名字下出现 `负责验证地图`；工具卡名为 `declare_direction`，徽标 `完成`。
  - (c) `GET /api/agents` 仍没有 `branch`；`Ada` 的名字不变。`Lin` 的会话不因此多一条发给人的回复。
  - (d) 空白 `direction` → 结果 `INVALID_ARGUMENT`，原方向不变；从未声明过时仍为 `null`，侧栏仍显示占位文案。再声明 `负责私信` 后，名册和侧栏只剩这一句。
- **查询同事**：请 `Ada` 调用 `list_colleagues`，不要带参数。
  - (a) 结果 `code` 为 `COLLEAGUES_READ`。`colleagues` 含 `Lin` 的 `name`、`direction`（未声明为 `null`）、`directory`、`projects`、`status`、`since`；没有 `Ada`；没有 `session_id`、`workspace_id`、`branch`。`directory` 是 Lin 的个人目录，挂上的项目在其 `projects/<项目名>` 下，`projects` 是这些项目名。`status` 对照 Web 名册：`干活` → `working`，`等授权` → `awaiting_authorization`，`等人回话` 和 `空闲` 都是 `idle`。在 `Lin` 的会话点 `停止` 之后，这里是 `paused`（侧栏没有「暂停」二字，看 `GET /api/sessions/<Lin>` 的 `session.paused` 与按钮 `继续`）。
  - (b) 工具卡 `list_colleagues` 徽标 `完成`。
  - (c) 查询不改两边工作树，也不改人检出目录里的 `note.txt`。
  - (d) 带任意参数调用 → `INVALID_ARGUMENT`，错误为 `list_colleagues 不接受参数。`。用返回的 `directory` 请 `Ada` `read_file` 读 `Lin` 项目工作树（`projects/<项目名>` 下）里尚未提交的文件，读得到；`write_file` 写进该目录 → `ENVIRONMENT_PERMISSION_DENIED`，错误含 `path 不在可写入范围内`，`Lin` 的文件不变。
- **加入与方向事实**：先有 `Ada`，再创建 `Lin`，然后让 `Lin` 声明方向。
  - (a) `Ada` 的 journal 先有 `bot.colleague_joined`（`name` 为 `Lin`），之后有 `Lin` 的 `bot.direction_declared`。两条 `requests_decision` 都是 false。
  - (b) `Ada` 的时间线不因此多一条发给人的回复气泡。
  - (c) `/api/agents` 仍是这两个 Agent，群和这些事实不会变成名册里的另一行。
  - (d) `Lin` 尚未声明时，`Ada` 查到的 `direction` 为 `null`，侧栏 `Lin` 名字下显示 `打开对话，开始协作`。

共同记忆增量验证仍从两个 Bot 的 `消息` 输入框进入：请 Ada 读取后保存一句团队约定；Lin 在下次工作时应直接看到该正文，也能读取并修改，Ada 再工作时看到 Lin 的新版本。(a) 数据库最新正文、版本、修改者及两边 journal 的观察一致；(b) 时间线出现对应读写工具卡；(c) 不改工作目录或项目文件，不因更新让空闲同事多出回复；(d) 501字符正文得到 `SHARED_MEMORY_TOO_LONG`，旧版本得到 `SHARED_MEMORY_CHANGED` 并返回最新正文，原文不被覆盖。压缩后继续问同一约定，对照实际模型输入中的 `<shared_memory>`，不能只凭回复猜测注入成功。未实测的子路径单独标注，不以进程测试替代页面证据。

## Gotchas

- 方向是 Agent 自己的声明，不是人在界面上批准的角色。系统不解析对话里的「同意」。
- 同事目录只读，开启全部文件访问后也一样。命令不按路径区分读写，这条判据用 `write_file`，不用 `execute_command`。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：declare_direction 和 list_colleagues 真实调用通过，侧栏显示最新方向；越权写入和非法参数未重验。

2026-10-20 Bot 解耦增量：隔离 home、零代码项目、真实 deepseek/deepseek-flash，通过个人目录写入并读回、方向声明、私信往返、两名成员群聊发言。证据路径与完整日志保存在本地验证记录；未据此宣称其余子功能或完整功能地图已重验。

2026-10-22 共同记忆增量：隔离 Web、1440×1000、真实 deepseek-flash，通过 Ada 保存、Lin 读取修订、Ada 不再调用读取工具即回复最新正文；两名 Bot 的实际 Compact 新窗口均含当前正文。500/501字符、清空、旧版本冲突和并发修改由自动化测试覆盖，未在页面驱动这些错误路径；其他系统与完整功能地图未重验。证据留在本地 `.tools/verification/shared-memory/evidence/`。
