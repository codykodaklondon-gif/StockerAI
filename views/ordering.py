"""Plan & order, steps 3–4: the final order list and the supplier directory / contact tools."""
from datetime import date
from html import escape

import pandas as pd
import streamlit as st

import ui
from stocker import db, exporter, notifier, orders, review, suppliers
from views.common import cached_recommendations, data_key


NONE = "— none —"


def sup_labels(sups: pd.DataFrame) -> dict:
    return {r.supplier_id: f"{r.company_name} ({r.supplier_id})" for r in sups.itertuples()}


def sku_of(label) -> str | None:
    """'White Bread (BRD-01)' -> 'BRD-01'."""
    return str(label).rsplit("(", 1)[1].rstrip(")") if isinstance(label, str) and "(" in label else None


def page_order_list():
    ui.heading("Final order list", "Adjust the list, then order.",
               "Start from Stocker AI's suggestions: change quantities, untick or add products, "
               "and choose a supplier for each line.")
    key = data_key()
    rec, prods, sups = cached_recommendations(key), db.products(), suppliers.load()
    draft, saved, stale = orders.load(rec, prods)
    origin = orders.source()
    approved = review.get()
    if approved and approved["status"] == "approved" and (origin or {}).get("draft_id") != approved["id"]:
        try:
            po = review.official_order(approved["id"])
            w, b = st.columns([3, 1], vertical_alignment="center")
            w.info(f"Purchase #{po['id']} was approved by {po['approved_by']} in **2 · Review & approve** "
                   f"({len(po['items'])} items). Use it as this list?", icon=":material/verified:")
            if b.button("Use approved list", type="primary", width="stretch"):
                orders.from_approved(approved["id"])
                st.session_state.pop("order_editor", None)
                st.rerun()
        except review.ReviewError:
            pass
    if origin:
        st.caption(f":material/link: This list comes from purchase #{origin['order_id']}, approved by "
                   f"{origin['approved_by']} in **2 · Review & approve**. Your edits here are kept.")
    if stale:
        w, b = st.columns([3, 1], vertical_alignment="center")
        w.warning("The analysis was updated after you saved this list. Your edits are kept below.")
        if b.button("Use the new suggestions", width="stretch"):
            orders.reset()
            st.session_state.pop("order_editor", None)
            st.rerun()
    elif saved:
        st.caption("✓ This is your saved final list.")
    else:
        st.caption("These are Stocker AI's suggestions, at the middle of each suggested range. "
                   "Nothing is saved until you press **Save final list**.")
    if sups.empty:
        st.info("No suppliers yet. Load or add them on the **Suppliers** page to choose one per line.")

    prod_label = {r.sku: f"{r.name} ({r.sku})" for r in prods.itertuples()}
    sup_label = sup_labels(sups)
    view = pd.DataFrame({
        "Order": draft["order"].fillna(True).astype(bool).values,
        "Product": [prod_label.get(s, f"{p} ({s})") for s, p in zip(draft["sku"], draft["product"])],
        "Suggested": draft["suggested"].fillna("").values,
        "Quantity": pd.to_numeric(draft["qty"], errors="coerce").fillna(0).astype(int).values,
        "Unit cost (S$)": pd.to_numeric(draft["unit_cost"], errors="coerce").fillna(0).values,
        "Supplier": [sup_label.get(s, NONE) for s in draft["supplier_id"]],
        "Note": draft["note"].fillna("").values,
    })
    edited = st.data_editor(
        view, num_rows="dynamic", hide_index=True, width="stretch", key="order_editor",
        column_config={
            "Order": st.column_config.CheckboxColumn("Order", help="Untick to keep the line but leave it out of the order.",
                                                     default=True, width="small"),
            "Product": st.column_config.SelectboxColumn("Product", options=list(prod_label.values()), required=True, width="medium"),
            "Suggested": st.column_config.TextColumn("Suggested", disabled=True, help="Stocker AI's suggested range."),
            "Quantity": st.column_config.NumberColumn("Quantity", min_value=0, step=1, default=0),
            "Unit cost (S$)": st.column_config.NumberColumn("Unit cost (S$)", min_value=0.0, format="%.2f"),
            "Supplier": st.column_config.SelectboxColumn("Supplier", options=[NONE] + sorted(sup_label.values()), width="large"),
            "Note": st.column_config.TextColumn("Note to supplier", help="e.g. pack size, brand, cut-off time"),
        })
    names, costs = prods.set_index("sku")["name"], prods.set_index("sku")["unit_cost"]
    sku = edited["Product"].map(sku_of)
    new = pd.DataFrame({
        "order": edited["Order"].fillna(True).astype(bool), "sku": sku,
        "product": [names.get(s, str(l).rsplit(" (", 1)[0]) for s, l in zip(sku, edited["Product"])],
        "suggested": edited["Suggested"].fillna(""),
        "qty": pd.to_numeric(edited["Quantity"], errors="coerce").fillna(0).astype(int),
        "unit_cost": pd.to_numeric(edited["Unit cost (S$)"], errors="coerce").fillna(sku.map(costs)).fillna(0),
        "supplier_id": edited["Supplier"].map({v: k for k, v in sup_label.items()}),
        "note": edited["Note"].fillna(""),
    })[lambda d: d["sku"].notna()]
    changes = st.session_state.get("order_editor", {})
    if any(changes.get(k) for k in ("edited_rows", "added_rows", "deleted_rows")):
        st.warning("You have unsaved changes.")

    b = st.columns(3)
    if b[0].button("Save final list", type="primary", icon=":material/save:", width="stretch"):
        orders.save(new)
        st.session_state.pop("order_editor", None)
        st.toast("Final order list saved.")
        st.rerun()
    if b[1].button("Fill empty suppliers with best match", icon=":material/auto_fix_high:", width="stretch",
                   disabled=sups.empty, help="Uses each product's saved supplier, otherwise the closest match by category."):
        have = suppliers.assignments()
        cats = prods.set_index("sku")["category"]
        for i in new.index[new["supplier_id"].isna()]:
            s = new.at[i, "sku"]
            best = have.get(s) or next(iter(suppliers.suggest(new.at[i, "product"], cats.get(s, ""), sups, 1)), (None,))[0]
            new.at[i, "supplier_id"] = best
        orders.save(new)
        st.session_state.pop("order_editor", None)
        st.rerun()
    if b[2].button("Reset to suggestions", icon=":material/restart_alt:", width="stretch", disabled=not saved):
        orders.reset()
        st.session_state.pop("order_editor", None)
        st.rerun()

    o = orders.ordered(new)
    m = st.columns(3)
    m[0].metric("Lines to order", len(o))
    m[1].metric("Units", f"{int(o['qty'].sum()):,}")
    m[2].metric("Estimated cost", f"S${o['amount'].sum():,.2f}" if o["amount"].sum() else "—")
    st.subheader("By supplier")
    summary = orders.by_supplier(new, sups)
    if summary.empty:
        st.info("Nothing to order yet.")
    else:
        st.dataframe(summary.drop(columns="supplier_id"), hide_index=True, width="stretch",
                     column_config={"Est. cost (S$)": st.column_config.NumberColumn(format="%.2f")})
        if (summary["supplier_id"] == "").any():
            st.caption("Some lines have no supplier. Choose one above, or press **Fill empty suppliers with best match**.")
    n1, n2 = st.columns(2)
    n1.button("Export the final list →", icon=":material/download:", width="stretch", on_click=ui.go_to, args=("📤 Export",))
    n2.button("Contact suppliers →", icon=":material/send:", type="primary", width="stretch",
              on_click=ui.go_to, args=("🤝 Suppliers",))


def supplier_card(sup: dict):
    """Everything known about one supplier, with clickable contact details."""
    st.markdown(f"#### {sup['company_name']}" + (f" · {sup['name_zh']}" if sup.get("name_zh") else ""))
    tone = {"Ordered": "sage", "Contacted": "amber", "Awaiting reply": "amber", "Not suitable": "red"}
    ui.badge(sup.get("contact_status") or "Not contacted", tone.get(sup.get("contact_status"), "neutral"))
    rows = [("Category", sup.get("category")), ("Products", sup.get("products")),
            ("Contact person", sup.get("contact_person")),
            ("Email", " · ".join(f"[{e}](mailto:{e})" for e in suppliers.split(sup.get("emails")))),
            ("Phone", " · ".join(f"[{p}](tel:{p.replace(' ', '')})" + (" 📱" if suppliers.is_mobile(p) else "")
                                 for p in suppliers.split(sup.get("phone_numbers")))),
            ("WhatsApp", " · ".join(suppliers.split(sup.get("whatsapp_numbers")))),
            ("Address", ", ".join(v for v in (sup.get("address"), sup.get("postal_code") and f"Singapore {sup['postal_code']}") if v)),
            ("Website", sup.get("website")), ("Minimum order", sup.get("minimum_order")),
            ("Delivery", sup.get("delivery_details")), ("Supplies retail?", sup.get("retail_supply_evidence")),
            ("Last contacted", sup.get("last_contacted")), ("Notes", sup.get("notes")),
            ("Source", " · ".join(v for v in (sup.get("source_type"), sup.get("retrieved_on") and f"retrieved {sup['retrieved_on']}") if v))]
    st.markdown("\n".join(f"- **{k}:** {v}" for k, v in rows if v))
    if not suppliers.mobiles(sup) and sup.get("phone_numbers"):
        st.caption("📞 Landline only: call, or use email. SMS and WhatsApp need a mobile number (+65 8/9…).")


def supplier_contact(sups: pd.DataFrame):
    if sups.empty:
        st.info("Load or add suppliers first (see the **Directory** tab).")
        return
    draft, _, _ = orders.load(cached_recommendations(data_key()), db.products())
    final = orders.ordered(draft)
    on_list = [s for s in dict.fromkeys(final["supplier_id"].dropna()) if s in set(sups["supplier_id"])]
    count = final.groupby("supplier_id")["sku"].count().to_dict() if not final.empty else {}
    names = {r.supplier_id: f"{r.company_name} · {r.category or ''}" for r in sups.itertuples()}
    options = on_list + [s for s in sups["supplier_id"] if s not in count]   # suppliers on the order list first
    if not on_list:
        st.caption("No suppliers on your final order list yet: choose them on the **Final order list** page, "
                   "or pick any supplier below to send an enquiry.")
    if st.session_state.get("contact_sid") not in options:   # e.g. a supplier that was just deleted
        st.session_state.pop("contact_sid", None)
    sid = st.selectbox("Supplier", options, key="contact_sid",
                       format_func=lambda s: (f"★ {names[s]} — {count[s]} item{'s' if count[s] > 1 else ''} on your list"
                                              if s in count else names[s]))
    sup, lines = suppliers.get(sid), final[final["supplier_id"] == sid]
    left, right = st.columns([1, 1.35], gap="large")
    with left:
        with st.container(border=True):
            supplier_card(sup)
        if not lines.empty:
            st.markdown("**On your order list**")
            st.dataframe(lines[["product", "qty", "unit_cost", "amount"]].rename(columns={
                "product": "Product", "qty": "Qty", "unit_cost": "Unit cost (S$)", "amount": "Amount (S$)"}),
                hide_index=True, width="stretch")
        current = sup.get("contact_status") or "Not contacted"
        skey = f"status_{sid}_{current}"     # keyed by the stored status, so other buttons' changes show up
        st.selectbox("Status", suppliers.STATUSES, key=skey,
                     index=suppliers.STATUSES.index(current) if current in suppliers.STATUSES else 0,
                     on_change=lambda: suppliers.set_status(sid, st.session_state[skey]))
        hist = suppliers.history(sid)
        if not hist.empty:
            with st.expander(f"Contact history ({len(hist)})"):
                st.dataframe(hist, hide_index=True, width="stretch")

    with right:
        emails, mobiles = suppliers.split(sup.get("emails")), suppliers.mobiles(sup)
        kind = st.segmented_control("Message", list(orders.KINDS), format_func=orders.KINDS.get, key=f"kind_{sid}",
                                    default="order" if not lines.empty else "enquiry") or "enquiry"
        channel = st.segmented_control("Send by", orders.CHANNELS, key=f"channel_{sid}",
                                       default="Email" if emails else ("SMS" if mobiles else "Email")) or "Email"
        if channel == "Email" and not emails:
            st.warning("No email address for this supplier. Add one in the Directory tab, or call them.")
        if channel != "Email" and not mobiles:
            st.warning("No mobile number for this supplier, so SMS / WhatsApp will not reach them.")
        o1, o2 = st.columns(2)
        deliver_by = o1.date_input("Deliver by", date.today() + pd.Timedelta(days=2), key=f"deliver_{sid}",
                                   disabled=kind != "order")
        prices = o2.checkbox("Include our target prices", False, key=f"prices_{sid}",
                             disabled=kind != "order" or channel != "Email",
                             help="Adds the unit cost from your list next to each item.")
        store = suppliers.store()
        missing = suppliers.store_missing(store)
        sk, bk = f"msg_subject_{sid}_{channel}", f"msg_body_{sid}_{channel}"
        if st.button("✨ Fill in my store details & write the message", type="primary", width="stretch"):
            st.session_state[sk], st.session_state[bk] = orders.compose(
                kind, channel, sup, lines, store,
                deliver_by=deliver_by if kind == "order" else None, include_prices=prices)
        if missing:
            st.warning("Your store details are incomplete (" + ", ".join(missing) + "). "
                       "Fill them in under **My store details** so messages are signed properly.")
        subject = st.text_input("Subject", key=sk) if channel == "Email" else ""
        body = st.text_area("Message", key=bk, height=330 if channel == "Email" else 160,
                            placeholder="Press the button above to write the message, then edit it as you like.")
        if channel == "SMS" and body:
            st.caption(f"{len(body)} characters · about {-(-len(body) // 153)} SMS")
        if not body:
            return
        if channel == "Email" and emails:
            to = st.multiselect("To", emails, default=emails[:1], key=f"to_{sid}")
            a, b = st.columns(2)
            a.link_button("Open in my email app", orders.mailto(to, subject, body), icon=":material/mail:",
                          width="stretch", disabled=not to)
            smtp = notifier.config()["email"]
            can_send = bool(smtp["host"]) and bool(to)
            if b.button("Send now from Stocker AI", icon=":material/send:", width="stretch", disabled=not can_send,
                        help="Uses the email account set up on the Alerts page." if smtp["host"] else
                        "Set up an email account on the Alerts page to send from here."):
                try:
                    notifier.send_email({**smtp, "to": ", ".join(to)}, subject, escape(body))
                except Exception as e:
                    st.error(f"Could not send: {type(e).__name__}: {e}")
                else:
                    suppliers.log_contact(sid, "Email (sent)", kind, subject, body)
                    st.success(f"Sent to {', '.join(to)} and marked as contacted.")
            if len(orders.mailto(to, subject, body)) > 1900:
                st.caption("Long message: some email apps cut very long links. If the text looks cut, copy it below.")
        elif channel != "Email" and mobiles:
            num = st.selectbox("Mobile number", mobiles, key=f"num_{sid}")
            link = orders.sms_link(num, body) if channel == "SMS" else orders.whatsapp_link(num, body)
            st.link_button(f"Open in {'Messages' if channel == 'SMS' else 'WhatsApp'}", link, width="stretch",
                           icon=":material/sms:" if channel == "SMS" else ":material/chat:")
            if channel == "SMS":
                st.caption("Opens your phone's or Mac's Messages app. On Windows, use WhatsApp or copy the text.")
        with st.expander("Copy the text"):
            st.code((f"Subject: {subject}\n\n" if subject else "") + body, language=None, wrap_lines=True)
        c1, c2 = st.columns(2)
        if c1.button("Mark as contacted", icon=":material/check_circle:", width="stretch"):
            suppliers.log_contact(sid, channel, orders.KINDS[kind], subject, body)
            st.toast("Marked as contacted and saved to the history.")
            st.rerun()
        if kind == "order" and not lines.empty:
            inv_lines, totals = exporter.invoice_totals(exporter.invoice_lines(lines), 0)
            info = {"shop": store["name"] or "My shop", "supplier": sup["company_name"], "number": orders.po_number(sid),
                    "date": date.today(), "title": "Purchase order", "notes": store.get("delivery_notes", ""),
                    "shop_details": orders.store_block(store), "supplier_details": orders.supplier_block(sup)}
            c2.download_button("Download PO to attach", exporter.invoice_html(inv_lines, totals, info),
                               f"{info['number']}.html", "text/html", icon=":material/attach_file:", width="stretch")


def supplier_directory(sups: pd.DataFrame):
    with st.expander("Import suppliers", icon=":material/upload_file:", expanded=sups.empty):
        st.caption("CSV or Excel with columns like company_name, category, phone_numbers, emails, address, products. "
                   "Existing suppliers (same supplier_id) are updated; contact status is never overwritten.")
        if suppliers.BUNDLED.exists():
            if st.button("Load the bundled Singapore supplier list", icon=":material/library_add:"):
                res = suppliers.load_bundled()
                st.success(f"Added {res['added']} suppliers, updated {res['updated']}.")
                st.rerun()
        up = st.file_uploader("Supplier file", type=["csv", "xlsx", "xls"], key="sup_upload")
        if up and st.button("Import this file", type="primary"):
            try:
                res = suppliers.import_file(up.getvalue(), up.name)
            except Exception as e:
                st.error(f"Could not import: {e}")
            else:
                n = suppliers.auto_assign()
                st.success(f"Added {res['added']} suppliers, updated {res['updated']}."
                           + (f" Matched {n} products to a supplier." if n else ""))
                st.rerun()
    if sups.empty:
        return
    f1, f2 = st.columns([2, 2])
    q = f1.text_input("Search", placeholder="Name, product, address, postal code…", key="sup_q")
    cats = f2.multiselect("Category", sorted(sups["category"].dropna().unique()), key="sup_cats")
    g = st.columns(4)
    has_email = g[0].checkbox("Has email", key="sup_email")
    has_mobile = g[1].checkbox("Has mobile", key="sup_mobile", help="A +65 8/9… number, so SMS and WhatsApp work.")
    retail = g[2].checkbox("Retail supply published", key="sup_retail",
                           help="The supplier's own page or directory says they supply shops / take small orders.")
    statuses = g[3].multiselect("Status", suppliers.STATUSES, key="sup_status", placeholder="Any status")
    view = sups
    if q:
        hay = view[["company_name", "products", "category", "address", "postal_code", "emails", "name_zh"]].fillna("").agg(" ".join, axis=1)
        view = view[hay.str.contains(q, case=False, regex=False)]
    if cats:
        view = view[view["category"].isin(cats)]
    if has_email:
        view = view[view["emails"].notna()]
    if has_mobile:
        view = view[[bool(suppliers.mobiles(r)) for r in view.to_dict("records")]]
    if retail:
        view = view[view["retail_supply_evidence"].notna() & (view["retail_supply_evidence"] != suppliers.CONFIRMED)]
    if statuses:
        view = view[view["contact_status"].isin(statuses)]
    st.caption(f"{len(view)} of {len(sups)} suppliers · click a row to see everything about it.")
    event = st.dataframe(
        view[["company_name", "category", "products", "phone_numbers", "emails", "postal_code",
              "retail_supply_evidence", "contact_status"]],
        hide_index=True, width="stretch", height=320, on_select="rerun", selection_mode="single-row", key="sup_table",
        column_config={"company_name": "Supplier", "category": "Category", "products": "Products",
                       "phone_numbers": "Phone", "emails": "Email", "postal_code": "Postal code",
                       "retail_supply_evidence": "Supplies retail?", "contact_status": "Status"})
    picked = event.selection.rows if event and event.selection else []
    a, b = st.columns(2)
    a.download_button("Download this list (Excel)", exporter.to_xlsx({"Suppliers": view}),
                      f"suppliers_{date.today():%Y-%m-%d}.xlsx", icon=":material/download:", width="stretch")
    if not picked:
        with st.expander("Add a supplier", icon=":material/add:"):
            supplier_form({}, "new")
        return
    sup = suppliers.get(view.iloc[picked[0]]["supplier_id"])
    left, right = st.columns([1.2, 1], gap="large")
    with left:
        with st.container(border=True):
            supplier_card(sup)
    with right:
        with st.expander("Edit this supplier", icon=":material/edit:"):
            supplier_form(sup, sup["supplier_id"])
        if st.button("Contact this supplier", type="primary", width="stretch",
                     on_click=lambda: st.session_state.update(contact_sid=sup["supplier_id"])):
            st.toast("Selected. Open the **Contact** tab to write the message.")
        with st.popover("Delete supplier", icon=":material/delete:"):
            st.write(f"Delete **{sup['company_name']}** from your directory?")
            if st.button("Delete", type="primary", key=f"del_{sup['supplier_id']}"):
                suppliers.delete(sup["supplier_id"])
                st.rerun()


def supplier_form(sup: dict, key: str):
    labels = {"company_name": "Company name *", "category": "Category", "contact_person": "Contact person",
              "phone_numbers": "Phone numbers (separate with ;)", "emails": "Emails (separate with ;)",
              "whatsapp_numbers": "WhatsApp numbers", "address": "Address", "postal_code": "Postal code",
              "products": "Products", "website": "Website", "minimum_order": "Minimum order",
              "delivery_details": "Delivery details", "notes": "Notes"}
    with st.form(f"sup_form_{key}", border=False):
        values = {}
        cols = st.columns(2)
        for i, (f, lab) in enumerate(labels.items()):
            values[f] = cols[i % 2].text_input(lab, sup.get(f) or "", key=f"sf_{key}_{f}")
        if st.form_submit_button("Save supplier", type="primary"):
            if not values["company_name"].strip():
                st.error("Company name is required.")
            else:
                suppliers.save({**values, "supplier_id": sup.get("supplier_id"),
                                "contact_status": sup.get("contact_status")})
                st.toast("Supplier saved.")
                st.rerun()


def supplier_assign(sups: pd.DataFrame):
    st.caption("Each product's usual supplier. New order lists start with these, and saving the final order "
               "list updates them.")
    if sups.empty:
        st.info("Load or add suppliers first (see the **Directory** tab).")
        return
    prods, have = db.products(), suppliers.assignments()
    sup_label = sup_labels(sups)
    names = sups.set_index("supplier_id")["company_name"]
    view = pd.DataFrame({
        "Product": [f"{r.name} ({r.sku})" for r in prods.itertuples()], "Category": prods["category"].values,
        "Supplier": [sup_label.get(have.get(s), NONE) for s in prods["sku"]],
        "Best matches": [" · ".join(names[i] for i, _ in suppliers.suggest(r.name, r.category, sups))
                         for r in prods.itertuples()]})
    edited = st.data_editor(view, hide_index=True, width="stretch", key="assign_editor", disabled=["Product", "Category", "Best matches"],
                            column_config={"Supplier": st.column_config.SelectboxColumn(
                                "Supplier", options=[NONE] + sorted(sup_label.values()), width="large"),
                                "Best matches": st.column_config.TextColumn(width="large")})
    a, b = st.columns(2)
    if a.button("Save", type="primary", width="stretch", key="assign_save"):
        back = {v: k for k, v in sup_label.items()}
        suppliers.assign({sku_of(p): back.get(s) for p, s in zip(edited["Product"], edited["Supplier"])})
        st.session_state.pop("assign_editor", None)
        st.toast("Saved.")
        st.rerun()
    if b.button("Fill empty ones with the best match", icon=":material/auto_fix_high:", width="stretch"):
        n = suppliers.auto_assign()
        st.session_state.pop("assign_editor", None)
        st.toast(f"Matched {n} products.")
        st.rerun()
    st.caption("Matches compare the product's name and category with each supplier's category and product list; "
               "suppliers that publish retail supply or have an email rank higher. Please check before ordering.")


def store_form():
    st.caption("Used to sign messages to suppliers and to fill in the buyer on purchase orders.")
    info = suppliers.store()
    with st.form("store_form"):
        cols = st.columns(2)
        values = {}
        for i, (f, lab) in enumerate(suppliers.STORE_FIELDS.items()):
            values[f] = cols[i % 2].text_input(lab, info.get(f, ""), key=f"store_{f}",
                                               placeholder={"payment_terms": "e.g. cash on delivery, 30 days",
                                                            "delivery_notes": "e.g. back door, before 9am",
                                                            "phone": "+65 9123 4567"}.get(f, ""))
        if st.form_submit_button("Save store details", type="primary"):
            suppliers.save_store(values)
            st.toast("Store details saved.")
            st.rerun()
    if missing := suppliers.store_missing(info):
        st.caption("Still needed for messages: " + ", ".join(missing))


def page_suppliers():
    ui.heading("Suppliers", "Contact the right suppliers.",
               "Supplier details, a message ready to send by email, SMS or WhatsApp, and which supplier "
               "you use for each product.")
    sups = suppliers.load()
    contact_tab, dir_tab, assign_tab, store_tab = st.tabs(
        [":material/send: Contact", f":material/contacts: Directory ({len(sups)})",
         ":material/link: Products → suppliers", ":material/storefront: My store details"])
    with contact_tab:
        supplier_contact(sups)
    with dir_tab:
        supplier_directory(sups)
    with assign_tab:
        supplier_assign(sups)
    with store_tab:
        store_form()
