[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingPlainTextForPassword", "GuestCredentialFile", Justification = "This parameter is a protected credential file path, never a password value.")]
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$VmName,

    [Parameter(Mandatory)]
    [guid]$VmUuid,

    [Parameter(Mandatory)]
    [string]$GuestCredentialFile,

    [Parameter(Mandatory)]
    [string]$OutputPath,

    [string]$GuestUser = "actor"
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) { throw "Public guest diagnostics require Windows" }
if ($VmName -notmatch '^[A-Za-z0-9._-]+$') { throw "VM name is invalid" }
if ($GuestUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw "Guest user is invalid" }
if (-not [IO.Path]::IsPathFullyQualified($GuestCredentialFile) -or
    -not [IO.Path]::IsPathFullyQualified($OutputPath)) {
    throw "Diagnostic paths must be absolute"
}
if (-not (Test-Path -LiteralPath $GuestCredentialFile -PathType Leaf)) {
    throw "Guest credential file is missing"
}

$vbox = Get-Command VBoxManage.exe -ErrorAction SilentlyContinue
if (-not $vbox) { throw "VBoxManage.exe is missing" }
$vmInfo = & $vbox.Source showvminfo $VmName --machinereadable
if ($LASTEXITCODE -ne 0) { throw "Cannot read the requested VM" }
$uuidLine = $vmInfo | Where-Object { $_ -match '^UUID="([0-9A-Fa-f-]+)"$' } | Select-Object -First 1
$actualUuid = [guid]([regex]::Match($uuidLine, '^UUID="([0-9A-Fa-f-]+)"$').Groups[1].Value)
if ($actualUuid -ne $VmUuid) { throw "Registered VM UUID does not match the expected identity" }
$stateLine = $vmInfo | Where-Object { $_ -match '^VMState="(.+)"$' } | Select-Object -First 1
$vmState = [regex]::Match($stateLine, '^VMState="(.+)"$').Groups[1].Value

$lines = [Collections.Generic.List[string]]::new()
$lines.Add("vm_uuid=$actualUuid")
$lines.Add("vm_state=$vmState")
if ($vmState -ne "running") {
    $lines.Add("guest_control=unavailable-vm-not-running")
    $lines | Set-Content -LiteralPath $OutputPath
    exit 0
}

function Invoke-GuestProbe {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Executable,
        [string[]]$Arguments = @()
    )

    $output = & $vbox.Source guestcontrol $VmName run `
        "--username=$GuestUser" "--passwordfile=$GuestCredentialFile" `
        "--exe=$Executable" --wait-stdout --wait-stderr -- @Arguments 2>&1
    $exitCode = $LASTEXITCODE
    $lines.Add("=== $Name ===")
    $lines.Add("exit_code=$exitCode")
    foreach ($line in @($output)) { $lines.Add([string]$line) }
}

Invoke-GuestProbe -Name "identity" -Executable "/usr/bin/id" -Arguments @("-u", "-n")
Invoke-GuestProbe -Name "os-release" -Executable "/bin/cat" -Arguments @("/etc/os-release")
Invoke-GuestProbe -Name "root-filesystem" -Executable "/usr/bin/findmnt" -Arguments @("-n", "-o", "SOURCE,FSTYPE", "/")
Invoke-GuestProbe -Name "system-state" -Executable "/usr/bin/systemctl" -Arguments @("is-system-running")
Invoke-GuestProbe -Name "default-target" -Executable "/usr/bin/systemctl" -Arguments @("get-default")
Invoke-GuestProbe -Name "ssh-service" -Executable "/usr/bin/systemctl" -Arguments @("is-active", "ssh.service")
Invoke-GuestProbe -Name "guest-utils-service" -Executable "/usr/bin/systemctl" -Arguments @("is-active", "virtualbox-guest-utils.service")
Invoke-GuestProbe -Name "base-state" -Executable "/usr/bin/stat" -Arguments @("-c", "%F %a %U:%G %n", "/var/lib/projectbuddy/base-bootstrap-state.json")
Invoke-GuestProbe -Name "base-receipt" -Executable "/usr/bin/stat" -Arguments @("-c", "%F %a %U:%G %n", "/var/lib/projectbuddy/base-bootstrap-receipt.json")
Invoke-GuestProbe -Name "base-finalizer" -Executable "/usr/bin/stat" -Arguments @("-c", "%F %a %U:%G %n", "/usr/local/sbin/sandbox-base-bootstrap-finalize")
Invoke-GuestProbe -Name "base-timer" -Executable "/usr/bin/systemctl" -Arguments @("status", "sandbox-base-bootstrap-receipt.timer", "--no-pager", "--lines=0")

$lines | Set-Content -LiteralPath $OutputPath
$global:LASTEXITCODE = 0