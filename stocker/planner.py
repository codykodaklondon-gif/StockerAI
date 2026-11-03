"""Personalised purchase plan from the owner's own words ("Tell me your needs").

Flow:
  1. context()  — the shop's real data: products, costs, stock, shelf life, forecasts,
                  the standard recommendations, risks and customer-habit insights.
  2. the AI     — reads the owner's free text plus that context and returns a JSON plan:
                  a needs summary, items (product + quantity + reason), assumptions and questions.
  3. validate() — the program, not the AI, does every number that matters: unknown products are
                  dropped, costs come from the database, the budget is enforced, and perishables
                  are checked against shelf life. Every change is reported to the owner.
"""
import json
import math

import numpy as np
import pandas as pd

from . import advisor, agent, db, engine, profile

MAX_PRODUCTS = 60           # products sent to the model (by sales volume)
PERISHABLE_DAYS = 10        # shelf life at or below this counts as perishable (bread, milk, cakes)
WASTE_SLACK = 1.2           # stock + order above 120% of what sells within shelf life = waste risk

SYSTEM = """You are Stocker AI, helping a small shop owner plan what to buy.
You receive (1) the owner's request in their own words and (2) the shop's real data as JSON.

Do this:
1. Understand the owner: budget, planning horizon, storage/fridge limits, goals, special events, worries,
   and how experienced they are. Only record what they actually said or clearly implied.
2. Build a purchase plan using ONLY products listed in the data, referenced by their exact "sku".
   Base quantities on the forecasts, current stock and the system suggestion, then adjust them to the
   owner's needs (e.g. fewer perishables when they fear waste, more of event-relevant items when they
   want to avoid running out). Whole units only. Leave out products that should not be bought.
3. Give every item a short reason a beginner understands, citing the data (forecast, stock, shelf life,
   customer habits) and the owner's need it serves.
4. Do NOT compute money totals and do NOT invent costs, stock levels, capacities or products; the program
   computes costs from the shop's records and enforces the budget. If data is missing (no cost, no stock
   count, no fridge size), say so in "data_gaps" or "assumptions".
5. Anything you cannot quantify (e.g. fridge size not given): state the assumption you used, and ask the
   owner a short follow-up question when the answer would change the plan.
6. Write all text in the same language as the owner's request.

Return ONLY a JSON object with exactly these keys:
{
  "summary": {
    "budget_sgd": number or null,
    "horizon_days": integer (days the purchase should cover; "tomorrow" = 1; default 1 if unclear),
    "storage": string or null,
    "goals": [string], "events": [string], "worries": [string],
    "experience": string or null
  },
  "plan": [ {"sku": string, "quantity": integer, "priority": integer 1-5 (1 = most important), "reason": string} ],
  "strategy": {"budget": string, "space": string, "waste": string, "stockout": string},
  "assumptions": [string],
  "questions": [string],
  "data_gaps": [string]
}"""


def _num(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def context(horizon_days: int = 7) -> dict:
    """The shop's real data, compact enough for one model call."""
    prods = db.products().set_index("sku")
    perishable = db.perishable(prods.reset_index()).set_axis(prods.index)
    models = engine.load_models()
    start = advisor.plan_start()
    rec = advisor.recommendations()
    rec = rec.set_index("sku") if not rec.empty else rec
    risks = advisor.risks_frame()
    vol = {s: m.avg_28 for s, m in models.items()}
    skus = sorted(models, key=lambda s: -vol.get(s, 0))[:MAX_PRODUCTS]
    items = []
    for sku in skus:
        m, p = models[sku], prods.loc[sku]
        fc = m.forecast(horizon_days, start=start)
        r = rec.loc[sku] if not rec.empty and sku in rec.index else None
        items.append({
            "sku": sku, "name": p["name"], "category": p["category"],
            "unit_cost_sgd": _num(float(p["unit_cost"])) if pd.notna(p["unit_cost"]) and p["unit_cost"] > 0 else None,
            "unit_price_sgd": _num(float(p["unit_price"])) if pd.notna(p["unit_price"]) and p["unit_price"] > 0 else None,
            "stock_on_hand": _num(float(p["current_stock"])) if pd.notna(p["current_stock"]) else None,
            "shelf_life_days": int(p["shelf_life_days"]) if pd.notna(p["shelf_life_days"]) else None,
            "perishable": bool(perishable.get(sku, False)),
            "lead_time_days": int(p["lead_time_days"]) if pd.notna(p["lead_time_days"]) else None,
            "avg_daily_sales_28d": round(m.avg_28, 1),
            "forecast_by_day": [round(float(v), 1) for v in fc["forecast"]],
            "forecast_low_high_day1": [round(float(fc["low"].iloc[0]), 1), round(float(fc["high"].iloc[0]), 1)],
            "busiest_weekday_factor": round(max(m.dow), 2),
            "system_suggestion": None if r is None else {
                "action": r["action"], "order_range": [int(r["order_low"]), int(r["order_high"])]},
            "risks": [] if risks.empty else risks.loc[risks["sku"] == sku, "message"].tolist(),
        })
    prof = profile.build(56)
    sales = db.sales()
    return {
        "plan_starts": start.strftime("%Y-%m-%d (%A)"),
        "currency": "SGD",
        "latest_sales_record": sales["date"].max().strftime("%Y-%m-%d") if not sales.empty else None,
        "products": items,
        "shop_wide_alerts": [] if risks.empty else risks.loc[risks["sku"] == "-", "message"].tolist(),
        "customer_insights": prof.get("insights", []) if prof.get("available") else [],
        "notes": {
            "products_without_cost": [i["sku"] for i in items if i["unit_cost_sgd"] is None],
            "products_without_stock_count": [i["sku"] for i in items if i["stock_on_hand"] is None],
            "forecast_by_day_starts_on": start.strftime("%Y-%m-%d"),
        },
    }


def generate(request: str) -> dict:
    """Owner's words -> AI plan -> program-checked plan. Raises agent.AIError / API errors unchanged."""
    request = request.strip()
    if not request:
        raise ValueError("Please describe your needs first.")
    ctx = context()
    raw = agent.complete_json(SYSTEM, f"OWNER REQUEST:\n{request}\n\nSHOP DATA (JSON):\n{json.dumps(ctx, ensure_ascii=False)}")
    return validate(raw, ctx)


def _int(x, default=0):
    try:
        return max(0, int(round(float(x))))
    except (TypeError, ValueError):
        return default


def validate(raw: dict, ctx: dict) -> dict:
    """Turn the model's JSON into a plan whose numbers the program has checked."""
    products = {i["sku"]: i for i in ctx["products"]}
    summary = raw.get("summary") or {}
    horizon = min(max(_int(summary.get("horizon_days"), 1), 1), 7)
    budget = summary.get("budget_sgd")
    try:
        budget = float(budget) if budget is not None else None
        budget = budget if budget and budget > 0 else None
    except (TypeError, ValueError):
        budget = None
    checks, rows = [], []
    for it in raw.get("plan") or []:
        if not isinstance(it, dict):
            continue
        sku, qty = str(it.get("sku", "")).strip(), _int(it.get("quantity"))
        if sku not in products:
            checks.append(f"Removed “{sku or '?'}”: it is not a product in your records.")
            continue
        if qty == 0:
            continue
        p = products[sku]
        expected = sum(p["forecast_by_day"][:horizon])
        rows.append({"sku": sku, "product": p["name"], "category": p["category"], "quantity": qty,
                     "priority": min(max(_int(it.get("priority"), 3), 1), 5),
                     "unit_cost": p["unit_cost_sgd"], "stock": p["stock_on_hand"],
                     "expected_sales": round(expected, 1), "shelf_life": p["shelf_life_days"],
                     "reason": str(it.get("reason", "")).strip()})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.groupby("sku", as_index=False).agg(          # the model may list a product twice
            {**{c: "first" for c in df.columns if c not in ("sku", "quantity", "priority")},
             "quantity": "sum", "priority": "min"})

    # budget: enforced by the program, trimming the lowest-priority items first
    unknown = [] if df.empty else df.loc[df["unit_cost"].isna(), "product"].tolist()
    if unknown:
        checks.append("No purchase cost on record for: " + ", ".join(unknown)
                      + ". Their cost is not included in the total; add unit costs on the Stock & products page.")
    if budget is not None and not df.empty:
        costed = df["unit_cost"].notna()
        total = float((df.loc[costed, "quantity"] * df.loc[costed, "unit_cost"]).sum())
        if total > budget + 1e-9:
            before = df["quantity"].copy()
            ai_total = total
            order = df[costed].sort_values(["priority", "unit_cost"], ascending=[False, False]).index
            for idx in order:
                while total > budget + 1e-9 and df.at[idx, "quantity"] > 0:
                    df.at[idx, "quantity"] -= 1
                    total -= df.at[idx, "unit_cost"]
            changed = [f"{df.at[i, 'product']} {before[i]} → {df.at[i, 'quantity']}"
                       for i in df.index if df.at[i, "quantity"] != before[i]]
            checks.append(f"The AI's plan came to S${ai_total:,.2f}, over your S${budget:,.2f} budget. The app cut the "
                          f"lowest-priority items to fit: {', '.join(changed)}.")
            df = df[df["quantity"] > 0]

    if not df.empty:
        df["est_cost"] = (df["quantity"] * df["unit_cost"]).round(2)
        on_hand = df["stock"].fillna(0)
        df["covers"] = np.where(df["expected_sales"] > 0, (on_hand + df["quantity"]) / df["expected_sales"], np.nan)
        # waste check for perishables: will stock + order sell before it spoils?
        for _, r in df.iterrows():
            life = r["shelf_life"]
            if life and life <= PERISHABLE_DAYS:
                sellable = products[r["sku"]]["forecast_by_day"][:int(life)]
                if sum(sellable) > 0 and (on_hand[_] + r["quantity"]) > WASTE_SLACK * sum(sellable):
                    checks.append(f"Waste risk: {r['product']} keeps {int(life)} day(s); stock + order "
                                  f"({on_hand[_] + r['quantity']:.0f}) is more than about {sum(sellable):.0f} "
                                  "expected to sell in that time.")
        df = df.sort_values(["priority", "est_cost"], ascending=[True, False]).reset_index(drop=True)
    total = float(df["est_cost"].sum()) if not df.empty else 0.0
    return {
        "summary": {**summary, "budget_sgd": budget, "horizon_days": horizon},
        "items": df,
        "total_cost": round(total, 2),
        "budget": budget,
        "within_budget": None if budget is None else total <= budget + 1e-9,
        "strategy": raw.get("strategy") or {},
        "assumptions": [str(a) for a in raw.get("assumptions") or []],
        "questions": [str(q) for q in raw.get("questions") or []],
        "data_gaps": [str(g) for g in raw.get("data_gaps") or []],
        "checks": checks,
        "plan_starts": ctx["plan_starts"],
        "model": agent.active()[2] if agent.active() else None,
    }
