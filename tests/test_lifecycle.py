import time
import unittest
from rigol_scope.controls import Controls
from rigol_scope.errors import ScopeError
from test_p2 import Fake

class PowerFake(Fake):
    def __init__(self):
        super().__init__();self.data[':SYSTEM:LOWPOWER']='1';self.data[':SYSTEM:ERROR']='0,"No error"';self.deadline=time.monotonic()+30

class LifecycleTests(unittest.TestCase):
    def test_wake_reads_back_and_does_not_enable_outputs(self):
        t=PowerFake();r=Controls(t).wake()
        self.assertTrue(r['verified']);self.assertTrue(r['before']['low_power']);self.assertFalse(r['actual']['low_power'])
        self.assertEqual([x for x in t.commands if '?' not in x],[':SYSTem:LOWPower 0'])
        t.commands.clear();self.assertEqual(Controls(t).wake()['writes_sent'],0)
        self.assertTrue(all('?' in x for x in t.commands))
    def test_release_closes_unused_and_sets_auto(self):
        t=PowerFake();t.data[':SYSTEM:LOWPOWER']='0';t.data[':SOURCE1:OUTPUT:STATE']='1';t.data[':SOURCE2:OUTPUT:STATE']='1'
        r=Controls(t).release([2]);self.assertTrue(r['verified'],r)
        self.assertEqual(t.data[':SOURCE1:OUTPUT:STATE'],'0');self.assertEqual(t.data[':SOURCE2:OUTPUT:STATE'],'1')
        self.assertEqual(t.data[':ACQUIRE:MDEPTH'],'AUTO')
        t.commands.clear();self.assertTrue(Controls(t).release([2])['verified']);self.assertTrue(all('?' in x for x in t.commands))
    def test_release_default_closes_both(self):
        t=PowerFake();t.data[':SYSTEM:LOWPOWER']='0'
        for ch in [1,2]:t.data[f':SOURCE{ch}:OUTPUT:STATE']='1'
        self.assertTrue(Controls(t).release()['verified'])
        for ch in [1,2]:self.assertEqual(t.data[f':SOURCE{ch}:OUTPUT:STATE'],'0')
    def test_release_sleep_and_invalid_request_no_writes(self):
        t=PowerFake();d=Controls(t)
        self.assertFalse(d.release()['verified'])
        for keep in [[True],[3],[1,1],'1']:
            with self.assertRaises(ScopeError):d.release(keep)
        self.assertTrue(all('?' in x for x in t.commands))
    def test_release_failure_is_not_success(self):
        class RefuseOff(PowerFake):
            def write(self,command):
                if ':OUTPut:STATe ' in command:self.commands.append(command)
                else:super().write(command)
        t=RefuseOff();t.data[':SYSTEM:LOWPOWER']='0';t.data[':SOURCE1:OUTPUT:STATE']='1'
        r=Controls(t).release();self.assertFalse(r['verified']);self.assertTrue(r['errors']);self.assertEqual(t.data[':ACQUIRE:MDEPTH'],'AUTO')

class WakeServiceTests(unittest.TestCase):
    def test_wake_is_only_pre_identity_write_and_pinned(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from rigol_scope.config import Settings,Device
        from rigol_scope.service import ScopeService
        class Sleeping(PowerFake):
            def __init__(self,device,deadline):
                super().__init__();self.device=device;self.deadline=deadline
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def query(self,cmd,**kw):
                if cmd=='*IDN?' and self.data[':SYSTEM:LOWPOWER']=='1':raise ScopeError('asleep')
                return super().query(cmd,**kw)
        with tempfile.TemporaryDirectory() as tmp:
            service=ScopeService(Settings({'test':Device('test','127.0.0.1',5555,serial='SN')},Path(tmp)))
            with patch('rigol_scope.service.TCPTransport',Sleeping),patch('rigol_scope.service.time.sleep'):
                r=service.wake('test',confirmed=True);self.assertTrue(r['verified'])
            with patch('rigol_scope.service.TCPTransport') as connect:
                with self.assertRaises(ScopeError):service.wake('test')
                connect.assert_not_called()
            unpinned=ScopeService(Settings({'test':Device('test','127.0.0.1',5555)},Path(tmp)))
            with patch('rigol_scope.service.TCPTransport') as connect:
                with self.assertRaises(ScopeError):unpinned.wake('test',confirmed=True)
                connect.assert_not_called()

class AutoDepthTests(unittest.TestCase):
    def test_auto_query_returns_effective_numeric_depth(self):
        class NumericAuto(PowerFake):
            def write(self,cmd):
                super().write(cmd)
                if cmd==':ACQuire:MDEPth AUTO':self.data[':ACQUIRE:MDEPTH']='625000'
        t=NumericAuto();r=Controls(t).configure_acquisition({'memory_depth':'AUTO'})
        self.assertTrue(r['verified'],r);self.assertEqual(r['effective_memory_depth'],625000)
        self.assertEqual(r['memory_depth_selection_requested'],'AUTO')
    def test_auto_command_error_is_not_hidden_by_numeric_depth(self):
        class RejectAuto(PowerFake):
            def write(self,cmd):
                if cmd==':ACQuire:MDEPth AUTO':self.pending=True;self.commands.append(cmd)
                else:super().write(cmd)
            def query(self,cmd,**kw):
                if cmd==':SYSTem:ERRor?' and getattr(self,'pending',False):
                    self.pending=False;return '-200,"Command execute failed"'
                return super().query(cmd,**kw)
        r=Controls(RejectAuto()).configure_acquisition({'memory_depth':'AUTO'})
        self.assertFalse(r['verified']);self.assertTrue(r['scpi_errors'])
