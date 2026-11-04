#requires -Version 7.0
<#
.SYNOPSIS
    Reports whether a set of named command-line tools are present on PATH,
    along with their resolved version where available.

.DESCRIPTION
    Minimal, project-agnostic toolchain smoke test. Takes no project-specific
    configuration and has no side effects (it never installs, downloads, or
    modifies anything) -- its only purpose is to prove that a script shipped
    from VITechnologies/projectbuddy can be consumed, verified, and executed
    by a downstream repository's workflow, exactly as documented in that
    repository's README.md "Consumption model" section.

.PARAMETER ToolNames
    One or more command names to check for on PATH (e.g. 'git', 'pwsh').

.EXAMPLE
    ./Test-ToolchainPresence.ps1 -ToolNames git, pwsh
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string[]]$ToolNames
)

$ErrorActionPreference = "Stop"

$results = foreach ($name in $ToolNames) {
    $command = Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    [pscustomobject]@{
        Tool    = $name
        Present = [bool]$command
        Path    = if ($command) { $command.Source } else { $null }
    }
}

$results | Format-Table -AutoSize | Out-String | Write-Output

$missing = @($results | Where-Object { -not $_.Present })
if ($missing.Count -gt 0) {
    throw "Missing required tool(s): $(($missing | ForEach-Object { $_.Tool }) -join ', ')"
}

Write-Output "All $($results.Count) requested tool(s) present."
