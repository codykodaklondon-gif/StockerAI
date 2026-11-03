"""Shared helpers for every page: cached analysis results, data versioning and small UI pieces.

Cached results are keyed by data_key(), which changes whenever the workspace database changes
(import, stock movement, retrain) or the day rolls over, so cached values are never stale."""
from datetime import date
import io
import os

import pandas as pd
import streamlit as st

import ui
from stocker import advisor, agent, db, engine, evidence, importer, notifier, profile, sample
from stocker.icons import icon
from views import advice_review


AI_LIMIT = int(os.environ.get("STOCKER_AI_LIMIT", 30))   # questions per visitor on the shared key


HOLIDAY_COUNTRIES = {"SG": "Singapore", "MY": "Malaysia", "ID": "Indonesia", "TH": "Thailand", "PH": "Philippines",
                     "VN": "Vietnam", "CN": "China", "HK": "Hong Kong", "TW": "Taiwan", "AU": "Australia",
                     "GB": "United Kingdom", "FR": "France", "US": "United States"}


SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}


ACTION_ICON = {"Stock more": "⬆️", "Plan order": "📝", "Stock less": "⬇️", "Maintain": "✅",
               "Stop reordering": "⛔", "Collect more data": "⏳"}


KIND_ICON = {"Out of stock": "🚫", "Stock-out risk": "⚠️", "Below reorder point": "🔻", "Expiry risk": "⏳",
             "Expired batch": "🗑️", "Batch expires today": "⏰", "Batch expiring soon": "⏳", "Dead stock": "💤",
             "Overstock": "📦", "Demand spike": "📈", "Demand drop": "📉", "Trend shift": "🔀",
             "Insufficient data": "❔", "Stale data": "🕰️", "Stock levels not set": "📝"}


def has_data() -> bool:
    return not db.query("SELECT 1 FROM sales LIMIT 1").empty


def has_models() -> bool:
    return cached_summary(data_key())["trained"] > 0


def retrain():
    bar = st.progress(0.0, text="Training models…")
    shown = [-1]

    def update(p):   # redraw at most ~50 times; one message per product slows big catalogues down
        if int(p * 50) != shown[0]:
            shown[0] = int(p * 50)
            bar.progress(p, text=f"Training models… {p:.0%}")
    engine.train_all(progress=update)
    bar.empty()


# ------------------------------------------------------------------ cached analysis
# Results are cached per workspace and invalidated whenever its database file changes
# (any import, stock movement or retrain) or the day rolls over.
def data_key() -> tuple:
    path = db.current()
    return str(path), path.stat().st_mtime_ns if path.exists() else 0, date.today().isoformat()


@st.cache_data(max_entries=32, show_spinner="Checking risks…")
def cached_risks(key: tuple) -> pd.DataFrame:
    return advisor.risks_frame()


@st.cache_data(max_entries=32, show_spinner="Building the stocking list…")
def cached_recommendations(key: tuple) -> pd.DataFrame:
    return evidence.save_current()   # same advice, plus a snapshot for the outcome review


@st.cache_data(max_entries=32, show_spinner=False)
def cached_kpis(key: tuple) -> dict:
    return advisor.kpis()


@st.cache_data(max_entries=32, show_spinner="Analysing customers…")
def cached_profile(key: tuple, days: int) -> dict:
    return profile.build(days)


@st.cache_data(max_entries=32, show_spinner=False)
def cached_daily_revenue(key: tuple, days: int = 90) -> pd.DataFrame:
    df = db.query("SELECT date, SUM(revenue) AS revenue FROM sales GROUP BY date ORDER BY date DESC LIMIT ?", (days,))
    return df.assign(date=pd.to_datetime(df["date"])).set_index("date").sort_index()


MAX_CARDS = 12   # cards are rich widgets; beyond this, the full table/list is the better view


@st.cache_data(max_entries=32, show_spinner=False)
def cached_summary(key: tuple) -> dict:
    return engine.training_summary()


@st.cache_data(max_entries=32, show_spinner=False)
def cached_all_models(key: tuple) -> dict:
    return engine.load_models()


@st.cache_data(max_entries=32, show_spinner=False)
def cached_briefing(key: tuple) -> str:
    return agent.offline_answer()


@st.cache_data(max_entries=64, show_spinner="Checking past accuracy…")
def cached_accuracy(key: tuple, sku: str, variant: str = "standard") -> pd.DataFrame:
    return engine.backtest_frame(engine.daily_series(sku), variant=variant)


@st.cache_data(max_entries=32, show_spinner=False)
def cached_evidence(key: tuple) -> dict:
    return evidence.assess_all()


@st.cache_data(max_entries=16, show_spinner=False)
def cached_alert_preview(key: tuple):
    # alert settings live in the workspace database, so saving them changes the key too
    return notifier.daily_report({**notifier.config(), "only_if_needed": False})


@st.cache_data(max_entries=2, show_spinner="Reading file…")
def parse_upload(data: bytes, name: str) -> pd.DataFrame:
    return importer.read_file(io.BytesIO(data), name)


@st.cache_data(show_spinner=False)
def sample_file() -> str:
    demo, _ = sample.generate(days=60)
    return sample.to_transactions(demo).to_csv(index=False)


@st.cache_data(ttl=3600, show_spinner="Checking key…")
def cached_models(provider: str, key: str) -> list[str]:
    return agent.list_models(provider, key)


def risk_cards(df: pd.DataFrame):
    labels = {"critical": "Act now", "high": "Check today", "medium": "Plan a next step", "low": "Keep an eye on this"}
    tones = {"critical": "red", "high": "coral", "medium": "amber", "low": "sage"}
    for r in df.itertuples():
        with st.container(border=True):
            ui.badge(f"{labels.get(r.severity, 'Review')} · {r.severity.title()}", tones.get(r.severity, "neutral"))
            ui.product_heading(r.product, f"{KIND_ICON.get(r.kind, '•')} {r.kind}", symbol=icon(r.product))
            st.write(r.message)
            st.write(f"**Next step:** {r.action}")
            with st.expander("Show details"):
                st.write(f"Risk type: {r.kind}")
                st.write(f"Severity: {r.severity.title()}")
                if r.value_at_risk:
                    st.write(f"Estimated value at risk: S${r.value_at_risk:,.2f}")
                st.caption(f"Product code: {r.sku}")


def purchase_card(r):
    with st.container(border=True):
        title, action = st.columns([3, 2], vertical_alignment="center")
        with title:
            ui.product_heading(r.product, r.category)
        with action:
            tone = "coral" if r.action == "Stock more" else "sage" if r.action == "Maintain" else "amber"
            ui.badge(r.action, tone)
        current, suggested = st.columns(2)
        current.write(f"**{r.stock:g} units** left in stock" if pd.notna(r.stock) else "Stock level not recorded")
        if r.order_high and r.action == "Plan order":
            suggested.write(f"Expected need: **{r.order_low:,}–{r.order_high:,} units**")
        elif r.order_high:
            suggested.write(f"Suggested purchase: **{r.order_low:,}–{r.order_high:,} units**")
        else:
            suggested.write("No purchase quantity suggested")
        with st.expander("Why this amount?" if r.order_high else "Why this advice?"):
            st.write(r.reason)
            st.divider()
            st.write(f"Expected sales: {r.daily_forecast:g} units per day")
            st.write(f"Stock covers: {r.cover_days:g} days" if pd.notna(r.cover_days) else "Days of cover: unavailable")
            st.write(f"Estimated purchase cost: S${r.est_cost:,.2f}")
            st.caption(f"Product code: {r.sku}")
            st.divider()
            advice_review.evidence_lines(cached_evidence(data_key()).get(r.sku))
