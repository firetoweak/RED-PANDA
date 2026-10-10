$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $projectRoot
try {
    if (-not (Get-Command rg -ErrorAction SilentlyContinue)) {
        throw "Install ripgrep first: winget install --id BurntSushi.ripgrep.MSVC --exact"
    }
    if (-not (Get-Command fd -ErrorAction SilentlyContinue) -and -not (Get-Command fdfind -ErrorAction SilentlyContinue)) {
        throw "Install fd first: winget install --id sharkdp.fd --exact"
    }

    $pythonVersion = (Get-Content (Join-Path $projectRoot ".python-version") -Raw).Trim()
    $uvVersion = "0.12.19"
    $toolsDir = Join-Path $projectRoot ".tools"
    $uvDir = Join-Path $toolsDir "uv"
    $managedPythonDir = Join-Path $toolsDir "python"
    $uvCacheDir = Join-Path $toolsDir "uv-cache"
    New-Item -ItemType Directory -Force -Path $uvDir, $managedPythonDir, $uvCacheDir | Out-Null

    $env:UV_PYTHON_INSTALL_DIR = $managedPythonDir
    $env:UV_CACHE_DIR = $uvCacheDir
    $env:UV_PYTHON_INSTALL_BIN = "0"
    $env:UV_PYTHON_INSTALL_REGISTRY = "0"
    $env:UV_MANAGED_PYTHON = "1"
    $env:UV_NO_MODIFY_PATH = "1"

    $uv = Join-Path $uvDir "uv.exe"
    $installedUvVersion = if (Test-Path -LiteralPath $uv -PathType Leaf) { & $uv --version } else { "" }
    if ($installedUvVersion -notlike "uv $uvVersion*") {
        $env:UV_INSTALL_DIR = $uvDir
        Invoke-RestMethod "https://releases.astral.sh/github/uv/releases/download/$uvVersion/uv-installer.ps1" | Invoke-Expression
    }
    if (-not (Test-Path -LiteralPath $uv -PathType Leaf)) {
        throw "uv install failed: $uv was not found"
    }

    & $uv python install $pythonVersion --install-dir $managedPythonDir --no-bin --no-registry
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install project Python $pythonVersion."
    }

    $environmentDir = Join-Path $projectRoot "redpanda-env"
    $python = Join-Path $environmentDir "Scripts\python.exe"
    $createEnvironment = -not (Test-Path -LiteralPath $python -PathType Leaf)
    if (-not $createEnvironment) {
        $basePrefix = & $python -c "import sys; print(sys.base_prefix)"
        $pythonRoot = [System.IO.Path]::GetFullPath($managedPythonDir).TrimEnd('\') + '\'
        if ($LASTEXITCODE -ne 0 -or -not $basePrefix.StartsWith($pythonRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            $createEnvironment = $true
        }
    }
    if ($createEnvironment) {
        & $uv venv --clear --python $pythonVersion --managed-python $environmentDir
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create the project Python environment."
        }
    }

    $environmentPythonVersion = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $basePrefix = & $python -c "import sys; print(sys.base_prefix)"
    $pythonRoot = [System.IO.Path]::GetFullPath($managedPythonDir).TrimEnd('\') + '\'
    if ($LASTEXITCODE -ne 0 -or $environmentPythonVersion -ne $pythonVersion -or -not $basePrefix.StartsWith($pythonRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "redpanda-env is not using the project Python. Delete redpanda-env and run this script again."
    }

    & $uv pip install --python $python pip -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Python dependencies."
    }

    & $python -m redpanda.initialize
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to initialize personal model configuration."
    }

    Write-Host "Setup complete. Fill provider settings in your personal connections.json."
}
finally {
    Pop-Location
}
