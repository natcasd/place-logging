from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

import gemini_contract_baseline as baseline
import pipeline


class GeminiContractBaselineTests(unittest.TestCase):
    def test_checked_in_reference_is_sanitized_and_matches_case_ids(self) -> None:
        cases = json.loads(baseline.DEFAULT_CASES.read_text(encoding="utf-8"))
        reference_path = baseline.DEFAULT_CASES.with_name("gemini_contract_baseline_reference_20260915.json")
        reference = json.loads(reference_path.read_text(encoding="utf-8"))
        self.assertTrue({case["id"] for case in reference["cases"]} <= {case["id"] for case in cases})
        self.assertEqual(reference["summary"]["selected"], len(reference["cases"]))
        self.assertEqual(reference["summary"]["passed"], 11)
        self.assertTrue(all("source_content" not in case for case in reference["cases"]))
        self.assertTrue(all("description" not in case for case in reference["cases"]))

    def test_cases_are_labeled_and_cover_each_gemini_contract(self) -> None:
        cases = json.loads(baseline.DEFAULT_CASES.read_text(encoding="utf-8"))
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        self.assertEqual({case["suite"] for case in cases}, {"text", "media", "tiebreaker"})
        self.assertTrue(
            all("expected_decision" in case if case["suite"] == "tiebreaker" else "expected_entries" in case for case in cases)
        )

    def test_score_catches_type_disagreement_and_extra_entry(self) -> None:
        result = baseline.score_entries(
            [
                {"extracted_name": "The Campbell", "type_name": "Restaurant", "location_query": "The Campbell"},
                {"extracted_name": "Supplier", "type_name": "Misc. Store"},
            ],
            [{"name": "The Campbell", "type": "Cocktail Bar", "has_location_query": True}],
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["wrong_type"][0]["actual"], "Restaurant")
        self.assertEqual(result["unexpected"][0]["name"], "Supplier")

    def test_score_catches_business_hours_as_timing(self) -> None:
        result = baseline.score_entries(
            [{"extracted_name": "The Audley Public House", "type_name": "Pub", "recurrence_text": "Thursday to Sunday"}],
            [{"name": "The Audley Public House", "type": "Pub", "absent_fields": ["recurrence_text"]}],
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["wrong_fields"][0]["field"], "recurrence_text")

    def test_usage_metadata_normalizes_both_gemini_apis(self) -> None:
        generated = pipeline._gemini_usage_counts(
            SimpleNamespace(
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=3,
                    thoughts_token_count=2,
                    total_token_count=15,
                )
            )
        )
        interaction = pipeline._gemini_usage_counts(
            SimpleNamespace(
                usage={"total_input_tokens": 20, "total_output_tokens": 4, "total_tokens": 24}
            )
        )
        self.assertEqual(generated["input_tokens"], 10)
        self.assertEqual(generated["thought_tokens"], 2)
        self.assertEqual(interaction["input_tokens"], 20)
        self.assertEqual(interaction["output_tokens"], 4)

    def test_successful_call_reports_usage_once_and_unknown_is_not_zero(self) -> None:
        recorded = []
        response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=10, total_token_count=12))
        self.assertIs(
            pipeline._call_gemini_with_retry(
                lambda: response,
                "test",
                model_name="sample-model",
                usage_sink=recorded.append,
            ),
            response,
        )
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["input_tokens"], 10)
        self.assertIsNone(recorded[0]["output_tokens"])
        self.assertEqual(recorded[0]["model"], "sample-model")

    def test_usage_sink_failure_does_not_repeat_successful_gemini_call(self) -> None:
        attempts = []

        def operation():
            attempts.append(1)
            return SimpleNamespace(text="ok")

        def failed_sink(_usage):
            raise RuntimeError("sink unavailable")

        self.assertEqual(
            pipeline._call_gemini_with_retry(operation, "test", usage_sink=failed_sink).text,
            "ok",
        )
        self.assertEqual(len(attempts), 1)

    def test_tiebreaker_score_requires_correct_pick_and_confidence(self) -> None:
        score = baseline.score_tiebreaker(
            {"pick": 0, "confidence": "medium"},
            {"pick": 0, "confidence": "high"},
        )
        self.assertFalse(score["passed"])
        self.assertEqual(score["wrong_fields"][0]["field"], "confidence")

    def test_extraction_sends_evidence_not_storage_or_duplicate_metadata(self) -> None:
        prompt = pipeline._extraction_prompt(
            {
                "source_platform": "instagram",
                "caption_or_description": "A recommended café",
                "uploader": "Le Chêne",
                "creator_display_name": "Le Chêne",
                "source_account_handle": "lechenenyc",
                "native_location": {"name": "Le Chêne"},
                "native_location_tag": "Le Chêne",
                "webpage_url": "https://www.instagram.com/reel/example/",
                "media_preserved": False,
            }
        )
        self.assertIn('"caption_or_description": "A recommended café"', prompt)
        self.assertIn('"creator_display_name": "Le Chêne"', prompt)
        self.assertIn('"native_location": {"name": "Le Chêne"}', prompt)
        for field in ("uploader", "native_location_tag", "webpage_url", "media_preserved"):
            self.assertNotIn(f'"{field}":', prompt)

    def test_tiebreaker_omits_unrelated_extraction_fields(self) -> None:
        prompt = pipeline._tiebreaker_prompt(
            {
                "extracted_name": "Le Chêne",
                "type_name": "Restaurant",
                "description": "French dinner in the West Village",
                "location_hints": {"city": "New York"},
                "extraction_confidence": "high",
                "timestamp_seconds": 12,
                "slide_index": 2,
                "dishes": ["bread"],
            },
            [{"displayName": {"text": "Le Chêne"}, "formattedAddress": "New York", "types": ["restaurant"]}],
        )
        self.assertIn("French dinner in the West Village", prompt)
        self.assertIn("New York", prompt)
        for field in ("extraction_confidence", "timestamp_seconds", "slide_index", "dishes"):
            self.assertNotIn(field, prompt)

    def test_confidence_is_retained_for_quality_and_future_review(self) -> None:
        item = pipeline.EXTRACTION_RESPONSE_SCHEMA["properties"]["entries"]["items"]
        self.assertIn("extraction_confidence", item["properties"])
        self.assertIn("extraction_confidence", item["required"])

    def test_merge_fills_transport_error_without_hiding_completed_defect(self) -> None:
        failed = {"id": "one", "usage": [], "error": "503"}
        defect = {
            "id": "two",
            "usage": [],
            "score": {"passed": False, "missing": ["Venue"], "unexpected": [], "wrong_type": [], "wrong_fields": []},
        }
        passing = {
            "id": "two",
            "usage": [],
            "score": {"passed": True, "missing": [], "unexpected": [], "wrong_type": [], "wrong_fields": []},
        }
        merged = baseline.merge_reports(
            [
                {"contract_sha256": "same", "cases": [failed, defect]},
                {"contract_sha256": "same", "cases": [{**passing, "id": "one"}, passing]},
            ]
        )
        by_id = {case["id"]: case for case in merged["cases"]}
        self.assertTrue(by_id["one"]["score"]["passed"])
        self.assertFalse(by_id["two"]["score"]["passed"])


if __name__ == "__main__":
    unittest.main()
