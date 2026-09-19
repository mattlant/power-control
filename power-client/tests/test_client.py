import asyncio
from copy import deepcopy
import json
import unittest

from power_client.client import PowerServiceClient
from power_client.errors import AuthenticationError, AuthorizationError, ConfigurationError, ConflictError, NotFoundError, ProtocolError, RequestTimeoutError, ServiceUnavailableError, ValidationError
from power_client.models import BearerCredential, LeaseId, ProfileName, TransportResponse, thaw_json
from tests.helpers import lease_collection_payload, lease_payload, profile_application_payload, profile_catalog_payload, status_payload, suspend_receipt_payload


class Credential:
    async def read(self):
        return BearerCredential("id", "secret")


class Transport:
    def __init__(self, response): self.response = response; self.requests = []; self.closed = 0
    async def send(self, request): self.requests.append(request); return self.response
    async def aclose(self): self.closed += 1


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_selects_generic_get_and_closes_owned_transport(self):
        transport = Transport(TransportResponse(200, json.dumps(status_payload()).encode()))
        client = PowerServiceClient(transport, Credential(), owns_transport=True)
        self.assertEqual((await client.get_status()).request_id, "request-1")
        self.assertEqual(transport.requests[0].path, "/v1/status")
        await client.aclose(); await client.aclose()
        self.assertEqual(transport.closed, 1)
        with self.assertRaises(ConfigurationError): await client.get_status()

    async def test_text_status_lease_details_use_list_endpoint_and_preserve_status_on_forbidden(self):
        unblocked = Transport(TransportResponse(200, json.dumps(status_payload()).encode()))
        details = await PowerServiceClient(unblocked, Credential()).get_status_with_leases()
        self.assertIsNone(details.leases)
        self.assertEqual([(request.method.value, request.path) for request in unblocked.requests], [("GET", "/v1/status")])

        blocked = deepcopy(status_payload())
        blocked["lifecycle"]["blockers"] = ["lease"]
        collection = lease_collection_payload({"id": "123e4567-e89b-12d3-a456-426614174000", "expires_at": "2026-09-12T12:00:00Z", "ttl_seconds": 30})

        class SequenceTransport(Transport):
            def __init__(self, responses): super().__init__(None); self.responses = responses
            async def send(self, request):
                self.requests.append(request)
                return self.responses[len(self.requests) - 1]

        transport = SequenceTransport([TransportResponse(200, json.dumps(blocked).encode()), TransportResponse(200, json.dumps(collection).encode())])
        details = await PowerServiceClient(transport, Credential()).get_status_with_leases()
        self.assertEqual(details.leases.leases[0].lease_id.value, "123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual([(request.method.value, request.path) for request in transport.requests], [("GET", "/v1/status"), ("GET", "/v1/leases")])

        forbidden = SequenceTransport([TransportResponse(200, json.dumps(blocked).encode()), TransportResponse(403, b'{"error":{"code":"forbidden","request_id":"r"}}')])
        details = await PowerServiceClient(forbidden, Credential()).get_status_with_leases()
        self.assertIsNone(details.leases)

    async def test_api_errors_are_typed(self):
        transport = Transport(TransportResponse(401, b'{"error":{"code":"unauthenticated","request_id":"r"}}'))
        with self.assertRaises(AuthenticationError):
            await PowerServiceClient(transport, Credential()).get_status()

    async def test_malformed_json_is_a_protocol_error(self):
        from power_client.errors import ProtocolError
        transport = Transport(TransportResponse(200, b"not-json"))
        with self.assertRaises(ProtocolError):
            await PowerServiceClient(transport, Credential()).get_status()

    async def test_client_translates_transport_error_to_public_operation(self):
        class FailingTransport:
            async def send(self, request):
                raise RequestTimeoutError("transport", "https://service.example", "timeout")
            async def aclose(self):
                return None
        with self.assertRaises(RequestTimeoutError) as raised:
            await PowerServiceClient(FailingTransport(), Credential()).get_status()
        self.assertEqual(raised.exception.operation, "get_status")
        self.assertEqual(raised.exception.endpoint, "https://service.example")

    async def test_profile_and_suspend_operations_use_typed_results_and_exact_requests(self):
        responses = [
            TransportResponse(200, json.dumps(profile_catalog_payload()).encode()),
            TransportResponse(200, json.dumps(profile_application_payload()).encode()),
            TransportResponse(202, json.dumps(suspend_receipt_payload()).encode()),
        ]

        class SequenceTransport(Transport):
            async def send(self, request):
                self.requests.append(request)
                return responses[len(self.requests) - 1]

        transport = SequenceTransport(None)
        client = PowerServiceClient(transport, Credential())
        self.assertEqual((await client.list_profiles()).names, ("balanced", "performance"))
        self.assertEqual((await client.apply_profile(ProfileName("balanced"))).name.value, "balanced")
        self.assertEqual((await client.request_suspend()).outcome.value, "accepted")
        self.assertEqual([(request.method.value, request.path, request.body) for request in transport.requests], [
            ("GET", "/v1/profiles", None),
            ("POST", "/v1/profiles/balanced", None),
            ("POST", "/v1/suspend", None),
        ])

    async def test_invalid_profile_name_is_rejected_before_credential_or_transport(self):
        class CountingCredential(Credential):
            def __init__(self): self.reads = 0
            async def read(self):
                self.reads += 1
                return await super().read()

        credential = CountingCredential()
        transport = Transport(TransportResponse(200, b"{}"))
        with self.assertRaises(ConfigurationError):
            await PowerServiceClient(transport, credential).apply_profile(ProfileName("bad/name"))
        self.assertEqual(credential.reads, 0)
        self.assertEqual(transport.requests, [])

    async def test_lease_operations_use_exact_routes_bodies_and_typed_results(self):
        lease_id = LeaseId("123e4567-e89b-12d3-a456-426614174000")
        responses = [
            TransportResponse(201, json.dumps(lease_payload(ttl_seconds=20)).encode()),
            TransportResponse(201, json.dumps(lease_payload(request_id="renew-1", ttl_seconds=10)).encode()),
            TransportResponse(204, b""),
        ]

        class SequenceTransport(Transport):
            async def send(self, request):
                self.requests.append(request)
                return responses[len(self.requests) - 1]

        transport = SequenceTransport(None)
        client = PowerServiceClient(transport, Credential())
        acquired = await client.acquire_lease(20)
        renewed = await client.renew_lease(lease_id, 10)
        released = await client.release_lease(lease_id)
        self.assertEqual(acquired.ttl_seconds, 20)
        self.assertEqual(renewed.request_id, "renew-1")
        self.assertIsNone(released)
        self.assertEqual([(request.method.value, request.path, thaw_json(request.body) if request.body else None) for request in transport.requests], [
            ("POST", "/v1/leases", {"ttl_seconds": 20}),
            ("POST", "/v1/leases/123e4567-e89b-12d3-a456-426614174000/renew", {"ttl_seconds": 10}),
            ("DELETE", "/v1/leases/123e4567-e89b-12d3-a456-426614174000", None),
        ])

    async def test_list_leases_uses_typed_collection_and_get_route(self):
        payload = lease_collection_payload(
            {"id": "123e4567-e89b-12d3-a456-426614174000", "expires_at": "2026-09-12T12:00:00Z", "ttl_seconds": 30},
            {"id": "123e4567-e89b-12d3-a456-426614174001", "expires_at": "2026-09-12T12:01:00Z", "ttl_seconds": 60},
        )
        transport = Transport(TransportResponse(200, json.dumps(payload).encode()))
        leases = await PowerServiceClient(transport, Credential()).list_leases()
        self.assertEqual([lease.ttl_seconds for lease in leases.leases], [30, 60])
        self.assertEqual((transport.requests[0].method.value, transport.requests[0].path, transport.requests[0].body), ("GET", "/v1/leases", None))

    async def test_list_leases_rejects_malformed_collection(self):
        transport = Transport(TransportResponse(200, b'{"request_id":"r","leases":[{}]}'))
        with self.assertRaises(ProtocolError):
            await PowerServiceClient(transport, Credential()).list_leases()

    async def test_lease_local_validation_precedes_credential_and_transport(self):
        class CountingCredential(Credential):
            def __init__(self): self.reads = 0
            async def read(self):
                self.reads += 1
                return await super().read()

        credential = CountingCredential()
        transport = Transport(TransportResponse(201, json.dumps(lease_payload()).encode()))
        client = PowerServiceClient(transport, credential)
        for operation in (client.acquire_lease(0), client.acquire_lease(True), client.renew_lease(object(), 1), client.release_lease(object())):
            with self.assertRaises(ConfigurationError):
                await operation
        self.assertEqual(credential.reads, 0)
        self.assertEqual(transport.requests, [])

    async def test_lease_api_and_protocol_failures_are_typed(self):
        lease_id = LeaseId("123e4567-e89b-12d3-a456-426614174000")
        for status, expected in (
            (401, AuthenticationError),
            (403, AuthorizationError),
            (404, NotFoundError),
            (409, ConflictError),
            (422, ValidationError),
            (503, ServiceUnavailableError),
        ):
            transport = Transport(TransportResponse(status, b'{"error":{"code":"lease_not_found","request_id":"r"}}'))
            with self.assertRaises(expected):
                await PowerServiceClient(transport, Credential()).renew_lease(lease_id, 1)
        transport = Transport(TransportResponse(201, json.dumps({"request_id": "r", "lease": {}}).encode()))
        with self.assertRaises(ProtocolError):
            await PowerServiceClient(transport, Credential()).acquire_lease(1)
        transport = Transport(TransportResponse(204, b"unexpected"))
        with self.assertRaises(ProtocolError):
            await PowerServiceClient(transport, Credential()).release_lease(lease_id)

    async def test_lease_transport_timeout_is_relabelled_without_retry(self):
        class FailingTransport:
            def __init__(self): self.calls = 0
            async def send(self, request):
                self.calls += 1
                raise RequestTimeoutError("transport", "https://service.example", "timeout")
            async def aclose(self):
                return None

        transport = FailingTransport()
        with self.assertRaises(RequestTimeoutError) as raised:
            await PowerServiceClient(transport, Credential()).acquire_lease(1)
        self.assertEqual(raised.exception.operation, "acquire_lease")
        self.assertEqual(transport.calls, 1)

    async def test_lease_cancellation_propagates_without_retry_or_compensation(self):
        class CancelingCredential:
            async def read(self):
                raise asyncio.CancelledError()

        class CountingTransport(Transport):
            async def send(self, request):
                self.requests.append(request)
                raise asyncio.CancelledError()

        transport = CountingTransport(None)
        with self.assertRaises(asyncio.CancelledError):
            await PowerServiceClient(transport, CancelingCredential()).acquire_lease(1)
        self.assertEqual(transport.requests, [])
        with self.assertRaises(asyncio.CancelledError):
            await PowerServiceClient(transport, Credential()).renew_lease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), 1)
        self.assertEqual(len(transport.requests), 1)
