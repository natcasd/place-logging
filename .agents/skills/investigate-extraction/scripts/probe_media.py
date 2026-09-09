#!/usr/bin/env python3
"""Ask one structured Gemini question about a complete production source."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from typing import Any


REMOTE_PROGRAM = r'''
import base64
import json
import mimetypes
import os
import shutil
import sys
from pathlib import Path

from google.genai import types
from pipeline import _call_gemini_with_retry, _client, fetch, source_platform

request = json.loads(base64.b64decode(sys.argv[1]).decode())
source_url = request["source_url"]
question = request["question"]
platform = source_platform(source_url)
if platform not in {"instagram", "youtube"}:
    raise SystemExit("only Instagram and YouTube sources are supported")
model = request.get("model") or os.environ.get(
    "GEMINI_INVESTIGATION_MODEL",
    os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
)
prompt = """Act as a forensic observer of the complete social-media source attached below.
Answer the investigator's narrow question using only observable evidence from the media and supplied URL context. Do not claim access to a previous model's hidden reasoning. Preserve the surrounding context needed to decide whether a named entity is independently recommended or is incidental, background text, a host venue, a geographic reference, or another supporting detail.

For each observation, identify the evidence modality, give a timestamp in seconds or a 1-based slide index when possible, include only the short wording needed to identify the evidence, explain its immediate context, and state confidence. Explicitly list uncertainties and useful follow-up questions.

Do not invent relationship labels such as takeover, residency, popup, storefront, or partnership unless the source states or visibly establishes them. Put deductions in the `inference` modality and describe the direct basis. Text supplied below as SOURCE_CAPTION is caption evidence, never visible-text evidence unless the same wording is independently visible in the attached media.

Investigator question:
""" + question

schema = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "observations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "modality": {
                        "type": "string",
                        "enum": ["speech", "visible_text", "caption", "visual", "metadata", "inference"],
                    },
                    "timestamp_seconds": {"type": "number"},
                    "slide_index": {"type": "integer"},
                    "observed_wording": {"type": "string"},
                    "context": {"type": "string"},
                    "interpretation": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["modality", "context", "interpretation", "confidence"],
            },
        },
        "uncertainties": {"type": "array", "items": {"type": "string"}},
        "recommended_followups": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "observations", "uncertainties", "recommended_followups"],
}

cleanup_dir = None
try:
    if platform == "youtube":
        response = _call_gemini_with_retry(
            lambda: _client().interactions.create(
                model=model,
                input=[
                    {"type": "text", "text": prompt},
                    {"type": "video", "uri": source_url},
                ],
                response_format=schema,
                store=False,
            ),
            "YouTube investigation probe",
        )
        output_text = response.output_text
    else:
        fetched = fetch(source_url, Path(os.environ.get("WORKDIR", "/tmp/place-logger-downloads")))
        cleanup_dir = fetched.cleanup_dir
        parts = [
            types.Part.from_bytes(
                data=path.read_bytes(),
                mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            )
            for path in fetched.media_paths
        ]
        caption = fetched.metadata.get("caption_or_description") or ""
        metadata_without_caption = {
            key: value
            for key, value in fetched.metadata.items()
            if key != "caption_or_description"
        }
        media_context = (
            "\n\nSOURCE_CAPTION (classify evidence from this block as caption):\n"
            + caption
            + "\n\nSOURCE_METADATA:\n"
            + json.dumps(metadata_without_caption, ensure_ascii=False, indent=2)
        )
        response = _call_gemini_with_retry(
            lambda: _client().models.generate_content(
                model=model,
                contents=[*parts, prompt + media_context],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            ),
            "Instagram investigation probe",
        )
        output_text = response.text
    parsed = json.loads(output_text)
    if platform == "instagram" and not any(
        media_type == "video" for media_type in fetched.metadata.get("media_types", [])
    ):
        for observation in parsed.get("observations", []):
            observation.pop("timestamp_seconds", None)
    print(json.dumps({
        "source_url": source_url,
        "source_platform": platform,
        "model": model,
        "question": question,
        "complete_source_analyzed": True,
        "result": parsed,
    }, ensure_ascii=False, indent=2))
finally:
    if cleanup_dir is not None:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--model")
    parser.add_argument("--app", default="place-logging")
    return parser


def remote_command(request: dict[str, Any]) -> str:
    encoded_program = base64.b64encode(REMOTE_PROGRAM.encode()).decode()
    bootstrap = (
        "import base64,sys;"
        f"exec(base64.b64decode('{encoded_program}').decode())"
    )
    request_json = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
    encoded_request = base64.b64encode(request_json.encode()).decode()
    return f'python -c "{bootstrap}" \'{encoded_request}\''


def main() -> int:
    args = build_parser().parse_args()
    request = {
        "source_url": args.source_url,
        "question": args.question,
        "model": args.model,
    }
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
