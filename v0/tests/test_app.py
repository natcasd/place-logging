from __future__ import annotations

import asyncio
import base64
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

    def test_places_requires_bearer_token(self) -> None:
        response = self.client.get("/api/v1/places")

        self.assertEqual(response.status_code, 401)
        self.service.places.assert_not_called()

    def test_places_returns_saved_places(self) -> None:
        self.service.places.return_value = [
            {
                "id": 7,
                "item_id": 12,
                "ordinal": 0,
                "name": "Test Place",
                "google_place_id": "places/test",
                "latitude": 40.7,
                "longitude": -74.0,
                "formatted_address": "123 Test St",
                "google_maps_url": "https://maps.google.com/test",
                "dishes": ["cream soda"],
                "why_its_cool": "A classic.",
                "tags": ["deli"],
                "timestamp_seconds": 13.2,
                "slide_index": None,
                "resolution_status": "resolved",
                "source_url": "https://youtu.be/test",
                "saved_at": "2026-08-13 12:00:00",
            }
        ]

        response = self.client.get(
            "/api/v1/places?limit=25",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["places"][0]["name"], "Test Place")
        self.assertEqual(response.json()["places"][0]["timestamp_seconds"], 13.2)
        self.assertIsNone(response.json()["places"][0]["slide_index"])
        self.service.places.assert_called_once_with(25)

    def test_places_rejects_excessive_limit(self) -> None:
        response = self.client.get(
            "/api/v1/places?limit=501",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 422)
        self.service.places.assert_not_called()

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

    def test_shortcut_adapter_decodes_url_into_shared_ingest_flow(self) -> None:
        source_url = "https://www.instagram.com/reel/test/"
        encoded_url = base64.b64encode(source_url.encode()).decode()

        response = self.client.post(
            "/api/v1/shortcut/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={
                "source_url_base64": encoded_url,
                "delivery": "telegram",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.service.ingest.assert_called_once_with(source_url, None)

    def test_shortcut_adapter_rejects_invalid_base64(self) -> None:
        response = self.client.post(
            "/api/v1/shortcut/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={"source_url_base64": "not-valid-base64!"},
        )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["detail"],
            "source_url_base64 must encode a UTF-8 URL",
        )
        self.service.ingest.assert_not_called()

    def test_shortcut_adapter_rejects_non_ascii_base64(self) -> None:
        response = self.client.post(
            "/api/v1/shortcut/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={"source_url_base64": "not-base64-🚫"},
        )

        self.assertEqual(response.status_code, 422)
        self.service.ingest.assert_not_called()

    def test_shortcut_adapter_requires_bearer_token(self) -> None:
        encoded_url = base64.b64encode(b"https://youtu.be/test").decode()

        response = self.client.post(
            "/api/v1/shortcut/ingests",
            json={"source_url_base64": encoded_url},
        )

        self.assertEqual(response.status_code, 401)
        self.service.ingest.assert_not_called()

    def test_shortcut_diagnostic_logs_shape_without_ingesting(self) -> None:
        with self.assertLogs("app", level="WARNING") as captured:
            response = self.client.post(
                "/api/v1/shortcut/diagnostics",
                headers={"Authorization": "Bearer api-secret"},
                json={
                    "input_type": "Media",
                    "detected_links": ["https://www.instagram.com/reel/test/"],
                    "shortcut_input": "sample share text",
                    "token": "must-not-appear",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content_type"], "application/json")
        self.assertGreater(response.json()["body_bytes"], 0)
        self.assertEqual(len(response.json()["body_sha256"]), 64)
        logs = "\n".join(captured.output)
        self.assertIn("Shortcut diagnostic", logs)
        self.assertIn("detected_links", logs)
        self.assertIn("https://www.instagram.com/reel/test/", logs)
        self.assertNotIn("must-not-appear", logs)
        self.service.ingest.assert_not_called()

    def test_shortcut_diagnostic_requires_bearer_token(self) -> None:
        response = self.client.post(
            "/api/v1/shortcut/diagnostics",
            content=b"raw shortcut input",
        )

        self.assertEqual(response.status_code, 401)
        self.service.ingest.assert_not_called()

    def test_shortcut_diagnostic_rejects_oversized_body(self) -> None:
        response = self.client.post(
            "/api/v1/shortcut/diagnostics",
            headers={"Authorization": "Bearer api-secret"},
            content=b"x" * 2_000_001,
        )

        self.assertEqual(response.status_code, 413)
        self.service.ingest.assert_not_called()

    def test_validation_failure_logs_shape_without_authorization(self) -> None:
        with self.assertLogs("app", level="WARNING") as captured:
            response = self.client.post(
                "/api/v1/ingests",
                headers={"Authorization": "Bearer do-not-log-this"},
                json={"source_url": {"unexpected": "object"}},
            )

        self.assertEqual(response.status_code, 422)
        logs = "\n".join(captured.output)
        self.assertIn("source_url_type': 'dict'", logs)
        self.assertIn("body_keys': ['source_url']", logs)
        self.assertIn("source_url_shape", logs)
        self.assertNotIn("do-not-log-this", logs)

    def test_request_observability_adds_request_id(self) -> None:
        response = self.client.get("/healthz")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["x-request-id"])

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
