# 可选代码项目

代码项目是 Bot 的可选编码场景。首页 `添加代码项目` 只登记本机目录，之后创建 Bot 时可关联；不再是进入聊天的前置条件。来源：`AgentHome.tsx`、`CreateWorkspaceModal.tsx`、`CreateAgentModal.tsx`、`registry.py` 与 `/api/workspaces*`。

## Sub-features

- `create`：`添加代码项目` 弹窗，字段 `名称`、`目录`，按钮 `创建`；空字段不能提交。目录可手填，也可 `选择文件夹`，选择窗口在后端所在电脑上打开。
- `list`：`GET /api/workspaces` 提供创建 Bot 的可选项目列表；不再生成侧栏分区或项目页。
- `registry`：注册表在启动时读取，运行中的创建、重命名和移除经内存快照发布。重命名、移除 API 仍保留，当前前端没有这些操作入口。移除登记不删除 Bot 身份或其目录。

## Driving it with the browser

- **登记**：(a) 名称 verify、目录 `$SCRATCH_WORKSPACE`，`GET /api/workspaces` 出现解析后的路径。(b) POST 为 201，弹窗关闭，新建 Bot 可选此项目。(c) 登记本身不修改该目录内容。(d) 不存在目录返回 400，重复目录 409，弹窗显示失败。
- **目录选择**：(a) 选中后填入路径，名称为空则补目录名。(b) 取消不改字段。(c) 不登记项目。(d) 无桌面等不可用情况返回 503，可以手填路径。选择器等待人操作不设超时。
- **仅 API 的管理**：(a) PATCH 改显示名、DELETE 移除登记。(b) 成功分别 200、204。(c) Bot 仍在名册，个人目录和代码工作树保留。(d) 不存在 id 为 404；空白名称为 422。
- **坏注册表**：(a) 重复 id 或重复路径令启动失败。(b) 明确暴露 `WorkspaceRegistryError`。(c) 不改写坏文件。(d) 不能把损坏降级为空列表。

## 验证范围

Web 不提供 `full_access` 开关。本次 live 聚焦无项目路径，代码项目登记、选择器及管理 API 沿用既有测试，未重新执行完整 UI 验证。历史按工作区分区、重命名/移除菜单和 `/workspaces/:id` 的截图不再适用。
