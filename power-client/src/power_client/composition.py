from __future__ import annotations

from .adapters import AioHttpTransport, AsyncioSleeper, DefaultReadinessProbeFactory, FileCredentialSource, MonotonicClock, SocketWakeSender
from .client import PowerServiceClient
from .config import CredentialFileReference, ServiceConnectionConfig
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
