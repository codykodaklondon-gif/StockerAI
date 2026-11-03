"""Import & export: sales import with column mapping, and the export page (tables, invoice, CSV, Excel)."""
from datetime import date

import streamlit as st

import ui
from stocker import agent, db, engine, exporter, importer, orders, sample, scan, suppliers
from views.common import cached_kpis, cached_recommendations, cached_risks, cached_summary, data_key, has_data, parse_upload, retrain, sample_file
from views.ordering import sup_labels


def page_import():
    st.header("Import data")
    file_tab, photo_tab = st.tabs([":material/table_view: Sales file (CSV / Excel)",
                                   ":material/photo_camera: Photo (receipt, invoice, list)"])
    with file_tab:
        file_import()
    with photo_tab:
        photo_import()

    st.divider()
    if has_data():   # once the analysis has run, its results can be exported
        st.button("Export the analysed lists (table, invoice, CSV, Excel) →", icon=":material/download:",
                  type="primary", on_click=ui.go_to, args=("📤 Export",))
    c1, c2 = st.columns(2)
    c1.download_button("Download a sample file (with customer data)",
                       sample_file(), "sample_sales.csv", "text/csv")
    with c2.popover("Reset my workspace"):
        st.write("Only affects your own workspace. Other visitors are not affected.")
        if st.button("Start empty (to import my own data)", type="primary"):
            db.reset()
            st.rerun()
        if st.button("Reload the demo shop"):
            sample.load_demo()
            engine.train_all()
            st.rerun()


def file_import():
    st.write("Upload historical sales from any POS export or spreadsheet (CSV or Excel). "
             "Transaction-level or daily totals both work; columns are detected automatically.")
    with st.expander("What should my file contain?", expanded=not has_data()):
        st.markdown("""
| Needed for | Columns |
|---|---|
| **Required** — forecasting, stocking list, risks | date, product (name or code), quantity |
| Better money figures | unit price or line total, unit cost, category |
| **Customer profile** | time of sale (or a date-time column), customer type (member / walk-in), receipt no., member ID |

- One row per sale line, or one row per product per day.
- Chinese headers such as 日期 / 商品名称 / 销量 / 会员类型 / 小票号 are recognised.
- Member IDs are hashed on import — no personal data is stored.
- Re-importing the same dates updates them instead of duplicating.
""")
    up = st.file_uploader("Sales file", type=["csv", "xlsx", "xls"])
    if up:
        try:
            df = parse_upload(up.getvalue(), up.name)
        except Exception as e:
            st.error(f"Could not read file: {e}")
            return
        st.caption(f"{len(df):,} rows · preview:")
        st.dataframe(df.head(8), width="stretch")
        detected = importer.detect_columns(df)
        st.subheader("Column mapping")
        opts = ["—"] + list(df.columns)
        labels = {"date": "Date *", "sku": "Product code", "name": "Product name *", "qty": "Quantity *",
                  "price": "Unit price", "revenue": "Line total", "category": "Category",
                  "stock": "Stock on hand", "cost": "Unit cost", "time": "Time / hour",
                  "customer_type": "Customer type (member / walk-in)", "txn": "Receipt / order no.",
                  "customer": "Customer or member ID (anonymised)"}
        # a form, so adjusting the mapping does not re-run the page until the import is submitted
        with st.form("mapping", border=False):
            cols = st.columns(3)
            mapping = {}
            for i, (f, lab) in enumerate(labels.items()):
                choice = cols[i % 3].selectbox(lab, opts, index=opts.index(detected[f]) if f in detected else 0,
                                               key=f"map_{f}")
                if choice != "—":
                    mapping[f] = choice
            submitted = st.form_submit_button("Import and train", type="primary")
        if submitted:
            try:
                res = importer.import_dataframe(df, mapping)
            except ValueError as e:
                st.error(str(e))
                return
            for w in res["warnings"]:
                st.warning(w)
            if res["detail_rows"]:
                st.info(f"Kept {res['detail_rows']:,} sale lines for the customer profile.")
            st.success(f"Imported {res['daily_rows']:,} product-days for {res['products']} products "
                       f"({res['new_products']} new), {res['date_from']} → {res['date_to']}.")
            retrain()
            with st.spinner("Preparing your stocking list and alerts…"):   # warm the caches for the next pages
                key = data_key()
                cached_recommendations(key), cached_risks(key), cached_kpis(key)
            s = cached_summary(data_key())
            if s.get("wape") is not None:
                weekly = (f" On weekly totals, which re-orders use: {s['week_wape']:.0%} vs {s['naive_week_wape']:.0%}."
                          if s.get("week_wape") is not None else "")
                st.info(f"Models trained. Back-test error {s['wape']:.0%} vs {s['naive_wape']:.0%} "
                        f"for a naive 28-day average.{weekly}")
            if s.get("ml_products"):
                st.success(f"Machine learning switched on for {s['ml_products']} products, where it was more accurate "
                           f"(recent error {s['recent_wape']:.0%} vs {s['recent_standard_wape']:.0%}).")
            st.caption("Tip: set stock levels, lead times and shelf life on the **Inventory** page for accurate risk alerts.")


def photo_import():
    st.write("Take a photo of a **receipt**, **supplier invoice**, **shopping list** or **product / price list** "
             "(PNG or JPEG). Stocker AI turns it into a table, suggests a category for each item and puts it in the "
             "right place. You check everything before it is saved.")
    if not agent.available():
        st.info("Reading photos needs an AI key. Add one under **Settings** in the sidebar "
                "(Google Gemini is free and reads photos well).", icon=":material/key:")
    ups = st.file_uploader("Photos", type=["png", "jpg", "jpeg"], accept_multiple_files=True, key="photo_files",
                           help="Several photos are read as pages of one document.")
    if ups:
        cols = st.columns(min(len(ups), 4))
        for i, u in enumerate(ups[:8]):
            cols[i % 4].image(u.getvalue(), caption=u.name, width="stretch")
        prints = [scan.fingerprint(u.getvalue()) for u in ups]
        if scan.already_saved(prints):
            st.warning("At least one of these photos was saved before. Saving it again would count it twice.")
        if st.button("Read with AI", icon=":material/document_scanner:", type="primary",
                     disabled=not agent.available()):
            try:
                with st.spinner("Reading the photo…"):
                    result = scan.read([u.getvalue() for u in ups])
            except (agent.AIError, ValueError) as e:
                st.error(str(e))
            except Exception as e:   # provider errors: show them, never invent a table
                st.error(f"The AI could not read the photo. {agent.explain(e)}")
            else:
                result["photos"], result["id"] = prints, "-".join(prints)[:40]
                st.session_state.scan = result
    res = st.session_state.get("scan")
    if not res:
        return
    if res["items"].empty:
        st.warning("No items were found on the photo. Try a sharper, well-lit photo taken straight on.")
        return
    st.subheader("Check what was read")
    kinds = list(scan.DOC_TYPES)
    k1, k2, k3 = st.columns([2, 1, 1.4])
    kind = k1.selectbox("What is this?", kinds, index=kinds.index(res["document_type"]),
                        format_func=scan.DOC_TYPES.get, key=f"scan_kind_{res['id']}")
    when = k2.date_input("Date", res["date"] or date.today(), key=f"scan_date_{res['id']}",
                         help=None if res["date"] else "No readable date on the photo: check it.")
    party = k3.text_input("Supplier / shop", res["party"], key=f"scan_party_{res['id']}")
    st.caption("Saves to: " + scan.DESTINATION[kind])
    if res["notes"]:
        st.info(res["notes"], icon=":material/info:")

    prods = db.products()
    options = [scan.NEW] + [scan.label(s, n) for s, n in zip(prods["sku"], prods["name"])]
    cats = sorted(set(prods["category"].dropna().astype(str)) | set(scan.CATEGORY_HINTS) | {"General"}
                  | set(res["items"]["category"].dropna().astype(str)))
    shown = {"sales": ["qty", "unit_price"], "purchase": ["qty", "unit_cost"], "shopping": ["qty"],
             "catalogue": ["unit_price", "unit_cost", "stock"]}[kind]
    edited = st.data_editor(
        res["items"], num_rows="dynamic", hide_index=True, width="stretch", key=f"scan_table_{res['id']}",
        column_order=["include", "name", "product", "category", *shown, "read_as"],
        column_config={
            "include": st.column_config.CheckboxColumn("Save", default=True, width="small"),
            "name": st.column_config.TextColumn("Item on the photo", required=True),
            "product": st.column_config.SelectboxColumn("Product in Stocker AI", options=options, default=scan.NEW,
                                                        required=True, help="Matched automatically; change it if wrong."),
            "category": st.column_config.SelectboxColumn("Category", options=cats, default="General", required=True,
                                                         help="Suggested by the AI; existing products keep theirs."),
            "qty": st.column_config.NumberColumn("Quantity", min_value=0, format="%g"),
            "unit_price": st.column_config.NumberColumn("Selling price (S$)", min_value=0, format="%.2f"),
            "unit_cost": st.column_config.NumberColumn("Unit cost (S$)", min_value=0, format="%.2f"),
            "stock": st.column_config.NumberColumn("Stock count", min_value=0, format="%g"),
            "read_as": st.column_config.TextColumn("Check", disabled=True, help="Lines the AI was unsure about are unticked."),
        })
    ticked = edited["include"].fillna(False).astype(bool)
    new = (ticked & (edited["product"] == scan.NEW)).sum()
    st.caption(f"{int(ticked.sum())} of {len(edited)} lines ticked · {int(new)} new product{'s' if new != 1 else ''} "
               "will be created. Unclear lines are unticked: check them against the photo first.")
    if st.button("Save to Stocker AI", icon=":material/save:", type="primary", key=f"scan_save_{res['id']}"):
        draft = orders.load(cached_recommendations(data_key()), prods)[0] if kind == "shopping" else None
        try:
            done = scan.save(kind, edited, when, party, res["photos"], draft)
        except ValueError as e:
            st.error(str(e))
            return
        del st.session_state["scan"]
        msg = {"sales": f"Added {done.get('units', 0):g} units sold on {when:%d %b %Y} to the sales history.",
               "purchase": f"Received {done.get('units', 0):g} units into stock; updated {done.get('costs_updated', 0)} unit costs.",
               "shopping": f"Added {done['lines']} lines to the Final order list.",
               "catalogue": f"Saved {done['lines']} products" + (f", with {done['stock_counts']} stock counts" if done.get("stock_counts") else "") + "."}[kind]
        st.success(msg + (f" Created {done['new_products']} new products." if done["new_products"] else ""))
        if kind == "sales":
            retrain()
        if kind == "shopping":
            st.button("Open the Final order list →", on_click=ui.go_to, args=("🧾 Order list",))


def page_export():
    ui.heading("Export", "Take your results with you.",
               "Download your final order list and the analysed lists as a table, a purchase invoice, CSV or Excel.")
    key = data_key()
    rec, risks, sups = cached_recommendations(key), cached_risks(key), suppliers.load()
    draft, saved, _ = orders.load(rec, db.products())
    final = orders.ordered(draft)
    summary = orders.by_supplier(draft, sups)
    lists = {"Final order list": exporter.final_table(final, sups),
             "Suppliers to contact": exporter.contact_table(summary, sups),
             "Purchase list": exporter.purchase_table(rec), "Full stocking list": exporter.stocking_table(rec),
             "Risk alerts": exporter.risk_table(risks)}
    s = cached_summary(data_key())
    c, b = st.columns([3, 1], vertical_alignment="center")
    c.caption(f"Analysis of {s.get('trained', 0)} products · models trained {s.get('trained_at') or '—'}. "
              + ("Final order list: your saved version." if saved else
                 "Final order list: not adjusted yet, so it shows Stocker AI's suggestions."))
    b.button("Adjust the final list", icon=":material/edit:", width="stretch", on_click=ui.go_to, args=("🧾 Order list",))
    choice = st.segmented_control("List to export", list(lists), default="Final order list", key="export_list") \
        or "Final order list"
    df = lists[choice]
    stem = f"stocker_{choice.lower().replace(' ', '_')}_{date.today():%Y-%m-%d}"
    table_tab, invoice_tab, csv_tab, xlsx_tab = st.tabs(
        [":material/table: Table", ":material/receipt_long: Invoice", ":material/csv: CSV", ":material/grid_on: Excel (XLSX)"])

    with table_tab:
        if df.empty:
            st.info(f"The {choice.lower()} is empty right now.")
        else:
            st.dataframe(df, hide_index=True, width="stretch")
            st.download_button("Download table (printable HTML)", exporter.table_html(df, f"Stocker AI · {choice}"),
                               f"{stem}.html", "text/html", icon=":material/print:", width="stretch")
            st.caption("Open the file in a browser and print it, or choose “Save as PDF”.")

    with csv_tab:
        st.write(f"**{choice}** · {len(df)} rows. Opens in Excel, Google Sheets or Numbers.")
        st.download_button("Download CSV", exporter.to_csv(df), f"{stem}.csv", "text/csv",
                           icon=":material/download:", width="stretch", disabled=df.empty)

    with xlsx_tab:
        all_sheets = st.toggle("Include all lists as separate sheets", value=True)
        sheets = lists if all_sheets else {choice: df}
        st.write(" · ".join(f"**{n}** ({len(d)} rows)" for n, d in sheets.items()))
        st.download_button("Download Excel", exporter.to_xlsx(sheets),
                           f"stocker_export_{date.today():%Y-%m-%d}.xlsx" if all_sheets else f"{stem}.xlsx",
                           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           icon=":material/download:", width="stretch")

    with invoice_tab:
        st.caption("Built from the final order list. Change quantities or suppliers on the **Final order list** page.")
        if final.empty:
            st.info("The final order list is empty, so there is nothing to put on an invoice.")
            return
        names = sup_labels(sups)
        groups = ["ALL"] + [g for g in summary["supplier_id"] if g]
        sid = st.selectbox("Invoice for", groups, format_func=lambda g: "All lines (one invoice)" if g == "ALL"
                           else names.get(g, g), key="inv_for")
        lines = final if sid == "ALL" else final[final["supplier_id"] == sid]
        store, sup = suppliers.store(), suppliers.get(sid) if sid != "ALL" else None
        info_l, info_r = st.columns(2)
        shop = info_l.text_input("Your shop (buyer)", store["name"] or "My shop", key="inv_shop")
        supplier = info_r.text_input("Supplier", sup["company_name"] if sup else "", placeholder="Supplier name",
                                     key=f"inv_supplier_{sid}")
        number = info_l.text_input("Invoice no.", orders.po_number(sid) if sup else
                                   f"PO-{date.today():%Y%m%d}-{st.session_state.ws[:4].upper()}", key=f"inv_number_{sid}")
        when = info_r.date_input("Date", date.today(), key="inv_date")
        title = info_l.text_input("Document title", "Purchase order", key="inv_title")
        tax = info_r.number_input("Tax (%)", 0.0, 100.0, 9.0, 0.5, key="inv_tax",
                                  help="Singapore GST is 9%. Set to 0 to leave tax out.")
        notes = st.text_area("Notes (optional)", store.get("delivery_notes", ""), key="inv_notes",
                             placeholder="e.g. Deliver before Friday 9am")
        inv_lines, totals = exporter.invoice_totals(exporter.invoice_lines(lines), tax)
        m = st.columns(3)
        m[0].metric("Items", totals["items"])
        m[1].metric("Subtotal", f"S${totals['subtotal']:,.2f}")
        m[2].metric("Total incl. tax", f"S${totals['total']:,.2f}")
        if totals["subtotal"] == 0:
            st.caption("Unit costs are 0. Add them on the Final order list or the Inventory page to get amounts.")
        info = {"shop": shop, "supplier": supplier, "number": number, "date": when, "title": title or "Invoice",
                "notes": notes, "shop_details": orders.store_block(store),
                "supplier_details": orders.supplier_block(sup) if sup else ""}
        html = exporter.invoice_html(inv_lines, totals, info)
        with st.expander("Preview invoice", expanded=True):
            if hasattr(st, "iframe"):   # every value in the invoice is HTML-escaped
                st.iframe(html, height=640)
            else:   # older Streamlit
                import streamlit.components.v1 as components
                components.html(html, height=640, scrolling=True)
        d1, d2 = st.columns(2)
        d1.download_button("Download invoice (HTML · print / PDF)", html, f"{number}.html", "text/html",
                           icon=":material/receipt_long:", width="stretch", type="primary")
        d2.download_button("Download invoice (Excel)", exporter.invoice_xlsx(inv_lines, totals, info),
                           f"{number}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           icon=":material/grid_on:", width="stretch")
