# 输入框附件

在输入框添加图片或文件随消息发送。来源：`Composer.tsx`、`AttachmentTile.tsx`、`FileAttachmentTile.tsx`、`app.py` 的附件接口、`redpanda/assistant/attachments.py`。

## Sub-features

- `upload-image`：拖入/粘贴 PNG、JPEG、WebP、GIF → `POST .../attachments`，返回 `kind: "image"` 与宽高。
- `upload-file`：其他类型 → 同接口，返回 `kind: "file"`。
- `local-file`：`本机文件` 按钮 → `POST .../attachments/select`，由后端在运行后端的电脑上弹系统对话框并直接挂入；浏览器不提交任何服务器路径。
- `download`：消息里的附件可点开，`GET .../attachments/{attachment_id}`。
- `retry-remove`：上传失败显示 `上传失败` 与 `重试`。

## How to get to it (user POV)

- 打开任一会话，在输入框拖入文件、粘贴截图，或点 `本机文件`。

## Driving it with the browser

- **图片**：上传一张 1×1 PNG 并发送 `图里是什么颜色？`。(a) `runtime_sessions/<会话>/` 下出现该附件，`GET .../attachments/<id>` 返回同字节、`Content-Type: image/png`。(b) 上传 201，响应 `width`/`height` 为 1。(c) 工作区目录不出现该图片。(d) 上传超过 20 MiB（`MAX_SOURCE_BYTES`）的图片 → 400 带 `detail`，输入框显示 `上传失败，请重试或移除`，会话目录不留半截文件。
- **普通文件**：上传 `a.txt` → (a) 下载回来逐字节一致，响应带 `filename`。(d) 文件名为空 → 400 `缺少附件文件名`。
- **本机选择**：(a) 点 `本机文件` → 后端电脑上弹出系统对话框；选中后输入框出现文件附件，名称与大小以后端返回为准。(b) 取消 → 输入框不留该附件，不报错。(c) 选择器已知不可用（无桌面等）→ 503，界面给出原因，可重试或改用拖入。(d) `GET .../attachments/../../config.json` 之类 → 必须 400 `attachment id 格式无效` 或 404，不能返回文件内容。
- **引用无效附件发消息**：`inputs` 的 `artifact_refs` 填伪造 id → 400 `附件不存在` 或 `artifact_refs 必须是有效附件 id`。

## Gotchas

- 本机选择与目录选择一样依赖运行后端的电脑有可用桌面；已知不可用返回 503，未知异常仍原样暴露。等待用户选择不设固定超时。
- 浏览器不再提交任何服务器路径；后端只返回附件引用或取消结果。

## Live status
- 部分（2026-10-12，修复后）：任意路径入口（`POST /api/files/select`、`.../attachments/from-path`）已删除；上传、下载、失效连接 409 由隔离 API 验证通过（16/16，见 `.tools/verification/fixapi-20261012/`）
- 未验（待真实 UI）：系统对话框实际选择、取消、再次打开，以及失败后的重试交互
