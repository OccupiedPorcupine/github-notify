"""The filter predicate (§5), in one place.

The poller, the terminal `scope` command and the Telegram `/repos` command all
call this. Keeping three copies in sync by hand was already drifting.
"""

from __future__ import annotations

from .config import Account, Config
from .github import Notification


def drop_reason(account: Account, config: Config, note: Notification) -> str | None:
    """Return a `category:detail` drop reason, or None to forward.

    Not yet applied: dropping self-authored events, which needs the triggering
    comment's author and therefore enrichment (build step 2).
    """
    if note.reason not in account.reasons:
        return f"reason:{note.reason}"
    if not account.accepts_subject_type(note.subject_type):
        return f"subject_type:{note.subject_type}"
    if not account.accepts_repo(note.repo_full_name):
        return f"repo:{note.repo_full_name}"
    if note.subject_type == "PullRequest" and not config.behaviour.include_prs:
        return "include_prs:false"
    return None


def humanise(reason: str) -> str:
    category, _, detail = reason.partition(":")
    return f"{category.replace('_', ' ')} {detail}".strip()
