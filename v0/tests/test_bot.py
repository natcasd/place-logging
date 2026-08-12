from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import bot


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
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
