# `tools/north-star-a4/`: harness for the registered test of standard `@8`

Pre-registration: [#337, A4-r1](https://github.com/VITechnologies/projectbuddy/issues/337#issuecomment-7012817917)
(sha256 of the comment body `7737129e303a4c42669e27ccdef5b8fb3690789189ab290aef3e89e8ffdb50cb`).
Issue: [#379](https://github.com/VITechnologies/projectbuddy/issues/379).

- [`a4.py`](a4.py): `select` (the frozen sample and its digest), `workspace` (one
  authoring workspace per arm and block), `packets` (blind packets and key),
  `analyse` (E1 and E2 tests, bootstrap intervals, decision).
- [`run_session.sh`](run_session.sh): one automated authoring session (pinned model, sandbox
  flags, no memory), which marks its session in `kpi_local` for #379.
- [`rubrics.md`](rubrics.md): the reviewer instructions, used verbatim.
- [`power.py`](power.py): the joint-power simulation of A4-r1 section 6.
- [`pilot-labels.json`](pilot-labels.json): the labels of the control calibration pilot.

The workspace needs a clone of the member at commit `6a9ad5cc6bf9751ea3d80467840ab6dd7e48e3f2`,
the projectbuddy wiki at `a1e6f4c`, the RSR checkout with tags `v0.11.0` and a working tree at
`v0.12.0`, and the pytest-junit report of the 21 test files named by the eligible pages.
