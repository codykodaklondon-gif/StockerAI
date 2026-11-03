"""Plan & order steps connected (needs → review → final list → suppliers), and perishable products.

Run from the project folder:  python -m unittest discover -s tests -v
No AI key is needed: the plan is built from a fixed model reply.
"""
import os
import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from stocker import db, engine, orders, planner, review, sample  # noqa: E402

NEEDS = "Budget S$100 for tomorrow, small fridge."
REPLY = {"summary": {"horizon_days": 2, "budget_sgd": 100, "goals": ["less waste"]},
         "plan": [{"sku": "BRD-01", "quantity": 20, "priority": 1, "reason": "Best seller"},
                  {"sku": "COF-01", "quantity": 3, "priority": 2, "reason": "Running low"},
                  {"sku": "XXX-99", "quantity": 5, "reason": "Not a product"}]}


def plan() -> dict:
    return planner.validate(REPLY, planner.context())


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = "test" + uuid.uuid4().hex[:10]
        db.use_workspace(self.ws)
        sample.load_demo()
        engine.train_all()

    def tearDown(self):
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def approve(self) -> dict:
        d = review.from_plan(plan(), NEEDS)
        for sku in sorted(review.risky_skus(d)):
            review.set_handling(d["id"], sku, "keep")
        d = review.get(d["id"])
        review.approve(d["id"], "Lan", "", d["content_hash"])
        return review.get(d["id"])

    def app(self, page):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at.query_params["ws"] = self.ws
        at.session_state["nav"] = page
        return at


class StepsConnected(Base):
    def test_plan_becomes_a_pending_draft_with_app_checks(self):
        old = review.from_plan(plan(), "first")
        d = review.from_plan(plan(), NEEDS)
        self.assertEqual(d["status"], "pending")
        self.assertEqual({i["sku"]: i["quantity"] for i in d["items"]}, {"BRD-01": 20, "COF-01": 3})
        self.assertEqual(d["summary"]["source"], "plan")
        self.assertIn(NEEDS, d["summary"]["overview"])
        self.assertTrue(all(r["source"] == "App check" for r in d["risks"]))
        self.assertEqual(review.get(old["id"])["status"], "superseded")      # one open draft at a time

    def test_an_empty_plan_cannot_be_sent(self):
        empty = planner.validate({"plan": []}, planner.context())
        with self.assertRaises(review.ReviewError):
            review.from_plan(empty, NEEDS)

    def test_approved_purchase_becomes_the_final_list(self):
        with self.assertRaises(review.ReviewError):                          # not before approval
            orders.from_approved(review.from_plan(plan(), NEEDS)["id"])
        d = self.approve()
        orders.from_approved(d["id"])
        lines, saved, stale = orders.load(pd.DataFrame(), db.products())
        self.assertTrue(saved)
        self.assertEqual(dict(zip(lines["sku"], lines["qty"])), {"BRD-01": 20, "COF-01": 3})
        self.assertEqual(orders.source()["draft_id"], d["id"])
        engine.train_all()                                                  # new analysis: the owner's decision stays
        self.assertFalse(orders.load(pd.DataFrame(), db.products())[2])
        before = orders.source()
        lines.loc[0, "qty"] = 25
        orders.save(lines)                                                  # edits keep the link to the approval
        self.assertEqual(orders.source(), before)
        orders.reset()
        self.assertIsNone(orders.source())

    def test_needs_page_sends_the_plan_to_review(self):
        at = self.app("📝 Needs")
        at.session_state["needs_text"] = NEEDS
        at.session_state["plan_request"] = NEEDS
        at.session_state["plan_result"] = plan()
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        next(b for b in at.button if b.label == "Send this plan for review →").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(at.session_state["nav"], "✅ Review")
        self.assertEqual(review.get()["summary"]["source"], "plan")
        self.assertTrue(any("From step 1" in m.value for m in at.markdown))

    def test_ai_purchase_assistant_keeps_the_steps(self):
        at = self.app("📝 Needs")
        at.session_state["needs_text"] = NEEDS
        at.session_state["plan_request"] = NEEDS
        at.session_state["plan_result"] = plan()
        at.run()
        next(c for c in at.segmented_control if c.label == "Purchase workflow").set_value("AI purchase assistant").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any(b.label == "Confirm real order" and b.disabled for b in at.button))   # nothing connected
        next(r for r in at.radio if r.label == "Order mode").set_value("Simulation").run()
        next(t for t in at.text_input if t.label.startswith("Confirmation email")).set_value("owner@example.com").run()
        next(b for b in at.button if b.label == "Approve simulation").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertFalse(at.session_state["last_simulation"]["real_order_placed"])
        next(b for b in at.button if b.label == "Send this plan for review →").click().run()
        self.assertEqual(at.session_state["nav"], "✅ Review")
        self.assertEqual(review.get()["summary"]["source"], "plan")

    def test_review_page_hands_the_approved_list_to_step_3(self):
        d = self.approve()
        at = self.app("✅ Review").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        next(b for b in at.button if b.label == "Use as my final order list →").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(at.session_state["nav"], "🧾 Order list")
        self.assertEqual(orders.source()["draft_id"], d["id"])
        self.assertTrue(any("comes from purchase" in c.value for c in at.caption))

    def test_order_list_offers_an_approved_purchase(self):
        self.approve()
        at = self.app("🧾 Order list").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        next(b for b in at.button if b.label == "Use approved list").click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIsNotNone(orders.source())


class Perishable(Base):
    def test_automatic_rule_and_owner_override(self):
        p = db.products()
        kind = dict(zip(p["sku"], db.perishable(p)))
        self.assertTrue(kind["BRD-01"] and kind["MLK-01"] and kind["EGG-12"])  # keep a month or less
        self.assertTrue(kind["ICE-01"])                                       # frozen: must be kept cold
        self.assertFalse(kind["COF-01"] or kind["TIS-01"] or kind["RCE-05"])
        db.set_perishable({"COF-01": True, "BRD-01": True})
        stored = dict(db.query("SELECT sku, perishable FROM products").itertuples(index=False))
        self.assertEqual(stored["COF-01"], 1)
        self.assertTrue(pd.isna(stored["BRD-01"]))                            # same as automatic: not stored
        self.assertTrue(dict(zip(p["sku"], db.perishable(db.products())))["COF-01"])
        self.assertIn("perishable", planner.context()["products"][0])

    def test_old_workspace_gets_the_column(self):
        path = db.DATA_DIR / f"test{uuid.uuid4().hex[:10]}.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, unit_cost REAL, "
                    "unit_price REAL, lead_time_days INTEGER, current_stock REAL, min_stock REAL, "
                    "shelf_life_days INTEGER, active INTEGER DEFAULT 1)")
        con.commit()
        con.close()
        try:
            db.init(path)
            cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(products)")}
            self.assertIn("perishable", cols)
        finally:
            path.unlink(missing_ok=True)

    def test_inventory_page_shows_and_filters_the_column(self):
        at = self.app("📦 Inventory").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn("Perishable (4)", str(at.get("button_group")[0].proto))   # bread, milk, eggs, ice cream
        at.session_state["inv_type"] = "Perishable"
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])


if __name__ == "__main__":
    unittest.main()
