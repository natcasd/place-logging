-- SQLite multi-user foundation. Applied only by multi_user_migration.py.
-- The legacy API cannot use this schema until account-scoped storage is ready.

CREATE TABLE users (
  id TEXT PRIMARY KEY NOT NULL CHECK (length(trim(id)) > 0),
  display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
  mutation_sequence INTEGER NOT NULL DEFAULT 0 CHECK (mutation_sequence >= 0),
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE post_processing_cache (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  platform TEXT NOT NULL CHECK (platform IN ('instagram', 'tiktok', 'youtube')),
  post_id TEXT NOT NULL CHECK (length(trim(post_id)) > 0),
  canonical_url TEXT NOT NULL,
  processing_version TEXT NOT NULL CHECK (length(trim(processing_version)) > 0),
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'processing', 'retry_scheduled', 'completed', 'failed')),
  result_json TEXT,
  attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  next_retry_at TIMESTAMP,
  lease_token TEXT,
  lease_expires_at TIMESTAMP,
  error_type TEXT,
  error_message TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP,
  UNIQUE(platform, post_id, processing_version),
  UNIQUE(id, platform, post_id),
  CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
  CHECK (
    (status = 'completed' AND completed_at IS NOT NULL AND result_json IS NOT NULL
      AND CASE WHEN json_valid(result_json) THEN
        json_type(result_json) = 'object'
        AND COALESCE(json_type(result_json, '$.mentions') = 'array', 0)
      ELSE 0 END)
    OR (status != 'completed' AND result_json IS NULL AND completed_at IS NULL)
  )
);

CREATE TRIGGER immutable_completed_post_result
BEFORE UPDATE ON post_processing_cache
WHEN OLD.status = 'completed' AND (
  NEW.id IS NOT OLD.id OR NEW.platform IS NOT OLD.platform
  OR NEW.post_id IS NOT OLD.post_id OR NEW.canonical_url IS NOT OLD.canonical_url
  OR NEW.processing_version IS NOT OLD.processing_version
  OR NEW.result_json IS NOT OLD.result_json OR NEW.status IS NOT OLD.status
  OR NEW.completed_at IS NOT OLD.completed_at
)
BEGIN
  SELECT RAISE(ABORT, 'Published post extraction is immutable; use a new processing version');
END;

CREATE TABLE captures (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  vertical TEXT NOT NULL,
  source_url TEXT,
  raw_payload_json TEXT,
  llm_output_json TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  input_kind TEXT NOT NULL CHECK (input_kind IN ('public_post', 'direct', 'legacy')),
  capture_channel TEXT NOT NULL,
  source_platform TEXT,
  source_post_id TEXT,
  post_cache_id INTEGER,
  input_text TEXT,
  context_json TEXT CHECK (context_json IS NULL OR json_valid(context_json)),
  private_result_json TEXT,
  materialization_state TEXT NOT NULL DEFAULT 'pending'
    CHECK (materialization_state IN ('pending', 'complete', 'legacy_unverified')),
  last_submitted_at TIMESTAMP,
  UNIQUE(user_id, id),
  FOREIGN KEY(post_cache_id, source_platform, source_post_id)
    REFERENCES post_processing_cache(id, platform, post_id),
  CHECK (
    (input_kind = 'public_post' AND source_url IS NOT NULL
      AND source_platform IS NOT NULL AND source_post_id IS NOT NULL
      AND length(trim(source_post_id)) > 0
      AND source_platform IN ('instagram', 'tiktok', 'youtube')
      AND (private_result_json IS NULL OR (capture_channel = 'legacy' AND post_cache_id IS NULL)))
    OR (input_kind = 'direct' AND post_cache_id IS NULL
      AND source_platform IS NULL AND source_post_id IS NULL)
    OR (input_kind = 'legacy' AND post_cache_id IS NULL
      AND source_platform IS NULL AND source_post_id IS NULL
      AND private_result_json IS NULL AND materialization_state = 'legacy_unverified')
  ),
  CHECK (private_result_json IS NULL OR CASE WHEN json_valid(private_result_json) THEN
    json_type(private_result_json) = 'object'
    AND COALESCE(json_type(private_result_json, '$.mentions') = 'array', 0)
    ELSE 0 END),
  CHECK (materialization_state != 'complete'
    OR (input_kind = 'public_post' AND (post_cache_id IS NOT NULL
      OR (capture_channel = 'legacy' AND private_result_json IS NOT NULL)))
    OR (input_kind = 'direct' AND private_result_json IS NOT NULL))
);

CREATE UNIQUE INDEX idx_captures_owner_source
  ON captures(user_id, source_platform, source_post_id)
  WHERE input_kind = 'public_post' AND materialization_state != 'legacy_unverified'
    AND (capture_channel != 'legacy' OR materialization_state != 'complete');
-- Historical duplicate captures keep their IDs and mentions until explicitly
-- reconciled. Account-scoped ingest must inspect them before creating/promoting
-- a normal capture; the migration must not silently merge personal history.

CREATE TRIGGER completed_capture_requires_published_cache_insert
BEFORE INSERT ON captures
WHEN NEW.materialization_state = 'complete' AND NEW.input_kind = 'public_post'
  AND NEW.private_result_json IS NULL
  AND NOT EXISTS (SELECT 1 FROM post_processing_cache
    WHERE id = NEW.post_cache_id AND status = 'completed')
BEGIN
  SELECT RAISE(ABORT, 'Capture requires a published post result');
END;

CREATE TRIGGER completed_capture_requires_published_cache_update
BEFORE UPDATE ON captures
WHEN NEW.materialization_state = 'complete' AND NEW.input_kind = 'public_post'
  AND NEW.private_result_json IS NULL
  AND NOT EXISTS (SELECT 1 FROM post_processing_cache
    WHERE id = NEW.post_cache_id AND status = 'completed')
BEGIN
  SELECT RAISE(ABORT, 'Capture requires a published post result');
END;

CREATE TRIGGER pin_completed_capture_baseline
BEFORE UPDATE ON captures
WHEN OLD.materialization_state = 'complete' AND (
  NEW.post_cache_id IS NOT OLD.post_cache_id
  OR NEW.private_result_json IS NOT OLD.private_result_json
  OR NEW.input_kind IS NOT OLD.input_kind
  OR NEW.source_platform IS NOT OLD.source_platform
  OR NEW.source_post_id IS NOT OLD.source_post_id
  OR NEW.materialization_state IS NOT OLD.materialization_state
  OR NEW.capture_channel IS NOT OLD.capture_channel
)
BEGIN
  SELECT RAISE(ABORT, 'Completed capture baseline is pinned');
END;

CREATE TABLE locations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  google_place_id TEXT NOT NULL UNIQUE,
  display_name TEXT,
  lat REAL,
  lng REAL,
  formatted_address TEXT,
  google_maps_url TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE recommendations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  entry_type TEXT NOT NULL,
  type_key TEXT NOT NULL,
  identity_key TEXT NOT NULL,
  location_id INTEGER REFERENCES locations(id),
  starts_at TEXT,
  ends_at TEXT,
  recurrence_text TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(user_id, id),
  UNIQUE(user_id, identity_key)
);

CREATE TABLE recommendation_mentions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  entry_id INTEGER,
  item_id INTEGER NOT NULL,
  ordinal INTEGER NOT NULL,
  source_name TEXT NOT NULL,
  source_type TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  dishes_json TEXT,
  why_its_cool TEXT,
  tags_json TEXT,
  timestamp_seconds REAL,
  slide_index INTEGER,
  resolution_status TEXT NOT NULL,
  resolution_candidates_json TEXT,
  location_query TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  output_key TEXT NOT NULL CHECK (length(trim(output_key)) > 0),
  removed_at TIMESTAMP,
  last_user_change_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_user_change_sequence >= 0),
  UNIQUE(user_id, id),
  UNIQUE(item_id, output_key),
  UNIQUE(item_id, ordinal),
  FOREIGN KEY(user_id, entry_id) REFERENCES recommendations(user_id, id)
    DEFERRABLE INITIALLY DEFERRED,
  FOREIGN KEY(user_id, item_id) REFERENCES captures(user_id, id) ON DELETE CASCADE,
  CHECK ((removed_at IS NULL AND entry_id IS NOT NULL)
    OR (removed_at IS NOT NULL AND entry_id IS NULL))
);

-- Multiple original outputs from one capture can group into one recommendation.
-- Their lifecycle identity remains (item_id, output_key), including removals.
CREATE INDEX idx_mentions_owner_capture ON recommendation_mentions(user_id, item_id);
CREATE INDEX idx_mentions_owner_recommendation ON recommendation_mentions(user_id, entry_id)
  WHERE removed_at IS NULL;
CREATE INDEX idx_recommendations_owner_location ON recommendations(user_id, location_id);

CREATE TABLE movie_enrichments (
  entry_id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,
  provider_id TEXT,
  resolved_title TEXT,
  release_year INTEGER,
  letterboxd_url TEXT,
  match_status TEXT NOT NULL,
  match_confidence REAL,
  checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id, entry_id) REFERENCES recommendations(user_id, id) ON DELETE CASCADE
);

CREATE TABLE ingest_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  source_url TEXT,
  source_platform TEXT NOT NULL DEFAULT 'other',
  status TEXT NOT NULL,
  stage TEXT NOT NULL,
  item_id INTEGER,
  result_json TEXT,
  error_type TEXT,
  error_message TEXT,
  failure_kind TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 1,
  max_attempts INTEGER NOT NULL DEFAULT 4,
  next_retry_at TIMESTAMP,
  last_retry_at TIMESTAMP,
  started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TIMESTAMP,
  idempotency_key TEXT NOT NULL CHECK (length(trim(idempotency_key)) > 0),
  intent TEXT NOT NULL CHECK (intent IN ('capture', 'reshare', 'legacy')),
  accepted_sequence INTEGER NOT NULL DEFAULT 0 CHECK (accepted_sequence >= 0),
  UNIQUE(user_id, id),
  UNIQUE(user_id, idempotency_key),
  FOREIGN KEY(user_id, item_id) REFERENCES captures(user_id, id) ON DELETE CASCADE
);

CREATE TABLE ingest_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ingest_run_id INTEGER NOT NULL,
  stage TEXT NOT NULL,
  status TEXT NOT NULL,
  message TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(user_id, ingest_run_id) REFERENCES ingest_runs(user_id, id) ON DELETE CASCADE
);

CREATE INDEX idx_ingest_runs_owner_updated ON ingest_runs(user_id, updated_at DESC);
CREATE INDEX idx_ingest_events_owner_run ON ingest_events(user_id, ingest_run_id, id);
CREATE INDEX idx_post_processing_due ON post_processing_cache(status, next_retry_at);
