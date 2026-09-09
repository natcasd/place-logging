---
name: investigate-extraction
description: Investigate why a particular recommendation or place was extracted, resolved, or merged by Place Logger, and iterate on a fix when requested. Use when a saved entry looks wrong, unfamiliar, overly broad, unsupported by its Instagram or YouTube source, or otherwise needs an evidence-backed production diagnosis from an entry name, source URL, item ID, ingest ID, entry ID, or source-connection ID.
---

# Investigate Extraction

Trace one suspicious saved result through production data and, when needed, ask Gemini targeted questions about the complete original media. Separate source interpretation, product policy, and implementation. Keep the investigation read-only unless the user separately asks to change code or data.

## Workflow

1. Locate the repository root and run all bundled scripts from there.
2. Resolve the user's identifier with `scripts/export_case.py find`. Prefer `source_connection_id`, which identifies one extracted entry from one source. If several records match a name, show the compact candidates and ask the user to select one.
3. Export the selected case with `scripts/export_case.py export`. Treat the exported database values as the historical record; do not infer missing provenance from the current code.
4. Establish the current baseline before interpreting the media deeply. Inspect Git history or persisted provenance, then rerun the complete source through `scripts/rerun_current_extraction.py` after obtaining approval for the model call. Compare the historical and deployed-current entry sets field by field. Do not assume the historical result still reproduces.
5. If the deployed extractor now produces an outcome already established as correct by product policy, classify the case as historical-only and skip deeper model calls unless a material question remains.
6. If the current result remains suspicious, classify the first plausible failure boundary:
   - `extraction`: Gemini introduced an unsupported or incidental recommendation.
   - `normalization`: deterministic post-processing altered or retained the wrong field.
   - `resolution`: Google Places or the Gemini tiebreaker selected the wrong location.
   - `canonicalization`: a source occurrence merged into the wrong canonical entry.
   - `insufficient_evidence`: the stored trace cannot establish the boundary.
7. Build an evidence inventory from the stored source and fresh media. Separate directly observed facts from inferences. If interpretation beyond the current extraction is needed, propose one targeted question and obtain user approval before running `scripts/probe_media.py`; let each answer determine whether another probe is necessary.
8. Pass a ground-truth gate before recommending a fix:
   - Apply an existing explicit product rule when it determines the expected output.
   - Otherwise present the plausible saved-entry outcomes, the evidence for each, and the product tradeoff. Make a recommendation, but ask the user to choose when more than one behavior is reasonable.
   - Record the agreed expected entry set, including an empty set, and a short exclusion reason for every recognized-but-omitted entity. Exclusion reasons are diagnostic evidence, not user-facing saved notes.
9. Only after the ground truth is established, inspect the relevant code paths in `v0/pipeline.py`, `v0/store.py`, and `v0/ingest_service.py`. Identify the smallest general rule that explains the case without naming the example entities.
10. When the user requested implementation, edit the prompt or code, add deterministic regression coverage, and rerun the same complete source with the candidate prompt. Then rerun at least one adjacent case that could reveal overcorrection. Do not deploy or mutate stored production data without separate authorization.
11. Report the historical result, deployed baseline, evidence inventory, agreed ground truth, failure boundary, candidate result, adjacent-case result, remaining uncertainty, and tests. State that reacquired media is not normally guaranteed to match the originally processed bytes.

## Commands

Find a record:

```bash
python3 .agents/skills/investigate-extraction/scripts/export_case.py find --query "Ito"
python3 .agents/skills/investigate-extraction/scripts/export_case.py find --entry-id 491
```

Export one source occurrence:

```bash
python3 .agents/skills/investigate-extraction/scripts/export_case.py export --source-connection-id 523
```

Probe the complete media after approval:

```bash
python3 .agents/skills/investigate-extraction/scripts/probe_media.py \
  --source-url "https://www.instagram.com/reel/example/" \
  --question "Is Trakt independently recommended? Cite timestamped evidence and distinguish speech, visible text, caption, and inference."
```

Rerun the exact deployed extractor after approval:

```bash
python3 .agents/skills/investigate-extraction/scripts/rerun_current_extraction.py \
  --source-url "https://www.instagram.com/reel/example/"
```

Test the prompt defined in a local candidate `pipeline.py` against the same production media and model environment:

```bash
python3 .agents/skills/investigate-extraction/scripts/rerun_current_extraction.py \
  --source-url "https://www.instagram.com/reel/example/" \
  --candidate-pipeline v0/pipeline.py
```

Pass `--model` only when the user requests a particular diagnostic model. Otherwise use `GEMINI_INVESTIGATION_MODEL`, then the deployed `GEMINI_MODEL` fallback.

All scripts default to the Fly app `place-logging`. Use `--app` only for an explicitly selected environment. Never print, copy, or store API keys or Fly tokens.

## Probe design

Keep the complete media attached for broad context. Ask for observable evidence rather than chain-of-thought. Useful probes include:

- inventory every named entity and classify its role in the post;
- trace every appearance or mention of the suspicious name;
- decide whether it is independently recommended or merely incidental, contextual, a host venue, or background text;
- identify whether a temporary offering has an explicit current, upcoming, or recurring opportunity, rather than merely a past appearance;
- test whether a former employer, credential, comparison point, or quality benchmark was mistaken for a recommendation;
- identify geographic evidence and distinguish a venue from a city or region;
- look for evidence contradicting the suspicious extraction;
- produce the minimal supported recommendation set under the production inclusion rules.

Request timestamps or slide indexes, evidence modality, short observed wording, interpretation, and uncertainty. Use a focused follow-up or inspect extracted frames when an answer is ambiguous.

## Report contract

Follow the concise structure in [references/diagnosis.md](references/diagnosis.md). Separate facts stored at ingest time, fresh Gemini observations, Codex inference, user-selected product policy, and candidate implementation results. Do not silently repair or delete the record.
