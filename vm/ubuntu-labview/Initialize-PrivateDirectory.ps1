[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "Private-directory initialization requires Windows"
}

New-Item -ItemType Directory -Path $Path -Force | Out-Null
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = [Security.Principal.SecurityIdentifier]::new("S-1-5-18")
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($currentSid, $systemSid)) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid,
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit,
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow
    )
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $Path -AclObject $acl

$verifier = Join-Path $PSScriptRoot "Test-PrivateDirectoryAcl.ps1"
& $verifier -Path $Path | Out-Null

Write-Output "Private-directory ACL: PASS"
