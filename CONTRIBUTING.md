# Contributing

Use uv and keep `pyproject.toml` and `uv.lock` together. See [environment instructions](docs/uv.md).
Run the unittest suite and public-package checks described in [README](README.md).
Tests and CI must use mocks or loopback devices. A physical device test requires an explicitly selected,
identity-pinned instrument, current wiring and task authorization; never use a developer's local address as a default.

Preserve bounded deadlines, strict identity checks, shared locks, structured error receipts,
voltage checks and caller-owned measurement decisions. Do not add generic raw SCPI escape tools.
Runtime dependency updates and new hardware features need their own validation;
document what was tested and what remains unverified.

Keep examples generic. Do not commit credentials, SSH configuration, serial numbers, private host
addresses, manufacturer manuals, raw captures or screenshots. Retain upstream attribution in NOTICE.
Open an issue with software/contract versions, model/firmware and redacted evidence; keep identifiers private.
