# -*- coding: utf-8 -*-
"""Броячът на прегледите: кое не е човек и откъде идва общият брой."""
import datetime
import sqlite3

import pytest


@pytest.fixture
def clean(app, db):
    with sqlite3.connect(app.DB_PATH) as c:
        c.execute("DELETE FROM page_views")
        c.commit()
    yield app


class _Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


CHROME = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/129.0 Mobile Safari/537.36"


@pytest.mark.parametrize("headers,bot", [
    ({"user-agent": CHROME, "accept-language": "bg-BG,bg;q=0.9"}, False),
    ({"user-agent": CHROME}, True),                                   # без Accept-Language
    ({"user-agent": "", "accept-language": "bg"}, True),
    ({"user-agent": "WhatsApp/2.23.20.0", "accept-language": "bg"}, True),
    ({"user-agent": "python-httpx/0.27", "accept-language": "bg"}, True),
    ({"user-agent": CHROME + " Chrome-Lighthouse", "accept-language": "en"}, True),
    ({"user-agent": "Mozilla/5.0 (compatible; UptimeRobot/2.0)", "accept-language": "en"}, True),
    ({"user-agent": CHROME, "accept-language": "bg", "sec-purpose": "prefetch;prerender"}, True),
])
def test_not_a_reader(app, headers, bot):
    req = _Req(headers)
    assert app._not_a_reader(req, req.headers.get("user-agent", "").lower()) is bot


def test_page_view_middleware_skips_scripts(clean):
    import time
    from fastapi.testclient import TestClient
    client = TestClient(clean.app, base_url="https://astrokarta.bg")
    client.get("/", headers={"user-agent": CHROME})                          # скрипт: без език
    client.get("/", headers={"user-agent": CHROME, "accept-language": "bg"})  # човек
    for _ in range(50):
        with sqlite3.connect(clean.DB_PATH) as c:
            n = c.execute("SELECT COUNT(*) FROM page_views").fetchone()[0]
        if n:
            break
        time.sleep(0.05)
    time.sleep(0.2)
    with sqlite3.connect(clean.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM page_views").fetchone()[0] == 1


def test_one_visitor_is_capped_per_day(clean, monkeypatch):
    monkeypatch.setattr(clean, "PAGE_VIEWS_PER_VISITOR_DAY", 3)
    monkeypatch.setattr(clean, "_PV_VISITOR_COUNTS", {})
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    for _ in range(10):
        clean._record_page_view("/", None, now, {"visitor": "abc"})
    clean._record_page_view("/", None, now, {"visitor": "other"})
    with sqlite3.connect(clean.DB_PATH) as c:
        rows = dict(c.execute("SELECT visitor, COUNT(*) FROM page_views GROUP BY visitor").fetchall())
    assert rows == {"abc": 3, "other": 1}


def test_reports_split_people_from_scripts(clean):
    now = datetime.datetime.utcnow().isoformat(timespec="seconds")
    with sqlite3.connect(clean.DB_PATH) as c:
        for i in range(60):
            c.execute("INSERT INTO page_views (path, viewed_at, visitor, device) VALUES (?, ?, 'scraper', 'desktop')",
                      (f"/horoskop/{i}", now))
        for v in ("a", "b", "c"):
            c.execute("INSERT INTO page_views (path, viewed_at, visitor, referrer) VALUES ('/', ?, ?, 'google.com')", (now, v))
        c.execute("INSERT INTO page_views (path, viewed_at) VALUES ('/', ?)", (now,))   # стар запис
        c.commit()
    t = clean.build_reports(30)["traffic"]
    assert t["views"] == 64 and t["heavy_visitors"] == 1 and t["heavy_views"] == 60
    assert t["unhashed_views"] == 1 and t["human_views"] == 3
    assert t["heavy"][0]["pages"] == 60 and len(t["heavy"][0]["visitor"]) <= 8
    assert sum(d["views"] for d in t["by_day"]) == 64
    assert sum(d["heavy_views"] for d in t["by_day"]) == 60
