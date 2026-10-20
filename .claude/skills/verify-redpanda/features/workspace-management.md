# 工作区管理

用户在侧栏 `新建工作区` 登记一个本机目录作为项目。来源：`web/src/features/sessions/CreateWorkspaceModal.tsx`、`RenameWorkspaceModal.tsx`、`RemoveWorkspaceModal.tsx`、`WorkspaceHeader.tsx`、`AgentSidebar.tsx`、`AgentHome.tsx`、`redpanda/filesystem/registry.py`、`redpanda/bootstrap.py`、`app.py` 的 `/api/workspaces*`。

## Sub-features

- `create`：弹窗 `新建工作区`，字段 `名称`（占位 `ai_charter`）、`目录`（占位 `请选择工作区文件夹`，提示 `选择窗口在运行后端的电脑上打开，也可以直接填写该电脑上的目录路径`），按钮 `创建`；名称或目录为空时 `创建` 不可点。失败提示 `创建失败`/`目录不存在，或者已经被另一个工作区占用。`。
- `pick-directory`：`选择文件夹` 按钮调用 `POST /api/workspaces/select-directory`，由服务端弹本机系统对话框（标题 `选择工作区文件夹`）。这是 human-in-the-loop：人不选就一直等，不设超时，不算挂起问题。选中后填入目录，名称为空时用目录名补上。失败提示 `选择失败`/`文件夹选择失败，请查看服务端错误。`。
- `rename`：工作区标题右侧 `<名称> 工作区操作`（右键标题同样打开）→ `重命名工作区` → 弹窗 `重命名工作区`、字段 `名称`（说明 `只修改显示名称，工作区目录保持不变。`）、按钮 `取消`/`保存` → `PATCH /api/workspaces/{id}`。失败提示 `重命名失败`/`未能保存名称，请重试。`。
- `remove`：同一菜单 `移除工作区` → 弹窗 `移除工作区`（说明工作区目录不会被删除，绑在上面的 Agent 不再列出，工作树和分支留在项目里，再次添加同一目录不会认回这些 Agent）→ `取消`/`移除` → `DELETE /api/workspaces/{id}`（204）。只从 `workspaces.json` 摘掉登记。失败提示 `移除失败`/`未能移除工作区，请重试。`。
- `list`：`GET /api/workspaces`，侧栏按工作区分组列 Agent；组标题上有 `在 <名称> 新建 Agent`（提示 `在此项目新建 Agent`）。工作台也按项目展示卡片；项目名称打开 `/workspaces/:workspaceId`，只显示该项目的 Agent 和群聊。
- `registry-load`：`workspaces.json` 只在服务启动时读一次（`bootstrap.py` 的 `WorkspaceRegistry.load`）。

## How to get to it (user POV)

- 侧栏「工作区」标题旁的图标按钮（aria-label `新建工作区`）→ 填名称与目录 → `创建`。
- 一个工作区都没有时，侧栏还会显示 `添加一个项目，再在里面新建 Agent。` 和文字按钮 `新建工作区`；主区域顶部有 `新建工作区`，空态另有 `创建工作区` 按钮（见 web-session-chat）。这些入口打开同一个弹窗。

## Driving it with the browser

- **创建**：名称 `verify`，目录填 `$SCRATCH_WORKSPACE`。
  - (a) `$REDPANDA_HOME/workspaces.json` 的 `workspaces` 数组新增一项，路径为解析后的绝对路径，`full_access` 为 `false`；`GET /api/workspaces` 同样包含它。
  - (b) `POST /api/workspaces` 返回 201；弹窗关闭，侧栏出现该工作区。
  - (c) `$SCRATCH_WORKSPACE` 里的文件不被改动、不新建文件。
  - (d) 错误路径①：目录填一个不存在的路径 → 400（`detail` 以 `工作区路径不是已存在的目录:` 开头），弹窗显示 `创建失败`/`目录不存在，或者已经被另一个工作区占用。`，`workspaces.json` 不变。错误路径②：同一目录再建一次 → 409，`workspaces.json` 仍只有一项。错误路径③（直接 `curl`，探测用）：名称为空串、目录有效 → 应是 4xx 且 `workspaces.json` 不变（当前源码不满足，见 Gotchas）。
- **目录选择**：点 `选择文件夹` → 选择窗口在运行后端的电脑上打开，等待用户选择不设固定超时；取消后返回 `{"directory": null}`，按钮恢复可用，字段不变。已知不可用（无桌面等）→ 503，弹窗显示 `选择失败`/`文件夹选择失败，请查看服务端错误。`，可改为手填路径继续。
- **重命名**：(a) `GET /api/workspaces` 名称更新，`task_root` 不变。(b) `PATCH` 返回 200，侧栏名称更新，弹窗关闭。(c) 目录与其下会话不变。(d) 空白名称（直接 `PATCH`）→ 422，名称不变；不存在的 id → 404。
- **移除**：(a) 该项从 `GET /api/workspaces` 与 `workspaces.json` 消失，目录仍在，`runtime_sessions/` 下会话目录仍在。(b) 204；正在看该工作区的会话时页面跳到另一个工作区或 `/`。(c) 其他工作区不变。(d) 再删一次 → 404。
- **注册表损坏**：停服务，把 `workspaces.json` 改坏（重复 id 或同一路径两项），再启动。(a) 启动失败，错误为 `WorkspaceRegistryError`（`工作区 id 不能重复` 或 `同一路径不能注册为多个工作区`），`workspaces.json` 不被改写。(c) 服务运行中手改 `workspaces.json` 不会被重新读取：`GET /api/workspaces` 保持内存中的登记，下一次创建/重命名/移除会用内存内容覆盖文件。

## Gotchas

- Web 端创建时 `fullAccess` 写死为 `false`（`CreateWorkspaceModal.tsx`），界面上没有「整台电脑」选项；`full_access: true` 只能经 API 或其他入口登记。终端入口会打印 `工作区：整台电脑`。验证完全访问模式要明确记为「Web 不可达」。
- 移除不可撤销（界面无「恢复」）；同目录可重新 `新建工作区`。
- `WorkspaceRegistryError` 的 HTTP 响应体只有 `{"detail": ...}`（400），没有 `code` 字段；运行期能走到它的是创建时目录不存在。注册表文件本身损坏只会在启动时暴露，不会以 HTTP 400 返回。
- 可访问名称冲突：没有工作区时，侧栏图标按钮、侧栏文字按钮和主区域顶部按钮都叫 `新建工作区`。可直接使用名称唯一的主区 `创建工作区`；验证其他入口时必须再限定所属区域与控件，不能只按 button 角色和名称定位。
- 已知产品缺口（2026-10-18 源码核对）：`POST /api/workspaces` 的 `WorkspaceCreateRequest.name` 没有非空校验，空名称由 `WorkspaceRecord` 抛 `ValueError`，`app.py` 无对应处理，预计返回 500 而不是 4xx。界面因 `创建` 在名称为空时不可点而走不到。判据保持 4xx 的预期。
- 历史 live 记录：通过（r2）：冲突 409、坏路径 400；`select-directory` 有图形环境时拉起选择器并等待——符合 human-in-the-loop 设计；UI 截图未验。2026-10-16（main@e191eab，仅 API，界面未验证）：坏路径 400、重复 409、重命名 200、空名 422、移除 204 且目录保留、重复移除 404。2026-10-18 的修订只做源码核对，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：从空首页创建临时 Git 工作区通过，侧栏与 GET 登记一致；目录选择、重命名、移除和错误路径未重验。
