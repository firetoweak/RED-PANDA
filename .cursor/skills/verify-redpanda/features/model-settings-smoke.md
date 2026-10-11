# Model settings smoke

The person opens `模型配置`, sees the candidate list and whether each provider is filled in, and can discard an unsaved candidate. The `测试` button is a live model call and is not part of this smoke.

## Sub-features

- `settings-open` shows the settings page from the sidebar.
- `settings-status` shows each provider as `已配置` or `待配置` and the personal connections path.
- `settings-draft` adds a candidate to the unsaved draft and then discards it.
- `settings-test` is the `测试 <model>` button. Skip it unless a live call is the thing under test.

## How to get to it (user POV)

- Choose `模型配置` in the sidebar footer.
- Read the heading `模型配置`, the candidate table, and the provider badges.
- Choose `添加模型`, fill the form, choose `加入待保存配置`, then choose `撤销修改` to drop the draft.
- Choose `测试 <model>` only to send a real short request.

## Driving it with the browser

Preconditions:

- Doctor exits 0 and the sidebar says `已连接`.
- Viewport is at least 1280×800.
- A fresh home still has the initial candidate `deepseek/deepseek-v4-pro`. If this home was given a different `config.json`, assert that file's default instead of the initial id.

- **Open settings.** Run `browser open $REDPANDA_VERIFY_URL/settings/models`. Or from the app shell run `browser click --role button --name "模型配置"`. The heading reads `模型配置`. The URL path is `/settings/models`.
- **Read status.** The table contains `deepseek/deepseek-v4-pro`. Each provider badge reads `已配置` or `待配置`. The monospace connections path equals Doctor's `connections_path`.
- **Draft a candidate.** Run `browser click --role button --name "添加模型"`. A dialog titled `添加模型` opens. Fill the textbox `模型名称` with `verify-smoke-model`. Run `browser click --role button --name "加入待保存配置"`. The table lists `deepseek/verify-smoke-model` and `保存配置` is enabled.
- **Discard it.** Run `browser click --role button --name "撤销修改"`. The extra row is gone.
- **Confirm nothing was saved.** Run `curl -fsS $REDPANDA_VERIFY_URL/api/model-settings`. `config.model.candidates` does not include `verify-smoke-model`, and the body contains no `api_key`.
- **Leave 测试 alone.** The button named `测试 deepseek/deepseek-v4-pro` is disabled while that provider badge is `待配置`. Do not click it for this smoke. Clicking it sends a real request and shows `测试通过` or `测试失败`.
- **Proof.** Snapshot and screenshot the page after discard, with the heading and the RED PANDA sidebar visible. Save the settings JSON under `$EVIDENCE_DIR/model-settings-smoke/state.json`.

## Gotchas

- The page polls settings about every 5 seconds. Assert the table after the discard click, not on a stale snapshot taken before it.
- `保存配置` writes the disposable home's `config.json`. This smoke stops at `撤销修改`.
- Deleting the current default is refused until another default is saved. Do not delete `deepseek/deepseek-v4-pro` here.
- `已配置` means the connection file has the required field. It does not mean the key or the local server works. vLLM and Ollama count as configured when the key is empty and `base_url` is set. `live_model_ready` follows the default model, not those badges.
- Provider badges and the connections path are visible without a key. Do not copy `connections.json` into the evidence directory.
