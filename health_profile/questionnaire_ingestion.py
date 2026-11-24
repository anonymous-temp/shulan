"""Independent questionnaire ingestion with the shared evidence and fact governance."""

import hashlib
from uuid import NAMESPACE_URL
from uuid import uuid5

from modules.questionnaire.context import answer_rows

from .fact_writer import HealthProfileFactWriter
from .ingestion import HealthFactPolicy
from .ingestion import PolicyDisposition
from .models import ProfileIngestionSourceType
from .processing import ProfileIngestionProcessingError
from .processing import ProfileIngestionProcessingResult
from .record_ingestion import RecordAnswerCandidateExtractor
from .record_ingestion import RecordSubjectResolver
from .record_source import RecordProfileSourceRecord


class QuestionnaireProfileProcessor:
    GRAPH_VERSION = 'profile-ingest-questionnaire-v1'

    def __init__(
        self,
        *,
        store,
        repository,
        profile_service,
        agent_hub,
        documents=None,
        report_source=None,
        answer_extractor=None,
    ):
        self.store = store
        self.repository = repository
        self.profile_service = profile_service
        self.documents = documents
        self.report_source = report_source
        self.extractor = answer_extractor or RecordAnswerCandidateExtractor(
            agent_hub,
            source_type=ProfileIngestionSourceType.QUESTIONNAIRE,
        )
        self.resolver = RecordSubjectResolver(repository, profile_service)
        self.policy = HealthFactPolicy()
        self.writer = HealthProfileFactWriter(repository)

    async def process(self, job):
        if job.source_type is not ProfileIngestionSourceType.QUESTIONNAIRE:
            raise ProfileIngestionProcessingError('source.unsupported', 'Only questionnaire jobs are supported')
        snapshot = await self.store.get_submitted(job.user_id, job.source_id)
        if (
            snapshot is None
            or snapshot.get('userId') != job.user_id
            or snapshot.get('sourceRevision') != job.source_revision
        ):
            raise ProfileIngestionProcessingError('source.mismatch', 'Submitted questionnaire source is unavailable')
        await self.repository.ensure_indexes()
        bootstrap = await self.profile_service.ensure_self_profile(job.user_id)
        subject = bootstrap.subject
        if job.subject_id is not None and job.subject_id != subject.subject_id:
            raise ProfileIngestionProcessingError('subject.mismatch', 'Questionnaire subject does not match job')
        # Publication happens only after submission. A failed delivery retries this
        # stable report id; report sources retain their independent source type.
        if snapshot.get('artifacts'):
            if self.documents is None or self.report_source is None:
                raise ProfileIngestionProcessingError(
                    'source.publisher_unavailable', 'Report source publisher is required'
                )
            successful = {
                item.get('artifactId')
                for item in snapshot.get('reports', [])
                if isinstance(item.get('interpretation'), dict)
            }
            published = set()
            for artifact in snapshot['artifacts']:
                if artifact.get('artifactId') not in successful or artifact.get('kind') == 'document_reference':
                    continue
                content_key = artifact.get('contentSha256') or artifact['artifactId']
                if content_key in published:
                    continue
                result = await self.documents.read_artifact(user_id=job.user_id, artifact=artifact)
                if not isinstance(result, dict):
                    raise ProfileIngestionProcessingError(
                        'source.report_missing', 'Submitted report snapshot is unavailable'
                    )
                fingerprint = hashlib.sha256(f'{job.user_id}:{subject.subject_id}:{content_key}'.encode()).hexdigest()
                report_id = str(uuid5(NAMESPACE_URL, 'questionnaire-report:' + fingerprint))
                await self.report_source.save(
                    report_id=report_id,
                    user_id=job.user_id,
                    subject_id=subject.subject_id,
                    source_fingerprint=fingerprint,
                    result=result,
                )
                published.add(content_key)
        # Only mapped user answers, never generated titles/descriptions as evidence.
        rows = answer_rows(snapshot)
        source = RecordProfileSourceRecord(
            user_id=job.user_id,
            session_id=job.source_id,
            round_number=1,
            source_revision=job.source_revision,
            ingestion_status='queued',
            ingestion_job_id=job.job_id,
            session={},
        )
        candidates = await self.extractor.extract_answer_rows(source, rows, subject_id=subject.subject_id)
        facts = replayed = rejected = 0
        for candidate in candidates:
            resolution = await self.resolver.resolve(candidate, user_id=job.user_id, self_subject=subject)
            decision = self.policy.evaluate(resolution.candidate, subject_kind=resolution.subject_kind)
            if decision.disposition in {PolicyDisposition.REJECTED, PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION}:
                rejected += 1
                continue
            if resolution.subject is None:
                raise ProfileIngestionProcessingError(
                    'subject.resolution_inconsistent', 'Candidate has no resolved subject'
                )
            created = await self.writer.write(
                job=job, candidate=resolution.candidate, decision=decision, subject=resolution.subject
            )
            facts += int(created)
            replayed += int(not created)
        return ProfileIngestionProcessingResult(
            graph_version=self.GRAPH_VERSION,
            policy_version=self.policy.VERSION,
            extractor_version=self.extractor.VERSION + '+questionnaire-v1',
            candidate_count=len(candidates),
            fact_count=facts,
            replayed_fact_count=replayed,
            rejected_candidate_count=rejected,
        )
