# NSIS WinRM-setup installer prototype + Vagrant test harness

Research artifacts for issue #192 (epic #190): a prototype of packaging the
WinRM setup logic (currently hand-rolled as `../Enable-WinRmAutomation.ps1`)
as an NSIS-built installer, plus a fast-iteration Vagrant test harness.

**This does not fix #188.** See issue #191's closing comment: the ~17-20
minute restart that interrupts first-boot WinRM setup appears to race at
the OS/logon level, not something any in-guest command (regardless of
mechanism: scheduled task, RunOnce, NSIS installer) can out-schedule. The
value here is purely structural/diagnostic determinism -- see
`winrm-setup-installer.nsi`'s header comment for details.

## Files

- `../winrm-setup-installer.nsi` -- the NSIS source. Compile with
  `makensis winrm-setup-installer.nsi` (confirmed available via
  `apt install nsis`, v3.10-2 on Ubuntu; no Wine needed to compile).
  Produces `../winrm-setup-installer.exe` (gitignored, like all `*.exe`).
- `Vagrantfile` -- brings up the labview-benchmark-actor project's existing
  `actor/win11-labview2026` golden box (Windows 11 + LabVIEW 2026 + WinRM
  already configured), uploads the compiled installer, and runs it
  on-demand -- all in under a minute per iteration, instead of a ~20-30
  minute from-scratch Windows Setup cycle.

## Usage

```bash
# One-time environment fix if `vagrant box list` errors with a dangling
# ~/.vagrant.d/boxes or .../tmp symlink (found live 2026-11-03: both
# pointed at a non-existent "vihs-vagrant" path instead of the existing
# "vagrant-home" one) -- repoint them at wherever the box actually lives:
#   rm ~/.vagrant.d/boxes ~/.vagrant.d/tmp
#   ln -s /path/to/vagrant-home/boxes ~/.vagrant.d/boxes
#   ln -s /path/to/vagrant-home/tmp ~/.vagrant.d/tmp

# Compile after any .nsi edit:
cd ..
makensis winrm-setup-installer.nsi

cd vagrant-nsis-test
vagrant up                                        # first time: imports + boots the box
vagrant provision --provision-with upload-installer  # re-upload after a recompile
vagrant provision --provision-with run-installer     # run it, prints the log to the console

# If WinRM auth starts failing after a run (observed live: the installer's
# own WSMan/firewall/service-restart steps can leave WinRM in a state that
# only clears on a full reboot, not an account lockout) -- recover with:
vagrant halt --force || VBoxManage controlvm vm-nsis-installer-test poweroff
vagrant up

# Pull the installer's own detailed log directly any time:
vagrant winrm -c 'Get-Content C:\vm-provisioning\winrm-setup-install.log' -e

vagrant destroy -f                                # tear down when done
```

## Findings so far (2026-11-03)

Two iterations surfaced and fixed real bugs before any full-pipeline test
was needed:

1. `Set-Service wuauserv,UsoSvc,InstallService -StartupType Disabled` fails
   with a `ParameterBindingException` (`Set-Service` doesn't bind an array
   to `-Name` the way `Stop-Service` does) -- fixed by iterating with
   `ForEach-Object` instead.
2. Writing `LocalAccountTokenFilterPolicy` under `HKLM:\...\Policies\System`
   intermittently hit `UnauthorizedAccessException` over this harness's
   remote WinRM "elevated" session (not necessarily a bug in the script
   itself -- the real pipeline runs this as SYSTEM via FirstLogonCommands,
   not over remote WinRM; treat a failure here as a known harness
   limitation unless it also fails run the way the real pipeline runs it).
3. The network-category-to-Private step (present in
   `Enable-WinRmAutomation.ps1`) was missing from the first installer
   draft -- without it, `New-NetFirewallRule` silently doesn't take effect
   on a `Public`-categorized connection. Restored.
4. The original draft didn't set `$ErrorActionPreference = 'Stop'` with a
   `try`/`catch`, so PowerShell errors printed to the captured output but
   didn't fail the step's own exit code -- fixed so failures are
   unambiguous in the log.
