from pathlib import Path
import unittest


class StructureTests(unittest.TestCase):
    def test_only_adapter_uses_aiohttp_and_no_server_import_exists(self):
        source = Path(__file__).parents[1] / "src" / "power_client"
        for path in source.glob("*.py"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("power_service", content)
            if path.name not in {"adapters.py"}:
                self.assertNotIn("import aiohttp", content)
            self.assertNotIn("verify=False", content)

    def test_operation_paths_have_one_client_owner(self):
        client = (Path(__file__).parents[1] / "src" / "power_client" / "client.py").read_text(encoding="utf-8")
        self.assertEqual(client.count('"/v1/profiles"'), 1)
        self.assertEqual(client.count('"/v1/suspend"'), 1)
        self.assertEqual(client.count('"/v1/leases"'), 1)
        self.assertIn('f"/v1/profiles/{name.value}"', client)
        self.assertIn('f"/v1/leases/{lease_id.value}/renew"', client)
        self.assertIn('f"/v1/leases/{lease_id.value}"', client)

    def test_cli_has_no_transport_or_server_authority(self):
        cli = (Path(__file__).parents[1] / "src" / "power_client" / "cli.py").read_text(encoding="utf-8")
        for forbidden in ("aiohttp", "power_service", "Authorization", "verify=False", "import ssl", "socket", "UDP", "/v1/"):
            self.assertNotIn(forbidden, cli)

    def test_wake_ownership_has_no_concrete_io_or_mutation_residue(self):
        source = Path(__file__).parents[1] / "src" / "power_client"
        orchestration = (source / "orchestration.py").read_text(encoding="utf-8")
        cli = (source / "cli.py").read_text(encoding="utf-8")
        for forbidden in ("socket", "aiohttp", "import ssl", "sendto", "Bearer", "power_service"):
            self.assertNotIn(forbidden, orchestration)
        for forbidden in ("socket", "UDP", "sendto", "magic", "/v1/status", "Authorization", "retry"):
            self.assertNotIn(forbidden, cli)
        self.assertNotIn("profile", orchestration.lower())
        self.assertNotIn("lease", orchestration.lower())
        self.assertNotIn("suspend", orchestration.lower())
        self.assertIn("probe_results", (source / "models.py").read_text(encoding="utf-8"))
