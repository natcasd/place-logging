from __future__ import annotations

import json
import subprocess
import sys
import unittest

import entry_types


EXPECTED_TYPES = (
    "Restaurant",
    "Café",
    "Cocktail Bar",
    "Club",
    "Wine Bar",
    "Pub",
    "Bakery",
    "Museum",
    "Art Gallery",
    "Furniture Store",
    "Clothes Store",
    "Health and Beauty Store",
    "Misc. Store",
    "Spa",
    "Park",
    "Movie Theater",
    "Bike Route",
    "Hiking Trail",
    "Fitness",
    "Concert",
    "Pop-up",
    "Exhibit",
    "Book",
    "Movie",
    "Article",
    "Song",
    "Product",
    "Unknown",
)


class EntryTypeCatalogTests(unittest.TestCase):
    def test_catalog_has_the_approved_order_and_no_generic_bar_or_store(self) -> None:
        self.assertEqual(entry_types.ENTRY_TYPES, EXPECTED_TYPES)
        self.assertNotIn("Bar", entry_types.ENTRY_TYPES)
        self.assertNotIn("Store", entry_types.ENTRY_TYPES)

    def test_every_type_has_one_icon_and_a_classifier_description(self) -> None:
        for definition in entry_types.ENTRY_TYPE_DEFINITIONS:
            with self.subTest(entry_type=definition.name):
                self.assertTrue(definition.description)
                self.assertNotEqual(
                    bool(definition.icon_system),
                    bool(definition.icon_asset),
                )

    def test_only_movie_declares_the_movie_enricher(self) -> None:
        self.assertEqual(entry_types.entry_types_for_enricher("movie"), ("Movie",))
        self.assertEqual(entry_types.entry_type_enricher("Movie"), "movie")
        self.assertIsNone(entry_types.entry_type_enricher("Movie Theater"))

    def test_unknown_can_explicitly_have_a_location(self) -> None:
        unknown = entry_types.entry_type_definition("Unknown")
        self.assertIn("may still have a location", unknown.description)

    def test_checked_in_ios_catalog_is_current(self) -> None:
        result = subprocess.run(
            [sys.executable, "generate_ios_entry_types.py", "--check"],
            cwd=entry_types.CATALOG_PATH.parent,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_catalog_json_is_directly_readable(self) -> None:
        catalog = json.loads(entry_types.CATALOG_PATH.read_text(encoding="utf-8"))
        self.assertEqual([entry["name"] for entry in catalog], list(EXPECTED_TYPES))


if __name__ == "__main__":
    unittest.main()
