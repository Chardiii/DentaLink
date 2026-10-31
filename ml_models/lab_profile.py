"""What we know about Mrkzter Dental Laboratory's workload, and where each number comes from.

Sources:
  LAB      - told to the researchers by the lab (Oct 31, 2026)
  SAMPLE   - 9 photographed job orders received Oct 27-29, 2026
  RECEIPT  - Y2 Dental Supply delivery receipts DR-202644 (Oct 22) and DR-202701 (Oct 30)
  ASSUMED  - not yet confirmed by the lab; change here when it is

Every simulated case and every disc conversion reads from this file, so the forecasting
engine can be re-run as soon as the lab gives a better figure.
"""

# Volume (LAB): "roughly more than 100 job orders" and "300+ units" a month, varying by season.
ORDERS_PER_MONTH = 110
UNITS_PER_MONTH = 330

# Units per job order (SAMPLE): 1, 1, 2, 2, 4, 4, 5, 7, 11 -> mostly 1-4, a few full-arch rehabs.
# The weights average 2.9 units per order, so 110 orders give about 320 units (LAB: 300+).
UNITS_PER_ORDER = {1: 0.32, 2: 0.22, 3: 0.15, 4: 0.13, 5: 0.07, 6: 0.04, 7: 0.03, 8: 0.02, 10: 0.01, 11: 0.01}

# Material share of job orders (SAMPLE: 5 of 9 zirconia, 2 e.max, 1 Ceramage, 1 unstated veneer).
MATERIAL_MIX = {"zirconia": 0.60, "emax": 0.25, "composite": 0.10, "other": 0.05}

# Zirconia disc shades the lab buys (RECEIPT: A1, A2, A3, OM2, OM3) and how often each is
# prescribed (SAMPLE: A2 on 28 of 37 units; ASSUMED split of the rest).
# Prescribed shades without their own disc are milled from the nearest disc and stained.
ZIRCONIA_SHADE_MIX = {"A2": 0.48, "A3": 0.20, "A1": 0.14, "OM": 0.10, "other": 0.08}
SHADE_TO_DISC = {
    "A1": "A1", "B1": "A1",
    "A2": "A2", "B2": "A2", "C1": "A2",
    "A3": "A3", "A3.5": "A3", "B3": "A3", "C2": "A3", "C3": "A3", "D2": "A3", "D3": "A3", "other": "A3",
    "BL1": "OM", "BL2": "OM", "BL3": "OM", "BL4": "OM", "OM1": "OM", "OM2": "OM", "OM3": "OM", "OM": "OM",
}

# Disc yield (LAB, exocad designer's estimate): one 20 mm Sagemax disc fits 18-25 single units.
DISC_YIELD_MIN = 18
DISC_YIELD_MAX = 25
DISC_YIELD_TYPICAL = (DISC_YIELD_MIN + DISC_YIELD_MAX) / 2

# Disc prices in PHP (RECEIPT).
DISC_PRICE = {"A1": 12600, "A2": 12600, "A3": 12600, "OM": 14465}

# Supplier lead time (LAB): Y2 Dental Supply delivers in about 2-4 working days.
# Reorder decisions plan for the slow end, so an order placed today still arrives in time.
LEAD_TIME_WORKING_DAYS_MIN = 2
LEAD_TIME_WORKING_DAYS_MAX = 4

# Working days (ASSUMED): Monday-Saturday, Saturday about half a day, Sunday closed.
WEEKDAY_WEIGHT = {0: 1.0, 1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0, 5: 0.5, 6: 0.0}

# Seasonality (LAB): the "ber" months (September-December) are the busiest, with December the
# peak. The lab can't say when slow months come; they're unpredictable. So January-August share
# one level, and MONTH_NOISE adds month-to-month swings no calendar explains.
# 1.0 = an average month; the twelve factors average about 1.0. The exact sizes are ASSUMED.
MONTH_FACTOR = {
    1: 0.90, 2: 0.90, 3: 0.90, 4: 0.90, 5: 0.90, 6: 0.90, 7: 0.90, 8: 0.90,
    9: 1.05,
    10: 1.10,
    11: 1.20,
    12: 1.35,  # peak
}
# Typical month-to-month swing on top of the season (ASSUMED ~20%, since the lab calls it unpredictable).
MONTH_NOISE = 0.20
