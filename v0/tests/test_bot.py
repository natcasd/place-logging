from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_formats_duplicate_source(self) -> None:
        self.assertEqual(bot._format_result({"already_logged": True}), "Already logged")

    def test_formats_saved_source_after_extraction_outage(self) -> None:
        formatted = bot._format_result(
            {
                "metadata": {"extraction_status": "failed"},
                "resolved_things": [],
            }
        )

        self.assertIn("Source saved for review", formatted)
        self.assertIn("retried later", formatted)

    def test_formats_timestamp_and_slide_reference(self) -> None:
        result = {
            "resolved_places": [
                {
                    "status": "auto",
                    "extracted": {
                        "extracted_name": "Test Place",
                        "dishes": [],
                        "timestamp_seconds": 73.6,
                        "slide_index": 4,
                    },
                    "place": {"displayName": {"text": "Test Place"}},
                }
            ]
        }

        formatted = bot._format_result(result)

        self.assertIn("🖼 Slide 4", formatted)
        self.assertIn("🎬 Appears at 1:14", formatted)

    def test_formats_thing_identity_and_existing_source_outcome(self) -> None:
        result = {
            "saved_things": [
                {
                    "name": "Giacometti in the Temple of Dendur",
                    "type": "Exhibit",
                    "is_new": False,
                    "source_count": 2,
                }
            ],
            "resolved_things": [
                {
                    "status": "auto",
                    "extracted": {
                        "extracted_name": "Giacometti in the Temple of Dendur",
                        "type_name": "Exhibit",
                    },
                    "place": {"displayName": {"text": "The Met"}},
                }
            ],
        }

        formatted = bot._format_result(result)

        self.assertIn("*Giacometti in the Temple of Dendur*", formatted)
        self.assertIn("📍 The Met", formatted)
        self.assertIn("Added source · 2 total", formatted)

    @patch("bot.send_ingest_result", new_callable=AsyncMock)
    async def test_url_message_calls_shared_ingest_service(
        self,
        mock_send: AsyncMock,
    ) -> None:
        service = MagicMock()
        application = SimpleNamespace(
            bot_data={"allowed_ids": {123}, "ingest_service": service}
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=123),
            text="https://youtu.be/test dinner ideas",
            chat_id=456,
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(message=message)
        context = SimpleNamespace(application=application)

        await bot.handle(update, context)

        mock_send.assert_awaited_once_with(
            application,
            456,
            service,
            "https://youtu.be/test",
            "dinner ideas",
        )

    @patch("bot.send_ingest_result", new_callable=AsyncMock)
    async def test_unauthorized_message_is_ignored(
        self,
        mock_send: AsyncMock,
    ) -> None:
        application = SimpleNamespace(
            bot_data={"allowed_ids": {123}, "ingest_service": MagicMock()}
        )
        message = SimpleNamespace(
            from_user=SimpleNamespace(id=999),
            text="https://youtu.be/test",
            chat_id=999,
            reply_text=AsyncMock(),
        )

        await bot.handle(
            SimpleNamespace(message=message),
            SimpleNamespace(application=application),
        )

        mock_send.assert_not_awaited()
        message.reply_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
