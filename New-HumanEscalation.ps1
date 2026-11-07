<#
.SYNOPSIS
    Creates or updates a structured, first-class "needs human decision" GitHub
    issue for a blocker that automated iteration cannot resolve on its own.

.DESCRIPTION
    Project-agnostic escalation tool: every identifying value (target repo,
    escalation id, content) is a required or explicit input -- this script
    has no built-in default pointing at any particular fork or project,
    matching this engine's existing consumption model.

    Escalation is idempotent and keyed on -EscalationId, not -Title: calling
    this again with the same -EscalationId against the same -Repo updates the
    existing open issue in place (new context/recommendation, label and
    assignee re-applied) instead of creating a duplicate. This lets an agent
    re-run the same escalation call every time it re-confirms a blocker is
    still unresolved, without accumulating duplicate issues.

    The issue body always carries a hidden `<!-- escalation-id: ... -->`
    marker used for that idempotent lookup, and a `-Label` (auto-created in
    the target repo if it does not already exist) used by
    Get-HumanEscalations.ps1 to build a cross-repo dashboard.

.PARAMETER Repo
    Target repository in "owner/name" form. Must already have Issues enabled.

.PARAMETER EscalationId
    Stable slug identifying this specific blocker (e.g.
    "core-ab-engine-build-failure"). Reused across calls to update the same
    issue instead of creating a new one.

.PARAMETER Title
    Issue title. Only applied when creating a new issue -- when updating an
    existing escalation, the original title is left untouched so humans can
    rename it without a later agent run silently overwriting that rename.

.PARAMETER Context
    What is blocked, and why it matters (plain language, assume the reader
    has no prior context on this specific blocker).

.PARAMETER WhyAgentCannotResolve
    The specific reason this is not a "needs more iteration" case: e.g. a
    genuine semantic/architectural judgment call, a dependency on another
    unresolved blocker, access/credentials the agent does not hold, or a
    risk (such as a silent correctness bug) that was deliberately not
    auto-fixed.

.PARAMETER OptionsConsidered
    Zero or more concrete options already identified, each rendered as a
    bullet point.

.PARAMETER Recommendation
    The agent's best-guess recommendation, if it has one. Optional: leave
    blank when no option is clearly preferable.

.PARAMETER RelatedIssues
    Zero or more related issue references (e.g.
    "VITechnologies/SandboxSBOMReporting#302"), rendered as bullet points.

.PARAMETER Assignee
    GitHub username to assign the issue to. Optional.

.PARAMETER Label
    Label applied to the issue, created in the target repo (with a fixed
    color/description) if it does not already exist. Defaults to
    "needs-human-decision" so every consumer gets a consistent, greppable
    convention unless it deliberately opts into a different one.

.EXAMPLE
    ./New-HumanEscalation.ps1 `
        -Repo "VITechnologies/SandboxSBOMReporting" `
        -EscalationId "core-ab-engine-build-failure" `
        -Title "core Source Distribution build: unresolved AB_Engine failure needs human judgment" `
        -Context "core's Source Distribution build (issue #302) still fails after 3 unrelated pre-repo-split reference bugs were found and fixed via CI; this blocks UserEventRecorder and replay's own builds." `
        -WhyAgentCannotResolve "15+ CI dispatches across 2 sessions have not surfaced a root cause; MassCompile is clean and the failure is a blank, non-descriptive 'File Not Found' from the Application Builder itself. Further blind CI iteration has diminishing odds of success; this likely needs either an interactive LabVIEW IDE session or an NI support ticket." `
        -OptionsConsidered @("Keep iterating via CI dispatch + VNC diagnosis", "Open an NI support ticket with the collected error text", "Have a human drive an interactive Application Builder run to see the real underlying error") `
        -Recommendation "Have a human drive one interactive Application Builder run first (fastest path to a readable error); fall back to an NI support ticket only if that is also silent." `
        -RelatedIssues @("VITechnologies/SandboxSBOMReporting#302") `
        -Assignee "svelderrainruiz"
#>

param(
    [Parameter(Mandatory = $true)]
    [string]$Repo,

    [Parameter(Mandatory = $true)]
    [string]$EscalationId,

    [Parameter(Mandatory = $true)]
    [string]$Title,

    [Parameter(Mandatory = $true)]
    [string]$Context,

    [Parameter(Mandatory = $true)]
    [string]$WhyAgentCannotResolve,

    [string[]]$OptionsConsidered = @(),

    [string]$Recommendation = "",

    [string[]]$RelatedIssues = @(),

    [string]$Assignee = "",

    [string]$Label = "needs-human-decision"
)

$ErrorActionPreference = "Stop"

if ($Repo -notmatch "^[^/\s]+/[^/\s]+$") {
    throw "-Repo must be in 'owner/name' form, got: $Repo"
}
if ([string]::IsNullOrWhiteSpace($EscalationId) -or $EscalationId -match "\s") {
    throw "-EscalationId must be a non-blank slug with no whitespace, got: '$EscalationId'"
}

if (-not (Get-Command -Name "gh" -CommandType Application -ErrorAction SilentlyContinue)) {
    throw "GitHub CLI ('gh') is required and was not found on PATH."
}

function ConvertTo-BulletList {
    param([string[]]$Items)
    if (-not $Items -or $Items.Count -eq 0) {
        return "_None identified._"
    }
    return ($Items | ForEach-Object { "- $_" }) -join "`n"
}

$marker = "<!-- escalation-id: $EscalationId -->"
$recommendationText = if ([string]::IsNullOrWhiteSpace($Recommendation)) { "_No single option is clearly preferable yet._" } else { $Recommendation }

$body = @"
## Context

$Context

## Why the agent cannot resolve this

$WhyAgentCannotResolve

## Options considered

$(ConvertTo-BulletList -Items $OptionsConsidered)

## Recommendation

$recommendationText

## Related issues

$(ConvertTo-BulletList -Items $RelatedIssues)

$marker
<!-- generated-by: projectbuddy/New-HumanEscalation.ps1 -->
"@

# Ensure the escalation label exists in the target repo (idempotent: ignore
# the error gh raises when it already exists).
& gh label create $Label --repo $Repo --color "B60205" --description "Blocked on a human decision; not resolvable by further automated iteration" 2>$null
$null = $LASTEXITCODE # swallow non-zero from "already exists" without masking later checks

$bodyFile = New-TemporaryFile
try {
    Set-Content -LiteralPath $bodyFile -Value $body -NoNewline -Encoding utf8

    $existingJson = & gh issue list --repo $Repo --state open --search "`"$marker`" in:body" --json number,url,body 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "gh issue list search failed (exit $LASTEXITCODE): $existingJson -- falling through to create a new issue. This may create a duplicate if a matching escalation already exists; check $Repo manually if that matters here."
        $existing = @()
    } else {
        # GitHub's issue/code search is tokenized and eventually consistent
        # -- it can return a false match (or, after a very recent prior
        # create, a false miss). Re-verify client-side that the exact
        # marker string is actually present in each candidate's real body
        # before treating it as "the" existing escalation, rather than
        # trusting the search index alone.
        $candidates = if ([string]::IsNullOrWhiteSpace($existingJson)) { @() } else { $existingJson | ConvertFrom-Json }
        $existing = @($candidates | Where-Object { $_.body -and $_.body.Contains($marker) })
    }

    if ($existing -and $existing.Count -gt 0) {
        $number = $existing[0].number
        $editArgs = @("issue", "edit", $number, "--repo", $Repo, "--body-file", $bodyFile, "--add-label", $Label)
        if (-not [string]::IsNullOrWhiteSpace($Assignee)) {
            $editArgs += @("--add-assignee", $Assignee)
        }
        & gh @editArgs | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to update existing escalation issue #$number in $Repo."
        }
        Write-Output "Updated existing escalation '$EscalationId': $($existing[0].url)"
    } else {
        $createArgs = @("issue", "create", "--repo", $Repo, "--title", $Title, "--body-file", $bodyFile, "--label", $Label)
        if (-not [string]::IsNullOrWhiteSpace($Assignee)) {
            $createArgs += @("--assignee", $Assignee)
        }
        $url = & gh @createArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to create escalation issue in $Repo."
        }
        Write-Output "Created new escalation '$EscalationId': $url"
    }
} finally {
    Remove-Item -LiteralPath $bodyFile -ErrorAction SilentlyContinue
}
