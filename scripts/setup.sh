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

printf '%s\n' "Setup complete."
printf '%s\n' "Copy .env.example to .env and fill in the settings for your model provider."
