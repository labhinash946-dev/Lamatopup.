# Lama Topup backend

Flask + SQLite. Customers place orders and pay by Fonepay/eSewa. You verify the payment,
deliver the top-up yourself, and mark the order complete. There is no supplier API.
Weekly/monthly memberships get an expiry date so you know when to remind customers.

## Order flow

`awaiting_payment` -> `payment_submitted` -> `processing` -> `completed`
(or `rejected` before verification, `cancelled` any time before completion).

1. Customer: `POST /api/orders` -> gets `order.id` and a one-time `tracking_token`.
   Sending `payment_ref` (transaction ID) puts it straight into `payment_submitted`.
2. You check your Fonepay/eSewa statement, then call `verify-payment` (-> `processing`).
3. You deliver the top-up, then call `complete`. Memberships get `membership_expires_at`.

## Run

```
pip install -r requirements.txt
cp .env.example .env      # set ADMIN_TOKEN (24+ chars; 32+ recommended)
python app.py
python -m unittest discover -s tests
```

Without `ADMIN_TOKEN` the admin API is disabled (503).

## Admin page

Open `/admin` (for example `https://YOUR-APP.onrender.com/admin`) and sign in with your
`ADMIN_TOKEN`. Tabs: To verify, To deliver, Awaiting pay, Done, All. Tap an order to see the
player ID (with a copy button), the transaction ID and the history, then use the buttons:
Payment received, Mark delivered, Reject, Cancel / refund. It also shows this week's
totals and memberships ending in 2 days, and refreshes every 30 seconds (the browser tab
title shows how many orders need verifying). Not linked from the storefront.

## Customer tracking page

After checkout the storefront sends the customer to `/track`, which shows their order with a
progress bar (Placed, Payment sent, Verified, Delivered), updates every 15 seconds, and lets
them send a transaction ID if they paid after ordering. Orders are remembered on that device;
"Copy tracking link" lets a customer open the same order on another device. The tracking
token is removed from the address bar after the page loads. The storefront's Track Order
button opens the same page.

## Order alerts

You get a phone alert when a customer sends a payment (an order with a transaction ID, or a
transaction ID added later on the tracking page). Unpaid orders are silent unless you set
`NOTIFY_UNPAID=true`. Alerts are sent in the background; if Telegram or ntfy is down, orders
still work (one retry, failures are logged without secrets).

**Telegram (recommended):**
1. In Telegram, message `@BotFather`, send `/newbot`, and copy the bot token.
2. Open your new bot and send it any message.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the number after `"chat":{"id":`.
4. In Render, set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, then redeploy.

**ntfy (no account):** install the ntfy app, subscribe to a long random topic name, and set
`NTFY_URL=https://ntfy.sh/your-long-random-topic`. Anyone who knows the topic can read it, so
keep the name secret.

Then open `/admin` and tap **Send test alert**. Each alert includes the product, amount, player
ID, transaction ID and order ID, with an Open admin button (Telegram, https only).
Customers are not messaged: they follow progress on the tracking page.

## Public API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | DB check, used by Render |
| GET | `/api/help` | endpoint index |
| GET | `/api/catalog` | products and server-side prices |
| GET | `/api/check-player?id=` | Free Fire name check (convenience only) |
| POST | `/api/orders` | optional `Idempotency-Key` header |
| GET | `/api/orders/<id>` | needs `X-Order-Token` header |
| POST | `/api/orders/<id>/payment` | `{ "payment_ref": "..." }`, needs token |

## Admin API (`Authorization: Bearer $ADMIN_TOKEN`)

```
GET  /api/admin/orders?status=payment_submitted&q=&limit=50
GET  /api/admin/orders/<id>                 (full audit trail)
POST /api/admin/orders/<id>/verify-payment  (money received)
POST /api/admin/orders/<id>/complete        {"note": "..."}  (delivered)
POST /api/admin/orders/<id>/reject          {"reason": "..."}
POST /api/admin/orders/<id>/cancel          {"reason": "..."}
POST /api/admin/notify-test                 (sends a test alert)
GET  /api/admin/storage                     (persistent? order count, last backup)
GET  /api/admin/backup | /api/admin/export.csv
POST /api/admin/restore                     (upload a backup; ?force=1 to replace existing)
POST /api/admin/backup-now                  (send + pin a backup in Telegram now)
GET  /api/admin/memberships/expiring?days=2 (renewal reminder list)
GET  /api/admin/reports/weekly?days=7       (orders/revenue per day + per product, Nepal time)
```

Example: `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://YOUR-APP.onrender.com/api/admin/orders?status=payment_submitted`

## Storage and backups

Orders live in one SQLite file (`DATABASE_PATH`). **Where that file lives decides whether you
keep your orders.**

### Free plan (what `render.yaml` sets up)
Render Free cannot attach a disk, and local files are lost on every restart or redeploy,
including waking up after the 15-minute idle sleep. To make that survivable:

1. Set up Telegram alerts (see Order alerts) using a **private chat with your own bot**.
2. Open `/admin` -> Storage and backups -> **Back up to Telegram now**. It should say
   "Backup sent and pinned" and the card should show **Auto-restore: ready**.
3. From then on, every order change is sent to that chat as a database file (at most once per
   minute on temporary storage) and pinned. When the server wakes up with an empty database it
   downloads the newest pinned backup and restores it automatically, tracking links included.

Limits to know about: changes in the last minute before a wipe can be lost; the wake-up takes
longer (Render says about a minute) while it restores; your Telegram chat fills with backup files
(the files hold player IDs and transaction IDs, keep the chat private); Telegram lets bots download
files up to 20 MB (plenty for years of orders). Auto-restore only runs on temporary storage and only
when the database is empty, so it can never overwrite live orders. Keep downloading a manual backup
now and then as a second copy.

### Paid plan (recommended once the shop is busy)
Use `render.paid.yaml` (Starter web service + 1 GB disk at `/var/data`, about $7/month plus about
$0.25/GB for the disk, check Render's pricing page). Orders then survive restarts, Render snapshots
the disk daily, and Telegram backups drop to once an hour. If you already created the service by
hand: add the disk in the dashboard (Disks), mount it at `/var/data`, and set `DATABASE_PATH` to
`/var/data/lama.sqlite3`; restore your latest backup once from the admin page.

The admin page shows a red warning whenever the database is on temporary storage
(`PERSISTENT_STORAGE=true|false` overrides the automatic check).

**Backups** (admin page, "Storage and backups", or the API):
- `GET /api/admin/backup` downloads a consistent copy of the database.
- `GET /api/admin/export.csv` downloads orders for Excel/Sheets (formula-safe).
- With Telegram alerts configured, the database is also sent to your Telegram chat after changes,
  at most once per `BACKUP_INTERVAL_MIN` (default: 1 on temporary storage, 60 on a disk). The file contains customer player IDs and
  transaction IDs, so keep that chat private. Turn off with `BACKUP_TELEGRAM=false`.
- `POST /api/admin/restore` (multipart field `file`) restores a backup. It refuses to replace
  existing orders unless `?force=1` (the admin page asks you to confirm). Customers' tracking links
  keep working after a restore.

## Before going live

1. Free plan: confirm **Auto-restore: ready** in the admin page. Paid plan: put the database on the disk. Either way, tap **Download backup** once.
2. Keep one gunicorn worker (rate limits are in memory, SQLite is single-writer).
3. You can delete the old `GOXTOP_*`, `FF_*_CODE`, `EF_*_CODE` and `DRY_RUN` variables
   from Render. They are no longer read.

## Security notes

- Only `public/` is served. The old layout exposed `app.py` and `.env` over HTTP.
- Prices come from the server catalog, not the browser.
- Customer tracking tokens are stored hashed; unknown order and wrong token look identical.
- Admin brute-force is throttled; all state changes are audited in `order_events`.
