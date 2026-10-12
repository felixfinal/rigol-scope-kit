"""Versioned machine-readable contracts; the caller owns measurement decisions."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator, model_validator

CONTRACT_VERSION = "1.0.0"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ChannelPatch(StrictModel):
    enabled: StrictBool | None = None
    probe_ratio: float | None = Field(default=None, ge=.001, le=50000, description="Probe attenuation setting; must match the physical probe")
    impedance: Literal["1M","50"] | None = None
    coupling: Literal["DC","AC","GND"] | None = None
    scale_v_div: float | None = Field(default=None, gt=0)
    offset_v: float | None = None
    bandwidth_limit: Literal["OFF","20M","250M"] | None = None


class TimebasePatch(StrictModel):
    scale_s_div: float | None = Field(default=None, ge=5e-10, le=500)
    offset_s: float | None = None


class AcquisitionPatch(StrictModel):
    type: Literal["NORM","AVER","PEAK","HRES"] | None = None
    averages: Literal[2,4,8,16,32,64,128,256,512,1024,2048,4096,8192,16384,32768,65536] | None = None
    memory_depth: Literal[1000,10000,100000,1000000,10000000,25000000,50000000,100000000,125000000,200000000,250000000,500000000] | None = Field(default=None, description="Explicit points; AUTO is not supported by the atomic capture contract")


class TriggerPatch(StrictModel):
    coupling: Literal["AC","DC","LFR","HFR"] | None = None
    noise_reject: StrictBool | None = None
    holdoff_s: float | None = Field(default=None, ge=8e-9, le=10)
    kind: Literal["edge","pulse"] | None = None
    channel: StrictInt | Literal["first_selected"] | None = None
    level_v: float | None = None
    slope: Literal["POS","NEG","RFAL"] | None = None
    sweep: Literal["AUTO","NORM","SING"] | None = None
    condition: Literal["greater","less","between"] | None = None
    polarity: Literal["POS","NEG"] | None = None
    width_s: float | None = Field(default=None, ge=1e-9, le=10)
    lower_s: float | None = Field(default=None, ge=1e-9, le=10)
    upper_s: float | None = Field(default=None, ge=1e-9, le=10)


class CapturePatch(StrictModel):
    mode: Literal["NORM","RAW"] | None = None
    points: StrictInt | Literal["record"] | None = Field(default=None, description="'record' exports the whole configured RAW record, or 1000 NORM points")
    start: StrictInt | None = Field(default=None, ge=1, le=500000000)
    chunk_points: StrictInt | None = Field(default=None, ge=1, le=4000000)
    wait_s: float | None = Field(default=None, gt=0, le=10, description="Bounded SINGLE wait; request must fit the device operation deadline")
    max_bytes: StrictInt | None = Field(default=None, ge=1048576, le=2147483648)
    screenshot: StrictBool | None = None


class SettingsPatch(StrictModel):
    channel_defaults: ChannelPatch = Field(default_factory=ChannelPatch)
    channels: dict[str, ChannelPatch] = Field(default_factory=dict)
    timebase: TimebasePatch = Field(default_factory=TimebasePatch)
    acquisition: AcquisitionPatch = Field(default_factory=AcquisitionPatch)
    trigger: TriggerPatch = Field(default_factory=TriggerPatch)
    capture: CapturePatch = Field(default_factory=CapturePatch)

    @field_validator("channels")
    @classmethod
    def channel_keys(cls, value):
        if any(k not in {"1","2","3","4"} for k in value):
            raise ValueError("Channel keys must be strings '1' through '4'")
        return value


class ExpectedChannel(StrictModel):
    frequency_hz: float | None = Field(default=None, gt=0)
    voltage_min_v: float | None = None
    voltage_max_v: float | None = None
    min_event_width_s: float | None = Field(default=None, gt=0)
    min_samples_per_event: StrictInt | None = Field(default=None, ge=2, le=100000)


class ExpectedSignal(StrictModel):
    objective: str = Field(default="", max_length=2048, description="Caller-owned test objective; never interpreted as SCPI")
    channels: dict[str, ExpectedChannel] = Field(default_factory=dict)
    pretrigger_s: float | None = Field(default=None, ge=0)
    posttrigger_s: float | None = Field(default=None, ge=0)

    @field_validator("channels")
    @classmethod
    def channel_keys(cls, value):
        if any(k not in {"1","2","3","4"} for k in value):
            raise ValueError("Expected-signal channel keys must be '1' through '4'")
        return value


class Profile(StrictModel):
    schema_version: Literal[1] = 1
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    title: str = Field(min_length=1, max_length=128)
    description: str = Field(max_length=2048)
    assumptions: list[str] = Field(max_length=16)
    supported_models: list[Literal["MHO934","MHO954","MHO984"]] = Field(min_length=1, max_length=3)
    allowed_capture_channels: list[StrictInt] | None = None
    defaults: SettingsPatch

    @model_validator(mode="after")
    def complete_capture_defaults(self):
        values=self.defaults.capture.model_dump()
        if any(value is None for value in values.values()):
            raise ValueError("Profile must explicitly define every capture default")
        if self.allowed_capture_channels is not None:
            if not self.allowed_capture_channels or len(set(self.allowed_capture_channels))!=len(self.allowed_capture_channels) or any(x not in {1,2,3,4} for x in self.allowed_capture_channels):
                raise ValueError("Invalid allowed_capture_channels")
        return self


class MeasurementRequest(StrictModel):
    contract_version: Literal["1.0.0"]
    profile_id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    profile_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$", description="Digest returned by scope_profile_get; stale references fail before connecting")
    channels: list[StrictInt] = Field(default_factory=lambda:[1], min_length=1, max_length=4)
    overrides: SettingsPatch = Field(default_factory=SettingsPatch)
    expected_signal: ExpectedSignal = Field(default_factory=ExpectedSignal)

    @field_validator("channels")
    @classmethod
    def valid_channels(cls, value):
        if len(set(value)) != len(value) or any(x not in {1,2,3,4} for x in value):
            raise ValueError("Choose 1–4 unique analog channels")
        return value


def get_contract():
    from . import __version__
    return {
        "software_version":__version__,
        "contract_version":CONTRACT_VERSION,
        "instrument_access":False,
        "skill_uri":"scope://skill/SKILL.md",
        "skill_reference_uri":"scope://skill/references/{name}",
        "workflow":["scope_contract","scope_profiles_list","scope_profile_get","scope_measurement_preview (optional)","scope_measurement_capture","caller evaluates and supplies next request"],
        "call_shape":{"preview":{"device":"alias","request":"MeasurementRequest"},
                      "capture":{"device":"alias","request":"MeasurementRequest","confirmed":"current task authorization","preview_digest":"optional digest from preview","operation_timeout_s":"optional 5..900 seconds for deep capture; default device deadline"}},
        "return_contract":{"verified":"configuration/readback/capture success, not DUT correctness",
                           "profile_ref":"id, version and normalized content digest",
                           "parameter_origins":"per-field source: override, profile or live_readback",
                           "configuration_before_arm":"readback immediately before SINGLE",
                           "actual_settings":"final readback after capture (when available)",
                           "quality":"evidence and limitations for caller-supplied expectations",
                           "capture":"saved waveform result or null on pre-capture failure",
                           "screenshot":"saved display evidence or null",
                           "receipt_uri":"persistent success/failure receipt",
                           "application_verdict":"caller_decides"},
        "request_schema":MeasurementRequest.model_json_schema(),
        "profile_schema":Profile.model_json_schema(),
        "precedence":["explicit request overrides","selected profile defaults","fresh live readback for omitted setting fields"],
        "units":{"scale_v_div":"V/div","offset_v":"V","scale_s_div":"s/div","offset_s":"s","memory_depth":"points","pulse_widths":"s"},
        "atomicity":"One shared device lock covers snapshot, validation, configuration, readback, SINGLE, waveform export, screenshot and receipt. This is not rollback atomicity and cannot lock a front panel or another host.",
        "finish_state":"Capture ends STOP with applied settings. At task handoff call scope_release: unused AFG outputs off, memory AUTO, other settings retained. Previous acquired records are not restored.",
        "task_lifecycle":{"power_status":"scope_power_status","wake":"scope_wake confirmed=true, bounded and read back","release":"scope_release confirmed=true; unused outputs off and memory AUTO","keep_outputs":"explicit ongoing task only","automatic_disconnect_cleanup":False,"auto_memory":"handoff default; atomic RAW capture still uses explicit depth"},
        "interaction_limit":"Use scope_prepare_capture for a cross-call stimulus workflow; ordinary measurement_capture remains blocking. Prepared readiness requires observed WAIT, not a guessed delay.",
        "prepared_capture":{"tools":["scope_prepare_capture","scope_prepared_status","scope_prepared_collect","scope_prepared_cancel","scope_prepared_generator_output"],
                            "lease_s":[5,120],"ready_wait_s":[0.1,10],"export_timeout_s":[5,900],
                            "owner":"Short-lived local owner process holds serial/endpoint locks; expires and attempts cleanup even after client disconnect",
                            "credential":"reservation_token stays in the same caller run; pass only public ready information to external stimulus agents",
                            "cleanup":"STOP, unused AFG off, memory AUTO; keep only explicitly ongoing outputs. Network/process faults may leave unknown hardware state; inspect cleanup_complete.",
                            "cancel":"During export, cancellation is requested and applied at the next block boundary. Owner-token explicit recovery handles a crashed owner.",
                            "limits":"Front-panel or other-host STOP/interference cannot be excluded; validate event content. No guarantee of multi-instrument timing or exactly-once external stimulus."},
        "error_contract":{"validation":"No SCPI writes for known invalid/stale requests","readback_mismatch":"Stop before acquisition; return mismatches and applied steps","timeout":"No successful new capture claimed; report cleanup state","transport_loss":"Partial changes may remain; no automatic replay/reconnect"},
        "caller_responsibility":["Supply expected signal and target","Select physical-probe-compatible profile","Decide adjustments from actual settings, capture quality and analysis"],
        "server_responsibility":["Validate version/digest and parameters","Return per-field default origin","Execute once under lock","Report actual values and evidence; no autonomous retuning"],
        "software_support":{"atomic_trigger_kinds":["edge","pulse"],"unsupported":["runt/timeout/setup-hold/protocol trigger workflows","AFG control inside measurement profiles","AUTO memory depth in atomic workflow","unattended or recurring background jobs"]},
        "offline_analysis":{"single_quality":"frequency withheld for unsupported timing; extrema and stable levels separated","dual_tool":"scope_waveform_timing","dual_methods":["sine_fit with known frequency","edge_delay with max_delay_s"],"dual_total_samples":2000000,"instrument_access":False},
        "deep_export":{"max_chunk_points":4000000,"optional_operation_timeout_s":[5,900],"max_bytes":2147483648,"depth_selection":"caller chooses smallest adequate explicit supported depth; maximum is opt-in"},
        "hardware_support":"Instrument licenses and capabilities are queried live during preview/capture; software support is not inferred from licenses.",
        "verification":"Bench verification is scoped to verification_coverage; no all-settings or precision calibration claim.",
        "verification_coverage":{"model":"MHO954","full_raw_points_per_channel":{"1":[100000000,500000000],"2":[100000000],"4":[50000000]},"head_tail_only_max_depth":{"2":250000000,"4":125000000},"data_integrity":"Downloaded source hashes verified; 500M independent boundary/middle/tail windows byte-matched","limits":"Maximum multi-channel whole-record transfer and precision calibration are not claimed; documented in deep-records Skill reference"},
    }
