"""Stocker AI — Streamlit entry point.  Run:  streamlit run app.py

This file only starts the app: visitor workspace, sidebar navigation and routing.
Pages live in views/ (one module per sidebar section); analysis lives in stocker/.
"""
import os
import uuid

import streamlit as st

import ui
from stocker import APP_NAME, agent, db, engine, notifier, sample
from views import advice_review, assistant, data, help, insights, operations, ordering, planning, pricing, setup, shop
from views.common import HOLIDAY_COUNTRIES, cached_models, cached_summary, data_key, has_data, has_models, retrain

st.set_page_config(page_title=APP_NAME, page_icon="📦", layout="wide")
ui.theme()


# Shared AI key from Streamlit secrets (Streamlit Cloud) -> environment, so agent.shared() finds it
try:
    for _k in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "STOCKER_MODEL",
               "TELEGRAM_BOT_TOKEN", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD"):
        if _k in st.secrets and not os.environ.get(_k):
            os.environ[_k] = str(st.secrets[_k])
except Exception:  # no secrets file configured
    pass


def open_workspace():
    """Each visitor gets a private workspace, kept in the URL so a refresh keeps their data."""
    ws = st.query_params.get("ws") or st.session_state.get("ws") or uuid.uuid4().hex[:16]
    st.session_state.ws = ws
    if st.query_params.get("ws") != ws:
        st.query_params["ws"] = ws
    path = db.use_workspace(ws)
    new = not path.exists()
    db.init()
    if new:
        db.cleanup_workspaces()
    return new


first_visit = open_workspace()


@st.cache_resource
def alert_scheduler():
    """One background thread per app process sends the owner's daily summaries and instant alerts."""
    return notifier.start_background()


alert_scheduler()


# ------------------------------------------------------------------ sidebar
# Navigation: four everyday sections; analysis and setup sit under "More".
# Every page keeps its own key, so links elsewhere (ui.go_to) keep working.
GROUPS = [  # (id, sidebar label, icon, pages in step order)
    ("home", "My shop", ":material/storefront:", ["📊 Dashboard", "📒 Weekly", "🥬 Expiry"]),
    ("plan", "Plan & order", ":material/shopping_basket:", ["📝 Needs", "✅ Review", "🧾 Order list", "🤝 Suppliers"]),
    ("ai", "Ask Stocker AI", ":material/forum:", ["🤖 AI assistant"]),
    ("data", "Import & export", ":material/swap_vert:", ["📥 Import data", "📤 Export"]),
]
STEP_LABELS = {"📝 Needs": "1 · Plan with AI", "✅ Review": "2 · Review & approve", "🧾 Order list": "3 · Final list",
               "🤝 Suppliers": "4 · Suppliers", "📥 Import data": "Import sales", "📤 Export": "Export results",
               "📊 Dashboard": "Today", "📒 Weekly": "Weekly review", "🥬 Expiry": "Expiry & batches"}
MORE = [  # (heading, [(page, label, icon)])
    ("Insights", [("🛒 Stocking list", "Full shopping list", ":material/shopping_cart:"),
                  ("🚨 Risk monitor", "All alerts", ":material/notifications:"),
                  ("📈 Forecast", "Sales forecast", ":material/trending_up:"),
                  ("👥 Customer profile", "Customer habits", ":material/groups:"),
                  ("🧾 Advice review", "Advice evidence & review", ":material/fact_check:")]),
    ("Shop setup", [("📦 Inventory", "Stock & products", ":material/inventory_2:"),
                    ("📅 Events", "Holidays & promotions", ":material/event:"),
                    ("🔔 Alerts", "Alerts to phone & email", ":material/notifications_active:")]),
    ("Plans", [("💳 Pricing", "Pricing", ":material/sell:")]),
    ("Help", [("🛟 Help", "Help & contact", ":material/help:"),
              ("🏡 Welcome", "About Stocker AI", ":material/info:")]),
]
PAGES = {"🏡 Welcome": "Welcome", "📊 Dashboard": "Dashboard", "📥 Import data": "Import data",
         "🚨 Risk monitor": "Risk monitor", "🛒 Stocking list": "Stocking list", "📈 Forecast": "Forecast",
         "👥 Customer profile": "Customer profile", "📦 Inventory": "Inventory", "🤖 AI assistant": "AI assistant",
         "📝 Needs": "Needs", "✅ Review": "Review", "📤 Export": "Export", "📅 Events": "Events",
         "🔔 Alerts": "Alerts", "🧾 Order list": "Order list", "🤝 Suppliers": "Suppliers", "🛟 Help": "Help",
         "🥬 Expiry": "Expiry", "📒 Weekly": "Weekly",
         "🧾 Advice review": "Advice review", "💳 Pricing": "Pricing"}
GROUP_OF = {p: g for g, _, _, pages in GROUPS for p in pages}


def open_group(gid: str):
    """A section button reopens the page last used in that section."""
    pages = next(p for g, _, _, p in GROUPS if g == gid)
    last = st.session_state.get(f"last_{gid}")
    ui.go_to(last if last in pages else pages[0])


def step_bar(current: str):
    """Steps of the current section, shown above multi-page sections (Plan & order, Import & export)."""
    gid = GROUP_OF.get(current)
    pages = next((p for g, _, _, p in GROUPS if g == gid), [])
    if len(pages) < 2:
        return
    key = f"steps_{gid}_{current}"
    st.segmented_control("Step", pages, default=current, format_func=STEP_LABELS.get, key=key,
                         label_visibility="collapsed",
                         on_change=lambda: st.session_state[key] and ui.go_to(st.session_state[key]))


with st.sidebar:
    st.title(f"🌿 {APP_NAME}")
    st.caption("A little help for the everyday work of running your shop.")
    st.session_state.setdefault("nav", "🏡 Welcome")
    if st.session_state.nav not in PAGES:
        st.session_state.nav = "📊 Dashboard"
    current = st.session_state.nav
    if current in GROUP_OF:
        st.session_state[f"last_{GROUP_OF[current]}"] = current
    for gid, label, icon, pages in GROUPS:
        st.button(label, icon=icon, width="stretch", key=f"nav_{gid}",
                  type="primary" if current in pages else "secondary", on_click=open_group, args=(gid,))
    more_pages = [p for _, items in MORE for p, *_ in items]
    with st.expander("More", icon=":material/more_horiz:", expanded=current in more_pages):
        for heading, items in MORE:
            st.caption(heading.upper())
            for key, label, icon in items:
                st.button(label, icon=icon, width="stretch", key=f"nav_{key}",
                          type="primary" if current == key else "tertiary", on_click=ui.go_to, args=(key,))
    page = PAGES[current]

    ai_status = st.empty()   # filled in after Settings applies this run's key
    with st.expander("Settings", icon=":material/settings:"):
        st.markdown("**AI**")
        if agent.shared():
            st.caption("AI chat works out of the box. Optionally use your own key below.")
        names = list(agent.PROVIDERS)
        prov = st.selectbox("Provider", names, format_func=lambda p: agent.PROVIDERS[p]["label"], key="llm_provider")
        key = st.text_input("API key", type="password", key="llm_key",
                            help="Used only in your browser session, never stored. Free keys: console.groq.com · aistudio.google.com")
        key, model = "".join(key.split()), ""   # keys never contain spaces; a key copied over several lines arrives broken
        if key:
            try:
                options = cached_models(prov, key)
                model = st.selectbox("Model", options, key=f"llm_model_{prov}",
                                     help="Fetched from your account; the best tool-capable model is preselected.")
            except Exception as e:
                st.error(agent.key_problem(prov, key, e))
                key = ""   # a rejected key is not used, so the status below stays truthful
        agent.configure(prov, key, model)
        if st.session_state.get("chat_provider") not in (None, prov):
            st.session_state.history, st.session_state.display = [], []   # formats differ per provider
        st.session_state.chat_provider = prov
        act = agent.active()
        ai_status.caption(f"AI: 🟢 {agent.PROVIDERS[act[0]]['label']}" if act else "AI: ⚪ off — add a key in Settings")
        if act:
            st.caption(f"Model: {act[2]}")
        else:
            try:   # names only, never values: helps spot a mistyped secret on the hosting platform
                secret_names = list(st.secrets.keys())
                problem = None
            except Exception as e:   # e.g. invalid TOML: the error names the line, not the value
                secret_names, problem = [], f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
            if secret_names:
                st.caption("Secrets found: " + ", ".join(f"`{n}`" for n in secret_names) + " — expected `GROQ_API_KEY`.")
            elif problem:
                st.caption(f"Secrets could not be read ({problem}). Check the format: GROQ_API_KEY = \"gsk_...\"")
            else:
                st.caption("No secrets found on this server.")

        st.markdown("**Forecasting**")
        s = cached_summary(data_key())
        if s["trained"]:
            st.caption(f"Models: {s['trained']} products · trained {s['trained_at']}")
            if s.get("wape") is not None:
                st.caption(f"Back-test error {s['wape']:.0%} vs naive {s['naive_wape']:.0%}"
                           + (f" · weekly totals {s['week_wape']:.0%} vs {s['naive_week_wape']:.0%}"
                              if s.get("week_wape") is not None else ""))
            if s.get("ml_products"):
                st.caption(f"Machine learning in use for {s['ml_products']} products · recent error "
                           f"{s['recent_wape']:.0%} vs {s['recent_standard_wape']:.0%} standard model")
            else:
                st.caption(f"Machine learning switches on with {engine.mlmodel.MIN_HISTORY_DAYS}+ days of history "
                           "when it beats the standard model.")
        country = st.selectbox("Public holidays", list(HOLIDAY_COUNTRIES), format_func=HOLIDAY_COUNTRIES.get,
                               index=list(HOLIDAY_COUNTRIES).index(db.get_setting("holiday_country", "SG")),
                               help="Used by the machine-learning forecast. Retrain after changing it.")
        if country != db.get_setting("holiday_country", "SG"):
            db.set_setting("holiday_country", country)
        if has_data() and st.button("Retrain models", icon=":material/refresh:", width="stretch"):
            retrain()
            st.rerun()
        st.caption(f"🔒 Private workspace `{st.session_state.ws[:6]}` — your data is visible only to you. "
                   "Bookmark this page to come back to it.")

if first_visit:
    # new visitors start with a ready-made demo shop so every page has something to show
    with st.spinner("Setting up your demo shop…"):
        sample.load_demo()
        engine.train_all()

if not has_data() and page not in ("Import data", "Welcome", "Help", "Expiry", "Weekly"):
    st.header(f"Welcome to {APP_NAME}")
    st.write("Import your historical sales (CSV or Excel from any POS or spreadsheet) to get started, "
             "or load a demo shop to explore.")
    c1, c2 = st.columns(2)
    if c1.button("Load demo shop", type="primary"):
        sample.load_demo()
        retrain()
        st.rerun()
    c2.button("📥 Import my own file", on_click=lambda: st.session_state.update(nav="📥 Import data"))
    st.stop()

if has_data() and not has_models() and page not in ("Import data", "Welcome", "Help", "Expiry"):
    retrain()


PAGE_FUNCS = {
    "Welcome": shop.page_welcome, "Dashboard": shop.page_dashboard,
    "Weekly": operations.weekly_page, "Expiry": operations.expiry_page,
    "Needs": planning.page_needs, "Review": planning.page_review,
    "Order list": ordering.page_order_list, "Suppliers": ordering.page_suppliers,
    "AI assistant": assistant.page_ai,
    "Import data": data.page_import, "Export": data.page_export,
    "Stocking list": insights.page_stocking, "Risk monitor": insights.page_risks,
    "Forecast": insights.page_forecast, "Customer profile": insights.page_profile,
    "Inventory": setup.page_inventory, "Events": setup.page_events, "Alerts": setup.page_alerts,
    "Advice review": advice_review.page, "Help": help.page_support, "Pricing": pricing.page_pricing,
}
step_bar(current)
PAGE_FUNCS[page]()
