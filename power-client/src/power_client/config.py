from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from ipaddress import IPv4Address
import re
from urllib.parse import urlsplit

from .errors import ConfigurationError


@dataclass(frozen=True)
class TrustConfig:
    additional_ca_bundle_path: Path | None = None

    def __post_init__(self) -> None:
        if self.additional_ca_bundle_path is not None and not self.additional_ca_bundle_path.is_absolute():
            raise ConfigurationError("configuration", "additional_ca_bundle_path", "must be absolute")


@dataclass(frozen=True)
class ServiceConnectionConfig:
    endpoint: str
    request_timeout: float
    trust: TrustConfig

    def __post_init__(self) -> None:
        try:
            parts = urlsplit(self.endpoint)
            port = parts.port
        except ValueError as error:
            raise ConfigurationError("configuration", "endpoint", "has an invalid port") from error
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ConfigurationError("configuration", "endpoint", "must be an HTTPS origin")
        if port is not None and not 1 <= port <= 65535:
            raise ConfigurationError("configuration", "endpoint", "has an invalid port")
        if not isinstance(self.request_timeout, (int, float)) or isinstance(self.request_timeout, bool) or not math.isfinite(self.request_timeout) or self.request_timeout <= 0:
            raise ConfigurationError("configuration", "request_timeout", "must be finite and positive")


@dataclass(frozen=True)
class CredentialFileReference:
    path: Path

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ConfigurationError("configuration", "credential_file", "must be absolute")


@dataclass(frozen=True)
class WakeTarget:
    mac_address: str
    broadcast_address: IPv4Address
    udp_port: int

    def __post_init__(self) -> None:
        if not isinstance(self.mac_address, str):
            raise ConfigurationError("wake", "mac_address", "must be a colon-delimited EUI-48 address")
        match = re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", self.mac_address)
        if match is None:
            raise ConfigurationError("wake", "mac_address", "must be a colon-delimited EUI-48 address")
        if not isinstance(self.broadcast_address, IPv4Address):
            raise ConfigurationError("wake", "broadcast_address", "must be an IPv4 address")
        if type(self.udp_port) is not int or not 1 <= self.udp_port <= 65535:
            raise ConfigurationError("wake", "udp_port", "must be an integer from 1 to 65535")
        object.__setattr__(self, "mac_address", self.mac_address.lower())

    @property
    def mac_bytes(self) -> bytes:
        return bytes.fromhex(self.mac_address.replace(":", ""))


@dataclass(frozen=True)
class ServiceWaitPolicy:
    timeout_seconds: float
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (("timeout_seconds", self.timeout_seconds), ("poll_interval_seconds", self.poll_interval_seconds)):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError("wake", field_name, "must be finite and positive")
        if self.poll_interval_seconds > self.timeout_seconds:
            raise ConfigurationError("wake", "poll_interval_seconds", "must not exceed timeout_seconds")


def _validate_probe_identity(identity: object) -> None:
    if type(identity) is not str or not 1 <= len(identity) <= 64:
        raise ConfigurationError("wake", "probe.identity", "must be a non-empty string of at most 64 characters")


def _validate_host(host: object) -> None:
    if type(host) is not str or not 1 <= len(host) <= 253:
        raise ConfigurationError("wake", "probe.host", "must be a valid hostname")
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
        for label in labels
    ):
        raise ConfigurationError("wake", "probe.host", "must be a valid hostname")


@dataclass(frozen=True)
class ReadinessWaitPolicy:
    timeout_seconds: float
    attempt_timeout_seconds: float
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        for field_name, value in (
            ("readiness_wait_timeout", self.timeout_seconds),
            ("readiness_attempt_timeout", self.attempt_timeout_seconds),
            ("readiness_wait_poll_interval", self.poll_interval_seconds),
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ConfigurationError("wake", field_name, "must be finite and positive")
        if 2 * self.attempt_timeout_seconds + self.poll_interval_seconds > self.timeout_seconds:
            raise ConfigurationError("wake", "readiness_attempt_timeout", "does not leave room for two bounded attempts")


@dataclass(frozen=True)
class TcpReadinessTarget:
    identity: str
    host: str
    port: int

    def __post_init__(self) -> None:
        _validate_probe_identity(self.identity)
        _validate_host(self.host)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ConfigurationError("wake", "probe.port", "must be an integer from 1 to 65535")


@dataclass(frozen=True)
class HttpReadinessTarget:
    identity: str
    url: str
    expected_statuses: tuple[int, ...]

    def __post_init__(self) -> None:
        _validate_probe_identity(self.identity)
        if type(self.url) is not str:
            raise ConfigurationError("wake", "probe.url", "must be an HTTP or HTTPS URL")
        try:
            parts = urlsplit(self.url)
        except ValueError as error:
            raise ConfigurationError("wake", "probe.url", "must be an HTTP or HTTPS URL") from error
        try:
            hostname = parts.hostname
            port = parts.port
        except ValueError as error:
            raise ConfigurationError("wake", "probe.url", "has an invalid port") from error
        if (
            parts.scheme not in {"http", "https"}
            or not hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ConfigurationError("wake", "probe.url", "must be an HTTP or HTTPS URL without credentials, query, or fragment")
        _validate_host(hostname)
        if type(self.expected_statuses) is not tuple or not self.expected_statuses:
            raise ConfigurationError("wake", "probe.expected_statuses", "must be a non-empty tuple")
        if any(type(status) is not int or not 100 <= status <= 599 for status in self.expected_statuses):
            raise ConfigurationError("wake", "probe.expected_statuses", "must contain integer HTTP statuses from 100 to 599")
        if len(set(self.expected_statuses)) != len(self.expected_statuses):
            raise ConfigurationError("wake", "probe.expected_statuses", "must not contain duplicates")
