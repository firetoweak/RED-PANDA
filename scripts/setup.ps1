$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Push-Location $projectRoot
try {
    if (-not (Get-Command rg -ErrorAction SilentlyContinue)) {
        throw "Install ripgrep first: winget install --id BurntSushi.ripgrep.MSVC --exact"
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

    $environmentDir = Join-Path $projectRoot "helperme-env"
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
        throw "helperme-env is not using the project Python. Delete helperme-env and run this script again."
    }

    & $uv pip install --python $python pip -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Python dependencies."
    }

    $envFile = Join-Path $projectRoot ".env"
    if (Test-Path -LiteralPath $envFile -PathType Leaf) {
        $envStatus = & $python -c "from dotenv import dotenv_values; v=dotenv_values('.env').get('FERRO_MASTER_KEY'); print('ok' if v and 'replace_with_' not in v.lower() else 'missing')"
        if ($LASTEXITCODE -ne 0 -or $envStatus -ne "ok") {
            throw "Set FERRO_MASTER_KEY in .env, then run this script again."
        }
    }
    else {
        $masterKey = & $python -c "import secrets; print('fgw_' + secrets.token_hex(16))"
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to generate the Ferro access key."
        }
        $envContent = "FERRO_MASTER_KEY=`"$masterKey`"`n"
        [System.IO.File]::WriteAllText(
            $envFile,
            $envContent,
            [System.Text.UTF8Encoding]::new($false)
        )
    }

    $ferroVersion = & $python -c "from helperme.llm.config import FERRO_VERSION; print(FERRO_VERSION)"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to read the pinned Ferro version."
    }
    $installDir = Join-Path $projectRoot ".tools\ferro"
    New-Item -ItemType Directory -Force -Path $installDir | Out-Null

    $oldVersion = $env:FERROGW_VERSION
    $oldInstallDir = $env:FERROGW_INSTALL_DIR
    $oldNoModifyPath = $env:FERROGW_NO_MODIFY_PATH
    try {
        $env:FERROGW_VERSION = $ferroVersion
        $env:FERROGW_INSTALL_DIR = $installDir
        $env:FERROGW_NO_MODIFY_PATH = "1"
        Invoke-RestMethod "https://get.ferrolabs.ai/install.ps1" | Invoke-Expression
    }
    finally {
        if ($null -eq $oldVersion) { Remove-Item Env:FERROGW_VERSION -ErrorAction SilentlyContinue }
        else { $env:FERROGW_VERSION = $oldVersion }
        if ($null -eq $oldInstallDir) { Remove-Item Env:FERROGW_INSTALL_DIR -ErrorAction SilentlyContinue }
        else { $env:FERROGW_INSTALL_DIR = $oldInstallDir }
        if ($null -eq $oldNoModifyPath) { Remove-Item Env:FERROGW_NO_MODIFY_PATH -ErrorAction SilentlyContinue }
        else { $env:FERROGW_NO_MODIFY_PATH = $oldNoModifyPath }
    }

    $binary = Join-Path $installDir "ferrogw.exe"
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        throw "Ferro install failed: $binary was not found"
    }
    $gatewayKey = & $python -c "from dotenv import dotenv_values; print(dotenv_values('.env')['FERRO_MASTER_KEY'])"
    if ($LASTEXITCODE -ne 0 -or -not $gatewayKey) {
        throw "Failed to read FERRO_MASTER_KEY."
    }
    Write-Host "Setup complete."
    Write-Host "Add DEEPSEEK_API_KEY to .env before starting Ferro."
    $gatewayLogin = & $python -c "from urllib.parse import urlsplit; from helperme.llm.config import FERRO_BASE_URL; print(f'http://localhost:{urlsplit(FERRO_BASE_URL).port}/login')"
    Write-Host "Gateway login: $gatewayLogin"
    Write-Host "Gateway key: $gatewayKey"
}
finally {
    Pop-Location
}
