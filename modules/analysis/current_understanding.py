"""One current Document preparation contract, using only public Analysis ports."""

from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from typing import Protocol

from modules.analysis.basic_info_contracts import BASIC_INFO_CONTRACT
from modules.analysis.basic_info_contracts import CLINICAL_CONTENT_CONTRACT
from modules.analysis.basic_info_contracts import clinical_content_digest
from modules.analysis.contracts import OutputSelection
from modules.analysis.preparation import DocumentPreparationBinding
from modules.analysis.preparation import payload_digest
from modules.analysis.profile_delivery import current_observations
from modules.analysis.profile_delivery import report_patients
from modules.analysis.snapshots import FACTS_CONTRACT_V2
from modules.analysis.snapshots import MATERIAL_CONTRACT
from modules.analysis.snapshots import UNDERSTANDING_CONTRACT_V2
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import canonical_json
from modules.analysis.understanding import DocumentPreparationReceipt
from modules.analysis.understanding import DocumentPreparedBundle
from modules.analysis.understanding import DocumentSnapshotChangedError
from modules.analysis.understanding import DocumentUnderstandingUnavailableError


def _identity(value):
    if not isinstance(value, str) or not value.strip() or value.strip() != value:
        raise ValueError('an exact actual preparation identity is required')
    return value


def _selection(value):
    if not isinstance(value, OutputSelection):
        raise ValueError('the actual Analysis output selection is required')
    for field in asdict(value).values():
        _identity(field)
    return value


def _admission_quality(interpretation):
    """A current Document admission view, never an inferred successful AI extraction.

    The original processing object, including missing/null, remains in the fixed
    interpretation. The quality describes this round and never judges an observation.
    """
    original = interpretation.get('processing')
    if (
        isinstance(original, dict)
        and isinstance(original.get('status'), str)
        and original['status'] in {'complete', 'partial', 'unavailable'}
    ):
        return deepcopy(original)
    return {'status': 'unavailable', 'reasonCode': 'document.clinical_quality_unavailable'}


@dataclass(frozen=True, slots=True)
class DocumentPreparedInput:
    material_snapshot_ref: str
    facts_snapshot_ref: str
    understanding_snapshot_ref: str
    selection: OutputSelection
    preparation_receipt: DocumentPreparationReceipt

    def __post_init__(self):
        for ref in (self.material_snapshot_ref, self.facts_snapshot_ref, self.understanding_snapshot_ref):
            _identity(ref)
        _selection(self.selection)
        if not isinstance(self.preparation_receipt, DocumentPreparationReceipt):
            raise TypeError('the original preparation receipt is required')

    def as_dict(self):
        return {
            'materialSnapshotRef': self.material_snapshot_ref,
            'factsSnapshotRef': self.facts_snapshot_ref,
            'understandingSnapshotRef': self.understanding_snapshot_ref,
            'selection': asdict(self.selection),
            'preparationReceipt': asdict(self.preparation_receipt),
        }

    @classmethod
    def from_dict(cls, value):
        if set(value) != {
            'materialSnapshotRef',
            'factsSnapshotRef',
            'understandingSnapshotRef',
            'selection',
            'preparationReceipt',
        }:
            raise ValueError('the exact fixed preparation input is required')
        return cls(
            value['materialSnapshotRef'],
            value['factsSnapshotRef'],
            value['understandingSnapshotRef'],
            OutputSelection(**value['selection']),
            DocumentPreparationReceipt(**value['preparationReceipt']),
        )


@dataclass(frozen=True, slots=True)
class DocumentCurrentBundle(DocumentPreparedBundle):
    facts_contract = FACTS_CONTRACT_V2
    understanding_contract = UNDERSTANDING_CONTRACT_V2

    def __post_init__(self):
        super(DocumentCurrentBundle, self).__post_init__()
        facts, understanding = self.facts.payload, self.understanding.payload
        _selection(OutputSelection(**understanding['selection']))
        _identity(facts.get('extractorRevision'))
        _identity(facts.get('validationRevision'))
        raw = understanding['interpretation']
        quality = _admission_quality(raw)
        fields, slots = report_patients(
            raw, self.material.payload['parsedDocument'].get('text'), self.material.payload['documentId']
        )
        if (
            payload_digest(self.material.payload['parsedDocument']) != self.material.payload.get('parseContentDigest')
            or not isinstance(facts.get('interpretationRound'), int)
            or isinstance(facts['interpretationRound'], bool)
            or facts['interpretationRound'] < 1
            or not isinstance(facts.get('validationRevision'), str)
            or not facts['validationRevision'].strip()
            or understanding['interpretationRevision'] != facts['extractorRevision']
            or understanding['validationRevision'] != facts['validationRevision']
            or understanding['interpretationRound'] != facts['interpretationRound']
            or facts.get('observations') != current_observations(raw, slots)
            or facts.get('basicInfoContract') != BASIC_INFO_CONTRACT
            or facts.get('basicInfo') != fields
            or facts.get('basicInfoMeta') != raw['basicInfoMeta']
            or facts.get('quality') != quality
            or facts.get('clinicalAccepted') != (quality['status'] == 'complete')
            or facts.get('clinicalContentContract') != CLINICAL_CONTENT_CONTRACT
            or facts.get('clinicalContentDigest') != clinical_content_digest(raw, facts['observations'])
        ):
            raise ValueError('current facts must preserve the actual binding and independent admission decisions')

    def as_dict(self):
        return {
            'scope': self.material.scope.as_dict(),
            'selection': self.understanding.payload['selection'],
            'interpretationRound': self.facts.payload['interpretationRound'],
            'interpretationRevision': self.facts.payload['extractorRevision'],
            'materialSnapshotRef': self.material.snapshot_id,
            'factsSnapshotRef': self.facts.snapshot_id,
            'understandingSnapshotRef': self.understanding.snapshot_id,
            'preparationReceipt': asdict(self.receipt) if self.receipt else None,
        }


class DocumentPreparationPort(Protocol):
    async def prepare_document(self, *, user_id: str, subject_id: str, **file_kwargs):
        """Prepare without publishing; return the actual accepted fixed binding."""
        ...

    async def read_prepared_round(self, *, user_id: str, subject_id: str, document_id: str, interpretation_round: int):
        """Read one immutable public PreparedDocumentRound; never execute or use latest instead."""
        ...


class DocumentCurrentUnderstandingService:
    def __init__(self, *, snapshots, preparation: DocumentPreparationPort):
        self.snapshots, self.preparation = snapshots, preparation

    async def prepare_bundle(self, *, scope: DocumentScope, **kwargs):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        if {
            'user_id',
            'subject_id',
            'report_id',
            'publish',
            'interpretation_revision',
            'output_selection',
            'selection',
        } & kwargs.keys():
            raise ValueError('scope, publication mode and execution choice are owned by their providers')
        outcome = await self.preparation.prepare_document(user_id=scope.user_id, subject_id=scope.subject_id, **kwargs)
        binding = getattr(outcome, 'preparation_binding', None)
        if not isinstance(binding, DocumentPreparationBinding):
            raise DocumentUnderstandingUnavailableError('the actual accepted round binding is unavailable')
        bundle = await self.capture_bundle(
            scope=scope,
            document_id=outcome.document_id,
            expected_binding=binding,
            expected_selection=outcome.output_selection,
        )
        return replace(
            bundle, receipt=DocumentPreparationReceipt(outcome.document_id, outcome.trace_id, outcome.reused)
        )

    async def capture_bundle(self, *, scope, document_id, expected_binding, expected_selection=None):
        return await self.capture_round_bundle(
            scope=scope,
            document_id=document_id,
            interpretation_round=expected_binding.input_snapshot.interpretation_round,
            expected_binding=expected_binding,
            expected_selection=expected_selection,
        )

    async def capture_round_bundle(
        self, *, scope, document_id, interpretation_round, expected_binding=None, expected_selection=None
    ):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        if type(interpretation_round) is not int or interpretation_round < 1:
            raise ValueError('a fixed positive interpretation round is required')
        fixed = await self.preparation.read_prepared_round(
            user_id=scope.user_id,
            subject_id=scope.subject_id,
            document_id=document_id,
            interpretation_round=interpretation_round,
        )
        binding = fixed.preparation_binding
        if (
            not isinstance(binding, DocumentPreparationBinding)
            or fixed.user_id != scope.user_id
            or fixed.subject_id != scope.subject_id
            or fixed.document_id != document_id
            or binding.metadata['documentId'] != document_id
            or binding.input_snapshot.interpretation_round != interpretation_round
            or (expected_binding is not None and binding != expected_binding)
            or (expected_selection is not None and fixed.output_selection != expected_selection)
        ):
            raise DocumentSnapshotChangedError('the fixed prepared round identity differs')
        selection = _selection(fixed.output_selection)
        inputs = binding.input_snapshot
        if (
            payload_digest(fixed.parsed_document) != binding.parse_digest
            or payload_digest(fixed.interpretation) != binding.interpretation_digest
        ):
            raise DocumentSnapshotChangedError('the fixed preparation payload or actual execution differs')
        for value in (
            inputs.interpretation_revision,
            inputs.validation_revision,
            inputs.parse_artifact_ref,
            inputs.interpretation_artifact_ref,
        ):
            _identity(value)
        parsed, interpretation, metadata = fixed.parsed_document, fixed.interpretation, binding.metadata
        if not isinstance(parsed.get('text'), str):
            raise DocumentUnderstandingUnavailableError('the original full text is required for evidence')
        material = DocumentSnapshot(
            scope,
            MATERIAL_CONTRACT,
            _identity(metadata['canonicalReportId']),
            1,
            canonical_json(
                {
                    'documentId': document_id,
                    'contentSha256': metadata['contentSha256'],
                    'fileName': metadata['fileName'],
                    'fileType': metadata['fileType'],
                    'parserRevision': metadata['parserRevision'],
                    'originalArtifactRef': metadata['originalArtifactRef'],
                    'parseArtifactRef': inputs.parse_artifact_ref,
                    'parseContentDigest': binding.parse_digest,
                    'parsedDocument': parsed,
                    'documentMetadata': metadata,
                }
            ),
        )
        fields, slots = report_patients(interpretation, parsed['text'], document_id)
        observations = current_observations(interpretation, slots)
        quality = _admission_quality(interpretation)
        facts = DocumentSnapshot(
            scope,
            FACTS_CONTRACT_V2,
            material.source_id,
            1,
            canonical_json(
                {
                    'documentId': document_id,
                    'materialSnapshotRef': material.snapshot_id,
                    'extractorRevision': inputs.interpretation_revision,
                    'validationRevision': inputs.validation_revision,
                    'interpretationRound': inputs.interpretation_round,
                    'interpretationArtifactRef': inputs.interpretation_artifact_ref,
                    'quality': quality,
                    'observations': observations,
                    'interpretationContentDigest': binding.interpretation_digest,
                    'basicInfoContract': BASIC_INFO_CONTRACT,
                    'basicInfo': fields,
                    'basicInfoMeta': interpretation['basicInfoMeta'],
                    'clinicalAccepted': quality.get('status') == 'complete',
                    'clinicalContentContract': CLINICAL_CONTENT_CONTRACT,
                    'clinicalContentDigest': clinical_content_digest(interpretation, observations),
                }
            ),
        )
        understanding = DocumentSnapshot(
            scope,
            UNDERSTANDING_CONTRACT_V2,
            material.source_id,
            1,
            canonical_json(
                {
                    'selection': asdict(selection),
                    'classification': 'derived_interpretation',
                    'factsSnapshotRef': facts.snapshot_id,
                    'materialSnapshotRef': material.snapshot_id,
                    'documentMetadata': metadata,
                    'interpretationArtifactRef': inputs.interpretation_artifact_ref,
                    'interpretationRevision': inputs.interpretation_revision,
                    'validationRevision': inputs.validation_revision,
                    'interpretationRound': inputs.interpretation_round,
                    'interpretationContentDigest': binding.interpretation_digest,
                    'quality': quality,
                    'interpretation': interpretation,
                }
            ),
        )
        bundle = DocumentCurrentBundle(material, facts, understanding)
        for snapshot in (material, facts, understanding):
            await self.snapshots.put(snapshot)
        return bundle

    async def _read(self, scope, snapshot_id, contract, *, memo=None):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        key = ('snapshot', scope, snapshot_id)
        snapshot = memo.get(key) if memo is not None else None
        if snapshot is None:
            snapshot = await self.snapshots.get(scope, snapshot_id)
        if snapshot.contract_version != contract:
            raise ValueError('snapshot does not use the current Document contract')
        if memo is not None:
            memo[key] = snapshot
        return snapshot

    async def read_material(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, MATERIAL_CONTRACT)

    async def read_facts(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, FACTS_CONTRACT_V2)

    async def read_understanding(self, *, scope, snapshot_id):
        return await self._read(scope, snapshot_id, UNDERSTANDING_CONTRACT_V2)

    async def read_bundle(self, *, scope, understanding_snapshot_id):
        return await self._read_bundle(scope, understanding_snapshot_id)

    async def _read_bundle(self, scope, understanding_snapshot_id, *, memo=None):
        understanding = await self._read(scope, understanding_snapshot_id, UNDERSTANDING_CONTRACT_V2, memo=memo)
        payload = understanding.payload
        material = await self._read(scope, payload['materialSnapshotRef'], MATERIAL_CONTRACT, memo=memo)
        facts = await self._read(scope, payload['factsSnapshotRef'], FACTS_CONTRACT_V2, memo=memo)
        return DocumentCurrentBundle(material, facts, understanding)
