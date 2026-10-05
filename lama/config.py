"""Central configuration, read once from environment variables."""
import os
from dataclasses import dataclass


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default


# Your FreeFireInfoSite deployment is the default name source. Override with PLAYER_LOOKUP_URL
# ({uid} and {region} are replaced). Set PLAYER_LOOKUP_URL=off to disable it entirely.
DEFAULT_LOOKUP_URL = "https://freefireinfosite-sla5.onrender.com/player-info?uid={uid}&region={region}"


@dataclass(frozen=True)
class Config:
    # Storage / security
    database_path: str = "data/lama.sqlite3"
    admin_token: str = ""
    trust_proxy_hops: int = 1
    tz_offset_minutes: int = 345  # Nepal (UTC+05:45), used for daily reports
    persistent_storage: str = "auto"  # auto | true | false (tells the admin page if data survives restarts)
    backup_telegram: bool = True      # send DB backups to Telegram when Telegram alerts are configured
    backup_interval_min: int = 0      # 0 = auto: 1 min on temporary storage, 60 min on persistent storage

    # Rate limits (per IP)
    orders_per_hour: int = 10
    payments_per_hour: int = 20
    checks_per_minute: int = 20

    # Owner alerts (all optional)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ntfy_url: str = ""
    notify_unpaid: bool = False

    # Player name lookup
    player_lookup_url: str = DEFAULT_LOOKUP_URL
    player_lookup_region: str = "BD"
    player_lookup_api_key: str = ""
    player_lookup_timeout: int = 25  # Render free instances can take ~20s to wake up
    player_lookup_strict: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            database_path=os.getenv("DATABASE_PATH", cls.database_path).strip(),
            admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
            trust_proxy_hops=_int("TRUST_PROXY_HOPS", 1),
            tz_offset_minutes=_int("TZ_OFFSET_MINUTES", 345),
            persistent_storage=os.getenv("PERSISTENT_STORAGE", "auto").strip() or "auto",
            backup_telegram=_bool("BACKUP_TELEGRAM", True),
            backup_interval_min=max(0, _int("BACKUP_INTERVAL_MIN", 0)),
            orders_per_hour=_int("ORDERS_PER_HOUR", 10),
            payments_per_hour=_int("PAYMENTS_PER_HOUR", 20),
            checks_per_minute=_int("CHECKS_PER_MINUTE", 20),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            ntfy_url=os.getenv("NTFY_URL", "").strip(),
            notify_unpaid=_bool("NOTIFY_UNPAID", False),
            player_lookup_url=os.getenv("PLAYER_LOOKUP_URL", "").strip() or DEFAULT_LOOKUP_URL,
            player_lookup_region=(os.getenv("PLAYER_LOOKUP_REGION", "BD").strip() or "BD").upper(),
            player_lookup_api_key=os.getenv("PLAYER_LOOKUP_API_KEY", "").strip(),
            player_lookup_timeout=_int("PLAYER_LOOKUP_TIMEOUT", 25),
            player_lookup_strict=_bool("PLAYER_LOOKUP_STRICT", False),
        )

    def validate(self):
        """Fail fast on unsafe settings."""
        if self.admin_token and len(self.admin_token) < 24:
            raise RuntimeError("ADMIN_TOKEN must be at least 24 characters")
