$ErrorActionPreference = "Stop"

function Test-RunnerAdministrator {
    if (-not $IsWindows) {
        throw "The administrator preflight is only supported on Windows."
    }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } finally {
        $identity.Dispose()
    }
}

function Assert-RunnerAdministrator {
    if (-not (Test-RunnerAdministrator)) {
        throw "Self-hosted Windows runner is not elevated. Restart the runner from an administrator terminal (or configure its service with an elevated administrator token), then rerun. Administrator group membership alone is insufficient."
    }
    Write-Host "Administrator preflight passed: the self-hosted Windows runner process has an elevated administrator token."
}

Assert-RunnerAdministrator
