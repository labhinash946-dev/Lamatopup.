"""Order lifecycle. All state changes go through here and are audited.

awaiting_payment -> payment_submitted -> processing -> completed
                \-> rejected / cancelled          \-> failed -> (retry) payment_submitted
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from .catalog import find_product
from .db import utcnow
from .errors import ApiError
from .goxtop import SupplierUnreachable, map_supplier_status
from .security import hash_token, new_token, token_matches

OPEN = ("awaiting_payment", "payment_submitted", "processing")
USER_ID_RE = re.compile(r"[A-Za-z0-9_\- ]{1,80}")
REF_RE = re.compile(r"[A-Za-z0-9_\-\. /]{4,60}")

MESSAGES = {
    "awaiting_payment": "Waiting for your payment confirmation.",
    "payment_submitted": "Payment received. We are verifying it.",
    "processing": "Payment verified. Your top-up is being delivered.",
    "completed": "Delivered. Enjoy!",
    "failed": "Delivery failed. We will contact you or refund you.",
    "rejected": "We could not verify the payment for this order.",
    "cancelled": "This order was cancelled.",
}


def add_event(conn, order_id, actor, event, detail=""):
    conn.execute("INSERT INTO order_events(order_id, at, actor, event, detail) VALUES (?,?,?,?,?)",
                 (order_id, utcnow(), actor, event, str(detail)[:1000]))


def get_order(conn, order_id):
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        raise ApiError(404, "order_not_found", "Order not found")
    return row


def _set(conn, order_id, **fields):
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE orders SET {cols} WHERE id=?", (*fields.values(), order_id))


def _transition(conn, order_id, allowed_from, new_status, **fields):
    """Atomic compare-and-set; protects against double-clicks and races."""
    marks = ",".join("?" * len(allowed_from))
    fields["status"] = new_status
    fields["updated_at"] = utcnow()
    cols = ", ".join(f"{k}=?" for k in fields)
    cur = conn.execute(f"UPDATE orders SET {cols} WHERE id=? AND status IN ({marks})",
                       (*fields.values(), order_id, *allowed_from))
    conn.commit()
    if cur.rowcount == 0:
        current = get_order(conn, order_id)["status"]
        raise ApiError(409, "invalid_state", f"Order is '{current}' and cannot move to '{new_status}'")


def public_view(row):
    return {
        "id": row["id"], "game": row["game"], "product": row["product_name"],
        "amount": row["amount"], "user_id": row["user_id"], "status": row["status"],
        "message": MESSAGES.get(row["status"], ""),
        "membership_expires_at": row["membership_expires_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def admin_view(conn, row):
    d = {k: row[k] for k in row.keys() if k not in ("token_hash", "idempotency_key")}
    d["events"] = [dict(e) for e in conn.execute(
        "SELECT at, actor, event, detail FROM order_events WHERE order_id=? ORDER BY id", (row["id"],))]
    return d


def create_order(conn, cfg, catalog, body, client_ip="", idempotency_key=None):
    game = str(body.get("game", "")).lower().strip()
    product_id = str(body.get("product_id", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    server_id = str(body.get("server_id", "")).strip()[:40]
    contact = str(body.get("contact", "")).strip()[:40]
    remarks = str(body.get("remarks", "")).strip()[:200]
    payment_ref = str(body.get("payment_ref", "")).strip()

    if game not in catalog:
        raise ApiError(400, "unsupported_game", "Unsupported game")
    product = find_product(catalog, game, product_id)
    if not product:
        raise ApiError(400, "invalid_product", "Invalid product")
    if not USER_ID_RE.fullmatch(user_id):
        raise ApiError(400, "invalid_user_id", "Invalid player/user ID")
    if game == "freefire" and not re.fullmatch(r"\d{8,15}", user_id):
        raise ApiError(400, "invalid_user_id", "Free Fire UID must be 8-15 digits")
    if contact and not re.fullmatch(r"[0-9+\- ]{7,20}", contact):
        raise ApiError(400, "invalid_contact", "Invalid contact number")
    if payment_ref and not REF_RE.fullmatch(payment_ref):
        raise ApiError(400, "invalid_payment_ref", "Invalid payment reference")
    if idempotency_key and not re.fullmatch(r"[A-Za-z0-9_\-]{8,80}", idempotency_key):
        raise ApiError(400, "invalid_idempotency_key", "Invalid Idempotency-Key")

    if idempotency_key:
        existing = conn.execute("SELECT * FROM orders WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            return public_view(existing), None, True

    warnings = []
    if product.is_membership:
        open_dup = conn.execute(
            "SELECT id FROM orders WHERE game=? AND user_id=? AND product_id=? AND status IN (?,?,?)",
            (game, user_id, product.id, *OPEN)).fetchone()
        if open_dup:
            raise ApiError(409, "duplicate_open_order",
                           f"You already have an open {product.name} order ({open_dup['id']})")
        active = conn.execute(
            "SELECT membership_expires_at FROM orders WHERE game=? AND user_id=? AND product_id=? "
            "AND status='completed' AND membership_expires_at > ? ORDER BY membership_expires_at DESC",
            (game, user_id, product.id, utcnow())).fetchone()
        if active:
            warnings.append(f"This ID already has a {product.name} active until {active['membership_expires_at']}")

    order_id = "LAMA-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8].upper()
    token = new_token()
    now = utcnow()
    status = "payment_submitted" if payment_ref else "awaiting_payment"
    conn.execute(
        "INSERT INTO orders(id, token_hash, idempotency_key, game, product_id, product_name, product_kind, "
        "membership_days, amount, user_id, server_id, contact, remarks, payment_ref, status, client_ip, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (order_id, hash_token(token), idempotency_key, game, product.id, product.name, product.kind,
         product.membership_days, product.price, user_id, server_id, contact, remarks, payment_ref, status, client_ip, now, now))
    add_event(conn, order_id, "customer", "order_created", f"{product.name} Rs.{product.price}")
    if payment_ref:
        add_event(conn, order_id, "customer", "payment_submitted", payment_ref)
    conn.commit()
    view = public_view(get_order(conn, order_id))
    if warnings:
        view["warnings"] = warnings
    return view, token, False


def authorize_customer(conn, order_id, token):
    row = get_order(conn, order_id)
    if not token_matches(token, row["token_hash"]):
        # same response as unknown order: no enumeration
        raise ApiError(404, "order_not_found", "Order not found")
    return row


def submit_payment(conn, order_id, token, payment_ref):
    authorize_customer(conn, order_id, token)
    payment_ref = (payment_ref or "").strip()
    if not REF_RE.fullmatch(payment_ref):
        raise ApiError(400, "invalid_payment_ref", "Enter the transaction ID from your payment receipt")
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted"), "payment_submitted",
                payment_ref=payment_ref)
    add_event(conn, order_id, "customer", "payment_submitted", payment_ref)
    conn.commit()
    return public_view(get_order(conn, order_id))


def _complete(conn, order, actor, detail=""):
    product_days = order["membership_days"] if order["product_kind"] == "membership" else 0
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=product_days)).strftime("%Y-%m-%dT%H:%M:%SZ") if product_days else None
    _transition(conn, order["id"], ("processing",), "completed",
                completed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), membership_expires_at=expires)
    add_event(conn, order["id"], actor, "completed", detail)
    conn.commit()


def verify_and_fulfil(conn, cfg, catalog, supplier, order_id, actor="admin"):
    """Admin confirmed the money arrived. Claim the order exactly once, then call the supplier."""
    order = get_order(conn, order_id)
    product = find_product(catalog, order["game"], order["product_id"])
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted"), "processing")
    add_event(conn, order_id, actor, "payment_verified")
    conn.commit()

    if not product or not product.provider_code:
        _set(conn, order_id, supplier_status="manual")
        add_event(conn, order_id, "system", "manual_fulfilment", "No supplier code: deliver manually, then mark complete")
    elif cfg.dry_run:
        _set(conn, order_id, supplier_status="dry_run")
        add_event(conn, order_id, "system", "dry_run", "DRY_RUN=true: supplier not called")
    else:
        payload = {"partner_orderid": order_id, "game": catalog[order["game"]]["game_code"],
                   "denom": product.provider_code, "userid": order["user_id"]}
        if order["server_id"]:
            payload["server_code"] = order["server_id"]
        try:
            res = supplier.create_order(payload)
        except SupplierUnreachable as exc:
            # Unknown outcome: never re-send blindly. `sync` will look it up by partner_orderid.
            _set(conn, order_id, supplier_status="unknown", failure_reason=f"supplier unreachable: {exc}")
            add_event(conn, order_id, "system", "supplier_unreachable", exc)
            conn.commit()
            return public_and_admin(conn, order_id)
        _set(conn, order_id, supplier_response=res.raw)
        explicit_fail = isinstance(res.data, dict) and res.data.get("success") is False
        if 200 <= res.http_status < 300 and not explicit_fail:
            mapped = map_supplier_status(res.data)
            _set(conn, order_id, supplier_status=mapped)
            add_event(conn, order_id, "system", "supplier_accepted", f"http {res.http_status} -> {mapped}")
            conn.commit()
            if mapped == "completed":
                _complete(conn, get_order(conn, order_id), "system", "supplier reported success")
            elif mapped == "failed":
                _transition(conn, order_id, ("processing",), "failed", failure_reason="supplier reported failure")
        elif 400 <= res.http_status < 500 or explicit_fail:
            # definitively not placed: safe to retry after fixing the cause
            _transition(conn, order_id, ("processing",), "failed", supplier_status="rejected",
                        failure_reason=f"supplier rejected (http {res.http_status})")
            add_event(conn, order_id, "system", "supplier_rejected", res.raw[:300])
        else:
            _set(conn, order_id, supplier_status="unknown", failure_reason=f"supplier error http {res.http_status}")
            add_event(conn, order_id, "system", "supplier_error", f"http {res.http_status}")
    conn.commit()
    return public_and_admin(conn, order_id)


def public_and_admin(conn, order_id):
    return admin_view(conn, get_order(conn, order_id))


def sync_order(conn, cfg, supplier, order_id):
    order = get_order(conn, order_id)
    if order["status"] != "processing" or order["supplier_status"] in ("manual", "dry_run"):
        return admin_view(conn, order)
    try:
        res = supplier.order_status(order_id)
    except SupplierUnreachable as exc:
        add_event(conn, order_id, "system", "sync_unreachable", exc)
        conn.commit()
        return admin_view(conn, get_order(conn, order_id))
    if res.http_status == 404 and order["supplier_status"] == "unknown":
        # supplier never received it: safe to retry
        _transition(conn, order_id, ("processing",), "failed", supplier_status="rejected",
                    failure_reason="supplier has no record of this order")
        add_event(conn, order_id, "system", "sync_not_found", "Safe to retry")
    elif 200 <= res.http_status < 300:
        mapped = map_supplier_status(res.data)
        _set(conn, order_id, supplier_status=mapped, supplier_response=res.raw)
        add_event(conn, order_id, "system", "synced", mapped)
        conn.commit()
        if mapped == "completed":
            _complete(conn, get_order(conn, order_id), "system", "supplier reported success")
        elif mapped == "failed":
            _transition(conn, order_id, ("processing",), "failed", failure_reason="supplier reported failure")
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))


def sync_all(conn, cfg, supplier, limit=100):
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM orders WHERE status='processing' AND supplier_status NOT IN ('manual','dry_run') "
        "ORDER BY created_at LIMIT ?", (limit,))]
    out = []
    for oid in ids:
        v = sync_order(conn, cfg, supplier, oid)
        out.append({"id": oid, "status": v["status"], "supplier_status": v["supplier_status"]})
    return out


def reject(conn, order_id, reason, actor="admin"):
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted"), "rejected",
                failure_reason=(reason or "")[:200])
    add_event(conn, order_id, actor, "rejected", reason)
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))


def cancel(conn, order_id, reason, actor="admin"):
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted", "failed"), "cancelled",
                failure_reason=(reason or "")[:200])
    add_event(conn, order_id, actor, "cancelled", reason)
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))


def manual_complete(conn, order_id, note, actor="admin"):
    _complete(conn, get_order(conn, order_id), actor, note or "marked delivered manually")
    return admin_view(conn, get_order(conn, order_id))


def retry(conn, order_id, actor="admin"):
    order = get_order(conn, order_id)
    if order["status"] != "failed" or order["supplier_status"] != "rejected":
        raise ApiError(409, "not_retryable",
                       "Only orders the supplier definitively rejected can be retried")
    _transition(conn, order_id, ("failed",), "payment_submitted", failure_reason="")
    add_event(conn, order_id, actor, "retry_requested")
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))
