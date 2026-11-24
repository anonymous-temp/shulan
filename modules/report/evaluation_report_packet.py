"""Request-local model views of the immutable evaluation source snapshot.

Canonical source IDs and the complete inventory stay on the server. Models
receive each fact once, shared scopes and the actual same-owner result rows.
This is a lossless addressing change, never a source-value or public-schema
transformation.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
import re

from pydantic import BaseModel


_FACT_FIELDS = frozenset({'kind', 'name', 'value', 'unit', 'status', 'date',
    'referenceRange', 'reference_range', 'sourceCode', 'measurementType',
    'subject', 'certainty', 'abnormal'})
_PROSE_FIELDS = frozenset({'text', 'summary', 'explanation', 'association',
    'health_meaning', 'healthMeaning', 'description', 'basis', 'followup_action',
    'followupAction', 'attention_basis', 'attentionBasis', 'evidence_strength',
    'evidenceStrength', 'modifiability', 'source_advice', 'sourceAdvice'})
_METADATA_FIELDS = frozenset({'sources', 'evidence_refs', 'evidenceRefs',
    'source_items', 'sourceItems', 'reference_literature', 'referenceLiterature',
    'indicator_standards', 'indicatorStandards', 'availability'})
_RESULT_FIELDS = frozenset({'value', 'score', 'source_status', 'sourceStatus',
    'assessment_level', 'assessmentLevel', 'detection_status', 'detectionStatus'})


def _json(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json', by_alias=False)
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return deepcopy(value)


def _node(data, path):
    for part in path:
        data = data[part] if isinstance(data, (dict, list)) else getattr(data, part)
    return data


def _field(value, name, default=None):
    snake = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
    for key in (name, snake):
        if isinstance(value, dict) and key in value:
            return value[key]
        if not isinstance(value, dict) and hasattr(value, key):
            return getattr(value, key)
    return default


def _selected_risk_rows(data, category=None):
    section = _field(data, 'diseaseRiskAssessment', {})
    layout = _field(data, '_template_layout', {}) or {}
    groups = {row.get('riskCode', row.get('code')): row
        for row in layout.get('groups', ()) if isinstance(row, dict)}
    result = []
    for row in _field(section, 'riskRanking', ()):
        plain = _json(row)
        code = plain.get('code', '')
        if category and not str(code).startswith('templateRisk:' + category + ':'):
            continue
        binding = groups.get(code, {})
        if binding.get('category') and category and binding['category'] != category:
            continue
        plain['sourceNature'] = binding.get('origin') or 'unclassified_source_record'
        plain['certainty'] = binding.get('certainty') or plain.get('certainty')
        result.append(plain)
    return result


def _aggregate_frame(data, chapter, target):
    """Complete displayed results for a summary's actual section/group.

    Only selected risk rows are clinical synthesis inputs. Raw disease panels
    remain source facts and are never introduced here as patient diagnoses.
    """
    code = chapter.get('code')
    section = _field(data, code, {})
    owner = target.get('owner', {})
    path = target.get('path', ())
    if code == 'diseaseRiskAssessment':
        category = owner.get('code')
        known = {_field(row, 'code') for row in _field(section, 'categories', ())}
        context_codes = set(owner.get('clinicalContextCodes') or ())
        return {'code': category, 'name': owner.get('name'),
            'summaryMode': owner.get('summaryMode'),
            'selectedProblems': _selected_risk_rows(data, category if category in known else None),
            'clinicalManagementContext': [_json(row) for row in _field(
                _field(data, 'medicalAssessment', {}), 'findings', ())
                if _field(row, 'code') in context_codes]}
    fields = {
        'microbiomeAssessment': ('overallStructure', 'phylumComposition', 'healthMetrics',
            'shortChainFattyAcids', 'antimicrobialResistance', 'panelCounts'),
        'nutritionAssessment': ('majorMetabolism', 'detailedMetabolism', 'vitaminsAndMinerals',
            'aminoAcidsAndMetabolites', 'neurotransmitters'),
        'functionalPathways': ('pathways',),
        'medicalAssessment': ('findings', 'triggerAnalysis'),
    }
    if code in fields:
        collection = {'amino_metabolite_interpretation': 'aminoAcidsAndMetabolites',
            'aminoMetaboliteInterpretation': 'aminoAcidsAndMetabolites',
            'neurotransmitter_interpretation': 'neurotransmitters',
            'neurotransmitterInterpretation': 'neurotransmitters',
            'scfa_interpretation': 'shortChainFattyAcids',
            'scfaInterpretation': 'shortChainFattyAcids'}.get(path[1] if len(path) > 1 else '')
        selected = (collection,) if collection else fields[code]
        return {name: _json(_field(section, name)) for name in selected
            if _field(section, name) is not None}
    if code in {'healthOverview', 'comprehensiveConclusion'}:
        return {
            'healthOverview': {name: _json(_field(_field(data, 'healthOverview', {}), name))
                for name in ('assessmentChart', 'keyFindings')},
            'medicalAssessment': {name: _json(_field(_field(data, 'medicalAssessment', {}), name))
                for name in fields['medicalAssessment']},
            'selectedDiseaseProblems': _selected_risk_rows(data),
            **{section_code: _aggregate_frame(data, {'code': section_code}, {'path': [], 'owner': {}})
                for section_code in ('microbiomeAssessment', 'nutritionAssessment', 'functionalPathways')},
        }
    return _json(section)


def _replace(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, tuple):
        return [_replace(item, mapping) for item in value]
    if isinstance(value, dict):
        return {(mapping.get(key, key) if isinstance(key, str) else key):
            _replace(item, mapping) for key, item in value.items()}
    return deepcopy(value)


@dataclass(frozen=True)
class ChapterModelPacket:
    payload: dict
    _to_model: dict
    _to_source: dict

    def encode(self, value):
        return _replace(value, self._to_model)

    def decode(self, value):
        # Unknown handles are deliberately retained. The server's ordinary
        # scope validator rejects them rather than guessing an original ID.
        return _replace(value, self._to_source)


def _source_refs(value, handles):
    refs = []
    for source in value.get('sources', ()):
        identifier = source.get('record_id', source.get('recordId'))
        if identifier in handles and handles[identifier] not in refs:
            refs.append(handles[identifier])
    return refs


def _readonly(value, handles):
    if isinstance(value, list):
        return [_readonly(item, handles) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {key: _readonly(item, handles) for key, item in value.items()
        if key not in _PROSE_FIELDS | _METADATA_FIELDS and item is not None
        and item != [] and item != {}}
    if refs := _source_refs(value, handles):
        result['factRefs'] = refs
    return result


def _result_rows(value):
    if isinstance(value, list):
        return [row for item in value for row in _result_rows(item)]
    if not isinstance(value, dict):
        return []
    if any(key in value and value[key] is not None for key in _RESULT_FIELDS):
        return [value]
    return [row for key, item in value.items() if key not in _METADATA_FIELDS
        for row in _result_rows(item)]


def _grade_distribution(frame):
    rows = _result_rows(frame)
    counts = Counter(row.get('assessment_level', row.get('assessmentLevel'))
        for row in rows if row.get('assessment_level', row.get('assessmentLevel')))
    return {'resultCount': len(rows), 'displayAssessmentCounts': dict(counts),
        'withoutDisplayAssessment': sum(not row.get('assessment_level', row.get('assessmentLevel')) for row in rows)}


def build_chapter_packet(blueprint, chapter, targets, subject, previous_reviewed):
    """Build a complete, non-repeating view for one content work group.

    ``targets`` can be a mapping or a list. Full source scopes are interned;
    per-target rows reference them by ID. No source measurement is clipped,
    regraded or selected by position. A short fact handle is unique only inside
    this request and is translated back before any source/publication check.
    """
    targets = list(targets.values()) if isinstance(targets, dict) else list(targets)
    facts = {row.get('factId', row.get('fact_id')): row for row in blueprint.facts}
    facts.pop(None, None)
    handles = {fid: f'F{index:03d}' for index, fid in enumerate(facts, 1)}
    target_handles = {target['id']: f'T{index:03d}' for index, target in enumerate(targets, 1)}
    if handles.keys() & target_handles.keys():
        raise ValueError('fact_and_target_address_collision')
    addresses = {**handles, **target_handles}
    inverse = {handle: identifier for identifier, handle in addresses.items()}
    wanted = {fid for target in targets for fid in target.get('sourceFactIds', ()) if fid in facts}
    # Earlier reviewed content is a source-bound synthesis input. Its facts
    # also remain readable; an ID without the underlying value is not context.
    def collect_ids(value):
        if isinstance(value, str):
            return {value} if value in facts else set()
        if isinstance(value, (list, tuple)):
            return {fid for item in value for fid in collect_ids(item)}
        if isinstance(value, dict):
            return {fid for item in value.values() for fid in collect_ids(item)}
        return set()
    wanted.update(collect_ids(previous_reviewed))
    scopes, scope_ids, frames, frame_ids, model_targets = [], {}, [], {}, []

    def scope(identifiers):
        key = tuple(dict.fromkeys(handles[fid] for fid in identifiers if fid in handles))
        if key not in scope_ids:
            identity = f'S{len(scopes) + 1}'
            scopes.append({'scopeId': identity, 'factRefs': list(key)})
            scope_ids[key] = identity
        return scope_ids[key]

    for target in targets:
        model = {key: deepcopy(value) for key, value in target.items()
            if key not in {'sourceFactIds', 'owner'}}
        model['id'] = target_handles[target['id']]
        owner = {key: deepcopy(value) for key, value in target.get('owner', {}).items()
            if key not in {'primaryFactIds', 'requiresScientificMeaning'}}
        model['owner'] = _replace(owner, handles)
        model['scopeId'] = scope(target.get('sourceFactIds', ()))
        model['primaryScopeId'] = scope(target.get('owner', {}).get('primaryFactIds', ()))
        person = _json(subject)
        model['patientContext'] = {'name': person.get('name'),
            'gender': person.get('sex', person.get('gender')), 'age': person.get('age')}
        # Explanation wrappers hold no measured values. Their containing row
        # is the source owner; global chapter paragraphs instead use the fact
        # index and source-state annotations below, never a giant copied DTO.
        path = list(target['path'][:-1])
        try:
            raw = (_aggregate_frame(blueprint.data, chapter, target)
                if target.get('owner', {}).get('aggregate') else _json(_node(blueprint.data, path)))
            if (not target.get('owner', {}).get('aggregate') and isinstance(raw, dict)
                    and not any(key in raw for key in _RESULT_FIELDS | {'items', 'related_metrics', 'relatedMetrics', 'observations'})):
                if path and path[-1] in {'explanation', 'clinical_correlation', 'clinicalCorrelation'}:
                    path = path[:-1]
                    raw = _json(_node(blueprint.data, path))
            frame = _readonly(raw, handles)
        except (AttributeError, IndexError, KeyError, TypeError):
            frame = {}
        if frame and isinstance(frame, dict) and (set(frame) - {'factRefs'}):
            key = json.dumps(frame, ensure_ascii=False, sort_keys=True)
            if key not in frame_ids:
                identity = f'R{len(frames) + 1}'
                frame_ids[key] = identity
                frames.append({'frameId': identity, 'result': frame,
                    'distribution': _grade_distribution(frame)})
            model['resultFrameId'] = frame_ids[key]
        model_targets.append(model)

    source_states = []
    for row in chapter.get('sourceRows', ()):
        ids = [fid for fid in row.get('sourceFactIds', ()) if fid in wanted]
        if not ids:
            continue
        state = {key: deepcopy(row[key]) for key in ('code', 'name', 'kind',
            'category', 'measurementType', 'sourceStatus', 'assessmentLevel',
            'displayGrade', 'direction', 'directions', 'detectedGenes',
            'candidateGenes', 'geneIdentityContext') if key in row and row[key] is not None}
        state['factRefs'] = [handles[fid] for fid in ids]
        state = _replace(state, handles)
        if state not in source_states:
            source_states.append(state)
    model_facts = [{'factId': handles[fid], **{key: deepcopy(value)
        for key, value in fact.items() if key in _FACT_FIELDS}}
        for fid, fact in facts.items() if fid in wanted]
    gene_identity = []
    for gene in getattr(blueprint.data, '_template_layout', {}).get('geneDetections', ()):
        bound = [fid for fid in gene.get('sourceFactIds', ()) if fid in wanted]
        if not bound:
            continue
        symbols = list(gene.get('claimObjects') or ())
        gene_identity.append({'factRefs': [handles[fid] for fid in bound],
            'detectedGenes': symbols, 'candidateGenes': list(gene.get('candidateGenes') or ()),
            'annotationVerified': gene.get('annotationVerified') is True})
        # Normalize the model's subject to the actual detected objects. The
        # original report label/value remain in the immutable source snapshot.
        # A vendor's functional heading is not a verified gene identity.
        if gene.get('annotationVerified') is not True:
            for fact in model_facts:
                if fact['factId'] in {handles[fid] for fid in bound}:
                    fact['name'] = '基因检测记录' + ('：' + '、'.join(symbols) if symbols else '')
    focus = [{key: deepcopy(value) for key, value in item.items() if key in {
        'code', 'name', 'certainty', 'factIds', 'sourceFactIds', 'roleReason', 'priority'}}
        for item in blueprint.focus]
    try:
        introduction = _json(_node(blueprint.data, ['introduction']))
    except (AttributeError, KeyError, TypeError):
        introduction = {}
    chronology = [{'sourceCode': source.get('source_code', source.get('sourceCode')),
        'name': source.get('name'),
        'sourceRecordTime': source.get('observed_at', source.get('observedAt')),
        'timeMeaning': ('questionnaire_submission_not_symptom_onset'
            if source.get('source_code', source.get('sourceCode')) == 'B'
            else 'source_record_date_not_current_confirmation')}
        for source in introduction.get('sources', ()) if isinstance(source, dict)]
    from .evaluation_template_contract import REPORT_MEDICAL_SAFETY, PATIENT_TEXT_GUIDANCE
    person = _json(subject)
    payload = {'patientContext': {'name': person.get('name'),
            'gender': person.get('sex', person.get('gender')), 'age': person.get('age')},
        'medicalInterpretationSafety': list(REPORT_MEDICAL_SAFETY),
        'patientTextGuidance': list(PATIENT_TEXT_GUIDANCE),
        'measurementContext': {
            'antimicrobialResistance': _readonly(_json(_field(
                _field(blueprint.data, 'microbiomeAssessment', {}), 'antimicrobialResistance', [])), handles),
            'meaning': '测量值与原等级只读；正常和未检出是不同状态，不凭正常自行写未检出。'},
        'chapter': {key: deepcopy(value) for key, value in chapter.items()
            if key not in {'targets', 'sourceRows'}},
        'targets': model_targets, 'factScopes': scopes, 'facts': model_facts,
        'resultFrames': frames, 'sourceRows': source_states,
        'detectedGeneIdentity': gene_identity,
        'clinicalFocus': _replace(focus, handles), 'subject': _json(subject),
        'acceptedClinicalContext': _replace([{
            'code': _field(row, 'code'), 'name': _field(row, 'name'),
            'certainty': _field(row, 'certainty'), 'system': _field(row, 'system'),
            'directFactRefs': _source_refs(_json(row), handles)}
            for row in _field(_field(blueprint.data, 'medicalAssessment', {}), 'findings', ())], handles),
        'sourceChronology': chronology,
        'previousReviewedChapters': _replace(previous_reviewed, addresses),
        'sourceReadingContract': [
            'factScopes是允许的证据地址集合，不要在正文或输出中抄回整份scope。每句只选支持本句的1至3个代表性factId。',
            'resultFrames是同一来源对象的只读展示结果：直接使用其assessment_level/source_status/detection_status及原值，不用另一组阈值重分级。无等级保持未知；原始方向不生成新等级。',
            'distribution仅逐项计数现有展示等级，不是新评估。数值、箭头、原等级若有边界差异分别如实保留。',
            '事实中的日期按每个来源分别表述；不同日期资料可结合解读，不能写成同次检查或未经记录的复测。',
            'acceptedClinicalContext保留已审疾病/症状名称与原确定性，不降级或改名，不因分类增造主诉；一项阴性只支持其明确对象，未记录不等于没有其他系统症状。',
            'geneIdentityContext.reportedLabel仅为来源栏目标签，annotationVerified=false时不能把该标签当作已核实毒素身份或功能；只保留claimObjects中真实检出对象，不从候选名单补功能。',
            'detectedGeneIdentity在所有章节适用，包含综合通路和总览；annotationVerified=false时只用实际检测基因符号指代，不把原栏目中的毒素/感染身份写成事实，也不在患者文字中解释标签核验规则。结合实际症状给合适的日常行动即可。',
            'aggregate结果框包含该目标整组实际结果；整组总结须阅读全部成员，不只挑前几条。previousReviewedChapters保留已审各章全部段落，可用于综合承接，不丢弃后面的通路。',
            '类别summaryMode为routine_maintenance时只写本类有依据的日常维护，不借其他类别的隐血或代谢问题当本类病因，也不新增癌症结论；有clinicalManagementContext则写对应实际结节等问题的管理。',
            'patientTextGuidance用于所有正文及修复。medicalInterpretationSafety和measurementContext.meaning只约束内部推理，不把这些限制逐项讲给患者。',
            '真实事实的临床整合、同病归纳、个人优先顺序可用clinical_synthesis，并核来源性质、无虚构发现、无新增无据确诊/病因链、无因果升级与相称性；这不要求论文逐字写该患者排序。新增机制、确诊、具体干预或疗效须真实适用原文；applicable_guideline须有实际推荐方向及患者落实前提。',
        ]}
    return ChapterModelPacket(payload, addresses, inverse)
