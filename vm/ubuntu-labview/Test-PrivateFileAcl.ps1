[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Path,

    [switch]$AllowInheritance
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "Private-file ACL verification requires Windows"
}
if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    throw "Private file does not exist"
}

$acl = Get-Acl -LiteralPath $Path
if (-not $AllowInheritance -and -not $acl.AreAccessRulesProtected) {
    throw "Private file inherits access rules"
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$systemSid = "S-1-5-18"
$expectedSids = @($currentSid, $systemSid)
$rules = @($acl.GetAccessRules($true, [bool]$AllowInheritance, [Security.Principal.SecurityIdentifier]))
if ($rules.Count -ne $expectedSids.Count) {
    throw "Private file has an unexpected access-rule count"
}

foreach ($sid in $expectedSids) {
    $matching = @($rules | Where-Object {
        $_.IdentityReference.Value -eq $sid -and
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
            [Security.AccessControl.FileSystemRights]::FullControl
    })
    if ($matching.Count -ne 1) {
        throw "Private file ACL does not grant exactly one full-control rule to an expected identity"
    }
}

Write-Output "Private-file ACL: PASS"