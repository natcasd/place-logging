"""Restore optional movie links using the existing worker and private rows."""
from __future__ import annotations

from account_store import AccountStore, AccountUnavailable
from entry_types import entry_types_for_enricher
from movie_enrichment import MovieProvider
from post_processing_store import PostProcessingStore


class AccountMovieEnricher:
    def __init__(self, store: PostProcessingStore, provider: MovieProvider):
        self.store = store
        self.provider = provider

    def run_once(self) -> bool:
        # One lookup per worker pass. Existing enrichment rows (including errors)
        # are not fetched again. No new queue, schema, service or credentials.
        kinds = [kind.casefold() for kind in entry_types_for_enricher('movie')]
        if not kinds:
            return False
        with self.store._transaction() as con:
            row = con.execute(f'''SELECT r.id, r.user_id FROM recommendations r
                JOIN users u ON u.id = r.user_id AND u.status = 'active'
                LEFT JOIN movie_enrichments e ON e.entry_id = r.id AND e.user_id = r.user_id
                WHERE e.entry_id IS NULL AND lower(trim(r.entry_type)) IN ({','.join('?' for _ in kinds)})
                AND EXISTS (SELECT 1 FROM recommendation_mentions m
                    JOIN captures c ON c.user_id = m.user_id AND c.id = m.item_id
                    WHERE m.user_id = r.user_id AND m.entry_id = r.id AND m.removed_at IS NULL
                    AND c.capture_channel != 'legacy')
                ORDER BY r.id LIMIT 1''', kinds).fetchone()
        if row is None:
            return False
        account = AccountStore(self.store.db_path, row['user_id'])

        def evidence(con):
            entry = con.execute('SELECT name, entry_type FROM recommendations WHERE user_id = ? AND id = ?',
                                (account.user_id, row['id'])).fetchone()
            mentions = con.execute('''SELECT id, description FROM recommendation_mentions
                WHERE user_id = ? AND entry_id = ? AND removed_at IS NULL ORDER BY id''',
                (account.user_id, row['id'])).fetchall()
            return (tuple(entry), [tuple(m) for m in mentions]) if entry and mentions else None

        try:
            with account._transaction() as con:
                original = evidence(con)
            if original is None or original[0][1].strip().casefold() not in kinds:
                return True
            try:
                result = self.provider.lookup(original[0][0], ' '.join(m[1] for m in original[1]))
            except Exception:
                # Preserve the save and search fallback; never log private input.
                result = {'provider': self.provider.name, 'match_status': 'error'}
            with account._transaction(write=True) as con:
                if evidence(con) != original:
                    return True  # A deletion or edit during lookup wins.
                con.execute('''INSERT INTO movie_enrichments (entry_id, user_id, provider,
                    provider_id, resolved_title, release_year, letterboxd_url, match_status, match_confidence)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_id) DO NOTHING''',
                    (row['id'], account.user_id, result['provider'], result.get('provider_id'),
                     result.get('resolved_title'), result.get('release_year'), result.get('letterboxd_url'),
                     result['match_status'], result.get('match_confidence')))
        except AccountUnavailable:
            pass  # Account deletion wins; no library is recreated.
        return True
