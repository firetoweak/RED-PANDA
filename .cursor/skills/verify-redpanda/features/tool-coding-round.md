# Tool coding round

In a session, the person turns on `自动放行写文件等工具`, asks for a file edit, and sees `read_file`, `apply_patch`, and `execute_command` finish in the timeline. The scratch file changes on disk.

## Sub-features

- `auto-authorize-on` enables the switch before the prompt.
- `authorize-prompt` shows `等待授权` with `允许` and `拒绝` when the switch stays off. Drive this only as the explicit off-path, not together with the on-path in one turn.
- `coding-tools` runs `read_file`, then `apply_patch`, then `execute_command` from one composer message.
- `file-side-effect` leaves the scratch file changed.

Skip `authorize-prompt`, `coding-tools`, and `file-side-effect` unless Doctor reports both `live_model_ready` and `sandbox.ready`.

## How to get to it (user POV)

- Open a session from [Web session and chat](./web-session-chat.md). The composer is visible.
- Turn on the switch named `自动放行写文件等工具`.
- Type a request that names the three tools, then choose `发送`.
- Watch the timeline for those tool names. If the switch was left off, a dialog titled `等待授权` asks for `允许` or `拒绝`.

## Driving it with the browser

Preconditions:

- `web-session-chat` has created `verify-workspace` and a session is open.
- `$SCRATCH_WORKSPACE/note.txt` exists and its text is `verify`.
- Doctor still exits 0. `live_model_ready` and `sandbox.ready` are true. If either is false, snapshot the composer, write the false field in the note, and stop.

- **Turn the switch on.** Run `browser click --role switch --name "自动放行写文件等工具"`. The switch is checked.
- **Confirm the flag.** Run `curl -fsS $REDPANDA_VERIFY_URL/api/sessions/<session id>`. `session.auto_authorize` is true.
- **Ask for the edit.** Fill `消息` with this text and choose `发送`:

```text
请只做这三件事，不要委派，不要安装依赖。
1. 用 read_file 读取 note.txt。
2. 用 apply_patch 把文件内容改成 edited。
3. 用 execute_command 执行 printf '%s\n' edited，workspace_effect 用 may_write。
最后回复 CODING_ROUND_DONE。
```

- **Watch the tools.** The timeline shows monospace names `read_file`, `apply_patch`, and `execute_command`. The dialog `等待授权` does not appear. Wait until the assistant text includes `CODING_ROUND_DONE` or a tool error is visible. A sandbox error is a failed round, not a pass.
- **Confirm the file.** Read `$SCRATCH_WORKSPACE/note.txt`. After the on-path round its contents are `edited`. Run `curl -fsS $REDPANDA_VERIFY_URL/api/sessions/<session id>` and keep the body. `items` includes steps whose `tools` name those three tools.
- **Proof.** Save the snapshot and screenshot while the three tool names are visible. Save the conversation JSON and a copy of `note.txt` under `$EVIDENCE_DIR/tool-coding-round/`.
- **Off-path, only after that proof.** Do this in a new session, not in the session file rewind will use. Choose `在 verify-workspace 新建会话`, leave `自动放行写文件等工具` off, send `请 read_file 读取 note.txt，然后停止。`, and run `browser click --role button --name "允许"` in the dialog titled `等待授权`. Do not also click the inline `允许` on the tool card in the same moment.

## Gotchas

- The switch is per session. A new session starts off.
- `发送` stays disabled when the provider is not configured. Do not POST the input.
- macOS has no file view. A missing `redpanda-sandbox` binary or a missing `fusermount3` makes the file tools fail in the timeline; Doctor should already have skipped the round.
- `pytest -m process` can exercise the same tools without Web. Record it as a process test, not as this feature.
- A read-only command does not publish a file change. The `read_file` step can still show `从这一步之后重开`; its saved files are the bytes from before `apply_patch`. [File rewind](./file-rewind.md) uses that row.
- The model may still refuse or call a different tool. The pass condition is those three names in order and `note.txt` equal to `edited`, not the mere presence of a reply.
