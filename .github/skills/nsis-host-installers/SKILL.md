---
name: nsis-host-installers
description: 'Build or debug NSIS-based Windows host/guest bootstrap installers for VM provisioning (makensis, winrm-setup-installer.nsi, host-bootstrap-installer.nsi). Use when writing a new .nsi script, compiling with makensis, when a compiled installer exits 0 with no log/no error under a non-interactive runner account, or when parameterizing OutFile/InstallDir from a PowerShell build wrapper.'
---

# NSIS Host/Guest Bootstrap Installers

## When to use
- Writing or modifying a `.nsi` script for host-side (Windows runner) or
  guest-side (Windows VM Setup) bootstrap automation in this project.
- Debugging an NSIS-compiled installer that exits 0 but produces no log
  output, especially when invoked by a non-interactive service account
  (GitHub Actions self-hosted runner).
- Parameterizing an installer's output path/payload/install directory from
  a PowerShell build wrapper without hand-editing the `.nsi` source each
  time.

## Established patterns in this repo

1. **Structural-determinism logging** ([winrm-setup-installer.nsi](../../../vm/windows-labview/winrm-setup-installer.nsi),
   [host-bootstrap-installer.nsi](../../../vm/windows-labview/host-bootstrap-installer.nsi)):
   every check/install step uses a `RunStep` macro that writes its own
   command, exit code, and captured stdout/stderr to a persistent log file
   via `nsExec::ExecToStack` + `FileWrite`. A failure deep inside one step
   is fully diagnosable from the log alone -- no live session on the
   host/guest needed. Use `FileWrite` directly (not a `LogLine` macro) for
   lines built from macro parameters: NSIS's `!insertmacro` call-site
   parsing splits on whitespace, so re-substituting an already-quoted
   multi-word parameter into another macro call breaks it apart into bogus
   parameters; `FileWrite`'s own parsing takes the remainder of the line as
   one string instead.
2. **Parameterized OutFile/PayloadDir/InstallDir via `!ifndef`-guarded
   defines** (reference: [svelderrainruiz/labview-icon-editor-codex-skills `nsis/skill-layer-installer.nsi`](https://github.com/svelderrainruiz/labview-icon-editor-codex-skills/blob/main/nsis/skill-layer-installer.nsi)):

   ```nsis
   !ifndef OUT_FILE
     !define OUT_FILE "my-installer.exe"
   !endif
   !ifndef INSTALL_ROOT
     !define INSTALL_ROOT "C:\Users\Public\my-app\current"
   !endif
   OutFile "${OUT_FILE}"
   InstallDir "${INSTALL_ROOT}"
   ```

   A build wrapper (see that repo's `scripts/Build-SkillLayerInstaller.ps1`)
   then compiles with `makensis /DOUT_FILE=<path> /DINSTALL_ROOT=<path>
   script.nsi`. **This repo's current scripts hardcode `OutFile` instead**
   (e.g. `winrm-setup-installer.nsi`'s `OutFile "winrm-setup-installer.exe"`)
   and work around it by copying the `.nsi` source into a fresh temp
   directory before compiling (see `build-nsis-bootstrap-iso.ps1`) -- prefer
   the `!ifndef`-guarded-define pattern for any *new* installer to avoid
   that workaround entirely.
3. **Minimal non-admin installers**: when a script truly does not need
   elevation (e.g. just extracting a payload to a per-user directory), use
   `RequestExecutionLevel user`, not `admin` -- see the first gotcha below
   for why `admin` is dangerous under a non-interactive account.

## Critical gotchas (all confirmed live, 2026-11-05)

- **`RequestExecutionLevel admin` fails silently with no log, no error,
  exit 0 when run under a non-interactive service/runner account** (no
  interactive desktop session exists to show the UAC consent prompt). None
  of NSIS's `FileOpen`/`FileWrite`/`CreateDirectory` instructions check
  their own success by default, so the failure cascades silently. Fix:
  don't request elevation unless genuinely required; if a registry write
  needs elevation, try Machine scope and fall back to User scope on
  failure instead of requesting process-wide elevation.
- **PowerShell's `&` call operator does not reliably wait for/capture the
  exit code of an NSIS-compiled (GUI-subsystem) executable.** Always
  invoke via `Start-Process -FilePath <installer.exe> -ArgumentList '/S'
  -Wait -PassThru` and check `.ExitCode`, not `&`/`$LASTEXITCODE`.
- **`makensis -XOutFile <path>` does NOT override a script's own hardcoded
  `OutFile` directive** -- the script's own directive runs after the `-X`
  injection and wins. Either use the `!ifndef`-guarded define pattern
  above, or copy the `.nsi` source to a fresh temp directory, compile with
  `makensis -V2 <copy>`, then locate the output by its hardcoded filename
  in that same directory.
- **`VBoxManage extpack install --accept-license=<sha256>`** needs the
  exact hash of whichever license text VBoxManage internally selects;
  externally extracting+hashing the license text (e.g. via `tar`) can
  produce a locale/encoding mismatch that doesn't match, causing an
  interactive re-prompt that aborts on empty stdin. Simpler, robust fix:
  pipe a plain `"y"` to the interactive prompt directly
  (`echo y | VBoxManage extpack install ...`).

## References

- [winrm-setup-installer.nsi](../../../vm/windows-labview/winrm-setup-installer.nsi) --
  proven RunStep-macro + persistent-log pattern (guest-side WinRM
  enablement). Header comment documents it does NOT solve issue #188's
  reboot race.
- [host-bootstrap-installer.nsi](../../../vm/windows-labview/host-bootstrap-installer.nsi) --
  same pattern applied to host-side prerequisite bootstrap
  (chocolatey/Python/NSIS/VirtualBox), with explicit "GAP
  (operator-gated)" reporting instead of auto-fixing risky items
  (hypervisor install, VNC extension pack).
- External reference repo:
  [svelderrainruiz/labview-icon-editor-codex-skills](https://github.com/svelderrainruiz/labview-icon-editor-codex-skills) --
  a sibling project's NSIS "skill layer" packaging convention
  (`nsis/skill-layer-installer.nsi` + `scripts/Build-SkillLayerInstaller.ps1`),
  the source of the `!ifndef`-guarded-define pattern above.
