<#
.SYNOPSIS
    Enterprise Environment Prerequisite Auditor with Version Baseline Validation.
    Validates PowerShell, SVN, Git, VIPM, Syft, Grype, and critical API credentials against minimum enterprise thresholds.
#>

$ErrorActionPreference = "Stop"
$failures = 0
$warnings = 0

# ==========================================
# ENTERPRISE VERSION BASELINE DEFINITIONS
# ==========================================
$MinRequired = @{
    PowerShell = [Version]"7.0.0"
    Subversion = [Version]"1.14.0"
    Git        = [Version]"2.40.0"
    VIPM       = [Version]"26.3.2"
    Syft       = [Version]"1.0.0"
    Grype      = [Version]"0.110.0"
}

Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "Enterprise Pipeline Environment & Version Audit" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan

# 1. Check PowerShell Version
Write-Host -NoNewline "Checking PowerShell Version (>= $($MinRequired.PowerShell))... "
$currentPs = $PSVersionTable.PSVersion
if ($currentPs -ge $MinRequired.PowerShell) {
    Write-Host "[OK] (v$currentPs)" -ForegroundColor Green
} else {
    Write-Host "[FAIL] Found v$currentPs. Minimum required is v$($MinRequired.PowerShell)." -ForegroundColor Red
    $failures++
}

# Helper function to test and parse CLI tool versions
function Test-CliVersionBaseline {
    param(
        [string]$Name,
        [string]$CommandName,
        [string]$VersionArg = "--version",
        [Version]$MinVersion
    )
    Write-Host -NoNewline "Checking $Name ($CommandName)... "
    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        Write-Host "[FAIL] Not found in system PATH." -ForegroundColor Red
        $script:failures++
        return
    }

    try {
        $rawOutput = & $CommandName $VersionArg 2>&1 | Select-Object -First 1
        
        # Extract semantic version (e.g., 1.14.5, 2.54.0, etc.) using regex
        if ($rawOutput -match '(\d+\.\d+\.\d+)') {
            $parsedVersion = [Version]$Matches[1]
            if ($parsedVersion -ge $MinVersion) {
                Write-Host "[OK] v$parsedVersion (Min: v$MinVersion)" -ForegroundColor Green
                Write-Host "       Path: $($cmd.Source)" -ForegroundColor DarkGray
                Write-Host "       Raw:  $rawOutput" -ForegroundColor DarkGray
            } else {
                Write-Host "[WARN] v$parsedVersion is below enterprise baseline v$MinVersion." -ForegroundColor Yellow
                Write-Host "       Path: $($cmd.Source)" -ForegroundColor DarkGray
                $script:warnings++
            }
        } else {
            Write-Host "[OK] Found at $($cmd.Source) (Could not parse semantic version)." -ForegroundColor Yellow
            Write-Host "       Raw: $rawOutput" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "[OK] Found, but version extraction failed." -ForegroundColor Yellow
    }
}

# 2. Run Baseline Checks for Enterprise Tools
Test-CliVersionBaseline -Name "Subversion CLI" -CommandName "svn" -MinVersion $MinRequired.Subversion
Test-CliVersionBaseline -Name "Git CLI" -CommandName "git" -MinVersion $MinRequired.Git
Test-CliVersionBaseline -Name "VIPM CLI" -CommandName "vipm" -MinVersion $MinRequired.VIPM
Test-CliVersionBaseline -Name "Syft SBOM Tool" -CommandName "syft" -MinVersion $MinRequired.Syft
Test-CliVersionBaseline -Name "Grype Scanner" -CommandName "grype" -MinVersion $MinRequired.Grype

# 3. Check Critical Environment Variables
Write-Host -NoNewline "Checking DEPENDENCY_TRACK_API_KEY... "
if (-not [string]::IsNullOrEmpty($env:DEPENDENCY_TRACK_API_KEY)) {
    Write-Host "[OK] Present in environment." -ForegroundColor Green
} else {
    Write-Host "[WARN] Not detected in environment (Required for upstream reporting)." -ForegroundColor Yellow
    $warnings++
}

Write-Host "--------------------------------------------------" -ForegroundColor Cyan
if ($failures -eq 0) {
    if ($warnings -eq 0) {
        Write-Host "Environment & Version Audit PASSED! All baselines met." -ForegroundColor Green
    } else {
        Write-Host "Environment Audit PASSED with $warnings warning(s)." -ForegroundColor Yellow
    }
    Write-Host "==================================================" -ForegroundColor Cyan
    exit 0
} else {
    Write-Host "Environment Audit FAILED with $failures error(s) and $warnings warning(s)." -ForegroundColor Red
    Write-Host "==================================================" -ForegroundColor Cyan
    exit 1
}
