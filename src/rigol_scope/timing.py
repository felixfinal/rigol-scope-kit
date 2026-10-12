"""Two channels from one immutable capture; all results are host computations."""
from __future__ import annotations
import math
import numpy as np
from .analysis import signal_analysis, finite, MAX_POINTS, check_deadline
from .errors import ScopeError

ALGORITHM='saved-two-channel-timing-1'


def analyze_pair(a,b,method='edge_delay',frequency_hz=None,max_delay_s=None,edge='rising',
                 threshold_a_v=None,threshold_b_v=None,hysteresis_a_v=None,hysteresis_b_v=None,
                 minimum_signal_vpp=None,deadline=None):
    if a.capture_id!=b.capture_id or a.channel==b.channel:
        raise ScopeError('Choose distinct channels from one capture')
    if (len(a.volts)!=len(b.volts) or not math.isclose(a.dt,b.dt,rel_tol=1e-10,abs_tol=1e-18)
        or abs(a.t0-b.t0)>min(a.dt,b.dt)*1e-6 or a.metadata_sha256!=b.metadata_sha256):
        raise ScopeError('Channels must share an identical selected time grid and metadata snapshot; no resampling')
    if len(a.volts)+len(b.volts)>MAX_POINTS:raise ScopeError('Dual analysis exceeds 2M total selected samples; narrow the window')
    if method not in {'sine_fit','edge_delay'}:raise ScopeError('method must be sine_fit or edge_delay')
    if edge not in {'rising','falling'}:raise ScopeError('edge must be rising or falling')
    result={'valid':False,'phase_valid':False,'delay_b_minus_a_s':None,'phase_lead_b_deg':None,'period_s':None,
            'method':method,'quality':{'reasons':[],'warnings':[]},'diagnostics':{},
            'convention':{'delay':'positive means B occurs later than A',
                          'phase':'positive means B leads A; phase is wrapped to [-180,180)',
                          'sine_delay':'principal equivalent delay only, ambiguous by integer periods'}}
    qa=result['quality'];diagnostics=result['diagnostics']
    if method=='sine_fit':
        if max_delay_s is not None:raise ScopeError('sine_fit does not use max_delay_s')
        if any(x is not None for x in [threshold_a_v,threshold_b_v,hysteresis_a_v,hysteresis_b_v]):
            raise ScopeError('sine_fit does not use edge thresholds')
        f=finite(frequency_hz,'frequency_hz')
        if f<=0:raise ScopeError('frequency_hz must be positive')
        if f*a.dt>.1:qa['reasons'].append('FEWER_THAN_TEN_SAMPLES_PER_CYCLE')
        if (len(a.volts)-1)*a.dt*f<3:qa['reasons'].append('FEWER_THAN_THREE_CYCLES')
        if qa['reasons']:return result
        phases=[]
        t=np.arange(len(a.volts))*a.dt
        angle=2*np.pi*f*t
        design=np.column_stack([np.sin(angle),np.cos(angle),np.ones(len(t))])
        for label,wave in [('a',a),('b',b)]:
            metrics,_,quality,_,_=signal_analysis(wave,edge_measurements=False,minimum_signal_vpp=minimum_signal_vpp)
            # Use a common reference time; never fit arrays with independently shifted axes.
            coeff=np.linalg.lstsq(design,wave.volts,rcond=None)[0]
            residual=wave.volts-design@coeff
            amplitude=float(np.hypot(coeff[0],coeff[1]))
            residual_rms=float(np.sqrt(np.mean(residual*residual)))
            variance=float(np.var(wave.volts))
            explained=1-residual_rms**2/variance if variance>0 else 0.0
            noise=quality['noise_sigma_estimate_v']
            floor=0 if minimum_signal_vpp is None else finite(minimum_signal_vpp,'minimum_signal_vpp')
            valid=(2*amplitude>=max(1e-12,floor,6*noise) and explained>=.9 and not quality['adc_rail_suspected'])
            diagnostics[label]={'fitted_vpp_v':2*amplitude,'offset_v':float(coeff[2]),'residual_rms_v':residual_rms,
                                'explained_variance':explained,'valid':valid,'source_quality':quality}
            if not valid:qa['reasons'].append('CHANNEL_'+label.upper()+'_SINE_FIT_UNSUPPORTED')
            phases.append(float(np.degrees(np.arctan2(coeff[1],coeff[0]))))
            if deadline is not None:check_deadline(deadline)
        result['period_s']=1/f
        if not qa['reasons']:
            phase=(phases[1]-phases[0]+180)%360-180
            result.update(valid=True,phase_valid=True,phase_lead_b_deg=phase,delay_b_minus_a_s=-phase/(360*f))
        qa['warnings'].append('Fits a caller-supplied frequency, not a frequency measurement; fit residual and explained variance gate validity. Integer-cycle delay is unresolved.')
    else:
        if frequency_hz is not None:raise ScopeError('edge_delay estimates common period; frequency_hz is only for sine_fit')
        bound=finite(max_delay_s,'max_delay_s')
        if bound<=0:raise ScopeError('max_delay_s must be positive')
        analyses=[];edges=[]
        for label,wave,threshold,hysteresis in [('a',a,threshold_a_v,hysteresis_a_v),('b',b,threshold_b_v,hysteresis_b_v)]:
            m,p,q,t,pol=signal_analysis(wave,threshold_v=threshold,hysteresis_v=hysteresis,minimum_signal_vpp=minimum_signal_vpp)
            diagnostics[label]={'parameters':p,'quality':q};analyses.append((m,q))
            if not q['timing_valid']:qa['reasons'].append('CHANNEL_'+label.upper()+'_TIMING_UNSUPPORTED')
            edges.append(t[pol==(edge=='rising')])
            if deadline is not None:check_deadline(deadline)
        ta,tb=edges
        left=np.searchsorted(tb,ta-bound,'left');right=np.searchsorted(tb,ta+bound,'right')
        ba=np.searchsorted(ta,tb-bound,'left');bb=np.searchsorted(ta,tb+bound,'right')
        ambiguous=int(np.count_nonzero(right-left>1)+np.count_nonzero(bb-ba>1))
        indices=np.flatnonzero(right-left==1)
        if len(indices):indices=indices[(bb-ba)[left[indices]]==1]
        delays=tb[left[indices]]-ta[indices]
        diagnostics.update(matched_edges=len(delays),ambiguous_edges=ambiguous,
                           unmatched_a=len(ta)-len(indices),unmatched_b=len(tb)-len(indices),
                           matches=[{'a_s':float(ta[i]),'b_s':float(tb[left[i]]),'delay_s':float(tb[left[i]]-ta[i])} for i in indices[:50]],
                           matches_truncated=len(indices)>50)
        if ambiguous:qa['reasons'].append('AMBIGUOUS_EDGE_PAIRING_REDUCE_MAX_DELAY')
        if not len(delays):qa['reasons'].append('NO_EDGE_PAIR_IN_BOUND')
        if not qa['reasons']:
            delay=float(np.median(delays));result.update(valid=True,delay_b_minus_a_s=delay)
            diagnostics['delay_mad_s']=float(np.median(np.abs(delays-delay)))
            pa,pb=analyses[0][0]['period_s'],analyses[1][0]['period_s']
            if pa and pb and abs(pa-pb)/max(pa,pb)<=.01 and min(analyses[0][1]['complete_periods'],analyses[1][1]['complete_periods'])>=3:
                period=(pa+pb)/2
                if diagnostics['delay_mad_s']<=.02*period:
                    result.update(phase_valid=True,period_s=period,phase_lead_b_deg=(-360*delay/period+180)%360-180)
            if not result['phase_valid']:qa['warnings'].append('Delay pairs exist, but a stable common period is not established; phase is unavailable.')
        qa['warnings'].append('Pairing uses caller max_delay_s and unique reciprocal neighbors; same-capture timing does not prove event causality.')
    qa['warnings'].append('Host computation from saved samples; probe/channel deskew and analog calibration are not applied.')
    return result
