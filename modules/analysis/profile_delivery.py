"""Project fixed Analysis results for Profile without deciding account ownership."""

import re
import unicodedata

from modules.analysis.application.extraction import document_sections
from modules.analysis.application.extraction import normalize_document_time
from modules.analysis.application.extraction import validated_document_time
from modules.analysis.basic_info_contracts import accepted_basic_info
from modules.analysis.snapshots import _observations
from modules.analysis.snapshots import digest

UNASSIGNED = 'unassigned'


def _parts(node, path=''):
    yield path, node
    for index, child in enumerate(node.get('sections') or ()):
        yield from _parts(child, f'{path}/sections/{index}')


def _normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).split()).casefold()


def _anchor(fields):
    anchors = {
        _normalize(row['subjectRef'])
        for row in fields
        if row['subjectRef'] and row['subjectEvidence'] and row['subjectRef'] in row['subjectEvidence']['quote']
    }
    name = next((_normalize(row['value']) for row in fields if row['field'] == 'name'), None)
    if name in anchors:
        anchors.remove(name)
    return (next(iter(anchors)) if len(anchors) == 1 else None), name


def _section_fields(node, text):
    if 'basicInfo' not in node or 'basicInfoMeta' not in node:
        return []
    return accepted_basic_info(node, text)


def report_patients(interpretation, text, document_id):
    """Recognize distinct anchored sections, retaining the proved fields and their section binding."""
    root = accepted_basic_info(interpretation, text)
    parts = tuple(_parts(interpretation))
    sections = [(path, _section_fields(node, text)) for path, node in parts if path]
    root_rows = (*_observations(interpretation), *_pending_observations(interpretation))
    if any(not row['observationRef'].startswith('/sections/') for row in root_rows):
        local = interpretation.get('_profileSourcePatient')
        sections.insert(0, ('', _section_fields(local, text) if local else root))
    identities = {path: _anchor(fields) for path, fields in sections}
    by_name = {}
    for anchor, name in identities.values():
        if anchor and name:
            by_name.setdefault(name, set()).add(anchor)

    slots, fields_by_slot = {'': UNASSIGNED}, {}
    for path, fields in sections:
        anchor, name = identities[path]
        matched = by_name.get(name, ())
        if not anchor and len(matched) == 1:
            anchor = next(iter(matched))
        if anchor is None and (not name or len(matched) > 1):
            parent = path.rsplit('/sections/', 1)[0]
            slots[path] = slots.get(parent, UNASSIGNED)
            continue
        identity = ['anchor', anchor] if anchor else ['name', name]
        slot = 'patient_' + digest([document_id, *identity])
        slots[path] = slot
        for field in fields:
            fields_by_slot.setdefault((slot, field['field']), []).append(field)
    if len(set(slots.values()) - {UNASSIGNED}) < 2:
        # A conflicted field alone does not establish multiple patients. An
        # explicit judgment must have its own fixed original patient evidence.
        ambiguous = any(
            meta.get('subjectStatus') == 'ambiguous' and meta.get('subjectEvidence')
            for _, node in parts
            for meta in node.get('basicInfoMeta', {}).values()
        )
        return root, ({'': UNASSIGNED} if ambiguous else {})

    result = []
    for (slot, _), choices in fields_by_slot.items():
        if len({row['value'] for row in choices}) == 1:
            best = max(choices, key=lambda row: row['sourceReliability'] == 'medium')
            result.append({**best, 'patientSlot': slot})
    return result, slots


def _is_pending_mirror(item, sections):
    index = item.get('sourceSection')
    if type(index) is not int or not 0 <= index < len(sections):
        return False
    selected = sections[index]
    current = {key: value for key, value in item.items() if key not in {'sourceSection', 'observedAt'}}
    for candidate in selected.get('unresolvedItems') or ():
        original = {key: value for key, value in candidate.items() if key not in {'sourceSection', 'observedAt'}}
        if current == original and normalize_document_time(item.get('observedAt')) in {
            normalize_document_time(candidate.get('observedAt')),
            normalize_document_time(selected.get('reportTime')),
        }:
            return True
    return False


def _pending_observations(interpretation):
    for path, node in _parts(interpretation):
        sections = [part for child in node.get('sections') or () for part in document_sections(child)]
        for index, item in enumerate(node.get('unresolvedItems') or ()):
            if not isinstance(item, dict) or _is_pending_mirror(item, sections):
                continue
            if not item.get('itemName') and item.get('resultValue') is None:
                continue
            yield {
                'observationRef': f'{path}/unresolvedItems/{index}',
                'identityStatus': 'identity_unresolved',
                'stableObservationId': None,
                'label': item.get('itemName'),
                'rawValue': item.get('resultValue'),
                'unit': item.get('unit'),
                'referenceRange': item.get('referenceRange'),
                'flag': item.get('rawFlag', item.get('flag')),
                'observedAt': item.get('observedAt') or node.get('reportTime'),
                'kind': item.get('kind'),
                'method': item.get('method'),
                'specimen': item.get('specimen'),
                'narrativeType': None,
                'evidence': item.get('evidence') or [],
                'sourceId': item.get('sourceId'),
                'verificationStatus': 'unconfirmed',
            }


def current_observations(interpretation, slots):
    rows = [*_observations(interpretation), *_pending_observations(interpretation)]
    locations = {}
    for row in rows:
        if row['stableObservationId']:
            locations.setdefault(row['stableObservationId'], []).append(row)
    conflicts = {
        key
        for key, candidates in locations.items()
        if len(
            {
                (row['label'], str(row['rawValue']), row['unit'], validated_document_time(row['observedAt']))
                for row in candidates
            }
        )
        > 1
    }
    for row in rows:
        if isinstance(row['evidence'], dict):
            row['evidence'] = [row['evidence']]
        # A parser-backed fixed observation binding can support this row
        # independently of the processing status of any other row in the file.
        if 'verificationStatus' not in row:
            complete = validated_document_time(row['observedAt']) and row['label'] and row['rawValue'] is not None
            has_proof = row['identityStatus'] == 'verified' and bool(row['evidence'])
            row['verificationStatus'] = 'verified_source' if complete and has_proof else 'unconfirmed'
        if row['stableObservationId'] in conflicts:
            row['verificationStatus'] = 'conflicted'
        if slots:
            path = re.split(r'/(?:observations|unresolvedItems|expansion)/', row['observationRef'], maxsplit=1)[0]
            row['patientSlot'] = slots.get(path, UNASSIGNED)
    return rows


def has_clinical_delivery(facts):
    return bool(facts['observations'])
