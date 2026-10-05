import csv
import io
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from lama import create_app
from lama.backup import Backups, is_persistent, validate_backup
from lama.config import Config
from lama.notify import Notifier

ADMIN = "a" * 32
H = {"Authorization": f"Bearer {ADMIN}"}
BODY = {"game": "freefire", "product_id": "ff-7", "user_id": "123456789"}


class FakeResp:
    status_code = 200
    content = b""

    def __init__(self, payload=None, content=b""):
        self._p = payload if payload is not None else {"ok": True, "result": {"message_id": 7}}
        self.content = content

    def json(self):
        return self._p


def make(**cfg):
    d = tempfile.mkdtemp()
    app = create_app({"database_path": os.path.join(d, "t.sqlite3"), "admin_token": ADMIN,
                      "trust_proxy_hops": 0, **cfg})
    return app, app.test_client(), d


def place(c, uid="123456789", ref="FP123456", **extra):
    return c.post("/api/orders", json={**BODY, "user_id": uid, "payment_ref": ref, **extra}).get_json()


class StorageTests(unittest.TestCase):
    def test_backup_download_is_valid_and_admin_only(self):
        app, c, d = make()
        place(c)
        self.assertEqual(c.get("/api/admin/backup").status_code, 401)
        r = c.get("/api/admin/backup", headers=H)
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["Content-Disposition"])
        path = os.path.join(d, "dl.sqlite3")
        with open(path, "wb") as fh:
            fh.write(r.data)
        self.assertIsNone(validate_backup(path))
        self.assertEqual(sqlite3.connect(path).execute("SELECT COUNT(*) FROM orders").fetchone()[0], 1)

    def test_restore_roundtrip_keeps_orders_events_and_tracking_tokens(self):
        app, c, _ = make()
        j = place(c)
        oid, tok = j["order"]["id"], j["tracking_token"]
        c.post(f"/api/admin/orders/{oid}/verify-payment", headers=H)
        data = c.get("/api/admin/backup", headers=H).data

        app2, c2, _ = make()  # fresh, empty server (e.g. after a redeploy wiped the disk)
        self.assertEqual(c2.get(f"/api/orders/{oid}", headers={"X-Order-Token": tok}).status_code, 404)
        r = c2.post("/api/admin/restore", headers=H, data={"file": (io.BytesIO(data), "b.sqlite3")},
                    content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.get_json()["orders"]), (200, 1))
        st = c2.get(f"/api/orders/{oid}", headers={"X-Order-Token": tok}).get_json()["order"]
        self.assertEqual(st["status"], "processing")
        full = c2.get(f"/api/admin/orders/{oid}", headers=H).get_json()["order"]
        self.assertEqual([e["event"] for e in full["events"]][-1], "payment_verified")
        # still fully usable afterwards
        self.assertEqual(c2.post(f"/api/admin/orders/{oid}/complete", headers=H).status_code, 200)
        self.assertEqual(c2.post("/api/orders", json={**BODY, "user_id": "999999999"}).status_code, 201)

    def test_restore_refuses_to_overwrite_without_force_and_rejects_junk(self):
        app, c, _ = make()
        place(c)
        data = c.get("/api/admin/backup", headers=H).data
        f = lambda b: {"file": (io.BytesIO(b), "x.sqlite3")}
        r = c.post("/api/admin/restore", headers=H, data=f(data), content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.get_json()["code"]), (409, "not_empty"))
        ok = c.post("/api/admin/restore?force=1", headers=H, data=f(data), content_type="multipart/form-data")
        self.assertEqual(ok.status_code, 200)
        junk = c.post("/api/admin/restore?force=1", headers=H, data=f(b"not a database at all" * 50),
                      content_type="multipart/form-data")
        self.assertEqual((junk.status_code, junk.get_json()["code"]), (400, "invalid_backup"))
        other = os.path.join(tempfile.mkdtemp(), "o.sqlite3")
        odb = sqlite3.connect(other)
        odb.executescript("CREATE TABLE foo(a);")
        odb.close()
        with open(other, "rb") as fh:
            other_bytes = fh.read()
        bad = c.post("/api/admin/restore?force=1", headers=H, data=f(other_bytes),
                     content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(c.post("/api/admin/restore", headers=H).status_code, 400)
        self.assertEqual(c.post("/api/admin/restore", data=f(data), content_type="multipart/form-data").status_code, 401)
        # data survived all the failed attempts
        self.assertEqual(c.get("/api/admin/orders", headers=H).get_json()["total"], 1)

    def test_csv_export_hides_secrets_and_blocks_formula_injection(self):
        app, c, _ = make()
        place(c, remarks="=HYPERLINK(\"http://x\")")
        r = c.get("/api/admin/export.csv", headers=H)
        self.assertEqual(r.headers["Content-Type"].split(";")[0], "text/csv")
        rows = list(csv.reader(io.StringIO(r.data.decode())))
        self.assertEqual(rows[0][:3], ["id", "created_at", "status"])
        self.assertNotIn("token_hash", rows[0])
        self.assertTrue(rows[1][rows[0].index("remarks")].startswith("'="))
        self.assertEqual(c.get("/api/admin/export.csv").status_code, 401)

    def test_persistence_detection(self):
        with mock.patch.dict(os.environ, {"RENDER": "true"}):
            self.assertFalse(is_persistent(Config(database_path="data/lama.sqlite3")))
            self.assertTrue(is_persistent(Config(database_path="/var/data/lama.sqlite3")))
            self.assertTrue(is_persistent(Config(database_path="/x/y.db", persistent_storage="true")))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENDER", None)
            self.assertTrue(is_persistent(Config(database_path="data/lama.sqlite3")))
            self.assertFalse(is_persistent(Config(persistent_storage="false")))

    def test_storage_endpoint(self):
        app, c, _ = make(persistent_storage="false")
        place(c)
        j = c.get("/api/admin/storage", headers=H).get_json()
        self.assertFalse(j["persistent"])
        self.assertEqual(j["orders"], 1)
        self.assertGreater(j["db_bytes"], 0)
        self.assertFalse(j["telegram_backup"])
        self.assertEqual(c.get("/api/admin/storage").status_code, 401)

    def test_oversized_json_rejected_but_small_ok(self):
        app, c, _ = make()
        big = c.post("/api/orders", data=b"x" * 70000, content_type="application/json")
        self.assertEqual(big.status_code, 413)
        self.assertEqual(c.post("/api/orders", json=BODY).status_code, 201)


class TelegramBackupTests(unittest.TestCase):
    def test_backup_sent_then_throttled(self):
        app, c, _ = make(telegram_bot_token="T", telegram_chat_id="9")
        app.extensions["notifier"].inline = True
        app.extensions["backups"].inline = True
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            place(c, uid="111111111")  # also triggers a payment alert (json), not a document
            docs = [x for x in post.call_args_list if "sendDocument" in x[0][0]]
            self.assertEqual(len(docs), 1)
            kw = docs[0][1]
            self.assertEqual(kw["data"]["chat_id"], "9")
            name, blob = kw["files"]["document"]
            self.assertTrue(name.startswith("lama-backup-") and name.endswith(".sqlite3"))
            self.assertTrue(blob.startswith(b"SQLite format 3"))
            place(c, uid="222222222")  # within the hour: waits instead of sending again
            docs = [x for x in post.call_args_list if "sendDocument" in x[0][0]]
            self.assertEqual(len(docs), 1)
            self.assertGreater(app.extensions["backups"].scheduled_wait, 0)
        self.assertTrue(app.extensions["backups"].last_ok)

    def test_backup_is_pinned_and_run_now_reports_it(self):
        app, c, _ = make(telegram_bot_token="T", telegram_chat_id="9")
        app.extensions["notifier"].inline = True
        app.extensions["backups"].inline = True
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            place(c)
            j = c.post("/api/admin/backup-now", headers=H).get_json()
            self.assertEqual((j["sent"], j["pinned"]), (True, True))
            pin = [x for x in post.call_args_list if "pinChatMessage" in x[0][0]][0][1]["json"]
            self.assertEqual((pin["chat_id"], pin["message_id"], pin["disable_notification"]), ("9", 7, True))
            st = c.get("/api/admin/storage", headers=H).get_json()
            self.assertTrue(st["auto_restore_ready"])
        app2, c2, _ = make()
        self.assertEqual(c2.post("/api/admin/backup-now", headers=H).get_json()["code"], "backups_not_configured")

    def test_auto_restore_after_wipe_on_temporary_storage(self):
        app, c, _ = make(telegram_bot_token="T", telegram_chat_id="9", persistent_storage="false")
        j = place(c)
        oid, tok = j["order"]["id"], j["tracking_token"]
        blob = c.get("/api/admin/backup", headers=H).data

        def fake_get(self, url, **kw):
            if url.endswith("/getChat"):
                return FakeResp({"result": {"pinned_message": {"document": {
                    "file_name": "lama-backup-2026.sqlite3", "file_id": "FID"}}}})
            if url.endswith("/getFile"):
                return FakeResp({"result": {"file_path": "docs/file_1.sqlite3"}})
            return FakeResp(content=blob)

        with mock.patch.object(Notifier, "_get", fake_get):
            app2, c2, _ = make(telegram_bot_token="T", telegram_chat_id="9", persistent_storage="false")
        st = c2.get(f"/api/orders/{oid}", headers={"X-Order-Token": tok}).get_json()
        self.assertEqual(st["order"]["id"], oid)

        # persistent storage must never auto-overwrite; a pinned non-backup file is ignored
        with mock.patch.object(Notifier, "_get", fake_get):
            app3, c3, _ = make(telegram_bot_token="T", telegram_chat_id="9", persistent_storage="true")
        self.assertEqual(c3.get("/api/admin/orders", headers=H).get_json()["total"], 0)

        def other_pin(self, url, **kw):
            return FakeResp({"result": {"pinned_message": {"document": {"file_name": "cat.png", "file_id": "x"}}}})
        with mock.patch.object(Notifier, "_get", other_pin):
            app4, c4, _ = make(telegram_bot_token="T", telegram_chat_id="9", persistent_storage="false")
        self.assertEqual(c4.get("/api/admin/orders", headers=H).get_json()["total"], 0)

    def test_auto_restore_never_replaces_existing_orders_and_survives_network_errors(self):
        d = tempfile.mkdtemp()
        cfg = {"database_path": os.path.join(d, "t.sqlite3"), "admin_token": ADMIN, "trust_proxy_hops": 0,
               "telegram_bot_token": "T", "telegram_chat_id": "9", "persistent_storage": "false"}
        app = create_app(cfg)
        place(app.test_client())
        with mock.patch.object(Notifier, "_get", side_effect=AssertionError("must not be called")):
            app = create_app(cfg)  # DB has an order: no fetch at all
        self.assertEqual(app.test_client().get("/api/admin/orders", headers=H).get_json()["total"], 1)
        cfg["database_path"] = os.path.join(d, "empty.sqlite3")
        with mock.patch.object(Notifier, "_get", side_effect=OSError("offline")):
            app = create_app(cfg)  # offline at wake-up: starts normally, empty
        self.assertEqual(app.test_client().get("/api/health").status_code, 200)

    def test_interval_is_short_on_temporary_storage(self):
        app, c, _ = make(telegram_bot_token="T", telegram_chat_id="9", persistent_storage="false")
        self.assertEqual(app.extensions["backups"].interval_min, 1)
        app2, c2, _ = make(telegram_bot_token="T", telegram_chat_id="9", persistent_storage="true")
        self.assertEqual(app2.extensions["backups"].interval_min, 60)

    def test_failed_backup_does_not_break_orders_and_retries_next_time(self):
        app, c, _ = make(telegram_bot_token="T", telegram_chat_id="9")
        app.extensions["notifier"].inline = True
        app.extensions["backups"].inline = True
        with mock.patch.object(Notifier, "_post", side_effect=OSError("down")):
            self.assertEqual(c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"}).status_code, 201)
        self.assertIsNone(app.extensions["backups"].last_ok)

    def test_disabled_without_telegram_or_when_switched_off(self):
        app, c, _ = make()
        self.assertFalse(app.extensions["backups"].enabled)
        app2, c2, _ = make(telegram_bot_token="T", telegram_chat_id="9", backup_telegram=False)
        self.assertFalse(app2.extensions["backups"].enabled)


if __name__ == "__main__":
    unittest.main()
