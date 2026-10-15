# -*- coding: utf-8 -*-
"""Качеството на дневните хороскопи: чисто небе, домове по знак, проверка преди
кеша, подканите без примерни фрази, личният хороскоп по обяд и с български имена."""
import datetime
import json
import secrets
import sqlite3
import threading
import time
from zoneinfo import ZoneInfo

import pytest
from starlette.requests import Request

import bg_text
from bg_text import ReadingRejected, validate_horoscope


# --- помощни ------------------------------------------------------------------

SENT = ("Луната в Стрелец носи нужда от простор и откровени разговори, а Слънцето "
        "във Везни търси баланс между желанията и задълженията. ")
EXAMPLE_PHRASES = ("прибързани обещания", "спокойни разговори", "спорове с близки",
                   "важни решения", "подреждане на дома", "претоварване с работа")


def make_reply(titles, *, teaser=True, sentences=4, summary=None, body=None, last=None):
    """Отговор на модела, който минава проверката; частите се чупят по една."""
    data = {"mood": "Спокойствие", "energy": "Средна",
            "do": ["ранна разходка", "тих разговор", "малка пауза"],
            "avoid": ["забързани отговори", "късни вечери"],
            "focus": "баланс между дом и работа",
            "caution": "Нервността от бързането лесно се прехвърля върху околните."}
    if teaser:
        data["teaser"] = "Тих ден за разговори, в който търпението печели повече от бързането."
    data.update(summary or {})
    if body is None:
        body = "\n\n".join(f"{n}. **{t}**\n{SENT * sentences}".rstrip()
                           for n, t in enumerate(titles, 1))
    if last is not None:
        body = body.rstrip() + last
    return f"---SUMMARY---\n{json.dumps(data, ensure_ascii=False)}\n---END---\n\n{body}"


@pytest.fixture
def sign_reply(app):
    return lambda **kw: make_reply(app.SIGN_SECTION_TITLES, **kw)


@pytest.fixture
def personal_reply(app):
    return lambda **kw: make_reply(app.PERSONAL_SECTION_TITLES, teaser=False, **kw)


def _validate_sign(app, raw, **kw):
    return validate_horoscope(raw, titles=app.SIGN_SECTION_TITLES, teaser=True, **kw)


def _wait_jobs(app):
    with app._AI_JOBS_LOCK:
        jobs = [j for j in app._AI_JOBS.values() if not j["done"].is_set()]
    for job in jobs:
        job["done"].wait(30)


def _real_sky(app, date_iso="2026-10-15"):
    return app.sky_for_date(date_iso)


def _block(app, slug, date_iso="2026-10-15"):
    return app._sky_context_text(_real_sky(app, date_iso), app.ZODIAC_BY_SLUG[slug])


# --- T1: чисто небе ------------------------------------------------------------

@pytest.mark.parametrize("date_iso", ["2026-10-15", "2026-10-16", "2026-03-02", "2026-12-24"])
def test_sky_has_no_duplicates_angles_or_part_of_fortune(app, date_iso):
    sky = _real_sky(app, date_iso)
    bodies = set(app._DAILY_SKY_BODIES)
    seen = set()
    for a in sky["aspects"] + sky["background_aspects"]:
        assert a["active"] in bodies and a["passive"] in bodies, a
        assert a["type"] in app.MAJOR_ASPECTS
        assert a["deviation"] <= 3.0
        key = (frozenset((a["active"], a["passive"])), a["type"])
        assert key not in seen, f"аспектът е два пъти: {key}"
        seen.add(key)
    text = app._sky_context_text(sky)
    for banned in ("Имум Коели", "Част на щастието", "Асцендент", "Вертекс", "Хирон", "възел", "отклонение"):
        assert banned not in text


def test_slow_pairs_are_background_only(app):
    for date_iso in ("2026-10-15", "2026-11-20", "2027-01-10"):
        sky = _real_sky(app, date_iso)
        slow = app._SLOW_BODIES
        assert all(a["active"] in slow and a["passive"] in slow for a in sky["background_aspects"])
        assert not any(a["active"] in slow and a["passive"] in slow for a in sky["aspects"])
    # 15.10.2026: Уран–Плутон и Нептун–Плутон са били „новини“ в старата подкана.
    pairs = {frozenset((a["active"], a["passive"])) for a in _real_sky(app)["background_aspects"]}
    assert frozenset(("Uranus", "Pluto")) in pairs


def test_background_is_labelled_as_background_in_the_prompt(app):
    block = _block(app, "oven")
    assert "Дълготраен фон" in block and "най-много един" in block
    main = block.split("Главни аспекти")[1].split("Дълготраен фон")[0]
    assert "Нептун" not in main and "Уран" not in main     # само Плутон–Венера, без бавните двойки
    assert "Уран от 3-ти дом е в тригон с Плутон" in block.split("Дълготраен фон")[1]


def test_aspect_text_has_no_numbers_and_lowercase_aspect_names(app):
    block = _block(app, "oven")
    assert "е в секстил с Юпитер" in block
    assert "е в тригон със Сатурн" in block          # „със“ пред С
    for word in ("Секстил", "Тригон", "Квадрат", "Опозиция", "Съвпад"):
        assert word not in block
    assert "Слънце от 7-ми дом е в секстил" in block


# --- T2: домове по знак ----------------------------------------------------------

def test_house_in_sign_counts_from_the_sign(app):
    assert app.house_in_sign(0, 0) == 1           # Овен за Овен
    assert app.house_in_sign(6, 0) == 7           # Везни за Овен
    assert app.house_in_sign(0, 6) == 7           # Овен за Везни
    assert app.house_in_sign(11, 1) == 11         # Риби за Телец


def test_every_planet_has_a_house_and_a_theme_per_sign(app):
    block = _block(app, "oven")
    assert "Слънце: Везни, 22° — 7-ми дом (партньорство, връзки, договорки)" in block
    assert "Сатурн: Овен, 10°, ретрограден — 1-ви дом (личност, самочувствие, нови начала)" in block
    assert "Венера: Скорпион, 5°, ретроградна — 8-ми дом" in block
    vezni = _block(app, "vezni")
    assert "Слънце: Везни, 22° — 1-ви дом" in vezni
    assert "Сатурн: Овен, 10°, ретрограден — 7-ми дом" in vezni


def test_ruler_of_the_sign_is_named_with_its_position(app):
    assert "Управителят на Овен е Марс: Лъв, 9° — 5-ти дом" in _block(app, "oven")
    # Модерните управители: Плутон за Скорпион е ретрограден във Водолей (4-ти дом).
    assert "Управителят на Скорпион е Плутон: Водолей, 3°, ретрограден — 4-ти дом" in _block(app, "skorpion")


def test_twelve_signs_get_twelve_different_skies(app):
    blocks = {slug: _block(app, slug) for slug in app.ZODIAC_BY_SLUG}
    assert len(set(blocks.values())) == 12
    # Планетите са едни и същи, домовете — различни: редовете за Слънцето се различават.
    suns = {b.splitlines()[0] for b in blocks.values()}
    assert len(suns) == 12


def test_health_and_money_themes_are_attitude_only(app):
    themes = " ".join(app.HOUSE_TOPICS.values())
    for word in ("болест", "здраве", "заем", "инвестиц", "дългове"):
        assert word not in themes


def test_moon_sign_change_is_found_and_exact(app):
    day = datetime.date(2026, 10, 1)
    found = []
    for i in range(40):
        d = day + datetime.timedelta(days=i)
        change = app.moon_sign_change(d)
        if change:
            found.append((d, change))
    assert 12 <= len(found) <= 20            # Луната сменя знака на ~2,3 дни
    sofia, utc = ZoneInfo("Europe/Sofia"), datetime.timezone.utc
    for d, change in found[:6]:
        assert change["to_idx"] == (change["from_idx"] + 1) % 12
        hour, minute = map(int, change["time"].split(":"))
        at = datetime.datetime(d.year, d.month, d.day, hour, minute, tzinfo=sofia)
        before = int(app._moon_longitude((at - datetime.timedelta(minutes=2)).astimezone(utc)) // 30) % 12
        after = int(app._moon_longitude((at + datetime.timedelta(minutes=2)).astimezone(utc)) // 30) % 12
        assert (before, after) == (change["from_idx"], change["to_idx"])


def test_moon_ingress_is_in_the_prompt_with_the_new_house(app):
    day = next(datetime.date(2026, 10, 1) + datetime.timedelta(days=i) for i in range(10)
               if app.moon_sign_change(datetime.date(2026, 10, 1) + datetime.timedelta(days=i)))
    iso = day.isoformat()
    change = app.moon_sign_change(day)
    new_sign = app.ZODIAC_SIGNS[change["to_idx"]]["name"]
    text = app._sky_context_text(app.sky_for_date(iso), app.ZODIAC_BY_SLUG["oven"])
    assert f"в {change['time']} (по софийско време) преминава в {new_sign}" in text
    # Без смяна на знака няма и ред за това.
    quiet = next(datetime.date(2026, 10, 1) + datetime.timedelta(days=i) for i in range(10)
                 if not app.moon_sign_change(datetime.date(2026, 10, 1) + datetime.timedelta(days=i)))
    assert "преминава в" not in app._sky_context_text(app.sky_for_date(quiet.isoformat()),
                                                       app.ZODIAC_BY_SLUG["oven"])


def test_retrograde_planets_are_listed_explicitly(app):
    block = _block(app, "oven")
    assert "Ретроградни: Венера, Сатурн, Уран, Нептун, Плутон. Всички останали планети са директни." in block


# --- T3: проверката преди кеша ----------------------------------------------------

def test_a_good_reply_passes_and_is_normalised(app, sign_reply):
    out = _validate_sign(app, sign_reply())
    assert out.startswith("---SUMMARY---\n{")
    summary, body = app.split_summary(out)
    assert summary["energy"] == "Средна" and summary["teaser"]
    assert body.count("**") == 12
    assert body.splitlines()[0] == "1. **Общо усещане**"


def test_headings_are_canonical_by_position(app, sign_reply):
    raw = sign_reply().replace("**Любов**", "**Любов и отношения**").replace("**Късмет**", "**Късмет и шансове**")
    out = _validate_sign(app, raw)
    assert "**Любов**" in out and "**Късмет**" in out and "и отношения" not in out


LAT_O, LAT_A, LAT_E = "\u006f", "\u0061", "\u0065"       # латински o, a, e — като кирилските


def test_latin_lookalikes_inside_cyrillic_words_are_replaced(app, sign_reply):
    raw = (sign_reply().replace("Спокойствие", f"Сп{LAT_O}койствие")
           .replace("откровени", f"{LAT_O}ткровени").replace("баланс", f"б{LAT_A}л{LAT_A}нс"))
    assert bg_text.mixed_script_words(raw)
    out = _validate_sign(app, raw)
    assert not bg_text.mixed_script_words(out)
    assert "Спокойствие" in out and "откровени" in out and "баланс" in out


def test_other_latin_inside_a_cyrillic_word_is_rejected(app, sign_reply):
    raw = sign_reply().replace("откровени", "impulsивни")
    with pytest.raises(ReadingRejected, match="латиница"):
        _validate_sign(app, raw)


def test_english_words_from_the_prompt_are_rejected(app, sign_reply):
    with pytest.raises(ReadingRejected, match="чужда дума"):
        _validate_sign(app, sign_reply(last=" Mars е в Trine със Saturn."))


def test_degree_brackets_are_removed_not_rejected(app, sign_reply):
    """„(отклонение 2.6°)“ се маха направо — не струва нов опит."""
    out = _validate_sign(app, sign_reply(last=" Венера (отклонение 2.6°) зове. Края."))
    assert "отклонение" not in out and "Венера зове." in out


@pytest.mark.parametrize("leak", [
    " Точността е 0,6° днес.", " Имум Коели е в Нептун.",
    " Частта на щастието е в Лъв.", " Част на щастието е близо.", " Ретроградният Меркурий не е тук.",
])
def test_data_leaks_are_rejected(app, sign_reply, leak):
    with pytest.raises(ReadingRejected):
        _validate_sign(app, sign_reply(last=leak + " Края."))


@pytest.mark.parametrize("claim", [
    " Това може да засили възпалителни процеси.", " Не е моментът за инвестиции.",
    " Подпиши договора веднага.", " Стомахът ти ще реагира.",
])
def test_soft_health_and_money_claims_are_rejected(app, sign_reply, claim):
    with pytest.raises(ReadingRejected):
        _validate_sign(app, sign_reply(last=claim))


def test_truncated_reply_is_rejected(app, sign_reply):
    raw = sign_reply()
    with pytest.raises(ReadingRejected, match="цяло изречение"):
        _validate_sign(app, raw.rstrip(". ") + " а след това")


def test_bad_json_is_rejected(app, sign_reply):
    raw = sign_reply().replace('"energy": "Средна",', '"energy": "Средна" "do": 1,')
    with pytest.raises(ReadingRejected, match="JSON"):
        _validate_sign(app, raw)
    with pytest.raises(ReadingRejected, match="резюме"):
        _validate_sign(app, "Просто текст без резюме. " * 120)


@pytest.mark.parametrize("override,message", [
    ({"energy": "Страхотна"}, "energy"),
    ({"do": ["само една"]}, "do"),
    ({"avoid": []}, "avoid"),
    ({"mood": ""}, "mood"),
    ({"mood": "Много дълго и сложно описание на деня"}, "mood"),
    ({"focus": None}, "focus"),
    ({"caution": "<едно кратко изречение>"}, "ъглови скоби"),
    ({"teaser": ""}, "teaser"),
    ({"teaser": "Денят носи тих ритъм и време за разговори и подреждане на мислите."}, "Денят"),
    ({"teaser": "Тих ритъм " + "и още думи " * 20 + "накрая."}, "teaser"),
    ({"teaser": "Тих ритъм без точка"}, "изречение"),
])
def test_summary_fields_are_checked(app, sign_reply, override, message):
    with pytest.raises(ReadingRejected, match=message):
        _validate_sign(app, sign_reply(summary=override))


def test_wrong_number_of_sections_or_size_is_rejected(app, sign_reply, personal_reply):
    nine = make_reply(app.PERSONAL_SECTION_TITLES)
    with pytest.raises(ReadingRejected, match="разделите са 9"):
        _validate_sign(app, nine)
    with pytest.raises(ReadingRejected, match="размерът"):
        _validate_sign(app, sign_reply(sentences=1))
    # Прекалено дълъг, но цял — само бележка (SoftRejected носи годния текст).
    with pytest.raises(bg_text.SoftRejected, match="знака"):
        _validate_sign(app, sign_reply(sentences=40))


def test_long_personal_horoscope_is_accepted(app, personal_reply):
    """Истинските лични хороскопи (9 раздела) излизат по 8–10 хил. знака —
    на production се отхвърляха и пишеха втори път."""
    raw = personal_reply(sentences=8)
    body = raw.split("---END---", 1)[1]
    assert 8000 < len(body) < 11000
    out = validate_horoscope(raw, titles=app.PERSONAL_SECTION_TITLES)
    assert out.startswith("---SUMMARY---")
    five = make_reply(app.SIGN_SECTION_TITLES[:5])
    with pytest.raises(ReadingRejected, match="разделите са 5"):
        _validate_sign(app, five)


def test_relative_days_only_when_the_text_is_for_today(app, sign_reply):
    raw = sign_reply(last=" Днес е добре да си починеш.")
    assert _validate_sign(app, raw, relative_days=True)
    with pytest.raises(ReadingRejected, match="друг ден"):
        _validate_sign(app, raw, relative_days=False)
    with pytest.raises(ReadingRejected, match="друг ден"):
        _validate_sign(app, sign_reply(last=" Утре ще е по-леко."), relative_days=False)


def test_midsentence_aspect_names_are_lowercased(app, sign_reply):
    raw = sign_reply().replace("а Слънцето във Везни", "а Слънцето в Секстил с Юпитер", 1)
    out = _validate_sign(app, raw)
    assert "в секстил с Юпитер" in out and "Секстил" not in out


def test_personal_reply_keeps_nine_titles(app, personal_reply):
    out = validate_horoscope(personal_reply(), titles=app.PERSONAL_SECTION_TITLES)
    assert "9. **Есенцията на деня**" in out
    assert "teaser" not in app.split_summary(out)[0]


# --- повторен опит ----------------------------------------------------------------

def test_one_retry_with_the_reason_then_success(app, sign_reply):
    prompts = []
    replies = [sign_reply(last=" Имум Коели е в Нептун."), sign_reply()]

    def call(p):
        prompts.append(p)
        return replies.pop(0)

    out = app.generate_checked(call, "ПОДКАНА", lambda r: _validate_sign(app, r), "тест")
    assert out and len(prompts) == 2
    assert prompts[0] == "ПОДКАНА" and "ОТХВЪРЛЕН" in prompts[1] and "Имум Коели" in prompts[1]


def test_two_broken_replies_end_in_an_ai_error(app, sign_reply):
    """Счупен текст (отрязан) — два опита и грешка; в кеша не влиза нищо."""
    calls = []
    bad = sign_reply().rstrip(". ") + " а след това"
    with pytest.raises(app.AIError, match="проверката"):
        app.generate_checked(lambda p: calls.append(1) or bad, "П",
                             lambda r: _validate_sign(app, r), "тест")
    assert len(calls) == 2            # един нов опит, не повече


def test_two_rule_breaks_still_give_a_horoscope(app, sign_reply):
    """Цял текст с нарушено правило и на втория опит — записва се, не оставя
    човека без хороскоп (нарушението е в лога)."""
    calls = []
    bad = sign_reply(last=" Имум Коели.")
    out = app.generate_checked(lambda p: calls.append(1) or bad, "П",
                               lambda r: _validate_sign(app, r), "тест")
    assert len(calls) == 2 and out.startswith("---SUMMARY---")


# --- T4: finish_reason ---------------------------------------------------------------

class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _reply(monkeypatch, *payloads):
    queue = list(payloads)
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(queue.pop(0)))


def _choice(text, finish):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5}}


@pytest.mark.parametrize("finish", ["stop", None])
def test_finish_reason_stop_or_missing_is_accepted(app, monkeypatch, finish):
    _reply(monkeypatch, _choice("Готов текст.", finish))
    assert app.call_ai("k", "deepseek", "п") == "Готов текст."


@pytest.mark.parametrize("finish", ["content_filter", "insufficient_system_resource", "tool_calls"])
def test_any_other_finish_reason_is_an_error(app, monkeypatch, finish):
    _reply(monkeypatch, _choice("Част от текста, но спрян.", finish))
    with pytest.raises(app.AIError, match=finish):
        app.call_ai("k", "deepseek", "п")


def test_length_still_retries_with_double_limit(app, monkeypatch):
    _reply(monkeypatch, _choice("Отрязан", "length"), _choice("Цял текст.", "stop"))
    assert app.call_ai("k", "deepseek", "п") == "Цял текст."


def test_anthropic_refusal_is_an_error(app, monkeypatch):
    _reply(monkeypatch, {"content": [{"type": "text", "text": "Не мога."}], "stop_reason": "refusal"})
    with pytest.raises(app.AIError, match="refusal"):
        app.call_ai("k", "anthropic", "п")


# --- T5/T7: подканите ---------------------------------------------------------------

def _sign_prompt(app, slug="oven", date_iso="2026-10-15"):
    return app.sign_horoscope_prompt(app.ZODIAC_BY_SLUG[slug], app._bg_date(date_iso), date_iso,
                                     _real_sky(app, date_iso))


def test_sign_prompt_has_no_example_phrases_only_the_format(app):
    prompt = _sign_prompt(app)
    for phrase in EXAMPLE_PHRASES:
        assert phrase not in prompt.lower()
    assert '"mood": "<едно съществително' in prompt
    assert "Съсредоточен" not in prompt and "Емоционален" not in prompt


def test_sign_prompt_rules(app):
    prompt = _sign_prompt(app)
    assert "без думата „Внимавай“" in prompt
    assert "ЕЗИК БЕЗ РОД" in prompt and "сам/сама" in prompt
    assert "ЗДРАВЕ: само самочувствие, сън, движение и почивка" in prompt
    assert "ПАРИ: само нагласа" in prompt and "инвестирай" in prompt
    assert "никакви градуси, отклонения и числа в скоби" in prompt
    assert "teaser" in prompt and "не започва с „Денят“" in prompt
    assert "Имум Коели" in prompt.split("КАК ДА ПИШЕШ")[1]       # само като забрана


def test_sign_prompt_asks_for_exactly_six_sections(app):
    prompt = _sign_prompt(app)
    for n, title in enumerate(app.SIGN_SECTION_TITLES, 1):
        assert f"{n}. **{title}**" in prompt
    assert "7. **" not in prompt and "шестте заглавия" in prompt
    assert "Какво да избягваш" not in prompt and "В какво да внимаваш" not in prompt


def test_prompt_for_another_day_forbids_relative_days(app):
    today = app.sofia_today()
    tomorrow = (today + datetime.timedelta(days=1)).isoformat()
    other = _sign_prompt(app, date_iso=tomorrow)
    assert f"Текстът е за {app._bg_date(tomorrow)} и ще се чете и в друг ден" in other
    assert "не пиши „днес“" in other
    same = _sign_prompt(app, date_iso=today.isoformat())
    assert "не пиши „днес“" not in same


def test_prompt_passes_yesterdays_mood_and_focus(app, sign_reply):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM sign_horoscope")
        c.commit()
    sign = app.ZODIAC_BY_SLUG["oven"]
    app.set_sign_horoscope(sign["sign"], "2026-10-14",
                           _validate_sign(app, sign_reply(summary={"mood": "Увереност", "focus": "нови начинания"})))
    prompt = _sign_prompt(app, date_iso="2026-10-15")
    assert "ПРЕДИШНИЯТ ХОРОСКОП ЗА ТОЗИ ЗНАК" in prompt
    assert "настроение „Увереност“, фокус „нови начинания“" in prompt
    assert "14.10.2026" in prompt
    # Без предишен текст — няма и раздел.
    assert "ПРЕДИШНИЯТ" not in _sign_prompt(app, slug="telec", date_iso="2026-10-15")


def test_twelve_prompts_are_mostly_different(app):
    prompts = [_sign_prompt(app, slug) for slug in app.ZODIAC_BY_SLUG]
    assert len(set(prompts)) == 12
    sky_parts = [p.split("=== НЕБЕТО")[1].split("=== ЗАДАЧА")[0] for p in prompts]
    assert len(set(sky_parts)) == 12


# --- генерирането на зодийния хороскоп -----------------------------------------------

def _fake_ai(monkeypatch, app, replies, seen=None):
    monkeypatch.setattr(app, "ai_config_or_raise", lambda: ("key", "deepseek"))
    queue = list(replies)

    def fake(key, provider, prompt, max_tokens=4000, model=None):
        if seen is not None:
            seen.append({"prompt": prompt, "max_tokens": max_tokens})
        return queue.pop(0)
    monkeypatch.setattr(app, "call_ai", fake)


def _clear_signs(app):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM sign_horoscope")
        c.commit()


def test_sign_generation_stores_the_checked_text(app, monkeypatch, sign_reply):
    _clear_signs(app)
    seen = []
    raw = sign_reply().replace("откровени", f"{LAT_O}ткровени")
    _fake_ai(monkeypatch, app, [raw], seen)
    sign, iso = app.ZODIAC_BY_SLUG["oven"], app.sofia_today().isoformat()
    out = app._generate_sign_horoscope(sign, "тест", iso)
    stored = app.get_sign_horoscope(sign["sign"], iso)
    assert stored == out and f"{LAT_O}ткровени" not in stored and "откровени" in stored
    assert seen[0]["max_tokens"] == 4000


def test_rejected_text_never_reaches_the_cache(app, monkeypatch, sign_reply):
    _clear_signs(app)
    bad = sign_reply().rstrip(". ") + " а след това"
    seen = []
    _fake_ai(monkeypatch, app, [bad, bad], seen)
    sign, iso = app.ZODIAC_BY_SLUG["oven"], app.sofia_today().isoformat()
    with pytest.raises(app.AIError):
        app._generate_sign_horoscope(sign, "тест", iso)
    assert app.get_sign_horoscope(sign["sign"], iso) is None
    assert len(seen) == 2


def test_empty_sky_is_an_error_not_a_prompt_without_data(app, monkeypatch, sign_reply):
    _clear_signs(app)
    seen = []
    _fake_ai(monkeypatch, app, [sign_reply()], seen)
    monkeypatch.setattr(app, "sky_for_date", lambda d: dict(app._EMPTY_SKY))
    sign, iso = app.ZODIAC_BY_SLUG["oven"], app.sofia_today().isoformat()
    with pytest.raises(app.AIError, match="не се сметна"):
        app._generate_sign_horoscope(sign, "тест", iso)
    assert seen == [] and app.get_sign_horoscope(sign["sign"], iso) is None


def test_tomorrows_text_saying_today_gets_one_retry(app, monkeypatch, sign_reply):
    _clear_signs(app)
    tomorrow = (app.sofia_today() + datetime.timedelta(days=1)).isoformat()
    bad = sign_reply(last=" Днес е добре да си починеш.")
    seen = []
    _fake_ai(monkeypatch, app, [bad, sign_reply()], seen)
    app._generate_sign_horoscope(app.ZODIAC_BY_SLUG["oven"], "тест", tomorrow)
    assert len(seen) == 2 and "Днес е добре" not in app.get_sign_horoscope("Aries", tomorrow)


# --- H6: пауза след неуспех -------------------------------------------------------------

def _finished_job(error, age):
    ev = threading.Event()
    ev.set()
    return {"done": ev, "error": error, "finished_at": time.monotonic() - age}


def test_warm_waits_five_minutes_after_a_failure(app, monkeypatch):
    _clear_signs(app)
    today = app.sofia_today().isoformat()
    started = []
    monkeypatch.setattr(app, "ai_job", lambda key, fn: started.append(key) or _finished_job(None, 0))
    with app._AI_JOBS_LOCK:
        app._AI_JOBS[f"sign:Aries:{today}"] = _finished_job("AI грешка", 120)       # преди 2 мин
        app._AI_JOBS[f"sign:Taurus:{today}"] = _finished_job("AI грешка", 400)      # преди ~7 мин
    try:
        assert app.warm_sign_horoscopes() == 23
    finally:
        with app._AI_JOBS_LOCK:
            app._AI_JOBS.pop(f"sign:Aries:{today}", None)
            app._AI_JOBS.pop(f"sign:Taurus:{today}", None)
    assert f"sign:Aries:{today}" not in started
    assert f"sign:Taurus:{today}" in started
    assert app.SIGN_WARM_PAUSE >= 300


# --- ?refresh=true&date= за админа ------------------------------------------------------

def _request(token=None):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers,
                    "query_string": b"", "client": ("1.2.3.4", 1000), "server": ("t", 80),
                    "scheme": "https"})


@pytest.fixture
def admin_token(app, db):
    row = db.create_user(f"adm-{secrets.token_hex(3)}@example.com", db.hash_password("x"))
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("UPDATE users SET role = 'admin' WHERE id = ?", (row["id"],))
        c.commit()
    return app.create_token(row["id"], row["email"])


def test_admin_can_refresh_tomorrows_text(app, monkeypatch, admin_token):
    keys = []
    done = threading.Event()
    done.set()
    monkeypatch.setattr(app, "ai_job", lambda key, fn: keys.append(key) or {"done": done, "error": None})
    tomorrow = (app.sofia_today() + datetime.timedelta(days=1)).isoformat()
    app.api_horoskop("oven", _request(admin_token), refresh=True, date=tomorrow)
    assert keys == [f"sign:Aries:{tomorrow}"]


def test_date_is_ignored_for_everyone_else(app, monkeypatch):
    keys = []
    done = threading.Event()
    done.set()
    monkeypatch.setattr(app, "ai_job", lambda key, fn: keys.append(key) or {"done": done, "error": None})
    _clear_signs(app)
    tomorrow = (app.sofia_today() + datetime.timedelta(days=1)).isoformat()
    app.api_horoskop("oven", _request(), refresh=True, date=tomorrow)
    assert keys == [f"sign:Aries:{app.sofia_today().isoformat()}"]


def test_admin_date_must_be_today_or_tomorrow(app, admin_token):
    from fastapi import HTTPException
    far = (app.sofia_today() + datetime.timedelta(days=5)).isoformat()
    for bad in (far, "вчера", "2020-01-01"):
        with pytest.raises(HTTPException) as e:
            app.api_horoskop("oven", _request(admin_token), refresh=True, date=bad)
        assert e.value.status_code == 400


# --- T6/T7: страниците ---------------------------------------------------------------------

def _client(app):
    from fastapi.testclient import TestClient
    return TestClient(app.app, base_url="https://astrokarta.bg")


def _store_sign(app, sign_reply, slug="oven", iso=None, **kw):
    sign = app.ZODIAC_BY_SLUG[slug]
    iso = iso or app.sofia_today().isoformat()
    app.set_sign_horoscope(sign["sign"], iso, _validate_sign(app, sign_reply(**kw)))
    return sign


def test_guest_sees_three_of_six_sections_and_the_disclaimer(app, db, sign_reply):
    _clear_signs(app)
    _store_sign(app, sign_reply)
    html = _client(app).get("/horoskop/oven").text
    assert "Още 3 раздела" in html
    assert "С информативна и развлекателна цел — не е медицински или финансов съвет." in html
    visible = html.split('class="horo-locked"')[0]
    for title in app.SIGN_SECTION_TITLES[:3]:
        assert title in visible
    assert "Здраве и енергия" not in visible


def test_meta_description_is_the_teaser(app, db, sign_reply):
    _clear_signs(app)
    _store_sign(app, sign_reply, summary={"teaser": "Овен днес черпи сила от тихия ритъм на деня."})
    html = _client(app).get("/horoskop/oven").text
    desc = html.split('<meta name="description" content="')[1].split('"')[0]
    assert "тихия ритъм на деня" in desc and len(desc) <= 160


def test_hub_uses_the_teaser_as_the_excerpt(app, db, sign_reply):
    _clear_signs(app)
    _store_sign(app, sign_reply, slug="oven", summary={"teaser": "Овен получава време за тих разговор."})
    _store_sign(app, sign_reply, slug="telec", summary={"teaser": "Телец се учи да не бърза с отговорите."})
    html = _client(app).get("/horoskop").text
    assert "Овен получава време за тих разговор." in html
    assert "Телец се учи да не бърза с отговорите." in html
    assert "С информативна и развлекателна цел" in html


def test_old_texts_without_a_teaser_still_get_an_excerpt(app, db):
    _clear_signs(app)
    app.set_sign_horoscope("Aries", app.sofia_today().isoformat(),
                           '---SUMMARY---\n{"mood": "Добър ден"}\n---END---\n\n'
                           "1. **Общо усещане за деня**\nСтарият текст е още тук и се чете добре. Втори ред.")
    assert "Старият текст е още тук" in _client(app).get("/horoskop").text


def test_teaser_that_is_too_long_or_starts_with_denyat_is_not_used(app):
    assert app._summary_teaser({"teaser": "Тих ден."}) == ""
    assert app._summary_teaser({"teaser": "Денят е спокоен и дава време за дълъг разговор."}) == ""
    assert app._summary_teaser({"teaser": "а" * 200}) == ""
    assert app._summary_teaser({"teaser": "Тих ден за дълъг разговор и пауза."}) != ""
    assert app._summary_teaser(None) == ""


def test_six_sections_split_into_three_and_three(app):
    html = "".join(f"<h3>{i}</h3><p>текст</p>" for i in range(6))
    free, hidden, count = app._split_reading(html)
    assert free.count("<h3") == 3 and hidden.count("<h3") == 3 and count == 3


# --- T8: личният хороскоп ------------------------------------------------------------------

def _person(app, user_id, *, unknown=False, tz="Europe/Sofia"):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon, timezone,"
            " time_unknown) VALUES (?, 'Мария Петрова', 1990, 5, 14, ?, ?, 42.7, 23.3, ?, ?)",
            (user_id, 12 if unknown else 8, 0 if unknown else 30, tz, int(unknown)))
        c.commit()
        return cur.lastrowid


def _aspect(active, kind, passive, deviation):
    return {"type": kind, "type_bg": "", "active": active, "passive": passive,
            "active_bg": "", "passive_bg": "", "deviation": deviation,
            "difference": f"-00°{int(deviation * 60):02d}'00\""}


FAKE_ASPECTS = [
    _aspect("Mars", "Trine", "Saturn", 0.4),
    _aspect("Moon", "Square", "Venus", 0.9),
    _aspect("Venus", "Sextile", "Moon", 1.1),
    _aspect("Sun", "Conjunction", "Asc", 0.3),
    _aspect("Jupiter", "Opposition", "MC", 0.5),
    _aspect("Mercury", "Sextile", "Part of Fortune", 0.6),
]


def _run_personal(app, monkeypatch, user, personal_reply, *, unknown=False, tz="Europe/Sofia"):
    pid = _person(app, user["id"], unknown=unknown, tz=tz)
    calls, prompts = [], []

    def fake_transits(person, when):
        calls.append(when)
        return {"transit_aspects_to_natal": [dict(a) for a in FAKE_ASPECTS],
                "moon_phase_bg": "Растящ сърп", "shape_bg": "Сноп"}

    monkeypatch.setattr(app, "compute_transits", fake_transits)
    monkeypatch.setattr(app, "get_ai_config", lambda: ("key", "deepseek"))
    monkeypatch.setattr(app, "call_ai", lambda k, p, prompt, **kw: prompts.append(prompt) or personal_reply())
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM ai_cache WHERE person_id = ?", (pid,))
        c.commit()
    app.api_daily_horoscope(pid, user=(user["id"], user["email"]))
    _wait_jobs(app)
    return pid, calls, prompts


def test_personal_transits_are_for_local_noon(app, user, monkeypatch, personal_reply):
    for tz in ("Europe/Sofia", "America/New_York", "Pacific/Auckland"):
        _, calls, _ = _run_personal(app, monkeypatch, user, personal_reply, tz=tz)
        assert len(calls) == 1
        when = calls[0]
        assert (when.hour, when.minute, when.second) == (12, 0, 0)
        assert when.tzinfo is not None and str(when.tzinfo) == tz
        assert when.date() == datetime.datetime.now(ZoneInfo(tz)).date()


def test_personal_prompt_has_bulgarian_names_and_no_numbers(app, user, monkeypatch, personal_reply):
    _, _, prompts = _run_personal(app, monkeypatch, user, personal_reply)
    prompt = prompts[0]
    assert "транзитен Марс е в тригон с натален Сатурн" in prompt
    assert "транзитна Луна е в квадрат с натална Венера" in prompt
    assert "транзитна Венера е в секстил с натална Луна" in prompt
    for banned in ("Trine", "Square", "Sextile", "Mars", "Saturn", "(транзит)", "(натал)",
                   "отклонение", "°", "Bundle", "Waxing"):
        assert banned not in prompt
    assert "Растящ сърп" in prompt


def test_personal_prompt_has_no_example_phrases_and_keeps_nine_titles(app, user, monkeypatch, personal_reply):
    _, _, prompts = _run_personal(app, monkeypatch, user, personal_reply)
    prompt = prompts[0]
    for phrase in EXAMPLE_PHRASES:
        assert phrase not in prompt.lower()
    for n, title in enumerate(app.PERSONAL_SECTION_TITLES, 1):
        assert f"{n}. **{title}**" in prompt
    assert "Есенцията на деня" in prompt
    assert "финансови решения" not in prompt
    assert "ЕЗИК БЕЗ РОД" in prompt and "ЗДРАВЕ: само" in prompt and "ПАРИ: само" in prompt
    assert "без думата „Внимавай“" in prompt


def test_unknown_birth_time_changes_the_personal_prompt(app, user, monkeypatch, personal_reply):
    _, _, known = _run_personal(app, monkeypatch, user, personal_reply, unknown=False)
    assert app.TIME_UNKNOWN_NOTE.strip() not in known[0]
    assert "натален Сатурн" in known[0] and "натална Луна" in known[0]

    _, _, unknown = _run_personal(app, monkeypatch, user, personal_reply, unknown=True)
    prompt = unknown[0]
    assert app.TIME_UNKNOWN_NOTE.strip() in prompt
    assert "натална Луна" not in prompt                  # Венера–Луна и Луна–Венера: само Луна като натална
    assert "натален Сатурн" in prompt and "натална Венера" in prompt
    for banned in ("Асцендент", "Част на щастието", "Медиум Коели"):
        assert banned not in prompt.split("=== АКТИВНИ")[1].split("=== ЗАДАЧА")[0]


def test_drop_unreliable_natal_aspects(app):
    kept = app.drop_unreliable_natal_aspects(FAKE_ASPECTS)
    assert {(a["active"], a["passive"]) for a in kept} == {("Mars", "Saturn"), ("Moon", "Venus")}


def test_personal_text_is_checked_before_the_cache(app, user, monkeypatch, personal_reply):
    pid, _, _ = _run_personal(app, monkeypatch, user, lambda: "Просто текст без резюме.")
    assert app.get_ai_cache(pid, f"horoscope:{app.sofia_today().isoformat()}") is None


def test_personal_reading_that_passes_is_cached_with_nine_sections(app, user, monkeypatch, personal_reply):
    pid, _, _ = _run_personal(app, monkeypatch, user, personal_reply)
    tz_today = datetime.datetime.now(ZoneInfo("Europe/Sofia")).date().isoformat()
    cached = app.get_ai_cache(pid, f"horoscope:{tz_today}")
    assert cached and cached["content"].count("**") == 18
    assert "9. **Есенцията на деня**" in cached["content"]
