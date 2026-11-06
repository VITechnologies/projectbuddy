<#
.SYNOPSIS
    Lists open "needs human decision" escalation issues across one or more
    repositories, as a single consolidated dashboard.

.DESCRIPTION
    Companion to New-HumanEscalation.ps1: queries every repo in -Repos for
    open issues carrying -Label (defaulting to the same
    "needs-human-decision" convention) and prints a flat, sorted table of
    repo, issue number, title, assignee, and URL. Project-agnostic: the
    repository list is always an explicit input, never a built-in default.

.PARAMETER Repos
    One or more repositories in "owner/name" form to query.

.PARAMETER Label
    Label to filter on. Defaults to "needs-human-decision", matching
    New-HumanEscalation.ps1's default.

.PARAMETER AsJson
    Emit the raw combined result set as JSON instead of a formatted table
    (for consumption by another script or workflow step).

.EXAMPLE
    ./Get-HumanEscalations.ps1 -Repos `
        "VITechnologies/SandboxSBOMReporting", `
        "svelderrainruiz/projectbuddy"
#>

param(
    [Parameter(Mandatory = $true)]
    [string[]]$Repos,

    [string]$Label = "needs-human-decision",

    [switch]$AsJson
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command -Name "gh" -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI ('gh') is required and was not found on PATH."
}

$results = @()
foreach ($repo in $Repos) {
    if ($repo -notmatch "^[^/\s]+/[^/\s]+$") {
        throw "Each entry in -Repos must be in 'owner/name' form, got: $repo"
    }
    $json = & gh issue list --repo $repo --state open --label $Label --json number,title,url,assignees,createdAt,updatedAt 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Could not list issues for $repo (Issues may be disabled, or the label does not exist there yet). Skipping."
        continue
    }
    if ([string]::IsNullOrWhiteSpace($json)) {
        continue
    }
    $issues = $json | ConvertFrom-Json
    foreach ($issue in $issues) {
        $results += [pscustomobject]@{
            Repo       = $repo
            Number     = $issue.number
            Title      = $issue.title
            Assignees  = ($issue.assignees | ForEach-Object { $_.login }) -join ","
            UpdatedAt  = $issue.updatedAt
            Url        = $issue.url
        }
    }
}

if ($AsJson) {
    $results | ConvertTo-Json -Depth 4
    return
}

if ($results.Count -eq 0) {
    Write-Output "No open '$Label' escalations found across: $($Repos -join ', ')"
    return
}

$results | Sort-Object Repo, Number | Format-Table -AutoSize Repo, Number, Title, Assignees, UpdatedAt, Url
