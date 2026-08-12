"""Telegram command listener — check the bot's state from the chat (§10).

Runs alongside the GitHub pollers as a second asyncio task. Commands:

    /repos    which repos are feeding the bot, and how many would forward
    /scope    the effective filters — reasons on/off, allow/block, label map
    /status   last poll, cursor, errors, events seen today
    /help     the above

§11.1 is enforced here and it is the whole reason the allowlist is mandatory:
anyone who finds @the_bot can message it, and without the check a stranger
could read your private work issue content out of /repos.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Any

from . import filters, github
from .config import KNOWN_REASONS, Config
from .db import Database, iso, parse_iso, utcnow
from .format import clamp, escape_html
from .telegram import (
    TelegramClient,
    TelegramConflict,
    TelegramError,
    TelegramRateLimited,
)

if TYPE_CHECKING:
    from .main import AccountWorker

log = logging.getLogger("bot")

OFFSET_KEY = "telegram_update_offset"
LONG_POLL_SECONDS = 25
MAX_BACKOFF_SECONDS = 300.0

HELP = (
    "<b>github-notify</b>\n"
    "/repos — repos currently feeding the bot\n"
    "/scope — effective filters\n"
    "/status — last poll, cursor, errors\n"
    "/help — this"
)


class CommandBot:
    def __init__(
        self, config: Config, db: Database, workers: list["AccountWorker"]
    ) -> None:
        self.config = config
        self.db = db
        self.workers = workers
        # Long polling holds the connection open for LONG_POLL_SECONDS, so the
        # HTTP timeout has to comfortably exceed it.
        self.client = TelegramClient(
            config.telegram.bot_token or "", timeout=LONG_POLL_SECONDS + 20
        )
        self.backoff = 5.0

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- transport ---------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        if not self.config.telegram.bot_token:
            log.warning("no telegram bot token, command listener disabled")
            return
        if not self.config.telegram.allowed_chat_ids:
            # Refusing to listen is the safe failure: an open bot leaks issue
            # content to anyone who finds it (§11.1).
            log.error(
                "telegram.allowed_chat_ids is empty — command listener disabled. "
                "Add your chat id to config.yaml to enable /repos and /status."
            )
            return

        try:
            identity = await self.client.get_me()
            log.info("command listener ready", extra={"bot": f"@{identity.username}"})
        except TelegramError as exc:
            log.error("command listener could not start", extra={"error": str(exc)})
            return

        raw_offset = self.db.get_meta(OFFSET_KEY)
        offset = int(raw_offset) if raw_offset and raw_offset.isdigit() else None

        while not stop.is_set():
            try:
                updates = await self.client.get_updates(
                    offset=offset, timeout=LONG_POLL_SECONDS
                )
                self.backoff = 5.0
            except TelegramConflict as exc:
                # Someone ran `app.ping --discover`, or a second instance is up.
                log.warning("getUpdates conflict, backing off", extra={"error": str(exc)})
                await _sleep_or_stop(30.0, stop)
                continue
            except TelegramRateLimited as exc:
                await _sleep_or_stop(exc.retry_after, stop)
                continue
            except TelegramError as exc:
                log.warning(
                    "getUpdates failed, backing off",
                    extra={"error": str(exc), "backoff": self.backoff},
                )
                await _sleep_or_stop(self.backoff, stop)
                self.backoff = min(self.backoff * 2, MAX_BACKOFF_SECONDS)
                continue

            for update in updates:
                offset = int(update["update_id"]) + 1
                try:
                    await self.handle_update(update)
                except Exception:
                    # One bad command must never take down the listener.
                    log.exception("command handler raised")

            if offset is not None:
                self.db.set_meta(OFFSET_KEY, str(offset))

    async def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        text = (message.get("text") or "").strip()

        if chat_id is None or not text:
            return

        if chat_id not in self.config.telegram.allowed_chat_ids:
            # §11.1: drop silently. Replying would confirm the bot exists and
            # is live, which is exactly what an unknown sender is probing for.
            log.warning(
                "dropped message from chat outside the allowlist",
                extra={"chat_id": chat_id},
            )
            return

        if not text.startswith("/"):
            return

        # `/repos@githubsurveyorbot` is what Telegram sends in groups.
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        thread_id = message.get("message_thread_id")

        handlers = {
            "repos": self.cmd_repos,
            "scope": self.cmd_scope,
            "status": self.cmd_status,
            "help": self.cmd_help,
            "start": self.cmd_help,
        }
        handler = handlers.get(command)
        reply = await handler() if handler else f"unknown command /{escape_html(command)}\n\n{HELP}"

        log.info("command handled", extra={"command": command, "chat_id": chat_id})
        await self.client.send_message(chat_id, clamp(reply), thread_id=thread_id)

    # ---- commands ----------------------------------------------------------

    async def cmd_help(self) -> str:
        return HELP

    async def cmd_repos(self) -> str:
        """Live per-repo view: what is actually arriving, and what forwards."""
        blocks: list[str] = []
        for worker in self.workers:
            account, config = worker.account, worker.config
            try:
                result = await worker.client.poll_notifications()
            except github.GitHubError as exc:
                blocks.append(
                    f"<b>{escape_html(account.name)}</b>\n"
                    f"could not reach GitHub: {escape_html(str(exc)[:200])}"
                )
                continue

            notes = result.notifications
            if not notes:
                blocks.append(
                    f"<b>{escape_html(account.name)}</b>\n"
                    f"nothing unread right now. That means your GitHub inbox is "
                    f"clear, not that the bot is broken — /status for health."
                )
                continue

            per_repo: dict[str, list] = defaultdict(list)
            for note in notes:
                per_repo[note.repo_full_name].append(note)

            lines = []
            forwarded_total = 0
            for name in sorted(per_repo):
                group = per_repo[name]
                kept = [
                    n for n in group if filters.drop_reason(account, config, n) is None
                ]
                forwarded_total += len(kept)
                reasons = Counter(n.reason for n in group)
                reason_text = ", ".join(f"{r} {c}" for r, c in reasons.most_common())
                lines.append(
                    f"{escape_html(name)}\n"
                    f"  {len(group)} unread · {len(kept)}/{len(group)} forwarded\n"
                    f"  {escape_html(reason_text)}"
                )

            types = Counter(n.subject_type for n in notes)
            blocks.append(
                f"<b>{escape_html(account.name)}</b> — "
                f"{len(per_repo)} repo{'s' if len(per_repo) != 1 else ''}, "
                f"{len(notes)} unread\n\n"
                + "\n".join(lines)
                + f"\n\n{forwarded_total}/{len(notes)} would forward · "
                + escape_html(", ".join(f"{t} {c}" for t, c in types.most_common()))
            )

        return "\n\n".join(blocks)

    async def cmd_scope(self) -> str:
        blocks: list[str] = []
        for worker in self.workers:
            account = worker.account
            off = KNOWN_REASONS - set(account.reasons)
            subject = (
                "all"
                if account.subject_types == "all"
                else ", ".join(sorted(account.subject_types))
            )
            allow = ", ".join(account.repos.allow) if account.repos.allow else "all repos"
            block = ", ".join(account.repos.block) if account.repos.block else "none"
            label_count = len(account.prefix.label_map)
            label_note = "" if label_count else " (tier 1 never fires)"

            blocks.append(
                f"<b>{escape_html(account.name)}</b>\n"
                f"reasons on: {escape_html(', '.join(sorted(account.reasons)))}\n"
                f"reasons off: {escape_html(', '.join(sorted(off)) or 'none')}\n"
                f"subject types: {escape_html(subject)}\n"
                f"ci filter: {escape_html(account.ci_filter)}\n"
                f"allow: {escape_html(allow)}\n"
                f"block: {escape_html(block)}\n"
                f"label map: {label_count} entries{label_note}\n"
                f"prefix: {escape_html(' → '.join(account.prefix.strategy))}"
            )
        return "\n\n".join(blocks)

    async def cmd_status(self) -> str:
        midnight = iso(utcnow().replace(hour=0, minute=0, second=0, microsecond=0))
        blocks: list[str] = []
        for worker in self.workers:
            name = worker.account.name
            state = self.db.get_state(name)
            last_poll = state["last_poll_at"] if state else None
            error = (state["last_error"] if state else None) or "none"
            today = self.db.count_seen_since(name, midnight)
            login = worker.identity.login if worker.identity else "?"

            blocks.append(
                f"<b>{escape_html(name)}</b> ({escape_html(login)})\n"
                f"last poll: {escape_html(_ago(last_poll))}\n"
                f"cursor: {escape_html((state['cursor_ts'] if state else None) or '?')}\n"
                f"events today: {today}\n"
                f"error: {escape_html(str(error)[:200])}"
            )

        blocks.append("<i>mode: log-only — nothing is forwarded yet (build step 1)</i>")
        return "\n\n".join(blocks)


def _ago(timestamp: str | None) -> str:
    parsed = parse_iso(timestamp)
    if not parsed:
        return "never"
    seconds = int((utcnow() - parsed).total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


async def _sleep_or_stop(seconds: float, stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
