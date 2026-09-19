import json
from pathlib import Path
import ssl
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer
import trustme

from power_client.composition import compose_client
from power_client.config import CredentialFileReference, ServiceConnectionConfig, TrustConfig
from power_client.errors import RequestTimeoutError, TlsVerificationError
from power_client.models import LeaseId, ProfileName
from tests.helpers import lease_payload, profile_application_payload, profile_catalog_payload, status_payload, suspend_receipt_payload


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_composed_verified_tls_profile_and_suspend_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = trustme.CA()
            certificate = ca.issue_cert("localhost", "127.0.0.1")
            cert = root / "server.pem"; key = root / "server.key"; ca_file = root / "ca.pem"
            certificate.cert_chain_pems[0].write_to_path(cert); certificate.private_key_pem.write_to_path(key); ca.cert_pem.write_to_path(ca_file)
            credential = root / "credential"; credential.write_text("id.secret\n", encoding="utf-8"); credential.chmod(0o600)

            async def handler(request):
                if request.headers.get("Authorization") != "Bearer id.secret":
                    return web.json_response({"error": {"code": "unauthenticated", "request_id": "r"}}, status=401)
                if request.path == "/v1/profiles":
                    return web.json_response(profile_catalog_payload())
                if request.path == "/v1/profiles/balanced":
                    return web.json_response(profile_application_payload())
                if request.path == "/v1/suspend":
                    return web.json_response(suspend_receipt_payload(), status=202)
                if request.path == "/v1/leases":
                    self.assertEqual(await request.json(), {"ttl_seconds": 30})
                    return web.json_response(lease_payload(), status=201)
                if request.path.endswith("/renew"):
                    self.assertEqual(await request.json(), {"ttl_seconds": 15})
                    return web.json_response(lease_payload(request_id="renew-1", ttl_seconds=15), status=201)
                if request.path.endswith("/123e4567-e89b-12d3-a456-426614174000"):
                    self.assertEqual(await request.read(), b"")
                    return web.Response(status=204)
                return web.Response(status=404)

            app = web.Application()
            app.router.add_get("/v1/profiles", handler)
            app.router.add_post("/v1/profiles/{name}", handler)
            app.router.add_post("/v1/suspend", handler)
            app.router.add_post("/v1/leases", handler)
            app.router.add_post("/v1/leases/{lease_id}/renew", handler)
            app.router.add_delete("/v1/leases/{lease_id}", handler)
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH); context.load_cert_chain(cert, key)
            server = TestServer(app, scheme="https"); await server.start_server(ssl=context)
            try:
                endpoint = str(server.make_url("/")).rstrip("/")
                client = compose_client(ServiceConnectionConfig(endpoint, 2, TrustConfig(ca_file)), CredentialFileReference(credential))
                async with client:
                    self.assertEqual((await client.list_profiles()).request_id, "profiles-1")
                    self.assertEqual((await client.apply_profile(ProfileName("balanced"))).request_id, "apply-1")
                    self.assertEqual((await client.request_suspend()).request_id, "suspend-1")
                    lease_id = LeaseId("123e4567-e89b-12d3-a456-426614174000")
                    self.assertEqual((await client.acquire_lease(30)).lease_id, lease_id)
                    self.assertEqual((await client.renew_lease(lease_id, 15)).request_id, "renew-1")
                    self.assertIsNone(await client.release_lease(lease_id))
            finally:
                await server.close()

    async def test_composed_verified_tls_status_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = trustme.CA()
            certificate = ca.issue_cert("localhost", "127.0.0.1")
            cert = root / "server.pem"; key = root / "server.key"; ca_file = root / "ca.pem"
            certificate.cert_chain_pems[0].write_to_path(cert); certificate.private_key_pem.write_to_path(key); ca.cert_pem.write_to_path(ca_file)
            credential = root / "credential"; credential.write_text("id.secret\n", encoding="utf-8"); credential.chmod(0o600)
            async def handler(request):
                if request.headers.get("Authorization") != "Bearer id.secret": return web.Response(status=401, body=b'{"error":{"code":"unauthenticated","request_id":"r"}}')
                return web.Response(body=json.dumps(status_payload()).encode(), content_type="application/json")
            app = web.Application(); app.router.add_get("/v1/status", handler)
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH); context.load_cert_chain(cert, key)
            server = TestServer(app, scheme="https"); await server.start_server(ssl=context)
            try:
                client = compose_client(ServiceConnectionConfig(str(server.make_url("/")).rstrip("/"), 2, TrustConfig(ca_file)), CredentialFileReference(credential))
                async with client:
                    self.assertEqual((await client.get_status()).request_id, "request-1")
            finally:
                await server.close()

    async def test_wrong_ca_and_configured_timeout_fail_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca = trustme.CA(); wrong_ca = trustme.CA(); certificate = ca.issue_cert("localhost", "127.0.0.1")
            cert = root / "server.pem"; key = root / "server.key"; ca_file = root / "ca.pem"; wrong_ca_file = root / "wrong-ca.pem"
            certificate.cert_chain_pems[0].write_to_path(cert); certificate.private_key_pem.write_to_path(key); ca.cert_pem.write_to_path(ca_file); wrong_ca.cert_pem.write_to_path(wrong_ca_file)
            credential = root / "credential"; credential.write_text("id.secret\n", encoding="utf-8"); credential.chmod(0o600)
            async def delayed(request):
                import asyncio
                await asyncio.sleep(0.05)
                return web.Response(body=json.dumps(status_payload()).encode())
            app = web.Application(); app.router.add_get("/v1/status", delayed)
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH); context.load_cert_chain(cert, key)
            server = TestServer(app, scheme="https"); await server.start_server(ssl=context)
            endpoint = str(server.make_url("/")).rstrip("/")
            try:
                bad_trust = compose_client(ServiceConnectionConfig(endpoint, 1, TrustConfig(wrong_ca_file)), CredentialFileReference(credential))
                with self.assertRaises(TlsVerificationError):
                    await bad_trust.get_status()
                await bad_trust.aclose()
                timed_out = compose_client(ServiceConnectionConfig(endpoint, 0.001, TrustConfig(ca_file)), CredentialFileReference(credential))
                with self.assertRaises(RequestTimeoutError):
                    await timed_out.get_status()
                await timed_out.aclose()
            finally:
                await server.close()
