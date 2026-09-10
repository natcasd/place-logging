"""Persisted, credential-free movie identity enrichment via Wikidata."""
from __future__ import annotations

import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import requests

from store import movie_entries_for_enrichment, save_movie_enrichment


log = logging.getLogger(__name__)
WIKIDATA_API_ROOT = "https://www.wikidata.org/w/api.php"
WIKIDATA_QUERY_ROOT = "https://query.wikidata.org/sparql"
DEFAULT_USER_AGENT = "PlaceLogger/1.0 (https://github.com/natcasd/place-logging)"


class MovieProvider(Protocol):
    name: str

    def lookup(self, title: str, description: str) -> dict[str, Any]: ...


def _normalized(value: Any) -> str:
    text = "".join(
        character
        for character in unicodedata.normalize("NFKD", str(value or "").casefold())
        if not unicodedata.combining(character)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _year_hint(description: str) -> int | None:
    maximum = datetime.now(timezone.utc).year + 2
    for value in re.findall(r"\b(?:18|19|20)\d{2}\b", description):
        year = int(value)
        if 1888 <= year <= maximum:
            return year
    return None


_DIRECTOR_PATTERNS = (
    re.compile(
        r"\bdirected by\s+(.+?)(?=,|\.|\b(?:that|who|featuring|starring|about)\b|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bfilm by\s+(.+?)(?=,|\.|\b(?:that|who|featuring|starring|about)\b|$)",
        re.IGNORECASE,
    ),
)


def _director_hint(description: str) -> str | None:
    for pattern in _DIRECTOR_PATTERNS:
        if match := pattern.search(description):
            value = " ".join(match.group(1).split()).strip(" -")
            return value or None
    return None


def _binding_value(binding: dict[str, Any], key: str) -> str | None:
    value = (binding.get(key) or {}).get("value")
    return value if isinstance(value, str) and value else None


class WikidataMovieProvider:
    """Resolve conservative movie matches and their IMDb identifiers."""

    name = "wikidata"

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 10,
        user_agent: str | None = None,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = timeout
        self.user_agent = (
            user_agent
            or os.environ.get("WIKIDATA_USER_AGENT")
            or DEFAULT_USER_AGENT
        ).strip()

    def _get(self, **params: Any) -> dict[str, Any]:
        response = self.session.get(
            WIKIDATA_API_ROOT,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            params={"format": "json", "formatversion": 2, **params},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Wikidata returned a non-object response")
        return payload

    def _movie_candidates(self, entity_ids: list[str]) -> list[dict[str, Any]]:
        if not entity_ids:
            return []
        safe_ids = [entity_id for entity_id in entity_ids if re.fullmatch(r"Q\d+", entity_id)]
        if not safe_ids:
            return []
        values = " ".join(f"wd:{entity_id}" for entity_id in safe_ids)
        query = f"""
SELECT ?item ?itemLabel ?releaseDate ?imdb ?directorLabel WHERE {{
  VALUES ?item {{ {values} }}
  ?item wdt:P31/wdt:P279* wd:Q11424;
        wdt:P345 ?imdb.
  OPTIONAL {{ ?item wdt:P577 ?releaseDate. }}
  OPTIONAL {{ ?item wdt:P57 ?director. }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
""".strip()
        response = self.session.get(
            WIKIDATA_QUERY_ROOT,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            params={"query": query, "format": "json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        bindings = (payload.get("results") or {}).get("bindings", [])
        by_id: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            item_url = _binding_value(binding, "item") or ""
            entity_id = item_url.rsplit("/", 1)[-1]
            if entity_id not in safe_ids:
                continue
            candidate = by_id.setdefault(
                entity_id,
                {
                    "id": entity_id,
                    "title": _binding_value(binding, "itemLabel") or entity_id,
                    "imdb_id": _binding_value(binding, "imdb"),
                    "release_years": [],
                    "directors": [],
                },
            )
            release_date = _binding_value(binding, "releaseDate") or ""
            if match := re.match(r"^[+-]?(\d{4,})-", release_date):
                year = int(match.group(1))
                if year not in candidate["release_years"]:
                    candidate["release_years"].append(year)
            director = _binding_value(binding, "directorLabel")
            if director and director not in candidate["directors"]:
                candidate["directors"].append(director)
        return [by_id[entity_id] for entity_id in safe_ids if entity_id in by_id]

    def lookup(self, title: str, description: str) -> dict[str, Any]:
        year_hint = _year_hint(description)
        director_hint = _director_hint(description)
        search = self._get(
            action="wbsearchentities",
            search=title,
            language="en",
            uselang="en",
            type="item",
            limit=10,
        )
        candidate_ids = [
            result["id"]
            for result in search.get("search", [])
            if isinstance(result, dict)
            and isinstance(result.get("id"), str)
            and (
                _normalized(result.get("label")) == _normalized(title)
                or _normalized((result.get("match") or {}).get("text"))
                == _normalized(title)
            )
        ]
        candidates = self._movie_candidates(candidate_ids)
        if year_hint is not None:
            candidates = [
                candidate
                for candidate in candidates
                if year_hint in candidate["release_years"]
            ]
        if not candidates:
            return self._unmatched("not_found", release_year=year_hint)

        scored: list[tuple[float, dict[str, Any]]] = []
        normalized_director = _normalized(director_hint)
        for candidate in candidates:
            score = 0.70
            if year_hint is not None:
                score += 0.20
            if director_hint:
                matching_director = any(
                    normalized_director in _normalized(label)
                    or _normalized(label) in normalized_director
                    for label in candidate["directors"]
                )
                if not matching_director:
                    continue
                score += 0.10
            elif len(candidates) == 1:
                score += 0.10
            scored.append((score, candidate))

        if not scored:
            return self._unmatched("not_found", release_year=year_hint)
        scored.sort(key=lambda pair: pair[0], reverse=True)
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return self._unmatched("ambiguous", release_year=year_hint)

        score, match = scored[0]
        imdb_id = match["imdb_id"]
        return {
            "provider": self.name,
            "provider_id": match["id"],
            "resolved_title": match["title"] or title,
            "release_year": year_hint
            or (min(match["release_years"]) if match["release_years"] else None),
            "letterboxd_url": f"https://letterboxd.com/imdb/{imdb_id}/",
            "match_status": "matched",
            "match_confidence": round(min(score, 1.0), 2),
        }

    def _unmatched(
        self,
        status: str,
        *,
        release_year: int | None = None,
    ) -> dict[str, Any]:
        return {
            "provider": self.name,
            "provider_id": None,
            "resolved_title": None,
            "release_year": release_year,
            "letterboxd_url": None,
            "match_status": status,
            "match_confidence": None,
        }


def enrich_movie_entries(
    db_path: Path,
    provider: MovieProvider,
    entry_ids: list[int] | None = None,
    *,
    retry: bool = False,
) -> dict[str, int]:
    """Resolve and persist movie identities; a lookup failure never aborts ingest."""
    summary = {"checked": 0, "matched": 0, "unmatched": 0, "errors": 0}
    for entry in movie_entries_for_enrichment(db_path, entry_ids, retry=retry):
        summary["checked"] += 1
        try:
            result = provider.lookup(entry["name"], entry.get("descriptions") or "")
        except Exception:
            log.exception("Movie enrichment failed for entry_id=%s", entry["id"])
            result = {
                "provider": provider.name,
                "match_status": "error",
                "match_confidence": None,
            }
            summary["errors"] += 1
        else:
            if result.get("match_status") == "matched":
                summary["matched"] += 1
            else:
                summary["unmatched"] += 1
        save_movie_enrichment(db_path, entry["id"], result)
    return summary
