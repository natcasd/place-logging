"""Explicit account-service entrypoint; the production Docker command is unchanged.

Run only after the offline migration and verified legacy-owner binding:
uvicorn firebase_service:create_app --factory --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from account_app import create_account_app
from account_deletion import AccountDeletion
from firebase_identity import FirebaseSessionVerifier, IdentityStore, create_token_verifier
from post_processing_store import PostProcessingStore
from post_processing_worker import PostProcessingWorker
from public_processing_adapter import process_public_post


def validate_release_database(accounts: IdentityStore, project_id: str) -> None:
    with accounts.transaction() as con:
        if con.execute("SELECT 1 FROM captures WHERE materialization_state = 'legacy_unverified' LIMIT 1").fetchone():
            raise RuntimeError('Historical captures require offline reconciliation before release')
        if con.execute('SELECT 1 FROM users WHERE firebase_uid IS NULL LIMIT 1').fetchone():
            raise RuntimeError('Existing libraries require explicit verified identity binding before release')
        if con.execute('SELECT 1 FROM users WHERE firebase_project_id IS NOT NULL AND firebase_project_id != ? LIMIT 1',
                       (project_id,)).fetchone():
            raise RuntimeError('Database identities do not belong to the configured Firebase project')


def create_app():
    db_path = Path(os.environ['JOT_ACCOUNT_DB_PATH'])
    project = os.environ['FIREBASE_PROJECT_ID']
    version = os.environ['JOT_PROCESSING_VERSION']
    if not project.strip() or not version.strip():
        raise ValueError('Explicit Firebase project and processing version are required')
    accounts = IdentityStore(db_path)
    validate_release_database(accounts, project)
    credential_file = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    tokens = create_token_verifier(project, credential_path=Path(credential_file) if credential_file else None)
    worker = PostProcessingWorker(PostProcessingStore(db_path, version),
                                  Path(tempfile.gettempdir()), process_public_post)
    return create_account_app(db_path=db_path, verify_session=FirebaseSessionVerifier(tokens, accounts),
                              worker=worker, deletion=AccountDeletion(accounts, tokens))
