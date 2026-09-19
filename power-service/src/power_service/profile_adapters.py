from __future__ import annotations

import asyncio
from pathlib import Path

from .models import (
    CpuApplicationResult,
    CpuPolicyStatus,
    CpuProfileAction,
    CpuStatus,
    GpuApplicationResult,
    GpuProfileAction,
    GpuStatus,
    PolicyApplicationResult,
    ProfileComponentState,
    ProfileErrorCode,
)

NVIDIA_SMI_EXECUTABLE = Path("/usr/bin/nvidia-smi")


class CpuProfileAdapter:
    def __init__(self, timeout, runner=None, sysfs_root=Path("/sys/devices/system/cpu/cpufreq")):
        self.timeout = timeout
        self.runner = runner or asyncio.create_subprocess_exec
        self.root = sysfs_root

    def validate(self, action: CpuProfileAction, status: CpuStatus) -> None:
        policy_ids = tuple(policy.policy for policy in status.policies)
        if policy_ids != action.policy_ids:
            raise ValueError("unsupported policy set")

        for policy in status.policies:
            if action.governor not in policy.available_governors:
                raise ValueError("unsupported CPU governor")
            if not (
                    policy.hardware_min_khz
                    <= action.min_khz
                    <= action.max_khz
                    <= policy.hardware_max_khz
            ):
                raise ValueError("unsupported CPU frequency range")
            if action.epp is not None:
                directory = self._policy_path(policy)
                (directory / "energy_performance_preference").read_text()
                (directory / "energy_performance_available_preferences").read_text()

    def _policy_path(self, policy: CpuPolicyStatus) -> Path:
        return self.root / f"policy{policy.policy}"

    def _write_frequency_bounds(
            self, policy: CpuPolicyStatus, action: CpuProfileAction
    ) -> None:
        directory = self._policy_path(policy)
        current_min = int((directory / "scaling_min_freq").read_text().strip())
        current_max = int((directory / "scaling_max_freq").read_text().strip())

        def write(name: str, value: int) -> None:
            (directory / name).write_text(f"{value}\n")

        if action.min_khz < current_min:
            write("scaling_min_freq", action.min_khz)
            current_min = action.min_khz
        if action.max_khz > current_max:
            write("scaling_max_freq", action.max_khz)
            current_max = action.max_khz
        if action.min_khz != current_min:
            write("scaling_min_freq", action.min_khz)
            current_min = action.min_khz
        if action.max_khz != current_max:
            write("scaling_max_freq", action.max_khz)
            current_max = action.max_khz

    async def _apply_governor_and_frequency(
            self, policy: CpuPolicyStatus, action: CpuProfileAction
    ) -> tuple[str, int, int]:
        deadline = asyncio.get_running_loop().time() + self.timeout
        last_error = RuntimeError("CPU governor/range transition did not settle")

        while True:
            try:
                directory = self._policy_path(policy)
                (directory / "scaling_governor").write_text(action.governor + "\n")
                self._write_frequency_bounds(policy, action)
                governor = (directory / "scaling_governor").read_text().strip()
                configured_min = int(
                    (directory / "scaling_min_freq").read_text().strip()
                )
                configured_max = int(
                    (directory / "scaling_max_freq").read_text().strip()
                )
                if (
                        governor == action.governor
                        and configured_min == action.min_khz
                        and configured_max == action.max_khz
                ):
                    return governor, configured_min, configured_max
                last_error = RuntimeError("CPU post-governor confirmation mismatch")
            except Exception as error:
                last_error = error

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise last_error
            await asyncio.sleep(min(0.01, remaining))

    def _readback_matches(
            self, policy: CpuPolicyStatus, action: CpuProfileAction
    ) -> bool:
        directory = self._policy_path(policy)
        governor = (directory / "scaling_governor").read_text().strip()
        configured_min = int(
            (directory / "scaling_min_freq").read_text().strip()
        )
        configured_max = int(
            (directory / "scaling_max_freq").read_text().strip()
        )
        if governor != action.governor:
            return False
        if configured_min != action.min_khz or configured_max != action.max_khz:
            return False

        if action.epp is None:
            return True
        selected_epp = (
            (directory / "energy_performance_preference").read_text().strip()
        )
        return selected_epp == action.epp

    async def _apply_epp(
            self, policy: CpuPolicyStatus, action: CpuProfileAction
    ) -> ProfileErrorCode | None:
        path = self._policy_path(policy) / "energy_performance_available_preferences"
        deadline = asyncio.get_running_loop().time() + self.timeout
        available = tuple(path.read_text().split())
        requested_available = False
        attempts = 0
        while True:
            if action.epp in available:
                requested_available = True
                epp_path = self._policy_path(policy) / "energy_performance_preference"
                attempts += 1
                epp_path.write_text(action.epp + "\n")
                matches = self._readback_matches(policy, action)
                if matches:
                    return None
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
            available = tuple(path.read_text().split())
        if requested_available:
            return ProfileErrorCode.OPERATIONAL
        return ProfileErrorCode.UNSUPPORTED_CAPABILITY

    async def apply(
            self, action: CpuProfileAction, status: CpuStatus
    ) -> CpuApplicationResult:
        policy_results = []
        for policy in status.policies:
            try:
                governor, configured_min, configured_max = (
                    await self._apply_governor_and_frequency(policy, action)
                )
                if action.epp is not None:
                    epp_error = await self._apply_epp(policy, action)
                    if epp_error is not None:
                        policy_results.append(
                            PolicyApplicationResult(
                                policy.policy,
                                ProfileComponentState.FAILED,
                                epp_error,
                            )
                        )
                        continue
                elif not self._readback_matches(policy, action):
                    raise RuntimeError("CPU post-effect confirmation mismatch")
                policy_results.append(
                    PolicyApplicationResult(policy.policy, ProfileComponentState.APPLIED)
                )
            except TimeoutError:
                policy_results.append(
                    PolicyApplicationResult(
                        policy.policy,
                        ProfileComponentState.FAILED,
                        ProfileErrorCode.TIMEOUT,
                    )
                )
            except PermissionError:
                policy_results.append(
                    PolicyApplicationResult(
                        policy.policy,
                        ProfileComponentState.FAILED,
                        ProfileErrorCode.PERMISSION_DENIED,
                    )
                )
            except Exception:
                policy_results.append(
                    PolicyApplicationResult(
                        policy.policy,
                        ProfileComponentState.FAILED,
                        ProfileErrorCode.OPERATIONAL,
                    )
                )

        if all(
                result.state is ProfileComponentState.APPLIED
                for result in policy_results
        ):
            state = ProfileComponentState.APPLIED
        elif any(
                result.state is ProfileComponentState.APPLIED
                for result in policy_results
        ):
            state = ProfileComponentState.PARTIAL
        else:
            state = ProfileComponentState.FAILED
        return CpuApplicationResult(state, tuple(policy_results))


class NvidiaProfileAdapter:
    def __init__(self, index, timeout, reader, runner=None):
        self.index = index
        self.timeout = timeout
        self.reader = reader
        self.runner = runner or asyncio.create_subprocess_exec

    def validate(self, action: GpuProfileAction, status: GpuStatus) -> None:
        if status.index != self.index or not (
                status.power_limit_min_w
                <= action.power_limit_w
                <= status.power_limit_max_w
        ):
            raise ValueError("unsupported GPU capability")

    async def apply(self, action: GpuProfileAction, status: GpuStatus):
        try:
            process = await self.runner(
                str(NVIDIA_SMI_EXECUTABLE),
                "-i",
                str(self.index),
                "-pl",
                format(action.power_limit_w.normalize(), "f"),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.communicate(), self.timeout)
            except TimeoutError:
                process.kill()
                await process.communicate()
                raise
            if process.returncode:
                raise RuntimeError("nvidia-smi failed")
            current = await self.reader.read()
            if current is None or current.power_limit_w != action.power_limit_w:
                raise RuntimeError("confirmation mismatch")
            return GpuApplicationResult(ProfileComponentState.APPLIED, self.index)
        except TimeoutError:
            return GpuApplicationResult(
                ProfileComponentState.FAILED, self.index, ProfileErrorCode.TIMEOUT
            )
        except PermissionError:
            return GpuApplicationResult(
                ProfileComponentState.FAILED,
                self.index,
                ProfileErrorCode.PERMISSION_DENIED,
            )
        except Exception:
            return GpuApplicationResult(
                ProfileComponentState.FAILED,
                self.index,
                ProfileErrorCode.OPERATIONAL,
            )
