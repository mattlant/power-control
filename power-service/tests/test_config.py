import tempfile,unittest,os
from pathlib import Path
from power_service.config import load_api_config,load_broker_config
class ConfigTests(unittest.TestCase):
 def test_profile_configuration_has_no_executable_operand(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'broker.toml';p.write_text('socket_path="/run/broker.sock"\napi_uid=1\ngpu_index=0\nquery_timeout_seconds=5\noperation_timeout_seconds=5\n[[profiles]]\nname="quiet"\n[profiles.cpu]\npolicy_ids=[0]\ngovernor="powersave"\nmin_khz=1\nmax_khz=2\n[profiles.gpu]\npower_limit_w="200"\n');os.chmod(p,0o600)
   config=load_broker_config(p);self.assertEqual(config.profiles[0].name,'quiet')
 def test_command_key_is_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/'x.toml';p.write_text('listen_host="127.0.0.1"\nlisten_port=1\nbroker_socket="/x"\ntls_cert="/x"\ntls_key="/x"\nrequest_timeout_seconds=1\ncredentials=[]\ncommand="x"\n');os.chmod(p,0o600)
   with self.assertRaises(ValueError):load_api_config(p)

 def test_interactive_sessions_field_is_additive_and_strict(self):
  with tempfile.TemporaryDirectory() as d:
   prefix='''socket_path="/run/broker.sock"\napi_uid=1\ngpu_index=0\nquery_timeout_seconds=5\noperation_timeout_seconds=5\n'''
   profiles='''[[profiles]]\nname="quiet"\n[profiles.cpu]\npolicy_ids=[0]\ngovernor="powersave"\nmin_khz=1\nmax_khz=2\n[profiles.gpu]\npower_limit_w="200"\n'''
   auto='''[automatic_suspend]\nenabled=true\nmax_lease_ttl_seconds=3600\nstable_idle_seconds=3600\ngrace_seconds=60\nevaluation_interval_seconds=10\nmax_status_principals=20\nlocal_activity_probes=[]\n'''
   path=Path(d)/'broker.toml';path.write_text(prefix+profiles+auto);os.chmod(path,0o600)
   config=load_broker_config(path).automatic_suspend
   self.assertFalse(config.interactive_sessions_enabled)
   self.assertEqual(config.interactive_activity_timeout_seconds,60)
   path.write_text(prefix+profiles+auto+'interactive_sessions_enabled=true\n');
   self.assertTrue(load_broker_config(path).automatic_suspend.interactive_sessions_enabled)
   path.write_text(prefix+profiles+auto+'interactive_activity_timeout_seconds=45\n');
   self.assertEqual(load_broker_config(path).automatic_suspend.interactive_activity_timeout_seconds,45)
   path.write_text(prefix+profiles+auto+'interactive_activity_timeout_seconds=0\n');
   with self.assertRaises(ValueError):load_broker_config(path)
   path.write_text(prefix+profiles+auto+'interactive_sessions_enabled="true"\n');
   with self.assertRaises(ValueError):load_broker_config(path)
