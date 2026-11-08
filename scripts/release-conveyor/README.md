# Release conveyor harness prototype

This directory contains a synthetic transition-harness prototype for the
planned Docker-bracketed release qualification conveyor in the ProjectBuddy
wiki. The PR conformance workflow tests the harness only; it is not the
release gate and does not qualify a VM or release.

Run locally:

```bash
python3 -m unittest discover -s scripts/release-conveyor -p 'test_*.py'
python3 scripts/release-conveyor/docker_conformance.py
```

`harness.py` consumes station and stage records and never controls Docker,
VirtualBox, GitHub, or cleanup. The conformance driver supplies synthetic
records. The current prototype does not authenticate or verify actual LUnit,
VM-lineage, cleanup, host-placement, or runner evidence, nor enforce
append-only station identity across independent output locations. Those
capabilities are required before the harness can participate in release
qualification. Do not treat a `PASS` from these tests or this prototype as
release evidence.

The Docker image pins its Python base image by digest. A release-quality
launcher must independently enforce and attest the sandbox and image digest;
the harness's environment checks are defense in depth only.
