"""HTTP entrypoint for versioned ingest clients."""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

load_dotenv(Path(__file__).parent / ".env")

from ingest_service import IngestService  # noqa: E402
from movie_enrichment import WikidataMovieProvider  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("app")


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1, max_length=4096)


class ShortcutIngestRequest(BaseModel):
    """Apple Shortcuts transport envelope.

    Base64 makes the URL a plain string before it crosses Shortcuts' network
    permission boundary. The decoded value immediately enters the canonical
    IngestRequest flow below.
    """

    model_config = ConfigDict(extra="forbid")

    source_url_base64: str = Field(min_length=4, max_length=8192)


class ReviewLocationCandidate(BaseModel):
    id: str
    name: str
    formatted_address: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class SavedEntryOutcome(BaseModel):
    entry_id: int
    source_connection_id: int | None = None
    ordinal: int = 0
    name: str
    type: str
    description: str = ""
    location_id: int | None = None
    location_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    formatted_address: str | None = None
    google_maps_url: str | None = None
    timestamp_seconds: float | None = Field(default=None, ge=0)
    slide_index: int | None = Field(default=None, ge=1)
    resolution_status: str
    review_candidates: list[ReviewLocationCandidate] = Field(default_factory=list)
    is_new: bool
    source_count: int = Field(ge=1)


class IngestResponse(BaseModel):
    ingest_id: int
    item_id: int
    source_url: str
    metadata: dict[str, Any]
    places_extracted: list[dict[str, Any]]
    resolved_places: list[dict[str, Any]]
    entries_extracted: list[dict[str, Any]] = Field(default_factory=list)
    resolved_entries: list[dict[str, Any]] = Field(default_factory=list)
    saved_entries: list[SavedEntryOutcome] = Field(default_factory=list)
    already_logged: bool = False


class ShortcutDiagnosticResponse(BaseModel):
    request_id: str
    content_type: str | None
    body_bytes: int
    body_sha256: str


class SavedEntrySource(BaseModel):
    id: int
    item_id: int
    ordinal: int
    name: str
    type: str
    source_url: str
    source_platform: str
    creator: str | None = None
    description: str = ""
    dishes: list[str]
    why_its_cool: str
    tags: list[str]
    timestamp_seconds: float | None = Field(default=None, ge=0)
    slide_index: int | None = Field(default=None, ge=1)
    resolution_status: str
    location_query: str | None = None
    saved_at: str


class MovieEnrichment(BaseModel):
    provider: str | None = None
    provider_id: str | None = None
    resolved_title: str | None = None
    release_year: int | None = None
    letterboxd_url: str | None = None
    web_search_url: str
    match_status: str
    match_confidence: float | None = None


class SavedEntry(BaseModel):
    id: int
    location_id: int | None = None
    item_id: int
    ordinal: int
    name: str
    google_place_id: str | None
    latitude: float | None
    longitude: float | None
    formatted_address: str | None
    google_maps_url: str | None
    location_name: str | None = None
    dishes: list[str]
    why_its_cool: str
    tags: list[str]
    timestamp_seconds: float | None = Field(default=None, ge=0)
    slide_index: int | None = Field(default=None, ge=1)
    resolution_status: str
    type: str = "Place"
    description: str = ""
    starts_at: str | None = None
    ends_at: str | None = None
    recurrence_text: str | None = None
    location_query: str | None = None
    movie_enrichment: MovieEnrichment | None = None
    source_url: str
    saved_at: str
    sources: list[SavedEntrySource] = Field(default_factory=list)


class PlacesResponse(BaseModel):
    places: list[SavedEntry]


class EntriesResponse(BaseModel):
    entries: list[SavedEntry]


class SavedSource(BaseModel):
    id: int
    source_url: str
    source_platform: str
    creator: str | None
    caption: str | None
    summary: str | None = None
    media_count: int
    media_preserved: bool
    entry_count: int
    needs_review: bool
    saved_at: str


class SourcesResponse(BaseModel):
    sources: list[SavedSource]


class IngestActivityEvent(BaseModel):
    id: int
    stage: str
    status: str
    message: str
    created_at: str


class IngestActivity(BaseModel):
    id: int
    item_id: int | None = None
    source_url: str
    source_platform: str
    creator: str | None = None
    caption: str | None = None
    summary: str | None = None
    status: str
    stage: str
    error_type: str | None = None
    error_message: str | None = None
    started_at: str
    updated_at: str
    completed_at: str | None = None
    results: list[SavedEntryOutcome] = Field(default_factory=list)
    events: list[IngestActivityEvent] = Field(default_factory=list)


class ActivityResponse(BaseModel):
    activity: list[IngestActivity]


class ConfirmActivityLocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=512)


class ConfirmActivityLocationResponse(BaseModel):
    entry: SavedEntryOutcome


class DeletePlaceResponse(BaseModel):
    place_id: int
    deleted_places: int
    deleted_items: int


class DeleteEntryResponse(BaseModel):
    entry_id: int
    deleted_entries: int
    deleted_sources: int


class DeleteEntriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entry_ids: list[int] = Field(min_length=1, max_length=100)


class DeleteEntriesResponse(BaseModel):
    entry_ids: list[int]
    deleted_entries: int
    deleted_sources: int


@dataclass
class Runtime:
    service: IngestService
    ingest_api_token: str


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Set {name}")
    return value


def _validation_diagnostics(exc: RequestValidationError) -> dict[str, Any]:
    """Describe malformed API input without logging secrets or full payloads."""
    body = exc.body
    diagnostics: dict[str, Any] = {
        "body_type": type(body).__name__,
        "errors": [
            {
                "location": list(error.get("loc", ())),
                "type": error.get("type"),
                "input_type": type(error.get("input")).__name__,
            }
            for error in exc.errors()
        ],
    }
    if isinstance(body, dict):
        diagnostics["body_keys"] = sorted(str(key) for key in body)
        if "source_url" in body:
            source_url = body["source_url"]
            diagnostics["source_url_type"] = type(source_url).__name__
            if isinstance(source_url, str):
                diagnostics["source_url_preview"] = repr(source_url[:500])
            elif isinstance(source_url, (bytes, bytearray)):
                diagnostics["source_url_bytes"] = len(source_url)
            else:
                diagnostics["source_url_shape"] = _safe_shape(source_url)
    return diagnostics


def _safe_shape(value: Any, depth: int = 0) -> Any:
    """Return bounded diagnostic structure while redacting likely secrets."""
    if depth >= 3:
        return f"<{type(value).__name__}>"
    if isinstance(value, dict):
        shaped = {}
        for key, child in list(value.items())[:20]:
            key_text = str(key)
            if any(
                marker in key_text.lower()
                for marker in ("auth", "token", "secret", "password", "prompt")
            ):
                shaped[key_text] = "<redacted>"
            else:
                shaped[key_text] = _safe_shape(child, depth + 1)
        return shaped
    if isinstance(value, (list, tuple)):
        return [_safe_shape(child, depth + 1) for child in list(value)[:10]]
    if isinstance(value, str):
        return {"type": "str", "repr": repr(value[:500]), "length": len(value)}
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"type": type(value).__name__, "length": len(value)}
    return f"<{type(value).__name__}>"


def _require_ingest_auth(runtime: Runtime, authorization: str | None) -> None:
    expected = f"Bearer {runtime.ingest_api_token}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


def build_runtime() -> Runtime:
    root = Path(__file__).parent
    default_workdir = Path(tempfile.gettempdir()) / "place-logger-downloads"
    service = IngestService(
        db_path=Path(os.environ.get("DB_PATH", root / "data" / "places.db")),
        workdir=Path(os.environ.get("WORKDIR", default_workdir)),
        movie_provider=WikidataMovieProvider(),
    )
    service.initialize()
    return Runtime(
        service=service,
        ingest_api_token=_required_env("INGEST_API_TOKEN"),
    )


def create_app(injected_runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime = injected_runtime or build_runtime()
        application.state.runtime = runtime
        yield

    application = FastAPI(
        title="Place Logger API",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_observability(request: Request, call_next):
        request_id = request.headers.get("fly-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "HTTP request crashed request_id=%s method=%s path=%s",
                request_id,
                request.method,
                request.url.path,
            )
            raise
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        log.info(
            "HTTP request request_id=%s method=%s path=%s status=%s "
            "duration_ms=%s content_type=%r content_length=%r",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
            request.headers.get("content-type"),
            request.headers.get("content-length"),
        )
        response.headers["X-Request-ID"] = request_id
        return response

    @application.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        log.warning(
            "Request validation failed request_id=%s path=%s diagnostics=%s",
            getattr(request.state, "request_id", "unknown"),
            request.url.path,
            _validation_diagnostics(exc),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": jsonable_encoder(exc.errors())},
        )

    @application.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/v1/places", response_model=PlacesResponse)
    async def get_places(
        request: Request,
        authorization: str | None = Header(default=None),
        limit: int = 200,
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="limit must be between 1 and 500",
            )
        return {"places": await asyncio.to_thread(runtime.service.places, limit)}

    @application.get("/api/v1/entries", response_model=EntriesResponse)
    async def get_entries(
        request: Request,
        authorization: str | None = Header(default=None),
        limit: int = 200,
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        if not 1 <= limit <= 1_000:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="limit must be between 1 and 1000",
            )
        return {"entries": await asyncio.to_thread(runtime.service.entries, limit)}

    @application.get("/api/v1/sources", response_model=SourcesResponse)
    async def get_sources(
        request: Request,
        authorization: str | None = Header(default=None),
        limit: int = 200,
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="limit must be between 1 and 500",
            )
        return {"sources": await asyncio.to_thread(runtime.service.sources, limit)}

    @application.get("/api/v1/activity", response_model=ActivityResponse)
    async def get_activity(
        request: Request,
        authorization: str | None = Header(default=None),
        limit: int = 200,
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        if not 1 <= limit <= 500:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="limit must be between 1 and 500",
            )
        return {"activity": await asyncio.to_thread(runtime.service.activity, limit)}

    @application.post(
        "/api/v1/activity/{ingest_id}/entries/{entry_id}/location",
        response_model=ConfirmActivityLocationResponse,
    )
    async def confirm_activity_entry_location(
        ingest_id: int,
        entry_id: int,
        payload: ConfirmActivityLocationRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        try:
            result = await asyncio.to_thread(
                runtime.service.confirm_activity_location,
                ingest_id,
                entry_id,
                payload.candidate_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Activity recommendation not found",
            )
        return {"entry": result}

    @application.delete(
        "/api/v1/places/{place_id}",
        response_model=DeletePlaceResponse,
    )
    async def delete_saved_place(
        place_id: int,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, int]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        result = await asyncio.to_thread(runtime.service.delete_place, place_id)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Saved place not found",
            )
        log.info(
            "Saved place deleted request_id=%s place_id=%s deleted_places=%s "
            "deleted_items=%s",
            getattr(request.state, "request_id", "unknown"),
            place_id,
            result["deleted_places"],
            result["deleted_items"],
        )
        return {"place_id": place_id, **result}

    @application.delete(
        "/api/v1/entries/{entry_id}",
        response_model=DeleteEntryResponse,
    )
    async def delete_saved_entry(
        entry_id: int,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, int]:
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        result = await asyncio.to_thread(runtime.service.delete_entry, entry_id)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Saved entry not found",
            )
        return {
            "entry_id": entry_id,
            "deleted_entries": result["deleted_entries"],
            "deleted_sources": result["deleted_sources"],
        }

    @application.delete(
        "/api/v1/entries",
        response_model=DeleteEntriesResponse,
    )
    async def delete_saved_entries(
        payload: DeleteEntriesRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Delete canonical Entries atomically while preserving source posts."""
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)
        entry_ids = list(dict.fromkeys(payload.entry_ids))
        result = await asyncio.to_thread(runtime.service.delete_entries, entry_ids)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="One or more saved entries were not found",
            )
        return {"entry_ids": entry_ids, **result}

    @application.post("/api/v1/ingests", response_model=IngestResponse)
    async def create_ingest(
        payload: IngestRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        try:
            _require_ingest_auth(runtime, authorization)
        except HTTPException:
            log.warning(
                "Ingest authentication rejected request_id=%s",
                getattr(request.state, "request_id", "unknown"),
            )
            raise

        ingest_started = time.perf_counter()
        log.info(
            "Ingest accepted request_id=%s source_url=%r",
            getattr(request.state, "request_id", "unknown"),
            payload.source_url,
        )

        try:
            result = await asyncio.to_thread(
                runtime.service.ingest,
                payload.source_url,
            )
        except (ValueError, NotImplementedError) as exc:
            log.warning(
                "Ingest rejected request_id=%s error_type=%s detail=%r "
                "source_url=%r duration_ms=%s",
                getattr(request.state, "request_id", "unknown"),
                type(exc).__name__,
                str(exc),
                payload.source_url,
                round((time.perf_counter() - ingest_started) * 1000, 1),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log.exception(
                "API ingest failed request_id=%s source_url=%r duration_ms=%s",
                getattr(request.state, "request_id", "unknown"),
                payload.source_url,
                round((time.perf_counter() - ingest_started) * 1000, 1),
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Place Logger could not process that link",
            ) from exc
        log.info(
            "Ingest completed request_id=%s item_id=%s entries=%s "
            "duration_ms=%s",
            getattr(request.state, "request_id", "unknown"),
            result.get("item_id"),
            len(result.get("entries_extracted", result.get("places_extracted", []))),
            round((time.perf_counter() - ingest_started) * 1000, 1),
        )
        return result

    @application.post(
        "/api/v1/shortcut/diagnostics",
        response_model=ShortcutDiagnosticResponse,
    )
    async def create_shortcut_diagnostic(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Record how Apple Shortcuts serialized a share item; never ingest it."""
        runtime: Runtime = request.app.state.runtime
        _require_ingest_auth(runtime, authorization)

        body = await request.body()
        if len(body) > 2_000_000:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Diagnostic body exceeds 2 MB",
            )

        content_type = request.headers.get("content-type")
        body_sha256 = hashlib.sha256(body).hexdigest()
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            diagnostics: Any = {
                "body_type": "binary",
                "prefix_hex": body[:256].hex(),
            }
        else:
            try:
                parsed = json.loads(decoded)
            except json.JSONDecodeError:
                diagnostics = {
                    "body_type": "text",
                    "preview": repr(decoded[:4000]),
                    "text_length": len(decoded),
                }
            else:
                diagnostics = {
                    "body_type": "json",
                    "shape": _safe_shape(parsed),
                }

        request_id = getattr(request.state, "request_id", "unknown")
        log.warning(
            "Shortcut diagnostic request_id=%s content_type=%r body_bytes=%s "
            "body_sha256=%s diagnostics=%s",
            request_id,
            content_type,
            len(body),
            body_sha256,
            diagnostics,
        )
        return {
            "request_id": request_id,
            "content_type": content_type,
            "body_bytes": len(body),
            "body_sha256": body_sha256,
        }

    @application.post(
        "/api/v1/shortcut/ingests",
        response_model=IngestResponse,
    )
    async def create_shortcut_ingest(
        payload: ShortcutIngestRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Decode the Shortcuts envelope, then use the canonical ingest API."""
        try:
            source_url = base64.b64decode(
                payload.source_url_base64,
                validate=True,
            ).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="source_url_base64 must encode a UTF-8 URL",
            ) from exc

        if not 1 <= len(source_url) <= 4096:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Decoded source URL must contain 1 to 4096 characters",
            )

        return await create_ingest(
            IngestRequest(
                source_url=source_url,
            ),
            request,
            authorization,
        )

    return application


app = create_app()
