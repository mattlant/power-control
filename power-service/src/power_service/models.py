from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
import re
import math
from uuid import UUID

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _id(v: str) -> None:
    if not isinstance(v, str) or not _ID.fullmatch(v):
        raise ValueError("invalid identifier")


class Permission(StrEnum): STATUS = "status"; PROFILE = "profile"; SUSPEND = "suspend"; LEASE = "lease"


class Availability(StrEnum): AVAILABLE = "available"; UNAVAILABLE = "unavailable"


class OperationalErrorCode(
    StrEnum): NOT_FOUND = "not_found"; PERMISSION_DENIED = "permission_denied"; TIMEOUT = "timeout"; MALFORMED_OUTPUT = "malformed_output"; OPERATIONAL = "operational"


class ProfileErrorCode(
    StrEnum): PROFILE_NOT_FOUND = "profile_not_found"; INVALID_PROFILE = "invalid_profile"; UNSUPPORTED_CAPABILITY = "unsupported_capability"; CONFIGURATION = "configuration"; PERMISSION_DENIED = "permission_denied"; TIMEOUT = "timeout"; OPERATIONAL = "operational"


class CanSuspend(StrEnum): YES = "yes"; NO = "no"; CHALLENGE = "challenge"; UNKNOWN = "unknown"


class InhibitorKind(StrEnum): SLEEP = "sleep"; SHUTDOWN = "shutdown"; IDLE = "idle"


class InhibitorMode(StrEnum): BLOCK = "block"; DELAY = "delay"


class ServiceState(StrEnum): READY = "ready"; DEGRADED = "degraded"


class ProfileComponentState(
    StrEnum): APPLIED = "applied"; PARTIAL = "partial"; FAILED = "failed"; REJECTED = "rejected"; NOT_REQUESTED = "not_requested"


class ProfileOutcome(StrEnum): APPLIED = "applied"; PARTIAL = "partial"; FAILED = "failed"; REJECTED = "rejected"


class ReconciliationState(
    StrEnum): MATCHED = "matched"; UNMATCHED = "unmatched"; INDETERMINATE = "indeterminate"; UNAVAILABLE = "unavailable"


class SuspendOutcome(StrEnum):
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    AUTHORIZATION_REQUIRED = "authorization_required"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    OPERATIONAL_FAILURE = "operational_failure"


class LifecycleState(
    StrEnum): ACTIVE = "active"; IDLE_TIMING = "idle_timing"; PENDING_GRACE = "pending_grace"; SUSPEND_REQUESTED = "suspend_requested"


class LifecycleBlocker(StrEnum):
    LEASE = "lease"
    LOCAL_ACTIVITY = "local_activity"
    INHIBITOR = "inhibitor"
    BROKER_OPERATION = "broker_operation"
    SUSPEND_UNAVAILABLE = "suspend_unavailable"
    SUSPEND_FAILED = "suspend_failed"
    INTERACTIVE_SESSION_ACTIVITY = "interactive_session_activity"
    INTERACTIVE_SESSION_UNAVAILABLE = "interactive_session_unavailable"


class LeaseError(StrEnum): NOT_FOUND = "not_found"


class SuspendOrigin(StrEnum): DIRECT = "direct"; AUTOMATIC = "automatic"


@dataclass(frozen=True)
class InteractiveSession:
    session_id: str
    tty: str
    activity_monotonic_seconds: float

    def __post_init__(self):
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("invalid interactive session id")
        if not isinstance(self.tty, str) or not self.tty:
            raise ValueError("invalid interactive session tty")
        if not math.isfinite(self.activity_monotonic_seconds) or self.activity_monotonic_seconds <= 0:
            raise ValueError("invalid interactive session activity timestamp")


@dataclass(frozen=True)
class InteractiveSessionSnapshot:
    latest_activity_monotonic_seconds: float | None
    active_count: int

    def __post_init__(self):
        if not isinstance(self.active_count, int) or isinstance(self.active_count, bool) or self.active_count < 0:
            raise ValueError("invalid interactive session count")
        if self.latest_activity_monotonic_seconds is not None:
            if not math.isfinite(self.latest_activity_monotonic_seconds) or self.latest_activity_monotonic_seconds <= 0:
                raise ValueError("invalid latest interactive activity timestamp")
        if self.active_count == 0 and self.latest_activity_monotonic_seconds is not None:
            raise ValueError("empty interactive snapshot cannot have activity")
        if self.active_count > 0 and self.latest_activity_monotonic_seconds is None:
            raise ValueError("active interactive snapshot requires activity")


@dataclass(frozen=True)
class Principal:
    credential_id: str
    permissions: frozenset[Permission]

    def __post_init__(self): _id(self.credential_id)


@dataclass(frozen=True)
class CredentialRecord:
    credential_id: str
    salt: bytes
    scrypt_hash: bytes
    permissions: frozenset[Permission]

    def __post_init__(self):
        _id(self.credential_id)
        if not self.salt or len(self.scrypt_hash) != 32 or not self.permissions: raise ValueError("invalid credential")


@dataclass(frozen=True)
class ApiConfig:
    listen_host: IPv4Address | IPv6Address
    listen_port: int
    broker_socket: Path
    tls_cert: Path
    tls_key: Path
    request_timeout_seconds: int
    credentials: tuple[CredentialRecord, ...]

    def __post_init__(self):
        if not (
                self.listen_host.is_private or self.listen_host.is_loopback) or not 1 <= self.listen_port <= 65535 or not 1 <= self.request_timeout_seconds <= 30 or not self.credentials: raise ValueError(
            "invalid API configuration")
        if not all(p.is_absolute() for p in (self.broker_socket, self.tls_cert, self.tls_key)) or len(
            {x.credential_id for x in self.credentials}) != len(self.credentials): raise ValueError(
            "invalid API paths or credentials")


@dataclass(frozen=True)
class BrokerConfig:
    socket_path: Path
    api_uid: int
    gpu_index: int
    query_timeout_seconds: int | Path
    operation_timeout_seconds: int = 5
    profiles: tuple[ProfileDefinition, ...] = ()
    automatic_suspend: AutomaticSuspendConfig | None = None

    def __post_init__(self):
        if isinstance(self.query_timeout_seconds, Path): object.__setattr__(self, 'query_timeout_seconds',
                                                                            self.operation_timeout_seconds)
        if not self.socket_path.is_absolute() or self.api_uid < 0 or self.gpu_index < 0 or not 1 <= self.query_timeout_seconds <= 30 or not 1 <= self.operation_timeout_seconds <= 30: raise ValueError(
            "invalid broker configuration")
        if len({p.name for p in self.profiles}) != len(self.profiles): raise ValueError("duplicate profiles")


@dataclass(frozen=True)
class ComponentStatus:
    availability: Availability
    value: object | None = None
    error: OperationalErrorCode | None = None

    def __post_init__(self):
        if (self.availability is Availability.AVAILABLE) != (self.value is not None) or (
                self.availability is Availability.UNAVAILABLE) != (self.error is not None): raise ValueError(
            "invalid component status")


@dataclass(frozen=True)
class CpuPolicyStatus:
    policy: int
    affected_cpus: tuple[int, ...]
    driver: str
    governor: str
    available_governors: tuple[str, ...]
    hardware_min_khz: int
    hardware_max_khz: int
    configured_min_khz: int
    configured_max_khz: int
    epp: str | None = None
    available_epps: tuple[str, ...] | None = None

    def __post_init__(self):
        if self.policy < 0 or not self.affected_cpus or any(x < 0 for x in
                                                            self.affected_cpus) or not self.driver or not self.governor or not self.available_governors or min(
            self.hardware_min_khz,
            self.configured_min_khz) < 0 or self.hardware_min_khz > self.hardware_max_khz or self.configured_min_khz > self.configured_max_khz: raise ValueError(
            "invalid CPU policy")
        if (self.epp is None) != (self.available_epps is None) or self.available_epps is not None and (
                not self.epp or not self.available_epps or self.epp not in self.available_epps): raise ValueError(
            "invalid EPP")


@dataclass(frozen=True)
class CpuStatus: policies: tuple[CpuPolicyStatus, ...]


@dataclass(frozen=True)
class GpuStatus:
    index: int
    name: str
    driver_version: str
    power_limit_w: Decimal
    power_limit_min_w: Decimal
    power_limit_max_w: Decimal

    def __post_init__(self):
        if self.index < 0 or not self.name or not self.driver_version or self.power_limit_min_w < 0 or not self.power_limit_min_w <= self.power_limit_w <= self.power_limit_max_w: raise ValueError(
            "invalid GPU status")


@dataclass(frozen=True)
class InhibitorStatus: what: InhibitorKind; mode: InhibitorMode


@dataclass(frozen=True)
class LogindStatus: can_suspend: CanSuspend; inhibitors: tuple[InhibitorStatus, ...]


@dataclass(frozen=True)
class SuspendBlocker:
    what: InhibitorKind
    mode: InhibitorMode

    def __post_init__(self):
        if self.what is not InhibitorKind.SLEEP or self.mode is not InhibitorMode.BLOCK: raise ValueError(
            "invalid suspend blocker")


@dataclass(frozen=True)
class SuspendResult:
    outcome: SuspendOutcome
    can_suspend: CanSuspend | None = None
    blockers: tuple[SuspendBlocker, ...] = ()

    def __post_init__(self):
        if self.outcome is SuspendOutcome.ACCEPTED and (
                self.can_suspend not in (CanSuspend.YES, CanSuspend.CHALLENGE) or self.blockers): raise ValueError(
            "invalid accepted suspend")
        if self.outcome is SuspendOutcome.UNAVAILABLE and (
                self.can_suspend not in (CanSuspend.NO, CanSuspend.UNKNOWN) or self.blockers): raise ValueError(
            "invalid unavailable suspend")
        if self.outcome is SuspendOutcome.AUTHORIZATION_REQUIRED and (
                self.can_suspend is not CanSuspend.CHALLENGE or self.blockers): raise ValueError(
            "invalid authorization suspend")
        if self.outcome is SuspendOutcome.BLOCKED and (
                self.can_suspend not in (CanSuspend.YES, CanSuspend.CHALLENGE) or not self.blockers): raise ValueError(
            "invalid blocked suspend")
        if self.outcome in (SuspendOutcome.CONFLICT, SuspendOutcome.OPERATIONAL_FAILURE) and (
                self.can_suspend is not None or self.blockers): raise ValueError("invalid suspend outcome")


@dataclass(frozen=True)
class SuspendReceipt:
    """A broker-private capability for releasing a committed suspend."""

    receipt_id: UUID

    def __post_init__(self):
        if not isinstance(self.receipt_id, UUID) or str(self.receipt_id) != str(self.receipt_id):
            raise ValueError("invalid suspend receipt")


@dataclass(frozen=True)
class ProfileReconciliation: state: ReconciliationState; name: str | None = None; error: ProfileErrorCode | None = None


@dataclass(frozen=True)
class ServiceStatus:
    observed_at: datetime
    state: ServiceState
    cpu: ComponentStatus
    gpu: ComponentStatus
    logind: ComponentStatus
    profiles: ProfileReconciliation | None = None
    profile_names: tuple[str, ...] = ()
    lifecycle: LifecycleStatus | None = None

    def __post_init__(self):
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() != timezone.utc.utcoffset(
            None): raise ValueError("observed_at must be UTC")
        expected = ServiceState.READY if all(x.availability is Availability.AVAILABLE for x in
                                             (self.cpu, self.gpu, self.logind)) else ServiceState.DEGRADED
        if self.state is not expected: raise ValueError("inconsistent service state")


@dataclass(frozen=True)
class CpuProfileAction:
    policy_ids: tuple[int, ...]
    governor: str
    min_khz: int
    max_khz: int
    epp: str | None = None

    def __post_init__(self):
        if not self.policy_ids or tuple(sorted(
            set(self.policy_ids))) != self.policy_ids or not self.governor or self.min_khz <= 0 or self.max_khz <= 0 or self.min_khz > self.max_khz or self.epp == "": raise ValueError(
            "invalid CPU profile")


@dataclass(frozen=True)
class GpuProfileAction:
    power_limit_w: Decimal

    def __post_init__(self):
        if self.power_limit_w < 0: raise ValueError("invalid GPU profile")


@dataclass(frozen=True)
class ProfileDefinition:
    name: str
    cpu: CpuProfileAction
    gpu: GpuProfileAction

    def __post_init__(self): _id(self.name)


@dataclass(frozen=True)
class PolicyApplicationResult: policy: int; state: ProfileComponentState; error: ProfileErrorCode | None = None


@dataclass(frozen=True)
class CpuApplicationResult: state: ProfileComponentState; policies: tuple[PolicyApplicationResult, ...]


@dataclass(frozen=True)
class GpuApplicationResult: state: ProfileComponentState; index: int; error: ProfileErrorCode | None = None


@dataclass(frozen=True)
class ProfileApplyResult:
    name: str
    outcome: ProfileOutcome
    cpu: CpuApplicationResult
    gpu: GpuApplicationResult
    rejection: ProfileErrorCode | None = None


@dataclass(frozen=True)
class AutomaticSuspendConfig:
    enabled: bool
    max_lease_ttl_seconds: int
    stable_idle_seconds: int
    grace_seconds: int
    evaluation_interval_seconds: int
    max_status_principals: int
    local_activity_probes: tuple[LocalActivityProbeConfig, ...] = ()
    interactive_sessions_enabled: bool = False
    interactive_activity_timeout_seconds: int = 60

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or not isinstance(self.interactive_sessions_enabled,
                                                                bool): raise ValueError(
            "invalid automatic suspend switches")
        if not isinstance(self.interactive_activity_timeout_seconds, int) or isinstance(
            self.interactive_activity_timeout_seconds,
            bool) or not 1 <= self.interactive_activity_timeout_seconds <= 3600: raise ValueError(
            "invalid interactive activity timeout")
        if not 1 <= self.max_lease_ttl_seconds <= 86400 or not 1 <= self.stable_idle_seconds <= 86400 or not 1 <= self.grace_seconds <= 3600 or not 1 <= self.evaluation_interval_seconds <= 60 or not 1 <= self.max_status_principals <= 100: raise ValueError(
            "invalid automatic suspend configuration")


@dataclass(frozen=True)
class LocalActivityProbeConfig:
    kind: str
    value: str | int

    def __post_init__(self):
        if self.kind == "process_name" and isinstance(self.value, str) and re.fullmatch(r"[ -~]{1,15}",
                                                                                        self.value): return
        if self.kind == "tcp_listener" and isinstance(self.value, int) and 1 <= self.value <= 65535: return
        raise ValueError("invalid local activity probe")


@dataclass(frozen=True)
class Lease:
    lease_id: str
    principal_id: str
    ttl_seconds: int
    expires_at: datetime

    def __post_init__(self):
        from uuid import UUID
        if str(UUID(
            self.lease_id)) != self.lease_id or self.ttl_seconds <= 0 or self.expires_at.tzinfo is None: raise ValueError(
            "invalid lease")


@dataclass(frozen=True)
class LeaseSummary:
    active_count: int
    active_principal_count: int
    principal_ids: tuple[str, ...]
    principals_truncated: bool = False


@dataclass(frozen=True)
class AutomaticSuspendResult:
    outcome: SuspendOutcome
    occurred_at: datetime


@dataclass(frozen=True)
class LifecycleStatus:
    state: LifecycleState = LifecycleState.ACTIVE
    blockers: tuple[LifecycleBlocker, ...] = ()
    idle_started_at: datetime | None = None
    grace_started_at: datetime | None = None
    next_transition_at: datetime | None = None
    leases: LeaseSummary = LeaseSummary(0, 0, ())
    last_result: AutomaticSuspendResult | None = None
