"""Explicit UI example: no model request and no merchant submission."""
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import runpy
import os
import pandas as pd
import streamlit as st
st.set_page_config(page_title='StockerAI · Purchase preview', layout='wide')
st.warning('The recommendation list below is a fixed UI example. Real merchant links and the simulation are separate. No order has been placed.')
request = 'I am a first-time shop owner. I can spend at most S$120 tomorrow and have very limited fridge space. A nearby school is holding an event. I want to stock more student-friendly products, but I do not want too much leftover bread that could spoil.'
if not st.session_state.get('checkout_preview_initialized'):
    st.session_state.nav = '📝 Needs'
    st.session_state.purchase_mode = 'AI purchase assistant' if st.query_params.get('mode') == 'auto' else 'Step-by-step (original)'
    st.session_state.order_environment = 'Real order' if st.query_params.get('order') == 'real' else 'Simulation'
    st.session_state.needs_text = request
    st.session_state.plan_request = request
    rows = pd.DataFrame([
        {'sku': 'DRK-01', 'product': 'Isotonic Drink', 'quantity': 60, 'unit_cost': .9, 'est_cost': 54., 'covers': None, 'priority': 1, 'shelf_life': 270, 'reason': 'UI example: drinks for the school event. Generate a plan for actual recommendations.'},
        {'sku': 'BRD-01', 'product': 'White Bread', 'quantity': 10, 'unit_cost': 1.6, 'est_cost': 16., 'covers': None, 'priority': 2, 'shelf_life': 4, 'reason': 'UI example: a smaller bread quantity to reduce leftovers.'},
        {'sku': 'RCE-05', 'product': 'Rice 5kg', 'quantity': 3, 'unit_cost': 10.5, 'est_cost': 31.5, 'covers': None, 'priority': 3, 'shelf_life': 365, 'reason': 'UI example: illustrates keeping the original recommendation.'},
    ])
    st.session_state.plan_result = {'summary': {'budget_sgd': 120, 'horizon_days': 1, 'storage': 'Very limited fridge space', 'events': ['A nearby school is holding an event'], 'goals': ['Stock student-friendly products for the school event'], 'worries': ['Avoid leftover bread that could spoil'], 'experience': 'First-time shop owner'}, 'items': rows, 'demo_only': True, 'budget': 120, 'total_cost': 101.5, 'within_budget': True, 'checks': ['Fixed UI example, not a live AI recommendation. Estimated costs exclude actual delivery charges and cannot be used as a real order.'], 'strategy': {}, 'assumptions': [], 'data_gaps': [], 'questions': [], 'plan_starts': (datetime.now(ZoneInfo('Asia/Singapore')).date() + timedelta(days=1)).isoformat(), 'model': 'Fixed UI example (no live AI request or actual order)'}
    st.session_state.checkout_preview_initialized = True
st.session_state.simulation_preview_enabled = True
st.session_state.simulation_default_email = os.environ.get('STOCKER_CONTACT_EMAIL', '')
runpy.run_path(str(Path(__file__).with_name('app.py')), run_name='__main__')

st.set_page_config(page_title=('AI purchase assistant' if st.session_state.get('purchase_mode') == 'AI purchase assistant' else 'Step-by-step (original)') + ' · StockerAI preview')
