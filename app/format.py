"""Message formatting helpers (§6). Grows into the full formatter at step 3."""

from __future__ import annotations

# Telegram's HTML parse mode needs exactly these three escaped in text nodes.
# Escaping more (quotes, slashes) makes titles render with visible entities.
_ESCAPES = (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"))

TELEGRAM_MAX_CHARS = 4096


def escape_html(text: str | None) -> str:
    """Escape a string for interpolation into a Telegram HTML message.

    §6 and §9: every interpolated field goes through this. An issue titled
    `<script>` or `A & B` otherwise produces a 400 from Telegram, or worse,
    silently swallows part of the title.
    """
    if not text:
        return ""
    for needle, replacement in _ESCAPES:
        text = text.replace(needle, replacement)
    return text


def clamp(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Hard cap by characters, not bytes or UTF-16 units (§6).

    Python `len()` on `str` already counts code points, so this is correct for
    CJK and for emoji outside the BMP.
    """
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
