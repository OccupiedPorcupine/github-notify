"""Resolve the `FEAT #111` prefix (§6).

Tier order is label map first, conventional-commit second. That inversion was
deliberate: on a repo whose issue titles are descriptive Chinese, the
conventional-commit pattern never matches, so leading with it would mean every
message fell through to a generic default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import Account

# `[a-zA-Z]` for the type is load-bearing. Widening it to `\w` would match CJK
# in Python's regex engine and turn a Chinese title into a nonsense prefix.
#
# The `(?!//)` after the colon rejects URLs. A title like
# `https://example.com: broken` otherwise parses as type `https`. The spec
# proposed catching that by rejecting types over 12 characters, but `https` is
# five — that rule never fires here. Looking for the `//` is what actually works.
CONVENTIONAL_RE = re.compile(
    r"^\s*(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<breaking>!)?:(?!//)\s*(?P<subject>.+)$"
)
BRACKET_RE = re.compile(r"^\s*\[(\w+)\]\s*(.+)$")

MAX_TYPE_LEN = 12   # rejects `https://example.com: broken` matching as `https`
MAX_PREFIX_LEN = 10

CONVENTIONAL_MAP = {
    "feat": "FEAT",
    "feature": "FEAT",
    "fix": "FIX",
    "bugfix": "FIX",
    "hotfix": "FIX",
    "docs": "DOCS",
    "chore": "CHORE",
    "refactor": "REFACTOR",
    "test": "TEST",
    "tests": "TEST",
    "perf": "PERF",
    "build": "BUILD",
    "ci": "CI",
    "style": "STYLE",
    "revert": "REVERT",
}

TYPE_DEFAULTS = {
    "Issue": "ISSUE",
    "PullRequest": "PR",
    "Discussion": "DISC",
    "Commit": "COMMIT",
    "Release": "RELEASE",
    "CheckSuite": "CI",
    "RepositoryVulnerabilityAlert": "SECURITY",
    "RepositoryInvitation": "INVITE",
}


@dataclass(frozen=True)
class Prefix:
    text: str          # e.g. "FEAT" or "FEAT!"
    title: str         # display title, possibly with the matched prefix stripped
    tier: str          # which tier resolved it — logged, so drift is visible


def _from_labels(account: Account, labels: list[str]) -> str | None:
    for label in labels:
        mapped = account.prefix.label_map.get(label)
        if mapped:
            return mapped
    # Case-insensitive second pass, so `Bug` matches a `bug` key.
    lowered = {k.lower(): v for k, v in account.prefix.label_map.items()}
    for label in labels:
        mapped = lowered.get(label.lower())
        if mapped:
            return mapped
    return None


def _from_conventional(account: Account, title: str) -> tuple[str, str] | None:
    match = CONVENTIONAL_RE.match(title)
    if not match:
        return None

    kind = match.group("type")
    if len(kind) > MAX_TYPE_LEN:
        return None

    subject = (match.group("subject") or "").strip()
    if not subject:
        return None   # `feat:` with an empty subject — keep the raw title

    text = CONVENTIONAL_MAP.get(kind.lower(), kind.upper()[:MAX_PREFIX_LEN])
    if account.prefix.include_scope and match.group("scope"):
        text = f"{text}({match.group('scope').upper()})"
    if match.group("breaking"):
        text += "!"

    display = subject if account.prefix.strip_from_title else title
    return text, display


def _from_bracket(account: Account, title: str) -> tuple[str, str] | None:
    if not account.prefix.bracket_fallback:
        return None
    match = BRACKET_RE.match(title)
    if not match:
        return None
    return match.group(1).upper()[:MAX_PREFIX_LEN], match.group(2).strip()


def resolve(
    account: Account,
    *,
    title: str,
    subject_type: str,
    labels: list[str] | None = None,
    issue_type: str | None = None,
) -> Prefix:
    labels = labels or []

    for tier in account.prefix.strategy:
        if tier == "label":
            found = _from_labels(account, labels)
            if found:
                return Prefix(found, title, "label")

        elif tier == "conventional":
            found_pair = _from_conventional(account, title)
            if found_pair:
                return Prefix(found_pair[0], found_pair[1], "conventional")
            found_pair = _from_bracket(account, title)
            if found_pair:
                return Prefix(found_pair[0], found_pair[1], "bracket")

        elif tier == "issue_type":
            if issue_type:
                return Prefix(issue_type.upper()[:MAX_PREFIX_LEN], title, "issue_type")

    return Prefix(TYPE_DEFAULTS.get(subject_type, "NOTE"), title, "type_default")
