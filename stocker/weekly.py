"""A factual seven-day shop review, generated locally without an AI key.

Missing sales dates are gaps, not confirmed zero-sales days. Stock alerts describe
the current inventory; they are never presented as historical stock-out counts.
"""
from datetime import date

import pandas as pd

from . import advisor, batches, db, events


def _period(sales, start, end, calendar):
    rows = sales[sales["date"].between(start, end)]
    dates = pd.date_range(start, end)
    totals = rows.groupby("date")[["qty", "revenue"]].sum().reindex(dates)
    recorded = set(rows["date"].dt.normalize())
    closed = set(calendar[(calendar["kind"] == "Closed") & (calendar["scope"] == events.ALL)]["date"])
    covered = recorded | closed
    for day in dates:
        if day not in recorded and day in closed:
            totals.loc[day] = [0.0, 0.0]
    totals["record"] = ["Recorded" if d in recorded else "Marked closed" if d in closed else "No record" for d in dates]
    missing = [d.strftime("%Y-%m-%d") for d in dates if d not in covered]
    return {"start": start, "end": end, "revenue": float(rows["revenue"].sum()),
            "units": float(rows["qty"].sum()), "recorded_days": sum(d in recorded for d in dates),
            "covered_days": sum(d in covered for d in dates), "missing_dates": missing,
            "daily": totals.rename_axis("date").reset_index(), "rows": rows}


def build(end=None, *, today=None) -> dict:
    today = pd.Timestamp(today if today is not None else date.today()).normalize()
    sales = db.sales()
    sales = sales[sales["date"] <= today]          # future-dated entries are never actual sales here
    completed = sales[sales["date"] < today]
    if end is None:
        end = completed["date"].max() if not completed.empty else today - pd.Timedelta(days=1)
    end = pd.Timestamp(end).normalize()
    if pd.isna(end) or end >= today:
        raise ValueError("Choose a completed day before today for a seven-day review.")
    calendar = events.load()
    current = _period(sales, end - pd.Timedelta(days=6), end, calendar)
    previous = _period(sales, end - pd.Timedelta(days=13), end - pd.Timedelta(days=7), calendar)
    complete = current["covered_days"] == previous["covered_days"] == 7
    change = ((current["revenue"] / previous["revenue"] - 1) * 100
              if complete and previous["revenue"] > 0 else None)
    names = db.query("SELECT sku,name,category FROM products")
    sellers = (current["rows"].groupby("sku", as_index=False).agg(units=("qty", "sum"), revenue=("revenue", "sum"))
               .merge(names, on="sku", how="left").sort_values(["revenue", "units"], ascending=False))
    sellers["name"] = sellers["name"].fillna(sellers["sku"])
    risks = advisor.risks_frame()
    dated = batches.inventory(today)
    coverage = batches.coverage()
    expiry = dated[dated["days_left"] <= 7]
    coming = calendar[calendar["date"].between(today, today + pd.Timedelta(days=6))]
    rec = advisor.recommendations()
    buy = rec[(rec["action"] == "Stock more") & (rec["order_high"] > 0)] if not rec.empty else rec
    overstock = risks[risks["kind"].isin(["Overstock", "Dead stock", "Expiry risk"])] if not risks.empty else risks
    stockouts = risks[risks["kind"].isin(["Out of stock", "Stock-out risk"])] if not risks.empty else risks
    waste = db.query("SELECT e.qty,e.unit_cost FROM batch_events e WHERE e.kind='waste' AND date(e.ts) BETWEEN ? AND ?",
                     (current["start"].date().isoformat(), end.date().isoformat()))
    waste_units = float(waste["qty"].sum())
    waste_cost = float((waste["qty"] * pd.to_numeric(waste["unit_cost"], errors="coerce")).sum())
    unknown_waste_cost = int(waste["unit_cost"].isna().sum())
    notes = []
    if current["missing_dates"] or previous["missing_dates"]:
        notes.append("Some dates have no sales record. Totals include recorded sales only; missing dates are not treated as zero and the percentage comparison is withheld.")
    elif previous["revenue"] == 0:
        notes.append("The previous period has zero recorded revenue, so a percentage change is not meaningful.")
    latest = sales["date"].max() if not sales.empty else None
    if latest is None:
        notes.append("No sales have been imported yet. Batch dates can still be managed independently.")
    elif (today - latest).days > 3:
        notes.append(f"The latest sales record is from {latest:%d %b %Y}. Import recent sales before using current buying suggestions.")
    undated = float(coverage["undated_stock"].sum())
    unknown_stock = int(coverage["current_stock"].isna().sum())
    if undated > 0 or unknown_stock:
        notes.append(f"{undated:g} counted units have no batch date; {unknown_stock} product(s) have no stock count. Dated-batch alerts cannot cover all stock yet.")
    notes.append("A covered day has at least one shop sales record or is marked closed for the whole shop. This does not prove that every product's sales were imported.")
    notes.append("Stock alerts, batches and next steps below use current records, not reconstructed inventory for the reviewed week. No historical lost-sales figure is inferred.")
    highlights = [f"Recorded revenue was S${current['revenue']:,.2f} from {current['units']:g} units across {current['recorded_days']} recorded days."]
    if change is not None:
        highlights.append(f"Revenue {'rose' if change >= 0 else 'fell'} {abs(change):.1f}% compared with the previous seven days.")
    if not sellers.empty:
        first = sellers.iloc[0]
        highlights.append(f"Top seller by revenue: {first['name']} — S${first['revenue']:,.2f}, {first['units']:g} units.")
    actions = []
    if latest is None or (today - latest).days > 3:
        actions.append("Import recent sales before deciding the next order; the current records are missing or out of date.")
    expired = expiry[expiry["days_left"] < 0]
    if not expired.empty:
        actions.append(f"Remove {expired['remaining_qty'].sum():g} expired units in {len(expired)} batches from sale; record waste or arrange a supplier return.")
    due = expiry[expiry["days_left"] >= 0]
    if not due.empty:
        actions.append(f"Check {len(due)} batches ({due['remaining_qty'].sum():g} units) due within seven days, and put the earliest dates first.")
    if not buy.empty:
        products = ", ".join(buy.head(3)["product"].astype(str))
        actions.append(f"Review replenishment for {products}. Check the full shopping list before deciding quantities.")
    if not overstock.empty:
        products = ", ".join(overstock.drop_duplicates("sku").head(3)["product"].astype(str))
        actions.append(f"Review excess or slow-moving stock: {products}. Consider a smaller next order or a suitable promotion.")
    if not coming.empty:
        upcoming_names = ", ".join(coming["name"].drop_duplicates().head(3).astype(str))
        actions.append(f"Prepare for upcoming calendar events: {upcoming_names}. Check the forecast and available stock.")
    if undated > 0 or unknown_stock:
        actions.append("Count stock and add missing batch dates so expiry checks cover more of the shop.")
    if not actions:
        actions.append("No specific stock action was found from the available records. Keep sales and stock counts up to date.")
    return {"as_of": today, "latest_sale": latest, "current": current, "previous": previous,
            "change_pct": change, "top_sellers": sellers, "stockouts_now": stockouts,
            "overstock_now": overstock, "expiry_now": expiry, "buy_now": buy, "events_next": coming,
            "waste_units": waste_units, "waste_cost": waste_cost, "unknown_waste_cost": unknown_waste_cost,
            "notes": notes, "highlights": highlights, "actions": actions}


def summary_table(report=None) -> pd.DataFrame:
    r = report if report is not None else build()
    c, p = r["current"], r["previous"]
    return pd.DataFrame([
        {"Metric": "Recorded revenue (S$)", "Selected 7 days": c["revenue"], "Previous 7 days": p["revenue"]},
        {"Metric": "Recorded units", "Selected 7 days": c["units"], "Previous 7 days": p["units"]},
        {"Metric": "Days covered (records or marked closed)", "Selected 7 days": c["covered_days"], "Previous 7 days": p["covered_days"]},
    ])


def _md_table(df):
    def cell(v):
        return ("—" if pd.isna(v) else str(v)).replace("|", "\\|").replace("\n", " ")
    return "\n".join(["| " + " | ".join(map(str, df.columns)) + " |", "| " + " | ".join(["---"] * len(df.columns)) + " |"]
                     + ["| " + " | ".join(cell(v) for v in row) + " |" for row in df.itertuples(index=False, name=None)])


def markdown(r: dict) -> str:
    c, p = r["current"], r["previous"]
    lines = ["# Stocker AI · Weekly shop review", "",
             f"Sales period: {c['start']:%Y-%m-%d} to {c['end']:%Y-%m-%d} (7 calendar days).",
             f"Comparison: {p['start']:%Y-%m-%d} to {p['end']:%Y-%m-%d}.",
             f"Current inventory checked on {r['as_of']:%Y-%m-%d}. Generated from saved records; no AI key required.", "",
             "## At a glance", ""]
    lines += ["- " + s for s in r["highlights"]]
    lines += ["", _md_table(summary_table(r)), "", "## Top sellers by revenue", ""]
    top = r["top_sellers"].head(10)[["name", "units", "revenue"]].rename(columns={"name": "Product", "units": "Units", "revenue": "Revenue (S$)"}).round(2)
    lines += [_md_table(top) if not top.empty else "No recorded sales in this period."]
    lines += ["", "## Daily records", ""]
    daily = c["daily"].copy()
    daily["date"] = daily["date"].dt.strftime("%Y-%m-%d")
    lines += [_md_table(daily.round(2)), "", "## Recorded batch waste in this period", "",
              f"{r['waste_units']:g} units; known cost S${r['waste_cost']:,.2f}. {r['unknown_waste_cost']} waste record(s) have unknown cost. This counts batch waste entries only.",
              "", "## Stock to check now", "",
              f"Current stock-out alerts: {len(r['stockouts_now'])}; current excess/slow-stock alerts: {len(r['overstock_now'])}."]
    if not r["expiry_now"].empty:
        b = r["expiry_now"][["product", "batch_code", "remaining_qty", "expires_on", "status"]].copy()
        b["expires_on"] = b["expires_on"].dt.strftime("%Y-%m-%d")
        lines += ["", _md_table(b)]
    else:
        lines += ["No dated batch is overdue or due within the next seven days. Undated stock is not covered."]
    lines += ["", "## Next seven days: suggested actions", ""] + ["- " + a for a in r["actions"]]
    if not r["events_next"].empty:
        ev = r["events_next"][["date", "name", "scope"]].copy()
        ev["date"] = ev["date"].dt.strftime("%Y-%m-%d")
        lines += ["", _md_table(ev)]
    lines += ["", "## Data notes", ""] + ["- " + n for n in r["notes"]]
    return "\n".join(lines) + "\n"
