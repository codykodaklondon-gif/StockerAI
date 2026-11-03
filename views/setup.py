"""More → Shop setup: stock & products, holidays & promotions, alerts to phone & email."""
from datetime import date
from datetime import time
import os

import pandas as pd
import streamlit as st

import ui
from stocker import db, events, notifier
from stocker.icons import icon
from stocker.lifespan import lifespan_group
from views import location_map
from views.common import SEV_ICON, cached_alert_preview, cached_all_models, cached_risks, data_key, retrain


PERISHABLE, NON_PERISHABLE = "Perishable", "Non-perishable"


def page_inventory():
    st.header("Inventory")
    st.caption("Record deliveries, sales and stock counts. Risks update instantly.")
    st.button("Add a delivery with an expiry date →", on_click=ui.go_to, args=("🥬 Expiry",))
    st.caption("Batch sales use the earliest valid expiry first. For batch waste or date corrections, open Expiry & batches.")
    db.fill_locations()   # new products get the next free place on their shelf
    prods = db.products()
    with st.form("move", clear_on_submit=True):
        c = st.columns([3, 2, 2, 3])
        icons = dict(zip(prods["name"] + " (" + prods["sku"] + ")", map(icon, prods["name"], prods["category"])))
        label = c[0].selectbox("Product", prods["name"] + " (" + prods["sku"] + ")", key="inventory_product_selection",
                               format_func=lambda o: f"{icons.get(o, '')} {o}")
        kind = c[1].selectbox("Type", ["in", "out", "adjust"],
                              format_func={"in": "Stock in", "out": "Sold / used", "adjust": "Stock count"}.get)
        qty = c[2].number_input("Quantity", min_value=0.0, step=1.0)
        note = c[3].text_input("Note")
        if st.form_submit_button("Record", type="primary") and qty >= 0:
            sku = label.rsplit("(", 1)[1].rstrip(")")
            try:
                db.record_move(sku, kind, qty, note)
            except ValueError as e:
                st.error(str(e))
                return
            st.toast(f"Recorded {kind} of {qty:g} for {sku}")
            new = cached_risks(data_key())
            hit = new[new["sku"] == sku] if not new.empty else new
            for r in hit.itertuples():
                st.toast(f"{SEV_ICON[r.severity]} {r.kind}: {r.message}")

    st.subheader("Products")
    st.caption("Lifespan group is calculated from Shelf life (days): Short 1–30 · Medium >30–180 · Long >180 · Unknown if missing or invalid. Save changes to refresh the group. This is total shelf life, not days remaining for a batch.")
    st.caption("Location is filled in automatically: Fridge = Shelf A, Dry goods = Shelf B, Household = Shelf C, numbered from 1 "
               "(frozen first, then the shortest shelf life). Type your own, e.g. A5, to move a product; clear the cell to let the app choose again.")
    table = prods.assign(icon=list(map(icon, prods["name"], prods["category"])), shelf=prods["location"].map(db.shelf_name),
                         type=db.perishable(prods).map({True: PERISHABLE, False: NON_PERISHABLE}),
                         lifespan_group=prods["shelf_life_days"].map(lifespan_group))
    counts = table["type"].value_counts()
    show = st.segmented_control("Show", ["All", PERISHABLE, NON_PERISHABLE], default="All", key="inv_type",
                                format_func=lambda t: t if t == "All" else f"{t} ({counts.get(t, 0)})")
    if show in (PERISHABLE, NON_PERISHABLE):
        table = table[table["type"] == show]
    edited = st.data_editor(
        table[["icon", "sku", "name", "category", "location", "shelf", "type", "lifespan_group", "current_stock", "unit_cost", "unit_price",
               "lead_time_days", "shelf_life_days"]],
        hide_index=True, width="stretch", disabled=["icon", "sku", "shelf", "lifespan_group"], key=f"inv_editor_{show}",
        column_config={"icon": st.column_config.TextColumn("", width=40, help="Picked from the product name or category."),
                       "location": st.column_config.TextColumn("Location", help="Shelf letter and position, e.g. A1 or A-1. Save to refresh the shelf map below."),
                       "shelf": st.column_config.TextColumn("Shelf", help="Comes from the location letter: A = Fridge, B = Dry goods, C = Household.", width="medium"),
                       "lifespan_group": st.column_config.TextColumn("Lifespan group", help="Automatically calculated from total shelf-life days. Not batch expiry status."),
                       "lead_time_days": "Lead time (days)", "shelf_life_days": "Shelf life (days)",
                       "current_stock": "Stock",
                       "type": st.column_config.SelectboxColumn(
                           "Perishable?", options=[PERISHABLE, NON_PERISHABLE], required=True,
                           help=f"Set automatically: perishable if it keeps {db.PERISHABLE_SHELF_DAYS} days or less, "
                                "must be kept cold (frozen / chilled), or is fresh food with no shelf life recorded. "
                                "Change it here if your product is different.")},
    )
    if st.button("Save product changes"):
        db.upsert_products(edited.drop(columns=["icon", "shelf", "type", "lifespan_group"]), stock_source="manual_product_edit")
        db.set_perishable(dict(zip(edited["sku"], edited["type"] == PERISHABLE)))
        st.success("Saved.")
        st.rerun()
    location_map.render(prods)
    st.subheader("Recent movements")
    st.dataframe(db.moves(), hide_index=True, width="stretch")


def page_events():
    ui.heading("Holidays & promotions", "Tell the forecast what's coming.",
               "Mark public holidays, promotions and days the shop is closed. Stocker AI measures how past "
               "events changed each product's sales and adjusts the forecast for upcoming ones.")
    prods = db.products()
    scopes = ([events.ALL] + [f"Category: {c}" for c in sorted(prods["category"].dropna().unique())]
              + [f"Product: {r.sku} · {r.name}" for r in prods.itertuples()])
    with st.container(border=True):
        st.markdown("**Add public holidays**")
        c1, c2, c3 = st.columns([2, 2, 1.4], vertical_alignment="bottom")
        country = c1.selectbox("Country", list(events.PRESETS))
        year = date.today().year
        years = c2.multiselect("Years", list(range(year - 2, year + 2)), default=[year - 1, year, year + 1])
        if c3.button("Add holidays", type="primary", width="stretch"):
            n = events.add(events.preset_rows(country, years))
            retrain()
            st.toast(f"Added {n} holiday dates.")
            st.rerun()
        st.caption("Includes Lunar New Year for 2024–2028. Holidays that move each year (e.g. Hari Raya, "
                   "Deepavali, Hùng Kings) can be added as rows below — please check the dates.")

    st.subheader("Calendar")
    st.caption("**Expected change %**: leave blank to let Stocker AI measure the effect from past sales of the "
               "same event (or of the same kind). Enter it for new promotions, e.g. 30 = +30%. "
               "Kind **Closed** means no sales that day.")
    evs = events.load().assign(date=lambda d: d["date"].dt.date)
    edited = st.data_editor(
        evs, num_rows="dynamic", hide_index=True, width="stretch", key="events_editor",
        column_config={
            "date": st.column_config.DateColumn("Date", required=True, format="ddd DD MMM YYYY"),
            "name": st.column_config.TextColumn("Event", required=True),
            "kind": st.column_config.SelectboxColumn("Kind", options=events.KINDS, default="Holiday", required=True),
            "scope": st.column_config.SelectboxColumn("Applies to", options=scopes, default=events.ALL, width="medium"),
            "uplift": st.column_config.NumberColumn("Expected change %", min_value=-100, max_value=400, step=5),
        })
    if st.button("Save and update forecasts", type="primary"):
        events.save(edited)
        retrain()
        st.success("Saved. Forecasts, the stocking list and alerts now include these events.")
        st.rerun()

    st.subheader("What past events did to sales")
    rows = [{"Product": m.sku, "Event": name, "Effect": mult, "Days seen": n}
            for m in cached_all_models(data_key()).values() for name, (mult, n) in m.event_effects.items()]
    if not rows:
        st.info("No past events inside your sales history yet. Add holidays that fall within the dates you "
                "imported and Stocker AI will measure their effect.")
        return
    eff = pd.DataFrame(rows).merge(prods[["sku", "name"]], left_on="Product", right_on="sku")
    table = eff.pivot_table(index="name", columns="Event", values="Effect").round(2).rename_axis("Product")
    st.dataframe(table, width="stretch",
                 column_config={c: st.column_config.NumberColumn(c, format="%.2fx") for c in table.columns})
    st.caption("1.00x = a normal day. Effects backed by few sales are pulled toward 1.00x, so one odd day "
               "on a slow seller does not set the rule.")


def page_alerts():
    ui.heading("Alerts", "Hear about problems without opening the app.",
               "A daily summary of urgent alerts and what to buy, sent to Telegram or email — plus an "
               "optional instant message when something is about to run out.")
    cfg, state_ = notifier.settings(), notifier.state()
    active = notifier.channels(notifier.config())
    if cfg["enabled"] and active:
        st.success(f"On · daily summary at {cfg['send_time']} via {' and '.join(active)}"
                   + (" · instant stock-out alerts on" if cfg["instant"] else ""))
    elif cfg["enabled"]:
        st.warning("Alerts are on, but no channel is fully set up yet. Fill in Telegram or email below.")
    else:
        st.info("Alerts are off.")
    if last := state_.get("last"):
        failed = {k: v for k, v in last["results"].items() if v}
        msg = f"Last message: {last['what']} at {last['at']} via {', '.join(last['results']) or '—'}"
        if failed:
            st.error(msg + " — failed: " + "; ".join(f"{k}: {v}" for k, v in failed.items()))
        else:
            st.caption(msg + " ✓")
    st.caption("Messages are sent while Stocker AI is running on this computer. To get them when it is closed, "
               "schedule `python -m stocker.notifier` with cron or Windows Task Scheduler.")

    tg, em = cfg["telegram"], cfg["email"]
    with st.form("alerts_form"):
        st.markdown("**When**")
        w = st.columns([1, 1, 1.4], vertical_alignment="bottom")
        enabled = w[0].toggle("Send alerts", cfg["enabled"])
        send_at = w[1].time_input("Daily summary at", time.fromisoformat(cfg["send_time"]), step=900)
        min_sev = w[2].selectbox("Include alerts from", ["critical", "high", "medium", "low"],
                                 index=["critical", "high", "medium", "low"].index(cfg["min_severity"]),
                                 format_func=lambda s: f"{SEV_ICON[s]} {s.title()} and above" if s != "critical" else "🔴 Critical only")
        o = st.columns(3)
        include_buying = o[0].checkbox("Add buying list", cfg["include_buying"], help="Products to stock more of today.")
        only_if_needed = o[1].checkbox("Skip quiet days", cfg["only_if_needed"],
                                     help="Send nothing on days with no alerts and nothing to buy.")
        instant = o[2].checkbox("Instant stock-outs", cfg["instant"],
                                help="Message right away when a product runs out or will before a new order arrives. "
                                     "Checked every 10 minutes; each product alerts once until it is restocked.")

        st.markdown("**Telegram** — free, arrives on your phone")
        t = st.columns([0.75, 2, 1.2], vertical_alignment="bottom")
        tg_on = t[0].toggle("Use", tg["on"], key="tg_on")
        tg_token = t[1].text_input("Bot token", tg["token"], type="password",
                                   placeholder="from @BotFather" + (" (or leave blank: set in secrets)" if os.environ.get("TELEGRAM_BOT_TOKEN") else ""))
        tg_chat = t[2].text_input("Chat ID", tg["chat_id"], placeholder="e.g. 123456789")
        with st.expander("How to set up Telegram (2 minutes)"):
            st.markdown("1. In Telegram, open **@BotFather**, send `/newbot` and follow the steps. Copy the token it gives you.\n"
                        "2. Open your new bot and send it any message (e.g. *hi*).\n"
                        "3. Paste the token here, save, then press **Find my chat ID** below.\n"
                        "4. To alert a group (e.g. you and your staff), add the bot to the group and send a message there first.")

        st.markdown("**Email**")
        e1 = st.columns([0.75, 2, 0.8, 1.2], vertical_alignment="bottom")
        em_on = e1[0].toggle("Use", em["on"], key="em_on")
        em_host = e1[1].text_input("SMTP server", em["host"], placeholder="smtp.gmail.com")
        em_port = e1[2].number_input("Port", 1, 65535, int(em["port"] or 587))
        em_sec = e1[3].selectbox("Security", ["STARTTLS", "SSL", "None"], index=["STARTTLS", "SSL", "None"].index(em["security"]))
        e2 = st.columns(2)
        em_user = e2[0].text_input("Login (usually your email)", em["user"])
        em_pass = e2[1].text_input("Password / app password", em["password"], type="password",
                                   help="Gmail and Outlook need an *app password*, not your normal password.")
        e3 = st.columns(2)
        em_from = e3[0].text_input("From (optional)", em["sender"], placeholder="defaults to the login")
        em_to = e3[1].text_input("Send to", em["to"], placeholder="owner@example.com, manager@example.com")
        st.caption("🔒 Tokens and passwords are stored only in this computer's data folder.")
        if st.form_submit_button("Save alert settings", type="primary"):
            notifier.save_settings({
                "enabled": enabled, "send_time": send_at.strftime("%H:%M"), "min_severity": min_sev,
                "include_buying": include_buying, "only_if_needed": only_if_needed, "instant": instant,
                "telegram": {"on": tg_on, "token": tg_token.strip(), "chat_id": tg_chat.strip()},
                "email": {"on": em_on, "host": em_host.strip(), "port": int(em_port), "security": em_sec,
                          "user": em_user.strip(), "password": em_pass, "sender": em_from.strip(), "to": em_to.strip()}})
            st.toast("Alert settings saved.")
            st.rerun()

    b1, b2 = st.columns(2)
    if b1.button("Send a test now", icon=":material/send:", width="stretch", disabled=not active):
        with st.spinner("Sending…"):
            results = notifier.send_now()
        for ch, err in results.items():
            if err:
                st.error(f"{ch}: {err}")
            else:
                st.success(f"{ch}: sent ✓")
    token = notifier.config()["telegram"]["token"]
    if b2.button("Find my Telegram chat ID", icon=":material/search:", width="stretch", disabled=not token):
        try:
            chats = notifier.telegram_chats(token)
        except Exception as e:
            st.error(str(e))
        else:
            if chats:
                st.write("Chats that messaged your bot — copy the ID into **Chat ID** above:")
                st.dataframe(pd.DataFrame(chats, columns=["Chat ID", "Name"]), hide_index=True)
            else:
                st.warning("No messages yet. Send your bot a message in Telegram, then try again.")
    if not active:
        st.caption("Save at least one channel to send a test.")

    with st.expander("Preview today's summary", expanded=True):
        report = cached_alert_preview(data_key())
        st.html(f'<div class="alert-preview">{report[1].replace(chr(10), "<br>")}</div>')
