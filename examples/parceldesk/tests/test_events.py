import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from parceldesk.events import process
from parceldesk.store import initialize, snapshot


class WarehouseCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / "warehouse.sqlite"
        initialize(self.database)

    def event(self, kind="received", event_id="evt-001", quantity=4):
        return {"id": event_id, "kind": kind, "sku": "BAG-01", "quantity": quantity}

    def quantity(self):
        return snapshot(self.database)["stock"][0]["quantity"]


class TestReceive(WarehouseCase):
    def test_retry_returns_duplicate_and_changes_stock_once(self):
        event = self.event()
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(process(self.database, event)["status"], "duplicate")
        self.assertEqual(self.quantity(), 24)
        self.assertEqual(snapshot(self.database)["receipts"], 1)
        self.assertEqual(len(snapshot(self.database)["movements"]), 1)

    def test_concurrent_deliveries_have_one_effect(self):
        event = self.event(event_id="evt-concurrent")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: process(self.database, event), range(12)))
        self.assertEqual(sum(r["status"] == "processed" for r in results), 1)
        self.assertEqual(sum(r["status"] == "duplicate" for r in results), 11)
        self.assertEqual(self.quantity(), 24)

    def test_failed_movement_rolls_back_receipt_so_retry_can_succeed(self):
        event = self.event(event_id="evt-recoverable")
        with patch("parceldesk.events.adjust_stock", side_effect=RuntimeError("disk interrupted")):
            with self.assertRaises(RuntimeError):
                process(self.database, event)
        self.assertEqual(snapshot(self.database)["receipts"], 0)
        self.assertEqual(self.quantity(), 20)
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(self.quantity(), 24)

    def test_same_id_with_different_payload_is_rejected(self):
        process(self.database, self.event())
        with self.assertRaisesRegex(ValueError, "different data"):
            process(self.database, self.event(quantity=9))
        self.assertEqual(self.quantity(), 24)

    def test_bad_input_cannot_change_stock(self):
        for value in (0, -1, True, "4", None, 1_000_001):
            with self.subTest(quantity=value), self.assertRaises(ValueError):
                process(self.database, self.event(quantity=value))
        self.assertEqual(self.quantity(), 20)

    def test_unknown_sku_rolls_back_receipt(self):
        event = {**self.event(), "sku": "MISSING"}
        with self.assertRaises(ValueError):
            process(self.database, event)
        self.assertEqual(snapshot(self.database)["receipts"], 0)


if __name__ == "__main__":
    unittest.main()
