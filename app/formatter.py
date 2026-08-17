"""Build the Telegram message (§6).

    <a href="{html_url}">FEAT #111</a> · owner/repo
    alice commented
    Two or three sentences of what is actually going on.
    mention

HTML, not MarkdownV2: issue titles are full of `.`, `-`, `(` and `!`, every one
of which MarkdownV2 requires escaping.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import prefix as prefix_mod
from . import summary as summary_mod
from .config import Account, Behaviour
from .enrich import Enriched
from .format import TELEGRAM_MAX_CHARS, escape_html
from .github import Notification

# Telegram stamps every message with its send time. Only when send time and
# event time have actually drifted apart is the GitHub time worth repeating.
EVENT_TIME_GAP = timedelta(minutes=5)

# Which text answers "what is new here" for each reason (§6, D1).
COMMENT_FIRST = {"mention", "comment", "author", "subscribed", "manual", "team_mention"}
BODY_FIRST = {"assign", "review_requested", "state_change", "invitation", "security_alert"}

ACTION_BY_REASON = {
    "mention": "mentioned you",
    "team_mention": "mentioned your team",
    "assign": "assigned you",
    "review_requested": "requested your review",
    "review_request_removed": "withdrew the review request",
    "state_change": "changed the state",
    "security_alert": "raised a security alert",
    "invitation": "invited you",
    "ci_activity": "ran a workflow",
}


def _tz(behaviour: Behaviour) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(behaviour.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _pick_summary(note: Notification, enriched: Enriched) -> str:
    """Show the new information; fall back to context only when there is none."""
    comment = summary_mod.extract(enriched.comment_body)
    body = summary_mod.extract(enriched.body)

    if note.reason in COMMENT_FIRST:
        return comment or body
    if note.reason in BODY_FIRST:
        return body or comment
    return comment or body


def _state_chip(note: Notification, enriched: Enriched) -> str:
    if note.subject_type == "PullRequest":
        if enriched.merged:
            return "merged"
        if enriched.draft:
            return "draft"
        if enriched.state == "closed":
            return "closed"
    elif note.subject_type == "Issue" and enriched.state == "closed":
        return f"closed as {enriched.state_reason}" if enriched.state_reason else "closed"
    return ""


def _actor_line(note: Notification, enriched: Enriched) -> str:
    actor = enriched.actor
    action = ACTION_BY_REASON.get(note.reason)

    if enriched.comment_author:
        # A real comment triggered this, so name what they did to it.
        action = "commented" if note.reason not in ACTION_BY_REASON else action
        return f"{actor} {action}" if action else f"{actor} commented"
    if actor and action:
        return f"{actor} · {action}"
    if action:
        return action
    if actor:
        return f"opened by {actor}"
    return ""


def build_message(
    account: Account,
    behaviour: Behaviour,
    note: Notification,
    enriched: Enriched,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Return (html_message, prefix_tier). Tier is returned so it can be logged."""
    resolved = prefix_mod.resolve(
        account,
        title=note.subject_title,
        subject_type=note.subject_type,
        labels=enriched.labels,
        issue_type=enriched.issue_type,
    )

    number = note.number
    head_text = f"{resolved.text} #{number}" if number is not None else resolved.text

    lines: list[str] = []

    first = f'<a href="{escape_html(enriched.html_url)}">{escape_html(head_text)}</a>'
    first += f" · {escape_html(note.repo_full_name)}"
    chip = _state_chip(note, enriched)
    if chip:
        first += f" · {escape_html(chip)}"
    lines.append(first)

    # The title only earns its own line when the prefix didn't already carry it.
    title = resolved.title.strip()
    if title:
        lines.append(f"<b>{escape_html(title)}</b>")

    actor_line = _actor_line(note, enriched)
    if actor_line:
        lines.append(escape_html(actor_line))

    body = _pick_summary(note, enriched)
    if body:
        lines.append(escape_html(body))
    elif enriched.degraded:
        lines.append("<i>(could not read the body — deleted, moved, or no access)</i>")

    footer = note.reason
    now = now or datetime.now(timezone.utc)
    event = _parse(note.updated_at)
    if event and abs(now - event) > EVENT_TIME_GAP:
        local = event.astimezone(_tz(behaviour))
        footer += f" · event {local:%H:%M}"
    lines.append(f"<i>{escape_html(footer)}</i>")

    message = "\n".join(lines)
    if len(message) > TELEGRAM_MAX_CHARS:
        # Should be unreachable: the summary is capped at 400 chars long before
        # this. Belt and braces so a pathological title cannot 400 the send.
        message = message[: TELEGRAM_MAX_CHARS - 1] + "…"
    return message, resolved.tier


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
