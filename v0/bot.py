"""Telegram transport adapter for the shared ingest service."""
from __future__ import annotations

import asyncio
import logging
import re

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from ingest_service import IngestService

URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")
# python-telegram-bot logs Bot API request URLs through httpx at INFO level;
# those URLs contain the Telegram token. Keep them out of application logs.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _format_timestamp(value: object) -> str | None:
    try:
        seconds = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return None
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _format_result(result: dict) -> str:
    resolved = result.get("resolved_things", result.get("resolved_places", []))
    if not resolved:
        return "⚠️ Source saved, but no individual things were extracted."

    lines = []
    for r in resolved:
        extracted = r.get("extracted", {}) or {}
        status = r.get("status")
        name = extracted.get("extracted_name", "?")
        thing_type = extracted.get("type_name")
        description = extracted.get("description") or extracted.get("why_its_cool")
        dishes = extracted.get("dishes") or []
        dishes_str = ", ".join(dishes[:3]) + ("…" if len(dishes) > 3 else "")
        timestamp = _format_timestamp(extracted.get("timestamp_seconds"))
        slide_index = extracted.get("slide_index")

        if status == "auto":
            place = r["place"]
            display = (place.get("displayName") or {}).get("text", name)
            addr = place.get("shortFormattedAddress") or place.get("formattedAddress", "")
            url = place.get("googleMapsUri", "")
            lines.append(f"✅ *{display}*")
            if addr:
                lines.append(f"📍 {addr}")
            if dishes:
                lines.append(f"🍽 {dishes_str}")
            if slide_index:
                lines.append(f"🖼 Slide {slide_index}")
            if timestamp:
                lines.append(f"🎬 Appears at {timestamp}")
            if url:
                lines.append(f"🗺 [view on Google Maps]({url})")
        elif status == "not_applicable":
            lines.append(f"✅ *{name}*")
            if thing_type:
                lines.append(f"📁 {thing_type}")
            if description:
                lines.append(description)
            if slide_index:
                lines.append(f"🖼 Slide {slide_index}")
            if timestamp:
                lines.append(f"🎬 Appears at {timestamp}")
        elif status == "needs_review":
            count = len(r.get("candidates") or [])
            lines.append(f"⚠️ *{name}* — {count} possible matches, need to disambiguate")
        else:
            reason = r.get("reason", "")
            lines.append(f"⚠️ *{name}* — unresolved ({reason})")

        lines.append("")  # blank line between places

    return "\n".join(lines).strip()


def build_application(
    token: str,
    service: IngestService,
    allowed_ids: set[int],
) -> Application:
    application = Application.builder().token(token).updater(None).build()
    application.bot_data["ingest_service"] = service
    application.bot_data["allowed_ids"] = allowed_ids
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    return application


async def send_ingest_result(
    application: Application,
    chat_id: int,
    service: IngestService,
    source_url: str,
    user_prompt: str | None = None,
) -> dict:
    """Run an ingest and report progress/results to a Telegram chat."""
    ack = await application.bot.send_message(chat_id, "🔎 Working on it…")
    try:
        result = await asyncio.to_thread(service.ingest, source_url, user_prompt)
        await ack.edit_text(
            _format_result(result),
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )
        return result
    except Exception as exc:
        log.exception("ingest failed")
        await ack.edit_text(f"❌ {type(exc).__name__}: {exc}")
        raise


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None:
        return

    user_id = msg.from_user.id if msg.from_user else None
    allowed_ids: set[int] = ctx.application.bot_data["allowed_ids"]
    if allowed_ids and user_id not in allowed_ids:
        log.info("ignoring message from unauthorized user_id=%s", user_id)
        return

    text = msg.text or ""
    urls = URL_RE.findall(text)
    source_url = urls[0] if urls else None
    user_prompt = URL_RE.sub("", text).strip() or None

    if not source_url:
        await msg.reply_text("Send me a public Instagram or YouTube URL, optionally with a note.")
        return

    try:
        service: IngestService = ctx.application.bot_data["ingest_service"]
        await send_ingest_result(
            ctx.application,
            msg.chat_id,
            service,
            source_url,
            user_prompt,
        )
    except Exception:
        # send_ingest_result already logs and reports a safe user-facing failure.
        pass
