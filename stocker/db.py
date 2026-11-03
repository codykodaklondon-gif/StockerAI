"""SQLite storage layer: products, daily sales, stock movements and trained models."""
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("STOCKER_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data" / "workspaces"))
DEFAULT_DB = DATA_DIR / "default.db"

# Public edition: every visitor gets their own database file. The active one is held in a
# ContextVar so concurrent Streamlit sessions (each in its own thread) never see each other's data.
_current: ContextVar[Path] = ContextVar("stocker_db", default=DEFAULT_DB)


def use_workspace(workspace_id: str) -> Path:
    """Point this session at its own database file and return its path."""
    safe = "".join(c for c in workspace_id if c.isalnum())[:32] or "default"
    path = DATA_DIR / f"{safe}.db"
    _current.set(path)
    return path


def current() -> Path:
    return _current.get()


def cleanup_workspaces(max_age_hours: float = 48) -> int:
    """Delete visitor databases that have not been touched for a while."""
    if not DATA_DIR.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in DATA_DIR.glob("*.db"):
        if f != current() and f.stat().st_mtime < cutoff:
            if _review_in_use(f):
                continue   # an advice review must outlive the 48-hour cleanup; only Reset removes it
            f.unlink(missing_ok=True)
            removed += 1
    return removed

def _review_in_use(f: Path) -> bool:
    """A review window still open (or closed within a week), or anything the owner recorded for it.
    Demo visitors who never look at the review get cleaned up once their window has passed."""
    try:
        with sqlite3.connect(f) as con:
            return bool(con.execute(
                "SELECT EXISTS(SELECT 1 FROM advice_snapshots WHERE forecast_end >= date('now','localtime','-7 days'))"
                " OR EXISTS(SELECT 1 FROM advice_feedback) OR EXISTS(SELECT 1 FROM advice_stockout_reports)"
                " OR EXISTS(SELECT 1 FROM advice_zero_confirmations)").fetchone()[0])
    except sqlite3.OperationalError:   # no such table: an older workspace without reviews
        return False
    except sqlite3.DatabaseError:      # unreadable file: keep it rather than guess
        return True


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku            TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    category       TEXT DEFAULT 'General',
    location       TEXT DEFAULT '',
    unit_cost      REAL DEFAULT 0,
    unit_price     REAL DEFAULT 0,
    lead_time_days INTEGER DEFAULT 3,
    current_stock  REAL,            -- NULL = not recorded yet
    min_stock      REAL DEFAULT 0,
    shelf_life_days INTEGER,
    active         INTEGER DEFAULT 1,
    perishable     INTEGER          -- 1 / 0 set by the owner; NULL = decided by perishable()
);
CREATE TABLE IF NOT EXISTS sales (
    date TEXT NOT NULL,
    sku  TEXT NOT NULL,
    qty  REAL NOT NULL,
    revenue REAL DEFAULT 0,
    PRIMARY KEY (date, sku)
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    TEXT DEFAULT (datetime('now','localtime')),
    sku   TEXT NOT NULL,
    kind  TEXT NOT NULL,          -- in / out / adjust / waste / return
    qty   REAL NOT NULL,
    note  TEXT
);
CREATE TABLE IF NOT EXISTS sales_detail (   -- optional line-level data for customer profiling
    date     TEXT NOT NULL,
    hour     INTEGER,
    sku      TEXT NOT NULL,
    qty      REAL NOT NULL,
    revenue  REAL DEFAULT 0,
    customer_type TEXT,           -- e.g. Member / Walk-in
    txn      TEXT,                -- receipt / invoice id
    customer TEXT                 -- anonymised (hashed) customer id
);
CREATE INDEX IF NOT EXISTS idx_detail_date ON sales_detail(date);
CREATE TABLE IF NOT EXISTS drafts (            -- AI purchase drafts awaiting human review
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT DEFAULT (datetime('now','localtime')),
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending / rejected / approved / superseded
    model        TEXT,
    ai_raw       TEXT,          -- the model's original JSON, never edited
    ai_items     TEXT,          -- items as first drafted (after app validation)
    items        TEXT,          -- current items incl. reviewer edits and risk handling
    risks        TEXT,
    summary      TEXT,
    content_hash TEXT,
    approved_hash TEXT,
    reviewer     TEXT,          -- typed name; not a verified identity
    decision_note TEXT,
    decided_at   TEXT
);
CREATE TABLE IF NOT EXISTS draft_events (      -- audit trail
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    ts       TEXT DEFAULT (datetime('now','localtime')),
    actor    TEXT,
    action   TEXT NOT NULL,
    detail   TEXT
);
CREATE TABLE IF NOT EXISTS purchase_orders (   -- official lists; only created by an approval
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL,
    created_at  TEXT DEFAULT (datetime('now','localtime')),
    approved_by TEXT,
    content_hash TEXT,
    items       TEXT,
    total_cost  REAL,
    status      TEXT NOT NULL DEFAULT 'valid'    -- valid / void
);
CREATE TABLE IF NOT EXISTS settings (       -- small JSON values: holiday country, alert preferences
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS models (
    sku        TEXT PRIMARY KEY,
    trained_at TEXT,
    params     TEXT              -- JSON
);
CREATE TABLE IF NOT EXISTS events (         -- holidays, promotions, closures the forecast learns from
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    date   TEXT NOT NULL,
    name   TEXT NOT NULL,
    kind   TEXT DEFAULT 'Holiday',
    scope  TEXT DEFAULT 'All products',    -- 'All products' / 'Category: X' / 'Product: SKU · name'
    uplift REAL                            -- expected % change; NULL = measure it from past sales
);
CREATE TABLE IF NOT EXISTS suppliers (
    supplier_id   TEXT PRIMARY KEY,
    company_name  TEXT NOT NULL,
    category      TEXT,
    contact_person TEXT,
    phone_numbers TEXT,                    -- several values separated by ';'
    emails        TEXT,
    whatsapp_numbers TEXT,
    address       TEXT,
    postal_code   TEXT,
    products      TEXT,
    name_zh       TEXT,
    country       TEXT,
    website       TEXT,
    source_type   TEXT,
    source_urls   TEXT,
    retrieved_on  TEXT,
    retail_supply_evidence TEXT,
    minimum_order TEXT,
    delivery_details TEXT,
    contact_status TEXT DEFAULT 'Not contacted',
    last_contacted TEXT,
    notes         TEXT
);
CREATE TABLE IF NOT EXISTS product_suppliers (   -- preferred supplier per product
    sku         TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supplier_contacts (   -- messages sent or opened from the app
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT DEFAULT (datetime('now','localtime')),
    supplier_id TEXT NOT NULL,
    channel     TEXT,
    kind        TEXT,
    subject     TEXT,
    body        TEXT
);
CREATE TABLE IF NOT EXISTS stock_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    batch_code TEXT NOT NULL,
    received_on TEXT NOT NULL,
    expires_on TEXT NOT NULL,
    initial_qty REAL NOT NULL CHECK(initial_qty > 0),
    remaining_qty REAL NOT NULL CHECK(remaining_qty >= 0),
    unit_cost REAL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(sku, batch_code)
);
CREATE INDEX IF NOT EXISTS idx_batches_expiry ON stock_batches(expires_on, sku);
CREATE TABLE IF NOT EXISTS batch_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    ts TEXT DEFAULT (datetime('now','localtime')),
    kind TEXT NOT NULL,
    qty REAL NOT NULL,
    remaining_after REAL NOT NULL,
    unit_cost REAL,
    note TEXT NOT NULL DEFAULT ''
);
"""


@contextmanager
def connect(path: Path | None = None):
    path = path or current()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init(path: Path | None = None):
    with connect(path) as con:
        con.executescript(SCHEMA)
        # Existing workspaces keep all records; add only the new optional fields.
        have = {r[1] for r in con.execute("PRAGMA table_info(products)")}
        for col, kind in [("location", "TEXT DEFAULT ''"), ("perishable", "INTEGER")]:
            if col not in have:
                con.execute(f"ALTER TABLE products ADD COLUMN {col} {kind}")
        from .evidence import SCHEMA as EVIDENCE_SCHEMA
        con.executescript(EVIDENCE_SCHEMA)


def reset(path: Path | None = None):
    with connect(path) as con:
        con.executescript(
            "DROP TABLE IF EXISTS products; DROP TABLE IF EXISTS sales;"
            "DROP TABLE IF EXISTS stock_moves; DROP TABLE IF EXISTS models;"
            "DROP TABLE IF EXISTS sales_detail; DROP TABLE IF EXISTS drafts;"
            "DROP TABLE IF EXISTS draft_events; DROP TABLE IF EXISTS purchase_orders;"
            "DROP TABLE IF EXISTS events; DROP TABLE IF EXISTS product_suppliers;"
            "DROP TABLE IF EXISTS batch_events; DROP TABLE IF EXISTS stock_batches;"
            "DROP TABLE IF EXISTS advice_snapshots; DROP TABLE IF EXISTS inventory_freshness;"
            "DROP TABLE IF EXISTS inventory_observations; DROP TABLE IF EXISTS advice_feedback;"
            "DROP TABLE IF EXISTS advice_zero_confirmations; DROP TABLE IF EXISTS advice_stockout_reports;"
        )   # kept: settings (alerts, store details), the supplier directory and its contact history
    init(path)
    with connect(path) as con:   # the order list belongs to the old sales data
        con.execute("DELETE FROM settings WHERE key='order_draft'")


def query(sql: str, params=(), path: Path | None = None) -> pd.DataFrame:
    with connect(path) as con:
        return pd.read_sql_query(sql, con, params=params)


def products(path: Path | None = None) -> pd.DataFrame:
    return query("SELECT * FROM products WHERE active=1 ORDER BY name", path=path)


def sales(sku: str | None = None, path: Path | None = None) -> pd.DataFrame:
    if sku:
        df = query("SELECT * FROM sales WHERE sku=? ORDER BY date", (sku,), path)
    else:
        df = query("SELECT * FROM sales ORDER BY date", path=path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def upsert_products(df: pd.DataFrame, path: Path | None = None, *, stock_source="unverified"):
    cols = ["sku", "name", "category", "location", "unit_cost", "unit_price",
            "lead_time_days", "current_stock", "min_stock", "shelf_life_days", "perishable"]
    with connect(path) as con:
        old_stock = dict(con.execute("SELECT sku, current_stock FROM products").fetchall())
        for r in df.to_dict("records"):
            # Missing column on a sales import preserves location; a cleared editor cell clears it.
            if "location" in r:
                r["location"] = str(r["location"]).strip() if pd.notna(r["location"]) else ""
            present = [c for c in cols if c in r and pd.notna(r[c])]
            con.execute(
                f"INSERT INTO products ({','.join(present)}) VALUES ({','.join('?' * len(present))}) "
                f"ON CONFLICT(sku) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in present if c != "sku"),
                [r[c] for c in present],
            )
            if "current_stock" in present and old_stock.get(r["sku"]) != r["current_stock"]:
                counted = pd.Timestamp.today().date().isoformat() if stock_source == "manual_product_edit" else None
                con.execute("INSERT INTO inventory_freshness VALUES (?,?,datetime('now','localtime'),?) "
                            "ON CONFLICT(sku) DO UPDATE SET counted_on=excluded.counted_on,recorded_at=excluded.recorded_at,source=excluded.source",
                            (r["sku"], counted, stock_source))
                if counted:
                    con.execute("INSERT INTO inventory_observations (sku,observed_on,recorded_at,stock,source) VALUES (?,?,datetime('now','localtime'),?,?)",
                                (r["sku"], counted, r["current_stock"], stock_source))
            if "current_stock" in present:
                from . import batches
                batches.reconcile(con, r["sku"], float(r["current_stock"]), "Product stock count updated")


PERISHABLE_SHELF_DAYS = 30          # keeps a month or less: perishable
COLD_WORDS = ("frozen", "chilled", "fridge", "freezer")
FRESH_WORDS = ("bakery", "bread", "dairy", "fresh", "produce", "fruit", "vegetable", "meat", "seafood", "fish", "deli")


def perishable(prods: pd.DataFrame, owner: bool = True) -> pd.Series:
    """True for perishable products. The owner's own choice wins (`owner=False` ignores it); otherwise a product is
    perishable when it keeps 30 days or less, must be kept cold, or has no shelf life on record but is fresh food."""
    def auto(r) -> bool:
        cat = str(r.get("category") or "").lower()
        if any(w in cat for w in COLD_WORDS):
            return True
        if pd.notna(r.get("shelf_life_days")):
            return float(r["shelf_life_days"]) <= PERISHABLE_SHELF_DAYS
        return any(w in cat for w in FRESH_WORDS)
    return pd.Series([bool(r["perishable"]) if owner and pd.notna(r.get("perishable")) else auto(r)
                      for r in prods.to_dict("records")], index=prods.index, dtype=bool)


def set_perishable(choices: dict[str, bool], path: Path | None = None):
    """Save the owner's perishable / non-perishable choice per sku; a choice equal to the automatic one is stored as
    NULL, so the product keeps following its shelf life and category."""
    prods = products(path).set_index("sku")
    auto = perishable(prods.reset_index(), owner=False).set_axis(prods.index)
    with connect(path) as con:
        con.executemany("UPDATE products SET perishable=? WHERE sku=?",
                        [(None if bool(v) == bool(auto.get(s)) else int(bool(v)), s) for s, v in choices.items() if s in auto.index])


SHELVES = {"A": "Fridge", "B": "Dry goods", "C": "Household"}   # shelf letter → what is kept there
COOL_WORDS = COLD_WORDS + ("dairy", "milk", "meat", "seafood", "fish", "deli")
HOUSEHOLD_WORDS = ("household", "cleaning", "laundry", "toiletr", "personal care", "detergent", "tissue")


def parse_location(value) -> tuple[str, int] | None:
    """'A1', 'A-1' or 'a 01' → ('A', 1). Anything else ('', 'A-01-02', 'back room') → None."""
    m = re.fullmatch(r"\s*([A-Za-z]+)\s*-?\s*0*([1-9]\d*)\s*", str(value))
    return (m.group(1).upper(), int(m.group(2))) if m else None


def shelf_for(category) -> str:
    """Shelf letter a product belongs on: kept cold → A, household goods → C, everything else (dry goods) → B."""
    cat = str(category or "").lower()
    if any(w in cat for w in COOL_WORDS):
        return "A"
    return "C" if any(w in cat for w in HOUSEHOLD_WORDS) else "B"


def shelf_name(location) -> str:
    """'A1' → 'Fridge (Shelf A)'; a shelf letter without a name → 'Shelf D'; no valid location → ''."""
    hit = parse_location(location)
    if not hit:
        return ""
    return f"{SHELVES[hit[0]]} (Shelf {hit[0]})" if hit[0] in SHELVES else f"Shelf {hit[0]}"


def fill_locations(path: Path | None = None) -> int:
    """Give every product without a location the next free position on its shelf, e.g. A1, and save it, so labels
    stay put when products are added later. Frozen goods come first, then the shortest shelf life. Locations the
    owner typed are never changed. Returns how many were filled."""
    prods = products(path)
    empty = prods[prods["location"].fillna("").astype(str).str.strip() == ""]
    if empty.empty:
        return 0
    taken = {hit for hit in prods["location"].map(parse_location) if hit}
    empty = empty.assign(_frozen=~empty["category"].fillna("").str.lower().str.contains("frozen"),
                         _days=empty["shelf_life_days"].fillna(float("inf"))).sort_values(["_frozen", "_days", "name"])
    filled = []
    for r in empty.itertuples():
        letter, n = shelf_for(r.category), 1
        while (letter, n) in taken:
            n += 1
        taken.add((letter, n))
        filled.append((f"{letter}{n}", r.sku))
    with connect(path) as con:
        con.executemany("UPDATE products SET location=? WHERE sku=?", filled)
    return len(filled)


def upsert_sales(df: pd.DataFrame, path: Path | None = None) -> int:
    # plain Python lists: iterating pandas' Arrow-backed columns row by row is slow
    rows = list(zip(df["date"].dt.strftime("%Y-%m-%d").tolist(), df["sku"].astype(str).tolist(),
                    df["qty"].astype(float).tolist(), df["revenue"].fillna(0).astype(float).tolist()))
    with connect(path) as con:
        con.executemany(
            "INSERT INTO sales (date, sku, qty, revenue) VALUES (?,?,?,?) "
            "ON CONFLICT(date, sku) DO UPDATE SET qty=excluded.qty, revenue=excluded.revenue",
            rows,
        )
    return len(rows)


def replace_detail(df: pd.DataFrame, path: Path | None = None) -> int:
    """Store line-level rows, replacing any existing rows for the same dates."""
    if df.empty:
        return 0
    df = df.assign(date=df["date"].dt.strftime("%Y-%m-%d"))
    with connect(path) as con:
        con.executemany("DELETE FROM sales_detail WHERE date=?", [(d,) for d in df["date"].unique()])
        df[["date", "hour", "sku", "qty", "revenue", "customer_type", "txn", "customer"]].to_sql(
            "sales_detail", con, if_exists="append", index=False)
    return len(df)


def detail(days: int | None = None, path: Path | None = None) -> pd.DataFrame:
    """Line-level sales; with `days`, only the last `days` days up to the latest record (filtered in SQL)."""
    sql = "SELECT d.*, p.name, p.category FROM sales_detail d LEFT JOIN products p USING(sku)"
    if days:
        sql += f" WHERE d.date > date((SELECT MAX(date) FROM sales_detail), '-{int(days)} days')"
    df = query(sql, path=path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def record_move(sku: str, kind: str, qty: float, note: str = "", path: Path | None = None,
                batch_id: int | None = None):
    """Record a stock movement and update current stock immediately."""
    from . import batches
    qty = batches.quantity(qty, allow_zero=True)
    delta = {"in": qty, "out": -qty, "waste": -qty, "return": -qty, "adjust": 0}[kind]
    with connect(path) as con:
        con.execute("BEGIN IMMEDIATE")
        batches.apply_movement(con, sku, kind, qty, batch_id, note)
        con.execute("INSERT INTO stock_moves (sku, kind, qty, note) VALUES (?,?,?,?)",
                    (sku, kind, qty, note))
        if kind == "adjust":
            con.execute("UPDATE products SET current_stock=? WHERE sku=?", (qty, sku))
        else:
            con.execute("UPDATE products SET current_stock=MAX(0, COALESCE(current_stock, 0)+?) WHERE sku=?",
                        (delta, sku))
        balance = con.execute("SELECT current_stock FROM products WHERE sku=?", (sku,)).fetchone()
        if balance is not None:
            source = "physical_count" if kind == "adjust" else "movement_balance"
            con.execute("INSERT INTO inventory_observations (sku,observed_on,recorded_at,stock,source) VALUES (?,date('now','localtime'),datetime('now','localtime'),?,?)",
                        (sku, balance[0], source))
            if kind == "adjust":
                con.execute("INSERT INTO inventory_freshness VALUES (?,date('now','localtime'),datetime('now','localtime'),'physical_count') "
                            "ON CONFLICT(sku) DO UPDATE SET counted_on=excluded.counted_on,recorded_at=excluded.recorded_at,source=excluded.source", (sku,))
        if kind == "out":
            today = pd.Timestamp.today().strftime("%Y-%m-%d")
            con.execute(
                "INSERT INTO sales (date, sku, qty, revenue) "
                "SELECT ?, ?, ?, ? * unit_price FROM products WHERE sku=? "
                "ON CONFLICT(date, sku) DO UPDATE SET qty=qty+excluded.qty, revenue=revenue+excluded.revenue",
                (today, sku, qty, qty, sku),
            )


def moves(limit: int = 50, path: Path | None = None) -> pd.DataFrame:
    return query("SELECT m.ts, m.sku, p.name, m.kind, m.qty, m.note FROM stock_moves m "
                 "LEFT JOIN products p USING(sku) ORDER BY m.id DESC LIMIT ?", (limit,), path)


def get_setting(key: str, default=None, path: Path | None = None):
    df = query("SELECT value FROM settings WHERE key=?", (key,), path)
    if df.empty:
        return default
    try:
        return json.loads(df["value"].iloc[0])
    except (TypeError, ValueError):     # older workspaces stored some settings as plain text (e.g. SG)
        return df["value"].iloc[0]


def set_setting(key: str, value, path: Path | None = None):
    with connect(path) as con:
        con.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
