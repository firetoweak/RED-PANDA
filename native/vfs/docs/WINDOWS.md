# Windows filesystem foundation

This fork owns general filesystem semantics in `vfs-core` and transport/lifecycle
in `vfs-mount`. It does not own Command IDs, before/after evidence, publication,
undo policies or receipts. Those belong to an application using this library.
RED PANDA integration lives in the adjacent sandbox and Python adapter.

## Core contracts

Windows HostFS opens the root once and discovers children lazily. It reads native
handles, keeps complete volume/file identity, preserves actual name spelling and
compares names with Windows ordinal case rules. It does not scan or copy the whole
workspace on initialization. Its mutations are refused; reparse entries and volume
statistics are explicitly unsupported. All overlay writes go to the delta database.

Partial-origin copy-up keeps the inode alive while an open file survives unlink.
Created, copied-up and reopened handles expose the same view inode as namespace
lookup; the underlying delta inode is not an observable file-handle identity.
Full chunk overwrite does not read base bytes; a partial overwrite seeds only its
chunk. Unowned chunks remain dependent on the configured base. The default guard
uses size/write/change time and persistent identity; an explicit application content
validator may use hashes instead. This is chunk COW, not byte-range ownership.
The application captures Command intervals separately.

Schema 0.12 accepts only local filesystem storage and refuses other formats
before DDL. It retains the persistent namespaces introduced in 0.10;
old process-local or unnamespaced inode identities cannot be rebound reliably.
Each fresh delta persists a UUID namespace; its own file identities include that
namespace and inode. Independent files in different layers remain distinct, while
hard links and copy-up retain their original identity. Frozen artifacts preserve
the namespace; missing or malformed current namespaces are refused. Origins
and frozen artifacts use this identity representation. Core has no row journal
or relational-history reconstruction; Sandbox owns file-operation evidence.
Native identities identify physical files, not equivalent checkouts on another
machine. Rebinding to another base is not implicit; even identical bytes do not
authorize substituting a different physical origin.
The retained `fs_partial_origin.base_ino` is a historical hint; reopening resolves
its base path and checks the authoritative `fs_origin.base_identity`.

## WinFsp library

Enable the `winfsp` feature on `vfs-mount` for x64 Windows. Install the WinFsp
runtime and obtain its SDK separately. Set `WINFSP_INCLUDE_DIR` to the directory
containing `winfsp/winfsp.h`, and `WINFSP_LIB_DIR` to the directory containing
`winfsp-x64.lib`. These are build settings; production runtime code does not read
the environment for backend policy. A C compiler supported by `cc` is required.
The WinFsp runtime DLL must be discoverable when running the resulting program.

Build requirements and executable test commands are maintained in
[TESTING.md](TESTING.md); this document describes Windows contracts and limits.

`mount_fs(Arc<dyn FileSystem>, MountOpts)` owns the SDK dispatcher. Native calls
are translated directly into filesystem operations; there is no callback TCP
server, application control RPC or auxiliary authority database. Each open owns
its own core file handle through Close. Flush commits file bytes. Call and await
`MountHandle::unmount()` to stop/join callbacks, release lookups and finalize the
filesystem. Drop is best-effort cleanup and does not promise durable completion.
Unknown errors and corrupt state are retained as fatal mount errors; they are
surfaced on unmount rather than converted into ordinary namespace failures.

The current adapter supports regular-file read/write, create, resize, unlink,
file rename with optional replacement, directory creation, enumeration, opening and
empty-directory deletion. Creating directories dispatches to the existing core
`mkdir` operation; nested native creation can add delta directories beneath a base
directory without changing the host. Nonempty directories refuse deletion.
File replacement dispatches to the core rename operation rather than adapter
unlink followed by rename. WinFsp checks open destination handles before invoking
the callback; a busy destination refuses replacement and both paths remain intact.
After closing the destination, replacement can proceed while a source handle
remains open. Reclaimed destination origins are removed from the live overlay
mapping, so subsequent directory enumeration does not refer to a deleted inode.
The native test covers base/delta source and target combinations, partial COW
versions and cross-parent replacement, then reopens the
view and checks database integrity. This does not prove instruction-level crash
atomicity across the overlay's copy-up, rename and whiteout transactions.
Create and Open return the actual spelling of every path component through
WinFsp's normalized-name buffer. Without this, the case-insensitive driver assumes
uppercase opened names and can skip a case-only rename as an identical-name no-op.
Same-parent equivalent names identify the same directory entry and can be renamed
without enabling replacement; a different existing target still refuses that call.
The native test checks base, delta and nested Unicode file names, with and without
replacement, normalized handle paths, enumeration and spelling after reopening.
It refuses directory rename and creation with delete-on-close.
The read-only attribute maps to the core's write-mode bits and remains in the
delta; all other attribute changes and timestamp updates are refused. Git for
Windows requires explicit `core.hideDotFiles=false` for initialization because
hidden attributes have no stored representation.
It does not implement ACL/ADS/reparse operations, other metadata updates, hard-link
creation or a full mmap coherence contract. It exposes a synthetic virtual volume
capacity, not a disk quota or actual free-space measurement. Lookup references are
currently retained until unmount; bounded eviction needs a separate change.
The volume descriptor grants access to the mounting process user.

This is a private filesystem view, not process security isolation. Programs retain
their native environment and can access paths outside the mount. PID attribution,
Command view rotation, publication to the user's workspace and both undo policies
are application responsibilities.

## Verification boundary

The first consolidation was verified on Windows x64 with 208 core unit tests,
eight native filesystem contract tests and one explicitly enabled real WinFsp
mount test. The core/mount library Clippy check and workspace format check passed.
This result covers the `x86_64-pc-windows-gnullvm` build.

The follow-up rounds added real WinFsp attribute, I/O error and execution-crash
suites, the core-only write-crash suite and a process-level rerun of the Python
product flow. The execution-crash suite alone terminates owner and command
processes without Drop or unmount at fourteen checkpoints; every phase recovers
committed bytes, hard-link identity and a reusable mountpoint.

Run the native core suite and explicitly enable the real WinFsp tests on a
machine with the runtime. Linux FUSE and macOS HostFS need their own platform
verification; a Windows library build is not evidence that those gates passed.
The Python product-flow rerun skips the Linux/FUSE-only contracts on Windows.

## Process-crash validation

The serving process and native command process are separate. The first crash
experiment, which opened the serving process's own mount and then killed that
process, stalled process exit and retained an experimental volume. That scenario
remains unresolved. A serving process must use core operations and must not open
native handles into its own mount, including setting its working directory there.
Native mount paths belong to separate command processes. WinFsp's maintainer
[describes this self-reference deadlock](https://groups.google.com/g/winfsp/c/isuZQ1Byk1g):
the serving threads can be gone before process teardown closes the native handles.
This explanation matches the failed experiment; its actual kernel wait has not
been inspected and the residual volume has not been released.

The checkpoint suite terminates workers without Drop or unmount. It verifies
command death, owner death before and after Flush, completed create/rename/delete,
and core unlink while a native file remains open. Reopening checks full file bytes,
hard-link identity, whiteouts, orphan cleanup, the integrity battery and reuse of
the same mountpoint. Fixtures are small ordinary files and retained as evidence;
the test does not recursively clean a potentially stale mount.

Flushed bytes must survive. A write without explicit Flush may recover the old or
new complete value. Windows delete-pending is checked separately: DeleteFile can
return while another handle still keeps the file open. The current adapter does
not persist that pending disposition before Cleanup; after owner death the file
can reappear with its complete flushed content. Passing this observation case
does not establish durable pending-delete semantics.

The disposition cases also exercise TRUE/FALSE cancellation, cancellation with
the file still open when the owner dies, TRUE/FALSE/TRUE retoggling, deletion on
normal last close and deletion when the command process dies while the owner
remains available. Duplicate handles to one file object and independently opened
handles retain readable bytes until last cleanup, while new opens are denied.
The command-death cases wait for the namespace deletion to become visible through
core, issue an explicit fsync barrier and only then terminate the owner. They do
not establish a generic drain barrier for all queued native Cleanup callbacks.

SetDelete validates disposition without unlinking: WinFsp owns the transient
flag and performs the actual deletion through Cleanup. A cancellable disposition
is not a durable namespace mutation. Persisting and replaying incomplete deletion
intent across mount-owner death would require an additional explicit contract;
the current implementation does not provide it. Application result collection
must distinguish process exit, completed native cleanup and committed namespace
changes, rather than treating DeleteFile success alone as a completed deletion.

The core-only write-crash suite needs no WinFsp mount. A validation barrier pauses
the second missing COW chunk while the first is prepared in memory inside an open
transaction. After termination, neither chunk mappings nor overrides survive.
Five timed terminations sample repeated two-range writes and fsync calls. Both
ranges and all untouched bytes must recover as one complete generation, at least
as new as the last acknowledgment published after fsync. Every reopen also checks
base bytes, hard-link identity and database integrity. The acknowledgment marker
is test evidence on a normal process crash, not a second filesystem authority.

These are process-termination checkpoints, not proof of power-loss durability,
arbitrary instruction-level failure or atomic Command execution. Timed kills do
not identify the interrupted COMMIT instruction. Exact commit fault injection,
pending-delete recovery and failure during a multi-operation Command need further
tests.

## I/O failure boundary

The core error suite injects an I/O error while preparing the second COW chunk,
and a test-database trigger aborts insertion of the second mapping after the first
mapping was inserted. File bytes, modification/change times and storage rows
remain unchanged; removing the fault permits a successful new write.
The original typed I/O/database cause is retained.

The native error suite wraps file operations in tests only. Unexpected read,
write and fsync failures reject the native call, latch the mount and preserve the
first core cause for explicit unmount. A failed Flush never returns success.
Known storage-full errors return STATUS_DISK_FULL to Windows and allow retry
after the failure is removed. Cleanup failures are still fatal because Cleanup
cannot report a recoverable failure to its native caller.

These injected errors verify transaction rollback and transport error propagation.
They do not fill a real device, fail SQLite's operating-system fsync, simulate
power loss or establish recovery from physical media damage. Successful final
unmount after a one-shot injected error can commit bytes later; this does not
retroactively make the earlier failed Flush successful.
