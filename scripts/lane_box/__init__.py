"""Lane box: a LabVIEW-free Ubuntu base image for disposable lanes (VITechnologies/projectbuddy#183).

Modules:
- ``profile``: the lane profile (``projectbuddy/ubuntu-lane-profile@1``).
- ``toolchain``: guest-side installer that checks every digest before installing anything.
- ``access``: guest-side install and attestation of key-only root SSH from the host; the
  installed files are under ``guest/``.
- ``readiness``: guest-side scan for runner configuration, runner units, LabVIEW residue,
  the declared toolchain probes, and lane access (``projectbuddy/ubuntu-lane-readiness@2``).
- ``receipt``: the lane box receipt (``projectbuddy/ubuntu-lane-box@2``) and its verifier.
- ``packager``: host-side packager, dry-run by default.

Standard library only. The guest-side modules run on the guest's Python 3.12; the
host side and the tests run on Python 3.11 or later.
"""
