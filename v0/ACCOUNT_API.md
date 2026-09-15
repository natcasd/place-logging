# Account-scoped backend foundation

This is the second multi-user implementation stage. It adds private library
reads and existing library mutations against the migrated schema. It does not
yet provide a deployable multi-user app: login and account ingest processing
are subsequent stages.

## Boundary

`account_app.create_account_app(db_path=..., verify_session=...)` requires an
explicit migrated database and a trusted session verifier. The verifier takes
a bearer credential and returns `VerifiedAccount(user_id=...)` after validation
and server-side mapping to an internal account. Invalid, expired, or revoked
credentials must raise `InvalidSession`. There is no default verifier, public
development-token map, `X-User-ID` shortcut, or shared-token fallback.

The chosen login methods are Apple, Google, and email. The identity/session
provider remains to be selected and integrated. It must validate credentials
with its maintained SDK and enforce issuer/audience/expiry/revocation as
applicable. Linking login methods to an existing account requires verified
control; matching an unverified email must never merge accounts. Provider
identities and the stable internal owner ID are separate concepts. The tests
use a fake verifier only to exercise the account boundary; they do not implement
or validate real Apple/Google/email login.

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
| `DELETE /api/v1/activity/{id}` | Remove an owned failed/scheduled run and its events, preserving captures and other submissions. |
| `POST /api/v1/activity/{run}/entries/{entry}/location` | Confirm a stored candidate and group recommendations only within the owner. |
| `POST /api/v1/ingests`, `/shortcut/ingests` | Authenticate, then return 501 until account ingest is implemented. |
| `POST /api/v1/activity/{id}/retry` | Authenticate and check ownership, then return 501. |

The Shortcuts diagnostic endpoint is not registered in the account API. Health
and framework documentation contain no library data. There is no processing
worker or legacy service attached to this factory.

Map deletion detaches and hides removed mentions while retaining their stable
output identities and mutation sequence. It preserves captures, public cache
results, and shared locations. The following save/restoration stage must honor
these markers; this stage alone does not implement re-share restoration or
mention-specific deletion. A location confirmation also advances the user's
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

Next: private result materialization, individual mention deletion, deliberate
re-share restoration and retry ordering, then shared processing/legacy
reconciliation. Real session verification, provider account linking, and client
login remain required before enabling this API for users.
