import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest

from stocker import db, evidence

APP = Path(__file__).resolve().parents[1] / "app.py"


def label(elements, name):
    return next(e for e in elements if e.label == name)


class ReviewUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = patch.object(db, "DATA_DIR", Path(self.tmp.name))
        self.directory.start()
        self.at = AppTest.from_file(str(APP), default_timeout=30)
        self.at.session_state.nav = "🛒 Stocking list"
        self.at.run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        self.database = next(Path(self.tmp.name).glob("*.db"))

    def tearDown(self):
        self.directory.stop()
        self.tmp.cleanup()

    def test_each_purchase_card_has_real_evidence_and_a_next_step(self):
        text = "\n".join(x.value for x in self.at.markdown)
        self.assertIn("Next step:", text)
        self.assertIn("History span:", text)
        self.assertIn("Sales recorded through:", text)
        self.assertIn("Last recorded stock count:", text)

    def test_pending_review_and_adoption_form(self):
        self.at.session_state.nav = "🧾 Advice review"
        self.at.run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        self.assertTrue(any("Pending / incomplete" == m.value for m in self.at.metric))
        label(self.at.text_input, "Recorded by").set_value("Owner")
        label(self.at.selectbox, "Decision").select("modified")
        label(self.at.checkbox, "I can report how much was actually purchased").check()
        label(self.at.number_input, "Actual quantity purchased").set_value(5)
        label(self.at.button, "Save adoption record").click().run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        text = "\n".join(x.value for x in self.at.markdown)
        self.assertIn("modified", text)
        self.assertIn("Owner-reported quantity purchased: 5", text)

    def test_actual_sales_import_updates_frozen_review(self):
        self.at.session_state.nav = "🧾 Advice review"
        self.at.run()
        identifier = label(self.at.selectbox, "Saved advice").value
        sku = label(self.at.selectbox, "Product to review").value
        token = db._current.set(self.database)
        try:
            snapshot = evidence.get(identifier)
            dates = pd.date_range(snapshot["forecast_start"], snapshot["forecast_end"])
            db.upsert_sales(pd.DataFrame([{"date": d, "sku": sku, "qty": 12, "revenue": 24} for d in dates]))
        finally:
            db._current.reset(token)
        with patch.object(evidence, "today", return_value=dates[-1].date() + pd.Timedelta(days=1)):
            self.at.run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        self.assertTrue(any("84.0" in m.value for m in self.at.metric))
        text = "\n".join(x.value for x in self.at.markdown)
        self.assertIn("Prospective WAPE:", text)


if __name__ == "__main__":
    unittest.main()
