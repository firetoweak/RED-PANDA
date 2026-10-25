# 普通回复与文件交付

长期 Agent 的普通回复直接给人看，无论由人、同事、群消息还是例行任务开始。实时与历史都使用普通回复，不折叠成工作记录。对同事和群发言仍需显式工具，普通结尾不自动回复唤醒者。来源：`konden/assistant/conversations.py`、`konden/assistant/host/supervisor.py`、`konden/channels/web/hub.py`、`web/src/features/conversation/`。

## Sub-features

- `reply`：所有普通正文按顺序完整展示，包括伴随工具调用的过程说明，可以引用与复制；过程说明采用透明底色，结尾答复保留浅气泡。附件随对应正文直接可下载。
- `live`：流式正文直接可见，工具调用出现、继续执行、收尾和刷新后仍在原位，不收进执行详情。工具明细收进与思考、时间同行的纯图标，不显示文字和次数；只由用户展开，展开时不重复正文和附件。思考单独折叠。
- `unread`：离开当前 Agent 后，它的新回复计入未读。
- `explicit-peers`：普通结尾不会自动成为同事私信或群发言。
- `file`：`send_file` 带工作区相对路径和说明，交付文件副本；普通回答无需调用工具。

## How to get to it (user POV)

同项目创建两个 Agent，例如 Ada 与 Lin。打开 Ada 的会话先发送普通提问，再让 Lin 用 `send_message` 找 Ada 处理事项。例行任务入口见 routines，群消息入口见 group-chat。

## Driving it with the browser

- **直接回答**：发 `请只回复 PING_OK`，确认普通回复出现；右键回复正文后选择 `复制消息`，刷新后正文相同。
- **过程正文**：要求 Bot 分步检查个人目录，每次检查前用一句话说明发现与下一步。观察流式文字在工具调用出现后仍直接可读；结尾出现后，各段正文仍按顺序保留。执行图标没有文字与次数，点击后可查看调用明细且正文不重复；执行继续时保持用户的展开选择。刷新后过程正文与结尾均保留，过程正文也可右键引用与复制。
- **协作触发**：让 Lin 私信 Ada。Ada 的收尾直接对人显示；只有调用 `send_message` 才能给 Lin 发消息。群里同样只由 `send_group_message` 产生发言。
- **实时与未读**：在 Ada 回复时看流式正文；停在 Lin 页面时，Ada 的新回复增加未读。刷新后全文仍可见。
- **文件**：请 Ada 用 `send_file` 交付 `note.txt`，核对附件下载、正文说明和刷新后保留。越出工作区或文件不存在时不出现成功的下载消息。
- **隔离**：普通会话继续向用户回复；SubAgent 普通文字送给父 Agent，不直接送给用户，也不结束协作。

## Gotchas

- 2026-10-26 过程正文增量：Windows 隔离实例、Chrome、真实 DeepSeek 两步只读操作，过程说明在工具调用、收尾和刷新后直接保留；纯图标展开明细、过程正文引用、明暗主题与 390px 窄屏通过。未重验其他操作系统、完整协作流程及真实写文件授权；证据 `.tools/conversation-context/evidence/reply-delivery/`。

- 实时事件与步骤不再携带 user/log 分类。不会依据触发来源改变回复去向。
- 无可用模型时跳过发送与工具步骤，按 Doctor 的 live_model_ready 记录。
- 原工作记录折叠的旧验证记录不代表当前规则通过；本轮证据另存本地验证目录。

2026-10-20 Windows 隔离实例 + deepseek-flash：PING 开场、后续普通问答、send_file 附件及刷新后保留、Lin 私信触发 Ada 的 PEER_OK 均通过；Journal 核对只有 Lin → Ada 一条私信，无自动回发。附件接口下载内容与工作区文件一致；浏览器下载事件捕获超时，未用该事件确认本机下载。流式与未读由前端回归验证，群与例行任务触发由进程测试验证，未等待真实定时触发。证据留在本地 .tools/verification/reply-delivery-20261020。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：普通结束正文、私信触发 PRIVATE_RECEIVED、定时触发 WOKE_UP 实时与刷新历史一致；后台启动也直接普通回复。send_file 和正常例行触发未重验。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：普通回复、私信与定时触发的结果可见，刷新后原文一致；正文右键复制消息的剪贴板为原文。send_file 未重验。
