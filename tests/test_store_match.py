"""Offline test: pack sizes and the match score between your product and one at another store (suggest.py)."""

import _harness  # noqa: F401 (sets up the Home Assistant stubs)

import importlib

s = importlib.import_module("albert_heijn.suggest")

sizes = {"450 g": (450, "g"), "6 x 33 cl": (1980, "ml"), "1,5 liter": (1500, "ml"), "per stuk": (1, "st"),
         "10 stuks": (10, "st"), "ca. 500 gram": (500, "g"), "0,75 l": (750, "ml"), "1L": (1000, "ml"), "5 Stück": (5, "st"),
         "Nutella 630g": (630, "g"), "": None}
for text, want in sizes.items():
    assert s.parse_size(text) == want, (text, s.parse_size(text))
assert s.unit_price(3.79, (650, "g")) == 5.831 and s.unit_price(2.5, (10, "st")) == 0.25


def score(mine, theirs):
    return s.match_score(dict(zip(("title", "brand", "unit_size"), mine)), dict(zip(("title", "unit"), theirs)))


# same product, other size
assert score(("Calvé Pindakaas", "Calvé", "450 g"), ("Calvé pindakaas", "650 gram")) == {"score": 95, "kind": "size"}
# a branded product never matches a store brand
assert score(("Calvé Pindakaas", "Calvé", "450 g"), ("Picnic pindakaas", "450 gram"))["score"] < s.MATCH_MIN_SCORE
# store brand against store brand
assert score(("AH Halfvolle melk", "AH", "1 l"), ("Picnic halfvolle melk", "1 liter")) == {"score": 95, "kind": "same"}
assert score(("AH Halfvolle melk", "AH", "1 l"), ("Picnic volle melk", "1 liter"))["score"] < s.MATCH_MIN_SCORE
# written apart / together
assert score(("AH Scharrel eieren", "AH", "10 stuks"), ("Picnic scharreleieren", "10 stuks"))["score"] >= 90
# another flavour or variant is not the same
assert score(("Lay's Paprika chips", "Lay's", "225 g"), ("Lay's Naturel chips", "225 gram"))["score"] < s.MATCH_MIN_SCORE
assert score(("Coca-Cola Regular", "Coca-Cola", "1,5 l"), ("Coca-Cola Zero sugar", "1,5 liter"))["score"] < s.MATCH_MIN_SCORE
assert score(("AH Pindakaas met stukjes", "AH", "350 g"), ("Picnic pindakaas", "350 gram"))["score"] < s.MATCH_MIN_SCORE
# pieces vs weight: similar, can't recalculate
assert score(("AH Bananen", "AH", "5 stuks"), ("Picnic bananen", "1 kg"))["kind"] == "similar"

# real examples: bio = biologisch, packaging words, compound words, weak adjectives, "per kilo" vs "ca. 800 gram"
assert score(("AH Biologisch Pompoen", "AH Biologisch", "los per kilo"), ("Bio pompoen", "1 stuk (ca. 800 gram)")) == {"score": 95, "kind": "size"}
assert score(("AH Andijvie fijngesneden grootverpakking", "AH", "500 g"), ("Picnic andijvie fijngesneden", "500 gram"))["score"] == 95
assert score(("AH Rucola slamelange", "AH", "75 g"), ("Picnic rucola melange", "75 gram"))["score"] == 95
assert score(("Bolletje Echte beschuit", "Bolletje", "13 stuks"), ("Bolletje beschuit", "13 stuks"))["score"] == 95
assert score(("AH Volkoren brood", "AH", "800 g"), ("Picnic brood wit", "800 gram"))["score"] < s.MATCH_MIN_SCORE
assert s.parse_size("1 stuk (ca. 800 gram)") == (800, "g") and s.parse_size("los per kilo") == (1000, "g")
assert s.search_queries("AH Andijvie fijngesneden grootverpakking", "AH") == ["andijvie fijngesneden", "fijngesneden", "andijvie"]
assert s.search_queries("Bolletje Echte beschuit", "Bolletje") == ["bolletje beschuit", "beschuit"]
assert s.search_queries("AH Biologisch Pompoen", "AH Biologisch") == ["bio pompoen", "pompoen"]
assert s.search_query("AH Halfvolle melk 1 L", "AH") == "halfvolle melk"
best = s.best_match({"title": "AH Halfvolle melk", "brand": "AH", "unit_size": "1 l"},
                    [{"article_id": "a", "title": "Picnic volle melk", "unit": "1 liter"},
                     {"article_id": "b", "title": "Picnic halfvolle melk", "unit": "1 liter"}], skip={"x"})
assert best["article_id"] == "b"
print("MATCH OK")
