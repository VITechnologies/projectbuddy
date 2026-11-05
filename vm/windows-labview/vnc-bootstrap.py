#!/usr/bin/env python3
"""VNC-bootstrap: launch the NSIS WinRM-setup installer on a guest where
WinRM isn't configured yet, without depending on `FirstLogonCommands`
surviving the #188 restart (issue #201, epic #198).

Governing principle (epic #198): NSIS shall be leveraged for injection
points. This script's job is strictly to *trigger* the already-built NSIS
installer (vm/windows-labview/winrm-setup-installer.nsi, delivered via the
auxiliary ISO built by build-nsis-bootstrap-iso.sh/issue #200) -- open a
shell, elevate it, launch one .exe. It deliberately contains NO guest
configuration logic of its own (no reg.exe/Set-Service/New-NetFirewallRule
calls): every actual configuration step belongs inside the .nsi, with its
own nsExec-captured logging, not here.

The exact sequence below (wake the display, Win+R -> cmd -> Ctrl+Shift+Enter
-> UAC Yes -> run the installer from the auxiliary drive) was live-validated
manually against a real post-restart VM on 2026-11-03 -- see
https://github.com/VITechnologies/SandboxSBOMReporting/issues/199#issuecomment-6602003786
for the full evidence trail this script automates.

Requires `vncdotool` (not a tracked project dependency -- set up a venv):
    python3 -m venv .venv && .venv/bin/pip install -r vnc-bootstrap-requirements.txt
and the `VBoxManage`/`vnc-console.sh` tooling already used throughout this
VM pipeline. Run this script with that venv's `bin/` on PATH (it shells out
to the `vncdotool` CLI, not its Python library API), e.g.:
    PATH=".venv/bin:$PATH" python3 vnc-bootstrap.py --vm-name ... --winrm-host-port ...

Idempotent / safe to re-run: every phase here only polls, opens
applications, or (re-)launches the installer -- re-running after a partial
failure (e.g. a dropped VNC connection) is always safe. The installer
itself (winrm-setup-installer.nsi) is independently idempotent (stops/
disables services, sets registry values, re-enables remoting) regardless
of how many times it has already run.

Optional, strictly additive: --enable-service-bootstrap launches a second,
Windows-Service-based WinRM bootstrap installer (winrm-service-bootstrap-
installer.nsi) via this exact same proven mechanism, AFTER the primary
installer above has already made WinRM reachable. This is a research
prototype (issue #191) re-approached here, from a specialize-pass
RunAsynchronousCommand injection point, after issue #290 found that
injection point deterministically corrupted Windows Setup's resume state
before OOBE. Triggering it from here instead -- the same safe, proven
post-OOBE/post-#188-restart point as the primary installer -- sidesteps
that failure mode entirely. Best-effort: any failure in this supplemental
step is logged but never fails the overall bootstrap.

Exit codes: 0 on confirmed success (WinRM reachable afterward), nonzero on
any failure, with a clear stderr message identifying which phase failed.
"""
import argparse
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def log(message: str) -> None:
    print(f"[vnc-bootstrap] {message}", file=sys.stderr, flush=True)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise RuntimeError(f"{name} is required but not found on PATH")


def get_vbox_log_path(vm_name: str) -> Path:
    """Derive Logs/VBox.log from the VM's own .vbox settings file location,
    rather than assuming a BASEFOLDER convention -- works regardless of how
    the VM was created."""
    out = subprocess.run(
        ["VBoxManage", "showvminfo", vm_name, "--machinereadable"],
        capture_output=True, text=True, check=True,
    ).stdout
    match = re.search(r'^CfgFile="(.*)"$', out, re.MULTILINE)
    if not match:
        raise RuntimeError(f"could not determine CfgFile for VM {vm_name!r}")
    return Path(match.group(1)).parent / "Logs" / "VBox.log"


def get_vm_state(vm_name: str) -> str:
    out = subprocess.run(
        ["VBoxManage", "showvminfo", vm_name, "--machinereadable"],
        capture_output=True, text=True, check=True,
    ).stdout
    match = re.search(r'^VMState="(.*)"$', out, re.MULTILINE)
    return match.group(1) if match else "unknown"


def wait_for_marker_count(log_path: Path, marker: str, min_count: int, timeout: float, poll_interval: float = 10.0) -> bool:
    """Block until `marker` appears at least `min_count` times in `log_path`,
    or `timeout` seconds elapse. Returns whether the count was reached."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log_path.is_file():
            count = log_path.read_text(errors="replace").count(marker)
            if count >= min_count:
                return True
        time.sleep(poll_interval)
    return False


def wait_for_guest_stability(vm_name: str, oobe_timeout: float, restart_timeout: float, settle_seconds: float = 90.0) -> None:
    log_path = get_vbox_log_path(vm_name)
    log(f"waiting up to {oobe_timeout:.0f}s for first OOBE/auto-logon (VBoxTray.exe) in {log_path}")
    if not wait_for_marker_count(log_path, "VBoxTray.exe", 1, oobe_timeout):
        raise RuntimeError(f"guest never reached OOBE/auto-logon within {oobe_timeout:.0f}s")
    log("first OOBE/auto-logon confirmed")

    # NOTE: VBoxTray.exe's guest-log line only fires once per VM power-on
    # session, NOT again after an in-OS restart (confirmed live, 2026-11-03
    # -- waiting for a second occurrence here hangs for the full timeout
    # every time, even though the guest recovers from the #188 restart in
    # well under a minute). Watch for the restart event itself instead
    # (AHCI#0: Port 0 reset -- the same marker used manually throughout
    # #188's investigation), then add a short fixed settle buffer for the
    # guest to finish rebooting and auto-logging back in.
    log(f"waiting up to {restart_timeout:.0f}s for the #188 restart to recur (AHCI#0: Port 0 reset)")
    if wait_for_marker_count(log_path, "AHCI#0: Port 0 reset", 1, restart_timeout):
        log(f"restart event confirmed -- waiting {settle_seconds:.0f}s for the guest to finish recovering")
        time.sleep(settle_seconds)
    else:
        log("WARNING: no second OOBE/auto-logon seen within the timeout -- proceeding anyway "
            "(this guest may not have hit the #188 restart, or it simply needs more time)")


def open_vnc(vm_name: str, script_dir: Path, vnc_port: "int | None", inventory: "Path | None" = None) -> tuple[str, int, str]:
    cmd = [str(script_dir / "vnc-console.sh"), "open", vm_name, "--no-launch"]
    if vnc_port is not None:
        cmd += ["--port", str(vnc_port)]
    if inventory is not None:
        cmd += ["--inventory", str(inventory)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    output = result.stderr
    address_match = re.search(r"^Address:\s+(\S+)", output, re.MULTILINE)
    password_match = re.search(r"^Password:\s+(\S+)", output, re.MULTILINE)
    if not address_match or not password_match:
        raise RuntimeError(f"could not parse vnc-console.sh open output:\n{output}")
    host, port_str = address_match.group(1).rsplit(":", 1)
    return host, int(port_str), password_match.group(1)


def close_vnc(vm_name: str, script_dir: Path, inventory: "Path | None" = None) -> None:
    cmd = [str(script_dir / "vnc-console.sh"), "close", vm_name]
    if inventory is not None:
        cmd += ["--inventory", str(inventory)]
    subprocess.run(cmd, capture_output=True, text=True)


def open_vnc_with_abort_recovery(
    vm_name: str, script_dir: Path, vnc_port: "int | None", inventory: "Path | None", recovery_settle_seconds: float = 120.0
) -> tuple[str, int, str]:
    """Wrap open_vnc() with one bounded recovery attempt for a known,
    confirmed-live (2026-11-04, issue #226) intermittent VirtualBox VRDE/
    libvncserver segfault: enabling VRDE can abort the whole VM process
    outright (VMState=aborted), not just fail the VNC session. If that
    happens, restart the VM and retry once -- the guest OS itself was
    otherwise healthy (this isn't a guest-side failure), so a fresh
    power-on plus a fixed settle buffer is enough to reach a connectable
    desktop again, without re-running the full OOBE/restart wait."""
    try:
        return open_vnc(vm_name, script_dir, vnc_port, inventory)
    except subprocess.CalledProcessError as exc:
        state = get_vm_state(vm_name)
        if state != "aborted":
            raise RuntimeError(
                f"vnc-console.sh open failed (rc={exc.returncode}): {exc.stderr.strip() if exc.stderr else exc}"
            ) from exc
        log(f"WARNING: {vm_name} aborted while opening the VNC console (known issue #226) -- "
            f"restarting and retrying once after a {recovery_settle_seconds:.0f}s settle buffer.")
        subprocess.run(["VBoxManage", "startvm", vm_name, "--type", "headless"], capture_output=True, text=True, check=True)
        time.sleep(recovery_settle_seconds)
        return open_vnc(vm_name, script_dir, vnc_port, inventory)


def vncdo(host: str, port: int, password: str, *args: str, timeout: float = 30.0) -> str:
    cmd = ["vncdotool", "--force-caps", "-s", f"{host}::{port}", "-p", password, *args]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"vncdotool {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}")
    return result.stdout


def wake_and_verify_desktop(host: str, port: int, password: str, timeout: float = 300.0, poll_interval: float = 15.0) -> None:
    """The guest display goes to sleep/blank after sitting idle (confirmed
    live: both vncdotool and VBoxManage screenshotpng return a near-empty
    black capture in that state -- not a capture bug). Send a keypress and
    confirm a real (non-blank) screen before trusting any further input.

    This needs to be patient, not just retry quickly: under concurrent host
    load from other running VMs, the guest can still be genuinely mid-boot
    (POST/Windows loading/auto-logon) well past the fixed settle buffer,
    not merely "asleep". Confirmed live 2026-11-03: a healthy, uncontended
    run woke on the first attempt with a ~590KB capture, while a run
    contending with a second concurrently-running VM returned an identical
    997-byte capture on every one of 4 quick attempts (~20s total budget)
    across two independent full VM rebuilds -- i.e. a deterministic "still
    booting", not transient noise that a few quick retries would catch.
    Poll on a bounded time budget instead of a small fixed attempt count."""
    import tempfile
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        vncdo(host, port, password, "key", "esc")
        time.sleep(2)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            shot_path = handle.name
        vncdo(host, port, password, "capture", shot_path)
        size = Path(shot_path).stat().st_size
        Path(shot_path).unlink(missing_ok=True)
        # A blank/asleep desktop captures to ~1KB; a real Windows 11 desktop
        # or dialog is reliably tens to hundreds of KB (confirmed live).
        if size > 20_000:
            log(f"desktop confirmed reachable (capture size {size} bytes, attempt {attempt})")
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"could not confirm the guest desktop is reachable after {attempt} attempts over {timeout:.0f}s"
            )
        log(f"capture too small ({size} bytes) on attempt {attempt}; guest display may still be asleep/booting, retrying")
        time.sleep(poll_interval)


def launch_installer(host: str, port: int, password: str, volume_label: str, installer_name: str) -> None:
    # Win+R -> cmd -> Ctrl+Shift+Enter launches an elevated cmd.exe, which
    # produces a standard UAC consent dialog (confirmed live: the
    # auto-logon'd admin account is NOT pre-elevated). "Yes" is the
    # right-hand button; Left+Enter selects it without needing to guess
    # pixel coordinates.
    vncdo(host, port, password, "key", "super-r")
    time.sleep(1)
    vncdo(host, port, password, "type", "cmd")
    time.sleep(1)
    vncdo(host, port, password, "key", "ctrl-shift-enter")
    time.sleep(2)
    vncdo(host, port, password, "key", "left")
    time.sleep(1)
    vncdo(host, port, password, "key", "enter")
    time.sleep(2)
    # Click inside the (now elevated) console window first -- confirmed
    # live that a freshly-opened console can fail to receive the first
    # batch of typed keystrokes until it's explicitly focused.
    vncdo(host, port, password, "mousemove", "400", "300", "click", "1")
    time.sleep(1)

    # A single PowerShell invocation that finds the auxiliary drive by
    # volume label (never assume a fixed letter -- confirmed live that
    # Windows does not assign it deterministically) and launches the
    # installer with -Wait, all in one elevated-shell round trip. This
    # inherits the shell's elevated token, so the installer itself needs no
    # further elevation handling -- and it minimizes the number of
    # separate VNC round trips, each of which is a chance for a dropped
    # keystroke or a focus race (confirmed repeatedly during manual testing).
    find_and_run = (
        f"powershell -Command \""
        f"$d = (Get-Volume | Where-Object FileSystemLabel -eq '{volume_label}').DriveLetter; "
        f"if (-not $d) {{ Write-Output 'AUX DRIVE NOT FOUND'; exit 1 }}; "
        f"Start-Process -FilePath ($d + ':\\{installer_name}') -Wait"
        f"\""
    )
    vncdo(host, port, password, "type", find_and_run)
    time.sleep(1)
    vncdo(host, port, password, "key", "enter")
    log("installer launch command sent; not waiting for the shell to visibly return -- "
        "WinRM reachability (polled next) is the real completion signal")


def poll_winrm_ready(winrm_host: str, winrm_port: int, timeout: float, poll_interval: float = 10.0) -> bool:
    """A configured WS-Man listener returns a real HTTP response (e.g. 405
    Method Not Allowed for a bare GET) even unauthenticated; an
    unconfigured one just hangs/times out with zero bytes -- confirmed
    live. This needs no guest credentials and works whether or not the
    installer's own shell has visibly finished."""
    url = f"http://{winrm_host}:{winrm_port}/wsman"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5)
            return True
        except urllib.error.HTTPError:
            # Any HTTP-level response (even an error status) means a real
            # listener answered.
            return True
        except (urllib.error.URLError, socket.timeout, ConnectionError, TimeoutError):
            pass
        time.sleep(poll_interval)
    return False


def _launch_service_bootstrap_best_effort(
    vm_name: str, script_dir: Path, vnc_port: "int | None", inventory: "Path | None",
    volume_label: str, installer_name: str,
) -> None:
    """Opens a fresh VNC session (the primary one was already closed after
    the NSIS installer launch) and launches the Windows-Service bootstrap
    installer via the exact same proven launch_installer() mechanism. This
    runs AFTER poll_winrm_ready() already confirmed WinRM is reachable, so
    this step is purely supplemental (issue #290) -- any failure here is
    logged and swallowed, never propagated, so a problem in this optional
    robustness layer can never regress the primary, required WinRM
    bootstrap this function is called after."""
    try:
        host, port, password = open_vnc_with_abort_recovery(vm_name, script_dir, vnc_port, inventory)
        log(f"launching supplemental Windows-Service WinRM bootstrap (best-effort, issue #290): VNC console open at {host}:{port}")
        try:
            launch_installer(host, port, password, volume_label, installer_name)
        finally:
            close_vnc(vm_name, script_dir, inventory)
    except Exception as exc:  # noqa: BLE001 -- best-effort supplemental step, must never fail the primary bootstrap
        log(f"WARNING: supplemental Windows-Service bootstrap failed (non-fatal, issue #290): {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vm-name", required=True)
    parser.add_argument("--winrm-host", default="127.0.0.1")
    parser.add_argument("--winrm-host-port", type=int, required=True, help="Host-side NAT port forwarded to the guest's WinRM port (see build-virtualbox.sh's WINRM_HOST_PORT).")
    parser.add_argument("--vnc-port", type=int, default=None, help="Explicit VNC port; default lets vnc-console.sh derive one.")
    parser.add_argument("--inventory", type=Path, default=None, help="Explicit runner-inventory.json path to pass through to vnc-console.sh -- needed when this script is invoked from a location (e.g. a consumed pinned release) other than where the real inventory file lives; default lets vnc-console.sh use its own next-to-itself default.")
    parser.add_argument("--volume-label", default="SBOMNSISAUX", help="Volume label of the auxiliary ISO built by build-nsis-bootstrap-iso.sh.")
    parser.add_argument("--installer-name", default="winrm-setup-installer.exe")
    parser.add_argument("--oobe-timeout", type=float, default=1800.0, help="Seconds to wait for first OOBE/auto-logon.")
    parser.add_argument("--restart-timeout", type=float, default=1800.0, help="Seconds to wait for the #188 restart to recur (non-fatal if it doesn't).")
    parser.add_argument("--settle-seconds", type=float, default=90.0, help="Fixed buffer after the restart event to let the guest finish recovering before connecting.")
    parser.add_argument("--wake-timeout", type=float, default=300.0, help="Seconds to keep retrying the desktop-wake capture check before giving up (guest may still be booting under host load, not just asleep).")
    parser.add_argument("--wake-poll-interval", type=float, default=15.0, help="Seconds between desktop-wake capture attempts.")
    parser.add_argument("--winrm-timeout", type=float, default=300.0, help="Seconds to wait for WinRM to become reachable after launching the installer.")
    parser.add_argument("--enable-service-bootstrap", action="store_true", help="Research prototype (issue #191, re-approached in #290 after the specialize-pass injection point was found to deterministically corrupt Windows Setup's resume state before OOBE). After the proven NSIS installer above has already made WinRM reachable, ALSO launch the Windows-Service-based WinRM bootstrap installer via this exact same post-OOBE VNC mechanism, as a supplemental self-healing layer for any LATER restart during the rest of the pipeline (e.g. a VIPM/LabVIEW install triggering its own reboot). Best-effort: a failure here is logged but never fails the overall bootstrap, since WinRM is already confirmed working by this point.")
    parser.add_argument("--service-volume-label", default="SBOMSVCAUX", help="Volume label of the auxiliary ISO built by build-winrm-service-bootstrap-iso.sh.")
    parser.add_argument("--service-installer-name", default="winrm-service-bootstrap-installer.exe")
    args = parser.parse_args()

    require_tool("VBoxManage")
    require_tool("vncdotool")
    script_dir = Path(__file__).resolve().parent
    if not (script_dir / "vnc-console.sh").is_file():
        print(f"vnc-console.sh not found next to this script: {script_dir}", file=sys.stderr)
        return 2

    try:
        wait_for_guest_stability(args.vm_name, args.oobe_timeout, args.restart_timeout, args.settle_seconds)

        host, port, password = open_vnc_with_abort_recovery(args.vm_name, script_dir, args.vnc_port, args.inventory)
        log(f"VNC console open at {host}:{port}")
        try:
            wake_and_verify_desktop(host, port, password, args.wake_timeout, args.wake_poll_interval)
            launch_installer(host, port, password, args.volume_label, args.installer_name)
        finally:
            close_vnc(args.vm_name, script_dir, args.inventory)

        log(f"polling WinRM at {args.winrm_host}:{args.winrm_host_port} for up to {args.winrm_timeout:.0f}s")
        if poll_winrm_ready(args.winrm_host, args.winrm_host_port, args.winrm_timeout):
            log("SUCCESS: WinRM is reachable.")
            if args.enable_service_bootstrap:
                _launch_service_bootstrap_best_effort(
                    args.vm_name, script_dir, args.vnc_port, args.inventory,
                    args.service_volume_label, args.service_installer_name,
                )
            return 0
        print("FAILED: WinRM did not become reachable within the timeout after launching the installer.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 -- top-level CLI boundary, want a clean message not a traceback
        if isinstance(exc, subprocess.CalledProcessError):
            # str(exc) alone is just "Command '...' returned non-zero exit
            # status N." -- the actually-useful diagnostic is in
            # stderr/stdout, which would otherwise be silently lost here
            # (issue #226).
            detail = (exc.stderr or exc.stdout or "").strip()
            print(f"FAILED: {exc}" + (f"\n{detail}" if detail else ""), file=sys.stderr)
        else:
            print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
