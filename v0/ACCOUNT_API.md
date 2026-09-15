# Account-scoped backend foundation

The account API provides isolated library access and durable public-post
acceptance against the migrated schema. Firebase/client integration, historical
reconciliation, and release verification remain before production cutover.

## Boundary

`account_app.create_account_app(db_path=..., verify_session=...)` requires an
explicit migrated database and a trusted session verifier. The verifier takes
a bearer credential and returns `VerifiedAccount(user_id=...)` after validation
and server-side mapping to an internal account. Invalid, expired, or revoked
credentials must raise `InvalidSession`. There is no default verifier, public
development-token map, `X-User-ID` shortcut, or shared-token fallback.

The chosen login methods are Apple and Google through standard Firebase
Authentication on Blaze. The provider adapter remains to be integrated. It must validate credentials
with its maintained SDK and enforce issuer/audience/expiry/revocation as
applicable. Linking login methods to an existing account requires verified
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

`Dockerfile` still starts `app:app`, the existing single-user service. This
change does not migrate a database, start a new Fly app, change secrets, install
a phone build, or enable automatic deployment. Keep production on its current
release until the remaining stages and the cutover rehearsal are complete.

Next: shared processing and legacy reconciliation. Real session verification, provider account linking, and client
login remain required before enabling this API for users.

The private capture stage adds `DELETE /api/v1/mentions/{id}` and optional
`mention_id` in location confirmation. Cancelled Activity retains its request
key privately to prevent replay. Shared workers and production login remain
subsequent stages.

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
endpoint for completion; the legacy synchronous extraction envelope is unchanged.

Manual retry uses the existing Activity ID and original intent/sequence. Repeated
requests while queued or processing return that operation. A retry of a failed
attempt can start a fresh bounded attempt cycle; it never acquires new restoration
rights. Reconciliation is still required for legacy captures.

The optional worker is injected as
`create_account_app(db_path=..., verify_session=..., worker=...)`. Without one,
acceptance remains durable and a separate trusted worker must drain the queue.
Orderly shutdown stops claims and waits for in-flight processing/cleanup. Forced
termination leaves a recoverable lease. Docker still starts the legacy service;
no production database, deploy configuration, or phone build changes here.
