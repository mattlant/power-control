from .models import *


class ProfileReconciler:
    @staticmethod
    def reconcile(definitions, status):
        if status.cpu.availability is not Availability.AVAILABLE or status.gpu.availability is not Availability.AVAILABLE: return ProfileReconciliation(
            ReconciliationState.INDETERMINATE)
        matches = []
        for d in definitions:
            cpu = status.cpu.value
            gpu = status.gpu.value
            if tuple(p.policy for p in cpu.policies) == d.cpu.policy_ids and all(
                p.governor == d.cpu.governor and p.configured_min_khz == d.cpu.min_khz and p.configured_max_khz == d.cpu.max_khz and (
                        d.cpu.epp is None or p.epp == d.cpu.epp) for p in
                cpu.policies) and gpu.power_limit_w == d.gpu.power_limit_w: matches.append(d.name)
        if len(matches) == 1: return ProfileReconciliation(ReconciliationState.MATCHED, matches[0])
        return ProfileReconciliation(ReconciliationState.UNMATCHED)


class ProfileApplicator:
    def __init__(self, cpu, gpu):
        self.cpu = cpu;self.gpu = gpu

    def validate(self, d, status):
        if status.cpu.availability is not Availability.AVAILABLE or status.gpu.availability is not Availability.AVAILABLE: raise ValueError(
            'unsupported capability')
        self.cpu.validate(d.cpu, status.cpu.value)
        self.gpu.validate(d.gpu, status.gpu.value)

    async def apply(self, d, status):
        cpu = await self.cpu.apply(d.cpu, status.cpu.value)
        gpu = await self.gpu.apply(d.gpu, status.gpu.value)
        if cpu.state is ProfileComponentState.APPLIED and gpu.state is ProfileComponentState.APPLIED:
            out = ProfileOutcome.APPLIED
        elif cpu.state is ProfileComponentState.FAILED and gpu.state is ProfileComponentState.FAILED:
            out = ProfileOutcome.FAILED
        else:
            out = ProfileOutcome.PARTIAL
        return ProfileApplyResult(d.name, out, cpu, gpu)
