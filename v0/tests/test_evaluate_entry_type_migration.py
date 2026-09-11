from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import evaluate_entry_type_migration as evaluation
from store import init_db, save_ingest


class EntryTypeMigrationEvaluationTests(unittest.TestCase):
    def test_saved_sample_is_stratified_and_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for ordinal, entry_type in enumerate(("Movie", "Unknown", "Movie"), start=1):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://example.com/{ordinal}",
                        "metadata": {},
                        "resolved_entries": [{
                            "status": "not_applicable",
                            "extracted": {
                                "extracted_name": f"Entry {ordinal}",
                                "type_name": entry_type,
                                "description": f"Description {ordinal}",
                            },
                        }],
                    },
                )
            con = sqlite3.connect(db_path)
            before = con.total_changes
            cases = evaluation.saved_sample(con, ("Movie", "Unknown"), per_type=1)
            self.assertEqual(con.total_changes, before)
            con.close()

            self.assertEqual(len(cases), 2)
            self.assertEqual({case["current_type"] for case in cases}, {"Movie", "Unknown"})

    def test_report_scores_fixtures_and_marks_saved_changes(self) -> None:
        saved = [{"case_id": "saved:1", "current_type": "Bar", "name": "Bar"}]
        fixtures = [{"case_id": "fixture:1", "expected_type": "Pub", "name": "Pub"}]

        def classifier(cases):
            return [
                {
                    "case_id": case["case_id"],
                    "type_name": "Cocktail Bar" if case["case_id"] == "saved:1" else "Pub",
                    "reason": "test",
                }
                for case in cases
            ]

        report = evaluation.build_report(saved, fixtures, classifier=classifier)

        self.assertTrue(report["read_only"])
        self.assertEqual(report["saved_sample"]["changed_count"], 1)
        self.assertEqual(report["fixtures"]["accuracy"], 1.0)

    def test_fixture_expected_types_all_exist_in_catalog(self) -> None:
        self.assertGreaterEqual(len(evaluation.fixture_cases()), 30)


if __name__ == "__main__":
    unittest.main()
