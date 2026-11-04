[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Path
)

$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    throw "Private-directory ACL verification requires Windows"
}
if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    throw "Private directory does not exist"
}

$acl = Get-Acl -LiteralPath $Path
if (-not $acl.AreAccessRulesProtected) {
    throw "Private directory inherits access rules"
}

$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$systemSid = "S-1-5-18"
$expectedSids = @($currentSid, $systemSid)
$rules = @($acl.GetAccessRules($true, $false, [Security.Principal.SecurityIdentifier]))
if ($rules.Count -ne $expectedSids.Count) {
    throw "Private directory has an unexpected access-rule count"
}

foreach ($sid in $expectedSids) {
    $matching = @($rules | Where-Object {
        $_.IdentityReference.Value -eq $sid -and
        $_.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and
        ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -eq
            [Security.AccessControl.FileSystemRights]::FullControl -and
        ($_.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ContainerInherit) -ne 0 -and
        ($_.InheritanceFlags -band [Security.AccessControl.InheritanceFlags]::ObjectInherit) -ne 0 -and
        $_.PropagationFlags -eq [Security.AccessControl.PropagationFlags]::None
    })
    if ($matching.Count -ne 1) {
        throw "Private directory ACL does not grant one inheritable full-control rule to an expected identity"
    }
}

Write-Output "Private-directory ACL: PASS"