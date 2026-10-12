param([switch]$Developer)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$toolsDir = Join-Path $projectRoot ".tools"
$installed = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -like 'WinFsp *' } | Select-Object -First 1
$registry = Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\WinFsp' -ErrorAction SilentlyContinue
$installDir = if ($registry.InstallDir) { $registry.InstallDir } else { Join-Path ${env:ProgramFiles(x86)} 'WinFsp' }

function Test-WinFspComponents {
    if (-not (Test-Path -LiteralPath (Join-Path $installDir 'bin\winfsp-x64.dll') -PathType Leaf)) {
        return $false
    }
    if ($Developer) {
        return (Test-Path -LiteralPath (Join-Path $installDir 'inc\winfsp\winfsp.h') -PathType Leaf) -and
            (Test-Path -LiteralPath (Join-Path $installDir 'lib\winfsp-x64.lib') -PathType Leaf)
    }
    return $true
}

if ($installed -and (Test-WinFspComponents)) {
    Write-Host "WinFsp $($installed.DisplayVersion): requested components are already installed."
    return
}

# Use the installed version when adding SDK files; never downgrade a driver.
$headers = @{ Accept = 'application/vnd.github+json' }
if ($env:GH_TOKEN) { $headers.Authorization = "Bearer $env:GH_TOKEN" }
$releases = Invoke-RestMethod -Uri 'https://api.github.com/repos/winfsp/winfsp/releases?per_page=100' -Headers $headers
if ($installed) {
    $packageName = "winfsp-$($installed.DisplayVersion).msi"
    $release = $releases | Where-Object { $_.assets.name -contains $packageName } | Select-Object -First 1
    if (-not $release) {
        throw "Cannot find the official installer for WinFsp $($installed.DisplayVersion). Use its original installer to add the requested components."
    }
    $asset = $release.assets | Where-Object { $_.name -eq $packageName }
    Write-Host "Completing WinFsp $($installed.DisplayVersion) components."
} else {
    $release = $releases | Where-Object { -not $_.draft -and -not $_.prerelease } | Select-Object -First 1
    $asset = $release.assets | Where-Object { $_.name -match '^winfsp-[0-9.]+\.msi$' } | Select-Object -First 1
    if (-not $asset) {
        throw 'Cannot find an official stable WinFsp installer.'
    }
    Write-Host "Installing WinFsp $($release.tag_name)."
}

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
$packagePath = Join-Path $toolsDir $asset.name
$logPath = Join-Path $toolsDir 'winfsp-install.log'
Invoke-WebRequest -UseBasicParsing -Uri $asset.browser_download_url -OutFile $packagePath
$signature = Get-AuthenticodeSignature -LiteralPath $packagePath
if ($signature.Status -ne 'Valid') {
    throw "WinFsp installer signature is not valid: $($signature.Status)."
}

Write-Host 'Confirm the Windows administrator prompt to install. The computer will not restart automatically.'
$features = 'F.Main,F.User'
if ($Developer) { $features += ',F.Developer' }
$arguments = "/i `"$packagePath`" ADDLOCAL=$features /qb /norestart /L*v `"$logPath`""
$process = Start-Process -FilePath 'msiexec.exe' -Verb RunAs -ArgumentList $arguments -Wait -PassThru
if ($process.ExitCode -notin @(0, 3010)) {
    throw "WinFsp installation failed (exit code $($process.ExitCode)). Log: $logPath"
}
if (-not (Test-WinFspComponents)) {
    throw "WinFsp requested components were not found in $installDir. Log: $logPath"
}
Write-Host "WinFsp requested components are ready: $installDir"
if ($process.ExitCode -eq 3010) {
    Write-Host 'Restart Windows before using the sandbox.'
}
