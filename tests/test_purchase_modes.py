import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from streamlit.testing.v1 import AppTest
from stocker import db
from views.purchase_modes import AUTO, STEP


class PurchaseModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = patch.object(db, 'DATA_DIR', Path(self.tmp.name))
        self.directory.start()
        self.at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'preview.py'), default_timeout=60)
        self.at.run()
        self.assertFalse(self.at.exception, str(self.at.exception))
    def tearDown(self):
        self.directory.stop()
        self.tmp.cleanup()
    def mode(self):
        return next(c for c in self.at.segmented_control if c.label == 'Purchase workflow')
    def test_default_step_mode_does_not_create_automatic_purchase(self):
        self.assertEqual(self.mode().value, STEP)
        self.assertFalse(any(b.label in ('Confirm real order', 'Approve simulation') for b in self.at.button))
        db.use_workspace(self.at.session_state.ws)
        self.assertTrue(db.query("SELECT * FROM settings WHERE key LIKE 'checkout_addon_%'").empty)
        self.assertTrue(any(b.label == 'Regenerate with these needs' for b in self.at.button))
    def test_switching_modes_keeps_original_items_and_adds_auto_controls_only(self):
        original = self.at.session_state.plan_result['items'].copy(deep=True)
        self.mode().set_value(AUTO).run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        self.assertTrue(any(b.label == 'Approve simulation' and not b.disabled for b in self.at.button))
        self.assertTrue(any(c.label == 'Purchase channel' for c in self.at.segmented_control))
        pd.testing.assert_frame_equal(self.at.session_state.plan_result['items'], original)
        self.mode().set_value(STEP).run()
        self.assertFalse(any(b.label in ('Confirm real order', 'Approve simulation') for b in self.at.button))
        pd.testing.assert_frame_equal(self.at.session_state.plan_result['items'], original)
    def test_changed_needs_block_auto_using_old_plan(self):
        self.mode().set_value(AUTO).run()
        next(t for t in self.at.text_area if t.label == 'Your needs').set_value('改成预算 S$50').run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        self.assertFalse(any(b.label in ('Confirm real order', 'Approve simulation') for b in self.at.button))
