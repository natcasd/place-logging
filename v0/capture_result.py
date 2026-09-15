"""Bounded original-result contract, shared by public and private processing.

Only structured recommendation evidence and place IDs belong in this envelope.
Media, provider response blobs, user IDs, and private capture context are not
accepted. The processing adapter supplies keys once, before publishing a result.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(max_length=10000)]
Name = Annotated[str, StringConstraints(min_length=1, max_length=512)]
Key = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_.:-]{1,128}$')]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)


class OriginalEvidence(StrictModel):
    extracted_name: Name
    type_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    description: Text = ''
    dishes: list[Name] = Field(default_factory=list, max_length=100)
    why_its_cool: Text = ''
    tags: list[Name] = Field(default_factory=list, max_length=100)
    timestamp_seconds: float | None = Field(default=None, ge=0)
    slide_index: int | None = Field(default=None, ge=1)
    location_query: Name | None = None
    starts_at: Name | None = None
    ends_at: Name | None = None
    recurrence_text: Name | None = None


class OriginalMention(StrictModel):
    key: Key
    extracted: OriginalEvidence
    status: Literal['resolved', 'needs_review', 'unresolved', 'not_applicable']
    place_id: Name | None = None
    candidate_ids: list[Name] = Field(default_factory=list, max_length=10)

    @model_validator(mode='after')
    def resolution(self):
        if (self.status == 'resolved') != (self.place_id is not None):
            raise ValueError('Only resolved outputs must have a place ID')
        if self.candidate_ids and self.status != 'needs_review':
            raise ValueError('Only review outputs may have candidate IDs')
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError('Candidate IDs must be unique')
        return self


class SourceContent(StrictModel):
    summary: Text | None = None


class OriginalMetadata(StrictModel):
    uploader: Name | None = None
    caption_or_description: Text | None = None
    source_content: SourceContent | None = None
    media_count: int = Field(default=0, ge=0, le=1000)


class OriginalResult(StrictModel):
    metadata: OriginalMetadata = Field(default_factory=OriginalMetadata)
    mentions: list[OriginalMention] = Field(max_length=200)

    @model_validator(mode='after')
    def unique_keys(self):
        keys = [mention.key for mention in self.mentions]
        if len(set(keys)) != len(keys):
            raise ValueError('Output keys must be unique')
        return self


class DirectContext(StrictModel):
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    heading: float | None = Field(default=None, ge=0, lt=360)

    @model_validator(mode='after')
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError('Supply both latitude and longitude')
        return self


def validate_result(value: dict | str) -> OriginalResult:
    # Apply a bound before parsing persisted JSON or validating provider output.
    encoded = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
    if len(encoded.encode('utf-8')) > 1_000_000:
        raise ValueError('Original result exceeds 1 MB')
    return OriginalResult.model_validate_json(encoded)
