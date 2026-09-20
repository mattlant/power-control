from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import io
from pathlib import Path
import tempfile
import unittest
from ipaddress import IPv4Address

from power_client import (
    ConfigurationError,
    AuthorizationError,
    StatusLeaseDetails,
    LeaseId,
    LeaseCollection,
    ListedLease,
    ProfileCatalog,
    ProfileName,
    ProfileReconciliation,
    ReconciliationState,
    ServiceConnectionConfig,
    TrustConfig,
    WakeRequest,
    WakeTarget,
    ServiceWaitPolicy,
    ReadinessWaitPolicy,
    TcpReadinessTarget,
    HttpReadinessTarget,
    WakeResult,
    WakeError,
    ServiceReadinessTimeout,
    parse_service_status,
)
from tests.helpers import status_payload
from power_client.cli import (
    build_parser,
    load_cli_configuration,
    render_success,
    render_text,
    resolve_config_path,
    run_command,
    _write_error,
    _format_power,
)


@dataclass(frozen=True)
class Result:
    amount: Decimal
    observed_at: datetime


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True

    async def get_status(self):
        self.calls.append(("get_status", ()))
        return Result(Decimal("1.25"), datetime(2026, 1, 1, tzinfo=timezone.utc))

    async def get_status_with_leases(self):
        self.calls.append(("get_status_with_leases", ()))
        return Result(Decimal("1.25"), datetime(2026, 1, 1, tzinfo=timezone.utc))

    async def list_profiles(self):
        self.calls.append(("list_profiles", ()))
        return {"names": ("balanced",)}

    async def apply_profile(self, name):
        self.calls.append(("apply_profile", (name,)))
        return {"name": name.value}

    async def request_suspend(self):
        self.calls.append(("request_suspend", ()))
        return {"accepted": True}

    async def acquire_lease(self, ttl):
        self.calls.append(("acquire_lease", (ttl,)))
        return {"ttl": ttl}

    async def renew_lease(self, lease_id, ttl):
        self.calls.append(("renew_lease", (lease_id, ttl)))
        return {"lease_id": lease_id.value, "ttl": ttl}

    async def release_lease(self, lease_id):
        self.calls.append(("release_lease", (lease_id,)))

    async def list_leases(self):
        self.calls.append(("list_leases", ()))
        return LeaseCollection((), "leases-1")


class RecordingOrchestrator:
    def __init__(self):
        self.request = None
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True

    async def wake(self, request):
        self.request = request
        return WakeResult(object(), (), 2.0)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configuration = type("Configuration", (), {
            "connection": ServiceConnectionConfig("https://host:9443", 5.0, TrustConfig()),
            "credential_file": object(),
        })()

    def test_config_precedence_and_relative_paths(self):
        self.assertEqual(resolve_config_path("explicit.toml", {"POWERCTL_CONFIG": "env.toml"}), Path.cwd() / "explicit.toml")
        self.assertEqual(resolve_config_path(None, {"POWERCTL_CONFIG": "env.toml"}), Path.cwd() / "env.toml")
        with self.assertRaises(ConfigurationError):
            resolve_config_path(None, {"XDG_CONFIG_HOME": "relative"})

    def test_loader_rejects_unknown_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[service]\nendpoint='https://host:9443'\nrequest_timeout=5\ncredential_file='/tmp/credential'\nunknown=true\n", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown key\\(s\\): unknown"):
                load_cli_configuration(path)

    def test_loader_reports_distinct_file_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ConfigurationError, "file does not exist"):
                load_cli_configuration(root / "missing.toml")
            subdirectory = root / "config-directory"
            subdirectory.mkdir()
            with self.assertRaisesRegex(ConfigurationError, "not a regular file"):
                load_cli_configuration(subdirectory)

    def test_loader_reports_distinct_schema_and_value_failures(self):
        cases = [
            ("title = 'wrong root'\n", "unknown root key\\(s\\): title"),
            ("service = 'wrong type'\n", "must be a TOML table"),
            ("", "missing \\[service\\] table"),
            ("[service]\n", "service.endpoint: is required"),
            ("[service]\nendpoint = 3\nrequest_timeout = 5\ncredential_file = '/tmp/c'\n", "service.endpoint: must be a string"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for content, message in cases:
                path = Path(directory) / "config.toml"
                path.write_text(content, encoding="utf-8")
                with self.subTest(message=message):
                    with self.assertRaisesRegex(ConfigurationError, message):
                        load_cli_configuration(path)

    def test_loader_reports_malformed_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("[service\nendpoint = 'https://host:9443'", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "malformed TOML"):
                    load_cli_configuration(path)

    def test_loader_builds_strict_wake_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[service]\nendpoint='https://host:9443'\nrequest_timeout=5\ncredential_file='/tmp/c'\n"
                "[wake]\nmac_address='AA:BB:CC:DD:EE:FF'\nbroadcast_address='192.0.2.255'\n"
                "udp_port=9\nservice_wait_timeout=30.0\nservice_wait_poll_interval=2.0\n",
                encoding="utf-8",
            )
            configuration = load_cli_configuration(path)
            self.assertEqual(configuration.wake_request.target.mac_address, "aa:bb:cc:dd:ee:ff")
            self.assertEqual(configuration.wake_request.target.broadcast_address, IPv4Address("192.0.2.255"))
            self.assertEqual(configuration.wake_request.service_wait_policy.poll_interval_seconds, 2.0)

    def test_loader_builds_ordered_generic_probe_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                "[service]\nendpoint='https://host:9443'\nrequest_timeout=5\ncredential_file='/tmp/c'\n"
                "[wake]\nmac_address='AA:BB:CC:DD:EE:FF'\nbroadcast_address='192.0.2.255'\n"
                "udp_port=9\nservice_wait_timeout=30.0\nservice_wait_poll_interval=2.0\n"
                "readiness_wait_timeout=10.0\nreadiness_attempt_timeout=3.0\nreadiness_wait_poll_interval=2.0\n"
                "[[wake.probe]]\nidentity='http'\ntype='http'\nurl='https://workload.example.test/health'\nexpected_statuses=[200]\n"
                "[[wake.probe]]\nidentity='tcp'\ntype='tcp'\nhost='workload.example.test'\nport=8000\n",
                encoding="utf-8",
            )
            request = load_cli_configuration(path).wake_request
            self.assertEqual([probe.identity for probe in request.readiness_probes], ["http", "tcp"])
            self.assertEqual(request.readiness_wait_policy.attempt_timeout_seconds, 3.0)

    def test_loader_rejects_invalid_wake_schema_and_values(self):
        values = [
            ("unknown = true", "unknown key"),
            ("mac_address = 'not-mac'", "mac_address"),
            ("broadcast_address = 'not-ip'", "broadcast_address"),
            ("udp_port = true", "udp_port"),
            ("service_wait_poll_interval = 99", "poll_interval"),
        ]
        base = "[service]\nendpoint='https://host:9443'\nrequest_timeout=5\ncredential_file='/tmp/c'\n[wake]\nmac_address='AA:BB:CC:DD:EE:FF'\nbroadcast_address='192.0.2.255'\nudp_port=9\nservice_wait_timeout=30.0\nservice_wait_poll_interval=2.0\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for replacement, expected in values:
                content = base + replacement + "\n"
                path.write_text(content, encoding="utf-8")
                with self.subTest(expected=expected), self.assertRaises(ConfigurationError):
                    load_cli_configuration(path)

    def test_wake_delegates_once_and_does_not_construct_client(self):
        orchestrator = RecordingOrchestrator()
        configuration = type("Configuration", (), {
            "connection": self.configuration.connection,
            "credential_file": self.configuration.credential_file,
            "wake_request": WakeRequest(WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 9), ServiceWaitPolicy(30, 2)),
        })()
        parsed = build_parser().parse_args(["wake"])
        result = asyncio.run(run_command(parsed, configuration, client_factory=self.fail_factory, orchestrator_factory=lambda *_: orchestrator))
        self.assertIsInstance(result, WakeResult)
        self.assertIs(orchestrator.request, configuration.wake_request)
        self.assertTrue(orchestrator.closed)

    def _wake_configuration(self, *, with_policy=False):
        policy = ReadinessWaitPolicy(10, 3, 2) if with_policy else None
        configured_probes = (TcpReadinessTarget("configured", "host", 17340),) if with_policy else ()
        request = WakeRequest(
            WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 9),
            ServiceWaitPolicy(30, 2),
            configured_probes,
            policy,
        )
        return type("Configuration", (), {
            "connection": self.configuration.connection,
            "credential_file": self.configuration.credential_file,
            "wake_request": request,
        })()

    def test_ad_hoc_wake_targets_use_existing_models_and_delegate(self):
        cases = [
            (["wake", "tcp://host:17340"], TcpReadinessTarget, None),
            (["wake", "https://host/path"], HttpReadinessTarget, (200,)),
            (["wake", "https://host/path", "204"], HttpReadinessTarget, (204,)),
            (["wake", "http://host/path"], HttpReadinessTarget, (200,)),
        ]
        for argv, target_type, statuses in cases:
            with self.subTest(argv=argv):
                orchestrator = RecordingOrchestrator()
                configuration = self._wake_configuration()
                parsed = build_parser().parse_args(argv)
                asyncio.run(run_command(parsed, configuration, orchestrator_factory=lambda *_: orchestrator))
                request = orchestrator.request
                self.assertIsInstance(request.readiness_probes[0], target_type)
                if statuses is not None:
                    self.assertEqual(request.readiness_probes[0].expected_statuses, statuses)
                self.assertEqual(request.readiness_wait_policy, ReadinessWaitPolicy(120, 5, 2))

    def test_ad_hoc_target_reuses_configured_readiness_policy_and_replaces_probes(self):
        orchestrator = RecordingOrchestrator()
        configuration = self._wake_configuration(with_policy=True)
        parsed = build_parser().parse_args(["wake", "tcp://host:17340"])
        asyncio.run(run_command(parsed, configuration, orchestrator_factory=lambda *_: orchestrator))
        self.assertEqual(orchestrator.request.readiness_wait_policy, ReadinessWaitPolicy(10, 3, 2))
        self.assertEqual(orchestrator.request.readiness_probes[0].identity, "adhoc-tcp")

    def test_ad_hoc_invalid_arguments_fail_before_orchestrator_creation(self):
        configuration = self._wake_configuration()
        invalid = [
            ["wake", "tcp://host:17340", "200"],
            ["wake", "ftp://host/path"],
            ["wake", "tcp://host"],
            ["wake", "https://user@host/path"],
            ["wake", "https://host/path", "not-an-integer"],
            ["wake", "https://host/path", "99"],
            ["wake", "https://host/path", "600"],
        ]
        for argv in invalid:
            with self.subTest(argv=argv):
                parsed = build_parser().parse_args(argv)
                with self.assertRaises(ConfigurationError):
                    asyncio.run(run_command(parsed, configuration, orchestrator_factory=self.fail_factory))

    def test_ad_hoc_extra_positional_argument_is_rejected_by_parser(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["wake", "https://host/path", "200", "extra"])

    def test_wake_without_configuration_constructs_nothing(self):
        parsed = build_parser().parse_args(["wake"])
        with self.assertRaises(ConfigurationError):
            asyncio.run(run_command(parsed, self.configuration, client_factory=self.fail_factory, orchestrator_factory=self.fail_factory))

    def test_wake_text_and_error_output_are_redacted(self):
        parsed = build_parser().parse_args(["wake"])
        output = io.StringIO()
        render_text(parsed, WakeResult(type("Status", (), {"service_state": type("State", (), {"value": "ready"})()})(), (), 2.0), output)
        self.assertEqual(output.getvalue(), "Wake complete\nService:    ready\nElapsed:    2s\n")
        stderr = io.StringIO()
        target = WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 9)
        self.assertEqual(_write_error(WakeError("wake", target, "udp_send"), stderr), 4)
        self.assertNotIn(target.mac_address, stderr.getvalue())
        stderr = io.StringIO()
        self.assertEqual(_write_error(ServiceReadinessTimeout("wait_for_service", 30, "connection"), stderr), 4)
        self.assertNotIn("192.0.2.255", stderr.getvalue())

    def test_all_commands_delegate_once_with_typed_arguments(self):
        cases = [
            (["status"], "get_status_with_leases"),
            (["profile", "list"], "list_profiles"),
            (["profile", "apply", "balanced"], "apply_profile"),
            (["suspend"], "request_suspend"),
            (["lease", "acquire", "30"], "acquire_lease"),
            (["lease", "list"], "list_leases"),
            (["lease", "renew", "123e4567-e89b-12d3-a456-426614174000", "30"], "renew_lease"),
            (["lease", "release", "123e4567-e89b-12d3-a456-426614174000"], "release_lease"),
        ]
        for argv, operation in cases:
            client = RecordingClient()
            parsed = build_parser().parse_args(argv)
            result = asyncio.run(run_command(parsed, self.configuration, client_factory=lambda *_: client))
            self.assertEqual(client.calls[0][0], operation)
            self.assertTrue(client.closed)
            if operation == "apply_profile":
                self.assertIsInstance(client.calls[0][1][0], ProfileName)
            if operation in {"renew_lease", "release_lease"}:
                self.assertIsInstance(client.calls[0][1][0], LeaseId)
            if operation == "release_lease":
                self.assertEqual(result["released"], True)

    def test_render_success_is_deterministic_json(self):
        output = io.StringIO()
        render_success(Result(Decimal("1.25"), datetime(2026, 1, 1, tzinfo=timezone.utc)), output)
        self.assertEqual(output.getvalue(), '{"amount":"1.25","observed_at":"2026-01-01T00:00:00Z"}\n')

    def test_power_format_preserves_integer_watt_values(self):
        self.assertEqual(_format_power(Decimal("100")), "100")
        self.assertEqual(_format_power(Decimal("350")), "350")
        self.assertEqual(_format_power(Decimal("100.50")), "100.5")

    def test_output_mode_defaults_to_human_text_and_json_is_explicit(self):
        self.assertEqual(build_parser().parse_args(["status"]).output, "text")
        self.assertEqual(build_parser().parse_args(["--output", "json", "status"]).output, "json")

    def test_text_profile_list_is_compact_and_marks_reconciled_profile(self):
        parsed = build_parser().parse_args(["profile", "list"])
        output = io.StringIO()
        render_text(parsed, ProfileCatalog(("balanced", "performance"), ProfileReconciliation(ReconciliationState.MATCHED, "balanced"), "request-id"), output)
        self.assertEqual(output.getvalue(), "Profiles:\n* balanced\n  performance\n")

    def test_text_lease_list_renders_multiple_single_and_empty_collections(self):
        parsed = build_parser().parse_args(["lease", "list"])
        one = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 30)
        second = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174001"), datetime(2026, 1, 1, 2, tzinfo=timezone.utc), 60)
        output = io.StringIO()
        render_text(parsed, LeaseCollection((one, second), "r"), output)
        self.assertEqual(output.getvalue(), "ID                                    Acquired  Duration  Expires\n123e4567-e89b-12d3-a456-426614174000  —         30s       2026-01-01 01:00 UTC\n123e4567-e89b-12d3-a456-426614174001  —         1m        2026-01-01 02:00 UTC\n")
        output = io.StringIO()
        render_text(parsed, LeaseCollection((), "r"), output)
        self.assertEqual(output.getvalue(), "No active leases.\n")

    def test_json_lease_list_is_structured(self):
        parsed = build_parser().parse_args(["--output", "json", "lease", "list"])
        lease = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), datetime(2026, 1, 1, tzinfo=timezone.utc), 30)
        output = io.StringIO()
        render_success(LeaseCollection((lease,), "r"), output)
        self.assertEqual(output.getvalue(), '{"leases":[{"expires_at":"2026-01-01T00:00:00Z","lease_id":{"value":"123e4567-e89b-12d3-a456-426614174000"},"ttl_seconds":30}],"request_id":"r"}\n')

    def test_text_status_summarizes_operator_fields_without_request_id(self):
        parsed = build_parser().parse_args(["status"])
        output = io.StringIO()
        render_text(parsed, parse_service_status(status_payload()), output, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        rendered = output.getvalue()
        self.assertIn("Service:    ready", rendered)
        self.assertIn("CPU:        available", rendered)
        self.assertIn("current range: 0.001–0.002 MHz", rendered)
        self.assertIn("capable range: 0.001–0.002 MHz", rendered)
        self.assertIn("GPU:        available", rendered)
        self.assertIn("Test GPU (test)", rendered)
        self.assertIn("Suspend:    available", rendered)
        self.assertIn("Suspend:    waiting for inactivity", rendered)
        self.assertNotIn("request-1", rendered)

    def test_lease_blocked_status_shows_grace_after_latest_lease_when_it_outlasts_stable_idle(self):
        payload = status_payload()
        payload["lifecycle"].update({"blockers": ["lease"], "next_transition_at": "2026-01-01T00:01:00Z"})
        leases = LeaseCollection((
            ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), datetime(2026, 1, 1, 2, tzinfo=timezone.utc), 30),
        ), "leases-1")
        output = io.StringIO()
        render_text(build_parser().parse_args(["status"]), StatusLeaseDetails(parse_service_status(payload), leases), output,
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        rendered = output.getvalue()
        self.assertIn("Suspend:    blocked by lease", rendered)
        self.assertIn("Leases:     123e4567-e89b-12d3-a456-426614174000 (expires in 2h)", rendered)
        self.assertIn("Suspend in: grace starts after last lease expires in 2h", rendered)

    def test_lease_blocked_status_shows_grace_after_expiry_when_stable_idle_elapsed(self):
        payload = status_payload()
        payload["lifecycle"].update({"blockers": ["lease"], "next_transition_at": "2025-12-31T23:59:59Z"})
        lease = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), datetime(2026, 1, 1, 2, tzinfo=timezone.utc), 30)
        output = io.StringIO()
        render_text(build_parser().parse_args(["status"]), StatusLeaseDetails(parse_service_status(payload), LeaseCollection((lease,), "r")), output,
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertIn("Suspend in: grace starts after last lease expires in 2h", output.getvalue())

    def test_lease_blocked_status_lists_all_active_leases_and_uses_latest_expiry(self):
        payload = status_payload()
        payload["lifecycle"].update({"blockers": ["lease"], "next_transition_at": "2026-01-01T00:01:00Z"})
        active = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), datetime(2026, 1, 1, 1, tzinfo=timezone.utc), 30)
        latest = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174001"), datetime(2026, 1, 1, 2, tzinfo=timezone.utc), 30)
        output = io.StringIO()
        render_text(build_parser().parse_args(["status"]), StatusLeaseDetails(parse_service_status(payload), LeaseCollection((active, latest), "r")), output,
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        rendered = output.getvalue()
        self.assertIn(active.lease_id.value, rendered)
        self.assertIn(latest.lease_id.value, rendered)
        self.assertIn("expires in 2h", rendered)
        self.assertIn("Suspend in: grace starts after last lease expires in 2h", rendered)

    def test_lease_blocked_status_keeps_stable_idle_timing_when_leases_expire_first(self):
        payload = status_payload()
        payload["lifecycle"].update({"blockers": ["lease"], "next_transition_at": "2026-01-01T00:50:00Z"})
        lease = ListedLease(LeaseId("123e4567-e89b-12d3-a456-426614174000"), datetime(2026, 1, 1, 0, 40, tzinfo=timezone.utc), 30)
        output = io.StringIO()
        render_text(build_parser().parse_args(["status"]), StatusLeaseDetails(parse_service_status(payload), LeaseCollection((lease,), "r")), output,
                    now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        rendered = output.getvalue()
        self.assertIn("Leases:     123e4567-e89b-12d3-a456-426614174000 (expires in 40m)", rendered)
        self.assertIn("Suspend in: 50m", rendered)
        self.assertNotIn("after lease expiry", rendered)

    def test_text_status_delegates_to_list_leases_and_keeps_status_on_lease_authorization_failure(self):
        payload = status_payload()
        payload["lifecycle"].update({"blockers": ["lease"], "next_transition_at": "2026-01-01T00:01:00Z"})
        status = parse_service_status(payload)

        class LeaseBlockedClient(RecordingClient):
            async def get_status_with_leases(self):
                self.calls.append(("get_status_with_leases", ()))
                return StatusLeaseDetails(status, None)

        client = LeaseBlockedClient()
        result = asyncio.run(run_command(build_parser().parse_args(["status"]), self.configuration, client_factory=lambda *_: client))
        self.assertIsInstance(result, StatusLeaseDetails)
        self.assertIsNone(result.leases)
        self.assertEqual([call[0] for call in client.calls], ["get_status_with_leases"])
        output = io.StringIO()
        render_text(build_parser().parse_args(["status"]), result, output, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(output.getvalue().endswith("Suspend:    blocked by lease\n"))

    def test_invalid_typed_argument_does_not_construct_client(self):
        parsed = build_parser().parse_args(["lease", "release", "not-a-uuid"])
        with self.assertRaises(ConfigurationError):
            asyncio.run(run_command(parsed, self.configuration, client_factory=self.fail_factory))

    @staticmethod
    def fail_factory(*args):
        raise AssertionError("client must not be constructed")
