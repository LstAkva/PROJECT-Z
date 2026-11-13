"""
Brand identity "The Round": the mark is one centralized asset (inline partial + static SVG variants
with identical geometry), used in the shared header/footer, and the site defaults to the light theme.
"""
import re
from pathlib import Path

import pytest

PAGES = ["/", "/arena", "/daily", "/privacy", "/terms"]
VARIANTS = {
    "zakowhat-mark.svg": "#782F3B",
    "zakowhat-mark-white.svg": "#FFFFFF",
    "zakowhat-mark-black.svg": "#000000",
}
GEOMETRY = re.compile(r'\sd="[^"]*"|points="[^"]*"|transform="translate\(26[^"]*"')


def _geometry(text):
    return GEOMETRY.findall(text)


def test_mark_variants_share_one_geometry():
    partial = _geometry(Path("templates/components/brand_mark.html").read_text(encoding="utf-8"))
    assert len(partial) == 3
    for name, colour in VARIANTS.items():
        svg = Path("static/svg", name).read_text(encoding="utf-8")
        assert _geometry(svg) == partial, f"{name} geometry differs from the inline mark"
        assert colour in svg
    icon = Path("static/svg/zakowhat-app-icon.svg").read_text(encoding="utf-8")
    assert _geometry(icon) == partial
    assert "#782F3B" in icon and "#F1EBDD" in icon


@pytest.mark.parametrize("asset", list(VARIANTS) + ["zakowhat-app-icon.svg", "zakowhat-insignia.svg", "zakowhat-favicon.svg"])
def test_brand_assets_are_served(client, asset):
    res = client.get(f"/static/svg/{asset}")  # old branding files are kept, not deleted
    assert res.status_code == 200
    assert "svg" in res.headers["content-type"]


@pytest.mark.parametrize("page", PAGES)
def test_pages_use_the_new_lockup_icon_and_light_default(client, page):
    html = client.get(page).text
    header = html.split("</header>")[0]
    assert 'class="brand-mark"' in header and 'id="zw-spark-header"' in header
    assert '<span class="brand-title">ZakoWhat</span>' in header
    assert "zakowhat-insignia.svg" not in header
    assert 'id="zw-spark-footer"' in html            # unique mask id per instance
    assert "Intelligence meets culture." in html
    assert 'rel="icon" type="image/svg+xml" href="/static/svg/zakowhat-app-icon.svg"' in html
    assert re.search(r'<html lang="uz" data-theme="light"', html)
    assert "|| 'dark';" not in html                   # theme fallback is light everywhere


def test_brand_tokens_exist():
    css = Path("static/css/zakowhat-design-system.css").read_text(encoding="utf-8")
    for token, value in {"--ink": "#171417", "--charcoal": "#30252B", "--burgundy": "#782F3B",
                         "--brass": "#D8B779", "--ivory": "#F1EBDD"}.items():
        assert re.search(rf"{token}:\s*{value};", css), token
    assert "--brand-mark-color: var(--burgundy);" in css and "--brand-mark-color: var(--ivory);" in css


def test_arena_uses_design_system_classes_not_dark_utility_boxes(client):
    """Arena redesign: heading, filters, loading and empty states are styled by the design system.
    Regression for the dark grey loading box that sat on the ivory page."""
    html = client.get("/arena").text
    arena = html.split('id="screen-gameplay"')[0]
    for cls in ("arena-eyebrow", "arena-catalog-title", "mode-switcher-container", "category-pills-label",
                "arena-loading-spinner", "arena-skeleton-card", "arena-sections",
                "arena-section-title", "arena-card-grid"):
        assert cls in html, cls
    loading = re.search(r'<div id="arena-loading"[^>]*>', arena).group(0)
    assert 'class="arena-loading"' in loading and "bg-black" not in loading and "zinc" not in loading
    assert 'id="zw-spark-arena-empty"' in arena              # empty state shows the brand mark
    assert "zakowhat-insignia.svg" not in arena
    assert 'data-mode="classic_zakovat"' in html and 'data-mode="svoyak"' in html


def test_arena_empty_result_hides_previous_cards(client):
    """Regression: a search with no matches left the previous results visible under the empty state."""
    html = client.get("/arena").text
    public_list = html[html.index("async function loadQuizzes()"):]
    start = public_list.index("if (!quizzes || quizzes.length === 0) {")
    block = public_list[start:start + 400]
    assert "contentEl.classList.add('hidden')" in block


def test_arena_css_is_themed_by_tokens():
    css = Path("static/css/zakowhat-design-system.css").read_text(encoding="utf-8")
    title = re.search(r"\.arena-catalog-title \{[^}]*\}", css).group(0)
    assert "var(--font-serif)" in title and "uppercase" not in title
    assert re.search(r"\.pack-title \{[^}]*var\(--font-serif\)", css)
    assert re.search(r'\.arena-pack-card\[data-mode="classic_zakovat"\] \{\s*border-top-color: var\(--burgundy\);', css)
    # Svoya Igra rule has its own token: bright brass on dark, a deeper gold on ivory (brass-700 looked brown).
    assert re.search(r'\.arena-pack-card\[data-mode="svoyak"\] \{\s*border-top-color: var\(--mode-rule-svoyak\);', css)
    assert "--mode-rule-svoyak: var(--brass);" in css and "--mode-rule-svoyak: #B48A32;" in css
    assert re.search(r"\.arena-card-grid \{[^}]*grid-auto-rows: 1fr;", css)   # even catalogue rows
    # Hero glow fades to fully transparent on every edge of the clipping box (regression: a single
    # elliptical mask was still ~80% opaque at the right edge and left a visible rectangle).
    canvas = re.search(r"\.hero-girih-canvas \{[^}]*\}", css).group(0)
    assert "mask-composite: intersect" in canvas and "-webkit-mask-composite: source-in" in canvas
    for axis, direction in (("x", "to right"), ("y", "to bottom")):
        fade = re.search(rf"--hero-fade-{axis}: linear-gradient\({direction}, transparent 0%,.*transparent 100%\);", canvas)
        assert fade, axis
    assert "mask-image: var(--hero-fade-x), var(--hero-fade-y);" in canvas
    assert "mask-image: radial-gradient" not in canvas
    light_body = re.search(r'\[data-theme="light"\] body \{[^}]*\}', css).group(0)
    assert "radial-gradient" in light_body
    for selector in ('[data-theme="light"] .arena-pack-card {', '[data-theme="light"] .arena-toolbar {'):
        assert selector not in css  # covered by tokens now, no per-theme duplicates


def test_arena_heading_does_not_repeat_itself(client):
    """Eyebrow names the place, the title names the subject; "to'plamlar" is not said twice above the fold."""
    html = client.get("/arena").text
    eyebrow = re.search(r'<div class="arena-eyebrow">(.*?)</div>', html, re.S).group(1)
    assert "ZAKOWHAT ARENA" in eyebrow and "SARALANGAN" not in eyebrow
    assert "Zakovat Savollar To‘plamlari" in html


def test_arena_cards_keep_the_label_slot_without_a_fake_label(client):
    html = client.get("/arena").text
    fn = html[html.index("function packCategoryLabelHtml"):][:500]
    assert '<div class="pack-card-header" aria-hidden="true"></div>' in fn   # empty slot, no invented text
    css = Path("static/css/zakowhat-design-system.css").read_text(encoding="utf-8")
    assert re.search(r"\.pack-card-header \{[^}]*min-height:", css)


def test_pack_details_use_the_round_identity(client):
    """Pack Details (opened by "Batafsil", also at /quiz/{id}) is styled by the design system,
    not by the old boxed modal, and keeps every element id the controller fills in."""
    html = client.get("/quiz/1").text
    modal = html[html.index('id="modal-quiz-detail"'):html.index("MODAL: GAMEPLAY RESTART CONFIRMATION")]
    for cls in ("pack-detail-overlay", "pack-detail-crumbs", "pack-detail-title", "pack-detail-facts",
                "pack-detail-rounds", "pack-detail-table", "pack-detail-empty", "pack-detail-footer",
                "pack-detail-loading"):
        assert cls in modal, cls
    for old in ("codex-modal", "bg-black", "zinc", "text-white", "font-display"):
        assert old not in modal, old
    for element_id in ("detail-title", "detail-description", "detail-category-badge", "detail-mode-badge",
                       "detail-version-badge", "detail-metric-rounds", "detail-metric-questions",
                       "detail-metric-duration", "detail-metric-format", "detail-rounds-list",
                       "detail-top-player-chip", "detail-lb-preview-table-container", "detail-lb-preview-rows",
                       "detail-lb-preview-empty", "detail-btn-view-full-leaderboard", "detail-btn-play-now"):
        assert modal.count(f'id="{element_id}"') == 1, element_id
    assert modal.count("closeQuizDetailModal()") >= 3          # backdrop, breadcrumb/close, Orqaga
    css = Path("static/css/zakowhat-design-system.css").read_text(encoding="utf-8")
    assert re.search(r'\.pack-detail\[data-mode="classic_zakovat"\] \{\s*border-top-color: var\(--burgundy\);', css)
    assert re.search(r'\.pack-detail\[data-mode="svoyak"\] \{\s*border-top-color: var\(--mode-rule-svoyak\);', css)


def test_pack_details_controller_shows_format_and_never_question_text(client):
    html = client.get("/arena").text
    fn = html[html.index("async function openQuizDetail("):html.index("function closeQuizDetailModal()")]
    assert "sheet.dataset.state = 'loading'" in fn and "sheet.dataset.state = 'ready'" in fn
    assert "'Klassik Zakovat'" in fn and "'Svoya Igra'" in fn and "'Zakovat 2×12'" in fn
    assert "isSvoyakPack ? 'Mavzular' : 'Turlar'" in fn           # Svoya Igra counts themes, not rounds
    for leaked in ("question_text", "q.text", "correct_answer", ".answer"):
        assert leaked not in fn, leaked


def test_arena_cards_and_details_share_the_backend_duration(client):
    """Regression: cards hard-coded "24 savol / ~24 daq" while Pack Details showed the API estimate (~30 daq).
    Both now read estimated_duration_minutes, total_questions and total_rounds from the API."""
    html = client.get("/arena").text
    card = html[html.index("function buildCardHtml(q)"):][:1800]
    assert "q.estimated_duration_minutes" in card and "q.total_questions" in card and "q.total_rounds" in card
    assert "~24 daq" not in card and "~25 daq" not in card
    detail = html[html.index("async function openQuizDetail("):html.index("function closeQuizDetailModal()")]
    assert "`~${data.estimated_duration_minutes} daq`" in detail


def test_arena_description_names_both_formats(client):
    html = client.get("/arena").text
    desc = re.search(r'<p class="arena-catalog-desc">(.*?)</p>', html, re.S).group(1)
    assert "Klassik Zakovat" in desc and "Svoya Igra" in desc
    assert "2×12 formatidagi" not in desc


def test_game_settings_sheet_keeps_every_setting(client):
    html = client.get("/arena").text
    modal = html[html.index('id="modal-zakovat-settings"'):html.index("JAVASCRIPT GAMEPLAY ENGINE")]
    assert 'class="pack-detail game-setup"' in modal
    for old in ("zinc", "text-white", "bg-black", "amber", "font-display"):
        assert old not in modal, old
    for name, values in {"timer_mode": ("with_timer", "no_timer"), "svoyak_mode": ("free_choice", "classic")}.items():
        for value in values:
            assert re.search(rf'<input type="radio" name="{name}" value="{value}"', modal), (name, value)
    assert modal.count(" checked ") == 2                                  # defaults: with_timer, free_choice
    assert re.search(r'value="with_timer" checked', modal) and re.search(r'value="free_choice" checked', modal)
    for element_id in ("settings-modal-title", "settings-modal-subtitle", "settings-zakovat-options",
                       "settings-svoyak-options", "opt-label-with-timer", "opt-label-no-timer",
                       "opt-label-svoyak-free", "opt-label-svoyak-classic", "no-timer-practice-note",
                       "svoyak-scoring-rules", "start-game-confirm-btn"):
        assert modal.count(f'id="{element_id}"') == 1, element_id
    assert 'onclick="confirmStartGameWithSettings()"' in modal
    for text in ("90–180s", "liderlar jadvaliga kirmaydi", "+10…+50", "−10…−50", "O‘tkazish yoki vaqt tugashi: 0 ball"):
        assert text in modal, text                                        # rules text is unchanged
    controller = html[html.index("function updateTimerModeSelection()"):html.index("async function confirmStartGameWithSettings()")]
    assert "state.timerMode = val;" in controller and "state.svoyakMode = val;" in controller
    assert controller.count("classList.toggle('is-selected'") == 4


def test_leaderboard_sheet_matches_the_round(client):
    html = client.get("/arena").text
    modal = html[html.index('id="modal-leaderboard"'):html.index("MODAL 4: QUIZ DETAIL")]
    assert 'class="pack-detail leaderboard-sheet"' in modal
    for old in ("codex-modal", "zinc", "text-white", "bg-black", "animate-spin", "font-display"):
        assert old not in modal, old
    for element_id in ("leaderboard-quiz-title", "leaderboard-version-badge", "leaderboard-subtitle",
                       "leaderboard-loading", "leaderboard-empty", "leaderboard-error",
                       "leaderboard-table-container", "leaderboard-rows"):
        assert modal.count(f'id="{element_id}"') == 1, element_id
    fn = html[html.index("async function openLeaderboardModal("):html.index("function closeLeaderboardModal()")]
    # Public players open their profile by username; private players are not linked; no internal ids
    assert "openPublicProfileModal('${profileRef}')" in fn and "item.user_id" not in fn
    assert "const isMe = item.is_me === true;" in fn and "is-me" in fn       # server marks the viewer's own row
    assert "empty.innerHTML" not in fn                                     # an error no longer wipes the empty state
    assert "errorEl.innerText = err.message" in fn
    assert "escapeHtml(item.display_name)" in fn and "escapeHtml(item.username)" in fn
