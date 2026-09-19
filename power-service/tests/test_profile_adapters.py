import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from power_service.models import (
    CpuPolicyStatus,
    CpuProfileAction,
    CpuStatus,
    ProfileComponentState,
    ProfileErrorCode,
)
from power_service.profile_adapters import CpuProfileAdapter


def write_policy(
    root,
    policy,
    governor="performance",
    minimum=1,
    maximum=2,
    epp="performance",
    available_epps=("performance",),
):
    directory = root / f"policy{policy}"
    directory.mkdir()
    (directory / "scaling_governor").write_text(governor + "\n")
    (directory / "scaling_min_freq").write_text(f"{minimum}\n")
    (directory / "scaling_max_freq").write_text(f"{maximum}\n")
    (directory / "energy_performance_preference").write_text(epp + "\n")
    (directory / "energy_performance_available_preferences").write_text(
        " ".join(available_epps) + "\n"
    )


def policy_status(policy, cpu):
    return CpuPolicyStatus(
        policy,
        (cpu,),
        "driver",
        "powersave",
        ("powersave", "performance"),
        1,
        3,
        1,
        2,
        "powersave",
        ("powersave", "performance"),
    )


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def run_adapter(self, root, statuses, action):
        result = await CpuProfileAdapter(2, sysfs_root=root).apply(
            action, CpuStatus(tuple(statuses))
        )
        return result

    async def test_cpu_writes_bounded_sysfs_attributes_and_confirms_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, 0)
            action = CpuProfileAction((0,), "performance", 1, 2, "performance")
            result = await self.run_adapter(root, [policy_status(0, 3)], action)
            self.assertEqual(result.state, ProfileComponentState.APPLIED)

    def test_static_validation_checks_epp_files_without_pre_transition_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, 0, available_epps=("performance",))
            action = CpuProfileAction((0,), "powersave", 1, 2, "power")
            adapter = CpuProfileAdapter(2, sysfs_root=root)

            adapter.validate(action, CpuStatus((policy_status(0, 3),)))

    async def test_readback_mismatch_fails_one_policy_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, 0, governor="powersave")
            write_policy(root, 1)
            action = CpuProfileAction((0, 1), "performance", 1, 2, "performance")
            adapter = CpuProfileAdapter(2, sysfs_root=root)
            original_write = adapter._write_frequency_bounds

            def fail_policy_zero(policy, requested_action):
                if policy.policy == 0:
                    raise RuntimeError("simulated write failure")
                original_write(policy, requested_action)

            adapter._write_frequency_bounds = fail_policy_zero
            result = await adapter.apply(
                action, CpuStatus((policy_status(0, 3), policy_status(1, 4)))
            )

            self.assertEqual(result.state, ProfileComponentState.PARTIAL)
            self.assertEqual(result.policies[0].state, ProfileComponentState.FAILED)
            self.assertEqual(result.policies[0].error, ProfileErrorCode.OPERATIONAL)
            self.assertEqual(result.policies[1].state, ProfileComponentState.APPLIED)

    async def test_readback_failure_is_bounded_and_does_not_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, 0)
            write_policy(root, 1)
            (root / "policy0" / "scaling_max_freq").unlink()
            action = CpuProfileAction((0, 1), "performance", 1, 2, "performance")
            result = await self.run_adapter(
                root, [policy_status(0, 3), policy_status(1, 4)], action
            )

            self.assertEqual(result.policies[0].error, ProfileErrorCode.OPERATIONAL)
            self.assertEqual(result.policies[1].state, ProfileComponentState.APPLIED)
            self.assertEqual(
                (root / "policy1" / "scaling_governor").read_text().strip(),
                "performance",
            )

    async def test_governor_transition_uses_post_governor_epp_availability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, 0, governor="performance")
            action = CpuProfileAction((0,), "powersave", 1, 2, "power")

            async def refresh_epps(_delay):
                (root / "policy0" / "energy_performance_available_preferences").write_text(
                    "performance balance_performance power\n"
                )

            with patch(
                "power_service.profile_adapters.asyncio.sleep",
                side_effect=refresh_epps,
            ):
                result = await self.run_adapter(root, [policy_status(0, 3)], action)

            self.assertEqual(result.state, ProfileComponentState.APPLIED)
            self.assertEqual(
                (root / "policy0" / "energy_performance_preference")
                .read_text()
                .strip(),
                "power",
            )

    async def test_epp_write_retries_until_final_readback_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(
                root,
                0,
                governor="performance",
                available_epps=("performance", "balance_performance", "power"),
            )
            action = CpuProfileAction((0,), "powersave", 1, 2, "power")
            adapter = CpuProfileAdapter(2, sysfs_root=root)

            with patch.object(
                adapter,
                "_readback_matches",
                side_effect=(False, True),
            ):
                result = await adapter.apply(
                    action, CpuStatus((policy_status(0, 3),))
                )

            self.assertEqual(result.state, ProfileComponentState.APPLIED)

    async def test_frequency_transition_retries_transient_write_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(root, 0)
            action = CpuProfileAction((0,), "performance", 1, 2, "performance")
            adapter = CpuProfileAdapter(2, sysfs_root=root)
            original_write = adapter._write_frequency_bounds
            attempts = 0

            def fail_once(policy, requested_action):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("simulated transient write failure")
                original_write(policy, requested_action)

            adapter._write_frequency_bounds = fail_once
            result = await adapter.apply(
                action, CpuStatus((policy_status(0, 3),))
            )

            self.assertEqual(result.state, ProfileComponentState.APPLIED)
            self.assertEqual(attempts, 2)

    async def test_post_governor_epp_mismatch_is_unsupported_and_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_policy(
                root, 0, governor="performance", available_epps=("performance",)
            )
            write_policy(
                root,
                1,
                governor="performance",
                available_epps=("performance", "power"),
            )
            action = CpuProfileAction((0, 1), "powersave", 1, 2, "power")

            result = await self.run_adapter(
                root, [policy_status(0, 3), policy_status(1, 4)], action
            )

            self.assertEqual(result.state, ProfileComponentState.PARTIAL)
            self.assertEqual(result.policies[0].state, ProfileComponentState.FAILED)
            self.assertEqual(
                result.policies[0].error,
                ProfileErrorCode.UNSUPPORTED_CAPABILITY,
            )
            self.assertEqual(result.policies[1].state, ProfileComponentState.APPLIED)
            self.assertEqual(
                (root / "policy0" / "energy_performance_preference")
                .read_text()
                .strip(),
                "performance",
            )
