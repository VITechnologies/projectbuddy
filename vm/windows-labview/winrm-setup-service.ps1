<#
.SYNOPSIS
Idempotent WinRM-enabling logic, run by the SandboxSbomWinRmSetup Windows
Service (NSSM-wrapped, registered during the Windows Setup specialize pass)
on every boot until it succeeds, then self-disables.

.DESCRIPTION
This is the service-based alternative to FirstLogonCommands/RunOnce-driven
WinRM setup, built to test whether a genuine SCM service survives the
#188 restart where RunOnce did not (issue #191's closing comment: neither
RunOnce-from-specialize-pass nor RunOnce-from-oobeSystem-pass survived
it -- both are tied to a single logon-sequencing event this restart
interrupts). A service is registered once under
HKLM\SYSTEM\CurrentControlSet\Services\ (the same registry area the
already-proven-safe wuauserv/UsoSvc StartupType disable writes to during
the specialize pass -- NOT the RunOnce key Windows Setup's own
continuation machinery collides with) and is started fresh by the Service
Control Manager on literally every subsequent boot, regardless of how
many times the guest restarts before this logic finally gets a
chance to run to completion.

Mirrors winrm-setup-installer.nsi's six steps exactly, using the
production firewall rule name (SandboxSbom-WinRM-HTTP, matching
provision-windows-guest.py's cmd_verify_winrm_setup checklist) rather
than that prototype's placeholder name. Logs every step's command, exit
code, and output to the same persistent log file convention
(C:\vm-provisioning\winrm-setup-install.log) in append mode, since this
may run across several boots before succeeding.

Idempotency: Test-WinRmSetupComplete runs the exact same checklist as
cmd_verify_winrm_setup before and after attempting the fix. If already
satisfied on entry, exits immediately (fast no-op on every boot after
the first success) instead of re-running steps needlessly. After a
successful fix, re-verifies, writes a completion marker, and disables
its own service (Automatic startup would otherwise harmlessly re-run
this fast no-op check forever, but disabling it is cleaner and makes
"has this ever succeeded" trivially inspectable from service state
alone).
#>
$ErrorActionPreference = 'Stop'

$logPath = 'C:\vm-provisioning\winrm-setup-install.log'
$markerPath = 'C:\vm-provisioning\winrm-setup-complete.marker'
$serviceName = 'SandboxSbomWinRmSetup'

function Write-SetupLog {
    param([string]$Text)
    Add-Content -LiteralPath $logPath -Value $Text -Encoding UTF8
}

function Invoke-SetupStep {
    param(
        [string]$Description,
        [scriptblock]$Action
    )
    Write-SetupLog "---- $Description ----"
    try {
        $output = & $Action 2>&1 | Out-String
        Write-SetupLog "output:`r`n$output"
        Write-SetupLog "RESULT: OK"
    } catch {
        Write-SetupLog "output:`r`n$($_.Exception.Message)"
        Write-SetupLog "RESULT: FAILED"
        throw
    }
}

function Test-WinRmSetupComplete {
    # Same checklist as provision-windows-guest.py's cmd_verify_winrm_setup;
    # kept in sync deliberately so "setup complete" means the same thing
    # whether checked from this guest-local service or remotely over WinRM.
    $checks = [System.Collections.Generic.List[bool]]::new()

    foreach ($svcName in 'wuauserv', 'UsoSvc', 'InstallService') {
        $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
        $checks.Add($svc -and $svc.StartType -eq 'Disabled')
    }

    $policyValue = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name LocalAccountTokenFilterPolicy -ErrorAction SilentlyContinue).LocalAccountTokenFilterPolicy
    $checks.Add($policyValue -eq 1)

    $publicProfiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq 'Public'
    $checks.Add($null -eq $publicProfiles -or @($publicProfiles).Count -eq 0)

    $allowUnencrypted = (Get-Item WSMan:\localhost\Service\AllowUnencrypted -ErrorAction SilentlyContinue).Value
    $checks.Add($allowUnencrypted -eq 'true')

    $basicAuth = (Get-Item WSMan:\localhost\Service\Auth\Basic -ErrorAction SilentlyContinue).Value
    $checks.Add($basicAuth -eq 'true')

    $maxMem = (Get-Item WSMan:\localhost\Shell\MaxMemoryPerShellMB -ErrorAction SilentlyContinue).Value
    $checks.Add($maxMem -eq '1024')

    $fwRule = Get-NetFirewallRule -Name SandboxSbom-WinRM-HTTP -ErrorAction SilentlyContinue
    if ($fwRule) {
        $portFilter = $fwRule | Get-NetFirewallPortFilter
        $checks.Add(($fwRule.Enabled -eq 'True') -and ($fwRule.Direction -eq 'Inbound') -and ($fwRule.Action -eq 'Allow') -and ($portFilter.LocalPort -eq '5985'))
    } else {
        $checks.Add($false)
    }

    $winrmSvc = Get-Service -Name WinRM
    $checks.Add(($winrmSvc.StartType -eq 'Automatic') -and ($winrmSvc.Status -eq 'Running'))

    return -not ($checks -contains $false)
}

Write-SetupLog ""
Write-SetupLog "==== winrm-setup-service.ps1 run starting: $(Get-Date -Format o) ===="

if (Test-Path -LiteralPath $markerPath) {
    Write-SetupLog "Completion marker already present; nothing to do."
    exit 0
}

if (Test-WinRmSetupComplete) {
    Write-SetupLog "Already fully configured on entry (no marker was written by an earlier run, but every check already passes)."
    Set-Content -LiteralPath $markerPath -Value (Get-Date -Format o)
    exit 0
}

try {
    Invoke-SetupStep "Disable update-related services (wuauserv/UsoSvc/InstallService)" {
        "wuauserv", "UsoSvc", "InstallService" | ForEach-Object {
            Stop-Service $_ -Force -ErrorAction SilentlyContinue
            Set-Service $_ -StartupType Disabled -ErrorAction SilentlyContinue
        }
        Write-Output "done"
    }

    # Running as LocalSystem (this service's configured account), not an
    # elevated-but-not-SYSTEM session -- unlike winrm-setup-installer.nsi's
    # own comment here (written for epic #198's VNC-elevated-admin context,
    # where PowerShell's registry provider threw
    # UnauthorizedAccessException), SYSTEM should not need the reg.exe
    # workaround. Kept anyway for consistency and because it is already
    # proven to work in every context tested so far.
    Invoke-SetupStep "Set LocalAccountTokenFilterPolicy" {
        & reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f
        if ($LASTEXITCODE -ne 0) { throw "reg.exe add exited $LASTEXITCODE" }
        Write-Output "done"
    }

    Invoke-SetupStep "Enable-PSRemoting" {
        Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null
        Write-Output "done"
    }

    Invoke-SetupStep "Set network category to Private" {
        Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq 'Public' | Set-NetConnectionProfile -NetworkCategory Private
        Write-Output "done"
    }

    Invoke-SetupStep "Configure WSMan listener options and firewall rule" {
        Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $true
        Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $true
        Set-Item -Path WSMan:\localhost\Shell\MaxMemoryPerShellMB -Value 1024
        New-NetFirewallRule -Name SandboxSbom-WinRM-HTTP -DisplayName "Sandbox SBOM WinRM HTTP" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null
        Write-Output "done"
    }

    Invoke-SetupStep "Set WinRM to automatic startup and restart it" {
        Set-Service WinRM -StartupType Automatic
        Restart-Service WinRM
        Write-Output "done"
    }
} catch {
    Write-SetupLog "winrm-setup-service.ps1 run FAILED: $($_.Exception.Message)"
    Write-SetupLog "Will retry on next boot (service remains Automatic; no marker written)."
    exit 1
}

if (-not (Test-WinRmSetupComplete)) {
    Write-SetupLog "All steps reported OK, but post-verification still found a failing check. Will retry on next boot."
    exit 1
}

Set-Content -LiteralPath $markerPath -Value (Get-Date -Format o)
Write-SetupLog "All checks pass; wrote completion marker. Disabling $serviceName (no need to run again)."
try {
    & "$PSScriptRoot\nssm.exe" set $serviceName Start SERVICE_DISABLED | Out-Null
} catch {
    Write-SetupLog "Could not disable $serviceName (non-fatal, setup is already complete): $($_.Exception.Message)"
}
Write-SetupLog "==== winrm-setup-service.ps1 run finished: $(Get-Date -Format o) ===="
exit 0
