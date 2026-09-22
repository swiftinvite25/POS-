# Hardware Shop POS

Flask + Supabase Postgres MVP for a small hardware shop: POS, inventory,
expenses, dashboard, and reports for an owner + cashier team.

**Status:** Feature-complete MVP. Auth + roles, POS, products, inventory
(purchases), expenses, dashboard, reports, user management, and an
activity log are all implemented.

## Demo login

The seed data in `database/schema.sql` creates one shop with two accounts:

| Role    | Email               | Password    |
|---------|---------------------|-------------|
| Owner   | owner@demo.co.tz    | owner123    |
| Cashier | cashier@demo.co.tz  | cashier123  |

Change these (or deactivate the demo cashier from the Users page and add
real accounts) before using this for an actual shop.

## 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure `.env`

```bash
cp .env.example .env
```

Then fill in:

- `DATABASE_URL` — from your Supabase project: **Settings → Database →
  Connection string (URI)**.
- `SECRET_KEY` — generate with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `FLASK_ENV` — `development` locally, `production` on Render.

Never commit `.env` (it's already in `.gitignore`).

## 3. Create the database schema

Open your Supabase project's **SQL Editor** and run the contents of
`database/schema.sql`, or via `psql`:

```bash
psql "$DATABASE_URL" -f database/schema.sql
```

## 4. Run locally

```bash
python app.py
```

Visit `http://localhost:5000` — you'll be redirected to `/login`. Sign in
with one of the demo accounts above.

## 5. Deploy to Render

1. Push this repo to GitHub.
2. On Render: **New → Web Service**, connect the repo.
3. Build command: `pip install -r requirements.txt`
4. Start command: `gunicorn app:app`
5. Add environment variables `DATABASE_URL`, `SECRET_KEY`, `FLASK_ENV=production`
   in the Render dashboard (do not commit them).
6. Render sets `PORT` automatically — `app.py` already reads it.

## What's implemented

- **Auth & roles** — session-based login, hashed passwords (werkzeug
  scrypt), `login_required` / `owner_required` decorators. Every route
  that touches business data filters by the logged-in user's `shop_id`.
- **POS** (`/pos`) — live product search, cart with editable quantities,
  checkout that re-validates stock server-side (with row locking) before
  creating the sale, updating stock, and logging a stock movement.
- **Products** (`/products`) — list with search and a low-stock badge,
  add/edit forms.
- **Inventory** (`/inventory`) — record supplier purchases, which
  increase stock and optionally update the buying price; purchase
  history table.
- **Expenses** (`/expenses`) — record + list, filterable by date range.
- **Dashboard** (`/dashboard`, owner only) — today's sales, expenses,
  estimated profit, low-stock list, recent sales/purchases/expenses.
- **Reports** (`/reports`, owner only) — daily / weekly / monthly sales,
  expenses by category, and estimated profit.
- **Users** (`/users`, owner only) — add cashiers, activate/deactivate
  (owner accounts can't be deactivated from here; you can't deactivate
  yourself).
- **Activity log** (`/activity`, owner only) — every sale, purchase,
  expense, product change, login/logout, and user change is recorded.

Cashiers only see POS (including their own sales history, shown on that
page) — every other nav link and route is owner-only, enforced server-side
via `@owner_required`, not just hidden in the UI.

## Project structure

```text
hardware-pos/
├── app.py                 # Full Flask app — routes, auth, all features
├── requirements.txt
├── .env.example
├── database/
│   └── schema.sql         # Schema + demo seed data (shop, 2 users, products, a sale/purchase/expense)
├── templates/
│   ├── base.html          # Shared layout: navbar, flash messages
│   ├── login.html
│   ├── dashboard.html
│   ├── pos.html            # Cart JS lives here (vanilla JS, no build step)
│   ├── products.html
│   ├── product_form.html   # Shared by add + edit
│   ├── inventory.html
│   ├── expenses.html
│   ├── reports.html
│   ├── users.html
│   ├── activity.html
│   └── error.html          # 403 / 404 pages
└── static/
    ├── css/style.css
    └── js/
```

## Known limitations (intentional, for MVP scope)

- No password-reset flow — the owner resets a forgotten password
  directly in Supabase for now (update `password_hash` with a value
  from `werkzeug.security.generate_password_hash`).
- No pagination on activity log / purchase / sales history — capped at
  a fixed row limit instead, which is enough for a single small shop.
- "Estimated Profit" excludes unsold inventory value and taxes — it's
  sales gross profit minus expenses only, as specified.
