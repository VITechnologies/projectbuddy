[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "Private-file initialization requires Windows"
}
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Private file does not exist"
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$acl = [Security.AccessControl.FileSecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($currentSid, $systemSid)) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $Path -AclObject $acl

$verifier = Join-Path $PSScriptRoot "Test-PrivateFileAcl.ps1"
& $verifier -Path $Path | Out-Null

Write-Output "Private-file ACL: PASS"