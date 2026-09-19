import asyncio
import unittest
from datetime import datetime,timezone
from decimal import Decimal
from power_service.models import *
from power_service.profiles import ProfileApplicator, ProfileReconciler
class ProfileTests(unittest.TestCase):
 def test_reconciliation_matches_complete_cpu_gpu_state(self):
  action=CpuProfileAction((0,),'powersave',1,2); d=ProfileDefinition('quiet',action,GpuProfileAction(Decimal('2')))
  cpu=CpuStatus((CpuPolicyStatus(0,(0,),'driver','powersave',('powersave',),1,3,1,2),)); gpu=GpuStatus(0,'GPU','1',Decimal('2'),Decimal('1'),Decimal('3'));log=LogindStatus(CanSuspend.YES,())
  s=ServiceStatus(datetime.now(timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,log))
  self.assertEqual(ProfileReconciler.reconcile((d,),s).name,'quiet')

 def test_cpu_failure_preserves_gpu_attempt_and_partial_outcome(self):
  class CpuAdapter:
   def validate(self,action,status): pass
   async def apply(self,action,status):
    return CpuApplicationResult(ProfileComponentState.PARTIAL,(
     PolicyApplicationResult(0,ProfileComponentState.FAILED,ProfileErrorCode.UNSUPPORTED_CAPABILITY),
     PolicyApplicationResult(1,ProfileComponentState.APPLIED),
    ))
  class GpuAdapter:
   def validate(self,action,status): pass
   async def apply(self,action,status):
    return GpuApplicationResult(ProfileComponentState.APPLIED,0)
  action=CpuProfileAction((0,1),'powersave',1,2);d=ProfileDefinition('quiet',action,GpuProfileAction(Decimal('2')))
  cpu=CpuStatus((CpuPolicyStatus(0,(0,),'driver','powersave',('powersave',),1,3,1,2),CpuPolicyStatus(1,(1,),'driver','powersave',('powersave',),1,3,1,2)))
  gpu=GpuStatus(0,'GPU','1',Decimal('2'),Decimal('1'),Decimal('3'));log=LogindStatus(CanSuspend.YES,())
  status=ServiceStatus(datetime.now(timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,log))
  result=asyncio.run(ProfileApplicator(CpuAdapter(),GpuAdapter()).apply(d,status))
  self.assertEqual(result.outcome,ProfileOutcome.PARTIAL)
  self.assertEqual(result.cpu.state,ProfileComponentState.PARTIAL)
  self.assertEqual(result.gpu.state,ProfileComponentState.APPLIED)
