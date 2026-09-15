# Durable public-post processing

`PostProcessingStore` and `PostProcessingWorker` connect accepted private
captures to one shared job per platform, post, and processing version. The
worker is explicitly constructed and can be injected into the account API
factory for managed startup/shutdown. The released legacy API never starts it. No schema changes or additional tables are needed.

## Lifecycle

1. Discover queued requests from active accounts and create missing shared jobs
   transactionally. The database is the queue; no in-memory subscription is
   required after accepting a save.
2. Claim a job with a random lease token and expiry. The default global limit
   is one active public job, appropriate for the current Fly machine.
3. Renew the lease during processing. Progress, failure, and publication must
   present the current, unexpired token. A replacement claim invalidates the
   previous worker, including its shared location writes.
4. Publish the validated original result once. Temporary media and raw provider
   blobs are excluded. Place IDs stay in the original result; the current
   allowlisted place display lookup is stored separately in `locations`.
5. Deliver to each account through `CaptureStore.materialize`. Each transaction
   checks account status, request ownership, and deletion order again. A crash
   between publication and delivery is recovered on the next worker iteration.

`public_processing_adapter.process_public_post` calls the existing extraction
pipeline with only the canonical public URL, a temporary directory, and a
progress callback. It cannot read private saved mentions or direct/Siri input.
Stable output keys are assigned before publication. Completed empty extraction
is a successful result; missing/failed extraction is not.

## Failure and recovery

Transient failures use the existing classification and bounded backoff policy,
including Retry-After. Four failed/crashed attempts stop automatic processing.
A new deliberate share can start a fresh attempt cycle. The failed prior request
stays failed; cancelled requests stay hidden and do not subscribe again.
Completed cache hits, including pinned older versions, require no extraction.

New saves arriving during processing join the same result. An account being
deleted or disabled does not cancel a job needed by another account. Delivery
conflicts for one private capture do not prevent another account's delivery.
Persisted error messages are fixed messages, never raw provider exceptions.

Downloads use token-specific directories under `jot-public-jobs`. They are
removed on success/failure. Recovery removes only directories without an active
lease, ignores unrelated directories and symlinks, and snapshots directory names
before checking leases so a concurrently started job cannot be removed.

Use the same processing version when restarting workers. A deliberate algorithm
version change must drain/reconcile unfinished older jobs before retiring their
worker; completed captures remain pinned regardless of the current version.

## Remaining integration and release requirements

- HTTP acceptance, Shortcuts URL handling, manual retry, and optional worker
  lifespan are implemented. Configure the final Firebase-authenticated service
  entrypoint before release.
- Reconcile historical captures conservatively before allowing their re-share.
  They currently return a conflict and are never fed into the shared cache.
- Complete client sessions, account deletion, operational limits, and cutover
  rehearsal. Firebase Authentication on Blaze with Apple/Google is selected; integration remains.
- Complete the existing Google Places retention/refresh and map-display review
  before release. Keeping place payloads out of the immutable result does not
  by itself establish a compliant lifetime for the mutable location lookup.

Validation uses temporary SQLite databases and injected processors, without
calling paid APIs. Worker tests cover concurrent claims, takeover fencing,
publication/delivery recovery, private isolation, retry bounds, cancellation,
media cleanup, adapter boundaries, location-write rollback, and cache versions.
