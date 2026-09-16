"""Opt-in account API factory. Production still starts app:app.

A trusted session verifier is mandatory. It maps a validated session to an
internal user ID; Apple/Google identities must be linked by the identity
provider/session layer, never by an unverified email or client-supplied owner.
No development tokens, identity headers, or legacy shared-token fallback exist
in this module. Saves enter a durable queue drained by an explicit worker.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from account_store import AccountConflict, AccountStore, AccountUnavailable, RecordNotFound
from account_ingest import SourceResolutionUnavailable, resolve_public_url
from capture_store import CaptureStore
from post_processing_worker import PostProcessingWorker
from app import (
    ActivityResponse, ConfirmActivityLocationRequest, ConfirmActivityLocationResponse,
    DeleteActivityResponse, DeleteEntriesRequest, DeleteEntriesResponse, DeleteEntryResponse,
    EntriesResponse, IngestRequest, ShortcutIngestRequest, SourcesResponse,
    SavedEntry, SavedEntrySource, SavedSource, IngestActivity, SavedEntryOutcome,
)


class InvalidSession(Exception):
    """A verifier raises this for invalid, expired, or revoked credentials."""


class RecentSignInRequired(InvalidSession):
    """A valid session needs fresh provider authentication for a sensitive action."""


class SessionServiceUnavailable(Exception):
    """Temporary verifier failure; clients should retain their sign-in state."""


@dataclass(frozen=True)
class VerifiedAccount:
    user_id: str


class SessionVerifier(Protocol):
    def __call__(self, token: str) -> VerifiedAccount:
        """Verify the session and return its server-mapped internal account ID."""
        ...


class AccountDeletionHandler(Protocol):
    db_path: Path

    def request(self, token: str) -> None: ...

    def run(self, stop: Event) -> None: ...


# Null URLs are valid for direct/Siri input. Keep the released single-user
# contract untouched until the new iOS client is ready for these responses.
class AccountSourceMention(SavedEntrySource):
    source_url: str | None


class AccountEntry(SavedEntry):
    source_url: str | None
    sources: list[AccountSourceMention]


class AccountEntries(EntriesResponse):
    entries: list[AccountEntry]


class AccountSource(SavedSource):
    source_url: str | None


class AccountSources(SourcesResponse):
    sources: list[AccountSource]


class AccountActivityItem(IngestActivity):
    source_url: str | None


class AccountActivity(ActivityResponse):
    activity: list[AccountActivityItem]


class ConfirmMentionLocationRequest(ConfirmActivityLocationRequest):
    mention_id: int | None = Field(default=None, ge=1)


class AccountIngestRequest(IngestRequest):
    request_key: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class AccountShortcutRequest(ShortcutIngestRequest):
    request_key: str = Field(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')


class AcceptedIngest(BaseModel):
    ingest_id: int
    item_id: int
    status: str
    accepted_sequence: int
    saved_entries: list[SavedEntryOutcome] = Field(default_factory=list)


class SaveResult(AcceptedIngest):
    item_id: int | None
    failure_kind: str | None = None
    error_message: str | None = None
    next_retry_at: str | None = None


def create_account_app(*, db_path: Path, verify_session: SessionVerifier,
                       worker: PostProcessingWorker | None = None,
                       deletion: AccountDeletionHandler | None = None) -> FastAPI:
    if not callable(verify_session):
        raise ValueError('A session verifier is required')
    if worker is not None and worker.store.db_path.resolve() != db_path.resolve():
        raise ValueError('Worker and account API must use the same database')
    if deletion is not None and deletion.db_path.resolve() != db_path.resolve():
        raise ValueError('Deletion worker and account API must use the same database')

    @asynccontextmanager
    async def lifespan(application):
        stop = Event()
        threads = []
        if worker is not None:
            worker.store.active_tokens()  # Validate the explicit schema before starting.
            thread = Thread(target=worker.run, args=(stop,), name='jot-public-worker', daemon=True)
            thread.start()
            threads.append(thread)
        if deletion is not None:
            thread = Thread(target=deletion.run, args=(stop,), name='jot-account-deletion', daemon=True)
            thread.start()
            threads.append(thread)
        try:
            yield
        finally:
            stop.set()
            for thread in threads:
                # Drain in-flight work with its lease renewal and cleanup intact.
                # A forced process termination recovers through durable leases.
                await asyncio.to_thread(thread.join)

    application = FastAPI(title='Jot account API', version='1.0.0', lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)

    def scoped_store(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> AccountStore:
        if credentials is None:
            raise InvalidSession()
        identity = verify_session(credentials.credentials)
        if (not isinstance(identity, VerifiedAccount)
                or not isinstance(identity.user_id, str) or not identity.user_id.strip()):
            raise InvalidSession()
        account = AccountStore(db_path, identity.user_id)
        account.require_active()
        return account

    @application.exception_handler(InvalidSession)
    @application.exception_handler(AccountUnavailable)
    async def unauthorized(_request, _error):
        return JSONResponse(status_code=401, content={'detail': 'Authentication required'},
                            headers={'WWW-Authenticate': 'Bearer'})

    @application.exception_handler(RecordNotFound)
    async def not_found(_request, _error):
        return JSONResponse(status_code=404, content={'detail': 'Not found'})

    @application.exception_handler(RecentSignInRequired)
    async def recent_sign_in_required(_request, _error):
        return JSONResponse(status_code=403, content={
            'detail': 'Sign in again to confirm account deletion.',
            'code': 'recent_sign_in_required',
        })

    @application.exception_handler(SessionServiceUnavailable)
    async def session_unavailable(_request, _error):
        return JSONResponse(status_code=503, content={'detail': 'Sign-in verification is temporarily unavailable. Try again.'},
                            headers={'Retry-After': '5'})

    @application.exception_handler(AccountConflict)
    async def conflict(_request, error):
        return JSONResponse(status_code=409, content={'detail': str(error)})

    @application.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    @application.exception_handler(SourceResolutionUnavailable)
    async def resolution_unavailable(_request, _error):
        return JSONResponse(status_code=503, content={'detail': 'Could not resolve this share link. Try again.'})

    router = APIRouter(prefix='/api/v1', dependencies=[Depends(scoped_store)])

    # Deliberately outside the active-account router: a lost response can be
    # retried after status becomes deleting, without granting library access.
    @application.delete('/api/v1/account', status_code=202)
    def delete_account(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        if credentials is None:
            raise InvalidSession()
        if deletion is None:
            raise HTTPException(status_code=503, detail='Account deletion is temporarily unavailable')
        deletion.request(credentials.credentials)
        return {'status': 'deletion_requested'}

    @router.get('/account')
    def current_account(account: AccountStore = Depends(scoped_store)):
        with account._transaction() as con:
            row = con.execute('SELECT id, display_name FROM users WHERE id = ?', (account.user_id,)).fetchone()
            return dict(row)

    @router.get('/entries', response_model=AccountEntries)
    def entries(limit: int = Query(default=200, ge=1, le=1000), account: AccountStore = Depends(scoped_store)):
        return {'entries': account.entries(limit)}

    @router.get('/sources', response_model=AccountSources)
    def sources(limit: int = Query(default=200, ge=1, le=500), account: AccountStore = Depends(scoped_store)):
        return {'sources': account.sources(limit)}

    @router.get('/activity', response_model=AccountActivity)
    def activity(limit: int = Query(default=200, ge=1, le=500), account: AccountStore = Depends(scoped_store)):
        return {'activity': account.activity(limit)}

    @router.delete('/entries/{entry_id}', response_model=DeleteEntryResponse)
    def delete_entry(entry_id: int, account: AccountStore = Depends(scoped_store)):
        return {'entry_id': entry_id, **account.delete_entries([entry_id])}

    @router.delete('/entries', response_model=DeleteEntriesResponse)
    def delete_entries(payload: DeleteEntriesRequest, account: AccountStore = Depends(scoped_store)):
        ids = list(dict.fromkeys(payload.entry_ids))
        return {'entry_ids': ids, **account.delete_entries(ids)}

    @router.delete('/activity/{ingest_id}', response_model=DeleteActivityResponse)
    def delete_activity(ingest_id: int, account: AccountStore = Depends(scoped_store)):
        account.delete_failed_activity(ingest_id)
        return {'ingest_id': ingest_id}

    @router.delete('/mentions/{mention_id}')
    def delete_mention(mention_id: int, account: AccountStore = Depends(scoped_store)):
        return account.delete_mention(mention_id)

    @router.post('/activity/{ingest_id}/entries/{entry_id}/location', response_model=ConfirmActivityLocationResponse)
    def confirm_location(ingest_id: int, entry_id: int, payload: ConfirmMentionLocationRequest,
                         account: AccountStore = Depends(scoped_store)):
        return {'entry': account.confirm_activity_location(ingest_id, entry_id, payload.candidate_id,
                                                          mention_id=payload.mention_id)}

    def accept(url: str, key: str, account: AccountStore, channel: str):
        try:
            resolved = resolve_public_url(url)
            return CaptureStore(db_path, account.user_id).accept_public(resolved, key, channel=channel)
        except ValueError:
            raise HTTPException(422, 'A supported public post URL is required') from None

    @router.post('/ingests', status_code=202, response_model=AcceptedIngest)
    def ingest(payload: AccountIngestRequest, account: AccountStore = Depends(scoped_store)):
        return accept(payload.source_url, payload.request_key, account, 'share_extension')

    @router.get('/ingests/{ingest_id}', response_model=SaveResult)
    async def ingest_result(ingest_id: int, response: Response, wait_seconds: int = Query(default=0, ge=0, le=25),
                            account: AccountStore = Depends(scoped_store)):
        # Short read transactions let the durable worker keep making progress.
        # Each read rechecks account state and ownership, including during deletion.
        response.headers['Cache-Control'] = 'no-store'
        deadline = asyncio.get_running_loop().time() + wait_seconds
        captures = CaptureStore(db_path, account.user_id)
        while True:
            result = await asyncio.to_thread(captures.result, ingest_id)
            remaining = deadline - asyncio.get_running_loop().time()
            if result['status'] in {'completed', 'partial', 'failed'} or remaining <= 0:
                return result
            await asyncio.sleep(min(0.5, remaining))

    @router.post('/shortcut/ingests', status_code=202, response_model=AcceptedIngest)
    def shortcut_ingest(payload: AccountShortcutRequest, account: AccountStore = Depends(scoped_store)):
        try:
            url = base64.b64decode(payload.source_url_base64, validate=True).decode('utf-8')
        except (ValueError, binascii.Error, UnicodeError):
            raise HTTPException(422, 'source_url_base64 must encode a UTF-8 URL') from None
        return accept(url, payload.request_key, account, 'shortcut')

    @router.post('/activity/{ingest_id}/retry', status_code=202, response_model=AcceptedIngest)
    def retry(ingest_id: int, account: AccountStore = Depends(scoped_store)):
        return CaptureStore(db_path, account.user_id).retry_public(ingest_id)

    application.include_router(router)
    return application
