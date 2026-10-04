"""Backups, restore, CSV export and the "is my data safe?" check."""
import csv
import io
import logging
import os
import sqlite3
import tempfile
import threading
import time

from .db import SCHEMA, connect, init_db, utcnow
from .errors import ApiError

log = logging.getLogger("lama.backup")


def _required_order_columns():
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA)
    cols = {r[1] for r in mem.execute("PRAGMA table_info(orders)")}
    mem.close()
    return cols


REQUIRED_ORDER_COLS = _required_order_columns()


def is_persistent(cfg):
    """True if orders survive restarts and redeploys."""
    mode = cfg.persistent_storage.lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("false", "0", "no"):
        return False
    if os.getenv("RENDER"):  # Render sets this. Only the mounted disk (/var/data) survives.
        return os.path.abspath(cfg.database_path).startswith("/var/data/")
    return True


def snapshot_bytes(db_path):
    """Consistent copy of the live database (safe while the app is writing)."""
    src = sqlite3.connect(db_path, timeout=15)
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        dst.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        src.close()
        os.unlink(tmp)


def validate_backup(path):
    """Returns an error message, or None if the file is a usable Lama backup."""
    try:
        conn = sqlite3.connect(path)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "The backup file is damaged"
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"orders", "order_events"} <= tables:
                return "This is not a Lama backup"
            have = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
            if not REQUIRED_ORDER_COLS <= have:
                return "The backup is missing order columns"
            return None
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return "This is not a valid backup file"


def restore(upload_path, dest_path):
    err = validate_backup(upload_path)
    if err:
        raise ApiError(400, "invalid_backup", err)
    src = sqlite3.connect(upload_path)
    dst = connect(dest_path)
    try:
        src.backup(dst)
    except sqlite3.OperationalError:
        raise ApiError(503, "restore_busy", "Database is busy. Try again in a moment.")
    finally:
        src.close()
        dst.close()
    init_db(dest_path)  # adds anything newer than the backup


def auto_restore(cfg, notifier):
    """Free-plan safety net. If storage is temporary and the database is empty (the server was
    wiped on restart), pull the newest pinned Telegram backup. Returns the restored order count."""
    if is_persistent(cfg) or not (cfg.backup_telegram and notifier.telegram_on):
        return 0
    conn = connect(cfg.database_path)
    try:
        if conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]:
            return 0
    finally:
        conn.close()
    data = notifier.fetch_latest_backup()
    if not data:
        return 0
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        restore(tmp, cfg.database_path)
    except ApiError as exc:
        log.warning("auto-restore skipped: %s", exc.message)
        return 0
    finally:
        os.unlink(tmp)
    conn = connect(cfg.database_path)
    try:
        n = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        conn.close()
    log.info("auto-restored %s orders from the latest Telegram backup", n)
    return n


CSV_COLS = ["id", "created_at", "status", "game", "product_name", "amount", "user_id", "server_id",
            "contact", "payment_ref", "remarks", "failure_reason", "completed_at", "membership_expires_at"]


def _safe(v):
    s = "" if v is None else str(v)
    # stop spreadsheet formula injection from customer-typed fields
    return "'" + s if s and s[0] in "=+-@\t\r" else s


def orders_csv(conn):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    for r in conn.execute(f"SELECT {', '.join(CSV_COLS)} FROM orders ORDER BY created_at"):
        w.writerow([_safe(v) for v in r])
    return buf.getvalue()


class Backups:
    """Sends the database to Telegram after changes, at most once per interval."""

    def __init__(self, cfg, notifier):
        self.cfg, self.notifier = cfg, notifier
        self.inline = False  # tests: run now, record the wait instead of starting a timer
        self.scheduled_wait = None
        self.last_at = 0.0
        self.last_ok = None
        self.last_pinned = None
        self._pending = False
        self._lock = threading.Lock()

    @property
    def enabled(self):
        return bool(self.cfg.backup_telegram and self.notifier.telegram_on)

    @property
    def interval_min(self):
        return self.cfg.backup_interval_min or (60 if is_persistent(self.cfg) else 1)

    def touch(self):
        if not self.enabled:
            return
        with self._lock:
            if self._pending:
                return
            self._pending = True
            wait = max(0.0, self.interval_min * 60 - (time.time() - self.last_at))
        if self.inline:
            if wait > 0:
                self.scheduled_wait = wait
                with self._lock:
                    self._pending = False
            else:
                self.scheduled_wait = None
                self._run()
        elif wait > 0:
            t = threading.Timer(wait, self._run)
            t.daemon = True
            t.start()
        else:
            threading.Thread(target=self._run, daemon=True).start()

    def _send(self):
        ok, pinned = False, False
        try:
            data = snapshot_bytes(self.cfg.database_path)
            stamp = utcnow().replace(":", "").replace("-", "")
            res = self.notifier.send_file(f"lama-backup-{stamp}.sqlite3", data,
                                          "Lama database backup. Keep this file private.")
            ok, pinned = res["sent"], res["pinned"]
        except Exception as exc:
            log.warning("backup failed: %s", type(exc).__name__)
        if ok:
            with self._lock:
                self.last_at, self.last_ok, self.last_pinned = time.time(), utcnow(), pinned
        return {"sent": ok, "pinned": pinned}

    def _run(self):
        try:
            return self._send()
        finally:
            with self._lock:
                self._pending = False

    def run_now(self):
        """Synchronous backup used by the admin 'Back up now' button."""
        return self._send()
