"""Append this panel below the existing Suggested purchase; do not replace that page."""
import pandas as pd
import streamlit as st
from stocker import agent, checkout_flow as flow


def panel(plan, request, simulation_mode=False):
    if plan['items'].empty:
        return
    st.divider()
    st.subheader('Review and purchase')
    st.caption('Your original recommendations stay unchanged. Review the purchase before approving.')
    environment = st.radio('Order mode', ['Real order', 'Simulation'], index=1 if simulation_mode else 0, key='order_environment', horizontal=True)
    simulation_mode = environment == 'Simulation'
    state = flow.start(plan, request, environment='simulation' if simulation_mode else 'real')
    source = state['source']
    st.caption('Simulation: no supplier submission or email delivery.' if simulation_mode else 'Real order: use a merchant website or contact your supplier. No order has been placed yet.')
    key = f'checkout_channel_{source}_{state["revision"]}'
    channel = st.segmented_control('Purchase channel', list(flow.CHANNELS), default=state['channel'], key=key,
        disabled=state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown'))
    if channel and channel != state['channel']:
        try:
            state = flow.set_channel(source, channel)
            st.rerun()
        except flow.CheckoutError as e:
            st.error(str(e))
    if state['status'] == 'declined':
        st.write('Would you prefer to spend less, reduce perishables, or buy only the highest-priority items?')
        choices = flow.alternatives(state)
        for choice in choices:
            with st.container(border=True):
                label = {'budget':'Spend less', 'waste':'Reduce perishables', 'priority':'Buy priorities first'}[choice['key']]
                st.markdown('**' + label + '**')
                st.write(', '.join(f'{r["product"]} {r["quantity"]}' for r in choice['rows'] if r['quantity'] > 0))
                st.caption(f'Estimated item cost S${choice["estimate"]:,.2f}' + (' (some costs missing)' if choice['cost_incomplete'] else '') + '. Delivery and the current supplier quote still need checking.')
                st.caption({'budget':'Smaller quantities may require earlier replenishment.', 'waste':'Reduce short-life products; keep the others unchanged.', 'priority':'Defer lower-priority products for this purchase.'}[choice['key']])
                if st.button('Use this option: ' + label, key=f'checkout_alt_{source}_{choice["key"]}'):
                    flow.choose(source, choice['key'])
                    st.rerun()
        if not choices:
            st.info('No safe quantity reduction is available. Tell me whether you want to change the products, budget or timing.')
        st.caption('Your original recommendations are kept. Choosing an alternative does not place an order.')
    else:
        quote = state.get('quote')
        if quote:
            st.write(f'Supplier: **{quote["supplier"]}**')
            st.write('Recipient: ' + quote['recipient'])
            table = pd.DataFrame([{'Product': r['sku'], 'Brand / variant': r['brand'] + ' / ' + r['variant'],
                'Item size': str(r['net_content']) + ' ' + r['net_unit'],
                'Purchase quantity': f'{r["packs"]} {r["purchase_unit"]}',
                'Stock-unit equivalent': f'{r["packs"] * r["units_per_pack"]} {r["stock_unit"]}'} for r in quote['lines']])
            st.dataframe(table, hide_index=True, width='stretch')
            st.metric('Total including tax and delivery', f'S${float(quote["total_sgd"]):,.2f}')
            st.caption(f'Delivery: {quote["delivery"]} · Address: {quote["address"]} · Payment terms: {quote["payment_terms"]}')
            st.success('Checked: brand, variant, item size, pack conversion, availability, budget and delivery constraints.')
        else:
            table = pd.DataFrame(state['rows'])
            st.dataframe(table[['product', 'quantity']].rename(columns={'product': 'Product', 'quantity': 'Quantity (original stock unit)'}), hide_index=True, width='stretch')
            st.info('Simulation only. No real order or email will be sent.' if simulation_mode else 'Automatic checkout is not connected. You can continue on a real merchant website or email your supplier below.')
            if not simulation_mode:
                from views.real_purchase_routes import panel as real_routes
                real_routes(state)
        if simulation_mode:
            st.text_input('Confirmation email (simulation — not sent)', value=st.session_state.get('simulation_default_email', ''), key='simulation_recipient')
        ready = state['status'] == 'ready'
        a, b, c = st.columns(3)
        action_label = 'Approve simulation' if simulation_mode else 'Confirm real order'
        locked = state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown')
        if a.button(action_label, type='primary', disabled=locked or (not simulation_mode and not ready), key=f'checkout_yes_{source}_{state["revision"]}'):
            try:
                if simulation_mode:
                    from stocker import checkout_simulation
                    result = checkout_simulation.run(source, st.session_state.get('simulation_recipient', ''))
                    st.session_state.last_simulation = result
                else:
                    flow.submit(source, state['digest'])
                st.rerun()
            except flow.CheckoutError as e:
                st.error(str(e))
        locked = state['status'] in ('ordered', 'submitting', 'awaiting_supplier', 'unknown')
        if b.button('Decline — suggest alternatives', disabled=locked, key=f'checkout_no_{source}'):
            flow.reject(source)
            st.rerun()
        if c.button('Adjust', disabled=locked, key=f'checkout_edit_{source}'):
            st.session_state[f'checkout_editing_{source}'] = not st.session_state.get(f'checkout_editing_{source}', False)
    if state['status'] == 'declined' or st.session_state.get(f'checkout_editing_{source}'):
        feedback = st.text_input('What would you like to change?', placeholder='For example: keep the drinks, reduce bread, and stay within S$120.', key=f'checkout_feedback_{source}')
        if st.button('Update my proposal', disabled=not agent.available() or not feedback.strip(), key=f'checkout_apply_{source}'):
            try:
                with st.spinner('Updating your proposal for another review…'):
                    flow.tweak(source, feedback)
                st.rerun()
            except Exception as e:
                st.error('The update could not be completed. Your previous proposal is kept.')
        if not agent.available():
            st.caption('Free-text changes are unavailable right now. You can choose an alternative above.')
    labels = {'ordered':'Order placed — verified receipt', 'awaiting_supplier':'Message submitted; awaiting supplier confirmation', 'unknown':'Result unconfirmed; do not submit again', 'submitting':'Submitting', 'ready':'Waiting for final approval', 'declined':'Choose an alternative', 'needs_connection':'No order placed — connection required', 'needs_information':'No order placed — checks incomplete'}
    if not simulation_mode:
        st.caption('Order status: ' + labels.get(state['status'], 'Preparing — no order placed'))
    elif state['status'] != 'declined':
        result = st.session_state.get('last_simulation')
        completed = result and result['source'] == source and result['revision'] == state['revision']
        if not completed:
            st.caption('Simulation status: waiting for approval')
    if state['receipt']:
        if state['receipt'].get('order_id'):
            st.write('Order number: ' + str(state['receipt']['order_id']))
    if simulation_mode:
        from views.simulation_panel import render_result
        render_result(state)
