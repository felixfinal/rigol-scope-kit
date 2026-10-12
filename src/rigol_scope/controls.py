"""MHO900 validated, bounded controls. All configurations return actual readback."""
from __future__ import annotations

import hashlib
import json
import math
import time

from .errors import ScopeError
from .mho900 import MHO900, boolean, number

MEASUREMENTS = {
    "frequency": ("FREQuency", "Hz"), "period": ("PERiod", "s"),
    "vpp": ("VPP", "V"), "vmax": ("VMAX", "V"), "vmin": ("VMIN", "V"),
    "mean": ("VAVG", "V"), "rms": ("VRMS", "V"), "duty": ("PDUTy", "%"),
    "negative_duty": ("NDUTy", "%"), "rise_time": ("RTIMe", "s"), "fall_time": ("FTIMe", "s"),
    "positive_width": ("PWIDth", "s"), "negative_width": ("NWIDth", "s"),
}
TIMING = {"rr_delay": ("RRDelay", "s"), "rf_delay": ("RFDelay", "s"),
          "fr_delay": ("FRDelay", "s"), "ff_delay": ("FFDelay", "s"),
          "rr_phase": ("RRPHase", "deg"), "rf_phase": ("RFPHase", "deg"),
          "fr_phase": ("FRPHase", "deg"), "ff_phase": ("FFPHase", "deg")}
WAVES = {"SIN": "SINusoid", "SQU": "SQUare", "RAMP": "RAMP", "DC": "DC", "NOIS": "NOISe"}


def finite(value, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ScopeError(f"{name} must be finite and between {low} and {high}")
    return float(value)


def channel_id(channel: int, maximum=4):
    if type(channel) is not int or not 1 <= channel <= maximum:
        raise ScopeError(f"channel must be an integer from 1 to {maximum}")


def enum(value, name, choices):
    if not isinstance(value, str) or value not in choices:
        raise ScopeError(f"{name} must be one of {list(choices)}")
    return value


def fields(settings: dict, allowed):
    if not isinstance(settings, dict) or not settings or set(settings) - set(allowed):
        raise ScopeError(f"Expected nonempty settings using fields {list(allowed)}")


def same(expected, actual):
    if isinstance(expected, (float, int)) and not isinstance(expected, bool):
        return isinstance(actual, (float, int)) and math.isclose(expected, actual, rel_tol=1e-6, abs_tol=1e-12)
    return expected == actual


def config_result(before, requested, actual):
    mismatch = {k: {"requested": v, "actual": actual.get(k)} for k, v in requested.items() if not same(v, actual.get(k))}
    return {"before": before, "requested": requested, "actual": actual, "verified": not mismatch, "mismatches": mismatch}


def settings_hash(settings):
    signal = {k: v for k, v in settings.items() if k not in {"output_enabled", "settings_hash"}}
    return hashlib.sha256(json.dumps(signal, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Controls(MHO900):
    def generator_status(self, options):
        if not (options["AFG100"] or options["AFG50"] or options["BND"]):
            return {"available": False, "channels": []}
        return {"available": True, "channels": [{"channel": n, **self.generator_channel(n)} for n in (1, 2)],
                "fixed_source_impedance_ohm": 50, "note": "load is the voltage display convention, not physical source impedance"}

    def apply(self, commands):
        for command in commands:
            self.transport.write(command)
        if self.transport.query("*OPC?") != "1":
            raise ScopeError("Instrument did not report operation complete; inspect actual settings")

    def channel(self, channel):
        channel_id(channel)
        p = f":CHANnel{channel}"
        q = self.transport.query
        imp = q(p + ":IMPedance?")
        return {"enabled": boolean(q(p + ":DISPlay?")), "probe_ratio": number(q(p + ":PROBe?")),
                "impedance": "50" if imp.upper().startswith("FIFT") else "1M" if imp == "OMEG" else imp,
                "coupling": q(p + ":COUPling?"), "scale_v_div": number(q(p + ":SCALe?")),
                "offset_v": number(q(p + ":OFFSet?")), "bandwidth_limit": q(p + ":BWLimit?")}

    def configure_channel(self, channel, settings):
        channel_id(channel)
        fields(settings, ["enabled", "probe_ratio", "impedance", "coupling", "scale_v_div", "offset_v", "bandwidth_limit"])
        before = self.channel(channel)
        target = before | settings
        if type(target["enabled"]) is not bool:
            raise ScopeError("enabled must be Boolean")
        ratio = finite(target["probe_ratio"], "probe_ratio", .001, 50000)
        imp = enum(target["impedance"], "impedance", ["1M", "50"])
        enum(target["coupling"], "coupling", ["DC", "AC", "GND"])
        enum(target["bandwidth_limit"], "bandwidth_limit", ["OFF", "20M", "250M"])
        finite(target["scale_v_div"], "scale_v_div", (.001 if imp == "1M" else .0002) * ratio, (10 if imp == "1M" else 1) * ratio)
        finite(target["offset_v"], "offset_v", -100 * ratio, 100 * ratio)
        p = f":CHANnel{channel}"
        mapping = {"probe_ratio": "PROBe", "impedance": "IMPedance", "coupling": "COUPling", "scale_v_div": "SCALe", "offset_v": "OFFSet", "bandwidth_limit": "BWLimit", "enabled": "DISPlay"}
        def value(k):
            v = settings[k]
            if k == "impedance": return "OMEG" if v == "1M" else "FIFTy"
            if k == "enabled": return "1" if v else "0"
            return str(v)
        # Probe and input changes can asynchronously rescale the channel. Wait
        # and read back each such change before applying the final range.
        current = before
        intermediate = []
        for key in ("probe_ratio", "impedance", "coupling", "enabled"):
            if key not in settings or same(settings[key], current[key]):
                continue
            self.apply([f"{p}:{mapping[key]} {value(key)}"])
            current = self.channel(channel)
            intermediate.append({"field": key, "actual": current})
            if not same(settings[key], current[key]):
                result = config_result(before, settings, current)
                result["intermediate_readbacks"] = intermediate
                return result
        commands = [f"{p}:{mapping[k]} {value(k)}"
                    for k in ("scale_v_div", "offset_v", "bandwidth_limit")
                    if k in settings and not same(settings[k], current[k])]
        if commands:
            self.apply(commands)
            current = self.channel(channel)
        result = config_result(before, settings, current)
        result["intermediate_readbacks"] = intermediate
        return result

    def timebase(self):
        q = self.transport.query
        return {"scale_s_div": number(q(":TIMebase:MAIN:SCALe?")), "offset_s": number(q(":TIMebase:MAIN:OFFSet?"))}

    def configure_timebase(self, settings):
        fields(settings, ["scale_s_div", "offset_s"])
        before = self.timebase()
        target = before | settings
        scale = finite(target["scale_s_div"], "scale_s_div", 5e-10, 500)
        finite(target["offset_s"], "offset_s", -5 * scale, max(1, 100 * scale))
        mapping = {"scale_s_div": "SCALe", "offset_s": "OFFSet"}
        self.apply([f":TIMebase:MAIN:{mapping[k]} {v}" for k, v in settings.items()])
        return config_result(before, settings, self.timebase())

    def trigger_options(self):
        q=self.transport.query
        return {"coupling":q(":TRIGger:COUPling?"),
                "noise_reject":boolean(q(":TRIGger:NREJect?")),
                "holdoff_s":number(q(":TRIGger:HOLDoff?"))}

    def _trigger_option_commands(self, settings, mode, before):
        fields(settings,["coupling","noise_reject","holdoff_s"])
        if "coupling" in settings:
            if mode!="EDGE":raise ScopeError("Trigger coupling is supported only for analog EDGE trigger")
            enum(settings["coupling"],"trigger coupling",["AC","DC","LFR","HFR"])
        if "noise_reject" in settings and type(settings["noise_reject"]) is not bool:
            raise ScopeError("noise_reject must be Boolean")
        if "holdoff_s" in settings:finite(settings["holdoff_s"],"holdoff_s",8e-9,10)
        if mode not in {"EDGE","PULS"}:raise ScopeError("Trigger options currently support EDGE/PULS workflows only")
        commands=[]
        for key,value in settings.items():
            if same(value,before[key]):continue
            if key=="coupling":commands.append(":TRIGger:COUPling "+{"AC":"AC","DC":"DC","LFR":"LFReject","HFR":"HFReject"}[value])
            elif key=="noise_reject":commands.append(f":TRIGger:NREJect {int(value)}")
            else:commands.append(f":TRIGger:HOLDoff {value}")
        return commands

    def configure_trigger_options(self, settings):
        before=self.trigger_options();mode=self.transport.query(":TRIGger:MODE?")
        commands=self._trigger_option_commands(settings,mode,before)
        source=self.transport.query(":TRIGger:"+("EDGE" if mode=="EDGE" else "PULSe")+":SOURce?")
        if source not in {"CHAN1","CHAN2","CHAN3","CHAN4"}:raise ScopeError("Trigger options require an analog trigger source")
        if commands:self.apply(commands)
        return config_result(before,settings,self.trigger_options())

    def trigger(self):
        q = self.transport.query
        return {"mode": q(":TRIGger:MODE?"), "sweep": q(":TRIGger:SWEep?"),
                "source": q(":TRIGger:EDGE:SOURce?"), "slope": q(":TRIGger:EDGE:SLOPe?"),
                "level_v": number(q(":TRIGger:EDGE:LEVel?")), **self.trigger_options()}

    def configure_trigger(self, channel, level_v, slope="POS", sweep="AUTO", coupling=None, noise_reject=None, holdoff_s=None):
        channel_id(channel)
        enum(slope, "slope", ["POS", "NEG", "RFAL"])
        enum(sweep, "sweep", ["AUTO", "NORM", "SING"])
        chan = self.channel(channel)
        finite(level_v, "level_v", -4.5 * chan["scale_v_div"] - chan["offset_v"], 4.5 * chan["scale_v_div"] - chan["offset_v"])
        before = self.trigger()
        options={k:v for k,v in {"coupling":coupling,"noise_reject":noise_reject,"holdoff_s":holdoff_s}.items() if v is not None}
        option_commands=self._trigger_option_commands(options,"EDGE",before) if options else []
        requested = {"mode": "EDGE", "source": f"CHAN{channel}", "level_v": level_v, "slope": slope, "sweep": sweep,**options}
        self.apply([":TRIGger:MODE EDGE", f":TRIGger:EDGE:SOURce CHANnel{channel}", f":TRIGger:EDGE:SLOPe {slope}", f":TRIGger:EDGE:LEVel {level_v}", f":TRIGger:SWEep {sweep}"]+option_commands)
        return config_result(before, requested, self.trigger())

    def acquisition_control(self, action, wait_s=0):
        enum(action, "action", ["run", "stop", "single"])
        finite(wait_s, "wait_s", 0, 60)
        if action != "single" and wait_s:
            raise ScopeError("wait_s applies only to single")
        deadline = getattr(self.transport, "deadline", None)
        if wait_s and deadline is not None and wait_s > deadline - time.monotonic() - 2:
            raise ScopeError("wait_s exceeds the remaining operation budget; reserve time for STOP and cleanup")
        before = self.transport.query(":TRIGger:STATus?")
        if action == "single" and before == "STOP":
            # Establish non-STOP first so a stale stopped record cannot satisfy
            # the completion poll immediately after re-arming.
            ready = self.acquisition_control("run")
            if not ready["verified"]: raise ScopeError("Could not establish RUN before SINGLE")
        self.transport.write({"run": ":RUN", "stop": ":STOP", "single": ":SINGle"}[action])
        until = time.monotonic() + wait_s
        state = self.transport.query(":TRIGger:STATus?")
        settle_until = time.monotonic() + .5
        while action in {"run", "stop"} and ((action == "run" and state == "STOP") or (action == "stop" and state != "STOP")) and time.monotonic() < settle_until:
            self.transport.remaining()
            time.sleep(.02)
            state = self.transport.query(":TRIGger:STATus?")
        while action == "single" and state != "STOP" and time.monotonic() < until:
            self.transport.remaining()
            time.sleep(min(.05, max(0, until - time.monotonic())))
            state = self.transport.query(":TRIGger:STATus?")
        timed_out = action == "single" and wait_s > 0 and state != "STOP"
        if timed_out:
            self.transport.write(":STOP")
            state = self.transport.query(":TRIGger:STATus?")
        verified = not timed_out and ((action == "stop" and state == "STOP") or (action == "run" and state != "STOP") or (action == "single" and (wait_s == 0 or state == "STOP")))
        return {"before": before, "action": action, "state": state, "verified": verified, "trigger_timeout": timed_out,
                "note": "SINGLE returning STOP requires signal validation before claiming the intended event was captured."}

    def measure(self, channels, measurements):
        if not channels or len(channels) > 4 or len(set(channels)) != len(channels):
            raise ScopeError("Choose 1–4 unique channels")
        for channel in channels: channel_id(channel)
        if not measurements or len(measurements) > 12:
            raise ScopeError("Choose 1–12 measurements")
        for m in measurements: enum(m, "measurement", MEASUREMENTS)
        results = []
        for channel in channels:
            for m in measurements:
                command, unit = MEASUREMENTS[m]
                raw = self.transport.query(f":MEASure:ITEM? {command},CHANnel{channel}")
                try: value = number(raw)
                except ScopeError: value = None
                if value is not None and m in {"duty", "negative_duty"}:
                    # MHO900 :MEAS:ITEM? PDUT/NDUT returns a fraction, not percent.
                    value = value * 100 if 0 <= value <= 1 else None
                results.append({"channel": channel, "measurement": m, "value": value, "unit": unit, "valid": value is not None, "raw": raw})
        return {"measurements": results, "source": "instrument; sequential snapshot"}

    def acquisition_settings(self):
        q = self.transport.query
        depth = q(":ACQuire:MDEPth?")
        return {"type": q(":ACQuire:TYPE?"), "averages": int(number(q(":ACQuire:AVERages?"))),
                "memory_depth": "AUTO" if depth == "AUTO" else int(number(depth))}

    def pulse_trigger(self):
        q = self.transport.query
        return {"mode": q(":TRIGger:MODE?"), "sweep": q(":TRIGger:SWEep?"),
                "source": q(":TRIGger:PULSe:SOURce?"), "polarity": q(":TRIGger:PULSe:POLarity?"),
                "when": q(":TRIGger:PULSe:WHEN?"), "lower_s": number(q(":TRIGger:PULSe:LWIDth?")),
                "upper_s": number(q(":TRIGger:PULSe:UWIDth?")), "level_v": number(q(":TRIGger:PULSe:LEVel?")), **{k:v for k,v in self.trigger_options().items() if k!="coupling"}}

    def configure_pulse_trigger(self, channel, level_v, condition="greater", width_s=None,
                                lower_s=None, upper_s=None, polarity="POS", sweep="NORM", noise_reject=None, holdoff_s=None):
        channel_id(channel)
        enum(condition, "condition", ["greater", "less", "between"])
        enum(polarity, "polarity", ["POS", "NEG"])
        enum(sweep, "sweep", ["AUTO", "NORM", "SING"])
        if condition == "between":
            if width_s is not None: raise ScopeError("between uses lower_s/upper_s, not width_s")
            low, high = lower_s, upper_s
        elif condition == "greater":
            low, high = width_s, 10 if upper_s is None else upper_s
        else:
            low, high = 1e-9 if lower_s is None else lower_s, width_s
        finite(low, "lower_s", 1e-9, 10)
        finite(high, "upper_s", 1e-9, 10)
        if low >= high: raise ScopeError("Pulse lower bound must be smaller than upper bound")
        ch = self.channel(channel)
        finite(level_v, "level_v", -4.5 * ch["scale_v_div"] - ch["offset_v"], 4.5 * ch["scale_v_div"] - ch["offset_v"])
        before = self.pulse_trigger()
        options={k:v for k,v in {"noise_reject":noise_reject,"holdoff_s":holdoff_s}.items() if v is not None}
        option_commands=self._trigger_option_commands(options,"PULS",before) if options else []
        when = {"greater": "GRE", "less": "LESS", "between": "GLES"}[condition]
        requested = {"mode":"PULS", "sweep":sweep, "source":f"CHAN{channel}", "polarity":polarity,
                     "when":when, "lower_s":low, "upper_s":high, "level_v":level_v,**options}
        self.apply([":TRIGger:MODE PULSe", f":TRIGger:PULSe:SOURce CHANnel{channel}",
                    f":TRIGger:PULSe:POLarity {polarity}", ":TRIGger:PULSe:WHEN GLES",
                    ":TRIGger:PULSe:UWIDth 10", ":TRIGger:PULSe:LWIDth 1e-9",
                    f":TRIGger:PULSe:UWIDth {high}", f":TRIGger:PULSe:LWIDth {low}",
                    f":TRIGger:PULSe:WHEN {when}", f":TRIGger:PULSe:LEVel {level_v}",
                    f":TRIGger:SWEep {sweep}"]+option_commands)
        return config_result(before, requested, self.pulse_trigger())

    def configure_acquisition(self, settings):
        fields(settings, ["type", "averages", "memory_depth"])
        before = self.acquisition_settings()
        target = before | settings
        enum(target["type"], "type", ["NORM", "AVER", "PEAK", "HRES"])
        averages = target["averages"]
        if type(averages) is not int or averages < 2 or averages > 65536 or averages & (averages - 1):
            raise ScopeError("averages must be a power of two from 2 to 65536")
        depth = target["memory_depth"]
        if depth != "AUTO":
            if type(depth) is not int or depth not in {1000,10000,100000,1000000,10000000,25000000,50000000,100000000,125000000,200000000,250000000,500000000}:
                raise ScopeError("Unsupported memory_depth")
            count = sum(boolean(self.transport.query(f":CHANnel{n}:DISPlay?")) for n in range(1, 5))
            if count == 0: raise ScopeError("Enable an analog channel before choosing memory depth")
            maximum = (500000000 if self.options()["RLU-05"] else 100000000) // (1 if count == 1 else 2 if count == 2 else 4)
            if depth > maximum: raise ScopeError("Memory depth exceeds enabled-channel/license limit")
        mapping = {"type": "TYPE", "averages": "AVERages", "memory_depth": "MDEPth"}
        auto_requested = settings.get("memory_depth")=="AUTO"
        prior_errors=[]
        def read_errors():
            errors=[]
            for _ in range(16):
                response=self.transport.query(":SYSTem:ERRor?")
                try:code=int(response.split(",",1)[0])
                except ValueError:raise ScopeError("Invalid SCPI error queue response")
                if code==0:return errors
                errors.append(response)
            raise ScopeError("SCPI error queue did not clear within 16 entries")
        if auto_requested:prior_errors=read_errors()
        self.apply([f":ACQuire:{command} {settings[k]}" for k, command in mapping.items() if k in settings])
        actual=self.acquisition_settings()
        if not auto_requested:return config_result(before, settings, actual)
        command_errors=read_errors()
        result=config_result(before,{k:v for k,v in settings.items() if k!="memory_depth"},actual)
        depth_valid=actual["memory_depth"]=="AUTO" or (isinstance(actual["memory_depth"],int) and 0<actual["memory_depth"]<=500000000)
        result.update(requested=settings,verified=result["verified"] and not command_errors and depth_valid,
                      memory_depth_selection_requested="AUTO",effective_memory_depth=actual["memory_depth"],
                      prior_scpi_errors=prior_errors,scpi_errors=command_errors,
                      verification="AUTO command completion, empty new SCPI error queue and effective depth readback; mode latch is not independently queryable")
        return result

    def measure_timing(self, channel_a, channel_b, measurement):
        channel_id(channel_a); channel_id(channel_b)
        if channel_a == channel_b: raise ScopeError("Timing requires two different channels")
        enum(measurement, "measurement", TIMING)
        command, unit = TIMING[measurement]
        raw = self.transport.query(f":MEASure:ITEM? {command},CHANnel{channel_a},CHANnel{channel_b}")
        try: value = number(raw)
        except ScopeError: value = None
        return {"channel_a": channel_a, "channel_b": channel_b, "measurement": measurement, "value": value, "unit": unit, "valid": value is not None, "raw": raw}

    def generator_channel(self, channel):
        channel_id(channel, 2)
        q = self.transport.query
        p = f":SOURce{channel}"
        load = q(p + ":IMPedance?")
        result = {"waveform": q(p + ":FUNCtion?"), "output_enabled": boolean(q(p + ":OUTPut:STATe?")),
                  "frequency_hz": number(q(p + ":FREQuency?")), "amplitude_vpp": number(q(p + ":VOLTage:AMPLitude?")),
                  "offset_v": number(q(p + ":VOLTage:OFFSet?")), "phase_deg": number(q(p + ":PHASe?")),
                  "load": "50" if load.upper().startswith("FIFT") else "highz" if load == "OMEG" else load,
                  "modulation_enabled": boolean(q(p + ":MOD:STATe?"))}
        if result["waveform"] == "SQU": result["duty_percent"] = number(q(p + ":FUNCtion:SQUare:DUTY?"))
        if result["waveform"] == "RAMP": result["symmetry_percent"] = number(q(p + ":FUNCtion:RAMP:SYMMetry?"))
        result["settings_hash"] = settings_hash(result)
        return result

    def configure_generator(self, channel, settings):
        channel_id(channel, 2)
        fields(settings, ["waveform", "frequency_hz", "amplitude_vpp", "offset_v", "phase_deg", "load", "duty_percent", "symmetry_percent"])
        options = self.options()
        if not (options["AFG100"] or options["AFG50"] or options["BND"]): raise ScopeError("AFG option not active")
        before = self.generator_channel(channel)
        if before["output_enabled"]: raise ScopeError("Turn this output off explicitly before configuring it")
        if before["modulation_enabled"]: raise ScopeError("Active modulation requires the later modulation workflow")
        target = before | settings
        wave = enum(target["waveform"], "waveform", WAVES)
        load = enum(target["load"], "load", ["highz", "50"])
        high = options["AFG100"] or options["BND"]
        fmax = {"SIN": 100e6 if high else 50e6, "SQU": 20e6 if high else 10e6, "RAMP": 2e6 if high else 1e6, "DC": 100e6, "NOIS": 100e6}[wave]
        if wave in {"DC", "NOIS"} and "frequency_hz" in settings: raise ScopeError("DC/noise has no output frequency parameter")
        freq = finite(target["frequency_hz"], "frequency_hz", .002 if high else .02, fmax)
        max_amp = (20 if freq <= 50e6 else 10) / (2 if load == "50" else 1)
        if wave == "NOIS": max_amp = 5 if load == "50" else 10
        amp = finite(target["amplitude_vpp"], "amplitude_vpp", .001 if load == "50" else .002, max_amp)
        off_limit = (10 if load == "highz" else 5) if wave == "DC" else (max_amp - amp) / 2
        finite(target["offset_v"], "offset_v", -off_limit, off_limit)
        finite(target["phase_deg"], "phase_deg", 0, 360)
        if "duty_percent" in settings:
            if wave != "SQU": raise ScopeError("duty_percent requires square wave")
            finite(settings["duty_percent"], "duty_percent", 1, 99)
        if "symmetry_percent" in settings:
            if wave != "RAMP": raise ScopeError("symmetry_percent requires ramp")
            finite(settings["symmetry_percent"], "symmetry_percent", 0, 100)
        p = f":SOURce{channel}"
        # Output is off. Set load then remove offset before increasing amplitude.
        commands = [f"{p}:IMPedance {'OMEG' if load == 'highz' else 'FIFTy'}", f"{p}:VOLTage:OFFSet 0", f"{p}:FUNCtion {WAVES[wave]}"]
        if wave not in {"DC", "NOIS"}: commands.append(f"{p}:FREQuency {freq}")
        commands += [f"{p}:VOLTage:AMPLitude {amp}", f"{p}:VOLTage:OFFSet {target['offset_v']}", f"{p}:PHASe {target['phase_deg']}"]
        if "duty_percent" in settings: commands.append(f"{p}:FUNCtion:SQUare:DUTY {settings['duty_percent']}")
        if "symmetry_percent" in settings: commands.append(f"{p}:FUNCtion:RAMP:SYMMetry {settings['symmetry_percent']}")
        self.apply(commands)
        actual = self.generator_channel(channel)
        return config_result(before, settings | {"output_enabled": False}, actual)

    def generator_output(self, channel, enabled, expected_settings_hash=None, max_open_circuit_v=None):
        channel_id(channel, 2)
        if type(enabled) is not bool: raise ScopeError("enabled must be Boolean")
        before = self.generator_channel(channel)
        if enabled:
            if not expected_settings_hash or expected_settings_hash != before["settings_hash"]:
                raise ScopeError("Read/configure AFG first and supply its current settings_hash")
            if before["modulation_enabled"] or before["waveform"] not in WAVES:
                raise ScopeError("P2 output enable supports unmodulated basic waveforms only")
            limit = finite(max_open_circuit_v, "max_open_circuit_v", 0, 10)
            peak = abs(before["offset_v"]) + (0 if before["waveform"] == "DC" else before["amplitude_vpp"] / 2)
            if before["load"] == "50": peak *= 2
            if peak > limit + 1e-9: raise ScopeError(f"Open-circuit peak {peak:g} V exceeds requested bound {limit:g} V")
        self.apply([f":SOURce{channel}:OUTPut:STATe {int(enabled)}"])
        actual = self.generator_channel(channel)
        return config_result(before, {"output_enabled": enabled}, actual)

    def power_status(self):
        return {"low_power": boolean(self.transport.query(":SYSTem:LOWPower?")), "read_only": True}

    def wake(self, wait_s=10):
        finite(wait_s, "wait_s", .1, 15)
        before = self.power_status()
        if not before["low_power"]:
            return {"before": before, "actual": before, "verified": True, "writes_sent": 0}
        self.transport.write(":SYSTem:LOWPower 0")
        deadline = min(time.monotonic()+wait_s, self.transport.deadline-2)
        actual = before
        while time.monotonic()<deadline:
            actual = self.power_status()
            if not actual["low_power"]:
                self.apply([])
                return {"before":before,"actual":actual,"verified":True,"writes_sent":1,
                        "note":"Power state and operation completion verified; previous RAM waveforms are not restored."}
            time.sleep(.25)
        return {"before":before,"actual":actual,"verified":False,"writes_sent":1,
                "error":"Wake did not report active state within bounded wait"}

    def release(self, keep_generator_outputs=None):
        keep = [] if keep_generator_outputs is None else keep_generator_outputs
        if not isinstance(keep,list) or any(type(x) is not int or x not in {1,2} for x in keep) or len(set(keep))!=len(keep):
            raise ScopeError("keep_generator_outputs must contain unique channels 1/2 with an ongoing output requirement")
        result={"verified":False,"keep_generator_outputs":keep,"steps":[],"errors":[],
                "note":"Explicit task release, not process-exit cleanup or a cross-host lease. Unused AFG outputs off; memory AUTO; other settings retained."}
        if self.power_status()["low_power"]:
            result["errors"].append("Instrument is in low power; explicit wake required before release readbacks")
            return result
        options=self.options()
        if options["AFG100"] or options["AFG50"] or options["BND"]:
            for channel in (1,2):
                if channel in keep:continue
                try:
                    before=self.generator_channel(channel)
                    if before["output_enabled"]:
                        step=self.generator_output(channel,False)
                    else:
                        step={"verified":True,"actual":before,"writes_sent":0}
                    result["steps"].append({"operation":"generator_off","channel":channel,"result":step})
                    if not step["verified"]:result["errors"].append(f"AFG{channel} output-off readback mismatch")
                except ScopeError as exc:result["errors"].append(f"AFG{channel}: {exc}")
        try:
            before=self.acquisition_settings()
            step=({"verified":True,"actual":before,"writes_sent":0} if before["memory_depth"]=="AUTO"
                  else self.configure_acquisition({"memory_depth":"AUTO"}))
            result["steps"].append({"operation":"memory_auto","result":step})
            if not step["verified"]:result["errors"].append("Memory AUTO readback mismatch")
        except ScopeError as exc:result["errors"].append(f"Memory AUTO: {exc}")
        result["verified"]=not result["errors"]
        return result

    def synchronize_phase(self):
        self.apply([":SOURce1:PHASe:SYNChronize"])
        return {"verified": True, "channels": [self.generator_channel(1), self.generator_channel(2)],
                "verification": "command completion and setting readback; physical phase requires two-channel measurement"}
