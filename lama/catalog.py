"""Server-side catalog. Prices live here, never in the browser's request."""
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    id: str
    name: str
    price: int            # NPR
    kind: str             # diamonds | membership | coins | pack
    provider_code: str    # exact GoXtop denom; empty => manual fulfilment
    membership_days: int = 0

    @property
    def is_membership(self):
        return self.kind == "membership"


FREEFIRE = [
    ("ff-0", "25 Diamonds", 35, "diamonds", "FF_25_CODE", 0),
    ("ff-1", "50 Diamonds", 50, "diamonds", "FF_50_CODE", 0),
    ("ff-2", "115 Diamonds", 100, "diamonds", "FF_115_CODE", 0),
    ("ff-3", "240 Diamonds", 220, "diamonds", "FF_240_CODE", 0),
    ("ff-4", "610 Diamonds", 550, "diamonds", "FF_610_CODE", 0),
    ("ff-5", "1240 Diamonds", 1100, "diamonds", "FF_1240_CODE", 0),
    ("ff-6", "2530 Diamonds", 2130, "diamonds", "FF_2530_CODE", 0),
    ("ff-7", "Weekly Membership", 220, "membership", "FF_WEEKLY_CODE", 7),
    ("ff-8", "Monthly Membership", 1048, "membership", "FF_MONTHLY_CODE", 30),
]

# eFootball has no supplier mapping yet: orders are fulfilled manually by an
# admin unless you set EF_<n>_CODE (n = index in this list).
EFOOTBALL = [
    ("130 Coins", 210, "coins"), ("300 Coins", 450, "coins"),
    ("750 Coins", 1100, "coins"), ("1040 Coins", 1420, "coins"),
    ("2130 Coins", 2900, "coins"), ("3250 Coins", 4350, "coins"),
    ("5700 Coins", 7100, "coins"), ("12800 Coins", 15150, "coins"),
    ("Starter Set: Casillas", 420, "pack"), ("Luis Suarez Pack", 200, "pack"),
]


def build_catalog(cfg):
    ff = [Product(i, n, p, k, os.getenv(env, "").strip(), d) for i, n, p, k, env, d in FREEFIRE]
    ef = [Product(f"ef-{i}", n, p, k, os.getenv(f"EF_{i}_CODE", "").strip())
          for i, (n, p, k) in enumerate(EFOOTBALL)]
    return {
        "freefire": {"game_code": cfg.freefire_game_code, "products": ff},
        "efootball": {"game_code": cfg.efootball_game_code, "products": ef},
    }


def find_product(catalog, game, product_id):
    for p in catalog.get(game, {}).get("products", []):
        if p.id == product_id:
            return p
    return None
