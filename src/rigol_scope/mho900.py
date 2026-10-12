"""MHO900 programming guide PGA46002-1110: query-only P1 driver."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from .config import Device
from .errors import IdentityError, ProtocolError
from .transport import TCPTransport

OPTIONS = ("BND", "AFG100", "AFG50", "AUDio", "CAN-FD", "FLEX", "AERO", "RLU-05", "BWU03T05", "BWU03T08", "BWU05T08")


def number(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise ProtocolError(f"Expected numeric response, got {value!r}") from exc
    if not math.isfinite(result) or abs(result) >= 9e36:
        raise ProtocolError(f"Invalid/unavailable numeric response: {value!r}")
    return result


def boolean(value: str) -> bool:
    if value.upper() not in {"0", "1", "ON", "OFF"}:
        raise ProtocolError(f"Expected Boolean response, got {value!r}")
    return value.upper() in {"1", "ON"}


@dataclass(frozen=True)
class Identity:
    manufacturer: str
    model: str
    serial: str
    firmware: str

    @classmethod
    def read(cls, transport: TCPTransport, expected: Device) -> "Identity":
        fields = transport.query("*IDN?").strip().split(",")
        if len(fields) != 4 or any(not p.strip() for p in fields):
            raise IdentityError("Malformed instrument identity")
        result = cls(*(p.strip() for p in fields))
        if not result.manufacturer.upper().startswith("RIGOL") or result.model.upper() != expected.model:
            raise IdentityError(f"Expected RIGOL {expected.model}; received {result.manufacturer} {result.model}")
        if expected.serial and result.serial != expected.serial:
            raise IdentityError("Instrument serial does not match configured identity")
        return result


class MHO900:
    def __init__(self, transport: TCPTransport):
        self.transport = transport

    def options(self) -> dict[str, bool]:
        return {name: boolean(self.transport.query(f":SYSTem:OPTion:STATus? {name}")) for name in OPTIONS}

    def status(self) -> dict:
        q = self.transport.query
        acquisition = {
            "trigger_state_before": q(":TRIGger:STATus?"),
            "trigger_mode": q(":TRIGger:MODE?"),
            "trigger_sweep": q(":TRIGger:SWEep?"),
            "type": q(":ACQuire:TYPE?"),
            "sample_rate_sa_s": number(q(":ACQuire:SRATe?")),
            "memory_depth_response": q(":ACQuire:MDEPth?"),
            "time_scale_s_div": number(q(":TIMebase:MAIN:SCALe?")),
            "time_offset_s": number(q(":TIMebase:MAIN:OFFSet?")),
        }
        channels = []
        for channel in range(1, 5):
            prefix = f":CHANnel{channel}"
            channels.append({
                "channel": channel,
                "enabled": boolean(q(prefix + ":DISPlay?")),
                "coupling": q(prefix + ":COUPling?"),
                "impedance_setting": q(prefix + ":IMPedance?"),
                "probe_ratio": number(q(prefix + ":PROBe?")),
                "scale_v_div": number(q(prefix + ":SCALe?")),
                "offset_v": number(q(prefix + ":OFFSet?")),
                "bandwidth_limit": q(prefix + ":BWLimit?"),
            })
        acquisition["trigger_state_after"] = q(":TRIGger:STATus?")
        return {"acquisition": acquisition, "channels": channels,
                "consistency": "Sequential query snapshot; front-panel/other-host changes cannot be excluded."}

    def generator_status(self, options: dict[str, bool]) -> dict:
        available = options["AFG100"] or options["AFG50"] or options["BND"]
        if not available:
            return {"available": False, "channels": [], "reason": "No AFG/BND license reported by instrument"}
        q = self.transport.query
        channels = []
        for channel in (1, 2):
            prefix = f":SOURce{channel}"
            channels.append({
                "channel": channel,
                "output_enabled": boolean(q(prefix + ":OUTPut:STATe?")),
                "waveform": q(prefix + ":FUNCtion?"),
                "frequency_hz": number(q(prefix + ":FREQuency?")),
                "amplitude_vpp": number(q(prefix + ":VOLTage:AMPLitude?")),
                "offset_v": number(q(prefix + ":VOLTage:OFFSet?")),
                "phase_deg": number(q(prefix + ":PHASe?")),
                "load_setting": q(prefix + ":IMPedance?"),
                "modulation_enabled": boolean(q(prefix + ":MOD:STATe?")),
                "modulation_type": q(prefix + ":MOD:TYPe?"),
            })
        return {"available": True, "channels": channels,
                "fixed_source_impedance_ohm": 50,
                "note": "Load setting controls displayed voltage convention; output impedance remains 50 ohm."}

    def screenshot(self) -> bytes:
        return self.transport.query_binary(":DISPlay:DATA? PNG")
