# Diagnosis format

## Identification

Name the selected `entry_id`, `source_connection_id`, `item_id`, `ingest_id`, source URL, saved time, and current canonical entry. Note ambiguity in how the user identified it.

## Provenance

Trace only the stages established by stored data:

1. source metadata;
2. stored model output;
3. normalized source occurrence;
4. resolution query, candidates, and selected location;
5. canonical entry and its other source connections.

Call out missing historical fields such as model name, prompt version, raw pre-normalization response, complete candidate list, or tiebreaker response.

## Media observations

For each fresh Gemini probe, record the model, question, whether the complete source was analyzed, and observations with timestamps or slide indexes. Do not present a fresh explanation as the original model's reasoning.

## Ground truth

List the plausible saved-entry outcomes when the source and existing policy do not determine one answer. Separate the investigator's recommendation from the user's product decision. Record the agreed expected entry set, including an empty set, and why each recognized entity is included or excluded. Exclusion reasons stay in the investigation and do not become user-facing saved notes.

## Diagnosis

Choose one primary boundary: extraction, normalization, resolution, canonicalization, or insufficient evidence. Give a confidence level and cite the stored or freshly observed evidence that supports it. Mention plausible alternatives.

## Recommendation

Propose the smallest general prompt, code, provenance, or data change that addresses the boundary. Avoid rules containing the example's proper nouns. Specify a sanitized regression fixture and its expected entry set. Do not implement or mutate production unless separately requested.

## Candidate verification

When implementation was requested, report the candidate-prompt result on the complete source, an adjacent-case result that checks for overcorrection, deterministic tests, and any remaining mismatch. A successful fresh model run is forward-test evidence, not a deterministic regression test.
