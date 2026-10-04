import logging
import os
import sys
import time
import uuid
from dataclasses import replace

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from .catalog import build_catalog
from .config import Config
from .db import close_db, init_db
from .errors import ApiError
from .goxtop import GoxtopClient
from .players import PlayerLookup
from .security import RateLimiter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "public")


def create_app(overrides=None):
    from dotenv import load_dotenv
    load_dotenv()

    cfg = Config.from_env()
    if overrides:
        cfg = replace(cfg, **overrides)
    cfg.validate()

    # Only public/ is served: never the source tree, .env or the database.
    app = Flask(__name__, static_folder=PUBLIC, static_url_path="")
    app.config["LAMA"] = cfg
    app.config["JSON_SORT_KEYS"] = False
    if cfg.trust_proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=cfg.trust_proxy_hops, x_proto=1)

    app.extensions["catalog"] = build_catalog(cfg)
    app.extensions["supplier"] = GoxtopClient(cfg)
    app.extensions["players"] = PlayerLookup(cfg)
    app.extensions["limiter"] = RateLimiter()
    init_db(cfg.database_path)

    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("lama")

    from .routes import admin, api
    app.register_blueprint(api)
    app.register_blueprint(admin)
    app.teardown_appcontext(close_db)

    @app.before_request
    def _start():
        g.rid = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])[:40]
        g.t0 = time.time()

    @app.after_request
    def _finish(resp):
        resp.headers["X-Request-ID"] = getattr(g, "rid", "-")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
            log.info("%s %s %s %s %dms", g.rid, request.method, request.path, resp.status_code,
                     int((time.time() - g.t0) * 1000))
        return resp

    @app.get("/")
    def home():
        return send_from_directory(PUBLIC, "index.html")

    def err(status, code, message):
        r = jsonify({"success": False, "error": message, "code": code, "request_id": getattr(g, "rid", "-")})
        return r, status

    @app.errorhandler(ApiError)
    def _api_error(e):
        return err(e.status, e.code, e.message)

    @app.errorhandler(HTTPException)
    def _http_error(e):
        message = e.description
        if e.code == 404 and request.path.startswith("/api/"):
            message = "Endpoint not found. See /api/help for available endpoints."
        return err(e.code or 500, (e.name or "error").lower().replace(" ", "_"), message)

    @app.errorhandler(Exception)
    def _unhandled(e):
        log.exception("unhandled error %s", getattr(g, "rid", "-"))
        return err(500, "server_error", "Server error")

    return app
