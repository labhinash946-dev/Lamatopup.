"""SQLite storage (stdlib only). WAL mode, one connection per request."""
import os
import sqlite3
from datetime import datetime, timezone

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL,
    idempotency_key TEXT UNIQUE,
    game TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    product_kind TEXT NOT NULL,
    membership_days INTEGER NOT NULL DEFAULT 0,
    amount INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    server_id TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '',
    remarks TEXT NOT NULL DEFAULT '',
    payment_ref TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    failure_reason TEXT NOT NULL DEFAULT '',
    membership_expires_at TEXT,
    client_ip TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(game, user_id);
CREATE INDEX IF NOT EXISTS idx_orders_completed ON orders(completed_at);
CREATE INDEX IF NOT EXISTS idx_orders_expiry ON orders(membership_expires_at);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL REFERENCES orders(id),
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_order ON order_events(order_id);
"""


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path):
    if path != ":memory:":
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def get_db():
    if "db" not in g:
        g.db = connect(current_app.config["LAMA"].database_path)
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()
