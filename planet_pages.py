# -*- coding: utf-8 -*-
"""Данни за SEO страниците „планета в знак" (напр. /luna-v-skorpion).

Всяка планета (плюс Асцендента) има SEO slug, българско име и глиф.
Значението идва от translations.OBJECT_MEANINGS, а характерът на знака —
от translations.SIGN_MEANINGS. Текстът на страниците се генерира от AI
веднъж (кешира се в planet_sign_cache) и е КРАТЪК тизър, не пълно разчитане.
"""

PLANETS = [
    {"key": "Sun", "slug": "slantse", "name": "Слънце", "glyph": "☉"},
    {"key": "Moon", "slug": "luna", "name": "Луна", "glyph": "☽"},
    {"key": "Mercury", "slug": "merkuriy", "name": "Меркурий", "glyph": "☿"},
    {"key": "Venus", "slug": "venera", "name": "Венера", "glyph": "♀"},
    {"key": "Mars", "slug": "mars", "name": "Марс", "glyph": "♂"},
    {"key": "Jupiter", "slug": "yupiter", "name": "Юпитер", "glyph": "♃"},
    {"key": "Saturn", "slug": "saturn", "name": "Сатурн", "glyph": "♄"},
    {"key": "Uranus", "slug": "uran", "name": "Уран", "glyph": "♅"},
    {"key": "Neptune", "slug": "neptun", "name": "Нептун", "glyph": "♆"},
    {"key": "Pluto", "slug": "pluton", "name": "Плутон", "glyph": "♇"},
    {"key": "Asc", "slug": "ascendent", "name": "Асцендент", "glyph": "⬆"},
]

PLANETS_BY_KEY = {p["key"]: p for p in PLANETS}
PLANETS_BY_SLUG = {p["slug"]: p for p in PLANETS}
