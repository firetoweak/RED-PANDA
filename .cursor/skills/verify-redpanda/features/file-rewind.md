# File rewind

After a step that saved a workspace version, the person chooses `从这一步之后重开` and then `会话和文件一起回滚`. Later chat disappears and the workspace file returns to that step.

## Sub-features

- `rewind-open` opens the dialog from a step that can be rewound.
- `rewind-files` rolls the session and the workspace file back together.
- `rewind-disk` shows the restored bytes in the scratch file.

Skip the whole feature unless a coding round has already produced a rewindable step and Doctor reports `sandbox.ready`. macOS is always skipped.

## How to get to it (user POV)

- Finish the on-path in [Tool coding round](./tool-coding-round.md) so `note.txt` is `edited` and at least one step shows `从这一步之后重开`.
- Choose `从这一步之后重开` on the `read_file` step. That step stays. Later steps are dropped, and the files return to the state recorded when that step finished.
- In the dialog titled `从这一步之后重开`, choose `会话和文件一起回滚`.

## Driving it with the browser

Preconditions:

- The coding round passed, `note.txt` is `edited`, and the same session is open.
- Doctor exits 0 and `sandbox.ready` is true.
- A step row that shows `read_file` also shows `从这一步之后重开`. Several steps share that button name. If the `read_file` row has no such button, stop and record that. Do not click `只回滚会话` instead.
- If `read_file` and `apply_patch` are on the same row, that row's saved files are already `edited`. Send `请只用 apply_patch 把 note.txt 改成 later，然后回复 LATER_DONE。` first, wait until the file is `later`, then rewind the earlier row.

- **Open the dialog.** Run `browser click --role button --name "从这一步之后重开"` on the `read_file` row, or on the earlier combined row after the `later` follow-up. The heading in the dialog is `从这一步之后重开`, with buttons `只回滚会话` and `会话和文件一起回滚`.
- **Roll files back.** Run `browser click --role button --name "会话和文件一起回滚"`. The dialog closes. Transcript text from after that step is gone.
- **Confirm the file.** Read `$SCRATCH_WORKSPACE/note.txt`. Rewinding the `read_file` row restores `verify`. Rewinding the combined row after the follow-up restores `edited`.
- **Confirm the session.** Run `curl -fsS $REDPANDA_VERIFY_URL/api/sessions/<session id>`. The chosen step remains. Step items after it are gone. The original coding message stays, because it was sent before that step. The `later` follow-up message is gone when that was the path you rewound.
- **Proof.** Snapshot and screenshot the shortened transcript with the RED PANDA sidebar visible. Save the conversation JSON and the restored `note.txt` under `$EVIDENCE_DIR/file-rewind/`.

## Gotchas

- The rewind control is only rendered when that step has a successful file version. A missing button is not a hidden control.
- `只回滚会话` leaves `note.txt` as `edited`. It does not prove this feature.
- The server answers 409 with a detail that starts `这一步没有成功的版本记录，无法从它之后重开` when the step cannot be rewound. The dialog should not have offered the button in that case.
- Writes outside the workspace, package installs, and network calls stay after the rollback. Proof is the scratch file, not a claim about the rest of the machine.
- A command marked read-only is outside file rewind even if it wrote something.
- macOS cannot run this path. Doctor's sandbox reason is the skip note.
