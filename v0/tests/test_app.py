from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from app import Runtime, create_app


def canonical_result() -> dict:
    return {
        "item_id": 12,
        "source_url": "https://youtu.be/test",
        "user_prompt": None,
        "metadata": {"source_platform": "youtube"},
        "places_extracted": [{"extracted_name": "Test Place"}],
        "resolved_places": [
            {
                "extracted": {"extracted_name": "Test Place"},
                "status": "unresolved",
                "reason": "test",
            }
        ],
    }


class FakeTelegram:
    def __init__(self) -> None:
        self.bot = MagicMock()
        self.bot.send_message = AsyncMock()
        self.update_queue: asyncio.Queue = asyncio.Queue()


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MagicMock()
        self.service.ingest.return_value = canonical_result()
        self.telegram = FakeTelegram()
        self.runtime = Runtime(
            service=self.service,
            telegram=self.telegram,  # type: ignore[arg-type]
            ingest_api_token="api-secret",
            telegram_webhook_secret="webhook-secret",
            shortcut_chat_id=123,
        )
        self.client_context = TestClient(create_app(self.runtime))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_health_check_needs_no_authentication(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_ingest_requires_bearer_token(self) -> None:
        response = self.client.post(
            "/api/v1/ingests",
            json={"source_url": "https://youtu.be/test"},
        )
        self.assertEqual(response.status_code, 401)
        self.service.ingest.assert_not_called()

    def test_ingest_calls_shared_service_and_returns_result(self) -> None:
        response = self.client.post(
            "/api/v1/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={"source_url": "https://youtu.be/test"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item_id"], 12)
        self.assertEqual(response.json()["delivery_status"], "not_requested")
        self.service.ingest.assert_called_once_with(
            "https://youtu.be/test",
            None,
        )

    def test_telegram_delivery_reports_result(self) -> None:
        notification = SimpleNamespace(edit_text=AsyncMock())
        self.telegram.bot.send_message.return_value = notification

        response = self.client.post(
            "/api/v1/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={
                "source_url": "https://youtu.be/test",
                "delivery": "telegram",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["delivery_status"], "sent")
        self.telegram.bot.send_message.assert_awaited_once()
        notification.edit_text.assert_awaited_once()

    def test_delivery_failure_does_not_fail_saved_ingest(self) -> None:
        self.telegram.bot.send_message.side_effect = RuntimeError("Telegram down")

        response = self.client.post(
            "/api/v1/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={
                "source_url": "https://youtu.be/test",
                "delivery": "telegram",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item_id"], 12)
        self.assertEqual(response.json()["delivery_status"], "failed")

    def test_webhook_rejects_missing_telegram_secret(self) -> None:
        response = self.client.post("/webhook", json={"update_id": 1})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(self.telegram.update_queue.empty())

    def test_webhook_queues_verified_telegram_update(self) -> None:
        response = self.client.post(
            "/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "webhook-secret"},
            json={"update_id": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(self.telegram.update_queue.qsize(), 1)


if __name__ == "__main__":
    unittest.main()
