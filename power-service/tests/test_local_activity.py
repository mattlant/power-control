import tempfile
import unittest
from pathlib import Path

from power_service.local_activity import ConfiguredLocalActivityMonitor
from power_service.models import LocalActivityProbeConfig


class LocalActivityTests(unittest.TestCase):
    def test_exact_process_and_listener_probes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "123").mkdir()
            (root / "123" / "comm").write_text("worker\n")
            (root / "net").mkdir()
            line = "  0: 0100007F:2328 00000000:0000 0A\n"
            (root / "net" / "tcp").write_text("header\n" + line)
            (root / "net" / "tcp6").write_text("header\n")
            monitor = ConfiguredLocalActivityMonitor((LocalActivityProbeConfig("process_name", "worker"), LocalActivityProbeConfig("tcp_listener", 9000)), root)
            self.assertEqual(("process:worker", "tcp-listener:9000"), monitor.active())
