import re
import unittest
from views.planning import NEEDS_EXAMPLES
from views.purchase_modes import AUTO
from stocker import checkout_flow as flow, db
import test_purchase_modes

class EnglishReleaseTests(unittest.TestCase):
    setUp = test_purchase_modes.PurchaseModeTests.setUp
    tearDown = test_purchase_modes.PurchaseModeTests.tearDown

    def test_example_button_fills_english_and_keeps_constraints(self):
        label = 'New owner, S$120, small fridge, school event'
        next(b for b in self.at.button if b.label == label).click().run()
        text = next(t for t in self.at.text_area if t.label == 'Your needs').value
        self.assertEqual(text, NEEDS_EXAMPLES[label])
        self.assertIsNone(re.search(r'[\u4e00-\u9fff]', text))
        for term in ('S$120', 'tomorrow', 'fridge', 'school', 'bread'):
            self.assertIn(term, text)
        self.assertFalse(self.at.exception)

    def test_english_request_preserves_deadline_and_english_ui(self):
        next(c for c in self.at.segmented_control if c.label == 'Purchase workflow').set_value(AUTO).run()
        self.assertFalse(self.at.exception)
        db.use_workspace(self.at.session_state.ws)
        state = flow.start(self.at.session_state.plan_result, self.at.session_state.plan_request, environment='simulation')
        self.assertIsNotNone(state['delivery_deadline'])
        for collection in (self.at.markdown, self.at.info, self.at.warning, self.at.caption):
            for element in collection:
                self.assertIsNone(re.search(r'[\u4e00-\u9fff]', str(element.value)))
        for text in NEEDS_EXAMPLES.values():
            self.assertIsNone(re.search(r'[\u4e00-\u9fff]', text))
