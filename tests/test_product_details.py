"""Offline test: products without title/category are looked up once a week, and the till receipt name is the fallback."""

import os

from _harness import tmp  # sets up the Home Assistant stubs

import importlib

db = importlib.import_module("albert_heijn.db")
path = os.path.join(tmp, "details.db")
db.init(path)
now, week_ago = "2026-10-20T04:15:00+00:00", "2026-10-13T04:15:00+00:00"

# 1: only known from a till receipt; 2: from an order without a title; 3: complete
db.save_receipt(path, "r1", "2026-10-01", 3.0, [{"pos_id": 501, "name": "AH APPELSAP", "quantity": 1, "amount": 1.5}], now)
db.save_pos_map(path, {501: 1})
db.save_order(path, 10, "2026-10-04", "delivered", [{"product_id": 2, "title": "", "quantity": 1, "price": 2.0}], now)
db.fill_product_details(path, [{"product_id": 3, "title": "AH Melk", "category": "Zuivel", "image_url": "x"}], now)

missing = sorted(db.missing_details(path, week_ago, 100))
assert missing == [1, 2], missing
db.mark_details_tried(path, missing, now)
assert db.missing_details(path, week_ago, 100) == []  # tried this week: not again
assert sorted(db.missing_details(path, "2026-10-21", 100)) == [1, 2]  # a week later: again

# AH returns details for 2 -> filled; 1 is no longer sold online -> till name
db.fill_product_details(path, [{"product_id": 2, "title": "AH Pindakaas", "category": "Ontbijt", "image_url": "y"}], now)
titles = {p["product_id"]: p["title"] for p in db.products_overview(path)}
assert titles[2] == "AH Pindakaas" and titles[1] == "AH appelsap", titles
assert db.count_missing_details(path) == 1
print("DETAILS OK")
