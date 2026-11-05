[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingWriteHost", "", Justification = "Diagnostic progress output to the console, mirrors the bash builder's stderr logging; stdout carries only the final ISO path.")]
[CmdletBinding()]
param(
    [string]$Out = ""
)

<#
.SYNOPSIS
Builds the auxiliary ISO that carries the NSIS WinRM-setup installer onto a
fresh Windows guest (issue #200, epic #198), from a native Windows host.

.DESCRIPTION
Mirrors build-nsis-bootstrap-iso.sh (the Linux-host builder) exactly: per
epic #198's architecture principle ("NSIS shall be leveraged for injection
points"), this ISO must never carry anything beyond compiled NSIS
installer(s) -- no scripts, batch files, or other logic that bypasses NSIS
belongs here. The NSIS installer isn't a pre-built, operator-supplied .exe
like VIPM's -- it's compiled from tracked source (winrm-setup-installer.nsi)
via makensis.exe, every time this script runs, so the ISO's contents always
match the current source rather than a stale prebuilt binary (the compiled
.exe is gitignored repo-wide and must never be committed).

Builds the ISO using IMAPI2FS.MsftFileSystemImage, a native Windows COM
component that ships with every Windows release since Vista -- no external
tool (oscdimg, xorriso) is required. PowerShell's own COM method-invocation
binder cannot call IStream.Read directly on the result image's stream
(confirmed live: neither direct invocation nor a Marshal.GetTypedObjectForIUnknown
cast resolves it -- both still dispatch through the object's dynamic
__ComObject wrapper instead of the real interface), so the stream is drained
by a tiny embedded C# helper compiled in-process via Add-Type, which performs
a real, statically-bound interface call.

Prints the resulting ISO's absolute path on stdout as its last line (and
nothing else on stdout), so callers can capture it directly, e.g.:
    $isoPath = ./build-nsis-bootstrap-iso.ps1
#>

$ErrorActionPreference = "Stop"
$nsiSource = Join-Path $PSScriptRoot "winrm-setup-installer.nsi"
$volumeLabel = "SBOMNSISAUX"

if (-not (Test-Path -LiteralPath $nsiSource -PathType Leaf)) {
    Write-Error "NSIS installer source missing: $nsiSource" -ErrorAction Continue
    exit 2
}

$makensis = Get-Command makensis.exe -ErrorAction SilentlyContinue
if (-not $makensis) {
    $programFilesX86 = ${env:ProgramFiles(x86)}
    $fallback = if ($programFilesX86) { Join-Path $programFilesX86 "NSIS\makensis.exe" } else { $null }
    if ($fallback -and (Test-Path -LiteralPath $fallback -PathType Leaf)) {
        $makensisPath = $fallback
    } else {
        Write-Error "makensis.exe is required (install NSIS, e.g. 'choco install nsis')" -ErrorAction Continue
        exit 2
    }
} else {
    $makensisPath = $makensis.Source
}

$workDirectory = Join-Path ([IO.Path]::GetTempPath()) ("nsis-bootstrap-" + [guid]::NewGuid())
try {
    New-Item -ItemType Directory -Path $workDirectory -Force | Out-Null
    $nsiCopy = Join-Path $workDirectory (Split-Path -Leaf $nsiSource)
    Copy-Item -LiteralPath $nsiSource -Destination $nsiCopy

    & $makensisPath -V2 $nsiCopy | Write-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Error "makensis compilation failed with exit code $LASTEXITCODE" -ErrorAction Continue
        exit 3
    }
    $compiledExe = Join-Path $workDirectory "winrm-setup-installer.exe"
    if (-not (Test-Path -LiteralPath $compiledExe -PathType Leaf)) {
        Write-Error "makensis did not produce winrm-setup-installer.exe" -ErrorAction Continue
        exit 3
    }

    $isoRoot = Join-Path $workDirectory "iso-root"
    New-Item -ItemType Directory -Path $isoRoot -Force | Out-Null
    Copy-Item -LiteralPath $compiledExe -Destination $isoRoot

    if ([string]::IsNullOrWhiteSpace($Out)) {
        $Out = Join-Path ([IO.Path]::GetTempPath()) ("nsis-bootstrap-" + [guid]::NewGuid() + ".iso")
    }
    $outDirectory = Split-Path -Parent $Out
    if ($outDirectory -and -not (Test-Path -LiteralPath $outDirectory)) {
        New-Item -ItemType Directory -Path $outDirectory -Force | Out-Null
    }

    # A real, statically-bound interface call is required to drain
    # IMAPI2FS's result IStream -- see this script's .DESCRIPTION.
    $csharpLines = @(
        'using System;',
        'using System.IO;',
        'using System.Runtime.InteropServices.ComTypes;',
        'public static class ProjectBuddyIsoStreamCopier {',
        '    public static void CopyToFile(object comStream, string path) {',
        '        IStream stream = (IStream)comStream;',
        '        using (FileStream outFile = new FileStream(path, FileMode.Create, FileAccess.Write)) {',
        '            byte[] buffer = new byte[65536];',
        '            IntPtr bytesReadPtr = System.Runtime.InteropServices.Marshal.AllocHGlobal(4);',
        '            try {',
        '                int bytesRead;',
        '                do {',
        '                    stream.Read(buffer, buffer.Length, bytesReadPtr);',
        '                    bytesRead = System.Runtime.InteropServices.Marshal.ReadInt32(bytesReadPtr);',
        '                    if (bytesRead > 0) { outFile.Write(buffer, 0, bytesRead); }',
        '                } while (bytesRead > 0);',
        '            } finally {',
        '                System.Runtime.InteropServices.Marshal.FreeHGlobal(bytesReadPtr);',
        '            }',
        '        }',
        '    }',
        '}'
    )
    if (-not ([System.Management.Automation.PSTypeName]'ProjectBuddyIsoStreamCopier').Type) {
        Add-Type -TypeDefinition ($csharpLines -join [Environment]::NewLine)
    }

    $fsImage = New-Object -ComObject IMAPI2FS.MsftFileSystemImage
    $fsImage.VolumeName = $volumeLabel
    $fsImage.FileSystemsToCreate = 3  # FsiFileSystemISO9660 (1) | FsiFileSystemJoliet (2)
    $root = $fsImage.Root
    foreach ($file in Get-ChildItem -LiteralPath $isoRoot -File) {
        $root.AddTree($file.FullName, $false)
    }
    $resultImage = $fsImage.CreateResultImage()
    [ProjectBuddyIsoStreamCopier]::CopyToFile($resultImage.ImageStream, $Out)

    if (-not (Test-Path -LiteralPath $Out -PathType Leaf)) {
        Write-Error "ISO was not created: $Out" -ErrorAction Continue
        exit 3
    }
    Write-Output $Out
} finally {
    if (Test-Path -LiteralPath $workDirectory) {
        Remove-Item -LiteralPath $workDirectory -Recurse -Force
    }
}
