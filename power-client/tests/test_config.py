from pathlib import Path
from ipaddress import IPv4Address
import unittest

from power_client.config import (
    CredentialFileReference,
    HttpReadinessTarget,
    ReadinessWaitPolicy,
    ServiceConnectionConfig,
    ServiceWaitPolicy,
    TcpReadinessTarget,
    TrustConfig,
    WakeTarget,
)
from power_client.errors import ConfigurationError


class ConfigurationTests(unittest.TestCase):
    def test_origin_only_endpoint_and_positive_timeout(self):
        ServiceConnectionConfig("https://service.example:9443/", 1, TrustConfig())
        for endpoint in ("http://service", "https://service/v1", "https://user@service", "https://service?q=x"):
            with self.assertRaises(ConfigurationError):
                ServiceConnectionConfig(endpoint, 1, TrustConfig())
        with self.assertRaises(ConfigurationError):
            ServiceConnectionConfig("https://service", 0, TrustConfig())

    def test_paths_must_be_absolute(self):
        with self.assertRaises(ConfigurationError):
            CredentialFileReference(Path("credential"))
        with self.assertRaises(ConfigurationError):
            TrustConfig(Path("ca.pem"))

    def test_wake_target_normalizes_eui48_and_rejects_invalid_values(self):
        target = WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 9)
        self.assertEqual(target.mac_address, "aa:bb:cc:dd:ee:ff")
        self.assertEqual(target.mac_bytes, bytes.fromhex("aabbccddeeff"))
        invalid = [
            ("aabbccddeeff", IPv4Address("192.0.2.255"), 9),
            ("AA:BB:CC:DD:EE:FG", IPv4Address("192.0.2.255"), 9),
            ("AA:BB:CC:DD:EE:FF", "192.0.2.255", 9),
            ("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 0),
            ("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 65536),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                WakeTarget(*values)

    def test_service_wait_policy_is_finite_positive_and_bounded(self):
        ServiceWaitPolicy(5.0, 1.0)
        for values in ((0, 1), (5, 0), (float("inf"), 1), (5, 6), (True, 1)):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                ServiceWaitPolicy(*values)

    def test_readiness_policy_requires_two_bounded_attempts(self):
        ReadinessWaitPolicy(5, 2, 1)
        for values in ((0, 2, 1), (5, 2, 2), (4, 2, 1), (5, True, 1), (5, 2, float("inf"))):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                ReadinessWaitPolicy(*values)

    def test_generic_targets_validate_without_network_access(self):
        TcpReadinessTarget("tcp", "localhost", 8000)
        HttpReadinessTarget("http", "https://localhost:8443/health", (200, 204))
        invalid = (
            lambda: TcpReadinessTarget("", "localhost", 8000),
            lambda: TcpReadinessTarget("tcp", "bad host", 8000),
            lambda: TcpReadinessTarget("tcp", "localhost", 0),
            lambda: HttpReadinessTarget("http", "https://user@localhost/health", (200,)),
            lambda: HttpReadinessTarget("http", "https://localhost/health?q=secret", (200,)),
            lambda: HttpReadinessTarget("http", "https://localhost/health", ()),
        )
        for constructor in invalid:
            with self.assertRaises(ConfigurationError):
                constructor()
