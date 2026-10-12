"""stdio MCP; no connection is opened during discovery or initialization."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from functools import partial

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations

from .artifacts import read_artifact, read_artifact_chunk
from .config import Settings
from .errors import ScopeError
from .service import ScopeService
from .catalog import list_captures, capture_info
from .reports import derive, derive_timing
from .contracts import MeasurementRequest, get_contract
from .profiles import list_profiles, get_profile
from .measurement import read_receipt
from .skill_bundle import skill_root


def create_server(settings: Settings | None = None) -> FastMCP:
    mcp = FastMCP("rigol-scope", instructions="MHO900 bounded LAN SCPI tools. The caller supplies expected signals and decides adjustments. For new measurements, read scope_contract/profile defaults. Use scope_measurement_capture for ongoing events; use scope_prepare_capture for external stimulus, require ready before stimulus and keep the private token in the same caller run through collect/cancel. Hardware uses configured aliases; saved-data tools use capture_id and never need a live device check. State-changing calls require confirmed=True from current user authorization; no repeated confirmation within that scope. AFG enable requires current settings hash and explicit voltage bound. No unattended jobs.")

    def get_settings() -> Settings:
        return settings if settings is not None else Settings.load()

    async def call(method: str, device: str, image: bool = False, **params) -> CallToolResult:
        try:
            config = get_settings()
            service = ScopeService(config)
            # The thread retains its device lock until its bounded operation ends.
            result = await anyio.to_thread.run_sync(partial(getattr(service, method), device, **params))
            content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
            if image:
                shot=result if method=="screenshot" else (result.get("screenshot") or (result.get("measurement") or {}).get("screenshot"))
                if shot:
                    data = read_artifact(config.state_dir, shot["capture_id"], "screen.png")
                    content.append(ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType="image/png"))
            return CallToolResult(isError=result.get("verified") is False, content=content, structuredContent=result)
        except (ScopeError, OSError) as exc:
            error = {"error": str(exc), "error_type": type(exc).__name__}
            if getattr(exc,"writes_sent",None)==0:error.update(stage="preflight",writes_sent=0)
            return CallToolResult(isError=True, content=[TextContent(type="text", text=json.dumps(error))], structuredContent=error)

    annotations = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=False)
    writes = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @mcp.tool(annotations=annotations)
    def scope_contract(section: str = "overview") -> dict:
        """Read the versioned measurement contract, roles, units and schemas without instrument access. section: overview/request_schema/profile_schema/all."""
        import hashlib
        contract=get_contract()
        if section not in {"overview","request_schema","profile_schema","all"}:raise ScopeError("Unknown contract section")
        digest=hashlib.sha256(json.dumps(contract,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        if section in {"request_schema","profile_schema"}:
            return {"contract_version":contract["contract_version"],"contract_digest":digest,"instrument_access":False,section:contract[section]}
        if section=="overview":
            contract={k:v for k,v in contract.items() if k not in {"request_schema","profile_schema"}}
        return {**contract,"contract_digest":digest}

    @mcp.tool(annotations=annotations)
    def scope_profiles_list() -> dict:
        """List server-owned profile versions, digests, applicability and assumptions; no instrument access."""
        return list_profiles(get_settings().profiles_dir)

    @mcp.tool(annotations=annotations)
    def scope_profile_get(profile_id: str) -> dict:
        """Read concrete defaults and digest; use this version/digest in measurement requests."""
        return {"instrument_access":False,"profile":get_profile(get_settings().profiles_dir,profile_id)}

    @mcp.tool(annotations=annotations)
    async def scope_measurement_preview(request: MeasurementRequest, device: str = "bench-scope") -> CallToolResult:
        """Read-only merge of explicit overrides > versioned profile > fresh readback. Returns effective settings, origins and plan_digest, not a reservation."""
        return await call("measurement_preview",device,request=request)

    @mcp.tool(annotations=writes)
    async def scope_measurement_capture(request: MeasurementRequest, device: str = "bench-scope",
                                         confirmed: bool = False, preview_digest: str | None = None, operation_timeout_s: float | None = None) -> CallToolResult:
        """Validate, configure, read back, trigger once, export and screenshot under one device lock. Leaves settings applied and acquisition STOP; caller decides adjustments. Deep captures may set operation_timeout_s=5..900; otherwise device default applies."""
        return await call("measurement_capture",device,image=True,request=request,confirmed=confirmed,preview_digest=preview_digest,operation_timeout_s=operation_timeout_s)

    async def offline_call(operation: str, **params) -> CallToolResult:
        try:
            config=get_settings()
            if operation=="list":
                result=await anyio.to_thread.run_sync(partial(list_captures,config.state_dir,**params))
            elif operation=="info":
                result=await anyio.to_thread.run_sync(partial(capture_info,config.state_dir,**params))
            elif operation=="timing":
                result=await anyio.to_thread.run_sync(partial(derive_timing,config.state_dir,**params))
            else:
                result=await anyio.to_thread.run_sync(partial(derive,config.state_dir,operation=operation,**params))
            content=[TextContent(type="text",text=json.dumps(result,ensure_ascii=False,allow_nan=False))]
            if "plot_file" in result:
                png=read_artifact(config.state_dir,result["capture_id"],result["plot_file"])
                content.append(ImageContent(type="image",data=base64.b64encode(png).decode(),mimeType="image/png"))
            return CallToolResult(content=content,structuredContent=result)
        except (ScopeError,OSError,ValueError,TypeError) as exc:
            error={"error":str(exc),"error_type":type(exc).__name__,"instrument_access":False}
            return CallToolResult(isError=True,content=[TextContent(type="text",text=json.dumps(error))],structuredContent=error)

    @mcp.tool(annotations=annotations)
    async def scope_captures_list(limit: int = 20, cursor: str | None = None, serial: str | None = None,
                                  channel: int | None = None, kind: str | None = None, status: str | None = "complete",
                                  created_after: str | None = None, created_before: str | None = None) -> CallToolResult:
        """List saved captures by serial/channel/time/status, newest first. Cursor pagination. No instrument access."""
        return await offline_call("list",limit=limit,cursor=cursor,serial=serial,channel=channel,kind=kind,status=status,created_after=created_after,created_before=created_before)

    @mcp.tool(annotations=annotations)
    async def scope_capture_info(capture_id: str) -> CallToolResult:
        """Inspect a saved capture and list downloadable source/derived artifacts. No SCPI."""
        return await offline_call("info",capture_id=capture_id)

    @mcp.tool(annotations=writes)
    async def scope_waveform_plot(capture_id: str, channel: int = 1, start_s: float | None = None,
                                   end_s: float | None = None) -> CallToolResult:
        """Render saved waveform or selected trigger-relative interval as PNG using min/max envelopes. Saves derived files; no instrument access."""
        return await offline_call("plot",capture_id=capture_id,channel=channel,start_s=start_s,end_s=end_s)

    @mcp.tool(annotations=writes)
    async def scope_waveform_analyze(capture_id: str, channel: int = 1, start_s: float | None = None,
                                     end_s: float | None = None, threshold_v: float | None = None,
                                     hysteresis_v: float | None = None, low_level_v: float | None = None,
                                     high_level_v: float | None = None, export_csv: bool = False, minimum_signal_vpp: float | None = None) -> CallToolResult:
        """Compute saved-sample frequency/duty/pulse/10-90% edges and statistics, PNG+JSON+Markdown report. Up to 2M selected points; no SCPI."""
        return await offline_call("analyze",capture_id=capture_id,channel=channel,start_s=start_s,end_s=end_s,
                                  threshold_v=threshold_v,hysteresis_v=hysteresis_v,low_level_v=low_level_v,
                                  high_level_v=high_level_v,export_csv=export_csv,minimum_signal_vpp=minimum_signal_vpp)

    @mcp.tool(annotations=writes)
    async def scope_waveform_timing(capture_id: str, channel_a: int, channel_b: int,
                                    method: str = "edge_delay", frequency_hz: float | None = None,
                                    max_delay_s: float | None = None, edge: str = "rising",
                                    start_s: float | None = None, end_s: float | None = None,
                                    threshold_a_v: float | None = None, threshold_b_v: float | None = None,
                                    hysteresis_a_v: float | None = None, hysteresis_b_v: float | None = None,
                                    minimum_signal_vpp: float | None = None) -> CallToolResult:
        """Saved same-capture dual timing. edge_delay requires max_delay_s for unique pairing; sine_fit requires known frequency_hz and rejects poor fits. Positive delay means B later; positive phase means B leads. Saves PNG/JSON/Markdown, <=2M total samples, no SCPI."""
        return await offline_call("timing",capture_id=capture_id,channel_a=channel_a,channel_b=channel_b,
                                  method=method,frequency_hz=frequency_hz,max_delay_s=max_delay_s,edge=edge,
                                  start_s=start_s,end_s=end_s,threshold_a_v=threshold_a_v,threshold_b_v=threshold_b_v,
                                  hysteresis_a_v=hysteresis_a_v,hysteresis_b_v=hysteresis_b_v,minimum_signal_vpp=minimum_signal_vpp)

    @mcp.tool(annotations=writes)
    async def scope_waveform_search(capture_id: str, kind: str, channel: int = 1,
                                    minimum: float | None = None, maximum: float | None = None,
                                    positive: bool = True, start_s: float | None = None, end_s: float | None = None,
                                    threshold_v: float | None = None, hysteresis_v: float | None = None,
                                    low_level_v: float | None = None, high_level_v: float | None = None,
                                    limit: int = 50) -> CallToolResult:
        """Find saved pulse_width/edge_gap (seconds) or voltage_range (volts) outside minimum/maximum. Return bounded events, report and marked PNG; no SCPI."""
        return await offline_call("search",capture_id=capture_id,channel=channel,search_kind=kind,
                                  minimum=minimum,maximum=maximum,positive=positive,start_s=start_s,end_s=end_s,
                                  threshold_v=threshold_v,hysteresis_v=hysteresis_v,low_level_v=low_level_v,
                                  high_level_v=high_level_v,limit=limit)

    @mcp.tool(annotations=annotations)
    async def scope_trigger_options(device: str = "bench-scope") -> CallToolResult:
        """Read trigger coupling, noise rejection and holdoff; these are distinct from channel input coupling."""
        return await call("trigger_options",device)

    @mcp.tool(annotations=writes)
    async def scope_configure_trigger_options(settings: dict, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Set coupling AC/DC/LFR/HFR (EDGE only), noise_reject Boolean, holdoff_s 8ns..10s; skip unchanged values, verify readback."""
        return await call("execute_control",device,control_method="configure_trigger_options",settings=settings,confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_prepare_capture(request: MeasurementRequest, device: str = "bench-scope", confirmed: bool = False,
                                     lease_s: float = 60, ready_wait_s: float = 5, export_timeout_s: float = 30,
                                     keep_generator_outputs: list[int] | None = None) -> CallToolResult:
        """Configure and arm a single capture. Return ready only after observed WAIT. Bounded owner holds hardware locks; lease 5..120s, then STOP/release/AUTO cleanup. Keep reservation_token private in the same caller run; external stimulus agent receives no token."""
        return await call("prepare_capture",device,request=request,confirmed=confirmed,lease_s=lease_s,
                          ready_wait_s=ready_wait_s,export_timeout_s=export_timeout_s,keep_generator_outputs=keep_generator_outputs)

    @mcp.tool(annotations=annotations)
    async def scope_prepared_status(reservation_id: str, reservation_token: str, device: str = "bench-scope") -> CallToolResult:
        """Read owner state/terminal receipt without taking the device lock. Token stays with its owner; no instrument writes."""
        return await call("prepared_status",device,reservation_id=reservation_id,reservation_token=reservation_token)

    @mcp.tool(annotations=writes)
    async def scope_prepared_collect(reservation_id: str, reservation_token: str, device: str = "bench-scope", wait_s: float = 0) -> CallToolResult:
        """Collect only after STOP follows observed WAIT. wait_s<=10 may return capture_complete=false. Terminal retries return the same saved receipt, never re-arm. Export followed by STOP/unused outputs off/memory AUTO."""
        return await call("prepared_collect",device,image=True,reservation_id=reservation_id,reservation_token=reservation_token,wait_s=wait_s)

    @mcp.tool(annotations=writes)
    async def scope_prepared_cancel(reservation_id: str, reservation_token: str, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Owner cancellation with cleanup. Export cancels at next block boundary; inspect cleanup_complete/status. Explicit crash recovery allowed only with owner credential and acquired device locks."""
        return await call("prepared_cancel",device,reservation_id=reservation_id,reservation_token=reservation_token,confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_prepared_generator_output(reservation_id: str, reservation_token: str, channel: int, enabled: bool,
                                               device: str = "bench-scope", confirmed: bool = False,
                                               expected_settings_hash: str | None = None, max_open_circuit_v: float | None = None) -> CallToolResult:
        """Optional AFG stimulus through the reservation owner. Enable only while WAIT, with preconfigured hash and voltage bound. External MCU stimuli use their own owner/tool; never share this token."""
        return await call("prepared_generator_output",device,reservation_id=reservation_id,reservation_token=reservation_token,
                          channel=channel,enabled=enabled,confirmed=confirmed,expected_settings_hash=expected_settings_hash,max_open_circuit_v=max_open_circuit_v)

    @mcp.tool(annotations=writes)
    async def scope_configure_channel(channel: int, settings: dict, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Set enabled, probe_ratio, impedance ('1M'/'50'), coupling, scale_v_div, offset_v or bandwidth_limit; return requested/actual."""
        return await call("execute_control", device, control_method="configure_channel", channel=channel, settings=settings, confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_configure_timebase(settings: dict, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Set scale_s_div and/or offset_s; return requested and actual values."""
        return await call("execute_control", device, control_method="configure_timebase", settings=settings, confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_configure_trigger(channel: int, level_v: float, slope: str = "POS", sweep: str = "AUTO", device: str = "bench-scope", confirmed: bool = False, coupling: str | None = None, noise_reject: bool | None = None, holdoff_s: float | None = None) -> CallToolResult:
        """Configure EDGE trigger, POS/NEG/RFAL slope, AUTO/NORM/SING sweep; read back actual settings."""
        return await call("execute_control", device, control_method="configure_trigger", channel=channel, level_v=level_v, slope=slope, sweep=sweep, confirmed=confirmed, coupling=coupling, noise_reject=noise_reject, holdoff_s=holdoff_s)

    @mcp.tool(annotations=writes)
    async def scope_configure_pulse_trigger(channel: int, level_v: float, condition: str = "greater", width_s: float | None = None, lower_s: float | None = None, upper_s: float | None = None, polarity: str = "POS", sweep: str = "NORM", device: str = "bench-scope", confirmed: bool = False, noise_reject: bool | None = None, holdoff_s: float | None = None) -> CallToolResult:
        """Configure pulse greater/less width_s or between lower_s/upper_s, POS/NEG, with readback."""
        return await call("execute_control", device, control_method="configure_pulse_trigger", channel=channel, level_v=level_v, condition=condition, width_s=width_s, lower_s=lower_s, upper_s=upper_s, polarity=polarity, sweep=sweep, confirmed=confirmed, noise_reject=noise_reject, holdoff_s=holdoff_s)

    @mcp.tool(annotations=writes)
    async def scope_configure_acquisition(settings: dict, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Set type NORM/AVER/PEAK/HRES, averages or memory_depth (integer points/AUTO), respecting channel count and license."""
        return await call("execute_control", device, control_method="configure_acquisition", settings=settings, confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_control(action: str, wait_s: float = 0, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """run/stop/single acquisition. Optional bounded wait_s only for single; trigger timeout stops acquisition."""
        return await call("execute_control", device, control_method="acquisition_control", confirmed=confirmed, action=action, wait_s=wait_s)

    @mcp.tool(annotations=annotations)
    async def scope_measure(channels: list[int], measurements: list[str], device: str = "bench-scope") -> CallToolResult:
        """Instrument measurements: frequency, period, vpp, vmax, vmin, mean, rms, duty, negative_duty, rise_time, fall_time, positive_width, negative_width. Invalid readings return null."""
        return await call("measure", device, channels=channels, measurements=measurements)

    @mcp.tool(annotations=annotations)
    async def scope_measure_timing(channel_a: int, channel_b: int, measurement: str = "rr_delay", device: str = "bench-scope") -> CallToolResult:
        """Query two-channel rr/rf/fr/ff delay or phase with explicit source channels."""
        return await call("measure_timing", device, channel_a=channel_a, channel_b=channel_b, measurement=measurement)

    @mcp.tool(annotations=writes)
    async def scope_generator_configure(channel: int, settings: dict, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Configure an OFF AFG channel: waveform SIN/SQU/RAMP/DC/NOIS, frequency_hz, amplitude_vpp, offset_v, phase_deg, load highz/50, duty_percent or symmetry_percent."""
        return await call("execute_control", device, control_method="configure_generator", channel=channel, settings=settings, confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_generator_output(channel: int, enabled: bool, expected_settings_hash: str | None = None, max_open_circuit_v: float | None = None, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Enable only with current settings_hash and permitted worst-case open-circuit peak voltage. Disable explicitly; no reconnect auto-enable."""
        return await call("execute_control", device, control_method="generator_output", channel=channel, enabled=enabled, expected_settings_hash=expected_settings_hash, max_open_circuit_v=max_open_circuit_v, confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_generator_sync_phase(device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Synchronize AFG phases without changing output switches. Physical phase needs a two-channel measurement."""
        return await call("execute_control", device, control_method="synchronize_phase", confirmed=confirmed)

    @mcp.tool(annotations=writes)
    async def scope_waveform_export(channels: list[int], mode: str = "NORM", points: int = 1000, start: int = 1, chunk_points: int = 100000, stop_before_capture: bool = False, run_after_capture: bool = False, max_bytes: int = 268435456, device: str = "bench-scope", confirmed: bool = False, operation_timeout_s: float | None = None) -> CallToolResult:
        """Coherent WORD export from STOP, NORM<=1000 or bounded RAW window. Restore transfer settings; save binary/metadata, CSV for small records. No background continuation. Deep exports may set operation_timeout_s=5..900; otherwise device default applies."""
        return await call("waveform_export", device, channels=channels, mode=mode, points=points, start=start, chunk_points=chunk_points, stop_before_capture=stop_before_capture, run_after_capture=run_after_capture, max_bytes=max_bytes, confirmed=confirmed, operation_timeout_s=operation_timeout_s)

    @mcp.tool(annotations=writes)
    async def scope_capture_single(channels: list[int], mode: str = "NORM", points: int = 1000, wait_s: float = 10, device: str = "bench-scope", confirmed: bool = False) -> CallToolResult:
        """Arm SINGLE and export the stopped event under one device lock, leaving acquisition stopped. Uses current trigger settings."""
        return await call("capture_single", device, channels=channels, mode=mode, points=points, wait_s=wait_s, confirmed=confirmed)

    @mcp.tool(annotations=annotations)
    def scope_artifact_read(capture_id: str, name: str, offset: int = 0, size: int = 65536) -> dict:
        """Read a bounded saved artifact chunk (<=256 KiB), base64 plus offset/hash; no device access."""
        return read_artifact_chunk(get_settings().state_dir, capture_id, name, offset, size)

    @mcp.tool(annotations=annotations)
    async def scope_identify(device: str = "bench-scope") -> CallToolResult:
        """Read instrument identity. Unpinned configuration is allowed only here/doctor."""
        return await call("identify", device)

    @mcp.tool(annotations=annotations)
    async def scope_capabilities(device: str = "bench-scope") -> CallToolResult:
        """Query actual licenses and list implemented software capabilities."""
        return await call("capabilities", device)

    @mcp.tool(annotations=annotations)
    async def scope_power_status(device: str = "bench-scope") -> CallToolResult:
        """Read low-power state without waking the instrument."""
        return await call("power_status",device)

    @mcp.tool(annotations=writes)
    async def scope_wake(device: str = "bench-scope", confirmed: bool = False, wait_s: float = 10) -> CallToolResult:
        """Explicit bounded wake from low power; read back state. Does not enable AFG outputs or recover old RAM data."""
        return await call("wake",device,confirmed=confirmed,wait_s=wait_s)

    @mcp.tool(annotations=writes)
    async def scope_release(device: str = "bench-scope", confirmed: bool = False,
                            keep_generator_outputs: list[int] | None = None) -> CallToolResult:
        """Explicit end-of-task release: turn off unused AFG outputs and set memory depth AUTO. Retain outputs only with an ongoing task requirement; keep other settings. Verify every action, no automatic disconnect hook."""
        return await call("execute_control",device,control_method="release",confirmed=confirmed,keep_generator_outputs=keep_generator_outputs)

    @mcp.tool(annotations=annotations)
    async def scope_status(device: str = "bench-scope") -> CallToolResult:
        """Read four analog channels and acquisition configuration; does not stop or autoscale."""
        return await call("status", device)

    @mcp.tool(annotations=annotations)
    async def scope_generator_status(device: str = "bench-scope") -> CallToolResult:
        """Inspect both AFG channels, output/load/modulation state; never enables output."""
        return await call("generator_status", device)

    @mcp.tool(annotations=annotations)
    async def scope_screenshot(device: str = "bench-scope") -> CallToolResult:
        """Read current display as PNG; return image plus saved artifact metadata, without changing acquisition."""
        return await call("screenshot", device, image=True)

    @mcp.tool(annotations=annotations)
    async def scope_doctor(device: str = "bench-scope") -> CallToolResult:
        """Validate configuration and live identity only; no waveform or output test."""
        return await call("doctor", device)

    @mcp.resource("scope://prepared/{reservation_id}/receipt.json", mime_type="application/json")
    def prepared_receipt(reservation_id: str) -> str:
        """Read a terminal public receipt, never live credentials or private IPC details."""
        from .reservations import read_public_receipt
        return read_public_receipt(get_settings(),reservation_id)

    @mcp.resource("scope://skill/SKILL.md", mime_type="text/markdown")
    def skill_entry() -> str:
        """Read the Skill bundled with this running server release; no instrument access."""
        return (skill_root()/"SKILL.md").read_text()

    @mcp.resource("scope://skill/references/{name}", mime_type="text/markdown")
    def skill_reference(name: str) -> str:
        """Read a named bundled Skill reference; only packaged Markdown files are allowed."""
        root=skill_root()/"references"
        allowed={p.name for p in root.glob("*.md") if p.is_file() and not p.is_symlink()}
        if name not in allowed:raise ScopeError("Unknown Skill reference")
        return (root/name).read_text()

    @mcp.resource("scope://artifacts/{capture_id}/screen.png", mime_type="image/png")
    def screenshot_resource(capture_id: str) -> bytes:
        return read_artifact(get_settings().state_dir, capture_id, "screen.png")

    @mcp.resource("scope://artifacts/{capture_id}/metadata.json", mime_type="application/json")
    def metadata_resource(capture_id: str) -> str:
        return read_artifact(get_settings().state_dir, capture_id, "metadata.json").decode()

    @mcp.resource("scope://files/{capture_id}/{name}", mime_type="application/octet-stream")
    def file_resource(capture_id: str, name: str) -> bytes:
        return read_artifact(get_settings().state_dir,capture_id,name)

    @mcp.resource("scope://measurements/{operation_id}/receipt.json",mime_type="application/json")
    def receipt_resource(operation_id: str) -> str:
        return read_receipt(get_settings().state_dir,operation_id)

    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    create_server(Settings.load(args.config) if args.config else None).run(transport="stdio")


if __name__ == "__main__":
    main()
