"""Machine-learning forecaster, used as an automatic upgrade over the standard engine.

One gradient-boosting model is shared by all products (sales are scaled by each product's
recent average, so slow and fast sellers can learn from each other). Inputs: weekday, season,
public holidays and lagged sales. A product switches to this model only when the shop has
enough history and a back-test on the most recent weeks shows it beats the standard engine
for that product; otherwise the standard engine stays in charge.

Validated on real bakery data (dev-docs/ml-weather-experiment): with 21 months of history,
error fell from 36% to 32%; with 6 months, ML was worse, hence the history requirement.
"""
import numpy as np
import pandas as pd

MIN_HISTORY_DAYS = 365      # ML overfits on short histories (6 months tested worse than the standard engine)
HORIZON = 7                 # back-test horizon
FOLDS = 4                   # recent weekly folds used to choose between the models
PATH_DAYS = 45              # forecast days stored per product (covers plan start + 30-day forecast page)
TRAIN_DAYS = 400            # most recent days used for training
MAX_ROWS = 600_000          # cap training rows on very large catalogues
MARGIN = 0.97               # ML must beat the standard engine by >= 3% to be used
LAGS = (7, 14, 21, 28)      # all >= 7 days, so the first week needs no predicted inputs
WARMUP = 35                 # days of history needed before a row has every feature
MAX_CATEGORIES = 255        # scikit-learn limit for a categorical product feature
ITERATIONS = 100            # boosting rounds per fit (100 @ 0.15 matched 300 @ 0.05 on the bakery data, ~3x faster)
LEARNING_RATE = 0.15


def available() -> bool:
    try:
        import sklearn  # noqa: F401  (availability check only)
        import holidays  # noqa: F401
        return True
    except ImportError:
        return False


def _holiday_flags(dates: pd.DatetimeIndex, country: str) -> tuple[np.ndarray, np.ndarray]:
    import holidays
    try:
        cal = holidays.country_holidays(country, years=range(dates.min().year, dates.max().year + 1))
    except (KeyError, NotImplementedError):
        return np.zeros(len(dates)), np.zeros(len(dates))
    hd = pd.DatetimeIndex(list(cal.keys()))
    return dates.isin(hd).astype(float), (dates + pd.Timedelta(days=1)).isin(hd).astype(float)


class Panel:
    """Products x days matrix of sales, with room for future days."""

    def __init__(self, series: dict[str, pd.Series], country: str, extra_days: int = 0):
        self.skus = list(series)
        end = max(s.index.max() for s in series.values())
        start = max(min(s.index.min() for s in series.values()), end - pd.Timedelta(days=TRAIN_DAYS + WARMUP + 60))
        self.n_obs = (end - start).days + 1
        self.dates = pd.date_range(start, periods=self.n_obs + extra_days, freq="D")
        self.Y = np.zeros((len(self.skus), len(self.dates)))
        for i, sku in enumerate(self.skus):
            s = series[sku].reindex(self.dates[:self.n_obs], fill_value=0.0)
            self.Y[i, :self.n_obs] = s.to_numpy(dtype=float)
        shop = self.Y[:, :self.n_obs].sum(axis=0)
        self.open = np.ones(len(self.dates), dtype=bool)
        self.open[:self.n_obs] = shop > 0
        dow = self.dates.day_of_week.to_numpy()
        # weekdays the shop is usually closed (zero sales in >= 75% of the last 8 weeks) are forecast as 0
        recent = slice(max(0, self.n_obs - 56), self.n_obs)
        self.closed_dow = {d for d in range(7)
                           if (m := (dow[recent] == d)).any() and (~self.open[recent][m]).mean() >= 0.75}
        self.dow = dow
        self.month = self.dates.month.to_numpy()
        doy = self.dates.dayofyear.to_numpy()
        self.doy_sin, self.doy_cos = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
        self.hol, self.pre_hol = _holiday_flags(self.dates, country)
        # small catalogues learn a per-product effect; large ones describe products by sales volume instead
        self.use_product_id = len(self.skus) <= MAX_CATEGORIES

    def features(self, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Feature rows for every product at day indices t (all >= WARMUP). Returns (X, scale)."""
        P = len(self.skus)
        cs = np.concatenate([np.zeros((P, 1)), np.cumsum(self.Y, axis=1)], axis=1)
        mean7 = (cs[:, t - 6] - cs[:, t - 13]) / 7            # days t-13 .. t-7
        mean28 = (cs[:, t - 6] - cs[:, t - 34]) / 28          # days t-34 .. t-7
        scale = np.maximum(mean28, 0.5)
        cols = [self.Y[:, t - lag] / scale for lag in LAGS] + [mean7 / scale]
        tile = lambda a: np.broadcast_to(a[t], (P, len(t)))
        cols += [tile(self.dow), tile(self.month), tile(self.doy_sin), tile(self.doy_cos),
                 tile(self.hol), tile(self.pre_hol), np.log1p(mean28)]
        if self.use_product_id:
            cols.append(np.broadcast_to(np.arange(P)[:, None], (P, len(t))))
        X = np.stack([c.astype(float) for c in cols], axis=-1)   # P x len(t) x features
        return X, scale

    def train(self, cut: int, seed: int = 0):
        """Fit on open days before day index `cut`."""
        from sklearn.ensemble import HistGradientBoostingRegressor
        t = np.arange(max(WARMUP, cut - TRAIN_DAYS), cut)
        t = t[self.open[t]]
        X, scale = self.features(t)
        y = self.Y[:, t] / scale
        X, y = X.reshape(-1, X.shape[-1]), y.reshape(-1)
        if len(y) > MAX_ROWS:
            keep = np.random.default_rng(seed).choice(len(y), MAX_ROWS, replace=False)
            X, y = X[keep], y[keep]
        model = HistGradientBoostingRegressor(max_iter=ITERATIONS, learning_rate=LEARNING_RATE, max_leaf_nodes=31,
                                              min_samples_leaf=20,
                                              categorical_features=[X.shape[1] - 1] if self.use_product_id else None,
                                              random_state=seed)
        return model.fit(X, y)

    def predict(self, model, start: int, days: int) -> np.ndarray:
        """Forecast days start .. start+days-1 day by day; later days use earlier predictions as inputs."""
        Y_saved = self.Y.copy()
        out = np.zeros((len(self.skus), days))
        for k in range(days):
            t = start + k
            if self.dow[t] in self.closed_dow:
                pred = np.zeros(len(self.skus))
            else:
                X, scale = self.features(np.array([t]))
                pred = np.maximum(0.0, model.predict(X[:, 0, :]) * scale[:, 0])
            out[:, k] = pred
            self.Y[:, t] = pred
        self.Y = Y_saved
        return out


def train_and_select(series: dict[str, pd.Series], stats_predict, country: str, progress=None) -> dict[str, dict]:
    """Back-test ML against the standard engine on recent folds and forecast with ML where it wins.

    `stats_predict(sku, y, hist_dates, future_dates)` must return the standard engine's forecast for
    `future_dates` from past values `y` (engine.predict_arrays, with the product's chosen variant).
    Returns {sku: {"method", "ml_wape", "stats_wape_recent", "ml_start", "ml_path"}} (empty if ML not used).
    """
    if not available() or not series:
        return {}
    span = (max(s.index.max() for s in series.values()) - min(s.index.min() for s in series.values())).days + 1
    if span < MIN_HISTORY_DAYS:
        return {}
    panel = Panel(series, country)
    T = panel.n_obs
    P = len(panel.skus)
    err_ml, err_st, actual = np.zeros(P), np.zeros(P), np.zeros(P)
    steps = FOLDS + 1
    for f, k in enumerate(range(FOLDS, 0, -1)):
        cut = T - k * HORIZON
        model = panel.train(cut, seed=f)
        pred_ml = panel.predict(model, cut, HORIZON)
        open_ = panel.open[cut:cut + HORIZON]
        y = panel.Y[:, cut:cut + HORIZON]
        for i, sku in enumerate(panel.skus):
            st = stats_predict(sku, panel.Y[i, :cut], panel.dates[:cut], panel.dates[cut:cut + HORIZON])
            err_st[i] += np.abs(y[i, open_] - st[open_]).sum()
        err_ml += np.abs(y[:, open_] - pred_ml[:, open_]).sum(axis=1)
        actual += y[:, open_].sum(axis=1)
        if progress:
            progress((f + 1) / steps)
    if err_ml.sum() >= err_st.sum():               # ML does not help this shop overall: keep the standard engine
        return {}
    # final model on all data, forecasting from the day after the last sale
    future = Panel(series, country, extra_days=PATH_DAYS)
    model = future.train(T)
    path = future.predict(model, T, PATH_DAYS)
    start = future.dates[T].strftime("%Y-%m-%d")
    if progress:
        progress(1.0)
    out = {}
    for i, sku in enumerate(panel.skus):
        if actual[i] <= 0:
            continue
        ml_w, st_w = err_ml[i] / actual[i], err_st[i] / actual[i]
        use = err_ml[i] < MARGIN * err_st[i]
        out[sku] = {"method": "ml" if use else "stats", "ml_wape": float(ml_w), "stats_wape_recent": float(st_w),
                    "ml_start": start if use else None,
                    "ml_path": [round(float(v), 3) for v in path[i]] if use else None}
    return out
