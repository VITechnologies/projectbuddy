<#
.SYNOPSIS
    Enables Issues on a GitHub repository and seeds the first-class
    human-escalation label, idempotently.

.DESCRIPTION
    Companion to New-HumanEscalation.ps1/Get-HumanEscalations.ps1: those
    scripts assume Issues is already enabled on the target repo. Many repos
    this engine builds for (project-specific LabVIEW source repos, plugin
    module repos, etc.) start with Issues disabled, which forces their
    technical issues into whichever consumer repo happened to drive the
    work instead of their own tracker.

    This script only ever enables Issues -- it never disables it or touches
    any other repository setting (wiki, projects, etc.), and is a no-op if
    Issues is already enabled. Project-agnostic: the target repo is always
    an explicit input.

.PARAMETER Repo
    Target repository in "owner/name" form.

.PARAMETER Label
    Label to seed once Issues is enabled, so the repo is immediately ready
    to receive first-class human escalations. Defaults to
    "needs-human-decision", matching New-HumanEscalation.ps1's default. Pass
    an empty string to skip label seeding.

.EXAMPLE
    ./Enable-RepoIssueTracking.ps1 -Repo "svelderrainruiz/core"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [string]$Label = "needs-human-decision"
)

$ErrorActionPreference = "Stop"

if ($Repo -notmatch "^[^/\s]+/[^/\s]+$") {
    throw "-Repo must be in 'owner/name' form, got: $Repo"
}

if (-not (Get-Command -Name "gh" -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI ('gh') is required and was not found on PATH."
}

$current = & gh api "repos/$Repo" --jq ".has_issues" 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Could not read repository settings for $Repo. Ensure it exists and the authenticated token can read it: $current"
}

if ($current -eq "true") {
    Write-Output "$Repo already has Issues enabled; no change made."
} else {
    & gh repo edit $Repo --enable-issues | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to enable Issues on $Repo. The authenticated token needs 'administration:write' (or equivalent admin) permission on this repo."
    }
    Write-Output "Enabled Issues on $Repo."
}

if (-not [string]::IsNullOrWhiteSpace($Label)) {
    & gh label create $Label --repo $Repo --color "B60205" --description "Blocked on a human decision; not resolvable by further automated iteration" 2>$null
    $null = $LASTEXITCODE # swallow non-zero from "already exists" without masking earlier checks
    Write-Output "Ensured label '$Label' exists on $Repo."
}
