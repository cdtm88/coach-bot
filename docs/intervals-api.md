# intervals.icu API, verified

Verified against the live OpenAPI 3.0.1 spec at `https://intervals.icu/api/v1/docs`,
first on 27 July 2026 and re-checked on 28 July. 117 paths, 178 fields on
`Activity` (56 of them `icu_` prefixed), 46 on `Wellness`.

This exists because LOG-07 requires the manual activity endpoint to be checked
against the live spec before implementation, and because P03, P05 and P08 all
build against endpoints nobody had confirmed. Re-fetch the spec before starting
each of those phases; this file records what was true, not what will be.

## Authentication

The spec declares two schemes. We use the first and are forbidden the second.

| Scheme | Shape | Us |
| --- | --- | --- |
| `APIKey` | HTTP basic. Username is the literal `API_KEY`, password is the personal key from `/settings`. | Yes. RECOV-01 describes exactly this. |
| `AccessToken` | Bearer token from an OAuth flow. | No. SEC-04 forbids OAuth anywhere in the system. |

## Endpoints we depend on

| Requirement | Endpoint | Verified |
| --- | --- | --- |
| FIT-03 original file | `GET /api/v1/activity/{id}/file` | "Download original activity file, Strava activities not supported" |
| FIT-03 generated file | `GET /api/v1/activity/{id}/fit-file` | Present |
| FIT-05 backfill | `GET /api/v1/athlete/{id}/activities?oldest=&newest=` | Present. `oldest` is required; omitting it returns 422 with a named error. |
| LOG-07 manual activity | `POST /api/v1/athlete/{id}/activities/manual` | Present. `application/json`, body is the `Activity` schema. A bulk variant at `/activities/manual/bulk` is also present, re-verified against the live spec on 28 July 2026 (117 paths). An external review reported it absent; that review was working from a stale 104 path snapshot. |
| RECOV-01 wellness range | `GET /api/v1/athlete/{id}/wellness{ext}` | Present |
| RECOV-01 wellness by day | `GET,PUT /api/v1/athlete/{id}/wellness/{date}` | Present |
| PLAN-01 publish | `POST /api/v1/athlete/{id}/events`, `/events/bulk` | Present |
| PLAN-11 update in place | `GET,PUT,DELETE /api/v1/athlete/{id}/events/{eventId}` | Present |
| PLAN-09 structured render | `GET /api/v1/athlete/{id}/events/{eventId}/download{ext}` | "Download a planned workout in zwo, mrc, erg or fit format" |

`LOG-07`'s fallback is not needed. The requirement says a minimal TCX would be
generated and posted as multipart if the endpoint were unavailable. It is
available and takes JSON, so that branch is dead and the requirement is
simplified accordingly.

## What the wellness feed actually returns

Read on 28 July 2026 across 21 days: 22 rows, 46 distinct keys, 18 of them
populated on at least one day. This is the census, because the schema says what
*can* arrive and only a read says what does.

| Key | Populated | Used for |
| --- | --- | --- |
| `hrv`, `restingHR`, `sleepSecs`, `sleepScore`, `sleepQuality`, `respiration`, `spO2` | 13 of 22 | RECOV-02 storage, RECOV-04 deviation |
| `readiness` | 13 of 22 | Stored and shown; never an input to RECOV-04 |
| `ctl`, `atl`, `ctlLoad`, `atlLoad`, `rampRate` | 22 of 22 | RECOV-06's load signal |
| `tempWeight`, `tempRestingHR` | 22 of 22 | **Nothing. See below.** |
| `sportInfo`, `updated`, `id` | 22 of 22 | Metadata |
| `hrvSDNN`, `weight`, `bodyFat`, `locked`, `steps`, `vo2max`, `kcalConsumed`, and 21 others | never | — |

### `tempWeight` is not a body mass reading, and it looks exactly like one

This is the trap worth naming, because it is the obvious fix for HLTH-04 having
no source and it is wrong.

`weight` is null on every day and `tempWeight` is populated on every day. Across
the 22 day window `tempWeight` carried **two distinct values, one kilogram
apart**, alternating between them eight times. That is a carried-forward or
rounded stand-in the platform keeps so power-to-weight arithmetic has a number,
not a measurement series. `tempRestingHR` behaves identically — two distinct
values across 22 days — beside a `restingHR` that carries real values on 13.

Fitting HLTH-06's 28 day trend on that would draw a confident line through two
numbers and present it as a weight trend. It is precisely the harmful case open
item 1 was written to catch, arriving under a field name the item did not
anticipate. `coach.health.wellness.NEVER_STORED` names both fields and a test
asserts they do not reach the readings table.

### `atlLoad` is a real load signal

Populated on all 22 days: zero on eight, non-zero on fourteen. That makes
RECOV-06's cross check an actual distinction rather than a hypothetical one —
load recorded with no local activity means the upload is missing, not the
session. Note the third state: **no wellness row at all is not a recorded zero**,
and the code returns `None` rather than `False` for it, because absence of data
is never evidence of absence of activity.

### `sportInfo` carries a per-sport eFTP

Shape: `[{"type": "Ride", "eftp": 114.0, "wPrime": 8516.0, "pMax": 345.0}]`.
Nothing reads it yet. It is the natural source for `physiology.ftp_watts` when
BLOCK-06's ramp test is not the most recent evidence, and it is the same
information `SPORT_SETTINGS_UPDATED` would push if webhooks were ever turned on.
Recorded so it is not rediscovered.

## RECOV-02: the six fields exist on the schema

All six are properties of `Wellness`, alongside `weight` for HLTH-04:

| RECOV-02 field | Property |
| --- | --- |
| Sleep | `sleepSecs`, `sleepScore`, `sleepQuality` |
| Resting heart rate | `restingHR` |
| HRV | `hrv`, `hrvSDNN` |
| Recovery score | `readiness` |
| Respiration | `respiration` |
| SpO2 | `spO2` |

This settles what the API *can* carry. What the athlete's link actually
populates was open item 3, and V3a below answered it on 28 July 2026: six of the
seven fields arrive, `hrvSDNN` never does, and `weight` never does either. A
field the schema defines but the feed leaves null is exactly the case RECOV-02
handles: recorded absent, dropped from the deviation.

### A connected provider can overwrite an API written wellness value

New, and HLTH-04 depends on it. A forum user watched values written through the
API revert minutes later because the connected provider resynced over them.
Setting `"locked": true` in the same PUT stopped it. `Wellness.locked` is
confirmed present on the live schema.

This decides how body mass reaches the system. Either the write sends `weight`
and `locked` together, or a Whoop resync can silently revert the number the
weight trend is fitted on — and a trend fitted on a number that keeps reverting
is worse than no trend, because it looks like data.

Two cautions. The same thread reports no API path to unlock a day afterwards, so
locking is close to a one way door and should be deliberate rather than
defensive. And this is a forum report rather than documented behaviour, so it
needs confirming against the live account before HealthBridge writes anything.
See V3 below.

**V3a made this much less urgent.** No provider writes `weight` on this account —
it was null on all 22 days read — so there is nothing currently resyncing over an
API written value. The revert this section describes needs a competing writer and
there is not one. That does not make `locked` wrong, but it does mean the one way
door should stay shut until a revert is actually observed rather than being
walked through defensively on the strength of a forum post. See V3b.

`kcalConsumed` also exists on the schema. No current requirement needs it, but it
is the natural home for a daily energy figure derived from MacroLog's per meal
rows if the platform's own energy balance view is ever wanted. Out of scope for
v1; recorded so it is not rediscovered later.

## FIT-04: deduplication keys

`Activity` carries `id`, `external_id`, `source` and `start_date_local`. There is
no content hash on the upstream object, so FIT-04's content hash has to be
computed locally over the downloaded file rather than read from the API.

## Activity files are served gzipped

`GET /activity/{id}/file` returns the original upload **gzip compressed**. The
integration cookbook's own example writes the response to `activity.fit.gz`.
`GET /activity/{id}/fit-file` is the same but always FIT, regenerated by the
platform and carrying its edits, so it is not a substitute for the original under
FIT-03.

This matters more than it looks. Compression is invisible to a test suite that
builds its own plain fixtures, and the failure is silent in production: parsing
raises, the caller falls back to streams, and FIT-03's guarantee that the
original file is parsed is quietly not met while every test still passes.

It also breaks FIT-04 across paths. The webhook receives compressed bytes and the
watched folder receives plain ones, so hashing the bytes as received gives one
ride two hashes and therefore two session rows.

The code sniffs the two byte magic number and decompresses, rather than branching
on which endpoint produced the bytes. That is deliberate: httpx strips a
`Content-Encoding: gzip` header transparently but leaves a gzipped *payload*
alone, and the two are indistinguishable to the caller, so an endpoint based rule
would be correct only by luck. Hashing happens after decompression.

**Verified 28 July 2026, and the answer is neither.** V2 fetched a real Zwift
ride (`i169706449`, 68,547 bytes). The response carried `Content-Type:
application/octet-stream` and `Content-Encoding: gzip`, and the first two bytes on
arrival were `0e 20` — not the gzip magic `1f 8b`. So httpx had already stripped
the encoding and handed back plain FIT bytes, which parsed cleanly into 609
samples at 66.1 W average.

That is the transport encoding case. The payload case was not observed, so the
tests should simulate transport encoding as the normal path and keep a payload
case as the defensive one — the sniff in `parse.decompressed` handles both and
does not need to know which it is looking at.

## Analysis is asynchronous, and `analyzed` says so

`Activity.analyzed` is a date-time that is null until the platform finishes
processing. `ACTIVITY_UPLOADED` fires before that, so every `icu_` field read at
trigger time is provisional.

This resolves the tension between the two activity webhooks without giving up
either property. Trigger on upload, which keeps PERF-03's budget; record
`analyzed` and mark the row provisional; refresh the derived block when
`ACTIVITY_ANALYZED` arrives. FIT-03 is untouched, because parsed values come from
samples and have nothing to learn from a later read, and no second review is
generated: the ride was already reviewed when it landed.

## FIT-03: derived fields are prefixed and easy to segregate

Of the 178 `Activity` fields, 56 are `icu_` prefixed: `icu_ftp`, `icu_atl`,
`icu_ctl`, `icu_average_watts`, `icu_efficiency_factor`, `icu_hr_zones` and so
on, plus `hr_load`. FIT-03 says these are stored alongside parsed values and
never substituted for them, and the prefix makes that mechanically checkable
rather than a matter of care.

There is no undecorated `average_watts` on the object. That is what gives FIT-03
teeth: the platform's average is `icu_average_watts`, a derived field like the
rest, so a parsed average is either computed from samples or it does not exist.

`Activity.source` is an enum, useful for two things: spotting the Strava
condition below, and telling a Zwift ride from a manual write. It carried 14
values on 28 July 2026 — `STRAVA`, `UPLOAD`, `MANUAL`, `GARMIN_CONNECT`,
`OAUTH_CLIENT`, `DROPBOX`, `POLAR`, `SUUNTO`, `COROS`, `WAHOO`, `ZWIFT`, `ZEPP`,
`CONCEPT2`, `HUAWEI` — and it grows, so treat an unrecognised value as a new
integration rather than an error.

`paired_event_id` is the platform's own link from a completed activity to a
planned calendar event. PLAN-07 already says pairing uses the upstream link where
available; this is that field, and it should be FIT-05's primary key with date
and discipline as the fallback rather than the other way round. Not yet wired.

## Rate limits

Two windows, both reported on every response, per the developer's own post:

```
X-RateLimit-Limit:     <15m limit>,<daily limit>
X-RateLimit-Remaining: <15m remaining>,<daily remaining>
```

Daily limits reset at midnight UTC. Read these headers rather than guessing; the
6 hour reconcile and any backfill should back off on them.

`0` works in place of the athlete id on any path that takes one, resolving to the
athlete owning the key. Worth using: it removes a configuration value that can
drift out of step with the key.

## Webhooks: they exist, outside the OpenAPI spec

The spec contains no webhook path, which is why this was open item 10. They are
real; they are configured in the app management UI rather than through the API,
which is why the spec never mentions them. From the official integration
cookbook:

> Configure webhooks using the management page for your app. Look for your app in
> /settings and click "Manage App".

The payload carries the event and a shared secret:

```json
{
  "secret": "ooKeodacie8I",
  "events": [
    { "athlete_id": "...", "type": "ACTIVITY_UPLOADED",
      "timestamp": "2024-12-06T06:40:47.011+00:00", "activity": {} }
  ]
}
```

Event types that matter to us:

| Type | Use |
| --- | --- |
| `ACTIVITY_UPLOADED` | FIT-01's trigger. Fires on upload. |
| `ACTIVITY_ANALYZED` | Sent after a 60 second delay so multiple events for one activity consolidate into a single webhook. |
| `CALENDAR_UPDATED` | PLAN-11's athlete edit detection. Carries `oauth_client_id` and `external_id`, so we can filter to events our own app created. |
| `SPORT_SETTINGS_UPDATED` | Fires when FTP or zones change upstream. A physiology fact changing under us. |

`CALENDAR_EVENT_UPDATED` and `CALENDAR_EVENT_DELETED` are legacy; use
`CALENDAR_UPDATED`.

**Three things this changes.**

*FIT-02 says "signature verified".* There is no HMAC signature. Verification is a
shared secret carried in the body, so the check is a constant time comparison
against a configured value, and replay safety has to come from the timestamp plus
FIT-04's deduplication rather than from the transport.

*The 60 second delay on `ACTIVITY_ANALYZED` sits inside PERF-03's 5 minute
budget.* If we wait for the analysed event rather than the upload event, a third
of the budget is gone before we start. FIT-01 should trigger on
`ACTIVITY_UPLOADED`.

*Activity webhooks are not delivered for Strava activities.* Quoted directly from
the webhook documentation. This is only survivable because the Strava connection
was already dropped, so activities reach intervals.icu from Zwift and Wahoo
directly. It is worth confirming that no path still routes through Strava, since
reconnecting it later would silently disable the webhook for everything that
arrives that way. Related to open item 4.

## Planned workouts: upsert on our own key

From the official guide for uploading planned workouts.

```
POST /api/v1/athlete/0/events/bulk?upsert=true
PUT  /api/v1/athlete/0/events/bulk-delete
```

`external_id` is our primary key, and **"the external_id is only matched against
events created by your application"**. Events that do not exist are created,
those that do are updated. That is PLAN-11's stable coach id and "no duplicates
after ten changes" delivered by the API rather than by us, and it means we never
have to store an intervals.icu event id.

**PLAN-09 and PLAN-10 get easier than written.** A workout can be supplied three
ways: `file_contents_base64` (zwo, fit, mrc, erg), `file_contents` for raw ZWO, or
**`description` carrying native Intervals.icu workout text**. The third needs no
file generation at all, which is exactly what PLAN-10 wants when it says the
coach produces the step list and never the file.

Listing events returns everything on the calendar, not just ours; filter on
`oauth_client_id` to see only what we created.

## Webhooks require an OAuth application, and that collides with SEC-04

This is the significant unresolved thing, and the earlier optimistic reading of
it was wrong. Corrected here rather than quietly amended, because the earlier
version of this file argued SEC-04 was safe.

Webhooks are configured on an app's management page. Apps are OAuth
applications, and creating one is a manual process, from the OAuth post:

> Please mail the following info to david@intervals.icu: App name, Description,
> Website URL, Logo image URL (square, at least 128x128), Privacy policy URL,
> Redirect URI's, Your Intervals.icu ID
>
> Once your application has been created your app will show up on the /settings
> page (only for you as the owner) and you can click "Manage App" to retrieve
> your client_id and secret, change redirect URL's, **configure webhooks** and so
> on.

And the personal path is explicitly the one without any of that:

> Note that you don't need to do all this if you just want access to your own
> data. Use your API key to do that.

So the API key gets every endpoint we need and no webhook. The webhook is
attached to an application.

**What is known**

* App creation is required for webhooks, and requires a human at intervals.icu to
  approve it. There is a website URL and a privacy policy URL to supply for what
  is a single user personal tool.
* API calls can still use the API key. Registering an app does not force bearer
  tokens on the calls themselves.

**What is not known, and decides this**

Whether a webhook fires for the app owner's own activities without that athlete
having granted the app authorisation through the OAuth consent flow. The cookbook
leans the other way:

> You need to store the Intervals.icu athlete_id obtained via OAuth flow so you
> can map the webhook back to the athlete in your system.

If authorisation is required, then FIT-01 depends on running an OAuth flow at
least once and holding a bearer token, which is exactly what SEC-04 forbids:
"no OAuth flow exists anywhere in the system". A one time consent is still a
consent redirect and a token exchange.

**Resolved: the tokens are static, so there is nothing to implement**

Two statements in the OAuth thread settle it. From the developer:

> Intervals.icu is a bit easier because it doesn't use refresh tokens, only
> access tokens. (#9)

And on expiry, from a long standing integrator and confirmed by the developer in
the following post:

> AFAIK, the token is once-off (never had to re-login to get a new one). Whenever
> a new login is detected, a new token gets generated and that would be the token
> that has to be used across all your devices. The old one is discarded. (#20)
>
> It is as app4g says. (#22, david)

So the shape is: authorise once in a browser, receive a bearer token that does
not expire and cannot be refreshed because refresh does not exist, paste it into
`.env` beside the API key. The consent is a one time human action, not a code
path. The codebase gets no authorisation redirect, no token exchange, no refresh
timer and no OAuth client, which is exactly what SEC-04's acceptance asks for.

It is worth being precise about what did and did not change. SEC-04's prose said
"no OAuth flow exists anywhere in the system". A one time browser consent is an
OAuth flow, performed by a person, outside the system. The requirement has been
amended to say that plainly rather than left to read as though nothing happened.
The acceptance criterion is unchanged and still passes.

**Two operational consequences.**

Re-authorising issues a new token and discards the old one. Only one token exists
per app per athlete, so redoing the consent silently breaks a running deployment
until `.env` is updated. Worth a line in the setup guide rather than a surprise.

The API key still does every API call. The bearer token may end up entirely
unused at runtime: it is the consent that links the app to the athlete and makes
webhooks fire, not the token in a header. Keep using the key for calls and treat
the token as a registration artefact unless something proves otherwise.

There is also a manual path if self service registration is awkward. In 2024 the
developer offered to configure webhooks directly:

> There is some undocumented support for webhooks. If you send me one or two web
> hook URLs (e.g. one dev one prod). I will set that up for you. (#29)

Superseded by the self service management page later that year, but it suggests
a friendly response to a single user asking.

## Verification

Three empirical checks. Each is cheap, each needs the live API key, and each
blocks something. V2 and V3a were run on 28 July 2026 and their results are
recorded below and folded into the sections above. V1 and V3b are still
outstanding, and V3b may no longer be worth running.

### Rate limits, observed

Neither `X-RateLimit-Limit` nor `X-RateLimit-Remaining` was present on any
response during the V2 or V3a runs. The client parses them when they appear and
degrades to "unknown" when they do not, which is what it did. So the headroom
question that was meant to validate `COACH_POLL_INTERVAL_S=120` is unanswered
from headers, and the 120 second interval stands on the arithmetic rather than on
measurement: roughly 720 list calls a day plus one wellness call an hour. Nothing
has been rate limited in practice. Revisit if a 429 ever appears.

### V1. External id scoping under an API key — blocks P08 and PLAN-02

The `external_id` upsert rule and the `oauth_client_id` filter are both
documented in the context of OAuth applications: matching happens against events
"created by your application". A write authenticated with a personal API key has
no OAuth client, so that phrase is undefined for us and `oauth_client_id` is
probably null on everything the coach creates.

Two consequences if so. PLAN-05's orphan sweep cannot filter on
`oauth_client_id` and must use an `external_id` prefix convention instead, and
the upsert may match against a wider or narrower set than intended.

Three calls settle it. Publish one event with `external_id` `coach:test:1`
through `POST /events/bulk?upsert=true`. Read it back with `GET /events` over
that date and record whether `oauth_client_id` and `created_by_id` are
populated. Publish again with the same `external_id` and a changed `name`, then
confirm one event exists rather than two. Delete through `bulk-delete` by
`external_id` and confirm the returned count is 1.

### V2. File encoding on the wire — **run 28 July 2026**

Fetched `GET /activity/i169706449/file`. Result recorded under "Activity files
are served gzipped" above: 200, `Content-Encoding: gzip`, first two bytes `0e20`,
parsed to 609 samples. httpx strips the encoding, so the bytes arrive plain. The
sniff in `parse.decompressed` is correct either way and nothing changed.

### V3a. Wellness read — **run 28 July 2026, resolves open items 1, 2 and 3**

Read the last 21 days. 22 rows returned, 13 of them populated (the nine days
before 16 July are empty).

| Field | Populated |
| --- | --- |
| `sleepSecs`, `sleepScore`, `restingHR`, `hrv`, `readiness`, `respiration`, `spO2` | 13 of 22 |
| `hrvSDNN` | **0 of 22** |
| `weight` | **0 of 22** |

Three consequences, all now folded into the code and the PRD.

*Open item 1 is answered, and the answer was the third possibility nobody wrote
down.* The question was whether `weight` moves day to day or repeats. It does
neither: it is absent. So it is not a stale profile field the coach would anchor
on — it is nothing at all, which is a better outcome than the harmful case and a
worse one than the hoped-for case.

*Open item 2 follows: HealthBridge is required.* Nothing feeds body mass, so
HLTH-04's source does not exist until MacroLog writes it. That is the athlete's
side of the setup guide's division of labour and no requirement in this
repository covers it. P04's implementation gate does not depend on it — the trend
and every threshold in it are tested on seeded data — but P04's validation gate
cannot start until real readings arrive.

*Open item 3 is answered: `hrvSDNN` never arrives.* RECOV-02 drops it from the
deviation, which is the behaviour that requirement provides for rather than a
defect. The other six fields all populate.

### V3b. The wellness write — **probably not worth running**

Originally: PUT a test weight without `locked`, wait for a provider resync, read
it back, then repeat with `"locked": true`, and whichever survives decides how
HealthBridge writes body mass.

V3a undercut the premise. The revert this tests for needs a connected provider
that writes `weight`, and this account has none — the field is null on every day.
There is nothing to be overwritten by. Given that unlocking a day has no
documented API path, running the locked half is walking through a one way door to
answer a question the account no longer poses.

Recommendation: have HealthBridge write `weight` without `locked`, watch whether
a value ever reverts, and only then revisit. If one does revert, that is itself
the finding and the locked variant can be tested on a day that does not matter.

## A note on reviewing this document

An external integration review dated 28 July 2026 checked this file against an
OpenAPI snapshot and reported three failures. Two were real and are now folded in
above. The third — that `POST /activities/manual/bulk` does not exist — was wrong:
the snapshot carried 104 paths against the live document's 117, and the endpoint
is present. It would have made this file less accurate, not more.

The lesson is worth keeping. Re-fetch the live spec at the start of any phase
that touches a new endpoint, and when a source disagrees with this file, check
which one is stale before amending anything.
