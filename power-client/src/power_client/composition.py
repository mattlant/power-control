from __future__ import annotations

from typing import Protocol

from .adapters import AioHttpTransport, AsyncioSleeper, DefaultReadinessProbeFactory, FileCredentialSource, MonotonicClock, SocketWakeSender
from .client import PowerServiceClient
from .config import CredentialFileReference, ServiceConnectionConfig
from .models import Lease, LeaseId, ProfileApplication, ProfileName, WakeRequest, WakeResult
from .orchestration import PowerOrchestrator


def compose_client(connection: ServiceConnectionConfig, credential_file: CredentialFileReference) -> PowerServiceClient:
    return PowerServiceClient(AioHttpTransport(connection), FileCredentialSource(credential_file.path), owns_transport=True)


def compose_wake_orchestrator(connection: ServiceConnectionConfig, credential_file: CredentialFileReference) -> PowerOrchestrator:
    return PowerOrchestrator(
        compose_client(connection, credential_file),
        SocketWakeSender(),
        MonotonicClock(),
        AsyncioSleeper(),
        DefaultReadinessProbeFactory(connection.trust),
        owns_client=True,
        owns_probe_factory=True,
    )


class PowerManagement(Protocol):
    async def wake(self, request: WakeRequest) -> WakeResult: ...
    async def apply_profile(self, name: ProfileName) -> ProfileApplication: ...
    async def acquire_lease(self, ttl_seconds: int) -> Lease: ...
    async def renew_lease(self, lease_id: LeaseId, ttl_seconds: int) -> Lease: ...
    async def release_lease(self, lease_id: LeaseId) -> None: ...
    async def aclose(self) -> None: ...
    async def __aenter__(self) -> "PowerManagement": ...
    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None: ...


class _ComposedPowerManagement:
    def __init__(self, client: PowerServiceClient, orchestrator: PowerOrchestrator) -> None:
        self._client = client
        self._orchestrator = orchestrator

    async def wake(self, request: WakeRequest) -> WakeResult:
        return await self._orchestrator.wake(request)

    async def apply_profile(self, name: ProfileName) -> ProfileApplication:
        return await self._client.apply_profile(name)

    async def acquire_lease(self, ttl_seconds: int) -> Lease:
        return await self._client.acquire_lease(ttl_seconds)

    async def renew_lease(self, lease_id: LeaseId, ttl_seconds: int) -> Lease:
        return await self._client.renew_lease(lease_id, ttl_seconds)

    async def release_lease(self, lease_id: LeaseId) -> None:
        await self._client.release_lease(lease_id)

    async def aclose(self) -> None:
        await self._orchestrator.aclose()

    async def __aenter__(self) -> "_ComposedPowerManagement":
        await self._orchestrator.__aenter__()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self._orchestrator.__aexit__(exc_type, exc, traceback)


def compose_power_management(connection: ServiceConnectionConfig, credential_file: CredentialFileReference) -> PowerManagement:
    client = compose_client(connection, credential_file)
    orchestrator = PowerOrchestrator(
        client,
        SocketWakeSender(),
        MonotonicClock(),
        AsyncioSleeper(),
        DefaultReadinessProbeFactory(connection.trust),
        owns_client=True,
        owns_probe_factory=True,
    )
    return _ComposedPowerManagement(client, orchestrator)
