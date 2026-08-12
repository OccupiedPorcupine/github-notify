"""SQLite state (§8). Schema is the spec's, verbatim in shape."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts_state (
  account       TEXT PRIMARY KEY,
  last_modified TEXT,
  cursor_ts     TEXT,
  last_poll_at  TEXT,
  last_error    TEXT
);

CREATE TABLE IF NOT EXISTS seen (
  account     TEXT NOT NULL,
  thread_id   TEXT NOT NULL,
  dedupe_key  TEXT NOT NULL,
  sent_at     TEXT NOT NULL,
  tg_msg_id   INTEGER,
  PRIMARY KEY (account, thread_id, dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_seen_sent ON seen(sent_at);

-- Not in the spec's §8 schema. Added for the Telegram getUpdates offset, so a
-- restart does not re-process commands it already answered.
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS outbox (
  id          INTEGER PRIMARY KEY,
  account     TEXT,
  chat_id     INTEGER,
  payload     TEXT,
  attempts    INTEGER DEFAULT 0,
  next_try_at TEXT,
  created_at  TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """GitHub-flavoured ISO8601: UTC, seconds precision, trailing Z."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("GHN_DB_PATH", "data/state.db")
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---- meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ---- accounts_state ----------------------------------------------------

    def get_state(self, account: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM accounts_state WHERE account = ?", (account,)
        ).fetchone()

    def init_state(self, account: str, cursor_ts: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO accounts_state (account, cursor_ts) VALUES (?, ?)",
            (account, cursor_ts),
        )

    def update_state(
        self,
        account: str,
        *,
        last_modified: str | None = None,
        cursor_ts: str | None = None,
        last_poll_at: str | None = None,
        last_error: str | None = "",
    ) -> None:
        """Update the named fields only.

        `last_error` defaults to "" meaning "leave alone"; pass None to clear it
        and a string to set it. Cursor and last_modified are never advanced on a
        failed poll (§9, "LXC loses network").
        """
        sets, params = [], []
        if last_modified is not None:
            sets.append("last_modified = ?")
            params.append(last_modified)
        if cursor_ts is not None:
            sets.append("cursor_ts = ?")
            params.append(cursor_ts)
        if last_poll_at is not None:
            sets.append("last_poll_at = ?")
            params.append(last_poll_at)
        if last_error != "":
            sets.append("last_error = ?")
            params.append(last_error)
        if not sets:
            return
        params.append(account)
        self.conn.execute(
            f"UPDATE accounts_state SET {', '.join(sets)} WHERE account = ?", params
        )

    # ---- seen --------------------------------------------------------------

    def is_seen(self, account: str, thread_id: str, dedupe_key: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM seen WHERE account=? AND thread_id=? AND dedupe_key=?",
                (account, thread_id, dedupe_key),
            ).fetchone()
            is not None
        )

    def mark_seen(
        self,
        account: str,
        thread_id: str,
        dedupe_key: str,
        tg_msg_id: int | None = None,
    ) -> bool:
        """Record a (thread, event) as handled. Returns True if newly inserted."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO seen (account, thread_id, dedupe_key, sent_at, tg_msg_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (account, thread_id, dedupe_key, iso(utcnow()), tg_msg_id),
        )
        return cur.rowcount > 0

    def prune_seen(self, retention_days: int) -> int:
        cutoff = iso(utcnow() - timedelta(days=retention_days))
        cur = self.conn.execute("DELETE FROM seen WHERE sent_at < ?", (cutoff,))
        return cur.rowcount

    def count_seen_since(self, account: str, since: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM seen WHERE account=? AND sent_at >= ?",
            (account, since),
        ).fetchone()
        return int(row["n"])
