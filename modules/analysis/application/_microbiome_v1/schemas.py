"""Document-owned v1 leaf algorithms frozen from efca880 Report schemas.

Only the native-source dependency closure is retained. Import bindings point to
this fixed package; future Report changes must not alter this implementation.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field


def _to_camel(value: str) -> str:
    head, *tail = value.split('_')
    return head + ''.join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra='forbid',
    )


class SourceMetric(ApiModel):
    code: str
    name: str
    value: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    status: str | None = None


TaxonCategory = Literal[
    'beneficial',
    'pathogenic',
    'opportunistic',
    'fungi_pathogenic',
    'other',
]


class ParsedTaxon(ApiModel):
    taxon_id: str
    category: TaxonCategory
    latin_name: str
    chinese_name: str | None = None
    value: str | None = None
    unit: str | None = '%'
    reference_range: str | None = None
    source_status: str | None = None
    explicitly_abnormal: bool = False
    detection_status: Literal['detected', 'trace', 'not_detected', 'unknown'] = 'unknown'


class ParsedDisease(ApiModel):
    disease_id: str
    name: str
    source_score: str | None = None
    source_level: str | None = None
    source_description: str | None = None
    related_taxa: list[str] = Field(default_factory=list)


def stable_id(prefix: str, value: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', value.casefold()).strip('-')
    if not slug:
        slug = value.encode('utf-8').hex()[:32]
    # Keep IDs readable while guaranteeing that long queries with the same
    # prefix (for example colorectal cancer and colorectal adenoma searches)
    # remain distinct in the evidence audit.
    digest = hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]
    return f'{prefix}-{slug[:64]}-{digest}'
