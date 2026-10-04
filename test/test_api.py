import os
import tempfile
import unittest

from lama import create_app
from lama.goxtop import SupplierResult, SupplierUnreachable

ADMIN = "a" * 32
H = {"Authorization": f"Bearer {ADMIN}"}


class FakeSupplier:
    def __init__(self):
        self.created, self.mode, self.status_payload = [], "ok", {"status": "processing"}

    def create_order(self, payload):
        self.created.append(payload)
        if self.mode == "down":
            raise SupplierUnreachable("Timeout")
        if self.mode == "reject":
            return SupplierResult(400, {"success": False, "error": "bad denom"})
        return SupplierResult(200, {"success": True, "status": "processing"})

    def order_status(self, oid):
        if self.mode == "down":
            raise SupplierUnreachable("Timeout")
        if self.mode == "notfound":
            return SupplierResult(404, {"error": "nope"})
        return SupplierResult(200, self.status_payload)

    def balance(self):
        return SupplierResult(200, {"balance": 5})


class Base(unittest.TestCase):
    dry = False

    def setUp(self):
        os.environ["FF_WEEKLY_CODE"] = "WK1"
        os.environ["FF_115_CODE"] = "D115"
        self.tmp = tempfile.mkdtemp()
        self.app = create_app({
            "database_path": os.path.join(self.tmp, "t.sqlite3"), "admin_token": ADMIN,
            "dry_run": self.dry, "goxtop_api_key": "k", "trust_proxy_hops": 0})
        self.sup = FakeSupplier()
        self.app.extensions["supplier"] = self.sup
        self.c = self.app.test_client()

    def order(self, product="ff-7", uid="123456789", **extra):
        r = self.c.post("/api/orders", json={"game": "freefire", "product_id": product,
                                             "user_id": uid, **extra})
        return r, r.get_json()


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
        self.assertNotIn("supplier", str(ok.get_json()))

    def test_idempotency(self):
        h = {"Idempotency-Key": "abcdefgh12345"}
        a = self.c.post("/api/orders", json={"game": "freefire", "product_id": "ff-2", "user_id": "123456789"}, headers=h)
        b = self.c.post("/api/orders", json={"game": "freefire", "product_id": "ff-2", "user_id": "123456789"}, headers=h)
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
        self.app.config["LAMA"] = self.app.config["LAMA"].__class__(**{**self.app.config["LAMA"].__dict__, "orders_per_hour": 2})
        codes = [self.order(product="ff-0", uid=f"12345678{i}")[0].status_code for i in range(4)]
        self.assertEqual(codes[:2], [201, 201])
        self.assertEqual(codes[2], 429)

    def test_help_endpoint(self):
        r = self.c.get("/api/help")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["success"])
        self.assertIn("GET /api/health", r.get_json()["public"])

    def test_unknown_route_is_json_404(self):
        r = self.c.get("/api/nope")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["success"])


class AdminTests(Base):
    def test_admin_requires_token(self):
        self.assertEqual(self.c.get("/api/admin/orders").status_code, 401)
        self.assertEqual(self.c.get("/api/admin/orders", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(self.c.get("/api/admin/supplier/balance").status_code, 401)
        self.assertEqual(self.c.get("/api/balance").status_code, 404)  # old public leak is gone
        self.assertEqual(self.c.get("/api/admin/orders", headers=H).status_code, 200)

    def test_admin_disabled_without_token(self):
        app = create_app({"database_path": os.path.join(self.tmp, "x.sqlite3"), "admin_token": "",
                          "dry_run": True, "trust_proxy_hops": 0})
        self.assertEqual(app.test_client().get("/api/admin/orders").status_code, 503)

    def test_failed_admin_attempts_throttled(self):
        codes = [self.c.get("/api/admin/orders", headers={"Authorization": "Bearer nope"}).status_code for _ in range(12)]
        self.assertEqual(codes[-1], 429)

    def test_full_happy_path_with_membership_expiry(self):
        _, j = self.order()
        oid = j["order"]["id"]
        r = self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sup.created[0], {"partner_orderid": oid, "game": "freefire_global",
                                               "denom": "WK1", "userid": "123456789"})
        self.assertEqual(r.get_json()["order"]["status"], "processing")
        self.sup.status_payload = {"data": {"status": "Success"}}
        s = self.c.post(f"/api/admin/orders/{oid}/sync", headers=H).get_json()["order"]
        self.assertEqual(s["status"], "completed")
        self.assertTrue(s["membership_expires_at"])
        exp = self.c.get("/api/admin/memberships/expiring?days=8", headers=H).get_json()["items"]
        self.assertEqual(exp[0]["id"], oid)
        events = [e["event"] for e in s["events"]]
        self.assertEqual(events[0], "order_created")
        self.assertIn("payment_verified", events)

    def test_no_double_fulfilment(self):
        _, j = self.order()
        oid = j["order"]["id"]
        self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H)
        again = self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(len(self.sup.created), 1)

    def test_supplier_down_never_resends_and_sync_recovers(self):
        _, j = self.order()
        oid = j["order"]["id"]
        self.sup.mode = "down"
        r = self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H).get_json()["order"]
        self.assertEqual((r["status"], r["supplier_status"]), ("processing", "unknown"))
        self.assertEqual(self.c.post(f"/api/admin/orders/{oid}/retry", headers=H).status_code, 409)
        self.sup.mode = "notfound"
        s = self.c.post(f"/api/admin/orders/{oid}/sync", headers=H).get_json()["order"]
        self.assertEqual((s["status"], s["supplier_status"]), ("failed", "rejected"))
        self.assertEqual(self.c.post(f"/api/admin/orders/{oid}/retry", headers=H).get_json()["order"]["status"],
                         "payment_submitted")

    def test_supplier_rejection_is_retryable(self):
        _, j = self.order()
        oid = j["order"]["id"]
        self.sup.mode = "reject"
        r = self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H).get_json()["order"]
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.c.post(f"/api/admin/orders/{oid}/retry", headers=H).status_code, 200)

    def test_reject_and_manual_efootball(self):
        _, j = self.order()
        oid = j["order"]["id"]
        self.assertEqual(self.c.post(f"/api/admin/orders/{oid}/reject", headers=H, json={"reason": "no money"})
                         .get_json()["order"]["status"], "rejected")
        r = self.c.post("/api/orders", json={"game": "efootball", "product_id": "ef-0", "user_id": "kon123"})
        eid = r.get_json()["order"]["id"]
        v = self.c.post(f"/api/admin/orders/{eid}/verify-payment", headers=H).get_json()["order"]
        self.assertEqual(v["supplier_status"], "manual")
        self.assertEqual(self.sup.created, [])
        done = self.c.post(f"/api/admin/orders/{eid}/complete", headers=H, json={"note": "sent"}).get_json()["order"]
        self.assertEqual(done["status"], "completed")

    def test_weekly_report(self):
        for uid, prod in (("111111111", "ff-7"), ("222222222", "ff-2")):
            _, j = self.order(product=prod, uid=uid)
            oid = j["order"]["id"]
            self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H)
            self.sup.status_payload = {"status": "completed"}
            self.c.post(f"/api/admin/orders/{oid}/sync", headers=H)
        rep = self.c.get("/api/admin/reports/weekly", headers=H).get_json()
        self.assertEqual(rep["completed_orders"], 2)
        self.assertEqual(rep["revenue_npr"], 320)
        self.assertEqual(len(rep["per_product"]), 2)


class DryRunTests(Base):
    dry = True

    def test_dry_run_never_calls_supplier(self):
        _, j = self.order()
        oid = j["order"]["id"]
        r = self.c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H).get_json()["order"]
        self.assertEqual(r["supplier_status"], "dry_run")
        self.assertEqual(self.sup.created, [])


class ConfigTests(unittest.TestCase):
    def test_live_mode_requires_secrets(self):
        with self.assertRaises(RuntimeError):
            create_app({"dry_run": False, "goxtop_api_key": "", "admin_token": "",
                        "database_path": os.path.join(tempfile.mkdtemp(), "c.sqlite3")})


if __name__ == "__main__":
    unittest.main()
