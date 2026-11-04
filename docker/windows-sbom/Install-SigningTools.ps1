[CmdletBinding()]
param(
    [string]$Destination = "C:\tools\signing"
)

$ErrorActionPreference = "Stop"
if ([IntPtr]::Size -ne 8) { throw "Signing tools setup requires a 64-bit PowerShell process." }
    $packages = [ordered]@{
    "microsoft.windows.sdk.buildtools" = [ordered]@{
        version = "10.0.22621.3233"
        sha512 = "AlozSUw/OZxzPX2PDv2tQD0ez9uh1i+AnCUM8jLpYV+YdICXLLVl723+Cz7huVPpRPx28kFC2G5JW3dsmcYh8Q=="
    }
    "microsoft.artifactsigning.client" = [ordered]@{
        version = "1.0.146"
        sha512 = "/G0hG/VDIS6psW23b5t1V/ORIgn0PVAOtGj9w+hkPmAuI4pempfwlW50E2GtHscnz9+5Qz3hbC4mcb7ej2Yn1A=="
    }
}
$temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("signing-tools-" + [guid]::NewGuid())
$null = New-Item -ItemType Directory -Path $temporary
try {
    foreach ($package in $packages.GetEnumerator()) {
        $version = [string]$package.Value.version
        $archive = Join-Path $temporary "$($package.Key).nupkg"
        $uri = "https://api.nuget.org/v3-flatcontainer/$($package.Key)/$version/$($package.Key).$version.nupkg"
        Write-Output "Installing $($package.Key) $version..."
        & curl.exe --fail --silent --show-error --location --retry 3 --output $archive $uri
        if ($LASTEXITCODE -ne 0) { throw "Download of $($package.Key) failed with curl exit code $LASTEXITCODE." }
        $target = Join-Path $Destination $package.Key
        & (Join-Path $PSScriptRoot "Install-VerifiedNugetPackage.ps1") -ArchivePath $archive `
            -ExpectedSha512 ([string]$package.Value.sha512) -Destination $target
    }
    $signTool = @(Get-ChildItem -LiteralPath (Join-Path $Destination "microsoft.windows.sdk.buildtools") -Recurse -Filter signtool.exe |
        Where-Object { $_.Directory.Name -eq "x64" })
    $client = @(Get-ChildItem -LiteralPath (Join-Path $Destination "microsoft.artifactsigning.client") -Recurse -Filter Azure.CodeSigning.Dlib.dll |
        Where-Object { $_.Directory.Name -eq "x64" })
    if ($signTool.Count -ne 1 -or $client.Count -ne 1) {
        throw "Expected exactly one x64 SignTool and signing client; found $($signTool.Count) and $($client.Count)."
    }
    $dotnet = Join-Path $env:ProgramFiles "dotnet\dotnet.exe"
    if (-not (Test-Path -LiteralPath $dotnet)) { throw ".NET 8 x64 runtime is required for Artifact Signing." }
    $runtimes = & $dotnet --list-runtimes
    if ($LASTEXITCODE -ne 0 -or -not ($runtimes -match '^Microsoft.NETCore.App 8\.')) {
        throw ".NET 8 x64 runtime is required for Artifact Signing."
    }
    $toolSignature = Get-AuthenticodeSignature -LiteralPath $signTool[0].FullName
    if ($toolSignature.Status -ne "Valid" -or $toolSignature.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation(?:,|$)') {
        throw "Downloaded SignTool did not have a valid Microsoft Authenticode signature."
    }
    & $signTool[0].FullName /? | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "SignTool readiness check failed with exit code $LASTEXITCODE." }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class SbomSigningLibrary {
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr LoadLibraryEx(string file, IntPtr reserved, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    public static extern bool FreeLibrary(IntPtr module);
}
'@
    $module = [SbomSigningLibrary]::LoadLibraryEx($client[0].FullName, [IntPtr]::Zero, 0x1100)
    if ($module -eq [IntPtr]::Zero) {
        throw "Artifact Signing client failed to load: Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error())."
    }
    if (-not [SbomSigningLibrary]::FreeLibrary($module)) {
        throw "Artifact Signing client failed to unload: Win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error())."
    }
    $manifest = [ordered]@{
        packages = $packages
        signTool = $signTool[0].FullName
        dlib = $client[0].FullName
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Destination "tools.json") -Encoding utf8
    Write-Output "Signing tool manifest: $(Join-Path $Destination 'tools.json')"
    Write-Output "Readiness verified: Microsoft-signed x64 SignTool, .NET 8, and native signing client."
} finally {
    Remove-Item -LiteralPath $temporary -Recurse -Force
}
