[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingPlainTextForPassword", "GuestCredentialFile", Justification = "This parameter is a protected credential file path, never a password value.")]
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$VmName,

    [Parameter(Mandatory)]
    [guid]$VmUuid,

    [Parameter(Mandatory)]
    [string]$ExpectedSshFingerprint,

    [Parameter(Mandatory)]
    [string]$GuestCredentialFile,

    [Parameter(Mandatory)]
    [string]$KnownHosts,

    [ValidateRange(1024, 65535)]
    [int]$SshHostPort = 2222,

    [string]$GuestUser = "actor"
)

$ErrorActionPreference = "Stop"
if (-not $IsWindows) { throw "Public guest restart requires Windows" }
if ($VmName -notmatch '^[A-Za-z0-9._-]+$') { throw "VM name is invalid" }
if ($GuestUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw "Guest user is invalid" }
if ($ExpectedSshFingerprint -notmatch '^SHA256:[A-Za-z0-9+/]{20,}={0,2}$') { throw "Expected SSH fingerprint is invalid" }

$vbox = Get-Command VBoxManage.exe -ErrorAction SilentlyContinue
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
if (-not $vbox -or -not $python -or -not $ssh) { throw "VirtualBox, Python, and OpenSSH clients are required" }
$vmInfo = & $vbox.Source showvminfo $VmName --machinereadable
if ($LASTEXITCODE -ne 0) { throw "Cannot read the requested VM" }
$uuidLine = $vmInfo | Where-Object { $_ -match '^UUID="([0-9A-Fa-f-]+)"$' } | Select-Object -First 1
$actualUuid = [guid]([regex]::Match($uuidLine, '^UUID="([0-9A-Fa-f-]+)"$').Groups[1].Value)
if ($actualUuid -ne $VmUuid) { throw "Registered VM UUID does not match the expected identity" }
$stateLine = $vmInfo | Where-Object { $_ -match '^VMState="(.+)"$' } | Select-Object -First 1
if ([regex]::Match($stateLine, '^VMState="(.+)"$').Groups[1].Value -ne "running") { throw "VM must be running for guest restart" }

& (Join-Path $PSScriptRoot "Enroll-SshHostKey.ps1") -ExpectedFingerprint $ExpectedSshFingerprint `
    -KnownHostsPath $KnownHosts -SshPort $SshHostPort -PythonExecutable $python.Source | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Console-verified SSH enrollment failed" }

$common = @("-p", "$SshHostPort", "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=$KnownHosts")
$common += @("-o", "ConnectTimeout=5", "-o", "ConnectionAttempts=1")
$askpass = Join-Path ([IO.Path]::GetTempPath()) ("sandbox-restart-askpass-" + [guid]::NewGuid() + ".cmd")
[IO.File]::WriteAllText($askpass, "@type `"$GuestCredentialFile`"`r`n", [Text.UTF8Encoding]::new($false))
$oldDisplay = $env:DISPLAY
$oldAskpass = $env:SSH_ASKPASS
$oldAskpassRequire = $env:SSH_ASKPASS_REQUIRE
try {
    $env:DISPLAY = ":0"
    $env:SSH_ASKPASS = $askpass
    $env:SSH_ASKPASS_REQUIRE = "force"
    $oldBootId = ((& $ssh.Source @common "${GuestUser}@127.0.0.1" "cat /proc/sys/kernel/random/boot_id") -join "`n").Trim()
    if ($LASTEXITCODE -ne 0 -or $oldBootId -notmatch '^[0-9a-f-]{36}$') { throw "Cannot capture the pre-restart boot ID" }
    $password = (Get-Content -LiteralPath $GuestCredentialFile -Raw).TrimEnd("`r", "`n")
    "$password`n" | & $ssh.Source @common "${GuestUser}@127.0.0.1" "sudo -S --prompt= systemctl reboot" 2>$null

    $newBootId = ""
    for ($attempt = 1; $attempt -le 60; $attempt++) {
        Start-Sleep -Seconds 5
        $candidate = ((& $ssh.Source @common "${GuestUser}@127.0.0.1" "cat /proc/sys/kernel/random/boot_id" 2>$null) -join "`n").Trim()
        if ($LASTEXITCODE -eq 0 -and $candidate -match '^[0-9a-f-]{36}$' -and $candidate -ne $oldBootId) {
            $newBootId = $candidate
            break
        }
    }
} finally {
    $env:DISPLAY = $oldDisplay
    $env:SSH_ASKPASS = $oldAskpass
    $env:SSH_ASKPASS_REQUIRE = $oldAskpassRequire
    Remove-Item -LiteralPath $askpass -Force -ErrorAction SilentlyContinue
}
if (-not $newBootId) { throw "Guest did not return with a different boot ID" }
Write-Output "Public guest restart: PASS: $VmName ($VmUuid)"
