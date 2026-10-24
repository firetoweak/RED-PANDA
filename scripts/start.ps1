$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot "konden-env\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Run scripts\setup.ps1 first to prepare konden."
}
Push-Location $projectRoot
try {
    & $python -m scripts.launch @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
