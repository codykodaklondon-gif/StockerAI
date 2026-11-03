"""Customer profiling from anonymous, aggregate purchase behaviour.

Uses only behavioural signals (time of day, weekday, member vs walk-in, basket,
visit frequency). No names, phone numbers or demographic labels are used; customer
ids are hashed on import. Every figure is computed here, not by the AI.
"""

import numpy as np
import pandas as pd

from . import db

SLOTS = [(6, 11, "Morning"), (11, 14, "Lunch"), (14, 17, "Afternoon"), (17, 21, "Evening"), (21, 30, "Late")]
SLOT_ORDER = [s for *_, s in SLOTS] + ["Early"]


def slot(hour) -> str | None:
    if pd.isna(hour):
        return None
    for lo, hi, name in SLOTS:
        if lo <= hour < hi:
            return name
    return "Early"


def load(days: int = 56) -> pd.DataFrame:
    """Line-level data for the analysis window; falls back to daily totals."""
    d = db.detail(days)
    if d.empty:
        d = db.sales().merge(db.products()[["sku", "name", "category"]], on="sku", how="left")
        for c in ("hour", "customer_type", "txn", "customer"):
            d[c] = None
    if d.empty:
        return d
    d = d[d["date"] > d["date"].max() - pd.Timedelta(days=days)].copy()
    d["slot"] = d["hour"].map(slot)
    d["weekend"] = d["date"].dt.day_of_week >= 5
    return d


def capabilities(d: pd.DataFrame) -> dict:
    return {k: bool(v) for k, v in {
        "time": d["hour"].notna().any(),
        "customer_type": d["customer_type"].notna().any() and d["customer_type"].astype(str).str.len().gt(0).any(),
        "basket": d["txn"].notna().any(),
        "customers": d["customer"].notna().any() and d["customer"].astype(str).str.len().gt(0).any(),
    }.items()}


# ------------------------------------------------------------------ building blocks

def time_profile(d: pd.DataFrame) -> pd.DataFrame:
    t = d.dropna(subset=["hour"]).groupby("hour").agg(units=("qty", "sum"), revenue=("revenue", "sum"))
    days = d["date"].nunique() or 1
    return (t / days).round(2)


def slot_by_category(d: pd.DataFrame) -> pd.DataFrame:
    """Share of each category's units sold in each time slot."""
    t = d.dropna(subset=["slot"]).pivot_table(index="category", columns="slot", values="qty", aggfunc="sum", fill_value=0)
    t = t[[c for c in SLOT_ORDER if c in t.columns]]
    return t.div(t.sum(axis=1), axis=0).round(3)


def weekday_weekend(d: pd.DataFrame) -> pd.DataFrame:
    """Average daily units per category, weekday vs weekend, and the lift."""
    days = d.groupby("weekend")["date"].nunique()
    t = d.groupby(["category", "weekend"])["qty"].sum().unstack(fill_value=0)
    t = t.div(days, axis=1).rename(columns={False: "weekday", True: "weekend"})
    for c in ("weekday", "weekend"):
        if c not in t:
            t[c] = 0.0
    t["lift"] = (t["weekend"] / t["weekday"].replace(0, np.nan)).round(2)
    return t.round(1).sort_values("lift", ascending=False)


def customer_types(d: pd.DataFrame) -> pd.DataFrame:
    x = d[d["customer_type"].notna() & (d["customer_type"].astype(str) != "")]
    g = x.groupby("customer_type").agg(revenue=("revenue", "sum"), units=("qty", "sum"),
                                       receipts=("txn", "nunique"))
    g["revenue_share"] = (g["revenue"] / g["revenue"].sum()).round(3)
    g["avg_basket"] = (g["revenue"] / g["receipts"].replace(0, np.nan)).round(2)
    top = (x.groupby(["customer_type", "name"])["revenue"].sum().reset_index()
             .sort_values("revenue", ascending=False).groupby("customer_type").head(3)
             .groupby("customer_type")["name"].apply(", ".join))
    g["top_products"] = top
    return g.round(2)


def baskets(d: pd.DataFrame) -> dict:
    x = d.dropna(subset=["txn"])
    if x.empty:
        return {}
    b = x.groupby("txn").agg(items=("qty", "sum"), lines=("sku", "nunique"), value=("revenue", "sum"))
    # product pairs per receipt via a self-join (one row per distinct product per receipt, a < c)
    u = x[["txn", "name"]].drop_duplicates()
    u = u[u["txn"].isin(b.index[b["lines"] > 1])]
    j = u.merge(u, on="txn", suffixes=("_a", "_c"))
    j = j[j["name_a"] < j["name_c"]]
    pairs = j.groupby(["name_a", "name_c"]).size().to_dict()
    n = len(b)
    min_support = max(20, int(0.001 * n))   # ignore combinations seen on only a handful of receipts
    support = {k: v for k, v in pairs.items() if v >= min_support}
    item_count = x.groupby("name")["txn"].nunique()
    # lift > 1 means bought together more often than chance would predict
    scored = [{"pair": f"{a} + {c}", "receipts": v, "lift": round(v * n / (item_count[a] * item_count[c]), 2)}
              for (a, c), v in support.items()]
    top_pairs = sorted([s for s in scored if s["lift"] >= 1.2], key=lambda s: -s["lift"])[:8]
    return {"receipts": n, "avg_items": round(b["items"].mean(), 2), "avg_value": round(b["value"].mean(), 2),
            "multi_item_share": round((b["lines"] > 1).mean(), 3), "pairs": top_pairs}


def segments(d: pd.DataFrame) -> pd.DataFrame:
    """Rule-based behavioural segments of anonymous customers (recency / frequency / spend)."""
    x = d[d["customer"].notna() & (d["customer"].astype(str) != "")]
    if x.empty:
        return pd.DataFrame()
    end = d["date"].max()
    visits = x.groupby("customer").agg(first=("date", "min"), last=("date", "max"),
                                       visits=("date", "nunique"), spend=("revenue", "sum"))
    weeks = max((end - d["date"].min()).days / 7, 1)
    visits["per_week"] = visits["visits"] / weeks
    recency = (end - visits["last"]).dt.days

    def label(r, rec):
        if rec > 21:
            return "Lapsed"
        if (end - r["first"]).days <= 14:
            return "New"
        if r["per_week"] >= 1.5:
            return "Regular"
        return "Occasional"
    visits["segment"] = [label(r, rec) for (_, r), rec in zip(visits.iterrows(), recency)]
    s = visits.groupby("segment").agg(customers=("visits", "size"), avg_visits_per_week=("per_week", "mean"),
                                      avg_spend=("spend", "mean"), total_spend=("spend", "sum"))
    s["spend_share"] = s["total_spend"] / s["total_spend"].sum()
    order = ["Regular", "Occasional", "New", "Lapsed"]
    return s.reindex([o for o in order if o in s.index]).round(2)


def repeat_rate(d: pd.DataFrame, days: int = 30) -> float | None:
    x = d[d["customer"].notna() & (d["customer"].astype(str) != "")]
    x = x[x["date"] > x["date"].max() - pd.Timedelta(days=days)] if not x.empty else x
    if x.empty:
        return None
    v = x.groupby("customer")["date"].nunique()
    return float((v > 1).mean())


# ------------------------------------------------------------------ summary

def build(days: int = 56) -> dict:
    d = load(days)
    if d.empty:
        return {"available": False}
    cap = capabilities(d)
    out = {"available": True, "window_days": days, "capabilities": cap,
           "weekday_weekend": weekday_weekend(d)}
    if cap["time"]:
        out["by_hour"] = time_profile(d)
        out["slot_by_category"] = slot_by_category(d)
    if cap["customer_type"]:
        out["customer_types"] = customer_types(d)
    if cap["basket"]:
        out["baskets"] = baskets(d)
    if cap["customers"]:
        out["segments"] = segments(d)
        out["repeat_rate"] = repeat_rate(d)
    out["insights"] = insights(out)
    return out


def insights(p: dict) -> list[str]:
    notes = []
    if "by_hour" in p and not p["by_hour"].empty:
        h = p["by_hour"]["revenue"]
        peak = int(h.idxmax())
        top3 = h.sort_values(ascending=False).head(3).sum() / h.sum()
        notes.append(f"Busiest hour is {peak}:00–{peak + 1}:00; the top 3 hours bring {top3:.0%} of daily revenue.")
    if "slot_by_category" in p:
        s = p["slot_by_category"]
        single = len(s) == 1          # no real categories in the data (everything is "General")
        for cat, row in s.iterrows():
            if row.max() >= 0.5:
                who = "Overall" if single else cat
                notes.append(f"{who}: {row.max():.0%} of units sell in the {row.idxmax().lower()} — "
                             f"{'have stock ready' if single else 'stock it'} before then.")
    ww = p.get("weekday_weekend")
    if ww is not None and not ww.empty and ww["lift"].notna().any():
        top = ww["lift"].idxmax()
        if ww.loc[top, "lift"] >= 1.15:
            who = "Your shop" if len(ww) == 1 else top
            notes.append(f"{who} sells {ww.loc[top, 'lift']:.2f}x more per day on weekends.")
    ct = p.get("customer_types")
    if ct is not None and len(ct) >= 2:
        lead = ct["revenue_share"].idxmax()
        notes.append(f"{lead} customers bring {ct.loc[lead, 'revenue_share']:.0%} of revenue"
                     + (f", average basket ${ct.loc[lead, 'avg_basket']:.2f}" if pd.notna(ct.loc[lead, 'avg_basket']) else "") + ".")
        if {"Member", "Walk-in"} <= set(ct.index) and pd.notna(ct.loc["Member", "avg_basket"]) and pd.notna(ct.loc["Walk-in", "avg_basket"]):
            r = ct.loc["Member", "avg_basket"] / ct.loc["Walk-in", "avg_basket"]
            notes.append(f"Members spend {r:.2f}x a walk-in per receipt." if r >= 1 else
                         f"Walk-in receipts are {1 / r - 1:.0%} larger than members' (members visit more often but buy less each time).")
    b = p.get("baskets") or {}
    if b.get("pairs"):
        top = b["pairs"][0]
        notes.append(f"{top['pair']} are bought together {top['lift']:.1f}x more often than chance "
                     f"({top['receipts']} receipts) — keep both in stock and shelve them near each other.")
    seg = p.get("segments")
    if seg is not None and "Regular" in getattr(seg, "index", []):
        notes.append(f"{int(seg.loc['Regular', 'customers'])} regular customers generate "
                     f"{seg.loc['Regular', 'spend_share']:.0%} of member spending.")
    if p.get("repeat_rate") is not None:
        notes.append(f"{p['repeat_rate']:.0%} of identified customers came back more than once in the last 30 days.")
    return notes
