"""Versioned derived interpretation bridge for behavior-preserving consumers.

The full interpretation is not a Profile fact set. Only the separately referenced
Facts snapshot is eligible for DocumentPublisher.
"""

from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from typing import ClassVar

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.preparation import DocumentPreparationBinding
from modules.analysis.preparation import payload_digest
from modules.analysis.snapshots import FACTS_CONTRACT
from modules.analysis.snapshots import MATERIAL_CONTRACT
from modules.analysis.snapshots import UNDERSTANDING_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import DocumentSnapshotService
from modules.analysis.snapshots import _observations
from modules.analysis.snapshots import canonical_json

UNDERSTANDING_SELECTION = OutputSelection('legacy_understanding', '1', 'legacy-understanding-v1')


class DocumentSnapshotChangedError(RuntimeError):
    code = 'document.snapshot_changed'
    retryable = True


class DocumentUnderstandingUnavailableError(DocumentNotFoundError):
    code = 'document.understanding_unavailable'
    retryable = False


@dataclass(frozen=True, slots=True)
class DocumentPreparationReceipt:
    """Request-local outcome; never part of a snapshot's content identity."""

    document_id: str
    trace_id: str
    reused: bool

    def __post_init__(self):
        if not all(isinstance(value, str) and value.strip() for value in (self.document_id, self.trace_id)):
            raise ValueError('preparation receipt requires actual document and request trace identities')
        if type(self.reused) is not bool:
            raise TypeError('preparation receipt reused must be a boolean')


@dataclass(frozen=True, slots=True)
class DocumentPreparedBundle:
    facts_contract: ClassVar[str] = FACTS_CONTRACT
    understanding_contract: ClassVar[str] = UNDERSTANDING_CONTRACT

    material: DocumentSnapshot
    facts: DocumentSnapshot
    understanding: DocumentSnapshot
    receipt: DocumentPreparationReceipt | None = None

    def __post_init__(self):
        if (
            self.material.contract_version != MATERIAL_CONTRACT
            or self.facts.contract_version != self.facts_contract
            or self.understanding.contract_version != self.understanding_contract
            or len(
                {
                    (item.scope, item.source_id, item.source_revision)
                    for item in (self.material, self.facts, self.understanding)
                }
            )
            != 1
            or self.facts.payload.get('materialSnapshotRef') != self.material.snapshot_id
            or self.understanding.payload.get('materialSnapshotRef') != self.material.snapshot_id
            or self.understanding.payload.get('factsSnapshotRef') != self.facts.snapshot_id
            or self.facts.payload.get('documentId') != self.material.payload.get('documentId')
            or self.understanding.payload.get('quality') != self.facts.payload.get('quality')
            or self.understanding.payload.get('interpretationContentDigest')
            != self.facts.payload.get('interpretationContentDigest')
            or payload_digest(self.understanding.payload.get('interpretation'))
            != self.facts.payload.get('interpretationContentDigest')
        ):
            raise ValueError('bundle snapshots must share one fixed source and input binding')
        if self.receipt is not None and (
            not isinstance(self.receipt, DocumentPreparationReceipt)
            or self.receipt.document_id != self.material.payload.get('documentId')
        ):
            raise ValueError('preparation receipt must identify the bundled document')


class DocumentUnderstandingService(DocumentSnapshotService):
    """Explicit capture/prepare commands and pure immutable reads for the bridge."""

    bundle_type = DocumentPreparedBundle
    understanding_contract = UNDERSTANDING_CONTRACT

    @staticmethod
    def _selection(selection):
        if selection != UNDERSTANDING_SELECTION:
            raise UnsupportedOutputVersionError('the exact understanding bridge implementation is unavailable')

    async def _inputs(self, scope, document_id):
        reuse, document = await self._stored(scope, document_id)
        document = deepcopy(document)
        if document.get('status') != 'completed':
            raise DocumentUnderstandingUnavailableError('a settled interpretation is required')
        parsed = deepcopy(await reuse._payload(document, 'docmind_result'))
        interpretation = deepcopy(await reuse._payload(document, 'interpretation'))
        if not isinstance(parsed, dict) or not isinstance(interpretation, dict):
            raise DocumentUnderstandingUnavailableError('fixed parser/interpretation content is unavailable')
        return document, parsed, interpretation

    @staticmethod
    def _material(scope, binding, parsed):
        metadata = binding.metadata
        return DocumentSnapshot(
            scope,
            MATERIAL_CONTRACT,
            metadata['canonicalReportId'],
            1,
            canonical_json(
                {
                    'documentId': metadata['documentId'],
                    'contentSha256': metadata['contentSha256'],
                    'fileName': metadata['fileName'],
                    'fileType': metadata['fileType'],
                    'parserRevision': metadata['parserRevision'],
                    'originalArtifactRef': metadata['originalArtifactRef'],
                    'parseArtifactRef': binding.input_snapshot.parse_artifact_ref,
                    'parsedDocument': parsed,
                    'documentMetadata': metadata,
                }
            ),
        )

    @staticmethod
    def _facts(scope, binding, material, interpretation):
        inputs = binding.input_snapshot
        return DocumentSnapshot(
            scope,
            FACTS_CONTRACT,
            material.source_id,
            material.source_revision,
            canonical_json(
                {
                    'documentId': binding.metadata['documentId'],
                    'materialSnapshotRef': material.snapshot_id,
                    'extractorRevision': inputs.interpretation_revision,
                    'validationRevision': inputs.validation_revision,
                    'interpretationRound': inputs.interpretation_round,
                    'interpretationArtifactRef': inputs.interpretation_artifact_ref,
                    'legacySourceRevision': binding.metadata['legacySourceRevision'],
                    'quality': interpretation.get('processing', {'status': 'complete'}),
                    'observations': _observations(interpretation),
                    # Provenance, not consumer-specific interpretation fields.
                    'interpretationContentDigest': binding.interpretation_digest,
                }
            ),
        )

    @staticmethod
    def _understanding(facts, material, interpretation, selection):
        content = facts.payload
        if (
            not content.get('interpretationContentDigest')
            or 'documentMetadata' not in material.payload
            or payload_digest(interpretation) != content['interpretationContentDigest']
        ):
            raise DocumentUnderstandingUnavailableError('fixed interpretation identity is missing or differs')
        if _observations(interpretation) != content['observations']:
            raise DocumentUnderstandingUnavailableError('interpretation does not match the selected facts')
        if interpretation.get('processing', {'status': 'complete'}) != content['quality']:
            raise DocumentUnderstandingUnavailableError('interpretation does not match the selected quality')
        return DocumentSnapshot(
            facts.scope,
            UNDERSTANDING_CONTRACT,
            facts.source_id,
            facts.source_revision,
            canonical_json(
                {
                    'selection': asdict(selection),
                    'classification': 'derived_interpretation',
                    'factsSnapshotRef': facts.snapshot_id,
                    'materialSnapshotRef': material.snapshot_id,
                    'documentMetadata': material.payload['documentMetadata'],
                    'interpretationArtifactRef': content['interpretationArtifactRef'],
                    'interpretationRevision': content['extractorRevision'],
                    'validationRevision': content['validationRevision'],
                    'interpretationRound': content['interpretationRound'],
                    'interpretationContentDigest': content['interpretationContentDigest'],
                    'inputVersionStatus': 'known'
                    if content['extractorRevision'] and content['validationRevision']
                    else 'legacy_unversioned',
                    'quality': content['quality'],
                    'interpretation': interpretation,
                }
            ),
        )

    async def capture_bundle(
        self,
        *,
        scope: DocumentScope,
        document_id: str,
        selection: OutputSelection,
        expected_binding: DocumentPreparationBinding | None = None,
    ) -> DocumentPreparedBundle:
        self._selection(selection)
        try:
            document, parsed, interpretation = await self._inputs(scope, document_id)
        except DocumentUnderstandingUnavailableError as error:
            if expected_binding is not None:
                raise DocumentSnapshotChangedError('document changed after preparation') from error
            raise
        binding = DocumentPreparationBinding.capture(document, parsed, interpretation)
        if expected_binding is not None and binding != expected_binding:
            raise DocumentSnapshotChangedError('document changed after preparation; do not substitute a newer input')
        material = self._material(scope, binding, parsed)
        facts = self._facts(scope, binding, material, interpretation)
        understanding = self._understanding(facts, material, interpretation, selection)
        try:
            current, current_parsed, current_interpretation = await self._inputs(scope, document_id)
        except DocumentUnderstandingUnavailableError as error:
            raise DocumentSnapshotChangedError('document changed during capture') from error
        if DocumentPreparationBinding.capture(current, current_parsed, current_interpretation) != binding:
            raise DocumentSnapshotChangedError('document changed during capture; retry the explicit capture command')
        bundle = self.bundle_type(material, facts, understanding)
        for snapshot in (material, facts, understanding):
            await self.snapshots.put(snapshot)
        return bundle

    async def prepare_bundle(self, *, scope: DocumentScope, selection: OutputSelection, **kwargs):
        self._selection(selection)
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required')
        if {'user_id', 'subject_id', 'report_id', 'publish'} & kwargs.keys():
            raise ValueError('preparation identity and publication mode are fixed by the scope')
        reuse = self.processing._document_reuse
        if reuse is None:
            raise DocumentUnderstandingUnavailableError('document reuse storage is required')
        outcome = await reuse.process(user_id=scope.user_id, subject_id=scope.subject_id, publish=False, **kwargs)
        if not isinstance(getattr(outcome, 'preparation_binding', None), DocumentPreparationBinding):
            raise DocumentUnderstandingUnavailableError('processor did not freeze its actual input binding')
        receipt = DocumentPreparationReceipt(outcome.document_id, outcome.trace_id, outcome.reused)
        bundle = await self.capture_bundle(
            scope=scope,
            document_id=outcome.document_id,
            selection=selection,
            expected_binding=outcome.preparation_binding,
        )
        return replace(bundle, receipt=receipt)

    async def capture_round_bundle(
        self, *, scope: DocumentScope, document_id: str, interpretation_round: int, selection=UNDERSTANDING_SELECTION
    ):
        """Explicit capture from one durable round, independent of the latest index state."""
        self._selection(selection)
        if not isinstance(scope, DocumentScope):
            raise TypeError('DocumentScope is required')
        reuse = self.processing._document_reuse
        document = await reuse.index.get_archived_round(
            document_id, scope.user_id, scope.subject_id, interpretation_round
        )
        if document is None:
            raise DocumentNotFoundError('the fixed document round is unavailable')
        if (
            document.get('_id') != document_id
            or document.get('userId') != scope.user_id
            or document.get('subjectId') != scope.subject_id
            or document.get('interpretationResultRound') != interpretation_round
        ):
            raise DocumentSnapshotChangedError('the archived document round identity differs')
        parsed = await reuse._payload(document, 'docmind_result')
        interpretation = await reuse._payload(document, 'interpretation')
        if (
            payload_digest(parsed) != document['parseDigest']
            or payload_digest(interpretation) != document['interpretationDigest']
        ):
            raise DocumentSnapshotChangedError('the fixed document round archive differs')
        binding = DocumentPreparationBinding.capture(document, parsed, interpretation)
        material = self._material(scope, binding, parsed)
        facts = self._facts(scope, binding, material, interpretation)
        understanding = self._understanding(facts, material, interpretation, selection)
        if (
            await reuse.index.get_archived_round(document_id, scope.user_id, scope.subject_id, interpretation_round)
            != document
        ):
            raise DocumentSnapshotChangedError('the fixed document round changed during capture')
        bundle = self.bundle_type(material, facts, understanding)
        for snapshot in (material, facts, understanding):
            await self.snapshots.put(snapshot)
        return bundle

    async def capture_understanding(self, *, scope, facts_snapshot_id, selection):
        self._selection(selection)
        facts = await self.read_facts(scope=scope, snapshot_id=facts_snapshot_id)
        content = facts.payload
        if not content.get('interpretationContentDigest'):
            raise DocumentUnderstandingUnavailableError('legacy facts lack a fixed full interpretation digest')
        material = await self.read_material(scope=scope, snapshot_id=content['materialSnapshotRef'])
        # Read the selected archive reference, never the current document index.
        interpretation = await self.processing._document_trace_store.read_payload(
            content['interpretationArtifactRef'], scope.user_id, scope.subject_id, 'interpretation'
        )
        if not isinstance(interpretation, dict):
            raise DocumentUnderstandingUnavailableError('selected interpretation archive is unavailable')
        snapshot = self._understanding(facts, material, interpretation, selection)
        self.bundle_type(material, facts, snapshot)
        await self.snapshots.put(snapshot)
        return snapshot

    async def read_understanding(self, *, scope, snapshot_id, selection):
        self._selection(selection)
        snapshot = await self._read(scope, snapshot_id, self.understanding_contract)
        if snapshot.payload.get('selection') != asdict(selection):
            raise UnsupportedOutputVersionError('snapshot does not match the selected understanding bridge')
        return snapshot

    async def read_bundle(self, *, scope, understanding_snapshot_id, selection):
        understanding = await self.read_understanding(
            scope=scope, snapshot_id=understanding_snapshot_id, selection=selection
        )
        material = await self.read_material(scope=scope, snapshot_id=understanding.payload['materialSnapshotRef'])
        facts = await self.read_facts(scope=scope, snapshot_id=understanding.payload['factsSnapshotRef'])
        return self.bundle_type(material, facts, understanding)
