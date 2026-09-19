from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import math
import re
from typing import Generic, Mapping, TypeAlias, TypeVar
from urllib.parse import urlsplit
from uuid import UUID

from .config import (
    HttpReadinessTarget,
    ReadinessWaitPolicy,
    ServiceWaitPolicy,
    TcpReadinessTarget,
    WakeTarget,
)

from .errors import ConfigurationError, ProtocolError


class HttpMethod(StrEnum):
    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"


class ServiceState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"


class Availability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class CanSuspend(StrEnum):
    YES = "yes"
    NO = "no"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"


class ReconciliationState(StrEnum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    INDETERMINATE = "indeterminate"
    UNAVAILABLE = "unavailable"


class LifecycleState(StrEnum):
    ACTIVE = "active"
    IDLE_TIMING = "idle_timing"
    PENDING_GRACE = "pending_grace"
    SUSPEND_REQUESTED = "suspend_requested"


class SuspendOutcome(StrEnum):
    ACCEPTED = "accepted"
    UNAVAILABLE = "unavailable"
    AUTHORIZATION_REQUIRED = "authorization_required"
    BLOCKED = "blocked"
    CONFLICT = "conflict"
    OPERATIONAL_FAILURE = "operational_failure"


class ProfileOutcome(StrEnum):
    APPLIED = "applied"
    PARTIAL = "partial"
    FAILED = "failed"


class ProfileComponentState(StrEnum):
    APPLIED = "applied"
    PARTIAL = "partial"
    FAILED = "failed"
    REJECTED = "rejected"
    NOT_REQUESTED = "not_requested"


JsonInputValue: TypeAlias = str | int | float | bool | None | list["JsonInputValue"] | Mapping[str, "JsonInputValue"]


@dataclass(frozen=True)
class FrozenJsonArray:
    items: tuple["FrozenJsonValue", ...]


@dataclass(frozen=True)
class FrozenJsonObject:
    items: tuple[tuple[str, "FrozenJsonValue"], ...]


FrozenJsonValue: TypeAlias = str | int | float | bool | None | FrozenJsonArray | FrozenJsonObject


def _freeze_json(value: object) -> FrozenJsonValue:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigurationError("transport_request", "body", "contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        pairs: list[tuple[str, FrozenJsonValue]] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise ConfigurationError("transport_request", "body", "contains a non-string object key")
            pairs.append((key, _freeze_json(item)))
        return FrozenJsonObject(tuple(pairs))
    if isinstance(value, list):
        return FrozenJsonArray(tuple(_freeze_json(item) for item in value))
    raise ConfigurationError("transport_request", "body", "contains an unsupported JSON value")


def freeze_json_object(body: Mapping[str, JsonInputValue]) -> FrozenJsonObject:
    if not isinstance(body, Mapping):
        raise ConfigurationError("transport_request", "body", "must be a JSON object")
    frozen = _freeze_json(body)
    if not isinstance(frozen, FrozenJsonObject):
        raise ConfigurationError("transport_request", "body", "must be a JSON object")
    return frozen


def thaw_json(value: FrozenJsonValue) -> object:
    if isinstance(value, FrozenJsonObject):
        return {key: thaw_json(item) for key, item in value.items}
    if isinstance(value, FrozenJsonArray):
        return [thaw_json(item) for item in value.items]
    return value


@dataclass(frozen=True)
class BearerCredential:
    credential_id: str
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        if not self.credential_id or not self.secret:
            raise ConfigurationError("credential", "value", "must contain an identifier and secret")


@dataclass(frozen=True, init=False)
class TransportRequest:
    method: HttpMethod
    path: str
    body: FrozenJsonObject | None
    credential: BearerCredential = field(repr=False)

    def __init__(self, method: HttpMethod, path: str, body: Mapping[str, JsonInputValue] | None, credential: BearerCredential) -> None:
        if not isinstance(method, HttpMethod):
            raise ConfigurationError("transport_request", "method", "is unsupported")
        parts = urlsplit(path)
        if not path.startswith("/v1/") or parts.scheme or parts.netloc or parts.query or parts.fragment:
            raise ConfigurationError("transport_request", "path", "must be an absolute /v1/ API path")
        if method in (HttpMethod.GET, HttpMethod.DELETE) and body is not None:
            raise ConfigurationError("transport_request", "body", "is not allowed for this method")
        if not isinstance(credential, BearerCredential):
            raise ConfigurationError("transport_request", "credential", "is invalid")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "body", None if body is None else freeze_json_object(body))
        object.__setattr__(self, "credential", credential)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes


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


@dataclass(frozen=True)
class CpuStatus:
    policies: tuple[CpuPolicyStatus, ...]


@dataclass(frozen=True)
class GpuStatus:
    index: int
    name: str
    driver_version: str
    power_limit_w: Decimal
    power_limit_min_w: Decimal
    power_limit_max_w: Decimal


@dataclass(frozen=True)
class Inhibitor:
    what: str
    mode: str


@dataclass(frozen=True)
class LogindStatus:
    can_suspend: CanSuspend
    inhibitors: tuple[Inhibitor, ...]


ComponentValue = TypeVar("ComponentValue")


@dataclass(frozen=True)
class Component(Generic[ComponentValue]):
    state: Availability
    value: ComponentValue | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.state is Availability.AVAILABLE:
            if self.value is None or self.error_code is not None:
                raise ValueError("available components require a value and no error")
        elif self.state is Availability.UNAVAILABLE:
            if self.value is not None or not self.error_code:
                raise ValueError("unavailable components require an error and no value")
        else:
            raise ValueError("invalid component state")


@dataclass(frozen=True)
class ProfileReconciliation:
    state: ReconciliationState
    name: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class ProfileCatalog:
    names: tuple[str, ...]
    reconciliation: ProfileReconciliation
    request_id: str


@dataclass(frozen=True)
class ProfileName:
    value: str

    def __post_init__(self) -> None:
        if type(self.value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.value) is None:
            raise ConfigurationError("apply_profile", "name", "must match [A-Za-z0-9_-]{1,64}")


@dataclass(frozen=True)
class LeaseId:
    value: str

    def __post_init__(self) -> None:
        try:
            canonical = type(self.value) is str and str(UUID(self.value)) == self.value
        except ValueError:
            canonical = False
        if not canonical:
            raise ConfigurationError("lease_id", "value", "must be a canonical UUID")


@dataclass(frozen=True)
class Lease:
    lease_id: LeaseId
    expires_at: datetime
    ttl_seconds: int
    request_id: str


@dataclass(frozen=True)
class ListedLease:
    lease_id: LeaseId
    expires_at: datetime
    ttl_seconds: int


@dataclass(frozen=True)
class LeaseCollection:
    leases: tuple[ListedLease, ...]
    request_id: str


@dataclass(frozen=True)
class StatusLeaseDetails:
    status: ServiceStatus
    leases: LeaseCollection | None


@dataclass(frozen=True)
class PolicyApplicationResult:
    policy: int
    state: ProfileComponentState
    error_code: str | None = None


@dataclass(frozen=True)
class CpuApplicationResult:
    state: ProfileComponentState
    policies: tuple[PolicyApplicationResult, ...]


@dataclass(frozen=True)
class GpuApplicationResult:
    state: ProfileComponentState
    index: int
    error_code: str | None = None


@dataclass(frozen=True)
class ProfileApplication:
    name: ProfileName
    outcome: ProfileOutcome
    cpu: CpuApplicationResult
    gpu: GpuApplicationResult
    status: ServiceStatus
    request_id: str


@dataclass(frozen=True)
class SuspendBlocker:
    what: str
    mode: str


@dataclass(frozen=True)
class SuspendReceipt:
    outcome: SuspendOutcome
    can_suspend: CanSuspend
    blockers: tuple[SuspendBlocker, ...]
    status: ServiceStatus
    request_id: str


@dataclass(frozen=True)
class LeaseSummary:
    active_count: int
    active_principal_count: int
    principal_ids: tuple[str, ...]
    principals_truncated: bool


@dataclass(frozen=True)
class AutomaticSuspendResult:
    outcome: SuspendOutcome
    occurred_at: datetime


@dataclass(frozen=True)
class LifecycleStatus:
    state: LifecycleState
    blockers: tuple[str, ...]
    idle_started_at: datetime | None
    grace_started_at: datetime | None
    next_transition_at: datetime | None
    leases: LeaseSummary
    last_result: AutomaticSuspendResult | None


@dataclass(frozen=True)
class ServiceStatus:
    request_id: str
    service_state: ServiceState
    cpu: Component[CpuStatus]
    gpu: Component[GpuStatus]
    logind: Component[LogindStatus]
    profiles: ProfileCatalog | None
    lifecycle: LifecycleStatus


@dataclass(frozen=True)
class WakeRequest:
    target: WakeTarget
    service_wait_policy: ServiceWaitPolicy
    readiness_probes: tuple["ReadinessProbeDefinition", ...] = ()
    readiness_wait_policy: ReadinessWaitPolicy | None = None

    def __post_init__(self) -> None:
        if type(self.readiness_probes) is not tuple:
            raise ConfigurationError("wake", "readiness_probes", "must be a tuple")
        identities: set[str] = set()
        for definition in self.readiness_probes:
            if not isinstance(definition, (TcpReadinessTarget, HttpReadinessTarget)):
                raise ConfigurationError("wake", "readiness_probes", "contains an unsupported probe definition")
            if definition.identity in identities:
                raise ConfigurationError("wake", "probe.identity", "must be unique within the wake request")
            identities.add(definition.identity)
        if self.readiness_probes and not isinstance(self.readiness_wait_policy, ReadinessWaitPolicy):
            raise ConfigurationError("wake", "readiness_wait_policy", "is required when probes are configured")
        if not self.readiness_probes and self.readiness_wait_policy is not None:
            raise ConfigurationError("wake", "readiness_wait_policy", "requires at least one probe")


ReadinessProbeDefinition: TypeAlias = TcpReadinessTarget | HttpReadinessTarget


class ProbeState(StrEnum):
    READY = "ready"
    NOT_READY = "not_ready"


@dataclass(frozen=True)
class ProbeObservation:
    state: ProbeState
    last_failure: str | None = None

    def __post_init__(self) -> None:
        if self.state is ProbeState.READY and self.last_failure is not None:
            raise ConfigurationError("wake", "probe.observation", "ready observations cannot contain a failure")
        if self.state is ProbeState.NOT_READY and not self.last_failure:
            raise ConfigurationError("wake", "probe.observation", "not-ready observations require a failure category")


@dataclass(frozen=True)
class ProbeResult:
    probe_identity: str
    state: str
    elapsed_seconds: float
    last_failure: str | None = None


@dataclass(frozen=True)
class WakeResult:
    service_status: ServiceStatus
    probe_results: tuple[ProbeResult, ...]
    elapsed_seconds: float


def _fail(detail: str, request_id: str | None = None) -> None:
    raise ProtocolError("get_status", request_id, detail)


def _object(value: object, name: str, request_id: str | None = None) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{name} must be an object", request_id)
    return value


def _string(value: object, name: str, request_id: str | None = None) -> str:
    if not isinstance(value, str) or not value:
        _fail(f"{name} must be a nonempty string", request_id)
    return value


def _integer(value: object, name: str, request_id: str | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(f"{name} must be an integer", request_id)
    return value


def _optional_string(data: Mapping[str, object], key: str, name: str, request_id: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    return _string(value, name, request_id)


def _timestamp(value: object, name: str, request_id: str | None = None) -> datetime:
    text = _string(value, name, request_id)
    if not text.endswith("Z"):
        _fail(f"{name} must be a UTC RFC 3339 timestamp", request_id)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail(f"{name} is invalid", request_id)
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        _fail(f"{name} must be UTC", request_id)
    return parsed


def _enum(kind: type[StrEnum], value: object, name: str, request_id: str | None = None) -> StrEnum:
    try:
        return kind(_string(value, name, request_id))
    except ValueError:
        _fail(f"{name} has an unknown value", request_id)


def _component(data: object, kind: str, request_id: str) -> Component[object]:
    raw = _object(data, kind, request_id)
    state = _enum(Availability, raw.get("state"), f"{kind}.state", request_id)
    if state is Availability.UNAVAILABLE:
        value_members = {
            "cpu": {"policies"},
            "gpu": {"index", "name", "driver_version", "power_limit_w", "power_limit_min_w", "power_limit_max_w"},
            "logind": {"can_suspend", "inhibitors"},
        }[kind]
        if value_members.intersection(raw):
            _fail(f"{kind} unavailable response contains value members", request_id)
        error = _object(raw.get("error"), f"{kind}.error", request_id)
        return Component(state, error_code=_string(error.get("code"), f"{kind}.error.code", request_id))
    if state is not Availability.AVAILABLE:
        _fail(f"{kind}.state is invalid", request_id)
    if "error" in raw:
        _fail(f"{kind} available response contains an error", request_id)
    if kind == "cpu":
        policies = raw.get("policies")
        if not isinstance(policies, list) or not policies:
            _fail("cpu.policies must be a nonempty array", request_id)
        parsed = []
        for item in policies:
            policy = _object(item, "cpu.policy", request_id)
            cpus = policy.get("affected_cpus")
            governors = policy.get("available_governors")
            if not isinstance(cpus, list) or not cpus or not isinstance(governors, list) or not governors:
                _fail("cpu policy arrays are invalid", request_id)
            epp = _optional_string(policy, "epp", "cpu.policy.epp", request_id)
            epps = policy.get("available_epps")
            if (epp is None) != (epps is None):
                _fail("cpu epp and available_epps must be jointly present", request_id)
            if epps is not None:
                if not isinstance(epps, list) or not epps or not all(isinstance(item, str) and item for item in epps):
                    _fail("cpu.available_epps is invalid", request_id)
                if epp not in epps:
                    _fail("cpu.epp must be included in available_epps", request_id)
            parsed.append(CpuPolicyStatus(
                _integer(policy.get("policy"), "cpu.policy.policy", request_id), tuple(_integer(x, "cpu.policy.cpu", request_id) for x in cpus),
                _string(policy.get("driver"), "cpu.policy.driver", request_id), _string(policy.get("governor"), "cpu.policy.governor", request_id),
                tuple(_string(x, "cpu.policy.governor", request_id) for x in governors),
                _integer(policy.get("hardware_min_khz"), "cpu.policy.hardware_min_khz", request_id), _integer(policy.get("hardware_max_khz"), "cpu.policy.hardware_max_khz", request_id),
                _integer(policy.get("configured_min_khz"), "cpu.policy.configured_min_khz", request_id), _integer(policy.get("configured_max_khz"), "cpu.policy.configured_max_khz", request_id),
                epp, tuple(epps) if epps is not None else None,
            ))
        return Component(state, CpuStatus(tuple(parsed)))
    if kind == "gpu":
        try:
            watts = [Decimal(_string(raw.get(key), key, request_id)) for key in ("power_limit_w", "power_limit_min_w", "power_limit_max_w")]
        except (InvalidOperation, ValueError):
            _fail("gpu power limit is invalid", request_id)
        if not all(value.is_finite() for value in watts) or not watts[1] <= watts[0] <= watts[2]:
            _fail("gpu power limits are inconsistent", request_id)
        return Component(state, GpuStatus(_integer(raw.get("index"), "gpu.index", request_id), _string(raw.get("name"), "gpu.name", request_id), _string(raw.get("driver_version"), "gpu.driver_version", request_id), *watts))
    inhibitors = raw.get("inhibitors")
    if not isinstance(inhibitors, list):
        _fail("logind.inhibitors must be an array", request_id)
    return Component(state, LogindStatus(_enum(CanSuspend, raw.get("can_suspend"), "logind.can_suspend", request_id), tuple(Inhibitor(_string(_object(item, "inhibitor", request_id).get("what"), "inhibitor.what", request_id), _string(_object(item, "inhibitor", request_id).get("mode"), "inhibitor.mode", request_id)) for item in inhibitors)))


def _parse_service_status(
    data: object,
    *,
    operation: str = "get_status",
    request_id: str | None = None,
) -> ServiceStatus:
    root = _object(data, "status")
    if request_id is None:
        request_id = _string(root.get("request_id"), "request_id")
    elif "request_id" in root:
        _fail("nested status must not contain request_id", request_id)
    service = _object(root.get("service"), "service", request_id)
    cpu = _component(root.get("cpu"), "cpu", request_id)
    gpu = _component(root.get("gpu"), "gpu", request_id)
    logind = _component(root.get("logind"), "logind", request_id)
    state = _enum(ServiceState, service.get("state"), "service.state", request_id)
    expected = ServiceState.READY if all(component.state is Availability.AVAILABLE for component in (cpu, gpu, logind)) else ServiceState.DEGRADED
    if state is not expected:
        _fail("service state is inconsistent with components", request_id)
    profiles = None
    if "profiles" in root:
        profile_data = _object(root["profiles"], "profiles", request_id)
        if _enum(Availability, profile_data.get("state"), "profiles.state", request_id) is not Availability.AVAILABLE:
            _fail("profiles must be available when present", request_id)
        names = profile_data.get("names")
        reconciliation = _object(profile_data.get("reconciliation"), "profiles.reconciliation", request_id)
        if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
            _fail("profiles.names is invalid", request_id)
        error = reconciliation.get("error")
        error_code = None if error is None else _string(_object(error, "profiles.error", request_id).get("code"), "profiles.error.code", request_id)
        profiles = ProfileCatalog(tuple(names), ProfileReconciliation(_enum(ReconciliationState, reconciliation.get("state"), "profiles.reconciliation.state", request_id), _optional_string(reconciliation, "name", "profiles.reconciliation.name", request_id), error_code), request_id)
    lifecycle_raw = _object(root.get("lifecycle"), "lifecycle", request_id)
    leases_raw = _object(lifecycle_raw.get("leases"), "lifecycle.leases", request_id)
    principals = leases_raw.get("principal_ids")
    blockers = lifecycle_raw.get("blockers")
    if not isinstance(principals, list) or not all(isinstance(item, str) for item in principals) or not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers) or not isinstance(leases_raw.get("principals_truncated"), bool):
        _fail("lifecycle values are invalid", request_id)
    def optional_timestamp(key: str) -> datetime | None:
        value = lifecycle_raw.get(key)
        return None if value is None else _timestamp(value, f"lifecycle.{key}", request_id)
    last = lifecycle_raw.get("last_result")
    last_result = None
    if last is not None:
        last_data = _object(last, "lifecycle.last_result", request_id)
        last_result = AutomaticSuspendResult(_enum(SuspendOutcome, last_data.get("outcome"), "lifecycle.last_result.outcome", request_id), _timestamp(last_data.get("occurred_at"), "lifecycle.last_result.occurred_at", request_id))
    idle_started_at = optional_timestamp("idle_started_at")
    grace_started_at = optional_timestamp("grace_started_at")
    next_transition_at = optional_timestamp("next_transition_at")
    timeline = [value for value in (idle_started_at, grace_started_at, next_transition_at) if value is not None]
    if any(previous > following for previous, following in zip(timeline, timeline[1:])):
        _fail("lifecycle timestamps are not chronological", request_id)
    lifecycle = LifecycleStatus(_enum(LifecycleState, lifecycle_raw.get("state"), "lifecycle.state", request_id), tuple(blockers), idle_started_at, grace_started_at, next_transition_at, LeaseSummary(_integer(leases_raw.get("active_count"), "leases.active_count", request_id), _integer(leases_raw.get("active_principal_count"), "leases.active_principal_count", request_id), tuple(principals), leases_raw["principals_truncated"]), last_result)
    return ServiceStatus(request_id, state, cpu, gpu, logind, profiles, lifecycle)


def parse_service_status(
    data: object,
    *,
    operation: str = "get_status",
    request_id: str | None = None,
) -> ServiceStatus:
    try:
        return _parse_service_status(data, operation=operation, request_id=request_id)
    except ProtocolError as error:
        if error.operation == operation:
            raise
        raise ProtocolError(operation, error.request_id, error.detail) from error


def _profile_error(detail: str, request_id: str | None) -> None:
    raise ProtocolError("apply_profile", request_id, detail)


def _profile_name(value: object, name: str, request_id: str | None) -> ProfileName:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) is None:
        _profile_error(f"{name} is invalid", request_id)
    return ProfileName(value)


def _profile_reconciliation(data: object, request_id: str) -> ProfileReconciliation:
    raw = _object(data, "reconciliation", request_id)
    state = _enum(ReconciliationState, raw.get("state"), "reconciliation.state", request_id)
    name = raw.get("name")
    if state is ReconciliationState.MATCHED:
        if "name" not in raw:
            _fail("matched reconciliation must contain only name", request_id)
        parsed_name = _profile_name(name, "reconciliation.name", request_id)
        return ProfileReconciliation(state, parsed_name.value)
    if "name" in raw:
        _fail("unmatched reconciliation must not contain name", request_id)
    return ProfileReconciliation(state)


def parse_profile_catalog(data: object) -> ProfileCatalog:
    try:
        root = _object(data, "profiles")
        request_id = _string(root.get("request_id"), "request_id")
        state = _enum(Availability, root.get("state"), "state", request_id)
        names = root.get("names")
        if not isinstance(names, list):
            _fail("names must be an array", request_id)
        parsed_names = tuple(_profile_name(item, "names", request_id).value for item in names)
        if len(set(parsed_names)) != len(parsed_names):
            _fail("names must be unique", request_id)
        reconciliation = _profile_reconciliation(root.get("reconciliation"), request_id)
        if state is Availability.UNAVAILABLE:
            if reconciliation.state is not ReconciliationState.UNAVAILABLE:
                _fail("unavailable profiles require unavailable reconciliation", request_id)
        elif reconciliation.state not in {
            ReconciliationState.MATCHED,
            ReconciliationState.UNMATCHED,
            ReconciliationState.INDETERMINATE,
        }:
            _fail("available profiles have invalid reconciliation", request_id)
        return ProfileCatalog(parsed_names, reconciliation, request_id)
    except ProtocolError as error:
        raise ProtocolError("list_profiles", error.request_id, error.detail) from error


def _validate_application_component(
    state: ProfileComponentState,
    error_code: str | None,
    name: str,
    request_id: str,
) -> None:
    error_states = {ProfileComponentState.FAILED, ProfileComponentState.REJECTED}
    if state in error_states and not error_code:
        _profile_error(f"{name} requires an error", request_id)
    if state not in error_states and error_code is not None:
        _profile_error(f"{name} must not contain an error", request_id)


def _application_error_code(data: object, name: str, request_id: str) -> str | None:
    if data is None:
        return None
    error = _object(data, name, request_id)
    return _string(error.get("code"), f"{name}.code", request_id)


def _parse_cpu_application(data: object, request_id: str) -> CpuApplicationResult:
    raw = _object(data, "cpu", request_id)
    state = _enum(ProfileComponentState, raw.get("state"), "cpu.state", request_id)
    policies = raw.get("policies")
    if not isinstance(policies, list) or not policies:
        _profile_error("cpu.policies must be a nonempty array", request_id)
    parsed: list[PolicyApplicationResult] = []
    seen: set[int] = set()
    for item in policies:
        policy = _object(item, "cpu.policy", request_id)
        number = _integer(policy.get("policy"), "cpu.policy.policy", request_id)
        if number < 0 or number in seen:
            _profile_error("cpu policy numbers must be nonnegative and unique", request_id)
        seen.add(number)
        policy_state = _enum(ProfileComponentState, policy.get("state"), "cpu.policy.state", request_id)
        error_code = _application_error_code(policy.get("error"), "cpu.policy.error", request_id)
        _validate_application_component(policy_state, error_code, "cpu.policy", request_id)
        parsed.append(PolicyApplicationResult(number, policy_state, error_code))
    policy_states = {item.state for item in parsed}
    failed_states = {ProfileComponentState.FAILED, ProfileComponentState.REJECTED}
    if state is ProfileComponentState.APPLIED and policy_states & failed_states:
        _profile_error("applied CPU component contains failed policies", request_id)
    if state is ProfileComponentState.PARTIAL and not (policy_states & failed_states or ProfileComponentState.PARTIAL in policy_states):
        _profile_error("partial CPU component has no partial or failed policy", request_id)
    if state is ProfileComponentState.FAILED and not policy_states & failed_states:
        _profile_error("failed CPU component has no failed policy", request_id)
    return CpuApplicationResult(state, tuple(parsed))


def _parse_gpu_application(data: object, request_id: str) -> GpuApplicationResult:
    raw = _object(data, "gpu", request_id)
    state = _enum(ProfileComponentState, raw.get("state"), "gpu.state", request_id)
    index = _integer(raw.get("index"), "gpu.index", request_id)
    if index < 0:
        _profile_error("gpu.index must be nonnegative", request_id)
    error_code = _application_error_code(raw.get("error"), "gpu.error", request_id)
    _validate_application_component(state, error_code, "gpu", request_id)
    return GpuApplicationResult(state, index, error_code)


def parse_broker_status(data: object, *, operation: str, request_id: str) -> ServiceStatus:
    raw = _object(data, "status", request_id)
    if "request_id" in raw or "service" in raw or "service_state" not in raw:
        raise ProtocolError(operation, request_id, "broker status has the wrong wire shape")
    broker = dict(raw)
    broker["request_id"] = request_id
    broker["service"] = {"state": broker.pop("service_state")}
    try:
        return parse_service_status(broker, request_id=None)
    except ProtocolError as error:
        raise ProtocolError(operation, request_id, error.detail) from error


def parse_profile_application(data: object, *, requested_name: ProfileName) -> ProfileApplication:
    root = _object(data, "profile application")
    request_id = _string(root.get("request_id"), "request_id")
    application = _object(root.get("application"), "application", request_id)
    returned_name = _profile_name(application.get("profile"), "application.profile", request_id)
    if returned_name != requested_name:
        _profile_error("response profile does not match request", request_id)
    outcome = _enum(ProfileOutcome, application.get("outcome"), "application.outcome", request_id)
    cpu = _parse_cpu_application(application.get("cpu"), request_id)
    gpu = _parse_gpu_application(application.get("gpu"), request_id)
    if outcome is ProfileOutcome.APPLIED and any(
        component.state in {ProfileComponentState.FAILED, ProfileComponentState.REJECTED}
        for component in (cpu, gpu)
    ):
        _profile_error("applied outcome contains failed components", request_id)
    if outcome in {ProfileOutcome.PARTIAL, ProfileOutcome.FAILED} and not (
        cpu.state in {ProfileComponentState.FAILED, ProfileComponentState.REJECTED}
        or gpu.state in {ProfileComponentState.FAILED, ProfileComponentState.REJECTED}
        or any(policy.state in {ProfileComponentState.FAILED, ProfileComponentState.REJECTED} for policy in cpu.policies)
    ):
        _profile_error("non-applied outcome has no failed component", request_id)
    status = parse_broker_status(root.get("status"), operation="apply_profile", request_id=request_id)
    return ProfileApplication(returned_name, outcome, cpu, gpu, status, request_id)


def parse_suspend_receipt(data: object) -> SuspendReceipt:
    try:
        root = _object(data, "suspend receipt")
        request_id = _string(root.get("request_id"), "request_id")
        suspend = _object(root.get("suspend"), "suspend", request_id)
        if _enum(SuspendOutcome, suspend.get("outcome"), "suspend.outcome", request_id) is not SuspendOutcome.ACCEPTED:
            _fail("suspend receipt outcome must be accepted", request_id)
        can_suspend = _enum(CanSuspend, suspend.get("can_suspend"), "suspend.can_suspend", request_id)
        if "blockers" in suspend:
            _fail("accepted suspend receipt must not contain blockers", request_id)
        status = parse_service_status(root.get("status"), request_id=request_id)
        return SuspendReceipt(SuspendOutcome.ACCEPTED, can_suspend, (), status, request_id)
    except ProtocolError as error:
        raise ProtocolError("request_suspend", error.request_id, error.detail) from error


def parse_lease(data: object, *, operation: str) -> Lease:
    try:
        root = _object(data, "lease response")
        request_id = _string(root.get("request_id"), "request_id")
        lease = _object(root.get("lease"), "lease", request_id)
        try:
            lease_id = LeaseId(lease.get("id"))
        except (ConfigurationError, ValueError, AttributeError) as error:
            _fail("lease.id must be a canonical UUID", request_id)
        expires_at = _timestamp(lease.get("expires_at"), "lease.expires_at", request_id)
        ttl_seconds = _integer(lease.get("ttl_seconds"), "lease.ttl_seconds", request_id)
        if ttl_seconds < 1:
            _fail("lease.ttl_seconds must be at least 1", request_id)
        return Lease(lease_id, expires_at, ttl_seconds, request_id)
    except ProtocolError as error:
        raise ProtocolError(operation, error.request_id, error.detail) from error


def parse_lease_collection(data: object) -> LeaseCollection:
    try:
        root = _object(data, "lease collection")
        request_id = _string(root.get("request_id"), "request_id")
        leases = root.get("leases")
        if not isinstance(leases, list):
            _fail("leases must be an array", request_id)
        parsed = []
        for index, value in enumerate(leases):
            lease = _object(value, f"leases[{index}]", request_id)
            try:
                lease_id = LeaseId(lease.get("id"))
            except (ConfigurationError, ValueError, AttributeError):
                _fail(f"leases[{index}].id must be a canonical UUID", request_id)
            expires_at = _timestamp(lease.get("expires_at"), f"leases[{index}].expires_at", request_id)
            ttl_seconds = _integer(lease.get("ttl_seconds"), f"leases[{index}].ttl_seconds", request_id)
            if ttl_seconds < 1:
                _fail(f"leases[{index}].ttl_seconds must be at least 1", request_id)
            parsed.append(ListedLease(lease_id, expires_at, ttl_seconds))
        return LeaseCollection(tuple(parsed), request_id)
    except ProtocolError as error:
        raise ProtocolError("list_leases", error.request_id, error.detail) from error
