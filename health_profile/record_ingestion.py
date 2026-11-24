"""已完成建档问答到受控用户健康事实的摄取。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any
from typing import Protocol
from typing import runtime_checkable

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.health_profile import PROFILE_RECORD_EXTRACTION_CAPABILITY
from utils.log import getLogger

from .candidate_identity import PreparedCandidateIdentity
from .candidate_identity import finalize_candidate_identities
from .fact_writer import HealthProfileFactWriter
from .ingestion import LIFESTYLE_FACT_LABELS
from .ingestion import FactTypeRegistry
from .ingestion import HealthFactPolicy
from .ingestion import PolicyDisposition
from .models import AssertionLevel
from .models import EffectivePeriod
from .models import FactSource
from .models import FactValueType
from .models import HealthProfileJob
from .models import HealthSubject
from .models import MemoryCandidate
from .models import ProfileIngestionSourceType
from .models import SubjectKind
from .models import SubjectStatus
from .processing import ProfileIngestionProcessingError
from .processing import ProfileIngestionProcessingResult
from .record_observations import FAMILY_REPORTED
from .record_observations import GUARDED_RECORD_TYPES
from .record_observations import OBSERVATION_TYPES
from .record_observations import fallback_history_statement
from .record_observations import normalize_record_statement
from .record_observations import source_strings
from .record_source import RecordProfileSourceRecord
from .record_source import RecordProfileStore
from .repository import HealthProfileRepository

logger = getLogger(__name__)

_CHINA_TIMEZONE = timezone(timedelta(hours=8))
_EXTRACTION_BATCH_SIZE = 12
_MAX_EXTRACTED_CANDIDATES = 64
_MAX_ATTACHED_SOURCE_CHARS = 16000
_OTHER_SUBJECT_TERMS = (
    '家族',
    '家人',
    '亲属',
    '父亲',
    '爸爸',
    '母亲',
    '妈妈',
    '父母',
    '儿子',
    '女儿',
    '孩子',
    '兄弟',
    '姐妹',
    '哥哥',
    '弟弟',
    '姐姐',
    '妹妹',
    '祖父',
    '祖母',
    '爷爷',
    '奶奶',
    '外公',
    '外婆',
    '配偶',
    '丈夫',
    '妻子',
    '老公',
    '老婆',
    '朋友',
)


@runtime_checkable
class RecordProfileService(Protocol):
    async def ensure_self_profile(
        self,
        user_id: str,
        *,
        display_name: str | None = None,
    ): ...

    async def upsert_subject(
        self,
        user_id: str,
        request_id: str,
        *,
        subject_id: str | None = None,
        kind: str | SubjectKind | None = None,
        display_name: str | None | object = None,
        relationship: Mapping[str, Any] | None | object = None,
        expected_subject_version: int | None = None,
    ): ...


@dataclass(frozen=True, slots=True)
class RecordSubjectResolution:
    """建档候选的确定性主体解析结果。"""

    candidate: MemoryCandidate
    subject: HealthSubject | None
    subject_kind: SubjectKind
    reason_code: str


@dataclass(frozen=True, slots=True)
class _StableRelationship:
    kind: SubjectKind
    relation_type: str
    display_name: str


_STABLE_RELATIONSHIPS = {
    'father': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'father',
        '父亲',
    ),
    'dad': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'father',
        '父亲',
    ),
    '爸爸': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'father',
        '父亲',
    ),
    '父亲': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'father',
        '父亲',
    ),
    'mother': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'mother',
        '母亲',
    ),
    'mom': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'mother',
        '母亲',
    ),
    '妈妈': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'mother',
        '母亲',
    ),
    '母亲': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'mother',
        '母亲',
    ),
    'paternalgrandfather': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'paternal_grandfather',
        '祖父',
    ),
    '爷爷': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'paternal_grandfather',
        '祖父',
    ),
    '祖父': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'paternal_grandfather',
        '祖父',
    ),
    'paternalgrandmother': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'paternal_grandmother',
        '祖母',
    ),
    '奶奶': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'paternal_grandmother',
        '祖母',
    ),
    '祖母': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'paternal_grandmother',
        '祖母',
    ),
    'maternalgrandfather': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'maternal_grandfather',
        '外祖父',
    ),
    '外公': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'maternal_grandfather',
        '外祖父',
    ),
    '外祖父': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'maternal_grandfather',
        '外祖父',
    ),
    'maternalgrandmother': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'maternal_grandmother',
        '外祖母',
    ),
    '外婆': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'maternal_grandmother',
        '外祖母',
    ),
    '外祖母': _StableRelationship(
        SubjectKind.BLOOD_RELATIVE,
        'maternal_grandmother',
        '外祖母',
    ),
}


class RecordSubjectResolver:
    """只自动复用不具有多人歧义的亲属主体。"""

    VERSION = 'record-subject-resolver-v1'

    def __init__(
        self,
        repository: HealthProfileRepository,
        profile_service: RecordProfileService,
    ) -> None:
        if not isinstance(repository, HealthProfileRepository):
            raise TypeError(
                'repository must implement HealthProfileRepository',
            )
        if not isinstance(profile_service, RecordProfileService):
            raise TypeError(
                'profile_service must implement RecordProfileService',
            )
        self._repository = repository
        self._profile_service = profile_service

    async def resolve(
        self,
        candidate: MemoryCandidate,
        *,
        user_id: str,
        self_subject: HealthSubject,
    ) -> RecordSubjectResolution:
        kind = candidate.subject_kind_hint
        if candidate.subject_id == self_subject.subject_id:
            if kind not in {None, SubjectKind.SELF}:
                return self._unresolved(candidate, kind, 'subject_kind_mismatch')
            return RecordSubjectResolution(
                candidate=candidate,
                subject=self_subject,
                subject_kind=SubjectKind.SELF,
                reason_code='self_subject',
            )
        if candidate.subject_id is not None:
            subject = await self._repository.get_subject(
                user_id,
                candidate.subject_id,
            )
            if (
                subject is None
                or subject.status is not SubjectStatus.ACTIVE
                or (kind is not None and subject.kind is not kind)
            ):
                return self._unresolved(
                    candidate,
                    kind or SubjectKind.UNKNOWN,
                    'subject_reference_invalid',
                )
            return RecordSubjectResolution(
                candidate=candidate,
                subject=subject,
                subject_kind=subject.kind,
                reason_code='subject_reference',
            )
        if kind is SubjectKind.SELF:
            return RecordSubjectResolution(
                candidate=candidate.model_copy(
                    update={
                        'subject_id': self_subject.subject_id,
                    }
                ),
                subject=self_subject,
                subject_kind=SubjectKind.SELF,
                reason_code='self_subject',
            )
        if kind is None or kind is SubjectKind.UNKNOWN:
            return self._unresolved(
                candidate,
                SubjectKind.UNKNOWN,
                'subject_kind_unknown',
            )

        relationship = _stable_relationship(candidate.subject_hint)
        if relationship is None or relationship.kind is not kind:
            return self._unresolved(
                candidate,
                kind,
                'subject_requires_confirmation',
            )
        matches = tuple(
            subject
            for subject in await self._repository.list_subjects(user_id)
            if (
                subject.status is SubjectStatus.ACTIVE
                and subject.kind is relationship.kind
                and subject.relationship is not None
                and subject.relationship.relation_type == relationship.relation_type
            )
        )
        if len(matches) > 1:
            return self._unresolved(candidate, kind, 'subject_ambiguous')
        if matches:
            subject = matches[0]
            return RecordSubjectResolution(
                candidate=candidate.model_copy(
                    update={
                        'subject_id': subject.subject_id,
                    }
                ),
                subject=subject,
                subject_kind=subject.kind,
                reason_code='stable_relation_reused',
            )

        result = await self._profile_service.upsert_subject(
            user_id,
            _subject_resolution_request_id(user_id, relationship),
            kind=relationship.kind,
            display_name=relationship.display_name,
            relationship={
                'relationType': relationship.relation_type,
                'isBloodRelative': (relationship.kind is SubjectKind.BLOOD_RELATIVE),
            },
        )
        subject = result.subject
        if (
            subject.kind is not relationship.kind
            or subject.relationship is None
            or subject.relationship.relation_type != relationship.relation_type
        ):
            raise ProfileIngestionProcessingError(
                'subject.resolution_conflict',
                '稳定亲属主体创建结果与请求不一致',
            )
        return RecordSubjectResolution(
            candidate=candidate.model_copy(
                update={
                    'subject_id': subject.subject_id,
                }
            ),
            subject=subject,
            subject_kind=subject.kind,
            reason_code='stable_relation_created',
        )

    @staticmethod
    def _unresolved(
        candidate: MemoryCandidate,
        kind: SubjectKind,
        reason_code: str,
    ) -> RecordSubjectResolution:
        return RecordSubjectResolution(
            candidate=candidate.model_copy(update={'subject_id': None}),
            subject=None,
            subject_kind=kind,
            reason_code=reason_code,
        )


# 解析器已经同时服务建档和普通聊天候选；保留 Record* 名称兼容首版入口，
# 新代码使用与来源无关的别名。
SubjectProfileService = RecordProfileService
CandidateSubjectResolution = RecordSubjectResolution
CandidateSubjectResolver = RecordSubjectResolver


class _ExtractedCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    answer_index: int = Field(alias='answerIndex', ge=0)
    fact_type: str = Field(alias='factType', min_length=1, max_length=128)
    display_name: str = Field(alias='displayName', min_length=1, max_length=128)
    value_type: FactValueType = Field(alias='valueType')
    value: Any
    subject_kind: SubjectKind = Field(alias='subjectKind')
    subject_hint: str | None = Field(
        default=None,
        alias='subjectHint',
        max_length=128,
    )
    evidence_text: str = Field(
        alias='evidenceText',
        min_length=1,
        max_length=2048,
    )
    confidence: float = Field(ge=0, le=1)


class _ExtractionEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')

    candidates: list[_ExtractedCandidate] = Field(max_length=64)


class RecordBasicInfoMapper:
    """只映射具备明确用户输入来源的基础信息。"""

    VERSION = 'record-basic-info-mapper-v2'

    def map(
        self,
        record: RecordProfileSourceRecord,
        *,
        subject_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        session = record.session
        values = session.get('extractedInfo')
        sources = session.get('extractedInfoSources')
        if not isinstance(values, dict) or not isinstance(sources, dict):
            # 旧快照没有字段级来源，不能把报告抽取值误标成用户确认值。
            return ()
        observed_at = _session_time(
            session.get('qaCompletedAt') or session.get('completedAt'),
        )
        if observed_at is None:
            return ()
        candidates = []
        specifications = (
            (
                'gender',
                'demographic.sex',
                '性别',
                FactValueType.CODED,
                _gender_value,
            ),
            (
                'age',
                'demographic.age',
                '年龄',
                FactValueType.QUANTITY,
                lambda value: _quantity(
                    value,
                    unit='years',
                    low=0,
                    high=150,
                ),
            ),
            (
                'height',
                'anthropometric.height',
                '身高',
                FactValueType.QUANTITY,
                lambda value: _quantity(value, unit='cm', low=50, high=250),
            ),
            (
                'weight',
                'anthropometric.weight',
                '体重',
                FactValueType.QUANTITY,
                lambda value: _quantity(value, unit='kg', low=2, high=500),
            ),
        )
        for field, fact_type, display_name, value_type, normalizer in specifications:
            if sources.get(field) != 'user_input':
                continue
            normalized = normalizer(values.get(field))
            if normalized is None:
                continue
            pointer = f'/extractedInfo/{field}'
            candidates.append(
                MemoryCandidate(
                    candidate_id=_candidate_id(
                        'record_basic',
                        record.session_id,
                        pointer,
                        fact_type,
                    ),
                    subject_id=subject_id,
                    subject_hint='本人',
                    fact_type=fact_type,
                    display_name=display_name,
                    value_type=value_type,
                    value=normalized,
                    assertion_level=AssertionLevel.USER_CONFIRMED,
                    confidence=1.0,
                    effective_period=EffectivePeriod(start=observed_at),
                    source=FactSource(
                        source_type=ProfileIngestionSourceType.RECORD.value,
                        source_id=record.session_id,
                        source_revision=record.source_revision,
                        pointer=pointer,
                        observed_at=observed_at,
                        excerpt_hash=_sha256_json(values.get(field)),
                    ),
                )
            )
        return tuple(candidates)


class RecordAnswerCandidateExtractor:
    """通过受控无状态 Agent 提取回答及创建附带数据的候选。"""

    VERSION = 'record-answer-candidate-extractor-v11'

    def __init__(
        self,
        agent_hub: AgentHub,
        *,
        registry: FactTypeRegistry | None = None,
        source_type: ProfileIngestionSourceType = ProfileIngestionSourceType.RECORD,
    ) -> None:
        if not isinstance(agent_hub, AgentHub):
            raise TypeError('agent_hub must be an AgentHub')
        if source_type not in (ProfileIngestionSourceType.RECORD, ProfileIngestionSourceType.QUESTIONNAIRE):
            raise ValueError('unsupported answer source')
        self._source_type = source_type
        self._agent_hub = agent_hub
        self._registry = registry or FactTypeRegistry.default()
        self._allowed_fact_types = tuple(
            definition.fact_type
            for definition in self._registry.list_definitions()
            if source_type in definition.permitted_source_types
        )

    async def extract(
        self,
        record: RecordProfileSourceRecord,
        *,
        subject_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        return await self.extract_answer_rows(record, _sanitized_answers(record.session), subject_id=subject_id)

    async def extract_answer_rows(
        self,
        record: RecordProfileSourceRecord,
        answers: list[dict[str, Any]],
        *,
        subject_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        """Shared evidence checks; callers supply source-specific immutable rows."""
        if not answers:
            return ()
        extracted = []
        for offset in range(0, len(answers), _EXTRACTION_BATCH_SIZE):
            batch = answers[offset : offset + _EXTRACTION_BATCH_SIZE]
            extracted.extend(await self._extract_batch(record, batch))
            if len(extracted) > _MAX_EXTRACTED_CANDIDATES:
                raise ProfileIngestionProcessingError(
                    'extractor.too_many_candidates',
                    '建档候选事实数量超过单次摄取限制',
                )

        for answer in answers:
            if answer.get('sourceKind') not in {'qa_answer', 'questionnaire_answer'} or any(
                item.answer_index == answer['answerIndex'] and item.fact_type in GUARDED_RECORD_TYPES
                for item in extracted
            ):
                continue
            statement = fallback_history_statement(
                answer=answer['answer'], question=answer['question'], dimension=answer.get('intakeDimension')
            )
            if statement is not None:
                extracted.append(
                    _ExtractedCandidate(
                        answerIndex=answer['answerIndex'],
                        factType=statement.fact_type,
                        displayName=statement.display_name,
                        valueType=statement.value_type,
                        value=statement.value,
                        subjectKind=statement.subject_kind,
                        subjectHint=statement.subject_hint,
                        evidenceText=statement.evidence_text,
                        confidence=0.8,
                    )
                )
        answer_by_index = {item['answerIndex']: item for item in answers}
        expanded = []
        for item in extracted:
            conditions = item.value.get('conditions') if isinstance(item.value, dict) else None
            if item.fact_type in OBSERVATION_TYPES and isinstance(conditions, list) and 1 < len(conditions) <= 16:
                expanded.extend(
                    item.model_copy(update={'value': {**item.value, 'conditions': [condition]}})
                    for condition in conditions
                )
            else:
                expanded.append(item)
            if len(expanded) > _MAX_EXTRACTED_CANDIDATES:
                raise ProfileIngestionProcessingError(
                    'extractor.too_many_candidates', '建档候选事实数量超过单次摄取限制'
                )
        prepared_candidates = []
        for item in expanded:
            answer = answer_by_index.get(item.answer_index)
            if answer is None:
                raise ProfileIngestionProcessingError(
                    'extractor.invalid_answer_reference',
                    '候选引用了不存在的建档回答',
                )
            if self._registry.get(item.fact_type) is None:
                raise ProfileIngestionProcessingError(
                    'extractor.unknown_fact_type',
                    '候选使用了未注册的事实类型',
                )
            evidence_text = item.evidence_text.strip()
            if item.fact_type in LIFESTYLE_FACT_LABELS:
                evidence_text = _lifestyle_source_evidence(
                    answer['answer'],
                    evidence_text,
                    subject_kind=item.subject_kind,
                    subject_hint=item.subject_hint,
                    question=answer['question'],
                )
            if not evidence_text or (
                item.fact_type not in LIFESTYLE_FACT_LABELS
                and not _source_contains_evidence(answer['answer'], evidence_text)
            ):
                logger.warning(
                    '建档画像候选已跳过: '
                    f'session_id={record.session_id}, '
                    f'answer_index={item.answer_index}, '
                    f'fact_type={item.fact_type}, '
                    f'subject_kind={item.subject_kind.value}, '
                    'reason_code=evidence_not_verbatim',
                )
                continue
            source_kind = answer.get('sourceKind', 'qa_answer')
            if source_kind not in {'qa_answer', 'questionnaire_answer'} and (
                item.subject_kind is not SubjectKind.SELF or item.fact_type in OBSERVATION_TYPES
            ):
                continue
            if item.fact_type in GUARDED_RECORD_TYPES:
                statement = normalize_record_statement(
                    fact_type=item.fact_type,
                    value=item.value,
                    display_name=item.display_name,
                    evidence=evidence_text,
                    answer=answer['answer'],
                    question=answer['question'],
                    dimension=answer.get('intakeDimension'),
                    subject_kind=item.subject_kind.value,
                    subject_hint=item.subject_hint,
                    stable_relationship=(
                        relationship.kind is SubjectKind.BLOOD_RELATIVE
                        if (relationship := _stable_relationship(item.subject_hint)) is not None
                        else False
                    ),
                )
                if statement is None and source_kind in {'qa_answer', 'questionnaire_answer'}:
                    statement = fallback_history_statement(
                        answer=answer['answer'], question=answer['question'], dimension=answer.get('intakeDimension')
                    )
                if statement is None:
                    continue
                item = item.model_copy(
                    update={
                        'fact_type': statement.fact_type,
                        'value_type': FactValueType(statement.value_type),
                        'value': statement.value,
                        'display_name': statement.display_name,
                        'subject_kind': SubjectKind(statement.subject_kind),
                        'subject_hint': statement.subject_hint,
                    }
                )
                evidence_text = statement.evidence_text
            if source_kind not in {'qa_answer', 'questionnaire_answer'} and item.subject_kind is not SubjectKind.SELF:
                logger.warning(
                    '建档附带数据候选已跳过: '
                    f'session_id={record.session_id}, '
                    f'answer_index={item.answer_index}, '
                    f'fact_type={item.fact_type}, '
                    f'subject_kind={item.subject_kind.value}, '
                    'reason_code=attached_source_requires_self',
                )
                continue
            # Lifestyle evidence already passed source-local ownership checks;
            # the generic guard cannot distinguish a subject from a companion.
            if (
                item.fact_type not in LIFESTYLE_FACT_LABELS
                and item.fact_type != FAMILY_REPORTED
                and not _subject_evidence_is_safe(
                    subject_kind=item.subject_kind,
                    subject_hint=item.subject_hint,
                    evidence_text=evidence_text,
                    question=answer['question'],
                )
            ):
                logger.warning(
                    '建档画像候选已跳过: '
                    f'session_id={record.session_id}, '
                    f'answer_index={item.answer_index}, '
                    f'fact_type={item.fact_type}, '
                    f'subject_kind={item.subject_kind.value}, '
                    'reason_code=subject_evidence_unsafe',
                )
                continue
            subject_slot = _subject_slot(item.subject_kind, item.subject_hint)
            value_type = item.value_type
            try:
                value = _normalized_model_value(value_type, item.value)
            except (TypeError, ValueError) as error:
                if item.fact_type in LIFESTYLE_FACT_LABELS and not _LIFESTYLE_CAUSAL_LANGUAGE.search(evidence_text):
                    value_type, value = FactValueType.TEXT, evidence_text
                else:
                    logger.warning(
                        '建档画像候选值无法规范化: '
                        f'session_id={record.session_id}, '
                        f'answer_index={item.answer_index}, '
                        f'fact_type={item.fact_type}, '
                        f'value_type={item.value_type.value}, '
                        f'error_type={type(error).__name__}',
                    )
                    raise ProfileIngestionProcessingError(
                        'extractor.invalid_candidate_value',
                        '建档候选值与声明类型不一致',
                    ) from error
            if item.fact_type in LIFESTYLE_FACT_LABELS:
                value = _lifestyle_behavior_only(value)
                if not _lifestyle_value_is_supported(value_type, value, evidence_text):
                    if _LIFESTYLE_CAUSAL_LANGUAGE.search(evidence_text):
                        logger.warning(
                            'Lifestyle candidate rejected: session_id=%s, answer_index=%s, '
                            'fact_type=%s, reason_code=lifestyle_causal_evidence',
                            record.session_id,
                            item.answer_index,
                            item.fact_type,
                        )
                        continue
                    # The evidence was already checked verbatim against the
                    # user's answer. Keep it instead of losing a real behavior
                    # because a model paraphrased or invented structured details.
                    value_type, value = FactValueType.TEXT, evidence_text
                if item.fact_type == 'lifestyle.stress_coping' and not _stress_coping_value_is_behavior(value):
                    if not _stress_coping_value_is_behavior(evidence_text):
                        logger.warning(
                            'Stress coping candidate rejected: session_id=%s, answer_index=%s, '
                            'reason_code=stress_behavior_unavailable',
                            record.session_id,
                            item.answer_index,
                        )
                        continue
                    value_type, value = FactValueType.TEXT, evidence_text
            observed_at = _session_time(answer.get('answeredAt'))
            if observed_at is None:
                observed_at = _session_time(
                    record.session.get('qaCompletedAt') or record.session.get('completedAt'),
                )
            if observed_at is None:
                raise ProfileIngestionProcessingError(
                    'source.time_unavailable',
                    '建档回答缺少可解析时间',
                )
            pointer = answer.get('sourcePointer')
            if not isinstance(pointer, str) or not pointer.startswith('/'):
                raise ProfileIngestionProcessingError(
                    'source.pointer_invalid',
                    '建档候选引用的来源路径无效',
                )
            value_fingerprint = _sha256_json(
                {
                    'valueType': value_type.value,
                    'value': value,
                }
            )
            candidate_prefix = (
                'questionnaire_answer'
                if self._source_type is ProfileIngestionSourceType.QUESTIONNAIRE
                else 'record_answer'
                if source_kind == 'qa_answer'
                else 'record_attached'
            )
            legacy_candidate_id = _candidate_id(
                candidate_prefix,
                record.session_id,
                pointer,
                item.fact_type,
                subject_slot,
            )
            prepared_candidates.append(
                PreparedCandidateIdentity(
                    candidate=MemoryCandidate(
                        candidate_id=_candidate_id(
                            candidate_prefix,
                            record.session_id,
                            pointer,
                            item.fact_type,
                            subject_slot,
                            value_fingerprint,
                        ),
                        subject_id=(subject_id if item.subject_kind is SubjectKind.SELF else None),
                        subject_hint=item.subject_hint,
                        subject_kind_hint=item.subject_kind,
                        fact_type=item.fact_type,
                        display_name=LIFESTYLE_FACT_LABELS.get(item.fact_type, item.display_name.strip()),
                        value_type=value_type,
                        value=value,
                        assertion_level=AssertionLevel.USER_REPORTED,
                        confidence=min(item.confidence, 0.8),
                        effective_period=EffectivePeriod(start=observed_at),
                        source=FactSource(
                            source_type=self._source_type.value,
                            source_id=record.session_id,
                            source_revision=record.source_revision,
                            pointer=pointer,
                            observed_at=observed_at,
                            excerpt_hash=_sha256_json(
                                {
                                    'question': answer['question'],
                                    'answer': answer['answer'],
                                }
                            ),
                        ),
                        evidence_text=evidence_text,
                    ),
                    logical_slot=(
                        str(item.answer_index),
                        item.fact_type,
                        subject_slot,
                    ),
                    value_fingerprint=value_fingerprint,
                    legacy_candidate_id=legacy_candidate_id,
                )
            )
        identity_result = finalize_candidate_identities(prepared_candidates)
        if identity_result.deduplicated_count:
            logger.warning(
                '建档画像重复候选已去重: '
                f'session_id={record.session_id}, '
                f'deduplicated_count={identity_result.deduplicated_count}, '
                'reason_code=duplicate_candidate_value',
            )
        return identity_result.candidates

    async def _extract_batch(
        self,
        record: RecordProfileSourceRecord,
        answers: list[dict[str, Any]],
    ) -> list[_ExtractedCandidate]:
        content = json.dumps(
            {
                'allowedFactTypes': self._allowed_fact_types,
                'answers': answers,
            },
            ensure_ascii=False,
            separators=(',', ':'),
        )
        try:
            result = await self._agent_hub.invoke(
                PROFILE_RECORD_EXTRACTION_CAPABILITY,
                AgentRequest({'source_content': content}),
                AgentRunContext.create(
                    user_id=record.user_id,
                    session_id=record.session_id,
                    request_id=(f'profile-extract:{record.session_id}:{answers[0]["answerIndex"]}'),
                ),
            )
        except Exception as error:
            logger.warning(
                '建档画像候选提取 Agent 执行失败: '
                f'session_id={record.session_id}, '
                f'first_answer_index={answers[0]["answerIndex"]}, '
                f'error_type={type(error).__name__}',
            )
            raise ProfileIngestionProcessingError(
                'extractor.agent_failure',
                '建档候选提取 Agent 执行失败',
            ) from error
        content = result.data.get('content')
        if not isinstance(content, str):
            raise ProfileIngestionProcessingError(
                'extractor.invalid_output',
                '建档候选提取 Agent 未返回文本',
            )
        try:
            return _parse_extraction(content).candidates
        except (TypeError, ValueError, ValidationError) as error:
            logger.warning(
                '建档画像候选提取结果无法解析: '
                f'session_id={record.session_id}, '
                f'first_answer_index={answers[0]["answerIndex"]}, '
                f'error_type={type(error).__name__}',
            )
            raise ProfileIngestionProcessingError(
                'extractor.invalid_output',
                '建档候选提取 Agent 返回格式不合法',
            ) from error


class RecordProfileProcessor:
    """解析建档候选主体，并把候选受控写入对应画像。"""

    GRAPH_VERSION = 'profile-ingest-record-v5'

    def __init__(
        self,
        *,
        store: RecordProfileStore,
        repository: HealthProfileRepository,
        profile_service: RecordProfileService,
        agent_hub: AgentHub,
        basic_mapper: RecordBasicInfoMapper | None = None,
        answer_extractor: RecordAnswerCandidateExtractor | None = None,
        subject_resolver: RecordSubjectResolver | None = None,
        policy: HealthFactPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(store, RecordProfileStore):
            raise TypeError('store must implement RecordProfileStore')
        if not isinstance(repository, HealthProfileRepository):
            raise TypeError(
                'repository must implement HealthProfileRepository',
            )
        if not isinstance(profile_service, RecordProfileService):
            raise TypeError(
                'profile_service must implement RecordProfileService',
            )
        self._store = store
        self._repository = repository
        self._profile_service = profile_service
        self._basic_mapper = basic_mapper or RecordBasicInfoMapper()
        self._answer_extractor = answer_extractor or (RecordAnswerCandidateExtractor(agent_hub))
        self._subject_resolver = subject_resolver or RecordSubjectResolver(
            repository,
            profile_service,
        )
        self._policy = policy or HealthFactPolicy()
        self._writer = HealthProfileFactWriter(
            repository,
            clock=clock,
            id_factory=id_factory,
        )

    async def process(
        self,
        job: HealthProfileJob,
    ) -> ProfileIngestionProcessingResult:
        if not isinstance(job, HealthProfileJob):
            raise TypeError('job must be a HealthProfileJob')
        if job.source_type is not ProfileIngestionSourceType.RECORD:
            raise ProfileIngestionProcessingError(
                'source.unsupported',
                '当前处理器只支持建档问答任务',
            )
        record = await self._store.get_completed(
            user_id=job.user_id,
            session_id=job.source_id,
        )
        if record is None:
            raise ProfileIngestionProcessingError(
                'source.not_found',
                '找不到摄取任务引用的已完成建档快照',
            )
        if record.user_id != job.user_id or record.source_revision != job.source_revision:
            raise ProfileIngestionProcessingError(
                'source.mismatch',
                '建档快照与摄取任务不一致',
            )
        await self._repository.ensure_indexes()
        display_name = _user_input_name(record.session)
        bootstrap = await self._profile_service.ensure_self_profile(
            job.user_id,
            display_name=display_name,
        )
        subject = bootstrap.subject
        if job.subject_id is not None and job.subject_id != subject.subject_id:
            raise ProfileIngestionProcessingError(
                'subject.mismatch',
                '建档任务主体与用户本人主体不一致',
            )

        direct = self._basic_mapper.map(
            record,
            subject_id=subject.subject_id,
        )
        direct_result = await self._write_candidates(
            job=job,
            candidates=direct,
            self_subject=subject,
        )

        # 确定性基础事实必须先于 LLM 候选提取落库。提取失败时任务仍会
        # 按统一策略重试，但已确认的性别、年龄和人体测量值不会被连带丢失。
        extracted = await self._answer_extractor.extract(
            record,
            subject_id=subject.subject_id,
        )
        extracted_result = await self._write_candidates(
            job=job,
            candidates=extracted,
            self_subject=subject,
        )
        facts = direct_result[0] + extracted_result[0]
        replayed = direct_result[1] + extracted_result[1]
        rejected = direct_result[2] + extracted_result[2]
        candidates = direct + extracted
        return ProfileIngestionProcessingResult(
            graph_version=self.GRAPH_VERSION,
            policy_version=self._policy.VERSION,
            extractor_version=(
                f'{self._basic_mapper.VERSION}+{self._answer_extractor.VERSION}+{self._subject_resolver.VERSION}'
            ),
            candidate_count=len(candidates),
            fact_count=facts,
            replayed_fact_count=replayed,
            rejected_candidate_count=rejected,
        )

    async def _write_candidates(
        self,
        *,
        job: HealthProfileJob,
        candidates: tuple[MemoryCandidate, ...],
        self_subject: HealthSubject,
    ) -> tuple[int, int, int]:
        facts = 0
        replayed = 0
        rejected = 0
        for candidate in candidates:
            resolution = await self._subject_resolver.resolve(
                candidate,
                user_id=job.user_id,
                self_subject=self_subject,
            )
            decision = self._policy.evaluate(
                resolution.candidate,
                subject_kind=resolution.subject_kind,
            )
            if decision.disposition in {
                PolicyDisposition.REJECTED,
                PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION,
            }:
                logger.warning(
                    '建档画像候选未写入: '
                    f'job_id={job.job_id}, '
                    f'candidate_id={candidate.candidate_id}, '
                    f'fact_type={candidate.fact_type}, '
                    f'disposition={decision.disposition.value}, '
                    f'reason_code={decision.reason_code}',
                )
                rejected += 1
                continue
            if resolution.subject is None:
                raise ProfileIngestionProcessingError(
                    'subject.resolution_inconsistent',
                    '主体未解析的候选不能写入用户画像',
                )
            created = await self._writer.write(
                job=job,
                candidate=resolution.candidate,
                decision=decision,
                subject=resolution.subject,
            )
            if created:
                facts += 1
            else:
                replayed += 1
        return facts, replayed, rejected


def _sanitized_answers(session: dict[str, Any]) -> list[dict[str, Any]]:
    history = session.get('qaHistory')
    if not isinstance(history, list):
        history = []
    answers = []
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        question = item.get('question')
        if not isinstance(question, dict):
            continue
        question_text = question.get('question')
        if not isinstance(question_text, str) or not question_text.strip():
            continue
        answer = item.get('answer')
        if answer is None:
            continue
        answers.append(
            {
                'answerIndex': index,
                'sourceKind': 'qa_answer',
                'sourcePointer': f'/qaHistory/{index}/answer',
                'questionId': question.get('questionId'),
                'questionType': question.get('questionType'),
                'intakeDimension': question.get('intakeDimension'),
                'question': question_text.strip(),
                'answer': answer,
                'answeredAt': item.get('answeredAt'),
            }
        )

    health_data = session.get('healthData')
    if not isinstance(health_data, dict):
        return answers

    raw_sources = []
    next_index = len(history)
    for source_kind, label, field_name in (
        ('hospital_data', '院内数据', 'hospitalData'),
        ('we_run_data', '微信运动数据', 'weRunData'),
    ):
        values = health_data.get(field_name)
        if not isinstance(values, list):
            continue
        for item_index, value in enumerate(values):
            raw_sources.append(
                {
                    'answerIndex': next_index + item_index,
                    'sourceKind': source_kind,
                    'sourcePointer': f'/healthData/{field_name}/{item_index}',
                    'questionId': f'source_{source_kind}_{item_index}',
                    'questionType': 'object',
                    'question': f'用户在建档创建时提供的{label}',
                    'answer': value,
                    'answeredAt': _structured_source_time(value),
                }
            )
        next_index += len(values)

    smart_device_data = health_data.get('smartDeviceData')
    if isinstance(smart_device_data, dict):
        for item_index, (key, value) in enumerate(
            sorted(
                smart_device_data.items(),
                key=lambda item: str(item[0]),
            )
        ):
            raw_sources.append(
                {
                    'answerIndex': next_index + item_index,
                    'sourceKind': 'smart_device_data',
                    'sourcePointer': ('/healthData/smartDeviceData/' + _json_pointer_token(str(key))),
                    'questionId': f'source_smart_device_{item_index}',
                    'questionType': 'object',
                    'question': f'用户在建档创建时提供的智能设备数据：{key}',
                    'answer': value,
                    'answeredAt': _structured_source_time(value),
                }
            )

    used_chars = 0
    for source in raw_sources:
        try:
            source_chars = len(
                json.dumps(
                    source['answer'],
                    ensure_ascii=False,
                    separators=(',', ':'),
                )
            )
        except (TypeError, ValueError) as error:
            logger.warning(
                '建档附带数据无法序列化，已跳过: '
                f'source_kind={source["sourceKind"]}, '
                f'answer_index={source["answerIndex"]}, '
                f'error_type={type(error).__name__}',
            )
            continue
        if used_chars + source_chars > _MAX_ATTACHED_SOURCE_CHARS:
            logger.warning(
                '建档附带数据超过画像提取预算，已跳过: '
                f'source_kind={source["sourceKind"]}, '
                f'answer_index={source["answerIndex"]}, '
                f'source_chars={source_chars}',
            )
            continue
        used_chars += source_chars
        answers.append(source)
    return answers


def _structured_source_time(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in (
        'observedAt',
        'recordedAt',
        'reportTime',
        'measurementTime',
        'date',
        'time',
        'timestamp',
        'createdAt',
        'createTime',
    ):
        candidate = value.get(key)
        if _session_time(candidate) is not None:
            return candidate.strip()
    return None


def _json_pointer_token(value: str) -> str:
    return value.replace('~', '~0').replace('/', '~1')


def _parse_extraction(content: str) -> _ExtractionEnvelope:
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('missing JSON object')
    return _ExtractionEnvelope.model_validate_json(text[start : end + 1])


def _normalized_model_value(value_type: FactValueType, value: Any) -> Any:
    if value_type in {FactValueType.TEXT, FactValueType.CODED}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError('text candidate requires a non-empty string')
        return value.strip()[:1024]
    if value_type is FactValueType.BOOLEAN:
        if not isinstance(value, bool):
            raise TypeError('boolean candidate requires a boolean')
        return value
    if value_type is FactValueType.QUANTITY:
        if not isinstance(value, dict):
            raise TypeError('quantity candidate requires an object')
        number = value.get('number')
        unit = value.get('unit')
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise TypeError('quantity number must be numeric')
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError('quantity unit cannot be empty')
        return {'number': float(number), 'unit': unit.strip()[:32]}
    if value_type is FactValueType.DATE:
        if not isinstance(value, str):
            raise TypeError('date candidate requires a string')
        return date.fromisoformat(value).isoformat()
    if value_type is FactValueType.OBJECT:
        if not isinstance(value, dict) or not value:
            raise TypeError('object candidate requires a non-empty object')
        if len(_json_bytes(value)) > 4096:
            raise ValueError('object candidate is too large')
        return value
    raise ValueError(f'unsupported record candidate value type: {value_type}')


_LIFESTYLE_CAUSAL_LANGUAGE = re.compile(
    r'导致|引起|造成|诱发|病因|诱因|致病|致使|\b(?:caus(?:e[ds]?|ing|al)|trigger(?:s|ed|ing)?)\b',
    re.IGNORECASE,
)
_LIFESTYLE_INTERPRETATION_KEYS = frozenset(
    {
        'cause',
        'causes',
        'causal',
        'causality',
        'possiblecause',
        'risk',
        'riskfactor',
        'association',
        'associations',
        'diagnosis',
        'reasoning',
        'explanation',
    }
)
_STRESS_DIAGNOSTIC_LANGUAGE = re.compile(
    r'焦虑(?:症|障碍)|抑郁(?:症|障碍)|精神(?:病|疾病|障碍)'
    r'|\b(?:anxiety disorder|depress(?:ion|ive disorder)|mental (?:illness|disorder))\b',
    re.IGNORECASE,
)
_STRESS_FEELING_ONLY = re.compile(
    r'^(?:我|本人|自己|最近|近期|近来|这段时间|目前|工作|生活|学习|总是|一直|每天|有时|偶尔|感觉|觉得|的|都|也|还|[，、\s])*'
    r'(?:压力(?:很|比较|非常|特别|有点|较|太)*(?:大|高|多)'
    r'|(?:很|比较|非常|特别|有点|较|太)*(?:焦虑|抑郁|紧张|烦躁|情绪低落))'
    r'(?:[，。、；,.!！?？\s]|了|已久|最近|一直)*$'
    r'|^(?:I (?:feel|am)|feeling|very|really|always|currently|recently|a bit|so|\s)*'
    r'(?:stressed|anxious|depressed|under stress|under pressure)[.!?\s]*$',
    re.IGNORECASE,
)
_BEHAVIOR_CLAUSE_BOUNDARY = re.compile(r'[，。！？!?；;\n]|(?<!\d),|,(?!\d)|但是|不过|但|却|而是')
_SOURCE_NUMBER = re.compile(r'(?<![\d.,eE+−-])[+−-]?\d(?:[\d.]|,(?=\d))*(?:[eE][+−-]?\d+)?')
_VALID_NUMBER = re.compile(r'[+−-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][+−-]?\d+)?')
_CHINESE_BEHAVIOR_UNITS = (
    '毫升',
    '公升',
    '分升',
    '升',
    '毫克',
    '微克',
    '千克',
    '公斤',
    '公克',
    '克',
    '斤',
    '两',
    '磅',
    '盎司',
    '个小时',
    '小时',
    '分钟',
    '分',
    '秒钟',
    '秒',
    '刻钟',
    '天',
    '星期',
    '周',
    '个月',
    '月',
    '年',
    '公里',
    '千米',
    '米',
    '步',
    '层',
    '次',
    '回',
    '趟',
    '遍',
    '组',
    '包',
    '瓶',
    '杯',
    '碗',
    '份',
    '个',
    '支',
    '根',
    '盒',
    '袋',
    '罐',
    '壶',
    '口',
    '勺',
    '汤匙',
    '茶匙',
    '滴',
    '盅',
    '听',
)
_CHINESE_BEHAVIOR_UNIT_TOKEN = '|'.join(
    re.escape(unit) for unit in sorted(_CHINESE_BEHAVIOR_UNITS, key=len, reverse=True)
)
_SOURCE_UNIT = re.compile(
    rf'(?:[A-Za-zµμ%]+(?:[/·][A-Za-zµμ%0-9^²³]+)*'
    rf'|(?:{_CHINESE_BEHAVIOR_UNIT_TOKEN})(?:[/／](?:{_CHINESE_BEHAVIOR_UNIT_TOKEN}))*)'
)
_BEHAVIOR_SCOPE = re.compile(
    r'从不|从未|从来不|从来没有|不曾|没有|不再|没|不|并非|未'
    r'|只(?:有|要|在)?|如果|要是|假如|除非|当|时候|(?<!小)时(?!间)|期间|情况下'
    r'|记不清|说不清|不清楚|不确定|不知道|不记得|没注意|未注意|未留意'
    r'|工作日|休息日|节假日|周末|周[一二三四五六日天]|星期[一二三四五六日天]'
    r'|(?:每|隔)(?:[一二两三四五六七八九十几\d]+)?(?:天|周|星期|月|年|晚|早|日)'
    r'|早上|晚上|白天|夜间|平时|偶尔|有时|经常|常常|通常|往往|常(?=不|喝|吃|会)'
    r'|以前|过去|曾经|现在|目前|最近|近期|近来|一直|已经|去年|今年|上个月|这个月'
    r'|(?:近|过去|最近)(?:[一二两三四五六七八九十几\d]+)(?:个)?(?:天|周|月|年)'
)
_UNCERTAIN_BEHAVIOR = re.compile(r'记不清|说不清|不清楚|不确定|不知道|不记得|没注意|未注意|未留意|不了解')
_QUALIFIED_NEGATION = re.compile(
    r'不是|不太|不常|不经常|不总|不一定|不完全|不只|不单|未必|并非|没必要|但是|不过|但|却|而是'
)
_COMPLETE_DENIAL = re.compile(
    r'从不|从未|从来不|从来没有|不曾'
    r'|^(?:我|本人|自己)?(?:现在|目前|过去|一直|平时|也|还|同样|都)*(?:没有|不再|没|不|未)'
)
_CONDITIONAL_BEHAVIOR = re.compile(r'如果|要是|假如|除非|只有|只要|(?:忙|闲|加班|工作|休假).*(?:时|时候)|情况下')
_DETAIL_CONTINUATION = re.compile(r'^(?:每次|单次|一次|每回|持续|时长|大约|约|时间|数量|每周|每天|每晚)')
_BEHAVIOR_TIME = re.compile(r'^(?:我|本人|自己)?(?:工作日|休息日|周末|以前|过去|现在|目前|平时|晚上|早上)')
_BEHAVIOR_ACTION = re.compile(r'喝|饮|吃|吸烟|抽烟|运动|散步|跑步|睡|坐|休息')
_BEHAVIOR_TIME_CLAUSE = re.compile(
    r'(?:我|本人|自己)?(?:工作日|休息日|周末|以前|过去|现在|目前|平时|晚上|早上)(?:时|期间)?'
)
_BEHAVIOR_COORDINATE_BOUNDARY = re.compile(
    r'(?:以及|并且|和|及|、)(?=(?:我|本人|自己)?(?:喝|饮|吃|吸烟|抽烟|运动|散步|跑步|睡|坐|休息))'
)
_BEHAVIOR_OWNER = re.compile(
    r'(?P<other>(?:我(?:的)?)?(?:'
    + '|'.join(re.escape(term) for term in sorted((*_OTHER_SUBJECT_TERMS, '同事', '邻居'), key=len, reverse=True))
    + r'))|(?P<self>本人|自己|我(?!们))|(?P<ambiguous>我们|他们|她们|他|她)'
)
_STRESS_STATE = re.compile(
    r'^(?:(?:我|本人|自己|最近|近期|近来|目前|一直|总是|每天|工作|生活|学习|感觉|觉得|感到|出现|变得|容易|都|也|还|有点|比较|非常|很|挺|太|的)\s*)*'
    r'(?:压力|焦虑|抑郁|紧张|烦躁|烦恼|心烦|心情|情绪|担心|害怕|恐惧|孤独|难过|疲惫|疲劳|累|失眠|血压|血糖|心率|头痛|头疼|不舒服|不适)'
    r'|^(?:I (?:feel|am)|feeling|very|really|always|currently|recently|a bit|so|\s)*'
    r'(?:stressed|anxious|depressed|under stress|under pressure|tired|sad|worried)\b',
    re.IGNORECASE,
)
_COPING_ACTION = re.compile(
    r'^(?:做|打|玩|看|听|读|写|画|练|学|种|养|走|跑|骑|游|唱|弹|跳|躺|睡|聊|逛|搓|钓|刻|编|捏|去|出去|出门|外出|捣鼓|烤|烘焙|烹饪|整理|收拾|照料|参加|约|找|陪|与|和|跟|独处|冥想|运动|阅读|散步|休息|歇|放松|暂停|留出|安排|停下)'
    r'|\b(?:walk|read|listen|play|run|rest|relax|exercise|meditate|garden|cook|paint|draw|write|talk|sleep)\b',
    re.IGNORECASE,
)
_HABIT_ACTION = re.compile(
    r'(?:我|本人|自己)?(?:平时|通常|一般|经常|常常|总会|都会|会|习惯于|习惯|喜欢|选择|通过|用)'
    r'(?P<action>[^，,。！？!?；;\n]+)'
)
_ACTION_TIME = re.compile(r'^(?:每(?:天|晚|早|周|月)|周末|节假日|下班后|睡前|空闲时|有空时|忙碌时|忙时)(?:我)?')
_REST_ARRANGEMENT = re.compile(r'休息|放松|歇|暂停|独处|交流|固定节奏|专门留出时间|恢复精力|解压|减压')
_REST_INTENTION_ONLY = re.compile(
    r'^(?:我|本人|自己)?(?:需要|想要|希望|打算|准备|想|应该|不会|无法|不能|难以|没法|不知道怎么|不知道如何|不知道怎样)'
    r'(?:能|可以|要|真正|好好)?(?:休息|放松|解压|减压)'
)
_COPING_STATE_OR_CONTEXT = re.compile(
    r'压力|焦虑|抑郁|紧张|烦躁|烦恼|心烦|心情|情绪|担心|害怕|恐惧|孤独|难过|疲惫|疲劳|失眠'
    r'|血压|血糖|心率|头痛|头疼|不舒服|不适|难受|确诊|疾病|糖尿病|不清楚|记不清|不知道|不确定'
    r'|(?:工作|生活|学习|事情|任务).*(?:忙|多|重)|(?:很|挺|非常|比较|偏|太).*(?:大|高|低|差|好|累|忙|多|少)$'
)


def _behavior_boundaries(text: str) -> list[re.Match]:
    return sorted(
        [*_BEHAVIOR_CLAUSE_BOUNDARY.finditer(text), *_BEHAVIOR_COORDINATE_BOUNDARY.finditer(text)],
        key=lambda match: match.start(),
    )


def _behavior_clause_span(text: str, start: int, end: int) -> tuple[int, int]:
    boundaries = _behavior_boundaries(text)
    left = max((match.end() for match in boundaries if match.end() <= start), default=0)
    right = min((match.start() for match in boundaries if match.start() >= end), default=len(text))
    return left, right


def _behavior_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Expand a quote without reconstructing or joining the user's string values."""
    left, right = _behavior_clause_span(text, start, end)
    boundaries = _behavior_boundaries(text)
    preceding = next((match for match in boundaries if match.end() == left), None)
    if preceding and preceding.group() in {'，', ','}:
        prior_left, _ = _behavior_clause_span(text, preceding.start(), preceding.start())
        prior = text[prior_left : preceding.start()].strip()
        current = text[left:right].strip()
        if (
            _CONDITIONAL_BEHAVIOR.search(prior)
            or _BEHAVIOR_TIME_CLAUSE.fullmatch(prior)
            or _continues_behavior(prior, current)
        ):
            # A condition and an adjacent amount are part of the same statement.
            left = prior_left
    following_boundary = next((match for match in boundaries if match.start() == right), None)
    if following_boundary and following_boundary.group() in {'，', ','}:
        next_left = following_boundary.end()
        _, next_right = _behavior_clause_span(text, next_left, next_left)
        following = text[next_left:next_right].strip()
        actions = set(_BEHAVIOR_ACTION.findall(text[left:right]))
        time_continuation = _BEHAVIOR_TIME.match(following) and actions.intersection(
            _BEHAVIOR_ACTION.findall(following)
        )
        if _continues_behavior(text[left:right], following) or time_continuation:
            right = next_right
    return left, right


def _continues_behavior(prior: str, following: str) -> bool:
    if not _DETAIL_CONTINUATION.match(following):
        return False
    following_actions = set(_BEHAVIOR_ACTION.findall(following))
    return not following_actions or bool(following_actions.intersection(_BEHAVIOR_ACTION.findall(prior)))


def _behavior_owners(text: str) -> list[re.Match]:
    owners: list[re.Match] = []
    for owner in _BEHAVIOR_OWNER.finditer(text):
        if owner.lastgroup == 'other' and owners:
            left, _ = _behavior_clause_span(text, owner.start(), owner.end())
            lead = text[max(left, owners[-1].end()) : owner.start()]
            if re.search(r'(?:和|与|跟|陪|陪着|陪同|同)\s*$', lead) and not _BEHAVIOR_ACTION.search(lead):
                # A companion of an already named actor is not a new subject.
                continue
        owners.append(owner)
    return owners


def _behavior_owner_key(match: re.Match) -> str:
    if match.lastgroup == 'self':
        return SubjectKind.SELF.value
    if match.lastgroup == 'other':
        name = re.sub(r'^我(?:的)?', '', match.group())
        relationship = _stable_relationship(name)
        return f'{relationship.kind.value}:{relationship.relation_type}' if relationship else f'other:{name}'
    return 'ambiguous'


def _lifestyle_source_evidence(
    answer: Any,
    quote: str,
    *,
    subject_kind: SubjectKind,
    subject_hint: str | None,
    question: str,
) -> str | None:
    """Resolve every literal occurrence before evaluating any model-supplied value."""
    if not quote:
        return None
    contexts: set[tuple[str, str]] = set()
    relationship = _stable_relationship(subject_hint)
    expected_owner = (
        SubjectKind.SELF.value
        if subject_kind is SubjectKind.SELF
        else f'{relationship.kind.value}:{relationship.relation_type}'
        if relationship and relationship.kind is subject_kind
        else f'other:{subject_hint}'
    )
    for text in source_strings(answer):
        owners = _behavior_owners(text)
        for literal in re.finditer(f'(?={re.escape(quote)})', text):
            start, end = literal.start(), literal.start() + len(quote)
            clause_left, clause_right = _behavior_clause_span(text, start, end)
            left, right = _behavior_span(text, start, end)
            local_owners = [owner for owner in owners if left <= owner.start() < right]
            inherited = [owner for owner in owners if owner.start() < left]
            if not local_owners and inherited:
                local_owners = [inherited[-1]]
                # Keep the explicit owner in the same contiguous source slice.
                left = local_owners[0].start()
            owner_keys = {_behavior_owner_key(owner) for owner in local_owners}
            if not owner_keys:
                # Unmarked replies to a self-directed lifestyle question are valid;
                # relatives and ambiguous references require explicit attribution.
                if subject_kind is not SubjectKind.SELF or _mentions_other_subject(question):
                    continue
            elif owner_keys != {expected_owner}:
                continue
            contexts.add((text[left:right].strip(), text[clause_left:clause_right].strip()))
    # Different clauses can expand to the same paragraph, but still give the
    # quote conflicting polarity/time. Never select one using the model status.
    if len(contexts) != 1:
        return None
    return next(iter(contexts))[0]


def _behavior_context(description: str, evidence_text: str) -> str:
    """Restrict checks to one unambiguous behavior scope within verified evidence."""
    if not description:
        return ''
    contexts = set()
    for literal in re.finditer(f'(?={re.escape(description)})', evidence_text):
        left, right = _behavior_span(evidence_text, literal.start(), literal.start() + len(description))
        contexts.add(evidence_text[left:right])
    return next(iter(contexts)) if len(contexts) == 1 else ''


def _lifestyle_scope_is_supported(value: Any, evidence_text: str) -> bool:
    if isinstance(value, dict):
        for key in value:
            normalized = str(key).casefold().replace('_', '').replace('-', '')
            if normalized in {'status', 'negation', 'unknown', 'description'} and key != normalized:
                return False
    description = value.get('description') if isinstance(value, dict) else value
    if not isinstance(description, str) or not description.strip():
        return False
    context = _behavior_context(description.strip(), evidence_text)
    if not context:
        return False
    # Never trim a qualifier from the local claim. Other topics in preceding or
    # following clauses do not turn a positive behavior into a denial.
    if _BEHAVIOR_SCOPE.findall(context) != _BEHAVIOR_SCOPE.findall(description):
        return False
    if not isinstance(value, dict):
        return True
    uncertain = bool(_UNCERTAIN_BEHAVIOR.search(context))
    qualified = bool(_QUALIFIED_NEGATION.search(context) or _CONDITIONAL_BEHAVIOR.search(context))
    denied = bool(_COMPLETE_DENIAL.search(context)) and not uncertain and not qualified
    status = value.get('status')
    if 'status' in value and (not isinstance(status, str) or status not in {'reported', 'denied', 'unknown'}):
        return False
    if status == 'unknown' and not uncertain:
        return False
    if status == 'denied' and not denied:
        return False
    if status == 'reported' and (uncertain or denied):
        return False
    if 'unknown' in value and (not isinstance(value['unknown'], bool) or value['unknown'] != uncertain):
        return False
    if 'negation' in value and (not isinstance(value['negation'], bool) or value['negation'] != denied):
        return False
    return True


def _stress_coping_value_is_behavior(value: Any) -> bool:
    """Keep coping behavior distinct from a bare feeling or a clinical label."""
    if isinstance(value, dict):
        return _stress_coping_value_is_behavior(value.get('description'))
    if isinstance(value, list):
        return bool(value) and all(_stress_coping_value_is_behavior(detail) for detail in value)
    if not isinstance(value, str):
        return False
    if _STRESS_DIAGNOSTIC_LANGUAGE.search(value) or _STRESS_FEELING_ONLY.fullmatch(value.strip()):
        return False
    for clause in _BEHAVIOR_CLAUSE_BOUNDARY.split(value):
        clause = clause.strip()
        if _STRESS_STATE.search(clause) and (condition := re.search(r'(?:时候|时|之后|后)(?:我)?(.+)$', clause)):
            clause = condition[1]
        if not clause or _STRESS_STATE.search(clause) or _REST_INTENTION_ONLY.match(clause):
            continue
        if _REST_ARRANGEMENT.search(clause):
            return True
        timed = _ACTION_TIME.sub('', clause)
        if _COPING_ACTION.search(timed):
            return True
        habitual = _HABIT_ACTION.search(clause)
        if habitual and not _STRESS_STATE.search(habitual['action']):
            return True
        # Short activity names and unmarked actions are valid answers too. The
        # extractor has already classified this verbatim source as coping; do not
        # make an exhaustive hobby list a second requirement. State, uncertainty
        # and context-only answers still cannot establish a coping behavior.
        if 2 <= len(clause) <= 24 and not _COPING_STATE_OR_CONTEXT.search(clause):
            return True
    return False


def _lifestyle_behavior_only(value: Any) -> Any:
    """Discard interpretation fields while preserving explicit behavior for independent validation."""
    if isinstance(value, dict):
        return {
            key: _lifestyle_behavior_only(detail)
            for key, detail in value.items()
            if str(key).casefold().replace('_', '').replace('-', '') not in _LIFESTYLE_INTERPRETATION_KEYS
            and not _LIFESTYLE_CAUSAL_LANGUAGE.search(str(key))
        }
    if isinstance(value, list):
        return [_lifestyle_behavior_only(detail) for detail in value]
    return value


def _lifestyle_value_is_supported(value_type: FactValueType, value: Any, evidence_text: str) -> bool:
    """Keep explicit behavior details; causal interpretations belong to derived explanations."""
    if value_type is FactValueType.CODED:
        # Legacy smoking codes must not bypass the same source-scope checks.
        return _smoking_code_is_supported(value, evidence_text)
    if value_type is FactValueType.TEXT:
        return (
            _lifestyle_scope_is_supported(value, evidence_text) and _lifestyle_detail_support(value, evidence_text)[0]
        )
    if value_type is not FactValueType.OBJECT:
        return False
    if not _lifestyle_scope_is_supported(value, evidence_text):
        return False
    description = value['description'].strip()
    valid, grounded_details = _lifestyle_detail_support(value, _behavior_context(description, evidence_text))
    return valid and grounded_details > 0


def _smoking_code_is_supported(value: Any, evidence_text: str) -> bool:
    if not isinstance(value, str) or _UNCERTAIN_BEHAVIOR.search(evidence_text):
        return False
    if _QUALIFIED_NEGATION.search(evidence_text) or _CONDITIONAL_BEHAVIOR.search(evidence_text):
        return False
    predicates = {
        'current': r'^(?:我|本人|自己)?(?:目前|现在|平时|一直)?(?:在)?(?:吸烟|抽烟)$',
        'never': r'^(?:我|本人|自己)?(?:从不|从未|从来不|从来没有)(?:吸烟|抽烟)$',
        'former': r'^(?:我|本人|自己)?(?:已经|已|目前|现在)?(?:戒烟|戒掉烟)(?:了)?$',
    }
    pattern = predicates.get(value)
    return bool(pattern and re.fullmatch(pattern, evidence_text.strip()))


def _lifestyle_detail_support(value: Any, evidence_text: str) -> tuple[bool, int]:
    if isinstance(value, dict):
        if any(
            str(key).casefold().replace('_', '').replace('-', '') in {'status', 'negation', 'unknown'} for key in value
        ):
            if not _lifestyle_scope_is_supported(value, evidence_text):
                return False, 0
        is_quantity = 'number' in value and 'unit' in value
        if is_quantity:
            number, unit = value['number'], value['unit']
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not isinstance(unit, str):
                return False, 0
            if not _number_is_supported(number, evidence_text, unit=unit):
                return False, 0
        grounded_details = 0
        for key, detail in value.items():
            normalized_key = str(key).casefold().replace('_', '').replace('-', '')
            if normalized_key in _LIFESTYLE_INTERPRETATION_KEYS or _LIFESTYLE_CAUSAL_LANGUAGE.search(str(key)):
                return False, 0
            if is_quantity and key in {'number', 'unit'}:
                grounded_details += 1
                continue
            if normalized_key == 'status' and isinstance(detail, str) and detail in {'reported', 'denied', 'unknown'}:
                continue
            if normalized_key in {'negation', 'unknown'} and isinstance(detail, bool):
                continue
            valid, count = _lifestyle_detail_support(detail, evidence_text)
            if not valid:
                return False, 0
            grounded_details += count
        return True, grounded_details
    if isinstance(value, list):
        supports = [_lifestyle_detail_support(detail, evidence_text) for detail in value]
        return all(valid for valid, _ in supports), sum(count for _, count in supports)
    if value is None:
        return True, 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # A bare number cannot prove which frequency, duration or amount it means.
        # Numeric values are accepted only inside a verified number/unit pair above.
        return False, 0
    text = json.dumps(value) if isinstance(value, bool) else value
    valid = (
        isinstance(text, str)
        and bool(text.strip())
        and _verbatim_detail(text.strip(), evidence_text)
        and not _LIFESTYLE_CAUSAL_LANGUAGE.search(text)
    )
    return bool(valid), int(bool(valid))


def _verbatim_detail(text: str, evidence_text: str) -> bool:
    """String quantities need the same full-token boundary as numeric values."""
    for literal in re.finditer(re.escape(text), evidence_text):
        for number in _SOURCE_NUMBER.finditer(text):
            source_number = _SOURCE_NUMBER.match(evidence_text, literal.start() + number.start())
            if source_number is None or source_number.group() != number.group():
                break
            unit_start = source_number.end()
            while unit_start < len(evidence_text) and evidence_text[unit_start].isspace():
                unit_start += 1
            source_unit = _SOURCE_UNIT.match(evidence_text, unit_start)
            if source_unit and source_unit.end() > literal.end():
                break
        else:
            return True
    return False


def _number_is_supported(number: int | float, evidence_text: str, *, unit: str | None = None) -> bool:
    """Compare complete source numbers, keeping units attached to their own amount."""
    try:
        expected = Decimal(str(number))
    except InvalidOperation:
        return False
    if not expected.is_finite() or (unit is not None and not unit.strip()):
        return False
    for match in _SOURCE_NUMBER.finditer(evidence_text):
        token = match.group()
        if not _VALID_NUMBER.fullmatch(token):
            continue
        source_number = Decimal(token.replace(',', '').replace('−', '-'))
        if source_number != expected:
            continue
        source_unit = _SOURCE_UNIT.match(evidence_text[match.end() :].lstrip())
        if unit is None or (source_unit and source_unit.group() == unit):
            return True
    return False


def _gender_value(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if normalized in {'女', '女性', 'female', 'f'}:
        return 'female'
    if normalized in {'男', '男性', 'male', 'm'}:
        return 'male'
    return None


def _quantity(
    value: Any,
    *,
    unit: str,
    low: float,
    high: float,
) -> dict[str, Any] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        match = re.search(r'-?\d+(?:\.\d+)?', value)
        if match is None:
            return None
        number = float(match.group())
    else:
        return None
    if not low <= number <= high:
        return None
    return {'number': number, 'unit': unit}


def _user_input_name(session: dict[str, Any]) -> str | None:
    sources = session.get('extractedInfoSources')
    values = session.get('extractedInfo')
    if not isinstance(sources, dict) or not isinstance(values, dict):
        return None
    if sources.get('name') != 'user_input':
        return None
    value = values.get('name')
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:128]


def _mentions_other_subject(question: str) -> bool:
    return any(term in question for term in _OTHER_SUBJECT_TERMS)


def _source_contains_evidence(value: Any, evidence_text: str) -> bool:
    if not evidence_text:
        return False
    if isinstance(value, str):
        return evidence_text in value
    try:
        content = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        content = str(value)
    return evidence_text in content


def _subject_evidence_is_safe(
    *,
    subject_kind: SubjectKind,
    subject_hint: str | None,
    evidence_text: str,
    question: str,
) -> bool:
    if subject_kind is SubjectKind.SELF:
        if _mentions_other_subject(evidence_text):
            return False
        # “有/没有”等短回答必须结合问题判断主体；开放式问题中的
        # 本人证据则不会被同一回答的其他亲属片段污染。
        if _mentions_other_subject(question) and not _mentions_self(
            evidence_text,
        ):
            return False
        return True
    if subject_kind is SubjectKind.UNKNOWN:
        return True
    context = f'{question}\n{evidence_text}'
    return _subject_hint_is_mentioned(subject_hint, context)


def _mentions_self(content: str) -> bool:
    return any(term in content for term in ('我', '本人', '自己'))


def _subject_hint_is_mentioned(
    subject_hint: str | None,
    content: str,
) -> bool:
    hint = _normalized_subject_hint(subject_hint)
    if not hint:
        return False
    normalized_content = re.sub(r'[\s_-]+', '', content.strip().casefold())
    if hint in normalized_content:
        return True
    relationship = _stable_relationship(subject_hint)
    if relationship is None:
        return False
    return any(
        alias in normalized_content for alias, candidate in _STABLE_RELATIONSHIPS.items() if candidate == relationship
    )


def _subject_slot(
    subject_kind: SubjectKind,
    subject_hint: str | None,
) -> str:
    if subject_kind is SubjectKind.SELF:
        return SubjectKind.SELF.value
    return f'{subject_kind.value}:{_normalized_subject_hint(subject_hint)}'


def _normalized_subject_hint(value: str | None) -> str:
    if not isinstance(value, str):
        return ''
    return re.sub(r'[\s_-]+', '', value.strip().casefold())


def _stable_relationship(value: str | None) -> _StableRelationship | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r'[\s_-]+', '', value.strip().casefold())
    return _STABLE_RELATIONSHIPS.get(key)


def _subject_resolution_request_id(
    user_id: str,
    relationship: _StableRelationship,
) -> str:
    identity = f'{user_id}:{relationship.kind.value}:{relationship.relation_type}'
    digest = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]
    return f'profile-subject-resolve:{digest}'


def _session_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith('Z'):
        normalized = f'{normalized[:-1]}+00:00'
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=_CHINA_TIMEZONE)
    return parsed


def _candidate_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256(':'.join(parts).encode('utf-8')).hexdigest()[:24]
    return f'{prefix}_{digest}'


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
