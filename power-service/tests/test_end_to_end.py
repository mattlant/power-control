import asyncio,hashlib,os,ssl,tempfile,unittest
from ipaddress import ip_address
from pathlib import Path
from aiohttp import ClientSession, TCPConnector
import trustme
from power_service.api import ApiApplication
from power_service.auth import TokenAuthenticator
from power_service.broker_client import StatusBrokerClient
from power_service.broker_server import BrokerServer
from power_service.models import ApiConfig,BrokerConfig,CredentialRecord,Permission
from helpers import ready_status

class Collector:
 async def collect(self): return ready_status()
class EndToEndTests(unittest.IsolatedAsyncioTestCase):
 async def _application(self, config, credentials):
  return ApiApplication.create(config,TokenAuthenticator(credentials),StatusBrokerClient(config.broker_socket,2))
 async def test_tls_api_over_real_unix_broker(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory); socket_path=root/'broker.sock'; ca=trustme.CA(); certificate=ca.issue_cert('localhost','127.0.0.1');cert=root/'cert.pem';key=root/'key.pem';certificate.cert_chain_pems[0].write_to_path(cert);certificate.private_key_pem.write_to_path(key)
   salt=b'salt';reader=CredentialRecord('reader',salt,hashlib.scrypt(b'secret',salt=salt,n=16384,r=8,p=1,dklen=32),frozenset({Permission.STATUS}));denied=CredentialRecord('denied',salt,hashlib.scrypt(b'secret',salt=salt,n=16384,r=8,p=1,dklen=32),frozenset({Permission.PROFILE}));api_config=ApiConfig(ip_address('127.0.0.1'),9443,socket_path,cert,key,2,(reader,denied));broker_config=BrokerConfig(socket_path,os.getuid(),0,Path('/bin/true'),2)
   broker=BrokerServer(broker_config,Collector(),socket_owner=lambda path,uid:None);await broker.start();self.assertTrue(all(sock.family==__import__('socket').AF_UNIX for sock in broker.server.sockets))
   server_context=ssl.create_default_context(ssl.Purpose.CLIENT_AUTH);server_context.load_cert_chain(cert,key)
   from aiohttp.test_utils import TestServer
   web_server=TestServer(await self._application(api_config,(reader,denied)),scheme='https');await web_server.start_server(ssl=server_context);client_context=ssl.create_default_context();ca.configure_trust(client_context);connector=TCPConnector(ssl=client_context)
   try:
    async with ClientSession(connector=connector) as session:
     url=str(web_server.make_url('/v1/status'))
     response=await session.get(url);self.assertEqual(response.status,401)
     response=await session.get(url,headers={'Authorization':'Bearer denied.secret'});self.assertEqual(response.status,403)
     response=await session.get(url,headers={'Authorization':'Bearer reader.secret'});self.assertEqual(response.status,200);self.assertEqual((await response.json())['service']['state'],'ready')
   finally:await web_server.close();await broker.close()
   unavailable=ApiApplication.create(api_config,TokenAuthenticator((reader,)),StatusBrokerClient(socket_path,1));unavailable_server=TestServer(unavailable,scheme='https');await unavailable_server.start_server(ssl=server_context)
   try:
    async with ClientSession(connector=TCPConnector(ssl=client_context)) as session:
     url=str(unavailable_server.make_url('/v1/status'));response=await session.get(url,headers={'Authorization':'Bearer reader.secret'});self.assertEqual(response.status,503)
   finally:await unavailable_server.close()
