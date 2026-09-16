# Multi-user implementation audit — September 16, 2026

## Conclusion

The audit found regressions; it does not support a blanket claim that every prior
choice was correct. The account/data foundation has meaningful isolation and
migration evidence. The notification workaround was the wrong interim choice
for the requested behavior and has been removed from the draft.

Scope: changes from the pre-multi-user revision `26588ed` through the deployed
`6e316de`, the notification draft, native clients, migration/reconciliation,
Firebase verification, processing, deletion, deployment configuration and costs
introduced by the architecture. Production was inspected read-only. No live
library mutations, account deletions, service changes, or deployments occurred
as part of this audit.

## Findings and corrections

| Finding | Impact | Correction in this draft |
|---|---|---|
| Detailed notifications were replaced with acceptance alerts. | A queued save looked like the final result and no later result alert followed. | Restore the extension-owned ordinary request and local result notification. The API accepts durably, then optionally waits for the result. |
| Proposed background download polling was presented too confidently. | Repeated background transfers can incur increasing iOS scheduling delays; not dependable prompt delivery. | Remove its coordinator, App Groups, background callbacks and Keychain accessibility change. APNs after membership approval is the next delivery step. |
| Movie enrichment was omitted from the new worker. | Newly saved movies could lose film identification/Letterboxd enrichment; existing rows remained intact. | Reuse the existing Wikidata provider and movie table with account-scoped reads/writes in the existing worker. Failures preserve the recommendation, and deletion wins during lookup. No historical bulk backfill. |
| Historical failed saves were categorically rejected by Retry. | Migrated failure rows could advertise retry but return conflict. | Explicit retry adopts the original Activity row into durable processing. It resolves old short links outside the write transaction and never grants re-share restoration rights. |
| FCM was treated as selected because Auth uses Firebase. | An unrelated vendor dependency was implied. | Document direct APNs from Fly; FCM is optional, unselected and absent from dependencies. |
| Release documentation still described completed integration as pending. | Could mislead future work into repeating migration or provisioning. | Correct active release documentation and distinguish outstanding device checks. |

Apple explicitly documents the delay risk in sequential background downloads:
[Downloading files in the background](https://developer.apple.com/documentation/foundation/downloading-files-in-the-background).

## What was verified

- **Library preservation:** the cutover records full old/new API-response parity:
  600 recommendations, 433 sources and 450 Activity records. Every original stored
  field was compared during migration/reconciliation. Binding used the explicitly
  confirmed Firebase UID, never email or first-registration order. The owner also
  confirmed the library on the installed phone.
- **Current live state:** one Fly machine, the same volume, the account service,
  602 recommendations, 435 captures, and two completed post-cutover saves. SQLite
  integrity is `ok`, foreign-key violations are zero, the original backup's SHA-256
  is unchanged, and the worker temporary-media directory contains no files.
  This is a point-in-time check, not proof against every future failure.
- **Account isolation:** all data routes derive the owner from verified identity;
  nested reads and mutations are scoped, with ownership enforced by composite
  foreign keys. Tests exercise foreign IDs, mixed-owner batches, account deletion,
  concurrent operations, stale tokens and stale client responses. The public cache
  receives fresh public extraction, never historical private edits or Siri context.
- **Deletion/restoration:** removing the last mention removes its recommendation;
  map deletion removes all its owned mentions. Retries preserve deletions and
  deliberate later shares restore removed outputs while preserving active edits.
- **Sessions:** official Firebase verification checks signature/project/expiry and
  revocation. Native SDK renewal is used; no arbitrary weekly logout was added.
  Apple linking uses the signed-in Firebase user, not email-based library merging.
- **Infrastructure:** SQLite remains on Fly. No Postgres, Firestore, Cloud Functions,
  FCM, separate queue service or automatic backup service was added. Apple Maps
  stays in place. The replacement Places key is already deployed. This does not
  make existing Fly, Places or Gemini usage free, or establish a hard spending cap.
- **Release control:** merges do not auto-deploy; deployment requires explicit
  workflow dispatch and successful CI for the chosen main revision.

## Validation of the corrected draft

- 318 backend tests passed; generated entry types and Python compilation passed.
- 15 native transport/response tests passed.
- Full unsigned simulator app and share-extension build passed.
- On a disposable copy of the actual cutover database, all 14 canonical historical
  failures queued through Retry without changing existing recommendations. The
  original snapshot stayed unchanged. The three shortened links need external
  resolution; success/outage handling is covered with injected resolver tests,
  not claimed as live TikTok availability verification.
- No schema migration is required by these corrections.

## Remaining limits and release checks

1. Test the restored request/local-notification flow on the physical phone. It
   preserves the prior mechanism, but iOS may terminate the extension, and a later
   automatic retry may finish after its request ends. Server processing survives;
   a later local alert is not guaranteed. APNs is future implementation, not a toggle.
2. Real Apple sign-in/linking awaits membership/provider setup. A second real
   account/device and real provider account-deletion/revocation still need an
   end-to-end check; multi-account tests are not a substitute for those.
3. This remains a small MVP: one SQLite volume/worker, no added automatic backups
   per the user's choice, and no per-user processing quotas. Existing downloader
   subprocesses also lack a global wall-clock deadline. Review those limits before
   opening unrestricted registration; this audit is not a public-scale load test.
4. The old Google project remains pending dependency cleanup. Issue #131 tracks
   the existing Google Places/Apple Maps question; no map-provider change was made.
5. The corrected draft has not been deployed or installed. Deploy the waiting-result
   endpoint before the matching phone build. Never restore an old snapshot over
   the current database to roll back application code.
