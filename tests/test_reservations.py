import json,os,signal,socket,socketserver,threading,time,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from rigol_scope.config import Device,Settings
from rigol_scope.controls import Controls
from rigol_scope.errors import ScopeError,BusyError
from rigol_scope.profiles import get_profile
from rigol_scope.service import ScopeService
from rigol_scope.reservations import load_state
from test_p2 import Fake

class TriggerOptionTests(unittest.TestCase):
    def test_options_bounds_readback_and_skip_unchanged(self):
        t=Fake();d=Controls(t)
        r=d.configure_trigger_options({'coupling':'HFR','noise_reject':True,'holdoff_s':1e-6})
        self.assertTrue(r['verified'],r);t.commands.clear()
        self.assertTrue(d.configure_trigger_options(r['actual'])['verified'])
        self.assertTrue(all('?' in x for x in t.commands))
        for settings in [{'holdoff_s':1e-10},{'holdoff_s':11},{'noise_reject':1},{'coupling':'bad'}]:
            t.commands.clear()
            with self.assertRaises(ScopeError):d.configure_trigger_options(settings)
            self.assertTrue(all('?' in x for x in t.commands))
    def test_digital_source_options_are_rejected_before_writes(self):
        t=Fake();t.data[":TRIGGER:EDGE:SOURCE"]="D0"
        with self.assertRaises(ScopeError):Controls(t).configure_trigger_options({"noise_reject":True})
        self.assertTrue(all("?" in x for x in t.commands))
    def test_coupling_is_edge_only_and_validated_before_mode_write(self):
        t=Fake();d=Controls(t);t.data[':TRIGGER:MODE']='PULS'
        with self.assertRaises(ScopeError):d.configure_trigger_options({'coupling':'AC'})
        self.assertTrue(all('?' in x for x in t.commands))
        self.assertTrue(d.configure_trigger(1,1,coupling='AC',noise_reject=True,holdoff_s=.001)['verified'])
        self.assertTrue(d.configure_pulse_trigger(1,1,width_s=.0001,noise_reject=False,holdoff_s=8e-9)['verified'])

class Station(Fake):
    def __init__(self):
        super().__init__();self.mutex=threading.RLock();self.early=False;self.late=False;self.connections=0;self.read_delay=0
        self.data.update({':ACQUIRE:SRATE':'1000000',':SYSTEM:LOWPOWER':'0',':SYSTEM:ERROR':'0,"No error"'})
    def write(self,cmd):
        if cmd==':SINGle':
            self.commands.append(cmd);self.data[':TRIGGER:STATUS']='STOP' if self.early else 'WAIT';return
        super().write(cmd)
        if ':OUTPut:STATe 1' in cmd and self.data[':TRIGGER:STATUS']=='WAIT':self.data[':TRIGGER:STATUS']='STOP'
    def query(self,cmd,**kw):
        if self.late and cmd==':ACQuire:SRATe?' and self.data[':TRIGGER:STATUS']=='WAIT':self.data[':TRIGGER:STATUS']='STOP'
        return super().query(cmd,**kw)
    def query_binary(self,cmd):
        if self.read_delay:time.sleep(self.read_delay)
        return super().query_binary(cmd)

class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        station=self.server.station
        station.connections+=1
        while raw:=self.rfile.readline():
            cmd=raw.decode().strip()
            try:
                with station.mutex:
                    if cmd==':WAVeform:DATA?':
                        data=station.query_binary(cmd);size=str(len(data)).encode();answer=b'#'+str(len(size)).encode()+size+data+b'\n'
                    elif '?' in cmd:answer=(station.query(cmd)+'\n').encode()
                    else:station.write(cmd);continue
                self.wfile.write(answer);self.wfile.flush()
            except (OSError,KeyError):break

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.station=Station();self.server=Server(('127.0.0.1',0),Handler);self.server.station=self.station
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.settings=Settings({'test':Device('test','127.0.0.1',self.server.server_address[1],serial='SN')},self.root)
        self.service=ScopeService(self.settings);self.handles=[]
        p=get_profile(None,'calibration-ch1-10x')
        self.request={'contract_version':'1.0.0','profile_id':p['profile_id'],'profile_version':p['version'],'profile_digest':p['digest'],
                      'overrides':{'capture':{'points':1000,'screenshot':False}}}
    def tearDown(self):
        for handle in self.handles:
            try:self.service.prepared_cancel('test',handle['reservation_id'],handle['reservation_token'],confirmed=True)
            except Exception:pass
        self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
    def prepare(self,**kw):
        h=self.service.prepare_capture('test',self.request,confirmed=True,lease_s=kw.pop('lease_s',10),**kw)
        self.handles.append(h);return h
    def args(self,h):return ('test',h['reservation_id'],h['reservation_token'])
    def terminal(self,h,timeout=8):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            s=self.service.prepared_status(*self.args(h))
            if s['status'] in {'collected','cancelled','expired','failed'}:return s
            time.sleep(.05)
        self.fail('Owner did not reach terminal state')
    def test_ready_busy_wrong_owner_stimulus_collect_and_repeat(self):
        h=self.prepare();self.assertTrue(h['ready'],h);self.assertEqual(h['trigger_state'],'WAIT')
        state=load_state(self.settings,h['reservation_id'])
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as broken:broken.connect(state['socket_path'])
        time.sleep(.1)
        self.assertTrue(self.service.prepared_status(*self.args(h))['ready'])
        connections=self.station.connections
        with self.assertRaises(BusyError):self.service.status('test')
        self.assertEqual(self.station.connections,connections)
        with self.assertRaises(ScopeError):self.service.prepared_collect('test',h['reservation_id'],'wrong')
        waiting=self.service.prepared_collect(*self.args(h),wait_s=.1)
        self.assertFalse(waiting['capture_complete']);self.assertEqual(waiting['status'],'ready')
        source=Controls(self.station).generator_channel(1)
        self.service.prepared_generator_output(*self.args(h),channel=1,enabled=True,confirmed=True,expected_settings_hash=source['settings_hash'],max_open_circuit_v=.6)
        result=self.service.prepared_collect(*self.args(h),wait_s=1)
        self.assertEqual(result['status'],'collected',result);self.assertTrue(result['capture_complete']);self.assertTrue(result['cleanup_complete'])
        self.assertEqual(self.station.data[':SOURCE1:OUTPUT:STATE'],'0');self.assertEqual(self.station.data[':ACQUIRE:MDEPTH'],'AUTO')
        repeated=self.service.prepared_collect(*self.args(h))
        self.assertEqual(result['measurement']['capture']['capture_id'],repeated['measurement']['capture']['capture_id'])
        from rigol_scope.reservations import read_public_receipt
        receipt=json.loads(read_public_receipt(self.settings,h['reservation_id']))
        self.assertEqual(receipt['stimulus_wait_remaining_s'],0)
        self.assertTrue(receipt['cleanup_complete']);self.assertNotIn('token_hash',receipt)
        self.assertEqual(self.station.commands.count(':SINGle'),1)
        with self.assertRaises(ScopeError):self.service.prepared_generator_output(*self.args(h),channel=1,enabled=True,confirmed=True,expected_settings_hash=source['settings_hash'],max_open_circuit_v=.6)
        for f in self.root.rglob('*.json'):self.assertNotIn(h['reservation_token'],f.read_text())
    def test_expiry_without_further_calls_cleans_up(self):
        h=self.prepare(lease_s=5);self.assertTrue(h['ready'])
        s=self.terminal(h,8);self.assertEqual(s['status'],'expired',s);self.assertTrue(s['cleanup_complete'])
        self.assertEqual(self.station.data[':TRIGGER:STATUS'],'STOP');self.assertEqual(self.station.data[':ACQUIRE:MDEPTH'],'AUTO')
    def test_cancel_and_crashed_owner_recovery(self):
        h=self.prepare();s=self.service.prepared_cancel(*self.args(h),confirmed=True)
        s=self.terminal(h);self.assertEqual(s['status'],'cancelled');self.assertTrue(s['cleanup_complete'])
        h=self.prepare();state=load_state(self.settings,h['reservation_id']);os.kill(state['pid'],signal.SIGKILL);time.sleep(.15)
        s=self.service.prepared_cancel(*self.args(h),confirmed=True)
        self.assertEqual(s['status'],'cancelled');self.assertTrue(s['verified']);self.assertIn('recovery',s)
    def test_early_capture_never_reports_ready(self):
        self.station.early=True;h=self.prepare()
        self.assertEqual(h['status'],'failed',h);self.assertFalse(h['ready']);self.assertIsNone(h['measurement']['capture'])
        self.assertIn('TRIGGER_BEFORE_READY',h['measurement']['failure']['message'])
    def test_trigger_between_wait_and_readiness_is_rejected(self):
        self.station.late=True;h=self.prepare()
        self.assertEqual(h['status'],'failed');self.assertFalse(h['ready'])
        self.assertIn('TRIGGER_BEFORE_READY',h['measurement']['failure']['message'])
    def test_bad_profile_and_unconfirmed_do_not_spawn(self):
        with patch('rigol_scope.reservations.subprocess.Popen') as child:
            with self.assertRaises(ScopeError):self.service.prepare_capture('test',self.request)
            req=dict(self.request,profile_digest='0'*64)
            with self.assertRaises(ScopeError):self.service.prepare_capture('test',req,confirmed=True)
            child.assert_not_called()
    def test_cancellation_during_export_finishes_at_block_boundary(self):
        self.request['overrides']['capture']['chunk_points']=100
        self.station.read_delay=.15;h=self.prepare()
        self.station.data[':TRIGGER:STATUS']='STOP'
        results=[]
        thread=threading.Thread(target=lambda:results.append(self.service.prepared_collect(*self.args(h),wait_s=1)))
        thread.start();until=time.monotonic()+3
        while time.monotonic()<until and self.service.prepared_status(*self.args(h))['status']!='collecting':time.sleep(.02)
        self.service.prepared_cancel(*self.args(h),confirmed=True)
        thread.join(8);self.assertFalse(thread.is_alive())
        result=self.terminal(h)
        self.assertEqual(result['status'],'cancelled',result);self.assertTrue(result['cleanup_complete']);self.assertFalse(result['capture_complete'])

class PublicStateTests(unittest.TestCase):
    def test_ready_is_false_after_expiry_even_before_cleanup_publishes(self):
        from rigol_scope.reservations import public
        r=public({'reservation_id':'a'*32,'status':'ready','ready':True,'expires_at':'2000-01-01T00:00:00+00:00','token_hash':'secret','socket_path':'private'})
        self.assertFalse(r['ready']);self.assertEqual(r['stimulus_wait_remaining_s'],0)
        self.assertNotIn('token_hash',r);self.assertNotIn('socket_path',r)
