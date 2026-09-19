import asyncio
import unittest
from ipaddress import IPv4Address

from power_client.config import ReadinessWaitPolicy, ServiceWaitPolicy, TcpReadinessTarget, WakeTarget
from power_client.errors import (
    AuthenticationError,
    ConfigurationError,
    ProtocolError,
    ServiceReadinessTimeout,
    ServiceUnavailableError,
    TransportError,
    WakeError,
)
from power_client.models import ProbeObservation, ProbeState, WakeRequest
from power_client.orchestration import PowerOrchestrator


class Factory:
    def __init__(self, events=None):
        self.events = events if events is not None else []
        self.closed = 0

    def build(self, definition):
        self.events.append("probe")
        raise AssertionError("no probe expected")

    async def aclose(self):
        self.closed += 1


class ScriptedProbe:
    def __init__(self, identity, observations, clock, events=None):
        self.identity = identity
        self.observations = list(observations)
        self.clock = clock
        self.timeouts = []
        self.events = events if events is not None else []

    async def check(self, timeout_seconds):
        self.timeouts.append(timeout_seconds)
        self.events.append(f"check:{self.identity}")
        observation = self.observations.pop(0)
        if isinstance(observation, (int, float)):
            self.clock.value += observation
            observation = ProbeObservation(ProbeState.NOT_READY, "connection")
        return observation


class ScriptedFactory:
    def __init__(self, probes, events=None):
        self.probes = {probe.identity: probe for probe in probes}
        self.events = events if events is not None else []
        self.built = []
        self.closed = 0

    def build(self, definition):
        self.built.append(definition.identity)
        self.events.append(f"build:{definition.identity}")
        return self.probes[definition.identity]

    async def aclose(self):
        self.closed += 1


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value


class Sleeper:
    def __init__(self, clock):
        self.clock = clock
        self.delays = []

    async def sleep(self, delay):
        self.delays.append(delay)
        self.clock.value += delay


class Sender:
    def __init__(self, events):
        self.events = events

    async def send_magic_packet(self, target):
        self.events.append("packet")


class Client:
    def __init__(self, events, responses):
        self.events = events
        self.responses = list(responses)
        self.closed = 0

    async def get_status(self):
        self.events.append("status")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self):
        self.closed += 1


def request(timeout=5.0, interval=2.0):
    return WakeRequest(WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("192.0.2.255"), 9), ServiceWaitPolicy(timeout, interval))


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_packet_precedes_already_awake_status(self):
        events = []
        clock = Clock()
        client = Client(events, [object()])
        result = await PowerOrchestrator(client, Sender(events), clock, Sleeper(clock), Factory(events)).wake(request())
        self.assertIsNotNone(result.service_status)
        self.assertEqual(events, ["packet", "status"])

    async def test_transient_failures_retry_at_bounded_cadence(self):
        events = []
        clock = Clock()
        sleeper = Sleeper(clock)
        client = Client(events, [TransportError("get_status", "https://redacted", "connection"), ServiceUnavailableError("get_status", 503, "request", "unavailable"), object()])
        result = await PowerOrchestrator(client, Sender(events), clock, sleeper, Factory(events)).wake(request())
        self.assertIsNotNone(result.service_status)
        self.assertEqual(sleeper.delays, [2.0, 2.0])

    async def test_deadline_raises_without_post_deadline_attempt(self):
        events = []
        clock = Clock()
        sleeper = Sleeper(clock)
        client = Client(events, [TransportError("get_status", "https://redacted", "timeout")])
        with self.assertRaises(ServiceReadinessTimeout) as raised:
            await PowerOrchestrator(client, Sender(events), clock, sleeper, Factory(events)).wake(request(timeout=2.0, interval=2.0))
        self.assertEqual(raised.exception.last_failure, "timeout")
        self.assertEqual(events, ["packet", "status"])

    async def test_cancellation_propagates(self):
        class CancelSender:
            async def send_magic_packet(self, target):
                raise asyncio.CancelledError

        with self.assertRaises(asyncio.CancelledError):
            await PowerOrchestrator(Client([], [object()]), CancelSender(), Clock(), Sleeper(Clock()), Factory()).wake(request())

    async def test_fatal_status_errors_do_not_retry(self):
        for error in (
            AuthenticationError("get_status", 401, "request-id", "invalid"),
            ProtocolError("get_status", "request-id", "malformed"),
            ConfigurationError("get_status", "client", "invalid"),
        ):
            with self.subTest(error=type(error).__name__):
                events = []
                client = Client(events, [error])
                with self.assertRaises(type(error)):
                    await PowerOrchestrator(client, Sender(events), Clock(), Sleeper(Clock()), Factory()).wake(request())
                self.assertEqual(events, ["packet", "status"])

    async def test_sender_failure_prevents_status_and_redacts_target(self):
        class FailingSender:
            async def send_magic_packet(self, target):
                raise WakeError("wake", target, "udp_send")

        events = []
        target = request().target
        error = WakeError("wake", target, "udp_send")
        self.assertNotIn(target.mac_address, str(error))
        self.assertNotIn(str(target.broadcast_address), str(error))
        with self.assertRaises(WakeError):
            await PowerOrchestrator(Client(events, [object()]), FailingSender(), Clock(), Sleeper(Clock()), Factory()).wake(request())
        self.assertEqual(events, [])

    async def test_status_cancellation_propagates_without_retry(self):
        class CancelClient(Client):
            async def get_status(self):
                self.events.append("status")
                raise asyncio.CancelledError

        events = []
        with self.assertRaises(asyncio.CancelledError):
            await PowerOrchestrator(CancelClient(events, []), Sender(events), Clock(), Sleeper(Clock()), Factory()).wake(request())
        self.assertEqual(events, ["packet", "status"])

    async def test_sleeper_cancellation_propagates_without_extra_status(self):
        class CancelSleeper:
            async def sleep(self, delay):
                raise asyncio.CancelledError

        events = []
        client = Client(events, [TransportError("get_status", "https://redacted", "connection")])
        with self.assertRaises(asyncio.CancelledError):
            await PowerOrchestrator(client, Sender(events), Clock(), CancelSleeper(), Factory()).wake(request())
        self.assertEqual(events, ["packet", "status"])

    async def test_client_lifecycle_is_owned_only_when_composed(self):
        clock = Clock()
        owned = Client([], [object()])
        orchestrator = PowerOrchestrator(owned, Sender([]), clock, Sleeper(clock), Factory(), owns_client=True)
        await orchestrator.aclose()
        await orchestrator.aclose()
        self.assertEqual(owned.closed, 1)

        injected = Client([], [object()])
        await PowerOrchestrator(injected, Sender([]), clock, Sleeper(clock), Factory()).aclose()
        self.assertEqual(injected.closed, 0)

    async def test_result_has_no_workload_probe_or_lifecycle_claim(self):
        events = []
        result = await PowerOrchestrator(Client(events, [object()]), Sender(events), Clock(), Sleeper(Clock()), Factory()).wake(request())
        self.assertEqual(result.probe_results, ())
        self.assertEqual(events, ["packet", "status"])

    async def test_workloads_run_after_service_in_declared_order(self):
        events = []
        clock = Clock()
        first = ScriptedProbe("first", [ProbeObservation(ProbeState.READY)], clock, events)
        second = ScriptedProbe("second", [ProbeObservation(ProbeState.READY)], clock, events)
        factory = ScriptedFactory((first, second), events)
        wake_request = WakeRequest(
            request().target,
            request().service_wait_policy,
            (TcpReadinessTarget("first", "localhost", 1), TcpReadinessTarget("second", "localhost", 2)),
            ReadinessWaitPolicy(5, 2, 1),
        )
        result = await PowerOrchestrator(Client(events, [object()]), Sender(events), clock, Sleeper(clock), factory).wake(wake_request)
        self.assertEqual(events, ["packet", "status", "build:first", "check:first", "build:second", "check:second"])
        self.assertEqual([item.probe_identity for item in result.probe_results], ["first", "second"])

    async def test_workload_retries_transient_observation_with_bounded_attempts(self):
        clock = Clock()
        sleeper = Sleeper(clock)
        probe = ScriptedProbe("target", [ProbeObservation(ProbeState.NOT_READY, "name_resolution"), ProbeObservation(ProbeState.READY)], clock)
        factory = ScriptedFactory((probe,))
        wake_request = WakeRequest(request().target, request().service_wait_policy, (TcpReadinessTarget("target", "localhost", 1),), ReadinessWaitPolicy(5, 2, 1))
        result = await PowerOrchestrator(Client([], [object()]), Sender([]), clock, sleeper, factory).wake(wake_request)
        self.assertEqual(probe.timeouts, [2, 2])
        self.assertEqual(sleeper.delays, [1])
        self.assertEqual(result.probe_results[0].state, "ready")

    async def test_workload_timeout_does_not_attempt_after_deadline(self):
        clock = Clock()
        probe = ScriptedProbe("target", [2, 2, ProbeObservation(ProbeState.READY)], clock)
        factory = ScriptedFactory((probe,))
        wake_request = WakeRequest(request().target, request().service_wait_policy, (TcpReadinessTarget("target", "localhost", 1),), ReadinessWaitPolicy(5, 2, 1))
        with self.assertRaises(Exception) as raised:
            await PowerOrchestrator(Client([], [object()]), Sender([]), clock, Sleeper(clock), factory).wake(wake_request)
        self.assertEqual(type(raised.exception).__name__, "WorkloadReadinessTimeout")
        self.assertEqual(len(probe.timeouts), 2)
