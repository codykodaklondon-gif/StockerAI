import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from stocker import checkout_flow as flow, db


def plan():
    return {'items': pd.DataFrame([
        {'sku': 'D', 'product': 'Drink', 'quantity': 24, 'unit_cost': 1, 'shelf_life': 270, 'priority': 1},
        {'sku': 'B', 'product': 'Bread', 'quantity': 8, 'unit_cost': 2, 'shelf_life': 4, 'priority': 2}]),
        'budget': 120, 'summary': {'storage': 'small fridge', 'events': ['school event']}, 'plan_starts': '2026-11-03'}


def quote():
    return {'channel': 'Website', 'supplier': 'Test supplier', 'recipient': 'Test checkout', 'quote_id': 'test-quote',
        'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), 'currency': 'SGD',
        'lines': [{'sku': sku, 'merchant_sku': 'vendor-' + sku, 'brand': 'A', 'variant': 'Original',
                   'stock_unit': '瓶', 'net_content': 500, 'net_unit': 'ml', 'purchase_unit': '箱',
                   'units_per_pack': count, 'packs': 1, 'pack_price_sgd': price, 'available': True,
                   'url': 'https://supplier.example/item/' + sku} for sku, count, price in [('D', 24, 24), ('B', 8, 16)]],
        'freight_sgd': 5, 'extra_tax_sgd': 0, 'total_sgd': 45, 'delivery': 'tomorrow', 'address': 'Test address',
        'payment_terms': 'invoice', 'storage_check': {'capacity_litres': 20, 'required_litres': 0, 'source': 'test inventory capacity'},
        'delivery_by': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'event_check': {'needed_by': (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(), 'source': 'test school schedule'}}


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.token = db._current.set(Path(self.tmp.name) / 'test.db')
        db.init()
        db.upsert_products(pd.DataFrame([{'sku': 'D', 'name': 'Drink'}, {'sku': 'B', 'name': 'Bread'}]))
        for sku in ('D', 'B'):
            db.set_setting('checkout_product_' + sku, {'brand': 'A', 'variant': 'Original', 'net_content': 500, 'net_unit': 'ml', 'stock_unit': '瓶'})
    def tearDown(self):
        db._current.reset(self.token)
        self.tmp.cleanup()
    def state(self):
        return flow.start(plan(), '新手，预算 S$120，小冰箱，学校活动，不想剩面包。')
    def ready(self):
        self.adapter = Mock()
        self.adapter.prepare.return_value = quote()
        self.adapter.refresh.side_effect = lambda q: copy.deepcopy(q)
        self.patch = patch.dict(flow._CONNECTORS, {'Website': self.adapter})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        return self.state()
    def test_unconnected_never_claims_prepared_quote_or_order(self):
        original = plan()
        before = original['items'].copy(deep=True)
        state = flow.start(original, 'budget S$120')
        pd.testing.assert_frame_equal(original['items'], before)
        self.assertEqual(state['status'], 'needs_connection')
        self.assertIsNone(state['quote'])
        with self.assertRaises(flow.CheckoutError):
            flow.submit(state['source'], '')
    def test_self_checks_brand_packaging_budget_and_storage(self):
        state = self.state()
        self.assertEqual(flow.verify_quote(state, quote()), '45')
        for mutation in ('brand', 'packs', 'freight', 'expired', 'storage'):
            q = quote()
            if mutation == 'brand': q['lines'][0]['brand'] = 'Wrong'
            if mutation == 'packs': q['lines'][0]['packs'] = 24
            if mutation == 'freight': q.update(freight_sgd=100, total_sgd=140)
            if mutation == 'expired': q['expires_at'] = '2020-01-01T00:00:00+00:00'
            if mutation == 'storage': q['storage_check']['required_litres'] = 21
            with self.subTest(mutation=mutation):
                with self.assertRaises(flow.CheckoutError):
                    flow.verify_quote(state, q)
    def test_decline_offers_backups_without_changing_original(self):
        state = self.state()
        original = copy.deepcopy(state['original_rows'])
        declined = flow.reject(state['source'])
        choices = flow.alternatives(declined)
        self.assertTrue(choices)
        self.assertEqual(choices[0]['key'], 'waste')
        revised = flow.choose(state['source'], choices[0]['key'])
        self.assertEqual(revised['original_rows'], original)
        self.assertEqual(revised['revision'], 2)
        self.assertNotEqual(revised['rows'], original)
        self.assertNotEqual(revised['status'], 'ordered')
    def test_confirm_real_receipt_only_and_prevent_duplicate_submit(self):
        state = self.ready()
        self.assertEqual(state['status'], 'ready')
        self.adapter.submit.return_value = 'test-request'
        self.adapter.read_result.return_value = {'request_id': 'test-request', 'quote_digest': flow._hash(state['quote']), 'status': 'order_confirmed', 'order_id': 'test-only-order'}
        sent = flow.submit(state['source'], state['digest'])
        self.assertEqual(sent['status'], 'ordered')
        with self.assertRaises(flow.CheckoutError):
            flow.submit(state['source'], state['digest'])
        self.assertEqual(self.adapter.submit.call_count, 1)
    def test_message_acceptance_is_not_an_order(self):
        state = self.ready()
        self.adapter.submit.return_value = 'msg-test'
        self.adapter.read_result.return_value = {'request_id': 'msg-test', 'quote_digest': flow._hash(state['quote']), 'status': 'accepted_message'}
        self.assertEqual(flow.submit(state['source'], state['digest'])['status'], 'awaiting_supplier')
    def test_ambiguous_result_blocks_retry_and_ordered_status(self):
        state = self.ready()
        self.adapter.submit.side_effect = TimeoutError
        self.assertEqual(flow.submit(state['source'], state['digest'])['status'], 'unknown')
        with self.assertRaises(flow.CheckoutError):
            flow.submit(state['source'], state['digest'])
        self.assertEqual(self.adapter.submit.call_count, 1)
    def test_live_price_change_requires_confirmation_again(self):
        state = self.ready()
        self.adapter.refresh.return_value = {**state['quote'], 'total_sgd': 46}
        self.adapter.refresh.side_effect = None
        with self.assertRaises(flow.CheckoutError):
            flow.submit(state['source'], state['digest'])
        self.adapter.submit.assert_not_called()
