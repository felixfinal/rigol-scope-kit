"""Generate bounded derived artifacts on the server, keeping original captures immutable."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import time
import uuid

import numpy as np
from . import __version__

from .analysis import ALGORITHM, check_deadline, envelope, load_window, search_events, signal_analysis
from .artifacts import capture_directory
from .errors import ScopeError
from .locking import device_lock


def derive(state_dir: Path, capture_id: str, channel=1, operation="analyze",
           start_s=None, end_s=None, threshold_v=None, hysteresis_v=None,
           low_level_v=None, high_level_v=None, search_kind=None, positive=True,
           minimum=None, maximum=None, limit=50, export_csv=False, minimum_signal_vpp=None):
    if operation not in {"plot","analyze","search"}: raise ScopeError("Unknown analysis operation")
    if type(export_csv) is not bool: raise ScopeError("export_csv must be Boolean")
    started=time.monotonic()
    deadline=started+30
    # Serializes CPU/render work across clients without taking the hardware lock.
    with device_lock(state_dir/"locks","offline-analysis",deadline):
        wave=load_window(state_dir,capture_id,channel,start_s,end_s,deadline)
        if export_csv and len(wave.volts)>100000:
            raise ScopeError("CSV export is limited to 100,000 selected points; raw binary remains available")
        metrics,parameters,quality,times,polarity=signal_analysis(
            wave,threshold_v,hysteresis_v,low_level_v,high_level_v,edge_measurements=operation!="plot",minimum_signal_vpp=minimum_signal_vpp)
        search=None
        if operation=="search":
            search=search_events(wave,times,polarity,search_kind,positive,minimum,maximum,limit)
        check_deadline(deadline)
        directory=capture_directory(state_dir,capture_id)
        if shutil.disk_usage(directory).free<32*1024*1024:
            raise ScopeError("Less than 32 MiB free for derived artifacts")
        count=0
        for _ in directory.iterdir():
            count+=1
            if count>1900: raise ScopeError("Capture artifact quota reached")
        analysis_id=uuid.uuid4().hex
        names={"plot":f"plot-{analysis_id}.png","analysis":f"analysis-{analysis_id}.json",
               "report":f"report-{analysis_id}.md"}
        result={"schema_version":1,"analysis_id":analysis_id,"capture_id":capture_id,
                "operation":operation,"created_at":datetime.now(timezone.utc).isoformat(),
                "status":"complete","instrument_access":False,"algorithm":ALGORITHM,
                "software":{"rigol_scope_kit":__version__,"numpy":np.__version__},
                "source":wave.provenance(),"parameters":parameters,"metrics":metrics,
                "quality":quality,"search":search,"artifacts":{},
                "plot_method":"original line <=2400 points; min/max envelope per bin otherwise"}
        staged=[]
        def stage(name, data):
            path=directory/(name+".partial")
            path.write_bytes(data)
            staged.append((path,directory/name))
            result["artifacts"][name]={"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),
                                      "uri":f"scope://files/{capture_id}/{name}"}
        try:
            from io import BytesIO
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            import matplotlib
            result["software"]["matplotlib"]=matplotlib.__version__
            figure=Figure(figsize=(10,4.5),dpi=140,layout="constrained")
            FigureCanvasAgg(figure)
            ax=figure.subplots()
            if len(wave.volts)<=2400:
                ax.plot(wave.t0+np.arange(len(wave.volts))*wave.dt,wave.volts,lw=.9,color="#1669ad")
            else:
                x,low,high=envelope(wave,1200)
                ax.vlines(x,low,high,lw=.7,color="#1669ad",label="Min/max envelope")
            if operation!="plot":
                ax.axhline(parameters["threshold_v"],color="#a85a00",ls="--",lw=.7,label="Edge threshold")
            if search:
                for event in search["events"][:20]:
                    ax.axvspan(event["start_s"],event["end_s"],alpha=.25,color="#db504a")
            ax.set(xlabel="Time relative to trigger (s)",ylabel="Voltage (V)",
                   title=f"Saved waveform | CH{channel} | {capture_id[:12]}")
            ax.grid(True,alpha=.22)
            if ax.get_legend_handles_labels()[0]:ax.legend(loc="best",fontsize=8)
            image=BytesIO()
            figure.savefig(image,format="png",metadata={"Software":"rigol-scope-kit "+ALGORITHM})
            stage(names["plot"],image.getvalue())
            figure.clear()
            if export_csv:
                import csv
                from io import StringIO
                out=StringIO()
                writer=csv.writer(out);writer.writerow(["sample_index","time_s","voltage_v"])
                for i,v in enumerate(wave.volts):
                    writer.writerow([wave.first_index+i,wave.t0+i*wave.dt,float(v)])
                names["csv"]=f"window-{analysis_id}.csv"
                stage(names["csv"],out.getvalue().encode())
            report=f"""# Saved waveform analysis

Capture: {capture_id}; channel: {channel}; algorithm: {ALGORITHM}.

Computed on the server from saved samples. No instrument access.

## Source and window

- SHA-256: {wave.source_sha256}
- Metadata SHA-256: {wave.metadata_sha256}
- Points: {len(wave.volts)} of {wave.record_points}
- Time: {wave.t0:.9g} to {wave.t0+(len(wave.volts)-1)*wave.dt:.9g} s
- Sample interval: {wave.dt:.9g} s

## Computed measurements

| Measurement | Value |
| --- | --- |
"""
            report+="".join(f"| {key} | {value if value is not None else 'unavailable'} |\n" for key,value in metrics.items())
            report+="\n## Quality and limitations\n\n"
            report+="\n".join("- "+message for message in quality["warnings"])+"\n"
            report+="\nThresholds, interpolation and complete-window edge rules are recorded in the JSON result. Plot envelopes preserve sample extrema; they are not used as measurement input.\n"
            if search:
                report+=f"\n## Search\n\n{search['match_count']} matches; {search['returned_count']} returned. Bounds are in {search['bound_unit']}. See JSON for event locations and truncation status.\n"
            stage(names["report"],report.encode())
            result["elapsed_s"]=round(time.monotonic()-started,4)
            result["analysis_uri"]=f"scope://files/{capture_id}/{names['analysis']}"
            result["plot_file"]=names["plot"]
            result["report_file"]=names["report"]
            result["analysis_file"]=names["analysis"]
            check_deadline(deadline)
            # Publish supporting files first; the JSON manifest commits the set last.
            for temporary,target in staged:temporary.replace(target)
            temporary=directory/(names["analysis"]+".partial")
            temporary.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n")
            temporary.replace(directory/names["analysis"])
            return result
        finally:
            for temporary,_ in staged:
                if temporary.exists():temporary.unlink()


def derive_timing(state_dir: Path,capture_id: str,channel_a: int,channel_b: int,
                  method='edge_delay',frequency_hz=None,max_delay_s=None,edge='rising',
                  start_s=None,end_s=None,threshold_a_v=None,threshold_b_v=None,
                  hysteresis_a_v=None,hysteresis_b_v=None,minimum_signal_vpp=None):
    from .timing import analyze_pair, ALGORITHM as TIMING_ALGORITHM
    if type(channel_a) is not int or type(channel_b) is not int or channel_a==channel_b:
        raise ScopeError('Choose two distinct integer channel numbers')
    started=time.monotonic();deadline=started+30
    with device_lock(state_dir/'locks','offline-analysis',deadline):
        a=load_window(state_dir,capture_id,channel_a,start_s,end_s,deadline,max_points=1000000)
        b=load_window(state_dir,capture_id,channel_b,start_s,end_s,deadline,max_points=1000000)
        params=dict(method=method,frequency_hz=frequency_hz,max_delay_s=max_delay_s,edge=edge,
                    threshold_a_v=threshold_a_v,threshold_b_v=threshold_b_v,hysteresis_a_v=hysteresis_a_v,
                    hysteresis_b_v=hysteresis_b_v,minimum_signal_vpp=minimum_signal_vpp)
        timing=analyze_pair(a,b,**params,deadline=deadline)
        check_deadline(deadline)
        directory=capture_directory(state_dir,capture_id)
        if shutil.disk_usage(directory).free<32*1024*1024:raise ScopeError('Less than 32 MiB free for derived artifacts')
        if sum(1 for _ in directory.iterdir())>1900:raise ScopeError('Capture artifact quota reached')
        analysis_id=uuid.uuid4().hex
        names={'plot':f'plot-{analysis_id}.png','report':f'report-{analysis_id}.md','analysis':f'analysis-{analysis_id}.json'}
        result={'schema_version':1,'analysis_id':analysis_id,'capture_id':capture_id,'operation':'timing',
                'status':'complete','created_at':datetime.now(timezone.utc).isoformat(),'instrument_access':False,
                'algorithm':TIMING_ALGORITHM,'software':{'rigol_scope_kit':__version__,'numpy':np.__version__},
                'source_a':a.provenance(),'source_b':b.provenance(),'parameters':params,'timing':timing,
                'quality':timing['quality'],'artifacts':{},'plot_method':'overlaid original samples or per-channel min/max envelope; no alignment shift'}
        staged=[]
        def stage(name,data):
            temp=directory/(name+'.partial');temp.write_bytes(data);staged.append((temp,directory/name))
            result['artifacts'][name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'uri':f'scope://files/{capture_id}/{name}'}
        try:
            from io import BytesIO
            from matplotlib.figure import Figure
            from matplotlib.backends.backend_agg import FigureCanvasAgg
            figure=Figure(figsize=(10,4.5),dpi=140,layout='constrained');FigureCanvasAgg(figure);ax=figure.subplots()
            for wave,color in [(a,'#1669ad'),(b,'#b04a9f')]:
                if len(wave.volts)<=2400:ax.plot(wave.t0+np.arange(len(wave.volts))*wave.dt,wave.volts,color=color,lw=.8,label=f'CH{wave.channel}')
                else:
                    x,lo,hi=envelope(wave);ax.vlines(x,lo,hi,color=color,lw=.6,label=f'CH{wave.channel} envelope')
            ax.set(xlabel='Time relative to trigger (s)',ylabel='Voltage (V)',title=f'Saved dual-channel {method} | valid={timing["valid"]}')
            ax.grid(True,alpha=.2);ax.legend();png=BytesIO();figure.savefig(png,format='png');figure.clear();stage(names['plot'],png.getvalue())
            report=f'''# Saved dual-channel timing

Capture: {capture_id}; A=CH{channel_a}, B=CH{channel_b}; algorithm: {TIMING_ALGORITHM}.

No instrument access. Same capture and time grid; both source hashes verified. Original records remain unchanged.

- Method: {method}
- Delay valid: {timing['valid']}
- B-minus-A delay: {timing['delay_b_minus_a_s']} s (positive means B later)
- Phase valid: {timing['phase_valid']}
- B-leads-A phase: {timing['phase_lead_b_deg']} degrees
- Source A SHA-256: {a.source_sha256}
- Source B SHA-256: {b.source_sha256}

## Quality and limitations

'''+ '\n'.join('- '+x for x in timing['quality']['reasons']+timing['quality']['warnings'])+'\n'
            stage(names['report'],report.encode())
            result.update(plot_file=names['plot'],report_file=names['report'],analysis_file=names['analysis'],
                          analysis_uri=f'scope://files/{capture_id}/{names["analysis"]}',elapsed_s=round(time.monotonic()-started,4))
            check_deadline(deadline)
            for temp,target in staged:temp.replace(target)
            temp=directory/(names['analysis']+'.partial');temp.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+'\n');temp.replace(directory/names['analysis'])
            return result
        finally:
            for temp,_ in staged:
                if temp.exists():temp.unlink()
