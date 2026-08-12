"""Manual ping — send a test message end to end, without waiting for GitHub.

    docker compose exec github-notify python -m app.ping --discover
    docker compose exec github-notify python -m app.ping
    docker compose exec github-notify python -m app.ping --chat-id 123456789

The sample message is deliberately shaped like a real §6 notification and
carries the characters that break things: `&`, `<`, `>`, a full-width middot,
CJK text, and a link. If it arrives with the title as a live hyperlink and no
stray `&amp;` visible, the formatter's assumptions hold (§0.4).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import ConfigError, load_config
from .format import escape_html
from .telegram import (
    TelegramClient,
    TelegramError,
    TelegramForbidden,
)

# Mirrors the §6 layout: linked prefix + number, repo, actor line, summary,
# reason footer. Every interpolated value goes through escape_html.
SAMPLE_URL = "https://github.com/"
SAMPLE_TITLE_TEXT = "PING #0"
SAMPLE_REPO = "owner/repo"
SAMPLE_ACTOR = "github-notify"
SAMPLE_BODY = (
    "Manual ping from the homelab LXC. If this line is intact and the header "
    "above is a link, HTML parse mode is working. Escaping check: A & B "
    "<not-a-tag>. CJK check: 销售单据生命周期过账设计与实现。"
)


def build_sample() -> str:
    return (
        f'<a href="{SAMPLE_URL}">{escape_html(SAMPLE_TITLE_TEXT)}</a>'
        f" · {escape_html(SAMPLE_REPO)}\n"
        f"{escape_html(SAMPLE_ACTOR)} sent a test\n"
        f"{escape_html(SAMPLE_BODY)}\n"
        f"manual"
    )


def resolve_chat_id(config, override: int | None) -> tuple[int | None, int | None]:
    """Pick a target chat, preferring an explicit override. Returns (chat, thread)."""
    if override is not None:
        return override, None
    account = config.accounts[0]
    return account.destination.chat_id, account.destination.thread_id


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(require_telegram=False)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if not config.telegram.bot_token:
        print(
            f"{config.telegram.bot_token_env} is unset or blank in .env",
            file=sys.stderr,
        )
        return 2

    client = TelegramClient(config.telegram.bot_token)
    try:
        identity = await client.get_me()
        print(f"bot: @{identity.username} (id {identity.id})")

        if args.discover:
            chats = await client.discover_chats()
            if not chats:
                print(
                    "no pending updates. Open the chat with the bot, press Start "
                    "or send any message, then run this again (§0.3)."
                )
                return 1
            print("\nchats that have messaged this bot:")
            for chat in chats:
                thread = f", thread_id {chat.thread_id}" if chat.thread_id else ""
                print(
                    f"  chat_id {chat.chat_id}  ({chat.chat_type}{thread})"
                    f"  {chat.title}"
                )
            print(
                "\nPut the id in config.yaml under accounts[].destination.chat_id "
                "and telegram.allowed_chat_ids."
            )
            return 0

        chat_id, thread_id = resolve_chat_id(config, args.chat_id)
        if chat_id is None:
            print(
                "no chat id: destination.chat_id is unset in config.yaml and "
                "--chat-id was not given. Run with --discover to find it.",
                file=sys.stderr,
            )
            return 2

        allowed = config.telegram.allowed_chat_ids
        if allowed and chat_id not in allowed:
            # §11.1 cuts both ways: never send to a chat outside the allowlist.
            print(
                f"refusing to send: chat_id {chat_id} is not in "
                f"telegram.allowed_chat_ids {sorted(allowed)}",
                file=sys.stderr,
            )
            return 2
        if not allowed:
            print(
                "warning: telegram.allowed_chat_ids is empty. Fine for a ping, "
                "but §11.1 requires it before the bot starts sending for real."
            )

        text = args.text or build_sample()
        message_id = await client.send_message(chat_id, text, thread_id=thread_id)
        print(f"sent to chat {chat_id}: message_id {message_id}")
        print(
            "Check Telegram: the header should be a live link, and the body "
            "should show `A & B <not-a-tag>` literally, not as markup."
        )
        return 0

    except TelegramForbidden as exc:
        print(f"{exc}", file=sys.stderr)
        return 1
    except TelegramError as exc:
        print(f"telegram error: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.ping", description="send a manual test message"
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="list chats that have messaged the bot, then exit without sending",
    )
    parser.add_argument(
        "--chat-id", type=int, default=None, help="override the configured chat id"
    )
    parser.add_argument("--text", default=None, help="send this text instead of the sample")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
