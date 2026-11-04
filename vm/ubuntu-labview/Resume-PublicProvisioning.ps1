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
    [string]$EvidenceDirectory,

    [Parameter(Mandatory)]
    [string]$KnownHosts,

    [ValidateRange(1024, 65535)]
    [int]$SshHostPort = 2222,

    [ValidateRange(1, 7200)]
    [int]$WaitSeconds = 3600
)

$ErrorActionPreference = "Stop"
$snapshotName = "labview2026-q3-pro-installed-preactivation"

if (-not $IsWindows) { throw "Public provisioning resume requires Windows" }
if ($VmName -notmatch '^[A-Za-z0-9._-]+$') { throw "VM name is invalid" }
foreach ($path in @($GuestCredentialFile, $EvidenceDirectory, $KnownHosts)) {
    if (-not [IO.Path]::IsPathFullyQualified($path)) { throw "Resume paths must be absolute" }
}
if (-not (Test-Path -LiteralPath $GuestCredentialFile -PathType Leaf)) {
    throw "Guest credential file is missing"
}

$vbox = Get-Command VBoxManage.exe -ErrorAction SilentlyContinue
if (-not $vbox) { throw "VBoxManage.exe is missing" }
$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) { throw "Python is missing" }

$vmInfo = & $vbox.Source showvminfo $VmName --machinereadable
if ($LASTEXITCODE -ne 0) { throw "Cannot read the requested VM" }
$uuidLine = $vmInfo | Where-Object { $_ -match '^UUID="([0-9A-Fa-f-]+)"$' } | Select-Object -First 1
$actualUuid = [guid]([regex]::Match($uuidLine, '^UUID="([0-9A-Fa-f-]+)"$').Groups[1].Value)
if ($actualUuid -ne $VmUuid) { throw "Registered VM UUID does not match the expected identity" }
$stateLine = $vmInfo | Where-Object { $_ -match '^VMState="(.+)"$' } | Select-Object -First 1
$vmState = [regex]::Match($stateLine, '^VMState="(.+)"$').Groups[1].Value
if ($vmState -notin @("poweroff", "running")) {
    throw "VM must be powered off or running to resume public provisioning"
}

$snapshots = & $vbox.Source snapshot $VmName list --machinereadable 2>&1
$noSnapshots = $LASTEXITCODE -ne 0 -and ($snapshots -join "`n").Trim() -eq "This machine does not have any snapshots"
if ($LASTEXITCODE -ne 0 -and -not $noSnapshots) { throw "Cannot inspect snapshot inventory" }
if ($snapshots | Where-Object { $_ -match '^SnapshotName(?:-\d+)?="(.+)"$' -and $Matches[1] -eq $snapshotName }) {
    throw "Target preactivation snapshot already exists"
}

if ($vmState -eq "poweroff") {
    & $vbox.Source startvm $VmName --type headless
    if ($LASTEXITCODE -ne 0) { throw "Cannot start the identity-verified VM" }
}

$adapter = Join-Path $PSScriptRoot "complete-public-provisioning.py"
& $python.Source $adapter `
    --vm-name $VmName --vm-uuid $VmUuid.ToString() `
    --guest-password-file $GuestCredentialFile --evidence-dir $EvidenceDirectory `
    --known-hosts $KnownHosts --ssh-port $SshHostPort --wait-seconds $WaitSeconds `
    --resume-base-receipt --run
if ($LASTEXITCODE -ne 0) { throw "Public provisioning adapter exited $LASTEXITCODE" }