---
name: verify-redpanda
description: "Drive RED PANDA (小熊猫) Web the way a user does — workspace, chat, tool authorization, and file rewind — and capture proof. Use when changing Web, sandbox file restore, model settings, or when a change needs a real-app check before it ships."
---

# Verify RED PANDA

Drive the Web app a person actually uses. The page is served by `web_chat.py`. Chat, tool calls, and file rewind also need a configured model and, for file tools, the native sandbox. This skill does not cover the terminal (`console_chat.py`) or ACP (`acp_chat.py`) entry points.

UI copy is Chinese. Match the accessible names below exactly. Set the browser viewport to at least 1280×800 before any click; below the `sm` breakpoint the sidebar is collapsed behind `打开会话列表`.

## Launch

One verification run owns one data directory, one port, and one process. Do not start it on `~/.redpanda` or on a port that already answers. Do not pass `--workspace`; the empty-home path is part of the map. Do not pass `--dev` for this port: Vite's proxy is fixed at `127.0.0.1:8765`, so an isolated port must serve the built files in `web/dist`.

From the repository root:

```sh
RUN_ID="$(date +%Y%m%d%H%M%S)"
RUN_DIR="/tmp/redpanda-verify-$RUN_ID"
export REDPANDA_HOME="$RUN_DIR/home"
SCRATCH_WORKSPACE="$RUN_DIR/workspace"
EVIDENCE_DIR="/tmp/redpanda-verify-evidence/$RUN_ID"
PORT=8876
export REDPANDA_VERIFY_URL="http://127.0.0.1:$PORT"
SESSION="redpanda-verify-$RUN_ID"
mkdir -p "$REDPANDA_HOME" "$SCRATCH_WORKSPACE" "$EVIDENCE_DIR"
printf 'verify\n' > "$SCRATCH_WORKSPACE/note.txt"
```

`REDPANDA_HOME` and `SCRATCH_WORKSPACE` are siblings. The personal directory must not sit inside the workspace directory.

Install once if `./redpanda-env/bin/python` or `web/dist/index.html` is missing. `sh scripts/setup.sh` needs `rg` and `fd` or `fdfind`, downloads the project Python 3.13 into `redpanda-env`, and initializes whichever personal directory `REDPANDA_HOME` points at. Existing `config.json` and `connections.json` there are left alone.

```sh
sh scripts/setup.sh
( cd web && npm ci && npm run build )
```

File-tool and rewind proofs also need the native sandbox. Linux needs `fusermount3` and a writable `/dev/fuse`. macOS has no file view; do not build or drive file rewind there.

```sh
./redpanda-env/bin/python scripts/build_sandbox.py
```

Pick another `PORT` if `8876` is already listening. Then start the server in its own tmux session and record the process id. `exec` keeps that id on `web_chat.py`.

```sh
tmux new-session -d -s "$SESSION" -c "$PWD" \
  "export REDPANDA_HOME='$REDPANDA_HOME'; echo \$\$ > '$RUN_DIR/web.pid'; exec ./redpanda-env/bin/python web_chat.py --host 127.0.0.1 --port $PORT > '$RUN_DIR/web.log' 2>&1"
```

Ready means `doctor.py` exits 0. The log line `Uvicorn running on http://127.0.0.1:$PORT` is the same moment, not a substitute for Doctor. If the log stops before that line, read `web.log` and do not drive the port.

Windows, from the repository root in PowerShell, uses the same variables. Build with `.\redpanda-env\Scripts\python.exe scripts/build_sandbox.py` after WinFsp is installed, then:

```powershell
$env:REDPANDA_HOME = $env:REDPANDA_HOME
$proc = Start-Process -FilePath .\redpanda-env\Scripts\python.exe -ArgumentList @('web_chat.py','--host','127.0.0.1','--port',$env:PORT) -PassThru -NoNewWindow -RedirectStandardOutput "$env:RUN_DIR\web.log" -RedirectStandardError "$env:RUN_DIR\web.err"
Set-Content -Path "$env:RUN_DIR\web.pid" -Value $proc.Id
```

## Doctor

Run this before the first click, after any surprising failure, and before claiming the instance is still the one you started.

```sh
./redpanda-env/bin/python .cursor/skills/verify-redpanda/doctor.py \
  --url "$REDPANDA_VERIFY_URL" \
  --home "$REDPANDA_HOME" \
  --pid-file "$RUN_DIR/web.pid"
```

Any Python 3.11+ can run it; the script uses only the standard library. Exit 0 means the pid is alive, the process is `web_chat.py` with this `REDPANDA_HOME`, `GET /` contains `<title>RED PANDA</title>`, `GET /api/workspaces` is a list, `GET /api/model-settings` points `connections_path` at `$REDPANDA_HOME/connections.json` and does not echo credentials, and `GET /api/events` emits `connected`. The JSON also reports `live_model_ready` and `sandbox.ready`.

`live_model_ready` is false when the default model's provider is `待配置`. That means the connection file has no usable setting. It does not mean the key is valid when true. **Skip chat send, the 测试 button, coding tool rounds, and file rewind when `live_model_ready` is false.** Say so in the proof note. Do not type a message anyway.

`sandbox.ready` is false on macOS, when `redpanda/sandbox/bin/redpanda-sandbox` is missing, or on Linux without `/dev/fuse` and `fusermount3`. **Skip coding tool rounds and file rewind in that case.** `python -m pytest -m process` is a separate contract check; it is not a substitute for those Web entries.

Add `--require-model` or `--require-sandbox` only when the feature you are about to drive cannot be skipped.

A non-zero exit means do not click. Wrong home, a dead pid, or a page that is not RED PANDA is a different instance.

## Drive

Use the browser automation in the session (Playwright or Chrome DevTools). The verbs below are the harness. There is no `browser` binary in this repository. Resolve each target by role and accessible name. Do not click by coordinates or DOM order.

- `browser open <url>`
- `browser click --role <role> --name "<accessible name>"`
- `browser fill --role textbox --name "<accessible name>" --value "<text>"`
- `browser press --key Enter`
- `browser snapshot --aria --path <file>`
- `browser screenshot --path <file>`

`curl` is only the read-only confirmation written next to an action. Posting to `/api/workspaces`, `/api/sessions`, or `/api/sessions/.../inputs` does not prove the page.

Read `features/README.md`, then the feature file. Drive the entry point that file names. A green `python -m pytest` or `python -m pytest -m process` run is not Web proof. Live model tests (`REDPANDA_RUN_LIVE_TESTS=1 python -m pytest tests/live`) call a real model and still are not the Web page.

Wait until the sidebar text is `已连接`. `正在连接…` means the event stream is not up; run Doctor again instead of typing.

## Evidence

Write proof under `$EVIDENCE_DIR/<feature-id>/`. Cleanup must not delete that directory.

For each driven entry point save:

- `snapshot.txt` — accessibility snapshot taken after the action, while the resulting state is on screen
- `page.png` — screenshot of that same state, with the sidebar word `RED PANDA` visible
- `state.json` — the read-only HTTP body named in the feature file
- `note.txt` — feature id, entry point, and either `passed` or `skipped` plus the Doctor field that blocked it (`live_model_ready` or `sandbox.ready`)

A skipped live-model or sandbox step is not a pass and not a pass via pytest. Capture the action and the result. For a workspace, the result is the sidebar control and `GET /api/workspaces`. For a chat send, the result is the user text in the transcript and `GET /api/sessions/<id>`. For a file rewind, the result is the shortened transcript and the workspace file contents on disk.

## Cleanup

Stop only the process you started, then delete the run directory. Leave `$EVIDENCE_DIR` in place.

```sh
pid="$(cat "$RUN_DIR/web.pid")"
tmux kill-session -t "$SESSION"
if kill -0 "$pid" 2>/dev/null; then kill "$pid"; fi
for _ in 1 2 3 4 5; do
  kill -0 "$pid" 2>/dev/null || break
  sleep 0.2
done
if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; fi
rm -rf "$RUN_DIR"
test -d "$EVIDENCE_DIR"
```

On Windows, `Stop-Process -Id <pid from web.pid>` and remove `$RUN_DIR` only. Do not stop every `python` or `web_chat.py`. After cleanup, the screenshot and `state.json` must still be in `$EVIDENCE_DIR`.

## Helpers

`doctor.py` is the only helper. It is read-only: four GETs, plus the pid and the sandbox binary path. Invocation is in Doctor. `--require-model` and `--require-sandbox` turn the matching warning into a blocker.

## Maintenance

When Web labels, routes, or these flows change, run `/maintain-verification-skill` and correct this map from the page you actually drove.
