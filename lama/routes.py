import re
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

from . import orders as svc
from .db import get_db, utcnow
from .errors import ApiError
from .security import admin_required, client_ip, enforce

api = Blueprint("api", __name__, url_prefix="/api")
admin = Blueprint("admin", __name__, url_prefix="/api/admin")


def ext(name):
    return current_app.extensions[name]


def body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- public
@api.get("/health")
def health():
    cfg = current_app.config["LAMA"]
    db_ok = True
    try:
        get_db().execute("SELECT 1").fetchone()
    except Exception:
        db_ok = False
    return jsonify({"ok": db_ok, "dry_run": cfg.dry_run}), (200 if db_ok else 503)


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
    return jsonify({"success": True, "order": view})


# ---------------------------------------------------------------- admin
def _cfg():
    return current_app.config["LAMA"]


@admin.get("/orders")
@admin_required
def admin_list():
    status = request.args.get("status", "").strip()
    q = request.args.get("q", "").strip()
    limit = max(1, min(int(request.args.get("limit", 50) or 50), 200))
    offset = max(0, int(request.args.get("offset", 0) or 0))
    where, args = [], []
    if status:
        where.append("status=?"); args.append(status)
    if q:
        where.append("(id LIKE ? OR user_id LIKE ? OR payment_ref LIKE ?)")
        args += [f"%{q}%"] * 3
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    db = get_db()
    total = db.execute(f"SELECT COUNT(*) c FROM orders {clause}", args).fetchone()["c"]
    rows = db.execute(f"SELECT * FROM orders {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                      (*args, limit, offset)).fetchall()
    items = [{k: r[k] for k in r.keys() if k not in ("token_hash", "idempotency_key", "supplier_response")}
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
    out = svc.verify_and_fulfil(get_db(), _cfg(), ext("catalog"), ext("supplier"), order_id)
    return jsonify({"success": True, "order": out})


@admin.post("/orders/<order_id>/reject")
@admin_required
def admin_reject(order_id):
    return jsonify({"success": True, "order": svc.reject(get_db(), order_id, body().get("reason", ""))})


@admin.post("/orders/<order_id>/cancel")
@admin_required
def admin_cancel(order_id):
    return jsonify({"success": True, "order": svc.cancel(get_db(), order_id, body().get("reason", ""))})


@admin.post("/orders/<order_id>/complete")
@admin_required
def admin_complete(order_id):
    return jsonify({"success": True, "order": svc.manual_complete(get_db(), order_id, body().get("note", ""))})


@admin.post("/orders/<order_id>/retry")
@admin_required
def admin_retry(order_id):
    return jsonify({"success": True, "order": svc.retry(get_db(), order_id)})


@admin.post("/orders/<order_id>/sync")
@admin_required
def admin_sync(order_id):
    return jsonify({"success": True, "order": svc.sync_order(get_db(), _cfg(), ext("supplier"), order_id)})


@admin.post("/sync")
@admin_required
def admin_sync_all():
    return jsonify({"success": True, "synced": svc.sync_all(get_db(), _cfg(), ext("supplier"))})


@admin.get("/memberships/expiring")
@admin_required
def admin_expiring():
    """Weekly/monthly memberships ending soon: your renewal reminder list."""
    days = max(0, min(int(request.args.get("days", 2) or 2), 30))
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
    days = max(1, min(int(request.args.get("days", 7) or 7), 90))
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


@admin.get("/supplier/balance")
@admin_required
def supplier_balance():
    r = ext("supplier").balance()
    return jsonify(r.data), r.http_status


@admin.get("/supplier/games")
@admin_required
def supplier_games():
    r = ext("supplier").games()
    return jsonify(r.data), r.http_status


@admin.get("/supplier/products/<game_code>")
@admin_required
def supplier_products(game_code):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,60}", game_code):
        raise ApiError(400, "invalid_game_code", "Invalid game code")
    r = ext("supplier").products(game_code)
    return jsonify(r.data), r.http_status
