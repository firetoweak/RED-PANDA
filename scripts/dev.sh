#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python="$project_root/konden-env/bin/python"
if [ ! -x "$python" ] || [ ! -f "$project_root/web/node_modules/vite/bin/vite.js" ]; then
  printf '%s\n' "Run sh scripts/setup.sh first to prepare the project environment." >&2
  exit 1
fi
cd "$project_root"
exec "$python" -m scripts.launch --dev "$@"
