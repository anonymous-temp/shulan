"""有界的画像重点规划、循证解释与本地引用校验。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from datetime import timezone
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from utils.log import getLogger

from ...knowledge import KnowledgeDocument
from ...knowledge import KnowledgeQuery
from ...knowledge import KnowledgeSource
from ...llm.protocol import LLMGateway
from ...llm.types import LLMRequest
from .focus import POLICY_VERSION
from .focus import association_anchor
from .focus import balanced_topics
from .focus import clinical_anchor
from .focus import default_topics
from .focus import evidence_anchor
from .focus import fact_focus
from .focus import fact_rank
from .focus import json_object

MAX_INPUT_CHARS = 120_000
MAX_DOCUMENT_CHARS = 4000
MAX_EVIDENCE_ITEMS = 12
MAX_QUERIES = 8
logger = getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


class PlannedTopic(_StrictModel):
    title: str = Field(min_length=1, max_length=120)
    factIds: list[str] = Field(min_length=1, max_length=40)
    query: str = Field(min_length=1, max_length=256)
    domain: Literal['gut', 'metabolic', 'other']
    relationship: Literal['direct', 'indirect']
    rationale: str = Field(min_length=1, max_length=500)


class FocusPlan(_StrictModel):
    topics: list[PlannedTopic] = Field(max_length=6)


class EvidenceSupport(_StrictModel):
    evidenceId: str = Field(min_length=1)
    quote: str = Field(min_length=12, max_length=900)


class ProfileInsight(_StrictModel):
    topicId: str = Field(min_length=1)
    kind: Literal['risk', 'association']
    title: str = Field(min_length=1, max_length=100)
    explanation: str = Field(min_length=1, max_length=900)
    action: str = Field(max_length=500)
    followUp: str = Field(max_length=500)
    factIds: list[str] = Field(min_length=1, max_length=40)
    supports: list[EvidenceSupport] = Field(min_length=1, max_length=6)


class InterpretationCache:
    """缓存已验证的非空结果及正常空结果；最后一个等待者取消时终止生成。"""

    def __init__(self, *, ttl=600.0, capacity=256, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl
        self.capacity = capacity
        self.clock = clock
        self.entries: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self.inflight: dict[str, list] = {}

    async def get_or_create(self, key, factory):
        now = self.clock()
        for expired in [item for item, (expires, _) in self.entries.items() if expires <= now]:
            del self.entries[expired]
        if key in self.entries:
            self.entries.move_to_end(key)
            return deepcopy(self.entries[key][1])
        flight = self.inflight.get(key)
        if flight is None:

            async def generate():
                result = await factory()
                if result['complete'] or (
                    result.get('evidence', {}).get('status') == 'ready' and result['evidence'].get('items')
                ):
                    self.entries[key] = (self.clock() + self.ttl, deepcopy(result))
                    self.entries.move_to_end(key)
                    while len(self.entries) > self.capacity:
                        self.entries.popitem(last=False)
                return result

            flight = [asyncio.create_task(generate()), 0]
            self.inflight[key] = flight
        flight[1] += 1
        try:
            return deepcopy(await asyncio.shield(flight[0]))
        finally:
            flight[1] -= 1
            if flight[1] == 0:
                self.inflight.pop(key, None)
                if not flight[0].done():
                    flight[0].cancel()
                    await asyncio.gather(flight[0], return_exceptions=True)


class ProfileInterpreter:
    """每个已注册 Graph 一个实例，所有患者共享有界缓存及检索并发限制。"""

    def __init__(
        self,
        gateway,
        *,
        llm_gateway=None,
        planner_profile=None,
        synthesis_profile=None,
        timeout_seconds=60.0,
        cache=None,
    ):
        self.gateway = gateway
        self.llm = llm_gateway
        self.planner = planner_profile
        self.synthesizer = synthesis_profile
        self.timeout = timeout_seconds
        self.cache = cache or InterpretationCache()
        self.retrieval_slots = asyncio.Semaphore(3)

    async def run(self, facts, *, user_id, subject_id, profile_version, clinical_context):
        signature = [
            'profile-evidence-v2',
            POLICY_VERSION,
            user_id,
            subject_id,
            profile_version,
            facts,
            clinical_context,
            [
                (profile.name, profile.model, dict(profile.model_kwargs), profile.prompt_name)
                for profile in (self.planner, self.synthesizer)
                if profile is not None
            ],
            # 默认模型由此网关实例固定；不跨网关共享缓存。
            id(self.llm),
            self.gateway.enabled,
        ]
        key = hashlib.sha256(
            json.dumps(signature, sort_keys=True, ensure_ascii=False, default=str).encode()
        ).hexdigest()
        started = time.monotonic()
        result = await self.cache.get_or_create(key, lambda: self._generate(facts, clinical_context))
        logger.info(
            'Profile interpretation status=%s complete=%s facts=%d insights=%d elapsed_ms=%d',
            result['evidence']['status'],
            result['complete'],
            len(facts),
            len(result['insights']),
            (time.monotonic() - started) * 1000,
        )
        return result

    async def _model(self, profile, payload, deadline, stage_limit):
        if not isinstance(self.llm, LLMGateway) or profile is None:
            raise ValueError('profile model unavailable')
        content = json.dumps(payload, ensure_ascii=False)
        if len(content) > MAX_INPUT_CHARS:
            raise ValueError('profile model input exceeds budget')
        remaining = min(stage_limit, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('profile interpretation deadline')
        async with asyncio.timeout(remaining):
            result = await self.llm.complete(LLMRequest(profile=profile, content=content, thinking=False))
        return json_object(result.content)

    async def _generate(self, facts, clinical_context):
        deadline = time.monotonic() + self.timeout
        topics = default_topics(facts)
        if not topics or not self.gateway.enabled:
            return self._result('empty' if not topics else 'unavailable', [], [], not topics)
        planning_ok = False
        try:
            planned = await self._model(
                self.planner, {'facts': facts, 'clinicalContext': clinical_context}, deadline, 15
            )
            topics, planning_ok = _planned_topics(FocusPlan.model_validate(planned), facts, topics)
        except Exception as error:
            # 所有失败都只影响解释，不记录病例正文或模型输出。
            logger.warning('Profile focus planning fallback category=%s', type(error).__name__)
        documents, successful, failures = await self._retrieve(topics, deadline, facts)
        if not documents:
            return self._result(
                'empty' if successful else 'unavailable', [], [], planning_ok and successful > 0 and failures == 0
            )
        model_documents = [
            document_payload(key, value) for key, value in documents.items() if source_passages(value['document'])
        ]
        if not model_documents:
            return self._result('empty', [], [], planning_ok and failures == 0, documents)
        insights = []
        synthesis_ok = False
        try:
            output = await self._model(
                self.synthesizer,
                {
                    'facts': facts,
                    'clinicalContext': clinical_context,
                    'topics': [{key: value for key, value in topic.items() if key != 'rank'} for topic in topics],
                    'documents': model_documents,
                },
                deadline,
                25,
            )
            insights, synthesis_ok = validate_insights(output, facts, topics, documents)
        except Exception as error:
            logger.warning('Profile synthesis fallback category=%s', type(error).__name__)
        if not insights:
            # 正常判定无相关解释可复用；校验失败或技术故障产生的空结果仍可重试。
            return self._result('empty', [], [], planning_ok and synthesis_ok and failures == 0, documents)
        # 条目只保留实际支撑已验证解释的资料，结论一律为基于事实与原文的解释。
        items = evidence_items(documents, insights, facts)
        if not items:
            return self._result('empty', [], [], False, documents)
        return self._result('ready', items, insights, planning_ok and synthesis_ok and failures == 0, documents)

    async def _retrieve(self, topics, deadline, facts):
        by_id = {fact['factId']: fact for fact in facts}
        association_topics = {
            topic['topicId']
            for topic in topics
            if any(association_anchor(by_id[fact_id]) for fact_id in topic['factIds'])
        }
        retrieval_deadline = min(deadline, time.monotonic() + 20)
        results: list[tuple[dict, tuple]] = []
        successful = 0
        failures = 0

        async def retrieve(topic, source):
            nonlocal successful, failures
            try:
                async with self.retrieval_slots:
                    docs = await self.gateway.retrieve(
                        KnowledgeQuery(
                            source=source,
                            query=topic['query'],
                            count=3,
                            include_source_text=True,
                        )
                    )
                results.append((topic, tuple(doc for doc in docs[:3] if isinstance(doc, KnowledgeDocument))))
                successful += 1
            except Exception:
                failures += 1

        async def batch(requests):
            nonlocal failures
            if not requests:
                return
            tasks = [asyncio.create_task(retrieve(topic, source)) for topic, source in requests]
            try:
                _, pending = await asyncio.wait(tasks, timeout=max(0, retrieval_deadline - time.monotonic()))
                failures += len(pending)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        await batch([(topic, KnowledgeSource.GUIDE) for topic in topics])
        found = {topic['topicId'] for topic, docs in results if docs}
        remaining = MAX_QUERIES - len(topics)
        association = balanced_topics(
            [topic for topic in topics if topic['topicId'] in association_topics],
            limit=remaining,
        )
        missing = balanced_topics(
            [topic for topic in topics if topic['topicId'] not in found and topic['topicId'] not in association_topics],
            limit=remaining - len(association),
        )
        if time.monotonic() < retrieval_deadline:
            await batch([(topic, KnowledgeSource.LITERATURE) for topic in [*association, *missing]])
        # 先合并所有来源映射，再按主题轮转截取，防止早停丢掉共享证据的 factIds。
        pool: dict[str, dict] = {}
        per_topic: dict[str, list[str]] = {topic['topicId']: [] for topic in topics}
        for topic in topics:
            topic_results = [docs for result_topic, docs in results if result_topic['topicId'] == topic['topicId']]
            if topic['topicId'] in association_topics:
                topic_results.sort(key=lambda docs: 0 if docs and docs[0].source is KnowledgeSource.LITERATURE else 1)
            for docs in topic_results:
                for doc in docs:
                    key = document_id(doc)
                    entry = pool.setdefault(key, {'document': doc, 'factIds': [], 'topicIds': []})
                    if entry['document'] is not doc:
                        merged = dict(entry['document'].metadata)
                        passages = list(_source_text_list(merged))
                        for passage in _source_text_list(doc.metadata):
                            if passage not in passages:
                                passages.append(passage)
                        merged['sourceTexts'] = passages
                        entry['document'] = replace(entry['document'], metadata=merged)
                    entry['factIds'] = list(dict.fromkeys([*entry['factIds'], *topic['factIds']]))
                    entry['topicIds'] = list(dict.fromkeys([*entry['topicIds'], topic['topicId']]))
                    if key not in per_topic[topic['topicId']]:
                        per_topic[topic['topicId']].append(key)
        selected = []
        for index in range(MAX_EVIDENCE_ITEMS):
            for keys in per_topic.values():
                if index < len(keys) and keys[index] not in selected:
                    selected.append(keys[index])
        return {key: pool[key] for key in selected[:MAX_EVIDENCE_ITEMS]}, successful, failures

    @staticmethod
    def _result(status, items, insights, complete, documents=None):
        revisions = {
            str(entry['document'].metadata['knowledgeBaseRevision'])
            for entry in (documents or {}).values()
            if entry['document'].metadata.get('knowledgeBaseRevision')
        }
        generated_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        return {
            'evidence': {
                'status': status,
                'items': items,
                'generatedAt': generated_at,
                'knowledgeBaseRevision': next(iter(revisions)) if len(revisions) == 1 else None,
            },
            'insights': insights,
            'complete': complete,
        }


def _planned_topics(plan, facts, defaults):
    by_id = {fact['factId']: fact for fact in facts}
    topics = []
    covered = set()
    for item in plan.topics:
        ids = list(dict.fromkeys(item.factIds))
        if any(key not in by_id for key in ids):
            continue
        selected = [by_id[key] for key in ids]
        if not any(evidence_anchor(fact) for fact in selected):
            continue
        # 不允许查询包含身份、URL 或没有任何事实名称的脱离事实查询。
        if (
            re.search(r'https?://|@|\d{7,}|[\r\n]', item.query)
            or not any(fact['displayName'] in item.query for fact in selected)
            or any(key in item.query for key in by_id)
        ):
            continue
        direct_domains = {fact_focus(fact)[1] for fact in selected}
        indirect = item.domain not in direct_domains
        if indirect and item.relationship != 'indirect':
            continue
        if indirect and sum(clinical_anchor(fact) for fact in selected) < 2:
            # 孤立外围问题的远端研究关联不能把整个画像转向一个新专科。
            continue
        identity = '\n'.join(sorted(ids)) + '\n' + item.domain
        rank = min(fact_rank(fact) for fact in selected)
        if indirect:
            rank = (max(3, rank[0]), *rank[1:])
        topics.append(
            {
                'topicId': 'focus_' + hashlib.sha256(identity.encode()).hexdigest()[:16],
                'title': item.title,
                'domain': item.domain,
                'query': item.query,
                'factIds': ids,
                'rank': rank,
                'relationship': 'indirect' if indirect else 'direct',
                'rationale': item.rationale,
            }
        )
        covered.update(ids)
    valid_plan = len(topics) == len(plan.topics)
    for topic in defaults:
        missing_domain = topic['domain'] in {'gut', 'metabolic'} and not any(
            candidate['domain'] == topic['domain'] for candidate in topics
        )
        if not set(topic['factIds']).issubset(covered) or missing_domain:
            topics.append(topic)
    return balanced_topics(topics) or defaults, valid_plan


def document_id(document):
    identity = [
        document.source.value,
        str(document.metadata.get('id') or document.metadata.get('documentId') or ''),
        document.title,
        document.url or '',
    ]
    return 'evidence_' + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def source_passages(document):
    passages = []
    for raw in _source_text_list(document.metadata):
        if (
            isinstance(raw, dict)
            and isinstance(raw.get('kind'), str)
            and raw['kind'] in {'abstract', 'fullText', 'guideBlock'}
        ):
            text = raw.get('text')
            if isinstance(text, str) and text.strip():
                passages.append({'kind': raw['kind'], 'text': text.strip()[:MAX_DOCUMENT_CHARS]})
    priority = {'guideBlock': 0, 'abstract': 1, 'fullText': 2}
    return sorted(passages, key=lambda passage: priority[passage['kind']])[:3]


def _source_text_list(metadata):
    value = metadata.get('sourceTexts', [])
    return value if isinstance(value, list) else []


def document_payload(key, entry):
    doc = entry['document']
    return {
        'evidenceId': key,
        'title': doc.title,
        'factIds': entry['factIds'],
        'topicIds': entry['topicIds'],
        'sourceTexts': source_passages(doc),
    }


def evidence_items(documents, insights, facts):
    """只构建支撑已验证解释的条目；结论永远是基于事实与原文的解释态。"""
    fact_by_id = {fact['factId']: fact for fact in facts}
    conclusions: dict[str, list[str]] = {}
    fact_ids: dict[str, list[str]] = {}
    for insight in insights:
        for support in insight['supports']:
            key = support['evidenceId']
            conclusions.setdefault(key, []).append(insight['explanation'])
            bucket = fact_ids.setdefault(key, [])
            for fact_id in insight['factIds']:
                if fact_id not in bucket:
                    bucket.append(fact_id)
    items = []
    for key, entry in documents.items():
        if key not in conclusions:
            continue
        doc = entry['document']
        metadata = doc.metadata
        related = sorted(
            fact_ids.get(key, []),
            key=lambda fact_id: fact_rank(fact_by_id[fact_id]),
        )
        items.append(
            {
                'evidenceId': key,
                'title': doc.title,
                'conclusion': '\n'.join(dict.fromkeys(conclusions[key])),
                'evidenceGrade': 'ungraded',
                'relevanceGrade': 'retrieved',
                'factIds': related,
                'citations': [
                    {
                        'citationId': str(
                            metadata.get('id') or metadata.get('documentId') or key.removeprefix('evidence_')
                        ),
                        'title': doc.title,
                        'url': doc.url,
                        'source': doc.source.value,
                        'publisher': _optional_text(
                            metadata.get('publisher') or metadata.get('journal') or metadata.get('source')
                        ),
                        'publishedAt': _optional_text(
                            metadata.get('publicationDate') or metadata.get('publishedAt') or metadata.get('year')
                        ),
                    }
                ],
            }
        )
    return items[:MAX_EVIDENCE_ITEMS]


def _optional_text(value):
    return None if value is None else str(value)


def _remove_reference_markers(text, marker):
    """只整理紧邻已移除引用的分隔符，不改动其他医学文字。"""
    separators = r'[\s,，;；、]*'
    pattern = re.compile(rf'(?P<before>{separators}){marker}(?P<after>(?:{separators}{marker})*{separators})')

    def remove(match):
        left = text[: match.start()]
        right = text[match.end() :]
        if not left or not right or left[-1] in '(（[【' or right[0] in ')）]】.。!?！？':
            return ''
        before, after = match.group('before', 'after')
        punctuation = re.search(r'[,，;；、]', before + after)
        if punctuation:
            return punctuation.group()
        return ' ' if before or after else ''

    return pattern.sub(remove, text).strip()


def _clean_explanation_text(text, internal_ids):
    """内部 ID 留在绑定字段；正文只移除标识及其引用外壳。"""
    identities = [re.escape(identity) for identity in sorted(internal_ids, key=len, reverse=True) if identity]
    tokens = '(?:' + '|'.join([*identities, r'(?:fact|evidence)_[A-Za-z0-9_-]+']) + ')'
    marker = '\x00'
    cleaned = re.sub(
        rf'(?<![A-Za-z0-9_])(?<![A-Za-z0-9_][.-]){tokens}(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])',
        marker,
        text,
    )
    if cleaned == text:
        return text
    label = (
        r'(?i:[\x60"“‘\']?(?:factIds?|evidenceIds?|topicIds?|'
        r'(?:事实|证据|主题)(?:\s*ID|编号)?|引用|ID)[\x60"”’\']?)\s*[:：=]\s*'
    )
    quoted_marker = rf'[\x60"“‘\']\s*{marker}\s*[\x60"”’\']'
    brackets = re.compile(
        r'\([^()（）\[\]【】]*\)|（[^()（）\[\]【】]*）|'
        r'\[[^()（）\[\]【】]*\]|【[^()（）\[\]【】]*】'
    )

    def clean_bracket(match):
        bracket = match.group()
        if marker not in bracket:
            return bracket
        body = _remove_reference_markers(bracket[1:-1], marker)
        return bracket[0] + body + bracket[-1] if body else marker

    while True:
        previous = cleaned
        cleaned = re.sub(quoted_marker, marker, cleaned)
        cleaned = re.sub(label + marker, marker, cleaned)
        cleaned = brackets.sub(clean_bracket, cleaned)
        if cleaned == previous:
            break
    return _remove_reference_markers(cleaned, marker)


def validate_insights(output, facts, topics, documents):
    if set(output) != {'insights'} or not isinstance(output['insights'], list) or len(output['insights']) > 6:
        return [], False
    by_id = {fact['factId']: fact for fact in facts}
    by_topic = {topic['topicId']: topic for topic in topics}
    internal_ids = {*by_id, *by_topic, *documents}
    accepted = []
    seen = set()
    for raw in output['insights']:
        try:
            insight = ProfileInsight.model_validate(raw)
            topic = by_topic[insight.topicId]
            ids = set(insight.factIds)
            if not ids.issubset(topic['factIds']) or insight.topicId in seen:
                continue
            selected = [by_id[key] for key in ids]
            if insight.kind == 'risk' and not any(clinical_anchor(fact) for fact in selected):
                continue
            if insight.kind == 'association' and not any(association_anchor(fact) for fact in selected):
                continue
            cleaned_explanation = _clean_explanation_text(insight.explanation, internal_ids)
            if not cleaned_explanation:
                continue
            text = '\n'.join((insight.title, cleaned_explanation, insight.action, insight.followUp))
            if re.search(r'已确诊|诊断为|患有|患病概率|\d+(?:\.\d+)?\s*%|\d+\s*(?:mg|毫克|片/天)', text):
                continue
            explanation = re.sub(r'\s+', '', cleaned_explanation).casefold()
            if any(
                phrase in explanation
                for phrase in (
                    '检索资料摘要',
                    '可作为健康背景',
                    '不能单独据此判断疾病风险',
                    '需要结合现有事实进一步评估',
                )
            ):
                continue
            if not any(re.sub(r'\s+', '', fact['displayName']).casefold() in explanation for fact in selected):
                # conclusion 必须明确指出它解释的是哪项当前事实。
                continue
            supported_ids = set()
            for support in insight.supports:
                entry = documents[support.evidenceId]
                if insight.topicId not in entry['topicIds']:
                    raise ValueError('citation belongs to a different topic')
                passages = source_passages(entry['document'])
                quote = re.sub(r'\s+', '', support.quote)
                if len(quote) < 12 or not any(quote in re.sub(r'\s+', '', part['text']) for part in passages):
                    raise ValueError('citation does not quote supplied source text')
                supported_ids.update(entry['factIds'])
            if not ids.issubset(supported_ids):
                continue
            result = insight.model_dump()
            result['explanation'] = cleaned_explanation
            result['factIds'] = sorted(ids, key=lambda key: fact_rank(by_id[key]))
            if insight.kind == 'association':
                result['action'] = ''
                result['followUp'] = ''
            result['rank'] = topic['rank']
            accepted.append(result)
            seen.add(insight.topicId)
        except (ValueError, TypeError, KeyError):
            continue
    accepted.sort(key=lambda item: (item['rank'], item['topicId']))
    return accepted, len(accepted) == len(output['insights'])
