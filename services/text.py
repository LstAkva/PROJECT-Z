"""Cleaning of short free-text fields people see on screen (names, team names, answers)."""
import re
import unicodedata

# Cc = control characters (NUL, ...), Cf = invisible formatting (zero-width space/joiner, right-to-left override, ...),
# Co / Cs / Cn = private-use, surrogate and unassigned code points.
_INVISIBLE_CATEGORIES = {"Cc", "Cf", "Co", "Cs", "Cn"}
_SPACES = re.compile(r"\s+")


def clean_visible_text(value, max_length: int = 0) -> str:
    """Removes invisible and control characters, collapses whitespace (including line breaks) to single spaces
    and trims. A value made only of invisible characters becomes "" so callers can reject it."""
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if unicodedata.category(ch) not in _INVISIBLE_CATEGORIES or ch in "\t\n\r")
    text = _SPACES.sub(" ", kept).strip()
    if max_length:
        text = text[:max_length].rstrip()
    return text
