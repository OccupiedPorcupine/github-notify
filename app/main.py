"""github-notify — build step 1: poller + SQLite state, log-only.

Nothing is sent to Telegram yet. Every notification that survives filtering is
logged as a `would_send` record so a day of real traffic tells you the actual
per-day rate, which is what §5 says should decide the coalesce window and
whether `subscribed: off` holds.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import signal
import sys
from collections import Counter
from datetime import timedelta

from . import filters, github
from .bot import CommandBot
from .config import Account, Config, ConfigError, load_config
from .db import Database, iso, parse_iso, utcnow
from .github import GitHubClient, Notification
from .logging_setup import setup_logging

log = logging.getLogger("main")

HEARTBEAT_PATH = os.environ.get("GHN_HEARTBEAT", "/data/heartbeat")
LOCK_PATH = os.environ.get("GHN_LOCK", "/data/github-notify.lock")

# How long to wait after an error the operator has to fix by hand. Long, because
# retrying a revoked token faster changes nothing and just fills the log (§9).
FATAL_RETRY_SECONDS = 900.0
MAX_BACKOFF_SECONDS = 600.0


def acquire_single_instance_lock() -> "object":
    """§9: two instances double-send. Fail fast rather than racing."""
    os.makedirs(os.path.dirname(LOCK_PATH) or ".", exist_ok=True)
    handle = open(LOCK_PATH, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"another github-notify already holds {LOCK_PATH}. Refusing to start "
            f"a second instance."
        ) from None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def touch_heartbeat() -> None:
    """§9/§10: deadman signal. Stale mtime means the poller is wedged."""
    try:
        with open(HEARTBEAT_PATH, "w") as handle:
            handle.write(iso(utcnow()))
    except OSError as exc:
        log.warning("heartbeat write failed", extra={"error": str(exc)})


class AccountWorker:
    def __init__(self, account: Account, config: Config, db: Database) -> None:
        self.account = account
        self.config = config
        self.db = db
        self.client = GitHubClient(account.api_base, account.token)
        self.identity: github.Identity | None = None
        self.backoff = 5.0
        self._alerted_fatal: str | None = None

    async def aclose(self) -> None:
        await self.client.aclose()

    # ---- startup -----------------------------------------------------------

    async def probe_until_ready(self, stop: asyncio.Event) -> bool:
        while not stop.is_set():
            try:
                self.identity = await self.client.probe()
            except github.TransientError as exc:
                log.warning(
                    "probe failed, retrying",
                    extra={"account": self.account.name, "error": str(exc)},
                )
                await _sleep_or_stop(self.backoff, stop)
                self.backoff = min(self.backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except github.GitHubError as exc:
                # 401, scope, SSO, org policy. None of these fix themselves.
                log.error(
                    "startup probe rejected — fix required",
                    extra={
                        "account": self.account.name,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "authorize_url": getattr(exc, "authorize_url", None),
                    },
                )
                return False

            self.backoff = 5.0
            missing_recommended = sorted(
                github.RECOMMENDED_SCOPES - self.identity.scopes
            )
            log.info(
                "authenticated",
                extra={
                    "account": self.account.name,
                    "login": self.identity.login,
                    "scopes": sorted(self.identity.scopes),
                    "rate_limit": self.identity.rate_limit,
                },
            )
            if self.identity.rate_limit == 60:
                log.error(
                    "rate limit is 60, which means the token is not being read "
                    "at all — requests are going out unauthenticated (§12 D6)",
                    extra={"account": self.account.name},
                )
                return False
            if missing_recommended and self.identity.scopes:
                log.warning(
                    "token lacks `repo`; private issue and comment bodies will "
                    "not be readable, so those messages arrive title-only",
                    extra={
                        "account": self.account.name,
                        "missing": missing_recommended,
                    },
                )
            return True
        return False

    def ensure_cursor(self) -> str:
        """§9: first ever run sets the cursor to now. Never backfill cold."""
        state = self.db.get_state(self.account.name)
        if state is None:
            cursor = iso(utcnow())
            self.db.init_state(self.account.name, cursor)
            log.info(
                "cold start — cursor set to now, no backfill",
                extra={"account": self.account.name, "cursor_ts": cursor},
            )
            return cursor

        cursor = state["cursor_ts"] or iso(utcnow())
        parsed = parse_iso(cursor)
        max_gap = timedelta(hours=self.config.behaviour.backfill_max_hours)
        if parsed and utcnow() - parsed > max_gap:
            gap_hours = round((utcnow() - parsed).total_seconds() / 3600, 1)
            # §9: don't replay days of history. Skip forward and say so.
            fresh = iso(utcnow())
            self.db.update_state(
                self.account.name, cursor_ts=fresh, last_modified=""
            )
            log.warning(
                "downtime exceeded backfill_max_hours — skipping the gap "
                "instead of replaying it; check https://github.com/notifications",
                extra={
                    "account": self.account.name,
                    "gap_hours": gap_hours,
                    "backfill_max_hours": self.config.behaviour.backfill_max_hours,
                    "cursor_ts": fresh,
                },
            )
            return fresh
        return cursor

    # ---- poll loop ---------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        if not await self.probe_until_ready(stop):
            log.error(
                "worker stopping — startup probe did not pass",
                extra={"account": self.account.name},
            )
            return

        cursor = self.ensure_cursor()
        state = self.db.get_state(self.account.name)
        last_modified = (state["last_modified"] or None) if state else None
        last_prune = utcnow()

        while not stop.is_set():
            try:
                result = await self.client.poll_notifications(
                    since=cursor, last_modified=last_modified
                )
            except github.RateLimited as exc:
                log.warning(
                    "rate limited",
                    extra={
                        "account": self.account.name,
                        "retry_after": exc.retry_after,
                    },
                )
                self.db.update_state(self.account.name, last_error=str(exc))
                await _sleep_or_stop(exc.retry_after, stop)
                continue
            except github.TransientError as exc:
                log.warning(
                    "poll failed, backing off — cursor not advanced",
                    extra={
                        "account": self.account.name,
                        "error": str(exc),
                        "backoff": self.backoff,
                    },
                )
                self.db.update_state(self.account.name, last_error=str(exc))
                await _sleep_or_stop(self.backoff, stop)
                self.backoff = min(self.backoff * 2, MAX_BACKOFF_SECONDS)
                continue
            except github.GitHubError as exc:
                # Auth, SSO, scope, org policy. Alert once, then slow-retry.
                signature = f"{type(exc).__name__}:{exc}"
                if signature != self._alerted_fatal:
                    log.error(
                        "GitHub rejected the poll — needs operator action",
                        extra={
                            "account": self.account.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "authorize_url": getattr(exc, "authorize_url", None),
                        },
                    )
                    self._alerted_fatal = signature
                self.db.update_state(self.account.name, last_error=str(exc))
                await _sleep_or_stop(FATAL_RETRY_SECONDS, stop)
                continue

            self.backoff = 5.0
            self._alerted_fatal = None
            cursor = self.handle_result(result, cursor)
            last_modified = result.last_modified
            self.db.update_state(
                self.account.name,
                last_modified=last_modified or "",
                cursor_ts=cursor,
                last_poll_at=iso(utcnow()),
                last_error=None,
            )
            touch_heartbeat()

            if utcnow() - last_prune > timedelta(hours=24):
                pruned = self.db.prune_seen(self.config.behaviour.seen_retention_days)
                last_prune = utcnow()
                log.info(
                    "pruned seen table",
                    extra={"account": self.account.name, "rows": pruned},
                )

            await _sleep_or_stop(float(result.poll_interval), stop)

    def handle_result(self, result: github.PollResult, cursor: str) -> str:
        if result.not_modified:
            log.debug("304 not modified", extra={"account": self.account.name})
            return cursor

        kept = 0
        duplicates = 0
        dropped: Counter[str] = Counter()
        newest = parse_iso(cursor)

        for note in result.notifications:
            updated = parse_iso(note.updated_at)
            if updated and (newest is None or updated > newest):
                newest = updated

            drop_reason = filters.drop_reason(self.account, self.config, note)
            if drop_reason:
                dropped[drop_reason.split(":", 1)[0]] += 1
                log.debug(
                    "filtered",
                    extra={
                        "account": self.account.name,
                        "thread_id": note.thread_id,
                        "reason": note.reason,
                        "drop": drop_reason,
                    },
                )
                continue

            if not self.db.mark_seen(
                self.account.name, note.thread_id, note.dedupe_key
            ):
                duplicates += 1
                continue

            kept += 1
            # Step 1 stops here. Step 3 replaces this with enrich → format → send.
            log.info(
                "would_send",
                extra={
                    "account": self.account.name,
                    "thread_id": note.thread_id,
                    "reason": note.reason,
                    "subject_type": note.subject_type,
                    "repo": note.repo_full_name,
                    "number": note.number,
                    "title": note.subject_title,
                    "updated_at": note.updated_at,
                    "dedupe_key": note.dedupe_key,
                    "has_comment": note.latest_comment_url is not None,
                },
            )

        log.info(
            "poll complete",
            extra={
                "account": self.account.name,
                "received": len(result.notifications),
                "new": kept,
                "duplicate": duplicates,
                "dropped": dict(dropped),
                "poll_interval": result.poll_interval,
            },
        )

        # §9 clock skew: the cursor comes from GitHub's timestamps, not ours.
        return iso(newest) if newest else cursor


async def _sleep_or_stop(seconds: float, stop: asyncio.Event) -> None:
    """Sleep, but wake immediately on shutdown."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def amain() -> int:
    setup_logging()

    try:
        config = load_config(require_telegram=False)
    except ConfigError as exc:
        log.error("config error", extra={"error": str(exc)})
        return 2

    lock = acquire_single_instance_lock()
    db = Database()

    log.info(
        "starting",
        extra={
            "mode": "log-only (build step 1, no sending)",
            "accounts": [a.name for a in config.accounts],
            "timezone": config.behaviour.timezone,
            "db": db.path,
        },
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    workers = [AccountWorker(a, config, db) for a in config.accounts]
    command_bot = CommandBot(config, db, workers)
    try:
        await asyncio.gather(
            *(w.run(stop) for w in workers),
            command_bot.run(stop),
        )
    finally:
        for worker in workers:
            await worker.aclose()
        await command_bot.aclose()
        db.close()
        lock.close()
        log.info("stopped")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
