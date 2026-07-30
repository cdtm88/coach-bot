# Deploying it

The commands, in order, for a first deployment onto a Linux host with Docker.
`docs/setup.md` is the wider guide — accounts, credentials, what each variable
gates. This is the sequence.

Roughly 30 minutes, most of it waiting for the image to build.

> **The one thing that has never been tested.** Every check in this document has
> been run except the image build itself: the machine this was written on has the
> Docker CLI and no daemon. The compose file is validated, the shell script is
> syntax-checked, the healthcheck command has been executed, and 630 tests pass —
> but `docker build` has not run once. If step 3 fails it will be in the
> dependency install or one of the `COPY` lines, and neither is subtle.

## What you need first

* A Linux host with Docker and the compose plugin. Two cores and 2 GB is ample;
  the database is a few hundred megabytes after years.
* The credentials in `docs/setup.md` section 4. The stack refuses to start
  without six of them and names the one that is missing.
* A Cloudflare account, **only** when you want MacroLog to reach the coach. The
  stack runs complete and unreachable without it, which is the right first boot.

## 1. Clone and configure

```bash
git clone https://github.com/cdtm88/coach-bot.git
cd coach-bot
cp .env.example .env
```

Fill in `.env`. Six values have no default and `docker compose up` fails naming
whichever is missing:

| | |
| --- | --- |
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` |
| `COACH_TZ` | `Asia/Dubai` — an IANA name |
| `ANTHROPIC_API_KEY` | |
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | your numeric chat id (SEC-03's allowlist) |
| `MACRO_INGEST_SECRET` | `openssl rand -hex 32` |

Two things worth getting right the first time rather than debugging later.

**Use `hex`, not `base64`, for the two secrets.** base64 emits `/` and `+`, and a
`/` in the password breaks the connection URI — which surfaces as an
authentication failure that looks like a wrong password.

**Do not set `DATABASE_URL`.** Compose derives it from `POSTGRES_PASSWORD`, and
the two disagreeing is a confusing way to fail. It is in `.env.example` for the
case where you run a process outside compose, and that is all.

`COACH_TZ` is required by the compose file specifically because the code has a
default. Unset, `clock.configured_tz` falls back to UTC and **nothing errors** —
every day and week boundary is simply computed four hours out, and the nightly
pass reports success while windowing the wrong hours. Failing at `up` is cheaper
than finding that in a month of consolidated days.

## 2. Create the host directories

```bash
mkdir -p var/fit-archive var/fit-inbox backups
sudo chown -R 10001 var/fit-archive var/fit-inbox backups
```

The containers run as uid 10001 and bind-mount all three. Skip the `chown` and
the failure arrives at the first activity rather than at boot, which is the worst
time to learn about it.

`var/fit-archive` is worth a moment's thought. It is the permanent copy of every
file the system has ever seen and it is never pruned — that is the whole of
FIT-15, because disconnecting an upstream integration deletes that source's
activities *upstream* and this is what survives it. Put it on storage you back
up. `backups/` holds the nightly `pg_dump` and the markdown fact export, so the
same applies.

## 3. Build and start

```bash
docker compose build          # the step that has never been run; see the note above
docker compose up -d
docker compose ps
```

Six services. `migrate` runs once and exits with code 0 — that is success, not a
crash, and everything else waits for it:

| | |
| --- | --- |
| `postgres` | the memory. No published port, ever (SEC-05) |
| `migrate` | applies `migrations/` and exits |
| `agent` | the conversation: Telegram long poll in, one reply per backlog |
| `scheduler` | the nightly jobs at your local 03:00 |
| `ingest` | every inbound feed: two routes and six loops |
| `backup` | the nightly `pg_dump` at 03:30, after consolidation |

```bash
docker compose logs -f agent scheduler ingest
```

`ingest` is the only service with a healthcheck, because it is the only one with
a route to probe. A long poll that has stopped looks identical to one that is
waiting, so `agent` and `scheduler` have none — watch their logs instead.

## 4. Seed the memory

Read `seeds/athlete.json` before you run this, especially the constraints.
Anything wrong there propagates everywhere, and constraints cannot be corrected
automatically by design: only you can change one, and only by saying so. Every
entry carries a `reason` you can check against `docs/seed/`.

```bash
docker compose run --rm agent coach-seed --file /app/seeds/athlete.json
```

Re-running is safe. A value already current is left alone rather than superseded.

## 5. Say hello

Message the bot. If nothing comes back, in this order:

```bash
docker compose logs agent | tail -50
docker compose exec postgres psql -U coach -d coach -c \
  "select role, left(body, 60), occurred_at from messages order by id desc limit 5"
```

A message that reached the database and got no reply is a model or spend problem.
One that never arrived is the token or the allowlist — `TELEGRAM_ALLOWED_CHAT_ID`
refuses everything else on purpose (SEC-03), including you with the wrong id.

```bash
docker compose exec postgres psql -U coach -d coach -c \
  "select purpose, model, cost_usd, created_at from model_calls order by id desc limit 5"
```

## 6. The tunnel, when MacroLog needs it

Only now, and only if you want the two inbound routes reachable. Create a named
tunnel in the Cloudflare dashboard, point a hostname at `http://ingest:8080`, put
the token in `.env` as `CLOUDFLARE_TUNNEL_TOKEN`, then:

```bash
docker compose --profile tunnel up -d
curl https://coach.yourdomain/health     # {"ok": true}
```

The profile exists because `cloudflared` is the only service that makes anything
reachable from outside. Without it the stack is complete and unreachable.

Note what the tunnel points at: `ingest:8080`, over the compose network. The
ingest process binds `0.0.0.0` **inside its container** for exactly this reason —
`cloudflared` is a separate container with its own loopback, so a coach bound to
127.0.0.1 would be reachable by nothing at all. What keeps that narrow is the
absence of a `ports:` stanza: no published port means the compose network and
nowhere else. `COACH_INGEST_HOST` still defaults to loopback for anyone running a
process directly on a host.

## Verifying the backup, which is the part everyone skips

OBS-06 wants the nightly backup verified restorable, and an untested backup is a
hope. The `backup` service dumps once at startup so there is something to test
immediately.

```bash
ls -la backups/                       # coach-YYYY-MM-DD.dump, plus facts-*.md
docker compose logs backup
```

Restore it into a throwaway database on the same server:

```bash
docker compose exec postgres createdb -U coach restore_test
docker compose exec postgres pg_restore -U coach -d restore_test \
  /backups/coach-$(date +%F).dump
docker compose exec postgres psql -U coach -d restore_test -c \
  "select count(*) from facts where status = 'active'"
docker compose exec postgres dropdb -U coach restore_test
```

If the count matches the live database, the dump is real. Do this once now and
once a quarter; the dump is written to a `.partial` name and moved into place, so
a dump interrupted half way through is never mistaken for a complete one.

## Upgrading

```bash
git pull
docker compose build
docker compose up -d
```

Migrations apply on boot, so a new phase's schema lands as part of `up`. The
named volume keeps the data. `uv.lock` is committed, so the same commit produces
the same dependency set — a rebuild in six months installs what was tested, not
whatever is newest.

A major Postgres version bump is the one upgrade the named volume does not
survive on its own: dump first, bring up the new version on an empty volume,
restore. Do not change the image tag and hope.

## What is running that costs money

`agent` calls the model on every message. `scheduler` calls it once a night for
consolidation, on the heavier tier. OBS-07's hard stop is in at
`DAILY_SPEND_CAP_USD` (3.00 by default) and the coach says it is capped rather
than going quiet.

```bash
docker compose exec postgres psql -U coach -d coach -c \
  "select created_at::date as day, sum(cost_usd) from model_calls group by 1 order by 1 desc limit 14"
```

## What will not work yet, and why that is expected

Three things need real elapsed time before they produce anything, and none of it
can be backfilled — which is the argument for deploying before the remaining
phases rather than after:

* **Recovery deviation** needs 28 days of your own wellness history before it is
  usable at all. It is standardised against your own trailing baseline, not
  against a population.
* **Observed availability** needs about two weeks of calendar data before a
  weekday pattern is a pattern rather than a Tuesday.
* **The body mass trend** needs 28 days of readings, and nothing writes body mass
  until MacroLog's HealthBridge does. The wellness feed carries no weight at all;
  that was verified against the live account on 28 July 2026.

Two more are not about time:

* **No generator emits interval step lists yet**, so every session publishes with
  duration and purpose rather than as structured intervals. That is correct for
  steady endurance and gym work and is a BLOCK change, not a deployment one.
* **The weekly review and the daily nudges are P10** and not built. The coach
  reacts; it does not yet have a rhythm.
