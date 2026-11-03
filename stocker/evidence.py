"""Frozen advice evidence and prospective outcome reviews. No confidence scores.

Historical training backtests, prospective forecast errors, self-reported actions
and observed inventory events are deliberately kept separate.
"""
import hashlib
import json
import math
import uuid
from datetime import date, timedelta

import pandas as pd

from . import db, engine

SCHEMA = """
CREATE TABLE IF NOT EXISTS advice_snapshots (
 id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, captured_on TEXT NOT NULL,
 forecast_start TEXT NOT NULL, forecast_end TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_freshness (
 sku TEXT PRIMARY KEY, counted_on TEXT, recorded_at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inventory_observations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL,
 observed_on TEXT NOT NULL, recorded_at TEXT NOT NULL, stock REAL NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advice_feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL, sku TEXT NOT NULL,
 recorded_at TEXT NOT NULL, decision TEXT NOT NULL, purchased_qty REAL,
 purchased_on TEXT, owner TEXT NOT NULL, note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advice_zero_confirmations (
 snapshot_id TEXT NOT NULL, sku TEXT NOT NULL, confirmed_at TEXT NOT NULL,
 owner TEXT NOT NULL, note TEXT NOT NULL, PRIMARY KEY(snapshot_id,sku)
);
CREATE TABLE IF NOT EXISTS advice_stockout_reports (
 id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id TEXT NOT NULL, sku TEXT NOT NULL,
 observed_on TEXT NOT NULL, recorded_at TEXT NOT NULL, owner TEXT NOT NULL, note TEXT NOT NULL
);
"""


def today():
    # Same business-day clock as the existing forecast and inventory app.
    return pd.Timestamp.today().date()


def timestamp():
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def _plain(value):
    """JSON-safe copy: numpy scalars become Python numbers, NaN/inf become None."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "item"):   # numpy scalar
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _dump(value):
    return json.dumps(_plain(value), ensure_ascii=False, sort_keys=True, allow_nan=False)


def _number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _owner(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100:
        raise ValueError("Enter your name (up to 100 characters).")
    return value.strip()


# Fingerprint of the business data behind the advice. Sales are summarised in SQL (count, date range,
# weighted totals) instead of hashing every row, so it stays fast on large imports.
_SIGNATURE_SQL = [
    "SELECT * FROM products ORDER BY sku",
    "SELECT COUNT(*), MIN(date), MAX(date), TOTAL(qty), TOTAL(revenue), TOTAL(qty * julianday(date)) FROM sales",
    "SELECT sku, trained_at FROM models ORDER BY sku",
    "SELECT * FROM inventory_freshness ORDER BY sku",
    "SELECT COALESCE(MAX(id), 0) FROM stock_moves",
    "SELECT COALESCE(MAX(id), 0) FROM inventory_observations",
]


def signature(con=None):
    if con is None:
        with db.connect() as connection:
            return signature(connection)
    state = [[list(r) for r in con.execute(sql)] for sql in _SIGNATURE_SQL]
    return hashlib.sha256(_dump(state).encode()).hexdigest()


def save_current():
    """Current purchase advice, saved once per data state and day for a later outcome review."""
    from . import advisor
    expected = signature()
    rec = advisor.recommendations()
    capture(rec, advisor.plan_start(), expected)
    return rec


def assess_all(as_of=None):
    """Observable data facts, never a made-up probability of correctness."""
    as_of = as_of or today()
    products = db.products()
    sales = db.sales()
    models = engine.load_models()
    dates = sales.groupby("sku")["date"].agg(["min", "max", "nunique"]) if not sales.empty else pd.DataFrame()
    fresh = db.query("SELECT * FROM inventory_freshness").set_index("sku").to_dict("index")
    trained = db.query("SELECT sku,trained_at FROM models").set_index("sku")["trained_at"].to_dict()
    out = {}
    for p in products.itertuples():
        m = models.get(p.sku)
        end = dates.loc[p.sku, "max"].date() if p.sku in dates.index else None
        span = (end - dates.loc[p.sku, "min"].date()).days + 1 if end else 0
        observed = int(dates.loc[p.sku, "nunique"]) if end else 0
        f = fresh.get(p.sku, {})
        counted = date.fromisoformat(f["counted_on"]) if f.get("counted_on") else None
        sales_age = (as_of - end).days if end else None
        stock_age = (as_of - counted).days if counted else None
        warnings, missing = [], []
        if pd.isna(p.current_stock):
            missing.append("current stock")
            warnings.append("Current stock is missing; purchase quantities need an inventory check.")
        if stock_age is None:
            missing.append("stock count date")
            warnings.append("Stock count date is unknown. Imported stock is not treated as a fresh physical count.")
        elif stock_age < 0:
            warnings.append("Stock count date is in the future. Check the date.")
        elif stock_age > 7:
            warnings.append(f"Stock was last counted {stock_age} days ago.")
        if end is None:
            warnings.append("No sales history is recorded.")
        elif sales_age < 0:
            warnings.append("Sales include future-dated records. Check the file dates.")
        elif sales_age > 3:
            warnings.append(f"Latest recorded sales are {sales_age} days old.")
        if not m or m.status == "insufficient":
            warnings.append(f"Fewer than {engine.MIN_DAYS} history days, or no trained model.")
        if m and end and end.isoformat() > m.last_date:
            warnings.append("New sales have arrived since model training; retrain before relying on it.")
        if span > observed:
            warnings.append(f"{span - observed} dates have no explicit sales row; the current model fills these gaps with zero.")
        if _number(p.unit_cost) in (None, 0.0):
            missing.append("unit cost")
            warnings.append("Unit cost is missing or zero; estimated purchasing cost may be incomplete.")
        stock_warning = pd.isna(p.current_stock) or stock_age is None or stock_age < 0 or stock_age > 7
        next_action = ("Confirm current stock before deciding the purchase quantity." if stock_warning else
                       "Check the listed data issues before using this advice." if warnings else
                       "Review the suggested range and your current trading conditions.")
        out[p.sku] = {
            "history_span_days": span, "recorded_sales_days": observed,
            "last_sales_date": end.isoformat() if end else None, "sales_age_days": sales_age,
            "last_stock_count": counted.isoformat() if counted else None,
            "stock_age_days": stock_age, "stock_source": f.get("source", "legacy / unknown"),
            "trained_at": trained.get(p.sku), "backtest_wape": m.wape if m else None,
            "naive_backtest_wape": m.naive_wape if m else None,
            "missing_fields": missing, "warnings": warnings, "next_action": next_action,
            "label": "Check stock first" if stock_warning else "Check data" if warnings else "Evidence available",
        }
    return out


def capture(recommendations, recommendation_start, expected_signature):
    """Save each distinct generated advice state once per business day.

    The evaluation forecast is a NEW forward seven-day forecast, starting after
    both today and the latest uploaded sale. Original purchase ranges retain
    their own original lead-time + review-cycle planning period.
    """
    if recommendations.empty:
        return None
    day = today()
    key = hashlib.sha256(f"{day}|{expected_signature}|{recommendation_start}|7".encode()).hexdigest()
    with db.connect() as con:
        existing = con.execute("SELECT id FROM advice_snapshots WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return existing[0]
        con.execute("BEGIN IMMEDIATE")
        if signature(con) != expected_signature:
            return None  # Concurrent business changes: never save mismatched advice/evidence.
        existing = con.execute("SELECT id FROM advice_snapshots WHERE dedupe_key=?", (key,)).fetchone()
        if existing:
            return existing[0]
        last = con.execute("SELECT MAX(date) FROM sales").fetchone()[0]
        start = max(day + timedelta(days=1), date.fromisoformat(last) + timedelta(days=1)) if last else day + timedelta(days=1)
        end = start + timedelta(days=6)
        models = engine.load_models()
        quality = assess_all(day)
        from . import advisor
        risks = advisor.detect_risks()
        products = db.products().set_index("sku")
        items = []
        for row in recommendations.to_dict("records"):
            sku = row["sku"]
            m = models.get(sku)
            if not m:
                continue
            fc = m.forecast(7, start=pd.Timestamp(start))
            daily = [{"date": r.date.date().isoformat(), "forecast": float(r.forecast),
                      "low": float(r.low), "high": float(r.high)} for r in fc.itertuples()]
            lead = int(_number(products.loc[sku, "lead_time_days"]) or 1)
            items.append({
                "sku": sku, "product": row["product"], "action": row["action"],
                "order_low": int(row["order_low"]), "order_high": int(row["order_high"]),
                "reason": row["reason"], "stock": _number(row["stock"]),
                "cover_days": _number(row["cover_days"]), "daily_demand": _number(row["daily_forecast"]),
                "recommendation_start": pd.Timestamp(recommendation_start).date().isoformat(),
                "recommendation_end": (pd.Timestamp(recommendation_start) + pd.Timedelta(days=lead + advisor.REVIEW_DAYS - 1)).date().isoformat(),
                "forecast": daily, "naive_daily": float(m.avg_28), "model": m.__dict__,
                "evidence": quality.get(sku, {}),
                "risks": [{"kind": r.kind, "severity": r.severity, "message": r.message}
                          for r in risks if r.sku == sku],
            })
        if not items:
            return None
        payload = {"items": items, "sales_cutoff": last,
                   "last_move_id": con.execute("SELECT COALESCE(MAX(id),0) FROM stock_moves").fetchone()[0],
                   "last_observation_id": con.execute("SELECT COALESCE(MAX(id),0) FROM inventory_observations").fetchone()[0]}
        identifier = uuid.uuid4().hex
        con.execute("INSERT INTO advice_snapshots VALUES (?,?,?,?,?,?,?)",
                    (identifier, key, timestamp(), day.isoformat(), start.isoformat(), end.isoformat(), _dump(payload)))
        return identifier


def snapshots():
    return db.query("SELECT id,created_at,captured_on,forecast_start,forecast_end FROM advice_snapshots ORDER BY created_at DESC,rowid DESC")


def get(snapshot_id):
    with db.connect() as con:
        row = con.execute("SELECT * FROM advice_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError("Snapshot not found in this workspace.")
    result = dict(row)
    result["payload"] = json.loads(result["payload"])
    return result


def _item(snapshot_id, sku):
    snapshot = get(snapshot_id)
    item = next((i for i in snapshot["payload"]["items"] if i["sku"] == sku), None)
    if item is None:
        raise ValueError("Product is not in this advice snapshot.")
    return snapshot, item


def record_feedback(snapshot_id, sku, decision, purchased_qty, purchased_on, owner, note="", as_of=None):
    snapshot, _ = _item(snapshot_id, sku)
    if decision not in ("followed", "modified", "not_followed", "undecided"):
        raise ValueError("Choose a valid adoption decision.")
    owner = _owner(owner)
    if purchased_qty is not None:
        purchased_qty = _number(purchased_qty)
        if purchased_qty is None or purchased_qty < 0:
            raise ValueError("Purchased quantity must be finite and non-negative.")
        purchased_on = date.fromisoformat(str(purchased_on))
        if not date.fromisoformat(snapshot["captured_on"]) <= purchased_on <= min(as_of or today(), date.fromisoformat(snapshot["forecast_end"])):
            raise ValueError("Purchase date must be after capture, within this review period and not in the future.")
    with db.connect() as con:
        con.execute("INSERT INTO advice_feedback (snapshot_id,sku,recorded_at,decision,purchased_qty,purchased_on,owner,note) VALUES (?,?,?,?,?,?,?,?)",
                    (snapshot_id, sku, timestamp(), decision, purchased_qty,
                     str(purchased_on) if purchased_qty is not None else None, owner, str(note)[:2000]))


def confirm_missing_days_zero(snapshot_id, sku, owner, note="", as_of=None):
    snapshot, _ = _item(snapshot_id, sku)
    if date.fromisoformat(snapshot["forecast_end"]) >= (as_of or today()):
        raise ValueError("The full forecast window must have ended before confirming complete sales coverage.")
    owner = _owner(owner)
    with db.connect() as con:
        con.execute("INSERT INTO advice_zero_confirmations VALUES (?,?,?,?,?) ON CONFLICT(snapshot_id,sku) "
                    "DO UPDATE SET confirmed_at=excluded.confirmed_at,owner=excluded.owner,note=excluded.note",
                    (snapshot_id, sku, timestamp(), owner, str(note)[:2000]))


def record_stockout(snapshot_id, sku, observed_on, owner, note="", as_of=None):
    snapshot, _ = _item(snapshot_id, sku)
    observed_on = date.fromisoformat(str(observed_on))
    if not date.fromisoformat(snapshot["captured_on"]) <= observed_on <= min(as_of or today(), date.fromisoformat(snapshot["forecast_end"])):
        raise ValueError("Stockout date must be within this review period and not in the future.")
    with db.connect() as con:
        con.execute("INSERT INTO advice_stockout_reports (snapshot_id,sku,observed_on,recorded_at,owner,note) VALUES (?,?,?,?,?,?)",
                    (snapshot_id, sku, str(observed_on), timestamp(), _owner(owner), str(note)[:2000]))


def report(snapshot_id, sku, as_of=None):
    snapshot, item = _item(snapshot_id, sku)
    day = as_of or today()
    start, end = date.fromisoformat(snapshot["forecast_start"]), date.fromisoformat(snapshot["forecast_end"])
    with db.connect() as con:
        actual_rows = {r["date"]: float(r["qty"]) for r in con.execute(
            "SELECT date,qty FROM sales WHERE sku=? AND date BETWEEN ? AND ?", (sku, str(start), str(end)))}
        confirmation = con.execute("SELECT * FROM advice_zero_confirmations WHERE snapshot_id=? AND sku=?", (snapshot_id, sku)).fetchone()
        feedback = con.execute("SELECT * FROM advice_feedback WHERE snapshot_id=? AND sku=? ORDER BY id DESC LIMIT 1", (snapshot_id, sku)).fetchone()
        observations = [dict(r) for r in con.execute(
            "SELECT * FROM inventory_observations WHERE sku=? AND id>? AND observed_on BETWEEN ? AND ? ORDER BY observed_on,id",
            (sku, snapshot["payload"]["last_observation_id"], snapshot["captured_on"], str(min(day, end))))]
        stockouts = [dict(r) for r in con.execute(
            "SELECT * FROM advice_stockout_reports WHERE snapshot_id=? AND sku=? AND observed_on<=? ORDER BY observed_on,id",
            (snapshot_id, sku, str(min(day, end))))]
        receipts = con.execute("SELECT SUM(qty) FROM stock_moves WHERE sku=? AND id>? AND kind='in' AND date(ts) BETWEEN ? AND ?",
                               (sku, snapshot["payload"]["last_move_id"], snapshot["captured_on"], str(min(day, end)))).fetchone()[0]
    rows = []
    for f in item["forecast"]:
        d = date.fromisoformat(f["date"])
        closed = d < day
        # Missing rows remain unknown unless the owner confirms a complete import.
        actual = actual_rows.get(f["date"]) if closed else None
        imputed = closed and actual is None and confirmation is not None
        if imputed:
            actual = 0.0
        rows.append({**f, "actual": actual, "zero_confirmed": imputed,
                     "closed_day": closed, "naive": item["naive_daily"]})
    observed = [r for r in rows if r["actual"] is not None]
    complete = len(observed) == 7 and day > end
    actual_total = sum(r["actual"] for r in observed)
    error = sum(abs(r["forecast"] - r["actual"]) for r in observed)
    naive_error = sum(abs(r["naive"] - r["actual"]) for r in observed)
    counts = [o for o in observations if o["source"] in ("physical_count", "manual_product_edit")]
    latest_count = counts[-1] if counts else None
    daily = item["daily_demand"]
    after_cover = latest_count["stock"] / daily if latest_count and daily and daily > 0 else None
    zero_stock = [o for o in observations if o["stock"] == 0]
    return {
        "snapshot": snapshot, "item": item, "daily": rows, "complete": complete,
        "status": "complete" if complete else "awaiting future sales" if day <= start else "in progress" if day <= end else "missing sales records",
        "observed_days": len(observed), "forecast_total": sum(r["forecast"] for r in rows),
        "actual_total": actual_total if complete else None,
        "observed_actual_total": actual_total if observed else None,
        "bias_units": actual_total - sum(r["forecast"] for r in rows) if complete else None,
        "mae": error / 7 if complete else None,
        "wape": error / actual_total if complete and actual_total > 0 else None,
        "naive_wape": naive_error / actual_total if complete and actual_total > 0 else None,
        "feedback": dict(feedback) if feedback else None,
        "recorded_receipts": float(receipts) if receipts is not None else None,
        "stockout_reports": stockouts, "zero_stock_observations": zero_stock,
        "latest_count": latest_count, "after_cover_days": after_cover,
        "coverage_change": after_cover - item["cover_days"] if after_cover is not None and item["cover_days"] is not None else None,
        "zero_confirmation": dict(confirmation) if confirmation else None,
    }
