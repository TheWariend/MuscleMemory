from concurrent.futures import ThreadPoolExecutor

from parceldesk.events import process
from parceldesk.store import snapshot
from test_events import WarehouseCase


class TestReturns(WarehouseCase):
    def test_customer_return_retry_cannot_add_stock_twice(self):
        event = self.event("returned", "evt-return-001", quantity=2)
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(process(self.database, event)["status"], "duplicate")
        self.assertEqual(self.quantity(), 22)
        self.assertEqual(len(snapshot(self.database)["movements"]), 1)

    def test_parallel_return_notifications_have_one_effect(self):
        event = self.event("returned", "evt-return-concurrent", quantity=3)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: process(self.database, event), range(12)))
        self.assertEqual(sum(r["status"] == "processed" for r in results), 1)
        self.assertEqual(self.quantity(), 23)

    def test_return_cannot_reuse_a_receipt_id_for_a_different_operation(self):
        event = self.event("returned", "evt-return-conflict")
        process(self.database, event)
        with self.assertRaisesRegex(ValueError, "different data"):
            process(self.database, {**event, "kind": "received"})
        self.assertEqual(self.quantity(), 24)

    def test_different_ids_represent_two_genuine_returns(self):
        process(self.database, self.event("returned", "evt-return-a", quantity=1))
        process(self.database, self.event("returned", "evt-return-b", quantity=1))
        self.assertEqual(self.quantity(), 22)
        self.assertEqual(snapshot(self.database)["receipts"], 2)

    def test_invalid_kind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown event kind"):
            process(self.database, self.event("not-a-real-event"))
        self.assertEqual(self.quantity(), 20)
