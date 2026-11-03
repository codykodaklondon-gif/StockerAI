"""Plan & order, steps 1–2: plan with AI from the owner's own words, and review/approve the AI draft."""
import pandas as pd
import streamlit as st

import ui
from stocker import agent, db, orders, planner, review


def esc(text) -> str:
    """Text for st.markdown: a bare "$" (as in "S$80") would otherwise start a maths formula."""
    return str(text).replace("$", "\\$")


NEEDS_EXAMPLES = {
    "New owner, S$120, small fridge, school event":
        "I am a first-time shop owner. I can spend at most S$120 tomorrow and have very limited fridge space. A nearby school is holding an event. I want to stock more student-friendly products, "
        "but I do not want too much leftover bread that could spoil.",
    "Tight budget, cut waste":
        "Money is tight this week. I can spend at most S$80 for tomorrow and I really want to waste less — "
        "last week I threw away a lot of bread and milk.",
    "Event — avoid running out":
        "There's a school sports day near my shop tomorrow. Budget up to S$300. I'd rather have a bit extra "
        "than run out of drinks and snacks; space is fine.",
}


def page_needs():
    ui.heading("Plan my order", "Tell me your needs.",
               "Describe your budget, space, plans and worries in your own words. Stocker AI reads them together "
               "with your sales, stock and costs, and drafts a purchase plan. The app checks every number.")
    st.session_state.setdefault("needs_text", "")
    st.caption("Try an example:")
    with st.container(horizontal=True):
        for label, text in NEEDS_EXAMPLES.items():
            st.button(label, key=f"ex_{label}", on_click=lambda t=text: st.session_state.update(needs_text=t))
    st.text_area("Your needs", key="needs_text", height=130, label_visibility="collapsed",
                 placeholder="e.g. I'm new, I can spend up to S$120 tomorrow, my fridge is small, and there's a "
                             "school event nearby. I don't want leftover bread.")
    ready = agent.available()
    if not ready:
        st.info("Add an AI key under **Settings** in the sidebar to generate a plan.", icon=":material/key:")
    if st.button("Generate my plan" if "plan_result" not in st.session_state else "Regenerate with these needs",
                 type="primary", icon=":material/auto_awesome:", disabled=not ready or not st.session_state.needs_text.strip()):
        with st.status("Reading your needs and checking your sales, stock and costs…", expanded=False) as status:
            try:
                st.session_state.plan_result = planner.generate(st.session_state.needs_text)
                st.session_state.plan_request = st.session_state.needs_text
                status.update(label="Plan ready", state="complete")
            except Exception as e:   # real error, no made-up plan
                st.session_state.pop("plan_result", None)
                status.update(label="Could not create a plan", state="error")
                st.error(agent.explain(e))
                return
    res = st.session_state.get("plan_result")
    if not res:
        return
    if st.session_state.get("plan_request") != st.session_state.needs_text:
        st.caption(":material/info: You changed your needs — press **Regenerate** to update the plan.")
    s = res["summary"]

    st.subheader("What I understood")
    with st.container(border=True):
        cols = st.columns(3)
        cols[0].metric("Budget", f"S${res['budget']:,.2f}" if res["budget"] else "Not given")
        cols[1].metric("Plan covers", f"{s['horizon_days']} day" + ("s" if s["horizon_days"] > 1 else ""),
                       help=f"Starting {res['plan_starts']}")
        cols[2].metric("Storage", s.get("storage") or "Not specified")
        for label, key in (("Goals", "goals"), ("Events", "events"), ("Worries", "worries")):
            if s.get(key):
                st.markdown(f"**{label}:** " + esc(" · ".join(s[key])))
        if s.get("experience"):
            st.markdown(f"**About you:** {esc(s['experience'])}")

    st.subheader("Suggested purchase")
    items = res["items"]
    if items.empty:
        st.warning("The AI did not suggest buying anything for these needs.")
    else:
        if res["budget"]:
            used = res["total_cost"] / res["budget"]
            st.progress(min(used, 1.0), text=f"S\\${res['total_cost']:,.2f} of S\\${res['budget']:,.2f} budget "
                                             f"({used:.0%}) · checked by the app")
        else:
            st.metric("Estimated cost", f"S${res['total_cost']:,.2f}")
        st.dataframe(
            items[["product", "quantity", "unit_cost", "est_cost", "covers", "reason"]],
            hide_index=True, width="stretch",
            column_config={
                "product": "Product", "quantity": st.column_config.NumberColumn("Buy", format="%d"),
                "unit_cost": st.column_config.NumberColumn("Unit cost", format="S$%.2f"),
                "est_cost": st.column_config.NumberColumn("Cost", format="S$%.2f"),
                "covers": st.column_config.NumberColumn("Covers demand", format="percent",
                                                        help="(stock + this order) ÷ expected sales for the plan period"),
                "reason": st.column_config.TextColumn("Why", width="large"),
            })
        st.download_button("Download plan (CSV)", items.drop(columns=["sku"]).to_csv(index=False),
                           "purchase_plan.csv", "text/csv", icon=":material/download:")
    if res["checks"]:
        with st.container(border=True):
            st.markdown(":material/fact_check: **App checks**")
            for c in res["checks"]:
                st.markdown(f"- {esc(c)}")

    strat = {k: v for k, v in res["strategy"].items() if v}
    if strat:
        st.subheader("How this plan fits your needs")
        names = {"budget": ":material/payments: Budget", "space": ":material/kitchen: Space",
                 "waste": ":material/delete: Waste", "stockout": ":material/inventory: Running out"}
        for k, v in strat.items():
            st.markdown(f"**{names.get(k, k.title())}** — {esc(v)}")
    if res["assumptions"] or res["data_gaps"]:
        st.info("**Assumptions & missing data**\n\n" + "\n".join(f"- {esc(a)}" for a in res["assumptions"] + res["data_gaps"]),
                icon=":material/help:")
    if res["questions"]:
        st.warning("**To make the plan better, tell me:**\n\n" + "\n".join(f"- {esc(q)}" for q in res["questions"]),
                   icon=":material/chat:")
    st.caption(f"Generated by {res['model']}. Suggestions only — you decide what to buy.")

    # ---- step 1 → step 2
    with st.container(border=True):
        st.markdown("**Next: review and approve this plan**")
        st.caption("Sends these items to **2 · Review & approve**, where the app checks each one for expiry, overstock and "
                   "missing data. It replaces any draft waiting there.")
        if st.button("Send this plan for review →", type="primary", icon=":material/fact_check:",
                     disabled=items.empty or st.session_state.get("plan_request") != st.session_state.needs_text):
            try:
                review.from_plan(res, st.session_state.get("plan_request", ""))
            except review.ReviewError as e:
                st.error(str(e))
                return
            st.session_state.review_msg = None
            ui.go_to("✅ Review")
            st.rerun()


STATUS_BANNER = {
    "pending": ("warning", "Pending review — not approved, cannot be executed",
                "Check the AI's suggestions and risks, adjust anything you like, then approve or reject."),
    "approved": ("success", "Approved — official purchase list created",
                 "Changing anything below will cancel this approval and send the draft back for review."),
    "rejected": ("error", "Rejected — this draft will not be used", "Generate a new draft when you're ready."),
}


def _review_action(fn, *args):
    """Run a review action from a widget callback; rule violations are shown, never hidden."""
    db.use_workspace(st.session_state.ws)   # callbacks run before the script re-selects this visitor's workspace
    try:
        fn(*args)
        st.session_state.review_msg = None
    except review.ReviewError as e:
        st.session_state.review_msg = str(e)


def page_review():
    ui.heading("Review AI draft", "Check before you buy.",
               "Stocker AI drafts your next purchase and points out the risks. Nothing is bought, sent or changed "
               "until you approve it.")
    d = review.get()
    ready = agent.available()
    if not ready:
        st.info("Add an AI key under **Settings** in the sidebar to draft a purchase.", icon=":material/key:")
    label = "Draft a purchase with AI" if d is None else "Generate a new AI draft (replaces this one)"
    if st.button(label, type="primary" if d is None else "secondary", icon=":material/auto_awesome:", disabled=not ready):
        with st.status("Stocker AI is drafting your purchase and checking the risks…") as status:
            try:
                d = review.generate()
                st.session_state.review_msg = None
                status.update(label="Draft ready — waiting for your review", state="complete")
            except Exception as e:      # real model / network error, no made-up draft
                status.update(label="Could not create a draft", state="error")
                st.error(agent.explain(e))
                return
    if d is None:
        st.caption("Or start from **1 · Plan with AI**: describe your needs there and send the plan here.")
        return
    if st.session_state.get("review_msg"):
        st.error(st.session_state.review_msg, icon=":material/block:")

    kind, title, body = STATUS_BANNER[d["status"]]
    getattr(st, kind)(body, title=title, icon={"warning": ":material/pending_actions:", "success": ":material/verified:",
                                                "error": ":material/cancel:"}[kind])
    if d["status"] != "pending" and d["reviewer"]:
        st.caption(f"{d['status'].title()} by {d['reviewer']} (name typed, not verified) at {d['decided_at']}"
                   + (f" — “{d['decision_note']}”" if d["decision_note"] else ""))
    s = d["summary"]
    if s.get("source") == "plan":
        st.markdown(f":material/link: **From step 1:** {esc(s['overview'])}"
                    + (f" Budget S\\${s['budget']:,.2f}." if s.get("budget") else ""))
    else:
        st.markdown(f"**AI overview:** {esc(s['overview'])}" if s["overview"] else "")
    st.caption(f"Draft #{d['id']} · created {d['created_at']} by {d['model'] or 'AI'} · plan starts {s['plan_starts']}")
    for n in s["app_notes"]:
        st.caption(f":material/info: {esc(n)}")

    editable = d["status"] in ("pending", "approved")
    risks = d["risks"]
    by_sku: dict[str, list] = {}
    for r in risks:
        by_sku.setdefault(r["sku"], []).append(r)

    # ---- risks the AI found (and the app checked)
    st.subheader(f"Risks to check ({len(risks)})")
    if not risks:
        st.success("No risks found in this draft.")
    for r in risks:
        with st.container(border=True):
            tag = "AI" if r["source"] == "AI" else "App check"
            verdict = ("✓ confirmed by your data" if r["confirmed_by_data"]
                       else "⚠ the app could not confirm this from your data")
            ui.badge(f"{review.RISK_TYPES[r['type']]} · {tag}", "coral" if r["type"] in ("expiry", "overstock") else "amber")
            st.markdown(f"**{r['product']}** — {esc(r['evidence'])}")
            st.markdown(f"**Possible impact:** {esc(r['impact'])}  \n**Suggestion:** {esc(r['suggestion'])}")
            st.caption(verdict)
    if s["uncertainties"]:
        st.info("**What the AI could not confirm**\n\n" + "\n".join(f"- {esc(u)}" for u in s["uncertainties"]),
                icon=":material/help:")

    # ---- items: edit quantities, decide on risky ones
    st.subheader("Draft purchase")
    for it in d["items"]:
        risky = it["sku"] in by_sku
        with st.container(border=True):
            top = st.columns([3, 2], vertical_alignment="center")
            top[0].markdown(f"**{it['product']}**" + ("  :orange-badge[:material/warning: has risks]" if risky else ""))
            top[0].caption(f"AI suggested {it['ai_quantity']} · stock "
                           + (f"{it['stock']:g}" if it["stock"] is not None else "unknown")
                           + (f" · keeps {it['shelf_life']} days" if it["shelf_life"] else "")
                           + (f" · S\\${it['unit_cost']:.2f} each" if it["unit_cost"] else " · cost unknown"))
            top[1].number_input("Quantity to buy", min_value=0, step=1, value=int(it["quantity"]),
                                key=f"q_{d['id']}_{it['sku']}_{it['quantity']}", disabled=not editable,
                                on_change=lambda sku=it["sku"], k=f"q_{d['id']}_{it['sku']}_{it['quantity']}":
                                _review_action(review.set_quantity, d["id"], sku, st.session_state[k],
                                               st.session_state.get("reviewer_name", "")))
            st.caption(f"Why: {esc(it['reason'])}")
            if risky:
                h = it.get("handling") or {}
                if h.get("choice"):
                    st.markdown(f":green[:material/check_circle: Your decision: {review.HANDLING[h['choice']]}]"
                                + (f" — “{esc(h['note'])}”" if h.get("note") else ""))
                with st.form(f"h_{d['id']}_{it['sku']}", border=False):
                    choice = st.segmented_control("How will you handle this risk?", list(review.HANDLING),
                                                  format_func=lambda c: review.HANDLING[c].split(" —")[0],
                                                  default=h.get("choice"), disabled=not editable)
                    note = st.text_input("Note (optional)", value=h.get("note", ""), disabled=not editable)
                    if st.form_submit_button("Save decision", disabled=not editable):
                        if choice:
                            _review_action(review.set_handling, d["id"], it["sku"], choice, note,
                                           st.session_state.get("reviewer_name", ""))
                        else:
                            st.session_state.review_msg = "Pick how you will handle the risk first."
                        st.rerun()
    st.metric("Estimated cost (from your recorded costs)", f"S${review.total_cost(d['items']):,.2f}")

    # ---- decision
    if editable:
        st.subheader("Your decision")
        with st.container(border=True):
            st.text_input("Reviewer name", key="reviewer_name", help="Typed name only — not a verified login.")
            st.text_area("Review note (optional)", key="review_note", height=80)
            left = review.unresolved(d)
            if left:
                st.caption(":material/warning: Decide how to handle the risks for: " + ", ".join(left))
            c = st.columns(2)
            if d["status"] == "pending":
                # name and note are read when the button is clicked; the hash is what the reviewer is looking at now
                c[0].button("Approve and create official purchase list", type="primary", icon=":material/verified:",
                            width="stretch", on_click=lambda h=d["content_hash"]: _review_action(
                                review.approve, d["id"], st.session_state.get("reviewer_name", ""),
                                st.session_state.get("review_note", ""), h))
            c[1].button("Reject draft", icon=":material/cancel:", width="stretch", on_click=lambda: _review_action(
                review.reject, d["id"], st.session_state.get("reviewer_name", ""), st.session_state.get("review_note", "")))

    # ---- official list: only exists after approval (checked by review.official_order, not just here)
    st.subheader("Official purchase list")
    try:
        po = review.official_order(d["id"])
        items = pd.DataFrame(po["items"])
        items["est_cost"] = (items["quantity"] * items["unit_cost"]).round(2)
        st.dataframe(items[["product", "quantity", "unit_cost", "est_cost"]], hide_index=True, width="stretch",
                     column_config={"product": "Product", "quantity": "Buy",
                                    "unit_cost": st.column_config.NumberColumn("Unit cost", format="S$%.2f"),
                                    "est_cost": st.column_config.NumberColumn("Cost", format="S$%.2f")})
        st.download_button("Download official purchase list (CSV)", review.export_csv(d["id"]),
                           f"purchase_list_{po['id']}.csv", "text/csv", icon=":material/download:",
                           on_click=review.log_export, args=(d["id"],))
        # ---- step 2 → step 3
        used = (orders.source() or {}).get("order_id") == int(po["id"])
        with st.container(border=True):
            st.markdown("**Next: final order list**")
            if used:
                st.caption("✓ This approved list is your final order list. Choose suppliers there.")
                st.button("Open the final order list →", type="primary", icon=":material/receipt_long:",
                          on_click=ui.go_to, args=("🧾 Order list",))
            else:
                st.caption("Puts these items on **3 · Final list**, replacing what is there now, so you can pick a "
                           "supplier for each line and send the orders.")
                if st.button("Use as my final order list →", type="primary", icon=":material/receipt_long:"):
                    orders.from_approved(d["id"])
                    st.session_state.pop("order_editor", None)
                    ui.go_to("🧾 Order list")
                    st.rerun()
    except review.ReviewError as e:
        st.info(str(e), icon=":material/lock:")

    with st.expander("Review history (audit trail)"):
        ev = review.events(d["id"])
        st.dataframe(ev, hide_index=True, width="stretch")
        st.caption("Original AI draft, exactly as the model returned it:")
        st.json(d["ai_raw"], expanded=False)


# Additive mode selector. Original page_needs implementation above stays unchanged.
_original_page_needs = page_needs


def page_needs():
    from views.purchase_modes import page
    page(_original_page_needs)
