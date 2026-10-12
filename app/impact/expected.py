"""
Визначення очікуваного напрямку руху ціни на основі економічної події.

ВАЖЛИВО: порядок правил критичний!
`get_expected_direction` бере ПЕРШЕ правило, яке матчить title.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


RULES = [
    # =====================================================================
    # USD: зайнятість — СПЕЦИФІЧНІ ПЕРЕД загальними
    # =====================================================================

    # --- ADP Non-Farm Employment (risk-on логіка 2026) ---
    {"pattern": ["adp non-farm", "adp employment"],
     "higher": "up", "lower": "down"},

    # --- Average Hourly Earnings ---
    {"pattern": ["average hourly earnings", "average earnings"],
     "higher": "up", "lower": "down"},

    # --- NFP (справжній, НЕ ADP) ---
    {"pattern": ["non-farm employment change", "nonfarm payrolls",
                 "non-farm payrolls", "nonfarm employment change"],
     "higher": "down", "lower": "up"},

    # --- Загальне employment change ---
    {"pattern": ["employment change"],
     "higher": "up", "lower": "down"},

    # --- Unemployment Claims: більше заявок = dovish = up ---
    {"pattern": ["unemployment claims", "jobless claims", "initial claims"],
     "higher": "up", "lower": "down"},

    # --- Challenger Job Cuts: більше скорочень = dovish = up ---
    {"pattern": ["challenger job cuts"],
     "higher": "up", "lower": "down"},

    # --- JOLTS: більше вакансій = risk-on = up ---
    {"pattern": ["jolts"],
     "higher": "up", "lower": "down"},

    # --- Unemployment rate: вище = dovish = up ---
    {"pattern": ["unemployment rate"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # USD: інфляція — ПЕРЕД GDP
    # =====================================================================

    # GDP Price Index / Deflator (ціновий індекс, НЕ GDP)
    {"pattern": ["gdp price index", "gdp deflator"],
     "higher": "down", "lower": "up"},

    # CPI, PPI, PCE
    {"pattern": ["cpi m/m", "cpi y/y", "core cpi", "ppi m/m", "ppi y/y",
                 "core pce", "pce price index", "trimmed mean cpi",
                 "pce price index m/m", "core pce price index m/m",
                 "core ppi m/m", "core ppi y/y"],
     "higher": "down", "lower": "up"},

    # =====================================================================
    # USD: GDP
    # =====================================================================
    {"pattern": ["gdp"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # USD: споживання
    # =====================================================================
    {"pattern": ["retail sales", "core retail sales"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # Ставки
    # =====================================================================
    {"pattern": ["federal funds rate", "interest rate decision",
                 "cash rate", "bank rate", "official cash rate",
                 "monetary policy decision"],
     "higher": "down", "lower": "up"},

    # =====================================================================
    # ISM / PMI — Manufacturing і Services реагують ПО-РІЗНОМУ
    # =====================================================================

    # ISM Manufacturing: сильні дані → risk-on → up
    {"pattern": ["ism manufacturing pmi", "manufacturing pmi"],
     "higher": "up", "lower": "down"},

    # ISM Services: сильні дані → hawkish Fed → down
    #               слабкі дані → dovish Fed → up
    {"pattern": ["ism services pmi", "services pmi"],
     "higher": "down", "lower": "up"},

    # =====================================================================
    # Consumer Confidence
    # =====================================================================
    {"pattern": ["cb consumer confidence", "consumer confidence"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # Trade balance
    # =====================================================================
    {"pattern": ["trade balance"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # Building permits
    # =====================================================================
    {"pattern": ["building permits", "housing starts"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # Industrial production
    # =====================================================================
    {"pattern": ["industrial production"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # Construction spending
    # =====================================================================
    {"pattern": ["construction spending"],
     "higher": "up", "lower": "down"},

    # =====================================================================
    # Vehicle sales
    # =====================================================================
    {"pattern": ["vehicle sales", "omdia"],
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

    УВАГА: бере ПЕРШЕ правило, яке матчить title.
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
      NO_DATA   — взагалі немає forecast/actual
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