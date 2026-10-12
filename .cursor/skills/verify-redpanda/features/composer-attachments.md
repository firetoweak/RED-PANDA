# 输入框附件

在输入框添加图片或文件随消息发送。来源：`Composer.tsx`、`AttachmentTile.tsx`、`FileAttachmentTile.tsx`、`app.py` 的附件接口、`redpanda/assistant/attachments.py`。

## Sub-features

- `upload-image`：拖入/粘贴 PNG、JPEG、WebP、GIF → `POST .../attachments`，返回 `kind: "image"` 与宽高。
- `upload-file`：其他类型 → 同接口，返回 `kind: "file"`。
- `local-file`：`本机文件` 按钮（`添加本机文件，也可拖入或粘贴`）→ `POST /api/files/select` 弹系统对话框 → `POST .../attachments/from-path`。
- `download`：消息里的附件可点开，`GET .../attachments/{attachment_id}`。
- `retry-remove`：上传失败显示 `上传失败` 与 `重试`。

## How to get to it (user POV)

- 打开任一会话，在输入框拖入文件、粘贴截图，或点 `本机文件`。

## Driving it with the browser

- **图片**：上传一张 1×1 PNG 并发送 `图里是什么颜色？`。(a) `runtime_sessions/<会话>/` 下出现该附件，`GET .../attachments/<id>` 返回同字节、`Content-Type: image/png`。(b) 上传 201，响应 `width`/`height` 为 1。(c) 工作区目录不出现该图片。(d) 上传超过 20 MiB（`MAX_SOURCE_BYTES`）的图片 → 400 带 `detail`，输入框显示 `上传失败，请重试或移除`，会话目录不留半截文件。
- **普通文件**：上传 `a.txt` → (a) 下载回来逐字节一致，响应带 `filename`。(d) 文件名为空 → 400 `缺少附件文件名`。
- **本机路径**：(d) `from-path` 传相对路径 → 400 `必须是本机绝对路径`；`GET .../attachments/../../config.json` 之类 → 必须 400 `attachment id 格式无效` 或 404，不能返回文件内容。
- **引用无效附件发消息**：`inputs` 的 `artifact_refs` 填伪造 id → 400 `附件不存在` 或 `artifact_refs 必须是有效附件 id`。

## Gotchas

- `本机文件` 与目录选择一样依赖服务端桌面；无桌面时预期 503。
- `from-path` 会读取服务端机器上任意绝对路径的文件。服务以 `--host 0.0.0.0` 局域网共享且没有鉴权时，这是读取服务端任意文件的口子（见报告「疑似问题」）。验证时记录是否有任何路径限制。

## Live status
- 失败（2026-10-12，main@1a4790a）
- 已确认问题（证据路径相对 `/workspace/verify-run-20261012`）：
  - 本机文件附件接口无鉴权，可读取服务器任意文件（严重）：`evidence/composer-attachments/results.json`
  - 无效连接 id 上传返回 500：`evidence/composer-attachments/results.json`
