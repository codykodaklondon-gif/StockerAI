"""Photo import: receipts, supplier invoices, shopping lists and product lists, read by the AI.

The AI only transcribes what is on the photo (items, quantities, prices), says what kind of
document it is and suggests a category per item. Everything after that is code: matching to
existing products, checking numbers, and writing to the right place in the database. Nothing
is saved until the owner has checked the table and pressed Save.

Where each document type goes:
  sales      -> sales history (added to that day's sales), new products created
  purchase   -> stock received (stock on hand goes up) and latest unit costs
  shopping   -> lines on the Final order list
  catalogue  -> products: names, categories, prices, costs and stock counts
"""
import base64
import difflib
import hashlib
import io
import re
from datetime import date

import pandas as pd

from . import agent, db, importer, orders

DOC_TYPES = {
    "sales": "Sales receipt / sales record",
    "purchase": "Supplier invoice / delivery note",
    "shopping": "Shopping list (things to buy)",
    "catalogue": "Product, price or stock list",
}
DESTINATION = {
    "sales": "Adds these quantities to that day's **sales history** (forecasts are retrained). New products are created.",
    "purchase": "Adds the quantities to **stock on hand** as a delivery and updates each product's **unit cost**. "
                "New products are created.",
    "shopping": "Adds the lines to your **Final order list** (existing lines for the same product get the new quantity).",
    "catalogue": "Creates or updates **products**: name, category, selling price, unit cost, and stock count if the list has one.",
}
COLUMNS = ["include", "name", "product", "category", "qty", "unit_price", "unit_cost", "stock", "read_as"]
NEW = "➕ New product"
MAX_SIDE = 1600           # photos are shrunk to this many pixels on the long side before sending
MAX_PRODUCTS = 400        # existing products listed to the AI for matching
SEEN_KEY = "scanned_photos"

# Common shop categories offered for new products, and keywords used when the AI gives none
CATEGORY_HINTS = {
    "Bakery": ["bread", "bun", "cake", "pastry", "croissant", "roti", "loaf", "toast", "kaya", "muffin", "面包", "蛋糕"],
    "Dairy": ["milk", "egg", "cheese", "yogurt", "yoghurt", "butter", "cream", "牛奶", "鸡蛋", "奶"],
    "Beverage": ["coffee", "tea", "teh", "kopi", "juice", "drink", "water", "soda", "cola", "milo", "beer", "咖啡", "茶", "饮料", "水"],
    "Snacks": ["chip", "crisp", "biscuit", "cookie", "candy", "chocolate", "snack", "nut", "饼干", "零食", "糖"],
    "Grocery": ["rice", "noodle", "flour", "sugar", "salt", "oil", "sauce", "can", "pasta", "bean", "米", "面", "油", "酱"],
    "Frozen": ["frozen", "ice cream", "dumpling", "nugget", "冰淇淋", "冷冻"],
    "Household": ["tissue", "detergent", "soap", "shampoo", "toilet", "bleach", "sponge", "纸巾", "洗"],
    "Fresh produce": ["apple", "banana", "orange", "vegetable", "fruit", "tomato", "onion", "potato", "veg", "蔬菜", "水果"],
    "Meat & seafood": ["chicken", "pork", "beef", "fish", "prawn", "meat", "seafood", "鸡", "猪", "牛", "鱼", "虾"],
}

SYSTEM = """You read photos of documents from a small shop and transcribe them into JSON.

Return only a JSON object:
{"document_type": "sales" | "purchase" | "shopping" | "catalogue",
 "date": "YYYY-MM-DD" or null,
 "party": "supplier or shop name on the document, or null",
 "notes": "anything unclear, in one or two short sentences, or empty",
 "items": [{"name": str, "qty": number or null, "unit_price": number or null, "line_total": number or null,
            "unit_cost": number or null, "stock": number or null, "category": str, "sku": str or null,
            "unclear": true/false}]}

document_type:
- "sales": a receipt or record of things the shop SOLD to customers (POS receipt, daily sales sheet).
- "purchase": an invoice, bill or delivery note from a SUPPLIER for goods the shop BOUGHT.
- "shopping": a list of things the shop wants to buy (handwritten or typed), usually without prices.
- "catalogue": a product list, price list, menu or stock count.

Rules:
- Copy names, quantities and prices exactly as written. Never invent or calculate a number that is not on the
  photo; use null when a value is missing or unreadable, and set "unclear": true for any line you are unsure of.
- Leave out totals, subtotals, tax, discounts, deposits, payment and change lines; only real items.
- "name" is the item without the quantity written beside it ("eggs 10 trays" has name "eggs" and qty 10), but keep
  a pack size that is part of the product ("Eggs 12s", "Fresh Milk 1L", "Noodles 5s").
- "qty" is the number of units (a line "3 x 2.50" has qty 3 and unit_price 2.50). For a stock count, put the
  count in "stock" and leave qty null.
- "unit_price" is the price on the line: the selling price on a receipt or price list, the price paid on an
  invoice. "unit_cost" only when a list shows a separate cost / buying price column next to the selling price.
- "category": choose the best fit from the shop's existing categories when one fits; otherwise a short, common
  category name (for example Bakery, Dairy, Beverage, Snacks, Grocery, Frozen, Household).
- "sku": when an item is clearly the same product as one in the shop's product list, give that product's sku;
  otherwise null.
- Names can be in any language; keep them as written."""


def prepare(data: bytes) -> tuple[str, str]:
    """(base64 JPEG, media type) of a photo, upright and shrunk so it reads well and uploads fast."""
    from PIL import Image, ImageOps
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except Exception as e:
        raise ValueError("This file is not a readable PNG or JPEG image.") from e
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return base64.standard_b64encode(out.getvalue()).decode("ascii"), "image/jpeg"


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def already_saved(prints: list[str]) -> list[str]:
    """Fingerprints of photos that were saved before (so the same receipt is not counted twice)."""
    seen = set(db.get_setting(SEEN_KEY, []) or [])
    return [p for p in prints if p in seen]


def read(photos: list[bytes]) -> dict:
    """Send the photos to the AI and return its transcription, cleaned and matched to existing products."""
    prods = db.products()
    cats = sorted(prods["category"].dropna().astype(str).unique())
    listing = "\n".join(f"{r.sku} | {r.name} | {r.category}" for r in prods.head(MAX_PRODUCTS).itertuples())
    text = (f"The shop's existing categories: {', '.join(cats) or 'none yet'}.\n"
            f"The shop's products (sku | name | category):\n{listing or '(none yet)'}\n\n"
            f"Transcribe the {'photo' if len(photos) == 1 else f'{len(photos)} photos (pages of one document)'}.")
    raw = agent.complete_vision(SYSTEM, text, [prepare(p) for p in photos])
    return tidy(raw, prods)


def _number(v) -> float | None:
    """A number from the AI's JSON, which may arrive as text such as "S$2.50" or "1,5"."""
    if v is None or isinstance(v, bool):
        return None
    x = importer.to_number(pd.Series([v], dtype=object if not isinstance(v, (int, float)) else float)).iloc[0]
    return float(x) if pd.notna(x) else None


def label(sku: str, name: str) -> str:
    return f"{name} ({sku})"


def match(name: str, prods: pd.DataFrame, hint: str | None = None) -> str | None:
    """SKU of the existing product this line is, or None for a new product."""
    if prods.empty:
        return None
    skus = set(prods["sku"])
    if hint and str(hint) in skus:
        return str(hint)
    names = {str(n).lower(): s for n, s in zip(prods["name"], prods["sku"])}
    key = str(name).lower().strip()
    if key in names:
        return names[key]
    close = difflib.get_close_matches(key, list(names), n=1, cutoff=0.82)
    return names[close[0]] if close else None


def guess_category(name: str, existing: list[str]) -> str:
    """Category from keywords, preferring a matching existing category; 'General' when nothing fits."""
    low = str(name).lower()
    for cat, words in CATEGORY_HINTS.items():
        if any(w in low for w in words):
            same = next((c for c in existing if c.lower() == cat.lower() or c.lower().rstrip("s") == cat.lower().rstrip("s")), None)
            return same or cat
    return "General"


def tidy(raw: dict, prods: pd.DataFrame) -> dict:
    """Turn the AI's JSON into the review table: numbers checked, products matched, a category on every line."""
    kind = raw.get("document_type") if raw.get("document_type") in DOC_TYPES else "sales"
    try:
        when = pd.Timestamp(raw.get("date")).date() if raw.get("date") else None
    except (ValueError, TypeError):
        when = None
    if when and (when > date.today() or when.year < 2000):
        when = None                          # a misread date is worse than none: the owner picks it
    existing = sorted(prods["category"].dropna().astype(str).unique()) if not prods.empty else []
    by_sku = prods.set_index("sku") if not prods.empty else pd.DataFrame()
    rows = []
    for it in raw.get("items") or []:
        if not isinstance(it, dict) or not str(it.get("name") or "").strip():
            continue
        name = str(it["name"]).strip()[:120]
        qty, price, total, cost, stock = (_number(it.get(k)) for k in ("qty", "unit_price", "line_total", "unit_cost", "stock"))
        if price is None and total is not None and qty:
            price = round(total / qty, 2)    # only when both numbers are on the document
        if qty is None and total is not None and price:
            qty = round(total / price, 3)
        sku = match(name, prods, it.get("sku"))
        cat = str(it.get("category") or "").strip()[:40]
        if sku is not None:
            cat = str(by_sku.loc[sku, "category"])       # an existing product keeps its category
        elif not cat:
            cat = guess_category(name, existing)
        else:                                           # reuse the shop's spelling ("Beverages" vs "Beverage")
            cat = next((c for c in existing if c.lower().rstrip("s") == cat.lower().rstrip("s")), cat)
        read_as = []
        if it.get("unclear"):
            read_as.append("unclear on photo")
        if qty is None and kind != "catalogue":
            read_as.append("no quantity")
        rows.append({"include": not it.get("unclear") and (qty is not None or kind == "catalogue"),
                     "name": name, "product": label(sku, by_sku.loc[sku, "name"]) if sku else NEW, "category": cat,
                     "qty": qty, "unit_price": price if kind != "purchase" else None,
                     "unit_cost": price if kind == "purchase" else cost, "stock": stock,
                     "read_as": ", ".join(read_as)})
    return {"document_type": kind, "date": when, "party": str(raw.get("party") or "")[:80],
            "notes": str(raw.get("notes") or "")[:400], "items": pd.DataFrame(rows, columns=COLUMNS)}


# ------------------------------------------------------------------ saving

def new_sku(name: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", str(name).upper()).strip("-")[:24] or "ITEM"
    sku, n = base, 2
    while sku in taken:
        sku, n = f"{base}-{n}", n + 1
    return sku


def _lines(df: pd.DataFrame, prods: pd.DataFrame) -> pd.DataFrame:
    """Ticked lines with a sku (new products get one), numbers as floats."""
    df = df[df["include"].fillna(False).astype(bool) & df["name"].fillna("").astype(str).str.strip().ne("")].copy()
    sku_of = {label(s, n): s for s, n in zip(prods["sku"], prods["name"])}
    taken = set(prods["sku"])
    skus = []
    for r in df.itertuples():
        sku = sku_of.get(r.product)
        if sku is None:
            sku = new_sku(r.name, taken)
            taken.add(sku)
        skus.append(sku)
    df["sku"] = skus
    df["is_new"] = [p not in sku_of for p in df["product"]]
    for c in ("qty", "unit_price", "unit_cost", "stock"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["category"] = df["category"].fillna("").astype(str).str.strip().replace("", "General")
    return df


def _new_products(df: pd.DataFrame):
    new = df[df["is_new"]].drop_duplicates("sku")
    if not new.empty:
        db.upsert_products(pd.DataFrame({"sku": new["sku"], "name": new["name"].str.strip(), "category": new["category"],
                                         "unit_price": new["unit_price"], "unit_cost": new["unit_cost"]}))


def save(kind: str, df: pd.DataFrame, when: date, party: str = "", photos: list[str] | None = None,
         draft: pd.DataFrame | None = None) -> dict:
    """Write the checked lines to the database for this document type. Returns what was done."""
    if kind not in DOC_TYPES:
        raise ValueError("Choose what kind of document this is.")
    prods = db.products()
    lines = _lines(df, prods)
    if lines.empty:
        raise ValueError("No lines are ticked. Tick the lines to save.")
    need = {"sales": "qty", "purchase": "qty", "shopping": "qty"}.get(kind)
    if need and (lines[need].isna() | (lines[need] <= 0)).any():
        bad = ", ".join(lines.loc[lines[need].isna() | (lines[need] <= 0), "name"].head(3))
        raise ValueError(f"Enter a quantity above 0 for every ticked line (missing for {bad}).")
    if kind == "sales" and when > date.today():
        raise ValueError("The sales date cannot be in the future.")
    note = f"Photo import: {DOC_TYPES[kind]}" + (f" · {party}" if party else "") + f" · {when:%d %b %Y}"
    done = {"kind": kind, "lines": len(lines), "new_products": int(lines.drop_duplicates("sku")["is_new"].sum())}

    if kind == "catalogue":
        rows = pd.DataFrame({"sku": lines["sku"], "name": lines["name"].str.strip(), "category": lines["category"],
                             "unit_price": lines["unit_price"], "unit_cost": lines["unit_cost"],
                             "current_stock": lines["stock"]})
        old = prods.set_index("sku")["name"] if not prods.empty else pd.Series(dtype=str)
        rows.loc[rows["sku"].isin(old.index), "name"] = rows["sku"].map(old)   # keep the shop's own product names
        db.upsert_products(rows.drop_duplicates("sku", keep="last"))
        done["stock_counts"] = int(lines["stock"].notna().sum())
    else:
        _new_products(lines)
        if not prods.empty:   # the owner may have corrected an existing product's category in the table
            cat = prods.set_index("sku")["category"]
            moved = lines[~lines["is_new"] & (lines["category"] != lines["sku"].map(cat))].drop_duplicates("sku")
            with db.connect() as con:
                con.executemany("UPDATE products SET category=? WHERE sku=?", [(r.category, r.sku) for r in moved.itertuples()])
            done["recategorised"] = len(moved)

    if kind == "sales":
        day = pd.Timestamp(when).strftime("%Y-%m-%d")
        price = prods.set_index("sku")["unit_price"] if not prods.empty else pd.Series(dtype=float)
        g = lines.assign(revenue=lines["qty"] * lines["unit_price"].fillna(lines["sku"].map(price)).fillna(0)) \
            .groupby("sku", as_index=False)[["qty", "revenue"]].sum()
        with db.connect() as con:      # added to the day's sales: a receipt is part of the day, not all of it
            con.executemany("INSERT INTO sales (date, sku, qty, revenue) VALUES (?,?,?,?) "
                            "ON CONFLICT(date, sku) DO UPDATE SET qty=qty+excluded.qty, revenue=revenue+excluded.revenue",
                            [(day, r.sku, float(r.qty), float(r.revenue)) for r in g.itertuples()])
        done["units"] = float(g["qty"].sum())
    elif kind == "purchase":
        for r in lines.itertuples():
            db.record_move(r.sku, "in", float(r.qty), note)
        costs = lines.dropna(subset=["unit_cost"]).drop_duplicates("sku", keep="last")
        if not costs.empty:
            with db.connect() as con:
                con.executemany("UPDATE products SET unit_cost=? WHERE sku=?",
                                [(float(r.unit_cost), r.sku) for r in costs.itertuples()])
        done["units"], done["costs_updated"] = float(lines["qty"].sum()), len(costs)
    elif kind == "shopping":
        draft = (draft if draft is not None else pd.DataFrame(columns=orders.COLUMNS)).copy()
        cost = db.products().set_index("sku")["unit_cost"]
        for r in lines.itertuples():
            hit = draft.index[draft["sku"] == r.sku]
            if len(hit):
                draft.loc[hit, ["order", "qty"]] = [True, int(round(r.qty))]
            else:
                draft.loc[len(draft)] = {"order": True, "sku": r.sku, "product": r.name, "suggested": "from photo",
                                         "qty": int(round(r.qty)), "unit_cost": float(cost.get(r.sku) or 0),
                                         "supplier_id": None, "note": ""}
        orders.save(draft.reset_index(drop=True))
        done["units"] = float(lines["qty"].sum())

    if photos:
        seen = list(db.get_setting(SEEN_KEY, []) or [])
        db.set_setting(SEEN_KEY, (seen + [p for p in photos if p not in seen])[-500:])
    return done
