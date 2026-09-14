from __future__ import annotations

import unittest
from unittest.mock import MagicMock

import requests

from retry_policy import classify_failure, retry_delay_seconds


class RetryPolicyTests(unittest.TestCase):
    def test_honors_numeric_retry_after_header(self) -> None:
        response = MagicMock()
        response.status_code = 429
        response.headers = {"Retry-After": "17"}
        error = requests.HTTPError("rate limited", response=response)

        decision = classify_failure(
            error,
            stage="extracting",
            platform="gemini",
        )

        self.assertTrue(decision.retryable)
        self.assertEqual(decision.retry_after_seconds, 17)
        self.assertEqual(
            retry_delay_seconds(
                error,
                attempt=1,
                base_seconds=3,
                maximum_seconds=60,
            ),
            17,
        )

    def test_private_media_is_permanent_but_still_has_a_clear_kind(self) -> None:
        decision = classify_failure(
            RuntimeError("This TikTok is a private post; login required"),
            stage="fetching",
            platform="tiktok",
        )

        self.assertFalse(decision.retryable)
        self.assertEqual(decision.failure_kind, "media_fetch_failed")
        self.assertIn("private", decision.user_message.lower())


if __name__ == "__main__":
    unittest.main()
