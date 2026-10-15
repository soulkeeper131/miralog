# -*- coding: utf-8 -*-
"""Обща основа на изчистването: едно име на модул, грешки на човешки език,
страница 404, имейли без „Здравей, ime.prezime!“."""
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent


# --- имена ---------------------------------------------------------------------

def test_every_module_has_one_full_and_one_short_name(app):
    for f in app.FEATURE_CATALOGUE:
        assert f.get("name") and f.get("short"), f["key"]
        assert f.get("note") != "Какво ще узнаеш?", f["key"]


@pytest.mark.parametrize("old", [
    "Лунен хороскоп календар", "Хороскоп за конкретен период",
    "емоционална съвместимост", "Личен портрет", "Астро портрет",
    "астрологичен профил", "по-силния модел",
])
def test_old_module_names_are_gone_from_what_the_client_sees(old):
    """Един продукт носеше по 3–5 имена между менюто, таба, PDF-а и имейлите."""
    files = list((ROOT / "templates").glob("*.html")) + [
        ROOT / "static" / n for n in ("locked.js", "modules.js", "onboarding.js")]
    for path in files:
        if path.name == "admin.html":
            continue
        text = path.read_text(encoding="utf-8")
        visible = re.sub(r"<!--.*?-->|/\*.*?\*/|//[^\n]*|\{#.*?#\}", "", text, flags=re.S)
        assert old not in visible, f"{path.name}: {old}"


def test_bundle_is_called_all_readings(app):
    assert app.BUNDLE_NAME == "Всички разчитания"


# --- грешки --------------------------------------------------------------------

def test_unknown_page_is_an_html_page_for_browsers():
    from fastapi.testclient import TestClient
    import app as app_module
    client = TestClient(app_module.app)
    r = client.get("/nyama-takava-stranitsa", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Тази страница не съществува" in r.text and "Към началото" in r.text
    assert 'name="viewport"' in r.text


def test_unknown_api_and_files_stay_json():
    from fastapi.testclient import TestClient
    import app as app_module
    client = TestClient(app_module.app)
    r = client.get("/api/nyama", headers={"accept": "text/html"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    r = client.get("/static/nyama.css", headers={"accept": "text/html"})
    assert r.status_code == 404 and "Тази страница" not in r.text


def test_failed_stripe_checkout_is_an_error_not_a_promise_to_call(app, user, monkeypatch):
    """Гръмнал Stripe връщаше manual: „Ще се свържем с теб“ — а никой не се свързваше."""
    from starlette.requests import Request
    monkeypatch.setattr(app.billing, "stripe_enabled", lambda: True)

    def boom(**kw):
        raise RuntimeError("No such price: sk_live_secret")
    monkeypatch.setattr(app.billing, "create_feature_checkout", boom)
    monkeypatch.setattr(app, "report_checkout_failure", lambda *a, **k: None)
    req = Request({"type": "http", "method": "POST", "path": "/api/features/moon/request",
                   "headers": [(b"host", b"astrokarta.bg")], "query_string": b"",
                   "scheme": "https", "server": ("astrokarta.bg", 443), "client": ("1.2.3.4", 1)})
    with pytest.raises(HTTPException) as e:
        app.api_request_feature("moon", req, user=(user["id"], user["email"]))
    assert e.value.status_code == 502
    assert "Не е удържана сума" in e.value.detail
    assert "sk_live" not in e.value.detail


def test_person_not_found_is_in_bulgarian():
    text = (ROOT / "app.py").read_text(encoding="utf-8")
    assert "not found\")" not in text


# --- имейли --------------------------------------------------------------------

def test_greeting_without_a_name_is_plain(app):
    _, body = app.render_email_template("welcome", name="", link="https://x/dashboard")
    assert body.startswith("Здравей!")
    _, body = app.render_email_template("reset_password", name="", link="https://x/r")
    assert body.startswith("Здравей!")
    _, body = app.render_email_template("reset_password", name="Мария", link="https://x/r")
    assert body.startswith("Здравей, Мария!")


def test_digest_has_a_way_out(app):
    subject, body = app.render_email_template(
        "digest", name="", reading="…", date="15.10.2026",
        settings_link="https://astrokarta.bg/settings")
    assert "15.10.2026" in subject
    assert "https://astrokarta.bg/settings" in body


def test_terms_do_not_mention_a_subscription():
    text = (ROOT / "templates" / "terms.html").read_text(encoding="utf-8")
    assert "Абонаментът може" not in text and "платения период" not in text


# --- синтаксис на JavaScript ---------------------------------------------------

@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="няма node")
def test_every_script_parses():
    """`await` в функция без async счупи modules.js — нито един бутон за покупка
    в картата не работеше. Проверява файловете и вградените скриптове."""
    import subprocess
    import tempfile
    bad = []
    for path in sorted((ROOT / "static").glob("*.js")):
        r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        if r.returncode:
            bad.append(f"{path.name}: {r.stderr.strip().splitlines()[:4]}")
    for path in sorted((ROOT / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        for i, m in enumerate(re.finditer(
                r"<script(?![^>]*\bsrc=)(?![^>]*ld\+json)[^>]*>(.*?)</script>", text, re.S)):
            code = re.sub(r"\{%.*?%\}", "", re.sub(r"\{\{.*?\}\}", "0", m.group(1)), flags=re.S)
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
                f.write(code)
            r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
            Path(f.name).unlink()
            if r.returncode:
                bad.append(f"{path.name} #{i}: {r.stderr.strip().splitlines()[:4]}")
    assert not bad, "\n".join(bad)
