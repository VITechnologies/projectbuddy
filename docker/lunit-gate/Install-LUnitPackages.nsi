; Deploys the LUnit/LUnit CLI LabVIEW VI packages into a LabVIEW installation
; by direct file extraction (NSIS's own File instruction, a decades-proven
; mechanism for placing files into Windows system directories) -- no VIPM
; install, no VIPM activation, no secrets, per ADR-0003/REQ-034.
;
; Compiled with the already SHA-256-verified package files bundled in at
; BUILD time (see Get-VerifiedLUnitPackageStaging.ps1 and
; Build-LUnitWindowsImage.ps1 -- that script runs the staging step, then
; invokes makensis with -DLUNIT_STAGING pointing at the verified output),
; not downloaded fresh inside the target container/VM. This also sidesteps
; two real Windows-container-specific bugs hit by this engine's first
; (raw PowerShell + .NET ZipFile) implementation attempt: a MAX_PATH
; violation during ZipFile.ExtractToDirectory on a long upstream path, and
; an "access denied" failure moving a directory across a Windows-container's
; SMB-backed bind mount -- neither applies to NSIS's own File/CreateDirectory
; instructions extracting directly to their final destination.
;
; Compiled locally with makensis (apt install nsis, v3.10-2) -- no Wine or
; Windows build environment needed to COMPILE it; running the resulting
; .exe still requires a Windows guest. Fast-iterated against a real
; Windows+LabVIEW environment via vagrant-nsis-test/ (the
; labview-benchmark-actor project's `actor/win11-labview2026` golden box)
; before being baked into windows.Dockerfile -- see
; vm/windows-labview/winrm-setup-installer.nsi for the established
; RunStep-macro + persistent-log pattern this script reuses.

!include LogicLib.nsh

!ifndef LUNIT_STAGING
  !define LUNIT_STAGING "..\..\..\lunit-staging"
!endif
!ifndef INSTALL_ROOT
  !define INSTALL_ROOT "C:\Program Files\National Instruments\LabVIEW 2026"
!endif
!ifndef OUT_FILE
  !define OUT_FILE "Install-LUnitPackages.exe"
!endif
!ifndef LOG_PATH
  !define LOG_PATH "C:\lunit-gate\install.log"
!endif

Name "LUnit Package Installer"
OutFile "${OUT_FILE}"
InstallDir "${INSTALL_ROOT}"
SilentInstall silent
RequestExecutionLevel admin
ShowInstDetails nevershow

Var LogHandle
Var StepOutput
Var StepRC

; Reused verbatim from vm/windows-labview/winrm-setup-installer.nsi: runs one
; PowerShell command, capturing combined stdout/stderr and the return code
; via nsExec::ExecToStack, appending a full record to the log file -- a
; failure deep inside any one step is fully diagnosable from the log alone.
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

Section "LUnit packages" SEC_MAIN
  CreateDirectory "C:\lunit-gate"
  FileOpen $LogHandle "${LOG_PATH}" w
  FileWrite $LogHandle "LUnit package installer starting$\r$\n"
  FileWrite $LogHandle "InstallDir: $INSTDIR$\r$\n"

  ; File /r bundles (at compile time) and extracts (at install time) the
  ; already-verified package trees directly to their final destination --
  ; no intermediate temp-directory extraction/move step at all, which is
  ; precisely where both real bugs above occurred in the previous approach.
  SetOutPath "$INSTDIR\vi.lib\Astemes\LUnit"
  File /r "${LUNIT_STAGING}\LUnit\*.*"
  FileWrite $LogHandle "Extracted LUnit to $INSTDIR\vi.lib\Astemes\LUnit$\r$\n"

  SetOutPath "$INSTDIR\vi.lib\Astemes\LUnit CLI"
  File /r "${LUNIT_STAGING}\LUnit CLI\*.*"
  FileWrite $LogHandle "Extracted LUnit CLI to $INSTDIR\vi.lib\Astemes\LUnit CLI$\r$\n"

  SetOutPath "C:\lunit-gate"
  File "${LUNIT_STAGING}\dependency-manifest.json"
  FileWrite $LogHandle "Copied dependency-manifest.json to C:\lunit-gate$\r$\n"

  ; Configure LabVIEW's TCP VI Server (required for LabVIEWCLI's -PortNumber
  ; connection) -- a small, targeted PowerShell call via RunStep, not a
  ; wholesale script invocation, keeping this installer's own log the single
  ; source of diagnostic truth for every step.
  !insertmacro RunStep "Configure LabVIEW.ini TCP server settings" \
    'powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = \"Stop\"; $$ini = \"$INSTDIR\LabVIEW.ini\"; $$lines = @(Get-Content -LiteralPath $$ini); foreach ($$setting in @(\"server.tcp.enabled=True\",\"server.tcp.port=3363\",\"server.tcp.access=+127.0.0.1\",\"server.vi.access=+*\")) { $$key = $$setting.Split(\"=\")[0]; $$lines = @($$lines | Where-Object { $$_ -notmatch (\"^\\s*\" + [regex]::Escape($$key) + \"\\s*=\") }); $$lines += $$setting }; $$lines | Set-Content -LiteralPath $$ini -Encoding Ascii; Write-Output done"'

  ; Verify the deployment actually landed where LabVIEWCLI will look for it
  ; -- fail loudly (non-zero exit) rather than silently producing a
  ; container that looks built but cannot actually run LUnit.
  ${IfNot} ${FileExists} "$INSTDIR\vi.lib\Astemes\LUnit CLI\LUnitCLI\RunOperation.vi"
    FileWrite $LogHandle "RESULT: FAILED -- RunOperation.vi missing after extraction$\r$\n"
    FileClose $LogHandle
    SetErrorLevel 1
    Quit
  ${EndIf}

  FileWrite $LogHandle "LUnit package installer completed successfully$\r$\n"
  FileClose $LogHandle
SectionEnd
