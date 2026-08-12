"""GitHub Notifications API client (§4, §9)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("github")

API_VERSION = "2022-11-28"
USER_AGENT = "github-notify/0.1 (+homelab)"

# Scopes a classic PAT needs. `repo` is only required to read private issue and
# comment bodies — without it the bot still works, title-only (§0.1).
REQUIRED_SCOPES = {"notifications"}
RECOMMENDED_SCOPES = {"repo"}


class GitHubError(Exception):
    """Base for GitHub API failures."""


class AuthError(GitHubError):
    """401. Token wrong, expired or revoked. Alert once, then back off (§9)."""


class SSOError(GitHubError):
    """403 carrying X-GitHub-SSO. Self-service fix, so say so (§12 D6)."""

    def __init__(self, message: str, authorize_url: str | None = None) -> None:
        super().__init__(message)
        self.authorize_url = authorize_url


class ScopeError(GitHubError):
    """Token is valid but lacks a scope the bot cannot work without."""


class RateLimited(GitHubError):
    """Primary or secondary rate limit. `retry_after` is seconds."""

    def __init__(self, message: str, retry_after: float) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientError(GitHubError):
    """5xx, timeout, connection reset. Retry with backoff."""


@dataclass(frozen=True)
class Notification:
    """One notification thread, flattened to what the pipeline actually uses."""

    thread_id: str
    reason: str
    updated_at: str
    repo_full_name: str
    subject_type: str
    subject_title: str
    subject_url: str | None
    latest_comment_url: str | None
    raw: dict[str, Any]

    @property
    def number(self) -> int | None:
        """Issue/PR number, derived from subject.url.

        §4: the notification payload carries neither the number nor an HTML
        URL, so both have to be reconstructed.
        """
        if not self.subject_url:
            return None
        match = re.search(r"/(?:issues|pulls)/(\d+)$", self.subject_url)
        return int(match.group(1)) if match else None

    @property
    def latest_comment_id(self) -> str | None:
        if not self.latest_comment_url:
            return None
        match = re.search(r"/comments/(\d+)$", self.latest_comment_url)
        return match.group(1) if match else None

    @property
    def dedupe_key(self) -> str:
        """§9: thread id alone is not enough — updated_at moves on every event."""
        return self.latest_comment_id or self.updated_at

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "Notification":
        subject = payload.get("subject") or {}
        repository = payload.get("repository") or {}
        return cls(
            thread_id=str(payload.get("id")),
            reason=payload.get("reason") or "unknown",
            updated_at=payload.get("updated_at") or "",
            repo_full_name=repository.get("full_name") or "unknown/unknown",
            subject_type=subject.get("type") or "Unknown",
            subject_title=subject.get("title") or "",
            subject_url=subject.get("url"),
            latest_comment_url=subject.get("latest_comment_url"),
            raw=payload,
        )


@dataclass
class PollResult:
    notifications: list[Notification]
    poll_interval: int
    last_modified: str | None
    not_modified: bool


@dataclass(frozen=True)
class Identity:
    login: str
    scopes: frozenset[str]
    rate_limit: int | None


class GitHubClient:
    def __init__(self, api_base: str, token: str, *, timeout: float = 30.0) -> None:
        self.api_base = api_base.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- error classification ---------------------------------------------

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return

        if status == 401:
            raise AuthError(
                "GitHub rejected the token (401 Bad credentials). It is wrong, "
                "expired, or revoked. Generate a new classic PAT and update .env."
            )

        if status == 403:
            sso_header = response.headers.get("X-GitHub-SSO")
            if sso_header:
                match = re.search(r"url=(\S+)", sso_header)
                url = match.group(1).rstrip(";,") if match else None
                raise SSOError(
                    "The token needs SAML SSO authorisation for this org. This is "
                    "normally self-service: open the URL below and authorise the "
                    "token. Until then the org's notifications are silently "
                    "omitted rather than erroring (§12 D6).",
                    authorize_url=url,
                )
            if response.headers.get("X-RateLimit-Remaining") == "0":
                raise RateLimited(
                    "GitHub primary rate limit exhausted.",
                    _retry_after_seconds(response, default=60.0),
                )
            body = (response.text or "")[:300]
            if "secondary rate limit" in body.lower():
                raise RateLimited(
                    "GitHub secondary rate limit.",
                    _retry_after_seconds(response, default=60.0),
                )
            raise GitHubError(
                f"GitHub returned 403 and it is not an SSO or rate-limit case. "
                f"The org may block personal access tokens, which is not "
                f"self-service. Body: {body}"
            )

        if status == 429:
            raise RateLimited(
                "GitHub returned 429.", _retry_after_seconds(response, default=60.0)
            )

        if status >= 500:
            raise TransientError(f"GitHub returned {status}.")

        raise GitHubError(f"GitHub returned {status}: {(response.text or '')[:300]}")

    # ---- calls -------------------------------------------------------------

    async def probe(self) -> Identity:
        """Startup check (§9): is the token valid, and does it carry the scopes?

        This is the same ground the spec's preflight covers, run every boot so a
        rotated or downgraded token fails loudly at startup instead of looking
        like an empty notification stream.
        """
        try:
            response = await self._client.get(f"{self.api_base}/user")
        except httpx.HTTPError as exc:
            raise TransientError(f"could not reach {self.api_base}: {exc}") from exc

        self._raise_for_status(response)

        raw_scopes = response.headers.get("X-OAuth-Scopes", "")
        scopes = frozenset(s.strip() for s in raw_scopes.split(",") if s.strip())
        rate_limit = response.headers.get("X-RateLimit-Limit")

        # A fine-grained PAT sends no X-OAuth-Scopes header at all. Absence is
        # therefore not proof of a missing scope — only an explicit list is.
        if raw_scopes and not REQUIRED_SCOPES <= scopes:
            missing = sorted(REQUIRED_SCOPES - scopes)
            raise ScopeError(
                f"token is missing required scope(s) {missing}. It has "
                f"{sorted(scopes)}. Regenerate the classic PAT with "
                f"`notifications` and `repo` ticked (§0.1)."
            )

        return Identity(
            login=(response.json() or {}).get("login", "unknown"),
            scopes=scopes,
            rate_limit=int(rate_limit) if rate_limit and rate_limit.isdigit() else None,
        )

    async def poll_notifications(
        self, *, since: str | None = None, last_modified: str | None = None
    ) -> PollResult:
        params: dict[str, str] = {"all": "false", "per_page": "50"}
        if since:
            params["since"] = since

        headers = {}
        if last_modified:
            # §4: a 304 does not count against the rate limit.
            headers["If-Modified-Since"] = last_modified

        try:
            response = await self._client.get(
                f"{self.api_base}/notifications", params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            raise TransientError(f"notifications request failed: {exc}") from exc

        poll_interval = _int_header(response, "X-Poll-Interval", default=60)

        if response.status_code == 304:
            return PollResult([], poll_interval, last_modified, not_modified=True)

        self._raise_for_status(response)

        payload = response.json() or []
        return PollResult(
            notifications=[Notification.from_payload(item) for item in payload],
            poll_interval=poll_interval,
            last_modified=response.headers.get("Last-Modified", last_modified),
            not_modified=False,
        )

    async def list_watched_repos(self, *, max_pages: int = 10) -> list[str]:
        """Repos the account watches (GET /user/subscriptions).

        This is GitHub's own "watching" list, which is a different thing from
        what the bot forwards: watching produces the `subscribed` reason, and
        §5 turns that off. Shown by `--watching` so the distinction is visible
        rather than assumed.
        """
        names: list[str] = []
        for page in range(1, max_pages + 1):
            try:
                response = await self._client.get(
                    f"{self.api_base}/user/subscriptions",
                    params={"per_page": "100", "page": str(page)},
                )
            except httpx.HTTPError as exc:
                raise TransientError(f"subscriptions request failed: {exc}") from exc
            self._raise_for_status(response)
            batch = response.json() or []
            if not batch:
                break
            names.extend(repo.get("full_name", "?") for repo in batch)
            if len(batch) < 100:
                break
        return names


def _int_header(response: httpx.Response, name: str, *, default: int) -> int:
    raw = response.headers.get(name)
    if raw and raw.isdigit():
        return int(raw)
    return default


def _retry_after_seconds(response: httpx.Response, *, default: float) -> float:
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    reset = response.headers.get("X-RateLimit-Reset")
    if reset and reset.isdigit():
        import time

        return max(1.0, float(reset) - time.time())
    return default
