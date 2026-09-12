#!/usr/bin/env python3
"""Rerun the deployed extractor, optionally with a local candidate prompt."""

from __future__ import annotations

import argparse
import ast
import base64
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REMOTE_PROGRAM = r'''
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pipeline
from pipeline import (
    extract_bundle,
    extract_youtube_bundle,
    fetch,
    source_platform,
)
from store import list_entry_types

request = json.loads(base64.b64decode(sys.argv[1]).decode())
source_url = request["source_url"]
candidate_prompt = request.get("candidate_prompt")
if candidate_prompt is not None:
    pipeline.EXTRACTOR_PROMPT = candidate_prompt
platform = source_platform(source_url)
if platform not in {"instagram", "youtube"}:
    raise SystemExit("only Instagram and YouTube sources are supported")
existing_types = list_entry_types(Path(os.environ.get("DB_PATH", "/data/places.db")))
cleanup_dir = None
try:
    if platform == "youtube":
        bundle = extract_youtube_bundle(source_url, existing_types)
        metadata = {"source_platform": "youtube", "webpage_url": source_url}
        model = os.environ.get(
            "GEMINI_YOUTUBE_MODEL",
            os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        )
    else:
        fetched = fetch(
            source_url,
            Path(os.environ.get("WORKDIR", "/tmp/place-logger-downloads")),
        )
        cleanup_dir = fetched.cleanup_dir
        metadata = fetched.metadata
        bundle = extract_bundle(
            fetched.media_paths,
            metadata,
            existing_types,
        )
        model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    print(json.dumps({
        "source_url": source_url,
        "source_platform": platform,
        "model": model,
        "prompt_source": request.get("prompt_source", "deployed"),
        "prompt_sha256": hashlib.sha256(
            pipeline.EXTRACTOR_PROMPT.encode()
        ).hexdigest(),
        "complete_source_analyzed": True,
        "metadata": metadata,
        "current_extraction": bundle,
        "warning": "This is a fresh current-model result, not the historical model response.",
    }, ensure_ascii=False, indent=2))
finally:
    if cleanup_dir is not None:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument(
        "--candidate-pipeline",
        type=Path,
        help="load EXTRACTOR_PROMPT from this local pipeline.py and use it remotely",
    )
    parser.add_argument("--app", default="place-logging")
    return parser


def _string_expression(node: ast.AST, values: dict[str, str]) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in values:
        return values[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _string_expression(node.left, values) + _string_expression(
            node.right, values
        )
    raise ValueError("prompt expression contains unsupported Python syntax")


def load_extractor_prompt(path: Path) -> str:
    """Read string assignments without importing the candidate application."""
    tree = ast.parse(path.read_text(), filename=str(path))
    values: dict[str, str] = {}
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            values[target.id] = _string_expression(statement.value, values)
        except ValueError:
            continue
    try:
        return values["EXTRACTOR_PROMPT"]
    except KeyError as exc:
        raise ValueError(f"EXTRACTOR_PROMPT not found in {path}") from exc


def remote_command(request: dict[str, Any]) -> str:
    encoded_program = base64.b64encode(REMOTE_PROGRAM.encode()).decode()
    encoded_request = base64.b64encode(
        json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    bootstrap = (
        "import base64,sys;"
        f"exec(base64.b64decode('{encoded_program}').decode())"
    )
    return f'python -c "{bootstrap}" \'{encoded_request}\''


def main() -> int:
    args = build_parser().parse_args()
    request = {"source_url": args.source_url}
    if args.candidate_pipeline:
        try:
            candidate_prompt = load_extractor_prompt(args.candidate_pipeline)
        except (OSError, SyntaxError, ValueError) as exc:
            raise SystemExit(str(exc)) from exc
        request.update(
            {
                "candidate_prompt": candidate_prompt,
                "prompt_source": str(args.candidate_pipeline),
            }
        )
    result = subprocess.run(
        [
            "flyctl",
            "ssh",
            "console",
            "--app",
            args.app,
            "--command",
            remote_command(request),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return result.returncode
    print(result.stdout.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
