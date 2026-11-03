"""AI purchase drafts with mandatory human review ("Human Must Decide").

The AI drafts a purchase plan and explains its risks; the shop owner must review it. The rules
live here, in the logic layer, so no screen can bypass them:

  * every draft starts as `pending` — not approved, cannot be executed;
  * approval needs a reviewer name, an explicit handling choice for every risky item,
    and the exact content the reviewer saw (content hash), otherwise it is refused;
  * the official purchase list exists only after approval, and export refuses anything else;
  * editing an approved draft revokes the approval and voids its official list;
  * generating a new draft supersedes the old one;
  * nothing here changes inventory — approval only produces the list.

Everything is recorded in `draft_events`: the original AI output, risks, edits, decisions, times.
Reviewer names are typed in, not verified identities.
"""
import hashlib
import json

import pandas as pd

from . import agent, db, engine, planner

RISK_TYPES = {"expiry": "May expire before it sells", "overstock": "Too much stock",
              "missing_cost": "Cost unknown", "missing_stock": "Stock count unknown",
              "demand_change": "Sales changed suddenly", "weak_forecast": "Forecast is uncertain",
              "other": "Other"}
HANDLING = {"keep": "Keep the quantity — I accept this risk",
            "reduce": "I reduced the quantity",
            "remove": "Remove this item",
            "check": "Buy it, but I'll check stock/cost first"}
OVERSTOCK_DAYS = 45
ALERT_WORDS = {"overstock": ("Stock covers", "excess", "No sales in 28 days"), "expiry": ("may expire",),
               "demand_change": ("Sold ", "Weekly demand moved"), "missing_stock": ("no stock count",)}
WEAK_WAPE = 0.6

SYSTEM = """You are Stocker AI. Draft the shop's next purchase order for the owner to REVIEW (they decide; you advise).
You get the shop's real data as JSON: products with cost, price, stock on hand, shelf life, lead time, daily forecasts,
the system's suggested order range and current alerts.

1. Draft the order: choose products (exact "sku" from the data only) and whole-unit quantities for the coming order cycle
   (about lead time + 7 days), guided by forecasts, stock and the suggested ranges. Skip products that should not be bought.
2. For each item give a short reason a shop owner understands, citing the data.
3. Point out risks honestly — at least: items that may expire before they sell (compare stock + order with forecast
   over the shelf life), overstock, missing cost, missing stock count, sudden sales changes, and weak/uncertain forecasts.
   For each risk give: the evidence (numbers from the data), the possible impact, and a suggested way to handle it.
   You may include a risk for a product you chose NOT to buy if the owner should know (e.g. overstock already on shelf).
4. Never invent stock, cost, shelf life or products. If a value is null in the data, say it is unknown.
5. Do not compute money totals; the app computes costs.

Return ONLY JSON:
{
 "overview": string (2-3 sentences for the owner),
 "items": [{"sku": string, "quantity": integer, "reason": string}],
 "risks": [{"sku": string or "-" for shop-wide, "type": one of expiry|overstock|missing_cost|missing_stock|demand_change|weak_forecast|other,
            "evidence": string, "impact": string, "suggestion": string}],
 "uncertainties": [string]
}"""


class ReviewError(PermissionError):
    """An action the review rules do not allow (shown to the user)."""


# ------------------------------------------------------------------ storage helpers

def _log(con, draft_id: int, action: str, actor: str | None = None, detail=None):
    con.execute("INSERT INTO draft_events (draft_id, actor, action, detail) VALUES (?,?,?,?)",
                (draft_id, actor, action, json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None))


def _hash(items: list[dict]) -> str:
    core = [{"sku": i["sku"], "quantity": int(i["quantity"]), "handling": (i.get("handling") or {}).get("choice")}
            for i in sorted(items, key=lambda i: i["sku"])]
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()[:16]


def get(draft_id: int | None = None) -> dict | None:
    """A draft with its JSON fields decoded (latest non-superseded one if no id)."""
    sql = ("SELECT * FROM drafts WHERE id=?" if draft_id else
           "SELECT * FROM drafts WHERE status!='superseded' ORDER BY id DESC LIMIT 1")
    df = db.query(sql, (draft_id,) if draft_id else ())
    if df.empty:
        return None
    d = df.iloc[0].to_dict()
    for k in ("ai_raw", "ai_items", "items", "risks", "summary"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def events(draft_id: int) -> pd.DataFrame:
    return db.query("SELECT ts, actor, action, detail FROM draft_events WHERE draft_id=? ORDER BY id", (draft_id,))


def risky_skus(d: dict) -> set[str]:
    present = {i["sku"] for i in d["items"]}
    return {r["sku"] for r in d["risks"] if r["sku"] in present}


def unresolved(d: dict) -> list[str]:
    """Products whose risks still need the owner's explicit handling choice."""
    risky = risky_skus(d)
    return [i["product"] for i in d["items"] if i["sku"] in risky and not (i.get("handling") or {}).get("choice")]


def total_cost(items: list[dict]) -> float:
    return round(sum(i["quantity"] * i["unit_cost"] for i in items if i.get("unit_cost")), 2)


# ------------------------------------------------------------------ drafting

def _fact_risks(items: list[dict], ctx: dict) -> list[dict]:
    """Risks the app can establish from the data on its own (used to check and complete the AI's list)."""
    prods = {p["sku"]: p for p in ctx["products"]}
    models = engine.load_models()
    out = []
    for it in items:
        p, sku, qty = prods[it["sku"]], it["sku"], it["quantity"]
        stock = p["stock_on_hand"]
        daily = sum(p["forecast_by_day"]) / len(p["forecast_by_day"])
        if p["unit_cost_sgd"] is None:
            out.append({"sku": sku, "type": "missing_cost", "evidence": "No purchase cost on record.",
                        "impact": "The order total is incomplete, so the budget can't be checked.",
                        "suggestion": "Enter the unit cost on the Stock & products page before buying."})
        if stock is None:
            out.append({"sku": sku, "type": "missing_stock", "evidence": "No stock count on record.",
                        "impact": "We can't tell if you already have enough on the shelf.",
                        "suggestion": "Count what you have before ordering."})
        on_hand = stock or 0
        life = p["shelf_life_days"]
        if life and life <= planner.PERISHABLE_DAYS:
            sellable = sum(p["forecast_by_day"][:life]) if life <= 7 else daily * life
            if on_hand + qty > planner.WASTE_SLACK * sellable:
                out.append({"sku": sku, "type": "expiry",
                            "evidence": f"Keeps {life} day(s); about {sellable:.0f} expected to sell in that time, "
                                        f"but stock + order = {on_hand + qty:.0f}.",
                            "impact": f"Roughly {on_hand + qty - sellable:.0f} units could expire unsold.",
                            "suggestion": f"Order about {max(0, sellable - on_hand):.0f} or fewer."})
        if daily > 0 and (on_hand + qty) / daily > OVERSTOCK_DAYS:
            out.append({"sku": sku, "type": "overstock",
                        "evidence": f"Stock + order would last about {(on_hand + qty) / daily:.0f} days at "
                                    f"{daily:.1f}/day.",
                        "impact": "Cash is tied up and shelf space used for a long time.",
                        "suggestion": "Order less, or skip this cycle."})
        m = models.get(sku)
        if m and (m.status == "insufficient" or m.n_days < 28 or (m.wape or 0) > WEAK_WAPE):
            why = (f"only {m.n_days} days of sales history" if m.n_days < 28 or m.status == "insufficient"
                   else f"past forecasts were off by {m.wape:.0%} on average")
            out.append({"sku": sku, "type": "weak_forecast", "evidence": f"The forecast is uncertain: {why}.",
                        "impact": "The real demand could be quite different from the plan.",
                        "suggestion": "Start with a smaller quantity and re-order if it sells."})
        for msg in p["risks"]:
            if any(k in msg for k in ("Sold ", "Weekly demand moved")):
                out.append({"sku": sku, "type": "demand_change", "evidence": msg,
                            "impact": "Recent sales may not reflect what will happen next.",
                            "suggestion": "Check why sales changed before relying on the forecast."})
    return out


def generate() -> dict:
    """Ask the model for a draft, validate it, store it as `pending`. Model errors propagate unchanged."""
    ctx = planner.context()
    raw = agent.complete_json(SYSTEM, "SHOP DATA (JSON):\n" + json.dumps(ctx, ensure_ascii=False))
    prods = {p["sku"]: p for p in ctx["products"]}
    notes, items = [], {}
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        sku, qty = str(it.get("sku", "")).strip(), planner._int(it.get("quantity"))
        if sku not in prods:
            notes.append(f"Removed “{sku or '?'}” from the AI draft: not a product in your records.")
            continue
        if qty <= 0:
            continue
        p = prods[sku]
        if sku in items:
            items[sku]["quantity"] += qty
            continue
        items[sku] = {"sku": sku, "product": p["name"], "quantity": qty, "ai_quantity": qty,
                      "unit_cost": p["unit_cost_sgd"], "stock": p["stock_on_hand"], "shelf_life": p["shelf_life_days"],
                      "reason": str(it.get("reason", "")).strip(), "handling": None}
    items = list(items.values())
    for i in items:
        i["ai_quantity"] = i["quantity"]

    # risks: the AI's explanations, checked against the data; app-found risks the AI missed are added
    facts = _fact_risks(items, ctx)
    fact_keys = {(r["sku"], r["type"]) for r in facts}
    risks = []
    for r in raw.get("risks") or []:
        if not isinstance(r, dict):
            continue
        sku = str(r.get("sku", "-")).strip() or "-"
        if sku != "-" and sku not in prods:
            continue
        rtype = r.get("type") if r.get("type") in RISK_TYPES else "other"
        confirmed = (sku, rtype) in fact_keys or (    # products not in the draft: check the app's own alerts
            sku in prods and any(k in m for m in prods[sku]["risks"] for k in ALERT_WORDS.get(rtype, ())))
        risks.append({"sku": sku, "product": prods[sku]["name"] if sku in prods else "Whole shop", "type": rtype,
                      "evidence": str(r.get("evidence", "")), "impact": str(r.get("impact", "")),
                      "suggestion": str(r.get("suggestion", "")), "source": "AI", "confirmed_by_data": confirmed})
    ai_keys = {(r["sku"], r["type"]) for r in risks}
    for f in facts:
        if (f["sku"], f["type"]) not in ai_keys:
            risks.append({**f, "product": prods[f["sku"]]["name"], "source": "App check", "confirmed_by_data": True})

    summary = {"overview": str(raw.get("overview", "")), "uncertainties": [str(u) for u in raw.get("uncertainties") or []],
               "app_notes": notes, "plan_starts": ctx["plan_starts"]}
    model = agent.active()[2] if agent.active() else None
    return _store(raw, items, risks, summary, model, "drafted", {"items": len(items), "risks": len(risks), "notes": notes})


def from_plan(plan: dict, needs: str) -> dict:
    """Step 1 → step 2: the owner's plan ("Tell me your needs") becomes a pending draft to review.

    No second AI call: the plan's items and reasons are kept, and the app's own checks supply the risks."""
    ctx = planner.context()
    prods = {p["sku"]: p for p in ctx["products"]}
    rows = plan["items"].to_dict("records") if len(plan["items"]) else []
    items = [{"sku": r["sku"], "product": prods[r["sku"]]["name"], "quantity": int(r["quantity"]),
              "ai_quantity": int(r["quantity"]), "unit_cost": prods[r["sku"]]["unit_cost_sgd"],
              "stock": prods[r["sku"]]["stock_on_hand"], "shelf_life": prods[r["sku"]]["shelf_life_days"],
              "reason": str(r.get("reason") or ""), "handling": None}
             for r in rows if r["sku"] in prods and int(r["quantity"]) > 0]
    if not items:
        raise ReviewError("The plan has nothing to buy, so there is nothing to review.")
    risks = [{**f, "product": prods[f["sku"]]["name"], "source": "App check", "confirmed_by_data": True}
             for f in _fact_risks(items, ctx)]
    s = plan["summary"]
    overview = f"Drafted from your plan for: “{needs.strip()[:200]}”" if needs.strip() else "Drafted from your plan."
    summary = {"overview": overview, "uncertainties": list(plan.get("assumptions") or []) + list(plan.get("data_gaps") or []),
               "app_notes": list(plan.get("checks") or []), "plan_starts": plan.get("plan_starts") or ctx["plan_starts"],
               "source": "plan", "budget": plan.get("budget"), "horizon_days": s.get("horizon_days")}
    raw = {"source": "plan", "needs": needs, "items": [{k: r.get(k) for k in ("sku", "quantity", "reason")} for r in rows]}
    return _store(raw, items, risks, summary, plan.get("model"), "drafted from your plan",
                  {"items": len(items), "risks": len(risks), "needs": needs.strip()[:200]})


def _store(raw: dict, items: list[dict], risks: list[dict], summary: dict, model: str | None, action: str, detail) -> dict:
    """Save a new pending draft; it replaces (supersedes) any open one."""
    h = _hash(items)
    with db.connect() as con:
        old = con.execute("SELECT id FROM drafts WHERE status IN ('pending','approved')").fetchall()
        for (oid,) in old:
            con.execute("UPDATE drafts SET status='superseded' WHERE id=?", (oid,))
            con.execute("UPDATE purchase_orders SET status='void' WHERE draft_id=?", (oid,))
            _log(con, oid, "superseded", None, "A new draft replaced this one; any approval no longer applies.")
        cur = con.execute(
            "INSERT INTO drafts (status, model, ai_raw, ai_items, items, risks, summary, content_hash) "
            "VALUES ('pending',?,?,?,?,?,?,?)",
            (model, json.dumps(raw, ensure_ascii=False), json.dumps(items, ensure_ascii=False),
             json.dumps(items, ensure_ascii=False), json.dumps(risks, ensure_ascii=False),
             json.dumps(summary, ensure_ascii=False), h))
        did = cur.lastrowid
        _log(con, did, action, f"AI ({model})", detail)
    return get(did)


# ------------------------------------------------------------------ review actions (rules enforced here)

def _require(d: dict | None, *statuses: str) -> dict:
    if d is None:
        raise ReviewError("No draft found.")
    if d["status"] not in statuses:
        raise ReviewError(f"This draft is {d['status']}; that action is not allowed.")
    return d


def _save_items(d: dict, items: list[dict], actor: str, action: str, detail) -> dict:
    """Persist edited items. Any change to an approved draft revokes the approval and voids its list."""
    h = _hash(items)
    with db.connect() as con:
        if d["status"] == "approved" and h != d["approved_hash"]:
            con.execute("UPDATE drafts SET status='pending', approved_hash=NULL, decided_at=NULL WHERE id=?", (d["id"],))
            con.execute("UPDATE purchase_orders SET status='void' WHERE draft_id=?", (d["id"],))
            _log(con, d["id"], "approval revoked", actor, "Approved content was changed; it must be reviewed again.")
        con.execute("UPDATE drafts SET items=?, content_hash=? WHERE id=?",
                    (json.dumps(items, ensure_ascii=False), h, d["id"]))
        _log(con, d["id"], action, actor, detail)
    return get(d["id"])


def set_quantity(draft_id: int, sku: str, quantity: int, actor: str = "") -> dict:
    d = _require(get(draft_id), "pending", "approved")
    quantity = planner._int(quantity)
    items = d["items"]
    it = next((i for i in items if i["sku"] == sku), None)
    if it is None:
        raise ReviewError("That product is not in this draft.")
    if it["quantity"] == quantity:
        return d
    old = it["quantity"]
    it["quantity"] = quantity
    if it.get("handling") and it["handling"].get("choice") in ("keep", "reduce"):
        it["handling"] = None             # the risk decision was made for a different quantity
    return _save_items(d, items, actor or None, "quantity changed",
                       {"sku": sku, "product": it["product"], "from": old, "to": quantity})


def set_handling(draft_id: int, sku: str, choice: str, note: str = "", actor: str = "") -> dict:
    d = _require(get(draft_id), "pending", "approved")
    if choice not in HANDLING:
        raise ReviewError("Unknown handling choice.")
    items = d["items"]
    it = next((i for i in items if i["sku"] == sku), None)
    if it is None:
        raise ReviewError("That product is not in this draft.")
    if choice == "reduce" and it["quantity"] >= it["ai_quantity"]:
        raise ReviewError(f"“Reduced” needs a quantity below the AI's {it['ai_quantity']}. Change the quantity first.")
    if choice == "remove":
        it["quantity"] = 0
    it["handling"] = {"choice": choice, "note": note.strip()}
    return _save_items(d, items, actor or None, "risk handled",
                       {"sku": sku, "product": it["product"], "choice": HANDLING[choice], "note": note.strip()})


def approve(draft_id: int, reviewer: str, note: str, seen_hash: str) -> int:
    """Approve exactly what the reviewer saw; creates the official purchase list. Returns its id."""
    d = _require(get(draft_id), "pending")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ReviewError("Enter the reviewer's name before approving.")
    if seen_hash != d["content_hash"]:
        raise ReviewError("The draft changed since you looked at it. Review it again before approving.")
    left = unresolved(d)
    if left:
        raise ReviewError("Choose how to handle the risk for: " + ", ".join(left) + ".")
    items = [i for i in d["items"] if i["quantity"] > 0]
    if not items:
        raise ReviewError("Nothing left to buy in this draft. Reject it instead.")
    with db.connect() as con:
        con.execute("UPDATE drafts SET status='approved', approved_hash=?, reviewer=?, decision_note=?, "
                    "decided_at=datetime('now','localtime') WHERE id=?", (d["content_hash"], reviewer, note.strip(), d["id"]))
        cur = con.execute("INSERT INTO purchase_orders (draft_id, approved_by, content_hash, items, total_cost) "
                          "VALUES (?,?,?,?,?)", (d["id"], reviewer, d["content_hash"],
                                                 json.dumps(items, ensure_ascii=False), total_cost(items)))
        _log(con, d["id"], "approved", reviewer, {"note": note.strip(), "order_id": cur.lastrowid,
                                                  "total_cost": total_cost(items)})
        return cur.lastrowid


def reject(draft_id: int, reviewer: str, note: str) -> None:
    d = _require(get(draft_id), "pending", "approved")
    reviewer = reviewer.strip()
    if not reviewer:
        raise ReviewError("Enter the reviewer's name before rejecting.")
    with db.connect() as con:
        con.execute("UPDATE drafts SET status='rejected', approved_hash=NULL, reviewer=?, decision_note=?, "
                    "decided_at=datetime('now','localtime') WHERE id=?", (reviewer, note.strip(), d["id"]))
        con.execute("UPDATE purchase_orders SET status='void' WHERE draft_id=?", (d["id"],))
        _log(con, d["id"], "rejected", reviewer, {"note": note.strip()})


def official_order(draft_id: int) -> dict:
    """The official purchase list — only for an approved draft whose content still matches the approval."""
    d = get(draft_id)
    if d is None or d["status"] != "approved" or d["approved_hash"] != d["content_hash"]:
        raise ReviewError("Not approved yet — the official purchase list is only available after approval.")
    po = db.query("SELECT * FROM purchase_orders WHERE draft_id=? AND status='valid' ORDER BY id DESC LIMIT 1",
                  (draft_id,))
    if po.empty or po.iloc[0]["content_hash"] != d["approved_hash"]:
        raise ReviewError("The approval no longer matches this draft. Review it again.")
    row = po.iloc[0].to_dict()
    row["items"] = json.loads(row["items"])
    return row


def log_export(draft_id: int) -> None:
    po = official_order(draft_id)               # raises unless approved
    with db.connect() as con:
        _log(con, draft_id, "exported", po["approved_by"], {"order_id": int(po["id"])})


def export_csv(draft_id: int) -> str:
    po = official_order(draft_id)               # raises unless approved
    df = pd.DataFrame(po["items"])[["sku", "product", "quantity", "unit_cost"]]
    df["est_cost"] = (df["quantity"] * df["unit_cost"]).round(2)
    header = (f"# Official purchase list #{po['id']} — approved by {po['approved_by']} (name typed, not verified) "
              f"on {po['created_at']}\n")
    return header + df.to_csv(index=False)
