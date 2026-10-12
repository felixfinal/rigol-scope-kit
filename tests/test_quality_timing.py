import asyncio
from dataclasses import replace
import hashlib,json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from rigol_scope.analysis import WaveWindow,signal_analysis,load_window
from rigol_scope.timing import analyze_pair
from rigol_scope.reports import derive_timing
from rigol_scope.config import Settings
from rigol_scope.mcp_server import create_server
from rigol_scope.errors import ScopeError
from rigol_scope.artifacts import read_artifact
from test_analysis import fixture,trapezoid


def wave(y,ch=1,dt=1e-6):
    y=np.asarray(y,dtype=float)
    return WaveWindow('a'*32,ch,y,dt,-.005,0,'abc',len(y)*2,len(y),20000,40000,metadata_sha256='same')


def pair_fixture(root,phase=90):
    t=np.arange(10000)*1e-6
    cid=fixture(root,.5*np.sin(2*np.pi*1000*t))
    folder=root/'captures'/cid
    m=json.loads((folder/'metadata.json').read_text())
    raw=np.round(.5*np.sin(2*np.pi*1000*t+np.deg2rad(phase))/.001+32768).astype('<u2').tobytes()
    (folder/'ch2.bin').write_bytes(raw)
    m['channels']['2']=dict(m['channels']['1'],sha256=hashlib.sha256(raw).hexdigest())
    (folder/'metadata.json').write_text(json.dumps(m))
    return cid


class QualityTests(unittest.TestCase):
    def test_noise_has_no_frequency_or_duty(self):
        y=np.random.default_rng(41).normal(0,.004,100000)
        m,p,q,t,pol=signal_analysis(wave(y))
        self.assertIsNone(m['frequency_hz']);self.assertIsNone(m['duty_percent'])
        self.assertFalse(q['frequency_valid']);self.assertFalse(len(t))
        self.assertIn('NOISE_DOMINATED_OR_UNRESOLVED',q['timing_reasons'])
        self.assertGreater(m['vpp_v'],0)

    def test_low_amplitude_real_sine_without_arbitrary_voltage_floor(self):
        t=np.arange(10000)*1e-6;y=1e-5*np.sin(2*np.pi*1000*t)
        m,_,q,_,_=signal_analysis(wave(y))
        self.assertTrue(q['frequency_valid']);self.assertAlmostEqual(m['frequency_hz'],1000)
        m,_,q,_,_=signal_analysis(wave(y),minimum_signal_vpp=.001)
        self.assertIsNone(m['frequency_hz']);self.assertIn('BELOW_REQUESTED_AMPLITUDE',q['timing_reasons'])

    def test_extrema_and_stable_levels_are_separate(self):
        y=trapezoid(10000);y[50]=7
        m,_,q,_,_=signal_analysis(wave(y))
        self.assertEqual(m['vpp_v'],7)
        self.assertEqual(m['stable_level_difference_v'],3)
        self.assertGreater(q['excursion_samples'],0)
        sine=wave(np.sin(2*np.pi*1000*np.arange(10000)*1e-6))
        m,_,q,_,_=signal_analysis(sine)
        self.assertIsNone(m['stable_level_difference_v'])

    def test_rail_contact_and_irregular_periods_invalidate_frequency(self):
        w=replace(wave(trapezoid()),code_min=0)
        m,_,q,_,_=signal_analysis(w)
        self.assertTrue(q['adc_rail_suspected']);self.assertFalse(q['frequency_valid']);self.assertIsNone(m['frequency_hz'])
        y=trapezoid();y[2000:3000]=0
        m,_,q,_,_=signal_analysis(wave(y))
        self.assertIn('IRREGULAR_PERIODS',q['timing_reasons']);self.assertIsNone(m['frequency_hz'])

    def test_negative_minimum_signal_rejected(self):
        with self.assertRaises(ScopeError):signal_analysis(wave(trapezoid()),minimum_signal_vpp=-1)


class PairTests(unittest.TestCase):
    def test_sine_phase_delay_sign_and_wrap(self):
        t=np.arange(10000)*1e-6
        a=wave(.5*np.sin(2*np.pi*1000*t))
        for phase in [0,.28,90,-90,180]:
            b=wave(.7*np.sin(2*np.pi*1000*t+np.deg2rad(phase))+.03,ch=2)
            r=analyze_pair(a,b,method='sine_fit',frequency_hz=1000)
            self.assertTrue(r['valid'],r)
            self.assertAlmostEqual((r['phase_lead_b_deg']-phase+180)%360-180,0,places=8)
            self.assertAlmostEqual(r['delay_b_minus_a_s'],-r['phase_lead_b_deg']/360000)

    def test_noise_wrong_frequency_square_and_short_window_rejected(self):
        t=np.arange(10000)*1e-6;a=wave(np.sin(2*np.pi*1000*t))
        for y in [np.random.default_rng(10).normal(0,.1,len(t)),np.sin(2*np.pi*1700*t),np.sign(np.sin(2*np.pi*1000*t))]:
            r=analyze_pair(a,wave(y,2),method='sine_fit',frequency_hz=1000)
            self.assertFalse(r['valid']);self.assertIsNone(r['phase_lead_b_deg'])
        r=analyze_pair(wave(a.volts[:100]),wave(a.volts[:100],2),method='sine_fit',frequency_hz=1000)
        self.assertFalse(r['valid'])

    def test_unique_edge_delay_and_ambiguous_bound(self):
        t=np.arange(10000)*1e-6
        a=wave(np.where(np.sin(2*np.pi*1000*t)>=0,1,0))
        b=wave(np.where(np.sin(2*np.pi*1000*(t-.00025))>=0,1,0),2)
        r=analyze_pair(a,b,max_delay_s=.0004)
        self.assertTrue(r['valid'],r);self.assertAlmostEqual(r['delay_b_minus_a_s'],.00025,delta=2e-6)
        self.assertTrue(r['phase_valid']);self.assertAlmostEqual(r['phase_lead_b_deg'],-90,delta=1)
        r=analyze_pair(a,b,max_delay_s=.0008)
        self.assertFalse(r['valid']);self.assertIn('AMBIGUOUS_EDGE_PAIRING_REDUCE_MAX_DELAY',r['quality']['reasons'])

    def test_same_capture_grid_and_arguments(self):
        a=wave(trapezoid());b=wave(trapezoid(),2)
        for invalid in [replace(b,t0=.1),replace(b,dt=2e-6),replace(b,capture_id='b'*32),replace(b,metadata_sha256='different')]:
            with self.assertRaises(ScopeError):analyze_pair(a,invalid,max_delay_s=.0001)
        for kw in [{},{'method':'sine_fit'},{'max_delay_s':-1},{'max_delay_s':.1,'frequency_hz':1000},{'method':'sine_fit','frequency_hz':1000,'threshold_a_v':0}]:
            with self.assertRaises(ScopeError):analyze_pair(a,b,**kw)

    def test_total_points_limit(self):
        a=wave(np.zeros(1000001));b=wave(a.volts,2)
        with self.assertRaisesRegex(ScopeError,'2M total'):analyze_pair(a,b,method='sine_fit',frequency_hz=1000)

    def test_dual_report_hashes_and_source_immutability(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cid=pair_fixture(root);folder=root/'captures'/cid
            originals={p.name:p.read_bytes() for p in folder.iterdir()}
            result=derive_timing(root,cid,1,2,method='sine_fit',frequency_hz=1000)
            self.assertTrue(result['timing']['valid']);self.assertFalse(result['instrument_access'])
            self.assertTrue(read_artifact(root,cid,result['plot_file']).startswith(b'\x89PNG'))
            self.assertIn('source_b',result)
            for name,data in originals.items():self.assertEqual((folder/name).read_bytes(),data)
            raw=folder/'ch2.bin';raw.write_bytes(b'\x00\x00'+raw.read_bytes()[2:])
            with self.assertRaisesRegex(ScopeError,'SHA-256'):derive_timing(root,cid,1,2,method='sine_fit',frequency_hz=1000)

    def test_mcp_dual_analysis_never_constructs_hardware(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);cid=pair_fixture(root)
                with patch('rigol_scope.mcp_server.ScopeService',side_effect=AssertionError('no hardware')):
                    server=create_server(Settings({},root))
                    r=await server.call_tool('scope_waveform_timing',{'capture_id':cid,'channel_a':1,'channel_b':2,'method':'sine_fit','frequency_hz':1000})
                    self.assertFalse(r.isError);self.assertTrue(r.structuredContent['timing']['valid'])
                    bad=await server.call_tool('scope_waveform_timing',{'capture_id':cid,'channel_a':1,'channel_b':1,'method':'sine_fit','frequency_hz':1000})
                    self.assertTrue(bad.isError)
        asyncio.run(run())

class SingleEdgeTests(unittest.TestCase):
    def test_one_step_has_delay_without_inventing_period(self):
        a=wave(np.r_[np.zeros(3000),np.ones(3000)])
        b=wave(np.r_[np.zeros(3250),np.ones(2750)],2)
        r=analyze_pair(a,b,max_delay_s=.0004)
        self.assertTrue(r['valid'],r)
        self.assertAlmostEqual(r['delay_b_minus_a_s'],.00025)
        self.assertFalse(r['phase_valid']);self.assertIsNone(r['phase_lead_b_deg'])

class DualBudgetTests(unittest.TestCase):
    def test_oversized_window_refused_before_opening_raw_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cid=fixture(root,np.zeros(1000001))
            (root/'captures'/cid/'ch1.bin').unlink()
            with self.assertRaisesRegex(ScopeError,'1000000 points'):
                derive_timing(root,cid,1,2,method='sine_fit',frequency_hz=1000)
