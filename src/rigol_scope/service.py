from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
import time
import uuid

from . import __version__
from .artifacts import save_screenshot
from .config import Settings
from .errors import IdentityError, ScopeError, MeasurementPreflightError
from .locking import device_lock
from .mho900 import Identity
from .controls import Controls, channel_id, finite
from .waveforms import export_waveforms
from .transport import TCPTransport
from .contracts import CONTRACT_VERSION, MeasurementRequest
from .profiles import get_profile
from .measurement import snapshot, resolve, quality_report, execute


class ScopeService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _audit(self, event: dict):
        directory = self.settings.state_dir / "events"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        (directory / (event["operation_id"] + ".json")).write_text(json.dumps(event, indent=2) + "\n")

    @contextmanager
    def session(self, alias: str, operation: str, read_only: bool = True, operation_timeout_s=None, wake_before_identity=False, reservation_owner=False, lock_timeout_s=None):
        device = self.settings.device(alias)
        if operation != "identify" and not device.serial:
            raise IdentityError("First run identify, then pin the returned serial in device configuration")
        if not reservation_owner:
            from .reservations import assert_unreserved
            assert_unreserved(self.settings,device)
        started = time.monotonic()
        budget = device.operation_timeout_s if operation_timeout_s is None else finite(operation_timeout_s,"operation_timeout_s",5,900)
        deadline = started + budget
        event = {"operation_id": uuid.uuid4().hex, "device": alias, "operation": operation,
                 "started_at": datetime.now(timezone.utc).isoformat(), "read_only": read_only,"operation_timeout_s":budget}
        try:
            # Serial lock is acquired before network access; endpoint lock also
            # coordinates initial unpinned identity reads on the same hostname.
            key = "serial:" + device.serial if device.serial else "unbound:" + device.host.lower()
            with device_lock(self.settings.state_dir / "locks", key, deadline if lock_timeout_s is None else min(deadline,started+lock_timeout_s)):
                with device_lock(self.settings.state_dir / "locks", f"endpoint:{device.host.lower()}:{device.port}", deadline if lock_timeout_s is None else min(deadline,started+lock_timeout_s)):
                    with TCPTransport(device, deadline) as transport:
                        if wake_before_identity:
                            # Low power can accept TCP while not answering even *IDN?.
                            # Only this explicit, pinned-target wake bypasses pre-write identity.
                            transport.write(":SYSTem:LOWPower 0")
                            event["wake_sent_before_identity"]=True
                            event["commands"]=transport.commands
                            time.sleep(2)
                        identity = Identity.read(transport, device)
                        event["identity"] = asdict(identity)
                        controller=Controls(transport)
                        controller.operation_id=event["operation_id"]
                        controller.operation_timeout_s=budget
                        try:
                            yield controller, asdict(identity)
                        finally:
                            event["commands"] = transport.commands
                            if hasattr(controller,"reservation_budgets"):event["phase_budgets"]=controller.reservation_budgets
                            if hasattr(controller,"result_verified"):event["verified"]=controller.result_verified
            event["status"] = "failed" if event.get("verified") is False else "complete"
        except Exception as exc:
            event["status"] = "failed"
            event["error_type"] = type(exc).__name__
            event["error"] = str(exc)
            raise
        finally:
            event["elapsed_s"] = round(time.monotonic() - started, 4)
            self._audit(event)

    def identify(self, device: str) -> dict:
        with self.session(device, "identify") as (_, identity):
            return {"identity": identity, "serial_pinned": bool(self.settings.device(device).serial), "read_only": True}

    def capabilities(self, device: str) -> dict:
        with self.session(device, "capabilities") as (driver, identity):
            return {"identity": identity, "options": driver.options(), "read_only": True,
                    "implemented": ["identify", "capabilities", "status", "screenshot", "generator_status", "configuration", "measure", "measure_timing", "generator_output", "waveform_export", "capture_single", "power_status", "wake", "release", "trigger_options", "configure_trigger_options", "prepare_capture", "prepared_status", "prepared_collect", "prepared_cancel", "prepared_generator_output"],
                    "not_implemented": ["decode", "modulation_control", "arbitrary_waveform_upload", "unattended_acquisition_loops"],
                    "offline_tools": ["captures_list","capture_info","waveform_plot","waveform_analyze","waveform_search","waveform_timing"],
                    "offline_limits": {"selected_points":2000000,"dual_total_selected_points":2000000,"time_budget_s":30},
                    "deep_export_limits":{"chunk_points":4000000,"operation_timeout_s":[5,900],"max_bytes":2147483648},
                    "measurement_contract":CONTRACT_VERSION,
                    "measurement_features":["versioned_profiles","read_only_preview","atomic_configuration_capture","caller_owned_adjustment","bounded_prepared_capture","trigger_coupling_noise_holdoff"],
                    "digital_probe": "not_present_per_user; no hardware validation"}

    def wake(self, device, confirmed=False, wait_s=10):
        if confirmed is not True:raise ScopeError("confirmed=True required for explicit wake")
        finite(wait_s,"wait_s",3,15)
        with self.session(device,"wake",read_only=False,operation_timeout_s=wait_s+10,wake_before_identity=True) as (driver,identity):
            driver.apply([])
            actual=driver.power_status()
            driver.result_verified=not actual["low_power"]
            return {"identity":identity,"before":{"low_power":"unqueried; sleep may not answer"},
                    "actual":actual,"verified":driver.result_verified,"wake_command_sent":True,
                    "note":"Explicit wake sent once to pinned endpoint before identity check; no AFG enable or previous RAM recovery."}

    def trigger_options(self,device):
        with self.session(device,"trigger_options") as (driver,identity):
            return {"identity":identity,"read_only":True,"mode":driver.transport.query(":TRIGger:MODE?"),**driver.trigger_options()}

    def prepare_capture(self,device,request,confirmed=False,lease_s=60,ready_wait_s=5,export_timeout_s=30,keep_generator_outputs=None):
        if confirmed is not True:raise ScopeError("confirmed=True required to reserve and arm")
        req,_=self._measurement_request(request)
        from .reservations import prepare
        return prepare(self.settings,device,req.model_dump(mode="json",exclude_none=True),lease_s,ready_wait_s,export_timeout_s,keep_generator_outputs)

    def prepared_status(self,device,reservation_id,reservation_token):
        from .reservations import status
        return status(self.settings,device,reservation_id,reservation_token)

    def prepared_collect(self,device,reservation_id,reservation_token,wait_s=0):
        finite(wait_s,"wait_s",0,10)
        from .reservations import command
        return command(self.settings,device,reservation_id,reservation_token,"collect",wait_s=wait_s)

    def prepared_cancel(self,device,reservation_id,reservation_token,confirmed=False):
        if confirmed is not True:raise ScopeError("confirmed=True required to cancel/clean up")
        from .reservations import command
        return command(self.settings,device,reservation_id,reservation_token,"cancel")

    def prepared_generator_output(self,device,reservation_id,reservation_token,channel,enabled,confirmed=False,expected_settings_hash=None,max_open_circuit_v=None):
        if confirmed is not True:raise ScopeError("confirmed=True required for stimulus output")
        from .reservations import command
        return command(self.settings,device,reservation_id,reservation_token,"generator_output",channel=channel,enabled=enabled,
                       expected_settings_hash=expected_settings_hash,max_open_circuit_v=max_open_circuit_v)

    def power_status(self, device: str) -> dict:
        with self.session(device,"power_status") as (driver,identity):
            return {"identity":identity,**driver.power_status()}

    def status(self, device: str) -> dict:
        with self.session(device, "status") as (driver, identity):
            return {"identity": identity, "read_only": True, **driver.status()}

    def generator_status(self, device: str) -> dict:
        with self.session(device, "generator_status") as (driver, identity):
            options = driver.options()
            return {"identity": identity, "options": options, "read_only": True, **driver.generator_status(options)}

    def screenshot(self, device: str) -> dict:
        with self.session(device, "screenshot") as (driver, identity):
            return save_screenshot(self.settings.state_dir, driver.screenshot(), identity, device)

    def doctor(self, device: str) -> dict:
        return {"version": __version__, "state_dir": str(self.settings.state_dir),
                "transport": "lan-tcp", **self.identify(device),
                "note": "Identity check only; no bandwidth, waveform or hardware output validation."}

    def execute_control(self, device, control_method, confirmed=False, **params):
        if confirmed is not True:
            raise ScopeError("This operation changes instrument state; confirmed=True records the user's current task authorization")
        allowed = {"configure_channel", "configure_timebase", "configure_trigger", "configure_pulse_trigger", "configure_acquisition", "acquisition_control", "configure_generator", "generator_output", "synchronize_phase", "wake", "release", "configure_trigger_options"}
        if control_method not in allowed: raise ScopeError("Unknown control operation")
        with self.session(device, control_method, read_only=False) as (driver, identity):
            result=getattr(driver, control_method)(**params)
            driver.result_verified=result.get("verified",True)
            return {"identity": identity, "read_only": False, **result}

    def measure(self, device, channels, measurements):
        with self.session(device, "measure") as (driver, identity):
            return {"identity": identity, "read_only": True, **driver.measure(channels, measurements)}

    def measure_timing(self, device, channel_a, channel_b, measurement):
        with self.session(device, "measure_timing") as (driver, identity):
            return {"identity": identity, "read_only": True, **driver.measure_timing(channel_a, channel_b, measurement)}

    def waveform_export(self, device, confirmed=False, operation_timeout_s=None, **params):
        if confirmed is not True: raise ScopeError("Waveform export changes transfer settings; current task authorization is required")
        with self.session(device, "waveform_export", read_only=False, operation_timeout_s=operation_timeout_s) as (driver, identity):
            return export_waveforms(driver, self.settings.state_dir, identity, **params)

    def capture_single(self, device, channels, mode="NORM", points=1000, wait_s=10, confirmed=False):
        if confirmed is not True: raise ScopeError("Single capture changes acquisition; current task authorization is required")
        if not channels or len(channels) > 4 or len(set(channels)) != len(channels): raise ScopeError("Choose 1–4 unique channels")
        for channel in channels: channel_id(channel)
        if mode not in {"NORM", "RAW"}: raise ScopeError("Expected NORM or RAW")
        if type(points) is not int or not 1 <= points <= (1000 if mode == "NORM" else 500000000): raise ScopeError("Invalid point count")
        finite(wait_s, "wait_s", .01, 60)
        with self.session(device, "capture_single", read_only=False) as (driver, identity):
            for channel in channels:
                if driver.transport.query(f":CHANnel{channel}:DISPlay?") != "1": raise ScopeError("Requested channel is not enabled")
            depth = driver.transport.query(":ACQuire:MDEPth?")
            if mode == "RAW" and depth != "AUTO" and points > int(float(depth)): raise ScopeError("Requested points exceed configured memory depth")
            if points * len(channels) * 2 + 1048576 > 268435456: raise ScopeError("Single capture exceeds interactive artifact budget; configure/export a smaller window")
            state = driver.acquisition_control("single", wait_s)
            if not state["verified"]:
                return {"identity": identity, "verified": False, "capture": None, "acquisition": state}
            capture = export_waveforms(driver, self.settings.state_dir, identity, channels, mode=mode, points=points)
            return {**capture, "acquisition": state}

    def _measurement_request(self, request):
        try:
            req=request if isinstance(request,MeasurementRequest) else MeasurementRequest.model_validate(request)
            profile=get_profile(self.settings.profiles_dir,req.profile_id,req.profile_version,req.profile_digest)
            return req,profile
        except (ScopeError,ValueError) as exc:
            raise MeasurementPreflightError(str(exc)) from exc

    def _resolve_measurement(self, driver, profile, req, device):
        try:
            before=snapshot(driver)
            return resolve(profile,req,before,driver.options(),device)
        except (ScopeError,ValueError) as exc:
            raise MeasurementPreflightError(str(exc)) from exc

    def measurement_preview(self, device, request):
        req,profile=self._measurement_request(request)
        with self.session(device,"measurement_preview") as (driver,identity):
            plan=self._resolve_measurement(driver,profile,req,self.settings.device(device))
            return {"contract_version":CONTRACT_VERSION,"read_only":True,"instrument_access":True,
                    "valid":True,"identity":identity,**plan,"quality":quality_report(plan),
                    "note":"Preview is not a reservation. Supply plan_digest as preview_digest to reject changed preserved settings."}

    def measurement_capture(self, device, request, confirmed=False, preview_digest=None, operation_timeout_s=None):
        if confirmed is not True:raise ScopeError("confirmed=True records current user authorization for configuration and capture")
        req,profile=self._measurement_request(request)
        with self.session(device,"measurement_capture",read_only=False,operation_timeout_s=operation_timeout_s) as (driver,identity):
            plan=self._resolve_measurement(driver,profile,req,self.settings.device(device))
            if preview_digest is not None and preview_digest!=plan["plan_digest"]:
                raise MeasurementPreflightError("PLAN_CHANGED: preserved settings or request differ from preview; no writes sent")
            result=execute(driver,identity,self.settings.state_dir,plan)
            driver.result_verified=result["verified"]
            return result
