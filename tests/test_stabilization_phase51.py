"""
Phase 5.1 stabilization.

Two kinds of tests live here:

* API contract tests that play real games against the test database and pin the response fields the
  redesigned frontend now depends on (theme titles, per-question points, round scores, durations,
  validation error shape).
* Page-structure invariants for bugs that were confirmed in a real browser playthrough (duplicate element
  ids, the wrong button being re-wired, "[object Object]" error dialogs, unreachable sheets on Escape).
  These cannot execute the page script (there is no JS runtime in the test environment), so they assert
  the invariant on the served HTML / stylesheet rather than re-run the behaviour.
"""
import collections
import re
from pathlib import Path

import pytest
from fastapi import status

from api.auth import create_access_token
from tests.test_svoyak_gameplay import create_svoyak_test_game, svoyak_user  # noqa: F401  (fixture)

CSS = Path("static/css/zakowhat-design-system.css")


@pytest.fixture(scope="module")
def page():
    return Path("templates/index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css():
    return CSS.read_text(encoding="utf-8")


def _between(text, start, end):
    a = text.index(start)
    return text[a:text.index(end, a)]


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.email)}"}


def _play_cell(client, token, headers, r_idx, q_idx, answer):
    sel = client.post(f"/api/play/{token}/select", json={"round_index": r_idx, "question_index": q_idx}, headers=headers)
    assert sel.status_code == status.HTTP_200_OK, sel.text
    ans = client.post(f"/api/play/{token}/answer", json={"answer": answer}, headers=headers)
    assert ans.status_code == status.HTTP_200_OK, ans.text
    body = ans.json()
    if body.get("awaiting_continue"):
        cont = client.post(f"/api/play/{token}/continue", headers=headers)
        assert cont.status_code == status.HTTP_200_OK, cont.text
    return body


# =========================================================================== API contracts (real games)

def test_svoyak_full_game_scores_and_exposes_what_the_results_screen_shows(client, db_session, svoyak_user):  # noqa: F811
    """Plays all 25 cells: one wrong (−20), one pass (0), 23 correct. The results and review payloads must
    carry the theme title, the per-round score and the per-question points the frontend renders."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Stabilization")
    headers = _auth(svoyak_user)
    start = client.post(f"/api/play/start/{quiz.id}", json={"svoyak_mode": "free_choice"}, headers=headers)
    assert start.status_code == status.HTTP_201_CREATED
    token = start.json()["session_token"]

    ladder = [10, 20, 30, 40, 50]
    last = None
    for r_idx in range(5):
        for q_idx, pts in enumerate(ladder):
            if (r_idx, q_idx) == (1, 1):
                last = _play_cell(client, token, headers, r_idx, q_idx, "butunlay xato javob")
                assert last["is_correct"] is False and last["points_awarded"] == -20
            elif (r_idx, q_idx) == (2, 2):
                last = _play_cell(client, token, headers, r_idx, q_idx, "o'tkazish")
                assert last["points_awarded"] == 0
            else:
                last = _play_cell(client, token, headers, r_idx, q_idx, f"Javob {r_idx + 1}_{pts}")
                assert last["is_correct"] is True and last["points_awarded"] == pts

    expected_total = 750 - 20 - 20 - 30          # lost the +20, paid the −20 penalty, skipped the 30
    assert last["total_score"] == expected_total == 680

    client.post(f"/api/play/{token}/continue", headers=headers)          # finalize (idempotent)
    results = client.get(f"/api/play/{token}/results", headers=headers).json()
    assert results["game_mode"] == "svoyak"                              # drives the "Jami ball" headline
    assert results["total_score"] == expected_total
    assert (results["total_correct"], results["total_incorrect"], results["total_unanswered"]) == (23, 1, 1)
    assert [r["round_type"] for r in results["rounds"]] == ["svoyak_theme"] * 5
    assert [r["round_score"] for r in results["rounds"]] == [150, 110, 120, 150, 150]

    review = client.get(f"/api/play/{token}/review", headers=headers).json()
    assert [r["config"]["theme_title"] for r in review["rounds"]] == [f"Mavzu {i}" for i in range(1, 6)]
    awarded = [q["points_awarded"] for r in review["rounds"] for q in r["questions"]]
    assert sum(awarded) == expected_total and awarded.count(-20) == 1 and awarded.count(0) == 1
    assert review["total_questions"] == 25


def test_pack_list_and_details_agree_on_counts_and_duration(client, db_session, svoyak_user):  # noqa: F811
    """Arena cards and Pack Details read the same backend fields; with a seeded pack they must match."""
    quiz = create_svoyak_test_game(client, db_session, "Svoyak Duration Check")
    listed = next(q for q in client.get("/api/quizzes").json() if q["quiz_id"] == quiz.id)
    detail = client.get(f"/api/quizzes/{quiz.id}").json()
    for field in ("total_questions", "total_rounds", "estimated_duration_minutes"):
        assert listed[field] == detail[field], field
    assert (detail["total_questions"], detail["total_rounds"]) == (25, 5)
    assert detail["estimated_duration_minutes"] == 31
    for rnd in detail["rounds"]:                                          # structure only, never content
        assert all(set(q) <= {"sequence", "points"} for q in rnd["questions"])


def test_validation_errors_arrive_as_a_field_list(client):
    """The frontend's apiDetail() maps this shape to an Uzbek sentence (it used to print "[object Object]")."""
    res = client.post("/api/auth/register", json={"email": "short-pw@zakowhat.uz", "password": "abc12",
                                                   "display_name": "Qisqa Parol"})
    assert res.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
    detail = res.json()["detail"]
    assert isinstance(detail, list) and detail[0]["loc"][-1] == "password" and detail[0]["msg"]


def test_application_errors_arrive_as_a_string(client):
    res = client.get("/api/quizzes/99999999")
    assert res.status_code == status.HTTP_404_NOT_FOUND
    assert isinstance(res.json()["detail"], str)


# =========================================================================== page invariants

def test_no_element_id_is_used_twice(client):
    """Regression: `reveal-continue-btn` existed twice, so the round review re-wired the question reveal button."""
    html = client.get("/arena").text
    ids = re.findall(r'\bid="([^"${}]+)"', html)
    duplicates = sorted(i for i, n in collections.Counter(ids).items() if n > 1)
    assert not duplicates, duplicates


def test_round_review_and_question_reveal_use_their_own_buttons(page):
    reveal_panel = _between(page, 'id="answer-reveal-panel"', '<!-- Footer: format and keyboard hint -->')
    assert 'id="reveal-continue-btn"' in reveal_panel and 'onclick="handleRevealContinue()"' in reveal_panel
    round_review = _between(page, '<section id="screen-reveal"', "SCREEN 4: FINAL RESULTS")
    assert 'id="round-review-continue-btn"' in round_review
    assert 'onclick="handleContinueToNextRound()"' in round_review

    for fn, end in (("async function loadRoundReveal()", "let isContinuingRound = false;"),
                    ("async function handleContinueToNextRound()", "let isFinalizingResults = false;"),
                    ("async function handleFinalizeAndShowResults()", "let isLoadingResults = false;")):
        body = _between(page, fn, end)
        assert "getElementById('round-review-continue-btn')" in body, fn
        assert "getElementById('reveal-continue-btn')" not in body, fn
    for fn, end in (("function showAnswerReveal(result, onContinue)", "function hideAnswerReveal()"),
                    ("async function handleRevealContinue()", "async function continueToNextQuestion()")):
        body = _between(page, fn, end)
        assert "getElementById('reveal-continue-btn')" in body, fn
        assert "round-review-continue-btn" not in body, fn


def test_api_errors_are_never_stringified_blindly(page):
    """Every error path goes through apiDetail()/describeError(); none builds a message from a raw object."""
    assert not re.findall(r"\b(?:err|data)\.detail \|\|", page)
    assert "const err = await res.json();" not in page                    # non-JSON error bodies are tolerated
    assert page.count("await res.json().catch(() => ({}))") >= 10
    assert "'Xatolik: ' + err.message" not in page and '"Xatolik: " + err.message' not in page
    helper = _between(page, "function apiDetail(body)", "function describeError(err)")
    assert "typeof detail === 'string'" in helper and "Array.isArray(detail)" in helper
    assert "item.loc[item.loc.length - 1]" in helper
    net = _between(page, "function describeError(err)", "function escapeHtml(text)")
    assert "err instanceof TypeError" in net


def test_failed_submission_restores_controls_and_clock(page):
    submit = _between(page, "async function submitAnswer(answerText)", "function showIntermediateBreak(breakData)")
    failure = submit[submit.rindex("} catch (err) {"):]
    for line in ("state.isSubmitting = false;", "if (input) input.disabled = false;",
                 "if (submitBtn) submitBtn.disabled = false;", "if (passBtnEl) passBtnEl.disabled = false;",
                 "startTimer(state.timeLeft);"):
        assert line in failure, line
    assert "state.timerMode !== 'no_timer' && state.timeLeft > 0" in failure   # never restarts a practice clock


def test_break_countdown_only_continues_from_the_break_screen(page):
    brk = _between(page, "function showIntermediateBreak(breakData)", "let isContinuingFromBreak = false;")
    guard = brk[brk.index("if (state.breakTimeLeft <= 0) {"):]
    assert guard.index("clearInterval(state.breakInterval);") < guard.index("handleIntermediateContinue();")
    assert "breakScreen && !breakScreen.classList.contains('hidden')" in guard
    cont = _between(page, "async function handleIntermediateContinue()", "function roundDisplayName(r, index)")
    assert "2-turga o'tish" not in cont                                   # label no longer changes after first use
    assert "<span>2-turni boshlash</span><span>&rarr;</span>" in cont


def test_timers_are_cleared_on_every_way_out_of_a_game(page):
    for fn, end in (("function goToQuizSelection()", "async function restoreActiveSession()"),
                    ("window.addEventListener('popstate'", "function renderSvoyakBoard(boardData)"),
                    ("function showIntermediateBreak(breakData)", "let isContinuingFromBreak = false;")):
        body = _between(page, fn, end)
        assert "clearInterval(state.timerInterval);" in body and "clearInterval(state.breakInterval);" in body, fn
    restart = _between(page, "async function executeRestartGameplay()", "// Initialize on Load")
    assert "clearInterval(state.timerInterval);" in restart
    assert page.count("setInterval(") == 2                                # question clock + break clock, nothing else


def test_escape_closes_every_sheet_and_shortcuts_ignore_typing(page):
    closers = _between(page, "const SHEET_CLOSERS = [", "];")
    for modal_id in ("modal-gameplay-restart", "modal-zakovat-settings", "modal-leaderboard", "modal-review",
                     "modal-public-profile", "modal-quiz-detail"):
        assert f"'{modal_id}'" in closers and f'id="{modal_id}"' in page, modal_id
    handler = _between(page, "// Global Keyboard Listeners", "(function manageSheetFocus()")
    assert "if (document.querySelector('dialog[open]')) return;" in handler   # custom dialogs keep their own Escape
    assert "/^(INPUT|TEXTAREA|SELECT)$/" in handler and "isAnySheetOpen()" in handler
    typing_guard = handler.index("typingTarget ||")
    assert typing_guard < handler.index("handleTrueFalseSubmit('rost')")       # guard runs before the shortcuts


def test_sheets_are_labelled_focusable_dialogs(page):
    sheets = re.findall(r'<article[^>]*class="pack-detail[^"]*"[^>]*>', page)
    assert len(sheets) == 5
    for tag in sheets:
        assert 'tabindex="-1"' in tag and 'aria-modal="true"' in tag, tag
        assert re.search(r'role="(dialog|alertdialog)"', tag), tag
        label_id = re.search(r'aria-labelledby="([^"]+)"', tag).group(1)
        assert page.count(f'id="{label_id}"') == 1, label_id
    assert "function manageSheetFocus()" in page and "new MutationObserver(" in page


def test_icon_only_controls_and_images_have_accessible_names(page):
    screens = _between(page, '<section id="screen-gameplay"', "SCREEN 6: PLAYER PROFILE")
    for button in re.findall(r'<button[^>]*class="game-bar-btn"[^>]*>', screens):
        assert 'aria-label="' in button, button
    for image in re.findall(r"<img\b[^>]*>", screens):
        assert re.search(r'\balt="', image), image
    assert 'id="answer-text-input"' in screens and 'aria-label="Javobingiz"' in screens
    assert 'id="gameplay-clock-announce" class="zw-visually-hidden" aria-live="polite"' in screens
    assert 'id="gameplay-feedback-banner" class="hidden feedback-banner" role="status"' in screens
    board = _between(page, "function renderSvoyakBoard(boardData)", "async function selectSvoyakQuestion(")
    assert 'aria-label="${escapeHtml(theme.theme_title)}: ${cell.points} ball"' in board
    # verdicts never rely on colour alone: each has a symbol or words
    outcome = _between(page, "function describeAnswerOutcome(result, answerText)", "function showAnswerReveal(")
    for text in ("✓ To‘g‘ri javob", "✕ Noto‘g‘ri javob", "O‘tkazildi", "Vaqt tugadi"):
        assert text in outcome, text


def test_user_supplied_strings_are_escaped_before_reaching_innerhtml(page):
    """Leaderboard, profile and review rows are built with innerHTML; names and avatar paths go through escapeHtml."""
    assert not re.findall(r'<img src="\$\{(?!escapeHtml\()[^}]*avatar_url', page)
    lb = _between(page, "async function openLeaderboardModal(", "function closeLeaderboardModal()")
    for value in ("escapeHtml(item.display_name)", "escapeHtml(item.username)", "escapeHtml(item.avatar_url)"):
        assert value in lb, value
    assert "errorEl.innerText = err.message" in lb                        # server text is never parsed as HTML
    review = _between(page, "function buildReviewQuestionsHtml(rounds)", "async function loadRoundReveal()")
    interpolations = re.findall(r"\$\{([^{}]+)\}", review)
    risky = [i for i in interpolations if re.search(r"\b(q\.text|q\.submitted_answer|q\.explanation|roundTitle|correct_answers)\b", i)
             and "escapeHtml(" not in i and "userAns" not in i]
    assert not risky, risky


def test_svoyak_results_and_review_name_themes_and_points(page):
    helper = _between(page, "function roundDisplayName(r, index)", "function buildReviewQuestionsHtml(rounds)")
    assert "r.round_type === 'svoyak_theme'" in helper and "r.config && r.config.theme_title" in helper
    review = _between(page, "function buildReviewQuestionsHtml(rounds)", "async function loadRoundReveal()")
    assert "const roundTitle = roundDisplayName(r, rIdx);" in review
    assert "Number(q.points_awarded)" in review and "${badgeLabel}${pointsLabel}" in review
    results = _between(page, "async function loadFinalResults()", "async function openReviewModal()")
    assert "roundDisplayName(r, rIdx)" in results and "r.round_score" in results
    assert "'Mavzular bo‘yicha taqsimot' : 'Turlar bo‘yicha taqsimot'" in results
    assert 'id="results-breakdown-title"' in page


def test_leaderboard_date_does_not_depend_on_locale_month_names(page):
    lb = _between(page, "async function openLeaderboardModal(", "function closeLeaderboardModal()")
    assert "toLocaleDateString(" not in lb                               # rendered "M11 12 15:16" in Chromium
    assert "two(d.getDate())}.${two(d.getMonth() + 1)}.${d.getFullYear()}" in lb


def test_missing_question_image_hides_its_frame(page):
    assert re.search(r'<img id="question-media-img"[^>]*onerror="this\.parentElement\.classList\.add\(\'hidden\'\)"', page)


# =========================================================================== stylesheet

def test_phone_touch_targets_are_about_44px(css):
    block = css[css.index("/* ---- Touch targets on phones"):]
    block = block[:block.index("\n}\n") + 3]
    assert block.strip().count("@media (max-width: 640px)") == 1
    for selector, minimum in ((".theme-toggle-btn", 44), ("button.mobile-menu-btn", 44), (".mode-tab-btn", 44),
                              (".category-pill", 40), (".game-bar-btn", 44)):
        rule = re.search(re.escape(selector) + r" \{([^}]*)\}", block).group(1)
        sizes = [int(v) for v in re.findall(r"(?:min-height|height): (\d+)px", rule)]
        assert sizes and max(sizes) >= minimum, (selector, rule)
    assert re.search(r"\.pack-actions \.btn,\s*\.pack-actions \.btn-text-subtle \{[^}]*min-height: 44px;", block)


def test_dim_text_meets_contrast_on_the_dark_surfaces(css):
    """--text-dim carries small labels; on the dark surface it must reach WCAG AA (4.5:1)."""
    def lum(hex_colour):
        channels = [int(hex_colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

    def ratio(a, b):
        hi, lo = sorted((lum(a), lum(b)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    # the light token block starts a line; dark values are the root defaults before it
    split_at = re.search(r'^\[data-theme="light"\] \{', css, re.M).start()
    root = css[:split_at]
    dim_dark = re.search(r"--text-dim: (#[0-9A-Fa-f]{6});", root).group(1)
    surface_token = re.search(r"--bg-surface-elevated: var\((--[\w-]+)\);", root).group(1)
    surface_dark = re.search(re.escape(surface_token) + r": (#[0-9A-Fa-f]{6});", root).group(1)
    assert ratio(dim_dark, surface_dark) >= 4.5, (dim_dark, surface_dark)

    light = css[split_at:]
    dim_light = re.search(r"--text-dim: (#[0-9A-Fa-f]{6});", light).group(1)
    surface_light = re.search(r"--bg-surface-elevated: (#[0-9A-Fa-f]{6});", light).group(1)
    assert ratio(dim_light, surface_light) >= 4.5, (dim_light, surface_light)


def test_legacy_gameplay_styles_are_gone_and_the_homepage_demo_is_intact(css, client):
    for dead in ("intermission-card", "results-codex-card", "results-stat-box", "score-box", "review-modal-container",
                 "codex-modal-container", "arena-leader-bar", "arena-eyebrow-sep"):
        assert not re.search(r"\." + re.escape(dead) + r"(?![\w-])", css), dead
    home = client.get("/").text
    for cls in ("table-outer-rim", "question-stage-card", "clock-display", "submission-bar", "feedback-banner"):
        assert cls in home and re.search(r"\." + re.escape(cls) + r"(?![\w-])", css), cls
    assert css.count("{") == css.count("}")


# =========================================================================== public-site hygiene

@pytest.mark.parametrize("template", ["owner.html", "bank.html", "builder.html"])
def test_private_tools_ask_not_to_be_indexed(template):
    head = Path("templates", template).read_text(encoding="utf-8").split("</head>")[0]
    assert '<meta name="robots" content="noindex, nofollow">' in head


@pytest.mark.parametrize("path", ["/", "/arena", "/daily"])
def test_public_pages_are_indexable_and_describe_themselves_for_sharing(client, path):
    head = client.get(path).text.split("</head>")[0]
    assert 'name="robots"' not in head
    assert head.count("<title>") == 1 and head.count('name="description"') == 1
    for prop in ("og:type", "og:site_name", "og:title", "og:description"):
        assert head.count(f'property="{prop}"') == 1, prop
