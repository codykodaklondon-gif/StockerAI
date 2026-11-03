"""Real merchant navigation and supplier-email handoff. Opening is not ordering."""
from urllib.parse import quote
import streamlit as st

SHOPS = {
    'Grocery Owl B2B — Lam Soon': 'https://www.b2b.groceryowl.com.sg/',
    'FairPrice for Business': 'https://www.fairprice.com.sg/business-about-us',
    'RedMart / Lazada': 'https://redmart.lazada.sg/',
}


def panel(state):
    st.markdown('**Continue with a real supplier**')
    st.caption('These links open real purchasing websites. Sign in on the merchant’s own website. Opening a link does not place an order.')
    columns = st.columns(3)
    for column, (name, url) in zip(columns, SHOPS.items()):
        column.link_button(name, url, width='stretch')
    recipient = st.text_input('Supplier email', value=st.session_state.get('simulation_default_email', ''), key='real_supplier_email')
    lines = '\n'.join(f'- {r["product"]}: requested quantity {r["quantity"]}; please confirm the matching SKU and selling unit.' for r in state['rows'])
    body = ('Hello,\n\nPlease provide a quotation and confirm availability for the following proposed purchase:\n\n'
            + lines + '\n\nPlease confirm the brand, variant, net content per item, units per pack/carton, price including GST, delivery charge, minimum order and earliest delivery date.'
            + (f'\nOur maximum budget is S${state["budget"]:.2f}, including GST and delivery.' if state.get('budget') else '')
            + '\n\nPlease do not substitute brands or pack sizes without our approval. This is an enquiry, not a confirmed purchase order.\n\nThank you.')
    subject = 'Purchase enquiry — availability, pack sizes and quotation'
    with st.expander('Review the supplier email'):
        st.write('Subject: ' + subject)
        st.text(body)
    if recipient and '@' in recipient and not any(x in recipient for x in '\r\n,; '):
        st.link_button('Open email to supplier', 'mailto:' + recipient + '?subject=' + quote(subject, safe='') + '&body=' + quote(body, safe=''), type='primary')
    st.caption('Send from your email app after review. This app will not mark the email as sent or the purchase as ordered without confirmation.')
