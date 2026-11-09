"""
Homepage sample question (permanent feature) — regression guards.

- Temporary "Bek va Lola" test content is gone; the demo uses question bank #957 (source:
  «Svoya igra baza» Telegram channel, post #42) with its source answer and source explanation.
- The demo is self-contained: it never calls the API (no session, stats, leaderboard or pack effects).
- The timer is a one-shot countdown (no infinite restart loop).
"""
import re

from fastapi import status


def _homepage(client):
    res = client.get("/")
    assert res.status_code == status.HTTP_200_OK
    return res.text


def _demo_script(html):
    start = html.index("HOMEPAGE SAMPLE QUESTION")
    return html[start:html.index("</script>", start)]


def test_temporary_bek_va_lola_content_removed(client):
    html = _homepage(client)
    assert "Bek va Lola" not in html
    assert "Ushbu inson kim?" not in html
    # The old fake feedback (any input was "accepted") is gone
    assert "Qabul qilindi" not in html


def test_demo_uses_verified_bank_question_957(client):
    html = _homepage(client)
    assert "Bu sana Albert Eynshteynning tug'ilgan kuni." in html
    assert "Ushbu bayram nimaga bag'ishlangan?" in html
    script = _demo_script(html)
    # Accepted answers come from the source only (no invented variants)
    assert "acceptedAnswers: ['Pi kuni']" in script
    # Explanation is the source's own text, shown only when present
    assert "Albert Eynshteynning tug'ilgan kuni 14-mart . 14 mart , 13:59 -> 3 14 159 -> 3.14159 -> pi." in script
    assert "explWrap.hidden = !(SAMPLE.explanation && SAMPLE.explanation.trim())" in script
    assert "Manba: «Svoya igra baza» Telegram kanali" in html


def test_demo_is_self_contained_no_api_calls(client):
    script = _demo_script(_homepage(client))
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script
    assert "/api/" not in script


def test_demo_timer_is_one_shot(client):
    html = _homepage(client)
    script = _demo_script(html)
    assert 'id="countdownDisplay">01:30<' in html
    assert "totalSeconds = 98" not in html  # old looping reset
    assert "if (timerId !== null || sampleFinished) return;" in script
    assert "if (timeLeft === 0) finishSample(true);" in script
    assert "clearInterval(timerId)" in script


def test_demo_reveal_and_mode_links(client):
    html = _homepage(client)
    assert 'id="sampleReveal" hidden' in html
    for el in ("sampleVerdict", "sampleSubmitted", "sampleCorrect", "sampleExplanationWrap"):
        assert f'id="{el}"' in html
    assert re.search(r'href="/arena\?mode=classic_zakovat"[^>]*id="sampleCtaZakovat"', html)
    assert re.search(r'href="/arena\?mode=svoyak"[^>]*id="sampleCtaSvoyak"', html)


# ---------------------------------------------------------------------------
# Approved homepage copy (Task 4)
# ---------------------------------------------------------------------------

def test_homepage_approved_copy_present(client):
    html = _homepage(client)
    assert "<title>ZakoWhat — Zakovat va Svoya Igra onlayn</title>" in html
    assert "Daqiqa Ichra Haqiqat Jilvasi" in html
    assert "Onlayn Intellektual Maydon" in html
    assert "Zakovat va Svoya Igra to‘plamlarini istalgan vaqtda, o‘zingiz o‘ynang." in html
    assert "Ikki o‘yin — bitta maydon" in html
    assert 'id="game-card-zakovat"' in html and 'id="game-card-svoyak"' in html
    assert "To‘plamlarni o‘ynash uchun bepul ro‘yxatdan o‘ting." in html


def test_homepage_makes_no_unverifiable_or_outdated_claims(client):
    html = _homepage(client)
    assert "Mustaqil" not in html and "mustaqil" not in html
    assert "tahririyat" not in html            # no claim that every question is editorially reviewed
    assert "Kutilayotgan Bosqich" not in html  # no promise of live team play
    assert "Jonli jamoaviy" not in html
    assert "Uch Asosiy Poydevor" not in html
    # Explanations are optional: only promised conditionally
    assert "mavjud bo‘lsa izoh" in html


def test_homepage_game_and_daily_links(client):
    html = _homepage(client)
    assert re.search(r'href="/arena\?mode=classic_zakovat"[^>]*id="gameCtaZakovat"', html)
    assert re.search(r'href="/arena\?mode=svoyak"[^>]*id="gameCtaSvoyak"', html)
    assert re.search(r'href="/daily"[^>]*id="dailyQuestionLink"', html)
    assert 'href="/daily" class="btn btn-secondary"' in html  # hero Kun Savoli button kept
    assert re.search(r'href="/arena"[^>]*id="finalCtaArena"', html)


def test_arena_page_title_matches_homepage_positioning(client):
    """The Arena/gameplay page uses the same approved title and makes no "Mustaqil" claim."""
    res = client.get("/arena")
    assert res.status_code == status.HTTP_200_OK
    head = res.text[:res.text.index("</head>")]
    assert "<title>ZakoWhat — Zakovat va Svoya Igra onlayn</title>" in head
    assert "Mustaqil" not in head and "mustaqil" not in head
