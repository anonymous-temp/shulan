"""Document-owned immutable material/fact contracts and explicit capture commands.

These contracts describe source observations, never Report projections or Profile
fact types. JSON is stored as text internally so callers cannot mutate a snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Protocol

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.extraction import document_sections
from modules.analysis.application.extraction import normalize_document_time
from modules.analysis.contracts import OutputSelection

MATERIAL_CONTRACT = 'document.material.v1'
FACTS_CONTRACT = 'document.facts.v1'
FACTS_CONTRACT_V2 = 'document.facts.v2'
UNDERSTANDING_CONTRACT_V2 = 'document.understanding.v2'
COMPLETE_SOURCE_CONTRACT_V2 = 'document.source-observations.v2'
SOURCE_INVENTORY_CONTRACT_V2 = 'document.source-inventory.v2'
SOURCE_CONTRIBUTION_CONTRACT_V2 = 'document.source-contribution.v2'
SOURCE_CAPTURE_PROOF_CONTRACT_V2 = 'document.source-capture-proof.v2'
RETRACTION_CONTRACT = 'document.retraction.v1'
UNDERSTANDING_CONTRACT = 'document.understanding.v1'
COMPLETE_SOURCE_CONTRACT = 'document.source-observations.v1'
SOURCE_INVENTORY_CONTRACT = 'document.source-inventory.v1'
SOURCE_CONTRIBUTION_CONTRACT = 'document.source-contribution.v1'
SOURCE_CAPTURE_PROOF_CONTRACT = 'document.source-capture-proof.v1'
LEGACY_REPORT_SOURCE_CONTRACT = 'document.legacy-report-source.v1'
LEGACY_REPORT_INPUT_CONTRACT = 'document.legacy-report-input.v1'
MANAGED_REPORT_CONTRACT = 'document.managed-report-preparation.v1'
CURRENT_REPORT_PROJECTION_CONTRACT = 'document.current-report-projection.v1'
FORMAL_LEGACY_REPORT_CONTRACT = 'document.formal-legacy-report-source.v1'


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical_json(value).encode('utf-8')).hexdigest()


@dataclass(frozen=True, slots=True)
class DocumentScope:
    user_id: str
    subject_id: str

    def __post_init__(self):
        if any(not isinstance(v, str) or not v or v.strip() != v for v in (self.user_id, self.subject_id)):
            raise ValueError('an explicit user and subject scope is required')

    def as_dict(self):
        return {'userId': self.user_id, 'subjectId': self.subject_id}


@dataclass(frozen=True, slots=True)
class DocumentSnapshot:
    scope: DocumentScope
    contract_version: str
    source_id: str
    source_revision: int
    _payload_json: str

    def __post_init__(self):
        if self.contract_version not in {
            MATERIAL_CONTRACT,
            FACTS_CONTRACT_V2,
            UNDERSTANDING_CONTRACT_V2,
            COMPLETE_SOURCE_CONTRACT_V2,
            SOURCE_INVENTORY_CONTRACT_V2,
            SOURCE_CONTRIBUTION_CONTRACT_V2,
            SOURCE_CAPTURE_PROOF_CONTRACT_V2,
            FACTS_CONTRACT,
            RETRACTION_CONTRACT,
            UNDERSTANDING_CONTRACT,
            COMPLETE_SOURCE_CONTRACT,
            SOURCE_INVENTORY_CONTRACT,
            SOURCE_CONTRIBUTION_CONTRACT,
            SOURCE_CAPTURE_PROOF_CONTRACT,
            LEGACY_REPORT_SOURCE_CONTRACT,
            LEGACY_REPORT_INPUT_CONTRACT,
            MANAGED_REPORT_CONTRACT,
            CURRENT_REPORT_PROJECTION_CONTRACT,
            FORMAL_LEGACY_REPORT_CONTRACT,
        }:
            raise ValueError('unsupported document snapshot contract')
        if not self.source_id or type(self.source_revision) is not int or self.source_revision < 1:
            raise ValueError('invalid document source identity')
        payload = json.loads(self._payload_json)
        if self.contract_version == RETRACTION_CONTRACT:
            expected = {'publicationRevision', 'priorRevision', 'eventKind'}
            if (
                set(payload) not in (expected, expected | {'lifecycleEpoch'})
                or (
                    'lifecycleEpoch' in payload
                    and (type(payload['lifecycleEpoch']) is not int or payload['lifecycleEpoch'] < 1)
                )
                or payload['eventKind'] != 'retract'
                or type(payload['priorRevision']) is not int
                or payload['priorRevision'] < 1
                or type(payload['publicationRevision']) is not int
                or payload['publicationRevision'] != payload['priorRevision'] + 1
            ):
                raise ValueError('invalid immutable retraction snapshot')
        object.__setattr__(self, '_payload_json', canonical_json(payload))

    @property
    def payload(self):
        return json.loads(self._payload_json)

    @property
    def canonical(self):
        return {
            'scope': self.scope.as_dict(),
            'contractVersion': self.contract_version,
            'sourceType': 'health_report',
            'sourceId': self.source_id,
            'sourceRevision': self.source_revision,
            **(self.payload if self.contract_version == RETRACTION_CONTRACT else {'payload': self.payload}),
        }

    @property
    def content_digest(self):
        return digest(self.canonical)

    @property
    def snapshot_id(self):
        prefix = (
            'document_source_snapshot_'
            if self.contract_version in {COMPLETE_SOURCE_CONTRACT, COMPLETE_SOURCE_CONTRACT_V2}
            else 'document_snapshot_'
        )
        return prefix + self.content_digest


class DocumentSnapshotStore(Protocol):
    async def put(self, snapshot: DocumentSnapshot) -> None: ...
    async def get(self, scope: DocumentScope, snapshot_id: str) -> DocumentSnapshot: ...


class DocumentFactsProjector(Protocol):
    selection: OutputSelection

    async def project(self, snapshot: DocumentSnapshot) -> dict: ...


@dataclass(frozen=True, slots=True)
class DocumentProjection:
    facts_snapshot_id: str
    selection: OutputSelection
    _payload_json: str

    @property
    def payload(self):
        return json.loads(self._payload_json)


def _is_section_mirror(item: dict, sections: list[dict], *, normalize_display_date=False) -> bool:
    # Match document_sections' occurrence rule without flattening away the
    # original JSON pointer carried by the public snapshot.
    index = item.get('sourceSection')
    if type(index) is not int or not 0 <= index < len(sections):
        return False
    source = sections[index]
    section_date = normalize_document_time(source.get('reportTime'))
    if normalize_display_date:
        ignored = {'sourceSection', 'observedAt'}
        candidate_date = normalize_document_time(item.get('observedAt')) or section_date
        candidate = {key: value for key, value in item.items() if key not in ignored}
        return any(
            candidate == {key: value for key, value in child.items() if key not in ignored}
            and candidate_date == (normalize_document_time(child.get('observedAt')) or section_date)
            for child in source.get('observations', [])
        )
    candidate = {key: value for key, value in item.items() if key != 'sourceSection'}
    for original in source.get('observations', []):
        projected = {key: value for key, value in original.items() if key != 'sourceSection'}
        date_text = normalize_document_time(original.get('observedAt')) or section_date
        if date_text:
            projected['observedAt'] = date_text
        if candidate == projected:
            return True
    return False


def _is_joined_imaging(node: dict, field: str, sections: list[dict]) -> bool:
    # Joined display text has no independent occurrence proof. Equal wording
    # with root evidence is still an independent source observation.
    if not sections or node.get('expansionEvidence'):
        return False
    if not all(type(section.get('sectionIndex')) is int for section in sections):
        return False
    values = [
        section['expansion'][field]
        for section in sections
        if isinstance(section.get('expansion'), dict) and section['expansion'].get(field)
    ]
    joined = '\n'.join(dict.fromkeys(value.strip() for value in values)) or None
    return node['expansion'].get(field) == joined


def _observations(result: dict, *, normalize_display_date=False) -> list[dict]:
    rows = []
    bindings = result.get('sourceBindings') or {}

    def append(pointer, item, node):
        binding = bindings.get(pointer)
        verified = isinstance(binding, str) and binding.startswith('v6:')
        rows.append(
            {
                'observationRef': pointer,
                'identityStatus': 'verified' if verified else 'identity_unresolved',
                'stableObservationId': binding if verified else None,
                'label': item.get('itemName'),
                'rawValue': item.get('resultValue'),
                'unit': item.get('unit'),
                'referenceRange': item.get('referenceRange'),
                'flag': item.get('rawFlag', item.get('flag')),
                'observedAt': item.get('observedAt') or node.get('reportTime'),
                'kind': item.get('kind'),
                'method': item.get('method'),
                'specimen': item.get('specimen'),
                'narrativeType': item.get('narrativeType'),
                'evidence': item.get('evidence') or [],
                'sourceId': item.get('sourceId'),
            }
        )

    def visit(node, path=''):
        sections = node.get('sections') or []
        flattened = [part for child in sections if isinstance(child, dict) for part in document_sections(child)]
        for index, item in enumerate(node.get('observations') or []):
            if not isinstance(item, dict):
                continue
            if _is_section_mirror(item, flattened, normalize_display_date=normalize_display_date):
                continue
            append(f'{path}/observations/{index}', item, node)
        if node.get('reportType') == 'imaging' and isinstance(node.get('expansion'), dict):
            for name, label in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                value = node['expansion'].get(name)
                if value and not _is_joined_imaging(node, name, flattened):
                    append(
                        f'{path}/expansion/{name}',
                        {
                            'itemName': label,
                            'resultValue': value,
                            'kind': 'narrative',
                            'narrativeType': name,
                            'evidence': (node.get('expansionEvidence') or {}).get(name, []),
                        },
                        node,
                    )
        for index, child in enumerate(sections):
            if isinstance(child, dict):
                visit(child, f'{path}/sections/{index}')

    visit(result)
    return rows


class DocumentSnapshotService:
    """Pure read operations and separately named commands; no implicit publication.

    Capture archives existing stored parser/interpreter results. Prepare may call
    OCR/the interpreter, but never the legacy publication gateway. Project only
    consumes a fixed facts snapshot and the explicitly supplied implementation.
    """

    def __init__(self, *, processing_service, snapshots: DocumentSnapshotStore):
        self.processing = processing_service
        self.snapshots = snapshots

    async def _stored(self, scope: DocumentScope, document_id: str):
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required; implicit subject creation is unsupported')
        reuse = self.processing._document_reuse
        if reuse is None:
            raise RuntimeError('document reuse storage is required')
        document = await reuse.index.get(document_id, scope.user_id, scope.subject_id)
        if document is None:
            raise DocumentNotFoundError('document not found in scope')
        return reuse, document

    async def capture_material(self, *, scope: DocumentScope, document_id: str) -> DocumentSnapshot:
        reuse, document = await self._stored(scope, document_id)
        raw = await reuse._payload(document, 'docmind_result')
        if not isinstance(raw, dict):
            raise DocumentNotFoundError('stored parser result is unavailable')
        snapshot = DocumentSnapshot(
            scope,
            MATERIAL_CONTRACT,
            document['canonicalReportId'],
            1,
            canonical_json(
                {
                    'documentId': document_id,
                    'contentSha256': document['contentSha256'],
                    'fileName': document['fileName'],
                    'fileType': document['fileType'],
                    'parserRevision': document.get('parserVersion'),
                    'originalArtifactRef': document.get('payloadRefs', {}).get('original_file'),
                    'parseArtifactRef': document.get('payloadRefs', {}).get('docmind_result'),
                    'parsedDocument': raw,
                }
            ),
        )
        await self.snapshots.put(snapshot)
        return snapshot

    async def capture_facts(self, *, scope: DocumentScope, document_id: str) -> DocumentSnapshot:
        reuse, document = await self._stored(scope, document_id)
        if document.get('status') != 'completed':
            raise DocumentNotFoundError('a settled interpretation is required for fact capture')
        result = await reuse._payload(document, 'interpretation')
        if not isinstance(result, dict):
            raise DocumentNotFoundError('stored interpretation is unavailable')
        material = await self.capture_material(scope=scope, document_id=document_id)
        snapshot = DocumentSnapshot(
            scope,
            FACTS_CONTRACT,
            document['canonicalReportId'],
            1,
            canonical_json(
                {
                    'documentId': document_id,
                    'materialSnapshotRef': material.snapshot_id,
                    'extractorRevision': document.get('interpretationVersion'),
                    'validationRevision': document.get('interpretationValidatedVersion'),
                    'interpretationRound': document.get(
                        'interpretationResultRound', document.get('interpretationRound', 0)
                    ),
                    'interpretationArtifactRef': document.get('payloadRefs', {}).get('interpretation'),
                    'legacySourceRevision': document.get('sourceRevision'),
                    'quality': result.get('processing', {'status': 'complete'}),
                    'observations': _observations(result),
                }
            ),
        )
        # Detect recovery/rewrites racing capture. Never label new bytes with old
        # extractor metadata; callers can retry the explicit capture command.
        _, current = await self._stored(scope, document_id)
        fields = (
            'status',
            'payloadRefs',
            'interpretationVersion',
            'interpretationValidatedVersion',
            'interpretationResultRound',
            'interpretationRound',
        )
        latest = await reuse._payload(current, 'interpretation')
        if any(current.get(k) != document.get(k) for k in fields) or latest != result:
            raise RuntimeError('document changed during fact capture; retry capture')
        await self.snapshots.put(snapshot)
        return snapshot

    async def read_material(self, *, scope: DocumentScope, snapshot_id: str) -> DocumentSnapshot:
        return await self._read(scope, snapshot_id, MATERIAL_CONTRACT)

    async def read_facts(self, *, scope: DocumentScope, snapshot_id: str) -> DocumentSnapshot:
        return await self._read(scope, snapshot_id, FACTS_CONTRACT)

    async def read_retraction(self, *, scope: DocumentScope, snapshot_id: str) -> DocumentSnapshot:
        return await self._read(scope, snapshot_id, RETRACTION_CONTRACT)

    async def _read(self, scope, snapshot_id, contract):
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required')
        snapshot = await self.snapshots.get(scope, snapshot_id)
        if snapshot.contract_version != contract:
            raise ValueError('snapshot contract does not match the requested capability')
        return snapshot

    async def prepare_document(self, *, scope: DocumentScope, **kwargs) -> DocumentSnapshot:
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required')
        forbidden = {'user_id', 'subject_id', 'report_id', 'publish'} & kwargs.keys()
        if forbidden:
            raise ValueError('preparation identity and publication mode are fixed by the scope')
        if self.processing._document_reuse is None:
            raise RuntimeError('document reuse storage is required')
        outcome = await self.processing._document_reuse.process(
            user_id=scope.user_id,
            subject_id=scope.subject_id,
            publish=False,
            **kwargs,
        )
        return await self.capture_facts(scope=scope, document_id=outcome.document_id)

    async def project(
        self, *, scope: DocumentScope, snapshot_id: str, selection: OutputSelection, projector: DocumentFactsProjector
    ) -> DocumentProjection:
        if projector.selection != selection:
            raise ValueError('projector does not implement the exact selected output version')
        snapshot = await self.read_facts(scope=scope, snapshot_id=snapshot_id)
        return DocumentProjection(snapshot.snapshot_id, selection, canonical_json(await projector.project(snapshot)))
