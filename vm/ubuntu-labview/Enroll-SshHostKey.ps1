[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^SHA256:[A-Za-z0-9+/]{20,}={0,2}$')]
    [string]$ExpectedFingerprint,
    [Parameter(Mandatory)]
    [string]$KnownHostsPath,
    [string]$SshHost = "127.0.0.1",
    [ValidateRange(1, 65535)]
    [int]$SshPort = 2222,
    [string]$PythonExecutable = "python"
)

$ErrorActionPreference = "Stop"
$helper = Join-Path $PSScriptRoot "enroll-ssh-host-key.py"
$aclVerifier = Join-Path $PSScriptRoot "Test-PrivateFileAcl.ps1"
$keyscanExecutable = "ssh-keyscan"
$gitKeyscan = Join-Path $env:ProgramFiles "Git\usr\bin\ssh-keyscan.exe"
if (Test-Path -LiteralPath $gitKeyscan -PathType Leaf) {
    $keyscanExecutable = $gitKeyscan
}

& $PythonExecutable $helper `
    --host $SshHost `
    --port $SshPort `
    --expected-fingerprint $ExpectedFingerprint `
    --out $KnownHostsPath `
    --keyscan-executable $keyscanExecutable
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($account in @($identity, "NT AUTHORITY\SYSTEM")) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            $account,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $KnownHostsPath -AclObject $acl
    & $aclVerifier -Path $KnownHostsPath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Known-hosts ACL verification failed"
    }
} catch {
    Remove-Item -LiteralPath $KnownHostsPath -Force -ErrorAction SilentlyContinue
    throw
}