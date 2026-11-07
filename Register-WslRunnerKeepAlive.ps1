#requires -Version 7.0
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateNotNullOrEmpty()]
    [string]$DistroName = "Ubuntu",
    [ValidatePattern('^[^\\/:*?"<>|]+$')]
    [string]$ShortcutName = "ProjectBuddy WSL Runner KeepAlive",
    [switch]$Run
)

$ErrorActionPreference = "Stop"
$script:WslRunnerKeepAliveDotSourced = $MyInvocation.InvocationName -eq "."
$script:WslRunnerKeepAliveDescription = "Managed by Register-WslRunnerKeepAlive.ps1 v3; starts the WSL runner keep-alive terminal at user logon."

function Get-WslRunnerExecutablePath {
    $systemExecutable = Join-Path $env:SystemRoot "System32\wsl.exe"
    if (Test-Path -LiteralPath $systemExecutable -PathType Leaf) {
        return $systemExecutable
    }

    $wslCommand = Get-Command wsl.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    return $wslCommand.Source
}

function Get-WslRunnerDistributionName {
    $wslExecutable = Get-WslRunnerExecutablePath
    $output = @(& $wslExecutable --list --quiet 2>&1)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Unable to enumerate registered WSL distributions (exit code $exitCode)."
    }

    $names = @(
        foreach ($entry in $output) {
            $name = ([string]$entry -replace "`0", "").Trim()
            if ($name.StartsWith("*")) {
                $name = $name.Substring(1).Trim()
            }
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $name
            }
        }
    )
    if ($names.Count -eq 0) {
        throw "WSL did not report any registered distributions."
    }
    return ,$names
}

function Get-WslRunnerTerminalExecutablePath {
    $terminalCommand = Get-Command wt.exe -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    return $terminalCommand.Source
}

function Get-WslRunnerKeepAliveDescription {
    return $script:WslRunnerKeepAliveDescription
}

function Get-WslRunnerStartupShortcut {
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }

    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $null
    try {
        $shortcut = $shell.CreateShortcut($Path)
        return [pscustomobject]@{
            TargetPath     = [string]$shortcut.TargetPath
            Arguments      = [string]$shortcut.Arguments
            WorkingDirectory = [string]$shortcut.WorkingDirectory
            Description    = [string]$shortcut.Description
            WindowStyle    = [int]$shortcut.WindowStyle
        }
    } finally {
        if ($null -ne $shortcut) {
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
}

function Test-WslRunnerKeepAliveProcess {
    param(
        [Parameter(Mandatory)]
        [string]$DistroName
    )

    $wslExecutable = Get-WslRunnerExecutablePath
    $null = & $wslExecutable `
        --distribution $DistroName `
        --exec /usr/bin/pgrep -f '^/bin/sleep infinity$'
    $exitCode = $LASTEXITCODE
    if ($exitCode -eq 0) {
        return $true
    }
    if ($exitCode -eq 1) {
        return $false
    }
    throw "Unable to check the WSL keep-alive process (pgrep exit code $exitCode)."
}

function Get-WslRunnerKeepAlivePlan {
    param(
        [Parameter(Mandatory)]
        [string]$DistroName,
        [Parameter(Mandatory)]
        [string]$ShortcutName,
        [Parameter(Mandatory)]
        [string]$WslExecutable,
        [Parameter(Mandatory)]
        [string]$TerminalExecutable,
        [Parameter(Mandatory)]
        [string]$UserId,
        [Parameter(Mandatory)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory)]
        [string[]]$InstalledDistributionNames,
        [Parameter(Mandatory)]
        [string]$StartupFolder,
        [AllowNull()]
        [object]$ExistingShortcut = $null
    )

    if ($DistroName -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        throw "WSL distribution names must use only ASCII letters, digits, periods, underscores, and hyphens."
    }
    if ($InstalledDistributionNames -notcontains $DistroName) {
        throw "WSL distribution '$DistroName' is not registered for this user."
    }

    $expectedTerminal = [System.IO.Path]::GetFullPath($TerminalExecutable)
    $expectedWsl = [System.IO.Path]::GetFullPath($WslExecutable)
    $startupPath = [System.IO.Path]::GetFullPath($StartupFolder)
    $shortcutPath = Join-Path $startupPath "$ShortcutName.lnk"
    $arguments = "-w new $expectedWsl --distribution $DistroName --exec /bin/sleep infinity"
    $existingShortcutMatches = $false
    if ($null -ne $ExistingShortcut) {
        $actualTerminal = [System.IO.Path]::GetFullPath([string]$ExistingShortcut.TargetPath)
        $actualWorkingDirectory = [System.IO.Path]::GetFullPath([string]$ExistingShortcut.WorkingDirectory)
        $expectedWorkingDirectory = [System.IO.Path]::GetFullPath($WorkingDirectory)
        $existingShortcutMatches = $actualTerminal.Equals(
            $expectedTerminal,
            [System.StringComparison]::OrdinalIgnoreCase
        ) -and
            [string]$ExistingShortcut.Arguments -ceq $arguments -and
            $actualWorkingDirectory.Equals(
                $expectedWorkingDirectory,
                [System.StringComparison]::OrdinalIgnoreCase
            ) -and
            [string]$ExistingShortcut.Description -ceq $script:WslRunnerKeepAliveDescription -and
            [int]$ExistingShortcut.WindowStyle -eq 7
        if (-not $existingShortcutMatches) {
            throw "Startup shortcut '$shortcutPath' already exists with different settings; refusing to replace it."
        }
    }

    return [pscustomobject]@{
        DistroName         = $DistroName
        ShortcutName       = $ShortcutName
        ShortcutPath       = $shortcutPath
        StartupFolder      = $startupPath
        UserId             = $UserId
        WorkingDirectory   = [System.IO.Path]::GetFullPath($WorkingDirectory)
        WslExecutable      = $expectedWsl
        TerminalExecutable = $expectedTerminal
        Arguments          = $arguments
        Description        = $script:WslRunnerKeepAliveDescription
        WindowStyle        = 7
        ExistingShortcut   = ($null -ne $ExistingShortcut)
    }
}

function Get-WslRunnerKeepAlivePlanFromHost {
    param(
        [Parameter(Mandatory)]
        [string]$DistroName,
        [Parameter(Mandatory)]
        [string]$ShortcutName
    )

    $wslExecutable = Get-WslRunnerExecutablePath
    $terminalExecutable = Get-WslRunnerTerminalExecutablePath
    $distributionNames = Get-WslRunnerDistributionName
    $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $startupFolder = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
    $shortcutPath = Join-Path $startupFolder "$ShortcutName.lnk"
    $existingShortcut = Get-WslRunnerStartupShortcut -Path $shortcutPath
    return Get-WslRunnerKeepAlivePlan `
        -DistroName $DistroName `
        -ShortcutName $ShortcutName `
        -WslExecutable $wslExecutable `
        -TerminalExecutable $terminalExecutable `
        -UserId $userId `
        -WorkingDirectory $env:USERPROFILE `
        -InstalledDistributionNames $distributionNames `
        -StartupFolder $startupFolder `
        -ExistingShortcut $existingShortcut
}

if (-not $script:WslRunnerKeepAliveDotSourced) {
    $plan = Get-WslRunnerKeepAlivePlanFromHost -DistroName $DistroName -ShortcutName $ShortcutName
    if (-not $Run) {
        Write-Information "Read-only WSL runner keep-alive plan:" -InformationAction Continue
        $plan | Format-List DistroName, ShortcutName, ShortcutPath, UserId, TerminalExecutable, Arguments, ExistingShortcut
        Write-Information "No change made. Use -Run and approve its confirmation prompt to install the per-user Startup shortcut and start the terminal." -InformationAction Continue
        return
    }

    if (-not $PSCmdlet.ShouldProcess(
        $plan.ShortcutPath,
        "Install and start the WSL runner keep-alive terminal"
    )) {
        return
    }

    if (-not $plan.ExistingShortcut) {
        $null = New-Item -ItemType Directory -Path $plan.StartupFolder -Force
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $null
        try {
            $shortcut = $shell.CreateShortcut($plan.ShortcutPath)
            $shortcut.TargetPath = $plan.TerminalExecutable
            $shortcut.Arguments = $plan.Arguments
            $shortcut.WorkingDirectory = $plan.WorkingDirectory
            $shortcut.Description = $plan.Description
            $shortcut.WindowStyle = $plan.WindowStyle
            $shortcut.Save()
        } finally {
            if ($null -ne $shortcut) {
                [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
            }
            [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }

    if (-not (Test-WslRunnerKeepAliveProcess -DistroName $plan.DistroName)) {
        $null = Start-Process `
            -FilePath $plan.TerminalExecutable `
            -ArgumentList @(
                "-w", "new", $plan.WslExecutable, "--distribution", $plan.DistroName,
                "--exec", "/bin/sleep", "infinity"
            ) `
            -WorkingDirectory $plan.WorkingDirectory `
            -WindowStyle Minimized `
            -PassThru
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if (Test-WslRunnerKeepAliveProcess -DistroName $plan.DistroName) {
            Write-Information "Startup shortcut '$($plan.ShortcutPath)' is installed and WSL distribution '$($plan.DistroName)' has a persistent keep-alive process." -InformationAction Continue
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)

    throw "Windows Terminal did not start a persistent WSL keep-alive process for '$($plan.DistroName)'."
}
