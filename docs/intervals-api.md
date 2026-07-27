# intervals.icu API, verified

Verified against the live OpenAPI 3.0.1 spec at `https://intervals.icu/api/v1/docs`
on 27 July 2026. 117 paths, 174 fields on `Activity`, 46 on `Wellness`.

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
| LOG-07 manual activity | `POST /api/v1/athlete/{id}/activities/manual` | Present. `application/json`, body is the `Activity` schema. A bulk variant exists. |
| RECOV-01 wellness range | `GET /api/v1/athlete/{id}/wellness{ext}` | Present |
| RECOV-01 wellness by day | `GET,PUT /api/v1/athlete/{id}/wellness/{date}` | Present |
| PLAN-01 publish | `POST /api/v1/athlete/{id}/events`, `/events/bulk` | Present |
| PLAN-11 update in place | `GET,PUT,DELETE /api/v1/athlete/{id}/events/{eventId}` | Present |
| PLAN-09 structured render | `GET /api/v1/athlete/{id}/events/{eventId}/download{ext}` | "Download a planned workout in zwo, mrc, erg or fit format" |

`LOG-07`'s fallback is not needed. The requirement says a minimal TCX would be
generated and posted as multipart if the endpoint were unavailable. It is
available and takes JSON, so that branch is dead and the requirement is
simplified accordingly.

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

This settles what the API *can* carry. It does not settle what the athlete's
Whoop link actually populates, which is open item 3 and needs a real range read
against his account. A field the schema defines but the feed leaves null is
exactly the case RECOV-02 handles: recorded absent, dropped from the deviation.

## FIT-04: deduplication keys

`Activity` carries `id`, `external_id`, `source` and `start_date_local`. There is
no content hash on the upstream object, so FIT-04's content hash has to be
computed locally over the downloaded file rather than read from the API.

## FIT-03: derived fields are prefixed and easy to segregate

Of the 174 `Activity` fields, the values intervals.icu computes are almost all
`icu_` prefixed: `icu_ftp`, `icu_atl`, `icu_ctl`, `icu_average_watts`,
`icu_efficiency_factor`, `icu_hr_zones` and so on, plus `hr_load`. FIT-03 says
these are stored alongside parsed values and never substituted for them, and the
prefix makes that mechanically checkable rather than a matter of care.

## The gap: no webhook in the spec

**FIT-01 assumes a webhook and the API spec contains none.** Searching the whole
document finds zero occurrences of `webhook`, `callback` or `web_hook`.

That does not prove webhooks do not exist. Registration may live in the account
settings UI rather than the API, which is common. But it is not verifiable from
the spec, and FIT-01's acceptance depends on it: "a new Zwift ride appears as a
session row within 2 minutes without polling".

If there is no webhook, the consequences are concrete. The 6 hour reconcile
becomes the only ingest path rather than a backstop, a ride waits up to 6 hours
for its review, and PERF-03's 5 minute end to end budget cannot be met. The
options at that point are a shorter poll interval, which contradicts FIT-01's
own wording, or relaxing the budget. Both are decisions rather than fixes.

Tracked as open item 10, and it needs a look at the intervals.icu settings page
by someone logged in.
