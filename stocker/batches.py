"""Dated stock batches. Register existing stock without counting it twice.

Only explicit stock movements change quantities. Sales-file imports contain historical
sales, so importing them never consumes current batches. Generic sales movements use
first-expiring, first-out; expired batches must be removed as waste, not sold.
"""
import math
from datetime import date

import pandas as pd

from . import db

EPS = 1e-7


def quantity(value, allow_zero=False) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError("Enter a valid quantity.") from None
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError("Quantity must be positive." if not allow_zero else "Quantity cannot be negative.")
    return value


def _day(value) -> str:
    try:
        day = pd.Timestamp(value)
        if pd.isna(day):
            raise ValueError
        return day.date().isoformat()
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Enter a valid calendar date.") from None


def _details(code, received, expires):
    code = str(code).strip()
    if not code or len(code) > 80:
        raise ValueError("Enter a batch code of 1–80 characters.")
    received, expires = _day(received), _day(expires)
    if received > date.today().isoformat():
        raise ValueError("A received batch cannot have a future arrival date.")
    if expires < received:
        raise ValueError("The expiry date cannot be before the arrival date.")
    return code, received, expires


def _product(con, sku):
    row = con.execute("SELECT * FROM products WHERE sku=? AND active=1", (sku,)).fetchone()
    if row is None:
        raise ValueError("Choose an existing active product.")
    return row


def _log(con, batch, kind, qty, remaining, note=""):
    con.execute("INSERT INTO batch_events (batch_id,kind,qty,remaining_after,unit_cost,note) VALUES (?,?,?,?,?,?)",
                (batch["id"], kind, qty, remaining, batch["unit_cost"], note))


def add(sku: str, code: str, qty: float, received, expires, *, mode="existing", note="", unit_cost=None) -> int:
    """Label existing units, or record a new delivery and its stock increase atomically."""
    if mode not in ("existing", "delivery"):
        raise ValueError("Choose existing stock or a new delivery.")
    qty = quantity(qty)
    code, received, expires = _details(code, received, expires)
    if unit_cost is not None:
        unit_cost = quantity(unit_cost, allow_zero=True)
    with db.connect() as con:
        # Serialise capacity checks with the insert (two browser sessions may share a workspace).
        con.execute("BEGIN IMMEDIATE")
        p = _product(con, sku)
        if con.execute("SELECT 1 FROM stock_batches WHERE sku=? AND batch_code=?", (sku, code)).fetchone():
            raise ValueError("That batch code already exists for this product. Use its existing record.")
        total = p["current_stock"]
        if total is None:
            raise ValueError("Record the product's current stock count first, so batches can be reconciled.")
        tracked = con.execute("SELECT COALESCE(SUM(remaining_qty),0) FROM stock_batches WHERE sku=?", (sku,)).fetchone()[0]
        if mode == "existing" and qty > max(0, total - tracked) + EPS:
            raise ValueError(f"Only {max(0, total-tracked):g} units of existing stock have no batch date yet.")
        cost = unit_cost if unit_cost is not None else (float(p["unit_cost"]) if p["unit_cost"] else None)
        if mode == "delivery":
            con.execute("UPDATE products SET current_stock=current_stock+? WHERE sku=?", (qty, sku))
            con.execute("INSERT INTO stock_moves(sku,kind,qty,note) VALUES (?,?,?,?)",
                        (sku, "in", qty, f"Batch {code}: {note}"))
        cur = con.execute("INSERT INTO stock_batches(sku,batch_code,received_on,expires_on,initial_qty,remaining_qty,unit_cost,note) "
                          "VALUES (?,?,?,?,?,?,?,?)", (sku, code, received, expires, qty, qty, cost, str(note).strip()))
        batch = con.execute("SELECT * FROM stock_batches WHERE id=?", (cur.lastrowid,)).fetchone()
        _log(con, batch, "delivery" if mode == "delivery" else "label_existing", qty, qty, str(note).strip())
        return cur.lastrowid


def update_dates(batch_id: int, code: str, received, expires, note=""):
    code, received, expires = _details(code, received, expires)
    with db.connect() as con:
        con.execute("BEGIN IMMEDIATE")
        b = con.execute("SELECT * FROM stock_batches WHERE id=?", (int(batch_id),)).fetchone()
        if b is None:
            raise ValueError("Batch not found. Refresh the page.")
        if con.execute("SELECT 1 FROM stock_batches WHERE sku=? AND batch_code=? AND id!=?",
                       (b["sku"], code, batch_id)).fetchone():
            raise ValueError("That batch code already exists for this product.")
        con.execute("UPDATE stock_batches SET batch_code=?,received_on=?,expires_on=?,note=? WHERE id=?",
                    (code, received, expires, str(note).strip(), batch_id))
        _log(con, b, "details", 0, b["remaining_qty"],
             f"Dates {b['received_on']} / {b['expires_on']} → {received} / {expires}. {str(note).strip()}")


def reconcile(con, sku: str, total: float, note: str):
    """A stock count uses up undated units first, then corrects dated balances (FEFO)."""
    total = quantity(total, allow_zero=True)
    rows = con.execute("SELECT * FROM stock_batches WHERE sku=? AND remaining_qty>0 ORDER BY expires_on,id", (sku,)).fetchall()
    excess = max(0, sum(b["remaining_qty"] for b in rows) - total)
    for b in rows:
        take = min(excess, b["remaining_qty"])
        if take <= EPS:
            break
        remaining = max(0, b["remaining_qty"] - take)
        con.execute("UPDATE stock_batches SET remaining_qty=? WHERE id=?", (remaining, b["id"]))
        _log(con, b, "count_adjustment", take, remaining, note)
        excess -= take


def apply_movement(con, sku, kind, qty, batch_id, note):
    """Called inside db.record_move's transaction before total stock / sales are updated."""
    p = _product(con, sku)
    rows = con.execute("SELECT * FROM stock_batches WHERE sku=? AND remaining_qty>0 ORDER BY expires_on,id", (sku,)).fetchall()
    if kind == "adjust":
        reconcile(con, sku, qty, note or "Stock count corrected; not a sale")
        return
    if kind == "in":
        if batch_id is not None:
            raise ValueError("Register a dated delivery on the Expiry & batches page.")
        return
    total = float(p["current_stock"] or 0)
    if not rows and batch_id is None:
        if kind in ("waste", "return") and qty > total + EPS:
            raise ValueError("The removed quantity cannot exceed current stock.")
        return                  # legacy, undated stock keeps its original movement behaviour
    if qty > total + EPS:
        raise ValueError("This quantity exceeds current stock. Check the count first.")
    today = date.today().isoformat()
    if batch_id is not None:
        chosen = next((b for b in rows if b["id"] == int(batch_id)), None)
        if chosen is None or qty > chosen["remaining_qty"] + EPS:
            raise ValueError("This batch does not have enough remaining stock. Refresh and check its balance.")
        if kind == "out" and chosen["expires_on"] < today:
            raise ValueError("This batch has expired. Remove it as waste instead of recording a sale.")
        rows = [chosen]
    elif kind == "out":
        available = [b for b in rows if b["expires_on"] >= today]
        undated = max(0, total - sum(b["remaining_qty"] for b in rows))
        if qty > sum(b["remaining_qty"] for b in available) + undated + EPS:
            raise ValueError("Part of this stock has expired. Remove the expired batch as waste before recording this sale.")
        rows = available
    left = qty
    for b in rows:
        take = min(left, b["remaining_qty"])
        if take <= EPS:
            break
        remaining = max(0, b["remaining_qty"] - take)
        con.execute("UPDATE stock_batches SET remaining_qty=? WHERE id=?", (remaining, b["id"]))
        _log(con, b, "sold" if kind == "out" else kind, take, remaining, note)
        left -= take


def reduce(batch_id: int, kind: str, qty: float, note=""):
    if kind not in ("out", "waste", "return"):
        raise ValueError("Choose sold, waste or a supplier return.")
    b = db.query("SELECT sku FROM stock_batches WHERE id=?", (int(batch_id),))
    if b.empty:
        raise ValueError("Batch not found.")
    db.record_move(b.iloc[0]["sku"], kind, quantity(qty), note, batch_id=batch_id)


def inventory(as_of=None, days=7, include_empty=False) -> pd.DataFrame:
    today = pd.Timestamp(_day(as_of if as_of is not None else date.today()))
    df = db.query("SELECT b.*,p.name AS product,p.category,p.current_stock FROM stock_batches b "
                  "JOIN products p USING(sku) WHERE p.active=1 "
                  + ("" if include_empty else "AND b.remaining_qty>0 ") + "ORDER BY b.expires_on,b.id")
    for c in ("received_on", "expires_on"):
        df[c] = pd.to_datetime(df[c])
    df["days_left"] = (df["expires_on"] - today).dt.days
    df["status"] = df["days_left"].map(lambda d: "Expired" if d < 0 else "Expires today" if d == 0
                                        else "Due soon" if d <= days else "Later")
    df["stock_cost"] = df["remaining_qty"] * pd.to_numeric(df["unit_cost"], errors="coerce")
    return df


def coverage() -> pd.DataFrame:
    df = db.query("SELECT p.sku,p.name,p.current_stock,COALESCE(SUM(b.remaining_qty),0) AS dated_stock "
                  "FROM products p LEFT JOIN stock_batches b USING(sku) WHERE p.active=1 GROUP BY p.sku ORDER BY p.name")
    df["undated_stock"] = (df["current_stock"] - df["dated_stock"]).clip(lower=0)
    return df


def history(limit=200) -> pd.DataFrame:
    return db.query("SELECT e.ts,b.sku,p.name AS product,b.batch_code,e.kind,e.qty,e.remaining_after,e.note "
                    "FROM batch_events e JOIN stock_batches b ON b.id=e.batch_id JOIN products p USING(sku) "
                    "ORDER BY e.id DESC LIMIT ?", (limit,))


def alerts(as_of=None, days=None) -> list[dict]:
    days = int(days if days is not None else db.get_setting("expiry_warning_days", 7))
    df = inventory(as_of, days)
    out = []
    for b in df[df["days_left"] <= days].itertuples():
        expired = b.days_left < 0
        when = "has expired" if expired else "expires today" if b.days_left == 0 else f"expires in {b.days_left} days"
        out.append({"severity": "critical" if expired else "high" if b.days_left <= 1 else "medium",
                    "kind": "Expired batch" if expired else "Batch expires today" if b.days_left == 0 else "Batch expiring soon",
                    "sku": b.sku, "product": b.product,
                    "message": f"Batch {b.batch_code}: {b.remaining_qty:g} units {when} ({b.expires_on:%d %b %Y}). This is a recorded expiry date.",
                    "action": "Remove from sale and record waste or a supplier return." if expired else
                              "Check the label and condition; put this batch first and review any promotion before the expiry date.",
                    "value_at_risk": round(float(b.stock_cost), 2) if pd.notna(b.stock_cost) else 0.0,
                    "meta": {"batch_id": b.id, "batch_code": b.batch_code, "days_left": b.days_left,
                             "quantity": b.remaining_qty, "basis": "recorded expiry date; stock cost, not a confirmed loss"}})
    return out
