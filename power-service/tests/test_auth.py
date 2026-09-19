import hashlib,unittest
from power_service.auth import TokenAuthenticator,authorize
from power_service.models import CredentialRecord,Permission
class AuthTests(unittest.TestCase):
 def test_authentication_and_permission(self):
  salt=b'x'; secret='right'; record=CredentialRecord('alice',salt,hashlib.scrypt(secret.encode(),salt=salt,n=16384,r=8,p=1,dklen=32),frozenset({Permission.STATUS}))
  auth=TokenAuthenticator((record,)); principal=auth.authenticate('Bearer alice.right')
  self.assertTrue(authorize(principal,Permission.STATUS));self.assertIsNone(auth.authenticate('Bearer alice.wrong'))
