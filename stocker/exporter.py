"""Export analysis results: tables, CSV, Excel and a printable purchase invoice.

Only formats what the advisor already computed; no quantities or costs are recalculated
here except the invoice arithmetic (quantity x unit cost, tax, total).
"""
import io
from datetime import date
from html import escape

import pandas as pd

STOCKING_COLUMNS = {
    "sku": "SKU", "product": "Product", "category": "Category", "action": "Action",
    "stock": "Stock on hand", "daily_forecast": "Forecast / day", "cover_days": "Days of cover",
    "order_low": "Order min", "order_high": "Order max", "est_cost": "Est. cost (S$)", "reason": "Why",
}
RISK_COLUMNS = {
    "severity": "Severity", "kind": "Risk type", "sku": "SKU", "product": "Product",
    "message": "What is happening", "action": "Next step", "value_at_risk": "Value at risk (S$)",
}


def stocking_table(rec: pd.DataFrame) -> pd.DataFrame:
    """Full stocking list with readable column names."""
    if rec.empty:
        return pd.DataFrame(columns=list(STOCKING_COLUMNS.values()))
    return rec[[c for c in STOCKING_COLUMNS if c in rec.columns]].rename(columns=STOCKING_COLUMNS)


def purchase_table(rec: pd.DataFrame) -> pd.DataFrame:
    """Only the products with a suggested purchase quantity."""
    return stocking_table(rec[rec["order_high"] > 0] if not rec.empty else rec)


def risk_table(risks: pd.DataFrame) -> pd.DataFrame:
    if risks.empty:
        return pd.DataFrame(columns=list(RISK_COLUMNS.values()))
    out = risks[[c for c in RISK_COLUMNS if c in risks.columns]].rename(columns=RISK_COLUMNS)
    return out.assign(Severity=out["Severity"].str.title()).round({"Value at risk (S$)": 2})


def final_table(lines: pd.DataFrame, sups: pd.DataFrame) -> pd.DataFrame:
    """The owner's final order list (ordered lines only), grouped by supplier, with contact details."""
    cols = ["Supplier", "Supplier ID", "SKU", "Product", "Quantity", "Unit cost (S$)", "Amount (S$)",
            "Suggested range", "Note", "Supplier email", "Supplier phone"]
    if lines.empty:
        return pd.DataFrame(columns=cols)
    info = sups.set_index("supplier_id") if not sups.empty else pd.DataFrame(columns=["company_name", "emails", "phone_numbers"])
    sid = lines["supplier_id"].fillna("")
    look = lambda col: [info.loc[s, col] if s in info.index else "" for s in sid]
    out = pd.DataFrame({
        "Supplier": [n or "— no supplier chosen" for n in look("company_name")], "Supplier ID": sid.values,
        "SKU": lines["sku"].values, "Product": lines["product"].values, "Quantity": lines["qty"].values,
        "Unit cost (S$)": lines["unit_cost"].values, "Amount (S$)": lines["amount"].values,
        "Suggested range": lines["suggested"].fillna("").values, "Note": lines["note"].values,
        "Supplier email": look("emails"), "Supplier phone": look("phone_numbers")})
    return out.sort_values(["Supplier", "Product"]).reset_index(drop=True)


def contact_table(summary: pd.DataFrame, sups: pd.DataFrame) -> pd.DataFrame:
    """One row per supplier on the order list, with everything needed to contact them."""
    keep = {"company_name": "Supplier", "category": "Category", "contact_person": "Contact person",
            "emails": "Emails", "phone_numbers": "Phones", "whatsapp_numbers": "WhatsApp", "address": "Address",
            "postal_code": "Postal code", "minimum_order": "Minimum order", "delivery_details": "Delivery",
            "retail_supply_evidence": "Supplies retail?", "contact_status": "Status", "last_contacted": "Last contacted"}
    if summary.empty or sups.empty:
        return pd.DataFrame(columns=["Supplier ID", *keep.values(), "Items", "Units", "Est. cost (S$)"])
    s = summary[summary["supplier_id"] != ""].merge(sups, on="supplier_id", how="left")
    return (s[["supplier_id", *keep, "Items", "Units", "Est. cost (S$)"]]
            .rename(columns={"supplier_id": "Supplier ID", **keep}).fillna(""))


def to_csv(df: pd.DataFrame) -> bytes:
    # utf-8 with BOM so Excel opens Chinese / Vietnamese product names correctly
    return df.to_csv(index=False).encode("utf-8-sig")


def _fit_columns(ws, max_width: int = 60):
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), max_width)


def to_xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    """One worksheet per table, bold frozen header and sensible column widths."""
    from openpyxl.styles import Alignment, Font, PatternFill

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name[:31], index=False)
            ws = xw.sheets[name[:31]]
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="C64E35")
                c.alignment = Alignment(vertical="center", wrap_text=True)
            ws.freeze_panes = "A2"
            _fit_columns(ws)
            for col in ws.iter_cols(min_row=1, max_row=1):
                if "S$" in str(col[0].value):
                    for c in ws[col[0].column_letter][1:]:
                        c.number_format = "#,##0.00"
    return buf.getvalue()


def table_html(df: pd.DataFrame, title: str) -> str:
    """Stand-alone printable HTML page for a table."""
    body = df.to_html(index=False, na_rep="", float_format=lambda x: f"{x:,.2f}", border=0, escape=True)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(title)}</title>
<style>
body{{font-family:Arial,sans-serif;color:#493a31;margin:32px}}
h1{{font-size:22px;margin:0 0 4px}} p{{color:#77695f;margin:0 0 18px;font-size:13px}}
table{{border-collapse:collapse;width:100%;font-size:12px}}
th{{background:#c64e35;color:#fff;text-align:left;padding:7px 8px}}
td{{border-bottom:1px solid #e8dfd3;padding:6px 8px;vertical-align:top}}
tr:nth-child(even) td{{background:#fbf7f0}}
@media print{{body{{margin:0}} th{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
</style></head><body><h1>{escape(title)}</h1>
<p>Exported from Stocker AI on {date.today():%d %b %Y} · {len(df)} rows</p>{body}</body></html>"""


# ------------------------------------------------------------------ invoice

def invoice_lines(lines: pd.DataFrame) -> pd.DataFrame:
    """Invoice rows from final order list lines."""
    return pd.DataFrame({"SKU": lines["sku"].values, "Product": lines["product"].values,
                         "Quantity": lines["qty"].astype(int).values,
                         "Unit cost (S$)": pd.to_numeric(lines["unit_cost"]).round(2).values})


def invoice_totals(lines: pd.DataFrame, tax_rate: float = 0.0) -> tuple[pd.DataFrame, dict]:
    """Add line amounts; returns (lines, totals). Rows with no quantity are dropped."""
    lines = lines.dropna(subset=["Product"])
    lines = lines[pd.to_numeric(lines["Quantity"], errors="coerce").fillna(0) > 0].copy()
    lines["Quantity"] = pd.to_numeric(lines["Quantity"]).astype(int)
    lines["Unit cost (S$)"] = pd.to_numeric(lines["Unit cost (S$)"], errors="coerce").fillna(0).round(2)
    lines["Amount (S$)"] = (lines["Quantity"] * lines["Unit cost (S$)"]).round(2)
    subtotal = round(float(lines["Amount (S$)"].sum()), 2)
    tax = round(subtotal * tax_rate / 100, 2)
    return lines.reset_index(drop=True), {"items": len(lines), "units": int(lines["Quantity"].sum()),
                                          "subtotal": subtotal, "tax_rate": tax_rate, "tax": tax,
                                          "total": round(subtotal + tax, 2)}


def _details(text: str | None) -> str:
    return "".join(f"<br><small>{escape(t)}</small>" for t in (text or "").split("\n") if t.strip())


def invoice_html(lines: pd.DataFrame, totals: dict, info: dict) -> str:
    """Printable invoice; open it in a browser and print or 'Save as PDF'."""
    rows = "".join(
        f"<tr><td>{i}</td><td>{escape(str(r['SKU']))}</td><td>{escape(str(r['Product']))}</td>"
        f"<td class=n>{r['Quantity']:,}</td><td class=n>{r['Unit cost (S$)']:,.2f}</td>"
        f"<td class=n>{r['Amount (S$)']:,.2f}</td></tr>"
        for i, r in enumerate(lines.to_dict("records"), start=1))
    notes = f"<div class=notes><strong>Notes</strong><br>{escape(info['notes'])}</div>" if info.get("notes") else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{escape(info['number'])}</title>
<style>
body{{font-family:Arial,sans-serif;color:#493a31;margin:40px auto;max-width:820px;padding:0 16px}}
.top{{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:3px solid #c64e35;padding-bottom:16px}}
h1{{margin:0;font-size:28px;letter-spacing:.04em;color:#c64e35}}
.meta{{text-align:right;font-size:13px;line-height:1.7}}
.parties{{display:flex;gap:48px;margin:22px 0;font-size:13px;line-height:1.6}}
.parties span{{display:block;font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:#866440}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#f3eee5;text-align:left;padding:8px;border-bottom:2px solid #e8dfd3}}
td{{padding:8px;border-bottom:1px solid #e8dfd3}} .n{{text-align:right}}
.totals{{margin-left:auto;width:300px;margin-top:16px;font-size:14px}}
.totals div{{display:flex;justify-content:space-between;padding:4px 0}}
.totals .grand{{border-top:2px solid #493a31;font-weight:700;font-size:16px;margin-top:4px;padding-top:8px}}
.notes{{margin-top:28px;font-size:13px;color:#77695f}}
.foot{{margin-top:40px;font-size:11px;color:#998b80;border-top:1px solid #e8dfd3;padding-top:10px}}
@media print{{body{{margin:0 auto}} th{{-webkit-print-color-adjust:exact;print-color-adjust:exact}}}}
</style></head><body>
<div class=top><div><h1>{escape(info['title'].upper())}</h1>
<div style="font-size:13px;margin-top:6px">{escape(info['shop'])}</div></div>
<div class=meta><strong>No.</strong> {escape(info['number'])}<br><strong>Date</strong> {info['date']:%d %b %Y}</div></div>
<div class=parties><div><span>From (buyer)</span>{escape(info['shop'])}{_details(info.get('shop_details'))}</div>
<div><span>To (supplier)</span>{escape(info['supplier'] or '—')}{_details(info.get('supplier_details'))}</div></div>
<table><thead><tr><th>#</th><th>SKU</th><th>Product</th><th class=n>Qty</th>
<th class=n>Unit cost (S$)</th><th class=n>Amount (S$)</th></tr></thead><tbody>{rows}</tbody></table>
<div class=totals><div><span>Subtotal</span><span>S${totals['subtotal']:,.2f}</span></div>
<div><span>Tax ({totals['tax_rate']:g}%)</span><span>S${totals['tax']:,.2f}</span></div>
<div class=grand><span>Total</span><span>S${totals['total']:,.2f}</span></div></div>
{notes}<div class=foot>{totals['items']} items · {totals['units']:,} units · generated by Stocker AI from the stocking list.
Quantities are suggestions; check them before sending.</div></body></html>"""


def invoice_xlsx(lines: pd.DataFrame, totals: dict, info: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws["A1"] = info["title"].upper()
    ws["A1"].font = Font(bold=True, size=18, color="C64E35")
    meta = [("No.", info["number"]), ("Date", info["date"].strftime("%d %b %Y")),
            ("From (buyer)", info["shop"]), ("To (supplier)", info["supplier"] or "—")]
    for i, (k, v) in enumerate(meta, start=3):
        ws.cell(i, 1, k).font = Font(bold=True)
        ws.cell(i, 2, v)
    head = 8
    headers = ["#", "SKU", "Product", "Qty", "Unit cost (S$)", "Amount (S$)"]
    for j, h in enumerate(headers, start=1):
        c = ws.cell(head, j, h)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="C64E35")
        c.alignment = Alignment(horizontal="center")
    for i, r in enumerate(lines.to_dict("records"), start=1):
        row = head + i
        ws.cell(row, 1, i)
        ws.cell(row, 2, r["SKU"])
        ws.cell(row, 3, r["Product"])
        ws.cell(row, 4, int(r["Quantity"]))
        ws.cell(row, 5, float(r["Unit cost (S$)"])).number_format = "#,##0.00"
        ws.cell(row, 6, f"=D{row}*E{row}").number_format = "#,##0.00"   # stays correct if edited in Excel
    last = head + len(lines)
    line = Side(style="thin", color="493A31")
    rows = [("Subtotal", f"=SUM(F{head + 1}:F{max(last, head + 1)})"),
            (f"Tax ({totals['tax_rate']:g}%)", f"=ROUND(F{last + 2}*{totals['tax_rate']}/100,2)"),
            ("Total", f"=F{last + 2}+F{last + 3}")]
    for k, (label, formula) in enumerate(rows, start=2):
        ws.cell(last + k, 5, label).font = Font(bold=True)
        c = ws.cell(last + k, 6, formula)
        c.number_format = "#,##0.00"
        if label == "Total":
            c.font = Font(bold=True)
            c.border = ws.cell(last + k, 5).border = Border(top=line)
    if info.get("notes"):
        ws.cell(last + 6, 1, "Notes").font = Font(bold=True)
        ws.cell(last + 6, 2, info["notes"])
    for col, w in zip("ABCDEF", [6, 14, 34, 10, 16, 16]):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
