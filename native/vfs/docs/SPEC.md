# RED PANDA VFS 文件存储格式

**Version:** 0.12

## Introduction

This local format stores current filesystem state and overlay lineage.
Only version 0.12 is accepted. File-operation history belongs to Sandbox's
file-management layer; core provides current-state transactions and frozen artifacts.

All timestamps in this specification use Unix epoch format (seconds since 1970-01-01 00:00:00 UTC) with optional nanosecond precision via separate `_nsec` columns.

## Runtime Architecture and Safety Invariants

The persistent Vfs authority is the SQLite database described by this
specification. Runtime mounts, caches, file handles, FUSE lookup references, and
overlay inode maps are acceleration structures only; they MUST be reconstructible
from the database plus the configured read-only base path and MUST NOT become
the only source of virtual filesystem state.

Vfs sandboxing is built around two invariants:

1. A portable Vfs database contains all writable virtual filesystem state.
   Clean shutdown SHOULD checkpoint transient SQLite sidecars so backups and
   materialized copies can be represented as a single main database file.
2. Copy-on-write sandbox writes MUST NOT modify the real filesystem. Overlay
   backends MAY read from an explicitly scoped base directory, but file creates,
   writes, truncates, chmod/chown/utimens, links, renames, and deletes are
   represented in the Vfs delta database and overlay metadata.

Implementations MAY use kernel caches, positive/negative lookup caches,
attribute caches, read-dir caches, and parallel FUSE dispatch, provided they
preserve POSIX lookup reference accounting. In particular, any cached positive
lookup reply that creates a kernel lookup reference MUST either reach the backing
filesystem lookup path or explicitly retain the backing inode reference before
replying; later `FORGET` requests must release the same reference count.
Namespace mutations MUST invalidate affected cached dentries and attributes
before the mutation is considered visible to the caller.

The subsections below describe the acceleration structures the reference
implementation actually ships and the invariants each one must preserve.
Configuration is declared in the core and FUSE config modules.

### Write Batching and Durability

Writes MAY be acknowledged from an in-memory pending map before their bytes
reach SQLite. The reference implementation batches FUSE writeback-cache
writes and drains them on a short timer window, a per-inode pending-byte
trigger, a global pending-byte cap, and bounded per-transaction inode/byte
budgets (`VFS_BATCH_*` knobs). Buffered acknowledgement is only permitted
for volatile-durability writes; any operation that promises durability —
`fsync`, an NFSv3 `WRITE` acknowledged as `FILE_SYNC`, or unmount/shutdown
finalization — MUST NOT return until the affected pending bytes are committed
to the database (a per-inode or filesystem-wide commit barrier).

Pending state is an acceleration structure, never a second authority:
metadata reads (`stat`, directory attributes) MUST merge pending sizes and
times so applications observe their own buffered writes, and deletions MUST
discard the dead inode's pending bytes. On unclean termination, volatile
(never-fsynced) bytes MAY be lost, but the database MUST remain consistent:
a crash may lose the tail of un-synced data, never corrupt committed state.

### FUSE no-open / no-flush Lifecycle

On Linux the FUSE adapter runs with zero-message opens and zero-message
flush by default: it answers `OPEN`/`RELEASE` (and close-time `FLUSH`) with
`ENOSYS`, after which the kernel stops sending them and issues data requests
with `fh = 0`. Implementations using this mode MUST NOT key any state to
open/release pairs. Per-inode resources (backing file handles for base reads,
caches) are keyed by inode number, held in a bounded LRU
(`VFS_FUSE_INO_FILES_CAP`), and reclaimed by kernel `FORGET` traffic and
LRU eviction rather than by `RELEASE`.

Consequences that MUST hold:

1. Unlink-while-open works through lookup-reference accounting: an unlinked
   inode's rows are reaped only after the kernel drops its last lookup
   reference (deferred reap), not at `RELEASE` time.
2. Close does not imply commit. With no-flush enabled the kernel has already
   written back dirty pages before close; durability is promised only by
   `fsync` (see the durability contract above).
3. Both behaviors retain kill switches (`VFS_FUSE_NOOPEN`,
   `VFS_FUSE_NOFLUSH`) that select the default and disabled legs.

### FUSE-over-io_uring Transport

On kernels that expose `/sys/module/fuse/parameters/enable_uring = Y`, the
FUSE session attempts the FUSE-over-io_uring transport by default
(`VFS_FUSE_URING`, bounded queue depth via `VFS_FUSE_URING_DEPTH`)
and falls back to the classic `/dev/fuse` read/write loop when io_uring
setup is unavailable or fails. The transport is a performance detail only:
request semantics, reply contents, cache invalidation, and teardown bounds
MUST be identical on both legs, and unmount MUST join transport threads on
both legs without leaking the mount.

### Overlay Base Reality

Overlay mounts scope reads to the configured base directory and write only
to the delta database (see the sandbox invariants above). Two consequences
are contractual:

1. Renaming a base-layer directory returns `EXDEV` rather than attempting a
   recursive copy-up; callers (e.g. `mv`) then perform an explicit
   copy+delete, which lands in the delta layer.
2. External mutation of the base tree under a live mount is detected where
   it matters: partial-origin reads validate the recorded base fingerprint
   and MUST fail rather than silently mix bytes from a drifted base file
   (see Partial-Origin Overlay Mode).

## Virtual Filesystem

The virtual filesystem provides POSIX-like file operations for agent artifacts. The filesystem separates namespace (paths and names) from data (file content and metadata) using a Unix-like inode design. This enables hard links (multiple paths to the same file), efficient file operations, proper file metadata (permissions, timestamps), and chunked content storage.

### Schema

#### Table: `fs_config`

Stores filesystem-level configuration. This table is initialized once when the filesystem is created and MUST NOT be modified afterward.

```sql
CREATE TABLE fs_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)
```

**Fields:**

- `key` - Configuration key
- `value` - Configuration value (stored as text)

**Required Configuration:**

| Key | Description | Default |
|-----|-------------|---------|
| `schema_version` | On-disk schema version | `0.12` |
| `filesystem_id` | Immutable UUID v4 creation namespace of this delta | Generated once at database creation |
| `chunk_size` | Size of data chunks in bytes | `65536` |
| `inline_threshold` | Maximum dense regular-file size stored inline in `fs_inode.data_inline` | `16384` |

**Notes:**

- `chunk_size` determines the fixed size of data chunks in `fs_data`
- New filesystems use 64 KiB chunks by default
- `inline_threshold` determines when dense regular files may avoid `fs_data` rows entirely
- Schema and geometry keys are immutable after initialization
- Implementations MAY define additional configuration keys

#### Table: `fs_inode`

Stores file and directory metadata.

```sql
CREATE TABLE fs_inode (
  ino INTEGER PRIMARY KEY AUTOINCREMENT,
  mode INTEGER NOT NULL,
  nlink INTEGER NOT NULL DEFAULT 0,
  uid INTEGER NOT NULL DEFAULT 0,
  gid INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,
  atime INTEGER NOT NULL,
  mtime INTEGER NOT NULL,
  ctime INTEGER NOT NULL,
  rdev INTEGER NOT NULL DEFAULT 0,
  atime_nsec INTEGER NOT NULL DEFAULT 0,
  mtime_nsec INTEGER NOT NULL DEFAULT 0,
  ctime_nsec INTEGER NOT NULL DEFAULT 0,
  data_inline BLOB,
  storage_kind INTEGER NOT NULL DEFAULT 0
)
```

**Fields:**

- `ino` - Inode number (unique identifier)
- `mode` - File type and permissions (Unix mode bits)
- `nlink` - Number of hard links pointing to this inode
- `uid` - Owner user ID
- `gid` - Owner group ID
- `size` - Total file size in bytes
- `atime` - Last access time (Unix timestamp, seconds)
- `mtime` - Last modification time (Unix timestamp, seconds)
- `ctime` - Creation/change time (Unix timestamp, seconds)
- `rdev` - Device number for character and block devices (major/minor encoded)
- `atime_nsec` - Nanosecond component of last access time (0–999999999)
- `mtime_nsec` - Nanosecond component of last modification time (0–999999999)
- `ctime_nsec` - Nanosecond component of creation/change time (0–999999999)
- `data_inline` - Optional inline content for dense small regular files
- `storage_kind` - Storage layout marker: `0` for chunked data in `fs_data`, `1` for inline data in `data_inline`

**Storage Layout Rules:**

- Directories and symlinks MUST use `storage_kind = 0` and `data_inline IS NULL`
- Inline regular files MUST use `storage_kind = 1`, store all bytes in `data_inline`, and have no `fs_data` rows
- Chunked regular files MUST use `storage_kind = 0` and `data_inline IS NULL`
- `size` is authoritative for both layouts
- Inline files represent dense content only; sparse writes MUST transition to chunked storage
- Implementations MAY transition chunked files back to inline after truncation only when the resulting file is dense and at or below `inline_threshold`

**Mode Encoding:**

The `mode` field combines file type and permissions:

```
File type (upper bits):
  0o170000 - File type mask (S_IFMT)
  0o100000 - Regular file (S_IFREG)
  0o040000 - Directory (S_IFDIR)
  0o120000 - Symbolic link (S_IFLNK)
  0o010000 - FIFO/named pipe (S_IFIFO)
  0o020000 - Character device (S_IFCHR)
  0o060000 - Block device (S_IFBLK)
  0o140000 - Socket (S_IFSOCK)

Permissions (lower 12 bits):
  0o000777 - Permission bits (rwxrwxrwx)

Example:
  0o100644 - Regular file, rw-r--r--
  0o040755 - Directory, rwxr-xr-x
```

**Special Inodes:**

- Inode 1 MUST be the root directory

#### Table: `fs_dentry`

Maps names to inodes (directory entries).

```sql
CREATE TABLE fs_dentry (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  parent_ino INTEGER NOT NULL,
  ino INTEGER NOT NULL,
  UNIQUE(parent_ino, name)
)

CREATE INDEX idx_fs_dentry_parent ON fs_dentry(parent_ino, name)
```

**Fields:**

- `id` - Internal entry ID
- `name` - Basename (filename or directory name)
- `parent_ino` - Parent directory inode number
- `ino` - Inode this entry points to

**Constraints:**

- `UNIQUE(parent_ino, name)` - No duplicate names in a directory

**Notes:**

- Root directory (ino=1) has no dentry (no parent)
- Multiple dentries MAY point to the same inode (hard links)
- Link count is stored in `fs_inode.nlink` and must be incremented/decremented when dentries are added/removed

#### Table: `fs_data`

Maps each live file chunk to content in the shared chunk store. Chunk size is
configured at filesystem level via `fs_config`.

```sql
CREATE TABLE fs_data (
  ino INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  digest BLOB NOT NULL,
  PRIMARY KEY (ino, chunk_index)
)
```

**Fields:**

- `ino` - Inode number
- `chunk_index` - Zero-based chunk index (chunk 0 contains bytes 0 to chunk_size-1)
- `digest` - Raw 32-byte BLAKE3 digest identifying the chunk's `fs_chunk` row

**Notes:**

- Directories MUST NOT have data chunks
- Inline regular files MUST NOT have data chunks
- Chunk size is determined by the `chunk_size` value in `fs_config`
- New v0.7+ filesystems default to 64 KiB chunks
- All chunks except the last chunk of a dense chunked file SHOULD be exactly `chunk_size` bytes
- The last chunk MAY be smaller than `chunk_size`
- Sparse holes MAY be represented by missing chunk rows and MUST read back as zero bytes
- All-zero chunk rows MAY be omitted when doing so preserves read semantics
- Every `digest` MUST resolve to exactly one `fs_chunk` row
- Byte offset for a chunk = `chunk_index * chunk_size`
- To read at byte offset `N`: `chunk_index = N / chunk_size`, `offset_in_chunk = N % chunk_size`

#### Table: `fs_chunk`

Stores content-addressed chunk bytes shared by every `fs_data` mapping with
the same digest.

```sql
CREATE TABLE fs_chunk (
  digest BLOB PRIMARY KEY,
  data BLOB NOT NULL,
  refcount INTEGER NOT NULL DEFAULT 0
)
```

**Fields:**

- `digest` - Raw 32-byte BLAKE3 digest of `data`
- `data` - Binary chunk content, up to `chunk_size` bytes
- `refcount` - Number of live `fs_data` mappings that reference this digest

**Notes:**

- Equal chunk bytes MUST share one row.
- `refcount` counts live mappings only. Zero-refcount chunks may be reclaimed
  independently of Sandbox operation history. Frozen artifacts own their bytes.
- Digest computation and insertion MUST occur in the same transaction as the
  corresponding `fs_data` mapping change.

#### Table: `fs_symlink`

Stores symbolic link targets.

```sql
CREATE TABLE fs_symlink (
  ino INTEGER PRIMARY KEY,
  target TEXT NOT NULL
)
```

**Fields:**

- `ino` - Inode number of the symlink
- `target` - Target path (may be absolute or relative)

### Operations

#### Path Resolution

To resolve a path to an inode:

1. Start at root inode (ino=1)
2. Split path by `/` and filter empty components
3. For each component:
   ```sql
   SELECT ino FROM fs_dentry WHERE parent_ino = ? AND name = ?
   ```
4. Return final inode or NULL if any component not found

#### Creating a File

1. Resolve parent directory path to inode
2. Get chunk size from config:
   ```sql
   SELECT value FROM fs_config WHERE key = 'chunk_size'
   ```
3. Insert inode:
   ```sql
   INSERT INTO fs_inode (mode, uid, gid, size, atime, mtime, ctime)
   VALUES (?, ?, ?, 0, ?, ?, ?)
   RETURNING ino
   ```
4. Insert directory entry:
   ```sql
   INSERT INTO fs_dentry (name, parent_ino, ino)
   VALUES (?, ?, ?)
   ```
5. Increment link count:
   ```sql
   UPDATE fs_inode SET nlink = nlink + 1 WHERE ino = ?
   ```
6. If initial content is dense and `size <= inline_threshold`, store it inline:
   ```sql
   UPDATE fs_inode
   SET size = ?, data_inline = ?, storage_kind = 1, mtime = ?
   WHERE ino = ?
   ```
7. Otherwise, split data into chunks and content-address every stored chunk.
   Chunks that are entirely zero may be omitted — a missing mapping reads
   back as zeroes — but a writer that stores them pays almost nothing, since
   every zero chunk deduplicates to the one all-zero `fs_chunk` row:
   ```sql
   INSERT INTO fs_chunk (digest, data, refcount)
   VALUES (?, ?, 1)
   ON CONFLICT(digest) DO UPDATE SET refcount = refcount + 1;

   INSERT INTO fs_data (ino, chunk_index, digest)
   VALUES (?, ?, ?)
   ```
   Where `digest` is the raw 32-byte BLAKE3 digest of `data`, and
   `chunk_index` starts at 0 and increments for each logical chunk.
8. Update inode size and mark chunked storage:
   ```sql
   UPDATE fs_inode SET size = ?, data_inline = NULL, storage_kind = 0, mtime = ? WHERE ino = ?
   ```

#### Reading a File

1. Resolve path to inode
2. Fetch inode size and storage layout:
   ```sql
   SELECT size, storage_kind, data_inline FROM fs_inode WHERE ino = ?
   ```
3. If `storage_kind = 1`, return `data_inline` truncated to `size`
4. Otherwise, fetch all chunks in order:
   ```sql
   SELECT d.chunk_index, c.data
   FROM fs_data d
   JOIN fs_chunk c ON c.digest = d.digest
   WHERE d.ino = ?
   ORDER BY d.chunk_index ASC
   ```
5. Concatenate chunks in order, treating missing sparse chunks as zeroes up to `size`
6. Update access time:
   ```sql
   UPDATE fs_inode SET atime = ? WHERE ino = ?
   ```

#### Reading a File at Offset

To read `length` bytes starting at byte offset `offset`:

1. Resolve path to inode
2. Fetch inode size and storage layout:
   ```sql
   SELECT size, storage_kind, data_inline FROM fs_inode WHERE ino = ?
   ```
3. If `storage_kind = 1`, slice `data_inline` according to `offset` and `length`
4. Otherwise, get chunk size from config:
   ```sql
   SELECT value FROM fs_config WHERE key = 'chunk_size'
   ```
5. Calculate chunk range:
   - `start_chunk = offset / chunk_size`
   - `end_chunk = (offset + length - 1) / chunk_size`
6. Fetch required chunks:
   ```sql
   SELECT d.chunk_index, c.data
   FROM fs_data d
   JOIN fs_chunk c ON c.digest = d.digest
   WHERE d.ino = ? AND d.chunk_index >= ? AND d.chunk_index <= ?
   ORDER BY d.chunk_index ASC
   ```
7. Extract the requested byte range from the chunks:
   - `offset_in_first_chunk = offset % chunk_size`
   - Skip first `offset_in_first_chunk` bytes of first chunk
   - Take `length` total bytes across chunks
   - Fill missing sparse chunks with zeroes up to EOF

#### Listing a Directory

1. Resolve directory path to inode
2. Query entries:
   ```sql
   SELECT name FROM fs_dentry WHERE parent_ino = ? ORDER BY name ASC
   ```

#### Deleting a File

1. Resolve path to get inode and parent
2. Delete directory entry:
   ```sql
   DELETE FROM fs_dentry WHERE parent_ino = ? AND name = ?
   ```
3. Decrement link count:
   ```sql
   UPDATE fs_inode SET nlink = nlink - 1 WHERE ino = ?
   ```
4. Check if last link:
   ```sql
   SELECT nlink FROM fs_inode WHERE ino = ?
   ```
5. If nlink = 0, delete the inode and its mappings, decrementing the
   corresponding `fs_chunk.refcount` values in the same transaction:
   ```sql
   DELETE FROM fs_inode WHERE ino = ?
   DELETE FROM fs_data WHERE ino = ?
   ```
6. Garbage-collect only zero-refcount `fs_chunk` rows after acknowledged writes drain.

#### Creating a Hard Link

1. Resolve source path to get inode
2. Resolve destination parent to get parent_ino
3. Insert new directory entry:
   ```sql
   INSERT INTO fs_dentry (name, parent_ino, ino)
   VALUES (?, ?, ?)
   ```
4. Increment link count:
   ```sql
   UPDATE fs_inode SET nlink = nlink + 1 WHERE ino = ?
   ```

#### Reading File Metadata (stat)

1. Resolve path to inode
2. Query inode (includes link count):
   ```sql
   SELECT ino, mode, nlink, uid, gid, size, atime, mtime, ctime, rdev,
          atime_nsec, mtime_nsec, ctime_nsec
   FROM fs_inode WHERE ino = ?
   ```

### Initialization

When creating a new agent database, initialize the filesystem configuration and root directory:

```sql
-- Initialize filesystem configuration
INSERT INTO fs_config (key, value) VALUES ('schema_version', '0.12');
INSERT INTO fs_config (key, value) VALUES ('chunk_size', '65536');
INSERT INTO fs_config (key, value) VALUES ('inline_threshold', '16384');

-- Initialize root directory
INSERT INTO fs_inode (ino, mode, nlink, uid, gid, size, atime, mtime, ctime)
VALUES (1, 16877, 2, 0, 0, 0, unixepoch(), unixepoch(), unixepoch());
```

Where `16877` = `0o040755` (directory with rwxr-xr-x permissions).

**Note:** The `chunk_size` and `inline_threshold` values can be customized at filesystem creation time but MUST NOT be changed afterward. The root directory starts at `nlink=2` for its synthetic `.` and `..` references.

### Format Boundary

`PRAGMA user_version = 12` identifies the local-only filesystem format. `MIN_SUPPORTED` is
0.12. Initialization, writable open and read-only open MUST refuse any older
format before running schema DDL. The older application tables and remote-chunk
layouts are outside this local filesystem contract; no migration path is provided.
A current database with missing or incompatible identity columns is corrupt
and MUST be refused, rather than repaired as a legacy layout.

`filesystem_id` is a canonical UUID v4 generated exactly once in the new-schema
transaction. Files created in that delta have identity `vfs:<filesystem_id>:<ino>`.
Independent deltas therefore do not alias when their inode numbers coincide.
Hard links share identity; overlay copy-up retains the originating identity.
Frozen artifacts and writable copies preserve `filesystem_id`.
Current databases with a missing or malformed namespace MUST be refused without
regenerating it. Content digests validate bytes; they do not identify independent files.

### Consistency Rules

1. Root inode (ino=1) MUST always exist
2. Every dentry MUST reference a valid inode
3. Every dentry MUST reference a valid parent inode
4. No directory MAY contain duplicate names
5. Directories MUST have mode with S_IFDIR bit set
6. Regular files MUST have mode with S_IFREG bit set
7. Inline regular files MUST have `storage_kind = 1`, `data_inline` length equal to `size`, and no `fs_data` rows
8. Chunked regular files MUST have `storage_kind = 0` and `data_inline IS NULL`
9. File reads MUST return exactly `size` bytes regardless of sparse missing chunks
10. Every inode MUST have at least one dentry (except root)
11. Every `fs_data.digest` and `fs_chunk.digest` MUST be exactly 32 bytes
12. Every `fs_data.digest` MUST resolve to an `fs_chunk` row
13. Every `fs_chunk.refcount` MUST equal the number of live `fs_data` mappings for that digest

### Implementation Notes

- Use `RETURNING` clause to safely get auto-generated inode numbers
- Parent directories are created implicitly as needed
- Empty files have an inode but no data chunks
- Symlink resolution is implementation-defined (not part of schema)
- Use transactions for multi-step operations to maintain consistency

### Extension Points

Implementations MAY extend the filesystem schema with additional functionality:

- Extended attributes table
- File ACLs and advanced permissions
- Quota tracking per user/group
- Operation history (owned by the Sandbox file-management layer)
- Content deduplication
- Compression metadata
- File checksums/hashes

Such extensions SHOULD use separate tables to maintain referential integrity.

## Overlay Filesystem

The overlay filesystem provides copy-on-write semantics by layering a writable delta filesystem on top of a read-only base filesystem. Changes are written to the delta layer while the base layer remains unmodified. This enables sandboxed execution where modifications can be discarded or committed independently.

### Whiteouts

When a file is deleted from an overlay filesystem, the deletion must be recorded so that lookups do not fall through to the base layer. This is accomplished using "whiteouts" - markers that indicate a path has been explicitly deleted.

#### Table: `fs_whiteout`

Tracks deleted paths in the overlay to prevent base layer visibility.

```sql
CREATE TABLE fs_whiteout (
  path TEXT PRIMARY KEY,
  parent_path TEXT NOT NULL,
  created_at INTEGER NOT NULL
)

CREATE INDEX idx_fs_whiteout_parent ON fs_whiteout(parent_path)
```

**Fields:**

- `path` - Normalized absolute path that has been deleted
- `parent_path` - Parent directory path (for efficient child lookups)
- `created_at` - Deletion timestamp (Unix timestamp, seconds)

**Notes:**

- The `parent_path` column enables O(1) lookups of whiteouts within a directory, avoiding expensive `LIKE` pattern matching
- For the root directory `/`, `parent_path` is `/`
- For other paths, `parent_path` is the path with the final component removed (e.g., `/foo/bar` has parent `/foo`)

### Overlay Configuration

Overlay databases persist the base layer they were initialized with so an existing database can be reopened with the same overlay semantics.

#### Table: `fs_overlay_config`

```sql
CREATE TABLE fs_overlay_config (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
)
```

**Required Configuration:**

| Key | Description |
|-----|-------------|
| `base_path` | Canonical path to the read-only base directory |

**Optional Configuration:**

| Key | Description |
|-----|-------------|
| `parent_artifact` | sha256 (64 lowercase hex chars) of the frozen parent artifact a branch delta reads through |


### Frozen parent artifacts

A child delta records its immutable parent under `parent_artifact`; `base_path`
records the scoped host base. The application resolves and verifies the parent
chain before building layered overlays. Parent artifacts are opened strictly
read-only, and their reads/finalize must not modify the database file family.
`snapshot_into` drains acknowledged writes and creates a durable single-file
database through a consistent SQLite snapshot. Artifact naming, ownership,
publication and collection belong to RED PANDA, outside this library.

### Operations

#### Create Whiteout

When deleting a file that exists in the base layer:

```sql
INSERT INTO fs_whiteout (path, parent_path, created_at)
VALUES (?, ?, ?)
ON CONFLICT(path) DO UPDATE SET created_at = excluded.created_at
```

#### Check for Whiteout

Before falling through to the base layer during lookup:

```sql
SELECT 1 FROM fs_whiteout WHERE path = ?
```

#### Remove Whiteout

When creating a file at a previously deleted path:

```sql
DELETE FROM fs_whiteout WHERE path = ?
```

#### List Child Whiteouts

When listing a directory, get whiteouts to exclude from base layer results:

```sql
SELECT path FROM fs_whiteout WHERE parent_path = ?
```

### Overlay Lookup Semantics

1. Check if path exists in delta layer → return delta entry
2. Check if path has a whiteout → return "not found"
3. Check if path exists in base layer → return base entry
4. Return "not found"

### Persistent Base Origin Tracking

Copy-up MUST preserve an object's identity independently of an adapter's
process-local inode cache. `fs_origin` maps a persistent base identity to its
private delta inode. Visiting a hard-link alias after restart MUST resolve
to the same private inode even when the base adapter allocates new cache numbers.
Overlay inode numbers remain mount-local and are not on-disk object identities.

```sql
CREATE TABLE fs_origin (
  delta_ino INTEGER PRIMARY KEY,
  base_identity TEXT NOT NULL UNIQUE
)
```

A base adapter supplies an opaque stable identity within its configured base.
Windows HostFS uses the volume serial number and the complete 128-bit native
file ID. Unix HostFS uses device/inode identity; a Vfs base uses its durable
inode ID. Identity is preserved in origin rows and frozen artifacts.
An overlay used as another base preserves its origin identity across copy-up.

### Partial-Origin Overlay Mode

Partial-origin copy-up is an opt-in overlay mode selected by the first-class
CLI policy `--partial-origin <off|on|auto>` (with
`--partial-origin-threshold-bytes` sizing the `auto` cutoff). The default
overlay behavior remains whole-file copy-up (`off`). In opt-in mode,
write-opening a regular base-layer file creates a delta inode with the
original size and metadata, records the base path/fingerprint in
`fs_partial_origin`, and stores only changed chunk mappings in `fs_data` plus
`fs_chunk_override`; their bytes live in `fs_chunk`. Reads merge changed
chunks from the delta layer with unchanged chunks from the base layer.

The base fallback is part of the file's integrity contract. Implementations MUST
validate persistent identity before falling back to base bytes. By default,
reads also fail if the recorded base size or time fingerprint changes. An
application MAY select an explicit `BaseValidator` content policy (for example,
hash validation); that policy does not replace persistent identity checks. Snapshot/restore of the main
delta database is supported only when the same unchanged base path is available.
A database containing partial-origin rows is not portable on its own:
`vfs backup` rejects it unless `--materialize` folds the base bytes in,
`vfs materialize` produces a portable copy, and `vfs integrity`
exposes the dependency via `--require-portable` and `--check-base`.

#### Tables: `fs_partial_origin` and `fs_chunk_override`

```sql
CREATE TABLE fs_partial_origin (
  delta_ino INTEGER PRIMARY KEY,
  base_ino INTEGER NOT NULL,
  base_path TEXT NOT NULL,
  base_size INTEGER NOT NULL,
  base_fingerprint_size INTEGER NOT NULL DEFAULT -1,
  base_mtime INTEGER NOT NULL DEFAULT 0,
  base_mtime_nsec INTEGER NOT NULL DEFAULT 0,
  base_ctime INTEGER NOT NULL DEFAULT 0,
  base_ctime_nsec INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
)

CREATE TABLE fs_chunk_override (
  delta_ino INTEGER NOT NULL,
  chunk_index INTEGER NOT NULL,
  PRIMARY KEY (delta_ino, chunk_index)
)
```

Partial-origin stays opt-in. Coverage pinning the mode includes remount,
main-DB snapshot restore, unlink cleanup/whiteout behavior, hardlink survival,
rename plus `readdir_plus`, truncate shrink/extend, and base drift detection.
It SHOULD NOT become the default until the FUSE/CLI torture and POSIX gates
pass with the policy enabled.

### Consistency Rules

1. A whiteout MUST be removed when a new file is created at that path
2. A whiteout MUST be created when deleting a file that exists in the base layer
3. The `parent_path` MUST be correctly derived from `path`
4. Whiteouts only affect overlay lookups, not the underlying base filesystem
5. When copying a file from base to delta, the origin mapping MUST be stored
6. Mount-local overlay inode numbers MUST remain stable across copy-up
7. Legacy overlay formats MUST be refused before current-format schema operations
8. Partial-origin sidecars MUST survive while an unlinked private inode has open handles and be collected when that inode is reaped

## File history ownership

The core stores current file state, overlay lineage and immutable SQLite artifacts.
Sandbox's file-management layer owns operation evidence, restore decisions and
child-view exchange. Core mutations have no row journal or relational history
snapshots. SQLite transactions, WAL and crash recovery remain required.

Unmapped content has `fs_chunk.refcount = 0`. `collect_unused_chunks` drains
acknowledged writes and deletes only these chunks; live mappings retain their
references. A frozen database owns its own rows and is never edited by collection
in the writable source. Collection is separate from operation-history retention.

## Revision History

Older entries describe the upstream history, including features removed by this fork.

### Version 0.12 (RED PANDA)

Remove internal row journals, relational root snapshots, replay APIs and their
configuration. Keep current-state SQLite transactions and immutable artifacts.
File history is owned by the Sandbox file-management layer. Versions 0.11 and
earlier are refused. Unused chunk collection is independent of history retention.

### Version 0.11 (RED PANDA)

Filesystem-only local format: no KV/tool tables, encryption opening options,
remote chunk source or public handoff API. Version 0.10 and earlier are refused.
Journal, root snapshots, replay and internal history metadata are retained.


### Version 0.10 (fork)

- Persist immutable UUID namespaces in `fs_config.filesystem_id`.
- Qualify each delta-created file's identity with its owning namespace.
- Preserve hard-link and copy-up identity across stacked views and reconstruction.
- Refuse older ambiguous origins and current missing/malformed namespaces.

### Version 0.9 (fork)

- Replace `fs_origin.base_ino` and snapshot origins with `base_identity` text.
- Preserve stable origin identities in the row-delta journal.
- Establish 0.9 as the minimum accepted format; refuse older process-local identities.
- Add Windows ordinal name semantics and lazy native base identity.

### Version 0.8

- Replaced operation-specific journal payloads with replayable row-delta
  post-images grouped atomically by `txn_id`
- Added immutable root snapshots for inode, namespace, content mapping,
  symlink, overlay, and provenance state
- Normalized inline bytes to content-addressed digests in both journal and
  snapshot rows; snapshots pin chunks explicitly, journal retention is
  derived from the digests retained rows name
- Dropped v0.7's `fs_journal_chunk` pin table and the journal's `txn_id`
  index: both charged every mutating commit for facts that are derivable
  offline
- Added history epoch, validity, and floor markers
- Defined exact reconstruction at complete transaction boundaries, including
  future trimming, inode allocator preservation, refcount repair, and
  integrity verification
- Made journal retention snapshot-covered and made pack/revert establish fresh
  history floors
- The v0.7 → v0.8 migration discards the old non-replayable journal and
  establishes one migration root at epoch 1 through sequence 0
- Added the `chunks_hollow` marker in `fs_config` identifying a remote
  metadata artifact whose chunk bytes live in object storage; hollow
  databases refuse every mutation-capable open

### Version 0.7

- Replaced inline chunk blobs in `fs_data` with raw 32-byte BLAKE3 digest
  mappings into the deduplicated `fs_chunk` content-addressed store
- Added exact live-mapping refcounts and journal-aware retention for
  zero-refcount chunk rows
- Added the thin logical-operation journal: one row per operation,
  transaction grouping, digest-only chunk references, and explicit retention
  state
- The v0.6 → v0.7 migration preserves byte-identical file contents while
  deduplicating equal chunks; copy migration additionally re-chunks into the
  current 64 KiB layout

### Version 0.6

- Added `fs_session_metadata(key, value)` for persistent handoff state
- `vfs pack` increments the `generation` key and reads `seeded_paths` into its manifest
- The v0.5 → v0.6 migration is additive and creates the metadata table in the same schema transaction
- Copy migration preserves session metadata when present

### Version 0.5

- Default chunk size raised to 64 KiB for new filesystems (`chunk_size` in `fs_config`)
- Added inline storage for dense regular files at or below `inline_threshold` (current default 16 KiB): `data_inline` and `storage_kind` columns on `fs_inode`, with layout rules and consistency checks
- Added `inline_threshold` to the required `fs_config` keys
- Added partial-origin overlay mode tables (`fs_partial_origin`, `fs_chunk_override`) behind the opt-in `--partial-origin` CLI policy
- Whiteout schema requires `parent_path`; legacy `fs_whiteout(path, created_at)` rows are synthesized on migration
- Schema migrations are keyed by `PRAGMA user_version`; `vfs migrate` lands
  any supported old schema at the current version in place, and `--copy`
  rebuilds with the current chunk layout

### Version 0.4

- Added nanosecond timestamp precision for `atime`, `mtime`, and `ctime`
- Added `atime_nsec`, `mtime_nsec`, `ctime_nsec` columns to `fs_inode` table (DEFAULT 0 for backward compatibility)
- Nanosecond precision enables correct NFS `wcc_data` cache invalidation when multiple operations occur within the same second
- Added POSIX special file support (FIFOs, character devices, block devices, sockets)
- Added `rdev` column to `fs_inode` table for device major/minor numbers
- Added `S_IFIFO`, `S_IFCHR`, `S_IFBLK`, `S_IFSOCK` file type constants to Mode Encoding
- Updated stat query to include `rdev` field

### Version 0.3

- Added `fs_origin` table to Overlay Filesystem for tracking copy-up origin inodes
- Origin tracking ensures consistent inode numbers after copy-up (similar to Linux overlayfs `trusted.overlay.origin`)

### Version 0.2

- Added Overlay Filesystem section with `fs_whiteout` table for copy-on-write semantics
- Whiteout table includes `parent_path` column with index for efficient O(1) child lookups
- Added `nlink` column to `fs_inode` table to store link count directly
- Link count is now maintained in the inode rather than computed via COUNT(*) on `fs_dentry`

### Version 0.1

- Added `fs_config` table for filesystem-level configuration
- Changed `fs_data` table to use fixed-size chunks with `chunk_index` instead of variable-size chunks with `offset` and `size`
- Added `chunk_size` configuration option (default: 4096 bytes)
- Added "Reading a File at Offset" operation for efficient partial reads
- Chunk-based storage enables efficient random access reads without loading entire files

### Version 0.0

- Initial specification
- Tool call audit trail (`tool_calls` table)
- Virtual filesystem (`fs_inode`, `fs_dentry`, `fs_data`, `fs_symlink` tables)
- Key-value store (`kv_store` table)
