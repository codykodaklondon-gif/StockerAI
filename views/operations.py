"""Expiry management and the weekly review, using the neighbourhood UI."""
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import ui
from stocker import batches, db, exporter, weekly


def batch_table(frame):
    out = frame[["product", "sku", "batch_code", "remaining_qty", "received_on", "expires_on",
                 "days_left", "status", "stock_cost", "note"]].copy()
    for c in ("received_on", "expires_on"):
        out[c] = out[c].dt.strftime("%Y-%m-%d")
    out["stock_cost"] = out["stock_cost"].round(2)
    return out.rename(columns={"product": "Product", "sku": "Code", "batch_code": "Batch",
                               "remaining_qty": "Units left", "received_on": "Arrived", "expires_on": "Expires",
                               "days_left": "Days left", "status": "Status", "stock_cost": "Stock cost (S$)", "note": "Note"})


def _saved(message):
    st.session_state.operations_notice = message
    st.rerun()


def expiry_page():
    ui.heading("Expiry & batches", "Keep the freshest stock moving.",
               "Record the date on each batch, see what is due soon, and keep track of what leaves the shelf.")
    if "operations_notice" in st.session_state:
        st.success(st.session_state.pop("operations_notice"))
    prods = db.products()
    if prods.empty:
        st.info("Import your product list first. You can manage batches even before there is enough sales history to forecast.")
        st.button("Import sales →", on_click=ui.go_to, args=("📥 Import data",))
        return
    days = int(db.get_setting("expiry_warning_days", 7))
    with st.expander("Reminder window"):
        with st.form("expiry_settings"):
            new_days = st.number_input("Remind me this many days before expiry", 1, 30, min(30, max(1, days)))
            if st.form_submit_button("Save reminder window"):
                db.set_setting("expiry_warning_days", int(new_days))
                _saved("Reminder window saved. Current alerts now use this setting.")
    all_batches = batches.inventory(days=days)
    overdue = all_batches[all_batches["days_left"] < 0]
    due = all_batches[all_batches["days_left"].between(0, days)]
    cov = batches.coverage()
    cols = st.columns(3)
    cols[0].metric("Expired units to remove", f"{overdue['remaining_qty'].sum():g}")
    cols[1].metric(f"Units due within {days} days", f"{due['remaining_qty'].sum():g}")
    cols[2].metric("Counted units without a date", f"{cov['undated_stock'].sum():g}")
    unknown = int(cov["current_stock"].isna().sum())
    st.caption(f"Checked on {date.today():%d %b %Y}. {unknown} product(s) still need a stock count. "
               "Expiry dates come from your entries; no date is guessed from shelf life. Check the label and storage conditions.")
    if not overdue.empty:
        st.error("Expired units remain in the stock records. Remove them from sale and record waste or arrange a supplier return.")
    choices = ["Needs attention", "All remaining stock", "Expired", "Due soon", "History (including empty batches)"]
    view_choice = st.selectbox("Show batches", choices)
    search = st.text_input("Find a product or batch", placeholder="Product name, code or batch code")
    view = batches.inventory(days=days, include_empty=True) if view_choice.startswith("History") else all_batches
    if view_choice == "Needs attention":
        view = view[view["days_left"] <= days]
    elif view_choice == "Expired":
        view = view[view["days_left"] < 0]
    elif view_choice == "Due soon":
        view = view[view["days_left"].between(0, days)]
    if search.strip():
        text = search.strip().casefold()
        view = view[view[["product", "sku", "batch_code"]].astype(str).apply(
            lambda col: col.str.casefold().str.contains(text, regex=False)).any(axis=1)]
    if view.empty:
        st.info("No batches match this view. Add dates below, or choose another view. Stock without a batch date is not included in expiry checks.")
    else:
        for b in view.head(6).itertuples():
            with st.container(border=True):
                ui.badge(b.status, "red" if b.days_left < 0 else "coral" if b.days_left <= 1 else "amber" if b.days_left <= days else "sage")
                ui.product_heading(b.product, f"Batch {b.batch_code}")
                st.write(f"**{b.remaining_qty:g} units** · Expires **{b.expires_on:%d %b %Y}**")
                st.caption("Remove from sale; record waste or contact the supplier." if b.days_left < 0 else
                           "Put the earliest expiry dates first. Review any promotion before the expiry date.")
        if len(view) > 6:
            st.caption(f"Showing 6 cards. All {len(view)} matching batches are in the table.")
        table = batch_table(view)
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={"Stock cost (S$)": st.column_config.NumberColumn(format="S$%.2f")})
        st.download_button("Download these batches (CSV)", exporter.to_csv(table), "expiry_batches.csv", "text/csv")
    st.divider()
    st.subheader("Add a batch")
    st.caption("Label existing stock to add its dates without increasing inventory. Use New delivery only when extra stock has actually arrived.")
    labels = {r.sku: f"{r.name} ({r.sku})" for r in prods.itertuples()}
    sku = st.selectbox("Product for the new batch", list(labels), format_func=labels.get)
    pos = cov.set_index("sku").loc[sku]
    st.caption(f"Current count: {pos.current_stock:g} · Available to label: {pos.undated_stock:g} units"
               if pd.notna(pos.current_stock) else "Current stock is unknown. Enter a stock count on Stock & products first.")
    with st.form("add_expiry_batch", clear_on_submit=True):
        mode = st.radio("What are you recording?", ["Label existing stock", "New delivery"], horizontal=True)
        a, b = st.columns(2)
        code = a.text_input("Batch code", max_chars=80, placeholder="e.g. MILK-1102-A")
        qty = b.number_input("Units in this batch", min_value=0.0, step=1.0)
        received = a.date_input("Arrival date", value=date.today(), max_value=date.today())
        expiry = b.date_input("Expiry date on the label", value=date.today() + timedelta(days=7),
                              min_value=date(2000, 1, 1), max_value=date.today() + timedelta(days=3650))
        note = st.text_input("Batch note (optional)", placeholder="Shelf location or supplier")
        if st.form_submit_button("Save batch", type="primary"):
            try:
                batches.add(sku, code, qty, received, expiry, mode="delivery" if mode == "New delivery" else "existing", note=note)
            except ValueError as e:
                st.error(str(e))
            else:
                _saved("Batch saved. " + ("Stock increased once for this delivery." if mode == "New delivery" else "The existing stock total is unchanged."))
    if not all_batches.empty:
        with st.expander("Record a batch sale, waste or return"):
            opts = {int(r.id): f"{r.product} · {r.batch_code} · {r.remaining_qty:g} left" for r in all_batches.itertuples()}
            bid = st.selectbox("Batch to update", list(opts), format_func=opts.get, key="consume_batch")
            st.caption("A sale reduces this batch and total stock and adds today's sales. Waste and supplier returns reduce stock only; returns are kept separate from waste. Do not record sales again if they are already in an imported file.")
            with st.form("consume_expiry_batch"):
                kind = st.radio("Movement", ["Sold", "Waste / discarded", "Returned to supplier"], horizontal=True)
                amount = st.number_input("Units leaving this batch", min_value=0.0, step=1.0)
                reason = st.text_input("Movement note", placeholder="e.g. expired / damaged / returned to supplier")
                if st.form_submit_button("Record batch movement", type="primary"):
                    try:
                        batches.reduce(bid, {"Sold": "out", "Waste / discarded": "waste", "Returned to supplier": "return"}[kind], amount, reason)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        _saved("Batch and total stock updated together.")
        with st.expander("Correct a batch date or code"):
            opts = {int(r.id): f"{r.product} · {r.batch_code}" for r in all_batches.itertuples()}
            bid = st.selectbox("Batch to correct", list(opts), format_func=opts.get, key="edit_batch")
            old = all_batches.set_index("id").loc[bid]
            with st.form(f"batch_details_{bid}"):
                code = st.text_input("Correct batch code", value=old.batch_code)
                received = st.date_input("Correct arrival date", value=old.received_on.date(), max_value=date.today())
                expiry = st.date_input("Correct expiry date", value=old.expires_on.date(), min_value=date(2000, 1, 1),
                                       max_value=date.today() + timedelta(days=3650))
                note = st.text_input("Correct note", value=old.note)
                if st.form_submit_button("Save batch details"):
                    try:
                        batches.update_dates(bid, code, received, expiry, note)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        _saved("Batch details corrected. Stock quantities are unchanged.")
    with st.expander("Stock covered by batch dates"):
        st.dataframe(cov.rename(columns={"name": "Product", "current_stock": "Total stock", "dated_stock": "Dated units",
                                         "undated_stock": "Units without a date"}), hide_index=True, width="stretch")
        st.caption("A general stock-out movement consumes the earliest valid batch first. Stock-count reductions use undated stock first, then correct dated balances. Historical sales-file imports never deduct stock again.")
    with st.expander("Batch movement history"):
        st.dataframe(batches.history(), hide_index=True, width="stretch")


@st.cache_data(max_entries=24, show_spinner="Preparing your weekly review…")
def _weekly_cached(key, end):
    return weekly.build(end)


def weekly_page():
    ui.heading("Weekly review", "A little time to look back.",
               "See what sold, what needs attention now, and what to prepare for the next seven days.")
    yesterday = date.today() - timedelta(days=1)
    last = db.query("SELECT MAX(date) d FROM sales WHERE date<=?", (yesterday.isoformat(),))["d"].iloc[0]
    default = pd.Timestamp(last).date() if last is not None else yesterday
    end = st.date_input("Review the seven days ending on", value=default, max_value=yesterday,
                        min_value=min(date(2000, 1, 1), default), key="weekly_end")
    path = db.current()
    r = _weekly_cached((str(path), path.stat().st_mtime_ns, date.today().isoformat()), end)
    c, p = r["current"], r["previous"]
    st.caption(f"Sales: {c['start']:%d %b %Y}–{c['end']:%d %b %Y} · Compared with {p['start']:%d %b}–{p['end']:%d %b %Y}. "
               "Seven calendar days, not seven rows. Today is excluded because it is still in progress.")
    a, b, d = st.columns(3)
    a.metric("Recorded revenue", f"S${c['revenue']:,.2f}", f"{r['change_pct']:+.1f}% vs prior 7 days" if r["change_pct"] is not None else None)
    b.metric("Recorded units sold", f"{c['units']:g}")
    d.metric("Days covered", f"{c['covered_days']} / 7")
    if c["covered_days"] < 7 or p["covered_days"] < 7:
        st.warning("There are gaps in the sales records. These are recorded totals, not a complete-week estimate. A missing day is not assumed to mean zero sales.")
    st.subheader("This period at a glance")
    for note in r["highlights"]:
        st.write("• " + note)
    left, right = st.columns([1.1, 1], gap="large")
    with left:
        with st.container(border=True):
            st.subheader("Sales day by day")
            daily = c["daily"]
            fig = go.Figure(go.Bar(x=daily["date"], y=daily["revenue"], marker_color="#A6BA8F", name="Recorded revenue"))
            fig.update_layout(height=280, yaxis_title="Revenue (S$)")
            st.plotly_chart(ui.chart_style(fig), width="stretch")
            st.caption("A gap means no record. A zero is shown only for a recorded zero or a shop-wide day marked closed.")
    with right:
        st.subheader("Top sellers by revenue")
        if r["top_sellers"].empty:
            st.info("No sales records in this period.")
        else:
            st.dataframe(r["top_sellers"].head(5)[["name", "units", "revenue"]].rename(columns={"name": "Product", "units": "Units", "revenue": "Revenue (S$)"}).round(2),
                         hide_index=True, width="stretch",
                         column_config={"Revenue (S$)": st.column_config.NumberColumn(format="S$%.2f")})
    st.subheader("Stock to check now")
    st.caption(f"Current stock on {r['as_of']:%d %b %Y}. Historical stock-outs cannot be reconstructed from sales alone.")
    a, b, d = st.columns(3)
    a.metric("Stock-out alerts now", len(r["stockouts_now"]))
    b.metric("Excess / slow-stock alerts now", len(r["overstock_now"]))
    d.metric("Expired / soon-due batches now", len(r["expiry_now"]))
    if not r["expiry_now"].empty:
        st.dataframe(batch_table(r["expiry_now"]), hide_index=True, width="stretch",
                     column_config={"Stock cost (S$)": st.column_config.NumberColumn(format="S$%.2f")})
    if not r["overstock_now"].empty:
        with st.expander("Excess and slow-moving products"):
            st.dataframe(r["overstock_now"][["product", "kind", "message", "action"]], hide_index=True, width="stretch")
    st.subheader("A few steps for the next seven days")
    for i, action in enumerate(r["actions"], start=1):
        with st.container(border=True):
            st.write(f"**{i}.** {action}")
    a, b = st.columns(2)
    a.button("Manage expiry dates →", on_click=ui.go_to, args=("🥬 Expiry",), width="stretch")
    b.button("Open the shopping list →", on_click=ui.go_to, args=("🛒 Stocking list",), width="stretch")
    with st.expander("Recorded waste and data notes"):
        st.write(f"Batch waste recorded in the reviewed period: **{r['waste_units']:g} units**, known cost **S${r['waste_cost']:,.2f}**.")
        st.caption(f"{r['unknown_waste_cost']} waste record(s) have unknown cost. This is batch waste only, not an estimate of all shop losses.")
        for note in r["notes"]:
            st.write("• " + note)
        st.dataframe(c["daily"], hide_index=True, width="stretch")
        st.write("Missing dates this period: " + (", ".join(c["missing_dates"]) or "None"))
        st.write("Missing dates in comparison period: " + (", ".join(p["missing_dates"]) or "None"))
    st.subheader("Take the review with you")
    stem = f"weekly_review_{end:%Y-%m-%d}"
    a, b = st.columns(2)
    a.download_button("Download weekly review (MD)", weekly.markdown(r).encode("utf-8"), f"{stem}.md", "text/markdown", width="stretch")
    b.download_button("Download review tables (Excel)", exporter.to_xlsx({
        "Review dates": pd.DataFrame({"Item": ["Sales start", "Sales end", "Comparison start", "Comparison end", "Current inventory checked", "Latest sale"],
                                      "Date": [c["start"].date().isoformat(), c["end"].date().isoformat(),
                                               p["start"].date().isoformat(), p["end"].date().isoformat(),
                                               r["as_of"].date().isoformat(),
                                               r["latest_sale"].date().isoformat() if r["latest_sale"] is not None else "No records"]}),
        "Period totals": weekly.summary_table(r), "Daily records": c["daily"],
        "Top sellers": r["top_sellers"].drop(columns="category", errors="ignore"),
        "Expiry now": batch_table(r["expiry_now"]),
        "Next steps": pd.DataFrame({"Action": r["actions"]}), "Data notes": pd.DataFrame({"Note": r["notes"]})}),
        f"{stem}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", width="stretch")


def dashboard_summary():
    with st.container(border=True):
        left, right = st.columns([2, 1], vertical_alignment="center")
        dated = batches.inventory()
        due = dated[dated["days_left"] <= 7]
        with left:
            st.markdown(":material/event_available: **A little look ahead — and back**")
            st.caption(f"{len(due)} dated batches are expired or due within 7 days. "
                       "Open your weekly review for sales highlights and next steps.")
        with right:
            st.button("Expiry & batches", on_click=ui.go_to, args=("🥬 Expiry",), width="stretch", key="home_expiry")
            st.button("Weekly review", on_click=ui.go_to, args=("📒 Weekly",), width="stretch", key="home_weekly")
