"""Photo import and per-product forecast methods.

Run from the project folder:  python -m unittest discover -s tests -v
No AI key is needed: the model's reading of the photo is replaced by a fixed reply.
"""
import io
import os
import sys
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from stocker import agent, db, engine, orders, sample, scan  # noqa: E402

YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
FAKE = {
    "sales": {"document_type": "sales", "date": YESTERDAY, "party": "", "notes": "",
              "items": [{"name": "White Bread", "qty": 3, "unit_price": 2.8, "category": "Bakery", "sku": "BRD-01"},
                        {"name": "Kaya Toast Set", "qty": 2, "unit_price": "S$4.50", "category": "", "sku": None},
                        {"name": "Smudged line", "qty": None, "unit_price": 1, "category": "Snacks", "unclear": True},
                        {"name": "", "qty": 9}]},
    "purchase": {"document_type": "purchase", "date": YESTERDAY, "party": "Ban Seng Trading", "notes": "",
                 "items": [{"name": "fresh milk 1l", "qty": 24, "unit_price": 2.2, "line_total": 52.8, "category": "Dairy"},
                           {"name": "Oat Milk 1L", "qty": 6, "line_total": 21.0, "category": "Dairy"}]},
    "shopping": {"document_type": "shopping", "date": None, "party": "", "notes": "",
                 "items": [{"name": "Eggs (12 pack)", "qty": 10, "category": "Dairy"},
                           {"name": "Bin bags", "qty": 2, "category": "Household"}]},
    "catalogue": {"document_type": "catalogue", "date": "2099-01-01", "party": "", "notes": "",
                  "items": [{"name": "Potato Chips", "unit_price": 2.6, "unit_cost": 1.3, "stock": 40, "category": "Snacks"},
                            {"name": "Milo Tin 1.5kg", "unit_price": 18.9, "category": "Beverages", "stock": 5}]},
}


def photo() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (2400, 1200), "white").save(out, "PNG")
    return out.getvalue()


def fresh_workspace() -> str:
    ws = "test" + uuid.uuid4().hex[:10]
    db.use_workspace(ws)
    sample.load_demo()
    engine.train_all()
    return ws


class PhotoImport(unittest.TestCase):
    def setUp(self):
        self.ws = fresh_workspace()
        self._orig = agent.complete_vision
        self.reply = None
        agent.complete_vision = lambda system, text, images: self.reply

    def tearDown(self):
        agent.complete_vision = self._orig
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def read(self, kind):
        self.reply = FAKE[kind]
        return scan.read([photo()])

    def test_photos_are_shrunk_to_jpeg(self):
        from PIL import Image
        import base64
        data, media = scan.prepare(photo())
        self.assertEqual(media, "image/jpeg")
        self.assertLessEqual(max(Image.open(io.BytesIO(base64.b64decode(data))).size), scan.MAX_SIDE)
        with self.assertRaises(ValueError):
            scan.prepare(b"not an image")

    def test_receipt_lines_are_matched_categorised_and_checked(self):
        res = self.read("sales")
        items = res["items"]
        self.assertEqual(len(items), 3)                                   # the blank line is dropped
        self.assertEqual(items.loc[0, "product"], scan.label("BRD-01", "White Bread"))
        self.assertEqual(items.loc[1, "product"], scan.NEW)
        self.assertEqual(items.loc[1, "unit_price"], 4.5)                 # "S$4.50" parsed
        self.assertEqual(items.loc[1, "category"], "Bakery")              # keyword guess when the AI gives none
        self.assertFalse(items.loc[2, "include"])                         # unclear lines start unticked
        self.assertEqual(str(res["date"]), YESTERDAY)

    def test_receipt_adds_to_the_days_sales_and_creates_products(self):
        res = self.read("sales")
        day = YESTERDAY
        before = db.query("SELECT qty FROM sales WHERE date=? AND sku='BRD-01'", (day,))["qty"].sum()
        done = scan.save("sales", res["items"], res["date"], photos=["abc"])
        after = db.query("SELECT qty FROM sales WHERE date=? AND sku='BRD-01'", (day,))["qty"].sum()
        self.assertEqual(after - before, 3)                               # added, not replaced
        self.assertEqual(done["new_products"], 1)
        new = db.products().set_index("name").loc["Kaya Toast Set"]
        self.assertEqual(new["category"], "Bakery")
        self.assertEqual(scan.already_saved(["abc", "xyz"]), ["abc"])

    def test_invoice_receives_stock_and_updates_costs(self):
        res = self.read("purchase")
        self.assertEqual(res["items"].loc[0, "product"], scan.label("MLK-01", "Fresh Milk 1L"))   # fuzzy match
        self.assertEqual(res["items"].loc[1, "unit_cost"], 3.5)          # line total / qty
        stock = db.products().set_index("sku").loc["MLK-01", "current_stock"]
        scan.save("purchase", res["items"], res["date"], res["party"])
        p = db.products().set_index("sku")
        self.assertEqual(p.loc["MLK-01", "current_stock"], stock + 24)
        self.assertEqual(p.loc["MLK-01", "unit_cost"], 2.2)
        self.assertEqual(p.set_index("name").loc["Oat Milk 1L", "current_stock"], 6)

    def test_shopping_list_goes_to_the_final_order_list(self):
        res = self.read("shopping")
        draft = orders.load(pd.DataFrame(), db.products())[0]
        scan.save("shopping", res["items"], date.today(), draft=draft)
        saved, ok, _ = orders.load(pd.DataFrame(), db.products())
        self.assertTrue(ok)
        lines = saved.set_index("product")
        self.assertEqual(lines.loc["Eggs (12 pack)", "qty"], 10)
        self.assertIn("Bin bags", lines.index)

    def test_product_list_updates_products_and_ignores_future_dates(self):
        res = self.read("catalogue")
        self.assertIsNone(res["date"])
        self.assertEqual(res["items"].loc[1, "category"], "Beverage")     # the shop's own spelling
        scan.save("catalogue", res["items"], date.today())
        p = db.products().set_index("sku")
        self.assertEqual(p.loc["SNK-01", "unit_price"], 2.6)
        self.assertEqual(p.loc["SNK-01", "current_stock"], 40)
        self.assertEqual(p.loc["SNK-01", "unit_cost"], 1.3)                 # cost column of a price list
        self.assertEqual(p.loc["SNK-01", "name"], "Potato Chips")

    def test_missing_quantities_are_refused(self):
        res = self.read("sales")
        items = res["items"].copy()
        items["include"] = True
        with self.assertRaises(ValueError):
            scan.save("sales", items, res["date"])

    def test_import_page_reads_and_saves_a_photo(self):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at.query_params["ws"] = self.ws
        self.reply = FAKE["purchase"]
        at.session_state["nav"] = "📥 Import data"
        at.session_state["scan"] = {**scan.tidy(FAKE["purchase"], db.products()), "photos": ["p1"], "id": "p1"}
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        save = next(b for b in at.button if b.label == "Save to Stocker AI")
        save.click().run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any("Received 30 units" in s.value for s in at.success), [s.value for s in at.success])


class ForecastMethods(unittest.TestCase):
    def test_monthly_cycle_is_found_only_when_there_is_one(self):
        idx = pd.date_range(end=pd.Timestamp.today().normalize(), periods=180)
        payday = np.where(idx.day >= 25, 1.4, 1.0)
        picks = {"flat": 0, "payday": 0}
        for seed in range(10):      # the choice is statistical, so judge it over several shops
            rng = np.random.default_rng(seed)
            flat = pd.Series(rng.poisson(30, len(idx)).astype(float), index=idx)
            cyclic = pd.Series(rng.poisson(30 * payday).astype(float), index=idx)
            picks["flat"] += engine.choose_variant(flat)[0] == "monthly"
            picks["payday"] += engine.choose_variant(cyclic)[0] == "monthly"
        self.assertLessEqual(picks["flat"], 1, picks)
        self.assertGreaterEqual(picks["payday"], 6, picks)
        m = engine.fit(cyclic, "X", "monthly")
        self.assertGreater(m.cycle_mult(idx[idx.day == 28][-1]), 1.1)   # paydays forecast higher
        self.assertLess(m.cycle_mult(idx[idx.day == 10][-1]), 1.0)

    def test_standard_variant_is_unchanged(self):
        idx = pd.date_range(end="2026-10-31", periods=120)
        s = pd.Series(np.random.default_rng(1).poisson(20, 120).astype(float), index=idx)
        m = engine.fit(s, "X")
        self.assertEqual(m.cycle, [0.0, 0.0])
        self.assertEqual(engine.backtest(s), engine.backtest(s, variant="standard"))


if __name__ == "__main__":
    unittest.main()
