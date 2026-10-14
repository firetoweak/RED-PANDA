param(
    [string]$OutputRoot = '.tools/windows-hook-probe',
    [string]$Toolchain = '.tools/llvm-mingw-20261006-ucrt-x86_64/bin'
)
$ErrorActionPreference = 'Stop'
$pin = '1dbd4ea281d73d478aa8d09a8b83fbd265f715f7'
$source = $PSScriptRoot
New-Item -ItemType Directory -Force $OutputRoot | Out-Null
$output = (Resolve-Path $OutputRoot).Path
$cc = (Resolve-Path "$Toolchain/x86_64-w64-mingw32-clang++.exe").Path
$ar = (Resolve-Path "$Toolchain/llvm-ar.exe").Path
$detours = Join-Path $output 'detours'
if (-not (Test-Path -LiteralPath $detours)) {
    New-Item -ItemType Directory $detours | Out-Null
    & git -C $detours init -q
    if ($LASTEXITCODE) { throw 'git init failed' }
    & git -C $detours remote add origin https://github.com/microsoft/Detours.git
    if ($LASTEXITCODE) { throw 'git remote failed' }
    & git -C $detours fetch --depth 1 origin $pin
    if ($LASTEXITCODE) { throw 'git fetch failed' }
    & git -C $detours checkout -q --detach FETCH_HEAD
    if ($LASTEXITCODE) { throw 'git checkout failed' }
}
$head = (& git -C $detours rev-parse HEAD).Trim()
if ($head -ne $pin) { throw "Detours revision differs from pinned source: $head" }
Set-Content -LiteralPath "$output/DETOURS_COMMIT.txt" -Value $head -Encoding ascii
$common = @('-std=c++17','-O2','-DDETOUR_DEBUG=0','-D_WIN32_WINNT=0x0A00','-DWIN32_LEAN_AND_MEAN','-Wno-unknown-pragmas','-Wno-macro-redefined','-Wno-ignored-attributes','-I',"$detours/src")
New-Item -ItemType Directory -Force "$output/objects" | Out-Null
$objects = @()
foreach ($unit in @('detours','modules','disasm','image','creatwth','disolx86','disolx64','disolia64','disolarm','disolarm64')) {
    $object = "$output/objects/$unit.o"
    & $cc @common -c "$detours/src/$unit.cpp" -o $object
    if ($LASTEXITCODE) { throw "Detours compilation failed: $unit" }
    $objects += $object
}
& $ar rcs "$output/detours.a" @objects
if ($LASTEXITCODE) { throw 'Detours archive failed' }
foreach ($name in @('hook','hook-attach-failure')) {
    $flags = @('-static','-shared')
    if ($name -eq 'hook-attach-failure') { $flags += '-DPROBE_ATTACH_FAILURE' }
    & $cc @common @flags "$source/hook.cpp" "$source/hook.def" "$output/detours.a" -o "$output/$name.dll"
    if ($LASTEXITCODE) { throw "Hook compilation failed: $name" }
}
foreach ($name in @('launcher','launcher-no-policy')) {
    $flags = @('-static','-municode')
    if ($name -eq 'launcher-no-policy') { $flags += '-DPROBE_NO_POLICY' }
    & $cc @common @flags "$source/launcher.cpp" "$output/detours.a" -o "$output/$name.exe"
    if ($LASTEXITCODE) { throw "Launcher compilation failed: $name" }
}
foreach ($name in @('cases.py','run_probe.py','git_control.py','limits_probe.py','benchmark.py','metadata.py')) {
    $from = [IO.Path]::GetFullPath("$source/$name")
    $to = [IO.Path]::GetFullPath("$output/$name")
    if ($from -ne $to) { Copy-Item -LiteralPath $from -Destination $to }
}
Write-Output "Built Windows x64 hook prototype at $output"
