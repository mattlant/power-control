from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta

from .local_activity import LocalActivityError
from .models import (
    AutomaticSuspendResult,
    InteractiveSessionSnapshot,
    LifecycleBlocker,
    LifecycleState,
    LifecycleStatus,
    SuspendOrigin,
    SuspendOutcome,
)


@dataclass(frozen=True)
class EligibilitySnapshot:
    blockers: tuple[LifecycleBlocker, ...]
    interactive_activity_monotonic_seconds: float | None = None
    local_activity_monotonic_seconds: float | None = None
    evaluated_monotonic_seconds: float | None = None

    def __bool__(self):
        return bool(self.blockers)

    def __contains__(self, value):
        return value in self.blockers

    def __iter__(self):
        return iter(self.blockers)


class LifecycleController:
    """The broker's sole automatic-suspend authority."""

    def __init__(
            self,
            leases,
            monitor,
            collector,
            runtime,
            config,
            monotonic,
            utc,
            interactive_session_reader=None,
    ):
        self.leases = leases
        self.monitor = monitor
        self.collector = collector
        self.runtime = runtime
        self.config = config
        self.monotonic = monotonic
        self.utc = utc
        self.interactive_session_reader = interactive_session_reader
        self._event = asyncio.Event()
        self._task = None
        self._status = LifecycleStatus()
        self._reconciliation_baseline = self.monotonic()
        self._activity_baseline = self._reconciliation_baseline
        self._projected_timestamps = {}
        self._lifecycle_generation = 0
        self._automatic_suspend_commit_lock = asyncio.Lock()

    def status(self):
        return replace(
            self._status,
            leases=self.leases.summary(self.config.max_status_principals),
        )

    def signal(self):
        self._event.set()

    def reconfigure(self, config, interactive_session_reader):
        self.config = config
        self.interactive_session_reader = interactive_session_reader

    async def start(self):
        if self.config.enabled and self._task is None:
            self._reconciliation_baseline = self.monotonic()
            self._activity_baseline = self._reconciliation_baseline
            self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        self._status = LifecycleStatus(blockers=(LifecycleBlocker.BROKER_OPERATION,))

    async def reconcile(self, clear_leases=True):
        async with self._automatic_suspend_commit_lock:
            self._reset_reconciliation(clear_leases)

    def _reset_reconciliation(self, clear_leases):
        if clear_leases:
            self.leases.clear()
        self._reconciliation_baseline = self.monotonic()
        self._activity_baseline = self._reconciliation_baseline
        self._projected_timestamps.clear()
        self._status = LifecycleStatus()
        self._lifecycle_generation += 1
        self.signal()

    async def reconcile_resume(self):
        async with self._automatic_suspend_commit_lock:
            self._reset_reconciliation(clear_leases=True)

    def _generation_is_current(self, generation):
        return generation == self._lifecycle_generation

    async def _wait(self, seconds):
        self._event.clear()
        try:
            await asyncio.wait_for(self._event.wait(), max(0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def _project_monotonic(self, timestamp):
        if timestamp is None:
            return None
        projected = self._projected_timestamps.get(timestamp)
        if projected is None:
            elapsed = self.monotonic() - timestamp
            projected = self.utc() - timedelta(seconds=elapsed)
            self._projected_timestamps[timestamp] = projected
        return projected

    async def _snapshot(self, owned_reservation=None):
        generation = self._lifecycle_generation
        self.leases.expire()
        blockers = []

        if self.leases.summary(self.config.max_status_principals).active_count:
            blockers.append(LifecycleBlocker.LEASE)

        local_activity_observed = False
        try:
            local_activity_observed = bool(self.monitor.active())
            if local_activity_observed:
                blockers.append(LifecycleBlocker.LOCAL_ACTIVITY)
        except LocalActivityError:
            blockers.append(LifecycleBlocker.LOCAL_ACTIVITY)

        try:
            status = await self.collector.collect()
            if self.runtime.gate.busy_except(owned_reservation):
                blockers.append(LifecycleBlocker.BROKER_OPERATION)
            if status.logind.availability.value != "available":
                blockers.append(LifecycleBlocker.SUSPEND_UNAVAILABLE)
            elif any(
                    inhibitor.what.value == "sleep" and inhibitor.mode.value == "block"
                    for inhibitor in status.logind.value.inhibitors
            ):
                blockers.append(LifecycleBlocker.INHIBITOR)
        except Exception:
            blockers.append(LifecycleBlocker.SUSPEND_UNAVAILABLE)

        latest_activity = None
        if self.interactive_session_reader is not None:
            try:
                snapshot = await self.interactive_session_reader.read()
                if not isinstance(snapshot, InteractiveSessionSnapshot):
                    raise ValueError("invalid interactive session snapshot")
                latest_activity = snapshot.latest_activity_monotonic_seconds
            except Exception:
                latest_activity = None
                blockers.append(LifecycleBlocker.INTERACTIVE_SESSION_UNAVAILABLE)

        evaluated_at = self.monotonic()
        if latest_activity is not None:
            if latest_activity > evaluated_at:
                blockers.append(LifecycleBlocker.INTERACTIVE_SESSION_UNAVAILABLE)
                latest_activity = None
            elif evaluated_at - latest_activity < self.config.interactive_activity_timeout_seconds:
                blockers.append(LifecycleBlocker.INTERACTIVE_SESSION_ACTIVITY)

        local_activity_timestamp = evaluated_at if local_activity_observed else None
        activity_timestamps = tuple(
            timestamp
            for timestamp in (latest_activity, local_activity_timestamp)
            if timestamp is not None
        )
        if activity_timestamps and self._generation_is_current(generation):
            self._activity_baseline = max(
                self._activity_baseline,
                *activity_timestamps,
            )

        return EligibilitySnapshot(
            blockers=tuple(sorted(set(blockers), key=lambda blocker: blocker.value)),
            interactive_activity_monotonic_seconds=latest_activity,
            local_activity_monotonic_seconds=local_activity_timestamp,
            evaluated_monotonic_seconds=evaluated_at,
        )

    def _publish(
            self,
            state,
            blockers=(),
            idle_started_monotonic=None,
            grace_started_monotonic=None,
            next_transition_monotonic=None,
            result=None,
    ):
        timestamps = {
            timestamp
            for timestamp in (
                idle_started_monotonic,
                grace_started_monotonic,
                next_transition_monotonic,
            )
            if timestamp is not None
        }
        self._status = LifecycleStatus(
            state,
            tuple(blockers),
            self._project_monotonic(idle_started_monotonic),
            self._project_monotonic(grace_started_monotonic),
            self._project_monotonic(next_transition_monotonic),
            self.leases.summary(self.config.max_status_principals),
            result if result is not None else self._status.last_result,
        )
        self._projected_timestamps = {
            timestamp: self._projected_timestamps[timestamp]
            for timestamp in timestamps
        }

    def _candidate(self, snapshot):
        return max(self._reconciliation_baseline, self._activity_baseline)

    async def _wait_until_candidate_deadline(self, snapshot, candidate):
        now = snapshot.evaluated_monotonic_seconds
        deadline = candidate + self.config.stable_idle_seconds
        if deadline <= now:
            return False
        wait_seconds = deadline - now
        if self.interactive_session_reader is not None:
            wait_seconds = min(wait_seconds, self.config.evaluation_interval_seconds)
        return await self._wait(wait_seconds)

    async def _run_pending_grace(self, candidate, generation=None):
        generation = self._lifecycle_generation if generation is None else generation
        grace_started = self.monotonic()
        grace_deadline = grace_started + self.config.grace_seconds
        while True:
            if not self._generation_is_current(generation):
                return None
            self._publish(
                LifecycleState.PENDING_GRACE,
                idle_started_monotonic=candidate,
                grace_started_monotonic=grace_started,
                next_transition_monotonic=grace_deadline,
            )
            wait_seconds = min(
                self.config.evaluation_interval_seconds,
                max(0, grace_deadline - self.monotonic()),
            )
            await self._wait(wait_seconds)
            if not self._generation_is_current(generation):
                return None
            snapshot = await self._snapshot()
            if not self._generation_is_current(generation):
                return None
            substantive_blockers = tuple(
                blocker
                for blocker in snapshot.blockers
                if blocker is not LifecycleBlocker.BROKER_OPERATION
            )
            if substantive_blockers:
                return None
            now = self.monotonic()
            if LifecycleBlocker.BROKER_OPERATION in snapshot.blockers:
                if now >= grace_deadline:
                    await self._wait(self.config.evaluation_interval_seconds)
                continue
            if now < grace_deadline:
                continue
            return grace_deadline

    async def _run(self):
        while True:
            generation = self._lifecycle_generation
            snapshot = await self._snapshot()
            if not self._generation_is_current(generation):
                continue
            candidate = self._candidate(snapshot)

            if snapshot.blockers:
                self._publish(
                    LifecycleState.ACTIVE,
                    snapshot.blockers,
                    idle_started_monotonic=candidate,
                    next_transition_monotonic=candidate + self.config.stable_idle_seconds,
                )
                interrupted = await self._wait_until_candidate_deadline(snapshot, candidate)
                if interrupted:
                    continue
                snapshot = await self._snapshot()
                if not self._generation_is_current(generation):
                    continue
                candidate = self._candidate(snapshot)
                if snapshot.blockers:
                    self._publish(
                        LifecycleState.ACTIVE,
                        snapshot.blockers,
                        idle_started_monotonic=candidate,
                        next_transition_monotonic=candidate + self.config.stable_idle_seconds,
                    )
                    await self._wait(self.config.evaluation_interval_seconds)
                    continue

            now = snapshot.evaluated_monotonic_seconds
            if candidate + self.config.stable_idle_seconds > now:
                self._publish(
                    LifecycleState.IDLE_TIMING,
                    idle_started_monotonic=candidate,
                    next_transition_monotonic=candidate + self.config.stable_idle_seconds,
                )
                await self._wait_until_candidate_deadline(snapshot, candidate)
                continue

            reservation_deadline = await self._run_pending_grace(candidate, generation)
            if reservation_deadline is None:
                continue

            async with self._automatic_suspend_commit_lock:
                if not self._generation_is_current(generation):
                    continue
                reservation = self.runtime.reserve_automatic_suspend()
                if reservation is None:
                    self._publish(
                        LifecycleState.ACTIVE,
                        (LifecycleBlocker.BROKER_OPERATION,),
                    )
                    continue
                try:
                    self._publish(
                        LifecycleState.SUSPEND_REQUESTED,
                        idle_started_monotonic=candidate,
                    )
                    final_snapshot = await self._snapshot(reservation)
                    if not self._generation_is_current(generation):
                        continue
                    if final_snapshot:
                        self._publish(LifecycleState.ACTIVE, final_snapshot.blockers)
                        continue
                    result, _ = await self.runtime.suspend_reserved(
                        reservation,
                        SuspendOrigin.AUTOMATIC,
                    )
                    self._publish(
                        LifecycleState.ACTIVE,
                        result=AutomaticSuspendResult(result.outcome, self.utc()),
                    )
                except Exception:
                    self._publish(
                        LifecycleState.ACTIVE,
                        (LifecycleBlocker.SUSPEND_FAILED,),
                        result=AutomaticSuspendResult(
                            SuspendOutcome.OPERATIONAL_FAILURE,
                            self.utc(),
                        ),
                    )
                finally:
                    self.runtime.release_automatic_suspend(reservation)
