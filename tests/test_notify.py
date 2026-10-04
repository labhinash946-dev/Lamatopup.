import os
import tempfile
import unittest
from unittest import mock

import requests

from lama import create_app
from lama.notify import Notifier
from lama.config import Config

ADMIN = "a" * 32
H = {"Authorization": f"Bearer {ADMIN}"}


class FakeResp:
    def __init__(self, code=200):
        self.status_code = code


def make(**cfg):
    app = create_app({"database_path": os.path.join(tempfile.mkdtemp(), "t.sqlite3"),
                      "admin_token": ADMIN, "trust_proxy_hops": 0, "backup_telegram": False, **cfg})
    app.extensions["notifier"].inline = True
    return app, app.test_client()


BODY = {"game": "freefire", "product_id": "ff-7", "user_id": "123456789"}


class NotifyTests(unittest.TestCase):
    def test_telegram_alert_on_payment(self):
        app, c = make(telegram_bot_token="TOKEN", telegram_chat_id="42")
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            r = c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(post.call_count, 1)
        url, kw = post.call_args[0][0], post.call_args[1]
        self.assertEqual(url, "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(kw["json"]["chat_id"], "42")
        text = kw["json"]["text"]
        for part in ("Payment to verify", "Weekly Membership", "Rs. 220", "123456789", "FP123456"):
            self.assertIn(part, text)
        self.assertNotIn("reply_markup", kw["json"])  # http://localhost is not an https URL

    def test_unpaid_order_is_silent_by_default_then_pay_alerts(self):
        app, c = make(telegram_bot_token="T", telegram_chat_id="1")
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            j = c.post("/api/orders", json=BODY).get_json()
            self.assertEqual(post.call_count, 0)
            c.post(f"/api/orders/{j['order']['id']}/payment", json={"payment_ref": "TXN99999"},
                   headers={"X-Order-Token": j["tracking_token"]})
            self.assertEqual(post.call_count, 1)
            self.assertIn("TXN99999", post.call_args[1]["json"]["text"])

    def test_notify_unpaid_flag(self):
        app, c = make(telegram_bot_token="T", telegram_chat_id="1", notify_unpaid=True)
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            c.post("/api/orders", json=BODY)
        self.assertIn("New order (unpaid)", post.call_args[1]["json"]["text"])

    def test_idempotent_replay_does_not_alert_twice(self):
        app, c = make(telegram_bot_token="T", telegram_chat_id="1")
        h = {"Idempotency-Key": "abcdefgh12345"}
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"}, headers=h)
            c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"}, headers=h)
        self.assertEqual(post.call_count, 1)

    def test_failing_channel_never_breaks_orders(self):
        app, c = make(telegram_bot_token="T", telegram_chat_id="1")
        with mock.patch.object(Notifier, "_post", side_effect=requests.ConnectionError("boom T")):
            r = c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"})
        self.assertEqual(r.status_code, 201)

    def test_retry_once_on_http_error(self):
        app, c = make(telegram_bot_token="T", telegram_chat_id="1")
        with mock.patch.object(Notifier, "_post", side_effect=[FakeResp(500), FakeResp(200)]) as post:
            c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"})
        self.assertEqual(post.call_count, 2)

    def test_ntfy_channel_and_https_button(self):
        cfg = Config(ntfy_url="https://ntfy.sh/secret-topic", telegram_bot_token="T", telegram_chat_id="1")
        n = Notifier(cfg)
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            n.send("💰 Payment to verify", ["<b>X</b> · Rs. 1", "ID: <code>1</code>"], "https://shop.example/admin", "high")
        tg = [c for c in post.call_args_list if "telegram" in c[0][0]][0][1]["json"]
        self.assertEqual(tg["reply_markup"]["inline_keyboard"][0][0]["url"], "https://shop.example/admin")
        nt = [c for c in post.call_args_list if "ntfy" in c[0][0]][0][1]
        self.assertEqual(nt["headers"]["Click"], "https://shop.example/admin")
        self.assertEqual(nt["headers"]["Priority"], "high")
        self.assertEqual(nt["data"].decode(), "X · Rs. 1\nID: 1")
        nt["headers"]["Title"].encode("latin-1")  # must be header-safe

    def test_html_in_fields_is_escaped(self):
        n = Notifier(Config(telegram_bot_token="T", telegram_chat_id="1"))
        n.inline = True
        row = {"product_name": "<b>x</b>", "amount": 1, "game": "freefire", "user_id": "<i>1",
               "contact": "", "payment_ref": "", "id": "A"}
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()) as post:
            n.order_event(row, "payment")
        text = post.call_args[1]["json"]["text"]
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertNotIn("<i>", text)

    def test_notify_test_endpoint(self):
        app, c = make()
        self.assertEqual(c.post("/api/admin/notify-test").status_code, 401)
        r = c.post("/api/admin/notify-test", headers=H)
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "alerts_not_configured"))
        app2, c2 = make(telegram_bot_token="T", telegram_chat_id="1")
        with mock.patch.object(Notifier, "_post", return_value=FakeResp()):
            ok = c2.post("/api/admin/notify-test", headers=H)
        self.assertEqual(ok.get_json()["channels"], ["telegram"])
        with mock.patch.object(Notifier, "_post", return_value=FakeResp(401)):
            bad = c2.post("/api/admin/notify-test", headers=H)
        self.assertEqual(bad.status_code, 502)

    def test_no_channels_means_no_calls(self):
        app, c = make()
        with mock.patch.object(Notifier, "_post") as post:
            self.assertEqual(c.post("/api/orders", json={**BODY, "payment_ref": "FP123456"}).status_code, 201)
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
