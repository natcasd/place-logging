"""HTTP entrypoint for Telegram and versioned ingest clients."""
from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from telegram import Update
from telegram.ext import Application

load_dotenv(Path(__file__).parent / ".env")

from bot import _format_result, build_application  # noqa: E402
from ingest_service import IngestService  # noqa: E402


logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("app")
# Telegram Bot API URLs contain the bot token. Never emit them to app logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str = Field(min_length=1, max_length=4096)
    user_prompt: str | None = Field(default=None, max_length=4000)
    delivery: Literal["response_only", "telegram"] = "response_only"


class ShortcutIngestRequest(BaseModel):
    """Apple Shortcuts transport envelope.

    Base64 makes the URL a plain string before it crosses Shortcuts' network
    permission boundary. The decoded value immediately enters the canonical
    IngestRequest flow below.
    """

    model_config = ConfigDict(extra="forbid")

    source_url_base64: str = Field(min_length=4, max_length=8192)
    user_prompt: str | None = Field(default=None, max_length=4000)
    delivery: Literal["response_only", "telegram"] = "response_only"


class IngestResponse(BaseModel):
    item_id: int
    source_url: str
    user_prompt: str | None
    metadata: dict[str, Any]
    places_extracted: list[dict[str, Any]]
    resolved_places: list[dict[str, Any]]
    delivery_status: Literal["not_requested", "sent", "failed"]


@dataclass
class Runtime:
    service: IngestService
    telegram: Application
    ingest_api_token: str
    telegram_webhook_secret: str
    shortcut_chat_id: int | None


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Set {name}")
    return value


def _public_url() -> str:
    configured = os.environ.get("PUBLIC_URL")
    if configured:
        return configured.rstrip("/")
    app_name = os.environ.get("FLY_APP_NAME")
    if not app_name:
        raise RuntimeError("Set PUBLIC_URL or run on Fly (FLY_APP_NAME auto-injected)")
    return f"https://{app_name}.fly.dev"


def _allowed_ids() -> set[int]:
    return {
        int(value)
        for value in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if value.strip()
    }


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
        if "delivery" in body:
            diagnostics["delivery"] = _safe_shape(body["delivery"])
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


def build_runtime() -> Runtime:
    root = Path(__file__).parent
    service = IngestService(
        db_path=Path(os.environ.get("DB_PATH", root / "data" / "places.db")),
        workdir=Path(os.environ.get("WORKDIR", root / "data" / "downloads")),
    )
    service.initialize()
    allowed_ids = _allowed_ids()
    shortcut_chat_id_value = os.environ.get("SHORTCUT_TELEGRAM_CHAT_ID")
    shortcut_chat_id = (
        int(shortcut_chat_id_value)
        if shortcut_chat_id_value
        else next(iter(allowed_ids), None)
    )
    telegram = build_application(
        _required_env("TELEGRAM_BOT_TOKEN"),
        service,
        allowed_ids,
    )
    return Runtime(
        service=service,
        telegram=telegram,
        ingest_api_token=_required_env("INGEST_API_TOKEN"),
        telegram_webhook_secret=_required_env("TELEGRAM_WEBHOOK_SECRET"),
        shortcut_chat_id=shortcut_chat_id,
    )


def create_app(injected_runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        runtime = injected_runtime or build_runtime()
        application.state.runtime = runtime
        if injected_runtime is None:
            await runtime.telegram.initialize()
            await runtime.telegram.start()
            webhook_url = f"{_public_url()}/webhook"
            await runtime.telegram.bot.set_webhook(
                url=webhook_url,
                secret_token=runtime.telegram_webhook_secret,
                allowed_updates=Update.ALL_TYPES,
            )
            log.info("Telegram webhook registered at %s", webhook_url)
        try:
            yield
        finally:
            if injected_runtime is None:
                await runtime.telegram.stop()
                await runtime.telegram.shutdown()

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

    @application.post("/webhook", include_in_schema=False)
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: str | None = Header(default=None),
    ) -> dict[str, bool]:
        runtime: Runtime = request.app.state.runtime
        supplied = x_telegram_bot_api_secret_token or ""
        if not secrets.compare_digest(supplied, runtime.telegram_webhook_secret):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        update = Update.de_json(await request.json(), runtime.telegram.bot)
        await runtime.telegram.update_queue.put(update)
        return {"ok": True}

    @application.post("/api/v1/ingests", response_model=IngestResponse)
    async def create_ingest(
        payload: IngestRequest,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        runtime: Runtime = request.app.state.runtime
        expected = f"Bearer {runtime.ingest_api_token}"
        if not authorization or not secrets.compare_digest(authorization, expected):
            log.warning(
                "Ingest authentication rejected request_id=%s",
                getattr(request.state, "request_id", "unknown"),
            )
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        ingest_started = time.perf_counter()
        log.info(
            "Ingest accepted request_id=%s source_url=%r delivery=%s",
            getattr(request.state, "request_id", "unknown"),
            payload.source_url,
            payload.delivery,
        )

        notification = None
        delivery_status: Literal["not_requested", "sent", "failed"] = (
            "not_requested"
        )
        if payload.delivery == "telegram":
            if runtime.shortcut_chat_id is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Telegram delivery is not configured",
                )
            try:
                notification = await runtime.telegram.bot.send_message(
                    runtime.shortcut_chat_id,
                    "🔎 Working on your shared link…",
                )
            except Exception:
                delivery_status = "failed"
                log.exception("Could not send Telegram ingest acknowledgement")

        try:
            result = await asyncio.to_thread(
                runtime.service.ingest,
                payload.source_url,
                payload.user_prompt,
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
            if notification is not None:
                await notification.edit_text(f"❌ {type(exc).__name__}: {exc}")
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
            if notification is not None:
                await notification.edit_text("❌ Place Logger could not process that link.")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Place Logger could not process that link",
            ) from exc

        if notification is not None:
            try:
                await notification.edit_text(
                    _format_result(result),
                    parse_mode="Markdown",
                    disable_web_page_preview=True,
                )
                delivery_status = "sent"
            except Exception:
                delivery_status = "failed"
                log.exception("Ingest saved but Telegram result delivery failed")
        log.info(
            "Ingest completed request_id=%s item_id=%s places=%s "
            "delivery_status=%s duration_ms=%s",
            getattr(request.state, "request_id", "unknown"),
            result.get("item_id"),
            len(result.get("places_extracted", [])),
            delivery_status,
            round((time.perf_counter() - ingest_started) * 1000, 1),
        )
        return {**result, "delivery_status": delivery_status}

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
                user_prompt=payload.user_prompt,
                delivery=payload.delivery,
            ),
            request,
            authorization,
        )

    return application


app = create_app()
