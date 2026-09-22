-- ============================================================
-- Hardware Shop POS — Database Schema
-- Target: Supabase PostgreSQL
-- ============================================================
-- Run this once against your Supabase project's SQL editor,
-- or via psql using the DATABASE_URL from your .env file.
--
-- Design notes:
--   - UUID primary keys (gen_random_uuid(), from pgcrypto,
--     which Supabase enables by default).
--   - Every business table carries shop_id so a shop's data
--     is always scoped and never leaks across shops.
--   - CHECK constraints enforce the simple business rules
--     (no negative stock, valid roles, valid payment methods)
--     directly in the database as a second line of defense
--     behind the application-level validation in Flask.
-- ============================================================

create extension if not exists pgcrypto;

-- ------------------------------------------------------------
-- shops
-- ------------------------------------------------------------
create table if not exists shops (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    created_at  timestamptz not null default now()
);

-- ------------------------------------------------------------
-- profiles (application users: owners and cashiers)
-- Named "profiles" rather than "users" to avoid clashing with
-- Postgres/Supabase's own reserved "auth.users" namespace.
-- ------------------------------------------------------------
create table if not exists profiles (
    id             uuid primary key default gen_random_uuid(),
    shop_id        uuid not null references shops(id) on delete cascade,
    name           text not null,
    email          text not null unique,
    password_hash  text not null,
    role           text not null check (role in ('owner', 'cashier')),
    is_active      boolean not null default true,
    created_at     timestamptz not null default now()
);

create index if not exists idx_profiles_shop_id on profiles(shop_id);

-- ------------------------------------------------------------
-- products
-- ------------------------------------------------------------
create table if not exists products (
    id               uuid primary key default gen_random_uuid(),
    shop_id          uuid not null references shops(id) on delete cascade,
    name             text not null,
    sku              text,
    unit             text not null default 'Piece',
    buying_price     numeric(12, 2) not null default 0 check (buying_price >= 0),
    selling_price    numeric(12, 2) not null default 0 check (selling_price >= 0),
    stock_quantity   numeric(12, 2) not null default 0 check (stock_quantity >= 0),
    minimum_stock    numeric(12, 2) not null default 0 check (minimum_stock >= 0),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create index if not exists idx_products_shop_id on products(shop_id);
create unique index if not exists idx_products_shop_sku
    on products(shop_id, sku) where sku is not null and sku <> '';

-- keep updated_at current on every row change
create or replace function set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_products_updated_at on products;
create trigger trg_products_updated_at
    before update on products
    for each row execute function set_updated_at();

-- ------------------------------------------------------------
-- sales
-- ------------------------------------------------------------
create table if not exists sales (
    id              uuid primary key default gen_random_uuid(),
    shop_id         uuid not null references shops(id) on delete cascade,
    cashier_id      uuid not null references profiles(id),
    total_amount    numeric(12, 2) not null check (total_amount >= 0),
    payment_method  text not null check (
        payment_method in ('Cash', 'M-Pesa', 'Mixx by Yas', 'Airtel Money', 'Bank')
    ),
    created_at      timestamptz not null default now()
);

create index if not exists idx_sales_shop_id on sales(shop_id);
create index if not exists idx_sales_created_at on sales(created_at);

-- ------------------------------------------------------------
-- sale_items
-- ------------------------------------------------------------
create table if not exists sale_items (
    id            uuid primary key default gen_random_uuid(),
    sale_id       uuid not null references sales(id) on delete cascade,
    product_id    uuid not null references products(id),
    quantity      numeric(12, 2) not null check (quantity > 0),
    unit_price    numeric(12, 2) not null check (unit_price >= 0),
    buying_price  numeric(12, 2) not null check (buying_price >= 0),
    subtotal      numeric(12, 2) not null check (subtotal >= 0)
);

create index if not exists idx_sale_items_sale_id on sale_items(sale_id);
create index if not exists idx_sale_items_product_id on sale_items(product_id);

-- ------------------------------------------------------------
-- purchases
-- ------------------------------------------------------------
create table if not exists purchases (
    id              uuid primary key default gen_random_uuid(),
    shop_id         uuid not null references shops(id) on delete cascade,
    user_id         uuid not null references profiles(id),
    supplier_name   text,
    total_amount    numeric(12, 2) not null check (total_amount >= 0),
    created_at      timestamptz not null default now()
);

create index if not exists idx_purchases_shop_id on purchases(shop_id);

-- ------------------------------------------------------------
-- purchase_items
-- ------------------------------------------------------------
create table if not exists purchase_items (
    id             uuid primary key default gen_random_uuid(),
    purchase_id    uuid not null references purchases(id) on delete cascade,
    product_id     uuid not null references products(id),
    quantity       numeric(12, 2) not null check (quantity > 0),
    buying_price   numeric(12, 2) not null check (buying_price >= 0),
    subtotal       numeric(12, 2) not null check (subtotal >= 0)
);

create index if not exists idx_purchase_items_purchase_id on purchase_items(purchase_id);
create index if not exists idx_purchase_items_product_id on purchase_items(product_id);

-- ------------------------------------------------------------
-- expenses
-- ------------------------------------------------------------
create table if not exists expenses (
    id           uuid primary key default gen_random_uuid(),
    shop_id      uuid not null references shops(id) on delete cascade,
    user_id      uuid not null references profiles(id),
    description  text not null,
    category     text not null default 'Other',
    amount       numeric(12, 2) not null check (amount >= 0),
    created_at   timestamptz not null default now()
);

create index if not exists idx_expenses_shop_id on expenses(shop_id);
create index if not exists idx_expenses_created_at on expenses(created_at);

-- ------------------------------------------------------------
-- stock_movements
-- ------------------------------------------------------------
create table if not exists stock_movements (
    id             uuid primary key default gen_random_uuid(),
    shop_id        uuid not null references shops(id) on delete cascade,
    product_id     uuid not null references products(id),
    user_id        uuid not null references profiles(id),
    movement_type  text not null check (movement_type in ('PURCHASE', 'SALE', 'ADJUSTMENT')),
    quantity       numeric(12, 2) not null,
    reference_id   uuid,
    created_at     timestamptz not null default now()
);

create index if not exists idx_stock_movements_shop_id on stock_movements(shop_id);
create index if not exists idx_stock_movements_product_id on stock_movements(product_id);

-- ------------------------------------------------------------
-- activity_logs
-- ------------------------------------------------------------
create table if not exists activity_logs (
    id           uuid primary key default gen_random_uuid(),
    shop_id      uuid not null references shops(id) on delete cascade,
    user_id      uuid references profiles(id),
    action       text not null,
    description  text,
    created_at   timestamptz not null default now()
);

create index if not exists idx_activity_logs_shop_id on activity_logs(shop_id);
create index if not exists idx_activity_logs_created_at on activity_logs(created_at);

-- ============================================================
-- Demo seed data
-- ============================================================
-- Login credentials for the seeded accounts:
--   Owner:   owner@demo.co.tz   / owner123
--   Cashier: cashier@demo.co.tz / cashier123
-- (Change these immediately in any real deployment — the /users
-- page lets the owner add real cashier accounts and deactivate
-- this demo one.)
--
-- Safe to re-run: guarded with WHERE NOT EXISTS checks so it
-- won't create duplicate rows if you run the file twice.

do $$
declare
    v_shop_id       uuid;
    v_owner_id      uuid;
    v_cashier_id    uuid;
    v_cement_id     uuid;
    v_nails_id      uuid;
    v_sale_id       uuid;
    v_purchase_id   uuid;
begin
    if exists (select 1 from shops where name = 'Demo Hardware Shop') then
        raise notice 'Demo data already present — skipping seed.';
        return;
    end if;

    insert into shops (name) values ('Demo Hardware Shop')
        returning id into v_shop_id;

    insert into profiles (shop_id, name, email, password_hash, role)
        values (v_shop_id, 'Shop Owner', 'owner@demo.co.tz',
                'scrypt:32768:8:1$ClawuYqSxBjx2GBN$78dc9429682508f8ef917f7b0e4b1ac628ae097c5784eac9224008e8efb77b24ab6d8609fdcf8ad008d4ceb7b0c87f3ed1023b8cf9bee6f1f308ef9425710c20',
                'owner')
        returning id into v_owner_id;

    insert into profiles (shop_id, name, email, password_hash, role)
        values (v_shop_id, 'Demo Cashier', 'cashier@demo.co.tz',
                'scrypt:32768:8:1$0kYyxQNS4qkLmBkD$118bb432983246a0ad0bed7278144819d4ce5e216c65d9088ffb9c8457c1bf21f303259fa6329becedd39130f315914f465524e7766aa8a51f6181ae021ce283',
                'cashier')
        returning id into v_cashier_id;

    insert into products (shop_id, name, sku, unit, buying_price, selling_price, stock_quantity, minimum_stock)
        values
            (v_shop_id, 'Cement 50kg',    'CEM-50',  'Bag',   18000, 21000, 120, 20),
            (v_shop_id, 'Roofing Sheet',  'RF-SHT',  'Piece', 25000, 30000, 60,  10),
            (v_shop_id, 'PVC Pipe 4"',    'PVC-4',   'Piece', 12000, 15500, 80,  15),
            (v_shop_id, 'Nails 1kg',      'NAIL-1K', 'Kg',    3500,  4500,  150, 25),
            (v_shop_id, 'Paint 20L',      'PNT-20L', 'Piece', 65000, 78000, 25,  5),
            (v_shop_id, 'Binding Wire',   'BW-ROLL', 'Kg',    4000,  5200,  40,  10),
            (v_shop_id, 'Building Blocks','BLK-STD', 'Piece', 800,   1200,  500, 100);

    select id into v_cement_id from products where shop_id = v_shop_id and sku = 'CEM-50';
    select id into v_nails_id  from products where shop_id = v_shop_id and sku = 'NAIL-1K';

    -- Sample purchase: 50 bags of cement from a supplier
    insert into purchases (shop_id, user_id, supplier_name, total_amount)
        values (v_shop_id, v_owner_id, 'XYZ Hardware Supplies', 50 * 18000)
        returning id into v_purchase_id;
    insert into purchase_items (purchase_id, product_id, quantity, buying_price, subtotal)
        values (v_purchase_id, v_cement_id, 50, 18000, 50 * 18000);
    insert into stock_movements (shop_id, product_id, user_id, movement_type, quantity, reference_id)
        values (v_shop_id, v_cement_id, v_owner_id, 'PURCHASE', 50, v_purchase_id);

    -- Sample sale: 5 bags of cement + 2kg nails, cash
    insert into sales (shop_id, cashier_id, total_amount, payment_method)
        values (v_shop_id, v_cashier_id, 5 * 21000 + 2 * 4500, 'Cash')
        returning id into v_sale_id;
    insert into sale_items (sale_id, product_id, quantity, unit_price, buying_price, subtotal)
        values
            (v_sale_id, v_cement_id, 5, 21000, 18000, 5 * 21000),
            (v_sale_id, v_nails_id,  2, 4500,  3500,  2 * 4500);
    insert into stock_movements (shop_id, product_id, user_id, movement_type, quantity, reference_id)
        values
            (v_shop_id, v_cement_id, v_cashier_id, 'SALE', -5, v_sale_id),
            (v_shop_id, v_nails_id,  v_cashier_id, 'SALE', -2, v_sale_id);

    -- Sample expense
    insert into expenses (shop_id, user_id, description, category, amount)
        values (v_shop_id, v_owner_id, 'Fuel for delivery truck', 'Transport', 25000);

    -- Reflect the sample sale/purchase in stock_quantity
    update products set stock_quantity = stock_quantity + 50 where id = v_cement_id;
    update products set stock_quantity = stock_quantity - 5  where id = v_cement_id;
    update products set stock_quantity = stock_quantity - 2  where id = v_nails_id;

    insert into activity_logs (shop_id, user_id, action, description)
        values (v_shop_id, v_owner_id, 'SEED_DATA', 'Demo data created');

    raise notice 'Demo data seeded. Login as owner@demo.co.tz / owner123 or cashier@demo.co.tz / cashier123';
end $$;
