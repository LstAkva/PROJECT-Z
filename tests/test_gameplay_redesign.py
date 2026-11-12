"""
Gameplay redesign ("The Round"): Classic Zakovat, Svoya Igra, the break, the round review, final results,
the answer review and the restart confirmation are styled by theme tokens (light and dark), while every
element id, handler and piece of game logic the controller relies on stays in place.
"""
import re
from pathlib import Path

import pytest

TEMPLATE = Path("templates/index.html")
CSS = Path("static/css/zakowhat-design-system.css")

# Tailwind utility colours / hard-coded dark values that made gameplay dark-only.
DARK_ONLY = re.compile(
    r"zinc-|amber-|emerald-|rose-|text-white|bg-black|white/\[|white/\d|#7A1627|#1e1b18|text-brass-|border-brass-"
)


@pytest.fixture(scope="module")
def html():
    return TEMPLATE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css():
    return CSS.read_text(encoding="utf-8")


def _between(text, start, end):
    a = text.index(start)
    return text[a:text.index(end, a)]


def _screens(html):
    return _between(html, '<section id="screen-gameplay"', "SCREEN 6: PLAYER PROFILE")


def _fn(html, name, end):
    return _between(html, name, end)


# --------------------------------------------------------------------------- theme support

def test_gameplay_screens_have_no_dark_only_styling(html):
    """Regression: the table, break, round review and results were dark-only (hard-coded utility colours)."""
    screens = _screens(html)
    assert not DARK_ONLY.findall(screens)
    assert "style=" not in screens.replace('style="width: 8%;"', "")   # only the progress width is inline
    for modal_start, modal_end in (('id="modal-review"', "MODAL 3: LEADERBOARD"),
                                   ('id="modal-gameplay-restart"', "MODAL 5: ZAKOVAT GAMEPLAY SETTINGS")):
        modal = _between(html, modal_start, modal_end)
        assert not DARK_ONLY.findall(modal), modal_start
        assert 'class="pack-detail ' in modal                           # same sheet shell as Pack Details


@pytest.mark.parametrize("fn,end", [
    ("function renderSvoyakBoard(boardData)", "async function selectSvoyakQuestion("),
    ("function buildReviewQuestionsHtml(rounds)", "async function loadRoundReveal()"),
    ("async function loadRoundReveal()", "async function handleContinueToNextRound()"),
    ("async function loadFinalResults()", "async function openReviewModal()"),
    ("function showAnswerReveal(result, onContinue)", "function hideAnswerReveal()"),
    ("function renderRankedBadge()", "function startTimer(seconds)"),
])
def test_gameplay_renderers_emit_no_dark_only_classes(html, fn, end):
    assert not DARK_ONLY.findall(_fn(html, fn, end)), fn


def test_gameplay_stylesheet_uses_tokens_only(css):
    """Section 16 contains no literal colours, so both themes are driven by the token layer."""
    section = css[css.index("16. GAMEPLAY — THE ROUND"):]
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", section)
    assert "rgba(" not in section and "rgb(" not in section
    for token in ("--bg-surface-elevated", "--text-main", "--text-muted", "--border-subtle", "--burgundy",
                  "--link-color", "--text-brass", "--mode-rule-svoyak", "--state-correct", "--state-incorrect"):
        assert f"var({token})" in section, token


def test_verdict_tokens_exist_for_both_themes(css):
    for token in ("--state-correct", "--state-incorrect"):
        values = re.findall(rf"^\s*{token}: (#[0-9A-Fa-f]{{6}});", css, re.M)
        assert len(values) == 2 and values[0] != values[1], (token, values)   # dark default + light override
        # the light value is defined after the light theme block starts
        second = [m.start() for m in re.finditer(rf"^\s*{token}: ", css, re.M)][1]
        assert css.rfind('[data-theme="light"]', 0, second) != -1, token


def test_homepage_demo_table_is_left_alone(css, client):
    """The old table classes still style the homepage demo; gameplay no longer uses them."""
    home = client.get("/").text
    for cls in ("table-outer-rim", "question-stage-card", "clock-display", "submission-bar"):
        assert cls in home and re.search(rf"\.{cls} \{{", css), cls
    assert "table-outer-rim" not in _screens(TEMPLATE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- preserved structure

GAMEPLAY_IDS = [
    "game-table", "gameplay-quiz-title", "gameplay-round-badge", "gameplay-question-counter",
    "gameplay-ranked-badge", "gameplay-clock", "gameplay-timer-value", "gameplay-timer-unit",
    "gameplay-progress-bar", "svoyak-board-container", "svoyak-board-score-pill", "svoyak-grid-rows",
    "gameplay-question-stage", "question-media-container", "question-media-img", "gameplay-card-tag",
    "gameplay-card-subtag", "gameplay-question-text", "answer-form-wrapper", "text-answer-form",
    "answer-text-input", "answer-pass-btn", "answer-submit-btn", "svoyak-penalty-note", "true-false-actions",
    "gameplay-feedback-banner", "answer-reveal-panel", "reveal-submitted-answer", "reveal-correct-answer",
    "reveal-explanation-wrap", "reveal-explanation", "gameplay-format-label",
    "intermediate-break-title", "intermediate-break-message", "intermediate-correct-count",
    "intermediate-incorrect-count", "intermediate-unanswered-count", "intermediate-timer-display",
    "intermediate-continue-btn",
    "reveal-heading-badge", "reveal-heading-title", "reveal-round-score-text", "reveal-hidden-rule-card",
    "reveal-hidden-rule-content", "reveal-questions-list", "reveal-continue-label",
    "results-correct-count", "results-incorrect-count", "results-unanswered-count", "results-active-time",
    "results-status-badge", "results-total-questions", "results-rounds-breakdown", "results-anon-banner",
    "results-review-btn-label", "results-restart-btn", "results-exit-btn-label",
]


def test_every_gameplay_element_the_controller_uses_is_still_there(html):
    screens = _screens(html)
    for element_id in GAMEPLAY_IDS:
        assert screens.count(f'id="{element_id}"') == 1, element_id
    # Every id the gameplay controller looks up exists somewhere in the page (no renamed-away targets).
    controller = _between(html, "function renderQuestionState(stateObj)", "function closeLeaderboardModal()")
    controller += _between(html, "function renderSvoyakBoard(boardData)", "async function selectSvoyakQuestion(")
    looked_up = set(re.findall(r"getElementById\('([^']+)'\)", controller))
    known_absent = set()
    missing = sorted(i for i in looked_up - known_absent if f'id="{i}"' not in html)
    assert not missing, missing
    assert "querySelector('.question-stage-card')" not in html and "querySelector('.clock-display')" not in html


def test_gameplay_actions_are_all_still_wired(html):
    screens = _screens(html)
    for handler in ('onclick="confirmExitGameplay()"', 'onclick="confirmRestartGameplay()"',
                    'onsubmit="handleTextAnswerSubmit(event)"', 'onclick="handleSvoyakPass()"',
                    "onclick=\"handleTrueFalseSubmit('rost')\"", 'onclick="handleRevealContinue()"',
                    'onclick="handleIntermediateContinue()"', 'onclick="handleContinueToNextRound()"',
                    'onclick="openReviewModal()"', 'onclick="openLeaderboardModal(state.currentQuizId)"',
                    'onclick="goToQuizSelection()"', "onclick=\"openAuthModal('register')\""):
        assert handler in screens, handler
    assert screens.count('onclick="confirmRestartGameplay()"') == 2      # header bar + results
    restart = _between(html, 'id="modal-gameplay-restart"', "MODAL 5: ZAKOVAT GAMEPLAY SETTINGS")
    assert 'onclick="closeRestartModal()"' in restart and 'onclick="executeRestartGameplay()"' in restart
    assert 'id="btn-confirm-restart-action"' in restart and 'id="restart-practice-note"' in restart
    review = _between(html, 'id="modal-review"', "MODAL 3: LEADERBOARD")
    for element_id in ("review-correct-count", "review-incorrect-count", "review-unanswered-count",
                       "review-active-time", "review-hidden-rule-box", "review-hidden-rule-text",
                       "review-questions-list", "review-total-score"):
        assert review.count(f'id="{element_id}"') == 1, element_id


def test_game_logic_is_unchanged(html):
    """The redesign may restyle, never re-rule: timing, submission, passing, routing and scoring text."""
    for snippet in (
        "state.timerMode = (question.time_limit_seconds === 0) ? 'no_timer' : 'with_timer';",
        "const timeLimit = (question.time_limit_seconds === 0) ? 0 : rawLimit;",
        "if (seconds === 0 || state.timerMode === 'no_timer') {",
        "if (state.isSubmitting || state.timerMode === 'no_timer') return;",
        'submitAnswer(""); // Submit empty answer when time runs out',
        "if (!answer && state.gameMode === 'svoyak') {",
        'submitAnswer("o\'tkazish");',
        "'Idempotency-Key': clientSubId,",
        "if (result.awaiting_continue) {",
        "} else if (result.intermediate_break) {",
        "} else if (result.board_available && result.board) {",
        "if (Object.prototype.hasOwnProperty.call(result, 'correct_answer')) {",
        "penaltyNote.innerText = `To‘g‘ri javob: +${pts} ball · Noto‘g‘ri javob: −${pts} ball · O‘tkazish: 0 ball`;",
        "body: JSON.stringify({ round_index: roundIndex, question_index: questionIndex })",
        "if (awarded !== null ? awarded > 0 : cell.is_correct === true) {",
        "resultsRestartBtn.classList.toggle('hidden', !state.currentUser);",
        "let breakSec = (breakData && breakData.break_duration_seconds) ? breakData.break_duration_seconds : 180;",
    ):
        assert html.count(snippet) == 1, snippet


# --------------------------------------------------------------------------- format-specific behaviour

def test_table_tells_the_stylesheet_format_and_view(html, css):
    question = _fn(html, "function renderQuestionState(stateObj)", "function renderRankedBadge()")
    assert "gameTable.dataset.mode = isSvoyak ? 'svoyak' : 'classic_zakovat';" in question
    assert "gameTable.dataset.view = 'question';" in question
    board = _fn(html, "function renderSvoyakBoard(boardData)", "async function selectSvoyakQuestion(")
    assert "gameTable.dataset.mode = 'svoyak';" in board and "gameTable.dataset.view = 'board';" in board
    # Classic: burgundy rule and progress; Svoya Igra: brass
    assert re.search(r"\.game-table \{[^}]*border-top: 3px solid var\(--burgundy\);", css)
    assert re.search(r'\.game-table\[data-mode="svoyak"\] \{[^}]*border-top-color: var\(--mode-rule-svoyak\);', css)
    assert re.search(r"\.game-progress-fill \{[^}]*background: var\(--burgundy\);", css)
    # Classic hides the card labels that only repeat the header; Svoya Igra keeps its theme label
    assert re.search(r'\.game-table:not\(\[data-mode="svoyak"\]\) \.game-stage-tags \{\s*display: none;', css)


def test_question_is_the_typographic_centre(css):
    question = re.search(r"\.game-question \{[^}]*\}", css).group(0)
    assert "var(--font-serif)" in question and "overflow-wrap: anywhere" in question   # long Uzbek text wraps
    media = re.search(r"\.game-media-img \{[^}]*\}", css).group(0)
    assert "max-width: 100%" in media and "object-fit: contain" in media               # images never overflow
    clock = re.search(r"\.game-clock \{[^}]*\}", css).group(0)
    assert "var(--link-color)" in clock and "tabular-nums" in clock
    assert re.search(r"\.game-clock\.is-low \{\s*color: var\(--state-incorrect\);", css)


def test_clock_states_are_visual_only(html):
    timer = _fn(html, "function startTimer(seconds)", "function handleTimeOut()")
    assert "clockEl.classList.toggle('is-untimed', untimed);" in timer
    assert "clockEl.classList.toggle('is-low', !untimed && state.timeLeft <= 10);" in timer
    assert "if (timerEl) timerEl.innerText = '∞';" in timer          # practice mode still shows no countdown
    assert timer.count("handleTimeOut();") == 1 and timer.count("setInterval(") == 1


def test_svoyak_board_cells_distinguish_available_and_played(html, css):
    board = _fn(html, "function renderSvoyakBoard(boardData)", "async function selectSvoyakQuestion(")
    assert 'onclick="selectSvoyakQuestion(${rIdx}, ${qIdx})"' in board
    assert 'class="game-cell"' in board                                # available: a button
    for state_class in ("is-won", "is-lost", "is-zero"):
        assert f'<div class="game-cell is-played {state_class}"' in board   # played: not a button
    assert re.search(r"\.game-cell \{[^}]*color: var\(--text-brass\);", css)             # values in brass
    hover = re.search(r"button\.game-cell:hover,\s*button\.game-cell:focus-visible \{[^}]*\}", css).group(0)
    assert "background: var(--burgundy)" in hover                                        # active in burgundy
    played = re.search(r"\.game-cell\.is-played \{[^}]*\}", css).group(0)
    assert "border-style: dashed" in played and "cursor: default" in played


def test_svoyak_score_stays_visible(html, css):
    screens = _screens(html)
    assert screens.count('id="gameplay-score-value"') == 1
    board = _fn(html, "function renderSvoyakBoard(boardData)", "async function selectSvoyakQuestion(")
    assert "scorePill.innerText = `Ball: ${boardData.total_score ?? 0}`;" in board
    assert "runningScoreEl.innerText = (boardData.total_score ?? 0);" in board
    submit = _fn(html, "async function submitAnswer(answerText)", "function showIntermediateBreak(breakData)")
    assert "runningScoreEl.innerText = (state.totalScore ?? 0);" in submit
    assert re.search(r'\.game-table\[data-mode="svoyak"\]\[data-view="question"\] \.game-score-chip \{\s*display: inline;', css)
    assert re.search(r"\.game-score-chip \{\s*display: none;", css)                      # never shown in Zakovat


def test_svoyak_final_score_is_shown_in_results_and_review(html):
    """Regression: the controller wrote the points total into elements that did not exist in the page."""
    screens = _screens(html)
    assert 'id="results-score-wrap" class="hidden game-total"' in screens
    assert screens.count('id="results-total-score"') == 1
    results = _fn(html, "async function loadFinalResults()", "async function openReviewModal()")
    assert "scoreWrap.classList.toggle('hidden', !isSvoyakResult);" in results
    review = _fn(html, "async function openReviewModal()", "function closeReviewModal()")
    assert "reviewScoreWrap.classList.toggle('hidden', !isSvoyakReview);" in review
    assert "(24 savol)</h3>" not in html                    # review title no longer claims 24 questions


def test_review_entries_keep_their_content_and_states(html):
    review = _fn(html, "function buildReviewQuestionsHtml(rounds)", "async function loadRoundReveal()")
    assert "class=\"review-question-item ${stateClass}\"" in review
    assert "q.is_correct ? 'correct' : (isUnans ? 'unanswered' : 'incorrect')" in review
    for piece in ("escapeHtml(q.text)", "escapeHtml(q.submitted_answer || \"Javob berilmadi\")",
                  "escapeHtml((q.correct_answers || []).join(' / '))", "escapeHtml(q.explanation)",
                  "Savol #${globalIndex}", "${qCount} ta savol"):
        assert piece in review, piece


def test_feedback_and_reveal_cover_every_outcome(html, css):
    outcome = _fn(html, "function describeAnswerOutcome(result, answerText)", "function showAnswerReveal(")
    for kind in ("kind: 'correct'", "kind: 'incorrect'", "kind: 'neutral'"):
        assert kind in outcome
    for kind, token in (("correct", "--state-correct"), ("incorrect", "--state-incorrect"), ("neutral", "--border-medium")):
        assert re.search(rf"\.game-table \.feedback-banner\.{kind} \{{[^}}]*var\({token}\)", css), kind
    reveal = _fn(html, "function showAnswerReveal(result, onContinue)", "function hideAnswerReveal()")
    assert "submittedEl.classList.toggle('is-empty', !submitted);" in reveal
    assert "state.lastSubmitWasPass ? 'O‘tkazildi' : 'Javob berilmadi'" in reveal
    assert re.search(r"\.game-reveal-row dd\.is-empty \{", css)


def test_hidden_elements_stay_hidden(css):
    """Toggled elements rely on the global .hidden rule beating the component display values."""
    assert re.search(r"\.hidden \{\s*display: none !important;", css)
