$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $projectRoot "konden-env\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $projectRoot "web\node_modules\vite\bin\vite.js") -PathType Leaf)) {
    throw "Run scripts\setup.ps1 first to prepare the project environment."
}
Push-Location $projectRoot
try {
    & $python -m scripts.launch --dev @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
