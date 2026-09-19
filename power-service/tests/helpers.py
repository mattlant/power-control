from datetime import datetime, timezone
from decimal import Decimal
from power_service.models import *

def ready_status():
 cpu=CpuStatus((CpuPolicyStatus(0,(0,),"test-driver","powersave",("powersave",),1,2,1,2),))
 gpu=GpuStatus(0,"Test GPU","test-driver",Decimal("2"),Decimal("1"),Decimal("3"))
 logind=LogindStatus(CanSuspend.YES,())
 return ServiceStatus(datetime.now(timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,logind))
