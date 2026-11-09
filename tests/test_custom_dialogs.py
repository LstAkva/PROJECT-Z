"""
Regression: native alert()/confirm()/prompt() were replaced by the ZakoWhat dialog component
(static/js/zakowhat-dialog.js). Native dialogs must not come back, every page that uses the
component must load it, and destructive confirmations must still wait for the answer.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = ["index.html", "owner.html", "builder.html", "bank.html"]
NATIVE_CALL = re.compile(r"(?<![\w$.])(alert|confirm|prompt)\s*\(")


def _inline_scripts(name):
    html = Path("templates", name).read_text(encoding="utf-8")
    return [m.group(1) for m in re.finditer(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.DOTALL)]


@pytest.mark.parametrize("name", TEMPLATES)
def test_no_native_browser_dialogs_left(name):
    for script in _inline_scripts(name):
        found = [m.group(0) for m in NATIVE_CALL.finditer(script)]
        assert not found, f"{name} still calls native dialogs: {found}"


@pytest.mark.parametrize("name", TEMPLATES)
def test_pages_using_dialogs_load_the_component(name):
    html = Path("templates", name).read_text(encoding="utf-8")
    assert "zwAlert(" in html or "zwConfirm(" in html or "zwPrompt(" in html
    assert "static_url('js/zakowhat-dialog.js')" in html


@pytest.mark.parametrize("name", TEMPLATES)
def test_confirmations_and_prompts_are_awaited(name):
    """A non-awaited zwConfirm/zwPrompt returns a Promise (always truthy): the guard would be bypassed."""
    for script in _inline_scripts(name):
        for m in re.finditer(r"(?<![\w$.])(zwConfirm|zwPrompt)\s*\(", script):
            before = script[max(0, m.start() - 12):m.start()]
            assert "await" in before, f"{name}: {m.group(1)} not awaited near: {script[m.start()-60:m.start()+40]!r}"


def test_destructive_confirmations_use_danger_variant():
    joined = "".join(s for n in TEMPLATES for s in _inline_scripts(n))
    for phrase in ("o'chirmoqchimisiz", "o'chirishni tasdiqlaysizmi", "nashrdan olib"):
        for m in re.finditer(re.escape(phrase), joined):
            call = joined[m.start():m.start() + 400]
            assert 'variant: "danger"' in call, f"destructive confirm without danger variant: {call[:120]!r}"


def test_dialog_component_is_served(client):
    from main import static_url
    res = client.get(static_url("js/zakowhat-dialog.js"))
    assert res.status_code == 200
    body = res.text
    for api in ("window.zwAlert", "window.zwConfirm", "window.zwPrompt", "showModal", "textContent"):
        assert api in body
    assert "innerHTML" not in body  # messages are rendered as text, never HTML


def test_dialog_script_rendered_on_arena(client):
    html = client.get("/arena").text
    assert re.search(r'src="/static/js/zakowhat-dialog\.js\?v=[0-9a-f]{12}"', html)
