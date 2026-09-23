# Hardware Shop POS

Flask + Supabase Postgres MVP for a small hardware shop: POS, inventory,
expenses, dashboard, and reports for an owner + cashier team.

**Status:** Feature-complete MVP, now multi-tenant with subscription
billing. Auth + roles, POS, products, inventory (purchases), expenses,
dashboard, reports, user management, an activity log, and a platform
admin panel for onboarding client shops and tracking subscription
payments are all implemented.

## Multi-tenant billing

One shared database serves every client shop — every business table
carries `shop_id`, and every query filters by it, so each client's
data is fully isolated even though it's the same Postgres database.
This is the standard SaaS approach and avoids the cost/complexity of
a separate database per client.

**Pricing:** 50,000 TZS/month, or 500,000 TZS/year (a ~2-month discount).
**Grace period:** 7 days after a subscription's period end before the
shop is locked out. Status is computed live from `subscription_period_end`
on every request — no cron job needed:
- **Active** — before `subscription_period_end`
- **Grace** — up to 7 days after, shop still works but a warning banner shows
- **Locked** — beyond the grace period, every page redirects to a
  "Subscription Expired" screen (login/logout still work) until a
  platform admin records a payment

## One login, multiple shops

A login (`users` table) is a **global identity**, separate from which
shop(s) it can access. The `shop_members` table is the join: one row
per (shop, user) pair, with that person's role *on that shop*. This
means:

- The same email/password can own or work at more than one client shop.
- A cashier deactivated on Shop A keeps working normally on Shop B —
  deactivation is per-membership, not global.
- **On login:** if an account belongs to exactly one shop, it logs
  straight in (same one-step flow as before). If it belongs to more
  than one, `/select-shop` shows a picker; a "(switch shop)" link
  appears in the navbar afterward to change shops without logging out.
- **Adding a cashier** (Users page, or the admin's "New Shop" owner
  field): entering an email that already has an account links that
  existing login to the new shop instead of creating a duplicate one
  — just leave the password field blank when reusing an account.

### Platform admin panel (`/admin`)

A separate login, not tied to any shop, for onboarding clients and
recording payments:

| | |
|---|---|
| Login | admin@hardwarepos.co.tz |
| Password | changeme123 |

**Change this immediately** — there's no in-app password change yet, so
update `password_hash` directly in Supabase (generate a new hash with
`python3 -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('yournewpassword'))"`).

From `/admin` you can:
- **Add a new shop** (`/admin/shops/new`) — creates the shop and either
  a new owner account or links an existing one (see above), and records
  the first payment, in one form.
- **View all shops** with live subscription status (active/grace/locked).
- **Record a payment** for any shop — this pushes `subscription_period_end`
  forward (extending from the current period end if still active/in-grace,
  or restarting from today if the shop had lapsed) and unlocks it
  immediately if it was locked.

## Demo login (client-shop side)

The seed data in `database/schema.sql` creates **two** demo shops to
show the multi-shop feature in action — the demo owner account is a
member of both:

| Role    | Email               | Password    | Shops |
|---------|---------------------|-------------|-------|
| Owner   | owner@demo.co.tz    | owner123    | Demo Hardware Shop **and** Branch 2 — try the shop switcher |
| Cashier | cashier@demo.co.tz  | cashier123  | Demo Hardware Shop only |

Change these (or deactivate the demo cashier from the Users page and add
real accounts) before using this for an actual shop.

## Upgrading an existing deployment to multi-shop

If you already ran an earlier version of `schema.sql` (the one with a
`profiles` table), you need a **one-time** reset before the current
`schema.sql` will work — the old single-shop-per-login `profiles`
table is being replaced by `users` + `shop_members`:

1. In Supabase's SQL editor, run `database/migrate_to_multi_shop.sql`.
   **This drops every app table and all data in it** — safe right now
   because there's no real client data yet, but never run it again
   once real shops are live.
2. Then run `database/schema.sql` as usual — it recreates everything
   fresh with the new structure and reseeds the demo shops + admin.

A brand-new deployment (nothing in Supabase yet) can skip step 1 and
just run `schema.sql` directly.

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
- **Users** (`/users`, owner only) — add cashiers (or link an existing
  account — see "One login, multiple shops" above), activate/deactivate
  per-shop (owner accounts can't be deactivated from here; you can't
  deactivate yourself).
- **Activity log** (`/activity`, owner only) — every sale, purchase,
  expense, product change, login/logout, and user change is recorded.
- **Multi-shop login** (`/select-shop`) — a picker for accounts that
  belong to more than one shop, plus a "(switch shop)" link in the nav.
- **Platform admin** (`/admin`) — separate superadmin login (not tied
  to any shop) to onboard new client shops and record subscription
  payments. See "Multi-tenant billing" above.
- **Subscription enforcement** — every shop request checks
  `subscription_period_end` and redirects to a lock page once the
  7-day grace period passes; admin routes are always reachable so
  billing can be fixed even for a locked shop.

Cashiers only see POS (including their own sales history, shown on that
page) — every other nav link and route is owner-only, enforced server-side
via `@owner_required`, not just hidden in the UI.

## Project structure

```text
hardware-pos/
├── app.py                 # Full Flask app — shop routes, admin panel, billing logic
├── requirements.txt
├── runtime.txt             # Pins Python version for Render (psycopg2 compatibility)
├── .env.example
├── database/
│   ├── schema.sql                  # Schema + demo seed data + platform admin seed (safe to re-run)
│   └── migrate_to_multi_shop.sql   # One-time destructive reset — see "Upgrading" above
├── templates/
│   ├── base.html          # Shared layout: navbar (shop or admin), shop switcher, flash messages, grace banner
│   ├── login.html
│   ├── select_shop.html     # Shop picker for multi-shop logins
│   ├── dashboard.html
│   ├── pos.html              # Cart JS lives here (vanilla JS, no build step)
│   ├── products.html
│   ├── product_form.html     # Shared by add + edit
│   ├── inventory.html
│   ├── expenses.html
│   ├── reports.html
│   ├── users.html
│   ├── activity.html
│   ├── locked.html           # Shown to a shop's users once locked out
│   ├── admin_login.html
│   ├── admin_dashboard.html  # All client shops + subscription status
│   ├── admin_shop_new.html   # Onboard a new client shop + owner (or link existing)
│   ├── admin_shop_detail.html # Record payment, view users + payment history
│   └── error.html            # 403 / 404 pages
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
- No online payment gateway — payments are recorded manually by the
  platform admin after receiving money by other means (bank, mobile
  money, cash). Adding an M-Pesa/mobile-money API integration would be
  the natural next step if you want this automated.
- No in-app password change for platform admins — update the hash
  directly in Supabase for now.
