#!/usr/bin/env sh
# Fork format contract: old artifacts are refused without mutation.
set -eu
DIR="$(cd "$(dirname "$0")" && pwd)"
CLI_DIR="$(cd "$DIR/.." && pwd)"
FIXTURES="$DIR/fixtures/migrate"
ROOT="$(mktemp -d "${TMPDIR:-/tmp}/vfs-format-refusal.XXXXXX")"
trap 'rm -rf "$ROOT"' EXIT INT TERM
fail() { echo "FAILED: $*"; exit 1; }
if [ -n "${VFS_BIN:-}" ]; then
    BIN="$VFS_BIN"
else
    cargo build --quiet --manifest-path "$CLI_DIR/Cargo.toml"
    BIN="$CLI_DIR/../../target/debug/vfs"
fi
echo -n "TEST fork format refusal... "
digest() {
    python3 - "$1" <<'PY'
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
}
for name in v0_0 v0_2 v0_4 v0_6 v0_7; do
    DB="$ROOT/$name.db"
    cp "$FIXTURES/$name.db" "$DB"
    BEFORE="$(digest "$DB")"
    for mode in open migrate dry-run copy; do
        case "$mode" in
            open) set -- fs "$DB" ls / ;;
            migrate) set -- migrate "$DB" ;;
            dry-run) set -- migrate "$DB" --dry-run ;;
            copy) set -- migrate "$DB" --copy "$ROOT/target.db" --verify ;;
        esac
        if "$BIN" "$@" >"$ROOT/result" 2>&1; then
            fail "$name: $mode accepted an unsupported artifact"
        fi
        grep -q 'cannot be converted' "$ROOT/result" || fail "$name: missing refusal reason"
        [ "$(digest "$DB")" = "$BEFORE" ] || fail "$name: $mode changed the original database"
        [ ! -e "$ROOT/target.db" ] || fail "$name: copy created a target"
    done
done
DB="$ROOT/encrypted.db"
cp "$FIXTURES/v0_4-encrypted.db" "$DB"
BEFORE="$(digest "$DB")"
if "$BIN" migrate "$DB" --key 00112233445566778899aabbccddeeff00112233445566778899aabbccddeeff --cipher aes256gcm >"$ROOT/result" 2>&1; then
    fail "encrypted old artifact was accepted"
fi
grep -q 'cannot be converted' "$ROOT/result" || fail "encrypted refusal reason missing"
[ "$(digest "$DB")" = "$BEFORE" ] || fail "encrypted original changed"
echo "OK"
