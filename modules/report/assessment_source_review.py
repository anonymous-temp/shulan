"""Private grounding of immutable, medically reviewed report paragraphs.

O handles describe source occurrences, never inferred clinical equivalence.
P handles and server hashes bind proofs to the exact text being published.
The independent review reads typed source authority. This module validates its
completeness and identity; it does not edit medical prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy

from .assessment_measurement_claims import CLAIM_USES
from .assessment_measurement_claims import advice_authority_issue
from .assessment_measurement_claims import claim_authority_issue
from .assessment_measurement_claims import exact_advice_support
from .assessment_measurement_claims import paragraph_claim_candidates
from .assessment_measurement_claims import paragraph_literal_issues
from .assessment_source_receipts import ACTION_PASSAGES
from .assessment_source_receipts import claim_issue_code
from .assessment_source_receipts import normalize_receipt
from .assessment_source_receipts import renumber_cell_claims

SOURCE_REVIEW_VERSION = 'source-grounding-v20'
_VIOLATION_KINDS = {
    'source_fact',
    'clinical_assessment',
    'acquisition_condition',
    'microbiome_measurement',
    'event_relation',
    'behavior_scope',
    'original_advice_scope',
    'activity_applicability',
    'evidence_applicability',
}
_OBSERVATION_FIELDS = (
    'name',
    'value',
    'unit',
    'referenceRange',
    'status',
    'observedAt',
    'sourceReportedAge',
    'assessmentState',
    'referenceAssessmentState',
    'sourceStatusState',
    'statusReferenceConflict',
    'sourceFindingState',
    'sourceFindingQuote',
    'sourceFindingQualifier',
    'normalityAuthority',
    'sourceRole',
    'sourceNature',
    'sourceContext',
    'sourceSpans',
    'acquisitionContext',
    'acquisitionConditions',
    'sourceObservation',
    'observationType',
    'sectionRole',
    'sourceLabel',
)

GROUNDING_GUIDE = [
    '这是独立的最终来源审查，正文已冻结，不得改写。逐个核对所有C的全部P，返回每P的sourceVerdict。'
    'supported仅在该P的全部实际命题核对后使用；只把真实来源错误列入violations，不抄写已支持命题的证明。'
    '服务端私有绑定版本和来源，不回传hash或反复抄写已确认的来源原句。',
    'claimCandidates是少量易混命题的审查线索。仍逐P阅读全文。每K只回id/use，'
    'observed_measurement或applied_threshold另选该实际测值的observationIds，reported_advice另选精确adviceIds。'
    'patient_management只列本K实际用到的passageIds，无需E时省去。单一动作和单一E明确对应时可省。'
    '没有K可省去claimDecisions。'
    '按整句、标题和前后段理解指代、否定与条件范围。'
    '称谓、来源方用语和套话等文风问题由出版前的确定性措辞整理处理，不属于来源错误，不列入violations。',
    'comparisonClaims逐项指明所用的对照及其个人测项。检验单参考范围、目标表中的目标区间与筛查切点分开核，'
    '明确注明目标区间的偏高或偏低不改变检验单结论。筛查带只能说明符合其前提下的筛查位置。'
    '逐个分句并跨相邻P核“同日、当日、同期”的先行事件。区分检查日、问卷回答日、收样日与报告日。'
    'microbiomeDates没有采样日时不能由其他日期补出，问卷提交时间也不等于回顾事件发生时间。',
    'acquisition_condition的use：observed_measurement=本人的已发生测量/状态，'
    'proposed_test=明确提出未来取得该条件的检查，denied_or_general=真正否定该限定或一般定义。'
    '不能因空腹血糖正常而排除某病，仍预设了空腹测量；否定排除结论不否定内嵌测量。'
    '建议复查空腹血糖；本次葡萄糖原值是两个不同命题，不能因附近有数值就强绑未来条件。'
    '具体来源不能定位时不要选另一测项或事件来补条件；返回真实问题。',
    'external_threshold的use：applied_threshold=正文以该有前提界值判定本人的测项已经符合或不符合标准。'
    '若只引用通用定义、明确条件未知不能判，或仅比较本项打印参考范围，返回not_applied。'
    '对于实际应用，核该测项所指的确切O，不能用同类同值的另一日期、原参考区间或并列另一指标补采集前提。'
    '引用中的criterion保留该单项前提。不能把同一引文中别的并列指标的限定移给本项。'
    '逐P对照实际原段、原题名及scope中的管辖语境。缺少能确定本项适用条件的完整源句时，只能作为通用背景；'
    '不得因当前引用摘录没有重复段外条件就以该数字判定本人是否达标。',
    '每个patient_action K单独判断，不能用同P真实原医嘱代替另一个新动作的判断。'
    '具体可执行的本报告行动用patient_management，准确转述用reported_advice并给本命题覆盖的精确A编号。'
    '病例既有药物、已发生过的活动、一般管理目标和对信息的解释不填patient_management来充数。'
    '不得只挑术语相似的A；分清原医嘱与设备原建议，核科室、项目、动作强度、间隔、原对象及原条件。'
    '复查与就诊并列时，说明不把前者当成后者的先决条件，是解释原文直接性，不是新增患者行动。'
    '区别“复查仍异常才进一步查因”和“复查仍异常才就诊”。限定条件只对应原句限定的具体动作。'
    '实际role=action中的每个E只能挂到它确实支持的patient_management命题。'
    '无适用E可直接使用适当的原始F和医学复核；原医嘱用明确A/F，无须给原医嘱找旁证。'
    '若一个K仍同时含原医嘱和新增安排，用mixed_action，需重新分P。'
    '知识、状态、既往用药、否定推论、解释性关注或评估判断依据用not_action。'
    '只有实际安排执行的动作接受action role及E用途审查。还须逐项审查未被K词形捕获的真实行动。',
    'event_relation的use：established_link=实际肯定回顾留样就是所引临床事件/标本，'
    'unestablished_or_other_event=该句真正未建立关系或明确指另一个事件。'
    '菌群检测说明“该样本中的靶标序列”是其自身检测对象，不等于它与临床或回顾事件共用同份标本。'
    '是否同一标本尚未确认的后置否定是合法范围；先肯定该次临床标本、后一句写未知，不能撤销前句绑定。',
    '先分清本人事实、原医生评估、一般医学知识、本报告的临床推理与新建议。只核正文实际采用的命题，'
    '未采用的背景、未逐字重列全部正常项、没有重复原医嘱，不是来源错误。F出现在背景中不等于正文断言了该F的所有内容。',
    '已独立医学审核的通用生理定义不要求在患者检测值中再次实测证明；症状否认、既往史和用药回答也有各自原事实，'
    '不因不属于测量O或生活方式列表而失效。仍核这些知识是否被误推成患者本人的机制、状态或因果。',
    '按实际observations逐项核数值、单位、打印区间、源状态、原文所见和限定。组内有项目缺参考或原状态时，'
    '不得写全部在参考范围内。记录为0不补出判定。影像原文仅见/未见征象或未见明显异常时须保留这种限定。'
    'derivedIndex逐项标明公式、实际输入和实测分级。不同公式不共用输入列表，按标准分级不改变测值的实测来源。'
    '正常或异常的原医生评估可准确转述，但不能冒充独立测量；原总结与实测有冲突时保留方向差异。',
    'primaryRecordIds、sourceRow及templateDuty确定本格实际检测对象；contextRecordIds只是已审核的关联背景。'
    '背景中的另一种assay不能改写本格结果的测量类型、对象或阴性含义；共同患者、同名菌及相关功能不等于同一检测。',
    'measurementClaims.typedMeasurementGroups逐F给出真实测量类型。逐句解析上述/这些/均等总括指代的实际集合：'
    '同P既有功能分数又有菌丰度时，不能把不同类型统一定义成相对丰度；限定需在实际命题中明确，不能靠后置边界补救。',
    '每项检查的采集条件只来自它自己的acquisitionConditions。分类code、同日另一空腹检查、参考区间'
    '或体检准备问卷都不能补出本项空腹；无本项条件时不得借此计算需要该条件的指数或套用其界值。',
    '逐实际微生态测量对象检查含义。相对丰度0只证明本样本报告了0份额，不能写成未检出、未测到、'
    '检测限以下或体内缺失；仅表示/只说明引出的定义句也须核对。真正的不能推出/不等于保留否定范围。'
    '只有detection.status明确not_detected的靶标序列支持序列阴性；只有analyticalLimits实际reported'
    '才可讨论相应限值。面板无异常仅是该assay的比较结论。',
    '面板汇总只说明所属比较。介绍汇总后列出的每个物种也要核对来源面板，构成图谱独立列出。'
    '候选毒力/耐药基因与实际检出的基因分开；源指数、菌群关联和功能预测不升级为患者疾病概率、'
    '人体营养素/递质/毒素浓度、临床缺乏或宿主免疫状态。即使只写指标名，也须保留各项原分档及共有端点。',
    '功能预测的组名不拥有成员共用档位。遇到“某组评分偏低”等合称，核对实际纳入的每项。'
    '报告的逐项明示档位、组报箭头、数字匹配的区间分别保存。若逐项原档已经明确且与数值相符，仅保留数字交界不应撤销该档。'
    '没有逐项明示时，原箭头不能给共有端点定档。可如实表达部分项目降低，并分别列明已知分档。',
    '核回顾事件、行为及时间作用域：没有共同eventId和关联许可不能写同期或伴随；问卷提交日不等于事件日。'
    '行为题目中的目标、既往事件、当前习惯、否定范围彼此不同；不同源日期不合成一个当前状态。'
    '标题、先列有日期结果再写“留样时自述”等语序，也可能暗示同一标本。须说明回顾日期或标本对应未知；'
    '没有回顾日期时，也不能肯定两个记录来自不同日期或标本。'
    '跨页同值不自动证明同次测量，年龄/日期差异不能借同日等修饰语合并。'
    '逐句及跨段核对“该次、当时、该标本、这份标本”等指代实际绑定了什么事件。'
    '同段先写有日期的检验后，用确定指代称回顾事件发生于那个标本，属于实际绑定。'
    '后句写关系未知不能撤销前句已作的同次绑定。'
    '建议增加某行为，不是声称本人已经有该行为，不要误当作行为原事实。',
    'sourceAdvice是原医嘱，只在正文转述原医生安排时核其直接性与条件；直接复查和就诊不能改为复查仍异常才就诊。'
    '本报告依据个人资料作出的新临床建议另有职责：常规就医评估、核对矛盾检验等，不要求原始记录已经逐字开出同一医嘱。'
    '它们已接受独立医学审核，此阶段核实际适用性、是否冒充原医嘱、是否违背个人约束；'
    '不能仅因F未写过这条新建议就判unsupported。sourceAdvice库存可来自本格已审核关联的原记录，'
    '不要求作者为准确转述原医嘱额外引用一个尚未使用的F。'
    '复合原建议须逐动作核对象与条件：并列病灶或同F不同器官不能共享各自检查项目、周期或就诊条件；'
    '“如饮酒者”等条件不可删成人人必须执行，复查与专科并列不可改成先后门槛。'
    '新的诊断、因果、特殊干预和具体界值仍须有相应依据。',
    '活动安排核真实约束如何影响起步和递进，可多个域共同支撑，不要求每个正常观察重复一套动作。'
    '既不遗漏会改变活动安全的重要所见，也不由轻微发现凭空新增禁忌或先就医门槛。',
    '每条实际引用逐一核对象、人群、方法、终点和用途。只为一般背景的二手转述、其它设备/动物/专病证据，'
    '不能给本例行动背书；某F可独立支持原医嘱，但不因此使并引的不相关E有效。'
    '按P的实际role及passageRoles判断引用用途；文献真实内容不能因题名含同词就认定适用。',
    '方法或编写检查报告的指引即使真实，也不能代替本例结果的意义。只应承接其中适用的临床命题，'
    '不能把作者应如何写报告的流程内容当患者解读而判通过。',
    'violations只报告真实错误：quote须是该P.text或title中未改一字的连续原句，reason指出具体错误和实际依据，'
    'recordIds/passageIds只选该P实际使用的F/E，不要求补不存在的来源。没有错误用supported和空violations。'
    '格式修复只补遗漏P/回执字段，不改正文，不把原句的错误换一个枚举伪装为通过。',
]


def _hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            default=str,
        ).encode()
    ).hexdigest()


def paragraph_version(paragraphs):
    return _hash(
        [
            {
                'id': f'P{index:02d}',
                'title': part.get('title'),
                'text': part['text'],
                'recordIds': list(part.get('factIds') or ()),
                'passageIds': list(part.get('cite') or ()),
                'role': part.get('role'),
            }
            for index, part in enumerate(paragraphs, 1)
        ]
    )


def short_observation_ids(value, registry):
    if isinstance(value, list):
        return [short_observation_ids(item, registry) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, item in value.items():
        if key == 'observationId' and isinstance(item, str):
            result[key] = registry['handles'].get(item, item)
        elif key == 'observationIds' and isinstance(item, list):
            result[key] = [registry['handles'].get(identity, identity) for identity in item]
        else:
            result[key] = short_observation_ids(item, registry)
    return result


def build_observation_registry(groups):
    """One private canonical map for the report; do not coalesce equal values."""
    canonical = {}
    group_members = {}
    for group in groups:
        code = group.get('code')
        if not code:
            continue
        ids = []
        for member in group.get('members', ()):
            identity = member.get('observationId')
            if not isinstance(identity, str) or not identity:
                continue
            canonical.setdefault(identity, deepcopy(member))
            ids.append(identity)
        group_members[code] = list(dict.fromkeys(ids))
    handles = {identity: f'O{index:04d}' for index, identity in enumerate(sorted(canonical), 1)}
    return {
        'canonical': canonical,
        'handles': handles,
        'reverse': {handle: identity for identity, handle in handles.items()},
        'groups': {code: [handles[identity] for identity in ids] for code, ids in group_members.items()},
    }


def model_observation_groups(groups, registry):
    result = []
    for group in groups:
        members = []
        for member in group.get('members', ()):
            handle = registry['handles'].get(member.get('observationId'))
            if handle is None and member.get('id') in registry['reverse']:
                handle = member['id']
            if not handle:
                continue
            members.append(
                {
                    'id': handle,
                    'recordIds': list(
                        dict.fromkeys(
                            rid
                            for rid in (
                                member.get('recordId'),
                                *member.get('recordIds', ()),
                            )
                            if rid
                        )
                    ),
                    **{key: deepcopy(member[key]) for key in _OBSERVATION_FIELDS if key in member},
                }
            )
        result.append(
            {
                'code': group['code'],
                'name': group.get('name'),
                'memberIds': [member['id'] for member in members],
                'members': members,
            }
        )
    return result


def model_source_contract(contract, registry):
    """Short identities for author/reviewer, without requiring proof output."""
    value = short_observation_ids(contract, registry)
    value['clinicalObservationGroups'] = model_observation_groups(
        contract.get('clinicalObservationGroups', ()),
        registry,
    )
    value.pop('reviewObligations', None)
    value.pop('requiredChecks', None)
    return value


def medical_source_contract(contract):
    """Medical review reads admitted records; it does not assemble O receipts."""
    return {key: deepcopy(value) for key, value in contract.items() if key != 'clinicalObservationGroups'}


def compact_microbiome_observation(observation):
    value = deepcopy(observation)
    value.pop('observationIds', None)
    value.pop('observationLocators', None)
    if isinstance(value.get('observations'), list):
        value['observations'] = [compact_microbiome_observation(item) for item in value['observations']]
    return value


def proof_output_schema():
    return {
        'cells': [
            {
                'id': 'C01',
                'paragraphs': [
                    {
                        'id': 'P01；逐个覆盖本C全部P',
                        'sourceVerdict': 'supported|unsupported',
                        'claimDecisions': [
                            {
                                'id': '该P内服务器给出的K；无K时空数组',
                                'use': '按K.kind取frame说明的语义类别',
                                'observationIds': ['仅observed_measurement/applied_threshold取实际来源O'],
                                'adviceIds': ['仅reported_advice取覆盖本K所有转述的精确原医嘱A'],
                                'passageIds': ['仅patient_management取直接支持本动作的E，没有时省略'],
                            }
                        ],
                        'violations': [
                            {
                                'kind': '|'.join(sorted(_VIOLATION_KINDS)),
                                'quote': '该P.text或title中实际有错的连续原句，不改字',
                                'recordIds': ['该命题实际使用的F；没有则空数组'],
                                'passageIds': ['该命题实际引用的E；没有则空数组'],
                                'reason': '具体语义错误及实际源依据；不改正文',
                            }
                        ],
                    }
                ],
            }
        ]
    }


def _criterion_passages(passage):
    """Keep the cited text and admitted literal scopes separate."""
    return tuple(
        dict.fromkeys(
            text
            for text in (
                passage.get('text'),
                (passage.get('publication') or {}).get('title'),
                *(
                    scope.get('sourceQuote')
                    for scope in passage.get('sourceScope') or ()
                    if scope.get('governingContext')
                ),
            )
            if isinstance(text, str) and text
        )
    )


def _panel_labels(records):
    labels = set()
    for record in records:
        observation = record.get('sourceObservation') or {}
        for item in observation.get('observations') or [observation]:
            labels.update(str(label) for label in item.get('sourcePanelLabels') or ())
    return {label for label in labels if label}


def draft_source_findings(cell, parts, records):
    """Show the medical editor source issues while the draft can still be corrected."""
    groups = cell.get('sourceContract', {}).get('clinicalObservationGroups', ())
    observations = [member for group in groups for member in group.get('members', ())]
    panel_labels = _panel_labels(records.values())
    findings = []
    previous_records = []
    previous_text = ''
    for index, part in enumerate(parts, 1):
        bound = [records[rid] for rid in part['factIds'] if rid in records]
        issues = paragraph_literal_issues(
            {**part, 'recordIds': part['factIds']}, observations, bound,
            panel_labels=panel_labels, clinical_groups=groups, comparison_claims=cell.get('comparisonClaims', ()),
            previous={'text': previous_text, 'records': previous_records}, microbiome_dates=cell.get('microbiomeDates'),
        )
        if re.search(r'同日|当日|当天|同期|同次|同时期', part['text']):
            times = {}
            for row in [*previous_records, *bound]:
                obs = row.get('sourceObservation') or {}
                acq = obs.get('acquisitionContext') or {}
                when = acq.get('observedAt') or row.get('reportedAt') or row.get('date')
                if not when and (obs.get('assayIds') or obs.get('sourcePanel') or obs.get('observations')):
                    microbiome = cell.get('microbiomeDates') or {}
                    when = (microbiome.get('sampleDate') or
                            (f"收样 {microbiome['receivedDate']}" if microbiome.get('receivedDate') else None) or
                            (f"报告 {microbiome['reportDate']}" if microbiome.get('reportDate') else None))
                if when:
                    times[row['id']] = str(when)
            if len(set(times.values())) > 1:
                by_time = {}
                for rid, when in times.items():
                    by_time.setdefault(when, []).append(rid)
                dates = '、'.join(f'{when}（{"/".join(ids)}）' for when, ids in by_time.items())
                issues.append('核实时间指代对象，分别写检查日、问卷回答日、收样日或报告日。' + dates)
        if part.get('role') != 'action' and re.search(r'建议|请前往|需复查|需就诊|应复查|应就诊', part['text']):
            issues.append('核实这句是否有给患者的实际行动。如有且本格允许，请单列action，引用该动作的原医嘱'
                          '或适用依据。如本格只负责解释，保留发现、优先关注的理由及结论。')
        if part.get('cite') and len(re.split(r'[。！？](?=\S)', part['text'])) > 1:
            issues.append('核对每句所需的依据，将文献直接支持的判断与个人实测或原医嘱分开绑定。')
        if issues:
            findings.append({'paragraph': index, 'issues': issues})
        previous_records = bound
        previous_text = part['text']
    return findings


def build_source_frame(cells, paragraphs, *, registry, records, evidence, subject=None, clinical_actions=()):
    """Return a compact model frame and private exact-text/source binding."""
    records_by_id = {record['id']: deepcopy(record) for record in records if record.get('id')}
    panel_labels = _panel_labels(records_by_id.values())
    passage_by_id = {
        passage['pid']: {
            **deepcopy(passage),
            'publication': {key: deepcopy(entry[key]) for key in ('title', 'type', 'year', 'source') if key in entry},
        }
        for entry in evidence
        for passage in entry.get('passages', ())
    }
    model_cells = []
    shared_observations = {}
    private = {'cells': {}, 'registry': registry}
    for cell_id, parts in paragraphs.items():
        cell = cells[cell_id]
        immutable = [
            {
                'id': f'P{index:02d}',
                'title': part.get('title'),
                'text': part['text'],
                'recordIds': list(part.get('factIds') or ()),
                'passageIds': list(part.get('cite') or ()),
                'role': part.get('role'),
            }
            for index, part in enumerate(parts, 1)
        ]
        version = paragraph_version(parts)
        used = {rid for part in immutable for rid in part['recordIds']}
        cited = {pid for part in immutable for pid in part['passageIds']}
        primary_ids = list(dict.fromkeys(cell.get('recordIds') or ()))
        context_ids = list(dict.fromkeys(cell.get('contextRecordIds') or ()))
        authorized = used | set(primary_ids) | set(context_ids)
        contract = model_source_contract(cell.get('sourceContract') or {}, registry)
        groups = contract.pop('clinicalObservationGroups', [])
        shared_observations.update({member['id']: member for group in groups for member in group['members']})
        local_observations = [member for group in groups for member in group['members']]
        literal_issues = {}
        previous_part = None
        for part in immutable:
            part['claimCandidates'] = paragraph_claim_candidates(
                part,
                local_observations,
                contract,
                cell_record_ids=used,
                passages={
                    pid: _criterion_passages(passage_by_id[pid]) for pid in part['passageIds'] if pid in passage_by_id
                },
            )
            bound = [records_by_id[rid] for rid in part['recordIds'] if rid in records_by_id]
            literal_issues[part['id']] = paragraph_literal_issues(
                part,
                local_observations,
                bound,
                panel_labels=panel_labels,
                clinical_groups=groups,
                comparison_claims=cell.get('comparisonClaims', ()), previous=previous_part,
                microbiome_dates=cell.get('microbiomeDates'),
            )
            previous_part = {'text': part['text'], 'records': bound}
        renumber_cell_claims(immutable)
        actions = [
            *clinical_actions,
            *cell.get('sourceClinicalActions', ()),
            *((cell.get('actionContext') or {}).get('originalAdvice') or ()),
        ]
        advice = []
        for action in actions:
            action_ids = set(action.get('recordIds', ()))
            if not action_ids.intersection(authorized):
                continue
            for quote in action.get('originalAdvice', ()):
                if not isinstance(quote, str) or not quote:
                    continue
                scopes = [
                    deepcopy(scope)
                    for scope in action.get('sourceAdviceScopes', ())
                    if isinstance(scope, dict) and scope.get('quote') == quote
                ]
                quote_ids = (
                    list(dict.fromkeys(rid for scope in scopes for rid in scope.get('sourceFactIds', ())))
                    if scopes
                    else list(action.get('recordIds') or ())
                )
                if not set(quote_ids).intersection(authorized):
                    continue
                item = {**deepcopy(action), 'sourceQuote': quote}
                item.pop('originalAdvice', None)
                item['recordIds'] = quote_ids
                item['sourceAdviceScopes'] = scopes
                item['unscopedAdvice'] = [text for text in action.get('unscopedAdvice', ()) if text == quote]
                if item not in advice:
                    advice.append(item)
        advice = [{'id': f'A{index:02d}', **item} for index, item in enumerate(advice, 1)]
        selected_records = []
        for rid in sorted(used):
            if rid not in records_by_id:
                continue
            record = deepcopy(records_by_id[rid])
            # Typed main objects already carry admitted measurement meaning.
            # Keep the literal source private, rather than duplicating aliases.
            record.pop('literalSource', None)
            selected_records.append(record)
        scoped = {
            'id': cell_id,
            'item': cell.get('item'),
            'question': cell.get('question'),
            'sourceRow': deepcopy(cell.get('row') or {}),
            'templateDuty': deepcopy(cell.get('templateDuty') or {}),
            'requiresPersonalAction': cell.get('requiresPersonalAction') is True,
            'primaryRecordIds': primary_ids,
            'contextRecordIds': context_ids,
            'comparisonScope': deepcopy(cell.get('comparisonScope') or {}),
            'comparisonClaims': deepcopy(cell.get('comparisonClaims') or []),
            'microbiomeDates': deepcopy(cell.get('microbiomeDates') or {}),
            'version': 'V' + version[:10],
            'paragraphs': immutable,
            'records': selected_records,
            'clinicalObservationGroups': [
                {key: group[key] for key in ('code', 'name', 'memberIds')} for group in groups
            ],
            'observationIds': list(dict.fromkeys(member['id'] for group in groups for member in group['members'])),
            'sourceBoundaries': {
                key: contract[key]
                for key in (
                    'unknownEvents',
                    'establishedEventRelations',
                    'behaviorScopes',
                    'measurementClaims',
                    'measurementMeaning',
                    'careAdvice',
                    'activity',
                )
                if key in contract
            },
            'sourceSelections': deepcopy(cell.get('sourceSelections') or []),
            'clinicalSelections': deepcopy(cell.get('clinicalSelections') or []),
            'sourceAdvice': advice,
            'evidence': [passage_by_id[pid] for pid in sorted(cited) if pid in passage_by_id],
            'passageRoles': {pid: cell.get('passageRoles', {}).get(pid, []) for pid in sorted(cited)},
        }
        model_cells.append(scoped)
        private['cells'][cell_id] = {
            'versionHash': version,
            'paragraphs': {part['id']: part for part in immutable},
            'groups': {group['code']: group for group in groups},
            'observations': {member['id']: member for group in groups for member in group['members']},
            'records': {record['id']: record for record in selected_records},
            'sourceBoundaries': scoped['sourceBoundaries'],
            'advice': {row['id']: row for row in advice},
            'evidence': {row['pid']: row for row in scoped['evidence']},
            'passageRoles': scoped['passageRoles'],
            'literalIssues': literal_issues,
            'requiresPersonalAction': scoped['requiresPersonalAction'],
        }
    return {
        'version': SOURCE_REVIEW_VERSION,
        'claimUses': deepcopy(CLAIM_USES),
        'subject': deepcopy(subject or {}),
        'observations': list(shared_observations.values()),
        'cells': model_cells,
    }, private


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def _issue(collection, cell_id, location, detail, **extra):
    collection.setdefault(cell_id, []).append(
        {'type': 'source_proof', 'location': location, 'detail': detail, **extra},
    )


def _advice_entries_for_paragraph(source, cell):
    return {
        aid: advice
        for aid, advice in cell['advice'].items()
        if set(advice.get('recordIds', ())).intersection(source['recordIds'])
    }


def _exact_advice_correction(candidate, allowed, record_ids):
    actual_records = set(record_ids)
    matches = []
    for aid, item in allowed.items():
        if not set(item.get('recordIds', ())).issubset(actual_records):
            continue
        scopes = item.get('sourceAdviceScopes') or ()
        if not scopes or not all(
            scope.get('sourceFactIds') and set(scope['sourceFactIds']).issubset(actual_records) for scope in scopes
        ):
            continue
        if exact_advice_support(candidate, item, inventory=allowed.values()):
            matches.append(aid)
    return matches[0] if len(matches) == 1 else None


def _check_claim_decisions(part, source, cell, *, formats, semantic, cell_id, location, advice_corrections):
    expected = {candidate['id']: candidate for candidate in source['claimCandidates']}
    decisions = part.get('claimDecisions', [])
    if not isinstance(decisions, list):
        _issue(formats, cell_id, location, 'claimDecisions须逐个返回本P的K；无K用空数组。')
        return False
    seen, attached = set(), set()
    actual_action = False
    action_candidates = sum(candidate['kind'] == 'patient_action' for candidate in expected.values())
    for decision in decisions:
        kid = decision.get('id') if isinstance(decision, dict) else None
        candidate = expected.get(kid) if isinstance(kid, str) else None
        if candidate is None or kid in seen:
            _issue(formats, cell_id, location, 'K须属于本P且只返回一次。')
            continue
        seen.add(kid)
        use = decision.get('use')
        if not isinstance(use, str) or use not in CLAIM_USES[candidate['kind']]:
            _issue(formats, cell_id, location, f'{kid}的use须取该kind的语义类别。')
            continue
        if not _strings(decision.get('observationIds', [])):
            _issue(formats, cell_id, location, f'{kid}的observationIds须是来源O数组。')
            continue
        if (
            use in {'observed_measurement', 'applied_threshold'}
            and candidate['observationIds']
            and not decision.get('observationIds')
        ):
            _issue(formats, cell_id, location, f'{kid}须定位本K所指的实际来源O，不改正文。')
            continue
        passages = decision.get('passageIds', [])
        if (
            'passageIds' not in decision
            and use == 'patient_management'
            and action_candidates == 1
            and len(source['passageIds']) == 1
        ):
            passages = source['passageIds']
        if (
            not _strings(passages)
            or not set(passages).issubset(source['passageIds'])
            or (passages and use != 'patient_management')
        ):
            _issue(formats, cell_id, location, f'{kid}仅实际新增动作可选择本P实际对应的E。')
            continue
        if use == 'reported_advice':
            allowed = _advice_entries_for_paragraph(source, cell)
            if not allowed:
                _issue(
                    semantic,
                    cell_id,
                    f'{location}.{kid}',
                    f'{source["id"]}/{kid}：所引原记录没有原医嘱库存，新增行动不能作为原医嘱转述。',
                )
                continue
            advice_ids = decision.get('adviceIds')
            if not _strings(advice_ids):
                advice_ids = []
            elif not set(advice_ids).issubset(allowed) and set(advice_ids).intersection(allowed):
                # Receipt bookkeeping: drop A ids this P's records do not admit.
                advice_ids = [aid for aid in advice_ids if aid in allowed]
                advice_corrections.setdefault(kid, {
                    'previousAdviceIds': list(decision.get('adviceIds')), 'adviceIds': list(advice_ids),
                    'recordIds': sorted({rid for aid in advice_ids for rid in allowed[aid].get('recordIds', ())}),
                })
            if not advice_ids or not set(advice_ids).intersection(allowed):
                exact = _exact_advice_correction(candidate, allowed, source['recordIds'])
                if exact:
                    advice_ids = [exact]
                    advice_corrections[kid] = {
                        'previousAdviceIds': list(decision.get('adviceIds') or ()),
                        'adviceIds': [exact],
                        'recordIds': list(allowed[exact]['recordIds']),
                    }
                else:
                    # The reviewer found this retelling in none of the P's original
                    # orders: a content finding for the author, not a receipt gap.
                    _issue(
                        semantic,
                        cell_id,
                        f'{location}.{kid}',
                        f'{source["id"]}/{kid}（{candidate["term"]}）：转述的原医嘱在本段所引原记录的医嘱中找不到对应条目。',
                        paragraph=source['id'],
                    )
                    continue
            permitted = set(advice_ids).issubset(allowed)
            problem = (
                advice_authority_issue(
                    candidate,
                    [allowed[aid] for aid in advice_ids],
                    inventory=allowed.values(),
                )
                if permitted
                else None
            )
            if not permitted or problem:
                exact = _exact_advice_correction(candidate, allowed, source['recordIds'])
                if exact:
                    advice_corrections[kid] = {
                        'previousAdviceIds': list(advice_ids),
                        'adviceIds': [exact],
                        'recordIds': list(allowed[exact]['recordIds']),
                    }
                elif not permitted:
                    _issue(formats, cell_id, location, f'{kid}须定位本命题对应且本P实际F准入的原医嘱A。')
                    continue
                else:
                    _issue(
                        semantic,
                        cell_id,
                        f'{location}.{kid}',
                        f'{source["id"]}/{kid}（{candidate["term"]}）：{problem}',
                    )
                    continue
        if problem := claim_authority_issue(
            candidate,
            {**decision, 'passageIds': passages},
            source,
            cell['observations'],
            passage_roles=cell['passageRoles'],
        ):
            _issue(
                semantic,
                cell_id,
                f'{location}.{kid}',
                f'{source["id"]}/{kid}（{candidate.get("term") or candidate["kind"]}）：{problem}',
                paragraph=source['id'],
                code=claim_issue_code(problem),
                term=candidate.get('term'),
                start=candidate.get('start'),
            )
            continue
        if use in {'patient_management', 'reported_advice'}:
            actual_action = actual_action or source.get('role') == 'action'
        attached.update(passages)
    if seen != set(expected):
        _issue(formats, cell_id, location, '只补遗漏K的语义回执，不改已冻结正文。')
    if seen == set(expected) and source.get('role') == 'action' and set(source['passageIds']) - attached:
        _issue(
            semantic,
            cell_id,
            location,
            '该行动段有E未能支持其中任何实际新增行动。原医嘱仅依本项A/F，解释和背景引文应另放。',
            paragraph=source['id'],
            code=ACTION_PASSAGES,
            passageIds=sorted(set(source['passageIds']) - attached),
        )
    return actual_action


def validate_source_proofs(frame, private, response):
    """Bind a complete independent source review to the immutable final text.

    The reviewer reports semantic exceptions, rather than re-encoding every
    supported fact. Protocol failures only repair the review receipt; real
    source errors return to the author and can never approve a changed text.
    """
    formats, semantic, proofs, paragraph_audit = {}, {}, {}, {}
    receipts, receipt_corrections = {}, {}
    rows = response.get('cells') if isinstance(response, dict) else None
    seen = set()
    for row in rows if isinstance(rows, list) else ():
        cell_id = row.get('id') if isinstance(row, dict) else None
        if not isinstance(cell_id, str) or cell_id not in private['cells']:
            continue
        if cell_id in seen:
            _issue(formats, cell_id, 'id', '同一C重复返回；只修来源审核回执。')
            continue
        seen.add(cell_id)
        cell = private['cells'][cell_id]
        row, receipt_notes = normalize_receipt(row, cell)
        receipts[cell_id] = row
        if receipt_notes:
            receipt_corrections[cell_id] = receipt_notes
        outcomes = paragraph_audit.setdefault(cell_id, {pid: 'missing' for pid in cell['paragraphs']})
        advice_corrections = {}
        parts = row.get('paragraphs')
        if not isinstance(parts, list):
            _issue(formats, cell_id, 'paragraphs', '须逐个覆盖本C全部冻结P。')
            continue
        reviewed = set()
        has_actual_action = False
        for index, part in enumerate(parts):
            location = f'paragraphs[{index}]'
            pid = part.get('id') if isinstance(part, dict) else None
            source = cell['paragraphs'].get(pid) if isinstance(pid, str) else None
            if source is None or pid in reviewed:
                _issue(formats, cell_id, location, 'P须来自本C且只返回一次。')
                continue
            reviewed.add(pid)
            outcomes[pid] = 'format_error'
            verdict, violations = part.get('sourceVerdict'), part.get('violations')
            if (
                not isinstance(verdict, str)
                or verdict not in {'supported', 'unsupported'}
                or not isinstance(violations, list)
            ):
                _issue(formats, cell_id, location, '须返回sourceVerdict及violations数组。')
                continue
            if bool(violations) != (verdict == 'unsupported'):
                _issue(formats, cell_id, location, 'unsupported须有真实错误；supported的violations须为空。')
                continue
            formats_before = len(formats.get(cell_id, ()))
            semantic_before = len(semantic.get(cell_id, ()))
            corrections = {}
            has_actual_action |= _check_claim_decisions(
                part,
                source,
                cell,
                formats=formats,
                semantic=semantic,
                cell_id=cell_id,
                location=location,
                advice_corrections=corrections,
            )
            for problem in cell.get('literalIssues', {}).get(pid, ()):
                _issue(semantic, cell_id, location, f'{pid}：{problem}')
            for issue_index, violation in enumerate(violations):
                issue_location = f'{location}.violations[{issue_index}]'
                if not isinstance(violation, dict):
                    _issue(formats, cell_id, issue_location, '错误须为对象。')
                    continue
                quote, reason = violation.get('quote'), violation.get('reason')
                if (
                    not isinstance(quote, str)
                    or not quote
                    or quote not in source['text']
                    and quote not in (source.get('title') or '')
                ):
                    _issue(formats, cell_id, issue_location, 'quote须为该P.text或title未改字的连续原句；不改正文。')
                    continue
                kind = violation.get('kind')
                if not isinstance(kind, str) or kind not in _VIOLATION_KINDS:
                    _issue(formats, cell_id, issue_location, 'kind须为本协议的真实来源错误类别。')
                    continue
                if not isinstance(reason, str) or not reason.strip():
                    _issue(formats, cell_id, issue_location, '须指出原句具体错误及实际源依据。')
                    continue
                records, passages = violation.get('recordIds'), violation.get('passageIds')
                if (
                    not _strings(records)
                    or not set(records).issubset(source['recordIds'])
                    or not _strings(passages)
                    or not set(passages).issubset(source['passageIds'])
                ):
                    _issue(formats, cell_id, issue_location, 'F/E仅限该P实际使用者；无相应来源时留空数组。')
                    continue
                _issue(semantic, cell_id, issue_location, f'{quote}：{reason}')
            if len(semantic.get(cell_id, ())) > semantic_before:
                outcomes[pid] = 'source_error'
            elif len(formats.get(cell_id, ())) == formats_before:
                outcomes[pid] = 'supported'
                if corrections:
                    advice_corrections[pid] = corrections
        if reviewed != set(cell['paragraphs']):
            _issue(formats, cell_id, 'paragraphs', '必须逐个核对本C全部P，不得漏段。')
        if cell.get('requiresPersonalAction') and cell_id not in formats and not has_actual_action:
            _issue(semantic, cell_id, 'paragraphs', '本格承担个人行动职责，应有可核验的实际原医嘱或本报告新增行动。')
        if cell_id not in formats and cell_id not in semantic:
            proof = deepcopy(row)
            proof['serverVersionHash'] = cell['versionHash']
            proof['serverSourceBindings'] = {
                pid: {key: deepcopy(part[key]) for key in ('recordIds', 'passageIds')}
                for pid, part in cell['paragraphs'].items()
            }
            if advice_corrections:
                for part in proof['paragraphs']:
                    corrected = advice_corrections.get(part['id'], {})
                    for choice in part.get('claimDecisions', ()):
                        if item := corrected.get(choice['id']):
                            choice['adviceIds'] = item['adviceIds']
                proof['serverAdviceCorrections'] = advice_corrections
            proofs[cell_id] = proof
    for cell_id in private['cells']:
        if cell_id not in seen:
            _issue(formats, cell_id, 'id', '缺本格逐P来源审核回执；只修回执，不改医学正文。')
    proofs = {cell_id: proof for cell_id, proof in proofs.items() if cell_id not in formats and cell_id not in semantic}
    return {
        'accepted': list(proofs),
        'formatIssues': formats,
        'semanticIssues': semantic,
        'proofs': proofs,
        'paragraphAudit': paragraph_audit,
        'receipts': receipts,
        'receiptCorrections': receipt_corrections,
    }
