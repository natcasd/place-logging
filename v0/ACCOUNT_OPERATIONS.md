# Multi-user operations and release checks

These controls apply to the opt-in `firebase_service:create_app` entrypoint.
The production Docker command remains unchanged. Merging this code does not
switch the live service, migrate a library, or install a phone build.

## Queue and usage controls

Defaults: 20 pending saves per account, 200 across the service, 100 new saves per
account per rolling 24 hours, 1,000 across the service, and five manual retries
per save. Automatic worker attempts remain bounded separately. Change these with
`JOT_USER_PENDING_LIMIT`, `JOT_TOTAL_PENDING_LIMIT`, `JOT_USER_DAILY_SAVE_LIMIT`,
`JOT_TOTAL_DAILY_SAVE_LIMIT`, and `JOT_MANUAL_RETRY_LIMIT`; values must be positive.
These are queue/usage controls, not a dollar spending cap. Each processor attempt
can make several provider calls. Account deletion removes its usage history.

Limits run inside the acceptance transaction. Concurrent requests cannot exceed
capacity; hiding failed Activity does not reset new-save counts. A replay of an
accepted request key succeeds even when capacity is full. Limits return 429 with
Retry-After and preserve sign-in. Existing libraries stay readable.

Set `JOT_PROCESSING_PAUSE_FILE` to a dedicated absolute path, for example
`/data/jot-processing.paused`. Creating that file pauses new save acceptance,
manual retries and new provider jobs. Removing it resumes processing. In-flight
jobs finish, existing results can still deliver, and reads/deletion keep working.
This is a graceful processing pause, not a global write lock for cutover.

Every minute the service logs aggregate pending depth, oldest pending age,
recent failures, active jobs and pending account deletions. Legacy pipeline
payload/URL logs are suppressed; numeric Gemini usage remains. Counts of saves
are not counts of model calls. HTTP access/debug logs are disabled for this
entrypoint. Alert routing and provider billing budgets remain deployment setup.

## Deletion-safe backups

`JOT_DELETION_JOURNAL_PATH` must identify an existing, private SQLite journal on
persistent storage, separate from the application database. Initialize it once
before the first account-service launch:

```sh
python account_recovery.py init-journal --journal /data/jot-deletions.sqlite
```

Never initialize an empty replacement during recovery. The journal retains only
Firebase project, UID and deletion-request time. It contains no names, media,
credentials or library content. Its purpose is to prevent restoring an old
backup from reopening an account deleted after that backup. Keep these minimal
records while any backups or restoration candidates can contain those accounts.

Deletion intent commits to the journal before the API acknowledges it. A crash
between journal and application commits is recovered by replaying the intent.
Startup reapplies deletions before serving requests; the deletion worker also
reapplies them before removing Firebase identities and private rows. Failure to
write the journal prevents successful acceptance. Missing journal fails startup.

Create consistent SQLite backups with the backup API, including committed WAL
contents; never copy just a live `.sqlite` file and omit its WAL:

```sh
python account_recovery.py backup --source /data/jot-accounts.sqlite --output /private-backups/accounts-UNIQUE.sqlite
python account_recovery.py backup-journal --journal /data/jot-deletions.sqlite --output /private-backups/deletions-UNIQUE.sqlite
```

Commands require a new output, use mode 0600, validate integrity and never
replace the source. Copy the app backup and latest journal to restricted off-host
storage. A same-volume backup alone does not survive losing the volume.
Before release, configure and verify the off-host backup job and retention:
proposed initial policy is daily application snapshots retained 30 days, with the
latest deletion journal durably copied after each deletion. This scheduling,
remote storage and expiry enforcement are **not provisioned by this code**.
An old journal can miss later deletions; do not restore unless journal freshness
is established. Backup records are not claimed to be instantly erased.

Prepare recovery while the service is stopped, using the latest journal:

```sh
python account_recovery.py restore-copy --source /private-backups/accounts-OLD.sqlite --output /data/jot-restored-NEW.sqlite --journal /data/jot-deletions.sqlite --project jot-app-20260915
```

This marks later-deleted accounts unavailable in the candidate without changing
the source. Startup/worker completes their deletion. Verify surviving accounts,
row counts, relationships and API parity before manually selecting the candidate.
The command never switches the live database. Restoring a historical backup loses
later saves unless those writes are reconciled; prefer a forward fix after new
writes have been accepted. Retain the newer DB for reconciliation.

## Coordinated release order

1. Finish Apple Developer approval/provider configuration and test Apple/Google
   login, Apple revocation, Keychain sharing, logout/relaunch and account switches
   on signed builds. Test two independent accounts, including the same post,
   independent mention/map deletion, deliberate re-share and extension ingestion.
2. Stage backend-only credentials with `firebaseauth.users.get` and
   `firebaseauth.users.delete`; no credential goes in the mobile app or Git.
   Configure the new project's Places key and test actual resolver behavior.
   Keep Apple Maps as requested.
3. Validate the backup destination, deletion-journal replication and retention.
   Prepare the replacement Fly command and env, preserving the old release and
   DB paths. Disable old global routes by running only the new factory entrypoint.
4. Stop/drain the live service for a coordinated write pause. Take a fresh,
   consistent production backup. Reconcile a new schema-5 copy and explicitly
   bind Nathan's freshly verified Firebase UID with `bind_legacy_account.py`.
   Never assign the old library to the first registrant or infer by email.
5. Compare every stored field and authenticated API responses against the final
   snapshot. Verify Nathan sees his existing library and a second account starts
   empty. Initialize the journal once and rehearse recovery on separate copies.
6. Deploy the backend and matching phone builds together, then verify new saves,
   restarts, deletions, monitoring and backups. Merging alone remains non-deploying.
7. Verify the old Google project has no remaining Places, OAuth, build or runtime
   dependencies before retiring it. Keep it until the live replacement is proven.

Unverified release steps remain gates; passing mocked tests and an unsigned
simulator build does not establish real-device or live-cutover readiness.
