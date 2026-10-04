"""Admin auth, rate limiting, tokens."""
import hashlib
import hmac
import secrets
import threading
import time
from functools import wraps

from flask import current_app, request

from .errors import ApiError


class RateLimiter:
    """Sliding window, in-memory (per worker process)."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, bucket, key, limit, window):
        now = time.time()
        k = (bucket, key)
        with self._lock:
            hits = [t for t in self._hits.get(k, []) if now - t < window]
            hits.append(now)
            self._hits[k] = hits
            if len(self._hits) > 10000:
                for old in [x for x, v in self._hits.items() if not v or now - v[-1] > window]:
                    self._hits.pop(old, None)
            return len(hits) > limit


def client_ip():
    return request.remote_addr or "unknown"


def enforce(bucket, limit, window, message="Too many requests, try again later"):
    if limit and current_app.extensions["limiter"].hit(bucket, client_ip(), limit, window):
        raise ApiError(429, "rate_limited", message)


def new_token():
    return secrets.token_urlsafe(18)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def token_matches(token, token_hash):
    return bool(token) and hmac.compare_digest(hash_token(token), token_hash)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        cfg = current_app.config["LAMA"]
        if not cfg.admin_token:
            raise ApiError(503, "admin_disabled", "Admin API is disabled (ADMIN_TOKEN not set)")
        header = request.headers.get("Authorization", "")
        supplied = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied.encode(), cfg.admin_token.encode()):
            # count only failures, so legitimate admin use is never throttled
            if current_app.extensions["limiter"].hit("admin-fail", client_ip(), 10, 60):
                raise ApiError(429, "rate_limited", "Too many failed attempts")
            raise ApiError(401, "unauthorized", "Invalid admin token")
        return fn(*args, **kwargs)
    return wrapper
