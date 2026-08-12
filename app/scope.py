"""Show what the bot is actually following.

    docker compose exec github-notify python -m app.scope
    docker compose exec github-notify python -m app.scope --watching

Two different questions, both worth answering:

  1. What the config permits — reasons, subject types, repo allow/block.
  2. What is actually arriving from GitHub right now, per repo, and whether
     each of those would be forwarded under the current filters.

The bot does not subscribe to anything itself. The stream is driven by GitHub's
own notification rules (what you're mentioned in, assigned to, participating
in), and the config only ever narrows that. So (2) is the honest answer to
"what is it following", and (1) explains why anything in it is being dropped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter, defaultdict

from . import filters, github
from .config import KNOWN_REASONS, Account, Config, ConfigError, load_config
from .github import GitHubClient, Notification

BULLET = "  "


def _fmt_list(values, empty: str) -> str:
    return ", ".join(sorted(values)) if values else empty


def print_config_scope(account: Account, config: Config) -> None:
    enabled = set(account.reasons)
    disabled = KNOWN_REASONS - enabled

    print(f"account: {account.name}  ({account.api_base})")
    print()
    print("filters")
    print(f"{BULLET}reasons on      : {_fmt_list(enabled, '(none)')}")
    print(f"{BULLET}reasons off     : {_fmt_list(disabled, '(none)')}")
    if "subscribed" in disabled:
        print(
            f"{BULLET}                  `subscribed` off is deliberate (§5): it fires "
            f"for every comment"
        )
        print(
            f"{BULLET}                  on every watched repo, including threads you "
            f"never touched."
        )
    subject = (
        "all" if account.subject_types == "all" else _fmt_list(account.subject_types, "(none)")
    )
    print(f"{BULLET}subject types   : {subject}")
    print(f"{BULLET}ci filter       : {account.ci_filter}")
    print(f"{BULLET}include PRs     : {config.behaviour.include_prs}")
    print(
        f"{BULLET}repos allow     : "
        f"{_fmt_list(account.repos.allow, '(empty — every repo the stream carries)')}"
    )
    print(f"{BULLET}repos block     : {_fmt_list(account.repos.block, '(none)')}")
    dest = account.destination
    thread = f", thread {dest.thread_id}" if dest.thread_id else ""
    print(f"{BULLET}destination     : chat {dest.chat_id}{thread}")
    print(f"{BULLET}prefix tiers    : {' → '.join(account.prefix.strategy)}")
    print(
        f"{BULLET}label map       : "
        f"{len(account.prefix.label_map)} entr{'y' if len(account.prefix.label_map) == 1 else 'ies'}"
        f"{' — tier 1 will never fire until populated (§6)' if not account.prefix.label_map else ''}"
    )


def print_live_stream(account: Account, config: Config, notes: list[Notification]) -> None:
    print()
    print(f"live notification stream ({len(notes)} unread)")
    if not notes:
        print(f"{BULLET}(nothing unread right now — this says nothing about the")
        print(f"{BULLET} bot's health, only that your GitHub inbox is clear)")
        return

    per_repo: dict[str, list[Notification]] = defaultdict(list)
    for note in notes:
        per_repo[note.repo_full_name].append(note)

    width = max(len(name) for name in per_repo)
    print()
    print(f"{BULLET}{'REPO'.ljust(width)}   N  FORWARDED  REASONS")
    forwarded_total = 0
    for name in sorted(per_repo):
        group = per_repo[name]
        kept = [n for n in group if filters.drop_reason(account, config, n) is None]
        forwarded_total += len(kept)
        reasons = Counter(n.reason for n in group)
        reason_text = ", ".join(f"{r} {c}" for r, c in reasons.most_common())
        print(
            f"{BULLET}{name.ljust(width)}  {len(group):>2}  "
            f"{f'{len(kept)}/{len(group)}':>9}  {reason_text}"
        )

    print()
    print(f"{BULLET}{forwarded_total} of {len(notes)} would be forwarded under current filters")

    dropped = Counter(
        reason for n in notes if (reason := filters.drop_reason(account, config, n)) is not None
    )
    if dropped:
        print(f"{BULLET}dropped: " + ", ".join(f"{filters.humanise(r)} ({c})" for r, c in dropped.most_common()))

    types = Counter(n.subject_type for n in notes)
    print(f"{BULLET}subject types in stream: " + ", ".join(f"{t} {c}" for t, c in types.most_common()))


async def run(args: argparse.Namespace) -> int:
    try:
        config = load_config(require_telegram=False)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    for index, account in enumerate(config.accounts):
        if index:
            print("\n" + "-" * 60 + "\n")

        client = GitHubClient(account.api_base, account.token)
        try:
            identity = await client.probe()
            print(f"github login: {identity.login}")
            print_config_scope(account, config)

            result = await client.poll_notifications()
            print_live_stream(account, config, result.notifications)

            if args.watching:
                watched = await client.list_watched_repos()
                print()
                print(f"watched on GitHub ({len(watched)})")
                if not watched:
                    print(f"{BULLET}(none)")
                for name in sorted(watched):
                    print(f"{BULLET}{name}")
                print()
                print(
                    f"{BULLET}Watching alone does not notify here: it produces the "
                    f"`subscribed`"
                )
                print(
                    f"{BULLET}reason, which is off. These repos only reach you when "
                    f"you're"
                )
                print(f"{BULLET}mentioned, assigned, reviewing, or already participating.")

        except github.GitHubError as exc:
            print(
                f"github error for account {account.name}: {exc}",
                file=sys.stderr,
            )
            return 1
        finally:
            await client.aclose()

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.scope",
        description="show which repos the bot is following and why",
    )
    parser.add_argument(
        "--watching",
        action="store_true",
        help="also list repos watched on GitHub (GET /user/subscriptions)",
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
