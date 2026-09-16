# Account-scoped backend foundation

The account API provides isolated library access and durable public-post
acceptance against the migrated schema. The Firebase account service is live as
of the September 16 cutover. See [the implementation audit](MULTI_USER_AUDIT.md)
for verified behavior and outstanding device checks.

## Boundary

`account_app.create_account_app(db_path=..., verify_session=...)` requires an
explicit migrated database and a trusted session verifier. The verifier takes
a bearer credential and returns `VerifiedAccount(user_id=...)` after validation
and server-side mapping to an internal account. Invalid, expired, or revoked
credentials must raise `InvalidSession`. There is no default verifier, public
development-token map, `X-User-ID` shortcut, or shared-token fallback.

The chosen login methods are Apple and Google through standard Firebase
Authentication on Blaze. The provider adapter validates credentials with the
Firebase Admin SDK and checks issuer, audience, expiry, and revocation. Linking login methods to an existing account requires verified
control; matching an unverified email must never merge accounts. Provider
identities and the stable internal owner ID are separate concepts. The tests
use a fake verifier only to exercise the account boundary; they do not implement
or validate real Apple/Google login.

Every API data route uses the same authentication dependency. `AccountStore`
requires an explicit owner and checks that the account still exists and is
active inside each database transaction. A mutation uses `BEGIN IMMEDIATE`, so
ownership validation, status checks, sequence increments, and writes commit or
roll back together. Reads use a consistent transaction snapshot. Foreign and
nonexistent resource IDs both produce the same 404 response.

## Endpoint coverage

| Endpoint | Behavior in this stage |
|---|---|
| `GET /api/v1/entries` | Own active recommendations, mentions, and movie enrichment. |
| `GET /api/v1/sources` | Own captures, including captures with no active mentions. |
| `GET /api/v1/activity` | Own runs/events, with results reconstructed from current owned mentions. |
| `DELETE /api/v1/entries/{id}` | Remove all active mentions in the user's recommendation. |
| `DELETE /api/v1/entries` | All-or-nothing ownership check for the entire batch. |
| `DELETE /api/v1/activity/{id}` | Hide an owned failed/scheduled run, retain its cancellation identity, and remove its events. |
| `POST /api/v1/activity/{run}/entries/{entry}/location` | Confirm a stored candidate and group recommendations only within the owner. |
| `POST /api/v1/ingests`, `/shortcut/ingests` | Authenticate and durably accept a save, returning 202. |
| `POST /api/v1/activity/{id}/retry` | Requeue an owned failed/scheduled public save without advancing its original action sequence. |

The Shortcuts diagnostic endpoint is not registered in the account API. Health
and framework documentation contain no library data. An optional explicit `PostProcessingWorker` is started and drained through the
application lifespan. Its database must match the API database. No legacy
service is attached, and startup never creates or migrates a database.

Map deletion detaches and hides removed mentions while retaining their stable
output identities and mutation sequence. It preserves captures, public cache
results, and shared locations. The [private capture stage](PRIVATE_CAPTURES.md) honors
these markers and implements re-share
restoration and mention-specific deletion. A location confirmation also advances the user's
mutation sequence and records the edited mention. Private review can reuse a
shared place but cannot overwrite an existing shared place's metadata.

Direct/Siri captures can appear in the account API with null source URLs. The
released single-user response models remain unchanged. Response formatting is
shared with the old store through pure serialization helpers; account queries
and all writes are separate and explicitly scoped. Activity never falls back
to stale JSON result IDs when no capture exists.

## Verification and release

Run from `v0`:

```sh
python -m unittest discover -s tests -p test_account_access.py -v
python -m unittest discover -s tests -v
python generate_ios_entry_types.py --check
python -m compileall -q .
```

Tests cover migrated-library parity, nested-data isolation, map/batch deletion,
location confirmation, movie enrichment, direct captures, invalid/revoked
sessions, account disablement, concurrency, rollback, and no legacy fallback.
All fixtures and fake sessions are local to temporary test databases.

September 15 validation: all 195 backend tests pass, including 27 account-access
tests. Account-scoped reads against the earlier local production snapshot match
the original responses exactly: 598 recommendations, 432 sources, and 448
Activity records. Generated entry types and compilation checks also pass.

Fly's process command overrides the Docker default with `firebase_service:create_app`.
The explicit cutover is complete; never rerun the original migration over live data.
The API supports `DELETE /api/v1/mentions/{id}` and optional `mention_id` in location
confirmation. Cancelled Activity retains its request key privately to prevent replay.

## Durable HTTP acceptance

`POST /api/v1/ingests` accepts `source_url` and a required `request_key` (1–128
ASCII letters, digits, underscores, periods, colons, or hyphens). Generate the
key once for a deliberate share and retain it for every transport retry. A
new deliberate re-share uses a new key. Keys are scoped to the account.

`POST /api/v1/shortcut/ingests` uses the same key and `source_url_base64` instead.
The strict base64/UTF-8 adapter enters the same account acceptance flow. Caller
owner fields are rejected. Unsupported URLs return 422; temporarily unresolved
TikTok share links return 503 before creating a request. Each short-link redirect
is checked against the TikTok host allowlist before it is requested.

The 202 response contains `ingest_id`, `item_id`, `status`, `accepted_sequence`,
and current `saved_entries`. It means the operation is durably recorded, not
that extraction has completed. Replays return the existing operation, including
a cancelled status for a removed failure. Clients refresh the owned Activity
endpoint for completion. The native share extension opts into `wait_seconds=150`
on the POST, holding its ordinary request for a real result (200) or returning
pending acceptance at the deadline (202). Acceptance remains durable if the
connection ends. `GET /api/v1/ingests/{id}` reads only the owner's existing result;
an optional wait of up to 25 seconds does not resubmit the post.

Manual retry uses the existing Activity ID and original intent/sequence. Repeated
requests while queued or processing return that operation. A retry of a failed
attempt can start a fresh bounded attempt cycle; it never acquires new restoration
rights. A historical failed run can be adopted on explicit retry after source
resolution and capture reconciliation; it keeps its Activity ID and uses capture
intent, so retry cannot restore deleted mentions.

The optional worker is injected as
`create_account_app(db_path=..., verify_session=..., worker=...)`. Without one,
acceptance remains durable and a separate trusted worker must drain the queue.
Orderly shutdown stops claims and waits for in-flight processing/cleanup. Forced
termination leaves a recoverable lease. Fly starts this worker through the
Firebase service entrypoint.

## Firebase integration

The [Firebase verifier and explicit service entrypoint](FIREBASE_AUTH.md) verify
Apple/Google ID tokens and resolve the account on the server. `GET /api/v1/account`
returns its internal ID and display name. Google sign-in, existing-library binding,
and deployment are complete. Apple provider setup and device checks remain.
