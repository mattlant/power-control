from __future__ import annotations

import asyncio
from collections.abc import Iterable

from .client import PowerServiceClient
from .config import ReadinessWaitPolicy, ServiceWaitPolicy
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    ProtocolError,
    ServiceReadinessTimeout,
    ServiceUnavailableError,
    TlsVerificationError,
    TransportError,
    ValidationError,
    WakeError,
    WorkloadReadinessError,
    WorkloadReadinessTimeout,
)
from .models import ProbeResult, ProbeState, ServiceStatus, WakeRequest, WakeResult
from .ports import Clock, ReadinessProbeFactory, Sleeper, WakeSender


class PowerOrchestrator:
    def __init__(
        self,
        client: PowerServiceClient,
        wake_sender: WakeSender,
        clock: Clock,
        sleeper: Sleeper,
        probe_factory: ReadinessProbeFactory,
        *,
        owns_client: bool = False,
        owns_probe_factory: bool = False,
    ) -> None:
        self._client = client
        self._wake_sender = wake_sender
        self._clock = clock
        self._sleeper = sleeper
        self._probe_factory = probe_factory
        self._owns_client = owns_client
        self._owns_probe_factory = owns_probe_factory
        self._closed = False

    async def wake(self, request: WakeRequest) -> WakeResult:
        if not isinstance(request, WakeRequest):
            raise ConfigurationError("wake", "request", "must be a WakeRequest")
        started = self._clock.now()
        await self._wake_sender.send_magic_packet(request.target)
        status = await self.wait_for_service(request.service_wait_policy)
        probe_results = ()
        if request.readiness_probes:
            probes = (self._probe_factory.build(definition) for definition in request.readiness_probes)
            probe_results = await self.wait_for_workloads(probes, request.readiness_wait_policy)
        return WakeResult(status, probe_results, self._clock.now() - started)

    async def wait_for_workloads(self, probes: Iterable[object], policy: ReadinessWaitPolicy) -> tuple[ProbeResult, ...]:
        deadline = self._clock.now() + policy.timeout_seconds
        results: list[ProbeResult] = []
        for probe in probes:
            identity = probe.identity
            probe_started = self._clock.now()
            last_failure: str | None = None
            while True:
                remaining = deadline - self._clock.now()
                if remaining <= 0:
                    raise WorkloadReadinessTimeout("wait_for_workloads", identity, self._clock.now() - probe_started, last_failure)
                try:
                    observation = await probe.check(min(policy.attempt_timeout_seconds, remaining))
                except TlsVerificationError as error:
                    raise WorkloadReadinessError(
                        "wait_for_workloads", identity, self._clock.now() - probe_started, error.cause_category
                    ) from error
                elapsed = self._clock.now() - probe_started
                if observation.state is ProbeState.READY:
                    results.append(ProbeResult(identity, ProbeState.READY.value, elapsed, None))
                    break
                last_failure = observation.last_failure
                remaining = deadline - self._clock.now()
                if remaining <= 0:
                    raise WorkloadReadinessTimeout("wait_for_workloads", identity, elapsed, last_failure)
                await self._sleeper.sleep(min(policy.poll_interval_seconds, remaining))
        return tuple(results)

    async def wait_for_service(self, policy: ServiceWaitPolicy) -> ServiceStatus:
        deadline = self._clock.now() + policy.timeout_seconds
        last_failure: str | None = None
        while True:
            try:
                return await self._client.get_status()
            except (TransportError, ServiceUnavailableError) as error:
                last_failure = error.cause_category if isinstance(error, TransportError) else error.server_code
                remaining = deadline - self._clock.now()
                if remaining <= 0:
                    raise ServiceReadinessTimeout("wait_for_service", policy.timeout_seconds, last_failure) from error
                await self._sleeper.sleep(min(policy.poll_interval_seconds, remaining))
                if self._clock.now() >= deadline:
                    raise ServiceReadinessTimeout("wait_for_service", policy.timeout_seconds, last_failure) from error
            except (AuthenticationError, AuthorizationError, NotFoundError, ConflictError, ValidationError, ProtocolError, ConfigurationError):
                raise
            except asyncio.CancelledError:
                raise

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        if self._owns_probe_factory:
            try:
                await self._probe_factory.aclose()
            except BaseException as error:
                failures.append(error)
        if self._owns_client:
            try:
                await self._client.aclose()
            except BaseException as error:
                failures.append(error)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("power orchestrator cleanup failed", failures)

    async def __aenter__(self) -> "PowerOrchestrator":
        if self._closed:
            raise ConfigurationError("wake", "state", "orchestrator is closed")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()
