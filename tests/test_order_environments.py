import copy
import unittest
from stocker import checkout_flow as flow, checkout_simulation
import test_checkout_flow
import test_purchase_modes

class EnvironmentIsolationTests(unittest.TestCase):
    setUp = test_checkout_flow.CheckoutTests.setUp
    tearDown = test_checkout_flow.CheckoutTests.tearDown
    def test_simulation_and_real_order_have_independent_state(self):
        real = flow.start(test_checkout_flow.plan(), 'budget S$120', environment='real')
        sim = flow.start(test_checkout_flow.plan(), 'budget S$120', environment='simulation')
        self.assertNotEqual(real['source'], sim['source'])
        before = copy.deepcopy(flow.load(real['source']))
        checkout_simulation.run(sim['source'], 'test@example.com')
        flow.reject(sim['source'])
        self.assertEqual(flow.load(real['source']), before)
        with self.assertRaises(flow.CheckoutError):
            checkout_simulation.run(real['source'], 'test@example.com')
        with self.assertRaises(flow.CheckoutError):
            flow.submit(sim['source'], 'not-a-real-order')
    def test_fixed_example_is_never_a_real_order(self):
        proposal = test_checkout_flow.plan()
        proposal['demo_only'] = True
        state = flow.start(proposal, 'budget S$120', environment='real')
        with self.assertRaises(flow.CheckoutError):
            flow.verify_quote(state, test_checkout_flow.quote())

class EnvironmentUITests(unittest.TestCase):
    setUp = test_purchase_modes.PurchaseModeTests.setUp
    tearDown = test_purchase_modes.PurchaseModeTests.tearDown
    def test_customer_can_switch_between_real_and_simulation(self):
        from views.purchase_modes import AUTO
        next(c for c in self.at.segmented_control if c.label == 'Purchase workflow').set_value(AUTO).run()
        self.assertTrue(any(b.label == 'Approve simulation' and not b.disabled for b in self.at.button))
        next(r for r in self.at.radio if r.label == 'Order mode').set_value('Real order').run()
        self.assertFalse(self.at.exception, str(self.at.exception))
        self.assertTrue(any(b.label == 'Confirm real order' and b.disabled for b in self.at.button))
        self.assertFalse(any(b.label == 'Approve simulation' for b in self.at.button))
        next(r for r in self.at.radio if r.label == 'Order mode').set_value('Simulation').run()
        self.assertTrue(any(b.label == 'Approve simulation' and not b.disabled for b in self.at.button))
        self.assertFalse(any(e.label == '版本备份' for e in self.at.expander))
