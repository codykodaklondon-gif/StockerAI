"""More → Insights: full shopping list, all alerts, sales forecast and customer habits."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ui
from stocker import advisor, db, engine
from views.common import ACTION_ICON, MAX_CARDS, SEV_ICON, cached_accuracy, cached_all_models, cached_profile, cached_recommendations, cached_risks, data_key, purchase_card, risk_cards


def page_risks():
    ui.heading("Things to check", "A few things to look at.",
               "Important things first. Each alert explains what is happening and what you can do.")
    st.caption("Updates every 30 seconds and after stock movements or imports.")

    @st.fragment(run_every="30s")
    def live():
        db.use_workspace(st.session_state.ws)
        risks = cached_risks(data_key())
        st.caption(f"Last check: {pd.Timestamp.now():%H:%M:%S}")
        if risks.empty:
            st.success("All clear — no risks detected right now.")
            return
        counts = risks["severity"].value_counts()
        c = st.columns(4)
        for i, sev in enumerate(["critical", "high", "medium", "low"]):
            c[i].metric(f"{SEV_ICON[sev]} {sev.title()}", int(counts.get(sev, 0)))
        with st.expander("Value at risk"):
            st.metric("Estimated value at risk", f"S${risks['value_at_risk'].sum():,.0f}")
        kinds = st.multiselect("Filter by type", sorted(risks["kind"].unique()))
        if kinds:
            risks = risks[risks["kind"].isin(kinds)]
        risk_cards(risks.head(MAX_CARDS))
        if len(risks) > MAX_CARDS:
            st.caption(f"Showing the {MAX_CARDS} most important of {len(risks)} alerts.")
            with st.expander(f"All {len(risks)} alerts"):
                st.dataframe(risks[["severity", "kind", "product", "message", "action", "value_at_risk"]],
                             hide_index=True, width="stretch",
                             column_config={"value_at_risk": st.column_config.NumberColumn("Value at risk", format="S$%.0f")})

    live()
    with st.expander("How risks are detected"):
        st.markdown(f"""
- **Out of stock / stock-out risk** — stock will not last until a new order can arrive (lead time).
- **Below reorder point** — stock is under lead-time demand plus a 95% safety buffer.
- **Dated batches** — actual recorded expiry dates, including expired stock and batches due soon. Manage them on **Expiry & batches**.
- **Expiry risk** — an estimate for stock without batch dates: it may take longer to sell than its shelf life.
- **Overstock** — more than {advisor.OVERSTOCK_DAYS} days of cover; **Dead stock** — no sales in 28 days.
- **Demand spike / drop** — latest day is more than 3 standard deviations from the forecast.
- **Trend shift** — last 7 days differ from the 28-day average by 40% or more.
- **Stale / insufficient data** — forecasts need recent data and at least {engine.MIN_DAYS} days of history.
""")


def page_stocking():
    ui.heading("What to buy", "Your shopping list.",
               "A starting point — check your remaining stock and decide how much to buy.")
    rec = cached_recommendations(data_key())
    if rec.empty:
        st.info("There are no stocking suggestions yet. Add sales and train models to get started.")
        return
    buy = rec[rec["order_high"] > 0]
    if not buy.empty:
        budget, download = st.columns([1, 1], vertical_alignment="center")
        if buy["est_cost"].sum() > 0:
            budget.metric("Estimated purchase budget", f"S${buy['est_cost'].sum():,.2f}")
        else:   # no unit costs recorded: a S$0 budget would be misleading
            budget.metric("Estimated purchase budget", "Not available")
            budget.caption("Add unit costs on the Inventory page to estimate it.")
        with download:
            st.download_button("Download purchase list (CSV)",
                               buy[["sku", "product", "order_low", "order_high", "est_cost"]].to_csv(index=False),
                               "purchase_list.csv", "text/csv", width="stretch")
            st.caption("Includes all products with a suggested purchase, regardless of the filter below.")
    actions = st.multiselect("Show", list(ACTION_ICON), default=[a for a in ACTION_ICON if a != "Maintain"])
    view = rec[rec["action"].isin(actions)] if actions else rec
    if view.empty:
        st.info("No products match this filter. Choose another action to see more.")
    for r in view.head(MAX_CARDS).itertuples():
        purchase_card(r)
    if len(view) > MAX_CARDS:
        st.caption(f"Showing the first {MAX_CARDS} of {len(view)} products — open the complete table below for the rest.")
    with st.expander("View complete table", expanded=len(view) > MAX_CARDS):
        table = view.assign(action=view["action"].map(lambda a: f"{ACTION_ICON[a]} {a}"))
        st.dataframe(
            table[["product", "action", "order_low", "order_high", "stock", "daily_forecast",
                   "cover_days", "est_cost", "reason"]],
            hide_index=True, width="stretch",
            column_config={
                "order_low": st.column_config.NumberColumn("Order min"),
                "order_high": st.column_config.NumberColumn("Order max"),
                "daily_forecast": st.column_config.NumberColumn("Forecast/day"),
                "cover_days": st.column_config.NumberColumn("Days of cover", format="%.1f"),
                "est_cost": st.column_config.NumberColumn("Est. cost", format="$%.2f"),
                "reason": st.column_config.TextColumn("Why", width="large"),
            },
        )


def page_forecast():
    st.header("Forecast")
    prods = db.products()
    label = st.selectbox("Product", prods["name"] + " (" + prods["sku"] + ")")
    sku = label.rsplit("(", 1)[1].rstrip(")")
    horizon = st.slider("Days ahead", 7, 30, 14)
    m = cached_all_models(data_key()).get(sku)
    if not m:
        st.warning("No model yet. Retrain from the sidebar.")
        return
    hist = engine.daily_series(sku).tail(60)
    fc = m.forecast(horizon, start=advisor.plan_start())
    fig = go.Figure()
    fig.add_scatter(x=hist.index, y=hist.values, name="Actual", line=dict(color="#4C78A8"))
    fig.add_scatter(x=pd.concat([fc["date"], fc["date"][::-1]]),
                    y=pd.concat([fc["high"], fc["low"][::-1]]),
                    fill="toself", fillcolor="rgba(245,133,24,0.18)", line=dict(width=0), name="80% range")
    fig.add_scatter(x=fc["date"], y=fc["forecast"], name="Forecast", line=dict(color="#F58518", dash="dash"))
    ev = fc[fc["event"] != ""]
    if not ev.empty:   # holidays / promotions the forecast was adjusted for
        fig.add_scatter(x=ev["date"], y=ev["forecast"], mode="markers", name="Holiday / promotion",
                        marker=dict(color="#C64E35", size=11, symbol="star"),
                        text=ev["event"], hovertemplate="%{text}<br>%{y:.1f} units<extra></extra>")
    fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0), legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")
    if not ev.empty:
        st.caption("★ Adjusted for: " + " · ".join(f"{r.event} ({r.date:%a %d %b})" for r in ev.itertuples())
                   + ". Manage these under **Holidays & promotions**.")

    c = st.columns(4)
    c[0].metric(f"Expected next {horizon} days", f"{fc['forecast'].sum():.0f}")
    c[1].metric("Avg last 7 days", f"{m.avg_7:.1f}/day", f"{(m.avg_7 / m.avg_28 - 1):+.0%} vs 28d" if m.avg_28 else None)
    if m.method == "ml":
        c[2].metric("Model", "Machine learning")
        c[3].metric("Recent error", f"{m.ml_wape:.0%}", f"{(m.ml_wape - m.stats_wape_recent) * 100:+.0f} pts vs standard",
                    delta_color="inverse")
    else:
        c[2].metric("Back-test error", f"{m.wape:.0%}" if m.wape is not None else "—")
        c[3].metric("Naive method error", f"{m.naive_wape:.0%}" if m.naive_wape is not None else "—")
        if m.variant != "standard":
            st.caption(f"Forecast method: **{engine.VARIANTS.get(m.variant, m.variant)}**, chosen because it was clearly "
                       "the most accurate on this product's own history.")
        elif len(m.variant_wape) > 1:
            st.caption(f"Forecast method: **{engine.VARIANTS['standard']}** (the default; no other method was clearly "
                       "more accurate on this product's history).")
    st.markdown("**Why**  \n" + "\n".join(f"- {n}" for n in engine.explain(m)))
    dow = pd.DataFrame({"Day": engine.DOW_NAMES, "Multiplier": m.dow})
    st.bar_chart(dow, x="Day", y="Multiplier", height=220)

    st.subheader("How accurate has it been?")
    bt = cached_accuracy(data_key(), sku, m.variant)
    if bt.empty:
        st.info(f"Not enough history yet: the accuracy check needs at least {engine.MIN_DAYS + 14} days of sales.")
        return
    fig = go.Figure()
    fig.add_bar(x=bt["date"], y=bt["actual"], name="Actually sold", marker_color="#A6BA8F")
    fig.add_scatter(x=bt["date"], y=bt["forecast"], name="Stocker AI forecast", line=dict(color="#C64E35", width=2.5))
    fig.add_scatter(x=bt["date"], y=bt["naive"], name="Simple 28-day average", line=dict(color="#9C8E80", dash="dot"))
    fig.update_layout(height=300, yaxis_title="Units / day")
    st.plotly_chart(ui.chart_style(fig), width="stretch")
    err = lambda col: (bt[col] - bt["actual"]).abs().sum() / bt["actual"].sum() if bt["actual"].sum() else None
    within = ((bt["forecast"] - bt["actual"]).abs() <= 0.2 * bt["actual"].clip(lower=1)).mean()
    over, weeks = m.weeks_over
    a = st.columns(4)
    a[0].metric("Weekly total error", f"{m.week_wape:.0%}" if m.week_wape is not None else "—",
                f"{m.week_wape - m.naive_week_wape:+.0%} vs simple average" if m.week_wape is not None else None,
                delta_color="inverse",
                help="How far each week's forecast total was from what really sold that week. "
                     "This is what a re-order depends on.")
    a[1].metric("Weeks above the range", f"{over} of {weeks}" if weeks else "—",
                help="Weeks that sold more than the top of the forecast's 80% range. About 1 in 10 is expected; "
                     "many more means the range is too narrow for this product.")
    a[2].metric("Daily error", f"{err('forecast'):.0%}" if err("forecast") is not None else "—",
                f"{err('forecast') - err('naive'):+.0%} vs simple average" if err("naive") is not None else None,
                delta_color="inverse", help="Total units the forecast was off by, day by day, as a share of what really sold.")
    a[3].metric("Days within ±20%", f"{within:.0%}",
                help="Single days are noisy, especially for slow sellers, so this is the strictest measure.")
    st.caption(f"Last {len(bt)} days. Each week the model was refitted on the sales before it and asked to "
               "forecast the next 7 days, so these are honest out-of-sample forecasts"
               + (" (shown for the standard engine; this product is forecast by machine learning)." if m.method == "ml" else "."))
    if len(m.variant_wape) > 1:
        with st.expander("Methods compared for this product"):
            st.dataframe(pd.DataFrame([{"Method": engine.VARIANTS[k], "Back-test error": v, "Chosen": "✓" if k == m.variant else ""}
                                       for k, v in sorted(m.variant_wape.items(), key=lambda x: x[1])]),
                         hide_index=True, width="stretch",
                         column_config={"Back-test error": st.column_config.NumberColumn(format="percent")})
            st.caption(f"The standard method is kept unless another is at least {engine.SWITCH_MARGIN:.0%} more accurate, "
                       "so the choice doesn't flip on noise. With a year of history, machine learning is also compared.")


def page_profile():
    st.header("Customer profile")
    st.caption("Built from anonymous purchase behaviour only — no names, phone numbers or demographic labels.")
    days = st.select_slider("Analysis window", [14, 28, 56, 90], value=56, format_func=lambda d: f"Last {d} days")
    p = cached_profile(data_key(), days)
    if not p.get("available"):
        st.info("No data yet.")
        return
    cap = p["capabilities"]
    missing = [n for k, n in [("time", "time of sale"), ("customer_type", "customer type"),
                              ("basket", "receipt no."), ("customers", "member ID")] if not cap[k]]
    if missing:
        st.warning("Some sections are hidden because your data has no " + ", ".join(missing) +
                   " column. Add them on **Import data** for a fuller profile.")

    st.subheader("Key insights")
    for n in p["insights"]:
        st.markdown(f"- {n}")

    if "segments" in p:
        seg = p["segments"]
        st.subheader("Customer segments")
        cols = st.columns(len(seg) + 1)
        for c, (name, r) in zip(cols, seg.iterrows()):
            c.metric(name, f"{int(r['customers'])} customers", f"{r['spend_share']:.0%} of spend", delta_color="off")
        if p.get("repeat_rate") is not None:
            cols[-1].metric("Repeat rate (30d)", f"{p['repeat_rate']:.0%}")
        st.caption("Regular: 1.5+ visits/week · Occasional: less often · New: first seen in the last 14 days · "
                   "Lapsed: no visit in 21 days.")

    left, right = st.columns(2)
    if "by_hour" in p:
        with left:
            st.subheader("When customers buy")
            h = p["by_hour"].reset_index()
            fig = go.Figure(go.Bar(x=h["hour"].astype(int).astype(str) + ":00", y=h["revenue"], marker_color="#4C78A8"))
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0), yaxis_title="Avg revenue / day")
            st.plotly_chart(fig, width="stretch")
    if "customer_types" in p:
        with right:
            st.subheader("Member vs walk-in")
            ct = p["customer_types"].reset_index()
            fig = go.Figure(go.Pie(labels=ct["customer_type"], values=ct["revenue"], hole=0.55,
                                   marker_colors=["#4C78A8", "#F58518", "#54A24B", "#B279A2"]))
            fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, width="stretch")
            st.dataframe(ct[["customer_type", "revenue_share", "avg_basket", "top_products"]], hide_index=True,
                         width="stretch", column_config={
                             "customer_type": "Type",
                             "revenue_share": st.column_config.NumberColumn("Revenue share", format="percent"),
                             "avg_basket": st.column_config.NumberColumn("Avg basket", format="$%.2f"),
                             "top_products": "Top products"})

    if "slot_by_category" in p:
        st.subheader("What sells when")
        s = p["slot_by_category"]
        fig = go.Figure(go.Heatmap(z=s.values, x=s.columns, y=s.index, colorscale="Blues",
                                   text=(s.values * 100).round(0), texttemplate="%{text:.0f}%", showscale=False))
        fig.update_layout(height=60 + 34 * len(s), margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, width="stretch")
        st.caption("Share of each category's units sold in each time slot — prepare stock before its peak.")

    st.subheader("Weekday vs weekend")
    ww = p["weekday_weekend"].reset_index()
    fig = go.Figure([go.Bar(name="Weekday", x=ww["category"], y=ww["weekday"], marker_color="#4C78A8"),
                     go.Bar(name="Weekend", x=ww["category"], y=ww["weekend"], marker_color="#F58518")])
    fig.update_layout(barmode="group", height=300, margin=dict(l=0, r=0, t=10, b=0),
                      yaxis_title="Avg units / day", legend=dict(orientation="h"))
    st.plotly_chart(fig, width="stretch")

    b = p.get("baskets")
    if b:
        st.subheader("Baskets")
        c = st.columns(3)
        c[0].metric("Avg items per receipt", b["avg_items"])
        c[1].metric("Avg receipt value", f"${b['avg_value']:.2f}")
        c[2].metric("Multi-item receipts", f"{b['multi_item_share']:.0%}")
        if b["pairs"]:
            st.markdown("**Often bought together**")
            st.dataframe(pd.DataFrame(b["pairs"]), hide_index=True, width="stretch",
                         column_config={"lift": st.column_config.NumberColumn("Lift", help="How many times more often than chance", format="%.1fx")})
