"""Translate fresh public pipeline output into the bounded storage contract.

Only this adapter invokes the existing media/model pipeline. It never reads the
saved library, so personal edits cannot become another user's starting result.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import Field

from capture_result import Name, Text, StrictModel, OriginalEvidence, OriginalResult, validate_result


class PlaceLookup(StrictModel):
    google_place_id: Name
    display_name: Name | None = None
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    formatted_address: Text | None = None
    google_maps_url: Text | None = None


@dataclass(frozen=True)
class ProcessedPost:
    original: OriginalResult
    locations: tuple[PlaceLookup, ...]


def adapt_public_result(value: dict) -> ProcessedPost:
    metadata = value.get('metadata') or {}
    if metadata.get('extraction_status') == 'failed':
        raise ValueError('Pipeline returned an unsuccessful extraction')
    if not isinstance(value.get('resolved_entries'), list):
        raise ValueError('Pipeline must explicitly return its completed outputs')
    if len(value['resolved_entries']) > 200:
        raise ValueError('Too many extracted outputs')
    mentions = []
    locations = {}

    def remember(place):
        if not isinstance(place, dict) or not place.get('id'):
            raise ValueError('Resolved place requires a provider ID')
        point = place.get('location') or {}
        lookup = PlaceLookup(google_place_id=place['id'], display_name=(place.get('displayName') or {}).get('text'),
                             lat=point.get('latitude'), lng=point.get('longitude'),
                             formatted_address=place.get('formattedAddress'), google_maps_url=place.get('googleMapsUri'))
        if (lookup.lat is None) != (lookup.lng is None):
            raise ValueError('Incomplete place coordinates')
        locations[lookup.google_place_id] = lookup
        return lookup.google_place_id

    for ordinal, output in enumerate(value['resolved_entries']):
        evidence = {k: v for k, v in output['extracted'].items() if k in OriginalEvidence.model_fields}
        for key in ('location_query', 'starts_at', 'ends_at', 'recurrence_text'):
            if evidence.get(key) in ('', None):
                evidence.pop(key, None)
        state = 'resolved' if output['status'] == 'auto' else output['status']
        mention = {'key': f'output-{ordinal:04d}', 'extracted': evidence, 'status': state}
        for key in ('location_query_used', 'resolution_code'):
            if output.get(key):
                mention[key] = output[key]
        if state == 'resolved':
            mention['place_id'] = remember(output['place'])
        elif state == 'needs_review':
            candidates = output.get('candidates') or []
            if len(candidates) > 10:
                raise ValueError('Too many place candidates')
            mention['candidate_ids'] = list(dict.fromkeys(remember(place) for place in candidates))
        mentions.append(mention)
    display = {k: metadata[k] for k in ('uploader', 'caption_or_description', 'media_count') if metadata.get(k) is not None}
    content = metadata.get('source_content')
    if content is not None:
        display['source_content'] = {'summary': content.get('summary')}
    native_location = metadata.get('native_location')
    if isinstance(native_location, dict):
        bounded_location = {
            key: native_location[key]
            for key in ('name', 'address', 'city', 'region', 'country')
            if isinstance(native_location.get(key), str)
            and native_location[key].strip()
        }
        if bounded_location:
            display['native_location'] = bounded_location
    return ProcessedPost(validate_result({'metadata': display, 'mentions': mentions}), tuple(locations.values()))


def process_public_post(url: str, directory: Path, progress: Callable[[str], None]) -> ProcessedPost:
    from pipeline import process_ingest
    return adapt_public_result(process_ingest(url, directory, progress=progress))
