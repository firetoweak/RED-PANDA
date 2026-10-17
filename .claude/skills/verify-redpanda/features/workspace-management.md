# 工作区管理

用户在侧栏 `新建工作区` 登记一个本机目录作为 Agent 的任务根。来源：`web/src/features/sessions/CreateWorkspaceModal.tsx`、`RenameWorkspaceModal.tsx`、`RemoveWorkspaceModal.tsx`、`WorkspaceHeader.tsx`、`redpanda/filesystem/registry.py`、`app.py` 的 `/api/workspaces*`。

## Sub-features

- `create`：弹窗 `新建工作区`，字段 `名称`、`目录`（提示 `选择窗口在运行后端的电脑上打开，也可以直接填写该电脑上的目录路径`），按钮 `创建`。
- `pick-directory`：目录选择按钮调用 `POST /api/workspaces/select-directory`，由服务端弹本机系统对话框。这是 human-in-the-loop：人不选就一直等，不设超时，不算挂起问题。
- `rename`：工作区标题右侧 `<名称> 工作区操作`（右键标题同样打开）→ `重命名工作区` → 弹窗 `重命名工作区`、字段 `名称` → `PATCH /api/workspaces/{id}`，只改显示名，目录不变。
- `remove`：同一菜单 `移除工作区` → 弹窗 `移除工作区` → `移除` → `DELETE /api/workspaces/{id}`（204）。只从 `workspaces.json` 摘掉登记；目录与会话记录不删，但其下会话不再列出。
- `archive-all`：同一菜单 `归档所有会话`，逐个调用会话归档。
- `list`：`GET /api/workspaces`，侧栏按工作区分组列会话，组上有 `在 <名称> 新建会话`。

## How to get to it (user POV)

- 侧栏顶部 `新建工作区` 按钮（aria-label `新建工作区`）→ 填名称与目录 → `创建`。

## Driving it with the browser

- **创建**：名称 `verify`，目录填 `$SCRATCH_WORKSPACE`。
  - (a) `$REDPANDA_HOME/workspaces.json` 的 `workspaces` 数组新增一项，路径为解析后的绝对路径，`full_access` 为 `false`；`GET /api/workspaces` 同样包含它。
  - (b) `POST /api/workspaces` 返回 201；弹窗关闭，侧栏出现该工作区。
  - (c) `$SCRATCH_WORKSPACE` 里的文件不被改动、不新建文件。
  - (d) 错误路径①：目录填一个不存在的路径 → 400，弹窗显示 `创建失败`/`目录不存在，或者已经被另一个工作区占用。`，`workspaces.json` 不变。错误路径②：同一目录再建一次 → 409（`WorkspacePathTaken`），`workspaces.json` 仍只有一项。
- **目录选择**：点 `选择文件夹` → 选择窗口在运行后端的电脑上打开，等待用户选择不设固定超时，取消后按钮恢复可用。已知不可用（无桌面等）→ 503，弹窗显示 `文件夹选择失败，请查看服务端错误。`，可改为手填路径继续。
- **重命名**：(a) `GET /api/workspaces` 名称更新，`task_root` 不变。(d) 空白名称 → 422，名称不变。
- **移除**：(a) 该项从 `GET /api/workspaces` 与 `workspaces.json` 消失，目录仍在。(b) 204。(d) 再删一次 → 404。
- **新建会话**：点 `在 verify 新建会话` → 进入 `/workspaces/<id>` 草稿，(a) 此时会话列表不增加，发出首条消息后才增加。

## Gotchas

- Web 端创建时 `fullAccess` 写死为 `false`（`CreateWorkspaceModal.tsx`），界面上没有「整台电脑」选项；`full_access: true` 只能经 API 或其他入口登记。终端入口会打印 `工作区：整台电脑`。验证完全访问模式要明确记为「Web 不可达」。
- 移除不可撤销（界面无「恢复」）；同目录可重新 `新建工作区`。
- `workspaces.json` 被手改坏（重复 id、同路径两项）时服务应返回 400 `WORKSPACE_REGISTRY_ERROR`，而非崩溃。

## Live status
- 通过（r2）：冲突 409、坏路径 400；`select-directory` 有图形环境时拉起选择器并等待——符合 human-in-the-loop 设计
- 未验：UI 截图
- 2026-10-16（main@e191eab，仅 API，界面未验证）：坏路径 400、重复 409、重命名 200、空名 422、移除 204 且目录保留、重复移除 404
