"""The final order list, and the messages that send it to suppliers.

The list starts from the stocking advice (products with a suggested purchase, at the middle of
the range) and the owner adjusts it: quantities, extra or removed products, and a supplier per
line. Messages are plain templates filled from the store details and the list; nothing is
estimated here and nothing is sent without the owner pressing a button.
"""
from datetime import date
from urllib.parse import quote

import pandas as pd

from . import db, engine, suppliers

DRAFT_KEY = "order_draft"
COLUMNS = ["order", "sku", "product", "suggested", "qty", "unit_cost", "supplier_id", "note"]
KINDS = {"order": "Purchase order", "enquiry": "Price & terms enquiry"}
CHANNELS = ["Email", "SMS", "WhatsApp"]


def _based_on() -> str:
    return str(engine.training_summary().get("trained_at") or "")


def suggested(rec: pd.DataFrame, prods: pd.DataFrame) -> pd.DataFrame:
    """Starting list: every product with a suggested purchase, its preferred supplier and unit cost."""
    if rec.empty:
        return pd.DataFrame(columns=COLUMNS)
    buy = rec[rec["order_high"] > 0]
    cost = prods.set_index("sku")["unit_cost"]
    assigned = suppliers.assignments()
    return pd.DataFrame({
        "order": True, "sku": buy["sku"].values, "product": buy["product"].values,
        "suggested": [f"{lo:,}–{hi:,}" for lo, hi in zip(buy["order_low"], buy["order_high"])],
        "qty": ((buy["order_low"] + buy["order_high"]) / 2).round().astype(int).values,
        "unit_cost": buy["sku"].map(cost).fillna(0).round(2).values,
        "supplier_id": [assigned.get(s) for s in buy["sku"]], "note": "",
    })


def load(rec: pd.DataFrame, prods: pd.DataFrame) -> tuple[pd.DataFrame, bool, bool]:
    """(list, saved?, stale?). Stale = the analysis was re-run after a list built from the advice was saved;
    a list taken from an approved purchase is the owner's decision, so it never goes stale."""
    draft = db.get_setting(DRAFT_KEY)
    if not draft:
        return suggested(rec, prods), False, False
    df = pd.DataFrame(draft["lines"], columns=COLUMNS)
    return df, True, not draft.get("source") and draft.get("based_on") != _based_on()


def source() -> dict | None:
    """Where the saved list came from: {"draft_id", "order_id", "approved_by"} for an approved purchase, else None."""
    return (db.get_setting(DRAFT_KEY) or {}).get("source")


def from_approved(draft_id: int) -> pd.DataFrame:
    """Step 2 → step 3: the approved official purchase list becomes the final order list (replacing the current one).
    Refused unless the draft is approved and unchanged since (review.official_order checks that)."""
    from . import review
    po = review.official_order(draft_id)
    cost = db.products().set_index("sku")["unit_cost"]
    assigned = suppliers.assignments()
    lines = pd.DataFrame([{
        "order": True, "sku": i["sku"], "product": i["product"], "suggested": f"approved #{po['id']}",
        "qty": int(i["quantity"]), "unit_cost": i["unit_cost"] if i.get("unit_cost") is not None else cost.get(i["sku"], 0),
        "supplier_id": assigned.get(i["sku"]), "note": ""} for i in po["items"]], columns=COLUMNS)
    save(lines, remember_suppliers=False,
         origin={"draft_id": int(draft_id), "order_id": int(po["id"]), "approved_by": po["approved_by"]})
    return lines


def save(df: pd.DataFrame, remember_suppliers: bool = True, origin: dict | None = None):
    """Save the final list. It keeps saying where it came from (`origin`) until the owner resets it."""
    origin = origin if origin is not None else source()
    df = df[df["sku"].notna()].copy()
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int)
    df["unit_cost"] = pd.to_numeric(df["unit_cost"], errors="coerce").fillna(0).round(2)
    df["order"] = df["order"].fillna(True).astype(bool)
    df = df.astype(object).where(df.notna(), None)
    db.set_setting(DRAFT_KEY, {"based_on": _based_on(), "lines": df[COLUMNS].to_dict("records"), "source": origin})
    if remember_suppliers:   # next time this product is suggested, it starts with the same supplier
        suppliers.assign({r["sku"]: r["supplier_id"] for r in df.to_dict("records") if r["supplier_id"]})


def reset():
    with db.connect() as con:
        con.execute("DELETE FROM settings WHERE key=?", (DRAFT_KEY,))


def ordered(df: pd.DataFrame) -> pd.DataFrame:
    """Lines that are ticked and have a quantity, with the line amount."""
    out = df[df["order"].fillna(False).astype(bool) & (pd.to_numeric(df["qty"], errors="coerce").fillna(0) > 0)].copy()
    out["qty"] = pd.to_numeric(out["qty"]).astype(int)
    out["unit_cost"] = pd.to_numeric(out["unit_cost"], errors="coerce").fillna(0)
    out["amount"] = (out["qty"] * out["unit_cost"]).round(2)
    out["note"] = out["note"].fillna("").astype(str)
    return out


def by_supplier(df: pd.DataFrame, sups: pd.DataFrame) -> pd.DataFrame:
    """One row per supplier on the list (lines without a supplier grouped as '—')."""
    o = ordered(df)
    if o.empty:
        return pd.DataFrame(columns=["supplier_id", "Supplier", "Items", "Units", "Est. cost (S$)", "Contact", "Status"])
    o["supplier_id"] = o["supplier_id"].fillna("")
    g = o.groupby("supplier_id").agg(Items=("sku", "count"), Units=("qty", "sum"), cost=("amount", "sum")).reset_index()
    info = sups.set_index("supplier_id") if not sups.empty else pd.DataFrame()

    def contact(sid):
        if sid not in info.index:
            return ""
        r = info.loc[sid]
        ways = (["email"] if r["emails"] else []) + (["SMS/WhatsApp"] if suppliers.mobiles(r.to_dict()) else []) \
            + (["phone"] if r["phone_numbers"] else [])
        return ", ".join(dict.fromkeys(ways)) or "no contact details"
    g["Supplier"] = [info.loc[s, "company_name"] if s in info.index else "— no supplier chosen" for s in g["supplier_id"]]
    g["Contact"] = [contact(s) for s in g["supplier_id"]]
    g["Status"] = [info.loc[s, "contact_status"] if s in info.index else "" for s in g["supplier_id"]]
    return g.rename(columns={"cost": "Est. cost (S$)"}).round({"Est. cost (S$)": 2})[
        ["supplier_id", "Supplier", "Items", "Units", "Est. cost (S$)", "Contact", "Status"]]


# ------------------------------------------------------------------ messages

def po_number(supplier_id: str, when: date | None = None) -> str:
    return f"PO-{(when or date.today()):%Y%m%d}-{supplier_id.split('-')[-1]}"


def _signature(store: dict) -> str:
    contact = " · ".join(v for v in (store.get("phone"), store.get("email")) if v)
    return "\n".join(v for v in (store.get("contact_person"), store.get("name"), contact) if v)


def _address(store: dict) -> str:
    return ", ".join(v for v in (store.get("address"), f"Singapore {store['postal_code']}" if store.get("postal_code") else "") if v)


def compose(kind: str, channel: str, sup: dict, lines: pd.DataFrame, store: dict, *,
            deliver_by: date | None = None, include_prices: bool = False) -> tuple[str, str]:
    """(subject, body) for a supplier. `lines` are this supplier's ordered lines (may be empty for an enquiry)."""
    name = sup["company_name"].title() if sup["company_name"].isupper() else sup["company_name"]   # "BAN SENG" -> "Ban Seng"
    company = sup.get("contact_person") or f"{name} team"
    me, shop = store.get("contact_person"), store.get("name")
    who = f"{me} from {shop}" if me and shop else (me or (f"the owner of {shop}" if shop else "a shop owner"))
    when = f"{deliver_by:%A %d %b %Y}" if deliver_by else "as soon as possible"
    items = [f"{i}. {r.product} — {r.qty:,} units" + (f" (target S${r.unit_cost:,.2f}/unit)" if include_prices and r.unit_cost else "")
             + (f" — {r.note}" if r.note else "") for i, r in enumerate(lines.itertuples(), start=1)]
    short = ", ".join(f"{r.product} x{r.qty:,}" for r in lines.itertuples())

    if channel != "Email":       # SMS / WhatsApp: short, no subject
        if kind == "order":
            if len(short) > 220:
                short = ", ".join(f"{r.product} x{r.qty:,}" for r in lines.head(4).itertuples()) + f" +{len(lines) - 4} more"
            body = (f"Hi {company}, this is {who}. We'd like to order: {short}. "
                    f"Delivery {when} to {_address(store) or 'our shop'}. "
                    "Could you confirm prices and availability? Thank you!")
        else:
            body = (f"Hi {company}, this is {who} in Singapore. Do you supply small retail shops? "
                    "Could you share your price list, minimum order and delivery days? Thank you!")
        contact = store.get("email") or store.get("phone")
        return "", body + (f"\n{contact}" if contact and channel == "WhatsApp" else "")

    where = f" at {_address(store)}" if _address(store) else ""
    intro = f"Dear {company},\n\n" + (f"My name is {me} and I run {shop}{where}." if me and shop else
                                       f"I am writing from {shop}{where}." if shop else
                                       f"My name is {me} and I run a shop{where}." if me else
                                       f"I am writing on behalf of our shop{where}.")
    terms = ("Could you please confirm availability, unit prices and the total including delivery? "
             "Please also let us know your minimum order, delivery days and payment terms")
    if store.get("payment_terms"):
        terms += f" (we usually work with {store['payment_terms']})"
    terms += "."
    if sup.get("minimum_order"):
        terms += f"\nWe noted your minimum order: {sup['minimum_order']}."
    if kind == "order":
        number = po_number(sup["supplier_id"])
        subject = f"Purchase order {number}" + (f" — {shop}" if shop else "")
        parts = [intro, "We would like to place the following order:", "\n".join(items) or "(no items)",
                 f"Requested delivery: {when}" + (f"\nDelivery address: {_address(store)}" if _address(store) else "")
                 + (f"\nDelivery notes: {store['delivery_notes']}" if store.get("delivery_notes") else ""),
                 terms, f"Our reference: {number}" + (f" · UEN {store['uen']}" if store.get("uen") else "")]
    else:
        subject = f"Supply enquiry from {shop}" if shop else "Supply enquiry"
        need = ("\nWe are mainly looking for:\n" + "\n".join(f"- {r.product}" + (f" (about {r.qty:,} units per order)" if r.qty else "")
                                                         for r in lines.itertuples())) if not lines.empty else ""
        parts = [intro, "We are looking for reliable suppliers and would like to know whether you supply small retail shops."
                 + need,
                 "Could you please send us your current price list and let us know your minimum order, "
                 "delivery areas and days, and payment terms?"]
    return subject, "\n\n".join(parts + ["Thank you, and we look forward to hearing from you.", "Best regards,\n" + _signature(store)])


def mailto(to: list[str], subject: str, body: str) -> str:
    return f"mailto:{','.join(to)}?subject={quote(subject)}&body={quote(body)}"


def sms_link(number: str, body: str) -> str:
    # '?&body=' is understood by both iPhone/Mac Messages and Android
    return f"sms:{number.replace(' ', '')}?&body={quote(body)}"


def whatsapp_link(number: str, body: str) -> str:
    digits = "".join(c for c in number if c.isdigit())
    return f"https://wa.me/{digits if digits.startswith('65') or len(digits) > 8 else '65' + digits}?text={quote(body)}"


def store_block(store: dict) -> str:
    """Store lines printed under the buyer on a purchase order."""
    return "\n".join(v for v in (store.get("contact_person"), _address(store), store.get("phone"), store.get("email"),
                                 store.get("uen") and f"UEN {store['uen']}") if v)


def supplier_block(sup: dict) -> str:
    addr = ", ".join(v for v in (sup.get("address"), sup.get("postal_code") and f"Singapore {sup['postal_code']}") if v)
    return "\n".join(v for v in (sup.get("contact_person"), addr, (suppliers.split(sup.get("emails")) or [""])[0],
                                 (suppliers.split(sup.get("phone_numbers")) or [""])[0]) if v)
