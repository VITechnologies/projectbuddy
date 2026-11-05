#!/usr/bin/env python3
"""Drive Phase 3 guest provisioning (VIPM + LabVIEW Professional install) and
runner registration over WinRM, the only reliable automation channel for this
VM (see /memories/repo/ubuntu-vm-automation.md: VBoxManage guestcontrol
reliably fails with "guest execution service is not ready" against this
host/VM combination).

Connects with NTLM/Negotiate transport, not Basic -- Basic auth never actually
works here even though Enable-WinRmAutomation.ps1 configures it as a fallback
(the listener only ever offers WWW-Authenticate: Negotiate in testing).

Each subcommand is independently idempotent and verifies its own outcome
before reporting success; none of them touch LabVIEW/VIPM activation, which
remains an operator-driven interactive step.
"""
import argparse
import base64
import hashlib
import sys

try:
    import winrm
except ImportError:
    print("pywinrm is required: python3 -m venv .venv && .venv/bin/pip install pywinrm", file=sys.stderr)
    raise


def connect(host: str, port: int, user: str, password_file: str, read_timeout_sec: int = 60, operation_timeout_sec: int = 55) -> "winrm.Session":
    with open(password_file, "r", encoding="utf-8") as handle:
        password = handle.read().rstrip("\n")
    # NOTE: timeouts must be passed via the constructor; setting
    # session.protocol.read_timeout_sec post-hoc silently has no effect
    # (proven live: a post-hoc assignment still produced a 30s ReadTimeoutError).
    return winrm.Session(
        f"{host}:{port}",
        auth=(user, password),
        transport="ntlm",
        read_timeout_sec=read_timeout_sec,
        operation_timeout_sec=operation_timeout_sec,
    )


def run_ps(session: "winrm.Session", script: str) -> "winrm.Response":
    result = session.run_ps(script)
    sys.stdout.write(result.std_out.decode(errors="replace"))
    sys.stderr.write(result.std_err.decode(errors="replace"))
    return result


def cmd_wait_ready(args: argparse.Namespace) -> int:
    session = connect(args.host, args.port, args.user, args.password_file)
    result = run_ps(session, "Write-Output ((Get-CimInstance Win32_OperatingSystem).Caption)")
    if result.status_code != 0:
        print("Guest is not responding over WinRM yet.", file=sys.stderr)
        return 1
    print("WinRM is ready.")
    return 0


def cmd_verify_winrm_setup(args: argparse.Namespace) -> int:
    """Independently verify the guest's actual configured state matches what
    winrm-setup-installer.nsi (issue #192/#200/#201, epic #198) is supposed
    to have done -- not just that WinRM responds at all, but that it was
    actually configured by that installer's real steps, not by some other
    stale/partial mechanism. Issue #202's acceptance criteria: the resulting
    VM must be "otherwise indistinguishable from one provisioned the old
    way" (Enable-WinRmAutomation.ps1) -- this uses the .nsi's own step list
    as the checklist, exactly as that issue specifies, since it's the one
    actually running now.

    Each check is independent and reported individually (PASS/FAIL with the
    actual observed value) so a failure here points directly at which .nsi
    step didn't take effect, rather than just "WinRM is broken somehow".
    """
    session = connect(args.host, args.port, args.user, args.password_file)
    script = r"""
$checks = [System.Collections.Generic.List[object]]::new()
function Add-Check($name, $pass, $detail) {
    $checks.Add([pscustomobject]@{ Name = $name; Pass = $pass; Detail = $detail })
}

foreach ($svcName in 'wuauserv', 'UsoSvc', 'InstallService') {
    $svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
    if (-not $svc) {
        Add-Check "Service $svcName disabled" $false "service not found"
    } else {
        Add-Check "Service $svcName disabled" ($svc.StartType -eq 'Disabled') "StartType=$($svc.StartType)"
    }
}

$policyValue = (Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name LocalAccountTokenFilterPolicy -ErrorAction SilentlyContinue).LocalAccountTokenFilterPolicy
Add-Check 'LocalAccountTokenFilterPolicy = 1' ($policyValue -eq 1) "value=$policyValue"

$publicProfiles = Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq 'Public'
Add-Check 'No network profile left as Public' ($null -eq $publicProfiles -or @($publicProfiles).Count -eq 0) "public count=$(@($publicProfiles).Count)"

$allowUnencrypted = (Get-Item WSMan:\localhost\Service\AllowUnencrypted -ErrorAction SilentlyContinue).Value
Add-Check 'WSMan AllowUnencrypted = true' ($allowUnencrypted -eq 'true') "value=$allowUnencrypted"

$basicAuth = (Get-Item WSMan:\localhost\Service\Auth\Basic -ErrorAction SilentlyContinue).Value
Add-Check 'WSMan Basic auth = true' ($basicAuth -eq 'true') "value=$basicAuth"

$maxMem = (Get-Item WSMan:\localhost\Shell\MaxMemoryPerShellMB -ErrorAction SilentlyContinue).Value
Add-Check 'WSMan MaxMemoryPerShellMB = 1024' ($maxMem -eq '1024') "value=$maxMem"

$fwRule = Get-NetFirewallRule -Name SandboxSbom-WinRM-HTTP -ErrorAction SilentlyContinue
if (-not $fwRule) {
    Add-Check 'Firewall rule SandboxSbom-WinRM-HTTP exists and allows inbound 5985' $false 'rule not found'
} else {
    $portFilter = $fwRule | Get-NetFirewallPortFilter
    $ok = ($fwRule.Enabled -eq 'True') -and ($fwRule.Direction -eq 'Inbound') -and ($fwRule.Action -eq 'Allow') -and ($portFilter.LocalPort -eq '5985')
    Add-Check 'Firewall rule SandboxSbom-WinRM-HTTP exists and allows inbound 5985' $ok "Enabled=$($fwRule.Enabled) Direction=$($fwRule.Direction) Action=$($fwRule.Action) LocalPort=$($portFilter.LocalPort)"
}

$winrmSvc = Get-Service -Name WinRM
Add-Check 'WinRM service: StartType Automatic, Status Running' (($winrmSvc.StartType -eq 'Automatic') -and ($winrmSvc.Status -eq 'Running')) "StartType=$($winrmSvc.StartType) Status=$($winrmSvc.Status)"

$failed = @($checks | Where-Object { -not $_.Pass })
foreach ($check in $checks) {
    $mark = if ($check.Pass) { 'PASS' } else { 'FAIL' }
    Write-Output "$mark - $($check.Name) ($($check.Detail))"
}
Write-Output "---- $($checks.Count - $failed.Count)/$($checks.Count) checks passed ----"
if ($failed.Count -gt 0) { exit 1 }
"""
    result = run_ps(session, script)
    return result.status_code


def cmd_diagnose_reboot_race(args: argparse.Namespace) -> int:
    """Root-cause investigation for issue #188: correlate the exact
    winlogon.exe-initiated restart event (System log, Event ID 1074,
    reason "Operating System: Upgrade (Planned)") against CBS.log and
    Windows Setup's own Panther logs, to find what CBS/servicing action
    was in progress or had just completed immediately before the restart
    fired. This is the researcher's own recommended next step recorded in
    issue #191's closing comment, after finding that no in-guest
    scheduling mechanism (RunOnce from either the specialize or
    oobeSystem pass) survived this restart.

    Read-only: makes no guest changes. Safe to run against any guest that
    has already completed (or attempted) first-boot setup, regardless of
    whether that attempt ultimately succeeded -- the relevant logs persist
    across later reboots.
    """
    session = connect(args.host, args.port, args.user, args.password_file)
    script = r"""
$ErrorActionPreference = 'Continue'
Write-Output "==== System log: Event ID 1074 (controlled restart/shutdown requests) ===="
$events1074 = Get-WinEvent -FilterHashtable @{LogName='System'; Id=1074} -ErrorAction SilentlyContinue
if (-not $events1074) {
    Write-Output "(none found -- System log may have rolled over, or no controlled restart was ever logged)"
} else {
    foreach ($e in $events1074) {
        Write-Output "---- $($e.TimeCreated.ToString('o')) ----"
        Write-Output $e.Message
    }
}

Write-Output ""
Write-Output "==== System log: Event ID 41 (Kernel-Power, unexpected/dirty reboot) ===="
$events41 = Get-WinEvent -FilterHashtable @{LogName='System'; Id=41; ProviderName='Microsoft-Windows-Kernel-Power'} -ErrorAction SilentlyContinue
if (-not $events41) {
    Write-Output "(none found)"
} else {
    foreach ($e in $events41) {
        Write-Output "---- $($e.TimeCreated.ToString('o')) ----"
        Write-Output $e.Message
    }
}

Write-Output ""
Write-Output "==== Setup event log channel, last 40 (chronological) ===="
try {
    $setupEvents = Get-WinEvent -LogName 'Setup' -MaxEvents 40 -ErrorAction Stop | Sort-Object TimeCreated
    foreach ($e in $setupEvents) {
        $oneLine = ($e.Message -replace "\r?\n", ' ')
        Write-Output "$($e.TimeCreated.ToString('o')) [$($e.Id)] $($e.LevelDisplayName): $oneLine"
    }
} catch {
    Write-Output "(Setup log channel unavailable: $($_.Exception.Message))"
}

Write-Output ""
Write-Output "==== CBS.log excerpt around each 1074 restart (+/- 3 minutes) ===="
$cbsPath = "$env:windir\Logs\CBS\CBS.log"
if (-not (Test-Path -LiteralPath $cbsPath)) {
    Write-Output "(CBS.log not found at $cbsPath)"
} elseif (-not $events1074) {
    Write-Output "(no restart timestamps to correlate against)"
} else {
    # CBS.log lines look like: 2026-11-05 08:41:23, Info ...
    $cbsLines = Get-Content -LiteralPath $cbsPath
    foreach ($e in $events1074) {
        $windowStart = $e.TimeCreated.AddMinutes(-3)
        $windowEnd = $e.TimeCreated.AddMinutes(3)
        Write-Output "---- window around $($e.TimeCreated.ToString('o')) ----"
        $matched = 0
        foreach ($line in $cbsLines) {
            if ($line -match '^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),') {
                try {
                    $ts = [datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null)
                } catch {
                    continue
                }
                if ($ts -ge $windowStart -and $ts -le $windowEnd) {
                    Write-Output $line
                    $matched++
                }
            }
        }
        Write-Output "($matched matching CBS.log lines in this window)"
    }
}

Write-Output ""
Write-Output "==== Windows Setup Panther logs (last 40 lines each, if present) ===="
foreach ($p in @("$env:windir\Panther\UnattendGC\setupact.log", "$env:windir\Panther\setupact.log", "C:\vboxpostinstall.log")) {
    if (Test-Path -LiteralPath $p) {
        Write-Output "---- $p (last 40 lines) ----"
        Get-Content -LiteralPath $p -Tail 40
    } else {
        Write-Output "---- $p (not present) ----"
    }
}
"""
    result = run_ps(session, script)
    return result.status_code


def cmd_shutdown_guest(args: argparse.Namespace) -> int:
    """Issue a graceful OS-level shutdown over WinRM.

    VBoxManage's "acpipowerbutton" has proven unreliable against this guest
    (confirmed live 2026-11-02: WinRM stayed fully responsive, but the VM
    never actually reached the "poweroff" state within a 5-minute wait after
    sending the ACPI signal). WinRM is this pipeline's one consistently
    reliable automation channel, so prefer an explicit `shutdown.exe` call
    issued through it; callers should still poll VBoxManage state afterward
    and fall back to ACPI/hard poweroff if this also times out.
    """
    session = connect(args.host, args.port, args.user, args.password_file)
    # shutdown.exe tears down the WinRM connection itself, so a nonzero/absent
    # response here is expected and not a failure signal -- the caller polls
    # VBoxManage VM state separately to confirm the guest actually powered off.
    try:
        run_ps(session, "shutdown.exe /s /t 0 /f")
    except Exception as exc:  # noqa: BLE001 - connection teardown is expected
        print(f"shutdown request sent (connection dropped as expected: {exc})", file=sys.stderr)
    return 0


def _push_file(session: "winrm.Session", local_path: str, remote_path: str, chunk_size: int = 1500) -> bool:
    """Copy a local file into the guest over WinRM as chunked base64 text
    commands (no native file-copy channel is reliable for this VM -- see
    module docstring), verifying a SHA-256 round-trip before declaring
    success. Each chunk's PowerShell command is itself UTF-16LE+base64
    encoded again by pywinrm for WinRM transmission (`-EncodedCommand`),
    roughly tripling the effective size -- confirmed live 2026-11-02 that
    the original chunk_size=4000 pushed individual append commands over a
    practical command-line length limit ("The command line is too long"),
    even though the plain chunk text itself looked small. 1500 keeps each
    encoded command comfortably under that ceiling.
    """
    with open(local_path, "rb") as handle:
        data = handle.read()
    expected_sha256 = hashlib.sha256(data).hexdigest()
    b64 = base64.b64encode(data).decode("ascii")
    remote_dir = remote_path.rsplit("\\", 1)[0]
    remote_tmp = remote_path + ".b64.tmp"

    init_script = rf"""
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath '{remote_dir}')) {{ New-Item -ItemType Directory -Path '{remote_dir}' -Force | Out-Null }}
if (Test-Path -LiteralPath '{remote_tmp}') {{ Remove-Item -LiteralPath '{remote_tmp}' -Force }}
"""
    result = session.run_ps(init_script)
    if result.status_code != 0:
        sys.stderr.write(result.std_err.decode(errors="replace"))
        return False

    for offset in range(0, len(b64), chunk_size):
        chunk = b64[offset:offset + chunk_size]
        append_script = f"Add-Content -LiteralPath '{remote_tmp}' -Value '{chunk}' -NoNewline -Encoding Ascii"
        result = session.run_ps(append_script)
        if result.status_code != 0:
            sys.stderr.write(result.std_err.decode(errors="replace"))
            return False

    finalize_script = rf"""
$ErrorActionPreference = 'Stop'
$b64 = Get-Content -LiteralPath '{remote_tmp}' -Raw
$bytes = [Convert]::FromBase64String($b64)
[IO.File]::WriteAllBytes('{remote_path}', $bytes)
Remove-Item -LiteralPath '{remote_tmp}' -Force
$hash = (Get-FileHash -LiteralPath '{remote_path}' -Algorithm SHA256).Hash.ToLower()
if ($hash -ne '{expected_sha256}') {{ throw "SHA256 mismatch after transfer: expected {expected_sha256}, got $hash" }}
Write-Output "File transferred and verified: {remote_path}"
"""
    result = run_ps(session, finalize_script)
    return result.status_code == 0


def cmd_register_runner(args: argparse.Namespace) -> int:
    """Install prerequisites, authenticate gh, and run Register-WindowsRunner.ps1
    inside the guest -- fully over WinRM, no interactive VRDE session required.
    This automates the procedure proven live 2026-11-02 against a real VM
    and target repository (see README.md's "Functional proof" section for
    the specific evidence trail).

    Deliberately does NOT touch LabVIEW/VIPM activation -- a registered,
    online runner with an unlicensed LabVIEW/VIPM install is a safe, inert
    state; the human activation step remains the sole final gate before real
    build jobs should be routed to it.

    The registration PAT is read from a local file (never a CLI argument, to
    avoid host-side shell-history/process-list exposure) and embedded
    directly into the guest-side script string -- the same trust model this
    pipeline already applies to the guest admin password. `gh auth login` and
    Register-WindowsRunner.ps1 both run in this SAME WinRM session
    deliberately: an interactively-authenticated `gh` session and a separate
    WinRM session do not share Windows Credential Manager state (confirmed
    live -- see README's "Credential-persistence gotcha").
    """
    session = connect(args.host, args.port, args.user, args.password_file, read_timeout_sec=600, operation_timeout_sec=580)
    with open(args.pat_file, "r", encoding="utf-8") as handle:
        pat = handle.read().rstrip("\n")

    prereq_script = rf"""
$ErrorActionPreference = 'Stop'
# Invoke-WebRequest's default progress bar emits a CLIXML progress-stream
# record for every chunk written, which over WinRM remoting can collide
# with/be mistaken for the error stream -- pywinrm's error decoder then
# fails with "There was a problem converting the Powershell error message:
# not well-formed (invalid token)" and the real underlying error (if any)
# can be lost. Confirmed live 2026-11-05 during a register-runner failure
# whose root cause became unrecoverable from the captured logs for this
# reason. Suppressing the progress bar also makes the download itself
# dramatically faster (a well-known side effect of this same PowerShell
# gotcha).
$ProgressPreference = 'SilentlyContinue'
function Test-Cmd($name) {{ Get-Command $name -CommandType Application -ErrorAction SilentlyContinue }}
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (Test-Cmd 'gh')) {{
    winget install --id GitHub.cli --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Null
}}
if (-not (Test-Cmd 'git')) {{
    winget install --id Git.Git --source winget --silent --accept-package-agreements --accept-source-agreements | Out-Null
}}
if (-not (Test-Cmd 'pwsh')) {{
    $msiPath = "$env:TEMP\{args.pwsh_msi_filename}"
    Invoke-WebRequest -Uri '{args.pwsh_msi_url}' -OutFile $msiPath
    $actualHash = (Get-FileHash -LiteralPath $msiPath -Algorithm SHA256).Hash
    if ($actualHash -ne '{args.pwsh_msi_sha256}') {{ throw "PowerShell MSI SHA256 mismatch: expected {args.pwsh_msi_sha256}, got $actualHash" }}
    Start-Process msiexec.exe -ArgumentList "/i `"$msiPath`" /quiet /norestart ADD_PATH=1" -Wait
    Remove-Item -LiteralPath $msiPath -Force
}}
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (Test-Cmd 'pwsh')) {{ throw 'pwsh (PowerShell 7) still not found on PATH after installing the MSI.' }}
if (-not (Test-Cmd 'gh')) {{ throw 'gh CLI not found on PATH after installation attempt.' }}
if (-not (Test-Cmd 'git')) {{ throw 'git not found on PATH after installation attempt (required for gh auth login credential persistence).' }}
Write-Output 'Prerequisites verified: gh, git, pwsh all present.'
"""
    result = run_ps(session, prereq_script)
    if result.status_code != 0:
        return result.status_code

    runner_script = rf"""
$ErrorActionPreference = 'Stop'
# Same Invoke-WebRequest progress-stream/CLIXML gotcha as prereq_script
# above -- suppress it here too.
$ProgressPreference = 'SilentlyContinue'
$runnerRoot = '{args.runner_root}'
if (-not (Test-Path -LiteralPath (Join-Path $runnerRoot 'config.cmd'))) {{
    $zipPath = "$env:TEMP\{args.runner_filename}"
    Invoke-WebRequest -Uri '{args.runner_url}' -OutFile $zipPath
    $actualHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash
    if ($actualHash -ne '{args.runner_sha256}') {{ throw "actions-runner zip SHA256 mismatch: expected {args.runner_sha256}, got $actualHash" }}
    New-Item -ItemType Directory -Path $runnerRoot -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::ExtractToDirectory($zipPath, $runnerRoot)
    Remove-Item -LiteralPath $zipPath -Force
}}
Write-Output "actions-runner present at $runnerRoot."
"""
    result = run_ps(session, runner_script)
    if result.status_code != 0:
        return result.status_code

    if not _push_file(session, args.register_script, r"C:\vm-provisioning\Register-WindowsRunner.ps1"):
        return 1

    labels_ps_list = ",".join(f"'{label.strip()}'" for label in args.labels.split(",") if label.strip())
    register_script = rf"""
$ErrorActionPreference = 'Stop'
$token = '{pat}'
$token | gh auth login --with-token
Remove-Variable token
$who = gh api user --jq '.login'
Write-Output "gh authenticated as: $who"
& pwsh -NoProfile -File 'C:\vm-provisioning\Register-WindowsRunner.ps1' `
  -Repository '{args.repo}' `
  -RunnerName '{args.runner_name}' `
  -RunnerRoot '{args.runner_root}' `
  -Labels {labels_ps_list} `
  -DedicatedServiceAccountName '{args.service_account}' `
  -UseDedicatedServiceAccount `
  -AuthorizeActionsStatus -Run -Confirm:$false
if ($LASTEXITCODE -ne 0) {{ throw "Register-WindowsRunner.ps1 exited with code $LASTEXITCODE." }}
try {{ gh auth logout --hostname github.com --user $who 2>$null }} catch {{ }}
Write-Output 'Runner registration completed and PAT session logged out.'
"""
    result = run_ps(session, register_script)
    return result.status_code


def cmd_install_vipm(args: argparse.Namespace) -> int:
    session = connect(args.host, args.port, args.user, args.password_file)
    script = rf"""
$ErrorActionPreference = 'Stop'
# Don't assume a fixed drive letter for the attached installer ISO -- the
# leftover VBoxManage unattended-install "aux" ISO (carries the answer file/
# FirstLogonCommands) is often still attached after Setup, which shifts CD-ROM
# drive letter assignment by one relative to a VM that never had it attached
# or had it ejected (confirmed live 2026-11-02: VIPM ISO landed on a different
# letter than the hardcoded default on a freshly-built VM). Search all CD-ROM
# drives for the expected file instead.
$installerPath = Get-CimInstance Win32_CDROMDrive |
    ForEach-Object {{ Join-Path $_.Drive 'vipm-26.3.4056-windows-setup.exe' }} |
    Where-Object {{ Test-Path -LiteralPath $_ }} |
    Select-Object -First 1
if (-not $installerPath) {{
    $fallback = '{args.installer_drive}:\vipm-26.3.4056-windows-setup.exe'
    if (Test-Path -LiteralPath $fallback) {{ $installerPath = $fallback }}
    else {{ throw "VIPM installer not found on any CD-ROM drive (tried fallback $fallback)." }}
}}
Write-Output "VIPM installer found at $installerPath"
$signature = Get-AuthenticodeSignature -LiteralPath $installerPath
$signer = if ($signature.SignerCertificate) {{
    $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
}} else {{ '' }}
if ($signature.Status -ne 'Valid' -or $signer -ne 'James Kring, Inc.') {{
    throw "VIPM installer is not validly signed by James Kring, Inc. (status=$($signature.Status), signer=$signer)."
}}
Write-Output "Signature verified: $signer ($($signature.Status))"
$process = Start-Process -FilePath $installerPath -ArgumentList '/exenoui','/qn','/norestart' -Wait -PassThru
if ($process.ExitCode -ne 0) {{ throw "VIPM installer exited with code $($process.ExitCode)." }}
# Registry PATH changes made by the installer are not visible to this already-running
# session's cached $env:Path (classic Windows gotcha); recompute it before checking.
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
$vipm = Get-Command vipm -CommandType Application -ErrorAction SilentlyContinue
if (-not $vipm) {{ throw 'vipm.exe was not found on PATH after installation.' }}
& vipm --version
Write-Output 'VIPM install verified.'
"""
    result = run_ps(session, script)
    return result.status_code


def cmd_install_labview(args: argparse.Namespace) -> int:
    """Install LabVIEW Professional from the operator-supplied offline nipkg
    feed. The ISO ships a Debian-style repository (feeds/<product>/Packages +
    pool/*.nipkg), not a wizard-style setup.exe; nipkg.exe itself comes
    bundled with the VIPM install (verified 2026-11-02), so this does not
    need to bootstrap NI Package Manager separately. "Professional" vs other
    editions is a licensing distinction applied after this same install, not
    a different top-level package -- see provisioning-manifest.json.

    The embedded PowerShell script below is kept terse on purpose: it is sent
    to the guest as a base64 `-EncodedCommand` over WinRM, and a verbose
    version (with the full inline rationale previously here) tripped
    "The command line is too long" live 2026-11-02 -- the longer explanations
    for each step live only in this docstring/these Python comments now, not
    in the string actually transmitted.

    - Don't assume a fixed drive letter for the attached LabVIEW ISO: a
      leftover VBoxManage unattended-install aux ISO can shift CD-ROM drive
      letter assignment, so search all CD-ROM drives for the feed folder.
    - `feed-remove` on a not-yet-registered feed is the expected case on a
      first run; under `$ErrorActionPreference = 'Stop'`, nipkg's nonzero
      exit is wrapped as a terminating NativeCommandError regardless of
      `2>$null` redirection, so that expected failure must be caught
      explicitly.
    - Don't throw on a nonzero `nipkg install` exit code directly: confirmed
      live on two separate VMs that nipkg can report a nonzero exit (e.g.
      -125071) from the large `--include-recommended` package set even
      though the actual LabVIEW install underneath fully succeeded
      (LabVIEW.exe present, correct ProductVersion, and a direct idempotent
      re-run of the same install command afterward reporting "already
      installed" with exit code 0). The LabVIEW.exe verification below is
      the real authority; the nipkg exit code is surfaced only as context if
      that verification also fails.
    - NI's supporting packages (e.g.
      ni-assistant-framework-labview-2023/2024/2025-support) create empty
      "LabVIEW <year>" compatibility stub directories alongside the real
      install, and those sort alphabetically before "LabVIEW 2026" -- so
      picking the first "LabVIEW*" match is wrong. Only a directory that
      actually contains LabVIEW.exe is the real install.
    """
    # The full install resolves dozens of dependency packages and routinely
    # takes well over the pywinrm default 30s read timeout; proven live with
    # read_timeout_sec=3650 / operation_timeout_sec=3600 against a 35GB feed.
    session = connect(args.host, args.port, args.user, args.password_file, read_timeout_sec=3650, operation_timeout_sec=3600)
    script = rf"""
$ErrorActionPreference = 'Stop'
$nipkg = 'C:\Program Files\National Instruments\NI Package Manager\nipkg.exe'
if (-not (Test-Path -LiteralPath $nipkg)) {{ throw "nipkg.exe not found." }}
$feedDir = Get-CimInstance Win32_CDROMDrive |
    ForEach-Object {{ Join-Path $_.Drive 'feeds\ni-labview-2026' }} |
    Where-Object {{ Test-Path -LiteralPath $_ }} |
    Select-Object -First 1
if (-not $feedDir) {{
    $fallback = '{args.installer_drive}:\feeds\ni-labview-2026'
    if (Test-Path -LiteralPath $fallback) {{ $feedDir = $fallback }}
    else {{ throw "LabVIEW feed directory not found (tried fallback $fallback)." }}
}}
try {{ & $nipkg feed-remove {args.feed_name} 2>$null }} catch {{ }}
& $nipkg feed-add --system --name={args.feed_name} $feedDir
if ($LASTEXITCODE -ne 0) {{ throw "nipkg feed-add exited with code $LASTEXITCODE." }}
& $nipkg update {args.feed_name}
if ($LASTEXITCODE -ne 0) {{ throw "nipkg update exited with code $LASTEXITCODE." }}
& $nipkg install --accept-eulas --yes --include-recommended {args.package}
$nipkgInstallExitCode = $LASTEXITCODE
$env:Path = [System.Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path', 'User')
$labviewRoot = Get-ChildItem -LiteralPath 'C:\Program Files\National Instruments' -Directory -Filter 'LabVIEW*' -ErrorAction SilentlyContinue |
    Where-Object {{ Test-Path -LiteralPath (Join-Path $_.FullName 'LabVIEW.exe') }} |
    Select-Object -First 1
if (-not $labviewRoot) {{ throw "No LabVIEW.exe found after install (nipkg exit code $nipkgInstallExitCode)." }}
$labviewExe = Join-Path $labviewRoot.FullName 'LabVIEW.exe'
$productVersion = (Get-Item -LiteralPath $labviewExe).VersionInfo.ProductVersion
Write-Output "LabVIEW install verified: $($labviewRoot.FullName) (ProductVersion $productVersion, nipkg exit code $nipkgInstallExitCode)"
"""
    result = run_ps(session, script)
    return result.status_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15985)
    parser.add_argument("--user", default="actor")
    parser.add_argument("--password-file", required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("wait-ready").set_defaults(func=cmd_wait_ready)
    subparsers.add_parser("verify-winrm-setup").set_defaults(func=cmd_verify_winrm_setup)
    subparsers.add_parser("diagnose-reboot-race").set_defaults(func=cmd_diagnose_reboot_race)
    subparsers.add_parser("shutdown-guest").set_defaults(func=cmd_shutdown_guest)

    vipm_parser = subparsers.add_parser("install-vipm")
    vipm_parser.add_argument("--installer-drive", default="F", help="Drive letter the VIPM installer ISO mounts as.")
    vipm_parser.set_defaults(func=cmd_install_vipm)

    labview_parser = subparsers.add_parser("install-labview")
    labview_parser.add_argument("--installer-drive", default="E", help="Drive letter the LabVIEW installer ISO mounts as.")
    labview_parser.add_argument("--feed-name", default="windows-labview2026-feed", help="nipkg feed name to register for the offline LabVIEW feed.")
    labview_parser.add_argument("--package", default="ni-labview-2026-core-en", help="Top-level nipkg meta-package to install (provides ni-labview-2026).")
    labview_parser.set_defaults(func=cmd_install_labview)

    runner_parser = subparsers.add_parser("register-runner")
    runner_parser.add_argument("--repo", required=True, help="Target repository to register the runner against, e.g. my-org/my-repo.")
    runner_parser.add_argument("--runner-name", required=True, help="Distinct runner identity name.")
    runner_parser.add_argument("--labels", required=True, help="Comma-separated custom labels, e.g. windows-labview2026-testcoord.")
    runner_parser.add_argument("--service-account", required=True, help="Dedicated local service account name Register-WindowsRunner.ps1 creates/uses to run the runner service.")
    runner_parser.add_argument("--pat-file", required=True, help="Local path to a file containing a fine-grained PAT (Administration: Read and write, Actions: Read on --repo). Never pass the token value directly.")
    runner_parser.add_argument("--register-script", required=True, help="Local path to Register-WindowsRunner.ps1 to transfer into the guest.")
    runner_parser.add_argument("--runner-root", default=r"C:\actions-runner", help="Guest directory to install the GitHub Actions runner into.")
    runner_parser.add_argument("--runner-url", required=True, help="Download URL for the actions-runner-win-x64 zip (see provisioning-manifest.json).")
    runner_parser.add_argument("--runner-filename", required=True, help="Filename to save the downloaded runner zip as (see provisioning-manifest.json).")
    runner_parser.add_argument("--runner-sha256", required=True, help="Expected SHA-256 of the runner zip (see provisioning-manifest.json).")
    runner_parser.add_argument("--pwsh-msi-url", required=True, help="Download URL for the PowerShell 7 win-x64 MSI, installed only if pwsh isn't already present (see provisioning-manifest.json).")
    runner_parser.add_argument("--pwsh-msi-filename", required=True, help="Filename to save the downloaded PowerShell MSI as (see provisioning-manifest.json).")
    runner_parser.add_argument("--pwsh-msi-sha256", required=True, help="Expected SHA-256 of the PowerShell MSI (see provisioning-manifest.json).")
    runner_parser.set_defaults(func=cmd_register_runner)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
