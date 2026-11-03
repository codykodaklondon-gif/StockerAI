"""Generate a realistic demo dataset for a generic small shop."""
import numpy as np
import pandas as pd

CATALOG = [
    # sku, name, category, base/day, price, cost, lead, weekend_mult, trend/day, shelf_life
    ("BRD-01", "White Bread", "Bakery", 40, 2.8, 1.6, 1, 1.35, 0.000, 4),
    ("MLK-01", "Fresh Milk 1L", "Dairy", 30, 3.5, 2.4, 2, 1.20, 0.002, 10),
    ("EGG-12", "Eggs (12 pack)", "Dairy", 22, 4.2, 2.9, 2, 1.25, 0.000, 21),
    ("COF-01", "Instant Coffee", "Beverage", 12, 6.9, 4.1, 5, 1.05, 0.000, 365),
    ("TEA-01", "Teh Bags 100s", "Beverage", 6, 5.5, 3.2, 5, 1.00, -0.003, 365),
    ("DRK-01", "Isotonic Drink", "Beverage", 25, 1.9, 0.9, 3, 1.40, 0.006, 270),
    ("NDL-01", "Instant Noodles 5s", "Grocery", 18, 4.5, 2.7, 4, 1.10, 0.000, 240),
    ("RCE-05", "Rice 5kg", "Grocery", 4, 14.9, 10.5, 7, 1.15, 0.000, 365),
    ("TIS-01", "Tissue Pack", "Household", 9, 3.2, 1.8, 7, 1.05, 0.000, None),
    ("DET-01", "Detergent 2L", "Household", 3, 11.9, 7.2, 7, 1.10, -0.004, None),
    ("ICE-01", "Ice Cream Tub", "Frozen", 7, 8.5, 5.0, 3, 1.50, 0.004, 180),
    ("SNK-01", "Potato Chips", "Snacks", 15, 2.5, 1.3, 4, 1.30, 0.000, 120),
]


def generate(days: int = 180, seed: int = 7, end: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    end = (end or pd.Timestamp.today()).normalize() - pd.Timedelta(days=1)
    dates = pd.date_range(end=end, periods=days, freq="D")
    holidays = {d for d in dates if (d.month, d.day) in {(1, 1), (5, 1), (8, 9), (12, 25), (10, 31), (11, 1)}}
    rows, prods = [], []
    for sku, name, cat, base, price, cost, lead, wk, trend, life in CATALOG:
        for i, d in enumerate(dates):
            lam = base * (1 + trend * i)
            if d.day_of_week >= 5:
                lam *= wk
            if d in holidays:
                lam *= 1.6
            lam *= 1 + 0.15 * np.sin(2 * np.pi * i / 30)  # monthly payday cycle
            q = rng.poisson(max(lam, 0.1))
            rows.append({"Date": d.date(), "Product Code": sku, "Product Name": name,
                         "Category": cat, "Quantity": q, "Unit Price": price})
        avg = base * (1 + trend * days)
        # deliberately varied stock positions so the risk monitor has something to find
        cover = {"BRD-01": 0.6, "MLK-01": 3, "EGG-12": 5, "COF-01": 25, "TEA-01": 70,
                 "DRK-01": 2, "NDL-01": 12, "RCE-05": 9, "TIS-01": 20, "DET-01": 95,
                 "ICE-01": 6, "SNK-01": 10}[sku]
        prods.append({"sku": sku, "name": name, "category": cat, "unit_price": price,
                      "unit_cost": cost, "lead_time_days": lead,
                      "current_stock": round(avg * cover), "shelf_life_days": life})
    sales = pd.DataFrame(rows)
    # plant an anomaly: yesterday's isotonic drink sales spiked
    m = (sales["Product Code"] == "DRK-01") & (sales["Date"] == end.date())
    sales.loc[m, "Quantity"] = sales.loc[m, "Quantity"] * 3
    return sales, pd.DataFrame(prods)


# share of each category's sales by hour (shop open 7:00-22:00)
HOURS = np.arange(7, 22)
HOUR_PROFILE = {
    "Bakery":    [10, 14, 12, 8, 5, 4, 3, 3, 3, 3, 4, 4, 3, 2, 1],
    "Dairy":     [8, 11, 10, 7, 5, 4, 4, 4, 5, 6, 7, 6, 4, 2, 1],
    "Beverage":  [3, 4, 4, 5, 7, 9, 10, 10, 9, 8, 7, 6, 5, 3, 2],
    "Snacks":    [1, 2, 2, 3, 5, 6, 7, 8, 9, 10, 10, 9, 8, 6, 4],
    "Frozen":    [1, 1, 2, 3, 5, 8, 10, 11, 10, 9, 8, 7, 6, 4, 2],
}
DEFAULT_PROFILE = [3, 4, 5, 6, 7, 7, 7, 7, 7, 8, 9, 9, 7, 5, 3]
MEMBER_BIAS = {"Grocery": 0.65, "Household": 0.7, "Dairy": 0.55, "Bakery": 0.5}   # P(member) by category


def to_transactions(daily: pd.DataFrame, seed: int = 11, n_members: int = 220) -> pd.DataFrame:
    """Split daily product totals into receipt lines with time of day and anonymous customers."""
    rng = np.random.default_rng(seed)
    members = [f"M{1000 + i}" for i in range(n_members)]
    # regulars visit far more often than occasional members
    weights = rng.pareto(1.3, n_members) + 0.2
    weights /= weights.sum()
    rows = []
    for (d, sku, name, cat, price), g in daily.groupby(["Date", "Product Code", "Product Name", "Category", "Unit Price"]):
        q = int(g["Quantity"].sum())
        if q == 0:
            continue
        prof = np.array(HOUR_PROFILE.get(cat, DEFAULT_PROFILE), float)
        prof /= prof.sum()
        while q > 0:
            units = int(min(q, rng.choice([1, 1, 1, 2, 2, 3])))
            q -= units
            member = rng.random() < MEMBER_BIAS.get(cat, 0.4)
            hour = int(rng.choice(HOURS, p=prof))
            rows.append((d, hour, sku, name, cat, units, price,
                         "Member" if member else "Walk-in",
                         rng.choice(members, p=weights) if member else ""))
    tx = pd.DataFrame(rows, columns=["Date", "Hour", "Product Code", "Product Name", "Category",
                                     "Quantity", "Unit Price", "Customer Type", "Member ID"])
    # group lines into receipts: same customer (or a walk-in) in the same hour shares a receipt
    tx["k"] = tx["Member ID"].where(tx["Member ID"] != "", "W" + (rng.integers(0, 6, len(tx))).astype(str))
    # planted affinity for the demo: many snack buyers also pick up a drink on the same receipt
    snacks = tx.index[(tx["Category"] == "Snacks") & (rng.random(len(tx)) < 0.6)]
    drinks = tx[tx["Product Code"] == "DRK-01"].groupby(["Date", "Hour"])["k"].first()
    keys = [drinks.get((tx.at[i, "Date"], tx.at[i, "Hour"])) for i in snacks]
    tx.loc[snacks, "k"] = [k if k is not None else tx.at[i, "k"] for i, k in zip(snacks, keys)]
    tx["Receipt No"] = tx.groupby(["Date", "Hour", "k"]).ngroup().map(lambda i: f"R{i:06d}")
    tx["Date"] = pd.to_datetime(tx["Date"]) + pd.to_timedelta(tx["Hour"], unit="h") \
        + pd.to_timedelta(rng.integers(0, 60, len(tx)), unit="m")
    return tx.drop(columns=["k", "Hour"]).sort_values("Date").reset_index(drop=True)


def demo_events(today: pd.Timestamp | None = None) -> list[dict]:
    """A few calendar entries so the demo shows event-aware forecasts: one past holiday to learn
    from, upcoming holidays that reuse what was learned, and a promotion this weekend."""
    today = (today or pd.Timestamp.today()).normalize()
    sat = today + pd.Timedelta(days=(5 - today.day_of_week) % 7 or 7)
    rows = [{"date": f"{y}-08-09", "name": "National Day", "kind": "Holiday", "scope": "All products", "uplift": None}
            for y in (today.year - 1, today.year)]
    rows += [{"date": f"{today.year}-12-25", "name": "Christmas Day", "kind": "Holiday", "scope": "All products", "uplift": None},
             {"date": f"{today.year + 1}-01-01", "name": "New Year's Day", "kind": "Holiday", "scope": "All products", "uplift": None}]
    rows += [{"date": (sat + pd.Timedelta(days=k)).strftime("%Y-%m-%d"), "name": "Snack weekend promo",
              "kind": "Promotion", "scope": "Category: Snacks", "uplift": 30.0} for k in (0, 1)]
    return rows


def load_demo():
    from . import batches, db, events, importer
    db.reset()
    sales, prods = generate()
    res = importer.import_dataframe(to_transactions(sales))
    db.upsert_products(prods)
    events.add(demo_events())
    today = pd.Timestamp.today().normalize()
    for sku, code, share, offset in [("BRD-01", "DEMO-BREAD-TODAY", 1, 0),
                                      ("MLK-01", "DEMO-MILK-OLD", .2, -1),
                                      ("MLK-01", "DEMO-MILK-FRESH", .5, 3),
                                      ("SNK-01", "DEMO-SNACKS", .5, 6)]:
        total = float(prods.set_index("sku").loc[sku, "current_stock"])
        batches.add(sku, code, max(1, int(total * share)), today - pd.Timedelta(days=7),
                    today + pd.Timedelta(days=offset), mode="existing", note="Demo only: example expiry date; not a real delivery.")
    from . import suppliers
    if suppliers.BUNDLED.exists() and suppliers.load().empty:   # demo shop: the bundled supplier list, matched
        suppliers.load_bundled()
    suppliers.auto_assign()
    return res
