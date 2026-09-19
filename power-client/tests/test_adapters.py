import os
import asyncio
from pathlib import Path
from ipaddress import IPv4Address
import socket
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
import power_client.adapters as adapters
from power_client.adapters import FileCredentialSource, HttpReadinessProbe, ReadinessHttpSession, SocketWakeSender, TcpReadinessProbe
from power_client.config import HttpReadinessTarget, TcpReadinessTarget, TrustConfig, WakeTarget
from power_client.errors import ConfigurationError
from power_client.models import ProbeState


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_probe_reports_success_and_refusal(self):
        async def handler(reader, writer):
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            ready = await TcpReadinessProbe(TcpReadinessTarget("tcp", "127.0.0.1", port)).check(1)
            refused = await TcpReadinessProbe(TcpReadinessTarget("tcp", "127.0.0.1", port + 1)).check(0.1)
        finally:
            server.close()
            await server.wait_closed()
        self.assertEqual(ready.state, ProbeState.READY)
        self.assertEqual(refused.last_failure, "connection")

    async def test_http_probe_uses_get_without_bearer_and_maps_status(self):
        seen_headers = []

        async def handler(request):
            seen_headers.append(dict(request.headers))
            return web.Response(status=204)

        app = web.Application()
        app.router.add_get("/health", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        session = ReadinessHttpSession(TrustConfig())
        try:
            target = HttpReadinessTarget("http", f"http://127.0.0.1:{port}/health", (204,))
            observation = await HttpReadinessProbe(target, session).check(1)
        finally:
            await session.aclose()
            await runner.cleanup()
        self.assertEqual(observation.state, ProbeState.READY)
        self.assertNotIn("Authorization", seen_headers[0])
    async def test_socket_sender_emits_one_magic_packet(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(2)
        target = WakeTarget("AA:BB:CC:DD:EE:FF", IPv4Address("127.0.0.1"), receiver.getsockname()[1])
        await SocketWakeSender().send_magic_packet(target)
        packet, _ = receiver.recvfrom(2048)
        receiver.close()
        self.assertEqual(packet, b"\xff" * 6 + bytes.fromhex("aabbccddeeff") * 16)
        self.assertEqual(len(packet), 102)

    async def test_posix_file_credential_requires_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("id.secret\n", encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertEqual((await FileCredentialSource(path).read()).credential_id, "id")
            os.chmod(path, 0o644)
            with self.assertRaises(ConfigurationError):
                await FileCredentialSource(path).read()

    async def test_file_adapter_rejects_symlinks_and_accepts_dotted_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("id.secret.with.dots\n", encoding="utf-8")
            os.chmod(target, 0o600)
            link = root / "credential"
            link.symlink_to(target)
            with self.assertRaises(ConfigurationError):
                await FileCredentialSource(link).read()
            self.assertEqual((await FileCredentialSource(target).read()).secret, "secret.with.dots")

    async def test_windows_file_credential_uses_acl_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("id.secret\n", encoding="utf-8")
            with (
                patch.object(adapters.os, "name", "nt"),
                patch.object(adapters, "_windows_file_is_protected", return_value=True) as validator,
            ):
                credential = FileCredentialSource(path)._read_file()
            self.assertEqual(credential.credential_id, "id")
            validator.assert_called_once_with(path)

    async def test_windows_file_credential_rejects_unprotected_acl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("id.secret\n", encoding="utf-8")
            with (
                patch.object(adapters.os, "name", "nt"),
                patch.object(adapters, "_windows_file_is_protected", return_value=False),
            ):
                with self.assertRaises(ConfigurationError):
                    FileCredentialSource(path)._read_file()
