# ProjectBuddy

A public, project-agnostic engine for provisioning self-hosted build/test
infrastructure and running native SBOM reporting pipelines, maintained by
[VI Technologies](https://github.com/VITechnologies).

Anyone forking this repository can use it for their own projects by
supplying their own `.toml`/`.json` project configuration — this repository
ships with **zero pre-committed project configs**, VI Technologies-specific
identifiers, or private infrastructure state. Private consumers (such as
`VITechnologies/SandboxSBOMReporting`) supply their own real project
configuration and infrastructure state separately, and consume this
repository's reusable workflows and scripts as a versioned, pinned
dependency — never the other way around. No credentials or private
configuration ever flow into this repository.

## Consumption model

- **Reusable GitHub Actions workflows** are called by downstream repositories
  via `uses: VITechnologies/projectbuddy/.github/workflows/<name>.yml@<tag>`,
  pinned to a release tag whose commit SHA is independently documented and
  verified by the caller (matching the pattern already established for
  LabVIEW plugin module releases in the `labview-benchmark-actor-plugins`
  org).
- **Directly-invoked scripts** (not wrapped in a GitHub Actions workflow) are
  consumed via a second `actions/checkout` step pinned to the exact release
  commit SHA (not just the tag), inside any downstream workflow that needs
  them — the same trust model GitHub's own `uses: owner/repo@<sha>` syntax
  already applies to every action pinned throughout this engine's callers.
  This repository does not use git submodules or vendored/copied script
  files for this purpose.
- **Upgrade path**: downstream repositories bump their pinned tag/SHA
  deliberately, as a reviewed code change (a version-bump commit), never
  automatically and never by tracking a floating branch or `latest`.

See [CHANGELOG.md](CHANGELOG.md) for released versions.
