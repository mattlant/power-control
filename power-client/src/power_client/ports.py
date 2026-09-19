from __future__ import annotations

from typing import Protocol, runtime_checkable

from .config import WakeTarget
from .models import BearerCredential, ProbeObservation, ReadinessProbeDefinition, TransportRequest, TransportResponse


@runtime_checkable
class CredentialSource(Protocol):
    async def read(self) -> BearerCredential: ...


@runtime_checkable
class HttpTransport(Protocol):
    async def send(self, request: TransportRequest) -> TransportResponse: ...
    async def aclose(self) -> None: ...


@runtime_checkable
class WakeSender(Protocol):
    async def send_magic_packet(self, target: WakeTarget) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> float: ...


@runtime_checkable
class Sleeper(Protocol):
    async def sleep(self, delay_seconds: float) -> None: ...


@runtime_checkable
class ReadinessProbe(Protocol):
    @property
    def identity(self) -> str: ...

    async def check(self, timeout_seconds: float) -> ProbeObservation: ...


@runtime_checkable
class ReadinessProbeFactory(Protocol):
    def build(self, definition: ReadinessProbeDefinition) -> ReadinessProbe: ...
    async def aclose(self) -> None: ...
