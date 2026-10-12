# Changelog

## 0.6.1 — 2026-10-12

- Consolidate public English/Chinese documentation, current capabilities, architecture and validation boundaries.
- Use uv.lock for installation, development, reproducible builds, CI and migration.
- Add generic local/SSH MCP configuration, offline examples and allowlisted source export/package checks.
- Bundle current Skill and profiles; remove host-specific wording and correct 50 Ω acceptance status.
- Allow CLI contract discovery without device configuration or instrument access.
- Clarify that unsupported background jobs mean unattended/recurring work; bounded prepared capture remains supported.
- Verify exact public source bytes in wheel/sdist and add release-boundary regression tests.
- Keep the instrument-control API and contract 1.0.0 unchanged. This release has not been deployed to the bench host.

## 0.6.0 — existing bench baseline

- Add trigger coupling/noise rejection/holdoff and short-lived prepared capture ownership.
- Support WAIT-before-stimulus, leased AFG enabling, collect/cancel/expiry and explicit recovery.

## Earlier development

Implemented bounded TCP framing, identity/locks, read-only queries, channel/trigger/AFG control,
waveform capture/export, artifact delivery, offline analysis/timing, profiles/contracts,
deep-record budgets and explicit wake/release. Historical private release files are not included here.
