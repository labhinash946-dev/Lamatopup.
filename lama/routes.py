import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

from flask import Blueprint, Response, current_app, jsonify, request

from . import backup as bk
from . import orders as svc
from .db import close_db, get_db, utcnow
from .errors import ApiError
from .security import admin_required, client_ip, enforce

api = Blueprint("api", __name__, url_prefix="/api")
admin = Blueprint("admin", __name__, url_prefix="/api/admin")


def ext(name):
    return current_app.extensions[name]


def changed():
    ext("backups").touch()


def _int_arg(name, default):
    try:
        return int(request.args.get(name, default) or default)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid_parameter", f"'{name}' must be a number")


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- public
@api.get("/health")
def health():
    db_ok = True
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False
    return jsonify({"ok": db_ok}), (200 if db_ok else 503)


@api.get("/help")
def help_index():
    """Human-readable index of the public API (admin routes need a token)."""
    return jsonify({
        "success": True, "service": "Lama Topup API",
        "public": [
            "GET /api/health", "GET /api/help", "GET /api/catalog",
            "GET /api/check-player?id=<uid>",
            "POST /api/orders  (optional Idempotency-Key header)",
            "GET /api/orders/<id>  (header X-Order-Token)",
            "POST /api/orders/<id>/payment  (header X-Order-Token, body {payment_ref})",
        ],
        "admin": "Under /api/admin/*, send Authorization: Bearer <ADMIN_TOKEN>. See README.",
    })


@api.get("/catalog")
def catalog():
    out = {}
    for game, g in ext("catalog").items():
        out[game] = [{"id": p.id, "name": p.name, "price": p.price, "kind": p.kind,
                      "membership_days": p.membership_days} for p in g["products"]]
    return jsonify({"success": True, "catalog": out})


@api.get("/lookup-status")
def lookup_status():
    cfg = current_app.config["LAMA"]
    return jsonify({"custom_lookup_configured": ext("players").custom_configured,
                    "region": cfg.player_lookup_region, "strict": cfg.player_lookup_strict})


@api.get("/check-player")
def check_player():
    cfg = current_app.config["LAMA"]
    uid = (request.args.get("id") or "").strip()
    if not re.fullmatch(r"\d{8,15}", uid):
        raise ApiError(400, "invalid_uid", "Enter a valid numeric UID (8-15 digits)")
    enforce("check", cfg.checks_per_minute, 60, "Too many checks, try again in a minute")
    try:
        name, region = ext("players").check(uid)
    except RuntimeError:
        raise ApiError(502, "lookup_unavailable", "Name check is unavailable right now")
    if not name:
        raise ApiError(404, "player_not_found", "No player found for this UID")
    return jsonify({"success": True, "name": name, "region": region, "uid": uid})


@api.post("/orders")
def create_order():
    cfg = current_app.config["LAMA"]
    enforce("orders", cfg.orders_per_hour, 3600, "Too many orders from your network, try later")
    view, token, replayed = svc.create_order(
        get_db(), cfg, ext("catalog"), body(), client_ip(),
        request.headers.get("Idempotency-Key"))
    if not replayed:
        row = svc.get_order(get_db(), view["id"])
        ext("notifier").order_event(
            row, "payment" if row["status"] == "payment_submitted" else "new", request.url_root,
            len(svc.duplicate_refs(get_db(), row["id"], row["payment_ref"])))
    if not replayed:
        changed()
    resp = {"success": True, "order": view, "replayed": replayed}
    if token:
        resp["tracking_token"] = token  # shown once; only its hash is stored
    return jsonify(resp), (200 if replayed else 201)


def _token():
    return request.headers.get("X-Order-Token") or request.args.get("token") or ""


@api.get("/orders/<order_id>")
def order_status(order_id):
    enforce("track", 60, 60)
    row = svc.authorize_customer(get_db(), order_id, _token())
    return jsonify({"success": True, "order": svc.public_view(row)})


@api.post("/orders/<order_id>/payment")
def order_payment(order_id):
    cfg = current_app.config["LAMA"]
    enforce("payment", cfg.payments_per_hour, 3600)
    view = svc.submit_payment(get_db(), order_id, _token(), body().get("payment_ref"))
    row = svc.get_order(get_db(), order_id)
    ext("notifier").order_event(row, "payment", request.url_root,
                                len(svc.duplicate_refs(get_db(), order_id, row["payment_ref"])))
    changed()
    return jsonify({"success": True, "order": view})


# ---------------------------------------------------------------- admin
def _cfg():
    return current_app.config["LAMA"]


@admin.get("/orders")
@admin_required
def admin_list():
    status = request.args.get("status", "").strip()
    q = request.args.get("q", "").strip()
    limit = max(1, min(_int_arg("limit", 50), 200))
    offset = max(0, _int_arg("offset", 0))
    where, args = [], []
    if status:
        where.append("status=?"); args.append(status)
    if q:
        where.append("(id LIKE ? OR user_id LIKE ? OR payment_ref LIKE ?)")
        args += [f"%{q}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) c FROM orders {clause}", args).fetchone()["c"]
    rows = db.execute(f"SELECT orders.*, (SELECT COUNT(*) FROM orders o2 WHERE o2.id != orders.id "
                      f"AND orders.payment_ref != '' AND o2.payment_ref = orders.payment_ref COLLATE NOCASE) "
                      f"AS dup_count FROM orders {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                      (*args, limit, offset)).fetchall()
    items = [{k: r[k] for k in r.keys() if k not in ("token_hash", "idempotency_key")}
             for r in rows]
    return jsonify({"success": True, "total": total, "items": items})


@admin.get("/orders/<order_id>")
@admin_required
def admin_get(order_id):
    db = get_db()
    return jsonify({"success": True, "order": svc.admin_view(db, svc.get_order(db, order_id))})


@admin.post("/orders/<order_id>/verify-payment")
@admin_required
def admin_verify(order_id):
    out = svc.verify_payment(get_db(), order_id)
    changed()
    return jsonify({"success": True, "order": out})


@admin.post("/orders/<order_id>/reject")
@admin_required
def admin_reject(order_id):
    out = svc.reject(get_db(), order_id, body().get("reason", ""))
    changed()
    return jsonify({"success": True, "order": out})


@admin.post("/orders/<order_id>/cancel")
@admin_required
def admin_cancel(order_id):
    out = svc.cancel(get_db(), order_id, body().get("reason", ""))
    changed()
    return jsonify({"success": True, "order": out})


@admin.post("/orders/<order_id>/complete")
@admin_required
def admin_complete(order_id):
    out = svc.manual_complete(get_db(), order_id, body().get("note", ""))
    changed()
    return jsonify({"success": True, "order": out})


@admin.get("/storage")
@admin_required
def storage():
    cfg, b = _cfg(), ext("backups")
    path = cfg.database_path
    return jsonify({"success": True, "persistent": bk.is_persistent(cfg),
                    "orders": get_db().execute("SELECT COUNT(*) c FROM orders").fetchone()["c"],
                    "db_bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                    "telegram_backup": b.enabled, "last_backup_at": b.last_ok,
                    "auto_restore_ready": bool(b.enabled and b.last_pinned),
                    "backup_interval_min": b.interval_min})


def _stamp():
    return utcnow().replace(":", "").replace("-", "")


@admin.get("/backup")
@admin_required
def backup_download():
    data = bk.snapshot_bytes(_cfg().database_path)
    return Response(data, mimetype="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="lama-backup-{_stamp()}.sqlite3"'})


@admin.post("/backup-now")
@admin_required
def backup_now():
    b = ext("backups")
    if not b.enabled:
        raise ApiError(400, "backups_not_configured", "Set up Telegram alerts first (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).")
    res = b.run_now()
    if not res["sent"]:
        raise ApiError(502, "backup_failed", "Could not send the backup to Telegram")
    return jsonify({"success": True, **res})


@admin.get("/export.csv")
@admin_required
def export_csv():
    return Response(bk.orders_csv(get_db()), mimetype="text/csv", headers={
        "Content-Disposition": f'attachment; filename="lama-orders-{_stamp()}.csv"'})


@admin.post("/restore")
@admin_required
def restore_backup():
    upload = request.files.get("file")
    if not upload:
        raise ApiError(400, "no_file", "Attach the backup file as 'file'")
    n = get_db().execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    if n and request.args.get("force") != "1":
        raise ApiError(409, "not_empty", f"Current data has {n} orders. Restoring replaces them.")
    fd, tmp = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        upload.save(tmp)
        close_db()
        bk.restore(tmp, _cfg().database_path)
    finally:
        os.unlink(tmp)
    count = get_db().execute("SELECT COUNT(*) c FROM orders").fetchone()["c"]
    return jsonify({"success": True, "orders": count})


@admin.post("/notify-test")
@admin_required
def notify_test():
    n = ext("notifier")
    if not n.enabled:
        raise ApiError(400, "alerts_not_configured",
                       "No alert channel set. Add TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID or NTFY_URL.")
    result = n.send("Test alert", ["Alerts are working. You will be notified here when a customer pays."],
                    request.url_root.rstrip("/") + "/admin")
    if not all(result.values()):
        raise ApiError(502, "alert_failed", "Alert failed for: " + ", ".join(k for k, v in result.items() if not v))
    return jsonify({"success": True, "channels": list(result)})


@admin.get("/memberships/expiring")
@admin_required
def admin_expiring():
    """Weekly/monthly memberships ending soon: your renewal reminder list."""
    days = max(0, min(_int_arg("days", 2), 30))
    now = datetime.now(timezone.utc)
    upto = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = get_db().execute(
        "SELECT id, game, user_id, contact, product_name, membership_expires_at FROM orders "
        "WHERE status='completed' AND membership_expires_at IS NOT NULL AND membership_expires_at <= ? "
        "AND membership_expires_at >= ? ORDER BY membership_expires_at",
        (upto, (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ"))).fetchall()
    return jsonify({"success": True, "items": [dict(r) for r in rows]})


@admin.get("/reports/weekly")
@admin_required
def admin_weekly():
    days = max(1, min(_int_arg("days", 7), 90))
    off = _cfg().tz_offset_minutes
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mod = f"{off:+d} minutes"
    db = get_db()
    per_day = db.execute(
        "SELECT date(completed_at, ?) AS day, COUNT(*) AS orders, SUM(amount) AS revenue FROM orders "
        "WHERE status='completed' AND completed_at >= ? GROUP BY day ORDER BY day", (mod, since)).fetchall()
    per_product = db.execute(
        "SELECT product_name, COUNT(*) AS orders, SUM(amount) AS revenue FROM orders "
        "WHERE status='completed' AND completed_at >= ? GROUP BY product_name ORDER BY revenue DESC",
        (since,)).fetchall()
    by_status = db.execute("SELECT status, COUNT(*) AS n FROM orders WHERE created_at >= ? GROUP BY status",
                           (since,)).fetchall()
    open_now = db.execute("SELECT status, COUNT(*) AS n FROM orders WHERE status IN (?,?,?) GROUP BY status",
                          svc.OPEN).fetchall()
    total_rev = sum(r["revenue"] or 0 for r in per_day)
    return jsonify({"success": True, "window_days": days, "generated_at": utcnow(),
                    "completed_orders": sum(r["orders"] for r in per_day), "revenue_npr": total_rev,
                    "per_day": [dict(r) for r in per_day], "per_product": [dict(r) for r in per_product],
                    "created_by_status": {r["status"]: r["n"] for r in by_status},
                    "open_now": {r["status"]: r["n"] for r in open_now}})

