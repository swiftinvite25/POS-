"""
Hardware Shop POS — Flask application.

Single-file Flask app (per the project's "keep it simple" scope):
  - Session-based auth with hashed passwords, two roles (owner, cashier)
  - POS: product search, cart, checkout with server-side stock validation
  - Products: CRUD + low-stock indicator
  - Inventory: record stock purchases, which increase stock
  - Expenses: record + list, filterable by date
  - Dashboard: today's sales/expenses/profit, stock health, recent activity
  - Reports: daily / weekly / monthly sales, expenses, estimated profit
  - Users: owner can add/deactivate cashiers
  - Activity log: every important write is recorded

Every business table is scoped by shop_id, and every query that
reads or writes business data filters on g.user["shop_id"] — that's
the one rule that must never be broken as this file grows.
"""

import os
import uuid
from datetime import date, datetime, timedelta
from functools import wraps

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
app.config["DATABASE_URL"] = os.environ["DATABASE_URL"]

IS_PRODUCTION = os.environ.get("FLASK_ENV", "development") == "production"

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
)

PAYMENT_METHODS = ["Cash", "M-Pesa", "Mixx by Yas", "Airtel Money", "Bank"]
PRODUCT_UNITS = ["Piece", "Bag", "Box", "Meter", "Kg", "Litre"]
EXPENSE_CATEGORIES = ["Transport", "Electricity", "Rent", "Salary", "Other"]

# ------------------------------------------------------------------
# Subscription billing (TZS)
# ------------------------------------------------------------------
PLAN_PRICES = {"monthly": 50_000, "annual": 500_000}  # annual = ~2 months free
PLAN_LENGTH_DAYS = {"monthly": 30, "annual": 365}
GRACE_PERIOD_DAYS = 7
DEFAULT_PAGE_SIZE = 25


# ------------------------------------------------------------------
# Database connection handling
# ------------------------------------------------------------------
# One connection per request, opened lazily and closed automatically
# when the request ends. Committed on success, rolled back on any
# unhandled exception — this is what makes each route's writes
# (e.g. sale + sale_items + stock update + activity log) atomic
# without needing explicit transaction management in every view.
def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(
            app.config["DATABASE_URL"],
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
    return g.db


def get_page_params(param="page", page_size=DEFAULT_PAGE_SIZE):
    """Return a safe 1-based page and SQL offset for a table."""
    try:
        page = max(1, int(request.args.get(param, 1)))
    except (TypeError, ValueError):
        page = 1
    return page, page_size, (page - 1) * page_size


def make_pagination(total, page, page_size, param):
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    return {
        "page": page,
        "pages": pages,
        "total": total,
        "param": param,
    }


@app.context_processor
def pagination_helpers():
    def pagination_url(param, page):
        values = request.args.to_dict()
        values[param] = page
        return url_for(request.endpoint, **values)

    return {"pagination_url": pagination_url}


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        if exception is None:
            db.commit()
        else:
            db.rollback()
        db.close()


# ------------------------------------------------------------------
# Auth: session loading, decorators, helpers
# ------------------------------------------------------------------
@app.before_request
def load_logged_in_user():
    """Populate g.user (current shop context) and g.user_shops (every
    active shop this login can access) from the session.

    A login (users row) is a global identity — it can belong to more
    than one shop via shop_members. session["shop_id"] holds which
    shop is "active" for this browser session; g.user is None until
    that's set to a shop the user actually has active access to, even
    if they're otherwise authenticated (session["user_id"] present).
    Re-reading from the database every request (rather than trusting
    role/is_active from the session) means a deactivated membership
    or password change takes effect on the very next request.
    """
    g.user = None
    g.user_shops = []

    user_id = session.get("user_id")
    if user_id is None:
        return

    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id, name, email FROM users WHERE id = %s", (user_id,))
        base_user = cur.fetchone()

    if base_user is None:
        session.clear()
        return

    with db.cursor() as cur:
        cur.execute(
            """SELECT sm.shop_id, sm.role, s.name AS shop_name
               FROM shop_members sm JOIN shops s ON s.id = sm.shop_id
               WHERE sm.user_id = %s AND sm.is_active = true
               ORDER BY s.name ASC""",
            (user_id,),
        )
        g.user_shops = cur.fetchall()

    shop_id = session.get("shop_id")
    match = next((m for m in g.user_shops if str(m["shop_id"]) == str(shop_id)), None) if shop_id else None

    if match is not None:
        g.user = {
            "id": base_user["id"],
            "name": base_user["name"],
            "email": base_user["email"],
            "shop_id": match["shop_id"],
            "shop_name": match["shop_name"],
            "role": match["role"],
        }


@app.before_request
def load_logged_in_admin():
    """Populate g.admin from the session for the separate /admin panel.

    Platform admins are a distinct login (no shop_id, no role) —
    kept in their own session key so an admin session and a shop
    user session can never be confused with each other.
    """
    admin_id = session.get("admin_id")
    g.admin = None
    if admin_id is not None:
        g.admin = {"id": admin_id, "name": session.get("admin_name")}


def get_subscription_status(shop):
    """Work out a shop's billing status from its subscription_period_end.

    No cron job needed — status is derived on the fly from today's
    date vs. the stored period end (+ a 7-day grace window), so it's
    always correct the instant a payment is recorded or a deadline
    passes.
    """
    today = date.today()
    period_end = shop["subscription_period_end"]
    grace_end = period_end + timedelta(days=GRACE_PERIOD_DAYS)

    if today <= period_end:
        return {"status": "active", "period_end": period_end, "grace_end": grace_end,
                "days_left": (period_end - today).days}
    if today <= grace_end:
        return {"status": "grace", "period_end": period_end, "grace_end": grace_end,
                "days_left": (grace_end - today).days}
    return {"status": "locked", "period_end": period_end, "grace_end": grace_end,
            "days_left": 0}


# Endpoints reachable even when a shop is locked or a user isn't
# logged in — the lock page itself, logout, and the two login pages.
SUBSCRIPTION_EXEMPT_ENDPOINTS = {"logout", "subscription_locked", "login", "select_shop", "static"}


@app.before_request
def enforce_subscription():
    """Redirect to the lock page once a shop's grace period has passed.

    Runs after load_logged_in_user, so g.user is already set. Admin
    routes (endpoint starts with "admin_") are never subject to this —
    the platform admin must always be able to reach the panel to fix
    a shop's billing, even for a shop that's currently locked.
    """
    if g.user is None:
        return
    if request.endpoint in SUBSCRIPTION_EXEMPT_ENDPOINTS:
        return
    if request.endpoint and request.endpoint.startswith("admin_"):
        return

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT plan_type, subscription_period_end FROM shops WHERE id = %s",
            (g.user["shop_id"],),
        )
        shop = cur.fetchone()

    status = get_subscription_status(shop)
    g.subscription = status
    if status["status"] == "locked":
        return redirect(url_for("subscription_locked"))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if session.get("user_id") and g.user_shops:
                return redirect(url_for("select_shop", next=request.path))
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if session.get("user_id") and g.user_shops:
                return redirect(url_for("select_shop", next=request.path))
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.admin is None:
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def log_activity(action, description="", shop_id=None, user_id=None):
    """Record an entry in activity_logs.

    Defaults to the current shop/user context (g.user); pass explicit
    shop_id/user_id for the rare case of logging before g.user exists
    yet in this request (e.g. right after picking a shop at login).
    """
    shop_id = shop_id if shop_id is not None else g.user["shop_id"]
    user_id = user_id if user_id is not None else g.user["id"]
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO activity_logs (shop_id, user_id, action, description)
               VALUES (%s, %s, %s, %s)""",
            (shop_id, user_id, action, description),
        )


def parse_decimal(value, default="0"):
    """Parse a form field into a plain float, falling back on bad input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@app.context_processor
def inject_globals():
    return {
        "current_user": g.get("user"),
        "user_shops": g.get("user_shops", []),
        "current_admin": g.get("admin"),
        "subscription": g.get("subscription"),
        "payment_methods": PAYMENT_METHODS,
        "plan_prices": PLAN_PRICES,
    }


# ------------------------------------------------------------------
# Home / health check
# ------------------------------------------------------------------
@app.route("/")
def index():
    if g.user is not None:
        if g.user["role"] == "owner":
            return redirect(url_for("dashboard"))
        return redirect(url_for("pos"))
    if session.get("user_id") and g.user_shops:
        return redirect(url_for("select_shop"))
    if g.admin is not None:
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("login"))


# ------------------------------------------------------------------
# Auth routes
# ------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, password_hash FROM users WHERE email = %s",
                (email,),
            )
            user = cur.fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "danger")
        else:
            with db.cursor() as cur:
                cur.execute(
                    """SELECT sm.shop_id, sm.role, s.name AS shop_name
                       FROM shop_members sm JOIN shops s ON s.id = sm.shop_id
                       WHERE sm.user_id = %s AND sm.is_active = true
                       ORDER BY s.name ASC""",
                    (user["id"],),
                )
                memberships = cur.fetchall()

            if not memberships:
                flash("This account has no active shop access. Contact your administrator.", "danger")
            else:
                session.clear()
                session["user_id"] = str(user["id"])
                next_url = request.args.get("next")

                if len(memberships) == 1:
                    # Only one shop — skip the picker and log straight in,
                    # same one-step experience as before multi-shop support.
                    session["shop_id"] = str(memberships[0]["shop_id"])
                    log_activity(
                        "LOGIN", f"{user['name']} logged in",
                        shop_id=memberships[0]["shop_id"], user_id=user["id"],
                    )
                    return redirect(next_url or url_for("index"))

                return redirect(url_for("select_shop", next=next_url) if next_url else url_for("select_shop"))

    return render_template("login.html")


@app.route("/select-shop", methods=["GET", "POST"])
def select_shop():
    """Let a login that belongs to more than one shop pick which one
    is active for this browser session.

    Also doubles as the "switch shop" page — reached via ?switch=1
    from the nav even when a shop is already selected.
    """
    if session.get("user_id") is None:
        return redirect(url_for("login"))
    if g.user is not None and not request.args.get("switch"):
        return redirect(url_for("index"))

    shops = g.user_shops
    if not shops:
        flash("You don't have access to any shop. Contact your administrator.", "danger")
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        shop_id = request.form.get("shop_id")
        match = next((s for s in shops if str(s["shop_id"]) == shop_id), None)
        if match is None:
            flash("Invalid selection.", "danger")
        else:
            session["shop_id"] = shop_id
            log_activity(
                "LOGIN", f"Switched to {match['shop_name']}",
                shop_id=shop_id, user_id=session["user_id"],
            )
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))

    return render_template("select_shop.html", shops=shops)


@app.route("/logout")
def logout():
    if g.user is not None:
        log_activity("LOGOUT", f"{g.user['name']} logged out")
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


@app.route("/subscription-locked")
def subscription_locked():
    """Shown instead of every page once a shop's grace period has passed.

    Reachable while logged in even when enforce_subscription would
    otherwise redirect here — it's in SUBSCRIPTION_EXEMPT_ENDPOINTS —
    so a locked shop's owner can still see what's owed and log out.
    """
    if g.user is None:
        return redirect(url_for("login"))

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT name, plan_type, subscription_period_end FROM shops WHERE id = %s",
            (g.user["shop_id"],),
        )
        shop = cur.fetchone()

    status = get_subscription_status(shop)
    if status["status"] != "locked":
        return redirect(url_for("index"))

    return render_template(
        "locked.html",
        shop_name=shop["name"],
        plan_type=shop["plan_type"],
        amount_due=PLAN_PRICES[shop["plan_type"]],
        period_end=status["period_end"],
        grace_end=status["grace_end"],
    )


# ------------------------------------------------------------------
# Dashboard (owner only)
# ------------------------------------------------------------------
@app.route("/dashboard")
@owner_required
def dashboard():
    db = get_db()
    shop_id = g.user["shop_id"]

    with db.cursor() as cur:
        # Today's summary
        cur.execute(
            """SELECT COUNT(*) AS count, COALESCE(SUM(total_amount), 0) AS total
               FROM sales
               WHERE shop_id = %s AND created_at::date = CURRENT_DATE""",
            (shop_id,),
        )
        sales_today = cur.fetchone()

        cur.execute(
            """SELECT COALESCE(SUM(amount), 0) AS total
               FROM expenses
               WHERE shop_id = %s AND created_at::date = CURRENT_DATE""",
            (shop_id,),
        )
        expenses_today = cur.fetchone()["total"]

        cur.execute(
            """SELECT COALESCE(SUM((si.unit_price - si.buying_price) * si.quantity), 0) AS gross_profit
               FROM sale_items si
               JOIN sales s ON s.id = si.sale_id
               WHERE s.shop_id = %s AND s.created_at::date = CURRENT_DATE""",
            (shop_id,),
        )
        gross_profit_today = cur.fetchone()["gross_profit"]

        # Stock
        cur.execute("SELECT COUNT(*) AS count FROM products WHERE shop_id = %s", (shop_id,))
        total_products = cur.fetchone()["count"]

        cur.execute(
            """SELECT COUNT(*) AS count FROM products
               WHERE shop_id = %s AND stock_quantity <= minimum_stock""",
            (shop_id,),
        )
        low_stock_count = cur.fetchone()["count"]

        cur.execute(
            """SELECT id, name, stock_quantity, minimum_stock, unit FROM products
               WHERE shop_id = %s AND stock_quantity <= minimum_stock
               ORDER BY (stock_quantity - minimum_stock) ASC LIMIT 8""",
            (shop_id,),
        )
        low_stock_products = cur.fetchall()

        # Recent activity
        cur.execute(
            """SELECT s.id, s.total_amount, s.payment_method, s.created_at, p.name AS cashier_name
               FROM sales s JOIN users p ON p.id = s.cashier_id
               WHERE s.shop_id = %s ORDER BY s.created_at DESC LIMIT 6""",
            (shop_id,),
        )
        recent_sales = cur.fetchall()

        cur.execute(
            """SELECT id, supplier_name, total_amount, created_at FROM purchases
               WHERE shop_id = %s ORDER BY created_at DESC LIMIT 6""",
            (shop_id,),
        )
        recent_purchases = cur.fetchall()

        cur.execute(
            """SELECT id, description, category, amount, created_at FROM expenses
               WHERE shop_id = %s ORDER BY created_at DESC LIMIT 6""",
            (shop_id,),
        )
        recent_expenses = cur.fetchall()

    return render_template(
        "dashboard.html",
        sales_today=sales_today,
        expenses_today=expenses_today,
        gross_profit_today=gross_profit_today,
        estimated_profit_today=gross_profit_today - expenses_today,
        total_products=total_products,
        low_stock_count=low_stock_count,
        low_stock_products=low_stock_products,
        recent_sales=recent_sales,
        recent_purchases=recent_purchases,
        recent_expenses=recent_expenses,
    )


# ------------------------------------------------------------------
# POS (owner + cashier)
# ------------------------------------------------------------------
@app.route("/pos")
@login_required
def pos():
    db = get_db()
    shop_id = g.user["shop_id"]
    page, page_size, offset = get_page_params("sales_page")

    with db.cursor() as cur:
        # Sales history: cashiers see their own sales, owners see all.
        if g.user["role"] == "owner":
            cur.execute("SELECT COUNT(*) AS count FROM sales WHERE shop_id = %s", (shop_id,))
            sales_total = cur.fetchone()["count"]
            cur.execute(
                """SELECT s.id, s.total_amount, s.payment_method, s.created_at, p.name AS cashier_name
                   FROM sales s JOIN users p ON p.id = s.cashier_id
                   WHERE s.shop_id = %s ORDER BY s.created_at DESC LIMIT %s OFFSET %s""",
                (shop_id, page_size, offset),
            )
        else:
            cur.execute("SELECT COUNT(*) AS count FROM sales WHERE shop_id = %s AND cashier_id = %s", (shop_id, g.user["id"]))
            sales_total = cur.fetchone()["count"]
            cur.execute(
                """SELECT s.id, s.total_amount, s.payment_method, s.created_at, p.name AS cashier_name
                   FROM sales s JOIN users p ON p.id = s.cashier_id
                   WHERE s.shop_id = %s AND s.cashier_id = %s
                   ORDER BY s.created_at DESC LIMIT %s OFFSET %s""",
                (shop_id, g.user["id"], page_size, offset),
            )
        recent_sales = cur.fetchall()

    return render_template("pos.html", recent_sales=recent_sales,
                           sales_pagination=make_pagination(sales_total, page, page_size, "sales_page"))


@app.route("/pos/search")
@login_required
def pos_search():
    query = request.args.get("q", "").strip()
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """SELECT id, name, sku, unit, selling_price, stock_quantity
               FROM products
               WHERE shop_id = %s AND stock_quantity > 0
                 AND (name ILIKE %s OR sku ILIKE %s)
               ORDER BY name ASC LIMIT 20""",
            (g.user["shop_id"], f"%{query}%", f"%{query}%"),
        )
        products = cur.fetchall()

    return jsonify(
        [
            {
                "id": str(p["id"]),
                "name": p["name"],
                "sku": p["sku"],
                "unit": p["unit"],
                "selling_price": float(p["selling_price"]),
                "stock_quantity": float(p["stock_quantity"]),
            }
            for p in products
        ]
    )


@app.route("/pos/checkout", methods=["POST"])
@login_required
def pos_checkout():
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    payment_method = payload.get("payment_method")

    if not items:
        return jsonify({"error": "Cart is empty."}), 400
    if payment_method not in PAYMENT_METHODS:
        return jsonify({"error": "Invalid payment method."}), 400

    shop_id = g.user["shop_id"]
    db = get_db()

    try:
        with db.cursor() as cur:
            total_amount = 0
            locked_products = {}

            for item in items:
                try:
                    product_id = uuid.UUID(str(item.get("product_id")))
                    quantity = float(item.get("quantity"))
                except (TypeError, ValueError):
                    return jsonify({"error": "Invalid cart item."}), 400

                if quantity <= 0:
                    return jsonify({"error": "Quantity must be greater than zero."}), 400

                # Lock the row so two simultaneous sales of the same
                # product can't both pass the stock check.
                cur.execute(
                    """SELECT id, name, selling_price, buying_price, stock_quantity
                       FROM products WHERE id = %s AND shop_id = %s FOR UPDATE""",
                    (str(product_id), shop_id),
                )
                product = cur.fetchone()
                if product is None:
                    return jsonify({"error": "Product not found."}), 400
                if product["stock_quantity"] < quantity:
                    return jsonify(
                        {"error": f"Not enough stock for {product['name']} "
                                  f"(have {product['stock_quantity']}, need {quantity})."}
                    ), 400

                locked_products[str(product_id)] = {"product": product, "quantity": quantity}
                total_amount += float(product["selling_price"]) * quantity

            # Create the sale
            cur.execute(
                """INSERT INTO sales (shop_id, cashier_id, total_amount, payment_method)
                   VALUES (%s, %s, %s, %s) RETURNING id, created_at""",
                (shop_id, g.user["id"], total_amount, payment_method),
            )
            sale = cur.fetchone()

            for product_id, entry in locked_products.items():
                product = entry["product"]
                quantity = entry["quantity"]
                unit_price = float(product["selling_price"])
                buying_price = float(product["buying_price"])
                subtotal = unit_price * quantity

                cur.execute(
                    """INSERT INTO sale_items
                       (sale_id, product_id, quantity, unit_price, buying_price, subtotal)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (sale["id"], product_id, quantity, unit_price, buying_price, subtotal),
                )

                cur.execute(
                    "UPDATE products SET stock_quantity = stock_quantity - %s WHERE id = %s",
                    (quantity, product_id),
                )

                cur.execute(
                    """INSERT INTO stock_movements
                       (shop_id, product_id, user_id, movement_type, quantity, reference_id)
                       VALUES (%s, %s, %s, 'SALE', %s, %s)""",
                    (shop_id, product_id, g.user["id"], -quantity, sale["id"]),
                )

            log_activity(
                "SALE_CREATED",
                f"Sale of {total_amount:,.0f} TSh ({payment_method}) by {g.user['name']}",
            )
    except Exception:
        db.rollback()
        raise

    return jsonify({"sale_id": str(sale["id"]), "total_amount": total_amount})


# ------------------------------------------------------------------
# Products (owner only)
# ------------------------------------------------------------------
@app.route("/products")
@owner_required
def products():
    query = request.args.get("q", "").strip()
    page, page_size, offset = get_page_params("products_page")
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS count FROM products
               WHERE shop_id = %s AND (name ILIKE %s OR sku ILIKE %s)""",
            (g.user["shop_id"], f"%{query}%", f"%{query}%"),
        )
        products_total = cur.fetchone()["count"]
        cur.execute(
            """SELECT * FROM products
               WHERE shop_id = %s AND (name ILIKE %s OR sku ILIKE %s)
               ORDER BY name ASC LIMIT %s OFFSET %s""",
            (g.user["shop_id"], f"%{query}%", f"%{query}%", page_size, offset),
        )
        product_list = cur.fetchall()
    return render_template("products.html", products=product_list, query=query,
                           products_pagination=make_pagination(products_total, page, page_size, "products_page"))


@app.route("/products/add", methods=["GET", "POST"])
@owner_required
def product_add():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip() or None
        unit = request.form.get("unit", "Piece")
        buying_price = parse_decimal(request.form.get("buying_price"))
        selling_price = parse_decimal(request.form.get("selling_price"))
        stock_quantity = parse_decimal(request.form.get("stock_quantity"))
        minimum_stock = parse_decimal(request.form.get("minimum_stock"))

        if not name:
            flash("Product name is required.", "danger")
        elif buying_price < 0 or selling_price < 0 or stock_quantity < 0 or minimum_stock < 0:
            flash("Prices and quantities cannot be negative.", "danger")
        else:
            db = get_db()
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO products
                       (shop_id, name, sku, unit, buying_price, selling_price,
                        stock_quantity, minimum_stock)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (g.user["shop_id"], name, sku, unit, buying_price, selling_price,
                     stock_quantity, minimum_stock),
                )
            log_activity("PRODUCT_CREATED", f"Added product '{name}'")
            flash(f"Product '{name}' added.", "success")
            return redirect(url_for("products"))

    return render_template(
        "product_form.html", product=None, units=PRODUCT_UNITS, mode="add"
    )


@app.route("/products/edit/<product_id>", methods=["GET", "POST"])
@owner_required
def product_edit(product_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM products WHERE id = %s AND shop_id = %s",
            (product_id, g.user["shop_id"]),
        )
        product = cur.fetchone()

    if product is None:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        sku = request.form.get("sku", "").strip() or None
        unit = request.form.get("unit", "Piece")
        buying_price = parse_decimal(request.form.get("buying_price"))
        selling_price = parse_decimal(request.form.get("selling_price"))
        stock_quantity = parse_decimal(request.form.get("stock_quantity"))
        minimum_stock = parse_decimal(request.form.get("minimum_stock"))

        if not name:
            flash("Product name is required.", "danger")
        elif buying_price < 0 or selling_price < 0 or stock_quantity < 0 or minimum_stock < 0:
            flash("Prices and quantities cannot be negative.", "danger")
        else:
            with db.cursor() as cur:
                cur.execute(
                    """UPDATE products SET name=%s, sku=%s, unit=%s, buying_price=%s,
                       selling_price=%s, stock_quantity=%s, minimum_stock=%s
                       WHERE id=%s AND shop_id=%s""",
                    (name, sku, unit, buying_price, selling_price, stock_quantity,
                     minimum_stock, product_id, g.user["shop_id"]),
                )
            log_activity("PRODUCT_UPDATED", f"Updated product '{name}'")
            flash(f"Product '{name}' updated.", "success")
            return redirect(url_for("products"))

    return render_template(
        "product_form.html", product=product, units=PRODUCT_UNITS, mode="edit"
    )


# ------------------------------------------------------------------
# Inventory / Purchases (owner only)
# ------------------------------------------------------------------
@app.route("/inventory", methods=["GET", "POST"])
@owner_required
def inventory():
    db = get_db()
    shop_id = g.user["shop_id"]
    page, page_size, offset = get_page_params("purchases_page")

    if request.method == "POST":
        product_id = request.form.get("product_id")
        quantity = parse_decimal(request.form.get("quantity"))
        buying_price = parse_decimal(request.form.get("buying_price"))
        supplier_name = request.form.get("supplier_name", "").strip() or None
        update_price = request.form.get("update_price") == "on"

        if not product_id or quantity <= 0 or buying_price < 0:
            flash("Select a product and enter a valid quantity and buying price.", "danger")
        else:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, name FROM products WHERE id = %s AND shop_id = %s",
                    (product_id, shop_id),
                )
                product = cur.fetchone()
                if product is None:
                    flash("Product not found.", "danger")
                    return redirect(url_for("inventory"))

                total_amount = quantity * buying_price

                cur.execute(
                    """INSERT INTO purchases (shop_id, user_id, supplier_name, total_amount)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (shop_id, g.user["id"], supplier_name, total_amount),
                )
                purchase = cur.fetchone()

                cur.execute(
                    """INSERT INTO purchase_items
                       (purchase_id, product_id, quantity, buying_price, subtotal)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (purchase["id"], product_id, quantity, buying_price, total_amount),
                )

                if update_price:
                    cur.execute(
                        """UPDATE products SET stock_quantity = stock_quantity + %s,
                           buying_price = %s WHERE id = %s""",
                        (quantity, buying_price, product_id),
                    )
                else:
                    cur.execute(
                        "UPDATE products SET stock_quantity = stock_quantity + %s WHERE id = %s",
                        (quantity, product_id),
                    )

                cur.execute(
                    """INSERT INTO stock_movements
                       (shop_id, product_id, user_id, movement_type, quantity, reference_id)
                       VALUES (%s, %s, %s, 'PURCHASE', %s, %s)""",
                    (shop_id, product_id, g.user["id"], quantity, purchase["id"]),
                )

            log_activity(
                "PURCHASE_CREATED",
                f"Purchased {quantity} of '{product['name']}' from "
                f"{supplier_name or 'unknown supplier'} for {total_amount:,.0f} TSh",
            )
            flash("Purchase recorded and stock updated.", "success")
            return redirect(url_for("inventory"))

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, unit, buying_price FROM products WHERE shop_id = %s ORDER BY name",
            (shop_id,),
        )
        product_list = cur.fetchall()

        cur.execute(
            """SELECT COUNT(*) AS count
               FROM purchases pu JOIN purchase_items pi ON pi.purchase_id = pu.id
               JOIN products pr ON pr.id = pi.product_id WHERE pu.shop_id = %s""",
            (shop_id,),
        )
        purchases_total = cur.fetchone()["count"]

        cur.execute(
            """SELECT pu.id, pu.supplier_name, pu.total_amount, pu.created_at,
                      pi.quantity, pi.buying_price, pr.name AS product_name, pr.unit
               FROM purchases pu
               JOIN purchase_items pi ON pi.purchase_id = pu.id
               JOIN products pr ON pr.id = pi.product_id
               WHERE pu.shop_id = %s
                    ORDER BY pu.created_at DESC LIMIT %s OFFSET %s""",
                (shop_id, page_size, offset),
        )
        purchase_history = cur.fetchall()

    return render_template(
        "inventory.html", products=product_list, purchase_history=purchase_history,
        purchases_pagination=make_pagination(purchases_total, page, page_size, "purchases_page")
    )


# ------------------------------------------------------------------
# Expenses (owner only)
# ------------------------------------------------------------------
@app.route("/expenses", methods=["GET", "POST"])
@owner_required
def expenses():
    db = get_db()
    shop_id = g.user["shop_id"]

    if request.method == "POST":
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Other")
        amount = parse_decimal(request.form.get("amount"))

        if not description or amount <= 0:
            flash("Enter a description and an amount greater than zero.", "danger")
        else:
            with db.cursor() as cur:
                cur.execute(
                    """INSERT INTO expenses (shop_id, user_id, description, category, amount)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (shop_id, g.user["id"], description, category, amount),
                )
            log_activity("EXPENSE_CREATED", f"{description} — {amount:,.0f} TSh ({category})")
            flash("Expense recorded.", "success")
            return redirect(url_for("expenses"))

    start_date = request.args.get("start_date", "")
    end_date = request.args.get("end_date", "")
    page, page_size, offset = get_page_params("expenses_page")

    filters = " WHERE shop_id = %s"
    params = [shop_id]
    if start_date:
        filters += " AND created_at::date >= %s"
        params.append(start_date)
    if end_date:
        filters += " AND created_at::date <= %s"
        params.append(end_date)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM expenses" + filters, params)
        expenses_total = cur.fetchone()["count"]
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS total FROM expenses" + filters, params)
        total = cur.fetchone()["total"]
        cur.execute("SELECT * FROM expenses" + filters + " ORDER BY created_at DESC LIMIT %s OFFSET %s", params + [page_size, offset])
        expense_list = cur.fetchall()

    return render_template(
        "expenses.html",
        expenses=expense_list,
        categories=EXPENSE_CATEGORIES,
        start_date=start_date,
        end_date=end_date,
        total=total,
        expenses_pagination=make_pagination(expenses_total, page, page_size, "expenses_page"),
    )


# ------------------------------------------------------------------
# Reports (owner only)
# ------------------------------------------------------------------
@app.route("/reports")
@owner_required
def reports():
    period = request.args.get("period", "daily")
    shop_id = g.user["shop_id"]

    today = date.today()
    if period == "weekly":
        start = today - timedelta(days=today.weekday())
        label = f"Week of {start.strftime('%d %b %Y')}"
    elif period == "monthly":
        start = today.replace(day=1)
        label = today.strftime("%B %Y")
    else:
        period = "daily"
        start = today
        label = today.strftime("%d %b %Y")

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """SELECT COUNT(*) AS count, COALESCE(SUM(total_amount), 0) AS total
               FROM sales WHERE shop_id = %s AND created_at::date >= %s""",
            (shop_id, start),
        )
        sales_summary = cur.fetchone()

        cur.execute(
            """SELECT COALESCE(SUM((si.unit_price - si.buying_price) * si.quantity), 0) AS gross_profit
               FROM sale_items si JOIN sales s ON s.id = si.sale_id
               WHERE s.shop_id = %s AND s.created_at::date >= %s""",
            (shop_id, start),
        )
        gross_profit = float(cur.fetchone()["gross_profit"] or 0)

        cur.execute(
            """SELECT category, COALESCE(SUM(amount), 0) AS total FROM expenses
               WHERE shop_id = %s AND created_at::date >= %s
               GROUP BY category ORDER BY total DESC""",
            (shop_id, start),
        )
        expenses_by_category = cur.fetchall()
        total_expenses = float(sum(float(e["total"]) for e in expenses_by_category))

        cur.execute(
            """SELECT s.created_at::date AS day, COUNT(*) AS count, SUM(s.total_amount) AS total
               FROM sales s WHERE s.shop_id = %s AND s.created_at::date >= %s
               GROUP BY day ORDER BY day ASC""",
            (shop_id, start),
        )
        sales_by_day = cur.fetchall()

    return render_template(
        "reports.html",
        period=period,
        label=label,
        sales_summary=sales_summary,
        gross_profit=gross_profit,
        total_expenses=total_expenses,
        estimated_profit=gross_profit - total_expenses,
        expenses_by_category=expenses_by_category,
        sales_by_day=sales_by_day,
    )


# ------------------------------------------------------------------
# Users (owner only) — manages this shop's membership. A person's
# login (users table) is global, so adding a cashier by an email
# that already exists elsewhere links that same login to this shop
# instead of creating a second account for them.
# ------------------------------------------------------------------
@app.route("/users", methods=["GET", "POST"])
@owner_required
def users():
    db = get_db()
    shop_id = g.user["shop_id"]
    page, page_size, offset = get_page_params("users_page")

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email:
            flash("Email is required.", "danger")
            return redirect(url_for("users"))

        with db.cursor() as cur:
            cur.execute("SELECT id, name FROM users WHERE email = %s", (email,))
            existing = cur.fetchone()

            if existing is not None:
                cur.execute(
                    "SELECT is_active FROM shop_members WHERE shop_id = %s AND user_id = %s",
                    (shop_id, existing["id"]),
                )
                membership = cur.fetchone()

                if membership is not None and membership["is_active"]:
                    flash(f"{existing['name']} is already an active member of this shop.", "danger")
                elif membership is not None:
                    cur.execute(
                        """UPDATE shop_members SET is_active = true, role = 'cashier'
                           WHERE shop_id = %s AND user_id = %s""",
                        (shop_id, existing["id"]),
                    )
                    log_activity("USER_ACTIVATED", f"Re-activated '{existing['name']}' on this shop")
                    flash(f"{existing['name']}'s existing account was re-activated as a cashier here.", "success")
                else:
                    cur.execute(
                        "INSERT INTO shop_members (shop_id, user_id, role) VALUES (%s, %s, 'cashier')",
                        (shop_id, existing["id"]),
                    )
                    log_activity("USER_CREATED", f"Linked existing account '{existing['name']}' as cashier")
                    flash(f"Linked {existing['name']}'s existing login as a cashier on this shop.", "success")
                return redirect(url_for("users"))

            if not name or len(password) < 6:
                flash("For a new account, name and a password of at least 6 characters "
                      "are required. To reuse an existing login instead, enter that "
                      "person's existing email and leave the password blank.", "danger")
            else:
                cur.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                    (name, email, generate_password_hash(password)),
                )
                new_user = cur.fetchone()
                cur.execute(
                    "INSERT INTO shop_members (shop_id, user_id, role) VALUES (%s, %s, 'cashier')",
                    (shop_id, new_user["id"]),
                )
                log_activity("USER_CREATED", f"Added cashier '{name}' ({email})")
                flash(f"Cashier '{name}' added.", "success")
                return redirect(url_for("users"))

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM shop_members WHERE shop_id = %s", (shop_id,))
        users_total = cur.fetchone()["count"]
        cur.execute(
            """SELECT u.id, u.name, u.email, sm.role, sm.is_active, sm.created_at
               FROM shop_members sm JOIN users u ON u.id = sm.user_id
                    WHERE sm.shop_id = %s ORDER BY sm.created_at ASC LIMIT %s OFFSET %s""",
                (shop_id, page_size, offset),
        )
        user_list = cur.fetchall()

    return render_template("users.html", users=user_list,
                           users_pagination=make_pagination(users_total, page, page_size, "users_page"))


@app.route("/users/<user_id>/toggle", methods=["POST"])
@owner_required
def user_toggle(user_id):
    """Toggle this person's membership on THIS shop only — deactivating
    them here has no effect on any other shop the same login belongs to.
    """
    db = get_db()
    shop_id = g.user["shop_id"]

    if user_id == str(g.user["id"]):
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("users"))

    with db.cursor() as cur:
        cur.execute(
            """SELECT sm.role, sm.is_active, u.name FROM shop_members sm
               JOIN users u ON u.id = sm.user_id
               WHERE sm.shop_id = %s AND sm.user_id = %s""",
            (shop_id, user_id),
        )
        target = cur.fetchone()
        if target is None:
            abort(404)
        if target["role"] == "owner":
            flash("Owner accounts cannot be deactivated here.", "danger")
            return redirect(url_for("users"))

        new_status = not target["is_active"]
        cur.execute(
            "UPDATE shop_members SET is_active = %s WHERE shop_id = %s AND user_id = %s",
            (new_status, shop_id, user_id),
        )

    action = "USER_ACTIVATED" if new_status else "USER_DEACTIVATED"
    log_activity(action, f"{'Activated' if new_status else 'Deactivated'} '{target['name']}' on this shop")
    flash(f"{target['name']} {'activated' if new_status else 'deactivated'} on this shop.", "success")
    return redirect(url_for("users"))


# ------------------------------------------------------------------
# Activity log (owner only)
# ------------------------------------------------------------------
@app.route("/activity")
@owner_required
def activity():
    db = get_db()
    page, page_size, offset = get_page_params("activity_page")
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM activity_logs WHERE shop_id = %s", (g.user["shop_id"],))
        logs_total = cur.fetchone()["count"]
        cur.execute(
            """SELECT a.id, a.action, a.description, a.created_at, p.name AS user_name
               FROM activity_logs a LEFT JOIN users p ON p.id = a.user_id
                    WHERE a.shop_id = %s ORDER BY a.created_at DESC LIMIT %s OFFSET %s""",
                (g.user["shop_id"], page_size, offset),
        )
        logs = cur.fetchall()
    return render_template("activity.html", logs=logs,
                           logs_pagination=make_pagination(logs_total, page, page_size, "activity_page"))


# ------------------------------------------------------------------
# Platform admin panel — onboards client shops, records payments
# ------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if g.admin is not None:
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        db = get_db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, password_hash FROM platform_admins WHERE email = %s",
                (email,),
            )
            admin = cur.fetchone()

        if admin is None or not check_password_hash(admin["password_hash"], password):
            flash("Incorrect email or password.", "danger")
        else:
            session.clear()
            session["admin_id"] = str(admin["id"])
            session["admin_name"] = admin["name"]
            g.admin = {"id": admin["id"], "name": admin["name"]}
            next_url = request.args.get("next")
            return redirect(next_url or url_for("admin_dashboard"))

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Logged out of the admin panel.", "info")
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    db = get_db()
    page, page_size, offset = get_page_params("shops_page")
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS count FROM shops")
        shops_total = cur.fetchone()["count"]
        cur.execute(
            """SELECT s.id, s.name, s.plan_type, s.subscription_period_end, s.created_at,
                      (SELECT u.name FROM shop_members sm JOIN users u ON u.id = sm.user_id
                       WHERE sm.shop_id = s.id AND sm.role = 'owner'
                       ORDER BY sm.created_at ASC LIMIT 1) AS owner_name,
                      (SELECT u.email FROM shop_members sm JOIN users u ON u.id = sm.user_id
                       WHERE sm.shop_id = s.id AND sm.role = 'owner'
                       ORDER BY sm.created_at ASC LIMIT 1) AS owner_email
                    FROM shops s ORDER BY s.created_at DESC LIMIT %s OFFSET %s""",
                (page_size, offset),
        )
        shops = cur.fetchall()

    shop_rows = []
    for shop in shops:
        status = get_subscription_status(shop)
        shop_rows.append({**shop, **status})

    return render_template("admin_dashboard.html", shops=shop_rows,
                           shops_pagination=make_pagination(shops_total, page, page_size, "shops_page"))


@app.route("/admin/shops/new", methods=["GET", "POST"])
@admin_required
def admin_shop_new():
    """Create a client shop. If the owner's email already belongs to
    an existing login (e.g. they already own another shop), that login
    is linked as owner here too instead of creating a duplicate account
    — this is what makes "one login, multiple shops" possible.
    """
    if request.method == "POST":
        shop_name = request.form.get("shop_name", "").strip()
        owner_name = request.form.get("owner_name", "").strip()
        owner_email = request.form.get("owner_email", "").strip().lower()
        owner_password = request.form.get("owner_password", "")
        plan_type = request.form.get("plan_type", "monthly")
        amount = parse_decimal(request.form.get("amount"), default=str(PLAN_PRICES.get(plan_type, 0)))

        if plan_type not in PLAN_PRICES:
            flash("Invalid plan type.", "danger")
            return render_template("admin_shop_new.html", plan_prices=PLAN_PRICES, form=request.form)
        if not shop_name or not owner_email:
            flash("Shop name and owner email are required.", "danger")
            return render_template("admin_shop_new.html", plan_prices=PLAN_PRICES, form=request.form)

        db = get_db()
        with db.cursor() as cur:
            cur.execute("SELECT id, name FROM users WHERE email = %s", (owner_email,))
            existing = cur.fetchone()

            if existing is None and (not owner_name or len(owner_password) < 6):
                flash("For a brand-new owner, name and a password of at least 6 characters "
                      "are required. To make an existing account the owner here too, just "
                      "enter their email and leave the password blank.", "danger")
                return render_template("admin_shop_new.html", plan_prices=PLAN_PRICES, form=request.form)

            today = date.today()
            period_end = today + timedelta(days=PLAN_LENGTH_DAYS[plan_type])

            cur.execute(
                """INSERT INTO shops (name, plan_type, subscription_period_end)
                   VALUES (%s, %s, %s) RETURNING id""",
                (shop_name, plan_type, period_end),
            )
            shop = cur.fetchone()

            if existing is not None:
                owner_id = existing["id"]
                note = f"linked existing account ({existing['name']}, {owner_email}) as owner"
            else:
                cur.execute(
                    "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s) RETURNING id",
                    (owner_name, owner_email, generate_password_hash(owner_password)),
                )
                owner_id = cur.fetchone()["id"]
                note = f"created new owner account for {owner_email}"

            cur.execute(
                "INSERT INTO shop_members (shop_id, user_id, role) VALUES (%s, %s, 'owner')",
                (shop["id"], owner_id),
            )

            cur.execute(
                """INSERT INTO payments (shop_id, plan_type, amount, period_start,
                   period_end, reference, recorded_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (shop["id"], plan_type, amount, today, period_end,
                 "Initial signup", g.admin["id"]),
            )

        flash(f"Shop '{shop_name}' created — {note}.", "success")
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_shop_new.html", plan_prices=PLAN_PRICES, form={})


@app.route("/admin/shops/<shop_id>")
@admin_required
def admin_shop_detail(shop_id):
    db = get_db()
    users_page, page_size, users_offset = get_page_params("shop_users_page")
    payments_page, _, payments_offset = get_page_params("payments_page")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM shops WHERE id = %s", (shop_id,))
        shop = cur.fetchone()
        if shop is None:
            abort(404)

        cur.execute("SELECT COUNT(*) AS count FROM shop_members WHERE shop_id = %s", (shop_id,))
        shop_users_total = cur.fetchone()["count"]
        cur.execute(
            """SELECT u.id, u.name, u.email, sm.role, sm.is_active
               FROM shop_members sm JOIN users u ON u.id = sm.user_id
               WHERE sm.shop_id = %s ORDER BY sm.created_at ASC LIMIT %s OFFSET %s""",
            (shop_id, page_size, users_offset),
        )
        shop_users = cur.fetchall()

        cur.execute("SELECT COUNT(*) AS count FROM payments WHERE shop_id = %s", (shop_id,))
        payments_total = cur.fetchone()["count"]
        cur.execute(
            """SELECT amount, plan_type, period_start, period_end, reference, created_at
                    FROM payments WHERE shop_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s""",
                (shop_id, page_size, payments_offset),
        )
        payment_history = cur.fetchall()

    status = get_subscription_status(shop)
    return render_template(
        "admin_shop_detail.html",
        shop=shop,
        status=status,
        shop_users=shop_users,
        payment_history=payment_history,
        plan_prices=PLAN_PRICES,
        shop_users_pagination=make_pagination(shop_users_total, users_page, page_size, "shop_users_page"),
        payments_pagination=make_pagination(payments_total, payments_page, page_size, "payments_page"),
    )


@app.route("/admin/shops/<shop_id>/payment", methods=["POST"])
@admin_required
def admin_shop_payment(shop_id):
    db = get_db()
    with db.cursor() as cur:
        cur.execute("SELECT id, subscription_period_end FROM shops WHERE id = %s", (shop_id,))
        shop = cur.fetchone()
        if shop is None:
            abort(404)

        plan_type = request.form.get("plan_type", "monthly")
        amount = parse_decimal(request.form.get("amount"), default=str(PLAN_PRICES.get(plan_type, 0)))
        reference = request.form.get("reference", "").strip() or None

        if plan_type not in PLAN_PRICES:
            flash("Invalid plan type.", "danger")
            return redirect(url_for("admin_shop_detail", shop_id=shop_id))

        today = date.today()
        current_end = shop["subscription_period_end"]
        # Paying while still active/in-grace extends from the current
        # period end (no paid days lost); paying after a lockout starts
        # the new period from today instead.
        period_start = current_end if current_end >= today else today
        period_end = period_start + timedelta(days=PLAN_LENGTH_DAYS[plan_type])

        cur.execute(
            "UPDATE shops SET plan_type = %s, subscription_period_end = %s WHERE id = %s",
            (plan_type, period_end, shop_id),
        )
        cur.execute(
            """INSERT INTO payments (shop_id, plan_type, amount, period_start,
               period_end, reference, recorded_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (shop_id, plan_type, amount, period_start, period_end, reference, g.admin["id"]),
        )

    flash(f"Payment recorded — subscription now runs to {period_end.strftime('%d %b %Y')}.", "success")
    return redirect(url_for("admin_shop_detail", shop_id=shop_id))


# ------------------------------------------------------------------
# Error handlers
# ------------------------------------------------------------------
@app.errorhandler(403)
def forbidden(_e):
    return render_template("error.html", code=403, message="You don't have access to that page."), 403


@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html", code=404, message="Page not found."), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=not IS_PRODUCTION)
