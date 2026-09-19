from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from .errors import ApiError, AuthenticationError, AuthorizationError, ConfigurationError, ConflictError, NotFoundError, ProtocolError, ServiceUnavailableError, TransportError, ValidationError
from .models import (
    HttpMethod,
    ProfileApplication,
    ProfileCatalog,
    JsonInputValue,
    Lease,
    LeaseCollection,
    StatusLeaseDetails,
    LeaseId,
    ProfileName,
    ServiceStatus,
    SuspendReceipt,
    TransportRequest,
    parse_profile_application,
    parse_profile_catalog,
    parse_service_status,
    parse_suspend_receipt,
    parse_lease,
    parse_lease_collection,
)
from .ports import CredentialSource, HttpTransport


_LEASES_PATH = "/v1/leases"


class PowerServiceClient:
    def __init__(self, transport: HttpTransport, credential_source: CredentialSource, *, owns_transport: bool = False) -> None:
        self._transport = transport
        self._credential_source = credential_source
        self._owns_transport = owns_transport
        self._closed = False

    async def get_status(self) -> ServiceStatus:
        return await self._execute("get_status", HttpMethod.GET, "/v1/status", 200, parse_service_status)

    async def get_status_with_leases(self) -> StatusLeaseDetails:
        status = await self.get_status()
        if "lease" not in status.lifecycle.blockers:
            return StatusLeaseDetails(status, None)
        try:
            return StatusLeaseDetails(status, await self.list_leases())
        except AuthorizationError:
            return StatusLeaseDetails(status, None)

    async def list_profiles(self) -> ProfileCatalog:
        return await self._execute("list_profiles", HttpMethod.GET, "/v1/profiles", 200, parse_profile_catalog)

    async def apply_profile(self, name: ProfileName) -> ProfileApplication:
        if not isinstance(name, ProfileName):
            raise ConfigurationError("apply_profile", "name", "must be a ProfileName")
        return await self._execute(
            "apply_profile",
            HttpMethod.POST,
            f"/v1/profiles/{name.value}",
            200,
            lambda data: parse_profile_application(data, requested_name=name),
        )

    async def request_suspend(self) -> SuspendReceipt:
        return await self._execute("request_suspend", HttpMethod.POST, "/v1/suspend", 202, parse_suspend_receipt)

    async def acquire_lease(self, ttl_seconds: int) -> Lease:
        ttl = self._lease_ttl(ttl_seconds, "acquire_lease")
        return await self._execute(
            "acquire_lease",
            HttpMethod.POST,
            _LEASES_PATH,
            201,
            lambda data: parse_lease(data, operation="acquire_lease"),
            body={"ttl_seconds": ttl},
        )

    async def list_leases(self) -> LeaseCollection:
        return await self._execute("list_leases", HttpMethod.GET, _LEASES_PATH, 200, parse_lease_collection)

    async def renew_lease(self, lease_id: LeaseId, ttl_seconds: int) -> Lease:
        self._require_lease_id(lease_id, "renew_lease")
        ttl = self._lease_ttl(ttl_seconds, "renew_lease")
        return await self._execute(
            "renew_lease",
            HttpMethod.POST,
            f"/v1/leases/{lease_id.value}/renew",
            201,
            lambda data: parse_lease(data, operation="renew_lease"),
            body={"ttl_seconds": ttl},
        )

    async def release_lease(self, lease_id: LeaseId) -> None:
        self._require_lease_id(lease_id, "release_lease")
        await self._execute_empty("release_lease", f"/v1/leases/{lease_id.value}")

    @staticmethod
    def _lease_ttl(ttl_seconds: object, operation: str) -> int:
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 2147483647:
            raise ConfigurationError(operation, "ttl_seconds", "must be an integer from 1 to 2147483647")
        return ttl_seconds

    @staticmethod
    def _require_lease_id(lease_id: object, operation: str) -> None:
        if type(lease_id) is not LeaseId:
            raise ConfigurationError(operation, "lease_id", "must be a LeaseId")

    async def _execute(
        self,
        operation: str,
        method: HttpMethod,
        path: str,
        success_status: int,
        parser: Callable[[object], object],
        *,
        body: Mapping[str, JsonInputValue] | None = None,
    ) -> object:
        if self._closed:
            raise ConfigurationError(operation, "client", "client is closed")
        credential = await self._credential_source.read()
        try:
            response = await self._transport.send(TransportRequest(method, path, body, credential))
        except TransportError as error:
            raise type(error)(operation, error.endpoint, error.cause_category) from error
        decoded = self._decode_response(response.body, operation)
        if response.status == success_status:
            return parser(decoded)
        self._raise_api_error(operation, response.status, decoded)

    async def _execute_empty(self, operation: str, path: str) -> None:
        if self._closed:
            raise ConfigurationError(operation, "client", "client is closed")
        credential = await self._credential_source.read()
        try:
            response = await self._transport.send(TransportRequest(HttpMethod.DELETE, path, None, credential))
        except TransportError as error:
            raise type(error)(operation, error.endpoint, error.cause_category) from error
        if response.status == 204:
            if response.body:
                raise ProtocolError(operation, None, "successful empty response must have no body")
            return
        self._raise_api_error(operation, response.status, self._decode_response(response.body, operation))

    @staticmethod
    def _decode_response(body: bytes, operation: str) -> dict[str, object]:
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProtocolError(operation, None, "response is not valid UTF-8 JSON") from error
        if not isinstance(decoded, dict):
            raise ProtocolError(operation, None, "response root must be an object")
        return decoded

    def _raise_api_error(self, operation: str, status: int, data: dict[str, object]) -> None:
        try:
            error = data["error"]
            if not isinstance(error, dict):
                raise TypeError()
            code = error["code"]
            request_id = error["request_id"]
            if not isinstance(code, str) or not isinstance(request_id, str):
                raise TypeError()
        except (KeyError, TypeError) as error:
            raise ProtocolError(operation, None, "error envelope is malformed") from error
        classes = {401: AuthenticationError, 403: AuthorizationError, 404: NotFoundError, 409: ConflictError, 422: ValidationError, 503: ServiceUnavailableError}
        raise classes.get(status, ApiError)(operation, status, request_id, code)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_transport:
            await self._transport.aclose()

    async def __aenter__(self) -> "PowerServiceClient":
        if self._closed:
            raise ConfigurationError("client", "state", "is closed")
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()
