"""Backtest the forecasting models and turn the best forecast into a disc purchase plan.

Backtesting replays the past: at each of the last TEST_WEEKS weeks, every model sees only the
weeks before it, forecasts the next 4 weeks, and is scored against what actually happened.

Run:  python -m ml_models.backtest                 (simulated history)
      python -m ml_models.backtest --real FILE.csv (real job orders: lab_in, material, shade, units)
      add --json OUT.json to save the results
"""
import argparse
import json
import math

import numpy as np
import pandas as pd

from ml_models import lab_profile as lp
from ml_models.demand import SERIES, units_to_discs, weekly_demand
from ml_models.models import BASELINES, HORIZON, RandomForestForecaster
from ml_models.simulate import simulate_cases

TEST_WEEKS = 26     # half a year of weekly forecasts
REFIT_EVERY = 4     # retrain the forest monthly, as the lab would


def load_real_cases(path):
    """Job orders transcribed to CSV (needs lab_in, material, shade, units columns)."""
    df = pd.read_csv(path)
    df = df[df["lab_in"].notna()]
    return pd.DataFrame({"date": df["lab_in"], "material": df["material"], "shade": df["shade"], "units": df["units"]})


def backtest(weekly, test_weeks=TEST_WEEKS, seed=7, n_estimators=200):
    values, weeks = weekly.to_numpy(), weekly.index
    n_weeks, n_series = values.shape
    origins = range(n_weeks - test_weeks - HORIZON, n_weeks - HORIZON)
    names = list(BASELINES) + ["Random Forest"]
    # forecasts[name] -> list of (series, origin, forecast[4], actual[4])
    records = {name: [] for name in names}
    forest = None
    for k, t in enumerate(origins):
        if k % REFIT_EVERY == 0:
            forest = RandomForestForecaster(n_estimators=n_estimators, seed=seed).fit(weekly.iloc[: t + 1])
        for s in range(n_series):
            history = values[: t + 1, s]
            actual = values[t + 1: t + 1 + HORIZON, s]
            for name, model in BASELINES.items():
                records[name].append((s, t, model(history), actual))
            records["Random Forest"].append((s, t, forest.predict(history, weeks[t + 1], s, n_series), actual))
    return records, forest


def score(records):
    """Mean absolute error and WAPE, for next week and for the 4-week total."""
    out = {}
    for name, rows in records.items():
        f1 = np.array([r[2][0] for r in rows]); a1 = np.array([r[3][0] for r in rows])
        f4 = np.array([r[2].sum() for r in rows]); a4 = np.array([r[3].sum() for r in rows])
        out[name] = {
            "mae_next_week": float(np.abs(f1 - a1).mean()),
            "wape_next_week": float(np.abs(f1 - a1).sum() / max(a1.sum(), 1e-9)),
            "mae_4_weeks": float(np.abs(f4 - a4).mean()),
            "wape_4_weeks": float(np.abs(f4 - a4).sum() / max(a4.sum(), 1e-9)),
        }
    return out


def purchase_plan(weekly, records, forest, model_name):
    """Next 4 weeks of zirconia per disc shade: units, typical discs, and a cautious buy.

    Uses the model that won the backtest. The cautious buy covers 90% of that model's
    forecast misses in the backtest and assumes the low end of the disc yield, so a
    shortage is unlikely.
    """
    values, n_series = weekly.to_numpy(), weekly.shape[1]
    next_week = weekly.index[-1] + pd.Timedelta(weeks=1)
    plan = []
    for s, name in enumerate(SERIES):
        if model_name == "Random Forest":
            forecast = forest.predict(values[:, s], next_week, s, n_series).sum()
        else:
            forecast = BASELINES[model_name](values[:, s]).sum()
        misses = [r[3].sum() - r[2].sum() for r in records[model_name] if r[0] == s]
        cushion = max(float(np.quantile(misses, 0.9)), 0.0)
        row = {"series": name, "units_next_4_weeks": round(float(forecast), 1),
               "units_90pct": round(float(forecast + cushion), 1)}
        if name.startswith("zirconia"):
            typical, _ = units_to_discs(forecast)
            cautious = math.ceil((forecast + cushion) / lp.DISC_YIELD_MIN)
            price = lp.DISC_PRICE[name.split("_")[1]]
            row.update(discs_typical=round(typical, 1), discs_to_buy=cautious, cost_php=cautious * price)
        plan.append(row)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--real", help="CSV of real job orders instead of simulated history")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", help="write results to this file")
    args = parser.parse_args()

    cases = load_real_cases(args.real) if args.real else simulate_cases(seed=args.seed)
    weekly = weekly_demand(cases)
    source = f"real job orders ({args.real})" if args.real else "simulated history (lab_profile.py)"
    print(f"Data: {source}: {len(cases)} job orders, {len(weekly)} weeks, "
          f"{weekly.index[0].date()} to {weekly.index[-1].date()}")
    if len(weekly) < TEST_WEEKS + HORIZON + 26:
        print(f"Not enough history to backtest: need at least {TEST_WEEKS + HORIZON + 26} weeks.")
        print("Weekly units per series:")
        print(weekly.to_string())
        return

    monthly_units = cases.set_index(pd.to_datetime(cases["date"]))["units"].resample("MS").sum()
    print(f"Average per month: {len(cases) / len(monthly_units):.0f} job orders, {monthly_units.mean():.0f} units")

    records, forest = backtest(weekly, seed=args.seed)
    scores = score(records)
    best = min(scores, key=lambda n: scores[n]["wape_4_weeks"])
    print(f"\nBacktest: {TEST_WEEKS} weekly forecasts x {len(SERIES)} series (lower is better)")
    print(f"{'Model':<24}{'Next week MAE':>15}{'WAPE':>8}{'4-week MAE':>13}{'WAPE':>8}")
    for name, s in sorted(scores.items(), key=lambda kv: kv[1]["wape_4_weeks"]):
        print(f"{name:<24}{s['mae_next_week']:>15.2f}{s['wape_next_week']:>8.1%}{s['mae_4_weeks']:>13.2f}{s['wape_4_weeks']:>8.1%}")
    print(f"Best 4-week forecast: {best}")

    print("\nWhat the Random Forest relies on:")
    for group, value in forest.importances().items():
        print(f"  {group:<38}{value:>6.1%}")

    plan = purchase_plan(weekly, records, forest, best)
    print(f"\nNext 4 weeks (from {weekly.index[-1].date() + pd.Timedelta(weeks=1)}), using {best}:")
    for row in plan:
        line = f"  {row['series']:<13} {row['units_next_4_weeks']:>6.1f} units (90%: {row['units_90pct']:.0f})"
        if "discs_to_buy" in row:
            line += f" -> ~{row['discs_typical']} discs, buy {row['discs_to_buy']} (PHP {row['cost_php']:,})"
        print(line)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"source": source, "scores": scores, "best": best,
                       "importances": forest.importances(), "plan": plan}, f, indent=2)
        print(f"\nSaved {args.json}")


if __name__ == "__main__":
    main()
