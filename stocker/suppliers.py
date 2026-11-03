"""Supplier directory, product-to-supplier matching and the store's own details.

The directory imports from CSV / Excel (the bundled Singapore list or the owner's own file).
Contact status is tracked by the app, so re-importing a file never resets who was contacted.
"""
import re
from pathlib import Path

import pandas as pd

from . import db, importer

FIELDS = ["supplier_id", "company_name", "category", "contact_person", "phone_numbers", "emails",
          "whatsapp_numbers", "address", "postal_code", "products", "name_zh", "country", "website",
          "source_type", "source_urls", "retrieved_on", "retail_supply_evidence", "minimum_order",
          "delivery_details", "contact_status", "last_contacted", "notes"]
TRACKED = ("contact_status", "last_contacted")      # owned by the app once a supplier exists
STATUSES = ["Not contacted", "Contacted", "Awaiting reply", "Ordered", "Not suitable"]
BUNDLED = Path(__file__).resolve().parent.parent / "assets" / "singapore_suppliers.xlsx"
ALIASES = {"id": "supplier_id", "supplierid": "supplier_id", "company": "company_name", "name": "company_name",
           "supplier": "company_name", "suppliername": "company_name", "companyname": "company_name",
           "phone": "phone_numbers", "phones": "phone_numbers", "tel": "phone_numbers", "telephone": "phone_numbers",
           "phonenumbers": "phone_numbers", "email": "emails", "emailaddress": "emails", "whatsapp": "whatsapp_numbers",
           "whatsappnumbers": "whatsapp_numbers", "contact": "contact_person", "contactname": "contact_person",
           "contactperson": "contact_person", "postcode": "postal_code", "postalcode": "postal_code",
           "minimumorder": "minimum_order", "moq": "minimum_order", "delivery": "delivery_details",
           "deliverydetails": "delivery_details", "status": "contact_status", "contactstatus": "contact_status"}


def split(value) -> list[str]:
    """'a; b' -> ['a', 'b'] for the multi-value phone / email columns."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    return [v.strip() for v in re.split(r"[;,\n]", str(value)) if v.strip()]


def is_mobile(number: str) -> bool:
    """Singapore mobiles start with 8 or 9; landlines (6...) cannot receive SMS or WhatsApp."""
    digits = re.sub(r"\D", "", number)
    return bool(re.fullmatch(r"(65)?[89]\d{7}", digits))


def mobiles(sup: dict) -> list[str]:
    nums = split(sup.get("whatsapp_numbers")) + split(sup.get("phone_numbers"))
    return list(dict.fromkeys(n for n in nums if is_mobile(n)))


def load() -> pd.DataFrame:
    return db.query("SELECT * FROM suppliers ORDER BY category, company_name")


def get(supplier_id: str) -> dict | None:
    df = db.query("SELECT * FROM suppliers WHERE supplier_id=?", (supplier_id,))
    return df.iloc[0].where(df.iloc[0].notna(), None).to_dict() if not df.empty else None


def label(row) -> str:
    return f"{row['company_name']} ({row['supplier_id']})"


# ------------------------------------------------------------------ import / edit

def _clean(df: pd.DataFrame) -> pd.DataFrame:
    cols = {}
    for c in df.columns:
        key = re.sub(r"[\s_\-\.]", "", str(c)).lower()
        cols[c] = next((f for f in FIELDS if f.replace("_", "") == key), ALIASES.get(key))
    df = df.rename(columns={c: f for c, f in cols.items() if f}).loc[:, lambda d: ~d.columns.duplicated()]
    df = df[[c for c in FIELDS if c in df.columns]].copy()
    if "company_name" not in df:
        raise ValueError("No company / supplier name column found.")
    for c in df.columns:
        s = df[c].astype("string").str.strip()
        df[c] = s.mask(s.isin(["", "nan", "None", "NaT", "<NA>"]))
    if "retrieved_on" in df:     # Excel dates arrive as '2026-11-02 00:00:00'
        df["retrieved_on"] = df["retrieved_on"].str.slice(0, 10)
    return df[df["company_name"].notna()]


def import_frame(df: pd.DataFrame) -> dict:
    """Add or update suppliers; returns counts. Rows without an id get one; app-tracked status is kept."""
    df = _clean(df)
    existing = set(load()["supplier_id"])
    if "supplier_id" not in df:
        df["supplier_id"] = pd.NA
    n = len(existing) + 1
    for i in df.index[df["supplier_id"].isna()]:
        while f"SUP-{n:04d}" in existing:
            n += 1
        df.at[i, "supplier_id"] = f"SUP-{n:04d}"
        existing.add(f"SUP-{n:04d}")
    present = [c for c in FIELDS if c in df.columns]
    update = [c for c in present if c not in TRACKED and c != "supplier_id"]
    rows = [[None if pd.isna(v) else v for v in r] for r in df[present].itertuples(index=False)]
    before = set(load()["supplier_id"])
    with db.connect() as con:
        con.executemany(
            f"INSERT INTO suppliers ({','.join(present)}) VALUES ({','.join('?' * len(present))}) "
            f"ON CONFLICT(supplier_id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in update), rows)
        con.execute("UPDATE suppliers SET contact_status='Not contacted' WHERE contact_status IS NULL")
    added = len(set(df["supplier_id"]) - before)
    return {"added": added, "updated": len(df) - added}


def import_file(data: bytes, name: str) -> dict:
    import io
    return import_frame(importer.read_file(io.BytesIO(data), name))


def load_bundled() -> dict:
    with open(BUNDLED, "rb") as f:
        return import_file(f.read(), BUNDLED.name)


def save(sup: dict):
    """Insert or update one supplier from the edit form (empty strings become blank)."""
    sup = {k: (v.strip() if isinstance(v, str) else v) or None for k, v in sup.items() if k in FIELDS}
    if not sup.get("supplier_id"):
        ids = set(load()["supplier_id"])
        n = len(ids) + 1
        while f"SUP-{n:04d}" in ids:
            n += 1
        sup["supplier_id"] = f"SUP-{n:04d}"
    sup["contact_status"] = sup.get("contact_status") or "Not contacted"
    cols = list(sup)
    with db.connect() as con:
        con.execute(f"INSERT INTO suppliers ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                    f"ON CONFLICT(supplier_id) DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in cols if c != "supplier_id"),
                    [sup[c] for c in cols])
    return sup["supplier_id"]


def delete(supplier_id: str):
    with db.connect() as con:
        con.execute("DELETE FROM suppliers WHERE supplier_id=?", (supplier_id,))
        con.execute("DELETE FROM product_suppliers WHERE supplier_id=?", (supplier_id,))


def log_contact(supplier_id: str, channel: str, kind: str, subject: str, body: str, status: str = "Contacted"):
    with db.connect() as con:
        con.execute("INSERT INTO supplier_contacts (supplier_id, channel, kind, subject, body) VALUES (?,?,?,?,?)",
                    (supplier_id, channel, kind, subject, body))
        con.execute("UPDATE suppliers SET contact_status=?, last_contacted=date('now','localtime') WHERE supplier_id=?",
                    (status, supplier_id))


def set_status(supplier_id: str, status: str):
    with db.connect() as con:
        con.execute("UPDATE suppliers SET contact_status=? WHERE supplier_id=?", (status, supplier_id))


def history(supplier_id: str) -> pd.DataFrame:
    return db.query("SELECT ts, channel, kind, subject FROM supplier_contacts WHERE supplier_id=? ORDER BY id DESC",
                    (supplier_id,))


# ------------------------------------------------------------------ matching products to suppliers
# Keywords for a product's category; the product's own name words are matched as well.
HINTS = {
    "bakery": ["bakery", "bread", "bak", "confection", "pastr", "cake"],
    "dairy": ["dairy", "milk", "uht", "cheese", "yogurt", "butter", "egg"],
    "beverage": ["beverage", "drink", "coffee", "tea", "juice", "water"],
    "grocery": ["general grocer", "grocer", "provision", "rice", "dry goods", "noodle", "canned", "oil"],
    "household": ["household", "toiletr", "detergent", "tissue", "cleaning"],
    "frozen": ["frozen", "ice cream", "dim sum"],
    "snack": ["snack", "chips", "confection", "biscuit", "nut"],
    "produce": ["fresh produce", "vegetable", "fruit"],
    "meat": ["meat", "poultry", "chicken"],
    "seafood": ["seafood", "fish"],
}
CONFIRMED = "Not confirmed"


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]{4,}", str(text).lower())} - {"pack", "with", "fresh", "instant"}


def suggest(name: str, category: str, sups: pd.DataFrame, top: int = 3) -> list[tuple[str, float]]:
    """Best-matching suppliers for a product as [(supplier_id, score)], by category and product words.
    Suppliers that publish retail supply or have an email get a small bonus."""
    if sups.empty:
        return []
    hints = next((v for k, v in HINTS.items() if k in str(category).lower()), [])
    words = _words(name)
    scored = []
    for r in sups.itertuples():
        text = f"{r.category or ''} {r.products or ''}".lower()
        hit = 2 * sum(h in text for h in hints) + 3 * sum(w in text for w in words)
        if not hit:
            continue
        bonus = (r.retail_supply_evidence not in (None, CONFIRMED)) * 1.0 + bool(r.emails) * 0.5 + bool(r.phone_numbers) * 0.25
        scored.append((r.supplier_id, hit + bonus))
    return sorted(scored, key=lambda x: -x[1])[:top]


def assignments() -> dict[str, str]:
    df = db.query("SELECT sku, supplier_id FROM product_suppliers")
    return dict(zip(df["sku"], df["supplier_id"]))


def assign(mapping: dict[str, str | None]):
    with db.connect() as con:
        for sku, sid in mapping.items():
            if sid:
                con.execute("INSERT INTO product_suppliers (sku, supplier_id) VALUES (?, ?) "
                            "ON CONFLICT(sku) DO UPDATE SET supplier_id=excluded.supplier_id", (sku, sid))
            else:
                con.execute("DELETE FROM product_suppliers WHERE sku=?", (sku,))


def auto_assign(only_missing: bool = True) -> int:
    """Give every product (without one) its best-matching supplier. Returns how many were set."""
    sups, have = load(), assignments()
    new = {}
    for p in db.products().itertuples():
        if only_missing and p.sku in have:
            continue
        best = suggest(p.name, p.category, sups, top=1)
        if best:
            new[p.sku] = best[0][0]
    assign(new)
    return len(new)


# ------------------------------------------------------------------ the store's own details

STORE_FIELDS = {"name": "Store name", "contact_person": "Your name", "phone": "Phone", "email": "Email",
                "address": "Address", "postal_code": "Postal code", "uen": "UEN (optional)",
                "delivery_notes": "Delivery instructions", "payment_terms": "Preferred payment terms"}


def store() -> dict:
    return {k: "" for k in STORE_FIELDS} | (db.get_setting("store", {}) or {})


def save_store(info: dict):
    db.set_setting("store", {k: str(info.get(k) or "").strip() for k in STORE_FIELDS})


def store_missing(info: dict) -> list[str]:
    need = ["name", "contact_person", "address"]
    out = [STORE_FIELDS[k] for k in need if not info.get(k)]
    if not info.get("phone") and not info.get("email"):
        out.append("Phone or email")
    return out
