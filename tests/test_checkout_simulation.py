import copy
import unittest
from stocker import checkout_flow as flow, checkout_simulation as simulation
import test_checkout_flow

class SimulationTests(unittest.TestCase):
    setUp = test_checkout_flow.CheckoutTests.setUp
    tearDown = test_checkout_flow.CheckoutTests.tearDown
    def test_local_simulation_never_changes_real_state_or_sends(self):
        state = flow.start(test_checkout_flow.plan(), 'budget S$120', environment='simulation')
        before = copy.deepcopy(flow.load(state['source']))
        result = simulation.run(state['source'], 'test@example.com')
        self.assertFalse(result['email_sent'])
        self.assertFalse(result['real_order_placed'])
        self.assertTrue(result['simulation_id'].startswith('SIM-'))
        self.assertEqual(flow.load(state['source']), before)
        self.assertIn(b'X-StockerAI-Simulation: true', simulation.eml(result))
    def test_invalid_email_is_rejected(self):
        state = flow.start(test_checkout_flow.plan(), 'budget S$120', environment='simulation')
        with self.assertRaises(flow.CheckoutError):
            simulation.run(state['source'], 'bad\nBcc:other@example.com')

class SimulationUITest(unittest.TestCase):
    def test_clicking_agree_only_generates_local_email(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from streamlit.testing.v1 import AppTest
        from stocker import db
        from views.purchase_modes import AUTO
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DATA_DIR', Path(directory)):
            at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'preview.py'), default_timeout=60)
            at.run()
            next(c for c in at.segmented_control if c.label == 'Purchase workflow').set_value(AUTO).run()
            next(t for t in at.text_input if t.label == 'Confirmation email (simulation — not sent)').set_value('test@example.com').run()
            next(b for b in at.button if b.label == 'Approve simulation').click().run()
            self.assertFalse(at.exception, str(at.exception))
            self.assertEqual(at.session_state.last_simulation['status'], 'simulation_complete')
            self.assertFalse(any(e.label == '版本备份' for e in at.expander))
            self.assertEqual(len([b for b in at.button if b.label == 'Approve simulation']), 1)
            self.assertFalse(at.session_state.last_simulation['email_sent'])
            self.assertFalse(at.session_state.last_simulation['real_order_placed'])
