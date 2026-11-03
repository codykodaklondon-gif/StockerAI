"""Additive purchase workflow: consumes, never rewrites, the existing planner result.

Real channel connectors must be installed by deployment code. None is bundled:
we cannot claim a live merchant connection, quote or order without one.
"""
import copy
import hashlib
import json
import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from decimal import Decimal
from urllib.parse import urlparse

from . import agent, db, planner, suppliers

CHANNELS = ('Automatic', 'Website', 'WhatsApp', 'Email')
_CONNECTORS = {}


class CheckoutError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _key(source):
    return 'checkout_addon_' + source


def _save(state):
    db.set_setting(_key(state['source']), state)
    return state


def start(plan, request, environment='real'):
    """Snapshot the existing output. No second AI model chooses replacement quantities."""
    if environment not in ('real', 'simulation'):
        raise CheckoutError('Invalid order environment.')
    rows = json.loads(plan['items'].to_json(orient='records'))
    identity = {'demo_only': bool(plan.get('demo_only')), 'environment': environment, 'items': rows, 'request': request, 'budget': plan.get('budget'), 'starts': plan.get('plan_starts')}
    source = _hash(identity)
    previous = db.get_setting(_key(source))
    if previous:
        return previous
    state = {'demo_only': bool(plan.get('demo_only')), 'environment': environment, 'source': source, 'request': request, 'budget': plan.get('budget'),
             'summary': plan.get('summary', {}),
             'delivery_deadline': (datetime.now(ZoneInfo('Asia/Singapore')).date() + timedelta(days=1)).isoformat() if ('明天' in request or 'tomorrow' in request.casefold()) else None, 'rows': rows, 'original_rows': copy.deepcopy(rows),
             'revision': 1, 'channel': 'Automatic', 'status': 'preparing', 'quote': None,
             'receipt': None, 'history': [], 'missing': [], 'digest': ''}
    return prepare(_save(state))


def load(source):
    result = db.get_setting(_key(source))
    if not result:
        raise CheckoutError('The purchase plan could not be found.')
    return result


def _checkpoint(state, event):
    state['history'].append({'event': event, 'revision': state['revision'], 'at': datetime.now(timezone.utc).isoformat(), 'rows': copy.deepcopy(state['rows'])})


def _clean(text):
    return ' '.join(str(text or '').casefold().split())


def _positive(value):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result <= 0:
            raise ValueError
        return result
    except Exception:
        raise CheckoutError('Product quantity, specification or price is incomplete.') from None


def _net(value, unit):
    units = {'ml': ('v', 1), 'l': ('v', 1000), 'g': ('m', 1), 'kg': ('m', 1000), 'mg': ('m', Decimal('.001'))}
    if _clean(unit) not in units:
        raise CheckoutError('The net content unit per item is unclear.')
    dimension, scale = units[_clean(unit)]
    return dimension, _positive(value) * scale


def verify_quote(state, quote):
    """Self-check actual quote against saved inventory identity and customer constraints."""
    if state.get('demo_only'):
        raise CheckoutError('A fixed UI example cannot be used to submit a real purchase. Generate a real plan first.')
    if quote.get('channel') not in CHANNELS[1:] or quote.get('currency') != 'SGD':
        raise CheckoutError('The purchase channel or settlement currency has not been verified.')
    if not quote.get('supplier') or not quote.get('recipient') or not quote.get('quote_id'):
        raise CheckoutError('The supplier, recipient or quote reference is incomplete.')
    try:
        deadline = datetime.fromisoformat(quote['expires_at'])
        if deadline.tzinfo is None or deadline <= datetime.now(timezone.utc):
            raise ValueError
    except Exception:
        raise CheckoutError('The quote expiry has not been verified or the quote has expired.') from None
    expected = {r['sku']: r for r in state['rows'] if r['quantity'] > 0}
    lines = quote.get('lines', [])
    if len(lines) != len(expected) or len({r['sku'] for r in lines}) != len(lines):
        raise CheckoutError('The quote contains missing or duplicate products.')
    products = db.products().set_index('sku')
    total = Decimal('0')
    for line in lines:
        sku = line['sku']
        if sku not in expected or sku not in products.index:
            raise CheckoutError('The product is not in this purchase plan.')
        record = products.loc[sku].to_dict()
        # Optional metadata already saved by a product-data integration; no filling by the buyer here.
        extras = db.get_setting('checkout_product_' + sku, {}) or {}
        record.update(extras)
        for key in ('brand', 'variant', 'stock_unit'):
            if not _clean(record.get(key)) or _clean(record[key]) != _clean(line.get(key)):
                raise CheckoutError('The brand, variant or inventory counting unit is missing or does not match.')
        if _net(record.get('net_content'), record.get('net_unit')) != _net(line.get('net_content'), line.get('net_unit')):
            raise CheckoutError('Net content per item does not match.')
        if not line.get('merchant_sku') or not line.get('purchase_unit'):
            raise CheckoutError('The supplier product code or selling unit is missing.')
        need = _positive(expected[sku]['quantity'])
        count, packs = _positive(line.get('units_per_pack')), _positive(line.get('packs'))
        if any(v != v.to_integral_value() for v in (need, count, packs)) or count * packs != need:
            raise CheckoutError('Pack quantities do not match: bottles cannot be treated as cartons, and quantities cannot be rounded automatically.')
        if line.get('available') is not True:
            raise CheckoutError('Current availability has not been verified.')
        if quote['channel'] == 'Website':
            url = urlparse(line.get('url', ''))
            if url.scheme != 'https' or not url.hostname or url.username or url.password:
                raise CheckoutError('The actual product page has not been verified.')
        total += _positive(line.get('pack_price_sgd')) * packs
    try:
        freight = Decimal(str(quote['freight_sgd']))
        tax = Decimal(str(quote['extra_tax_sgd']))
        if not freight.is_finite() or not tax.is_finite() or min(freight, tax) < 0:
            raise ValueError
        total += freight + tax
        if total != Decimal(str(quote['total_sgd'])):
            raise ValueError
    except Exception:
        raise CheckoutError('The final total including tax and delivery could not be verified.') from None
    if state['budget'] and total > Decimal(str(state['budget'])):
        raise CheckoutError(f'The total including delivery exceeds your S${state["budget"]:g} budget.')
    if not quote.get('delivery') or not quote.get('address') or not quote.get('payment_terms'):
        raise CheckoutError('Delivery timing, address or payment terms have not been verified.')
    # Small fridge cannot be objectively checked without storage data. Unknown must not pass.
    if state['summary'].get('storage'):
        check = quote.get('storage_check') or {}
        try:
            capacity = _positive(check.get('capacity_litres'))
            required = Decimal(str(check.get('required_litres')))
            if not required.is_finite() or required < 0 or required > capacity or not check.get('source'):
                raise ValueError
        except Exception:
            raise CheckoutError('Fridge capacity and required volume have not been verified, or the purchase exceeds available storage.') from None
    if state.get('delivery_deadline'):
        try:
            latest = datetime.fromisoformat(quote['delivery_by'])
            if latest.tzinfo is None or latest.date() > datetime.fromisoformat(state['delivery_deadline']).date():
                raise ValueError
        except Exception:
            raise CheckoutError('The latest arrival time does not meet your requirement for tomorrow.') from None
    if state['summary'].get('events'):
        check = quote.get('event_check') or {}
        try:
            needed = datetime.fromisoformat(check['needed_by'])
            arrival = datetime.fromisoformat(quote['delivery_by'])
            if needed.tzinfo is None or arrival.tzinfo is None or arrival > needed or not check.get('source'):
                raise ValueError
        except Exception:
            raise CheckoutError('The event time and actual arrival time have not been verified.') from None
    return str(total)


def prepare(state):
    state = copy.deepcopy(state)
    state['quote'], state['receipt'], state['digest'] = None, None, ''
    if state.get('environment') == 'simulation':
        state['status'], state['missing'] = 'needs_connection', []
        return _save(state)
    connector = _CONNECTORS.get(state['channel'])
    if not connector and state['channel'] == 'Automatic':
        connector = next(iter(_CONNECTORS.values()), None)
    if not connector:
        state['status'] = 'needs_connection'
        state['missing'] = ['Connect an actual merchant or supplier ordering channel to retrieve current products, packaging and quotes.']
        return _save(state)
    try:
        quote = connector.prepare(copy.deepcopy(state['rows']), {'budget': state['budget'], 'request': state['request'], 'summary': state['summary']})
        verify_quote(state, quote)
        state['quote'], state['status'], state['missing'] = quote, 'ready', []
        state['digest'] = _hash({'quote': quote, 'revision': state['revision'], 'rows': state['rows']})
    except Exception as e:
        state['status'], state['missing'] = 'needs_information', [str(e)]
    return _save(state)


def reject(source):
    state = load(source)
    if state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown'):
        raise CheckoutError('An actual submission already exists. Declining the plan does not cancel that order.')
    _checkpoint(state, 'owner declined; offering alternatives')
    state['status'], state['digest'] = 'declined', ''
    return _save(state)


def alternatives(state):
    rows, result, seen = state['rows'], [], set()
    candidates = [('budget', 'Spend less', 'Reduce quantities; you may need to restock sooner.', lambda r: int(r['quantity']) // 2),
                  ('waste', 'Reduce perishables', 'Reduce short shelf-life products and keep other quantities unchanged.', lambda r: int(r['quantity']) // 2 if r.get('shelf_life') and r['shelf_life'] <= 7 else int(r['quantity'])),
                  ('priority', 'Buy priorities first', 'Keep only the highest-priority products in the current plan.', lambda r: int(r['quantity']) if r.get('priority', 3) == min(x.get('priority', 3) for x in rows) else 0)]
    for key, label, tradeoff, quantity in candidates:
        changed = [{**r, 'quantity': quantity(r)} for r in rows]
        signature = tuple((r['sku'], r['quantity']) for r in changed)
        if not any(r['quantity'] > 0 for r in changed) or signature in seen or all(r['quantity'] == old['quantity'] for r, old in zip(changed, rows)):
            continue
        seen.add(signature)
        result.append({'key': key, 'label': label, 'tradeoff': tradeoff, 'rows': changed,
            'estimate': round(sum(r['quantity'] * (r.get('unit_cost') or 0) for r in changed), 2),
            'cost_incomplete': any(r['quantity'] > 0 and r.get('unit_cost') is None for r in changed)})
    worries = state['request'].lower()
    if any(w in worries for w in ('面包', '过期', '浪费', 'waste', 'bread', 'spoil', 'leftover', 'expiry')):
        result.sort(key=lambda x: x['key'] != 'waste')
    return result


def choose(source, key):
    state = load(source)
    if state['status'] != 'declined':
        raise CheckoutError('Review the alternative suggestions first.')
    choice = next((a for a in alternatives(state) if a['key'] == key), None)
    if not choice:
        raise CheckoutError('The alternative plan has changed.')
    _checkpoint(state, 'owner selected alternative: ' + key)
    state['rows'] = [r for r in choice['rows'] if r['quantity'] > 0]
    state['revision'] += 1
    return prepare(state)


def tweak(source, feedback):
    state = load(source)
    if state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown'):
        raise CheckoutError('A submission already exists. Verify the original order first.')
    revised = planner.generate(state['request'] + '\nCurrent proposed items: ' + json.dumps(state['rows'], ensure_ascii=False) + '\nOwner requested adjustment: ' + feedback + '\nKeep the original budget ceiling and unchanged items. Write all user-facing text in English, regardless of the input language.')
    if state['budget'] and (revised['budget'] is None or revised['budget'] > state['budget']):
        raise CheckoutError('The original budget limit must be preserved. Generate a new plan explicitly to increase it.')
    _checkpoint(state, 'owner requested adjustment: ' + feedback)
    state['rows'] = json.loads(revised['items'].to_json(orient='records'))
    state['revision'] += 1
    return prepare(state)


def submit(source, seen_digest):
    state = load(source)
    if state.get('environment') != 'real':
        raise CheckoutError('Simulation cannot submit a real order.')
    if state['status'] != 'ready' or not seen_digest or state['digest'] != seen_digest:
        raise CheckoutError('The purchase is not ready or its contents have changed. Review it again.')
    quote = state['quote']
    connector = _CONNECTORS.get(quote['channel'])
    if not connector:
        raise CheckoutError('No real ordering channel is connected.')
    verify_quote(state, quote)
    # Must re-read the live checkout; any price, packaging, address or term change invalidates consent.
    if _hash(connector.refresh(quote)) != _hash(quote):
        prepare(state)
        raise CheckoutError('Checkout details have changed. Review and confirm the updated details.')
    # Claim inside a write transaction, preventing duplicate clicks for the same revision.
    with db.connect() as con:
        current = json.loads(con.execute('SELECT value FROM settings WHERE key=?', (_key(source),)).fetchone()[0])
        if current['status'] != 'ready' or current['digest'] != seen_digest:
            raise CheckoutError('This purchase is already being processed or has changed. Duplicate submission is blocked.')
        state['status'] = 'submitting'
        con.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(state, ensure_ascii=False), _key(source)))
    try:
        external_id = connector.submit(quote, idempotency_key=source + ':' + str(state['revision']))
        receipt = connector.read_result(external_id)
        if receipt.get('request_id') != external_id or receipt.get('quote_digest') != _hash(quote):
            raise CheckoutError('The submission receipt does not match this purchase.')
        if receipt.get('status') == 'accepted_message':
            state['status'] = 'awaiting_supplier'
        elif receipt.get('status') in ('order_placed', 'order_confirmed') and receipt.get('order_id'):
            state['status'] = 'ordered'
        else:
            raise CheckoutError('No verifiable confirmation of a successful actual order was received.')
        state['receipt'] = receipt
    except Exception:
        state['status'] = 'unknown'
        state['missing'] = ['The submission result needs verification. It cannot be marked as ordered and will not be retried automatically.']
    return _save(state)


def set_channel(source, channel):
    if channel not in CHANNELS:
        raise CheckoutError('Unsupported purchase channel.')
    state = load(source)
    if state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown'):
        raise CheckoutError('A submission result already exists. Verify the original order first.')
    if channel == state['channel']:
        return state
    _checkpoint(state, 'owner changed channel: ' + channel)
    state['channel'], state['revision'] = channel, state['revision'] + 1
    return prepare(state)
