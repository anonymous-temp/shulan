"""Neutral clinical rows with attributable occurrences from fixed Report output."""

import re

from modules.analysis.complete_source import _require
from modules.analysis.snapshots import digest
from utils.report_source_observations import report_source_items

_MAGNITUDE = (
    r'(?:<=|>=|≤|≥|<|>)?\s*[+-]?'
    r'(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?'
)
_NUMBER = re.compile(_MAGNITUDE)
_SCALE_UNIT = r'10(?:\^[+-]?\d{1,2}|[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]{1,3})\s*/\s*[A-Za-zµμ][A-Za-z]{0,15}'
_SCALE_SUFFIX = rf'(?:[ \t]*[xX×*]\s*|[ \t]+){_SCALE_UNIT}'
_UNIT_TOKEN = r'[A-Za-zµμ][A-Za-zµμ0-9^().+·*\-]{0,15}'
_COMPOUND_UNIT = rf'(?:{_UNIT_TOKEN})?(?:[ \t]*/[ \t]*{_UNIT_TOKEN}){{1,3}}'
_SIMPLE_UNIT = r'[pnumcdkµμ]?(?:g|l|L|m|mol|U|IU)|fL|s|min|hr|mmHg|kPa|bpm|CFU|copies|cells|unit|units|分|次|岁|℃|[%％]'
_MEASURED_RESULT = re.compile(
    rf'(?P<magnitude>{_MAGNITUDE})(?P<unit>(?:{_SCALE_SUFFIX}|[ \t]*(?:{_COMPOUND_UNIT}|{_SIMPLE_UNIT})))'
)


def _inspection_value(row):
    """Preserve the fixed result while separating unambiguous units for Profile."""
    value, unit = row.get('testResultValue'), row.get('unit')
    if not isinstance(value, str) or _NUMBER.fullmatch(value.strip()):
        return value, unit
    text = value.strip()
    if isinstance(unit, str) and unit.strip():
        suffix = unit.strip()
        prefix = text.removesuffix(suffix).rstrip()
        if prefix != text and _NUMBER.fullmatch(prefix):
            return prefix, unit
        return value, unit
    if match := _MEASURED_RESULT.fullmatch(text):
        return match['magnitude'], match['unit'].strip()
    return value, unit


def report_occurrence_observations(projection, *, projection_ref, dependencies):
    by_document = {row['documentId']: row['contentSha256'] for row in dependencies}
    observations, seen = [], set()
    for pointer, item in report_source_items(projection):
        if item['sourceCategory'] != 'supplementaryHospitalData':
            continue
        row = projection['supplementaryHospitalData'][int(pointer.rsplit('/', 1)[1])]
        occurrence = row.get('sourceOccurrence')
        if occurrence is None:
            continue
        document, checksum = row.get('sourceDocumentId'), row.get('sourceChecksum')
        _require(
            isinstance(occurrence, str)
            and bool(occurrence.strip())
            and isinstance(document, str)
            and document in by_document
            and by_document[document] == checksum,
            'Report occurrence is not attributable to its fixed original',
        )
        _require(
            row.get('dataType') in {'EXAM', 'INSPECTION'}
            and isinstance(item.get('itemName'), str)
            and bool(item['itemName'].strip())
            and item.get('resultValue') is not None,
            'Report occurrence does not contain an original clinical result',
        )
        identity = 'report_occurrence_' + digest([document, checksum, occurrence])
        _require(identity not in seen, 'duplicate Report source occurrence')
        seen.add(identity)
        evidence = {
            'artifactRef': projection_ref,
            'pointer': '/payload/projection' + pointer,
            'sourceDocumentId': document,
            'sourceChecksum': checksum,
            'sourceOccurrence': occurrence,
        }
        if row.get('sourcePage') is not None:
            evidence['sourcePage'] = row['sourcePage']
        kind, narrative_type = item['kind'], None
        raw_value, unit = _inspection_value(row) if kind == 'measurement' else (item['resultValue'], row.get('unit'))
        if kind == 'narrative':
            if row.get('sourceReportType') == 'imaging' and item['itemName'].endswith(('影像诊断', '影像所见')):
                narrative_type = 'diagnosis' if item['itemName'].endswith('影像诊断') else 'finding'
            else:
                kind = 'assessment'
        observations.append(
            {
                'observationRef': '/reportSource' + pointer,
                'identityStatus': 'verified',
                'stableObservationId': identity,
                'label': item['itemName'],
                'rawValue': raw_value,
                'unit': unit,
                'referenceRange': item.get('referenceRange'),
                'flag': item.get('flag'),
                'observedAt': item.get('observedAt'),
                'kind': kind,
                'narrativeType': narrative_type,
                'method': row.get('method'),
                'specimen': row.get('specimen'),
                'sourceId': None,
                'sourceCategory': item['sourceCategory'],
                'evidence': [evidence],
            }
        )
    return observations
