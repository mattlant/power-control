import unittest
from decimal import Decimal

from power_service.models import *


class ModelTests(unittest.TestCase):
    def test_profile_invariants(self):
        profile = ProfileDefinition(
            "quiet",
            CpuProfileAction((0, 1), "powersave", 400, 1000, "balance_performance"),
            GpuProfileAction(Decimal("200")),
        )
        self.assertEqual(profile.name, "quiet")
        with self.assertRaises(ValueError):
            CpuProfileAction((1, 0), "powersave", 1, 2)
        with self.assertRaises(ValueError):
            ProfileDefinition("bad name", profile.cpu, profile.gpu)

    def test_suspend_result_invariants(self):
        self.assertEqual(
            SuspendResult(SuspendOutcome.ACCEPTED, CanSuspend.YES).outcome,
            SuspendOutcome.ACCEPTED,
        )
        with self.assertRaises(ValueError):
            SuspendResult(SuspendOutcome.BLOCKED, CanSuspend.YES)
        with self.assertRaises(ValueError):
            SuspendResult(SuspendOutcome.CONFLICT, CanSuspend.YES)

    def test_interactive_session_snapshot_invariants(self):
        session = InteractiveSession("c1", "/dev/pts/1", 12.5)
        self.assertEqual(session.tty, "/dev/pts/1")
        self.assertEqual(InteractiveSessionSnapshot(12.5, 1).active_count, 1)
        self.assertEqual(InteractiveSessionSnapshot(None, 0).active_count, 0)
        with self.assertRaises(ValueError):
            InteractiveSession("", "/dev/pts/1", 1)
        with self.assertRaises(ValueError):
            InteractiveSession("c1", "/dev/pts/1", 0)
        with self.assertRaises(ValueError):
            InteractiveSessionSnapshot(None, 1)

    def test_interactive_blockers_are_distinct(self):
        self.assertNotEqual(
            LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,
            LifecycleBlocker.INTERACTIVE_SESSION_UNAVAILABLE,
        )
