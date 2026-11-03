"""Historical data import: CSV / Excel with automatic column detection.

Accepts transaction-level or daily records from any POS export or spreadsheet.
Columns are matched by name (English and Chinese aliases); the merchant does not
need to reformat their file.
"""
import hashlib
import io
import re

import pandas as pd

from . import db

ALIASES = {
    "date":     ["date", "day", "datetime", "invoicedate", "orderdate", "transactiondate",
                 "time", "timestamp", "saledate", "日期", "时间", "销售日期"],
    "sku":      ["sku", "stockcode", "productid", "itemid", "itemcode", "productcode",
                 "barcode", "code", "货号", "编码", "商品编码"],
    "name":     ["name", "product", "productname", "item", "itemname", "description", "article",
                 "articlename", "goods", "商品", "商品名称", "名称", "品名"],
    "qty":      ["qty", "quantity", "units", "unitssold", "sold", "count", "amount_sold",
                 "数量", "销量", "销售数量"],
    "price":    ["price", "unitprice", "sellingprice", "单价", "售价"],
    "revenue":  ["revenue", "sales", "total", "totalamount", "amount", "lineamount",
                 "销售额", "金额", "总额"],
    "category": ["category", "type", "group", "department", "类别", "分类", "品类"],
    "stock":    ["stock", "onhand", "inventory", "currentstock", "stocklevel", "库存", "现有库存"],
    "cost":     ["cost", "unitcost", "purchaseprice", "成本", "进价"],
    "time":     ["time", "hour", "timeslot", "saletime", "小时", "时段", "时间"],
    "customer_type": ["customertype", "membertype", "member", "membership", "customergroup",
                      "会员类型", "客户类型", "会员"],
    "txn":      ["invoice", "invoiceno", "receipt", "receiptno", "orderid", "orderno", "transactionid",
                 "txn", "billno", "ticket", "ticketnumber", "ticketno", "ticketid", "transaction",
                 "单号", "订单号", "小票号"],
    "customer": ["customerid", "memberid", "clientid", "loyaltyid", "会员号", "客户编号", "顾客编号"],
}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-\.()（）]", "", str(s)).lower()


def _is_index_column(name: str) -> bool:
    """Row-number columns that spreadsheets and pandas exports add (e.g. 'Unnamed: 0')."""
    n = _norm(name)
    return n == "" or n.startswith("unnamed") or n in ("index", "rowid", "no", "序号")


def detect_columns(df: pd.DataFrame) -> dict[str, str]:
    """Map our canonical fields to the file's column names."""
    found, used = {}, set()
    normed = {c: _norm(c) for c in df.columns if not _is_index_column(c)}
    # first pass: exact names, trying each field's aliases in priority order
    # (so a file with both "date" and "time" maps date->date and time->time)
    for field, names in ALIASES.items():
        match = next((col for alias in names for col, n in normed.items() if n == alias and col not in used), None)
        if match is not None:
            found[field] = match
            used.add(match)
    # second pass: substring matches for anything still missing
    for field, names in ALIASES.items():
        if field in found:
            continue
        for col, n in normed.items():
            if col not in used and any(a in n for a in names if len(a) > 3):
                found[field] = col
                used.add(col)
                break
    # fallback: a column that parses as dates
    if "date" not in found:
        for col in normed:
            if col in used or df[col].dtype.kind in "if":
                continue
            parsed = pd.to_datetime(df[col], errors="coerce")
            if parsed.notna().mean() > 0.9:
                found["date"] = col
                break
    return found


_CURRENCY = re.compile(r"[^\d,.\-]")


def to_number(s: pd.Series) -> pd.Series:
    """Parse numbers written with currency signs or European decimals: "0,90 €", "$1,234.50", "1.234,56"."""
    if s.dtype.kind in "iuf":
        return s.astype(float)
    x = s.astype("string").str.replace(_CURRENCY, "", regex=True)
    both = x.str.contains(",", regex=False) & x.str.contains(".", regex=False)
    euro = both & (x.str.rfind(",") > x.str.rfind("."))           # 1.234,56
    x = x.mask(euro, x.str.replace(".", "", regex=False))
    x = x.mask(both & ~euro, x.str.replace(",", "", regex=False))  # 1,234.56
    x = x.str.replace(",", ".", regex=False)                       # 0,90
    return pd.to_numeric(x, errors="coerce")


def read_file(file, filename: str) -> pd.DataFrame:
    data = file.read() if hasattr(file, "read") else open(file, "rb").read()
    if filename.lower().endswith((".xlsx", ".xls")):
        try:   # calamine (Rust) parses large workbooks several times faster than openpyxl
            return pd.read_excel(io.BytesIO(data), engine="calamine")
        except ImportError:
            return pd.read_excel(io.BytesIO(data))
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(data), encoding=enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Unable to decode file")


def _text(df: pd.DataFrame, mapping: dict, field: str) -> pd.Series:
    """Optional text column as strings, with blanks as missing."""
    if field not in mapping:
        return pd.Series(pd.NA, index=df.index, dtype="string")
    s = df[mapping[field]].astype("string").str.strip()
    return s.mask(s.isin(["", "nan", "None"]))


def normalise(df: pd.DataFrame, mapping: dict[str, str]) -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    """Return (daily_sales, products, warnings). Line-level detail is kept in `.attrs['detail']`."""
    warnings = []
    if "date" not in mapping:
        raise ValueError("Could not find a date column. Please choose it manually.")
    if "sku" not in mapping and "name" not in mapping:
        raise ValueError("Could not find a product column. Please choose it manually.")

    out = pd.DataFrame()
    stamp = pd.to_datetime(df[mapping["date"]], errors="coerce", dayfirst=False)
    out["date"] = stamp.dt.normalize()
    if "time" in mapping:
        t = df[mapping["time"]]
        hour = pd.to_numeric(t, errors="coerce")
        out["hour"] = hour.where(hour.notna(), pd.to_datetime(t.astype(str), errors="coerce", format="mixed").dt.hour)
    elif (stamp.dt.hour.fillna(0) + stamp.dt.minute.fillna(0)).gt(0).any():
        out["hour"] = stamp.dt.hour          # date column carries a time of day
    else:
        out["hour"] = None
    out["customer_type"] = _text(df, mapping, "customer_type").str.capitalize()
    out["txn"] = _text(df, mapping, "txn")
    # customer ids are hashed on import: no personal identifiers are stored (PDPA data minimisation)
    out["customer"] = _text(df, mapping, "customer").map(
        lambda s: hashlib.sha256(s.encode()).hexdigest()[:12], na_action="ignore")
    out["name"] = df[mapping.get("name", mapping.get("sku"))].astype(str).str.strip()
    out["sku"] = (df[mapping["sku"]].astype(str).str.strip() if "sku" in mapping
                  else out["name"].str.upper().str.replace(r"[^A-Z0-9]+", "-", regex=True).str.strip("-"))
    out["qty"] = to_number(df[mapping["qty"]]) if "qty" in mapping else 1.0
    if "qty" not in mapping:
        warnings.append("No quantity column found: each row is counted as 1 unit sold.")
    price = to_number(df[mapping["price"]]) if "price" in mapping else None
    if "revenue" in mapping:
        out["revenue"] = to_number(df[mapping["revenue"]])
    elif price is not None:
        out["revenue"] = out["qty"] * price
    else:
        out["revenue"] = 0.0
    out["category"] = df[mapping["category"]].astype(str) if "category" in mapping else "General"
    out["stock"] = to_number(df[mapping["stock"]]) if "stock" in mapping else None
    out["cost"] = to_number(df[mapping["cost"]]) if "cost" in mapping else None
    out["price"] = price

    bad = out["date"].isna() | out["qty"].isna()
    if bad.any():
        warnings.append(f"Skipped {int(bad.sum())} rows with unreadable date or quantity.")
    out = out[~bad]
    returns = out["qty"] < 0
    if returns.any():
        warnings.append(f"Ignored {int(returns.sum())} return/negative rows.")
        out = out[~returns]

    daily = (out.groupby(["date", "sku"], as_index=False)
                .agg(qty=("qty", "sum"), revenue=("revenue", "sum")))

    last = out.sort_values("date").groupby("sku").tail(1).set_index("sku")
    prods = out.groupby("sku").agg(name=("name", "first"), category=("category", "first")).reset_index()
    unit_price = (out.groupby("sku")["revenue"].sum() / out.groupby("sku")["qty"].sum()).replace([float("inf")], 0)
    prods["unit_price"] = prods["sku"].map(unit_price).fillna(0).round(2)
    if out["cost"].notna().any():
        prods["unit_cost"] = prods["sku"].map(out.groupby("sku")["cost"].median())
    if out["stock"].notna().any():
        prods["current_stock"] = prods["sku"].map(last["stock"])
    has_profile = out["hour"].notna().any() or out["customer_type"].notna().any() or out["txn"].notna().any()
    daily.attrs["detail"] = out[["date", "hour", "sku", "qty", "revenue", "customer_type", "txn", "customer"]] if has_profile else pd.DataFrame()
    if not has_profile:
        warnings.append("No time, customer type or receipt columns found: customer profiling will be limited.")
    return daily, prods, warnings


def import_dataframe(df: pd.DataFrame, mapping: dict[str, str] | None = None) -> dict:
    mapping = mapping or detect_columns(df)
    daily, prods, warnings = normalise(df, mapping)
    db.init()
    existing = set(db.products()["sku"])
    db.upsert_products(prods)
    n = db.upsert_sales(daily)
    detail_rows = db.replace_detail(daily.attrs.get("detail", pd.DataFrame()))
    return {"detail_rows": detail_rows,
        "rows_in": len(df), "daily_rows": n, "products": len(prods),
        "new_products": len(set(prods["sku"]) - existing),
        "date_from": daily["date"].min().date().isoformat() if n else None,
        "date_to": daily["date"].max().date().isoformat() if n else None,
        "mapping": mapping, "warnings": warnings,
    }
