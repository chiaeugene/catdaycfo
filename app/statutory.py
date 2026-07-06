"""Malaysian statutory contribution auto-calculation (EPF / SOCSO / EIS).

Percentage-based approximations of the official schedules:
- EPF (KWSP): employee 11%; employer 13% for monthly wages <= RM5,000, else 12%.
  Official rule rounds the wage up to the next bracket; we round contributions
  up to the next ringgit, which matches the schedule closely.
- SOCSO (PERKESO, Category 1): ~1.75% employer / ~0.5% employee,
  wage ceiling RM6,000 (capped at RM104.15 / RM29.75 per official table).
- EIS (SIP): 0.2% each side, same RM6,000 ceiling (cap RM11.90).

These follow the official tables within a few sen — verify against the
current-year schedules for statutory filings.
"""
import math


def calc_statutory(gross: float) -> dict:
    g = max(0.0, gross or 0.0)

    epf_er_rate = 0.13 if g <= 5000 else 0.12
    epf_er = math.ceil(g * epf_er_rate) if g else 0.0
    epf_ee = math.ceil(g * 0.11) if g else 0.0

    capped = min(g, 6000.0)
    socso_er = min(round(capped * 0.0175, 2), 104.15)
    socso_ee = min(round(capped * 0.005, 2), 29.75)
    eis_er = min(round(capped * 0.002, 2), 11.90)
    eis_ee = min(round(capped * 0.002, 2), 11.90)

    return {
        "epf_er": float(epf_er), "epf_ee": float(epf_ee),
        "socso_er": socso_er, "socso_ee": socso_ee,
        "eis_er": eis_er, "eis_ee": eis_ee,
    }
