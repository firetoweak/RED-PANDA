# 落地与同步

`land_branch` 把 Agent 分支上已经提交的工作合入项目集成分支。`sync_branch` 把集成分支上同事已经落地的工作合进自己的分支。两个工具都不接受参数，都不进入待处理条，也不改人当前检出的分支和工作目录。未提交的修改不跟着落地。冲突时落地放弃、集成分支不动；同步则停在冲突状态，留给 Agent 用普通 Git 命令解决或放弃。来源：`redpanda/assistant/agent_tools.py`、`redpanda/assistant/host/agent_worktrees.py`。落地后变化查看变空，见 [session-file-changes.md](./session-file-changes.md)。

工作树在 `$REDPANDA_HOME/state/coding_bot/<sha256(workspace_id) 的前 20 位>/`：`integration` 是集成工作树，`<sha256(session_id) 的前 20 位>` 是该 Agent 的工作树。哈希用 UTF-8 的 sha256 十六进制。提交可以直接在 Agent 工作树里用 Git 准备，再请模型单独调用工具；工具只搬已提交的内容。

## Sub-features

- `land`：已提交的工作进入集成分支。能快进则快进，否则产生作者为该 Agent 名字、说明为 `合入 <名字> 的工作` 的合并提交。
- `withheld`：未提交的修改留在 Agent 工作树，不进入集成分支。
- `sync`：同事落地之后，另一位 `sync_branch` 才能在自己的工作树里拿到。说明为 `同步已落地的工作`。
- `conflict`：落地冲突不改集成分支。同步冲突把合并留在进行中。
- `blocked`：集成工作树里会被覆盖的文件挡住落地；Agent 工作树里会被覆盖的未提交文件挡住同步。

## How to get to it (user POV)

- 同一项目的 `Ada` 与 `Lin`，项目是 `$SCRATCH_WORKSPACE`。
- 在 Agent 工作树里准备提交，再在该 Agent 的 `消息` 里请它只调用 `land_branch` 或 `sync_branch`。
- 没有「落地」或「同步」按钮。

## Driving it with the browser

需要 `live_model_ready` 才能请模型调用工具。`code` 在该会话 journal 的命令结果里。成功调用的那一步 `tools` 里只有这一个工具。

- **落地已提交的工作**：在 `Ada` 的工作树提交 `landed.txt`，内容 `landed`。请它调用 `land_branch`。
  - (a) 结果 `LAND_COMPLETED`，`data.fast_forward` 为 true 时集成分支 HEAD 等于 `Ada` 分支 HEAD；否则集成工作树多一个作者为 `Ada`、说明为 `合入 Ada 的工作` 的合并提交。`integration` 工作树里有 `landed.txt`。`GET /api/sessions/<Ada>/changes` 不再包含这个已落地的文件。
  - (b) 工具卡 `land_branch` 徽标 `完成`。输入框上方不出现这条调用的待处理条。
  - (c) `$SCRATCH_WORKSPACE` 的 HEAD、暂存区和 `note.txt` 不变。集成分支出现在该仓库的分支列表里是正常的，不要把人的检出切到那条分支。
  - (d) 没有新提交时再落地 → `LAND_UNCHANGED`，提示集成分支已经包含全部分支上已提交的工作，集成 HEAD 不变。
- **未提交的不带走**：`Ada` 落地之后，在它的工作树再写 `draft.txt` 且不提交，然后落地。
  - (a) 结果 `LAND_UNCHANGED`。`integration` 里没有 `draft.txt`。`Ada` 工作树里的未跟踪文件还在。
  - (b) 变化查看仍能看到 `draft.txt`（尚未落地的工作还在差异里）。
  - (c) 人的 `note.txt` 不变。
  - (d) 把 `draft.txt` 提交后再落地，它才出现在 `integration`。
- **同步**：`Ada` 已经把 `landed.txt` 落地。请 `Lin` 调用 `sync_branch`。
  - (a) 结果 `SYNC_COMPLETED`。`Lin` 的工作树里出现 `landed.txt`，内容与集成一致。能快进则 `fast_forward` 为 true，否则多一个说明为 `同步已落地的工作` 的合并提交。
  - (b) 工具卡 `sync_branch` 徽标 `完成`，没有待处理条。
  - (c) `Ada` 工作树里尚未提交的 `draft.txt` 不会出现在 `Lin` 的工作树。人的检出不变。
  - (d) `Lin` 已经含有集成分支的全部提交时再同步 → `SYNC_UNCHANGED`。
- **冲突**：`Ada` 与 `Lin` 各自提交对同一行的不同修改。`Ada` 先落地成功。请 `Lin` 落地，不要先同步。
  - (a) `Lin` 的结果为 `LAND_CONFLICT`，`data.files` 列出冲突文件，错误为 `你的分支与集成分支上已落地的工作冲突，集成分支没有改变。`。集成 HEAD 仍是 `Ada` 落地后的提交。
  - (b) 然后请 `Lin` `sync_branch`。结果 `SYNC_CONFLICT`，错误为 `同步有冲突，合并停在冲突状态。`。`Lin` 的工作树处于合并进行中，冲突文件里有冲突标记。
  - (c) 这次失败的落地没有改集成分支，也没有改人的检出。
  - (d) 冲突还在时再 `sync_branch` → `SYNC_MERGE_IN_PROGRESS`，`还有一次合并没有完成。`。在 `Lin` 的工作树 `git merge --abort` 后，工作树离开合并状态，集成分支不受影响。
- **挡住**：在 `integration` 工作树里改掉即将被落地覆盖的已跟踪文件且不提交，请 `Ada` 落地。
  - (a) 结果 `LAND_BLOCKED`，`data.files` 列出这些文件，集成 HEAD 不变，挡住的文件还在。
  - (b) 工具卡徽标 `完成`（这是 `ok: false` 的已知结果）。
  - (c) Agent 工作树里的提交都还在。不要让 Agent 去清理集成工作树。
  - (d) 在 `Lin` 的工作树留一个会被同步覆盖的未提交文件，再 `sync_branch` → `SYNC_BLOCKED`，错误含 `会被同步覆盖，同步没有执行`，`Lin` 的 HEAD 不变，该文件还在。

## Gotchas

- 两个工具都必须单独调用。同一步里还有别的工具时，这一步不会落地或同步，Git 状态保持调用前的样子。
- 带参数调用的结果是 `INVALID_ARGUMENT`，错误为 `land_branch 不接受参数` 或 `sync_branch 不接受参数`。
- Git 的其他失败（例如 `index.lock`、文件被占用）是 `LAND_FAILED` 或 `SYNC_FAILED`，`data.git` 为 Git 原文。不要把这类失败说成「文件会被覆盖」。
- 落地不经人批准。未 live 跑过。
