#!/usr/bin/env sh
set -eu
project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python="$project_root/redpanda-env/bin/python"
if [ ! -x "$python" ]; then
  printf '%s\n' "Run sh scripts/setup.sh first to prepare RED PANDA." >&2
  exit 1
fi
cd "$project_root"
exec "$python" -m scripts.launch "$@"
