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
