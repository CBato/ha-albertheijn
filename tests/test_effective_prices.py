import os
import sqlite3
import tempfile

import _harness  # noqa: F401 (sets up the Home Assistant stubs)

import importlib
suggest = importlib.import_module("albert_heijn.suggest")
db = importlib.import_module("albert_heijn.db")
items = [
    {"product_id": 502305, "quantity": 1, "price": 2.29, "price_before_bonus": 2.29, "bonus": "3 voor 5.00"},
    {"product_id": 520805, "quantity": 1, "price": 2.29, "price_before_bonus": 2.29, "bonus": "3 voor 5.00"},
    {"product_id": 509423, "quantity": 1, "price": 2.29, "price_before_bonus": 2.29, "bonus": "3 voor 5.00"},
    {"product_id": 61007, "quantity": 1, "price": 1.25, "price_before_bonus": 1.39, "bonus": "10% KORTING"},
    {"product_id": 526381, "quantity": 1, "price": 2.19, "price_before_bonus": 2.19, "bonus": "2 voor 3.49"},
    {"product_id": 377719, "quantity": 4, "price": 2.19, "price_before_bonus": 2.19, "bonus": "1 + 1 gratis"},
    {"product_id": 490856, "quantity": 1, "price": 2.49, "price_before_bonus": 3.19, "bonus": "VOOR 2.49"},
    {"product_id": 62661, "quantity": 1, "price": 1.39, "price_before_bonus": 1.39, "bonus": None},
]
eff = suggest.order_effective_prices(items)
print(eff)
assert eff[502305] == eff[520805] == eff[509423] == 1.667      # 3 voor 5: mixed group of 3 products
assert eff[61007] == 1.25 and eff[490856] == 2.49 and eff[62661] == 1.39   # already in the price / no offer
assert eff[526381] == 2.19                                      # 2 voor 3.49 with only 1: no discount
assert eff[377719] == 1.095                                     # 1+1 with 4
# 5 items in a "3 voor 5" group: 3 discounted, 2 at full price -> average
five = [{"product_id": 1, "quantity": 5, "price": 2.29, "price_before_bonus": 2.29, "bonus": "3 voor 5.00"}]
print("5 in 3-voor-5:", suggest.order_effective_prices(five))
assert suggest.order_effective_prices(five)[1] == round(2.29 - 0.6 * (2.29 - 5 / 3), 3)

# migration: old database without order_items.effective gets the column and the values
path = os.path.join(tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(__file__))), "old.db")
con = sqlite3.connect(path)
con.executescript("""CREATE TABLE orders (order_id INTEGER PRIMARY KEY, date TEXT NOT NULL, status TEXT NOT NULL, updated TEXT);
CREATE TABLE order_items (order_id INTEGER NOT NULL, product_id INTEGER NOT NULL, quantity INTEGER NOT NULL, price REAL,
  price_before_bonus REAL, bonus TEXT, unit_price TEXT, PRIMARY KEY (order_id, product_id));
CREATE TABLE prices (product_id INTEGER NOT NULL, date TEXT NOT NULL, price REAL, price_before_bonus REAL, bonus TEXT, unit_price TEXT,
  PRIMARY KEY (product_id, date));""")
con.execute("INSERT INTO orders VALUES (1, '2026-09-01', 'delivered', NULL)")
con.executemany("INSERT INTO order_items VALUES (1, ?, ?, ?, ?, ?, NULL)",
                [(i["product_id"], i["quantity"], i["price"], i["price_before_bonus"], i["bonus"]) for i in items])
con.commit(); con.close()
db.init(path)
con = sqlite3.connect(path)
got = dict(con.execute("SELECT product_id, effective FROM order_items").fetchall())
print("migrated:", got)
assert got[502305] == 1.667 and got[377719] == 1.095 and got[62661] == 1.39
ph = db.product_history(path, 502305)
print("history price:", ph["purchases"][0]["price"], "list", ph["purchases"][0]["price_list"])
assert ph["purchases"][0]["price"] == 1.667 and ph["purchases"][0]["price_list"] == 2.29
print("EFFECTIVE OK")
