# -*- coding: utf-8 -*-
"""
Mechanical Bulgarian text corrections applied to generated readings.

Only rules that are unambiguous enough to apply without judgement live here.
Anything that needs to know the sentence's meaning (definite article, commas)
is left to the model's own prompt instructions.
"""

import json
import re

# "във" and "със" are the long forms, used only before words starting with the
# same or a closely related consonant. Everywhere else the short form is correct.
_VAV_LONG = re.compile(r"\b(В|в)ъв(\s+)(?![вфВФ])", re.UNICODE)
_SAS_LONG = re.compile(r"\b(С|с)ъс(\s+)(?![сзшжСЗШЖ])", re.UNICODE)

# The mirror case: short form where the long one is required.
_VAV_SHORT = re.compile(r"\b(В|в)(\s+)(?=[вфВФ])", re.UNICODE)
_SAS_SHORT = re.compile(r"\b(С|с)(\s+)(?=[сзшжСЗШЖ])", re.UNICODE)

# Spacing and punctuation tidy-ups.
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?…])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
# Не след „:“ в „https://“ и не пред затварящ знак: „**Слънце:**“ ставаше
# „**Слънце: **“ и удебеляването се чупеше.
_MISSING_SPACE_AFTER_PUNCT = re.compile(r"([,;:])(?=[^\s\d/*)\]»“”\"'_])")
# Адресите се пазят цели — запетая или двоеточие в тях не е пунктуация.
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def _fix_prepositions(text: str) -> str:
    text = _VAV_LONG.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)
    text = _SAS_LONG.sub(lambda m: f"{m.group(1)}{m.group(2)}", text)
    text = _VAV_SHORT.sub(lambda m: f"{m.group(1)}ъв{m.group(2)}", text)
    text = _SAS_SHORT.sub(lambda m: f"{m.group(1)}ъс{m.group(2)}", text)
    return text


def _fix_punctuation(text: str) -> str:
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _MISSING_SPACE_AFTER_PUNCT.sub(r"\1 ", text)
    text = _MULTI_SPACE.sub(" ", text)
    return text


def clean_bg(text: str) -> str:
    """Apply the mechanical corrections, leaving markdown structure untouched."""
    if not text:
        return text

    out = []
    for line in text.split("\n"):
        # Markdown markers and inline code are left alone; only prose is touched.
        if line.strip().startswith("```"):
            out.append(line)
            continue
        parts = _URL.split(line)
        urls = _URL.findall(line)
        # „силeн“ с латинско e се поправя във всяко разчитане, не само в хороскопите.
        fixed = _fix_punctuation(_fix_prepositions(fix_latin_lookalikes(parts[0])))
        for url, rest in zip(urls, parts[1:]):
            fixed += url + _fix_punctuation(_fix_prepositions(fix_latin_lookalikes(rest)))
        out.append(fixed)
    return "\n".join(out)


def count_issues(text: str) -> dict:
    """Report remaining в/във and с/със problems — used to verify the rules work."""
    if not text:
        return {"vav": 0, "sas": 0}
    return {
        "vav": len(_VAV_LONG.findall(text)) + len(_VAV_SHORT.findall(text)),
        "sas": len(_SAS_LONG.findall(text)) + len(_SAS_SHORT.findall(text)),
    }


# --- Проверка на хороскоп преди запис в кеша -------------------------------
# Кешът пази текста за деня и го виждат всички посетители. Нищо досега не го
# проверяваше: на production стигнаха „impulsивно“ (латиница в българска дума),
# „(отклонение 2.6°)“ от подканата, „ретроградният Меркурий не е тук“ и
# отрязани отговори. Тук са правилата, които не искат преценка на смисъла.

class ReadingRejected(ValueError):
    """Текстът не е годен за кеша; съобщението казва защо (влиза и в повторния опит)."""


class SoftRejected(ReadingRejected):
    """Текстът е цял и четим, но нарушава правило (здраве/пари, „днес“ за утре,
    дълго настроение…). Повтаря се веднъж; ако и вторият е такъв — записва се
    `text` (вече поправеният), вместо човекът да остане без хороскоп."""

    def __init__(self, reason: str, text: str):
        super().__init__(reason)
        self.text = text


# Латински букви, които на око са същите като българските — моделът понякога
# ги слага вътре в дума. Заменят се САМО в дума, която вече има кирилица.
_LATIN_TO_CYR = {
    "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х", "y": "у", "k": "к",
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М",
    "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
}
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_CYR = re.compile(r"[А-Яа-яЁё]")
_LATIN = re.compile(r"[A-Za-z]")
_ROMAN = re.compile(r"^[IVXLC]+$")


def fix_latin_lookalikes(text: str) -> str:
    """„силeн“ с латинско e → „силен“. Дума с друга латиница остава както е —
    проверката по-долу я отхвърля („impulsивно“ не е печатна грешка)."""
    def fix(m):
        word = m.group(0)
        if not (_CYR.search(word) and _LATIN.search(word)):
            return word
        # Само ако ВСЯКА латинска буква има кирилски двойник — иначе
        # „impulsивно“ ставаше „imрulsивно“, още по-счупено.
        if any(_LATIN.match(ch) and ch not in _LATIN_TO_CYR for ch in word):
            return word
        return "".join(_LATIN_TO_CYR.get(ch, ch) for ch in word)
    return _WORD.sub(fix, text or "")


def mixed_script_words(text: str) -> list:
    """Думите с кирилица И латиница вътре (след замяната на двойниците)."""
    return [w for w in _WORD.findall(text or "") if _CYR.search(w) and _LATIN.search(w)]


def latin_words(text: str) -> list:
    """Цели латински думи от поне 3 букви: „Mars“, „Trine“ — имена от подканата."""
    return [w for w in _WORD.findall(text or "")
            if len(w) >= 3 and not _CYR.search(w) and not _ROMAN.match(w)]


# Аспектът е обикновена дума в изречението: „в Секстил със“ е от подканата.
_CAP_ASPECT = re.compile(r"(?<=[а-я] )(Секстил|Тригон|Квадрат|Опозиция|Съвпад)(?![а-я])")


def lower_midsentence_aspects(text: str) -> str:
    return _CAP_ASPECT.sub(lambda m: m.group(1).lower(), text or "")


# Данни от подканата, които не бива да стигат до читателя.
_LEAKS = [
    (re.compile(r"отклонени", re.I), "„отклонение“ (число от подканата)"),
    (re.compile(r"\d[.,]\d\s*°"), "градуси с десетична част"),
    (re.compile(r"Имум\s+Коели", re.I), "„Имум Коели“"),
    (re.compile(r"Част(?:та)?\s+на\s+щастие", re.I), "„Частта на щастието“"),
    (re.compile(r"не\s+е\s+тук", re.I), "„не е тук“"),
    (re.compile(r"[<>]"), "ъглови скоби от образеца"),
]

# Меки здравни и финансови твърдения: подканата ги забранява, моделът пак ги
# връща. Само най-ясните думи, за да не се реже здрав текст.
# Скоби с градуси/отклонения, изтекли от подканата: „ (отклонение 2.6°)“, „(0.1°)“.
_DEGREE_PARENS = re.compile(r"\s*\((?:[^()]{0,30}?)\d+(?:[.,]\d+)?\s*°[^()]{0,10}\)")

_SAFETY = [
    (re.compile(r"възпал|стомах|храносмил|кръвно|имунитет|имунна|болест|симптом|диагноз|"
                r"лекарств|таблетк|\bоперац\w+", re.I),
     "здравно твърдение (органи, болести, симптоми)"),
    (re.compile(r"инвестир|инвестиц|кредит|\bзаем(?:и|а|ът)?\b|борс[аи]\b|\bакции\b|"
                r"\bподпиши\b|\bподписвай\b|\bкупи\b|\bкупувай\b", re.I),
     "финансов съвет (инвестиране, заем, подписване, покупка)"),
]

# Относителните дни не важат за текст, който ще се чете и в друг ден.
_RELATIVE_DAY = re.compile(r"\b(?:днес|днешн\w*|утре|утрешн\w*|вчера|вчерашн\w*)\b", re.I)

_SENTENCE_END = ".!?…»\"”)"
_HEADING = re.compile(r"^\s*(\d+)[.)]\s*\*\*(.+?)\*\*[:：]?\s*(.*)$")
_SUMMARY_BLOCK = re.compile(r"---SUMMARY---\s*(.*?)\s*---END---\s*", re.DOTALL)
_ENERGY = {"висока": "Висока", "средна": "Средна", "ниска": "Ниска"}

READING_MIN_CHARS = 1800
READING_MAX_CHARS = 7500
SECTION_MIN_CHARS = 60
TEASER_MAX = 160          # подканата иска до 140; тук е малко толеранс за броенето на модела


def _clean_str(value, name: str, max_len: int, soft: list) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReadingRejected(f"резюмето няма поле „{name}“")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) > max_len:
        soft.append(f"полето „{name}“ е прекалено дълго ({len(value)} знака)")
    return value


def _clean_list(value, name: str, lo: int, hi: int, soft: list) -> list:
    if not isinstance(value, list):
        raise ReadingRejected(f"резюмето няма списък „{name}“")
    items = [re.sub(r"\s+", " ", v).strip() for v in value if isinstance(v, str) and v.strip()]
    if not items:
        raise ReadingRejected(f"списъкът „{name}“ е празен")
    if not (lo <= len(items) <= hi):
        soft.append(f"„{name}“ има {len(items)} реда, очакват се {lo}-{hi}")
        items = items[:hi]
    if any(len(item) > 70 for item in items):
        soft.append(f"ред от „{name}“ е прекалено дълъг")
    return items


def validate_horoscope(raw: str, *, titles: list, teaser: bool = False,
                       relative_days: bool = True) -> str:
    """Проверява отговора на модела и връща поправения текст за кеша.

    Сами се поправят само безопасните неща: латинските двойници в кирилски
    думи, аспектите с главна буква насред изречение, заглавията на разделите
    (по позиция) и форматът на резюмето. Всичко друго е `ReadingRejected` —
    викащият прави един нов опит и после показва грешка, вместо да запише
    счупен текст за целия ден.

    `titles` — заглавията на разделите по ред; `teaser` — искаме ли откъс за
    общата страница; `relative_days` — позволени ли са „днес“/„утре“."""
    text = lower_midsentence_aspects(fix_latin_lookalikes((raw or "").strip()))
    # „(отклонение 2.6°)“ и подобни скоби с градуси — махат се направо.
    text = _DEGREE_PARENS.sub("", text)
    soft: list = []          # нарушения, с които текстът все пак е годен (виж SoftRejected)
    if not text:
        raise ReadingRejected("празен отговор")

    m = _SUMMARY_BLOCK.search(text)
    if not m:
        raise ReadingRejected("няма резюме между ---SUMMARY--- и ---END---")
    block = re.sub(r"^```(?:json)?\s*|\s*```$", "", m.group(1).strip())
    try:
        data = json.loads(block)
    except ValueError:
        raise ReadingRejected("резюмето не е валиден JSON")
    if not isinstance(data, dict):
        raise ReadingRejected("резюмето не е JSON обект")

    energy = _ENERGY.get(str(data.get("energy", "")).strip().lower())
    if not energy:
        raise ReadingRejected("„energy“ не е Висока, Средна или Ниска")
    summary = {
        "mood": _clean_str(data.get("mood"), "mood", 40, soft),
        "energy": energy,
        "do": _clean_list(data.get("do"), "do", 2, 4, soft),
        "avoid": _clean_list(data.get("avoid"), "avoid", 2, 4, soft),
        "focus": _clean_str(data.get("focus"), "focus", 100, soft),
        "caution": _clean_str(data.get("caution"), "caution", 240, soft),
    }
    if len(summary["mood"].split()) > 4:
        soft.append("„mood“ не е кратко съществително")
    if teaser:
        # Без откъс страницата ползва първите изречения — затова е „меко“.
        t = data.get("teaser") if isinstance(data.get("teaser"), str) else ""
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            soft.append("резюмето няма поле „teaser“")
        elif len(t) > TEASER_MAX:
            soft.append(f"полето „teaser“ е прекалено дълго ({len(t)} знака)")
        elif re.match(r"денят\b", t, re.I):
            soft.append("„teaser“ започва с „Денят“")
        elif t[-1] not in ".!?…":
            soft.append("„teaser“ не е цяло изречение")
        else:
            summary["teaser"] = t

    # Разделите: точно толкова, номерирани по ред, всеки със съдържание.
    lines = text[m.end():].strip().splitlines()
    marks = [(i, _HEADING.match(ln)) for i, ln in enumerate(lines)]
    marks = [(i, h) for i, h in marks if h]
    if len(marks) != len(titles):
        raise ReadingRejected(f"разделите са {len(marks)}, очакват се {len(titles)}")
    for n, (i, h) in enumerate(marks, 1):
        if int(h.group(1)) != n:
            raise ReadingRejected("разделите не са номерирани по ред")
    bounds = [i for i, _ in marks] + [len(lines)]
    for n, (i, h) in enumerate(marks, 1):
        part = "\n".join([h.group(3)] + lines[i + 1:bounds[n]]).strip()
        if len(part) < SECTION_MIN_CHARS:
            raise ReadingRejected(f"раздел {n} е празен или прекалено къс")
        # Заглавието е по позиция: интерфейсът намира разделите по него.
        lines[i] = f"{n}. **{titles[n - 1]}**" + (f" {h.group(3)}" if h.group(3) else "")
    body = "\n".join(lines[marks[0][0]:]).strip()

    # Прекалено кратък е счупен текст; прекалено дълъг е цял — само го отбелязваме
    # (личните с 9 раздела излизат по 8–10 хил. знака и се отхвърляха напразно).
    max_chars = READING_MAX_CHARS * len(titles) // 6
    if len(body) < READING_MIN_CHARS:
        raise ReadingRejected(f"размерът на текста е {len(body)} знака "
                              f"(очакват се поне {READING_MIN_CHARS})")
    if len(body) > max_chars:
        soft.append(f"текстът е {len(body)} знака (до {max_chars})")
    if body.rstrip("*_ \n\t")[-1:] not in _SENTENCE_END:
        raise ReadingRejected("текстът не свършва с цяло изречение (отрязан е)")

    # Всичко, което читателят ще види, се чете наведнъж: резюме и текст.
    visible = " ".join([summary["mood"], summary["focus"], summary["caution"],
                        summary.get("teaser", ""), *summary["do"], *summary["avoid"], body])
    mixed = mixed_script_words(visible)
    if mixed:
        soft.append(f"латиница в българска дума: „{mixed[0]}“")
    foreign = latin_words(visible)
    if foreign:
        soft.append(f"чужда дума с латински букви: „{foreign[0]}“")
    for pattern, what in _LEAKS + _SAFETY:
        hit = pattern.search(visible)
        if hit:
            soft.append(f"{what}: „{hit.group(0)}“")
    if not relative_days:
        hit = _RELATIVE_DAY.search(visible)
        if hit:
            soft.append(f"„{hit.group(0)}“ в текст за друг ден")

    head = json.dumps(summary, ensure_ascii=False)
    out = f"---SUMMARY---\n{head}\n---END---\n\n{body}"
    if soft:
        raise SoftRejected("; ".join(soft[:3]), out)
    return out
