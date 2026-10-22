# 可选代码项目

代码项目是 Bot 可挂载的资源。首页 `添加代码项目` 只登记本机目录；创建 Bot 时可选关联，之后在 Bot 资料里 `关联项目`（已登记项目或本地仓库路径，后者原地登记）或 `解除`；Bot 也能用 `link_project` 自己挂上。一位 Bot 可挂多个项目，工作树在其个人目录的 `projects/<项目名>`。接口：`POST /api/agents/{id}/projects`（`workspace_id` 或 `path` 二选一）、`POST /api/agents/{id}/projects/{name}/unlink`。来源：`AgentHome.tsx`、`CreateWorkspaceModal.tsx`、`CreateAgentModal.tsx`、`registry.py` 与 `/api/workspaces*`。

## Sub-features

- `create`：首页或 `新建 Bot` 中的 `添加代码项目` 打开弹窗，字段 `名称`、`目录`，按钮 `创建`；空字段不能提交。目录可手填，也可 `选择文件夹`，选择窗口在后端所在电脑上打开。从新建 Bot 进入时，创建后自动选中新项目，取消时返回原表单，均保留 Bot 名字。
- `list`：`GET /api/workspaces` 提供创建 Bot 与 `关联项目` 的可选项目列表；不再生成侧栏分区或项目页。
- `link`：Bot 资料 `关联项目` 选已登记项目或填本地仓库路径，`关联`。(a) `GET /api/agents` 该 Bot 的 `projects` 多一项（`name`、`workspace_id`、`directory`），`directory` 在其个人目录 `projects/` 下且为该项目分支的 worktree；填路径时 `GET /api/workspaces` 多一条原地登记。(b) 返回 200，弹窗关闭，资料与页头列出项目名。(c) 人检出目录不变；Bot 时间线多一条 `bot.project_linked`，不请求决策。(d) 再挂同一项目 409 `这个项目已经挂在这里。`；非 Git 或无提交路径 409，且不新增登记。
- `unlink`：资料里项目旁 `解除`。(a) 工作树干净且已全部落地时，该项从 `projects` 消失，worktree 与分支删除。(b) 有未提交改动或未落地提交时 409 `解除会丢掉这个项目里还没落地的工作，没有解除。`，资料显示该提示。(c) 拒绝时工作树与分支原样保留。(d) 不影响同一项目的其他 Bot。
- `registry`：注册表在启动时读取，运行中的创建、重命名和移除经内存快照发布。重命名、移除 API 仍保留，当前前端没有这些操作入口。移除登记不删除 Bot 身份或其目录。

## Driving it with the browser

- **登记**：(a) 名称 verify、目录 `$SCRATCH_WORKSPACE`，`GET /api/workspaces` 出现解析后的路径。(b) POST 为 201，弹窗关闭，新建 Bot 可选此项目。(c) 登记本身不修改该目录内容。(d) 不存在目录返回 400，重复目录 409，弹窗显示失败。
- **目录选择**：(a) 选中后填入路径，名称为空则补目录名。(b) 取消不改字段。(c) 不登记项目。(d) 无桌面等不可用情况返回 503，可以手填路径。选择器等待人操作不设超时。
- **新建 Bot 内添加**：先填 Bot 名字，再添加代码项目；成功后返回新建 Bot，项目已选中、名字保留，`GET /api/workspaces` 有新增项，`GET /api/agents` 此时仍无新 Bot。取消添加也保留名字与原项目选择。
- **仅 API 的管理**：(a) PATCH 改显示名、DELETE 移除登记。(b) 成功分别 200、204。(c) Bot 仍在名册，个人目录和代码工作树保留。(d) 不存在 id 为 404；空白名称为 422。
- **坏注册表**：(a) 重复 id 或重复路径令启动失败。(b) 明确暴露 `WorkspaceRegistryError`。(c) 不改写坏文件。(d) 不能把损坏降级为空列表。

## 验证范围

Web 不提供 `full_access` 开关。本次 live 聚焦无项目路径，代码项目登记、选择器及管理 API 沿用既有测试，未重新执行完整 UI 验证。历史按工作区分区、重命名/移除菜单和 `/workspaces/:id` 的截图不再适用。

2026-10-22 增量：Windows 隔离 Web 实测从新建 Bot 添加项目（手填目录）、自动选中与名字保留通过，1440 / 390 宽度弹窗可用。原生选择进程启动后未出现可供自动化操作的窗口，系统窗口选取与取消未验证；隔离实例未配置默认模型，Bot 最终创建被配置校验拒绝，未验证真实模型流程。
