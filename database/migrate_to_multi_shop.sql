-- ============================================================
-- ONE-TIME MIGRATION: profiles -> users + shop_members
-- ============================================================
-- Run this ONCE, before schema.sql, to move from the old
-- single-shop-per-login schema to the new multi-shop one where
-- one login (email) can belong to more than one shop.
--
-- THIS IS DESTRUCTIVE: it drops every app table and all data in
-- them, so anything you've created so far (demo or otherwise)
-- is wiped. That's fine right now because there's no real client
-- data yet — do NOT run this again once real shops are live;
-- schema.sql itself stays safe to re-run any time after this.
--
-- Order matters (children before parents, to satisfy foreign keys):
drop table if exists activity_logs cascade;
drop table if exists stock_movements cascade;
drop table if exists purchase_items cascade;
drop table if exists purchases cascade;
drop table if exists sale_items cascade;
drop table if exists sales cascade;
drop table if exists expenses cascade;
drop table if exists products cascade;
drop table if exists payments cascade;
drop table if exists shop_members cascade;
drop table if exists profiles cascade;   -- the old table this migration removes
drop table if exists users cascade;      -- in case a partial run created it already
drop table if exists shops cascade;
drop table if exists platform_admins cascade;

-- After this runs successfully, run the full database/schema.sql
-- to recreate everything fresh with the new structure and reseed
-- the demo shops + platform admin.
