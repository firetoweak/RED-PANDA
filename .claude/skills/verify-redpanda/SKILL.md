---
name: verify-redpanda
description: "Drive RED PANDA (小熊猫) Web the way a user does — workspace, Agent roster and chat, colleague tools, private messages, landing and sync, group chat, tool authorization, and Bot renaming — and capture proof. Use when changing Web, Bot names, model settings, or when a change needs a real-app check before it ships."
---

# Verify RED PANDA

Drive the Web app a person actually uses. The page is served by `web_chat.py`. Chat and tool calls also need a configured model. Web sessions are persistent Bots with their own ordinary directories under `$REDPANDA_HOME/state/bots/`. Create and chat without registering a project. Optionally link a code project at creation or from Bot details. That path needs a Git repository with a first commit; the Bot project worktree lives in its personal directory under `projects/<name>/`. Integration and child worktrees live under `$REDPANDA_HOME/state/coding_bot/`. The terminal entry point is covered by `features/console-chat.md`; use a separate disposable home for each terminal drive, never the Web home.

UI copy is Chinese. Match the accessible names below exactly. Set the browser viewport to at least 1280×800 before any click; below the `sm` breakpoint the sidebar is collapsed behind `打开 Agent 名册`.

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
git -C "$SCRATCH_WORKSPACE" init -q
git -C "$SCRATCH_WORKSPACE" add note.txt
git -C "$SCRATCH_WORKSPACE" -c user.name=verify -c user.email=verify@example.invalid commit -qm init
```

`REDPANDA_HOME` and `SCRATCH_WORKSPACE` are siblings. The personal directory must not sit inside the workspace directory.

Install once if `./redpanda-env/bin/python` or `web/dist/index.html` is missing. Re-run setup after updates that change browser dependencies. `sh scripts/setup.sh` (Linux x64 only; network required) downloads the project Python into `redpanda-env`, then runs `scripts/setup.py` in four steps: it prepares pinned application tools and a private Chrome for Testing under `.tools/runtime/`, installs Python deps, runs `npm ci` in `scripts/browser/` and `web/` and builds the web bundle, and initializes whichever personal directory `REDPANDA_HOME` points at. Existing `config.json` and `connections.json` there are left alone. `setup.py --check` checks system command prerequisites and rejects Node outside 22.x (≥22.13) or 24.x; it does not prove the browser MCP dependencies are installed. If only the web bundle is stale, rebuilding it alone is enough:

```sh
sh scripts/setup.sh
( cd web && npm ci && npm run build )
```

Pick another `PORT` if `8876` is already listening. Then start the server in its own tmux session and record the process id. `exec` keeps that id on `web_chat.py`.

```sh
tmux new-session -d -s "$SESSION" -c "$PWD" \
  "export REDPANDA_HOME='$REDPANDA_HOME'; echo \$\$ > '$RUN_DIR/web.pid'; exec ./redpanda-env/bin/python web_chat.py --host 127.0.0.1 --port $PORT > '$RUN_DIR/web.log' 2>&1"
```

Ready means `doctor.py` exits 0. The log line `Uvicorn running on http://127.0.0.1:$PORT` is the same moment, not a substitute for Doctor. If the log stops before that line, read `web.log` and do not drive the port.

### Windows

From the repository root in PowerShell. Install once with `.\scripts\setup.ps1`: it installs the pinned tools (portable Git when missing), Python deps, and builds `web\dist`.

Set the run variables as process environment, so the server started below inherits them:

```powershell
$RunId = Get-Date -Format yyyyMMddHHmmss
$env:RUN_DIR = "$env:TEMP\redpanda-verify-$RunId"
$env:REDPANDA_HOME = "$env:RUN_DIR\home"
$env:SCRATCH_WORKSPACE = "$env:RUN_DIR\workspace"
$env:EVIDENCE_DIR = "$env:TEMP\redpanda-verify-evidence\$RunId"
$env:PORT = "8876"
$env:REDPANDA_VERIFY_URL = "http://127.0.0.1:$env:PORT"
New-Item -ItemType Directory -Force $env:REDPANDA_HOME, $env:SCRATCH_WORKSPACE, $env:EVIDENCE_DIR | Out-Null
Set-Content -Path "$env:SCRATCH_WORKSPACE\note.txt" -Value verify
git -C $env:SCRATCH_WORKSPACE init -q
git -C $env:SCRATCH_WORKSPACE add note.txt
git -C $env:SCRATCH_WORKSPACE -c user.name=verify -c user.email=verify@example.invalid commit -qm init
$proc = Start-Process -FilePath .\redpanda-env\Scripts\python.exe -ArgumentList @('web_chat.py','--host','127.0.0.1','--port',$env:PORT) -PassThru -WindowStyle Hidden -RedirectStandardOutput "$env:RUN_DIR\web.log" -RedirectStandardError "$env:RUN_DIR\web.err"
Set-Content -Path "$env:RUN_DIR\web.pid" -Value $proc.Id
```

`-WindowStyle Hidden` gives the server its own console. Do not use `-NoNewWindow`: a server that shares your console receives your Ctrl+C.

### Terminal session

For `console-chat`, create a fresh `$RUN_DIR/tui-home` and copy only the needed model configuration into it. Run `./redpanda-env/bin/python console_chat.py --home "$RUN_DIR/tui-home" --workspace "$SCRATCH_WORKSPACE"` in a real interactive terminal; on Windows use `.\redpanda-env\Scripts\python.exe`. Do not run two CLIs against the same home. Before typing, confirm the process command line, the isolated home, and its startup session id. The Web Doctor does not health-check a TUI; verify its prompt and successful model reply in the terminal itself. Exit with Ctrl+D and confirm process exit.

## Doctor

Run this before the first click, after any surprising failure, and before claiming the instance is still the one you started.

```sh
./redpanda-env/bin/python .claude/skills/verify-redpanda/doctor.py \
  --url "$REDPANDA_VERIFY_URL" \
  --home "$REDPANDA_HOME" \
  --pid-file "$RUN_DIR/web.pid"
```

```powershell
.\redpanda-env\Scripts\python.exe .claude\skills\verify-redpanda\doctor.py --url $env:REDPANDA_VERIFY_URL --home $env:REDPANDA_HOME --pid-file "$env:RUN_DIR\web.pid"
```

Any Python 3.11+ can run it; the script uses only the standard library. Exit 0 means the pid is alive, the process is `web_chat.py` with this `REDPANDA_HOME` (on Windows the process environment is not readable: the command line is checked, and the home is checked only through `connections_path`), `GET /` contains `<title>RED PANDA</title>`, `GET /api/workspaces` is a list, `GET /api/model-settings` points `connections_path` at `$REDPANDA_HOME/connections.json` and does not echo credentials, and `GET /api/events` emits `connected`. The JSON also reports `live_model_ready`.

`live_model_ready` is false when no default model is selected or its provider has no usable connection setting. True does not mean the key is valid. vLLM and Ollama can still report configured with an empty key, because a base URL is enough; another provider's configured badge does not establish readiness for the default model. **Skip chat send, the 测试 button, and coding tool rounds when `live_model_ready` is false.** Say so in the proof note. Do not type a message anyway.

Add `--require-model` only when the feature you are about to drive cannot be skipped. `python -m pytest -m process` is a separate contract check, not a substitute for Web entries.

A non-zero exit means do not click. Wrong home, a dead pid, or a page that is not RED PANDA is a different instance.

## Drive

Use the browser automation in the session (Playwright or Chrome DevTools). The verbs below are the harness. There is no `browser` binary in this repository. Resolve each target by role and accessible name. Do not click by coordinates or DOM order.

- `browser open <url>`
- `browser click --role <role> --name "<accessible name>"`
- `browser fill --role textbox --name "<accessible name>" --value "<text>"`
- `browser press --key Enter`
- `browser snapshot --aria --path <file>`
- `browser screenshot --path <file>`

The conversation timeline is a virtual list (react-virtuoso) and dialogs and menus are Mantine transitions; all of them need `requestAnimationFrame` and `ResizeObserver`. In a browser pane that is hidden (for example the desktop app's built-in Browser pane while it is not on screen) those callbacks do not fire: the timeline renders no turns and `新建工作区`, `外观`, `新建群聊` never open. Check with a tiny probe before trusting an empty page; if the pane cannot be kept visible, record those entries as `unreachable` with that prerequisite, not as product failures.

`curl` is only the read-only confirmation written next to an action. Posting to `/api/workspaces`, `/api/agents`, or `/api/sessions/.../inputs` does not prove the page.

Read `features/README.md`, then the feature file. Drive the entry point that file names. A green `python -m pytest` or `python -m pytest -m process` run is not Web proof. Live model tests (`REDPANDA_RUN_LIVE_TESTS=1 python -m pytest tests/live`) call a real model and still are not the Web page.

Colleague tools, private messages, landing and sync, and group chat are `features/colleague-tools.md`, `features/private-messages.md`, `features/land-and-sync.md`, and `features/group-chat.md`. Drive them from the page those files name. Pending items highlight the corresponding Bot in the sidebar and are handled in that Bot's conversation; there is no standalone inbox page. Communication-limit reports and unattended-routine prompts are covered by `features/inbox.md` and `features/routines.md`.

Wait until the sidebar connection indicator has role `status` and accessible name `已连接`. A connecting indicator and visible `正在连接…` text mean the page is not ready; run Doctor again instead of typing.

## Evidence

Write proof under `$EVIDENCE_DIR/<feature-id>/`. Cleanup must not delete that directory.

For each driven entry point save:

- `snapshot.txt` — accessibility snapshot taken after the action, while the resulting state is on screen
- `page.png` — screenshot of that same state, with the sidebar `新建 Bot` entry visible on desktop (on mobile, include the `打开 Agent 名册` control)
- `state.json` — the read-only HTTP body named in the feature file
- `note.txt` — feature id, entry point, and either `passed` or `skipped` plus the Doctor field that blocked it (`live_model_ready`)

For TUI, save `terminal.txt` (startup, command input, reply and exit), the isolated session metadata or journal observations, and `note.txt`; a browser screenshot is not TUI proof.

A skipped live-model step is not a pass and not a pass via pytest. Capture the action and the result. For a workspace, the result is the sidebar control and `GET /api/workspaces`. For a chat send, the result is the user text in the transcript and `GET /api/sessions/<id>`. For a coding round, the result is the transcript and the file contents in the Agent's worktree.

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

On Windows, stop only the recorded pid. Session workers sit in a kill-on-close job of that process, so they exit with it. Do not stop every `python` or `web_chat.py`.

```powershell
Stop-Process -Id (Get-Content "$env:RUN_DIR\web.pid") -Force
Wait-Process -Id (Get-Content "$env:RUN_DIR\web.pid") -Timeout 10 -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $env:RUN_DIR
Test-Path $env:EVIDENCE_DIR
```

After cleanup, the screenshot and `state.json` must still be in `$EVIDENCE_DIR`.

## Helpers

`doctor.py` is the only helper. It is read-only: four GETs plus the pid. Invocation is in Doctor. `--require-model` turns the model warning into a blocker.

## Maintenance

When Web labels, routes, or these flows change, run `/maintain-verification-skill` and correct this map from the page you actually drove.
