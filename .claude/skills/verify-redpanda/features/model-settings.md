# 模型配置

侧栏 `设置 → 模型配置` 抽屉管理候选模型、默认模型、推理程度与 compact 阈值，可测试连接；会话内可切换模型。来源：`web/src/features/models/ModelSettings.tsx`、`ModelTestButton.tsx`、`web/src/features/models/ModelSelector.tsx`、`redpanda/model_settings.py`、`redpanda/config.py`（候选与默认的形状校验）、`redpanda/llm/config.py`（`ModelConfig`、初始 `connections.json`）、`thinllm/providers.py`（`PROVIDERS`）、`redpanda/assistant/model_connection.py`、`redpanda/channels/web/app.py` 的 `/api/model-settings*` 与 `/api/sessions/{id}/model`，说明文档 `docs/模型配置.md`。

## Sub-features

- `provider-badges`：`供应商连接` 区按 `PROVIDERS` 顺序列出 `deepseek`、`openai`、`qwen`、`stepfun`、`bigmodel`、`vllm`、`ollama`，每个徽标 `<provider> · 已配置` 或 `<provider> · 缺少 <missing>`。托管供应商缺的是 `api_key`；本地供应商（`vllm`、`ollama`）不要求 key，缺的是 `base_url`。密钥只在 `connections.json` 里，页面不编辑密钥。
- `candidates`：`候选模型` 表，列 `新会话默认`（Radio，`aria-label` 为 `默认模型 <provider/model>`）、`模型`、`推理程度`、`compact 触发值`、`连接`（`已配置` / `待配置`，`待配置` 只出现在这一列）、`操作`（`测试`、`编辑`、`删除`）。页首 `添加模型`；弹窗标题 `添加模型` / `编辑模型`，字段 `供应商`、`模型名称`、`compact 触发值（输入 tokens）`（最小 1，新模型默认 200000），只有供应商 `stepfun` 且模型名称 `step-5-preview` 时才出现 `推理程度`（`供应商默认`/`低`/`中`/`高`）。弹窗确认按钮 `加入待保存配置`，只改草稿。
- `save`：`保存配置` → `PUT /api/model-settings`，成功提示 `已保存。默认模型用于新会话；模型参数在下一次决策生效。`，失败显示 `保存失败` Alert；`撤销修改` 把草稿恢复为已保存配置。两个按钮在草稿与已保存一致时禁用。
- `test`：每行按钮 `测试`（`aria-label` 为 `测试 <provider/model>`）→ `POST /api/model-settings/test`，结果 `测试通过 · N ms` 或 `测试失败 · N ms` 加一行失败原因。
- `session-model`：会话输入区按钮（`aria-label` 为 `切换会话模型：<去掉 provider 的模型名>`，未选时 `切换会话模型：选择模型`）→ 菜单选候选 → `PUT /api/sessions/{id}/model`。

## How to get to it (user POV)

- 侧栏底部点 `设置`，菜单选择 `模型配置`；打开右侧抽屉，聊天路由保持不变。没有候选模型时，设置页 `候选模型` 区显示 `先添加一个模型，再开始会话。`（首页没有这句）。

## Driving it with the browser

- **徽标**：
  - (a) 与 `$REDPANDA_HOME/connections.json` 一致：托管供应商 `api_key` 为空 → `<provider> · 缺少 api_key`；`GET /api/model-settings` 的 `providers[]` 里 `configured`/`missing` 对应。
  - (b) 候选表 `连接` 列与该供应商徽标一致（`已配置` / `待配置`）。
  - (c) 读页面不写 `connections.json`（已存在时字节不变）。
  - (d) 把 `vllm` 的 `base_url` 改成空字符串 → `vllm · 缺少 base_url`。初始 `connections.json` 已为 `vllm`/`ollama` 填好本机 `base_url`，所以它们默认显示 `已配置`，属设计（PR #10 已记录）。
- **添加并保存**：`添加模型`，`供应商` 选 `deepseek`，填模型名称，`compact 触发值（输入 tokens）` 填 `64000`，`加入待保存配置`，再 `保存配置`。
  - (a) `config.json` 出现该模型与阈值；`GET /api/model-settings` 一致。第一个候选会自动成为默认（Radio 选中）。
  - (b) PUT 200，显示已保存提示。
  - (c) `connections.json` 字节不变（保存不能把密钥写进 `config.json`，也不能改动密钥）；点 `加入待保存配置` 后、`保存配置` 前 `config.json` 不变。
  - (d) 模型名留空或阈值填 `0` → 弹窗内 `请输入模型名称和正整数 compact 触发值。`，不发请求、文件不变；重复添加同名 → `候选列表已经包含这个模型。`。形状错误直接 PUT（如候选重复、有候选但 `default` 为 `null`）→ 400，`detail` 为 `候选模型不能重复` / `有候选模型时必须选择默认模型`；过期的 `connection_id` → 409 `Web 连接已失效，请重新连接页面。`。
- **删除默认模型**：
  - (a) 默认模型那一行的 `删除` 是禁用的，UI 不能直接删。
  - (d) 要走到服务端拒绝：在草稿里先把 `新会话默认` 改到另一个候选，再删原默认模型，然后 `保存配置` → 409 `请先保存新的默认模型，再删除原默认模型`，页面 `保存失败`，`config.json` 不变。删除仍被某个会话选用的模型并保存 → 409 `请先切换以下会话的模型：<session_id 列表>`，`config.json` 不变。
- **草稿**：添加候选后关闭抽屉，打开 `共同记忆` 再返回 `模型配置`，候选草稿仍在；没有点 `保存配置` 时 `config.json` 不变。点 `撤销修改` 才恢复已保存配置；刷新页面会丢弃草稿。
- **测试**：
  - (a) 测试不写任何文件。
  - (b) 已保存且供应商已配置 → `测试通过 · N ms`；把 key 改错 → `测试失败 · N ms` 加原因，响应不回显 key。测试失败时 HTTP 仍是 200（`ok: false`）。
  - (c) 未保存的新模型或供应商未配置时按钮禁用，`请先保存模型` / `请先填写供应商连接配置` 只是悬停 `title`，不会显示为结果。
  - (d) 直接 POST 一个不在候选里的模型 → 400 `模型不在候选列表中：<model>`；请求本身失败时按钮下显示 `测试请求失败：…`。
- **会话切换模型**：在会话里点 `切换会话模型：…` 换成另一候选。
  - (a) `runtime_sessions/models.json` 中该会话记录为新模型；`GET /api/sessions/<id>/model` 的 `selected.model` 为新模型名，`selected` / `effective` 是完整模型配置对象而非字符串。参数尚未应用时 `pending` 为 true，下一次决策后 `effective` 等于 `selected`、`pending` 变 false。
  - (b) 按钮文字变为新模型名（不带 provider）。
  - (c) 其他会话与默认模型不变。
  - (d) 供应商未配置的候选在菜单里禁用；直接 PUT 它 → 400 `<provider> 尚未配置 <field>`；PUT 不在候选里的模型 → 400 `模型不在候选列表中：<model>`。

## Gotchas

- 设置 `REDPANDA_CONFIG` 环境变量会让 `config.json` 指向别处（`redpanda/config.py`），验证时确认未设置。
- `测试` 需要可用连接，`live_model_ready` 为 false 时跳过 `测试` 与发送，只验证配置编辑和禁用状态。
- `推理程度` 只对 `stepfun/step-5-preview` 开放；其他模型该列显示 `—`，服务端对其他模型传 `reasoning_effort` 直接拒绝。
- 抽屉打开时每 5 秒轮询 `GET /api/model-settings`；草稿与旧的已保存配置一致时会被新数据覆盖，有未保存修改时保留草稿。
- 历史验证：2026-10-12（main@1a4790a）通过。2026-10-18 按源码改写按钮文案、徽标与错误路径，未 live 复跑。

- 2026-10-20 维护实测（`origin/main@32860f6`，Windows Chrome/ConPTY，隔离 home）：UI 添加 deepseek-flash、阈值 64000、保存和真实连接测试通过（1209 ms），未保存时测试禁用；删除默认与会话换模型未重验。

2026-10-22 设置抽屉增量：桌面1440×1000与窄屏390×844，通过菜单进入、模型编辑弹窗、阈值草稿跨抽屉保留、撤销和保存；API 确认保存后的阈值为64000。候选表在窄屏内部横向滚动，页面无横向溢出。真实连接测试收到模型空响应，页面原样呈现失败原因；同一模型的真实 Bot 对话成功。删除默认与会话切换模型未重验。证据留在本地 `.tools/verification/settings/evidence/`。

- 2026-10-22 全量维护（`origin/main@4c000ca`，Windows Chrome/ConPTY，隔离 home、真实 DeepSeek）：设置抽屉编辑当前模型阈值，加入待保存配置并保存，GET 确认生效；连接测试、模型切换和表单边界未重验。
