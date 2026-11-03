"""Local-only approval simulation. Never sends email or modifies a real order state."""
import copy
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from decimal import Decimal

from . import checkout_flow as flow, db


def run(source, recipient):
    recipient = recipient.strip()
    if not re.fullmatch(r'[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+', recipient):
        raise flow.CheckoutError('Enter a valid email address for the simulation.')
    state = flow.load(source)
    if state.get('environment') != 'simulation':
        raise flow.CheckoutError('Switch to Simulation before running a simulation.')
    if state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown'):
        raise flow.CheckoutError('An actual submission already exists. A simulation cannot overwrite it.')
    if state['status'] == 'declined':
        raise flow.CheckoutError('Choose an alternative before approving the simulation.')
    rows = copy.deepcopy(state['rows'])
    if not rows or any(not r.get('unit_cost') or int(r['quantity']) != r['quantity'] or r['quantity'] <= 0 for r in rows):
        raise flow.CheckoutError('The simulation needs valid quantities and reference purchase prices.')
    total = sum(Decimal(str(r['quantity'])) * Decimal(str(r['unit_cost'])) for r in rows)
    if state['budget'] and total > Decimal(str(state['budget'])):
        raise flow.CheckoutError('Estimated product costs exceed your budget. Adjust the plan before simulation approval.')
    simulation_id = 'SIM-' + source[:8].upper() + '-R' + str(state['revision'])
    subject = '[SIMULATION — NOT A PURCHASE ORDER] StockerAI approval'
    lines = '\n'.join(f'- {r["product"]}: {r["quantity"]} original stock units; reference unit cost S${r["unit_cost"]:.2f}' for r in rows)
    body = (f'This is a locally generated simulation, not a purchase order.\nSimulation reference: {simulation_id}\nRecipient: {recipient}\n\n'
            + lines + f'\n\nEstimated item cost: S${total:.2f}. '
            + (f'Budget limit: S${state["budget"]:.2f}.' if state['budget'] else 'No budget supplied.')
            + '\n\nReal brands, pack sizes, availability, current prices and delivery charges have not been verified.'
            + '\nNo supplier submission or email delivery has taken place. This reference is not a merchant order number.')
    result = {'status': 'simulation_complete', 'simulation_id': simulation_id, 'source': source,
              'revision': state['revision'], 'recipient': recipient, 'subject': subject, 'body': body,
              'reference_total': str(total), 'created_at': datetime.now(timezone.utc).isoformat(),
              'real_order_placed': False, 'email_sent': False}
    db.set_setting('checkout_simulation_' + source, result)
    return result


def eml(result):
    message = EmailMessage(policy=SMTP)
    message['To'] = result['recipient']
    message['Subject'] = result['subject']
    message['X-StockerAI-Simulation'] = 'true'
    message.set_content(result['body'])
    return message.as_bytes()
