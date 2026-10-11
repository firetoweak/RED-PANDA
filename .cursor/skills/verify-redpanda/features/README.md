# RED PANDA verification map

This directory is the maintained source for verifying RED PANDA Web. Read this index, then drive the feature file that matches the change.

## Baseline preconditions

- Launch one isolated instance with the commands in `../SKILL.md`. The origin is `$REDPANDA_VERIFY_URL`. The personal directory is `$REDPANDA_HOME`. The scratch workspace is `$SCRATCH_WORKSPACE`.
- Doctor exits 0 against that pid and home before the first click.
- Viewport is at least 1280×800 so the sidebar is on screen.
- The scratch directory exists and contains `note.txt` with the single line `verify`.
- Do not drive `~/.redpanda`, port 8765 when something else owns it, or a server started with `--workspace` or `--dev`.

## Driving conventions

- Start from the baseline unless the feature file says otherwise.
- Prefer role and accessible name. UI strings are Chinese and must match.
- Browser verbs are defined in `../SKILL.md`. Run them with the session's browser tools.
- Confirm mutations with the listed GET. Do not POST the API as the user action.
- A feature step whose Doctor warning says to skip stays `skipped`. Do not mark it passed because pytest passed.

## Proof and skip reporting

- Save the snapshot, screenshot, HTTP body, and note under `$EVIDENCE_DIR/<feature-id>/`.
- The screenshot shows the RED PANDA sidebar.
- Record the feature id and the entry point in `note.txt`.
- When `live_model_ready` is false, skip chat send, `测试`, coding rounds, and file rewind. Write that field in the note.
- When `sandbox.ready` is false, skip coding rounds and file rewind. On macOS the reason is that the file view is not implemented.
- An unreachable entry stays unreachable. Another route does not verify it.

## Feature entry contract

Each feature file has an H1, one opening paragraph, then four H2 sections in order: `Sub-features`, `How to get to it (user POV)`, `Driving it with the browser`, `Gotchas`.

## Features

- [Web session and chat](./web-session-chat.md) — create a workspace from the empty home, land in a draft session, and send a message when a model is configured.
- [Tool coding round](./tool-coding-round.md) — turn on automatic tool authorization and run `read_file`, `apply_patch`, and `execute_command` from the composer.
- [File rewind](./file-rewind.md) — from a saved step, choose `会话和文件一起回滚` and see the workspace file return.
- [Model settings smoke](./model-settings-smoke.md) — open `模型配置`, read provider badges, and discard an unsaved candidate.
