"""Shelf map: locations filled in automatically per shelf (Fridge A, Dry goods B, Household C), A1 and A-1 both accepted.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

from stocker import db, sample  # noqa: E402


class Shelves(unittest.TestCase):
    def setUp(self):
        self.ws = "test" + uuid.uuid4().hex[:10]
        db.use_workspace(self.ws)
        sample.load_demo()

    def tearDown(self):
        db.use_workspace(self.ws)
        db.current().unlink(missing_ok=True)

    def where(self) -> dict:
        return dict(zip(db.products()["sku"], db.products()["location"]))

    def test_each_product_gets_a_place_on_its_shelf(self):
        self.assertEqual(db.fill_locations(), 12)
        loc = self.where()
        self.assertEqual(loc["ICE-01"], "A1")                                   # frozen first in the fridge
        self.assertEqual((loc["MLK-01"], loc["EGG-12"]), ("A2", "A3"))         # then the shortest shelf life
        self.assertEqual(loc["BRD-01"], "B1")                                   # dry goods
        self.assertEqual({loc["DET-01"], loc["TIS-01"]}, {"C1", "C2"})          # household
        self.assertEqual(len(set(loc.values())), 12)
        self.assertEqual(db.fill_locations(), 0)                                # saved once: labels stay put
        self.assertEqual(db.shelf_name("A1"), "Fridge (Shelf A)")
        self.assertEqual(db.shelf_name("D-4"), "Shelf D")

    def test_owner_locations_are_kept_and_skipped(self):
        db.upsert_products(pd.DataFrame([dict(sku="BRD-01", name="White Bread", location="A1")]))
        db.fill_locations()
        loc = self.where()
        self.assertEqual(loc["BRD-01"], "A1")
        self.assertEqual(loc["ICE-01"], "A2")
        db.upsert_products(pd.DataFrame([dict(sku="NEW-01", name="Yoghurt", category="Dairy", shelf_life_days=14)]))
        db.fill_locations()
        self.assertEqual(self.where()["NEW-01"], "A5")                          # a new product takes the next free place

    def test_a1_and_a_dash_1_mean_the_same_place(self):
        for text in ["A1", "a-1", " A-01 ", "A 1"]:
            self.assertEqual(db.parse_location(text), ("A", 1))
        for text in ["", "A-01-02", "back room", "A0"]:
            self.assertIsNone(db.parse_location(text))

    def test_inventory_map_works_without_typing_locations(self):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at.query_params["ws"] = self.ws
        at.session_state["nav"] = "📦 Inventory"
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        shelf = next(s for s in at.selectbox if s.label == "Shelf")
        self.assertEqual(shelf.options, ["Shelf A · Fridge", "Shelf B · Dry goods", "Shelf C · Household"])
        next(b for b in at.button if b.label == "🍨 Ice Cream Tub").click().run()
        self.assertEqual(next(s for s in at.selectbox if s.label == "Product").value, "Ice Cream Tub (ICE-01)")
        shelf = next(s for s in at.selectbox if s.label == "Shelf")
        shelf.set_value("B").run()                                              # 7 dry goods: three rows still
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertTrue(any(b.label == "🍞 White Bread" for b in at.button))
        self.assertEqual(sum(b.label == "Empty position" for b in at.button), 2)

    def test_a_long_shelf_gets_more_rows(self):
        db.upsert_products(pd.DataFrame([dict(sku=f"X{i}", name=f"Can {i}", category="Grocery", location=f"D{i}")
                                         for i in range(1, 12)]))
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
        at.query_params["ws"] = self.ws
        at.session_state["nav"] = "📦 Inventory"
        at.run()
        next(s for s in at.selectbox if s.label == "Shelf").set_value("D").run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertEqual(sum(" Can " in b.label for b in at.button), 11)
        self.assertEqual(sum(b.label == "Empty position" for b in at.button), 1)   # 4 rows of 3


if __name__ == "__main__":
    unittest.main()
