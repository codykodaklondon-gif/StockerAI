"""Run: python -m unittest discover -s tests -v"""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stocker import advisor, batches, db, events, weekly


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = db.DATA_DIR
        db.DATA_DIR = Path(self.tmp.name)
        db.use_workspace("test")
        db.init()
        db.upsert_products(pd.DataFrame([
            {"sku": "A", "name": "Milk", "current_stock": 10, "unit_cost": 2, "unit_price": 3, "shelf_life_days": 10},
            {"sku": "B", "name": "Bread", "unit_cost": 1, "unit_price": 2},
        ]))

    def tearDown(self):
        db.DATA_DIR = self.old_dir
        db.use_workspace("default")
        self.tmp.cleanup()

    def lot(self, code="A1", qty=4, offset=2, **kw):
        return batches.add("A", code, qty, date.today() - timedelta(days=10), date.today() + timedelta(days=offset), **kw)

    def stock(self):
        return float(db.products().set_index("sku").loc["A", "current_stock"])

    def balance(self, bid):
        return float(db.query("SELECT remaining_qty FROM stock_batches WHERE id=?", (bid,)).iloc[0, 0])

    def sales(self, start, days=14, revenue=30):
        dates = pd.date_range(start, periods=days)
        db.upsert_sales(pd.DataFrame({"date": dates, "sku": "A", "qty": 10., "revenue": float(revenue)}))

    def test_existing_stock_does_not_increase_count(self):
        self.lot()
        self.assertEqual(self.stock(), 10)
        self.assertEqual(batches.coverage().set_index("sku").loc["A", "undated_stock"], 6)
        self.assertTrue(db.moves().empty)

    def test_delivery_increases_stock_exactly_once_and_rejects_duplicate(self):
        self.lot(mode="delivery")
        self.assertEqual(self.stock(), 14)
        self.assertEqual(len(db.moves()), 1)
        with self.assertRaises(ValueError):
            self.lot(mode="delivery")
        self.assertEqual(self.stock(), 14)
        self.assertEqual(len(db.moves()), 1)

    def test_capacity_unknown_and_invalid_inputs(self):
        with self.assertRaises(ValueError): self.lot(qty=11)
        for v in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError): self.lot(qty=v)
        with self.assertRaises(ValueError):
            batches.add("B", "B1", 2, date.today(), date.today()+timedelta(days=3))
        with self.assertRaises(ValueError):
            batches.add("A", "A1", 2, date.today(), date.today()-timedelta(days=1))
        self.assertTrue(batches.inventory().empty)
        self.assertEqual(self.stock(), 10)

    def test_fefo_then_undated_and_no_historical_import_deduction(self):
        later = self.lot("LATER", 3, 5)
        sooner = self.lot("SOON", 4, 1)
        db.record_move("A", "out", 5)
        self.assertEqual(self.balance(sooner), 0)
        self.assertEqual(self.balance(later), 2)
        self.assertEqual(self.stock(), 5)
        self.sales("2025-01-01")
        self.sales("2025-01-01")
        self.assertEqual(self.balance(later), 2)
        self.assertEqual(self.stock(), 5)

    def test_expired_sale_is_blocked_and_rolls_back(self):
        expired = self.lot("OLD", 4, -1)
        self.lot("NEW", 4, 2)
        with self.assertRaises(ValueError): batches.reduce(expired, "out", 1)
        with self.assertRaises(ValueError): db.record_move("A", "out", 7)
        self.assertEqual(self.stock(), 10)
        self.assertEqual(self.balance(expired), 4)
        self.assertTrue(db.sales().empty)
        self.assertTrue(db.moves().empty)

    def test_waste_is_not_sales_and_clears_alert(self):
        expired = self.lot("OLD", 4, -1)
        self.assertEqual(advisor.risks_frame().iloc[0]["kind"], "Expired batch")
        batches.reduce(expired, "waste", 4, "Expired")
        self.assertEqual(self.stock(), 6)
        self.assertTrue(db.sales().empty)
        self.assertFalse(any(a["kind"] == "Expired batch" for a in batches.alerts()))
        waste = db.query("SELECT * FROM batch_events WHERE kind='waste'")
        self.assertEqual(float(waste.iloc[0]["qty"]), 4)
        self.assertEqual(float(waste.iloc[0]["unit_cost"]), 2)

    def test_supplier_return_is_separate_from_waste(self):
        bid = self.lot("RETURN", 4, -1)
        batches.reduce(bid, "return", 2, "Supplier accepted return")
        self.assertEqual(self.stock(), 8)
        self.assertEqual(self.balance(bid), 2)
        self.assertTrue(db.sales().empty)
        self.assertTrue(db.query("SELECT * FROM batch_events WHERE kind='waste'").empty)
        self.assertEqual(len(db.query("SELECT * FROM batch_events WHERE kind='return'")), 1)

    def test_count_and_product_editor_reconcile_batch_balances(self):
        first = self.lot("FIRST", 4, 1)
        second = self.lot("SECOND", 4, 4)
        db.record_move("A", "adjust", 9)    # one of the two undated units went missing
        self.assertEqual(self.balance(first), 4)
        db.upsert_products(pd.DataFrame([{"sku": "A", "name": "Milk", "current_stock": 3}]))
        self.assertEqual(self.balance(first), 0)
        self.assertEqual(self.balance(second), 3)
        self.assertTrue(db.sales().empty)
        self.assertEqual(self.stock(), 3)

    def test_date_correction_and_workspace_isolation(self):
        bid = self.lot(offset=0)
        self.assertEqual(batches.alerts()[0]["kind"], "Batch expires today")
        batches.update_dates(bid, "FIXED", date.today()-timedelta(days=1), date.today()+timedelta(days=20))
        self.assertEqual(batches.alerts(), [])
        self.assertEqual(self.stock(), 10)
        db.use_workspace("other"); db.init()
        self.assertTrue(batches.inventory().empty)
        self.assertTrue(db.products().empty)

    def test_reset_removes_batches_with_original_shop_records(self):
        self.lot()
        db.reset()
        self.assertTrue(batches.inventory(include_empty=True).empty)
        self.assertTrue(batches.history().empty)

    def test_existing_schema_upgrade_preserves_data_and_settings(self):
        # A v6 database has these original tables but no batch tables.
        with db.connect() as con:
            con.execute("DROP TABLE batch_events")
            con.execute("DROP TABLE stock_batches")
            con.execute("INSERT INTO settings(key,value) VALUES ('saved', '123')")
        self.sales("2025-01-01")
        original_products, original_sales = db.products(), db.sales()
        db.init(); db.init()
        pd.testing.assert_frame_equal(db.products(), original_products)
        pd.testing.assert_frame_equal(db.sales(), original_sales)
        self.assertEqual(db.get_setting("saved"), 123)
        self.assertTrue(batches.inventory().empty)

    def test_notification_worker_upgrades_legacy_workspace_without_sending(self):
        from stocker import notifier
        with db.connect() as con:
            con.execute("DROP TABLE batch_events")
            con.execute("DROP TABLE stock_batches")
        notifier.save_settings({"enabled": True, "send_time": "00:00", "only_if_needed": False})
        with patch.object(notifier, "channels", return_value=["test"]), \
                patch.object(notifier, "deliver", return_value={"test": None}) as deliver:
            self.assertEqual(notifier.run_due(), 1)
            self.assertEqual(deliver.call_count, 1)
        self.assertTrue(batches.inventory().empty)

    def test_waste_report_uses_event_cost_snapshot(self):
        bid = self.lot()
        batches.reduce(bid, "waste", 2)
        # Put a test event in a completed week, then change the product's current cost.
        with db.connect() as con:
            con.execute("UPDATE batch_events SET ts='2025-01-12 10:00:00' WHERE kind='waste'")
        db.upsert_products(pd.DataFrame([{"sku": "A", "name": "Milk", "unit_cost": 99}]))
        r = weekly.build("2025-01-14")
        self.assertEqual(r["waste_units"], 2)
        self.assertEqual(r["waste_cost"], 4)

    def test_weekly_calendar_boundaries_and_exact_comparison(self):
        self.sales("2025-12-25", 14)
        # Previous 7 days: 210. Selected 7 days: 420. A year boundary is intentional.
        db.upsert_sales(pd.DataFrame({"date": pd.date_range("2026-01-01", periods=7), "sku": "A", "qty": 20., "revenue": 60.}))
        r = weekly.build("2026-01-07")
        self.assertEqual(r["previous"]["start"], pd.Timestamp("2025-12-25"))
        self.assertEqual(r["current"]["revenue"], 420)
        self.assertEqual(r["previous"]["revenue"], 210)
        self.assertEqual(r["change_pct"], 100)
        self.assertIn("not reconstructed inventory", " ".join(r["notes"]))
        self.assertIn("2026-01-07", weekly.markdown(r))

    def test_weekly_missing_dates_are_gaps_not_zero(self):
        self.sales("2025-01-01", 14)
        with db.connect() as con: con.execute("DELETE FROM sales WHERE date='2025-01-12'")
        r = weekly.build("2025-01-14")
        self.assertIsNone(r["change_pct"])
        self.assertEqual(r["current"]["covered_days"], 6)
        day = r["current"]["daily"].set_index("date").loc[pd.Timestamp("2025-01-12")]
        self.assertTrue(pd.isna(day["revenue"]))
        self.assertEqual(day["record"], "No record")
        self.assertEqual(r["current"]["missing_dates"], ["2025-01-12"])

    def test_closed_day_and_zero_previous_revenue(self):
        self.sales("2025-01-01", 14, revenue=0)
        with db.connect() as con: con.execute("DELETE FROM sales WHERE date='2025-01-12'")
        events.add([{"date": "2025-01-12", "name": "Shop closed", "kind": "Closed", "scope": events.ALL, "uplift": None}])
        r = weekly.build("2025-01-14")
        self.assertEqual(r["current"]["covered_days"], 7)
        self.assertEqual(r["current"]["daily"].set_index("date").loc[pd.Timestamp("2025-01-12"), "revenue"], 0)
        self.assertIsNone(r["change_pct"])

    def test_future_sales_and_incomplete_today_are_excluded(self):
        today = pd.Timestamp.today().normalize()
        db.upsert_sales(pd.DataFrame({"date": [today-pd.Timedelta(days=1), today, today+pd.Timedelta(days=1)],
                                     "sku": "A", "qty": [1., 10., 100.], "revenue": [3., 30., 300.]}))
        r = weekly.build()
        self.assertEqual(r["current"]["end"], today-pd.Timedelta(days=1))
        self.assertEqual(r["current"]["revenue"], 3)
        with self.assertRaises(ValueError): weekly.build(today)

    def test_empty_week_and_unknown_cost_do_not_invent_results(self):
        r = weekly.build()
        self.assertEqual(r["current"]["revenue"], 0)
        self.assertEqual(r["current"]["covered_days"], 0)
        self.assertIsNone(r["change_pct"])
        self.assertTrue(r["top_sellers"].empty)
        self.assertIn("No recorded sales", weekly.markdown(r))


if __name__ == "__main__":
    unittest.main()
