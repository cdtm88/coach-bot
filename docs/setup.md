# AI Cycling Coach: Setup Guide

> Read with `docs/prd.md` and `docs/memory-design.md`.
> On conflict: this document wins on credentials and infrastructure.


Everything you need to do by hand before and around the build, in order, with what breaks and how you would know.

Claude Code writes the code. This document covers the accounts, credentials and infrastructure only you can create.

## 1. Division of labour

Who does what

| Area | You | Claude Code |
| --- | --- | --- |
| Accounts and credentials | Create everything, paste into .env | Reads from environment, never sees the values in chat |
| MacroLog, including HealthBridge | All of it; it is a separate app on the phone | Nothing. It is outside this repository and outside the phase plan |
| Homelab infrastructure | Docker host, tunnel, DNS name, backups | Writes the compose file and Dockerfiles |
| Database | Nothing; it runs in the stack | Schema, migrations, seed scripts |
| Application | Direct and review | All of it |
| Seed content | Approve the persona and baseline facts | Extracts and loads them |
| Verification | Read the first week of output critically | Recall suite, linter, tests |

## 2. Before anything else

**Settled, and simpler than planned.** With Zwift and Whoop both connected to intervals.icu, activities and recovery arrive through one API with key based authentication. No Whoop OAuth app, and outdoor rides work the same as indoor ones. The local FIT folder is still ingested as a first class path and kept permanently, because disconnecting an upstream integration causes that source data to be deleted upstream.

### Prerequisites checklist

* Homelab host with Docker and Docker Compose, always on.
* A domain you control for the tunnel hostname.
* intervals.icu account with Zwift and Whoop connected, which you have. Note your athlete id and API key from the settings page.
* Google account holding the calendars the coach should read, including the one your golf goes in.
* MacroLog on the phone, with HealthBridge for body mass.
* Anthropic API key with billing enabled.
* Somewhere to store secrets that is not the repository.

## 3. Setup steps in order

### Step 1: Directory and secrets

Roughly 15 minutes

* Create the project directory on the homelab with subfolders for the compose stack, migrations, and the watched FIT folder.
* Create a `.env` file and add it to `.gitignore` before writing anything into it. This is the single most common way credentials leak.
* Decide the tunnel hostname now, for example coach.yourdomain, because three later steps need it.

### Step 2: Cloudflare tunnel

Roughly 30 minutes

* Add the domain to Cloudflare if it is not already there.
* Create a tunnel and install the connector as a container in the stack.
* Route the hostname to the ingest service. Expose only the MacroLog macro endpoint for now. The intervals.icu webhook route exists but is unused while there is no registered app, so there is nothing to point at it yet. Nothing else, and never the database.
* Verify from mobile data, not home wifi, or you will prove nothing.

### Step 3: Postgres

Roughly 10 minutes

* Runs as a container in the same stack with a named volume. No manual schema work; migrations run on boot.
* Set a strong password in .env. The port stays internal to the Docker network and is never published to the host.
* Point your existing backup routine at the nightly dump directory.

### Step 4: Telegram bot

Roughly 10 minutes

* Message BotFather, create the bot, save the token.
* Disable group privacy and group joining; this bot is one to one only.
* Get your numeric chat id by messaging the bot once and reading the update, then put it in .env as the allowlist.
* Set the bot commands list later, once the tool surface is settled.

### Step 5: Anthropic API

Roughly 5 minutes

* Create a key scoped to this project so its spend is separable.
* Set a monthly budget alert at the provider for USD 60. This is an alert, not a stop, and it is the number you will actually watch.
* The hard stop is separate and lives in the coach: OBS-07 halts model calls for the day at USD 3.00 and says so rather than going silent. Set it in `.env`, not in code.
* The two are deliberately not proportional. Thirty days at the daily cap would be USD 90, above the monthly alert, because the daily figure is a runaway backstop rather than a budget slice. Any sustained overspend trips the monthly alert first, which is the ordering you want; the daily stop only catches a genuine loop. The nightly consolidation is the largest single cost and you want to see it move.

### Step 6: intervals.icu API

Roughly 15 minutes

* Copy your athlete id and API key from the intervals.icu settings page into .env. Key based auth, so no OAuth flow and no token refresh to maintain.
* **No webhook is needed.** Webhooks require an app that only intervals.icu staff can create, and ingest does not depend on one. The API key alone covers every endpoint the coach uses. `INTERVALS_WEBHOOK_SECRET` can stay blank; the receiver is built and tested but idle, and switching it on later is a config change rather than a code change. See open item 11 for what that would take.
* Confirm both connections are live on the platform side: Zwift feeding activities, Whoop feeding wellness.
* **Strava, if you ever reconnect it.** Activity webhooks are never delivered for Strava sourced activities. That mattered a great deal when the webhook was the ingest mechanism; it matters much less now, because the poll does not care where an activity came from and will pick a Strava sourced ride up on the next pass like any other. Reconnecting Strava is therefore no longer dangerous — but if you later switch the webhook on and reconnect Strava, ingest for anything routed that way would silently fall back to the poll with no error anywhere. Turn one on or the other, not both without thinking.
* Check what the wellness payload actually contains for a recent day before assuming it covers everything, and record the answer against open item 3 in the PRD. If a metric is missing, it is dropped from the recovery deviation calculation per RECOV-02. Adding a direct Whoop integration is not an option: RECOV-03 forbids a Whoop client and SEC-04 forbids OAuth anywhere in the system.
* Confirm the Zwift connection in intervals.icu settings is the two way integration, not just activity import. With it connected, planned workouts push to Zwift directly and no workout files ever need moving. Test it with one workout before the coach depends on it.

### Step 6b: Sync Zwift's activity folder

Roughly 15 minutes, and the highest value 15 minutes in this guide

This is the fastest ingest path and the only one that survives intervals.icu being unavailable. Zwift already writes every ride to `Documents/Zwift/Activities` on the machine you ride on. Get those files to `COACH_FIT_WATCH` on the machine running the coach and rides ingest in seconds, with no API call, no credential and no webhook.

* Syncthing is the straightforward option: share `Documents/Zwift/Activities` from the riding machine, receive it into `COACH_FIT_WATCH` on the server. Dropbox or any file sync works equally well. If the coach runs on the same machine you ride on, point `COACH_FIT_WATCH` straight at the Zwift folder and skip the sync entirely.
* Make the receiving side read only if your sync tool offers it. Nothing in the coach deletes from that folder, and FIT-15 depends on the archive never being pruned, but a sync tool configured to mirror deletions could remove files upstream-deleted at the source. Receive only, never mirror.
* Test it before trusting it: drop any `.fit` file into the folder and watch a session row appear within one poll interval. `uv run coach-ingest` logs each scan.

Outdoor rides that sync straight from a head unit to intervals.icu will not appear here. Those come in on the poll below, which is why both paths exist.

### Step 6c: Set the poll interval

Roughly 2 minutes

The poll is what covers every source the folder does not. Defaults are in `.env`:

* `COACH_POLL_INTERVAL_S=120` — how often to ask intervals.icu for new activities. One API call per poll, and a file download only when it finds something new. 120s keeps PERF-03's five minute budget with room to spare.
* `COACH_SWEEP_INTERVAL_S=21600` — the slow pass that ages out prescriptions nothing satisfied. Six hours; an 18 hour grace window has nothing to say to a question asked more often.

Both are floored in code (30s and 300s) so a mistyped value cannot burn the daily rate limit. **Check the headroom once with a real key before leaving it unattended:** every response carries `X-RateLimit-Limit` and `X-RateLimit-Remaining` as `15m,daily` pairs. At 120s the poll makes about 720 calls a day. If that is close to the daily allowance, raise the interval — the folder path is unaffected either way, so Zwift rides stay fast regardless.

### Step 7: Calendar read access

Roughly 10 minutes

* No Google Cloud project, no consent screen, no OAuth. In Google Calendar settings, open each calendar you want the coach to see and copy the secret address in iCal format.
* Include anything that shapes your week: work, personal, golf, travel.
* Paste the URLs into `CALENDAR_ICS_URLS` in .env as a comma separated list. Treat them as passwords, because anyone holding one can read that calendar. The coach never writes them to the database and never logs them — including through `httpx`, which logs the request URL on every call and has a redacting filter installed on it for exactly that reason.
* The coach reads them every six hours (`COACH_CALENDAR_INTERVAL_S`), covering four weeks back and three forward. Backwards because observed availability is a claim about weeks that have happened; forwards because that is what scheduling needs.
* Mark anything you want the coach to ignore as "free" rather than deleting it. A birthday reminder marked busy costs you an evening a week.
* Note the trade: Google publishes these feeds on a cache, so a commitment added an hour ago may not appear immediately. Acceptable here because the coach only reads them and the weekly review confirms the week ahead, which is what CALR-05 encodes. Swapping to OAuth is not the escape hatch if the lag bites — SEC-04 forbids OAuth anywhere in the system, and PLAN-04 is scoped to what the feed showed at scheduling time precisely so the lag is survivable.

### Step 8: MacroLog wiring

Roughly 10 minutes

* No third party export app and no subscription. Apple Health data reaches the system through the HealthBridge module inside MacroLog.
* Put the intervals.icu API key into MacroLog's gitignored config file alongside the Anthropic key. The same key goes in the coach environment, so it lives in two places.
* Point MacroLog's macro writes at `POST https://<your tunnel host>/macrolog/meals`, with `MACRO_INGEST_SECRET` in an `X-Coach-Secret` header. Use a different value from `INTERVALS_WEBHOOK_SECRET`: one leaking must not open the other route.
* The payload is `{"meals": [ ... ], "deleted": [ ... ]}`, both halves optional, or a bare meal object for the simplest client. Each meal needs an `id` (yours, and the idempotency key) and an `eaten_at` carrying an offset — a naive timestamp is refused rather than guessed at, because the ambiguity is a whole day at the boundary. `kcal`, `protein_g`, `carbs_g`, `fat_g` and `fibre_g` are read; anything else you send is kept whole and not lost. Replaying a payload updates in place, and an id in `deleted` removes the row.
* **Build HealthBridge.** The check that would have made it unnecessary was run on 28 July 2026: wellness carries no `weight` on any day, so nothing feeds body mass and the coach has a working trend pipeline with nothing in it. Open items 1 and 2 in the PRD are closed on that basis.
* HealthBridge is yours, not Claude Code's. It is a module inside MacroLog rather than this repository, so no requirement covers it and nothing in the phase plan builds it. Treat it as a prerequisite of P04's validation gate; P04 itself is built and merged without it.
* Write `weight` to `PUT /api/v1/athlete/0/wellness/{date}` **without** `"locked": true`. The locked flag exists to stop a connected provider resyncing over an API written value, but no provider writes weight on this account and there is no documented way to unlock a day afterwards. Watch for a value reverting; if one ever does, that is the finding and the locked variant can be tested on a day that does not matter.

### Step 9: Backfill

Roughly 10 minutes

* Nothing to install. Confirm your full activity history is visible on intervals.icu, then let the backfill run in silent mode.
* Spot check three older rides against the platform to confirm parsed values match rather than trusting the count of rows loaded.
* Ride once and confirm it lands. Through the watched folder it should appear within seconds; through the poll, within `COACH_POLL_INTERVAL_S`. If neither, the six hourly sweep is the backstop — verify that rather than assume it. There is no webhook to test.

### Step 10: Seed memory

Roughly 30 minutes

* The source coaching conversation is committed at `docs/seed/coaching-conversation.md`, and both artefacts below derive from it. It is the audit trail; read it before changing either.
* Review `prompts/persona.md`, written from that conversation's voice, before it is loaded. It sets the tone for years.
* Review `seeds/athlete.json`, especially the constraints. Anything wrong here propagates everywhere, and constraints cannot be corrected automatically by design: only you can change one, and only by saying so. Every entry carries a `reason` you can check against the transcript.
* Apply it with `coach-seed` once the schema is migrated. Re-running is safe: a value already current is left alone rather than superseded.
* Confirm the starting block and let it publish to the calendar.

### Step 11: Verify

Roughly 60 minutes

* Read the first recall suite run yourself rather than trusting the pass flag.
* Ask the coach what it knows about three topics and check the answers against what you actually said.
* Break something deliberately: stop the health feed for two days and confirm the coach asks rather than assuming you stopped eating.
* Move a calendar session and confirm the change is noticed.

## 4. Environment variables

```
DATABASE_URL=postgresql://coach:PASSWORD@postgres:5432/coach
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_ID=
PUBLIC_BASE_URL=https://coach.yourdomain
MACRO_INGEST_SECRET=
INTERVALS_ATHLETE_ID=
INTERVALS_API_KEY=
INTERVALS_WEBHOOK_SECRET=
CALENDAR_ICS_URLS=
DAILY_SPEND_CAP_USD=3.00
TZ=Asia/Dubai
COACH_TZ=Asia/Dubai
COACH_INGEST_PORT=8080
COACH_FIT_ARCHIVE=var/fit-archive
COACH_FIT_WATCH=var/fit-inbox
COACH_POLL_INTERVAL_S=120
COACH_SWEEP_INTERVAL_S=21600
```

The two folder paths are worth choosing deliberately. `COACH_FIT_ARCHIVE` is the permanent copy of every file the system has seen and is never pruned, which is the whole point of FIT-15 — disconnecting an upstream integration deletes that source's activities upstream, and this is what survives it. Put it on storage you back up. `COACH_FIT_WATCH` is a drop folder: anything ending in `.fit` that lands there is ingested on the next six hourly pass, with no upstream involved at all.

Never commit this file. Rotate the Anthropic key and the ingest secret if it is ever pasted into a chat, a screenshot or an issue.

## 5. Running it

#### Processes

`coach-ingest` is the only long-running process, and it runs every inbound feed as loops inside one process:

* **Two HTTP routes** on `COACH_INGEST_PORT`, bound to loopback — the tunnel is what makes them reachable, so there is no reason to listen anywhere else. `POST /macrolog/meals` is MacroLog's; `POST /webhook/intervals` is the idle intervals.icu receiver. `GET /health` answers without a secret if you want something for the tunnel to probe.
* **The activity poll** (`COACH_POLL_INTERVAL_S`, 120s): asks intervals.icu what is new and scans the watched folder. This is the primary ingest path.
* **The wellness poll** (`COACH_WELLNESS_INTERVAL_S`, hourly): body mass and recovery. Slower because the feed changes once a day.
* **The calendar poll** (`COACH_CALENDAR_INTERVAL_S`, six hourly): the secret iCal feeds. Google serves them from a cache, so asking more often buys nothing.
* **The sweep** (`COACH_SWEEP_INTERVAL_S`, six hourly): ages out prescriptions nothing satisfied.
* **A drain worker** for the webhook queue, idle unless a webhook is ever configured. The route only queues; a delivery that fails is retried on the next pass rather than lost.

If the process is killed mid-ride, queued deliveries survive in the database and are picked up on restart. Nothing is held in memory that matters.

`coach-agent` is the conversation: a Telegram long poll, and one turn per backlog. It needs `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_ID` and `ANTHROPIC_API_KEY`, and refuses to start without them rather than dying on your first message.

`coach-scheduler` is the nightly work, on your local 03:00 rather than UTC. It runs confidence decay and the markdown fact export. **It does not yet run consolidation** — CONS-02's diff prompt has not been written — and it says so in a warning on startup rather than pretending. A night missed because the process was down runs when it comes back, which is why this is a loop rather than a cron entry.

Run all three. Only `coach-ingest` needs the tunnel.

#### Daily, automatic

* Zwift rides arrive through the watched folder within seconds of the file syncing. Everything else arrives on the poll, by default within two minutes. A session review follows either way, inside the five minute budget.
* MacroLog posts macros as you log meals, and body mass to intervals.icu wellness; the coach reads wellness back hourly (`COACH_WELLNESS_INTERVAL_S`) and re-reading is free, so a late provider fill-in is picked up regardless.
* Calendar feeds fetch every six hours; planned sessions publish to intervals.icu on block change.
* Decay and the fact export run at 03:00 local. Consolidation, the recall suite and the linter are not wired yet — see `docs/state-of-build.md`.

#### Weekly, yours

* The Sunday review arrives in chat. Answer it properly; it is the one input the system genuinely needs from you.
* Skim the failures alert channel. Silence means everything passed.

#### Occasionally

* Read the markdown fact export once a month. It takes two minutes and is the cheapest way to catch memory drift.
* Confirm a test restore of the database works at least once. An untested backup is a hope.

## 6. When it breaks

Symptoms and first checks

| Symptom | Likely cause | First check |
| --- | --- | --- |
| Coach stops replying | Long polling process died, or the API key hit a limit | Container status, then cost log |
| Recovery data stops | Whoop to intervals.icu link dropped, or the API key was rotated | Wellness endpoint directly with curl, then the platform connection page |
| Weight trend goes flat | HealthKit read permission revoked, which returns empty rather than an error | MacroLog HealthBridge queue, then the 12 day heartbeat should already have raised it |
| Coach never mentions weight at all | No reading has ever arrived, so there is no gap to raise — only a feed that has never delivered | `select count(*) from body_mass_readings`, then whether HealthBridge is writing at all |
| Sessions missing | Webhook not delivered, not a missed session | Run reconcile, then compare against the activity list upstream |
| Duplicate planned workouts | Coach id not written or not read on update | Planned event metadata upstream |
| Coach ignores a commitment | iCal feed cache had not updated when it planned | Fetch the feed URL directly and compare; mention it in the weekly review |
| Coach asserts something wrong | Bad fact, or a stale one that should have decayed | Ask it what it knows and when that changed; correct in one message |
| Recall suite fails | Consolidation wrote something it should not have | fact\_events for that night, ordered by time |

## 7. Total effort

Around three and a half hours across all eleven steps, most of it in steps 10 and 11 where you are reading output rather than configuring anything. With intervals.icu replacing the Whoop app and the file sync, and iCal feeds replacing Google OAuth, there is no OAuth anywhere in the system. The tunnel in step 2 is now the longest single task.

Sequence them just ahead of the phase that needs them: steps 1 to 5 before phase 00, step 6 before phase 03, step 8 before phase 04, step 9 alongside phase 03, step 7 before phase 06.

Step 10 splits across two phases. The coaching conversation, the persona and the seeded constraint facts are needed before phase 01, because CHAT-02 loads the persona and SAFE-01 loads the constraints into every prompt. The starting block is a phase 07 concern and waits.

Setup guide, July 2026. Companion to the PRD and the memory subsystem design.
