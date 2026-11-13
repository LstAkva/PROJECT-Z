"""
Phase 8 (public growth) and the Phase 7/9 page wiring: robots.txt, sitemap, canonical and social
metadata, the share image, the Arena progress labels, homepage "continue" and the zero-time timer.
"""
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi import status

PUBLIC_PAGES = {"/": "/", "/arena": "/arena", "/daily": "/daily", "/privacy": "/privacy", "/terms": "/terms"}
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _head(client, path):
    res = client.get(path)
    assert res.status_code == status.HTTP_200_OK, path
    return res.text.split("</head>")[0]


# =========================================================================== robots.txt and sitemap

def test_robots_txt_allows_public_pages_and_points_to_the_sitemap(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)
    res = client.get("/robots.txt")
    assert res.status_code == status.HTTP_200_OK and res.headers["content-type"].startswith("text/plain")
    lines = res.text.splitlines()
    assert "User-agent: *" in lines and "Allow: /" in lines
    for private in ("/api/", "/owner", "/bank", "/builder", "/staging", "/play/", "/profile", "/u/"):
        assert f"Disallow: {private}" in lines, private
    for public in ("/arena", "/daily", "/privacy", "/terms"):
        assert f"Disallow: {public}" not in lines
    assert "Sitemap: https://zakowhat.uz/sitemap.xml" in lines


def test_sitemap_lists_only_canonical_public_pages_on_the_public_domain(client, monkeypatch):
    monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)
    res = client.get("/sitemap.xml")
    assert res.status_code == status.HTTP_200_OK and "xml" in res.headers["content-type"]
    urls = [loc.text for loc in ET.fromstring(res.text).findall("sm:url/sm:loc", NS)]
    assert urls == ["https://zakowhat.uz/", "https://zakowhat.uz/arena", "https://zakowhat.uz/daily",
                    "https://zakowhat.uz/privacy", "https://zakowhat.uz/terms"]
    for url in urls:
        assert not re.search(r"localhost|127\.0\.0\.1|/api/|/owner|/play/|/profile|/u/|/staging", url)


@pytest.mark.parametrize("value,expected", [("https://staging.zakowhat.uz", "https://staging.zakowhat.uz"),
                                            ("", "https://zakowhat.uz"), ("localhost:8000", "https://zakowhat.uz")])
def test_sitemap_and_robots_follow_the_public_origin_setting(client, monkeypatch, value, expected):
    monkeypatch.setenv("PUBLIC_SITE_URL", value)
    assert f"Sitemap: {expected}/sitemap.xml" in client.get("/robots.txt").text
    urls = [loc.text for loc in ET.fromstring(client.get("/sitemap.xml").text).findall("sm:url/sm:loc", NS)]
    assert all(u.startswith(expected + "/") for u in urls)


# =========================================================================== page metadata

@pytest.mark.parametrize("path,canonical_path", PUBLIC_PAGES.items())
def test_public_pages_have_canonical_and_social_metadata(client, monkeypatch, path, canonical_path):
    monkeypatch.delenv("PUBLIC_SITE_URL", raising=False)
    head = _head(client, path)
    assert head.count('rel="canonical"') == 1
    assert f'<link rel="canonical" href="https://zakowhat.uz{canonical_path}">' in head
    assert f'<meta property="og:url" content="https://zakowhat.uz{canonical_path}">' in head
    for prop in ("og:type", "og:site_name", "og:locale", "og:title", "og:description", "og:image",
                 "og:image:width", "og:image:height"):
        assert head.count(f'property="{prop}"') == 1, prop
    assert '<meta property="og:image" content="https://zakowhat.uz/static/img/og-zakowhat.png">' in head
    assert '<meta name="twitter:card" content="summary_large_image">' in head
    assert head.count("<title>") == 1 and head.count('name="description"') == 1
    assert 'name="robots"' not in head                                   # public pages are indexable


def test_titles_and_descriptions_differ_between_pages(client):
    titles = {p: re.search(r"<title>([^<]+)</title>", _head(client, p)).group(1) for p in PUBLIC_PAGES}
    descriptions = {p: re.search(r'name="description" content="([^"]+)"', _head(client, p)).group(1) for p in PUBLIC_PAGES}
    assert len(set(titles.values())) >= 4                                # "/" and "/arena" share the approved title
    assert len(set(descriptions.values())) >= 3


@pytest.mark.parametrize("path", ["/quiz/1", "/play/1", "/profile", "/u/someone", "/staging"])
def test_user_specific_and_client_rendered_paths_are_not_indexed(client, path):
    head = _head(client, path)
    assert '<meta name="robots" content="noindex, follow">' in head
    assert '<link rel="canonical" href="https://zakowhat.uz/arena">' in head


def test_share_image_is_a_1200x630_png_served_from_static(client):
    data = Path("static/img/og-zakowhat.png").read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", data[16:24]) == (1200, 630)
    assert len(data) < 300_000                                          # small enough for Telegram previews
    res = client.get("/static/img/og-zakowhat.png")
    assert res.status_code == status.HTTP_200_OK and res.headers["content-type"] == "image/png"


# =========================================================================== page wiring (Phase 7 / 9)

def test_homepage_continue_section_is_hidden_until_the_server_reports_a_game(client):
    html = client.get("/").text
    section = re.search(r'<section id="resume-game"[^>]*>', html).group(0)
    assert 'class="resume-game hidden"' in section
    script = html[html.index("(async function showResumableGame()"):]
    script = script[:script.index("})();")]
    assert "fetch('/api/me/active-game')" in script and "if (!res.ok) return;" in script
    assert "textContent" in script and "innerHTML" not in script        # titles are never parsed as HTML
    assert "box.classList.remove('hidden')" in script


def test_play_url_looks_up_the_server_game_without_starting_one(client):
    html = client.get("/arena").text
    fn = html[html.index("async function adoptServerActiveGame(quizId)"):html.index("async function restoreActiveSession()")]
    assert "/me/active-game?quiz_id=" in fn and "/play/start" not in fn and "method: 'POST'" not in fn
    assert "String(game.quiz_id) !== String(quizId)" in fn                # only the pack in the URL


def test_arena_progress_is_fetched_once_per_render_and_never_breaks_the_list(client):
    html = client.get("/arena").text
    fn = html[html.index("async function decorateArenaProgress(reqId)"):html.index("// Generic labels repeat")]
    assert fn.count("fetch(") == 1 and "if (!state.currentUser) return;" in fn
    assert "if (!res.ok) return;" in fn and "state.loadQuizzesRequestId !== reqId" in fn
    assert "tag.textContent = label.text" in fn
    assert html.count("decorateArenaProgress(currentReqId);") == 2


def test_a_timed_question_with_no_time_left_times_out_instead_of_turning_untimed(client):
    html = client.get("/arena").text
    timer = html[html.index("function startTimer(seconds)"):html.index("function handleTimeOut()")]
    zero = timer.index("seconds <= 0 && state.currentQuestionTimed")
    untimed = timer.index("if (seconds === 0 || state.timerMode === 'no_timer')")
    assert zero < untimed and "setTimeout(handleTimeOut, 0)" in timer[zero:untimed]
    render = html[html.index("function renderQuestionState(stateObj)"):html.index("function renderRankedBadge()")]
    assert "state.currentQuestionTimed = Number(question.time_limit_seconds) > 0;" in render


def test_session_expiry_message_matches_the_real_limit(client):
    from api.play import SESSION_INACTIVITY_LIMIT_SECONDS
    source = Path("api/play.py").read_text(encoding="utf-8")
    assert SESSION_INACTIVITY_LIMIT_SECONDS == 1800
    assert "5 daqiqa" not in source and source.count("30 daqiqa faolsizlik") == 3
