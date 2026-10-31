"""Forecasting models for weekly material demand.

Every model answers the same question: given a series' weekly units up to now, how many
units will be needed in each of the next HORIZON weeks? The baselines are the simple methods
a lab could do by hand; the Random Forest has to beat them to be worth using.
"""
import numpy as np
from sklearn.ensemble import RandomForestRegressor

HORIZON = 4  # weeks; the lab can plan a month of disc purchases at a time


# ---------------------------------------------------------------- baselines

def naive(history):
    """Next weeks = last week."""
    return np.repeat(history[-1], HORIZON)


def moving_average(history, window=4):
    """Next weeks = average of the last `window` weeks."""
    return np.repeat(history[-window:].mean(), HORIZON)


def exponential_smoothing(history, alpha=0.3):
    """Simple exponential smoothing: recent weeks count more, older weeks fade out."""
    level = history[0]
    for value in history[1:]:
        level = alpha * value + (1 - alpha) * level
    return np.repeat(level, HORIZON)


def croston(history, alpha=0.1):
    """Croston's method for intermittent demand (weeks with zero use, like the OM shade).

    Smooths the size of non-zero weeks and the gap between them separately; the forecast is
    size / gap.
    """
    nonzero = np.flatnonzero(history > 0)
    if len(nonzero) == 0:
        return np.zeros(HORIZON)
    size, gap, last = history[nonzero[0]], nonzero[0] + 1, nonzero[0]
    for i in nonzero[1:]:
        size = alpha * history[i] + (1 - alpha) * size
        gap = alpha * (i - last) + (1 - alpha) * gap
        last = i
    return np.repeat(size / gap, HORIZON)


BASELINES = {
    "Naive": naive,
    "Moving average (4 wk)": moving_average,
    "Exponential smoothing": exponential_smoothing,
    "Exponential smoothing (slow)": lambda h: exponential_smoothing(h, alpha=0.1),
    "Croston": croston,
}


# ---------------------------------------------------------------- Random Forest

LAGS = 8


def _features(history, week_start, series_index, n_series):
    """Features describing a series at one point in time, to predict the weeks after it.

    Values are divided by the series' recent average so one model can learn from every
    shade at once (A2 sells far more than OM, but their ups and downs look alike).
    """
    scale = max(history[-13:].mean(), 1e-6)
    recent = history[-LAGS:] / scale
    week = week_start.isocalendar().week
    month = week_start.month
    one_hot = np.zeros(n_series)
    one_hot[series_index] = 1
    year_ago = history[-52] / scale if len(history) >= 52 else 1.0
    return np.concatenate([
        recent,
        [history[-4:].mean() / scale, history[-8:].mean() / scale, history[-26:].mean() / scale,
         history[-8:].std() / scale],
        [np.sin(2 * np.pi * week / 52), np.cos(2 * np.pi * week / 52)],
        [np.sin(2 * np.pi * month / 12), np.cos(2 * np.pi * month / 12)],
        [year_ago],
        one_hot,
    ]), scale


class RandomForestForecaster:
    """One Random Forest per horizon week, shared across all series ("global" model)."""

    def __init__(self, n_estimators=200, seed=7):
        self.n_estimators = n_estimators
        self.seed = seed
        self.models = []

    def fit(self, weekly):
        """Train on a weekly table (rows = weeks, columns = series)."""
        values, weeks = weekly.to_numpy(), weekly.index
        n_series = values.shape[1]
        X, Y = [], []
        for t in range(13, len(values) - HORIZON):
            next_week = weeks[t + 1]
            for s in range(n_series):
                x, scale = _features(values[: t + 1, s], next_week, s, n_series)
                X.append(x)
                Y.append(values[t + 1: t + 1 + HORIZON, s] / scale)
        X, Y = np.array(X), np.array(Y)
        self.models = [
            # Larger leaves and a feature subset keep the forest from memorising noise:
            # with a few hundred training weeks, weekly demand is mostly random variation.
            RandomForestRegressor(n_estimators=self.n_estimators, min_samples_leaf=20, max_features=0.5,
                                  random_state=self.seed + h).fit(X, Y[:, h])
            for h in range(HORIZON)
        ]
        return self

    def predict(self, history, next_week, series_index, n_series):
        x, scale = _features(history, next_week, series_index, n_series)
        return np.array([max(m.predict(x[None, :])[0], 0.0) * scale for m in self.models])

    def importances(self):
        """Average feature importance across horizons, grouped into readable names."""
        imp = np.mean([m.feature_importances_ for m in self.models], axis=0)
        groups = {
            "Last 8 weeks (each week)": imp[:LAGS].sum(),
            "4-, 8- and 26-week averages, variability": imp[LAGS:LAGS + 4].sum(),
            "Week of the year": imp[LAGS + 4:LAGS + 6].sum(),
            "Month": imp[LAGS + 6:LAGS + 8].sum(),
            "Same week last year": imp[LAGS + 8],
            "Which material/shade": imp[LAGS + 9:].sum(),
        }
        return dict(sorted(groups.items(), key=lambda kv: -kv[1]))
