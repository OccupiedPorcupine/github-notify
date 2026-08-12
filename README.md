# github-notify

Pushes GitHub notifications that involve you personally — mentions, assignments,
review requests, replies in threads you're in — to a Telegram chat, within about
a minute.

It is a push channel, not an inbox manager. It never marks anything as read, so
your GitHub notification inbox stays fully usable in parallel.

> **Status: build step 1 of 6.** The poller, state, filtering and the Telegram
> command listener work. Message formatting and forwarding are not wired up yet:
> notifications are currently logged as `would_send` records rather than
> delivered. See [Build order](#build-order).

## Why polling, not webhooks

Webhooks need repo or org admin rights and a publicly reachable endpoint. On a
work account you probably have neither. The Notifications API already encodes
"things that concern me", which is the filter you actually want, and a personal
access token is enough.

## How it works

```
Poller      one worker per account; GET /notifications, honours X-Poll-Interval
   ↓
Filter      drop by reason, drop muted repos, drop self-authored
   ↓
Dedupe      SQLite, keyed on (thread_id, latest_comment_id)
   ↓
Enricher    fetch the triggering comment and issue body        [not yet built]
   ↓
Formatter   build the HTML message, resolve prefix, truncate   [not yet built]
   ↓
Sender      outbox, 429 backoff, per-chat rate limit           [not yet built]
```

Single async process. SQLite for state. No broker.

A few details that matter in practice:

- **Conditional requests.** `If-Modified-Since` is sent with the previous
  `Last-Modified`; a `304` costs nothing against the rate limit.
- **The cursor is GitHub's clock, not yours.** Avoids skew.
- **Cold start never backfills.** First run sets the cursor to now. If the bot
  was down longer than `backfill_max_hours`, it skips the gap and says so rather
  than replaying days of history.
- **Dedupe is per event, not per thread.** A thread's `updated_at` changes on
  every event, so thread id alone would re-send endlessly.

## Setup

Requires Docker and a Telegram bot token.

**1. GitHub token.** A classic PAT from <https://github.com/settings/tokens>
with exactly two scopes:

- `notifications` — read the notification stream
- `repo` — read issue and comment bodies in private repos; without it those
  arrive title-only

The bot never writes, so no write scope is needed. If your org enforces SAML
SSO, use **Configure SSO → Authorize** on the token afterwards. Without that the
token looks valid but silently returns nothing from that org.

**2. Telegram bot.** Talk to [@BotFather](https://t.me/BotFather), send
`/newbot`, keep the token.

**3. Configure.**

```bash
cp .env.example .env && chmod 600 .env   # fill in the two tokens
cp config.example.yaml config.yaml
docker compose up -d --build
```

**4. Find your chat id.** Bots cannot message you first, so open the chat with
your bot and press Start, then:

```bash
docker compose exec github-notify python -m app.ping --discover
```

Put the id into `config.yaml` under both `allowed_chat_ids` and
`destination.chat_id`, then restart. Verify delivery end to end:

```bash
docker compose exec github-notify python -m app.ping
```

## Telegram commands

| Command | Shows |
|---|---|
| `/repos` | Which repos are feeding the bot, and how many would forward |
| `/scope` | Effective filters — reasons on/off, allow/block, prefix tiers |
| `/status` | Last poll, cursor, errors, events seen today |
| `/help` | The above |

Same views from a shell, with more detail:

```bash
docker compose exec github-notify python -m app.scope --watching
```

## Filtering

Fires on `mention`, `assign`, `review_requested`, `team_mention`, `author`,
`comment`, `state_change`, `manual`, `security_alert`, `invitation`, and
`ci_activity` (failures only by default).

`subscribed` is **off**. It fires for every comment on every thread in every
repo you watch, including threads you have never touched — on a work org where
you're auto-subscribed to everything you have write access to, that is hundreds
of messages a day, and it will bury the mentions you care about. Turn it on for
a single repo via `repos.allow` first if you want it.

## Security

- **The chat allowlist is mandatory.** Anyone who finds your bot's username can
  message it. Updates from chats outside `allowed_chat_ids` are dropped without
  a reply, and the command listener refuses to start while the list is empty.
- **Telegram is not end-to-end encrypted in regular chats.** Private issue
  bodies will sit on Telegram's servers. If that's a problem, keep the summary
  to title-only so just the issue number, repo name and link leave your network,
  and consider whether the repo name itself is sensitive.
- Tokens live in `.env`, mode `600`, never in the config or in git.
- Outbound HTTPS only. Nothing listens.

## Build order

0. ~~Preflight the token against the work org~~ (folded into the startup probe)
1. ~~Poller, SQLite state, log-only~~ ✅
2. Filter and dedupe verified against a real stream for a day, drop
   self-authored events
3. Formatter for `Issue` and `PullRequest`, start sending
4. Outbox, retries, rate limiting
5. Remaining subject types
6. Multi-account, config reload

## Notes

Comments in the source cite section numbers (`§5`, `D3`) from a design document
that isn't part of this repo. They're kept because they record *why* a rule
exists, which is usually the thing that gets lost.
