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

**The three ways out, none free**

1. Register the app, authorise it against the athlete's own account once, store
   the bearer token. Keeps FIT-01 and PERF-03 intact. Requires amending SEC-04,
   which was written to keep exactly this out of the system.
2. No webhooks. Ingest becomes the reconcile loop only. FIT-01's "within 2
   minutes without polling" and PERF-03's 5 minute budget both have to change,
   or the interval has to drop far enough to be polling in all but name.
3. Register the app and test whether owner webhooks fire unauthorised. Costs an
   email and a wait, and may simply confirm option 1.

Tracked as open item 11. Nothing in P03 should be built until it is settled,
because it decides whether ingest is push or pull.
