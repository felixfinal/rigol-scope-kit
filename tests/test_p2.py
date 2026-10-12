import asyncio
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from rigol_scope.artifacts import read_artifact_chunk
from rigol_scope.config import Device, Settings
from rigol_scope.controls import Controls, config_result
from rigol_scope.errors import ScopeError
from rigol_scope.mcp_server import create_server
from rigol_scope.service import ScopeService
from rigol_scope.waveforms import Preamble, decode_word, export_waveforms


class Fake:
    def __init__(self):
        self.commands = []
        self.device = Device("test", "127.0.0.1", 1, serial="SN")
        self.data = {
            ":TRIGGER:COUPLING":"DC", ":TRIGGER:NREJECT":"0", ":TRIGGER:HOLDOFF":"8e-9",
            ":TRIGGER:STATUS": "STOP", ":TRIGGER:MODE": "EDGE", ":TRIGGER:SWEEP": "AUTO",
            ":TRIGGER:EDGE:SOURCE": "CHAN1", ":TRIGGER:EDGE:SLOPE": "POS", ":TRIGGER:EDGE:LEVEL": "1.5",
            ":TIMEBASE:MAIN:SCALE": ".0005", ":TIMEBASE:MAIN:OFFSET": "0",
            ":ACQUIRE:TYPE": "NORM", ":ACQUIRE:MDEPTH": "10000", ":ACQUIRE:AVERAGES": "2",
            ":WAVEFORM:SOURCE": "CHAN1", ":WAVEFORM:MODE": "NORM", ":WAVEFORM:FORMAT": "WORD",
            ":WAVEFORM:START": "1", ":WAVEFORM:STOP": "1000",
            ":TRIGGER:PULSE:SOURCE":"CHAN1", ":TRIGGER:PULSE:POLARITY":"POS",
            ":TRIGGER:PULSE:WHEN":"GRE", ":TRIGGER:PULSE:LWIDTH":"0.000001",
            ":TRIGGER:PULSE:UWIDTH":"0.000002", ":TRIGGER:PULSE:LEVEL":"1.5",
        }
        for ch in range(1, 5):
            for key, value in {"DISPLAY":"1", "PROBE":"1", "IMPEDANCE":"OMEG", "COUPLING":"DC", "SCALE":"1", "OFFSET":"0", "BWLIMIT":"OFF"}.items():
                self.data[f":CHANNEL{ch}:{key}"] = value
        for ch in (1, 2):
            for key, value in {"OUTPUT:STATE":"0", "FUNCTION":"SIN", "FREQUENCY":"1000", "VOLTAGE:AMPLITUDE":"1", "VOLTAGE:OFFSET":"0", "PHASE":"0", "IMPEDANCE":"OMEG", "MOD:STATE":"0", "FUNCTION:SQUARE:DUTY":"50", "FUNCTION:RAMP:SYMMETRY":"50"}.items():
                self.data[f":SOURCE{ch}:{key}"] = value
        self.fail_block = False
    def remaining(self): return 10
    def query(self, command, **_):
        self.commands.append(command)
        if command == "*OPC?": return "1"
        if command == "*IDN?": return "RIGOL,MHO954,SN,FW"
        if command.startswith(":SYSTem:OPTion:STATus?"): return "1"
        if command.startswith(":MEASure:ITEM?"): return "1000"
        if command == ":WAVeform:PREamble?":
            count = int(self.data[":WAVEFORM:STOP"]) - int(self.data[":WAVEFORM:START"]) + 1
            mode = 2 if self.data[":WAVEFORM:MODE"] == "RAW" else 0
            return f"1,{mode},{count},1,0.000001,-0.001,0,0.001,0,32768"
        return self.data[command.rstrip("?").upper()]
    def write(self, command):
        self.commands.append(command)
        if command in {":RUN", ":STOP", ":SINGle"}:
            self.data[":TRIGGER:STATUS"] = "RUN" if command == ":RUN" else "STOP"
            return
        key, _, value = command.partition(" ")
        value = {"LFReject":"LFR", "HFReject":"HFR", "FIFTy":"FIFT", "SINusoid":"SIN", "SQUare":"SQU", "NOISe":"NOIS", "PULSe":"PULS", "CHANnel1":"CHAN1", "CHANnel2":"CHAN2"}.get(value, value)
        self.data[key.upper()] = value
    def query_binary(self, command):
        if self.fail_block: raise ScopeError("simulated transfer failure")
        start, stop = int(self.data[":WAVEFORM:START"]), int(self.data[":WAVEFORM:STOP"])
        return b"".join(struct.pack("<H",32768 + i) for i in range(start - 1, stop))


class ControlsTests(unittest.TestCase):
    def test_invalid_setting_does_not_write(self):
        t=Fake();d=Controls(t)
        for values in [{"scale_v_div":float("nan")},{"coupling":"DC;:RUN"},{"typo":1},{"enabled":1},{"probe_ratio":0}]:
            with self.assertRaises(ScopeError): d.configure_channel(1,values)
        self.assertTrue(all("?" in c for c in t.commands))

    def test_channel_setting_readback(self):
        d=Controls(Fake())
        r=d.configure_channel(1,{"scale_v_div":.5,"offset_v":-.2})
        self.assertTrue(r["verified"])
        self.assertEqual(r["actual"]["offset_v"],-.2)

    def test_timebase_and_trigger_readback(self):
        d=Controls(Fake())
        self.assertTrue(d.configure_timebase({"scale_s_div":.001,"offset_s":.002})["verified"])
        self.assertTrue(d.configure_trigger(1,1.5)["verified"])

    def test_memory_limit_tracks_channels(self):
        t=Fake();d=Controls(t)
        with self.assertRaises(ScopeError):d.configure_acquisition({"memory_depth":500000000})
        self.assertTrue(d.configure_acquisition({"memory_depth":1000000})["verified"])

    def test_pulse_thresholds_and_invalid_range(self):
        t=Fake();d=Controls(t)
        with self.assertRaises(ScopeError):d.configure_pulse_trigger(1,1.5,"between",lower_s=.002,upper_s=.001)
        self.assertFalse(t.commands)
        r=d.configure_pulse_trigger(1,1.5,"greater",width_s=.0004)
        self.assertTrue(r["verified"],r)
        self.assertEqual(r["actual"]["when"],"GRE")

    def test_single_does_not_accept_stale_stop_or_oversized_wait(self):
        t=Fake();d=Controls(t)
        t.deadline=time.monotonic()+.5
        with self.assertRaises(ScopeError):d.acquisition_control("single",10)
        self.assertFalse(t.commands)
        t.deadline=time.monotonic()+30
        r=d.acquisition_control("single",1)
        self.assertTrue(r["verified"])
        self.assertLess(t.commands.index(":RUN"),t.commands.index(":SINGle"))

    def test_capture_parameters_validated_before_hardware(self):
        with tempfile.TemporaryDirectory() as temp,patch("rigol_scope.service.TCPTransport") as connect:
            service=ScopeService(Settings({"test":Device("test","127.0.0.1",1,serial="SN")},Path(temp)))
            with self.assertRaises(ScopeError):service.capture_single("test",[1],points=-1,confirmed=True)
            with self.assertRaises(ScopeError):service.capture_single("test",[1],wait_s=0,confirmed=True)
            connect.assert_not_called()

    def test_mismatched_readback_is_not_success(self):
        self.assertFalse(config_result({}, {"x":1}, {"x":2})["verified"])

    def test_output_must_be_off_before_configure(self):
        t=Fake();t.data[":SOURCE1:OUTPUT:STATE"]="1"
        with self.assertRaises(ScopeError): Controls(t).configure_generator(1,{"frequency_hz":2000})
        self.assertTrue(all("?" in c for c in t.commands))

    def test_frequency_and_offset_limits_checked_before_write(self):
        t=Fake();d=Controls(t)
        for values in [{"waveform":"SQU","frequency_hz":100e6},{"amplitude_vpp":20,"offset_v":1},{"waveform":"SIN","duty_percent":50}]:
            with self.assertRaises(ScopeError):d.configure_generator(1,values)
        self.assertTrue(all("?" in c for c in t.commands))

    def test_generator_output_uses_fresh_hash_and_peak_bound(self):
        t=Fake();d=Controls(t)
        actual=d.configure_generator(1,{"amplitude_vpp":2,"load":"50","offset_v":.1})["actual"]
        h=actual["settings_hash"]
        with self.assertRaises(ScopeError):d.generator_output(1,True,"stale",3)
        with self.assertRaises(ScopeError):d.generator_output(1,True,h,1.1)
        self.assertTrue(d.generator_output(1,True,h,2.2)["verified"])
        self.assertTrue(d.generator_output(1,False)["verified"])

    def test_measure_invalid_is_null(self):
        t=Fake()
        with patch.object(t,"query",return_value="9.9e37"):
            r=Controls(t).measure([1],["frequency"])
        self.assertIsNone(r["measurements"][0]["value"])

    def test_mho_duty_fraction_is_converted_to_percent(self):
        t=Fake()
        with patch.object(t,"query",return_value="5.000000E-1"):
            r=Controls(t).measure([1],["duty"])["measurements"][0]
        self.assertEqual(r["value"],50)
        self.assertEqual(r["unit"],"%")
        self.assertEqual(r["raw"],"5.000000E-1")

    def test_write_authorization_before_connection(self):
        with tempfile.TemporaryDirectory() as temp,patch("rigol_scope.service.TCPTransport") as connect:
            s=ScopeService(Settings({"test":Device("test","127.0.0.1",1,serial="SN")},Path(temp)))
            with self.assertRaises(ScopeError):s.execute_control("test","configure_timebase",settings={"scale_s_div":.001})
            connect.assert_not_called()

    def test_control_mcp_argument_routing(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp,patch.object(ScopeService,"execute_control",return_value={"verified":True}) as call:
                r=await create_server(Settings({},Path(temp))).call_tool("scope_control",{"action":"stop","confirmed":True})
                self.assertFalse(r.isError)
                self.assertEqual(call.call_args.kwargs["control_method"],"acquisition_control")
                self.assertEqual(call.call_args.kwargs["action"],"stop")
        asyncio.run(run())


class WaveTests(unittest.TestCase):
    def test_little_unsigned_and_trigger_time(self):
        p=Preamble.parse("1,2,2,1,5e-7,-.00202,0,6.6667e-5,-23147,32768")
        codes=[x[0] for x in decode_word(struct.pack("<HH",9618,55926),p,2)]
        self.assertAlmostEqual(p.voltage(codes[0]),-.0002,places=7)
        self.assertAlmostEqual(p.voltage(codes[1]),3.087,places=4)
        self.assertAlmostEqual(p.time(128),-.001956)
        with self.assertRaises(ScopeError):list(decode_word(b"x",p,2))

    def test_chunk_export_full_points_time_and_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Fake();old={k:v for k,v in t.data.items() if k.startswith(":WAVEFORM:")}
            r=export_waveforms(Controls(t),Path(temp),{},[1,2],mode="RAW",points=256,start=129,chunk_points=64)
            self.assertTrue(r["verified"],r)
            root=Path(r["directory"])
            raw=(root/"ch1.bin").read_bytes()
            self.assertEqual(len(raw),512)
            self.assertEqual(struct.unpack_from("<H",raw)[0],32768+128)
            self.assertEqual(hashlib.sha256(raw).hexdigest(),r["channels"]["1"]["sha256"])
            self.assertEqual({k:v for k,v in t.data.items() if k.startswith(":WAVEFORM:")},old)
            self.assertAlmostEqual(r["channels"]["1"]["time_start_s"],-.000872)
            chunk=read_artifact_chunk(Path(temp),r["capture_id"],"ch1.bin",128,64)
            self.assertEqual(chunk["bytes"],64)

    def test_running_capture_requires_explicit_stop(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Fake();t.data[":TRIGGER:STATUS"]="RUN"
            with self.assertRaises(ScopeError):export_waveforms(Controls(t),Path(temp),{},[1])
            self.assertTrue(all("?" in c for c in t.commands))

    def test_failure_preserves_partial_metadata_and_restores(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Fake();t.fail_block=True
            r=export_waveforms(Controls(t),Path(temp),{},[1],points=100)
            self.assertFalse(r["verified"])
            self.assertEqual(r["status"],"partial")
            meta=json.loads((Path(r["directory"])/"metadata.json").read_text())
            self.assertEqual(meta["error"]["type"],"ScopeError")
            self.assertTrue(meta["restoration"]["verified"])

    def test_budget_and_window_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            for params in [{"points":1001},{"mode":"RAW","points":1000000,"max_bytes":1000},{"mode":"RAW","points":100000,"chunk_points":1}]:
                t=Fake()
                with self.assertRaises(ScopeError):export_waveforms(Controls(t),Path(temp),{},[1],**params)
                self.assertFalse(t.commands)


if __name__=="__main__": unittest.main()

class ChannelSettlingTests(unittest.TestCase):
    def test_probe_rescaling_finishes_before_explicit_range(self):
        class DeferredProbe(Fake):
            pending=None
            def write(self,command):
                if command.startswith(':CHANnel1:PROBe '):
                    old=float(self.data[':CHANNEL1:PROBE'])
                    self.pending=float(command.split()[-1])/old
                super().write(command)
            def query(self,command,**kw):
                if command=='*OPC?' and self.pending is not None:
                    self.data[':CHANNEL1:SCALE']=str(float(self.data[':CHANNEL1:SCALE'])*self.pending)
                    self.pending=None
                return super().query(command,**kw)
        t=DeferredProbe();t.data[':CHANNEL1:SCALE']='.05'
        r=Controls(t).configure_channel(1,{'probe_ratio':10,'scale_v_div':.2})
        self.assertTrue(r['verified'],r)
        self.assertEqual(r['intermediate_readbacks'][0]['actual']['scale_v_div'],.5)
        self.assertEqual(r['actual']['scale_v_div'],.2)

    def test_identical_channel_plan_sends_no_writes(self):
        t=Fake();d=Controls(t);settings=d.channel(1);t.commands.clear()
        self.assertTrue(d.configure_channel(1,settings)['verified'])
        self.assertTrue(all('?' in x for x in t.commands))

    def test_failed_probe_readback_does_not_apply_range(self):
        class RefuseProbe(Fake):
            def write(self,command):
                if ':PROBe ' in command:self.commands.append(command)
                else:super().write(command)
        t=RefuseProbe();r=Controls(t).configure_channel(1,{'probe_ratio':10,'scale_v_div':.2})
        self.assertFalse(r['verified'])
        self.assertFalse(any(':SCALe ' in x for x in t.commands))

class DeepExportTests(unittest.TestCase):
    def test_streaming_statistics_and_preview_across_chunks(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Fake();t.data[':ACQUIRE:MDEPTH']='1000000'
            result=export_waveforms(Controls(t),Path(temp),{'serial':'SN'},[1],mode='RAW',points=20001,start=31,chunk_points=7000)
            self.assertTrue(result['verified'],result)
            row=result['channels']['1']
            self.assertEqual(row['bytes_written'],40002)
            self.assertAlmostEqual(row['min_v'],.03)
            self.assertAlmostEqual(row['max_v'],20.03)
            self.assertAlmostEqual(row['mean_v'],10.03)
            self.assertEqual(len(row['preview']),128)
            for index,(ts,v) in enumerate(row['preview']):
                self.assertAlmostEqual(v,(30+index*(20001//128))*.001)
            self.assertGreaterEqual(result['performance']['statistics_s'],0)

    def test_budget_reserved_before_next_block(self):
        with tempfile.TemporaryDirectory() as temp:
            t=Fake();t.deadline=time.monotonic()+2
            r=export_waveforms(Controls(t),Path(temp),{'serial':'SN'},[1],points=1000)
            self.assertFalse(r['verified'])
            self.assertTrue(r['restoration']['verified'])
            self.assertEqual(r['status'],'partial')

    def test_timeout_bound_validated_before_connect(self):
        with tempfile.TemporaryDirectory() as temp,patch('rigol_scope.service.TCPTransport') as connect:
            service=ScopeService(Settings({'test':Device('test','127.0.0.1',1,serial='SN')},Path(temp)))
            for value in [float('nan'),True,901,0]:
                with self.assertRaises(ScopeError):service.waveform_export('test',confirmed=True,channels=[1],operation_timeout_s=value)
            connect.assert_not_called()

class StaleTransferWindowTests(unittest.TestCase):
    def test_invalid_old_window_is_normalized_and_disclosed(self):
        class StaleWindow(Fake):
            def write(self,command):
                if command==':WAVeform:MODE NORM':
                    super().write(command)
                    self.data[':WAVEFORM:START']='1';self.data[':WAVEFORM:STOP']='1000'
                    return
                if command.startswith((':WAVeform:STARt ',':WAVeform:STOP ')) and int(self.data[':WAVEFORM:STOP'])>int(self.data[':ACQUIRE:MDEPTH']):
                    self.commands.append(command);return
                super().write(command)
        with tempfile.TemporaryDirectory() as tmp:
            t=StaleWindow();t.data.update({':WAVEFORM:MODE':'RAW',':WAVEFORM:START':'15000001',':WAVEFORM:STOP':'16000000'})
            r=export_waveforms(Controls(t),Path(tmp),{'serial':'SN'},[1],mode='RAW',points=1000)
            self.assertTrue(r['verified'],r)
            self.assertTrue(r['restoration']['previous_window_invalid'])
            self.assertFalse(r['restoration']['previous_settings_restored'])
            self.assertEqual(r['restoration']['actual']['STARt'],'1')
