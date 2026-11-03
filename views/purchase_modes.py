"""Two purchase modes share the untouched original recommendation generator."""
import streamlit as st

STEP = 'Step-by-step (original)'
AUTO = 'AI purchase assistant'


def page(original_page):
    mode = st.segmented_control('Purchase workflow', [STEP, AUTO], default=STEP, key='purchase_mode')
    if mode == AUTO:
        st.caption('Keep the original recommendations. Let AI prepare the purchase for your final review.')
    else:
        st.caption('Original workflow: Generate plan → Review & approve → Final list → Suppliers.')
    original_page()
    if mode != AUTO:
        return
    result = st.session_state.get('plan_result')
    if result is None or result['items'].empty:
        return
    request = st.session_state.get('plan_request', '')
    if request != st.session_state.get('needs_text', ''):
        st.info('Your requirements have changed. Generate an updated recommendation above before preparing a purchase.')
        return
    from views.checkout_panel import panel
    panel(result, request, simulation_mode=bool(st.session_state.get('simulation_preview_enabled')))
