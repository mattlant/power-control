import math
import unittest
from datetime import timezone

from ipaddress import IPv4Address

from power_client.config import ReadinessWaitPolicy, ServiceWaitPolicy, TcpReadinessTarget, WakeTarget
from power_client.errors import ConfigurationError, ProtocolError
from power_client.models import (
    Availability,
    BearerCredential,
    Component,
    HttpMethod,
    LeaseId,
    ProfileName,
    TransportRequest,
    parse_profile_application,
    parse_profile_catalog,
    parse_service_status,
    parse_lease,
    WakeRequest,
)
from tests.helpers import lease_payload, profile_application_payload, profile_catalog_payload, status_payload


class ModelTests(unittest.TestCase):
    def test_wake_request_preserves_no_probe_and_ordered_probe_inputs(self):
        target = WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 9)
        first = TcpReadinessTarget("first", "localhost", 1)
        second = TcpReadinessTarget("second", "localhost", 2)
        request = WakeRequest(target, ServiceWaitPolicy(5, 1), (first, second), ReadinessWaitPolicy(5, 2, 1))
        self.assertEqual(request.readiness_probes, (first, second))
        with self.assertRaises(ConfigurationError):
            WakeRequest(target, ServiceWaitPolicy(5, 1), (first,), None)
        with self.assertRaises(ConfigurationError):
            WakeRequest(target, ServiceWaitPolicy(5, 1), (first, first), ReadinessWaitPolicy(5, 2, 1))

    def test_maps_full_status_and_ignores_additive_members(self):
        payload = status_payload()
        payload["new_field"] = "future"
        status = parse_service_status(payload)
        self.assertEqual(status.request_id, "request-1")
        self.assertEqual(status.cpu.value.policies[0].policy, 0)

    def test_transport_request_is_snapshot_and_secret_safe(self):
        body = {"nested": [{"value": 1}]}
        request = TransportRequest(HttpMethod.POST, "/v1/example", body, BearerCredential("id", "secret"))
        body["nested"][0]["value"] = 2
        self.assertNotIn("secret", repr(request))
        self.assertEqual(request.body.items[0][1].items[0].items[0][1], 1)

    def test_request_rejects_nonfinite_values(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ConfigurationError):
                TransportRequest(HttpMethod.POST, "/v1/example", {"nested": [value]}, BearerCredential("id", "secret"))

    def test_request_rejects_non_object_post_body_with_configuration_error(self):
        with self.assertRaises(ConfigurationError):
            TransportRequest(HttpMethod.POST, "/v1/example", [], BearerCredential("id", "secret"))

    def test_invalid_status_is_protocol_error(self):
        payload = status_payload()
        payload["service"] = {"state": "unknown"}
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)

    def test_component_enforces_resolved_state_invariants(self):
        with self.assertRaises(ValueError):
            Component(Availability.AVAILABLE)
        with self.assertRaises(ValueError):
            Component(Availability.UNAVAILABLE, error_code=None)
        with self.assertRaises(ValueError):
            Component(Availability.AVAILABLE, value="payload", error_code="unexpected")

    def test_present_optional_values_have_strict_types(self):
        payload = status_payload()
        payload["cpu"]["policies"][0]["epp"] = 1
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)
        payload = status_payload()
        payload["profiles"] = {"state": "available", "names": [], "reconciliation": {"state": "unmatched", "name": 1}}
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)

    def test_cpu_epp_requires_matching_nonempty_available_epps(self):
        payload = status_payload()
        policy = payload["cpu"]["policies"][0]
        policy["epp"] = "performance"
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)
        payload = status_payload()
        policy = payload["cpu"]["policies"][0]
        policy["epp"] = "performance"
        policy["available_epps"] = ["balance_performance"]
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)
        payload = status_payload()
        policy = payload["cpu"]["policies"][0]
        policy["epp"] = "performance"
        policy["available_epps"] = ["performance"]
        self.assertEqual(parse_service_status(payload).cpu.value.policies[0].epp, "performance")

    def test_component_payload_contradictions_are_protocol_errors(self):
        payload = status_payload()
        payload["cpu"]["error"] = {"code": "unexpected"}
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)
        payload = status_payload()
        payload["gpu"] = {"state": "unavailable", "error": {"code": "missing"}, "index": 0}
        payload["service"] = {"state": "degraded"}
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)

    def test_lifecycle_timestamps_must_be_chronological(self):
        payload = status_payload()
        lifecycle = payload["lifecycle"]
        lifecycle["idle_started_at"] = "2026-09-10T00:00:02Z"
        lifecycle["grace_started_at"] = "2026-09-10T00:00:01Z"
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)

    def test_profile_catalog_accepts_discovery_unavailable_without_inventing_error(self):
        catalog = parse_profile_catalog(profile_catalog_payload(state="unavailable"))
        self.assertEqual(catalog.names, ("balanced", "performance"))
        self.assertEqual(catalog.reconciliation.error_code, None)

    def test_profile_application_requires_matching_requested_name(self):
        payload = profile_application_payload()
        payload["application"]["profile"] = "performance"
        with self.assertRaises(ProtocolError) as raised:
            parse_profile_application(payload, requested_name=ProfileName("balanced"))
        self.assertEqual(raised.exception.operation, "apply_profile")

    def test_profile_name_uses_exact_server_grammar(self):
        for value in ("", "bad/name", "bad?name", "é", "x" * 65, True):
            with self.assertRaises(ConfigurationError):
                ProfileName(value)
        payload = status_payload()
        lifecycle = payload["lifecycle"]
        lifecycle["idle_started_at"] = "2026-09-10T00:00:02Z"
        lifecycle["next_transition_at"] = "2026-09-10T00:00:01Z"
        with self.assertRaises(ProtocolError):
            parse_service_status(payload)
        payload = status_payload()
        lifecycle = payload["lifecycle"]
        lifecycle["idle_started_at"] = "2026-09-10T00:00:01Z"
        lifecycle["next_transition_at"] = "2026-09-10T00:00:02Z"
        self.assertIsNotNone(parse_service_status(payload).lifecycle.next_transition_at)

    def test_lease_id_requires_lowercase_canonical_uuid(self):
        self.assertEqual(LeaseId("123e4567-e89b-12d3-a456-426614174000").value, "123e4567-e89b-12d3-a456-426614174000")
        for value in ("123E4567-E89B-12D3-A456-426614174000", "123e4567e89b12d3a456426614174000", "bad", True):
            with self.assertRaises(ConfigurationError):
                LeaseId(value)

    def test_parse_lease_maps_effective_ttl_and_ignores_additive_fields(self):
        lease = parse_lease(lease_payload(ttl_seconds=7), operation="acquire_lease")
        self.assertEqual(lease.lease_id.value, "123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(lease.ttl_seconds, 7)
        self.assertEqual(lease.expires_at.tzinfo, timezone.utc)
        with self.assertRaises((AttributeError, TypeError)):
            lease.ttl_seconds = 8

    def test_parse_lease_rejects_malformed_required_fields_with_operation_and_request(self):
        cases = [
            ("request_id", None),
            ("lease", None),
            ("id", "bad"),
            ("expires_at", "2026-09-12T12:00:00+00:00"),
            ("ttl_seconds", True),
            ("ttl_seconds", 0),
        ]
        for field, value in cases:
            payload = lease_payload()
            if field in {"request_id", "lease"}:
                payload[field] = value
            else:
                payload["lease"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ProtocolError) as raised:
                parse_lease(payload, operation="renew_lease")
            self.assertEqual(raised.exception.operation, "renew_lease")
            self.assertEqual(raised.exception.request_id, None if field == "request_id" else "lease-1")
