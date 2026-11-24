"""Plan the reader's questions and source relationships before drafting prose.

The plan contains source handles and editorial responsibilities, never a second
set of patient findings. A separate model reviews the complete plan. The normal
writer still authors and medically reviews every published paragraph.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from time import monotonic

from .assessment_red_flags import RULES_VERSION as RED_FLAG_RULES_VERSION
from .assessment_red_flags import advice_followups
from .assessment_red_flags import evaluate_red_flags
from .assessment_test_gaps import RULES_VERSION as TEST_GAP_RULES_VERSION
from .assessment_test_gaps import evaluate_test_gaps

PLAN_VERSION = 'assessment-content-plan.v10-rule-priority'
_SECTIONS = ('clusters', 'symptoms', 'lifestyleFactors', 'recognitions', 'cellScopes')
_CHECKS = (
    'sourceBindingsValid',
    'sourceNaturePreserved',
    'personalHistoryNotInvented',
    'relatedObservationsSelected',
    'problemsIntegratedWithoutDuplication',
    'clinicalPrioritySupported',
    'noUnsupportedCausation',
    'themesAreSharedManagementQuestions',
    'normalAndRoutineFindingsNotSeparateClusters',
)
_ID_FIELDS = (
    'factIds',
    'clinicalFactIds',
    'microbiomeFactIds',
    'lifestyleFactIds',
    'contextFactIds',
    'goalFactIds',
    'conflictFactIds',
)
_RELATION_KINDS = {
    'clinical_context',
    'diet_context',
    'functional_context',
    'normal_counterpoint',
    'different_assay_context',
    'co_management',
}
_PLAN_RULES = [
    '只规划这位受检者的阅读问题、真实事实关系和栏目职责，不撰写医学成稿，不新增测量、病史或诊断。',
    '从原始检查、问卷和菌群结果共同理解实际健康主题，不按单个异常、检验数量或疾病目录一项一簇。'
    '存在共同管理目标且事实确实相关时整合；常规随访小发现不挤占主要健康主题。',
    'clusters不是记录归档，不要求所有记录归入集群。不要创建“其他发现”“正常筛查”或整章菌群目录'
    '这种收纳桶；这些事实仍会完整进入原始临床/菌群表与随访层。'
    '有共同可改变环节的临床问题应围绕同一个管理问题整合，不能按疾病名逐项设簇。',
    '主集群须围绕综合问题，不把一个常规随访问题加几项正常化验包装成多维主题。'
    '临床查因之外，实际不适与恢复、成组功能潜能偏移与膳食支持若构成重要共同问题，也应有清楚职责。'
    '没有宿主营养缺乏诊断不妨碍解释有来源的功能支持方向，但不因此补造缺乏。',
    'symptoms只放实际回答成立的不适，不放减重、控糖等改善目标。每个不适单独选择相关临床、'
    '微生态和日常背景。问题里列出症状不代表有该症状；某个复合问题的否定不抹掉其他明确回答。',
    'symptoms.factIds只放实际主诉原文来源，相关检查、微生态和行为分别放对应列的编号。'
    '已有symptomSlots可复用code，不限制从其他真实来源识别明确不适。'
    '症状的临床列优先选会改变临床解释的实际异常或正常对照；代谢背景不能挤掉直接相关的排查依据。'
    '有价值的否认可选作contextFactIds，保留其题目限定、范围和时点，不能并入肯定主诉或否认其他题的症状。',
    'lifestyleFactors.factIds只放行为或客观场景本身。直接影响活动、饮食等选择的真实临床限制及相关结果放'
    'contextFactIds。clusterCodes由服务器按已审事实连接，请填空数组。',
    '已标为reported_symptom、病史、家族背景或管理目标的facet不能单独作为lifestyleFactors的行为主锚；'
    '症状在symptoms解读，临床发现和目标可作为真实行为的背景。',
    'clusters和symptoms的clinicalFactIds含多个不同所见时，clinicalSelections用recordId与完整'
    'patientRows.sourceSpans.quote选本主题真正采用的原子结果。共同检查编号不代表全部病灶都相关，'
    '同一长记录中的正常描述和其他器官不能自动进入本主题信号。普通独立标量填空数组。',
    'symptomSlots中reported_symptom的明确肯定不适必须保留为主诉；回顾性不适保留原时间条件，'
    '不能因近期另题否认或混有管理目标而清空。某列关联不充分时删去该列不成立的关系，不能删主诉。',
    '复合临床行为记录有多个patientRows时，lifestyleFactors.sourceSelections用recordId和quote选择'
    '本主题实际采用的完整reported_behavior原文片段；可以选同一记录中多个确实相关片段。'
    'quote须逐字复制完整sourceSpans.quote，不能摘掉否定、时间或条件。普通独立问卷记录填空数组。',
    'clusters通常为少量综合健康主题，不为凑数制造主题。clinicalFactIds选择改变临床判断的实测锚点；'
    'microbiomeFactIds选择与该主题有关的样本构成、功能或来源关联。保留不同证据层级，'
    '不把粪便DNA、相对丰度或功能潜能改写成人体缺乏、活性感染、病因或个人患病概率。',
    'clinicalFactIds只能引用kind=clinical，microbiomeFactIds只能引用kind=microbe/function/context。'
    '问卷的原话、家族史、主诉、行为和目标只放factIds或lifestyleFactIds，不能混入clinicalFactIds。'
    'factIds不必重复其他列已选编号，输出只保留能改变本主题结论的代表性记录。',
    'microbiomeFactIds须选择sourceCells属于organisms或function的实际分项锚点。'
    '整体评分、GMWI和“异常/检出”等汇总或疾病关联标签可作背景，不能代替具体菌种丰度、'
    '原功能评分或明确序列结果。只选与本主题确有意义的分项，不能为凑联系借无关分项。',
    'P0只授予覆盖priorityItems的主题。priorityItems由程序按危险信号规则（检验阈值、影像分级、心电图、'
    '原检查建议明确要求尽快就诊）判定；菌群分档、一般微小异常不能触发P0，也不能仅因数值偏离获得更高阅读优先级。',
    'P0表示优先完成有依据的临床查因，不等于急诊；保留priorityItems的查因职责，不能改成“先判断是否需要查因”。'
    'routineFollowups中的结节、息肉、囊肿等常规随访留在临床表及随访层，不单独成簇。',
    '存在异常检查结果时至少规划一个主题；单一临床问题有独立管理目标时可以单独成簇。'
    '全部检查正常、没有需要共同管理的问题时clusters返回空数组，不为凑数制造主题。',
    '临床查因集群只保留实际临床与主诉线索，不把同一人的候选菌种、序列或功能评分连成查因依据。'
    '微生态维度中的其他可能关系只供源记录待解释，直到后续独立证据审核通过。',
    '同一测量的原值、复算值和来源镜像不是多个独立问题。不同日期、方法或体格测量场景分别保留，'
    '没有可比历次结果时不编造变化。设备建议目标不是已经发生的体重或功能变化。',
    '记录的sourceSemantics和clinicalSourceProjection规定检测对象与可用的个人陈述。通用教育说明'
    '不属于个人结果；主检个人摘要与原始检验冲突时保留这一冲突，不从摘要后续通用解释新增病情。',
    '有sourceObservation时，以其中明确的检测对象、测量性质、状态、原分档及原句规划。'
    '未提供的采集条件、检出状态或方法限值保持未提供；不能用字段代码或参考范围补齐。'
    '完整原字面记录另行私有追溯，不用模糊原标签取代已明确的序列靶标或相对丰度面板含义。',
    'clinicalFactIds须有direct_measurement、derived_measurement、exam_finding或physical_finding实际观察。'
    'summary_patient_claim用于来源冲突的factIds背景，不作为独立临床测量；reported_behavior放行为背景。',
    'lifestyleFactors只来自实际回答或客观相关记录；不猜饮食偏好、压力、睡眠质量、用药、'
    '益生菌使用、饮水量、时长及暴露。保留题目条件，合并选项不拆成某一项确定行为。',
    '原问题的回顾时间条件不能省略或扩大为现在；原件的通用知识说明不是受检者的个体结论。'
    'qualified_history_context保留原替代范围，不以extractedFacet单独证明疾病史。',
    'recognitions只列本人的已有理解、管理目标与实测发现之间确有来源的差异。'
    '家族史始终属于相应亲属，不改成本人诊断。正常结果不能证明完成了未做的筛查。',
    'recognitions只允许source_conflict或goal_alignment：前者有互相冲突的实际来源，后者有明确个人目标'
    '与客观基线。普通家族史、既往史、设备背景和检查盘点留在相应章节，不伪装成认知误区。'
    '受检者没有声称某种因果时，不以纠正这种假想认知另造一行；没有合适项目就返回空数组。',
    'purpose说明本组事实需要解决的具体问题与应保留的界限，不写规则性占位正文、'
    '资料处理流程、空泛疾病排除或与这组结果无关的机制。',
    'name与purpose本身也是个人断言，须一并核对时间与对象。共同管理不等于同次发生；'
    'eventDate/eventId未知或eventLinkEstablished=false时，主题名用查因、经历复核或共同管理问题表述，'
    '不能把回顾症状和有日期的检验在标题里写成同次伴随状态，再只在正文补时间限制。'
    '保留原始测项的采集条件；旧字段code或另一项明示空腹的测量不能给未明示条件的项目补上空腹。',
    'cellScopes逐格独立选择有用背景，不靠共同集群把整簇所有记录灌入每格。'
    '本格factIds由服务器确定，其他确有用途的事实放contextFactIds并说明relations。'
    '优先阅读templateDuty：明确功能的生理意义、本次格局、真实临床/饮食及跨表管理意义是不同层级；'
    '可以共用管理方向，无需先把菌群预测证明成人体缺乏，不能仅因不构成因果就删去所有相关背景。',
    '逐格寻找实际直接相关的对应来源：功能预测与对应功能菌底座、原临床对象、真实食物记录分别理解。'
    '正常与偏离成员一同用于判断。没有额外个人背景时，仍可规划主对象本身成立的功能解释；'
    'contextPurpose写本格成立的实际问题，不写禁用清单或复述原分数。',
    'assayGroups、sourceSemantics说明不同面板与图谱的对象和方法。一个面板的全零只能概括该面板，'
    '不能泛化为所有同类生物。statusReferenceComparable=false时状态描述与参考范围属于不同比较基准，'
    '不能把两种可并存的说法规划成来源冲突。',
    'sourceSemantics.assayIds关联共享assayGroups中的id，目录只提供一份。按organismKind寻找同类'
    '其他检测，按type与measurementBasis区分图谱、面板和序列；成员与覆盖状态从目录读取，'
    '不要把同一记录的多个面板身份当成多次独立检测。',
    'reportedTemporalContext给出主诉或行为的时间作用域；回顾事件与有日期的检验没有明确共同标本/事件'
    '来源时，仅作clinical_context，不写成同次、同期或当时检验。currentStateConfirmed=false的经历'
    '不升级为当前持续症状，所有层级均保留原条件。',
    '只使用提供的事实编号，保留原始性质和原结果；模板栏目是内容职责，不是可以借用的患者资料。',
]


_OBSERVATION_ROLES = {'direct_measurement', 'derived_measurement', 'exam_finding', 'physical_finding'}
_NON_BEHAVIOR_ROLES = {
    'reported_symptom',
    'reported_history',
    'qualified_history_context',
    'management_goal',
    'reported_goal',
}
# Program fallback when no reviewed lifestyle row survives: one row per
# dimension of section 5.2, each bound only to real questionnaire answers.
_LIFESTYLE_DIMENSIONS = (
    ('sleep', '睡眠', r'睡眠|睡多|入睡|失眠|作息|熬夜'),
    ('exercise', '运动与久坐', r'运动|锻炼|久坐|坐着|步行|活动量|健身'),
    ('drinks', '饮酒与饮品', r'饮酒|喝酒|酒精|饮水|喝水|咖啡|浓茶|饮品'),
    ('smoking', '吸烟', r'吸烟|抽烟|烟草|二手烟'),
    ('stress', '精神压力', r'压力|情绪|焦虑|紧张|心情|抑郁'),
    ('oralCare', '口腔护理', r'刷牙|牙线|口腔清洁|漱口'),
    ('diet', '饮食与进餐', r'正餐|饮食|甜食|含糖|零食|夜宵|咸|油腻|油炸|蔬菜|水果|主食|早餐|进餐|吃'),
)
_LIFESTYLE_EXCLUDED_QUESTIONS = re.compile(
    r'不适|症状|疾病|诊断|药|保健品|体重|腰围|大便|排便|便秘|腹痛|心慌|乏力|空腹|月经|绝经|家族|血亲|隐血|血脂|血糖'
)
_SUMMARY_RECORD = re.compile(r'^\s*(?:主检结论|总检结论|主检|小结|结论)')


def _clinical_literal(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).rstrip('。；;，,')


def _observation_quotes(fact):
    return [
        span['quote']
        for item in fact.get('_clinicalPatientRows', ())
        if item.get('role') in _OBSERVATION_ROLES
        for span in item.get('sourceSpans', ())
    ]


def _behavior_anchor(fact):
    """Whether a record can be the primary anchor of a lifestyle row."""
    if (fact.get('_reportedSourceRole') or {}).get('role') in _NON_BEHAVIOR_ROLES:
        return False
    if fact.get('kind') == 'clinical':
        return any(item.get('role') == 'reported_behavior' for item in fact.get('_clinicalPatientRows', ()))
    return fact.get('kind') == 'self_reported'


def _fact_id(row):
    return row.get('factId', row.get('fact_id'))


def _snapshot(blueprint, subject=None):
    from .assessment_clinical_observation import model_clinical_observation
    from .assessment_microbiome_view import source_semantic_context
    from .assessment_presentation import assessment_template_duty
    from .assessment_source_context import assay_catalog_context
    from .assessment_source_context import assessment_reference_date
    from .assessment_source_context import model_microbiome_observation
    from .assessment_source_context import model_source_semantics
    from .assessment_source_context import source_context_handles
    from .clinical_source_projection import get_patient_projection
    from .clinical_source_projection import patient_projection_context
    from .clinical_source_projection import reported_temporal_context

    facts = {_fact_id(row): deepcopy(row) for row in blueprint.facts if _fact_id(row)}
    clinical_projections = getattr(blueprint, 'assessment_sources', {}).get('clinicalPatientProjections', {})
    microbiome_semantics = getattr(blueprint, 'assessment_sources', {}).get('microbiomeSourceSemantics', {})
    function_duties = getattr(blueprint, 'assessment_sources', {}).get('functionCellDuties', {})
    handles = {fact_id: f'F{index:04d}' for index, fact_id in enumerate(facts, 1)}
    records = []
    for fact_id, row in facts.items():
        record = {'id': handles[fact_id]}
        for key in ('kind', 'name', 'value', 'unit', 'status', 'date', 'abnormal', 'imaging', 'source'):
            if row.get(key) is not None:
                record[key] = deepcopy(row[key])
        reference = row.get('referenceRange', row.get('reference_range'))
        if reference:
            record['referenceRange'] = reference
        if row.get('reportedAssertionKind', row.get('reported_assertion_kind')):
            record['assertionKind'] = row.get('reportedAssertionKind', row.get('reported_assertion_kind'))
        if row.get('kind') == 'clinical':
            projection = get_patient_projection(row, projection_index=clinical_projections)
            if projection is not None:
                record['value'] = projection.get('patientValue')
                record['clinicalSourceProjection'] = source_context_handles(
                    patient_projection_context(projection), handles
                )
                row['_clinicalPatientObservation'] = any(
                    item.get('role')
                    in {'direct_measurement', 'derived_measurement', 'exam_finding', 'physical_finding'}
                    for item in projection.get('patientRows', ())
                )
                row['_clinicalPatientRows'] = deepcopy(projection.get('patientRows', ()))
            observation = model_clinical_observation(row, projection_index=clinical_projections)
            if observation is not None:
                record['sourceObservation'] = source_context_handles(
                    {key: value for key, value in observation.items()
                     if key not in {'literalSource', 'observationIds'}}, handles,
                )
                row['_contentLiteralSource'] = deepcopy(observation.get('literalSource'))
                record['name'] = observation['label']
                for field in ('value', 'unit', 'status'):
                    record[field] = deepcopy(observation.get(field))
        if fact_id in microbiome_semantics:
            semantics = [source_semantic_context(item) for item in microbiome_semantics[fact_id]]
            record['sourceSemantics'] = [
                model_source_semantics(item, handles) for item in semantics
            ]
            observation = model_microbiome_observation(row, semantics)
            if observation is not None:
                record['sourceObservation'] = source_context_handles(
                    {key: value for key, value in observation.items() if key != 'literalSource'}, handles,
                )
                row['_contentLiteralSource'] = deepcopy(observation.get('literalSource'))
                # This is the same admitted semantic observation, not a
                # second per-record catalogue. Full provenance stays private.
                record.pop('sourceSemantics', None)
                record['name'] = observation['label']
                record['value'] = deepcopy(observation['value'])
                record['status'] = deepcopy(observation['status'])
                record['unit'] = deepcopy(observation['unit'])
        records.append(record)
    scopes = []
    for chapter in blueprint.chapters:
        for target in chapter['targets']:
            own = target.get('owner') or {}
            code = target['path'][1] if target['path'][0] == '_assessment_editorial' else own.get('contentScopeCode')
            if tuple(target['path'][:2]) == ('microbiome_assessment', 'structure_interpretation'):
                code = 'structureSummary'
            if code is None:
                continue
            duty = function_duties.get(code) or assessment_template_duty(code)
            scopes.append(
                {
                    'code': code,
                    'name': own.get('name'),
                    'question': assessment_template_duty(code),
                    'templateDuty': deepcopy(duty),
                    'factIds': [handles[fid] for fid in own.get('primaryFactIds', ()) if fid in handles],
                }
            )
    scopes = list({row['code']: row for row in scopes}.values())
    existing = {row['code'] for row in scopes}
    for code, name in (
        ('clinicalLead', '临床发现整体判断'),
        ('sampleSummary', '样本与实际检测条件'),
        ('fermentationSummary', '有来源的消化耐受与发酵线索'),
        ('layer.clinical', '临床查因与器质随访层'),
        ('layer.core', '共同健康主题与微生态协同层'),
        ('layer.maintenance', '长期营养与恢复维护层'),
    ):
        if code not in existing:
            scopes.append(
                {
                    'code': code,
                    'name': name,
                    'question': assessment_template_duty(code),
                    'templateDuty': assessment_template_duty(code),
                    'factIds': [],
                }
            )
    memberships = {}
    for cell in scopes:
        if cell['code'].startswith(
            ('body.', 'clinical.', 'clinicalRow.', 'digestive.', 'structure.', 'organisms.', 'function.')
        ):
            for identifier in cell['factIds']:
                memberships.setdefault(identifier, []).append(cell['code'])
    for record in records:
        record['sourceCells'] = list(dict.fromkeys(memberships.get(record['id'], ())))
    for fid, row in facts.items():
        row['_contentSourceCells'] = list(dict.fromkeys(memberships.get(handles[fid], ())))
    problems = []
    for row in blueprint.focus:
        problem = {
            key: deepcopy(row[key])
            for key in ('code', 'name', 'category', 'certainty', 'role', 'focusLabel', 'originalAdvice')
            if key in row
        }
        problem['factIds'] = [
            handles[fid] for fid in row.get('factIds', row.get('originalFactIds', ())) if fid in handles
        ]
        problems.append(problem)
    source_context = deepcopy(getattr(blueprint, 'assessment_sources', {}))
    # The model receives the same admitted spans beside each record; do not
    # also send a second raw/private clinical catalogue through this metadata.
    source_context.pop('clinicalPatientProjections', None)
    source_context.pop('microbiomeSourceSemantics', None)
    # The full group inventory is for a writer's coverage checks. Planning
    # already has the admitted records and does not need a second catalogue.
    source_context.pop('clinicalObservationGroups', None)
    source_context['activityConstraintGroups'] = source_context_handles(
        source_context.get('activityConstraintGroups', []), handles,
    )
    reported_roles = source_context.pop('reportedSourceRoles', {})
    temporal_contexts = source_context.pop('reportedTemporalContexts', {})
    source_by_handle = {handle: fid for fid, handle in handles.items()}
    for record in records:
        fid = source_by_handle[record['id']]
        if fid in reported_roles:
            record['reportedSourceRole'] = deepcopy(reported_roles[fid])
            facts[fid]['_reportedSourceRole'] = deepcopy(reported_roles[fid])
        temporal = temporal_contexts.get(fid) or reported_temporal_context(
            facts[fid],
            reported_role=reported_roles.get(fid),
        )
        if temporal:
            record['reportedTemporalContext'] = deepcopy(temporal)
            facts[fid]['_reportedTemporalContext'] = deepcopy(temporal)
            if record.get('kind') == 'self_reported':
                recorded_at = record.pop('date', None)
                reported_at = temporal.get('reportedAt') or recorded_at
                if reported_at is not None:
                    record['reportedAt'] = deepcopy(reported_at)
        if record.get('kind') == 'self_reported':
            from .assessment_reported_observation import model_reported_observation
            from .assessment_reported_observation import model_reported_role

            observation = model_reported_observation(facts[fid],
                reported_role=reported_roles.get(fid), temporal_context=temporal)
            if observation is not None:
                facts[fid]['_contentLiteralSource'] = deepcopy(observation.pop('literalSource'))
                record['sourceObservation'] = source_context_handles(observation, handles)
                record['name'], record['value'] = observation['label'], deepcopy(observation['value'])
                record['reportedSourceRole'] = model_reported_role(reported_roles.get(fid) or {})
    source_context['assayGroups'] = assay_catalog_context(source_context.get('assayGroups', ()), handles)
    for slot in source_context.get('symptomSlots', ()):
        slot['factIds'] = [handles[fid] for fid in slot.get('factIds', ()) if fid in handles]
    for action in source_context.get('clinicalActions', ()):
        identifiers = action.pop('sourceFactIds', action.get('recordIds', ()))
        action['recordIds'] = [handles[fid] for fid in identifiers if fid in handles]
    actions_by_code = {action['code']: action for action in source_context.get('clinicalActions', ())}
    for problem in problems:
        if action := actions_by_code.get(problem.get('code')):
            problem['originalAdvice'] = deepcopy(action.get('originalAdvice') or [])
    # P0 and routine follow-up come from the danger-signal rule table, never
    # from the wording of the original report (spec section 4).
    internal_problems = [
        {**{key: deepcopy(problem.get(key)) for key in ('code', 'name', 'role', 'focusLabel', 'category')},
         'originalAdvice': deepcopy(problem.get('originalAdvice') or []),
         'factIds': [source_by_handle[handle] for handle in problem.get('factIds', ()) if handle in source_by_handle]}
        for problem in problems
    ]
    flags = evaluate_red_flags(facts.values(), subject, internal_problems)
    for problem in problems:
        linked = [item['code'] for item in flags['priorityItems'] if problem.get('code') in item['problemCodes']]
        problem['priorityItemCodes'] = linked
        problem['priorityWorkup'] = 'red_flag_rule' if linked else None
        if linked and problem.get('focusLabel') != '优先查因':
            problem['sourceFocusLabel'] = problem.get('focusLabel')
            problem['focusLabel'] = '优先查因'
        elif not linked and problem.get('focusLabel') == '优先查因':
            problem['sourceFocusLabel'] = problem['focusLabel']
            problem['focusLabel'] = '重点管理'
    rules = {
        'priorityItems': flags['priorityItems'],
        'routineFollowups': flags['routineFollowups'],
        'routineProblemCodes': sorted({code for item in flags['routineFollowups'] for code in item['problemCodes']}),
        'testGaps': evaluate_test_gaps(facts.values(), subject),
        'problems': internal_problems,
        'context': flags['context'],
        'versions': {'redFlags': RED_FLAG_RULES_VERSION, 'testGaps': TEST_GAP_RULES_VERSION},
    }
    return (
        facts,
        handles,
        {
            'assessmentDate': assessment_reference_date(),
            'records': records,
            'cells': scopes,
            'clinicalProblems': problems,
            'clinicalTrends': deepcopy(getattr(blueprint, 'clinical_trends', [])),
            'priorityItems': [
                {'code': item['code'], 'title': item['title'], 'timeframe': item['timeframe']['text'],
                 'department': item['department'], 'checks': list(item['checks']),
                 'recordIds': [handles[fid] for fid in item['factIds'] if fid in handles],
                 'problemCodes': list(item['problemCodes'])}
                for item in flags['priorityItems']
            ],
            'routineFollowups': [
                {'code': item['code'], 'title': item['title'], 'interval': item['interval']['text'],
                 'recordIds': [handles[fid] for fid in item['factIds'] if fid in handles],
                 'problemCodes': list(item['problemCodes'])}
                for item in flags['routineFollowups']
            ],
            **source_context,
        },
        rules,
    )


def _contract(sections):
    common = {
        'code': '本次计划内唯一主题标识',
        'name': '具体且有来源的主题名称',
        'factIds': ['F编号'],
        'purpose': '本主题需要回答的问题',
    }
    contract = {
        'clusters': [
            {
                **common,
                'priority': 'P0/P1/P2/P3',
                'clinicalFactIds': ['F编号'],
                'microbiomeFactIds': ['F编号'],
                'problemCodes': ['既有临床问题code'],
                'clinicalSelections': [{'recordId': '临床F编号', 'quote': '完整patientRows.sourceSpans.quote'}],
            }
        ],
        'symptoms': [
            {
                **common,
                'clinicalFactIds': ['F编号'],
                'microbiomeFactIds': ['F编号'],
                'lifestyleFactIds': ['F编号'],
                'contextFactIds': ['具有关联价值且保留范围的对照否定或其他背景F编号'],
                'clinicalSelections': [{'recordId': '临床F编号', 'quote': '完整patientRows.sourceSpans.quote'}],
            }
        ],
        'lifestyleFactors': [
            {
                **common,
                'clusterCodes': [],
                'contextFactIds': ['F编号'],
                'sourceSelections': [
                    {'recordId': '临床行为F编号', 'quote': '完整reported_behavior的sourceSpans.quote'}
                ],
            }
        ],
        'recognitions': [
            {**common, 'kind': 'source_conflict/goal_alignment', 'goalFactIds': ['F编号'], 'conflictFactIds': ['F编号']}
        ],
        'cellScopes': [
            {
                'code': '本批cells提供的code，逐格返回',
                'factIds': ['本格固定主记录F编号，原样保留'],
                'contextFactIds': ['本格直接相关的背景F编号'],
                'purpose': '原样保留本格question的正向模板职责',
                'contextPurpose': '本格有来源的具体解读问题和关系用途',
                'contextStatus': 'related_sources_selected|no_additional_context',
                'relations': [
                    {
                        'recordId': 'contextFactIds中的F编号',
                        'kind': (
                            'clinical_context|diet_context|functional_context|normal_counterpoint|'
                            'different_assay_context|co_management'
                        ),
                        'purpose': '该实际来源帮助解释本格的具体用途',
                    }
                ],
            }
        ],
    }
    return {section: contract[section] for section in sections}


def _specific_microbiome_fact(fact):
    cells = fact.get('_contentSourceCells', ())
    if not any(code.startswith(('function.', 'organisms.')) for code in cells):
        return False
    value = str(fact.get('value') or '').strip()
    if fact.get('kind') in {'microbe', 'function'}:
        return bool(re.fullmatch(r'[<>≤≥]?\s*-?\d+(?:\.\d+)?\s*%?', value))
    # Sequence rows carry their literal gene results, distinct from a generic
    # group-state label. Candidate/detected semantics remain separately bound.
    return 'organisms.genes' in cells and value not in {
        '',
        '正常',
        '异常',
        '检出',
        '未检出',
        str(fact.get('status') or '').strip(),
    }


def _problem_metadata(packet):
    return {row['code']: row for row in packet['clinicalProblems'] if row.get('code')}


def _unlinked_recalled_observations(ids, facts):
    return [
        {'recordId': fid, **deepcopy(temporal)}
        for fid in ids
        if (temporal := facts[fid].get('_reportedTemporalContext') or {}).get('kind') == 'retrospective_event'
        and not (temporal.get('eventId') and temporal.get('mayQualifyDatedClinicalResult') is True)
    ]


def _decode_row(section, source, used, context):
    """Decode one planned row; return ``(row, issues)`` or raise ``ValueError``.

    A raised error or a non-empty issue list affects only this row. The caller
    decides whether to repair, drop or reject it.
    """
    reverse, scopes, facts, handles = context['reverse'], context['scopes'], context['facts'], context['handles']
    problem_codes, items = context['problemCodes'], context['priorityItems']
    content_issues = []
    if not isinstance(source, dict):
        raise ValueError('content_plan_invalid_row')
    row = deepcopy(source)
    code = row.get('code')
    if not isinstance(code, str) or not code.strip() or code in used:
        raise ValueError(f'{section}: content_plan_invalid_code {code!r}')
    if section == 'cellScopes' and code not in scopes:
        raise ValueError(f'cellScopes.{code}: content_plan_unknown_cell')
    for field in _ID_FIELDS:
        ids = row.get(field, [])
        if not isinstance(ids, list):
            raise ValueError(f'{section}.{code}.{field}: expected source ID array')
        unknown = [fid for fid in ids if not isinstance(fid, str) or fid not in reverse]
        if unknown:
            raise ValueError(f'{section}.{code}.{field}: unknown source IDs {unknown}')
        row[field] = list(dict.fromkeys(reverse[fid] for fid in ids))
    if section in {'clusters', 'recognitions'}:
        row['factIds'] = list(dict.fromkeys(fid for field in _ID_FIELDS for fid in row[field]))
    elif section == 'cellScopes':
        original = [reverse[fid] for fid in scopes[code].get('factIds', ()) if fid in reverse]
        row['factIds'] = list(dict.fromkeys(original))
    elif section == 'symptoms':
        affirmed = [fid for fid in row['factIds']
            if (facts[fid].get('_reportedSourceRole') or {}).get('role') == 'reported_symptom']
        if affirmed:
            # A supporting answer does not have the primary assertion's polarity or time.
            row['contextFactIds'] = list(dict.fromkeys([
                *row['contextFactIds'], *(fid for fid in row['factIds'] if fid not in affirmed),
            ]))
            row['factIds'] = affirmed
    if not row['factIds']:
        raise ValueError(f'{section}.{code}: content_plan_unbound_row')
    if section != 'cellScopes' and not isinstance(row.get('name'), str):
        raise ValueError(f'{section}.{code}: content_plan_name_missing')
    if not isinstance(row.get('purpose'), str) or not row['purpose'].strip():
        raise ValueError(f'{section}.{code}: content_plan_purpose_missing')
    recalled = _unlinked_recalled_observations(
        list(dict.fromkeys(fid for field in _ID_FIELDS for fid in row[field])), facts,
    )
    row.pop('unlinkedRecalledObservations', None)
    if recalled:
        row['unlinkedRecalledObservations'] = recalled
    if section == 'symptoms' and any(item['recordId'] in row['factIds'] for item in recalled):
        # Free-text planning cannot turn a separate laboratory result into a result of the recalled event.
        row['name'] = '、'.join(str(facts[fid]['name']) for fid in row['factIds'])
        row['purpose'] = (
            '说明本人回顾的不适及其问题原文限定。分别解释相关检查、对照事实及日常背景，各按原时间范围，'
            '保留回顾事件和有日期检查之间尚未建立的联系。'
        )
    for field, allowed in (
        ('clinicalFactIds', {'clinical'}),
        ('microbiomeFactIds', {'microbe', 'function', 'context'}),
    ):
        mismatches = [
            f'{handles[fid]}={facts[fid].get("kind")}'
            for fid in row[field]
            if facts[fid].get('kind') not in allowed
        ]
        if mismatches:
            content_issues.append(f'{section}.{code}.{field}: wrong source kinds {mismatches}')
    unsupported_clinical = [
        handles[fid] for fid in row['clinicalFactIds'] if facts[fid].get('_clinicalPatientObservation') is False
    ]
    if unsupported_clinical:
        content_issues.append(
            f'{section}.{code}.clinicalFactIds: {unsupported_clinical} '
            'are summary claims, advice or background; '
            'use actual patient observations as clinical anchors and retain conflicting claims only as context'
        )
    if section in {'clusters', 'symptoms'}:
        selections = row.get('clinicalSelections', [])
        if not isinstance(selections, list):
            raise ValueError(f'{section}.{code}.clinicalSelections: expected an array')
        restored_selections = []
        for selection in selections:
            if not isinstance(selection, dict) or selection.get('recordId') not in reverse:
                raise ValueError(f'{section}.{code}.clinicalSelections: unknown recordId')
            fid, quote = reverse[selection['recordId']], selection.get('quote')
            matches = [original for original in _observation_quotes(facts[fid])
                       if isinstance(quote, str) and _clinical_literal(original) == _clinical_literal(quote)]
            if fid not in row['clinicalFactIds'] or len(set(matches)) != 1:
                content_issues.append(
                    f'{section}.{code}.clinicalSelections: {selection["recordId"]} must own a complete '
                    'patientRows.sourceSpans.quote for this theme; preserve conditions and polarity'
                )
            else:
                restored_selections.append({'recordId': fid, 'quote': matches[0]})
        row['clinicalSelections'] = restored_selections
        for fid in row['clinicalFactIds']:
            observations = [
                item
                for item in facts[fid].get('_clinicalPatientRows', ())
                if item.get('role')
                in {'direct_measurement', 'derived_measurement', 'exam_finding', 'physical_finding'}
            ]
            if len(observations) > 1 and not any(item['recordId'] == fid for item in restored_selections):
                content_issues.append(
                    f'{section}.{code}: select the actual clinical observation(s) from {handles[fid]} '
                    'in clinicalSelections; sharing a report does not select its other findings'
                )
    if row['microbiomeFactIds'] and not any(
        _specific_microbiome_fact(facts[fid]) for fid in row['microbiomeFactIds']
    ):
        content_issues.append(
            f'{section}.{code}: microbiome column needs a relevant measured '
            'organism/function/sequence item from sourceCells, not only group states or model labels'
        )
    if section == 'clusters' and row.get('priority') not in {'P0', 'P1', 'P2', 'P3'}:
        raise ValueError(f'clusters.{code}: content_plan_priority_missing')
    if section == 'clusters':
        codes = row.get('problemCodes', [])
        if not isinstance(codes, list) or any(
            not isinstance(item, str) or item not in problem_codes for item in codes
        ):
            raise ValueError(f'clusters.{code}: content_plan_problem_codes_invalid')
        # P0 is granted only by the danger-signal rules. Ownership of a rule
        # item is resolved across all themes once every row is decoded.
        actual = set(row['clinicalFactIds'] or row['factIds'])
        row['_priorityCandidates'] = [
            {'code': item['code'], 'byProblem': bool(set(codes).intersection(item.get('problemCodes', ()))),
             'overlap': len(actual.intersection(item.get('factIds', ())))}
            for item in items
            if set(codes).intersection(item.get('problemCodes', ())) or actual.intersection(item.get('factIds', ()))
        ]
        row['priorityWorkupCodes'] = [item for item in codes if item in context['priorityProblems']]
        row['microbiomeRelationStatus'] = (
            'source_only_pending_interpretation' if row['microbiomeFactIds'] else 'none'
        )
        kinds = {facts[fid]['kind'] for fid in row['factIds']}
        routine_only = set(codes).intersection(context['routineProblemCodes']) or (
            not codes and len(row['clinicalFactIds']) <= 1
        )
        if kinds == {'clinical'} and not row['_priorityCandidates'] and len(codes) <= 1 and routine_only:
            content_issues.append(
                f'clusters.{code}: a single routine follow-up finding is not a management theme; '
                'it is kept in routineFollowups and the clinical table'
            )
    if section == 'recognitions':
        if row.get('kind') == 'source_conflict':
            if len(row['conflictFactIds']) < 2:
                content_issues.append(f'recognitions.{code}: source_conflict needs conflicting source records')
        elif row.get('kind') == 'goal_alignment':
            if not row['goalFactIds'] or any(
                facts[fid]['kind'] != 'self_reported' for fid in row['goalFactIds']
            ):
                content_issues.append(f'recognitions.{code}: goal_alignment needs an explicit personal goal')
        else:
            content_issues.append(
                f'recognitions.{code}: background inventory is not a recognition '
                'item; use source_conflict or goal_alignment, or omit the row'
            )
    if section == 'lifestyleFactors':
        for fid in row['factIds']:
            if not _behavior_anchor(facts[fid]):
                content_issues.append(
                    f'lifestyleFactors.{code}: {handles[fid]} is not a behavior/scenario anchor; '
                    'retain symptoms in symptoms, and clinical findings/history/goals as relevant context'
                )
        selections = row.get('sourceSelections', [])
        if not isinstance(selections, list):
            raise ValueError(f'lifestyleFactors.{code}.sourceSelections: expected an array')
        selected = []
        for selection in selections:
            if not isinstance(selection, dict) or selection.get('recordId') not in reverse:
                raise ValueError(f'lifestyleFactors.{code}.sourceSelections: unknown recordId')
            fid, quote = reverse[selection['recordId']], selection.get('quote')
            admitted = [
                span['quote']
                for item in facts[fid].get('_clinicalPatientRows', ())
                if item.get('role') == 'reported_behavior'
                for span in item.get('sourceSpans', ())
            ]

            def literal_key(text):
                return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).rstrip('。；;')

            restored = [
                original
                for original in admitted
                if isinstance(quote, str) and literal_key(original) == literal_key(quote)
            ]
            if fid not in row['factIds'] or len(set(restored)) != 1:
                content_issues.append(
                    f'lifestyleFactors.{code}.sourceSelections: {selection["recordId"]} needs an owned, '
                    'complete reported_behavior sourceSpans.quote; preserve its conditions'
                )
            else:
                selected.append({'recordId': fid, 'quote': restored[0]})
        row['sourceSelections'] = selected
        for fid in row['factIds']:
            facets = [
                item
                for item in facts[fid].get('_clinicalPatientRows', ())
                if item.get('role') == 'reported_behavior'
            ]
            if len(facets) > 1 and not any(item['recordId'] == fid for item in selected):
                content_issues.append(
                    f'lifestyleFactors.{code}: choose the relevant complete behavior facets from '
                    f'{handles[fid]} in sourceSelections instead of selecting its whole compound record'
                )
    if section == 'cellScopes' and 'contextPurpose' in row:
        if not isinstance(row['contextPurpose'], str) or not row['contextPurpose'].strip():
            raise ValueError(f'cellScopes.{code}: contextPurpose is required')
        if row.get('contextStatus') not in {'related_sources_selected', 'no_additional_context'}:
            raise ValueError(f'cellScopes.{code}: contextStatus is required')
        if row['contextFactIds'] and row['contextStatus'] != 'related_sources_selected':
            raise ValueError(f'cellScopes.{code}: contextStatus does not match selected sources')
        relations = row.get('relations', [])
        if not isinstance(relations, list):
            raise ValueError(f'cellScopes.{code}: relations must be an array')
        converted = []
        for relation in relations:
            if (
                not isinstance(relation, dict)
                or relation.get('recordId') not in reverse
                or relation.get('kind') not in _RELATION_KINDS
                or not isinstance(relation.get('purpose'), str)
                or not relation['purpose'].strip()
            ):
                raise ValueError(f'cellScopes.{code}: invalid source relation')
            fid = reverse[relation['recordId']]
            if fid not in row['contextFactIds']:
                raise ValueError(f'cellScopes.{code}: relations must bind contextFactIds')
            converted.append({**relation, 'recordId': fid})
        if row['contextFactIds'] and not set(row['contextFactIds']) <= {
            relation['recordId'] for relation in converted
        }:
            raise ValueError(f'cellScopes.{code}: explain each selected context source')
        row['relations'] = converted
        row['purpose'] = scopes[code]['question']
    return row, content_issues


def _resolve_priority(clusters, items, facts):
    """Give each P0 rule item to the themes that own it, then fix priorities.

    A theme bound to the item's clinical problem owns it; otherwise the themes
    with the largest overlap of the item's records do. Only owners are P0, so
    a symptom or lifestyle theme that merely cites one of the records keeps
    its own priority.
    """
    owners = {}
    for item in items:
        candidates = [(row, entry) for row in clusters for entry in row.get('_priorityCandidates', ())
                      if entry['code'] == item['code']]
        by_problem = [row for row, entry in candidates if entry['byProblem']]
        if by_problem:
            owners[item['code']] = by_problem
        elif candidates:
            best = max(entry['overlap'] for _, entry in candidates)
            owners[item['code']] = [row for row, entry in candidates if entry['overlap'] == best]
    for row in clusters:
        row.pop('_priorityCandidates', None)
        owned = [item['code'] for item in items if any(owner is row for owner in owners.get(item['code'], ()))]
        if row['priority'] == 'P0' and not owned:
            row.setdefault('sourcePriority', 'P0')
            row['priority'] = 'P1'
            row['purpose'] = '按各自原始结果、资料差异及医生的具体建议说明需复核或管理的事项。'
        elif owned and row['priority'] != 'P0':
            row.setdefault('sourcePriority', row['priority'])
            row['priority'] = 'P0'
        row['priorityItemCodes'] = owned
        if owned:
            pending = [fid for fid in row['factIds'] if facts[fid]['kind'] in {'microbe', 'function'}
                       or fid in row['microbiomeFactIds']]
            row['sourceOnlyPendingMicrobiomeFactIds'] = pending
            for field in _ID_FIELDS:
                row[field] = [fid for fid in row[field] if fid not in pending]
            if pending:
                row['microbiomeRelationStatus'] = 'source_only_pending_interpretation'


def _decode_report(plan, facts, handles, scopes, problem_codes=(), *, symptom_slots=(), check_workup_coverage=True,
                   priority_items=(), routine_problem_codes=()):
    """Decode every planned row independently.

    Returns ``(result, row_issues, section_issues)``. ``row_issues`` maps
    ``(section, index)`` to the messages of a row left out of ``result``;
    ``section_issues`` lists ``(section, message)`` gaps no single row owns
    (an affirmed symptom without a row, a P0 rule item without a theme).
    """
    if not isinstance(plan, dict) or any(not isinstance(plan.get(key), list) for key in _SECTIONS):
        raise ValueError('content_plan_sections_missing')
    reverse = {value: key for key, value in handles.items()}
    scopes = dict(scopes)
    for section, prefixes in (
        ('clusters', ('synthesis.', 'synthesisAction.')),
        ('symptoms', ('symptom.', 'symptomEvidence.')),
        ('lifestyleFactors', ('lifestyle.',)),
        ('recognitions', ('recognition.',)),
    ):
        for row in plan[section]:
            if not isinstance(row, dict) or not isinstance(row.get('code'), str):
                continue
            # A theme/recognition owns the selected observations across all
            # source columns. The model need not repeat these in factIds.
            # Reusing only that column here silently stripped the clinical
            # and microbial anchors from otherwise reviewed cell scopes.
            primary = (
                list(dict.fromkeys(fid for field in _ID_FIELDS if isinstance(row.get(field), list)
                                   for fid in row[field]))
                if section in {'clusters', 'recognitions'}
                else row.get('factIds', [])
            )
            if section == 'clusters':
                pending = set(row.get('sourceOnlyPendingMicrobiomeFactIds', ()))
                primary = [fid for fid in primary if fid not in pending]
            for prefix in prefixes:
                endings = ('.clinical', '.microbiome') if prefix == 'symptomEvidence.' else ('',)
                for ending in endings:
                    scopes.setdefault(prefix + row['code'] + ending, {'factIds': primary})
    items = [item for item in priority_items if isinstance(item, dict) and item.get('code')]
    context = {
        'reverse': reverse,
        'scopes': scopes,
        'facts': facts,
        'handles': handles,
        'problemCodes': problem_codes if isinstance(problem_codes, dict) else {},
        'priorityItems': items,
        'priorityProblems': {code for item in items for code in item.get('problemCodes', ())},
        'routineProblemCodes': set(routine_problem_codes),
    }
    result = {'version': PLAN_VERSION, 'status': 'reviewed'}
    row_issues, section_issues = {}, []
    for section in _SECTIONS:
        rows, used = [], set()
        for index, source in enumerate(plan[section]):
            try:
                row, issues = _decode_row(section, source, used, context)
            except (ValueError, TypeError, KeyError) as error:
                row, issues = None, [str(error)]
            if issues:
                row_issues[(section, index)] = issues
                continue
            used.add(row['code'])
            rows.append(row)
        result[section] = rows
    _resolve_priority(result['clusters'], items, facts)
    for slot in symptom_slots:
        identifiers = {reverse[fid] for fid in slot.get('factIds', ()) if fid in reverse}
        affirmed = {
            fid
            for fid in identifiers
            if (facts[fid].get('_reportedSourceRole') or {}).get('role') == 'reported_symptom'
        }
        if affirmed and not any(affirmed.intersection(row['factIds']) for row in result['symptoms']):
            section_issues.append(('symptoms',
                f'symptoms: retain affirmed symptom {slot.get("name")} from '
                f'{[handles[fid] for fid in affirmed]}; preserve its original time/conditions. '
                'Unsupported clinical or microbiome relationships may be omitted, not the affirmed symptom itself'
            ))
    if check_workup_coverage:
        covered = {code for row in result['clusters'] for code in row['priorityItemCodes']}
        for item in items:
            if item['code'] not in covered:
                section_issues.append(('clusters',
                    f'clusters: priority item {item["code"]} ({item.get("title")}) '
                    'has not been assigned a P0 workup theme'
                ))
    cluster_codes = {row['code'] for row in result['clusters']}
    for row in result['lifestyleFactors']:
        codes = row.get('clusterCodes', [])
        row['clusterCodes'] = [code for code in codes if code in cluster_codes] if isinstance(codes, list) else []
    return result, row_issues, section_issues


def _decode(plan, facts, handles, scopes, problem_codes=(), *, symptom_slots=(), check_workup_coverage=True,
            priority_items=(), routine_problem_codes=()):
    """Strict decode: any row or section issue rejects the plan (review feedback)."""
    result, row_issues, section_issues = _decode_report(
        plan, facts, handles, scopes, problem_codes, symptom_slots=symptom_slots,
        check_workup_coverage=check_workup_coverage, priority_items=priority_items,
        routine_problem_codes=routine_problem_codes,
    )
    messages = [message for issues in row_issues.values() for message in issues]
    messages.extend(message for _, message in section_issues)
    if messages:
        raise ValueError('; '.join(messages))
    return result


def _related_scopes(plan, cells):
    """Project reviewed source relationships without generating another plan.

    A cell keeps its literal owner records. Only directly connected, reviewed
    themes supply context; sharing a patient alone does not create an edge.
    """
    def admitted_ids(row, fields):
        pending = set(row.get('sourceOnlyPendingMicrobiomeFactIds', ()))
        return list(dict.fromkeys(fid for field in fields for fid in row.get(field, ()) if fid not in pending))

    relationships = []
    for section in ('clusters', 'symptoms', 'recognitions'):
        for row in plan[section]:
            ids = admitted_ids(row, _ID_FIELDS)
            relationships.append((set(ids), ids, row['purpose']))
    for row in plan['lifestyleFactors']:
        ids = list(dict.fromkeys([*row['factIds'], *row.get('contextFactIds', ())]))
        relationships.append((set(row['factIds']), ids, row['purpose']))
    result = []
    stable_prefixes = ('body.', 'clinical.', 'clinicalRow.', 'structure.', 'organisms.', 'function.', 'system.')
    stable_summaries = {
        'bodySummary',
        'clinicalLead',
        'specialtySummary',
        'trendSummary',
        'microbiomeLead',
        'sampleSummary',
    }
    for cell in cells:
        if not cell['code'].startswith(stable_prefixes) and cell['code'] not in stable_summaries:
            continue
        primary = list(cell.get('factIds', ()))
        if not primary:
            continue
        primary_set = set(primary)
        matching = (
            []
            if cell['code'] in {'sampleSummary', 'lifestyleSummary'}
            else [(ids, purpose) for anchors, ids, purpose in relationships if primary_set.intersection(anchors)]
        )
        context = list(dict.fromkeys(fid for ids, _ in matching for fid in ids if fid not in primary_set))
        result.append(
            {
                'code': cell['code'],
                'factIds': primary,
                'contextFactIds': context,
                'purpose': cell.get('question') or '解释本格真实观测的主要格局及其个人管理意义。',
            }
        )

    def bind(code, primary, context, purpose):
        primary = list(dict.fromkeys(primary))
        result.append(
            {
                'code': code,
                'factIds': primary,
                'contextFactIds': list(dict.fromkeys(fid for fid in context if fid not in primary)),
                'purpose': purpose,
            }
        )

    for row in plan['clusters']:
        ids = admitted_ids(row, _ID_FIELDS)
        for prefix in ('synthesis.', 'synthesisAction.'):
            bind(prefix + row['code'], ids, [], row['purpose'])
    for row in plan['symptoms']:
        context = [
            fid for field in ('clinicalFactIds', 'microbiomeFactIds', 'lifestyleFactIds', 'contextFactIds')
            for fid in row.get(field, ())
        ]
        bind('symptom.' + row['code'], row['factIds'], context, row['purpose'])
        for kind in ('clinical', 'microbiome'):
            if row.get(kind + 'FactIds'):
                bind(
                    'symptomEvidence.' + row['code'] + '.' + kind, row['factIds'], row[kind + 'FactIds'], row['purpose']
                )
    for row in plan['lifestyleFactors']:
        # A link to the same management theme is navigation. Only the
        # reviewed habit-to-observation relation licenses a clinical claim.
        bind('lifestyle.' + row['code'], row['factIds'], row.get('contextFactIds', ()), row['purpose'])
    for row in plan['recognitions']:
        bind('recognition.' + row['code'], row['factIds'], [], row['purpose'])
    return result


def _connect_lifestyle_to_clusters(lifestyle, clusters):
    """Join reviewed behavior and clinical anchors after independent planning."""
    anchors = [
        (cluster['code'], set(cluster.get('factIds', ())) | set(cluster.get('lifestyleFactIds', ())),
         set(cluster.get('clinicalFactIds', ())))
        for cluster in clusters if cluster.get('priority') != 'P0'
    ]
    return [
        {**row, 'clusterCodes': [code for code, primary, clinical in anchors
          if primary.intersection(row.get('factIds', ())) or clinical.intersection(row.get('contextFactIds', ()))]}
        for row in lifestyle
    ]


def _cluster_coverage(packet, draft):
    """Show the reviewer the source groups that the proposed themes actually cover."""
    clusters = draft.get('clusters') if isinstance(draft, dict) else None
    rows = [row for row in clusters if isinstance(row, dict)] if isinstance(clusters, list) else []
    assigned = {fid for row in rows for field in _ID_FIELDS if isinstance(row.get(field), list)
                for fid in row[field] if isinstance(fid, str)}
    assigned_problems = {code for row in rows if isinstance(row.get('problemCodes'), list)
                         for code in row['problemCodes'] if isinstance(code, str)}
    followups = [row['name'] for row in packet['clinicalProblems'] if row['code'] not in assigned_problems
                 and (row.get('originalAdvice') or row.get('priorityWorkup'))]
    panels = [{
        'name': cell['name'],
        'sourceRecords': len(cell['factIds']),
        'recordsInThemes': len(assigned.intersection(cell['factIds'])),
    } for cell in packet['cells'] if cell['code'].startswith(('function.', 'organisms.')) and cell.get('factIds')]
    uncovered = [item['title'] for item in packet.get('priorityItems', ())
                 if not assigned.intersection(item.get('recordIds', ()))
                 and not assigned_problems.intersection(item.get('problemCodes', ()))]
    return {'clinicalFollowupsOutsideThemes': followups, 'microbiomePanels': panels,
            'priorityItemsOutsideThemes': uncovered}


def _review_failure(reviewed, sections):
    if not isinstance(reviewed, dict) or any(
        (reviewed.get('checks') or {}).get(key) is not True for key in _CHECKS
    ):
        return 'review_checks_incomplete'
    subset = reviewed.get('plan')
    if not isinstance(subset, dict) or any(not isinstance(subset.get(key), list) for key in sections):
        return 'sections_missing'
    return None


def _section_status(sections):
    return {key: {'state': 'failed', 'reasons': [], 'dropped': [], 'repaired': [], 'rows': 0} for key in sections}


def _row_code(row, index):
    code = row.get('code') if isinstance(row, dict) else None
    return code if isinstance(code, str) and code.strip() else f'#{index}'


async def _repair_rows(client, patient, person, sections, subset, row_issues, section_issues, schema, scope_tasks,
                       diagnostic):
    """One directed repair: only the failing rows, each with its own errors."""
    from .evaluation_report import _write_private_diagnostic

    failed = [
        {'section': key, 'row': subset[key][index], 'errors': messages}
        for (key, index), messages in sorted(row_issues.items(), key=lambda item: (item[0][0], item[0][1]))
        if key in sections
    ]
    response = await client.generate(
        {
            'task': 'repair_source_bound_assessment_content_items',
            'section': '+'.join(sections),
            'subject': person,
            'patient': patient,
            'acceptedRows': {
                key: [row for index, row in enumerate(subset[key]) if (key, index) not in row_issues]
                for key in sections
            },
            'failedRows': failed,
            'sectionIssues': [message for _, message in section_issues],
            'instructions': [
                *_PLAN_RULES,
                *scope_tasks,
                '只修正failedRows中的条目，并补足sectionIssues指出的缺项。按每条errors逐项修正来源编号、'
                '来源性质和原文选择；无法用真实来源修正的条目直接省去，不要改成其他主题。',
                'acceptedRows已通过核对，只作上下文，不要重复返回。checks只评价本次返回的条目。',
            ],
            'checksRequired': list(_CHECKS),
            'outputSchema': {'checks': {key: True for key in _CHECKS}, 'plan': schema},
        }
    )
    _write_private_diagnostic(f'content-plan-{diagnostic}-repair', response)
    if not isinstance(response, dict) or any(
        (response.get('checks') or {}).get(key) is not True for key in _CHECKS
    ) or not isinstance(response.get('plan'), dict):
        return None
    plan = response['plan']
    repaired = {key: [row for row in plan.get(key, ()) if isinstance(row, dict)]
                if isinstance(plan.get(key), list) else [] for key in sections}
    if 'lifestyleFactors' in repaired:
        repaired['lifestyleFactors'] = [{**row, 'clusterCodes': []} for row in repaired['lifestyleFactors']]
    return repaired


async def _plan_section(client, packet, person, sections, accepted, facts, handles, journal, *, label=None,
                        rules=None):
    """Plan, review and validate one duty. Never raises for content problems.

    Returns ``(rows_by_section, status_by_section)``. A row that fails
    validation gets one directed repair; if it still fails it is dropped and
    the reason is recorded. Only a failed model call or a review that stays
    structurally incomplete empties the section (cells fall back to their
    source-bound baseline instead).
    """
    from .evaluation_report import _write_private_diagnostic

    started = monotonic()
    section = '+'.join(sections)
    diagnostic = label or section
    cell_stage = sections == ('cellScopes',)
    patient = {key: value for key, value in packet.items() if cell_stage or key != 'cells'}
    scopes = {row['code']: row for row in packet['cells']}
    schema = _contract(sections)
    rules = rules or {}
    status = _section_status(sections)
    scope_tasks = []
    if 'clusters' in sections:
        scope_tasks.append(
            '逐项辨别真正不同的管理目的：临床查因、核心代谢管理、已有症状的观察、肠道支持、长期随访或营养维护。'
            '用对应事实确定哪些成立，按会改变的行动或复评目标合并。'
            '长期关注若有独立目标和不同来源支撑，应单列清楚；只有孤立发现时归临床随访层。'
            'P1需要实际核心管理理由，P2需要协同或长期观察理由，在purpose中指出优先级依据。'
            '成组偏移可有共同观察目标；不同器官发现与营养预测之间即使共存也不能据此认定同一病因。'
        )
    if 'recognitions' in sections:
        scope_tasks.append(
            '分别盘点所有明确的本人目标和可核对的来源冲突。不同目标对应不同复评指标时分别说明，'
            '给出两端的记录；临床原文相互冲突时说明待核对哪两处，不替受检者假设错误观点。'
        )

    def decode(subset):
        combined = {key: deepcopy(accepted.get(key, [])) for key in _SECTIONS}
        combined.update({key: subset[key] for key in sections})
        return _decode_report(
            combined,
            facts,
            handles,
            scopes,
            _problem_metadata(packet),
            symptom_slots=packet.get('symptomSlots', ()) if 'symptoms' in sections else (),
            check_workup_coverage=False,
            priority_items=rules.get('priorityItems', ()),
            routine_problem_codes=rules.get('routineProblemCodes', ()),
        )

    baseline = []
    unrequested_cell_codes = []
    try:
        if cell_stage:
            # Existing table ownership is authoritative. The review call can add
            # relevant context directly to this source-backed baseline.
            baseline = [
                {'code': row['code'], 'factIds': row['factIds'], 'purpose': row['question'],
                 'contextPurpose': row['question'], 'contextFactIds': [],
                 'contextStatus': 'no_additional_context', 'relations': []}
                for row in scopes.values()
            ]
            draft = {'cellScopes': deepcopy(baseline)}
            _write_private_diagnostic('content-plan-' + diagnostic + '-source-baseline', draft)
        else:
            response = await client.generate(
                {
                    'task': 'plan_source_bound_assessment_content',
                    'section': section,
                    'subject': person,
                    'patient': patient,
                    'acceptedPlan': accepted,
                    'instructions': [
                        *_PLAN_RULES,
                        *scope_tasks,
                        '本次只返回outputSchema指定数组。每个主题选足以支持判断的代表性锚点，'
                        '不要枚举同一主题下所有指标或所有菌种。purpose用一句话明确本主题职责。',
                    ],
                    'outputSchema': schema,
                }
            )
            _write_private_diagnostic('content-plan-' + diagnostic + '-author', response)
            draft = response.get('plan', response) if isinstance(response, dict) else response
        feedback = []
        subset = None
        failure = None
        for attempt in range(2):
            reviewed = await client.generate(
                {
                    'task': 'review_source_bound_assessment_content',
                    'section': section,
                    'subject': person,
                    'patient': patient,
                    'acceptedPlan': accepted,
                    'draftPlan': draft,
                    **({'themeCoverage': _cluster_coverage(packet, draft)} if 'clusters' in sections else {}),
                    **({'requestedCellCodes': list(scopes)} if cell_stage else {}),
                    'structuralFeedback': feedback,
                    'instructions': [
                        *_PLAN_RULES,
                        *scope_tasks,
                        *(['核对themeCoverage所示未覆盖项：是否有会改变下一步安排的独立管理目标。'
                           '仅在有来源、有实际不同任务时补入；不将未分配检查强行凑成一组。']
                          if 'clusters' in sections else []),
                        *(['逐格审查已固定的主记录，主动选择对本格有直接帮助的相关背景及理由。'] if cell_stage else []),
                        '先修正草案的无源事实、无关关系、编号和结构，再独立检查修正后的plan。'
                        'checks只评价返回的修订plan。只返回checks和本次指定数组组成的plan，'
                        '无来源的可选条目省去，无法修正的检查返回false。',
                    ],
                    'checksRequired': list(_CHECKS),
                    'outputSchema': {'checks': {key: True for key in _CHECKS}, 'plan': schema},
                }
            )
            _write_private_diagnostic(f'content-plan-{diagnostic}-review-{attempt + 1}', reviewed)
            failure = _review_failure(reviewed, sections)
            candidate = reviewed.get('plan') if isinstance(reviewed, dict) else None
            if failure is None:
                subset = {key: list(candidate[key]) for key in sections}
                break
            feedback = [failure]
            draft = candidate if isinstance(candidate, dict) else draft
            if isinstance(candidate, dict) and all(isinstance(candidate.get(key), list) for key in sections):
                # Tell the second review exactly which rows also fail validation.
                _, issues, gaps = decode({key: list(candidate[key]) for key in sections})
                feedback.extend(message for messages in issues.values() for message in messages)
                feedback.extend(message for _, message in gaps)
        if subset is None:
            for key in sections:
                status[key]['reasons'].append(failure or 'review_missing')
            if cell_stage:
                status['cellScopes'].update(state='fallback', rows=len(baseline))
                status['cellScopes']['reasons'].append('source_baseline_used')
                journal[diagnostic] = {'status': 'fallback', 'reason': failure,
                                       'elapsedMs': round((monotonic() - started) * 1000, 3)}
                return {'cellScopes': deepcopy(baseline)}, status
            journal[diagnostic] = {'status': 'failed', 'reason': failure,
                                   'elapsedMs': round((monotonic() - started) * 1000, 3)}
            return {key: [] for key in sections}, status
        if 'lifestyleFactors' in sections:
            subset['lifestyleFactors'] = [
                {**row, 'clusterCodes': []} if isinstance(row, dict) else row for row in subset['lifestyleFactors']
            ]
        if cell_stage:
            # Each batch owns only its requested cells. A model may echo
            # another cell from the shared source catalogue; quarantine that
            # output. A missing, duplicated or incomplete cell keeps its
            # source-bound baseline instead of invalidating the batch.
            returned = {code: [] for code in scopes}
            for row in subset['cellScopes']:
                code = row.get('code') if isinstance(row, dict) else None
                if not isinstance(code, str) or code not in returned:
                    unexpected = code if isinstance(code, str) else '<invalid-cell-code>'
                    if unexpected not in unrequested_cell_codes:
                        unrequested_cell_codes.append(unexpected)
                    continue
                returned[code].append(row)
            by_code = {row['code']: row for row in baseline}
            cells = []
            for code, rows in returned.items():
                row = rows[0] if rows else None
                if row is None or any(field not in row for field in ('contextPurpose', 'contextStatus', 'relations')):
                    status['cellScopes']['dropped'].append({'code': code, 'errors': ['cell_context_missing']})
                    cells.append(deepcopy(by_code[code]))
                    continue
                # The planner owns relationships, not the identity or purpose
                # of a source-bound table cell.
                cells.append({**row, 'factIds': list(scopes[code]['factIds']), 'purpose': scopes[code]['question']})
            subset = {'cellScopes': cells}
        decoded, row_issues, section_issues = decode(subset)
        original_failures = {(key, _row_code(subset[key][index], index)): messages
                             for (key, index), messages in row_issues.items()}
        if (row_issues or section_issues) and not cell_stage:
            repaired = await _repair_rows(client, patient, person, sections, subset, row_issues, section_issues,
                                          schema, scope_tasks, diagnostic)
            if repaired is None:
                for key in sections:
                    status[key]['reasons'].append('repair_rejected')
            else:
                subset = {
                    key: [*(row for index, row in enumerate(subset[key]) if (key, index) not in row_issues),
                          *repaired[key]]
                    for key in sections
                }
                repaired_codes = {(key, _row_code(row, index)) for key in sections
                                  for index, row in enumerate(repaired[key])}
                decoded, row_issues, section_issues = decode(subset)
                failing = {(key, _row_code(subset[key][index], index)) for key, index in row_issues}
                for key, code in sorted(repaired_codes - failing):
                    status[key]['repaired'].append(code)
        if cell_stage and row_issues:
            by_code = {row['code']: row for row in baseline}
            for (key, index), messages in row_issues.items():
                code = _row_code(subset[key][index], index)
                status['cellScopes']['dropped'].append({'code': code, 'errors': [m[:240] for m in messages[:3]]})
                if code in by_code:
                    subset[key][index] = deepcopy(by_code[code])
            decoded, row_issues, section_issues = decode(subset)
        kept = {key: [row for index, row in enumerate(subset[key]) if (key, index) not in row_issues]
                for key in sections}
        dropped = {(key, _row_code(subset[key][index], index)): messages
                   for (key, index), messages in row_issues.items()}
        survivors = {(key, _row_code(row, index)) for key in sections for index, row in enumerate(kept[key])}
        for (key, code), messages in original_failures.items():
            if (key, code) not in survivors and (key, code) not in dropped:
                dropped[(key, code)] = messages
        for (key, code), messages in dropped.items():
            status[key]['dropped'].append({'code': code, 'errors': [message[:240] for message in messages[:3]]})
        if 'symptoms' in sections:
            for original, resolved in zip(kept['symptoms'], decoded['symptoms'], strict=True):
                original.update(name=resolved['name'], purpose=resolved['purpose'],
                    factIds=[handles[fid] for fid in resolved['factIds']],
                    contextFactIds=[handles[fid] for fid in resolved['contextFactIds']])
        if 'clusters' in sections:
            for original, resolved in zip(kept['clusters'], decoded['clusters'], strict=True):
                original.update(
                    priority=resolved['priority'],
                    purpose=resolved['purpose'],
                    sourceOnlyPendingMicrobiomeFactIds=[
                        handles[fid] for fid in resolved.get('sourceOnlyPendingMicrobiomeFactIds', ())
                    ],
                )
                if 'sourcePriority' in resolved:
                    original['sourcePriority'] = resolved['sourcePriority']
        for key in sections:
            entry = status[key]
            entry['rows'] = len(kept[key])
            entry['state'] = 'partial' if entry['dropped'] else 'reviewed'
            if entry['dropped']:
                entry['reasons'].append('items_dropped')
            if entry['repaired']:
                entry['reasons'].append('items_repaired')
        for key, message in section_issues:
            if key in status and 'section_issue_pending' not in status[key]['reasons']:
                status[key]['reasons'].append('section_issue_pending')
        journal[diagnostic] = {
            'status': 'reviewed' if all(status[key]['state'] == 'reviewed' for key in sections) else 'partial',
            'rows': {key: len(kept[key]) for key in sections},
            'elapsedMs': round((monotonic() - started) * 1000, 3),
            **({'unrequestedCellCodesIgnored': unrequested_cell_codes} if unrequested_cell_codes else {}),
            **({'dropped': {key: status[key]['dropped'] for key in sections if status[key]['dropped']}}
               if any(status[key]['dropped'] for key in sections) else {}),
        }
        return {key: kept[key] for key in sections}, status
    except asyncio.CancelledError:
        raise
    except Exception as error:
        reason = 'model_call_failed:' + type(error).__name__
        for key in sections:
            status[key]['reasons'].append(reason)
        journal[diagnostic] = {'status': 'failed', 'reason': reason + ':' + str(error)[:160],
                               'elapsedMs': round((monotonic() - started) * 1000, 3)}
        if cell_stage and baseline:
            status['cellScopes'].update(state='fallback', rows=len(baseline))
            status['cellScopes']['reasons'].append('source_baseline_used')
            return {'cellScopes': deepcopy(baseline)}, status
        return {key: [] for key in sections}, status


def _unique_code(base, used):
    code = re.sub(r'[^0-9A-Za-z_.:-]+', '_', base).strip('_') or 'item'
    candidate, index = code, 2
    while candidate in used:
        candidate, index = f'{code}_{index}', index + 1
    used.add(candidate)
    return candidate


def _auto_selections(fids, facts, handles, hints=()):
    """Choose admitted observation quotes for program-built theme rows."""
    hint_text = _clinical_literal(' '.join(str(hint) for hint in hints))
    selections = []
    for fid in fids:
        quotes = list(dict.fromkeys(_observation_quotes(facts[fid])))
        observations = [item for item in facts[fid].get('_clinicalPatientRows', ())
                        if item.get('role') in _OBSERVATION_ROLES]
        if len(observations) <= 1 or not quotes:
            continue
        chosen = [quote for quote in quotes if _clinical_literal(quote) and (
            _clinical_literal(quote) in hint_text
            or any(part and part in _clinical_literal(quote) for part in re.split(r'[：:，,。；;]', hint_text)
                   if len(part) >= 4)
        )] or quotes[:1]
        selections.extend({'recordId': handles[fid], 'quote': quote} for quote in chosen)
    return selections


def _anchor_ids(fids, facts):
    return [fid for fid in fids if fid in facts and facts[fid].get('kind') == 'clinical'
            and facts[fid].get('_clinicalPatientObservation') is not False]


def _rule_cluster(item, facts, handles, problem_codes, used):
    clinical = _anchor_ids(item.get('factIds', ()), facts)
    if not clinical:
        return None
    checks = '、'.join(item.get('checks', ()))
    return {
        'code': _unique_code('p0_' + item['code'].lower(), used),
        'name': item['title'],
        'priority': 'P0',
        'factIds': [],
        'clinicalFactIds': [handles[fid] for fid in clinical],
        'microbiomeFactIds': [],
        'problemCodes': [code for code in item.get('problemCodes', ()) if code in problem_codes],
        'clinicalSelections': _auto_selections(clinical, facts, handles, item.get('evidenceText', ())),
        'purpose': f'按危险信号规则优先完成{item.get("department") or "相关专科"}评估'
                   f'（{item["timeframe"]["text"]}）：{checks}。',
        'origin': 'red_flag_rule',
    }


def _routine_problem(problem, routine_codes):
    return problem.get('code') in routine_codes or problem.get('role') == 'secondary_finding'


def _fallback_clusters(packet, facts, handles, rules, used):
    """Program themes when abnormal findings exist but no reviewed theme survived."""
    reverse = {value: key for key, value in handles.items()}
    covered = {code for item in rules.get('priorityItems', ()) for code in item.get('problemCodes', ())}
    groups = {}
    for problem in packet.get('clinicalProblems', ()):
        if problem.get('code') in covered or _routine_problem(problem, rules.get('routineProblemCodes', ())):
            continue
        if problem.get('role') not in {None, 'health_problem'}:
            continue
        groups.setdefault(problem.get('category') or problem.get('code'), []).append(problem)
    clusters = []
    for problems in groups.values():
        fids = [reverse[handle] for problem in problems for handle in problem.get('factIds', ()) if handle in reverse]
        clinical = _anchor_ids(list(dict.fromkeys(fids)), facts)
        if not clinical:
            continue
        names = [str(problem.get('name')) for problem in problems if problem.get('name')]
        title = '、'.join(names[:3]) + ('等' if len(names) > 3 else '')
        priority = 'P1' if any(problem.get('focusLabel') in {'重点管理', '近期复评', '优先查因'}
                               for problem in problems) else 'P2'
        clusters.append({
            'code': _unique_code('rule_' + str(problems[0].get('category') or 'problem'), used),
            'name': title + '的管理与复评',
            'priority': priority,
            'factIds': [],
            'clinicalFactIds': [handles[fid] for fid in clinical],
            'microbiomeFactIds': [],
            'problemCodes': [problem['code'] for problem in problems if problem.get('code')],
            'clinicalSelections': _auto_selections(clinical, facts, handles, names),
            'purpose': f'围绕{title}说明实际检查结果、原检查建议及需要复评的指标。',
            'origin': 'rule_fallback',
        })
    if clusters:
        return clusters
    microbiome = [fid for fid, fact in facts.items() if fact.get('kind') in {'microbe', 'function'}
                  and fact.get('abnormal') is True and _specific_microbiome_fact(fact)][:8]
    if microbiome:
        return [{
            'code': _unique_code('rule_microbiome', used),
            'name': '肠道菌群分项偏移的观察与支持',
            'priority': 'P2',
            'factIds': [],
            'clinicalFactIds': [],
            'microbiomeFactIds': [handles[fid] for fid in microbiome],
            'problemCodes': [],
            'clinicalSelections': [],
            'purpose': '说明本次粪便样本中偏离参考范围的菌群与功能分项及共同观察目标；'
                       '不把相对丰度或功能评分改写成人体缺乏、感染或疾病。',
            'origin': 'rule_fallback',
        }]
    abnormal = [fid for fid in _anchor_ids(list(facts), facts)
                if facts[fid].get('abnormal') is True and not _SUMMARY_RECORD.search(str(facts[fid].get('name')))][:8]
    if abnormal:
        return [{
            'code': _unique_code('rule_abnormal_results', used),
            'name': '需复评的异常检查结果',
            'priority': 'P2',
            'factIds': [],
            'clinicalFactIds': [handles[fid] for fid in abnormal],
            'microbiomeFactIds': [],
            'problemCodes': [],
            'clinicalSelections': _auto_selections(abnormal, facts, handles),
            'purpose': '按原参考范围说明各项异常结果，并列出需要复查的指标。',
            'origin': 'rule_fallback',
        }]
    return []


def _imbalance_present(packet, facts, rules):
    """Abnormal findings that warrant at least one theme (routine-only findings do not)."""
    if rules.get('priorityItems'):
        return True
    routine = rules.get('routineProblemCodes', ())
    problems = packet.get('clinicalProblems', ())
    if any(problem.get('role') in {None, 'health_problem'} and not _routine_problem(problem, routine)
           for problem in problems):
        return True
    # The clinical organiser's problem list decides which abnormal results are
    # health problems. Only without any such list do raw flags stand in for it.
    routine_ids = {fid for item in rules.get('routineFollowups', ()) for fid in item.get('factIds', ())}
    if not problems and any(fact.get('kind') == 'clinical' and fact.get('abnormal') is True
                            and fid not in routine_ids and not _SUMMARY_RECORD.search(str(fact.get('name')))
                            for fid, fact in facts.items()):
        return True
    return any(fact.get('kind') in {'microbe', 'function'} and fact.get('abnormal') is True
               and _specific_microbiome_fact(fact) for fact in facts.values())


def _fallback_symptoms(packet, facts, handles, rows):
    reverse = {value: key for key, value in handles.items()}
    present = {reverse[handle] for row in rows for handle in row.get('factIds', ()) if handle in reverse}
    used = {row.get('code') for row in rows}
    result = []
    for slot in packet.get('symptomSlots', ()):
        affirmed = [reverse[handle] for handle in slot.get('factIds', ()) if handle in reverse
                    and (facts[reverse[handle]].get('_reportedSourceRole') or {}).get('role') == 'reported_symptom']
        if not affirmed or present.intersection(affirmed):
            continue
        result.append({
            'code': _unique_code(str(slot.get('code') or 'symptom'), used),
            'name': str(slot.get('name') or facts[affirmed[0]].get('name')),
            'factIds': [handles[fid] for fid in affirmed],
            'clinicalFactIds': [],
            'microbiomeFactIds': [],
            'lifestyleFactIds': [],
            'contextFactIds': [],
            'clinicalSelections': [],
            'purpose': '说明本人报告的这项不适及其原始时间和条件；相关检查只在有来源时说明，不推断病因。',
            'origin': 'rule_fallback',
        })
    return result


def _fallback_lifestyle(facts, handles):
    buckets = {}
    for fid, fact in facts.items():
        if fact.get('kind') != 'self_reported' or not _behavior_anchor(fact):
            continue
        question, answer = str(fact.get('name') or ''), str(fact.get('value') or '').strip()
        if not answer or _LIFESTYLE_EXCLUDED_QUESTIONS.search(question):
            continue
        for code, _, pattern in _LIFESTYLE_DIMENSIONS:
            if re.search(pattern, question):
                buckets.setdefault(code, []).append(fid)
                break
    return [
        {
            'code': code,
            'name': name,
            'factIds': [handles[fid] for fid in buckets[code]],
            'contextFactIds': [],
            'clusterCodes': [],
            'sourceSelections': [],
            'purpose': f'按原题条件说明{name}方面的实际回答，以及与本次检查结果有来源支持的关系。',
            'origin': 'rule_fallback',
        }
        for code, name, _ in _LIFESTYLE_DIMENSIONS
        if code in buckets
    ]


def _ensure_priority_coverage(clusters, rules, facts, handles, problem_codes):
    """Every P0 rule item is owned by a theme; add a rule theme for the rest."""
    reverse = {value: key for key, value in handles.items()}
    used = {row.get('code') for row in clusters}
    added = []
    for item in rules.get('priorityItems', ()):
        owners = [
            row for row in clusters
            if {reverse.get(handle) for handle in (
                row.get('clinicalFactIds') or [h for field in _ID_FIELDS for h in row.get(field, ())]
            )}.intersection(item.get('factIds', ()))
            or set(row.get('problemCodes', ())).intersection(item.get('problemCodes', ()))
        ]
        if owners:
            continue
        row = _rule_cluster(item, facts, handles, problem_codes, used)
        if row is not None:
            added.append(row)
    return [*added, *clusters], added


def _finish_status(status, key, rows, *, fallback=(), empty_reason=None):
    """Final state of a planned section after program fallbacks."""
    entry = status.setdefault(key, _section_status((key,))[key])
    if fallback:
        entry['reasons'].append('rule_fallback')
        entry['state'] = 'partial' if len(rows) > len(fallback) else 'fallback'
    elif not rows and empty_reason:
        entry['reasons'].append(empty_reason)
        entry['state'] = 'empty'
    elif not rows and entry['state'] == 'reviewed':
        entry['state'] = 'empty'
    entry['rows'] = len(rows)
    entry['reasons'] = list(dict.fromkeys(entry['reasons']))
    return entry


async def plan_assessment_content(client, blueprint, subject, *, deadline=None, audit=None):
    """Return a reviewed source plan whose sections fail independently.

    A failing row is repaired once and otherwise dropped; a failing section is
    emptied or filled by program rules. ``status`` stays ``reviewed`` whenever
    usable content exists; ``sectionStatus`` and ``completeness`` say exactly
    what was degraded and why. Only a person without source facts (or without
    any time budget) gets ``status='unavailable'``.
    """
    unavailable = {'version': PLAN_VERSION, 'status': 'unavailable', **{key: [] for key in _SECTIONS}}
    journal = audit if isinstance(audit, dict) else {}
    started = monotonic()
    person = subject.model_dump(mode='json', by_alias=True) if hasattr(subject, 'model_dump') else subject
    facts, handles, packet, rules = _snapshot(blueprint, person)
    if not facts:
        journal['contentPlan'] = {'status': 'unavailable', 'reason': 'no_source_facts'}
        return unavailable
    remaining = None if deadline is None else max(0.0, deadline - started)
    budget = min(720.0, remaining) if remaining is not None else 720.0
    if budget <= 0:
        journal['contentPlan'] = {'status': 'unavailable', 'reason': 'no_time_budget'}
        return unavailable
    from .evaluation_report import _write_private_diagnostic

    _write_private_diagnostic('content-plan-sources', packet)
    stages = {}
    empty = {key: [] for key in _SECTIONS}
    stable = [
        row
        for row in packet['cells']
        if row.get('factIds')
        and (
            row['code'].startswith(
                ('body.', 'clinical.', 'clinicalRow.', 'digestive.', 'structure.', 'organisms.', 'function.')
            )
            or row['code']
            in {
                'bodySummary',
                'clinicalLead',
                'specialtySummary',
                'trendSummary',
                'microbiomeLead',
                'structureSummary',
                'riskSummary',
                'sampleSummary',
                'fermentationSummary',
            }
        )
    ]
    limiter = asyncio.Semaphore(4)

    async def plan_cells(index, cells):
        async with limiter:
            return await _plan_section(
                client, {**packet, 'cells': cells}, person, ('cellScopes',), empty, facts, handles, stages,
                label=f'cellScopes-{index:02d}', rules=rules,
            )

    duties = {
        'clusters': ('clusters',),
        'symptoms': ('symptoms',),
        'lifestyleFactors+recognitions': ('lifestyleFactors', 'recognitions'),
    }
    tasks = {
        name: asyncio.create_task(_plan_section(
            client, packet, person, sections, empty, facts, handles, stages, rules=rules,
        ))
        for name, sections in duties.items()
    }
    batches = [stable[start: start + 24] for start in range(0, len(stable), 24)]
    for index, cells in enumerate(batches, 1):
        tasks[f'cellScopes-{index:02d}'] = asyncio.create_task(plan_cells(index, cells))
    try:
        done, pending = await asyncio.wait(tasks.values(), timeout=budget)
    finally:
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    section_status = {}
    accepted = {key: [] for key in _SECTIONS}
    cell_rows = []
    cell_states = []
    for name, task in tasks.items():
        sections = duties.get(name, ('cellScopes',))
        if task in pending or task.cancelled():
            outcome = {key: [] for key in sections}
            status = _section_status(sections)
            for key in sections:
                status[key]['reasons'].append('timeout')
            stages.setdefault(name, {'status': 'timeout'})
        elif task.exception() is not None:
            outcome = {key: [] for key in sections}
            status = _section_status(sections)
            for key in sections:
                status[key]['reasons'].append('stage_error:' + type(task.exception()).__name__)
        else:
            outcome, status = task.result()
        if sections == ('cellScopes',):
            cell_rows.extend(outcome['cellScopes'])
            cell_states.append(status['cellScopes'])
            continue
        for key in sections:
            accepted[key] = list(outcome[key])
            section_status[key] = status[key]
    try:
        problem_codes = _problem_metadata(packet)
        accepted['clusters'], rule_clusters = _ensure_priority_coverage(
            accepted['clusters'], rules, facts, handles, problem_codes,
        )
        imbalance = _imbalance_present(packet, facts, rules)
        fallback_clusters = []
        if imbalance and not accepted['clusters']:
            fallback_clusters = _fallback_clusters(packet, facts, handles, rules,
                                                   {row.get('code') for row in accepted['clusters']})
            accepted['clusters'] = fallback_clusters
        fallback_symptoms = _fallback_symptoms(packet, facts, handles, accepted['symptoms'])
        accepted['symptoms'] = [*accepted['symptoms'], *fallback_symptoms]
        fallback_lifestyle = [] if accepted['lifestyleFactors'] else _fallback_lifestyle(facts, handles)
        accepted['lifestyleFactors'] = accepted['lifestyleFactors'] or fallback_lifestyle
        accepted['lifestyleFactors'] = _connect_lifestyle_to_clusters(
            accepted['lifestyleFactors'], accepted['clusters'],
        )
        derived = {row['code']: row for row in _related_scopes(accepted, packet['cells'])}
        derived.update({row['code']: row for row in cell_rows if isinstance(row, dict) and row.get('code')})
        accepted['cellScopes'] = list(derived.values())
        result, row_issues, section_issues = _decode_report(
            accepted,
            facts,
            handles,
            {row['code']: row for row in packet['cells']},
            problem_codes,
            symptom_slots=packet.get('symptomSlots', ()),
            priority_items=rules.get('priorityItems', ()),
            routine_problem_codes=rules.get('routineProblemCodes', ()),
        )
        for (key, index), messages in row_issues.items():
            entry = section_status.setdefault(key, _section_status((key,))[key])
            entry['dropped'].append({'code': _row_code(accepted[key][index], index),
                                     'errors': [message[:240] for message in messages[:3]]})
            entry['reasons'].append('items_dropped_in_final_check')
            if entry['state'] == 'reviewed':
                entry['state'] = 'partial'
        origins = {key: {row.get('code'): row.get('origin', 'model') for row in accepted[key] if isinstance(row, dict)}
                   for key in ('clusters', 'symptoms', 'lifestyleFactors')}
        for key, mapping in origins.items():
            for row in result[key]:
                row['origin'] = mapping.get(row['code'], 'model')
        if rule_clusters:
            entry = section_status.setdefault('clusters', _section_status(('clusters',))['clusters'])
            entry['reasons'].append('priority_rule_cluster_added')
        _finish_status(section_status, 'clusters', result['clusters'], fallback=fallback_clusters,
                       empty_reason=None if imbalance else 'no_imbalance')
        if rule_clusters and section_status['clusters']['state'] == 'reviewed':
            section_status['clusters']['state'] = 'partial'
        _finish_status(section_status, 'symptoms', result['symptoms'], fallback=fallback_symptoms,
                       empty_reason=None if packet.get('symptomSlots') else 'no_reported_symptoms')
        has_answers = bool(_fallback_lifestyle(facts, handles))
        _finish_status(section_status, 'lifestyleFactors', result['lifestyleFactors'], fallback=fallback_lifestyle,
                       empty_reason=None if has_answers else 'no_lifestyle_answers')
        recognition_failed = section_status.get('recognitions', {}).get('state') == 'failed'
        _finish_status(section_status, 'recognitions', result['recognitions'],
                       empty_reason=None if recognition_failed else 'no_recognition_items')
        cell_entry = _section_status(('cellScopes',))['cellScopes']
        cell_entry['reasons'] = list(dict.fromkeys(reason for state in cell_states for reason in state['reasons']))
        cell_entry['dropped'] = [item for state in cell_states for item in state['dropped']]
        cell_entry['rows'] = len(result['cellScopes'])
        cell_entry['state'] = (
            'reviewed' if cell_states and all(state['state'] == 'reviewed' for state in cell_states)
            else 'fallback' if cell_states and all(state['state'] in {'fallback', 'failed'} for state in cell_states)
            else 'partial' if cell_states else 'empty'
        )
        section_status['cellScopes'] = cell_entry
        cluster_codes = {item: [row['code'] for row in result['clusters'] if item in row['priorityItemCodes']]
                         for item in (entry['code'] for entry in rules.get('priorityItems', ()))}
        result['priorityItems'] = [{**deepcopy(item), 'clusterCodes': cluster_codes.get(item['code'], [])}
                                   for item in rules.get('priorityItems', ())]
        excluded = {code for item in [*rules.get('priorityItems', ()), *rules.get('routineFollowups', ())]
                    for code in item.get('problemCodes', ())}
        excluded.update(code for row in result['clusters'] for code in row.get('problemCodes', ()))
        result['routineFollowups'] = [
            *deepcopy(rules.get('routineFollowups', [])),
            *advice_followups(rules.get('problems', ()), facts.values(), exclude_problem_codes=excluded),
        ]
        result['testGaps'] = deepcopy(rules.get('testGaps', []))
        result['clusterState'] = 'clusters_present' if result['clusters'] else (
            'no_imbalance' if not imbalance else 'unavailable')
        section_status['priorityItems'] = {'state': 'rule', 'reasons': [], 'rows': len(result['priorityItems'])}
        section_status['routineFollowups'] = {'state': 'rule', 'reasons': [], 'rows': len(result['routineFollowups'])}
        section_status['testGaps'] = {'state': 'rule', 'reasons': [], 'rows': len(result['testGaps'])}
        result['sectionStatus'] = section_status
        result['completeness'] = 'degraded' if any(
            entry.get('state') in {'partial', 'fallback', 'failed'} for entry in section_status.values()
        ) else 'complete'
        result['rulesVersion'] = deepcopy(rules.get('versions', {}))
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _write_private_diagnostic(
            'content-plan-failure',
            {'errorType': type(error).__name__, 'reason': str(error), 'stages': stages,
             'sectionStatus': section_status},
        )
        journal['contentPlan'] = {
            'status': 'unavailable',
            'reason': type(error).__name__ + ':' + str(error)[:160],
            'stages': stages,
            'elapsedMs': round((monotonic() - started) * 1000, 3),
        }
        return unavailable
    if result['completeness'] == 'degraded':
        _write_private_diagnostic('content-plan-degraded', {'sectionStatus': section_status, 'stages': stages})
    signature = json.dumps(records := packet['records'], ensure_ascii=False, sort_keys=True).encode()
    journal['contentPlan'] = {
        'status': 'reviewed',
        'completeness': result['completeness'],
        'sectionStatus': deepcopy(section_status),
        'sourceSha256': hashlib.sha256(signature).hexdigest(),
        'sourceCount': len(records),
        'plan': deepcopy(result),
        'stages': stages,
        'elapsedMs': round((monotonic() - started) * 1000, 3),
    }
    return result
