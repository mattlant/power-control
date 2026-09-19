import asyncio,tempfile,unittest,os,signal,stat,pwd
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
from power_service.broker_main import install_signal_handlers
from power_service.broker_server import BrokerRuntime,BrokerServer,set_socket_group
from power_service.host_status import ScopePtyInteractiveSessionReader
from power_service.models import *
class Collector:
 async def collect(self):raise AssertionError('collector must not be called')
class BrokerTests(unittest.IsolatedAsyncioTestCase):
 async def test_interactive_reader_construction_preserves_disabled_no_op_and_injection(self):
  enabled=AutomaticSuspendConfig(True,30,10,1,1,10,interactive_sessions_enabled=True,interactive_activity_timeout_seconds=10)
  disabled=AutomaticSuspendConfig(True,30,10,1,1,10)
  config=BrokerConfig(Path('/tmp/broker.sock'),0,0,5,5,automatic_suspend=enabled)
  injected=object();runtime=BrokerRuntime(config,Collector(),interactive_session_reader=injected)
  self.assertIs(runtime.interactive_session_reader,injected)
  self.assertIsInstance(runtime._interactive_reader(enabled),ScopePtyInteractiveSessionReader)
  self.assertIsNone(runtime._interactive_reader(disabled))

 async def test_suspend_vetoes_block_inhibitor_and_honors_delay(self):
  def service(inhibitors):
   cpu=CpuStatus((CpuPolicyStatus(0,(0,),"driver","powersave",("powersave",),1,3,1,2),))
   gpu=GpuStatus(0,"GPU","driver",Decimal("2"),Decimal("1"),Decimal("3"))
   return ServiceStatus(datetime.now(timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,LogindStatus(CanSuspend.YES,inhibitors)))
  class Collector:
   def __init__(self,inhibitors):self.inhibitors=inhibitors;self.gpu=self
   async def collect(self):return service(self.inhibitors)
  class Commit:
   safe_cleanup_deadline_monotonic_seconds=999999999
   def release(self):pass
  class Attempt:
   def __init__(self):self.commit=Commit();self.outcome=None;self.ambiguous_submission=False
  class Adapter:
   def __init__(self):self.calls=0
   async def commit(self,can_suspend):self.calls+=1;return Attempt()
  config=BrokerConfig(Path('/tmp/broker.sock'),0,0,5,5)
  adapter=Adapter();blocked=BrokerRuntime(config,Collector((InhibitorStatus(InhibitorKind.SLEEP,InhibitorMode.BLOCK),)),suspend_adapter=adapter)
  result,_=await blocked.suspend();self.assertEqual(result.outcome,SuspendOutcome.BLOCKED);self.assertEqual(adapter.calls,0)
  delay=BrokerRuntime(config,Collector((InhibitorStatus(InhibitorKind.SLEEP,InhibitorMode.DELAY),)),suspend_adapter=adapter)
  result,_=await delay.suspend();self.assertEqual(result.outcome,SuspendOutcome.ACCEPTED);self.assertEqual(adapter.calls,1)
 async def test_suspend_reports_conflict_while_operation_is_reserved(self):
  class Collector:
   gpu=None
   async def collect(self):
    unavailable=ComponentStatus(Availability.UNAVAILABLE,error=OperationalErrorCode.OPERATIONAL)
    return ServiceStatus(datetime.now(timezone.utc),ServiceState.DEGRADED,unavailable,unavailable,unavailable)
  runtime=BrokerRuntime(BrokerConfig(Path('/tmp/broker.sock'),0,0,5,5),Collector())
  lease=runtime.gate.try_acquire_suspend();task=asyncio.create_task(runtime.suspend());await asyncio.sleep(0);runtime.gate.release(lease)
  result,_=await task;self.assertEqual(result.outcome,SuspendOutcome.CONFLICT)
 async def test_sighup_reload_reconciles_without_terminating_runtime(self):
  class Collector:
   def __init__(self):
    self.gpu = self
   async def collect(self):
    cpu=CpuStatus((CpuPolicyStatus(0,(0,),"driver","powersave",("powersave",),1,3,1,2),))
    gpu=GpuStatus(0,"GPU","driver",Decimal("2"),Decimal("1"),Decimal("3"))
    logind=LogindStatus(CanSuspend.YES,())
    return ServiceStatus(datetime.now(timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,logind))
  class Loop:
   def __init__(self):self.handlers={}
   def add_signal_handler(self,signum,callback):self.handlers[signum]=callback
   def remove_signal_handler(self,signum):self.handlers.pop(signum,None)
  def write_config(path,name):
   path.write_text(f'''socket_path = "/run/power-service/broker.sock"\napi_uid = 1\ngpu_index = 0\nquery_timeout_seconds = 5\noperation_timeout_seconds = 5\n[[profiles]]\nname = "{name}"\n[profiles.cpu]\npolicy_ids = [0]\ngovernor = "powersave"\nmin_khz = 1\nmax_khz = 2\n[profiles.gpu]\npower_limit_w = "2"\n''')
  with tempfile.TemporaryDirectory() as directory:
   config_path=Path(directory)/"broker.toml";write_config(config_path,"quiet");os.chmod(config_path,0o600)
   config=BrokerConfig(Path(directory)/"broker.sock",999999,0,5,5,(ProfileDefinition("quiet",CpuProfileAction((0,),"powersave",1,2),GpuProfileAction(Decimal("2"))),))
   collector=Collector();runtime=BrokerRuntime(config,collector,config_path=config_path);server=type("Server",(),{"runtime":runtime})();loop=Loop();stop=asyncio.Event();install_signal_handlers(loop,server,stop)
   write_config(config_path,"performance");loop.handlers[signal.SIGHUP]();await asyncio.sleep(0);await asyncio.sleep(0)
   self.assertFalse(stop.is_set());self.assertEqual(runtime.config.profiles[0].name,"performance");self.assertEqual((await runtime.status()).profiles.name,"performance")

 async def test_socket_owner_contract(self):
  with tempfile.TemporaryDirectory() as directory:
   calls=[];config=BrokerConfig(Path(directory)/'broker.sock',999999,0,Path('/bin/true'),5)
   server=BrokerServer(config,Collector(),lambda writer:0,lambda path,uid:calls.append((path,uid)));await server.start()
   self.assertEqual(calls,[(config.socket_path,999999)]);await server.close()
 def test_socket_group_contract_preserves_broker_owner(self):
  with tempfile.TemporaryDirectory() as directory:
   path=Path(directory)/'broker.sock';path.touch();os.chmod(path,0o600)
   owner=os.stat(path).st_uid;set_socket_group(path,os.getuid());metadata=os.stat(path)
   self.assertEqual(metadata.st_uid,owner)
   self.assertEqual(metadata.st_gid,pwd.getpwuid(os.getuid()).pw_gid)
   self.assertEqual(stat.S_IMODE(metadata.st_mode),0o660)
 async def test_wrong_peer_closes_without_collection(self):
  with tempfile.TemporaryDirectory() as directory:
   config=BrokerConfig(Path(directory)/'broker.sock',999999,0,Path('/bin/true'),5);server=BrokerServer(config,Collector(),lambda writer:0,lambda path,uid:None);await server.start()
   reader,writer=await asyncio.open_unix_connection(str(config.socket_path));writer.write(b'bad\n');await writer.drain()
   try:self.assertEqual(await reader.read(),b'')
   except ConnectionResetError:pass
   writer.close()
   try:await writer.wait_closed()
   except ConnectionResetError:pass
   await server.close()
