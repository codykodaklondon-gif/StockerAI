"""My shop: the welcome page and today's overview."""
import plotly.graph_objects as go
import streamlit as st

import ui
from views import operations
from views.assistant import chat_panel
from views.common import cached_daily_revenue, cached_kpis, cached_recommendations, cached_risks, data_key, purchase_card, risk_cards


# ------------------------------------------------------------------ pages
def page_welcome():
    with st.container(key="welcome_hero"):
        left, right = st.columns([1, 1.05], gap="large", vertical_alignment="center")
        with left:
            st.html('<div class="eyebrow">A little help, every day</div>'
                    '<h1 class="welcome-title">A little help<br>for your shop.</h1>'
                    '<p class="welcome-copy">See what to restock, what can wait, '
                    'and how your shop is doing.</p>')
            st.button("Open my shop →", type="primary", width="stretch",
                      on_click=ui.go_to, args=("📊 Dashboard",))
            st.button("Import sales", width="stretch", on_click=ui.go_to,
                      args=("📥 Import data",))
            st.button("Export results", icon=":material/download:", width="stretch",
                      on_click=ui.go_to, args=("📤 Export",))
            st.caption("Explore your shop, bring your own sales records, or export the analysis.")
        with right:
            st.image(str(ui.ROOT / "assets" / "neighbourhood-shop.png"), width="stretch")
    with st.container(border=True, key="welcome_chat"):
        st.markdown(":material/forum: **Ask Stocker AI**")
        st.caption("Ask about your shop in plain language. Numbers come from your own sales records.")
        chat_panel("welcome", height=360)
        st.button("Open the full chat →", type="tertiary", on_click=ui.go_to, args=("🤖 AI assistant",))
    for col, title, body in zip(st.columns(3),
                                ["Know what to buy", "Catch problems early", "Get to know your shop"],
                                ["A practical stocking list, with the reasons behind it.",
                                 "See what needs attention before it becomes a bigger problem.",
                                 "Make sense of sales patterns and customer habits."]):
        with col:
            st.html(f'<div class="feature"><strong>{title}</strong><p>{body}</p></div>')
    st.html('<div class="ui-footer">Stocker AI · For the everyday work of running a shop.</div>')


def page_dashboard():
    ui.heading("My shop / Today", "Let's check your shop.",
               "A quick look at what needs attention, and how things are going.")
    operations.dashboard_summary()
    with st.container(border=True, horizontal=True, vertical_alignment="center"):
        st.markdown(":material/edit_note: **Planning tomorrow's order?** Tell Stocker AI your budget, space and plans "
                    "— it builds a purchase plan from your own sales.")
        st.button("Tell me your needs", type="primary", on_click=ui.go_to, args=("📝 Needs",), key="needs_cta")
    with st.form("ask_home", border=True):
        st.markdown(":material/forum: **Ask Stocker AI**")
        q_col, b_col = st.columns([5, 1], vertical_alignment="bottom")
        question = q_col.text_input("Your question", placeholder="e.g. What should I order for the weekend?",
                                    label_visibility="collapsed")
        if b_col.form_submit_button("Ask", type="primary", width="stretch") and question.strip():
            st.session_state.pending_question = question.strip()
            st.session_state.nav = "🤖 AI assistant"
            st.rerun()
    k = cached_kpis(data_key())
    risks = cached_risks(data_key())
    urgent = int(risks["severity"].isin(["critical", "high"]).sum()) if not risks.empty else 0
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        products, attention = st.columns(2)
        products.metric("Products in your shop", k["products"])
        attention.metric("Urgent alerts to check", urgent)
        st.subheader("Start here today")
        rec = cached_recommendations(data_key())
        todo = rec[rec["action"] != "Maintain"] if not rec.empty else rec
        if todo.empty:
            st.success("Nothing to change today. Stock levels look healthy.")
        for r in todo.head(5).itertuples():
            purchase_card(r)
        if len(todo) > 5:
            st.caption(f"Showing 5 of {len(todo)} items that need attention.")
        st.button("See what to buy →", type="primary", width="stretch",
                  on_click=ui.go_to, args=("🛒 Stocking list",))
    with right:
        with st.container(border=True):
            st.subheader("How sales are going")
            st.caption("Revenue over the latest 90 recorded days")
            daily = cached_daily_revenue(data_key())
            fig = go.Figure(go.Bar(x=daily.index, y=daily["revenue"], name="Daily revenue", marker_color="#A6BA8F"))
            fig.add_scatter(x=daily.index, y=daily["revenue"].rolling(7).mean(), name="7-day average",
                            line=dict(color="#C64E35", width=2.5))
            fig.update_layout(height=290, yaxis_title="Revenue (S$)")
            st.plotly_chart(ui.chart_style(fig), width="stretch")
        with st.expander("More shop details"):
            st.metric("Stock value (cost)", f"S${k['stock_value']:,.0f}")
            st.metric("Revenue, last 30 recorded days", f"S${k['revenue_30d']:,.0f}")
        st.subheader("Things to look at")
        if risks.empty:
            st.success("No risks detected.")
        else:
            risk_cards(risks.head(4))
        st.button("See all alerts →", width="stretch", on_click=ui.go_to, args=("🚨 Risk monitor",))
