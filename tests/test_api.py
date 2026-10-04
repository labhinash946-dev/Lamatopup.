import os
import tempfile
import unittest

from lama import create_app

ADMIN = "a" * 32
H = {"Authorization": f"Bearer {ADMIN}"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = create_app({"database_path": os.path.join(self.tmp, "t.sqlite3"),
                               "admin_token": ADMIN, "trust_proxy_hops": 0})
        self.c = self.app.test_client()

    def order(self, product="ff-7", uid="123456789", **extra):
        r = self.c.post("/api/orders", json={"game": "freefire", "product_id": product,
                                             "user_id": uid, **extra})
        return r, r.get_json()

    def post(self, path, **kw):
        return self.c.post(path, headers=H, **kw)


class PublicTests(Base):
    def test_source_and_db_not_served(self):
        for path in ("/app.py", "/lama/config.py", "/.env", "/data/lama.sqlite3", "/requirements.txt"):
            self.assertEqual(self.c.get(path).status_code, 404, path)
        self.assertEqual(self.c.get("/").status_code, 200)
        self.assertEqual(self.c.get("/style.css").status_code, 200)

    def test_create_order_uses_server_price(self):
        r, j = self.order(amount=1, price=1)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(j["order"]["amount"], 220)
        self.assertEqual(j["order"]["status"], "awaiting_payment")
        self.assertIn("tracking_token", j)
        self.assertNotIn("token_hash", str(j))

    def test_validation(self):
        self.assertEqual(self.order(uid="abc")[0].status_code, 400)
        self.assertEqual(self.order(product="zz")[0].status_code, 400)
        r = self.c.post("/api/orders", json={"game": "pubg", "product_id": "x", "user_id": "1"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.post("/api/orders", data="junk").status_code, 400)

    def test_tracking_requires_token(self):
        _, j = self.order()
        oid, tok = j["order"]["id"], j["tracking_token"]
        self.assertEqual(self.c.get(f"/api/orders/{oid}").status_code, 404)
        self.assertEqual(self.c.get(f"/api/orders/{oid}", headers={"X-Order-Token": "x" * 24}).status_code, 404)
        ok = self.c.get(f"/api/orders/{oid}", headers={"X-Order-Token": tok})
        self.assertEqual(ok.status_code, 200)

    def test_idempotency(self):
        h = {"Idempotency-Key": "abcdefgh12345"}
        body = {"game": "freefire", "product_id": "ff-2", "user_id": "123456789"}
        a = self.c.post("/api/orders", json=body, headers=h)
        b = self.c.post("/api/orders", json=body, headers=h)
        self.assertEqual(a.get_json()["order"]["id"], b.get_json()["order"]["id"])
        self.assertTrue(b.get_json()["replayed"])
        self.assertNotIn("tracking_token", b.get_json())

    def test_duplicate_open_membership_blocked(self):
        self.assertEqual(self.order()[0].status_code, 201)
        r, j = self.order()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(j["code"], "duplicate_open_order")

    def test_payment_ref_submission(self):
        _, j = self.order()
        oid, tok = j["order"]["id"], j["tracking_token"]
        r = self.c.post(f"/api/orders/{oid}/payment", json={"payment_ref": "TXN12345"},
                        headers={"X-Order-Token": tok})
        self.assertEqual(r.get_json()["order"]["status"], "payment_submitted")
        bad = self.c.post(f"/api/orders/{oid}/payment", json={"payment_ref": "x"}, headers={"X-Order-Token": tok})
        self.assertEqual(bad.status_code, 400)

    def test_rate_limit(self):
        cfg = self.app.config["LAMA"]
        self.app.config["LAMA"] = cfg.__class__(**{**cfg.__dict__, "orders_per_hour": 2})
        codes = [self.order(product="ff-0", uid=f"12345678{i}")[0].status_code for i in range(4)]
        self.assertEqual(codes[:2], [201, 201])
        self.assertEqual(codes[2], 429)

    def test_track_page_served(self):
        r = self.c.get("/track")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Track your order", r.data)
        self.assertIn("noindex", r.headers["X-Robots-Tag"])
        self.assertIn(b"/track?o=", self.c.get("/script.js").data)

    def test_customer_can_track_and_pay_after_checkout(self):
        _, j = self.order(product="ff-3")
        oid, h = j["order"]["id"], {"X-Order-Token": j["tracking_token"]}
        s1 = self.c.get(f"/api/orders/{oid}", headers=h).get_json()["order"]
        self.assertEqual(s1["status"], "awaiting_payment")
        self.c.post(f"/api/orders/{oid}/payment", json={"payment_ref": "FP123456"}, headers=h)
        self.post(f"/api/admin/orders/{oid}/verify-payment")
        self.post(f"/api/admin/orders/{oid}/complete")
        s2 = self.c.get(f"/api/orders/{oid}", headers=h).get_json()["order"]
        self.assertEqual(s2["status"], "completed")
        self.assertNotIn("payment_ref", s2)

    def test_help_and_unknown_route(self):
        r = self.c.get("/api/help")
        self.assertEqual(r.status_code, 200)
        self.assertIn("GET /api/health", r.get_json()["public"])
        n = self.c.get("/api/nope")
        self.assertEqual(n.status_code, 404)
        self.assertFalse(n.get_json()["success"])

    def test_no_supplier_endpoints_left(self):
        for path in ("/api/balance", "/api/games", "/api/admin/supplier/balance"):
            self.assertEqual(self.c.get(path, headers=H).status_code, 404, path)
        self.assertIn(self.post("/api/admin/sync").status_code, (404, 405))


class AdminTests(Base):
    def test_admin_requires_token(self):
        self.assertEqual(self.c.get("/api/admin/orders").status_code, 401)
        self.assertEqual(self.c.get("/api/admin/orders", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.c.get("/api/admin/orders", headers=H).status_code, 200)

    def test_admin_page_served_but_api_still_locked(self):
        r = self.c.get("/admin")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Lama Admin", r.data)
        self.assertIn("noindex", r.headers["X-Robots-Tag"])
        self.assertNotIn(ADMIN.encode(), r.data)
        self.assertEqual(self.c.get("/api/admin/orders").status_code, 401)

    def test_admin_disabled_without_token(self):
        app = create_app({"database_path": os.path.join(self.tmp, "x.sqlite3"), "admin_token": "",
                          "trust_proxy_hops": 0})
        self.assertEqual(app.test_client().get("/api/admin/orders").status_code, 503)

    def test_failed_admin_attempts_throttled(self):
        codes = [self.c.get("/api/admin/orders", headers={"Authorization": "Bearer nope"}).status_code
                 for _ in range(12)]
        self.assertEqual(codes[-1], 429)

    def test_short_admin_token_rejected(self):
        with self.assertRaises(RuntimeError):
            create_app({"database_path": os.path.join(self.tmp, "y.sqlite3"), "admin_token": "short"})

    def test_manual_flow_with_membership_expiry(self):
        _, j = self.order()
        oid = j["order"]["id"]
        v = self.post(f"/api/admin/orders/{oid}/verify-payment")
        self.assertEqual(v.get_json()["order"]["status"], "processing")
        done = self.post(f"/api/admin/orders/{oid}/complete", json={"note": "sent via player ID"})
        o = done.get_json()["order"]
        self.assertEqual(o["status"], "completed")
        self.assertTrue(o["membership_expires_at"])
        exp = self.c.get("/api/admin/memberships/expiring?days=8", headers=H).get_json()["items"]
        self.assertEqual(exp[0]["id"], oid)
        events = [e["event"] for e in o["events"]]
        self.assertEqual(events, ["order_created", "payment_verified", "completed"])

    def test_cannot_complete_before_payment_verified(self):
        _, j = self.order()
        r = self.post(f"/api/admin/orders/{j['order']['id']}/complete")
        self.assertEqual(r.status_code, 409)

    def test_verify_is_idempotent_guarded(self):
        _, j = self.order()
        oid = j["order"]["id"]
        self.assertEqual(self.post(f"/api/admin/orders/{oid}/verify-payment").status_code, 200)
        self.assertEqual(self.post(f"/api/admin/orders/{oid}/verify-payment").status_code, 409)

    def test_reject_and_cancel(self):
        _, a = self.order(uid="111111111")
        _, b = self.order(uid="222222222")
        ra = self.post(f"/api/admin/orders/{a['order']['id']}/reject", json={"reason": "no money"})
        self.assertEqual(ra.get_json()["order"]["status"], "rejected")
        bid = b["order"]["id"]
        self.post(f"/api/admin/orders/{bid}/verify-payment")
        rb = self.post(f"/api/admin/orders/{bid}/cancel", json={"reason": "refunded"})
        self.assertEqual(rb.get_json()["order"]["status"], "cancelled")
        self.assertEqual(self.post(f"/api/admin/orders/{bid}/complete").status_code, 409)

    def test_efootball_order(self):
        r = self.c.post("/api/orders", json={"game": "efootball", "product_id": "ef-0", "user_id": "kon123"})
        eid = r.get_json()["order"]["id"]
        self.post(f"/api/admin/orders/{eid}/verify-payment")
        done = self.post(f"/api/admin/orders/{eid}/complete").get_json()["order"]
        self.assertEqual(done["status"], "completed")

    def test_weekly_report(self):
        for uid, prod in (("111111111", "ff-7"), ("222222222", "ff-2")):
            _, j = self.order(product=prod, uid=uid)
            oid = j["order"]["id"]
            self.post(f"/api/admin/orders/{oid}/verify-payment")
            self.post(f"/api/admin/orders/{oid}/complete")
        rep = self.c.get("/api/admin/reports/weekly", headers=H).get_json()
        self.assertEqual(rep["completed_orders"], 2)
        self.assertEqual(rep["revenue_npr"], 320)
        self.assertEqual(len(rep["per_product"]), 2)


if __name__ == "__main__":
    unittest.main()
