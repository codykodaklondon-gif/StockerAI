"""Deterministic analysis and forecasting engine.

"Training" fits a small, explainable model per product from its own history:
  * level      — robust 28-day baseline (trimmed mean)
  * trend      — damped daily slope (recent 7 days vs. 28-day baseline)
  * weekday    — day-of-week multipliers from the last 8 weeks, shrunk toward 1
  * sigma      — residual standard deviation, used for ranges and safety stock
Each product then gets the variant of this model that back-tests best (see VARIANTS):
the standard one above, unless a simpler or a monthly-cycle variant is clearly more
accurate on that product's own history. Every model is back-tested against a naive
28-day average so accuracy is measured, not assumed. Holidays and promotions from the events calendar are taken out of the
baseline and applied to upcoming days (see events.py). Every number comes from code;
the AI layer only explains them.

With a year or more of history, a machine-learning model (mlmodel.py) is back-tested
against this engine and takes over for the products where it is more accurate.
"""
import json
from dataclasses import asdict, dataclass, field

import numpy as np
import pandas as pd

from . import db, events, mlmodel

MIN_DAYS = 14
DAMPING = 0.85
DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
VARIANTS = {     # standard-engine variants tried per product; "standard" is the default and the fallback
    "standard": "Weekly pattern + trend",
    "steady": "Steady average",                       # noisy slow sellers: patterns are mostly noise
    "weekday": "Same-weekday average",                # strong, stable weekly pattern
    "monthly": "Weekly pattern + monthly cycle",      # paydays / month-end bills
}
MONTHLY_MIN_DAYS = 84     # three months before a monthly cycle can be told apart from noise
SWITCH_MARGIN = 0.03      # another variant must beat "standard" by 3% (relative) in the back-test
RANGE_Z = 1.28            # 80% range


@dataclass
class Model:
    sku: str
    n_days: int
    level: float
    trend: float
    dow: list[float]
    sigma: float
    last_date: str
    avg_7: float
    avg_28: float
    wape: float | None = None
    naive_wape: float | None = None
    status: str = "ok"           # ok / insufficient / inactive
    method: str = "stats"        # stats / ml
    ml_wape: float | None = None             # recent back-test error of the ML model
    stats_wape_recent: float | None = None   # same folds, standard engine
    ml_start: str | None = None              # first date of the stored ML forecast
    ml_path: list[float] | None = None       # ML daily forecasts from ml_start
    event_days: dict = field(default_factory=dict)      # ISO date -> [multiplier, event names], upcoming days
    event_effects: dict = field(default_factory=dict)   # event name -> [measured multiplier, days seen]
    ml_event_days: dict = field(default_factory=dict)   # same, without public holidays (ML learns those itself)
    variant: str = "standard"    # key of VARIANTS: which standard-engine variant this product uses
    cycle: list[float] = field(default_factory=lambda: [0.0, 0.0])   # monthly cycle: (sin, cos) per harmonic
    variant_wape: dict = field(default_factory=dict)    # back-test error of every variant tried
    week_wape: float | None = None          # error on 7-day totals, what a re-order uses
    naive_week_wape: float | None = None
    weeks_over: list[int] = field(default_factory=lambda: [0, 0])     # [weeks sold above the 80% range, weeks tested]

    def event_mult(self, d) -> float:
        return self.event_days.get(pd.Timestamp(d).strftime("%Y-%m-%d"), [1.0])[0]

    def cycle_mult(self, d) -> float:
        return float(_cycle(self.cycle, _phase(pd.DatetimeIndex([pd.Timestamp(d)])))[0])

    def forecast_arrays(self, horizon: int = 7, start: pd.Timestamp | None = None) -> dict:
        """Daily forecast as numpy arrays (no DataFrame), for the stocking list and risk checks."""
        start = start or pd.Timestamp(self.last_date) + pd.Timedelta(days=1)
        dates = pd.date_range(start, periods=horizon, freq="D")
        keys = dates.strftime("%Y-%m-%d")
        h = np.arange(1, horizon + 1)
        damp = np.cumsum(DAMPING ** h)
        mult = np.array([self.event_days.get(k, (1.0,))[0] for k in keys])
        cyc = _cycle(self.cycle, _phase(dates))
        mu = np.maximum(0.0, (self.level + self.trend * damp) * np.asarray(self.dow)[dates.day_of_week] * cyc) * mult
        if self.method == "ml" and self.ml_path:
            # ML already knows public holidays; only promotions / closures / other events are applied
            off = (dates - pd.Timestamp(self.ml_start)).days.to_numpy()
            on_path = (off >= 0) & (off < len(self.ml_path))
            if on_path.any():
                ml_mult = np.array([self.ml_event_days.get(k, (1.0,))[0] for k in keys])
                mu = np.where(on_path, np.asarray(self.ml_path)[np.clip(off, 0, len(self.ml_path) - 1)] * ml_mult, mu)
                mult = np.where(on_path, ml_mult, mult)
        sd = self.sigma * np.sqrt(1 + 0.05 * h) * mult
        return {"date": dates, "forecast": mu, "low": np.maximum(0.0, mu - RANGE_Z * sd), "high": mu + RANGE_Z * sd,
                "event": [self.event_days.get(k, (1.0, ""))[1] for k in keys]}   # event names (shown with ★)

    def forecast(self, horizon: int = 7, start: pd.Timestamp | None = None) -> pd.DataFrame:
        return pd.DataFrame(self.forecast_arrays(horizon, start))


def daily_series(sku: str, sales: pd.DataFrame | None = None, until: pd.Timestamp | None = None) -> pd.Series:
    """Daily quantity series with missing days filled as zero."""
    df = sales if sales is not None else db.sales(sku)
    if "sku" in df and df["sku"].nunique() > 1:     # callers may pass a frame already filtered to one sku
        df = df[df["sku"] == sku]
    if df.empty:
        return pd.Series(dtype=float)
    s = df.groupby("date")["qty"].sum()
    end = until or s.index.max()
    s = s.reindex(pd.date_range(s.index.min(), end, freq="D"), fill_value=0.0)
    return s.astype(float)


def _trimmed_mean(x: np.ndarray, frac: float = 0.1) -> float:
    if len(x) == 0:
        return 0.0
    x = np.sort(x)
    k = int(len(x) * frac)
    return float(x[k:len(x) - k].mean()) if len(x) > 2 * k else float(x.mean())


def _phase(index: pd.DatetimeIndex) -> np.ndarray:
    """Position of each day in its calendar month, as an angle (for the monthly cycle)."""
    return 2 * np.pi * (index.day.to_numpy() - 1) / index.days_in_month.to_numpy()


def _harmonics(phase: np.ndarray, n: int) -> np.ndarray:
    """sin/cos of the first n monthly harmonics; two let a cycle peak sharply (a payday) instead of a smooth wave."""
    return np.column_stack([f(k * phase) for k in range(1, n + 1) for f in (np.sin, np.cos)])


def _cycle(cycle: list[float], phase: np.ndarray) -> np.ndarray:
    """Monthly multiplier for each day; cycle = [] or all zeros means none."""
    if not any(cycle):
        return np.ones(len(phase))
    return 1 + _harmonics(phase, len(cycle) // 2) @ np.asarray(cycle)


def fit(series: pd.Series, sku: str = "", variant: str = "standard") -> Model:
    n = len(series)
    last = series.index.max().strftime("%Y-%m-%d") if n else ""
    if n < MIN_DAYS:
        avg = float(series.mean()) if n else 0.0
        return Model(sku, n, avg, 0.0, [1.0] * 7, max(1.0, avg ** 0.5), last, avg, avg,
                     status="insufficient")
    return _fit_arrays(series.to_numpy(dtype=float), series.index.day_of_week.to_numpy(), sku, n, last,
                       variant=variant, phase=_phase(series.index))


def _monthly_cycle(y: np.ndarray, mult_days: np.ndarray, phase: np.ndarray, harmonics: int = 2) -> list[float]:
    """Day-of-month pattern (sin, cos amplitudes per harmonic) from the last 16 weeks, shrunk toward none
    by how clearly it stands out from day-to-day noise."""
    w = min(len(y), 112)
    z = y[-w:] / mult_days[-w:]
    base = pd.Series(z).rolling(29, center=True, min_periods=15).mean().to_numpy()   # a month's average around each day
    ok = base > 0
    if ok.sum() < 56:
        return [0.0] * (2 * harmonics)
    r = z[ok] / base[ok] - 1
    X = _harmonics(phase[-w:][ok], harmonics)
    coef, *_ = np.linalg.lstsq(X, r, rcond=None)
    amp2 = float(coef @ coef)
    se2 = 2 * float(np.var(r - X @ coef)) / len(r)       # noise in each coefficient
    coef = coef * max(0.0, 1 - 1.5 * X.shape[1] * se2 / amp2) if amp2 > 0 else coef * 0
    peak = float(np.abs(_harmonics(np.linspace(0, 2 * np.pi, 31), harmonics) @ coef).max())
    coef = coef * min(1.0, 0.4 / max(peak, 1e-9))         # never more than +-40% on any day
    return [float(c) for c in coef]


def _fit_arrays(y: np.ndarray, days: np.ndarray, sku: str = "", n: int = 0, last: str = "",
                variant: str = "standard", phase: np.ndarray | None = None) -> Model:
    """Core of `fit` on plain numpy arrays (values and weekday numbers); used by back-testing too."""
    y28, d28 = y[-28:], days[-28:]
    avg7 = float(y[-7:].mean())
    avg28 = float(y28.mean())

    # weekday multipliers over the last 8 weeks, shrunk toward 1 by sample size
    y56, d56 = y[-56:], days[-56:]
    overall = y56.mean() or 1.0
    dow = []
    for k in range(7):
        vals = y56[d56 == k]
        raw = (vals.mean() / overall) if len(vals) else 1.0
        # stronger shrinkage for low-volume items, where weekday noise dominates
        w = len(vals) / (len(vals) + 3 + 40 / max(overall, 1.0))
        dow.append(float(raw if variant == "weekday" else w * raw + (1 - w)))
    if variant == "steady":
        dow = [1.0] * 7
    norm = np.mean(dow)
    dow = [float(d / norm) for d in dow]
    mult = np.array(dow)

    cycle, cyc = [0.0, 0.0], np.ones(len(y))
    if variant == "monthly" and phase is not None:
        cycle = _monthly_cycle(y, mult[days], phase)
        cyc = _cycle(cycle, phase)
    adj = mult[days] * cyc        # weekday x monthly multiplier of every past day

    if variant in ("steady", "weekday"):
        # plain averages: no trend, no trimming
        level = float((y56 / adj[-56:]).mean()) if variant == "weekday" else avg28
        trend = 0.0
    else:
        # level de-seasonalised so weekday (and monthly) effects are not double counted
        level = _trimmed_mean(y28 / adj[-28:])
        recent = float((y[-7:] / adj[-7:]).mean())
        trend = (recent - level) / 17.5 if level > 0 else 0.0     # 17.5 = gap between window centres
        trend = float(np.clip(trend, -0.05 * level, 0.05 * level))

    fitted = level * adj[-28:]
    sigma = float(np.std(y28 - fitted, ddof=1)) if len(y28) > 1 else 1.0
    sigma = max(sigma, 0.5, 0.1 * level)

    status = "inactive" if y28.sum() == 0 else "ok"
    return Model(sku, n or len(y), level, trend, dow, sigma, last, avg7, avg28, status=status,
                 variant=variant, cycle=cycle)


def predict_arrays(y: np.ndarray, hist: pd.DatetimeIndex, future: pd.DatetimeIndex, variant: str = "standard") -> np.ndarray:
    """Fit a variant on past values `y` (dated `hist`) and forecast the `future` dates; used by the ML comparison."""
    m = _fit_arrays(y, hist.day_of_week.to_numpy(), variant=variant, phase=_phase(hist))
    cyc = _cycle(m.cycle, _phase(future))
    return np.maximum(0.0, (m.level + m.trend * _DAMP[:len(future)]) * np.array(m.dow)[future.day_of_week.to_numpy()] * cyc)


_DAMP = np.cumsum(DAMPING ** np.arange(1, 32))      # damped-trend multiplier for horizon h = 1..31


def _folds(y: np.ndarray, days: np.ndarray, horizon: int, folds: int, variant: str = "standard",
           phase: np.ndarray | None = None):
    """Rolling origin: refit on everything before each cut, forecast the next `horizon` days.
    Yields (cut, actual, forecast, naive, half-width of the 80% range for the period total)."""
    for f in range(folds, 0, -1):
        cut = len(y) - f * horizon
        if cut < max(MIN_DAYS, 28):
            continue
        test = y[cut:cut + horizon]
        m = _fit_arrays(y[:cut], days[:cut], variant=variant, phase=phase[:cut] if phase is not None else None)
        # same formula as Model.forecast, vectorised over the test days
        cyc = _cycle(m.cycle, phase[cut:cut + horizon]) if phase is not None else 1.0
        pred = np.maximum(0.0, (m.level + m.trend * _DAMP[:len(test)]) * np.array(m.dow)[days[cut:cut + horizon]] * cyc)
        yield cut, test, pred, np.full(len(test), y[:cut][-28:].mean()), RANGE_Z * m.sigma * np.sqrt(len(test))


def score(series: pd.Series, variant: str = "standard", horizon: int = 7, folds: int = 8) -> dict | None:
    """Rolling-origin back-test of one variant: daily and 7-day-total errors (WAPE) for the variant and
    for a naive 28-day average, and how many weeks sold above the forecast's 80% range."""
    if len(series) < MIN_DAYS + horizon * 2:
        return None
    y = series.to_numpy(dtype=float)
    err_m = err_n = week_m = week_n = actual = 0.0
    over = weeks = 0
    for _, test, pred, naive, spread in _folds(y, series.index.day_of_week.to_numpy(), horizon, folds,
                                               variant, _phase(series.index)):
        err_m += np.abs(test - pred).sum()
        err_n += np.abs(test - naive).sum()
        week_m += abs(test.sum() - pred.sum())
        week_n += abs(test.sum() - naive.sum())
        over += int(test.sum() > pred.sum() + spread)
        weeks += 1
        actual += test.sum()
    if actual == 0:
        return None
    return {"wape": float(err_m / actual), "naive_wape": float(err_n / actual), "week_wape": float(week_m / actual),
            "naive_week_wape": float(week_n / actual), "weeks_over": [over, weeks]}


def backtest(series: pd.Series, horizon: int = 7, folds: int = 8, variant: str = "standard") -> tuple[float | None, float | None]:
    """Rolling-origin back-test. Returns (model WAPE, naive 28-day-average WAPE)."""
    r = score(series, variant, horizon, folds)
    return (r["wape"], r["naive_wape"]) if r else (None, None)


def choose_variant(series: pd.Series) -> tuple[str, dict]:
    """Back-test every variant on this product; keep "standard" unless another is clearly better.
    Returns (variant, {variant: score})."""
    scores = {}
    for variant in VARIANTS:
        if variant == "monthly" and len(series) < MONTHLY_MIN_DAYS:
            continue
        r = score(series, variant)
        if r is not None:
            scores[variant] = r
    if "standard" not in scores:
        return "standard", scores
    best = min(scores, key=lambda k: scores[k]["wape"])
    if scores[best]["wape"] < scores["standard"]["wape"] * (1 - SWITCH_MARGIN):
        return best, scores
    return "standard", scores


def backtest_frame(series: pd.Series, horizon: int = 7, folds: int = 8, variant: str = "standard") -> pd.DataFrame:
    """The back-test day by day (date, actual, forecast, naive), for a forecast-vs-actual chart."""
    cols = ["date", "actual", "forecast", "naive"]
    if len(series) < MIN_DAYS + horizon * 2:
        return pd.DataFrame(columns=cols)
    y = series.to_numpy(dtype=float)
    parts = [pd.DataFrame({"date": series.index[cut:cut + len(test)], "actual": test, "forecast": pred, "naive": naive})
             for cut, test, pred, naive, _ in _folds(y, series.index.day_of_week.to_numpy(), horizon, folds,
                                                     variant, _phase(series.index))]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=cols)


def train_all(progress=None) -> pd.DataFrame:
    """Train and back-test a model for every product; persist the parameters."""
    sales = db.sales()
    prods = db.products()
    if sales.empty:
        return pd.DataFrame()
    global_end = sales["date"].max()
    by_sku = dict(tuple(sales.groupby("sku")))     # one pass instead of re-scanning all sales per product
    empty = sales.iloc[0:0]
    use_ml = mlmodel.available() and (global_end - sales["date"].min()).days + 1 >= mlmodel.MIN_HISTORY_DAYS
    share = 0.4 if use_ml else 1.0                 # progress bar: standard engine first, then ML
    calendar = events.load()
    category = prods.set_index("sku")["category"]
    models, series = {}, {}
    for i, sku in enumerate(prods["sku"]):
        s = daily_series(sku, by_sku.get(sku, empty), until=global_end)
        mine = events.for_product(calendar, sku, category.get(sku))
        # fit on the baseline without past event days; back-test on what really sold
        base, effects, kind_effects = events.prepare(s, mine)
        # pick the variant on what really sold, then fit it on the cleaned baseline
        variant, scores = choose_variant(s)
        m = fit(base, sku, variant)
        if variant in scores:
            r = scores[variant]
            m.wape, m.naive_wape = r["wape"], r["naive_wape"]
            m.week_wape, m.naive_week_wape, m.weeks_over = r["week_wape"], r["naive_week_wape"], r["weeks_over"]
            m.variant_wape = {k: v["wape"] for k, v in scores.items()}
        if not mine.empty:
            m.event_effects = effects
            m.event_days = events.future_days(mine, effects, kind_effects, global_end)
            m.ml_event_days = events.future_days(mine[mine["kind"] != "Holiday"], effects, kind_effects, global_end)
        models[sku] = m
        if m.status == "ok":
            series[sku] = s
        if progress:
            progress(share * (i + 1) / len(prods))
    if use_ml:
        try:
            chosen = mlmodel.train_and_select(
                series, lambda sku, y, hist, future: predict_arrays(y, hist, future, models[sku].variant),
                db.get_setting("holiday_country", "SG"),
                progress=(lambda f: progress(share + (1 - share) * f)) if progress else None)
        except Exception:   # ML is an optional upgrade: any failure leaves the standard engine in charge
            chosen = {}
        for sku, info in chosen.items():
            for k, v in info.items():
                setattr(models[sku], k, v)
    with db.connect() as con:
        con.executemany(
            "INSERT INTO models (sku, trained_at, params) VALUES (?, datetime('now','localtime'), ?) "
            "ON CONFLICT(sku) DO UPDATE SET trained_at=excluded.trained_at, params=excluded.params",
            [(sku, json.dumps(asdict(m))) for sku, m in models.items()])
    if progress:
        progress(1.0)
    return pd.DataFrame([asdict(m) for m in models.values()])


def load_models() -> dict[str, Model]:
    df = db.query("SELECT sku, params FROM models")
    return {r.sku: Model(**json.loads(r.params)) for r in df.itertuples()}


def training_summary() -> dict:
    models = load_models()
    if not models:
        return {"trained": 0}
    # volume-weighted, so a few slow sellers do not dominate the headline number
    w = [m for m in models.values() if m.wape is not None]
    vol = sum(m.avg_28 for m in w) or 1.0
    avg = lambda attr: sum((getattr(m, attr) or 0) * m.avg_28 for m in w) / vol if w else None
    weekly = all(m.week_wape is not None for m in w)
    trained_at = db.query("SELECT MAX(trained_at) t FROM models")["t"].iloc[0]
    return {
        "trained": len(models),
        "trained_at": trained_at,
        "insufficient": sum(m.status == "insufficient" for m in models.values()),
        "wape": avg("wape"),
        "naive_wape": avg("naive_wape"),
        "week_wape": avg("week_wape") if weekly else None,
        "naive_week_wape": avg("naive_week_wape") if weekly else None,
        "variants": {k: sum(m.variant == k for m in models.values()) for k in VARIANTS},
        **_ml_summary(models),
    }


def _ml_summary(models: dict[str, "Model"]) -> dict:
    """How many products use ML, and recent error with the chosen models vs the standard engine alone."""
    rec = [m for m in models.values() if m.ml_wape is not None and m.stats_wape_recent is not None]
    if not rec:
        return {"ml_products": 0}
    vol = sum(m.avg_28 for m in rec) or 1.0
    chosen = sum((m.ml_wape if m.method == "ml" else m.stats_wape_recent) * m.avg_28 for m in rec) / vol
    standard = sum(m.stats_wape_recent * m.avg_28 for m in rec) / vol
    return {"ml_products": sum(m.method == "ml" for m in models.values()),
            "recent_wape": chosen, "recent_standard_wape": standard}


def explain(m: Model) -> list[str]:
    """Plain-language evidence for a model, used by recommendations and the AI agent."""
    notes = []
    if m.status == "insufficient":
        return [f"Only {m.n_days} days of history; at least {MIN_DAYS} are needed for reliable advice."]
    peak = int(np.argmax(m.dow))
    low = int(np.argmin(m.dow))
    if m.dow[peak] > 1.12:
        notes.append(f"{DOW_NAMES[peak]} sales run {m.dow[peak]:.2f}x the weekly average (last 8 weeks).")
    if m.dow[low] < 0.88:
        notes.append(f"{DOW_NAMES[low]} is the quietest day at {m.dow[low]:.2f}x average.")
    if m.avg_28 >= 1:          # percentages on tiny volumes are noise ("0.0/day, -100%")
        change = (m.avg_7 - m.avg_28) / m.avg_28
        if abs(change) >= 0.1:
            notes.append(f"Last 7 days averaged {m.avg_7:.1f}/day, {change:+.0%} vs the 28-day average of {m.avg_28:.1f}.")
    if m.method == "ml" and m.ml_wape is not None:
        notes.insert(0, f"Forecast by machine learning (weekday, season, holidays, recent sales): recent error "
                        f"{m.ml_wape:.0%} vs {m.stats_wape_recent:.0%} for the standard model.")
    else:
        month = _cycle(m.cycle, 2 * np.pi * np.arange(30) / 30) - 1      # effect on days 1..30
        if any(m.cycle) and month.max() >= 0.05:
            notes.append(f"Monthly cycle: sales run up to {month.max():.0%} above normal around day "
                         f"{int(month.argmax()) + 1} of the month.")
        if m.variant != "standard" and len(m.variant_wape) > 1:
            notes.append(f"Uses the '{VARIANTS[m.variant]}' method: it was the most accurate on this product's history "
                         f"({m.variant_wape[m.variant]:.1%} vs {m.variant_wape['standard']:.1%} for the standard method).")
        if m.wape is not None and m.naive_wape:
            notes.append(f"Back-test error {m.wape:.0%} vs {m.naive_wape:.0%} for a simple 28-day average.")
    for name, (mult, n) in m.event_effects.items():
        if abs(mult - 1) >= 0.1:
            notes.append(f"{name}: sales ran {mult:.2f}x normal on {n} past day{'s' if n > 1 else ''}.")
    today = pd.Timestamp.today().strftime("%Y-%m-%d")
    for d, (mult, name) in sorted(m.event_days.items())[:20]:
        if d >= today and abs(mult - 1) >= 0.05 and len(notes) < 8:
            notes.append(f"{name} on {pd.Timestamp(d):%a %d %b}: forecast adjusted to {mult:.2f}x.")
            break    # the next event is enough here; the forecast table lists the rest
    return notes or [f"Demand is steady at about {m.level:.1f} units/day."]
