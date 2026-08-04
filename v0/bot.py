"""
Telegram bot (webhook mode). Receives a URL (+ optional text), runs the pipeline,
replies with the result, persists to SQLite.

Run with:
    source .venv/bin/activate
    python bot.py

Requires cloudflared tunnel (or real host) pointing at localhost:$PORT and PUBLIC_URL
set to that tunnel URL in .env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

load_dotenv(Path(__file__).parent / ".env")

from pipeline import process_ingest       # noqa: E402
from store import init_db, save_ingest    # noqa: E402


DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "data" / "places.db"))
WORKDIR = Path(os.environ.get("WORKDIR", Path(__file__).parent / "data" / "downloads"))

ALLOWED_IDS = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip()
}

URL_RE = re.compile(r"https?://\S+")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


def _format_result(result: dict) -> str:
    resolved = result.get("resolved_places", [])
    if not resolved:
        return "⚠️ No places extracted from this source."

    lines = []
    for r in resolved:
        extracted = r.get("extracted", {}) or {}
        status = r.get("status")
        name = extracted.get("extracted_name", "?")
        dishes = extracted.get("dishes") or []
        dishes_str = ", ".join(dishes[:3]) + ("…" if len(dishes) > 3 else "")

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
            if url:
                lines.append(f"🗺 [view on Google Maps]({url})")
        elif status == "needs_review":
            count = len(r.get("candidates") or [])
            lines.append(f"⚠️ *{name}* — {count} possible matches, need to disambiguate")
        else:
            reason = r.get("reason", "")
            lines.append(f"⚠️ *{name}* — unresolved ({reason})")

        lines.append("")  # blank line between places

    return "\n".join(lines).strip()


async def handle(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if msg is None:
        return

    user_id = msg.from_user.id if msg.from_user else None
    if ALLOWED_IDS and user_id not in ALLOWED_IDS:
        log.info("ignoring message from unauthorized user_id=%s", user_id)
        return

    text = msg.text or ""
    urls = URL_RE.findall(text)
    source_url = urls[0] if urls else None
    user_prompt = URL_RE.sub("", text).strip() or None

    if not source_url:
        await msg.reply_text("Send me a URL (Instagram Reel / TikTok / YouTube), optionally with a note.")
        return

    ack = await msg.reply_text("🔎 Working on it…")

    try:
        result = await asyncio.to_thread(process_ingest, source_url, user_prompt, WORKDIR)
        await asyncio.to_thread(save_ingest, DB_PATH, result)
        await ack.edit_text(_format_result(result), parse_mode="Markdown", disable_web_page_preview=True)
    except Exception as exc:
        log.exception("ingest failed")
        await ack.edit_text(f"❌ {type(exc).__name__}: {exc}")


def main() -> None:
    init_db(DB_PATH)

    token = os.environ["TELEGRAM_BOT_TOKEN"]

    public_url = os.environ.get("PUBLIC_URL")
    if not public_url:
        app_name = os.environ.get("FLY_APP_NAME")
        if not app_name:
            raise RuntimeError("Set PUBLIC_URL or run on Fly (FLY_APP_NAME auto-injected)")
        public_url = f"https://{app_name}.fly.dev"
    public_url = public_url.rstrip("/")

    port = int(os.environ.get("PORT", "8000"))

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    log.info("Starting bot in webhook mode; registering %s/webhook", public_url)
    app.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path="webhook",
        webhook_url=f"{public_url}/webhook",
    )


if __name__ == "__main__":
    main()
