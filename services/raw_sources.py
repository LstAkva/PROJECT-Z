"""
Location of raw Telegram exports (local-only source files, never committed).

Raw exports live in a git-ignored directory:
    <project>/data/raw/telegram/        (default)
or wherever the ZAKOWHAT_RAW_TELEGRAM_DIR environment variable points.

`raw_export_path("1.json")` resolves a file name to an existing file, looking in the raw
directory first and then in the project root (where the exports used to be kept).
An explicit path that exists is returned unchanged.
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RAW_TELEGRAM_DIR = os.path.join("data", "raw", "telegram")


def raw_telegram_dir() -> str:
    configured = os.getenv("ZAKOWHAT_RAW_TELEGRAM_DIR", "").strip()
    path = configured or DEFAULT_RAW_TELEGRAM_DIR
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def raw_export_path(name: str) -> str:
    """Resolve a raw export file name (or path) to where the file actually is."""
    if os.path.exists(name):
        return name
    base = os.path.basename(name)
    in_raw_dir = os.path.join(raw_telegram_dir(), base)
    if os.path.exists(in_raw_dir):
        return in_raw_dir
    in_root = os.path.join(PROJECT_ROOT, base)
    if os.path.exists(in_root):
        return in_root
    return in_raw_dir  # not found: report the expected location
