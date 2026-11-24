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

PLAN_VERSION = 'assessment-content-plan.v9-diagnostic-priority'
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
_PRIORITY_ADVICE = re.compile(
    r'(?:优先|尽快|即刻|立即|及时|紧急)(?:\s|前往|到|接受|安排|完成|至|进行|转诊){0,10}'
    r'(?:就诊|检查|复查|评估|排查|查因|转诊|急诊)|转(?:入)?急诊',
)
_SPECIFIC_WORKUP = re.compile(r'复查|进一步检查|明确诊断|明确病因|排查|查因|referr?al for diagnostic', re.I)
_RELATED_CLINICAL_EVALUATION = re.compile(r'就诊|专科评估|查因|诊断|consult|diagnos\w*', re.I)
_POSITIVE_RESULT = re.compile(r'(?:弱|强|可疑)?阳性(?:\s*\+{1,3})?|positive|positive \(weak\)|reactive', re.I)

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
    'P0必须由真实临床查因需要、警示表现或原医嘱支持；菌群分档不能独自触发P0。'
    '一般微小异常不能仅因有数值偏离获得比重要代谢/器官问题更高的阅读优先级。',
    'clinicalProblems中的优先查因职责必须保留；P0表示优先完成有依据的临床查因，不等于急诊。'
    '不能把已有明确查因安排改成“先判断是否需要查因”。常规影像随访留在临床表及随访层。',
    '只有clinicalProblems中通过priorityWorkup核验的事项可升为P0；原建议仅结合临床就诊、'
    '一般复核或报告表述冲突，不自动获得与明确优先查因相同的优先级。',
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


def _fact_id(row):
    return row.get('factId', row.get('fact_id'))


def _source_workup_priority(problem, records):
    if problem.get('focusLabel') != '优先查因':
        return None
    advice = [line for line in problem.get('originalAdvice', ()) if isinstance(line, str)]
    if any(_PRIORITY_ADVICE.search(line) and not re.search(r'无需|无须|不必|不需要|不作为', line) for line in advice):
        return 'explicit_priority_in_original_advice'
    positive = any(
        records[fid].get('kind') == 'clinical'
        and (records[fid].get('sourceObservation') or {}).get('role') in {'direct_measurement', 'exam_finding'}
        and any(_POSITIVE_RESULT.fullmatch(str(records[fid].get(field) or '').strip())
            for field in ('value', 'status'))
        for fid in problem.get('factIds', ()) if fid in records
    )
    if positive and any(_SPECIFIC_WORKUP.search(line) for line in advice) and any(
        _RELATED_CLINICAL_EVALUATION.search(line) for line in advice
    ):
        return 'positive_observation_with_original_diagnostic_evaluation'
    return None


def _snapshot(blueprint):
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
    records_by_handle = {record['id']: record for record in records}
    for problem in problems:
        if action := actions_by_code.get(problem.get('code')):
            problem['originalAdvice'] = deepcopy(action.get('originalAdvice') or [])
        if problem.get('focusLabel') == '优先查因':
            problem['priorityWorkup'] = _source_workup_priority(problem, records_by_handle)
            if problem['priorityWorkup'] is None:
                problem['sourceFocusLabel'] = problem['focusLabel']
                problem['focusLabel'] = '重点管理'
    return (
        facts,
        handles,
        {
            'assessmentDate': assessment_reference_date(),
            'records': records,
            'cells': scopes,
            'clinicalProblems': problems,
            'clinicalTrends': deepcopy(getattr(blueprint, 'clinical_trends', [])),
            **source_context,
        },
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


def _decode(plan, facts, handles, scopes, problem_codes=(), *, symptom_slots=(), check_workup_coverage=True):
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
                list(dict.fromkeys(fid for field in _ID_FIELDS for fid in row.get(field, ())))
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
    result = {'version': PLAN_VERSION, 'status': 'reviewed'}
    content_issues = []
    confirmed_workups = {
        code: {reverse[fid] for fid in problem.get('factIds', ())
            if fid in reverse and facts[reverse[fid]]['kind'] == 'clinical'}
        for code, problem in (problem_codes.items() if isinstance(problem_codes, dict) else ())
        if problem.get('focusLabel') == '优先查因' and problem.get('priorityWorkup')
    }
    for section in _SECTIONS:
        rows, used = [], set()
        for source in plan[section]:
            if not isinstance(source, dict):
                raise ValueError('content_plan_invalid_row')
            row = deepcopy(source)
            code = row.get('code')
            if not isinstance(code, str) or not code.strip() or code in used:
                raise ValueError('content_plan_invalid_code')
            if section == 'cellScopes' and code not in scopes:
                raise ValueError('content_plan_unknown_cell')
            used.add(code)
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
                raise ValueError('content_plan_unbound_row')
            if section != 'cellScopes' and not isinstance(row.get('name'), str):
                raise ValueError('content_plan_name_missing')
            if not isinstance(row.get('purpose'), str) or not row['purpose'].strip():
                raise ValueError('content_plan_purpose_missing')
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
                    admitted = [
                        span['quote']
                        for item in facts[fid].get('_clinicalPatientRows', ())
                        if item.get('role')
                        in {'direct_measurement', 'derived_measurement', 'exam_finding', 'physical_finding'}
                        for span in item.get('sourceSpans', ())
                    ]

                    def clinical_literal(text):
                        return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).rstrip('。；;，,')

                    matches = [
                        original
                        for original in admitted
                        if isinstance(quote, str) and clinical_literal(original) == clinical_literal(quote)
                    ]
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
                raise ValueError('content_plan_priority_missing')
            if section == 'clusters':
                codes = row.get('problemCodes', [])
                if not isinstance(codes, list) or any(
                    not isinstance(code, str) or code not in problem_codes for code in codes
                ):
                    raise ValueError('content_plan_problem_codes_invalid')
                actual = set(row['clinicalFactIds'] or row['factIds'])
                owned_workups = [code for code in codes if actual.intersection(confirmed_workups.get(code, ()))]
                if row['priority'] == 'P0' and not owned_workups:
                    row['priority'] = 'P1'
                    row['purpose'] = '按各自原始结果、资料差异及医生的具体建议说明需复核或管理的事项。'
                row['priorityWorkupCodes'] = owned_workups
                row['microbiomeRelationStatus'] = (
                    'source_only_pending_interpretation' if row['microbiomeFactIds'] else 'none'
                )
                if owned_workups:
                    pending = [fid for fid in row['factIds'] if facts[fid]['kind'] in {'microbe', 'function'}
                        or fid in row['microbiomeFactIds']]
                    row['sourceOnlyPendingMicrobiomeFactIds'] = pending
                    for field in _ID_FIELDS:
                        row[field] = [fid for fid in row[field] if fid not in pending]
                    if pending:
                        row['microbiomeRelationStatus'] = 'source_only_pending_interpretation'
                kinds = {facts[fid]['kind'] for fid in row['factIds']}
                if kinds == {'clinical'} and row['priority'] != 'P0' and len(codes) <= 1:
                    content_issues.append(
                        f'clusters.{code}: one clinical follow-up is not an integrated '
                        'management theme; retain it in the clinical table and surveillance layer'
                    )
                if owned_workups and row['priority'] != 'P0':
                    content_issues.append(
                        f'clusters.{code}: retain priority P0 for the verified diagnostic evaluation '
                        f'{owned_workups}; it does not imply emergency care'
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
                    nature = (facts[fid].get('_reportedSourceRole') or {}).get('role')
                    clinical_behavior = any(
                        item.get('role') == 'reported_behavior' for item in facts[fid].get('_clinicalPatientRows', ())
                    )
                    if (
                        nature
                        in {
                            'reported_symptom',
                            'reported_history',
                            'qualified_history_context',
                            'management_goal',
                            'reported_goal',
                        }
                        or facts[fid].get('kind') == 'clinical'
                        and not clinical_behavior
                        or facts[fid].get('kind') not in {'clinical', 'self_reported'}
                    ):
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
            rows.append(row)
        result[section] = rows
    for slot in symptom_slots:
        identifiers = {reverse[fid] for fid in slot.get('factIds', ()) if fid in reverse}
        affirmed = {
            fid
            for fid in identifiers
            if (facts[fid].get('_reportedSourceRole') or {}).get('role') == 'reported_symptom'
        }
        if affirmed and not any(affirmed.intersection(row['factIds']) for row in result['symptoms']):
            content_issues.append(
                f'symptoms: retain affirmed symptom {slot.get("name")} from '
                f'{[handles[fid] for fid in affirmed]}; preserve its original time/conditions. '
                'Unsupported clinical or microbiome relationships may be omitted, not the affirmed symptom itself'
            )
    if check_workup_coverage:
        covered = {code for row in result['clusters'] for code in row['priorityWorkupCodes']}
        for code, clinical in confirmed_workups.items():
            if clinical and code not in covered:
                content_issues.append(
                    f'clusters: source-bound diagnostic evaluation {code} '
                    'has not been assigned a workup theme'
                )
    cluster_codes = {row['code'] for row in result['clusters']}
    for row in result['lifestyleFactors']:
        codes = row.get('clusterCodes', [])
        if not isinstance(codes, list) or any(code not in cluster_codes for code in codes):
            raise ValueError('content_plan_unknown_cluster')
    if content_issues:
        raise ValueError('; '.join(content_issues))
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
    return {'clinicalFollowupsOutsideThemes': followups, 'microbiomePanels': panels}


async def _plan_section(client, packet, person, sections, accepted, facts, handles, journal, *, label=None):
    from .evaluation_report import _write_private_diagnostic

    started = monotonic()
    section = '+'.join(sections)
    diagnostic = label or section
    cell_stage = sections == ('cellScopes',)
    patient = {key: value for key, value in packet.items() if cell_stage or key != 'cells'}
    scopes = {row['code']: row for row in packet['cells']}
    schema = _contract(sections)
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
    if cell_stage:
        # Existing table ownership is authoritative. The review call can add
        # relevant context directly to this source-backed baseline.
        draft = {'cellScopes': [
            {'code': row['code'], 'factIds': row['factIds'], 'purpose': row['question'],
             'contextPurpose': row['question'], 'contextFactIds': [],
             'contextStatus': 'no_additional_context', 'relations': []}
            for row in scopes.values()
        ]}
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
    unrequested_cell_codes = []
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
        try:
            if not isinstance(reviewed, dict) or any(
                (reviewed.get('checks') or {}).get(key) is not True for key in _CHECKS
            ):
                raise ValueError('content_plan_independent_review_incomplete')
            subset = reviewed.get('plan')
            if not isinstance(subset, dict) or any(not isinstance(subset.get(key), list) for key in sections):
                raise ValueError('content_plan_sections_missing')
            if 'lifestyleFactors' in sections:
                subset = {**subset, 'lifestyleFactors': [
                    {**row, 'clusterCodes': []} if isinstance(row, dict) else row
                    for row in subset['lifestyleFactors']
                ]}
            if cell_stage:
                # Each batch owns only its requested cells. A model may echo
                # another cell from the shared source catalogue; quarantine
                # that output rather than invalidate all independently
                # reviewed cells or let it overwrite another batch's owner.
                returned = {code: [] for code in scopes}
                for row in subset['cellScopes']:
                    code = row.get('code') if isinstance(row, dict) else None
                    if not isinstance(code, str) or code not in returned:
                        unexpected = code if isinstance(code, str) else '<invalid-cell-code>'
                        if unexpected not in unrequested_cell_codes:
                            unrequested_cell_codes.append(unexpected)
                        continue
                    returned[code].append(row)
                missing = [code for code, rows in returned.items() if not rows]
                duplicated = [code for code, rows in returned.items() if len(rows) > 1]
                if missing or duplicated:
                    raise ValueError(
                        f'cellScopes: missing={missing}; duplicated={duplicated}; '
                        f'return each requestedCellCodes entry exactly once: {list(scopes)}'
                    )
                subset = {**subset, 'cellScopes': [rows[0] for rows in returned.values()]}
                # The planner owns relationships, not the identity or purpose
                # of a source-bound table cell.
                for row in subset['cellScopes']:
                    if any(field not in row for field in ('contextPurpose', 'contextStatus', 'relations')):
                        raise ValueError('cellScopes: contextPurpose, contextStatus and relations are required')
                    row['factIds'] = list(scopes[row['code']]['factIds'])
                    row['purpose'] = scopes[row['code']]['question']
            combined = {key: deepcopy(accepted.get(key, [])) for key in _SECTIONS}
            combined.update({key: subset[key] for key in sections})
            decoded = _decode(
                combined,
                facts,
                handles,
                scopes,
                _problem_metadata(packet),
                symptom_slots=packet.get('symptomSlots', ()) if 'symptoms' in sections else (),
                check_workup_coverage='clusters' in sections,
            )
            if 'symptoms' in sections:
                for original, resolved in zip(subset['symptoms'], decoded['symptoms'], strict=True):
                    original.update(name=resolved['name'], purpose=resolved['purpose'],
                        factIds=[handles[fid] for fid in resolved['factIds']],
                        contextFactIds=[handles[fid] for fid in resolved['contextFactIds']])
            if 'clusters' in sections:
                for original, resolved in zip(subset['clusters'], decoded['clusters'], strict=True):
                    original.update(
                        priority=resolved['priority'],
                        purpose=resolved['purpose'],
                        sourceOnlyPendingMicrobiomeFactIds=[
                            handles[fid] for fid in resolved.get('sourceOnlyPendingMicrobiomeFactIds', ())
                        ],
                    )
            journal[diagnostic] = {
                'status': 'reviewed', 'rows': {key: len(subset[key]) for key in sections},
                'elapsedMs': round((monotonic() - started) * 1000, 3),
                **({'unrequestedCellCodesIgnored': unrequested_cell_codes} if unrequested_cell_codes else {}),
            }
            return {key: subset[key] for key in sections}
        except (ValueError, TypeError) as error:
            if attempt:
                raise
            feedback = [str(error)]
            draft = reviewed.get('plan', reviewed) if isinstance(reviewed, dict) else reviewed
    raise ValueError('content_plan_review_missing')


async def plan_assessment_content(client, blueprint, subject, *, deadline=None, audit=None):
    """Return an independently reviewed source plan, or an explicit empty receipt."""
    unavailable = {'version': PLAN_VERSION, 'status': 'unavailable', **{key: [] for key in _SECTIONS}}
    journal = audit if isinstance(audit, dict) else {}
    started = monotonic()
    facts, handles, packet = _snapshot(blueprint)
    if not facts:
        journal['contentPlan'] = {'status': 'unavailable', 'reason': 'no_source_facts'}
        return unavailable
    remaining = None if deadline is None else max(0.0, deadline - started)
    budget = min(720.0, remaining) if remaining is not None else 720.0
    if budget <= 0:
        return unavailable
    person = subject.model_dump(mode='json', by_alias=True) if hasattr(subject, 'model_dump') else subject
    from .evaluation_report import _write_private_diagnostic

    _write_private_diagnostic('content-plan-sources', packet)
    stages = {}
    try:
        async with asyncio.timeout(budget):
            empty = {key: [] for key in _SECTIONS}
            # Each planning duty reviews its own source anchors concurrently.
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
            cluster_task = asyncio.create_task(
                _plan_section(client, packet, person, ('clusters',), empty, facts, handles, stages)
            )

            async def plan_cells(index, cells):
                async with limiter:
                    return await _plan_section(
                        client,
                        {**packet, 'cells': cells},
                        person,
                        ('cellScopes',),
                        empty,
                        facts,
                        handles,
                        stages,
                        label=f'cellScopes-{index:02d}',
                    )

            symptoms_task = asyncio.create_task(
                _plan_section(client, packet, person, ('symptoms',), empty, facts, handles, stages)
            )
            tasks = [
                cluster_task,
                symptoms_task,
                asyncio.create_task(_plan_section(
                    client, packet, person, ('lifestyleFactors', 'recognitions'), empty, facts, handles, stages,
                )),
                *(asyncio.create_task(plan_cells(index, stable[start : start + 24]))
                    for index, start in enumerate(range(0, len(stable), 24), 1)
                ),
            ]
            try:
                clusters, symptoms, lifestyle, *scoped = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            accepted = {**empty, **clusters, **symptoms, **lifestyle}
            accepted['lifestyleFactors'] = _connect_lifestyle_to_clusters(
                accepted['lifestyleFactors'], accepted['clusters'],
            )
            derived = {row['code']: row for row in _related_scopes(accepted, packet['cells'])}
            for subset in scoped:
                derived.update({row['code']: row for row in subset['cellScopes']})
            accepted['cellScopes'] = list(derived.values())
            result = _decode(
                accepted,
                facts,
                handles,
                {row['code']: row for row in packet['cells']},
                _problem_metadata(packet),
                symptom_slots=packet.get('symptomSlots', ()),
            )
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _write_private_diagnostic(
            'content-plan-failure',
            {
                'errorType': type(error).__name__,
                'reason': str(error),
                'stages': stages,
            },
        )
        journal['contentPlan'] = {
            'status': 'unavailable',
            'reason': type(error).__name__ + ':' + str(error)[:160],
            'elapsedMs': round((monotonic() - started) * 1000, 3),
        }
        return unavailable
    signature = json.dumps(records := packet['records'], ensure_ascii=False, sort_keys=True).encode()
    journal['contentPlan'] = {
        'status': 'reviewed',
        'sourceSha256': hashlib.sha256(signature).hexdigest(),
        'sourceCount': len(records),
        'plan': deepcopy(result),
        'stages': stages,
        'elapsedMs': round((monotonic() - started) * 1000, 3),
    }
    return result
