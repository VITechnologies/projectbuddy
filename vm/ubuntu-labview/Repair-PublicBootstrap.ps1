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

if (-not $IsWindows) { throw "Public bootstrap repair requires Windows" }
if ($VmName -notmatch '^[A-Za-z0-9._-]+$') { throw "VM name is invalid" }
if ($GuestUser -notmatch '^[a-z_][a-z0-9_-]*$') { throw "Guest user is invalid" }
if ($ExpectedSshFingerprint -notmatch '^SHA256:[A-Za-z0-9+/]{20,}={0,2}$') {
    throw "Expected SSH fingerprint is invalid"
}
foreach ($path in @($GuestCredentialFile, $KnownHosts)) {
    if (-not [IO.Path]::IsPathFullyQualified($path)) { throw "Repair paths must be absolute" }
}
if (-not (Test-Path -LiteralPath $GuestCredentialFile -PathType Leaf)) {
    throw "Guest credential file is missing"
}
if (Test-Path -LiteralPath $KnownHosts) { throw "Known-hosts output already exists" }

$vbox = Get-Command VBoxManage.exe -ErrorAction SilentlyContinue
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$ssh = Get-Command ssh.exe -ErrorAction SilentlyContinue
$scp = Get-Command scp.exe -ErrorAction SilentlyContinue
if (-not $vbox -or -not $python -or -not $ssh -or -not $scp) {
    throw "VirtualBox, Python, and OpenSSH clients are required"
}
$vmInfo = & $vbox.Source showvminfo $VmName --machinereadable
if ($LASTEXITCODE -ne 0) { throw "Cannot read the requested VM" }
$uuidLine = $vmInfo | Where-Object { $_ -match '^UUID="([0-9A-Fa-f-]+)"$' } | Select-Object -First 1
$actualUuid = [guid]([regex]::Match($uuidLine, '^UUID="([0-9A-Fa-f-]+)"$').Groups[1].Value)
if ($actualUuid -ne $VmUuid) { throw "Registered VM UUID does not match the expected identity" }
$stateLine = $vmInfo | Where-Object { $_ -match '^VMState="(.+)"$' } | Select-Object -First 1
if ([regex]::Match($stateLine, '^VMState="(.+)"$').Groups[1].Value -ne "running") {
    throw "VM must be running for bootstrap repair"
}

$enrollment = Join-Path $PSScriptRoot "Enroll-SshHostKey.ps1"
& $enrollment -ExpectedFingerprint $ExpectedSshFingerprint -KnownHostsPath $KnownHosts `
    -SshHost 127.0.0.1 -SshPort $SshHostPort -PythonExecutable $python.Source | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Console-verified SSH enrollment failed" }

$temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("sandbox-bootstrap-repair-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temporaryDirectory | Out-Null
$renderedBootstrap = Join-Path $temporaryDirectory "base-bootstrap.sh"
$askpass = Join-Path $temporaryDirectory "askpass.cmd"
try {
    $template = Get-Content -LiteralPath (Join-Path $PSScriptRoot "base-bootstrap.sh") -Raw
    $rendered = $template.Replace("`r`n", "`n").Replace("`r", "`n")
    $rendered = $rendered.Replace("@@VM_NAME@@", $VmName).Replace("@@VM_UUID@@", $VmUuid.ToString())
    [IO.File]::WriteAllText($renderedBootstrap, $rendered, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($askpass, "@type `"$GuestCredentialFile`"`r`n", [Text.UTF8Encoding]::new($false))

    $oldDisplay = $env:DISPLAY
    $oldAskpass = $env:SSH_ASKPASS
    $oldAskpassRequire = $env:SSH_ASKPASS_REQUIRE
    try {
        $env:DISPLAY = ":0"
        $env:SSH_ASKPASS = $askpass
        $env:SSH_ASKPASS_REQUIRE = "force"
        $common = @(
            "-P", "$SshHostPort", "-o", "StrictHostKeyChecking=yes",
            "-o", "UserKnownHostsFile=$KnownHosts"
        )
        & $scp.Source @common $renderedBootstrap "${GuestUser}@127.0.0.1:/tmp/sandbox-base-bootstrap-repair.sh"
        if ($LASTEXITCODE -ne 0) { throw "Cannot upload bootstrap repair script" }

        $password = (Get-Content -LiteralPath $GuestCredentialFile -Raw).TrimEnd("`r", "`n")
        $remoteCommand = "chmod 700 /tmp/sandbox-base-bootstrap-repair.sh && sudo -S --prompt= /bin/bash -c '/bin/bash /tmp/sandbox-base-bootstrap-repair.sh && systemctl start sandbox-base-bootstrap-receipt.service' && rm -f /tmp/sandbox-base-bootstrap-repair.sh"
        "$password`n" | & $ssh.Source "-p" "$SshHostPort" "-o" "StrictHostKeyChecking=yes" `
            "-o" "UserKnownHostsFile=$KnownHosts" "${GuestUser}@127.0.0.1" $remoteCommand
        if ($LASTEXITCODE -ne 0) { throw "Strict-SSH bootstrap repair failed" }
    } finally {
        $env:DISPLAY = $oldDisplay
        $env:SSH_ASKPASS = $oldAskpass
        $env:SSH_ASKPASS_REQUIRE = $oldAskpassRequire
    }
} finally {
    if (Test-Path -LiteralPath $temporaryDirectory) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

$property = & $vbox.Source guestproperty get $VmName /SandboxSBOMReporting/SshHostEd25519Fingerprint
if ($LASTEXITCODE -ne 0 -or ($property -join "`n") -notmatch '^Value: SHA256:[A-Za-z0-9+/]{20,}={0,2}$') {
    throw "Bootstrap repair did not publish the VM SSH fingerprint property"
}
$publishedFingerprint = [regex]::Match(($property -join "`n"), '^Value: (SHA256:[A-Za-z0-9+/]{20,}={0,2})$').Groups[1].Value
if ($publishedFingerprint -ne $ExpectedSshFingerprint) {
    throw "Bootstrap repair published a different VM SSH fingerprint"
}
Write-Output "Public bootstrap repair: PASS: $VmName ($VmUuid)"