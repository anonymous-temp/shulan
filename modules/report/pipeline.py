"""Four-stage report pipeline: parse, factual build, enrich, callback."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import uuid4

from modules.report.artifacts import active_final_report_id
from modules.report.artifacts import bind_report_batch
from modules.report.artifacts import callbacks_are_deferred
from modules.report.artifacts import is_deliverable
from modules.report.artifacts import note_report_documents
from modules.report.artifacts import note_report_subject
from modules.report.artifacts import reset_report_batch
from modules.report.artifacts import save_report_artifact
from modules.report.callback import _fixed_callback_body
from modules.report.clinical import attach_clinical_followup_context
from modules.report.clinical import attach_suggested_evaluations
from modules.report.clinical import build_abnormal_microbes
from modules.report.clinical import build_source_diseases
from modules.report.clinical import determine_enterotype
from modules.report.clinical import disease_has_patient_microbiome_signal
from modules.report.clinical import promote_evidence_disease_associations
from modules.report.clinical import recover_source_bound_diagnostic_interpretations
from modules.report.clinical_report import assess_clinical_evidence
from modules.report.clinical_report import attach_clinical_problems
from modules.report.clinical_report import clinical_assessment_request
from modules.report.clinical_report import clinical_evidence_coverage
from modules.report.clinical_report import clinical_gap_queries
from modules.report.clinical_report import clinical_queries
from modules.report.clinical_report import collect_source_facts
from modules.report.clinical_report import ensure_document_fallback
from modules.report.clinical_report import organize_problems
from modules.report.clinical_report import prepare_clinical_search_terms
from modules.report.evaluation_builder import build_evaluation_data
from modules.report.evaluation_evidence_plan import build_evaluation_evidence_plan
from modules.report.evaluation_evidence_plan import evaluation_plan_queries
from modules.report.evaluation_evidence_plan import evaluation_recall_queries
from modules.report.evaluation_evidence_plan import final_evaluation_evidence_coverage
from modules.report.evaluation_evidence_plan import select_topic_evidence
from modules.report.evaluation_narrative import add_evaluation_source_facts
from modules.report.evaluation_narrative import apply_evaluation_blocks
from modules.report.evaluation_narrative import compose_evaluation_narrative
from modules.report.evaluation_narrative import finalize_evaluation_references
from modules.report.evaluation_originals import build_evaluation_evidence_summaries
from modules.report.evaluation_originals import evaluation_citation_records
from modules.report.evaluation_originals import evaluation_original_text
from modules.report.evaluation_originals import partition_evaluation_originals
from modules.report.evaluation_narrative import _evaluation_sync
from modules.report.evaluation_priority_focus import finalize_priority_focus
from modules.report.evidence import EvidenceQuery
from modules.report.evidence import EvidenceRetrievalResult
from modules.report.evidence import _source_explicitly_supports_relation
from modules.report.evidence import _source_relation_direction
from modules.report.evidence import attach_evidence_refs
from modules.report.evidence import citation_numbers_for_semantic_records
from modules.report.evidence import consolidate_public_records
from modules.report.evidence import is_likely_single_case
from modules.report.evidence import is_preclinical_evidence
from modules.report.evidence import make_candidate_queries
from modules.report.evidence import make_contextual_relation_queries
from modules.report.evidence import make_coverage_queries
from modules.report.evidence import make_function_barrier_queries
from modules.report.evidence import make_queries
from modules.report.evidence import make_relation_fallback_queries
from modules.report.evidence import merge_retrieval_results
from modules.report.evidence import number_evidence
from modules.report.evidence import promote_discovery_conditions_to_bindings
from modules.report.evidence import recover_exact_relation_interpretations
from modules.report.evidence import recover_source_bound_topic_interpretations
from modules.report.evidence import retain_referenced_evidence
from modules.report.evidence import retrieve_evidence
from modules.report.evidence import uncovered_function_barrier_queries
from modules.report.evidence import update_query_audit_counts
from modules.report.evidence import validate_relation_bindings
from modules.report.fallback_policy import REPORT_GENERATION_CONCURRENCY
from modules.report.health_assessment import apply_abnormal_indicator_summaries
from modules.report.health_assessment import apply_assessment_explanations
from modules.report.health_assessment import build_health_assessment
from modules.report.narrative import NarrativeClient
from modules.report.narrative import NarrativeError
from modules.report.narrative import _retry_evidence_enrichment
from modules.report.narrative import apply_draft
from modules.report.narrative import apply_extractive_health_fallback
from modules.report.narrative import ensure_source_fact_row_narratives
from modules.report.narrative import generate_narrative
from modules.report.narrative import interpret_evidence
from modules.report.narrative import repair_disease_evidence_claims
from modules.report.narrative import supplement_common_examinations
from modules.report.parsing import MICROBIOME_RESULT_PENDING_SUMMARY
from modules.report.parsing import SourceParseError
from modules.report.parsing import attach_fmt_typing_context
from modules.report.parsing import parse_request_source
from modules.report.patient_context import build_patient_evidence_context
from modules.report.performance import increment_report_metric
from modules.report.performance import report_metrics_snapshot
from modules.report.performance import reset_report_metrics
from modules.report.performance import start_report_metrics
from modules.report.ports import DocumentTextGateway
from modules.report.ports import EvidenceGateway
from modules.report.ports import ProfileSnapshotGateway
from modules.report.ports import ReportArtifactStore
from modules.report.ports import ReportCallbackGateway
from modules.report.ports import SourceGateway
from modules.report.profile_snapshot import profile_indicator_facts
from modules.report.schemas import AbnormalMicrobesSection
from modules.report.schemas import DiseaseRisksSection
from modules.report.schemas import EvidenceChannelAudit
from modules.report.schemas import EvidenceCoverageIssue
from modules.report.schemas import EvidenceQueryAudit
from modules.report.schemas import EvidenceStageAudit
from modules.report.schemas import FailureCallback
from modules.report.schemas import HealthAssessment
from modules.report.schemas import HealthManagementSection
from modules.report.schemas import RelationCoverageAudit
from modules.report.schemas import RemoteFileInput
from modules.report.schemas import ReportAudit
from modules.report.schemas import ReportContent
from modules.report.schemas import ReportPerformanceAudit
from modules.report.schemas import ReportRequest
from modules.report.schemas import SampleInfo
from modules.report.schemas import SubjectInfo
from modules.report.schemas import SuccessCallback
from modules.report.schemas import WithheldDiseaseAudit
from modules.report.task_coordinator import ReportExecutionResult
from utils.log import getLogger
from utils.report_files import normalize_legacy_report_files

logger = getLogger(__name__)

SessionLoader = Callable[[str, str], Awaitable[dict[str, Any] | None]]
FollowupReportHandler = Callable[[dict[str, Any], str, str], Awaitable[Any]]
FollowupReplayHandler = Callable[[tuple[dict[str, Any], ...]], Awaitable[Any]]


@dataclass
class _GenerationProgress:
    request: Any | None = None
    evaluation_mode: bool = False
    clinical_document: Any | None = None
    evaluation_report_state: Any | None = None
    evaluation_plan: Any | None = None
    evaluation_queries: list[Any] = field(default_factory=list)
    evaluation_recall_used: int = 0
    parsed: Any | None = None
    microbes: Any | None = None
    diseases: Any | None = None
    health: Any | None = None
    health_assessment: Any | None = None
    references: list[Any] = field(default_factory=list)
    evidence_records: list[Any] = field(default_factory=list)
    failed_supports: set[str] = field(default_factory=set)
    query_audits: list[Any] = field(default_factory=list)
    relation_coverage: list[Any] = field(default_factory=list)
    stage_outcomes: list[Any] = field(default_factory=list)
    withheld_diseases: list[Any] = field(default_factory=list)
    stage_timings: dict[str, float] = field(default_factory=dict)
    # Exact semantic evidence can reappear in later coverage stages. Keep the
    # validated interpretation for the lifetime of this report task so the
    # same evidence key is never sent to the model twice.
    interpretation_cache: dict[str, Any] = field(default_factory=dict)
    retrieval_cache: dict[tuple[str, EvidenceQuery], EvidenceRetrievalResult] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)
    evaluation_work_deadline: float | None = None


_ACTIVE_PROGRESS: ContextVar[_GenerationProgress | None] = ContextVar(
    'report_generation_progress',
    default=None,
)

_INTERPRETATION_PROMPT_VERSION = 'report-evidence-v1'
_PROCESS_INTERPRETATION_CACHE_MAX = 2048
_PROCESS_INTERPRETATION_CACHE: OrderedDict[str, Any] = OrderedDict()


def _process_interpretation_cache_key(
    record: Any,
    patient_context: dict[str, Any] | None,
) -> str:
    record_payload = record.model_dump(
        mode='json',
        by_alias=True,
        exclude={'interpretation'},
        exclude_none=False,
    )
    raw = json.dumps(
        {
            'version': _INTERPRETATION_PROMPT_VERSION,
            'record': record_payload,
            'patientContext': patient_context or {},
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _process_interpretation_get(key: str) -> Any | None:
    cached = _PROCESS_INTERPRETATION_CACHE.get(key)
    if cached is None:
        return None
    _PROCESS_INTERPRETATION_CACHE.move_to_end(key)
    return cached.model_copy(deep=True)


def _process_interpretation_set(key: str, value: Any) -> None:
    _PROCESS_INTERPRETATION_CACHE[key] = value.model_copy(deep=True)
    _PROCESS_INTERPRETATION_CACHE.move_to_end(key)
    while len(_PROCESS_INTERPRETATION_CACHE) > _PROCESS_INTERPRETATION_CACHE_MAX:
        _PROCESS_INTERPRETATION_CACHE.popitem(last=False)


class ReportPipeline:
    def __init__(
        self,
        *,
        source_client: SourceGateway,
        evidence_client: EvidenceGateway,
        callback_client: ReportCallbackGateway,
        session_loader: SessionLoader,
        narrative_client: NarrativeClient | None,
        narrative_client_factory: Any = None,
        followup_report_handler: FollowupReportHandler | None = None,
        followup_replay_handler: FollowupReplayHandler | None = None,
        public_evidence_client: EvidenceGateway | None = None,
        document_client: DocumentTextGateway | None = None,
        document_source_factory: Any = None,
        profile_snapshot_client: ProfileSnapshotGateway | None = None,
        group_message_client: Any = None,
        artifact_store: ReportArtifactStore | None = None,
        session_timeout_seconds: float = 2.0,
        generation_timeout_seconds: float = 1800.0,
        max_concurrent_evaluate_reports: int = REPORT_GENERATION_CONCURRENCY,
        clinical_report_enabled: bool = False,
        research_agent: Any = None,
        web_evidence_client: Any = None,
    ) -> None:
        self.source_client = source_client
        # Optional third evidence source (allow-listed web pages); None keeps
        # library + PubMed recall only.
        self.web_evidence_client = web_evidence_client
        self.clinical_report_enabled = clinical_report_enabled
        self.research_agent = research_agent
        self.evidence_client = evidence_client
        self.callback_client = callback_client
        self.session_loader = session_loader
        self.narrative_client = narrative_client
        self.narrative_client_factory = narrative_client_factory
        self.followup_report_handler = followup_report_handler
        self.followup_replay_handler = followup_replay_handler
        self.public_evidence_client = public_evidence_client
        self.document_client = document_client
        self.document_source_factory = document_source_factory
        self.profile_snapshot_client = profile_snapshot_client
        self.group_message_client = group_message_client
        self.artifact_store = artifact_store
        self.session_timeout_seconds = max(0.1, session_timeout_seconds)
        self.generation_timeout_seconds = min(
            1800.0,
            max(0.01, generation_timeout_seconds),
        )
        # Admit only the expensive evaluate generation. The callback and the
        # existing downstream integration run after this slot is released, so
        # other evaluate requests are not blocked by code outside this report.
        self._evaluate_slots = asyncio.Semaphore(max(1, int(max_concurrent_evaluate_reports)))

    async def aclose(self) -> None:
        """Close evaluate-report adapters owned by this pipeline."""

        seen: set[int] = set()
        for client in (
            self.source_client,
            self.evidence_client,
            self.public_evidence_client,
            self.web_evidence_client,
            self.callback_client,
            self.document_client,
            self.narrative_client,
            self.group_message_client,
        ):
            if client is None or id(client) in seen:
                continue
            seen.add(id(client))
            close = getattr(client, 'aclose', None)
            if close is not None:
                await close()

    async def generate(self, request: ReportRequest) -> SuccessCallback:
        progress = _ACTIVE_PROGRESS.get()
        pipeline_started = time.monotonic()
        stage_timings: dict[str, float] = {}
        if progress is not None:
            progress.started = pipeline_started
            progress.stage_timings = stage_timings
        # 共享 Pipeline 实例不得被写入用户身份：为本次执行创建绑定
        # user_id/session_id 的叙事适配器，并在下面全程显式传递。
        # 未注入工厂时（测试与旧调用方）继续使用无身份 client。
        narrative_client = self.narrative_client
        if self.narrative_client_factory is not None:
            narrative_client = self.narrative_client_factory.create(
                user_id=request.user_id,
                session_id=request.session_id,
                execution_id=uuid4().hex,
            )
        # 本次执行只读一次长期画像，重试不重读；读取失败按无画像继续。
        profile_snapshot = None
        if self.profile_snapshot_client is not None:
            profile_snapshot = await self.profile_snapshot_client.read(
                request.user_id,
            )
        # 同一批 typed 指标同时供评分与证据适用性使用，两条链路口径一致。
        profile_facts = profile_indicator_facts(profile_snapshot)
        if profile_snapshot is not None:
            note_report_subject(profile_snapshot.self_subject_id)
        stage_started = time.monotonic()
        native_trigger = self.clinical_report_enabled and (
            request.group_id is not None or request.survey_list is not None
            or any(getattr(request, field) is not None for field in (
                'init_survey_schema', 'init_survey_answer', 'advanced_survey_schema', 'advanced_survey_answer')))
        # Native triggers carry their own questionnaires and IM group. Their
        # internal session trace is not a license to reuse a legacy QA cache.
        session_payload = {} if native_trigger else await self._load_session(request)
        request = merge_request_session_fallbacks(
            request,
            session_payload,
            include_subject=not self.clinical_report_enabled,
        )
        session_context = extract_session_context(session_payload)
        if self.clinical_report_enabled:
            from modules.report.evaluation_request_input import fmt_typing_report_source, prepare_evaluation_request
            request = prepare_evaluation_request(request, profile_snapshot)
            fmt_url, _ = fmt_typing_report_source(request)
            spec = request.microbiome_report_data
            fmt_urls = {url for url in (fmt_url,
                spec.fmt_typing_report_url if spec is not None else None) if url}
            if fmt_urls:
                request = request.model_copy(update={'file_list': [item for item in request.file_list
                    if item.url not in fmt_urls]})
        group_context = None
        group_errors = []
        if self.clinical_report_enabled and request.group_id is not None:
            group_started = time.monotonic()
            group_context = await self._load_group_context(request)
            group_errors = list(group_context.source_errors)
            request = _merge_group_attachments(request, group_context, group_errors)
            session_context['messages'] = [
                {'role': 'user', 'content': row['content'], 'recordId': row.get('recordId'),
                 'submittedAt': _group_message_date(row.get('sendTime')), 'source': row.get('source')}
                for row in group_context.patient_messages]
            stage_timings['group_context'] = _elapsed_ms(group_started)
        survey_in_context = False
        if native_trigger:
            survey = (request.model_extra or {}).get('healthSurvey')
            if survey not in (None, {}, [], ''):
                # The same source projection reaches scoring, evidence
                # applicability and narrative, and is injected exactly once.
                session_context['answers'] = [
                    session_context.get('answers'), {'healthSurvey': survey}]
                survey_in_context = True
        stage_timings['session_load'] = _elapsed_ms(stage_started)
        stage_started = time.monotonic()
        document_source = None
        if self.document_source_factory is not None and (
            request.file_list
            or (
                self.clinical_report_enabled
                and getattr(self.document_source_factory, 'supports_dedicated_evaluation_pdf', False) is True
                and request.microbiome_report_data is not None
                and request.microbiome_report_data.original_report_url
            )
        ):
            document_source = self.document_source_factory.create(
                user_id=request.user_id,
                session_id=request.session_id,
                final_report_id=active_final_report_id(),
            )
        enable_native_evaluation = getattr(document_source, 'enable_native_evaluation', None)
        if self.clinical_report_enabled and callable(enable_native_evaluation):
            enable_native_evaluation()
        parsed = await parse_request_source(
            request,
            source_client=self.source_client,
            document_client=self.document_client,
            document_source=document_source,
            enforce_subject_identity=self.clinical_report_enabled,
        )
        settle = getattr(document_source, 'settle_attachments', None)
        if callable(settle):
            expected_subject = SubjectInfo(name=request.name, sex=request.sex)
            parsed.source_documents = await settle(parsed.source_documents, expected_subject=expected_subject)
        if self.clinical_report_enabled:
            parsed = await attach_fmt_typing_context(request, parsed,
                source_client=self.source_client, document_client=self.document_client)
            parsed.source_errors.extend(error for error in group_errors if error not in parsed.source_errors)
            from modules.report.clinical_report import _hospital_row_classifier
            from modules.report.evaluation_sources import select_clinical_snapshot

            request = select_clinical_snapshot(request, parsed, classifier=_hospital_row_classifier(parsed))
        public_attachments = getattr(document_source, 'public_attachment_references', None)
        if callable(public_attachments):
            parsed.source_documents = public_attachments(parsed.source_documents)
        note_report_documents(parsed.source_documents)
        request_updates: dict[str, Any] = {}
        if parsed.supplementary_hospital_data:
            # Non-microbiome check-up documents are caller-supplied clinical
            # context.  Reuse the existing hospital-data pipeline rather than
            # inventing a second scoring/narrative path or changing the public
            # request/callback contracts.
            request_updates['hospital_data'] = [
                *request.hospital_data,
                *parsed.supplementary_hospital_data,
            ]
        # Explicit request demographics remain authoritative. When callers
        # submit only a PDF, labelled subject facts parsed from that document
        # must still reach assessment and evidence applicability logic; using
        # them only in the callback header left cardiovascular scoring with
        # a false "年龄、性别缺失" result.
        for subject_field in ('name', 'age', 'sex'):
            if getattr(request, subject_field) is None:
                value = getattr(parsed.subject, subject_field)
                if value is not None:
                    request_updates[subject_field] = value
        if request_updates:
            request = request.model_copy(update=request_updates)
        stage_timings['source_download_and_parse'] = _elapsed_ms(stage_started)
        microbiome_result_pending = parsed.source_type == 'pending'

        stage_started = time.monotonic()
        microbes = build_abnormal_microbes(parsed)
        if microbiome_result_pending:
            microbes.summary = _microbiome_pending_summary(self.clinical_report_enabled)
        diseases = build_source_diseases(parsed, request.hospital_data)
        withheld_diseases = [
            WithheldDiseaseAudit(
                disease_id=item.disease_id,
                name=item.name,
                reason='检测方未提供完整项目名称',
            )
            for item in diseases.items
            if item.item_type == 'unclear'
        ]
        diseases.items = [item for item in diseases.items if item.item_type != 'unclear']
        health = HealthManagementSection(enterotype=determine_enterotype(parsed))
        health_assessment = build_health_assessment(
            clinical_assessment_request(request, parsed) if self.clinical_report_enabled else request,
            parsed,
            session_context,
            profile_facts,
        )
        attach_clinical_followup_context(
            diseases,
            health_assessment,
            request.hospital_data,
        )
        evidence_context = build_patient_evidence_context(
            request,
            parsed,
            session_context,
            profile_snapshot,
        )
        stage_timings['fact_build'] = _elapsed_ms(stage_started)
        if progress is not None:
            progress.parsed = parsed
            progress.request = request
            progress.evaluation_mode = self.clinical_report_enabled
            progress.microbes = microbes
            progress.diseases = diseases
            progress.health = health
            progress.health_assessment = health_assessment
            progress.withheld_diseases = withheld_diseases

        clinical_document = None
        if self.clinical_report_enabled:
            clinical_started = time.monotonic()
            clinical_session_context = copy.deepcopy(session_context)
            survey = (request.model_extra or {}).get('healthSurvey')
            if survey not in (None, {}, [], '') and not survey_in_context:
                # Explicit questionnaire answers are source data, including
                # negative answers and their original submission dates.
                # The existing assertion extractor still decides whether an
                # answer actually states a symptom/history; questions alone
                # and model guesses never become clinical diagnoses.
                clinical_session_context['answers'] = [
                    clinical_session_context.get('answers'),
                    {'healthSurvey': survey},
                ]
            clinical_document = collect_source_facts(request, parsed,
                patient_context=evidence_context, session_context=clinical_session_context,
                assess_source_results=True)
            if group_context is not None:
                # Other members can inform interpretation as attributed
                # background, never as a statement made by the patient.
                clinical_document.background['groupConversationContext'] = {
                    'groupId': request.group_id,
                    'otherMemberMessages': copy.deepcopy(group_context.other_messages),
                    'patientMessageSources': [
                        {key: value for key, value in row.items() if key != 'content'}
                        for row in group_context.patient_messages],
                }
            if progress is not None:
                progress.clinical_document = clinical_document
            stage_timings['source_fact_collection'] = _elapsed_ms(clinical_started)

            # Evaluation has its own source-first report mainline.  The API,
            # parser, task admission, model pools and callback lifecycle stay
            # here; the risk-centered legacy enrichment path below does not
            # run for an evaluation request.
            from .evaluation_report import generate_evaluation_report
            return await generate_evaluation_report(
                pipeline=self, request=request, parsed=parsed,
                document=clinical_document, client=narrative_client,
                health_assessment=health_assessment, microbes=microbes,
                diseases=diseases, health=health, progress=progress,
                started=pipeline_started, stage_timings=stage_timings,
                withheld_diseases=withheld_diseases,
            )

        evidence_stage_started = time.monotonic()
        partial_reasons: list[str] = []
        if microbiome_result_pending:
            if not self.clinical_report_enabled:
                partial_reasons.append('MICROBIOME_RESULT_PENDING')
            elif not clinical_document or not any(fact.kind != 'context'
                    and fact.value is not None and str(fact.value).strip()
                    for fact in clinical_document.facts):
                partial_reasons.append('ASSESSMENT_DATA_UNAVAILABLE')
        if group_errors:
            partial_reasons.append('GROUP_CONTEXT_INCOMPLETE')
        if any('FMT_TYPING_' in error for error in parsed.source_errors):
            partial_reasons.append('FMT_TYPING_SOURCE_INCOMPLETE')
        if microbiome_result_pending and parsed.source_errors:
            partial_reasons.extend(['DOCUMENT_SOURCE_INCOMPLETE'] if self.clinical_report_enabled
                else _source_unavailable_reasons(parsed.source_errors))
        if document_source is not None and parsed.source_errors:
            partial_reasons.append('DOCUMENT_SOURCE_INCOMPLETE')
        if any('SHARED_INTERPRETATION_FALLBACK' in error for error in parsed.source_errors):
            partial_reasons.append('SHARED_INTERPRETATION_FALLBACK')
        if any(item.matches_source_summary is False for item in parsed.panel_counts):
            partial_reasons.append('SOURCE_COUNT_MISMATCH')
        # The 30-minute hard limit is a ceiling rather than extra unbounded
        # search time. These cumulative deadlines reserve the final six
        # minutes for report prose/health management and the last minute for
        # callback delivery. Short unit-test timeouts use the same scaled
        # phase proportions instead of escaping through fixed constants.
        (
            primary_deadline,
            candidate_deadline,
            enrichment_deadline,
            public_deadline,
        ) = _generation_stage_deadlines(
            started=pipeline_started,
            total_seconds=self.generation_timeout_seconds,
        )
        if clinical_document is not None:
            # The source microbiome catalogue remains in parsed facts; it is
            # not this document's clinical problem list or retrieval queue.
            diseases.items = [d for d in diseases.items if d.source_type == 'clinical_context']
            # Plan report-one questions after problem organization. The new
            # source-domain facts never become fabricated clinical problems.
            add_evaluation_source_facts(clinical_document, parsed)
            evaluation_data = build_evaluation_data(request, parsed, clinical_document, health_assessment)
            add_evaluation_source_facts(clinical_document, parsed, evaluation_data)
            evaluation_data = build_evaluation_data(request, parsed, clinical_document, health_assessment)
            from .evaluation_risk_assessment import add_profile_risk_facts, discover_risk_candidates
            risk_started = time.monotonic()
            add_profile_risk_facts(clinical_document, profile_snapshot, user_id=request.user_id)
            from .evaluation_risk_source_links import bind_evaluation_source_links
            bind_evaluation_source_links(clinical_document, evaluation_data)
            await discover_risk_candidates(narrative_client, clinical_document,
                subject={'gender': parsed.subject.sex, 'age': parsed.subject.age})
            stage_timings['integrated_risk_discovery'] = _elapsed_ms(risk_started)
            evidence_result = await self._retrieve_evaluation_evidence(
                clinical_document,
                evaluation_data,
                parsed.subject,
                narrative_client,
                primary_deadline,
                public_deadline,
                stage_timings,
                progress,
                microbes,
                diseases,
                research_scope=(request.user_id, request.session_id, active_final_report_id()),
            )
        else:
            queries = make_queries(
                microbes,
                diseases,
                age=parsed.subject.age,
                hospital_data=request.hospital_data,
            )
            primary_evidence_started = time.monotonic()
            primary_result = await _retrieve_evidence_until(
                self.evidence_client,
                queries,
                deadline=primary_deadline,
                provider='evimed_v2',
            )
            # Persist raw screened records before the optional model pass.  If the
            # model itself consumes the remaining report deadline, deterministic
            # source-bound recovery can still salvage exact statements.
            _checkpoint_evidence(
                progress,
                primary_result,
                microbes=microbes,
                diseases=diseases,
            )
            _primary_references, primary_numbers = number_evidence(primary_result.records)
            patient_context = {
                # Preserve the original prompt shape while adding the richer
                # context used for applicability ranking.  This is internal only
                # and does not change the request or callback contracts.
                'subject': parsed.subject.model_dump(mode='json', by_alias=True),
                'sample': parsed.sample.model_dump(mode='json', by_alias=True),
                'evidenceContext': evidence_context.model_dump(
                    mode='json',
                    by_alias=True,
                    exclude_none=False,
                ),
            }
            if narrative_client is not None and primary_result.records:
                await _interpret_evidence_until(
                    narrative_client,
                    records=primary_result.records,
                    numbers=primary_numbers,
                    patient_context=patient_context,
                    deadline=primary_deadline,
                )

            promote_discovery_conditions_to_bindings(primary_result.records, microbes)
            validate_relation_bindings(primary_result.records)
            _checkpoint_evidence(progress, primary_result, microbes=microbes, diseases=diseases)

            primary_relation_fallback = _bounded_relation_fallback_queries(
                make_relation_fallback_queries(
                    queries,
                    primary_result.records,
                )
            )
            if primary_relation_fallback:
                relation_result = await _retrieve_evidence_until(
                    self.evidence_client,
                    primary_relation_fallback,
                    deadline=primary_deadline,
                    provider='evimed_v2',
                )
                _relation_references, relation_numbers = number_evidence(relation_result.records)
                if narrative_client is not None and relation_result.records:
                    await _interpret_evidence_until(
                        narrative_client,
                        records=relation_result.records,
                        numbers=relation_numbers,
                        patient_context=patient_context,
                        deadline=primary_deadline,
                        _retry_enrichment=False,
                    )
                primary_result = merge_retrieval_results(
                    primary_result,
                    relation_result,
                )
                promote_discovery_conditions_to_bindings(primary_result.records, microbes)
                validate_relation_bindings(primary_result.records)
                _checkpoint_evidence(
                    progress,
                    primary_result,
                    microbes=microbes,
                    diseases=diseases,
                )

            coverage_queries = _bounded_evimed_coverage_queries(
                make_coverage_queries(
                    microbes,
                    diseases,
                    primary_result.records,
                    age=parsed.subject.age,
                    hospital_data=request.hospital_data,
                )
            )
            if coverage_queries:
                coverage_result = await _retrieve_evidence_until(
                    self.evidence_client,
                    coverage_queries,
                    deadline=primary_deadline,
                    provider='evimed_v2',
                )
                _coverage_references, coverage_numbers = number_evidence(coverage_result.records)
                if narrative_client is not None and coverage_result.records:
                    await _interpret_evidence_until(
                        narrative_client,
                        records=coverage_result.records,
                        numbers=coverage_numbers,
                        patient_context=patient_context,
                        deadline=primary_deadline,
                    )
                primary_result = merge_retrieval_results(
                    primary_result,
                    coverage_result,
                )

            promote_discovery_conditions_to_bindings(primary_result.records, microbes)
            validate_relation_bindings(primary_result.records)
            _checkpoint_evidence(progress, primary_result, microbes=microbes, diseases=diseases)
            stage_timings['primary_evidence'] = _elapsed_ms(primary_evidence_started)

            candidate_validation_started = time.monotonic()
            candidate_queries = _bounded_evimed_candidate_queries(
                make_candidate_queries(
                    microbes,
                    primary_result.records,
                    diseases=diseases,
                    age=parsed.subject.age,
                )
            )
            if candidate_queries:
                secondary_result = await _retrieve_evidence_until(
                    self.evidence_client,
                    candidate_queries,
                    deadline=candidate_deadline,
                    provider='evimed_v2',
                )
                _secondary_references, secondary_numbers = number_evidence(secondary_result.records)
                if narrative_client is not None and secondary_result.records:
                    await _interpret_evidence_until(
                        narrative_client,
                        records=secondary_result.records,
                        numbers=secondary_numbers,
                        patient_context=patient_context,
                        deadline=candidate_deadline,
                        _retry_enrichment=False,
                    )
                validate_relation_bindings(secondary_result.records)
                promote_discovery_conditions_to_bindings(
                    secondary_result.records,
                    microbes,
                )
                validate_relation_bindings(secondary_result.records)
                _checkpoint_evidence(
                    progress,
                    merge_retrieval_results(primary_result, secondary_result),
                    microbes=microbes,
                    diseases=diseases,
                )
                secondary_relation_fallback = _bounded_relation_fallback_queries(
                    make_relation_fallback_queries(
                        candidate_queries,
                        secondary_result.records,
                    )
                )
                if secondary_relation_fallback:
                    secondary_fallback_result = await _retrieve_evidence_until(
                        self.evidence_client,
                        secondary_relation_fallback,
                        deadline=candidate_deadline,
                        provider='evimed_v2',
                    )
                    _fallback_references, fallback_numbers = number_evidence(secondary_fallback_result.records)
                    if narrative_client is not None and secondary_fallback_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=secondary_fallback_result.records,
                            numbers=fallback_numbers,
                            patient_context=patient_context,
                            deadline=candidate_deadline,
                            _retry_enrichment=False,
                        )
                    secondary_result = merge_retrieval_results(
                        secondary_result,
                        secondary_fallback_result,
                    )
                    promote_discovery_conditions_to_bindings(
                        secondary_result.records,
                        microbes,
                    )
                    validate_relation_bindings(secondary_result.records)
                    _checkpoint_evidence(
                        progress,
                        merge_retrieval_results(primary_result, secondary_result),
                        microbes=microbes,
                        diseases=diseases,
                    )
                contextual_queries = _bounded_contextual_relation_queries(
                    make_contextual_relation_queries(
                        candidate_queries,
                        [*primary_result.records, *secondary_result.records],
                        evidence_context,
                    )
                )
                if contextual_queries:
                    contextual_result = await _retrieve_evidence_until(
                        self.evidence_client,
                        contextual_queries,
                        deadline=candidate_deadline,
                        provider='evimed_v2',
                    )
                    _contextual_refs, contextual_numbers = number_evidence(contextual_result.records)
                    if narrative_client is not None and contextual_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=contextual_result.records,
                            numbers=contextual_numbers,
                            patient_context=patient_context,
                            deadline=candidate_deadline,
                            _retry_enrichment=False,
                        )
                    validate_relation_bindings(contextual_result.records)
                    secondary_result = merge_retrieval_results(
                        secondary_result,
                        contextual_result,
                    )
                    _checkpoint_evidence(
                        progress,
                        merge_retrieval_results(primary_result, secondary_result),
                        microbes=microbes,
                        diseases=diseases,
                    )
                evidence_result = merge_retrieval_results(primary_result, secondary_result)
            else:
                evidence_result = primary_result
            _checkpoint_evidence(progress, evidence_result, microbes=microbes, diseases=diseases)
            stage_timings['candidate_validation'] = _elapsed_ms(candidate_validation_started)

            # Relation evidence can introduce a clinically relevant disease that
            # was not present as a usable source row (for example a detected
            # pathogen with a validated human stool association).  Previously the
            # disease was promoted only during final JSON assembly, after all
            # diagnostic-guide searches had already finished.  That ordering made
            # the disease visible but guaranteed an empty supplemental-check cell.
            # Promote validated candidates now and give only those new/updated
            # rows one bounded EviMed diagnostic pass before optional function
            # enrichment consumes the remaining stage budget.
            promoted_disease_ids = _promote_evidence_diseases_for_followup(
                microbes=microbes,
                diseases=diseases,
                records=evidence_result.records,
                subject=parsed.subject,
            )
            promoted_diagnostic_queries = _bounded_evimed_coverage_queries(
                [
                    query
                    for query in _make_public_coverage_queries(
                        microbes,
                        diseases,
                        evidence_result.records,
                        subject=parsed.subject,
                        age=parsed.subject.age,
                        hospital_data=request.hospital_data,
                    )
                    if query.intent == 'diagnostic_guideline' and query.disease_id in promoted_disease_ids
                ]
            )
            if promoted_diagnostic_queries:
                promoted_diagnostic_result = await _retrieve_evidence_until(
                    self.evidence_client,
                    promoted_diagnostic_queries,
                    deadline=enrichment_deadline,
                    provider='evimed_v2',
                )
                _promoted_refs, promoted_numbers = number_evidence(promoted_diagnostic_result.records)
                if narrative_client is not None and promoted_diagnostic_result.records:
                    await _interpret_evidence_until(
                        narrative_client,
                        records=promoted_diagnostic_result.records,
                        numbers=promoted_numbers,
                        patient_context=patient_context,
                        deadline=enrichment_deadline,
                        _retry_enrichment=False,
                    )
                recover_source_bound_diagnostic_interpretations(
                    diseases,
                    promoted_diagnostic_result.records,
                    subject_age=parsed.subject.age,
                )
                _apply_subject_demographic_gates(
                    promoted_diagnostic_result.records,
                    subject=parsed.subject,
                )
                evidence_result = merge_retrieval_results(
                    evidence_result,
                    promoted_diagnostic_result,
                )
                _checkpoint_evidence(
                    progress,
                    evidence_result,
                    microbes=microbes,
                    diseases=diseases,
                )

            function_guide_started = time.monotonic()
            function_queries = make_function_barrier_queries(
                parsed,
                microbes,
                age=parsed.subject.age,
            )
            if function_queries:
                function_result = await _retrieve_evidence_until(
                    self.evidence_client,
                    function_queries,
                    deadline=enrichment_deadline,
                    provider='evimed_v2',
                )
                _function_refs, function_numbers = number_evidence(function_result.records)
                if narrative_client is not None and function_result.records:
                    await _interpret_evidence_until(
                        narrative_client,
                        records=function_result.records,
                        numbers=function_numbers,
                        patient_context=patient_context,
                        deadline=enrichment_deadline,
                        _retry_enrichment=False,
                    )
                recover_source_bound_topic_interpretations(function_result.records)
                _apply_subject_demographic_gates(
                    function_result.records,
                    subject=parsed.subject,
                )
                expanded_function_queries = uncovered_function_barrier_queries(
                    make_function_barrier_queries(
                        parsed,
                        microbes,
                        age=parsed.subject.age,
                        expanded=True,
                    ),
                    function_result.records,
                )
                if expanded_function_queries:
                    expanded_function_result = await _retrieve_evidence_until(
                        self.evidence_client,
                        expanded_function_queries,
                        deadline=enrichment_deadline,
                        provider='evimed_v2',
                    )
                    _expanded_refs, expanded_numbers = number_evidence(expanded_function_result.records)
                    if narrative_client is not None and expanded_function_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=expanded_function_result.records,
                            numbers=expanded_numbers,
                            patient_context=patient_context,
                            deadline=enrichment_deadline,
                            _retry_enrichment=False,
                        )
                    recover_source_bound_topic_interpretations(expanded_function_result.records)
                    _apply_subject_demographic_gates(
                        expanded_function_result.records,
                        subject=parsed.subject,
                    )
                    function_result = merge_retrieval_results(
                        function_result,
                        expanded_function_result,
                    )
                evidence_result = merge_retrieval_results(
                    evidence_result,
                    function_result,
                )
                _checkpoint_evidence(
                    progress,
                    evidence_result,
                    microbes=microbes,
                    diseases=diseases,
                )
            # Empty and unmatched individual queries remain visible in the audit.
            # They are valid outcomes of staged retrieval, not generation failure.
            # Raw EviMed hits remain available in the query audit, but a record is
            # public evidence only after its title/summary has passed structured
            # interpretation.  This prevents an uninterpreted title from being
            # presented as support for a clinical conclusion.
            promote_discovery_conditions_to_bindings(evidence_result.records, microbes)
            validate_relation_bindings(evidence_result.records)
            # Recover exact source-bound interpretations before deciding which
            # rows still need the expensive PubMed/PMC fallback.  The same recovery
            # used to run only after public retrieval, so an already sufficient
            # EviMed guide or exact relation could still schedule another network
            # query.  Eligibility gates remain unchanged; this only closes work
            # that the final binding stage would accept deterministically anyway.
            _recover_public_eligible_interpretations(
                diseases=diseases,
                records=evidence_result.records,
                subject=parsed.subject,
            )
            promote_discovery_conditions_to_bindings(evidence_result.records, microbes)
            validate_relation_bindings(evidence_result.records)
            _checkpoint_evidence(
                progress,
                evidence_result,
                microbes=microbes,
                diseases=diseases,
            )
            stage_timings['function_barrier_and_guide'] = _elapsed_ms(function_guide_started)
            public_fallback_started = time.monotonic()
            if self.public_evidence_client is not None and narrative_client is not None:
                # Exact taxon-condition searches are the most useful fallback for
                # the first report module. Derive the missing set from the same
                # public-row eligibility checks used by ``make_coverage_queries``;
                # raw hits and uninterpreted bindings therefore cannot suppress a
                # needed PubMed query. Conversely, taxa that already have a
                # displayable relation do not consume the small public budget and
                # starve later uncovered rows.
                unresolved_microbe_ids = {
                    query.microbe_id
                    for query in _make_public_coverage_queries(
                        microbes,
                        diseases,
                        evidence_result.records,
                        subject=parsed.subject,
                        age=parsed.subject.age,
                        hospital_data=request.hospital_data,
                    )
                    if query.microbe_id and query.intent in {'microbe_association', 'microbe_clinical_significance'}
                }
                priority_exact_queries = _bounded_priority_public_relation_queries(
                    [
                        query
                        for query in make_candidate_queries(
                            microbes,
                            evidence_result.records,
                            diseases=diseases,
                            age=parsed.subject.age,
                        )
                        if query.microbe_id in unresolved_microbe_ids
                    ],
                    max_queries=12,
                )
                attempted_relation_ids = {query.relation_id for query in priority_exact_queries if query.relation_id}
                if priority_exact_queries:
                    priority_exact_result = await _retrieve_evidence_until(
                        self.public_evidence_client,
                        priority_exact_queries,
                        concurrency=3,
                        deadline=public_deadline,
                        provider='public_web',
                    )
                    _priority_refs, priority_numbers = number_evidence(priority_exact_result.records)
                    if priority_exact_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=priority_exact_result.records,
                            numbers=priority_numbers,
                            patient_context=patient_context,
                            deadline=public_deadline,
                            _retry_enrichment=False,
                        )
                    promote_discovery_conditions_to_bindings(
                        priority_exact_result.records,
                        microbes,
                    )
                    validate_relation_bindings(priority_exact_result.records)
                    evidence_result = merge_retrieval_results(
                        evidence_result,
                        priority_exact_result,
                    )
                    promote_discovery_conditions_to_bindings(
                        evidence_result.records,
                        microbes,
                    )
                    validate_relation_bindings(evidence_result.records)
                    priority_promoted_disease_ids = (
                        _promote_evidence_diseases_for_followup(
                            microbes=microbes,
                            diseases=diseases,
                            records=evidence_result.records,
                            subject=parsed.subject,
                        )
                        - promoted_disease_ids
                    )
                    _checkpoint_evidence(
                        progress,
                        evidence_result,
                        microbes=microbes,
                        diseases=diseases,
                    )

                    # Newly validated taxon-condition relations must receive
                    # their EviMed V2 diagnostic-guide pass immediately. Waiting
                    # until all broad public searches finish made common hospital
                    # examinations structurally empty whenever that lower-value
                    # expansion consumed the public deadline.
                    priority_diagnostic_queries = _bounded_evimed_coverage_queries(
                        [
                            query
                            for query in _make_public_coverage_queries(
                                microbes,
                                diseases,
                                evidence_result.records,
                                subject=parsed.subject,
                                age=parsed.subject.age,
                                hospital_data=request.hospital_data,
                            )
                            if query.intent == 'diagnostic_guideline'
                            and query.disease_id in priority_promoted_disease_ids
                        ]
                    )
                    if priority_diagnostic_queries:
                        priority_diagnostic_result = await _retrieve_evidence_until(
                            self.evidence_client,
                            priority_diagnostic_queries,
                            deadline=public_deadline,
                            provider='evimed_v2',
                        )
                        _diagnostic_refs, diagnostic_numbers = number_evidence(priority_diagnostic_result.records)
                        if priority_diagnostic_result.records:
                            await _interpret_evidence_until(
                                narrative_client,
                                records=priority_diagnostic_result.records,
                                numbers=diagnostic_numbers,
                                patient_context=patient_context,
                                deadline=public_deadline,
                                _retry_enrichment=False,
                            )
                        recover_source_bound_diagnostic_interpretations(
                            diseases,
                            priority_diagnostic_result.records,
                            subject_age=parsed.subject.age,
                        )
                        _apply_subject_demographic_gates(
                            priority_diagnostic_result.records,
                            subject=parsed.subject,
                        )
                        evidence_result = merge_retrieval_results(
                            evidence_result,
                            priority_diagnostic_result,
                        )
                        _checkpoint_evidence(
                            progress,
                            evidence_result,
                            microbes=microbes,
                            diseases=diseases,
                        )
                    promoted_disease_ids.update(priority_promoted_disease_ids)

                # Recompute coverage after the high-value exact pass. Disease
                # explanations and diagnostic guidance belong to the second core
                # module and run before optional function/barrier enrichment.
                # Previously the function model could consume the remaining public
                # deadline and leave source disease rows structurally present but
                # medically empty.
                fallback_queries = _bounded_public_fallback_queries(
                    _make_public_coverage_queries(
                        microbes,
                        diseases,
                        evidence_result.records,
                        subject=parsed.subject,
                        age=parsed.subject.age,
                        hospital_data=request.hospital_data,
                    )
                )
                if fallback_queries:
                    public_result = await _retrieve_evidence_until(
                        self.public_evidence_client,
                        fallback_queries,
                        concurrency=3,
                        deadline=public_deadline,
                        provider='public_web',
                    )
                    _public_refs, public_numbers = number_evidence(public_result.records)
                    if public_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=public_result.records,
                            numbers=public_numbers,
                            patient_context=patient_context,
                            deadline=public_deadline,
                            _retry_enrichment=False,
                            _retry_public_disease_claims=True,
                        )
                    promote_discovery_conditions_to_bindings(
                        public_result.records,
                        microbes,
                    )
                    validate_relation_bindings(public_result.records)
                    evidence_result = merge_retrieval_results(
                        evidence_result,
                        public_result,
                    )
                    promote_discovery_conditions_to_bindings(
                        evidence_result.records,
                        microbes,
                    )
                    validate_relation_bindings(evidence_result.records)
                    _checkpoint_evidence(
                        progress,
                        evidence_result,
                        microbes=microbes,
                        diseases=diseases,
                    )

                    # A broad overview search can still rank treatment papers or
                    # narrow cohorts above a usable disease definition.  Give only
                    # disease rows that remain without a publishable explanation
                    # one second, clinically worded public query.  This is capped
                    # at one additional query per disease and therefore cannot
                    # recreate the former unbounded synonym expansion.
                    secondary_public_disease_queries = _bounded_secondary_public_disease_queries(
                        _make_public_coverage_queries(
                            microbes,
                            diseases,
                            evidence_result.records,
                            subject=parsed.subject,
                            age=parsed.subject.age,
                            hospital_data=request.hospital_data,
                        ),
                        attempted_queries=fallback_queries,
                    )
                    if secondary_public_disease_queries:
                        secondary_public_result = await _retrieve_evidence_until(
                            self.public_evidence_client,
                            secondary_public_disease_queries,
                            concurrency=3,
                            deadline=public_deadline,
                            provider='public_web',
                        )
                        _secondary_refs, secondary_numbers = number_evidence(secondary_public_result.records)
                        if secondary_public_result.records:
                            await _interpret_evidence_until(
                                narrative_client,
                                records=secondary_public_result.records,
                                numbers=secondary_numbers,
                                patient_context=patient_context,
                                deadline=public_deadline,
                                _retry_enrichment=False,
                                _retry_public_disease_claims=True,
                            )
                        evidence_result = merge_retrieval_results(
                            evidence_result,
                            secondary_public_result,
                        )
                        _checkpoint_evidence(
                            progress,
                            evidence_result,
                            microbes=microbes,
                            diseases=diseases,
                        )

                    followup_microbe_ids = {query.microbe_id for query in fallback_queries if query.microbe_id}
                    exact_public_queries = _bounded_priority_public_relation_queries(
                        [
                            query
                            for query in make_candidate_queries(
                                microbes,
                                evidence_result.records,
                                diseases=diseases,
                                age=parsed.subject.age,
                            )
                            if query.microbe_id in followup_microbe_ids
                            and query.relation_id not in attempted_relation_ids
                        ],
                        max_queries=4,
                    )
                    if exact_public_queries:
                        exact_public_result = await _retrieve_evidence_until(
                            self.public_evidence_client,
                            exact_public_queries,
                            concurrency=3,
                            deadline=public_deadline,
                            provider='public_web',
                        )
                        _exact_refs, exact_numbers = number_evidence(exact_public_result.records)
                        if exact_public_result.records:
                            await _interpret_evidence_until(
                                narrative_client,
                                records=exact_public_result.records,
                                numbers=exact_numbers,
                                patient_context=patient_context,
                                deadline=public_deadline,
                                _retry_enrichment=False,
                            )
                        promote_discovery_conditions_to_bindings(
                            exact_public_result.records,
                            microbes,
                        )
                        validate_relation_bindings(exact_public_result.records)
                        evidence_result = merge_retrieval_results(
                            evidence_result,
                            exact_public_result,
                        )
                        _checkpoint_evidence(
                            progress,
                            evidence_result,
                            microbes=microbes,
                            diseases=diseases,
                        )
                    promote_discovery_conditions_to_bindings(
                        evidence_result.records,
                        microbes,
                    )
                    validate_relation_bindings(evidence_result.records)

                # Public taxon discovery can promote a new disease after the
                # earlier EviMed diagnostic pass has already finished.  Give only
                # those newly promoted rows one final bounded EviMed guide search
                # before optional function enrichment can consume the deadline.
                late_promoted_disease_ids = (
                    _promote_evidence_diseases_for_followup(
                        microbes=microbes,
                        diseases=diseases,
                        records=evidence_result.records,
                        subject=parsed.subject,
                    )
                    - promoted_disease_ids
                )
                late_diagnostic_queries = _bounded_evimed_coverage_queries(
                    [
                        query
                        for query in _make_public_coverage_queries(
                            microbes,
                            diseases,
                            evidence_result.records,
                            subject=parsed.subject,
                            age=parsed.subject.age,
                            hospital_data=request.hospital_data,
                        )
                        if query.intent == 'diagnostic_guideline' and query.disease_id in late_promoted_disease_ids
                    ]
                )
                if late_diagnostic_queries:
                    late_diagnostic_result = await _retrieve_evidence_until(
                        self.evidence_client,
                        late_diagnostic_queries,
                        deadline=public_deadline,
                        provider='evimed_v2',
                    )
                    _late_refs, late_numbers = number_evidence(late_diagnostic_result.records)
                    if late_diagnostic_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=late_diagnostic_result.records,
                            numbers=late_numbers,
                            patient_context=patient_context,
                            deadline=public_deadline,
                            _retry_enrichment=False,
                        )
                    recover_source_bound_diagnostic_interpretations(
                        diseases,
                        late_diagnostic_result.records,
                        subject_age=parsed.subject.age,
                    )
                    _apply_subject_demographic_gates(
                        late_diagnostic_result.records,
                        subject=parsed.subject,
                    )
                    evidence_result = merge_retrieval_results(
                        evidence_result,
                        late_diagnostic_result,
                    )
                    _checkpoint_evidence(
                        progress,
                        evidence_result,
                        microbes=microbes,
                        diseases=diseases,
                    )

                # A successful first diagnostic search is not sufficient when its
                # retained source still cannot bind one concrete hospital test to
                # the disease.  Retry only the next, previously untried diagnostic
                # hint for disease rows already backed by a patient microbiome
                # signal.  This closes a real gap (for example an ultrasound-only
                # kidney-stone search returning no usable local statement) without
                # reopening the former unbounded synonym expansion.
                alternate_diagnostic_queries = _bounded_untried_diagnostic_queries(
                    [
                        query
                        for query in _make_public_coverage_queries(
                            microbes,
                            diseases,
                            evidence_result.records,
                            subject=parsed.subject,
                            age=parsed.subject.age,
                            hospital_data=request.hospital_data,
                        )
                        if query.intent == 'diagnostic_guideline'
                        # Disease rows promoted by the public fallback are just as
                        # patient-bound as rows promoted during the earlier
                        # EviMed pass.  Excluding them here meant a late
                        # Oxalobacter -> kidney-stone row could try the ultrasound
                        # hint but never the next bounded CT hint.
                        and query.disease_id in (promoted_disease_ids | late_promoted_disease_ids)
                    ],
                    query_audits=evidence_result.query_audits,
                )
                if alternate_diagnostic_queries:
                    alternate_diagnostic_result = await _retrieve_evidence_until(
                        self.evidence_client,
                        alternate_diagnostic_queries,
                        deadline=public_deadline,
                        provider='evimed_v2',
                    )
                    _alternate_refs, alternate_numbers = number_evidence(alternate_diagnostic_result.records)
                    if narrative_client is not None and alternate_diagnostic_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=alternate_diagnostic_result.records,
                            numbers=alternate_numbers,
                            patient_context=patient_context,
                            deadline=public_deadline,
                            _retry_enrichment=False,
                        )
                    recover_source_bound_diagnostic_interpretations(
                        diseases,
                        alternate_diagnostic_result.records,
                        subject_age=parsed.subject.age,
                    )
                    _apply_subject_demographic_gates(
                        alternate_diagnostic_result.records,
                        subject=parsed.subject,
                    )
                    evidence_result = merge_retrieval_results(
                        evidence_result,
                        alternate_diagnostic_result,
                    )
                    _checkpoint_evidence(
                        progress,
                        evidence_result,
                        microbes=microbes,
                        diseases=diseases,
                    )

                # EviMed remains the primary source for function/barrier content.
                # Public function enrichment is useful, but it is lower priority
                # than the two patient-facing evidence modules above and therefore
                # receives only whatever bounded public time remains.
                public_function_queries = _bounded_public_function_queries(
                    uncovered_function_barrier_queries(
                        make_function_barrier_queries(
                            parsed,
                            microbes,
                            age=parsed.subject.age,
                            expanded=True,
                        ),
                        evidence_result.records,
                    )
                )
                if public_function_queries:
                    public_function_result = await _retrieve_evidence_until(
                        self.public_evidence_client,
                        public_function_queries,
                        concurrency=3,
                        deadline=public_deadline,
                        provider='public_web',
                    )
                    _function_public_refs, function_public_numbers = number_evidence(public_function_result.records)
                    if public_function_result.records:
                        await _interpret_evidence_until(
                            narrative_client,
                            records=public_function_result.records,
                            numbers=function_public_numbers,
                            patient_context=patient_context,
                            deadline=public_deadline,
                            _retry_enrichment=False,
                        )
                    recover_source_bound_topic_interpretations(public_function_result.records)
                    _apply_subject_demographic_gates(
                        public_function_result.records,
                        subject=parsed.subject,
                    )
                    evidence_result = merge_retrieval_results(
                        evidence_result,
                        public_function_result,
                    )
                    _checkpoint_evidence(
                        progress,
                        evidence_result,
                        microbes=microbes,
                        diseases=diseases,
                    )

            stage_timings['public_evidence_fallback'] = _elapsed_ms(public_fallback_started)
        evidence_binding_started = time.monotonic()
        # AI interpretation can fail for an otherwise exact, already-screened
        # taxon-outcome source. Recover only source-explicit relations before
        # the final public gate so transient model failures do not erase valid
        # evidence from otherwise identical reports.
        if clinical_document is None:
            _recover_public_eligible_interpretations(
                diseases=diseases,
                records=evidence_result.records,
                subject=parsed.subject,
            )
            validate_relation_bindings(evidence_result.records)
        _checkpoint_evidence(
            progress,
            evidence_result,
            microbes=microbes,
            diseases=diseases,
        )
        public_records = (
            evidence_result.records
            if clinical_document is not None
            else _eligible_public_records(
                evidence_result.records,
                microbes=microbes,
                subject=parsed.subject,
            )
        )
        recovered_supports = {support for record in public_records for support in record.supports}
        if evidence_result.failed_supports - recovered_supports or any(
            channel.status == 'failed' for audit in evidence_result.query_audits for channel in audit.channels
        ):
            partial_reasons.append('EVIDENCE_RETRIEVAL_INCOMPLETE')
        if clinical_document is None:
            update_query_audit_counts(evidence_result, public_records=public_records)
        # Evaluate candidates deliberately bypass the old clinical interpreter.
        # Absence of that unrelated pass is not an AI-interpretation failure;
        # publication is decided by the later per-sentence original reviews.
        if clinical_document is None and any(
            audit.retained_count > audit.interpreted_count for audit in evidence_result.query_audits
        ):
            partial_reasons.append('EVIDENCE_INTERPRETATION_INCOMPLETE')
        if any(audit.raw_count > 0 for audit in evidence_result.query_audits) and not public_records:
            partial_reasons.append('EVIDENCE_FILTER_EMPTY')
        if any(audit.relation_id and audit.public_count == 0 for audit in evidence_result.query_audits):
            partial_reasons.append('TAXON_RELATION_UNRESOLVED')
        # Keep intent-specific interpretations for binding medical meaning and
        # diagnostic purpose.  The same EviMed source may be returned for a
        # disease explanation and a diagnostic query; collapsing those records
        # first loses one of the two validated interpretations.  Bibliography
        # metadata is still deduplicated independently.
        semantic_records = public_records
        clinical_summaries = None
        if clinical_document is not None:
            semantic_records, references, numbers, clinical_summaries = _number_evaluation_originals(
                clinical_document, semantic_records, stage='initial_numbering')
            public_records = semantic_records
            evidence_result.records = semantic_records
            _checkpoint_evidence(progress, evidence_result, microbes=microbes, diseases=diseases)
        else:
            citation_records = consolidate_public_records([record.model_copy(deep=True) for record in semantic_records])
            references, citation_numbers = number_evidence(citation_records)
            numbers = citation_numbers_for_semantic_records(
                semantic_records,
                citation_records,
                citation_numbers,
            )
        attach_evidence_refs(
            microbes,
            diseases,
            semantic_records,
            numbers,
        )
        if clinical_document is None:
            promote_evidence_disease_associations(diseases, microbes)
        _attach_function_barrier_refs(
            microbes,
            semantic_records,
            numbers,
        )
        attach_suggested_evaluations(
            diseases,
            semantic_records,
            numbers,
            subject_age=parsed.subject.age,
        )
        ensure_source_fact_row_narratives(
            microbes=microbes,
            diseases=diseases,
        )
        stage_timings['evidence_binding'] = _elapsed_ms(evidence_binding_started)
        if progress is not None:
            progress.references = [reference.model_copy(deep=True) for reference in references]
            progress.query_audits = evidence_result.query_audits
            progress.relation_coverage = evidence_result.relation_coverage
            progress.stage_outcomes = _stage_outcomes(
                evidence_result.query_audits,
                microbes=microbes,
                diseases=diseases,
            )
        stage_timings['evidence_retrieval_and_interpretation'] = _elapsed_ms(evidence_stage_started)
        # A completed search may validly yield no direct evidence for a row;
        # empty fields stay empty instead of being guessed.

        evidence_summaries = (clinical_summaries
            if clinical_document is not None else _evidence_summaries(semantic_records, numbers))
        if clinical_document is not None:
            from .evaluation_risk_assessment import refresh_risk_original_scope
            refresh_risk_original_scope(clinical_document, evidence_summaries)
        # Persist deterministic, evidence-bounded health content before the
        # final model call. If that call reaches the report deadline, the
        # progress snapshot still has a health portrait and cited advice.
        executive_summary = apply_extractive_health_fallback(
            health=health,
            evidence_summaries=evidence_summaries,
            microbes=microbes,
            diseases=diseases,
            hospital_data=request.hospital_data,
            health_assessment=health_assessment,
        )

        evaluation_coverage_plan = evaluation_coverage_selection = None
        evaluation_reviewed_blocks = []
        evaluation_reference_keys = {}
        narrative_started = time.monotonic()
        # Integrated evaluate publishes the independently reviewed risk
        # registry's clinical actions. This legacy DTO is not an input to
        # build_evaluation_data; generating a second examination list here
        # only adds unused advice and unrelated partial-failure flags.
        if narrative_client is not None and clinical_document is None:
            try:
                unresolved_checks = await supplement_common_examinations(
                    narrative_client,
                    diseases=diseases,
                    hospital_data=request.hospital_data,
                    subject=parsed.subject,
                    session_context=session_context,
                )
                if unresolved_checks:
                    partial_reasons.append('COMMON_EXAMINATION_INCOMPLETE')
            except NarrativeError:
                partial_reasons.append('COMMON_EXAMINATION_UNAVAILABLE')
                logger.warning('report common examination supplementation failed')
        if clinical_document is not None:
            from .evaluation_risk_projection import project_integrated_risks
            semantic_records, evidence_summaries, references, numbers = await self._assess_integrated_risks(
                clinical_document, evaluation_data, parsed.subject, narrative_client,
                semantic_records, evidence_summaries, references, numbers,
                public_deadline, stage_timings, progress, microbes, diseases)
            await _evaluation_sync(self.clinical_report_enabled,
                project_integrated_risks, evaluation_data, clinical_document)
            registry = clinical_document.risk_registry
            partial_reasons.extend(registry.incomplete)
            from .evaluation_evidence_plan import refresh_evaluation_evidence_consumers
            evaluation_coverage_plan = refresh_evaluation_evidence_consumers(
                getattr(clinical_document, '_evaluation_evidence_plan', None),
                evaluation_data, clinical_document)
            clinical_document._evaluation_evidence_plan = evaluation_coverage_plan
            if progress is not None:
                progress.evaluation_plan = evaluation_coverage_plan
            await compose_evaluation_narrative(narrative_client, clinical_document, evaluation_data, evidence_summaries,
                subject={"gender": parsed.subject.sex, "age": parsed.subject.age},
                original_evidence_texts={record['number']: record['sourceText'] for record in evidence_summaries
                    if record['sourceText'].strip()},
                source_records={'request.hospitalData': request.hospital_data,
                    'parsed.supplementaryHospitalData': parsed.supplementary_hospital_data})
            # Composition can remove unanswered questionnaire slots. Audit
            # its current consumer addresses, without a new retrieval budget
            # or treating any selected candidate as an approved assertion.
            evaluation_coverage_plan = clinical_document._evaluation_evidence_plan
            if progress is not None:
                progress.evaluation_plan = evaluation_coverage_plan
            evaluation_coverage_selection = select_topic_evidence(
                evaluation_coverage_plan, evidence_summaries,
                per_topic_limit=len(evidence_summaries), max_records=len(evidence_summaries))
            # Retention mutates citation numbers; preserve the reviewed
            # sentences and their original stable-key mapping for final audit.
            evaluation_reviewed_blocks = copy.deepcopy(clinical_document.sections)
            evaluation_reference_keys = {record['number']: record['evidenceKey']
                for record in evaluation_coverage_selection.records}
            evaluation_data = await _evaluation_sync(
                self.clinical_report_enabled, apply_evaluation_blocks,
                evaluation_data, clinical_document)
            if narrative_client is not None:
                # Repair only gaps observed after publication guards have
                # actually run. An omitted slot is not a completed report,
                # and source inventories cannot stand in for interpretation.
                from .evaluation_risk_assessment import compose_risk_summaries
                from .evaluation_narrative import find_publication_repair_codes
                await compose_risk_summaries(narrative_client, clinical_document,
                    subject={"gender": parsed.subject.sex, "age": parsed.subject.age},
                    evidence_summaries=evidence_summaries, only_missing=True)
                await _evaluation_sync(self.clinical_report_enabled,
                    project_integrated_risks, evaluation_data, clinical_document)
                from .evaluation_plan_review import review_evaluation_plan
                plan_review = await review_evaluation_plan(narrative_client,
                    evaluation_data, clinical_document,
                    subject={"gender": parsed.subject.sex, "age": parsed.subject.age})
                repair_codes = await _evaluation_sync(self.clinical_report_enabled,
                    find_publication_repair_codes, evaluation_data, clinical_document)
                repair_codes = list(dict.fromkeys([*repair_codes,
                    *plan_review.get('repairCodes', [])]))
                if repair_codes:
                    await compose_evaluation_narrative(narrative_client, clinical_document,
                        evaluation_data, evidence_summaries,
                        subject={"gender": parsed.subject.sex, "age": parsed.subject.age},
                        original_evidence_texts={record['number']: record['sourceText']
                            for record in evidence_summaries if record['sourceText'].strip()},
                        source_records={'request.hospitalData': request.hospital_data,
                            'parsed.supplementaryHospitalData': parsed.supplementary_hospital_data},
                        only_codes=repair_codes)
                    evaluation_data = await _evaluation_sync(self.clinical_report_enabled,
                        apply_evaluation_blocks, evaluation_data, clinical_document)
                    await _evaluation_sync(self.clinical_report_enabled,
                        project_integrated_risks, evaluation_data, clinical_document)
                    await review_evaluation_plan(narrative_client, evaluation_data,
                        clinical_document,
                        subject={"gender": parsed.subject.sex, "age": parsed.subject.age})
                # Coverage must describe the final repaired live blocks,
                # before reference retention renumbers their citations.
                evaluation_coverage_plan = refresh_evaluation_evidence_consumers(
                    getattr(clinical_document, '_evaluation_evidence_plan', None),
                    evaluation_data, clinical_document)
                clinical_document._evaluation_evidence_plan = evaluation_coverage_plan
                evaluation_coverage_selection = select_topic_evidence(evaluation_coverage_plan,
                    evidence_summaries, per_topic_limit=len(evidence_summaries),
                    max_records=len(evidence_summaries))
                evaluation_reviewed_blocks = copy.deepcopy(clinical_document.sections)
                evaluation_reference_keys = {record['number']: record['evidenceKey']
                    for record in evaluation_coverage_selection.records}
            executive_summary = evaluation_data.health_overview.overall_summary.text or executive_summary
            partial_reasons.extend(clinical_document.incomplete)
        elif narrative_client is None:
            partial_reasons.append('MODEL_NARRATIVE_UNAVAILABLE')
        else:
            try:
                draft = await generate_narrative(
                    narrative_client,
                    parsed=parsed,
                    microbes=microbes,
                    diseases=diseases,
                    hospital_data=request.hospital_data,
                    session_context=session_context,
                    evidence_summaries=evidence_summaries,
                    enterotype=(health.enterotype.name if health.enterotype is not None else None),
                    health_assessment=health_assessment,
                )
                model_executive_summary = apply_draft(
                    draft,
                    microbes=microbes,
                    diseases=diseases,
                    health=health,
                    hospital_data=request.hospital_data,
                )
                # The deterministic checkpoint is already source/evidence
                # bounded.  A model response may be useful for item prose while
                # its optional global summary fails validation; that must not
                # erase the valid checkpoint and leave the callback empty.
                executive_summary = model_executive_summary or executive_summary
                apply_assessment_explanations(
                    health_assessment,
                    [item.model_dump(mode='json', by_alias=True) for item in draft.assessment_explanations],
                )
                apply_abnormal_indicator_summaries(
                    health_assessment,
                    [item.model_dump(mode='json', by_alias=True) for item in draft.abnormal_indicator_summaries],
                )
                if microbiome_result_pending:
                    microbes.summary = _microbiome_pending_summary(self.clinical_report_enabled)
                if not _narrative_is_complete(
                    executive_summary=executive_summary,
                    microbes=microbes,
                    diseases=diseases,
                    health=health,
                ) or any(not item.clinical_significance for item in health_assessment.abnormal_indicators):
                    partial_reasons.append('MODEL_NARRATIVE_INCOMPLETE')
            except NarrativeError as error:
                logger.error(
                    'report narrative generation failed: user_id=%s session_id=%s error_type=%s',
                    request.user_id,
                    request.session_id,
                    type(error).__name__,
                )
                partial_reasons.append('MODEL_NARRATIVE_UNAVAILABLE')
        stage_timings['report_narrative'] = _elapsed_ms(narrative_started)

        if microbiome_result_pending:
            microbes.summary = _microbiome_pending_summary(self.clinical_report_enabled)
            if request.hospital_data or clinical_document is not None:
                # A clinical-only upload is not an empty report. Keep the
                # already generated whole-person health summary and confine
                # the microbiome-pending notice to the microbiome module.
                # Overwriting it here previously discarded the parsed lung,
                # liver, cardiovascular and metabolic findings from the final
                # conclusion.
                executive_summary = executive_summary or health.health_profile
            else:
                executive_summary = _pending_executive_summary(
                    request,
                    has_session_context=bool(session_context),
                    evaluation_mode=self.clinical_report_enabled,
                )
            if not health.health_profile:
                health.health_profile = _pending_health_profile(
                    request,
                    has_session_context=bool(session_context),
                    evaluation_mode=self.clinical_report_enabled,
                )

        finalization_started = time.monotonic()
        partial_reasons.extend(
            []
            if clinical_document is not None
            else _content_completeness_reasons(
                microbes=microbes,
                diseases=diseases,
                query_audits=evidence_result.query_audits,
            )
        )

        def finalize_report():
            nonlocal references, evaluation_data
            # Retention mutates these same reference objects in place. Carry
            # their stable candidate identity through that operation instead
            # of reconstructing review permission from a public bibliography.
            retained_identity_keys = {id(reference): evaluation_reference_keys.get(reference.number)
                for reference in references}
            references = retain_referenced_evidence(
                references,
                microbes=microbes,
                diseases=diseases,
                health=health,
                clinical_document=clinical_document,
            )
            if progress is not None:
                # Retention renumbers facts/blocks and references synchronously.
                # Recovery must see that same checkpoint, without discarded
                # records sharing the newly assigned numbers.
                progress.references = [reference.model_copy(deep=True) for reference in references]
            # Reference numbering is finalized once for the internal source DTO.
            # Re-project the new public data afterwards so its prose and appendix
            # use the remapped numbers without changing downstream generation.
            evaluation_data = build_evaluation_data(request, parsed, clinical_document, health_assessment, references)
            if clinical_document is not None:
                evaluation_data = apply_evaluation_blocks(evaluation_data, clinical_document)
                from .evaluation_risk_projection import project_integrated_risks
                project_integrated_risks(evaluation_data, clinical_document, references, final=True)
            if self.clinical_report_enabled:
                finalize_priority_focus(evaluation_data, facts={fact.fact_id: fact for fact in clinical_document.facts}
                    if clinical_document is not None else None)
            evaluation_data = finalize_evaluation_references(evaluation_data)
            if clinical_document is not None and evaluation_coverage_plan is not None:
                coverage_diagnostics = {}
                current_reference_keys = {reference.number: retained_identity_keys[id(reference)]
                    for reference in references if retained_identity_keys.get(id(reference))}
                final_coverage = final_evaluation_evidence_coverage(
                    evaluation_coverage_plan, evaluation_coverage_selection, evaluation_data,
                    copy.deepcopy(clinical_document.sections), reference_keys=current_reference_keys,
                    diagnostics=coverage_diagnostics)
                clinical_document._evaluation_final_coverage_diagnostics = coverage_diagnostics
                logger.info('report evaluation final evidence gate diagnostics: %s',
                    json.dumps(coverage_diagnostics, ensure_ascii=False))
                logger.info('report evaluation final evidence coverage: topics=%s candidates=%s '
                    'approved_topics=%s covered_topics=%s approved_claims=%s approved_sources=%s coverage=%s',
                    len(final_coverage), len(evaluation_coverage_selection.records),
                    sum(bool(row['approvedRoles']) for row in final_coverage),
                    sum(row['status'] == 'covered' for row in final_coverage),
                    sum(row['approvedClaimCount'] for row in final_coverage),
                    len({key for row in final_coverage for key in row['approvedEvidenceKeys']}),
                    json.dumps(final_coverage, ensure_ascii=False))
            return ReportContent(
                subject=parsed.subject,
                sample=parsed.sample,
                health_assessment=health_assessment,
                executive_summary=executive_summary,
                abnormal_microbes=microbes,
                disease_risks=diseases,
                health_management=health,
                evidence_references=references,
                clinical_document=clinical_document,
                evaluation_data=evaluation_data,
            )

        report = await _evaluation_sync(self.clinical_report_enabled, finalize_report)
        if self.clinical_report_enabled and clinical_document is not None:
            # Final publication can reject a sentence after reference
            # retention/reprojection. Its partial outcome must survive into
            # the same callback status, rather than a success with empty prose.
            partial_reasons.extend(clinical_document.incomplete)
        unique_reasons = list(dict.fromkeys(partial_reasons))
        stage_timings['json_finalization'] = _elapsed_ms(finalization_started)
        stage_timings['total'] = _elapsed_ms(pipeline_started)
        performance = _performance_audit(
            stage_timings=stage_timings,
            query_audits=evidence_result.query_audits,
            public_evidence_count=_public_reference_count(references),
            source_snapshot_count=len(parsed.source_history),
            source_error_count=len(parsed.source_errors),
        )
        _log_report_performance(
            status='partial_success' if unique_reasons else 'success',
            partial_reasons=unique_reasons,
            performance=performance,
            query_audits=evidence_result.query_audits,
        )
        return SuccessCallback(
            user_id=request.user_id,
            session_id=request.session_id,
            status='partial_success' if unique_reasons else 'success',
            generated_at=datetime.now(UTC),
            report=report,
            partial_reasons=unique_reasons,
            audit=ReportAudit(
                source_count_reconciliation=parsed.panel_counts,
                evidence_queries=evidence_result.query_audits,
                relation_coverage=evidence_result.relation_coverage,
                withheld_disease_items=withheld_diseases,
                coverage_issues=_coverage_issues(parsed, microbes, diseases),
                stage_outcomes=_stage_outcomes(
                    evidence_result.query_audits,
                    microbes=microbes,
                    diseases=diseases,
                ),
                performance=performance,
            ),
        )

    async def _retrieve_evaluation_evidence(
        self,
        document,
        data,
        subject,
        client,
        primary_deadline,
        public_deadline,
        timings,
        progress,
        microbes,
        diseases,
        *,
        research_scope=None,
    ):
        """Retrieve originals, then repair unmet relations within one budget.

        Evidence coverage review only schedules a search; final risk and
        sentence reviews still independently authorize every public claim.
        """
        from modules.report.evaluation_evidence_plan import (
            evaluation_gap_queries, evaluation_management_gaps, prioritize_evaluation_recalls,
            truncated_original_queries,
        )
        from modules.report.evaluation_risk_assessment import review_risk_evidence

        started = time.monotonic()
        await prepare_clinical_search_terms(client, document, include_existing=True)
        timings['clinical_search_terms'] = _elapsed_ms(started)
        plan = build_evaluation_evidence_plan(data, document, max_queries=48, max_taxon_queries=8,
            subject={'age': subject.age, 'gender': subject.sex})
        document._evaluation_evidence_plan = plan
        if progress is not None:
            progress.evaluation_plan = plan
        age = int(subject.age) if str(subject.age).isdigit() else None
        age_group = 'adult' if age is not None and age >= 18 else 'pediatric' if age is not None else 'unknown'
        queries = [replace(query, age_group=age_group) for query in evaluation_plan_queries(plan)]
        started = time.monotonic()
        result = await _retrieve_evidence_until(self.evidence_client, queries,
            deadline=primary_deadline, provider='evimed_v2')
        result.records = list(_partition_evaluation_original_pool(document,
            result.records, stage='primary_retrieval').existing)
        _checkpoint_evidence(progress, result, microbes=microbes, diseases=diseases)
        timings['primary_clinical_evidence'] = _elapsed_ms(started)

        def candidates(records, attempted_queries=()):
            # summary/AI-summary/evidenceText cannot manufacture the independent
            # original abstract expected by evaluate's final citation reviews.
            originals = [record for record in records if evaluation_original_text(record)]
            return select_topic_evidence(plan, originals, per_topic_limit=2, max_records=64,
                subject_age=age, subject_sex=subject.sex, attempted_queries=attempted_queries)

        selected = candidates(result.records, queries)
        queried = {support for query in queries for support in query.supports}
        core = {topic.topic_id for topic in plan.topics if topic.kind in {'risk_management', 'clinical', 'function'}
            or topic.kind == 'lifestyle' and 'adult_sleep_duration_reference' in topic.source_semantics
            or topic.kind == 'disease' and any(marker in topic.source_semantics
                for marker in ('source_model', 'source_association'))
            or topic.kind == 'taxon' and topic.priority == 3}
        fallback_started = time.monotonic()
        public_recall = self.public_evidence_client is not None
        risk_topics = [topic for topic in plan.topics if topic.kind == 'risk']
        # Compatibility without derived candidates keeps the existing one
        # pass. With risks, use at most 24 of the same 32 slots here and
        # reserve eight for gaps found by the final numbered-evidence review.
        # No round receives a fresh retrieval budget or deadline.
        attempts = list(queries)
        recall_count, remaining = 0, 24 if risk_topics else 32
        risk_gaps = []
        # A clipped provider block may omit the recommendation's governing
        # population or conditions. Exact publication lookup spends at most
        # two of this same recall budget; originals are never invented or
        # replaced with AI summaries and the primary provider stays unchanged.
        hydration_queries = (truncated_original_queries(plan, selected.records,
            max_queries=min(2, remaining)) if public_recall else [])
        document._evaluation_original_hydration = []
        document._evaluation_hydrated_originals = []
        if hydration_queries and time.monotonic() < public_deadline:
            from .evaluation_originals import (
                hydrated_original_matches_publication, bind_hydrated_evaluation_publication,
                retain_new_evaluation_originals,
            )
            remaining -= len(hydration_queries)
            recall_count += len(hydration_queries)
            attempts.extend(hydration_queries)
            supplement = await _retrieve_evidence_until(self.public_evidence_client, hydration_queries,
                deadline=public_deadline, provider='public_literature')
            partition = _partition_evaluation_original_pool(document, result.records,
                supplement.records, stage='hydration_retrieval')
            result.records = list(partition.existing)
            supplement = replace(supplement, records=list(partition.candidates))
            if partition.conflict_keys:
                selected = candidates(result.records, attempts)
            accepted = []
            for record in supplement.records:
                parent = next((existing for existing in selected.records
                    if hydrated_original_matches_publication(existing, record)), None)
                bound = parent is not None and bind_hydrated_evaluation_publication(parent, record)
                document._evaluation_original_hydration.append({
                    'evidenceKey': record.evidence_key,
                    'requestedPublicationKey': parent.evidence_key if parent is not None else None,
                    'status': 'same_publication_original_retained' if bound else 'publication_or_edition_mismatch',
                    'originalLength': len(evaluation_original_text(record)),
                    'originalSha256': hashlib.sha256(evaluation_original_text(record).encode()).hexdigest(),
                    'queryIds': list(record.query_ids),
                })
                if bound and evaluation_original_text(record):
                    accepted.append(record)
            partition = _partition_evaluation_original_pool(document, result.records,
                accepted, stage='hydration_originals')
            result.records = list(partition.existing)
            accepted = retain_new_evaluation_originals(partition.existing, partition.candidates)
            document._evaluation_hydrated_originals = list(accepted)
            result = merge_retrieval_results(result, replace(supplement, records=accepted))
            selected = candidates(result.records, attempts)
            _checkpoint_evidence(progress, result, microbes=microbes, diseases=diseases)

        async def reviewed_gaps():
            if not risk_topics:
                return []
            management_gaps = evaluation_management_gaps(plan, selected)
            available = public_deadline - time.monotonic()
            if available <= 0:
                return [*management_gaps, *[{'riskId': support, 'reason': 'coverage_deadline', 'missingRole': 'risk_relationship'}
                    for topic in risk_topics for support in topic.source_support_ids]]
            try:
                gaps = await review_risk_evidence(client, document, selected.records,
                    subject={'age': age, 'gender': subject.sex},
                    deadline=min(public_deadline, time.monotonic() + 45.0))
                return [*management_gaps, *gaps]
            except Exception:
                return [*management_gaps, *[{'riskId': support, 'reason': 'coverage_review_unavailable', 'missingRole': 'risk_relationship'}
                    for topic in risk_topics for support in topic.source_support_ids]]

        risk_gaps = await reviewed_gaps()
        for round_index in range(2 if risk_topics else 1):
            if remaining <= 0 or time.monotonic() >= public_deadline:
                break
            pending = [topic_id for topic_id in queried & core if not selected.by_topic.get(topic_id)]
            for gap in risk_gaps:
                if not isinstance(gap, dict):
                    continue
                relation_id = gap.get('relationId')
                pending.extend(topic.topic_id for topic in risk_topics if (
                    relation_id and (relation_id in topic.source_support_ids
                        or topic.query_relations and topic.query_relations[0].get('relationId') == relation_id))
                    or not relation_id and gap.get('riskId') in topic.source_support_ids)
            # An existing source-function duty still receives one recall;
            # the second pass is only for a remaining actual coverage gap.
            if round_index and not pending:
                break
            round_limit = min(12 if risk_topics and round_index == 0 else remaining, remaining)
            use_public = public_recall and (round_index > 0 or not risk_topics)
            adaptive = []
            if risk_gaps:
                try:
                    async with asyncio.timeout(min(45.0, max(.001, public_deadline - time.monotonic()))):
                        adaptive = await evaluation_gap_queries(client, plan, risk_gaps,
                            max_queries=round_limit, public=use_public, previous_queries=attempts)
                except TimeoutError:
                    adaptive = []
            # Local query planning is not a network budget. Construct the
            # available duties before selecting this round's bounded calls;
            # otherwise the first 32 risk aliases hide late function topics.
            extras = evaluation_recall_queries(plan, uncovered_topic_ids=pending,
                max_queries=max(32, len(plan.topics)), public=use_public)
            fallback_queries = [replace(query, age_group=age_group) for query in
                prioritize_evaluation_recalls(plan, adaptive, extras, attempts,
                    max_queries=round_limit, round_index=round_index)]
            if not fallback_queries:
                break
            remaining -= len(fallback_queries)
            recall_count += len(fallback_queries)
            attempts.extend(fallback_queries)
            fallback = await _retrieve_evidence_until(
                self.public_evidence_client if use_public else self.evidence_client, fallback_queries,
                deadline=public_deadline, provider='public_literature' if use_public else 'evimed_v2')
            partition = _partition_evaluation_original_pool(document, result.records,
                fallback.records, stage=f'recall_{round_index + 1}_retrieval')
            result.records = list(partition.existing)
            fallback = replace(fallback, records=list(partition.candidates))
            result = merge_retrieval_results(result, fallback)
            selected = candidates(result.records, attempts)
            queried.update(support for query in fallback_queries for support in query.supports)
            risk_gaps = await reviewed_gaps()
            _checkpoint_evidence(progress, result, microbes=microbes, diseases=diseases)
        timings['public_evidence_fallback'] = _elapsed_ms(fallback_started)
        # The parent chooses remaining gaps deterministically. The private child
        # may revise searches within its budget; returned originals still pass
        # the existing candidate and final citation/claim validation gates.
        if self.research_agent is not None and research_scope and research_scope[2]:
            from modules.report.research_integration import supplement_evidence

            started = time.monotonic()
            supplement = await supplement_evidence(
                self.research_agent,
                plan,
                selected,
                scope=research_scope,
                deadline=public_deadline,
            )
            partition = _partition_evaluation_original_pool(document, result.records,
                supplement.records, stage='research_loop_originals')
            result.records = list(partition.existing)
            supplement = replace(supplement, records=list(partition.candidates))
            result = merge_retrieval_results(result, supplement)
            selected = candidates(result.records, attempts)
            timings['research_loop'] = _elapsed_ms(started)
        document._evaluation_risk_queries = tuple(attempts)
        document._evaluation_risk_recall_used = recall_count
        if progress is not None:
            progress.evaluation_queries = tuple(attempts)
            progress.evaluation_recall_used = recall_count
        result.records = list(selected.records)
        for audit in result.query_audits:
            # These are candidates; zero approved counts at this phase are
            # intentional and must not be presented as completed assessment.
            audit.interpreted_count = 0
            audit.public_count = 0
            logger.info(
                'report evaluation evidence query: intent=%s target=%r raw=%s screened=%s retained=%s rejected=%s',
                audit.intent,
                audit.target,
                audit.raw_count,
                audit.screened_count,
                audit.retained_count,
                audit.rejected_reasons,
            )
        logger.info(
            'report evaluation evidence candidates: topics=%s primary_queries=%s recall_queries=%s '
            'literal_abstract_records=%s topic_candidates=%s approved_topics=0 coverage=%s',
            len(plan.topics), len(queries), recall_count, len(result.records),
            sum(bool(keys) for keys in selected.by_topic.values()),
            json.dumps(list(selected.coverage), ensure_ascii=False),
        )
        _checkpoint_evidence(progress, result, microbes=microbes, diseases=diseases)
        return result

    async def _assess_integrated_risks(
        self, document, data, subject, client, records, summaries, references, numbers,
        deadline, timings, progress, microbes, diseases,
    ):
        """Close final risk-review gaps within the original shared recall budget."""
        from .evaluation_risk_assessment import (assess_risks, approved_assessments,
            compose_risk_summaries, refresh_risk_original_scope)
        from .evaluation_evidence_plan import evaluation_gap_queries

        started = time.monotonic()
        patient = {'gender': subject.sex, 'age': subject.age}
        await assess_risks(client, document, summaries, subject=patient)
        registry = document.risk_registry
        approved = {risk.risk_id for risk in approved_assessments(document)}
        pending = {risk.risk_id for risk in registry.candidates} - approved
        pending.update(gap['riskId'] for gap in registry.gaps
            if gap.get('missingRole') in {'management', 'risk_relationship'}
            and gap.get('riskId') in approved)
        plan = getattr(document, '_evaluation_evidence_plan', None)
        used = getattr(document, '_evaluation_risk_recall_used', 0)
        previous = list(getattr(document, '_evaluation_risk_queries', ()))
        if pending and plan is not None and client is not None and time.monotonic() < deadline and used < 32:
            gaps = [gap for gap in registry.gaps if gap.get('riskId') in pending]
            public = self.public_evidence_client is not None
            queries = await evaluation_gap_queries(client, plan, gaps,
                max_queries=min(8, 32 - used), public=public, previous_queries=previous)
            age_group = 'adult' if type(subject.age) is int and subject.age >= 18 else (
                'pediatric' if type(subject.age) is int else 'unknown')
            queries = [replace(query, age_group=age_group) for query in queries]
            if queries and time.monotonic() < deadline:
                gateway = self.public_evidence_client if public else self.evidence_client
                supplement = await _retrieve_evidence_until(gateway, queries, deadline=deadline,
                    provider='public_literature' if public else 'evimed_v2')
                from .evaluation_originals import retain_new_evaluation_originals
                partition = _partition_evaluation_original_pool(document, records,
                    supplement.records, stage='final_risk_recall_originals')
                records = list(partition.existing)
                additions = retain_new_evaluation_originals(partition.existing, partition.candidates)
                selection = None
                if additions:
                    selection = select_topic_evidence(plan, additions, per_topic_limit=2, max_records=16,
                        subject_age=subject.age if type(subject.age) is int else None,
                        subject_sex=subject.sex, attempted_queries=[*previous, *queries])
                    records = [*records, *selection.records]
                if additions or partition.conflict_keys:
                    records, references, numbers, summaries = _number_evaluation_originals(
                        document, records, stage='final_risk_recall_numbering')
                    changed_original_risks = refresh_risk_original_scope(document, summaries)
                    candidate_ids = {risk.risk_id for risk in registry.candidates}
                    affected = {risk_id for topic in plan.topics
                        if topic.kind in {'risk', 'risk_management'}
                        and selection is not None and selection.by_topic.get(topic.topic_id)
                        for risk_id in topic.source_support_ids if risk_id in candidate_ids}
                    affected.update(changed_original_risks)
                    # A genuinely applicable new source can also inform an
                    # already reviewed risk or provide counterevidence. Exact
                    # role matching above permits that reuse; unrelated pending
                    # risks and bibliography renumbering do not justify another
                    # author/repair/review cycle.
                    if affected and time.monotonic() < deadline:
                        await assess_risks(client, document, summaries, subject=patient,
                            risk_ids=affected, preserve_existing=True)
                    elif affected:
                        if 'INTEGRATED_RISK_ASSESSMENT_INCOMPLETE' not in document.incomplete:
                            document.incomplete.append('INTEGRATED_RISK_ASSESSMENT_INCOMPLETE')
                object.__setattr__(document, '_evaluation_risk_recall_used', used + len(queries))
                object.__setattr__(document, '_evaluation_risk_queries', [*previous, *queries])
                if progress is not None:
                    progress.query_audits.extend(supplement.query_audits)
                    progress.references = [reference.model_copy(deep=True) for reference in references]
                    progress.evidence_records = [record.model_copy(deep=True) for record in records]
                    progress.evaluation_recall_used = used + len(queries)
        # Chapter 2 and chapter 5 consume the same independently reviewed
        # configured-domain snapshot. These are health scores, not a
        # conversion of the microbiome source score or disease probability.
        from .evaluation_dimension_scores import assess_dimension_scores
        dimension_started = time.monotonic()
        await assess_dimension_scores(client, document, summaries, subject=patient)
        timings['dimension_assessment'] = _elapsed_ms(dimension_started)
        await compose_risk_summaries(client, document, subject=patient, evidence_summaries=summaries)
        timings['integrated_risk_assessment'] = _elapsed_ms(started)
        logger.info('report integrated risk assessment: candidates=%s approved=%s pending=%s',
            len(registry.candidates), len(approved_assessments(document)),
            len({risk.risk_id for risk in registry.candidates}
                - {risk.risk_id for risk in approved_assessments(document)}))
        return records, summaries, references, numbers

    async def _retrieve_clinical_evidence(
        self,
        document,
        subject,
        client,
        primary_deadline,
        public_deadline,
        timings,
        progress,
        microbes,
        diseases,
    ):
        """Retrieve for the new document, without promoting old disease rows.

        Preserve the existing gateways, per-query candidate screening, retries
        and deadline. Public fallback serves only uncovered current topics.
        """
        started = time.monotonic()
        await prepare_clinical_search_terms(client, document, include_existing=True)
        timings['clinical_search_terms'] = _elapsed_ms(started)
        started = time.monotonic()
        queries = clinical_queries(document, subject.age)
        demographics = {'age': subject.age, 'gender': subject.sex}
        result = await _retrieve_evidence_until(
            self.evidence_client, queries, deadline=primary_deadline, provider='evimed_v2'
        )
        _checkpoint_evidence(progress, result, microbes=microbes, diseases=diseases)
        accepted = await assess_clinical_evidence(client, result.records, document, subject=demographics)
        timings['primary_clinical_evidence'] = _elapsed_ms(started)
        # A diagnostic source does not complete the same problem's cause or
        # management search. Coverage belongs to a purpose, not just an ID.
        pending = clinical_gap_queries(document, subject.age, accepted)
        fallback_started = time.monotonic()
        if pending and self.public_evidence_client is not None:
            fallback = await _retrieve_evidence_until(
                self.public_evidence_client, pending, deadline=public_deadline, provider='public_literature'
            )
            accepted.extend(await assess_clinical_evidence(client, fallback.records, document, subject=demographics))
            result = merge_retrieval_results(result, fallback)
        # Retain all query audits, including rejection, failure and empty
        # results. Unassessed raw candidates never become public citations.
        # Check the accepted semantic records before source deduplication;
        # duplicate sources can carry complementary assessed role quotes.
        covered = clinical_evidence_coverage(accepted, document)
        result.records = merge_retrieval_results(
            EvidenceRetrievalResult(records=accepted, failed_supports=set(), query_audits=[], relation_coverage=[])
        ).records
        for audit in result.query_audits:
            logger.info(
                'report clinical evidence query: intent=%s target=%r query=%r raw=%s '
                'screened=%s retained=%s rejected=%s',
                audit.intent,
                audit.target,
                audit.query_text,
                audit.raw_count,
                audit.screened_count,
                audit.retained_count,
                audit.rejected_reasons,
            )
        if any((s, q.intent) not in covered for q in queries for s in q.supports):
            document.incomplete.append('CLINICAL_EVIDENCE_COVERAGE_INCOMPLETE')
        _checkpoint_evidence(progress, result, microbes=microbes, diseases=diseases)
        timings['public_evidence_fallback'] = _elapsed_ms(fallback_started)
        return result

    async def _generate_evaluate_payload(
        self,
        request: ReportRequest,
    ) -> Any:
        """Generate one evaluate payload under the process-local admission gate."""

        async with self._evaluate_slots:
            progress = _GenerationProgress(evaluation_mode=self.clinical_report_enabled)
            progress_token = _ACTIVE_PROGRESS.set(progress)
            metrics_token = start_report_metrics()
            reserve_seconds = _callback_reserve_seconds(self.generation_timeout_seconds)
            work_timeout = max(
                0.01,
                self.generation_timeout_seconds - reserve_seconds,
            )
            work_deadline = time.monotonic() + work_timeout
            progress.evaluation_work_deadline = work_deadline
            try:
                async with asyncio.timeout(work_timeout):
                    return await self.generate(request)
            except SourceParseError as error:
                logger.error(
                    'report source parsing failed: user_id=%s session_id=%s error_type=%s',
                    request.user_id,
                    request.session_id,
                    type(error).__name__,
                )
                return _empty_partial_payload(
                    request,
                    reasons=['MICROBIOME_SOURCE_UNAVAILABLE'],
                    evaluation_mode=self.clinical_report_enabled,
                )
            except TimeoutError:
                logger.error(
                    'report generation timed out: user_id=%s session_id=%s',
                    request.user_id,
                    request.session_id,
                )
                if progress.evaluation_report_state is not None:
                    from .evaluation_report import recover_evaluation_report
                    return await _evaluation_sync(True, recover_evaluation_report,
                        progress.evaluation_report_state, reason='REPORT_GENERATION_TIMEOUT')
                return await _evaluation_sync(
                    self.clinical_report_enabled, _recover_partial_payload,
                    request,
                    progress,
                    fallback_reason='REPORT_GENERATION_TIMEOUT',
                    deadline_reached=True,
                )
            except Exception as error:
                logger.error(
                    'report generation failed: user_id=%s session_id=%s error_type=%s',
                    request.user_id,
                    request.session_id,
                    type(error).__name__,
                )
                if progress.evaluation_report_state is not None:
                    from .evaluation_report import recover_evaluation_report
                    return await _evaluation_sync(True, recover_evaluation_report,
                        progress.evaluation_report_state, reason='REPORT_GENERATION_UNAVAILABLE')
                return await _evaluation_sync(
                    self.clinical_report_enabled, _recover_partial_payload,
                    request,
                    progress,
                    fallback_reason='REPORT_GENERATION_UNAVAILABLE',
                    deadline_reached=time.monotonic() >= work_deadline,
                )
            finally:
                _ACTIVE_PROGRESS.reset(progress_token)
                reset_report_metrics(metrics_token)

    async def execute_and_callback(
        self,
        request: ReportRequest,
        *,
        final_report_id: str | None = None,
        defer_callbacks: bool = False,
    ) -> ReportExecutionResult:
        batch_token = bind_report_batch(
            store=self.artifact_store,
            final_report_id=final_report_id,
            user_id=request.user_id,
            session_id=request.session_id,
            defer_callbacks=defer_callbacks,
        )
        try:
            return await self._execute_and_callback(request)
        finally:
            reset_report_batch(batch_token)

    async def _execute_and_callback(
        self,
        request: ReportRequest,
    ) -> ReportExecutionResult:
        payload = await self._generate_evaluate_payload(request)
        evaluate_delivery_status = 'success'

        # Construct the public contract before delivery.  Delivery is an
        # independent terminal step and must never decide whether the
        # follow-up reports are generated.
        # The public evaluate document and the existing follow-up DTO have
        # independent contracts. A display version must not select the input
        # contract of instruction/opinion/explain.
        build_followup_body = getattr(self.callback_client, 'build_followup_body', None)
        try:
            if build_followup_body is not None:
                generation_payload = build_followup_body(payload)
            elif isinstance(payload, dict) and 'report' not in payload:
                # Legacy test adapters may already return their full DTO.
                # It remains private generation input, never public fallback.
                generation_payload = copy.deepcopy(payload)
            else:
                generation_payload = _fixed_callback_body(payload)
        except Exception as error:
            logger.error('follow-up source mapping failed: error_type=%s', type(error).__name__)
            payload = FailureCallback(
                user_id=request.user_id,
                session_id=request.session_id,
                error_code='REPORT_SOURCE_MAPPING_FAILED',
                message='系统无法构造报告来源结构',
            )
            generation_payload = _fixed_callback_body(payload)
        try:
            fixed_payload = self.callback_client.build_body(payload)
        except Exception as error:
            from .evaluation_report import _safe_validation_errors, _write_private_diagnostic
            details = _safe_validation_errors(error)
            logger.error('fixed evaluate callback construction failed: error_type=%s validation_errors=%s',
                type(error).__name__, json.dumps(details, ensure_ascii=False))
            report_data = getattr(getattr(payload, 'report', None), 'evaluation_data', None)
            _write_private_diagnostic('callback-construction-failure', {
                'errorType': type(error).__name__, 'validationErrors': details,
                'workProduct': payload,
                'templateLayout': getattr(report_data, '_template_layout', None),
                'templateDimensionScores': getattr(report_data, '_template_dimension_scores', None),
            })
            # A broken adapter must never expose its unvalidated work product.
            from modules.report.evaluation_callback import build_frontend_evaluation_callback

            fixed_payload = build_frontend_evaluation_callback(
                FailureCallback(
                    user_id=request.user_id,
                    session_id=request.session_id,
                    error_code='FIXED_REPORT_CONSTRUCTION_FAILED',
                    message='系统无法构造固定报告结构',
                )
            )
        from modules.report.evaluation_request_input import (
            evaluation_callback_request_context, followup_request_context)
        fixed_payload.update(evaluation_callback_request_context(request))
        fixed_payload.update(type='evaluate', userId=request.user_id, sessionId=request.session_id)
        fixed_payload['finalReportId'] = active_final_report_id() or fixed_payload.get('finalReportId')
        generation_payload = {**generation_payload, **followup_request_context(request)}
        reviewed_source = getattr(payload, '_reviewed_seven_chapter', None)
        report_document = getattr(getattr(payload, 'report', None), 'clinical_document', None)
        if report_document is not None and report_document.background.get('reportGeneration', {}).get(
                'mainline') in {'source-first-seven-chapters-v1', 'template-cell-seven-chapters-v2'}:
            generation_payload['_seven_chapter_mainline'] = True
        if reviewed_source is not None:
            generation_payload['_reviewed_seven_chapter'] = reviewed_source
        # Source-presence checks use the complete source DTO, including normal
        # results; the public chapter layout is deliberately irrelevant here.
        source_present = _has_evaluate_source_facts(generation_payload) or _has_evaluate_source_facts(fixed_payload)
        evaluate_generation_status = (
            'failed'
            if fixed_payload.get('status') == 'failed'
            else (
                'fallback_empty'
                if fixed_payload.get('status') in {'partial_success', 'failed'} and not source_present
                else 'success'
            )
        )
        if callbacks_are_deferred():
            if fixed_payload.get('status') == 'partial_success' and not source_present:
                fixed_payload['status'] = 'failed'
                fixed_payload['data'] = {}
                fixed_payload['error'] = {
                    'errorCode': 'REPORT_GENERATION_FAILED',
                    'message': '报告未能生成有效内容',
                }
            evaluate_generation_status = fixed_payload.get('status', 'failed')
        # 先保存、再发布：保存失败不发送对应的成功回调，也不重跑解析或模型。
        save_result = await save_report_artifact(
            'evaluate',
            fixed_payload,
            generation_status=evaluate_generation_status,
        )
        if not is_deliverable(save_result):
            evaluate_delivery_status = 'not_delivered_unsaved'
            logger.warning(
                'evaluate artifact not stored (%s); success callback withheld: user_id=%s session_id=%s',
                save_result,
                request.user_id,
                request.session_id,
            )
        elif callbacks_are_deferred():
            evaluate_delivery_status = 'pending'
        else:
            try:
                if hasattr(self.callback_client, 'deliver'):
                    await self.callback_client.deliver(fixed_payload)
                else:  # Backward-compatible test doubles.
                    delivered = await self.callback_client.send(payload)
                    if isinstance(delivered, dict):
                        fixed_payload = delivered
            except Exception as error:
                evaluate_delivery_status = 'delivery_failed'
                logger.warning(
                    'evaluate callback delivery failed; follow-up generation continues: '
                    'user_id=%s session_id=%s error_type=%s',
                    request.user_id,
                    request.session_id,
                    type(error).__name__,
                )

        followup_payloads: tuple[dict[str, Any], ...] = ()
        module_statuses: dict[str, dict[str, str]] = {
            'evaluate': {
                'generation': evaluate_generation_status,
                'delivery': evaluate_delivery_status,
            }
        }
        if fixed_payload.get('status') not in {'success', 'partial_success'}:
            return ReportExecutionResult(
                evaluate_payload=fixed_payload,
                module_statuses=module_statuses,
            )
        if self.followup_report_handler is None:
            return ReportExecutionResult(
                evaluate_payload=fixed_payload,
                module_statuses=module_statuses,
            )
        try:
            followup_result = await self.followup_report_handler(
                generation_payload,
                request.user_id,
                request.session_id,
            )
            raw_payloads = getattr(followup_result, 'payloads', ())
            followup_payloads = tuple(dict(item) for item in raw_payloads if isinstance(item, dict))
            raw_statuses = getattr(followup_result, 'module_statuses', {})
            if isinstance(raw_statuses, dict):
                module_statuses.update(
                    {str(key): dict(value) for key, value in raw_statuses.items() if isinstance(value, dict)}
                )
        except Exception as error:
            logger.error(
                'follow-up report orchestration failed: user_id=%s session_id=%s error_type=%s',
                request.user_id,
                request.session_id,
                type(error).__name__,
            )
        return ReportExecutionResult(
            evaluate_payload=fixed_payload,
            followup_payloads=followup_payloads,
            module_statuses=module_statuses,
        )

    async def replay_execution(self, result: ReportExecutionResult) -> None:
        """Redeliver a completed identical request without regenerating it."""

        try:
            if hasattr(self.callback_client, 'deliver'):
                await self.callback_client.deliver(result.evaluate_payload)
            else:
                await self.callback_client.send(result.evaluate_payload)
        except Exception as error:
            logger.warning('cached evaluate callback redelivery failed: error_type=%s', type(error).__name__)
        if result.followup_payloads and self.followup_replay_handler is not None:
            try:
                await self.followup_replay_handler(result.followup_payloads)
            except Exception as error:
                logger.warning('cached follow-up callback redelivery failed: error_type=%s', type(error).__name__)

    async def _load_session(self, request: ReportRequest) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self.session_timeout_seconds):
                session = await self.session_loader(request.user_id, request.session_id)
        except TimeoutError:
            logger.warning(
                'optional session lookup timed out: user_id=%s session_id=%s',
                request.user_id,
                request.session_id,
            )
            return {}
        except Exception as error:
            logger.warning(
                'optional session lookup failed: user_id=%s session_id=%s error_type=%s',
                request.user_id,
                request.session_id,
                type(error).__name__,
            )
            return {}
        return session if isinstance(session, dict) else {}

    async def _load_group_context(self, request: ReportRequest):
        from modules.report.group_context import GroupContextResult

        if self.group_message_client is None:
            return GroupContextResult(source_errors=['GROUP_CONTEXT_UNAVAILABLE'])
        try:
            async with asyncio.timeout(min(60.0, self.generation_timeout_seconds)):
                result = await self.group_message_client.read(
                    user_id=request.user_id, group_id=request.group_id)
            if not isinstance(result, GroupContextResult):
                return GroupContextResult(source_errors=['GROUP_RESPONSE_INVALID'])
            return result
        except Exception as error:
            logger.warning('report group context unavailable: error_type=%s', type(error).__name__)
            return GroupContextResult(source_errors=['GROUP_CONTEXT_UNAVAILABLE'])


def _relation_record_is_public(record: Any, microbes: Any, *, subject: Any | None = None) -> bool:
    interpretation = record.interpretation
    if (
        interpretation is None
        or interpretation.target_alignment != 'direct'
        # Missing population/sample detail is a soft limitation. Exact
        # taxon-outcome evidence may remain visible at a lower evidence grade;
        # only an explicit mismatch is excluded below or by ``not_applicable``.
        or interpretation.patient_applicability == 'not_applicable'
    ):
        return False
    restricted_population_context = ' '.join(
        str(value or '')
        for value in (
            record.title,
            record.source,
            interpretation.population,
            interpretation.claim,
        )
    ).casefold()
    demographic_context = ' '.join(
        str(value or '')
        for value in (
            record.title,
            record.source,
            interpretation.population,
            interpretation.claim,
        )
    ).casefold()
    subject_age = getattr(subject, 'age', None)
    subject_sex = (getattr(subject, 'sex', None) or '').strip()
    if (
        subject_age is not None
        and subject_age >= 18
        and any(
            marker in demographic_context
            for marker in (
                'newborn',
                'neonatal',
                'infant',
                'pediatric',
                'paediatric',
                'children',
                'childhood',
                '新生儿',
                '婴幼儿',
                '婴儿',
                '儿童',
                '小儿',
                '患儿',
            )
        )
    ):
        return False
    if (
        subject_age is not None
        and subject_age < 65
        and any(marker in demographic_context for marker in ('elderly', 'older adults', '老年'))
    ):
        return False
    if subject_sex == '男' and any(
        marker in demographic_context
        for marker in (
            'pregnan',
            'preterm birth',
            'premature birth',
            'vulvovaginal',
            'women with',
            'female patient',
            '孕妇',
            '妊娠',
            '妇女',
            '外阴阴道',
            '阴道白念珠菌',
            '复发性念珠菌性阴道炎',
            '早产',
            '早产女性',
        )
    ):
        return False
    if any(
        marker in restricted_population_context
        for marker in (
            'covid-19',
            'coronavirus',
            'immune checkpoint',
            'immunotherapy',
            '新冠',
            '免疫检查点抑制剂',
            '免疫治疗',
            'transplant patient',
            'transplant recipient',
            '移植患者',
            '移植受者',
            'iga deficiency',
            'selective iga deficiency',
            '选择性iga缺乏',
            'iga缺乏',
            'hiv',
            'aids',
            '艾滋',
            'immunocompromised',
            'immunodeficient',
            'immune-deficient',
            '免疫缺陷',
            '免疫受损',
            '免疫功能低下',
            '免疫功能受损',
            'thalassemia',
            '地中海贫血',
            'injection drug user',
            'injecting drug user',
            '注射吸毒',
            'vietnamese patient',
            'vietnamese',
            'iraqi kurdish',
            'solid tumor',
            '实体瘤',
            'oncology patient',
            'cancer patient',
            '肿瘤科患者',
            '肿瘤患者',
            'healthcare worker',
            'health care worker',
            '医护人员',
            'hematologic malignan',
            '恶性血液病',
            'dialysis patient',
            '透析患者',
            '血透患者',
            'hemodialysis patient',
            'haemodialysis patient',
            'prosthetic valve',
            'artificial valve',
            '人工瓣膜',
            'intensive care',
            'critical care',
            'coronary care',
            'icu patient',
            '重症监护',
            '冠心病监护',
            'ventilator-associated',
            'mechanically ventilated',
            '呼吸机相关',
            'nosocomial pneumonia',
            'hospital-acquired pneumonia',
            '院内肺炎',
            '医院获得性肺炎',
            'in japan',
            '日本菌血症',
            '日本旅行者',
            'western africa',
            'west africa',
            '非洲西部',
            '西非',
            'immune reconstitution',
            '免疫重建炎症',
            'in the tropics',
            '热带地区',
            'hospital setting',
            '医院环境',
            'cystic fibrosis',
            '囊性纤维化',
        )
    ):
        return False
    source_context = f'{record.title} {record.summary or ""}'.casefold()
    claim_context = interpretation.claim.casefold()
    if any(
        marker in source_context
        for marker in (
            'esbl',
            'extended-spectrum beta-lactamase',
            'extended spectrum beta-lactamase',
            '产超广谱β-内酰胺酶',
            '产超广谱β内酰胺酶',
        )
    ):
        return False
    if any(
        marker in source_context
        for marker in (
            'oral leukoplakia',
            'candida leukoplakia',
            '口腔白斑',
        )
    ):
        return False
    if re.search(r'(?:不是|并非)[^。；;]{0,24}(?:病因|原因|相关)', claim_context):
        return False
    if interpretation.study_design == '病例报告':
        return False
    case_context = ' '.join(
        str(value or '')
        for value in (
            record.title,
            record.source,
            interpretation.claim,
            interpretation.limitations,
        )
    ).casefold()
    if any(
        marker in case_context
        for marker in (
            'case report',
            'case-report',
            'rare case',
            'single case',
            '病例报告',
            '罕见病例',
            '单个病例',
            '一例',
            '1例',
            '个案',
        )
    ):
        return False
    if any(
        marker in claim_context
        for marker in (
            'not a cause',
            'not associated',
            'no association',
            'no significant association',
            '不是……的原因',
            '无相关性',
            '未见相关',
        )
    ):
        return False
    if any(
        marker in record.title.casefold()
        for marker in (
            'nanoparticle',
            'plasmonic detection',
            'rapid detection',
            'biosensor',
            'assay development',
            'genetic transformation',
            'molecular method',
            'methods in molecular biology',
            'laboratory protocol',
            'experimental protocol',
            'cloning vector',
            'structural characterization',
            'first description of dual-species',
            '纳米颗粒',
            '快速检测方法',
            '检测方法的建立',
            '分子生物学方法',
            '实验方法',
            '实验室方案',
            '克隆载体',
            '结构表征',
        )
    ):
        return False
    has_preclinical_source_marker = any(
        marker in source_context
        for marker in (
            'epithelial cell',
            'cell line',
            'in vitro',
            'in-vitro',
            '体内外实验',
            'in silico',
            'in-silico',
            'nematode',
            'animal model',
            'animal experiment',
            '细胞实验',
            '细胞研究',
            '细胞系',
            '线虫',
            '动物模型',
            '动物实验',
            'poultry',
            'chicken',
            'broiler',
            'avian model',
            'plant pathogen',
            'plant disease',
            'crop disease',
            'wheat rust',
            'agricultural pathogen',
            '家禽',
            '禽类',
            '鸡模型',
            '植物病原',
            '植物病害',
            '作物病害',
            '小麦锈病',
        )
    )
    synthesis_metadata = ' '.join(record.study_types).casefold()
    synthesis_has_exact_relation = (record.category == 'guideline' or 'review' in synthesis_metadata) and any(
        _source_explicitly_supports_relation(record, binding) for binding in record.relation_bindings
    )
    # A mixed paper can report an exact human fecal depletion result first and
    # use cells or mice only for the subsequent mechanism experiment.  Keep
    # the human association while continuing to exclude the mechanism itself;
    # exact taxon/outcome binding, human/gut wording and structured direction
    # are all mandatory, so an animal-only study cannot use this exception.
    source_bound_human_abundance = bool(
        interpretation.direction in {'increased', 'decreased'}
        and any(
            marker in source_context
            for marker in (
                'human',
                'patient',
                'participant',
                'cohort',
                'case-control',
                'cross-sectional',
                '患者',
                '人群',
                '队列',
            )
        )
        and any(
            marker in source_context
            for marker in (
                'gut',
                'intestinal',
                'stool',
                'fecal',
                'faecal',
                '肠道',
                '粪便',
            )
        )
        and any(
            binding.relation_type == 'epidemiologic_association'
            and _source_explicitly_supports_relation(record, binding)
            for binding in record.relation_bindings
        )
    )
    if has_preclinical_source_marker and not (synthesis_has_exact_relation or source_bound_human_abundance):
        return False
    if any(
        marker in source_context
        for marker in (
            'biofilm formation',
            'surfaces used in the food industry',
            'functional insights into',
            'modular lpmo',
            'avirulence phenotype',
            'dna marker',
            'molecular ecology',
            '生物膜形成模型',
            '食品工业表面',
            '无毒表型',
            'dna标记',
            'fecal microbial transplant failure',
            'faecal microbial transplant failure',
            'transplant failure',
            'donor-derived',
            'economic analysis',
            'cost-effectiveness',
            'cost effectiveness',
            'empiric versus diagnostic',
            '粪菌移植失败',
            '供者来源',
            '经济学分析',
            '成本效果',
            '成本效益',
            'gene set enrichment analysis',
            'evolutionary dynamics',
            '基因集富集分析',
            '进化动力学',
            'carbapenem resistance',
            'carbapenems resistance',
            'practical guide to the treatment',
            '碳青霉烯耐药',
            '治疗实用指南',
            'oral-gut axis',
            'therapeutic implications',
            '口腔-肠道轴',
            '口肠轴',
            '治疗意义',
            '抗菌素耐药',
            'growth inhibitory activity',
            'plant extract',
            'prevention and treatment of',
            'capsule production',
            '荚膜表达',
            '培养条件',
            'antibodies in',
            'antibody response',
            'immune response to',
            '抗体检测',
            '免疫应答',
            'vaccine candidate',
            '疫苗候选',
            '疫苗立场',
            'retention of medical devices',
            'medical device',
            '医疗器械留置',
            '留置医疗器械',
            'sinus and orbit',
            '鼻窦和眼眶',
            'in air and relationship',
            '空气中的曲霉',
            '肺部隐球菌感染',
            '临床检验规程',
        )
    ):
        return False
    if any(
        marker in source_context
        for marker in (
            '中医辨证',
            '中医证候',
            '治法方药',
            '方药共识',
        )
    ):
        return False
    action_markers = (
        'treatment',
        'therapy',
        'management',
        'fecal microbiota transplantation',
        'faecal microbiota transplantation',
        'fecal microbial transplant',
        'faecal microbial transplant',
        'antimicrobial activity',
        'antimicrobial treatment',
        'efficacy of',
        'mouthwash',
        '治疗',
        '管理',
        '粪菌移植',
        '抗菌活性',
        '疗效',
        '漱口水',
    )
    explicit_relation_markers = (
        'causative agent',
        'caused by',
        'cause of',
        'is the cause',
        'toxin produced by',
        'neurotoxin produced by',
        'causes ',
        'responsible for',
        'pathogen',
        'pathogenic',
        'associated with',
        'association with',
        '引起',
        '导致',
        '病原',
        '致病',
        '相关',
        '关联',
    )
    if any(marker in source_context for marker in action_markers) and not any(
        marker in source_context for marker in explicit_relation_markers
    ):
        source_abundance_relation = interpretation.direction in {'increased', 'decreased'} and any(
            binding.relation_type == 'epidemiologic_association'
            and _source_explicitly_supports_relation(record, binding)
            and _source_relation_direction(record, binding) == interpretation.direction
            for binding in record.relation_bindings
        )
        exact_diagnostic_relation = any(marker in source_context for marker in ('diagnos', '诊断')) and any(
            binding.microbe_name.casefold() in source_context for binding in record.relation_bindings
        )
        if not exact_diagnostic_relation and not source_abundance_relation:
            return False
    if (
        record.relation_bindings
        and all(binding.relation_type == 'epidemiologic_association' for binding in record.relation_bindings)
        and any(
            marker in source_context
            for marker in (
                'post-neurosurgery',
                'post neurosurgery',
                'open fracture',
                'root canal',
                'periodontitis',
                'keratitis',
                'penile skin',
                '神经外科术后',
                '开放性骨折',
                '根管感染',
                '牙周炎',
                '角膜炎',
                '阴茎皮肤',
            )
        )
    ):
        return False
    excluded_site_outcomes = (
        '社区获得性肺炎',
        '嗜肺军团菌肺炎',
        '小脑功能障碍',
        '无创伤性脾破裂',
        '非创伤性脾破裂',
        '根管感染',
        '手术部位感染',
        '根尖周疾病',
        '牙周炎',
        '牙周病',
        '阴茎皮肤感染',
        '复杂性皮肤和软组织感染',
        '复杂性皮肤软组织感染',
        '脂溢性皮炎',
        '帕金森病',
        '头皮屑',
        '头屑',
        '花斑癣',
        '马拉色菌毛囊炎',
        '真菌性角膜炎',
        '眼部感染',
        '骨炎',
        '硬膜外脓肿',
        '脊椎炎',
        '脊柱炎',
        '椎间盘炎',
        '椎体炎',
        '牙髓感染',
        '义齿性口炎',
        '口腔白斑',
        '糖尿病足感染',
        '创伤伤口感染',
        '创面感染',
        '甲真菌病',
        '免疫重建炎症综合征',
        '侵袭性肺曲霉病',
        '支气管扩张',
        '真菌致敏',
        '过敏性支气管肺曲霉病',
        '重症哮喘伴真菌致敏',
        '角膜炎',
        '口腔念珠菌病',
        '食管念珠菌病',
        '性传播感染',
        '性传播疾病',
        '念珠菌性食管炎',
        '胆道感染',
        '皮肤和软组织感染',
        '口腔白斑病',
        '呼吸机相关性肺炎',
        '院内肺炎',
        '医院获得性肺炎',
        '导管相关血流感染',
        '慢性肺曲霉病',
        '慢性呼吸系统疾病',
        '侵袭性真菌病（鼻窦和眼眶）',
        '鼻窦和眼眶',
    )
    excluded_generic_outcomes = (
        '抗菌素耐药革兰氏阴性菌感染',
        '抗菌药物耐药革兰阴性菌感染',
        '胃肠道疾病',
        '发病率和死亡率',
        '发病率与死亡率',
        '潜在人类病原体',
        '机会性病原体',
        '医疗相关感染',
    )
    record.relation_bindings = [
        binding
        for binding in record.relation_bindings
        if not any(marker in binding.disease_name for marker in (*excluded_site_outcomes, *excluded_generic_outcomes))
        and not (binding.disease_name == '肺炎' and 'candida' in binding.microbe_name.casefold())
    ]
    microbes_by_id = {item.microbe_id: item for item in microbes.items}
    for binding in record.relation_bindings:
        microbe = microbes_by_id.get(binding.microbe_id)
        if microbe is None:
            return False
        expected_alignment = 'species' if len(microbe.scientific_name.split()) >= 2 else 'genus'
        if interpretation.taxon_alignment != expected_alignment:
            return False
    return bool(record.relation_bindings)


def _microbe_record_is_public(record: Any, microbes: Any) -> bool:
    """Reject strain/toxin or genus/species mismatches for every microbe citation."""
    microbe_intents = {
        'microbe_discovery',
        'microbe_clinical_significance',
        'microbe_association',
        'microbe_disease_association',
        'gut_function',
        'gut_barrier',
    }
    if not (set(record.intents) & microbe_intents):
        return True
    interpretation = record.interpretation
    if interpretation is None or not record.microbe_ids:
        return False
    microbes_by_id = {item.microbe_id: item for item in microbes.items}
    source_text = f'{record.title} {record.summary or ""}'
    for microbe_id in record.microbe_ids:
        microbe = microbes_by_id.get(microbe_id)
        if microbe is None:
            return False
        expected_alignment = 'species' if len(microbe.scientific_name.split()) >= 2 else 'genus'
        if interpretation.taxon_alignment != expected_alignment:
            return False
        if expected_alignment == 'species':
            genus = re.escape(microbe.scientific_name.split()[0])
            same_genus_species = {
                match.casefold()
                for match in re.findall(
                    rf'\b{genus}\s+[a-z][a-z-]+\b',
                    source_text,
                    flags=re.IGNORECASE,
                )
            }
            if (
                len(same_genus_species) > 1
                and microbe.scientific_name.casefold() not in record.title.casefold()
                and not any(
                    binding.microbe_id == microbe_id and _source_explicitly_supports_relation(record, binding)
                    for binding in record.relation_bindings
                )
            ):
                return False
    return True


def _eligible_public_records(
    records: list[Any],
    *,
    microbes: Any,
    subject: Any,
) -> list[Any]:
    """Apply the patient-facing hard gates to interpreted evidence records."""

    microbe_intents = {
        'microbe_discovery',
        'microbe_clinical_significance',
        'microbe_association',
        'microbe_disease_association',
    }
    direct_only_intents = {
        'diagnostic_guideline',
        'health_management',
        'intervention',
        'gut_function',
        'gut_barrier',
    }
    return [
        record
        for record in records
        if record.interpretation is not None
        and not is_preclinical_evidence(record)
        and not is_likely_single_case(record)
        and record.interpretation.target_alignment != 'not_applicable'
        and record.interpretation.patient_applicability != 'not_applicable'
        and _record_matches_subject_demographics(record, subject=subject)
        and not (record.interpretation.study_design == '病例报告' and bool(set(record.intents) & microbe_intents))
        and _microbe_record_is_public(record, microbes)
        and (not record.relation_bindings or _relation_record_is_public(record, microbes, subject=subject))
        and (
            'disease_clinical_significance' not in record.intents or record.interpretation.target_alignment == 'direct'
        )
        and (not set(record.intents) <= direct_only_intents or record.interpretation.target_alignment == 'direct')
    ]


def _record_matches_subject_demographics(record: Any, *, subject: Any) -> bool:
    """Reject explicit patient-population conflicts for every public intent.

    ``contextual`` remains a soft grade when details are absent. This function
    handles only a source that explicitly restricts itself to a population the
    current subject cannot belong to, including a different age/sex group or a
    narrow comorbidity cohort being reused as general evidence.
    """

    interpretation = record.interpretation
    if interpretation is None:
        return False
    context = ' '.join(
        str(value or '')
        for value in (
            record.title,
            (record.summary or '')[:800],
            interpretation.population,
            interpretation.applicability,
        )
    ).casefold()
    # Sex gates must describe the population to which the *whole source*
    # applies.  A multi-variant guideline may discuss pregnancy or another
    # sex-specific subgroup in its summary while also giving recommendations
    # for the general adult population.  Treating any such mention as an
    # exclusive population incorrectly removed otherwise applicable guidance
    # (for example the ACR urolithiasis imaging guideline for an adult man).
    demographic_scope_context = ' '.join(
        str(value or '')
        for value in (
            record.title,
            interpretation.population,
            interpretation.applicability,
        )
    ).casefold()
    age = subject.get('age') if isinstance(subject, dict) else getattr(subject, 'age', None)
    sex_value = subject.get('sex') if isinstance(subject, dict) else getattr(subject, 'sex', None)
    sex = str(sex_value or '').strip()
    pediatric_markers = (
        'newborn',
        'neonatal',
        'infant',
        'pediatric',
        'paediatric',
        'children',
        'childhood',
        'adolescent',
        '新生儿',
        '婴幼儿',
        '婴儿',
        '儿童',
        '小儿',
        '患儿',
        '青少年',
    )
    older_markers = ('elderly', 'older adults', 'geriatric', '老年', '老年人')
    adult_markers = (
        'adult',
        'middle-aged',
        'middle aged',
        'aged 18',
        '18 years',
        '成人',
        '成年人',
        '中年',
    )
    if (
        age is not None
        and age >= 18
        and any(_contains_population_marker(context, marker) for marker in pediatric_markers)
        and not any(_contains_population_marker(context, marker) for marker in adult_markers)
    ):
        return False
    if age is not None and age < 65 and any(_contains_population_marker(context, marker) for marker in older_markers):
        return False
    if age is not None:
        lower_age_limits = [
            int(value)
            for value in re.findall(
                r'(?:aged?|age|\u5e74\u9f84)\s*(\d{1,3})\s*(?:\u5c81)?\s*'
                r'(?:\+|and older|or older|\u4ee5\u4e0a)',
                context,
            )
        ]
        if lower_age_limits and age < min(lower_age_limits):
            return False
    if sex == '\u7537' and any(
        marker in demographic_scope_context
        for marker in (
            'pregnan',
            'preterm birth',
            'vulvovaginal',
            'women with',
            'female patient',
            '\u5b55\u5987',
            '\u598a\u5a20',
            '\u5987\u5973',
            '\u5973\u6027\u60a3\u8005',
            '\u5916\u9634\u9634\u9053',
            '\u65e9\u4ea7',
        )
    ):
        return False
    if sex == '\u5973' and any(
        marker in demographic_scope_context
        for marker in (
            'male patient',
            'men with',
            'prostate',
            '\u7537\u6027\u60a3\u8005',
            '\u524d\u5217\u817a',
        )
    ):
        return False

    # Procedure, treatment and critical-care cohorts are never a substitute
    # for general patient evidence. Disease-defined cohorts are allowed only
    # when that same condition is the requested disease target.
    target_context = ' '.join(str(target or '') for target in record.targets).casefold()
    # A review can explicitly describe the *absence* of a comorbidity, for
    # example ``HIV-negative patients with M. kansasii pulmonary disease``.
    # Matching the bare token ``hiv`` in that phrase reverses its meaning and
    # incorrectly rejects otherwise general clinical evidence.  Remove only
    # explicit negated cohort phrases before applying the narrow-population
    # gate; keep the original context for age/sex and all other checks.
    restriction_context = context
    for negated_cohort_pattern in (
        r'\bwithout\s+cystic\s+fibrosis(?:\s+(?:or|and)\s+(?:hiv|human\s+immunodeficiency\s+virus)(?:\s+infection)?)?\b',
        r'\b(?:hiv|human\s+immunodeficiency\s+virus)[-\s]+negative\b',
        r'\bnon[-\s]+(?:hiv|human\s+immunodeficiency\s+virus)\b',
        r'\bwithout\s+(?:hiv|human\s+immunodeficiency\s+virus)(?:\s+infection)?\b',
    ):
        restriction_context = re.sub(
            negated_cohort_pattern,
            ' ',
            restriction_context,
        )
    always_restricted_cohorts = (
        'immune checkpoint',
        'immunotherapy',
        '\u514d\u75ab\u68c0\u67e5\u70b9\u6291\u5236\u5242',
        '\u514d\u75ab\u6cbb\u7597',
        'dialysis patient',
        'hemodialysis patient',
        'haemodialysis patient',
        '\u900f\u6790\u60a3\u8005',
        '\u8840\u900f\u60a3\u8005',
        'intensive care',
        'icu patient',
        '\u91cd\u75c7\u76d1\u62a4',
        'postoperative',
        'post-operative',
        'after surgery',
        'anastomosis',
        '\u672f\u540e',
        '\u543b\u5408\u53e3',
        'diabetic kidney disease',
        'diabetic nephropathy',
        '\u7cd6\u5c3f\u75c5\u80be\u810f\u75c5',
        '\u7cd6\u5c3f\u75c5\u80be\u75c5',
    )
    if any(marker in context for marker in always_restricted_cohorts):
        return False
    target_sensitive_cohorts = (
        (('covid-19', '\u65b0\u51a0'), ('covid', '\u65b0\u51a0')),
        (
            (
                'transplant recipient',
                'transplant patient',
                '\u79fb\u690d\u53d7\u8005',
                '\u79fb\u690d\u60a3\u8005',
            ),
            ('transplant', '\u79fb\u690d'),
        ),
        (('hiv', 'aids', '\u827e\u6ecb'), ('hiv', 'aids', '\u827e\u6ecb')),
        (
            (
                'immunocompromised',
                'immunodeficient',
                '\u514d\u75ab\u7f3a\u9677',
                '\u514d\u75ab\u529f\u80fd\u4f4e\u4e0b',
            ),
            ('immunodeficiency', '\u514d\u75ab\u7f3a\u9677'),
        ),
        (
            ('oncology patient', 'cancer patient', '\u80bf\u7624\u60a3\u8005'),
            ('cancer', 'carcinoma', '\u764c', '\u80bf\u7624'),
        ),
        (
            ('cystic fibrosis', '\u56ca\u6027\u7ea4\u7ef4\u5316'),
            ('cystic fibrosis', '\u56ca\u6027\u7ea4\u7ef4\u5316'),
        ),
        (
            ('sickle cell disease', '\u9570\u72b6\u7ec6\u80de\u75c5'),
            ('sickle cell', '\u9570\u72b6\u7ec6\u80de\u75c5'),
        ),
        (
            (
                'psychiatric patient',
                'psychiatric patients',
                '\u7cbe\u795e\u75c5\u4eba',
                '\u7cbe\u795e\u75c5\u60a3\u8005',
            ),
            ('psychiatric', '\u7cbe\u795e\u75c5'),
        ),
    )
    for cohort_markers, matching_targets in target_sensitive_cohorts:
        if any(marker in restriction_context for marker in cohort_markers) and not any(
            marker in target_context for marker in matching_targets
        ):
            return False
    return True


def _contains_population_marker(context: str, marker: str) -> bool:
    """Match demographic words without reading Latin taxa as age groups.

    ``Bifidobacterium adolescentis`` previously matched the substring
    ``adolescent`` and made valid adult-cohort evidence look pediatric-only.
    English population terms therefore require non-letter boundaries; Chinese
    markers remain ordinary phrases.
    """

    if marker.isascii():
        return bool(
            re.search(
                rf'(?<![A-Za-z]){re.escape(marker)}(?![A-Za-z])',
                context,
                flags=re.IGNORECASE,
            )
        )
    return marker in context


def _apply_subject_demographic_gates(
    records: list[Any],
    *,
    subject: Any,
) -> None:
    """Persist explicit conflicts before coverage and fallback selection."""

    for record in records:
        interpretation = record.interpretation
        if interpretation is None:
            continue
        if _record_matches_subject_demographics(record, subject=subject):
            continue
        interpretation.patient_applicability = 'not_applicable'
        for binding in record.relation_bindings:
            binding.patient_applicability = 'not_applicable'


def _recover_public_eligible_interpretations(
    *,
    diseases: Any,
    records: list[Any],
    subject: Any,
) -> None:
    """Run idempotent source-bound recovery before public coverage checks."""

    recover_exact_relation_interpretations(records)
    recover_source_bound_topic_interpretations(records)
    recover_source_bound_diagnostic_interpretations(
        diseases,
        records,
        subject_age=subject.age,
    )
    _apply_subject_demographic_gates(records, subject=subject)


def _checkpoint_evidence(
    progress: _GenerationProgress | None,
    result: EvidenceRetrievalResult,
    *,
    microbes: Any,
    diseases: Any,
) -> None:
    """Atomically retain the last completed evidence stage for deadline use."""

    if progress is None:
        return
    progress.evidence_records = [record.model_copy(deep=True) for record in result.records]
    progress.failed_supports = set(result.failed_supports)
    progress.query_audits = [audit.model_copy(deep=True) for audit in result.query_audits]
    progress.relation_coverage = [item.model_copy(deep=True) for item in result.relation_coverage]
    progress.stage_outcomes = _stage_outcomes(
        progress.query_audits,
        microbes=microbes,
        diseases=diseases,
    )


def _partition_evaluation_original_pool(document, existing, candidates=(), *, stage):
    """Quarantine every conflicting key for this request before source reuse.

    The original objects remain private audit evidence. A later retrieval
    cannot reintroduce one side of an already ambiguous semantic key.
    """
    partition = partition_evaluation_originals(existing, candidates)
    blocked = set(getattr(document, '_evaluation_quarantined_evidence_keys', ()))
    blocked.update(partition.conflict_keys)
    additional = [record for record in (*partition.existing, *partition.candidates)
        if record.evidence_key in blocked]
    affected = set(partition.conflict_keys) | {record.evidence_key for record in additional}
    if not affected:
        return partition
    quarantined = tuple([*partition.quarantined, *additional])
    prior_originals = list(getattr(document, '_evaluation_quarantined_originals', ()))
    seen = {id(record) for record in prior_originals}
    for record in quarantined:
        if id(record) not in seen:
            prior_originals.append(record)
            seen.add(id(record))
    object.__setattr__(document, '_evaluation_quarantined_evidence_keys', frozenset(blocked))
    object.__setattr__(document, '_evaluation_quarantined_originals', tuple(prior_originals))
    gaps = [dict(gap) for gap in partition.gaps]
    for key in sorted(affected - set(partition.conflict_keys)):
        gaps.append({'missingRole': 'evidence_provenance',
            'reason': 'semantic_key_publication_conflict', 'evidenceKey': key,
            'recordCount': sum(record.evidence_key == key for record in additional),
            'previouslyQuarantined': True})
    history = list(getattr(document, '_evaluation_original_provenance_diagnostics', ()))
    history.append({'stage': stage, 'conflictKeys': sorted(affected),
        'quarantinedRecordCount': len(quarantined), 'gaps': gaps})
    object.__setattr__(document, '_evaluation_original_provenance_diagnostics', tuple(history))
    if 'EVIDENCE_PROVENANCE_CONFLICT' not in document.incomplete:
        document.incomplete.append('EVIDENCE_PROVENANCE_CONFLICT')
    logger.warning('report evidence provenance quarantine: stage=%s keys=%s records=%s',
        stage, len(affected), len(quarantined))
    return replace(partition,
        existing=tuple(record for record in partition.existing if record.evidence_key not in blocked),
        candidates=tuple(record for record in partition.candidates if record.evidence_key not in blocked),
        quarantined=quarantined, conflict_keys=tuple(sorted(affected)), gaps=tuple(gaps))


def _number_evaluation_originals(document, records, *, stage):
    """Build one clean numbered original snapshot without choosing bad provenance."""
    partition = _partition_evaluation_original_pool(document, records, stage=stage)
    records = list(partition.existing)
    citation_records, publication_keys = evaluation_citation_records(records)
    references, citation_numbers = number_evidence(citation_records)
    numbers = {key: citation_numbers[publication] for key, publication in publication_keys.items()}
    summaries = build_evaluation_evidence_summaries(records, numbers)
    return records, references, numbers, summaries


def _attach_function_barrier_refs(
    microbes: Any,
    records: list[Any],
    numbers: dict[str, int],
) -> None:
    """Attach source-bound function claims without creating disease rows."""

    by_microbe: dict[str, list[Any]] = {}
    for record in records:
        if not set(record.intents) & {'gut_function', 'gut_barrier'}:
            continue
        interpretation = record.interpretation
        if (
            interpretation is None
            or interpretation.target_alignment != 'direct'
            or interpretation.patient_applicability == 'not_applicable'
            or record.evidence_key not in numbers
        ):
            continue
        for microbe_id in record.microbe_ids:
            by_microbe.setdefault(microbe_id, []).append(record)

    for microbe in microbes.items:
        selected = by_microbe.get(microbe.microbe_id, [])[:2]
        if not selected:
            continue
        claims = [
            ' '.join(record.interpretation.claim.split()).rstrip('。')
            for record in selected
            if record.interpretation is not None and record.interpretation.claim.strip()
        ]
        if not claims:
            continue
        microbe.functional_interpretation = '；'.join(dict.fromkeys(claims)) + '。'
        microbe.functional_evidence_refs = sorted(
            {numbers[record.evidence_key] for record in selected if record.evidence_key in numbers}
        )
        base = (microbe.medical_interpretation or '').rstrip('。')
        functional = microbe.functional_interpretation.rstrip('。')
        microbe.medical_interpretation = f'{base}。{functional}。' if base else f'{functional}。'
        microbe.interpretation_evidence_refs = sorted(
            set(microbe.interpretation_evidence_refs + microbe.functional_evidence_refs)
        )
        microbe.evidence_refs = sorted(set(microbe.evidence_refs + microbe.functional_evidence_refs))


def _deadline_partial_payload(
    request: ReportRequest,
    progress: _GenerationProgress,
    *,
    fallback_reason: str | None = None,
    deadline_reached: bool = True,
) -> SuccessCallback:
    """Finalize the last source checkpoint with its actual terminal cause."""

    parsed = progress.parsed
    microbes = progress.microbes.model_copy(deep=True)
    diseases = progress.diseases.model_copy(deep=True)
    health = progress.health.model_copy(deep=True)
    clinical_document = progress.clinical_document.model_copy(deep=True) if progress.clinical_document is not None else None
    if clinical_document is not None:
        from .evaluation_risk_source_links import transfer_evaluation_source_links
        transfer_evaluation_source_links(progress.clinical_document, clinical_document)
    references: list[Any] = []
    query_audits = [item.model_copy(deep=True) for item in progress.query_audits]
    relation_coverage = [item.model_copy(deep=True) for item in progress.relation_coverage]

    public_records = []
    if clinical_document is not None:
        # Keep the exact numbered, assessed snapshot used to compose the
        # clinical document. Never re-run legacy microbiome promotion here.
        references = [item.model_copy(deep=True) for item in progress.references]
        references = retain_referenced_evidence(
            references,
            microbes=microbes,
            diseases=diseases,
            health=health,
            clinical_document=clinical_document,
        )
        executive_summary = None
    elif progress.evidence_records:
        # A deadline can land between any two external calls. Rebuild all
        # public bindings from the most recent completed checkpoint instead of
        # returning a structurally valid but clinically empty report.
        _clear_evidence_bound_content(microbes, diseases)
        records = [record.model_copy(deep=True) for record in progress.evidence_records]
        promote_discovery_conditions_to_bindings(records, microbes)
        validate_relation_bindings(records)
        recover_exact_relation_interpretations(records)
        recover_source_bound_topic_interpretations(records)
        recover_source_bound_diagnostic_interpretations(
            diseases,
            records,
            subject_age=parsed.subject.age,
        )
        validate_relation_bindings(records)
        public_records = _eligible_public_records(
            records,
            microbes=microbes,
            subject=parsed.subject,
        )
        checkpoint_result = EvidenceRetrievalResult(
            records=records,
            failed_supports=set(progress.failed_supports),
            query_audits=query_audits,
            relation_coverage=relation_coverage,
        )
        update_query_audit_counts(
            checkpoint_result,
            public_records=public_records,
        )
        citation_records = consolidate_public_records([record.model_copy(deep=True) for record in public_records])
        references, citation_numbers = number_evidence(citation_records)
        numbers = citation_numbers_for_semantic_records(
            public_records,
            citation_records,
            citation_numbers,
        )
        attach_evidence_refs(microbes, diseases, public_records, numbers)
        promote_evidence_disease_associations(diseases, microbes)
        _attach_function_barrier_refs(microbes, public_records, numbers)
        attach_suggested_evaluations(
            diseases,
            public_records,
            numbers,
            subject_age=parsed.subject.age,
        )
        ensure_source_fact_row_narratives(
            microbes=microbes,
            diseases=diseases,
        )
        executive_summary = apply_extractive_health_fallback(
            health=health,
            evidence_summaries=_evidence_summaries(public_records, numbers),
            microbes=microbes,
            diseases=diseases,
            hospital_data=request.hospital_data,
            health_assessment=progress.health_assessment,
        )
        references = retain_referenced_evidence(
            references,
            microbes=microbes,
            diseases=diseases,
            health=health,
        )
        query_audits = checkpoint_result.query_audits
        relation_coverage = checkpoint_result.relation_coverage
    elif progress.references:
        # Compatibility path for a deadline after the old final reference
        # checkpoint but before callback construction.
        references = [item.model_copy(deep=True) for item in progress.references]
        references = retain_referenced_evidence(
            references,
            microbes=microbes,
            diseases=diseases,
            health=health,
        )
        executive_summary = apply_extractive_health_fallback(
            health=health,
            evidence_summaries=[],
            microbes=microbes,
            diseases=diseases,
            hospital_data=request.hospital_data,
            health_assessment=progress.health_assessment,
        )
    else:
        _clear_evidence_bound_content(microbes, diseases)
        ensure_source_fact_row_narratives(
            microbes=microbes,
            diseases=diseases,
        )
        health.recommendations = []
        health.health_level = None
        health.health_profile = None
        executive_summary = None
    reasons = [fallback_reason] if fallback_reason else []
    if deadline_reached:
        reasons.append('GENERATION_DEADLINE_REACHED')
    if any(item.matches_source_summary is False for item in parsed.panel_counts):
        reasons.append('SOURCE_COUNT_MISMATCH')
    if clinical_document is None:
        reasons.extend(
            _content_completeness_reasons(
                microbes=microbes,
                diseases=diseases,
                query_audits=query_audits,
            )
        )
    recovered_supports = {
        support for record in (public_records if progress.evidence_records else []) for support in record.supports
    }
    if progress.failed_supports - recovered_supports:
        reasons.append('EVIDENCE_RETRIEVAL_INCOMPLETE')
    stage_timings = dict(progress.stage_timings)
    stage_timings['total'] = _elapsed_ms(progress.started)
    performance = _performance_audit(
        stage_timings=stage_timings,
        query_audits=query_audits,
        public_evidence_count=_public_reference_count(references),
        source_snapshot_count=len(parsed.source_history),
        source_error_count=len(parsed.source_errors),
    )
    partial_reasons = list(dict.fromkeys(reasons))
    recovered_assessment = progress.health_assessment or build_health_assessment(
        clinical_assessment_request(request, parsed) if clinical_document is not None else request, parsed, {}
    )
    if clinical_document is not None and not progress.evaluation_mode:
        # Preserve completed batches and their remapped references. Missing
        # siblings get explicit incomplete text, never a fabricated result.
        ensure_document_fallback(clinical_document)
        executive_summary = next(b.text for b in clinical_document.sections if b.code == 'overview')
        partial_reasons.append('CLINICAL_REPORT_NARRATIVE_INCOMPLETE')
    evaluation_data = build_evaluation_data(
        progress.request or request, parsed, clinical_document, recovered_assessment, references
    )
    if clinical_document is not None:
        evaluation_data = apply_evaluation_blocks(evaluation_data, clinical_document)
        from .evaluation_risk_projection import project_integrated_risks
        project_integrated_risks(evaluation_data, clinical_document, references, final=True)
        if progress.evaluation_mode:
            executive_summary = evaluation_data.health_overview.overall_summary.text or executive_summary
            partial_reasons.append('EVALUATION_NARRATIVE_INCOMPLETE')
    if progress.evaluation_mode:
        finalize_priority_focus(evaluation_data, facts={fact.fact_id: fact for fact in clinical_document.facts}
            if clinical_document is not None else None)
    evaluation_data = finalize_evaluation_references(evaluation_data)
    if clinical_document is not None:
        partial_reasons = list(dict.fromkeys([*partial_reasons, *clinical_document.incomplete]))
    _log_report_performance(
        status='partial_success',
        partial_reasons=partial_reasons,
        performance=performance,
        query_audits=query_audits,
    )
    return SuccessCallback(
        user_id=request.user_id,
        session_id=request.session_id,
        status='partial_success',
        generated_at=datetime.now(UTC),
        report=ReportContent(
            subject=parsed.subject,
            sample=parsed.sample,
            health_assessment=recovered_assessment,
            executive_summary=executive_summary,
            abnormal_microbes=microbes,
            disease_risks=diseases,
            health_management=health,
            evidence_references=references,
            clinical_document=clinical_document,
            evaluation_data=evaluation_data,
        ),
        partial_reasons=partial_reasons,
        audit=ReportAudit(
            source_count_reconciliation=parsed.panel_counts,
            evidence_queries=query_audits,
            relation_coverage=relation_coverage,
            stage_outcomes=_stage_outcomes(
                query_audits,
                microbes=microbes,
                diseases=diseases,
            ),
            withheld_disease_items=progress.withheld_diseases,
            coverage_issues=_coverage_issues(parsed, microbes, diseases),
            performance=performance,
        ),
    )


def _recover_partial_payload(
    request: ReportRequest,
    progress: _GenerationProgress,
    *,
    fallback_reason: str,
    deadline_reached: bool | None = None,
) -> SuccessCallback:
    """Preserve completed facts, otherwise return the fixed empty report."""

    if all(
        value is not None
        for value in (
            progress.parsed,
            progress.microbes,
            progress.diseases,
            progress.health,
        )
    ):
        try:
            partial = _deadline_partial_payload(request, progress,
                fallback_reason=fallback_reason,
                deadline_reached=(fallback_reason == 'REPORT_GENERATION_TIMEOUT'
                    if deadline_reached is None else deadline_reached))
            return partial
        except Exception as error:
            logger.error('could not finalize the completed report checkpoint: error_type=%s', type(error).__name__)
    return _empty_partial_payload(request, reasons=[fallback_reason], evaluation_mode=progress.evaluation_mode)


def _has_evaluate_source_facts(data: dict[str, Any]) -> bool:
    """Keep evaluate's empty-result check independent of follow-up orchestration.

    Preserve the existing check, including normal dimension evidence. Removing
    a follow-up generation gate must not break evaluate artifact persistence.
    """
    if data.get('schemaVersion') == 'evaluate.v2.0':
        return any(
            isinstance(item, dict)
            and item.get('availability') in {'available', 'partial'}
            and (item.get('value') is not None or item.get('sourceStatus') is not None)
            for item in ((data.get('data') or {}).get('appendices') or {}).get('sourceItems') or []
        )
    basis = data.get('diagnostic_basis') or {}
    if basis.get('abnormal_findings'):
        return True
    context = (data.get('diagnosis_summary') or {}).get('clinical_context') or {}
    if context.get('report.version') == 'clinical-v1' and any(
        key.startswith(('report.fact.clinical.', 'report.fact.function.', 'report.fact.self_reported.'))
        and isinstance(value, str)
        and value.strip()
        for key, value in context.items()
    ):
        return True
    assessment = data.get('healthAssessment') or data.get('health_assessment') or {}
    if assessment.get('abnormalIndicators') or assessment.get('abnormal_indicators'):
        return True
    return any(
        isinstance(dimension, dict) and (dimension.get('evidenceUsed') or dimension.get('evidence_used'))
        for dimension in assessment.get('dimensions') or []
    )


def _empty_partial_payload(
    request: ReportRequest,
    *,
    reasons: list[str],
    evaluation_mode: bool = False,
) -> SuccessCallback:
    """Build the minimum terminal report for an accepted API request.

    Optional source, evidence and model failures must not turn a valid request
    into ``failed``.  Empty modules remain present and no unsupported medical
    conclusion is invented.
    """

    return SuccessCallback(
        user_id=request.user_id,
        session_id=request.session_id,
        status='partial_success',
        generated_at=datetime.now(UTC),
        report=ReportContent(
            subject=SubjectInfo(
                name=request.name,
                age=request.age,
                sex=request.sex,
            ),
            sample=SampleInfo(),
            health_assessment=HealthAssessment(),
            executive_summary=('当前尚无足以生成健康评估的有效资料。' if evaluation_mode else
                '肠道菌群检测结果暂未取得，当前保留阶段性健康评估结构。'
                '待检测结果出具后，可重新生成包含菌群解读与循证依据的完整报告。'
            ),
            abnormal_microbes=AbnormalMicrobesSection(
                summary=_microbiome_pending_summary(evaluation_mode),
            ),
            disease_risks=DiseaseRisksSection(),
            health_management=HealthManagementSection(),
        ),
        partial_reasons=list(dict.fromkeys(reasons)),
    )


def _clear_evidence_bound_content(microbes: Any, diseases: Any) -> None:
    """Reset derived evidence fields before a deterministic checkpoint rebuild."""

    for item in microbes.items:
        item.disease_and_risks = []
        item.medical_interpretation = None
        item.interpretation_evidence_refs = []
        item.association_evidence_refs = []
        item.evidence_refs = []
        item.clinical_role = None
        item.functional_interpretation = None
        item.functional_evidence_refs = []
    for item in diseases.items:
        item.medical_interpretation = None
        # Source-report organism links are patient-specific parsed facts used
        # to formulate and audit evidence searches. Clearing them at every
        # checkpoint made a complete provider disease panel collapse to the
        # few relationships rediscovered during one bounded search run.
        if item.source_type != 'source_report':
            item.related_microbes = []
        item.association_evidence_refs = []
        item.confirmation_evidence_refs = []
        item.suggested_evaluations = [
            value for value in item.suggested_evaluations if value.recommendation_source == 'model'
        ]
        item.evidence_refs = []


def _session_document_sources(session, health_data):
    """Prefer owned archived references; old sessions may still only have URLs."""
    legacy = normalize_legacy_report_files(health_data.get('fileList')) or []
    documents = session.get('documents')
    if not isinstance(documents, list):
        return legacy
    indexed = {}
    for row in documents:
        if not isinstance(row, dict):
            continue
        index, document_id = row.get('fileIndex'), row.get('documentId')
        if type(index) is int and index >= 0 and isinstance(document_id, str) and document_id.strip():
            indexed[index] = {'documentId': document_id}
    if not indexed:
        return legacy
    # Preserve source sequence and retain URL-only files in mixed historical sessions.
    return [
        indexed.get(index, legacy[index] if index < len(legacy) else None)
        for index in sorted(set(range(len(legacy))) | set(indexed))
    ]


def merge_request_session_fallbacks(
    request: ReportRequest,
    session: dict[str, Any],
    *,
    include_subject: bool = True,
) -> ReportRequest:
    """Fill optional request data from the referenced conversation.

    Values sent explicitly to ``/api/v1/report`` always win.  A conversation
    can only fill missing demographics or empty source arrays, and every
    candidate is revalidated by ``ReportRequest`` before it reaches parsing.
    Invalid/stale session data is ignored field by field instead of failing the
    report task. Evaluate leaves missing personal fields for the supplied PDF
    to fill instead of taking them from conversation history.
    """

    health_data = session.get('healthData')
    if not isinstance(health_data, dict):
        health_data = {}
    extracted = session.get('extractedInfo')
    if not isinstance(extracted, dict):
        extracted = {}

    def first_value(*values: Any) -> Any:
        return next(
            (value for value in values if value not in (None, '', [], {})),
            None,
        )

    fallbacks: tuple[tuple[str, bool, Any], ...] = (
        (
            'name',
            include_subject and request.name is None,
            first_value(extracted.get('name'), health_data.get('name')),
        ),
        (
            'age',
            include_subject and request.age is None,
            first_value(extracted.get('age'), health_data.get('age')),
        ),
        (
            'sex',
            include_subject and request.sex is None,
            first_value(
                extracted.get('gender'),
                extracted.get('sex'),
                health_data.get('gender'),
                health_data.get('sex'),
            ),
        ),
        ('fileList', not request.file_list, _session_document_sources(session, health_data)),
        ('testData', not request.test_data, health_data.get('testData')),
        (
            'hospitalData',
            not request.hospital_data,
            health_data.get('hospitalData'),
        ),
        (
            'microbiomeData',
            request.microbiome_data is None,
            health_data.get('microbiomeData'),
        ),
    )
    merged = request
    for alias, missing, value in fallbacks:
        if not missing or value in (None, '', [], {}):
            continue
        payload = merged.model_dump(mode='json', by_alias=True)
        payload[alias] = value
        try:
            merged = ReportRequest.model_validate(payload)
        except Exception:
            logger.warning(
                'ignored invalid report session fallback: field=%s user_id=%s session_id=%s',
                alias,
                request.user_id,
                request.session_id,
            )
    return merged


def _group_message_date(value):
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000, UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            pass
    return None


def _merge_group_attachments(request, context, errors):
    from modules.report.evaluation_request_input import fmt_typing_report_source

    files = list(request.file_list)
    seen = {item.url for item in files if item.url}
    fmt_url, _ = fmt_typing_report_source(request)
    if fmt_url:
        seen.add(fmt_url)
    spec = request.microbiome_report_data
    if spec is not None:
        # Dedicated C and matching sources retain their independent purpose.
        seen.update(url for url in (spec.original_report_url, spec.data_info_url,
            spec.fmt_typing_report_url) if url)
    for attachment in context.attachments:
        if attachment.url in seen:
            continue
        try:
            item = RemoteFileInput(url=attachment.url, fileType=attachment.file_type,
                sourceName='IM群聊附件', sourceMessageId=attachment.message_id,
                sourceGroupId=request.group_id, sourceSenderId=attachment.sender_id,
                sourceSendTime=attachment.send_time)
        except ValueError:
            errors.append('GROUP_ATTACHMENT_UNAVAILABLE')
            continue
        if len(files) >= 10:
            errors.append('GROUP_ATTACHMENTS_TRUNCATED')
            continue
        files.append(item)
        seen.add(item.url)
    return request.model_copy(update={'file_list': files})


def extract_session_context(session: dict[str, Any]) -> dict[str, Any]:
    """Keep patient-authored clinical/lifestyle context, not storage metadata."""
    allowed_keys = {
        'healthData',
        'qaHistory',
        'extractedInfo',
        'currentQuestion',
        'answers',
        'answer',
        'questions',
        'question',
        'messages',
        'history',
        'conversation',
        'content',
        'text',
        'role',
        'symptoms',
        'symptom',
        'lifestyle',
        'diet',
        'exercise',
        'sleep',
        'bowel',
        'value',
        'name',
    }
    prohibited_fragments = ('product', 'package', 'recommendation', 'prompt', 'token')
    source_payload_keys = {
        'filelist',
        'hospitaldata',
        'testdata',
        'microbiomedata',
    }

    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 6:
            return None
        if isinstance(value, dict):
            role = str(value.get('role') or '').strip().casefold()
            if role in {'assistant', 'system'}:
                return None
            output = {}
            for key, item in value.items():
                key_text = str(key)
                if any(fragment in key_text.casefold() for fragment in prohibited_fragments):
                    continue
                if key_text.casefold() in source_payload_keys:
                    # Source files and hospital rows have already been merged
                    # into the typed request above.  Do not duplicate URLs or
                    # entire reports in the narrative/session prompt.
                    continue
                if depth == 0 and key_text not in allowed_keys:
                    continue
                cleaned = clean(item, depth + 1)
                if cleaned not in (None, '', [], {}):
                    output[key_text] = cleaned
            return output
        if isinstance(value, list):
            return [item for raw in value[:100] if (item := clean(raw, depth + 1)) is not None]
        if isinstance(value, (str, int, float, bool)):
            return str(value)[:4000] if isinstance(value, str) else value
        return None

    sanitized = clean(session)
    # Bound payload size before sending context to the model.
    encoded = json.dumps(sanitized, ensure_ascii=False)
    if len(encoded) > 30000:
        return {'content': encoded[:30000]}
    return sanitized if isinstance(sanitized, dict) else {}


def _narrative_is_complete(
    *,
    executive_summary: str | None,
    microbes,
    diseases,
    health: HealthManagementSection,
) -> bool:
    if not executive_summary or not microbes.summary:
        return False
    if not health.health_level or not health.health_profile:
        return False
    if any(
        not item.topic or item.priority not in {'重点关注', '建议关注'} or not item.content
        for item in health.recommendations
    ):
        return False
    if any(not item.conclusion for item in microbes.items):
        return False
    if any(item.disease_and_risks and not item.medical_interpretation for item in microbes.items):
        return False
    if any(item.model_interpretation_required and not item.model_interpretation_generated for item in microbes.items):
        return False
    return not any(item.association_evidence_refs and not item.medical_interpretation for item in diseases.items)


def _callback_reserve_seconds(total_seconds: float) -> float:
    """Reserve callback time without consuming tiny test deadlines."""

    return min(60.0, max(0.0, total_seconds) * 0.1)


def _generation_stage_deadlines(
    *,
    started: float,
    total_seconds: float,
) -> tuple[float, float, float, float]:
    """Return scaled cumulative deadlines for the four external-work phases.

    At the production 1800-second limit these land at 540, 840, 1080 and
    1380 seconds. The remaining 360 seconds belong to health/narrative and
    final assembly, followed by the separately reserved 60-second callback.
    """

    # Direct constructor calls below the environment's supported 300-second
    # minimum are unit-test probes. Let the outer generation timeout own those
    # probes so they still exercise deadline recovery rather than instantly
    # exhausting every internal phase.
    if total_seconds < 300.0:
        outside_probe = started + max(1.0, total_seconds * 2.0)
        return (outside_probe,) * 4

    work_seconds = max(
        0.01,
        total_seconds - _callback_reserve_seconds(total_seconds),
    )
    reference_work_seconds = 1740.0
    offsets = (
        work_seconds * 540.0 / reference_work_seconds,
        work_seconds * 840.0 / reference_work_seconds,
        work_seconds * 1080.0 / reference_work_seconds,
        work_seconds * 1380.0 / reference_work_seconds,
    )
    return tuple(started + offset for offset in offsets)


def _evidence_summaries(
    records: list[Any],
    numbers: dict[str, int],
    *,
    include_source: bool = False,
) -> list[dict[str, Any]]:
    """Build one canonical model/fallback view of numbered public evidence."""

    return [
        {
            'number': numbers[record.evidence_key],
            'category': record.category,
            'title': record.title,
            'summary': record.summary,
            **(
                {
                    'sourceText': record.abstract or record.summary or record.evidence_text or '',
                    'evidenceKey': record.evidence_key,
                    'year': record.year,
                    'doi': record.doi,
                    'url': record.url,
                }
                if include_source
                else {}
            ),
            'supports': record.supports,
            'microbeIds': record.microbe_ids,
            'diseaseIds': record.disease_ids,
            'relationBindings': [
                binding.model_dump(mode='json', by_alias=True) for binding in record.relation_bindings
            ],
            'intents': record.intents,
            'targets': record.targets,
            'interpretation': (
                record.interpretation.model_dump(mode='json', by_alias=True) if record.interpretation else None
            ),
        }
        for record in records
        if record.evidence_key in numbers
    ]


def _bounded_public_fallback_queries(
    queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    """Keep PubMed fallback focused on still-empty first/second-module rows."""

    allowed_intents = {
        'microbe_discovery',
        'microbe_clinical_significance',
        'microbe_association',
        'disease_clinical_significance',
        'diagnostic_guideline',
        'health_management',
        'intervention',
    }
    intent_priority = {
        # Exact curated taxon relations have already run before this helper.
        # Give each preserved source-disease row its missing explanation and
        # examination evidence as one stable pair before broad taxon retries.
        # Keeping these two intents at the same priority preserves the source
        # row order emitted by ``make_coverage_queries`` (clinical, diagnostic,
        # next disease).  The former two-tier ordering ran every explanation
        # first and systematically starved diagnostic evidence near a deadline.
        'disease_clinical_significance': 0,
        'diagnostic_guideline': 0,
        'health_management': 1,
        'intervention': 1,
        'microbe_association': 2,
        'microbe_clinical_significance': 2,
        'microbe_discovery': 2,
    }
    # Select the most clinically specific query for each unresolved row.  The
    # coverage builder intentionally emits several forms (bilingual exact,
    # clinical-features, and Chinese clinical wording).  Keeping the first
    # form made PubMed spend the only fallback attempt on broad risk-factor or
    # treatment papers, while the later definition/symptom query that can
    # populate the patient-facing explanation never ran.
    best_by_key: dict[tuple[str, str], tuple[tuple[int, int], int, EvidenceQuery]] = {}
    for index, query in enumerate(queries):
        if query.intent not in allowed_intents:
            continue
        target_id = (
            query.target
            if query.intent in {'health_management', 'intervention'}
            else query.microbe_id or query.disease_id or next(iter(query.supports), query.target)
        )
        key = (query.intent, target_id)
        score = _coverage_query_selection_score(query)
        if query.intent == 'disease_clinical_significance' and re.fullmatch(
            r'"[^"\r\n]+"\[title\]',
            query.query.strip(),
            re.IGNORECASE,
        ):
            # PubMed title matching is the highest-precision first fallback
            # for a disease explanation. EviMed keeps its natural-language
            # overview query; only the public selector receives this boost.
            score = (score[0] + 30, score[1])
        existing = best_by_key.get(key)
        if existing is None or score > existing[0]:
            best_by_key[key] = (score, index, query)
    selected = list(best_by_key.values())
    selected.sort(
        key=lambda item: (
            intent_priority.get(item[2].intent, 99),
            item[1],
        )
    )
    return [query for _score, _index, query in selected]


def _make_public_coverage_queries(
    microbes: Any,
    diseases: Any,
    records: list[Any],
    *,
    subject: Any,
    age: int | None,
    hospital_data: list[Any] | None,
) -> list[EvidenceQuery]:
    """Build fallback work only from evidence that can reach the callback.

    Coverage used to be calculated from every interpreted record.  A case
    report, demographic mismatch, or otherwise non-public hit could therefore
    suppress the PubMed retry and later be removed by the final public gate,
    leaving a disease row without evidence.  Use the exact final eligibility
    set here so scheduling and callback behavior cannot disagree.
    """

    public_records = _eligible_public_records(
        records,
        microbes=microbes,
        subject=subject,
    )
    return make_coverage_queries(
        microbes,
        diseases,
        public_records,
        age=age,
        hospital_data=hospital_data,
    )


def _promote_evidence_diseases_for_followup(
    *,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    records: list[Any],
    subject: Any,
) -> set[str]:
    """Promote only publishable taxon relations before diagnostic retrieval.

    Citation numbers are provisional at this point and are rebuilt during the
    final binding stage.  Their only purpose here is to run the same clinical
    promotion gate used by final JSON assembly, so query scheduling cannot see
    fewer disease rows than the callback will later publish.
    """

    recover_exact_relation_interpretations(records)
    validate_relation_bindings(records)
    public_records = _eligible_public_records(
        records,
        microbes=microbes,
        subject=subject,
    )
    if not public_records:
        return set()
    citation_records = consolidate_public_records([record.model_copy(deep=True) for record in public_records])
    _references, citation_numbers = number_evidence(citation_records)
    semantic_numbers = citation_numbers_for_semantic_records(
        public_records,
        citation_records,
        citation_numbers,
    )
    attach_evidence_refs(
        microbes,
        diseases,
        public_records,
        semantic_numbers,
    )
    promote_evidence_disease_associations(diseases, microbes)
    return {
        disease.disease_id
        for disease in diseases.items
        if disease.related_microbes and disease.association_evidence_refs
    }


def _coverage_query_selection_score(query: EvidenceQuery) -> tuple[int, int]:
    """Rank one bounded retry by its ability to explain the requested row."""

    if query.intent != 'disease_clinical_significance':
        return (0, 0)
    text = ' '.join(query.query.casefold().split())
    clinical_markers = (
        'clinical significance',
        'clinical feature',
        'clinical characteristics',
        'symptom',
        'manifestation',
        'definition',
        'overview',
        'pathophysiology',
        '临床意义',
        '临床表现',
        '相关风险',
        '定义',
        '症状',
    )
    score = 10 if any(marker in text for marker in clinical_markers) else 0
    if 'overview' in text:
        score += 4
    # A mixed-language exact-name query remains useful, but it should not beat
    # an explicit clinical-features query when the report has only one public
    # fallback request for this disease.
    has_chinese = bool(re.search(r'[\u3400-\u9fff]', text))
    has_latin = bool(re.search(r'[a-z]', text))
    if has_latin and not has_chinese:
        score += 3
    elif has_chinese and not has_latin:
        score += 1
    elif has_chinese and has_latin and score == 0:
        score -= 2
    return (score, -len(text))


def _bounded_secondary_public_disease_queries(
    queries: list[EvidenceQuery],
    *,
    attempted_queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    """Select one untried clinical-description query per uncovered disease."""

    attempted = {
        (query.disease_id or next(iter(query.supports), ''), query.query)
        for query in attempted_queries
        if query.intent == 'disease_clinical_significance'
    }
    best_by_disease: dict[str, tuple[tuple[int, int], int, EvidenceQuery]] = {}
    for index, query in enumerate(queries):
        if query.intent != 'disease_clinical_significance':
            continue
        disease_id = query.disease_id or next(iter(query.supports), '')
        if not disease_id or (disease_id, query.query) in attempted:
            continue
        score = _coverage_query_selection_score(query)
        lowered_query = query.query.casefold()
        if '[title/abstract]' in lowered_query and any(
            marker in lowered_query
            for marker in (
                '"gut microbiome"',
                '"gut microbiota"',
                '"intestinal microbiome"',
                '"intestinal microbiota"',
            )
        ):
            # The second public pass explains why a source microbiome report
            # listed the disease. It is more relevant to the report's
            # "医学解释与关联风险" column than another encyclopedia-style
            # definition query, while the first overview pass remains intact.
            score = (score[0] + 30, score[1])
        existing = best_by_disease.get(disease_id)
        if existing is None or score > existing[0]:
            best_by_disease[disease_id] = (score, index, query)
    selected = sorted(best_by_disease.values(), key=lambda item: item[1])
    return [query for _score, _index, query in selected]


def _bounded_public_function_queries(
    queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    """Run at most one public fallback per unresolved taxon/function line."""

    retained: list[EvidenceQuery] = []
    seen: set[tuple[str, str, str]] = set()
    for query in queries:
        if query.intent not in {'gut_function', 'gut_barrier'}:
            continue
        key = (query.intent, query.microbe_id or '', query.target)
        if key in seen:
            continue
        seen.add(key)
        retained.append(query)
    return retained


def _bounded_evimed_coverage_queries(
    queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    """Give every uncovered row a retry without executing every synonym form."""

    intent_priority = {
        # Primary discovery already queried every taxon once.  At coverage
        # time, preserve the two user-facing disease columns together before
        # spending the remaining stage budget on broad taxon synonyms.
        'disease_clinical_significance': 0,
        'diagnostic_guideline': 0,
        'health_management': 0,
        'intervention': 0,
        'microbe_association': 1,
        'microbe_clinical_significance': 1,
        'microbe_discovery': 1,
    }
    best_by_key: dict[tuple[str, str], tuple[tuple[int, int], int, EvidenceQuery]] = {}
    for index, query in enumerate(queries):
        target_id = (
            query.target
            if query.intent in {'health_management', 'intervention'}
            else query.microbe_id or query.disease_id or next(iter(query.supports), query.target)
        )
        key = (query.intent, target_id)
        score = _coverage_query_selection_score(query)
        existing = best_by_key.get(key)
        if existing is None or score > existing[0]:
            best_by_key[key] = (score, index, query)
    selected = list(best_by_key.values())
    selected.sort(
        key=lambda item: (
            intent_priority.get(item[2].intent, 2),
            item[1],
        )
    )
    return [query for _score, _index, query in selected]


def _bounded_untried_diagnostic_queries(
    queries: list[EvidenceQuery],
    *,
    query_audits: list[EvidenceQueryAudit],
) -> list[EvidenceQuery]:
    """Select one alternate diagnostic query per still-uncovered disease."""

    attempted_query_ids = {audit.query_id for audit in query_audits}
    return _bounded_evimed_coverage_queries(
        [
            query
            for query in queries
            if query.intent == 'diagnostic_guideline' and query.query_id not in attempted_query_ids
        ]
    )


def _bounded_evimed_candidate_queries(
    queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    counts: dict[str, int] = {}
    retained: list[EvidenceQuery] = []
    for query in queries:
        target_id = query.microbe_id or next(iter(query.supports), query.target)
        # Two independent candidates are sufficient for comparison before the
        # exact-relation fallback. The previous limit of three, followed by up
        # to two more fallback queries, was the largest source of query growth.
        limit = 2
        if counts.get(target_id, 0) >= limit:
            continue
        counts[target_id] = counts.get(target_id, 0) + 1
        retained.append(query)
    return retained


def _bounded_relation_fallback_queries(
    queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    """Retry at most one unresolved exact relation per report taxon."""

    counts: dict[str, int] = {}
    retained: list[EvidenceQuery] = []
    for query in queries:
        target_id = query.microbe_id or next(iter(query.supports), query.target)
        if counts.get(target_id, 0) >= 1:
            continue
        counts[target_id] = counts.get(target_id, 0) + 1
        retained.append(query)
    return retained


def _bounded_contextual_relation_queries(
    queries: list[EvidenceQuery],
) -> list[EvidenceQuery]:
    """Allow one patient-context refinement per unresolved report taxon."""

    retained: list[EvidenceQuery] = []
    seen: set[str] = set()
    for query in queries:
        target_id = query.microbe_id or next(iter(query.supports), query.target)
        if target_id in seen:
            continue
        seen.add(target_id)
        retained.append(query)
    return retained


def _bounded_priority_public_relation_queries(
    queries: list[EvidenceQuery],
    *,
    max_queries: int,
) -> list[EvidenceQuery]:
    """Prioritize one exact relation per uncovered taxon under the deadline.

    PubMed/PMC calls are rate limited by the official API. Running every
    candidate before broader disease/diagnostic coverage pushed a real report
    into the reserved deadline window. Keep one source-report-aligned exact
    relation per taxon and a small report-wide cap. Beneficial/other taxa must
    remain eligible here: their direction-specific epidemiologic relation is
    exactly the gap that a broad ``low abundance`` fallback often cannot find.
    The returned paper still passes the ordinary exact taxon, condition,
    human-context and abundance-direction validators before publication.
    """

    relation_rank = {
        'established_pathogen': 0,
        'opportunistic': 1,
        'epidemiologic_association': 2,
        'colonization': 3,
        'environmental_or_dietary': 4,
    }
    ordered = sorted(
        enumerate(queries),
        key=lambda pair: (
            relation_rank.get(pair[1].relation_type or '', 5),
            pair[0],
        ),
    )
    retained: list[EvidenceQuery] = []
    seen_microbes: set[str] = set()
    for _source_index, query in ordered:
        if query.intent != 'microbe_disease_association' or not query.microbe_id or query.microbe_id in seen_microbes:
            continue
        retained.append(query)
        seen_microbes.add(query.microbe_id)
        if len(retained) >= max(0, max_queries):
            break
    return retained


async def _retrieve_evidence_until(
    client: Any,
    queries: list[EvidenceQuery],
    *,
    deadline: float,
    provider: str,
    concurrency: int = 8,
) -> EvidenceRetrievalResult:
    """Bound retrieval with a work-conserving, stable-order worker queue.

    Fixed concurrency-sized batches create head-of-line blocking: one slow
    request holds the next batch even when the other workers have already
    finished.  Keep at most ``concurrency`` single-query tasks active and start
    the next query whenever any task completes.  Results are merged by the
    original query index, so scheduling cannot change evidence numbering,
    public ordering or audit ordering.  Queries still active at the deadline
    receive the same explicit timeout audit rows as queries not yet started.
    """

    # One public semantic query already opens its PubMed guide and literature
    # channels concurrently. Cap the outer workers at two so slow search/fetch
    # responses can overlap without returning to the former six-stream retry
    # storm. The client-wide pace and shared 429 cooldown remain authoritative.
    if provider in {'public_web', 'public_literature'}:
        concurrency = min(2, max(1, concurrency))

    if not queries:
        return await retrieve_evidence(client, [], concurrency=concurrency)

    worker_count = max(1, concurrency)
    results_by_index: dict[int, EvidenceRetrievalResult] = {}
    active: dict[asyncio.Task[EvidenceRetrievalResult], int] = {}
    progress = _ACTIVE_PROGRESS.get()
    retrieval_cache = progress.retrieval_cache if progress is not None else None
    next_index = 0

    def start_available() -> None:
        nonlocal next_index
        while next_index < len(queries) and len(active) < worker_count:
            index = next_index
            cache_key = (provider, queries[index])
            cached = retrieval_cache.get(cache_key) if retrieval_cache is not None else None
            if cached is not None:
                results_by_index[index] = copy.deepcopy(cached)
                next_index += 1
                continue
            task = asyncio.create_task(retrieve_evidence(client, [queries[index]], concurrency=1))
            active[task] = index
            next_index += 1

    start_available()
    try:
        while active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            done, _pending = await asyncio.wait(
                active,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break
            for task in sorted(done, key=lambda item: active[item]):
                index = active.pop(task)
                result = task.result()
                results_by_index[index] = result
                if retrieval_cache is not None:
                    retrieval_cache[(provider, queries[index])] = copy.deepcopy(result)
            start_available()
    finally:
        # A task can finish between the deadline check and cancellation. Keep
        # that completed result instead of incorrectly marking it timed out.
        for task, index in list(active.items()):
            if task.done() and not task.cancelled():
                results_by_index[index] = task.result()
                active.pop(task, None)
        pending_tasks = list(active)
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

    if len(results_by_index) < len(queries):
        logger.warning(
            'evidence stage budget exhausted: provider=%s completed=%s remaining=%s',
            provider,
            len(results_by_index),
            len(queries) - len(results_by_index),
        )

    completed: EvidenceRetrievalResult | None = None
    for index, query in enumerate(queries):
        result = results_by_index.get(index)
        if result is None:
            result = _timed_out_retrieval_result([query], provider=provider)
        completed = merge_retrieval_results(completed, result) if completed is not None else result
    return completed or _timed_out_retrieval_result(queries, provider=provider)


async def _interpret_evidence_until(
    client: NarrativeClient,
    *,
    records: list[Any],
    numbers: dict[str, int],
    patient_context: dict[str, Any] | None,
    deadline: float,
    _retry_enrichment: bool = True,
    _retry_public_disease_claims: bool = False,
) -> set[int]:
    """Bound model interpretation while keeping records completed before cancel."""

    progress = _ACTIVE_PROGRESS.get()
    cache = progress.interpretation_cache if progress is not None else None
    cached_numbers: set[int] = set()
    pending_records: list[Any] = []
    process_keys: dict[str, str] = {}
    for record in records:
        cached = cache.get(record.evidence_key) if cache is not None else None
        process_key = _process_interpretation_cache_key(record, patient_context)
        process_keys[record.evidence_key] = process_key
        if cached is None:
            cached = _process_interpretation_get(process_key)
            if cached is not None:
                increment_report_metric('model.interpretation_cache_hit')
        if record.interpretation is None and cached is not None:
            record.interpretation = cached.model_copy(deep=True)
        if record.interpretation is not None:
            if record.evidence_key in numbers:
                cached_numbers.add(numbers[record.evidence_key])
        else:
            pending_records.append(record)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return cached_numbers
    completed_numbers: set[int]
    try:
        async with asyncio.timeout(remaining):
            completed_numbers = set(cached_numbers)
            if pending_records:
                completed_numbers.update(
                    await interpret_evidence(
                        client,
                        records=pending_records,
                        numbers=numbers,
                        patient_context=patient_context,
                        # Run the same enrichment functions below against the
                        # full hydrated set. This lets cached base
                        # interpretations reuse the item-level repair pass
                        # without repeating the main model interpretation.
                        _retry_enrichment=False,
                    )
                )
            if _retry_enrichment:
                await _retry_evidence_enrichment(
                    client,
                    records=records,
                    numbers=numbers,
                    patient_context=patient_context,
                )
            if _retry_public_disease_claims:
                await repair_disease_evidence_claims(
                    client,
                    records=records,
                    numbers=numbers,
                )
    except TimeoutError:
        logger.warning(
            'evidence interpretation budget exhausted: records=%s',
            len(records),
        )
        completed_numbers = {
            numbers[record.evidence_key]
            for record in records
            if record.interpretation is not None and record.evidence_key in numbers
        }
    if cache is not None:
        for record in records:
            if record.interpretation is not None:
                cache[record.evidence_key] = record.interpretation.model_copy(deep=True)
    for record in records:
        if record.interpretation is not None:
            _process_interpretation_set(
                process_keys[record.evidence_key],
                record.interpretation,
            )
    subject = (patient_context or {}).get('subject', {})
    _apply_subject_demographic_gates(records, subject=subject)
    return completed_numbers


def _timed_out_retrieval_result(
    queries: list[EvidenceQuery],
    *,
    provider: str,
) -> EvidenceRetrievalResult:
    audits: list[EvidenceQueryAudit] = []
    relation_coverage: list[RelationCoverageAudit] = []
    for query in queries:
        channels = [
            EvidenceChannelAudit(
                channel=channel,
                status=('failed' if channel in query.channels else 'not_requested'),
                provider=(provider if channel in query.channels else None),
            )
            for channel in ('guide', 'literature')
        ]
        audits.append(
            EvidenceQueryAudit(
                query_id=query.query_id,
                query_text=query.query,
                query_language=query.query_language,
                intent=query.intent,
                target=query.target or query.query,
                microbe_id=query.microbe_id,
                disease_id=query.disease_id,
                relation_id=query.relation_id,
                channels=channels,
                rejected_reasons={'stage_timeout': 1},
            )
        )
        if query.relation_disease_id and query.relation_source_taxon:
            relation_coverage.append(
                RelationCoverageAudit(
                    disease_id=query.relation_disease_id,
                    disease_name=query.relation_disease_name or '',
                    source_taxon=query.relation_source_taxon,
                    matched_microbe_ids=list(query.matched_microbe_ids),
                    query_id=query.query_id,
                )
            )
    return EvidenceRetrievalResult(
        records=[],
        failed_supports={support for query in queries for support in query.supports},
        query_audits=audits,
        relation_coverage=relation_coverage,
    )


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000, 3)


def _public_reference_count(references: list[Any]) -> int:
    return sum(
        1
        for reference in references
        if reference.provider
        in {
            'pubmed',
            'pmc',
            'official_guideline',
            'crossref',
            'crossref_verified',
        }
    )


def _performance_audit(
    *,
    stage_timings: dict[str, float],
    query_audits: list[Any],
    public_evidence_count: int,
    source_snapshot_count: int,
    source_error_count: int,
) -> ReportPerformanceAudit:
    runtime_counters, runtime_elapsed_ms = report_metrics_snapshot()
    provider_query_counts: dict[str, int] = {}
    provider_elapsed_ms: dict[str, float] = {}
    for audit in query_audits:
        for channel in audit.channels:
            if channel.status == 'not_requested':
                continue
            provider = channel.provider or 'unknown'
            key = f'{provider}:{channel.channel}'
            provider_query_counts[key] = provider_query_counts.get(key, 0) + 1
            provider_elapsed_ms[key] = round(
                provider_elapsed_ms.get(key, 0.0) + channel.elapsed_ms,
                3,
            )
    return ReportPerformanceAudit(
        stage_timings_ms=stage_timings,
        provider_query_counts=provider_query_counts,
        provider_elapsed_ms=provider_elapsed_ms,
        runtime_counters=runtime_counters,
        runtime_elapsed_ms=runtime_elapsed_ms,
        raw_hit_count=sum(audit.raw_count for audit in query_audits),
        screened_hit_count=sum(audit.screened_count for audit in query_audits),
        interpreted_hit_count=sum(audit.interpreted_count for audit in query_audits),
        public_evidence_count=public_evidence_count,
        source_snapshot_count=source_snapshot_count,
        source_error_count=source_error_count,
    )


def _log_report_performance(
    *,
    status: str,
    partial_reasons: list[str],
    performance: ReportPerformanceAudit,
    query_audits: list[Any],
) -> None:
    """Emit internal timing details without changing the callback contract."""

    slowest_queries = sorted(
        (
            {
                'queryId': audit.query_id,
                'intent': audit.intent,
                'target': audit.target,
                'elapsedMs': round(sum(channel.elapsed_ms for channel in audit.channels), 3),
                'channelFailures': sum(channel.status == 'failed' for channel in audit.channels),
                'stageTimeout': bool(audit.rejected_reasons.get('stage_timeout')),
            }
            for audit in query_audits
        ),
        key=lambda item: item['elapsedMs'],
        reverse=True,
    )[:10]
    logger.info(
        'report generation performance: %s',
        json.dumps(
            {
                'status': status,
                'partialReasons': partial_reasons,
                'performance': performance.model_dump(mode='json', by_alias=True, exclude_none=False),
                'slowestQueries': slowest_queries,
            },
            ensure_ascii=False,
            separators=(',', ':'),
        ),
    )


def _coverage_issues(parsed: Any, microbes: Any, diseases: Any) -> list[EvidenceCoverageIssue]:
    issues: list[EvidenceCoverageIssue] = []
    issues.extend(
        EvidenceCoverageIssue(
            target_type='source',
            target_id=item.source_name,
            reason='SOURCE_DATE_UNRESOLVED',
        )
        for item in parsed.source_history
        if item.date_unresolved
    )
    issues.extend(
        EvidenceCoverageIssue(
            target_type='source',
            target_id=item.disease_id,
            reason='SOURCE_DISEASE_INDEX_UNAVAILABLE',
        )
        for item in parsed.disease_results
        if item.source_score is None or item.source_level is None
    )
    issues.extend(
        EvidenceCoverageIssue(
            target_type='microbe',
            target_id=item.microbe_id,
            reason='NO_ELIGIBLE_HUMAN_RELATION_EVIDENCE',
        )
        for item in microbes.items
        if not item.disease_and_risks
    )
    for item in diseases.items:
        if not item.medical_interpretation:
            issues.append(
                EvidenceCoverageIssue(
                    target_type='disease',
                    target_id=item.disease_id,
                    reason='NO_ELIGIBLE_DISEASE_EXPLANATION_EVIDENCE',
                )
            )
        if not item.suggested_evaluations:
            issues.append(
                EvidenceCoverageIssue(
                    target_type='diagnostic',
                    target_id=item.disease_id,
                    reason='NO_ELIGIBLE_DIAGNOSTIC_GUIDE_EVIDENCE',
                )
            )
    return issues


def _stage_outcomes(
    query_audits: list[Any],
    *,
    microbes: Any,
    diseases: Any,
) -> list[EvidenceStageAudit]:
    """Aggregate query attempts into target-level, non-patient audit rows."""

    microbe_names = {item.microbe_id: item.name for item in microbes.items}
    disease_names = {item.disease_id: item.name for item in diseases.items}
    grouped: dict[tuple[str, str, str, str], list[Any]] = {}
    intent_stages = {
        'microbe_discovery': ('microbe', 'candidate_discovery'),
        'microbe_clinical_significance': ('microbe', 'clinical_significance'),
        'microbe_association': ('microbe', 'clinical_significance'),
        'microbe_disease_association': ('microbe', 'candidate_validation'),
        'disease_clinical_significance': ('disease', 'disease_explanation'),
        'diagnostic_guideline': ('diagnostic', 'diagnostic_guidance'),
        'gut_function': ('function', 'function_validation'),
        'gut_barrier': ('barrier', 'barrier_validation'),
        'intervention': ('intervention', 'intervention_validation'),
        'health_management': ('health_management', 'health_management'),
    }

    for audit in query_audits:
        target_type, stage = intent_stages.get(
            audit.intent,
            ('report', str(audit.intent)),
        )
        if target_type == 'microbe':
            target_id = audit.microbe_id or audit.relation_id or audit.query_id
            target = microbe_names.get(audit.microbe_id or '', audit.target)
        elif target_type in {'disease', 'diagnostic'}:
            target_id = audit.disease_id or audit.query_id
            target = disease_names.get(audit.disease_id or '', audit.target)
        else:
            target_id = audit.disease_id or audit.microbe_id or audit.query_id
            target = audit.target
        key = (target_type, target_id, target, stage)
        grouped.setdefault(key, []).append(audit)

    output: list[EvidenceStageAudit] = []
    for (target_type, target_id, target, stage), audits in grouped.items():
        attempts: list[str] = []
        reasons: list[str] = []
        raw_count = sum(item.raw_count for item in audits)
        retained_count = sum(item.retained_count for item in audits)
        interpreted_count = sum(item.interpreted_count for item in audits)
        public_count = sum(item.public_count for item in audits)
        has_failed_channel = False
        for audit in audits:
            for channel in audit.channels:
                if channel.status == 'not_requested':
                    continue
                provider = channel.provider or 'unknown'
                attempt = 'pubmed_pmc' if provider == 'public_web' else f'{provider}_{channel.channel}'
                if attempt not in attempts:
                    attempts.append(attempt)
                if channel.status == 'failed':
                    has_failed_channel = True
                    reasons.append(f'CHANNEL_FAILED:{provider.upper()}:{channel.channel.upper()}')
            reasons.extend(f'{reason.upper()}:{count}' for reason, count in audit.rejected_reasons.items() if count)

        if raw_count == 0:
            reasons.append('NO_RESULT_RETURNED')
        elif retained_count == 0:
            reasons.append('NO_RELEVANT_RESULT_RETAINED')
        elif interpreted_count == 0:
            reasons.append('MODEL_INTERPRETATION_UNAVAILABLE')
        elif public_count == 0:
            reasons.append('NO_ELIGIBLE_PUBLIC_EVIDENCE')

        if public_count > 0 and has_failed_channel:
            result = 'degraded_success'
        elif public_count > 0 and interpreted_count < retained_count:
            result = 'partial_success'
        elif public_count > 0:
            result = 'success'
        else:
            result = 'insufficient_data'

        output.append(
            EvidenceStageAudit(
                target_type=target_type,
                target_id=target_id,
                target=target,
                stage=stage,
                result=result,
                attempts=attempts,
                raw_result_count=raw_count,
                retained_result_count=retained_count,
                interpreted_result_count=interpreted_count,
                public_result_count=public_count,
                reasons=list(dict.fromkeys(reasons)),
            )
        )
    return output


def _content_completeness_reasons(
    *,
    microbes,
    diseases,
    query_audits: list[Any] | None = None,
) -> list[str]:
    """Return stable audit codes for evidence-backed public fields left empty.

    Unresolved provider template rows remain in the internal audit but are not
    public patient results.  Completeness therefore considers only rows with a
    real source signal or an evidence-promoted abnormal-taxon association.
    """

    reasons: list[str] = []
    if any(
        not item.disease_and_risks or not item.medical_interpretation or not item.evidence_refs
        for item in microbes.items
    ):
        reasons.append('MICROBE_EVIDENCE_INCOMPLETE')
    public_diseases = [item for item in diseases.items if disease_has_patient_microbiome_signal(item)]
    if any(not item.medical_interpretation or not item.association_evidence_refs for item in public_diseases):
        reasons.append('DISEASE_EVIDENCE_INCOMPLETE')
    diagnostic_targets = [
        item
        for item in diseases.items
        if item.source_type == 'clinical_context'
        or (
            disease_has_patient_microbiome_signal(item)
            and item.medical_interpretation
            and item.association_evidence_refs
        )
    ]
    malformed_evaluation = any(
        any(
            not evaluation.rationale or (not evaluation.evidence_refs and evaluation.recommendation_source != 'model')
            for evaluation in item.suggested_evaluations
        )
        for item in diagnostic_targets
    )
    unresolved_diagnostic_search = any(
        not item.suggested_evaluations
        and _diagnostic_query_execution_incomplete(
            item.disease_id,
            query_audits or [],
        )
        for item in diagnostic_targets
    )
    if malformed_evaluation or unresolved_diagnostic_search:
        reasons.append('DIAGNOSTIC_EVIDENCE_INCOMPLETE')
    return reasons


def _diagnostic_query_execution_incomplete(
    disease_id: str,
    query_audits: list[Any],
) -> bool:
    """Distinguish a valid empty search from technical diagnostic degradation."""

    audits = [
        audit for audit in query_audits if audit.intent == 'diagnostic_guideline' and audit.disease_id == disease_id
    ]
    if not audits:
        return True
    return any(
        audit.rejected_reasons.get('stage_timeout')
        or any(channel.status == 'failed' for channel in audit.channels)
        or audit.retained_count > audit.interpreted_count
        for audit in audits
    )


def _source_unavailable_reasons(source_errors: list[str]) -> list[str]:
    reasons = ['MICROBIOME_SOURCE_UNAVAILABLE']
    if any(error.startswith('testData[') for error in source_errors):
        reasons.append('TEST_DATA_UNAVAILABLE')
    if any('(pdf)' in error for error in source_errors):
        reasons.append('PDF_PARSE_UNAVAILABLE')
    if any('(image)' in error for error in source_errors):
        reasons.append('IMAGE_OCR_UNAVAILABLE')
    if any(error.startswith('microbiomeData:') for error in source_errors):
        reasons.append('MICROBIOME_DATA_UNAVAILABLE')
    return reasons


def _microbiome_pending_summary(evaluation_mode: bool) -> str:
    return '未提供可用的肠道菌群检测结果。' if evaluation_mode else MICROBIOME_RESULT_PENDING_SUMMARY


def _pending_executive_summary(request: ReportRequest, *, has_session_context: bool,
                               evaluation_mode: bool = False) -> str:
    """Build a factual interim summary while microbiome results are pending."""

    if evaluation_mode:
        return ('根据已提供的检查、检测和健康问答资料开展整体健康评估。'
            if request.hospital_data or has_session_context else '当前尚无足以生成健康评估的有效资料。')
    parts = [('肠道菌群检测结果尚在生成中，当前报告为阶段性说明，暂不展示异常菌群、菌群相关疾病或证据解读。')]
    hospital_facts = [text for item in request.hospital_data[:3] if (text := _pending_hospital_fact(item))]
    if hospital_facts:
        parts.append('现阶段已纳入的院内资料包括' + '；'.join(hospital_facts) + '。')
    elif has_session_context:
        parts.append('现阶段已收到基本信息和健康问答资料，作为后续评估的背景。')
    else:
        parts.append('现阶段仅收到受检者基本信息。')
    parts.append('菌群检测结果出具后，报告将自动更新。')
    return ''.join(parts)


def _pending_health_profile(
    request: ReportRequest,
    *,
    has_session_context: bool,
    evaluation_mode: bool = False,
) -> str:
    if evaluation_mode:
        return ('围绕当前已提供资料中的疾病、异常指标和健康问题进行综合评估。'
            if request.hospital_data or has_session_context else '当前尚无可用于健康评估的有效资料。')
    if request.hospital_data:
        names = list(dict.fromkeys(item.item_name for item in request.hospital_data if item.item_name))[:3]
        return f'当前已纳入{"、".join(names)}等院内资料；菌群检测结果出具后可进一步补充肠道微生态评估。'
    if has_session_context:
        return '当前已收到基本信息和健康问答资料；菌群检测结果出具后可进一步补充肠道微生态评估。'
    return '当前仅收到基本信息，菌群检测结果出具后可进一步补充肠道微生态评估。'


def _pending_hospital_fact(item: Any) -> str | None:
    name = ' '.join(item.item_name.split())
    if not name:
        return None
    if item.data_type == 'EXAM':
        result = ' '.join((item.exam_result or '').split())
        return f'{name}：{result[:120]}' if result else name
    value = str(item.test_result_value or '').strip()
    status = ' '.join((item.result_status_name or '').split())
    details = '，'.join(part for part in (value, status) if part)
    return f'{name}：{details}' if details else name
