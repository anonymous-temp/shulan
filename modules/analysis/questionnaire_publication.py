"""Publish committed Q attachments without reinterpreting or generating material."""

import hashlib
from copy import deepcopy
from uuid import NAMESPACE_URL
from uuid import uuid5

from modules.analysis.compatibility import _legacy_revision
from modules.analysis.questionnaire_publication_contracts import QUESTIONNAIRE_COMMAND_CONTRACT
from modules.analysis.questionnaire_publication_contracts import QUESTIONNAIRE_RECEIPT_CONTRACT
from modules.analysis.questionnaire_publication_contracts import SUBMITTED_SELECTION
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationConflict
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationRequest
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationUnavailable
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from modules.health_profile.contracts import ProfileFreshSourceStateV1
from modules.health_profile.contracts import ProfileSourceEventV1


def _require(value, message):
    if not value:
        raise QuestionnairePublicationConflict(message)


def _identity(scope, content_key):
    fingerprint = hashlib.sha256(f'{scope.user_id}:{scope.subject_id}:{content_key}'.encode()).hexdigest()
    return str(uuid5(NAMESPACE_URL, 'questionnaire-report:' + fingerprint)), fingerprint


class DocumentQuestionnaireAttachmentPublisher:
    """Durable per-item ACKs; the Q owner worker supplies attempts/deadline and recovery.

    Only public Q readers and public Profile ready lookup are injected. A real
    LegacyPublicationGateway preserves the original health_report queue effects.
    """

    def __init__(self, *, submitted_reader, source_reader, ready_reader, legacy_publication, records):
        if legacy_publication.ledger is None:
            raise ValueError('Questionnaire attachment writes require the shared formal source gate')
        self.submitted_reader, self.source_reader = submitted_reader, source_reader
        self.ready_reader, self.legacy_publication, self.records = ready_reader, legacy_publication, records

    async def _materials(self, request):
        submitted = await self.submitted_reader.read_snapshot(
            deepcopy(request.submitted_reference), user_id=request.scope.user_id
        )
        _require(
            submitted['reference'] == request.submitted_reference
            and submitted['contentDigest'] == request.submitted_digest
            and digest(submitted['payload']) == request.submitted_digest,
            'the fixed submitted Questionnaire material changed',
        )
        payload, event = submitted['payload'], ProfileSourceEventV1.model_validate(request.questionnaire_event)
        proof = payload['submissionProof']
        _require(
            payload.get('provider') == 'questionnaire'
            and all(payload.get(k) == v for k, v in SUBMITTED_SELECTION.items())
            and proof.get('contractVersion') == 'questionnaire-submission-proof.v1'
            and proof.get('status') == 'submitted'
            and proof.get('deliveryMode') == 'profile-source-event.v1'
            and proof.get('userId') == request.scope.user_id
            and proof.get('questionnaireId') == event.source_id
            and proof.get('submissionId') == event.source_id
            and type(proof.get('sourceRevision')) is int
            and proof['sourceRevision'] == 1,
            'only exact committed new-mode Questionnaire sources may publish attachments',
        )
        raw = await self.source_reader.read_source_snapshot(event.snapshot_ref, scope=event.scope)
        _require(
            isinstance(raw, dict)
            and digest(raw) == event.content_digest
            and raw.get('contractVersion') == event.snapshot_ref.contract_version
            and raw.get('scope') == request.scope.as_dict()
            and raw.get('sourceType') == 'questionnaire'
            and raw.get('sourceId') == event.source_id
            and type(raw.get('sourceRevision')) is int
            and raw['sourceRevision'] == 1
            and raw.get('status') == proof['status']
            and raw.get('sessionId') == proof['sessionId']
            and raw.get('submittedAt') == proof['submittedAt']
            and raw.get('submissionProof') == proof,
            'the Q raw source and submitted attachments do not prove the same immutable submission',
        )
        attachments, excluded = payload['attachments'], payload['excluded']
        _require(
            isinstance(attachments, list) and isinstance(excluded, list) and len(attachments) + len(excluded) <= 5,
            'the submitted Questionnaire attachment inventory exceeds its supported bound',
        )
        ids, content_keys, items = set(), set(), []
        for ordinal, row in enumerate(attachments):
            artifact_id, content_key, artifact = row['artifactId'], row['contentKey'], row['artifact']
            _require(
                isinstance(artifact_id, str)
                and bool(artifact_id)
                and isinstance(content_key, str)
                and bool(content_key)
                and artifact_id not in ids
                and content_key not in content_keys
                and artifact.get('artifactId') == artifact_id
                and artifact.get('questionnaireId') == event.source_id
                and artifact.get('kind') != 'document_reference'
                and content_key == (artifact.get('contentSha256') or artifact_id)
                and row.get('interpretationContractVersion') == 'questionnaire-archived-interpretation.v1'
                and isinstance(row.get('interpretation'), dict)
                and row.get('interpretationDigest') == digest(row['interpretation']),
                'fixed attachment selection or archived interpretation identity changed',
            )
            report_id, fingerprint = _identity(request.scope, content_key)
            items.append(
                {
                    'ordinal': ordinal,
                    'artifactId': artifact_id,
                    'contentKey': content_key,
                    'reportId': report_id,
                    'sourceFingerprint': fingerprint,
                    'interpretationDigest': row['interpretationDigest'],
                }
            )
            ids.add(artifact_id)
            content_keys.add(content_key)
        for row in excluded:
            _require(
                isinstance(row, dict)
                and isinstance(row.get('artifactId'), str)
                and row['artifactId'] not in ids
                and row.get('reason') in {'document_reference', 'no_interpretation', 'duplicate_content'},
                'the fixed excluded attachment inventory changed',
            )
            ids.add(row['artifactId'])
        return submitted, items

    async def prepare(
        self, *, scope, request_id, submitted_reference, submitted_digest, questionnaire_event, selection
    ):
        request = QuestionnairePublicationRequest(
            scope, request_id, submitted_reference, submitted_digest, questionnaire_event, selection
        )
        existing = await self.records.get('questionnaire_command', scope, request.command_id)
        if existing is not None:
            stored, _ = await self._command(scope, request.command_id)
            _require(stored['request'] == request.as_dict(), 'the Questionnaire command id has different inputs')
            return deepcopy(stored['prepared'])
        submitted, items = await self._materials(request)
        manifest = {'request': request.as_dict(), 'items': items, 'excluded': submitted['payload']['excluded']}
        prepared = {
            'contractVersion': QUESTIONNAIRE_COMMAND_CONTRACT,
            'commandId': request.command_id,
            'commandDigest': digest(manifest),
            'scope': scope.as_dict(),
            'questionnaireId': request.questionnaire_event['sourceId'],
            'questionnaireSourceRevision': 1,
            'status': 'prepared',
            'items': items,
            'excluded': submitted['payload']['excluded'],
        }
        await self.records.put('questionnaire_command', scope, request.command_id, {**manifest, 'prepared': prepared})
        stored, _ = await self._command(scope, request.command_id)
        return deepcopy(stored['prepared'])

    async def _command(self, scope, command_id):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an explicit fixed scope is required')
        stored = await self.records.require('questionnaire_command', scope, command_id)
        request = QuestionnairePublicationRequest.from_dict(stored['request'])
        manifest = {key: stored[key] for key in ('request', 'items', 'excluded')}
        expected = {
            'contractVersion': QUESTIONNAIRE_COMMAND_CONTRACT,
            'commandId': request.command_id,
            'commandDigest': digest(manifest),
            'scope': scope.as_dict(),
            'questionnaireId': request.questionnaire_event['sourceId'],
            'questionnaireSourceRevision': 1,
            'status': 'prepared',
            'items': stored['items'],
            'excluded': stored['excluded'],
        }
        _require(
            request.scope == scope and request.command_id == command_id and stored['prepared'] == expected,
            'the fixed Questionnaire command binding changed',
        )
        return stored, request

    async def _ready(self, request):
        event = ProfileSourceEventV1.model_validate(request.questionnaire_event)
        value = await self.ready_reader.get_cutover(event.scope, 'questionnaire', event.source_id)
        if value is None:
            raise QuestionnairePublicationUnavailable('Questionnaire fresh admission is not ready')
        state = ProfileFreshSourceStateV1.model_validate(value)
        auth = state.authorization
        if state.status != 'ready':
            raise QuestionnairePublicationUnavailable('Questionnaire fresh admission is not ready')
        _require(
            auth.admission_kind == 'fresh'
            and auth.scope == event.scope
            and auth.source_type == 'questionnaire'
            and auth.source_id == event.source_id
            and auth.material_contract_version == event.snapshot_ref.contract_version
            and auth.material_snapshot_ref == event.snapshot_ref
            and auth.material_content_digest == event.content_digest
            and auth.owner_fence_ref.provider == 'questionnaire'
            and auth.owner_fence_ref.contract_version == 'profile-owner-admission-fence.v1',
            'the actual Questionnaire ready authorization does not bind this fixed source material',
        )
        await self.records.put(
            'questionnaire_ready', request.scope, request.command_id, auth.model_dump(mode='json', by_alias=True)
        )

    @staticmethod
    def _acknowledgement(scope, item, saved):
        record = saved.record
        _require(
            record.report_id == item['reportId']
            and record.user_id == scope.user_id
            and record.subject_id == scope.subject_id
            and record.source_fingerprint == item['sourceFingerprint']
            and record.canonical_report_id in (None, item['reportId'])
            and record.document_id is None
            and isinstance(record.result, dict),
            'the actual legacy attachment acknowledgement has another source identity',
        )
        if record.ingestion_status not in {'pending', 'queued'}:
            raise QuestionnairePublicationUnavailable('the attachment legacy source is not accepted for delivery')
        _require(
            record.ingestion_status != 'queued'
            or isinstance(record.ingestion_job_id, str)
            and bool(record.ingestion_job_id),
            'queued attachment has no real job identity',
        )
        return {
            'artifactId': item['artifactId'],
            'reportId': record.report_id,
            'legacySourceRevision': _legacy_revision(record.source_revision),
            'inputInterpretationDigest': item['interpretationDigest'],
            'storedResultDigest': digest(record.result),
            'ingestionStatus': record.ingestion_status,
            'ingestionJobId': record.ingestion_job_id,
        }

    async def publish(self, *, scope, command_id):
        completed = await self.read_receipt(scope=scope, command_id=command_id)
        if completed is not None:
            return completed
        stored, request = await self._command(scope, command_id)
        await self._ready(request)
        submitted, items = await self._materials(request)
        _require(
            items == stored['items'] and submitted['payload']['excluded'] == stored['excluded'],
            'the full fixed attachment inventory changed before publication',
        )
        receipts = []
        # Validate every material above before the first formal save. Preserve order.
        for item, material in zip(items, submitted['payload']['attachments']):
            key = command_id + ':' + str(item['ordinal'])
            acknowledgement = await self.records.get('questionnaire_attachment', scope, key)
            if acknowledgement is None:
                saved = await self.legacy_publication.save(
                    report_id=item['reportId'],
                    user_id=scope.user_id,
                    subject_id=scope.subject_id,
                    source_fingerprint=item['sourceFingerprint'],
                    result=deepcopy(material['interpretation']),
                )
                acknowledgement = self._acknowledgement(scope, item, saved)
                acknowledgement = await self.records.put_acknowledgement(scope, key, acknowledgement)
            self._validate_ack(item, acknowledgement)
            receipts.append(acknowledgement)
        result = {
            'contractVersion': QUESTIONNAIRE_RECEIPT_CONTRACT,
            'commandId': command_id,
            'commandDigest': stored['prepared']['commandDigest'],
            'status': 'accepted',
            'items': receipts,
            'excluded': stored['excluded'],
        }
        await self.records.put('questionnaire_receipt', scope, command_id, result)
        return await self.read_receipt(scope=scope, command_id=command_id)

    @staticmethod
    def _validate_ack(item, receipt):
        _require(
            receipt['artifactId'] == item['artifactId']
            and receipt['reportId'] == item['reportId']
            and receipt['inputInterpretationDigest'] == item['interpretationDigest']
            and receipt['ingestionStatus'] in {'pending', 'queued'}
            and (
                receipt['ingestionStatus'] != 'queued'
                or isinstance(receipt['ingestionJobId'], str)
                and bool(receipt['ingestionJobId'])
            )
            and isinstance(receipt['storedResultDigest'], str)
            and len(receipt['storedResultDigest']) == 64
            and all(char in '0123456789abcdef' for char in receipt['storedResultDigest']),
            'the persisted attachment acknowledgement differs from its fixed input',
        )
        _legacy_revision(receipt['legacySourceRevision'])

    async def read_receipt(self, *, scope, command_id):
        stored, _ = await self._command(scope, command_id)
        result = await self.records.get('questionnaire_receipt', scope, command_id)
        if result is None:
            return None
        _require(
            result['contractVersion'] == QUESTIONNAIRE_RECEIPT_CONTRACT
            and result['commandId'] == command_id
            and result['commandDigest'] == stored['prepared']['commandDigest']
            and result['status'] == 'accepted'
            and result['excluded'] == stored['excluded']
            and len(result['items']) == len(stored['items']),
            'the fixed Questionnaire publication receipt changed',
        )
        for item, acknowledgement in zip(stored['items'], result['items']):
            self._validate_ack(item, acknowledgement)
            actual = await self.records.require(
                'questionnaire_attachment', scope, command_id + ':' + str(item['ordinal'])
            )
            _require(
                actual == acknowledgement, 'the attachment receipt differs from its durable original acknowledgement'
            )
        return deepcopy(result)
