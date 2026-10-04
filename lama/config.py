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


@dataclass(frozen=True)
class Config:
    # Supplier (GoXtop)
    goxtop_base_url: str = "https://goxtop.com/api/v.1"
    goxtop_api_key: str = ""
    goxtop_create_path: str = "/create"
    goxtop_timeout: int = 20
    freefire_game_code: str = "freefire_global"
    efootball_game_code: str = "efootball"
    dry_run: bool = True

    # Storage / security
    database_path: str = "data/lama.sqlite3"
    admin_token: str = ""
    trust_proxy_hops: int = 1
    tz_offset_minutes: int = 345  # Nepal (UTC+05:45), used for daily reports

    # Rate limits (per IP)
    orders_per_hour: int = 10
    payments_per_hour: int = 20
    checks_per_minute: int = 20

    # Player name lookup
    player_lookup_url: str = ""
    player_lookup_region: str = "BD"
    player_lookup_api_key: str = ""
    player_lookup_timeout: int = 15
    player_lookup_strict: bool = False

    @classmethod
    def from_env(cls):
        return cls(
            goxtop_base_url=os.getenv("GOXTOP_BASE_URL", cls.goxtop_base_url).strip().rstrip("/"),
            goxtop_api_key=os.getenv("GOXTOP_API_KEY", "").strip(),
            goxtop_create_path=os.getenv("GOXTOP_CREATE_PATH", cls.goxtop_create_path).strip(),
            goxtop_timeout=_int("GOXTOP_TIMEOUT", 20),
            freefire_game_code=os.getenv("GOXTOP_FREEFIRE_GAME_CODE", cls.freefire_game_code).strip(),
            efootball_game_code=os.getenv("GOXTOP_EFOOTBALL_GAME_CODE", cls.efootball_game_code).strip(),
            dry_run=_bool("DRY_RUN", True),
            database_path=os.getenv("DATABASE_PATH", cls.database_path).strip(),
            admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
            trust_proxy_hops=_int("TRUST_PROXY_HOPS", 1),
            tz_offset_minutes=_int("TZ_OFFSET_MINUTES", 345),
            orders_per_hour=_int("ORDERS_PER_HOUR", 10),
            payments_per_hour=_int("PAYMENTS_PER_HOUR", 20),
            checks_per_minute=_int("CHECKS_PER_MINUTE", 20),
            player_lookup_url=os.getenv("PLAYER_LOOKUP_URL", "").strip(),
            player_lookup_region=(os.getenv("PLAYER_LOOKUP_REGION", "BD").strip() or "BD").upper(),
            player_lookup_api_key=os.getenv("PLAYER_LOOKUP_API_KEY", "").strip(),
            player_lookup_timeout=_int("PLAYER_LOOKUP_TIMEOUT", 15),
            player_lookup_strict=_bool("PLAYER_LOOKUP_STRICT", False),
        )

    def validate(self):
        """Fail fast on unsafe production settings."""
        if not self.dry_run:
            missing = [n for n, v in (("GOXTOP_API_KEY", self.goxtop_api_key),
                                      ("ADMIN_TOKEN", self.admin_token)) if not v]
            if missing:
                raise RuntimeError("DRY_RUN=false requires: " + ", ".join(missing))
        if self.admin_token and len(self.admin_token) < 24:
            raise RuntimeError("ADMIN_TOKEN must be at least 24 characters")
