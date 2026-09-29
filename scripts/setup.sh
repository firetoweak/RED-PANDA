#!/usr/bin/env sh
set -eu

project_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_root"

if ! command -v rg >/dev/null 2>&1; then
  printf '%s\n' "Install ripgrep first: https://github.com/BurntSushi/ripgrep#installation" >&2
  exit 1
fi

python_version=$(tr -d '\r\n' < .python-version)
uv_version=0.12.19
tools_dir="$project_root/.tools"
uv_dir="$tools_dir/uv"
managed_python_dir="$tools_dir/python"
uv_cache_dir="$tools_dir/uv-cache"
mkdir -p "$uv_dir" "$managed_python_dir" "$uv_cache_dir"

export UV_PYTHON_INSTALL_DIR="$managed_python_dir"
export UV_CACHE_DIR="$uv_cache_dir"
export UV_PYTHON_INSTALL_BIN=0
export UV_PYTHON_INSTALL_REGISTRY=0
export UV_MANAGED_PYTHON=1
export UV_NO_MODIFY_PATH=1

uv="$uv_dir/uv"
installed_uv_version=""
if [ -x "$uv" ]; then
  installed_uv_version=$("$uv" --version 2>/dev/null || true)
fi
case "$installed_uv_version" in
  "uv $uv_version"*) ;;
  *)
    installer_url="https://releases.astral.sh/github/uv/releases/download/$uv_version/uv-installer.sh"
    curl -fsSL "$installer_url" | env UV_INSTALL_DIR="$uv_dir" UV_NO_MODIFY_PATH=1 sh
    ;;
esac
if [ ! -x "$uv" ]; then
  printf '%s\n' "uv install failed: $uv was not found" >&2
  exit 1
fi

"$uv" python install "$python_version" --install-dir "$managed_python_dir" --no-bin

environment_dir="$project_root/helperme-env"
venv_python="$environment_dir/bin/python"
create_environment=0
if [ ! -x "$venv_python" ]; then
  create_environment=1
else
  base_prefix=$("$venv_python" -c 'import sys; print(sys.base_prefix)')
  case "$base_prefix/" in
    "$managed_python_dir"/*) ;;
    *) create_environment=1 ;;
  esac
fi
if [ "$create_environment" -eq 1 ]; then
  "$uv" venv --clear --python "$python_version" --managed-python "$environment_dir"
fi
python="$venv_python"
environment_python_version=$($python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
base_prefix=$($python -c 'import sys; print(sys.base_prefix)')
case "$base_prefix/" in
  "$managed_python_dir"/*) ;;
  *)
    printf '%s\n' "helperme-env is not using the project Python. Delete helperme-env and run this script again." >&2
    exit 1
    ;;
esac
if [ "$environment_python_version" != "$python_version" ]; then
  printf '%s\n' "helperme-env must use Python $python_version. Delete helperme-env and run this script again." >&2
  exit 1
fi

"$uv" pip install --python "$python" pip -r requirements.txt

if [ -f .env ]; then
  env_status=$($python -c 'from dotenv import dotenv_values; v=dotenv_values(".env").get("FERRO_MASTER_KEY"); print("ok" if v and "replace_with_" not in v.lower() else "missing")')
  if [ "$env_status" != "ok" ]; then
    printf '%s\n' "Set FERRO_MASTER_KEY in .env, then run this script again." >&2
    exit 1
  fi
else
  master_key="fgw_$($python -c 'import secrets; print(secrets.token_hex(16))')"
  umask 077
  FERRO_SETUP_MASTER_KEY="$master_key" "$python" - <<'PY'
import os
from pathlib import Path

value = os.environ["FERRO_SETUP_MASTER_KEY"]
quoted = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
Path(".env").write_text(f"FERRO_MASTER_KEY={quoted}\n", encoding="utf-8")
Path(".env").chmod(0o600)
PY
fi

ferro_version=$($python -c 'from helperme.llm.config import FERRO_VERSION; print(FERRO_VERSION)')
install_dir="$project_root/.tools/ferro"
mkdir -p "$install_dir"
installer=$(curl -fsSL https://get.ferrolabs.ai/install.sh)
printf '%s\n' "$installer" | env \
  FERROGW_VERSION="$ferro_version" \
  FERROGW_INSTALL_DIR="$install_dir" \
  FERROGW_NO_MODIFY_PATH=1 \
  sh

if [ ! -x "$install_dir/ferrogw" ]; then
  printf '%s\n' "Ferro install failed: $install_dir/ferrogw was not found" >&2
  exit 1
fi

gateway_key=$($python -c 'from dotenv import dotenv_values; print(dotenv_values(".env")["FERRO_MASTER_KEY"])')
printf '%s\n' "Setup complete."
printf '%s\n' "Add DEEPSEEK_API_KEY to .env before starting Ferro."
gateway_login=$($python -c 'from urllib.parse import urlsplit; from helperme.llm.config import FERRO_BASE_URL; print(f"http://localhost:{urlsplit(FERRO_BASE_URL).port}/login")')
printf '%s\n' "Gateway login: $gateway_login"
printf '%s\n' "Gateway key: $gateway_key"
