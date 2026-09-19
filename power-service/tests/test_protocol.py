import unittest
from dataclasses import replace
import json
from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4
from power_service.models import *
from power_service.mapping import service_status_to_broker_result,broker_result_to_service_status
from power_service.mapping import service_status_to_status_dto
from power_service.protocol import *
class ProtocolTests(unittest.TestCase):
 def status(self):
  cpu=CpuStatus((CpuPolicyStatus(0,(0,),"amd-pstate-epp","powersave",("powersave",),1,2,1,2),))
  gpu=GpuStatus(0,"GPU","1",Decimal('2'),Decimal('1'),Decimal('3'))
  log=LogindStatus(CanSuspend.CHALLENGE,(InhibitorStatus(InhibitorKind.SLEEP,InhibitorMode.DELAY),))
  return ServiceStatus(datetime(2026,1,1,tzinfo=timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,log))
 def test_request_and_full_status_round_trip(self):
  rid=uuid4();self.assertEqual(decode_request(encode_request(BrokerRequest(rid))).request_id,rid)
  result=service_status_to_broker_result(self.status(),rid); decoded=decode_result(encode_result(result));self.assertEqual(broker_result_to_service_status(decoded),self.status())
 def test_rejects_extra_request_field(self):
  with self.assertRaises(ValueError):decode_request(b'{"version":1,"request_id":"00000000-0000-0000-0000-000000000000","operation":"get_status","x":1}')
 def test_rejects_invalid_result_version_and_cpu_invariants(self):
  rid="00000000-0000-0000-0000-000000000000"
  with self.assertRaises(ValueError): decode_result(json.dumps({"version":2,"request_id":rid,"kind":"failure","code":"validation"}))
  invalid={"version":1,"request_id":rid,"kind":"status","status":{"observed_at":"2026-01-01T00:00:00Z","service_state":"degraded","cpu":{"state":"available","policies":[{"policy":-1,"affected_cpus":[],"driver":"","governor":"","available_governors":[],"hardware_min_khz":-1,"hardware_max_khz":0,"configured_min_khz":0,"configured_max_khz":0}]},"gpu":{"state":"unavailable","error":{"code":"operational"}},"logind":{"state":"unavailable","error":{"code":"operational"}}}}
  with self.assertRaises(ValueError): decode_result(json.dumps(invalid))
 def test_suspend_round_trip_and_empty_request(self):
  rid=uuid4(); request=BrokerRequest(rid,BrokerOperation.SUSPEND);self.assertEqual(decode_request(encode_request(request)),request)
  result=BrokerSuspendResult(rid,SuspendResult(SuspendOutcome.BLOCKED,CanSuspend.YES,(SuspendBlocker(InhibitorKind.SLEEP,InhibitorMode.BLOCK),)),self.status())
  self.assertEqual(decode_result(encode_result(result)),result)
  with self.assertRaises(ValueError):decode_request(b'{"version":1,"request_id":"00000000-0000-0000-0000-000000000000","operation":"suspend","profile":"x"}')
 def test_committed_suspend_and_release_receipt_round_trip(self):
  receipt=uuid4(); rid=uuid4()
  result=BrokerSuspendResult(rid,SuspendResult(SuspendOutcome.ACCEPTED,CanSuspend.YES),self.status(),receipt)
  self.assertEqual(decode_result(encode_result(result)),result)
  request=BrokerRequest(rid,BrokerOperation.RELEASE_SUSPEND,suspend_receipt_id=receipt)
  self.assertEqual(decode_request(encode_request(request)),request)
  self.assertEqual(decode_result(encode_result(BrokerReleaseSuspendResult(rid))),BrokerReleaseSuspendResult(rid))
 def test_successful_lease_release_round_trip_has_no_null_error(self):
  result=BrokerLeaseResult(uuid4(),None,None,self.status())
  encoded=encode_result(result)
  self.assertIn(b'"released":true',encoded)
  self.assertNotIn(b'"error":null',encoded)
  self.assertEqual(decode_result(encoded),result)
 def test_lease_list_round_trip_preserves_multiple_leases(self):
  first=Lease('123e4567-e89b-12d3-a456-426614174000','one',30,datetime(2026,1,1,tzinfo=timezone.utc))
  second=Lease('123e4567-e89b-12d3-a456-426614174001','two',60,datetime(2026,1,1,0,1,tzinfo=timezone.utc))
  result=BrokerLeasesResult(uuid4(),(first,second))
  decoded=decode_result(encode_result(result))
  self.assertEqual([lease.lease_id for lease in decoded.leases],[first.lease_id,second.lease_id])
 def test_lifecycle_round_trip_preserves_active_lease_for_api_projection(self):
  status=replace(self.status(),lifecycle=LifecycleStatus(LifecycleState.IDLE_TIMING,(LifecycleBlocker.LEASE,),datetime(2026,1,1,tzinfo=timezone.utc),None,datetime(2026,1,1,0,1,tzinfo=timezone.utc),LeaseSummary(1,1,("principal",),False)))
  decoded=decode_result(encode_result(BrokerStatusResult(uuid4(),status)))
  self.assertEqual(decoded.status.lifecycle,status.lifecycle)
  dto=service_status_to_status_dto(decoded.status)
  self.assertEqual(1,dto['lifecycle']['leases']['active_count'])
  self.assertEqual('idle_timing',dto['lifecycle']['state'])
