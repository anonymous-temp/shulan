"""Report owns source interpretation and cache; Document owns immutable inputs."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from types import MappingProxyType
from typing import Protocol

from modules.analysis.application.extraction import normalize_document_time
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.current_occurrence import OCCURRENCE_REVISION
from modules.analysis.current_understanding import DocumentCurrentBundle
from modules.analysis.snapshots import MANAGED_REPORT_CONTRACT
from modules.analysis.snapshots import MATERIAL_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentPreparedBundle
from modules.report.document_rendering import REPORT_INTERPRETATION_PROJECTION_REVISION
from modules.report.document_rendering import report_understanding_content_v1
from modules.report.document_source import EXTRACTOR_VERSION
from modules.report.document_source import extract_intrinsic_report_source

REPORT_SOURCE_SELECTION = OutputSelection('report_source', '1', EXTRACTOR_VERSION)
REPORT_UNDERSTANDING_SELECTION = OutputSelection('legacy_understanding', '1', 'legacy-understanding-v1')
REPORT_PREPARED_PROJECTION_REVISION = 'report-prepared-projection.v1'
REPORT_PREPARED_SELECTION = OutputSelection('report_source', '1', 'report-source-v16')
REPORT_CURRENT_INTERPRETATION_PROJECTION_REVISION = 'report-understanding-current.v2'
REPORT_OBSERVATION_REFS_PROJECTION_REVISION = OCCURRENCE_REVISION


def _archived_page_sizes(data):
    info = data.get('docInfo') if isinstance(data, dict) else None
    pages = info.get('pages') if isinstance(info, dict) else None
    if not isinstance(pages, list):
        return ()
    return tuple(
        (index, (page['imageWidth'], page['imageHeight']))
        for index, page in enumerate(pages, 1)
        if isinstance(page, dict)
        and all(
            type(page.get(field)) in (int, float) and 0 < page[field] <= 100000
            for field in ('imageWidth', 'imageHeight')
        )
    )


@dataclass(frozen=True)
class ReportSourceInput:
    """One immutable consumer input; report text and metadata are copies on read."""

    _manifest_json: str
    _report_json: str
    text: str
    content: bytes
    _archive_layouts_json: str = '[]'
    _archive_page_sizes: tuple[tuple[int, tuple[float, float]], ...] = ()

    @classmethod
    def from_bundle(cls, bundle: DocumentPreparedBundle, content: bytes):
        if not isinstance(bundle, DocumentPreparedBundle) or not isinstance(content, bytes):
            raise TypeError('A fixed Document bundle and original bytes are required')
        material, understanding = bundle.material.payload, bundle.understanding.payload
        if understanding.get('selection') != asdict(REPORT_UNDERSTANDING_SELECTION):
            raise UnsupportedOutputVersionError('Report does not support this understanding input contract')
        if hashlib.sha256(content).hexdigest() != material['contentSha256']:
            raise ValueError('Original bytes do not match the selected immutable material')
        size = material['documentMetadata'].get('byteSize')
        if size is not None and len(content) != size:
            raise ValueError('Original byte size differs from the selected material')
        parsed = material.get('parsedDocument')
        if not isinstance(parsed, dict) or not isinstance(parsed.get('text'), str):
            raise ValueError('Selected material has no archived parser text')
        manifest = {
            'scope': bundle.material.scope.as_dict(),
            'mode': 'interpreted',
            'materialSnapshotRef': bundle.material.snapshot_id,
            'factsSnapshotRef': bundle.facts.snapshot_id,
            'understandingSnapshotRef': bundle.understanding.snapshot_id,
            'inputSelection': asdict(REPORT_UNDERSTANDING_SELECTION),
            'interpretationProjectionRevision': REPORT_INTERPRETATION_PROJECTION_REVISION,
            'documentId': material['documentId'],
            'fileName': material['fileName'],
            'fileType': material['fileType'],
            'contentSha256': material['contentSha256'],
        }
        # v16 consumes the source-approved public shape, just as legacy public_result
        # did. Keep the full immutable understanding ref in the cache manifest;
        # sanitizing this private consumer copy never rewrites provider evidence.
        report = report_understanding_content_v1(understanding['interpretation'])
        return cls(canonical_json(manifest), canonical_json(report), parsed['text'], content)

    @classmethod
    def from_current_bundle(
        cls, bundle: DocumentCurrentBundle, content: bytes, *, include_analysis_observation_refs=False
    ):
        """Bind Report's source input to one validated current Document round."""
        if not isinstance(bundle, DocumentCurrentBundle) or not isinstance(content, bytes):
            raise TypeError('A fixed current Document bundle and original bytes are required')
        material, facts, understanding = bundle.material.payload, bundle.facts.payload, bundle.understanding.payload
        if hashlib.sha256(content).hexdigest() != material['contentSha256']:
            raise ValueError('Original bytes do not match the selected immutable material')
        size = material['documentMetadata'].get('byteSize')
        if type(size) is not int or size <= 0 or len(content) != size:
            raise ValueError('Original byte size differs from the selected material')
        parsed = material['parsedDocument']
        text = parsed.get('text')
        if parsed.get('success') is not True or not isinstance(text, str) or not text.strip():
            raise ValueError('Current source requires successful, nonempty archived parser text')
        manifest = {
            'scope': bundle.material.scope.as_dict(),
            'mode': 'current',
            'materialSnapshotRef': bundle.material.snapshot_id,
            'factsSnapshotRef': bundle.facts.snapshot_id,
            'understandingSnapshotRef': bundle.understanding.snapshot_id,
            'inputSelection': understanding['selection'],
            'interpretationProjectionRevision': (
                REPORT_OBSERVATION_REFS_PROJECTION_REVISION
                if include_analysis_observation_refs
                else REPORT_CURRENT_INTERPRETATION_PROJECTION_REVISION
            ),
            'interpretationRound': facts['interpretationRound'],
            'interpretationRevision': facts['extractorRevision'],
            'validationRevision': facts['validationRevision'],
            'clinicalContentContract': facts['clinicalContentContract'],
            'clinicalContentDigest': facts['clinicalContentDigest'],
            'clinicalAccepted': facts['clinicalAccepted'],
            'quality': facts['quality'],
            'documentId': material['documentId'],
            'fileName': material['fileName'],
            'fileType': material['fileType'],
            'contentSha256': material['contentSha256'],
        }
        report = _current_report_source_content(
            understanding['interpretation'], facts, include_analysis_observation_refs=include_analysis_observation_refs
        )
        data = parsed.get('data') if include_analysis_observation_refs else None
        layouts = data.get('layouts') if isinstance(data, dict) else None
        page_sizes = _archived_page_sizes(data)
        return cls(
            canonical_json(manifest),
            canonical_json(report),
            text,
            content,
            canonical_json(layouts if isinstance(layouts, list) else []),
            page_sizes,
        )

    @classmethod
    def from_failed_material(cls, material: DocumentSnapshot, content: bytes):
        if not isinstance(material, DocumentSnapshot) or material.contract_version != MATERIAL_CONTRACT:
            raise ValueError('A fixed Document material snapshot is required')
        payload = material.payload
        revision = payload.get('interpretationRevision') or ''
        version = re.fullmatch(r'analysis-health-report-v(\d+)', revision)
        if version is None or not 0 < int(version[1]) < 6 or not payload.get('failureTrace'):
            raise UnsupportedOutputVersionError('This interpretation cannot use the legacy raw-source fallback')
        if not isinstance(content, bytes) or hashlib.sha256(content).hexdigest() != payload.get('contentSha256'):
            raise ValueError('Original bytes do not match selected failed material')
        metadata = payload['documentMetadata']
        if metadata.get('byteSize') is not None and len(content) != metadata['byteSize']:
            raise ValueError('Original byte size differs from selected failed material')
        parsed = payload['parsedDocument']
        if parsed.get('success') is not True or not isinstance(parsed.get('text'), str) or not parsed['text'].strip():
            raise ValueError('Legacy fallback requires successful, nonempty archived parser text')
        manifest = {
            'scope': material.scope.as_dict(),
            'mode': 'legacy_fallback',
            'materialSnapshotRef': material.snapshot_id,
            'inputSelection': MATERIAL_CONTRACT,
            'interpretationRevision': revision,
            'validationRevision': payload.get('validationRevision'),
            'documentId': metadata['documentId'],
            'fileName': metadata['fileName'],
            'fileType': metadata['fileType'],
            'contentSha256': payload['contentSha256'],
        }
        return cls(canonical_json(manifest), '{}', parsed['text'], content)

    @property
    def manifest(self):
        return json.loads(self._manifest_json)

    @property
    def report(self):
        return json.loads(self._report_json)

    @property
    def include_analysis_observation_refs(self):
        return self.manifest.get('interpretationProjectionRevision') == REPORT_OBSERVATION_REFS_PROJECTION_REVISION

    @property
    def archive_layouts(self):
        return json.loads(self._archive_layouts_json)

    @property
    def archive_page_sizes(self):
        return dict(self._archive_page_sizes)

    @property
    def document_id(self):
        return self.manifest['documentId']

    @property
    def file_name(self):
        return self.manifest['fileName']

    @property
    def file_type(self):
        return self.manifest['fileType']

    @property
    def content_sha256(self):
        return self.manifest['contentSha256']


class ReportProjectionStore(Protocol):
    async def get(self, key: str) -> dict | None: ...
    async def put_once(self, key: str, value: dict) -> dict: ...


def _current_section_mirror_refs(item, sections, path, fixed_rows):
    index = item.get('sourceSection')
    if type(index) is not int or not 0 <= index < len(sections):
        return ()
    section = sections[index]
    if not isinstance(section, dict):
        return ()
    section_time = normalize_document_time(section.get('reportTime'))

    def normalized(row):
        content = {key: value for key, value in row.items() if key not in {'sourceSection', 'observedAt'}}
        return content, normalize_document_time(row.get('observedAt')) or section_time

    candidate = normalized(item)
    return tuple(
        pointer
        for offset, child in enumerate(section.get('observations') or [])
        if (pointer := f'{path}/sections/{index}/observations/{offset}') in fixed_rows
        and normalized(child) == candidate
    )


def _is_current_imaging_summary(value, field, sections):
    if not isinstance(value, str) or not value.strip():
        return False
    child_values = []
    for section in sections:
        if isinstance(section, dict) and section.get('reportType') == 'imaging':
            expansion = section.get('expansion')
            child = expansion.get(field) if isinstance(expansion, dict) else None
            if isinstance(child, str) and child.strip():
                child_values.append(child)
    if not child_values:
        return False
    # Single-segment normalization concatenates all values. The multi-segment
    # merge strips and deduplicates those same values before concatenation.
    merged = '\n'.join(dict.fromkeys(child.strip() for child in child_values))
    return value == '\n'.join(child_values) or value.strip() == merged


def _current_report_source_content(
    interpretation: dict, facts: dict, *, include_analysis_observation_refs=False
) -> dict:
    # Keep immutable observation pointers and their actual section context.
    # A root display row can still occur in the fixed facts if its date was
    # written differently from the otherwise identical child row.
    rows = {row['observationRef']: row for row in facts['observations']}
    mirrors_by_ref = {}

    def visit(node, path=''):
        content = {key: node[key] for key in ('reportType', 'reportTime', 'title') if key in node}
        sections = node.get('sections') or []
        observations = []
        for index, item in enumerate(node.get('observations') or []):
            pointer = f'{path}/observations/{index}'
            row = rows.get(pointer)
            if row is None:
                continue
            mirrors = mirrors_by_ref.get(pointer, ())
            if child_refs := _current_section_mirror_refs(item, sections, path, rows):
                if include_analysis_observation_refs:
                    for child_ref in child_refs:
                        mirrors_by_ref.setdefault(child_ref, []).extend((pointer, *mirrors))
                continue
            observation = {
                'itemName': row['label'],
                'resultValue': row['rawValue'],
                'unit': row['unit'],
                'referenceRange': row['referenceRange'],
                'flag': item.get('flag'),
                'observedAt': row['observedAt'],
                'kind': row['kind'],
                'specimen': row['specimen'],
                'method': row['method'],
                'observationRef': pointer,
            }
            if mirrors:
                observation['mirrorObservationRefs'] = list(dict.fromkeys(mirrors))
            observations.append(observation)
        if observations:
            content['observations'] = observations
        raw_expansion = node.get('expansion')
        if node.get('reportType') == 'imaging' and isinstance(raw_expansion, dict):
            expansion, refs, summaries = {}, {}, []
            for field in ('findings', 'diagnosis'):
                row = rows.get(f'{path}/expansion/{field}')
                value = raw_expansion.get(field)
                summary = _is_current_imaging_summary(value, field, sections)
                if row is not None or summary:
                    expansion[field] = row['rawValue'] if row is not None else value
                if summary:
                    summaries.append(field)
                if include_analysis_observation_refs and row is not None:
                    refs[field] = row['observationRef']
            if expansion:
                content['expansion'] = expansion
            if refs:
                content['imagingObservationRefs'] = refs
            if summaries:
                content['derivedImagingFields'] = summaries
        if sections:
            content['sections'] = [
                visit(child, f'{path}/sections/{index}')
                for index, child in enumerate(sections)
                if isinstance(child, dict)
            ]
        return content

    report = visit(interpretation)
    quality = facts['quality']
    retryable, issue_count = quality.get('retryable'), quality.get('issueCount')
    report['processing'] = {
        'status': quality['status'],
        'retryable': retryable if isinstance(retryable, bool) else True,
        'issueCount': issue_count if type(issue_count) is int and issue_count >= 0 else 0,
    }
    return report


async def project_current_report_source(
    inputs: tuple[tuple[DocumentCurrentBundle, bytes], ...],
) -> tuple[OutputSelection, dict]:
    """Extract intrinsic Report facts from ordered, fixed originals."""
    return await asyncio.to_thread(_project_current_report_source, tuple(inputs))


async def project_current_report_source_with_observation_refs(
    inputs: tuple[tuple[DocumentCurrentBundle, bytes], ...],
) -> tuple[OutputSelection, dict]:
    """Extract the current Report facts and verified fixed observation pointers."""
    return await asyncio.to_thread(
        _project_current_report_source, tuple(inputs), include_analysis_observation_refs=True
    )


def _project_current_report_source(inputs, *, include_analysis_observation_refs=False):
    if not inputs:
        raise ValueError('Report projection requires fixed current Document inputs')
    sources = tuple(
        ReportSourceInput.from_current_bundle(
            bundle, content, include_analysis_observation_refs=include_analysis_observation_refs
        )
        for bundle, content in inputs
    )
    scope = sources[0].manifest['scope']
    if any(source.manifest['scope'] != scope for source in sources):
        raise PermissionError('Report cannot combine sources from different user/subject scopes')
    unique = {}
    for source in sources:
        if unique.setdefault(source.document_id, source) != source:
            raise ValueError('One document has conflicting fixed Report inputs')
    sources = tuple(unique.values())
    return REPORT_SOURCE_SELECTION, normalize_report_projection(extract_intrinsic_report_source(sources), sources)


class ReportProjectionService:
    """No parsing, model, source publication or implicit newest-version lookup."""

    def __init__(self, store: ReportProjectionStore, *, implementations=None):
        self.store = store
        self.implementations = MappingProxyType(
            dict(implementations)
            if implementations is not None
            else {
                REPORT_SOURCE_SELECTION: extract_intrinsic_report_source,
            }
        )

    async def project(self, inputs, *, selection: OutputSelection):
        # Freeze the complete ordered input manifest before cache I/O.
        inputs = tuple(inputs)
        if not inputs or any(not isinstance(item, ReportSourceInput) for item in inputs):
            raise ValueError('Report projection requires fixed Document inputs')
        if not isinstance(selection, OutputSelection) or selection not in self.implementations:
            raise UnsupportedOutputVersionError('The exact Report projection implementation is unavailable')
        scope = inputs[0].manifest['scope']
        if any(item.manifest['scope'] != scope for item in inputs):
            raise PermissionError('Report cannot combine sources from different user/subject scopes')
        raw_fallback = any(item.manifest.get('mode') == 'legacy_fallback' for item in inputs)
        if raw_fallback and any(
            item.report.get('processing', {}).get('status', 'complete') != 'complete' for item in inputs
        ):
            raise ValueError('Cannot combine a raw fallback with quarantined interpretation input')
        # Legacy process_documents executes each document only at its first
        # position. Keep the accepted manifest complete while preserving that
        # extraction behavior; conflicting fixed versions cannot be reconciled
        # by consulting the current Document index.
        unique = {}
        for item in inputs:
            previous = unique.get(item.document_id)
            if previous is not None and previous != item:
                raise ValueError('One document has conflicting fixed Report inputs')
            unique.setdefault(item.document_id, item)
        # The original raw fallback bypasses process_documents: preserve every
        # accepted attachment and use only original text/bytes for the whole
        # group, including those whose interpretation completed successfully.
        execution_inputs = (
            tuple(replace(item, _report_json='{}') for item in inputs) if raw_fallback else tuple(unique.values())
        )
        binding = {'selection': asdict(selection), 'inputs': [item.manifest for item in inputs]}
        extractor = self.implementations[selection]
        key = 'report_projection_' + digest(binding)
        cached = await self.store.get(key)
        if cached is not None:
            return self._verified(cached, binding)
        if inspect.iscoroutinefunction(extractor):
            raw = await extractor(execution_inputs)
        else:
            raw = await asyncio.to_thread(extractor, execution_inputs)
            if inspect.isawaitable(raw):
                raw = await raw
        output = normalize_report_projection(raw, execution_inputs)
        row = {'binding': binding, 'output': output, 'outputDigest': digest(output)}
        return self._verified(await self.store.put_once(key, row), binding)

    async def project_prepared(self, inputs, *, selection, projection):
        """Normalize the exact public v16 result once; never rerun its extractor."""
        inputs = tuple(inputs)
        if not inputs or any(not isinstance(item, ReportSourceInput) for item in inputs):
            raise ValueError('fixed Report inputs are required')
        if selection != REPORT_PREPARED_SELECTION:
            raise UnsupportedOutputVersionError('the prepared Report projection version is unavailable')
        if not isinstance(projection, DocumentSnapshot) or projection.contract_version != MANAGED_REPORT_CONTRACT:
            raise ValueError('an immutable managed Report preparation is required')
        payload = projection.payload
        selected = payload.get('manifest', {}).get('inputs')
        if (
            payload.get('selection') != asdict(selection)
            or not isinstance(selected, list)
            or len(selected) != len(inputs)
        ):
            raise ValueError('the prepared Report selection or input count differs')
        unique = {}
        for item, fixed in zip(inputs, selected, strict=True):
            manifest = item.manifest
            if (
                manifest['scope'] != projection.scope.as_dict()
                or manifest['mode'] != 'interpreted'
                or any(
                    fixed.get(key) != manifest[key]
                    for key in ('materialSnapshotRef', 'factsSnapshotRef', 'understandingSnapshotRef')
                )
            ):
                raise ValueError('the prepared Report projection binds different fixed inputs')
            previous = unique.setdefault(item.document_id, item)
            if previous != item:
                raise ValueError('one document has conflicting prepared Report inputs')
        binding = {
            'consumerRevision': REPORT_PREPARED_PROJECTION_REVISION,
            'selection': asdict(selection),
            'inputs': [item.manifest for item in inputs],
            'projectionRef': projection.snapshot_id,
            'projectionDigest': projection.content_digest,
        }
        key = 'report_prepared_projection_' + digest(binding)
        cached = await self.store.get(key)
        if cached is not None:
            return self._verified(cached, binding)
        output = normalize_report_projection(payload['projection'], tuple(unique.values()))
        quality = payload.get('processingStatus')
        if quality not in {'complete', 'partial', 'unavailable'}:
            raise ValueError('the prepared Report quality is unavailable')
        if output.get('processing', {}).get('status', 'complete') != quality:
            raise ValueError('the prepared Report quality differs from its fixed inputs')
        row = {'binding': binding, 'output': output, 'outputDigest': digest(output)}
        return self._verified(await self.store.put_once(key, row), binding)

    @staticmethod
    def _verified(row, binding):
        if row.get('binding') != binding or row.get('outputDigest') != digest(row.get('output')):
            raise ValueError('Report projection cache has a conflicting input/version/output binding')
        return deepcopy(row['output'])


def normalize_report_projection(value, snapshots):
    """Freeze Report's v1 consumer projection; legacy shared aliases may coexist."""
    if not isinstance(value, dict):
        raise ValueError('Report source extractor must return an object')
    result = {
        key: deepcopy(value[key])
        for key in (
            'subject',
            'sample',
            'metrics',
            'taxa',
            'diseaseResults',
            'panelCounts',
            'enterotype',
            'sourceSummary',
            'supplementaryHospitalData',
        )
        if key in value
    }
    for key in ('subject', 'sample'):
        result.setdefault(key, {})
        if not isinstance(result[key], dict):
            raise ValueError('Report source subject and sample must be objects')
    for key in ('metrics', 'taxa', 'diseaseResults', 'panelCounts', 'supplementaryHospitalData'):
        result.setdefault(key, [])
        if not isinstance(result[key], list) or any(not isinstance(item, dict) for item in result[key]):
            raise ValueError('Report source facts must be object arrays')
    result.setdefault('enterotype', None)
    result.setdefault('sourceSummary', None)
    first = snapshots[0]
    result.update(
        {
            'sourceType': first.file_type,
            'sourceName': first.file_name if len(snapshots) == 1 else ' / '.join(item.file_name for item in snapshots),
            'sourceChecksum': first.content_sha256 if len(snapshots) == 1 else None,
            'sourceDocuments': [
                {'documentId': item.document_id, 'sourceName': item.file_name, 'sourceChecksum': item.content_sha256}
                for item in snapshots
            ],
        }
    )
    states = [item.report.get('processing') for item in snapshots]
    incomplete = [
        state for state in states if isinstance(state, dict) and state.get('status') in {'partial', 'unavailable'}
    ]
    if incomplete:
        result['processing'] = {
            'status': 'unavailable' if any(state.get('status') == 'unavailable' for state in incomplete) else 'partial',
            'retryable': any(state.get('retryable', True) for state in incomplete),
            'issueCount': sum(state.get('issueCount', 0) for state in incomplete),
        }
    return result
