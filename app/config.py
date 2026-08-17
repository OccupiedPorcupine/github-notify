"""Config loading: structure from config.yaml, secrets from the environment (§7)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .logging_setup import register_secret


class ConfigError(Exception):
    """Raised for a config problem that should stop startup loudly (§9)."""


DEFAULT_REASONS = [
    "mention",
    "assign",
    "review_requested",
    "team_mention",
    "author",
    "comment",
    "state_change",
    "manual",
    "security_alert",
    "invitation",
    "ci_activity",
]

# Every reason GitHub currently documents. Anything outside this set in config
# is almost certainly a typo, and a typo'd reason silently filters everything.
KNOWN_REASONS = set(DEFAULT_REASONS) | {"subscribed"}


@dataclass(frozen=True)
class Destination:
    chat_id: int | None = None
    thread_id: int | None = None


@dataclass(frozen=True)
class RepoFilter:
    allow: list[str] = field(default_factory=list)
    block: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PrefixConfig:
    strategy: list[str] = field(
        default_factory=lambda: ["label", "conventional", "issue_type", "type_default"]
    )
    include_scope: bool = False
    strip_from_title: bool = True
    bracket_fallback: bool = False
    label_map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Account:
    name: str
    enabled: bool
    api_base: str
    token_env: str
    token: str
    destination: Destination
    reasons: frozenset[str]
    subject_types: str | frozenset[str]
    ci_filter: str
    ci_destination: Destination
    repos: RepoFilter
    prefix: PrefixConfig

    def accepts_subject_type(self, subject_type: str) -> bool:
        if self.subject_types == "all":
            return True
        return subject_type in self.subject_types

    def accepts_repo(self, full_name: str) -> bool:
        lowered = full_name.lower()
        if self.repos.allow and lowered not in {r.lower() for r in self.repos.allow}:
            return False
        return lowered not in {r.lower() for r in self.repos.block}


@dataclass(frozen=True)
class Behaviour:
    timezone: str = "Asia/Singapore"
    include_prs: bool = True
    mark_as_read: bool = False
    backfill_max_hours: int = 12
    coalesce_window_seconds: int = 90
    seen_retention_days: int = 30


@dataclass(frozen=True)
class TelegramConfig:
    bot_token_env: str
    bot_token: str | None
    allowed_chat_ids: frozenset[int]


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    accounts: list[Account]
    behaviour: Behaviour


def _require_env(name: str, what: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{what} is empty: environment variable {name} is unset or blank. "
            f"Set it in .env (mode 600) and restart."
        )
    register_secret(value)
    return value


def _destination(raw: Any) -> Destination:
    raw = raw or {}
    return Destination(chat_id=raw.get("chat_id"), thread_id=raw.get("thread_id"))


def _account(raw: dict[str, Any]) -> Account:
    name = raw.get("name")
    if not name:
        raise ConfigError("an account entry has no `name`")

    token_env = raw.get("token_env")
    if not token_env:
        raise ConfigError(f"account {name!r} has no `token_env`")

    reasons = raw.get("reasons") or DEFAULT_REASONS
    unknown = set(reasons) - KNOWN_REASONS
    if unknown:
        raise ConfigError(
            f"account {name!r} lists unrecognised reason(s) {sorted(unknown)}. "
            f"Valid values: {sorted(KNOWN_REASONS)}"
        )

    ci_filter = raw.get("ci_filter", "failures_only")
    if ci_filter not in {"failures_only", "all", "off"}:
        raise ConfigError(
            f"account {name!r}: ci_filter must be failures_only|all|off, got {ci_filter!r}"
        )

    subject_types_raw = raw.get("subject_types", "all")
    subject_types: str | frozenset[str]
    if subject_types_raw == "all":
        subject_types = "all"
    elif isinstance(subject_types_raw, list):
        subject_types = frozenset(subject_types_raw)
    else:
        raise ConfigError(
            f"account {name!r}: subject_types must be 'all' or a list, got {subject_types_raw!r}"
        )

    repos_raw = raw.get("repos") or {}
    prefix_raw = raw.get("prefix") or {}

    return Account(
        name=name,
        enabled=bool(raw.get("enabled", True)),
        api_base=(raw.get("api_base") or "https://api.github.com").rstrip("/"),
        token_env=token_env,
        token=_require_env(token_env, f"GitHub token for account {name!r}"),
        destination=_destination(raw.get("destination")),
        reasons=frozenset(reasons),
        subject_types=subject_types,
        ci_filter=ci_filter,
        ci_destination=_destination(raw.get("ci_destination")),
        repos=RepoFilter(
            allow=list(repos_raw.get("allow") or []),
            block=list(repos_raw.get("block") or []),
        ),
        prefix=PrefixConfig(
            strategy=list(
                prefix_raw.get("strategy")
                or ["label", "conventional", "issue_type", "type_default"]
            ),
            include_scope=bool(prefix_raw.get("include_scope", False)),
            strip_from_title=bool(prefix_raw.get("strip_from_title", True)),
            bracket_fallback=bool(prefix_raw.get("bracket_fallback", False)),
            label_map=dict(prefix_raw.get("label_map") or {}),
        ),
    )


def load_config(path: str | None = None, *, require_telegram: bool = False) -> Config:
    """Load and validate config.

    `require_telegram` stays False through step 1 (log-only, nothing is sent).
    Flip it on at step 3 so a missing chat allowlist fails startup rather than
    quietly sending nowhere — or worse, anywhere (§11.1).
    """
    path = path or os.environ.get("GHN_CONFIG", "config.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
    except FileNotFoundError:
        raise ConfigError(f"config file not found at {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"config file at {path} is not valid YAML: {exc}") from None

    tg_raw = raw.get("telegram") or {}
    tg_env = tg_raw.get("bot_token_env", "TELEGRAM_BOT_TOKEN")
    allowed = frozenset(int(c) for c in (tg_raw.get("allowed_chat_ids") or []))

    if require_telegram:
        bot_token: str | None = _require_env(tg_env, "Telegram bot token")
        if not allowed:
            raise ConfigError(
                "telegram.allowed_chat_ids is empty. This is security critical "
                "(§11.1): without it, anyone who finds the bot can talk to it. "
                "Add your chat id before enabling sending."
            )
    else:
        bot_token = os.environ.get(tg_env) or None
        register_secret(bot_token)

    accounts_raw = raw.get("accounts") or []
    if not accounts_raw:
        raise ConfigError("config has no `accounts`")

    accounts = [_account(a) for a in accounts_raw if a.get("enabled", True)]
    if not accounts:
        raise ConfigError("every account in config is disabled; nothing to poll")

    names = [a.name for a in accounts]
    if len(set(names)) != len(names):
        raise ConfigError(f"duplicate account names: {names}")

    if require_telegram:
        for account in accounts:
            if account.destination.chat_id is None:
                raise ConfigError(
                    f"account {account.name!r} has no destination.chat_id, so its "
                    f"notifications have nowhere to go. Run "
                    f"`python -m app.ping --discover` to find your chat id."
                )
            # A typo here would send private work issue content to a stranger,
            # so the destination must be on the allowlist, not merely valid.
            if account.destination.chat_id not in allowed:
                raise ConfigError(
                    f"account {account.name!r} sends to chat "
                    f"{account.destination.chat_id}, which is not in "
                    f"telegram.allowed_chat_ids {sorted(allowed)}. Add it, or fix "
                    f"the destination."
                )
            ci_chat = account.ci_destination.chat_id
            if ci_chat is not None and ci_chat not in allowed:
                raise ConfigError(
                    f"account {account.name!r} routes CI to chat {ci_chat}, which "
                    f"is not in telegram.allowed_chat_ids {sorted(allowed)}."
                )

    behaviour_raw = raw.get("behaviour") or {}
    behaviour = Behaviour(
        timezone=behaviour_raw.get("timezone", "Asia/Singapore"),
        include_prs=bool(behaviour_raw.get("include_prs", True)),
        mark_as_read=bool(behaviour_raw.get("mark_as_read", False)),
        backfill_max_hours=int(behaviour_raw.get("backfill_max_hours", 12)),
        coalesce_window_seconds=int(behaviour_raw.get("coalesce_window_seconds", 90)),
        seen_retention_days=int(behaviour_raw.get("seen_retention_days", 30)),
    )

    if behaviour.mark_as_read:
        raise ConfigError(
            "behaviour.mark_as_read is true, but D3 resolved this to never. "
            "The bot holds a read-only token by design; marking read would need "
            "a write scope. Set it to false."
        )

    return Config(
        telegram=TelegramConfig(
            bot_token_env=tg_env, bot_token=bot_token, allowed_chat_ids=allowed
        ),
        accounts=accounts,
        behaviour=behaviour,
    )
