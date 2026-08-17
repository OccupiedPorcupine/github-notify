"""Extract a 2-3 sentence summary from GitHub markdown (§6).

Extracted text, never generated. Summarising a two-sentence comment into two
sentences gains nothing and adds a failure mode.

The segmentation has to handle Chinese, which is what breaks the naive rule:
`[.!?]\\s` finds zero boundaries in a Chinese paragraph, so the summary silently
degrades into "first N characters, cut mid-word".
"""

from __future__ import annotations

import re

# ASCII and full-width terminators. Crucially NOT followed by a required \s —
# Chinese does not put spaces between words.
TERMINATORS = ".!?。！？；…"

# A bare `.` only ends a sentence when it is not between digits. Without the
# digit guard, "Postgres 17.6.1" becomes three sentences and renders as
# "Postgres 17. 6. 1", and every "1." in a numbered list splits too.
_SENTENCE_RE = re.compile(r".*?(?:(?<!\d)\.(?!\d)|[!?。！？；…])+", re.DOTALL)

MAX_CHARS = 400
MAX_CHARS_CJK = 200          # 400 CJK chars carries 2-3x the content of 400 ASCII
CJK_DOMINANT_RATIO = 0.30

_CJK_RE = re.compile(
    r"[一-鿿㐀-䶿豈-﫿\U00020000-\U0002ebef]"
)

_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_DETAILS_RE = re.compile(r"<details.*?</details>", re.DOTALL | re.IGNORECASE)
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_QUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
_MENTION_RE = re.compile(r"(?<![\w/])@[A-Za-z0-9][A-Za-z0-9-]*")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
# Emphasis markers render as literal asterisks in Telegram HTML mode.
_EMPHASIS_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(?=\S)(.+?)(?<=\S)\1", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t]+")
_BLANK_RE = re.compile(r"\n{2,}")


def is_cjk_dominant(text: str) -> bool:
    if not text:
        return False
    cjk = len(_CJK_RE.findall(text))
    return cjk / len(text) > CJK_DOMINANT_RATIO


def strip_markdown(text: str | None) -> str:
    """Remove the artifacts that render as noise or dead text in Telegram."""
    if not text:
        return ""

    # Order matters: details blocks and fences can contain everything else.
    text = _HTML_COMMENT_RE.sub(" ", text)
    text = _DETAILS_RE.sub(" ", text)
    text = _FENCE_RE.sub(" ", text)
    text = _IMAGE_RE.sub(" ", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _QUOTE_LINE_RE.sub(" ", text)      # quoted reply text
    text = _HEADING_RE.sub("", text)
    text = _EMPHASIS_RE.sub(r"\2", text)
    text = _INLINE_CODE_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _MENTION_RE.sub("", text)          # would render as dead text
    text = text.replace("\r", "")
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n", text)
    return text.strip()


def extract(body: str | None, *, max_sentences: int = 3) -> str:
    """First few sentences of `body`, cleaned and capped. '' if nothing usable.

    An empty return is meaningful: the caller falls back to title-only, which is
    the right answer for a body that was entirely a code block or a template.
    """
    cleaned = strip_markdown(body)
    if not cleaned:
        return ""

    cap = MAX_CHARS_CJK if is_cjk_dominant(cleaned) else MAX_CHARS

    sentences: list[str] = []
    consumed = 0
    for match in _SENTENCE_RE.finditer(cleaned):
        sentence = match.group().strip()
        if not sentence:
            continue
        sentences.append(sentence)
        consumed = match.end()
        if len(sentences) >= max_sentences:
            break

    if not sentences:
        # No terminator at all — a wall of text, common in pasted logs.
        # len() on str counts code points, so this is safe for CJK and emoji.
        if len(cleaned) <= cap:
            return cleaned
        return cleaned[:cap].rstrip() + "…"

    summary = " ".join(sentences)
    if len(summary) > cap:
        return summary[:cap].rstrip() + "…"

    # If we stopped early on sentence count but there is clearly more, say so.
    if len(cleaned) > consumed + 1:
        summary += " …"
    return summary
