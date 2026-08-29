# -*- coding: utf-8 -*-
"""Данни за SEO страниците „планета в дом" (напр. /luna-v-7-dom).

12-те дома са вечнозелени — позицията на една планета в даден дом носи
устойчиво значение, което не се мени с времето. Значенията на домовете
идват от translations.HOUSE_MEANINGS (един източник на истина); тук е само
структурата за URL/заглавия.
"""

HOUSES = [
    {"key": "1st House", "num": 1, "slug": "1-dom", "name": "1-ви дом", "short": "1 дом"},
    {"key": "2nd House", "num": 2, "slug": "2-dom", "name": "2-ри дом", "short": "2 дом"},
    {"key": "3rd House", "num": 3, "slug": "3-dom", "name": "3-ти дом", "short": "3 дом"},
    {"key": "4th House", "num": 4, "slug": "4-dom", "name": "4-ти дом", "short": "4 дом"},
    {"key": "5th House", "num": 5, "slug": "5-dom", "name": "5-ти дом", "short": "5 дом"},
    {"key": "6th House", "num": 6, "slug": "6-dom", "name": "6-ти дом", "short": "6 дом"},
    {"key": "7th House", "num": 7, "slug": "7-dom", "name": "7-ми дом", "short": "7 дом"},
    {"key": "8th House", "num": 8, "slug": "8-dom", "name": "8-ми дом", "short": "8 дом"},
    {"key": "9th House", "num": 9, "slug": "9-dom", "name": "9-ти дом", "short": "9 дом"},
    {"key": "10th House", "num": 10, "slug": "10-dom", "name": "10-ти дом", "short": "10 дом"},
    {"key": "11th House", "num": 11, "slug": "11-dom", "name": "11-ти дом", "short": "11 дом"},
    {"key": "12th House", "num": 12, "slug": "12-dom", "name": "12-ти дом", "short": "12 дом"},
]

HOUSES_BY_NUM = {h["num"]: h for h in HOUSES}
