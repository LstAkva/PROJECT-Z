"""
Regression: after a deploy, browsers reused a stale cached zakowhat-design-system.css
with the new HTML, so the new header rendered unstyled. Every page must link its CSS/JS
with a content-hash version (?v=...) so a changed file gets a new URL.
"""
import hashlib
import re

import pytest

from main import static_url

PAGES = ["/", "/daily", "/arena", "/privacy", "/terms"]
ASSETS = ["css/zakowhat-design-system.css", "js/zakowhat-navbar.js"]


def expected_version(asset):
    with open(f"static/{asset}", "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:12]


@pytest.mark.parametrize("asset", ASSETS)
def test_static_url_uses_content_hash(asset):
    assert static_url(asset) == f"/static/{asset}?v={expected_version(asset)}"
    assert static_url("/" + asset) == static_url(asset)


def test_static_url_missing_file_falls_back_to_plain_path():
    assert static_url("css/does-not-exist.css") == "/static/css/does-not-exist.css"


@pytest.mark.parametrize("page", PAGES)
def test_pages_link_versioned_css_and_js(client, page):
    response = client.get(page)
    assert response.status_code == 200
    html = response.text
    for asset in ASSETS:
        versioned = f"/static/{asset}?v={expected_version(asset)}"
        assert versioned in html, f"{page} does not link {versioned}"
        unversioned = re.findall(rf'(?:href|src)="/static/{re.escape(asset)}"', html)
        assert not unversioned, f"{page} still links an unversioned {asset}"
    assert "static_url(" not in html  # template expression was rendered, not printed


@pytest.mark.parametrize("asset", ASSETS)
def test_versioned_asset_is_served(client, asset):
    response = client.get(static_url(asset))
    assert response.status_code == 200
