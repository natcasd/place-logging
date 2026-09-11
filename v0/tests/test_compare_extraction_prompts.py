from __future__ import annotations

import unittest

import compare_extraction_prompts as comparison
import pipeline


class ExtractionPromptComparisonTests(unittest.TestCase):
    def test_baseline_prompt_restores_old_type_and_timing_rules(self) -> None:
        baseline = comparison.baseline_prompt_template()
        self.assertIn("Restaurant, Café, Bar, Bakery", baseline)
        self.assertIn("ONLY for a Concert, Pop-up, or Exhibit", baseline)
        self.assertNotIn("- Cocktail Bar:", baseline)
        self.assertIn("- Cocktail Bar:", pipeline.EXTRACTOR_PROMPT)

    def test_baseline_schema_restores_old_enum(self) -> None:
        enum = comparison.baseline_schema()["properties"]["entries"]["items"][
            "properties"
        ]["type_name"]["enum"]
        self.assertEqual(enum, list(comparison.BASELINE_ENTRY_TYPES))

    def test_output_comparison_reports_count_name_and_field_changes(self) -> None:
        old = {
            "entries": [
                {"extracted_name": "Bar Antonio", "type_name": "Bar"},
                {"extracted_name": "Only Before", "type_name": "Store"},
            ]
        }
        new = {
            "entries": [
                {"extracted_name": "Bar Antonio", "type_name": "Restaurant"},
                {"extracted_name": "Only After", "type_name": "Misc. Store"},
            ]
        }
        result = comparison.compare_outputs(old, new)
        self.assertEqual(result["baseline_entry_count"], 2)
        self.assertEqual(result["catalog_entry_count"], 2)
        self.assertEqual(result["baseline_only_names"], ["Only Before"])
        self.assertEqual(result["catalog_only_names"], ["Only After"])
        self.assertEqual(
            result["field_changes"][0]["changes"]["type_name"],
            {"baseline": "Bar", "catalog": "Restaurant"},
        )


if __name__ == "__main__":
    unittest.main()
