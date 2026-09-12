from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import Runtime, build_runtime, create_app


def canonical_result() -> dict:
    return {
        "ingest_id": 34,
        "item_id": 12,
        "source_url": "https://youtu.be/test",
        "metadata": {"source_platform": "youtube"},
        "places_extracted": [{"extracted_name": "Test Place"}],
        "resolved_places": [
            {
                "extracted": {"extracted_name": "Test Place"},
                "status": "unresolved",
                "reason": "test",
            }
        ],
        "saved_entries": [
            {
                "entry_id": 8,
                "name": "Test Place",
                "type": "Restaurant",
                "description": "A cozy neighborhood spot known for handmade pasta.",
                "location_id": None,
                "location_name": None,
                "latitude": None,
                "longitude": None,
                "resolution_status": "unresolved",
                "is_new": True,
                "source_count": 1,
            }
        ],
    }


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MagicMock()
        self.service.ingest.return_value = canonical_result()
        self.runtime = Runtime(
            service=self.service,
            ingest_api_token="api-secret",
        )
        self.client_context = TestClient(create_app(self.runtime))
        self.client = self.client_context.__enter__()

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)

    def test_health_check_needs_no_authentication(self) -> None:
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_runtime_initializes_without_retired_transport_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "DB_PATH": str(Path(temp_dir) / "places.db"),
                "WORKDIR": str(Path(temp_dir) / "downloads"),
                "INGEST_API_TOKEN": "api-secret",
            },
            clear=True,
        ):
            runtime = build_runtime()

        self.assertEqual(runtime.ingest_api_token, "api-secret")

    def test_openapi_contract_excludes_retired_ingest_fields(self) -> None:
        schema = self.client.get("/openapi.json").json()
        components = schema["components"]["schemas"]

        for model in ("IngestRequest", "ShortcutIngestRequest", "IngestResponse"):
            properties = components[model]["properties"]
            self.assertNotIn("user_prompt", properties)
            self.assertNotIn("delivery", properties)
            self.assertNotIn("delivery_status", properties)
        self.assertNotIn("/webhook", schema["paths"])

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

    def test_entries_returns_location_and_non_location_entries(self) -> None:
        self.service.entries.return_value = [
            {
                "id": 8,
                "item_id": 12,
                "ordinal": 0,
                "name": "The Creative Act",
                "google_place_id": None,
                "latitude": None,
                "longitude": None,
                "formatted_address": None,
                "google_maps_url": None,
                "location_name": "The Creative Act Bookstore",
                "dishes": [],
                "why_its_cool": "",
                "tags": [],
                "timestamp_seconds": 3.0,
                "slide_index": None,
                "resolution_status": "not_applicable",
                "type": "Book",
                "description": "A book about creativity.",
                "starts_at": None,
                "ends_at": None,
                "recurrence_text": None,
                "location_query": None,
                "source_url": "https://youtu.be/test",
                "saved_at": "2026-09-02 12:00:00",
                "sources": [
                    {
                        "id": 31,
                        "item_id": 12,
                        "ordinal": 0,
                        "name": "The Creative Act",
                        "type": "Book",
                        "source_url": "https://youtu.be/test",
                        "source_platform": "youtube",
                        "creator": "Reader",
                        "description": "A book about creativity.",
                        "dishes": [],
                        "why_its_cool": "",
                        "tags": [],
                        "timestamp_seconds": 3.0,
                        "slide_index": None,
                        "resolution_status": "not_applicable",
                        "location_query": None,
                        "saved_at": "2026-09-02 12:00:00",
                    }
                ],
            }
        ]

        response = self.client.get(
            "/api/v1/entries",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entries"][0]["type"], "Book")
        self.assertIsNone(response.json()["entries"][0]["latitude"])
        self.assertEqual(
            response.json()["entries"][0]["location_name"],
            "The Creative Act Bookstore",
        )
        self.assertEqual(response.json()["entries"][0]["sources"][0]["creator"], "Reader")

    def test_sources_includes_sources_needing_review(self) -> None:
        self.service.sources.return_value = [
            {
                "id": 12,
                "source_url": "https://youtu.be/test",
                "source_platform": "youtube",
                "creator": None,
                "caption": None,
                "media_count": 0,
                "media_preserved": False,
                "entry_count": 0,
                "needs_review": True,
                "saved_at": "2026-09-02 12:00:00",
            }
        ]

        response = self.client.get(
            "/api/v1/sources",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sources"][0]["needs_review"])

    def test_activity_returns_processing_history_and_results(self) -> None:
        self.service.activity.return_value = [
            {
                "id": 34,
                "item_id": 12,
                "source_url": "https://youtu.be/test",
                "source_platform": "youtube",
                "status": "completed",
                "stage": "completed",
                "started_at": "2026-09-03 12:00:00",
                "updated_at": "2026-09-03 12:01:00",
                "completed_at": "2026-09-03 12:01:00",
                "results": canonical_result()["saved_entries"],
                "events": [
                    {
                        "id": 1,
                        "stage": "completed",
                        "status": "completed",
                        "message": "Saved 1 entry",
                        "created_at": "2026-09-03 12:01:00",
                    }
                ],
            }
        ]

        response = self.client.get(
            "/api/v1/activity?limit=25",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["activity"][0]["results"][0]["name"], "Test Place")
        self.assertEqual(
            response.json()["activity"][0]["results"][0]["description"],
            "A cozy neighborhood spot known for handmade pasta.",
        )
        self.service.activity.assert_called_once_with(25)

    def test_confirms_activity_location_candidate(self) -> None:
        confirmed = canonical_result()["saved_entries"][0] | {
            "source_connection_id": 21,
            "ordinal": 0,
            "location_id": 5,
            "location_name": "Test Place",
            "latitude": 40.7,
            "longitude": -73.9,
            "formatted_address": "123 Test St",
            "resolution_status": "user_confirmed",
            "review_candidates": [],
        }
        self.service.confirm_activity_location.return_value = confirmed

        response = self.client.post(
            "/api/v1/activity/34/entries/8/location",
            headers={"Authorization": "Bearer api-secret"},
            json={"candidate_id": "places/test"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entry"]["location_name"], "Test Place")
        self.service.confirm_activity_location.assert_called_once_with(
            34,
            8,
            "places/test",
        )

    def test_rejects_unavailable_activity_location_candidate(self) -> None:
        self.service.confirm_activity_location.side_effect = ValueError(
            "Select one of the available location candidates"
        )

        response = self.client.post(
            "/api/v1/activity/34/entries/8/location",
            headers={"Authorization": "Bearer api-secret"},
            json={"candidate_id": "places/not-offered"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {"detail": "Select one of the available location candidates"},
        )

    def test_places_rejects_excessive_limit(self) -> None:
        response = self.client.get(
            "/api/v1/places?limit=501",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 422)
        self.service.places.assert_not_called()

    def test_entries_accepts_temporary_thousand_item_limit(self) -> None:
        self.service.entries.return_value = []

        response = self.client.get(
            "/api/v1/entries?limit=1000",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.service.entries.assert_called_once_with(1000)

    def test_delete_place_requires_bearer_token(self) -> None:
        response = self.client.delete("/api/v1/places/7")

        self.assertEqual(response.status_code, 401)
        self.service.delete_place.assert_not_called()

    def test_delete_place_returns_deleted_counts(self) -> None:
        self.service.delete_place.return_value = {
            "deleted_places": 2,
            "deleted_items": 1,
        }

        response = self.client.delete(
            "/api/v1/places/7",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"place_id": 7, "deleted_places": 2, "deleted_items": 1},
        )
        self.service.delete_place.assert_called_once_with(7)

    def test_delete_place_returns_not_found(self) -> None:
        self.service.delete_place.return_value = None

        response = self.client.delete(
            "/api/v1/places/999",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Saved place not found"})

    def test_delete_entry_uses_compatible_store_operation(self) -> None:
        self.service.delete_entry.return_value = {
            "deleted_entries": 1,
            "deleted_sources": 0,
        }

        response = self.client.delete(
            "/api/v1/entries/8",
            headers={"Authorization": "Bearer api-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_entries"], 1)
        self.service.delete_entry.assert_called_once_with(8)

    def test_delete_entry_card_deletes_exact_references(self) -> None:
        self.service.delete_entries.return_value = {
            "deleted_entries": 3,
            "deleted_sources": 0,
        }

        response = self.client.request(
            "DELETE",
            "/api/v1/entries",
            headers={"Authorization": "Bearer api-secret"},
            json={"entry_ids": [8, 9, 10]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["entry_ids"], [8, 9, 10])
        self.assertEqual(response.json()["deleted_entries"], 3)
        self.service.delete_entries.assert_called_once_with([8, 9, 10])

    def test_delete_entry_card_rejects_missing_reference_without_partial_delete(self) -> None:
        self.service.delete_entries.return_value = None

        response = self.client.request(
            "DELETE",
            "/api/v1/entries",
            headers={"Authorization": "Bearer api-secret"},
            json={"entry_ids": [8, 999]},
        )

        self.assertEqual(response.status_code, 404)
        self.service.delete_entries.assert_called_once_with([8, 999])

    def test_delete_entry_card_requires_authentication(self) -> None:
        response = self.client.request(
            "DELETE",
            "/api/v1/entries",
            json={"entry_ids": [8]},
        )

        self.assertEqual(response.status_code, 401)
        self.service.delete_entries.assert_not_called()

    def test_ingest_calls_shared_service_and_returns_result(self) -> None:
        response = self.client.post(
            "/api/v1/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={"source_url": "https://youtu.be/test"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item_id"], 12)
        self.assertNotIn("delivery_status", response.json())
        self.assertNotIn("user_prompt", response.json())
        self.service.ingest.assert_called_once_with("https://youtu.be/test")

    def test_ingest_rejects_removed_prompt_and_delivery_fields(self) -> None:
        for field, value in (
            ("delivery", "response_only"),
            ("user_prompt", "Focus on Brooklyn"),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/v1/ingests",
                    headers={"Authorization": "Bearer api-secret"},
                    json={"source_url": "https://youtu.be/test", field: value},
                )
                self.assertEqual(response.status_code, 422)
        self.service.ingest.assert_not_called()

    def test_shortcut_adapter_decodes_url_into_shared_ingest_flow(self) -> None:
        source_url = "https://www.instagram.com/reel/test/"
        encoded_url = base64.b64encode(source_url.encode()).decode()

        response = self.client.post(
            "/api/v1/shortcut/ingests",
            headers={"Authorization": "Bearer api-secret"},
            json={"source_url_base64": encoded_url},
        )

        self.assertEqual(response.status_code, 200)
        self.service.ingest.assert_called_once_with(source_url)

    def test_shortcut_adapter_rejects_removed_prompt_and_delivery_fields(self) -> None:
        encoded_url = base64.b64encode(b"https://youtu.be/test").decode()
        for field, value in (
            ("delivery", "response_only"),
            ("user_prompt", "Focus on Brooklyn"),
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/v1/shortcut/ingests",
                    headers={"Authorization": "Bearer api-secret"},
                    json={"source_url_base64": encoded_url, field: value},
                )
                self.assertEqual(response.status_code, 422)
        self.service.ingest.assert_not_called()

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

    def test_obsolete_webhook_is_not_registered(self) -> None:
        response = self.client.post("/webhook", json={"update_id": 1})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
