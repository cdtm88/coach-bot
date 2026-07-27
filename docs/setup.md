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
* Route the hostname to the ingest service. Expose only two named routes: the MacroLog macro endpoint and the intervals.icu webhook. Nothing else, and never the database.
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
* Set a monthly budget alert at the provider. This is an alert, not a stop.
* The hard stop is separate and lives in the coach: OBS-07 halts model calls for the day once a daily cap is hit, and says so rather than going silent. Both figures are open item 7 in the PRD and need setting before P12. The nightly consolidation is the largest single cost and you want to see it move.

### Step 6: intervals.icu API and webhook

Roughly 15 minutes

* Copy your athlete id and API key from the intervals.icu settings page into .env. Key based auth, so no OAuth flow and no token refresh to maintain.
* Register a webhook pointing at your tunnel hostname for activity uploads, and store the signing secret.
* Confirm both connections are live on the platform side: Zwift feeding activities, Whoop feeding wellness.
* Check what the wellness payload actually contains for a recent day before assuming it covers everything, and record the answer against open item 3 in the PRD. If a metric is missing, it is dropped from the recovery deviation calculation per RECOV-02. Adding a direct Whoop integration is not an option: RECOV-03 forbids a Whoop client and SEC-04 forbids OAuth anywhere in the system.
* Confirm the Zwift connection in intervals.icu settings is the two way integration, not just activity import. With it connected, planned workouts push to Zwift directly and no workout files ever need moving. Test it with one workout before the coach depends on it.

### Step 7: Calendar read access

Roughly 10 minutes

* No Google Cloud project, no consent screen, no OAuth. In Google Calendar settings, open each calendar you want the coach to see and copy the secret address in iCal format.
* Include anything that shapes your week: work, personal, golf, travel.
* Paste the URLs into .env as a comma separated list. Treat them as passwords, because anyone holding one can read that calendar.
* Note the trade: Google publishes these feeds on a cache, so a commitment added an hour ago may not appear immediately. Acceptable here because the coach only reads them and the weekly review confirms the week ahead, which is what CALR-05 encodes. Swapping to OAuth is not the escape hatch if the lag bites — SEC-04 forbids OAuth anywhere in the system, and PLAN-04 is scoped to what the feed showed at scheduling time precisely so the lag is survivable.

### Step 8: MacroLog wiring

Roughly 10 minutes

* No third party export app and no subscription. Apple Health data reaches the system through the HealthBridge module inside MacroLog.
* Put the intervals.icu API key into MacroLog's gitignored config file alongside the Anthropic key. The same key goes in the coach environment, so it lives in two places.
* Point MacroLog's macro writes at the coach ingest endpoint on your tunnel hostname, with the shared secret as a header.
* Before building HealthBridge, read wellness for the last fortnight and check whether weight already varies day to day. If it does, something is already feeding it and the module is unnecessary. This is open item 1 in the PRD and it blocks P04.
* HealthBridge is yours, not Claude Code's. It is a module inside MacroLog rather than this repository, so no requirement covers it and nothing in the phase plan builds it. If item 1 resolves toward building it, treat it as a prerequisite of P04.

### Step 9: Backfill

Roughly 10 minutes

* Nothing to install. Confirm your full activity history is visible on intervals.icu, then let the backfill run in silent mode.
* Spot check three older rides against the platform to confirm parsed values match rather than trusting the count of rows loaded.
* Ride once and confirm the webhook fires end to end. If it does not, the 6 hour reconcile will still catch it, which is exactly the behaviour you want to verify rather than assume.

### Step 10: Seed memory

Roughly 30 minutes

* Commit the source coaching conversation to `docs/seed/coaching-conversation.md` first. CHAT-02 requires it in the repository, and P01 cannot start without it (open item 9).
* Review the persona system prompt extracted from it, at `prompts/persona.md`, before it is loaded. It sets the tone for years.
* Review the seeded facts, especially constraints. Anything wrong here propagates everywhere and constraints cannot be corrected automatically by design.
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
TZ=Asia/Dubai
```

Never commit this file. Rotate the Anthropic key and the ingest secret if it is ever pasted into a chat, a screenshot or an issue.

## 5. Running it

#### Daily, automatic

* Activities arrive by webhook on upload; a session review follows within minutes.
* MacroLog posts macros as you log meals and body mass to intervals.icu; wellness is read each morning and reconciled every six hours.
* Calendar feeds fetch every six hours; planned sessions publish to intervals.icu on block change.
* Consolidation runs at 03:00, then the recall suite and linter.

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
