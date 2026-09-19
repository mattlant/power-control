"""Bounded, broker-owned activity leases.

Expiry decisions use monotonic time; UTC is only a public projection.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from .models import Lease, LeaseError, LeaseSummary


@dataclass(frozen=True)
class _StoredLease:
    lease_id: str
    principal_id: str
    deadline_monotonic: float
    ttl_seconds: int


class LeaseStore:
    def __init__(self, monotonic_clock, utc_clock, max_ttl_seconds):
        self._monotonic = monotonic_clock
        self._utc = utc_clock
        self._maximum = max_ttl_seconds
        self._leases = {}

    def expire(self):
        now = self._monotonic()
        for lease_id, lease in tuple(self._leases.items()):
            if lease.deadline_monotonic <= now:
                del self._leases[lease_id]

    def _project(self, stored):
        remaining = max(0, stored.deadline_monotonic - self._monotonic())
        return Lease(stored.lease_id, stored.principal_id, stored.ttl_seconds,
                     self._utc() + timedelta(seconds=remaining))

    def acquire(self, principal_id, requested_ttl_seconds):
        self.expire()
        ttl = min(requested_ttl_seconds, self._maximum)
        lease = _StoredLease(str(uuid4()), principal_id, self._monotonic() + ttl, ttl)
        self._leases[lease.lease_id] = lease
        return self._project(lease)

    def renew(self, principal_id, lease_id, requested_ttl_seconds):
        self.expire()
        old = self._leases.get(lease_id)
        if old is None or old.principal_id != principal_id:
            return LeaseError.NOT_FOUND
        ttl = min(requested_ttl_seconds, self._maximum)
        new = _StoredLease(old.lease_id, old.principal_id, self._monotonic() + ttl, ttl)
        self._leases[lease_id] = new
        return self._project(new)

    def release(self, principal_id, lease_id):
        self.expire()
        lease = self._leases.get(lease_id)
        if lease is None or lease.principal_id != principal_id:
            return LeaseError.NOT_FOUND
        del self._leases[lease_id]
        return None

    def summary(self, maximum_principals):
        self.expire()
        principals = sorted({lease.principal_id for lease in self._leases.values()})
        return LeaseSummary(len(self._leases), len(principals), tuple(principals[:maximum_principals]),
                            len(principals) > maximum_principals)

    def list_active(self):
        self.expire()
        return tuple(sorted((self._project(lease) for lease in self._leases.values()),
                            key=lambda lease: (lease.expires_at, lease.lease_id)))

    def clear(self):
        self._leases.clear()
