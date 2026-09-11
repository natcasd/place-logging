# Entry type catalog implementation plan

## Model

`v0/entry_type_catalog.json` is the only hand-maintained definition of active
types. Each row contains the displayed name, the classifier boundary, its icon,
and an optional enrichment trigger. There are no hierarchy, plural-label,
alias, or type-specific field-schema layers.

Location and timing remain optional entry properties. A type never determines
whether an entry can resolve to a map location. `Unknown` entries with resolved
coordinates use the question-mark pin.

The backend loads and validates the catalog at startup. The extraction prompt
and response enum are derived from it. iOS uses a generated Swift table; CI can
run `python v0/generate_ios_entry_types.py --check` to prevent drift.

## Rollout gates

1. **Code integration:** ship catalog-derived extraction, enrichment dispatch,
   categories, and pins without rewriting existing data.
2. **Regression evaluation:** run `evaluate_entry_type_migration.py` against
   the 36 checked-in edge cases plus a stratified 40–60-row saved sample.
3. **Review:** inspect every changed saved row, paying special attention to old
   `Bar`, `Store`, `Movie`, and `Unknown` records. Tune only catalog definitions
   if a boundary is consistently wrong, then rerun the same sample.
4. **Targeted migration plan:** produce an ID-by-ID plan only for legacy types
   that must split. The plan must record the catalog hash and current type and
   must not modify location, timing, sources, names, or descriptions.
5. **Apply with safeguards:** after explicit approval, back up SQLite, verify
   every target row still matches the plan, update both canonical and legacy
   compatibility rows in one transaction, and abort on any mismatch.
6. **Post-apply checks:** compare counts, confirm no active extraction path can
   emit generic `Bar` or `Store`, and visually inspect each custom map symbol.

No production evaluation call or data backfill is part of the code-integration
step.
