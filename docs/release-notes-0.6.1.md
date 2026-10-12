# rigol-scope-kit 0.6.1

Portable Linux SCPI service, 41-tool stdio MCP server and bundled Agent Skill for RIGOL MHO900.
This release consolidates documentation, generic configuration, uv-based dependencies and release checks.
CLI contract discovery now works without a configured instrument. The hardware-control API and contract 1.0.0 are unchanged.

MHO954 historical validation includes basic control, both AFG outputs, 50 Ω direct connection,
trigger options, WAIT-before-stimulus and single-channel 500M-point full export.
MHO934/MHO984 and maximum-depth full multi-channel export remain unverified.
Near-zero instrument phase/delay can return an invalid sentinel; offline sine fitting is available.

Install from the source release with `uv sync --frozen --no-dev`; see [uv instructions](uv.md).
Linux server required; remote clients connect over SSH stdio. No unattended jobs or raw SCPI escape interface.
Apache-2.0; upstream attribution retained. Physical controls were not retested for this packaging/documentation revision.
