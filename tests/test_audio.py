# -*- coding: utf-8 -*-
"""Гласовото четене: при отказ от Microsoft — без срив и без празни файлове.

На 13.10.2026 Microsoft промени услугата и edge-tts 7.2.8 започна да получава
403. Тогава се видяха два проблема: заявката гърмеше със „Срив на сайта“, а
неуспешният синтез оставяше празен mp3, който после се връщаше като готов —
човекът чуваше тишина без грешка, дори след поправката.
"""
import sqlite3
import sys
import types

import pytest
from fastapi import HTTPException


class _Refused(Exception):
    pass


def _fake_edge_tts(monkeypatch, fail=False, audio=b"ID3fake-mp3-frames" * 100):
    """Подменя edge_tts. Като истинския: save() създава файла преди да гърми."""
    calls = []

    class Communicate:
        def __init__(self, text, voice):
            calls.append(text)
            self.text = text

        async def save(self, path):
            open(path, "wb").close()          # истинският го прави точно така
            if fail:
                raise _Refused("403, message='Invalid response status'")
            with open(path, "wb") as fh:
                fh.write(audio)

        async def stream(self):
            if fail:
                raise _Refused("403, message='Invalid response status'")
            yield {"type": "audio", "data": audio}

    mod = types.ModuleType("edge_tts")
    mod.Communicate = Communicate
    monkeypatch.setitem(sys.modules, "edge_tts", mod)
    return calls


SHORT = "Кратко разчитане за проба."
LONG = ("Дълго разчитане. " * 400).strip()      # минава през частите


@pytest.mark.parametrize("text", [SHORT, LONG], ids=["short", "long"])
def test_failed_synthesis_leaves_no_file(app, monkeypatch, tmp_path, text):
    _fake_edge_tts(monkeypatch, fail=True)
    path = tmp_path / "x.mp3"
    with pytest.raises(Exception):
        app._text_to_audio(text, str(path))
    assert not path.exists(), "остана празен mp3, който после ще се връща като готов"
    assert not list(tmp_path.glob("*.part")), "остана недовършен файл"


@pytest.mark.parametrize("text", [SHORT, LONG], ids=["short", "long"])
def test_successful_synthesis_writes_audio(app, monkeypatch, tmp_path, text):
    _fake_edge_tts(monkeypatch)
    path = tmp_path / "x.mp3"
    app._text_to_audio(text, str(path))
    assert path.stat().st_size > 0


def _reading(app, user):
    with sqlite3.connect(app.DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO persons (user_id, name, year, month, day, hour, minute,"
            " lat, lon, timezone) VALUES (?, 'Тест', 1990, 5, 14, 8, 30, 42.7, 23.3, 'Europe/Sofia')",
            (user["id"],))
        c.commit()
        pid = cur.lastrowid
    key = "horoscope:2026-10-14"
    app.set_ai_cache(pid, key, "1. **Общо усещане за деня**\nСпокоен ден с ясни решения.")
    return pid, key


def test_empty_cached_audio_is_made_again(app, user, monkeypatch):
    """Празните файлове от аварията трябва да се оправят сами."""
    pid, key = _reading(app, user)
    calls = _fake_edge_tts(monkeypatch)
    app.api_reading_audio(pid, key, user=(user["id"], user["email"]))
    made = list((app.DB_PATH.parent / "audio").glob(f"{pid}_*.mp3"))
    assert made and made[0].stat().st_size > 0

    # Като на сървъра след аварията: файлът с правилното име, но празен.
    made[0].write_bytes(b"")
    calls.clear()
    app.api_reading_audio(pid, key, user=(user["id"], user["email"]))
    assert calls, "празният файл беше върнат като готов, без нов синтез"
    assert made[0].stat().st_size > 0


def test_refused_synthesis_is_a_clear_503(app, user, monkeypatch):
    pid, key = _reading(app, user)
    _fake_edge_tts(monkeypatch, fail=True)
    reported = []
    monkeypatch.setattr(app, "report_problem", lambda k, t, d, **kw: reported.append(k) or True)
    with pytest.raises(HTTPException) as err:
        app.api_reading_audio(pid, key, user=(user["id"], user["email"]))
    assert err.value.status_code == 503
    assert "гласовото четене" in err.value.detail.lower()
    assert reported == ["tts"], "трябва писмо „гласовото четене не работи“, не „срив“"
