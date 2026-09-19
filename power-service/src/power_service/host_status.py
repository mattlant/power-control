from __future__ import annotations
import asyncio
import json
import math
import os
import stat
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from .models import *


class CpuFreqReader:
    def __init__(self, root=Path('/sys/devices/system/cpu/cpufreq')):
        self.root = root

    def read(self):
        policies = []
        for directory in sorted(self.root.glob('policy*'), key=lambda p: int(p.name[6:])):
            read = lambda name: directory.joinpath(name).read_text().strip()
            epp_file = directory / 'energy_performance_preference'
            epps_file = directory / 'energy_performance_available_preferences'
            epp = epps = None
            if epp_file.exists() and epps_file.exists(): epp = read('energy_performance_preference'); epps = tuple(
                read('energy_performance_available_preferences').split())
            policies.append(
                CpuPolicyStatus(int(directory.name[6:]), tuple(int(x) for x in read('related_cpus').split()),
                                read('scaling_driver'), read('scaling_governor'),
                                tuple(read('scaling_available_governors').split()), int(read('cpuinfo_min_freq')),
                                int(read('cpuinfo_max_freq')), int(read('scaling_min_freq')),
                                int(read('scaling_max_freq')), epp, epps))
        if not policies: raise FileNotFoundError('no CPU policies')
        return CpuStatus(tuple(policies))


class NvidiaReader:
    def __init__(self, path: Path | None, index: int, timeout: int = 5, runner=None):
        self.path = path or Path(
            '/usr/bin/nvidia-smi');self.index = index;self.timeout = timeout;self.runner = runner or asyncio.create_subprocess_exec

    async def read(self):
        proc = await self.runner(str(self.path),
                                 f'--query-gpu=index,name,driver_version,power.limit,power.min_limit,power.max_limit',
                                 '--format=csv,noheader,nounits', '-i', str(self.index), stdout=asyncio.subprocess.PIPE,
                                 stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout)
        except TimeoutError:
            proc.kill(); await proc.communicate(); raise
        if proc.returncode: return None
        parts = out.decode().strip().split(',')
        if len(parts) != 6: raise ValueError('malformed nvidia output')
        return GpuStatus(int(parts[0]), parts[1].strip(), parts[2].strip(), *(Decimal(x.strip()) for x in parts[3:]))


class LogindReader:
    def __init__(self, runner=None):
        self.runner = runner or asyncio.create_subprocess_exec

    async def _call(self, method):
        p = await self.runner('busctl', '--system', '--no-pager', '--json=short', '--timeout=5', 'call',
                              'org.freedesktop.login1', '/org/freedesktop/login1', 'org.freedesktop.login1.Manager',
                              method, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), 5)
        if p.returncode: raise RuntimeError('busctl failed')
        return json.loads(out)

    async def read(self):
        can, inhibitors = await self._call('CanSuspend'), await self._call('ListInhibitors')
        if can.get('type') != 's' or not isinstance(can.get('data'), list) or len(can['data']) != 1: raise ValueError(
            'bad CanSuspend')
        items = []
        if inhibitors.get('type') != 'a(ssssuu)' or not isinstance(inhibitors.get('data'), list) or len(
            inhibitors['data']) != 1 or not isinstance(inhibitors['data'][0], list): raise ValueError(
            'bad ListInhibitors')
        for row in inhibitors['data'][0]:
            if not isinstance(row, list) or len(row) != 6: raise ValueError('bad inhibitor')
            if row[0] in InhibitorKind._value2member_map_ and row[3] in InhibitorMode._value2member_map_: items.append(
                InhibitorStatus(InhibitorKind(row[0]), InhibitorMode(row[3])))
        return LogindStatus(CanSuspend(can['data'][0]), tuple(items))


class InteractiveSessionError(RuntimeError):
    """The logind session authority could not produce a trustworthy snapshot."""


class ScopePtyInteractiveSessionReader:
    """Read scoped controlling-PTY activity for eligible logind sessions."""

    _BUSCTL_PREFIX = (
        "busctl",
        "--system",
        "--no-pager",
        "--json=short",
    )

    def __init__(
            self,
            runner=None,
            query_timeout_seconds=5,
            monotonic=None,
            wall_time=None,
            proc_root=Path("/proc"),
            cgroup_root=Path("/sys/fs/cgroup"),
            device_root=Path("/dev"),
    ):
        self.runner = runner or asyncio.create_subprocess_exec
        self.query_timeout_seconds = query_timeout_seconds
        self.monotonic = monotonic or time.monotonic
        self.wall_time = wall_time or time.time
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self.device_root = Path(device_root)

    async def _call(self, arguments, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise InteractiveSessionError("interactive session query timed out")

        timeout_seconds = max(1, math.ceil(remaining))
        command = self._BUSCTL_PREFIX + (f"--timeout={timeout_seconds}", "call") + tuple(arguments)
        try:
            process = await self.runner(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), remaining)
            except asyncio.CancelledError:
                process.kill()
                await process.communicate()
                raise
            except (asyncio.TimeoutError, TimeoutError) as error:
                process.kill()
                await process.communicate()
                raise InteractiveSessionError("interactive session query timed out") from error
        except InteractiveSessionError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise InteractiveSessionError("interactive session query failed") from error

        if getattr(process, "returncode", 1) != 0:
            raise InteractiveSessionError("interactive session query failed")
        try:
            if isinstance(output, bytes):
                output = output.decode()
            value = json.loads(output)
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise InteractiveSessionError("interactive session reply was not JSON") from error
        if not isinstance(value, dict):
            raise InteractiveSessionError("interactive session reply was not an object")
        return value

    @staticmethod
    def _rows(reply):
        if reply.get("type") != "a(susso)":
            raise InteractiveSessionError("invalid ListSessions signature")
        data = reply.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], list):
            raise InteractiveSessionError("invalid ListSessions data")
        rows = []
        for row in data[0]:
            if not isinstance(row, list) or len(row) != 5:
                raise InteractiveSessionError("invalid ListSessions row")
            session_id, uid, user, seat, path = row
            if (
                    not isinstance(session_id, str)
                    or not session_id
                    or not isinstance(uid, int)
                    or isinstance(uid, bool)
                    or not 0 <= uid <= 2 ** 32 - 1
                    or not isinstance(user, str)
                    or not isinstance(seat, str)
                    or not isinstance(path, str)
                    or not path.startswith("/")
                    or ".." in path.split("/")
            ):
                raise InteractiveSessionError("invalid ListSessions row")
            rows.append((session_id, path))
        if len({session_id for session_id, _ in rows}) != len(rows):
            raise InteractiveSessionError("duplicate logind session id")
        return rows

    @staticmethod
    def _variant(value):
        if isinstance(value, dict):
            if set(value) != {"type", "data"} or not isinstance(value["type"], str):
                raise InteractiveSessionError("invalid session property variant")
            return value["type"], value["data"]
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
            return value[0], value[1]
        raise InteractiveSessionError("invalid session property variant")

    @classmethod
    def _properties(cls, reply):
        if reply.get("type") != "a{sv}":
            raise InteractiveSessionError("invalid GetAll signature")
        data = reply.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise InteractiveSessionError("invalid GetAll data")
        properties = {}
        for name, value in data[0].items():
            if not isinstance(name, str) or not name:
                raise InteractiveSessionError("invalid GetAll property")
            if name in properties:
                raise InteractiveSessionError("duplicate session property")
            properties[name] = cls._variant(value)

        required = {"Class", "Type", "State", "Scope"}
        if not required.issubset(properties):
            raise InteractiveSessionError("missing session property")
        for name in ("Class", "Type", "State", "Scope"):
            if properties[name][0] != "s" or not isinstance(properties[name][1], str):
                raise InteractiveSessionError("invalid session property type")
        return properties

    @classmethod
    def _single_value(cls, reply, signature, message):
        if reply.get("type") != signature:
            raise InteractiveSessionError(message)
        data = reply.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise InteractiveSessionError(message)
        return data[0]

    @staticmethod
    def _safe_control_group(value):
        if not isinstance(value, str) or not value.startswith("/"):
            raise InteractiveSessionError("invalid ControlGroup")
        parts = value.split("/")[1:]
        if not parts or any(not part or part in {".", ".."} for part in parts):
            raise InteractiveSessionError("invalid ControlGroup")
        return parts

    async def _scope_control_group(self, scope, deadline):
        allowed_scope_characters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.@:-"
        if (
                not isinstance(scope, str)
                or not scope.endswith(".scope")
                or not scope
                or any(character not in allowed_scope_characters for character in scope)
        ):
            raise InteractiveSessionError("invalid session scope")
        unit_reply = await self._call(
            (
                "org.freedesktop.systemd1",
                "/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager",
                "GetUnit",
                "s",
                scope,
            ),
            deadline,
        )
        unit_path = self._single_value(unit_reply, "o", "invalid systemd unit path")
        if not isinstance(unit_path, str) or not unit_path.startswith("/"):
            raise InteractiveSessionError("invalid systemd unit path")
        group_reply = await self._call(
            (
                "org.freedesktop.systemd1",
                unit_path,
                "org.freedesktop.DBus.Properties",
                "Get",
                "ss",
                "org.freedesktop.systemd1.Scope",
                "ControlGroup",
            ),
            deadline,
        )
        if group_reply.get("type") != "v":
            raise InteractiveSessionError("invalid ControlGroup reply")
        data = group_reply.get("data")
        if not isinstance(data, list) or len(data) != 1:
            raise InteractiveSessionError("invalid ControlGroup reply")
        signature, value = self._variant(data[0])
        if signature != "s":
            raise InteractiveSessionError("invalid ControlGroup reply")
        return self._safe_control_group(value)

    def _scope_pids(self, control_group, deadline):
        if self.monotonic() >= deadline:
            raise InteractiveSessionError("interactive session query timed out")
        path = self.cgroup_root.joinpath(*control_group, "cgroup.procs")
        try:
            rows = path.read_text().splitlines()
        except OSError as error:
            raise InteractiveSessionError("cannot read session cgroup") from error
        if self.monotonic() >= deadline:
            raise InteractiveSessionError("interactive session query timed out")
        pids = set()
        for row in rows:
            if not row.isdecimal() or row.startswith("0") or int(row) <= 0:
                raise InteractiveSessionError("invalid cgroup PID")
            pids.add(int(row))
        return pids

    def _tty_device_for_pid(self, pid):
        proc_stat = self.proc_root / str(pid) / "stat"
        try:
            record = proc_stat.read_text()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise InteractiveSessionError("cannot read process stat") from error
        closing = record.rfind(")")
        fields = record[closing + 2:].split() if closing >= 0 else []
        if len(fields) < 5:
            raise InteractiveSessionError("invalid process stat")
        try:
            tty_number = int(fields[4])
        except ValueError as error:
            raise InteractiveSessionError("invalid process tty") from error
        if tty_number == 0:
            return None
        if tty_number < 0:
            raise InteractiveSessionError("invalid process tty")
        major = os.major(tty_number)
        minor = os.minor(tty_number)
        if 136 <= major <= 143:
            device = self.device_root / "pts" / str(minor)
        elif major == 4 and 1 <= minor <= 63:
            device = self.device_root / f"tty{minor}"
        else:
            raise InteractiveSessionError("forbidden controlling tty")
        try:
            metadata = device.stat()
        except OSError as error:
            raise InteractiveSessionError("cannot stat controlling tty") from error
        if not stat.S_ISCHR(metadata.st_mode) or metadata.st_rdev != tty_number:
            raise InteractiveSessionError("invalid controlling tty device")
        return device

    @staticmethod
    def _project_atime(atime, wall_end, monotonic_end):
        if not isinstance(atime, (int, float)) or not math.isfinite(atime) or atime <= 0 or atime > wall_end:
            raise InteractiveSessionError("invalid terminal activity timestamp")
        projected = monotonic_end - (wall_end - atime)
        if projected > monotonic_end:
            raise InteractiveSessionError("terminal activity is in the future")
        return math.floor(projected)

    async def read(self):
        deadline = self.monotonic() + self.query_timeout_seconds
        wall_start = self.wall_time()
        monotonic_start = self.monotonic()
        manager_reply = await self._call(
            (
                "org.freedesktop.login1",
                "/org/freedesktop/login1",
                "org.freedesktop.login1.Manager",
                "ListSessions",
            ),
            deadline,
        )
        sessions = self._rows(manager_reply)
        device_atimes = {}
        active_sessions = 0
        for session_id, path in sessions:
            properties_reply = await self._call(
                (
                    "org.freedesktop.login1",
                    path,
                    "org.freedesktop.DBus.Properties",
                    "GetAll",
                    "s",
                    "org.freedesktop.login1.Session",
                ),
                deadline,
            )
            properties = self._properties(properties_reply)
            values = {name: value for name, (_, value) in properties.items()}
            relevant = (
                    values["Class"] == "user"
                    and values["Type"] == "tty"
                    and values["State"] in {"active", "online"}
            )
            if not relevant:
                continue
            scope = values["Scope"]
            if not scope:
                raise InteractiveSessionError("relevant session has no scope")
            control_group = await self._scope_control_group(scope, deadline)
            session_devices = set()
            for pid in self._scope_pids(control_group, deadline):
                device = self._tty_device_for_pid(pid)
                if device is not None:
                    session_devices.add(device)
            if session_devices:
                active_sessions += 1
                for device in session_devices:
                    if device in device_atimes:
                        continue
                    try:
                        device_atimes[device] = device.stat().st_atime
                    except OSError as error:
                        raise InteractiveSessionError("cannot stat terminal activity") from error

        wall_end = self.wall_time()
        monotonic_end = self.monotonic()
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in
                   (wall_start, monotonic_start, wall_end, monotonic_end)):
            raise InteractiveSessionError("invalid clock sample")
        if abs((wall_end - wall_start) - (monotonic_end - monotonic_start)) > 1:
            raise InteractiveSessionError("wall clock changed during snapshot")
        if not device_atimes:
            return InteractiveSessionSnapshot(None, 0)
        timestamps = [
            self._project_atime(atime, wall_end, monotonic_end)
            for atime in device_atimes.values()
        ]
        return InteractiveSessionSnapshot(max(timestamps), active_sessions)


class LogindSuspendCommit:
    """The delay-inhibitor resource held from logind acknowledgement to release."""

    def __init__(self, fd, submission_monotonic_seconds, safe_cleanup_deadline_monotonic_seconds):
        self.fd = fd
        self.submission_monotonic_seconds = submission_monotonic_seconds
        self.safe_cleanup_deadline_monotonic_seconds = safe_cleanup_deadline_monotonic_seconds
        self.released = False

    def release(self):
        if not self.released:
            self.released = True
            os.close(self.fd)


class LogindSuspendAttempt:
    """One pre-commit outcome, or the irreversible committed resource."""

    def __init__(self, commit=None, outcome=None, ambiguous_submission=False):
        if (commit is None) == (outcome is None):
            raise ValueError("attempt requires exactly one result")
        self.commit = commit
        self.outcome = outcome
        self.ambiguous_submission = ambiguous_submission


class LogindSuspendAdapter:
    """Commit Suspend(false) with a retained logind sleep:delay inhibitor."""

    LOGIND_NAME = "org.freedesktop.login1"
    LOGIND_PATH = "/org/freedesktop/login1"
    LOGIND_MANAGER = "org.freedesktop.login1.Manager"
    PROPERTIES = "org.freedesktop.DBus.Properties"
    LOCAL_RESPONSE_HANDOFF_SECONDS = 1
    RELEASE_RECEIPT_SECONDS = 1
    LOGIND_SAFETY_MARGIN_SECONDS = 1
    COMMIT_GUARD_SECONDS = 0.25
    MIN_INHIBIT_DELAY_MAX_SECONDS = 5

    def __init__(self, timeout_seconds=5, monotonic=None, bus_factory=None):
        self.timeout_seconds = timeout_seconds
        self.monotonic = monotonic or time.monotonic
        self.bus_factory = bus_factory

    async def _connect(self):
        if self.bus_factory is not None:
            return await self.bus_factory()
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType
        return await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()

    @staticmethod
    def _delay_microseconds(value):
        # dbus-next exposes a property reply as a Variant in a one-item body.
        if hasattr(value, "value"):
            value = value.value
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("invalid InhibitDelayMaxUSec")
        return value

    async def commit(self, can_suspend):
        bus = None
        fd = None
        submitted = False
        try:
            bus = await asyncio.wait_for(self._connect(), self.timeout_seconds)
            introspection = await asyncio.wait_for(
                bus.introspect(self.LOGIND_NAME, self.LOGIND_PATH), self.timeout_seconds
            )
            proxy = bus.get_proxy_object(self.LOGIND_NAME, self.LOGIND_PATH, introspection)
            manager = proxy.get_interface(self.LOGIND_MANAGER)
            properties = proxy.get_interface(self.PROPERTIES)
            delay_variant = await asyncio.wait_for(
                properties.call_get(self.LOGIND_MANAGER, "InhibitDelayMaxUSec"), self.timeout_seconds
            )
            delay_seconds = self._delay_microseconds(delay_variant) / 1_000_000
            if delay_seconds < self.MIN_INHIBIT_DELAY_MAX_SECONDS:
                return LogindSuspendAttempt(outcome=SuspendOutcome.OPERATIONAL_FAILURE)
            fd = await asyncio.wait_for(
                manager.call_inhibit("sleep", "power-service-broker", "commit direct suspend response", "delay"),
                self.timeout_seconds,
            )
            if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
                raise ValueError("invalid delay inhibitor fd")
            submission = self.monotonic()
            safe_deadline = submission + delay_seconds - self.LOGIND_SAFETY_MARGIN_SECONDS
            acknowledgement_deadline = safe_deadline - (
                    self.LOCAL_RESPONSE_HANDOFF_SECONDS + self.RELEASE_RECEIPT_SECONDS + self.COMMIT_GUARD_SECONDS
            )
            remaining = acknowledgement_deadline - self.monotonic()
            if remaining <= 0:
                return LogindSuspendAttempt(outcome=SuspendOutcome.OPERATIONAL_FAILURE)
            submitted = True
            try:
                await asyncio.wait_for(manager.call_suspend(False), remaining)
            except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError):
                return LogindSuspendAttempt(outcome=SuspendOutcome.OPERATIONAL_FAILURE, ambiguous_submission=True)
            # Observing the normal reply is the irreversible commit event. Do not
            # compare clocks after it: a delivered reply can never become a failure.
            commit = LogindSuspendCommit(fd, submission, safe_deadline)
            fd = None
            return LogindSuspendAttempt(commit=commit)
        except asyncio.CancelledError:
            if submitted:
                return LogindSuspendAttempt(outcome=SuspendOutcome.OPERATIONAL_FAILURE, ambiguous_submission=True)
            raise
        except Exception:
            return LogindSuspendAttempt(
                outcome=SuspendOutcome.AUTHORIZATION_REQUIRED if can_suspend is CanSuspend.CHALLENGE else SuspendOutcome.OPERATIONAL_FAILURE
            )
        finally:
            if fd is not None:
                os.close(fd)
            if bus is not None:
                bus.disconnect()


class HostStatusCollector:
    def __init__(self, cpu, gpu, logind):
        self.cpu = cpu;self.gpu = gpu;self.logind = logind

    async def collect(self):
        async def value(reader, async_method=False):
            try:
                return ComponentStatus(Availability.AVAILABLE, await reader.read() if async_method else reader.read())
            except FileNotFoundError:
                return ComponentStatus(Availability.UNAVAILABLE, error=OperationalErrorCode.NOT_FOUND)
            except PermissionError:
                return ComponentStatus(Availability.UNAVAILABLE, error=OperationalErrorCode.PERMISSION_DENIED)
            except TimeoutError:
                return ComponentStatus(Availability.UNAVAILABLE, error=OperationalErrorCode.TIMEOUT)
            except ValueError:
                return ComponentStatus(Availability.UNAVAILABLE, error=OperationalErrorCode.MALFORMED_OUTPUT)
            except Exception:
                return ComponentStatus(Availability.UNAVAILABLE, error=OperationalErrorCode.OPERATIONAL)

        cpu, gpu, log = await value(self.cpu), await value(self.gpu, True), await value(self.logind, True)
        state = ServiceState.READY if all(
            x.availability is Availability.AVAILABLE for x in (cpu, gpu, log)) else ServiceState.DEGRADED
        return ServiceStatus(datetime.now(timezone.utc), state, cpu, gpu, log)
