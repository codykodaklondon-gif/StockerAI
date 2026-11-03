import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from stocker import db, engine, evidence


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "shop.db"
        self.token = db._current.set(self.path)
        db.init()
        db.upsert_products(pd.DataFrame([{"sku": "B", "name": "Bread", "category": "Bakery",
            "current_stock": 100, "unit_cost": 2, "unit_price": 3, "lead_time_days": 2}]))
        dates = pd.date_range(end=pd.Timestamp(evidence.today()), periods=60)
        db.upsert_sales(pd.DataFrame([{"date": d, "sku": "B", "qty": 10, "revenue": 30} for d in dates]))
        engine.train_all()

    def tearDown(self):
        db._current.reset(self.token)
        self.tmp.cleanup()

    def snapshot(self):
        evidence.save_current()
        return evidence.snapshots()["id"].iloc[0]

    def fill(self, identifier, qty=12, omit=()):
        snapshot = evidence.get(identifier)
        dates = pd.date_range(snapshot["forecast_start"], snapshot["forecast_end"])
        rows = [{"date": d, "sku": "B", "qty": qty, "revenue": qty * 3}
                for index, d in enumerate(dates) if index not in omit]
        if rows:
            db.upsert_sales(pd.DataFrame(rows))
        return dates[-1].date() + timedelta(days=1)

    def test_unknown_inventory_age_is_not_reported_as_fresh(self):
        facts = evidence.assess_all()["B"]
        self.assertIsNone(facts["stock_age_days"])
        self.assertEqual(facts["label"], "Check stock first")
        self.assertNotIn("confidence", facts)

    def test_old_count_age_and_unchanged_product_edit(self):
        db.record_move("B", "adjust", 100)
        old = (evidence.today() - timedelta(days=8)).isoformat()
        with db.connect() as con:
            con.execute("UPDATE inventory_freshness SET counted_on=? WHERE sku='B'", (old,))
        db.upsert_products(db.products())
        facts = evidence.assess_all()["B"]
        self.assertEqual(facts["stock_age_days"], 8)
        self.assertTrue(any("8 days" in x for x in facts["warnings"]))

    def test_only_physical_count_refreshes_count_age(self):
        db.record_move("B", "adjust", 100)
        db.record_move("B", "in", 5)
        self.assertEqual(evidence.assess_all()["B"]["last_stock_count"], evidence.today().isoformat())
        db.upsert_products(pd.DataFrame([{"sku": "B", "name": "Bread", "current_stock": 130}]))
        self.assertIsNone(evidence.assess_all()["B"]["last_stock_count"])

    def test_snapshot_is_frozen_and_deduplicates_identical_reruns(self):
        identifier = self.snapshot()
        original = evidence.get(identifier)["payload"]
        evidence.save_current()
        self.assertEqual(len(evidence.snapshots()), 1)
        self.fill(identifier)
        engine.train_all()
        evidence.save_current()
        self.assertEqual(len(evidence.snapshots()), 2)
        self.assertEqual(evidence.get(identifier)["payload"], original)

    def test_forecast_evaluation_starts_after_capture_and_training_data(self):
        identifier = self.snapshot()
        snapshot = evidence.get(identifier)
        self.assertGreater(snapshot["forecast_start"], snapshot["captured_on"])
        self.assertGreater(snapshot["forecast_start"], snapshot["payload"]["sales_cutoff"])
        self.assertEqual(len(snapshot["payload"]["items"][0]["forecast"]), 7)

    def test_future_uploaded_actuals_do_not_get_scored_early(self):
        identifier = self.snapshot()
        self.fill(identifier)
        result = evidence.report(identifier, "B")
        self.assertFalse(result["complete"])
        self.assertIsNone(result["actual_total"])
        self.assertEqual(result["observed_days"], 0)

    def test_completed_daily_wape_and_baseline_are_actual_calculations(self):
        identifier = self.snapshot()
        day = self.fill(identifier, 12)
        result = evidence.report(identifier, "B", as_of=day)
        self.assertTrue(result["complete"])
        self.assertAlmostEqual(result["forecast_total"], 70)
        self.assertAlmostEqual(result["actual_total"], 84)
        self.assertAlmostEqual(result["bias_units"], 14)
        self.assertAlmostEqual(result["mae"], 2)
        self.assertAlmostEqual(result["wape"], 14 / 84)
        self.assertAlmostEqual(result["naive_wape"], 14 / 84)
        self.assertNotIn("money_saved", result)

    def test_missing_days_are_unknown_until_owner_confirms_zero(self):
        identifier = self.snapshot()
        day = self.fill(identifier, 12, omit=(2, 5))
        result = evidence.report(identifier, "B", as_of=day)
        self.assertEqual(result["observed_days"], 5)
        self.assertIsNone(result["wape"])
        self.assertFalse(result["complete"])
        evidence.confirm_missing_days_zero(identifier, "B", "Owner", "Checked full file", as_of=day)
        result = evidence.report(identifier, "B", as_of=day)
        self.assertTrue(result["complete"])
        self.assertEqual(result["actual_total"], 60)
        self.assertEqual(sum(r["zero_confirmed"] for r in result["daily"]), 2)

    def test_cannot_confirm_future_sales_as_complete(self):
        identifier = self.snapshot()
        with self.assertRaises(ValueError):
            evidence.confirm_missing_days_zero(identifier, "B", "Owner")

    def test_zero_sales_has_no_fake_zero_percent_error(self):
        identifier = self.snapshot()
        day = self.fill(identifier, 0)
        result = evidence.report(identifier, "B", as_of=day)
        self.assertTrue(result["complete"])
        self.assertEqual(result["actual_total"], 0)
        self.assertIsNone(result["wape"])
        self.assertAlmostEqual(result["mae"], 10)

    def test_adoption_and_receipts_are_separate_and_not_double_counted(self):
        db.record_move("B", "in", 5)
        identifier = self.snapshot()
        db.record_move("B", "in", 2)
        self.assertIsNone(evidence.report(identifier, "B")["feedback"])
        evidence.record_feedback(identifier, "B", "modified", 3, evidence.today(), "Owner")
        result = evidence.report(identifier, "B")
        self.assertEqual(result["feedback"]["purchased_qty"], 3)
        self.assertEqual(result["recorded_receipts"], 2)
        self.assertEqual(db.products()["current_stock"].iloc[0], 107)

    def test_no_stockout_record_is_not_a_negative_observation(self):
        identifier = self.snapshot()
        result = evidence.report(identifier, "B")
        self.assertEqual(result["stockout_reports"], [])
        self.assertEqual(result["zero_stock_observations"], [])
        evidence.record_stockout(identifier, "B", evidence.today(), "Owner", "Shelf was empty")
        result = evidence.report(identifier, "B")
        self.assertEqual(len(result["stockout_reports"]), 1)
        self.assertEqual(result["stockout_reports"][0]["owner"], "Owner")

    def test_followup_count_uses_frozen_demand_not_retrained_denominator(self):
        identifier = self.snapshot()
        db.record_move("B", "adjust", 50)
        result = evidence.report(identifier, "B")
        self.assertAlmostEqual(result["after_cover_days"], 5)
        self.assertAlmostEqual(result["coverage_change"], -5)
        self.assertEqual(result["latest_count"]["source"], "physical_count")

    def test_stock_receipt_is_not_a_physical_count_for_coverage(self):
        identifier = self.snapshot()
        db.record_move("B", "in", 100)
        result = evidence.report(identifier, "B")
        self.assertIsNone(result["after_cover_days"])

    def test_wrong_workspace_cannot_read_snapshot(self):
        identifier = self.snapshot()
        token = db._current.set(Path(self.tmp.name) / "other.db")
        try:
            db.init()
            with self.assertRaises(ValueError):
                evidence.get(identifier)
        finally:
            db._current.reset(token)

    def test_invalid_feedback_and_future_stockout_are_rejected(self):
        identifier = self.snapshot()
        for qty in (-1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                evidence.record_feedback(identifier, "B", "followed", qty, evidence.today(), "Owner")
        with self.assertRaises(ValueError):
            evidence.record_stockout(identifier, "B", evidence.today() + timedelta(days=1), "Owner")

    def test_reset_removes_history_and_inventory_evidence(self):
        identifier = self.snapshot()
        db.reset()
        with self.assertRaises(ValueError):
            evidence.get(identifier)
        self.assertTrue(db.query("SELECT * FROM inventory_freshness").empty)

    def test_review_workspace_survives_the_old_48_hour_cleanup(self):
        self.snapshot()
        aged = evidence.today() - timedelta(days=4)
        past = pd.Timestamp(aged).timestamp()
        os.utime(self.path, (past, past))
        token = db._current.set(Path(self.tmp.name) / "current.db")
        try:
            db.init()
            with patch.object(db, "DATA_DIR", Path(self.tmp.name)):
                db.cleanup_workspaces()
            self.assertTrue(self.path.exists())
        finally:
            db._current.reset(token)


if __name__ == "__main__":
    unittest.main()
