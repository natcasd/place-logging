from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import firebase_admin
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from firebase_admin import auth, credentials
from google.auth import crypt, jwt

import store
from account_app import InvalidSession, SessionServiceUnavailable, create_account_app
from account_store import AccountConflict, AccountStore, AccountUnavailable
from bind_legacy_account import prepare_bound_copy
from firebase_identity import FirebaseIdentity, FirebaseSessionVerifier, FirebaseTokenVerifier, IdentityStore
from firebase_service import validate_release_database
from legacy_reconciliation import prepare_reconciled_copy


class FirebaseIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.private = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()).decode()
        cls.public = cls.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                      serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source, self.target = self.root / 'original.sqlite', self.root / 'accounts.sqlite'
        store.init_db(self.source)
        store.save_ingest(self.source, {'source_url': 'https://youtu.be/oldpost',
            'entries_extracted': [{'extracted_name': 'Private Cafe', 'type_name': 'Restaurant'}],
            'resolved_entries': [{'extracted': {'extracted_name': 'Private Cafe', 'type_name': 'Restaurant'},
                                 'status': 'resolved', 'place': {'id': 'private-place'}}]})
        prepare_reconciled_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        self.identities = IdentityStore(self.target)
        self.project = 'jot-test-project'
        self.app = firebase_admin.initialize_app(credentials.Certificate({
            'type': 'service_account', 'project_id': self.project,
            'private_key_id': 'local-test', 'private_key': self.private,
            'client_email': 'test@jot-test-project.iam.gserviceaccount.com',
            'token_uri': 'https://oauth2.googleapis.com/token',
        }), {'projectId': self.project}, name=uuid.uuid4().hex)
        self.addCleanup(firebase_admin.delete_app, self.app)
        self.client = auth.Client(self.app)
        self.client._token_verifier.request = Mock(return_value=SimpleNamespace(
            status=200, data=json.dumps({'local-test': self.public}).encode()))
        self.user = self.enterContext(patch.object(self.client, 'get_user', return_value=auth.UserRecord({
            'localId': 'verified-nathan', 'validSince': '0', 'disabled': False})))
        self.tokens = FirebaseTokenVerifier(self.project, self.client)
        self.sessions = FirebaseSessionVerifier(self.tokens, self.identities)

    def token(self, **changes):
        now = int(time.time())
        claims = {'sub': 'verified-nathan', 'aud': self.project,
                  'iss': 'https://securetoken.google.com/' + self.project,
                  'iat': now, 'exp': now + 3600, 'auth_time': now,
                  'name': 'Nathan', 'email': 'nathan@example.com',
                  'firebase': {'sign_in_provider': 'google.com', 'identities': {'google.com': ['provider-id']}}}
        claims.update(changes)
        return jwt.encode(crypt.RSASigner.from_string(self.private, key_id='local-test'), claims).decode()

    def test_real_sdk_signature_and_revocation_check_precede_private_mapping(self):
        identity = self.tokens.verify(self.token())
        self.identities.bind_existing('nathan', identity)
        self.assertEqual(self.sessions(self.token()).user_id, 'nathan')
        self.user.assert_called_with('verified-nathan')
        api = TestClient(create_account_app(db_path=self.target, verify_session=self.sessions))
        headers = {'Authorization': 'Bearer ' + self.token()}
        self.assertEqual(api.get('/api/v1/account', headers=headers).json(), {'id': 'nathan', 'display_name': 'Nathan'})
        self.assertEqual(api.get('/api/v1/entries', headers=headers).json()['entries'][0]['name'], 'Private Cafe')

    def test_first_login_and_matching_name_email_or_internal_uid_never_claim_legacy(self):
        account = self.sessions(self.token(sub='nathan'))
        self.assertNotEqual(account.user_id, 'nathan')
        self.assertEqual(AccountStore(self.target, account.user_id).entries(), [])
        self.assertEqual(len(AccountStore(self.target, 'nathan').entries()), 1)
        with sqlite3.connect(self.target) as con:
            self.assertEqual(con.execute("SELECT firebase_uid FROM users WHERE id='nathan'").fetchone(), (None,))

    def test_both_linked_providers_resolve_same_firebase_uid_to_one_library(self):
        self.identities.bind_existing('nathan', self.tokens.verify(self.token()))
        apple = self.token(firebase={'sign_in_provider': 'apple.com', 'identities': {'apple.com': ['apple-id']}})
        self.assertEqual(self.sessions(apple), self.sessions(self.token()))

    def test_separate_firebase_uid_with_same_email_gets_separate_account(self):
        self.identities.bind_existing('nathan', self.tokens.verify(self.token()))
        other = self.sessions(self.token(sub='another-firebase-uid'))
        self.assertNotEqual(other.user_id, 'nathan')
        self.assertEqual(AccountStore(self.target, other.user_id).entries(), [])

    def test_concurrent_registration_creates_one_empty_account(self):
        identity = self.tokens.verify(self.token())
        with ThreadPoolExecutor(max_workers=6) as executor:
            ids = list(executor.map(lambda _: self.identities.resolve(identity).user_id, range(12)))
        self.assertEqual(len(set(ids)), 1)
        self.assertNotEqual(ids[0], 'nathan')

    def test_invalid_project_issuer_expiry_and_signature_rejected_by_sdk(self):
        invalid = [self.token(aud='another-project'), self.token(iss='https://attacker.example'),
                   self.token(iat=int(time.time())-4000, exp=int(time.time())-100)]
        token = self.token()
        pieces = token.split('.')
        pieces[2] = ('A' if pieces[2][0] != 'A' else 'B') + pieces[2][1:]
        invalid += ['.'.join(pieces), 'unsigned-user-id', '', self.token(iat=int(time.time())+3600)]
        for token in invalid:
            with self.subTest(token_kind=token[:10]), self.assertRaises(InvalidSession):
                self.sessions(token)
        with sqlite3.connect(self.target) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)

    def test_revoked_disabled_and_deleted_firebase_user_rejected(self):
        self.user.return_value = auth.UserRecord({'localId': 'verified-nathan', 'validSince': str(int(time.time())+10)})
        with self.assertRaises(InvalidSession):
            self.sessions(self.token())
        self.user.return_value = auth.UserRecord({'localId': 'verified-nathan', 'disabled': True, 'validSince': '0'})
        with self.assertRaises(InvalidSession):
            self.sessions(self.token())
        self.user.side_effect = auth.UserNotFoundError('Gone')
        with self.assertRaises(InvalidSession):
            self.sessions(self.token())

    def test_disabled_internal_account_cannot_be_recreated(self):
        self.identities.bind_existing('nathan', self.tokens.verify(self.token()))
        with sqlite3.connect(self.target) as con:
            con.execute("UPDATE users SET status='disabled' WHERE id='nathan'")
        with self.assertRaises(AccountUnavailable):
            self.sessions(self.token())
        with sqlite3.connect(self.target) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)

    def test_unsupported_providers_tenants_and_bad_auth_time_rejected(self):
        invalid = [{'sign_in_provider': p} for p in ('anonymous', 'custom', 'password', 'phone')]
        invalid.append({'sign_in_provider': 'google.com', 'tenant': 'other'})
        for firebase in invalid:
            with self.subTest(firebase=firebase), self.assertRaises(InvalidSession):
                self.sessions(self.token(firebase=firebase))
        with self.assertRaises(InvalidSession):
            self.tokens.verify(self.token(auth_time=int(time.time())+50))

    def test_recent_login_only_required_for_sensitive_binding(self):
        old = self.token(auth_time=int(time.time())-86400*60)
        self.assertEqual(self.tokens.verify(old).uid, 'verified-nathan')
        with self.assertRaises(InvalidSession):
            self.tokens.verify(old, recent=True)

    def test_emulator_environment_cannot_disable_signature_checks(self):
        with patch.dict(os.environ, {'FIREBASE_AUTH_EMULATOR_HOST': 'localhost:9099'}):
            with self.assertRaises(RuntimeError):
                FirebaseTokenVerifier(self.project, self.client)
            with self.assertRaises(RuntimeError):
                self.tokens.verify(self.token())

    def test_auth_service_failure_is_503_without_exposing_token_or_creating_account(self):
        self.user.side_effect = auth.InsufficientPermissionError('sensitive provider error', None, None)
        api = TestClient(create_account_app(db_path=self.target, verify_session=self.sessions))
        response = api.get('/api/v1/account', headers={'Authorization': 'Bearer ' + self.token()})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('sensitive', response.text)
        with sqlite3.connect(self.target) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM users').fetchone()[0], 1)

    def test_bound_copy_preserves_original_and_api_data(self):
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        bound = self.root / 'bound.sqlite'
        identity = self.tokens.verify(self.token(), recent=True)
        report = prepare_bound_copy(self.source, bound, owner_id='nathan', owner_name='Nathan', identity=identity)
        self.assertEqual(report['firebase_uid'], identity.uid)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)
        self.assertEqual(AccountStore(bound, 'nathan').entries(), store.list_entries(self.source))
        self.assertEqual(IdentityStore(bound).resolve(identity).user_id, 'nathan')
        with self.assertRaises(ValueError):
            prepare_bound_copy(self.source, bound, owner_id='nathan', owner_name='Nathan', identity=identity)

    def test_existing_binding_cannot_be_replaced_or_silently_merged(self):
        identity = self.tokens.verify(self.token())
        self.identities.bind_existing('nathan', identity)
        self.identities.bind_existing('nathan', identity)
        other = FirebaseIdentity(self.project, 'other', 'Nathan', int(time.time()))
        with self.assertRaises(AccountConflict):
            self.identities.bind_existing('nathan', other)
        empty = self.identities.resolve(other)
        with self.assertRaises(AccountConflict):
            self.identities.bind_existing(empty.user_id, identity)

    def test_preexisting_registration_conflict_does_not_publish_or_change_source(self):
        identity = self.tokens.verify(self.token())
        self.identities.resolve(identity)
        before = hashlib.sha256(self.target.read_bytes()).hexdigest()
        output = self.root / 'conflict.sqlite'
        with self.assertRaises(AccountConflict):
            prepare_bound_copy(self.target, output, owner_id='nathan', owner_name='Nathan', identity=identity)
        self.assertFalse(output.exists())
        self.assertEqual(hashlib.sha256(self.target.read_bytes()).hexdigest(), before)

    def test_schema_rejects_half_bindings_and_duplicate_identities(self):
        with sqlite3.connect(self.target) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE users SET firebase_uid='uid' WHERE id='nathan'")
        identity = self.tokens.verify(self.token())
        self.identities.bind_existing('nathan', identity)
        with sqlite3.connect(self.target) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute('INSERT INTO users(id, display_name, firebase_project_id, firebase_uid) VALUES (?, ?, ?, ?)',
                            ('duplicate', 'Duplicate', self.project, identity.uid))

    def test_service_refuses_unbound_library_and_wrong_project(self):
        with self.assertRaisesRegex(RuntimeError, 'binding'):
            validate_release_database(self.identities, self.project)
        self.identities.bind_existing('nathan', self.tokens.verify(self.token()))
        validate_release_database(self.identities, self.project)
        with self.assertRaisesRegex(RuntimeError, 'project'):
            validate_release_database(self.identities, 'other-project')
