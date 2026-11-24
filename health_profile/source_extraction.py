"""Fixed Profile extraction with durable per-call output and candidate checkpoints."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from copy import deepcopy
from dataclasses import asdict

from agents import AgentHub
from agents import AgentResult
from agents import AgentSelection
from agents.errors import AgentCapabilityNotFoundError
from agents.graphs.health_profile import PROFILE_CHAT_EXTRACTION_CAPABILITY
from agents.graphs.health_profile import PROFILE_RECORD_EXTRACTION_CAPABILITY

from .chat_ingestion import ChatMessageCandidateExtractor
from .chat_source import ChatProfileSourceRecord
from .chat_source import _message_time
from .chat_source import _required_text
from .contracts import profile_content_digest
from .ingestion import FactTypeRegistry
from .models import MemoryCandidate
from .models import ProfileIngestionSourceType
from .processing import ProfileIngestionProcessingError
from .record_ingestion import RecordAnswerCandidateExtractor
from .record_ingestion import RecordBasicInfoMapper
from .record_ingestion import RecordSubjectResolver
from .record_source import RecordProfileSourceRecord
from .source_contracts import ProfileChatSourceSnapshotV1
from .source_contracts import ProfileChatSourceSnapshotV2
from .source_contracts import ProfileQuestionnaireSourceSnapshotV2
from .source_contracts import ProfileRecordSourceSnapshotV1
from .source_contracts import ProfileRecordSourceSnapshotV2
from .source_contracts import parse_profile_source_snapshot
from .source_material import ProfileSourceApplicationError
from .source_material import ProfileSourceMaterial
from .source_publications import ProfileSourceConflictError
from .source_publications import ProfileSourceLeaseError

CHAT_EXTRACTION = AgentSelection(PROFILE_CHAT_EXTRACTION_CAPABILITY, 'profile-chat-extraction-v3')
ANSWER_EXTRACTION = AgentSelection(PROFILE_RECORD_EXTRACTION_CAPABILITY, 'profile-record-extraction-v4')


class ProfileUserSourceResolver:
    """The source owner's public reader returns a complete immutable typed snapshot."""

    def __init__(self, reader):
        self.reader = reader

    async def resolve(self, event):
        raw = await self.reader.read_source_snapshot(event.snapshot_ref, scope=event.scope)
        snapshot = parse_profile_source_snapshot(raw)
        if snapshot.contract_version != event.snapshot_ref.contract_version:
            raise ProfileSourceApplicationError('source.material_contract_mismatch')
        return ProfileSourceMaterial(
            scope=snapshot.scope,
            source_type=snapshot.source_type,
            source_id=snapshot.source_id,
            source_revision=snapshot.source_revision,
            snapshot=snapshot.model_dump(mode='json', by_alias=True),
        )


class DurableProfileSourceExtractor:
    """Profile owns extraction selection; consumers only select the evidence contract."""

    def __init__(self, hub, *, policy_version):
        if not isinstance(hub, AgentHub):
            raise TypeError('source extraction requires an AgentHub')
        self.hub = hub
        self.policy_version = policy_version

    def processing_manifest(self, source):
        selection = CHAT_EXTRACTION if source.source_type == 'chat' else ANSWER_EXTRACTION
        return {
            'implementationRevision': (
                'profile-record-source-v2'
                if isinstance(source, ProfileRecordSourceSnapshotV2)
                else 'profile-chat-source-v2'
                if isinstance(source, ProfileChatSourceSnapshotV2)
                else 'profile-questionnaire-source-v2'
                if isinstance(source, ProfileQuestionnaireSourceSnapshotV2)
                else f'profile-{source.source_type}-source-v1'
            ),
            'materialContract': source.contract_version,
            'childSelection': asdict(selection),
            'extractorVersion': ChatMessageCandidateExtractor.VERSION
            if source.source_type == 'chat'
            else RecordAnswerCandidateExtractor.VERSION,
            'basicMapperVersion': RecordBasicInfoMapper.VERSION if source.source_type == 'record' else None,
            'subjectResolverVersion': RecordSubjectResolver.VERSION,
            'policyVersion': self.policy_version,
            'factRegistryVersion': FactTypeRegistry.VERSION,
        }

    async def extract(self, inbox, cursor, publication, event, material):
        source = parse_profile_source_snapshot(material.snapshot)
        if (
            source.scope != event.scope
            or source.source_type != event.source_type
            or source.source_id != event.source_id
            or source.source_revision != event.source_revision
        ):
            raise ProfileSourceApplicationError('source.material_scope_mismatch')
        selection = CHAT_EXTRACTION if source.source_type == 'chat' else ANSWER_EXTRACTION
        manifest = self.processing_manifest(source)
        try:
            saved = await inbox.freeze_processing(cursor, publication['_id'], manifest)
        except ProfileSourceConflictError as error:
            raise ProfileSourceApplicationError('source.processing_version_unavailable') from error
        if saved.get('extractedCandidates') is not None:
            return tuple(MemoryCandidate.model_validate(row) for row in saved['extractedCandidates'])
        hub = _DurableExtractionHub(self.hub, inbox, cursor, publication['_id'], selection)
        try:
            if isinstance(source, (ProfileChatSourceSnapshotV1, ProfileChatSourceSnapshotV2)):
                try:
                    content = (
                        _required_text('content', source.content)
                        if isinstance(source, ProfileChatSourceSnapshotV2)
                        else source.content
                    )
                    created_at = (
                        _message_time(source.timestamp)
                        if isinstance(source, ProfileChatSourceSnapshotV2)
                        else source.created_at
                    )
                except (TypeError, ValueError) as error:
                    raise ProfileSourceApplicationError('source.chat_input_invalid') from error
                record = ChatProfileSourceRecord(
                    user_id=source.scope.user_id,
                    session_id=source.session_id,
                    message_id=source.source_id,
                    source_revision=source.source_revision,
                    ingestion_status='source_managed',
                    ingestion_job_id=None,
                    content=content,
                    created_at=created_at,
                )
                candidates = await ChatMessageCandidateExtractor(hub).extract(
                    record,
                    self_subject_id=source.scope.subject_id,
                )
            else:
                session = {}
                if isinstance(source, (ProfileRecordSourceSnapshotV1, ProfileRecordSourceSnapshotV2)):
                    session = {
                        'extractedInfo': {row.field: deepcopy(row.value) for row in source.user_inputs},
                        'extractedInfoSources': {row.field: row.provenance for row in source.user_inputs},
                        'qaCompletedAt': source.completed_at,
                    }
                if isinstance(source, ProfileRecordSourceSnapshotV2):
                    session.update(
                        qaHistory=list(deepcopy(source.qa_history)),
                        healthData={
                            'hospitalData': list(deepcopy(source.hospital_data)),
                            'weRunData': list(deepcopy(source.we_run_data)),
                            'smartDeviceData': deepcopy(source.smart_device_data),
                        },
                    )
                record = RecordProfileSourceRecord(
                    user_id=source.scope.user_id,
                    session_id=source.source_id,
                    round_number=source.round_number
                    if isinstance(source, (ProfileRecordSourceSnapshotV1, ProfileRecordSourceSnapshotV2))
                    else 1,
                    source_revision=source.source_revision,
                    ingestion_status='source_managed',
                    ingestion_job_id=None,
                    session=session,
                )
                direct = RecordBasicInfoMapper().map(record, subject_id=source.scope.subject_id) if session else ()
                if isinstance(source, ProfileRecordSourceSnapshotV2):
                    basic = await inbox.save_basic_candidates(
                        cursor, publication['_id'], [row.model_dump(mode='json') for row in direct]
                    )
                    direct = tuple(MemoryCandidate.model_validate(row) for row in basic['basicCandidatesCheckpoint'])
                    extracted = await RecordAnswerCandidateExtractor(hub).extract(
                        record, subject_id=source.scope.subject_id
                    )
                else:
                    rows = []
                    for answer in source.answers:
                        row = answer.model_dump(by_alias=True, exclude={'provided'})
                        kind = 'qa_answer' if source.source_type == 'record' else 'questionnaire_answer'
                        if isinstance(source, ProfileQuestionnaireSourceSnapshotV2):
                            # Retain the exact legacy answer_rows key order sent to the model.
                            row = {'answerIndex': row.pop('answerIndex'), 'sourceKind': kind, **row}
                        else:
                            row['sourceKind'] = kind
                        rows.append(row)
                    extracted = await RecordAnswerCandidateExtractor(
                        hub, source_type=ProfileIngestionSourceType(source.source_type)
                    ).extract_answer_rows(record, rows, subject_id=source.scope.subject_id)
                candidates = (*direct, *extracted)
        except ProfileIngestionProcessingError as error:
            # Legacy extractors wrap all hub errors. Preserve publication ownership
            # and version errors so a stale worker never updates another receipt.
            cause = error.__cause__
            if isinstance(cause, (ProfileSourceLeaseError, ProfileSourceApplicationError)):
                raise cause from None
            if isinstance(cause, AgentCapabilityNotFoundError):
                raise ProfileSourceApplicationError('source.extraction_version_unavailable') from error
            raise ProfileSourceApplicationError(
                error.error_code,
                retryable=error.error_code == 'extractor.agent_failure',
            ) from error
        saved = await inbox.save_extracted_candidates(
            cursor,
            publication['_id'],
            [row.model_dump(mode='json') for row in candidates],
        )
        return tuple(MemoryCandidate.model_validate(row) for row in saved['extractedCandidates'])


class _DurableExtractionHub(AgentHub):
    """A fixed, source-scoped adapter; never grants extraction a runtime registry."""

    def __init__(self, delegate, inbox, cursor, publication_id, selection):
        self.delegate, self.inbox, self.cursor = delegate, inbox, cursor
        self.publication_id, self.selection = publication_id, selection

    async def invoke(self, capability, request, context, *, thread_id=None, selection=None):
        if capability != self.selection.capability or selection not in (None, self.selection):
            raise ProfileSourceApplicationError('source.extraction_capability_mismatch')
        key = profile_content_digest(
            {
                'selection': asdict(self.selection),
                'payload': dict(request.payload),
                'userId': context.user_id,
                'sessionId': context.session_id,
                'requestId': context.request_id,
            }
        )
        publication = await self.inbox.store.get(self.publication_id)
        cached = publication.get('extractionCalls', {}).get(key)
        if cached is None:
            await self.inbox.check_lease(self.cursor)
            work = asyncio.create_task(
                self.delegate.invoke(
                    capability,
                    request,
                    context,
                    thread_id=thread_id,
                    selection=self.selection,
                )
            )

            async def heartbeat():
                while True:
                    await asyncio.sleep(max(0.1, self.cursor.get('leaseDurationSeconds', 120) / 3))
                    try:
                        await self.inbox.check_lease(self.cursor)
                    except BaseException:
                        work.cancel()
                        raise

            renewal = asyncio.create_task(heartbeat())
            try:
                result = await work
                await self.inbox.check_lease(self.cursor)
                cached = {
                    'capability': result.capability,
                    'graphVersion': result.graph_version,
                    'data': deepcopy(dict(result.data)),
                }
                json.dumps(cached, allow_nan=False)
                saved = await self.inbox.save_extraction_call(self.cursor, self.publication_id, key, cached)
                cached = saved['extractionCalls'][key]
            finally:
                renewal.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal
                if not work.done():
                    work.cancel()
                    with suppress(asyncio.CancelledError):
                        await work
        return AgentResult(cached['capability'], context.run_id, cached['graphVersion'], deepcopy(cached['data']))
