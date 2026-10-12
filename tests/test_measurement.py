import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from rigol_scope.config import Device, Settings
from rigol_scope.contracts import MeasurementRequest, get_contract
from rigol_scope.controls import Controls
from rigol_scope.errors import ScopeError
from rigol_scope.measurement import snapshot, resolve, quality_report, read_receipt
from rigol_scope.profiles import get_profile, profiles
from rigol_scope.service import ScopeService
from rigol_scope.mcp_server import create_server
from test_p2 import Fake


class Instrument(Fake):
    def __init__(self,device=None,deadline=None):
        super().__init__()
        self.device=device or Device("test","127.0.0.1",1,serial="SN")
        self.deadline=deadline or time.monotonic()+30
        self.data[":ACQUIRE:SRATE"]="200000000"
    def __enter__(self):return self
    def __exit__(self,*_):pass


def request(profile_id="calibration-ch1-10x", overrides=None, expected_signal=None):
    p=get_profile(None,profile_id)
    override={"capture":{"points":1000,"screenshot":False}}
    if overrides:
        for key,value in overrides.items():
            override[key]=override.get(key,{})|value
    return MeasurementRequest(contract_version="1.0.0",profile_id=p["profile_id"],profile_version=p["version"],profile_digest=p["digest"],
                              overrides=override,expected_signal=expected_signal or {})


class ProfileTests(unittest.TestCase):
    def test_contract_schemas_and_all_defaults_are_discoverable(self):
        c=get_contract()
        self.assertEqual(c["contract_version"],"1.0.0")
        self.assertIn("profile_digest",c["request_schema"]["required"])
        p=profiles()
        self.assertEqual(set(p),{"passive-10x","calibration-ch1-10x"})
        self.assertEqual(p["passive-10x"]["defaults"]["channel_defaults"]["impedance"],"1M")
        self.assertEqual(p["passive-10x"]["defaults"]["acquisition"]["memory_depth"],1000000)

    def test_stale_profile_fails_before_hardware(self):
        with tempfile.TemporaryDirectory() as temp,patch("rigol_scope.service.TCPTransport") as connect:
            service=ScopeService(Settings({"test":Device("test","127.0.0.1",1,serial="SN")},Path(temp)))
            req=request().model_dump(mode="json");req["profile_digest"]="0"*64
            with self.assertRaisesRegex(ScopeError,"DIGEST"):service.measurement_preview("test",req)
            req["profile_version"]="9.0.0"
            with self.assertRaisesRegex(ScopeError,"VERSION"):service.measurement_capture("test",req,confirmed=True)
            connect.assert_not_called()

    def test_operator_profile_digest_changes_with_same_version(self):
        with tempfile.TemporaryDirectory() as temp:
            p=get_profile(None,"passive-10x")
            for key in ("origin","digest"):p.pop(key)
            p["profile_id"]="project-test"
            path=Path(temp)/"project.json";path.write_text(json.dumps(p))
            before=get_profile(Path(temp),"project-test")
            p["defaults"]["acquisition"]["memory_depth"]=100000
            path.write_text(json.dumps(p))
            after=get_profile(Path(temp),"project-test")
            self.assertNotEqual(before["digest"],after["digest"])
            with self.assertRaisesRegex(ScopeError,"DIGEST"):get_profile(Path(temp),"project-test","1.0.0",before["digest"])

    def test_unknown_fields_and_nonfinite_values_rejected(self):
        r=request().model_dump(mode="json")
        r["overrides"]["channel_defaults"]["invented"]=1
        with self.assertRaises(ValidationError):MeasurementRequest.model_validate(r)
        r=request().model_dump(mode="json");r["overrides"]["timebase"]["offset_s"]=float("nan")
        with self.assertRaises(ValidationError):MeasurementRequest.model_validate(r)

    def test_override_profile_and_live_precedence(self):
        t=Instrument();d=Controls(t)
        req=request("passive-10x",{"channels":{"1":{"scale_v_div":.5}}})
        plan=resolve(get_profile(None,req.profile_id),req,snapshot(d),d.options(),t.device)
        self.assertEqual(plan["effective"]["channels"]["1"]["probe_ratio"],10)
        self.assertEqual(plan["parameter_origins"] if "parameter_origins" in plan else plan["origins"]["channels.1.scale_v_div"],"override.channels.1")
        self.assertEqual(plan["origins"]["channels.1.offset_v"],"live_readback")
        self.assertEqual(plan["origins"]["channels.1.impedance"],"profile.channel_defaults")
        self.assertEqual(plan["effective"]["capture"]["points"],1000)
        self.assertTrue(all("?" in command for command in t.commands))

    def test_pulse_override_and_bounds(self):
        t=Instrument();d=Controls(t)
        req=request(overrides={"trigger":{"kind":"pulse","condition":"greater","width_s":.0004}})
        plan=resolve(get_profile(None,req.profile_id),req,snapshot(d),d.options(),t.device)
        self.assertEqual(plan["effective"]["trigger"]["lower_s"],.0004)
        self.assertEqual(plan["effective"]["trigger"]["upper_s"],10)
        self.assertNotIn("slope",plan["effective"]["trigger"])
        bad=request(overrides={"trigger":{"kind":"pulse","condition":"between","lower_s":.001,"upper_s":.0005}})
        with self.assertRaises(ScopeError):resolve(get_profile(None,bad.profile_id),bad,snapshot(d),d.options(),t.device)
        self.assertTrue(all("?" in command for command in t.commands))


class WorkflowTests(unittest.TestCase):
    def setup_service(self,temp,t):
        s=Settings({"test":t.device},Path(temp))
        return ScopeService(s)

    def test_preview_no_writes_and_changed_preserved_state_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Instrument();service=self.setup_service(temp,t)
            with patch("rigol_scope.service.TCPTransport",return_value=t):
                r=service.measurement_preview("test",request("passive-10x"))
                self.assertTrue(all("?" in command for command in t.commands))
                t.data[":CHANNEL1:SCALE"]="2"
                with self.assertRaisesRegex(ScopeError,"PLAN_CHANGED"):
                    service.measurement_capture("test",request("passive-10x"),confirmed=True,preview_digest=r["plan_digest"])
                self.assertTrue(all("?" in command for command in t.commands))

    def test_atomic_capture_receipt_and_context(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Instrument();service=self.setup_service(temp,t)
            with patch("rigol_scope.service.TCPTransport",return_value=t):
                r=service.measurement_capture("test",request(),confirmed=True)
            self.assertTrue(r["verified"],r)
            self.assertEqual(r["acquisition_after"],"STOP")
            self.assertEqual(r["profile_ref"]["version"],"1.0.0")
            self.assertEqual(r["parameter_origins"]["capture.points"],"override.capture")
            saved=json.loads(read_receipt(Path(temp),r["operation_id"]))
            self.assertEqual(saved["plan_digest"],r["plan_digest"])
            meta=json.loads((Path(r["capture"]["directory"])/"metadata.json").read_text())
            self.assertEqual(meta["measurement_context"]["profile_ref"],r["profile_ref"])
            self.assertFalse(any(command.startswith(":SOURCE") for command in t.commands))

    def test_all_groups_validated_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Instrument();service=self.setup_service(temp,t)
            req=request(overrides={"channel_defaults":{"scale_v_div":.5},"capture":{"points":2000000}})
            with patch("rigol_scope.service.TCPTransport",return_value=t):
                with self.assertRaises(ScopeError):service.measurement_capture("test",req,confirmed=True)
            self.assertTrue(all("?" in command for command in t.commands))

    def test_readback_failure_stops_before_single(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Instrument();service=self.setup_service(temp,t)
            with patch("rigol_scope.service.TCPTransport",return_value=t),patch.object(Controls,"configure_timebase",return_value={"verified":False,"actual":{}}):
                r=service.measurement_capture("test",request(),confirmed=True)
            self.assertFalse(r["verified"])
            self.assertEqual(r["failure"]["stage"],"timebase")
            self.assertNotIn(":SINGle",t.commands)
            self.assertEqual(t.data[":TRIGGER:STATUS"],"STOP")

    def test_exported_resolution_used_for_quality(self):
        t=Instrument();d=Controls(t)
        req=request(expected_signal={"channels":{"1":{"min_event_width_s":1e-6,"min_samples_per_event":20}}})
        plan=resolve(get_profile(None,req.profile_id),req,snapshot(d),d.options(),t.device)
        q=quality_report(plan,200e6,{"channels":{"1":{"points_written":1000,"time_start_s":-.0025,"time_end_s":.002495}}})
        self.assertEqual(q["channels"]["1"]["instrument_samples_per_event"],200)
        self.assertLess(q["channels"]["1"]["exported_samples_per_event"],1)
        self.assertTrue(any(x["code"]=="EXPECTED_EVENT_UNDERSAMPLED" for x in q["findings"]))

    def test_shared_lock_covers_configuration_to_export(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Instrument();service=self.setup_service(temp,t)
            entered=threading.Event();release=threading.Event();other_done=threading.Event();errors=[]
            from rigol_scope.measurement import export_waveforms as real_export
            def held_export(*a,**kw):
                entered.set()
                if not release.wait(3):raise RuntimeError("test gate timeout")
                return real_export(*a,**kw)
            def capture():
                try:service.measurement_capture("test",request(),confirmed=True)
                except Exception as exc:errors.append(exc)
            def reader():
                try:service.identify("test");other_done.set()
                except Exception as exc:errors.append(exc)
            with patch("rigol_scope.service.TCPTransport",return_value=t),patch("rigol_scope.measurement.export_waveforms",side_effect=held_export):
                a=threading.Thread(target=capture);a.start()
                self.assertTrue(entered.wait(2))
                b=threading.Thread(target=reader);b.start()
                self.assertFalse(other_done.wait(.1))
                release.set();a.join(3);b.join(3)
                self.assertTrue(other_done.is_set())
                self.assertFalse(errors,errors)

    def test_mcp_contract_is_offline_and_request_schema_present(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp,patch("rigol_scope.service.TCPTransport") as connect:
                mcp=create_server(Settings({},Path(temp)))
                r=await mcp.call_tool("scope_contract",{})
                body=r.structuredContent if hasattr(r,"structuredContent") else json.loads(r[0].text)
                self.assertIn("contract_version",body)
                tools=await mcp.list_tools()
                tool=next(x for x in tools if x.name=="scope_measurement_capture")
                self.assertIn("request",tool.inputSchema["properties"])
                connect.assert_not_called()
        asyncio.run(run())

    def test_mcp_typed_request_reaches_service(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp,patch.object(ScopeService,"measurement_preview",return_value={"valid":True}) as preview:
                mcp=create_server(Settings({},Path(temp)))
                result=await mcp.call_tool("scope_measurement_preview",{"device":"test","request":request().model_dump(mode="json")})
                self.assertFalse(result.isError)
                self.assertIsInstance(preview.call_args.kwargs["request"],MeasurementRequest)
        asyncio.run(run())

    def test_mcp_stale_profile_reports_no_writes(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp,patch("rigol_scope.service.TCPTransport") as connect:
                mcp=create_server(Settings({"test":Device("test","127.0.0.1",1,serial="SN")},Path(temp)))
                req=request().model_dump(mode="json");req["profile_digest"]="0"*64
                r=await mcp.call_tool("scope_measurement_capture",{"device":"test","request":req,"confirmed":True})
                self.assertTrue(r.isError)
                self.assertEqual(r.structuredContent["writes_sent"],0)
                self.assertEqual(r.structuredContent["stage"],"preflight")
                connect.assert_not_called()
        asyncio.run(run())


if __name__=="__main__":unittest.main()

class SkillResourceTests(unittest.TestCase):
    def test_bundled_skill_resources_and_traversal_rejection(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp,patch('rigol_scope.service.TCPTransport') as connect:
                mcp=create_server(Settings({'test':Device('test','127.0.0.1',1,serial='SN')},Path(temp)))
                entry=list(await mcp.read_resource('scope://skill/SKILL.md'))
                self.assertIn('scope_measurement_capture',entry[0].content)
                reference=list(await mcp.read_resource('scope://skill/references/measurement-contract.md'))
                self.assertIn('profile_digest',reference[0].content)
                with self.assertRaises(Exception):await mcp.read_resource('scope://skill/references/..%2F..%2Fconfig.py')
                connect.assert_not_called()
        asyncio.run(run())
