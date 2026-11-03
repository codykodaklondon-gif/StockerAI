"""Plain-language simulation result; technical records stay in the backend."""
import streamlit as st
from stocker import checkout_simulation as simulation


def render_result(state):
    result = st.session_state.get('last_simulation')
    if state['status'] == 'declined':
        return
    if not result or result['source'] != state['source'] or result['revision'] != state['revision']:
        return
    st.success('Simulation complete. No real order was placed and no email was sent.')
    st.write('Confirmation email: ' + result['recipient'])
    st.write(f'Estimated item cost: S${float(result["reference_total"]):,.2f}')
    with st.expander('Review confirmation email'):
        st.write('Subject: ' + result['subject'])
        st.text(result['body'])
        st.download_button('Download simulation email', simulation.eml(result), 'StockerAI-simulation.eml', 'message/rfc822', key=f'sim_download_{state["source"]}')
