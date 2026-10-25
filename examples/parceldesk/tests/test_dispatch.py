from concurrent.futures import ThreadPoolExecutor

from parceldesk.events import process
from parceldesk.store import snapshot
from test_events import WarehouseCase


class TestDispatch(WarehouseCase):
    def test_courier_retry_cannot_deduct_stock_twice(self):
        event = self.event("dispatched", "evt-courier-001")
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(process(self.database, event)["status"], "duplicate")
        self.assertEqual(self.quantity(), 16)
        self.assertEqual(len(snapshot(self.database)["movements"]), 1)

    def test_concurrent_courier_deliveries_have_one_effect(self):
        event = self.event("dispatched", "evt-courier-concurrent", quantity=1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: process(self.database, event), range(10)))
        self.assertEqual(sum(r["status"] == "processed" for r in results), 1)
        self.assertEqual(self.quantity(), 19)

    def test_failed_dispatch_does_not_consume_event_id(self):
        event = self.event("dispatched", "evt-not-yet-in-stock", quantity=22)
        with self.assertRaisesRegex(ValueError, "insufficient stock"):
            process(self.database, event)
        self.assertEqual(snapshot(self.database)["receipts"], 0)
        process(self.database, self.event("received", "evt-replenish", quantity=5))
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(process(self.database, event)["status"], "duplicate")
        self.assertEqual(self.quantity(), 3)

    def test_same_courier_id_with_changed_payload_is_rejected(self):
        process(self.database, self.event("dispatched", "evt-conflict"))
        with self.assertRaisesRegex(ValueError, "different data"):
            process(self.database, self.event("dispatched", "evt-conflict", quantity=2))
        self.assertEqual(self.quantity(), 16)
