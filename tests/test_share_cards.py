# -*- coding: utf-8 -*-
"""Картите за споделяне: картинки за всяка мрежа и преглед на линка без JS."""
import asyncio
import io
import sqlite3

import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request


def _req(path="/"):
    return Request({"type": "http", "method": "GET", "path": path, "headers": [(b"host", b"astrokarta.bg")],
                    "query_string": b"", "scheme": "https", "server": ("astrokarta.bg", 443),
                    "client": ("1.2.3.4", 1)})


def _person(app, user_id, name="Мария Иванова Петрова"):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("INSERT INTO persons (user_id, name, year, month, day, hour, minute, lat, lon, timezone)"
                  " VALUES (?, ?, 1990, 5, 14, 8, 30, 42.7, 23.3, 'Europe/Sofia')", (user_id, name))
        c.commit()
        return c.execute("SELECT MAX(id) FROM persons").fetchone()[0]


@pytest.fixture(autouse=True)
def _og_tmp(app, tmp_path, monkeypatch):
    monkeypatch.setattr(app, "OG_DIR", tmp_path / "og")


def _image(resp):
    assert resp.media_type == "image/jpeg"
    return Image.open(io.BytesIO(resp.body))


@pytest.mark.parametrize("size,expected", [("og", (1200, 630)), ("square", (1080, 1080)),
                                           ("story", (1080, 1920)), ("нещо", (1200, 630))])
def test_sign_card_in_every_size(app, size, expected):
    img = _image(asyncio.run(app.og_sign_horoscope("oven", size=size)))
    assert img.size == expected


def test_cards_are_small_enough_for_whatsapp(app):
    """WhatsApp изпуска прегледи с картинка над ~300 KB."""
    for route, slug in ((app.og_sign_horoscope, "oven"), (app.og_sign_profile, "lav"),
                        (app.og_compatibility, "oven-lav")):
        for size in ("og", "story"):
            assert len(asyncio.run(route(slug, size=size)).body) < 300_000


def test_card_is_cached_on_disk(app):
    asyncio.run(app.og_sign_profile("lav", size="og"))
    files = list(app.OG_DIR.glob("*.jpg"))
    assert len(files) == 1
    asyncio.run(app.og_sign_profile("lav", size="og"))
    assert len(list(app.OG_DIR.glob("*.jpg"))) == 1


def test_unknown_slug_is_404(app):
    with pytest.raises(HTTPException) as e:
        asyncio.run(app.og_sign_horoscope("nyama", size="og"))
    assert e.value.status_code == 404


def test_old_cards_are_pruned(app):
    import os
    import time
    asyncio.run(app.og_sign_profile("lav", size="og"))
    f = next(app.OG_DIR.glob("*.jpg"))
    old = time.time() - (app.OG_KEEP_DAYS + 1) * 86400
    os.utime(f, (old, old))
    assert app.prune_og_cache() == 1 and not f.exists()


def _shared(app, user):
    app.grant_feature_purchase(user["id"], "profile", 500, "EUR", None)
    pid = _person(app, user["id"])
    app.set_ai_cache(pid, "profile", '---SUMMARY---\n{"mood": "спокойствие", "focus": "дом"}\n---END---\n'
                                     "1. **Кой си ти**\nРоден си на 14.05.1990 в София. Още текст.")
    res = app.api_create_share(pid, app.ShareCreate(cache_key="profile"), _req(),
                               user=(user["id"], user["email"]))
    return pid, res


def test_one_link_per_reading(app, db, user):
    """Всяко „Сподели“ правеше нов линк и старите се трупаха."""
    pid, first = _shared(app, user)
    again = app.api_create_share(pid, app.ShareCreate(cache_key="profile"), _req(),
                                 user=(user["id"], user["email"]))
    assert first["token"] == again["token"]
    assert "/og/share/" in first["image"]


def test_shared_page_has_preview_without_javascript(app, db, user):
    """Facebook/Viber/WhatsApp не изпълняват JS — прегледът беше празен."""
    _, res = _shared(app, user)
    resp = asyncio.run(app.share_page(_req(), res["token"]))
    html = resp.body.decode()
    assert resp.status_code == 200
    assert 'property="og:image"' in html and f"/og/share/{res['token']}.jpg" in html
    assert 'property="og:title"' in html and "Мария" in html
    assert "Петрова" not in html and "Иванова" not in html          # само малкото име
    assert resp.headers.get("x-robots-tag", "").startswith("noindex")


def test_shared_card_has_no_birth_data(app, db, user):
    _, res = _shared(app, user)
    meta = app._share_meta(res["token"])
    assert meta["name"] == "Мария"
    assert "1990" not in meta["description"]                        # резюмето, не текстът
    img = _image(asyncio.run(app.og_share(res["token"], size="og")))
    assert img.size == (1200, 630)


def test_missing_share_is_404(app):
    resp = asyncio.run(app.share_page(_req(), "nyama-takav"))
    assert resp.status_code == 404
    with pytest.raises(HTTPException):
        asyncio.run(app.og_share("nyama-takav"))


def test_uploaded_square_logo_is_not_the_link_image(app, monkeypatch):
    """Каченото лого (квадрат) ставаше картинката на всяка страница."""
    monkeypatch.setattr(app, "brand", lambda: {"name": "АстроКарта", "logo": "/uploads/logo.png",
                                               "domain": "astrokarta.bg", "slug": "AstroKarta"})
    app.set_setting("seo_og_image", "")
    assert app.seo_settings()["seo_og_image"] == "/static/og-image.jpg"


def test_sign_page_uses_its_own_card(app):
    ctx = app.seo_context(_req())
    app._own_og_image(ctx, _req(), "/og/horoskop/oven.jpg", "2026-10-15")
    assert ctx["seo_image"].endswith("/og/horoskop/oven.jpg?v=1-2026-10-15")
    assert ctx["seo_image_size"] == (1200, 630)
