"""Saved-waveform analysis. No SCPI imports, connections or live device access."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import math
from pathlib import Path
import time

import numpy as np

from .artifacts import artifact_path
from .catalog import load_metadata
from .errors import ScopeError
from .waveforms import Preamble

MAX_POINTS = 2_000_000
MAX_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_EDGES = 100_000
ALGORITHM = "saved-waveform-quality-2"


def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ScopeError(f"{name} must be finite")
    return float(value)


def check_deadline(deadline):
    if time.monotonic() > deadline:
        raise ScopeError("Offline analysis exceeded time budget")


@dataclass
class WaveWindow:
    capture_id: str
    channel: int
    volts: np.ndarray
    dt: float
    t0: float
    first_index: int
    source_sha256: str
    source_bytes: int
    record_points: int
    code_min: int
    code_max: int
    identity: dict = field(default_factory=dict)
    preamble: dict = field(default_factory=dict)
    metadata_sha256: str = ""

    def provenance(self):
        return {"capture_id": self.capture_id, "channel": self.channel,
                "source_sha256": self.source_sha256, "source_bytes": self.source_bytes,
                "hash_verified": True, "record_points": self.record_points,
                "identity":self.identity,"preamble":self.preamble,"metadata_sha256":self.metadata_sha256,
                "selected_points": len(self.volts), "first_sample_index": self.first_index,
                "start_s": self.t0, "end_s": self.t0 + (len(self.volts)-1)*self.dt,
                "sample_interval_s": self.dt, "source_type": "saved_RAW_or_NORM_not_live_measurement"}


def load_window(state_dir: Path, capture_id: str, channel=1, start_s=None, end_s=None,
                deadline=None, max_points=MAX_POINTS) -> WaveWindow:
    deadline = deadline or time.monotonic()+20
    if type(channel) is not int or not 1 <= channel <= 4:
        raise ScopeError("channel must be 1..4")
    meta = load_metadata(state_dir, capture_id)
    if meta["kind"] != "waveform" or meta["status"] != "complete":
        raise ScopeError("Analysis requires a complete waveform capture")
    if (meta.get("format"), meta.get("dtype"), meta.get("byteorder")) != ("WORD", "uint16", "little"):
        raise ScopeError("Only verified little-endian uint16 WORD captures are supported")
    try:
        row = meta["channels"][str(channel)]
        points = row["points_written"]
        chunks = row["chunks"]
        source_start = meta["start_index_one_based"]
        if type(points) is not int or points <= 0 or type(source_start) is not int or source_start < 1:
            raise ValueError("invalid record length/start")
        if not chunks or len(chunks)>10000: raise ValueError("invalid chunks")
        if row["bytes_written"] != 2*points: raise ValueError("byte count mismatch")
        pre = Preamble(**chunks[0]["preamble"])
        if pre.format != 1 or pre.x_increment <= 0 or pre.y_increment <= 0:
            raise ValueError("invalid scaling")
        scale = (pre.x_increment, pre.x_origin, pre.x_reference, pre.y_increment, pre.y_origin, pre.y_reference)
        if not all(math.isfinite(x) for x in scale): raise ValueError("non-finite preamble")
        expected = source_start
        for chunk in chunks:
            cp = Preamble(**chunk["preamble"])
            if chunk["start"] != expected or chunk["stop"] < chunk["start"]:
                raise ValueError("non-contiguous chunks")
            count = chunk["stop"]-chunk["start"]+1
            if cp.points != count or cp.format != 1:
                raise ValueError("chunk length/format mismatch")
            if (cp.x_increment,cp.x_origin,cp.x_reference,cp.y_increment,cp.y_origin,cp.y_reference) != scale:
                raise ValueError("inconsistent chunk scaling")
            expected = chunk["stop"]+1
        if expected-source_start != points: raise ValueError("record/chunk length mismatch")
        digest_expected = row["sha256"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ScopeError(f"Invalid waveform record: {exc}") from exc
    dt = pre.x_increment
    record_t0 = pre.time(source_start-1)
    first, stop = 0, points
    if start_s is not None: first = max(0, math.ceil((finite(start_s,"start_s")-record_t0)/dt-1e-8))
    if end_s is not None: stop = min(points, math.floor((finite(end_s,"end_s")-record_t0)/dt+1e-8)+1)
    if stop-first < 2: raise ScopeError("Selected time interval contains fewer than two samples")
    if type(max_points) is not int or not 1<=max_points<=MAX_POINTS:raise ScopeError("Invalid analysis point budget")
    if stop-first > max_points:
        raise ScopeError(f"Analysis window exceeds {max_points} points; select start_s/end_s (no silent decimation)")
    path = artifact_path(state_dir,capture_id,f"ch{channel}.bin")
    size = path.stat().st_size
    if size != points*2 or size > MAX_SOURCE_BYTES:
        raise ScopeError("Source byte count invalid or exceeds 1 GiB verification budget")
    digest = hashlib.sha256()
    # One open file handle for checksum and selected data; compare fstat around reads.
    with path.open("rb") as stream:
        import os
        before = os.fstat(stream.fileno())
        while block := stream.read(1024*1024):
            digest.update(block)
            check_deadline(deadline)
        if digest.hexdigest() != digest_expected:
            raise ScopeError("Raw waveform SHA-256 mismatch; analysis refused")
        stream.seek(first*2)
        raw = stream.read((stop-first)*2)
        after = os.fstat(stream.fileno())
        if (before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns):
            raise ScopeError("Waveform changed during analysis")
    if len(raw) != (stop-first)*2: raise ScopeError("Truncated selected waveform")
    codes = np.frombuffer(raw,dtype="<u2")
    volts = (codes.astype(np.float64)-pre.y_origin-pre.y_reference)*pre.y_increment
    if not np.isfinite(volts).all() or np.max(np.abs(volts))>1e12: raise ScopeError("Invalid voltage conversion")
    if not math.isfinite(record_t0) or not math.isfinite(record_t0+(points-1)*dt):
        raise ScopeError("Invalid record time axis")
    check_deadline(deadline)
    return WaveWindow(capture_id,channel,volts,dt,record_t0+first*dt,source_start-1+first,
                      digest.hexdigest(),size,points,int(codes.min()),int(codes.max()),
                      meta.get("identity",{}),asdict(pre),meta["_metadata_sha256"])


def envelope(wave: WaveWindow, bins=1200):
    """Min/max vertical segments retain single-sample excursions in every bin."""
    y=wave.volts
    step=max(1,math.ceil(len(y)/bins))
    starts=np.arange(0,len(y),step)
    ends=np.minimum(starts+step,len(y))
    x=wave.t0+(starts+ends-1)*.5*wave.dt
    return x,np.minimum.reduceat(y,starts),np.maximum.reduceat(y,starts)


def _crossings(y, threshold, rising):
    mask=((y[:-1]<threshold)&(y[1:]>=threshold)) if rising else ((y[:-1]>threshold)&(y[1:]<=threshold))
    indices=np.flatnonzero(mask)+1
    fractions=(threshold-y[indices-1])/(y[indices]-y[indices-1])
    return indices,indices-1+fractions


def signal_analysis(wave: WaveWindow, threshold_v=None, hysteresis_v=None,
                    low_level_v=None, high_level_v=None, edge_measurements=True, minimum_signal_vpp=None):
    y=wave.volts
    low=float(np.quantile(y,.05)) if low_level_v is None else finite(low_level_v,"low_level_v")
    high=float(np.quantile(y,.95)) if high_level_v is None else finite(high_level_v,"high_level_v")
    if high < low: raise ScopeError("high_level_v must not be smaller than low_level_v")
    span=high-low
    midpoint=(low+high)/2 if threshold_v is None else finite(threshold_v,"threshold_v")
    hysteresis=.05*span if hysteresis_v is None else finite(hysteresis_v,"hysteresis_v")
    if hysteresis < 0: raise ScopeError("hysteresis_v must be nonnegative")
    metrics={"min_v":float(y.min()),"max_v":float(y.max()),"vpp_v":float(np.ptp(y)),
             "mean_v":float(y.mean()),"rms_v":float(np.sqrt(np.mean(y*y))),"stddev_v":float(y.std()),
             "frequency_hz":None,"period_s":None,"duty_percent":None,
             "positive_width_s":None,"negative_width_s":None,"rise_time_s":None,"fall_time_s":None}
    robust_low,robust_high=(float(v) for v in np.quantile(y,[.05,.95]))
    robust_span=robust_high-robust_low
    differences=np.diff(y)
    noise_sigma=float(np.median(np.abs(differences-np.median(differences)))/(.67448975*np.sqrt(2)))
    lower_mask=y<=robust_low+.1*robust_span
    upper_mask=y>=robust_high-.1*robust_span
    plateau_supported=(robust_span>1e-12 and float(lower_mask.mean())>=.25 and float(upper_mask.mean())>=.25
                       and float((~lower_mask & ~upper_mask).mean())<=.3)
    plateau_low=float(np.median(y[lower_mask])) if plateau_supported else None
    plateau_high=float(np.median(y[upper_mask])) if plateau_supported else None
    metrics.update(robust_span_v=robust_span,stable_low_v=plateau_low,stable_high_v=plateau_high,
                   stable_level_difference_v=plateau_high-plateau_low if plateau_supported else None)
    minimum=0.0 if minimum_signal_vpp is None else finite(minimum_signal_vpp,"minimum_signal_vpp")
    if minimum<0:raise ScopeError("minimum_signal_vpp must be nonnegative")
    params={"low_level_v":low,"high_level_v":high,"threshold_v":midpoint,"hysteresis_v":hysteresis,
            "level_method":"5th/95th percentile unless explicitly supplied",
            "edge_method":"Schmitt qualification then last interpolated midpoint crossing",
            "rise_fall_method":"10-90% crossings, at least 2 sample intervals"}
    params["minimum_signal_vpp"]=minimum
    params["quality_rule"]="robust span >= 6 * adjacent-difference noise estimate; period CV <= .2; >=10 samples/period"
    quality={"warnings":[],"timing_valid":False,"frequency_valid":False,"timing_reasons":[],
             "noise_sigma_estimate_v":noise_sigma,"robust_span_to_noise":robust_span/noise_sigma if noise_sigma>0 else None,
             "stable_levels_supported":plateau_supported,
             "excursion_samples":int(np.count_nonzero((y<robust_low-.1*robust_span)|(y>robust_high+.1*robust_span))) if robust_span>0 else 0,
             "signal_presence":"not_established","quality_method":"conservative heuristics, not proof of absence or analog noise calibration"}
    if quality["excursion_samples"]:
        quality["warnings"].append("Samples extend beyond the robust range; extrema Vpp includes excursions. Rare pulses may be intentional.")
    quality["adc_rail_suspected"]=wave.code_min==0 or wave.code_max==65535
    if quality["adc_rail_suspected"]:quality["warnings"].append("ADC code touches a rail; clipping is possible, not proven.")
    if not edge_measurements:
        quality["warnings"].append("Edge measurements were not requested.")
        return metrics,params,quality,np.array([]),np.array([],dtype=bool)
    if span <= max(1e-12,max(abs(low),abs(high))*1e-12):
        quality["timing_reasons"].append("INSUFFICIENT_LEVEL_SEPARATION")
        quality["warnings"].insert(0,"Insufficient level separation; edge measurements unavailable. Rare pulses may require explicit levels.")
        return metrics,params,quality,np.array([]),np.array([],dtype=bool)
    if robust_span < minimum or (noise_sigma>0 and robust_span < 6*noise_sigma):
        quality["timing_reasons"].append("BELOW_REQUESTED_AMPLITUDE" if robust_span<minimum else "NOISE_DOMINATED_OR_UNRESOLVED")
        quality["warnings"].append("No supported timing: signal separation is below the requested floor or noise estimate. Not proof that no signal exists.")
        return metrics,params,quality,np.array([]),np.array([],dtype=bool)
    quality["signal_presence"]="level_separation_supported"
    above=y>=midpoint+hysteresis/2
    below=y<=midpoint-hysteresis/2
    outside=np.flatnonzero(above|below)
    states=above[outside]
    changes=np.flatnonzero(states[1:]!=states[:-1])+1
    if len(changes)>MAX_EDGES:raise ScopeError("Too many transitions; narrow the window or increase hysteresis")
    confirm=outside[changes]
    polarity=states[changes]
    edge_indices=np.empty(len(confirm),dtype=np.int64)
    sample_positions=np.empty(len(confirm))
    crossing_cache={}
    for rising in (True,False):
        candidates,positions=_crossings(y,midpoint,rising)
        crossing_cache[rising]=(candidates,positions)
        mask=polarity==rising
        chosen=np.searchsorted(candidates,confirm[mask],side="right")-1
        if np.any(chosen<0):raise ScopeError("Inconsistent edge qualification")
        edge_indices[mask]=candidates[chosen]
        sample_positions[mask]=positions[chosen]
    times=wave.t0+sample_positions*wave.dt
    rises=times[polarity];falls=times[~polarity]
    period_rising=len(rises)>=2 or len(falls)<2
    periods=np.diff(rises if period_rising else falls)
    params["period_edge"]="rising" if period_rising else "falling"
    widths=np.diff(times)
    positive=widths[polarity[:-1]]
    negative=widths[~polarity[:-1]]
    complete_cycles=np.flatnonzero(polarity[:-2]==period_rising) if len(times)>=3 else np.array([],dtype=int)
    duty=100*(times[complete_cycles+1]-times[complete_cycles])/(times[complete_cycles+2]-times[complete_cycles]) if len(complete_cycles) else np.array([])
    if not period_rising:duty=100-duty
    def median(a):return float(np.median(a)) if len(a) else None
    metrics.update(period_s=median(periods),positive_width_s=median(positive),negative_width_s=median(negative),duty_percent=median(duty))
    if metrics["period_s"] and metrics["period_s"]>0:metrics["frequency_hz"]=1/metrics["period_s"]
    quality.update(rising_edges=len(rises),falling_edges=len(falls),complete_periods=len(periods),
                   period_coefficient_of_variation=float(np.std(periods)/np.mean(periods)) if len(periods)>=3 else None)
    if len(periods)<3:quality["warnings"].append("Fewer than three full periods; frequency/duty estimate has limited support.")
    if len(periods)>=3 and quality["period_coefficient_of_variation"]>.05:
        quality["warnings"].append("Edge intervals vary; a single frequency may not describe this waveform.")
    if metrics["period_s"] and metrics["period_s"]/wave.dt<10:
        quality["warnings"].append("Fewer than ten samples per period; timing is poorly resolved and aliasing cannot be excluded.")
    reasons=quality["timing_reasons"]
    if not len(periods):reasons.append("NO_COMPLETE_PERIOD")
    if metrics["period_s"] and metrics["period_s"]/wave.dt<10:reasons.append("UNDERSAMPLED_PERIOD")
    if len(periods)>=3 and quality["period_coefficient_of_variation"]>.2:reasons.append("IRREGULAR_PERIODS")
    if quality["adc_rail_suspected"]:reasons.append("ADC_RAIL_CONTACT")
    quality["frequency_valid"]=bool(len(periods) and not reasons)
    quality["timing_valid"]=bool(len(times)>=1 and not any(x in reasons for x in ["UNDERSAMPLED_PERIOD","ADC_RAIL_CONTACT"]))
    if not quality["frequency_valid"]:
        metrics["frequency_hz"]=metrics["period_s"]=metrics["duty_percent"]=None
    # Locate 10/90 crossings around each qualified edge, bounded by adjacent midpoint edges.
    for rising,name in ((True,"rise_time_s"),(False,"fall_time_s")):
        before_level=low+.1*span if rising else low+.9*span
        after_level=low+.9*span if rising else low+.1*span
        ia,pa=_crossings(y,before_level,rising);ib,pb=_crossings(y,after_level,rising)
        found=[];unresolved=0
        for i in np.flatnonzero(polarity==rising):
            a=np.searchsorted(ia,edge_indices[i],side="right")-1
            b=np.searchsorted(ib,edge_indices[i],side="left")
            if a<0 or b>=len(ib):continue
            left=sample_positions[i-1] if i else -1
            right=sample_positions[i+1] if i+1<len(times) else len(y)
            if not (left<pa[a]<=sample_positions[i]<=pb[b]<right):continue
            duration=(pb[b]-pa[a])*wave.dt
            if duration<2*wave.dt:unresolved+=1
            else:found.append(duration)
        metrics[name]=median(found)
        quality[name+"_resolved_edges"]=len(found)
        quality[name+"_unresolved_edges"]=unresolved
    if not quality["timing_valid"]:
        for key in ("positive_width_s","negative_width_s","rise_time_s","fall_time_s"):metrics[key]=None
    quality["warnings"].append("Computed from saved samples; this does not rule out analog bandwidth limits or aliasing.")
    return metrics,params,quality,times,polarity


def search_events(wave, times, polarity, kind, positive=True, minimum=None, maximum=None, limit=50):
    if kind not in {"pulse_width","edge_gap","voltage_range"}:
        raise ScopeError("Search kind must be pulse_width, edge_gap or voltage_range")
    if type(positive) is not bool or type(limit) is not int or not 1<=limit<=200:
        raise ScopeError("positive must be Boolean; limit must be 1..200")
    if minimum is None and maximum is None:
        raise ScopeError("Provide at least one minimum/maximum bound")
    if minimum is not None: minimum=finite(minimum,"minimum")
    if maximum is not None: maximum=finite(maximum,"maximum")
    if minimum is not None and maximum is not None and minimum>maximum:
        raise ScopeError("minimum exceeds maximum")
    if kind!="voltage_range" and any(v is not None and v<0 for v in (minimum,maximum)):
        raise ScopeError("Time bounds must be nonnegative")
    def outside(values):
        return ((values<minimum) if minimum is not None else np.zeros(len(values),dtype=bool)) | ((values>maximum) if maximum is not None else np.zeros(len(values),dtype=bool))
    events=[]
    if kind=="voltage_range":
        mask=outside(wave.volts)
        transitions=np.diff(np.r_[False,mask,False].astype(np.int8))
        starts=np.flatnonzero(transitions==1);stops=np.flatnonzero(transitions==-1)
        total=len(starts)
        for a,b in zip(starts[:limit],stops[:limit]):
            events.append({"start_s":wave.t0+int(a)*wave.dt,"end_s":wave.t0+int(b)*wave.dt,
                           "duration_s":int(b-a)*wave.dt,"min_v":float(wave.volts[a:b].min()),
                           "max_v":float(wave.volts[a:b].max()),
                           "touches_window_boundary":bool(a==0 or b==len(wave.volts))})
    else:
        if kind=="pulse_width":
            starts=times[:-1][polarity[:-1]==positive]
            stops=times[1:][polarity[:-1]==positive]
        else:
            selected=times[polarity==positive]
            starts,stops=selected[:-1],selected[1:]
        durations=stops-starts
        matches=np.flatnonzero(outside(durations))
        total=len(matches)
        for i in matches[:limit]:
            events.append({"start_s":float(starts[i]),"end_s":float(stops[i]),"duration_s":float(durations[i])})
    return {"kind":kind,"positive":positive,"minimum":minimum,"maximum":maximum,
            "bound_unit":"V" if kind=="voltage_range" else "s","match_count":total,
            "returned_count":len(events),"truncated":total>limit,"events":events,
            "method":"strictly outside bounds; pulse/edge intervals require both endpoints in the selected window; voltage durations use sample bins"}
