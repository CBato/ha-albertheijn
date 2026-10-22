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
assert score(("Calvé Pindakaas", "Calvé", "450 g"), ("Calvé pindakaas", "650 gram")) == {"score": 85, "kind": "size"}
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
assert score(("AH Biologisch Pompoen", "AH Biologisch", "los per kilo"), ("Bio pompoen", "1 stuk (ca. 800 gram)")) == {"score": 82, "kind": "size"}
# same size first, another size lower (worse the bigger the difference), every extra word costs ("met appel")
hak = [score(("Hak Rode kool", "Hak", "700 g"), t) for t in
       (("Hak rode kool", "700 gram"), ("Hak rode kool met appel", "700 gram"), ("Hak rode kool", "355 gram"), ("Hak rode kool met appel", "355 gram"))]
assert [h["score"] for h in hak] == [95, 90, 80, 65] and hak[0]["kind"] == "same" and hak[2]["kind"] == "size", hak
assert score(("AH Andijvie fijngesneden grootverpakking", "AH", "500 g"), ("Picnic andijvie fijngesneden", "500 gram"))["score"] == 95
assert score(("AH Rucola slamelange", "AH", "75 g"), ("Picnic rucola melange", "75 gram"))["score"] == 95
assert score(("Bolletje Echte beschuit", "Bolletje", "13 stuks"), ("Bolletje beschuit", "13 stuks"))["score"] == 95
assert score(("AH Volkoren brood", "AH", "800 g"), ("Picnic brood wit", "800 gram"))["score"] < s.MATCH_MIN_SCORE
assert s.parse_size("1 stuk (ca. 800 gram)") == (800, "g") and s.parse_size("los per kilo") == (1000, "g")
assert s.search_queries("AH Andijvie fijngesneden grootverpakking", "AH") == ["andijvie fijngesneden", "andijvie"]
# store brand: the first word, not the longest ("toegevoegd")
assert s.search_queries("AH Appelmoes 0% suiker toegevoegd", "AH")[1] == "appelmoes"

# real cases from a match export (22 Oct 2026): should NOT match
for mine, theirs in [
    (("AH Appelsap", "AH", "1,5 l"), ("Picnic appel nectar", "1.5 liter")),            # sap is not nectar
    (("AH Elstar appelsap", "AH", "1 l"), ("Elstar appels", "1,5 kilo")),               # juice is not apples
    (("AH Goudse jong belegen 48+ plakken", "AH", "190 g"), ("Picnic goudse jong belegen 30+ plakken", "190 gram")),
    (("Dodoni Griekse yoghurt 0% vet", "Dodoni", "500 g"), ("Dodoni Griekse yoghurt 5%", "500 gram")),
    (("Holie's Chewy bar coconut chocolate", "Holie's", "105 g"), ("Holie's chewy bar double chocolate", "105 gram")),
    (("AH Smeerkaas naturel 15+", "AH", "200 g"), ("Echte Smeerkaas 48+ naturel", "100 gram")),
]:
    assert score(mine, theirs)["score"] < s.MATCH_MIN_SCORE, (mine[0], score(mine, theirs))
# should match, and these are so clearly the same that they are confirmed automatically (95)
for mine, theirs in [
    (("AH Paprika rood", "AH", "per stuk"), ("Rode paprika", "1 stuk")),                 # rood / rode
    (("Van Gilse Witte basterdsuiker", "Van Gilse", "600 g"), ("Van Gilse basterdsuiker wit", "600 gram")),
    (("Teisseire Zero tropical siroop", "Teisseire", "0,6 l"), ("Teisseire Siroop tropical 0%", "600 ml")),  # zero / 0%
    (("Pukka Relax", "Pukka", "20 stuks"), ("Pukka relax thee", "20 koppen")),           # koppen = stuks, "thee"
    (("Clipper Zen again organic infusion", "Clipper", "20 stuks"), ("Clipper bio zen again", "20 koppen")),
    (("AH Pitloze witte druiven", "AH", "500 g"), ("Witte druiven", "500 gram")),
    (("AH Luxe roerbak Provençaals", "AH", "400 g"), ("Picnic Provençaalse roerbakgroenten", "400 gram")),
    (("AH Andijvie fijngesneden kleinverpakking", "AH", "250 g"), ("Picnic andijvie fijngesneden", "250 gram")),
    (("AH Witte vrije uitloopeieren M", "AH", "6 stuks"), ("Picnic witte vrije uitloop eieren", "6 stuks M/L")),
    (("Danio Romige kwark aardbei", "Danio", "450 g"), ("Danio aardbei kwark", "450 gram")),
    (("Almhof Choco met echte slagroom original", "Almhof", "180 g"), ("Almhof choco met échte slagroom", "180 ml")),  # g ≈ ml
    (("Mentos Gum White sweet mint", "Mentos Gum", "112.5 g"), ("Mentos White sweetmint", "112 gram • 75 stuks")),  # half the brand
    (("De Zaanse Hoeve Roomboter ongezouten", "De Zaanse Hoeve", "250 g"), ("Picnic Ongezouten roomboter", "250 gram")),  # AH brand
    (("Optimel Drinkyoghurt framboos", "Optimel", "1 l"), ("Optimel drinkyoghurt framboos 0%", "1 liter")),  # grade only there
    (("Jozo Naturel tafelzout", "Jozo", "125 g"), ("Jozo tafelzout", "125 gram")),             # "naturel" left out
    (("AH Pannenkoeken naturel", "AH", "8 stuks"), ("Picnic pannenkoeken", "8 stuks")),
    (("Tuc paprika", "Tuc", "100 g"), ("Tuc Crackers Paprika", "100 gram")),                   # a category word added
    (("Nestlé Kaktus", "Nestlé", "9 stuks"), ("Nestlé kaktus ijsje", "9 stuks")),
    (("Aviko Supercrunch originals Hollandse friet", "Aviko", "750 g"), ("Aviko SuperCrunch Hollandse frites", "750 gram")),
    (("AH Roma tomaten", "AH", "750 g"), ("Pruimtomaten", "750 gram")),
    (("AH Gesneden rode uien", "AH", "150 g"), ("Picnic rode uien fijngesnipperd", "150 gram")),
    (("AH Fijngesneden ijsbergsla", "AH", "100 g"), ("Picnic ijsbergsla fijn", "100 gram")),
]:
    assert score(mine, theirs) == {"score": 95, "kind": "same"}, (mine[0], score(mine, theirs))
# likely (shown, not confirmed automatically): a brand with a shorter name there, "Original" = "Naturel"
for mine, theirs in [
    (("Actimel Drinkyoghurt aardbei", "Actimel", "8 x 100 ml"), ("Actimel aardbei", "8 x 100 ml")),
    (("Pringles Original", "Pringles", "165 g"), ("Pringles Naturel", "165 gram")),
]:
    assert 60 <= score(mine, theirs)["score"] < 95, (mine[0], score(mine, theirs))
# your store brand against another maker's product ("Bar le Duc ..."): their name is no extra word, but comparable at
# most (85), never confirmed automatically
for mine, theirs in [
    (("AH Mineraalwater koolzuurhoudend", "AH", "1,5 l"), ("Bar le Duc mineraalwater koolzuurhoudend", "1.5 liter")),
    (("AH Iets kruimige aardappelen", "AH", "1 kg"), ("Jongens van de Klei kruimige aardappelen", "1 kilo")),
    (("AH Drinkyoghurt aardbei kerssmaak", "AH", "1 l"), ("Optimel drinkyoghurt aardbei kers 0%", "1 liter")),  # kerssmaak
]:
    assert score(mine, theirs) == {"score": 85, "kind": "same"}, (mine[0], score(mine, theirs))
# grams vs pieces stays "similar" without the heavy weight-vs-volume penalty
assert score(("Unox Knaks", "Unox", "400 g"), ("Unox knaks", "11 stuks")) == {"score": 90, "kind": "similar"}
assert s.search_queries("Bolletje Echte beschuit", "Bolletje") == ["bolletje beschuit", "beschuit"]
assert s.search_queries("AH Biologisch Pompoen", "AH Biologisch") == ["bio pompoen", "pompoen"]
assert s.search_query("AH Halfvolle melk 1 L", "AH") == "halfvolle melk"
best = s.best_match({"title": "AH Halfvolle melk", "brand": "AH", "unit_size": "1 l"},
                    [{"article_id": "a", "title": "Picnic volle melk", "unit": "1 liter"},
                     {"article_id": "b", "title": "Picnic halfvolle melk", "unit": "1 liter"}], skip={"x"})
assert best["article_id"] == "b"
print("MATCH OK")
