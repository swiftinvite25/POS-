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
    """Populate g.user from the session on every request.

    Re-reading from the database (rather than trusting the session
    alone) means a deactivated cashier is locked out on their very
    next request, not just their next login.
    """
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return

    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """SELECT id, shop_id, name, email, role, is_active
               FROM profiles WHERE id = %s""",
            (user_id,),
        )
        user = cur.fetchone()

    if user is None or not user["is_active"]:
        session.clear()
        g.user = None
    else:
        g.user = user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def owner_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.path))
        if g.user["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def log_activity(action, description=""):
    """Record an entry in activity_logs for the current user's shop."""
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """INSERT INTO activity_logs (shop_id, user_id, action, description)
               VALUES (%s, %s, %s, %s)""",
            (g.user["shop_id"], g.user["id"], action, description),
        )


def parse_decimal(value, default="0"):
    """Parse a form field into a plain float, falling back on bad input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


@app.context_processor
def inject_globals():
    return {"current_user": g.get("user"), "payment_methods": PAYMENT_METHODS}


# ------------------------------------------------------------------
# Home / health check
# ------------------------------------------------------------------
@app.route("/")
def index():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user["role"] == "owner":
        return redirect(url_for("dashboard"))
    return redirect(url_for("pos"))


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
                """SELECT id, shop_id, name, password_hash, role, is_active
                   FROM profiles WHERE email = %s""",
                (email,),
            )
            user = cur.fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Incorrect email or password.", "danger")
        elif not user["is_active"]:
            flash("This account has been deactivated. Contact the shop owner.", "danger")
        else:
            session.clear()
            session["user_id"] = str(user["id"])
            g.user = user
            log_activity("LOGIN", f"{user['name']} logged in")
            next_url = request.args.get("next")
            return redirect(next_url or url_for("index"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    if g.user is not None:
        log_activity("LOGOUT", f"{g.user['name']} logged out")
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


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
               FROM sales s JOIN profiles p ON p.id = s.cashier_id
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

    with db.cursor() as cur:
        # Sales history: cashiers see their own sales, owners see all.
        if g.user["role"] == "owner":
            cur.execute(
                """SELECT s.id, s.total_amount, s.payment_method, s.created_at, p.name AS cashier_name
                   FROM sales s JOIN profiles p ON p.id = s.cashier_id
                   WHERE s.shop_id = %s ORDER BY s.created_at DESC LIMIT 20""",
                (shop_id,),
            )
        else:
            cur.execute(
                """SELECT s.id, s.total_amount, s.payment_method, s.created_at, p.name AS cashier_name
                   FROM sales s JOIN profiles p ON p.id = s.cashier_id
                   WHERE s.shop_id = %s AND s.cashier_id = %s
                   ORDER BY s.created_at DESC LIMIT 20""",
                (shop_id, g.user["id"]),
            )
        recent_sales = cur.fetchall()

    return render_template("pos.html", recent_sales=recent_sales)


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
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """SELECT * FROM products
               WHERE shop_id = %s AND (name ILIKE %s OR sku ILIKE %s)
               ORDER BY name ASC""",
            (g.user["shop_id"], f"%{query}%", f"%{query}%"),
        )
        product_list = cur.fetchall()
    return render_template("products.html", products=product_list, query=query)


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
            """SELECT pu.id, pu.supplier_name, pu.total_amount, pu.created_at,
                      pi.quantity, pi.buying_price, pr.name AS product_name, pr.unit
               FROM purchases pu
               JOIN purchase_items pi ON pi.purchase_id = pu.id
               JOIN products pr ON pr.id = pi.product_id
               WHERE pu.shop_id = %s
               ORDER BY pu.created_at DESC LIMIT 30""",
            (shop_id,),
        )
        purchase_history = cur.fetchall()

    return render_template(
        "inventory.html", products=product_list, purchase_history=purchase_history
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

    query = "SELECT * FROM expenses WHERE shop_id = %s"
    params = [shop_id]
    if start_date:
        query += " AND created_at::date >= %s"
        params.append(start_date)
    if end_date:
        query += " AND created_at::date <= %s"
        params.append(end_date)
    query += " ORDER BY created_at DESC LIMIT 100"

    with db.cursor() as cur:
        cur.execute(query, params)
        expense_list = cur.fetchall()
        total = sum(float(e["amount"]) for e in expense_list)

    return render_template(
        "expenses.html",
        expenses=expense_list,
        categories=EXPENSE_CATEGORIES,
        start_date=start_date,
        end_date=end_date,
        total=total,
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
        gross_profit = float(cur.fetchone()["gross_profit"])

        cur.execute(
            """SELECT category, COALESCE(SUM(amount), 0) AS total FROM expenses
               WHERE shop_id = %s AND created_at::date >= %s
               GROUP BY category ORDER BY total DESC""",
            (shop_id, start),
        )
        expenses_by_category = cur.fetchall()
        total_expenses = sum(float(e["total"]) for e in expenses_by_category)

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
# Users (owner only)
# ------------------------------------------------------------------
@app.route("/users", methods=["GET", "POST"])
@owner_required
def users():
    db = get_db()
    shop_id = g.user["shop_id"]

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or len(password) < 6:
            flash("Name, email, and a password of at least 6 characters are required.", "danger")
        else:
            with db.cursor() as cur:
                cur.execute("SELECT id FROM profiles WHERE email = %s", (email,))
                if cur.fetchone() is not None:
                    flash("A user with that email already exists.", "danger")
                else:
                    cur.execute(
                        """INSERT INTO profiles (shop_id, name, email, password_hash, role)
                           VALUES (%s, %s, %s, %s, 'cashier')""",
                        (shop_id, name, email, generate_password_hash(password)),
                    )
                    log_activity("USER_CREATED", f"Added cashier '{name}' ({email})")
                    flash(f"Cashier '{name}' added.", "success")
                    return redirect(url_for("users"))

    with db.cursor() as cur:
        cur.execute(
            """SELECT id, name, email, role, is_active, created_at FROM profiles
               WHERE shop_id = %s ORDER BY created_at ASC""",
            (shop_id,),
        )
        user_list = cur.fetchall()

    return render_template("users.html", users=user_list)


@app.route("/users/<user_id>/toggle", methods=["POST"])
@owner_required
def user_toggle(user_id):
    db = get_db()
    shop_id = g.user["shop_id"]

    if user_id == str(g.user["id"]):
        flash("You cannot deactivate your own account.", "danger")
        return redirect(url_for("users"))

    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, role, is_active FROM profiles WHERE id = %s AND shop_id = %s",
            (user_id, shop_id),
        )
        target = cur.fetchone()
        if target is None:
            abort(404)
        if target["role"] == "owner":
            flash("Owner accounts cannot be deactivated here.", "danger")
            return redirect(url_for("users"))

        new_status = not target["is_active"]
        cur.execute("UPDATE profiles SET is_active = %s WHERE id = %s", (new_status, user_id))

    action = "USER_ACTIVATED" if new_status else "USER_DEACTIVATED"
    log_activity(action, f"{'Activated' if new_status else 'Deactivated'} '{target['name']}'")
    flash(f"{target['name']} {'activated' if new_status else 'deactivated'}.", "success")
    return redirect(url_for("users"))


# ------------------------------------------------------------------
# Activity log (owner only)
# ------------------------------------------------------------------
@app.route("/activity")
@owner_required
def activity():
    db = get_db()
    with db.cursor() as cur:
        cur.execute(
            """SELECT a.id, a.action, a.description, a.created_at, p.name AS user_name
               FROM activity_logs a LEFT JOIN profiles p ON p.id = a.user_id
               WHERE a.shop_id = %s ORDER BY a.created_at DESC LIMIT 200""",
            (g.user["shop_id"],),
        )
        logs = cur.fetchall()
    return render_template("activity.html", logs=logs)


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
