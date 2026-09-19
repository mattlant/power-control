from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import ssl
import stat
import socket
import time

import aiohttp

from .config import HttpReadinessTarget, ServiceConnectionConfig, TcpReadinessTarget, TrustConfig, WakeTarget
from .errors import ConfigurationError, RequestTimeoutError, TlsVerificationError, TransportError, WakeError
from .models import BearerCredential, ProbeObservation, ProbeState, ReadinessProbeDefinition, TransportRequest, TransportResponse, thaw_json


class FileCredentialSource:
    """Platform-appropriate protected-file credential adapter."""

    def __init__(self, path: Path) -> None:
        self._path = path

    async def read(self) -> BearerCredential:
        return await asyncio.to_thread(self._read_file)

    def _read_file(self) -> BearerCredential:
        try:
            mode = self._path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
                raise ValueError()
            if os.name == "posix":
                if mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise ValueError()
            elif os.name == "nt":
                if not _windows_file_is_protected(self._path):
                    raise ValueError()
            else:
                raise ValueError()
            value = self._path.read_text(encoding="utf-8")
        except (OSError, ValueError) as error:
            raise ConfigurationError("credential", "file", "is unavailable or insufficiently protected") from error
        if value.endswith("\n"):
            value = value[:-1]
        if "\n" in value or "\r" in value or "." not in value:
            raise ConfigurationError("credential", "file", "has invalid content")
        credential_id, secret = value.split(".", 1)
        return BearerCredential(credential_id, secret)


def _windows_file_is_protected(path: Path) -> bool:
    """Require a Windows file DACL limited to the owner and built-in admins."""
    if os.name != "nt":
        raise OSError("Windows ACL validation is only available on Windows")

    advapi32 = ctypes.WinDLL("Advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32", use_last_error=True)
    security_descriptor = ctypes.c_void_p()
    owner_sid = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    dacl_present = ctypes.c_int()
    dacl_defaulted = ctypes.c_int()

    get_named_security_info = advapi32.GetNamedSecurityInfoW
    get_named_security_info.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_named_security_info.restype = ctypes.c_uint32
    get_security_descriptor_dacl = advapi32.GetSecurityDescriptorDacl
    get_security_descriptor_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_int),
    ]
    get_security_descriptor_dacl.restype = ctypes.c_int
    get_ace = advapi32.GetAce
    get_ace.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    get_ace.restype = ctypes.c_int
    convert_sid_to_string = advapi32.ConvertSidToStringSidW
    convert_sid_to_string.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    convert_sid_to_string.restype = ctypes.c_int
    local_free = kernel32.LocalFree
    local_free.argtypes = [ctypes.c_void_p]
    local_free.restype = ctypes.c_void_p

    security_info = 0x00000001 | 0x00000004
    result = get_named_security_info(
        str(path),
        1,
        security_info,
        ctypes.byref(owner_sid),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(security_descriptor),
    )
    if result != 0:
        raise OSError(result, "GetNamedSecurityInfoW failed")

    try:
        if not get_security_descriptor_dacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present.value or not dacl:
            return False

        class AceHeader(ctypes.Structure):
            _fields_ = [
                ("ace_type", ctypes.c_ubyte),
                ("ace_flags", ctypes.c_ubyte),
                ("ace_size", ctypes.c_uint16),
            ]

        class Acl(ctypes.Structure):
            _fields_ = [
                ("acl_revision", ctypes.c_ubyte),
                ("sbz1", ctypes.c_ubyte),
                ("acl_size", ctypes.c_uint16),
                ("ace_count", ctypes.c_uint16),
                ("sbz2", ctypes.c_uint16),
            ]

        class AccessAllowedAce(ctypes.Structure):
            _fields_ = [
                ("header", AceHeader),
                ("mask", ctypes.c_uint32),
                ("sid_start", ctypes.c_uint32),
            ]

        owner_string = ctypes.c_wchar_p()
        if not convert_sid_to_string(owner_sid, ctypes.byref(owner_string)):
            raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
        try:
            allowed_sids = {owner_string.value, "S-1-5-18", "S-1-5-32-544"}
        finally:
            local_free(owner_string)

        acl = ctypes.cast(dacl, ctypes.POINTER(Acl)).contents
        for index in range(acl.ace_count):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)):
                raise OSError(ctypes.get_last_error(), "GetAce failed")
            header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
            if header.ace_type != 0:
                continue
            allowed_ace = ctypes.cast(ace, ctypes.POINTER(AccessAllowedAce)).contents
            sid_pointer = ctypes.c_void_p(ace.value + AccessAllowedAce.sid_start.offset)
            sid_string = ctypes.c_wchar_p()
            if not convert_sid_to_string(sid_pointer, ctypes.byref(sid_string)):
                raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
            try:
                if allowed_ace.mask and sid_string.value not in allowed_sids:
                    return False
            finally:
                local_free(sid_string)
        return True
    finally:
        local_free(security_descriptor)


class AioHttpTransport:
    _MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(self, connection: ServiceConnectionConfig) -> None:
        self._endpoint = connection.endpoint.rstrip("/")
        self._timeout = connection.request_timeout
        self._context = ssl.create_default_context()
        if connection.trust.additional_ca_bundle_path is not None:
            self._context.load_verify_locations(cafile=connection.trust.additional_ca_bundle_path)
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise ConfigurationError("transport", "state", "is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def send(self, request: TransportRequest) -> TransportResponse:
        session = await self._get_session()
        headers = {"Authorization": f"Bearer {request.credential.credential_id}.{request.credential.secret}"}
        body = None
        if request.body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(thaw_json(request.body), allow_nan=False)
        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with session.request(request.method.value, self._endpoint + request.path, headers=headers, data=body, timeout=timeout, ssl=self._context) as response:
                if response.content_length is not None and response.content_length > self._MAX_RESPONSE_BYTES:
                    raise TransportError("transport", self._endpoint, "response_too_large")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > self._MAX_RESPONSE_BYTES:
                        raise TransportError("transport", self._endpoint, "response_too_large")
                    chunks.append(chunk)
                return TransportResponse(response.status, b"".join(chunks))
        except asyncio.TimeoutError as error:
            raise RequestTimeoutError("transport", self._endpoint, "timeout") from error
        except aiohttp.ClientConnectorCertificateError as error:
            raise TlsVerificationError("transport", self._endpoint, "certificate_verification") from error
        except aiohttp.ClientSSLError as error:
            raise TlsVerificationError("transport", self._endpoint, "tls") from error
        except asyncio.CancelledError:
            raise
        except TransportError:
            raise
        except aiohttp.ClientError as error:
            raise TransportError("transport", self._endpoint, "connection") from error

    async def aclose(self) -> None:
        self._closed = True
        if self._session is not None:
            await self._session.close()


class ReadinessHttpSession:
    """Lazy verified HTTP session used only for generic readiness observations."""

    def __init__(self, trust: TrustConfig) -> None:
        self._context = ssl.create_default_context()
        if trust.additional_ca_bundle_path is not None:
            self._context.load_verify_locations(cafile=trust.additional_ca_bundle_path)
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise ConfigurationError("readiness", "state", "is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def request(self, target: HttpReadinessTarget, timeout_seconds: float) -> int:
        session = await self._get_session()
        try:
            timeout = aiohttp.ClientTimeout(total=timeout_seconds)
            async with session.get(target.url, timeout=timeout, ssl=self._context) as response:
                return response.status
        except asyncio.TimeoutError:
            raise
        except aiohttp.ClientConnectorCertificateError as error:
            raise TlsVerificationError("readiness", "redacted", "tls_verification") from error
        except aiohttp.ClientSSLError as error:
            raise TlsVerificationError("readiness", "redacted", "tls_verification") from error

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._session is not None:
            await self._session.close()


class TcpReadinessProbe:
    def __init__(self, target: TcpReadinessTarget) -> None:
        self._target = target

    @property
    def identity(self) -> str:
        return self._target.identity

    async def check(self, timeout_seconds: float) -> ProbeObservation:
        writer: asyncio.StreamWriter | None = None
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._target.host, self._target.port),
                timeout=timeout_seconds,
            )
            return ProbeObservation(ProbeState.READY)
        except asyncio.CancelledError:
            raise
        except socket.gaierror:
            return ProbeObservation(ProbeState.NOT_READY, "name_resolution")
        except (OSError, asyncio.TimeoutError):
            return ProbeObservation(ProbeState.NOT_READY, "connection")
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.CancelledError):
                    if asyncio.current_task() and asyncio.current_task().cancelling():
                        raise


class HttpReadinessProbe:
    def __init__(self, target: HttpReadinessTarget, session: ReadinessHttpSession) -> None:
        self._target = target
        self._session = session

    @property
    def identity(self) -> str:
        return self._target.identity

    async def check(self, timeout_seconds: float) -> ProbeObservation:
        try:
            status = await self._session.request(self._target, timeout_seconds)
        except asyncio.CancelledError:
            raise
        except TlsVerificationError:
            raise
        except asyncio.TimeoutError:
            return ProbeObservation(ProbeState.NOT_READY, "timeout")
        except aiohttp.ClientConnectorError as error:
            if isinstance(error.os_error, socket.gaierror):
                return ProbeObservation(ProbeState.NOT_READY, "name_resolution")
            return ProbeObservation(ProbeState.NOT_READY, "connection")
        except aiohttp.ClientError:
            return ProbeObservation(ProbeState.NOT_READY, "connection")
        if status in self._target.expected_statuses:
            return ProbeObservation(ProbeState.READY)
        return ProbeObservation(ProbeState.NOT_READY, "http_status")


class DefaultReadinessProbeFactory:
    def __init__(self, trust: TrustConfig) -> None:
        self._http_session = ReadinessHttpSession(trust)
        self._closed = False

    def build(self, definition: ReadinessProbeDefinition):
        if self._closed:
            raise ConfigurationError("readiness", "state", "factory is closed")
        if isinstance(definition, TcpReadinessTarget):
            return TcpReadinessProbe(definition)
        if isinstance(definition, HttpReadinessTarget):
            return HttpReadinessProbe(definition, self._http_session)
        raise ConfigurationError("wake", "probe", "contains an unsupported definition")

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._http_session.aclose()


class SocketWakeSender:
    async def send_magic_packet(self, target: WakeTarget) -> None:
        packet = b"\xff" * 6 + target.mac_bytes * 16
        try:
            def send() -> None:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
                    sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    sender.sendto(packet, (str(target.broadcast_address), target.udp_port))
            await asyncio.to_thread(send)
        except asyncio.CancelledError:
            raise
        except OSError as error:
            raise WakeError("wake", target, "udp_send") from error


class MonotonicClock:
    def now(self) -> float:
        return time.monotonic()


class AsyncioSleeper:
    async def sleep(self, delay_seconds: float) -> None:
        await asyncio.sleep(delay_seconds)
