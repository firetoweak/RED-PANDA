# 输入框附件

在输入框添加图片或文件随消息发送。来源：`web/src/features/conversation/Composer.tsx`、`AttachmentTile.tsx`、`FileAttachmentTile.tsx`、`app.py` 的附件接口、`konden/channels/web/channel.py`、`konden/channels/web/directory_picker.py`、`konden/assistant/attachments.py`、`konden/assistant/file_attachments.py`。

## Sub-features

- `upload-image`：拖入/粘贴 PNG、JPEG、WebP、GIF → `POST .../attachments`（multipart，字段 `connection_id`、`file`），201，响应 `kind: "image"`、`name`、`size`、`attachment_id`（`sha256:<64位十六进制>`）、`mime`、`width`、`height`。
- `upload-file`：其他类型 → 同接口，201，响应 `kind: "file"`、`attachment_id`（`file:<32位十六进制>`）、`name`、`size`。
- `local-file`：输入框下方 `+` 图标按钮（可访问名称 `Add attachment`，悬停提示 `选择窗口在运行后端的电脑上打开，也可拖入或粘贴文件`）→ `POST .../attachments/select`；后端在运行后端的电脑上弹系统对话框（标题 `选择要分析的文件`），选中后直接挂入，浏览器不提交任何服务器路径。等待期间输入框里有一个名为 `本机文件` 的占位附件，选中后换成真实文件名与大小，取消则移除。
- `download`：消息里的附件可点开，`GET .../attachments/{attachment_id}`；文件附件是带下载文件名的链接，图片附件点开是大图预览。
- `retry-remove`：上传失败的图片块显示遮罩 `上传失败` 和按钮 `重试`（其 `title` 为后端 `detail`）；文件块在大小位置显示后端 `detail`（无 detail 时为 `上传失败`）和 `重试`；任一附件块都有 `Remove <名称>` 按钮。

## How to get to it (user POV)

- 打开任一会话，在输入框拖入文件、粘贴截图，或点输入框下方的 `+`（`Add attachment`）。

## Driving it with the browser

- **图片**：准备一张有效 PNG 并发送 `图里是什么颜色？`。可在本轮临时目录用 Pillow 生成 64×64 红图，再用 `Image.open(path).verify()` 校验；不要照抄未经校验的 base64。上传/下载判据核对实际宽高和字节，模型识色另记观察。
  - (a) `$KONDEN_HOME/runtime_sessions/<sha256(会话 id)>/.attachments/<64位十六进制>` 出现该图片；`GET .../attachments/<id>` 返回同字节、`Content-Type: image/png`，不带 `Content-Disposition` 文件名。
  - (b) 上传 201，响应 `width`/`height` 与原图一致，`name` 为原文件名。
  - (c) 工作区目录不出现该图片。
  - (d) 上传超过 20 MiB（`MAX_SOURCE_BYTES` = 20971520）的图片 → 400，`detail` 形如 `图片字节数 20971521 超过上限 20971520`（服务端只读到上限加一字节）；图片块显示 `上传失败` 遮罩，`重试` 的 `title` 是该 `detail`；`.attachments/` 下不留半截文件。只有响应里没有 `detail` 时界面才显示兜底文字 `上传失败，请重试或移除`。
- **普通文件**：拖入 `a.txt`。
  - (a) 存于 `.attachments/files/<32位十六进制>/a.txt`；下载回来逐字节一致，`Content-Type: application/octet-stream`，`Content-Disposition` 带 `filename` `a.txt`。
  - (b) 上传 201，响应 `name` 为 `a.txt`、`size` 为字节数。
  - (c) 工作区目录不出现该文件。
  - (d) 直接 `curl` 上传（探测用）：文件名为空 → 400 `缺少附件文件名`；文件名含路径分隔符或为 `..` 等 → 400 `附件名称必须是有效的单个文件名`。
- **本机选择**：
  - (a) 点 `Add attachment` → 后端电脑上弹出 `选择要分析的文件`；选中后输入框出现文件附件，名称与大小以后端返回为准；磁盘上只写一个指针文件 `.attachments/files/<32位十六进制>.mount`（内容为所选文件绝对路径），不复制原件。
  - (b) 取消 → 响应 `{"file": null}`，输入框不留 `本机文件` 占位，不报错。
  - (c) 所选原文件不被修改或移动。选择器已知不可用（无桌面等）→ 503，界面给出原因，可 `重试` 或改用拖入。
  - (d) 对话框返回的路径已不是现存文件（选中后立刻被删，时序难以手工触发）→ 400 `只能挂入本机已存在的文件`；触发不了就记为未覆盖，不用直接 POST 伪造路径（接口不接受路径）。`GET .../attachments/` 后接含 `..` 或其他非附件 id 格式的值 → 必须 400 `attachment id 格式无效` 或 404，不能返回文件内容。
- **跨会话与无效引用**：
  - (a) 会话 A 的附件 id 用会话 B 的路径下载 → 404 `附件不属于当前会话。`。
  - (b) `inputs` 的 `artifact_refs` 填格式不合法的 id → 422（请求校验，消息含 `artifact_refs 必须是有效的附件 id`）；填格式合法但不属于本会话的 id（如另一会话的附件）→ 400 `附件不属于当前会话`。
  - (c) 两种情况 journal 都不增加用户消息。
  - (d) 不存在 `附件不存在` 这条提示，不要按它判定。

## Gotchas

- 本机选择与目录选择一样依赖运行后端的电脑有可用桌面；已知不可用返回 503，未知异常仍原样暴露。等待用户选择不设固定超时。
- 浏览器不再提交任何服务器路径；后端只返回附件引用或取消结果。本机选择挂入的是指针，原文件之后被改动或删除会影响后续读取。
- `本机文件` 只是等待选择期间占位块的名字，不是按钮；按钮名称是 `Add attachment`。
- 已知产品缺口（2026-10-18 源码核对）：`Composer.tsx` 里有一个 `<input type="file" hidden multiple>`，但没有任何控件触发它，是死入口；浏览器原生选文件不可达，拖入与粘贴才是浏览器侧上传入口。
- 历史 live 记录：通过（r2）：上传/下载正常，坏 connection 409，`from-path` 已 405；回滚后附件同会话 API 仍 200 但 Web 不显示——不算问题；跨会话 404 通过。2026-10-18 的修订只做源码核对，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：有效 64×64 红色 PNG 经真实粘贴、发送、预览和 GET 下载通过，下载与原件逐字节相同；模型把红色说成白色，单记为识图观察。原先一次测试 PNG 的校验和错误已排除；本机选择、普通文件和跨会话边界未重验。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：有效 64×64 PNG 真实粘贴、发送、刷新预览通过，GET 下载与原件逐字节相同；本机选择、普通文件、跨会话边界未重验。
