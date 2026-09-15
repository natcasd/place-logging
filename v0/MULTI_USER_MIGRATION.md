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
  recommendation is removed. Transactional delete/restore operations are
  implemented in `CaptureStore`; migration itself never restores a removal.
- New intentional shares have separate ingest request keys and histories.
  Delivery retries reuse an existing key. Request intent and mutation ordering
  are persisted for later workers to enforce.

See `multi_user_schema.sql` for columns, constraints, and indexes. The schema
uses `PRAGMA application_id = 0x4A4F544D` and `user_version = 3`.

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

## Reconcile historical captures on another offline copy

The schema-only command above preserves captures as `legacy_unverified`. The
release preparation command also freezes verified **private** restoration
baselines, still without modifying its source:

```sh
python legacy_reconciliation.py \
  --source /absolute/path/to/current-snapshot.sqlite \
  --output /absolute/path/to/new-reconciled.sqlite \
  --owner-id nathan \
  --owner-name Nathan
```

Original extraction ordinals and names must match surviving mentions. Current
private categories and place resolutions remain private: they are never offered
to another user through the shared cache. Unknown correspondence or malformed
original extraction fails the whole copy without publishing an output.

Missing outputs receive hidden removal identities. All surviving IDs, edited
fields, timestamps, and Activity remain unchanged, checked field by field.
Neither migration nor an automatic retry restores removed outputs. A later
deliberate re-share can restore them; if an old deleted place resolution is no
longer available, the restored mention is unresolved rather than guessed.

The private baseline uses the existing `private_result_json` column. Schema 3
permits this only for historical public captures with no shared-cache reference.
New public captures still use shared results, and direct/Siri captures remain
private. Completed baselines cannot be replaced or changed into another channel.
No new tables or columns are added.

Historical duplicate captures retain their IDs and mentions. A deliberate share
restores the matching private source group atomically, preserving active edits.
The unique owner/post index excludes these complete historical captures as well
as unreconciled ones; new normal captures retain their uniqueness constraint.
Another account saving that post uses fresh public processing, never these
historical private baselines.

Both offline commands accept version-1/version-2 account snapshots and preserve
every stored field and sequence high-water mark while rebuilding constraints.
Unknown tables, columns, triggers/views, or additional indexes require review.
Nothing runs on server startup or changes the deployed single-user database.

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

## Remaining release stages

Account-scoped reads/mutations, durable ingestion, shared processing, ordered
restoration, and conservative legacy reconciliation are implemented. Remaining:

1. Firebase identity verification and explicit binding of Nathan's verified UID
   to his existing internal account. Never assign it to the first registrant.
2. iOS/extension sessions, account-scoped local state, and async ingest handling.
3. Account deletion, provider-data retention/refresh, and operational limits.
4. End-to-end isolation and recovery tests, then a separately rehearsed cutover
   with a write pause and a fresh backup. The old application cannot safely run
   against the account schema; rollback must use the matching code and database.

### Reconciliation rehearsal

The same saved production snapshot was reconciled on September 15, 2026.
All 432 captures were reconciled, including both captures in the duplicate group.
The 14 absent original outputs stayed hidden. Of all restoration outputs, 26
have no usable resolved place (including those 14); active library data remains
unchanged. All 598 recommendations, 432 sources, and 448 Activity responses
matched the original API projections exactly. Original-file SHA-256 was unchanged,
and upgrading the prior version-2 snapshot produced the same reconciliation
counts. This is rehearsal evidence, not a claim that production was migrated.
