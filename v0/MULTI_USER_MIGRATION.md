# SQLite multi-user schema and migration

This is the first implementation stage: a concrete schema and an offline,
verified migration. It does **not** enable multi-user access in the HTTP API or
iOS client, and is not a production cutover command.

## Model

The six core tables are `users`, `captures`, `recommendation_mentions`,
`recommendations`, `locations`, and `post_processing_cache`. Existing movie
enrichment and ingest history remain supporting tables.

- Captures, recommendations, mentions, enrichment, and ingest history have an
  explicit owner. Composite foreign keys reject cross-owner relationships.
- Recommendation identity is unique within an account, not globally.
- A Capture is a private input. Social captures reference a public post/result;
  direct/Siri captures can have no URL and a private original result.
- The shared cache holds one public post/version, durable processing state, and
  an immutable successful original result. No shared result is populated from
  Nathan's current mutable library.
- Removed mentions can retain their capture/output identity after their parent
  recommendation is removed. Implementing the transactional delete/restore
  operations is the next storage stage; schema support alone does not run them.
- New intentional shares have separate ingest request keys and histories.
  Delivery retries reuse an existing key. Request intent and mutation ordering
  are persisted for later workers to enforce.

See `multi_user_schema.sql` for columns, constraints, and indexes. The schema
uses `PRAGMA application_id = 0x4A4F544D` and `user_version = 1`.

## Rehearse against a database copy

Run from `v0`, using Python with SQLite JSON support:

```sh
python multi_user_migration.py \
  --source /absolute/path/to/current-snapshot.sqlite \
  --output /absolute/path/to/new-multi-user.sqlite \
  --owner-id nathan \
  --owner-name Nathan
```

The owner ID is a stable internal account identifier, not a login credential or
an authentication bypass. The owner is an explicit argument, never an implicit
default for future users.

The command:

1. Opens the source read-only and takes a consistent SQLite backup, including
   committed WAL data, into a private temporary file.
2. Validates the current single-user schema and database integrity. Unknown
   columns, tables, triggers, or views require explicit migration review.
3. Rebuilds the affected tables in one transaction and assigns private rows to
   the specified owner. Copies original values exactly, including IDs, edits,
   timestamps, source connections, failed Activity, and sequence high-water marks.
4. Checks every original field using per-table hashes, reconciles ownership,
   and runs SQLite integrity and foreign-key checks before commit.
5. Publishes a mode-0600 output file only after successful verification. It
   refuses to overwrite an existing output or migrate the source in place.
   Failed migration leaves the source untouched and publishes no partial file.

The JSON report contains counts and hashes, not source URLs or library content.
The internal `migrate_copy()` operation is idempotent on an already migrated
database for the same owner. The CLI deliberately requires a new output path.

## Legacy data and duplicates

All imported captures are marked `legacy_unverified`. Existing editable fields
and original payloads are preserved privately, and the shared cache starts empty.
Synthetic `legacy-mention:<id>` keys identify retained mentions without pretending
that historical ordinals reliably map to a new original extraction.

Existing duplicate captures are preserved and reported by capture ID. The
normal unique owner/post index excludes these unverified historical rows. This
is an explicit migration exception, not permission for new duplicate saves.
Account-scoped ingest must inspect and reconcile matching legacy captures before
creating/promoting a normal capture. It must never discard an older capture's
surviving mentions or restore old deletions merely because a source is reused.

If baseline provenance cannot be established, retain the user's current library
and process a fresh shared baseline for a later requester. Reconciliation and
intentional restoration of ambiguous legacy captures must be completed and
tested before production cutover.

## Legacy API guard

`store._connect()` rejects migrated databases by application marker or ownership
columns. This intentionally prevents the current shared-token API from listing
or deleting new users' data. It continues to work normally with the unchanged
single-user database. The migration is not called by `init_db()`.

Do not point a deployed app or legacy backfill script at the migrated database.
Database constraints cannot replace account-scoped read/write authorization.
The old application is not a rollback implementation for the new schema.

## Rehearsal evidence — September 15, 2026

A consistent read-only snapshot of the running Fly database was migrated
locally. Production was not changed.

| Preserved table | Rows |
|---|---:|
| Captures | 432 |
| Recommendations | 598 |
| Recommendation mentions | 651 |
| Locations | 496 |
| Movie enrichments | 3 |
| Ingest runs | 448 |
| Ingest events | 1,711 |

Every original field matched its before-migration hash. Read-only replay of the
existing library/source/Activity projections on the isolated one-owner copy
also produced identical responses. This replay is a test-only guard bypass;
it is never enabled in the application.

One duplicate public-capture group was found (IDs 283 and 420); both were
retained. Zero original library rows were promoted into the shared cache.
Foreign-key and integrity checks passed.

## Next implementation stages

1. Account-scoped reads/library mutations and the session-verifier boundary are
   implemented in [the opt-in account API](ACCOUNT_API.md). Production identity
   integration and account ingest remain required before cutover.
2. Shared result validation and job claims; private materialization;
   mention/map deletion and deliberate re-share restoration, including races.
3. Legacy-capture reconciliation, source identity/output-key validation, and
   provider-data retention handling. JSON item keys require application-level
   validation; SQL currently validates the envelope, not each output's contents.
4. iOS/extension sessions, account-scoped local state, and fast ingest acceptance.
5. Production identity integration, end-to-end isolation and recovery tests,
   then a separately rehearsed cutover with a write pause and fresh backup.

Do not claim the app supports a second account until these dependent stages
are implemented. Temporary-file cleanup and provider costs remain independent
of the schema migration. This command copies database records, not downloaded
media files; existing legacy JSON remains private pending baseline validation.
