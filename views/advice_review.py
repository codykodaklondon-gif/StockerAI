"""Advice evidence (inside each purchase card) and the outcome review page (More → Insights)."""
import pandas as pd
import streamlit as st

from stocker import evidence


def evidence_lines(facts):
    """Data facts behind one product's advice. Shown inside an expander, so it adds no clutter to the card."""
    if not facts:
        return
    st.write("**Next step:** " + facts["next_action"])
    st.write(f"History span: {facts['history_span_days']} days; explicit sales records: {facts['recorded_sales_days']} days.")
    st.write("Sales recorded through: " + (facts["last_sales_date"] or "Unknown"))
    st.write("Last recorded stock count: " + (facts["last_stock_count"] or "Unknown — confirm stock first"))
    st.write("Model trained at: " + (facts["trained_at"] or "Not available"))
    w, n = facts["backtest_wape"], facts["naive_backtest_wape"]
    st.write(f"Historical backtest WAPE: {w:.1%}" if w is not None else "Historical backtest: not enough evidence.")
    if n is not None:
        st.caption(f"Simple 28-day-average backtest WAPE: {n:.1%}")
    for warning in facts["warnings"]:
        st.warning(warning)
    st.caption("These are data facts and historical errors, not a confidence percentage or a guarantee.")


def evidence_card(facts):
    with st.expander("Evidence for this advice"):
        evidence_lines(facts)


def _fmt(value, digits=1):
    return "Not available" if value is None else f"{value:,.{digits}f}"


def page():
    st.header("Advice evidence & review")
    st.caption("First check the evidence behind advice. Later compare its frozen forecast with recorded outcomes.")
    if st.button("Save current advice for future review"):
        evidence.save_current()
        st.success("Current advice saved (identical advice is not duplicated).")
    snapshots = evidence.snapshots()
    if snapshots.empty:
        st.info("Open My shop / Full shopping list, or save current advice above. Reviews start prospectively; past forecasts are not invented.")
        return
    ids = snapshots["id"].tolist()
    labels = {r.id: f"Saved {r.created_at} · forecast {r.forecast_start} to {r.forecast_end}" for r in snapshots.itertuples()}
    selected = st.selectbox("Saved advice", ids, format_func=lambda value: labels[value], key="review_snapshot")
    snapshot = evidence.get(selected)
    items = {i["sku"]: i for i in snapshot["payload"]["items"]}
    sku = st.selectbox("Product to review", list(items),
                       format_func=lambda value: f"{items[value]['product']} ({value})", key=f"review_sku_{selected}")
    result = evidence.report(selected, sku)
    item = result["item"]
    st.subheader("What was recommended")
    st.write(f"**{item['action']}** · suggested quantity {item['order_low']}–{item['order_high']}")
    st.write(item["reason"])
    st.caption(f"Original purchasing period: {item['recommendation_start']} to {item['recommendation_end']} "
               "(lead time + review cycle). This is separate from the forward seven-day forecast evaluation below.")
    for risk in item["risks"]:
        st.write(f"Original alert — {risk['kind']}: {risk['message']}")
    evidence_card(item["evidence"])

    st.subheader("1. Did the seven-day forecast match sales?")
    st.write(f"**{result['status']}** · {result['observed_days']}/7 closed days with usable sales data")
    st.caption("Imported daily rows are treated as the supplied daily totals. If the source file is incomplete, the review is not reliable.")
    a, b = st.columns(2)
    a.metric("Frozen seven-day forecast", _fmt(result["forecast_total"]))
    b.metric("Actual seven-day sales", _fmt(result["actual_total"]) if result["complete"] else "Pending / incomplete")
    frame = pd.DataFrame(result["daily"])
    chart = frame.assign(date=pd.to_datetime(frame["date"])).set_index("date")
    st.line_chart(chart[["forecast", "actual", "low", "high"]], width="stretch")
    with st.expander("Daily evidence and CSV"):
        st.dataframe(frame, hide_index=True, width="stretch")
        st.download_button("Download forecast review data", frame.to_csv(index=False),
                           f"forecast_review_{selected[:8]}.csv", "text/csv")
    if result["complete"]:
        st.write(f"Actual minus forecast: {result['bias_units']:+.1f} units. Daily mean absolute error: {result['mae']:.2f} units.")
        if result["wape"] is None:
            st.info("Actual sales total is zero. Percentage error is undefined; it is not reported as 0%.")
        else:
            st.write(f"Prospective WAPE: {result['wape']:.1%}; frozen simple-average baseline: {result['naive_wape']:.1%}.")
        st.caption("Prospective WAPE uses the sum of absolute daily errors divided by total actual units. "
                   "It differs from the historical backtest shown above and is not 'accuracy = 100% minus error'.")
    else:
        st.info("Future dates and today's unfinished sales are excluded. Missing records are unknown, not automatically zero.")
        if result["observed_actual_total"] is not None:
            st.write("Sales recorded on usable closed days so far: " + _fmt(result["observed_actual_total"]))
    if evidence.today().isoformat() > snapshot["forecast_end"] and not result["complete"]:
        with st.expander("Missing rows really mean zero sales?"):
            st.write("Only use this if the whole period's import is complete and omitted dates genuinely mean zero sales. "
                     "Otherwise import the missing records. Your confirmation is saved separately as owner-reported evidence.")
            with st.form(f"zero_{selected}_{sku}"):
                owner = st.text_input("Confirmed by")
                ack = st.checkbox("I checked that the period is complete and missing dates mean zero sales.")
                note = st.text_input("Coverage note")
                if st.form_submit_button("Confirm complete coverage"):
                    try:
                        if not ack:
                            raise ValueError("Confirm complete coverage explicitly first.")
                        evidence.confirm_missing_days_zero(selected, sku, owner, note)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    if result["zero_confirmation"]:
        st.caption("Missing-date zero confirmation: " + result["zero_confirmation"]["owner"] + " · " + result["zero_confirmation"]["confirmed_at"])

    st.subheader("2. Was the purchase advice followed?")
    feedback = result["feedback"]
    if feedback:
        st.write(f"Owner-reported decision: **{feedback['decision']}** · {feedback['owner']} · {feedback['recorded_at']}")
        if feedback["purchased_qty"] is not None:
            st.write(f"Owner-reported quantity purchased: {feedback['purchased_qty']:g}, on {feedback['purchased_on']}.")
        if feedback["note"]:
            st.write(feedback["note"])
    else:
        st.info("No adoption record. A stock receipt alone does not prove the owner followed this advice.")
    st.write("Separate inventory receipts recorded after this snapshot: " + _fmt(result["recorded_receipts"]))
    with st.expander("Record what you decided"):
        with st.form(f"adoption_{selected}_{sku}"):
            owner = st.text_input("Recorded by")
            decision = st.selectbox("Decision", ["undecided", "followed", "modified", "not_followed"])
            has_quantity = st.checkbox("I can report how much was actually purchased")
            quantity = st.number_input("Actual quantity purchased", min_value=0.0, step=1.0)
            max_date = min(evidence.today(), pd.Timestamp(snapshot["forecast_end"]).date())
            purchase_date = st.date_input("Purchase date", value=max_date,
                                          min_value=pd.Timestamp(snapshot["captured_on"]).date(), max_value=max_date)
            note = st.text_area("Decision note")
            if st.form_submit_button("Save adoption record"):
                try:
                    evidence.record_feedback(selected, sku, decision, quantity if has_quantity else None,
                                             purchase_date, owner, note)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        st.caption("This records your report only. It does not add inventory or duplicate stock receipts.")

    st.subheader("3. Was a stockout recorded?")
    if result["stockout_reports"]:
        st.write(f"Owner-recorded stockout entries: {len(result['stockout_reports'])}.")
        st.dataframe(pd.DataFrame(result["stockout_reports"])[["observed_on", "owner", "note"]], hide_index=True)
    if result["zero_stock_observations"]:
        st.write(f"Inventory observations at zero: {len(result['zero_stock_observations'])}. "
                 "A calculated zero balance does not independently prove lost sales.")
    if not result["stockout_reports"] and not result["zero_stock_observations"]:
        st.info("No recorded evidence of a stockout. This does not prove that no stockout happened.")
    with st.expander("Record a stockout you observed"):
        with st.form(f"stockout_{selected}_{sku}"):
            observed = st.date_input("Stockout date", value=max_date,
                                     min_value=pd.Timestamp(snapshot["captured_on"]).date(), max_value=max_date)
            owner = st.text_input("Observed by")
            note = st.text_area("Stockout note")
            if st.form_submit_button("Save stockout evidence"):
                try:
                    evidence.record_stockout(selected, sku, observed, owner, note)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    st.subheader("4. Did stock coverage change?")
    st.write("Coverage at capture: " + _fmt(item["cover_days"]) + " days")
    if result["after_cover_days"] is None:
        st.info("No suitable follow-up physical count, or no usable baseline demand. Record a stock count on Inventory to build evidence.")
    else:
        st.write(f"Coverage at follow-up count ({result['latest_count']['observed_on']}): {result['after_cover_days']:.1f} days.")
        if result["coverage_change"] is not None:
            st.write(f"Change: {result['coverage_change']:+.1f} days.")
        st.caption("Both figures use the original estimated daily demand so the denominator stays fixed. "
                   "This is an inventory indicator, not proof that the advice caused an improvement.")
    st.info("This page assesses forecast performance and recorded actions. It does not calculate money saved. "
            "Savings require actual purchasing costs, waste, margins and other operating records.")
