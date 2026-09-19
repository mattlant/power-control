import asyncio
from uuid import uuid4
from .protocol import *
from .mapping import status_from_dict


class BrokerUnavailable(Exception): pass


class ProfileBrokerClient:
    def __init__(self, socket_path, timeout_seconds):
        self.socket_path = socket_path;self.timeout_seconds = timeout_seconds

    async def _call(self, request):
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection(self.socket_path), self.timeout_seconds)
            w.write(encode_request(request))
            await w.drain()
            frame = await asyncio.wait_for(r.readline(), self.timeout_seconds)
            w.close()
            await w.wait_closed()
            result = decode_result(frame)
            if result.request_id != request.request_id or isinstance(result, BrokerFailure): raise BrokerUnavailable()
            return result
        except Exception as e:
            raise BrokerUnavailable() from e

    async def get_status(self):
        return (await self._call(BrokerRequest(uuid4()))).status

    async def get_profiles(self):
        return await self._call(BrokerRequest(uuid4(), BrokerOperation.GET_PROFILES))

    async def apply_profile(self, name):
        return await self._call(BrokerRequest(uuid4(), BrokerOperation.APPLY_PROFILE, name))

    async def suspend(self):
        return await self._call(BrokerRequest(uuid4(), BrokerOperation.SUSPEND))

    async def release_suspend(self, receipt_id):
        return await self._call(BrokerRequest(uuid4(), BrokerOperation.RELEASE_SUSPEND, suspend_receipt_id=receipt_id))

    async def acquire_lease(self, principal_id, ttl):
        return await self._call(
            BrokerRequest(uuid4(), BrokerOperation.ACQUIRE_LEASE, principal_id=principal_id, requested_ttl_seconds=ttl))

    async def renew_lease(self, principal_id, lease_id, ttl):
        return await self._call(
            BrokerRequest(uuid4(), BrokerOperation.RENEW_LEASE, principal_id=principal_id, lease_id=lease_id,
                          requested_ttl_seconds=ttl))

    async def release_lease(self, principal_id, lease_id):
        return await self._call(
            BrokerRequest(uuid4(), BrokerOperation.RELEASE_LEASE, principal_id=principal_id, lease_id=lease_id))

    async def list_leases(self):
        return await self._call(BrokerRequest(uuid4(), BrokerOperation.LIST_LEASES))


StatusBrokerClient = ProfileBrokerClient
