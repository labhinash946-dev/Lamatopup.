"""Owner alerts via Telegram and/or ntfy. Never blocks or breaks an order."""
import html
import logging
import threading

import requests

log = logging.getLogger("lama.notify")
GAMES = {"freefire": "Free Fire", "efootball": "eFootball"}


class Notifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.inline = False  # tests set True to run synchronously
        if cfg.telegram_bot_token and not cfg.telegram_chat_id:
            log.warning("TELEGRAM_BOT_TOKEN is set but TELEGRAM_CHAT_ID is missing: Telegram alerts off")

    @property
    def telegram_on(self):
        return bool(self.cfg.telegram_bot_token and self.cfg.telegram_chat_id)

    @property
    def ntfy_on(self):
        return self.cfg.ntfy_url.startswith(("https://", "http://"))

    @property
    def enabled(self):
        return self.telegram_on or self.ntfy_on

    def _post(self, url, **kwargs):
        return requests.post(url, timeout=8, **kwargs)

    def _get(self, url, **kwargs):
        return requests.get(url, timeout=15, **kwargs)

    def send(self, title, lines, admin_url="", priority="default"):
        """Blocking. Returns {channel: True/False}. Secrets are never logged."""
        result = {}
        if self.telegram_on:
            text = "<b>%s</b>\n%s" % (html.escape(title), "\n".join(lines))
            payload = {"chat_id": self.cfg.telegram_chat_id, "text": text,
                       "parse_mode": "HTML", "disable_web_page_preview": True}
            if admin_url.startswith("https://"):  # Telegram rejects non-https buttons
                payload["reply_markup"] = {"inline_keyboard": [[{"text": "Open admin", "url": admin_url}]]}
            result["telegram"] = self._try(
                f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage", "telegram", json=payload)
        if self.ntfy_on:
            plain = "\n".join(html.unescape(l.replace("<b>", "").replace("</b>", "")
                                              .replace("<code>", "").replace("</code>", "")) for l in lines)
            headers = {"Title": "Lama: " + title.encode("ascii", "ignore").decode().strip(),
                       "Priority": "high" if priority == "high" else "default", "Tags": "moneybag"}
            if admin_url:
                headers["Click"] = admin_url
            result["ntfy"] = self._try(self.cfg.ntfy_url, "ntfy", data=plain.encode("utf-8"), headers=headers)
        return result

    def send_file(self, filename, data, caption=""):
        """Blocking Telegram document upload that also pins the message, so the latest
        backup can be found again after a restart. Returns {"sent": bool, "pinned": bool}."""
        out = {"sent": False, "pinned": False}
        if not self.telegram_on:
            return out
        base = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"
        for _ in (1, 2):
            try:
                r = self._post(f"{base}/sendDocument",
                               data={"chat_id": self.cfg.telegram_chat_id, "caption": caption[:900]},
                               files={"document": (filename, data)})
            except Exception as exc:  # the URL holds the bot token: log the class only
                log.warning("telegram backup error: %s", type(exc).__name__)
                continue
            if r.status_code >= 300:
                log.warning("telegram backup failed: http %s", r.status_code)
                continue
            out["sent"] = True
            try:
                mid = r.json()["result"]["message_id"]
                p = self._post(f"{base}/pinChatMessage", json={
                    "chat_id": self.cfg.telegram_chat_id, "message_id": mid, "disable_notification": True})
                out["pinned"] = p.status_code < 300
            except Exception as exc:
                log.warning("telegram pin error: %s", type(exc).__name__)
            break
        return out

    def fetch_latest_backup(self):
        """Download the newest pinned lama-backup file, or None."""
        if not self.telegram_on:
            return None
        base = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}"
        try:
            chat = self._get(f"{base}/getChat", params={"chat_id": self.cfg.telegram_chat_id}).json()
            doc = ((chat.get("result") or {}).get("pinned_message") or {}).get("document") or {}
            if not str(doc.get("file_name", "")).startswith("lama-backup-") or not doc.get("file_id"):
                return None
            info = self._get(f"{base}/getFile", params={"file_id": doc["file_id"]}).json()
            path = info["result"]["file_path"]
            r = self._get(f"https://api.telegram.org/file/bot{self.cfg.telegram_bot_token}/{path}")
            return r.content if r.status_code == 200 else None
        except Exception as exc:
            log.warning("telegram restore fetch error: %s", type(exc).__name__)
            return None

    def _try(self, url, name, **kwargs):
        for attempt in (1, 2):
            try:
                r = self._post(url, **kwargs)
                if r.status_code < 300:
                    return True
                log.warning("%s alert failed: http %s", name, r.status_code)
            except Exception as exc:  # never include the URL: it contains the bot token
                log.warning("%s alert error: %s", name, type(exc).__name__)
        return False

    def dispatch(self, *args, **kwargs):
        if not self.enabled:
            return
        if self.inline:
            self.send(*args, **kwargs)
        else:
            threading.Thread(target=self.send, args=args, kwargs=kwargs, daemon=True).start()

    def order_event(self, row, kind, base_url="", duplicates=0):
        """kind: 'payment' (needs verifying) or 'new' (placed, unpaid)."""
        if kind == "new" and not self.cfg.notify_unpaid:
            return
        e = html.escape
        title = "Payment to verify" if kind == "payment" else "New order (unpaid)"
        lines = [f"<b>{e(row['product_name'])}</b> · Rs. {row['amount']}",
                 f"{GAMES.get(row['game'], e(row['game']))} ID: <code>{e(row['user_id'])}</code>"]
        if row["contact"]:
            lines.append(f"Contact: {e(row['contact'])}")
        if row["payment_ref"]:
            lines.append(f"Txn ref: <code>{e(row['payment_ref'])}</code>")
        if duplicates:
            lines.append(f"⚠️ This txn ref was already used on {duplicates} other order(s). Check before delivering!")
        lines.append(f"Order: <code>{e(row['id'])}</code>")
        admin_url = (base_url.rstrip("/") + "/admin") if base_url else ""
        self.dispatch("💰 " + title if kind == "payment" else "🛒 " + title, lines, admin_url,
                      "high" if kind == "payment" else "default")
