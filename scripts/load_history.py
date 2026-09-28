"""
Разове завантаження історичних свічок SOLUSDT з Bybit.

Використання:
    python scripts/load_history.py         # 30 днів за замовчуванням
    python scripts/load_history.py 7
    python scripts/load_history.py 90

Логіка винесена в app/prices/history.py — щоб той самий код
використовувався і зі scheduler всередині FastAPI.
"""

import sys
import asyncio
import logging
from pathlib import Path

# --- Підняти корінь проєкту в sys.path ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import settings
from app.db import init_db
from app.prices.history import load_history_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    # Гарантуємо, що схема БД існує
    asyncio.run(init_db())

    logger.info(f"Старт. DB = {settings.db_path_abs}")
    logger.info(f"Завантажуємо історію SOLUSDT за {days} днів...")

    total = load_history_sync(days=days, interval="1")

    logger.info(f"✅ Готово. Всього збережено: {total} свічок.")