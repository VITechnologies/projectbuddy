; Research prototype for issue #191's own recommended next step: a genuine
; Windows Service (SCM-managed, Automatic startup), as an alternative to
; FirstLogonCommands/RunOnce-driven WinRM setup -- neither of which
; survived the #188 restart (see #191's closing comment: both the
; specialize-pass and oobeSystem-pass variants failed, the former with an
; even earlier Setup-crash because writing to RunOnce during the
; specialize pass collides with Windows Setup's own use of that same key
; for its own specialize-to-oobeSystem continuation).
;
; A Windows Service is registered under
; HKLM\SYSTEM\CurrentControlSet\Services\ -- the SAME registry area the
; already-proven-safe wuauserv/UsoSvc StartupType=Disabled writes target
; during the specialize pass (see winrm-setup-installer.nsi's own first
; step) -- NOT the RunOnce key Setup's own bookkeeping collides with. The
; Service Control Manager starts an Automatic-startup service fresh on
; literally every subsequent boot, independent of any single
; logon-sequencing event, which RunOnce/FirstLogonCommands structurally
; cannot do once interrupted.
;
; NSIS itself cannot produce a native service-dispatcher executable (no
; StartServiceCtrlDispatcher/control-handler implementation), so this
; packages NSSM (nssm.cc, public domain -- see provisioning-manifest.json
; for the pinned build/hash) as the service binary, configured to wrap
; powershell.exe running winrm-setup-service.ps1 (the actual idempotent
; WinRM-enabling logic, kept in sync with winrm-setup-installer.nsi's six
; steps). This installer's own job is intentionally minimal and fast
; (extract two packaged files, run a handful of `nssm.exe` registration
; calls) to keep the specialize-pass injection point as small a risk
; window as reasonably possible, mirroring the existing proven pattern.
;
; Uses the same RunStep-macro/persistent-log architecture as
; winrm-setup-installer.nsi and host-bootstrap-installer.nsi (see either
; file's own header comment for why FileWrite, not a LogLine macro, is
; used for parameterized lines).
;
; Compiled by build-winrm-service-bootstrap-installer-iso.ps1/.sh (not yet
; written as of this prototype's first commit -- see that script's own
; header once added), which downloads+verifies NSSM per
; provisioning-manifest.json and stages both packaged files into this
; script's own directory before invoking makensis, the same
; copy-to-temp-then-compile pattern used by build-nsis-bootstrap-iso.ps1.

!include LogicLib.nsh

Name "WinRM Service Bootstrap"
OutFile "winrm-service-bootstrap-installer.exe"
SilentInstall silent
RequestExecutionLevel admin
ShowInstDetails nevershow

!ifndef NSSM_EXE_SOURCE
  !define NSSM_EXE_SOURCE "nssm.exe"
!endif
!ifndef WINRM_SETUP_SERVICE_SOURCE
  !define WINRM_SETUP_SERVICE_SOURCE "winrm-setup-service.ps1"
!endif

Var LogHandle
Var StepOutput
Var StepRC

!macro LogLine text
  FileWrite $LogHandle "${text}$\r$\n"
!macroend

!macro RunStep description command
  FileWrite $LogHandle "---- ${description} ----$\r$\n"
  nsExec::ExecToStack '${command}'
  Pop $StepRC
  Pop $StepOutput
  FileWrite $LogHandle "exit code: $StepRC$\r$\n"
  FileWrite $LogHandle "output:$\r$\n$StepOutput$\r$\n"
  ${If} $StepRC != 0
    FileWrite $LogHandle "RESULT: FAILED$\r$\n"
  ${Else}
    FileWrite $LogHandle "RESULT: OK$\r$\n"
  ${EndIf}
!macroend

Section "WinRM Service Bootstrap" SEC_MAIN
  CreateDirectory "C:\vm-provisioning"
  FileOpen $LogHandle "C:\vm-provisioning\winrm-service-bootstrap-install.log" w
  !insertmacro LogLine "WinRM service bootstrap installer starting"

  SetOutPath "C:\vm-provisioning"
  File "/oname=nssm.exe" "${NSSM_EXE_SOURCE}"
  File "/oname=winrm-setup-service.ps1" "${WINRM_SETUP_SERVICE_SOURCE}"
  !insertmacro LogLine "Extracted nssm.exe and winrm-setup-service.ps1 to C:\vm-provisioning"

  ; Remove any stale prior registration first (idempotent re-run safety --
  ; e.g. a retried create-vm attempt against a VM that got this far before
  ; failing a later phase).
  !insertmacro RunStep "Remove any pre-existing SandboxSbomWinRmSetup service" \
    'C:\vm-provisioning\nssm.exe remove SandboxSbomWinRmSetup confirm'

  !insertmacro RunStep "Install SandboxSbomWinRmSetup service" \
    'C:\vm-provisioning\nssm.exe install SandboxSbomWinRmSetup C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'

  !insertmacro RunStep "Configure service arguments" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup AppParameters "-NoProfile -ExecutionPolicy Bypass -File C:\vm-provisioning\winrm-setup-service.ps1"'

  !insertmacro RunStep "Configure service startup directory" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup AppDirectory C:\vm-provisioning'

  !insertmacro RunStep "Configure service account (LocalSystem)" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup ObjectName LocalSystem'

  ; Without this, the wrapped powershell.exe (a console application) has no
  ; stdout/stderr handle to write to under NSSM -- confirmed live: the
  ; service hung indefinitely partway through Enable-PSRemoting (which,
  ; among this script's six steps, produces the most console output) even
  ; though the exact same cmdlet completed in ~5 seconds when run directly
  ; as SYSTEM via a one-off scheduled task (ruling out "SYSTEM account"
  ; itself as the cause). This is a well-documented NSSM gotcha: redirect
  ; both streams to files so the console pipe never fills and blocks the
  ; wrapped process.
  !insertmacro RunStep "Configure stdout/stderr redirection (avoid console-pipe hang)" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup AppStdout C:\vm-provisioning\winrm-setup-service-stdout.log'

  !insertmacro RunStep "Configure stderr redirection" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup AppStderr C:\vm-provisioning\winrm-setup-service-stderr.log'

  ; A clean exit (the script's own normal completion, success or a
  ; not-yet-ready failure deferring to the next boot) should NOT trigger
  ; NSSM's own restart-the-wrapped-process loop -- the script is meant to
  ; run once per *boot* (via SCM's Automatic startup), not be kept
  ; perpetually running or immediately relaunched by NSSM within the same
  ; boot.
  !insertmacro RunStep "Configure exit behavior (no restart loop)" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup AppExit Default Exit'

  !insertmacro RunStep "Set service to Automatic startup" \
    'C:\vm-provisioning\nssm.exe set SandboxSbomWinRmSetup Start SERVICE_AUTO_START'

  ; Confirmed live: a plain Automatic-start service began running only 14
  ; seconds after boot (per the System log's LastBootUpTime vs. this
  ; service's own log timestamp) and stalled partway through its WSMan/
  ; WinRM configuration steps -- the exact same script completes in ~10
  ; seconds flat when started manually post-boot (system fully settled),
  ; so this is a boot-timing race (network stack / dependent services not
  ; yet ready that early), not a logic bug. "Automatic (Delayed Start)" is
  ; the standard Windows mechanism for exactly this class of problem: SCM
  ; waits for other auto-start services to finish starting (plus a short
  ; additional delay) before starting this one. NSSM has no dedicated
  ; option for this -- it is a plain registry DWORD alongside the Start
  ; value SERVICE_AUTO_START already configures. The #188 restart fires
  ; 17-46 minutes into boot in every observed case so far, leaving ample
  ; margin versus the Delayed Start mechanism's typical ~1-2 minute delay.
  !insertmacro RunStep "Configure Automatic (Delayed Start)" \
    'reg.exe add "HKLM\SYSTEM\CurrentControlSet\Services\SandboxSbomWinRmSetup" /v DelayedAutostart /t REG_DWORD /d 1 /f'

  !insertmacro LogLine "WinRM service bootstrap installer finished (service registered, not started -- Automatic Delayed Start picks it up on the next boot)"
  FileClose $LogHandle
SectionEnd
