"""
Визначення очікуваного напрямку руху ціни на основі економічної події.

Логіка: для кожної події є правило, яке каже, куди "має" піти ціна
ризикових активів (SOL) при actual > forecast і при actual < forecast.

Консервативна логіка (baseline):
- Інфляція вище прогнозу → hawkish Fed → risk-off → SOL down
- Зайнятість вище прогнозу → сильна економіка → hawkish → SOL down
- GDP вище прогнозу → risk-on → SOL up
- Unemployment вище → dovish → SOL up
- Rate hike → SOL down, cut → up
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Кожне правило: список patterns у title → що робити при higher/lower
RULES = [
    # --- USD: інфляція ---
    {"pattern": ["cpi m/m", "cpi y/y", "core cpi", "ppi m/m", "ppi y/y",
                 "core pce", "pce price index", "trimmed mean cpi"],
     "higher": "down", "lower": "up"},

    # --- USD: зайнятість ---
    {"pattern": ["non-farm", "nonfarm", "adp non-farm", "average hourly earnings",
                 "average earnings", "employment change"],
     "higher": "down", "lower": "up"},

    # --- USD: GDP ---
    {"pattern": ["gdp"],
     "higher": "up", "lower": "down"},

    # --- USD: споживання ---
    {"pattern": ["retail sales"],
     "higher": "up", "lower": "down"},

    # --- USD: безробіття (вище = погано = dovish = up) ---
    {"pattern": ["unemployment rate"],
     "higher": "up", "lower": "down"},

    # --- Ставки ---
    {"pattern": ["federal funds rate", "interest rate decision",
                 "cash rate", "bank rate", "official cash rate",
                 "monetary policy decision"],
     "higher": "down", "lower": "up"},

    # --- ISM / PMI ---
    {"pattern": ["ism manufacturing pmi", "ism services pmi"],
     "higher": "up", "lower": "down"},

    # --- Consumer confidence ---
    {"pattern": ["cb consumer confidence", "consumer confidence"],
     "higher": "up", "lower": "down"},

    # --- JOLTS (більше вакансій = сильніший ринок = hawkish = down) ---
    {"pattern": ["jolts"],
     "higher": "down", "lower": "up"},

    # --- Trade balance (менший дефіцит = сильніше = risk-on) ---
    {"pattern": ["trade balance"],
     "higher": "up", "lower": "down"},

    # --- Building permits / housing (сильніше = risk-on) ---
    {"pattern": ["building permits", "housing starts"],
     "higher": "up", "lower": "down"},

    # --- Industrial production ---
    {"pattern": ["industrial production"],
     "higher": "up", "lower": "down"},
]


def get_expected_direction(title: str,
                            forecast: Optional[str],
                            actual: Optional[str]) -> Optional[str]:
    """
    Повертає:
      - 'up' / 'down' — очікуваний напрямок
      - 'flat' — якщо actual == forecast
      - None — якщо немає даних або правило не знайдено
    """
    if not forecast or not actual:
        return None

    f = _parse_number(forecast)
    a = _parse_number(actual)
    if f is None or a is None:
        return None

    if a == f:
        return "flat"

    t = title.lower()
    rule = None
    for r in RULES:
        if any(p in t for p in r["pattern"]):
            rule = r
            break

    if rule is None:
        return None

    if a > f:
        return rule["higher"]
    else:
        return rule["lower"]


def _parse_number(s: str) -> Optional[float]:
    """
    '0.3%' → 0.3
    '250K' → 250000
    '-1.2' → -1.2
    '1.5B' → 1.5e9
    """
    if not s:
        return None

    s = s.strip().replace("%", "").replace(",", "").replace(" ", "").upper()
    if s in ("", "-", "N/A", "NA"):
        return None

    multiplier = 1
    if s.endswith("K"):
        multiplier = 1_000
        s = s[:-1]
    elif s.endswith("M"):
        multiplier = 1_000_000
        s = s[:-1]
    elif s.endswith("B"):
        multiplier = 1_000_000_000
        s = s[:-1]

    try:
        return float(s) * multiplier
    except ValueError:
        return None


def classify_hit(expected: Optional[str], actual: Optional[str]) -> str:
    """
    Повертає:
      HIT      — напрямок справдився
      MISS     — не справдився
      NEUTRAL  — хтось із них 'flat'
      N/A      — немає expected або actual
    """
    if expected is None or actual is None:
        return "N/A"
    if expected == "flat" or actual == "flat":
        return "NEUTRAL"
    if expected == actual:
        return "HIT"
    return "MISS"