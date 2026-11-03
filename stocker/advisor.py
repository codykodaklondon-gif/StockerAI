"""Recommendation engine and real-time risk detection.

Both work from the trained models plus the *current* stock level, so any stock
movement or new sales data changes the output immediately. Rules are fixed and
documented: the same data always gives the same advice.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import batches, db, engine

REVIEW_DAYS = 7        # how often the merchant typically reorders
SERVICE_Z = 1.65       # ~95% service level for safety stock
OVERSTOCK_DAYS = 60
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass
class Position:
    sku: str
    name: str
    category: str
    stock: float | None       # None = stock level not recorded
    lead: int
    daily: float              # expected units/day over the next lead+review window
    cover_days: float
    reorder_point: float
    safety_stock: float
    target: float
    unit_cost: float
    shelf_life: float | None
    model: engine.Model


def plan_start() -> pd.Timestamp:
    """First day to plan for: today, or the day after the last sale if the data is more than a week old
    (e.g. a historical file), so forecasts continue from the data instead of jumping years ahead."""
    today = pd.Timestamp.today().normalize()
    last = db.query("SELECT MAX(date) d FROM sales")["d"].iloc[0]
    if last is None:
        return today
    nxt = pd.Timestamp(last) + pd.Timedelta(days=1)
    return nxt if (today - nxt).days > 7 else today


_positions_memo: tuple = (None, [])     # (database version, positions) — swapped in one assignment


def positions() -> list[Position]:
    """Every product's stock position. Memoised per database version, so the stocking list, risk checks
    and KPIs of one request share a single calculation (any write changes the file and refreshes it).
    The memo is replaced atomically, so concurrent sessions never see a half-updated value."""
    global _positions_memo
    path = db.current()
    key = (str(path), path.stat().st_mtime_ns if path.exists() else 0, pd.Timestamp.today().normalize())
    memo_key, value = _positions_memo
    if memo_key != key:
        value = _compute_positions()
        _positions_memo = (key, value)
    return value


def _compute_positions() -> list[Position]:
    prods = db.products()
    models = engine.load_models()
    start = plan_start()
    out = []
    for p in prods.itertuples():
        m = models.get(p.sku)
        if m is None:
            continue
        lead = int(p.lead_time_days or 1)
        window = lead + REVIEW_DAYS
        fc = m.forecast_arrays(window, start=start)["forecast"]
        demand = float(fc.sum())
        daily = demand / window
        ss = SERVICE_Z * m.sigma * np.sqrt(lead)
        rop = float(fc[:lead].sum()) + ss
        stock = float(p.current_stock) if pd.notna(p.current_stock) else None
        cover = float("nan") if stock is None else (stock / daily if daily > 0 else float("inf"))
        out.append(Position(
            p.sku, p.name, p.category, stock, lead, daily, cover,
            rop, ss, demand + ss, float(p.unit_cost or 0),
            float(p.shelf_life_days) if pd.notna(p.shelf_life_days) else None, m,
        ))
    return out


# ------------------------------------------------------------------ recommendations

def recommendations() -> pd.DataFrame:
    rows = []
    start = plan_start()
    for pos in positions():
        m = pos.model
        reasons = engine.explain(m)
        window = pos.lead + REVIEW_DAYS
        on_hand = pos.stock or 0.0
        # 80% range for the window total: daily errors partly cancel, so spread grows with sqrt(days);
        # summing the daily ranges would overstate it
        total = float(m.forecast_arrays(window, start=start)["forecast"].sum())
        spread = 1.28 * m.sigma * np.sqrt(window)
        need_low = max(0.0, total - spread) + 0.5 * pos.safety_stock - on_hand
        need_high = total + spread + pos.safety_stock - on_hand
        q_low, q_high = max(0, round(need_low)), max(0, round(need_high))

        if m.status == "insufficient":
            action = "Collect more data"
        elif m.status == "inactive" or (pos.stock is not None and m.avg_28 > 0
                                        and m.avg_7 < 0.3 * m.avg_28 and pos.cover_days > 30):
            action = "Stop reordering"
            reasons.insert(0, "Sales have almost stopped; sell through existing stock first.")
            q_low = q_high = 0
        elif pos.stock is None:
            # without a stock count we can still say how much the next order period needs
            action = "Plan order"
            reasons.insert(0, f"Stock level not recorded. Expected need for the next {window} days "
                              f"(lead time + {REVIEW_DAYS}-day order cycle) — subtract what you have on hand.")
        elif pos.stock <= pos.reorder_point:
            action = "Stock more"
            reasons.insert(0, f"Stock of {pos.stock:.0f} covers {pos.cover_days:.1f} days, "
                              f"below the {pos.lead}-day lead time plus safety buffer.")
            q_low = max(q_low, 1)
            q_high = max(q_high, q_low)
        elif pos.cover_days > OVERSTOCK_DAYS or (pos.shelf_life and pos.cover_days > pos.shelf_life):
            action = "Stock less"
            limit = min(OVERSTOCK_DAYS, pos.shelf_life or OVERSTOCK_DAYS)
            reasons.insert(0, f"Current stock lasts about {pos.cover_days:.0f} days, more than the {limit:.0f}-day limit.")
            q_low = q_high = 0
        else:
            action = "Maintain"
            reasons.insert(0, f"Stock covers {pos.cover_days:.1f} days, enough until the next order.")
            q_low = q_high = 0

        rows.append({
            "sku": pos.sku, "product": pos.name, "category": pos.category, "action": action,
            "order_low": q_low, "order_high": q_high, "stock": pos.stock,
            "daily_forecast": round(pos.daily, 1),
            "cover_days": round(pos.cover_days, 1) if np.isfinite(pos.cover_days) else None,   # nan/inf -> None
            "reorder_point": round(pos.reorder_point, 1),
            "est_cost": round((q_low + q_high) / 2 * pos.unit_cost, 2),
            "reason": " ".join(reasons),
        })
    order = {"Stock more": 0, "Plan order": 1, "Stock less": 2, "Stop reordering": 3, "Collect more data": 4, "Maintain": 5}
    df = pd.DataFrame(rows)
    if not df.empty:
        # most urgent action first; within an action, the biggest sellers first
        df = (df.assign(_o=df["action"].map(order))
                .sort_values(["_o", "daily_forecast"], ascending=[True, False])
                .drop(columns="_o").reset_index(drop=True))
    return df


# ------------------------------------------------------------------ risk detection

@dataclass
class Risk:
    severity: str
    kind: str
    sku: str
    product: str
    message: str
    action: str
    value_at_risk: float = 0.0
    meta: dict = field(default_factory=dict)


def detect_risks() -> list[Risk]:
    risks: list[Risk] = [Risk(**r) for r in batches.alerts()]
    sales = db.sales()
    today = pd.Timestamp.today().normalize()
    if sales.empty:
        return sorted(risks, key=lambda r: (SEVERITY_ORDER[r.severity], -r.value_at_risk))

    last_data = sales["date"].max()
    stale = (today - last_data).days
    if stale > 3:
        risks.append(Risk("medium", "Stale data", "-", "All products",
                          f"Latest sales record is {stale} days old ({last_data.date()}).",
                          "Import or enter recent sales so forecasts stay accurate."))

    prods = db.products()
    unknown = int(prods["current_stock"].isna().sum())
    if unknown:
        risks.append(Risk("medium", "Stock levels not set", "-", f"{unknown} products",
                          f"{unknown} of {len(prods)} products have no stock count, so stock-out, overstock "
                          "and expiry alerts are off for them.",
                          "Enter current stock on the Inventory page (or import a file with a stock column)."))
    prices = prods.set_index("sku")["unit_price"].fillna(0).to_dict()
    by_sku = dict(tuple(sales.groupby("sku")))     # one pass instead of re-scanning all sales per product
    dated = batches.coverage().set_index("sku")["dated_stock"].to_dict()
    for pos in positions():
        m = pos.model
        price = float(prices.get(pos.sku, 0) or 0)
        if m.status == "insufficient":
            risks.append(Risk("low", "Insufficient data", pos.sku, pos.name,
                              f"Only {m.n_days} days of sales history.",
                              f"Keep recording sales; advice starts after {engine.MIN_DAYS} days."))
            continue

        if pos.stock is not None:      # stock-based checks need a recorded stock level
            _stock_risks(risks, pos, price, max(0, pos.stock - dated.get(pos.sku, 0)))

        # 4. demand anomaly on the latest day
        s = engine.daily_series(pos.sku, by_sku.get(pos.sku, sales.iloc[0:0]), until=last_data)
        if len(s) >= engine.MIN_DAYS:
            y = s.iloc[-1]
            exp = m.level * m.dow[s.index[-1].day_of_week] * m.cycle_mult(s.index[-1]) * m.event_mult(s.index[-1])   # holidays are expected
            z = (y - exp) / m.sigma if m.sigma else 0
            if z >= 3:
                risks.append(Risk("medium", "Demand spike", pos.sku, pos.name,
                                  f"Sold {y:.0f} on {s.index[-1].date()}, vs about {exp:.0f} expected ({z:.1f} sigma).",
                                  "Check stock and whether the spike will continue.", 0.0, {"z": z}))
            elif z <= -3 and exp >= 3:
                risks.append(Risk("low", "Demand drop", pos.sku, pos.name,
                                  f"Sold {y:.0f} on {s.index[-1].date()}, vs about {exp:.0f} expected.",
                                  "Check for supply, display or pricing issues.", 0.0, {"z": z}))

        # 5. trend shift
        if m.avg_28 >= 2:
            change = (m.avg_7 - m.avg_28) / m.avg_28
            if abs(change) >= 0.4:
                risks.append(Risk("low", "Trend shift", pos.sku, pos.name,
                                  f"Weekly demand moved {change:+.0%} vs the 28-day average.",
                                  "Review order quantities for this item."))

    risks.sort(key=lambda r: (SEVERITY_ORDER[r.severity], -r.value_at_risk))
    return risks



def _stock_risks(risks: list, pos: Position, price: float, undated_stock: float | None = None):
    m = pos.model
    # 1. stock-out
    if pos.stock <= 0 and pos.daily > 0.2:
        risks.append(Risk("critical", "Out of stock", pos.sku, pos.name,
                          f"No stock left; losing about {pos.daily:.0f} units/day in sales.",
                          "Reorder immediately.", pos.daily * price * pos.lead))
    elif pos.cover_days < pos.lead:
        short = pos.daily * pos.lead - pos.stock
        risks.append(Risk("high", "Stock-out risk", pos.sku, pos.name,
                          f"Stock runs out in {pos.cover_days:.1f} days, before a new order can arrive ({pos.lead}-day lead time).",
                          "Order now and consider a faster supplier.", short * price,
                          {"days_left": pos.cover_days}))
    elif pos.stock <= pos.reorder_point:
        risks.append(Risk("medium", "Below reorder point", pos.sku, pos.name,
                          f"Stock {pos.stock:.0f} is below the reorder point of {pos.reorder_point:.0f}.",
                          "Place an order today.", 0.0, {"days_left": pos.cover_days}))

    # 2. expiry / spoilage
    expiry_stock = pos.stock if undated_stock is None else undated_stock
    if pos.shelf_life and pos.daily > 0 and expiry_stock / pos.daily > pos.shelf_life:
        waste = expiry_stock - pos.daily * pos.shelf_life
        risks.append(Risk("high" if waste * pos.unit_cost > 50 else "medium", "Expiry risk", pos.sku, pos.name,
                          f"About {waste:.0f} undated units may expire before they sell (shelf life estimate: {pos.shelf_life:.0f} days; no recorded batch date).",
                          "Run a promotion or pause reorders.", waste * pos.unit_cost))

    # 3. overstock / dead stock
    if pos.stock > 0 and m.avg_28 * 28 == 0:
        risks.append(Risk("high", "Dead stock", pos.sku, pos.name,
                          f"No sales in 28 days with {pos.stock:.0f} units on hand.",
                          "Clear or return this stock.", pos.stock * pos.unit_cost))
    elif np.isfinite(pos.cover_days) and pos.cover_days > OVERSTOCK_DAYS:
        excess = pos.stock - pos.daily * OVERSTOCK_DAYS
        risks.append(Risk("medium", "Overstock", pos.sku, pos.name,
                          f"Stock covers {pos.cover_days:.0f} days; about {excess:.0f} units are excess.",
                          "Skip the next order.", excess * pos.unit_cost))

def risks_frame() -> pd.DataFrame:
    rs = detect_risks()
    return pd.DataFrame([{k: v for k, v in r.__dict__.items() if k != "meta"} for r in rs])


def kpis() -> dict:
    pos = positions()
    prods = db.products()
    stock_value = float((prods["current_stock"].fillna(0) * prods["unit_cost"].fillna(0)).sum())
    sales = db.sales()
    last = sales["date"].max() if not sales.empty else None
    rev30 = float(sales[sales["date"] > last - pd.Timedelta(days=30)]["revenue"].sum()) if last is not None else 0
    return {
        "products": len(prods),
        "stock_value": stock_value,
        "revenue_30d": rev30,
        "low_cover": sum(p.stock is not None and p.stock <= p.reorder_point for p in pos),
    }
