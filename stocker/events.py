"""Holidays, promotions and closures: a small calendar the forecast learns from.

Past event days are taken out of the baseline, so a holiday spike does not inflate
normal demand, and each event's effect is measured per product from its own sales.
Upcoming event days then get that measured effect, or the change the owner entered.
"""
import pandas as pd

from . import db

KINDS = ["Holiday", "Promotion", "Closed", "Other"]
ALL = "All products"

# Lunar New Year (Tết) dates; fixed-date holidays are generated for any year.
LUNAR_NEW_YEAR = {2024: "2024-02-10", 2025: "2025-01-29", 2026: "2026-02-17",
                  2027: "2027-02-06", 2028: "2028-01-26"}
PRESETS = {
    "Vietnam": {"fixed": [("01-01", "New Year's Day"), ("04-30", "Reunification Day"),
                          ("05-01", "Labour Day"), ("09-02", "National Day")],
                "lunar": [(-1, "Lunar New Year's Eve"), (0, "Lunar New Year"), (1, "Lunar New Year"),
                          (2, "Lunar New Year")]},
    "Singapore": {"fixed": [("01-01", "New Year's Day"), ("05-01", "Labour Day"),
                            ("08-09", "National Day"), ("12-25", "Christmas Day")],
                  "lunar": [(0, "Chinese New Year"), (1, "Chinese New Year")]},
}


def load() -> pd.DataFrame:
    df = db.query("SELECT date, name, kind, scope, uplift FROM events ORDER BY date, name")
    df["date"] = pd.to_datetime(df["date"])
    return df


def save(df: pd.DataFrame):
    """Replace the calendar with the edited table (rows without a date or name are dropped)."""
    df = df.dropna(subset=["date", "name"])
    df = df[df["name"].astype(str).str.strip() != ""]
    rows = [(pd.Timestamp(r["date"]).strftime("%Y-%m-%d"), str(r["name"]).strip(),
             r.get("kind") if r.get("kind") in KINDS else "Holiday",
             r["scope"] if isinstance(r.get("scope"), str) and r["scope"] else ALL,
             None if pd.isna(r.get("uplift")) else float(r["uplift"]))
            for r in df.to_dict("records")]
    with db.connect() as con:
        con.execute("DELETE FROM events")
        con.executemany("INSERT INTO events (date, name, kind, scope, uplift) VALUES (?,?,?,?,?)", rows)


def add(rows: list[dict]) -> int:
    """Append events, skipping ones already in the calendar (same date, name and scope)."""
    cur = load()
    have = set(zip(cur["date"].dt.strftime("%Y-%m-%d"), cur["name"], cur["scope"]))
    new = [r for r in rows if (r["date"], r["name"], r.get("scope", ALL)) not in have]
    if new:
        save(pd.concat([cur, pd.DataFrame(new).assign(date=lambda d: pd.to_datetime(d["date"]))],
                       ignore_index=True))
    return len(new)


def preset_rows(country: str, years: list[int]) -> list[dict]:
    p = PRESETS[country]
    rows = [{"date": f"{y}-{md}", "name": name} for y in years for md, name in p["fixed"]]
    for y in years:
        if y in LUNAR_NEW_YEAR:
            day = pd.Timestamp(LUNAR_NEW_YEAR[y])
            rows += [{"date": (day + pd.Timedelta(days=k)).strftime("%Y-%m-%d"), "name": name}
                     for k, name in p["lunar"]]
    return [{**r, "kind": "Holiday", "scope": ALL, "uplift": None} for r in rows]


def applies(scope: str, sku: str, category: str) -> bool:
    scope = scope or ALL
    if scope.startswith("Category: "):
        return category == scope.removeprefix("Category: ")
    if scope.startswith("Product: "):
        return sku == scope.removeprefix("Product: ").split(" · ")[0]
    return True


def for_product(evs: pd.DataFrame, sku: str, category: str) -> pd.DataFrame:
    if evs.empty:
        return evs
    return evs[[applies(s, sku, category) for s in evs["scope"]]]


PRIOR_UNITS = 20    # evidence needed before an effect moves far from 1.0x


def _effect(pairs: list[tuple[float, float]]) -> list:
    """[multiplier, days] from (actual, typical) unit pairs: total sold / total typical, pulled toward
    1.0x by PRIOR_UNITS. A slow seller's one odd holiday barely counts; a busy product's counts fully."""
    actual = sum(a for a, _ in pairs)
    typical = sum(t for _, t in pairs)
    return [round((actual + PRIOR_UNITS) / (typical + PRIOR_UNITS), 3), len(pairs)]


def prepare(series: pd.Series, evs: pd.DataFrame) -> tuple[pd.Series, dict, dict]:
    """Return (baseline series, effect per event name, effect per kind).

    Each past event day is replaced by the average of the same weekday in the 4 weeks
    before it (skipping other event days); its effect compares what sold with that typical value."""
    if series.empty or evs.empty:
        return series, {}, {}
    past = evs[(evs["date"] >= series.index.min()) & (evs["date"] <= series.index.max())]
    event_days = set(past["date"])
    clean = series.copy()
    by_name, by_kind = {}, {}
    for d, g in past.groupby("date"):
        prior = [d - pd.Timedelta(weeks=k) for k in range(1, 5)]
        typical = [series[p] for p in prior if p in series.index and p not in event_days]
        if not typical:
            continue
        base = sum(typical) / len(typical)
        clean[d] = base
        for r in g.itertuples():
            by_name.setdefault(r.name, []).append((series[d], base))
            by_kind.setdefault(r.kind, []).append((series[d], base))
    return (clean, {k: _effect(v) for k, v in by_name.items()},
            {k: _effect(v)[0] for k, v in by_kind.items()})


def multiplier(row, effects: dict, kind_effects: dict) -> float:
    """Owner's expected change first, then the measured effect of this event, then of its kind."""
    if pd.notna(row.uplift):
        return max(0.0, 1 + row.uplift / 100)
    if row.kind == "Closed":
        return 0.0
    if row.name in effects:
        return effects[row.name][0]
    return kind_effects.get(row.kind, 1.0)


def future_days(evs: pd.DataFrame, effects: dict, kind_effects: dict, start: pd.Timestamp) -> dict:
    """{ISO date: [multiplier, event names]} for event days from `start` on."""
    out = {}
    for r in evs[evs["date"] >= start].itertuples():
        key = r.date.strftime("%Y-%m-%d")
        mult, names = out.get(key, [1.0, []])
        out[key] = [min(5.0, mult * multiplier(r, effects, kind_effects)), names + [r.name]]
    return {k: [round(m, 3), ", ".join(n)] for k, (m, n) in out.items()}


def upcoming(days: int = 7) -> pd.DataFrame:
    today = pd.Timestamp.today().normalize()
    evs = load()
    return evs[(evs["date"] >= today) & (evs["date"] < today + pd.Timedelta(days=days))]
