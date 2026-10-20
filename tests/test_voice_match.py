"""Offline test: finding a product by name for "zet ... op de lijst" (suggest.match_products)."""

import _harness  # noqa: F401 (sets up the Home Assistant stubs)

import importlib

suggest = importlib.import_module("albert_heijn.suggest")
match = suggest.match_products

bought = [
    {"product_id": 1, "title": "AH Halfvolle melk", "brand": "AH", "count": 14, "score": 9.0},
    {"product_id": 2, "title": "Campina Volle melk", "brand": "Campina", "count": 2, "score": 1.1},
    {"product_id": 3, "title": "AH Scharrel eieren 10 stuks", "brand": "AH", "count": 6, "score": 4.0},
    {"product_id": 4, "title": "Lay's Paprika chips", "brand": "Lay's", "count": 3, "score": 2.0},
    {"product_id": 5, "title": "Lay's Naturel chips", "brand": "Lay's", "count": 3, "score": 1.8},
    {"product_id": 6, "title": "Crème fraîche", "brand": "AH", "count": 1, "score": 0.5},
    {"product_id": 7, "title": "AH Bananen", "brand": "AH", "count": 0, "score": 0},  # only "Eerder gekocht"
]

# melk: bought much more often than the other one -> clear
opts, clear = match(bought, "melk")
assert [p["product_id"] for p in opts] == [1, 2] and clear, opts
# "een pak melk": filler words ignored
assert match(bought, "een pak melk")[1]
# volle melk: only one product has both words
opts, clear = match(bought, "volle melk")
assert [p["product_id"] for p in opts] == [2] and clear
# chips: two flavours bought about as often -> ask
opts, clear = match(bought, "chips")
assert {p["product_id"] for p in opts} == {4, 5} and not clear
# plural and accents
assert match(bought, "eieren")[0][0]["product_id"] == 3
assert match(bought, "creme fraiche")[0][0]["product_id"] == 6
# only in "Eerder gekocht": one match is clear
opts, clear = match(bought, "bananen")
assert [p["product_id"] for p in opts] == [7] and clear
# nothing
assert match(bought, "pindakaas") == ([], False)
print("VOICE OK")
