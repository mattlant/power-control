from datetime import datetime, timezone
import unittest

from power_service.leases import LeaseStore
from power_service.models import LeaseError


class LeaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.store = LeaseStore(lambda: self.now, lambda: datetime(2026, 1, 1, tzinfo=timezone.utc), 30)

    def test_cap_aggregate_and_dead_client_expiry(self):
        first = self.store.acquire("one", 100)
        self.store.acquire("two", 10)
        self.assertEqual(30, first.ttl_seconds)
        self.assertEqual(2, self.store.summary(10).active_count)
        self.now = 11
        self.store.expire()
        self.assertEqual(("one",), self.store.summary(10).principal_ids)
        self.now = 31
        self.store.expire()
        self.assertEqual(0, self.store.summary(10).active_count)

    def test_renew_and_release_do_not_disclose_other_principals(self):
        lease = self.store.acquire("one", 5)
        self.assertIs(LeaseError.NOT_FOUND, self.store.renew("two", lease.lease_id, 5))
        self.assertIs(LeaseError.NOT_FOUND, self.store.release("two", lease.lease_id))
        renewed = self.store.renew("one", lease.lease_id, 100)
        self.assertEqual(30, renewed.ttl_seconds)
        self.assertIsNone(self.store.release("one", lease.lease_id))

    def test_list_active_expires_stale_leases_and_orders_remaining_leases(self):
        first = self.store.acquire("one", 20)
        second = self.store.acquire("two", 5)
        self.now = 6
        leases = self.store.list_active()
        self.assertEqual([lease.lease_id for lease in leases], [first.lease_id])
        self.assertEqual(20, leases[0].ttl_seconds)
        self.assertEqual(1, self.store.summary(10).active_count)
