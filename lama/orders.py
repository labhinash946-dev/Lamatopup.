"""Order lifecycle. All state changes go through here and are audited.

    awaiting_payment -> payment_submitted -> processing -> completed
    (awaiting_payment | payment_submitted) -> rejected
    (awaiting_payment | payment_submitted | processing) -> cancelled

Fulfilment is manual: confirm the payment, deliver the top-up yourself, then mark it complete.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone

from .catalog import find_product
from .db import utcnow
from .errors import ApiError
from .security import hash_token, new_token, token_matches

OPEN = ("awaiting_payment", "payment_submitted", "processing")
USER_ID_RE = re.compile(r"[A-Za-z0-9_\- ]{1,80}")
REF_RE = re.compile(r"[A-Za-z0-9_\-\. /]{4,60}")

MESSAGES = {
    "awaiting_payment": "Waiting for your payment confirmation.",
    "payment_submitted": "Payment received. We are verifying it.",
    "processing": "Payment verified. Your top-up is being delivered.",
    "completed": "Delivered. Enjoy!",
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


def duplicate_refs(conn, order_id, payment_ref):
    """Other orders that used the same transaction ID (case-insensitive). A reused ID is a fraud signal."""
    ref = (payment_ref or "").strip()
    if not ref:
        return []
    rows = conn.execute(
        "SELECT id, status, user_id FROM orders WHERE id != ? AND payment_ref = ? COLLATE NOCASE "
        "ORDER BY created_at", (order_id, ref)).fetchall()
    return [dict(r) for r in rows]


def _flag_duplicates(conn, order_id, payment_ref):
    dups = duplicate_refs(conn, order_id, payment_ref)
    if dups:
        add_event(conn, order_id, "system", "duplicate_ref",
                  "Transaction ID also used on: " + ", ".join(d["id"] for d in dups))
        conn.commit()
    return dups


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
    d["duplicate_refs"] = duplicate_refs(conn, row["id"], row["payment_ref"])
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
    if payment_ref:
        _flag_duplicates(conn, order_id, payment_ref)
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
    _flag_duplicates(conn, order_id, payment_ref)
    return public_view(get_order(conn, order_id))


def _complete(conn, order, actor, detail=""):
    days = order["membership_days"] if order["product_kind"] == "membership" else 0
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ") if days else None
    _transition(conn, order["id"], ("processing",), "completed",
                completed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"), membership_expires_at=expires)
    add_event(conn, order["id"], actor, "completed", detail)
    conn.commit()


def verify_payment(conn, order_id, actor="admin"):
    """Admin confirmed the money arrived. Moves the order to 'processing' exactly once."""
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted"), "processing")
    add_event(conn, order_id, actor, "payment_verified", "Deliver the top-up, then mark complete")
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))


def reject(conn, order_id, reason, actor="admin"):
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted"), "rejected",
                failure_reason=(reason or "")[:200])
    add_event(conn, order_id, actor, "rejected", reason)
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))


def cancel(conn, order_id, reason, actor="admin"):
    _transition(conn, order_id, ("awaiting_payment", "payment_submitted", "processing"), "cancelled",
                failure_reason=(reason or "")[:200])
    add_event(conn, order_id, actor, "cancelled", reason)
    conn.commit()
    return admin_view(conn, get_order(conn, order_id))


def manual_complete(conn, order_id, note, actor="admin"):
    _complete(conn, get_order(conn, order_id), actor, note or "delivered")
    return admin_view(conn, get_order(conn, order_id))
