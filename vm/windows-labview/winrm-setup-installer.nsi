; Prototype for issue #192 (epic #190): package the WinRM setup logic
; (currently hand-rolled as Enable-WinRmAutomation.ps1, base64-encoded and
; invoked via VBoxManage unattended install's --post-install-command) as an
; NSIS-built, silently-installable .exe instead.
;
; This does NOT solve the #188 reboot race (see issue #191's closing
; comment: neither specialize-pass nor oobeSystem-pass command registration
; survives that restart, regardless of mechanism) -- its value is
; structural/diagnostic determinism: every step writes its own command,
; exit code, and captured stdout/stderr to a persistent log file on disk
; (C:\vm-provisioning\winrm-setup-install.log), unlike the current approach
; where a failure deep inside the single monolithic
; Enable-WinRmAutomation.ps1 script leaves no trace beyond "the log file
; stopped". This log is designed to be pulled back and iterated on fast via
; a Vagrant test harness (vagrant-nsis-test/) against the labview-
; benchmark-actor project's existing `actor/win11-labview2026` golden box,
; instead of a full ~20-30 minute from-scratch Windows Setup cycle for
; every change.
;
; Compiled locally with makensis (confirmed available via `apt install
; nsis`, v3.10-2) -- no Wine or Windows build environment needed to COMPILE
; it; running the resulting .exe still requires a Windows guest.
;
; Keep the actual WinRM setup logic in sync with Enable-WinRmAutomation.ps1
; and ensure-winrm-self-heal.ps1 if requirements change -- this is a
; prototype living alongside those, not (yet) a replacement for either.

!include LogicLib.nsh

Name "WinRM Setup"
OutFile "winrm-setup-installer.exe"
SilentInstall silent
RequestExecutionLevel admin
ShowInstDetails nevershow

Var LogHandle
Var StepOutput
Var StepRC

!macro LogLine text
  FileWrite $LogHandle "${text}$\r$\n"
!macroend

; Runs one PowerShell command, capturing its combined stdout/stderr and
; return code via nsExec::ExecToStack, and appends a full record (command,
; return code, captured output) to the log file -- so a failure deep inside
; any one step is fully diagnosable from the log alone, without needing a
; live VNC/WinRM session into the guest to find out what happened.
;
; Uses FileWrite directly (not the LogLine macro) for lines built from the
; ${description}/${command} parameters: NSIS's !insertmacro call-site
; parsing splits on whitespace, so passing an already-substituted
; multi-word/quoted parameter into ANOTHER !insertmacro call (as LogLine
; would need) breaks apart into many bogus parameters. FileWrite's own
; argument parsing takes the remainder of the line as one string instead,
; which doesn't have this problem.
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

Section "WinRM setup" SEC_MAIN
  CreateDirectory "C:\vm-provisioning"
  FileOpen $LogHandle "C:\vm-provisioning\winrm-setup-install.log" w
  !insertmacro LogLine "WinRM setup installer starting"

  !insertmacro RunStep "Disable update-related services (wuauserv/UsoSvc/InstallService)" \
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; try { \"wuauserv\",\"UsoSvc\",\"InstallService\" | ForEach-Object { Stop-Service $$_ -Force -ErrorAction SilentlyContinue; Set-Service $$_ -StartupType Disabled -ErrorAction SilentlyContinue }; Write-Output done } catch { Write-Output (\"ERROR: \" + $$_.Exception.Message); exit 1 }"'

  ; PowerShell's registry provider (New-Item/New-ItemProperty) throws
  ; UnauthorizedAccessException writing this specific key even from a
  ; genuinely local, UAC-elevated admin session -- confirmed live both over
  ; remote WinRM (this Vagrant harness) and via a real VNC-driven elevated
  ; session on a freshly-provisioned VM (issue #199/#201, epic #198). This
  ; is no longer a "test-harness-only" limitation to shrug off: epic #198's
  ; design runs this installer from exactly that VNC-elevated-admin context
  ; in production, not as SYSTEM via FirstLogonCommands. reg.exe (a plain
  ; Win32 API wrapper, not .NET's more conservative RegistryKey class) adds
  ; the same value successfully from the identical elevated-but-not-SYSTEM
  ; context -- use it instead of PowerShell's provider for this one key.
  !insertmacro RunStep "Set LocalAccountTokenFilterPolicy" \
    'reg.exe add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" /v LocalAccountTokenFilterPolicy /t REG_DWORD /d 1 /f'

  !insertmacro RunStep "Enable-PSRemoting" \
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; try { Enable-PSRemoting -Force -SkipNetworkProfileCheck | Out-Null; Write-Output done } catch { Write-Output (\"ERROR: \" + $$_.Exception.Message); exit 1 }"'

  ; Restored from Enable-WinRmAutomation.ps1 (omitted from this prototype's
  ; first iteration, caught live by this same Vagrant test harness): a
  ; "Public" network category blocks the firewall rule below from actually
  ; taking effect, confirmed live in testing ("WinRM firewall exception
  ; will not work since one of the network connection types on this
  ; machine is set to Public").
  !insertmacro RunStep "Set network category to Private" \
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; try { Get-NetConnectionProfile -ErrorAction SilentlyContinue | Where-Object NetworkCategory -eq Public | Set-NetConnectionProfile -NetworkCategory Private; Write-Output done } catch { Write-Output (\"ERROR: \" + $$_.Exception.Message); exit 1 }"'

  ; NSIS interpolates $variable inside string literals (even quoted ones),
  ; so a literal "$true" for PowerShell must be escaped as "$$true".
  !insertmacro RunStep "Configure WSMan listener options and firewall rule" \
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; try { Set-Item -Path WSMan:\localhost\Service\AllowUnencrypted -Value $$true; Set-Item -Path WSMan:\localhost\Service\Auth\Basic -Value $$true; Set-Item -Path WSMan:\localhost\Shell\MaxMemoryPerShellMB -Value 1024; New-NetFirewallRule -Name VmProvisioning-WinRM-HTTP -DisplayName \"VM Provisioning WinRM HTTP\" -Direction Inbound -Protocol TCP -LocalPort 5985 -Action Allow -Profile Any -ErrorAction SilentlyContinue | Out-Null; Write-Output done } catch { Write-Output (\"ERROR: \" + $$_.Exception.Message); exit 1 }"'

  !insertmacro RunStep "Set WinRM to automatic startup and restart it" \
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; try { Set-Service WinRM -StartupType Automatic; Restart-Service WinRM; Write-Output done } catch { Write-Output (\"ERROR: \" + $$_.Exception.Message); exit 1 }"'

  !insertmacro LogLine "WinRM setup installer finished"
  FileClose $LogHandle
SectionEnd
