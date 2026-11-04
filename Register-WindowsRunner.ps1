#requires -Version 7.0
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    # Repository, RunnerName, and DedicatedServiceAccountName are mandatory
    # in this generic engine -- no fork's own repo/runner naming is assumed.
    # A private consumer repository may wrap this script with its own
    # defaults for convenient interactive use against its own real values.
    [Parameter(Mandatory)]
    [ValidatePattern("^[^/]+/[^/]+$")]
    [string]$Repository,
    [Parameter(Mandatory)]
    [string]$RunnerName,
    [string]$RunnerRoot = "C:\actions-runner",
    [string[]]$Labels = @("docker"),
    [string]$WorkFolder = "_work",
    [switch]$UseDedicatedServiceAccount,
    [ValidatePattern("^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,19}$")]
    [string]$DedicatedServiceAccountName,
    [switch]$AuthorizeActionsStatus,
    [switch]$Run
)

$ErrorActionPreference = "Stop"
$script:RunnerRegistrationDotSourced = $MyInvocation.InvocationName -eq "."

function Invoke-Gh {
    param(
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $previousTokens = @{}
    try {
        foreach ($name in @("GH_TOKEN", "GITHUB_TOKEN")) {
            $previousTokens[$name] = [System.Environment]::GetEnvironmentVariable($name, "Process")
            Set-RunnerProcessEnvironmentValue -Name $name -Value $null
        }
        $output = & gh @Arguments 2>$null
        $exitCode = $LASTEXITCODE
        if ($exitCode -ne 0) {
            throw "GitHub CLI '$($Arguments[0])' failed with exit code $exitCode."
        }
        return ,$output
    } finally {
        foreach ($name in $previousTokens.Keys) {
            Set-RunnerProcessEnvironmentValue -Name $name -Value $previousTokens[$name]
        }
    }
}

function ConvertFrom-GhJson {
    param(
        [Parameter(Mandatory)]
        [object[]]$Output,
        [Parameter(Mandatory)]
        [string]$Context
    )

    $json = [string]::Join([Environment]::NewLine, [string[]]$Output)
    try {
        return ConvertFrom-Json -InputObject $json -AsHashtable
    } catch {
        throw "GitHub returned invalid JSON while checking $Context."
    }
}

function Get-GitHubRunCount {
    param(
        [Parameter(Mandatory)]
        [string]$Repository,
        [Parameter(Mandatory)]
        [ValidateSet("queued", "in_progress")]
        [string]$Status
    )

    $output = Invoke-Gh -Arguments @(
        "run", "list", "--repo", $Repository, "--status", $Status,
        "--limit", "1000", "--json", "databaseId"
    )
    $runs = ConvertFrom-GhJson -Output $output -Context "$Status workflow runs"
    $count = @($runs).Count
    if ($count -ge 1000) {
        throw "At least 1000 $Status runs were returned; refusing to assume the queue is clear."
    }
    return $count
}

function Get-GitHubRunner {
    param(
        [Parameter(Mandatory)]
        [string]$Repository,
        [Parameter(Mandatory)]
        [string]$RunnerName
    )

    $output = Invoke-Gh -Arguments @(
        "api", "repos/$Repository/actions/runners?per_page=100"
    )
    $response = ConvertFrom-GhJson -Output $output -Context "registered runners"
    if ($response.total_count -gt @($response.runners).Count) {
        throw "More than 100 repository runners exist; refusing to use an incomplete inventory."
    }

    $matchingRunners = @($response.runners | Where-Object { $_.name -ieq $RunnerName })
    if ($matchingRunners.Count -gt 1) {
        throw "More than one registered runner matches '$RunnerName'."
    }
    if ($matchingRunners.Count -eq 0) {
        return $null
    }
    return $matchingRunners[0]
}

function Test-PathWithinRoot {
    param(
        [Parameter(Mandatory)]
        [string]$Path,
        [Parameter(Mandatory)]
        [string]$Root
    )

    $rootPath = [System.IO.Path]::GetFullPath($Root).TrimEnd("\", "/")
    $candidatePath = [System.IO.Path]::GetFullPath($Path)
    $rootPrefix = $rootPath + [System.IO.Path]::DirectorySeparatorChar
    return $candidatePath.Equals($rootPath, [System.StringComparison]::OrdinalIgnoreCase) -or
        $candidatePath.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-ServiceExecutablePath {
    param(
        [Parameter(Mandatory)]
        [string]$CommandLine
    )

    $value = $CommandLine.Trim()
    if ($value.StartsWith('"')) {
        $closingQuote = $value.IndexOf('"', 1)
        if ($closingQuote -lt 2) {
            throw "Could not parse a runner service executable path."
        }
        return $value.Substring(1, $closingQuote - 1)
    }

    $executableEnd = $value.IndexOf(".exe", [System.StringComparison]::OrdinalIgnoreCase)
    if ($executableEnd -ge 0) {
        return $value.Substring(0, $executableEnd + 4)
    }

    $space = $value.IndexOf(" ")
    if ($space -ge 0) {
        return $value.Substring(0, $space)
    }
    return $value
}

function Test-ServiceAccountMatch {
    param(
        [Parameter(Mandatory)]
        [string]$Actual,
        [Parameter(Mandatory)]
        [string]$Expected
    )

    if ($Actual -ieq $Expected) {
        return $true
    }

    $actualParts = $Actual -split "\\", 2
    $expectedParts = $Expected -split "\\", 2
    if ($actualParts.Count -ne 2 -or $expectedParts.Count -ne 2) {
        return $false
    }

    $localPrefixes = @(".", $env:COMPUTERNAME)
    return (
        $actualParts[1] -ieq $expectedParts[1] -and
        $actualParts[0] -in $localPrefixes -and
        $expectedParts[0] -in $localPrefixes
    )
}

function Get-RunnerLocalState {
    param(
        [Parameter(Mandatory)]
        [string]$RunnerRoot
    )

    $rootPath = [System.IO.Path]::GetFullPath($RunnerRoot)
    $allListeners = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'Runner.Listener.exe'")
    $unknownListeners = @($allListeners | Where-Object { [string]::IsNullOrWhiteSpace($_.ExecutablePath) })
    if ($unknownListeners.Count -gt 0) {
        throw "Could not verify a Runner.Listener.exe path; run the check from an elevated PowerShell."
    }

    $listeners = @(
        $allListeners | Where-Object {
            Test-PathWithinRoot -Path $_.ExecutablePath -Root $rootPath
        }
    )

    $runnerServices = @(Get-CimInstance -ClassName Win32_Service | Where-Object { $_.Name -like "actions.runner*" })
    $unknownServices = @($runnerServices | Where-Object { [string]::IsNullOrWhiteSpace($_.PathName) })
    if ($unknownServices.Count -gt 0) {
        throw "Could not verify a runner service executable path; run the check from an elevated PowerShell."
    }
    $services = @(
        $runnerServices | Where-Object {
            $executablePath = Get-ServiceExecutablePath -CommandLine $_.PathName
            Test-PathWithinRoot -Path $executablePath -Root $rootPath
        }
    )
    return [pscustomobject]@{
        Listeners = $listeners
        Services = $services
        LocalConfigPresent = Test-Path -LiteralPath (Join-Path $rootPath ".runner") -PathType Leaf
    }
}

function Assert-RunnerSafeToReconfigure {
    param(
        $Runner,
        [Parameter(Mandatory)]
        [int]$QueuedRuns,
        [Parameter(Mandatory)]
        [int]$InProgressRuns
    )

    if ($QueuedRuns -gt 0 -or $InProgressRuns -gt 0) {
        throw "Queued ($QueuedRuns) or in-progress ($InProgressRuns) repository runs exist; refusing runner reconfiguration."
    }
    if ($Runner -and ($Runner.busy -or $Runner.status -ne "offline")) {
        throw "Runner '$($Runner.name)' must be offline and not busy before reconfiguration."
    }
}

function Get-DockerOSType {
    $output = & docker info --format "{{.OSType}}" 2>$null
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "docker info failed with exit code $exitCode; no engine switch was attempted."
    }
    $osType = [string]::Join("", [string[]]$output).Trim()
    if ($osType -ne "windows") {
        throw "Docker must already be in Windows-container mode; current OSType is '$osType'. No engine switch was attempted."
    }
    return $osType
}

function New-RunnerServicePassword {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Generates an in-memory secure string only.")]
    param()

    $alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()-_=+"
    $bytes = [byte[]]::new(48)
    $characters = [char[]]::new($bytes.Length)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    try {
        $requiredSets = @(
            "abcdefghijklmnopqrstuvwxyz",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "0123456789",
            "!@#$%^&*()-_=+"
        )
        for ($index = 0; $index -lt $requiredSets.Count; $index++) {
            $characters[$index] = $requiredSets[$index][[int]$bytes[$index] % $requiredSets[$index].Length]
        }
        for ($index = $requiredSets.Count; $index -lt $bytes.Length; $index++) {
            $characters[$index] = $alphabet[[int]$bytes[$index] % $alphabet.Length]
        }
        for ($index = $characters.Length - 1; $index -gt 0; $index--) {
            $swapIndex = [System.Security.Cryptography.RandomNumberGenerator]::GetInt32($index + 1)
            $temporary = $characters[$index]
            $characters[$index] = $characters[$swapIndex]
            $characters[$swapIndex] = $temporary
        }
        $securePassword = [System.Security.SecureString]::new()
        try {
            foreach ($character in $characters) {
                $securePassword.AppendChar($character)
            }
            $securePassword.MakeReadOnly()
            $result = $securePassword
            $securePassword = $null
            return $result
        } finally {
            if ($securePassword) {
                $securePassword.Dispose()
            }
        }
    } finally {
        [Array]::Clear($bytes, 0, $bytes.Length)
        [Array]::Clear($characters, 0, $characters.Length)
    }
}

function Initialize-DedicatedRunnerServiceAccount {
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [Parameter(Mandatory)]
        [securestring]$Password,
        [switch]$RequiresDocker
    )

    foreach ($commandName in @("Get-LocalUser", "New-LocalUser", "Set-LocalUser", "Get-LocalGroup", "Get-LocalGroupMember", "Add-LocalGroupMember")) {
        if (-not (Get-Command $commandName -ErrorAction SilentlyContinue)) {
            throw "Required Windows local-account command '$commandName' is unavailable."
        }
    }

    $description = "Managed runner $Name"
    $user = Get-LocalUser -Name $Name -ErrorAction SilentlyContinue
    if ($user) {
        if ($user.Description -cne $description) {
            throw "Local account '$Name' already exists and is not owned by this runner tool."
        }
        Set-LocalUser -Name $Name -Password $Password -PasswordNeverExpires $true -UserMayChangePassword $false -ErrorAction Stop
    } else {
        $user = New-LocalUser -Name $Name -Password $Password -Description $description -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires -ErrorAction Stop
    }

    $groupNames = @("Administrators")
    if ($RequiresDocker) {
        $groupNames += "docker-users"
    }
    foreach ($groupName in $groupNames) {
        $group = Get-LocalGroup -Name $groupName -ErrorAction Stop
        $members = @(Get-LocalGroupMember -Group $group.Name -ErrorAction Stop)
        $memberSids = @($members | ForEach-Object { $_.SID.Value })
        if ($user.SID.Value -notin $memberSids) {
            Add-LocalGroupMember -Group $group.Name -Member $user -ErrorAction Stop
        }
    }

    return "$env:COMPUTERNAME\$Name"
}

function Test-RunnerDockerServiceAccount {
    param(
        [Parameter(Mandatory)]
        [string]$ServiceAccount,
        [Parameter(Mandatory)]
        [securestring]$ServicePassword
    )

    $dockerPath = (Get-Command docker.exe -ErrorAction Stop).Source
    $taskName = "RunnerDockerSmoke-$([guid]::NewGuid().ToString('N'))"
    $probeDirectory = Join-Path $env:ProgramData $taskName
    $probeScriptPath = Join-Path $probeDirectory "probe.ps1"
    $resultPath = Join-Path $probeDirectory "result.json"
    $serviceSid = ([System.Security.Principal.NTAccount]::new($ServiceAccount)).Translate(
        [System.Security.Principal.SecurityIdentifier]
    )
    $systemSid = [System.Security.Principal.SecurityIdentifier]::new("S-1-5-18")
    $currentSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User

    $null = New-Item -ItemType Directory -Path $probeDirectory
    $directoryAcl = Get-Acl -LiteralPath $probeDirectory
    $directoryAcl.SetAccessRuleProtection($true, $false)
    $directoryAcl.SetOwner($currentSid)
    foreach ($sid in @($systemSid, $currentSid, $serviceSid)) {
        $rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
            $sid, "FullControl", "ContainerInherit,ObjectInherit", "None", "Allow"
        )
        $directoryAcl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $probeDirectory -AclObject $directoryAcl

    $dockerLiteral = "'" + $dockerPath.Replace("'", "''") + "'"
    $resultLiteral = "'" + $resultPath.Replace("'", "''") + "'"
    $probeSource = @'
$ErrorActionPreference = "Stop"
$result = [ordered]@{
    identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    identitySid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    dockerInfoExit = 1
    dockerOSType = ""
    containerSmokeExit = 1
    error = ""
}
$exitCode = 1
try {
    $infoOutput = & __DOCKER_PATH__ info --format "{{.OSType}}" 2>&1
    $result.dockerInfoExit = $LASTEXITCODE
    $result.dockerOSType = [string]::Join("", [string[]]$infoOutput).Trim()
    if ($result.dockerInfoExit -ne 0) {
        throw "docker info failed: $([string]::Join(" ", [string[]]$infoOutput))"
    }
    if ($result.dockerOSType -ne "windows") {
        throw "Expected Docker OSType windows; found '$($result.dockerOSType)'."
    }

    & __DOCKER_PATH__ run --rm --pull=never --network none --isolation=hyperv mcr.microsoft.com/windows/nanoserver:ltsc2022 cmd /c ver 2>&1 | Out-Null
    $result.containerSmokeExit = $LASTEXITCODE
    if ($result.containerSmokeExit -ne 0) {
        throw "Windows Nano Server container smoke failed with exit code $($result.containerSmokeExit)."
    }
    $exitCode = 0
} catch {
    $result.error = $_.Exception.Message
}
[System.IO.File]::WriteAllText(__RESULT_PATH__, (ConvertTo-Json $result -Depth 3))
exit $exitCode
'@
    $probeSource = $probeSource.Replace("__DOCKER_PATH__", $dockerLiteral).Replace("__RESULT_PATH__", $resultLiteral)
    [System.IO.File]::WriteAllText($probeScriptPath, $probeSource, [System.Text.UTF8Encoding]::new($false))

    $passwordPointer = [IntPtr]::Zero
    $passwordText = $null
    try {
        $passwordPointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ServicePassword)
        $passwordText = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
        $powerShellPath = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
        $action = New-ScheduledTaskAction -Execute $powerShellPath -Argument (
            '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $probeScriptPath + '"'
        )
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5)
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
            -User $ServiceAccount -Password $passwordText -RunLevel Highest -Force | Out-Null
        Start-ScheduledTask -TaskName $taskName

        $deadline = (Get-Date).AddSeconds(90)
        do {
            Start-Sleep -Seconds 2
            $task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
            $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction Stop
        } while ($task.State -eq "Running" -and (Get-Date) -lt $deadline)

        if ($task.State -eq "Running") {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            throw "Docker service-account smoke exceeded 90 seconds."
        }
        if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) {
            throw "Docker service-account smoke produced no result (task exit $($taskInfo.LastTaskResult))."
        }
        $result = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        if ($result.identitySid -ne $serviceSid.Value) {
            throw "Docker smoke ran under a different Windows account than the runner service."
        }
        if ($result.dockerInfoExit -ne 0 -or $result.dockerOSType -ne "windows") {
            throw "Docker service-account preflight failed: $($result.error)"
        }
        if ($result.containerSmokeExit -ne 0 -or $taskInfo.LastTaskResult -ne 0) {
            throw "Windows container smoke failed under the runner service account: $($result.error)"
        }

        return [pscustomobject]@{
            Identity = $result.identity
            DockerOSType = $result.dockerOSType
            ContainerImage = "mcr.microsoft.com/windows/nanoserver:ltsc2022"
            Isolation = "hyperv"
            Result = "passed under the runner service account"
        }
    } finally {
        if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
        }
        if (Test-Path -LiteralPath $probeDirectory) {
            Remove-Item -LiteralPath $probeDirectory -Recurse -Force
        }
        if ($passwordPointer -ne [IntPtr]::Zero) {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
        }
        $passwordText = $null
    }
}

function Get-RunnerRegistrationPlan {
    param(
        [Parameter(Mandatory)]
        [string]$Repository,
        [Parameter(Mandatory)]
        [string]$RunnerName,
        [Parameter(Mandatory)]
        [string]$RunnerRoot,
        [Parameter(Mandatory)]
        [string[]]$Labels
    )

    $rootPath = [System.IO.Path]::GetFullPath($RunnerRoot)
    $configPath = Join-Path $rootPath "config.cmd"
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Runner configuration command not found: '$configPath'."
    }
    if ($Labels.Count -eq 0 -or @($Labels | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count -gt 0) {
        throw "At least one nonblank custom runner label is required."
    }
    if ($Labels | Where-Object { $_ -match "," }) {
        throw "Pass labels as separate -Labels values; labels cannot contain commas."
    }

    $null = Invoke-Gh -Arguments @("auth", "status", "--hostname", "github.com")
    $runner = Get-GitHubRunner -Repository $Repository -RunnerName $RunnerName
    $queuedRuns = Get-GitHubRunCount -Repository $Repository -Status "queued"
    $inProgressRuns = Get-GitHubRunCount -Repository $Repository -Status "in_progress"

    Assert-RunnerSafeToReconfigure -Runner $runner -QueuedRuns $queuedRuns -InProgressRuns $inProgressRuns

    $localState = Get-RunnerLocalState -RunnerRoot $rootPath
    if ($localState.Listeners.Count -gt 0) {
        throw "Runner.Listener.exe is still running from '$rootPath'; stop the runner and retry after it reports offline."
    }
    $runningServices = @($localState.Services | Where-Object { $_.State -ne "Stopped" })
    if ($runningServices.Count -gt 0) {
        throw "A runner service for '$rootPath' is not stopped; stop it and retry after the runner reports offline."
    }
    if ($localState.Services.Count -gt 1) {
        throw "Multiple runner services reference '$rootPath'; refusing to remove an ambiguous service set."
    }

    # Docker is only required when the runner actually advertises a 'docker' label;
    # this script is also reused to register non-Docker native Windows runners.
    $requiresDocker = [bool]($Labels | Where-Object { $_ -eq "docker" })
    $dockerOsType = if ($requiresDocker) { Get-DockerOSType } else { $null }
    return [pscustomobject]@{
        Repository = $Repository
        RunnerName = $RunnerName
        RunnerRoot = $rootPath
        RequiresDocker = $requiresDocker
        DockerOSType = $dockerOsType
        ExistingRunnerId = if ($runner) { $runner.id } else { $null }
        ExistingRunnerStatus = if ($runner) { $runner.status } else { "absent" }
        ExistingRunnerBusy = if ($runner) { [bool]$runner.busy } else { $false }
        QueuedRuns = $queuedRuns
        InProgressRuns = $inProgressRuns
        LocalConfigPresent = $localState.LocalConfigPresent
        StoppedServicesToRemove = @($localState.Services | ForEach-Object { $_.Name })
        CustomLabels = @($Labels)
    }
}

function Get-RunnerRegistrationToken {
    param(
        [Parameter(Mandatory)]
        [string]$Repository
    )

    $output = Invoke-Gh -Arguments @(
        "api", "--method", "POST",
        "repos/$Repository/actions/runners/registration-token",
        "--jq", ".token"
    )
    $token = [string]::Join("", [string[]]$output).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "GitHub did not return a runner registration token."
    }
    return $token
}

function Set-RunnerProcessEnvironmentValue {
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute("PSUseShouldProcessForStateChangingFunctions", "", Justification = "Changes only transient process environment variables and restores them in a finally block.")]
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [AllowNull()]
        [object]$Value
    )

    if ($null -eq $Value) {
        Remove-Item -Path "Env:$Name" -ErrorAction SilentlyContinue
    } else {
        [System.Environment]::SetEnvironmentVariable($Name, [string]$Value, "Process")
    }
}

function Invoke-RunnerConfiguration {
    param(
        [Parameter(Mandatory)]
        [string]$ConfigPath,
        [Parameter(Mandatory)]
        [string]$Repository,
        [Parameter(Mandatory)]
        [string]$RegistrationToken,
        [Parameter(Mandatory)]
        [string]$RunnerName,
        [Parameter(Mandatory)]
        [string[]]$Labels,
        [Parameter(Mandatory)]
        [string]$WorkFolder,
        [Parameter(Mandatory)]
        [string]$ServiceAccount,
        [Parameter(Mandatory)]
        [securestring]$ServicePassword,
        [switch]$ReplaceExisting,
        [scriptblock]$ConfigInvoker
    )

    # NAME/URL/LABELS/WORK/WINDOWSLOGONACCOUNT are passed as EXPLICIT CLI
    # ARGUMENTS below, not environment variables. Root-caused 2026-11-02
    # (recurred on two separate VMs, "testcoord" and "uer", after an earlier
    # investigation incorrectly concluded it was a one-off transient glitch):
    # Runner.Listener.exe configure --unattended intermittently read a WRONG
    # value (the Linux host's own hostname) from the ACTIONS_RUNNER_INPUT_NAME
    # environment variable, confirmed via its own _diag log ("Env arg 'name':
    # 'sergio-ThinkPad-P16-Gen-3'") despite this script setting the Process-
    # scoped env var to the correct value moments earlier, and despite
    # independently verifying (live, on the affected guest) that env var
    # inheritance through the SAME process chain (pwsh -> nested pwsh ->
    # cmd.exe) works correctly in isolation. The exact mechanism was never
    # pinned down, but CLI arguments are actions/runner's primary, most
    # battle-tested configuration channel (the one GitHub's own UI generates)
    # and sidestep whatever is unreliable about the env-var channel for these
    # fields. TOKEN and WINDOWSLOGONPASSWORD stay as environment variables
    # deliberately, to avoid putting secrets in a process command line
    # visible to other processes via Get-CimInstance Win32_Process etc.
    $inputValues = [ordered]@{
        ACTIONS_RUNNER_INPUT_TOKEN = $RegistrationToken
    }

    $secretPointer = [IntPtr]::Zero
    $passwordText = $null
    $previousValues = @{}
    try {
        $secretPointer = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($ServicePassword)
        $passwordText = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
        $inputValues["ACTIONS_RUNNER_INPUT_WINDOWSLOGONPASSWORD"] = $passwordText

        foreach ($name in $inputValues.Keys) {
            $previous = [System.Environment]::GetEnvironmentVariable($name, "Process")
            $previousValues[$name] = $previous
            if (-not [string]::IsNullOrEmpty($previous)) {
                throw "Conflicting $name environment variable exists; clear it before configuring the runner."
            }
        }
        foreach ($name in $inputValues.Keys) {
            [System.Environment]::SetEnvironmentVariable($name, [string]$inputValues[$name], "Process")
        }

        $arguments = @(
            "--unattended", "--runasservice",
            "--url", "https://github.com/$Repository",
            "--name", $RunnerName,
            "--labels", ($Labels -join ","),
            "--work", $WorkFolder,
            "--windowslogonaccount", $ServiceAccount
        )
        if ($ReplaceExisting) {
            $arguments += "--replace"
        }

        if ($ConfigInvoker) {
            $exitCode = & $ConfigInvoker $ConfigPath $arguments
        } else {
            & $ConfigPath @arguments
            $exitCode = $LASTEXITCODE
        }
        if ($exitCode -ne 0) {
            throw "Runner configuration failed with exit code $exitCode."
        }

        return [pscustomobject]@{
            ConfigPath = $ConfigPath
            Arguments = $arguments
            ExitCode = $exitCode
        }
    } finally {
        foreach ($name in $previousValues.Keys) {
            Set-RunnerProcessEnvironmentValue -Name $name -Value $previousValues[$name]
        }
        if ($secretPointer -ne [IntPtr]::Zero) {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer)
        }
        $passwordText = $null
        $RegistrationToken = $null
    }
}

function Test-RunnerAdminContext {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [System.Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Wait-ForRunnerService {
    param(
        [Parameter(Mandatory)]
        [string]$RunnerRoot,
        [Parameter(Mandatory)]
        [string]$ServiceAccount,
        [int]$TimeoutSeconds = 60
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $localState = Get-RunnerLocalState -RunnerRoot $RunnerRoot
        if ($localState.Services.Count -gt 1) {
            throw "Multiple runner services reference '$RunnerRoot' after configuration."
        }
        if ($localState.Services.Count -eq 1) {
            $service = $localState.Services[0]
            if (-not (Test-ServiceAccountMatch -Actual $service.StartName -Expected $ServiceAccount)) {
                throw "Runner service identity does not match the requested account."
            }
            if ($service.State -eq "Stopped") {
                Start-Service -Name $service.Name -ErrorAction Stop
            }
            $serviceController = Get-Service -Name $service.Name -ErrorAction Stop
            if ($serviceController.Status -eq [System.ServiceProcess.ServiceControllerStatus]::Running) {
                return $service
            }
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    throw "The runner service did not reach Running within $TimeoutSeconds seconds."
}

function Wait-ForRegisteredRunner {
    param(
        [Parameter(Mandatory)]
        [string]$Repository,
        [Parameter(Mandatory)]
        [string]$RunnerName,
        [Parameter(Mandatory)]
        [string[]]$Labels,
        [int]$TimeoutSeconds = 90
    )

    $expectedLabels = @("self-hosted", "Windows", "X64") + $Labels
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $runner = Get-GitHubRunner -Repository $Repository -RunnerName $RunnerName
        if ($runner -and $runner.status -eq "online") {
            $actualLabels = @($runner.labels | ForEach-Object { $_.name })
            $missing = @($expectedLabels | Where-Object { $_ -notin $actualLabels })
            if ($missing.Count -gt 0) {
                throw "Registered runner is missing expected labels: $($missing -join ', ')."
            }
            return $runner
        }
        Start-Sleep -Seconds 3
    } while ((Get-Date) -lt $deadline)

    throw "The runner did not appear online within $TimeoutSeconds seconds."
}

if (-not $script:RunnerRegistrationDotSourced) {
    if (-not $AuthorizeActionsStatus) {
        throw "Pass -AuthorizeActionsStatus to authorize read-only Actions runner and queue checks."
    }
    if (-not (Test-Path -LiteralPath $RunnerRoot -PathType Container)) {
        throw "Runner root not found: '$RunnerRoot'."
    }

    $plan = Get-RunnerRegistrationPlan -Repository $Repository -RunnerName $RunnerName -RunnerRoot $RunnerRoot -Labels $Labels

    if (-not $Run) {
        $plan
        return
    }

    if (-not $PSCmdlet.ShouldProcess("$Repository/$RunnerName", "Re-register offline Windows runner as a service")) {
        return
    }
    if (-not (Test-RunnerAdminContext)) {
        throw "Run this command from an elevated PowerShell under the intended Windows service account."
    }

    $registrationToken = $null
    $securePassword = $null
    try {
        $currentPlan = Get-RunnerRegistrationPlan -Repository $Repository -RunnerName $RunnerName -RunnerRoot $RunnerRoot -Labels $Labels
        if ($currentPlan.ExistingRunnerId -ne $plan.ExistingRunnerId) {
            throw "Runner registration changed after the initial plan; rerun the tool."
        }

        $registrationToken = Get-RunnerRegistrationToken -Repository $Repository
        if ($UseDedicatedServiceAccount) {
            $securePassword = New-RunnerServicePassword
            $serviceAccount = Initialize-DedicatedRunnerServiceAccount -Name $DedicatedServiceAccountName -Password $securePassword -RequiresDocker:$currentPlan.RequiresDocker
        } else {
            $serviceAccount = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            if ($serviceAccount -match "^(NT AUTHORITY\\)?(SYSTEM|LOCAL SERVICE|NETWORK SERVICE)$") {
                throw "Built-in service accounts are not supported. Run elevated as the intended Windows service account or use -UseDedicatedServiceAccount."
            }
            $securePassword = Read-Host -Prompt "Windows service password for '$serviceAccount'" -AsSecureString
            if ($securePassword.Length -eq 0) {
                throw "A nonempty Windows service password is required."
            }
        }

        $dockerSmoke = if ($currentPlan.RequiresDocker) {
            Test-RunnerDockerServiceAccount -ServiceAccount $serviceAccount -ServicePassword $securePassword
        } else {
            $null
        }

        foreach ($serviceName in $currentPlan.StoppedServicesToRemove) {
            Remove-Service -Name $serviceName -ErrorAction Stop
        }
        if ($currentPlan.LocalConfigPresent) {
            $configPath = Join-Path $RunnerRoot "config.cmd"
            & $configPath remove --local
            if ($LASTEXITCODE -ne 0) {
                throw "Local runner configuration removal failed with exit code $LASTEXITCODE."
            }
        }

        $configPath = Join-Path $RunnerRoot "config.cmd"
        $null = Invoke-RunnerConfiguration -ConfigPath $configPath -Repository $Repository -RegistrationToken $registrationToken -RunnerName $RunnerName -Labels $Labels -WorkFolder $WorkFolder -ServiceAccount $serviceAccount -ServicePassword $securePassword -ReplaceExisting:($null -ne $currentPlan.ExistingRunnerId)
        $registrationToken = $null
        $securePassword.Dispose()
        $securePassword = $null

        $service = Wait-ForRunnerService -RunnerRoot $RunnerRoot -ServiceAccount $serviceAccount
        $runner = Wait-ForRegisteredRunner -Repository $Repository -RunnerName $RunnerName -Labels $Labels
        [pscustomobject]@{
            Repository = $Repository
            RunnerName = $RunnerName
            RunnerStatus = $runner.status
            Busy = [bool]$runner.busy
            Labels = @($runner.labels | ForEach-Object { $_.name })
            ServiceName = $service.Name
            ServiceState = $service.State
            ServiceAccount = $service.StartName
            DockerOSType = $currentPlan.DockerOSType
            DockerServiceSmokeTest = if ($dockerSmoke) { $dockerSmoke.Result } else { "skipped (no docker label)" }
            DockerSmokeIdentity = if ($dockerSmoke) { $dockerSmoke.Identity } else { $null }
            DockerSmokeContainer = if ($dockerSmoke) { "$($dockerSmoke.ContainerImage) ($($dockerSmoke.Isolation) isolation)" } else { $null }
        }
    } finally {
        $registrationToken = $null
        if ($securePassword -is [securestring]) {
            $securePassword.Dispose()
        }
    }
}
