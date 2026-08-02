# Adding the coach to a Postgres you already run

For the case `docs/deploy.md` does not cover: the database is already deployed,
with its own conventions, and only the application services are missing. Written
against the Unraid handover of 2 August 2026 and kept general where it can be.

Nothing here modifies the database service, its init scripts, or its backup
sidecar. The application adapts to what is there.

## What had to change in the application, and why

**`DATABASE_URL` is no longer required.** It is still honoured and still what the
bundled `docker-compose.yml` sets, but when it is absent and `PGHOST` is present,
the connection is opened with no conninfo at all and libpq reads `PGHOST`,
`PGPORT`, `PGUSER`, `PGDATABASE`, `PGPASSWORD` and `PGSSLMODE` from the
environment itself.

That is not a convenience. A password living in a file at
`/run/secrets/app_password` and exported as `PGPASSWORD` is the whole point of
mounting it as a secret; assembling a URI from it would put it back into a
string, where it appears in `ps`, in a crash traceback, and in any log line that
echoes the connection target. `tests/test_db_connection.py` pins both routes and
scans for anyone reassembling one.

`Config.from_env` changed with it, and that was the blocking half:
`coach-migrate` and `coach-seed` both go through it, so a libpq-only deployment
could not have run its migrations at all.

## Four services, not two

The handover assumed one `app`. There are three long-running processes, and they
are separate because they fail independently — a wedged Telegram poll should not
stop activity ingest.

| | |
| --- | --- |
| `migrate` | `python -m coach.migrate`, runs once, exits 0 |
| `coach-agent` | Telegram long poll; one reply per backlog |
| `coach-ingest` | two HTTP routes and six polling loops |
| `coach-scheduler` | the nightly jobs at 03:00, and P10's three timed messages |

Only `coach-agent` and `coach-scheduler` need the Anthropic key. Only
`coach-ingest` needs the macro secret and the calendar URLs. Each gets what it
reads and nothing else, so one leaked environment is not three.

**`coach-scheduler` holds the most, and that changed with P10.** It used to need
no Telegram credentials at all; now it sends the morning message, the evening
follow-up and the Sunday review, so it carries the bot token and the allowlisted
chat id alongside the Anthropic and intervals.icu keys. Worth stating because it
is the one service whose secret list grew, and a stack deployed before P10 will
log `P10 notifications not scheduled: TELEGRAM_ALLOWED_CHAT_ID is not set` and
carry on doing everything else — which is the intended failure, and easy to miss.

## Secrets

Following the existing `secrets/` pattern, 0700 root. Four more alongside
`app_password`, one of which already exists as an empty placeholder:

```
anthropic_api_key
intervals_api_key
telegram_token                  # the existing empty one, filled
macro_ingest_secret             # openssl rand -hex 32
```

**Not beside the compose file.** On Unraid the Compose Manager plugin keeps
projects under `/boot/config/plugins/compose.manager/projects/`, and `/boot` is
the flash drive — vfat, so `chmod 600` there is a silent no-op and the files stay
world readable. The flash is also what the flash backup copies wholesale, which
would put the API keys inside a backup archive. Keep them on the array beside the
database's own secrets and give the `secrets:` block absolute paths.

**Ownership matters here, unusually.** Outside swarm, Compose bind-mounts a
secret file with its host ownership intact — the `uid`, `gid` and `mode` fields
of the long syntax are swarm-only and are ignored. All four services run as
`10001`, so a root-owned 0600 file is unreadable to them, and the failure is a
container that starts and then cannot authenticate. `chown 10001:10001` the four
new files. The existing `app_password` needs nothing: it is 0644, which uid 10001
can already read, and the directory being 0700 is what keeps it private on the
host.

Address them as `/mnt/cache/...` rather than `/mnt/user/...` if the database
stack does — on a cache-only share both reach the same files, one through shfs
and one directly, and using both forms for one dataset across containers is a
well-known way to get confusing results on Unraid. Matching whatever the existing
stack uses costs nothing.

Write them with `printf` rather than `echo` out of habit rather than necessity —
the entrypoints read each one through `$(cat …)`, and command substitution strips
trailing newlines. `read -rsp` avoids putting a key into shell history at all.

Everything else is ordinary configuration and belongs in `.env`.

## What `.env` needs

No `DATABASE_URL` and no `POSTGRES_PASSWORD`: the libpq variables are set
literally in the compose `environment:` block and the password comes from the
mounted file.

| | |
| --- | --- |
| `COACH_TZ` | required; the zone the coach reasons in, an IANA name |
| `COACH_MORNING_HOUR` etc. | optional; NOTIF-05's four hours, defaulted below |
| `TELEGRAM_CHAT_ID` | required; SEC-03's allowlist, checked on every message |
| `COACH_FIT_ARCHIVE`, `COACH_FIT_WATCH` | required; the two host paths below |
| `INTERVALS_ATHLETE_ID` | optional; `0` resolves to the key owner |
| `TZ`, `COACH_LOG_LEVEL`, `DAILY_SPEND_CAP_USD` | optional, all defaulted |
| `CALENDAR_ICS_URLS` | empty until the secret iCal addresses exist |

The four required ones are required in the literal sense — each is `${VAR:?…}`,
so `docker compose up` refuses to start **any** service until they are set, not
just the one that reads them. That is deliberate in each case. An unset allowlist
defaulting to something permissive would be a coach that talks to whoever finds
the bot. And a FIT path defaulting to something relative would resolve beside the
compose file, which is the flash drive — so the archive that must never be pruned
would be filling up the one volume you least want it on, silently and correctly
as far as Compose is concerned.

P10 adds four optional hours, all in `COACH_TZ` rather than UTC. Omit them and
the coach uses these:

```
COACH_MORNING_HOUR=6        # NOTIF-01, names today's session or the rest day
COACH_FOLLOW_UP_HOUR=21     # NOTIF-02, one check when a session left no trace
COACH_REVIEW_HOUR=18        # REV-01
COACH_REVIEW_WEEKDAY=6      # Monday is 0, Sunday is 6
```

An unparseable or out-of-range value logs and falls back rather than raising: a
typo should cost a message at the wrong time, not the 03:00 consolidation pass.

`PUBLIC_BASE_URL` starts mattering here too — NOTIF-04 serves charts at
`/charts/load` and `/charts/weight`, and that is what makes the link the coach
sends resolvable from a phone rather than only from inside the network.

`INTERVALS_WEBHOOK_SECRET` can stay unset. The receiver is built and idle, and
unset means every webhook payload is refused — the right posture with no
registered app.

## A separate project, joining the database's network

The coach is its own Compose project and does not `include:` the database's
compose file. That is the whole shape, and getting it wrong fails immediately:
`include` merges the other file into *this* project, so this project then tries
to create the database container too, and Docker refuses because a container of
that name already exists and belongs to the other project. Two projects cannot
both own one container.

So the database is referenced, never declared. Its network is attached as
`external`, which is what puts the coach's containers in the same DNS namespace
as it:

```bash
docker inspect <db container> --format \
  'project={{index .Config.Labels "com.docker.compose.project"}}{{println}}{{range $k,$v := .NetworkSettings.Networks}}net={{$k}} aliases={{$v.Aliases}}{{println}}{{end}}'
```

The network name goes in the `networks:` block below; the aliases tell you what
`PGHOST` should be. A Compose service is reachable by its *service* name, which
is often not the `container_name` — on the Unraid stack, `coach-db` also answers
to `postgres`, so `PGHOST: postgres` is right.

There is no `depends_on: postgres` for the same reason: `depends_on` cannot cross
a project boundary. Nothing is lost — the database is already running. What
changes is that if it is ever stopped, the coach's containers fail on connect and
restart rather than wait, which is the honest behaviour when there is no longer
anything local to wait on.

## The service blocks

Paste into a `docker-compose.yml` of their own. `x-coach` factors out what all
four share; if the file already has anchors, put it beside them.

```yaml
x-coach: &coach
  build:
    context: /mnt/cache/appdata/coach-bot/src     # wherever the checkout lives
  user: "10001:10001"
  restart: unless-stopped
  logging:
    driver: json-file
    options: { max-size: "10m", max-file: "5" }
  environment: &coach-env
    PGHOST: postgres
    PGPORT: "5432"
    PGUSER: coach
    PGDATABASE: coach
    PGSSLMODE: disable              # shared network only; no published port
    COACH_TZ: ${COACH_TZ:?set COACH_TZ in .env, an IANA name like Asia/Dubai}
    TZ: ${TZ:-UTC}
    COACH_LOG_LEVEL: ${COACH_LOG_LEVEL:-INFO}
  secrets: [app_password]
  networks: [db]
  # `container_name` is per service below, not here: the anchor is shared and
  # four containers cannot have one name. Worth setting so the Docker page
  # reads `coach-agent` beside `coach-db` rather than `coach_bot-coach-agent-1`.

services:

  migrate:
    <<: *coach
    container_name: coach-migrate
    restart: "no"
    entrypoint:
      - /bin/sh
      - -c
      - 'export PGPASSWORD="$$(cat /run/secrets/app_password)"; exec python -m coach.migrate'

  coach-agent:
    <<: *coach
    container_name: coach-agent
    secrets: [app_password, anthropic_api_key, telegram_bot_token]
    environment:
      <<: *coach-env
      TELEGRAM_ALLOWED_CHAT_ID: ${TELEGRAM_CHAT_ID:?set TELEGRAM_CHAT_ID in .env}
      DAILY_SPEND_CAP_USD: ${DAILY_SPEND_CAP_USD:-3.00}
      PERSONA_PATH: /app/prompts/persona.md
    entrypoint:
      - /bin/sh
      - -c
      - 'export PGPASSWORD="$$(cat /run/secrets/app_password)"
         ANTHROPIC_API_KEY="$$(cat /run/secrets/anthropic_api_key)"
         TELEGRAM_BOT_TOKEN="$$(cat /run/secrets/telegram_bot_token)";
         exec coach-agent'
    depends_on:
      migrate: { condition: service_completed_successfully }

  coach-scheduler:
    <<: *coach
    container_name: coach-scheduler
    # Four secrets, which is more than any other service holds. P10 is why: the
    # scheduler sends the morning message, the evening follow-up and the Sunday
    # review, so it needs the same Telegram credentials the agent does. The
    # alternative — routing its messages through the agent — would mean a queue
    # between two containers for three messages a day.
    secrets: [app_password, anthropic_api_key, intervals_api_key, telegram_bot_token]
    environment:
      <<: *coach-env
      DAILY_SPEND_CAP_USD: ${DAILY_SPEND_CAP_USD:-3.00}
      COACH_EXPORT_DIR: /backups
      INTERVALS_ATHLETE_ID: ${INTERVALS_ATHLETE_ID:-0}
      TELEGRAM_ALLOWED_CHAT_ID: ${TELEGRAM_CHAT_ID:?set TELEGRAM_CHAT_ID in .env}
    volumes:
      # MEM-12's markdown fact export, beside the pg_dump the sidecar writes.
      # Retention there is scoped by filename pattern precisely so this can
      # share the directory.
      - /mnt/cache/appdata/coach-bot/backups/postgres:/backups
    entrypoint:
      - /bin/sh
      - -c
      - 'export PGPASSWORD="$$(cat /run/secrets/app_password)"
         ANTHROPIC_API_KEY="$$(cat /run/secrets/anthropic_api_key)"
         INTERVALS_API_KEY="$$(cat /run/secrets/intervals_api_key)"
         TELEGRAM_BOT_TOKEN="$$(cat /run/secrets/telegram_bot_token)";
         exec coach-scheduler'
    depends_on:
      migrate: { condition: service_completed_successfully }

  coach-ingest:
    <<: *coach
    container_name: coach-ingest
    secrets: [app_password, intervals_api_key, macro_ingest_secret]
    environment:
      <<: *coach-env
      INTERVALS_ATHLETE_ID: ${INTERVALS_ATHLETE_ID:-0}
      CALENDAR_ICS_URLS: ${CALENDAR_ICS_URLS:-}
      COACH_INGEST_PORT: "8080"
      # 0.0.0.0 inside the container, and required rather than relaxed: a tunnel
      # container has its own network namespace and cannot reach this one's
      # loopback. No `ports:` stanza means the compose network and nowhere else.
      COACH_INGEST_HOST: "0.0.0.0"
      COACH_FIT_ARCHIVE: /var/fit-archive
      COACH_FIT_WATCH: /var/fit-inbox
    volumes:
      # FIT-15: the permanent copy of every file the system has seen, never
      # pruned. Put the host path on storage that is backed up — and keep any
      # appdata backup job away from pgdata, which is a different rule entirely.
      - ${COACH_FIT_ARCHIVE:?set COACH_FIT_ARCHIVE in .env}:/var/fit-archive
      - ${COACH_FIT_WATCH:?set COACH_FIT_WATCH in .env}:/var/fit-inbox
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=5).status==200 else 1)\""]
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 30s
    entrypoint:
      - /bin/sh
      - -c
      - 'export PGPASSWORD="$$(cat /run/secrets/app_password)"
         INTERVALS_API_KEY="$$(cat /run/secrets/intervals_api_key)"
         MACRO_INGEST_SECRET="$$(cat /run/secrets/macro_ingest_secret)";
         exec coach-ingest'
    depends_on:
      migrate: { condition: service_completed_successfully }

# Absolute, because the secrets live on the array and the compose file may not.
# The names on the left are what the containers see under /run/secrets; only the
# telegram one differs from its file, which is why it is spelled out.
# `external` because the database's own project created it. This is what puts
# the coach in the same DNS namespace as the database without either project
# owning the other's containers.
networks:
  db:
    external: true
    name: coach_bot_default

secrets:
  app_password:        { file: /mnt/cache/appdata/coach-bot/secrets/app_password }
  anthropic_api_key:   { file: /mnt/cache/appdata/coach-bot/secrets/anthropic_api_key }
  telegram_bot_token:  { file: /mnt/cache/appdata/coach-bot/secrets/telegram_token }
  intervals_api_key:   { file: /mnt/cache/appdata/coach-bot/secrets/intervals_api_key }
  macro_ingest_secret: { file: /mnt/cache/appdata/coach-bot/secrets/macro_ingest_secret }
```

## Two host directories

On the array, for the same reason the secrets are — a relative path would resolve
beside the compose file, which on Unraid is the flash drive, and the FIT archive
is the one directory in the system that grows forever and is never pruned.

```bash
mkdir -p /mnt/cache/appdata/coach-bot/fit-archive /mnt/cache/appdata/coach-bot/fit-inbox
chown -R 10001:10001 /mnt/cache/appdata/coach-bot/fit-archive /mnt/cache/appdata/coach-bot/fit-inbox
chmod 750 /mnt/cache/appdata/coach-bot/fit-archive /mnt/cache/appdata/coach-bot/fit-inbox
```

Then point `COACH_FIT_ARCHIVE` and `COACH_FIT_WATCH` in `.env` at them. `ls -ldn`
rather than `ls -ld` to check: the uid prints as `10001` because no such user
exists on the host, which is expected — it only has to mean something inside the
container. `10001` matches the `user:` above and the uid baked into the image. The
`backups/postgres` directory is already writable by the sidecar; the scheduler
writes a `facts-*.md` into it, which is why the existing retention is scoped to
`daily/` and `monthly/` by pattern.

## Bring it up

```bash
docker compose up -d migrate
docker compose logs migrate            # 13 migrations, then exit 0
docker compose up -d coach-agent coach-ingest coach-scheduler
```

Then seed, once, after reading `seeds/athlete.json` — especially the constraints,
which cannot be corrected automatically by design:

```bash
docker compose run --rm --entrypoint /bin/sh coach-agent -c \
  'export PGPASSWORD="$(cat /run/secrets/app_password)"; \
   exec coach-seed --file /app/seeds/athlete.json'
```

Re-running is safe: a value already current is left alone rather than superseded.

## What the handover flagged, answered

**`LAST_RUN` saying `FAIL` on an empty schema.** It will flip to `OK` after
`migrate` runs — the schema is 30-odd tables and well past the 20KB floor. Worth
checking the morning after rather than assuming.

**The `coach` role is not a superuser.** Nothing in the migrations needs one:
they create tables, indexes, constraints and comments in the `coach` database,
and no extension is installed. `013_adjustments.sql` is the newest and does
nothing unusual.

**`python -m coach.migrate`** is supported and behaves as specified — numbered
files in order, each in a transaction, applied versions recorded, abort on first
error, exit 0 on success and non-zero on failure. Verified against a libpq-only
environment.

**No retry-on-startup loops.** There are none; `depends_on` with
`service_healthy` is the whole mechanism.

## Still outstanding on the infrastructure side

From the handover, unchanged by this: `backups/postgres` is not synced offsite,
the monthly restore test is not scheduled, and Telegram alerting for it is not
configured. None blocks the coach. The last one is worth doing early for a reason
the handover does not mention — the same chat that gets the restore alert is the
one the coach talks to, so a silent backup failure and a silent coach failure
would look identical.
