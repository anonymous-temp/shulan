"""Current patient fields, retaining AI grades and the exact original evidence.

This is a storage/admission contract. It never judges an AI grade, infers a
birthday, or reads mutable Profile state.
"""

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from math import isfinite

from modules.analysis.application.extraction import merge_document_results
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import DocumentRetiredContribution
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest

BASIC_INFO_CONTRACT = 'analysis.basic-info.v1'
BASIC_INFO_FIELDS = ('name', 'gender', 'age')
BASIC_INFO_STATUSES = {'extracted', 'not_found', 'unresolved', 'conflicting'}


def _require(condition, message):
    if not condition:
        raise ValueError('invalid fixed basic information: ' + message)


def _value(field, value):
    if field == 'name':
        return isinstance(value, str) and bool(value.strip())
    if field == 'gender':
        return value in ('男', '女')
    return type(value) in (int, float) and isfinite(value) and 0 <= value <= 150


def _evidence(value, text, *, required=False):
    if value is None:
        _require(not required, 'an extracted field needs original evidence')
        return
    _require(isinstance(value, dict) and set(value) == {'quote', 'start', 'end'}, 'evidence shape differs')
    quote, start, end = value['quote'], value['start'], value['end']
    _require(isinstance(quote, str) and bool(quote.strip()), 'evidence quote is empty')
    _require(type(start) is int and type(end) is int and 0 <= start < end <= len(text), 'evidence bounds differ')
    _require(text[start:end] == quote, 'evidence does not match the fixed original text')


def _observed_at(value):
    if value is None:
        return
    _require(isinstance(value, str), 'observation time is not ISO text')
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            _require(len(value) > 10 and value[10] in ('T', ' '), 'observation time lacks a calendar date')
            datetime.fromisoformat(value)
    except (ValueError, TypeError) as error:
        raise ValueError('invalid fixed basic information: observation time is not ISO date/time') from error


def accepted_basic_info(interpretation, text):
    """Validate current top-level metadata and return only individually usable fields.

    Section-local fields are archive content only. `single` is the interpreter's
    explicit patient determination; an optional report patient reference is not
    an application subject ID or an additional authorization protocol.
    """
    _require(isinstance(text, str), 'fixed original text is unavailable')
    values, metadata = interpretation.get('basicInfo'), interpretation.get('basicInfoMeta')
    _require(isinstance(values, dict) and set(values) == set(BASIC_INFO_FIELDS), 'field set differs')
    _require(isinstance(metadata, dict) and set(metadata) == set(BASIC_INFO_FIELDS), 'metadata field set differs')
    accepted = []
    for field in BASIC_INFO_FIELDS:
        value, meta = values[field], metadata[field]
        _require(isinstance(meta, dict), 'field metadata is not an object')
        required = {
            'status',
            'sourceReliability',
            'observedAt',
            'reason',
            'evidence',
            'subjectRef',
            'subjectEvidence',
            'subjectStatus',
            'candidates',
        }
        _require(set(meta) == required, 'metadata contract differs')
        status, reliability = meta['status'], meta['sourceReliability']
        _require(isinstance(status, str) and status in BASIC_INFO_STATUSES, 'unknown extraction status')
        _require(reliability is None or reliability in ('low', 'medium'), 'report grade is not low/medium')
        _require(meta['subjectStatus'] in ('single', 'ambiguous', 'unknown'), 'unknown patient status')
        _require(isinstance(meta['reason'], str) and len(meta['reason']) <= 2000, 'reason is not short text')
        _require(meta['subjectRef'] is None or isinstance(meta['subjectRef'], str), 'patient reference is not text')
        _observed_at(meta['observedAt'])
        _evidence(meta['evidence'], text, required=status == 'extracted')
        _evidence(meta['subjectEvidence'], text)
        # Analysis caps each model response separately and can merge multiple responses or sections.
        _require(isinstance(meta['candidates'], list), 'candidates are not an array')
        for candidate in meta['candidates']:
            _require(isinstance(candidate, dict), 'candidate is not an object')
            _require(
                set(candidate) == {'value', 'evidence', 'sourceReliability', 'reason', 'subjectRef'},
                'candidate contract differs',
            )
            _require(_value(field, candidate['value']), 'candidate field value is invalid')
            _require(candidate['sourceReliability'] in ('low', 'medium'), 'candidate grade is not low/medium')
            _require(isinstance(candidate['reason'], str), 'candidate reason is not text')
            _require(
                candidate['subjectRef'] is None or isinstance(candidate['subjectRef'], str), 'candidate patient differs'
            )
            _evidence(candidate['evidence'], text, required=True)
        if status == 'extracted':
            _require(_value(field, value), 'extracted field value is invalid')
            _require(
                reliability in ('low', 'medium') and meta['subjectStatus'] == 'single', 'patient is not determined'
            )
            _require(
                bool(meta['reason'].strip()) and not meta['candidates'], 'extracted field is not a single decision'
            )
            accepted.append(
                {'field': field, 'value': value, **{k: v for k, v in meta.items() if k not in {'status', 'candidates'}}}
            )
        else:
            _require(value is None and reliability is None, 'unresolved fields cannot supply a current value')
            if status == 'not_found':
                _require(meta['evidence'] is None and not meta['candidates'], 'missing field cannot supply evidence')
    return accepted


ANALYSIS_PUBLIC_CONFIRMATION = 'analysis_public'

CLINICAL_CONTENT_CONTRACT = 'document.clinical-content.v1'
_DEMOGRAPHIC_FIELDS = ('basicInfo', 'basicInfoMeta', 'name', 'gender', 'age', '_profileSourcePatient')
_CLINICAL_ROW_ARRAYS = ('observations', 'expansion', 'unresolvedItems')
_REPORT_TYPES = ('lab', 'imaging', 'checkup', 'microbiome', 'unknown')
_EMPTY_REPORT_FIELDS = {
    'reportType': (None, *_REPORT_TYPES),
    'status': (None, 'pending'),
    'title': (None, ''),
    'reportTime': (None, ''),
    'aiSummary': (None, ''),
    'observations': (None, []),
    'unresolvedItems': (None, []),
    'sections': (None, []),
    'metadataEvidence': (None, {}),
    'expansionEvidence': (None, {}),
    'expansion': (None, [], {}, {'findings': None, 'diagnosis': None}, {'checkupOrgName': None, 'checkupTime': None}),
}
_EXECUTION_FIELDS = {
    'version',
    'sourceRevision',
    'interpretationRevision',
    'validationRevision',
    'extractorRevision',
    'interpretationRound',
    'implementationRevision',
    'traceId',
    'executionId',
    'elapsedMs',
}


def _demographic_section(original, content, index):
    demographics = any(key in original for key in _DEMOGRAPHIC_FIELDS) or any(
        isinstance(original.get(key), dict) and set(original[key]).intersection(('name', 'gender', 'age'))
        for key in ('subject', 'patientInfo')
    )
    return demographics and all(
        type(value) is int and value == index
        if key == 'sectionIndex'
        else key in _EMPTY_REPORT_FIELDS and value in _EMPTY_REPORT_FIELDS[key]
        for key, value in content.items()
    )


def _strip_nonclinical_fields(original):
    content = deepcopy({key: value for key, value in original.items() if key != 'sections'})
    for field in _DEMOGRAPHIC_FIELDS:
        content.pop(field, None)
    for field in ('subject', 'patientInfo'):
        if isinstance(content.get(field), dict):
            for demographic in ('name', 'gender', 'age'):
                content[field].pop(demographic, None)
            if not content[field]:
                content.pop(field)
    for field in _EXECUTION_FIELDS:
        content.pop(field, None)
    if isinstance(content.get('processing'), dict):
        for field in _EXECUTION_FIELDS:
            content['processing'].pop(field, None)
    return content


def _derived_clinical_aggregate(node, sections):
    """Recognize the merged view without discarding independent root content."""
    if not sections or node.get('expansionEvidence'):
        return False
    for index, section in enumerate(sections):
        if (
            not isinstance(section, dict)
            or type(section.get('sectionIndex')) is not int
            or section['sectionIndex'] != index
            or section.get('sections')
            or section.get('reportType') not in _REPORT_TYPES
            or section.get('status') not in ('normal', 'abnormal', 'pending')
            or section.get('metadataEvidence') is not None
            and not isinstance(section['metadataEvidence'], dict)
        ):
            return False
        for field in ('observations', 'unresolvedItems'):
            rows = section.get(field, [])
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) or row.get('evidence') and not isinstance(row['evidence'], dict)
                for row in rows
            ):
                return False
    parts = [_strip_nonclinical_fields(section) for section in sections]
    # A second empty part makes the merge reconstruct even a one-section view.
    if len(parts) == 1:
        parts.append({'reportType': 'unknown', 'status': 'pending'})
    projected = merge_document_results(parts)
    return all(
        node.get(key) == projected[key] for key in ('reportType', 'expansion', 'observations', 'unresolvedItems')
    )


def _remap_section_rows(content, indexes):
    for field in _CLINICAL_ROW_ARRAYS:
        rows = content.get(field)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and type(row.get('sourceSection')) is int and row['sourceSection'] in indexes:
                row['sourceSection'] = indexes[row['sourceSection']]


def _project_clinical_tree(original, paths, original_path='', projected_path=''):
    content = _strip_nonclinical_fields(original)
    paths[original_path] = projected_path
    sections = original.get('sections')
    if not isinstance(sections, list):
        if 'sections' in original:
            content['sections'] = deepcopy(sections)
        return content

    if _derived_clinical_aggregate(original, sections):
        # Retained children and normalized rows already carry this same data.
        content.pop('reportType', None)
        content.pop('expansion', None)

    retained, indexes = [], {}
    for old_index, section in enumerate(sections):
        new_index = len(retained)
        if isinstance(section, dict):
            old_path = f'{original_path}/sections/{old_index}'
            new_path = f'{projected_path}/sections/{new_index}'
            projected = _project_clinical_tree(section, paths, old_path, new_path)
            if _demographic_section(section, projected, old_index):
                paths.pop(old_path)
                continue
            if type(projected.get('sectionIndex')) is int and projected['sectionIndex'] == old_index:
                projected['sectionIndex'] = new_index
            if not section.get('sections'):
                # Native source rows can carry their parent's section link on the leaf too.
                _remap_section_rows(projected, {old_index: new_index})
        else:
            projected = deepcopy(section)
        retained.append(projected)
        indexes[old_index] = new_index

    if all(isinstance(section, dict) and not section.get('sections') for section in sections):
        # Analysis section links address the flattened child list, which is
        # identical to the positional child list for this producer shape.
        _remap_section_rows(content, indexes)
    if retained or not sections:
        content['sections'] = retained
    return content


def _project_observation_refs(observations, paths):
    projected = deepcopy(observations)
    refs = {}
    for row in projected:
        if isinstance(row, dict):
            row.pop('patientSlot', None)
        if not isinstance(row, dict) or not isinstance(row.get('observationRef'), str):
            continue
        match = re.fullmatch(
            r'(.*)(/(?:observations|unresolvedItems)/(?:0|[1-9][0-9]*)|/expansion/(?:findings|diagnosis))',
            row['observationRef'],
        )
        if match and match[1] in paths:
            normalized = paths[match[1]] + match[2]
            refs[row['observationRef']] = normalized
            row['observationRef'] = normalized
    return projected, refs


def clinical_content(interpretation, observations):
    """Stable clinical comparison while keeping all fixed snapshots verbatim."""
    paths = {}
    clinical = _project_clinical_tree(interpretation, paths)
    projected, refs = _project_observation_refs(observations, paths)
    bindings = clinical.get('sourceBindings')
    if isinstance(bindings, dict):
        # The fixed observation reader recognizes only a binding for its own pointer.
        mapped = {refs.get(key, key): value for key, value in bindings.items()}
        if len(mapped) == len(bindings):
            clinical['sourceBindings'] = mapped
    return {
        'contractVersion': CLINICAL_CONTENT_CONTRACT,
        'interpretation': clinical,
        'observations': projected,
    }


def clinical_content_digest(interpretation, observations):
    return digest(clinical_content(interpretation, observations))


@dataclass(frozen=True, slots=True)
class DocumentCurrentSourcePlan:
    """Current complete inventory; the original lifecycle never changes on recovery."""

    scope: DocumentScope
    source_id: str
    source_revision: int
    active: tuple[DocumentContributionHandle, ...]
    retired: tuple[DocumentRetiredContribution, ...]
    fence_evidence_ref: str
    active_set_evidence_ref: str
    _fence_json: str
    transition_ref: str
    lifecycle_epoch: int

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        for value in (self.source_id, self.fence_evidence_ref, self.active_set_evidence_ref, self.transition_ref):
            exact_text(value, 'current source plan')
        if type(self.source_revision) is not int or self.source_revision < 1:
            raise ValueError('a positive material revision is required')
        if type(self.lifecycle_epoch) is not int or self.lifecycle_epoch < 1:
            raise ValueError('the original subject lifecycle must be a positive exact integer')
        if (
            not isinstance(self.active, tuple)
            or not isinstance(self.retired, tuple)
            or len(self.active) > 1000
            or len(self.retired) > 1000
            or any(not isinstance(row, DocumentContributionHandle) for row in self.active)
            or any(not isinstance(row, DocumentRetiredContribution) for row in self.retired)
            or (not self.active and not self.retired)
        ):
            raise ValueError('a bounded complete inventory or explicit retirement is required')
        if (
            self.fence != {'admissionKind': 'current', 'evidenceRef': self.active_set_evidence_ref}
            or self.fence_evidence_ref != self.active_set_evidence_ref
            or self.transition_ref != self.active_set_evidence_ref
        ):
            raise ValueError('the current plan must be the exact inventory evidence')
        object.__setattr__(self, '_fence_json', canonical_json(self.fence))

    @property
    def fence(self):
        return json.loads(self._fence_json)
