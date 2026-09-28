from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator
from pathlib import Path


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        str_strip_whitespace=True,
    )

    bybit_symbol: str = "SOLUSDT"
    bybit_kline_interval: int = 1
    tg_bot_token: str = ""
    calendar_importance: list[str] = ["high", "medium"]
    calendar_currencies: list[str] = ["USD"]
    calendar_refresh_hours: int = 6
    db_path: str = "data/calendar.sqlite"

    # Web
    host: str = "127.0.0.1"
    port: int = 5085   # ← БУЛО 8000

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