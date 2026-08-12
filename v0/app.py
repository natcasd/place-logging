"""HTTP entrypoint for Telegram and versioned ingest clients."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
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
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

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
            if notification is not None:
                await notification.edit_text(f"❌ {type(exc).__name__}: {exc}")
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except Exception as exc:
            log.exception("API ingest failed")
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
        return {**result, "delivery_status": delivery_status}

    return application


app = create_app()
