import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from power_service.broker_server import BrokerOperationGate
from power_service.lifecycle import EligibilitySnapshot, LifecycleController
from power_service.leases import LeaseStore
from power_service.local_activity import ConfiguredLocalActivityMonitor
from power_service.models import AutomaticSuspendConfig, Availability, ComponentStatus, InteractiveSessionSnapshot, LifecycleBlocker, LifecycleState, ServiceState, ServiceStatus, OperationalErrorCode
from tests.helpers import ready_status


class Collector:
    def __init__(self, status): self.status = status
    async def collect(self): return self.status


class Runtime:
    def __init__(self): self.gate = BrokerOperationGate()


class SessionReader:
    def __init__(self, snapshot=None, error=None):
        self.snapshot = snapshot
        self.error = error
        self.calls = 0

    async def read(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.snapshot


class ActivityMonitor:
    def __init__(self, active=()):
        self.active_result = active

    def active(self):
        return self.active_result


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_constant_activity_crosses_to_idle_once_without_utc_projection_jitter(self):
        class Clock:
            now = 0

        clock = Clock()
        config = AutomaticSuspendConfig(True, 30, 3600, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=60)
        reader = SessionReader(InteractiveSessionSnapshot(64503, 8))
        utc_origin = datetime(2026, 9, 9, tzinfo=timezone.utc)
        utc_offsets = iter((0.400663, 0.400664, 0.400663, 0.400664))
        controller = LifecycleController(
            LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: clock.now,
            lambda: utc_origin + timedelta(seconds=clock.now + next(utc_offsets)),
            reader,
        )
        clock.now = 64562.999

        active = await controller._snapshot()
        active_candidate = controller._candidate(active)
        controller._publish(LifecycleState.ACTIVE, active.blockers, idle_started_monotonic=active_candidate)

        clock.now = 64563.000
        first_idle = await controller._snapshot()
        first_idle_candidate = controller._candidate(first_idle)
        controller._publish(
            LifecycleState.IDLE_TIMING,
            first_idle.blockers,
            idle_started_monotonic=first_idle_candidate,
        )
        first_idle_started = controller.status().idle_started_at

        clock.now = 64564.000
        repeated_idle = await controller._snapshot()
        repeated_idle_candidate = controller._candidate(repeated_idle)
        controller._publish(
            LifecycleState.IDLE_TIMING,
            repeated_idle.blockers,
            idle_started_monotonic=repeated_idle_candidate,
        )

        self.assertIn(LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY, active)
        self.assertFalse(first_idle)
        self.assertFalse(repeated_idle)
        self.assertEqual(active_candidate, 64503)
        self.assertEqual(first_idle_candidate, 64503)
        self.assertEqual(repeated_idle_candidate, 64503)
        self.assertEqual(
            controller.status().idle_started_at,
            utc_origin + timedelta(seconds=64503.400663),
        )
        self.assertEqual(first_idle_started, controller.status().idle_started_at)

    async def test_activity_blocker_and_idle_state_use_the_same_evaluation_clock(self):
        class Clock:
            def __init__(self):
                self.values = iter((109.999999, 110.000001))

            def monotonic(self):
                return next(self.values)

        clock = Clock()
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=10)
        reader = SessionReader(InteractiveSessionSnapshot(100, 1))
        controller = LifecycleController(
            LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: 0,
            lambda: datetime.now(timezone.utc),
            reader,
        )
        controller.monotonic = clock.monotonic
        waits = []

        async def wait(seconds):
            waits.append(seconds)
            return False

        controller._wait = wait

        snapshot = await controller._snapshot()
        candidate = controller._candidate(snapshot)

        self.assertIn(LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY, snapshot)
        self.assertEqual(snapshot.evaluated_monotonic_seconds, 109.999999)
        self.assertFalse(await controller._wait_until_candidate_deadline(snapshot, candidate))
        self.assertEqual(len(waits), 1)
        self.assertAlmostEqual(waits[0], 0.000001)

    async def test_run_loop_polls_after_activity_timeout_and_advances_new_idle_deadline(self):
        class Clock:
            now = 0

        class StopLifecycle(Exception):
            pass

        clock = Clock()
        config = AutomaticSuspendConfig(
            True,
            30,
            100,
            1,
            2,
            10,
            interactive_sessions_enabled=True,
            interactive_activity_timeout_seconds=5,
        )
        reader = SessionReader(InteractiveSessionSnapshot(1, 1))
        controller = LifecycleController(
            LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
            reader,
        )
        clock.now = 2
        statuses = []
        waits = []
        publish = controller._publish

        def record_publish(*args, **kwargs):
            publish(*args, **kwargs)
            statuses.append(controller.status())
            if len(statuses) == 6:
                raise StopLifecycle()

        async def wait(seconds):
            waits.append(seconds)
            self.assertLessEqual(seconds, config.evaluation_interval_seconds)
            if len(waits) == 3:
                reader.snapshot = InteractiveSessionSnapshot(7, 1)
            clock.now += seconds
            return False

        controller._publish = record_publish
        controller._wait = wait

        with self.assertRaises(StopLifecycle):
            await controller._run()

        self.assertEqual(
            [status.state for status in statuses],
            [
                LifecycleState.ACTIVE,
                LifecycleState.ACTIVE,
                LifecycleState.IDLE_TIMING,
                LifecycleState.ACTIVE,
                LifecycleState.ACTIVE,
                LifecycleState.IDLE_TIMING,
            ],
        )
        self.assertEqual(
            [status.blockers for status in statuses],
            [
                (LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,),
                (LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,),
                (),
                (LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,),
                (LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,),
                (),
            ],
        )
        self.assertEqual(len(waits), 5)
        self.assertEqual(statuses[2].idle_started_at, datetime(2026, 9, 9, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(statuses[2].next_transition_at, datetime(2026, 9, 9, 0, 1, 41, tzinfo=timezone.utc))
        self.assertEqual(statuses[5].idle_started_at, datetime(2026, 9, 9, 0, 0, 7, tzinfo=timezone.utc))
        self.assertEqual(statuses[5].next_transition_at, datetime(2026, 9, 9, 0, 1, 47, tzinfo=timezone.utc))

    async def test_repeated_activity_snapshot_does_not_publish_idle_timing(self):
        class Clock:
            now = 0

        class StopLifecycle(Exception):
            pass

        clock = Clock()
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=10)
        reader = SessionReader(InteractiveSessionSnapshot(100, 1))
        controller = LifecycleController(
            LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            reader,
        )
        clock.now = 105
        statuses = []
        publish = controller._publish

        def record_publish(*args, **kwargs):
            publish(*args, **kwargs)
            statuses.append(controller.status())
            if len(statuses) == 2:
                raise StopLifecycle()

        async def wait(_seconds):
            return False

        controller._publish = record_publish
        controller._wait = wait

        with self.assertRaises(StopLifecycle):
            await controller._run()

        self.assertEqual(reader.calls, 2)
        self.assertEqual(
            [status.state for status in statuses],
            [LifecycleState.ACTIVE, LifecycleState.ACTIVE],
        )
        self.assertEqual(
            [status.blockers for status in statuses],
            [
                (LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,),
                (LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY,),
            ],
        )
        self.assertEqual(statuses[0].idle_started_at, statuses[1].idle_started_at)
        self.assertEqual(statuses[0].next_transition_at, statuses[1].next_transition_at)

    async def test_normalized_idle_activity_does_not_reset_idle_timing_or_blockers(self):
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=10)
        reader = SessionReader(InteractiveSessionSnapshot(100, 1))
        controller = LifecycleController(
            LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: 0,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc),
            reader,
        )
        controller.monotonic = lambda: 110

        first = await controller._snapshot()
        first_candidate = controller._candidate(first)
        controller._publish(LifecycleState.IDLE_TIMING, first.blockers, idle_started_monotonic=first_candidate)
        first_idle_started = controller.status().idle_started_at
        second = await controller._snapshot()
        second_candidate = controller._candidate(second)
        controller._publish(LifecycleState.IDLE_TIMING, second.blockers, idle_started_monotonic=second_candidate)

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(first_candidate, second_candidate)
        self.assertEqual(first_idle_started, controller.status().idle_started_at)

    async def test_later_second_activity_resets_the_existing_idle_window(self):
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=10)
        reader = SessionReader(InteractiveSessionSnapshot(100, 1))
        controller = LifecycleController(
            LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: 0,
            lambda: datetime.now(timezone.utc),
            reader,
        )
        controller.monotonic = lambda: 111

        first_candidate = controller._candidate(await controller._snapshot())
        reader.snapshot = InteractiveSessionSnapshot(101, 1)
        second_candidate = controller._candidate(await controller._snapshot())

        self.assertEqual(first_candidate, 100)
        self.assertEqual(second_candidate, 101)

    async def test_positive_local_activity_persists_after_probe_clears(self):
        class Clock:
            now = 100

        clock = Clock()
        monitor = ActivityMonitor(("process:worker",))
        controller = LifecycleController(
            LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30),
            monitor,
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 1, 1, 10),
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
        )

        first = await controller._snapshot()
        first_candidate = controller._candidate(first)
        controller._publish(
            LifecycleState.IDLE_TIMING,
            idle_started_monotonic=first_candidate,
            next_transition_monotonic=first_candidate + controller.config.stable_idle_seconds,
        )
        first_status = controller.status()

        clock.now = 103
        monitor.active_result = ()
        second = await controller._snapshot()
        second_candidate = controller._candidate(second)
        controller._publish(
            LifecycleState.IDLE_TIMING,
            idle_started_monotonic=second_candidate,
            next_transition_monotonic=second_candidate + controller.config.stable_idle_seconds,
        )
        second_status = controller.status()

        self.assertEqual(first_candidate, 100)
        self.assertEqual(second_candidate, 100)
        self.assertEqual(first_status.idle_started_at, second_status.idle_started_at)
        self.assertEqual(first_status.next_transition_at, second_status.next_transition_at)

    async def test_local_activity_persists_while_another_blocker_remains(self):
        class Clock:
            now = 100

        clock = Clock()
        monitor = ActivityMonitor(("process:worker",))
        leases = LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30)
        leases.acquire("principal-one", 10)
        controller = LifecycleController(
            leases,
            monitor,
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 1, 1, 10),
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
        )

        first = await controller._snapshot()
        self.assertIn(LifecycleBlocker.LEASE, first)
        self.assertIn(LifecycleBlocker.LOCAL_ACTIVITY, first)
        self.assertEqual(controller._candidate(first), 100)

        clock.now = 103
        monitor.active_result = ()
        second = await controller._snapshot()

        self.assertIn(LifecycleBlocker.LEASE, second)
        self.assertNotIn(LifecycleBlocker.LOCAL_ACTIVITY, second)
        self.assertEqual(controller._candidate(second), 100)

    async def test_broker_operation_during_grace_preserves_original_timestamps(self):
        class Clock:
            now = 0

        clock = Clock()
        controller = LifecycleController(
            LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 60, 10, 10),
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
        )
        snapshots = [
            EligibilitySnapshot((LifecycleBlocker.BROKER_OPERATION,)),
            EligibilitySnapshot(()),
            EligibilitySnapshot(()),
        ]
        waits = iter((10, 12, 60))
        published = []

        async def snapshot():
            return snapshots.pop(0)

        async def wait(_seconds):
            clock.now = next(waits)
            return False

        def publish(*args, **kwargs):
            published.append(kwargs.copy())

        controller._snapshot = snapshot
        controller._wait = wait
        controller._publish = publish

        result = await controller._run_pending_grace(0)

        self.assertEqual(result, 60)
        self.assertTrue(published)
        self.assertEqual({item["grace_started_monotonic"] for item in published}, {0})
        self.assertEqual({item["next_transition_monotonic"] for item in published}, {60})

    async def test_repeated_broker_operations_do_not_extend_grace(self):
        class Clock:
            now = 0

        clock = Clock()
        controller = LifecycleController(
            LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 60, 10, 10),
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
        )
        snapshots = [
            EligibilitySnapshot((LifecycleBlocker.BROKER_OPERATION,))
            for _ in range(6)
        ] + [EligibilitySnapshot(())]
        waits = iter((10, 20, 30, 40, 50, 55, 60))
        published = []

        async def snapshot():
            return snapshots.pop(0)

        async def wait(_seconds):
            clock.now = next(waits)
            return False

        def publish(*args, **kwargs):
            published.append(kwargs.copy())

        controller._snapshot = snapshot
        controller._wait = wait
        controller._publish = publish

        result = await controller._run_pending_grace(0)

        self.assertEqual(result, 60)
        self.assertEqual(
            {(item["grace_started_monotonic"], item["next_transition_monotonic"]) for item in published},
            {(0, 60)},
        )

    async def test_broker_operation_past_grace_deadline_waits_for_clear_without_new_grace(self):
        class Clock:
            now = 0

        clock = Clock()
        controller = LifecycleController(
            LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 60, 10, 10),
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
        )
        snapshots = [
            EligibilitySnapshot((LifecycleBlocker.BROKER_OPERATION,)),
            EligibilitySnapshot((LifecycleBlocker.BROKER_OPERATION,)),
            EligibilitySnapshot(()),
        ]
        waits = iter((10, 60, 65, 65))
        published = []

        async def snapshot():
            return snapshots.pop(0)

        async def wait(_seconds):
            clock.now = next(waits)
            return False

        def publish(*args, **kwargs):
            published.append(kwargs.copy())

        controller._snapshot = snapshot
        controller._wait = wait
        controller._publish = publish

        result = await controller._run_pending_grace(0)

        self.assertEqual(result, 60)
        self.assertEqual({item["grace_started_monotonic"] for item in published}, {0})
        self.assertEqual({item["next_transition_monotonic"] for item in published}, {60})

    async def test_substantive_blocker_still_cancels_grace(self):
        class Clock:
            now = 0

        clock = Clock()
        controller = LifecycleController(
            LeaseStore(lambda: clock.now, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 60, 10, 10),
            lambda: clock.now,
            lambda: datetime(2026, 9, 9, tzinfo=timezone.utc) + timedelta(seconds=clock.now),
        )
        snapshots = [EligibilitySnapshot((LifecycleBlocker.LEASE,))]

        async def snapshot():
            return snapshots.pop(0)

        async def wait(_seconds):
            clock.now = 10
            return False

        controller._snapshot = snapshot
        controller._wait = wait

        result = await controller._run_pending_grace(0)

        self.assertIsNone(result)

    def test_lifecycle_generated_timestamps_retain_subsecond_precision(self):
        now = datetime(2026, 9, 9, 16, 45, 38, 654321, tzinfo=timezone.utc)
        controller = LifecycleController(
            LeaseStore(lambda: 0, lambda: now, 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            AutomaticSuspendConfig(True, 30, 10, 1, 1, 10),
            lambda: 100.75,
            lambda: now,
        )

        controller._publish(
            LifecycleState.PENDING_GRACE,
            grace_started_monotonic=100.25,
            next_transition_monotonic=101.125,
        )

        status = controller.status()
        self.assertEqual(status.grace_started_at.microsecond, 154321)
        self.assertEqual(status.next_transition_at.microsecond, 29321)

    async def test_gate_and_unavailable_logind_block_eligibility(self):
        runtime = Runtime()
        config = AutomaticSuspendConfig(True, 30, 1, 1, 1, 10)
        controller = LifecycleController(LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30), ConfiguredLocalActivityMonitor(()), Collector(ready_status()), runtime, config, lambda: 0, lambda: datetime.now(timezone.utc))
        held = runtime.gate.try_acquire_suspend()
        blockers = await controller._snapshot()
        self.assertIn(LifecycleBlocker.BROKER_OPERATION, blockers)
        self.assertNotIn(LifecycleBlocker.BROKER_OPERATION, await controller._snapshot(held))
        runtime.gate.release(held)
        status = ready_status()
        unavailable = ServiceStatus(status.observed_at, ServiceState.DEGRADED, status.cpu, status.gpu, ComponentStatus(Availability.UNAVAILABLE, error=OperationalErrorCode.OPERATIONAL))
        controller.collector.status = unavailable
        self.assertIn(LifecycleBlocker.SUSPEND_UNAVAILABLE, await controller._snapshot())

    async def test_status_reflects_acquired_and_released_lease_immediately(self):
        config = AutomaticSuspendConfig(True, 30, 1, 1, 1, 10)
        store = LeaseStore(lambda: 0, lambda: datetime.now(timezone.utc), 30)
        controller = LifecycleController(store, ConfiguredLocalActivityMonitor(()), Collector(ready_status()), Runtime(), config, lambda: 0, lambda: datetime.now(timezone.utc))
        lease = store.acquire("principal-one", 10)
        self.assertEqual(1, controller.status().leases.active_count)
        self.assertEqual(("principal-one",), controller.status().leases.principal_ids)
        store.release("principal-one", lease.lease_id)
        self.assertEqual(0, controller.status().leases.active_count)

    async def test_interactive_activity_and_unavailable_authority_are_distinct_blockers(self):
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=10)
        reader = SessionReader(InteractiveSessionSnapshot(0.0 + 1, 1))
        controller = LifecycleController(
            LeaseStore(lambda: 2, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: 2,
            lambda: datetime.now(timezone.utc),
            reader,
        )
        self.assertIn(LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY, await controller._snapshot())
        self.assertEqual(reader.calls, 1)

        reader.error = RuntimeError("dbus unavailable")
        blockers = await controller._snapshot()
        self.assertIn(LifecycleBlocker.INTERACTIVE_SESSION_UNAVAILABLE, blockers)
        self.assertNotIn(LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY, blockers)

    async def test_disabled_interactive_sessions_do_not_query_reader(self):
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10)
        reader = SessionReader(error=RuntimeError("must not be called"))
        controller = LifecycleController(
            LeaseStore(lambda: 2, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: 2,
            lambda: datetime.now(timezone.utc),
            None,
        )
        await controller._snapshot()
        self.assertEqual(reader.calls, 0)

    async def test_stale_interactive_session_does_not_restart_a_second_idle_window(self):
        config = AutomaticSuspendConfig(True, 30, 10, 1, 1, 10, interactive_sessions_enabled=True, interactive_activity_timeout_seconds=10)
        reader = SessionReader(InteractiveSessionSnapshot(1, 1))
        controller = LifecycleController(
            LeaseStore(lambda: 20, lambda: datetime.now(timezone.utc), 30),
            ConfiguredLocalActivityMonitor(()),
            Collector(ready_status()),
            Runtime(),
            config,
            lambda: 20,
            lambda: datetime.now(timezone.utc),
            reader,
        )
        snapshot = await controller._snapshot()
        self.assertFalse(snapshot)
        self.assertEqual(controller._candidate(snapshot), 20)
