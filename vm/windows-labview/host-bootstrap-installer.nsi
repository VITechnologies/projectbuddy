; First-class host-bootstrap infrastructure for Windows self-hosted
; vm-provisioner runners (operator direction 2026-11-05), following the
; same structural-determinism principle as winrm-setup-installer.nsi --
; every check/install step writes its own command, exit code, and
; captured output to a persistent log file, so a failure is fully
; diagnosable from the log alone, without needing a live session on the
; host to find out what happened.
;
; Replaces the ad-hoc, scattered "Ensure X Installed on Windows" PowerShell
; steps previously duplicated across provision-windows-labview-vm.yml, each
; with its own copy-pasted PATH-refresh/fallback logic -- confirmed live
; 2026-11-05 to be a real source of bugs (a step replacing, rather than
; appending to, $env:Path discarded another step's fix for the rest of
; that same job). This installer makes PATH additions PERMANENT at the
; system/registry level (not a per-job $env:Path or $GITHUB_PATH
; workaround), so the fix persists across every future job on this host,
; not just the job that happened to trigger the install.
;
; Covers: chocolatey, Python, NSIS (makensis.exe), VirtualBox presence +
; PATH. Does NOT auto-install VirtualBox itself (a hypervisor with
; kernel-mode drivers is a different risk class than a build tool and can
; conflict with an existing Hyper-V/WSL2/Docker Desktop setup -- a real
; operator concern, not hypothetical, confirmed 2026-11-05) and does NOT
; auto-install the VirtualBox VNC Extension Pack (confirmed live
; 2026-11-05: Oracle's official Windows Extension Pack for VirtualBox
; 7.2.14 no longer includes any VRDE module at all; the VNC-capable module
; used on this project's Linux host comes from Ubuntu's own from-source
; rebuild of the fully open-source VBoxVNC code, not distributed as a
; downloadable file for Windows). Both are reported as clear, loud,
; operator-gated gaps in the log instead of silently failing or being
; worked around.
;
; Compiled with makensis (confirmed available via `apt install nsis`) --
; no Wine or Windows build environment needed to COMPILE it; running the
; resulting .exe requires a Windows host.

!include LogicLib.nsh

Name "VM Provisioner Host Bootstrap"
OutFile "host-bootstrap-installer.exe"
SilentInstall silent
RequestExecutionLevel admin
ShowInstDetails nevershow

Var LogHandle
Var StepOutput
Var StepRC

!macro LogLine text
  FileWrite $LogHandle "${text}$\r$\n"
!macroend

; See winrm-setup-installer.nsi's identical macro for the FileWrite-vs-
; !insertmacro call-site parsing rationale.
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

; Persists a directory onto the SYSTEM (HKLM) PATH permanently, if not
; already present -- not just this process's $env:Path, so every future
; job on this host (not just the one that happened to trigger this
; installer) sees it without needing its own PATH-refresh workaround.
; Confirmed live 2026-11-05: a stale/missing *registry* PATH entry (not
; just a stale process environment block) was the real root cause behind
; several bugs this same day (VBoxManage.exe discovery, python.exe
; discovery both needed per-step workarounds that this macro replaces).
!macro EnsureSystemPathContains dir
  nsExec::ExecToStack 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; $$p = [System.Environment]::GetEnvironmentVariable(\"Path\", \"Machine\"); $$want = \"${dir}\"; if (($$p -split \";\") -notcontains $$want) { [System.Environment]::SetEnvironmentVariable(\"Path\", \"$$p;$$want\", \"Machine\"); Write-Output \"added\" } else { Write-Output \"already present\" }"'
  Pop $StepRC
  Pop $StepOutput
  FileWrite $LogHandle "---- Ensure system PATH contains ${dir} ----$\r$\n"
  FileWrite $LogHandle "exit code: $StepRC$\r$\n"
  FileWrite $LogHandle "output:$\r$\n$StepOutput$\r$\n"
!macroend

Section "Host Bootstrap" SEC_MAIN
  CreateDirectory "C:\vm-provisioning"
  FileOpen $LogHandle "C:\vm-provisioning\host-bootstrap-install.log" w
  !insertmacro LogLine "Host bootstrap installer starting"

  ; --- Chocolatey ---
  nsExec::ExecToStack 'where choco.exe'
  Pop $StepRC
  Pop $StepOutput
  ${If} $StepRC != 0
    !insertmacro LogLine "chocolatey not found; installing"
    !insertmacro RunStep "Install chocolatey" \
      'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; Set-ExecutionPolicy Bypass -Scope Process -Force; [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; Invoke-Expression ((New-Object System.Net.WebClient).DownloadString(\"https://community.chocolatey.org/install.ps1\"))"'
  ${Else}
    !insertmacro LogLine "chocolatey already present"
  ${EndIf}
  !insertmacro EnsureSystemPathContains "C:\ProgramData\chocolatey\bin"

  ; --- Python ---
  nsExec::ExecToStack 'where python.exe'
  Pop $StepRC
  Pop $StepOutput
  ${If} $StepRC != 0
    !insertmacro LogLine "python not found; installing"
    !insertmacro RunStep "Install python3 via chocolatey" 'C:\ProgramData\chocolatey\bin\choco.exe install python3 --no-progress -y'
  ${Else}
    !insertmacro LogLine "python already present"
  ${EndIf}

  ; --- NSIS (makensis.exe) ---
  nsExec::ExecToStack 'where makensis.exe'
  Pop $StepRC
  Pop $StepOutput
  ${If} $StepRC != 0
    !insertmacro LogLine "makensis.exe not found; installing"
    !insertmacro RunStep "Install NSIS via chocolatey" 'C:\ProgramData\chocolatey\bin\choco.exe install nsis --no-progress -y'
    !insertmacro EnsureSystemPathContains "$PROGRAMFILES32\NSIS"
  ${Else}
    !insertmacro LogLine "makensis.exe already present"
  ${EndIf}

  ; --- VirtualBox presence (NOT auto-installed -- see header comment) ---
  nsExec::ExecToStack 'where VBoxManage.exe'
  Pop $StepRC
  Pop $StepOutput
  ${If} $StepRC != 0
    IfFileExists "$PROGRAMFILES64\Oracle\VirtualBox\VBoxManage.exe" 0 vbox_missing
      !insertmacro EnsureSystemPathContains "$PROGRAMFILES64\Oracle\VirtualBox"
      Goto vbox_done
    vbox_missing:
      !insertmacro LogLine "GAP (operator-gated, not auto-installed): VirtualBox not found. A hypervisor with kernel-mode drivers is a different risk class than a build tool -- it can conflict with an existing Hyper-V/WSL2/Docker Desktop setup on this host. Install it manually: https://www.virtualbox.org/wiki/Downloads"
    vbox_done:
  ${Else}
    !insertmacro LogLine "VirtualBox already present and on PATH"
  ${EndIf}

  ; --- VirtualBox VNC Extension Pack (NOT auto-installed -- see header) ---
  ; Delegates the "does the extpack list mention VNC" check to PowerShell
  ; (via its own -match) rather than hand-rolling string search in NSIS.
  nsExec::ExecToStack 'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"SilentlyContinue\"; $$ep = ((& VBoxManage.exe list extpacks) -join \"`n\"); Write-Output $$ep; if ($$ep -match \"Pack no\\. 0:\\s*VNC\") { exit 0 } else { exit 1 }"'
  Pop $StepRC
  Pop $StepOutput
  FileWrite $LogHandle "---- VirtualBox VNC Extension Pack check ----$\r$\n$StepOutput$\r$\n"
  ${If} $StepRC != 0
    !insertmacro LogLine "GAP (operator-gated, not auto-installed): VirtualBox VNC Extension Pack not found. Confirmed 2026-11-05: Oracle's official Windows Extension Pack no longer includes any VRDE module; the VNC-capable module used on this project's Linux host comes from Ubuntu's own from-source rebuild of VBoxVNC, not available as a downloadable file for Windows. VNC-dependent automation (vnc-bootstrap.py, enable-vrde) will not work on this host until this is resolved."
  ${Else}
    !insertmacro LogLine "VirtualBox VNC Extension Pack already present"
  ${EndIf}

  !insertmacro LogLine "Host bootstrap installer finished"
  FileClose $LogHandle
SectionEnd
