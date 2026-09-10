from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from movie_enrichment import WikidataMovieProvider, enrich_movie_entries
from store import init_db, list_entries, save_ingest


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict] = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return FakeResponse(self.payloads.pop(0))


def binding(
    entity_id: str,
    title: str,
    imdb_id: str,
    *,
    year: int | None = None,
    director: str | None = None,
) -> dict:
    result = {
        "item": {"value": f"http://www.wikidata.org/entity/{entity_id}"},
        "itemLabel": {"value": title},
        "imdb": {"value": imdb_id},
    }
    if year is not None:
        result["releaseDate"] = {"value": f"{year}-05-25T00:00:00Z"}
    if director is not None:
        result["directorLabel"] = {"value": director}
    return result


class WikidataMovieProviderTests(unittest.TestCase):
    def test_matches_title_year_and_director_to_an_imdb_letterboxd_link(self) -> None:
        session = FakeSession(
            [
                {
                    "search": [
                        {"id": "Q117663112", "label": "Perfect Days"},
                        {"id": "Q117792957", "label": "Perfect Days"},
                    ]
                },
                {
                    "results": {
                        "bindings": [
                            binding(
                                "Q117663112",
                                "Perfect Days",
                                "tt27503384",
                                year=2023,
                                director="Wim Wenders",
                            )
                        ]
                    }
                },
            ]
        )
        provider = WikidataMovieProvider(session=session, user_agent="Test/1.0")

        result = provider.lookup(
            "Perfect Days",
            "The 2023 film directed by Wim Wenders.",
        )

        self.assertEqual(result["match_status"], "matched")
        self.assertEqual(result["provider_id"], "Q117663112")
        self.assertEqual(result["release_year"], 2023)
        self.assertEqual(
            result["letterboxd_url"],
            "https://letterboxd.com/imdb/tt27503384/",
        )
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[0]["headers"]["User-Agent"], "Test/1.0")
        self.assertNotIn("Authorization", session.calls[0]["headers"])
        self.assertIn("wd:Q11424", session.calls[1]["params"]["query"])

    def test_accepts_an_exact_alias_returned_by_search(self) -> None:
        session = FakeSession(
            [
                {
                    "search": [
                        {
                            "id": "Q123",
                            "label": "Canonical title",
                            "match": {"text": "Alternate title"},
                        }
                    ]
                },
                {
                    "results": {
                        "bindings": [binding("Q123", "Canonical title", "tt1234567")]
                    }
                },
            ]
        )

        result = WikidataMovieProvider(session=session).lookup("Alternate title", "")

        self.assertEqual(result["match_status"], "matched")
        self.assertEqual(result["resolved_title"], "Canonical title")

    def test_returns_ambiguous_for_same_title_films_without_context(self) -> None:
        session = FakeSession(
            [
                {
                    "search": [
                        {"id": "Q1", "label": "The Bear"},
                        {"id": "Q2", "label": "The Bear"},
                    ]
                },
                {
                    "results": {
                        "bindings": [
                            binding("Q1", "The Bear", "tt0000001", year=1988),
                            binding("Q2", "The Bear", "tt0000002", year=2025),
                        ]
                    }
                },
            ]
        )

        result = WikidataMovieProvider(session=session).lookup("The Bear", "")

        self.assertEqual(result["match_status"], "ambiguous")
        self.assertIsNone(result["letterboxd_url"])

    def test_returns_not_found_when_no_search_result_is_a_film(self) -> None:
        session = FakeSession(
            [
                {"search": [{"id": "Q5", "label": "Éric Rohmer"}]},
                {"results": {"bindings": []}},
            ]
        )

        result = WikidataMovieProvider(session=session).lookup("Éric Rohmer", "")

        self.assertEqual(result["match_status"], "not_found")
        self.assertIsNone(result["letterboxd_url"])


class MovieEnrichmentPersistenceTests(unittest.TestCase):
    def test_enriches_only_movies_and_always_returns_a_google_link(self) -> None:
        class Provider:
            name = "test"
            calls: list[tuple[str, str]] = []

            def lookup(self, title: str, description: str) -> dict:
                self.calls.append((title, description))
                return {
                    "provider": self.name,
                    "provider_id": "Q117663112",
                    "resolved_title": "Perfect Days",
                    "release_year": 2023,
                    "letterboxd_url": "https://letterboxd.com/imdb/tt27503384/",
                    "match_status": "matched",
                    "match_confidence": 1.0,
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/movies/",
                    "metadata": {"source_platform": "instagram"},
                    "resolved_entries": [
                        {
                            "status": "not_applicable",
                            "extracted": {
                                "extracted_name": "Perfect Days",
                                "type_name": "Movie",
                                "description": "The 2023 film directed by Wim Wenders.",
                            },
                        },
                        {
                            "status": "not_applicable",
                            "extracted": {
                                "extracted_name": "The Creative Act",
                                "type_name": "Book",
                            },
                        },
                    ],
                },
            )
            provider = Provider()

            first = enrich_movie_entries(db_path, provider)
            second = enrich_movie_entries(db_path, provider)
            entries = list_entries(db_path)

            self.assertEqual(first, {"checked": 1, "matched": 1, "unmatched": 0, "errors": 0})
            self.assertEqual(second["checked"], 0)
            self.assertEqual(len(provider.calls), 1)
            movie = next(entry for entry in entries if entry["type"] == "Movie")
            book = next(entry for entry in entries if entry["type"] == "Book")
            self.assertEqual(movie["movie_enrichment"]["provider"], "test")
            self.assertEqual(
                movie["movie_enrichment"]["letterboxd_url"],
                "https://letterboxd.com/imdb/tt27503384/",
            )
            self.assertEqual(
                movie["movie_enrichment"]["web_search_url"],
                "https://www.google.com/search?q=Perfect+Days+2023+movie",
            )
            self.assertIsNone(book["movie_enrichment"])


if __name__ == "__main__":
    unittest.main()
