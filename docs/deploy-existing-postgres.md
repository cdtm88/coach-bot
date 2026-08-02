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
| `coach-scheduler` | the nightly jobs at 03:00 local |

Only `coach-agent` and `coach-scheduler` need the Anthropic key. Only
`coach-ingest` needs the intervals.icu key, the macro secret and the calendar
URLs. Each gets what it reads and nothing else, so one leaked environment is not
three.

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
| `TELEGRAM_CHAT_ID` | required; SEC-03's allowlist, checked on every message |
| `COACH_FIT_ARCHIVE`, `COACH_FIT_WATCH` | the two host paths below |
| `INTERVALS_ATHLETE_ID` | optional; `0` resolves to the key owner |
| `TZ`, `COACH_LOG_LEVEL`, `DAILY_SPEND_CAP_USD` | optional, all defaulted |
| `CALENDAR_ICS_URLS` | empty until the secret iCal addresses exist |

The two required ones are required in the literal sense — both are `${VAR:?…}`,
so `docker compose up` refuses to start **any** service until they are set, not
just the one that reads them. That is deliberate for `TELEGRAM_CHAT_ID`: an
unset allowlist that defaulted to something permissive would be a coach that
talks to whoever finds the bot.

`INTERVALS_WEBHOOK_SECRET` can stay unset. The receiver is built and idle, and
unset means every webhook payload is refused — the right posture with no
registered app.

## The service blocks

Paste into the existing `docker-compose.yml`. `x-coach` factors out what all four
share; if the file already has anchors, put it beside them.

```yaml
x-coach: &coach
  build:
    context: ./src/coach-bot          # wherever the checkout lives
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
    PGSSLMODE: disable              # compose network only; no published port
    COACH_TZ: ${COACH_TZ:?set COACH_TZ in .env, an IANA name like Asia/Dubai}
    TZ: ${TZ:-UTC}
    COACH_LOG_LEVEL: ${COACH_LOG_LEVEL:-INFO}
  secrets: [app_password]
  depends_on:
    postgres: { condition: service_healthy }

services:

  migrate:
    <<: *coach
    restart: "no"
    entrypoint:
      - /bin/sh
      - -c
      - 'export PGPASSWORD="$$(cat /run/secrets/app_password)"; exec python -m coach.migrate'

  coach-agent:
    <<: *coach
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
      postgres: { condition: service_healthy }
      migrate:  { condition: service_completed_successfully }

  coach-scheduler:
    <<: *coach
    secrets: [app_password, anthropic_api_key, intervals_api_key]
    environment:
      <<: *coach-env
      DAILY_SPEND_CAP_USD: ${DAILY_SPEND_CAP_USD:-3.00}
      COACH_EXPORT_DIR: /backups
      INTERVALS_ATHLETE_ID: ${INTERVALS_ATHLETE_ID:-0}
    volumes:
      # MEM-12's markdown fact export, beside the pg_dump the sidecar writes.
      # Retention there is scoped by filename pattern precisely so this can
      # share the directory.
      - ./backups/postgres:/backups
    entrypoint:
      - /bin/sh
      - -c
      - 'export PGPASSWORD="$$(cat /run/secrets/app_password)"
         ANTHROPIC_API_KEY="$$(cat /run/secrets/anthropic_api_key)"
         INTERVALS_API_KEY="$$(cat /run/secrets/intervals_api_key)";
         exec coach-scheduler'
    depends_on:
      postgres: { condition: service_healthy }
      migrate:  { condition: service_completed_successfully }

  coach-ingest:
    <<: *coach
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
      - ${COACH_FIT_ARCHIVE:-./fit-archive}:/var/fit-archive
      - ${COACH_FIT_WATCH:-./fit-inbox}:/var/fit-inbox
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
      postgres: { condition: service_healthy }
      migrate:  { condition: service_completed_successfully }

# Absolute, because the secrets live on the array and the compose file may not.
# The names on the left are what the containers see under /run/secrets; only the
# telegram one differs from its file, which is why it is spelled out.
secrets:
  app_password:        { file: /mnt/user/appdata/coach-bot/secrets/app_password }
  anthropic_api_key:   { file: /mnt/user/appdata/coach-bot/secrets/anthropic_api_key }
  telegram_bot_token:  { file: /mnt/user/appdata/coach-bot/secrets/telegram_token }
  intervals_api_key:   { file: /mnt/user/appdata/coach-bot/secrets/intervals_api_key }
  macro_ingest_secret: { file: /mnt/user/appdata/coach-bot/secrets/macro_ingest_secret }
```

## Two host directories

On the array, for the same reason the secrets are — a relative path would resolve
beside the compose file, which on Unraid is the flash drive, and the FIT archive
is the one directory in the system that grows forever and is never pruned.

```bash
mkdir -p /mnt/user/appdata/coach-bot/fit-archive /mnt/user/appdata/coach-bot/fit-inbox
chown -R 10001:10001 /mnt/user/appdata/coach-bot/fit-archive /mnt/user/appdata/coach-bot/fit-inbox
chmod 750 /mnt/user/appdata/coach-bot/fit-archive /mnt/user/appdata/coach-bot/fit-inbox
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
