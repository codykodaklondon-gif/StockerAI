"""App-level checks: every page renders, the review rules hold, AI exports build real files.

Run from the project folder:  python -m unittest discover -s tests -v
No AI key is needed: the model is replaced by a fixed reply where a test needs one.
"""
import io
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from stocker import agent, db, engine, exports, review, sample  # noqa: E402

PAGES = ["📊 Dashboard", "📒 Weekly", "🥬 Expiry", "📝 Needs", "✅ Review", "🧾 Order list", "🤝 Suppliers",
         "🤖 AI assistant", "📥 Import data", "📤 Export", "🛒 Stocking list", "🚨 Risk monitor", "📈 Forecast",
         "👥 Customer profile", "📦 Inventory", "📅 Events", "🔔 Alerts", "🧾 Advice review", "💳 Pricing", "🛟 Help", "🏡 Welcome"]

FAKE_DRAFT = {
    "overview": "Bread is running out but keeps only 4 days.",
    "items": [{"sku": "BRD-01", "quantity": 400, "reason": "bread runs out"},
              {"sku": "DRK-01", "quantity": 300, "reason": "drinks sell fast"},
              {"sku": "NOT-A-PRODUCT", "quantity": 5, "reason": "invented by the model"}],
    "risks": [{"sku": "BRD-01", "type": "expiry", "evidence": "keeps 4 days", "impact": "waste",
               "suggestion": "order less"}],
    "uncertainties": [],
}


def fresh_workspace() -> str:
    ws = "test" + uuid.uuid4().hex[:10]
    db.use_workspace(ws)
    sample.load_demo()
    engine.train_all()
    return ws


class PagesRender(unittest.TestCase):
    def test_every_page_renders_without_errors(self):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        ws = "test" + uuid.uuid4().hex[:10]
        at.query_params["ws"] = ws
        at.run()
        try:
            for page in PAGES:
                at.session_state["nav"] = page
                at.run()
                self.assertFalse(at.exception, f"{page}: {[e.value for e in at.exception]}")
            sidebar = [b.label for b in at.sidebar.button]
            self.assertEqual(sidebar[:4], ["My shop", "Plan & order", "Ask Stocker AI", "Import & export"])
        finally:
            db.use_workspace(ws)
            db.current().unlink(missing_ok=True)


class ReviewRules(unittest.TestCase):
    def setUp(self):
        self.ws = fresh_workspace()
        self._orig = agent.complete_json
        agent.complete_json = lambda system, user: FAKE_DRAFT

    def tearDown(self):
        agent.complete_json = self._orig
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def test_invented_products_are_dropped_and_draft_starts_pending(self):
        d = review.generate()
        self.assertEqual(d["status"], "pending")
        self.assertNotIn("NOT-A-PRODUCT", [i["sku"] for i in d["items"]])

    def test_nothing_official_before_approval_and_edits_revoke_it(self):
        d = review.generate()
        with self.assertRaises(review.ReviewError):
            review.export_csv(d["id"])                                   # not approved yet
        with self.assertRaises(review.ReviewError):
            review.approve(d["id"], "", "", d["content_hash"])           # no reviewer name
        with self.assertRaises(review.ReviewError):
            review.approve(d["id"], "Ann", "", d["content_hash"])        # risks not handled
        for sku in review.risky_skus(d):
            d = review.set_handling(d["id"], sku, "check", "", "Ann")
        with self.assertRaises(review.ReviewError):
            review.approve(d["id"], "Ann", "", "stale-hash")             # content changed since viewed
        review.approve(d["id"], "Ann", "", d["content_hash"])
        self.assertIn("Official purchase list", review.export_csv(d["id"]))
        d = review.set_quantity(d["id"], "DRK-01", 10, "Ann")            # edit after approval
        self.assertEqual(d["status"], "pending")
        with self.assertRaises(review.ReviewError):
            review.export_csv(d["id"])

    def test_review_never_changes_stock(self):
        before = db.products().set_index("sku")["current_stock"].to_dict()
        d = review.generate()
        for sku in review.risky_skus(d):
            d = review.set_handling(d["id"], sku, "keep", "", "Ann")
        review.approve(d["id"], "Ann", "", d["content_hash"])
        self.assertEqual(db.products().set_index("sku")["current_stock"].to_dict(), before)


class AIExports(unittest.TestCase):
    def setUp(self):
        self.ws = fresh_workspace()

    def tearDown(self):
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def test_csv_excel_word_build_from_app_data(self):
        import docx
        import openpyxl
        import pandas as pd
        csv = exports.get(exports.build("csv", "List", [{"table": "shopping_list"}])["file_id"])["data"]
        self.assertEqual(pd.read_csv(io.BytesIO(csv))["Order max"].tolist(),
                         exports.table("shopping_list")["Order max"].tolist())
        xlsx = exports.get(exports.build("xlsx", "Report", [{"table": "risk_alerts", "text": "note"}])["file_id"])
        self.assertIn("Summary", openpyxl.load_workbook(io.BytesIO(xlsx["data"])).sheetnames)
        word = exports.get(exports.build("docx", "Report", [{"heading": "Alerts", "table": "risk_alerts"}])["file_id"])
        self.assertTrue(docx.Document(io.BytesIO(word["data"])).tables)

    def test_bad_requests_are_reported_to_the_model(self):
        self.assertIn("error", agent.run_tool("export_file", {"format": "pdf", "title": "x",
                                                              "sections": [{"table": "inventory"}]}))


if __name__ == "__main__":
    unittest.main()
