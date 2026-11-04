[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSAvoidUsingPlainTextForPassword", "GuestCredentialFile", Justification = "This parameter is a protected credential file path, never a password value.")]
[Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "The only match is Stop-Build, a fail-fast helper that reports an error and exits without mutating external state.")]
[CmdletBinding()]
param(
    [switch]$Run,
    [ValidateSet("Gui", "Headless", "None")]
    [string]$StartMode = "Gui",
    [string]$ProvisioningManifest = (Join-Path $PSScriptRoot "provisioning-manifest.json"),
    # VmName, BaseFolder, GuestFullName, and GuestHostname are mandatory --
    # this script is a generic engine shared by any fork; SandboxSBOMReporting's
    # own real values are supplied explicitly by its own workflow inputs (see
    # ../../.github/workflows/provision-ubuntu-vm.yml), never baked in here.
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
    [int]$SshHostPort = 2222
)

$ErrorActionPreference = "Stop"
$script:ExitCode = 1
$privateDirectoryInitializer = Join-Path $PSScriptRoot "Initialize-PrivateDirectory.ps1"
$privateFileVerifier = Join-Path $PSScriptRoot "Test-PrivateFileAcl.ps1"

function Stop-Build {
    param([string]$Message, [int]$Code)
    Write-Error $Message -ErrorAction Continue
    exit $Code
}

function Assert-ManifestContract {
    param([pscustomobject]$Manifest)

    if ($Manifest.schema -ne "sandbox-sbom-reporting/ubuntu-labview-provisioning@1" -or
        $Manifest.os.distribution -ne "ubuntu" -or
        $Manifest.os.version -ne "24.04" -or
        $Manifest.os.codename -ne "noble" -or
        $Manifest.os.virtualBoxOsType -ne "Ubuntu24_LTS_64" -or
        $Manifest.os.iso.filename -ne "ubuntu-24.04.4-desktop-amd64.iso" -or
        $Manifest.os.iso.url -ne "https://releases.ubuntu.com/24.04/ubuntu-24.04.4-desktop-amd64.iso" -or
        $Manifest.os.iso.sha256 -ne "3a4c9877b483ab46d7c3fbe165a0db275e1ae3cfe56a5657e5a47c2f99a99d1e" -or
        $Manifest.virtualBox.diskGb -ne 80 -or
        $Manifest.virtualBox.memoryMb -ne 12288 -or
        $Manifest.virtualBox.cpus -ne 6 -or
        $Manifest.virtualBox.vramMb -ne 128) {
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
if ($GuestUser -notmatch '^[a-z_][a-z0-9_-]*$') {
    Stop-Build "Windows VirtualBox builder: GuestUser is invalid" 2
}

$bootstrapSource = Join-Path $PSScriptRoot "base-bootstrap.sh"
foreach ($requiredScript in @($bootstrapSource, $privateDirectoryInitializer, $privateFileVerifier)) {
    if (-not (Test-Path -LiteralPath $requiredScript -PathType Leaf)) {
        Stop-Build "Windows VirtualBox builder: required provisioning helper is missing" 2
    }
}

$isoContract = $manifest.os.iso
$vmProfile = $manifest.virtualBox
$vmDirectory = [IO.Path]::Combine($BaseFolder, $VmName)
$disk = [IO.Path]::Combine($vmDirectory, "$VmName.vdi")
$evidenceDirectory = [IO.Path]::Combine($BaseFolder, "evidence", $VmName)
$temporaryDisplay = "<runner-temporary>"
$passwordCopy = Join-Path $temporaryDisplay "guest-password"
$bootstrapTemplate = Join-Path $temporaryDisplay "base-bootstrap.sh"

Write-Output "VirtualBox build: $VmName"
Write-Output "Profile: $($manifest.os.virtualBoxOsType) | $($vmProfile.memoryMb) MB | $($vmProfile.cpus) CPU | $($vmProfile.vramMb) MB VRAM | $($vmProfile.diskGb) GB"
Write-Output "Storage: $BaseFolder (real builds require a physical NVMe disk resolved with Get-Partition and Get-Disk)"
Write-Output "ISO contract: $($isoContract.filename) | $($isoContract.sha256)"

$temporaryDirectory = $null
try {
    if ($Run) {
        if (-not $IsWindows) { Stop-Build "Windows VirtualBox builder: mutation requires Windows" 3 }
        if (-not (Get-Command VBoxManage.exe -ErrorAction SilentlyContinue)) {
            Stop-Build "Windows VirtualBox builder: VBoxManage.exe is missing" 3
        }
        $unattendedHelp = & VBoxManage.exe unattended install --help 2>&1
        if ($LASTEXITCODE -ne 0 -or ($unattendedHelp -join "`n") -notmatch '--post-install-template') {
            Stop-Build "Windows VirtualBox builder: VirtualBox lacks unattended post-install template support" 3
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
        $registered = & VBoxManage.exe list vms
        if ($LASTEXITCODE -ne 0) { Stop-Build "Windows VirtualBox builder: cannot list VMs" 3 }
        if ($registered -match ('"' + [regex]::Escape($VmName) + '"')) {
            Stop-Build "Windows VirtualBox builder: VM already exists" 3
        }

        New-Item -ItemType Directory -Path $BaseFolder, $evidenceDirectory -Force | Out-Null
        $temporaryDirectory = Join-Path ([IO.Path]::GetTempPath()) ("vm-build-" + [guid]::NewGuid())
        & $privateDirectoryInitializer -Path $temporaryDirectory | Out-Null
        $passwordCopy = Join-Path $temporaryDirectory "guest-password"
        $bootstrapTemplate = Join-Path $temporaryDirectory "base-bootstrap.sh"
        Copy-Item -LiteralPath $GuestCredentialFile -Destination $passwordCopy
        & $privateFileVerifier -Path $passwordCopy -AllowInheritance | Out-Null
    }

    Invoke-VBox @("createvm", "--name", $VmName, "--ostype", $manifest.os.virtualBoxOsType, "--basefolder", $BaseFolder, "--register")
    Invoke-VBox @("modifyvm", $VmName, "--memory", "$($vmProfile.memoryMb)", "--cpus", "$($vmProfile.cpus)", "--vram", "$($vmProfile.vramMb)", "--graphicscontroller", "vmsvga", "--chipset", "piix3", "--firmware", "bios", "--ioapic", "on", "--rtcuseutc", "on", "--nic1", "nat")
    Invoke-VBox @("modifyvm", $VmName, "--natpf1", "ssh,tcp,127.0.0.1,$SshHostPort,,22")
    Invoke-VBox @("createmedium", "disk", "--filename", $disk, "--size", "$($vmProfile.diskGb * 1024)", "--format", "VDI")
    Invoke-VBox @("storagectl", $VmName, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci", "--portcount", "2", "--bootable", "on")
    Invoke-VBox @("storageattach", $VmName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $disk)
    Invoke-VBox @("storagectl", $VmName, "--name", "IDE", "--add", "ide", "--controller", "PIIX4")

    $vmUuid = "00000000-0000-4000-8000-000000000000"
    if ($Run) {
        $vmInfo = & VBoxManage.exe showvminfo $VmName --machinereadable
        if ($LASTEXITCODE -ne 0) { Stop-Build "Windows VirtualBox builder: cannot resolve VM identity" 3 }
        $uuidLine = $vmInfo | Where-Object { $_ -match '^UUID="(.+)"$' } | Select-Object -First 1
        if (-not $uuidLine) { Stop-Build "Windows VirtualBox builder: VM UUID is missing" 3 }
        $vmUuid = [regex]::Match($uuidLine, '^UUID="(.+)"$').Groups[1].Value
    }

    if ($Run) {
        $bootstrap = (Get-Content -LiteralPath $bootstrapSource -Raw).Replace("@@VM_NAME@@", $VmName).Replace("@@VM_UUID@@", $vmUuid)
        [IO.File]::WriteAllText($bootstrapTemplate, $bootstrap, [Text.UTF8Encoding]::new($false))
        & $privateFileVerifier -Path $bootstrapTemplate -AllowInheritance | Out-Null
    }

    $isoArgument = "--iso=" + $(if ($Iso) { $Iso } else { "<pinned-iso>\$($isoContract.filename)" })
    $unattended = @(
        "unattended", "install", $VmName,
        $isoArgument,
        "--user=$GuestUser", "--user-password-file=$passwordCopy", "--full-user-name=$GuestFullName",
        "--locale=en_US", "--country=US", "--time-zone=UTC", "--hostname=$GuestHostname.local",
        "--post-install-template=$bootstrapTemplate", "--no-install-additions"
    )
    if ($StartMode -ne "None") { $unattended += "--start-vm=$($StartMode.ToLowerInvariant())" }
    Invoke-VBox -Arguments $unattended -RedactOutput

    Write-Output "Next after unattended installation and first boot:"
    Write-Output "  enroll the guest SSH key with enroll-vm-ssh-host-key.py using VM UUID $vmUuid"
    Write-Output "  require StrictHostKeyChecking=yes for every SSH/SCP operation"
    Write-Output "  retrieve and identity-verify $evidenceDirectory\base-bootstrap-receipt.json"
    Write-Output "  run the shared Ubuntu guest provision-guest.sh contract"
    $script:ExitCode = 0
} finally {
    if ($temporaryDirectory -and (Test-Path -LiteralPath $temporaryDirectory)) {
        Remove-Item -LiteralPath $temporaryDirectory -Recurse -Force
    }
}

exit $script:ExitCode