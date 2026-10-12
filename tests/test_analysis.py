import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from rigol_scope.analysis import WaveWindow, envelope, load_window, signal_analysis, search_events
from rigol_scope.artifacts import read_artifact, read_artifact_chunk
from rigol_scope.catalog import list_captures, capture_info
from rigol_scope.config import Settings
from rigol_scope.errors import ScopeError
from rigol_scope.mcp_server import create_server
from rigol_scope.reports import derive


def fixture(root, y, dt=1e-6, t0=-.001, index=1, status="complete"):
    cid=f"{index:032x}"
    d=root/"captures"/cid;d.mkdir(parents=True)
    codes=np.round(np.asarray(y)/.001+32768).astype("<u2")
    raw=codes.tobytes();(d/"ch1.bin").write_bytes(raw)
    pre={"format":1,"mode":2,"points":len(y),"count":1,"x_increment":dt,
         "x_origin":t0,"x_reference":0,"y_increment":.001,"y_origin":0,"y_reference":32768}
    meta={"schema_version":2,"kind":"waveform","capture_id":cid,"created_at":f"2020-01-01T00:00:{index:02d}+00:00",
          "status":status,"identity":{"serial":"TEST","model":"MHO954"},"format":"WORD","dtype":"uint16","byteorder":"little",
          "start_index_one_based":1,"channels":{"1":{"points_written":len(y),"bytes_written":len(raw),
          "sha256":hashlib.sha256(raw).hexdigest(),"chunks":[{"start":1,"stop":len(y),"preamble":pre}]}}}
    (d/"metadata.json").write_text(json.dumps(meta))
    return cid


def trapezoid(n=6000):
    p=np.arange(n)%1000
    return np.where(p<100,p*.03,np.where(p<500,3,np.where(p<600,(600-p)*.03,0)))


class AnalysisTests(unittest.TestCase):
    def test_known_frequency_duty_and_rise_fall(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            w=load_window(r,cid)
            m,p,q,_,_=signal_analysis(w,low_level_v=0,high_level_v=3)
            self.assertAlmostEqual(m["frequency_hz"],1000,places=6)
            self.assertAlmostEqual(m["duty_percent"],50,places=6)
            self.assertAlmostEqual(m["positive_width_s"],.0005,places=9)
            self.assertAlmostEqual(m["rise_time_s"],.00008,places=9)
            self.assertAlmostEqual(m["fall_time_s"],.00008,places=9)
            self.assertGreaterEqual(q["complete_periods"],4)

    def test_window_time_axis_and_point_selection(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            w=load_window(r,cid,start_s=0,end_s=.0001)
            self.assertEqual(w.first_index,1000)
            self.assertEqual(len(w.volts),101)
            self.assertAlmostEqual(w.t0,0)
            with self.assertRaises(ScopeError):load_window(r,cid,start_s=20)
            with self.assertRaises(ScopeError):load_window(r,cid,start_s=float("nan"))

    def test_falling_period_when_window_truncates_rising_cycle(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid(2050)[50:])
            m,p,q,_,_=signal_analysis(load_window(r,cid),low_level_v=0,high_level_v=3)
            self.assertEqual(q["rising_edges"],1)
            self.assertEqual(q["falling_edges"],2)
            self.assertEqual(p["period_edge"],"falling")
            self.assertAlmostEqual(m["frequency_hz"],1000,places=6)
            self.assertAlmostEqual(m["duty_percent"],50,places=6)

    def test_envelope_preserves_single_sample_spike(self):
        y=np.zeros(10000);y[1234]=3
        w=WaveWindow("a"*32,1,y,1e-6,0,0,"hash",20000,10000,32768,35768)
        x,lo,hi=envelope(w,100)
        self.assertEqual(len(x),100)
        self.assertEqual(hi[12],3)
        self.assertEqual(lo[12],0)

    def test_dc_reports_unavailable_timing(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,np.ones(1000))
            m,p,q,times,pol=signal_analysis(load_window(r,cid))
            self.assertIsNone(m["frequency_hz"])
            self.assertIsNone(m["duty_percent"])
            self.assertFalse(len(times))
            self.assertIn("Insufficient",q["warnings"][0])

    def test_short_pulse_search_and_unresolved_edge(self):
        y=np.zeros(6000);y[2000:2005]=3
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,y);w=load_window(r,cid)
            m,p,q,times,pol=signal_analysis(w,low_level_v=0,high_level_v=3)
            found=search_events(w,times,pol,"pulse_width",minimum=10e-6)
            self.assertEqual(found["match_count"],1)
            self.assertAlmostEqual(found["events"][0]["duration_s"],5e-6)
            self.assertIsNone(m["rise_time_s"])
            self.assertGreater(q["rise_time_s_unresolved_edges"],0)

    def test_missing_cycle_and_voltage_excursion(self):
        y=trapezoid();y[2000:3000]=0
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,y);w=load_window(r,cid)
            m,p,q,times,pol=signal_analysis(w,low_level_v=0,high_level_v=3)
            gap=search_events(w,times,pol,"edge_gap",maximum=.0011)
            self.assertEqual(gap["match_count"],1)
            self.assertAlmostEqual(gap["events"][0]["duration_s"],.002)
            self.assertGreater(q["period_coefficient_of_variation"],.05)
            over=search_events(w,times,pol,"voltage_range",maximum=2.5,limit=2)
            self.assertEqual(over["match_count"],5)
            self.assertTrue(over["truncated"])
            self.assertEqual(over["returned_count"],2)

    def test_checksum_and_partial_capture_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            p=r/"captures"/cid/"ch1.bin";p.write_bytes(b"\x00\x00"+p.read_bytes()[2:])
            with self.assertRaisesRegex(ScopeError,"SHA-256"):load_window(r,cid)
            cid=fixture(r,trapezoid(),index=2,status="partial")
            with self.assertRaisesRegex(ScopeError,"complete"):load_window(r,cid)

    def test_noncontiguous_metadata_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            p=r/"captures"/cid/"metadata.json";m=json.loads(p.read_text())
            m["channels"]["1"]["chunks"][0]["start"]=2;p.write_text(json.dumps(m))
            with self.assertRaisesRegex(ScopeError,"non-contiguous"):load_window(r,cid)

    def test_large_window_rejected_without_silent_downsampling(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,np.zeros(2_000_001))
            with self.assertRaisesRegex(ScopeError,"2000000"):load_window(r,cid)
            w=load_window(r,cid,start_s=0,end_s=.0001)
            self.assertEqual(len(w.volts),101)

    def test_uncommitted_derivative_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            name="plot-"+("a"*32)+".png"
            (r/"captures"/cid/name).write_bytes(b"orphan")
            with self.assertRaisesRegex(ScopeError,"not committed"):read_artifact(r,cid,name)
            self.assertNotIn(name,[x["name"] for x in capture_info(r,cid)["artifacts"]])

    def test_catalog_filters_and_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp)
            for i in range(1,4):fixture(r,trapezoid(),index=i)
            fixture(r,trapezoid(),index=4,status="partial")
            page=list_captures(r,limit=2,serial="TEST")
            self.assertEqual([int(x["capture_id"],16) for x in page["captures"]],[3,2])
            tail=list_captures(r,limit=2,serial="TEST",cursor=page["next_cursor"])
            self.assertEqual([int(x["capture_id"],16) for x in tail["captures"]],[1])
            with self.assertRaises(ScopeError):list_captures(r,serial="OTHER",cursor=page["next_cursor"])
            self.assertFalse(list_captures(r,channel=2)["captures"])
            self.assertEqual(len(list_captures(r,status=None)["captures"]),4)

    def test_symlink_directory_and_raw_file_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            target=r/"outside";(r/"captures"/cid).rename(target)
            (r/"captures"/cid).symlink_to(target)
            with self.assertRaises(ScopeError):load_window(r,cid)
            self.assertFalse(list_captures(r)["captures"])

    def test_report_png_csv_and_original_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            r=Path(temp);cid=fixture(r,trapezoid())
            before={p.name:p.read_bytes() for p in (r/"captures"/cid).iterdir()}
            result=derive(r,cid,export_csv=True)
            self.assertFalse(result["instrument_access"])
            for name,data in before.items():self.assertEqual((r/"captures"/cid/name).read_bytes(),data)
            png=read_artifact(r,cid,result["plot_file"])
            self.assertTrue(png.startswith(b"\x89PNG"))
            self.assertEqual(hashlib.sha256(png).hexdigest(),result["artifacts"][result["plot_file"]]["sha256"])
            info=capture_info(r,cid)
            names=[f["name"] for f in info["artifacts"]]
            self.assertIn(result["analysis_file"],names)
            self.assertTrue(any(name.startswith("window-") for name in names))
            self.assertEqual(read_artifact_chunk(r,cid,result["report_file"])["encoding"],"base64")

    def test_offline_mcp_never_constructs_hardware_service(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                r=Path(temp);cid=fixture(r,trapezoid())
                with patch("rigol_scope.mcp_server.ScopeService",side_effect=AssertionError("hardware forbidden")):
                    server=create_server(Settings({},r))
                    reply=await server.call_tool("scope_waveform_analyze",{"capture_id":cid})
                    self.assertFalse(reply.isError)
                    self.assertFalse(reply.structuredContent["instrument_access"])
                    self.assertEqual(reply.content[1].type,"image")
                    bad=await server.call_tool("scope_waveform_analyze",{"capture_id":"../bad"})
                    self.assertTrue(bad.isError)
        asyncio.run(run())


if __name__=="__main__":unittest.main()
