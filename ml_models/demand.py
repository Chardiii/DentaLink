"""Bill of materials: turn job orders into material demand.

A job order says what to make (material, shade, number of units). The lab buys zirconia as
discs, one shade per disc, so zirconia demand is tracked per disc shade. e.max is tracked as
total units (one CAD block or press ingot per unit). Other materials are not forecast yet.

The forecasting models work on weekly units; `units_to_discs` converts units to discs using
the designer's 18-25 units-per-disc yield.
"""
import math

import pandas as pd

from ml_models import lab_profile as lp

SERIES = ["zirconia_A1", "zirconia_A2", "zirconia_A3", "zirconia_OM", "emax"]


def normalize_material(text):
    """Map free-text material from a job order or DentaLink case to a material family."""
    t = str(text or "").lower()
    if "zircon" in t or "sagemax" in t or "pure-z" in t or "lava" in t:
        return "zirconia"
    if "e.max" in t or "emax" in t or "lithium" in t or "disilicate" in t:
        return "emax"
    if "ceramage" in t or "composite" in t:
        return "composite"
    return "other"


def disc_shade(shade):
    """Zirconia disc a prescribed shade is milled from (e.g. B1 -> A1 disc, BL2 -> OM disc)."""
    key = str(shade or "").strip().upper().replace(" ", "")
    return lp.SHADE_TO_DISC.get(key, lp.SHADE_TO_DISC["other"])


def series_for(material, shade):
    """Forecast series a case's units count toward, or None if that material isn't forecast."""
    family = normalize_material(material)
    if family == "zirconia":
        return f"zirconia_{disc_shade(shade)}"
    if family == "emax":
        return "emax"
    return None


def weekly_demand(cases):
    """Weekly units per series from cases with columns: date, material, shade, units.

    Weeks start on Monday. Weeks with no cases are filled with 0 so every series has the
    same, gap-free calendar.
    """
    df = pd.DataFrame(cases).copy()
    df["date"] = pd.to_datetime(df["date"])
    df["series"] = [series_for(m, s) for m, s in zip(df["material"], df["shade"])]
    df = df.dropna(subset=["series"])
    df["week"] = df["date"].dt.to_period("W-SUN").dt.start_time
    table = df.pivot_table(index="week", columns="series", values="units", aggfunc="sum", fill_value=0)
    weeks = pd.date_range(table.index.min(), table.index.max(), freq="W-MON")
    return table.reindex(index=weeks, columns=SERIES, fill_value=0).astype(float)


def units_to_discs(units):
    """(typical, cautious) number of zirconia discs needed for `units` single units.

    Typical uses the middle of the 18-25 yield; cautious assumes the low end, so it is the
    figure to buy against when running out would hold up cases.
    """
    units = max(float(units), 0.0)
    return units / lp.DISC_YIELD_TYPICAL, math.ceil(units / lp.DISC_YIELD_MIN) if units else 0
