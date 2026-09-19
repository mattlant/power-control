import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from power_client import PowerManagement
from power_client.composition import _ComposedPowerManagement, compose_power_management
from power_client.config import CredentialFileReference, ServiceConnectionConfig, TrustConfig
from power_client.errors import ConfigurationError
from power_client.orchestration import PowerOrchestrator


class Client:
    def __init__(self):
        self.calls = []
        self.closed = 0
        self.failure = None

    async def apply_profile(self, name):
        self.calls.append(("apply_profile", (name,)))
        if self.failure:
            raise self.failure
        return "profile"

    async def acquire_lease(self, ttl_seconds):
        self.calls.append(("acquire_lease", (ttl_seconds,)))
        return "acquire"

    async def renew_lease(self, lease_id, ttl_seconds):
        self.calls.append(("renew_lease", (lease_id, ttl_seconds)))
        return "renew"

    async def release_lease(self, lease_id):
        self.calls.append(("release_lease", (lease_id,)))

    async def aclose(self):
        self.closed += 1


class Orchestrator:
    def __init__(self, client, *args, **kwargs):
        self.client = client
        self.args = args
        self.kwargs = kwargs
        self.calls = []
        self.closed = 0
        self.failure = None

    async def wake(self, request):
        self.calls.append(("wake", request))
        if self.failure:
            raise self.failure
        return "wake"

    async def aclose(self):
        self.closed += 1

    async def __aenter__(self):
        self.calls.append(("enter", None))
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.calls.append(("exit", (exc_type, exc, traceback)))
        await self.aclose()


class Sender:
    async def send_magic_packet(self, target):
        return None


class Clock:
    def now(self):
        return 0.0


class Sleeper:
    async def sleep(self, delay):
        return None


class Factory:
    def __init__(self, trust):
        self.trust = trust
        self.closed = 0

    def build(self, definition):
        raise AssertionError("no probe expected")

    async def aclose(self):
        self.closed += 1


class PowerManagementTests(unittest.IsolatedAsyncioTestCase):
    def _connection(self):
        return ServiceConnectionConfig("https://power.example.test", 5, TrustConfig())

    def test_factory_builds_one_client_shared_with_owned_orchestrator(self):
        client = Client()
        with (
            patch("power_client.composition.compose_client", return_value=client) as compose_client,
            patch("power_client.composition.PowerOrchestrator", Orchestrator),
            patch("power_client.composition.SocketWakeSender", Sender),
            patch("power_client.composition.MonotonicClock", Clock),
            patch("power_client.composition.AsyncioSleeper", Sleeper),
            patch("power_client.composition.DefaultReadinessProbeFactory", Factory),
        ):
            management = compose_power_management(self._connection(), CredentialFileReference(Path("/credential")))

        self.assertIsInstance(management, _ComposedPowerManagement)
        self.assertIs(management._client, client)
        self.assertIs(management._orchestrator.client, client)
        self.assertEqual(compose_client.call_count, 1)
        self.assertEqual(management._orchestrator.kwargs, {"owns_client": True, "owns_probe_factory": True})
        with self.assertRaises(TypeError):
            PowerManagement()

    async def test_wake_profile_and_lease_operations_delegate_to_the_shared_collaborators(self):
        client = Client()
        orchestrator = Orchestrator(client)
        management = _ComposedPowerManagement(client, orchestrator)

        self.assertEqual(await management.wake("request"), "wake")
        self.assertEqual(await management.apply_profile("profile"), "profile")
        self.assertEqual(await management.acquire_lease(30), "acquire")
        self.assertEqual(await management.renew_lease("lease", 15), "renew")
        self.assertIsNone(await management.release_lease("lease"))
        self.assertEqual(orchestrator.calls, [("wake", "request")])
        self.assertEqual(client.calls, [
            ("apply_profile", ("profile",)),
            ("acquire_lease", (30,)),
            ("renew_lease", ("lease", 15)),
            ("release_lease", ("lease",)),
        ])

    async def test_close_inherits_orchestrator_once_only_resource_cleanup(self):
        client = Client()
        factory = Factory(TrustConfig())
        orchestrator = PowerOrchestrator(client, Sender(), Clock(), Sleeper(), factory, owns_client=True, owns_probe_factory=True)
        management = _ComposedPowerManagement(client, orchestrator)

        await management.aclose()
        await management.aclose()

        self.assertEqual(factory.closed, 1)
        self.assertEqual(client.closed, 1)

    async def test_context_entry_delegates_to_orchestrator_and_closed_entry_is_rejected(self):
        client = Client()
        factory = Factory(TrustConfig())
        orchestrator = PowerOrchestrator(client, Sender(), Clock(), Sleeper(), factory, owns_client=True, owns_probe_factory=True)
        management = _ComposedPowerManagement(client, orchestrator)

        async with management as entered:
            self.assertIs(entered, management)
        with self.assertRaises(ConfigurationError) as raised:
            async with management:
                pass
        self.assertEqual((raised.exception.operation, raised.exception.field, raised.exception.message), ("wake", "state", "orchestrator is closed"))

    async def test_typed_failure_identity_propagates(self):
        client = Client()
        orchestrator = Orchestrator(client)
        management = _ComposedPowerManagement(client, orchestrator)
        error = ConfigurationError("apply_profile", "name", "invalid")
        client.failure = error

        with self.assertRaises(ConfigurationError) as raised:
            await management.apply_profile("profile")
        self.assertIs(raised.exception, error)

    async def test_cancellation_propagates_from_wake_and_client_delegate(self):
        client = Client()
        orchestrator = Orchestrator(client)
        management = _ComposedPowerManagement(client, orchestrator)
        orchestrator.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await management.wake("request")

        client.failure = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await management.apply_profile("profile")
