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
    assert re.search(r'\.arena-pack-card\[data-mode="svoyak"\] \{\s*border-top-color: var\(--accent-brass\);', css)
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
