"""
Визначення очікуваного напрямку руху ціни на основі економічної події.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


RULES = [
    # --- USD: інфляція ---
    {"pattern": ["cpi m/m", "cpi y/y", "core cpi", "ppi m/m", "ppi y/y",
                 "core pce", "pce price index", "trimmed mean cpi",
                 "pce price index m/m", "core pce price index m/m"],
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

    # --- USD: безробіття ---
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

    # --- JOLTS ---
    {"pattern": ["jolts"],
     "higher": "down", "lower": "up"},

    # --- Trade balance ---
    {"pattern": ["trade balance"],
     "higher": "up", "lower": "down"},

    # --- Building permits / housing ---
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
    """'0.3%' → 0.3, '250K' → 250000, '-1.2' → -1.2"""
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


def classify_hit(expected: Optional[str], actual: Optional[str],
                 has_data: bool = True) -> str:
    """
    Повертає:
      HIT       — напрямок справдився
      MISS      — не справдився
      NEUTRAL   — хтось із них 'flat'
      N/A       — немає expected або actual
      NO_DATA   — взагалі немає forecast/actual (наприклад, виступи)
    """
    if not has_data:
        return "NO_DATA"
    if expected is None or actual is None:
        return "N/A"
    if expected == "flat" or actual == "flat":
        return "NEUTRAL"
    if expected == actual:
        return "HIT"
    return "MISS"