"""Durable deletion of one verified account; no client-selected owner IDs.

The private account becomes unavailable before contacting Firebase. Failed
provider calls retry from the existing users row after a restart. Delete the
Firebase identity before removing that row so an old identity cannot recreate
an active library while deletion is pending.
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path
from threading import Event

from firebase_admin import auth, exceptions
from google.auth.exceptions import GoogleAuthError

from account_store import AccountUnavailable
from firebase_identity import FirebaseTokenVerifier, IdentityStore

log = logging.getLogger(__name__)


class AccountDeletion:
    def __init__(self, accounts: IdentityStore, tokens: FirebaseTokenVerifier):
        self.accounts = accounts
        self.tokens = tokens
        self.db_path: Path = accounts.db_path

    def request(self, token: str) -> None:
        identity = self.tokens.verify(token, recent=True)
        with self.accounts.transaction() as con:
            row = con.execute(
                'SELECT id, status FROM users WHERE firebase_project_id = ? AND firebase_uid = ?',
                (identity.project_id, identity.uid),
            ).fetchone()
            if row is None:
                # A user can delete their identity before their first library
                # request. Queue an empty, separate account; never claim legacy.
                con.execute('''INSERT INTO users
                    (id, display_name, status, firebase_project_id, firebase_uid)
                    VALUES (?, ?, 'deleting', ?, ?)''',
                    (uuid.uuid4().hex, identity.display_name, identity.project_id, identity.uid))
            elif row['status'] == 'active':
                con.execute("UPDATE users SET status = 'deleting' WHERE id = ?", (row['id'],))
            elif row['status'] != 'deleting':
                raise AccountUnavailable()

    def drain_once(self) -> dict[str, int]:
        with self.accounts.transaction() as con:
            pending = con.execute('''SELECT id, firebase_uid FROM users
                WHERE status = 'deleting' AND firebase_project_id = ?
                ORDER BY created_at, id LIMIT 100''', (self.tokens.project_id,)).fetchall()
        completed = failed = 0
        for row in pending:
            try:
                self.tokens.client.delete_user(row['firebase_uid'])
            except auth.UserNotFoundError:
                pass  # Provider succeeded previously, or user deleted it separately.
            except (exceptions.FirebaseError, GoogleAuthError):
                failed += 1
                continue
            # Conditional delete tolerates concurrent workers. Foreign keys
            # cascade private captures, mentions, entries, enrichments and logs.
            # Shared public extraction/Places rows and other users remain intact.
            with self.accounts.transaction() as con:
                con.execute('PRAGMA secure_delete = ON')
                completed += con.execute('''DELETE FROM users
                    WHERE id = ? AND status = 'deleting'
                      AND firebase_project_id = ? AND firebase_uid = ?''',
                    (row['id'], self.tokens.project_id, row['firebase_uid'])).rowcount
        return {'completed': completed, 'pending_retry': failed}

    def run(self, stop: Event) -> None:
        while not stop.is_set():
            try:
                counts = self.drain_once()
                if counts['completed'] or counts['pending_retry']:
                    log.info('Account deletion completed=%s pending_retry=%s',
                             counts['completed'], counts['pending_retry'])
            except Exception:
                # No tokens, names, UIDs or provider error payloads in logs.
                log.error('Account deletion worker failed; retrying on next pass')
            stop.wait(30)
