from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        str_strip_whitespace=True,
    )

    # Bybit
    bybit_symbol: str = "SOLUSDT"
    bybit_kline_interval: int = 1

    # Bybit API (для LIVE)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = False

    # LIVE trading
    live_enabled: bool = False
    live_trade_size_usd: float = 5.0
    live_max_pct_of_balance: float = 10.0
    live_auto_close: bool = True
    live_exit_offset_min: int = 60
    live_stop_loss_pct: float = 5.0

    # Telegram
    tg_bot_token: str = ""
    tg_chat_id: int = 0

    # Calendar
    calendar_importance: list[str] = ["high", "medium"]
    calendar_currencies: list[str] = ["USD"]
    calendar_refresh_hours: int = 6

    # DB
    db_path: str = "data/calendar.sqlite"

    # Web
    host: str = "127.0.0.1"
    port: int = 5085

    @field_validator("tg_bot_token")
    @classmethod
    def clean_token(cls, v: str) -> str:
        return v.strip().strip('"').strip("'")

    @property
    def db_path_abs(self) -> str:
        p = Path(self.db_path)
        if not p.is_absolute():
            root = Path(__file__).resolve().parent.parent
            p = root / p
        return str(p.resolve())


settings = Settings()