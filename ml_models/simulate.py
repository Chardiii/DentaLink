"""Simulated job-order history, calibrated to the lab's own figures (see lab_profile.py).

Mrkzter keeps no inventory or job-order history, so the models are trained and tested on a
simulated history that matches what the lab told us and what the 9 photographed job orders
and 2 receipts show. Once DentaLink has collected months of real cases, the same models are
trained on those instead (see backtest.load_real_cases).

The simulation is seeded, so the same seed always gives the same history.
"""
import numpy as np
import pandas as pd

from ml_models import lab_profile as lp


def _pick(rng, weights, size):
    keys = list(weights)
    probs = np.array([weights[k] for k in keys], dtype=float)
    return rng.choice(keys, size=size, p=probs / probs.sum())


def simulate_cases(start="2023-11-01", end="2026-10-31", seed=7):
    """One row per simulated job order: date, material, shade, units."""
    rng = np.random.default_rng(seed)
    days = pd.date_range(start, end, freq="D")

    # Spread ORDERS_PER_MONTH over an average month's working days.
    avg_weekday_weight = sum(lp.WEEKDAY_WEIGHT.values()) / 7
    per_working_day = lp.ORDERS_PER_MONTH / (30.44 * avg_weekday_weight)

    # Each month runs a little above or below its seasonal norm (about +/-10%), the way a
    # real lab has good and bad months for reasons no calendar explains.
    month_noise = {p: rng.gamma(100, 1 / 100) for p in days.to_period("M").unique()}

    rows = []
    for day in days:
        rate = (per_working_day * lp.WEEKDAY_WEIGHT[day.weekday()]
                * lp.MONTH_FACTOR[day.month] * month_noise[day.to_period("M")])
        n = rng.poisson(rate)
        if not n:
            continue
        materials = _pick(rng, lp.MATERIAL_MIX, n)
        units = _pick(rng, lp.UNITS_PER_ORDER, n)
        shades = _pick(rng, lp.ZIRCONIA_SHADE_MIX, n)
        for material, unit_count, shade in zip(materials, units, shades):
            rows.append({
                "date": day,
                "material": material,
                "shade": shade if material == "zirconia" else "",
                "units": int(unit_count),
            })
    return pd.DataFrame(rows)
