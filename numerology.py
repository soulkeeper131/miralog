"""
Pythagorean numerology calculations.
Pure deterministic math from birth date and name — no AI involved.
"""

import re

MASTER_NUMBERS = {11, 22, 33}

# Pythagorean letter-to-number mapping (Latin alphabet, A=1..I=9 repeating)
LATIN_MAP = {}
for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
    LATIN_MAP[ch] = (i % 9) + 1

VOWELS = set("AEIOUY")

# Basic transliteration for Cyrillic (Bulgarian) names to Latin, so the same
# Pythagorean table can be applied consistently.
CYRILLIC_TO_LATIN = {
    "А": "A", "Б": "B", "В": "V", "Г": "G", "Д": "D", "Е": "E", "Ж": "ZH",
    "З": "Z", "И": "I", "Й": "Y", "К": "K", "Л": "L", "М": "M", "Н": "N",
    "О": "O", "П": "P", "Р": "R", "С": "S", "Т": "T", "У": "U", "Ф": "F",
    "Х": "H", "Ц": "TS", "Ч": "CH", "Ш": "SH", "Щ": "SHT", "Ъ": "A",
    "Ь": "Y", "Ю": "YU", "Я": "YA",
}

NUMBER_MEANINGS = {
    1: "Лидерство, независимост и инициативност. Хора с пионерски дух, които обичат да проправят собствен път.",
    2: "Дипломатичност, сътрудничество и чувствителност. Естествени миротворци с силна интуиция за отношенията.",
    3: "Творчество, себеизразяване и общителност. Артистична, оптимистична енергия, обичаща да вдъхновява околните.",
    4: "Стабилност, дисциплина и практичност. Изградители, на които може да се разчита за структура и ред.",
    5: "Свобода, промяна и приключения. Динамична енергия, която търси разнообразие и нови преживявания.",
    6: "Отговорност, грижа и хармония. Ориентирани към семейството и общността, с чувство за дълг.",
    7: "Анализ, духовност и вътрешно търсене. Мислители, привлечени към истината отвъд повърхността.",
    8: "Амбиция, материален успех и власт. Силна воля за постижения и управление на ресурси.",
    9: "Хуманизъм, състрадание и завършеност. Широк поглед към света и желание да служат на по-висша цел.",
    11: "Майсторско число на интуицията и духовното прозрение. Носи потенциал за вдъхновяващо влияние, но и вътрешно напрежение.",
    22: "Майсторско число на строителя. Способност да превръща големи визии в конкретна, трайна реалност.",
    33: "Майсторско число на учителя-лечител. Безкористна отдаденост на благото на другите, рядко изразено в пълна сила.",
}

PERSONAL_YEAR_MEANINGS = {
    1: "Начало на нов 9-годишен цикъл — време за нови начинания и смели решения.",
    2: "Година на търпение, сътрудничество и изграждане на връзки.",
    3: "Година на творчество, комуникация и социален разцвет.",
    4: "Година на упорит труд, изграждане на основи и дисциплина.",
    5: "Година на промяна, свобода и неочаквани обрати.",
    6: "Година на отговорност, дом, семейство и грижа за близките.",
    7: "Година на вътрешно търсене, анализ и духовно развитие.",
    8: "Година на материални постижения, кариера и финансов растеж.",
    9: "Година на завършване — освобождаване от старото преди нов цикъл.",
}


def _reduce(n: int, keep_master: bool = True) -> int:
    """Reduce a number to a single digit, preserving master numbers 11/22/33."""
    while n > 9 and not (keep_master and n in MASTER_NUMBERS):
        n = sum(int(d) for d in str(n))
    return n


def _transliterate(name: str) -> str:
    result = []
    for ch in name.upper():
        if ch in CYRILLIC_TO_LATIN:
            result.append(CYRILLIC_TO_LATIN[ch])
        else:
            result.append(ch)
    return "".join(result)


def life_path_number(year: int, month: int, day: int) -> int:
    """Life Path: reduce year, month, day separately then sum and reduce (preserves masters)."""
    total = _reduce(year, keep_master=False) if year > 9 else year
    y = sum(int(d) for d in str(year))
    m = month
    d = day
    return _reduce(_reduce(y, keep_master=False) + _reduce(m, keep_master=False) + _reduce(d, keep_master=False))


def _name_letters(name: str):
    latin = _transliterate(name)
    return [ch for ch in latin if ch in LATIN_MAP]


def expression_number(name: str) -> int:
    """Expression/Destiny number: sum of all letters in the full name."""
    letters = _name_letters(name)
    total = sum(LATIN_MAP[ch] for ch in letters)
    return _reduce(total)


def soul_urge_number(name: str) -> int:
    """Soul Urge (Heart's Desire): sum of vowels only."""
    letters = _name_letters(name)
    total = sum(LATIN_MAP[ch] for ch in letters if ch in VOWELS)
    return _reduce(total) if total else 0


def personality_number(name: str) -> int:
    """Personality: sum of consonants only."""
    letters = _name_letters(name)
    total = sum(LATIN_MAP[ch] for ch in letters if ch not in VOWELS)
    return _reduce(total) if total else 0


def birthday_number(day: int) -> int:
    """Birthday number: the day of birth, reduced (preserves masters)."""
    return _reduce(day)


def personal_year_number(year: int, month: int, day: int, target_year: int) -> int:
    """Personal year: birth month + birth day + target (current) year, reduced."""
    total = _reduce(month, keep_master=False) + _reduce(day, keep_master=False) + _reduce(target_year, keep_master=False)
    return _reduce(total, keep_master=False)


def compute_numerology(name: str, year: int, month: int, day: int, target_year: int = None) -> dict:
    """Compute the full Pythagorean numerology profile for a person."""
    import datetime
    if target_year is None:
        target_year = datetime.date.today().year

    life_path = life_path_number(year, month, day)
    expression = expression_number(name)
    soul_urge = soul_urge_number(name)
    personality = personality_number(name)
    birthday = birthday_number(day)
    personal_year = personal_year_number(year, month, day, target_year)

    return {
        "life_path": {"number": life_path, "meaning": NUMBER_MEANINGS.get(life_path, "")},
        "expression": {"number": expression, "meaning": NUMBER_MEANINGS.get(expression, "")},
        "soul_urge": {"number": soul_urge, "meaning": NUMBER_MEANINGS.get(soul_urge, "")},
        "personality": {"number": personality, "meaning": NUMBER_MEANINGS.get(personality, "")},
        "birthday": {"number": birthday, "meaning": NUMBER_MEANINGS.get(birthday, "")},
        "personal_year": {
            "number": personal_year,
            "year": target_year,
            "meaning": PERSONAL_YEAR_MEANINGS.get(personal_year, ""),
        },
    }
