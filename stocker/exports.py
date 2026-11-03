"""Files the AI assistant can hand to the owner: CSV, Excel and Word.

(The Export page and purchase invoice live in exporter.py; this module serves the chatbot.)

The model only chooses *what* goes in the file (format, title, which data tables, and — for Word —
its own explanatory paragraphs). Every table is filled here from the analysis code, so the numbers
in an exported file are the shop's real figures, never values typed by the model.
"""
import io
import re
import uuid
from collections import OrderedDict
from datetime import datetime

import pandas as pd

from . import advisor, db, engine, profile

FORMATS = {"csv": ("text/csv", "csv"),
           "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
           "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx")}
MAX_FILES = 50          # recent files kept in memory for download


# ------------------------------------------------------------------ data tables (all from code)

def _recommendations(**_):
    r = advisor.recommendations()
    if r.empty:
        return r
    return r[["product", "category", "action", "order_low", "order_high", "stock", "daily_forecast",
              "cover_days", "est_cost", "reason"]].rename(columns={
        "product": "Product", "category": "Category", "action": "Action", "order_low": "Order min",
        "order_high": "Order max", "stock": "Stock", "daily_forecast": "Forecast per day",
        "cover_days": "Days of cover", "est_cost": "Est. cost (S$)", "reason": "Why"})


def _risks(**_):
    r = advisor.risks_frame()
    if r.empty:
        return r
    return r[["severity", "kind", "product", "message", "action", "value_at_risk"]].rename(columns={
        "severity": "Severity", "kind": "Type", "product": "Product", "message": "What is happening",
        "action": "Next step", "value_at_risk": "Value at risk (S$)"}).round(2)


def _forecast(sku: str = "", days: int = 14, **_):
    m = engine.load_models().get(sku)
    if not m:
        raise ValueError(f"No forecast for product code '{sku}'. Use find_product to get the exact code.")
    fc = m.forecast(min(max(int(days), 1), 30), start=advisor.plan_start())
    name = db.query("SELECT name FROM products WHERE sku=?", (sku,))["name"].iloc[0]
    return pd.DataFrame({"Product": name, "Date": fc["date"].dt.strftime("%Y-%m-%d"),
                         "Weekday": fc["date"].dt.day_name(), "Forecast": fc["forecast"].round(1),
                         "Low (80%)": fc["low"].round(1), "High (80%)": fc["high"].round(1)})


def _top_sellers(days: int = 30, **_):
    s = db.sales()
    if s.empty:
        return pd.DataFrame()
    s = s[s["date"] > s["date"].max() - pd.Timedelta(days=int(days))]
    g = s.groupby("sku").agg(units=("qty", "sum"), revenue=("revenue", "sum")).reset_index()
    g = g.merge(db.products()[["sku", "name"]], on="sku").sort_values("revenue", ascending=False)
    return g[["name", "units", "revenue"]].rename(columns={"name": "Product", "units": f"Units ({days} days)",
                                                         "revenue": f"Revenue S$ ({days} days)"}).round(2)


def _inventory(**_):
    p = db.products()
    return p[["sku", "name", "category", "current_stock", "unit_cost", "unit_price", "lead_time_days",
              "shelf_life_days"]].rename(columns={
        "sku": "Code", "name": "Product", "category": "Category", "current_stock": "Stock",
        "unit_cost": "Unit cost (S$)", "unit_price": "Unit price (S$)", "lead_time_days": "Lead time (days)",
        "shelf_life_days": "Shelf life (days)"})


def _customer_insights(**_):
    p = profile.build(56)
    return pd.DataFrame({"Insight": p.get("insights", [])}) if p.get("available") else pd.DataFrame()


def _daily_sales(days: int = 30, **_):
    s = db.sales()
    if s.empty:
        return pd.DataFrame()
    d = s.groupby("date")[["qty", "revenue"]].sum().tail(int(days)).reset_index()
    d["date"] = d["date"].dt.strftime("%Y-%m-%d")
    return d.rename(columns={"date": "Date", "qty": "Units", "revenue": "Revenue (S$)"}).round(2)


DATASETS = {
    "shopping_list": (_recommendations, "What to buy: action, quantity range and reason for every product"),
    "risk_alerts": (_risks, "Current risks: stock-outs, expiry, overstock, demand changes"),
    "forecast": (_forecast, "Daily sales forecast for one product (needs sku; optional days, max 30)"),
    "top_sellers": (_top_sellers, "Best sellers by revenue (optional days, default 30)"),
    "inventory": (_inventory, "Products with stock, cost, price, lead time and shelf life"),
    "customer_insights": (_customer_insights, "Customer habit insights"),
    "daily_sales": (_daily_sales, "Shop-wide daily units and revenue (optional days, default 30)"),
}


def table(name: str, **params) -> pd.DataFrame:
    if name not in DATASETS:
        raise ValueError(f"Unknown table '{name}'. Choose from: {', '.join(DATASETS)}.")
    return DATASETS[name][0](**params)


# ------------------------------------------------------------------ writers

def _safe(name: str, n: int = 31) -> str:
    return re.sub(r"[\[\]:*?/\\]", " ", name).strip()[:n] or "Sheet"


def _csv(title, parts):
    buf = io.StringIO()
    for i, (heading, df, _) in enumerate(p for p in parts if p[1] is not None):
        if i:
            buf.write("\n")
        if len([p for p in parts if p[1] is not None]) > 1:
            buf.write(f"# {heading}\n")
        df.to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8-sig")       # BOM so Excel opens Chinese text correctly


def _xlsx(title, parts):
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        notes = [(h, t) for h, df, t in parts if t]
        if notes or not any(df is not None for _, df, _ in parts):
            rows = [[title], [f"Created by Stocker AI on {datetime.now():%Y-%m-%d %H:%M}"], []]
            for h, t in notes:
                rows += [[h], [t], []]
            pd.DataFrame(rows).to_excel(xw, sheet_name="Summary", index=False, header=False)
            ws = xw.sheets["Summary"]
            ws.column_dimensions["A"].width = 100
            ws["A1"].font = Font(bold=True, size=14)
            for row in ws.iter_rows():
                row[0].alignment = Alignment(wrap_text=True, vertical="top")
        used = set()
        for heading, df, _ in parts:
            if df is None:
                continue
            name = _safe(heading)
            while name in used:
                name = _safe(name[:28] + str(len(used)))
            used.add(name)
            df.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            for cell in ws[1]:
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="C64E35")
            for i, col in enumerate(df.columns, start=1):
                width = max([len(str(col))] + [len(str(v)) for v in df[col].head(200)]) + 2
                ws.column_dimensions[get_column_letter(i)].width = min(width, 60)
            ws.freeze_panes = "A2"
    return buf.getvalue()


def _docx(title, parts):
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Pt, RGBColor
    doc = Document()
    widest = max([len(df.columns) for _, df, _ in parts if df is not None] or [0])
    if widest > 5:                                  # wide tables read better on a landscape page
        sec = doc.sections[0]
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    doc.add_heading(title, level=0)
    note = doc.add_paragraph(f"Created by Stocker AI on {datetime.now():%Y-%m-%d %H:%M}. "
                             "Tables come from your own sales records; suggestions only — you decide.")
    note.runs[0].font.size, note.runs[0].font.color.rgb = Pt(9), RGBColor(0x80, 0x80, 0x80)
    for heading, df, text in parts:
        if heading:
            doc.add_heading(heading, level=1)
        for para in (text or "").split("\n"):
            if para.strip():
                doc.add_paragraph(para.strip())
        if df is not None:
            if df.empty:
                doc.add_paragraph("(No data.)")
                continue
            t = doc.add_table(rows=1, cols=len(df.columns))
            t.style = "Light Grid Accent 1"
            for cell, col in zip(t.rows[0].cells, df.columns):
                cell.text = str(col)
            for _, row in df.head(500).iterrows():
                cells = t.add_row().cells
                for cell, v in zip(cells, row):
                    cell.text = "" if pd.isna(v) else (f"{v:,.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))
            # columns sized by how much text they hold, so long "Why" columns are not squeezed
            sec = doc.sections[-1]
            usable = sec.page_width - sec.left_margin - sec.right_margin
            # short columns get exactly what their longest value / header word needs at 9 pt;
            # free-text columns share whatever room is left
            from docx.shared import Inches
            char, pad = Inches(0.058), Inches(0.14)
            short, free = {}, []
            for i, c in enumerate(df.columns):
                lens = df[c].astype(str).str.len()
                word = max(len(w) for w in str(c).split())
                if lens.mean() > 25:
                    free.append(i)
                else:
                    # header words are bold, so they need a wider allowance per character
                    short[i] = int(max(min(lens.max() + 1, 17) * char, word * Inches(0.078), 3 * char) + pad)
            room = max(usable - sum(short.values()), Inches(1.5) * max(len(free), 1))
            widths = [short[i] if i in short else int(room / len(free)) for i in range(len(df.columns))]
            if sum(widths) > usable:                 # very wide tables: scale everything down
                widths = [int(w * usable / sum(widths)) for w in widths]
            t.autofit = False
            for col, w in zip(t.columns, widths):   # the column grid (read by Word and LibreOffice)
                col.width = w
            for row in t.rows:                      # and each cell (Word prefers these)
                for cell, w in zip(row.cells, widths):
                    cell.width = w
            for row in t.rows:                      # compact table text
                for cell in row.cells:
                    for para in cell.paragraphs:
                        for run in para.runs:
                            run.font.size = Pt(9)
            if len(df) > 500:
                doc.add_paragraph(f"(First 500 of {len(df)} rows.)")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ------------------------------------------------------------------ public entry point + store

_store: "OrderedDict[str, dict]" = OrderedDict()


def build(fmt: str, title: str, sections: list[dict]) -> dict:
    """Create a file. sections: [{"heading": str, "table": dataset name or None, "params": {}, "text": str}]."""
    fmt = (fmt or "").lower().lstrip(".")
    fmt = {"excel": "xlsx", "xls": "xlsx", "word": "docx", "doc": "docx"}.get(fmt, fmt)
    if fmt not in FORMATS:
        raise ValueError("Format must be csv, xlsx (Excel) or docx (Word).")
    title = (title or "Stocker AI export").strip()[:120]
    if not sections:
        raise ValueError("Add at least one section (a data table or text).")
    parts = []
    for s in sections:
        tbl = s.get("table")
        df = table(tbl, **(s.get("params") or {})) if tbl else None
        text = (s.get("text") or "").strip() if fmt != "csv" else ""
        heading = (s.get("heading") or (DATASETS[tbl][1].split(":")[0] if tbl else "")).strip()
        parts.append((heading, df, text))
    if fmt == "csv" and not any(df is not None for _, df, _ in parts):
        raise ValueError("A CSV file needs at least one data table.")
    data = {"csv": _csv, "xlsx": _xlsx, "docx": _docx}[fmt](title, parts)
    fid = uuid.uuid4().hex[:12]
    filename = f"{_safe(title, 60).replace(' ', '_')}.{FORMATS[fmt][1]}"
    _store[fid] = {"id": fid, "name": filename, "mime": FORMATS[fmt][0], "data": data, "format": fmt}
    while len(_store) > MAX_FILES:
        _store.popitem(last=False)
    rows = {h or "table": len(df) for h, df, _ in parts if df is not None}
    return {"file_id": fid, "filename": filename, "format": fmt, "tables": rows}


def get(fid: str) -> dict | None:
    return _store.get(fid)
