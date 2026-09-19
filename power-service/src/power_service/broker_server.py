import asyncio
import os
import pwd
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import load_broker_config
from .host_status import ScopePtyInteractiveSessionReader, LogindSuspendAdapter
from .leases import LeaseStore
from .lifecycle import LifecycleController
from .local_activity import ConfiguredLocalActivityMonitor
from .profile_adapters import CpuProfileAdapter, NvidiaProfileAdapter
from .profiles import ProfileApplicator, ProfileReconciler
from .protocol import *


def get_peer_uid(writer): return \
struct.unpack('3i', writer.get_extra_info('socket').getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]


def set_socket_group(path, api_uid):
    os.chown(path, -1, pwd.getpwuid(api_uid).pw_gid)
    os.chmod(path, 0o660)


set_socket_owner = set_socket_group


class _GateLease:
    def __init__(self): self.future = asyncio.get_running_loop().create_future()


@dataclass
class _CommittedSuspend:
    lease: _GateLease
    commit: object
    watchdog: asyncio.Task


class BrokerOperationGate:
    def __init__(self):
        self._owner = None;self._waiters = deque()

    async def acquire_waiting(self):
        lease = _GateLease()
        if self._owner is None and not self._waiters: self._owner = lease;return lease
        self._waiters.append(lease)
        try:
            await lease.future
            return lease
        except asyncio.CancelledError:
            if lease in self._waiters:
                self._waiters.remove(lease)
            elif self._owner is lease:
                self.release(lease)
            raise

    def try_acquire_suspend(self):
        if self._owner is not None or self._waiters: return None
        lease = _GateLease()
        self._owner = lease
        return lease

    @property
    def busy(self):
        return self._owner is not None or bool(self._waiters)

    def busy_except(self, lease):
        return self._owner is not None and self._owner is not lease or bool(self._waiters)

    def release(self, lease):
        if self._owner is not lease: raise ValueError('gate lease is not owner')
        while self._waiters:
            next_lease = self._waiters.popleft()
            if not next_lease.future.cancelled(): self._owner = next_lease;next_lease.future.set_result(None);return
        self._owner = None


class BrokerRuntime:
    def __init__(self, config, collector, cpu=None, gpu=None, config_path=None, suspend_adapter=None,
                 interactive_session_reader=None):
        self.config = config
        self.collector = collector
        self.config_path = Path(config_path) if config_path else None
        self.gate = BrokerOperationGate()
        self.reconciliation = ProfileReconciliation(ReconciliationState.UNMATCHED)
        self.reload_error = None
        self.cpu = cpu or CpuProfileAdapter(config.operation_timeout_seconds)
        self.gpu = gpu or NvidiaProfileAdapter(config.gpu_index, config.operation_timeout_seconds,
                                               getattr(collector, 'gpu', collector))
        self.suspend_adapter = suspend_adapter or LogindSuspendAdapter(config.operation_timeout_seconds)
        self._committed_suspends = {}
        self._closing = False
        auto = config.automatic_suspend or AutomaticSuspendConfig(False, 3600, 900, 60, 10, 20)
        self.leases = LeaseStore(time.monotonic, lambda: datetime.now(timezone.utc), auto.max_lease_ttl_seconds)
        self.interactive_session_reader = self._interactive_reader(auto, interactive_session_reader)
        self.lifecycle = LifecycleController(self.leases, ConfiguredLocalActivityMonitor(auto.local_activity_probes),
                                             collector, self, auto, time.monotonic, lambda: datetime.now(timezone.utc),
                                             self.interactive_session_reader)

    def _interactive_reader(self, config, injected=None, query_timeout_seconds=None):
        if not config.interactive_sessions_enabled:
            return None
        timeout = query_timeout_seconds if query_timeout_seconds is not None else self.config.query_timeout_seconds
        return injected or ScopePtyInteractiveSessionReader(query_timeout_seconds=timeout)

    async def start_lifecycle(self):
        await self.lifecycle.start()

    async def stop_lifecycle(self):
        await self.lifecycle.stop()

    async def reconcile_lifecycle(self):
        await self.lifecycle.reconcile()

    def reserve_automatic_suspend(self):
        return self.gate.try_acquire_suspend()

    def release_automatic_suspend(self, reservation):
        self.gate.release(reservation);self.lifecycle.signal()

    async def acquire_lease(self, principal_id, ttl):
        lease = await self.gate.acquire_waiting()
        try:
            result = self.leases.acquire(principal_id, ttl)
            self.lifecycle.signal()
            return result, await self._status()
        finally:
            self.gate.release(lease)

    async def renew_lease(self, principal_id, lease_id, ttl):
        lease = await self.gate.acquire_waiting()
        try:
            result = self.leases.renew(principal_id, lease_id, ttl)
            self.lifecycle.signal()
            return result, await self._status()
        finally:
            self.gate.release(lease)

    async def release_lease(self, principal_id, lease_id):
        lease = await self.gate.acquire_waiting()
        try:
            result = self.leases.release(principal_id, lease_id)
            self.lifecycle.signal()
            return result, await self._status()
        finally:
            self.gate.release(lease)

    async def list_leases(self):
        lease = await self.gate.acquire_waiting()
        try:
            return self.leases.list_active()
        finally:
            self.gate.release(lease)

    async def reload(self):
        """Reload root configuration and reconcile while retaining the broker."""
        self.lifecycle.signal()
        lease = await self.gate.acquire_waiting()
        try:
            if self.config_path is None:
                raise ValueError("configuration path is required for reload")
            try:
                new_config = load_broker_config(self.config_path)
            except Exception:
                self.reload_error = ProfileErrorCode.CONFIGURATION
                self.reconciliation = ProfileReconciliation(ReconciliationState.UNAVAILABLE,
                                                            error=ProfileErrorCode.CONFIGURATION)
                return False
            old_auto = self.config.automatic_suspend or AutomaticSuspendConfig(False, 3600, 900, 60, 10, 20)
            new_auto = new_config.automatic_suspend or AutomaticSuspendConfig(False, 3600, 900, 60, 10, 20)
            new_reader = self._interactive_reader(new_auto, query_timeout_seconds=new_config.query_timeout_seconds)
            self.config = new_config
            self.interactive_session_reader = new_reader
            self.lifecycle.reconfigure(new_auto, new_reader)
            self.lifecycle.monitor = ConfiguredLocalActivityMonitor(new_auto.local_activity_probes)
            self.reload_error = None
            self.cpu.timeout = new_config.operation_timeout_seconds
            self.gpu.index = new_config.gpu_index
            self.gpu.timeout = new_config.operation_timeout_seconds
            status = await self.collector.collect()
            self.reconciliation = ProfileReconciler.reconcile(new_config.profiles, status)
            if old_auto.enabled and not new_auto.enabled:
                await self.lifecycle.stop()
            elif not old_auto.enabled and new_auto.enabled:
                await self.lifecycle.start()
            await self.lifecycle.reconcile()
            return True
        finally:
            self.gate.release(lease);self.lifecycle.signal()

    async def _status(self):
        s = await self.collector.collect()
        names = self.config.profiles
        if self.reload_error is None:
            self.reconciliation = ProfileReconciler.reconcile(names, s)
        else:
            self.reconciliation = ProfileReconciliation(ReconciliationState.UNAVAILABLE, error=self.reload_error)
        return ServiceStatus(s.observed_at, s.state, s.cpu, s.gpu, s.logind, self.reconciliation,
                             tuple(sorted(p.name for p in names)), self.lifecycle.status())

    async def status(self):
        lease = await self.gate.acquire_waiting()
        try:
            return await self._status()
        finally:
            self.gate.release(lease)

    async def profiles(self):
        lease = await self.gate.acquire_waiting()
        try:
            _ = await self._status()
            return BrokerProfilesResult(None, tuple(sorted(p.name for p in self.config.profiles)), self.reconciliation)
        finally:
            self.gate.release(lease)

    async def apply(self, name):
        self.lifecycle.signal()
        lease = await self.gate.acquire_waiting()
        try:
            s = await self._status()
            d = next((p for p in self.config.profiles if p.name == name), None)
            if self.reload_error is not None:
                rejection = ProfileErrorCode.CONFIGURATION
                a = ProfileApplyResult(name, ProfileOutcome.REJECTED,
                                       CpuApplicationResult(ProfileComponentState.NOT_REQUESTED, ()),
                                       GpuApplicationResult(ProfileComponentState.NOT_REQUESTED, self.config.gpu_index),
                                       rejection)
            elif d is None:
                a = ProfileApplyResult(name, ProfileOutcome.REJECTED,
                                       CpuApplicationResult(ProfileComponentState.NOT_REQUESTED, ()),
                                       GpuApplicationResult(ProfileComponentState.NOT_REQUESTED, self.config.gpu_index),
                                       ProfileErrorCode.PROFILE_NOT_FOUND)
            else:
                try:
                    self.cpu.validate(d.cpu, s.cpu.value)
                    self.gpu.validate(d.gpu,
                                                                            s.gpu.value)
                    a = await ProfileApplicator(
                        self.cpu, self.gpu).apply(d, s)
                except ValueError as e:
                    code = ProfileErrorCode.UNSUPPORTED_CAPABILITY if 'unsupported' in str(
                        e) else ProfileErrorCode.INVALID_PROFILE
                    a = ProfileApplyResult(name, ProfileOutcome.REJECTED,
                                           CpuApplicationResult(ProfileComponentState.REJECTED, ()),
                                           GpuApplicationResult(ProfileComponentState.REJECTED, self.config.gpu_index),
                                           code)
            fresh = await self._status()
            return a, fresh
        finally:
            self.gate.release(lease);self.lifecycle.signal()

    async def _suspend_attempt(self, lease):
        if self.gate._owner is not lease: raise ValueError('suspend reservation is not owner')
        status = await self._status()
        logind = status.logind
        if logind.availability is not Availability.AVAILABLE: return SuspendResult(
            SuspendOutcome.OPERATIONAL_FAILURE), status, None
        snapshot = logind.value
        if snapshot.can_suspend in (CanSuspend.NO, CanSuspend.UNKNOWN): return SuspendResult(SuspendOutcome.UNAVAILABLE,
                                                                                             snapshot.can_suspend), status, None
        blockers = tuple(SuspendBlocker(x.what, x.mode) for x in snapshot.inhibitors if
                         x.what is InhibitorKind.SLEEP and x.mode is InhibitorMode.BLOCK)
        if blockers: return SuspendResult(SuspendOutcome.BLOCKED, snapshot.can_suspend, blockers), status, None
        attempt = await self.suspend_adapter.commit(snapshot.can_suspend)
        if attempt.commit is not None: return SuspendResult(SuspendOutcome.ACCEPTED,
                                                            snapshot.can_suspend), status, attempt.commit
        outcome = attempt.outcome
        if outcome is SuspendOutcome.AUTHORIZATION_REQUIRED: return SuspendResult(outcome,
                                                                                  CanSuspend.CHALLENGE), status, None
        return SuspendResult(outcome), status, None

    async def _expire_committed_suspend(self, receipt_id, deadline):
        try:
            await asyncio.sleep(max(0, deadline - time.monotonic()))
            self._take_committed_suspend(receipt_id, "expired")
        except asyncio.CancelledError:
            pass

    def _take_committed_suspend(self, receipt_id, disposition):
        entry = self._committed_suspends.pop(receipt_id, None)
        if entry is None: return False
        if entry.watchdog is not asyncio.current_task(): entry.watchdog.cancel()
        entry.commit.release()
        self.gate.release(entry.lease)
        self.lifecycle.signal()
        return True

    async def begin_direct_suspend(self):
        self.lifecycle.signal()
        lease = self.gate.try_acquire_suspend()
        if lease is None:
            status = await self.status()
            return SuspendResult(SuspendOutcome.CONFLICT), status, None
        try:
            result, status, commit = await self._suspend_attempt(lease)
            if commit is None:
                self.gate.release(lease)
                self.lifecycle.signal()
                return result, status, None
            receipt_id = uuid4()
            watchdog = asyncio.create_task(
                self._expire_committed_suspend(receipt_id, commit.safe_cleanup_deadline_monotonic_seconds))
            self._committed_suspends[receipt_id] = _CommittedSuspend(lease, commit, watchdog)
            return result, status, receipt_id
        except Exception:
            self.gate.release(lease)
            self.lifecycle.signal()
            raise

    async def release_committed_suspend(self, receipt_id):
        if not self._take_committed_suspend(receipt_id, "released"): raise ValueError("unknown suspend receipt")

    async def suspend_reserved(self, lease, origin):
        result, status, commit = await self._suspend_attempt(lease)
        if commit is not None: commit.release()
        return result, status

    async def suspend(self):
        result, status, _ = await self.begin_direct_suspend()
        return result, status

    async def close_committed_suspends(self):
        self._closing = True
        for receipt_id in tuple(self._committed_suspends): self._take_committed_suspend(receipt_id, "shutdown")


class BrokerServer:
    def __init__(self, config, collector, peer_uid_reader=get_peer_uid, socket_owner=set_socket_group, runtime=None):
        self.config = config;self.collector = collector;self.peer_uid_reader = peer_uid_reader;self.socket_owner = socket_owner
        self.runtime = runtime or BrokerRuntime(
            config, collector);self.server = None

    async def start(self):
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.config.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.server = await asyncio.start_unix_server(self.serve_connection, path=str(self.config.socket_path))
        self.socket_owner(self.config.socket_path, self.config.api_uid)
        await self.runtime.start_lifecycle()

    async def serve_connection(self, r, w):
        try:
            if self.peer_uid_reader(w) != self.config.api_uid: return
            req = decode_request(await r.readline())
            if req.operation is BrokerOperation.GET_STATUS:
                result = BrokerStatusResult(req.request_id, await self.runtime.status())
            elif req.operation is BrokerOperation.GET_PROFILES:
                x = await self.runtime.profiles()
                result = BrokerProfilesResult(req.request_id, x.names, x.reconciliation)
            elif req.operation is BrokerOperation.APPLY_PROFILE:
                a, s = await self.runtime.apply(req.profile)
                result = BrokerProfileApplyResult(req.request_id, a, s)
            elif req.operation is BrokerOperation.ACQUIRE_LEASE:
                lease, status = await self.runtime.acquire_lease(req.principal_id, req.requested_ttl_seconds)
                result = BrokerLeaseResult(req.request_id, lease, None, status)
            elif req.operation is BrokerOperation.RENEW_LEASE:
                lease, status = await self.runtime.renew_lease(req.principal_id, req.lease_id,
                                                               req.requested_ttl_seconds)
                result = BrokerLeaseResult(req.request_id, lease if lease is not LeaseError.NOT_FOUND else None,
                                           LeaseError.NOT_FOUND if lease is LeaseError.NOT_FOUND else None, status)
            elif req.operation is BrokerOperation.RELEASE_LEASE:
                outcome, status = await self.runtime.release_lease(req.principal_id, req.lease_id)
                result = BrokerLeaseResult(req.request_id, None,
                                           LeaseError.NOT_FOUND if outcome is LeaseError.NOT_FOUND else None, status)
            elif req.operation is BrokerOperation.LIST_LEASES:
                result = BrokerLeasesResult(req.request_id, await self.runtime.list_leases())
            elif req.operation is BrokerOperation.SUSPEND:
                suspend, status, receipt_id = await self.runtime.begin_direct_suspend()
                result = BrokerSuspendResult(req.request_id, suspend, status, receipt_id)
            elif req.operation is BrokerOperation.RELEASE_SUSPEND:
                await self.runtime.release_committed_suspend(req.suspend_receipt_id)
                result = BrokerReleaseSuspendResult(req.request_id)
            else:
                raise ValueError("unsupported broker operation")
            w.write(encode_result(result))
            await w.drain()
        except Exception:
            try:
                w.write(encode_result(BrokerFailure(req.request_id if 'req' in locals() else __import__('uuid').uuid4(),
                                                    BrokerFailureCode.OPERATIONAL)));await w.drain()
            except Exception:
                pass
        finally:
            w.close();await w.wait_closed()

    async def close(self):
        await self.runtime.stop_lifecycle()
        await self.runtime.close_committed_suspends()
        if self.server: self.server.close();await self.server.wait_closed()
        try:
            self.config.socket_path.unlink()
        except FileNotFoundError:
            pass
