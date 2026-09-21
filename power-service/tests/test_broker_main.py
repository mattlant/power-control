import asyncio
import signal
import unittest

from power_service.broker_main import install_signal_handlers


class Loop:
    def __init__(self):
        self.handlers = {}

    def add_signal_handler(self, signum, callback):
        self.handlers[signum] = callback

    def remove_signal_handler(self, signum):
        self.handlers.pop(signum, None)


class Runtime:
    def __init__(self):
        self.reload_calls = 0
        self.resume_calls = 0

    async def reload(self):
        self.reload_calls += 1

    async def reconcile_resume(self):
        self.resume_calls += 1


class BrokerMainTests(unittest.IsolatedAsyncioTestCase):
    async def test_signal_handlers_dispatch_resume_reload_and_stop_and_cleanup(self):
        loop = Loop()
        runtime = Runtime()
        server = type("Server", (), {"runtime": runtime})()
        stop_event = asyncio.Event()

        install_signal_handlers(loop, server, stop_event)
        loop.handlers[signal.SIGUSR1]()
        loop.handlers[signal.SIGHUP]()
        loop.handlers[signal.SIGTERM]()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        self.assertEqual(runtime.resume_calls, 1)
        self.assertEqual(runtime.reload_calls, 1)
        self.assertTrue(stop_event.is_set())
        for signum in (signal.SIGHUP, signal.SIGUSR1, signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        self.assertEqual(loop.handlers, {})
