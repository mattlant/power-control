import asyncio,hashlib,unittest
from uuid import uuid4
from datetime import datetime,timezone
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path
from aiohttp.test_utils import AioHTTPTestCase
from power_service.api import ApiApplication
from power_service.auth import TokenAuthenticator
from power_service.models import *

class Client:
 def __init__(self,status,suspend_result=None):self.status=status;self.suspend_result=suspend_result;self.calls=0;self.leases=()
 async def get_status(self):self.calls+=1;return self.status
 async def suspend(self):self.calls+=1;return self.suspend_result
 async def release_suspend(self,receipt_id):self.released=receipt_id
 async def list_leases(self):self.calls+=1;return type('R',(),{'leases':self.leases})()
def status():
 cpu=CpuStatus((CpuPolicyStatus(0,(0,),"driver","powersave",("powersave",),1,2,1,2),))
 gpu=GpuStatus(0,"GPU","driver",Decimal('2'),Decimal('1'),Decimal('3'))
 log=LogindStatus(CanSuspend.YES,())
 return ServiceStatus(datetime.now(timezone.utc),ServiceState.READY,ComponentStatus(Availability.AVAILABLE,cpu),ComponentStatus(Availability.AVAILABLE,gpu),ComponentStatus(Availability.AVAILABLE,log))
class ApiTests(AioHTTPTestCase):
 async def get_application(self):
  salt=b's';record=CredentialRecord('reader',salt,hashlib.scrypt(b'secret',salt=salt,n=16384,r=8,p=1,dklen=32),frozenset({Permission.STATUS}));suspender=CredentialRecord('suspender',salt,hashlib.scrypt(b'secret',salt=salt,n=16384,r=8,p=1,dklen=32),frozenset({Permission.SUSPEND}));leaser=CredentialRecord('leaser',salt,hashlib.scrypt(b'secret',salt=salt,n=16384,r=8,p=1,dklen=32),frozenset({Permission.LEASE}))
  self.client_stub=Client(status(),type('R',(),{'suspend':SuspendResult(SuspendOutcome.ACCEPTED,CanSuspend.YES),'status':status(),'receipt_id':uuid4()})());config=ApiConfig(ip_address('127.0.0.1'),9443,Path('/tmp/broker'),Path('/tmp/cert'),Path('/tmp/key'),5,(record,suspender))
  config=ApiConfig(ip_address('127.0.0.1'),9443,Path('/tmp/broker'),Path('/tmp/cert'),Path('/tmp/key'),5,(record,suspender,leaser))
  return ApiApplication.create(config,TokenAuthenticator((record,suspender,leaser)),self.client_stub)

 async def test_list_leases_requires_permission_and_returns_active_collection(self):
  self.client_stub.leases=(Lease('123e4567-e89b-12d3-a456-426614174000','one',30,datetime(2026,1,1,1,tzinfo=timezone.utc)),Lease('123e4567-e89b-12d3-a456-426614174001','two',60,datetime(2026,1,1,2,tzinfo=timezone.utc)))
  response=await self.client.get('/v1/leases',headers={'Authorization':'Bearer reader.secret'});self.assertEqual(response.status,403)
  response=await self.client.get('/v1/leases',headers={'Authorization':'Bearer leaser.secret'});body=await response.json();self.assertEqual(response.status,200);self.assertEqual([item['id'] for item in body['leases']],['123e4567-e89b-12d3-a456-426614174000','123e4567-e89b-12d3-a456-426614174001']);self.assertEqual(body['leases'][0]['ttl_seconds'],30)
 async def test_authentication_and_projection(self):
  response=await self.client.get('/v1/status');self.assertEqual(response.status,401);self.assertEqual(self.client_stub.calls,0)
  response=await self.client.get('/v1/status',headers={'Authorization':'Bearer reader.secret'});body=await response.json();self.assertEqual(response.status,200);self.assertEqual(body['cpu']['state'],'available');self.assertNotIn('observed_at',body)
 async def test_suspend_requires_permission_and_projects_accepted_result(self):
  response=await self.client.post('/v1/suspend');self.assertEqual(response.status,401);self.assertEqual(self.client_stub.calls,0)
  response=await self.client.post('/v1/suspend',headers={'Authorization':'Bearer reader.secret'});self.assertEqual(response.status,403);self.assertEqual(self.client_stub.calls,0)
  response=await self.client.post('/v1/suspend',headers={'Authorization':'Bearer suspender.secret'},data='x');self.assertEqual(response.status,422);self.assertEqual(self.client_stub.calls,0)
  response=await self.client.post('/v1/suspend',headers={'Authorization':'Bearer suspender.secret'});body=await response.json();self.assertEqual(response.status,202);self.assertEqual(body['suspend']['outcome'],'accepted');self.assertEqual(self.client_stub.calls,1)
 async def test_suspend_projects_all_bounded_errors(self):
  cases=((SuspendOutcome.BLOCKED,CanSuspend.YES,409,'suspend_blocked'),(SuspendOutcome.CONFLICT,None,409,'suspend_conflict'),(SuspendOutcome.UNAVAILABLE,CanSuspend.NO,503,'suspend_unavailable'),(SuspendOutcome.AUTHORIZATION_REQUIRED,CanSuspend.CHALLENGE,503,'suspend_authorization_required'),(SuspendOutcome.OPERATIONAL_FAILURE,None,503,'suspend_operational_failure'))
  for outcome,can,http,code in cases:
   self.client_stub.suspend_result=type('R',(),{'suspend':SuspendResult(outcome,can,(SuspendBlocker(InhibitorKind.SLEEP,InhibitorMode.BLOCK),) if outcome is SuspendOutcome.BLOCKED else ()),'status':status()})()
   response=await self.client.post('/v1/suspend',headers={'Authorization':'Bearer suspender.secret'});body=await response.json();self.assertEqual((response.status,body['error']['code']),(http,code))
