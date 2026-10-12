# rigol-scope-kit

[中文说明](README.zh-CN.md)

Linux SCPI service, stdio MCP server and Agent Skill for RIGOL MHO900 oscilloscopes.
The measurement agent chooses a trigger, observation window, voltage range and record depth;
the service validates the request, captures once and returns readback, waveforms and evidence.
The agent evaluates that evidence and decides the next adjustment.

**Release 0.6.1 · contract 1.0.0 · 41 MCP tools · Apache-2.0.**
Bench validation covers an MHO954. MHO934/MHO984 are accepted model profiles but have not been bench-tested.
The server needs Linux and Python ≥3.11; remote clients only need SSH and an MCP client.

## Install with uv

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then from the repository:

```bash
git clone https://github.com/felixfinal/rigol-scope-kit.git
cd rigol-scope-kit
uv python install 3.13
uv sync --frozen --no-dev
uv run --frozen --no-sync rigol-scope contract
cp config/devices.example.toml config/devices.toml
```

Edit the device address and SCPI port (the example uses 5555). Run `identify` to obtain the
serial, then pin it in `devices.toml` before other live operations:

```bash
uv run --frozen --no-sync rigol-scope --config config/devices.toml identify
uv run --frozen --no-sync rigol-scope --config config/devices.toml status
```

`identify` and `status` contact the instrument. `contract` works without a device or configuration.
Keep `uv.lock` with the source; runtime installation excludes the development dependency group.
See [uv and offline installation](docs/uv.md) for pinned builds and migration details.

## MCP and Skill

Use [local MCP](config/mcp-local.example.json) or [SSH stdio MCP](config/mcp-ssh.example.json).
Replace the absolute executable/config paths and SSH host alias. The interpreter, dependencies,
state and instrument access live on the service host. Do not install on every MCP launch.

```bash
.venv/bin/rigol-scope-mcp --config /absolute/path/to/devices.toml
.venv/bin/rigol-scope-skill /new/path/rigol-oscilloscope
```

The wheel bundles the [Skill](skills/rigol-oscilloscope/SKILL.md), reference files and default profiles.
MCP also serves `scope://skill/SKILL.md` and `scope://skill/references/{name}`;
clients can retrieve them from the host instead of maintaining separate copies.
OpenClaw integration is optional. Other agent frameworks and direct MCP clients work through the same tools.

## Capabilities and limits

- Four analog channels, timebase, edge/pulse triggers, trigger coupling/noise rejection/holdoff,
  acquisition modes, measurements, screenshots and bounded single captures.
- Versioned profiles and request contracts; preview and atomic configure → readback → SINGLE → export.
- Prepared captures: observe WAIT, send an external stimulus, then collect/cancel under a short lease.
- Dual AFG basic waveforms, voltage-bound output enabling and phase synchronization.
- Chunked WORD waveform export, capture catalog, artifact retrieval, offline plots/measurements/search
  and dual-channel timing. Single-channel 500M-point export has been bench-validated.
- Wake and explicit release: unused AFG outputs off, depth AUTO, other settings retained.

See [capability matrix](docs/capabilities.md), [usage](docs/usage.md), [architecture](docs/architecture.md),
[validation](docs/validation.md) and [known issues](docs/known-issues.md).
Hardware options do not imply software support: advanced/protocol triggers, digital probes,
protocol decode, AFG modulation/burst/arbitrary upload and unattended jobs are not implemented.
Multi-channel maximum-depth full exports remain separately unverified.

## Development and release

```bash
uv sync --frozen
uv run --frozen --no-sync python -m unittest discover -s tests -q
uv run --frozen --no-sync python tools/verify_public.py
uv build --no-build-isolation --out-dir dist
uv run --frozen --no-sync twine check dist/*
uv run --frozen --no-sync python tools/verify_public.py --dist dist
```

Tests use mocks, loopback SCPI and short-lived local subprocesses; CI never accesses a physical instrument.
Read [contributing](CONTRIBUTING.md) and [release procedure](docs/releasing.md) before publishing.
Manufacturer manuals, private host configuration and live capture data are not distributed here.
See [NOTICE](NOTICE) for upstream attribution and [sources](docs/sources.md) for documentation links.
This project is not affiliated with RIGOL.
