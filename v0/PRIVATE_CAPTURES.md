# Private saves, deletion, and restoration

`CaptureStore` accepts durable user intent and materializes a validated original
result into that user's library. It performs no downloading or model calls and
does not publish public cache results. The [shared worker](SHARED_PROCESSING.md)
connects these operations to processing; authenticated HTTP ingestion now durably queues requests before returning 202.

## Operations

- `accept_public(url, request_key)` identifies a canonical public post. A new key
  creates a new private Activity request. An existing capture is reused; its
  baseline stays pinned. Reusing the key returns the same operation without
  advancing its accepted sequence. Using a key for different input is a conflict.
- `accept_direct(text, request_key, channel=..., context=...)` creates a private
  capture for a new Siri/typed invocation. A retry reuses the same capture. Text,
  channel, and bounded location context must match when reusing a key.
- `materialize(ingest_id, public_cache_id=... OR private_result=...)` validates
  and saves all outputs, pins the capture, and completes its Activity in one
  transaction. Account status and request ownership are checked inside it.
- `DELETE /api/v1/mentions/{id}` removes an owned mention. The parent is deleted
  only when no active mentions remain. Existing map/batch deletion removes all
  attached mentions. Removal clears editable payload but retains output identity
  and ordering. Source captures and shared results remain.

Automatic delivery/recovery uses the existing request key and accepted sequence.
A late completion cannot restore a mention deleted after that request was
accepted. A new deliberate post share can restore older removed mentions from
that capture's pinned baseline; active fields and private location edits remain
untouched. A re-share never restores another post's or a Siri capture's mentions.
A new post or new Siri invocation can independently create a recommendation.

Completed requests return current results without repeating materialization.
Failed Activity deletion now leaves a hidden cancelled request identity and
removes its events/error payload. This prevents a replayed old key from becoming
a new deliberate share. Cancelled requests are absent from Activity and cannot
be completed or retried. Account deletion cascades through these records too.

## Result contract and storage

`capture_result.py` validates a bounded result envelope: display metadata and a
list of uniquely keyed outputs. Each output has original extracted fields, a
resolution status, and optional place/candidate **IDs**. Unknown fields, raw
provider/media payloads, duplicate output keys, non-finite numbers, and oversized
results are rejected. Place display/coordinate lookup data stays in `locations`;
it is not embedded in the immutable original result.

Public captures reference the shared result without copying its metadata or
original extraction. Account reads join its metadata when needed. Direct
captures keep their original result privately. Active/removed output keys and
ordinals must match the pinned result exactly before restoration. Existing
legacy captures are not silently promoted; acceptance reports a reconciliation
conflict until the next stage handles their historical provenance.

## Schema version 2

A post may produce multiple mentions of the same place. An edit followed by a
re-share may also legitimately group two of that post's outputs together. Remove
the obsolete unique `(recommendation, capture)` index while retaining unique
`(capture, output_key)` and `(capture, ordinal)` lifecycle identities. Source
counts count distinct captures. No tables or columns are added.

The offline migration now creates version 2. Applying it to a version-1 copy
transactionally drops that one index, validates ownership/integrity, and advances
the version. The source file remains unchanged. There is no automatic startup
migration; the live database is still single-user.

When a capture has several mentions in one recommendation, location confirmation
accepts an optional `mention_id` alongside `candidate_id`. An ambiguous request
without a mention ID returns 409 instead of selecting an arbitrary mention.

## Validation

The restoration tests cover two-user copies, retry identity, late/out-of-order
completion, preserved edits, post-only restoration, map/mention deletion, private
Siri inputs, empty success, invalid baselines, version pinning, rollback,
concurrency, cancellation replay, legacy conflicts, same-post grouping, API
ownership, and the version-1 upgrade. Run `python -m unittest discover -s tests`.
