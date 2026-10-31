"""One forecast run for the Forecast Analytics page.

`run_forecast` backtests every model on the history, keeps the winner, and forecasts the next
4 weeks for each zirconia disc shade and for e.max. The result is plain JSON, so the app can
store it and render it later without re-running the models (a run takes about a minute).

`stock_plan` compares a stored forecast with what is on the shelf right now, so the page's
order advice follows the inventory without re-running anything.
"""
import math
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ml_models import lab_profile as lp
from ml_models.demand import SERIES, weekly_demand
from ml_models.simulate import simulate_cases

# Kept in step with backtest.TEST_WEEKS and models.HORIZON; those modules load scikit-learn,
# so they're imported only when a run starts, not every time the page opens.
TEST_WEEKS, HORIZON = 26, 4
MIN_REAL_WEEKS = TEST_WEEKS + HORIZON + 26  # a year of history before real cases can be backtested
HISTORY_WEEKS_SHOWN = 26

SERIES_LABELS = {
    "zirconia_A1": "Zirconia disc · A1",
    "zirconia_A2": "Zirconia disc · A2",
    "zirconia_A3": "Zirconia disc · A3",
    "zirconia_OM": "Zirconia disc · OM (bleach)",
    "emax": "e.max (blocks / ingots)",
}

# Inventory names that count as each series' stock (matched case-insensitively).
STOCK_KEYWORDS = {
    "zirconia": ("zirconia", "sagemax", "zr disc", "zirkon"),
    "emax": ("e.max", "emax", "lithium disilicate"),
}
SHADE_TOKENS = {"A1": ("a1", "b1"), "A2": ("a2",), "A3": ("a3",), "OM": ("om", "bl")}


def week_start(day):
    return day - timedelta(days=day.weekday())


def run_forecast(cases=None, source="simulated", today=None, n_estimators=150, seed=7):
    """Backtest, choose the best model, and forecast the next 4 weeks.

    cases: DataFrame of real job orders (date, material, shade, units) when source is "real";
           ignored for "simulated", which builds a history from lab_profile.py.
    """
    from ml_models.backtest import backtest, score
    from ml_models.models import BASELINES, RandomForestForecaster

    started = time.perf_counter()
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    this_week = week_start(today)
    last_full_day = this_week - timedelta(days=1)  # forecasts start this Monday

    if source == "simulated":
        cases = simulate_cases(end=last_full_day.isoformat(), seed=seed)
    cases = pd.DataFrame(cases)
    cases = cases[pd.to_datetime(cases["date"]).dt.date <= last_full_day]
    weekly = weekly_demand(cases)
    weekly = weekly.reindex(pd.date_range(weekly.index[0], pd.Timestamp(this_week) - pd.Timedelta(weeks=1), freq="W-MON"),
                            fill_value=0.0)
    if len(weekly) < MIN_REAL_WEEKS:
        raise ValueError(f"Need at least {MIN_REAL_WEEKS} weeks of history; have {len(weekly)}.")

    records, forest = backtest(weekly, seed=seed, n_estimators=n_estimators)
    scores = score(records)
    best = min(scores, key=lambda n: scores[n]["wape_4_weeks"])

    # Final forecast: refit the forest on everything when it won.
    if best == "Random Forest":
        forest = RandomForestForecaster(n_estimators=n_estimators, seed=seed).fit(weekly)
    values = weekly.to_numpy()
    future_weeks = [this_week + timedelta(weeks=h) for h in range(HORIZON)]

    series = []
    for s, key in enumerate(SERIES):
        history = values[:, s]
        if best == "Random Forest":
            forecast = forest.predict(history, pd.Timestamp(this_week), s, len(SERIES))
        else:
            forecast = BASELINES[best](history)
        forecast = np.maximum(forecast, 0)
        misses = [r[3].sum() - r[2].sum() for r in records[best] if r[0] == s]
        cushion = max(float(np.quantile(misses, 0.9)), 0.0)
        week1_misses = [r[3][0] - r[2][0] for r in records[best] if r[0] == s]
        week1_cushion = max(float(np.quantile(week1_misses, 0.9)), 0.0)
        units_4w = float(forecast.sum())
        units_90 = units_4w + cushion
        # Lead time: an order placed now arrives within 4 working days (~0.7 of a 5.5-day week).
        lead_weeks = lp.LEAD_TIME_WORKING_DAYS_MAX / 5.5
        lead_units = (forecast[0] + week1_cushion) * (1 + lead_weeks)
        is_disc = key.startswith("zirconia")
        per_item = lp.DISC_YIELD_MIN if is_disc else 1
        entry = {
            "key": key,
            "label": SERIES_LABELS[key],
            "kind": "disc" if is_disc else "unit",
            "history": [{"week": str(w.date()), "units": float(v)}
                        for w, v in zip(weekly.index[-HISTORY_WEEKS_SHOWN:], history[-HISTORY_WEEKS_SHOWN:])],
            "forecast": [{"week": w.isoformat(), "units": round(float(v), 1)} for w, v in zip(future_weeks, forecast)],
            "units_4w": round(units_4w, 1),
            "units_90": round(units_90, 1),
            "avg_weekly_recent": round(float(history[-8:].mean()), 1),
            "need_4w": math.ceil(units_90 / per_item - 1e-9) if units_90 > 0 else 0,
            "reorder_point": math.ceil(lead_units / per_item - 1e-9) if lead_units > 0 else 0,
        }
        if is_disc:
            shade = key.split("_")[1]
            entry.update(
                discs_typical=round(units_4w / lp.DISC_YIELD_TYPICAL, 1),
                price=lp.DISC_PRICE[shade],
                cost_4w=entry["need_4w"] * lp.DISC_PRICE[shade],
            )
        series.append(entry)

    monthly = cases.assign(date=pd.to_datetime(cases["date"])).set_index("date")["units"].resample("MS").agg(["count", "sum"])
    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "forecast_start": this_week.isoformat(),
        "history_start": str(weekly.index[0].date()),
        "history_end": str(weekly.index[-1].date()),
        "history_weeks": len(weekly),
        "orders_per_month": round(float(monthly["count"].mean()), 0),
        "units_per_month": round(float(monthly["sum"].mean()), 0),
        "best_model": best,
        "scores": {name: {k: round(v, 4) for k, v in s.items()} for name, s in scores.items()},
        "test_weeks": TEST_WEEKS,
        "importances": {k: round(float(v), 4) for k, v in forest.importances().items()} if forest else {},
        "series": series,
        "assumptions": profile_summary(),
        "seconds": round(time.perf_counter() - started, 1),
    }


def profile_summary():
    """The lab figures behind a run, with their sources, for the page's 'What this is based on'."""
    busy = ", ".join(date(2026, m, 1).strftime("%b") for m, f in lp.MONTH_FACTOR.items() if f > 1)
    return [
        {"label": "Job orders a month", "value": f"about {lp.ORDERS_PER_MONTH}", "source": "Lab"},
        {"label": "Units a month", "value": "300+", "source": "Lab"},
        {"label": "Busiest months", "value": f"{busy} (December peak)", "source": "Lab"},
        {"label": "Units per disc", "value": f"{lp.DISC_YIELD_MIN}–{lp.DISC_YIELD_MAX}", "source": "Lab (exocad designer)"},
        {"label": "Supplier delivery", "value": f"{lp.LEAD_TIME_WORKING_DAYS_MIN}–{lp.LEAD_TIME_WORKING_DAYS_MAX} working days", "source": "Lab"},
        {"label": "Material mix", "value": "zirconia 60%, e.max 25%, other 15%", "source": "9 job orders"},
        {"label": "Most prescribed shade", "value": "A2 (about half of zirconia)", "source": "9 job orders"},
        {"label": "Disc prices", "value": "₱12,600 (A1–A3), ₱14,465 (OM)", "source": "2 supplier receipts"},
        {"label": "Size of seasonal swings", "value": f"±{int(lp.MONTH_NOISE * 100)}% month to month", "source": "Assumed"},
    ]


def _stock_for(key, inventory):
    """Total on-hand quantity of inventory items that belong to a series, or None if none match."""
    family = "zirconia" if key.startswith("zirconia") else "emax"
    shade = key.split("_")[1].lower() if family == "zirconia" else None
    matched = []
    for item in inventory:
        name = str(item.get("item_name") or "").lower()
        if not any(word in name for word in STOCK_KEYWORDS[family]):
            continue
        # "Sagemax" contains "emax": a zirconia disc is never e.max stock.
        if family == "emax" and any(word in name for word in STOCK_KEYWORDS["zirconia"]):
            continue
        if shade:
            tokens = name.replace("-", " ").replace("/", " ").split()
            if not any(tok.startswith(t) for tok in tokens for t in SHADE_TOKENS[shade.upper()]):
                continue
        matched.append(item)
    if not matched:
        return None, []
    return sum(float(i.get("quantity") or 0) for i in matched), [i.get("item_name") for i in matched]


def stock_plan(result, inventory):
    """Order advice per series from a stored forecast and the current inventory.

    status: "order_now"  - stock won't last until a new order arrives
            "order_soon" - enough for now, not for the next 4 weeks
            "enough"     - covers the next 4 weeks
            "untracked"  - no matching item in Inventory
    """
    rows = []
    for s in result["series"]:
        on_hand, items = _stock_for(s["key"], inventory)
        row = dict(s, on_hand=on_hand, stock_items=items)
        if on_hand is None:
            row.update(status="untracked", order_qty=s["need_4w"])
        elif on_hand < s["reorder_point"]:
            row.update(status="order_now", order_qty=max(s["need_4w"] - math.floor(on_hand), 1))
        elif on_hand < s["need_4w"]:
            row.update(status="order_soon", order_qty=s["need_4w"] - math.floor(on_hand))
        else:
            row.update(status="enough", order_qty=0)
        if s["kind"] == "disc":
            row["order_cost"] = row["order_qty"] * s["price"]
        rows.append(row)
    return rows
