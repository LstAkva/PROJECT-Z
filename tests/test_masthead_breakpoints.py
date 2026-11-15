"""The masthead switches between the desktop nav and the hamburger menu with two media queries.

They must meet with no gap: with display scaling the viewport can be fractional (e.g. 767.5px), and
a `max-width: 767px` / `min-width: 768px` pair matched neither, so the desktop links and the
hamburger showed at the same time and overlapped.
"""
import re
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "zakowhat-design-system.css"


def _masthead_queries():
    css = CSS.read_text(encoding="utf-8")
    hide = re.search(r"@media \(max-width: ([\d.]+)px\) \{\s*\.masthead \.nav-desktop,\s*\.masthead \.auth-desktop \{\s*display: none", css)
    show = re.search(r"@media \(min-width: ([\d.]+)px\) \{\s*\.masthead \.nav-desktop,\s*\.masthead \.auth-desktop \{\s*display: flex", css)
    assert hide and show, "masthead responsive rules not found"
    return float(hide.group(1)), float(show.group(1))


def test_masthead_queries_leave_no_fractional_gap():
    max_mobile, min_desktop = _masthead_queries()
    assert min_desktop - max_mobile < 0.05


def test_desktop_header_only_where_it_fits():
    # Logo + three links + both guest buttons need ~920px; below that the buttons overlapped the links.
    _, min_desktop = _masthead_queries()
    assert min_desktop >= 1000
