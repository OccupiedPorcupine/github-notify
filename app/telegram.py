"""Telegram Bot API client (§9 delivery, §11 security).

Send-only. The bot never needs to receive anything except for `/status` and
`/reload` later, and those come through the same getUpdates call used here for
chat discovery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .format import TELEGRAM_MAX_CHARS

log = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """Base for Telegram API failures."""


class TelegramAuthError(TelegramError):
    """401/404 on the token. The bot token is wrong or revoked."""


class TelegramBadRequest(TelegramError):
    """400. Almost always malformed HTML in the message body."""


class TelegramForbidden(TelegramError):
    """403. The user blocked the bot, or never pressed Start."""


class TelegramConflict(TelegramError):
    """409. Another getUpdates poller is running against the same token."""


class TelegramRateLimited(TelegramError):
    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class BotIdentity:
    id: int
    username: str
    can_read_all_group_messages: bool


@dataclass(frozen=True)
class DiscoveredChat:
    chat_id: int
    chat_type: str
    title: str
    thread_id: int | None


class TelegramClient:
    def __init__(self, bot_token: str, *, timeout: float = 30.0) -> None:
        # Note the URL shape: /bot<TOKEN>, no slash between (§0.3).
        self._base = f"{API_BASE}/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, data: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.post(f"{self._base}/{method}", data=data or {})
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method} request failed: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            raise TelegramError(
                f"{method} returned non-JSON ({response.status_code})"
            ) from None

        if payload.get("ok"):
            return payload.get("result")

        description = payload.get("description", "no description")
        status = response.status_code

        if status == 429:
            retry_after = float(
                (payload.get("parameters") or {}).get("retry_after", 30)
            )
            # §9: honour retry_after exactly, and don't count it as a failure.
            raise TelegramRateLimited(description, retry_after)
        if status in (401, 404):
            raise TelegramAuthError(
                f"Telegram rejected the bot token ({status}): {description}. "
                f"Check TELEGRAM_BOT_TOKEN in .env."
            )
        if status == 403:
            raise TelegramForbidden(
                f"Telegram refused delivery (403): {description}. The usual cause "
                f"is that the chat has never pressed Start — bots cannot message "
                f"you first (§0.3)."
            )
        if status == 409:
            raise TelegramConflict(
                f"Telegram reports a conflicting getUpdates poller (409): "
                f"{description}. Only one process may long-poll a bot token."
            )
        if status == 400:
            raise TelegramBadRequest(f"Telegram rejected the request (400): {description}")
        raise TelegramError(f"{method} failed ({status}): {description}")

    async def get_me(self) -> BotIdentity:
        result = await self._call("getMe")
        return BotIdentity(
            id=result["id"],
            username=result.get("username", "unknown"),
            can_read_all_group_messages=bool(
                result.get("can_read_all_group_messages", False)
            ),
        )

    async def get_updates(
        self, *, offset: int | None = None, timeout: int = 25
    ) -> list[dict[str, Any]]:
        """Long-poll for updates. `offset` confirms everything below it."""
        data: dict[str, Any] = {
            "timeout": str(timeout),
            "allowed_updates": '["message"]',
        }
        if offset is not None:
            data["offset"] = str(offset)
        return await self._call("getUpdates", data) or []

    async def discover_chats(self) -> list[DiscoveredChat]:
        """Chat ids from pending updates (§0.3).

        Safe to call here because this bot does not run a getUpdates poller —
        the two are mutually exclusive. Once `/status` exists at step 6, this
        moves behind the same update loop.
        """
        updates = await self._call("getUpdates", {"limit": "100", "timeout": "0"})
        seen: dict[tuple[int, int | None], DiscoveredChat] = {}
        for update in updates or []:
            message = (
                update.get("message")
                or update.get("channel_post")
                or update.get("my_chat_member")
                or {}
            )
            chat = message.get("chat")
            if not chat:
                continue
            thread_id = message.get("message_thread_id")
            key = (chat["id"], thread_id)
            seen[key] = DiscoveredChat(
                chat_id=chat["id"],
                chat_type=chat.get("type", "unknown"),
                title=chat.get("title") or chat.get("username") or chat.get("first_name") or "",
                thread_id=thread_id,
            )
        return list(seen.values())

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        disable_preview: bool = True,
        max_attempts: int = 3,
    ) -> int:
        """Send one HTML message. Returns the Telegram message_id.

        §6: HTML, not MarkdownV2 — issue titles are full of `.`, `-`, `(` and
        `!`, all of which MarkdownV2 requires escaping and will bite you.
        """
        if len(text) > TELEGRAM_MAX_CHARS:
            raise ValueError(
                f"message is {len(text)} chars, over Telegram's {TELEGRAM_MAX_CHARS} "
                f"limit; truncate the summary before formatting (§9)"
            )

        data: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true" if disable_preview else "false",
        }
        if thread_id is not None:
            data["message_thread_id"] = str(thread_id)

        for attempt in range(1, max_attempts + 1):
            try:
                result = await self._call("sendMessage", data)
                return int(result["message_id"])
            except TelegramRateLimited as exc:
                if attempt == max_attempts:
                    raise
                log.warning(
                    "telegram rate limited, sleeping exactly retry_after",
                    extra={"retry_after": exc.retry_after, "attempt": attempt},
                )
                await asyncio.sleep(exc.retry_after)
        raise TelegramError("unreachable")
