"""Resolve and execute one caller-directed measurement under a shared device lock."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import shutil
import time
import uuid

from .artifacts import save_screenshot
from .contracts import CONTRACT_VERSION, MeasurementRequest, SettingsPatch
from .controls import channel_id, enum, finite, same, number
from .errors import ScopeError, MeasurementPreflightError
from .profiles import dump_model
from .waveforms import export_waveforms

CHANNEL_FIELDS={"enabled","probe_ratio","impedance","coupling","scale_v_div","offset_v","bandwidth_limit"}
TRIGGER_COMMON={"kind","channel","level_v","sweep","noise_reject","holdoff_s"}
EDGE_FIELDS=TRIGGER_COMMON|{"slope","coupling"}
PULSE_FIELDS=TRIGGER_COMMON|{"condition","polarity","width_s","lower_s","upper_s"}
MEMORIES={1000,10000,100000,1000000,10000000,25000000,50000000,100000000,125000000,200000000,250000000,500000000}


def snapshot(driver):
    return {"channels":{str(n):driver.channel(n) for n in range(1,5)},
            "timebase":driver.timebase(),"acquisition":driver.acquisition_settings(),
            "edge_trigger":driver.trigger(),"pulse_trigger":driver.pulse_trigger(),
            "trigger_state":driver.transport.query(":TRIGger:STATus?")}


def normalized_trigger(state, kind):
    row=state["edge_trigger" if kind=="edge" else "pulse_trigger"]
    source=row["source"]
    channel=int(source[-1]) if source in {"CHAN1","CHAN2","CHAN3","CHAN4"} else None
    result={"kind":kind,"channel":channel,"level_v":row["level_v"],"sweep":row["sweep"],"noise_reject":row["noise_reject"],"holdoff_s":row["holdoff_s"]}
    if kind=="edge":result.update(slope=row["slope"],coupling=row["coupling"])
    else:
        condition={"GRE":"greater","LESS":"less","GLES":"between"}.get(row["when"])
        result.update(condition=condition,polarity=row["polarity"],lower_s=row["lower_s"],upper_s=row["upper_s"])
        if condition!="between":result["width_s"]=row["lower_s"] if condition=="greater" else row["upper_s"]
    return result


def normalize_pulse(trigger):
    t=deepcopy(trigger)
    if t["condition"]=="between":
        t.pop("width_s",None)
    elif t["condition"]=="greater":
        t["lower_s"]=t["width_s"]
    else:
        t["upper_s"]=t["width_s"]
    return t


def validate_effective(effective, others, options, operation_timeout_s):
    # Validate every group before any SCPI write. Firmware-specific coercion is still checked after write.
    channels=effective["channels"]
    all_channels=others|channels
    for ch,values in channels.items():
        channel_id(int(ch))
        if set(values)!=CHANNEL_FIELDS:raise ScopeError("Incomplete channel plan")
        if values["enabled"] is not True:raise ScopeError("Captured channels must be enabled")
        ratio=finite(values["probe_ratio"],"probe_ratio",.001,50000)
        imp=enum(values["impedance"],"impedance",["1M","50"])
        enum(values["coupling"],"coupling",["DC","AC","GND"])
        enum(values["bandwidth_limit"],"bandwidth_limit",["OFF","20M","250M"])
        finite(values["scale_v_div"],"scale_v_div",(.001 if imp=="1M" else .0002)*ratio,(10 if imp=="1M" else 1)*ratio)
        finite(values["offset_v"],"offset_v",-100*ratio,100*ratio)
    tb=effective["timebase"]
    scale=finite(tb["scale_s_div"],"scale_s_div",5e-10,500)
    finite(tb["offset_s"],"offset_s",-5*scale,max(1,100*scale))
    acq=effective["acquisition"]
    enum(acq["type"],"type",["NORM","AVER","PEAK","HRES"])
    avg=acq["averages"]
    if type(avg) is not int or avg<2 or avg>65536 or avg&(avg-1):raise ScopeError("averages must be a power of two")
    depth=acq["memory_depth"]
    if type(depth) is not int or depth not in MEMORIES:raise ScopeError("Atomic capture requires an explicit supported memory_depth")
    count=sum(1 for c in all_channels.values() if c["enabled"])
    maximum=(500000000 if options["RLU-05"] else 100000000)//(1 if count==1 else 2 if count==2 else 4)
    if depth>maximum:raise ScopeError("Requested memory_depth exceeds the live channel/license limit")
    tr=effective["trigger"]
    channel_id(tr["channel"])
    target=all_channels[str(tr["channel"])]
    if not target["enabled"]:raise ScopeError("Trigger source must be enabled")
    enum(tr["sweep"],"trigger sweep",["AUTO","NORM","SING"])
    finite(tr["level_v"],"trigger level",-4.5*target["scale_v_div"]-target["offset_v"],4.5*target["scale_v_div"]-target["offset_v"])
    if tr["kind"]=="edge":enum(tr["slope"],"slope",["POS","NEG","RFAL"])
    else:
        enum(tr["condition"],"condition",["greater","less","between"])
        enum(tr["polarity"],"polarity",["POS","NEG"])
        low=finite(tr["lower_s"],"lower_s",1e-9,10)
        high=finite(tr["upper_s"],"upper_s",1e-9,10)
        if low>=high:raise ScopeError("Pulse lower_s must be less than upper_s")
    cap=effective["capture"]
    enum(cap["mode"],"capture mode",["NORM","RAW"])
    limit=1000 if cap["mode"]=="NORM" else depth
    if type(cap["points"]) is not int or not 1<=cap["points"]<=limit:raise ScopeError("Invalid capture point count")
    if cap["start"]+cap["points"]-1>limit:raise ScopeError("Capture window exceeds record depth")
    if cap["wait_s"]+5>=operation_timeout_s:raise ScopeError("SINGLE wait leaves insufficient operation budget for configuration/export")
    chunks=math.ceil(cap["points"]/cap["chunk_points"])
    if chunks>10000:raise ScopeError("Too many export chunks")
    estimate=len(channels)*(cap["points"]*(100 if cap["points"]<=10000 else 2)+chunks*1024)+1048576
    if estimate>cap["max_bytes"]:raise ScopeError("Capture exceeds file budget")
    return estimate


def resolve(profile, request: MeasurementRequest, before, options, device):
    if device.model not in profile["supported_models"]:raise ScopeError("Profile does not support the configured model")
    allowed=profile.get("allowed_capture_channels")
    if allowed is not None and any(ch not in allowed for ch in request.channels):raise ScopeError("Profile restricts capture channels")
    defaults=deepcopy(profile["defaults"])
    overrides=dump_model(request.overrides)
    selected={str(ch) for ch in request.channels}
    if set(overrides["channels"])-selected:raise ScopeError("Per-channel overrides may only target captured channels")
    origins={}
    def merge(path,live,*layers):
        result=deepcopy(live)
        for key in result:origins[path+"."+key]="live_readback"
        for label,patch in layers:
            for key,value in patch.items():
                result[key]=value;origins[path+"."+key]=label
        return result
    channels={}
    for ch in request.channels:
        key=str(ch)
        channels[key]=merge("channels."+key,before["channels"][key],
            ("profile.channel_defaults",defaults["channel_defaults"]),
            ("profile.channels."+key,defaults["channels"].get(key,{})),
            ("override.channel_defaults",overrides["channel_defaults"]),
            ("override.channels."+key,overrides["channels"].get(key,{})))
    others={key:value for key,value in before["channels"].items() if key not in selected}
    tb=merge("timebase",before["timebase"],("profile.timebase",defaults["timebase"]),("override.timebase",overrides["timebase"]))
    acq=merge("acquisition",before["acquisition"],("profile.acquisition",defaults["acquisition"]),("override.acquisition",overrides["acquisition"]))
    kind=overrides["trigger"].get("kind",defaults["trigger"].get("kind"))
    if kind is None:kind={"EDGE":"edge","PULS":"pulse"}.get(before["edge_trigger"]["mode"])
    if kind not in {"edge","pulse"}:raise ScopeError("Specify an implemented edge/pulse trigger")
    allowed_fields=EDGE_FIELDS if kind=="edge" else PULSE_FIELDS
    if set(overrides["trigger"])-allowed_fields:raise ScopeError("Override fields do not match the selected trigger kind")
    trigger=merge("trigger",normalized_trigger(before,kind),
                  ("profile.trigger",{k:v for k,v in defaults["trigger"].items() if k in allowed_fields}),
                  ("override.trigger",overrides["trigger"]))
    trigger["kind"]=kind
    if trigger["channel"]=="first_selected":
        trigger["channel"]=request.channels[0]
        origins["trigger.channel"]+=":first_selected"
    if kind=="pulse":
        condition=trigger["condition"]
        explicit=overrides["trigger"]
        profile_trigger=defaults["trigger"] if defaults["trigger"].get("kind")==kind else {}
        if condition=="between":
            if "width_s" in explicit:raise ScopeError("between uses lower_s/upper_s, not width_s")
            if not profile_trigger and not {"lower_s","upper_s"}<=set(explicit):raise ScopeError("Switching to pulse/between requires explicit lower_s and upper_s")
            trigger.pop("width_s",None);origins.pop("trigger.width_s",None)
        else:
            bound="lower_s" if condition=="greater" else "upper_s"
            if not profile_trigger and "width_s" not in explicit:raise ScopeError("Switching to pulse requires explicit width_s")
            if "width_s" not in explicit and "width_s" not in profile_trigger:
                trigger["width_s"]=trigger[bound];origins["trigger.width_s"]=origins["trigger."+bound]
            trigger[bound]=trigger["width_s"];origins["trigger."+bound]=origins["trigger.width_s"]
        trigger=normalize_pulse(trigger)
    cap=merge("capture",{},("profile.capture",defaults["capture"]),("override.capture",overrides["capture"]))
    if cap["points"]=="record":
        cap["points"]=1000 if cap["mode"]=="NORM" else acq["memory_depth"]
        origins["capture.points"]+=":whole_record"
    effective={"channels":channels,"timebase":tb,"acquisition":acq,"trigger":trigger,"capture":cap}
    # Validate merged values too (live inputs/profile files are not assumed to be valid).
    SettingsPatch.model_validate({"channels":channels,"timebase":tb,"acquisition":acq,"trigger":trigger,"capture":cap})
    estimate=validate_effective(effective,others,options,device.operation_timeout_s)
    expected=dump_model(request.expected_signal)
    if set(expected["channels"])-selected:raise ScopeError("Expected-signal channels must be captured")
    for item in expected["channels"].values():
        if item.get("voltage_min_v") is not None and item.get("voltage_max_v") is not None and item["voltage_min_v"]>item["voltage_max_v"]:
            raise ScopeError("Expected minimum voltage exceeds maximum")
    payload={"device":device.alias,"configured_identity":{"model":device.model,"serial":device.serial},
             "profile_ref":{k:profile[k] for k in ("profile_id","version","digest")},
             "effective":effective,"origins":origins,"preserved_other_channels":others,"expected_signal":expected}
    digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    return {**payload,"plan_digest":digest,"estimated_export_bytes":estimate,
            "profile_snapshot":profile,"request":dump_model(request),
            "before":before,"live_options":options,"profile_assumptions":profile["assumptions"],
            "finish_state":"STOP","configuration_left_applied":True}


def quality_report(plan, sample_rate=None, capture=None):
    effective=plan["effective"];expected=plan["expected_signal"]
    findings=[]
    per_channel={}
    for key,signal in expected["channels"].items():
        ch=effective["channels"][key]
        lo=-4*ch["scale_v_div"]-ch["offset_v"];hi=4*ch["scale_v_div"]-ch["offset_v"]
        row={"display_estimate_v":[lo,hi],"expected":signal}
        if signal.get("voltage_min_v",lo)<lo or signal.get("voltage_max_v",hi)>hi:
            findings.append({"code":"EXPECTED_RANGE_OUTSIDE_DISPLAY","channel":int(key),"severity":"warning"})
        if signal.get("frequency_hz"):
            row["expected_cycles_in_nominal_window"]=10*effective["timebase"]["scale_s_div"]*signal["frequency_hz"]
        if sample_rate and signal.get("min_event_width_s"):
            row["instrument_samples_per_event"]=sample_rate*signal["min_event_width_s"]
            exported_rate=sample_rate
            if capture and key in capture.get("channels",{}):
                channel_capture=capture["channels"][key]
                n=channel_capture.get("points_written",0)
                duration=channel_capture.get("time_end_s",0)-channel_capture.get("time_start_s",0)
                if n>1 and duration>0:exported_rate=(n-1)/duration
            row["exported_samples_per_event"]=exported_rate*signal["min_event_width_s"]
            if signal.get("min_samples_per_event") and row["exported_samples_per_event"]<signal["min_samples_per_event"]:
                findings.append({"code":"EXPECTED_EVENT_UNDERSAMPLED","channel":int(key),"severity":"warning"})
        if ch["bandwidth_limit"]!="OFF":row["bandwidth_limit"]=ch["bandwidth_limit"]
        per_channel[key]=row
    if capture and capture.get("channels"):
        first=next(iter(capture["channels"].values()))
        start,end=first.get("time_start_s"),first.get("time_end_s")
        if start is not None and expected.get("pretrigger_s") is not None and -start<expected["pretrigger_s"]:
            findings.append({"code":"PRETRIGGER_WINDOW_TOO_SHORT","severity":"warning"})
        if end is not None and expected.get("posttrigger_s") is not None and end<expected["posttrigger_s"]:
            findings.append({"code":"POSTTRIGGER_WINDOW_TOO_SHORT","severity":"warning"})
        observed={"start_s":start,"end_s":end,"points_per_channel":first["points_written"]}
    else:observed=None
    return {"findings":findings,"channels":per_channel,"actual_capture_window":observed,
            "expected_signal_supplied":bool(expected["channels"] or expected.get("pretrigger_s") is not None or expected.get("posttrigger_s") is not None),
            "application_verdict":"caller_decides","note":"Only supplied expectations are assessed. Sampling estimates do not establish analog bandwidth or prove the target event. No automatic retuning."}


def actual_settings(driver, selected, kind):
    state=snapshot(driver)
    return {"channels":{str(ch):state["channels"][str(ch)] for ch in selected},
            "other_channels":{key:row for key,row in state["channels"].items() if int(key) not in selected},
            "timebase":state["timebase"],"acquisition":state["acquisition"],
            "trigger":normalized_trigger(state,kind),
            "sample_rate_sa_s":number(driver.transport.query(":ACQuire:SRATe?"))}


def execute(driver, identity, state_dir: Path, plan, acquisition_callback=None):
    started=time.monotonic()
    effective=plan["effective"];cap=effective["capture"]
    if shutil.disk_usage(state_dir).free<plan["estimated_export_bytes"]+8*1024*1024:
        raise MeasurementPreflightError("Insufficient disk space before configuration")
    if cap["chunk_points"]*2>driver.transport.device.max_binary_bytes:raise MeasurementPreflightError("Chunk exceeds transport limit")
    if driver.transport.deadline-time.monotonic()<cap["wait_s"]+5:raise MeasurementPreflightError("Insufficient remaining operation time before configuration")
    receipt={"contract_version":CONTRACT_VERSION,"operation_id":getattr(driver,"operation_id",uuid.uuid4().hex),
             "created_at":datetime.now(timezone.utc).isoformat(),
             "operation_timeout_s":getattr(driver,"operation_timeout_s",driver.transport.device.operation_timeout_s),
             "profile_ref":plan["profile_ref"],"plan_digest":plan["plan_digest"],"identity":identity,
             "effective":effective,"parameter_origins":plan["origins"],"expected_signal":plan["expected_signal"],
             "before":plan["before"],"steps":[],"verified":False,"capture":None,"screenshot":None,
             "request":plan["request"],"profile_snapshot":plan["profile_snapshot"],
             "configuration_left_applied":True,"application_verdict":"caller_decides"}
    stage="stop"
    def apply_step(name,result):
        receipt["steps"].append({"stage":name,"result":result})
        if not result.get("verified"):raise ScopeError("READBACK_MISMATCH: "+name)
    try:
        apply_step("stop",driver.acquisition_control("stop"))
        stage="channels"
        for key,settings in effective["channels"].items():
            apply_step("channel_"+key,driver.configure_channel(int(key),settings))
        stage="timebase"
        apply_step(stage,driver.configure_timebase(effective["timebase"]))
        stage="acquisition"
        apply_step(stage,driver.configure_acquisition(effective["acquisition"]))
        stage="trigger"
        trigger=deepcopy(effective["trigger"]);kind=trigger.pop("kind")
        apply_step(stage,driver.configure_trigger(**trigger) if kind=="edge" else driver.configure_pulse_trigger(**trigger))
        stage="final_readback"
        actual=actual_settings(driver,[int(k) for k in effective["channels"]],kind)
        receipt["actual_settings"]=actual
        receipt["configuration_before_arm"]=deepcopy(actual)
        mismatches=[]
        for group in ("channels","timebase","acquisition","trigger"):
            if group=="channels":
                for ch,row in effective[group].items():
                    for key,value in row.items():
                        if not same(value,actual[group][ch].get(key)):mismatches.append(f"{group}.{ch}.{key}")
            else:
                for key,value in effective[group].items():
                    if not same(value,actual[group].get(key)):mismatches.append(f"{group}.{key}")
        if mismatches:raise ScopeError("FINAL_READBACK_MISMATCH: "+",".join(mismatches))
        receipt["quality"]=quality_report(plan,actual["sample_rate_sa_s"])
        stage="single"
        single=(acquisition_callback(driver,receipt) if acquisition_callback else driver.acquisition_control("single",cap["wait_s"]))
        receipt["acquisition"]=single
        if not single["verified"]:raise ScopeError("TRIGGER_TIMEOUT" if single["trigger_timeout"] else "ACQUISITION_NOT_VERIFIED")
        stage="export"
        capture=export_waveforms(driver,state_dir,identity,[int(k) for k in effective["channels"]],
            mode=cap["mode"],points=cap["points"],start=cap["start"],chunk_points=cap["chunk_points"],max_bytes=cap["max_bytes"])
        receipt["capture"]=capture
        if not capture["verified"]:raise ScopeError("EXPORT_NOT_VERIFIED")
        stage="screenshot"
        if cap["screenshot"]:receipt["screenshot"]=save_screenshot(state_dir,driver.screenshot(),identity,plan["device"])
        stage="post_capture_readback"
        final=actual_settings(driver,[int(k) for k in effective["channels"]],kind)
        receipt["actual_settings"]=final
        unexpected=[]
        for group in ("channels","other_channels","timebase","acquisition"):
            if final[group]!=actual[group]:unexpected.append(group)
        for key,value in actual["trigger"].items():
            if key=="sweep" and final["trigger"].get(key)=="SING":continue
            if not same(value,final["trigger"].get(key)):unexpected.append("trigger."+key)
        receipt["capture_side_effects"]={"trigger_sweep_before":actual["trigger"]["sweep"],"trigger_sweep_after":final["trigger"]["sweep"]}
        if unexpected:raise ScopeError("CONFIGURATION_CHANGED_DURING_CAPTURE: "+",".join(unexpected))
        receipt["quality"]=quality_report(plan,final["sample_rate_sa_s"],capture)
        receipt["acquisition_after"]=driver.transport.query(":TRIGger:STATus?")
        if receipt["acquisition_after"]!="STOP":raise ScopeError("Acquisition changed before receipt completion")
        receipt["verified"]=True
    except Exception as exc:
        receipt["failure"]={"stage":stage,"type":type(exc).__name__,"message":str(exc)}
        try:
            receipt["cleanup"]=driver.acquisition_control("stop")
        except Exception as cleanup:
            receipt["cleanup"]={"verified":False,"state":"unknown","error":str(cleanup)}
    # Persistent receipt survives failed/partial runs; no replay or rollback is attempted.
    directory=state_dir/"measurements";directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=directory/(receipt["operation_id"]+".json")
    receipt["receipt_path"]=str(path)
    receipt["receipt_uri"]=f"scope://measurements/{receipt['operation_id']}/receipt.json"
    receipt["elapsed_s"]=round(time.monotonic()-started,4)
    path.write_text(json.dumps(receipt,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
    if receipt["capture"] is not None:
        meta_path=Path(receipt["capture"]["directory"])/"metadata.json"
        meta=json.loads(meta_path.read_text())
        meta["measurement_context"]={k:receipt.get(k) for k in ("contract_version","operation_id","profile_ref","profile_snapshot","request","plan_digest","effective","parameter_origins","expected_signal","configuration_before_arm","actual_settings","quality","verified","failure","screenshot","receipt_uri")}
        temp=meta_path.with_name("metadata.json.measurement.tmp")
        temp.write_text(json.dumps(meta,ensure_ascii=False,indent=2,allow_nan=False)+"\n");temp.replace(meta_path)
    return receipt


def read_receipt(state_dir: Path, operation_id: str) -> str:
    import re
    if not re.fullmatch(r"[0-9a-f]{32}",operation_id):raise ScopeError("Invalid operation_id")
    root=(state_dir/"measurements").resolve()
    path=state_dir/"measurements"/(operation_id+".json")
    if path.is_symlink() or path.resolve().parent!=root or not path.is_file():raise ScopeError("Unknown measurement receipt")
    if path.stat().st_size>8*1024*1024:raise ScopeError("Receipt exceeds 8 MiB")
    return path.read_text()
