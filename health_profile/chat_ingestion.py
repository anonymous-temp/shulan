"""单条普通聊天用户消息到受控健康事实的摄取。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from datetime import date
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
from agents.graphs.health_profile import PROFILE_CHAT_EXTRACTION_CAPABILITY
from utils.log import getLogger

from .candidate_identity import PreparedCandidateIdentity
from .candidate_identity import finalize_candidate_identities
from .chat_source import ChatProfileSourceRecord
from .chat_source import ChatProfileStore
from .fact_writer import HealthProfileFactWriter
from .ingestion import FactTypeRegistry
from .ingestion import HealthFactPolicy
from .ingestion import PolicyDisposition
from .models import AssertionLevel
from .models import EffectivePeriod
from .models import FactSource
from .models import FactValueType
from .models import HealthProfileJob
from .models import MemoryCandidate
from .models import ProfileIngestionSourceType
from .models import SubjectKind
from .processing import ProfileIngestionProcessingError
from .processing import ProfileIngestionProcessingResult
from .record_ingestion import CandidateSubjectResolver
from .record_ingestion import SubjectProfileService
from .repository import HealthProfileRepository

logger = getLogger(__name__)

_MAX_SOURCE_CHARACTERS = 32 * 1024
_MAX_EXTRACTED_CANDIDATES = 32
_OTHER_SUBJECT_TERMS = (
    '家族', '家人', '亲属', '父亲', '爸爸', '母亲', '妈妈', '父母',
    '儿子', '女儿', '孩子', '兄弟', '姐妹', '哥哥', '弟弟', '姐姐',
    '妹妹', '祖父', '祖母', '爷爷', '奶奶', '外公', '外婆', '配偶',
    '丈夫', '妻子', '老公', '老婆', '朋友',
)


class _ExtractedChatCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')

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


class _ChatExtractionEnvelope(BaseModel):
    model_config = ConfigDict(extra='forbid')

    candidates: list[_ExtractedChatCandidate] = Field(
        max_length=_MAX_EXTRACTED_CANDIDATES,
    )


class ChatMessageCandidateExtractor:
    """通过无状态 Agent 从单条用户消息提取严格候选。"""

    VERSION = 'chat-message-candidate-extractor-v3'

    def __init__(
            self,
            agent_hub: AgentHub,
            *,
            registry: FactTypeRegistry | None = None,
    ) -> None:
        if not isinstance(agent_hub, AgentHub):
            raise TypeError('agent_hub must be an AgentHub')
        self._agent_hub = agent_hub
        self._registry = registry or FactTypeRegistry.default()
        self._allowed_fact_types = tuple(
            definition.fact_type
            for definition in self._registry.list_definitions()
            if ProfileIngestionSourceType.CHAT
            in definition.permitted_source_types
        )

    async def extract(
            self,
            record: ChatProfileSourceRecord,
            *,
            self_subject_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        content = record.content[:_MAX_SOURCE_CHARACTERS]
        request_content = json.dumps({
            'allowedFactTypes': self._allowed_fact_types,
            'message': {
                'messageId': record.message_id,
                'content': content,
                'timestamp': record.created_at.isoformat(),
            },
        }, ensure_ascii=False, separators=(',', ':'))
        try:
            result = await self._agent_hub.invoke(
                PROFILE_CHAT_EXTRACTION_CAPABILITY,
                AgentRequest({'source_content': request_content}),
                AgentRunContext.create(
                    user_id=record.user_id,
                    session_id=record.session_id,
                    request_id=f'profile-extract:{record.message_id}',
                ),
            )
        except Exception as error:
            logger.warning(
                '聊天画像候选提取 Agent 执行失败: '
                f'session_id={record.session_id}, '
                f'message_id={record.message_id}, '
                f'error_type={type(error).__name__}',
            )
            raise ProfileIngestionProcessingError(
                'extractor.agent_failure',
                '聊天候选提取 Agent 执行失败',
            ) from error
        output = result.data.get('content')
        if not isinstance(output, str):
            raise ProfileIngestionProcessingError(
                'extractor.invalid_output',
                '聊天候选提取 Agent 未返回文本',
            )
        try:
            extracted = _parse_extraction(output).candidates
        except (TypeError, ValueError, ValidationError) as error:
            logger.warning(
                '聊天画像候选提取结果无法解析: '
                f'session_id={record.session_id}, '
                f'message_id={record.message_id}, '
                f'error_type={type(error).__name__}',
            )
            raise ProfileIngestionProcessingError(
                'extractor.invalid_output',
                '聊天候选提取 Agent 返回格式不合法',
            ) from error

        prepared_candidates = []
        for item in extracted:
            subject_slot = _subject_slot(
                item.subject_kind,
                item.subject_hint,
            )
            slot = (item.fact_type, subject_slot)
            if self._registry.get(item.fact_type) is None:
                raise ProfileIngestionProcessingError(
                    'extractor.unknown_fact_type',
                    '候选使用了未注册的事实类型',
                )
            evidence_text = item.evidence_text.strip()
            if not _source_contains_evidence(content, evidence_text):
                # 模型必须给出能够在用户原文中逐字定位的证据，不能用改写
                # 或推断内容建立画像候选。
                logger.warning(
                    '聊天画像候选已跳过: '
                    f'message_id={record.message_id}, '
                    f'fact_type={item.fact_type}, '
                    f'subject_kind={item.subject_kind.value}, '
                    'reason_code=evidence_not_verbatim',
                )
                continue
            if not _subject_evidence_is_safe(
                    subject_kind=item.subject_kind,
                    subject_hint=item.subject_hint,
                    evidence_text=evidence_text,
            ):
                # 主体安全校验只检查当前候选的最小证据片段。这样混合消息
                # 中的本人事实不会再因其他片段提到家人而被误删。
                logger.warning(
                    '聊天画像候选已跳过: '
                    f'message_id={record.message_id}, '
                    f'fact_type={item.fact_type}, '
                    f'subject_kind={item.subject_kind.value}, '
                    'reason_code=subject_evidence_unsafe',
                )
                continue
            try:
                value = _normalized_model_value(item.value_type, item.value)
            except (TypeError, ValueError) as error:
                logger.warning(
                    '聊天画像候选值无法规范化: '
                    f'message_id={record.message_id}, '
                    f'fact_type={item.fact_type}, '
                    f'value_type={item.value_type.value}, '
                    f'error_type={type(error).__name__}',
                )
                raise ProfileIngestionProcessingError(
                    'extractor.invalid_candidate_value',
                    '聊天候选值与声明类型不一致',
                ) from error
            pointer = f'/messages/{record.message_id}/content'
            value_fingerprint = _sha256_json({
                'valueType': item.value_type.value,
                'value': value,
            })
            legacy_candidate_id = _candidate_id(
                record.message_id,
                item.fact_type,
                subject_slot,
            )
            prepared_candidates.append(PreparedCandidateIdentity(
                candidate=MemoryCandidate(
                    candidate_id=_candidate_id(
                        record.message_id,
                        item.fact_type,
                        subject_slot,
                        value_fingerprint,
                    ),
                    subject_id=(
                        self_subject_id
                        if item.subject_kind is SubjectKind.SELF
                        else None
                    ),
                    subject_hint=item.subject_hint,
                    subject_kind_hint=item.subject_kind,
                    fact_type=item.fact_type,
                    display_name=item.display_name.strip(),
                    value_type=item.value_type,
                    value=value,
                    assertion_level=AssertionLevel.USER_REPORTED,
                    confidence=min(item.confidence, 0.8),
                    effective_period=EffectivePeriod(start=record.created_at),
                    source=FactSource(
                        source_type=ProfileIngestionSourceType.CHAT.value,
                        source_id=record.message_id,
                        source_revision=record.source_revision,
                        pointer=pointer,
                        observed_at=record.created_at,
                        excerpt_hash=_sha256_json(record.content),
                    ),
                    evidence_text=evidence_text,
                ),
                logical_slot=(item.fact_type, subject_slot),
                value_fingerprint=value_fingerprint,
                legacy_candidate_id=legacy_candidate_id,
            ))
        identity_result = finalize_candidate_identities(prepared_candidates)
        if identity_result.deduplicated_count:
            logger.warning(
                '聊天画像重复候选已去重: '
                f'message_id={record.message_id}, '
                f'deduplicated_count={identity_result.deduplicated_count}, '
                'reason_code=duplicate_candidate_value',
            )
        return identity_result.candidates


class ChatProfileProcessor:
    """读取单条用户消息并把受控候选写入对应主体画像。"""

    GRAPH_VERSION = 'profile-ingest-chat-v2'

    def __init__(
            self,
            *,
            store: ChatProfileStore,
            repository: HealthProfileRepository,
            profile_service: SubjectProfileService,
            agent_hub: AgentHub,
            extractor: ChatMessageCandidateExtractor | None = None,
            subject_resolver: CandidateSubjectResolver | None = None,
            policy: HealthFactPolicy | None = None,
            clock: Callable[[], datetime] | None = None,
            id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if not isinstance(store, ChatProfileStore):
            raise TypeError('store must implement ChatProfileStore')
        if not isinstance(repository, HealthProfileRepository):
            raise TypeError(
                'repository must implement HealthProfileRepository',
            )
        if not isinstance(profile_service, SubjectProfileService):
            raise TypeError(
                'profile_service must implement SubjectProfileService',
            )
        self._store = store
        self._repository = repository
        self._profile_service = profile_service
        self._extractor = extractor or ChatMessageCandidateExtractor(agent_hub)
        self._subject_resolver = subject_resolver or CandidateSubjectResolver(
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
        if job.source_type is not ProfileIngestionSourceType.CHAT:
            raise ProfileIngestionProcessingError(
                'source.unsupported',
                '当前处理器只支持普通聊天用户消息任务',
            )
        record = await self._store.get_user_message(
            user_id=job.user_id,
            message_id=job.source_id,
        )
        if record is None:
            raise ProfileIngestionProcessingError(
                'source.not_found',
                '找不到摄取任务引用的用户消息快照',
            )
        if (
                record.user_id != job.user_id
                or record.message_id != job.source_id
                or record.source_revision != job.source_revision
        ):
            raise ProfileIngestionProcessingError(
                'source.mismatch',
                '用户消息快照与摄取任务不一致',
            )
        await self._repository.ensure_indexes()
        bootstrap = await self._profile_service.ensure_self_profile(
            job.user_id,
        )
        self_subject = bootstrap.subject
        if (
                job.subject_id is not None
                and job.subject_id != self_subject.subject_id
        ):
            raise ProfileIngestionProcessingError(
                'subject.mismatch',
                '聊天任务预设主体与用户本人主体不一致',
            )
        candidates = await self._extractor.extract(
            record,
            self_subject_id=self_subject.subject_id,
        )
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
                    '聊天画像候选未写入: '
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
                    '主体未解析的聊天候选不能写入用户画像',
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
        return ProfileIngestionProcessingResult(
            graph_version=self.GRAPH_VERSION,
            policy_version=self._policy.VERSION,
            extractor_version=(
                f'{self._extractor.VERSION}+'
                f'{self._subject_resolver.VERSION}'
            ),
            candidate_count=len(candidates),
            fact_count=facts,
            replayed_fact_count=replayed,
            rejected_candidate_count=rejected,
        )


def _parse_extraction(content: str) -> _ChatExtractionEnvelope:
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```[a-zA-Z]*\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    end = text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('missing JSON object')
    return _ChatExtractionEnvelope.model_validate_json(text[start:end + 1])


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
    raise ValueError(f'unsupported chat candidate value type: {value_type}')


def _mentions_other_subject(content: str) -> bool:
    return any(term in content for term in _OTHER_SUBJECT_TERMS)


def _source_contains_evidence(source: str, evidence_text: str) -> bool:
    return bool(evidence_text) and evidence_text in source


def _subject_evidence_is_safe(
        *,
        subject_kind: SubjectKind,
        subject_hint: str | None,
        evidence_text: str,
) -> bool:
    if subject_kind is SubjectKind.SELF:
        return not _mentions_other_subject(evidence_text)
    if subject_kind is SubjectKind.UNKNOWN:
        return True
    hint = _normalized_subject_hint(subject_hint)
    if not hint:
        return False
    normalized_evidence = re.sub(r'\s+', '', evidence_text).casefold()
    return hint in normalized_evidence


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


def _candidate_id(
        message_id: str,
        fact_type: str,
        subject_slot: str,
        value_fingerprint: str | None = None,
) -> str:
    parts = [message_id, fact_type, subject_slot]
    if value_fingerprint is not None:
        parts.append(value_fingerprint)
    digest = hashlib.sha256(
        ':'.join(parts).encode('utf-8'),
    ).hexdigest()[:24]
    return f'chat_message_{digest}'


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
