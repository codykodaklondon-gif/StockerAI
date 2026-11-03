"""Product icons on Inventory: picked from the name or category, shown only, never saved.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from stocker import db, engine, sample  # noqa: E402
from stocker.icons import icon  # noqa: E402


class Icons(unittest.TestCase):
    def test_name_first_then_category_then_a_box(self):
        self.assertEqual(icon("White Bread", "Bakery"), "🍞")
        self.assertEqual(icon("Ice Cream Tub", "Frozen"), "🍨")
        self.assertEqual(icon("Teh Bags 100s", "Beverage"), "🍵")
        self.assertEqual(icon("Steak", "Meat"), "🥩")            # no word matched: the category's icon
        self.assertEqual(icon("Mystery", None), "📦")
        self.assertEqual(icon("Instant Coffee"), "☕")

    def test_inventory_shows_icons_and_saves_names_unchanged(self):
        ws = "test" + uuid.uuid4().hex[:10]
        db.use_workspace(ws)
        sample.load_demo()
        try:
            os.chdir(ROOT)
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
            at.query_params["ws"] = ws
            at.session_state["nav"] = "📦 Inventory"
            at.run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
            picker = next(s for s in at.selectbox if s.label == "Product")
            self.assertIn("🍞 White Bread (BRD-01)", picker.options)
            next(s for s in at.selectbox if s.label == "Shelf").set_value("B").run()
            self.assertTrue(any(b.label == "🍞 White Bread" for b in at.button))
            next(b for b in at.button if b.label == "Save product changes").click().run()
            self.assertFalse(at.exception, [e.value for e in at.exception])
            self.assertNotIn("🍞", "".join(db.products()["name"]))
        finally:
            db.use_workspace(ws)
            db.current().unlink(missing_ok=True)

    def test_today_cards_and_alerts_show_icons(self):
        ws = "test" + uuid.uuid4().hex[:10]
        db.use_workspace(ws)
        sample.load_demo()
        engine.train_all()
        try:
            os.chdir(ROOT)
            for page in ("📊 Dashboard", "🚨 Risk monitor"):
                at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
                at.query_params["ws"] = ws
                at.session_state["nav"] = page
                at.run()
                self.assertFalse(at.exception, [e.value for e in at.exception])
                html = " ".join(h.proto.body for h in at.get("html"))
                self.assertNotIn("▧", html)                                   # no more blank squares
                self.assertIn('product-symbol" aria-hidden="true">🥛', html)  # Fresh Milk has alerts and advice
                if page == "🚨 Risk monitor":
                    self.assertIn("🗑️ Expired batch", html)                   # the alert's kind has its own icon
        finally:
            db.use_workspace(ws)
            db.current().unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
