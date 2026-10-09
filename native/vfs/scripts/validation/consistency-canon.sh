#!/usr/bin/env bash
#
# Consistency-canon census (architecture.md section 7).
#
# Mechanically checks the structural canon rules and prints one PASS/FAIL row
# per rule. Exit 0 only when every row passes. Wired into scripts/gate.sh as
# a gate step (VAL-CONS-014).
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

FAILURES=0

row() {
    status="$1"
    name="$2"
    detail="$3"
    printf '%-4s %-24s %s\n' "$status" "$name" "$detail"
    if [ "$status" = "FAIL" ]; then
        FAILURES=$((FAILURES + 1))
    fi
}

check_python() {
    name="$1"
    shift
    if detail="$(python3 "$@" 2>&1)"; then
        row PASS "$name" "$detail"
    else
        row FAIL "$name" "$detail"
    fi
}

# --- crate DAG -------------------------------------------------------------
check_python "dag" - <<'PY'
import json, subprocess, sys

expected = {
    "vfs-core": set(),
    "vfs-fuse": {"vfs-core"},
    "vfs-mount": {"vfs-core", "vfs-fuse"},
}

meta = json.loads(
    subprocess.run(
        ["cargo", "metadata", "--format-version=1", "--no-deps"],
        check=True, text=True, stdout=subprocess.PIPE,
    ).stdout
)
packages = {pkg["name"]: pkg for pkg in meta["packages"]}
if set(packages) != set(expected):
    print(f"workspace members {sorted(packages)} != expected {sorted(expected)}")
    sys.exit(1)
for name, wanted in expected.items():
    actual = {
        dep["name"] for dep in packages[name]["dependencies"]
        if dep["name"].startswith("vfs-")
    }
    if actual != wanted:
        print(f"{name} first-party deps {sorted(actual)} != expected {sorted(wanted)}")
        sys.exit(1)
print("three-crate workspace; first-party edges match README.md")
PY

# --- sealed transport surfaces ----------------------------------------------
check_python "sealed-surfaces" - <<'PY'
import re, sys
from pathlib import Path

problems = []

fuse = Path("crates/vfs-fuse/src/lib.rs").read_text(encoding="utf-8")
fuse_pubs = re.findall(r"^pub .*$", fuse, re.M)
if fuse_pubs != ["pub use adapter::{mount, FuseMountOptions, SessionHandle};"]:
    problems.append(f"fuse lib.rs pub surface drifted: {fuse_pubs}")
if re.search(r"^pub mod", fuse, re.M):
    problems.append("fuse lib.rs exposes a pub mod")

if problems:
    print("; ".join(problems))
    sys.exit(1)
print("fuse={mount, FuseMountOptions, SessionHandle}")
PY

# --- shared cfg(test)-aware scanner used by the next three checks -----------
export CANON_SCAN_HELPER="
import re

def nontest_lines(path):
    '''Yield (lineno, line) for lines outside cfg(test)/cfg(all(test, ..))
    module blocks, tracked by brace depth from the attribute.'''
    lines = path.read_text(encoding='utf-8').splitlines()
    in_test = False
    depth = 0
    pending_attr = False
    for lineno, line in enumerate(lines, 1):
        if not in_test and re.match(r'\s*#\[cfg\((all\()?test\b', line):
            pending_attr = True
            continue
        if pending_attr:
            if '{' in line:
                in_test = True
                depth = line.count('{') - line.count('}')
                pending_attr = False
                if depth <= 0:
                    in_test = False
                continue
            if re.match(r'\s*(#\[|mod\s+\w+\s*;|//)', line) or not line.strip():
                # attribute stack, sibling-module declaration, or comment
                if re.match(r'\s*mod\s+\w+\s*;', line):
                    pending_attr = False
                continue
            pending_attr = False
        if in_test:
            depth += line.count('{') - line.count('}')
            if depth <= 0:
                in_test = False
            continue
        yield lineno, line

"

# --- line-count cap ----------------------------------------------------------
check_python "line-count-cap" - <<'PY'
import os, re, sys
from pathlib import Path

exec(os.environ["CANON_SCAN_HELPER"])

CAP = 2500
offenders = []
for path in sorted(Path("crates").rglob("*.rs")):
    parts = path.parts
    if "target" in parts:
        continue
    if path.name == "tests.rs" or "tests" in parts:
        continue
    code = sum(
        1 for _, line in nontest_lines(path)
        if line.strip() and not line.strip().startswith("//")
    )
    if code > CAP:
        offenders.append(f"{path}:{code}")
if offenders:
    print(f"non-test code lines over {CAP}: {', '.join(offenders)}")
    sys.exit(1)
print(f"no production file over {CAP} non-test code lines")
PY

# --- tracing-only logging ----------------------------------------------------
check_python "tracing-only-logging" - <<'PY'
import os, re, sys
from pathlib import Path

exec(os.environ["CANON_SCAN_HELPER"])

# Libraries use tracing; only build scripts emit print macros.
def allowed(path):
    return path.name == "build.rs"

offenders = []
for path in sorted(Path("crates").rglob("*.rs")):
    parts = path.parts
    if "target" in parts or path.name == "tests.rs" or "tests" in parts:
        continue
    if allowed(path):
        continue
    for lineno, line in nontest_lines(path):
        if re.search(r"\b(eprintln!|println!|eprint!|print!)\(", line):
            offenders.append(f"{path}:{lineno}")
if offenders:
    print(f"print macros outside build scripts: {', '.join(offenders)}")
    sys.exit(1)
print("print macros confined to build scripts and test code")
PY

# --- env reads at the config edge ---------------------------------------------
check_python "env-read-locations" - <<'PY'
import os, re, sys
from pathlib import Path

exec(os.environ["CANON_SCAN_HELPER"])

# Env reads live in each crate's config module (canon section 7 item 3).
ALLOWED_PREFIXES = (
    "crates/vfs-core/src/config/",
    "crates/vfs-fuse/src/adapter/config.rs",
)

offenders = []
for path in sorted(Path("crates").rglob("*.rs")):
    parts = path.parts
    if "target" in parts or path.name in ("tests.rs", "build.rs") or "tests" in parts:
        continue
    if path.as_posix().startswith(ALLOWED_PREFIXES):
        continue
    for lineno, line in nontest_lines(path):
        if re.search(r"\benv::(var|var_os)\b", line):
            offenders.append(f"{path}:{lineno}")
if offenders:
    print(f"env reads outside config modules: {', '.join(offenders)}")
    sys.exit(1)
print("runtime env reads confined to config modules")
PY

# --- await_holding_lock lint -----------------------------------------------------
check_python "await-holding-lock" - <<'PY'
import sys
from pathlib import Path

root = Path("Cargo.toml").read_text(encoding="utf-8")
if 'await_holding_lock = "deny"' not in root:
    print("workspace lints table does not deny clippy::await_holding_lock")
    sys.exit(1)
missing = [
    crate for crate in ("vfs-core", "vfs-fuse", "vfs-mount")
    if "workspace = true" not in Path(f"crates/{crate}/Cargo.toml").read_text(encoding="utf-8").split("[lints]")[-1]
    or "[lints]" not in Path(f"crates/{crate}/Cargo.toml").read_text(encoding="utf-8")
]
if missing:
    print(f"crates missing [lints] workspace = true: {missing}")
    sys.exit(1)
print('clippy::await_holding_lock = "deny" via [workspace.lints]; all crates opt in')
PY

# --- lock-order headers -------------------------------------------------------------
check_python "lock-order-headers" - <<'PY'
import re, sys
from pathlib import Path

modules = [
    "crates/vfs-core/src/fs/vfs/batcher.rs",
    "crates/vfs-fuse/src/adapter/cache.rs",
    "crates/vfs-core/src/fs/overlay/mod.rs",
    "crates/vfs-core/src/semantics/handles.rs",
]
missing = [
    m for m in modules
    if not re.search(r"lock order", Path(m).read_text(encoding="utf-8"), re.I)
]
if missing:
    print(f"multi-lock modules missing a lock-order header: {missing}")
    sys.exit(1)
print("batcher, adapter cache, overlay, handle table document their lock order")
PY

# --- docs layout ------------------------------------------------------------------
check_python "docs-layout" - <<'PY'
import sys
from pathlib import Path

problems = []
for doc in ("TESTING.md", "SPEC.md", "WINDOWS.md"):
    if not Path("docs", doc).is_file():
        problems.append(f"docs/{doc} missing")
    if Path(doc).exists():
        problems.append(f"{doc} must live under docs/, found at repo root")
if problems:
    print("; ".join(problems))
    sys.exit(1)
print("user docs live under docs/ (TESTING, SPEC, WINDOWS)")
PY

# --- schema DDL centralization (VAL-CORE-014) ---------------------------------------
check_python "ddl-census" "$SCRIPT_DIR/ddl-census.py"

# --- changelog ----------------------------------------------------------------------
check_python "changelog" - <<'PY'
import sys
from pathlib import Path

path = Path("CHANGELOG.md")
if not path.is_file() or not path.read_text(encoding="utf-8").strip():
    print("CHANGELOG.md missing or empty at repo root")
    sys.exit(1)
print("CHANGELOG.md present at repo root")
PY

if [ "$FAILURES" -ne 0 ]; then
    printf '\nconsistency-canon: %d rule(s) failed\n' "$FAILURES"
    exit 1
fi
printf '\nconsistency-canon: all rules PASS\n'
