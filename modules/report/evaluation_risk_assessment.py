"""Request-local, source-grounded risk decisions for evaluate.

The models below are internal working state. They never add observations to
the immutable source inventory and are not a public callback extension.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from pydantic import model_validator
from .fallback_policy import EVALUATE_MODEL_CONCURRENCY
from .evaluation_catalog import RISK_CATEGORY_DEFINITIONS

_CATEGORY_CODES = tuple(code for code, _ in RISK_CATEGORY_DEFINITIONS)


def _camel(value):
    head, *tail = value.split('_')
    return head + ''.join(part.capitalize() for part in tail)


class _RiskModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra='forbid')

    @model_validator(mode='before')
    @classmethod
    def _route_retired_category(cls, value):
        if isinstance(value, dict) and value.get('category') == 'tumor':
            from .evaluation_catalog import disease_category

            return {**value, 'category': disease_category(
                str(value.get('name') or ''),
                code=value.get('disease_id') or value.get('diseaseId'),
                category='tumor',
            )}
        return value


Category = Literal['intestinal', 'metabolic', 'autoimmune', 'aging', 'mental', 'neurological']
Certainty = Literal['confirmed', 'current_problem', 'self_reported', 'potential_risk']
EntityKind = Literal['disease', 'disease_risk', 'symptom', 'indicator_problem']
Level = Literal['正常', '注意', '中风险', '高风险']


class RiskRelation(_RiskModel):
    relation_id: str
    kind: Literal['clinical', 'behavior', 'microbiome', 'function', 'source_model']
    fact_ids: list[str] = Field(min_length=1)
    exposure_terms: list[str] = Field(default_factory=list)
    outcome_terms: list[str] = Field(default_factory=list)
    question: str


class RiskCandidate(_RiskModel):
    risk_id: str
    disease_id: str
    name: str = Field(min_length=2, max_length=160)
    category: Category
    certainty: Certainty = 'potential_risk'
    entity_kind: EntityKind | None = None
    support_fact_ids: list[str] = Field(min_length=1)
    opposing_fact_ids: list[str] = Field(default_factory=list)
    background_fact_ids: list[str] = Field(default_factory=list)
    source_model_fact_ids: list[str] = Field(default_factory=list)
    relationship: str = Field(min_length=4, max_length=1200)
    search_terms: list[str] = Field(default_factory=list)
    exposure_terms: list[str] = Field(default_factory=list)
    relations: list[RiskRelation] = Field(default_factory=list)
    priority: int = Field(default=2, ge=1, le=3)
    _role_conflicts: list[str] = PrivateAttr(default_factory=list)
    _retrieval_aliases: list[str] = PrivateAttr(default_factory=list)
    # Request-local coverage hints guide recall/authoring only. They are not
    # publishable claims, and cannot be reconstructed from serialized JSON.
    _coverage_hints: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    # A normal-range opposition proposal is neutral during discovery, but
    # its existence must survive merging with another batch's positive role.
    _reference_opposition_proposals: set[str] = PrivateAttr(default_factory=set)
    _current_classification_scope: str | None = PrivateAttr(default=None)


class RiskEvidenceAnchor(_RiskModel):
    evidence_key: str
    quote: str = Field(min_length=8)
    direction: Literal['support', 'oppose', 'mixed'] = 'support'


class RiskClaim(_RiskModel):
    claim_id: str
    text: str = Field(min_length=4, max_length=1600)
    role: Literal['source_fact', 'diagnostic_principle', 'outcome', 'etiology', 'mechanism', 'management']
    fact_quotes: dict[str, str] = Field(min_length=1)
    evidence: list[RiskEvidenceAnchor] = Field(default_factory=list)
    exposure_terms: list[list[str]] = Field(default_factory=list)
    outcome_terms: list[str] = Field(default_factory=list)
    _applicability_receipt: Any = PrivateAttr(default=None)
    _applicability_errors: tuple[str,...] = PrivateAttr(default=())
    _classification_receipt: str | None = PrivateAttr(default=None)
    _management_witness: dict[str, Any] = PrivateAttr(default_factory=dict)
    _source_scope_bindings: dict[str, Any] = PrivateAttr(default_factory=dict)


class _RiskClaimSet(_RiskModel):
    risk_id: str
    claims: list[RiskClaim]


class RiskAction(_RiskModel):
    text: str = Field(min_length=4, max_length=1600)
    target: str = Field(default='', max_length=300)
    fact_ids: list[str] = Field(default_factory=list)
    evidence_keys: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)
    _planning_proof: str | None = PrivateAttr(default=None)
    _planning_witness: dict[str, Any] = PrivateAttr(default_factory=dict)


class RiskAssessment(_RiskModel):
    risk_id: str
    disease_id: str
    name: str
    category: Category
    certainty: Certainty
    entity_kind: EntityKind | None = None
    index: int = Field(ge=0, le=100, strict=True)
    level: Level
    evidence_strength: Literal['强', '中', '弱', '有限']
    conclusion: str = Field(min_length=4, max_length=3000)
    support_fact_ids: list[str] = Field(min_length=1)
    opposing_fact_ids: list[str] = Field(default_factory=list)
    background_fact_ids: list[str] = Field(default_factory=list)
    source_model_fact_ids: list[str] = Field(default_factory=list)
    evidence_keys: list[str] = Field(default_factory=list)
    opposing_evidence_keys: list[str] = Field(default_factory=list)
    # Prose is optional: a number needs its own bound independent factor
    # review, not a newly authored clinical story.
    scoring_rationale: str = Field(default='', max_length=2400)
    claims: list[RiskClaim] = Field(min_length=1)
    lifestyle_actions: list[RiskAction] = Field(default_factory=list)
    clinical_actions: list[RiskAction] = Field(default_factory=list)
    _numeric_receipt: Any = PrivateAttr(default=None)
    _entity_kind_receipt: str | None = PrivateAttr(default=None)


@dataclass(frozen=True)
class _NumericRiskDecisionReceipt:
    fingerprint: str
    audit_json: str


@dataclass(frozen=True)
class _RiskSourceScopeBinding:
    source_text: str
    title: str
    context_json: str
    receipt: Any
    verified_conditions: tuple = ()
    original_scope_hash: str = ''

    def __deepcopy__(self, memo):
        return self  # Immutable, same-process provenance; never a DTO field.


class RiskSummary(_RiskModel):
    slot: str = ''
    text: str = Field(min_length=4, max_length=700)
    risk_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(default_factory=list)
    evidence_keys: list[str] = Field(default_factory=list)


class RiskRegistry(_RiskModel):
    version: Literal['integrated-risk-v1'] = 'integrated-risk-v1'
    source_digest: str | None = None
    subject_snapshot: dict[str, Any] = Field(default_factory=dict)
    candidates: list[RiskCandidate] = Field(default_factory=list)
    assessments: list[RiskAssessment] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    incomplete: list[str] = Field(default_factory=list)
    coverage_dispositions: list[dict[str, Any]] = Field(default_factory=list)
    approved_summaries: dict[str, RiskSummary] = Field(default_factory=dict)
    # Only the independent reviewer gateway may bind these. A model's JSON,
    # stored DTO, or true approval flag cannot reconstruct an approval.
    _proofs: dict[str, str] = PrivateAttr(default_factory=dict)
    _evidence_hashes: dict[str, str] = PrivateAttr(default_factory=dict)
    _original_scope_snapshot: dict[str, str] = PrivateAttr(default_factory=dict)
    _summary_proof: str | None = PrivateAttr(default=None)
    _management_attempts: set[str] = PrivateAttr(default_factory=set)
    _management_feedback: dict[str, list[dict[str, Any]]] = PrivateAttr(default_factory=dict)
    _current_problem_merge_attempts: set[str] = PrivateAttr(default_factory=set)
    _retained_atoms: dict[str, Any] = PrivateAttr(default_factory=dict)
    _editorial_link_receipt: Any = PrivateAttr(default=None)
    _editorial_link_attempts: set[str] = PrivateAttr(default_factory=set)


SCORING_RUBRIC = {
    'version': 'integrated-attention-v2',
    'scale': '0–100综合关注指数；不是患病概率或已经临床验证的预测模型',
    'direction': 'higher_means_more_attention',
    'bands': [{'min': 0, 'max': 24, 'level': '正常'},
              {'min': 25, 'max': 49, 'level': '注意'},
              {'min': 50, 'max': 74, 'level': '中风险'},
              {'min': 75, 'max': 100, 'level': '高风险'}],
    'factors': [
        '当前已存在的问题、异常程度、持续性和对健康的影响',
        '同主题症状、行为、病史、家族背景及检测之间的个人相关性',
        '适用医学证据对拟判断关系的支持或相反结果',
        '原报告模型作为一个输入；不乘100、不平均不同量纲、不代替综合判断',
        '区分来源关联信号、真实当前异常、本人症状与适用科学关系的信息特异性；综合关注度体现个人相关性、当前影响和持续性，不仅按来源模型原分级排序',
        '只有来源风险信号时仍须评判；同时考虑已提供的本人状态与相反资料，不把筛查关联当作当前疾病，也不把未形成病因证据的背景当作未提供',
        '来源关联信号的个人特异性有限时，综合关注等级可以低于原模型等级；这须独立审核source_signal_specificity，不以未询问或未确诊编造阴性，也不凭同一信号无新依据抬高原等级',
        '证据强度与风险程度分开，不用弱证据机械计算低风险',
        '无资料不填写0或正常；不以低关注指数宣称排除疾病',
    ],
    'withinBandJudgement': {
        'lower': '所选关注档内，已审支持限于局部或单一信号，当前影响较小，或有真实同病限制因素；不得把未询问、未确诊当阴性',
        'middle': '所选关注档内，已审关系与个人状态对应明确，已有具体表现或多个相互补充的同病依据，但未显示本档更突出的持续影响',
        'upper': '所选关注档内，已审资料显示更明显或持续的当前影响、反复症状、功能受限，或多个独立且适用的同病支持；重复来源不重复加权',
    },
    'decisionTrace': [
        '先判断当前问题与潜在风险的性质，再依据已审个人程度、持续性、生活影响和同病相关性选择关注档，最后在该档内定位；不反向凑数字理由',
        '档内位置只采用已有事实和已审关系；没有症状程度、时间或生活影响就不编造其强弱，也不视为不存在',
        '同档项目可同分，也可因实际负担和个人相关性不同而异分；不得统一默认中点、惯用42或58、按条目次序加分，亦不为图形分散人为拉开',
        '独立数字审核逐项核验档内定位所用事实与claim；这些仅供内部复核，不新增公开字段、评分说明或患者规则文案',
    ],
}


def risk_level(index: int) -> str:
    if type(index) is not int or not 0 <= index <= 100:
        raise ValueError('invalid integrated risk index')
    return ('正常', '注意', '中风险', '高风险')[min(index // 25, 3)]


def risk_priority_key(row):
    """One ordering for an integrated-risk snapshot; source rows stay intact."""
    return (-row.index, 0 if row.certainty in {'confirmed', 'current_problem'} else 1,
        row.disease_id)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, default=str).encode()).hexdigest()


def _facts(document):
    return {fact.fact_id: fact for fact in document.facts}


def _source_link_rows(document,owner_ids,*,include_members=True):
    try:
        from .evaluation_risk_source_links import source_link_view
        view=source_link_view(document)
    except ImportError:
        return []
    facts=_facts(document);rows=[];seen=set()
    for identifier in owner_ids:
        for edge in view.get(identifier,[]):
            members=[fid for fid in edge.get('memberFactIds',[edge.get('memberFactId')]) if fid in facts]
            if not members:
                continue
            key=(edge.get('checksum'),edge.get('memberPath'),tuple(sorted(members)))
            if key in seen:
                continue
            seen.add(key)
            row={'ownerFactId':identifier,'panelFactId':edge.get('panelFactId'),
                'memberFactId':members[0],'memberFactIds':members,'relationRole':edge.get('relationRole'),
                'sourceStatus':edge.get('sourceStatus')}
            if include_members:
                row['memberFacts']=[_fact_view(facts[members[0]])]
            rows.append(row)
    return rows


def _attach_source_members(document,candidate):
    owners=list(dict.fromkeys([*candidate.support_fact_ids,*candidate.background_fact_ids,
        *candidate.opposing_fact_ids,*candidate.source_model_fact_ids]))
    rows=_source_link_rows(document,owners)
    existing=set(owners)
    for row in rows:
        fid=row.get('memberFactId')
        if fid in _facts(document) and fid not in existing:
            candidate.background_fact_ids.append(fid);existing.add(fid)
        fact=_facts(document).get(fid)
        if (fact is not None and (fact.abnormal or re.search(r'偏高|偏低|较低|缺乏|超标|↑|↓',
                str(row.get('sourceStatus') or fact.status or '')))
                and not any(fid in relation.fact_ids for relation in candidate.relations)):
            # This is only a provenance-bound retrieval question. The source
            # edge does not establish a medical association or a diagnosis.
            candidate.relations.append(RiskRelation(
                relation_id=candidate.risk_id+':source-member:'+_digest(fid)[:12],
                kind='microbiome' if fact.kind=='microbe' else 'function',fact_ids=[fid],
                exposure_terms=[fact.name],outcome_terms=list(dict.fromkeys(
                    [_endpoint_name(candidate.name),*candidate.search_terms])),
                question='核查'+fact.name+'的实测偏离与'+candidate.name+'是否存在适用关系'))
    return rows


def _source_digest(document):
    return _digest(sorted((fact.model_dump(mode='json', by_alias=True)
        for fact in document.facts), key=lambda value: value['factId']))


def _subject_view(subject):
    value = subject.model_dump(mode='json', by_alias=True) if hasattr(subject, 'model_dump') else subject
    if not isinstance(value, dict):
        return {}
    result = {'age': value['age']} if 'age' in value else {}
    sex = value.get('sex', value.get('gender'))
    if sex is not None:
        result['sex'] = sex
    return result


def _fact_view(fact):
    result={'factId': fact.fact_id, 'kind': fact.kind, 'name': fact.name,
        'value': fact.value, 'unit': fact.unit, 'referenceRange': fact.reference_range,
        'status': fact.status, 'date': fact.date, 'abnormal': fact.abnormal}
    result['sourceRole']=_fact_role(fact)
    if _reference_normal_numeric(fact):
        result['referenceInterpretation'] = 'source_reference_normal_not_future_risk_exclusion'
        result['futureRiskRole'] = 'unassessed_requires_disease_specific_evidence'
    if result['sourceRole']=='source_disease_model':
        result['modeledDiseaseId']=_risk_identity(fact.name.removesuffix('（来源疾病模型指数）'))
    if fact.name in {'肠道微生态总评分','肠道总评分','肠道菌群健康度评分','菌群健康度评分'}:
        from .evaluation_schema import _source_total_score_value
        displayed=_source_total_score_value(fact.value)
        if displayed!=fact.value:
            result['displayValue']=displayed
    elif fact.kind=='self_reported':
        displayed=_questionnaire_state_display(fact)
        if displayed and displayed!=fact.value:
            result['displayValue']=displayed
    return result


def _questionnaire_state_display(fact):
    """Reuse the adopted question/answer normalizer; never change a source."""
    if fact.kind!='self_reported':
        return None
    from .evaluation_fixed_assessment import _answer,_questionnaire_question,_questionnaire_display
    answer=_answer(fact).strip();question=_questionnaire_question(fact)
    if not re.fullmatch(r'否|无|没有|均无|以上均无|是|有|true|false|0',answer,re.I):
        return None
    # Zero is a boolean only for an explicit yes/no question, never for a
    # score, quantity, duration or continuous laboratory measurement.
    if answer=='0':
        if not re.search(r'是否|有无',question) or re.search(r'多少|几|评分|分数|次数|分钟|小时|数值|\d',question):
            return _questionnaire_display(fact)
        return _questionnaire_display({'name':question,'value':False})
    return _questionnaire_display(fact)


def _patient_assertion_object_bound(text,quotes,facts):
    from .evaluation_evidence_plan import risk_retrieval_concepts,_mentions
    for fid,quote in quotes.items():
        fact=facts[fid]
        if (_mentions(text,risk_retrieval_concepts([fact.name]))
                or len(_key(quote))>=3 and _key(quote) in _key(text)):
            return True
        display=_questionnaire_state_display(fact)
        if display:
            topic=re.sub(r'^(?:未报告|报告有|未|有)', '',display)
            # The existing question normalizer deliberately keeps "known"
            # and "explicit" wording. These frame the patient's answer;
            # they are not part of an allergy/drug/symptom entity name.
            topic=re.sub(r'^(?:明确|已知)(?:的)?','',topic)
            normalize=lambda value:re.sub(r'[或和及与/／、]','',_key(value))
            if len(normalize(topic))>=3 and normalize(topic) in normalize(text):
                return True
    return False


def _patient_negative_predicate_errors(text,quotes,facts):
    """A denied diagnosis does not deny symptoms; unknown is not negative."""
    if not re.search(r'否认|未见|没有|未发现|尚无|无|未报告|未提供',text):
        return []
    if re.search(r'未提供|不详|未知|未提示.{0,15}信息',text):
        return ['absence_of_information_is_not_negative_patient_fact']
    source=[]
    for fid,quote in quotes.items():
        fact=facts[fid];source.append(quote)
        normalized=_questionnaire_state_display(fact)
        if normalized:
            if normalized.startswith(('报告有','吸烟','使用过抗生素')):
                return ['negative_patient_assertion_opposes_source_answer']
            source.append(normalized)
        elif fact.kind=='clinical':
            source.append(fact.name+' '+str(fact.status or ''))
    scope=' '.join(source)
    for predicate in (r'症状|不适',r'诊断|确诊',r'病史|疾病史|过敏史'):
        if re.search(predicate,text) and not re.search(predicate,scope):
            return ['negative_patient_predicate_not_source_bound']
    # The literal question supplies the subject of a short "no", not all
    # diseases which happen to share that question's general topic. A
    # relative's positive history likewise cannot deny another disease.
    from .evaluation_fixed_assessment import _answer,_questionnaire_question
    for denial in _negative_patient_clauses(text):
        normalized=_negative_patient_topic(denial)
        if not normalized:
            continue
        bound=False
        for fid,quote in quotes.items():
            fact=facts[fid]
            display=_questionnaire_state_display(fact)
            if display and display.startswith(('未报告','未吸烟','未使用')):
                if _denied_patient_subset(denial,display):
                    bound=True;break
            for sentence in re.split(r'[，,。；;\n]+',quote):
                if (_negative_patient_clauses(sentence) and _denied_patient_subset(denial,sentence)):
                    bound=True;break
            if bound:
                break
            # A full yes/no question may have a source-normalized denial;
            # an ordinary narrative answer never inherits that denial.
            if (fact.kind=='self_reported' and re.fullmatch(r'否|无|没有|false|0',_answer(fact),re.I)
                    and display and _negative_patient_topic(_questionnaire_question(fact))==normalized):
                bound=True;break
        if not bound:
            return ['negative_patient_topic_or_predicate_not_source_bound']
    return []


def _negative_patient_clauses(text):
    """Split denials at their actual predicate, preserving each object."""
    return [match.group().strip() for match in re.finditer(
        r'(?:否认|未报告|未见|没有|未发现|尚无|暂无|无(?!法|论|需|创|效|关|论))'
        r'[^，,。；;\n]{1,90}',text or '')]


def _negative_patient_topic(text):
    value=re.sub(r'^(?:本人|用户|患者|自述|明确|已知|目前|当前|现在|近[一二三四五六七八九十\d]+个月|'
        r'未报告|报告有|否认|未见|没有|未发现|尚无|暂无|无|是否|有无|有|存在|出现|被)+','',str(text).strip())
    value=re.sub(r'(?:明确|已知)(?:的)?','',value)
    value=re.sub(r'[或和及与/／、]','',value)
    return _key(value.rstrip('？?。 '))


def _denied_patient_subset(assertion,source):
    """An explicit denied conjunction permits its members, never new ones."""
    target=_negative_patient_topic(assertion)
    if not target:
        return False
    if target==_negative_patient_topic(source):
        return True
    # Split only the explicit conjunction's objects. Do not use arbitrary
    # substrings: diagnosis and symptoms, or two diseases, remain distinct.
    strip=r'^(?:本人|用户|患者|自述|明确|已知|目前|当前|现在|未报告|报告有|否认|未见|没有|未发现|尚无|暂无|无|是否|有无)+'
    raw=re.sub(strip,'',str(source).strip())
    claimed=re.sub(strip,'',str(assertion).strip())
    parts=[_negative_patient_topic(part) for part in re.split(r'及|和|与|或|、|/|／',raw) if part.strip()]
    requested=[_negative_patient_topic(part) for part in re.split(r'及|和|与|或|、|/|／',claimed) if part.strip()]
    suffix=re.search(r'(过敏史|家族史|病史|诊断史|症状)\s*[。.!！]?$',raw)
    if suffix:
        parts.extend(part+_key(suffix[1]) for part in list(parts)
            if not re.search(r'史$|症状$|诊断$',part))
    return len(parts)>1 and bool(requested) and all(part in parts for part in requested)


def _patient_state_text_errors(text,facts):
    """Shared factual check for index reasons and public patient prose."""
    errors=[]
    for clause in _negative_patient_clauses(text):
        # Source provenance/absence-of-evidence is not a patient negative.
        if re.search(r'未(?:经|获|有).{0,8}(?:核验|审核|证据)|无.{0,8}(?:证据|依据)|'
                r'^无(?:已审|适用|明确|有效|独立)?(?:科学句|医学关联|科学关系|独立研究)|'
                r'未提供|不详|未知',clause):
            continue
        visible={fid:fact.value for fid,fact in facts.items() if fact.kind in {'clinical','self_reported'}
            or fid.startswith('profile:')}
        if not visible:
            errors.append('negative_patient_topic_or_predicate_not_source_bound')
        else:
            # Do not let one affirmative fact elsewhere override a genuine
            # explicit denial of this exact subject.
            if not any(not _patient_negative_predicate_errors(clause,{fid:quote},facts)
                    for fid,quote in visible.items()):
                errors.append('negative_patient_topic_or_predicate_not_source_bound')
    return list(dict.fromkeys(errors))


def _clinical_observed_abnormal(fact):
    from .clinical_report import _outside_printed_reference
    status = fact.status or ''
    reported = not re.search(r'未见|未发现|未提示|无(?:明显)?异常|正常|阴性|未检出', status) and bool(
        re.search(r'偏高|偏低|升高|降低|超标|异常|阳性|[↑↓]', status))
    return fact.kind == 'clinical' and (fact.abnormal or reported or _outside_printed_reference(
        fact.value, fact.reference_range, fact.unit))


def _reference_normal_numeric(fact):
    scalar = bool(re.fullmatch(r'\s*-?\d+(?:\.\d+)?\s*', str(fact.value)))
    paired_bp = bool(re.fullmatch(r'血压|血压测量|收缩压[/／]舒张压|BP|blood pressure', fact.name, re.I)
        and re.fullmatch(r'\s*\d+(?:\.\d+)?\s*[/／]\s*\d+(?:\.\d+)?\s*', str(fact.value)))
    return (fact.kind == 'clinical' and not _clinical_observed_abnormal(fact) and fact.reference_range is not None
        and (scalar or paired_bp)
        and not re.search(r'阴性|未检出|未见|否认', str(fact.status or '')))


def _fact_role(fact):
    if fact.kind=='function' and fact.name.endswith('（来源疾病模型指数）'):
        return 'source_disease_model'
    if fact.kind=='microbe':
        return 'measured_microbiome'
    if fact.kind=='function':
        return 'measured_function'
    if fact.kind=='self_reported':
        return 'patient_report'
    if fact.kind=='clinical':
        return 'clinical_measurement'
    if fact.fact_id.startswith('profile:'):
        return 'family_background' if '家族' in fact.name else 'profile_background'
    if fact.kind=='context' and fact.value in {'检出','未检出','阳性','阴性','异常','正常'} and (
            re.search(r'microbiome|菌群|宏基因',fact.source or '',re.I)
            or fact.fact_id.startswith('auxiliary-fact-context-')):
        return 'microbiome_association_screen'
    return 'source_context'


def add_profile_risk_facts(document, snapshot, *, user_id):
    """Import a service-owned profile slice without reviving old questionnaires.

    Native answers remain independent source facts. Profile history is never
    a current clinical examination; a relative's statement remains family
    context. Unconfirmed/inferred entries already have their separate
    background partition and cannot acquire positive-fact status here.
    """
    from .ports import ReportProfileFact, ReportProfileSnapshot
    from .profile_snapshot import (ANTHROPOMETRIC_ALLOW_LIST, BACKGROUND_ALLOW_LIST,
        FAMILY_HISTORY_ALLOW_LIST, profile_fact_text)
    from .schemas import ReportSourceFact

    if (not isinstance(snapshot, ReportProfileSnapshot) or snapshot.user_id != user_id
            or not isinstance(user_id, str) or not user_id.strip() or snapshot.stale
            or not isinstance(snapshot.self_subject_id, str) or not snapshot.self_subject_id.strip()):
        return 0
    labels = {**BACKGROUND_ALLOW_LIST, **FAMILY_HISTORY_ALLOW_LIST,
        'anthropometric.height': '身高', 'anthropometric.weight': '体重', 'anthropometric.waist': '腰围'}
    allowed = set(BACKGROUND_ALLOW_LIST) | set(FAMILY_HISTORY_ALLOW_LIST) | set(ANTHROPOMETRIC_ALLOW_LIST)
    assertions = {'observed', 'clinician_recorded', 'user_confirmed', 'user_reported'}
    known = {fact.fact_id for fact in document.facts}
    mirrors = {}

    def find_profile_answers(value, path):
        if isinstance(value, dict):
            identifier, origin = value.get('questionId'), value.get('sourcePath')
            if (isinstance(identifier, str) and origin == f'healthProfile.facts[{identifier}]'
                    and 'answer' in value):
                aliases = {path, 'clinicalDocument.background.' + path, origin}
                marker = 'healthSurvey.'
                if marker in path:
                    aliases.add('request.' + path[path.index(marker):])
                mirrors.setdefault(identifier, []).append((value['answer'], aliases))
            for key, child in value.items():
                find_profile_answers(child, path + '.' + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                find_profile_answers(child, path + f'[{index}]')

    find_profile_answers(document.background.get('patientReported', {}), 'patientReported')
    imported = 0
    for source in snapshot.facts:
        if (not isinstance(source, ReportProfileFact) or source.status != 'active'
                or source.assertion_level not in assertions or source.fact_type not in allowed
                or source.subject_kind not in {'self', 'blood_relative'}
                or not isinstance(source.fact_id, str) or not source.fact_id.strip()
                or not isinstance(source.subject_id, str) or not source.subject_id.strip()
                or source.value in (None, '', [], {})):
            continue
        if ((source.subject_kind == 'self' and source.subject_id != snapshot.self_subject_id)
                or (source.subject_kind == 'blood_relative' and source.subject_id == snapshot.self_subject_id)):
            continue
        identity = 'profile:' + source.fact_id
        if identity in known:
            continue
        family = source.subject_kind == 'blood_relative' or source.fact_type in FAMILY_HISTORY_ALLOW_LIST
        value = profile_fact_text(source.fact_type, source.display_name, source.value)
        if not value.strip():
            continue
        # prepare_evaluation_request can project this exact service fact into
        # its legacy questionnaire fallback. Use the real profile identity and
        # recorded source path, never equal names/values from independent QAs.
        if f'healthProfile.facts[{source.fact_id}]' in known or any(
                answer == value and bool(paths & known)
                for answer, paths in mirrors.get(source.fact_id, [])):
            continue
        # Explicit spouse/friend data cannot become inherited risk even if a
        # misclassified family-history record used an otherwise allowed type.
        relationship = source.value.get('relationship') if isinstance(source.value, dict) else None
        if family and isinstance(relationship, str) and re.search(
                r'配偶|丈夫|妻子|爱人|朋友|同事|公公|婆婆|岳父|岳母|spouse|husband|wife|friend|colleague',
                relationship, re.I):
            continue
        label = source.display_name or labels[source.fact_type]
        name = '家族史：' + label if family else label
        # The role is explicit in both name and source, including a family
        # diagnosis whose original text itself has no relationship word.
        document.facts.append(ReportSourceFact(
            fact_id=identity, kind='context' if family else 'self_reported',
            name=name, value=('血亲/家族记录：' + value) if family else value,
            status='家族背景' if family else '画像历史记录',
            date=source.effective_start or source.updated_at,
            source='长期健康画像；' + ('血亲/家族背景' if family else '本人记录')
                + '；断言：' + source.assertion_level + '；事实：' + source.fact_id))
        known.add(identity)
        imported += 1
    return imported


def _registry(document):
    registry = getattr(document, 'risk_registry', None)
    if not isinstance(registry, RiskRegistry):
        registry = RiskRegistry()
        document.risk_registry = registry
    return registry


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value))).casefold()


_CLINICAL_ENTITY_ALIASES={
    'cardiovascularDisease':('心血管疾病','cardiovascular disease','CVD'),
    'atheroscleroticCardiovascularDisease':('动脉粥样硬化性心血管疾病','atherosclerotic cardiovascular disease','ASCVD'),
    'centralObesity':('腹型肥胖','中心性肥胖','abdominal obesity','central obesity'),
    'gastroesophagealRefluxSymptoms':('反酸','餐后反酸','regurgitation','acid regurgitation','胃食管反流相关症状'),
    # Generic allergy labels describe the same supplied screening endpoint.
    # Specific food, airway and skin diseases retain their own identities.
    'allergy':('过敏','过敏症','过敏性疾病','allergy','allergic disease','allergic diseases'),
}


def _risk_identity(name):
    from .evaluation_catalog import fixed_disease_code
    base = re.sub(r'(?:相关|发生|易感)?风险$', '', str(name).strip())
    for identifier,aliases in _CLINICAL_ENTITY_ALIASES.items():
        if _key(base) in {_key(identifier),*(_key(alias) for alias in aliases)}:
            return identifier
    if re.fullmatch(r'(?:功能性)?便秘(?:相关)?(?:排便障碍|肠道功能紊乱|排便异常|症状|表现)?',base):
        return 'constipation'
    if re.fullmatch(r'(?:餐后反酸|胃食管反流|反酸)(?:相关)?(?:症状|上消化道症状|上消化道功能紊乱|胃食管功能问题|胃食管反流)?',base):
        return 'gastroesophagealRefluxSymptoms'
    if re.fullmatch(r'血脂异常(?:及代谢)?(?:相关)?(?:代谢)?',base):
        return 'hyperlipidemia'
    return fixed_disease_code(name) or fixed_disease_code(base) or 'disease-' + _digest(_key(base))[:20]


def _clean_ids(value, known):
    if not isinstance(value, list) or any(type(item) is not str for item in value):
        raise ValueError('fact IDs must be a list of strings')
    if not set(value) <= known.keys():
        raise ValueError('unknown patient fact')
    return list(dict.fromkeys(value))


_BOUNDARY = re.compile(r'检索(?:结果|过程)|审核(?:通过|未通过|规则)|缺少适用原文|'
    r'不能(?:据此|用于)|不构成诊断|原模型|原报告(?:记录|标记|显示)|'
    r'AI综合(?:评级|评估)|按(?:照)?(?:上述|本项|本报告)(?:规则|标准)|'
    r'尚未核验|未核验|待复核|待核实|需核对|来源疾病模型|已审内容|'
    r'不等同(?:于)?.{0,12}(?:诊断|患病)|不能替代.{0,20}(?:评估|诊断)|'
    r'需(?:要)?结合.{0,15}(?:临床|实际|评估|判断)|辅助参考|背景参考|'
    r'(?:仅|只)(?:作为?|作|供).{0,12}(?:参考|背景|线索)|不据此判断|'
    r'而非(?:临床)?诊断|尚无.{0,12}确诊依据|不单独作为.{0,12}依据|'
    r'不将.{0,15}(?:模型|菌群).{0,15}(?:依据|诊断)|'
    r'已审(?:核)?(?:的)?(?:内容|个人|依据|关系|原子|管理)|公开(?:关注|风险)?等级|本槽|\bfactIds?\b|\bplanning\b')


def _candidate_name(name):
    name = str(name or '').strip()
    # These are generated provenance/review wrappers, never medical stages.
    editorial=(r'(?:来源|原报告|原始报告)(?:疾病)?模型(?:指数|提示|信号|记录)?|'
        r'来源(?:报告|资料|提示)|(?:待|尚未)(?:复核|核验|核实|审核)')
    for _ in range(4):
        cleaned=re.sub(r'[（(](?:'+editorial+r')[）)]\s*$','',name).strip()
        cleaned=re.sub(r'(?:[，、；;：:\s]+)?(?:待复核|待核验|待核实|待审核|尚未核验)$','',cleaned).strip()
        if cleaned==name:
            break
        name=cleaned
    name = re.sub(r'[（(]([^）)]*)[）)]', lambda match: '' if re.search(
        r'甘油三酯|胆固醇|低密度|高密度|血糖|糖化|腰围|偏高|偏低|升高|降低|↑|↓', match[1])
        else match[0], name)
    name = re.sub(r'及[^，。；]{0,30}(?:管理|关注方向|调整方案)$', '', name)
    return name.strip(' ，；、')


def _source_entity_candidate_name(name,model_ids,known):
    # A structural source entity resolves its own editorial wrapper. It
    # cannot rename a different disease/subtype merely because its ID was
    # accidentally included in the proposed candidate.
    head=re.split(r'[（(]',name,1)[0].strip()
    if not re.search(r'[（(](?:来源|原报告|原始报告)',name):
        return name
    identities={_risk_identity(known[fid].name.removesuffix('（来源疾病模型指数）'))
        for fid in model_ids if fid in known and _fact_role(known[fid])=='source_disease_model'}
    return head if _risk_identity(head) in identities else name


def _source_association_candidate_name(document,name,row,known):
    """Resolve a report-screen wrapper only through its bound declaration."""
    if not re.search(r'(?:相关)?菌群(?:线索|信号|特征)(?:相关风险)?$',name):
        return name
    from .evaluation_risk_source_links import source_association_view
    base=re.sub(r'(?:相关)?菌群(?:线索|信号|特征)(?:相关风险)?$','',name).strip()
    ids={fid for key in ('supportFactIds','backgroundFactIds','sourceModelFactIds')
        for fid in _clean_ids(row.get(key,[]),known)}
    matches={item['name'] for item in source_association_view(document)
        if ids&set(item['factIds']) and _risk_identity(base)==_risk_identity(item['name'])}
    return next(iter(matches))+'相关风险' if len(matches)==1 else name


def _native_association_category(document,candidate):
    """A live same-condition native panel may supply its existing display axis."""
    from .evaluation_catalog import HEALTH_ASSOCIATION_CATEGORIES
    from .evaluation_risk_source_links import source_association_view
    owned=set([*candidate.support_fact_ids,*candidate.background_fact_ids,
        *candidate.opposing_fact_ids,*candidate.source_model_fact_ids])
    categories={HEALTH_ASSOCIATION_CATEGORIES[item['name']]
        for item in source_association_view(document)
        if item['name'] in HEALTH_ASSOCIATION_CATEGORIES
        and _risk_identity(item['name'])==candidate.disease_id
        and owned&set(item['factIds'])}
    return next(iter(categories)) if len(categories)==1 else None


def _workflow_endpoint(name):
    """A clinical activity is not a disease or a patient health state."""
    value=_endpoint_name(name)
    return bool(re.search(r'(?:风险)?(?:评估|评定|评价|筛查|检测)(?:相关)?$|'
        r'(?:管理|关注|干预|调整)(?:方向|方案|计划|重点)?(?:相关)?$|'
        r'\b(?:risk assessment|risk evaluation|screening|management plan|management direction)$|'
        r'^(?:(?:risk )?(?:evaluation|assessment|screening|management))\s+(?:of|for)\b',value,re.I))



_BARE_CLINICAL_OBSERVATIONS = {
    'symptom': (
        '腹痛','腹部疼痛','abdominal pain',
        '腹胀','腹胀排气','胃胀','胀气','abdominal bloating','bloating','flatulence',
        '腹泻','diarrhea','diarrhoea',
        '反酸','胃酸反流','酸反流','acid regurgitation','regurgitation','acid reflux',
        '烧心','heartburn','pyrosis',
        '便秘','constipation','排便困难','排便费力','硬便','hard stools','straining',
        '恶心','nausea','呕吐','vomiting',
        '头痛','headache','胸痛','chest pain',
        '便血','血便','黑便','hematochezia','melena',
        '乏力','疲劳','fatigue',
    ),
    'indicator_problem': (
        '身高','height','体重','body weight','腰围','waist circumference',
        '体质指数','体重指数','BMI','body mass index',
        '血压','blood pressure','收缩压','systolic blood pressure','舒张压','diastolic blood pressure',
        '血脂','血脂异常','blood lipids','abnormal blood lipids','blood lipid abnormality',
        'dyslipidemia','dyslipidaemia',
        '甘油三酯','triglyceride','triglycerides','TG',
        '总胆固醇','total cholesterol','TC',
        '低密度脂蛋白胆固醇','低密度脂蛋白','LDL cholesterol','LDL-C',
        '高密度脂蛋白胆固醇','高密度脂蛋白','HDL cholesterol','HDL-C',
        '血糖','空腹血糖','blood glucose','fasting plasma glucose','FPG',
        '糖化血红蛋白','glycated hemoglobin','glycated haemoglobin','HbA1c',
        '白细胞计数','white blood cell count','血红蛋白','hemoglobin','haemoglobin',
        '血小板计数','platelet count','肌酐','creatinine','尿蛋白','urine protein',
        '丙氨酸氨基转移酶','ALT','粪便隐血','fecal occult blood','faecal occult blood',
    ),
}


def non_disease_endpoint_kind(name):
    """Recognise an exact symptom/measurement, not a disease containing it.

    Risk wrappers and language variants cannot turn a source symptom screen
    into a disease. Medical qualifiers are preserved, so complete syndromes
    still require their ordinary independent disease review.
    """
    text=unicodedata.normalize('NFKC',str(name)).strip()
    text=re.sub(r'^(?:risk|susceptibility)\s+(?:of|for|to)\s+(?:developing\s+)?','',text,flags=re.I)
    text=re.sub(r'(?:相关|发生|发作|易感|患病)?(?:风险|倾向)(?:升高|增高|提示)?$','',text)
    text=re.sub(r'\s+(?:risk|susceptibility|predisposition)$','',text,flags=re.I)
    text=re.sub(r'(?:[- ]related|[- ]associated|相关)$','',text,flags=re.I).strip()
    text=re.sub(r'(?:相关)?菌群(?:异常|筛查|信号|特征)$','',text).strip()
    text=re.sub(r'^(?:gut )?microbiome[- ]associated\s+','',text,flags=re.I)
    text=re.sub(r'[- ]associated (?:microbiome|microbiota|bacteria)(?: abnormalities| features| signals)?$','',text,flags=re.I).strip()
    text=re.sub(r'(?:症状|表现)$','',text).strip()
    keys={kind:{_key(term) for term in terms} for kind,terms in _BARE_CLINICAL_OBSERVATIONS.items()}
    def one(value):
        if _key(value) in keys['symptom']:
            return 'symptom'
        raw=_key(value)
        if raw in keys['indicator_problem']:
            return 'indicator_problem'
        altered=re.sub(r'^(?:elevated|increased|decreased|abnormal|high|low)\s+','',value,flags=re.I)
        altered=re.sub(r'(?:偏高|偏低|升高|降低|增高|下降|异常|阳性|阴性|检出)$','',altered).strip()
        altered=re.sub(r'\s+(?:elevated|increased|decreased|abnormal|high|low|positive|negative|detected)$','',altered,flags=re.I).strip()
        return 'indicator_problem' if _key(altered) in keys['indicator_problem'] else None
    kind=one(text)
    if kind:
        return kind
    parts=[part.strip() for part in re.split(r'[/、,，;；]|\b(?:and|or)\b|和|与|及',text,flags=re.I) if part.strip()]
    kinds=[one(part) for part in parts]
    if len(parts)>1 and all(kinds):
        return 'symptom' if set(kinds)=={'symptom'} else 'indicator_problem'
    return None


def _nonspecific_clinical_endpoint(name):
    """A burden/theme is useful context, never a newly named disease.

    This only rejects an unresolved generated endpoint.  It does not infer
    a replacement diagnosis, restrict reports to a fixed disease catalogue,
    or discard the underlying observations and literature relationship.
    Admission repair must choose the actual supported clinical outcome.
    """
    value = _endpoint_name(name)
    if re.search(r'(?<![A-Za-z])(?:multi[- ]?morbidity|comorbid(?:ity|ities)(?: burden)?|combined (?:disease|risk) burden)(?![A-Za-z])', value, re.I):
        return True
    return bool(re.search(r'多病共存|多病共患|共病(?:负担|组合)|'
        r'(?:其他|其它|其余|剩余)(?:的)?(?:代谢)?(?:组分|成分|疾病|病种|风险)|'
        r'(?:疾病|风险|问题|指标|代谢)(?:的)?(?:组合|组合型|方向)|'
        r'(?:组合|综合负担|多系统负担)(?:相关)?$|'
        r'(?:及|与|合并|伴有)(?:代谢|心血管|营养|免疫)(?:相关)?$', value)
        or re.fullmatch(r'(?:心血管)?代谢(?:相关)?(?:负担|问题)?|'
            r'(?:肠道|免疫|神经|精神|营养|菌群)(?:相关)?(?:方向|负担|健康方向)', value))


def _lipid_subtype_name(name):
    return bool(re.search(r'混合[型性]?(?:高脂血症|血脂异常)|高胆固醇血症|高甘油三酯血症|'
        r'\b(?:mixed hyperlipid[ae]mia|hypercholesterol[ae]mia|hypertriglycerid[ae]mia)\b', str(name), re.I))


def _literal_lipid_subtype(name, facts):
    """A generic lipid abnormality cannot supply an unstated subtype label."""
    base = re.sub(r'(?:相关|发生|易感)?风险$', '', str(name)).strip()
    for fact in facts:
        if fact.kind not in {'clinical', 'self_reported'}:
            continue
        value = fact.value.strip()
        if re.fullmatch(r'否|无|没有|未诊断|未确诊|未患|0|false|阴性', value, re.I):
            continue
        text = value
        if base not in value:
            if (_key(fact.name) != _key(base) or not re.search(r'确诊|诊断|患有|有|^是$', value)
                    or re.search(r'否认|未确诊|未诊断|未患|没有|无|疑似|可能|考虑', value)):
                continue
            text = fact.name + ' ' + value
        for clause in re.split(r'[。；;\n]', text):
            if base not in clause:
                continue
            before = clause.split(base, 1)[0]
            if re.search(r'否认|未确诊|未诊断|未患|没有|无|疑似|可能|考虑', before):
                continue
            if fact.kind == 'self_reported' and re.search(r'家族|血亲|亲属|父亲|母亲|祖父|祖母', before):
                continue
            return True
    return False


def _personal_lipid_classification(claim, name, facts=None, originals=None):
    """Route a new subtype through actual diagnostic science, not aliases.

    This structural check is necessary, never sufficient for approval.  The
    existing claim and assessment reviews still verify the guideline rule,
    all thresholds, the person's conditions and the classification itself.
    """
    base = re.sub(r'(?:相关|发生|易感)?风险$', '', str(name)).strip()
    if (claim.role != 'diagnostic_principle' or not claim.evidence or base not in claim.text
            or not re.search(r'符合|满足[^。；;]{0,20}(?:标准|条件)|可判定为|诊断为|'
                r'\b(?:meets? (?:the )?criteria|classif\w* as)\b', claim.text, re.I)):
        return False
    if originals is not None and any(anchor.evidence_key not in originals
            or anchor.quote not in originals[anchor.evidence_key]['sourceText'] for anchor in claim.evidence):
        return False
    if facts is None:
        return True
    measured = [facts[fid] for fid, quote in claim.fact_quotes.items() if fid in facts
        and facts[fid].kind == 'clinical' and quote in facts[fid].value]
    if not measured:
        return False
    if re.search(r'混合[型性]?(?:高脂血症|血脂异常)|mixed hyperlipid', name, re.I):
        # The official clinical classification requires both TC and TG.
        # Neither an isolated LDL result nor a same-value other assay can
        # stand in for a missing/normal constituent. Guideline thresholds
        # and eligibility remain the independent reviewers' responsibility.
        return all(any(re.fullmatch(pattern, fact.name, re.I) and _clinical_observed_abnormal(fact)
            and not re.search(r'偏低|降低|↓', fact.status or '') for fact in measured)
            for pattern in (r'总胆固醇|总胆固醇\s*/\s*TC|TC|total cholesterol',
                r'甘油三酯|甘油三酯\s*/\s*TG|TG|triglycerides?'))
    return True


def _personal_clinical_disease_assessment(claim, candidate, facts=None, originals=None):
    """A reviewed personal diagnostic assessment is not incidence research.

    This is a necessary routing check only. Original entailment, applicable
    diagnostic conditions, missing criteria and the qualified conclusion all
    still pass the existing independent claim and clinical reviews.
    """
    personal_wording = re.search(r'(?:您|本人|本次|当前|这些|上述|该)(?:的)?[^。；;]{0,55}'
        r'(?:符合|提示|考虑|疑似|可能|需关注|支持.{0,8}评估)', claim.text)
    # Clinical prose may omit the subject. Same-target, fact-bound cautious
    # assessment still needs the full independent classification review;
    # routing it here confers no diagnostic or publication permission.
    cautious_wording = re.search(r'考虑|疑似|可能(?:存在|患有|为)|'
        r'需(?:要)?[^。；;]{0,16}(?:按|依据)[^。；;]{0,16}(?:标准|评估|判断)', claim.text)
    if (claim.role != 'diagnostic_principle' or not claim.evidence or not claim.fact_quotes
            or not (personal_wording or cautious_wording)
            or not any(_risk_identity(term) == _risk_identity(candidate.name) for term in claim.outcome_terms)
            or re.sub(r'(?:相关|发生|易感)?风险$', '', candidate.name) not in claim.text):
        return False
    if facts is not None and (not _valid_fact_quotes(claim.fact_quotes, facts, claim.fact_quotes)
            or not any(facts[fid].kind in {'clinical','self_reported'} for fid in claim.fact_quotes)):
        return False
    if originals is not None and any(anchor.evidence_key not in originals
            or anchor.quote not in originals[anchor.evidence_key]['sourceText'] for anchor in claim.evidence):
        return False
    return True


def _needs_specific_clinical_target(candidate):
    # Admission repair and functional-theme deduplication have different
    # duties. An unresolved clinical hypothesis must never silently vanish
    # as a functional supporting topic during safe independent fallback.
    return (candidate.certainty=='potential_risk'
        and any(relation.kind=='clinical' for relation in candidate.relations)
        and (bool(re.search(r'心血管代谢风险|cardiometabolic risk',candidate.name,re.I))
            or _generic_topic(candidate)))


def _conjoined_risk_endpoints(name, certainty):
    """An exposure and its proposed outcome must not acquire a third identity.

    Recognize only two already supported disease names. The model repairs
    their candidate boundaries; this helper neither infers a disease from a
    marker nor decides which condition the patient has.
    """
    if certainty != 'potential_risk' or not str(name).endswith('风险'):
        return ()
    base = re.sub(r'(?:相关|发生|易感)?风险$', '', str(name))
    parts = re.split(r'及|与|合并|伴有', base)
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return ()
    identities = [_risk_identity(part.strip()) for part in parts]
    if any(value.startswith('disease-') for value in identities) or len(set(identities)) != 2:
        return ()
    return tuple(part.strip() for part in parts)


def _candidate_entity_scope(candidate):
    kind = candidate.get('entityKind', candidate.get('entity_kind')) if isinstance(candidate, dict) else getattr(candidate, 'entity_kind', None)
    return 'observation' if kind in {'symptom', 'indicator_problem'} else 'disease'



def _candidate_merge_scope(candidate):
    """Unproved current classification cannot consume a future disease hypothesis."""
    if _candidate_entity_scope(candidate) != 'disease':
        return 'observation'
    certainty = candidate.get('certainty') if isinstance(candidate, dict) else candidate.certainty
    if certainty in {'confirmed', 'current_problem', 'self_reported'}:
        token = None if isinstance(candidate, dict) else getattr(candidate, '_current_classification_scope', None)
        reviewed = bool(token and hasattr(candidate, 'model_dump') and token == _digest(candidate.model_dump()))
        return 'disease' if reviewed else 'pending_current_classification'
    return 'disease'


def _admit_candidates(rows, document, known, registry, *, mark_invalid=True):
    seen = {(candidate.disease_id, _candidate_merge_scope(candidate)): candidate for candidate in registry.candidates}
    rejected = []
    for raw in rows:
        try:
            row = dict(raw)
            name = _candidate_name(row.get('name'))
            model_ids=_clean_ids(row.get('sourceModelFactIds',[]),known)
            model_ids.extend(fid for key in ('supportFactIds','backgroundFactIds','opposingFactIds')
                for fid in _clean_ids(row.get(key,[]),known) if _fact_role(known[fid])=='source_disease_model'
                and fid not in model_ids)
            name=_source_entity_candidate_name(name,model_ids,known)
            name=_source_association_candidate_name(document,name,row,known)
            if re.search(r'(?:相关)?菌群(?:线索|信号|特征)(?:相关风险)?$',name):
                raise ValueError('source_cue_is_not_a_disease_endpoint: use the exact bound report condition or a separately supported disease, not a microbiome cue label')
            if _workflow_endpoint(name):
                raise ValueError('workflow_target_is_not_a_disease: repair the hypothesis to an actual source-supported disease/health state; assessment or management is an action')
            if _nonspecific_clinical_endpoint(name):
                raise ValueError('nonspecific_clinical_endpoint_requires_repair: other components, multimorbidity, '
                    'combined risks and directions are supporting context, not a separate disease; '
                    'retain the original facts and relationship, choose its actual supported specific '
                    'disease or clinical problem with unchanged certainty, or attach it to that problem '
                    'when it has no independent disease outcome; do not invent ASCVD or any diagnosis')
            if _conjoined_risk_endpoints(name, row.get('certainty', 'potential_risk')):
                raise ValueError('conjoined_disease_endpoints_require_atomic_candidates: separate the existing health state from its proposed disease outcome, keeping their actual facts and certainty')
            identity = _risk_identity(name)
            scope = _candidate_merge_scope(row)
            row.update(riskId='risk-' + _digest(identity if scope == 'disease' else scope + ':' + identity)[:20],
                diseaseId=identity, name=name)
            # This is queue metadata, not a clinical risk grade. An out-of-
            # range scheduling number must not erase a valid health topic.
            priority = row.get('priority', 2)
            row['priority'] = min(3, max(1, priority)) if type(priority) is int else 2
            for key in ('supportFactIds', 'opposingFactIds', 'backgroundFactIds', 'sourceModelFactIds'):
                row[key] = _clean_ids(row.get(key, []), known)
            proposed_overlap = set(row['supportFactIds']) & set(row['opposingFactIds'])
            neutral = []
            if row.get('certainty', 'potential_risk') == 'potential_risk':
                neutral = [fid for fid in row['opposingFactIds'] if _reference_normal_numeric(known[fid])]
                row['opposingFactIds'] = [fid for fid in row['opposingFactIds'] if fid not in neutral]
                row['backgroundFactIds'].extend(fid for fid in neutral if fid not in row['backgroundFactIds'])
            # Role metadata is recoverable from the immutable fact itself;
            # this does not invent or reinterpret its observed value.
            misplaced=[fid for fid in row['sourceModelFactIds'] if _fact_role(known[fid])!='source_disease_model'
                or _risk_identity(known[fid].name.removesuffix('（来源疾病模型指数）'))!=identity]
            foreign_models={fid for fid in misplaced if _fact_role(known[fid])=='source_disease_model'}
            for key in ('supportFactIds','opposingFactIds'):
                row[key]=[fid for fid in row[key] if fid not in foreign_models]
            if proposed_overlap - foreign_models:
                raise ValueError('support and opposition must be distinct')
            row['sourceModelFactIds']=[fid for fid in row['sourceModelFactIds'] if fid not in misplaced]
            role_ids=set(row['supportFactIds']+row['opposingFactIds']+row['backgroundFactIds'])
            row['backgroundFactIds'].extend(fid for fid in misplaced if fid not in role_ids)
            for relation in row.get('relations',[]):
                relation_ids=_clean_ids(relation.get('factIds',relation.get('fact_ids',[])),known)
                present=set(row['supportFactIds']+row['opposingFactIds']+row['backgroundFactIds']+row['sourceModelFactIds'])
                row['backgroundFactIds'].extend(fid for fid in relation_ids if fid not in present)
                if relation.get('kind')=='source_model' and any(_fact_role(known[fid])!='source_disease_model' for fid in relation_ids):
                    relation['kind']='microbiome' if any(known[fid].kind=='microbe' for fid in relation_ids) else 'function'
                if relation.get('kind')=='clinical' and all(_fact_role(known[fid])=='microbiome_association_screen' for fid in relation_ids):
                    relation['kind']='microbiome'
            if set(row['supportFactIds']) & set(row['opposingFactIds']):
                raise ValueError('support and opposition must be distinct')
            candidate = RiskCandidate.model_validate(row)
            candidate._reference_opposition_proposals = set(neutral)
            if (_needs_specific_clinical_target(candidate)
                    and not _declared_risk_signal_ids(candidate,known)):
                raise ValueError('generic_clinical_target_requires_specific_hypothesis: return a source-bound disease endpoint, not an organ/metabolic management theme')
            from .evaluation_catalog import fixed_disease_code,disease_category
            if fixed_disease_code(identity):
                candidate.category=disease_category(code=identity,name=name)
            elif native_category:=_native_association_category(document,candidate):
                candidate.category=native_category
            if candidate.certainty in {'current_problem','self_reported'} and not any(
                    known[fid].kind in {'clinical','self_reported'} for fid in candidate.support_fact_ids):
                candidate.certainty='potential_risk'
            allowed = set([*candidate.support_fact_ids, *candidate.opposing_fact_ids,
                *candidate.background_fact_ids, *candidate.source_model_fact_ids])
            if any(not set(relation.fact_ids) <= allowed for relation in candidate.relations):
                raise ValueError('relation must bind its actual candidate facts')
            if any(known[fid].kind != 'function' or not re.search(r'模型|风险指数', known[fid].name)
                    for fid in candidate.source_model_fact_ids):
                raise ValueError('source model role not established by original fact')
            for fact in known.values():
                if (fact.kind == 'function' and fact.name.endswith('（来源疾病模型指数）')
                        and _risk_identity(fact.name.removesuffix('（来源疾病模型指数）')) == identity
                        and fact.fact_id not in candidate.source_model_fact_ids):
                    candidate.source_model_fact_ids.append(fact.fact_id)
            if candidate.certainty == 'confirmed' and not any(
                    problem.certainty == 'confirmed' and _risk_identity(problem.title) == identity
                    and set(candidate.support_fact_ids) & {source.fact_id for source in problem.sources}
                    for problem in document.problems):
                candidate.certainty = 'current_problem'
            previous = seen.get((identity, scope))
            if previous is not None:
                _merge_candidate(previous,candidate)
                if native_category:=_native_association_category(document,previous):
                    previous.category=native_category
            else:
                seen[(identity, scope)] = candidate
        except (ValueError, TypeError, ValidationError) as error:
            rejected.append({'candidate':raw, 'validationErrors':str(error)[:1200]})
            if mark_invalid:
                registry.incomplete.append('RISK_CANDIDATE_INVALID')
    registry.candidates = list(seen.values())
    return rejected


def _merge_candidate(target,source):
    if _candidate_merge_scope(target) != _candidate_merge_scope(source):
        return False  # An observation and a disease hypothesis are not synonyms.
    classified=any(getattr(item,'_current_classification_scope',None)==_digest(item.model_dump())
        for item in (target,source))
    for field in ('support_fact_ids','opposing_fact_ids','background_fact_ids',
            'source_model_fact_ids','search_terms','exposure_terms'):
        values=getattr(target,field)
        values.extend(value for value in getattr(source,field) if value not in values)
    target.relations.extend(relation for relation in source.relations if relation not in target.relations)
    target.priority=min(target.priority,source.priority)
    certainty_order = {'confirmed': 0, 'current_problem': 1, 'self_reported': 2, 'potential_risk': 3}
    if certainty_order[source.certainty] < certainty_order[target.certainty]:
        target.certainty, target.name = source.certainty, source.name
    target._reference_opposition_proposals.update(source._reference_opposition_proposals)
    conflicts=set(target.support_fact_ids)&(set(target.opposing_fact_ids)
        | target._reference_opposition_proposals)
    target._role_conflicts=list(dict.fromkeys([*target._role_conflicts,*source._role_conflicts,*sorted(conflicts)]))
    conflicts=set(target._role_conflicts)
    # A conflicted role is explicitly deferred for independent risk review,
    # not silently resolved in favour of either supporting interpretation.
    target.support_fact_ids=[fid for fid in target.support_fact_ids if fid not in conflicts]
    target.opposing_fact_ids=[fid for fid in target.opposing_fact_ids if fid not in conflicts]
    target.background_fact_ids.extend(fid for fid in conflicts if fid not in target.background_fact_ids)
    if classified:
        target._current_classification_scope=_digest(target.model_dump())


def _fact_chunks(facts, *, max_facts=36, max_chars=10000):
    """Bound input size without truncating an original observation."""
    batch, size = [], 0
    for fact in facts:
        cost = len(json.dumps(_fact_view(fact), ensure_ascii=False))
        if batch and (len(batch)>=max_facts or size+cost>max_chars):
            yield batch
            batch, size = [], 0
        batch.append(fact); size += cost
    if batch:
        yield batch


def _signal_batches(signals, facts):
    """At most four health themes, with each large theme safely partitioned."""
    parts = []
    for signal in signals:
        chunks = list(_fact_chunks([facts[fid] for fid in signal['factIds']]))
        for index, chunk in enumerate(chunks):
            part = {**signal, 'factIds':[fact.fact_id for fact in chunk],
                'facts':[_fact_view(fact) for fact in chunk]}
            if len(chunks)>1:
                part.update(signalId=signal['signalId']+':part:'+str(index+1),
                    sourceSignalId=signal['signalId'])
            parts.append(part)
    batch, size = [], 0
    for part in parts:
        cost = len(json.dumps(part, ensure_ascii=False))
        if batch and (len(batch)>=4 or size+cost>12000):
            yield batch
            batch, size = [], 0
        batch.append(part);size+=cost
    if batch:
        yield batch


async def _candidate_generate(client,task,facts):
    """Short transport IDs reduce repeated JSON output, never source IDs."""
    prefix='RF'
    while any(re.fullmatch(re.escape(prefix)+r'\d+',identifier) for identifier in facts):
        prefix+='F'
    aliases={identifier:prefix+str(index+1) for index,identifier in enumerate(facts)
        if len(identifier)>40}
    def replace(value,mapping,parent=''):
        if isinstance(value,dict):
            return {key:replace(child,mapping,key) for key,child in value.items()}
        if isinstance(value,list):
            return [replace(child,mapping,parent) for child in value]
        if isinstance(value,str) and (parent in {'factId','fact_id','factIds','fact_ids'}
                or parent.endswith(('FactId','FactIds','fact_id','fact_ids'))):
            return mapping.get(value,value)
        return value
    if not aliases:
        return await client.generate(task)
    response=await client.generate(replace(task,aliases))
    return replace(response,{alias:identifier for identifier,alias in aliases.items()})


async def _bounded_generate(client, tasks, *, facts=None):
    gate = asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
    async def run(task):
        async with gate:
            # A truncated/invalid result retries only its small independent
            # batch. Other completed domains are never thrown away.
            for attempt in range(2):
                try:
                    return await _candidate_generate(client,task,facts) if facts is not None else await client.generate(task)
                except Exception as error:
                    if attempt:
                        return error
    return await asyncio.gather(*(run(task) for task in tasks))


async def _admit_discovery_response(client, response, task, document, facts, registry):
    rows = response.get('candidates') if isinstance(response, dict) else None
    if not isinstance(rows, list):
        return False
    if task.get('domainFocus')=='菌群、功能与源疾病模型':
        current={fid for signal in task.get('sourceSignals',[])
            for fid in [*signal['factIds'],*signal.get('memberFactIds',[])]}
        # A clinical hypothesis copied unchanged into a normal-C batch is
        # already handled by the clinical task; it is not a new C finding.
        rows=[row for row in rows if isinstance(row,dict) and (
            set(row.get('sourceModelFactIds',[]))&current or any(
                relation.get('kind') in {'microbiome','function'} and set(relation.get('factIds',[]))&current
                for relation in row.get('relations',[]) if isinstance(relation,dict)))]
    rejected = _admit_candidates(rows, document, facts, registry, mark_invalid=False)
    for item in rejected:
        try:
            atomic_repair = 'conjoined_disease_endpoints_require_atomic_candidates' in str(item)
            repaired = await _candidate_generate(client,{**task, 'task':'repair_integrated_risk_candidate',
                'repairItem':item, 'rules':[*task['rules'],
                    '只修此一候选的字段格式、分类、已提供的真实ID与关系元数据。不可增加未知事实、虚构原始诊断或改变科学结论；不能修复则返回空candidates。',
                    '疾病/健康状态作为检索结局；风险评估、管理方向、筛查流程不是疾病。宽泛心血管代谢等主题须依据本批实际暴露提出具体可检索疾病假设，不能把原词加“相关风险”充当病名；没有具体假设则返回空，不新增患者诊断。',
                    *(['本项把两个不同疾病或当前问题及潜在结局写成一个名称。只拆成原名中已有的两个具体端点，各自绑定原候选中真正适用的事实、关系和确定性；原来只是假设的疾病保持potential_risk。当前血脂异常等不能再拼入脂肪肝/心血管风险标题，作为对应风险的依据即可。不得增加第三个疾病，也不得因拆分丢掉原有的潜在结局。'] if atomic_repair else [])],
                'outputSchema':task['outputSchema']},facts)
            fixed = repaired.get('candidates',[]) if isinstance(repaired,dict) else []
            if atomic_repair:
                endpoints = {_risk_identity(value) for value in _conjoined_risk_endpoints(
                    item['candidate'].get('name'), item['candidate'].get('certainty', 'potential_risk'))}
                fixed = [candidate for candidate in fixed if isinstance(candidate, dict)
                    and _risk_identity(candidate.get('name', '')) in endpoints]
            if not fixed or _admit_candidates(fixed[:2] if atomic_repair else fixed[:1],document,facts,registry,mark_invalid=False):
                registry.incomplete.append('RISK_CANDIDATE_INVALID')
            if atomic_repair and not endpoints <= {candidate.disease_id for candidate in registry.candidates}:
                registry.incomplete.append('RISK_CANDIDATE_INVALID')
        except Exception:
            registry.incomplete.append('RISK_CANDIDATE_INVALID')
    return True


def _generic_topic(candidate):
    from .evaluation_catalog import fixed_disease_code
    return (not fixed_disease_code(candidate.disease_id) and bool(re.search(
        r'菌群失(?:调|衡)|微生态失衡|合成代谢功能异常|代谢功能异常|合成代谢异常|功能异常相关|'
        r'情绪与精神心理|骨与神经退行性|菌群结构异常|有益菌占比|'
        r'骨代谢与骨量维护|肝脏代谢负荷|免疫调节功能减弱',candidate.name,re.I)))


def _candidate_group_valid(group,by_id,seen=()):
    members=group.get('memberRiskIds',[]) if isinstance(group,dict) else []
    target=group.get('canonicalRiskId') if isinstance(group,dict) else None
    if (not isinstance(members,list) or not members or any(not isinstance(identifier,str) for identifier in members)
            or target not in members or not set(members)<=by_id.keys() or set(seen).intersection(members)
            or len(set(members))!=len(members) or len(str(group.get('reason','')))<8):
        return False
    if len({_candidate_merge_scope(by_id[identifier]) for identifier in members}) > 1:
        return False
    from .evaluation_catalog import fixed_disease_code
    codes={by_id[identifier].disease_id for identifier in members
        if fixed_disease_code(by_id[identifier].disease_id)}
    topic_roles={_generic_topic(by_id[identifier]) for identifier in members}
    return not (len(topic_roles)>1 or len(codes)>1 or (codes and any(not fixed_disease_code(by_id[identifier].disease_id)
        for identifier in members)) or ('hyperlipidemia' in codes and any(
        re.search(r'心血管|动脉粥样硬化|ASCVD',by_id[identifier].name,re.I)
        and by_id[identifier].disease_id!='hyperlipidemia' for identifier in members)))


async def _deduplicate_candidate_topics(client,document):
    """Merge equivalent hypotheses, never an exposure and its distinct outcome."""
    registry=_registry(document)
    if len(registry.candidates)<=8:
        return
    by_id={candidate.risk_id:candidate for candidate in registry.candidates}
    try:
        response=await client.generate({'task':'consolidate_integrated_risk_candidates',
            'candidates':[{'riskId':row.risk_id,'name':row.name,'category':row.category,
                'certainty':row.certainty,'relationship':row.relationship,
                'sourceModelDiseases':[next(f.name for f in document.facts if f.fact_id==fid)
                    for fid in row.source_model_fact_ids], 'mayBeSupportingTheme':_generic_topic(row)}
                for row in by_id.values()],
            'rules':[
                '这是内部同义去重，仅合并输入候选，不能新增疾病、病因、事实或确定性。每个输入riskId恰好出现一次，或属于一个group，或属于一个supportingTopic。',
                '同一便秘、反酸/反流、血脂问题的不同叙述可合并；暴露与潜在结局是不同疾病，不合并血脂异常和ASCVD、不合并肥胖和脂肪肝、胃癌和反酸、肝硬化和MASLD、精神分裂症和抑郁。',
                'group的canonicalRiskId必须从本组memberRiskIds选择，采用最具体清晰的现有名称，不创造新名；具体不同疾病各自保留单人组。',
                'mayBeSupportingTheme=true的笼统菌群失调、营养/功能方向不是多个独立疾病，可放supportingTopics并绑定与其确实有关的已有具体风险，也可targets=[]留在功能/营养章节。不得用泛化方向取代具体疾病。',
                '每项附具体理由。只输出成员riskId/目标riskId，不复写候选全文、factIds或关系数组。'],
            'outputSchema':{'groups':[{'canonicalRiskId':'成员之一','memberRiskIds':['实际ID'],'reason':'同一问题的理由'}],
                'supportingTopics':[{'riskId':'可作支持主题的实际ID','targetRiskIds':['已有具体风险ID'],
                    'reason':'对应关系或仅留功能章节的理由'}]}})
        groups=response.get('groups',[]) if isinstance(response,dict) else []
        themes=response.get('supportingTopics',[]) if isinstance(response,dict) else []
        groups=groups if isinstance(groups,list) else []
        themes=themes if isinstance(themes,list) else []
        accepted_groups=[];assigned=set();invalid=set()
        for group in groups:
            if _candidate_group_valid(group,by_id,assigned):
                accepted_groups.append(group);assigned.update(group['memberRiskIds'])
            elif isinstance(group,dict) and isinstance(group.get('memberRiskIds'),list):
                invalid.update(identifier for identifier in group['memberRiskIds']
                    if isinstance(identifier,str) and identifier in by_id and identifier not in assigned)
        if invalid:
            try:
                repair=await client.generate({'task':'repair_integrated_risk_candidate_groups',
                    'candidates':[{'riskId':identifier,'name':by_id[identifier].name,
                        'diseaseId':by_id[identifier].disease_id,'category':by_id[identifier].category,
                        'mayBeSupportingTheme':_generic_topic(by_id[identifier])} for identifier in sorted(invalid)],
                    'existingTargetRiskIds':sorted(assigned),
                    'rules':['只处理这批被拒绝组的真实成员一次，不重做其它已完成组，不新增名称/ID/疾病或医学关联。',
                        '不同独立疾病必须各自保留单人组，血脂异常不与ASCVD合并，抑郁症不与笼统情绪主题合并，过敏不与免疫功能方向合并。',
                        '仅明确同一疾病的描述合并；不能确定同义则独立保留。mayBeSupportingTheme的纯功能目录放supportingTopics，可targets=[]留在营养/通路章节。'],
                    'outputSchema':{'groups':[{'canonicalRiskId':'成员之一','memberRiskIds':['本批实际ID'],
                        'reason':'同一问题或安全独立保留的理由'}],
                        'supportingTopics':[{'riskId':'本批纯主题ID','targetRiskIds':[],
                            'reason':'只作功能资料归组的理由'}]}})
                fixed_groups=repair.get('groups',[]) if isinstance(repair,dict) else []
                fixed_themes=repair.get('supportingTopics',[]) if isinstance(repair,dict) else []
                groups=accepted_groups+[g for g in fixed_groups if isinstance(g,dict)
                    and isinstance(g.get('memberRiskIds'),list) and all(isinstance(identifier,str)
                        and identifier in invalid for identifier in g['memberRiskIds'])]
                themes=[*themes,*[t for t in fixed_themes if isinstance(t,dict) and t.get('riskId') in invalid]]
            except Exception:
                groups=accepted_groups
        seen=set();result=[];canonical={}
        for group in groups:
            members=group.get('memberRiskIds',[]) if isinstance(group,dict) else []
            target=group.get('canonicalRiskId') if isinstance(group,dict) else None
            if not _candidate_group_valid(group,by_id,seen):
                continue
            from .evaluation_catalog import fixed_disease_code
            codes={by_id[identifier].disease_id for identifier in members
                if fixed_disease_code(by_id[identifier].disease_id)}
            lipid='hyperlipidemia' in codes
            if len(codes)>1 or (lipid and any(re.search(r'心血管|动脉粥样硬化|ASCVD',by_id[identifier].name,re.I)
                    and by_id[identifier].disease_id!='hyperlipidemia' for identifier in members)):
                continue
            if all(_generic_topic(by_id[identifier]) for identifier in members):
                themes.extend({'riskId':identifier,'targetRiskIds':[],
                    'reason':'保留为菌群或功能支持主题，不额外生成独立疾病。'} for identifier in members)
                continue
            specific=[by_id[identifier] for identifier in members if fixed_disease_code(by_id[identifier].disease_id)
                and by_id[identifier].source_model_fact_ids]
            if len(specific)==1 and all(by_id[identifier].certainty=='potential_risk' for identifier in members):
                # Preserve the concrete disease question when all members
                # were synonyms for a potential risk, not a current disorder.
                target=specific[0].risk_id
            adopted=by_id[target].model_copy(deep=True)
            for identifier in members:
                if identifier!=target:
                    _merge_candidate(adopted,by_id[identifier])
                canonical[identifier]=adopted
            result.append(adopted);seen.update(members)
        for theme in themes:
            identifier=theme.get('riskId') if isinstance(theme,dict) else None
            targets=theme.get('targetRiskIds',[]) if isinstance(theme,dict) else []
            if (identifier not in by_id or identifier in seen or not _generic_topic(by_id[identifier])
                    or not isinstance(targets,list) or not set(targets)<=canonical.keys()
                    or len(str(theme.get('reason','')))<8):
                continue
            for target in set(targets):
                adopted=canonical[target];source=by_id[identifier]
                ids=[*source.support_fact_ids,*source.opposing_fact_ids,*source.background_fact_ids,*source.source_model_fact_ids]
                bound=set(adopted.support_fact_ids+adopted.opposing_fact_ids+adopted.background_fact_ids)
                adopted.background_fact_ids.extend(fid for fid in ids if fid not in bound)
                adopted.relations.extend(relation for relation in source.relations if relation not in adopted.relations)
            registry.coverage_dispositions.append({'topicRiskId':identifier,'status':'supporting_theme',**theme})
            seen.add(identifier)
        if seen!=by_id.keys():
            # A refused merge is safely resolved by preserving each actual
            # disease. It is not a reason to discard them or force a merge.
            for identifier in by_id:
                if identifier in seen:
                    continue
                if _generic_topic(by_id[identifier]):
                    registry.coverage_dispositions.append({'topicRiskId':identifier,'status':'supporting_theme',
                        'targetRiskIds':[],'reason':'纯菌群/功能目录保留对应章节，不作为额外独立疾病。'})
                else:
                    result.append(by_id[identifier])
                seen.add(identifier)
        registry.candidates=result
        registry.incomplete=[reason for reason in registry.incomplete
            if reason!='RISK_CANDIDATE_DEDUPLICATION_INCOMPLETE']
    except Exception:
        registry.incomplete.append('RISK_CANDIDATE_DEDUPLICATION_INCOMPLETE')


def candidate_relations(candidate, facts):
    """Independent duties, never one paper proving the whole patient story."""
    if candidate.relations:
        return candidate.relations
    groups = {}
    model_ids = set(candidate.source_model_fact_ids)
    for fid in dict.fromkeys([*candidate.support_fact_ids, *candidate.background_fact_ids, *model_ids]):
        fact = facts.get(fid)
        if fact is None:
            continue
        kind = ('source_model' if fid in model_ids else 'clinical' if fact.kind == 'clinical'
            else 'behavior' if fact.kind == 'self_reported' else 'microbiome' if fact.kind == 'microbe' else 'function')
        groups.setdefault(kind, []).append(fid)
    return [RiskRelation(relation_id=candidate.risk_id + ':' + kind, kind=kind,
        fact_ids=ids, exposure_terms=list(dict.fromkeys(facts[fid].name for fid in ids)),
        outcome_terms=list(dict.fromkeys([_endpoint_name(candidate.name),*candidate.search_terms])),
        question='核查' + '、'.join(facts[fid].name for fid in ids) + '与' + candidate.name + '的对应关系')
        for kind, ids in groups.items()]


def risk_source_signals(document):
    """Actual source duties; a signal is not a new disease or risk verdict."""
    from .evaluation_catalog import fixed_disease_code
    from .evaluation_risk_source_links import source_association_view
    declared_associations = {fid for row in source_association_view(document) for fid in row['factIds']}
    groups = {}
    domains = (
        ('神经递质代谢', r'神经递质|多巴胺|氨基丁酸|血清素'),
        ('维生素合成', r'维生素|叶酸'),
        ('氨基酸代谢', r'氨基酸|精氨酸|半胱氨酸|谷氨酸|甘氨酸|组氨酸|异亮氨酸|亮氨酸|赖氨酸|甲硫氨酸|苯丙氨酸|脯氨酸|苏氨酸|色氨酸|酪氨酸|缬氨酸'),
        ('短链脂肪酸', r'短链脂肪酸|丁酸|丙酸|乙酸'),
        ('脂质与胆汁酸代谢', r'脂质|脂肪酸|胆汁酸|亚油酸|花生四烯酸|醚酯|甘油酯|磷脂|鞘脂|类固醇'),
    )
    # Build without relying on measured numbers or the word 'low' in an ID.
    for fact in document.facts:
        name = fact.name
        if name.endswith('（来源疾病模型指数）'):
            label = ('source_model', name.removesuffix('（来源疾病模型指数）'))
        elif fact.kind == 'context' and str(fact.value).strip().casefold() in {
                '检出','阳性','异常','检出相关菌群异常','detected','positive','abnormal'} and (
                fact.fact_id in declared_associations or fixed_disease_code(name)):
            label = ('source_association', name)
        elif fact.kind == 'microbe' and (fact.abnormal or re.search(r'偏高|偏低|超标|缺乏|↑|↓',fact.status or '')):
            label = ('abnormal_taxa', '相关菌群实测偏离' if re.search(r'listeria|streptococcus',name,re.I)
                else '乳酸菌群的实测偏离' if re.search(r'lactobac|lacticas|limosilacto|lactococcus',name,re.I)
                else '高阶菌群组成偏移' if re.search(r'菌门|菌纲|菌目|菌科',name)
                else '发酵及代谢相关菌群实测偏离')
        elif fact.kind in {'function','context'}:
            group = next((title for title,pattern in domains if re.search(pattern,name)),None)
            label = ('functional_domain',group) if group else None
        else:
            label = None
        if label:
            groups.setdefault(label, []).append(fact)
    output=[]
    for (kind,title), members in groups.items():
        if kind=='functional_domain' and not any(f.value=='异常' or re.search(r'[↑↓]',f.value)
                or re.search(r'偏高|偏低|异常',f.status or '') for f in members):
            continue
        links=_source_link_rows(document,[fact.fact_id for fact in members])
        output.append({'signalId':kind+':'+_digest(title)[:16], 'kind':kind,'name':title,
            'factIds':[f.fact_id for f in members], 'facts':[_fact_view(f) for f in members],
            'sourcePanelLinks':links,'memberFactIds':list(dict.fromkeys(edge['memberFactId'] for edge in links))})
    return output


async def discover_risk_candidates(client, document, *, subject=None):
    """Propose hypotheses from the complete facts, never source diagnoses."""
    registry, known = _registry(document), _facts(document)
    subject = _subject_view(subject)
    digest = _digest({'facts': _source_digest(document), 'subject': subject})
    if registry.source_digest == digest and registry.candidates:
        return registry
    registry.source_digest = digest
    registry.subject_snapshot = subject
    registry.candidates = []
    registry.assessments = []
    registry._proofs.clear()
    if not known or client is None:
        if known:
            registry.incomplete.append('RISK_DISCOVERY_UNAVAILABLE')
        return registry
    payload = {
        'task': 'discover_integrated_health_risks',
        'subject': subject or {},
        'facts': [_fact_view(fact) for fact in known.values()],
        'existingProblems': [problem.model_dump(mode='json', by_alias=True)
            for problem in document.problems],
        'rules': [
            '从全部输入事实共同发现具体健康问题及潜在疾病风险，不预设菌群、体检、问卷的固定主次。',
            '每个候选绑定真实factId；异常指标、行为、菌群功能可共同提出待文献验证的风险关系，不限于原报告病名或固定疾病目录。',
            'facts的value是逐字引文用原始值；如有displayValue只用于正文展示。factQuotes必须取value，正文不要复制浮点尾差。',
            '这只是内部检索假设，不是确诊；confirmed仅限真实明确诊断，其他标current_problem、self_reported或potential_risk。',
            '尚无明确诊断、也未核实完整适用分类条件的症状模式，应提出disease_risk/potential_risk的可能疾病假设；不要先标current_problem再要求其证明全部确诊条件。真实原始症状另以symptom保留，可能疾病不是症状改名或已患病。',
            '完整保留临床正常、否认、相反结果供综合，不把正常直接当排除潜在风险。家族疾病不当本人的疾病。',
            '检验参考正常只说明原报告判定，不表示未来疾病风险已被排除。referenceInterpretation标记的连续数值要结合相关行为、家族史和其他检查判断是否值得提出未来风险关系；必要时查适用指南阈值或连续风险研究。不得因sourceStatus正常直接全部放opposingFactIds；尚未检索时为中性background，不新增诊断或改原分级。',
            '不得因一种疾病相关就穷举无个人线索的疾病。保留已有疾病及实际重要问题，去除同病同义名重复；不同疾病可共享事实。',
            'category只可' + '/'.join(_CATEGORY_CODES) + '。autoimmune表示广义免疫维度；mental包含精神与睡眠相关问题；'
            '肿瘤、结节和息肉按其器官或系统归入这六类，保留原疾病名称与确定性，不能由异常指标推断已患肿瘤。',
            'source_model_fact_ids仅来源模型实际事实，不能把原模型名或指数当成已确诊。配型方案、供者资料不是本人疾病或检测异常。',
            'sourceRole=microbiome_association_screen表示相关菌群筛查的检出/未检出，不是本人有该症状、疾病或感染。source_disease_model才是来源疾病指数；实测总分/免疫/营养评分只属measured_function，不塞sourceModelFactIds。',
            'relationship写需要证据验证的具体“异常/行为/菌群功能→风险”关系。searchTerms只列疾病/健康问题本身的中英文同义术语；exposureTerms只列实际支持事实中的异常指标/行为/菌群对象及其准确中英同义名，不能混成完整查询。不得只写疾病定义。',
            '列出所有有实际线索的不同风险，不按首页8项截断。support/opposing/background事实按用途区分。',
        ],
        'outputSchema': {'candidates': [{
            'name': '具体健康问题或疾病风险', 'category': '已定义的疾病与健康维度代码',
            'certainty': 'potential_risk', 'entityKind': 'disease|disease_risk|symptom|indicator_problem',
            'supportFactIds': ['真实ID'],
            'opposingFactIds': [], 'backgroundFactIds': [], 'sourceModelFactIds': [],
            'relationship': '待检索的具体关系', 'searchTerms': ['疾病中文', 'Disease English'],
            'exposureTerms': ['来源指标/行为/菌群对象', 'Accurate English alias'], 'priority': 2,
        }]},
    }
    payload['rules'].extend([
        'priority只表示内部执行调度：1紧急优先、2一般、3常规，不是风险等级或排序名次。候选名称只写健康问题/疾病风险，指标放依据，不写管理方向。',
        '名称必须是具体疾病或明确的临床健康问题；其他组分、多病共存、组合风险或某某方向不另造疾病条目。'
        '这些研究结局或综合负担仍可作为原具体问题的关系依据；若原文真正指向另一具体疾病，'
        '提出那个疾病的独立潜在风险并逐条验证，不删除资料或把未知结局强换成固定病名。'
        '血脂异常本身是可用的临床问题，不因需要更具体名称就改成已患ASCVD；'
        '高胆固醇/高甘油三酯/混合型等亚型须有相应真实指标、条件及适用指南支持，否则保留准确上位名称。',
        'entityKind逐项区分：disease为有真实诊断或适用诊断条件的疾病；disease_risk为具体疾病的潜在风险；symptom只为本人主诉；indicator_problem仅为指标/状态。症状与指标作为事实依据保留，但不能充当具体疾病风险的全部召回。对这些真实线索另提出有证据可检索的疾病潜在风险，不将反酸或便秘症状硬改为GERD或功能性便秘确诊。',
        'relations把临床指标、行为、菌群、功能、原模型各条关系独立列出；不要求一篇论文同时证明全部资料，也不强制临床风险都由菌群证明。',
        '每条relation明确exposureTerms与outcomeTerms：前者是实际输入对象，后者是拟验证的真实疾病/健康结局准确中英文别名。已有异常本身与其可能影响的另一个疾病结局分开，不能把当前暴露词同时充当全部检索结局；searchTerms必须与该候选拟评结局一致。不要凭固定疾病名单补风险。',
        '关系暴露用源事实的实际医学对象，可含原参考范围内但有适用未来风险意义的连续指标；不得仅因参考正常省略其待证关系。家族史亲属角色、菌群/人体标本、预测功能与实测浓度分别保留。每条exposureTerms是一个对象的准确同义组，不写个人数值或多个对象的整句，也不把阳性筛查栏目名称本身当新的病因；可用真实panel成员或功能对象检索。',
        '异常菌和营养功能必须用真实成员factId参与候选关系，不仅引用总分。源模型与阳性关联属于实际线索，逐项联合评判；低关注可以不进首页，但不能完全不审。',
    ])
    payload['outputSchema']['candidates'][0]['relations'] = [{
        'relationId': '本候选内稳定关系标识', 'kind': 'clinical|behavior|microbiome|function|source_model',
        'factIds': ['该关系实际事实ID'], 'exposureTerms': ['准确对象术语'],
        'outcomeTerms':['该关系真实结局及准确英文别名'], 'question': '只验证一条具体关系'}]
    clinical = [fact for fact in known.values() if fact.kind in {'clinical', 'self_reported'}
        or fact.fact_id.startswith('profile:')]
    microbial = [fact for fact in known.values() if fact not in clinical]
    tasks = []
    if clinical:
        tasks.append({**payload, 'domainFocus': '临床与行为',
            'facts': [_fact_view(fact) for fact in clinical],
            'rules': [*payload['rules'], '本任务先完整处理临床、问卷与行为已有问题及潜在风险，菌群任务另行补充；不为缺菌群停下。',
                '当前异常状态与其可能指向的具体疾病结局承担不同职责。对本批真实异常临床指标，既识别当前明确健康问题，也提出适合该实际暴露检索的具体潜在疾病假设；不能因为已有血脂异常等状态候选就认为其疾病结局已覆盖。每个假设独立绑定真实指标、关系与potential_risk确定性，后续逐项检索和独立审核，不预先宣称患病、不凑无关疾病。',
                '当前血脂问题的规范疾病术语和亚型按实际诊断记录或适用指南条件判断；高脂血症等上位术语也须与真实类型匹配。参考范围外不自动满足某亚型，不能用一项LDL升高证明混合型，也不能将血脂异常换称已患动脉粥样硬化性心血管疾病。风险结局须另建候选、独立证据与评分。']})
    if microbial:
        signals = risk_source_signals(document)
        signal_ids = {fid for signal in signals for fid in signal['factIds']}
        shared = [fact for fact in microbial if fact.kind!='microbe' and fact.fact_id not in signal_ids]
        batches = list(_signal_batches(signals,known))
        # Normal/negative observations are still read, and can qualify the
        # hypotheses. They are not copied into every signal four times.
        residual = [fact for fact in microbial if fact.fact_id not in signal_ids
            and fact not in shared]
        for chunk in _fact_chunks(residual):
            batches.append([{'signalId':'source_background:'+_digest([f.fact_id for f in chunk])[:16],
                'kind':'source_background','name':'其余实测菌群及正常/阴性背景',
                'factIds':[f.fact_id for f in chunk], 'facts':[_fact_view(f) for f in chunk]}])
        if not batches:
            batches=[[]]
        for batch in batches:
            batch_ids = list(dict.fromkeys(fid for signal in batch for fid in signal['factIds']))
            tasks.append({**payload, 'domainFocus':'菌群、功能与源疾病模型',
                'facts':[_fact_view(known[fid]) for fid in batch_ids],
                'personalContextFacts':[_fact_view(fact) for fact in clinical],
                'sourceContextFacts':[_fact_view(fact) for fact in shared],
                'sourceSignals':[{key:value for key,value in signal.items() if key!='facts'} for signal in batch],
                'rules':[*payload['rules'],
                    '本任务仅处理当前至多4个健康主题，从菌群/功能异常和源模型出发，临床资料用于个体相关性，正常/相反菌群功能参与判断。其他主题由独立批次覆盖。',
                    '每批最多4个候选、每候选最多4条独立关系；不重复整份临床候选。每relation一个独立实际暴露及准确中英同义词，饮水/膳食/运动/睡眠须分别列，不以多个并列病因强求一篇文献同时证明。',
                    '只选能代表医学关系的实际成员factId，不把属种上下级、镜像卡片重复当独立证据。正常/阴性背景如无独立风险可返回空candidates。']})
            tasks[-1]['rules'].append('最多4个不是必须4个：正常C背景允许[]。仅补当前sourceSignals成员实际参与的新C关联，不重新列个人便秘/反流/血脂等全局临床候选；个人临床资料只用于判断当前C线索的重要性。')
    responses = await _bounded_generate(client,tasks,facts=known)
    for task,response in zip(tasks,responses):
        if not await _admit_discovery_response(client,response,task,document,known,registry):
            registry.incomplete.append('RISK_DISCOVERY_UNAVAILABLE')
    await _deduplicate_candidate_topics(client,document)
    await _complete_candidate_coverage(client, document, subject)
    await _complete_current_problem_coverage(client, document, subject)
    await _repair_observation_disease_hypotheses(client, document, subject)
    for candidate in registry.candidates:
        _attach_source_members(document,candidate)
    registry.incomplete = list(dict.fromkeys(registry.incomplete))
    return registry


async def _repair_observation_disease_hypotheses(client, document, subject):
    """One bounded recall of disease outcomes missed behind source states."""
    registry, facts = _registry(document), _facts(document)
    diseases = [row for row in registry.candidates if _candidate_entity_scope(row) == 'disease']
    missing = []
    duties = {}
    for row in registry.candidates:
        if _candidate_entity_scope(row) != 'observation':
            continue
        shared = [other for other in diseases if set(row.support_fact_ids) & set(other.support_fact_ids)]
        item = {'name': row.name, 'certainty': row.certainty, 'factIds': row.support_fact_ids}
        roles = []
        if not any(_covers_reviewable_classification(other, item, facts) for other in shared):
            roles.append('clinical_classification')
        if not shared:
            roles.append('future_disease_outcome')
        if roles:
            missing.append(row)
            duties[row.risk_id] = roles
    if not missing or client is None:
        return
    supplied = {fid for row in missing for fid in [*row.support_fact_ids, *row.background_fact_ids] if fid in facts}
    try:
        response = await _candidate_generate(client, {'task': 'repair_specific_disease_endpoint_hypotheses',
            'subject': subject, 'observations': [{**row.model_dump(mode='json', by_alias=True),
                'assessmentDuties': duties[row.risk_id]} for row in missing],
            'facts': [_fact_view(facts[fid]) for fid in sorted(supplied)],
            'existingDiseaseHypotheses': [row.model_dump(mode='json', by_alias=True) for row in diseases],
            'rules': ['每个观察的assessmentDuties区分两项职责：clinical_classification评判现有症状模式/临床测量是否值得提出某规范疾病分类假设；future_disease_outcome评判其可能影响的远期具体疾病。共享同一fact已用于其他远期疾病，不代表当前分类职责完成。仅在真实资料具有医学可检索关系时提出；不逐数值硬造疾病。'
                '这些真实主诉/指标仍需对应疾病分类或风险检索问题。'
                '仅一次提出与实际资料有医学可检索关系的具体疾病潜在风险，不复制观察名称作疾病，也不把反酸或便秘直接确诊为GERD或功能性便秘。',
                '临床分类假设可采用entityKind=disease、certainty=current_problem，或资料不足以成立当前病时采用disease_risk/potential_risk；后续必须适用指南条件及个人分类独审，不在此确诊。远期结局用disease_risk/potential_risk。实际factId、独立关系和assessmentDuty分别绑定。'
                '未核完整分类条件的症状模式优先提出potential_risk；已有同名current_problem候选并不代表个人分类已通过，不得仅因名字已存在而跳过谨慎疾病可能性。'
                '这是待证假设，后续照常检索、逐句来源/原文适用和风险独审；此处不生成分数、诊断结论或新患者事实。'
                '没有合理具体假设返回空，并保留内部缺口，不穷举疾病或把指标状态换词当病。'],
            'outputSchema': {'candidates': [{'name': '具体疾病的潜在风险', 'category': '已定义的疾病与健康维度代码',
                'entityKind': 'disease|disease_risk', 'certainty': 'current_problem|potential_risk',
                'assessmentDuty': 'clinical_classification|future_disease_outcome',
                'supportFactIds': ['本次真实ID'], 'opposingFactIds': [], 'backgroundFactIds': [],
                'sourceModelFactIds': [], 'relationship': '实际暴露与具体疾病待验证关系',
                'searchTerms': ['疾病中文与英文'], 'exposureTerms': ['实际暴露准确同义词'],
                'relations': [{'relationId': '同候选关系ID', 'kind': 'clinical|behavior', 'factIds': ['真实ID'],
                    'exposureTerms': ['一个实际暴露'], 'outcomeTerms': ['具体疾病'], 'question': '待核关系'}]}]}}, facts)
        proposed = [row for row in response.get('candidates', []) if isinstance(row, dict)
            and (row.get('entityKind'), row.get('certainty')) in {('disease_risk', 'potential_risk'),
                ('disease', 'current_problem')}]
        classification_names = {row.get('name') for row in proposed if row.get('assessmentDuty') == 'clinical_classification'}
        proposed = [{key: value for key, value in row.items() if key != 'assessmentDuty'} for row in proposed]
        _admit_candidates(proposed, document, facts, registry, mark_invalid=False)
    except Exception:
        classification_names = set()
    for row in missing:
        covered = any(_candidate_entity_scope(other) == 'disease'
            and set(row.support_fact_ids) & set(other.support_fact_ids) for other in registry.candidates)
        if not covered:
            registry.gaps.append({'riskId': row.risk_id, 'missingRole': 'disease_hypothesis',
                'reason': 'observation_has_no_specific_disease_outcome_hypothesis',
                'supportFactIds': list(row.support_fact_ids)})
            registry.incomplete.append('DISEASE_ENDPOINT_INCOMPLETE')
        if 'clinical_classification' in duties[row.risk_id] and not any(
                _candidate_entity_scope(other) == 'disease' and other.name in classification_names
                and set(row.support_fact_ids) & set(other.support_fact_ids)
                for other in registry.candidates):
            registry.gaps.append({'riskId': row.risk_id, 'missingRole': 'clinical_classification_hypothesis',
                'reason': 'current_observation_has_no_independently_reviewable_classification_hypothesis',
                'supportFactIds': list(row.support_fact_ids)})
            registry.incomplete.append('DISEASE_ENDPOINT_INCOMPLETE')


def _current_observation_duties(document):
    from .evaluation_risk_source_links import source_current_problem_view
    facts = _facts(document)
    result = source_current_problem_view(document)
    included = {fid for item in result for fid in item['factIds']}
    for problem in document.problems:
        if problem.certainty not in {'confirmed', 'self_reported'}:
            continue
        ids = list(dict.fromkeys(source.fact_id for source in problem.sources
            if source.fact_id in facts and source.quote and source.quote in facts[source.fact_id].value))
        if any(set(ids) & set(item['factIds']) and any(
                _key(facts[fid].name) and _key(facts[fid].name) in _key(problem.title)
                and _key(facts[fid].name) in _key(item.get('value') or '') for fid in ids)
                for item in result):
            continue  # An adopted symptom facet already represents this problem mirror.
        if ids and not any(item['name'] == problem.title and set(item['factIds']) == set(ids)
                for item in result):
            result.append({'observationId': problem.problem_id, 'name': problem.title,
                'certainty': problem.certainty, 'factIds': ids,
                'sourceDiagnosis': problem.certainty == 'confirmed'})
            included.update(ids)
    for fact in facts.values():
        if fact.fact_id not in included and _clinical_observed_abnormal(fact):
            result.append({'observationId': fact.fact_id, 'name': fact.name,
                'certainty': 'source_finding', 'factIds': [fact.fact_id], 'sourceDiagnosis': False})
    return result


def _covers_current_observation(candidate, item, facts):
    if candidate.certainty not in {'confirmed', 'current_problem', 'self_reported'}:
        return False
    ids = set(item['factIds'])
    shared = ids & set(candidate.support_fact_ids)
    if not shared:
        return False
    if item.get('certainty') != 'self_reported':
        if not ids <= set(candidate.support_fact_ids):
            return False
        from .evaluation_disease_identity import same_source_disease
        if same_source_disease(item['name'], re.sub(r'(?:相关)?风险$', '', candidate.name)):
            return True
        observations = [RiskClaim(claim_id='current:'+fid, role='source_fact',
            text=facts[fid].name+' '+facts[fid].value,
            fact_quotes={fid:facts[fid].value}) for fid in ids if fid in facts]
        return _same_observed_problem(candidate, observations, facts)
    # One questionnaire answer can contain constipation and reflux. A
    # shared question locator does not cover both personal symptoms, while
    # a named adopted symptom fact can represent its mirrored occurrences.
    name = re.sub(r'[（(]自述[）)]', '', item.get('name') or '')
    terms = [re.sub(r'(?:相关)?症状$', '', part) for part in re.split(r'[/／]', name)]
    return any(len(_key(term)) >= 2 and _key(term) in _key(candidate.name) for term in terms)


def _covers_reviewable_classification(candidate, item, facts):
    """A pending current-disease guess is not completed classification duty."""
    if not _covers_current_observation(candidate, item, facts):
        return False
    if item.get('sourceDiagnosis') is True:
        return True
    return (getattr(candidate, '_current_classification_scope', None)
        == _digest(candidate.model_dump()))


async def _complete_current_problem_coverage(client, document, subject):
    """A current abnormality must not vanish into another future disease.

    This completes adopted health states, not a diagnosis from a lone marker.
    The ordinary observation/claim review still governs every eventual row.
    """
    registry, facts = _registry(document), _facts(document)
    duties = _current_observation_duties(document)
    def represented(item):
        return any(_covers_current_observation(candidate, item, facts) for candidate in registry.candidates)
    missing = [item for item in duties if not represented(item)]
    if not missing:
        return
    tasks = []
    for offset in range(0, len(missing), 8):
        batch = missing[offset:offset + 8]
        ids = list(dict.fromkeys(fid for item in batch for fid in item['factIds']))
        tasks.append({'task': 'complete_observed_current_health_problems', 'subject': subject,
            'currentObservations': batch, 'facts': [_fact_view(facts[fid]) for fid in ids],
            'existingCandidates': [candidate.model_dump(mode='json', by_alias=True)
                for candidate in registry.candidates],
            'rules': ['只补已提供的确诊病史、当前客观异常健康状态和实际主诉，不新造疾病诊断或机制。',
                '当前异常本身与它可能影响的其他疾病是两项职责；把血脂数值用于心血管潜在风险，不能据此删掉已经存在的血脂异常状态。',
                '同系统同一状态的实际指标合并为一个专业健康问题，指标仅作依据；不逐项把每个数值建成疾病。',
                '没有明确诊断时采用客观健康状态，certainty=current_problem；实际自述用self_reported，不能把反酸等单个症状写成已诊断疾病。',
                'sourceDiagnosis=false不授权确诊具体病因。实际主诉entityKind=symptom，检验/体征状态entityKind=indicator_problem；只有真实已明确的疾病诊断可为disease。原始数值/单位/原级不变，不用无关阳性菌群给当前疾病背书。',
                '只返回这些观察的current_problem/self_reported/已有confirmed候选。已有同一当前候选可返回同名完整更新，真实支持ID必须保留。',
                '每个observationId给一个disposition，采用candidateNames指向当前状态；只有明确为同一实际观察的镜像才可duplicate，不能因已进入另一潜在风险称覆盖。'],
            'outputSchema': {'candidates': [{'name': '当前已存在的健康问题，非数值、管理方向或未诊断病因',
                    'category': '|'.join(_CATEGORY_CODES),
                    'certainty': 'current_problem|self_reported|已有明确诊断的confirmed',
                    'entityKind': 'symptom|indicator_problem|有实际明确诊断的disease',
                    'supportFactIds': ['实际支持ID'], 'opposingFactIds': [], 'backgroundFactIds': [],
                    'sourceModelFactIds': [], 'relationship': '该当前状态的实际观察依据',
                    'searchTerms': ['本状态准确中英文别名'], 'exposureTerms': ['实际观察对象'],
                    'relations': [], 'priority': 2}],
                'dispositions': [{'observationId': '原ID', 'candidateNames': ['当前状态名称'],
                    'status': 'covered|duplicate', 'factIds': ['实际ID'], 'reason': '具体采用理由'}]}})
    responses = await _bounded_generate(client, tasks, facts=facts)
    for task, response in zip(tasks, responses):
        rows = response.get('candidates', []) if isinstance(response, dict) else []
        rows = [row for row in rows if isinstance(row, dict) and row.get('certainty') in
            {'confirmed', 'current_problem', 'self_reported'}]
        await _admit_discovery_response(client, {**response, 'candidates': rows} if isinstance(response, dict)
            else {}, task, document, facts, registry)
    # A failed completion stays visible internally instead of certifying
    # exposure-only coverage as a successfully evaluated current problem.
    registry.coverage_dispositions = [row for row in registry.coverage_dispositions
        if row.get('kind') != 'current_observation']
    for item in duties:
        covered = represented(item)
        matches = [candidate.risk_id for candidate in registry.candidates
            if _covers_current_observation(candidate, item, facts)]
        registry.coverage_dispositions.append({'kind': 'current_observation',
            'observationId': item['observationId'], 'status': 'covered' if covered else 'needs_current_state',
            'factIds': item['factIds'], 'riskIds': matches})
        if not covered:
            registry.incomplete.append('RISK_CURRENT_PROBLEM_COVERAGE_INCOMPLETE')


async def _complete_source_disease_hypotheses(client,document,signals,subject):
    """Source disease names fix the question; the model enriches its premises."""
    from .evaluation_catalog import disease_category,FIXED_DISEASE_ALIASES,fixed_disease_code
    registry,facts=_registry(document),_facts(document)
    observations=[signal for signal in signals if non_disease_endpoint_kind(signal['name'])]
    source_observations=[{'signalId':signal['signalId'],'status':'source_observation',
        'factIds':list(signal['factIds']),'riskIds':[],
        'reason':'source_symptom_or_measurement_is_not_a_personal_disease',
        'sourceObservation':signal['name']} for signal in observations]
    signals=[signal for signal in signals if signal not in observations]
    tasks=[];scaffolds={}
    for signal in signals:
        identity=_risk_identity(signal['name'])
        candidate=next((row for row in registry.candidates if row.disease_id==identity
            and _candidate_entity_scope(row) == 'disease'),None)
        if candidate is None:
            candidate=RiskCandidate(risk_id='risk-'+_digest(identity)[:20],disease_id=identity,
                name=signal['name']+'相关风险',category=disease_category(signal['name'],code=identity),
                certainty='potential_risk',entity_kind='disease_risk',support_fact_ids=list(signal['factIds']),
                source_model_fact_ids=list(signal['factIds']) if signal['kind']=='source_model' else [],
                relationship='结合本人全部实际资料评估'+signal['name']+'相关风险及其个人依据',
                search_terms=list(dict.fromkeys([signal['name'],*FIXED_DISEASE_ALIASES.get(identity,())])))
            registry.candidates.append(candidate)
        if signal['kind']=='source_model':
            candidate.source_model_fact_ids.extend(fid for fid in signal['factIds'] if fid not in candidate.source_model_fact_ids)
        bound=set(candidate.support_fact_ids+candidate.opposing_fact_ids+candidate.background_fact_ids)
        candidate.background_fact_ids.extend(fid for fid in signal['factIds'] if fid not in bound)
        scaffolds[signal['signalId']]=candidate
    context=[_fact_view(fact) for fact in facts.values() if fact.kind!='microbe' or fact.abnormal
        or re.search(r'偏高|偏低|超标|缺乏',fact.status or '')]
    for offset in range(0,len(signals),4):
        batch=signals[offset:offset+4]
        tasks.append({'task':'complete_integrated_source_disease_hypotheses','subject':subject,
            'uncoveredSignals':[{**signal,'fixedRiskId':scaffolds[signal['signalId']].risk_id,
                'fixedDiseaseId':scaffolds[signal['signalId']].disease_id,
                'fixedName':scaffolds[signal['signalId']].name} for signal in batch],
            'facts':context,'rules':[
                '每个来源明确病名已经固定为独立内部评估假设，不是确诊，不强制高风险；不得改fixedRiskId/fixedDiseaseId/fixedName或用糖代谢/情绪精神/骨神经等泛方向替换本疾病。',
                '逐病结合真实菌群/功能、本人症状、行为、体检与正常/相反资料补支持、相反、背景事实及独立待验证关系。原始指数只作参考，不直接乘100、平均或变成患病概率。',
                'sourceRole=microbiome_association_screen是相关菌群筛查而非本人症状/诊断；不得把菌群合成评分改成人体浓度、单菌检出改为感染确诊。',
                '每个输入signalId恰好返回一项。status仅为初步判断：assess、normal_after_review、not_independent都必须继续保留本病独立文献核查与指数评估；不能因无症状/原低分跳过，也不因检出相关菌群就确诊或强制高风险。',
                '每项都列真实factIds及supportFactIds/opposingFactIds/backgroundFactIds。relations每条只验证一实际暴露与此固定疾病的关系，中英文exposureTerms准确列对象，outcomeTerms准确列此固定疾病的中英文同义名，不用评估/管理/线索等工作流名充当结局；不要求论文含本人数字。',
                'source_model仅指原模型值，不需论文再证明其分数。需要外部关系时以真实菌群/功能/临床/行为为暴露，不把原模型分值本身当医学机制。',
                '只输出该固定疾病的补充元数据，每项最多4条关系；不用另写完整candidates或再给泛化candidateNames。'],
            'outputSchema':{'items':[{'signalId':'输入ID','riskId':'输入fixedRiskId',
                'category':'|'.join(_CATEGORY_CODES)+'；已知疾病保持输入归类',
                'status':'assess|normal_after_review|not_independent','reason':'本病的个人综合裁定理由',
                'factIds':['实际ID'],'supportFactIds':['实际ID'],'opposingFactIds':[],
                'backgroundFactIds':[],'exposureTerms':['实际暴露中英文同义词'],
                'searchTerms':['固定疾病中英文同义词'],'relations':[{'relationId':'稳定关系标识',
                    'kind':'clinical|behavior|microbiome|function|source_model','factIds':['实际ID'],
                    'exposureTerms':['一个实际对象的中英文同义词'],
                    'outcomeTerms':['此固定疾病的准确中英文同义词'],'question':'该对象与此固定疾病的关系'}]}]}})
    responses=await _bounded_generate(client,tasks,facts=facts)
    dispositions=[]
    for task,response in zip(tasks,responses):
        rows=response.get('items',response.get('dispositions',[])) if isinstance(response,dict) else []
        for signal in task['uncoveredSignals']:
            candidate=scaffolds[signal['signalId']]
            matches=[row for row in rows if isinstance(row,dict) and row.get('signalId')==signal['signalId']]
            row=matches[0] if len(matches)==1 else {}
            status=row.get('status');ids=row.get('factIds')
            valid=(status in {'assess','not_independent','normal_after_review'}
                and isinstance(ids,list) and bool(ids) and set(ids)<=facts.keys()
                and bool(set(ids)&set(signal['factIds']))
                and len(str(row.get('reason','')))>=8)
            if row.get('riskId')!=candidate.risk_id:
                valid=False
            if valid:
                enriched=candidate.model_dump(mode='json',by_alias=True)
                if not fixed_disease_code(candidate.disease_id) and row.get('category') in _CATEGORY_CODES:
                    enriched['category']=row['category']
                for key in ('supportFactIds','opposingFactIds','backgroundFactIds','searchTerms','exposureTerms','relations'):
                    if key in row:
                        enriched[key]=row[key]
                if not enriched['supportFactIds']:
                    enriched['supportFactIds']=list(signal['factIds'])
                enriched['backgroundFactIds']=list(dict.fromkeys([*enriched['backgroundFactIds'],*ids]))
                valid=not _admit_candidates([enriched],document,facts,registry,mark_invalid=False)
                status='source_hypothesis_ready'
            if valid:
                # An explicit named source risk/screen still needs its own
                # reviewed index, even if the preliminary conclusion is low
                # concern. A qualitative dismissal is not that assessment.
                dispositions.append({'signalId':signal['signalId'],'status':status,
                    'factIds':ids,'riskIds':[candidate.risk_id] if status=='source_hypothesis_ready' else [],
                    'reason':row['reason'],'sourceDisease':signal['name']})
            else:
                dispositions.append({'signalId':signal['signalId'],'status':'needs_search',
                    'factIds':signal['factIds'],'riskIds':[candidate.risk_id],
                    'reason':'fixed_source_disease_hypothesis_not_completed','sourceDisease':signal['name']})
                registry.incomplete.append('RISK_SOURCE_COVERAGE_INCOMPLETE')
    return [*source_observations,*dispositions]


async def _complete_candidate_coverage(client, document, subject):
    registry, facts = _registry(document), _facts(document)
    registry.incomplete=[reason for reason in registry.incomplete if reason!='RISK_SOURCE_COVERAGE_INCOMPLETE']
    signals = [signal for batch in _signal_batches(risk_source_signals(document),facts) for signal in batch]
    pending = []
    dispositions = [row for row in registry.coverage_dispositions if row.get('status')=='supporting_theme']
    for signal in signals:
        if (signal['kind'] in {'source_model','source_association'}
                and non_disease_endpoint_kind(signal['name'])):
            dispositions.append({'signalId':signal['signalId'],'status':'source_observation',
                'factIds':list(signal['factIds']),'riskIds':[],
                'reason':'source_symptom_or_measurement_is_not_a_personal_disease',
                'sourceObservation':signal['name']})
            continue
        matches = [candidate for candidate in registry.candidates if set(signal['factIds']) & set([
            *candidate.support_fact_ids, *candidate.background_fact_ids, *candidate.opposing_fact_ids,
            *candidate.source_model_fact_ids])]
        if signal['kind'] in {'source_model','source_association'}:
            # Mentioning gastric-cancer score as background for reflux is
            # not a joint assessment of gastric cancer itself.
            matches=[candidate for candidate in matches if candidate.disease_id==_risk_identity(signal['name'])
                and _candidate_entity_scope(candidate)=='disease']
        if matches:
            dispositions.append({'signalId': signal['signalId'], 'status': 'covered',
                'riskIds': [candidate.risk_id for candidate in matches], 'factIds': signal['factIds']})
        else:
            pending.append(signal)
    source_pending=[signal for signal in pending if signal['kind'] in {'source_model','source_association'}]
    if source_pending:
        dispositions.extend(await _complete_source_disease_hypotheses(client,document,source_pending,subject))
    pending=[signal for signal in pending if signal not in source_pending]
    if pending:
        tasks=[]
        for batch in _signal_batches(pending,facts):
            tasks.append({
                'task': 'complete_integrated_risk_candidate_coverage', 'subject': subject,
                'uncoveredSignals': batch,
                'existingCandidates': [candidate.model_dump(mode='json',by_alias=True)
                    for candidate in registry.candidates],
                'personalContextFacts': [_fact_view(fact) for fact in facts.values()
                    if fact.kind in {'clinical','self_reported'} or fact.fact_id.startswith('profile:')],
                'rules': [
                    '逐项联合评判全部uncoveredSignals。可新增候选、合并既有候选，或根据个人实际资料记为不形成独立风险/综合低关注；不为凑数诊断。',
                    'microbiome_association_screen是相关菌群筛查，不是用户已出现该症状。来源疾病模型必须评判其本疾病，不能用胃癌分值已在反酸中出现、肝硬化分值已在脂肪肝中出现等代替本疾病判断。',
                    '每个signalId恰好给一项disposition，reason说明实际依据，factIds只能该信号或已给个人事实。不能仅因没症状、低原模型级就跳过。',
                    '需要候选时返回完整candidates，真实菌群/功能factId放支持/背景/反向，源模型放sourceModelFactIds；与同主题个人材料共同判断。',
                    'disposition状态只用merged/new_candidate/not_independent/normal_after_review/needs_search；采用候选给其candidateNames，未形成独立风险也要具体理由与真实factIds。',
                    '一文只验证一关系，relations分clinical/behavior/microbiome/function/source_model，不能强求一文覆盖所有资料。priority为1紧急/2一般/3常规。',
                    '不得将菌群合成功能改成人体浓度、单菌偏离改为感染确诊、属种重复改为独立证据权重；上下调并存按实际方向，不一概写不足。',
                    '菌群失调/维生素或氨基酸合成代谢异常是功能主题，不能为覆盖而各自塞成独立疾病候选。与现有具体风险有依据关系可merged，否则not_independent并说明其营养/通路意义，不抄一遍功能名当疾病。',
                    '本批至多4个健康主题，只返回本批dispositions，最多4个完整候选，每候选最多4条独立实际暴露关系；不要复写已覆盖的候选全部字段。',
                ],
                'outputSchema': {'candidates': '完整RiskCandidate字段数组',
                    'dispositions': [{'signalId':'输入ID','status':'状态', 'candidateNames':['采用候选名'],
                        'factIds':['实际ID'], 'reason':'实际个人依据与该信号处置理由'}]},
            })
        responses=await _bounded_generate(client,tasks,facts=facts)
        for task,response in zip(tasks,responses):
            batch=task['uncoveredSignals']
            await _admit_discovery_response(client,response,task,document,facts,registry)
            rows = response.get('dispositions', []) if isinstance(response, dict) else []
            for signal in batch:
                matches = [row for row in rows if isinstance(row,dict) and row.get('signalId')==signal['signalId']]
                row = matches[0] if len(matches)==1 else {}
                ids = row.get('factIds')
                status = row.get('status')
                targets = [candidate for candidate in registry.candidates
                    if candidate.name in row.get('candidateNames', [])]
                valid = (status in {'merged','new_candidate','not_independent','normal_after_review','needs_search'}
                    and isinstance(ids,list) and bool(ids) and set(ids)<=facts.keys()
                    and bool(set(ids)&set(signal['factIds']))
                    and isinstance(row.get('reason'),str) and len(row['reason'].strip())>=8)
                if status in {'merged','new_candidate'} and not targets:
                    valid=False
                if signal['kind'] in {'source_model','source_association'} and status in {'merged','new_candidate'}:
                    if not any(candidate.disease_id==_risk_identity(signal['name'])
                            and _candidate_entity_scope(candidate)=='disease' for candidate in targets):
                        valid=False
                if valid:
                    for candidate in targets:
                        target_ids = set([*candidate.support_fact_ids,*candidate.background_fact_ids,*candidate.opposing_fact_ids])
                        candidate.background_fact_ids.extend(fid for fid in ids if fid not in target_ids)
                        if signal['kind']=='source_model':
                            candidate.source_model_fact_ids.extend(fid for fid in signal['factIds']
                                if fid not in candidate.source_model_fact_ids)
                    dispositions.append({**row,'riskIds':[candidate.risk_id for candidate in targets]})
                    if status=='needs_search':
                        registry.incomplete.append('RISK_SOURCE_COVERAGE_INCOMPLETE')
                else:
                    dispositions.append({'signalId':signal['signalId'],'status':'needs_search',
                        'factIds':signal['factIds'],'reason':'source_signal_disposition_incomplete'})
                    registry.incomplete.append('RISK_SOURCE_COVERAGE_INCOMPLETE')
    registry.coverage_dispositions=dispositions
    # Cross-disease group headings remain useful as internal source themes,
    # but cannot compete with their now explicit disease questions as an
    # additional disease risk in the public ranking.
    broad=[candidate for candidate in registry.candidates if re.search(
        r'情绪与精神心理|骨与神经退行性',candidate.name)]
    if broad:
        registry.candidates=[candidate for candidate in registry.candidates if candidate not in broad]
        registry.coverage_dispositions.extend({'topicRiskId':candidate.risk_id,'status':'supporting_theme',
            'riskIds':[],'factIds':list(dict.fromkeys([*candidate.support_fact_ids,*candidate.background_fact_ids,
                *candidate.opposing_fact_ids,*candidate.source_model_fact_ids])),
            'reason':'跨多个独立疾病的资料分组保留内部，不额外作为一种疾病风险。'} for candidate in broad)


def _originals(records):
    from .evaluation_originals import evaluation_original_text
    result, unstable = {}, set()
    for item in records:
        try:
            row = item.model_dump(mode='json', by_alias=True) if hasattr(item, 'model_dump') else dict(item)
        except (TypeError, ValueError):
            continue
        key = row.get('evidenceKey') or row.get('evidence_key')
        text = row.get('sourceText')
        if not isinstance(text, str) or not text.strip():
            text = evaluation_original_text(row)
        if not isinstance(key, str) or not key or not isinstance(text, str) or not text.strip():
            continue
        if key in unstable:
            continue
        if key in result and result[key]['sourceText'] != text:
            # An unstable key cannot silently point at a different source.
            result.pop(key)
            unstable.add(key)
            continue
        result[key] = {'evidenceKey': key, 'sourceText': text,
            'title': row.get('title'), 'number': row.get('number'),
            'supports': row.get('supports') or [], 'studyType':row.get('studyType'), 'category':row.get('category')}
    return result


def _model_original(original, *, selected_quotes=(), query=''):
    """Bound a model reading packet; keep the actual full original private.

    Literal checks and applicability receipts always use ``_originals``.
    The packet's offsets/hash disclose omitted content without sending the
    same long publication in every author and reviewer request.
    """
    from .evaluation_originals import evaluation_source_packet
    packet = evaluation_source_packet(original, query=query,
        max_chars=12000, selected_quotes=selected_quotes)
    return {**{key: value for key, value in original.items() if key != 'sourceText'},
        'sourceText': packet['text'],
        'sourcePacket': {key: value for key, value in packet.items()
            if key not in {'text', 'selectedSourceText', 'selectedPassages'}}}


def _original_scope_hash(original):
    return _digest({key: original.get(key) for key in
        ('title', 'sourceText', 'studyType', 'category')})


def refresh_risk_original_scope(document, evidence_summaries):
    """Revoke stale source permission before any approved/pending shortcut.

    Longer recalled originals, changed population-bearing metadata and
    missing keys invalidate their earlier numeric/classification proofs.
    Reference numbering and retrieval relevance never change this scope.
    """
    registry = _registry(document)
    originals = _originals(evidence_summaries)
    keys = set(registry._evidence_hashes) | set(registry._original_scope_snapshot)
    changed = set()
    for key in keys:
        original = originals.get(key)
        body = _digest(original['sourceText']) if original is not None else None
        scope = _original_scope_hash(original) if original is not None else None
        if (body != registry._evidence_hashes.get(key)
                or scope != registry._original_scope_snapshot.get(key)):
            changed.add(key)
        if original is None:
            registry._evidence_hashes.pop(key, None)
            registry._original_scope_snapshot.pop(key, None)
        else:
            registry._evidence_hashes[key] = body
            registry._original_scope_snapshot[key] = scope
    affected = {row.risk_id for row in registry.assessments
        if changed & set([*row.evidence_keys, *row.opposing_evidence_keys,
            *(anchor.evidence_key for claim in row.claims for anchor in claim.evidence)])}
    for identifier in affected:
        registry._proofs.pop(identifier, None)
    for candidate in registry.candidates:
        if candidate.risk_id in affected:
            candidate._current_classification_scope = None
    return affected


def _record_risk_original_scope(registry, originals, keys):
    for key in keys:
        if key in originals:
            registry._original_scope_snapshot[key] = _original_scope_hash(originals[key])


def _coverage_relation_hints(candidate, originals, *, subject=None):
    signature = _digest(candidate.model_dump(mode='json', by_alias=True))
    population = _digest(_subject_view(subject))
    result = []
    for hint in candidate._coverage_hints:
        key, quote = hint.get('evidenceKey'), hint.get('quote')
        original = originals.get(key)
        if (hint.get('riskId') != candidate.risk_id or hint.get('candidateHash') != signature
                or hint.get('subjectHash') != population or not original
                or hint.get('sourceHash') != _digest(original['sourceText'])
                or not isinstance(quote, str) or quote not in original['sourceText']):
            continue
        result.append(dict(hint))
    return result


def _candidate_evidence(candidate, originals, *, purpose='relationship', subject=None):
    own = {candidate.risk_id, 'risk:' + candidate.risk_id, 'risk-management:'+candidate.risk_id}
    own.update(alias for identifier in candidate._retrieval_aliases
        for alias in (identifier,'risk:'+identifier,'risk-management:'+identifier))
    terms = [_key(term) for term in [candidate.name, *candidate.search_terms] if len(_key(term)) >= 4]
    sources = [row for row in originals.values() if own.intersection(row['supports']) or any(term in _key(
        str(row['title'] or '') + ' ' + row['sourceText']) for term in terms)]
    person=_subject_view(subject)
    hints = _coverage_relation_hints(candidate, originals, subject=subject) if purpose == 'relationship' else []
    hinted = {hint['evidenceKey'] for hint in hints}
    actionable={}
    if purpose=='management':
        from .evaluation_evidence_plan import management_actionable_content
        actionable={row['evidenceKey']:management_actionable_content(row['sourceText'],
            aliases=(candidate.name,*candidate.search_terms),human_trial=bool(re.search(
                r'randomi[sz]ed|clinical trial',str(row['title']or'')+' '+row['sourceText'],re.I))) for row in sources}
        sources=[row for row in sources if actionable[row['evidenceKey']]]
    def score(row):
        text=str(row['title'] or '')+' '+row['sourceText']
        relevance=20*bool(own.intersection(row['supports']))
        if purpose=='management':
            from .evaluation_evidence_plan import _risk_search_disease_code
            actual=actionable[row['evidenceKey']]
            same_condition=(_risk_search_disease_code(str(row['title']or'').strip(' .'))
                ==_risk_search_disease_code(candidate.name))
            relevance+=100*actual+60*bool(same_condition and _risk_search_disease_code(candidate.name))
            relevance+=30*('risk-management:'+candidate.risk_id in row['supports'])
            relevance+=15*bool(re.search(r'guideline|recommend|randomi[sz]ed|clinical trial|physical activity|diet',text,re.I))
            relevance-=10*bool(re.search(r'clinical significance.*diagnos|factors influencing diagnosis',str(row['title']),re.I))
            relevance-=40*bool(re.search(r'\bdesign and rationale\b|protocol|study design',str(row['title']),re.I))
        else:
            relevance += 100 * (row['evidenceKey'] in hinted)
            relevance+=10*bool(re.search(r'associat|prospective|risk of|risk for|randomi[sz]ed|guideline',text,re.I))
        return relevance
    def applicable(row):
        from types import SimpleNamespace
        from .evaluation_evidence_plan import _risk_scope_mismatch
        text=str(row['title'] or '')+' '+row['sourceText'][:2000]
        if _risk_scope_mismatch(SimpleNamespace(aliases=(candidate.name, *candidate.search_terms)),
                row['sourceText'], str(row['title'] or ''),
                subject_age=person.get('age'), subject_sex=person.get('sex')):
            return False
        if person.get('sex') in {'男','male'} and re.search(r'population of pregnant women|pregnancies complicated by',text,re.I):
            return False
        age=person.get('age')
        if type(age) is int and age>=18 and re.search(r'\b(?:pediatric|paediatric|infants|children aged)\b',str(row['title']),re.I):
            return False
        return True
    ordered = sorted((row for row in sources if applicable(row)),key=score,reverse=True)
    if not hints:
        return ordered
    # One verified original for each distinct relation before second-choice
    # generic sources. Shared sources remain single literal originals.
    available = {row['evidenceKey']: row for row in ordered}
    first = list(dict.fromkeys(hint['evidenceKey'] for hint in hints if hint['evidenceKey'] in available))
    return [available[key] for key in first] + [row for row in ordered if row['evidenceKey'] not in first]


def _candidate_view(candidate, facts):
    ids = list(dict.fromkeys([*candidate.support_fact_ids, *candidate.opposing_fact_ids,
        *candidate.background_fact_ids, *candidate.source_model_fact_ids]))
    result={**candidate.model_dump(mode='json', by_alias=True),
        'facts': [_fact_view(facts[fid]) for fid in ids if fid in facts]}
    if candidate._role_conflicts:
        result['roleConflictFactIds']=candidate._role_conflicts
    return result


def _decision_risk_view(candidate,facts,reviewed_claims=()):
    """Only approved observations/relations are positive scoring support."""
    result=_candidate_view(candidate,facts)
    for field in ('relationship','relations','searchTerms','exposureTerms'):
        result.pop(field,None)
    reviewed=list(dict.fromkeys(fid for claim in _score_claims(candidate,reviewed_claims)
        for fid in claim.fact_quotes if fid in facts))
    result['supportFactIds']=reviewed
    result['facts']=[_fact_view(facts[fid]) for fid in reviewed]
    # Complete background was considered upstream. It must not re-enter the
    # score as an unreviewed positive relationship merely through context.
    for field in ('opposingFactIds','backgroundFactIds','sourceModelFactIds'):
        result.pop(field,None)
    return result


def _limiting_context_quotes(fact):
    """Keep real normal/denied observations without adjacent positive data."""
    if fact.kind=='clinical' and fact.abnormal is not True and re.fullmatch(
            r'正常|阴性|未见异常|未检出',str(fact.status or '')):
        return [fact.value]
    if fact.kind=='self_reported':
        display=_questionnaire_state_display(fact)
        if display and display.startswith(('未报告','未吸烟','未使用')):
            from .evaluation_fixed_assessment import _answer
            answer=_answer(fact)
            if answer and answer in fact.value:
                return [answer]
        return list(dict.fromkeys(match.group().strip() for match in re.finditer(
            r'(?:无(?!法|论|需)|没有|否认|未发现|未曾|未有|未确诊|暂无|未见|^否$)'
            r'[^，。；;\n]{0,70}',fact.value) if match.group().strip()))
    return []


def _source_context_relevant(candidate,fact,claims=()):
    if fact.kind in {'clinical','self_reported'} or fact.fact_id.startswith('profile:'):
        return True
    if any(fact.fact_id in claim.fact_quotes for claim in claims):
        return True
    return (_fact_role(fact) in {'source_disease_model','microbiome_association_screen'}
        and _risk_identity(fact.name.removesuffix('（来源疾病模型指数）'))==candidate.disease_id)


def _score_context_errors(assessment,candidate,facts):
    if candidate is None or not re.search(r'未检出|阴性|正常|降低|减少|相反|限制',assessment.scoring_rationale):
        return []
    for fact in facts.values():
        if (_fact_role(fact)=='microbiome_association_screen'
                and fact.value in {'未检出','阴性','正常'}
                and not _source_context_relevant(candidate,fact,_score_claims(candidate,assessment.claims))
                and len(_key(fact.name))>=3 and _key(fact.name) in _key(assessment.scoring_rationale)):
            return ['unrelated_source_screen_used_to_limit_disease_index']
    return []


def _numeric_limiting_inventory(candidate, facts, claims, *, excluded_limits=()):
    """Literal eligible choices, not approval of their medical applicability."""
    positive={fid for claim in _score_claims(candidate,claims) for fid in claim.fact_quotes}
    allowed=set([*candidate.opposing_fact_ids,*candidate.background_fact_ids,
        *candidate.support_fact_ids,*candidate._role_conflicts])-positive
    result=[]
    for fid in sorted(allowed & facts.keys()):
        fact=facts[fid]
        for kind in ('actual_normal_result','explicit_symptom_denial'):
            quotes=[quote for quote in _limiting_context_quotes(fact)
                if _numeric_limiting_quote_allowed(fact,quote,kind)
                and (fid,quote,kind,candidate.disease_id) not in excluded_limits]
            if quotes:
                result.append({'factId':fid,'name':fact.name,'kind':kind,
                    'factQuotes':quotes,'sameDiseaseNotYetApproved':True})
    return result


def _decision_internal_context(candidate,facts,reviewed_claims=(),*,numeric=False,excluded_limits=()):
    support={fid for claim in _score_claims(candidate,reviewed_claims) for fid in claim.fact_quotes}
    declared=_declared_risk_signal_ids(candidate,facts)
    background=list(dict.fromkeys(fid for fid in [*candidate.background_fact_ids,*candidate.support_fact_ids,
        *candidate._role_conflicts] if fid in facts and fid not in support
        and fid not in candidate.opposing_fact_ids and fid not in candidate.source_model_fact_ids
        and fid not in declared))
    ids=list(dict.fromkeys([*support,*candidate.opposing_fact_ids,*candidate.source_model_fact_ids,*declared,*background]))
    core=_score_claims(candidate,reviewed_claims)
    signal_only=bool(candidate.certainty=='potential_risk' and core
        and all(claim.role=='source_fact' and set(claim.fact_quotes)<=set(declared) for claim in core))
    limiting={fid:_limiting_context_quotes(facts[fid]) for fid in
        dict.fromkeys([*background,*candidate.opposing_fact_ids]) if fid in facts and fid not in support}
    limiting={fid:quotes for fid,quotes in limiting.items() if quotes}
    choices=_numeric_limiting_inventory(candidate,facts,reviewed_claims,excluded_limits=excluded_limits) if numeric else []
    if numeric:
        permitted={item['factId']:{quote for other in choices if other['factId']==item['factId']
            for quote in other['factQuotes']} for item in choices}
        limiting={fid:[quote for quote in quotes if quote in permitted.get(fid,set())]
            for fid,quotes in limiting.items()}
        limiting={fid:quotes for fid,quotes in limiting.items() if quotes}
    if signal_only:
        ids=[fid for fid in ids if fid not in background or fid in limiting]
    def context_fact(fid):
        value=_fact_view(facts[fid])
        if fid in limiting:
            value['limitingFactQuotes']=limiting[fid]
            if signal_only:
                value['value']='；'.join(limiting[fid])
        value.update(supportEligible=fid in support,
            contextRole='reviewed_support' if fid in support else
                'opposing' if fid in candidate.opposing_fact_ids else
                'same_disease_source_signal' if fid in declared or fid in candidate.source_model_fact_ids
                else 'limiting_context' if signal_only and fid in limiting else 'provided_background')
        return value
    result={'opposingFactIds':list(candidate.opposing_fact_ids),
        'sourceModelFactIds':list(candidate.source_model_fact_ids),
        'backgroundFactIds':list(limiting) if signal_only else background,
        'limitingFactIds':list(limiting),
        'unreviewedCandidateFactIds':[] if signal_only else background,
        'positiveSupportFactIds':sorted(support),
        'supportBasis':'source_signal_only' if signal_only else 'individual_observation_or_reviewed_relationship',
        'sourceDeclaredRiskFactIds':declared,
        'providedPatientFields':_provided_patient_fields(facts),
        'patientStateDigest':_patient_state_digest(facts),
        # Complete raw states are a separate consistency domain, never
        # positive medical support. A name-only ledger hid real anomalies
        # and also leaked values embedded in generated fact names.
        'providedPatientState':[{**_fact_view(fact),'supportEligible':False,
            'permittedUse':'state_consistency_or_applicability_only'}
            for fact in facts.values() if fact.kind in {'clinical','self_reported'}
            or fact.fact_id.startswith('profile:')],
        'facts':[context_fact(fid) for fid in ids if fid in facts
            and _source_context_relevant(candidate,facts[fid],core)]}
    if numeric:
        result['allowedNumericLimitingFacts']=choices
        result['sourceGradeComparisonTargets']=_source_grade_targets(candidate,facts)
        result['sourceOnlyWithoutPersonalFactors']=_is_source_signal_only(candidate,facts,reviewed_claims)
        eligible={item['factId'] for item in choices}
        for item in result['providedPatientState']:
            item['numericLimitingEligible']=item['factId'] in eligible
    return result


def _index_author_context(document,candidate,facts,reviewed_claims=(),*,excluded_limits=()):
    """Keep signal-only support small while showing truthful personal states.

    A live prior independent decision may carry its real opposing premises.
    Candidate proposals alone do not authorize a new patient-negative story.
    """
    result=_decision_internal_context(candidate,facts,reviewed_claims,numeric=True,excluded_limits=excluded_limits)
    if result['supportBasis']!='source_signal_only':
        return result
    previous=next((row for row in approved_assessments(document)
        if row.risk_id==candidate.risk_id and row.disease_id==candidate.disease_id),None)
    reviewed_limits=set(previous.opposing_fact_ids) if previous is not None else set()
    owned=set([*result['positiveSupportFactIds'],*result['sourceDeclaredRiskFactIds'],
        *result['sourceModelFactIds'],*reviewed_limits])
    # The author must see the same true patient states as the independent
    # reviewer. Hiding them made a source-only number impossible to explain
    # and encouraged unsupported assumptions of absent symptoms/history.
    # These states remain explicitly ineligible for positive disease support.
    result.update(opposingFactIds=sorted(reviewed_limits),unreviewedCandidateFactIds=[])
    result['facts']=[item for item in result['facts'] if item['factId'] in owned]
    represented={item['factId'] for item in result['facts']}
    result['facts'].extend({**_fact_view(facts[fid]),'supportEligible':False,
        'contextRole':'independently_reviewed_opposing'} for fid in reviewed_limits-represented if fid in facts)
    return result


def _is_source_signal_only(candidate,facts,claims):
    core=_score_claims(candidate,claims)
    declared=set(_declared_risk_signal_ids(candidate,facts))
    return bool(candidate.certainty=='potential_risk' and core and declared and all(
        claim.role=='source_fact' and bool(claim.fact_quotes) and set(claim.fact_quotes)<=declared
        for claim in core))


def _index_decision_schema(candidate,facts,claims,*,opposing=True,excluded_limits=()):
    value={'riskId':candidate.risk_id,'index':'重新综合的0–100整数','level':'四级',
        'evidenceStrength':'强/中/弱/有限'}
    # The source grade stays a literal observation, not a hard floor for an
    # independent personal attention index. Specificity and personal
    # relevance still require the existing bound numeric-factor review;
    # missing clinical information must never become a fabricated negative.
    return value


def _patient_state_digest(facts):
    return _digest([_fact_view(fact) for fid,fact in sorted(facts.items())
        if fact.kind in {'clinical','self_reported'} or fid.startswith('profile:')])


def _score_claims(candidate,claims):
    return [claim for claim in claims if claim.role!='management' and not (
        candidate.certainty=='potential_risk' and claim.role=='diagnostic_principle'
        and not _personal_clinical_disease_assessment(claim, candidate))]


def _deduplicate_relation_atoms(claims,facts,*,preferred_ids=()):
    """Collapse the same bound observation/relation before fresh scoring.

    The chosen text is one complete already supplied atom. No qualifier is
    trimmed, and unrelated statements sharing a composite answer stay apart.
    Every retained/new atom still enters the existing independent reviewer.
    """
    groups={};order=[];preferred=set(preferred_ids)
    for claim in claims:
        patient=tuple(sorted(claim.fact_quotes.items()))
        if claim.role=='source_fact':
            scalar=all(fid in facts and (facts[fid].kind=='context' or re.fullmatch(
                r'[\s+\-<>≤≥\d.,%]+',facts[fid].value or '')) for fid in claim.fact_quotes)
            key=('source_fact',patient) if scalar else ('source_fact',patient,_key(claim.text))
        elif claim.role in {'outcome','etiology','mechanism'}:
            from .evaluation_evidence_plan import _RISK_OBJECT_ALIASES
            def object_key(group):
                names={_key(term) for term in group}
                matches=[aliases for aliases in _RISK_OBJECT_ALIASES
                    if names&{_key(alias) for alias in aliases}]
                if matches:
                    return _key(max(matches,key=lambda aliases:max(map(len,aliases)))[0])
                return '|'.join(sorted({_risk_identity(_endpoint_name(term)) for term in group}))
            exposure=tuple(sorted({object_key(group) for group in claim.exposure_terms}))
            outcome=tuple(sorted({_risk_identity(_endpoint_name(term)) for term in claim.outcome_terms}))
            anchors=tuple(sorted({(anchor.evidence_key,anchor.direction) for anchor in claim.evidence}))
            key=(claim.role,patient,exposure,outcome,anchors)
        else:
            key=(claim.role,claim.claim_id)
        if key not in groups:
            groups[key]=claim;order.append(key)
            continue
        old=groups[key]
        if claim.role in {'outcome','etiology','mechanism'}:
            # A shared paper/endpoint alone is insufficient. Prefix and
            # overlapping source anchors witness one relation; disjoint
            # results (including opposite directions/subgroups) stay apart.
            overlaps=all(any(anchor.evidence_key==other.evidence_key
                and (anchor.quote in other.quote or other.quote in anchor.quote)
                for other in old.evidence) for anchor in claim.evidence)
            if not overlaps:
                distinct=(*key,_digest(claim.model_dump(exclude={'claim_id'})))
                groups[distinct]=claim;order.append(distinct)
                continue
        if (claim.claim_id in preferred and old.claim_id not in preferred) or (
                claim.claim_id not in preferred and old.claim_id not in preferred
                and len(claim.text)>len(old.text)):
            groups[key]=claim
    return [groups[key] for key in order]


def _normalise_claim_metadata(value):
    result=dict(value)
    terms=result.get('outcomeTerms')
    if isinstance(terms,list) and terms and all(isinstance(group,list)
            and all(isinstance(term,str) for term in group) for group in terms):
        result['outcomeTerms']=list(dict.fromkeys(term for group in terms for term in group))
    return result


def _decision_opposing_keys(value,candidate,facts,originals):
    if not isinstance(value,list) or any(not isinstance(key,str) for key in value):
        raise ValueError('opposing evidence keys must be strings')
    # An exact supplied fact remains in its existing fact domain and role.
    # This repairs a slot error only: it never turns an approved supporting
    # observation into opposing evidence or accepts a guessed/unknown ID.
    allowed=set([*candidate.support_fact_ids,*candidate.opposing_fact_ids,
        *candidate.background_fact_ids,*candidate.source_model_fact_ids])
    return list(dict.fromkeys(key for key in value if not (
        key in facts and key in allowed and key not in originals)))


def _unsupported_food_context(text,facts):
    def asserted(value,pattern):
        return any(not re.search(r'没有|并非|未见|否认|不能|不宜|不吃|避免|限制|减少',
            value[max(0,match.start()-8):match.start()]) for match in re.finditer(pattern,value))
    patient=' '.join(fact.value.split('回答：')[-1] for fact in facts.values() if fact.kind=='self_reported')
    errors=[]
    if asserted(text,r'高脂外卖|高油外卖|高油高盐|高脂饮食|高油饮食') and not asserted(
            patient,r'高脂|高油|油炸|油腻|肥肉|高脂肪'):
        errors.append('unreported_high_fat_food_in_score_rationale')
    if asserted(text,r'高盐外卖|高盐饮食') and not asserted(patient,r'高盐|重盐|重口味|腌制'):
        errors.append('unreported_high_salt_food_in_score_rationale')
    return errors


def _foreign_model_language(text,candidate):
    return (not candidate.source_model_fact_ids and bool(re.search(
        r'(?:原|来源|本病)(?:疾病)?模型.{0,14}(?:低风险|中风险|高风险|风险较低|风险较高|指数|评分)',text)))


def _selected_quote_scientific_duty(text,quote):
    """Distinguish an observed relationship from a definition or preamble.

    This is a necessary scope check, never sufficient scientific approval.
    A study's topic and a list of co-components cannot supply an endpoint.
    """
    observed=bool(re.search(r'associated with (?:incident|future)|risk of developing|'
        r'(?:higher|increased|lower|reduced).{0,30}(?:incidence|risk of)|'
        r'contribut\w*.{0,50}(?:risk|onset)|hazard ratio|odds ratio|'
        r'发生风险.{0,8}(?:增加|升高|降低)|(?:发生|发病)率.{0,10}(?:增加|升高|降低)',quote,re.I))
    diagnostic=bool(re.search(r'characteri[sz]ed by|defined (?:by|as)|diagnostic (?:criteria|features?|components?)|'
        r'criteria for|(?:diagnosis|diagnoses).{0,50}(?:considered|based|criteria)|'
        r'(?:adiposity|disease|condition).{0,25}should be confirmed|screen positive|'
        r'in addition to BMI|co.occurring.{0,100}(?:markers|muscle)|'
        r'(?:诊断|判定)(?:依据|标准|组成|指标)|定义为|筛查阳性',quote,re.I))
    components=bool(re.search(r'\b(?:risk factors?|components?|criteria) include\b',quote,re.I)
        and re.search(r'组成|构成|集群|聚集|共现|共同|同类|特征|筛查|确认|指标之一',text))
    if not observed and (diagnostic or components):
        return 'diagnostic_principle'
    if (not observed and re.search(r'\b(?:important|key) (?:indicator|marker)\b|'
            r'重要(?:指标|标志)',quote,re.I)):
        return 'background'
    return None


def _empirical_background_anchor(quote,source):
    """A primary study's introduction is not its measured relationship."""
    text=str(source.get('sourceText') or '')
    title=str(source.get('title') or '')
    if re.search(r'guideline|consensus|position statement|scientific statement',title,re.I):
        return False
    sections=list(re.finditer(r'\b(BACKGROUND|INTRODUCTION|AIMS?|OBJECTIVES?|METHODS?|'
        r'MATERIALS AND METHODS|RESULTS|FINDINGS|INTERPRETATION|CONCLUSIONS?)\s*:',text,re.I))
    position=text.find(quote)
    if position<0:
        return False
    prior=[match for match in sections if match.start()<=position]
    if not prior or prior[-1][1].casefold() not in {'background','introduction','aim','aims','objective','objectives'}:
        return False
    method=next((match for match in sections if 'method' in match[1].casefold()),None)
    result=next((match for match in sections if match[1].casefold() in {'results','findings'}),None)
    if method is None or result is None:
        return False
    actual=text[method.end():result.start()]
    return bool(re.search(r'participants|subjects|patients|cohort|random|trial|'
        r'GWAS|cis.eQTL|genome.wide|Mendelian|RNA.sequen|colocali[sz]|'
        r'genotyp|risk variants?|instrumental variables?|实验|队列|受试者',actual+' '+title,re.I))


def _same_definition_list_objects(quote,exposure_groups,outcome_terms,source_text=''):
    match=re.search(r'(?:risk factors?|components?|criteria) include|(?:组成|诊断标准|要素)包括',quote,re.I)
    if match is None:
        return False
    before,listed=quote[:match.start()],quote[match.end():]
    target=_defined_quote_mention(listed,outcome_terms,source_text)
    outcome_before=_defined_quote_mention(before,outcome_terms,source_text)
    return bool(target and not outcome_before and any(
        _defined_quote_mention(listed,group,source_text) for group in exposure_groups))


def _coverage_relationship_errors(candidate, relation, facts, original, quote, review, plan=None):
    """Coverage can close a relation duty, never merely a same-disease hit."""
    from types import SimpleNamespace
    from .evaluation_evidence_plan import (_mentions, _risk_outcome_aliases,
        risk_retrieval_concepts, _risk_relation_candidate, _retrieval_topic)
    if review.get('evidenceRole') not in {None, 'risk_relationship', 'population_association', 'mechanism'}:
        return ['coverage_wrong_evidence_role']
    if review.get('supportsExposureOutcomeRelation') is False:
        return ['coverage_does_not_support_exposure_outcome']
    if _empirical_background_anchor(quote,original):
        return ['coverage_quote_is_empirical_background_not_study_relationship']
    if _selected_quote_scientific_duty(relation.question,quote) in {'diagnostic_principle','background'}:
        return ['coverage_quote_is_definition_or_background_not_relationship']
    row = relation.model_dump(mode='json', by_alias=True)
    outcome = _risk_outcome_aliases(candidate.model_dump(mode='json', by_alias=True), row)
    exposure = risk_retrieval_concepts(relation.exposure_terms,
        source_names=[facts[fid].name for fid in relation.fact_ids if fid in facts])
    topic = next((topic for topic in plan.topics if topic.kind == 'risk'
        and candidate.risk_id in topic.source_support_ids and topic.query_relations
        and topic.query_relations[0].get('relationId') == relation.relation_id), None) if plan else None
    if topic is not None:
        topic = _retrieval_topic(plan, topic)
        outcome, exposure = topic.query_concepts[:2]
    groups = (tuple(outcome), tuple(exposure))
    if _same_definition_list_objects(quote,[exposure],outcome,original['sourceText']):
        return ['coverage_quote_is_definition_or_background_not_relationship']
    defined = [_defined_quote_mention(quote, group, original['sourceText']) for group in groups]
    if not all(defined):
        return ['coverage_quote_missing_actual_exposure_or_outcome']
    if _risk_relation_candidate(SimpleNamespace(kind='risk', query_concepts=groups), quote):
        return []
    # An abbreviation may be used only when this source explicitly defines
    # it as the same object. The literal stored quote itself never changes.
    if (any(not _mentions(quote, group) for group in groups)
            and review.get('supportsExposureOutcomeRelation') is True
            and review.get('evidenceRole') in {'risk_relationship', 'population_association', 'mechanism'}
            and re.search(r'associat|correlat|risk factors? for|inciden|predict|contribut|'
                r'相关|关联|发生率|预测|危险因素', quote, re.I)):
        return []
    return ['coverage_quote_is_not_actual_relationship']


async def review_risk_evidence(client, document, records_or_summaries, *, subject=None, deadline=None):
    """Review independent relations; no paper must explain the whole record."""
    from time import monotonic
    registry, facts = _registry(document), _facts(document)
    expired_duties = set()
    originals = _originals(records_or_summaries)
    gaps, duties = [], []
    for candidate in registry.candidates:
        for relation in candidate_relations(candidate, facts):
            if relation.kind == 'source_model':
                continue
            sources = _candidate_evidence(candidate, originals, subject=subject)
            exact = [source for source in sources if relation.relation_id in source['supports']]
            sources = exact or sources
            if not sources:
                gaps.append({'riskId': candidate.risk_id, 'relationId':relation.relation_id,
                    'reason':'no_original_relation_evidence','searchTerms':candidate.search_terms,
                    'missingRole':'risk_relationship'})
            else:
                duties.append({'riskId':candidate.risk_id,'relation':relation.model_dump(mode='json',by_alias=True),
                    'requiredRole':'risk_relationship',
                    'facts':[_fact_view(facts[fid]) for fid in relation.fact_ids if fid in facts],
                    'sourceModelFactIds':candidate.source_model_fact_ids,'evidence':sources[:8]})
    if duties and client is not None:
        gate = asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
        async def review_batch(batch):
            async with gate:
                sources={source['evidenceKey']:source for item in batch for source in item['evidence']}
                return await client.generate({'task':'review_integrated_risk_evidence_coverage',
                    'subject':_subject_view(subject),
                    'items':[{key:value for key,value in item.items() if key!='evidence'} | {
                        'evidenceKeys':[source['evidenceKey'] for source in item['evidence']]} for item in batch],
                    'evidence':list(sources.values()),
                    'rules':[
                        '每项只验证该relation，不要求一篇文献同时解释其他临床/行为/菌群资料；临床关系不强制菌群证据。',
                        '患者数值由facts证明，论文不必包含本人具体数字。核查医学概念、方向、人群/部位/菌种层级；研究定义不能代替发生风险，观察关联不能升级确定因果。',
                        'covered必须有实际支持该独立关系的原始逐字quote；纯定义/目录/无关研究不算。支持、相反及混合结果均保留供综合。',
                        '本阶段requiredRole=risk_relationship，只查实际暴露→结局关联/机制。推荐人群声明、疾病定义、诊断组成项、同一标准中的危险因素列表、药物/生活方式治疗效果及转介建议属于别的职责，不能关闭该关系缺口；即使同病也必须covered=false。important indicator等背景重要性不等于本研究提供的发生关联；已患病人的治疗差距研究不能仅借背景首句证明初发风险。',
                        'explicit exposure/outcome以本人的事实对象为准，可采用同一医学概念的确切同义词与本篇明确给出的缩写定义；不能让菌群筛查阳性本身循环证明另一症状或疾病。',
                        '逐riskId+relationId返回covered、evidenceKey、quote、missingConditions及reason/searchTerms。source model的原数值不需要论文再证明。',
                    ],'outputSchema':{'items':[{'riskId':'原风险ID','relationId':'原关系ID','covered':True,
                        'evidenceRole':'risk_relationship|management|definition|population_scope|none',
                        'supportsExposureOutcomeRelation':True,
                        'evidenceKey':'原键','quote':'实际支持此关系的逐字原文','missingConditions':[],
                        'reason':'缺口原因','searchTerms':['改写词']} ]}})
        batches=[duties[offset:offset+6] for offset in range(0,len(duties),6)]
        if deadline is not None and deadline <= monotonic():
            responses=[]
            expired_duties={(item['riskId'],item['relation']['relationId']) for item in duties}
        else:
            tasks=[asyncio.create_task(review_batch(batch)) for batch in batches]
            try:
                if deadline is None:
                    responses=await asyncio.gather(*tasks,return_exceptions=True)
                else:
                    completed,pending=await asyncio.wait(tasks,timeout=max(0.0,deadline-monotonic()))
                    # Only successful, actually completed model reviews may
                    # close coverage. A deadline cannot approve or erase them.
                    responses=[]
                    for task,batch in zip(tasks,batches):
                        if task not in completed:
                            expired_duties.update((item['riskId'],item['relation']['relationId']) for item in batch)
                            continue
                        if task.cancelled():
                            continue
                        try:
                            responses.append(task.result())
                        except Exception as error:
                            responses.append(error)
            finally:
                pending=[task for task in tasks if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending,return_exceptions=True)
        reviews=[row for response in responses if isinstance(response,dict)
            and isinstance(response.get('items'),list) for row in response['items']]
        for item in duties:
            matches=[row for row in reviews if isinstance(row,dict)
                and row.get('riskId')==item['riskId'] and row.get('relationId')==item['relation']['relationId']]
            row=matches[0] if len(matches)==1 else {}
            key,quote=row.get('evidenceKey'),row.get('quote')
            candidate = next(candidate for candidate in registry.candidates if candidate.risk_id == item['riskId'])
            if row:
                candidate._coverage_hints = [hint for hint in candidate._coverage_hints
                    if hint.get('relationId') != item['relation']['relationId']]
            valid_anchor = (row.get('covered') is True and row.get('missingConditions')==[] and key in originals
                    and key in {source['evidenceKey'] for source in item['evidence']}
                    and isinstance(quote,str) and len(quote.strip())>=8 and quote in originals[key]['sourceText'])
            relation = RiskRelation.model_validate(item['relation'])
            role_errors = _coverage_relationship_errors(candidate, relation, facts, originals[key], quote, row,
                getattr(document, '_evaluation_evidence_plan', None)) if valid_anchor else []
            if valid_anchor and not role_errors:
                candidate._coverage_hints.append({'riskId': candidate.risk_id,
                    'relationId': item['relation']['relationId'], 'evidenceKey': key, 'quote': quote,
                    'candidateHash': _digest(candidate.model_dump(mode='json', by_alias=True)),
                    'subjectHash': _digest(_subject_view(subject)),
                    'sourceHash': _digest(originals[key]['sourceText'])})
                continue
            gaps.append({'riskId':item['riskId'],'relationId':item['relation']['relationId'],
                'reason':','.join(role_errors) or row.get('reason') or ('coverage_review_deadline'
                    if (item['riskId'],item['relation']['relationId']) in expired_duties
                    else 'independent_relationship_not_verified'),
                'searchTerms':row.get('searchTerms') if isinstance(row.get('searchTerms'),list) else [],
                'missingRole':'risk_relationship'})
    elif duties:
        gaps.extend({'riskId':item['riskId'],'relationId':item['relation']['relationId'],
            'reason':'coverage_review_unavailable','searchTerms':[],
            'missingRole':'risk_relationship'} for item in duties)
    registry.gaps=gaps
    return gaps


def _valid_fact_quotes(quotes, facts, allowed):
    return (isinstance(quotes, dict) and bool(quotes) and set(quotes) <= set(allowed)
        and all(isinstance(quote, str) and quote.strip() and quote in facts[fid].value
            for fid, quote in quotes.items() if fid in facts) and set(quotes) <= facts.keys())


@lru_cache(maxsize=128)
def _literal_abbreviation_definitions(source_text):
    """Cache immutable literal definitions by the exact, complete source.

    Forward matches, reverse matches and repeated definitions retain their
    original order. This parses text only; no patient or review state is cached.
    """
    definitions=[]
    for match in re.finditer(r'([A-Za-z][A-Za-z0-9 ,/\-]{3,180})\s*\(([A-Z][A-Za-z0-9+/\-]{1,24})\)',source_text):
        definitions.append((match[1].strip(),match[2]))
    for match in re.finditer(r'\b([A-Z][A-Za-z0-9+/\-]{1,24})\s*\(([A-Za-z][A-Za-z0-9 ,/\-]{3,180})\)',source_text):
        definitions.append((match[2].strip(),match[1]))
    return tuple(definitions)


def _defined_quote_mention(quote, terms, source_text):
    """Resolve only a short form explicitly defined by this selected source.

    Matching never rewrites a stored quote or borrows another publication's
    definitions. Conflicting definitions leave the acronym unresolved.
    """
    from .evaluation_evidence_plan import _mentions
    if _mentions(quote, tuple(terms)):
        return True
    normalize = lambda text: re.sub(r'[^a-z0-9]+', '', str(text).casefold())
    aliases = [normalize(term) for term in terms if isinstance(term, str)
        and term.isascii() and len(normalize(term)) >= 4]
    definitions = {}
    for long_form, short in _literal_abbreviation_definitions(source_text or ''):
        definitions.setdefault(short, []).append(normalize(long_form))
    for short, long_forms in definitions.items():
        if not re.search(r'(?<![A-Za-z0-9])' + re.escape(short) + r'(?![A-Za-z0-9])', quote):
            continue
        if aliases and all(any(form.endswith(alias) for alias in aliases) for form in long_forms):
            return True
    return False


def _concept_forms(terms, source_text=''):
    """Entity aliases and source-defined abbreviations, not new associations."""
    from .evaluation_catalog import FIXED_DISEASE_ALIASES
    from .evaluation_evidence_plan import risk_retrieval_concepts
    forms=[];literal={term.strip().casefold() for term in terms if isinstance(term,str)}
    sleep_forms=('睡眠','睡眠时长','睡眠不足','短睡眠','sleep','sleeping','sleep duration','short sleep duration')
    from .evaluation_evidence_plan import _mentions
    sleep_keys = {_key(value) for value in sleep_forms}
    def duration_surface(term):
        return (isinstance(term, str) and (_key(term) in sleep_keys
            or re.fullmatch(r'(?:夜间|每晚|长期|规律)?睡眠(?:时长|时间)?(?:不足|少于|短于|低于|多于|超过|[<>≤≥])?\s*'
                r'\d+(?:\.\d+)?\s*(?:小时|h)', term.strip(), re.I)
            or re.fullmatch(r'(?:sleeping|sleep(?: duration)?)\s+(?:(?:of|less than|under|below|shorter than|more than|over|at least)\s+)?'
                r'\d+(?:\.\d+)?\s*(?:hours?|h)(?:\s+(?:per|a) night)?', term.strip(), re.I)))
    if any(duration_surface(term) for term in terms):
        forms.extend(sleep_forms)
    for term in [*terms,*risk_retrieval_concepts(terms)]:
        if not isinstance(term,str) or not term.strip():
            continue
        base=_endpoint_name(term)
        for value in (term,base,*FIXED_DISEASE_ALIASES.get(_risk_identity(base),()),
                *_CLINICAL_ENTITY_ALIASES.get(_risk_identity(base),())):
            short=(bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9+/\-]{1,24}',value or ''))
                and sum(letter.isupper() for letter in value)>=2)
            canonical_ascvd = (value == 'ASCVD' and _risk_identity(base) == 'atheroscleroticCardiovascularDisease')
            if (short and value.casefold() not in literal and not canonical_ascvd
                    and not _defined_quote_mention(value,terms,source_text)):
                continue
            if value and value not in forms:
                forms.append(value)
    # Only definitions explicitly present in this very record may connect
    # an abbreviation to its long form. Preserve qualifiers such as other
    # components: they cannot silently become the disease itself.
    for long_form,short in _literal_abbreviation_definitions(source_text):
        for value in list(forms):
            if not value.isascii():
                continue
            # A regex match may include an introductory sentence. Match a
            # complete term suffix, never arbitrary shared words.
            plain=value.strip()
            words=long_form.split()
            suffixes=[plain,long_form,*[' '.join(words[index:]) for index in range(len(words)-1)],
                *[alias for aliases in FIXED_DISEASE_ALIASES.values()
                for alias in aliases if alias.isascii() and re.search(r'\b'+re.escape(alias)+r'\b',plain,re.I)]]
            for suffix in suffixes:
                if not re.search(r'(?:^|\b)'+re.escape(suffix)+r'$',long_form,re.I):
                    continue
                changed=re.sub(r'\b'+re.escape(suffix)+r'\b',short,plain,flags=re.I)
                # No literal replacement means no new form. Retain the rare
                # whitespace-normalization addition if plain is not present yet.
                if changed==plain and changed in forms:
                    continue
                if (_defined_quote_mention(short,[suffix],source_text)
                        and changed not in forms):
                    forms.append(changed)
    return tuple(forms)


def _source_group_definitions(source):
    """Return literal compound group conditions without flattening +/-."""
    result=[]
    for match in re.finditer(r'([A-Za-z][A-Za-z0-9 ,/\-]{3,180})\s*\(([A-Z][A-Za-z0-9+/\-]{1,24})\)',source or ''):
        code=match[2]
        if '/' in code and any(sign in code for sign in ('+','-')):
            result.append({'code':code,'definition':match[1].strip(),
                'components':[{'label':name,'polarity':sign} for name,sign in re.findall(r'([A-Za-z][A-Za-z0-9]*)([+-])',code)]})
    return result


@dataclass(frozen=True)
class _ScientificApplicabilityReceipt:
    fingerprint: str


def _source_applicability_requirements(claim, originals):
    if claim.role not in {'outcome','etiology','mechanism'}:
        return []
    inputs=tuple((a.evidence_key,a.quote,str(originals.get(a.evidence_key,{}).get('title') or ''),
        str(originals.get(a.evidence_key,{}).get('sourceText') or '')) for a in claim.evidence)
    # Only literal source parsing is cached. Fresh mutable views and all
    # patient/review receipts remain request-local and uncached.
    exposure=tuple(tuple(group) for group in claim.exposure_terms)
    return json.loads(_source_applicability_requirements_json(claim.role,exposure,inputs))


@lru_cache(maxsize=256)
def _source_applicability_requirements_json(role, exposure, inputs):
    from types import SimpleNamespace
    claim=SimpleNamespace(role=role,exposure_terms=exposure,
        evidence=[SimpleNamespace(evidence_key=key,quote=quote) for key,quote,_title,_source in inputs])
    originals={key:{'title':title,'sourceText':source} for key,_quote,title,source in inputs}
    return json.dumps(_source_applicability_requirements_uncached(claim,originals),ensure_ascii=False)


def _source_applicability_requirements_uncached(claim, originals):
    """Extract source conditions before viewing the patient's proposed story.

    These are observation/endpoint shapes, not disease-specific exclusions.
    Every condition retains its literal same-record witness. The independent
    reviewer must bind it to actual patient observations, not to claim prose.
    """
    if claim.role not in {'outcome','etiology','mechanism'}:
        return []
    result=[]
    patterns=(
        ('longitudinal_change',r'[^.。\n]*(?:trajector\w*|longitudinal\s+(?:changes?|increas\w*)|'
            r'腰围.{0,12}(?:持续|趋势)|纵向变化)[^.。\n]*'),
        ('genetic_mechanism',r'[^.。\n]*(?:genetic (?:predisposition|mechanisms?|factors?|variation)|'
            r'polygenic risk score|genotyp\w*|遗传(?:机制|易感|变异))[^.。\n]*'),
        ('derived_index',r'[^.。\n]*(?:(?:index|score|biomarker)[^.。\n]{0,180}'
            r'(?:derived|calculated|multiplying|ratio)|(?:derived|calculated)[^.。\n]{0,180}'
            r'(?:index|score)|指数[^.。\n]{0,80}(?:计算|乘|比值))[^.。\n]*'),
        ('diagnostic_identification',r'[^.。\n]*(?:(?:cutoff|cut-off|threshold)[^.。\n]{0,150}'
            r'(?:detect|diagnos)|early detection|诊断(?:阈值|界值)|早期识别)[^.。\n]*'),
        ('inverse_direction',r'[^.。\n]*(?:inversely associated|inverse (?:association|correlation)|'
            r'negatively associated|呈反向关联|负相关)[^.。\n]*'),
    )
    for anchor in claim.evidence:
        source=originals.get(anchor.evidence_key,{})
        literal=str(source.get('sourceText') or '')
        title=str(source.get('title') or '')
        # A trajectory/genetic study keeps that design even if an author
        # cherry-picks its generic introductory sentence as the anchor.
        applicable=set()
        if re.search(r'trajector|轨迹',title,re.I):
            applicable.add('longitudinal_change')
        if re.search(r'genetic predisposition|polygenic|genotyp|遗传易感|基因型',title+' '+anchor.quote,re.I):
            applicable.add('genetic_mechanism')
        for kind,pattern in patterns:
            search=literal if kind in applicable or kind=='derived_index' else anchor.quote
            if kind=='longitudinal_change' and re.search(pattern,anchor.quote,re.I):
                # The selected comparison identifies the exposure group's
                # direction. A generic earlier Methods/aim sentence does not.
                search=anchor.quote
            for match in re.finditer(pattern,search,re.I):
                quote=match.group().strip()
                if kind=='derived_index':
                    aliases=re.findall(r'\(([A-Z][A-Za-z0-9+/-]{1,20})\)',quote)
                    exposures=[term for group in claim.exposure_terms for term in group]
                    forms=_concept_forms(exposures,quote)
                    # Only an index used as the actual exposure creates an
                    # index premise. An endpoint acronym in the same paper
                    # (e.g. syndrome MetS beside a TyG definition) does not.
                    bound_aliases=[alias for alias in aliases if any(alias.casefold()==form.casefold()
                        for form in forms) or _defined_quote_mention(alias,exposures,quote)]
                    explicit_index=any(re.search(r'index|score|ratio|指数|评分|比值',term,re.I)
                        and re.search(re.escape(term),quote,re.I) for term in exposures)
                    if not (any(re.search(r'(?<![A-Za-z0-9])'+re.escape(alias)+r'(?![A-Za-z0-9])',
                            anchor.quote) for alias in bound_aliases) or explicit_index):
                        continue
                row={'kind':kind,'evidenceKey':anchor.evidence_key,'sourceQuote':quote}
                if kind=='longitudinal_change':
                    direction=re.search(r'\b(increasing|decreasing|rising|falling)\b'
                        r'[^.;\n]{0,100}\b(?:trajectory|trajectories|trend|trends)\b',quote,re.I)
                    row['exposureDirection']=('increasing' if direction and direction[1].lower() in
                        {'increasing','rising'} else 'decreasing' if direction else 'unknown')
                row['premiseId']='premise:'+_digest(row)[:18]
                if row not in result:
                    result.append(row)
                break
        if re.search(r'\b(?:vs\.?|versus|compared (?:with|to)|than)\b|相比|比较', anchor.quote, re.I):
            row={'kind':'comparison_population','evidenceKey':anchor.evidence_key,'sourceQuote':anchor.quote}
            row['premiseId']='premise:'+_digest(row)[:18]
            result.append(row)
        for group in _source_group_definitions(literal):
            if group['code'] in anchor.quote:
                row={'kind':'compound_group','evidenceKey':anchor.evidence_key,
                    'sourceQuote':next((match.group().strip() for match in re.finditer(
                        r'[^.。\n]*'+re.escape(group['code'])+r'[^.。\n]*',literal)),group['definition']),
                    'group':group}
                row['premiseId']='premise:'+_digest(row)[:18]
                result.append(row)
    return result


def _applicability_fingerprint(claim, facts, originals):
    return _digest({'claim':claim.model_dump(mode='json',by_alias=True),
        'patient':{fid:facts[fid].model_dump(mode='json',by_alias=True)
            for fid in claim.fact_quotes if fid in facts},
        'sources':{a.evidence_key:_digest(originals.get(a.evidence_key,{}).get('sourceText',''))
            for a in claim.evidence},'requirements':_source_applicability_requirements(claim,originals)})


def _source_applicability_binding_errors(claim, review, facts, originals):
    requirements=_source_applicability_requirements(claim,originals)
    if not requirements:
        return []
    rows=review.get('sourcePremiseBindings') if isinstance(review,dict) else None
    if not isinstance(rows,list) or len(rows)!=len(requirements):
        return ['scientific_source_prerequisites_not_independently_bound']
    expected={row['premiseId']:row for row in requirements}
    seen=set()
    for row in rows:
        if not isinstance(row,dict) or row.get('premiseId') not in expected or row['premiseId'] in seen:
            return ['scientific_source_prerequisites_not_independently_bound']
        premise=expected[row['premiseId']];seen.add(row['premiseId'])
        if (row.get('sourceQuote')!=premise['sourceQuote'] or row.get('evidenceKey')!=premise['evidenceKey']
                or row.get('patientMatch')!='matched' or row.get('missingConditions')!=[]):
            return ['scientific_patient_prerequisite_unknown_or_mismatched']
        bound=row.get('factQuotes')
        if not _valid_fact_quotes(bound,facts,claim.fact_quotes):
            return ['scientific_prerequisite_patient_binding_missing']
        selected=[facts[fid] for fid in bound]
        text=' '.join(fact.name+' '+fact.value+' '+str(fact.status or '') for fact in selected)
        kind=premise['kind']
        if kind=='longitudinal_change':
            dates={fact.date for fact in selected if fact.date}
            explicit=re.search(r'持续(?:增加|增大|增长)|逐(?:年|月|渐).{0,6}(?:增加|增大)|'
                r'从.{0,15}(?:增至|增加到)|increas\w*.{0,15}(?:over|years|months)|trajectory',text,re.I)
            if len(dates)<2 and not explicit:
                return ['single_measurement_does_not_establish_longitudinal_change']
            serial=[fact for fact in selected if fact.date and re.fullmatch(r'[+-]?\d+(?:\.\d+)?',fact.value)]
            if len(serial)>=2 and len({fact.unit for fact in serial})==1:
                serial.sort(key=lambda fact:fact.date)
                change=float(serial[-1].value)-float(serial[0].value)
                if (premise.get('exposureDirection')=='increasing' and change<=0
                        or premise.get('exposureDirection')=='decreasing' and change>=0):
                    return ['patient_longitudinal_direction_does_not_match_source']
        elif kind=='genetic_mechanism' and not re.search(
                r'基因(?:型|检测|变异|测序)|遗传检测|\brs\d+\b|\b[cp]\.\d|'
                r'genotyp|genetic (?:test|variant|score)|polygenic',text,re.I):
            return ['phenotypic_measurement_is_not_genetic_exposure']
        elif kind=='derived_index':
            from .evaluation_evidence_plan import _mentions
            # A list of parts is not itself an observed/derived index. An
            # actual index observation must be source-bound by its own name.
            aliases=[term for group in claim.exposure_terms for term in group]
            actual=[fact for fact in selected if (re.search(r'index|ratio|指数|评分|比值',fact.name,re.I)
                or re.fullmatch(r'[A-Z][A-Z0-9_-]{1,14}',fact.name))
                and (_mentions(fact.name,_concept_forms(aliases,premise['sourceQuote']))
                    or _defined_quote_mention(fact.name,aliases,premise['sourceQuote']))]
            if not actual:
                return ['components_do_not_establish_measured_or_derived_index']
        elif kind=='diagnostic_identification' and claim.role!='diagnostic_principle':
            return ['diagnostic_identification_is_not_incident_disease_relationship']
        elif kind=='inverse_direction':
            if row.get('patientRiskDirection')!='increasing':
                return ['opposing_relationship_is_not_positive_risk_support']
            if not any(re.search(r'偏低|降低|减少|缺乏|不足|↓',str(fact.status or '')) for fact in selected):
                return ['inverse_relation_requires_actual_reversed_exposure']
        elif kind=='comparison_population':
            scope=row.get('comparisonScope')
            if (not isinstance(scope,dict) or scope.get('duty') not in {
                    'incident_relationship','disease_reference_association','existing_disease_severity','other'}
                    or type(scope.get('allGroupsHaveTargetDisease')) is not bool
                    or scope.get('directionNotUpgraded') is not True):
                return ['scientific_comparison_population_or_inference_not_bound']
            groups=scope.get('groupQuotes')
            if (not isinstance(groups,list) or len(groups)<2 or any(not isinstance(group,str)
                    or len(group.strip())<2 or group not in originals[premise['evidenceKey']]['sourceText']
                    for group in groups)):
                return ['scientific_comparison_groups_not_literal']
            if scope.get('allGroupsHaveTargetDisease') or scope['duty']=='existing_disease_severity':
                from types import SimpleNamespace
                target=next((term for term in claim.outcome_terms if re.search(r'[\u4e00-\u9fff]',term)),
                    next(iter(claim.outcome_terms),''))
                source_claim=claim.model_copy(update={'role':'source_fact','fact_quotes':bound})
                if not target or not _explicit_personal_disease_source(source_claim,SimpleNamespace(name=target),facts):
                    return ['existing_disease_comparison_cannot_establish_first_disease_risk']
                if re.search(r'发生风险|发病风险|患病风险|风险因素|首次发生|导致.{0,12}疾病',claim.text):
                    return ['severity_group_difference_is_not_incident_disease_relationship']
            if scope['duty']=='other':
                return ['comparison_does_not_support_asserted_scientific_duty']
        elif kind=='compound_group':
            components=row.get('groupBindings')
            expected_components=premise['group']['components']
            if (not isinstance(components,list) or len(components)!=len(expected_components)
                    or any(not isinstance(component,dict) or component.get('patientMatch')!='matched'
                        or component.get('polarity')!=original['polarity']
                        or component.get('label')!=original['label']
                        or not _valid_fact_quotes(component.get('factQuotes'),facts,claim.fact_quotes)
                        for component,original in zip(components,expected_components))):
                return ['compound_source_group_conditions_not_bound']
    return []


def _specific_exposure_groups(text, explicit, known, *, outcomes=()):
    """Add only genuinely unbound exposure objects from the assertion.

    A complete outcome such as hypertension is not a second blood-pressure
    exposure. Nor is the substring cholesterol an extra assay inside an
    explicitly bound LDL measurement. Separate actual mentions still count.
    """
    def spans(term, value):
        if not isinstance(term, str) or not term:
            return []
        pattern = (r'(?<![A-Za-z0-9])' + re.escape(term) + r'(?![A-Za-z0-9])'
            if term.isascii() else re.escape(term))
        return [(match.start(), match.end()) for match in re.finditer(pattern, value, re.I)]
    surface = str(text)
    for term in sorted(_concept_forms(outcomes), key=len, reverse=True):
        for left, right in reversed(spans(term, surface)):
            surface = surface[:left] + ' ' * (right-left) + surface[right:]
    explicit_spans = [span for group in explicit for term in _concept_forms(group)
        for span in spans(term, surface)]
    occurrences=[]
    for index,aliases in enumerate(known):
        for alias in aliases:
            if isinstance(alias,str) and not alias.isascii():
                occurrences.extend((left,right,index) for left,right in spans(alias,surface)
                    if not any(start<=left and right<=end for start,end in explicit_spans))
    keep={index for start,end,index in occurrences if not any(
        left<=start and end<=right and (left<start or end<right)
        for left,right,_ in occurrences)}
    groups=[list(group) for group in explicit]
    for index,aliases in enumerate(known):
        if index in keep and not any(set(aliases)&set(group) for group in groups):
            groups.append(list(aliases))
    return groups


def _intervention_effect(text, quote):
    # Natural lower exposure / observational contrasts are different from
    # deliberately lowering a biomarker or administering an intervention.
    changed=bool(re.search(r'降低|减少|控制|治疗|干预|转向|采用|调整|坚持|增加|规律',text))
    active=bool(re.search(r'\b(?:lowering|reducing|treating|controlling|switching|administering)\b|'
        r'\b(?:treatment|intervention|therapy|management)\b.{0,80}\b(?:reduc|improv|lower)',quote,re.I))
    benefit=bool(re.search(r'\b(?:reduces?|lower(?:s|ed)?|improv(?:es?|ed)|prevent(?:s|ed)?)\b.{0,100}'
        r'\b(?:risk|events?|mortality|incidence|symptoms?|cholesterol)\b',quote,re.I))
    return changed and active and benefit


def _scientific_quote_scope_errors(claim, originals=None):
    """Check the selected anchors' duty, never borrow the paper's topic."""
    if claim.role not in {'outcome','etiology','mechanism'}:
        return []
    from .evaluation_evidence_plan import _mentions, _RISK_OBJECT_ALIASES
    quote=' '.join(anchor.quote for anchor in claim.evidence)
    def mentions(terms):
        return _mentions(quote, _concept_forms(terms)) or any(_defined_quote_mention(
            anchor.quote, _concept_forms(terms,str((originals or {}).get(anchor.evidence_key,{}).get('title') or '')+' '+
                (originals or {}).get(anchor.evidence_key, {}).get('sourceText', '')),
            str((originals or {}).get(anchor.evidence_key,{}).get('title') or '')+' '+
                (originals or {}).get(anchor.evidence_key, {}).get('sourceText', ''))
            for anchor in claim.evidence)
    errors=[]
    if any(_empirical_background_anchor(anchor.quote,(originals or {}).get(anchor.evidence_key,{}))
            for anchor in claim.evidence):
        errors.append('empirical_background_not_measured_study_relationship')
    duties={_selected_quote_scientific_duty(claim.text,anchor.quote) for anchor in claim.evidence}
    if 'diagnostic_principle' in duties or any(_same_definition_list_objects(
            anchor.quote,claim.exposure_terms,claim.outcome_terms,
            str((originals or {}).get(anchor.evidence_key,{}).get('title') or '')+' '+
            (originals or {}).get(anchor.evidence_key,{}).get('sourceText','')) for anchor in claim.evidence):
        errors.append('diagnostic_definition_not_personal_future_risk')
    if 'background' in duties:
        errors.append('background_statement_not_observed_risk_relationship')
    if (claim.role=='outcome' and re.search(
            r'符合.{0,35}(?:特征|定义|标准)|(?:表现|特征|定义)为|诊断(?:依据|组成|指标)|用于.{0,8}诊断',claim.text)
            and re.search(r'characteri[sz]ed by|defined (?:by|as)|diagnostic (?:criteria|features?)|'
                r'criteria for|特征|定义|诊断标准',quote,re.I)):
        errors.append('diagnostic_definition_not_personal_future_risk')
    if (claim.role=='outcome' and re.search(r'评估|评价|筛查|识别|诊断',claim.text)
            and re.search(r'aid.{0,35}(?:evaluation|assessment)|(?:used|diagnos\w*).{0,45}'
                r'(?:evaluation|assessment|screening)|用于.{0,20}(?:评估|评价|筛查|诊断)',quote,re.I)
            and not re.search(r'associated with (?:incident|future)|risk of developing|'
                r'(?:higher|increased).{0,12}(?:incidence|risk)|发生风险.{0,8}(?:增加|升高)',quote,re.I)):
        errors.append('diagnostic_definition_not_personal_future_risk')
    if claim.role=='outcome' and _intervention_effect(claim.text,quote):
        errors.append('intervention_effect_belongs_to_management')
    if (claim.role == 'outcome'
            and re.search(r'(?:转向|采用|调整|坚持|增加|规律).{0,25}(?:饮食|膳食|运动|快走|活动)|'
                r'(?:地中海|DASH).{0,12}(?:饮食|膳食)', claim.text, re.I)
            and re.search(r'(?:有助|能够|可以|可).{0,10}(?:改善|降低|缓解|控制|减少)', claim.text)):
        # A supported intervention benefit is useful management information;
        # it does not establish why the patient's present risk exists.
        errors.append('intervention_effect_belongs_to_management')
    if (claim.role in {'outcome','etiology','mechanism'} and (
            re.search(r'被作为.{0,12}(?:评估)?结局|评估结局|主要终点|次要终点|研究设计|随机分组',claim.text)
            or (re.search(r'primary outcome was|secondary outcomes included|were randomi[sz]ed',quote,re.I)
                and not re.search(r'significant(?:ly)? (?:improv|reduc|increas)|was associated|were associated|'
                    r'hazard ratio|odds ratio|compared.{0,50}(?:higher|lower)|improved compared',quote,re.I)))):
        errors.append('research_design_not_personal_risk_relationship')
    if (claim.role in {'outcome','etiology','mechanism'}
            and re.search(r'指南建议|建议.{0,25}(?:检查|筛查|检测|病例发现)|应.{0,8}筛查',claim.text)
            and re.search(r'recommend|should|case.find|screen',quote,re.I)
            and not re.search(r'(?:associated|increased|reduced).{0,15}(?:risk|incidence)|risk (?:of|for)',quote,re.I)):
        errors.append('management_instruction_is_not_scientific_outcome')
    if (re.search(r'for diagnosing|diagnostic (?:criteria|component)|criteria for diagnos|'
            r'(?:诊断|判定)(?:中|的|所用)?.{0,12}(?:组成|标准|指标|风险因素)',quote,re.I)
            and re.search(r'诊断|评估指标|诊断标准|诊断组成',claim.text)
            and not re.search(r'associated with (?:incident|future)|risk of developing|'
                r'predict.{0,20}(?:incidence|onset)|未来.{0,8}风险|发生风险',quote,re.I)):
        errors.append('diagnostic_definition_not_personal_future_risk')
    comparison=bool(re.search(r'\b(?:vs\.?|versus|compared (?:with|to)|than)\b|相比|比较',quote,re.I)
        and re.search(r'higher|lower|difference|greater|less|更高|更低|差异',quote,re.I))
    incident_relation=bool(re.search(r'(?:risk|incidence|hazard|odds).{0,35}(?:of|for|ratio)|'
        r'(?:associated|predict|contribut).{0,90}(?:incident|onset|develop)|发病|发生风险',quote,re.I))
    if (comparison and not incident_relation and re.search(
            r'风险因素|发生风险|发病风险|首次发生|预测.{0,10}(?:患病|发病)',claim.text)):
        errors.append('comparative_group_difference_does_not_establish_incident_risk')
    if not claim.exposure_terms or not claim.outcome_terms:
        errors.append('scientific_claim_missing_explicit_objects')
    relation=re.search(r'associat|risk factor|risk (?:of|for)|predict|contribut|caus|lead.{0,8}to|'
        r'link(?:ed)? (?:to|with)|correlat|mediat|promot|impair|alter|affect|influenc|'
        r'相关|风险因素|增加.{0,8}风险|导致|引起|促进|抑制|损伤|影响|介导',quote,re.I)
    if claim.role in {'etiology','mechanism'} and not relation:
        errors.append('etiology_or_mechanism_missing_relationship_in_selected_quote')
    if (claim.role=='outcome' and re.search(r'风险|会导致|可能导致',claim.text)
            and re.search(r'clinicians? should|first.line therap|not use|recommend.{0,40}(?:therapy|treatment)|'
                r'建议.{0,16}(?:药物|治疗)',quote,re.I) and not relation):
        errors.append('management_quote_does_not_establish_outcome')
    common=(*_RISK_OBJECT_ALIASES,
        ('饮水','饮水量','液体摄入','water intake','fluid intake','hydration'),
        ('活动不足','活动量','physical inactivity','physical activity','exercise'),
        ('夜宵','late eating','late-night eating','late-night meal','meal timing'),
        ('睡眠','睡眠不足','sleep','sleep duration'),
        ('高盐','食盐','salt','sodium'),('高油','高脂饮食','high-fat diet','dietary fat'))
    # Known bilingual objects are a completeness check against an author who
    # omits one of the multiple exposures asserted in the same sentence.
    groups=_specific_exposure_groups(claim.text,claim.exposure_terms,common, outcomes=claim.outcome_terms)
    for group in groups:
        if not group or not mentions(group):
            errors.append('asserted_exposure_missing_from_selected_quote')
    if claim.outcome_terms and not mentions(claim.outcome_terms):
        errors.append('asserted_outcome_missing_from_selected_quote')
    return list(dict.fromkeys(errors))


def _declared_risk_signal_ids(candidate,facts):
    if _risk_identity(candidate.name)!=candidate.disease_id:
        return []
    result=[]
    for fid in dict.fromkeys([*candidate.support_fact_ids,*candidate.background_fact_ids,
            *candidate.source_model_fact_ids]):
        fact=facts.get(fid)
        if fact is None:
            continue
        role=_fact_role(fact)
        name=fact.name.removesuffix('（来源疾病模型指数）')
        if (_risk_identity(name)==candidate.disease_id and (role=='source_disease_model'
                or role=='microbiome_association_screen' and fact.value in {'检出','阳性','异常'})):
            result.append(fid)
    return result


def _source_signal_claim(candidate,facts):
    ids=_declared_risk_signal_ids(candidate,facts)
    if not ids:
        return []
    fid=next((identifier for identifier in ids if _fact_role(facts[identifier])=='microbiome_association_screen'),ids[0])
    fact=facts[fid]
    text=(fact.name+'相关菌群特征检出。' if _fact_role(fact)=='microbiome_association_screen'
        else fact.name.removesuffix('（来源疾病模型指数）')+'相关风险信号需关注。')
    return [RiskClaim(claim_id='signal:'+_digest(fid)[:12],role='source_fact',text=text,fact_quotes={fid:fact.value})]


def _patient_source_role_errors(claim,facts):
    errors=[]
    for fid in claim.fact_quotes:
        fact=facts.get(fid)
        if fact is None or _fact_role(fact)!='microbiome_association_screen':
            continue
        own=[facts[identifier] for identifier in claim.fact_quotes if identifier in facts
            and facts[identifier].kind in {'clinical','self_reported'} and identifier!=fid]
        actual=any(fact.name in source.name and (source.abnormal or re.search(
            r'(?:有|出现|存在|反复|经常).{0,4}'+re.escape(fact.name),source.value)) for source in own)
        falsely_personal=re.search(r'(?:您|本人|用户|患者)(?:已|目前|现在|经常|反复)?'
            r'(?:有|出现|存在|患有|发生|确诊).{0,8}'+re.escape(fact.name),claim.text)
        if not actual and (falsely_personal or (claim.role=='source_fact' and not re.search(
                r'相关菌群|菌群筛查|菌群关联|菌群.{0,10}检出|检出.{0,10}菌群',claim.text))):
            errors.append('microbiome_screen_is_not_patient_symptom_or_diagnosis')
    return list(dict.fromkeys(errors))


def _source_observation_errors(claim,candidate,facts):
    """An adopted observation does not need a borrowed disease paper."""
    allowed=set([*candidate.support_fact_ids,*candidate.background_fact_ids,
        *candidate.opposing_fact_ids,*candidate.source_model_fact_ids])
    errors=[]
    if not _valid_fact_quotes(claim.fact_quotes,facts,allowed):
        errors.append('invalid_patient_quote')
    elif _negative_patient_clauses(claim.text):
        errors.extend(_patient_negative_predicate_errors(claim.text,claim.fact_quotes,facts))
    if set(claim.fact_quotes)&set(candidate.opposing_fact_ids):
        errors.append('opposing_fact_not_publishable_as_support')
    if _BOUNDARY.search(claim.text):
        errors.append('public_rule_text')
    if re.search(r'无已知.{0,18}(?:诊断|病史)|否认.{0,18}(?:病史|症状)|尚无.{0,12}确诊依据',claim.text):
        errors.append('negative_history_is_internal_context')
    declared=set(_declared_risk_signal_ids(candidate,facts))
    signal_claim=bool(set(claim.fact_quotes)&declared)
    if re.search(r'导致|引起|促使|诱发|危险因素|与.{0,30}相关|提示.{0,20}(?:风险|患病)|'
            r'(?:增加|降低|升高|减少).{0,20}(?:风险|发病)|符合.{0,20}(?:诊断|标准)',claim.text):
        direct_signal=signal_claim and re.search(r'相关风险|风险信号|相关菌群',claim.text) and not re.search(
            r'导致|引起|促使|诱发|危险因素|增加|降低|升高|减少|符合|患病|确诊',claim.text)
        if not direct_signal and not all(claim.text.strip('。 ') in facts[fid].value for fid in claim.fact_quotes if fid in facts):
            errors.append('source_observation_contains_new_medical_relationship')
    for fid in claim.fact_quotes:
        fact=facts.get(fid)
        if fact is None:
            continue
        if _fact_role(fact)=='source_disease_model' and fid not in declared:
            errors.append('source_model_kept_as_internal_reference')
        if (_fact_role(fact)=='microbiome_association_screen'
                and _risk_identity(fact.name) != _risk_identity(candidate.name)):
            errors.append('association_screen_endpoint_mismatch')
        if fid in declared and re.search(r'(?:您|本人|用户|患者)(?:已|目前)?(?:患有|患上|确诊|出现|存在)',claim.text):
            errors.append('reported_risk_signal_not_clinical_disease_or_symptom')
        if fact.kind=='clinical' and re.search(r'正常|阴性|未见异常',fact.status or '') and not fact.abnormal:
            errors.append('normal_clinical_fact_is_internal_context')
    errors.extend(_patient_source_role_errors(claim,facts))
    if any(facts[fid].kind in {'clinical', 'self_reported'}
            for fid in claim.fact_quotes if fid in facts):
        from .evaluation_prose_roles import source_only_clinical_proposition_errors
        errors.extend(source_only_clinical_proposition_errors(claim.text,
            [facts[fid] for fid in claim.fact_quotes if fid in facts],
            source_quotes={fid: [quote] for fid, quote in claim.fact_quotes.items()
                if fid in facts and isinstance(quote, str) and quote in facts[fid].value}))
    return list(dict.fromkeys(errors))


def _risk_source_scope_context(claim, *, subject=None):
    return {'subject': _subject_view(subject), 'application': 'individual',
        'problem_fact_ids': sorted(claim.fact_quotes)}


def _risk_source_scope_options(claim, originals):
    from .evaluation_source_scope import source_scope_options
    return {key: source_scope_options(originals.get(key, {}).get('sourceText'),
        str(originals.get(key, {}).get('title') or ''), selected_quotes=quotes)
        for key, quotes in _scope_quotes_by_source(claim).items()}


def _scope_quotes_by_source(claim):
    result = {}
    for anchor in claim.evidence:
        quotes = result.setdefault(anchor.evidence_key, [])
        if anchor.quote not in quotes:
            quotes.append(anchor.quote)
    return result


def _bind_risk_source_scope(claim, review, facts, originals, *, subject=None, document=None,
        verified_conditions=None):
    """Bind every real source qualification, including diagnostic principles."""
    from .evaluation_source_scope import validate_source_scope, source_clinical_condition_witnesses
    errors, bindings = [], {}
    context = _risk_source_scope_context(claim, subject=subject)
    scopes = review.get('sourceScopeReviews', {}) if isinstance(review, dict) else {}
    scopes = scopes if isinstance(scopes, dict) else {}
    conditions = (tuple(verified_conditions) if verified_conditions is not None else
        source_clinical_condition_witnesses(document, subject=subject) if document is not None else ())
    for key, quotes in _scope_quotes_by_source(claim).items():
        original = originals.get(key, {})
        result = validate_source_scope(source_text=original.get('sourceText'),
            title=str(original.get('title') or ''), selected_quotes=quotes,
            role=claim.role, claim=claim.text,
            patient_facts={fid: facts[fid] for fid in claim.fact_quotes if fid in facts},
            target_context=context, review_scope=scopes.get(key, {}), verified_conditions=conditions)
        if not result.valid:
            errors.extend('source_scope:' + value for value in result.errors)
        else:
            bindings[key] = _RiskSourceScopeBinding(original['sourceText'],
                str(original.get('title') or ''), json.dumps(context, sort_keys=True), result.receipt,
                tuple(witness for witness in conditions
                    if witness.authority_hash in result.receipt.condition_signatures),
                _original_scope_hash(original))
    claim._source_scope_bindings = bindings
    return list(dict.fromkeys(errors))


def _risk_source_scope_errors(claim, facts, originals, *, subject=None):
    from .evaluation_source_scope import source_scope_options, source_scope_receipt_matches
    errors = []
    for key, quotes in _scope_quotes_by_source(claim).items():
        original = originals.get(key, {})
        required = source_scope_options(original.get('sourceText'), str(original.get('title') or ''),
            selected_quotes=quotes)
        binding = claim._source_scope_bindings.get(key)
        if not required and binding is None:
            continue  # Existing independent entailment still applies to unrestricted originals.
        if (not isinstance(binding, _RiskSourceScopeBinding)
                or binding.original_scope_hash and binding.original_scope_hash != _original_scope_hash(original)
                or not source_scope_receipt_matches(
                binding.receipt, source_text=original.get('sourceText'),
                title=str(original.get('title') or ''), selected_quotes=quotes,
                role=claim.role, claim=claim.text,
                patient_facts={fid: facts[fid] for fid in claim.fact_quotes if fid in facts},
                target_context=_risk_source_scope_context(claim, subject=subject),
                verified_conditions=binding.verified_conditions)):
            errors.append('source_scope_receipt_missing_or_changed')
    return errors


def _conservative_observations(candidate,facts):
    """Keep a genuine adopted problem when a draft mislabels its observations."""
    if candidate.certainty not in {'confirmed','current_problem','self_reported'}:
        observed=[RiskClaim(claim_id='source:'+_digest(fid)[:10],role='source_fact',
            text=facts[fid].name+' '+facts[fid].value,fact_quotes={fid:facts[fid].value})
            for fid in candidate.support_fact_ids if fid in facts and facts[fid].kind=='clinical' and facts[fid].abnormal]
        if not _same_observed_problem(candidate,observed,facts):
            return []
    result=[]
    for fid in candidate.support_fact_ids:
        fact=facts.get(fid)
        if fact is None or fact.kind not in {'clinical','self_reported'}:
            continue
        if fact.kind=='clinical':
            if not fact.abnormal and not re.search(r'偏高|偏低|升高|降低|阳性|异常',fact.status or ''):
                continue
            text=fact.name+' '+fact.value+(fact.unit or '')
            if fact.reference_range:
                text+='（参考'+fact.reference_range+'）'
            if fact.status:
                text+='，'+fact.status
            quote=fact.value
        else:
            quote=fact.name if fact.name in fact.value else fact.value.split('回答：')[-1]
            text=quote
        claim=RiskClaim(claim_id='observation:'+_digest(fid)[:12],text=text.strip('。 ')+'。',
            role='source_fact',fact_quotes={fid:quote})
        if not _source_observation_errors(claim,candidate,facts):
            result.append(claim)
        if len(result)>=3:
            break
    return result


def _same_observed_problem(candidate,claims,facts):
    if getattr(candidate, 'entity_kind', None) == 'disease_risk':
        return False  # A symptom alias is not a new diagnostic decision.
    """Recognise a current source abnormality, not its future disease outcome."""
    if candidate.disease_id!=_risk_identity(candidate.name):
        return False
    if _lipid_subtype_name(candidate.name):
        supplied = [facts[fid] for claim in claims if claim.role == 'source_fact'
            for fid in claim.fact_quotes if fid in facts]
        if not _literal_lipid_subtype(candidate.name, supplied):
            return False  # A new subtype needs diagnostic-criteria review.
    from .evaluation_evidence_plan import risk_retrieval_concepts,_mentions
    base=re.sub(r'(?:相关|发生|易感)?风险$','',candidate.name)
    names=risk_retrieval_concepts([base])
    for claim in claims:
        if claim.role!='source_fact':
            continue
        for fid in claim.fact_quotes:
            fact=facts.get(fid)
            if fact is None or not _clinical_observed_abnormal(fact) or re.search(r'未见|没有|否认|阴性',fact.value):
                continue
            if _risk_identity(fact.name)==candidate.disease_id or _mentions(fact.name,names):
                return True
            # Dyslipidaemia is the adopted lipid abnormality itself. It is
            # not permission to infer CAD or diabetes from the same marker.
            if candidate.disease_id=='hyperlipidemia':
                status=str(fact.status or '')
                high=re.fullmatch(r'甘油三酯|总胆固醇|低密度脂蛋白(?:胆固醇)?|TG|TC|LDL(?:-C)?',fact.name,re.I)
                low=re.fullmatch(r'高密度脂蛋白(?:胆固醇)?|HDL(?:-C)?',fact.name,re.I)
                if high and not re.search(r'偏低|降低|↓',status) or low and re.search(r'偏低|降低|↓',status):
                    return True
            if candidate.disease_id=='centralObesity' and fact.name in {'腰围','waist circumference','WC'}:
                if not re.search(r'偏低|降低|↓',str(fact.status or '')):
                    return True
    return False


def _role_evidence_errors(claim, originals):
    errors = []
    for anchor in claim.evidence:
        quote = anchor.quote
        # A component definition is not evidence that the component will
        # develop, and the observed outcome cannot be reversed into exposure.
        definition = re.search(r'defined by|characterized by|components? of|定义为|组成(?:部分|要素)', quote, re.I)
        relation = re.search(r'associat|risk of|risk for|increas|reduc|predict|导致|相关|风险|降低|增加', quote, re.I)
        if claim.role in {'outcome','etiology','mechanism'} and definition and not relation:
            errors.append('definition_does_not_establish_relationship')
        endpoint=re.search(r'risk of (?:being diagnosed with|developing) (.{2,100}?)'
            r'(?:was|were|is) higher in (.{4,300})',quote,re.I)
        if endpoint:
            for english,chinese in (('hypertension','高血压'),('diabetes','糖尿病'),
                    ('obesity','肥胖'),('dyslipidemia','血脂异常')):
                asserted=re.search(re.escape(chinese)+r'(?:的)?(?:发生|患病|风险)|(?:发生|罹患|患上)'+re.escape(chinese),claim.text)
                if (asserted and re.search(english,endpoint[2],re.I)
                        and not re.search(english,endpoint[1],re.I)):
                    errors.append('association_outcome_reversed')
        if claim.role == 'management':
            original=originals.get(anchor.evidence_key,{})
            from .evaluation_evidence_plan import management_actionable_content,management_principle_errors
            trial=bool(re.search(r'random|trial|systematic|meta.analysis',str(original.get('studyType'))+' '+str(original.get('title')),re.I))
            errors.extend(management_principle_errors(anchor.quote,human_trial=trial))
            scope_statement=re.search(r'\b(?:aims?|focus|scope|purpose|objectives?)\b|'
                r'guideline.{0,30}(?:address|cover|include)|(?:指南|报告|文章).{0,18}(?:范围|旨在|目的|涵盖)',anchor.quote,re.I)
            if scope_statement and not management_actionable_content(anchor.quote,human_trial=trial):
                errors.append('management_scope_only')
            definition=original.get('sourceText','')
            if re.search(r'Mediterranean diet\s*\(MD\)|MD\s*\(Mediterranean diet\)',definition,re.I):
                quote=re.sub(r'\bMD\b','Mediterranean diet',quote)
            if not re.search(r'recommend|should|advis|physical activity|exercise|diet|intake|lifestyle|monitor|'
                    r'follow.up|screen|建议|推荐|运动|锻炼|饮食|膳食|摄入|监测|复查|随访|筛查',quote,re.I) and not (
                    management_actionable_content(quote,human_trial=trial)):
                errors.append('management_not_supported_by_action_quote')
            general_objects=(_management_objects(quote,general_principle=True)
                if _general_management_principle(quote) else set())
            domains = (
                (r'运动|快走|锻炼|有氧|活动量',r'physical activity|exercise|training|walking|sedentary|运动|活动|锻炼',{'activity'}),
                (r'夜宵|晚餐|外卖|蔬菜|水果|全谷|饮食',r'diet|food|meal|fiber|fibre|nutrition|fruit|vegetable|grain|饮食|膳食|进食|食物|纤维',{'diet','meal_timing'}),
                (r'复查|监测|筛查|就诊|至.{0,8}科',r'monitor|follow.up|screen|recommend|should|advis|评估|复查|监测|筛查|建议',{'clinical'}),
            )
            if any(re.search(pattern,claim.text) and not re.search(required,quote,re.I)
                    and not objects&general_objects for pattern,required,objects in domains):
                errors.append('specific_action_not_present_in_selected_original')
            specifics=(
                (r'(?:限制|控制|减少|降低|削减).{0,8}(?:能量|热量|卡路里)|能量缺口',
                    r'calori|energy.{0,16}(?:intake|restrict|deficit|balance)|hypocalor'),
                (r'全谷物|全谷类',r'whole.{0,3}grain|全谷'),
                (r'豆类',r'legume|pulses?|beans?|豆类'),
                (r'蔬菜',r'vegetable|蔬菜'),(r'水果',r'fruit|水果'),
                (r'限盐|减盐|低盐|控制.{0,4}盐|减少.{0,4}盐',r'sodium|salt|钠|盐'),
                (r'限糖|减糖|少糖|减少.{0,5}(?:糖|甜)',r'sugar|sweeten|糖'),
                (r'腰围测量|测量腰围|测量.{0,8}评估',r'measur|assess|screen|测量|评估'),
            )
            if any(re.search(pattern,claim.text) and not re.search(required,quote,re.I)
                    and not _management_objects(re.search(pattern,claim.text).group())&general_objects
                    for pattern,required in specifics):
                errors.append('specific_management_predicate_not_supported_by_selected_quote')
            if re.search(r'饮水|喝水|饮水量',claim.text) and re.search(
                    r'associat|more likely|correlat|participants',quote,re.I) and not re.search(
                    r'recommend|should|advis|trial|intervention|effective|improv|建议|推荐',quote,re.I):
                errors.append('observational_water_association_not_management_recommendation')
            trial_form=str(original.get('title') or '')+' '+str(original.get('studyType') or '')
            isolated=(re.search(r'supplement|randomi[sz]ed|\btrial\b',trial_form,re.I)
                and re.search(r'(?:intake|supplement|administration).{0,35}\d+\s*(?:g|mg|grams?)\b',quote,re.I)
                and not re.search(r'food|fruit|vegetable|whole.grain|meal',quote,re.I))
            if isolated and re.search(r'含.{0,20}(?:膳食|食物|食品)|膳食纤维来源',claim.text):
                errors.append('isolated_supplement_trial_not_food_source_recommendation')
    return list(dict.fromkeys([*errors,*_scientific_quote_scope_errors(claim, originals)]))


def _management_patient_scope_errors(claim,candidate,facts,originals,*,current_context=()):
    if claim.role!='management':
        return []
    from .evaluation_evidence_plan import _mentions,risk_retrieval_concepts
    patient=_patient_premise_text(claim,candidate,facts)
    patient+=' '+' '.join(' '.join(row.get('aliases',[])) for row in current_context)
    for anchor in claim.evidence:
        source=originals.get(anchor.evidence_key,{})
        conditional=re.search(r'do not respond|non.?respond|if the diagnosis is unclear|'
            r'if.{0,60}(?:ineffective|outlet obstruction)',anchor.quote,re.I)
        procedure=re.search(r'endoscop|manometr|pH monitoring|balloon expulsion|defecograph',anchor.quote,re.I)
        if conditional and procedure:
            stated=re.search(r'(?:治疗|处理|干预).{0,12}(?:无效|未缓解|无改善)|诊断(?:仍|尚)?(?:不明|不清|不明确)|明确.{0,5}出口梗阻',claim.text)
            actual=re.search(r'(?:治疗|处理|干预).{0,12}(?:无效|未缓解|无改善)|诊断(?:仍|尚)?(?:不明|不清|不明确)|明确.{0,5}出口梗阻',patient)
            if not stated and not actual:
                return ['conditional_procedure_requires_actual_or_preserved_condition']
        population=re.search(r'(?:patients|adults|people) with ([^:;]{3,150})',str(source.get('title') or ''),re.I)
        if not population:
            continue
        terms=risk_retrieval_concepts([population[1]])
        terms=tuple(dict.fromkeys([*terms,*re.findall(r'\(([A-Z][A-Z0-9-]{1,12})\)',population[1])]))
        if _mentions(patient,terms):
            continue
        # Actual same-problem symptoms can support ordinary daily management;
        # a source-only risk signal cannot stand in for an established organ
        # disease when borrowing that disease population's treatment effect.
        if candidate.certainty in {'current_problem','self_reported','confirmed'}:
            target=risk_retrieval_concepts([candidate.name,*candidate.search_terms])
            condition=any(_same_observed_problem(candidate,[RiskClaim(claim_id='condition:'+fid,
                text=fact.name+' '+fact.value,role='source_fact',fact_quotes={fid:fact.value})],facts)
                for fid,fact in facts.items() if fact.kind=='clinical')
            symptom=(candidate.certainty=='self_reported' and any(fid in facts and facts[fid].kind=='self_reported'
                and (fid.startswith('self-reported-fact-') or _risk_identity(facts[fid].name)==candidate.disease_id)
                for fid in candidate.support_fact_ids))
            matched=(_mentions(population[1],target) or any(len(_key(term))>=8
                and _key(term) in _key(population[1]) for term in target))
            if matched and (condition or symptom):
                continue
        if re.search(r'改善|逆转|缓解|降低|减轻|减少|升高|恢复',claim.text):
            return ['disease_treatment_effect_without_patient_condition']
    return []


def _assessment_errors(assessment, candidate, facts, originals):
    errors = []
    if _workflow_endpoint(assessment.name) or _workflow_endpoint(candidate.name):
        errors.append('workflow_target_is_not_a_disease')
    if _nonspecific_clinical_endpoint(assessment.name) or _nonspecific_clinical_endpoint(candidate.name):
        errors.append('nonspecific_clinical_endpoint_requires_repair')
    if (_lipid_subtype_name(candidate.name) and candidate.certainty in {'confirmed', 'current_problem'}
            and not _literal_lipid_subtype(candidate.name, [facts[fid] for claim in assessment.claims
                if claim.role == 'source_fact' for fid in claim.fact_quotes if fid in facts])
            and not any(_personal_lipid_classification(claim, candidate.name, facts, originals)
                for claim in assessment.claims)):
        errors.append('new_lipid_subtype_requires_personal_guideline_classification')
    if (assessment.risk_id, assessment.disease_id, assessment.name, assessment.category, assessment.certainty) != (
            candidate.risk_id, candidate.disease_id, candidate.name, candidate.category, candidate.certainty):
        errors.append('candidate_identity_changed')
    if assessment.level != risk_level(assessment.index):
        errors.append('index_level_mismatch')
    allowed = set([*candidate.support_fact_ids, *candidate.opposing_fact_ids,
        *candidate.background_fact_ids, *candidate.source_model_fact_ids])
    for ids in (assessment.support_fact_ids, assessment.opposing_fact_ids,
            assessment.background_fact_ids, assessment.source_model_fact_ids):
        if not set(ids) <= allowed or not set(ids) <= facts.keys():
            errors.append('unbound_fact')
    if not set(candidate.opposing_fact_ids) <= set(assessment.opposing_fact_ids):
        errors.append('counterevidence_omitted')
    if set(candidate.source_model_fact_ids) != set(assessment.source_model_fact_ids):
        errors.append('source_models_omitted')
    if set(assessment.support_fact_ids) & set(assessment.opposing_fact_ids):
        errors.append('support_opposition_overlap')
    claims = {claim.claim_id: claim for claim in assessment.claims}
    if len(claims) != len(assessment.claims):
        errors.append('duplicate_claim_id')
    used_keys = set()
    supported = set()
    for claim in assessment.claims:
        if not _valid_fact_quotes(claim.fact_quotes, facts, allowed):
            errors.append('invalid_patient_quote')
        if claim.role=='source_fact':
            errors.extend(_source_observation_errors(claim,candidate,facts))
        supported.update(claim.fact_quotes)
        if _BOUNDARY.search(claim.text):
            errors.append('public_rule_text')
        if set(claim.fact_quotes) & set(assessment.opposing_fact_ids):
            errors.append('counterevidence_in_public_prose')
        if claim.role != 'source_fact' and not claim.evidence:
            errors.append('medical_relation_missing_original_evidence')
        errors.extend(_role_evidence_errors(claim, originals))
        errors.extend(_relationship_endpoint_errors(claim,candidate,facts,originals))
        errors.extend(_management_patient_scope_errors(claim,candidate,facts,originals))
        errors.extend(_patient_source_role_errors(claim,facts))
        if claim.role in {'outcome','etiology','mechanism'}:
            from .evaluation_evidence_plan import _mentions
            patient_text=_patient_premise_text(claim,candidate,facts)
            from .evaluation_evidence_plan import risk_retrieval_concepts
            if any(not _mentions(patient_text,_concept_forms(group)) for group in claim.exposure_terms):
                errors.append('exposure_metadata_not_bound_to_assertion_or_patient')
            if set(claim.fact_quotes)-_scientific_premise_ids(claim,candidate,facts):
                errors.append('scientific_claim_contains_unbound_background_fact')
            # Match the same disease aliases/qualifiers as the draft gate;
            # assembling an approved claim must not change its endpoint test.
            if claim.outcome_terms and not _mentions(claim.text+' '+candidate.name,_concept_forms(claim.outcome_terms)):
                errors.append('outcome_metadata_not_bound_to_asserted_risk')
        for anchor in claim.evidence:
            source = originals.get(anchor.evidence_key)
            if source is None or anchor.quote not in source['sourceText']:
                errors.append('invalid_evidence_quote')
            used_keys.add(anchor.evidence_key)
    if not set(assessment.support_fact_ids) <= supported:
        errors.append('supporting_facts_not_reviewable')
    if set(assessment.evidence_keys) != used_keys:
        errors.append('evidence_key_mismatch')
    if not set(assessment.opposing_evidence_keys) <= originals.keys():
        errors.append('unknown_opposing_evidence')
    errors.extend(_unsupported_food_context(assessment.scoring_rationale,facts))
    if _foreign_model_language(assessment.scoring_rationale,candidate):
        errors.append('different_disease_model_cannot_be_named_as_this_risk_model')
    # A whole paragraph cannot smuggle an unreviewed assertion between claims.
    public_texts = [claim.text.strip() for claim in assessment.claims if claim.role != 'management']
    if assessment.conclusion.strip() != ''.join(public_texts):
        errors.append('conclusion_contains_unreviewed_text')
    if assessment.certainty == 'potential_risk' and not any(claim.role in {'outcome', 'etiology', 'mechanism'}
            and any(anchor.direction in {'support', 'mixed'} for anchor in claim.evidence)
            for claim in assessment.claims) and not any(_personal_clinical_disease_assessment(
                claim, candidate, facts, originals) for claim in assessment.claims) and not any(claim.role=='source_fact' and
                set(claim.fact_quotes)&set(_declared_risk_signal_ids(candidate,facts)) for claim in assessment.claims):
        errors.append('potential_risk_has_no_supported_relation')
    for action in [*assessment.lifestyle_actions, *assessment.clinical_actions]:
        selected = [claims.get(identifier) for identifier in action.claim_ids]
        planned = _approved_management_plan(action, assessment, facts, originals)
        if (not planned and not selected or any(claim is None or claim.role != 'management' for claim in selected)
                or not planned and action.text.strip() != ''.join(claim.text.strip() for claim in selected if claim is not None)
                or not planned and set(action.fact_ids) != {fid for claim in selected if claim for fid in claim.fact_quotes}
                or set(action.evidence_keys) != {anchor.evidence_key for claim in selected if claim for anchor in claim.evidence}):
            errors.append('action_not_grounded_in_reviewed_claims')
        if not _action_is_executable(action.text):
            errors.append('management_principle_is_not_executable_action')
        if re.search(r'PFMT|MTPN|(?:粪菌|菌群)移植|药物治疗|药物联用|每日剂量|疗程|'
                r'(?:服用|口服|注射|使用)[^。；]{0,16}(?:药|毫克|片|粒|补剂|益生菌)|'
                r'(?:补充)[^。；]{0,8}(?:益生菌|维生素|矿物质|补剂)', action.text, re.I):
            errors.append('action_exceeds_lifestyle_or_examination_scope')
    return list(dict.fromkeys(errors))


def _action_is_executable(text):
    """A class of interventions is not an instruction for this person."""
    for sentence in re.split(r'[。；;\n]', str(text)):
        # These subjects describe a risk/effect, not the user's next step.
        if re.search(r'(?:风险|发生率|患病率|概率|危险性).{0,5}(?:增加|减少|降低|升高|降至|减至|减到|增至)|'
                r'(?:增加|减少|降低|升高).{0,5}(?:风险|发生率|患病率|概率)', sentence):
            sentence = re.sub(r'(?:风险|发生率|患病率|概率|危险性).{0,5}(?:增加|减少|降低|升高|降至|减至|减到|增至)|'
                r'(?:增加|减少|降低|升高).{0,5}(?:风险|发生率|患病率|概率)', '', sentence)
        if re.search(r'(?:改为|改成|换成|替换|转向|提前|延后|减到|减至|降至|'
                r'选择|优先选|选用|采用|增加|减少|限制|避免|安排|记录|复查|测量|监测|预约|就诊|'
                r'不(?:立即)?(?:躺下|平卧)|前往|到.{0,12}科|至.{0,12}科|快走|散步|起身|中断)[^。；;]{1,80}', sentence):
            if not re.search(r'^\s*(?:对|针对).{0,25}(?:成人|人群|患者)可(?:提供|转介)', sentence):
                return True
        if re.search(r'不(?:立即)?(?:躺下|平卧)(?=$|[。；;])',sentence):
            return True
        if re.search(r'(?:保证|使|安排|保持)(?:每晚|夜间|规律地?)?睡眠(?:达到|不少于|至少|充足)', sentence):
            return True
    return False


def _ordinary_clinical_decision_spans(text):
    """A doctor's decision about treatment is not a treatment instruction."""
    if not re.search(r'医生|医师|专科|门诊|(?:至|到).{0,12}科',text):
        return []
    return [match.group() for match in re.finditer(
        r'(?:评估|讨论|判断|明确|确定)(?:是否|有无|需不需要|需否)[^，,。；;]{0,45}'
        r'(?:药物干预|药物治疗|用药|治疗|检查)[^，,。；;]{0,25}',text)
        if not re.search(r'服用|口服|注射|补剂|益生菌|每日剂量|毫克|\bmg\b|疗程|手术|活检|内镜|胃镜|肠镜',match.group(),re.I)]


def _management_plan_digest(action, assessment, facts, originals):
    claims={claim.claim_id:claim for claim in assessment.claims}
    return _digest({'riskId':assessment.risk_id,'diseaseId':assessment.disease_id,
        'action':action.model_dump(mode='json',by_alias=True),
        'actionScope':action._planning_witness,
        'principles':[claims[key].model_dump(mode='json',by_alias=True)
            for key in action.claim_ids if key in claims],
        'facts':[_fact_view(facts[fid]) for fid in action.fact_ids if fid in facts],
        'sources':{key:originals[key]['sourceText'] for key in action.evidence_keys if key in originals}})


def _approved_management_plan(action, assessment, facts, originals):
    return bool(action._planning_witness.get('version') == 'management-scope-v3'
        and action._planning_proof and action._planning_proof ==
        _management_plan_digest(action, assessment, facts, originals))


def _bind_initial_management_actions(assessment, facts, originals):
    """Keep the existing semantic receipt when an atomic action is adopted."""
    claims={claim.claim_id:claim for claim in assessment.claims}
    for action in [*assessment.lifestyle_actions,*assessment.clinical_actions]:
        if _approved_management_plan(action,assessment,facts,originals):
            continue
        selected=[claims.get(identifier) for identifier in action.claim_ids]
        if len(selected)!=1 or selected[0] is None:
            continue
        claim=selected[0];witness=claim._management_witness
        if (claim.role!='management' or witness.get('version')!='management-scope-v3'
                or witness.get('claimDigests',{}).get(claim.claim_id)!=_digest(claim.model_dump(mode='json',by_alias=True))
                or action.text.strip()!=claim.text.strip()
                or not _valid_fact_quotes(claim.fact_quotes,facts,claim.fact_quotes)
                or any(anchor.evidence_key not in originals or witness.get('sourceHashes',{}).get(anchor.evidence_key)
                    !=_digest(originals[anchor.evidence_key]['sourceText']) for anchor in claim.evidence)):
            continue
        action._planning_witness=dict(witness)
        action._planning_proof=_management_plan_digest(action,assessment,facts,originals)


def _separate_actions(assessment, candidate, facts, originals):
    """Reject a whole independent action without discarding a valid core."""
    allowed=set([*candidate.support_fact_ids,*candidate.opposing_fact_ids,
        *candidate.background_fact_ids,*candidate.source_model_fact_ids])
    reviewed_plan_claims = {identifier for action in [*assessment.lifestyle_actions,*assessment.clinical_actions]
        if _approved_management_plan(action,assessment,facts,originals) for identifier in action.claim_ids}
    retained=[]
    rejected=set()
    for claim in assessment.claims:
        if claim.role!='management':
            retained.append(claim)
            continue
        own_allowed = allowed | (set(claim.fact_quotes) if claim.claim_id in reviewed_plan_claims else set())
        valid=(_valid_fact_quotes(claim.fact_quotes,facts,own_allowed) and claim.evidence
            and not _role_evidence_errors(claim,originals)
            and all(anchor.evidence_key in originals and anchor.quote in originals[anchor.evidence_key]['sourceText']
                for anchor in claim.evidence))
        if valid:
            retained.append(claim)
        else:
            rejected.add(claim.claim_id)
    result=assessment.model_copy(deep=True)
    result.claims=retained
    by_id={claim.claim_id:claim for claim in retained}
    used=set()
    for field in ('lifestyle_actions','clinical_actions'):
        accepted=[]
        for offset,action in enumerate(getattr(result,field)):
            selected=[by_id.get(identifier) for identifier in action.claim_ids]
            planned = _approved_management_plan(action,result,facts,originals)
            valid=((bool(selected) or planned and field=='clinical_actions')
                and all(claim and claim.role=='management' for claim in selected)
                and _action_is_executable(action.text)
                and (planned or (
                    action.text.strip()==''.join(claim.text.strip() for claim in selected if claim)
                    and set(action.fact_ids)=={fid for claim in selected if claim for fid in claim.fact_quotes}))
                and set(action.evidence_keys)=={a.evidence_key for claim in selected if claim for a in claim.evidence}
                and not re.search(r'PFMT|MTPN|(?:粪菌|菌群)移植|药物治疗|每日剂量|疗程|'
                    r'(?:服用|口服|注射|使用).{0,16}(?:药|毫克|片|粒|补剂|益生菌)|'
                    r'补充.{0,8}(?:益生菌|维生素|矿物质|补剂)',action.text,re.I))
            if valid:
                accepted.append(action);used.update(action.claim_ids)
            else:
                rejected.add(field+':'+str(offset))
        setattr(result,field,accepted)
    result.claims=[claim for claim in retained if claim.role!='management' or claim.claim_id in used]
    result.evidence_keys=list(dict.fromkeys(anchor.evidence_key for claim in result.claims for anchor in claim.evidence))
    return result,rejected


async def _review_claim_semantics(client, rows, candidates, facts, originals, *, strict_sources=False,subject=None):
    """Second, short-scope entailment review, independent of score approval."""
    items=[]
    management_context=_management_current_context(rows,candidates,facts)
    for row in rows:
        for claim in row.claims:
            items.append({'id':row.risk_id+':'+claim.claim_id,'riskId':row.risk_id,
                'claimId':claim.claim_id,'claim':claim.text,'role':claim.role,
                'riskName':candidates[row.risk_id].name,
                'sourceDeclaredRiskFactIds':_declared_risk_signal_ids(candidates[row.risk_id],facts),
                'patientFacts':[_fact_view(facts[fid]) for fid in claim.fact_quotes],
                'factQuotes':claim.fact_quotes,'selectedQuotes':{a.evidence_key:a.quote for a in claim.evidence},
                'selectedQuoteGroups':_scope_quotes_by_source(claim),
                'exposureTerms':claim.exposure_terms,'outcomeTerms':claim.outcome_terms,
                'sourcePrerequisites':_source_applicability_requirements(claim,originals),
                'sourceScopeOptions': _risk_source_scope_options(claim, originals),
                'candidateCertainty':candidates[row.risk_id].certainty,
                'personalClassificationRequired':_personal_clinical_disease_assessment(
                    claim,candidates[row.risk_id],facts,originals),
                'sourceDefinedGroups':{anchor.evidence_key:_source_group_definitions(
                    str(originals.get(anchor.evidence_key,{}).get('title') or '')+' '+
                    originals.get(anchor.evidence_key,{}).get('sourceText','')) for anchor in claim.evidence},
                'evidenceKeys':[a.evidence_key for a in claim.evidence]})
            if claim.role=='management':
                items[-1]['managementPlan']={'clauses':_management_clauses(claim.text),
                    'currentProblemContext':management_context,
                    'patientContext':[_fact_view(fact) for fact in facts.values()
                        if fact.kind in {'clinical','self_reported'}],
                    'sourceScopes':_management_source_scope_requirements(claim,originals),
                    'principles':[{'principleId':'P1','quote':anchor.quote,'evidenceKey':anchor.evidence_key}
                        for anchor in claim.evidence]}
    base_payload={'task':'review_integrated_risk_claim_semantics','subject':_subject_view(subject),'items':items,
            'evidence':[_model_original(originals[key], selected_quotes=[
                quote for item in items for quote in item['selectedQuoteGroups'].get(key, [])])
                for key in {key for item in items for key in item['evidenceKeys']}],
            'rules':[
                '本任务只核对每条原子句的原文蕴含与患者适配，不看评分是否合理，不沿用先前valid结论。',
                '定义/诊断组成不证明个人未来风险；暴露与结局不可倒置。研究已患某病者的结局不能套用于未患该病者。论文无需包含本人的具体数值，数值由patientFacts证明。',
                '区分已患病人群的治疗/预后与病例对照中的疾病相关特征：同部位、同量测形态且本人同方向的病例对照/横断面关联，可忠实用于潜在相关特征，不要求本人先确诊该病；但不能升级因果、前瞻预测或患病概率。混合皮肤/粪便多样性须确认所用片段实际部位；菌群预测免疫/代谢评分不等于人体免疫细胞或血中浓度。',
                'source_fact仅可准确转述真实资料；diagnostic_principle可解释适用诊断/评估标准但不能将来源模型当本人确诊。关联研究不升级因果；机制研究不自动确认个人机制异常。',
                'outcome/etiology/mechanism每条只说一个实际暴露关系；exposureTerms中每组为同一暴露的准确中英同义词，outcomeTerms为该结局的同义词。每组暴露与结局必须出现在所选quote而不是仅出现在整篇论文，并对应患者事实及实际断言。一般生活方式治疗有效、不用某药作为一线，不能证明某饮食/睡眠行为是该疾病病因。',
                'management必须由所选原文支持具体动作和适用条件；疾病定义、危险因素列表、观察性关联不能用作运动/饮食/复查安排的依据。管理人群必须读完整来源scope，不能把已有MAFLD等疾病患者随访建议借给只有腰围或血脂异常者。',
                '普通饮食结构、规律活动、排便习惯等日常生活管理，可依据可信本人便秘等实际自述与一般适用指南，不要求先取得专病确诊。不能把缺少功能性便秘或IBS正式诊断一概列为这类普通建议的missingCondition；特定疾病治疗、菌株疗效、药物/补剂剂量仍须原文的真实适用前提。不得将自述症状升级为确诊，也不承诺未证明的个体疗效。',
                '先独立判定每句实际职责role，再判原文是否完整蕴含；role不是复写作者标签。只属于定义、诊断确认、筛查指标、同一标准中的组成/共现列表，返回diagnostic_principle；治疗效果或具体建议返回management；背景重要性/研究介绍不属于发生风险，不能返回outcome。不得因作者已标outcome而批准。逐条返回id、role、entailsClaim、populationApplicable、directionPreserved、factQuotes、evidenceQuotes、missingConditions、unsupportedAssertions。所有绑定逐字核对。',
                'sourceDefinedGroups是本篇原文明确的复合组别对应，+/-及参考组条件均保留；对象词命中不等于整组条件已满足。逐一核对患者是否适用、哪些阳性/阴性条件未知，不能把组合效应压成单个裸暴露、忽略DS-/DS+或参考组，也不能跨论文借组别定义。',
                'comparison_population逐项区分真实研究比较职责：incident_relationship、disease_reference_association、existing_disease_severity或other，复制真实groupQuotes，判断比较组是否都已有本项疾病。两组都已有目标病的严重度差异不能推未患人的首次患病风险；疾病与健康对照的相关特征可以忠实关联但不能升为因果或前瞻发病预测。已有目标病的条件必须绑定真实本人诊断事实，不能用当前暴露数值或候选病名自证。',
                '先独立阅读sourcePrerequisites对应的逐字原文，核对科学暴露的测量形态、时间变化、计算构成、遗传/干预条件及实际结局。再逐项输出sourcePremiseBindings，echo原premiseId/evidenceKey/sourceQuote，只有真实patientFacts的逐字factQuotes满足完整前提才patientMatch=matched，未知关键条件写unknown/missingConditions，不以claim或候选标题自证。',
                '先区分所选原文是本研究实际结果、权威综述的综合结论，还是原始研究的BACKGROUND/目的介绍。BACKGROUND不能充作该研究测得的关联；GWAS/cis-eQTL/风险变异共定位/RNA-seq/MR的实际暴露和推断层级必须按全文判断，不能用普通腰围代替本人已测遗传/表达异常，也不能把遗传工具估计直接改为个人未来发生概率。没有适用的实际关系可独立返回role=background或unsupported并拒绝该句，不强行在科学role中选一。',
                '单次偏高不等于随时间持续增长；成分异常不等于完整综合指数已测或已真实派生；普通血脂值不等于遗传分组；疾病诊断识别/阈值/筛查目标不等于未来发生关联；治疗有效属于management。原文中的反向关系只内部制约，除非本人实际暴露方向反转且完整适用，不得反着当正向风险依据。不得把限制句裁掉后强化结论。',
                '定义或筛查引用有AND、co-occurring、in addition to BMI等联合条件时，句子须保留完整适用逻辑；只讲腰围而省略BMI或低肌量/功能前提，entailsClaim=false。定义可以作为适用标准，但不增加另一疾病的发生风险。科学句若只说相关而原文未给方向，不得拿患者偏高自动解释为风险增加。',
            ],'outputSchema':{'items':[{'id':'原ID','role':'按句子与原文独立判定source_fact|diagnostic_principle|outcome|etiology|mechanism|management|background|unsupported','entailsClaim':True,
                'populationApplicable':True,'directionPreserved':True,'factQuotes':{},'evidenceQuotes':{},
                'evidenceQuoteGroups':{'实际evidenceKey':['该原文全部selectedQuoteGroups逐字句，不能只回最后一条']},
                'sourcePremiseBindings':[{'premiseId':'输入实际ID','evidenceKey':'本篇真实键',
                    'sourceQuote':'输入逐字前提','patientMatch':'matched|unknown|mismatch',
                    'factQuotes':{'真实ID':'逐字个人前提'},'missingConditions':[],
                    'patientRiskDirection':'increasing|decreasing|unknown',
                    'comparisonScope':{'duty':'incident_relationship|disease_reference_association|existing_disease_severity|other',
                        'groupQuotes':['本篇实际比较组逐字名','实际参考组逐字名'],
                        'allGroupsHaveTargetDisease':False,'directionNotUpgraded':True},
                    'groupBindings':[{'label':'原组名','polarity':'+或-',
                        'patientMatch':'matched|unknown|mismatch','factQuotes':{}}]}],
                'missingConditions':[],'unsupportedAssertions':[]}]}}
    if any(item['personalClassificationRequired'] for item in items):
        base_payload['rules'].append('personalClassificationRequired=true只核本句是否完整按原始适用指南将真实个人资料分类为该具体疾病或其可能性；须独立列出全部必要criteria/条件并逐一引用本篇sourceQuote和实际factQuotes。不是定义、下游风险、参考范围越界或源模型自证。缺时长/联合条件/量测条件就missingConditions，不能用作者标题补齐；current_problem不等于确诊，potential_risk保留可能性措辞。')
        base_payload['rules'].append('个人分类按candidateCertainty区分职责：current_problem/confirmed需current_classification且全部实际诊断必要条件完整满足、unconfirmedDiagnosticConditions=[]；potential_risk可采用potential_assessment与diagnosisNotAsserted=true，只根据适用指南的真实症状模式提出有据可能性，未具备的正式确诊条件单列unconfirmedDiagnosticConditions，不强迫先确诊才能评估风险。completeCriteria指本句谨慎结论所需条件完整，不能借potential省略真正入组/个人适用前提；句子不得声称正式标准已满足或已经患病。')
        base_payload['rules'].append('potential_assessment必须分别审查：assessmentBasisComplete/cautiousConclusionSupported表示适用原文和真实本人模式足以支持本句谨慎可能性；requiredAssessmentConditionsMissing列本句谨慎评估本身尚缺的必要前提。completeCriteria可为false表示正式确诊条件未齐，这些条件仅列unconfirmedDiagnosticConditions，不自动否决有据可能性。缺少适用目标疾病的原文、真实症状模式不匹配、只有不相关定义或已有患者严重度比较，仍必须拒绝；不能用这些新标记代替逐字criterionBindings或原文适用审核。OR诊断分支只需实际采用分支，不把未采用的并发症分支也强制为必要条件。')
        base_payload['outputSchema']['items'][0]['personalClassification']={
            'targetDisease':'本句实际目标疾病','completeCriteria':True,'allConditionsChecked':True,
            'missingConditions':[],'factQuotes':{},
            'conclusionUse':'current_classification|potential_assessment','diagnosisNotAsserted':True,
            'unconfirmedDiagnosticConditions':['未满足的正式确诊条件；仅potential可保留'],
            'assessmentBasisComplete':True,'cautiousConclusionSupported':True,
            'requiredAssessmentConditionsMissing':[],
            'criterionBindings':[{
                'evidenceKey':'本篇实际键','sourceQuote':'完整必要条件逐字原句',
                'factQuotes':{'真实ID':'逐字个人前提'},'conditionMet':True}]}
    base_payload['rules'].append('source_fact只能忠实描述患者实测值、其本人报告参考范围/分级、主诉或sourceDeclaredRiskFactIds已明确的同病风险信号，不需要论文。风险信号不等于临床疾病/本人症状，不新增病因；必须另返回sourceObservationEntailed与noNewDiagnosisOrRelation，原始范围比较不是新的疾病风险关联；相反/正常制约只内部。')
    base_payload['rules'].append('selectedQuoteGroups保留同一文献的全部逐字锚点，逐条读取并在evidenceQuoteGroups完整返回；兼容单值selectedQuotes不能覆盖同文献前面的锚点。任何一条不支持或限制条件未满足均不得批准整个原子句。')
    base_payload['rules'].append('sourceScopeOptions是本篇原文的实际限制前提，不是患者事实。逐evidenceKey返回sourceScopeReviews.premiseMatches，逐项复制premiseId/sourceQuote，result为met或not_applicable，factIds/factQuotes必须来自本项真实patientFacts。logicalRole=comparison_context的参考/比较组应返回context_preserved，保留真实比较职责，不能要求患者同时属于对照组；其余实际管辖条件仍完整核验。未知入组疾病、基因型、部位、年龄、方法或治疗前提不得以一般定义或populationApplicable=true跳过；诊断原则用于个人判断时同样必须满足。真正不受限原文才可作准确一般解释，不能删去引文中的限制后扩大为所有人。')
    base_payload['outputSchema']['items'][0]['sourceScopeReviews'] = {
        '本篇evidenceKey': {'premiseMatches': [{'premiseId': '输入原ID',
            'sourceQuote': '输入原限制句', 'result': 'met|not_applicable|context_preserved',
            'factIds': [], 'factQuotes': {}}]}}
    if any(item['role']=='management' for item in items):
        base_payload['rules'].extend([
            'management另返回managementScope，使用managementPlan的逐字clauses/原则P1和sourceScopes，抽原文sourceActions并逐行动子句匹配。不能只返回populationApplicable=true；每个scopeId均需源scope原句+本人逐字事实+确实同病当前状态核验。动作安全不等于来源人群适用。',
            'managementScope可把非医疗执行提醒、未来用餐安排或同父计划衔接单列implementation，绑定同完整计划真实action父句及同sourceActionIds，独审无新行为域/药剂量/医学效果。未来安排不伪造本人现状fact；真正现状仍factQuotes逐字，水果不能借衔接加饮水，低碳不能借衔接改餐时。',
            '真实general_principle（一般健康饮食/生活方式、lifestyle modifications/therapy）可支持同域普通个体化，不表示文献逐字规定该时间或剂量，也不承诺个体疗效；窄水果、低碳或制剂试验不能升格成任意生活方式。每条子句的行为对象都要有对应。原文一线处理的原则背景可用现有sourceActionIds，不伪填本人factQuotes；本人现状context仍必须逐字factQuotes。普通医生评估目的不当治疗效益或自行用药。',
            '研究疾病人群、使用特定药物、治疗失败等scope不得以普通不适或另一疾病代替；currentProblemContext只证明同一患者已有状态，不是新增病因或提高评分的依据。'])
        base_payload['outputSchema']['items'][0]['managementScope']=_management_scope_schema()
    if strict_sources:
        base_payload['outputSchema']['items'][0].update(sourceObservationEntailed=True,noNewDiagnosisOrRelation=True)
    tasks=[]
    unread_ids=set()
    def reading_packet(selected):
        evidence=[_model_original(originals[key], selected_quotes=[
            quote for item in selected for quote in item['selectedQuoteGroups'].get(key, [])])
            for key in {key for item in selected for key in item['evidenceKeys']}]
        texts={row['evidenceKey']: re.sub(r'\s+', ' ', row['sourceText']).strip()
            for row in evidence}
        missing={item['id'] for item in selected if any(
            re.sub(r'\s+', ' ', quote).strip() not in texts.get(key, '')
            for key, quotes in item['selectedQuoteGroups'].items() for quote in quotes)}
        return evidence,missing
    for offset in range(0,len(items),6):
        selected=items[offset:offset+6]
        evidence,missing=reading_packet(selected)
        if not missing:
            tasks.append({**base_payload,'items':selected,'evidence':evidence})
            continue
        # A shared paper's context budget must not silently hide one claim's
        # earlier anchors. Try an individual bounded packet; an over-budget
        # atomic claim remains partial and eligible for concise re-authoring.
        for item in selected:
            evidence,missing=reading_packet([item])
            if missing:
                unread_ids.add(item['id'])
            else:
                tasks.append({**base_payload,'items':[item],'evidence':evidence})
    responses=await _bounded_generate(client,tasks)
    reviews=[review for response in responses if isinstance(response,dict)
        for review in response.get('items',[]) if isinstance(review,dict)]
    decisions={}
    for row in rows:
        for claim in row.claims:
            identifier=row.risk_id+':'+claim.claim_id
            matches=[review for review in reviews if isinstance(review,dict) and review.get('id')==identifier]
            review=matches[0] if len(matches)==1 else {}
            if (claim.role in {'outcome','etiology','mechanism'}
                    and review.get('role')=='diagnostic_principle'):
                # A definition may remain explanatory after the same
                # independent entailment check. It cannot enter a potential
                # disease's scoring core through its old author label.
                claim.role='diagnostic_principle'
            applicability_errors=[*_source_applicability_binding_errors(claim,review,facts,originals),
                *_personal_classification_review_errors(claim,candidates[row.risk_id],review,facts,originals),
                *_bind_risk_source_scope(claim, review, facts, originals, subject=subject)]
            if identifier in unread_ids:
                applicability_errors.append('selected_original_context_exceeds_bounded_reading_packet')
            if review.get('role')!=claim.role:
                applicability_errors=[*applicability_errors,'claim_duty_does_not_match_selected_source']
            if claim.role=='management':
                scope=_management_scope_witness(_management_scope_rows(row,claim,facts,management_context,
                    candidate=candidates[row.risk_id], subject=subject),
                    {**review.get('managementScope',{}),
                        'sourceScopeReviewsByPrinciple': {'P1': review.get('sourceScopeReviews', {})}},facts,originals)
                if scope is None:
                    applicability_errors=[*applicability_errors,'management_scope_or_action_witness_missing']
                else:
                    claim._management_witness=scope
            claim._applicability_errors=tuple(applicability_errors)
            decisions[identifier]=(review.get('role')==claim.role and review.get('entailsClaim') is True
                and review.get('populationApplicable') is True and review.get('directionPreserved') is True
                and review.get('missingConditions')==[] and review.get('unsupportedAssertions')==[]
                and review.get('factQuotes')==claim.fact_quotes
                and (review.get('evidenceQuoteGroups') == _scope_quotes_by_source(claim)
                    or not any(len(quotes)>1 for quotes in _scope_quotes_by_source(claim).values())
                    and review.get('evidenceQuotes')=={anchor.evidence_key:anchor.quote for anchor in claim.evidence})
                and not _role_evidence_errors(claim,originals)
                and not applicability_errors
                and (not strict_sources or claim.role!='source_fact' or (
                    review.get('sourceObservationEntailed') is True and review.get('noNewDiagnosisOrRelation') is True)))
            if decisions[identifier] and _personal_clinical_disease_assessment(claim,candidates[row.risk_id],facts,originals):
                claim._classification_receipt=_classification_fingerprint(claim,candidates[row.risk_id],facts,originals)
            if decisions[identifier] and claim.role in {'outcome','etiology','mechanism'}:
                claim._applicability_receipt=_ScientificApplicabilityReceipt(
                    _applicability_fingerprint(claim,facts,originals))
    return decisions


def _index_reason_sentences(assessment):
    return [{'sentenceId':assessment.risk_id+':reason:'+str(index),'text':text.strip()}
        for index,text in enumerate(re.split(r'[。；;\n]+',assessment.scoring_rationale)) if text.strip()]


def _source_only_index_basis_errors(review, assessment, candidate, facts):
    """A background fact can constrain a signal, not silently amplify it."""
    if candidate is None or assessment.certainty != 'potential_risk':
        return []
    core = _score_claims(candidate, assessment.claims)
    if any(claim.role in {'outcome', 'etiology', 'mechanism'} and claim.evidence for claim in core):
        return []
    positive = {fid for claim in core if claim.role == 'source_fact' for fid in claim.fact_quotes}
    declared = set(_declared_risk_signal_ids(candidate, facts))
    if not positive or not positive <= declared:
        return []
    audit = review.get('indexBasis')
    if not isinstance(audit, dict) or audit.get('allReasonContributionsChecked') is not True:
        return ['source_only_index_contributions_not_reviewed']
    used = audit.get('positiveSupportFactIds')
    if (not isinstance(used, list) or not used or any(type(fid) is not str for fid in used)
            or not set(used) <= positive or audit.get('backgroundPositiveFactIds') != []):
        return ['unreviewed_background_raised_source_only_index']
    expected={row['sentenceId']:row['text'] for row in _index_reason_sentences(assessment)}
    contributions=audit.get('reasonContributions')
    if not isinstance(contributions,list) or len(contributions)!=len(expected):
        return ['source_only_reason_contributions_not_bound']
    # Full states are visible for consistency, never implicitly eligible.
    limiting_allowed={fid for fid in candidate.background_fact_ids+candidate.support_fact_ids
        if fid in facts and _limiting_context_quotes(facts[fid])}
    visible=(positive|set(candidate.opposing_fact_ids)|declared|set(candidate.source_model_fact_ids)|limiting_allowed)&facts.keys()
    consistency={fid for fid,fact in facts.items() if fact.kind in {'clinical','self_reported'}
        or fid.startswith('profile:')}
    # A substantive old-role violation is not hidden by a newer missing
    # witness field. This also keeps repair feedback medically specific.
    for item in contributions:
        plus=item.get('positiveFactIds') if isinstance(item,dict) else None
        if (isinstance(plus,list) and all(type(fid) is str for fid in plus)
                and set(plus)<=facts.keys() and not set(plus)<=positive):
            return ['unreviewed_background_raised_source_only_index']
    state_review=audit.get('stateConsistency')
    if (not isinstance(state_review,dict)
            or state_review.get('patientStateDigest')!=_patient_state_digest(facts)
            or state_review.get('falseAbsenceAssertions')!=[]):
        return ['source_only_patient_state_consistency_not_reviewed']
    if not expected:
        # No author prose means no patient assertion to invent or echo.
        # Source IDs, complete-state receipt, numeric/opposing review and
        # independent atom bindings remain mandatory at the same gateway.
        return []
    by_claim={claim.claim_id:claim for claim in core}
    seen=set();derived_positive=set()
    for item in contributions:
        if (not isinstance(item,dict) or item.get('sentenceId') not in expected
                or item['sentenceId'] in seen or item.get('text')!=expected[item['sentenceId']]
                or item.get('allContributionsCaptured') is not True):
            return ['source_only_reason_contributions_not_bound']
        seen.add(item['sentenceId'])
        groups=[item.get(key) for key in ('positiveFactIds','limitingFactIds','presenceOnlyFactIds')]
        if any(not isinstance(ids,list) or any(type(fid) is not str for fid in ids)
                or not set(ids)<=facts.keys() for ids in groups):
            return ['source_only_reason_contains_unknown_facts']
        plus,limiting,presence=map(set,groups)
        if not (plus|limiting|presence)<=visible:
            return ['unreviewed_background_raised_source_only_index']
        if (plus&limiting or plus&presence or limiting&presence
                or not plus<=positive or item.get('unsupportedPositiveRelation') is not False):
            return ['unreviewed_background_raised_source_only_index']
        if presence-set(declared)-positive or limiting-positive-set(candidate.opposing_fact_ids)-limiting_allowed:
            return ['unreviewed_background_raised_source_only_index']
        ids=item.get('supportingClaimIds')
        if not isinstance(ids,list) or not set(ids)<=by_claim.keys():
            return ['source_only_reason_contributions_not_bound']
        supported={fid for identifier in ids for fid in by_claim[identifier].fact_quotes}
        if (plus and (not ids or not plus<=supported)) or (
                item.get('raisesAttention') is True and not plus):
            return ['source_only_reason_contributions_not_bound']
        if type(item.get('raisesAttention')) is not bool:
            return ['source_only_reason_contributions_not_bound']
        assertions=item.get('patientAssertions')
        if not isinstance(assertions,list):
            return ['source_only_patient_assertions_not_bound']
        parts=[part.strip() for part in re.split(r'[，,]+',item['text']) if part.strip()]
        bound=set()
        for assertion in assertions:
            if (not isinstance(assertion,dict) or not isinstance(assertion.get('text'),str)
                    or not assertion['text'].strip() or not any(assertion['text'] in part for part in parts)
                    or assertion.get('use') not in {'positive','limiting'}):
                return ['source_only_patient_assertions_not_bound']
            quotes=assertion.get('factQuotes')
            if not _valid_fact_quotes(quotes,facts,visible):
                return ['source_only_patient_assertions_not_bound']
            identifiers=set(quotes);bound.update(identifiers)
            if not _patient_assertion_object_bound(assertion['text'],quotes,facts):
                return ['source_only_patient_assertion_object_not_bound']
            containing=[part for part in parts if assertion['text'] in part]
            predicate_errors=[error for part in containing for error in
                _patient_negative_predicate_errors(part,quotes,facts)]
            if predicate_errors:
                return predicate_errors
            if assertion['use']=='positive':
                if not identifiers<=plus or not identifiers<=supported:
                    return ['unreviewed_background_raised_source_only_index']
            elif not identifiers<=limiting:
                return ['source_only_patient_assertions_not_bound']
        # A false flag / empty array cannot conceal a named patient state.
        # Match actual source names, not a fixed disease/marker blacklist.
        mentioned={fid for fid,fact in facts.items() if fid in consistency
            and len(_key(fact.name))>=3 and _key(fact.name) in _key(item['text'])}
        unbound_names={_key(facts[fid].name) for fid in mentioned}-{_key(facts[fid].name) for fid in bound}
        required_parts=[part for part in parts if re.search(r'本人|用户|自述|主诉',part)
            or re.search(r'(?:否认|未报告|未见|没有|未发现|尚无|无).{0,18}(?:异常|症状|病史|诊断|确诊|疾病|检测|检查)',part)
            or any(len(_key(facts[fid].name))>=3 and _key(facts[fid].name) in _key(part)
                for fid in consistency)]
        if unbound_names or any(not any(assertion['text'] in part for assertion in assertions)
                for part in required_parts):
            return ['source_only_patient_assertions_not_bound']
        derived_positive.update(plus)
    if seen!=expected.keys() or derived_positive!=set(used):
        return ['source_only_reason_contributions_not_bound']
    return []


def _source_grade_targets(candidate,facts):
    return [{'factId':fid,'sourceValue':facts[fid].value,'sourceLevel':facts[fid].status}
        for fid in candidate.source_model_fact_ids if fid in facts
        and _fact_role(facts[fid])=='source_disease_model'
        and _risk_identity(facts[fid].name.removesuffix('（来源疾病模型指数）'))==candidate.disease_id]


def _numeric_limiting_quote_allowed(fact,quote,kind):
    if not isinstance(quote,str) or not quote or quote not in fact.value:
        return False
    if kind=='actual_normal_result':
        return bool(fact.kind=='clinical' and fact.abnormal is not True
            and quote in _limiting_context_quotes(fact))
    if kind!='explicit_symptom_denial' or fact.kind!='self_reported':
        return False
    # Unknown information, no diagnosis and family history are not a current
    # symptom-negative finding. A categorical answer keeps its actual topic.
    if re.search(r'诊断|确诊|病史|过敏史|用药史|家族|血亲|亲属|手术|住院|未报告|未提供|不清楚|未知|'
            r'diagnos|family history|unknown',fact.name+' '+quote,re.I):
        return False
    if re.search(r'无(?:改善|变化|固定|规律)|没有(?:改善|变化|固定|规律)|'
            r'no\s+(?:improvement|change|fixed)',quote,re.I):
        return False
    if not re.search(r'症状|不适|疼痛|腹痛|胸痛|头痛|关节痛|腹泻|便秘|便血|黑便|'
            r'发热|皮疹|瘙痒|喷嚏|鼻塞|喘息|呼吸困难|咳嗽|乏力|眩晕|麻木|口渴|多尿|尿频|'
            r'体重下降|symptoms?|pain|diarrh|bleed|rash|cough|fever',fact.name+' '+quote,re.I):
        return False
    return bool(quote in _limiting_context_quotes(fact) or quote in {'否','以上均无','均无'}
        and re.search(r'症状|发热|便血|疼痛|腹痛|腹泻|下降|symptom',fact.name,re.I))


def _numeric_limiting_repair_scope(document,assessment,candidate,originals):
    """Prior applicability is usable only for this unchanged source/core repair."""
    if not isinstance(originals,dict):
        return None
    core=_score_claims(candidate,assessment.claims)
    keys={anchor.evidence_key for claim in core for anchor in claim.evidence}
    keys.update(assessment.opposing_evidence_keys)
    if any(key not in originals or not isinstance(originals[key].get('sourceText'),str) for key in keys):
        return None
    return _digest({'riskId':assessment.risk_id,'diseaseId':candidate.disease_id,
        'category':candidate.category,'certainty':candidate.certainty,
        'facts':_source_digest(document),'subject':_registry(document).subject_snapshot,
        'core':[claim.model_dump(mode='json',by_alias=True) for claim in core],
        'originals':{key:{'title':originals[key].get('title'),'sourceText':originals[key]['sourceText']}
            for key in sorted(keys)}})


def _rejected_numeric_limiting_choices(document,assessment,candidate,facts,originals,critique):
    """Do not offer an explicit same-disease rejection again in this repair."""
    if not isinstance(critique,dict):
        return set()
    scope=_numeric_limiting_repair_scope(document,assessment,candidate,originals)
    state=critique.get('stateConsistency')
    factors=critique.get('numericFactors')
    core={claim.claim_id for claim in _score_claims(candidate,assessment.claims)}
    if (scope is None or critique.get('limitingReviewScope')!=scope
            or not isinstance(state,dict) or state.get('patientStateDigest')!=_patient_state_digest(facts)
            or state.get('falseAbsenceAssertions')!=[] or not isinstance(factors,dict)
            or factors.get('allFactorsChecked')is not True or factors.get('unboundFactors')!=[]
            or not isinstance(factors.get('approvedClaimIds'),list)
            or any(not isinstance(cid,str) for cid in factors['approvedClaimIds'])
            or len(factors['approvedClaimIds'])!=len(core) or set(factors['approvedClaimIds'])!=core):
        return set()
    allowed={(item['factId'],quote,item['kind'],candidate.disease_id)
        for item in _numeric_limiting_inventory(candidate,facts,assessment.claims)
        for quote in item['factQuotes']}
    result=set()
    for item in factors.get('limitingFacts',[]) if isinstance(factors.get('limitingFacts'),list) else []:
        if (not isinstance(item,dict) or item.get('sameDisease')is not False
                or any(not isinstance(item.get(key),str)
                    for key in ('factId','factQuote','kind','targetDiseaseId'))):
            continue
        choice=tuple(item[key] for key in ('factId','factQuote','kind','targetDiseaseId'))
        if choice in allowed:
            result.add(choice)
    return result


def _numeric_index_basis_errors(review,assessment,candidate,facts,*,excluded_limits=()):
    """A prose-free score needs exact, independently reviewed factor receipts."""
    if assessment.scoring_rationale.strip():
        return []  # Historical prose still enters every existing fact gate.
    if candidate is None or not isinstance(review,dict):
        return ['numeric_factor_review_missing']
    basis=review.get('indexBasis')
    if not isinstance(basis,dict):
        return ['numeric_factor_review_missing']
    core={claim.claim_id:claim for claim in _score_claims(candidate,assessment.claims)}
    positive={fid for claim in core.values() for fid in claim.fact_quotes}
    state=basis.get('stateConsistency')
    if (not isinstance(basis.get('positiveSupportFactIds'),list)
            or any(not isinstance(fid,str) for fid in basis['positiveSupportFactIds'])
            or set(basis.get('positiveSupportFactIds') or [])!=positive
            or basis.get('backgroundPositiveFactIds')!=[]
            or basis.get('reasonContributions')!=[]
            or not isinstance(state,dict) or state.get('patientStateDigest')!=_patient_state_digest(facts)
            or state.get('falseAbsenceAssertions')!=[]):
        return ['numeric_factor_state_or_support_not_bound']
    factors=basis.get('numericFactors')
    if (not isinstance(factors,dict) or set(factors)!={'approvedClaimIds','limitingFacts',
            'sourceGradeComparisons','allFactorsChecked','unboundFactors'}
            or factors.get('allFactorsChecked') is not True or factors.get('unboundFactors')!=[]
            or not isinstance(factors.get('approvedClaimIds'),list)
            or any(not isinstance(cid,str) for cid in factors['approvedClaimIds'])
            or len(factors['approvedClaimIds'])!=len(core) or set(factors['approvedClaimIds'])!=core.keys()):
        return ['numeric_core_factors_not_bound']
    limits=factors.get('limitingFacts')
    allowed=set([*candidate.opposing_fact_ids,*candidate.background_fact_ids,
        *candidate.support_fact_ids,*candidate._role_conflicts])-positive
    seen=set()
    if not isinstance(limits,list):
        return ['numeric_limiting_factors_not_bound']
    for item in limits:
        if (not isinstance(item,dict) or set(item)!={'factId','factQuote','kind','targetDiseaseId','sameDisease'}
                or not isinstance(item.get('factId'),str)
                or item.get('factId')not in allowed or item['factId']not in facts or item['factId']in seen
                or item.get('targetDiseaseId')!=candidate.disease_id or item.get('sameDisease')is not True
                or not _numeric_limiting_quote_allowed(facts[item['factId']],item.get('factQuote'),item.get('kind'))
                or tuple(item[key] for key in ('factId','factQuote','kind','targetDiseaseId')) in excluded_limits):
            return ['numeric_limiting_factors_not_bound']
        seen.add(item['factId'])
    comparisons=factors.get('sourceGradeComparisons')
    targets={item['factId']:item for item in _source_grade_targets(candidate,facts)}
    if not isinstance(comparisons,list) or len(comparisons)!=len(targets):
        return ['source_grade_comparison_not_bound']
    compared=set();levels={'低风险':0,'正常':0,'注意':1,'中风险':2,'高风险':3}
    for item in comparisons:
        if (not isinstance(item,dict) or set(item)!={'factId','sourceValue','sourceLevel','index','level',
                'comparison','basis','positiveClaimIds','limitingFactIds','missingNotUsedAsNegative','gradeJustified'}
                or not isinstance(item.get('factId'),str)
                or item.get('factId')not in targets or item['factId']in compared):
            return ['source_grade_comparison_not_bound']
        source=targets[item['factId']];old=levels.get(source['sourceLevel']);new=levels.get(assessment.level)
        comparison='not_comparable' if old is None else ('same' if old==new else 'higher' if new>old else 'lower')
        if (item.get('sourceValue')!=source['sourceValue'] or item.get('sourceLevel')!=source['sourceLevel']
                or type(item.get('index'))is not int or item['index']!=assessment.index
                or item.get('level')!=assessment.level or item.get('comparison')!=comparison
                or item.get('basis')not in {'reviewed_personal_factors','source_signal_specificity'}
                or not isinstance(item.get('positiveClaimIds'),list)
                or any(not isinstance(cid,str) for cid in item['positiveClaimIds']) or set(item['positiveClaimIds'])!=core.keys()
                or not isinstance(item.get('limitingFactIds'),list)
                or any(not isinstance(fid,str) for fid in item['limitingFactIds']) or set(item['limitingFactIds'])!=seen
                or item.get('missingNotUsedAsNegative')is not True or item.get('gradeJustified')is not True):
            return ['source_grade_comparison_not_bound']
        negative_source=any(fid in facts and _fact_role(facts[fid])=='microbiome_association_screen'
            and _risk_identity(facts[fid].name)==candidate.disease_id
            and facts[fid].value in {'未检出','阴性','正常'}
            for fid in candidate.opposing_fact_ids)
        source_conflict=len({levels[target['sourceLevel']] for target in targets.values()
            if target['sourceLevel'] in levels})>1
        if (comparison not in {'same','not_comparable'} and not seen and not negative_source
                and not assessment.opposing_evidence_keys and not source_conflict
                and _is_source_signal_only(candidate,facts,assessment.claims)
                and not (comparison=='lower' and item['basis']=='source_signal_specificity')):
            return ['source_only_grade_change_without_personal_basis']
        compared.add(item['factId'])
    # A free-text reviewer assertion cannot lend missing patient states to
    # this structural score receipt. It is not used as a clinical narrative.
    for key in ('reason','scoringRationale','indexRationale'):
        text=review.get(key)
        if isinstance(text,str) and _patient_state_text_errors(text,facts):
            return ['numeric_review_contains_unbound_patient_state']
    return []


def _numeric_receipt_fingerprint(assessment,document,audit_json):
    return _digest({'riskId':assessment.risk_id,'diseaseId':assessment.disease_id,
        'category':assessment.category,'certainty':assessment.certainty,
        'index':assessment.index,'level':assessment.level,'evidenceStrength':assessment.evidence_strength,
        'supportFactIds':assessment.support_fact_ids,'opposingFactIds':assessment.opposing_fact_ids,
        'sourceModelFactIds':assessment.source_model_fact_ids,
        'core':[claim.model_dump(mode='json',by_alias=True) for claim in assessment.claims if claim.role!='management'],
        'facts':_source_digest(document),'audit':audit_json})


def _numeric_receipt_valid(assessment,document):
    receipt=assessment._numeric_receipt
    return bool(isinstance(receipt,_NumericRiskDecisionReceipt) and receipt.fingerprint==
        _numeric_receipt_fingerprint(assessment,document,receipt.audit_json))


def _review_errors(review, assessment, facts, originals, candidate=None,*,excluded_numeric_limits=(), subject=None):
    if not isinstance(review, dict) or review.get('riskId') != assessment.risk_id:
        return ['independent_review_missing']
    errors = []
    if candidate is not None and candidate._role_conflicts and set(
            review.get('consideredConflictedFactIds') or [])!=set(candidate._role_conflicts):
        errors.append('conflicted_patient_fact_roles_not_independently_resolved')
    if ((review.get('coreValid',review.get('valid')) is not True) or review.get('scoreJustified') is not True
            or review.get('index') != assessment.index or review.get('level') != assessment.level
            or review.get('coreMissingConditions',review.get('missingConditions')) != []):
        errors.append('independent_risk_review_failed')
    unsupported=review.get('unsupportedClaims')
    management_ids={claim.claim_id for claim in assessment.claims if claim.role=='management'}
    if not isinstance(unsupported,list) or any(item not in management_ids for item in unsupported):
        errors.append('unsupported_core_assertions')
    opposing = review.get('consideredOpposingFactIds')
    visible=set(assessment.opposing_fact_ids)
    if candidate is not None:
        visible.update([*candidate.support_fact_ids,*candidate.background_fact_ids,
            *candidate.source_model_fact_ids,*candidate._role_conflicts])
    positive={fid for claim in assessment.claims if claim.role!='management' for fid in claim.fact_quotes}
    if (not isinstance(opposing, list) or any(type(fid) is not str for fid in opposing)
            or not set(assessment.opposing_fact_ids)<=set(opposing)
            or not set(opposing)<=visible&facts.keys()
            or (set(opposing)-set(assessment.opposing_fact_ids))&positive):
        errors.append('opposing_facts_not_reviewed')
    opposing_keys = review.get('consideredOpposingEvidenceKeys', [])
    if (not isinstance(opposing_keys, list) or any(type(key) is not str for key in opposing_keys)
            or set(opposing_keys) != set(assessment.opposing_evidence_keys)):
        errors.append('opposing_evidence_not_reviewed')
    rows = review.get('claims')
    if not isinstance(rows, list):
        return [*errors, 'independent_claim_reviews_missing']
    by_id = {}
    for row in rows:
        if isinstance(row, dict):
            by_id.setdefault(row.get('claimId'), []).append(row)
    for claim in assessment.claims:
        if claim.role=='management':
            continue
        errors.extend(_risk_source_scope_errors(claim, facts, originals, subject=subject))
        if _source_applicability_requirements(claim,originals):
            receipt=claim._applicability_receipt
            if (not isinstance(receipt,_ScientificApplicabilityReceipt)
                    or receipt.fingerprint!=_applicability_fingerprint(claim,facts,originals)):
                errors.append('scientific_source_applicability_receipt_missing_or_changed')
        matches = by_id.get(claim.claim_id, [])
        row = matches[0] if len(matches) == 1 else {}
        if row.get('valid') is not True or row.get('missingConditions') != []:
            errors.append('independent_claim_rejected')
        if row.get('factQuotes') != claim.fact_quotes or not _valid_fact_quotes(
                row.get('factQuotes'), facts, claim.fact_quotes):
            errors.append('independent_patient_binding_missing')
        required = {anchor.evidence_key: anchor.quote for anchor in claim.evidence}
        if row.get('evidenceQuotes') != required or any(
                key not in originals or quote not in originals[key]['sourceText'] for key, quote in required.items()):
            errors.append('independent_original_binding_missing')
    errors.extend(_source_only_index_basis_errors(review, assessment, candidate, facts))
    errors.extend(_numeric_index_basis_errors(review,assessment,candidate,facts,
        excluded_limits=excluded_numeric_limits))
    errors.extend(_score_context_errors(assessment,candidate,facts))
    errors.extend(_patient_state_text_errors(assessment.scoring_rationale,facts))
    if candidate is not None and isinstance(opposing,list) and any(fid in facts
            and not _source_context_relevant(candidate,facts[fid],_score_claims(candidate,assessment.claims))
            for fid in opposing):
        errors.append('unrelated_source_screen_used_to_limit_disease_index')
    return list(dict.fromkeys(errors))


def _cardio_display_options(assessment,candidate):
    if (candidate.certainty!='potential_risk' or not candidate.disease_id.startswith('disease-')
            or not re.search(r'心血管|动脉粥样硬化|ASCVD|cardiovascular',candidate.name,re.I)):
        return []
    options={}
    for claim in assessment.claims:
        if claim.role not in {'outcome','etiology','mechanism'} or not claim.evidence:
            continue
        for term in claim.outcome_terms:
            if not re.search(r'心血管|动脉粥样硬化',term):
                continue
            base=re.sub(r'(?:的)?(?:发生|患病|发病)?(?:风险|发生率|发病率)(?:增加|升高|降低|减少)?$','',term)
            if not 2<=len(base)<=80:
                continue
            name=base+'相关风险'
            options.setdefault(name,[]).append(claim.claim_id)
    return [{'name':name,'claimIds':list(dict.fromkeys(ids))} for name,ids in options.items()]


def _reviewed_display_name(assessment,review,candidate):
    """A clearer name must denote an already reviewed outcome, not a new one."""
    name=review.get('displayName')
    ids=review.get('displayNameClaimIds')
    if (not isinstance(name,str) or not 2<=len(name)<=160 or _candidate_name(name)!=name
            or _BOUNDARY.search(name) or _workflow_endpoint(name) or _nonspecific_clinical_endpoint(name)
            or re.search(r'确诊|已患|患有',name)
            or not isinstance(ids,list) or not ids):
        return None
    selected={claim.claim_id:claim for claim in assessment.claims}
    if not set(ids)<=selected.keys():
        return None
    if _lipid_subtype_name(name):
        # Display-name equivalence in the broad dyslipidaemia catalogue does
        # not prove a subtype.  Only an actual original clinical label may
        # rename a current upper-level problem; a separately reviewed subtype
        # risk keeps its own candidate and scientific endpoint instead.
        base = re.sub(r'(?:相关|发生|易感)?风险$', '', name)
        literal = any(base in quote and not re.search(r'否认|未确诊|未诊断|未患|没有|无|疑似|可能|考虑|'
            r'家族|血亲|亲属|父亲|母亲', quote.split(base, 1)[0])
            for claim in assessment.claims if claim.role == 'source_fact' for quote in claim.fact_quotes.values())
        classification = any(identifier in ids and _personal_lipid_classification(claim, name)
            for identifier, claim in selected.items())
        independent_risk = (candidate.certainty == 'potential_risk' and candidate.name == name
            and any(claim.role in {'outcome', 'etiology', 'mechanism'} and claim.evidence
                and any(_key(_endpoint_name(term)) == _key(_endpoint_name(name))
                    for term in claim.outcome_terms) for claim in assessment.claims))
        if not literal and not classification and not independent_risk:
            return None
    options=_cardio_display_options(assessment,candidate)
    if options:
        matched=[option for option in options if name==option['name']
            and set(option['claimIds']).intersection(ids)]
        if len(matched)==1:
            return matched[0]['name']
        if name==candidate.name and len(options)==1 and set(options[0]['claimIds']).intersection(ids):
            return options[0]['name']
        return None
    identity=_risk_identity(name)
    if identity==candidate.disease_id:
        return name
    for identifier in ids:
        claim=selected[identifier]
        if claim.role not in {'outcome','etiology','mechanism'} or not claim.evidence:
            continue
        if identity in {_risk_identity(term) for term in claim.outcome_terms}:
            return name
    return None


def _adopted_self_report_name(document,assessment):
    """Use the live, source-bound symptom facet, not a measurement title."""
    if document is None:
        return None
    from .evaluation_risk_source_links import source_current_problem_view
    sources=[claim for claim in assessment.claims if claim.role=='source_fact']
    used={fid for claim in sources for fid in claim.fact_quotes}
    text=_key(' '.join(claim.text for claim in sources))
    options=[]
    for item in source_current_problem_view(document):
        if item.get('certainty')!='self_reported' or not used&set(item.get('factIds',[])):
            continue
        parts=[_key(value) for value in re.split(r'[；;。\n]+',item.get('value') or '') if len(_key(value))>=4]
        # A shared composite questionnaire ID is never sufficient. The
        # already reviewed source sentence must match this symptom facet.
        if not parts or not all(part in text for part in parts):
            continue
        name=re.sub(r'[（(]自述[）)]','',item.get('name') or '').strip()
        for part in re.split(r'[/／]',name):
            if 2<=len(part)<=40 and not re.search(r'\d|每周|每天|每月',part):
                identity=_risk_identity(part)
                if not identity.startswith('disease-'):
                    options.append(part if part.endswith(('症状','表现')) else part+'症状')
        if not options and 2<=len(name)<=60 and not re.search(r'\d|每周|每天|每月',name):
            options.append(name)
    return min(options,key=len) if options else None


def _scope_adopted_symptom_claim(document,candidate,claim):
    """Bind a current source claim to its own adopted finding before review.

    One CT fact may contain several lesions. Its full literal value cannot
    lend a property of another lesion to this claim merely through a shared
    fact ID. Only the same source condition's verified excerpts are used for
    source-only clinical meaning; the immutable whole CT remains available.
    """
    if claim.role == 'source_fact' and candidate.certainty in {
            'confirmed', 'current_problem', 'self_reported'}:
        from .evaluation_disease_identity import same_source_disease
        facts = _facts(document)
        owners = [problem for problem in document.problems
            if same_source_disease(problem.title, _endpoint_name(candidate.name))
            and any(source.fact_id in claim.fact_quotes for source in problem.sources)]
        if len(owners) == 1:
            own = {}
            for source in owners[0].sources:
                fact = facts.get(source.fact_id)
                if (source.fact_id in claim.fact_quotes and fact is not None
                        and fact.kind == 'clinical' and source.quote
                        and source.quote in fact.value):
                    own.setdefault(source.fact_id, []).append(source.quote)
            if own:
                # Each witness remains a literal source excerpt. Separate
                # discontinuous excerpts are never joined into a fake quote.
                replacements = {fid: next(iter(dict.fromkeys(quotes)))
                    for fid, quotes in own.items()}
                if replacements:
                    claim = claim.model_copy(update={'fact_quotes': {
                        **claim.fact_quotes, **replacements}})
    if candidate.certainty!='self_reported' or claim.role!='source_fact':
        return claim
    from .evaluation_risk_source_links import source_current_problem_view
    facts=_facts(document);choices=[]
    for item in source_current_problem_view(document):
        if item.get('certainty')!='self_reported' or not set(item.get('factIds',[]))&set(claim.fact_quotes):
            continue
        names=[re.sub(r'[（(]自述[）)]','',name).strip() for name in re.split(r'[/／]',item.get('name') or '')]
        if not any(_risk_identity(name)==candidate.disease_id or
                len(_key(name.removesuffix('症状')))>=2 and _key(name.removesuffix('症状')) in _key(candidate.name)
                for name in names):
            continue
        pieces=[piece.strip() for piece in re.split(r'[；;。\n]+',item.get('value') or '') if piece.strip()]
        if not pieces:
            continue
        for fid in claim.fact_quotes:
            fact=facts.get(fid)
            if fact is None or fact.kind!='self_reported':
                continue
            spans=[(fact.value.find(piece),len(piece)) for piece in pieces]
            if any(start<0 for start,_ in spans):
                continue
            start=min(start for start,_ in spans);end=max(start+size for start,size in spans)
            quote=fact.value[start:end]
            if quote and all(_key(piece) in _key(claim.text) for piece in pieces):
                choices.append((len(quote),fid,quote,item.get('observationId') or item.get('name')))
    if not choices or len({choice[3] for choice in choices})!=1:
        return claim
    _,fid,quote,_=min(choices)
    return claim.model_copy(update={'text':quote.rstrip('。')+'。','fact_quotes':{fid:quote}})


def _self_report_state_name(assessment,candidate,facts,proposed=None,*,document=None):
    """A symptom report cannot acquire a disease title through classification."""
    if candidate.certainty!='self_reported':
        return proposed
    source_claims=[claim for claim in assessment.claims if claim.role=='source_fact']
    selected=[facts[fid] for claim in source_claims for fid in claim.fact_quotes
        if fid in facts and facts[fid].kind=='self_reported']
    if not selected:
        return proposed
    if _constipation_symptom_witnesses(assessment,facts):
        return '便秘症状'
    adopted=_adopted_self_report_name(document,assessment)
    if adopted:
        return adopted
    native_name=_endpoint_name(candidate.name)
    if any(native_name in fact.name+' '+fact.value or _risk_identity(fact.name)==candidate.disease_id
            for fact in selected):
        return proposed
    if proposed and any(proposed in fact.name+' '+fact.value for fact in selected):
        return proposed
    names=[fact.name.strip() for fact in selected if 2<=len(fact.name.strip())<=60
        and not re.search(r'[?？]|是否|什么|哪些|情况如何',fact.name)]
    return min(names,key=len) if names else proposed


def _constipation_symptom_witnesses(assessment,facts):
    """Exact patient scope and an already reviewed diagnostic-principle anchor."""
    from .evaluation_fixed_assessment import _answer
    observed=[quote for claim in assessment.claims if claim.role=='source_fact'
        and re.search(r'便秘|硬便|排便.{0,6}(?:次数|频率|用力|困难|费力|减少)|每周.{0,4}排便|'
            r'infrequent.{0,15}bowel|hard stools?|straining',claim.text,re.I)
        for fid,quote in claim.fact_quotes.items() if fid in facts and facts[fid].kind=='self_reported']
    result=set()
    for claim in assessment.claims:
        if (claim.role!='diagnostic_principle' or not claim.evidence
                or not any(_risk_identity(term)=='constipation' for term in claim.outcome_terms)):
            continue
        for fid,quote in claim.fact_quotes.items():
            fact=facts.get(fid)
            if (fact is None or fact.kind!='self_reported' or not quote or quote not in fact.value
                    or assessment.certainty=='self_reported' and not any(
                        _key(span) in _key(quote) for span in observed)):
                continue
            # Alias facts from one answer may have different IDs. Matching
            # the full answer as well as its exact adopted symptom span is
            # stricter than joining on a shared composite questionnaire ID.
            answer=_key(_answer(fact))
            for anchor in claim.evidence:
                result.add((anchor.evidence_key,anchor.quote,_key(quote),answer))
    return result


def _same_current_constipation_scope(left,right,facts):
    if (_candidate_entity_scope(left) != _candidate_entity_scope(right)
            or left.category!=right.category or {left.certainty,right.certainty}
            !={'current_problem','self_reported'}):
        return False
    condition=left if left.certainty=='current_problem' else right
    if condition.disease_id!='constipation' and _risk_identity(condition.name)!='constipation':
        return False
    return bool(_constipation_symptom_witnesses(left,facts)
        &_constipation_symptom_witnesses(right,facts))


def _risk_scope_publication_state(assessment, document, evidence_hashes):
    from .evaluation_source_scope import source_scope_receipt_matches
    facts = _facts(document)
    subject = _registry(document).subject_snapshot
    state = []
    for claim in assessment.claims:
        anchors = _scope_quotes_by_source(claim)
        for key, binding in sorted(claim._source_scope_bindings.items()):
            valid = isinstance(binding, _RiskSourceScopeBinding) and key in anchors
            if valid:
                scope = _registry(document)._original_scope_snapshot.get(key)
                valid = (evidence_hashes.get(key) == _digest(binding.source_text)
                    and (scope is None or not binding.original_scope_hash or scope == binding.original_scope_hash)
                    and source_scope_receipt_matches(binding.receipt,
                        source_text=binding.source_text, title=binding.title,
                        selected_quotes=anchors[key], role=claim.role, claim=claim.text,
                        patient_facts={fid: facts[fid] for fid in claim.fact_quotes if fid in facts},
                        target_context=_risk_source_scope_context(claim, subject=subject),
                        verified_conditions=binding.verified_conditions))
            state.append({'claimId': claim.claim_id, 'evidenceKey': key, 'valid': bool(valid),
                'binding': _digest(binding) if isinstance(binding, _RiskSourceScopeBinding) else None})
    return state


def _proof(assessment, document, evidence_hashes):
    return _digest({'assessment': assessment.model_dump(mode='json', by_alias=True),
        'numericReview':assessment._numeric_receipt.audit_json
            if isinstance(assessment._numeric_receipt,_NumericRiskDecisionReceipt) else None,
        'facts': _source_digest(document), 'evidence': {key: evidence_hashes.get(key)
            for key in [*assessment.evidence_keys, *assessment.opposing_evidence_keys]},
        'originalScope': {key: _registry(document)._original_scope_snapshot.get(key)
            for key in [*assessment.evidence_keys, *assessment.opposing_evidence_keys]},
        'subject': _registry(document).subject_snapshot,
        'sourceScope': _risk_scope_publication_state(assessment, document, evidence_hashes),
        'rubric': SCORING_RUBRIC['version']})


def approved_assessments(document):
    registry = _registry(document)
    return [row for row in registry.assessments if registry._proofs.get(row.risk_id)
        == _proof(row, document, registry._evidence_hashes) and (
            bool(row.scoring_rationale.strip()) or _numeric_receipt_valid(row,document))
        and all(binding['valid'] for binding in _risk_scope_publication_state(
            row, document, registry._evidence_hashes))]


def _entity_kind_fingerprint(risk):
    return _digest({'kind': risk.entity_kind, 'name': risk.name, 'diseaseId': risk.disease_id,
        'certainty': risk.certainty, 'claims': [claim.model_dump(mode='json', by_alias=True)
            for claim in risk.claims if claim.role != 'management']})



def _classification_fingerprint(claim, candidate, facts, originals):
    return _digest({'claim': claim.model_dump(), 'target': candidate.name,
        'certainty': candidate.certainty,
        'facts': {fid: facts[fid].model_dump() for fid in claim.fact_quotes if fid in facts},
        'sources': {a.evidence_key: _digest(originals.get(a.evidence_key, {}).get('sourceText', ''))
            for a in claim.evidence}})


def _explicit_personal_disease_source(claim, risk, facts):
    if claim.role != 'source_fact':
        return False
    base = _endpoint_name(risk.name)
    for fid, quote in claim.fact_quotes.items():
        fact = facts.get(fid)
        if fact is None or fact.kind not in {'clinical', 'self_reported'} or quote not in fact.value:
            continue
        text = fact.name + ' ' + quote
        if base not in text or re.search(r'否认|未确诊|未诊断|未患|没有|家族|血亲|亲属|父亲|母亲|疑似|可能|考虑', text):
            continue
        # A named diagnosis/history is literal; a measurement alias is not.
        if (base in quote or base == fact.name) and not re.fullmatch(r'[<>≤≥+\-]?\s*\d+(?:\.\d+)?', quote):
            if fact.kind == 'clinical' or re.search(r'诊断|确诊|患有|病史', text):
                return True
    return False


def _personal_classification_review_errors(claim, candidate, review, facts, originals):
    if not _personal_clinical_disease_assessment(claim, candidate, facts, originals):
        return []
    item = review.get('personalClassification')
    potential = candidate.certainty == 'potential_risk'
    cautious_complete = bool(isinstance(item, dict) and potential
        and item.get('assessmentBasisComplete') is True
        and item.get('cautiousConclusionSupported') is True
        and item.get('requiredAssessmentConditionsMissing') == [])
    # A cautious assessment and a formal diagnosis have distinct duties.
    # Some independent replies explicitly approve the former but repeat the
    # latter's exact missing conditions in both lists. Do not erase or infer
    # conditions: accept only that literal duplicate with every outer review
    # obligation met. Actual assessment gaps and unbound criteria still fail.
    nested_missing = item.get('missingConditions') if isinstance(item, dict) else None
    formal_missing = item.get('unconfirmedDiagnosticConditions') if isinstance(item, dict) else None
    formal_only_duplicates = bool(cautious_complete
        and item.get('conclusionUse') == 'potential_assessment'
        and item.get('completeCriteria') is False
        and item.get('diagnosisNotAsserted') is True
        and isinstance(nested_missing, list) and nested_missing
        and isinstance(formal_missing, list) and formal_missing
        and all(isinstance(value, str) and value.strip() for value in [*nested_missing, *formal_missing])
        and set(nested_missing) <= set(formal_missing)
        and review.get('entailsClaim') is True and review.get('populationApplicable') is True
        and review.get('directionPreserved') is True and review.get('missingConditions') == []
        and review.get('unsupportedAssertions') == []
        and review.get('factQuotes') == claim.fact_quotes
        and review.get('evidenceQuotes') == {anchor.evidence_key: anchor.quote for anchor in claim.evidence})
    if (not isinstance(item, dict) or item.get('targetDisease') != candidate.name
            or not (item.get('completeCriteria') is True or cautious_complete)
            or item.get('allConditionsChecked') is not True
            or not (nested_missing == [] or formal_only_duplicates)
            or item.get('factQuotes') != claim.fact_quotes):
        return ['personal_disease_classification_not_completely_reviewed']
    if (item.get('conclusionUse') != ('potential_assessment' if potential else 'current_classification')
            or potential and item.get('diagnosisNotAsserted') is not True
            or not potential and item.get('unconfirmedDiagnosticConditions', []) != []):
        return ['personal_classification_certainty_or_required_conditions_mismatch']
    bindings = item.get('criterionBindings')
    if not isinstance(bindings, list) or not bindings:
        return ['personal_disease_classification_criteria_missing']
    for binding in bindings:
        source = originals.get(binding.get('evidenceKey'), {}) if isinstance(binding, dict) else {}
        quote = binding.get('sourceQuote') if isinstance(binding, dict) else None
        if (not isinstance(quote, str) or len(quote.strip()) < 8 or quote not in source.get('sourceText', '')
                or binding.get('conditionMet') is not True
                or not _valid_fact_quotes(binding.get('factQuotes'), facts, claim.fact_quotes)):
            return ['personal_disease_classification_condition_missing_or_unbound']
    return []


def _has_current_disease_classification(risk, claims, facts, originals):
    for claim in claims:
        if _explicit_personal_disease_source(claim, risk, facts):
            return True
        if (claim._classification_receipt is not None and
                claim._classification_receipt == _classification_fingerprint(claim, risk, facts, originals)):
            return True
    return False


def is_publishable_disease_assessment(risk):
    """A disease heading needs the live independent endpoint-kind verdict.

    Callers still obtain rows through approved_assessments(document). A
    symptom/assay, catalogue alias or serialized kind label is not permission
    to call the person diseased or invent another disease outcome.
    """
    return bool(not non_disease_endpoint_kind(risk.name) and not _nonspecific_clinical_endpoint(risk.name)
        and getattr(risk, 'entity_kind', None) in {'disease', 'disease_risk'}
        and getattr(risk, '_entity_kind_receipt', None) == _entity_kind_fingerprint(risk))


def _live_disease_assessment_basis(risk, facts, registry):
    """Evaluate one member of a freshly verified registry snapshot."""
    if not is_publishable_disease_assessment(risk):
        return 'observation'
    core = _score_claims(risk, risk.claims)
    if any(_explicit_personal_disease_source(claim, risk, facts)
            or claim._classification_receipt is not None
            and _personal_clinical_disease_assessment(claim, risk, facts)
            for claim in core):
        return 'clinical_classification'
    if any(claim.role in {'outcome', 'etiology', 'mechanism'} and claim.evidence
            and claim.fact_quotes and _valid_fact_quotes(claim.fact_quotes, facts, claim.fact_quotes)
            and any(_risk_identity(_endpoint_name(term)) == _risk_identity(risk.name)
                for term in claim.outcome_terms)
            and all(anchor.evidence_key in registry._evidence_hashes for anchor in claim.evidence)
            and any(anchor.direction in {'support', 'mixed'} for anchor in claim.evidence)
            for claim in core):
        return 'individual_risk'
    declared = set(_declared_risk_signal_ids(risk, facts))
    if any(claim.role == 'source_fact' and set(claim.fact_quotes) & declared for claim in core):
        return 'source_model_only'
    return 'unresolved'


def disease_assessment_bases(document):
    """One request-local, identity-bound view; no cached or serialized proof.

    All members pass the existing live registry verification once before
    deriving their nature. The returned object identities cannot authorize a
    different risk with the same ID or survive a later mutation/rebuild.
    """
    rows = approved_assessments(document)
    facts, registry = _facts(document), _registry(document)
    return {risk.risk_id: {'assessment': risk,
        'basis': _live_disease_assessment_basis(risk, facts, registry)} for risk in rows}


def disease_assessment_basis(risk, document):
    """Separate actual screening results from personal disease assessments."""
    row = disease_assessment_bases(document).get(risk.risk_id)
    return row['basis'] if row is not None and row['assessment'] is risk else 'unavailable'


def is_personal_disease_assessment(risk, document):
    return disease_assessment_basis(risk, document) in {'clinical_classification', 'individual_risk'}


def _bind_reviewed_entity_kind(risk, review, facts=None, originals=None):
    kind = review.get('entityKind')
    ids = review.get('entityKindClaimIds')
    claims = {claim.claim_id: claim for claim in risk.claims if claim.role != 'management'}
    if (kind not in {'disease', 'disease_risk', 'symptom', 'indicator_problem'}
            or review.get('entityKindJustified') is not True or not isinstance(ids, list)
            or not ids or not set(ids) <= claims.keys()):
        return False
    if kind in {'disease', 'disease_risk'}:
        if non_disease_endpoint_kind(risk.name):
            return False
        if (review.get('specificDiseaseEndpoint') is not True
                or review.get('symptomOrIndicatorNotUpgraded') is not True
                or kind == 'disease_risk' and risk.certainty != 'potential_risk'
                or kind == 'disease' and risk.certainty == 'potential_risk'):
            return False
        if _nonspecific_clinical_endpoint(risk.name):
            return False
        if kind == 'disease' and not _has_current_disease_classification(
                risk, [claims[key] for key in ids], facts or {}, originals or {}):
            return False
        if re.search(r'症状|(?:指标|数值|检查)(?:异常|偏高|偏低)|血脂异常|多病共存|其他组分|方向$', risk.name):
            return False
        if kind == 'disease_risk' and not any(claims[key].role in {'outcome','etiology','mechanism'}
                and claims[key].evidence for key in ids) and not any(_personal_clinical_disease_assessment(
                    claims[key], risk) for key in ids) and not any(claims[key].role == 'source_fact'
                and re.search(r'相关(?:菌群)?(?:风险|信号|特征)|风险(?:提示|信号)|筛查', claims[key].text) for key in ids):
            return False
    risk.entity_kind = kind
    risk._entity_kind_receipt = _entity_kind_fingerprint(risk)
    return True


def indicator_problem_outcome_links(document):
    """Read-only display links from a lab problem to a reviewed disease risk.

    The two assessments remain distinct.  This grants no same-disease merge,
    new diagnosis, shared index or publication approval.  Projection may use
    the current problem as supporting clinical data beneath the outcome's
    own independently reviewed index/grade.  A serialized registry has no
    live authority and therefore yields no links.
    """
    from .evaluation_evidence_plan import _mentions, risk_retrieval_concepts
    rows = approved_assessments(document)
    facts, registry = _facts(document), _registry(document)
    patterns = {'血脂异常', '血脂异常状态', '血糖异常', '糖代谢异常',
        '血压偏高', '肝功能异常', '肾功能异常'}
    sources = [row for row in rows if row.certainty in {'confirmed', 'current_problem'}
        and _candidate_name(row.name) in patterns]
    links = []
    for source in sources:
        source_quotes = {fid: claim.claim_id for claim in source.claims if claim.role == 'source_fact'
            for fid, quote in claim.fact_quotes.items() if fid in facts and quote in facts[fid].value
            and _clinical_observed_abnormal(facts[fid])}
        if not source_quotes:
            continue
        for outcome in rows:
            identity = _risk_identity(outcome.name)
            if (outcome.risk_id == source.risk_id or identity == _risk_identity(source.name)
                    or _nonspecific_clinical_endpoint(outcome.name) or _workflow_endpoint(outcome.name)):
                continue
            for claim in outcome.claims:
                if (claim.role not in {'outcome', 'etiology', 'mechanism'} or not claim.evidence
                        or any(anchor.direction != 'support' for anchor in claim.evidence)
                        or not all(anchor.evidence_key in registry._evidence_hashes for anchor in claim.evidence)
                        or identity not in {_risk_identity(_endpoint_name(term)) for term in claim.outcome_terms}):
                    continue
                shared = set(source_quotes) & _scientific_premise_ids(claim, outcome, facts)
                shared = {fid for fid in shared if claim.fact_quotes.get(fid)
                    and claim.fact_quotes[fid] in facts[fid].value}
                terms = risk_retrieval_concepts([outcome.name, *claim.outcome_terms])
                if not shared or not any(_mentions(anchor.quote, terms) for anchor in claim.evidence):
                    continue
                links.append({'problemRiskId': source.risk_id, 'outcomeRiskId': outcome.risk_id,
                    'problemName': source.name, 'outcomeName': outcome.name,
                    'sharedFactIds': sorted(shared),
                    'sourcePositiveFactIds': sorted(source_quotes),
                    'sourceClaimIds': sorted({source.risk_id + ':' + source_quotes[fid] for fid in shared}),
                    'relationClaimIds': [outcome.risk_id + ':' + claim.claim_id],
                    'evidenceKeys': sorted({anchor.evidence_key for anchor in claim.evidence}),
                    'displayRelation': 'clinical_support_for_distinct_disease_outcome'})
    merged = {}
    for link in links:
        pair = (link['problemRiskId'], link['outcomeRiskId'])
        if pair not in merged:
            merged[pair] = link
        else:
            for key in ('sharedFactIds', 'sourceClaimIds', 'relationClaimIds', 'evidenceKeys'):
                merged[pair][key] = sorted(set(merged[pair][key]) | set(link[key]))
    for link in merged.values():
        link['coversAllPositiveClinicalFacts'] = set(link['sourcePositiveFactIds']) <= set(link['sharedFactIds'])
    return list(merged.values())


def risk_reference_numbers(document, assessment=None):
    prefix = 'integratedRisk:' + assessment.risk_id + ':' if assessment is not None else 'integratedRisk:'
    return sorted({number for block in document.sections if block.code.startswith(prefix)
        for number in block.evidence_refs})


def risk_assessment_views(document, evidence_summaries=None):
    originals = _originals(evidence_summaries or [])
    views = []
    for row in approved_assessments(document):
        value = row.model_dump(mode='json', by_alias=True)
        value.update(factIds=list(row.support_fact_ids), summary=row.conclusion,
            evidenceRefs=risk_reference_numbers(document, row) or sorted({originals[key]['number']
                for key in row.evidence_keys if key in originals and type(originals[key]['number']) is int}))
        views.append(value)
    return views


def bind_registry_citations(document, evidence_summaries):
    """Bind exact reviewed claims to current citation numbers for retention.

    Each claim is a distinct block; a broad paper or paragraph cannot support
    every risk by union. Standard reference retention can renumber these
    blocks without changing their stable risk and claim identities.
    """
    from .schemas import ClinicalReportBlock
    originals = _originals(evidence_summaries)
    document.sections = [block for block in document.sections if not block.code.startswith('integratedRisk:')]
    for assessment in approved_assessments(document):
        for claim in assessment.claims:
            refs, quotes, claims, fact_ids = [], {}, {}, {}
            for anchor in claim.evidence:
                source = originals.get(anchor.evidence_key)
                number = source.get('number') if source else None
                if type(number) is not int or anchor.quote not in source['sourceText']:
                    continue
                refs.append(number)
                previous = quotes.get(str(number))
                quotes[str(number)] = previous + '\n' + anchor.quote if previous and anchor.quote not in previous else anchor.quote
                claims[str(number)] = claim.text
                fact_ids[str(number)] = sorted(claim.fact_quotes)
            document.sections.append(ClinicalReportBlock(
                code='integratedRisk:' + assessment.risk_id + ':' + claim.claim_id,
                text=claim.text, state='ready', fact_ids=list(claim.fact_quotes),
                evidence_refs=sorted(set(refs)), evidence_quotes=quotes,
                evidence_claims=claims, evidence_fact_ids=fact_ids))


def _current_gaps(*groups):
    """Keep one current gap per risk/role/relation/claim, last attempt wins."""
    result={}
    for group in groups:
        for gap in group:
            if not isinstance(gap,dict):
                continue
            identity=tuple(gap.get(key) for key in ('riskId','missingRole','relationId','claimId'))
            if not identity[0] or not any(identity[1:]):
                identity=(*identity,_digest(gap))
            result[identity]=dict(gap)
    return list(result.values())


async def assess_risks(client, document, evidence_summaries, *, subject=None, risk_ids=None,
                       preserve_existing=False):
    """Evaluate small batches, retaining already reviewed request-local risks."""
    registry = _registry(document)
    refresh_risk_original_scope(document, evidence_summaries)
    for candidate in registry.candidates:
        unrelated=[fid for fid in candidate.opposing_fact_ids if fid in _facts(document)
            and not _source_context_relevant(candidate,_facts(document)[fid])]
        candidate.opposing_fact_ids=[fid for fid in candidate.opposing_fact_ids if fid not in unrelated]
        candidate.background_fact_ids.extend(fid for fid in unrelated if fid not in candidate.background_fact_ids)
        foreign=[fid for fid in candidate.source_model_fact_ids if fid not in _facts(document)
            or _fact_role(_facts(document)[fid])!='source_disease_model'
            or _risk_identity(_facts(document)[fid].name.removesuffix('（来源疾病模型指数）'))!=candidate.disease_id]
        candidate.source_model_fact_ids=[fid for fid in candidate.source_model_fact_ids if fid not in foreign]
        foreign_models={fid for fid in foreign if fid in _facts(document)
            and _fact_role(_facts(document)[fid])=='source_disease_model'}
        candidate.support_fact_ids=[fid for fid in candidate.support_fact_ids if fid not in foreign_models]
        candidate.opposing_fact_ids=[fid for fid in candidate.opposing_fact_ids if fid not in foreign_models]
        candidate.background_fact_ids.extend(fid for fid in foreign if fid in _facts(document)
            and fid not in candidate.background_fact_ids)
        _attach_source_members(document,candidate)
    if subject is not None and registry.subject_snapshot != _subject_view(subject):
        registry.subject_snapshot = _subject_view(subject)
        registry._proofs.clear()
    approved = {row.risk_id: row for row in approved_assessments(document)}
    previous_proofs = dict(registry._proofs)
    previous_gaps=_current_gaps(registry.gaps)
    previous_management_gaps = [dict(gap) for gap in previous_gaps if gap.get('missingRole') == 'management']
    requested = set(risk_ids) if risk_ids is not None else None
    pending = [candidate for candidate in registry.candidates if (
        candidate.risk_id in requested if requested is not None else candidate.risk_id not in approved)]
    if not pending:
        return registry
    gate = asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
    batch_gaps=[];completed=set()

    async def run_batch(candidates):
        async with gate:
            local = document.model_copy(deep=True)
            try:
                from .evaluation_risk_source_links import transfer_evaluation_source_links
                transfer_evaluation_source_links(document,local)
            except ImportError:
                pass
            local.risk_registry.candidates = candidates
            selected={candidate.risk_id for candidate in candidates}
            # A clone must not bring unrelated risks' old gaps back into
            # every concurrent result. Its output is this attempt only.
            local.risk_registry.gaps=[]
            if requested is not None and not preserve_existing:
                selected = {candidate.risk_id for candidate in candidates}
                local.risk_registry.assessments = [row for row in local.risk_registry.assessments
                    if row.risk_id not in selected]
                for identifier in selected:
                    local.risk_registry._proofs.pop(identifier, None)
            await _assess_risk_batch(client, local, evidence_summaries, subject=subject)
            return local.risk_registry

    if requested is not None and not preserve_existing:
        for identifier in requested:
            approved.pop(identifier, None)
            registry._proofs.pop(identifier, None)
    facts=_facts(document)
    originals=_originals(evidence_summaries)
    batches=[]
    for offset in range(0,len(pending),4):
        batch=pending[offset:offset+4]
        keys={row['evidenceKey'] for candidate in batch for purpose in ('relationship','management')
            for row in _candidate_evidence(candidate,originals,purpose=purpose,subject=subject)[:5]}
        cost=len(json.dumps({'risks':[_candidate_view(candidate,facts) for candidate in batch],
            'evidence':[_model_original(originals[key]) for key in keys]},ensure_ascii=False))
        batches.extend([batch[:2],batch[2:]] if cost>35000 and len(batch)>2 else [batch])
    results = await asyncio.gather(*(run_batch(batch) for batch in batches), return_exceptions=True)
    for batch,result in zip(batches,results):
        selected={candidate.risk_id for candidate in batch}
        if not isinstance(result, RiskRegistry):
            registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_UNAVAILABLE')
            continue
        registry._evidence_hashes.update(result._evidence_hashes)
        registry._original_scope_snapshot.update(result._original_scope_snapshot)
        for row in result.assessments:
            if row.risk_id not in selected:
                continue
            proof = result._proofs.get(row.risk_id)
            if proof == _proof(row, document, result._evidence_hashes):
                approved[row.risk_id] = row
                registry._proofs[row.risk_id] = proof
        completed.update(selected)
        batch_gaps.extend(gap for gap in result.gaps if gap.get('riskId') in selected)
        registry.incomplete.extend(result.incomplete)
    registry.assessments = list(approved.values())
    registry.gaps=_current_gaps([gap for gap in previous_gaps if gap.get('riskId') not in completed],batch_gaps)
    registry.gaps=[gap for gap in registry.gaps if gap.get('riskId') not in approved
        or gap.get('missingRole') in {'management','risk_relationship','disease_endpoint','disease_hypothesis'}]
    if preserve_existing:
        registry.gaps.extend(gap for gap in previous_management_gaps
            if gap.get('riskId') in approved
            and registry._proofs.get(gap['riskId']) == previous_proofs.get(gap['riskId']))
    for row in approved.values():
        if row.index >= 25 and not (row.lifestyle_actions or row.clinical_actions):
            source = next((candidate for candidate in registry.candidates if candidate.risk_id == row.risk_id), None)
            if not any(gap.get('riskId') == row.risk_id and gap.get('missingRole') == 'management'
                    for gap in registry.gaps):
                registry.gaps.append({'riskId': row.risk_id, 'reason': 'no_reviewed_personal_action',
                    'missingRole': 'management', 'searchTerms': source.search_terms if source else []})
    if len(approved) == len(registry.candidates):
        registry.incomplete = [reason for reason in registry.incomplete
            if not reason.startswith('INTEGRATED_RISK_')]
    else:
        registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_INCOMPLETE')
    if any(gap.get('missingRole') == 'management' for gap in registry.gaps):
        registry.incomplete.append('INTEGRATED_RISK_MANAGEMENT_INCOMPLETE')
    if any(gap.get('missingRole') == 'risk_relationship' for gap in registry.gaps):
        registry.incomplete.append('INTEGRATED_RISK_RELATIONSHIP_INCOMPLETE')
    registry.gaps=_current_gaps(registry.gaps)
    registry.incomplete = list(dict.fromkeys(registry.incomplete))
    await _consolidate_reviewed_targets(client,document,evidence_summaries,subject=subject)
    await repair_missing_management_actions(client,document,evidence_summaries,subject=subject)
    await _link_supporting_signal_topics(client,document)
    registry.incomplete=list(dict.fromkeys(registry.incomplete))
    bind_registry_citations(document, evidence_summaries)
    return registry


def _draft_claim_errors(claim,candidate,facts,originals):
    allowed=set([*candidate.support_fact_ids,*candidate.background_fact_ids,
        *candidate.opposing_fact_ids,*candidate.source_model_fact_ids])
    if claim.role=='source_fact':
        return _source_observation_errors(claim,candidate,facts)
    errors=[]
    exposure=' '.join(term for group in claim.exposure_terms for term in group)
    if re.search(r'富含甘油三酯.{0,8}脂蛋白|脂蛋白残粒|残粒胆固醇|triglyceride.rich lipoprotein|\bTRLs?\b|remnant', exposure, re.I):
        measured=[facts[fid] for fid in claim.fact_quotes if fid in facts and facts[fid].kind=='clinical']
        if not any(re.search(r'富含甘油三酯.{0,8}脂蛋白|脂蛋白残粒|残粒胆固醇|triglyceride.rich lipoprotein|\bTRL(?:s|-?C)?\b|remnant',
                fact.name,re.I) for fact in measured):
            errors.append('triglyceride_measurement_is_not_TRL_or_remnant_exposure')
    if _valid_fact_quotes(claim.fact_quotes,facts,allowed) and _negative_patient_clauses(claim.text):
        errors.extend(_patient_negative_predicate_errors(claim.text,claim.fact_quotes,facts))
    if not _valid_fact_quotes(claim.fact_quotes,facts,allowed):
        errors.append('invalid_patient_quote')
    if set(claim.fact_quotes)&set(candidate.opposing_fact_ids):
        errors.append('counterevidence_in_public_prose')
    if _BOUNDARY.search(claim.text):
        errors.append('public_rule_text')
    if not claim.evidence:
        errors.append('medical_relation_missing_original_evidence')
    for anchor in claim.evidence:
        if (anchor.evidence_key not in originals or anchor.quote not in originals[anchor.evidence_key]['sourceText']
                or anchor.direction=='oppose'):
            errors.append('invalid_or_opposing_evidence_quote')
    errors.extend(_role_evidence_errors(claim,originals))
    errors.extend(_relationship_endpoint_errors(claim,candidate,facts,originals))
    errors.extend(_management_patient_scope_errors(claim,candidate,facts,originals))
    errors.extend(_patient_source_role_errors(claim,facts))
    if claim.role in {'outcome','etiology','mechanism'}:
        from .evaluation_evidence_plan import _mentions
        patient=_patient_premise_text(claim,candidate,facts)
        from .evaluation_evidence_plan import risk_retrieval_concepts
        if any(not _mentions(patient,_concept_forms(group)) for group in claim.exposure_terms):
            errors.append('exposure_metadata_not_bound_to_assertion_or_patient')
        if set(claim.fact_quotes)-_scientific_premise_ids(claim,candidate,facts):
            errors.append('scientific_claim_contains_unbound_background_fact')
        if claim.outcome_terms and not _mentions(claim.text+' '+candidate.name,_concept_forms(claim.outcome_terms)):
            errors.append('outcome_metadata_not_bound_to_asserted_risk')
    return list(dict.fromkeys(errors))


def _patient_premise_text(claim,candidate,facts):
    """Exposure premises come from adopted observations, never the new claim."""
    text=' '.join(facts[fid].name+' '+facts[fid].value for fid in claim.fact_quotes if fid in facts)
    # A source-verified abnormal lipid measurement is a legitimate clinical
    # state synonym. This adds no fact and never diagnoses CAD/diabetes.
    lipid=RiskCandidate(risk_id='premise:lipid',disease_id='hyperlipidemia',name='血脂异常',
        category='metabolic',support_fact_ids=list(claim.fact_quotes),relationship='本人实测血脂异常状态')
    observed=claim.model_copy(update={'role':'source_fact'})
    if _same_observed_problem(lipid,[observed],facts):
        text+=' 血脂异常 高脂血症 dyslipidemia dyslipidaemia hyperlipidemia'
    if re.search(r'(?:排便|硬便).{0,25}(?:需要|需|很)?用力|用力排便',text):
        text+=' 排便费力 排便用力 straining'
    for fid in claim.fact_quotes:
        fact=facts.get(fid)
        if fact is None or fact.kind!='clinical' or not fact.abnormal:
            continue
        if re.search(r'偏低|降低|↓',fact.status or ''):
            continue
        if fact.name in {'腰围','waist circumference','WC'}:
            text+=' 腰围偏高 腹型肥胖 central obesity abdominal obesity'
        if fact.name in {'BMI','体质指数','体重指数'}:
            text+=' 体重超标 超重 overweight'
    return text


def _endpoint_name(value):
    value=re.sub(r'(?:的)?(?:发生|患病|发病)?(?:风险|发生率|发病率)(?:增加|升高|降低|减少)?$','',str(value).strip())
    value=re.sub(r'^(?:incident\s+|risk of (?:developing\s+)?|incidence of\s+)','',value,flags=re.I)
    value=re.sub(r'\s+(?:risk|susceptibility|incidence)$','',value,flags=re.I)
    return value.strip(' ，。')


def _quoted_endpoint_entities(terms,claim,originals):
    from .evaluation_catalog import fixed_disease_code
    from .evaluation_evidence_plan import _mentions
    names=[_endpoint_name(term) for term in terms if isinstance(term,str) and term.strip()
        and not _workflow_endpoint(term) and not _nonspecific_clinical_endpoint(term)]
    known={_risk_identity(name) for name in names if fixed_disease_code(name)
        or _risk_identity(name) in _CLINICAL_ENTITY_ALIASES}
    if not names or len(known)>1:
        return {}
    display=next((name for name in names if re.search(r'[\u4e00-\u9fff]',name)),names[0])
    identity=next(iter(known),_risk_identity(display))
    for anchor in claim.evidence:
        source=originals.get(anchor.evidence_key,{})
        definition=str(source.get('title') or '')+' '+source.get('sourceText','')
        aliases=_concept_forms(terms,definition)
        if _mentions(anchor.quote,aliases) or _defined_quote_mention(anchor.quote,aliases,definition):
            return {identity:display}
    return {}


def _relationship_endpoint_errors(claim,candidate,facts,originals):
    if claim.role not in {'outcome','etiology','mechanism'}:
        return []
    from .evaluation_catalog import fixed_disease_code
    outcomes=_quoted_endpoint_entities(claim.outcome_terms,claim,originals)
    exposures=_quoted_endpoint_entities([term for group in claim.exposure_terms for term in group],claim,originals)
    identity=_risk_identity(candidate.name)
    observed=claim.model_copy(update={'role':'source_fact'})
    if (candidate.certainty in {'confirmed','current_problem','self_reported'}
            or _same_observed_problem(candidate,[observed],facts)):
        if identity in outcomes or identity in exposures:
            return []
        # A current umbrella problem can be the exposure through its actual
        # abnormal constituent (LDL/TG -> ASCVD). Incidental source facts in
        # the same sentence cannot bind an unrelated exposure to that problem.
        premises=_scientific_premise_ids(claim,candidate,facts)
        bound=observed.model_copy(update={'fact_quotes':{fid:quote for fid,quote in claim.fact_quotes.items()
            if fid in premises}})
        if exposures and _same_observed_problem(candidate,[bound],facts):
            return []
        return ['relationship_endpoints_do_not_match_current_problem']
    precise=bool(fixed_disease_code(identity) or identity in _CLINICAL_ENTITY_ALIASES
        or _declared_risk_signal_ids(candidate,facts))
    if precise and identity not in outcomes:
        return ['relationship_outcome_is_different_disease']
    return []


def _align_reviewed_targets(document,sets,candidates,facts,originals):
    """Resolve only actual reviewed disease endpoints, never a broad heading."""
    registry=_registry(document)
    for row in sets:
        candidate=candidates[row.risk_id]
        if (candidate.certainty!='potential_risk' or not candidate.disease_id.startswith('disease-')
                or _declared_risk_signal_ids(candidate,facts)):
            continue
        endpoints={identity:name for claim in row.claims if claim.role in {'outcome','etiology','mechanism'}
            for identity,name in _quoted_endpoint_entities(claim.outcome_terms,claim,originals).items()}
        # A previously unknown rare disease can have a translated alias set;
        # the independent entailment review has already verified that set.
        own=any(_risk_identity(_endpoint_name(term))==candidate.disease_id for claim in row.claims
            for term in claim.outcome_terms)
        if own:
            continue
        if len(endpoints)==1:
            identity,name=next(iter(endpoints.items()))
            candidate.disease_id=identity;candidate.name=name+'相关风险'
            from .evaluation_catalog import disease_category,fixed_disease_code
            if fixed_disease_code(identity):
                candidate.category=disease_category(code=identity,name=name)
        elif endpoints:
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment',
                'reason':'multiple_independent_outcomes_need_separate_candidates'})
    return sets


async def _unify_same_current_problems(client,document):
    """Same symptom meaning needs independent adjudication, not shared IDs."""
    registry=_registry(document);facts=_facts(document)
    rows=[row for row in approved_assessments(document) if row.certainty in
        {'confirmed','current_problem','self_reported'}]
    by_id={row.risk_id:row for row in rows}
    candidates={row.risk_id:row for row in registry.candidates}
    pairs=[]
    for index,left in enumerate(rows):
        for right in rows[index+1:]:
            if (_candidate_merge_scope(candidates.get(left.risk_id,left)) != _candidate_merge_scope(candidates.get(right.risk_id,right))
                    or left.disease_id==right.disease_id or left.category!=right.category
                    or not (left.disease_id.startswith('disease-') or right.disease_id.startswith('disease-'))):
                continue
            if _same_current_constipation_scope(left,right,facts):
                continue  # One editorial family, with both decisions retained.
            left_ids={fid for claim in left.claims if claim.role=='source_fact' for fid in claim.fact_quotes}
            right_ids={fid for claim in right.claims if claim.role=='source_fact' for fid in claim.fact_quotes}
            if left_ids&right_ids:
                pairs.append([left.risk_id,right.risk_id])
    if not pairs or client is None:
        return
    signature=_digest({'pairs':pairs,'facts':_source_digest(document),
        'claims':{row.risk_id:[claim.model_dump() for claim in row.claims if claim.role!='management'] for row in rows}})
    if signature in registry._current_problem_merge_attempts:
        return
    registry._current_problem_merge_attempts.add(signature)
    offered={identifier for pair in pairs for identifier in pair}
    items=[{'riskId':row.risk_id,'name':row.name,'certainty':row.certainty,'diseaseId':row.disease_id,
        'claims':[claim.model_dump(mode='json',by_alias=True) for claim in row.claims if claim.role!='management'],
        'sourceFacts':[_fact_view(facts[fid]) for fid in row.support_fact_ids if fid in facts]}
        for row in rows if row.risk_id in offered]
    try:
        response=await client.generate({'task':'identify_same_current_health_problems','pairs':pairs,'items':items,
            'rules':['只判断已审当前问题是否为同一症状/临床状态的不同描述，不新增疾病或合并不同疾病。',
                '共享问卷factId仅供检索，不能作为同问题依据：同一问答可能同时有便秘与反酸，必须按各自已审source句的具体症状范围区分。',
                '同一排便频率、便性及费力表现的泛称与明确问题名可为别名；不同疾病、分期/亚型及独立临床后果不可因同来源合并。',
                '只返回输入riskId组和具体语义理由，不能改分；后续合并仍重新独立评定完整core。'],
            'outputSchema':{'groups':[{'riskIds':['输入同问题ID'],'reason':'同一问题的具体语义依据'}]}})
    except Exception:
        return
    groups=[];used=set()
    for raw in response.get('groups',[]) if isinstance(response,dict) else []:
        ids=raw.get('riskIds') if isinstance(raw,dict) else None
        if (not isinstance(ids,list) or len(ids)<2 or len(set(ids))!=len(ids)
                or not set(ids)<=offered or set(ids)&used):
            continue
        if (len({by_id[identifier].category for identifier in ids})!=1
                or len({_candidate_merge_scope(candidates.get(identifier,by_id[identifier])) for identifier in ids})!=1):
            continue
        known=[identifier for identifier in ids if not by_id[identifier].disease_id.startswith('disease-')]
        if len({by_id[identifier].disease_id for identifier in known})>1:
            continue
        primary=(known or ids)[0]
        bindings={identifier:[claim.claim_id for claim in by_id[identifier].claims
            if claim.role=='source_fact'] for identifier in ids}
        groups.append({'primaryRiskId':primary,'riskIds':ids,'sourceClaimBindings':bindings,
            'reason':str(raw.get('reason',''))});used.update(ids)
    if not groups:
        return
    try:
        reviewed=await client.generate({'task':'review_same_current_health_problems','groups':groups,'items':items,
            'rules':['独立核对每组是否同一个当前问题，不沿用作者理由。特别区分复合问卷中的不同主诉，不能仅按来源ID合并。',
                '逐组完整返回primaryRiskId/riskIds/sourceClaimBindings；仅sameCurrentProblem=true、noDistinctDiseaseOrSubtype=true、differentSymptomScopes=[]才准合并。'],
            'outputSchema':{'groups':[{'primaryRiskId':'输入ID','riskIds':[], 'sourceClaimBindings':{},
                'sameCurrentProblem':True,'noDistinctDiseaseOrSubtype':True,'differentSymptomScopes':[]}]}})
    except Exception:
        return
    for group in groups:
        matches=[row for row in reviewed.get('groups',[]) if isinstance(row,dict)
            and row.get('primaryRiskId')==group['primaryRiskId']]
        row=matches[0] if len(matches)==1 else {}
        if (set(row.get('riskIds',[]))!=set(group['riskIds'])
                or row.get('sourceClaimBindings')!=group['sourceClaimBindings']
                or row.get('sameCurrentProblem') is not True or row.get('noDistinctDiseaseOrSubtype') is not True
                or row.get('differentSymptomScopes')!=[]):
            continue
        primary=candidates[group['primaryRiskId']]
        for identifier in group['riskIds']:
            if identifier in candidates:
                candidates[identifier].disease_id=primary.disease_id
                candidates[identifier].name=primary.name
                candidates[identifier].category=primary.category


async def _consolidate_reviewed_targets(client,document,evidence_summaries,*,subject=None):
    """One fresh independent decision for duplicate actual disease targets."""
    registry=_registry(document)
    await _unify_same_current_problems(client,document)
    candidates_by_id={candidate.risk_id:candidate for candidate in registry.candidates}
    grouped={}
    for row in approved_assessments(document):
        candidate=candidates_by_id.get(row.risk_id)
        grouped.setdefault((candidate.disease_id if candidate else row.disease_id,
            _candidate_merge_scope(candidate or row)),[]).append(row)
    for (identity, entity_scope),rows in grouped.items():
        if len(rows)<2:
            continue
        candidates=[candidate for candidate in registry.candidates if candidate.disease_id==identity
            and _candidate_merge_scope(candidate)==entity_scope]
        approved_ids={row.risk_id for row in rows}
        primary=next((candidate for candidate in candidates
            if candidate.risk_id=='risk-'+_digest(identity)[:20] and candidate.risk_id in approved_ids),
            next(candidate for candidate in candidates if candidate.risk_id in approved_ids))
        retired={candidate.risk_id for candidate in candidates if candidate is not primary}
        for candidate in candidates:
            if candidate is not primary:
                _merge_candidate(primary,candidate)
        primary._retrieval_aliases=list(dict.fromkeys([*primary._retrieval_aliases,*retired]))
        # A prior approval is retained until the merged, complete target has
        # a new independently reviewed index. Never take max or average.
        previous=next(row for row in rows if row.risk_id==primary.risk_id)
        registry.assessments=[row for row in registry.assessments if row.risk_id not in retired]
        registry.candidates=[candidate for candidate in registry.candidates if candidate.risk_id not in retired]
        registry.gaps=_current_gaps([{**gap,'riskId':primary.risk_id} if gap.get('riskId') in retired else gap
            for gap in registry.gaps])
        for disposition in registry.coverage_dispositions:
            if isinstance(disposition.get('riskIds'),list):
                disposition['riskIds']=list(dict.fromkeys(primary.risk_id if key in retired else key
                    for key in disposition['riskIds']))
        local=document.model_copy(deep=True)
        try:
            from .evaluation_risk_source_links import transfer_evaluation_source_links
            transfer_evaluation_source_links(document,local)
        except ImportError:
            pass
        local.risk_registry.candidates=[primary]
        merged_atoms=[];seen_atoms=set();seen_ids=set()
        for old in rows:
            for claim in old.claims:
                if claim.role=='management':
                    continue
                atom=claim.model_copy(deep=True)
                signature=_digest(atom.model_dump(exclude={'claim_id'}))
                if signature in seen_atoms:
                    continue
                if atom.claim_id in seen_ids:
                    atom.claim_id='retained:'+_digest([old.risk_id,signature])[:18]
                seen_atoms.add(signature);seen_ids.add(atom.claim_id);merged_atoms.append(atom)
        local.risk_registry._retained_atoms[primary.risk_id]={
            'diseaseId':primary.disease_id,'claims':merged_atoms,
            'proof':_retained_atoms_digest(merged_atoms,primary.disease_id,local,_originals(evidence_summaries))}
        local.risk_registry.assessments=[];local.risk_registry._proofs={};local.risk_registry.gaps=[]
        try:
            await _assess_risk_batch(client,local,evidence_summaries,subject=subject)
            fresh=next((row for row in approved_assessments(local) if row.risk_id==primary.risk_id),None)
        except Exception:
            fresh=None
        if fresh is not None:
            registry.assessments=[row for row in registry.assessments if row.risk_id!=primary.risk_id]+[fresh]
            registry._evidence_hashes.update(local.risk_registry._evidence_hashes)
            registry._original_scope_snapshot.update(local.risk_registry._original_scope_snapshot)
            registry._proofs[primary.risk_id]=_proof(fresh,document,registry._evidence_hashes)
            registry.gaps=_current_gaps([gap for gap in registry.gaps if gap.get('riskId')!=primary.risk_id],
                [gap for gap in local.risk_registry.gaps if gap.get('missingRole') in {'management','risk_relationship'}])
        else:
            # The old decision remains explicitly partial; no new score is
            # manufactured from the multiple previous numbers.
            registry._proofs[primary.risk_id]=_proof(previous,document,registry._evidence_hashes)
            registry.gaps=_current_gaps(registry.gaps,[{'riskId':primary.risk_id,'missingRole':'risk_assessment',
                'reason':'merged_target_requires_fresh_independent_index'}])
            registry.incomplete.append('INTEGRATED_RISK_CONSOLIDATION_INCOMPLETE')
        for key in retired:
            registry._proofs.pop(key,None)


def _scientific_premise_ids(claim,candidate,facts):
    from .evaluation_evidence_plan import _mentions,risk_retrieval_concepts
    result=set()
    for fid,quote in claim.fact_quotes.items():
        atom=claim.model_copy(update={'fact_quotes':{fid:quote}})
        patient=_patient_premise_text(atom,candidate,facts)
        if any(_mentions(patient,_concept_forms(group)) for group in claim.exposure_terms):
            result.add(fid)
    return result


def _source_support_scope(document,candidate,claims):
    """Source truth and relevance to this disease are separate approvals."""
    facts=_facts(document)
    relevant={fid for claim in claims if claim.role in {'outcome','etiology','mechanism'}
        and claim.evidence for fid in _scientific_premise_ids(claim,candidate,facts)}
    relevant.update(fid for claim in claims if _personal_clinical_disease_assessment(claim, candidate, facts)
        for fid in claim.fact_quotes if facts[fid].kind in {'clinical','self_reported'})
    relevant.update(fid for claim in claims if _lipid_subtype_name(candidate.name)
        and _personal_lipid_classification(claim, candidate.name, facts)
        for fid in claim.fact_quotes if fid in facts and facts[fid].kind == 'clinical')
    allowed=set(_declared_risk_signal_ids(candidate,facts))|relevant
    if candidate.certainty in {'confirmed','current_problem','self_reported'}:
        reported={source.fact_id for problem in document.problems if problem.certainty=='self_reported'
            for source in problem.sources}
        for claim in claims:
            if claim.role!='source_fact':
                continue
            for fid,quote in claim.fact_quotes.items():
                fact=facts.get(fid)
                atom=claim.model_copy(update={'fact_quotes':{fid:quote}})
                if (fact is not None and (_same_observed_problem(candidate,[atom],facts)
                        or candidate.certainty=='self_reported' and fact.kind=='self_reported'
                        and (fid in reported or fid.startswith('self-reported-fact-')))):
                    allowed.add(fid)
    retained=[];mixed=[];background=[]
    for claim in claims:
        if (candidate.certainty=='potential_risk' and claim.role=='diagnostic_principle'
                and not _personal_clinical_disease_assessment(claim, candidate, facts)):
            background.append(claim)
        elif claim.role!='source_fact' or set(claim.fact_quotes)<=allowed:
            retained.append(claim)
        elif set(claim.fact_quotes)&allowed:
            mixed.append((claim,sorted(set(claim.fact_quotes)&allowed)))
        else:
            background.append(claim)
    return retained,mixed,background


async def _scope_risk_source_claims(client,document,sets,candidates,facts,originals,*,subject=None):
    """Do not turn a truthful unrelated observation into disease support."""
    registry=_registry(document);retained={};repairs=[];new_semantics={}
    for row in sets:
        candidate=candidates[row.risk_id]
        if candidate.certainty=='potential_risk' and _same_observed_problem(candidate,row.claims,facts):
            candidate.certainty='current_problem'
        accepted,mixed,background=_source_support_scope(document,candidate,row.claims)
        for claim in [*background,*[claim for claim,_ in mixed]]:
            registry.gaps.append({'riskId':row.risk_id,'claimId':claim.claim_id,
                'missingRole':'risk_relationship','factIds':list(claim.fact_quotes),
                'reason':'source_truth_is_not_this_disease_support','searchTerms':candidate.search_terms})
        for claim,ids in mixed:
            repairs.append({'riskId':row.risk_id,'claim':claim.model_dump(mode='json',by_alias=True),
                'allowedSupportFactIds':ids})
        retained[row.risk_id]=accepted
    if repairs:
        try:
            response=await client.generate({'task':'repair_integrated_risk_source_scope','items':repairs[:6],
                'facts':[_fact_view(facts[fid]) for fid in {fid for item in repairs[:6]
                    for fid in item['allowedSupportFactIds']}],
                'rules':['只拆分这批混合source句一次：保留allowedSupportFactIds实际支持的同病signal或当前状态，其他真实资料仅内部背景。不得程序式升为确诊。',
                    'claimId/riskId不变，role只能source_fact，不加文献、病因或新事实。只忠实表述给出的真实factQuotes；不能拆则omit。'],
                'outputSchema':{'repairs':[{'riskId':'原ID','claim':{'claimId':'原ID','role':'source_fact',
                    'text':'仅本病实际信号/状态','factQuotes':{}}}]}})
            allowed={(item['riskId'],item['claim']['claimId']):set(item['allowedSupportFactIds']) for item in repairs[:6]}
            pending=[]
            for item in response.get('repairs',[]) if isinstance(response,dict) else []:
                try:
                    identifier=item['riskId'];claim=RiskClaim.model_validate(item['claim'])
                    claim=_scope_adopted_symptom_claim(document,candidates[identifier],claim)
                    if (claim.role!='source_fact' or not set(claim.fact_quotes)<=allowed.get((identifier,claim.claim_id),set())
                            or _draft_claim_errors(claim,candidates[identifier],facts,originals)):
                        continue
                    pending.append(_RiskClaimSet(risk_id=identifier,claims=[claim]))
                except (ValueError,TypeError,KeyError,ValidationError):
                    continue
            checked=await _review_claim_semantics(client,pending,candidates,facts,originals,strict_sources=True,subject=subject) if pending else {}
            new_semantics.update(checked)
            for row in pending:
                for claim in row.claims:
                    if checked.get(row.risk_id+':'+claim.claim_id) is True:
                        retained[row.risk_id].append(claim)
        except Exception:
            pass
    # Recover a real adopted same-disease signal, without manufacturing a
    # clinical relationship, when a mixed sentence could not be repaired.
    pending=[]
    for identifier,claims in retained.items():
        declared=set(_declared_risk_signal_ids(candidates[identifier],facts))
        additions=[]
        if declared and not any(claim.role=='source_fact' and set(claim.fact_quotes)&declared for claim in claims):
            additions.extend(_source_signal_claim(candidates[identifier],facts))
        if candidates[identifier].certainty in {'confirmed','current_problem','self_reported'} and not any(
                claim.role=='source_fact' and any(facts[fid].kind in {'clinical','self_reported'}
                    for fid in claim.fact_quotes) for claim in claims):
            fallback=_conservative_observations(candidates[identifier],facts)
            accepted,_,_=_source_support_scope(document,candidates[identifier],fallback)
            additions.extend(accepted)
        if additions:
            pending.append(_RiskClaimSet(risk_id=identifier,claims=[
                _scope_adopted_symptom_claim(document,candidates[identifier],claim) for claim in additions]))
    if pending:
        checked=await _review_claim_semantics(client,pending,candidates,facts,originals,strict_sources=True,subject=subject)
        new_semantics.update(checked)
        for row in pending:
            retained[row.risk_id].extend(claim for claim in row.claims
                if checked.get(row.risk_id+':'+claim.claim_id) is True)
    return [_RiskClaimSet(risk_id=identifier,claims=claims) for identifier,claims in retained.items()],new_semantics


def _retained_atoms_digest(claims,disease_id,document,originals):
    return _digest({'diseaseId':disease_id,'facts':_source_digest(document),
        'claims':[claim.model_dump(mode='json',by_alias=True) for claim in claims],
        'originals':{anchor.evidence_key:_digest(originals[anchor.evidence_key]['sourceText'])
            for claim in claims for anchor in claim.evidence if anchor.evidence_key in originals}})


def _retained_core_claims(document,candidates,facts,originals):
    retained={}
    for assessment in approved_assessments(document):
        candidate=candidates.get(assessment.risk_id)
        if candidate is None or assessment.disease_id!=candidate.disease_id:
            continue
        claims=[]
        managed={identifier for action in [*assessment.lifestyle_actions,*assessment.clinical_actions]
            if _approved_management_plan(action,assessment,facts,originals) for identifier in action.claim_ids}
        for claim in assessment.claims:
            if (claim.role=='management' and claim.claim_id not in managed
                    or not _valid_fact_quotes(claim.fact_quotes,facts,facts)):
                continue
            if any(anchor.evidence_key not in originals or
                    anchor.quote not in originals[anchor.evidence_key]['sourceText'] for anchor in claim.evidence):
                continue
            claims.append(claim.model_copy(deep=True))
            known=set([*candidate.support_fact_ids,*candidate.background_fact_ids,*candidate.opposing_fact_ids])
            candidate.background_fact_ids.extend(fid for fid in claim.fact_quotes if fid not in known)
        if claims:
            retained[assessment.risk_id]=claims
    for identifier,entry in _registry(document)._retained_atoms.items():
        candidate=candidates.get(identifier)
        claims=entry.get('claims',[]) if isinstance(entry,dict) else []
        if (candidate is None or entry.get('diseaseId')!=candidate.disease_id
                or not all(isinstance(claim,RiskClaim) for claim in claims)
                or entry.get('proof')!=_retained_atoms_digest(claims,candidate.disease_id,document,originals)):
            continue
        valid=[claim.model_copy(deep=True) for claim in claims if _valid_fact_quotes(claim.fact_quotes,facts,facts)
            and all(anchor.evidence_key in originals and anchor.quote in originals[anchor.evidence_key]['sourceText']
                for anchor in claim.evidence)]
        retained.setdefault(identifier,[]).extend(valid)
    return retained


async def _review_core_withdrawals(client,retained,raw_by_id,originals,facts):
    proposed=[]
    for identifier,claims in retained.items():
        old={claim.claim_id:claim for claim in claims}
        for row in raw_by_id.get(identifier,{}).get('withdrawnCoreClaims',[]):
            if not isinstance(row,dict) or row.get('claimId') not in old:
                continue
            anchors=row.get('opposingEvidence',[])
            bindings={a.get('evidenceKey'):a.get('quote') for a in anchors if isinstance(a,dict)}
            if (not bindings or len(bindings)!=len(anchors) or any(key not in originals
                    or not isinstance(quote,str) or len(quote)<8 or quote not in originals[key]['sourceText']
                    for key,quote in bindings.items())):
                continue
            claim=old[row['claimId']]
            if set(bindings)<={anchor.evidence_key for anchor in claim.evidence}:
                continue
            proposed.append({'riskId':identifier,'claimId':claim.claim_id,
                'oldClaim':claim.model_dump(mode='json',by_alias=True),
                'patientFacts':[_fact_view(facts[fid]) for fid in claim.fact_quotes],
                'opposingEvidenceQuotes':bindings,'reason':str(row.get('reason',''))})
    if not proposed:
        return set()
    try:
        response=await client.generate({'task':'review_integrated_core_withdrawals','items':proposed[:12],
            'evidence':[_model_original(originals[key]) for key in {key for row in proposed[:12]
                for key in [*row['opposingEvidenceQuotes'],*[a['evidenceKey'] for a in row['oldClaim']['evidence']]]}],
            'rules':['只核新实际反证是否改变旧关系的患者适用/表述；新稿未复写、缺少新引文、词形变化均不能撤销已有逐字绑定的有效关系。',
                '不同研究出现混合关联通常影响强度与指数，不自动否定另一研究的忠实关联。必须明确原文方向/对象/人群或适用前提为何需要撤回。',
                '不改分、不新造医学结论。逐项返回相同riskId/claimId及逐字opposingEvidenceQuotes，只有withdrawalJustified/newEvidenceChangesApplicability均true才撤回。'],
            'outputSchema':{'items':[{'riskId':'原ID','claimId':'原ID','withdrawalJustified':True,
                'newEvidenceChangesApplicability':True,'opposingEvidenceQuotes':{},'reason':'具体原因'}]}})
    except Exception:
        return set()
    if not isinstance(response,dict):
        return set()
    result=set()
    for row in proposed[:12]:
        matches=[item for item in response.get('items',[]) if isinstance(item,dict)
            and item.get('riskId')==row['riskId'] and item.get('claimId')==row['claimId']]
        review=matches[0] if len(matches)==1 else {}
        if (review.get('withdrawalJustified') is True and review.get('newEvidenceChangesApplicability') is True
                and review.get('opposingEvidenceQuotes')==row['opposingEvidenceQuotes']
                and len(str(review.get('reason','')))>8):
            result.add((row['riskId'],row['claimId']))
    return result


async def _assess_claim_drafts(client,document,evidence_summaries,drafts,*,subject=None):
    """Approve atomic content first; only then write a new index decision."""
    registry,facts=_registry(document),_facts(document)
    originals=_originals(evidence_summaries)
    candidates={row.risk_id:row for row in registry.candidates}
    retained_actions={row.risk_id:row.model_copy(deep=True) for row in approved_assessments(document)
        if row.risk_id in candidates and row.disease_id==candidates[row.risk_id].disease_id}
    retained=_retained_core_claims(document,candidates,facts,originals)
    grouped={}
    for raw in drafts:
        if isinstance(raw,dict):
            grouped.setdefault(raw.get('riskId'),[]).append(raw)
    prepared=[];draft_by_id={};semantics={};repairable=[]
    def gap(candidate,claim_id,role,errors):
        registry.gaps.append({'riskId':candidate.risk_id,'claimId':claim_id,
            'missingRole':('management' if role=='management' else 'source_observation'
                if role=='source_fact' else 'risk_relationship'),
            'reason':','.join(errors),'searchTerms':candidate.search_terms})
    for identifier,candidate in candidates.items():
        matches=grouped.get(identifier,[])
        raw=matches[0] if len(matches)==1 else {}
        draft_by_id[identifier]=raw
        for missing in raw.get('relationshipGaps',[]) if isinstance(raw.get('relationshipGaps',[]),list) else []:
            if (isinstance(missing,dict) and isinstance(missing.get('factIds'),list)
                    and set(missing['factIds'])<=facts.keys()):
                registry.gaps.append({'riskId':identifier,'missingRole':'risk_relationship',
                    'relationId':missing.get('relationId'),'factIds':missing['factIds'],
                    'reason':'draft_declared_missing_applicable_relationship',
                    'searchTerms':missing.get('searchTerms',candidate.search_terms)})
        claims=[];seen=set()
        for field,role in (('sourceClaims','source_fact'),('relationshipClaims',None),('managementClaims','management')):
            for proposed in raw.get(field,[]) if isinstance(raw.get(field,[]),list) else []:
                try:
                    value=_normalise_claim_metadata(proposed)
                    if field=='sourceClaims':
                        value={key:value[key] for key in ('claimId','text','factQuotes') if key in value}
                        value.update(role='source_fact',evidence=[])
                    elif role:
                        value['role']=role
                    elif value.get('role') not in {'outcome','etiology','mechanism','diagnostic_principle'}:
                        raise ValueError('relationship claim must declare its scientific duty')
                    claim=RiskClaim.model_validate(value)
                    claim=_scope_adopted_symptom_claim(document,candidate,claim)
                    if (field=='relationshipClaims' and set(_scientific_quote_scope_errors(claim, originals)) & {
                            'management_instruction_is_not_scientific_outcome',
                            'intervention_effect_belongs_to_management'}):
                        # Correct the duty of an unchanged, explicitly
                        # recommended action; never count it as risk evidence.
                        claim=claim.model_copy(update={'role':'management'})
                    elif field=='relationshipClaims' and 'diagnostic_definition_not_personal_future_risk' in _scientific_quote_scope_errors(claim,originals):
                        claim=claim.model_copy(update={'role':'diagnostic_principle'})
                    if claim.claim_id in seen:
                        raise ValueError('duplicate claim identity')
                    seen.add(claim.claim_id)
                    errors=_draft_claim_errors(claim,candidate,facts,originals)
                    if errors:
                        gap(candidate,claim.claim_id,claim.role,errors)
                        repairable.append({'riskId':identifier,'claim':claim.model_dump(mode='json',by_alias=True),
                            'errors':errors})
                    else:
                        claims.append(claim)
                except (TypeError,ValueError,ValidationError) as error:
                    gap(candidate,str(proposed.get('claimId','invalid')) if isinstance(proposed,dict) else 'invalid',
                        role or 'outcome',['invalid_atomic_draft:'+str(error)[:180]])
        observed=set(fid for claim in claims if claim.role=='source_fact' for fid in claim.fact_quotes)
        # A valid screening sentence must not prevent recovery of an actual
        # symptom/clinical anomaly whose draft added an internal-rule suffix.
        recovery=(_conservative_observations(candidate,facts) if not any(
            facts[fid].kind in {'clinical','self_reported'} for fid in observed if fid in facts) else [])
        claims=[*recovery,*claims]
        if not any(claim.role=='source_fact' for claim in claims):
            claims=[*_source_signal_claim(candidate,facts),*claims]
        declared=set(_declared_risk_signal_ids(candidate,facts))
        if declared and not any(claim.role=='source_fact' and set(claim.fact_quotes)&declared for claim in claims):
            claims.extend(_source_signal_claim(candidate,facts))
        claims=[_scope_adopted_symptom_claim(document,candidate,claim) for claim in claims]
        if not claims:
            registry.gaps.append({'riskId':identifier,'missingRole':'risk_assessment','reason':'no_grounded_atomic_content'})
            if not any(row['riskId']==identifier for row in repairable):
                continue
        prepared.append(_RiskClaimSet(risk_id=identifier,claims=claims));draft_by_id[identifier]=raw
    if repairable:
        selected=repairable[:6]
        keys={anchor['evidenceKey'] for row in selected for anchor in row['claim'].get('evidence',[])
            if anchor['evidenceKey'] in originals}
        keys.update(source['evidenceKey'] for row in selected if row['claim']['role']=='management'
            for source in _candidate_evidence(candidates[row['riskId']],originals,purpose='management',subject=subject)[:5])
        try:
            repaired=await client.generate({'task':'repair_integrated_risk_atomic_claims','items':selected,
                'facts':[_fact_view(facts[fid]) for fid in {fid for row in selected for fid in row['claim']['factQuotes']}
                    if fid in facts], 'evidence':[_model_original(originals[key]) for key in keys],
                'rules':[
                    '只修输入至多6条原子句，最多这一次。不得增加病例事实、ID、原文或变成原来的完整故事；同一篇已给原文有正确段落时可重新选逐字quote。',
                    '每条保留riskId/claimId/role。管理一句若有多个谓语/对象而quote只支持部分，只保留确实有据的单一动作；DASH饮食不能凭空附加能量限制，纤维补剂有效不能扩成全谷豆类蔬果食品推荐，腰围下降是目标不能同时证明腰围测量评估价值。',
                    '管理必须是本人可执行的具体动作；“干预包括饮食、活动和行为改变”等分类或原则不是已完成建议。有原文推荐频次/时长/目标时引用其准确范围并合理个体化，不强制无依据数字；只能支持原则而不能给具体动作时omitted并保留管理补检缺口。',
                    '患者未证实存在某病时，不能把该病治疗RCT/Meta的器官疗效直接套用本人。可另选已给的一般超重/血脂/日常症状管理原文给可执行动作；不能只删“改善肝脂/硬度”等病效词后沿用专病治疗剂量。真实菊粉等研究效益可忠实作为管理知识，不扩成整食品/饮水方案或个人补剂剂量处方。',
                    '每个科学句只保留所选原文支持的暴露→结局，患者具体数值不能要求论文包含，exposureTerms用医学对象同义词。不能支持则放omitted，不做常识补写。',
                    'factQuotes必须保留本次已给的真实个人前提，可缩为该暴露实际对应的factId子集，但不能清空变成文献科普。原文缩写/发生风险词形按本篇实际定义处理，不改对象、病种、人群或因果方向。',
                    '只返回修正claim，不改指数/评分，不输出免责声明或反向正常结果。所有修正仍需逐条来源/原文与独立语义审核。'],
                'outputSchema':{'repairs':[{'riskId':'输入ID','claim':{'claimId':'原ID','role':'原role',
                    'text':'仅有据单一原子句','factQuotes':{},'evidence':[],
                    'exposureTerms':[],'outcomeTerms':[]}}],'omitted':[{'riskId':'输入ID','claimId':'原ID'}]}})
            rows=repaired.get('repairs',[]) if isinstance(repaired,dict) else []
            by_risk={row.risk_id:row for row in prepared}
            allowed={(row['riskId'],row['claim']['claimId']):row['claim']['role'] for row in selected}
            previous={(row['riskId'],row['claim']['claimId']):row['claim'] for row in selected}
            for item in rows:
                try:
                    identifier=item['riskId'];value=_normalise_claim_metadata(item['claim'])
                    if not value.get('factQuotes') and value.get('role') in {'outcome','etiology','mechanism'}:
                        supplied=previous.get((identifier,value.get('claimId')),{}).get('factQuotes',{})
                        supplied={fid:quote for fid,quote in supplied.items()
                            if fid in facts and isinstance(quote,str) and quote in facts[fid].value}
                        if supplied:
                            bound=RiskClaim.model_validate({**value,'factQuotes':supplied})
                            matched=_scientific_premise_ids(bound,candidates[identifier],facts)
                            value['factQuotes']={fid:quote for fid,quote in supplied.items() if fid in matched}
                    claim=RiskClaim.model_validate(value)
                    claim=_scope_adopted_symptom_claim(document,candidates[identifier],claim)
                    if allowed.get((identifier,claim.claim_id))!=claim.role or identifier not in by_risk:
                        continue
                    if _draft_claim_errors(claim,candidates[identifier],facts,originals):
                        continue
                    by_risk[identifier].claims.append(claim)
                    registry.gaps=[gap for gap in registry.gaps if not(
                        gap.get('riskId')==identifier and gap.get('claimId')==claim.claim_id)]
                except (TypeError,ValueError,ValidationError,KeyError):
                    continue
        except Exception:
            pass
    withdrawn=await _review_core_withdrawals(client,retained,draft_by_id,originals,facts)
    inherited={}
    for identifier,claims in retained.items():
        row=next((row for row in prepared if row.risk_id==identifier),None)
        if row is None:
            row=_RiskClaimSet(risk_id=identifier,claims=[]);prepared.append(row)
            draft_by_id.setdefault(identifier,{})
        for old in claims:
            if (identifier,old.claim_id) in withdrawn:
                continue
            same=next((claim for claim in row.claims if claim.model_dump(exclude={'claim_id'})==
                old.model_dump(exclude={'claim_id'})),None)
            if same is not None:
                same.claim_id=old.claim_id
            else:
                for claim in row.claims:
                    if claim.claim_id==old.claim_id:
                        claim.claim_id='new:'+_digest(claim.model_dump())[:18]
                row.claims.append(old)
            inherited[identifier+':'+old.claim_id]=True
    if not prepared:
        registry.incomplete.append('INTEGRATED_RISK_DRAFT_INCOMPLETE')
        return registry
    new_sets=[_RiskClaimSet(risk_id=row.risk_id,claims=[claim for claim in row.claims
        if row.risk_id+':'+claim.claim_id not in inherited]) for row in prepared]
    semantics=await _review_claim_semantics(client,[row for row in new_sets if row.claims],
        candidates,facts,originals,strict_sources=True,subject=subject)
    semantics.update(inherited)
    accepted_sets=[]
    for row in prepared:
        accepted=[]
        for claim in row.claims:
            if semantics.get(row.risk_id+':'+claim.claim_id) is True:
                accepted.append(claim)
            else:
                gap(candidates[row.risk_id],claim.claim_id,claim.role,
                    list(claim._applicability_errors) or ['atomic_source_or_original_semantics_rejected'])
        meaningful=[claim for claim in accepted if claim.role!='management']
        if not meaningful:
            continue
        preferred={claim.claim_id for claim in accepted if row.risk_id+':'+claim.claim_id in inherited}
        accepted_sets.append(_RiskClaimSet(risk_id=row.risk_id,
            claims=_deduplicate_relation_atoms(accepted,facts,preferred_ids=preferred)))
    accepted_sets=_align_reviewed_targets(document,accepted_sets,candidates,facts,originals)
    for row in accepted_sets:
        kept=[]
        for claim in row.claims:
            errors=_relationship_endpoint_errors(claim,candidates[row.risk_id],facts,originals)
            if errors:
                gap(candidates[row.risk_id],claim.claim_id,claim.role,errors)
            else:
                kept.append(claim)
        row.claims=kept
    scoped,scope_semantics=await _scope_risk_source_claims(client,document,accepted_sets,
        candidates,facts,originals,subject=subject)
    semantics.update(scope_semantics)
    usable=[]
    for row in scoped:
        accepted=row.claims
        meaningful=[claim for claim in accepted if claim.role!='management']
        if not meaningful:
            continue
        candidate=candidates[row.risk_id]
        if candidate.certainty=='potential_risk' and _same_observed_problem(candidate,meaningful,facts):
            # The initial discovery was a hypothesis. A source-matched
            # abnormality is an existing health problem, while CAD/other
            # future diseases still require a reviewed scientific relation.
            candidate.certainty='current_problem'
        declared=set(_declared_risk_signal_ids(candidate,facts))
        has_declared_signal=any(claim.role=='source_fact' and set(claim.fact_quotes)&declared for claim in meaningful)
        if (candidate.certainty=='potential_risk' and not has_declared_signal and not any(claim.role in {
                'outcome','etiology','mechanism'} and claim.evidence for claim in meaningful)
                and not any(_personal_clinical_disease_assessment(claim, candidate, facts, originals) for claim in meaningful)):
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment',
                'reason':'potential_risk_needs_independently_grounded_relationship','searchTerms':candidate.search_terms})
            continue
        usable.append(_RiskClaimSet(risk_id=row.risk_id,claims=accepted))
    if not usable:
        registry.incomplete.append('INTEGRATED_RISK_DRAFT_INCOMPLETE')
        return registry
    selected_keys={anchor.evidence_key for row in usable for claim in _score_claims(candidates[row.risk_id],row.claims)
        for anchor in claim.evidence}
    payload={'task':'decide_integrated_risks_from_approved_claims','subject':subject or {},
        'scoringRubric':SCORING_RUBRIC,'items':[{'risk':_decision_risk_view(candidates[row.risk_id],facts,row.claims),
            'approvedClaims':[claim.model_dump(mode='json',by_alias=True)
                for claim in _score_claims(candidates[row.risk_id],row.claims)],
            'internalContext':_index_author_context(document,candidates[row.risk_id],facts,row.claims)}
            for row in usable], 'evidence':[_model_original(originals[key]) for key in selected_keys],
        'rules':[
            '只基于已通过逐句来源/原文适用审核的approvedClaims重新综合每个风险的指数。之前任何草稿的旧分数全部作废，不能沿用删除错误断言前的分值。',
            '不存在的关联、被删病因和管理动作不得用于抬高/降低分数。当前真实血脂异常、已采用主诉等可依据本人资料独立作为健康问题，不需要借别的疾病论文确认原始异常。',
            '正向支持只限approvedClaims及positiveSupportFactIds。internalContext中supportEligible=false及providedPatientState完整真实状态只供适用、相反与缺失断言核查，绝不能凭名称、字段存在、旧草稿或同现抬分。所有分支只提交数字，不创作scoringRationale或新临床故事；数字依据由另一个独立审核者逐项核验。',
            '评分前区分已有疾病/异常、本人持续症状与仅来源关联信号。supportBasis=source_signal_only时综合原信号特异性、本人相关状态及相反资料，不能仅凭原模型中高分级使其自动成为主要临床问题；不设统一分数上限，也不拒绝有依据的潜在风险。',
            '不得把已输入但未作为正向支持的真实资料写成未提供，例如体重、腰围、压力、否认病史；缺乏同病关联证据与没有该资料是两回事。完整背景可制约个人适用性，不得自动变成新的正向医学关联。',
            '完整考虑internalContext内相反/正常资料及原模型，但这些只用于内部判断，不添加正文。原模型不能直接乘100或平均，综合关注指数不是患病概率，证据强度独立评定。',
            'sourceOnlyWithoutPersonalFactors=true且没有真正同病的适用限制时，其余资料尚未形成与本病的有效关联；有可比较原等级就保持该等级区间，AI独立评定区间内指数（低风险对应正常）。不要仅以信号特异性枚举或缺少诊断/症状降级，不将原数值乘100。这一条件不约束已有真实同病个人因素、关系或反证的综合判断。',
            'sourceDeclaredRiskFactIds是来源已明确给出的同病风险信号，不是AI凭空新推疾病；可据真实信号与本人资料综合评判，不要求新增独立机制才保留本病。风险信号不等于本人已患病/症状；新机制/病因仍只采用已审科学句。',
            '仅sourceModelFactIds才是本病原始模型；列表为空时不得称“本病原模型低/高风险”。其他疾病模型只可按其真实病名作为背景，例如肝硬化模型不能叫MASLD模型。',
            '常外卖不等于高脂/高油/高盐外卖，不可从未有的饮食属性抬高指数；背景仅按原值考虑，未证明的关系不再补回评分理由。',
            '本任务不创作任何新医学正文/claim/动作/文献。最终正文由已审非management原子句组合。没有充分依据决定指数时返回gap，不填0或正常凑数。',
            '严格只按outputSchema返回numeric-only数字字段，不返回scoringRationale、opposingEvidenceKeys或新临床解释。患者fact ID不是文献evidenceKey；全部真实相反事实及已审原文另由独立审核者核验。未确诊、未主动提及症状或无同病检验不构成风险阴性，不据此机械降级；不设原级硬下限、不机械复制或换算，指数0–100整数且四级与rubric一致。'],
        'outputSchema':{'decisions':[_index_decision_schema(candidates[row.risk_id],facts,row.claims)
            for row in usable],'gaps':[{'riskId':'输入ID','reason':'内部缺口'}]}}
    payload['rules'].extend([
        '所有风险理由都须核本人事实，不仅来源-only。只有问答明确否认的同一对象/谓词才能写否认；未报某病不等于否认，无确诊不等于无症状，家族史只报一种疾病不能推出没有其他疾病家族史。来源-only可按真实同病信号特异性及已知限制完成评判，不必编造症状、诊断或家族阴性来凑理由。',
        '已审同病来源风险信号本身已足以形成待评条目；缺少新增机制/病因不是拒绝综合指数的理由。按真实信号特异性、适用性及确有的已审限制由AI独立评定等级，不预设低关注、不机械复制/换算原模型。只提出数字，由独立审核逐事实绑定真实因素；同批其他item的背景不能借为本病正向，不创作患者不存在的状态。',
        'self_reported表示本人确实报告了该症状；不能仅因未获临床核验而降低实际症状负担。其他部位或其他症状的阴性不自动降低本问题严重度；只有与该问题实际相关且已核验适用的限制可影响分值。'])
    try:
        response=await client.generate(payload)
        decisions=response.get('decisions',[]) if isinstance(response,dict) else []
    except Exception:
        decisions=[]
    wrong=[]
    for row in decisions:
        if not isinstance(row,dict) or row.get('riskId') not in candidates:
            continue
        errors=[*(['unexpected_index_decision_fields'] if set(row)-{
            'riskId','index','level','evidenceStrength','scoringRationale','opposingEvidenceKeys'} else []),
            *_unsupported_food_context(str(row.get('scoringRationale','')),facts),
            *_patient_state_text_errors(str(row.get('scoringRationale','')),facts)]
        if _foreign_model_language(str(row.get('scoringRationale','')),candidates[row['riskId']]):
            errors.append('different_disease_model_in_new_index_reason')
        try:
            opposing=_decision_opposing_keys(row.get('opposingEvidenceKeys',[]),
                candidates[row['riskId']],facts,originals)
            if any(key not in originals for key in opposing):
                errors.append('unknown_opposing_evidence')
        except (TypeError,ValueError):
            errors.append('unknown_opposing_evidence')
        if errors:
            wrong.append({'riskId':row['riskId'],'errors':errors,
                'unsupportedPatientStatements':[clause for clause in _negative_patient_clauses(
                    str(row.get('scoringRationale',''))) if _patient_state_text_errors(clause,facts)]})
    if wrong:
        try:
            identifiers={row['riskId'] for row in wrong}
            repaired=await client.generate({**payload,'items':[item for item in payload['items']
                if item['risk']['riskId'] in identifiers],'repairItems':wrong,
                'rules':[*payload['rules'],'逐项处理repairItems真实错误，只返回fresh numeric-only数字，不给理由或opposingEvidenceKeys。未知文献键/事实错位若导致失败，旧分同样作废，按实际core与全部真实状态重新评定，不能只删除错误键或坏句后沿用旧分。否认诊断不等于无症状，未提供家族史不作阴性。鲜数字仍须新一轮完整独立factor审核。']})
            fixes=repaired.get('decisions',[]) if isinstance(repaired,dict) else []
            decisions=[row for row in decisions if row.get('riskId') not in identifiers]
            decisions.extend(row for row in fixes if row.get('riskId') in identifiers)
        except Exception:
            pass
    preliminary=[]
    for row in usable:
        matches=[decision for decision in decisions if isinstance(decision,dict) and decision.get('riskId')==row.risk_id]
        if len(matches)!=1:
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment','reason':'new_index_decision_missing'})
            continue
        decision=matches[0];candidate=candidates[row.risk_id];raw=draft_by_id[row.risk_id]
        if set(decision)-{'riskId','index','level','evidenceStrength','scoringRationale','opposingEvidenceKeys'}:
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment',
                'reason':'unexpected_index_decision_fields'})
            continue
        if _unsupported_food_context(str(decision.get('scoringRationale','')),facts):
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment',
                'reason':'unreported_food_attribute_in_new_index_reason'})
            continue
        if _foreign_model_language(str(decision.get('scoringRationale','')),candidate):
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment',
                'reason':'different_disease_model_in_new_index_reason'})
            continue
        if _patient_state_text_errors(str(decision.get('scoringRationale','')),facts):
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment',
                'reason':'unbound_negative_patient_state_in_new_index_reason'})
            continue
        support=list(dict.fromkeys(fid for claim in row.claims if claim.role!='management' for fid in claim.fact_quotes))
        opposite=list(candidate.opposing_fact_ids)
        background=[fid for fid in dict.fromkeys(candidate.background_fact_ids+candidate.support_fact_ids+
            [fid for claim in row.claims if claim.role=='management' for fid in claim.fact_quotes])
            if fid not in support and fid not in opposite]
        try:
            actions={}
            approved_management={claim.claim_id:claim for claim in row.claims if claim.role=='management'}
            for field in ('lifestyleActions','clinicalActions'):
                actions[field]=[]
                for action in raw.get(field,[]) if isinstance(raw.get(field,[]),list) else []:
                    ids=action.get('claimIds',[]) if isinstance(action,dict) else []
                    if ids and set(ids)<=approved_management.keys():
                        selected=[approved_management[identifier] for identifier in ids]
                        actions[field].append({**action,'text':''.join(claim.text.strip() for claim in selected),
                            'factIds':list(dict.fromkeys(fid for claim in selected for fid in claim.fact_quotes)),
                            'evidenceKeys':list(dict.fromkeys(a.evidence_key for claim in selected for a in claim.evidence))})
                previous=retained_actions.get(row.risk_id)
                if previous is not None:
                    old_field='lifestyle_actions' if field=='lifestyleActions' else 'clinical_actions'
                    for old_action in getattr(previous,old_field):
                        if (not _approved_management_plan(old_action,previous,facts,originals)
                                or not set(old_action.claim_ids)<=approved_management.keys()
                                or any(approved_management[identifier].model_dump(mode='json',by_alias=True)
                                    !=next(claim.model_dump(mode='json',by_alias=True) for claim in previous.claims
                                        if claim.claim_id==identifier) for identifier in old_action.claim_ids)):
                            continue
                        if not any((value.claim_ids if isinstance(value,RiskAction) else value.get('claimIds',[]))
                                ==old_action.claim_ids for value in actions[field]):
                            actions[field].append(old_action.model_copy(deep=True))
            result=RiskAssessment(risk_id=row.risk_id,disease_id=candidate.disease_id,name=candidate.name,
                category=candidate.category,certainty=candidate.certainty,entity_kind=candidate.entity_kind,index=decision['index'],level=decision['level'],
                evidence_strength=decision['evidenceStrength'],scoring_rationale=decision.get('scoringRationale',''),
                conclusion=''.join(claim.text.strip() for claim in row.claims if claim.role!='management'),
                support_fact_ids=support,opposing_fact_ids=opposite,background_fact_ids=background,
                source_model_fact_ids=candidate.source_model_fact_ids,claims=row.claims,
                evidence_keys=list(dict.fromkeys(a.evidence_key for claim in row.claims for a in claim.evidence)),
                opposing_evidence_keys=_decision_opposing_keys(decision.get('opposingEvidenceKeys',[]),candidate,facts,originals),
                lifestyle_actions=actions['lifestyleActions'],clinical_actions=actions['clinicalActions'])
            _bind_initial_management_actions(result,facts,originals)
            result,rejected=_separate_actions(result,candidate,facts,originals)
            if rejected:
                gap(candidate,'actions','management',['draft_action_not_bound_to_approved_claims'])
            errors=_assessment_errors(result,candidate,facts,originals)
            if errors:
                registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment','reason':','.join(errors)})
            else:
                preliminary.append(result)
        except (TypeError,ValueError,ValidationError,KeyError) as error:
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'risk_assessment','reason':'new_decision_invalid:'+str(error)[:300]})
    if not preliminary:
        registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_INCOMPLETE')
        return registry
    return await _review_assessment_rows(client,document,evidence_summaries,preliminary,
        subject=subject,known_semantics=semantics)


async def _assess_risk_batch(client, document, evidence_summaries, *, subject=None):
    """Author and independently review each candidate using literal originals."""
    registry, facts = _registry(document), _facts(document)
    originals = _originals(evidence_summaries)
    if not registry.candidates or client is None:
        if registry.candidates:
            registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_UNAVAILABLE')
        return registry
    items=[]
    for candidate in registry.candidates:
        relationship=[row['evidenceKey'] for row in _candidate_evidence(candidate, originals,
            purpose='relationship',subject=subject)[:5]]
        management=[row['evidenceKey'] for row in _candidate_evidence(candidate, originals,
            purpose='management',subject=subject)[:5]]
        items.append({'risk':_candidate_view(candidate,facts),
            'retrievalRelationHints': _coverage_relation_hints(candidate, originals, subject=subject),
            'sourcePanelLinks':_source_link_rows(document,[*candidate.support_fact_ids,
                *candidate.background_fact_ids,*candidate.source_model_fact_ids],include_members=False),
            'relationshipEvidenceKeys':relationship,'managementEvidenceKeys':management,
            'evidenceKeys':list(dict.fromkeys([*relationship,*management]))})
    retained=_retained_core_claims(document,{candidate.risk_id:candidate for candidate in registry.candidates},facts,originals)
    for item in items:
        previous=retained.get(item['risk']['riskId'],[])
        item['existingApprovedCore']=[claim.model_dump(mode='json',by_alias=True) for claim in previous]
        item['evidenceKeys']=list(dict.fromkeys([*item['evidenceKeys'],
            *(anchor.evidence_key for claim in previous for anchor in claim.evidence)]))
    selected_keys = {key for item in items for key in item['evidenceKeys']}
    author_payload={'task':'assess_integrated_health_risks','subject':subject or {},
        'items':items,'evidence':[_model_original(originals[key]) for key in selected_keys]}
    author_payload['stage']='atomic_draft_before_scoring'
    author_payload['rules']=[
        'retrievalRelationHints只是在本次同risk/同关系/同患者条件及同sourceText哈希下已核查的逐字原文线索，不是发表授权。优先逐关系读取真实quote并核适用性，仍可判方向不支持或放入缺口；不能因covered跳过逐句独审。',
        '本阶段只写分离的原子草稿，不写综合指数/index/level、完整conclusion或大段故事。先选真实原文片段，再写它能支持的一个关系，评分在原子句通过审核后另行完成。',
        'sourceClaims只忠实呈现本人已采用的异常、当前主诉或确切诊断。按该患者报告的参考范围表达TG/LDL偏高属于来源观察，不是outcome，不需要借肥胖/心血管论文证明检验异常。保留真实分值和原分级。',
        '严格同病source_disease_model或阳性microbiome_association_screen是已经提供的报告风险信号，可在sourceClaims忠实保留“某病相关菌群特征检出/相关风险信号”，不需要另发明新关联才保留本病。不要复抄原模型数值，不当临床诊断或用户症状；新机制/病因仍必须relationshipClaims引用真实适用原文。',
        'sourceClaims没有文献字段，不得塞病因/新疾病关联/新的诊断标准；筛查检出只表示相关菌群筛查，不能写本人已患病或出现该症状；菌群合成评分不能当人体浓度。',
        '相反/正常制约及否认病史只留internalContext，绝不混入本风险sourceClaims、relationshipClaims或将来正文；原报告独立正常项另由其他章节展示。',
        'relationshipClaims先选evidenceKey和逐字quote，再写一条该quote确实证明的关系。逐句选择outcome/etiology/mechanism/diagnostic_principle；不能先编完整故事后找同病名论文贴引用。',
        '原始研究的BACKGROUND/研究目的不是本研究的测得结果，不能用介绍句代替实际暴露→结局证据。通读Methods/Results的暴露与人群：GWAS/cis-eQTL/风险变异/RNA-seq或遗传工具估计不能被改成本人测过遗传异常或普通临床测量的个人预测；无适用正文结果则放relationshipGaps，不为补关系重复引用背景。权威指南/共识的实际综合结论按其真实职责使用。',
        '每条科学关系只包含一个真实医学对象与一个结局，exposureTerms每组为同一对象的准确中英同义词，不含本人数字/单位/频次；患者数值来自factQuotes，论文不必出现1200mL、8小时、3.6等个人数值。不同病因必须拆句。',
        '关系句只写该科学暴露→结局；本人测量/数值及其他背景观察另放sourceClaims或内部背景，不在同一关系句末尾加未关联的本人腰围/血脂名册。factQuotes必须选择该暴露真正对应的个人事实，不能清空，也不为凑个体化把其他事实塞同句。',
        '可能疾病评估不是必须先确诊：potential_risk下可用适用指南及真实本人症状模式写考虑/可能存在某病的diagnostic_principle，说明本句所需真实依据，不声称正式条件全满足或已确诊。当前分类应选适用分类标准，不能以某病导致远期共病的论文替代诊断阈值；未来风险应匹配单次暴露或实际已有纵向资料，不用纵向轨迹研究替代一次测量。',
        '暴露对象与结局均必须出现在所选quote而非仅同一篇论文；定义不能当未来风险关联，管理句不能反推病因，临床与菌群不用一篇论文同时证明。一般lifestyle不能代替纤维/饮水/运动等具体对象。',
        '所有factQuotes、原文quote和ID逐字来自输入。引用实际菌种/菌株、样本、人群和疾病适用性；来源panel的真实成员才是菌群依据，screen positive本身不能循环证明同病风险。镜像事实不是独立证据。',
        'managementClaims由管理/指南原文支持具体生活方式或专科检查动作，可含有据频次时长目标；真实干预效果（如转为地中海饮食可改善胆固醇）也属于管理作用，不放进当前风险发生关系，不和结论重复；不输出药物剂量、补剂处方或移植方案。lifestyleActions/clinicalActions只绑定本条已写管理原子句，文本精确拼接。',
        'managementClaims只从本risk的managementEvidenceKeys选择原文，relationshipEvidenceKeys中的危险因素、定义或背景不能代替管理推荐；没有适用管理源则保留内部缺口。',
        '科学关系要说明已测菌群/功能变化对本人哪种不适或风险有意义，结合已有临床线索表达关注重点；不复抄数值名册、不介绍研究如何设计或把哪些变量当primary/secondary outcome。只报道研究结局定义没有个人健康意义，省略。',
        '指南的筛查/检查/治疗推荐属于managementClaims，不能放outcome作为疾病风险证据；同一建议只留一处。病例人群中的菌属关联可保留属级和关联强度，不升级为本人确定因果或菌种结论。',
        '普通饮食、活动、排便习惯指导可基于可信的本人实际自述，不需要先有功能性便秘/IBS确诊；不能引用专病治疗或菌株RCT冒充这类普适管理，也不承诺个体疗效。',
        '管理必须回应本人已经给出的行为：例如就餐偏晚、夜宵、饮水、运动频次的实际记录，给下一步可执行安排。原文有推荐量时可作合理等价拆分到频次/时长/目标，不超出原文范围，不凭“纤维补剂有效”扩写具体食品。泛“生活方式/饮食指导”不是完成的行动。',
        '临床动作自然说明适合的科室、具体评估/检查和目的；不只写“接受指导”或“无创检查”。资料不足以选定检查时选择专科评估的明确问题，不编造检查剂量或用药。',
        '没有适用原文的关系放relationshipGaps，不写入科学句；真实血脂异常或主诉仍保留sourceClaims，不能因一条病因证据不足让整个实际健康问题消失。',
        'sourceClaim不写来源文件、原模型、AI、检索/审核/限制说明。每风险通常1–3条sourceClaims、至多4条科学关系和2条管理句；不要凑数、不要反复抄同一结论。',
        '本人自述直接写实际症状和持续时间，不附“未核验、尚未核验、待核对”等方法状态，也不把自述改为已确诊。相反/正常制约、无已知诊断等只放内部。',
        'existingApprovedCore是仍有效的已有独审原子句，不能因为本轮只写source或改了词形就删除。新稿只补真正新增事实/关系，完整旧+新有效core在后续重新独立评分，旧分不沿用。若新实际反证需撤回旧claim，填withdrawnCoreClaims并给真实opposingEvidence逐字原文，另行独审；没有新反证不得撤回。',
        '行动须明确本人下一步做什么。“干预包括饮食、活动、行为改变”等分类句或泛指导不算完成建议；引用仅有原则时放管理缺口，不挂lifestyleActions。具体动作无需强制数字，原文确有频次/时长/目标时准确引用并合理拆分。',
        '保持riskId；sourceClaims与relationshipClaims职责严格分开。每个sourceClaim的text、factQuotes应能独立忠实对应原始事实及其本人参考范围。']
    author_payload['outputSchema']={'drafts':[{'riskId':'原ID',
        'withdrawnCoreClaims':[{'claimId':'需撤回的existingApprovedCore原ID','reason':'新实际反证原因',
            'opposingEvidence':[{'evidenceKey':'真实新反证键','quote':'逐字原文'}]}],
        'sourceClaims':[{'claimId':'S1','text':'本人实际异常/主诉简句','factQuotes':{'真实factId':'逐字原值片段'}}],
        'relationshipClaims':[{'claimId':'R1','role':'outcome|etiology|mechanism|diagnostic_principle',
            'evidence':[{'evidenceKey':'先选实际原文键','quote':'先选逐字、适用的关系原文','direction':'support'}],
            'exposureTerms':[['实际医学对象中文','Accurate object English alias']],
            'outcomeTerms':['实际结局中文','Accurate outcome English alias'],
            'factQuotes':{'真实factId':'个人前提的逐字原值'},'text':'仅由所选原文支持的一条关系'}],
        'managementClaims':[{'claimId':'M1','text':'管理原文支持的一条动作','role':'management',
            'factQuotes':{'真实factId':'实际个人前提'},'evidence':[{'evidenceKey':'实际键','quote':'逐字管理原文'}]}],
        'lifestyleActions':[{'text':'M1文本','target':'对应的实际问题','factIds':['与M1一致'],
            'evidenceKeys':['与M1一致'],'claimIds':['M1']}],'clinicalActions':[],
        'internalContext':{'consideredOpposingFactIds':['相反/正常事实只内部'],
            'opposingEvidenceKeys':[],'sourceModelFactIds':['原模型仅内部参考']},
        'relationshipGaps':[{'relationId':'缺口对象','factIds':['真实ID'],'searchTerms':['具体待检索关系']}]}]}
    try:
        response = await client.generate(author_payload)
    except Exception:
        registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_UNAVAILABLE')
        return registry
    if isinstance(response,dict) and isinstance(response.get('drafts'),list):
        return await _assess_claim_drafts(client,document,evidence_summaries,response['drafts'],subject=subject)
    raw_rows = response.get('assessments') if isinstance(response, dict) else None
    if not isinstance(raw_rows, list):
        registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_INVALID')
        return registry
    candidates = {candidate.risk_id: candidate for candidate in registry.candidates}
    preliminary, repair = [], []
    grouped = {}
    for raw in raw_rows:
        if isinstance(raw, dict):
            grouped.setdefault(raw.get('riskId', raw.get('risk_id')), []).append(raw)
    for risk_id, candidate in candidates.items():
        matches = grouped.get(risk_id, [])
        if len(matches) != 1:
            registry.gaps.append({'riskId': risk_id, 'reason': 'missing_or_duplicate_risk_decision',
                'missingRole': 'risk_assessment'})
            continue
        raw = matches[0]
        try:
            row = RiskAssessment.model_validate(raw)
            row,rejected_actions=_separate_actions(row,candidate,facts,originals)
            if rejected_actions:
                registry.gaps.append({'riskId':row.risk_id,'missingRole':'management',
                    'reason':'independent_action_source_or_structure_rejected'})
            errors = _assessment_errors(row, candidate, facts, originals)
            if errors:
                registry.gaps.append({'riskId': row.risk_id, 'reason': ','.join(errors), 'missingRole': 'risk_assessment'})
                if set(errors) <= {'index_level_mismatch', 'invalid_patient_quote', 'invalid_evidence_quote',
                        'evidence_key_mismatch', 'conclusion_contains_unreviewed_text',
                        'action_not_grounded_in_reviewed_claims', 'supporting_facts_not_reviewable',
                        'medical_relation_missing_original_evidence', 'source_models_omitted'}:
                    repair.append({'riskId': risk_id, 'previousAssessment': raw, 'validationErrors': errors})
                continue
            preliminary.append(row)
        except (ValueError, TypeError, ValidationError) as error:
            registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_INVALID')
            repair.append({'riskId': risk_id, 'previousAssessment': raw,
                'validationErrors': str(error)[:2000]})
    if repair:
        try:
            repaired = await client.generate({**author_payload,
                'task': 'repair_integrated_risk_assessments', 'repairItems': repair,
                'items': [item for item in items if item['risk']['riskId'] in {row['riskId'] for row in repair}],
                'rules': [*author_payload['rules'],
                    '只修repairItems，最多此一次。重新核对源事实与原文，不为通过检查编造引文或删除患者前提；不能支持的关系返回gaps。完整返回该风险对象。']})
            repaired_rows = repaired.get('assessments', []) if isinstance(repaired, dict) else []
            by_id = {}
            for raw in repaired_rows:
                if isinstance(raw, dict):
                    by_id.setdefault(raw.get('riskId', raw.get('risk_id')), []).append(raw)
            for item in repair:
                matches = by_id.get(item['riskId'], [])
                if len(matches) != 1:
                    continue
                try:
                    row = RiskAssessment.model_validate(matches[0])
                    row,rejected_actions=_separate_actions(row,candidates[row.risk_id],facts,originals)
                    if rejected_actions:
                        registry.gaps.append({'riskId':row.risk_id,'missingRole':'management',
                            'reason':'independent_action_source_or_structure_rejected'})
                    if not _assessment_errors(row, candidates[row.risk_id], facts, originals):
                        preliminary.append(row)
                except (ValueError, TypeError, ValidationError, KeyError):
                    continue
        except Exception:
            pass
    if not preliminary:
        return registry
    return await _review_assessment_rows(client,document,evidence_summaries,preliminary,subject=subject)


async def _repair_index_attributions(client,document,rows,critiques,*,subject=None,originals=None):
    facts=_facts(document);candidates={row.risk_id:row for row in _registry(document).candidates}
    excluded={row.risk_id:_rejected_numeric_limiting_choices(document,row,candidates[row.risk_id],
        facts,originals,critiques.get(row.risk_id)) for row in rows}
    contexts={row.risk_id:_index_author_context(document,candidates[row.risk_id],facts,row.claims,
        excluded_limits=excluded[row.risk_id]) for row in rows}
    try:
        response=await client.generate({'task':'repair_integrated_risk_index_attribution','subject':_subject_view(subject),
            'scoringRubric':SCORING_RUBRIC,
            'items':[{'risk':_decision_risk_view(candidates[row.risk_id],facts,row.claims),
                'approvedClaims':[claim.model_dump(mode='json',by_alias=True)
                    for claim in _score_claims(candidates[row.risk_id],row.claims)],
                'internalContext':contexts[row.risk_id],
                'rejectedIndexReason':row.scoring_rationale,
                'reviewErrors':(critiques[row.risk_id].get('errors',[]) if isinstance(critiques[row.risk_id],dict)
                    else critiques[row.risk_id]),
                'rejectedContributionReview':(critiques[row.risk_id].get('contributions',[])
                    if isinstance(critiques[row.risk_id],dict) else []),
                'rejectedNumericFactors':(critiques[row.risk_id].get('numericFactors')
                    if isinstance(critiques[row.risk_id],dict) else None),
                'unsupportedPatientStatements':[clause for clause in _negative_patient_clauses(
                    row.scoring_rationale) if _patient_state_text_errors(clause,facts)]}
                for row in rows],
            'rules':['只按outputSchema重新提出numeric-only指数，不能写scoringRationale，不能改claims、诊断、原值、分级标准或动作。旧分受到错误状态/未审背景影响则作废，不能只删理由沿用旧分；鲜数字仍须新的独立完整factor审核。',
                '真实背景用于资料存在、患者适用和相反判断。反酸、腰围、血脂、便秘等不能因同现或笼统个人相关性转计到另一个未经证实的疾病。只有已审core能正向支持该病；不设来源-only硬上限，不按病名压分。',
                '逐项针对reviewErrors和rejectedContributionReview中的具体原句重评，不原样返回。问卷displayValue复用真实问题+否/0，不把合法否认当缺少对象；否认诊断不增加否认症状，家族仅报高血压不写无糖尿病家族史，未提供信息不当临床阴性。',
                'unsupportedPatientStatements列出实际未绑定的原句，只是拒绝反馈，不是本人事实。旧分作废，只按有效本病core重新计算，不返回理由；source_signal_only也不新增正向关系，不预设所有条目低关注。',
                '删除真正未审正向背景或不适用的其他疾病阴性筛查对本病指数的影响，然后全新评定。不为填理由新增本人症状、阴性、病史或泛背景；未给相关临床记载是未知，不是相反，也不能沿用受错误贡献影响的旧分。',
                '未诊断或未主动报告症状不是同病风险阴性；真实同病信号与实际限制由独立评分审核逐项绑定。不能把来源模型原等级直接乘100，也不平均不同量纲，不伪造关系或新临床文字。',
                '本次错误numericFactors已完整返回，必须据此作全新数值决策。limitingFacts只能从allowedNumericLimitingFacts选择且须确实同病适用；没有可用项就不以否认诊断/历史/家族或缺失资料作降分依据。sourceOnlyWithoutPersonalFactors=true且没有真正适用限制时，保留可比较原等级区间并独立给指数。',
                '前次独审已明确sameDisease=false的精确引文，仅在本次同一风险、原文、core和事实未变的repair中退出限制候选；完整患者状态及拒绝记录仍保留。它们只是已考虑但不适用的背景，不得为沿用旧分改标true；仍须重新提出数字并接受新的独立审核。'],
            'outputSchema':{'decisions':[_index_decision_schema(candidates[row.risk_id],facts,row.claims,
                opposing=False,excluded_limits=excluded[row.risk_id]) for row in rows]}})
    except Exception:
        return []
    result=[]
    for row in rows:
        found=[item for item in response.get('decisions',[]) if isinstance(item,dict)
            and item.get('riskId')==row.risk_id] if isinstance(response,dict) else []
        if len(found)!=1:
            continue
        decision=found[0]
        if set(decision)-{'riskId','index','level','evidenceStrength','scoringRationale','opposingEvidenceKeys'}:
            continue
        if excluded[row.risk_id] and str(decision.get('scoringRationale') or '').strip():
            continue  # This scoped repair must produce a fresh numeric decision.
        try:
            updated=RiskAssessment.model_validate({**row.model_dump(mode='json',by_alias=True),
                **{key:decision[key] for key in ('index','level','evidenceStrength')},
                'scoringRationale':decision.get('scoringRationale','')})
            # A score repair is not a serialized re-authorization of atoms
            # or actions. Preserve only this live, previously reviewed core.
            updated.claims=[claim.model_copy(deep=True) for claim in row.claims]
            updated.lifestyle_actions=[action.model_copy(deep=True) for action in row.lifestyle_actions]
            updated.clinical_actions=[action.model_copy(deep=True) for action in row.clinical_actions]
            if updated.level==risk_level(updated.index) and not _unsupported_food_context(updated.scoring_rationale,facts):
                result.append(updated)
        except (KeyError,TypeError,ValueError,ValidationError):
            continue
    return result


async def _review_assessment_rows(client,document,evidence_summaries,preliminary,*,subject=None,known_semantics=None,
                                  allow_index_repair=True,numeric_review_feedback=None):
    registry,facts=_registry(document),_facts(document)
    originals=_originals(evidence_summaries)
    candidates={candidate.risk_id:candidate for candidate in registry.candidates}
    excluded={row.risk_id:_rejected_numeric_limiting_choices(document,row,candidates[row.risk_id],
        facts,originals,(numeric_review_feedback or {}).get(row.risk_id)) for row in preliminary}
    def core_view(row):
        value=row.model_dump(mode='json',by_alias=True)
        if known_semantics is None:
            return value
        core=_score_claims(candidates[row.risk_id],row.claims)
        value.update(claims=[claim.model_dump(mode='json',by_alias=True) for claim in core],
            supportFactIds=list(dict.fromkeys(fid for claim in core for fid in claim.fact_quotes)),
            backgroundFactIds=[],lifestyleActions=[],clinicalActions=[],
            evidenceKeys=list(dict.fromkeys(anchor.evidence_key for claim in core for anchor in claim.evidence)))
        return value
    core_rows={row.risk_id:core_view(row) for row in preliminary}
    review_keys={key for row in preliminary for key in [*core_rows[row.risk_id]['evidenceKeys'],*row.opposing_evidence_keys]}
    review_payload = {
        'task': 'review_integrated_health_risks', 'subject': subject or {},
        'scoringRubric': SCORING_RUBRIC,
        'items': [{'assessment':core_rows[row.risk_id],
            'risk': _decision_risk_view(candidates[row.risk_id],facts,row.claims),
            'internalContext':_decision_internal_context(candidates[row.risk_id],facts,row.claims,
                numeric=not bool(row.scoring_rationale.strip()),excluded_limits=excluded[row.risk_id]),
            'previousRejectedFactorReview':(numeric_review_feedback or {}).get(row.risk_id),
            'indexReasonSentences':_index_reason_sentences(row),
            'numericDecisionOnly':not bool(row.scoring_rationale.strip()),
            'numericCoreClaimIds':[claim.claim_id for claim in _score_claims(candidates[row.risk_id],row.claims)],
            'sourceGradeComparisonTargets':_source_grade_targets(candidates[row.risk_id],facts),
            'sourceDeclaredRiskFactIds':_declared_risk_signal_ids(candidates[row.risk_id],facts),
            'displayNameOptions':_cardio_display_options(row,candidates[row.risk_id]),
            'evidenceKeys':core_rows[row.risk_id]['evidenceKeys'], 'opposingEvidenceKeys':row.opposing_evidence_keys} for row in preliminary],
        'evidence': [_model_original(originals[key]) for key in review_keys],
        'rules': [
            '独立逐句审核，不接受作者自称通过。综合指数不必在原报告中出现，但必须符合rubric与实际个人状态；风险高低和证据强弱分开。',
            '核对每个claim关系的原文支持、方向、研究人群/部位/菌种层级和患者真实前提。原文不必包含患者具体数值，患者数值必须由factQuotes逐字证明。',
            '科学关系的sourcePrerequisites已在独立原子审核中按本篇原文抽取并逐项绑定：单次测量、纵向轨迹、完整指数、遗传暴露、诊断识别、干预反应是不同的暴露/结局形态。评分终审不得把缺少的时间/组成/分组条件从候选标题或其他资料名字补出来，也不得把反向科学材料放进正向核心。',
            'source_fact只可描述实际事实；新增关系必须有证据。不得因为候选存在就默认为真，完整考虑相反检查、否认和背景。',
            '评分支持限risk.supportFactIds及已审claim；internalContext中未审事实不因名称、偏离、同现就成为本病病因或抬分理由。来源已有本病风险信号与实际当前异常可以综合重评；诊断定义/代谢综合征组成不能独立证明新的未来疾病发生风险。若评分理由借被删关系或未审行为/菌群/功能关联抬分，scoreJustified=false。',
            '逐项阅读supportEligible=false的background/opposing及providedPatientState完整原值，用于是否提供、真实当前状态/人群适用及反向判断；这些均没有新正向支持权限。当前已有血脂等异常不能被说成没有代谢异常。不能把已输入资料写成未提供，不能把未审背景放进加分/叠加理由。若违反，scoreJustified=false并在unsupportedClaims列scoringRationale。',
            'QA否定约束适用于所有风险，不仅source_signal_only：按原问题主题与答案核每个谓词。无已知诊断不等于无症状；一种亲属疾病不能证明另一种家族史阴性。任何错误本人状态均判scoreJustified=false，unsupportedClaims列scoringRationale，并在reasonContributions或对应claims指出原句；现有repair会据该原句重评，不许以科学关系存在为由忽略事实错误。',
            'supportBasis=source_signal_only时独立核对信号特异性及个人相关性；来源模型的原中高等级不能自动主导综合关注度，筛查关联不能当已有疾病。真实当前影响、症状持续性、来源状态及适用关系须共同考虑；不按疾病名字或弱证据机械压分，也不设硬上限。',
            'source_signal_only必须另返回indexBasis：逐项核对scoringRationale的全部加分贡献，positiveSupportFactIds只能来自已审同病正向信号；任何未审background被用于提高关注度都写入backgroundPositiveFactIds并将scoreJustified=false。资料真实存在或症状同现不构成疾病特异性正向支持。背景用于适用或降低关注不填该数组。只有完整核对才能allReasonContributionsChecked=true。',
            '不能只复述作者的backgroundPositive空数组。对indexReasonSentences逐句做reasonContributions：原sentenceId/text逐字返回，分别列真正正向、限制、仅存在的factIds，并给正向支持的已审claimIds。若某个背景用于“故高于单纯信号/构成现实问题/个人相关性明确/以症状负担为主”等提高本病关注，即属positive，不得改标presenceOnly来通过。每句全部贡献核对后allContributionsCaptured=true；存在未审正向关系则unsupportedPositiveRelation=true，scoreJustified=false。',
            'source_signal_only的indexBasis.stateConsistency逐项核对providedPatientState，返回其原patientStateDigest并检查falseAbsenceAssertions；已存在异常不因没被本风险采用就变成不存在。每个理由句按逗号分成原文子句，patientAssertions.text必须逐字等于涉及本人状态的一个完整子句，逐条绑定factQuotes和use=positive或limiting。每个状态子句的对象须确为所绑事实的对象；不能用空腹血糖正常证明“无代谢异常”，不能把多个子句整段绑定到一个正常值掩盖错误全称否定。不允许空数组或raisesAttention=false隐藏本人症状/指标的实际贡献；未审正向状态须拒绝并重新计算。没有涉及本人状态的证据局限句可patientAssertions=[]。',
            '全部numericDecisionOnly项不要求自由scoringRationale，indexReasonSentences和reasonContributions均为空；独立numericFactors与sourceGradeComparisons核对实际正向、限制及原级。历史或作者意外附带的非空理由仍按逐句事实/贡献门完整审核，不剪坏理由保留受影响指数。',
            '作者opposingEvidenceKeys若误放确切已给factId，已仅修回事实域并保留其原角色/原值；独立判断其作为支持、相反还是背景，不得因字段错位把已审支持变成反向事实。未知ID仍不接受。',
            'sourceDeclaredRiskFactIds限严格同病的来源风险模型/阳性关联。其风险信号可由真实来源忠实保留并结合个人资料独立重评，不当临床诊断/本人症状；输入从无该病风险信号的新推疾病仍需适用科学关系，诊断定义不替代未来风险证据。',
            '逐claim返回原claimId、valid、factQuotes、evidenceQuotes及missingConditions；factQuotes和evidenceQuotes需逐字核实后给出完整映射，不以valid=true代替绑定。',
            '每项返回原index/level、scoreJustified、consideredOpposingFactIds、missingConditions、unsupportedClaims。任何未支持断言或患者条件不足则valid=false。',
            '核心风险与各管理动作分别判定。coreValid/coreMissingConditions只评核心与指数；坏动作只在该management claim判false并于unsupportedClaims列该claimId，不抹掉有据核心。',
            'risk.roleConflictFactIds存在时逐项独立裁定该真实事实最终support/opposing/background用途，输出consideredConflictedFactIds；不能支持和相反同时出现。microbiome_association_screen绝不当本人症状或诊断，measured_function不当血中浓度。',
            '可执行生活方式在适用证据支持时允许频次时长目标；不批准药物剂量、补剂处方、菌群移植执行方案。',
            '对宽泛标题仅依据已审同病outcome选清楚具体displayName，附displayNameClaimIds；如唯一已审结局是TRL/残粒与ASCVD，则写动脉粥样硬化性心血管疾病相关风险，不能与当前血脂异常合并。来源风险本已具体则保持；不得添加新病/亚型/确诊。riskId与源关系不变。',
            '亚型分型不能借宽泛同义词或单个异常完成：原资料未明确亚型时，须有diagnostic_principle个人分类断言、'
            '真实指南规则及完整本人条件并经本次独审；泛定义不是个人已经符合标准。混合型高脂血症须核对TC与TG两项，'
            '不能用单独LDL升高代替TC，不满足则保留上位临床问题或真正已审的独立潜在疾病风险，不挪用指数。',
            '逐项独立返回entityKind及其entityKindClaimIds，entityKindJustified：disease是实际疾病，disease_risk是该具体疾病潜在风险，symptom是原始主诉，indicator_problem是检验/体征状态。病例主标题只能用前两类，且specificDiseaseEndpoint=true、symptomOrIndicatorNotUpgraded=true须核对实际原子与患者前提；不能因catalog同ID、源筛查或症状别名直接升级为当前疾病。血脂异常这一状态应作具体病/风险的支持；高脂血症及亚型须真实诊断或实际适用指南条件，不强换ASCVD。没有具体疾病结局保留症状/指标种类并指出缺口，不制造确诊。',
            '临床疑似或风险评估与未来发病关联分开：真实患者症状模式与适用指南支持的个人诊断评估断言，可为diagnostic_principle并保留考虑/疑似/潜在风险措辞；核实实际诊断条件和缺失条件，不强求发病结局研究，也不将一般定义或症状别名当本人分类。只有实际满足诊断条件并经完整独审才可陈述符合标准，不能靠删除疑似限定升级确诊。',
            'certainty=self_reported且实际仅报告症状时，displayName使用原始自述中明确的具体症状短语；不能因分类到某疾病就把反酸等症状标题换成该病确诊名称。有真实自述疾病诊断时保留自述性质，不改成医院证实。',
        ],
        'outputSchema': {'items': [{'riskId': '原ID', 'valid': True, 'coreValid':True,
            'coreMissingConditions':[], 'index': '原index',
            'level': '原level', 'scoreJustified': True, 'consideredOpposingFactIds': [],
            'consideredConflictedFactIds':[],
            'indexBasis':{'positiveSupportFactIds':['本病已审正向ID'],
                'backgroundPositiveFactIds':[], 'allReasonContributionsChecked':True,
                'stateConsistency':{'patientStateDigest':'输入internalContext原摘要',
                    'falseAbsenceAssertions':[]},
                'numericFactors':{'approvedClaimIds':['全部numericCoreClaimIds'],
                    'limitingFacts':[{'factId':'真实同病正常结果或明确症状否认ID',
                        'factQuote':'该value逐字片段','kind':'actual_normal_result|explicit_symptom_denial',
                        'targetDiseaseId':'输入risk.diseaseId','sameDisease':True}],
                    'sourceGradeComparisons':[{'factId':'每个sourceGradeComparisonTargets原ID',
                        'sourceValue':'原value字符串','sourceLevel':'原status或null','index':'原index','level':'原level',
                        'comparison':'same|higher|lower|not_comparable',
                        'basis':'reviewed_personal_factors|source_signal_specificity',
                        'positiveClaimIds':['全部numericCoreClaimIds'],'limitingFactIds':[],
                        'missingNotUsedAsNegative':True,'gradeJustified':True}],
                    'allFactorsChecked':True,'unboundFactors':[]},
                'reasonContributions':[{'sentenceId':'输入原ID','text':'逐字理由句',
                    'positiveFactIds':[],'limitingFactIds':[],'presenceOnlyFactIds':[],
                    'supportingClaimIds':[],'raisesAttention':False,'unsupportedPositiveRelation':False,
                    'allContributionsCaptured':True,'patientAssertions':[{'text':'本理由句的逐字本人状态子句',
                        'factQuotes':{},'use':'positive|limiting'}]}]},
            'consideredOpposingEvidenceKeys': [],
            'displayName':'基于已审结局的具体名称','displayNameClaimIds':['支撑名称的原claimId'],
            'entityKind':'disease|disease_risk|symptom|indicator_problem','entityKindClaimIds':['支撑对象性质的原claimId'],
            'entityKindJustified':True,'specificDiseaseEndpoint':True,'symptomOrIndicatorNotUpgraded':True,
            'missingConditions': [], 'unsupportedClaims': [],
            'claims': [{'claimId': '原ID', 'valid': True, 'factQuotes': {},
                'evidenceQuotes': {}, 'missingConditions': []}]}]},
    }
    if all(not row.scoring_rationale.strip() for row in preliminary):
        review_payload['outputSchema']['items'][0]['indexBasis']['reasonContributions']=[]
    review_payload['rules'].extend([
        'numericDecisionOnly=true的作者只给数字，不需要也不补自由临床理由；reasonContributions=[]。独立核实全部真实本人状态、已审core、适用相反结果和原模型，不能仅以scoreJustified=true通过，必须完整返回indexBasis.numericFactors。',
        'numericFactors.approvedClaimIds恰为本项全部numericCoreClaimIds；它们逐claim已有原始factQuotes/evidenceQuotes审核。positiveSupportFactIds恰为这些core的患者fact ID合集，backgroundPositiveFactIds=[]。不能凭背景同现抬分，也不能把未确诊、未询问、未主动报告症状或缺少家族史作风险阴性。',
        'numericFactors.limitingFacts只收当前风险真正适用的真实正常检查结果或问卷明确否认的同一症状；每项复制原factId/value引文，标实际kind/targetDiseaseId/sameDisease。没有真实限制就[]，不要为降分编造“无同病症状/无家族史”。诊断/病史未提供及未确诊不放限制；所有providedPatientState仍供完整一致性核查。',
        'sourceGradeComparisons逐一回传每个sourceGradeComparisonTargets的实际value/status和所审index/level；比较只描述等级higher/lower/same或无原级not_comparable，不换算原数值。positiveClaimIds恰为本项全部core，limitingFactIds恰为实际限制项ID。无同病相反结果时，原级作为参考基准，由同病信号特异性与实际支持独立判断，不能仅因没有确诊或症状记载降低潜在风险，不设硬下限、不乘100、不自动照搬。gradeJustified必须体现这次独立完整判断。',
        '不撰写新的scoringRationale、患者状态故事或通用理由。若输入意外已有scoringRationale，仍按原逐句事实门审核；坏理由使原分失效，不能剪掉文字直接保留旧分。所有数字结论另须独立core/原文/事实/相反核验；本协议仅分开数值与正文，不减少任何适用性审核。'])
    review_payload['rules'].extend([
        'limitingFacts只能逐字从internalContext.allowedNumericLimitingFacts选factId/kind/factQuotes，库存不等于同病适用批准，仍独立判断sameDisease；库存为空则limitingFacts=[]。完整providedPatientState继续核真实状态；numericLimitingEligible=false的诊断史/过敏史等不得改写成症状阴性。',
        'previousRejectedFactorReview记录前一轮确切错误及被拒numericFactors，不是新的患者事实。本轮必须修正具体错误，不能原样再次采用“无已知诊断”或历史否认来证明无症状；consideredOpposing可考虑后不采用，不必全部放限制项。',
        '本次同facts/core/原文repair中，前次明确sameDisease=false的精确引文已退出allowedNumericLimitingFacts；这些原始状态仍完整可见，只作已考虑但未采用的背景。不能再次填进limitingFacts或改标true，其他真实同病限制仍逐条独审。',
        '若只有同病原模型或筛查信号，须独立判断其对个人健康问题的特异性与关注意义，不能强制锁在原等级区间。'
        'basis=source_signal_specificity可支持较原级降低的综合关注判断，但必须核对真实同病信号及所给个人资料，不把未询问、未确诊或无检查编造为阴性；'
        '仅有同一信号且没有新的同病支持或适用证据时不能据此抬高原等级。原始数值与原等级始终作为独立来源记录保留。'])
    try:
        response = await client.generate(review_payload)
        reviews = response.get('items', []) if isinstance(response, dict) else []
    except Exception:
        registry.incomplete.append('INTEGRATED_RISK_REVIEW_UNAVAILABLE')
        return registry
    by_id = {}
    for review in reviews:
        if isinstance(review, dict):
            by_id.setdefault(review.get('riskId'), []).append(review)
    accepted = {row.risk_id: row for row in approved_assessments(document)}
    hashes = {key: _digest(row['sourceText']) for key, row in originals.items()}
    semantics=(known_semantics if known_semantics is not None else
        await _review_claim_semantics(client,preliminary,candidates,facts,originals,subject=subject))
    index_repairs=[];index_critiques={}
    for row in preliminary:
        matches = by_id.get(row.risk_id, [])
        review=matches[0] if len(matches)==1 else None
        errors = _review_errors(review, row, facts, originals,candidates[row.risk_id],
            excluded_numeric_limits=excluded[row.risk_id], subject=subject or registry.subject_snapshot)
        if not all(semantics.get(row.risk_id+':'+claim.claim_id) is True
                for claim in row.claims if claim.role!='management'):
            errors.append('independent_original_semantics_failed')
        if errors:
            registry.gaps.append({'riskId': row.risk_id, 'reason': ','.join(errors), 'missingRole': 'risk_assessment'})
            basis_errors=[*_source_only_index_basis_errors(review or {},row,candidates[row.risk_id],facts),
                *_numeric_index_basis_errors(review or {},row,candidates[row.risk_id],facts,
                    excluded_limits=excluded[row.risk_id]),
                *_score_context_errors(row,candidates[row.risk_id],facts),
                *_patient_state_text_errors(row.scoring_rationale,facts)]
            allowed_errors=set(basis_errors)|{'independent_risk_review_failed'}
            if (review or {}).get('unsupportedClaims')==['scoringRationale']:
                allowed_errors.add('unsupported_core_assertions')
            if allow_index_repair and basis_errors and set(errors)<=allowed_errors:
                index_repairs.append(row);index_critiques[row.risk_id]={'errors':errors,
                    'contributions':((review or {}).get('indexBasis') or {}).get('reasonContributions',[])
                        if isinstance((review or {}).get('indexBasis'),dict) else [],
                    'numericFactors':copy.deepcopy((review['indexBasis']).get('numericFactors'))
                        if isinstance((review or {}).get('indexBasis'),dict) else None,
                    'stateConsistency':copy.deepcopy((review['indexBasis']).get('stateConsistency'))
                        if isinstance((review or {}).get('indexBasis'),dict) else None,
                    'limitingReviewScope':_numeric_limiting_repair_scope(
                        document,row,candidates[row.risk_id],originals)}
            continue
        reviewed_opposing=list(dict.fromkeys(review.get('consideredOpposingFactIds',[])))
        row.opposing_fact_ids=reviewed_opposing
        row.background_fact_ids=[fid for fid in row.background_fact_ids if fid not in reviewed_opposing]
        candidate=candidates[row.risk_id]
        candidate.opposing_fact_ids=reviewed_opposing
        candidate.support_fact_ids=[fid for fid in candidate.support_fact_ids if fid not in reviewed_opposing]
        if not candidate.support_fact_ids:
            candidate.support_fact_ids=list(row.support_fact_ids)
        candidate.background_fact_ids=[fid for fid in candidate.background_fact_ids if fid not in reviewed_opposing]
        name=_reviewed_display_name(row,review,candidates[row.risk_id])
        name=_self_report_state_name(row,candidates[row.risk_id],facts,proposed=name,document=document)
        if name:
            identity=_risk_identity(name)
            update={'name':name}
            if row.certainty=='self_reported' and not identity.startswith('disease-'):
                if not _constipation_symptom_witnesses(row,facts):
                    update['disease_id']=identity
                    candidates[row.risk_id].disease_id=identity
            row=row.model_copy(update=update)
            candidates[row.risk_id].name=name
        claim_reviews={item.get('claimId'):item for item in review.get('claims',[]) if isinstance(item,dict)}
        rejected=set()
        for claim in row.claims:
            if claim.role!='management':
                continue
            if known_semantics is not None:
                if (semantics.get(row.risk_id+':'+claim.claim_id) is not True
                        or _draft_claim_errors(claim,candidates[row.risk_id],facts,originals)):
                    rejected.add(claim.claim_id)
                continue
            verdict=claim_reviews.get(claim.claim_id,{})
            if (semantics.get(row.risk_id+':'+claim.claim_id) is not True
                    or verdict.get('valid') is not True or verdict.get('missingConditions')!=[]
                    or verdict.get('factQuotes')!=claim.fact_quotes
                    or verdict.get('evidenceQuotes')!={a.evidence_key:a.quote for a in claim.evidence}):
                rejected.add(claim.claim_id)
        if rejected:
            row=row.model_copy(deep=True)
            row.claims=[claim for claim in row.claims if claim.claim_id not in rejected]
            for field in ('lifestyle_actions','clinical_actions'):
                setattr(row,field,[action for action in getattr(row,field) if not rejected.intersection(action.claim_ids)])
            row.evidence_keys=list(dict.fromkeys(anchor.evidence_key for claim in row.claims for anchor in claim.evidence))
            registry.gaps.append({'riskId':row.risk_id,'missingRole':'management',
                'reason':'management_original_semantics_or_applicability_failed'})
        if not _bind_reviewed_entity_kind(row, review, facts, originals):
            candidates[row.risk_id]._current_classification_scope = None
            registry.gaps.append({'riskId': row.risk_id, 'missingRole': 'disease_endpoint',
                'reason': 'independent_specific_disease_endpoint_kind_missing_or_invalid'})
            registry.incomplete.append('DISEASE_ENDPOINT_INCOMPLETE')
        if row._entity_kind_receipt is not None:
            candidates[row.risk_id].entity_kind = row.entity_kind
            if row.entity_kind == 'disease':
                candidates[row.risk_id]._current_classification_scope = _digest(candidates[row.risk_id].model_dump())
        accepted[row.risk_id] = row
        if not row.scoring_rationale.strip():
            audit_json=json.dumps(review['indexBasis'],ensure_ascii=False,sort_keys=True,separators=(',',':'))
            row._numeric_receipt=_NumericRiskDecisionReceipt(
                _numeric_receipt_fingerprint(row,document,audit_json),audit_json)
        registry._evidence_hashes.update({key: hashes[key]
            for key in [*row.evidence_keys, *row.opposing_evidence_keys]})
        _record_risk_original_scope(registry, originals,
            [*row.evidence_keys, *row.opposing_evidence_keys])
        registry._proofs[row.risk_id] = _proof(row, document, registry._evidence_hashes)
    registry.assessments = list(accepted.values())
    if index_repairs:
        repaired=await _repair_index_attributions(client,document,index_repairs,index_critiques,
            subject=subject,originals=originals)
        if repaired:
            await _review_assessment_rows(client,document,evidence_summaries,repaired,subject=subject,
                known_semantics=semantics,allow_index_repair=False,numeric_review_feedback=index_critiques)
            approved={row.risk_id for row in approved_assessments(document)}
            registry.gaps=[gap for gap in registry.gaps if not (gap.get('riskId') in approved
                and gap.get('missingRole')=='risk_assessment')]
    if len(approved_assessments(document)) < len(registry.candidates):
        registry.incomplete.append('INTEGRATED_RISK_ASSESSMENT_INCOMPLETE')
    registry.incomplete = list(dict.fromkeys(registry.incomplete))
    return registry


def _editorial_links_scope(document,links):
    selected={row.risk_id:row for row in approved_assessments(document)}
    ids={value for row in links for value in (row['signalRiskId'],row['primaryRiskId'])}
    if not ids<=selected.keys():
        return None
    return _digest({'facts':_source_digest(document),'links':links,'core':{identifier:{
        'diseaseId':selected[identifier].disease_id,'certainty':selected[identifier].certainty,
        'name':selected[identifier].name,'category':selected[identifier].category,
        'entityKind':getattr(selected[identifier], 'entity_kind', None),'entityScope':_candidate_entity_scope(selected[identifier]),
        'index':selected[identifier].index,'level':selected[identifier].level,
        'clinicalActions':[action.model_dump(mode='json',by_alias=True)
            for action in selected[identifier].clinical_actions],
        'claims':[claim.model_dump(mode='json',by_alias=True) for claim in selected[identifier].claims
            if claim.role!='management']} for identifier in ids}})


def editorial_risk_representatives(document,rows,*,current_only=False):
    """Only a private, independently bound family receipt affects emphasis."""
    receipt=_registry(document)._editorial_link_receipt
    if not isinstance(receipt,dict):
        return list(rows)
    links=receipt.get('links')
    if (not isinstance(links,list) or receipt.get('proof') is None
            or receipt['proof']!=_editorial_links_scope(document,links)):
        return list(rows)
    present={row.risk_id for row in rows}
    by_id={row.risk_id:row for row in rows}
    hidden={link['signalRiskId'] for link in links if link['primaryRiskId'] in present
        and link['signalRiskId'] in by_id
        and _candidate_entity_scope(by_id[link['signalRiskId']]) == _candidate_entity_scope(by_id[link['primaryRiskId']])
        and (not current_only or link.get('topicMode')=='same_current_symptom')}
    return [row for row in rows if row.risk_id not in hidden]


def display_risk_assessments(document, rows):
    """Omit only a reviewed, wholly derived duplicate clinical composition.

    Source-declared conditions and their original measurements remain one
    for one. The full assessed registry is retained internally. A shared
    marker, unknown name or common organ is never itself a deduplication
    decision: the existing independent family review must have concluded
    that the composition adds no distinct patient outcome.
    """
    registry = _registry(document)
    receipt = registry._editorial_link_receipt
    if (not isinstance(receipt, dict) or not isinstance(receipt.get('links'), list)
            or receipt.get('proof') is None
            or receipt['proof'] != _editorial_links_scope(document, receipt['links'])):
        return list(rows)
    facts = _facts(document)
    present = {row.risk_id: row for row in rows}
    redundant = set()
    for link in receipt['links']:
        signal = present.get(link['signalRiskId'])
        primary = present.get(link['primaryRiskId'])
        if (signal is None or primary is None or _candidate_entity_scope(signal) != _candidate_entity_scope(primary)
                or link.get('topicMode') != 'scientific_personal_phenotype'
                or signal.certainty != 'potential_risk' or not _risk_identity(signal.name).startswith('disease-')
                or signal.source_model_fact_ids or not signal.support_fact_ids
                or _risk_identity(primary.name).startswith('disease-')
                or not all(fid in facts and facts[fid].kind in {'clinical', 'self_reported'}
                    and _fact_role(facts[fid]) not in {'source_disease_model', 'microbiome_association_screen'}
                    for fid in signal.support_fact_ids)):
            continue
        redundant.add(signal.risk_id)
    return [row for row in rows if row.risk_id not in redundant]


def _current_symptom_editorial_links(rows,facts):
    links=[]
    for condition in rows:
        if condition.certainty!='current_problem':
            continue
        symptoms=[row for row in rows if _same_current_constipation_scope(condition,row,facts)]
        if not symptoms:
            continue
        primary=min([condition,*symptoms],key=lambda row:(not bool(row.clinical_actions),
            row.certainty!='self_reported',risk_priority_key(row)))
        for row in [condition,*symptoms]:
            if row is primary:
                continue
            links.append({'signalRiskId':row.risk_id,'primaryRiskId':primary.risk_id,
                'signalFactIds':list(row.support_fact_ids),'primaryFactIds':list(primary.support_fact_ids),
                'topicMode':'same_current_symptom'})
    # Multiple current aliases may share the same directly reviewed scope.
    # Choose one representative by its own real plan; never copy a plan.
    return list({row['signalRiskId']:row for row in links}.values())


async def _link_supporting_signal_topics(client,document):
    """Group overlapping emphasis without merging different disease records."""
    registry=_registry(document);facts=_facts(document);rows=approved_assessments(document)
    direct=_current_symptom_editorial_links(rows,facts)
    receipt=registry._editorial_link_receipt
    previous=(receipt['links'] if isinstance(receipt,dict) and isinstance(receipt.get('links'),list)
        and receipt.get('proof') is not None and receipt['proof']==_editorial_links_scope(document,receipt['links'])
        else [])
    direct_ids={row['signalRiskId'] for row in direct}
    retained=[*direct,*[row for row in previous if row['signalRiskId'] not in direct_ids]]
    if retained:
        registry._editorial_link_receipt={'links':retained,'proof':_editorial_links_scope(document,retained)}
    candidates={row.risk_id:row for row in registry.candidates}
    current=[row for row in rows if row.certainty in {'confirmed','current_problem','self_reported'}]
    signals=[row for row in rows if row.certainty=='potential_risk' and row.index>=25
        and _score_claims(candidates[row.risk_id],row.claims)
        and all(claim.role=='source_fact' for claim in _score_claims(candidates[row.risk_id],row.claims))]
    scientific=[row for row in rows if row.certainty=='potential_risk' and row.index>=25
        and any(claim.role in {'outcome','etiology','mechanism'} for claim in row.claims)]
    if client is None:
        return
    pairs=[{'signalRiskId':signal.risk_id,'primaryRiskId':primary.risk_id}
        for signal in signals for primary in current if signal.category==primary.category
        and _candidate_entity_scope(signal)==_candidate_entity_scope(primary)]
    # A composition often has more markers than the concrete disease whose
    # information it repeats. Prefer an established disease endpoint as the
    # representative; source count is not diagnostic specificity.
    ordered=sorted(scientific,key=lambda row:(_risk_identity(row.name).startswith('disease-'),
        -len(row.support_fact_ids),row.risk_id))
    scientific_pairs=set()
    for i,primary in enumerate(ordered):
        for signal in ordered[i+1:]:
            if signal.category==primary.category and set(signal.support_fact_ids)&set(primary.support_fact_ids):
                pairs.append({'signalRiskId':signal.risk_id,'primaryRiskId':primary.risk_id,
                    'topicMode':'scientific_personal_phenotype'})
                scientific_pairs.add((signal.risk_id,primary.risk_id))
    if not pairs:
        return
    selected={row.risk_id:row for row in [*current,*signals,*scientific]}
    views=[{'riskId':row.risk_id,'name':row.name,'certainty':row.certainty,
        'claims':[claim.model_dump(mode='json',by_alias=True) for claim in row.claims if claim.role!='management'],
        'sourceFacts':[_fact_view(facts[fid]) for fid in row.support_fact_ids if fid in facts]}
        for row in selected.values()]
    signature=_digest({'facts':_source_digest(document),'pairs':pairs,'items':views})
    if signature in registry._editorial_link_attempts:
        return
    registry._editorial_link_attempts.add(signature)
    try:
        response=await client.generate({'task':'identify_supporting_signal_families','pairs':pairs,'items':views,
            'rules':['只判断编辑层的同一健康问题家族，不改疾病定义、原风险行、分数、原等级或确定性。',
                '来源-only信号若只是当前已存在问题的同一/上位表型方向，没有不同独立疾病结局，可作为该问题的相关来源线索共同呈现，避免占多个首页重点。',
                '同科室、同器官、相同指标或共同诱因不等于同一问题家族；不同独立疾病结局必须保留。一般状态与部位/亚型定义仍分别保留，不能说两者临床定义相同。',
                'topicMode=scientific_personal_phenotype允许核对两个科学已审的不同定义是否在本人仅代表同一风险表型/管理重点且没有新增独立临床信息；不合并定义、诊断、指数或原始风险行。同用腰围/血脂但分别指向不同器官疾病，不是同一重点。',
                '若组合名只是已有异常/暴露加上笼统代谢风险，且已审科学句所指的具体疾病结局已由primary保留，组合不能再当一个额外疾病。逐条比较全部claims；有任何独立疾病结局或独特个人风险信息则不归并。不要因组合引用了更多同一血脂指标就把它放在具体ASCVD等疾病之前。',
                '只选已给pair，每个signal最多归一个当前primary；不新增医学关系。'],
            'outputSchema':{'links':[{'signalRiskId':'给定ID','primaryRiskId':'给定ID',
                'reason':'同一/上位状态家族而非另一独立疾病的具体语义依据'}]}})
    except Exception:
        return
    proposed=[];seen=set();allowed={(row['signalRiskId'],row['primaryRiskId']) for row in pairs}
    for row in response.get('links',[]) if isinstance(response,dict) else []:
        if not isinstance(row,dict):
            continue
        pair=(row.get('signalRiskId'),row.get('primaryRiskId'))
        if pair not in allowed or pair[0] in seen:
            continue
        seen.add(pair[0]);signal,primary=(selected[value] for value in pair)
        proposed.append({**row,'signalFactIds':signal.support_fact_ids,'primaryFactIds':primary.support_fact_ids,
            **({'topicMode':'scientific_personal_phenotype'} if pair in scientific_pairs else {})})
    if not proposed:
        return
    try:
        response=await client.generate({'task':'review_supporting_signal_families','links':proposed,'items':views,
            'rules':['独立判断是否仅为同一健康问题家族的来源线索；这不是病因审核或临床同病判定，不授权新的positive风险支持。',
                '必须保留各自不同临床定义/原始记录；只因共用指标、器官、诱因不能合并重点。若代表不同独立疾病结局则distinctDiseaseOutcome=true并拒绝。',
                '科学风险两定义若仅构成本人同一表型重点，另返回samePersonalRiskPhenotype=true、addsIndependentPatientOutcome=false，并逐字给共同supportFactIds；此仅改变重点呈现，不宣布两疾病定义相同。只因同指标而不同结局必须false。',
                '逐字绑定signalRiskId/primaryRiskId及真实signalFactIds/primaryFactIds，不以总valid替代；仅broaderSameConditionFamily、differentDefinitionsPreserved均true且distinctDiseaseOutcome=false可通过。'],
            'outputSchema':{'links':[{'signalRiskId':'原ID','primaryRiskId':'原ID','signalFactIds':[],
                'primaryFactIds':[],'broaderSameConditionFamily':True,'differentDefinitionsPreserved':True,
                'distinctDiseaseOutcome':False,'samePersonalRiskPhenotype':False,
                'addsIndependentPatientOutcome':True,'commonSupportFactIds':[]}]}})
    except Exception:
        return
    links=[]
    for proposed_row in proposed:
        found=[row for row in response.get('links',[]) if isinstance(row,dict)
            and row.get('signalRiskId')==proposed_row['signalRiskId']
            and row.get('primaryRiskId')==proposed_row['primaryRiskId']] if isinstance(response,dict) else []
        row=found[0] if len(found)==1 else {}
        extra=True
        pair=(proposed_row['signalRiskId'],proposed_row['primaryRiskId'])
        if pair in scientific_pairs:
            overlap=set(proposed_row['signalFactIds'])&set(proposed_row['primaryFactIds'])
            extra=(row.get('samePersonalRiskPhenotype') is True
                and row.get('addsIndependentPatientOutcome') is False
                and set(row.get('commonSupportFactIds') or [])==overlap and bool(overlap))
        if (extra and row.get('signalFactIds')==proposed_row['signalFactIds']
                and row.get('primaryFactIds')==proposed_row['primaryFactIds']
                and row.get('broaderSameConditionFamily') is True
                and row.get('differentDefinitionsPreserved') is True and row.get('distinctDiseaseOutcome') is False):
            links.append({key:proposed_row[key] for key in
                ('signalRiskId','primaryRiskId','signalFactIds','primaryFactIds','topicMode') if key in proposed_row})
    if links:
        linked_ids={row['signalRiskId'] for row in links}
        links=[*direct,*[row for row in links if row['signalRiskId'] not in direct_ids],
            *[row for row in retained if row['signalRiskId'] not in direct_ids|linked_ids]]
        registry._editorial_link_receipt={'links':links,'proof':_editorial_links_scope(document,links)}


def _summary_scope(document):
    registry=_registry(document)
    return _digest({'risks':{row.risk_id:registry._proofs.get(row.risk_id)
        for row in approved_assessments(document)},'summaries':{
        key:value.model_dump(mode='json',by_alias=True) for key,value in registry.approved_summaries.items()}})


def approved_risk_summaries(document):
    registry=_registry(document)
    return registry.approved_summaries if registry._summary_proof==_summary_scope(document) else {}


def risk_summary_reference_numbers(document, summary_or_slot):
    slot=summary_or_slot.slot if isinstance(summary_or_slot,RiskSummary) else str(summary_or_slot)
    return sorted({number for block in document.sections if block.code=='riskSummary:'+slot
        for number in block.evidence_refs})


def _summary_sentences(row):
    return [{'sentenceId':row.slot+':s'+str(offset),'text':text.strip()}
        for offset,text in enumerate(re.findall(r'[^。！？!?]+[。！？!?]?',row.text)) if text.strip()]


def _provided_patient_fields(facts):
    """Existence is not disease support; this view intentionally has no value."""
    result={}
    for fact in facts.values():
        if (fact.kind not in {'clinical','self_reported'} or not str(fact.value).strip()
                or str(fact.value).strip() in {'未提供','未填写','不详','未知','null','None'}):
            continue
        result.setdefault((fact.name,fact.kind),{'name':fact.name,'kind':fact.kind,'provided':True})
    return list(result.values())


def _summary_missing_fact_errors(text,bound,facts):
    from .evaluation_evidence_plan import risk_retrieval_concepts,_mentions
    clauses=re.findall(r'(?:未提供|没有提供|未上传|缺少|缺乏)([^，。；！？]{1,90})',text)
    if not clauses:
        return []
    fields=_provided_patient_fields(facts);errors=[]
    for clause in clauses:
        matched=False
        for field in fields:
            name=field['name']
            aliases=[name,*risk_retrieval_concepts([name])]
            aliases.extend(re.sub(r'^(?:空腹|餐后|随机|血清|血浆|全血|外周血)','',alias)
                for alias in list(aliases) if isinstance(alias,str))
            aliases=[alias for alias in aliases if len(_key(alias))>=2]
            if _mentions(clause,tuple(aliases)):
                matched=True;break
        if matched:
            errors.append('provided_patient_fact_reported_missing')
        elif not any(claim.role=='source_fact' and any(marker+clause in claim.text
                for marker in ('未提供','没有提供','未上传','缺少','缺乏')) for claim in bound):
            # No summary may infer an absence merely because its positive
            # support set does not contain that test/history.
            errors.append('unbound_missing_patient_fact')
    return list(dict.fromkeys(errors))


def _summary_scope_errors(row,selected,claims,facts,*,health_scores=None):
    """A slot cannot borrow a disease or positive assay from another slot."""
    from .evaluation_catalog import FIXED_DISEASE_ALIASES
    from .evaluation_content import integrated_risk_grade_failures
    from .evaluation_evidence_plan import _mentions
    if _BOUNDARY.search(row.text):
        return ['public_authoring_metadata_or_rule']
    if not set(row.claim_ids)<=claims.keys() or not set(row.risk_ids)<=selected.keys():
        return ['unknown_slot_binding']
    owners={claims[key][0].risk_id for key in row.claim_ids}
    if owners!=set(row.risk_ids):
        return ['risk_without_slot_claim']
    bound=[claims[key][1] for key in row.claim_ids]
    presence_errors=[*_summary_missing_fact_errors(row.text,bound,facts),
        *_patient_state_text_errors(row.text,facts)]
    if presence_errors:
        return presence_errors
    if row.slot in {'microbiome', 'nutrition', 'pathways'} and not any(
            fid in facts and _fact_role(facts[fid]) in {'measured_microbiome', 'measured_function'}
            for claim in bound for fid in claim.fact_quotes):
        return ['functional_slot_without_measured_function_or_microbiome_basis']
    allowed=' '.join([*(claim.text for claim in bound),*(selected[key].name for key in row.risk_ids)])
    errors=_summary_relation_errors(row,bound,selected,facts)
    errors.extend(integrated_risk_grade_failures(row.text, [
        {'name': selected[key].name, 'index': (health_scores or {}).get(key, {}).get('index'),
            'scoreKind': 'independent_health_score'} for key in row.risk_ids]))
    if row.slot in {'overview','medical','conclusion'} and re.search(
            r'(?:其余|其他|全部|所有)[^。！？]{0,20}风险[^。！？]{0,14}(?:正常|低风险)',row.text):
        remaining=[risk for key,risk in selected.items() if key not in row.risk_ids
            and risk.certainty=='potential_risk']
        if any((health_scores or {}).get(risk.risk_id, {}).get('level') != '低风险' for risk in remaining):
            errors.append('global_remaining_risk_grade_not_supported')
    for sentence in _summary_sentences(row):
        text=sentence['text']
        if re.search(r'(?:无|没有|未见|未报告)[^，。！？]{0,20}(?:症状|病史|不适|临床线索)',text):
            if not any(claim.role=='source_fact' and text.strip('。！？ ') in claim.text for claim in bound):
                errors.append('unbound_negative_patient_history')
    for code,aliases in FIXED_DISEASE_ALIASES.items():
        if not _mentions(row.text,aliases):
            continue
        if not _mentions(allowed,aliases):
            errors.append('unbound_disease:'+code)
            continue
        if any('筛查' in sentence['text'] and _mentions(sentence['text'],aliases)
                for sentence in _summary_sentences(row)) and not any(claim.role=='source_fact'
                and _mentions(claim.text,aliases) and any(fid in facts
                    and _fact_role(facts[fid])=='microbiome_association_screen'
                    and _risk_identity(facts[fid].name)==code for fid in claim.fact_quotes) for claim in bound):
            errors.append('source_model_is_not_association_screen:'+code)
        for sentence in _summary_sentences(row):
            if not _mentions(sentence['text'],aliases) or not re.search(r'检出|阳性',sentence['text']):
                continue
            positive=False
            for claim in bound:
                if claim.role!='source_fact' or not _mentions(claim.text,aliases):
                    continue
                for fid in claim.fact_quotes:
                    fact=facts.get(fid)
                    if fact is None:
                        continue
                    role=_fact_role(fact)
                    if (role=='microbiome_association_screen' and _risk_identity(fact.name)==code
                            and fact.value in {'检出','阳性','异常'}):
                        positive=True
                    if fact.kind=='clinical' and (fact.abnormal or re.search(r'检出|阳性',fact.value+' '+str(fact.status))):
                        positive=True
            if not positive:
                errors.append('model_is_not_positive_detection:'+code)
    return list(dict.fromkeys(errors))


def _summary_relation_errors(row,bound,selected,facts):
    """An editorial combination cannot create an unreviewed disease edge."""
    from .evaluation_catalog import FIXED_DISEASE_ALIASES
    from .evaluation_evidence_plan import _mentions
    scientific=[claim for claim in bound if claim.role in {'outcome','etiology','mechanism'} and claim.evidence]
    aliases={risk.risk_id:tuple(dict.fromkeys([_endpoint_name(risk.name),
        *FIXED_DISEASE_ALIASES.get(risk.disease_id,())])) for risk in selected.values()}
    edges=set()
    for claim in scientific:
        exposures={risk.risk_id for risk in selected.values() if any(
            _mentions(' '.join(group),aliases[risk.risk_id]) for group in claim.exposure_terms)}
        premises={fid:quote for fid,quote in claim.fact_quotes.items() if fid in facts}
        observation=claim.model_copy(update={'role':'source_fact','fact_quotes':premises})
        exposures.update(risk.risk_id for risk in selected.values() if risk.certainty in {
            'confirmed','current_problem','self_reported'} and _same_observed_problem(risk,[observation],facts))
        outcomes={risk.risk_id for risk in selected.values()
            if _mentions(' '.join(claim.outcome_terms),aliases[risk.risk_id])}
        edges.update(frozenset((left,right)) for left in exposures for right in outcomes if left!=right)
    for sentence in _summary_sentences(row):
        for clause in re.split(r'[；;。]+',sentence['text']):
            if not re.search(r'相互(?:关联|相关|影响)|彼此.{0,8}(?:关联|相关)|'
                    r'共享.{0,35}(?:基础|原因|诱因)|共同.{0,18}(?:基础|原因|诱因)|'
                    r'与[^。；]{0,100}(?:相关|关联|有关)|导致|引起',clause):
                continue
            mentioned={identifier for identifier in row.risk_ids if _mentions(clause,aliases[identifier])}
            if len(mentioned)>=2:
                if re.search(r'相互|彼此',clause):
                    supported=all(frozenset((left,right)) in edges for left in mentioned
                        for right in mentioned if left!=right)
                else:
                    supported=any(all(other==center or frozenset((center,other)) in edges
                        for other in mentioned) for center in mentioned)
            else:
                supported=any(any(_mentions(clause,_concept_forms(group)) for group in claim.exposure_terms)
                    and _mentions(clause,_concept_forms(claim.outcome_terms)) for claim in scientific)
            if not supported:
                return ['summary_relation_not_bound_to_selected_atoms']
    return []


def _summary_materials(document, risks, originals=None):
    """Expose actual reviewed personal actions, not just their principles.

    These are request-local author/review views, not new risk claims.  A
    changed or serialized action has no planning permission. Its real fact
    scope and original principle bindings remain necessary at publication.
    """
    facts = _facts(document)
    claims = {risk.risk_id + ':' + claim.claim_id: (risk, claim)
        for risk in risks for claim in risk.claims}
    bindings = {key: [key] for key in claims}
    if originals is None:
        return claims, bindings
    for risk in risks:
        own = {claim.claim_id: claim for claim in risk.claims}
        for action in risk.lifestyle_actions:
            selected = [own.get(key) for key in action.claim_ids]
            if (not selected or any(claim is None or claim.role != 'management' for claim in selected)
                    or not action.fact_ids or not set(action.fact_ids) <= facts.keys()
                    or set(action.evidence_keys) != {anchor.evidence_key for claim in selected for anchor in claim.evidence}
                    or any(anchor.evidence_key not in originals or anchor.quote not in originals[anchor.evidence_key]['sourceText']
                        for claim in selected for anchor in claim.evidence)):
                continue
            direct = (action.text.strip() == ''.join(claim.text.strip() for claim in selected)
                and set(action.fact_ids) == {fid for claim in selected for fid in claim.fact_quotes})
            if not direct and not _approved_management_plan(action, risk, facts, originals):
                continue
            # Exact atomic actions are already in approvedClaims. Only a
            # separately reviewed personal plan needs its own material ID.
            if direct:
                continue
            key = risk.risk_id + ':action:' + _digest(action.model_dump(mode='json', by_alias=True))[:20]
            material = RiskClaim(claim_id=key.split(':', 1)[1], role='management', text=action.text,
                fact_quotes={fid: facts[fid].value for fid in action.fact_ids},
                evidence=[anchor.model_copy(deep=True) for claim in selected for anchor in claim.evidence])
            claims[key] = (risk, material)
            bindings[key] = [risk.risk_id + ':' + claim.claim_id for claim in selected]
    return claims, bindings



def _summary_requires_interpretation(slot):
    return slot in {'overview','medical','microbiome','nutrition','pathways','conclusion'} or slot.startswith(('clinical:','detail:'))


def _summary_interpretation_material(risk, claim):
    return bool(claim.evidence and claim.role in {'outcome','etiology','mechanism','management'}
        or claim._classification_receipt is not None and _personal_clinical_disease_assessment(claim,risk))


def _detail_summary_has_interpretation(row, claims):
    """A detailed disease paragraph needs a reviewed meaning or action.

    This is only a necessary role check. It grants no publication approval;
    sentence entailment, exact patient scope and original evidence still
    require the existing independent review.
    """
    return (not _summary_requires_interpretation(row.slot) or any(
        _summary_interpretation_material(*claims[key]) for key in row.claim_ids if key in claims))


def _summary_review_approved(row,review,*,selected=None,claims=None,facts=None,health_scores=None):
    if not (all(review.get(key) is True for key in ('entailsSummary','noNewMedicalAssertions','preservesQualification'))
            and review.get('riskIds')==row.risk_ids and review.get('claimIds')==row.claim_ids
            and set(review.get('evidenceKeys') or [])==set(row.evidence_keys)):
        return False
    if claims is not None and not _detail_summary_has_interpretation(row, claims):
        return False
    expected={item['sentenceId'] for item in _summary_sentences(row)}
    roles=review.get('sentenceRoles')
    if not isinstance(roles,list) or len(roles)!=len(expected):
        return False
    seen=set(); interpreted=False
    for item in roles:
        if (not isinstance(item,dict) or item.get('sentenceId') not in expected or item['sentenceId'] in seen
                or item.get('role') not in {'patient_state','risk_meaning','priority','management'}
                or not isinstance(item.get('claimIds'),list) or not item['claimIds']
                or not set(item['claimIds'])<=set(row.claim_ids)):
            return False
        seen.add(item['sentenceId'])
        if claims is not None:
            bound_roles={claims[key][1].role for key in item['claimIds'] if key in claims}
            if item['role']=='management' and 'management' not in bound_roles:
                return False
            interpreted |= (item['role']=='risk_meaning' and (bool(bound_roles & {'outcome','etiology','mechanism'})
                or any(claims[key][1]._classification_receipt is not None and
                    _personal_clinical_disease_assessment(claims[key][1], claims[key][0]) for key in item['claimIds']))
                or item['role'] in {'management','priority'} and 'management' in bound_roles)
        if selected is not None and claims is not None and facts is not None:
            text=next(s['text'] for s in _summary_sentences(row) if s['sentenceId']==item['sentenceId'])
            ids=list(dict.fromkeys(claims[key][0].risk_id for key in item['claimIds']))
            sentence=row.model_copy(update={'text':text,'claim_ids':item['claimIds'],'risk_ids':ids})
            if _summary_scope_errors(sentence,selected,claims,facts,health_scores=health_scores):
                return False
    return seen==expected and (not _summary_requires_interpretation(row.slot) or interpreted)


async def _compose_risk_summary_batch(client,document,slots,*,subject=None,originals=None):
    """One bounded synthesis pass and independent review for distinct slots."""
    registry=_registry(document)
    risks=approved_assessments(document)
    from .evaluation_dimension_scores import approved_health_scores
    health_scores = approved_health_scores(document)
    from .evaluation_risk_projection import _editorial_risks, _health_priority
    risks=_editorial_risks([risk for risk in risks if is_publishable_disease_assessment(risk)],
        _facts(document),document=document)
    risks.sort(key=lambda risk: _health_priority(risk, health_scores))
    if all(slot.startswith(('key:','clinical:','detail:','priority:','disease:')) for slot in slots):
        identifiers={slot.split(':',1)[1] for slot in slots if slot.startswith(('key:','clinical:','detail:','priority:'))}
        categories={slot.split(':',1)[1] for slot in slots if slot.startswith('disease:')}
        risks=[row for row in risks if row.risk_id in identifiers or row.category in categories]
    if not risks:
        return {}
    selected={row.risk_id:row for row in risks}
    claims, _ = _summary_materials(document, risks, originals)
    instructions={
        'overview':'用自然一段呈现主要疾病或潜在疾病风险及本次关注程度，再结合真实个人诱因、可能健康影响和优先日常改善形成整体画像。仅选有据的重要问题，不把症状、指标或菌群检出状态当疾病结论；不列数值或规则，不强求三种来源各写一次。实际可用关系与行动必须综合表达，缺失环节不编造，不把共同存在拼成因果链。',
        'medical':'用二至三句说明主要临床疾病或有据潜在风险、当前关注程度及最有意义的个人诱因、健康影响和可行日常改善。症状与临床指标只作其依据，保留风险和已确诊的区别；不复制医学表、不只写行动命令，也不默认转诊。称您或省略主语。',
        'microbiome':'判断实际菌群结构与功能的主要问题，解释同题有据的健康影响、个人相关因素与适用改善方向；程度采用已审结论。少量真实发现作支持，不逐菌列升降，不因有原始状态就当作完整解读；原级与方向不混用，不借别病管理宣称改变菌群。',
        'nutrition':'综合实际营养功能的主要表现及程度，结合本组有据健康意义、真实个人因素和已审日常改善表达重点。选少量代表作支持，不列名册或正常计数，不把菌群合成评分诊断为人体缺乏，不将有害物生成低当短板，不因高分或升高就写健康优势。',
        'pathways':'归纳真实菌群功能通路的整体模式及已审个人健康意义，说明与前文具体健康问题的联系；不逐行复抄通路表，不把临床血脂改称菌群通路。',
        'conclusion':'用独立一段统筹主要疾病或潜在风险、关注次序、真实个人诱因与可能影响，再归纳最优先的有据饮食、活动、睡眠等改善方向；只写本槽实际已审关系与行动，不强凑缺失方向或默认检查。相同依据的近似问题合并管理，不重复首页风险表，不复制总览全文，不引入新疾病、病因、疗效或未绑定功能。',
        'priority_basis':'简短说明当前关注顺序，不描述评分或生成规则。'}
    slot_requests=[]
    for slot in slots:
        bound=([slot.split(':',1)[1]] if slot.startswith(('key:','clinical:','detail:','priority:')) else
            [row.risk_id for row in risks if row.category==slot.split(':',1)[1]]
            if slot.startswith('disease:') else list(selected))
        if slot in {'overview','medical','conclusion','priority_basis'} or slot.startswith('disease:'):
            from .evaluation_risk_projection import _is_main_health_risk
            # Use the same editorial eligibility as projection. Normal
            # potential rows stay in their original data/ranking, not a
            # chapter's list of priority problems.
            bound=[key for key in bound if _is_main_health_risk(selected[key], health_scores)]
        eligible=[key for key,(risk,claim) in claims.items() if risk.risk_id in bound
            and (slot.startswith(('priority:', 'key:', 'clinical:', 'detail:', 'disease:')) or slot in {
                'overview','medical','microbiome','nutrition','conclusion'} or claim.role!='management')]
        if slot.startswith(('clinical:', 'detail:')) and not any(
                claim.role in {'outcome','etiology','mechanism','management'} and claim.evidence
                or _personal_clinical_disease_assessment(claim, risk)
                for key in eligible for risk, claim in [claims[key]]):
            eligible = []  # No reviewable interpretation yet; do not author a source recap.
        slot_requests.append({'slot':slot,'instruction':instructions.get(slot,
            '简短说明本风险为何优先及对应的实际下一步关注，不重复原始指标清单，执行动作采用本风险已审内容。' if slot.startswith('priority:') else
            '用一至两句自然浓缩具体疾病或潜在疾病风险、本次关注程度、真实个人诱因、可能影响与优先日常改善；只采用本病已有支持的环节，不为凑齐编写。症状、指标或菌群仅作支持，不作疾病标题；不列数值或状态名册，不复制详细解读。' if slot.startswith('key:') else
            '用二至三句简明解释本病：先点明具体问题与本次关注程度，再说本病有据个人诱因、健康影响或可行改善；指标只作依据，不只写行动命令或宽泛多病共存，不复用首页key或第五章全文。' if slot.startswith('clinical:') else
            '用独立一段三至五句详细解释本病：先说明真实健康问题与本次关注程度，再解释本病已审的个人诱因、健康影响和适用日常改善。只写有同病已审关系的环节，不能把并存当因果、诊断定义当未来风险，不能以重述指标或检出代替解释。原值留在表格，菌群只作同病适用补充；本槽不能复用key原句充满段落。没有已审健康意义或管理支持时不返回本槽，不用规则或通用警示填空。' if slot.startswith('detail:') else
            '先概括本类真正需要关注的具体健康问题、相关症状和有据诱因。以实际临床状态与病史为核心，菌群仅补充同病已审关系；不重复指标或菌名名单、不跨疾病借用结局，不凑三种来源。'),
            'allowedRiskIds':bound,'allowedClaimIds':eligible})
    contracts={item['slot']:item for item in slot_requests}
    def admit(raw,pending,blocked):
        try:
            row=RiskSummary.model_validate(raw)
            contract=contracts.get(row.slot)
            if (contract is None or row.slot in pending or not row.claim_ids
                    or not set(row.risk_ids)<=set(contract['allowedRiskIds'])
                    or not set(row.claim_ids)<=set(contract['allowedClaimIds'])
                    or any(claims[key][0].risk_id not in row.risk_ids for key in row.claim_ids)):
                return
            expected={anchor.evidence_key for key in row.claim_ids for anchor in claims[key][1].evidence}
            if set(row.evidence_keys)!=expected:
                return
            if row.slot.startswith(('key:','clinical:','detail:','priority:')) and row.risk_ids!=[row.slot.split(':',1)[1]]:
                return
            if (not _detail_summary_has_interpretation(row, claims) or _BOUNDARY.search(row.text)
                    or _summary_scope_errors(row,selected,claims,_facts(document),health_scores=health_scores)):
                blocked[row.slot]=row
                return
            pending[row.slot]=row
        except (ValueError,TypeError,ValidationError):
            return
    try:
        response=await client.generate({'task':'compose_integrated_risk_summaries','subject':_subject_view(subject),
            'providedPatientFields':_provided_patient_fields(_facts(document)),
            'slots':slots,'slotRequests':slot_requests,
            'approvedRisks':[{'riskId':row.risk_id,'name':row.name,'category':row.category,
                'index':health_scores.get(row.risk_id, {}).get('index'),'level':health_scores.get(row.risk_id, {}).get('level'),'certainty':row.certainty,
                'scoreKind':'independent_health_score',
                'basisUse':'仅理解已审关注程度；不得原样发布相反资料、方法规则或借此新增医学关系'} for row in risks],
            'approvedClaims':[{'claimId':identifier,'riskId':row.risk_id,'role':claim.role,'text':claim.text,
                'factIds':list(claim.fact_quotes),'evidenceKeys':[a.evidence_key for a in claim.evidence]}
                for identifier,(row,claim) in claims.items()],
            'rules':[
                '综合疾病关注只采用approvedRisks中本次独立健康分提供的公开低风险/中风险/高风险三档；高分代表较好健康状态，index/level=null时不编等级，不能拿内部旧风险指数顶替，不写私有旧四档标签；'
                '原报告原级在源结果字段保留，不拿原级或另一疾病指数替换本病综合关注。'
                '已审management可忠实概括为日常改善方向，不把它反推为病因，也不扩写原句未支持的执行细节或效益。',
                '正文直接面向用户，统一用“您”或省略主语，不写“本人”“代表方向”“已列低项”；不写“公开关注等级”“已审个人依据”“本槽”或factId/planning等控制说明；每句提供新信息，不用异常A—关注A的循环句，也不先后重复同一份问题清单。',
                '疾病解读以本病已审的临床状态、症状、持续影响和病史为个人判断核心；菌群状态及源模型仅作同病有据补充。指标是依据，不是风险结论，源信号不是患病事实。涉及病因、健康风险或改善关系时只能忠实采用本slot实际绑定的已审原子句。',
                '本次仅生成slots和slotRequests中逐字列出的槽位，不是整份报告。响应slot必须在本批数组中；不在本批时绝不能输出overview/medical等全局栏目，不得将一个风险的key写成另一风险。',
                '按每个slotRequest的allowedRiskIds/allowedClaimIds选择材料；逐槽位写对应职责。没有可归纳材料可省略该slot，不为补齐栏目编内容。',
                '只归纳已批准状态与原子句，不新增疾病、机制、风险关系、建议或诊断。每段绑定真实riskIds、claimIds与evidenceKeys。',
                'source_fact只授权事实并列，不能因多个已知问题同时出现就写相互关联、共同诱因或共享基础。每个关联子句须由本slot实际选择的outcome/etiology/mechanism原子句支持其暴露与结局；不得借同风险未选择的关系，也不得把一条腰围→糖尿病关系扩大成腰围、血脂与糖尿病三者相互关联。',
                '各slot职责不同：overview为总体重点；medical为临床健康问题；microbiome为实际菌群/功能与风险；nutrition为营养相关重点；pathways为通路意义；conclusion为整体判断和先后；不将同一全文重复到多处。',
                'key:<riskId>只给该风险一句简短核心提示；clinical:<riskId>用二至三句完成当前问题、有据影响或改善，不复制首页短述或详细全文；detail:<riskId>为同病独立详细解读，至少有一条已审健康意义或适用管理，不能仅复述source_fact或定义。priority:<riskId>是该风险后续重点，不照抄原结论。disease:<类别>只归纳本类已评风险。',
                'key/disease以具体疾病或有据疾病风险为主，自然串联本次关注程度、真实个人诱因、可能健康影响和优先改善，只写实际支持的环节；症状是支撑而非新疾病标题。management不替代问题判断、不反推病因，也不复制数值或就诊流程。无相关已审内容的slot不返回。正文不写来源、原模型、AI评分或内部判断规则。',
                '不用“尚未核验、需结合临床评估、不等同诊断、仅作辅助参考”等方法/限制说明。用“自述餐后反酸”“某病潜在风险”直接保留真实确定性；不因去掉声明把风险改为患病或把自述改为确诊。',
                '总体段落不超过300字，key/priority不超过100字；clinical为二至三句、不超过220字；detail为独立三至五句、不超过450字，不照抄key、不凑字数。数值和全部菌名留表格。',
                'approvedRisks已按关注等级及同等级内的个人相关性排列。总体与结论优先回应同等级内已存在的问题和有个人关系依据的风险，不让孤立来源筛查信号替代这些现实管理重点。完整风险与原指数仍保留原表。',
                '每slot聚焦最多4个相关风险、最多8条已审原子句；只返回必要claimIds及其真实引文键，不复写全量风险表与全量参考文献。',
                'providedPatientFields只证明这些检查/问卷资料已提供，不授权任何新正向风险。没有出现在approvedClaims不等于未提供，绝不能编写“未提供血糖/病史”等缺失断言；概述实际已审问题即可。真实阴性与缺少资料不同。',
            ],'outputSchema':{'summaries':[{'slot':slot,'text':'仅该槽职责的简短个人结论；没有适用材料则不返回本项',
                'riskIds':['本槽allowedRiskIds内ID'],'claimIds':['本槽allowedClaimIds内ID'],
                'evidenceKeys':['这些claim实际引文键']} for slot in slots]}})
        rows=response.get('summaries',[]) if isinstance(response,dict) else []
        pending={};blocked={};misrouted=False
        for raw in rows:
            admit(raw,pending,blocked)
            if not isinstance(raw,dict) or raw.get('slot') not in {*pending,*blocked}:
                misrouted=True
        # A syntactically correct response may still omit a requested slot.
        # Repair every real omission once, not only malformed/misrouted output.
        missing=[contract for contract in slot_requests if contract['allowedClaimIds']
            and contract['slot'] not in pending and contract['slot'] not in blocked]
        if missing:
            # One local repair of dropped/misrouted slots, never a rerun of
            # already accepted global slots or a new medical judgement.
            try:
                allowed={key for item in missing for key in item['allowedClaimIds']}
                repair=await client.generate({'task':'repair_integrated_risk_summary_slots',
                    'slots':[item['slot'] for item in missing],'slotRequests':missing,
                    'rejectedSummaries':[row for row in rows if isinstance(row,dict)
                        and row.get('slot') in {item['slot'] for item in missing}],
                    'approvedRisks':[{'riskId':row.risk_id,'name':row.name,'category':row.category,
                        'certainty':row.certainty,'level':health_scores.get(row.risk_id, {}).get('level')} for row in risks],
                    'approvedClaims':[{'claimId':key,'riskId':claims[key][0].risk_id,
                        'role':claims[key][1].role,'text':claims[key][1].text,
                        'evidenceKeys':[anchor.evidence_key for anchor in claims[key][1].evidence]}
                        for key in claims if key in allowed],
                    'rules':['前稿遗漏或写错了这些槽位。仅一次修复本次逐字slots，禁止重新输出任何未请求的全局栏目。',
                        '每槽只用其allowedRiskIds/allowedClaimIds，不新增医疗判断、关系或动作；空材料不补造。',
                        '重新核对rejectedSummaries的riskIds/claimIds/evidenceKeys。每个提到的具体疾病都必须有本槽所选claim支撑，引用键必须恰好等于所选claim的实际引文键；不要原样重发错误对象或把别风险名称塞进本slot。',
                        '直接写个人实际问题、潜在风险和已审意义，不写来源/审核/规则/不能诊断等方法说明；保持自述与潜在的真实确定性。'],
                    'outputSchema':{'summaries':[{'slot':item['slot'],'text':'本槽简短个人结论',
                        'riskIds':[],'claimIds':[],'evidenceKeys':[]} for item in missing]}})
                for raw in repair.get('summaries',[]) if isinstance(repair,dict) else []:
                    if isinstance(raw,dict) and raw.get('slot') in {item['slot'] for item in missing}:
                        admit(raw,pending,blocked)
            except Exception:
                pass
        if blocked:
            # One scoped wording repair; unchanged claim bindings and the
            # independent entailment review still govern the replacement.
            try:
                repair=await client.generate({'task':'repair_integrated_risk_summary_rules',
                    'providedPatientFields':_provided_patient_fields(_facts(document)),
                    'items':[{'summary':row.model_dump(mode='json',by_alias=True),
                        'validationErrors':([*(['public_rule_text'] if _BOUNDARY.search(row.text) else []),
                            *_summary_scope_errors(row,selected,claims,_facts(document),health_scores=health_scores)]),
                        'sentences':_summary_sentences(row),
                        'approvedClaims':[{'claimId':key,'role':claims[key][1].role,'text':claims[key][1].text}
                            for key in row.claim_ids]} for row in blocked.values()],
                    'rules':['只重写已有slot文字一次，riskIds/claimIds/evidenceKeys全部保持。只归纳对应已审句，不补医学判断或建议。',
                        '逐项处理validationErrors，删去未绑定的具体疾病/超出原子句的关联/方法说明整句，不原样重发。若本槽只绑定当前问题，不再谈未绑定的精神疾病或其他风险；删去错误语义后重新组织一句有用的判断，不拼补免责声明。',
                        '已提供字段清单仅用于反驳错误缺失断言，不作新风险依据。不能因本slot没有正向血糖/病史支持就说用户未提供；不补新的患病或阴性结论，省略不属于本段的资料缺失讨论。',
                        '只允许本slot已绑定的风险与claim实际疾病，不借全局别slot的疾病。来源风险模型低/高结果不是菌群筛查阳性或检出，不能把风险signal改写为检出。',
                        '删除来源、模型、审核、未核验、仅辅助参考、不能/不等同诊断、需结合评估等内部方法说明。直接写个人实际问题、潜在风险及有据意义；保留自述/潜在/关联的真实确定性，不升级患病。'],
                    'outputSchema':{'summaries':[{'slot':'原slot','text':'不含方法规则的个人重点',
                        'riskIds':['原ID'],'claimIds':['原ID'],'evidenceKeys':['原键']}]}})
                for raw in repair.get('summaries',[]) if isinstance(repair,dict) else []:
                    try:
                        row=RiskSummary.model_validate(raw);old=blocked.get(row.slot)
                        if (old is not None and row.risk_ids==old.risk_ids and row.claim_ids==old.claim_ids
                                and row.evidence_keys==old.evidence_keys and not _BOUNDARY.search(row.text)
                                and not _summary_scope_errors(row,selected,claims,_facts(document),health_scores=health_scores)):
                            pending[row.slot]=row
                    except (ValueError,TypeError,ValidationError):
                        continue
            except Exception:
                pass
        if not pending:
            registry.incomplete.append('INTEGRATED_RISK_SUMMARY_INCOMPLETE')
            return {}
        review_payload={'task':'review_integrated_risk_summaries',
            'providedPatientFields':_provided_patient_fields(_facts(document)),
            'completeRiskSnapshot':[{'riskId':risk.risk_id,'name':risk.name,'level':health_scores.get(risk.risk_id, {}).get('level'),
                'certainty':risk.certainty} for risk in approved_assessments(document)],
            'items':[{'summary':row.model_dump(mode='json',by_alias=True),
                'sentences':_summary_sentences(row),
                'sourceClaims':[{'claimId':key,'text':claims[key][1].text,'role':claims[key][1].role}
                    for key in row.claim_ids],
                'sourceRisks':[{'riskId':identifier,'name':selected[identifier].name,
                    'level':health_scores.get(identifier, {}).get('level'),'index':health_scores.get(identifier, {}).get('index'),
                    'certainty':selected[identifier].certainty} for identifier in row.risk_ids]}
                for row in pending.values()],
            'rules':['逐段独立核查仅归纳给出的已批准原子句与状态，没有新增医学判断、病情、因果、建议或升级确定性。',
                '逐个关系子句核实所选sourceClaims是否真有该关系及其两个端点；多个source_fact的共同出现只支持事实并列，不证明相互关联、共同病因或共享基础。任一新增边将noNewMedicalAssertions=false，不能只因病名和riskIds都真实就批准。分区有当前中高关注问题时，低关注纯来源项不抢重点；原始值和完整ranking仍保留。',
                '核查slot职责和对象；科室检查或生活方式动作不能作为菌群/通路机制的依据。引用绑定不得借用未选claim。',
                '其余/全部等跨项目概括必须符合completeRiskSnapshot，不得把未选的注意/中高风险称正常。未在selected claims出现不等于本人没有该症状，不新写无症状或无病史；原风险模型不称关联筛查或菌群检出。',
                '独立核对providedPatientFields：它只证明资料存在，不是正向风险证据。已提供血糖等正常检查不得被写成未提供；缺少positive claim与缺少输入资料严格区分。错误缺失断言应noNewMedicalAssertions=false，不能因源slot没有选择该检查就批准。',
                '正文不含来源/模型/审核/未核验/不构成诊断/仅辅助参考等规则说明；用自述、潜在风险、相关的准确措辞保留确定性，不升级为本人确诊。',
                '逐一sentenceId给职责：patient_state本人状态、risk_meaning已有风险意义、priority关注重点、management已审适用改善；clinical/detail必须有risk_meaning或有原管理claim支撑的management/priority句，纯事实或诊断定义复述不是完整详细解读。internal_rule内部边界、source_method来源/方法说明、medical_warning通用免责声明均不得发布。一句混合本人状态与规则也归internal_rule，不因句中一部分有据就批准整句。必须覆盖全部句，不能漏句或借别的claim解释。',
                '逐slot返回entailsSummary/noNewMedicalAssertions/preservesQualification及原riskIds/claimIds/evidenceKeys；不以笼统valid代替。'],
            'outputSchema':{'items':[{'slot':'原slot','entailsSummary':True,'noNewMedicalAssertions':True,
                'preservesQualification':True,'riskIds':[],'claimIds':[],'evidenceKeys':[],
                'sentenceRoles':[{'sentenceId':'给定ID','role':'patient_state|risk_meaning|priority|management|internal_rule|source_method|medical_warning',
                    'claimIds':['确实支持本句的已给ID']}]}]}}
        response=await client.generate(review_payload)
        reviews=response.get('items',[]) if isinstance(response,dict) else []
        approved={}
        for slot,row in pending.items():
            matches=[review for review in reviews if isinstance(review,dict) and review.get('slot')==slot]
            review=matches[0] if len(matches)==1 else {}
            if _summary_review_approved(row,review,selected=selected,claims=claims,facts=_facts(document),health_scores=health_scores):
                approved[slot]=row
        failed={slot:row for slot,row in pending.items() if slot not in approved and slot not in blocked}
        if failed:
            try:
                repair=await client.generate({'task':'repair_integrated_risk_summary_rules',
                    'items':[{'summary':row.model_dump(mode='json',by_alias=True),
                        'previousReview':[review for review in reviews if isinstance(review,dict)
                            and review.get('slot')==row.slot],
                        'validationErrors':_summary_scope_errors(row,selected,claims,_facts(document),health_scores=health_scores),
                        'approvedClaims':[{'claimId':key,'role':claims[key][1].role,'text':claims[key][1].text}
                            for key in row.claim_ids]} for row in failed.values()],
                    'rules':['仅一次修正这些摘要，绑定ID不变。删除所有来源/内部规则/方法/通用警示句，保持自述/潜在/相关的实际确定性。',
                        '按previousReview的具体逐句职责与validationErrors修正；internal_rule/source_method/medical_warning整句改为已绑定本人状态或删除，未绑定病名不得继续出现。不要原样重发失败段。',
                        '只归纳已审本人状态和本病意义，不把无关观察并列暗示病因，不新增临床判断或行动。'],
                    'outputSchema':{'summaries':[{'slot':'原slot','text':'只含个人重点','riskIds':[],
                        'claimIds':[],'evidenceKeys':[]}]}})
                fixed={}
                for raw in repair.get('summaries',[]) if isinstance(repair,dict) else []:
                    try:
                        row=RiskSummary.model_validate(raw);old=failed.get(row.slot)
                        if (old is not None and row.risk_ids==old.risk_ids and row.claim_ids==old.claim_ids
                                and row.evidence_keys==old.evidence_keys and not _BOUNDARY.search(row.text)
                                and not _summary_scope_errors(row,selected,claims,_facts(document),health_scores=health_scores)):
                            fixed[row.slot]=row
                    except (TypeError,ValueError,ValidationError):
                        continue
                if fixed:
                    response=await client.generate({**review_payload,'items':[
                        {**item,'summary':fixed[item['summary']['slot']].model_dump(mode='json',by_alias=True),
                            'sentences':_summary_sentences(fixed[item['summary']['slot']])}
                        for item in review_payload['items'] if item['summary']['slot'] in fixed]})
                    rows=response.get('items',[]) if isinstance(response,dict) else []
                    for slot,row in fixed.items():
                        matches=[review for review in rows if isinstance(review,dict) and review.get('slot')==slot]
                        if len(matches)==1 and _summary_review_approved(row,matches[0],
                                selected=selected,claims=claims,facts=_facts(document),health_scores=health_scores):
                            approved[slot]=row
            except Exception:
                pass
        return approved
    except Exception:
        registry.incomplete.append('INTEGRATED_RISK_SUMMARY_INCOMPLETE')
        return {}


async def compose_risk_summaries(client,document,*,subject=None,evidence_summaries=None,only_missing=False):
    """Bound long reports by slots, preserving every independently approved batch."""
    registry=_registry(document)
    if evidence_summaries is not None:
        refresh_risk_original_scope(document, evidence_summaries)
    previous = dict(approved_risk_summaries(document))
    if client is None or previous and not only_missing:
        return previous
    risks=display_risk_assessments(document,[risk for risk in approved_assessments(document)
        if is_publishable_disease_assessment(risk)])
    if not risks:
        return {}
    # The overview, three domain abstracts and closing chapter have separate
    # visible duties, all bound to the same live reviewed registry.
    slots=['overview','medical','microbiome','nutrition','pathways','conclusion']
    slots.extend('disease:'+category for category in dict.fromkeys(row.category for row in risks))
    # Every displayed assessment needs an interpretation, including a low
    # composite which differs from the original model. Priority actions are
    # still reserved for actual current/attention problems below.
    slots.extend('key:'+row.risk_id for row in risks)
    slots.extend('clinical:'+row.risk_id for row in risks)
    slots.extend('detail:'+row.risk_id for row in risks)
    slots.extend('priority:'+row.risk_id for row in risks if row.index>=25
        or row.certainty in {'confirmed','current_problem','self_reported'})
    all_slots = list(slots)
    if only_missing:
        slots = [slot for slot in slots if slot not in previous]
    if not slots:
        return previous
    batches=[slots] if len(risks)==1 else [slots[offset:offset+8] for offset in range(0,len(slots),8)]
    originals = _originals(evidence_summaries) if evidence_summaries is not None else None
    gate=asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
    async def run(batch):
        async with gate:
            return await _compose_risk_summary_batch(client,document,batch,subject=subject,originals=originals)
    results=await asyncio.gather(*(run(batch) for batch in batches),return_exceptions=True)
    approved={**previous, **{key:value for result in results if isinstance(result,dict) for key,value in result.items()}}
    registry.approved_summaries=approved
    registry._summary_proof=_summary_scope(document)
    if 'overview' not in approved:
        registry.incomplete.append('INTEGRATED_RISK_SUMMARY_INCOMPLETE')
    elif any(slot not in approved for slot in all_slots if slot.startswith(('key:','clinical:','detail:','priority:','disease:'))):
        registry.incomplete.append('INTEGRATED_RISK_SUMMARY_INCOMPLETE')
    elif all(isinstance(result,dict) for result in results):
        registry.incomplete=[reason for reason in registry.incomplete if reason!='INTEGRATED_RISK_SUMMARY_INCOMPLETE']
    from .schemas import ClinicalReportBlock
    claims, material_bindings = _summary_materials(document, risks, originals)
    document.sections=[block for block in document.sections if not block.code.startswith('riskSummary:')]
    for slot,row in approved.items():
        refs=set();quotes={};bound={}
        for key in row.claim_ids:
            risk,claim=claims[key]
            for origin in material_bindings.get(key, [key]):
                block=next((block for block in document.sections if block.code=='integratedRisk:'+origin),None)
                if block:
                    refs.update(block.evidence_refs)
                    for number,quote in block.evidence_quotes.items():
                        quotes[number]='\n'.join(dict.fromkeys([*quotes.get(number,'').splitlines(),quote])).strip()
                        bound.setdefault(number,[])
                        bound[number].extend(fid for fid in claim.fact_quotes if fid not in bound[number])
        document.sections.append(ClinicalReportBlock(code='riskSummary:'+slot,text=row.text,
            fact_ids=sorted({fid for key in row.claim_ids for fid in claims[key][1].fact_quotes}),
            evidence_refs=sorted(refs),evidence_quotes=quotes,
            evidence_claims={str(number):row.text for number in refs},evidence_fact_ids=bound))
    return approved


def _management_anchor(reference, quote, sources):
    """An exact short key, or one literal unique allowed original; never guess."""
    if not isinstance(quote,str) or len(quote.strip())<8:
        return None
    exact=sources.get(reference)
    if exact is not None and _canonical_management_quote(quote, exact['sourceText']) is not None:
        return exact
    matches={source['evidenceKey']:source for source in sources.values()
        if _canonical_management_quote(quote, source['sourceText']) is not None}
    return next(iter(matches.values())) if len(matches)==1 else None


def _canonical_management_quote(quote, source):
    """Recover only case typography, returning the original literal span."""
    if not isinstance(quote, str) or not isinstance(source, str):
        return None
    if quote in source:
        return quote
    spans = {match.group() for match in re.finditer(re.escape(quote), source, re.I)}
    return next(iter(spans)) if len(spans) == 1 else None


def _management_source_scope_requirements(claim, originals):
    """Extract the article's declared care population, not every covariate.

    A recommendation inside a disease follow-up guideline retains the
    guideline's population even when its selected sentence omits the name.
    These are source witnesses, never permission to diagnose the patient.
    """
    population=r'(?:adults?|patients?|people|individuals|women|men|children)\s+(?:with|living with|receiving|undergoing)\s+[^.;:\n]{2,110}'
    result=[]
    for anchor in claim.evidence:
        source=originals.get(anchor.evidence_key,{})
        title=str(source.get('title')or'');text=source.get('sourceText','')
        selected=[match.group() for match in re.finditer(population,title,re.I)]
        trial=bool(re.search(r'randomi[sz]ed|\btrial\b',title,re.I))
        for sentence in re.split(r'(?<=[.!?])\s+|\n',text):
            if re.search(r'(?:guideline|consensus|statement|recommendations?).{0,100}(?:assessment|monitoring|management|treatment)',sentence,re.I) or (
                    trial and re.search(r'randomi[sz]ed|enrolled|included|conducted|participants',sentence,re.I)):
                selected.extend(match.group() for match in re.finditer(population,sentence,re.I))
        for quote in dict.fromkeys(selected):
            condition=re.sub(r'^.*?\b(?:with|living with|receiving|undergoing)\s+','',quote,flags=re.I)
            condition=re.split(r'\s+(?:in primary care|by clinicians|according to|using|during|who)\b',condition,flags=re.I)[0].strip()
            result.append({'scopeId':'MS:'+_digest([anchor.evidence_key,quote])[:16],
                'evidenceKey':anchor.evidence_key,'sourceQuote':quote,'condition':condition})
    return result


def _management_current_context(rows, candidates, facts):
    result=[]
    for assessment in rows:
        candidate=candidates.get(assessment.risk_id)
        if candidate is None:
            continue
        certainty=getattr(assessment,'certainty',candidate.certainty)
        name=getattr(assessment,'name',candidate.name)
        claims=[claim for claim in assessment.claims if claim.role=='source_fact'
            and _valid_fact_quotes(claim.fact_quotes,facts,claim.fact_quotes)]
        ids=list(dict.fromkeys(fid for claim in claims for fid in claim.fact_quotes))
        observed=_same_observed_problem(candidate,claims,facts)
        if not ids or not (certainty=='self_reported' and any(facts[fid].kind=='self_reported' for fid in ids)
                or observed):
            continue
        result.append({'riskId':assessment.risk_id,'name':name,
            'certainty':'current_problem' if observed else certainty,
            'factIds':ids,'aliases':list(dict.fromkeys([name,*candidate.search_terms]))})
    return result


def _management_scope_schema():
    return {'sourceActions':[{'id':'SA1','principleId':'P1','role':'recommendation|intervention_effect|general_principle',
        'quote':'所选原则内逐字句','objectQuote':'实际原干预或一般原则对象','actionQuote':'推荐动作或效益谓词完整原片段',
        'populationQuote':'实际适用人群原文或空','requiredConditions':[{'quote':'必要条件原文',
            'factQuotes':{'真实或F短ID':'实际本人逐字事实'},'satisfied':True}],
        'scopeBindings':[{'scopeId':'输入原scopeId','sourceQuote':'输入逐字人群前提',
            'patientMatch':'matched|unknown|mismatch','factQuotes':{'真实或F短ID':'实际本人条件'},
            'currentProblemIds':['确实已存在且同一条件的输入状态ID'],'missingConditions':[],
            'eligibilityRole':'explicit_inclusion或空字符串',
            'eligibilityQuote':'仅泛称风险因素人口时：本篇POPULATION/ELIGIBILITY段完整正向纳入原句，不选排除句',
            'conditionQuote':'该完整纳入句中实际满足的具体条件逐字片段；无则空字符串'}]}],
        'clauseMatches':[{'clauseId':'C1','text':'输入子句','role':'context|action|implementation|purpose|self_monitoring|local_clinical',
            'sourceActionIds':['SA1'],'stepObjectQuote':'本人动作对象原片段',
            'sourceEffectQuote':'仅治疗效益purpose须绑定真实效益原句；普通就诊评估目的不需要',
            'factQuotes':{'真实或F短ID':'实际本人逐字事实'},'sameObjectTerms':[],
            'implementation':{'kind':'future_placement|reminder|schedule_continuity|execution_transition',
                'parentClauseIds':['本计划真实已支持action句ID'],'noPatientObservation':True,
                'noNewActionObject':True,'noNewMedicalEffectOrDose':True,'samePersonalPlan':True},
            'reason':'同一行动域适配理由；不以安全替代原文支持'}],
        'planningTargets':[{'text':'输入频次、时长或日常时点目标片段','kind':'frequency|duration|timing',
            'ordinarySafeTarget':True,'isSourceMeasurement':False,'reason':'普通同域规划理由'}]}


def _management_scope_rows(row, claim, facts, context, *, candidate=None, subject=None):
    """The same scope contract is used by initial and planning review."""
    assessment=row if hasattr(row,'certainty') and hasattr(row,'name') else candidate
    return {'claims':[claim],'assessment':assessment,'action':RiskAction(text=claim.text,target=assessment.name,
        fact_ids=list(claim.fact_quotes),evidence_keys=list(dict.fromkeys(a.evidence_key for a in claim.evidence)),
        claim_ids=[claim.claim_id]),'allowed':set(facts),'localClinical':False,'planningTargets':[],
        'principleAliases':{'P1':claim.claim_id},'currentProblemContext':context,
        'actualSubject': _subject_view(subject)}


_MANAGEMENT_OBJECTS = (
    ('diet', r'\bdiet\w*|饮食|膳食结构|膳食评估'),
    ('fruit', r'fruit\w*|kiwi\w*|prunes?|水果|果类|猕猴桃|西梅'),
    ('vegetable', r'vegetable\w*|蔬菜'),
    ('whole_grain', r'whole[ -]grains?|全谷|粗粮'),
    ('legume', r'legume\w*|beans?|豆类|杂豆'),
    ('fiber', r'\bfib(?:er|re)\b|膳食纤维'),
    ('carbohydrate', r'carbohydrate\w*|starch\w*|碳水|淀粉|精白米面|精制主食|主食'),
    ('sugar', r'added sugar\w*|\bsugars?\b|添加糖|精制糖|限糖|减糖'),
    ('alcohol', r'alcohol\w*|酒精|饮酒|啤酒|限酒|减酒'),
    ('fat', r'\bfats?\b|fatty|脂肪|油脂|油炸'),
    ('fluid', r'\bwater\b|\bfluids?\b|饮水|喝水|白水|补水|饮液|水量'),
    ('activity', r'physical activity|exercis\w*|walking|sedentary|身体活动|日常活动|运动|活动|快走|散步|步行|走动|久坐|起身'),
    ('sedentary_break', r'sedentary|interrupt.{0,25}(?:sitting|seated)|break.{0,20}sitting|久坐|连续坐|静坐|起身|站立'),
    ('aerobic_activity', r'aerobic|brisk walking|exercise training|快走|有氧|训练'),
    ('meal_timing', r'meal timing|late (?:dinner|eating)|nighttime (?:meal|snack)|晚餐|夜宵|进食时间|进餐时间'),
    ('sleep', r'\bsleep\w*|睡眠|入睡|上床'),
    ('stress', r'\bstress[ -]+(?:reduc\w*|manage\w*|relief)\b|\breduc\w*.{0,12}\bstress\b|'
        r'\brelax\w*|放松|压力(?:调节|管理|缓解)'),
    ('position', r'postur\w*|head.of.bed|lying down|recumbent|平卧|躺下|床头|体位'),
    ('weight', r'adiposity|body (?:weight|mass)|weight (?:loss|reduction|management)|waist|overweight|obesity|体重|腰围|肥胖|脂肪量'),
    ('clinical', r'clinical (?:assessment|evaluation)|screen\w*|referr\w*|follow.up|counsel\w*|就诊|门诊|专科|临床评估|筛查|复查|排查|转诊|转介|预约|咨询'),
)


def _management_objects(text, *, general_principle=False):
    result={name for name,pattern in _MANAGEMENT_OBJECTS if re.search(pattern,text,re.I)}
    if re.fullmatch(r'\s*(?:physical activity|exercise|身体活动|日常活动|运动)\s*',text,re.I):
        result.update({'activity','sedentary_break','aerobic_activity'})
    if general_principle and re.search(r'healthy (?:diet\w*|lifestyle)|dietary (?:guidance|patterns?)|'
            r'balanced diet|健康饮食|均衡膳食|饮食指导|健康生活方式',text,re.I):
        result.update({'diet','fruit','vegetable','whole_grain','legume','fiber','carbohydrate','fat','sugar','alcohol','meal_timing'})
        if re.search(r'healthy (?:dietary and )?lifestyle|健康生活方式',text,re.I):
            result.update({'activity','sedentary_break','aerobic_activity','weight'})
    if general_principle and (re.search(r'\blifestyle (?:modifications?|patterns?|changes?|interventions?)\b|生活方式(?:调整|改变|干预)',text,re.I)
            or re.search(r'(?:recommend\w*|advis\w*|should).{0,50}lifestyle (?:modifications?|changes?|interventions?)',text,re.I)):
        result.update({'diet','fruit','vegetable','whole_grain','legume','fiber','carbohydrate','fat','sugar','alcohol',
            'meal_timing','activity','sedentary_break','aerobic_activity','position','weight'})
    if general_principle and re.search(r'lifestyle therap\w*|therapeutic lifestyle changes|治疗性生活方式',text,re.I):
        result.update({'diet','fruit','vegetable','whole_grain','legume','fiber','carbohydrate','fat','sugar','alcohol',
            'meal_timing','activity','sedentary_break','aerobic_activity'})
    if general_principle and re.search(r'\blifestyle habits\b', text, re.I):
        result.update({'diet','fruit','vegetable','whole_grain','legume','fiber',
            'carbohydrate','fat','sugar','alcohol','meal_timing','activity',
            'sedentary_break','aerobic_activity'})
    if general_principle and re.search(r'\bweight (?:loss|reduction)\b|减重|体重管理',text,re.I):
        result.update({'weight','diet','activity','sedentary_break','aerobic_activity'})
    if general_principle and re.search(r'physical activity|exercise|身体活动|日常活动',text,re.I) and not re.search(
            r'sedentary|久坐|连续坐|静坐',text,re.I):
        result.update({'activity','sedentary_break','aerobic_activity'})
    return result


def _general_management_principle(text):
    # An actual general first-line recommendation can be phrased as
    # improving habits, rather than using the word "modifications". Narrow
    # therapeutic diets and negated/research-only statements grant no such
    # general duty. Entailment and patient/source-scope reviews remain needed.
    habits = (re.search(r'\blifestyle habits\b', text, re.I)
        and re.search(r'(?:should|recommend\w*|advis\w*).{0,60}improv\w*|'
            r'first[ -]line.{0,45}(?:includes?|is).{0,30}improv\w*', text, re.I)
        and not re.search(r'not recommend|no evidence|\b(?:ketogenic|low[ -]carbohydrate|'
            r'fluid[ -]restricted|renal|therapeutic diet|restricted diet)\b', text, re.I))
    if habits:
        return True
    return bool(re.search(r'healthy (?:diet\w*|lifestyle)|dietary (?:guidance|patterns?)|balanced diet|'
        r'healthy dietary and lifestyle|lifestyle therap\w*|therapeutic lifestyle changes|治疗性生活方式|'
        r'weight (?:loss|reduction).{0,35}(?:useful|recommend|advis|should)|健康饮食|均衡膳食|饮食指导|健康生活方式|'
        r'(?:recommend\w*|advis\w*|should).{0,50}lifestyle (?:modifications?|changes?|interventions?)|'
        r'lifestyle (?:modifications?|changes?|interventions?).{0,70}(?:recommend\w*|advis\w*|should)',text,re.I)
        or re.fullmatch(r'\s*lifestyle (?:modifications?|patterns?|changes?|interventions?)\s*|生活方式(?:调整|改变|干预)',text,re.I))


def _management_clauses(text):
    return [{'clauseId':'C'+str(index+1),'text':part.strip()} for index,part in enumerate(
        part for part in re.split(r'[，,；;。！？!?]|(?<!\d)\.(?!\d)',text) if part.strip())]


def _management_baseline_merges_frequencies(text, quotes, facts):
    """A frequency attached to one source predicate cannot qualify its neighbour."""
    frequency=re.compile(r'每(周|天|日|月)[^\d，,。；;]{0,12}(\d+)[^，,。；;]{0,3}(次|天)')
    mentions=list(frequency.finditer(text));mentioned={match.groups() for match in mentions}
    if not mentioned:
        return False
    for fid in quotes:
        fact=facts.get(fid)
        if fact is None or fact.kind!='self_reported':
            continue
        parts=[part.strip() for part in re.split(r'[，,。；;]',fact.value.split('回答：')[-1]) if part.strip()]
        for source in parts:
            if not mentioned.intersection(match.groups() for match in frequency.finditer(source)):
                continue
            for neighbour in parts:
                if neighbour==source or re.search(r'\d|不|无|未|否认',neighbour):
                    continue
                noun=re.sub(r'经常|通常|常常|常|选择|选用|食用|饮用|喜欢|偏好|往往|目前|偶尔|吃|喝','',neighbour).strip()
                assigned=[re.split(r'和|以及|并|[，,。；;]|、(?=(?:常|通常|经常|偶尔|多为|容易|需要))',text[match.end():],maxsplit=1)[0]
                    for match in mentions if match.groups() in {entry.groups() for entry in frequency.finditer(source)}]
                if 2<=len(noun)<=12 and any(noun in tail for tail in assigned) and noun not in source:
                    return True
    return False


def _management_personal_step(text, target, quotes, facts, problem):
    """Remove an unsupported generated preface; never rewrite patient data."""
    parts=_management_clauses(text)
    patient={fid:facts[fid] for fid in quotes if fid in facts}
    retained=[part['text'] for part in parts if not (re.match(r'^(?:针对|结合|在现有|目前|原有)',part['text'])
        and (_management_baseline_merges_frequencies(part['text'],quotes,facts)
            or _unsupported_food_context(part['text'],patient)))]
    cleaned=text if len(retained)==len(parts) else '，'.join(retained)+'。'
    # The goal is not a licence to invent an existing dietary exposure. The
    # actual medical problem is a truthful fallback for generated metadata.
    asserted_target=re.sub(r'减少|限制|避免|控制|降低','',str(target))
    if _unsupported_food_context(asserted_target,patient):
        target=problem
    return cleaned,target


def _adopted_plan_fact_quotes(assessment, quotes, facts):
    """Bind an exact adopted symptom span inside its original questionnaire.

    The organizer creates a focused symptom fact while authors may cite the
    full original answer. Both are real source records. A literal, substantial
    span from this same risk's already approved observation may bridge that
    locator difference; numeric values, generic yes/no and similar diseases
    cannot. Every added quote is still independently reviewed for the plan.
    """
    result=dict(quotes)
    raw=[(facts[fid],quote) for fid,quote in quotes.items() if fid in facts
        and facts[fid].kind=='self_reported' and isinstance(quote,str) and quote in facts[fid].value]
    for claim in assessment.claims:
        if claim.role!='source_fact':
            continue
        for fid,quote in claim.fact_quotes.items():
            fact=facts.get(fid)
            if (fid not in assessment.support_fact_ids or fact is None or fact.kind!='self_reported'
                    or len(quote.strip())<8 or quote not in fact.value):
                continue
            if any(quote in literal and not (fact.date and original.date and fact.date!=original.date)
                    for original,literal in raw):
                result.setdefault(fid,quote)
    return result


def _management_review_batches(payload):
    """Bound the existing review stage's input and independent JSON output."""
    batches=[];items=[];clauses=0;size=0
    for item in payload['items']:
        cost=len(json.dumps(item,ensure_ascii=False));count=len(item.get('clauses',[]))
        if items and (len(items)>=3 or clauses+count>12 or size+cost>30000):
            batches.append({**payload,'items':items});items=[];clauses=0;size=0
        items.append(item);clauses+=count;size+=cost
    if items:
        batches.append({**payload,'items':items})
    return batches


def _planning_targets(text, proposed):
    """New ordinary frequency/duration proposals are not source measurements."""
    if not isinstance(proposed,list):
        return []
    result=[]
    instructions=[part['text'] for part in _management_clauses(text)
        if _action_is_executable(part['text'])
        and not (re.match(r'^在(?:现有|已有|原有|目前).{0,100}基础上$', part['text'])
            and not re.search(r'增加|减少|提前|延后|改为|替换|调整', part['text']))]
    for target in proposed:
        if (not isinstance(target,dict) or target.get('kind') not in {'frequency','duration','timing'}
                or not isinstance(target.get('text'),str)
                or not re.search(r'(?:\d+|[一二两三四五六七八九十百]+).*(?:次|天|日|周|分钟|小时|点|时|minutes?|hours?|days?|weeks?|times?)',target['text'],re.I)
                or re.search(r'毫升|毫克|千克|公斤|\b(?:ml|mg|kg|kcal)\b|克|百分|%',target['text'],re.I)):
            continue
        literal=target['text']
        if literal not in text:
            # Recover only a unique planning-slot spelling, not a new dose
            # or a different frequency. The full actual step is reviewed.
            tokens=re.findall(r'\d+(?:\.\d+)?|[一二两三四五六七八九十百]+|每|次|分钟|小时|天|日|周',literal)
            pattern=r'(?:安排|约|至少|不超过|\s)*'.join(map(re.escape,tokens))
            matches={match.group() for match in re.finditer(pattern,text)} if tokens else set()
            if len(matches)!=1:
                continue
            literal=next(iter(matches))
        if not any(literal in part for part in instructions):
            continue
        result.append({'text':literal,'kind':target['kind']})
    # Ordinary execution slots are selected from the actual step, then
    # reviewed as proposals. Dates, intake quantities and drug doses never
    # enter this grammar. This also covers a writer omitting one time slot.
    patterns=(('frequency',r'每(?:周|天|日)(?:约|至少|不超过|安排)?\s*[\d一二两三四五六七八九十]+\s*次'),
        ('duration',r'(?:每次|每隔|连续|持续)?\s*\d+(?:\s*[–—~～至-]\s*\d+)?\s*(?:分钟|小时|周)'),
        ('timing',r'(?<!\d)\d{1,2}(?::\d{2}(?:点|时)?|\s*[–—~～至-]\s*\d{1,2}\s*(?:点|时))'))
    for kind,pattern in patterns:
        for part in instructions:
            for match in re.finditer(pattern,part):
                literal=match.group().strip()
                if not any(literal in item['text'] or item['text'] in literal for item in result):
                    result.append({'text':literal,'kind':kind})
    return result


def _management_fact_quotes(value, row, facts):
    """Resolve an exact server-owned fact view back to its literal value.

    Reviewers sometimes quote the displayed name/value/unit/range/status
    together. Only that ordered view of this exact fact is recoverable;
    an added diagnosis, changed unit/range/status or arbitrary numeric
    containment remains invalid. The underlying fact is never modified.
    """
    if not isinstance(value,dict):
        return {}
    def compact(text):
        return re.sub(r'[\s，,；;：:（）()\[\]【】]+','',str(text))
    result={}
    for identifier,quote in value.items():
        fid=row.get('factAliases',{}).get(identifier,identifier)
        fact=facts.get(fid)
        if fact is not None and isinstance(quote,str) and quote not in fact.value:
            prefix=compact(fact.name)+compact(fact.value)
            if prefix and compact(fact.name) and compact(fact.value):
                pattern=re.escape(prefix)
                if fact.unit:
                    pattern+='(?:'+re.escape(compact(fact.unit))+')?'
                if fact.reference_range:
                    pattern+='(?:(?:参考范围|参考区间|参考值|参考)?'+re.escape(compact(fact.reference_range))+')?'
                if fact.status:
                    pattern+='(?:'+re.escape(compact(fact.status))+')?'
                if re.fullmatch(pattern,compact(quote)):
                    quote=fact.value
        result[fid]=quote
    return result


def _ordinary_diet_cooking_choice(text, match, row, facts, linked_sources):
    """A future cooking choice may instantiate a reviewed general diet rule.

    This is not an alias from a fruit/other specific intervention to all food.
    It requires an actual supplied dietary habit and a linked, genuinely
    general diet principle; the complete step still undergoes normal review.
    """
    if (not re.match(r'^(?:点外卖时|外食时|选择菜品时)?(?:优先|尽量|建议|可以|可)?(?:选择|选用|选|改选|改用)',text)
            or not re.search(r'蒸|煮|炖',text)
            or not re.search(r'外卖|菜品|菜肴|烹调|烹饪',text)
            or re.search(r'治疗|治愈|逆转|预防|降低|改善|降压|降脂|疗效|补剂|药物',text)
            or not any(source.get('principleLevel')=='general' and 'diet' in source.get('objects',[])
                and re.search(r'healthy (?:diet\w*|lifestyle)|dietary (?:guidance|patterns?)|balanced diet|'
                    r'健康饮食|均衡膳食|饮食指导|健康生活方式',str(source.get('quote')or''),re.I)
                for source in linked_sources)):
        return False
    bound=_management_fact_quotes(match.get('factQuotes'),row,facts)
    return any(fid in row['allowed'] and facts[fid].kind=='self_reported'
        and quote in facts[fid].value and re.search(r'外卖|菜|餐|主食|蔬果|全谷|饮食|膳食',quote)
        and not re.search(r'未提供|未填写|不清楚|不知道|不详',quote)
        for fid,quote in bound.items())


def _fruit_only_management_repair(row):
    """Constrain the next author attempt; this never approves a shortened step."""
    anchors=[anchor.quote for claim in row['claims'] for anchor in claim.evidence]
    if (anchors and all(not _general_management_principle(quote) for quote in anchors)
            and any('fruit' in _management_objects(quote) for quote in anchors)):
        return {'nextDraftScope':'single_ordinary_fruit_step','allowedBehaviorObjects':['fruit'],
            'instruction':'下一次仅重写一条与已选原文水果对象一致的普通水果行动，仍结合本人原有摄入与排便事实；'
                '不混入蔬菜、全谷物、饮水、如厕或运动命令，不自行补品种、剂量或默认频次。'
                '提交完整新步骤并重新逐句独立审核，不截断旧稿后沿用旧审批。'}
    return {}


def _single_fruit_step_scope(text, principles):
    if not _fruit_only_management_repair({'claims':principles}):
        return False
    objects=_management_objects(text)
    return 'fruit' in objects and objects <= {'fruit','diet'}


def _ordinary_current_clinical_plan(row, review, facts):
    """An independently reviewed visit/evaluation, not a cited treatment.

    Clinical intent is supplied by the existing independent review. Source
    and patient facts still establish a real current problem; no wording
    prefix, efficacy quote or unrelated lifestyle citation is required.
    """
    if not all(review.get(key)is True for key in (
            'valid','patientApplicable','clinicalEvaluationAppropriate','noUnsupportedTestOrSpecialty',
            'concreteLowRiskStep','noNewMedicalClaimOrDose')) or review.get('missingConditions')!=[]:
        return False
    assessment=row['assessment'];action=row['action']
    primary=set(assessment.support_fact_ids)&set(action.fact_ids)
    if not any(fid in facts and facts[fid].kind in {'clinical','self_reported'} for fid in primary):
        return False
    text=action.text
    ordinary=text
    for span in _ordinary_clinical_decision_spans(text):
        ordinary=ordinary.replace(span,' ')
    if re.search(r'确诊|诊断为|已患|患有|治愈|疗效|预防|缓解|降低|提高|改善|治疗|'
            r'服用|口服|注射|药物|处方|补剂|益生菌|内镜|胃镜|肠镜|活检|手术',ordinary,re.I):
        return False
    quotes={fid:facts[fid].value for fid in action.fact_ids if fid in facts}
    return not _low_risk_management_step_errors(text,quotes,[],facts,
        planning_targets=row.get('planningTargets',[]),medical_entities=[assessment.name])


def _explicit_management_population_condition(requirement, binding, source, current_context, bound, facts):
    """A declared risk-factor population may name exact eligible subgroups.

    Accept only a full positive sentence in this original's POPULATION or
    ELIGIBILITY section, plus its literal included condition and a same-
    condition current-problem witness. Do not turn an exclusion, risk-factor
    list in BACKGROUND, or a different disease cohort into eligibility.
    """
    if not re.search(r'risk factors?|风险因素|危险因素', requirement['condition'], re.I):
        return False
    if binding.get('eligibilityRole') != 'explicit_inclusion':
        return False
    quote, condition = binding.get('eligibilityQuote'), binding.get('conditionQuote')
    if not isinstance(quote, str) or not isinstance(condition, str) or len(condition.strip()) < 4:
        return False
    sections = re.findall(r'(?i:POPULATION|TARGET POPULATION|ELIGIBILITY)\s*:\s*(.*?)'
        r'(?=\b[A-Z][A-Z ]{2,}:|\Z)', source['sourceText'], re.S)
    sentences = [sentence.strip() for section in sections
        for sentence in re.split(r'(?<=[.!?])\s+(?=[A-Z])', section)]
    if quote.strip() not in sentences or condition not in quote or re.search(
            r'not included|excluded|does not apply|not eligible|排除|不纳入|不适用', quote, re.I):
        return False
    from .evaluation_evidence_plan import _mentions
    terms = _concept_forms([condition], quote)
    current_ids = set(binding.get('currentProblemIds', []))
    return any(context['riskId'] in current_ids and set(context['factIds']) & set(bound)
        and _mentions(' '.join(context['aliases']), terms)
        and any(fid in facts and facts[fid].kind in {'clinical', 'self_reported'} for fid in context['factIds'])
        for context in current_context)


def _management_subject_eligibility(condition, row):
    """A typed request subject can satisfy only a pure age/sex condition.

    It is not a patient fact ID and cannot supply diagnoses, symptoms or
    treatment prerequisites. Source population and all medical criteria keep
    their original, separately reviewed bindings.
    """
    actual = _subject_view(row.get('actualSubject'))
    age = actual.get('age')
    if isinstance(age, str) and re.fullmatch(r'\d{1,3}', age.strip()):
        age = int(age)
    if type(age) is not int or not 0 <= age <= 130:
        age = None
    sex = actual.get('sex') if isinstance(actual.get('sex'), str) else None
    supplied = condition.get('subjectEligibility')
    if isinstance(supplied, dict) and supplied:
        if (set(supplied) - {'age','sex'}
                or 'age' in supplied and (age is None or supplied['age'] != age)
                or 'sex' in supplied and (sex is None or supplied['sex'] != sex)):
            return None
    else:
        quotes = condition.get('factQuotes')
        if not isinstance(quotes, dict) or set(quotes) != {'subject'} or age is None:
            return None
        expected = {_key(f'{age}岁'), _key(f'{age}岁{sex or ""}')}
        if _key(quotes['subject']) not in expected:
            return None
    text = str(condition.get('quote') or '').strip()
    pure_sex = re.fullmatch(r'(?:(adult|adults|成年|成人)\s*)?'
        r'(women|woman|female|females|men|man|male|males|女性|男性)', text, re.I)
    if pure_sex:
        normalized = str(sex or '').casefold()
        actual_sex = ('female' if normalized in {'女','女性','female','f','woman','women'}
            else 'male' if normalized in {'男','男性','male','m','man','men'} else None)
        expected_sex = ('female' if pure_sex[2].casefold() in {
            'women','woman','female','females','女性'} else 'male')
        if actual_sex != expected_sex or pure_sex[1] and (age is None or age < 18):
            return None
        return {'age': age, 'sex': sex}
    if re.fullmatch(r'adults?|成人|成年人', text, re.I):
        if age is None or age < 18:
            return None
        return {'age': age, 'sex': sex}
    matched = re.fullmatch(r'(?:adults?\s+)?aged?\s+(\d{1,3})\s*(?:-|–|—|to)\s*(\d{1,3})(?:\s+years?)?', text, re.I)
    if matched and age is not None and int(matched[1]) <= age <= int(matched[2]):
        return {'age': age, 'sex': sex}
    return None



def _existing_stress_coping_continuation(text, match, row, facts, source_actions):
    """Maintain an actual coping habit without creating an exercise prescription."""
    if not re.fullmatch(r'(?:可|可以)?(?:继续|维持|保持)(?:用|通过)?散步(?:来|以)?放松(?:的习惯|方式)?', text):
        return False
    if not any('stress' in source.get('objects', []) for source in source_actions):
        return False
    bound = _management_fact_quotes(match.get('factQuotes'), row, facts)
    if not _valid_fact_quotes(bound, facts, row['allowed']):
        return False
    return any(facts[fid].kind == 'self_reported' and re.search(r'散步(?:来|以)?放松', quote)
        and not re.search(r'从不|不会|无法|没有|未能|很少', quote)
        for fid, quote in bound.items())



def _unchanged_sleep_baseline(text, bound, facts):
    matched = re.fullmatch(r'保持(?:现有|目前)?起床时间(?:约|为)?(\d{1,2})点(\d{1,2})分不变', text)
    if not matched:
        return False
    clock = matched[1] + '点' + matched[2] + '分'
    return any(facts[fid].kind == 'self_reported' and clock in quote and '起床' in quote
        for fid, quote in bound.items() if fid in facts)



def _planning_implementation_witness(text, match, expected, all_matches, source_actions, row, facts):
    """A logistical subclause inherits only its reviewed parent action duty."""
    binding=match.get('implementation')
    if (not isinstance(binding,dict) or binding.get('kind') not in {
            'future_placement','reminder','schedule_continuity','execution_transition'}
            or binding.get('noPatientObservation') is not True
            or binding.get('noNewActionObject') is not True
            or binding.get('noNewMedicalEffectOrDose') is not True
            or binding.get('samePersonalPlan') is not True):
        return False
    parents=binding.get('parentClauseIds')
    if (not isinstance(parents,list) or not parents or len(parents)!=len(set(parents))
            or match.get('clauseId') in parents):
        return False
    by_id={item.get('clauseId'):item for item in all_matches if isinstance(item,dict)}
    parent_rows=[by_id.get(identifier,{}) for identifier in parents]
    if any(parent.get('role')!='action' or parent.get('text')!=expected.get(parent.get('clauseId'))
            or not parent.get('sourceActionIds') for parent in parent_rows):
        return False
    links={identifier for parent in parent_rows for identifier in parent.get('sourceActionIds',[])}
    requested=set(match.get('sourceActionIds',[]))
    if not requested or not requested<=links or not links<=source_actions.keys():
        return False
    allowed={obj for key in requested for obj in source_actions[key]['objects']}
    # Known genuinely new behavior domains or clinical effects never become logistics.
    objects=_management_objects(text)
    if (binding['kind']=='future_placement' and 'meal_timing' in objects
            and not re.search(r'提前|延后|延迟|间隔|禁食|空腹|增加|减少|避免|改成|改为|\d',text)
            and allowed & {'fruit','vegetable','whole_grain','diet','carbohydrate'}):
        # Naming a meal as the location of a food step does not change its timing.
        objects=objects-{'meal_timing'}
    if objects and not objects<=allowed:
        return False
    if re.search(r'确诊|诊断|治愈|疗效|预防|缓解|降低|提高|改善|治疗|药物|服用|补剂|益生菌|'
            r'\d+(?:\.\d+)?\s*(?:mg|g|ml|毫克|克|毫升)',text,re.I):
        return False
    if any(not _valid_fact_quotes(_management_fact_quotes(parent.get('factQuotes'),row,facts),
            facts,row['allowed']) for parent in parent_rows):
        return False
    return True


def _management_scope_witness(row, review, facts, originals):
    """Verify original action objects for every personal action clause.

    Safety and semantic review remain necessary. These literal witnesses stop
    a true fruit effect from licensing water/activity, or a carbohydrate
    effect from licensing meal timing or body position.
    """
    from .evaluation_evidence_plan import management_principle_errors
    claims={claim.claim_id:claim for claim in row['claims']}
    principle_aliases = row.get('principleAliases', {})
    if (not isinstance(principle_aliases, dict)
            or len(principle_aliases) != len(claims)
            or set(principle_aliases.values()) != set(claims)):
        return None
    # A personal step inherits the full source's real population/method
    # prerequisites. Safe wording and a true recommendation are insufficient
    # when the selected study's patient group is not this patient's group.
    scope_reviews = review.get('sourceScopeReviewsByPrinciple', {})
    from .evaluation_source_scope import source_clinical_condition_witnesses
    conditions = source_clinical_condition_witnesses(row.get('_document'), subject=row.get('actualSubject'))
    for alias, identifier in principle_aliases.items():
        claim = claims.get(identifier)
        if claim is None:
            return None
        raw = scope_reviews.get(alias, {}) if isinstance(scope_reviews, dict) else {}
        restored = {}
        for key, value in raw.items() if isinstance(raw, dict) else ():
            if not isinstance(value, dict):
                continue
            matches = []
            for match in value.get('premiseMatches', []):
                if not isinstance(match, dict):
                    continue
                if (not isinstance(match.get('factIds', []), list)
                        or any(not isinstance(fid, str) for fid in match.get('factIds', []))
                        or not isinstance(match.get('factQuotes', {}), dict)):
                    return None
                aliases = row.get('factAliases', {})
                matches.append({**match,
                    'factIds': [aliases.get(fid, fid) for fid in match.get('factIds', [])],
                    'factQuotes': {aliases.get(fid, fid): quote for fid, quote in
                        match.get('factQuotes', {}).items()}})
            restored[key] = {**value, 'premiseMatches': matches}
        if _bind_risk_source_scope(claim, {'sourceScopeReviews': restored}, facts, originals,
                subject=row.get('actualSubject'), verified_conditions=conditions):
            return None
    def restored_quotes(value):
        return _management_fact_quotes(value,row,facts)
    def named_condition_supported(requirement,bound,current_ids):
        source=originals[requirement['evidenceKey']]
        terms=_concept_forms([requirement['condition']],str(source.get('title')or'')+' '+source['sourceText'])
        from .evaluation_evidence_plan import _mentions
        for context in row.get('currentProblemContext',[]):
            if (context['riskId'] in current_ids and set(context['factIds'])&set(bound)
                    and _mentions(' '.join(context['aliases']),terms)):
                return True
        return any(facts[fid].kind in {'clinical','self_reported'}
            and not re.search(r'父亲|母亲|家族|亲属|否认|未患|未诊断|没有|未见|无明确',facts[fid].name+' '+facts[fid].value)
            and _mentions(facts[fid].name+' '+facts[fid].value,terms)
            and not _reference_normal_numeric(facts[fid]) for fid in bound)
    def clinical_condition_supported(condition, bound):
        from .evaluation_catalog import FIXED_DISEASE_ALIASES
        from .evaluation_evidence_plan import _mentions
        text=condition['quote']
        if re.search(r'this disease|the disease|该病|本病',text,re.I):
            text += ' ' + str(condition.get('sourceTitle') or '')
        if not re.search(r'patients? with|diagnosed with|患有|确诊|已诊断',text,re.I) or re.search(
                r'family|parent|relative|家族|亲属',text,re.I):
            return True
        for code,aliases in FIXED_DISEASE_ALIASES.items():
            if not _mentions(text,aliases):
                continue
            if row['assessment'].certainty in {'confirmed','current_problem','self_reported'} and (
                    _risk_identity(row['assessment'].name)==code):
                continue
            if not any(_mentions(facts[fid].name+' '+facts[fid].value,aliases)
                    and not re.search(r'父亲|母亲|父母|家族|亲属|否认|未患|没有|未诊断',facts[fid].name+' '+facts[fid].value)
                    and (re.search(r'确诊|诊断|患有|known|diagnosed',facts[fid].value,re.I)
                        or _risk_identity(facts[fid].name)==code and facts[fid].value not in {'否','无','0','阴性','未检出'})
                    for fid in bound):
                return False
        return True
    source_actions={}
    for raw in review.get('sourceActions',[]) if isinstance(review.get('sourceActions'),list) else []:
        if not isinstance(raw,dict) or not isinstance(raw.get('id'),str) or raw['id'] in source_actions:
            return None
        principle_id=row.get('principleAliases',{}).get(raw.get('principleId'),raw.get('principleId'))
        claim=claims.get(principle_id)
        quote,obj=raw.get('quote'),raw.get('objectQuote')
        action_quote=raw.get('actionQuote')
        # The provider's chosen original is already a trusted literal anchor.
        # A reviewer copying its Chinese principle translation must not replace
        # that anchor, or erase an otherwise valid plan. Resolve only within
        # this exact principle, never by searching unrelated pooled papers.
        if claim is not None and len(claim.evidence)==1:
            trusted=claim.evidence[0]
            if not isinstance(quote,str) or quote not in trusted.quote:
                quote=trusted.quote
            if not isinstance(action_quote,str) or action_quote not in quote:
                action_quote=quote
            if not isinstance(obj,str) or obj not in quote:
                if _general_management_principle(quote):
                    obj=quote
                else:
                    requested=_management_objects(str(obj or ''))
                    spans=[match.group() for domain,pattern in _MANAGEMENT_OBJECTS if domain in requested
                        for match in re.finditer(pattern,quote,re.I)]
                    obj=max(spans,key=len) if spans else ''
        if (claim is None or raw.get('role') not in {'recommendation','intervention_effect','general_principle'}
                or not isinstance(quote,str) or len(quote.strip())<8 or not isinstance(obj,str)
                or len(obj.strip())<2 or obj not in quote or not isinstance(action_quote,str)
                or len(action_quote.strip())<8 or action_quote not in quote):
            return None
        anchor=next((anchor for anchor in claim.evidence if quote in anchor.quote),None)
        if anchor is None or quote not in originals[anchor.evidence_key]['sourceText']:
            return None
        trial=bool(re.search(r'randomi[sz]ed|\btrial\b',str(originals[anchor.evidence_key].get('title')or'')
            +' '+str(originals[anchor.evidence_key].get('studyType')or''),re.I))
        if management_principle_errors(action_quote,human_trial=trial):
            return None
        general=(raw['role'] in {'general_principle','recommendation'}
            and _general_management_principle(quote))
        objects=_management_objects(obj,general_principle=general)
        if raw['role']=='recommendation' and not general:
            action_objects=_management_objects(action_quote)
            if action_objects:
                objects=objects&action_objects if objects else action_objects
        population=raw.get('populationQuote','')
        source_title=str(originals[anchor.evidence_key].get('title')or'')
        requirements=[item for item in _management_source_scope_requirements(claim,originals)
            if item['evidenceKey']==anchor.evidence_key]
        if not isinstance(population,str) or population and population not in (
                source_title+' '+originals[anchor.evidence_key]['sourceText']):
            population=requirements[0]['sourceQuote'] if requirements else ''
        scoped_population=re.search(r'patients? with|diagnosed with|women with|children with|患有|确诊',
            source_title+' '+quote,re.I)
        if scoped_population and (not population or not clinical_condition_supported(
                {'quote':population,'sourceTitle':source_title},claim.fact_quotes)):
            return None
        conditions=raw.get('requiredConditions',[])
        if not isinstance(conditions,list):
            return None
        normalized_conditions=[]
        for condition in conditions:
            bound=restored_quotes(condition.get('factQuotes')) if isinstance(condition,dict) else {}
            if (isinstance(condition,dict) and isinstance(condition.get('quote'),str)
                    and condition['quote'] not in (source_title+' '+originals[anchor.evidence_key]['sourceText'])):
                # Recover a translated duplicate only via a controlled
                # medical-object alias that literally exists in this source.
                # Full sourceScopes below still require the real patient
                # condition; an unrecognised treatment prerequisite fails.
                source_text=source_title+' '+originals[anchor.evidence_key]['sourceText']
                spans={match.group() for term in _concept_forms([condition['quote']],source_text)
                    if term.isascii() and len(term)>=3 for match in re.finditer(
                        r'(?<![A-Za-z0-9])'+re.escape(term)+r'(?![A-Za-z0-9])',source_text,re.I)}
                if not spans:
                    return None
                condition={**condition,'quote':max(spans,key=len)}
            subject_scope = _management_subject_eligibility(condition, row) if isinstance(condition, dict) else None
            subject_only = bool(subject_scope and set(bound) == {'subject'})
            if subject_only:
                bound = {}  # A request subject is typed scope, never a synthetic fact ID.
            if (not isinstance(condition,dict) or condition.get('satisfied') is not True
                    or not isinstance(condition.get('quote'),str)
                    or condition['quote'] not in (str(originals[anchor.evidence_key].get('title')or'')+' '+originals[anchor.evidence_key]['sourceText'])
                    or not (_valid_fact_quotes(bound,facts,row['allowed']) or subject_scope and not bound)
                    or not clinical_condition_supported({**condition,'sourceTitle':source_title},bound)):
                return None
            normalized_conditions.append({**condition,'factQuotes':bound,
                **({'subjectEligibility': subject_scope} if subject_scope else {})})
        scope_bindings=raw.get('scopeBindings',[])
        if not isinstance(scope_bindings,list) or len(scope_bindings)!=len(requirements):
            return None
        normalized_scopes=[]
        for requirement in requirements:
            matches=[item for item in scope_bindings if isinstance(item,dict) and item.get('scopeId')==requirement['scopeId']]
            binding=matches[0] if len(matches)==1 else {}
            bound=restored_quotes(binding.get('factQuotes'))
            current_ids=binding.get('currentProblemIds',[])
            if (binding.get('patientMatch')!='matched'
                    or binding.get('missingConditions')!=[] or not isinstance(current_ids,list)
                    or not _valid_fact_quotes(bound,facts,row['allowed'])
                    or not (named_condition_supported(requirement,bound,current_ids)
                        or _explicit_management_population_condition(requirement, binding,
                            originals[requirement['evidenceKey']], row.get('currentProblemContext', []), bound, facts))):
                return None
            normalized_scopes.append({**binding,'sourceQuote':requirement['sourceQuote'],'factQuotes':bound})
        source_actions[raw['id']]={**raw,'principleId':principle_id,'quote':quote,'objectQuote':obj,
            'actionQuote':action_quote,'populationQuote':population,'requiredConditions':normalized_conditions,
            'scopeBindings':normalized_scopes,'principleLevel':'general' if general else 'specific',
            'evidenceKey':anchor.evidence_key,'objects':sorted(objects)}
    expected={clause['clauseId']:clause['text'] for clause in _management_clauses(row['action'].text)}
    matches=review.get('clauseMatches')
    if not isinstance(matches,list) or len(matches)!=len(expected):
        return None
    witnessed=[];seen=set();used_source_actions=set()
    for match in matches:
        if not isinstance(match,dict) or match.get('clauseId')not in expected or match['clauseId']in seen:
            return None
        seen.add(match['clauseId']);text=expected[match['clauseId']]
        if match.get('text')!=text or not isinstance(match.get('reason'),str)or len(match['reason'].strip())<4:
            return None
        role=match.get('role');links=match.get('sourceActionIds',[])
        if not isinstance(links,list) or any(key not in source_actions for key in links):
            return None
        if role=='implementation':
            if not _planning_implementation_witness(text, match, expected, matches, source_actions, row, facts):
                return None
            used_source_actions.update(links)
        elif role=='context':
            # Baseline context cannot hide an instruction behind a safe flag.
            bound=restored_quotes(match.get('factQuotes'))
            if re.search(r'保持|维持|增加|减少|替换|避免|提前|延后|起身|预约|就诊|评估',text) and not (
                    _valid_fact_quotes(bound, facts, row['allowed'])
                    and _unchanged_sleep_baseline(text, bound, facts)):
                return None
            if (_management_baseline_merges_frequencies(text,bound,facts)
                    or _unsupported_food_context(text,{fid:facts[fid] for fid in bound if fid in facts})):
                return None
            if not _valid_fact_quotes(bound,facts,row['allowed']):
                transition=bool(re.fullmatch(r'(?:再|随后|之后|接着)(?:回到|继续|恢复)(?:日常)?(?:工作|学习|原有活动|日常活动)',text))
                # A source-derived first-line/principle preface is not a
                # patient observation. It remains tied to this exact chosen
                # sourceAction, while patient conditions are checked above.
                if not transition and (bound or not links or re.search(r'本人|用户|自述|目前|本次|您的',text)):
                    return None
                used_source_actions.update(links)
        elif role=='local_clinical':
            if not row['localClinical'] or links:
                return None
        elif role=='self_monitoring':
            if not re.search(r'记录|观察|留意|对照|monitor|record',text,re.I) or re.search(
                    r'检测|检验|化验|内镜|活检|服用|补剂|药物',text):
                return None
        elif role=='purpose':
            effect=match.get('sourceEffectQuote')
            weight_context=(bool(re.fullmatch(r'配合(?:体重|腰围)(?:[和与](?:体重|腰围))?管理',text))
                and any('weight' in source_actions[key]['objects'] for key in links))
            ordinary_purpose=(not re.search(r'治愈|疗效|预防|缓解|降低|提高|改善|治疗|减少',text)
                and (re.match(r'^(?:作为|围绕|以便|用于|明确|了解|评估|判断|讨论|观察|记录)',text)
                    or weight_context))
            clinical_purpose=(row['localClinical'] and not links and (
                _ordinary_current_clinical_plan(row,review,facts) or any(
                    text in span or span in text for span in _ordinary_clinical_decision_spans(row['action'].text))))
            if clinical_purpose or ordinary_purpose and (row['localClinical'] or claims):
                used_source_actions.update(links)
            elif (not links or not re.match(r'^(?:以|使|有助于|从而|目标|用于)',text)
                    or not isinstance(effect,str) or len(effect.strip())<8
                    or not any(effect in source_actions[key]['quote'] for key in links)):
                return None
            else:
                used_source_actions.update(links)
        elif role=='action':
            if row['localClinical'] and not links:
                # Appropriate specialty/evaluation/goal is independently
                # reviewed locally. It is not a cited treatment effect.
                if not re.search(r'就诊|科|门诊|评估|讨论|检查|复查|记录|监测|带上|携带',text):
                    return None
                witnessed.append({**match,'factQuotes':restored_quotes(match.get('factQuotes'))})
                continue
            if not links:
                return None
            previous_actions={key for clause in witnessed if clause.get('role')=='action'
                for key in clause.get('sourceActionIds',[])}
            used_source_actions.update(links)
            objects=_management_objects(text)
            allowed_objects={obj for key in links for obj in source_actions[key]['objects']}
            if not objects and _ordinary_diet_cooking_choice(text,match,row,facts,
                    [source_actions[key] for key in links]):
                objects={'diet'}
            # A logistical continuation inherits only the immediately
            # reviewed action duty, never a new biological object/effect.
            if (not objects and set(links)<=previous_actions and re.fullmatch(
                    r'(?:可|可以|也可)?(?:分次|分段|逐步|渐进地?)(?:完成|进行|实施)',text)):
                witnessed.append({**match,'factQuotes':restored_quotes(match.get('factQuotes'))})
                continue
            if ('activity' in objects and 'activity' not in allowed_objects
                    and _existing_stress_coping_continuation(text, match, row, facts,
                        [source_actions[key] for key in links])):
                objects = objects - {'activity'}
            if objects and not objects<=allowed_objects:
                return None
            if ('sedentary_break' in allowed_objects and 'aerobic_activity' not in allowed_objects
                    and 'activity' in objects and 'sedentary_break' not in objects):
                return None
            object_quote=match.get('stepObjectQuote')
            if not isinstance(object_quote,str) or len(object_quote.strip())<2 or object_quote not in text:
                # This is the Chinese step side. The original side has
                # already been bound above. Known objects may be recovered
                # from this exact clause; no bilingual string equality is
                # required and unknown objects still need both-side aliases.
                if objects:
                    object_quote=text
                else:
                    aliases=match.get('sameObjectTerms',[])
                    local=[term for term in aliases if isinstance(term,str) and len(term.strip())>=2 and term in text]
                    foreign=[term for term in aliases if isinstance(term,str) and len(term.strip())>=2
                        and any(term in source_actions[key]['objectQuote'] for key in links)]
                    if not local or not foreign:
                        return None
                    object_quote=max(local,key=len)
            if not objects and not _management_objects(object_quote):
                # Unknown objects require an explicit bilingual equivalence
                # witness; a general validity flag is not enough.
                aliases=match.get('sameObjectTerms',[])
                if (not isinstance(aliases,list) or len(aliases)<2 or not any(term in text for term in aliases
                        if isinstance(term,str) and len(term.strip())>=2) or not any(any(term in source_actions[key]['objectQuote']
                            for term in aliases if isinstance(term,str) and len(term.strip())>=2) for key in links)):
                    return None
            match={**match,'stepObjectQuote':object_quote}
        else:
            return None
        witnessed.append({**match,'factQuotes':restored_quotes(match.get('factQuotes'))})
    if claims and {source_actions[key]['principleId'] for key in used_source_actions} != set(claims):
        return None
    if {source_actions[key]['evidenceKey'] for key in used_source_actions}!={
            anchor.evidence_key for claim in claims.values() for anchor in claim.evidence}:
        return None
    targets=row.get('planningTargets',[])
    target_reviews=review.get('planningTargets',[])
    if targets:
        if not isinstance(target_reviews,list) or len(target_reviews)!=len(targets):
            return None
        for target in targets:
            found=[item for item in target_reviews if isinstance(item,dict) and item.get('text')==target['text']
                and item.get('kind')==target['kind']]
            if (len(found)!=1 or found[0].get('ordinarySafeTarget')is not True
                    or found[0].get('isSourceMeasurement')is not False
                    or not isinstance(found[0].get('reason'),str) or len(found[0]['reason'].strip())<4):
                return None
    return {'version':'management-scope-v3','sourceActions':list(source_actions.values()),
        'clauseMatches':witnessed,'planningTargets':target_reviews if targets else [],
        'claimDigests':{key:_digest(claim.model_dump(mode='json',by_alias=True)) for key,claim in claims.items()},
        'sourceHashes':{anchor.evidence_key:_digest(originals[anchor.evidence_key]['sourceText'])
            for claim in claims.values() for anchor in claim.evidence}}


def _low_risk_management_step_errors(text, quotes, principles, facts, *, planning_targets=(), medical_entities=()):
    errors=[]
    if not _action_is_executable(text):
        errors.append('not_a_concrete_personal_step')
    safety_text=text
    for span in _ordinary_clinical_decision_spans(text):
        safety_text=safety_text.replace(span,' ')
    if re.search(r'确诊|诊断为|患有|药物|处方|补剂|益生菌|菌群移植|粪菌|PFMT|MTPN|服用|口服|注射|'
            r'补充.{0,8}(?:维生素|矿物质)|内镜|胃镜|肠镜|活检|手术',safety_text,re.I):
        errors.append('outside_low_risk_planning_scope')
    available=' '.join([*(str(facts[fid].value) for fid in quotes if fid in facts),
        *(anchor.quote for claim in principles for anchor in claim.evidence)])
    numeric_text=text
    for fid in quotes:
        observed=str(getattr(facts.get(fid),'date','') or '')
        match=re.fullmatch(r'(\d{4})-(\d{1,2})-(\d{1,2})',observed)
        if match:
            year,month,day=match.groups()
            for literal in (observed,f'{year}年{int(month)}月{int(day)}日'):
                numeric_text=numeric_text.replace(literal,' ')
    # A number within an adopted medical entity is not a new dose/target.
    # Only trusted candidate/source names are supplied by the caller.
    for entity in sorted((term for term in medical_entities if isinstance(term,str)
            and re.search(r'\d',term) and len(term)>=3),key=len,reverse=True):
        numeric_text=numeric_text.replace(entity,' ')
    for target in _planning_targets(text,list(planning_targets)):
        numeric_text=numeric_text.replace(target['text'],' ')
    if any(number not in re.findall(r'\d+(?:\.\d+)?',available)
            for number in re.findall(r'\d+(?:\.\d+)?',numeric_text)):
        errors.append('new_numeric_target_or_dose')
    compact=re.sub(r'\s+','',available).casefold()
    if any(re.sub(r'\s+','',value).casefold() not in compact for value in re.findall(
            r'\d+(?:\.\d+)?\s*(?:分钟|小时|毫克|克|次|mg\b|g\b|hours?\b|minutes?\b)',numeric_text,re.I)):
        errors.append('numeric_unit_not_grounded')
    for claim in principles:
        for anchor in claim.evidence:
            if (re.search(r'(?:intake|supplement|administration).{0,35}\d+\s*(?:g|mg|grams?)\b',anchor.quote,re.I)
                    and not re.search(r'food|fruit|vegetable|whole.grain|meal',anchor.quote,re.I)
                    and re.search(r'摄入|补充|膳食纤维来源|食用|剂量|\d+\s*(?:g|mg|克|毫克)',text,re.I)):
                errors.append('isolated_dose_not_ordinary_food_step')
    return list(dict.fromkeys(errors))


def _management_shared_sources(assessment, approved, facts, originals):
    """At most two already-reviewed principles for the same person's goal.

    This is reuse of immutable originals, not new source supports or a
    pooled literature search. Applicability is reviewed again for the step.
    """
    goal=_management_objects(assessment.name+' '+' '.join(facts[fid].name
        for fid in assessment.support_fact_ids if fid in facts))
    if not goal:
        return []
    result=[];seen=set()
    for risk in approved:
        for claim in risk.claims:
            witness=claim._management_witness
            if (claim.role!='management' or witness.get('version')!='management-scope-v3'
                    or witness.get('claimDigests',{}).get(claim.claim_id)!=_digest(claim.model_dump(mode='json',by_alias=True))):
                continue
            for anchor in claim.evidence:
                source=originals.get(anchor.evidence_key)
                if (source is not None and anchor.evidence_key not in seen
                        and witness.get('sourceHashes',{}).get(anchor.evidence_key)==_digest(source['sourceText'])
                        and goal&_management_objects(source['sourceText'])):
                    seen.add(anchor.evidence_key);result.append(source)
    return result[:2]


def _has_personal_lifestyle_plan(assessment,facts,originals):
    return any(_approved_management_plan(action,assessment,facts,originals)
        and any(fid in facts and facts[fid].kind=='self_reported' for fid in action.fact_ids)
        for action in assessment.lifestyle_actions)


def _management_behavior_domains(facts, fact_refs):
    """Locate supplied habits for planning; a match grants no risk inference.

    The whole fact, including a negative/unknown answer, stays available to
    the writer and independent reviewer. Topic names are internal handles,
    not a declaration that this person has an unhealthy habit.
    """
    definitions = (
        ('diet', '饮食与餐时', r'饮食|主食|蔬菜|水果|全谷|豆类|外卖|夜宵|晚餐|餐后|饮料|饮酒'),
        ('bowel', '饮水与排便习惯', r'饮水|喝水|排便|便秘|硬便|腹胀|膳食纤维'),
        ('activity', '运动与久坐', r'运动|活动|快走|步行|久坐|坐.{0,6}(?:小时|分钟)'),
        ('sleepStress', '睡眠与压力调节', r'睡眠|入睡|熬夜|就寝|起床|压力|放松|情绪'),
    )
    result = []
    for code, name, pattern in definitions:
        selected = [fact for fid, fact in facts.items() if fid in fact_refs
            and fact.kind == 'self_reported'
            and re.search(pattern, fact.name + ' ' + fact.value)]
        if selected:
            result.append({'code': code, 'name': name,
                'factQuotes': {fact_refs[fact.fact_id]: fact.value for fact in selected},
                'useAs': 'planning_context_only_not_disease_support'})
    return result


def live_management_plan_context(document, risk_id, text, evidence_summaries):
    """Revalidate one exact personal plan against fresh facts and originals.

    A returned view is useful only in this live request. Callers must look it
    up again at review time; copying this metadata into JSON is not approval.
    Ordinary planning steps do not authorize new physiological benefits.
    """
    facts, originals = _facts(document), _originals(evidence_summaries)
    matches = [(risk, action) for risk in approved_assessments(document) if risk.risk_id == risk_id
        for action in risk.lifestyle_actions if action.text == text
        and _approved_management_plan(action, risk, facts, originals)]
    if len(matches) != 1:
        return None
    risk, action = matches[0]
    witness = action._planning_witness
    behavior = {fid: quote for row in witness.get('clauseMatches', [])
        for fid, quote in row.get('factQuotes', {}).items()
        if fid in facts and facts[fid].kind == 'self_reported' and quote in facts[fid].value}
    return {'riskId': risk.risk_id, 'name': risk.name, 'action': action.text,
        'factIds': list(action.fact_ids), 'behaviorFactQuotes': behavior,
        'problemFactIds': list(risk.support_fact_ids), 'evidenceKeys': list(action.evidence_keys),
        'sourceActions': copy.deepcopy(witness.get('sourceActions', [])),
        'clauseMatches': copy.deepcopy(witness.get('clauseMatches', [])),
        'planningTargets': copy.deepcopy(witness.get('planningTargets', [])),
        'planningIdentity': action._planning_proof,
        'useAs': 'live_reviewed_personal_planning_not_new_scientific_effect'}


def approved_management_context(document):
    """Read-only action-object witnesses for narrative, never risk causality."""
    result=[];facts=_facts(document)
    for risk in approved_assessments(document):
        for action in [*risk.lifestyle_actions,*risk.clinical_actions]:
            witness=action._planning_witness
            if witness.get('version')!='management-scope-v3' or not action._planning_proof:
                continue
            behavior={fid:quote for clause in witness.get('clauseMatches',[])
                if clause.get('role') in {'action','context'}
                for fid,quote in clause.get('factQuotes',{}).items()
                if fid in facts and facts[fid].kind=='self_reported' and quote in facts[fid].value}
            if behavior:
                result.append({'riskId':risk.risk_id,'name':risk.name,'action':action.text,
                    'behaviorFactQuotes':behavior,'problemFactIds':risk.support_fact_ids,
                    'sourceActions':witness.get('sourceActions',[]),'evidenceKeys':action.evidence_keys})
    return result


def _verified_condition_model_view(document, subject, aliases):
    """Read-only review context; only the typed private witness grants scope."""
    from .evaluation_source_scope import source_clinical_condition_witnesses
    return [{'condition': witness.condition, 'certainty': witness.certainty,
        'factIds': [aliases[fid] for fid in witness.fact_ids if fid in aliases]}
        for witness in source_clinical_condition_witnesses(document, subject=subject)]


async def repair_missing_management_actions(client,document,evidence_summaries,*,subject=None):
    """One independently reviewed management-only pass; never re-score risks."""
    registry=_registry(document)
    if client is None:
        return registry
    facts,originals=_facts(document),_originals(evidence_summaries)
    refresh_risk_original_scope(document, evidence_summaries)
    candidates={row.risk_id:row for row in registry.candidates}
    contexts={};items=[];fact_refs={};fact_ids={};attempts={};plan_feedback={}
    def eligible(row):
        if row.risk_id not in candidates:
            return False
        return (row.certainty in {'confirmed','current_problem','self_reported'} or any(
            claim.role in {'outcome','etiology','mechanism'} for claim in row.claims)
            or row.level != '正常' and bool(_declared_risk_signal_ids(candidates[row.risk_id],facts)))
    approved=approved_assessments(document)
    current_context=_management_current_context(approved,candidates,facts)
    current=[row for row in approved if eligible(row)
        and (not _has_personal_lifestyle_plan(row,facts,originals)
            or not row.clinical_actions and not _is_source_signal_only(candidates[row.risk_id],facts,row.claims))]
    # All eligible current problems keep a planning opportunity. A top-risk
    # cutoff had silently excluded constipation/reflux and lower-ranked
    # risks even when their actual modifiable habits were supplied.
    for assessment in sorted(current,key=risk_priority_key):
        candidate=candidates.get(assessment.risk_id)
        if candidate is None:
            continue
        shared_sources=_management_shared_sources(assessment,approved,facts,originals)
        sources=list({source['evidenceKey']:source for source in [*shared_sources,
            *_candidate_evidence(candidate,originals,purpose='management',subject=subject)]}.values())[:5]
        source_only = _is_source_signal_only(candidate,facts,assessment.claims)
        needed = ([] if _has_personal_lifestyle_plan(assessment,facts,originals) else ['lifestyle'])+(
            [] if assessment.clinical_actions or source_only else ['clinical'])
        signature=_digest({'riskId':assessment.risk_id,'sources':{
            row['evidenceKey']:row['sourceText'] for row in sources},'facts':_source_digest(document),'neededKinds':needed})
        if signature in registry._management_attempts:
            # One existing later-stage repair may correct this same
            # rejected plan using concrete feedback; no unbounded loop.
            if not registry._management_feedback.get(assessment.risk_id):
                continue
            signature += ':bounded-repair'
            if signature in registry._management_attempts:
                continue
        plan_feedback[assessment.risk_id] = []
        allowed=set([*candidate.support_fact_ids,*candidate.opposing_fact_ids,*candidate.background_fact_ids])
        allowed={fid for fid in allowed if fid in facts and facts[fid].kind in {'clinical','self_reported'}}
        # A report-only signal still needs a practical ordinary plan. Its
        # same-person habits are planning context, never new score support or
        # an established disease/treatment indication.
        allowed.update(fid for fid,fact in facts.items() if fact.kind=='self_reported')
        allowed.update(_declared_risk_signal_ids(candidate,facts))
        if not allowed:
            continue
        for fid in sorted(allowed):
            if fid not in fact_refs:
                token='F'+str(len(fact_refs)+1);fact_refs[fid]=token;fact_ids[token]=fid
        own={}
        for index,source in enumerate(sources):
            # Source aliases are local to this risk's bounded source list.
            # There is no globally visible E6 that another item cannot cite.
            token='E'+str(index+1)
            own[token]=source
        reference='R'+str(len(items)+1)
        contexts[reference]=(assessment,candidate,allowed,own,needed)
        attempts[reference]=signature
        items.append({'riskId':reference,'name':assessment.name,'currentConclusion':assessment.conclusion,
            'requestedKinds':needed,
            'previousRejectedPlanFragments':registry._management_feedback.get(assessment.risk_id, [])[:5],
            'boundedRepairObjectScope':next((item for item in registry._management_feedback.get(assessment.risk_id, [])
                if item.get('nextDraftScope')=='single_ordinary_fruit_step'),None),
            'certainty':assessment.certainty,'sourceSignalOnly':source_only,
            'planningScope':'ordinary_habits_and_observation' if source_only else 'current_problem_management',
            'existingManagementPrinciples':[claim.model_dump(mode='json',by_alias=True)
                for claim in assessment.claims if claim.role=='management'],
            'currentProblemContext':current_context,
            'facts':[dict(_fact_view(facts[fid]),factId=fact_refs[fid]) for fid in sorted(allowed)],
            'sources':[{'evidenceId':token, **_model_original(source)}
                for token,source in own.items()]})
    if not items:
        return registry
    # Questionnaire facts describe the same person across problems. They
    # remain planning context, never extra disease-score support.
    shared = {fid for fid, fact in facts.items() if fact.kind == 'self_reported'}
    shared.update(fid for context in contexts.values() for fid in context[2])
    for fid in sorted(shared):
        if fid not in fact_refs:
            token='F'+str(len(fact_refs)+1);fact_refs[fid]=token;fact_ids[token]=fid
    contexts={key:(assessment,candidate,set(shared),own,needed)
        for key,(assessment,candidate,allowed,own,needed) in contexts.items()}
    try:
        draft=await client.generate({'task':'repair_missing_personal_management_actions','subject':_subject_view(subject),
            'patientFacts':[dict(_fact_view(facts[fid]),factId=fact_refs[fid]) for fid in sorted(shared)],
            'behaviorDomains':_management_behavior_domains(facts, fact_refs),
            'existingLifestylePlans':[{'riskId':row.risk_id, 'name':row.name,
                'actions':[action.text for action in row.lifestyle_actions]}
                for row in approved if row.lifestyle_actions],
            'items':items,'rules':[
                '仅补已审当前问题或有依据潜在风险的管理动作，不重评风险、分数、诊断或病因。先选原始推荐/干预原则逐字quote，再写忠实原则和本人执行步骤。sourceSignalOnly=true只规划普通习惯与症状观察；不能假定已患该病或据此安排疾病专属检查、治疗。',
                '步骤和target都不得添加资料没有的既有行为属性：常点外卖不等于高脂/高油/高盐；夜宵每周3次与常选外卖不是每周3次外卖夜宵。保留各自频次所属对象；未来建议与已经存在的行为分清。',
                'existingManagementPrinciples是已存在的一般原则，不等于已经有执行计划。针对实际主食、外卖夜宵、饮酒、快走或久坐写同域可执行步骤；不要只把原则翻译一遍。可按普通规划给适合本人的频次时长，不冒充文献的具体时间或个体疗效。',
                '先统筹behaviorDomains中真实且需要调整的饮食餐时、饮水排便、活动久坐、睡眠压力主题，再为对应当前问题或有依据风险补动作。领域名只定位资料，不代表其中行为异常；未知、否认和保护行为仍按原值核对，不强制凑满四类。',
                '一条普通行动可服务多个实际相关问题，优先形成同一份可执行安排；同样动作不要为每个疾病换词重复。仍须在每项risk分别绑定真实行为、适用原则和逐句审核，不共享未经审核的许可，不因共用行动增加疾病因果或风险分值。',
                '生活方式步骤要说明改什么、怎样做及可执行的日常时点或合理渐进目标；原文有频次时长目标时准确采用。只有风险名称或“关注、观察变化、保持健康生活”不算行动。专科咨询、检查或转介不替代lifestyle；缺少适用原则时明确留内部管理缺口，不能用clinical冒充已补齐。',
                '直接面向用户使用“您”或省略主语，不写“本人”“该用户”“已列低项”“代表方向”或报告编制方法；目标是可实际执行的行为改变，不重复检验值、疾病名单或研究介绍。',
                '优先使用已给真实推荐/转介句，不总选健康饮食很重要等泛句。可据原则及已提供行为写普通低风险具体步骤，不要求步骤与原则翻译逐字相同。',
                '原文中的研究干预分类不是任意日常食品组合的同义名。multi-component foods等分类必须有实际组成支持，不能仅凭“多组分”就把精米面换全谷/豆/蔬菜等任意组合声称为该试验类别并继承效益。可用真正一般健康饮食原则给普通步骤；若用水果等具体干预效益，应保持实际干预对象，不编配方或剂量。',
                '水果管理原则可结合本人原有摄入与排便记录安排渐进改变；真实强化咨询转介可变为预约专业人员并带异常结果。不能添加原文没有的健康效益。',
                'boundedRepairObjectScope=single_ordinary_fruit_step时仅重写一条水果生活方式步骤，严格保持本次可用原文的水果对象；不要把旧稿其他蔬菜、全谷物、饮水、如厕或运动命令补回。新步骤整稿重新独立审核，旧稿通过标志不继承。',
                '不发明药物、补充剂剂量、新诊断或侵入检查；制剂剂量试验不等于普通食品。普通低风险行为可按本人情况规划频次或时长，须在planningTargets标为计划目标，不声称是文献原剂量/本人已做；新体重、能量、克数/毫升等治疗或摄入量不能随意补。',
                '每个步骤只用真正支持其行动对象的原则。水果效益不授权饮水或运动，低碳效益不授权餐时或体位；一般健康生活方式原则也须准确界定实际覆盖的行为域。一个步骤有多个行动子句时，每个都必须能分别对应原始原则或属于围绕实际主诉的自我记录。',
                '打断连续久坐/短暂站立走动与增加一般有氧运动次数或时长是不同干预。不能因同属身体活动，就拿久坐打断推荐替代每周快走量的原文。原文已有一般身体活动或有氧运动原则时，才按本人习惯给相应规划。',
                'E短ID限本risk来源；F短ID可采用patientFacts中同一本人已提供的真实饮食/活动等背景，不限发现该风险时选的少量代表。背景用于个性化执行，不因此新增疾病关联或提高风险分数。quote/factQuotes逐字。',
                '仅补requestedKinds，已存在的计划不重复。clinical自然写适合科室、围绕当前已确定异常/持续主诉的具体评估与目的；有适用推荐才引用。对当前真实问题的普通临床评估安排，可principleIds=[]，不冒充文献推荐，不新增侵入检查、无依据新检验项目、诊断或治疗；普通就诊目的不是治疗效益，无需虚配疗效文献。当前已有持续主诉即可给相应专科的评估方向，不能无依据要求先调整饮食饮水若干周、达到新次数阈值后才评估。临床步骤优先引用当前异常或主诉本身的逐字事实，不用无关正常检查作评估前提，也不复抄原数值。',
                '真实成立的潜在风险可给适用初级预防和风险因素管理，不要求用户已患该病；不能把原报告单一筛查信号当临床检查适应症。有定义文献而无检查推荐时，普通当前主诉评估用principleIds=[]，不要将定义硬装成管理原则。',
                '每risk最多2原则、3步骤；没有适用生活原则则不硬补生活方案。无需以缺资料或方法声明凑临床安排。',
                '每个生活方式步骤只安排一个有原文支持的行为主题；不把已支持的水果或睡眠安排与没有对应原则的蔬菜、饮水、屏幕限制合成一整步。一个原则失效不能借另一个原则填空；只使用真正可用的principleIds。',
                '可执行不等于必须写新数字。没有同对象真实推荐数值时，用具体食物替换、日常选择、渐进调整或自我记录说明怎样做；不默认填150分钟、5次30分钟、1500毫升、两小碗、睡前1小时等常识目标。原文明确的成年人7小时睡眠可准确采用，不顺带新增屏幕限制或未经询问的行为。',
                '原文目标人群为泛称风险因素时，独立审核必须从同篇POPULATION段准确选择实际纳入条件；仅腰围或菌群关联不能冒充血脂异常等纳入条件，不忽略同段明确排除项。'],
            'outputSchema':{'items':[{'riskId':'R1','principles':[{'claimId':'P1','evidenceId':'E1',
                'quote':'逐字原文','text':'忠实原则','factQuotes':{'F1':'逐字值'}}],
                'steps':[{'kind':'lifestyle或clinical','text':'本人具体步骤','target':'本人问题',
                    'principleIds':['P1'],'factQuotes':{'F1':'逐字值'},
                    'planningTargets':[{'text':'步骤中的频次、时长或日常时点原片段','kind':'frequency或duration或timing'}]}]}]}})
    except Exception:
        return registry
    if not isinstance(draft,dict) or not isinstance(draft.get('items'),list):
        return registry
    returned={row.get('riskId') for row in draft['items'] if isinstance(row,dict)}
    prepared=[]
    for row in draft.get('items',[]) if isinstance(draft,dict) else []:
        if not isinstance(row,dict) or row.get('riskId') not in contexts:
            continue
        assessment,candidate,allowed,own,needed=contexts[row['riskId']];claims={}
        for raw in row.get('principles',[])[:2]:
            try:
                source=_management_anchor(raw.get('evidenceId'),raw.get('quote'),own)
                quotes=_adopted_plan_fact_quotes(assessment,
                    {fact_ids.get(key,key):value for key,value in raw.get('factQuotes',{}).items()},facts)
                if source is None or not _valid_fact_quotes(quotes,facts,allowed):
                    plan_feedback[assessment.risk_id].append({'reason':'original_anchor_or_literal_patient_quote_invalid',
                        'originalQuote':str(raw.get('quote', ''))[:500]})
                    continue
                claim=RiskClaim(claim_id='plan:'+_digest([assessment.risk_id,raw])[:16],role='management',
                    text=raw['text'],fact_quotes=quotes,evidence=[RiskEvidenceAnchor(evidence_key=source['evidenceKey'],
                        quote=_canonical_management_quote(raw['quote'], source['sourceText']))])
                principle_errors=[*_role_evidence_errors(claim,originals),
                    *_management_patient_scope_errors(claim,candidate,facts,originals,current_context=current_context)]
                if principle_errors:
                    plan_feedback[assessment.risk_id].append({'reason':','.join(principle_errors),
                        'originalQuote':str(raw.get('quote', ''))[:500]})
                    continue
                claims[raw['claimId']]=claim
            except (KeyError,TypeError,ValueError,ValidationError):
                continue
        for raw in row.get('steps',[])[:3]:
            try:
                raw=dict(raw)
                selected=[claims[key] for key in raw.get('principleIds',[]) if key in claims]
                fruit_only=any(item.get('nextDraftScope')=='single_ordinary_fruit_step'
                    for item in registry._management_feedback.get(assessment.risk_id, []))
                if (fruit_only and raw.get('kind')=='lifestyle'
                        and not _single_fruit_step_scope(str(raw.get('text')or''),selected)):
                    # Reject the whole mixed draft. No accepted fragment or
                    # previous reviewer verdict is reused as new approval.
                    plan_feedback[assessment.risk_id].append({'reason':'bounded_fruit_repair_scope_not_met',
                        'step':str(raw.get('text')or'')[:600],
                        'instruction':'整条步骤重写为当前水果原文支持的一条水果行动，再提交完整独审；不截断旧稿。'})
                    continue
                quotes=_adopted_plan_fact_quotes(assessment,
                    {fact_ids.get(key,key):value for key,value in raw.get('factQuotes',{}).items()},facts)
                raw['text'],raw['target']=_management_personal_step(raw['text'],raw.get('target',''),
                    quotes,facts,assessment.name)
                primary = set(assessment.support_fact_ids) & set(quotes)
                targets=_planning_targets(raw['text'],raw.get('planningTargets',[]))
                local_clinical = raw.get('kind') == 'clinical' and (raw.get('principleIds') == []
                    or len(selected)!=len(raw.get('principleIds',[])))
                if local_clinical:
                    selected=[]  # Never attach a rejected definition/conditional source as a citation.
                if ((not selected and not local_clinical) or len(selected)!=len(raw.get('principleIds',[]))
                        and not local_clinical
                        or local_clinical and not any(facts[fid].kind in {'clinical','self_reported'} for fid in primary)
                        or not _valid_fact_quotes(quotes,facts,allowed) or raw.get('kind') not in needed
                        or _low_risk_management_step_errors(raw['text'],quotes,selected,facts,planning_targets=targets,
                            medical_entities=_concept_forms([assessment.name,*candidate.search_terms]))):
                    plan_feedback[assessment.risk_id].append({'reason':'step_not_bound_to_accepted_principle_or_patient_scope_or_target',
                        'step':raw['text'][:600],
                        'details':_low_risk_management_step_errors(raw['text'],quotes,selected,facts,planning_targets=targets,
                            medical_entities=_concept_forms([assessment.name,*candidate.search_terms]))})
                    continue
                prepared.append({'id':'A'+str(len(prepared)+1),'reference':row['riskId'],
                    '_document':document,
                    'kind':raw['kind'],'assessment':assessment,'allowed':allowed,
                    'localClinical':local_clinical,
                    'currentProblemContext':current_context,
                    'planningTargets':targets,'factAliases':fact_ids,
                    'actualSubject':_subject_view(subject or registry.subject_snapshot),
                    'principleAliases':{'P'+str(index+1):claim.claim_id for index,claim in enumerate(selected)},
                    'claims':selected,'factQuotes':quotes,'action':RiskAction(text=raw['text'],target=raw.get('target',''),
                        fact_ids=list(quotes),evidence_keys=list(dict.fromkeys(a.evidence_key for claim in selected for a in claim.evidence)),
                        claim_ids=[claim.claim_id for claim in selected])})
            except (KeyError,TypeError,ValueError,ValidationError):
                continue
    if not prepared:
        registry._management_attempts.update(signature for ref,signature in attempts.items() if ref in returned)
        registry._management_feedback.update({key:value[:5] for key,value in plan_feedback.items() if value})
        return registry
    verified_condition_view = _verified_condition_model_view(document,
        subject or registry.subject_snapshot, fact_refs)
    review_payload={'task':'review_personal_management_plans','subject':_subject_view(subject),
            'items':[{'id':row['id'],'problem':row['assessment'].name,'step':row['action'].text,'target':row['action'].target,
                'localClinicalEvaluation':row['localClinical'],
                'currentProblemContext':current_context,
                'verifiedSourceConditions':verified_condition_view,
                'subjectEligibilityContext':row.get('actualSubject',{}),
                'sourceScopes':[{**scope,'principleId':'P'+str(index+1)}
                    for index,claim in enumerate(row['claims']) for scope in _management_source_scope_requirements(claim,originals)],
                'clauses':_management_clauses(row['action'].text),'planningTargets':row['planningTargets'],
                'currentProblemClaims':[claim.model_dump(mode='json',by_alias=True)
                    for claim in row['assessment'].claims if claim.role=='source_fact'],
                'facts':[dict(_fact_view(facts[fid]),factId=fact_refs[fid]) for fid in row['factQuotes']],
                'patientContext':[dict(_fact_view(facts[fid]),factId=fact_refs[fid]) for fid in sorted(row['allowed'])
                    if fid in row['factQuotes'] or fid in row['assessment'].support_fact_ids
                    or any(fid in context['factIds'] for context in current_context)
                    or any(fid in claim.fact_quotes for claim in row['claims'])],
                'principles':[{'principleId':'P'+str(index+1),'quote':anchor.quote,
                    'evidenceKey':anchor.evidence_key,'sourceTitle':originals[anchor.evidence_key]['title'],
                    'sourceScopeOptions':_risk_source_scope_options(claim, originals),
                    **_model_original(originals[anchor.evidence_key], selected_quotes=[anchor.quote])}
                    for index,claim in enumerate(row['claims']) for anchor in claim.evidence]}
                for row in prepared],
            'rules':['独立审原则原文和本人适用，步骤可以是原则结合真实行为的合理普通低风险具体化，不要求原文包含执行步骤逐字句。',
                'principleSupported须区分一般原则适配与研究干预实体等价：任意多种食品不能仅因“multi-component foods”分类名就等同研究干预并承接效益；蔬菜配全谷物可能是原研究中另一个比较类别。没有该类别组成证据则不允许这种等价。真实一般饮食原则仍可支持普通低风险步骤，不能把观察性风险关联变成干预推荐。',
                '不许借泛原则新增疾病/因果/药物/补剂/数字剂量/侵入检查；制剂≠食品、治疗/人群前提、菌株、年龄与反向证据均严审。',
                '分别审核sourcePopulationApplicable（原推荐人群/疾病条件是否适用）与patientApplicable（步骤本身是否适合本人）；普通步骤安全不等于可以借用不适用人群文献。verifiedSourceConditions是本轮已有真实诊断或已独立核实分类及其事实绑定，可用于同一患者的真实适用条件；不是候选假设或裸数值标签，也不授权新病因、剂量或扩大来源推荐。',
                '逐principleId和实际evidenceKey返回sourceScopeReviewsByPrinciple；对sourceScopeOptions每一真实premise复制premiseId/sourceQuote，以实际本人factIds/factQuotes或真实年龄/性别核验，result=met或not_applicable。仅logicalRole=comparison_context的参考/比较组返回context_preserved，不要求本人同时属于比较组；未知基因型、疾病、检查方法/部位、治疗条件不得用普通步骤安全或候选病名自证。依据可只来自本principle已绑定的真实factQuotes，不能临时猜入组条件。',
                '先从给定原则quote抽sourceActions：保留原文行动对象objectQuote与实际推荐/效益动作actionQuote、角色、populationQuote及确有的适用条件，并用本人factQuotes满足这些条件。不能只给valid=true；每一条clauses必须返回clauseMatches，行动子句分别绑定sourceActionIds和步骤对象原片段。水果研究不能匹配饮水或起身，低碳研究不能匹配晚餐时间或平卧。',
                'sourceScopes是整篇原文明确限定的管理人群，逐一scopeId返回scopeBindings并引用同一原句及本人实际条件/同病currentProblemIds；所选单句未重复病名也保留整篇人群，不能把MAFLD等患者随访泛化到只有腰围或血脂异常者。普通步骤安全不代替原人群适用。',
                '一般健康饮食与生活方式并列原则可支持相应饮食和活动规划；lifestyle modifications等真实一般推荐可作同域普通安排，但不得声称文献逐字指定了该时点、剂量或个体疗效。特定水果/低碳/制剂试验不因此扩大为所有日常行为。',
                '年龄/性别仅采用每项subjectEligibilityContext的真实结构值：纯Adults/明确年龄范围可返回subjectEligibility={age,sex}，不伪造factId=subject；疾病、症状、药物和其他入组条件仍绑定真实F编号或currentProblemIds，不能由人口字段替代。',
                '原文锚点由服务器复用已选principle.quote；sourceAction对象与谓词始终来自原英文锚点，clauseMatches对象始终来自实际中文步骤，不要求两种语言字符串相等。研究比较组不是被推荐对象；multi-component分类名不等任意组合。general_principle也要真正一般原则原文，不能把特定试验升格为任意生活方式指导。',
                '普通执行提醒、用餐时的未来安排、维持同一睡眠计划或执行衔接可标implementation，不能伪造本人现状fact。须绑定同完整计划内已有真实来源支持且有本人适用事实的action父句parentClauseIds、相同sourceActionIds，独立确认无新行动对象/疗效/剂量。父句可位于后文，但不能互相循环或仅有implementation。睡前提醒不要求论文逐字写本人提醒时间；水果的未来用餐安排不冒充已提供三餐史。若陈述本人既有习惯仍用context并绑定真实事实。增加饮水/运动、将低碳扩为餐时、医疗剂量或病效不属于实施衔接。',
                'context若陈述本人现状必须有本人factQuotes；仅说明原文一线处理/推荐背景的子句用现有sourceActionIds绑定对应源句，不为文献原则伪造本人事实。不能把保持/增加/减少/避免等行动标context来绕过支持；self_monitoring只对真实问题作普通记录观察，不新增检验。local_clinical用于无引文的当前主诉/异常临床评估，无sourceActionIds且不冒充文献规定；由医生评估是否需要药物或治疗不等于自行用药。',
                '再回到工作等没有新医疗断言的执行衔接可作context，不伪造本人已做过该步骤的factQuote。步骤和target均须忠实本人原记录，不能增加高脂/高盐属性或把一个谓词的频次绑定到邻近的另一行为。久坐打断与一般有氧量分别核对；行动对象取推荐谓词，不取人群前提中偶然出现的activity词。',
                'planningTargets是待审计划频次/时长/日常时点，不是原文剂量或本人已做。逐项给ordinarySafeTarget、isSourceMeasurement=false及理由；同一行动对象的合理低风险安排可有，不允许新增药补剂/克数毫升摄入治疗量，不能用原数字拼造目标或把维持原习惯称改善。普通临床评估目的不需要sourceEffectQuote；只有声称治疗效益的purpose才必须绑定实际效益原文。',
                'localClinicalEvaluation=true时无文献推荐，不把它当循证干预；只核真实当前问题、科室及具体评估目的是否匹配。不得推断未知标本/器官，不新增无依据检验或侵入检查；clinicalEvaluationAppropriate及noUnsupportedTestOrSpecialty均true才可过。',
                '仅valid、patientApplicable、concreteLowRiskStep、noNewMedicalClaimOrDose全部true且missingConditions空才通过；有引用原则者还需principleSupported与sourcePopulationApplicable均true。'],
            'outputSchema':{'items':[{'id':'A1','valid':True,'principleSupported':True,'patientApplicable':True,
                'sourcePopulationApplicable':True,'clinicalEvaluationAppropriate':True,'noUnsupportedTestOrSpecialty':True,
                'concreteLowRiskStep':True,'noNewMedicalClaimOrDose':True,'missingConditions':[],
                'sourceScopeReviewsByPrinciple':{'P1':{'实际evidenceKey':{'premiseMatches':[
                    {'premiseId':'输入原ID','sourceQuote':'输入原限制句',
                        'result':'met|not_applicable|context_preserved','factIds':[],'factQuotes':{}}]}}},
                'sourceActions':[{'id':'SA1','principleId':'原principleId','role':'recommendation或intervention_effect或general_principle',
                    'quote':'已选原则内逐字原句','objectQuote':'其中行动对象逐字片段',
                    'actionQuote':'其中实际推荐动作或干预效益谓词的完整逐字片段，不仅分类名','populationQuote':'实际人群原文或空',
                    'requiredConditions':[{'quote':'来源必要条件原文','factQuotes':{'F1':'本人逐字事实'},'subjectEligibility':{},'satisfied':True}]}],
                'clauseMatches':[{'clauseId':'C1','text':'输入子句原文','role':'context或action或implementation或self_monitoring或local_clinical',
                    'sourceActionIds':['SA1'],'stepObjectQuote':'步骤中实际行动对象','factQuotes':{'F1':'实际事实'},
                    'implementation':{'kind':'future_placement|reminder|schedule_continuity|execution_transition',
                        'parentClauseIds':['同完整计划内已支持action句ID'],'noPatientObservation':True,
                        'noNewActionObject':True,'noNewMedicalEffectOrDose':True,'samePersonalPlan':True},
                    'sameObjectTerms':['仅未识别的新对象需要：本人步骤中的原词','同一对象的来源原词；不填相关但不同的对象'],
                    'reason':'实际对应依据；不以动作安全代替原文支持'}],
                'planningTargets':[{'text':'原计划频次时长片段','kind':'frequency或duration',
                    'ordinarySafeTarget':True,'isSourceMeasurement':False,'reason':'适合本人且属于普通规划'}],
                **_management_scope_schema()}]}}
    responses=await _bounded_generate(client,_management_review_batches(review_payload))
    review={'items':[item for response in responses if isinstance(response,dict)
        and isinstance(response.get('items'),list) for item in response['items'] if isinstance(item,dict)]}
    # A transport/JSON failure is not a completed medical rejection. Preserve
    # completed batches, and leave the failed duties eligible in the existing
    # later report stage without adding a new phase or changing model limits.
    completed={item.get('id') for item in review['items']
        if isinstance(item.get('valid'),bool) and isinstance(item.get('missingConditions'),list)
        and sum(other.get('id')==item.get('id') for other in review['items'])==1}
    for ref,signature in attempts.items():
        ids={row['id'] for row in prepared if row['reference']==ref}
        if ref in returned and (not ids or ids<=completed):
            registry._management_attempts.add(signature)
    for row in prepared:
        matches=[item for item in review.get('items',[]) if isinstance(item,dict) and item.get('id')==row['id']] if isinstance(review,dict) else []
        if len(matches)!=1 or matches[0].get('missingConditions')!=[] or not all(matches[0].get(key) is True
                for key in ('valid','patientApplicable','concreteLowRiskStep','noNewMedicalClaimOrDose')):
            continue
        # A useful ordinary clinical assessment does not borrow a behaviour
        # counselling source to prove a test/follow-up. Use the already
        # independent local-clinical verdict and discard that false citation.
        verdict=matches[0]
        witness=_management_scope_witness(row,verdict,facts,originals)
        if (row['kind']=='clinical' and not row['localClinical']
                and witness is None
                and _ordinary_current_clinical_plan(row,verdict,facts)
                and all(item.get('role') in {'context','purpose','local_clinical','self_monitoring','action'}
                    for item in verdict.get('clauseMatches',[]))):
            row={**row,'localClinical':True,'claims':[],'principleAliases':{},
                'action':row['action'].model_copy(update={'claim_ids':[],'evidence_keys':[]})}
            verdict={**verdict,'sourceActions':[],'clauseMatches':[
                {**item,'sourceActionIds':[],
                    'role':'local_clinical' if item.get('role')=='action' else item.get('role')}
                for item in verdict.get('clauseMatches',[])]}
            witness=_management_scope_witness(row,verdict,facts,originals)
        required = ('clinicalEvaluationAppropriate','noUnsupportedTestOrSpecialty') if row['localClinical'] else (
            'principleSupported','sourcePopulationApplicable')
        if not all(matches[0].get(key) is True for key in required):
            continue
        if witness is None:
            plan_feedback[row['assessment'].risk_id].append({'reason':'independent_review_witness_incomplete',
                'step':row['action'].text[:600],
                **({'instruction':'按真实原文人口、逐字患者事实、同一行动对象及输入计划目标核对；精简为单一有据行为，不补默认数字。'}
                    | _fruit_only_management_repair(row))})
            continue
        assessment=row['assessment'];existing={claim.claim_id for claim in assessment.claims}
        assessment.claims.extend(claim for claim in row['claims'] if claim.claim_id not in existing)
        assessment.evidence_keys=list(dict.fromkeys([*assessment.evidence_keys,*row['action'].evidence_keys]))
        row['action']._planning_witness=witness
        row['action'].fact_ids=list(dict.fromkeys([*row['action'].fact_ids,
            *(fid for clause in witness['clauseMatches'] for fid in clause.get('factQuotes',{})),
            *(fid for source in witness['sourceActions'] for binding in source.get('scopeBindings',[])
                for fid in binding.get('factQuotes',{}))]))
        for claim in row['claims']:
            claim._management_witness=witness
        row['action']._planning_proof=_management_plan_digest(row['action'],assessment,facts,originals)
        if row['kind']=='lifestyle':
            assessment.lifestyle_actions=[action for action in assessment.lifestyle_actions
                if _approved_management_plan(action,assessment,facts,originals)]
        getattr(assessment,row['kind']+'_actions').append(row['action'])
        registry._evidence_hashes.update({key:_digest(originals[key]['sourceText']) for key in row['action'].evidence_keys})
        _record_risk_original_scope(registry, originals, row['action'].evidence_keys)
        registry._proofs[assessment.risk_id]=_proof(assessment,document,registry._evidence_hashes)
    registry._management_feedback.update({key:value[:5] for key,value in plan_feedback.items() if value})
    reviewed={row.risk_id:row for row in approved_assessments(document) if eligible(row)}
    registry.gaps=[gap for gap in registry.gaps if not(gap.get('riskId') in reviewed and gap.get('missingRole')=='management')]
    registry.gaps.extend({'riskId':row.risk_id,'missingRole':'management','actionKind':kind,
        'reason':'no_reviewed_'+kind+'_action','searchTerms':candidates[row.risk_id].search_terms}
        for row in reviewed.values() for kind in ('lifestyle','clinical')
        if (not _has_personal_lifestyle_plan(row,facts,originals) if kind=='lifestyle' else not row.clinical_actions)
        and row.risk_id in candidates)
    if any(gap.get('missingRole')=='management' for gap in registry.gaps):
        if 'INTEGRATED_RISK_MANAGEMENT_INCOMPLETE' not in registry.incomplete:
            registry.incomplete.append('INTEGRATED_RISK_MANAGEMENT_INCOMPLETE')
    else:
        registry.incomplete=[code for code in registry.incomplete if code!='INTEGRATED_RISK_MANAGEMENT_INCOMPLETE']
    return registry
