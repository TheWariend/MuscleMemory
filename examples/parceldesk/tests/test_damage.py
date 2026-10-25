from unittest.mock import patch

from parceldesk.events import process
from parceldesk.store import snapshot
from test_events import WarehouseCase


class TestDamage(WarehouseCase):
    def test_retried_damage_report_deducts_stock_once(self):
        event = self.event("damaged", "evt-damaged-001", quantity=3)
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(process(self.database, event)["status"], "duplicate")
        self.assertEqual(self.quantity(), 17)
        self.assertEqual(len(snapshot(self.database)["movements"]), 1)

    def test_damage_failure_does_not_consume_the_event(self):
        event = self.event("damaged", "evt-damage-interrupted", quantity=2)
        with patch("parceldesk.events.adjust_stock", side_effect=RuntimeError("disk interrupted")):
            with self.assertRaises(RuntimeError):
                process(self.database, event)
        self.assertEqual(snapshot(self.database)["receipts"], 0)
        self.assertEqual(process(self.database, event)["status"], "processed")
        self.assertEqual(self.quantity(), 18)

    def test_damage_cannot_create_negative_stock(self):
        with self.assertRaisesRegex(ValueError, "insufficient stock"):
            process(self.database, self.event("damaged", quantity=21))
        self.assertEqual(self.quantity(), 20)
        self.assertEqual(snapshot(self.database)["receipts"], 0)

    def test_reusing_damage_id_for_different_quantity_is_rejected(self):
        process(self.database, self.event("damaged", "evt-damage-conflict", quantity=1))
        with self.assertRaisesRegex(ValueError, "different data"):
            process(self.database, self.event("damaged", "evt-damage-conflict", quantity=2))
        self.assertEqual(self.quantity(), 19)
