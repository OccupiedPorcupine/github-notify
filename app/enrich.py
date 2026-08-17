"""Fetch the detail a notification payload doesn't carry (§4, §9).

The Notifications API gives you a title, a type, and API URLs. It does not give
you the issue number, the browser link, the body, the labels, or who did the
thing. All of that has to be fetched.

Every fetch here is allowed to fail softly: a 404 from a deleted or transferred
issue produces a title-only message rather than a dropped notification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import github
from .github import GitHubClient, Notification

log = logging.getLogger("enrich")

# Conclusions that are not worth waking someone up for (§5, ci_filter).
BORING_CI_CONCLUSIONS = {"success", "skipped", "neutral"}


@dataclass
class Enriched:
    html_url: str | None = None
    body: str | None = None
    subject_author: str | None = None
    labels: list[str] = field(default_factory=list)
    issue_type: str | None = None
    state: str | None = None            # open / closed
    state_reason: str | None = None     # completed / not_planned
    draft: bool = False
    merged: bool = False

    comment_body: str | None = None
    comment_author: str | None = None
    comment_url: str | None = None

    ci_conclusion: str | None = None
    degraded: bool = False              # enrichment failed; title-only message

    @property
    def actor(self) -> str | None:
        return self.comment_author or self.subject_author


def _web_url_fallback(note: Notification) -> str:
    """Construct a browser URL when we could not fetch the real one (§4)."""
    number = note.number
    if number is None:
        return f"https://github.com/{note.repo_full_name}"
    kind = "pull" if note.subject_type == "PullRequest" else "issues"
    return f"https://github.com/{note.repo_full_name}/{kind}/{number}"


async def enrich(client: GitHubClient, note: Notification) -> Enriched:
    result = Enriched()

    if note.subject_url:
        try:
            subject = await client.get_json(note.subject_url)
        except github.GitHubError as exc:
            log.warning(
                "subject fetch failed, degrading to title-only",
                extra={"thread_id": note.thread_id, "error": str(exc)},
            )
            subject = None
            result.degraded = True

        if subject:
            result.html_url = subject.get("html_url")
            result.body = subject.get("body")
            result.subject_author = ((subject.get("user") or {}).get("login"))
            result.labels = [
                label.get("name", "")
                for label in (subject.get("labels") or [])
                if isinstance(label, dict)
            ]
            result.issue_type = ((subject.get("type") or {}) or {}).get("name")
            result.state = subject.get("state")
            result.state_reason = subject.get("state_reason")
            result.draft = bool(subject.get("draft"))
            result.merged = bool(subject.get("merged"))
            result.ci_conclusion = subject.get("conclusion")
        else:
            result.degraded = True

    if not result.html_url:
        result.html_url = _web_url_fallback(note)

    if note.latest_comment_url and note.latest_comment_url != note.subject_url:
        try:
            comment = await client.get_json(note.latest_comment_url)
        except github.GitHubError as exc:
            log.warning(
                "comment fetch failed",
                extra={"thread_id": note.thread_id, "error": str(exc)},
            )
            comment = None

        if comment:
            result.comment_body = comment.get("body")
            result.comment_author = ((comment.get("user") or {}).get("login"))
            result.comment_url = comment.get("html_url")

    return result


def is_self_authored(enriched: Enriched, viewer_login: str) -> bool:
    """§5: you get notified for your own comments in threads you're in.

    Only the *triggering comment* counts. Being the issue author is not
    self-authorship — that's the whole point of the `author` reason.
    """
    if not enriched.comment_author:
        return False
    return enriched.comment_author.lower() == viewer_login.lower()


def ci_should_drop(enriched: Enriched, ci_filter: str) -> bool:
    if ci_filter == "off":
        return True
    if ci_filter == "all":
        return False
    conclusion = (enriched.ci_conclusion or "").lower()
    # A run still in progress has no conclusion yet; wait for the final event.
    if not conclusion:
        return True
    return conclusion in BORING_CI_CONCLUSIONS
