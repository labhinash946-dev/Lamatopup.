# Lama Topup backend

Flask + SQLite. Customers place orders, you verify the Fonepay/eSewa payment, and only then
the backend buys from GoXtop. Weekly/monthly memberships are tracked with expiry dates.

## Order flow

`awaiting_payment` -> `payment_submitted` -> `processing` -> `completed`
(or `rejected`, `cancelled`, `failed` -> `retry`).

1. Customer: `POST /api/orders` -> gets `order.id` and a one-time `tracking_token`.
   Sending `payment_ref` (transaction ID) puts it straight into `payment_submitted`.
2. You check your Fonepay/eSewa statement, then call `verify-payment`. **Nothing is sent
   to the supplier before this step.**
3. Backend calls GoXtop with `partner_orderid = order.id` (never re-sent blindly), then
   `sync` polls status until `completed`.

## Run

```
pip install -r requirements.txt
cp .env.example .env      # set ADMIN_TOKEN (32+ chars)
python app.py             # DRY_RUN=true: supplier is never called
python -m unittest discover -s tests
```

`DRY_RUN=false` refuses to start without `GOXTOP_API_KEY` and `ADMIN_TOKEN`.

## Public API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | DB check, used by Render |
| GET | `/api/catalog` | products and server-side prices |
| GET | `/api/check-player?id=` | Free Fire name check (convenience only) |
| POST | `/api/orders` | optional `Idempotency-Key` header |
| GET | `/api/orders/<id>` | needs `X-Order-Token` header |
| POST | `/api/orders/<id>/payment` | `{ "payment_ref": "..." }`, needs token |

## Admin API (`Authorization: Bearer $ADMIN_TOKEN`)

```
GET  /api/admin/orders?status=payment_submitted&q=&limit=50
GET  /api/admin/orders/<id>                 (full audit trail)
POST /api/admin/orders/<id>/verify-payment  (money received -> fulfil)
POST /api/admin/orders/<id>/reject          {"reason": "..."}
POST /api/admin/orders/<id>/cancel
POST /api/admin/orders/<id>/complete        (manual delivery, e.g. eFootball)
POST /api/admin/orders/<id>/retry           (only if supplier definitively rejected)
POST /api/admin/orders/<id>/sync            POST /api/admin/sync  (all processing)
GET  /api/admin/memberships/expiring?days=2 (weekly renewal reminder list)
GET  /api/admin/reports/weekly?days=7       (orders/revenue per day + per product, Nepal time)
GET  /api/admin/supplier/balance | games | products/<game_code>
```

Example: `curl -H "Authorization: Bearer $ADMIN_TOKEN" https://YOUR-APP.onrender.com/api/admin/orders?status=payment_submitted`

## Before going live

1. **Persistence.** Orders are stored in SQLite. Free Render instances lose the file on
   every deploy. Attach a persistent disk (see `render.yaml`) or accept data loss.
2. Set the `FF_*_CODE` values to the exact GoXtop `Pack` values from
   `GET /api/admin/supplier/products/freefire_global`.
3. Run one real test order with `DRY_RUN=false`, then open it in the admin API and check
   `supplier_response`. The GoXtop status vocabulary is not documented in this repo; if
   `sync` does not move the order to `completed`, adjust `DONE`/`FAILED` in `lama/goxtop.py`.
4. eFootball has no supplier code yet, so those orders are `manual_fulfilment`: deliver,
   then call `complete`. Set `EF_<n>_CODE` to automate later.
5. Keep one gunicorn worker (rate limits are in memory, SQLite is single-writer).

## Security notes

- Only `public/` is served. The old layout exposed `app.py` and `.env` over HTTP.
- Prices come from the server catalog, not the browser.
- Supplier balance/games/products moved behind the admin token.
- Customer tracking tokens are stored hashed; unknown order and wrong token look identical.
- Admin brute-force is throttled; all state changes are audited in `order_events`.
