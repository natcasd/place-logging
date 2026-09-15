"""Opt-in account API factory. Production still starts app:app.

A trusted session verifier is mandatory. It maps a validated session to an
internal user ID; Apple/Google/email identities must be linked by the identity
provider/session layer, never by an unverified email or client-supplied owner.
No development tokens, identity headers, or legacy shared-token fallback exist
in this module. Ingest processing is deliberately unavailable until PRs 3/4.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from pydantic import Field
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from account_store import AccountConflict, AccountStore, AccountUnavailable, RecordNotFound
from app import (
    ActivityResponse, ConfirmActivityLocationRequest, ConfirmActivityLocationResponse,
    DeleteActivityResponse, DeleteEntriesRequest, DeleteEntriesResponse, DeleteEntryResponse,
    EntriesResponse, IngestRequest, ShortcutIngestRequest, SourcesResponse,
    SavedEntry, SavedEntrySource, SavedSource, IngestActivity,
)


class InvalidSession(Exception):
    """A verifier raises this for invalid, expired, or revoked credentials."""


@dataclass(frozen=True)
class VerifiedAccount:
    user_id: str


class SessionVerifier(Protocol):
    def __call__(self, token: str) -> VerifiedAccount:
        """Verify the session and return its server-mapped internal account ID."""
        ...


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


def create_account_app(*, db_path: Path, verify_session: SessionVerifier) -> FastAPI:
    if not callable(verify_session):
        raise ValueError('A session verifier is required')
    application = FastAPI(title='Jot account API', version='1.0.0')
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

    @application.exception_handler(AccountConflict)
    async def conflict(_request, error):
        return JSONResponse(status_code=409, content={'detail': str(error)})

    @application.get('/healthz')
    def healthz():
        return {'status': 'ok'}

    router = APIRouter(prefix='/api/v1', dependencies=[Depends(scoped_store)])

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

    @router.post('/ingests', status_code=501)
    def ingest(_payload: IngestRequest):
        raise HTTPException(501, 'Account ingest processing is not enabled yet')

    @router.post('/shortcut/ingests', status_code=501)
    def shortcut_ingest(_payload: ShortcutIngestRequest):
        raise HTTPException(501, 'Account ingest processing is not enabled yet')

    @router.post('/activity/{ingest_id}/retry', status_code=501)
    def retry(ingest_id: int, account: AccountStore = Depends(scoped_store)):
        account.require_run(ingest_id)
        raise HTTPException(501, 'Account ingest processing is not enabled yet')

    application.include_router(router)
    return application
