"""Explicit account-service entrypoint; the production Docker command is unchanged.

Run only after the offline migration and verified legacy-owner binding:
uvicorn firebase_service:create_app --factory --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from account_operations import CaptureLimits, configure_private_logging
from account_recovery import DeletionJournal
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
    configure_private_logging()
    journal = DeletionJournal(Path(os.environ['JOT_DELETION_JOURNAL_PATH']))
    if journal.path == db_path.resolve():
        raise ValueError('Deletion journal must be separate from the account database')
    pause_file = Path(os.environ['JOT_PROCESSING_PAUSE_FILE'])
    if not pause_file.is_absolute() or pause_file.resolve() in {db_path.resolve(), journal.path}:
        raise ValueError('Use a separate absolute processing pause-file path')
    limits = CaptureLimits(
        user_pending=int(os.environ.get('JOT_USER_PENDING_LIMIT', '20')),
        total_pending=int(os.environ.get('JOT_TOTAL_PENDING_LIMIT', '200')),
        user_daily=int(os.environ.get('JOT_USER_DAILY_SAVE_LIMIT', '100')),
        total_daily=int(os.environ.get('JOT_TOTAL_DAILY_SAVE_LIMIT', '1000')),
        manual_retries=int(os.environ.get('JOT_MANUAL_RETRY_LIMIT', '5')),
        pause_file=pause_file,
    )
    accounts = IdentityStore(db_path)
    with accounts.transaction() as con:
        journal.apply(con, project)
    validate_release_database(accounts, project)
    credential_file = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    tokens = create_token_verifier(project, credential_path=Path(credential_file) if credential_file else None)
    worker = PostProcessingWorker(PostProcessingStore(db_path, version, pause_file=pause_file),
                                  Path(tempfile.gettempdir()), process_public_post)
    return create_account_app(db_path=db_path, verify_session=FirebaseSessionVerifier(tokens, accounts),
                              worker=worker, deletion=AccountDeletion(accounts, tokens, journal),
                              limits=limits, monitor=True)
