import unittest
from pathlib import Path


class DeploymentAssetContractTests(unittest.TestCase):
    def test_broker_creates_its_runtime_directory_before_hardening(self):
        unit = (Path(__file__).parents[1] / "systemd" / "power-service-broker.service").read_text()
        self.assertIn("RuntimeDirectory=power-service", unit)
        self.assertIn("RuntimeDirectoryMode=0755", unit)
        self.assertIn("ProtectSystem=full", unit)
        self.assertIn("ReadWritePaths=/sys/devices/system/cpu/cpufreq", unit)
        self.assertIn("AmbientCapabilities=CAP_SYS_ADMIN CAP_DAC_OVERRIDE", unit)
        self.assertIn("CapabilityBoundingSet=CAP_SYS_ADMIN CAP_DAC_OVERRIDE", unit)
        self.assertNotIn("ReadWritePaths=/run/power-service", unit)
        self.assertLess(unit.index("RuntimeDirectory=power-service"), unit.index("ExecStart="))

    def test_suspend_policy_is_limited_to_the_broker_identity(self):
        root = Path(__file__).parents[1] / "systemd"
        rule = (root / "49-power-service-broker.rules").read_text()
        self.assertIn('subject.user == "power-service-broker"', rule)
        self.assertIn('org.freedesktop.login1.suspend-multiple-sessions', rule)
        self.assertNotIn("ignore-inhibit", rule)
        self.assertNotIn("subject.active", rule)
