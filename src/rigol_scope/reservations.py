"""Short-lived owner process for arm -> external stimulus -> collect.

The owner holds existing serial/endpoint locks, expires after a bounded wait,
cleans up, and exits. No persistent daemon or unattended acquisition loop.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib,hmac,json,os,re,secrets,socket,subprocess,sys,tempfile,time,uuid,threading
from pathlib import Path
from .config import Device,Settings
from .controls import finite,number
from .errors import BusyError,ScopeError

TERMINAL={'collected','cancelled','expired','failed'}
MAX_MESSAGE=8*1024*1024


def directory(settings):
    root=settings.state_dir/'reservations';root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.is_symlink():raise ScopeError('Unsafe reservation directory')
    return root


def write_json(path,data):
    temp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False,allow_nan=False)
        os.replace(temp,path)
    finally:
        if temp.exists():temp.unlink()


def state_path(settings,rid):
    if not isinstance(rid,str) or not re.fullmatch('[0-9a-f]{32}',rid):raise ScopeError('Invalid reservation_id')
    return directory(settings)/(rid+'.json')


def load_state(settings,rid):
    p=state_path(settings,rid)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>MAX_MESSAGE:raise ScopeError('Unknown or unsafe reservation')
    return json.loads(p.read_text())


def marker_path(settings,device):
    return directory(settings)/('active-'+hashlib.sha256(device.serial.encode()).hexdigest()+'.json')


def assert_unreserved(settings,device):
    marker=marker_path(settings,device)
    if marker.exists():
        if marker.is_symlink():raise BusyError('Unsafe reservation marker')
        rid=json.loads(marker.read_text())['reservation_id']
        state=load_state(settings,rid)
        if state['status'] not in TERMINAL:
            raise BusyError('Instrument reserved by a prepared capture; use its owner API or wait for bounded cleanup')


def authenticate(settings,rid,token):
    state=load_state(settings,rid)
    if not isinstance(token,str) or len(token)>128 or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(),state['token_hash']):
        raise ScopeError('Invalid reservation credential')
    return state


def public(state):
    remaining=(0.0 if state['status'] in TERMINAL else max(0.0,datetime.fromisoformat(state['expires_at']).timestamp()-time.time()) if state.get('expires_at') else None)
    return {**{k:v for k,v in state.items() if k not in {'token_hash','socket_path','pid','device_config'}},
            'capture_complete':state['status']=='collected' and bool((state.get('measurement') or {}).get('verified')),
            'cleanup_complete':state.get('cleanup',{}).get('verified',False),
            'ready':bool(state.get('ready') and remaining is not None and remaining>0),
            'stimulus_wait_remaining_s':remaining,
            'reservation_receipt_uri':f"scope://prepared/{state['reservation_id']}/receipt.json" if state['status'] in TERMINAL else None}


def read_public_receipt(settings,rid):
    state=load_state(settings,rid)
    if state['status'] not in TERMINAL:raise ScopeError('Reservation is not terminal; owner must use authenticated status')
    return json.dumps(public(state),ensure_ascii=False,indent=2,allow_nan=False)


def receive(conn,deadline=None):
    data=bytearray()
    while not data.endswith(b'\n'):
        if deadline is not None:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ScopeError('Reservation IPC deadline exceeded')
            conn.settimeout(remaining)
        block=conn.recv(min(65536,MAX_MESSAGE-len(data)))
        if not block:raise ScopeError('Reservation IPC closed before complete message')
        data.extend(block)
        if len(data)>=MAX_MESSAGE:raise ScopeError('Reservation IPC message too large')
    return json.loads(data)


def send(conn,data):
    conn.sendall(json.dumps(data,ensure_ascii=False,allow_nan=False).encode()+b'\n')


def status(settings,device,rid,token):
    state=authenticate(settings,rid,token)
    if state['device']!=device or Device(**state['device_config'])!=settings.device(device):raise ScopeError('Reservation target/configuration differs from this caller')
    return public(state)


def command(settings,device,rid,token,action,**params):
    state=authenticate(settings,rid,token)
    if state['device']!=device or Device(**state['device_config'])!=settings.device(device):raise ScopeError('Reservation target/configuration differs from this caller')
    if state['status'] in TERMINAL:
        if action=='generator_output':raise ScopeError('Reservation is terminal; stimulus refused')
        return public(state)
    if action=='cancel':
        write_json(directory(settings)/(rid+'.cancel'),{'requested':True})
        if state['status']=='collecting':return {**public(state),'cancel_requested':True}
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as conn:
            timeout=state['export_timeout_s']+25 if action=='collect' else 20
            conn.settimeout(timeout)
            conn.connect(state['socket_path']);send(conn,{'token':token,'action':action,'params':params})
            response=receive(conn,time.monotonic()+timeout)
    except OSError as exc:
        state=authenticate(settings,rid,token)
        if state['status'] in TERMINAL:
            if action=='generator_output':raise ScopeError('Reservation ended; no stimulus success is claimed')
            return public(state)
        if action=='cancel':
            # The cancel flag can win the race with socket accept. Give the
            # live owner time to publish cleanup; do not mistake it for a crash.
            try:return recover_cancel(settings,device,state)
            except BusyError:
                until=time.monotonic()+12
                while time.monotonic()<until:
                    state=authenticate(settings,rid,token)
                    if state['status'] in TERMINAL:return public(state)
                    time.sleep(.05)
                return {**public(state),'cancel_requested':True}

        raise ScopeError('Reservation owner unavailable; inspect status or explicitly cancel, do not replay stimulus') from exc
    if 'rpc_error' in response:raise ScopeError(response['rpc_error'])
    return response


def prepare(settings,device,request,lease_s=60,ready_wait_s=5,export_timeout_s=30,keep_generator_outputs=None):
    finite(lease_s,'lease_s',5,120);finite(ready_wait_s,'ready_wait_s',.1,10);finite(export_timeout_s,'export_timeout_s',5,900)
    keep=[] if keep_generator_outputs is None else keep_generator_outputs
    if not isinstance(keep,list) or any(type(x) is not int or x not in {1,2} for x in keep) or len(set(keep))!=len(keep):
        raise ScopeError('keep_generator_outputs must be unique channels 1/2 for ongoing output tasks')
    target=settings.device(device)
    if not target.serial:raise ScopeError('Prepared capture requires a pinned serial')
    assert_unreserved(settings,target)
    rid=uuid.uuid4().hex;token=secrets.token_urlsafe(32)
    ipc=Path(tempfile.mkdtemp(prefix=f'rigol-{os.getuid()}-'))/'owner.sock'
    state={'reservation_id':rid,'device':device,'status':'starting','ready':False,'verified':True,
           'token_hash':hashlib.sha256(token.encode()).hexdigest(),'socket_path':str(ipc),
           'created_at':datetime.now(timezone.utc).isoformat(),'lease_s':lease_s,'export_timeout_s':export_timeout_s,
           'keep_generator_outputs':keep,'device_config':asdict(target)}
    write_json(state_path(settings,rid),state)
    payload={'state_dir':str(settings.state_dir),'profiles_dir':str(settings.profiles_dir) if settings.profiles_dir else None,
             'state':state,'token':token,'request':request,'ready_wait_s':ready_wait_s}
    with (directory(settings)/(rid+'.log')).open('ab') as log:
        env=dict(os.environ);env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])+os.pathsep+env.get('PYTHONPATH','')
        child=subprocess.Popen([sys.executable,'-m','rigol_scope.reservations','--worker'],stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL,stderr=log,start_new_session=True,env=env)
        child.stdin.write(json.dumps(payload).encode());child.stdin.close()
        threading.Thread(target=child.wait,daemon=True,name="rigol-owner-reaper").start()
    until=time.monotonic()+35+ready_wait_s
    while time.monotonic()<until:
        state=load_state(settings,rid)
        if state['status'] in TERMINAL or state.get('ready_observed'):
            return {**public(state),'reservation_token':token,'credential_note':'Keep in this caller run; never forward to a stimulus agent or save in user notes.'}
        if child.poll() is not None:raise ScopeError('Reservation owner exited before publishing readiness; inspect operator log')
        time.sleep(.05)
    # Owner retains its bounded timeout even if this client stops waiting.
    return {**public(state),'reservation_token':token,'verified':False,'error':'Preparation exceeded client wait; explicitly cancel with this credential'}


class ReservationCancelled(ScopeError):pass
class ReservationExpired(ScopeError):pass


class Owner:
    def __init__(self,payload,settings):
        self.settings=settings;self.state=payload['state'];self.rid=self.state['reservation_id'];self.token=payload['token']
        self.ready_wait_s=payload['ready_wait_s'];self.pending=None;self.server=None;self.expires=None
    def publish(self,**changes):
        self.state.update(changes,updated_at=datetime.now(timezone.utc).isoformat())
        write_json(state_path(self.settings,self.rid),self.state)
    def arm_and_wait(self,driver,receipt):
        t=driver.transport
        receipt['prepared_capture']={'reservation_id':self.rid,'prepare_timeout_s':driver.transport.device.operation_timeout_s,'lease_s':self.state['lease_s'],
                                     'export_timeout_s':self.state['export_timeout_s']}
        running=driver.acquisition_control('run')
        if not running['verified']:raise ScopeError('Cannot establish non-STOP before arming')
        t.write(':SINGle');until=time.monotonic()+self.ready_wait_s
        trigger=t.query(':TRIGger:STATus?')
        while trigger!='WAIT':
            if trigger in {'STOP','TD'}:raise ScopeError('TRIGGER_BEFORE_READY: no waiting state observed; do not send stimulus')
            if time.monotonic()>=until:raise ScopeError('NOT_READY: WAIT state was not observed in time')
            time.sleep(.05);trigger=t.query(':TRIGger:STATus?')
        ready_wall=time.time()
        self.expires=time.monotonic()+self.state['lease_s'];t.deadline=self.expires+10
        rate=number(t.query(':ACQuire:SRATe?'))
        if rate<=0:raise ScopeError('Sampling rate unavailable before stimulus')
        from .measurement import quality_report
        quality=quality_report({'effective':receipt['effective'],'expected_signal':receipt['expected_signal']},rate)
        if t.query(':TRIGger:STATus?')!='WAIT':raise ScopeError('TRIGGER_BEFORE_READY: waiting state changed before readiness publication')
        receipt['prepared_capture'].update(ready_observed=True,ready_state='WAIT',ready_at=datetime.now(timezone.utc).isoformat())
        self.publish(status='ready',ready=True,ready_observed=True,trigger_state='WAIT',identity=receipt['identity'],
                     expires_at=datetime.fromtimestamp(ready_wall+self.state['lease_s'],timezone.utc).isoformat(),
                     profile_ref=receipt['profile_ref'],plan_digest=receipt['plan_digest'],configuration_before_arm=receipt['configuration_before_arm'],
                     ready_sample_rate_sa_s=rate,quality_before_stimulus=quality)
        polled=time.monotonic()
        while True:
            if (directory(self.settings)/(self.rid+'.cancel')).exists():raise ReservationCancelled('Owner requested cancellation')
            if time.monotonic()-polled>=.25:
                trigger=t.query(':TRIGger:STATus?');polled=time.monotonic()
                if trigger!=self.state.get('trigger_state'):
                    self.publish(status='triggered' if trigger=='STOP' else 'ready',ready=trigger=='WAIT',trigger_state=trigger)
            if time.monotonic()>=self.expires:raise ReservationExpired('Prepared capture expired; no successful capture claimed')
            self.server.settimeout(min(.1,max(.01,self.expires-time.monotonic())))
            try:conn,_=self.server.accept()
            except socket.timeout:
                continue
            conn.settimeout(2)
            try:
                message=receive(conn,min(time.monotonic()+2,self.expires))
                if not isinstance(message.get('token'),str) or not hmac.compare_digest(message['token'],self.token):
                    send(conn,{'rpc_error':'Invalid reservation credential'});continue
                action=message.get('action');params=message.get('params',{})
                if time.monotonic()>=self.expires:raise ReservationExpired('Prepared capture expired')
                if action=='cancel':self.pending=conn;raise ReservationCancelled('Owner cancelled prepared capture')
                if action=='generator_output':
                    trigger=t.query(':TRIGger:STATus?')
                    if params.get('enabled') and trigger!='WAIT':raise ScopeError('Not waiting for trigger; do not send stimulus')
                    result=driver.generator_output(**params)
                    trigger=t.query(':TRIGger:STATus?')
                    self.publish(status='triggered' if trigger=='STOP' else 'ready',ready=trigger=='WAIT',trigger_state=trigger)
                    send(conn,{**public(self.state),'verified':result['verified'],'generator':result})
                elif action=='collect':
                    wait=finite(params.get('wait_s',0),'wait_s',0,10);until=min(self.expires,time.monotonic()+wait)
                    trigger=t.query(':TRIGger:STATus?')
                    while trigger!='STOP' and time.monotonic()<until:
                        if (directory(self.settings)/(self.rid+'.cancel')).exists():raise ReservationCancelled('Owner requested cancellation')
                        time.sleep(.05);trigger=t.query(':TRIGger:STATus?')
                    if time.monotonic()>=self.expires:raise ReservationExpired('Prepared capture expired before collection')
                    if trigger!='STOP':
                        self.publish(status='ready',ready=True,trigger_state=trigger)
                        send(conn,{**public(self.state),'capture_complete':False})
                    else:
                        self.pending=conn;t.deadline=time.monotonic()+self.state['export_timeout_s']
                        driver.operation_timeout_s=self.state['export_timeout_s'];receipt['operation_timeout_s']=self.state['export_timeout_s']
                        self.publish(status='collecting',ready=False,trigger_state='STOP')
                        return {'before':'WAIT','action':'single','state':'STOP','verified':True,'trigger_timeout':False,
                                'note':'STOP followed observed WAIT; front-panel/other-host STOP cannot be excluded. Caller validates intended event.'}
                else:send(conn,{'rpc_error':'Unknown prepared operation'})
            except (ReservationCancelled,ReservationExpired):
                self.pending=conn;raise
            except (ScopeError,ValueError,TypeError) as exc:
                if getattr(t,'sock',True) is None:raise
                try:send(conn,{'rpc_error':str(exc)})
                except OSError:pass
            except OSError:
                pass
            finally:
                if self.pending is not conn:conn.close()


def worker(payload):
    from .service import ScopeService
    from .measurement import execute
    state=payload['state'];device=Device(**state['device_config'])
    settings=Settings({device.alias:device},Path(payload['state_dir']),Path(payload['profiles_dir']) if payload['profiles_dir'] else None)
    owner=Owner(payload,settings);service=ScopeService(settings);marker=marker_path(settings,device);owns=False
    sock=Path(state['socket_path']);result=None
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server:
            owner.server=server;server.bind(str(sock));os.chmod(sock,0o600);server.listen(4)
            req,profile=service._measurement_request(payload['request'])
            with service.session(device.alias,'prepared_capture',read_only=False,reservation_owner=True,lock_timeout_s=.2) as (driver,identity):
                plan=service._resolve_measurement(driver,profile,req,device)
                if plan['effective']['trigger']['sweep']=='AUTO':raise ScopeError('Prepared capture requires NORM or SING trigger sweep')
                owner.publish(status='preparing',pid=os.getpid())
                write_json(marker,{'reservation_id':owner.rid});owns=True
                driver.reservation_budgets={'prepare_timeout_s':device.operation_timeout_s,'lease_s':state['lease_s'],'export_timeout_s':state['export_timeout_s'],'cleanup_timeout_s':12}
                cleanup_required=True
                driver.export_cancelled=lambda:(directory(settings)/(owner.rid+'.cancel')).exists()
                try:
                    result=execute(driver,identity,settings.state_dir,plan,acquisition_callback=owner.arm_and_wait)
                except Exception as exc:
                    if getattr(exc,'writes_sent',None)==0:cleanup_required=False
                    raise
                finally:
                    driver.transport.deadline=time.monotonic()+12
                    try:
                        if cleanup_required:
                            stopped=driver.acquisition_control('stop')
                            cleanup=driver.release(owner.state['keep_generator_outputs'])
                            cleanup['stop']=stopped;cleanup['verified']=cleanup['verified'] and stopped['verified']
                        else:cleanup={'verified':True,'not_required':True}
                    except Exception as exc:cleanup={'verified':False,'state':'unknown','error':str(exc)}
                    owner.state['cleanup']=cleanup
                failure=(result or {}).get('failure',{})
                outcome=('collected' if result and result['verified'] else 'cancelled' if failure.get('type')=='ReservationCancelled' or (directory(settings)/(owner.rid+'.cancel')).exists()
                         else 'expired' if failure.get('type')=='ReservationExpired' else 'failed')
                verified=bool(result and result['verified'] and cleanup['verified']) if outcome=='collected' else cleanup['verified'] if outcome=='cancelled' else False
                owner.publish(status=outcome,ready=False,verified=verified,measurement=result,cleanup=cleanup)
                driver.result_verified=verified
    except Exception as exc:
        owner.publish(status='failed',ready=False,verified=False,error={'type':type(exc).__name__,'message':str(exc)})
    finally:
        if owner.pending:
            try:send(owner.pending,public(owner.state))
            except OSError:pass
            owner.pending.close()
        if owns and marker.exists():
            if json.loads(marker.read_text()).get('reservation_id')==owner.rid:marker.unlink()
        cancel=directory(settings)/(owner.rid+'.cancel')
        if cancel.exists():cancel.unlink()
        if sock.exists():sock.unlink()
        try:sock.parent.rmdir()
        except OSError:pass


def recover_cancel(settings,device,state):
    """Explicit owner recovery after an owner-process crash, never automatic replay."""
    from .service import ScopeService
    target=settings.device(device);marker=marker_path(settings,target)
    if not marker.exists() or json.loads(marker.read_text()).get('reservation_id')!=state['reservation_id']:
        raise ScopeError('Owner unavailable and reservation does not hold marker; operator inspection required')
    with ScopeService(settings).session(device,'prepared_cancel_recovery',read_only=False,reservation_owner=True,lock_timeout_s=.2) as (driver,identity):
        stopped=driver.acquisition_control('stop');cleanup=driver.release(state['keep_generator_outputs'])
        state.update(status='cancelled',ready=False,verified=stopped['verified'] and cleanup['verified'],cleanup=cleanup,
                     recovery='Explicit cancel after IPC owner loss; no capture claimed',updated_at=datetime.now(timezone.utc).isoformat())
        write_json(state_path(settings,state['reservation_id']),state);marker.unlink()
        ipc=Path(state['socket_path'])
        if ipc.parent.name.startswith(f'rigol-{os.getuid()}-') and ipc.name=='owner.sock':
            if ipc.exists():ipc.unlink()
            try:ipc.parent.rmdir()
            except OSError:pass
        cancel=directory(settings)/(state['reservation_id']+'.cancel')
        if cancel.exists():cancel.unlink()
        return public(state)


if __name__=='__main__':
    if sys.argv[1:]!=['--worker']:raise SystemExit('Internal bounded reservation owner only')
    data=sys.stdin.buffer.read(262145)
    if len(data)>262144:raise SystemExit('Reservation start payload too large')
    worker(json.loads(data))
