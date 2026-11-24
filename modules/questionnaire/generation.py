"""Produce a complete, evidence-linked questionnaire without publishing profile facts."""

from __future__ import annotations

import asyncio
from copy import deepcopy

from modules.questionnaire import generation_policy as lifestyle
from modules.questionnaire.evidence import QuestionnaireEvidenceVersionError
from modules.questionnaire.evidence import evidence_rule_selection
from modules.questionnaire.evidence import require_evidence_selection
from modules.questionnaire.generation_policy import GeneratedQuestion
from modules.questionnaire.generation_policy import validate_answer_entry_instructions
from modules.questionnaire.generation_policy import validate_choice_labels
from modules.questionnaire.generation_policy import validate_new_question
from utils.report_flags import report_flag_is_abnormal

BASE_LIFESTYLE = (
    'diet_structure',
    'meal_pattern',
    'exercise_frequency',
    'sleep_duration',
    'smoking',
    'alcohol',
    'stress_coping',
)
GOALS = ('便秘', '反酸', '入睡困难', '容易疲劳', '减轻体重', '增加肌肉', '肩颈疼痛', '缓解压力', '不清楚')
HISTORY = {
    'medical_history': ('高血压', '糖尿病', '血脂异常', '冠心病', '脑卒中', '慢性肾病', '脂肪肝', '不清楚'),
    'family_history': ('高血压', '糖尿病', '冠心病', '脑卒中', '肺癌', '结直肠癌', '地中海贫血', '血友病', '不清楚'),
}
FACETS = frozenset(
    {
        'health_goals',
        'medical_history',
        'family_history',
        'symptom_presence',
        'symptom_timing',
        'symptom_severity',
        'followup_status',
        'followup_result',
        'diagnosis_confirmation',
        'medication_use',
        'medication_detail',
        'exposure',
        'test_context',
        'test_timing',
        'family_history_detail',
        'medical_history_detail',
        'health_goal_detail',
        'surgery_history',
        'surgery_detail',
        'supplement_use',
        'supplement_detail',
        'diet_change',
        'diet_change_detail',
        'recent_infection',
        'infection_detail',
        'bowel_pattern',
        'bowel_change',
        'food_tolerance',
        'food_tolerance_detail',
        'weight_change',
        'weight_change_detail',
        'red_flag_symptoms',
        'red_flag_detail',
        'treatment_response',
        'activity_limitations',
        'exposure_detail',
        'care_support',
        'adherence_barriers',
        *lifestyle.TOPICS,
    }
)
FEMALE_TERMS = ('月经', '经期', '经量', '绝经', '妊娠', '怀孕', '哺乳', '卵巢', '子宫', '妇科', '孕期')
MALE_TERMS = ('前列腺', '睾丸', '精液', '勃起')
PRIORITIES = {'priority': '优先跟进', 'routine': '常规跟进', 'uncertain': '待进一步确认'}

DETAIL_CONDITIONS = {
    'symptom_timing': '如果您有相关不适，',
    'symptom_severity': '如果您有相关不适，',
    'followup_result': '如果您已经做过相关复查，',
    'medication_detail': '如果您使用过相关药物，',
    'medical_history_detail': '如果医生曾明确诊断相关疾病，',
    'family_history_detail': '如果血亲有相关疾病，',
    'surgery_detail': '如果您做过相关手术或治疗，',
    'supplement_detail': '如果您使用过相关补充产品，',
    'diet_change_detail': '如果您近期改变过饮食，',
    'infection_detail': '如果近期发生过相关感染，',
    'food_tolerance_detail': '如果进食后出现过相关不适，',
    'weight_change_detail': '如果体重发生过变化，',
    'red_flag_detail': '如果出现过上述表现，',
    'treatment_response': '如果已经接受过相关处理，',
    'exposure_detail': '如果存在相关接触，',
}


def _text(value, limit=240):
    return value.strip()[:limit] if isinstance(value, str) else ''


def _safe_for_gender(value, gender):
    text = str(value)
    if gender != 'female' and any(term in text for term in FEMALE_TERMS):
        return False
    return not (gender != 'male' and any(term in text for term in MALE_TERMS))


def reported_gender(reports):
    """Use explicit structured report fields only; absence or conflict stays unknown."""
    values = set()
    references = []
    aliases = {
        '男': 'male',
        '男性': 'male',
        'male': 'male',
        'm': 'male',
        '女': 'female',
        '女性': 'female',
        'female': 'female',
        'f': 'female',
    }
    for index, report in enumerate(reports):
        for source in report.get('_questionnaireGenderEvidence', []):
            if isinstance(source, dict) and source.get('value') in {'male', 'female'}:
                values.add(source['value'])
                references.append({'reportIndex': report.get('_questionnaireReportIndex', index), **deepcopy(source)})
        for container in ('subject', 'basicInfo', 'patientInfo'):
            data = report.get(container)
            if not isinstance(data, dict):
                continue
            for key in ('gender', 'sex'):
                raw = data.get(key)
                gender = aliases.get(raw.strip().lower()) if isinstance(raw, str) else None
                if gender:
                    values.add(gender)
                    references.append(
                        {
                            'reportIndex': report.get('_questionnaireReportIndex', index),
                            'pointer': f'/{container}/{key}',
                        }
                    )
    return {
        'value': next(iter(values)) if len(values) == 1 else 'unknown',
        'status': 'conflicting' if len(values) > 1 else ('reported' if values else 'missing'),
        'sources': references,
    }


def build_evidence(reports, gender='unknown', *, limit=16, preserve_values=False):
    """Project actual extracted observations, preserving source pointers internally."""
    evidence = []
    for position, report in enumerate(reports):
        report_index = report.get('_questionnaireReportIndex', position)
        if report.get('reportType') == 'microbiome':
            continue
        children = report.get('sections') or []
        excluded_sections = {
            index
            for index, section in enumerate(children)
            if isinstance(section, dict) and section.get('reportType') == 'microbiome'
        }
        # The shared normalizer mirrors child observations at the root. Filter
        # both the typed child and its root projection, including legacy mirrors.
        excluded_rows = {
            (_text(row.get('itemName'), 80), str(row.get('resultValue')), _text(row.get('unit'), 30))
            for index in excluded_sections
            for row in (children[index].get('observations') or [])
            if isinstance(row, dict)
        }
        sections = [('', report)]
        sections.extend(
            (f'/sections/{i}', section)
            for i, section in enumerate(report.get('sections') or [])
            if isinstance(section, dict) and section.get('reportType') != 'microbiome'
        )
        seen = set()
        report_evidence = []
        for prefix, section in sections:
            for row_index, row in enumerate(section.get('observations') or []):
                if not isinstance(row, dict):
                    continue
                if not prefix and (
                    (isinstance(row.get('sourceSection'), int) and row.get('sourceSection') in excluded_sections)
                    or (_text(row.get('itemName'), 80), str(row.get('resultValue')), _text(row.get('unit'), 30))
                    in excluded_rows
                ):
                    continue
                name = _text(row.get('itemName'), 80)
                raw = row.get('resultValue')
                value = (
                    str(raw)
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool)
                    else (raw.strip() if preserve_values and isinstance(raw, str) else _text(raw, 120))
                )
                date = _text(row.get('observedAt') or section.get('reportTime') or report.get('reportTime'), 40)
                key = (name, value, _text(row.get('unit'), 30), date)
                if not name or not value or key in seen or not _safe_for_gender(name, gender):
                    continue
                seen.add(key)
                report_evidence.append(
                    {
                        'id': f'r{report_index + 1}_{len(seen)}',
                        'reportIndex': report_index,
                        'pointer': f'{prefix}/observations/{row_index}',
                        'name': name,
                        'value': value,
                        'unit': key[2],
                        'date': date,
                        'abnormal': report_flag_is_abnormal(row.get('flag')),
                        'reference': _text(row.get('referenceRange'), 100),
                    }
                )
            expansion = section.get('expansion')
            if isinstance(expansion, dict) and (prefix or not excluded_sections):
                for field in ('findings', 'diagnosis'):
                    raw_conclusion = expansion.get(field)
                    conclusion = (
                        raw_conclusion.strip()
                        if preserve_values and isinstance(raw_conclusion, str)
                        else _text(raw_conclusion, 180)
                    )
                    name = _text(section.get('title') or section.get('reportType'), 80) or '影像检查'
                    key = (name, conclusion)
                    if conclusion and key not in seen and _safe_for_gender(name + conclusion, gender):
                        seen.add(key)
                        report_evidence.append(
                            {
                                'id': f'r{report_index + 1}_{len(seen)}',
                                'reportIndex': report_index,
                                'pointer': f'{prefix}/expansion/{field}',
                                'name': name,
                                'value': conclusion,
                                'unit': '',
                                'date': _text(section.get('reportTime') or report.get('reportTime'), 40),
                                'abnormal': False,
                                'reference': '',
                            }
                        )
        evidence.extend(sorted(report_evidence, key=lambda row: not row['abnormal'])[:limit])
    return evidence


def _choice(name, title, choices, *, multiple=False, description='', none_text=None):
    result = {
        'name': name,
        'type': 'checkbox' if multiple else 'radiogroup',
        'title': title,
        'choices': [{'value': item, 'text': item} for item in choices],
    }
    if description:
        result['description'] = description
    if none_text:
        result.update(showNoneItem=True, noneText=none_text)
    return result


def _summary(evidence):
    if not evidence:
        return '请填写这份基础健康问卷，帮助我们了解您的健康目标、既往情况和日常习惯。'
    rows = sorted(evidence, key=lambda item: not item['abnormal'])[:4]
    values = [' '.join(filter(None, (row['name'], row['value'], row['unit']))) for row in rows]
    return '报告记录：' + '；'.join(values) + '。这些是检查记录，仍需结合检查时间和个人情况理解。'


def _linked(row, evidence):
    ids = row.get('evidenceIds')
    if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or key not in evidence for key in ids):
        return []
    return list(dict.fromkeys(ids))[:4]


def _facet(row):
    value = row.get('informationFacet')
    if not isinstance(value, str) or value not in FACETS:
        return None
    question = _text(row.get('title') or row.get('question'), 180)
    # A planner cannot evade existence/detail deduplication just by relabeling
    # a repeated closed question as an open follow-up facet.
    if not any(word in question for word in ('结果', '哪天', '日期', '数值', '多少')):
        if any(word in question for word in ('是否复查', '有没有复查', '复查过', '复测过')):
            return 'followup_status'
    return value


def build_questionnaire(evidence, plan, gender='unknown', *, basic_reason=None):
    """Validate one planner result, deduplicate information facets across both audiences."""
    by_id = {item['id']: item for item in evidence}
    selected = plan.get('lifestyleKeys', []) if plan else []
    selected = selected if isinstance(selected, list) else []
    keys = list(
        dict.fromkeys([*(key for key in selected if isinstance(key, str) and key in lifestyle.TOPICS), *BASE_LIFESTYLE])
    )[:7]
    risks, tests, followups, interview = [], [], [], []
    sources = {}
    factor_bindings = []
    reserved = {'health_goals', 'medical_history', 'family_history', *keys}
    used = set(reserved)
    seen_questions = set()
    for name in reserved:
        sources[name] = {'informationFacet': name, 'evidenceRefs': [], 'origin': 'template'}
    if plan and basic_reason is None:
        for row in plan.get('risks', []):
            if not isinstance(row, dict) or not _safe_for_gender(row, gender):
                continue
            refs = _linked(row, by_id)
            assessment, priority = _text(row.get('assessment')), row.get('priority')
            if (
                refs
                and assessment
                and priority in PRIORITIES
                and assessment not in [item['assessment'] for item in risks]
            ):
                risks.append({'priority': PRIORITIES[priority], 'assessment': assessment, 'evidenceRefs': refs})
        for row in plan.get('tests', []):
            if not isinstance(row, dict) or not _safe_for_gender(row, gender):
                continue
            refs = _linked(row, by_id)
            name, reason = _text(row.get('name'), 60), _text(row.get('reason'), 140)
            if refs and name and reason and name not in [item['name'] for item in tests]:
                tests.append({'name': name, 'reason': reason, 'evidenceRefs': refs})
        for row in plan.get('questions', []):
            if not isinstance(row, dict) or not _safe_for_gender(row, gender):
                continue
            facet, refs = _facet(row), _linked(row, by_id)
            title, description = _text(row.get('title'), 100), _text(row.get('description'), 180)
            choices = row.get('choices')
            if not isinstance(choices, list) or any(not isinstance(item, str) for item in choices):
                continue
            choices = list(dict.fromkeys(item.strip() for item in choices))
            if any(marker in choice for choice in choices for marker in ('拒答', '拒绝回答', '暂不透露', '暂不提供')):
                continue
            try:
                # Validate the actual published labels before the legacy
                # question model can strip any embedded input instructions.
                validate_choice_labels(choices)
                validate_new_question(
                    GeneratedQuestion(
                        question=title,
                        questionType='checkbox' if row.get('multiple') is True else 'radio',
                        options=choices,
                    ),
                    action='ask_intake',
                )
                validate_answer_entry_instructions(description, options=choices)
            except ValueError:
                continue
            if (
                not facet
                or facet in used
                or not refs
                or not title
                or title in seen_questions
                or not description
                or not 2 <= len(choices) <= 7
                or not all(choices)
                or any(word in title for word in ('请填写', '请描述', '请上传', '请提供', '请补充', '请写下'))
                or any(value.lower() == 'none' for value in choices)
            ):
                continue
            name = f'report_follow_up_{len(followups) + 1}'
            followups.append(
                _choice(name, title, choices, multiple=row.get('multiple') is True, description=description)
            )
            sources[name] = {'informationFacet': facet, 'evidenceRefs': refs, 'origin': 'report'}
            for binding in row.get('factorConditions', []) if isinstance(row.get('factorConditions'), list) else []:
                if isinstance(binding, dict):
                    factor_bindings.append({**deepcopy(binding), 'questionName': name})
            used.add(facet)
            seen_questions.add(title)
            if len(followups) == 5:
                break
        for row in plan.get('interview', []):
            if not isinstance(row, dict) or not _safe_for_gender(row, gender):
                continue
            facet, refs = _facet(row), _linked(row, by_id)
            question, reason = _text(row.get('question'), 180), _text(row.get('reason'), 140)
            if not facet or facet in used or not refs or not question or not reason or question in seen_questions:
                continue
            used.add(facet)
            seen_questions.add(question)
            condition = DETAIL_CONDITIONS.get(facet)
            if condition and not question.startswith(('如果', '若')):
                question = condition + question
            interview.append({'question': question, 'reason': reason, 'informationFacet': facet, 'evidenceRefs': refs})
            if len(interview) == 8:
                break
    bridges = {}
    for row in plan.get('bridges', []) if plan and isinstance(plan.get('bridges', []), list) else []:
        if not isinstance(row, dict) or not _safe_for_gender(row, gender) or basic_reason is not None:
            continue
        name, description, refs = row.get('name'), _text(row.get('description'), 180), _linked(row, by_id)
        if name not in {*keys, 'medical_history'} or not description or not refs:
            continue
        try:
            validate_answer_entry_instructions(description)
        except ValueError:
            continue
        bridges.setdefault(name, description)
        sources[name] = {'informationFacet': name, 'evidenceRefs': refs, 'origin': 'report_bridge'}
    for binding in plan.get('factorBindings', []) if isinstance(plan.get('factorBindings'), list) else []:
        if isinstance(binding, dict) and binding.get('questionName') in reserved:
            factor_bindings.append(deepcopy(binding))
    risks, tests = risks[:3], tests[:4]
    risk_note = '；'.join(f'{row["priority"]}：{row["assessment"]}' for row in risks) if risks else ''
    opening = _summary(evidence)
    if risk_note:
        opening += '\n简要风险分析：' + risk_note
    background = [
        _choice(
            'health_goals',
            '您最近有哪些身体不适，或希望改善哪些方面？',
            GOALS,
            multiple=True,
            description=opening,
            none_text='暂无明显问题',
        ),
        _choice(
            'medical_history',
            '医生曾经确诊过您患有以下哪些疾病？',
            HISTORY['medical_history'],
            multiple=True,
            description=bridges.get('medical_history') or '了解医生已经确认的个人疾病史。',
            none_text='以上均无',
        ),
        _choice(
            'family_history',
            '有血缘关系的亲属患过以下哪些疾病？',
            HISTORY['family_history'],
            multiple=True,
            description='了解血亲家族健康背景。',
            none_text='以上均无',
        ),
    ]
    daily = [
        _choice(
            key,
            lifestyle.TOPICS[key]['question'],
            lifestyle.TOPICS[key]['options'],
            multiple=lifestyle.TOPICS[key]['questionType'] == 'checkbox',
            description=bridges.get(key) or lifestyle.BRIDGES[key],
        )
        for key in keys
    ]
    pages = [{'name': 'health_background', 'elements': background}, {'name': 'daily_habits', 'elements': daily}]
    if followups:
        pages.insert(1, {'name': 'report_follow_up', 'elements': followups})
    message = ''
    if tests:
        lines = '\n'.join(f'{i}. {item["name"]}：{item["reason"]}' for i, item in enumerate(tests, 1))
        message = (
            '您好，我看了您提供的体检报告。建议您带上报告，与医生讨论是否需要以下复查或补充检查：\n'
            + lines
            + '\n如果近期已经做过，可以提供最近一次结果，便于结合本次报告继续跟进。'
        )
    elif evidence:
        message = '您好，我看了您提供的体检报告。请先填写这份健康问卷，帮助我们核实相关背景，再判断后续是否需要复查。'
    else:
        message = '您好，请填写这份基础健康问卷，帮助我们了解您的健康情况，便于后续与您沟通。'
    return {
        'questionnaire': {'title': '健康建档问卷', 'pages': pages},
        'guidance': {
            'reportSummary': _summary(evidence),
            'riskAssessment': risks,
            'riskNote': risk_note,
            'suggestedTests': tests,
            'clientCheckupMessage': message,
            'managerInterview': interview,
        },
        'questionSources': sources,
        'factorBindings': factor_bindings,
    }


class QuestionnaireGenerator:
    VERSION = 'questionnaire-generation-v1'

    def __init__(self, *, documents, planner=None, planner_timeout_seconds=45, factor_pipeline=None):
        self.documents = documents
        self.planner = planner
        self.planner_timeout_seconds = planner_timeout_seconds
        self.factor_pipeline = factor_pipeline

    async def generate(self, *, user_id, questionnaire_id, file_list):
        selection = (
            evidence_rule_selection(self.factor_pipeline.evidence_rule_selection())
            if self.factor_pipeline is not None
            else None
        )
        staged = await self.documents.stage(user_id=user_id, questionnaire_id=questionnaire_id, file_list=file_list)
        reports = staged['reports']
        usable = [
            {
                **item['interpretation'],
                '_questionnaireReportIndex': item['fileIndex'],
                '_questionnaireGenderEvidence': item.get('genderEvidence', []),
            }
            for item in reports
            if item.get('status') in {'complete', 'partial'} and isinstance(item.get('interpretation'), dict)
        ]
        gender = reported_gender(usable)
        evidence = build_evidence(
            usable,
            gender['value'],
            limit=None if self.factor_pipeline else 16,
            preserve_values=self.factor_pipeline is not None,
        )
        reason = None if evidence else ('no_reports' if not file_list else 'reports_unavailable')
        preparation = None
        if self.factor_pipeline is not None:
            try:
                async with asyncio.timeout(360):
                    preparation = await self.factor_pipeline.prepare(
                        reports=deepcopy(reports), questionnaire_id=questionnaire_id
                    )
                if not isinstance(preparation, dict):
                    raise ValueError('invalid factor preparation')
            except QuestionnaireEvidenceVersionError:
                raise
            except Exception:
                preparation = {
                    'version': 'factor-preparation-unavailable-v1',
                    'evidenceRuleSelection': selection,
                    'pipelineVersion': 'factor-assessment-v1',
                    'status': 'unavailable',
                    'targets': [],
                    'candidates': [],
                    'bindings': [],
                    'coverage': {'status': 'unavailable', 'reason': 'preparation_failed'},
                }
                if evidence:
                    reason = 'factor_preparation_unavailable'
        plan = {}
        if evidence and reason is None:
            try:
                if self.planner is None:
                    raise ValueError('planner unavailable')
                async with asyncio.timeout(self.planner_timeout_seconds):
                    arguments = {'evidence': deepcopy(evidence), 'gender': deepcopy(gender)}
                    if preparation is not None:
                        arguments['factor_context'] = {
                            key: deepcopy(preparation[key])
                            for key in ('version', 'targets', 'candidates', 'questionHints', 'coverage')
                            if key in preparation
                        }
                    plan = await self.planner.plan(**arguments)
                if not isinstance(plan, dict) or any(
                    not isinstance(plan.get(key), list) for key in ('risks', 'tests', 'questions', 'interview')
                ):
                    raise ValueError('invalid questionnaire plan')
            except Exception:
                plan, reason = {}, 'planner_unavailable'
        if self.factor_pipeline is not None:
            require_evidence_selection(self.factor_pipeline.evidence_rule_selection(), selection)
        built = build_questionnaire(evidence, plan, gender['value'], basic_reason=reason)
        if preparation is not None and preparation.get('status') != 'unavailable':
            preparation = self.factor_pipeline.bind_questions(
                preparation, built['questionnaire'], {'factorBindings': built['factorBindings']}
            )
        result = {
            'reports': reports,
            'guidance': built['guidance'],
            'questionnaire': built['questionnaire'],
            'mode': 'basic' if reason else 'report_informed',
            'processing': {
                'status': 'fallback' if reason and file_list else 'complete',
                'reason': reason,
                'reportCount': len(reports),
                'usableReportCount': len(usable),
            },
            'provenance': {
                'version': 'questionnaire-generation-v1',
                'gender': gender,
                'evidence': evidence,
                'questionSources': built['questionSources'],
            },
            'artifacts': staged['artifacts'],
        }
        if preparation is not None:
            result['factorPreparation'] = preparation
        return result

    async def ready_artifacts(self, snapshot, expires_at):
        await self.documents.ready_artifacts(snapshot, expires_at)

    async def pin_artifacts(self, snapshot):
        await self.documents.pin_artifacts(snapshot)

    async def expire_artifacts(self, snapshot):
        await self.documents.expire_artifacts(snapshot)

    async def read_artifact(self, *, user_id, artifact):
        return await self.documents.read_artifact(user_id=user_id, artifact=artifact)
