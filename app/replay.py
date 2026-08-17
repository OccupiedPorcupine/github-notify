"""Preview or re-send messages for notifications currently in your inbox.

    python -m app.replay                # print what the messages would look like
    python -m app.replay --send         # actually send them
    python -m app.replay --limit 3

Dry run by default, because the point of this tool is to look at the formatting
before anyone's phone buzzes.

It ignores the `seen` table on purpose, so it can re-render notifications the
poller already handled — which is what makes it useful for testing the
formatter against real content rather than waiting for new activity.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import enrich, filters, formatter, github
from .config import ConfigError, load_config
from .db import Database
from .github import GitHubClient
from .telegram import TelegramClient, TelegramError


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(require_telegram=args.send)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    db = Database() if args.mark_seen else None
    sender = TelegramClient(config.telegram.bot_token or "") if args.send else None
    exit_code = 0

    for account in config.accounts:
        client = GitHubClient(account.api_base, account.token)
        try:
            identity = await client.probe()
            result = await client.poll_notifications()

            candidates = [
                note
                for note in result.notifications
                if filters.drop_reason(account, config, note) is None
            ]
            if args.limit:
                candidates = candidates[: args.limit]

            print(
                f"account {account.name}: {len(result.notifications)} unread, "
                f"{len(candidates)} pass the filters"
                f"{' — SENDING' if args.send else ' (dry run)'}\n"
            )

            for note in candidates:
                enriched = await enrich.enrich(client, note)

                if enrich.is_self_authored(enriched, identity.login):
                    print(f"--- #{note.number} skipped: your own comment\n")
                    continue

                message, tier = formatter.build_message(
                    account, config.behaviour, note, enriched
                )
                print(f"--- #{note.number}  reason={note.reason}  prefix_tier={tier}")
                print(message)
                print()

                if sender:
                    try:
                        message_id = await sender.send_message(
                            account.destination.chat_id,
                            message,
                            thread_id=account.destination.thread_id,
                        )
                        print(f"    sent, message_id {message_id}\n")
                        if db:
                            db.mark_seen(
                                account.name,
                                note.thread_id,
                                note.dedupe_key,
                                tg_msg_id=message_id,
                            )
                    except TelegramError as exc:
                        print(f"    SEND FAILED: {exc}\n", file=sys.stderr)
                        exit_code = 1

        except github.GitHubError as exc:
            print(f"github error on {account.name}: {exc}", file=sys.stderr)
            exit_code = 1
        finally:
            await client.aclose()

    if sender:
        await sender.aclose()
    if db:
        db.close()
    return exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.replay",
        description="preview or re-send messages for current notifications",
    )
    parser.add_argument(
        "--send", action="store_true", help="actually send (default is a dry run)"
    )
    parser.add_argument(
        "--mark-seen",
        action="store_true",
        help="with --send, record these as delivered so the poller skips them",
    )
    parser.add_argument("--limit", type=int, default=0, help="only the first N")
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
