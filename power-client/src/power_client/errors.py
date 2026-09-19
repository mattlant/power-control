from __future__ import annotations


class PowerClientError(Exception):
    """Base class for errors deliberately exposed by the client."""


class ConfigurationError(PowerClientError):
    def __init__(self, operation: str, field: str, message: str) -> None:
        self.operation = operation
        self.field = field
        self.message = message
        super().__init__(f"{operation}: invalid {field}: {message}")


class TransportError(PowerClientError):
    def __init__(self, operation: str, endpoint: str, cause_category: str) -> None:
        self.operation = operation
        self.endpoint = endpoint
        self.cause_category = cause_category
        super().__init__(f"{operation}: transport failure at {endpoint} ({cause_category})")


class TlsVerificationError(TransportError):
    pass


class RequestTimeoutError(TransportError):
    pass


class ApiError(PowerClientError):
    def __init__(self, operation: str, http_status: int, request_id: str, server_code: str) -> None:
        self.operation = operation
        self.http_status = http_status
        self.request_id = request_id
        self.server_code = server_code
        super().__init__(f"{operation}: API status {http_status} ({server_code}), request {request_id}")


class AuthenticationError(ApiError):
    pass


class AuthorizationError(ApiError):
    pass


class NotFoundError(ApiError):
    pass


class ConflictError(ApiError):
    pass


class ValidationError(ApiError):
    pass


class ServiceUnavailableError(ApiError):
    pass


class ProtocolError(PowerClientError):
    def __init__(self, operation: str, request_id: str | None, detail: str) -> None:
        self.operation = operation
        self.request_id = request_id
        self.detail = detail
        suffix = f", request {request_id}" if request_id else ""
        super().__init__(f"{operation}: protocol failure ({detail}){suffix}")


class WakeError(PowerClientError):
    def __init__(self, operation: str, target: object, cause_category: str) -> None:
        self.operation = operation
        self.target = target
        self.cause_category = cause_category
        super().__init__(f"{operation}: wake failure ({cause_category})")


class ServiceReadinessTimeout(PowerClientError):
    def __init__(self, operation: str, elapsed_seconds: float, last_failure: str | None) -> None:
        self.operation = operation
        self.elapsed_seconds = elapsed_seconds
        self.last_failure = last_failure
        detail = last_failure or "deadline"
        super().__init__(f"{operation}: service readiness timeout ({detail})")


class WorkloadReadinessError(PowerClientError):
    def __init__(self, operation: str, probe_identity: str, elapsed_seconds: float, last_failure: str | None) -> None:
        self.operation = operation
        self.probe_identity = probe_identity
        self.elapsed_seconds = elapsed_seconds
        self.last_failure = last_failure
        detail = last_failure or "failure"
        super().__init__(f"{operation}: workload readiness failed for {probe_identity} ({detail})")


class WorkloadReadinessTimeout(WorkloadReadinessError):
    def __init__(self, operation: str, probe_identity: str, elapsed_seconds: float, last_failure: str | None) -> None:
        super().__init__(operation, probe_identity, elapsed_seconds, last_failure or "deadline")
        self.args = (f"{operation}: workload readiness timeout for {probe_identity} ({self.last_failure})",)
