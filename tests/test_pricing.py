"""Pricing page: three demo plans under More, listed only.

Run from the project folder:  python -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402


class PricingPage(unittest.TestCase):
    def test_plans_are_listed_under_more(self):
        os.chdir(ROOT)
        at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120)
        at.session_state["nav"] = "💳 Pricing"
        at.run()
        self.assertFalse(at.exception, [e.value for e in at.exception])
        self.assertIn("Pricing", [b.label for b in at.sidebar.button])
        self.assertEqual([s.value for s in at.subheader], ["Free", "Personal", "Business"])
        text = " ".join(m.value for m in at.markdown)
        for shown in ("S$0", "S$18", "S$30", "Only 5 AI questions a day", "No import from a photo",
                      "No holiday promotions"):
            self.assertIn(shown, text)


if __name__ == "__main__":
    unittest.main()
