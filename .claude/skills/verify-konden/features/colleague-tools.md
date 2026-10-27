# 名册工具

长期 Bot 用 `list_colleagues` 查询同事名字、目录、项目与当前状态。查询不含自己，也不含 Session、项目坐标或分支名。同事加入会作为不请求决策的事实进入其他长期 Bot 的时间线；不提供负责方向的填写、声明或展示。

## Sub-features

- `list`：`list_colleagues` 不接受参数。每位同事有 `name`、`directory`、`projects`、`status`、`since`。
- `roster-facts`：第二位 Agent 创建后，已在的同事收到 `bot.colleague_joined`；这些事实都不请求决策。
- `shared-memory`：所有长期 Bot 用 `read_shared_memory` 查阅、用 `update_shared_memory` 修改同一份正文，最多500字符，含标点、空白和换行，可以为空。修改需要已读的 `version` 和完整 `content`；沿用工具授权。状态在 `$KONDEN_HOME/state/shared_memory.sqlite`，修改者与来源一起保存。判断前读取产品中的当前正文；首次或版本变化时先提交 `bot.shared_memory` 观察，再呈现在 `<working_memory>` 中。未变化不重复追加，不唤醒空闲同事。Journal 只拥有观察，不能反写当前正文；compact 从观察原文重述。

- `personal-memory`：每个长期 Bot 用 `read_personal_memory` / `update_personal_memory` 读写自己的工作约束，最多200字符，可以为空。人从该 Bot 资料的 `个人记忆` 页签直接编辑；版本冲突、重试与修改来源沿用共同记忆规则。个人正文同样先形成 `bot.personal_memory` 观察，再按原位置追加呈现。两者都只收短、常用、常改、场景无关的工作约束，知识结论与技能文档归档后按需检索。

## How to get to it (user POV)

- 不关联项目也可以建两个 Bot，例如 `Ada` 与 `Lin`。
- 打开 `Ada` 的会话，在 `消息` 里请它查询同事。
- 人直接维护个人记忆：打开 Bot 对话 → Bot 资料 → `个人记忆`，编辑 `个人记忆正文` 后 `保存记忆`；切换页签保留草稿，切换 Bot 不串正文或草稿。
- 人直接维护共同记忆：侧栏底部 `设置 → 共同记忆` 打开抽屉，`共同记忆正文` 显示全文和最近修改者；`保存记忆` 提交，`撤销修改` 恢复当前正文。无需先创建 Bot。

## Driving it with the browser

Bot 工具和下一轮读取验证需要 `live_model_ready`；Web 共同记忆编辑本身不需要模型。工具结果的 `code` 在该会话 journal（`$KONDEN_HOME/runtime_sessions/<sha256(session_id)>/journal.sqlite`）的命令结果里。`GET /api/sessions/<id>` 的 `tools` 只有名字和状态：`ok: false` 的已知结果也显示徽标 `完成`。

- **查询同事**：请 `Ada` 调用 `list_colleagues`，不要带参数。
  - (a) 结果 `code` 为 `COLLEAGUES_READ`。`colleagues` 含 `Lin` 的 `name`、`directory`、`projects`、`status`、`since`；没有 `Ada`；没有 `session_id`、`workspace_id`、`branch`。`directory` 是 Lin 的个人目录，挂上的项目在其 `projects/<项目名>` 下，`projects` 是这些项目名。`status` 对照 Web 名册：`干活` → `working`，`等授权` → `awaiting_authorization`，`等人回话` 和 `空闲` 都是 `idle`。打断当前轮后，状态继续按实际运行情况投影；后续新消息仍能唤醒。
  - (b) 工具卡 `list_colleagues` 徽标 `完成`。
  - (c) 查询不改两边工作树，也不改人检出目录里的 `note.txt`。
  - (d) 带任意参数调用 → `INVALID_ARGUMENT`，错误为 `list_colleagues 不接受参数。`。用返回的 `directory` 请 `Ada` `read_file` 读 `Lin` 项目工作树（`projects/<项目名>` 下）里尚未提交的文件，读得到；`write_file` 写进该目录 → `ENVIRONMENT_PERMISSION_DENIED`，错误含 `path 不在可写入范围内`，`Lin` 的文件不变。
- **加入事实**：先有 `Ada`，再创建 `Lin`。`Ada` 的 journal 应有 `bot.colleague_joined`，`name` 为 `Lin` 且 `requests_decision` 为 false；不会因此多出给人的回复，名册仍是两个 Bot。

共同记忆增量验证仍从两个 Bot 的 `消息` 输入框进入：请 Ada 读取后保存一句团队工作约束；Lin 下次工作应直接看到当前正文，也能读取并修改，Ada 再工作时看到 Lin 的新版本。(a) 数据库拥有当前正文，各 Bot 的 journal 只记录已观察版本，空闲 Bot 可以尚未观察新版；(b) 页面保存后不得反写旧观察，下一次判断必须先有新版本观察，再出现在实际请求中；(c) 不改项目文件，不因更新唤醒空闲同事；(d) 501字符正文返回 `SHARED_MEMORY_TOO_LONG`，旧版本返回 `SHARED_MEMORY_CHANGED` 及最新正文。

前缀增量：实际连续推进至少三轮，记忆未变化时，后一请求应保留前一请求的完整消息前缀，不能用同一冻结历史反复调用来代替。分别修改共同、个人正文及清空后继续推进，旧观察留在原位置，新版只追加一次；旧版不得覆盖新正文。检查实际请求和 journal，不只凭回复猜测。工具结果脱水、工具 Schema 变化与 compact 换窗各有自己的前缀变化，不冒称为记忆更新引起。

Compact 增量：确认冻结的交接请求保留当时观察，新窗口从该起点最近的共同、个人观察原文重述，再按原顺序接入期间变化；不拿发布时的新版回填起点，也不重复注入期间更新。新窗口连续两轮及一次记忆更新／清空后仍保持该窗口内前缀。新旧窗口之间不要求前缀相同；handoff 不负责转述或恢复当前记忆。

Web 编辑增量：输入500个表情，计数是 `500 / 500` 且可保存；501个字显示 `超过500字，请精简后保存。` 且不能保存。清空后可保存，最近修改者为 `用户`；Bot 下次工作直接看到该正文。关闭抽屉、进入模型配置再返回，未保存草稿仍在。另一页面或 Bot 在编辑期间先保存时，显示最新正文并保留自己的草稿，`保存记忆` 禁用；点 `已核对，继续编辑` 后才能按新版本保存。GET/PUT `/api/shared-memory` 与页面一致；未知或失效 Web 连接不能写入，重投同一次成功操作不增加版本。Web 编辑与读取本身不调用模型。


个人记忆增量：Ada 从工具实际读取并保存个人约束，用户随后从页面修订；Ada 下一次判断应读到新正文，Lin 的个人记忆保持为空，共同正文也不变。检查 `GET /api/agents/<id>/memory`、`GET /api/shared-memory`、实际模型请求和操作结果。200个表情按200字符计数且可保存，201字禁止保存；清空可保存。用户编辑草稿期间 Bot 先写入新版本，应展示新正文、保留草稿并禁止保存，核对后才能继续。改名及服务重启保留归属；缺失或删除的 Bot、失效连接不能写入。不得以自动化测试代替页面子路径，未实测的范围单独注明。

## Gotchas

2026-10-27 观察追加与前缀增量（Windows Chrome、隔离 home 与端口、真实 DeepSeek Flash）：从个人记忆页签和共同记忆设置保存正文，连续九轮对话验证共同更新、个人替换、一次真实 Compact、新窗口连续推进及清空。实际请求对照确认普通连续轮次和记忆更新保留完整历史前缀；新窗口仅重述起点最近两份观察，连续与清空后的请求继续保留该窗口前缀，未变化时不重复。启动 Skill 结果脱水、重启后结果引用变化与窗口切换分别记录，未冒称为记忆失稳。源码统一检查及 Bot 通信／Worker／Compact 进程检查通过。证据留在本地 `.tools/live/evidence/`；其他模型、其他系统、长期维护遗漏率及供应商缓存命中率未测。本轮未重复验证第二 Bot 页面隔离、并发草稿和字数边界，沿用对应自动化与原轮次页面记录。

2026-10-27 个人记忆原方案验证记录（以下独立加载路径已被观察追加方案替代，不作为当前前缀证明；Windows Chrome、隔离 home、真实 DeepSeek Flash）：个人记忆工具读取、合并与替换约束，页面编辑后的下一次判断直接读取新正文，第二名 Bot 的个人记忆保持为空；共同正文未被个人操作修改。页面200字符计数、201字禁止保存，以及 Bot 更新期间保留用户草稿、核对新版本后保存均通过；桌面与390×844资料抽屉可用。一次真实 compact 后，核对实际新窗口请求末尾包含当前两份正文，压缩请求与历史前言没有加载的记忆块，也没有新增记忆观察事件。工作约束／知识沉淀／任务进度分类样本通过且未写入。证据在任务 worktree 的 `.tools/memory-verify-evidence/`；清空、缺失身份、失效连接、改名和重启由自动化覆盖，未在页面逐项重跑；未验证其他系统页面、长期维护遗漏率或其他模型。

2026-10-24 起负责方向已移除；以下声明方向验证仅为旧版本历史，不是当前入口。本次隔离 Web、真实 DeepSeek 从页面调用 `list_colleagues`，返回名字、目录、项目、状态与活动时间，不再带方向；声明工具和保存接口已删除。源码组装的身份与工具说明对照留在 `.tools/remove-direction/evidence/context-audit.json`，没有将它冒称为真实 Compact 验证。本轮未重验共同记忆页面或越权写入。

2026-10-23 共同维护方向增量：真实模型调用 `declare_direction` 自行细化，用户随后在资料中改写；Lin 的真实 `list_colleagues` 与 Ada 不调用工具的回答均采用用户最新方向。页面和重启验证范围见 [Bot 名册](session-sidebar.md)。

- 方向由人和 Bot 共同维护；Bot 调整无需逐次认可，用户修改在上下文中明确标注来源。方向不构成操作授权，系统不解析对话里的「同意」。
- 同事目录只读，开启全部文件访问后也一样。命令不按路径区分读写，这条判据用 `write_file`，不用 `execute_command`。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：declare_direction 和 list_colleagues 真实调用通过，侧栏显示最新方向；越权写入和非法参数未重验。

2026-10-20 Bot 解耦增量：隔离 home、零代码项目、真实 deepseek/deepseek-flash，通过个人目录写入并读回、方向声明、私信往返、两名成员群聊发言。证据路径与完整日志保存在本地验证记录；未据此宣称其余子功能或完整功能地图已重验。

2026-10-22 共同记忆增量：隔离 Web、1440×1000、真实 deepseek-flash，通过 Ada 保存、Lin 读取修订、Ada 不再调用读取工具即回复最新正文；两名 Bot 的实际 Compact 新窗口均含当前正文。500/501字符、清空、旧版本冲突和并发修改由自动化测试覆盖，未在页面驱动这些错误路径；其他系统与完整功能地图未重验。证据留在本地 `.tools/verification/shared-memory/evidence/`。

2026-10-22 Web 编辑增量：隔离 home，通过500个表情保存、501字禁止保存、清空、草稿跨抽屉保留，以及两个页面先后保存时保留草稿和核对最新正文。数据库用户来源与版本递增一致；Ada 真实对话先看到用户正文，用户再次修改后，Ada 下一轮无需读取工具直接复述新版本。深浅主题与390×844窄屏可用。本次没有重复驱动 Compact 页面场景，两个对应真实进程测试通过。证据留在本地 `.tools/verification/settings/evidence/`。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：真实 declare_direction/list_colleagues，用户保存共同记忆，另一 Bot 下一轮读到新内容；超限、清空和冲突未重验。
