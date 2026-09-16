"""Verified Firebase identities map to private internal accounts, never emails."""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import firebase_admin
from firebase_admin import auth, credentials, exceptions
from google.auth.exceptions import GoogleAuthError

from account_app import InvalidSession, RecentSignInRequired, SessionServiceUnavailable, VerifiedAccount
from account_store import AccountConflict, AccountUnavailable
from multi_user_migration import APPLICATION_ID, SCHEMA_VERSION


@dataclass(frozen=True)
class FirebaseIdentity:
    project_id: str
    uid: str
    display_name: str
    auth_time: int


class FirebaseTokenVerifier:
    def __init__(self, project_id: str, client: auth.Client):
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError('An explicit Firebase project is required')
        self.project_id = project_id
        self.client = client
        self._require_production_verification()

    @staticmethod
    def _require_production_verification():
        # The Admin SDK intentionally accepts unsigned emulator tokens when
        # this variable is present. This service must never allow that mode.
        if os.environ.get('FIREBASE_AUTH_EMULATOR_HOST'):
            raise RuntimeError('The account service cannot use the authentication emulator')

    def verify(self, token: str, *, recent: bool = False) -> FirebaseIdentity:
        self._require_production_verification()
        if not isinstance(token, str) or not 1 <= len(token) <= 16384:
            raise InvalidSession()
        try:
            claims = self.client.verify_id_token(token, check_revoked=True)
        except (ValueError, auth.InvalidIdTokenError, auth.RevokedIdTokenError,
                auth.UserDisabledError, auth.UserNotFoundError):
            raise InvalidSession() from None
        except (exceptions.FirebaseError, GoogleAuthError):
            raise SessionServiceUnavailable() from None
        firebase = claims.get('firebase')
        uid = claims.get('uid')
        authenticated_at = claims.get('auth_time')
        if (claims.get('aud') != self.project_id
                or claims.get('iss') != 'https://securetoken.google.com/' + self.project_id
                or not isinstance(uid, str) or not 1 <= len(uid) <= 128
                or uid != claims.get('sub')
                or not isinstance(firebase, dict) or firebase.get('tenant') is not None
                or firebase.get('sign_in_provider') not in {'apple.com', 'google.com'}
                or type(authenticated_at) is not int or authenticated_at < 0
                or authenticated_at > time.time()):
            raise InvalidSession()
        if recent and time.time() - authenticated_at > 300:
            raise RecentSignInRequired()
        name = claims.get('name')
        name = name.strip()[:120] if isinstance(name, str) else ''
        return FirebaseIdentity(self.project_id, uid, name or 'Jot user', authenticated_at)


def create_token_verifier(project_id: str, *, credential_path: Path | None = None) -> FirebaseTokenVerifier:
    FirebaseTokenVerifier._require_production_verification()
    if not project_id or not project_id.strip():
        raise ValueError('An explicit Firebase project is required')
    credential = credentials.Certificate(str(credential_path)) if credential_path else credentials.ApplicationDefault()
    app = firebase_admin.initialize_app(credential, {'projectId': project_id, 'httpTimeout': 10},
                                        name='jot-auth-' + uuid.uuid4().hex)
    return FirebaseTokenVerifier(project_id, auth.Client(app))


class IdentityStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @contextmanager
    def transaction(self):
        con = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=rw', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys = ON')
            if (con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or con.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise RuntimeError('Identity storage requires an explicitly migrated database')
            con.execute('BEGIN IMMEDIATE')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def resolve(self, identity: FirebaseIdentity, *,
                confirm_new_identity: Callable[[], FirebaseIdentity] | None = None) -> VerifiedAccount:
        with self.transaction() as con:
            row = con.execute('SELECT id, status FROM users WHERE firebase_project_id = ? AND firebase_uid = ?',
                              (identity.project_id, identity.uid)).fetchone()
            if row:
                if row['status'] != 'active':
                    raise AccountUnavailable()
                return VerifiedAccount(row['id'])
            if confirm_new_identity is not None:
                # A request may have verified its token just before deletion.
                # Recheck new identities while holding the SQLite write lock,
                # so a late request cannot recreate a just-deleted account.
                confirmed = confirm_new_identity()
                if (confirmed.project_id, confirmed.uid) != (identity.project_id, identity.uid):
                    raise InvalidSession()
            # Never claim an existing account, even if this is the first login,
            # its UID resembles an internal ID, or its email/name matches.
            user_id = uuid.uuid4().hex
            con.execute('INSERT INTO users (id, display_name, firebase_project_id, firebase_uid) VALUES (?, ?, ?, ?)',
                        (user_id, identity.display_name, identity.project_id, identity.uid))
            return VerifiedAccount(user_id)

    def bind_existing(self, owner_id: str, identity: FirebaseIdentity) -> None:
        """Offline operator action, never exposed as a user-facing claim endpoint."""
        with self.transaction() as con:
            owner = con.execute('SELECT * FROM users WHERE id = ?', (owner_id,)).fetchone()
            if owner is None or owner['status'] != 'active':
                raise AccountUnavailable()
            current = owner['firebase_project_id'], owner['firebase_uid']
            desired = identity.project_id, identity.uid
            if current == desired:
                return
            if current != (None, None):
                raise AccountConflict('This library is already bound to a different identity')
            if con.execute('SELECT 1 FROM users WHERE firebase_project_id = ? AND firebase_uid = ?', desired).fetchone():
                raise AccountConflict('This identity already belongs to another account; explicit review is required')
            con.execute('UPDATE users SET firebase_project_id = ?, firebase_uid = ? WHERE id = ?',
                        (*desired, owner_id))


class FirebaseSessionVerifier:
    def __init__(self, tokens: FirebaseTokenVerifier, accounts: IdentityStore):
        self.tokens = tokens
        self.accounts = accounts

    def __call__(self, token: str) -> VerifiedAccount:
        return self.accounts.resolve(self.tokens.verify(token),
                                     confirm_new_identity=lambda: self.tokens.verify(token))
