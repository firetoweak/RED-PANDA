# 模型配置

`/settings/models` 页面管理候选模型、默认模型、推理程度与 compact 阈值，可测试连接；会话内可切换模型。来源：`ModelSettingsPage.tsx`、`ModelTestButton.tsx`、`ModelSelector.tsx`、`redpanda/model_settings.py`、`app.py` 的 `/api/model-settings*` 与 `/api/sessions/{id}/model`，说明文档 `docs/模型配置.md`。

## Sub-features

- `provider-badges`：`供应商连接` 区每个供应商显示 `已配置` / `待配置`（`缺少 <字段>`）。密钥只在 `connections.json` 里，页面不编辑密钥。
- `candidates`：`候选模型` 列表，`添加模型`/`编辑模型`/`删除`，字段 `供应商`、`模型名称`、`推理程度`（`供应商默认`/`低`/`中`/`高`）、`compact 触发值（输入 tokens）`；默认模型标 `默认模型 `。
- `save`：`保存配置` → `PUT /api/model-settings`，提示 `已保存。默认模型用于新会话；模型参数在下一次决策生效。`
- `test`：`测试` → `POST /api/model-settings/test`，结果 `测试通过` / `测试失败`。
- `session-model`：输入区 `选择模型` → `PUT /api/sessions/{id}/model`。

## How to get to it (user POV)

- 侧栏进入 `模型配置`（路由 `/settings/models`）；没有模型时首页提示 `先添加一个模型，再开始会话。`

## Driving it with the browser

- **徽标**：(a) 与 `$REDPANDA_HOME/connections.json` 实际字段一致：缺 key 的供应商显示 `待配置`。(d) 本地供应商（不需要 key）可能显示 `已配置`，属设计（PR #10 已记录）。
- **添加并保存**：添加 `deepseek` 的一个模型，`compact 触发值` 填 `64000`，`保存配置`。(a) `config.json` 出现该模型与阈值；`GET /api/model-settings` 一致。(b) PUT 2xx，显示已保存提示。(c) `connections.json` 字节不变（保存不能把密钥写进 `config.json`，也不能改动密钥）。(d) 模型名留空或阈值填 `0`/`-1`/`abc` → 显示 `请输入模型名称和正整数 compact 触发值。`，不发请求、文件不变；重复添加同名 → `候选列表已经包含这个模型。`。
- **删除默认模型**：(d) 直接删除当前默认模型并保存 → 409 `请先保存新的默认模型，再删除原默认模型`；删除被会话使用的模型 → 409 `请先切换以下会话的模型：…`，`config.json` 不变，页面显示 `保存失败`。
- **丢弃未保存**：添加候选后离开页面不保存 → `config.json` mtime 不变。
- **测试**：未保存的模型点 `测试` → `请先保存模型`；供应商未配置 → `请先填写供应商连接配置`；配置正确 → `测试通过`；把 key 改错 → `测试失败` 且响应不回显 key。(c) 测试不写任何文件。
- **会话切换模型**：在会话里 `选择模型` 换成另一候选。(a) `runtime_sessions/models.json` 中该会话记录为新模型；`GET /api/sessions/<id>/model` 一致；下一轮 journal 的模型字段为新模型。(c) 其他会话与默认模型不变。(d) PUT 一个不在候选里的模型 → 400。

## Gotchas

- 设置 `REDPANDA_CONFIG` 环境变量会让 `config.json` 指向别处（`redpanda/config.py`），验证时确认未设置。
- `测试` 需要网络与真实 key，`live_model_ready` 为 false 时只验错误路径。
