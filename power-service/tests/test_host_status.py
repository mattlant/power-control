import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from power_service.host_status import InteractiveSessionError, LogindReader, LogindSuspendAdapter, ScopePtyInteractiveSessionReader
from power_service.models import CanSuspend, InhibitorKind, InhibitorMode, InhibitorStatus, InteractiveSessionSnapshot, SuspendOutcome


class Process:
    returncode = 0

    def __init__(self, payload):
        self.payload = payload

    async def communicate(self):
        return json.dumps(self.payload).encode(), b""


class LogindReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_ubuntu_busctl_array_argument_shape(self):
        replies = {
            "CanSuspend": {"type": "s", "data": ["yes"]},
            "ListInhibitors": {
                "type": "a(ssssuu)",
                "data": [[
                    ["shutdown", "Unattended Upgrades Shutdown", "Stop ongoing upgrades or perform upgrades before shutdown", "delay", 0, 1540],
                    ["sleep", "UPower", "Pause device polling", "delay", 0, 1996279],
                    ["sleep", "ModemManager", "ModemManager needs to reset devices", "delay", 0, 1996303],
                ]],
            },
        }

        async def runner(*args, **kwargs):
            return Process(replies[args[-1]])

        status = await LogindReader(runner).read()
        self.assertEqual(status.can_suspend, CanSuspend.YES)
        self.assertEqual(
            status.inhibitors,
            (
                InhibitorStatus(InhibitorKind.SHUTDOWN, InhibitorMode.DELAY),
                InhibitorStatus(InhibitorKind.SLEEP, InhibitorMode.DELAY),
                InhibitorStatus(InhibitorKind.SLEEP, InhibitorMode.DELAY),
            ),
        )

    async def test_normal_reply_commits_and_retains_the_delay_fd(self):
        read_fd, write_fd = os.pipe()
        calls = []
        class Manager:
            async def call_inhibit(self, *args): calls.append(("inhibit", args)); return read_fd
            async def call_suspend(self, interactive): calls.append(("suspend", interactive))
        class Properties:
            async def call_get(self, *_): return SimpleNamespace(value=5_000_000)
        class Bus:
            async def introspect(self, *_): return object()
            def get_proxy_object(self, *_): return self
            def get_interface(self, name): return Manager() if name.endswith("Manager") else Properties()
            def disconnect(self): pass
        async def factory(): return Bus()
        attempt = await LogindSuspendAdapter(bus_factory=factory).commit(CanSuspend.YES)
        self.assertIsNotNone(attempt.commit)
        self.assertEqual(calls[-1], ("suspend", False))
        attempt.commit.release()
        os.close(write_fd)

    async def test_timeout_after_submission_is_ambiguous_not_accepted(self):
        read_fd, write_fd = os.pipe()
        class Manager:
            async def call_inhibit(self, *_): return read_fd
            async def call_suspend(self, *_): await __import__("asyncio").sleep(10)
        class Properties:
            async def call_get(self, *_): return SimpleNamespace(value=5_000_000)
        class Bus:
            async def introspect(self, *_): return object()
            def get_proxy_object(self, *_): return self
            def get_interface(self, name): return Manager() if name.endswith("Manager") else Properties()
            def disconnect(self): pass
        async def factory(): return Bus()
        attempt = await LogindSuspendAdapter(bus_factory=factory, monotonic=lambda: 0).commit(CanSuspend.YES)
        self.assertTrue(attempt.ambiguous_submission)
        self.assertEqual(attempt.outcome, SuspendOutcome.OPERATIONAL_FAILURE)
        os.close(write_fd)


class InteractiveSessionReaderTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def properties(class_name="user", session_type="tty", state="active", scope="session-1.scope"):
        return {"type": "a{sv}", "data": [{
            "Class": {"type": "s", "data": class_name},
            "Type": {"type": "s", "data": session_type},
            "State": {"type": "s", "data": state},
            "Scope": {"type": "s", "data": scope},
        }]}

    def test_parses_target_shaped_get_all_property_map(self):
        reply = {
            "type": "a{sv}",
            "data": [{
                "Id": {"type": "s", "data": "24"},
                "Scope": {"type": "s", "data": "session-24.scope"},
                "Type": {"type": "s", "data": "tty"},
                "Class": {"type": "s", "data": "user"},
                "State": {"type": "s", "data": "active"},
            }],
        }

        properties = ScopePtyInteractiveSessionReader._properties(reply)

        self.assertEqual(properties["Scope"], ("s", "session-24.scope"))
        self.assertEqual(properties["Type"], ("s", "tty"))

    def test_rejects_non_map_get_all_data(self):
        reply = {
            "type": "a{sv}",
            "data": [[
                ["Scope", {"type": "s", "data": "session-24.scope"}],
            ]],
        }

        with self.assertRaises(InteractiveSessionError):
            ScopePtyInteractiveSessionReader._properties(reply)

    async def test_parses_target_shaped_control_group_variant(self):
        calls = []

        async def runner(*args, **kwargs):
            calls.append(args)
            if "GetUnit" in args:
                return Process({"type": "o", "data": ["/org/freedesktop/systemd1/unit/session_2d24_2escope"]})
            return Process({
                "type": "v",
                "data": [{
                    "type": "s",
                    "data": "/user.slice/user-1000.slice/session-24.scope",
                }],
            })

        reader = ScopePtyInteractiveSessionReader(runner, monotonic=lambda: 10)
        control_group = await reader._scope_control_group("session-24.scope", 20)

        self.assertEqual(control_group, ["user.slice", "user-1000.slice", "session-24.scope"])
        self.assertEqual(calls[-1][-7:], (
            "org.freedesktop.systemd1",
            "/org/freedesktop/systemd1/unit/session_2d24_2escope",
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            "org.freedesktop.systemd1.Scope",
            "ControlGroup",
        ))

    @staticmethod
    def _proc_stat(tty_number):
        return f"123 (bash) S 0 0 0 {tty_number} 0\n"

    async def test_discovers_scope_bounded_ptys_and_aggregates_latest_activity(self):
        first_tty = os.makedev(136, 1)
        second_tty = os.makedev(136, 2)
        replies = {
            "ListSessions": {
                "type": "a(susso)",
                "data": [[["c1", 1000, "matt", "seat0", "/session/c1"], ["c2", 1000, "matt", "", "/session/c2"]]],
            },
            "/session/c1": self.properties(scope="session-c1.scope"),
            "/session/c2": self.properties(scope="session-c2.scope"),
            "session-c1.scope": {"type": "o", "data": ["/unit/c1"]},
            "session-c2.scope": {"type": "o", "data": ["/unit/c2"]},
            "/unit/c1": {"type": "v", "data": [{"type": "s", "data": "/user.slice/user-1000.slice/session-1.scope"}]},
            "/unit/c2": {"type": "v", "data": [{"type": "s", "data": "/user.slice/user-1000.slice/session-2.scope"}]},
        }
        calls = []

        async def runner(*args, **kwargs):
            calls.append(args)
            if "ListSessions" in args:
                return Process(replies["ListSessions"])
            if "GetAll" in args:
                return Process(replies[args[args.index("GetAll") - 2]])
            if "GetUnit" in args:
                return Process(replies[args[-1]])
            return Process(replies[args[args.index("Get") - 2]])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for group, pid, tty in (("session-1.scope", 101, first_tty), ("session-2.scope", 102, second_tty)):
                cgroup = root / "cgroup" / "user.slice" / "user-1000.slice" / group
                cgroup.mkdir(parents=True)
                (cgroup / "cgroup.procs").write_text(f"{pid}\n")
                proc = root / "proc" / str(pid)
                proc.mkdir(parents=True)
                (proc / "stat").write_text(self._proc_stat(tty))
            reader = ScopePtyInteractiveSessionReader(
                runner,
                monotonic=lambda: 10,
                wall_time=lambda: 100,
                proc_root=root / "proc",
                cgroup_root=root / "cgroup",
                device_root=root / "dev",
            )
            original_stat = Path.stat

            def device_stat(path, *args, **kwargs):
                if path == root / "dev" / "pts" / "1":
                    return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=first_tty, st_atime=95)
                if path == root / "dev" / "pts" / "2":
                    return SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=second_tty, st_atime=98)
                return original_stat(path, *args, **kwargs)

            with patch.object(Path, "stat", device_stat):
                snapshot = await reader.read()
        self.assertEqual(snapshot.active_count, 2)
        self.assertEqual(snapshot.latest_activity_monotonic_seconds, 8)
        get_calls = [call[-7:] for call in calls if "Get" in call]
        self.assertEqual(
            get_calls,
            [
                (
                    "org.freedesktop.systemd1",
                    "/unit/c1",
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    "ss",
                    "org.freedesktop.systemd1.Scope",
                    "ControlGroup",
                ),
                (
                    "org.freedesktop.systemd1",
                    "/unit/c2",
                    "org.freedesktop.DBus.Properties",
                    "Get",
                    "ss",
                    "org.freedesktop.systemd1.Scope",
                    "ControlGroup",
                ),
            ],
        )

    async def test_excludes_valid_scope_without_a_controlling_pty(self):
        replies = {
            "ListSessions": {"type": "a(susso)", "data": [[["ssh", 1000, "matt", "", "/session/ssh"]]]},
            "/session/ssh": self.properties(),
            "session-1.scope": {"type": "o", "data": ["/unit/ssh"]},
            "/unit/ssh": {"type": "v", "data": [{"type": "s", "data": "/user.slice/user-1000.slice/session-1.scope"}]},
        }
        async def runner(*args, **kwargs):
            if "ListSessions" in args:
                return Process(replies["ListSessions"])
            if "GetAll" in args:
                return Process(replies[args[args.index("GetAll") - 2]])
            if "GetUnit" in args:
                return Process(replies[args[-1]])
            return Process(replies[args[args.index("Get") - 2]])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cgroup = root / "cgroup" / "user.slice" / "user-1000.slice" / "session-1.scope"
            cgroup.mkdir(parents=True)
            (cgroup / "cgroup.procs").write_text("101\n")
            proc = root / "proc" / "101"
            proc.mkdir(parents=True)
            (proc / "stat").write_text(self._proc_stat(0))
            snapshot = await ScopePtyInteractiveSessionReader(runner, monotonic=lambda: 10, wall_time=lambda: 100, proc_root=root / "proc", cgroup_root=root / "cgroup").read()
        self.assertEqual(snapshot, InteractiveSessionSnapshot(None, 0))

    async def test_invalid_scope_or_future_atime_is_fail_safe(self):
        replies = {
            "ListSessions": {"type": "a(susso)", "data": [[["c1", 1000, "matt", "", "/session/c1"]]]},
            "/session/c1": self.properties(scope="bad.scope"),
            "bad.scope": {"type": "o", "data": ["/unit/c1"]},
            "/unit/c1": {"type": "v", "data": [{"type": "s", "data": "relative/path"}]},
        }
        async def runner(*args, **kwargs):
            if "ListSessions" in args:
                return Process(replies["ListSessions"])
            if "GetAll" in args:
                return Process(replies[args[args.index("GetAll") - 2]])
            if "GetUnit" in args:
                return Process(replies[args[-1]])
            return Process(replies[args[args.index("Get") - 2]])

        with self.assertRaises(InteractiveSessionError):
            await ScopePtyInteractiveSessionReader(runner, monotonic=lambda: 10, wall_time=lambda: 100).read()

        with self.assertRaises(InteractiveSessionError):
            ScopePtyInteractiveSessionReader._project_atime(101, 100, 10)

    async def test_wall_clock_offset_change_is_fail_safe(self):
        async def runner(*args, **kwargs):
            return Process({"type": "a(susso)", "data": [[]]})

        wall_samples = iter((100, 103))
        reader = ScopePtyInteractiveSessionReader(
            runner,
            monotonic=lambda: 10,
            wall_time=lambda: next(wall_samples),
        )

        with self.assertRaises(InteractiveSessionError):
            await reader.read()

    def test_projection_normalizes_identical_atime_to_whole_seconds(self):
        first = ScopePtyInteractiveSessionReader._project_atime(100, 101, 201.00001)
        repeated = ScopePtyInteractiveSessionReader._project_atime(100, 101, 201.00091)
        later_activity = ScopePtyInteractiveSessionReader._project_atime(101, 102, 202.00001)

        self.assertEqual(first, 200)
        self.assertEqual(repeated, 200)
        self.assertEqual(later_activity, 201)
