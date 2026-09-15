"""Freeze private restoration baselines on a new, verified offline copy.

Historical edited resolutions never enter the shared post cache. Missing old
outputs stay removed; only a later deliberate re-share can restore them. When a
deleted output's original location is unavailable, restoration is unresolved.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from account_store import AccountStore
from capture_result import OriginalEvidence, validate_result
from multi_user_migration import _legacy_columns, prepare_copy
from store import _normalize_extracted_for_storage


def _original_rows(con, user_id):
    """Snapshot existing visible records, keyed by ID (not just row counts)."""
    result = {}
    for table, columns in _legacy_columns().items():
        fields = ', '.join('"' + c + '"' for c in columns)
        key = 'entry_id' if table == 'movie_enrichments' else 'id'
        where = '' if table == 'locations' else ' WHERE user_id = ?'
        params = () if table == 'locations' else (user_id,)
        result[table] = {row[key]: tuple(row[c] for c in columns)
                         for row in con.execute(f'SELECT {fields} FROM {table}' + where, params)}
    return result


def reconcile_account(db_path: Path, user_id: str) -> dict:
    account = AccountStore(db_path, user_id)
    with account._transaction(write=True) as con:
        before = _original_rows(con, user_id)
        captures = con.execute("SELECT * FROM captures WHERE user_id = ? AND materialization_state = 'legacy_unverified' ORDER BY id",
                               (user_id,)).fetchall()
        reconciled = removed = unresolved = 0
        for capture in captures:
            if capture['input_kind'] != 'public_post' or capture['capture_channel'] != 'legacy':
                raise ValueError(f"Capture {capture['id']} requires manual source reconciliation")
            raw = capture['llm_output_json']
            if raw is None or len(raw.encode()) > 1_000_000:
                raise ValueError(f"Capture {capture['id']} has no bounded original extraction")
            originals = json.loads(raw)
            if not isinstance(originals, list) or len(originals) > 200:
                raise ValueError(f"Capture {capture['id']} has invalid original outputs")
            mentions = {r['ordinal']: r for r in con.execute('''
                SELECT m.*, l.google_place_id FROM recommendation_mentions m
                LEFT JOIN recommendations r ON r.id = m.entry_id AND r.user_id = m.user_id
                LEFT JOIN locations l ON l.id = r.location_id
                WHERE m.user_id = ? AND m.item_id = ?
            ''', (user_id, capture['id']))}
            if any(i < 0 or i >= len(originals) for i in mentions):
                raise ValueError(f"Capture {capture['id']} has unmatched historical mentions")
            outputs = []
            for ordinal, original in enumerate(originals):
                if not isinstance(original, dict):
                    raise ValueError(f"Capture {capture['id']} has invalid evidence")
                evidence = _normalize_extracted_for_storage(original)
                evidence = {k: v for k, v in evidence.items() if k in OriginalEvidence.model_fields}
                evidence.setdefault('description', evidence.get('why_its_cool') or '')
                for key in ('location_query', 'starts_at', 'ends_at', 'recurrence_text'):
                    if evidence.get(key) in ('', None):
                        evidence.pop(key, None)
                row = mentions.get(ordinal)
                if row is not None and row['removed_at'] is None:
                    normalize = lambda value: ' '.join(str(value).split()).casefold()
                    if normalize(row['source_name']) != normalize(evidence.get('extracted_name', '')):
                        raise ValueError(f"Capture {capture['id']} has ambiguous output correspondence")
                    # Categories were migrated/edited independently of the raw
                    # extraction. Preserve the current private category as the
                    # restoration default; never publish it as shared evidence.
                    evidence['type_name'] = row['source_type']
                key = row['output_key'] if row is not None else f"legacy-output:{capture['id']}:{ordinal}"
                output = {'key': key, 'extracted': evidence, 'status': 'unresolved'}
                # These are private historical resolutions, not public provenance.
                if row is not None and row['removed_at'] is None:
                    if row['google_place_id']:
                        output.update(status='resolved', place_id=row['google_place_id'])
                    elif row['resolution_status'] == 'not_applicable':
                        output['status'] = 'not_applicable'
                    elif row['resolution_status'] == 'needs_review':
                        candidates = json.loads(row['resolution_candidates_json'] or '[]')
                        output.update(status='needs_review', candidate_ids=list(dict.fromkeys(
                            c['id'] for c in candidates if isinstance(c, dict) and c.get('id'))))
                if output['status'] == 'unresolved':
                    unresolved += 1
                outputs.append(output)
            baseline = validate_result({'mentions': outputs})
            for ordinal, output in enumerate(baseline.mentions):
                if ordinal in mentions:
                    continue
                # Preserve absent recommendations without inventing an old place
                # or making migration itself a deliberate restoration request.
                con.execute('''INSERT INTO recommendation_mentions (user_id, item_id, ordinal,
                    output_key, source_name, source_type, resolution_status, removed_at,
                    last_user_change_sequence, created_at)
                    VALUES (?, ?, ?, ?, '', '', 'unresolved', CURRENT_TIMESTAMP,
                        (SELECT mutation_sequence FROM users WHERE id = ?), ?)''',
                            (user_id, capture['id'], ordinal, output.key, user_id, capture['created_at']))
                removed += 1
            con.execute("UPDATE captures SET private_result_json = ?, materialization_state = 'complete' WHERE user_id = ? AND id = ?",
                        (baseline.model_dump_json(), user_id, capture['id']))
            reconciled += 1
        after = _original_rows(con, user_id)
        for table, rows in before.items():
            if any(after[table].get(key) != value for key, value in rows.items()):
                raise ValueError('Reconciliation changed an existing library record')
        if con.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Reconciliation violated database ownership')
        return {'reconciled_captures': reconciled, 'preserved_removed_outputs': removed,
                'unresolved_restoration_outputs': unresolved, 'existing_records_preserved': True}


def prepare_reconciled_copy(source: Path, output: Path, *, owner_id: str, owner_name: str) -> dict:
    output = output.absolute()
    if output.exists() or output.is_symlink() or output.resolve() == source.resolve():
        raise ValueError('Output must be a new file, different from the source')
    with tempfile.TemporaryDirectory(prefix='.jot-reconcile-', dir=output.parent) as temp:
        staged = Path(temp) / 'accounts.sqlite'
        migration = prepare_copy(source, staged, owner_id=owner_id, owner_name=owner_name)
        reconciliation = reconcile_account(staged, owner_id)
        with staged.open('rb') as handle:
            os.fsync(handle.fileno())
        os.link(staged, output)  # Atomic publication; refuses competing output.
        return {'migration': {k: v for k, v in migration.items() if k != 'output'},
                'reconciliation': reconciliation, 'output': str(output)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--owner-id', required=True)
    parser.add_argument('--owner-name', required=True)
    args = parser.parse_args()
    print(json.dumps(prepare_reconciled_copy(args.source, args.output,
        owner_id=args.owner_id, owner_name=args.owner_name), indent=2))
