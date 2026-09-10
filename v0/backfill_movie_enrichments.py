"""Resolve and persist outbound movie links for existing entries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from movie_enrichment import WikidataMovieProvider, enrich_movie_entries
from store import init_db


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument(
        "--retry",
        action="store_true",
        help="Recheck entries that already have a saved enrichment result.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    init_db(args.db_path)
    summary = enrich_movie_entries(
        args.db_path,
        WikidataMovieProvider(),
        retry=args.retry,
    )
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
