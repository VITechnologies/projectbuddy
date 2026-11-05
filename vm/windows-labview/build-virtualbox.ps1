[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingPlainTextForPassword", "GuestCredentialFile", Justification = "This parameter is a protected credential file path, never a password value.")]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "The only match is Stop-Build, a fail-fast helper that reports an error and exits without mutating external state.")]
[CmdletBinding()]
param(
    [switch]$Run,
    [ValidateSet("Gui", "Headless", "None")]
    [string]$StartMode = "Headless",
    [string]$ProvisioningManifest = (Join-Path $PSScriptRoot "provisioning-manifest.json"),
    # VmName, BaseFolder, GuestFullName, and GuestHostname are mandatory --
    # this script is a generic engine shared by any fork; SandboxSBOMReporting's
    # own real values are supplied explicitly by its own workflow inputs (see
    # ../../.github/workflows/provision-windows-labview-vm.yml), never baked
    # in here.
    [Parameter(Mandatory)]
    [string]$VmName,
    [Parameter(Mandatory)]
    [string]$BaseFolder,
    [string]$Iso = "",
    [string]$GuestCredentialFile = "",
    [string]$GuestUser = "actor",
    [Parameter(Mandatory)]
    [string]$GuestFullName,
    [Parameter(Mandatory)]
    [string]$GuestHostname,
    [ValidateRange(1024, 65535)]
    [int]$WinRmHostPort = 15985,
    # Left empty here (not a fixed default): auto-built fresh from
    # winrm-setup-installer.nsi via build-nsis-bootstrap-iso.ps1 unless the
    # caller explicitly overrides it with a path to a pre-built ISO (e.g. a
    # pinned/pre-verified build for a specific CI run). See this script's
    # unattended-install section for the same SATA-port-1 empty-then-swap
    # timing requirement the bash builder documents in detail.
    [string]$NsisBootstrapIso = ""
)

<#
.SYNOPSIS
Dry-run-first VirtualBox builder for a Windows 11 Pro guest intended to run
LabVIEW 2026 Q3 Professional + VIPM Professional as a self-hosted GitHub
Actions Windows runner, from a native Windows host.

.DESCRIPTION
Mirrors build-virtualbox.sh (the Linux-host builder) exactly: manifest-driven
hardware profile, NVMe-only durable storage, ISO hash verification, and
dry-run by default. This script only creates the VM shell and launches the
unattended Windows Setup phase (OS install + Guest Additions); it does not
install LabVIEW, VIPM, or register a GitHub Actions runner.

The NSIS WinRM-setup injection point (see build-nsis-bootstrap-iso.ps1) is
auto-built and attached on SATA port 1, moments after the VM starts (not
before -- VBoxManage unattended install unconditionally ejects any
pre-existing optical media on any controller/port before constructing its
own install media; attaching the real bootstrap ISO only after the VM is
already running avoids that ejection pass entirely, exactly as the bash
builder does). Set -NsisBootstrapIso to a pre-built ISO to override the
auto-build.
#>

$ErrorActionPreference = "Stop"
$script:ExitCode = 1
$privateDirectoryInitializer = Join-Path $PSScriptRoot "../ubuntu-labview/Initialize-PrivateDirectory.ps1"
$privateFileVerifier = Join-Path $PSScriptRoot "../ubuntu-labview/Test-PrivateFileAcl.ps1"
$nsisBuilder = Join-Path $PSScriptRoot "build-nsis-bootstrap-iso.ps1"
$scriptTemplate = Join-Path $PSScriptRoot "unattended-template.xml"

function Stop-Build {
    param([string]$Message, [int]$Code)
    Write-Error $Message -ErrorAction Continue
    exit $Code
}

function Assert-ManifestContract {
    param([pscustomobject]$Manifest)

    $os = $Manifest.os
    $vbox = $Manifest.virtualBox
    if ($os.virtualBoxOsType -ne "Windows11_64" -or
        $os.iso.filename -ne "Win11_25H2_English_x64_v2.iso" -or
        $os.iso.sha256 -ne "768984706b909479417b2368438909440f2967ff05c6a9195ed2667254e465e3" -or
        $vbox.diskGb -ne 120 -or $vbox.memoryMb -ne 8192 -or $vbox.cpus -ne 4 -or $vbox.vramMb -ne 128 -or
        $vbox.firmware -ne "efi64" -or $vbox.chipset -ne "ich9" -or $vbox.tpmType -ne "2.0") {
        Stop-Build "Windows VirtualBox builder: manifest contract drift" 2
    }
}

function Assert-WindowsNvmePath {
    param([string]$Path)

    if (-not $IsWindows) {
        Stop-Build "Windows VirtualBox builder: mutation requires Windows" 3
    }
    if (-not [IO.Path]::IsPathFullyQualified($Path) -or
        [Uri]::IsWellFormedUriString($Path, [UriKind]::Absolute) -or
        $Path.StartsWith("\\")) {
        Stop-Build "Windows VirtualBox builder: BaseFolder must be an absolute local path" 3
    }

    $probe = $Path
    while (-not (Test-Path -LiteralPath $probe)) {
        $parent = Split-Path -Parent $probe
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $probe) {
            Stop-Build "Windows VirtualBox builder: BaseFolder has no existing ancestor" 3
        }
        $probe = $parent
    }
    $item = Get-Item -LiteralPath $probe -Force
    $currentItem = $item
    while ($currentItem) {
        if (($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            Stop-Build "Windows VirtualBox builder: BaseFolder ancestor cannot be a reparse point" 3
        }
        $parentPath = Split-Path -Parent $currentItem.FullName
        if ([string]::IsNullOrWhiteSpace($parentPath) -or $parentPath -eq $currentItem.FullName) {
            break
        }
        $currentItem = Get-Item -LiteralPath $parentPath -Force
    }

    $volume = Get-Volume -FilePath $item.FullName
    if (-not $volume.DriveLetter -or $volume.DriveType -ne "Fixed") {
        Stop-Build "Windows VirtualBox builder: BaseFolder must resolve to a fixed local volume" 3
    }
    $partition = Get-Partition -DriveLetter $volume.DriveLetter
    if (@($partition).Count -ne 1) {
        Stop-Build "Windows VirtualBox builder: BaseFolder must resolve to one partition" 3
    }
    $disk = Get-Disk -Number $partition.DiskNumber
    if (@($disk).Count -ne 1 -or $disk.BusType -ne "NVMe") {
        Stop-Build "Windows VirtualBox builder: BaseFolder is not backed by internal NVMe" 3
    }
}

function Format-PlanArgument {
    param([string]$Value)
    if ($Value -match '[\s"]') { return '"' + $Value.Replace('"', '\"') + '"' }
    return $Value
}

function Write-Plan {
    param([string]$Command, [string[]]$Arguments)
    $rendered = @($Command) + @($Arguments | ForEach-Object { Format-PlanArgument $_ })
    Write-Output ("[dry-run] " + ($rendered -join " "))
}

function Invoke-VBox {
    param([string[]]$Arguments, [switch]$RedactOutput)

    if (-not $Run) {
        Write-Plan "VBoxManage.exe" $Arguments
        return
    }
    if ($RedactOutput) {
        & VBoxManage.exe @Arguments 2>&1 | ForEach-Object {
            $_ -replace '(?i)^(\s*(?:password|user-password|admin-password)\s*=).*$', '$1 [redacted]'
        }
    } else {
        & VBoxManage.exe @Arguments
    }
    if ($LASTEXITCODE -ne 0) {
        Stop-Build "Windows VirtualBox builder: VBoxManage.exe failed" 3
    }
}

function Get-VmState {
    param([string]$Name)
    $info = & VBoxManage.exe showvminfo $Name --machinereadable 2>$null
    $stateLine = $info | Where-Object { $_ -match '^VMState="(.+)"$' } | Select-Object -First 1
    if (-not $stateLine) { return $null }
    return [regex]::Match($stateLine, '^VMState="(.+)"$').Groups[1].Value
}

if (-not (Test-Path -LiteralPath $ProvisioningManifest -PathType Leaf)) {
    Stop-Build "Windows VirtualBox builder: manifest is not readable" 2
}
try {
    $manifest = Get-Content -LiteralPath $ProvisioningManifest -Raw | ConvertFrom-Json
} catch {
    Stop-Build "Windows VirtualBox builder: manifest is invalid JSON" 2
}
Assert-ManifestContract $manifest

if ($VmName -notmatch '^[A-Za-z0-9._-]+$') {
    Stop-Build "Windows VirtualBox builder: VmName contains unsupported characters" 2
}
if ($GuestUser -notmatch '^[A-Za-z][A-Za-z0-9_-]*$') {
    Stop-Build "Windows VirtualBox builder: GuestUser is invalid" 2
}
if (-not (Test-Path -LiteralPath $scriptTemplate -PathType Leaf)) {
    Stop-Build "Windows VirtualBox builder: unattended answer-file template missing: $scriptTemplate" 2
}
foreach ($requiredScript in @($privateDirectoryInitializer, $privateFileVerifier, $nsisBuilder)) {
    if (-not (Test-Path -LiteralPath $requiredScript -PathType Leaf)) {
        Stop-Build "Windows VirtualBox builder: required provisioning helper is missing: $requiredScript" 2
    }
}

$isoContract = $manifest.os.iso
$vmProfile = $manifest.virtualBox
$imageIndex = $manifest.os.virtualBoxImageIndex
$disk = [IO.Path]::Combine($BaseFolder, $VmName, "$VmName.vdi")
$evidenceDirectory = [IO.Path]::Combine($BaseFolder, "evidence", $VmName)
$temporaryDisplay = "<runner-temporary>"
$passwordCopy = Join-Path $temporaryDisplay "guest-password"

Write-Output "VirtualBox build: $VmName"
Write-Output "Profile: $($manifest.os.virtualBoxOsType) | $($vmProfile.memoryMb) MB | $($vmProfile.cpus) CPU | $($vmProfile.vramMb) MB VRAM | $($vmProfile.diskGb) GB"
Write-Output "Storage: $BaseFolder (real builds require a physical NVMe disk resolved with Get-Partition and Get-Disk)"
Write-Output "ISO contract: $($isoContract.filename) | $($isoContract.sha256)"

$temporaryDirectory = $null
try {
    if ($Run) {
        if (-not $IsWindows) { Stop-Build "Windows VirtualBox builder: mutation requires Windows" 3 }
        # A long-running GitHub Actions runner service process caches the
        # PATH it started with; a VirtualBox install that happened after
        # the service started (or even before, if the service was never
        # restarted since) won't be visible via Get-Command until this
        # process's PATH is refreshed from the registry -- confirmed live
        # 2026-11-05: VBoxManage.exe existed at the default install
        # location but "VBoxManage.exe is missing" fired anyway. Same
        # pattern this workflow already uses for chocolatey/NSIS.
        $env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
        if (-not (Get-Command VBoxManage.exe -ErrorAction SilentlyContinue)) {
            $fallback = Join-Path ${env:ProgramFiles} "Oracle\VirtualBox\VBoxManage.exe"
            if (Test-Path -LiteralPath $fallback -PathType Leaf) {
                $env:Path = "$(Split-Path -Parent $fallback);$env:Path"
            }
        }
        if (-not (Get-Command VBoxManage.exe -ErrorAction SilentlyContinue)) {
            Stop-Build "Windows VirtualBox builder: VBoxManage.exe is missing" 3
        }
        $unattendedHelp = & VBoxManage.exe unattended install --help 2>&1
        if ($LASTEXITCODE -ne 0 -or ($unattendedHelp -join "`n") -notmatch '--install-additions') {
            Stop-Build "Windows VirtualBox builder: VirtualBox lacks unattended --install-additions support" 3
        }
        Assert-WindowsNvmePath $BaseFolder
        if (-not (Test-Path -LiteralPath $Iso -PathType Leaf)) {
            Stop-Build "Windows VirtualBox builder: -Run requires the pinned ISO" 3
        }
        if (-not (Test-Path -LiteralPath $GuestCredentialFile -PathType Leaf) -or
            (Get-Item -LiteralPath $GuestCredentialFile).Length -eq 0) {
            Stop-Build "Windows VirtualBox builder: -Run requires a non-empty GuestCredentialFile" 3
        }
        & $privateFileVerifier -Path $GuestCredentialFile | Out-Null
        $actualHash = (Get-FileHash -LiteralPath $Iso -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $isoContract.sha256) {
            Stop-Build "Windows VirtualBox builder: ISO SHA-256 mismatch" 3
        }
        if (-not [string]::IsNullOrWhiteSpace($NsisBootstrapIso) -and -not (Test-Path -LiteralPath $NsisBootstrapIso -PathType Leaf)) {
            Stop-Build "Windows VirtualBox builder: NsisBootstrapIso is set but not a file" 3
        }

        $registered = & VBoxManage.exe list vms
        if ($LASTEXITCODE -ne 0) { Stop-Build "Windows VirtualBox builder: cannot list VMs" 3 }
        if ($registered -match ('"' + [regex]::Escape($VmName) + '"')) {
            Stop-Build "Windows VirtualBox builder: VM already exists" 3
        }

        New-Item -ItemType Directory -Path $BaseFolder, $evidenceDirectory, (Split-Path -Parent $disk) -Force | Out-Null
        $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("vm-build-" + [guid]::NewGuid())
        & $privateDirectoryInitializer -Path $temporaryDirectory | Out-Null
        $passwordCopy = Join-Path $temporaryDirectory "guest-password"
        Copy-Item -LiteralPath $GuestCredentialFile -Destination $passwordCopy
        & $privateFileVerifier -Path $passwordCopy -AllowInheritance | Out-Null
    }

    # Auto-build the auxiliary ISO here unless the caller already supplied
    # one -- building it fresh keeps the attached media always in sync with
    # tracked winrm-setup-installer.nsi source. For a real run this MUST be
    # written to a path that persists for the VM's own lifetime
    # ($BaseFolder\$VmName\, deleted only when the VM itself is torn down),
    # never a script-local temporary directory that is cleaned up within
    # seconds of this script exiting -- the next power-on after this one
    # (e.g. a later install-vipm workflow dispatch) re-validates every
    # attached medium's backing file from scratch and refuses to start if
    # it is gone. See build-virtualbox.sh's identical comment for the full
    # live-reproduction history (issue #205).
    if ([string]::IsNullOrWhiteSpace($NsisBootstrapIso)) {
        if ($Run) {
            $nsisOut = Join-Path $BaseFolder $VmName "nsis-bootstrap.iso"
        } else {
            if (-not $temporaryDirectory) {
                $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("vm-build-" + [guid]::NewGuid())
                New-Item -ItemType Directory -Path $temporaryDirectory -Force | Out-Null
            }
            $nsisOut = Join-Path $temporaryDirectory "nsis-bootstrap.iso"
        }
        # Dry-run still really builds this (no VM/persistent directory is
        # actually created) purely to validate makensis.exe/IMAPI2 work on
        # this host, exactly as the bash builder does; nothing references
        # the temp-dir output afterward. $LASTEXITCODE must be checked
        # explicitly -- a failing external script does not throw a
        # terminating PowerShell exception on its own, so without this
        # check a missing makensis.exe would silently fall through to a
        # "successful" dry-run plan instead of failing closed.
        $NsisBootstrapIso = & $nsisBuilder -Out $nsisOut
        if ($LASTEXITCODE -ne 0) {
            Stop-Build "Windows VirtualBox builder: build-nsis-bootstrap-iso.ps1 failed with exit code $LASTEXITCODE" 2
        }
    }

    Invoke-VBox @("createvm", "--name", $VmName, "--ostype", $manifest.os.virtualBoxOsType, "--register", "--basefolder", $BaseFolder)
    Invoke-VBox @("modifyvm", $VmName, "--memory", "$($vmProfile.memoryMb)", "--cpus", "$($vmProfile.cpus)", "--vram", "$($vmProfile.vramMb)",
        "--firmware", $vmProfile.firmware, "--chipset", $vmProfile.chipset, "--tpm-type", $vmProfile.tpmType,
        "--nested-paging", "on", "--ioapic", "on", "--graphicscontroller", "vmsvga", "--audio-driver", "none")
    Invoke-VBox @("modifynvram", $VmName, "inituefivarstore")
    Invoke-VBox @("modifynvram", $VmName, "enrollmssignatures")
    Invoke-VBox @("modifynvram", $VmName, "enrollorclpk")
    Invoke-VBox @("modifynvram", $VmName, "secureboot", "--enable")
    Invoke-VBox @("createmedium", "disk", "--filename", $disk, "--size", "$($vmProfile.diskGb * 1024)", "--format", "VDI", "--variant", "Standard")
    Invoke-VBox @("storagectl", $VmName, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci", "--portcount", "2", "--bootable", "on")
    Invoke-VBox @("storageattach", $VmName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $disk)
    if (-not [string]::IsNullOrWhiteSpace($NsisBootstrapIso)) {
        # Attach an EMPTY drive at this slot now, not the real ISO --
        # `storageattach --medium <iso>` on a port with no drive object yet
        # fails outright ("No drive attached to device slot..."), confirmed
        # live against the bash builder. The real ISO is swapped in below,
        # once `unattended install`'s unconditional eject-all-optical-media
        # pass is safely past; an already-empty drive has nothing for that
        # pass to eject, so it is unaffected.
        Invoke-VBox @("storageattach", $VmName, "--storagectl", "SATA", "--port", "1", "--device", "0", "--type", "dvddrive", "--medium", "emptydrive")
    }
    Invoke-VBox @("storagectl", $VmName, "--name", "IDE", "--add", "ide")
    Invoke-VBox @("storageattach", $VmName, "--storagectl", "IDE", "--port", "0", "--device", "0", "--type", "dvddrive", "--medium", $(if ($Iso) { $Iso } else { "emptydrive" }))
    Invoke-VBox @("modifyvm", $VmName, "--natpf1", "winrm,tcp,127.0.0.1,$WinRmHostPort,,5985")

    if ($StartMode -ne "None") {
        $isoArgument = if ($Iso) { $Iso } else { "<pinned-iso>\$($isoContract.filename)" }
        $unattended = @(
            "unattended", "install", $VmName,
            "--iso=$isoArgument",
            "--user=$GuestUser", "--user-password-file=$passwordCopy", "--full-user-name=$GuestFullName",
            "--hostname=$GuestHostname", "--image-index=$imageIndex", "--install-additions",
            "--script-template=$scriptTemplate", "--start-vm=$($StartMode.ToLowerInvariant())"
        )
        Invoke-VBox -Arguments $unattended -RedactOutput

        # NsisBootstrapIso must be attached AFTER unattended install starts
        # the VM, never before -- see this script's NSIS auto-build comment
        # above for why. SATA port 1 stays an empty, controller-only slot
        # through unattended install's own setup, then gets populated here
        # once the VM is already running and that ejection pass is long
        # past -- this still lands well before OOBE/FirstLogonCommands, so
        # there is no meaningful timing risk for anything that needs to
        # read this drive later.
        if (-not [string]::IsNullOrWhiteSpace($NsisBootstrapIso)) {
            if ($Run) {
                $state = $null
                for ($i = 0; $i -lt 12; $i++) {
                    $state = Get-VmState -Name $VmName
                    if ($state -eq "running") { break }
                    Start-Sleep -Seconds 1
                }
                if ($state -ne "running") {
                    Stop-Build "Windows VirtualBox builder: VM did not reach 'running' in time for NsisBootstrapIso attach (state: $state)" 4
                }
            }
            Invoke-VBox @("storageattach", $VmName, "--storagectl", "SATA", "--port", "1", "--device", "0", "--type", "dvddrive", "--medium", $NsisBootstrapIso)
        }
    } else {
        if (-not $Run) {
            Write-Output "[dry-run] (unattended install skipped: -StartMode None)"
        }
        if (-not [string]::IsNullOrWhiteSpace($NsisBootstrapIso)) {
            Write-Output "NOTE: -StartMode None set; NsisBootstrapIso was not attached (it must be attached after a later 'unattended install' call starts the VM, not before)."
        }
    }

    Write-Output "VM shell plan complete for $VmName (run=$([bool]$Run))."
    $script:ExitCode = 0
} finally {
    if ($temporaryDirectory -and (Test-Path -LiteralPath $temporaryDirectory)) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}
