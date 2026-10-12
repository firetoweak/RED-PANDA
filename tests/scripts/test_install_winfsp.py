import json
import os
from pathlib import Path
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows installer script")
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_winfsp.ps1"

# Substitute every system-changing operation. Execute the real script with
# Windows PowerShell 5.1 to exercise version selection and installer arguments.
DRIVER = r"""
$case = $env:REDPANDA_WINFSP_TEST_CASE | ConvertFrom-Json
$state = @{ Downloads = @(); Arguments = $null; Sdk = $case.sdk; Runtime = [bool]$case.version; Error = $null; ApiCalls = 0 }
function Get-ItemProperty {
    param($Path, $ErrorAction)
    if ($Path -like '*Uninstall*') {
        if ($case.version) {
            [pscustomobject]@{ DisplayName = 'WinFsp 2026 Beta4'; DisplayVersion = $case.version }
        }
    } else {
        [pscustomobject]@{ InstallDir = 'C:\Program Files (x86)\WinFsp' }
    }
}
function Test-Path {
    param($LiteralPath, $PathType)
    if ($LiteralPath -like '*winfsp-x64.dll') { return $state.Runtime }
    return $state.Sdk
}
function New-Item { param($ItemType, [switch]$Force, $Path) }
function Invoke-RestMethod {
    param($Uri)
    $state.ApiCalls++
    foreach ($version in @('2.2.26215', '2.1.25156')) {
        [pscustomobject]@{
            tag_name = $version; draft = $false; prerelease = ($version -eq '2.2.26215')
            assets = @([pscustomobject]@{
                name = "winfsp-$version.msi"
                browser_download_url = "https://example.test/winfsp-$version.msi"
            })
        }
    }
}
function Invoke-WebRequest {
    param([switch]$UseBasicParsing, $Uri, $OutFile)
    $state.Downloads += $Uri
}
function Get-AuthenticodeSignature {
    param($LiteralPath)
    [pscustomobject]@{ Status = $case.signature }
}
function Start-Process {
    param($FilePath, $Verb, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    $state.Arguments = $ArgumentList
    $state.Sdk = ($case.exit_code -in @(0, 3010))
    $state.Runtime = $state.Sdk
    [pscustomobject]@{ ExitCode = $case.exit_code }
}
try { & $env:REDPANDA_WINFSP_TEST_SCRIPT -Developer:$case.developer } catch { $state.Error = $_.Exception.Message }
$state | ConvertTo-Json -Compress
"""


def run_installer(*, version=None, sdk=False, developer=False, exit_code=0, signature="Valid"):
    case = {"version": version, "sdk": sdk, "developer": developer, "exit_code": exit_code, "signature": signature}
    env = os.environ | {
        "REDPANDA_WINFSP_TEST_CASE": json.dumps(case),
        "REDPANDA_WINFSP_TEST_SCRIPT": str(SCRIPT),
    }
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", DRIVER],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout.splitlines()[-1])


def test_existing_newer_driver_receives_matching_sdk_without_downgrade():
    state = run_installer(version="2.2.26215", developer=True)
    assert state["Error"] is None
    assert state["Downloads"] == ["https://example.test/winfsp-2.2.26215.msi"]
    assert 'ADDLOCAL=F.Main,F.User,F.Developer' in state["Arguments"]
    assert '/norestart' in state["Arguments"]
    assert '/L*v "' in state["Arguments"]
    assert '.msi" ADDLOCAL=' in state["Arguments"]


def test_fresh_install_chooses_stable_release():
    state = run_installer()
    assert state["Error"] is None
    assert state["Downloads"] == ["https://example.test/winfsp-2.1.25156.msi"]
    assert 'ADDLOCAL=F.Main,F.User ' in state["Arguments"]
    assert 'F.Developer' not in state["Arguments"]


@pytest.mark.parametrize("developer,sdk", [(False, False), (True, True)])
def test_complete_installation_skips_network_and_installer(developer, sdk):
    state = run_installer(version="2.2.26215", sdk=sdk, developer=developer)
    assert state["ApiCalls"] == 0
    assert state["Downloads"] == []
    assert state["Arguments"] is None


def test_installer_failure_reports_code_and_log():
    state = run_installer(version="2.2.26215", developer=True, exit_code=1603)
    assert "1603" in state["Error"]
    assert "winfsp-install.log" in state["Error"]


def test_unavailable_matching_version_does_not_install_another_version():
    state = run_installer(version="9.9.99999", developer=True)
    assert "9.9.99999" in state["Error"]
    assert state["Downloads"] == []
    assert state["Arguments"] is None


def test_invalid_signature_never_launches_installer():
    state = run_installer(signature="HashMismatch")
    assert "signature" in state["Error"]
    assert state["Arguments"] is None
