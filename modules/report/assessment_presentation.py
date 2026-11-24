"""Six-chapter view of the source records and the published evaluations."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import date
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

ASSESSMENT_TITLE = '个体化全息健康与肠道微生态深度评估报告'
ASSESSMENT_PREFACE = (
    '本报告综合临床体检与检验、影像资料、健康问卷及肠道菌群宏基因组检测结果，对受检者当前的健康状态作分系统评估，'
    '梳理需优先排查的事项、需协同管理的系统失衡方向和可作为今后对照的健康基线，并给出随访与干预方向。'
    '\n\n'
    '临床资料与菌群资料的采集日期、测量方法和适用范围不同，文中分别标明。'
    '菌群检测反映粪便样本的群落组成与预测功能，与临床检查结果对照解读。'
)
ASSESSMENT_READING_GUIDE = (
    (
        '报告定位',
        '在本次资料覆盖范围内，说明需优先排查的发现、需协同管理的失衡方向，以及可作为今后对照的稳健基线。',
    ),
    (
        '双轨评价',
        '每项检测对照两类区间：临床参考区间取自检验单，用于判断是否超出正常范围；'
        '目标区间取自有文献出处的目标表，用于识别参考区间内的亚临床偏离，'
        '仅在适用于受检者的性别、年龄与健康状况时给出，并标注文献编号。',
    ),
    (
        '结构导览',
        '第一章为全息健康图景概览与优先排查事项；第二章解析主诉症状与客观指标的关联；'
        '第三章与第四章分别为分系统临床检验评估和肠道微生态评估；'
        '第五章归纳系统失衡集群与生活诱因；第六章给出健康风险分层结论与干预方向。',
    ),
)


class AssessmentPresentation(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra='forbid')

    version: Literal['assessment.v1'] = 'assessment.v1'
    title: str = ASSESSMENT_TITLE
    overview: dict[str, Any]
    clinical: dict[str, Any]
    microbiome: dict[str, Any]
    clusters: list[dict[str, Any]]
    cluster_summary: str | None = Field(default=None, alias='clusterSummary')
    cluster_summary_evidence_refs: list[int] = Field(default_factory=list, alias='clusterSummaryEvidenceRefs')
    lifestyle_factors: list[dict[str, Any]] = Field(alias='lifestyleFactors')
    lifestyle_summary: str | None = Field(default=None, alias='lifestyleSummary')
    lifestyle_summary_evidence_refs: list[int] = Field(default_factory=list, alias='lifestyleSummaryEvidenceRefs')
    lifestyle_recognition: list[dict[str, Any]] = Field(default_factory=list, alias='lifestyleRecognition')
    conclusion: dict[str, Any]


_PRIORITIES = {
    '优先查因': 'P0',
    '重点管理': 'P1',
    '近期复评': 'P1',
    '持续管理': 'P2',
    '按症状管理': 'P2',
    '结合症状随访': 'P3',
}
_SHARED_MANAGEMENT = frozenset({'重点管理', '持续管理'})

_SYSTEMS = (
    ('cardiometabolic', '糖脂代谢与心脑血管系统', ('cardiometabolic',)),
    ('digestiveRenal', '肝胆胰消化与肾脏代谢', ('organMetabolism',)),
    ('microbiome', '肠道微生态与黏膜屏障', ()),
    ('endocrineMusculoskeletal', '内分泌激素与骨肌系统', ('endocrineMusculoskeletal',)),
    ('hematologyImmune', '血液免疫、甲基化与氧化应激', ('hematologyImmune',)),
)
NO_TREND_STATEMENT = '本次资料未包含可比较的历次结果。'
_LIFESTYLE_SECTIONS = (
    ('diet', '膳食结构与餐桌模式'),
    ('drinks', '饮酒应酬、饮水与饮品'),
    ('sleep', '睡眠节律与夜间恢复'),
    ('exercise', '运动习惯、久坐与结构约束'),
    ('exposures', '精神压力、环境暴露与口腔护理'),
)
_HABIT_QUESTIONS = (
    ('diet', '睡前夜宵', r'(?:睡前|临睡前).*夜宵|夜宵频率'),
    ('diet', '进餐时间', r'进餐时间|三餐时间|进餐.*规律'),
    ('drinks', '含糖饮料频率', r'^(?!.*甜食).*含糖饮料.*(?:频率|频繁|几天|多少次|多久|经常)'),
    ('diet', '甜食或含糖饮料', r'甜食.*含糖饮料|含糖饮料.*频[率繁]'),
    ('drinks', '常喝饮品', r'喝哪些饮品|常喝.*饮[品料]|常用饮品|主要喝.*饮'),
    ('drinks', '每日饮水', r'喝水.*(?:量|多少)|饮水量|日常饮水'),
    ('drinks', '检查前饮酒', r'体检前[^。？?]{0,18}饮酒'),
    ('drinks', '饮酒', r'饮酒|喝酒|酒精|^alcohol$'),
    ('sleep', '就寝时间', r'准备睡觉|就寝时间|上床时间'),
    ('sleep', '实际入睡时间', r'实际入睡|几点入睡|入睡时间'),
    ('sleep', '睡眠时长', r'睡多长|睡多久|睡眠时长'),
    ('sleep', '睡前活动', r'(?:睡前|临睡前).*(?:做哪些|做什么|活动)'),
    ('exercise', '安排运动', r'几天安排运动|每周运动|运动频[率次]'),
    ('exercise', '起身间隔', r'多久.*起身|多久.*走动|连续久坐'),
    ('exposures', '二手烟接触', r'二手烟|被动吸烟'),
    ('exposures', '主动吸烟', r'抽烟|吸烟'),
    ('exposures', '口腔清洁', r'刷牙|牙线|口腔清洁'),
)
_SYMPTOM_HABITS = (
    (r'便秘|排便困难|大便干|排便费力', {'每日饮水', '安排运动', '起身间隔'}),
    (r'反酸|烧心|反流', {'睡前夜宵', '进餐时间'}),
    (r'入睡|睡眠|失眠|早醒|疲劳|乏力|困倦', {'就寝时间', '实际入睡时间', '睡眠时长', '睡前活动'}),
    (r'肩颈|颈痛|肩痛|腰背|腰痛', {'安排运动', '起身间隔'}),
)
_SYMPTOM_OBSERVATIONS = (
    (r'便秘|排便|粪便|腹泻|腹胀|胀气', r'粪便|大便|肠镜|腹部查体',
     {'scfa', 'carbohydrate', 'beneficial', 'composition'}),
    (r'反酸|烧心|反流', r'胃镜|食管|贲门|反流', set()),
    (r'入睡|睡眠|失眠|早醒', r'睡眠监测|睡眠呼吸|呼吸暂停|PSG', set()),
    (r'疲劳|乏力|困倦', r'血红蛋白|铁蛋白|甲状腺功能|促甲状腺激素|游离甲状腺素|睡眠监测', set()),
    (r'肩颈|颈痛|肩痛|腰背|腰痛', r'颈椎|肩关节|脊柱|腰椎|肩部查体', set()),
)
_UNSUPPORTED_ASSOCIATION = re.compile(r'不(?:相关|用于|支持|构成|能解释)|无关|无法关联|未建立|未确立|尚需研究')
_OBSERVATIONAL_DESIGN = re.compile(
    r'横断面|病例[对對]照|观察性|队列|cross[- ]sectional|case[- ]control|observational|cohort', re.I,
)
_EXPERIMENTAL_DESIGN = re.compile(
    r'随机对照|补充试验|干预试验|randomi[sz]ed|clinical trial|placebo[- ]controlled|supplementation', re.I,
)
_EXPERIMENTAL_RESULT = re.compile(
    r'体外|动物实验|干预后|补充后|in vitro|ex vivo|replenish|after intervention|after supplementation', re.I,
)
_ASSOCIATION_RESULT = re.compile(
    r'相关|丰度|高于|低于|富集|偏低|偏高|association|correlat|abundance|enrich|deplet|decreas|increas|higher|lower',
    re.I,
)
_FERMENTATION_TRIGGER = re.compile(
    r'(?:进食|饮用|食用|吃|喝)?(?:奶|乳制品|豆类|豆制品|豆浆|发酵食物|发酵食品|膳食纤维|'
    r'洋葱|粗粮|全谷物|蔬菜|水果)[^。；，]{0,16}(?:后|时)[^。；，]{0,16}(?:胀气|腹胀|排气增多|腹泻|腹痛)'
)
_CONCLUSION_LAYERS = (
    ('clinical', '第一层级：临床专科排查与随访（首月）'),
    ('core', '第二层级：核心代谢与肠道微生态干预（90 天）'),
    ('maintenance', '第三层级：营养储备与长期健康维护（持续）'),
)
_LAYER_PRIORITIES = {'core': ('P1',), 'maintenance': ('P2', 'P3')}
_LAYER_EMPTY = {
    'clinical': '本次未发现需在首月内完成的专科排查、随访或补查事项。',
    'core': '本次未形成需在 90 天内集中干预的核心代谢或肠道微生态失衡集群。',
    'maintenance': '本次未形成需协同调理的失衡集群。',
}
_MICROBIOME_ITEMS = (
    ('probiotic', '功效菌选择依据'),
    ('fiber', '益生元与膳食纤维'),
    ('nutrient', '营养素配方方向'),
)
_MTPN_NAME = 'MTPN 个性化功能靶向配方'
_LSMI_NAME = 'LSMI 去诱因个性化生活方式干预'
_MTPN_AFTER_P0 = '存在优先排查事项，先完成第一层级排查，再开始配方干预。'
_MTPN_NO_MICROBIOME = '本次资料未包含可用于确定配方方向的肠道菌群检测结果。'
_LSMI_NO_FACTORS = '本次未记录可供针对性干预的生活诱因。'
_PRIORITY_SECTIONS = (
    ('urgent', '优先排查'),
    ('routine', '常规专科随访'),
    ('gaps', '检测盲区'),
)
_NO_URGENT = '本次未发现需优先就医排查的结果。'
_NO_ROUTINE = '本次未发现需定期专科复查的发现。'
_NO_CLUSTERS = '本次未形成需集中干预的失衡集群。'
_NO_GRAPH = '本次各系统未形成需集中干预的失衡方向。'
_ADVICE_STATUS = '检查建议'
# Recorded advice is marked by ``kind: 'advice'``; the older status marker is still read on input.
_ADVICE_STATUSES = frozenset({_ADVICE_STATUS, '原报告医嘱'})
_SELF_REPORTED_STATUS = '本人自述'
_BASIS_FIELDS = ('name', 'value', 'unit', 'referenceRange', 'status', 'observedAt')
_NOT_PERFORMED = re.compile(r'未做|未查|未检测|未进行|拒检|弃检')
_NORMAL_STATE = re.compile(r'^(?:正常|阴性|未见|参考范围内|在参考|稳健达标|未检出|无异常)')
_BODY_ELEMENTS = re.compile(
    r'体重指数|BMI|腰围|腰臀|体脂|脂肪量|内脏脂肪|骨骼肌|肌肉量|基础代谢|细胞外水|血压|心率', re.I,
)
_TRIGGER_SECTIONS = {
    'diet': 'diet', 'sleep': 'sleep', 'exercise': 'exercise', 'hydration': 'drinks',
    'drinks': 'drinks', 'alcohol': 'drinks', 'stress': 'exposures', 'oralCare': 'exposures',
}


def _unique(values: Iterable):
    return list(dict.fromkeys(values))


def _content_plan(data):
    plan = getattr(data, '_assessment_content_plan', None)
    return plan if isinstance(plan, dict) else None


def _row_fact_ids(rows, facts):
    return _unique(source['recordId'] for row in rows for source in row.get('sources', ())
                   if source.get('recordId') in facts)


def _sample_metadata_rows(data, facts):
    """Use actual source metadata observations, not unbound sample attributes."""
    fields = {
        'sampleType', 'sampleDate', 'samplingDate', 'signDate', 'toBeSentOutDate', 'reportDate',
        'qualityControl', 'preparationQc', 'dataQc', 'effectiveSequences', 'method', 'detectionMethod',
        'sample.sampleType', 'sample.sampleDate', 'sample.reportDate', 'sample.qualityControl',
    }
    rows = []
    for item in data.appendices.source_items:
        if item.source_type != 'microbiome':
            continue
        origins = [source.record_id for source in item.sources if source.record_id]
        if not any(origin.partition(':json:')[2] in fields for origin in origins):
            continue
        sources = [{'recordId': origin} for origin in origins if origin in facts]
        if sources:
            rows.append({'sources': sources})
    for fid, fact in facts.items():
        name = fact.get('name') if isinstance(fact, dict) else fact.name
        if name in {'来源收样日期', '来源报告日期', '来源采样日期'}:
            rows.append({'sources': [{'recordId': fid}]})
    return rows


def _panel_rows(group):
    return [*group.get('rows', ()), *((group.get('normalBaseline') or {}).get('rows') or ()),
            *((group.get('normalBaseline') or {}).get('undetectedRows') or ()),
            *group.get('panelStates', ())]


def _disease_model_rows(view):
    """Select the original disease indices, not functional or organism association scores."""
    return [row for row in view.get('_reportedAssessmentRows', ())
            if row.get('_sourceSemantics', {}).get('measurementType') == 'source_model_risk_assessment']


def _model_fact_ids(view, facts):
    return _unique(fid for row in _disease_model_rows(view)
                   for fid in [*row.get('_semanticOwnerRecordIds', ()), *_row_fact_ids([row], facts)]
                   if fid in facts)


def _reported_comparison_ids(facts):
    """A recalled prior result is useful history even when two measured time points do not exist."""
    terms = re.compile(r'以前也|以往也|既往(?:曾|亦)|之前查过|最近一次查|第一次出现|比(?:上次|此前)|和这次相比')
    return [fid for fid, fact in facts.items() if fact.get('kind') == 'self_reported'
            and terms.search(str(fact.get('name') or '') + ' ' + str(fact.get('value') or ''))]


def _reported_diet_context(facts):
    direct, foods, digestive = [], [], []
    for fid, fact in facts.items():
        if fact.get('kind') != 'self_reported':
            continue
        name = str(fact.get('name') or '')
        text = name + ' ' + str(fact.get('value') or '')
        if _reported_fermentation_trigger(fact):
            direct.append(fid)
        if re.search(r'食物|正餐|全谷物|全谷杂粮|豆制品|膳食纤维', text):
            foods.append(fid)
        if re.search(r'排便|便秘|腹泻|腹胀|胀气|腹痛', name):
            digestive.append(fid)
    return _unique([*direct, *foods, *digestive]) if direct or (foods and digestive) else []


def _reported_model_summary(parsed):
    """Print every reported score with its own reported class, including zero and ungraded scores."""
    by_grade = {}
    for result in parsed.disease_results:
        score = str(result.source_score if result.source_score is not None else '').strip()
        level = str(result.source_level or '').strip()
        if result.name and (score or level):
            by_grade.setdefault(level or '未列分档', []).append(
                f'{result.name}（{score}）' if score else result.name
            )
    if by_grade:
        groups = '；'.join(f'{grade}：{"、".join(_unique(items))}' for grade, items in by_grade.items())
        return ('菌群疾病关联模型的分档及指数：' + groups
                + '。指数表示样本在相应模型中的相对位置，不是个人患病概率；健康管理以临床检查结果为准。')
    return None


def _advice_heading_key(value):
    """Compare explicit source headings without inventing clinical aliases."""
    text = unicodedata.normalize('NFKC', str(value or '')).strip().strip('#*•- ')
    text = re.sub(r'^(?:\(?[0-9一二三四五六七八九十]+\)?[、.．:：)）]\s*)', '', text)
    key = ''.join(text.rstrip(':：').split()).casefold()
    # Projected imaging titles can include the actual lesion size. The source
    # heading names the lesion without repeating that measurement.
    return re.sub(
        r'[（(，,]?(?:大小|长径|最大径|直径)?约?\d+(?:\.\d+)?(?:[×x*]\d+(?:\.\d+)?){0,2}'
        r'(?:厘米|毫米|cm|mm)[）)]?$', '', key,
    )


def _clinical_advice_objects(data, facts):
    """Verified clinical owners provide labels, never inferred organ keywords."""
    from .evaluation_schema import clinical_problem_projection_codes

    frames = getattr(data, '_clinical_problem_projection', ())
    codes = clinical_problem_projection_codes(data) if frames else {}
    objects = []
    for group in getattr(data, '_template_layout', {}).get('groups', ()):
        if group.get('origin') != 'clinical_group':
            continue
        identifiers = {fid for fid in group.get('originalFactIds', group.get('sourceFactIds', ())) if fid in facts}
        labels = [group['name']]
        for frame in frames:
            if codes.get(frame.problem_id) in group.get('memberCodes', ()) and any(
                source.fact_id in identifiers for source in frame.sources
            ):
                labels.append(frame.title)
        objects.append({'code': group['code'], 'recordIds': identifiers,
                        'labels': {_advice_heading_key(label) for label in labels if label}})
    return objects


def _scoped_advice_fragments(source, start, end, *, group_code, objects, owners, source_field, source_labels,
                             finding_text=''):
    """Intersect admitted advice with literal object-heading sections.

    An unheaded single-record instruction keeps its record authority. A
    compound record with no unique heading cannot lend its group's name to
    the instruction. All retained quotes remain continuous source substrings.
    """
    related = [item for item in objects if item['recordIds'].intersection(owners)]
    headings = []
    for match in re.finditer(r'(?m)^([^\r\n]+)(?:\r?\n|$)', source):
        key = _advice_heading_key(match[1])
        targets = {item['code'] for item in related if key and key in item['labels']}
        standalone_object = (source_field == 'sourceAdvice' and 1 < len(key) <= 40
                             and re.fullmatch(r'[\w()（）/、-]+', key) and key in finding_text)
        if targets or standalone_object:
            headings.append((match.start(), match, targets))
    if not any(targets for _, _, targets in headings):
        headings = []
    boundaries = [(0, headings[0][0], None, set())] if headings and headings[0][0] else []
    boundaries.extend((position, headings[index + 1][0] if index + 1 < len(headings) else len(source),
                       match, targets) for index, (position, match, targets) in enumerate(headings))
    if not headings:
        boundaries = [(0, len(source), None, set())]
    fragments = []
    for left, right, heading, targets in boundaries:
        if heading is not None and group_code not in targets:
            continue
        left, right = max(left, start), min(right, end)
        if left >= right:
            continue
        part = source[left:right]
        leading = len(part) - len(part.lstrip())
        quote = part.strip()
        if not quote:
            continue
        left += leading
        right = left + len(quote)
        explicit = heading is not None and targets == {group_code}
        status = ('explicit_object' if explicit else 'source_record_scope'
                  if not headings and len(owners) == 1 and len(related) <= 1 else 'unknown_compound_object')
        fragment = {
            'quote': quote, 'scopeStatus': status,
            'objectLabels': [heading[1].strip()] if explicit else [],
            'sourceRecordLabels': list(source_labels), 'sourceFactIds': list(owners),
            'sourceSpans': [{'recordId': owners[0], 'sourceField': source_field,
                             'start': left, 'end': right, 'quote': quote, 'role': 'medical_advice'}],
        }
        if explicit:
            fragment['objectSourceSpan'] = {
                'recordId': owners[0], 'sourceField': source_field,
                'start': heading.start(1), 'end': heading.end(1),
                'quote': heading[1], 'role': 'advice_object_heading',
            }
        fragments.append(fragment)
    return fragments


def _source_clinical_actions(data, facts, *, projections=None):
    """Keep projected advice and sealed explicit advice-field witnesses."""
    from .clinical_source_projection import valid_source_span

    projections = projections if projections is not None else getattr(data, '_clinical_patient_projections', {})
    objects = _clinical_advice_objects(data, facts)
    actions = []
    for group in getattr(data, '_template_layout', {}).get('groups', ()):
        if group.get('origin') != 'clinical_group':
            continue
        identifiers = [fid for fid in group.get('originalFactIds', group.get('sourceFactIds', ())) if fid in facts]
        advice, owners, scopes = [], [], []

        def retain(source, start, end, source_ids, source_field):
            labels = [facts[fid].get('name') if isinstance(facts[fid], dict) else facts[fid].name
                      for fid in source_ids]
            findings = '。'.join(str(facts[fid].get('value') if isinstance(facts[fid], dict)
                                     else facts[fid].value) for fid in source_ids)
            for fragment in _scoped_advice_fragments(
                source, start, end, group_code=group['code'], objects=objects,
                owners=source_ids, source_field=source_field, source_labels=labels,
                finding_text=unicodedata.normalize('NFKC', findings).casefold(),
            ):
                # A device page may mix a specific retest with general
                # educational sentences and printed weight-control targets.
                # The latter remain source material, not follow-up tasks.
                fragment['displayScope'] = (
                    'recorded_doctor_instruction' if source_field == 'sourceAdvice'
                    else 'clinical_followup' if _clinical_followup(fragment['quote'])
                    or re.search(r'建议\s*(?:行\s*)?(?:OGTT|糖耐量试验|葡萄糖耐量试验)', fragment['quote'], re.I)
                    else 'source_reference'
                )
                if (fragment['scopeStatus'] != 'unknown_compound_object'
                        and fragment['displayScope'] != 'source_reference'):
                    advice.append(fragment['quote'])
                owners.extend(source_ids)
                if fragment not in scopes:
                    scopes.append(fragment)

        for fid in identifiers:
            for span in projections.get(fid, {}).get('adviceSpans', ()):
                if not isinstance(span, dict) or not valid_source_span(facts[fid], span):
                    continue
                # These are verified spans on this group's own original records.
                # Display summaries can contain different excerpts from the same record.
                source = facts[fid].get('value') if isinstance(facts[fid], dict) else facts[fid].value
                retain(source, span['start'], span['end'], [fid], 'value')
        # Explicit request advice is a distinct source field. Its immutable
        # witness can belong to a measurement owner without its text occurring
        # in the measurement value. Bind that exact field, never a generated
        # sourceAdvice or another record found by page/name similarity.
        witnesses = getattr(data, '_clinical_advice_witnesses_by_code', {}).get(group['code'], ())
        for witness in witnesses:
            witness_ids = sorted(fid for fid, _ in witness.fact_fingerprints if fid in facts)
            if not witness_ids or not set(witness_ids).issubset(identifiers):
                continue
            native_advice = {'factId': witness_ids[0], 'kind': 'clinical',
                             'name': witness.name, 'value': witness.text}
            # This sealed, explicit advice field already has source authority.
            # Keep its full continuous wording and conditions; its grammar
            # does not need to resemble an instruction sentence.
            span = {'recordId': witness_ids[0], 'start': 0, 'end': len(witness.text),
                    'quote': witness.text, 'role': 'medical_advice'}
            if valid_source_span(native_advice, span):
                retain(witness.text, 0, len(witness.text), witness_ids, 'sourceAdvice')
        if scopes:
            unscoped = _unique(item['quote'] for item in scopes
                               if item['scopeStatus'] == 'unknown_compound_object')
            actions.append({
                'code': group['code'], 'name': group['name'], 'focusLabel': group.get('focusLabel'),
                'originalAdvice': _unique(advice), 'sourceFactIds': _unique(owners),
                'sourceAdviceScopes': scopes, 'unscopedAdvice': unscoped,
                'conditionality': {'mode': 'preserve_source_conditions',
                    'authority': 'verified_source_advice', 'directness': 'preserve_source_wording',
                    'objectScope': 'contains_unknown_compound_object' if unscoped else 'verified_source_scopes',
                    'scopeRule': 'explicit_object仅授权原标题对象；source_record_scope仅授权原记录，'
                                 'unknown_compound_object不由动作组名推定病种适用范围。'},
                'noNewPrerequisite': True,
            })
    return actions


def _plan_list(plan, key):
    """A planner list read tolerantly: a missing or malformed field is empty."""
    value = (plan or {}).get(key)
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _period(value):
    """``{text, days}``, a plain phrase or a day count; anything else is unknown."""
    if isinstance(value, dict):
        text, days = value.get('text'), value.get('days')
    elif isinstance(value, str):
        text, days = value, None
    elif isinstance(value, int) and not isinstance(value, bool):
        text, days = None, value
    else:
        return None
    text = str(text).strip() if isinstance(text, (str, int)) and not isinstance(text, bool) else ''
    days = days if isinstance(days, int) and not isinstance(days, bool) and days > 0 else None
    if not text and days:
        text = f'{days} 天内'
    return {'text': text, 'days': days} if text else None


def _plain_strings(value):
    if isinstance(value, (str, dict)):
        value = [value]
    result = []
    for item in value if isinstance(value, (list, tuple)) else ():
        if isinstance(item, dict):
            item = item.get('name') or item.get('test') or item.get('title') or item.get('text')
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
    return _unique(result)


def _plan_fact_ids(row, *fields):
    return _unique(fid for field in fields for fid in (row.get(field) or ()) if isinstance(fid, str))


def _urgent_plan_items(plan):
    return [row for row in _plan_list(plan, 'priorityItems')
            if row.get('priority') in (None, '', 'P0') and str(row.get('title') or row.get('name') or '').strip()]


def _item_title(row, *fields):
    return next((str(row[field]).strip() for field in fields if str(row.get(field) or '').strip()), '')


def _priority_brief(rows, kind):
    """Program-decided items handed to the writer; the cell only phrases them."""
    lines = []
    for row in rows:
        if kind == 'gaps':
            title = _item_title(row, 'test', 'title', 'name')
            details = [f'理由：{row["reason"]}' if str(row.get('reason') or '').strip() else '']
        else:
            title = _item_title(row, 'title', 'name')
            period = _period(row.get('timeframe') if kind == 'urgent' else row.get('interval'))
            checks = _plain_strings(row.get('checks'))
            details = [
                f'{"时限" if kind == "urgent" else "间隔"}：{period["text"]}' if period else '',
                f'科室：{row["department"]}' if str(row.get('department') or '').strip() else '',
                f'需完成：{"、".join(checks)}' if checks else '',
            ]
        details = [part for part in details if part]
        if title:
            lines.append(title + (f'（{"；".join(details)}）' if details else ''))
    if not lines:
        return None
    lead = {
        'urgent': '程序判定的优先排查事项（P0），逐项成句，时限、科室和需完成的检查照用，不增删事项：',
        'routine': '程序判定的常规专科随访事项，逐项写发现、复查项目、间隔和科室，间隔照用：',
        'gaps': '程序判定的检测盲区，逐项写建议补查的项目与理由，不新增项目：',
    }[kind]
    return lead + '；'.join(lines) + '。'


def body_type_elements(clinical):
    """Body-type elements with their own recorded judgement, for a one-sentence verdict."""
    rows = [*clinical.get('bodyMetrics', ()),
            *(row for group in clinical.get('bodyGroups', ()) for row in group.get('rows', ()))]
    parts = []
    for row in rows:
        name = str(row.get('name') or '').strip()
        if not name or not _BODY_ELEMENTS.search(name) or row.get('value') in (None, ''):
            continue
        value = f'{row["value"]}{(" " + row["unit"]) if row.get("unit") else ""}'
        status = str(row.get('templateStatus') or row.get('status') or '').strip()
        parts.append(f'{name} {value}' + (f'（{status}）' if status else ''))
    parts = _unique(parts)
    if not parts:
        return None
    return ('体型判定要素（照用各项实测与原判定，不补阈值）：' + '；'.join(parts[:14]) + '。'
            '先用一句名词性判断概括体型类别、脂肪分布与肌肉量，再说明支撑该判断的关键实测。')


def _planned_source_bindings(clinical, microbiome, facts, plan, *, data=None):
    """Bind aggregate responsibilities to real source domains and reviewed links."""
    if plan.get('status') == 'unavailable':
        return {}

    def valid(ids):
        return _unique(fid for fid in ids if fid in facts)

    def rows_ids(groups, codes=None):
        return _row_fact_ids([row for group in groups if codes is None or group['code'] in codes
                              for row in _panel_rows(group)], facts)

    clusters = plan.get('clusters', ())
    priority_items = _urgent_plan_items(plan)
    routine_items = _plan_list(plan, 'routineFollowups')
    gap_items = _plan_list(plan, 'testGaps')
    clustered_problems = {code for row in clusters for code in row.get('problemCodes', ())}
    urgent_problems = {code for row in clusters if row.get('priority') == 'P0'
                       for code in row.get('problemCodes', ())}
    managed_problems = {code for row in clusters if row.get('priority') in {'P1', 'P2'}
                        for code in row.get('problemCodes', ())}
    cluster_ids = valid(fid for row in clusters for fid in row.get('factIds', ()))
    lifestyle_ids = valid(fid for row in plan.get('lifestyleFactors', ()) for fid in row.get('factIds', ()))
    symptom_ids = valid(fid for row in plan.get('symptoms', ()) for fid in row.get('factIds', ()))
    relationships = []
    for section in ('clusters', 'symptoms', 'recognitions'):
        for row in plan.get(section, ()):
            ids = valid(fid for field in ('factIds', 'clinicalFactIds', 'microbiomeFactIds', 'lifestyleFactIds')
                        for fid in row.get(field, ()))
            relationships.append((set(ids), ids))
    for row in plan.get('lifestyleFactors', ()):
        own = valid(row.get('factIds', ()))
        linked = valid(row.get('contextFactIds', ()))
        relationships.append((set(own), valid([*own, *linked])))

    def related(primary):
        owners = set(primary)
        return valid(fid for anchors, ids in relationships if owners.intersection(anchors)
                     for fid in ids if fid not in owners)

    def binding(primary, context=()):
        primary = valid(primary)
        owners = set(primary)
        return {'factIds': primary, 'contextFactIds': valid(
            fid for fid in [*related(primary), *context] if fid not in owners
        )}

    clinical_rows = [*clinical['bodyMetrics'],
                     *(row for group in clinical['clinicalGroups'] for row in group['rows']),
                     *clinical['specialtyFindings']]
    clinical_ids = _row_fact_ids(clinical_rows, facts)
    clinical_plan_ids = valid(fid for row in clusters for fid in row.get('clinicalFactIds', ()))
    urgent_ids = valid(fid for row in clusters if row.get('priority') == 'P0'
                       for fid in row.get('factIds', ())
                       if fid in facts and facts[fid].get('kind') in {'clinical', 'self_reported'})
    managed_ids = valid(fid for row in clusters if row.get('priority') in {'P1', 'P2'}
                        for fid in row.get('factIds', ()))
    routine_ids = []
    if data is not None:
        advice_by_code = {row['code']: row for row in _source_clinical_actions(data, facts)}
        for group in getattr(data, '_template_layout', {}).get('groups', ()):
            code = group.get('code')
            if group.get('origin') != 'clinical_group' or not (
                code in advice_by_code or group.get('role') in {'health_problem', 'secondary_finding'}
            ):
                continue
            identifiers = valid(fid for fid in group.get('originalFactIds', group.get('sourceFactIds', ()))
                                if fid in facts and facts[fid].get('kind') == 'clinical')
            clinical_plan_ids.extend(identifiers)
            if code in urgent_problems or not clusters and group.get('focusLabel') == '优先查因':
                urgent_ids.extend(identifiers)
            elif code in managed_problems:
                managed_ids.extend(identifiers)
            elif code in advice_by_code and code not in clustered_problems:
                routine_ids.extend(identifiers)
        clinical_plan_ids.extend(fid for row in advice_by_code.values() for fid in row['sourceFactIds'])
    clinical_plan_ids = valid(clinical_plan_ids)
    urgent_ids = valid([*(fid for row in priority_items for fid in _plan_fact_ids(row, 'factIds')), *urgent_ids])
    if 'routineFollowups' in plan:
        # The planner owns the follow-up list once it states one.
        routine_ids = valid(fid for row in routine_items for fid in _plan_fact_ids(row, 'factIds'))
    gap_ids = valid(fid for row in gap_items for fid in _plan_fact_ids(row, 'triggeredBy', 'factIds'))
    core_ids = valid(fid for row in clusters if row.get('priority') == 'P1' for fid in row.get('factIds', ()))
    supportive_ids = valid(fid for row in clusters if row.get('priority') in {'P2', 'P3'}
                           for fid in row.get('factIds', ()))
    urgent_brief = _priority_brief(priority_items, 'urgent') or _priority_brief(
        [{'title': row.get('name')} for row in clusters if row.get('priority') == 'P0'], 'urgent',
    )
    routine_brief = _priority_brief(routine_items, 'routine')
    gap_brief = _priority_brief(gap_items, 'gaps')
    functional_ids = rows_ids(microbiome['functionalGroups'])
    nutrient_ids = rows_ids(microbiome['functionalGroups'], {'aminoAcid', 'vitamin', 'nucleotidePlant'})
    beneficial_ids = rows_ids(microbiome['organismGroups'], {'beneficial'})
    fiber_ids = valid([
        *rows_ids(microbiome['functionalGroups'], {'carbohydrate', 'scfa'}),
        *rows_ids(microbiome['organismGroups'], {'beneficial', 'scfaProducers'}),
        *rows_ids(clinical['digestiveContext'], {'lowerDigestive'}),
    ])
    reported_ids = [fid for fid, fact in facts.items() if fact.get('kind') == 'self_reported']
    active_reported = set([*lifestyle_ids, *symptom_ids])
    background_ids = valid(
        fid for fid in reported_ids if fid not in active_reported or
        facts[fid].get('reportedAssertionKind', facts[fid].get('reported_assertion_kind')) == 'history'
    )
    summary_ids = valid([*cluster_ids, *lifestyle_ids, *clinical_plan_ids])
    if not summary_ids:
        summary_ids = valid([*clinical_ids, *_row_fact_ids(microbiome['structure'], facts),
                             *functional_ids, *rows_ids(microbiome['organismGroups'])]) or valid(reported_ids)
    bindings = {
        'overviewSummary': binding(summary_ids),
        'conclusionSummary': binding(summary_ids),
        'structureSummary': binding(_row_fact_ids([
            *microbiome['structure'],
            *(row for group in microbiome.get('structureGroups', ()) for row in _panel_rows(group)),
        ], facts)),
        'clinicalLead': binding(clinical_ids),
        'bodySummary': binding(_row_fact_ids([
            *clinical['bodyMetrics'], *(row for group in clinical.get('bodyGroups', ()) for row in group['rows']),
        ], facts)),
        'priority.urgent': binding(urgent_ids),
        'priority.routine': binding(routine_ids),
        'priority.gaps': binding(gap_ids),
        'baselineSummary': binding(_row_fact_ids([
            row for group in clinical.get('normalBaselines', ()) for row in group.get('rows', ())
        ], facts)),
        'graphSummary': binding(cluster_ids),
        'clusterSummary': binding(cluster_ids),
        'symptomSummary': binding(symptom_ids),
        'trendSummary': binding([*_row_fact_ids(
            [point for series in clinical['trends'] for point in series['points']], facts),
            *_reported_comparison_ids(facts)]),
        'fermentationSummary': binding(_reported_diet_context(facts)),
        'riskSummary': binding(_model_fact_ids(microbiome, facts)),
        'lifestyleSummary': binding(background_ids),
        'layer.clinical': binding([*urgent_ids, *routine_ids, *gap_ids]),
        'layer.core': binding(core_ids, [*lifestyle_ids, *symptom_ids]),
        'layer.maintenance': binding([*supportive_ids, *(functional_ids or lifestyle_ids)],
                                     [*lifestyle_ids, *symptom_ids]),
        'management.lifestyle': binding(lifestyle_ids, [*clinical_plan_ids, *symptom_ids]),
        'management.fiber': binding(fiber_ids),
        'management.probiotic': binding(beneficial_ids),
        'management.nutrient': binding(nutrient_ids),
    }
    for group in clinical['digestiveContext']:
        bindings['digestive.' + group['code']] = binding(_row_fact_ids(group['rows'], facts))
    # Program-decided content travels with the binding as the cell's context
    # purpose; the writer phrases it and the projection keeps the fields.
    purposes = {
        'bodySummary': body_type_elements(clinical),
        'priority.urgent': urgent_brief,
        'priority.routine': routine_brief,
        'priority.gaps': gap_brief,
        'layer.clinical': '；'.join(filter(None, (urgent_brief, routine_brief, gap_brief))) or None,
    }
    for key, purpose in purposes.items():
        if purpose and key in bindings:
            bindings[key]['contextPurpose'] = purpose
    return bindings


def assessment_template_duty(key):
    """Positive duties of the six-chapter reader slots (wiki/report-assessment-v2.md, section 2)."""
    duties = {
        # Chapter 1
        'overviewSummary': '健康状况总览：先点出最影响后续安排的实测发现及其数值，说明为何需要优先查明，'
                           '再写可协同管理的问题和可作为对照的稳健结果。只选能改变安排的结果。',
        'conclusionSummary': '综合评估结论：先写需优先完成的排查，再写长期管理要改变的方向，'
                             '最后写用哪些可复测的指标判断进展。具体措施由三个层级和干预方向展开。',
        'bodySummary': '1.1 体型判断：先用一句名词性判断概括体型类别、脂肪分布和肌肉量，只使用contextPurpose'
                       '列出的判定要素及各项原判定；再用一两句写出支撑判断的关键实测（身高、体重、体重指数、腰围、'
                       '体脂率、内脏脂肪、骨骼肌量、血压、心率中实际测过的项目）及今后复评的基线。'
                       '实测围度、计算指标与仪器估算分别归属，不同方法的测量不合并成变化。',
        'graphSummary': '1.4 失衡态势总述：用一句话概括各失衡集群的先后与共同管理环节，'
                        '需优先排查的集群写在前面。不罗列指标，不复述每个集群名。',
        'priority.urgent': '1.3 优先排查：按contextPurpose列出的事项逐项成句，每项写明触发的发现及数值、'
                           '需完成的检查、时限和就诊科室。时限、科室和检查照用，不增删事项，不增加先决条件；'
                           '原检查建议保留其条件与力度。',
        'priority.routine': '1.3 常规专科随访：按contextPurpose列出的事项逐项成句，写明需定期复查的发现'
                            '（保留分类、大小等记录要素）、复查项目、间隔和科室。间隔只用原检查建议或列出的内容。',
        'priority.gaps': '1.3 检测盲区：按contextPurpose列出的项目逐项写建议补查的检查及理由：'
                         '由哪项发现触发、补查能回答什么问题。只写列出的项目，不新增检查。',
        # Chapter 2
        'symptomSummary': '2.2 综合印证：把主诉、主要异常和生活因素串成一条有依据的关联链，'
                          '写明哪项发现支持哪条主诉、哪种生活因素与之相关；证据层级或时间关系不同处分别交代。'
                          '不写疗效预期和推销句。',
        # Chapter 3
        'clinicalLead': '第三章概览：区分需优先查明、需定期随访和有实测支持的稳健结果，'
                        '写出改变后续安排的发现及其意义，不按系统重抄指标。',
        'specialtySummary': '3.5 专项检测小结：概括各专项检测共同说明的健康判断和仍需关注的线索，'
                            '保留筛查性质与分项偏离的区别，不复述分项。',
        'trendSummary': '3.6 历年趋势：同一指标有两次及以上可比实测时，写日期、数值和变化方向；'
                        '本人回忆的既往结果按自述单列。旧测值日期不详时只作既往线索，并说明今后复评的基线。',
        'baselineSummary': '3.6 稳健基线：汇总本次在参考区间内的肿瘤标志物与核心器官功能（肝、肾、心、血常规），'
                           '说明可作为今后对照的基线。只写本次实际检测的项目，单次正常不推广为无病。',
        # Chapter 4
        'microbiomeLead': '第四章概览：先写最值得关注的消化道发现和群落整体特点，再概括保留的稳健基础。'
                          '消化系统临床检查与粪便菌群结果分别承担判断与提供线索的职责。',
        'sampleSummary': '第四章章首采样情况：写明采样前抗生素、泻药、益生菌的使用情况（取自问卷），'
                         '未记录时写“采样前用药情况未记录”；采样、收样和报告日期各自对应，不互相替代。'
                         '只写会影响本次对照的采样背景或质控。',
        'fermentationSummary': '第四章章首发酵耐受：结合产甲烷菌等产气相关结果、产气症状和排便性状给出发酵耐受判断。'
                               '只有记录了进食后的具体反应才写耐受情境；没有时给出观察要点，不宣告已有不耐受。',
        'structureSummary': '4.2 微生态稳态小结：说明总分分级与保留的多样性或组成基础如何并存，'
                            '再指出最有意义的结构偏移。'
                            '正常单项不抵消整体异常，也不改写为宿主临床状态。',
        'riskSummary': '第四章风险小结：列出菌群疾病风险模型的名称、分值与阈值分档，'
                       '说明哪些模型与哪项临床发现值得对照，'
                       '区分模型分档、临床实测异常和自述。分值不是个人患病概率。'
                       '明确写出“健康管理以临床检查结果为准”。',
        'digestive.oralGastric': '4.1 口腔与上消化道：分别解释口腔查体、胃部相关检查与消化症状的意义。'
                                 '不同标本和检测方式的同名菌结果不互相定性，呼气试验结果不推断胃酸状态。',
        'digestive.hepatobiliaryPancreatic': '4.1 肝胆胰：用影像和相关临床指标说明主要器官发现、'
                                             '正常对照及各自随访事项。'
                                             '检查只在其测量范围内解释，不从单项指标推断消化或吸收量。',
        'digestive.lowerDigestive': '4.1 下消化道黏膜：先解释实测粪便检查中会改变判断的结果，再写排便症状；'
                                    '日期不明的回顾只在带来额外线索时另段陈述，并照录其发生条件。保留原检查建议。',
        'microbiomeRow': '解释本行检测对象、实际偏移及生理意义，并连接有适用依据的健康关注点。',
        'structure.overallBalance': '4.2 总分与分级：写出总分、所在分级区间及其对群落状态的意义。'
                                    '分数接近分级界线时不外推临床进程。',
        'structure.diversity': '4.2 多样性：解释多样性指数和检出种数分别说明的群落特点，'
                               '以及保留的基础如何与整体失衡并存。',
        'structure.gmwi2': '4.2 健康指数：按报告给出的区间说明当前菌群模式的关联方向，负向指数的意义限定于其比较对象。',
        'structure.gutAge': '4.2 肠道生物学年龄：写出估算肠龄与实际年龄之差及其意义。'
                            '只采用确实用于年龄校准的日期，报告日期只用于指明是哪份结果。',
        'structure.colonizationResistance': '4.2 定植抗力：按本项分子和分母说明相对构成比例及其参考比较，'
                                            '个体感染防护能力须由临床结果判断。',
        'structure.composition': '4.2 菌门比例与肠型：分别比较菌门比例、F/B 比值和肠型，指出实际值得关注的组成差异。',
        'structure.barrier': '4.2 屏障与免疫相关评分：分别解释面板结论、成员结果和相关指数，说明样本显示的生态线索。',
        'organisms.otherOrganisms': '4.3 真菌、病毒与寄生虫：对照靶标面板与构成图谱各自的实际成员和例外，'
                                    '保留能确定的正常对照；属、种及面板级判断分别归属。',
        'organisms.genes': '4.3 毒力与耐药基因：区分已检出与未检出的靶标序列、候选注释和耐药相关丰度，'
                           '说明检测所见及能支持的临床关注范围。序列检出不等于活性毒素表达。',
        # Chapter 5
        'clusterSummary': '5.1 集群概览：先写需优先排查的集群及其理由，再说明其余集群可共用的管理目标与复评指标。',
        'lifestyleSummary': '5.2 先天背景与触发事件：用一段归纳家族史、既往病、用药、手术和烟酒史等背景与触发事件，'
                            '保留主体（本人或亲属）和时间；未记录的方面写“未记录”。背景不等于已证实的诱因。',
        # Chapter 6
        'layer.clinical': '6.1 第一层级（首月）：汇总优先排查事项、常规专科随访和检测盲区，每项写检查、时限和科室，'
                          '相同目的只写一次。按contextPurpose列出的事项写，不新增。',
        'layer.core': '6.1 第二层级（90 天）：针对核心干预集群写干预目标、起步方向和 90 天后复评的指标，'
                      '结合已记录的行为基线和临床约束。',
        'layer.maintenance': '6.1 第三层级（持续）：针对协同调理集群和营养储备写长期维护目标与可持续观察的指标。'
                             '功能预测偏低不作人体缺乏的判断，不开补充方案。',
        'management.probiotic': '6.2 MTPN 功效菌选择依据：依据缺失或偏低的菌说明功效菌选择方向与观察目标。'
                                '只写方向，不写产品名、菌株剂量或疗效。有优先排查事项时写明先完成第一层级排查。',
        'management.fiber': '6.2 MTPN 益生元与膳食纤维：按发酵耐受和排便性状说明益生元与膳食纤维的种类方向、'
                            '渐进方式和耐受观察。只写方向，不写产品名和剂量。',
        'management.nutrient': '6.2 MTPN 营养素配方方向：按营养合成偏低的通路与相关临床指标说明营养素配方方向。'
                               '预测合成偏低不等于人体缺乏；只写方向，不写产品名和剂量。',
        'management.lifestyle': '6.2 LSMI 去诱因生活方式干预：按第五章的诱因维度（饮食与进餐、饮酒与饮品、睡眠、'
                                '运动与久坐、精神压力与口腔护理）写起步动作、递进方式和观察指标。'
                                '只写已记录的诱因，保留临床约束和原检查建议的安排。',
    }
    if key in duties:
        return duties[key]
    prefix = key.partition('.')[0]
    if prefix == 'symptomEvidence' and key.endswith('.microbiome'):
        return ('用一两句交代本次菌群结果对理解该主诉的直接帮助和观察意义。'
                '只引用适用的检测，保留预测与人体实测的区别，不重复表中已有的分数和阈值。')
    return {
        'body': '1.1 体格分组：用本组各项原判定说明最重要的体格特点。不同方法或姿势的测量不改写为变化、'
                '不合并成一个范围，保留能解释结果的测量条件。',
        'system': '1.2 系统定论：用一句定论概括本系统的状态，点名最重的一到两项异常锚点及其数值；'
                  '已检项目均在参考区间内时写明这一点。定论限于本次已检项目，不推广为系统无病。',
        'clinical': '3.1–3.5 分系统小结：按本系统主要异常、独立成立的稳健结果和需核对的矛盾组织判断，'
                    '只把会改变随访或优先级的结果写入小结。',
        'clinicalRow': '第三章表格行解读：写明实测值与临床参考区间，有目标区间时写出目标区间及文献编号，再说明本行'
                       '的临床意义及与相关结果的联系。原始资料文字与数值互相矛盾时，另起一句“数据校准说明”，'
                       '写明以哪项为准及理由。不复述相邻列。',
        'specialtyRow': '3.5 专项检测行：解释本项检测的对象、实际判定及临床意义，只采用本项自身的分级与参考，'
                        '判断限于本项的测量或筛查用途。',
        'digestive': '4.1 消化环节：解释本环节的实际检查与自述，各按时点和来源性质说明其健康与管理意义。',
        'structure': '4.2 结构指标：说明本指标所测对象，以本项分级区间或参照定位本次结果，'
                     '健康联系只写到本指标能支持的范围。',
        'organisms': '4.3 菌群结构：写出异常项的方向、丰度与参考范围及功能意义，正常项给出计数和代表示例；'
                     '生理及健康联系写到所用证据能支持的程度。',
        'function': '4.4 营养合成与代谢潜能：写出偏离项的分值及所在分档区间，说明主要特点；与临床指标交叉印证'
                    '只在有直接依据时写。预测潜能不等于人体浓度或缺乏。',
        'symptom': '2.2 主诉解析：首句用名词性判断概括该主诉的主要线索，再解析相关临床指标、菌群和生活因素'
                   '如何共同解释这一表现，带关键数值和文献编号。主诉发生前的检查只作当时的基线；'
                   '时间关系未确认时分别陈述。',
        'symptomEvidence': '2.1 主诉证据：解释本列检测与该主诉的有据关联，'
                           '同时引用主诉及适用检测，保留各自时间和测量层级。',
        'synthesis': '5.1 健康影响解读：说明临床与影像锚点、微生态与代谢锚点及相关生活因素为何归入同一集群，'
                     '主要判断来自什么、辅助线索和证据限度是什么，以及对健康的影响。不把共现写成已证实的病因。',
        'synthesisAction': '5.1 处理方向：按集群优先级写处理方向。优先排查集群写需完成的检查、时限和科室；'
                           '核心干预和协同调理集群写干预方向与复评指标。原检查建议保留其条件与力度。',
        'lifestyle': '5.2 诱因表行：写行为特征（频次、场景，照问卷原意）、相关的客观异常和关联集群，'
                     '说明这一行为与异常之间有依据的联系，指出可调整的一点。行为不写成确定病因。',
        'recognition': '5.2 认知校准：只在问卷或自述中有可对照的个人认识时写：原有认识、本次结果显示的情况、'
                       '校准后的理解和今后追踪的指标。两份记录相互矛盾时分别写数值和判定，并写明由专科核实。',
    }.get(prefix, '按本格在六章报告中的职责，解释实际发现及有依据的健康意义。')


def assessment_publication_rows(clinical, microbiome):
    """Address actual public interpretation rows without treating old chapters as views."""
    rows = {}

    def collect(prefix, values):
        for row in values:
            if row.get('code') is not None:
                rows[prefix + '.' + str(row['code']) + '.interpretation'] = row

    for field in ('bodyMetrics', 'specialtyFindings'):
        collect('clinical.' + field, clinical.get(field, ()))
    for field in ('clinicalGroups', 'digestiveContext', 'normalBaselines'):
        for group in clinical.get(field, ()):
            collect('clinical.' + field + '.' + group['code'] + '.rows', group.get('rows', ()))
    collect('microbiome.structure', microbiome.get('structure', ()))
    for field in ('structureGroups', 'organismGroups', 'functionalGroups'):
        for group in microbiome.get(field, ()):
            prefix = 'microbiome.' + field + '.' + group['code']
            collect(prefix + '.rows', group.get('rows', ()))
            collect(prefix + '.panelStates', group.get('panelStates', ()))
            baseline = group.get('normalBaseline') or {}
            for collection in ('rows', 'undetectedRows'):
                collect(prefix + '.normalBaseline.' + collection, baseline.get(collection, ()))
    return rows


def _planned_editorial_jobs(clinical, microbiome, facts, plan, *, sample_rows=(), data=None):
    """Author section meanings from a reviewed, source-bound association plan."""
    if plan.get('status') == 'unavailable':
        return
    scopes = {row['code']: row for row in plan.get('cellScopes', ())}
    source_bindings = _planned_source_bindings(clinical, microbiome, facts, plan, data=data)
    emitted = {'overviewSummary', 'conclusionSummary'}
    clusters = plan.get('clusters', ())
    cluster_ids = _unique(fid for row in clusters for fid in row.get('factIds', ()) if fid in facts)
    behavior_ids = [fid for fid, fact in facts.items() if fact.get('kind') == 'self_reported']

    def job(chapter, key, name, purpose, identifiers=(), *, compact=False):
        scope = scopes.get(key, source_bindings.get(key))
        if scope is not None:
            if not any(fid in facts for fid in scope.get('factIds', ())):
                emitted.add(key)
                return
            identifiers = [*scope.get('factIds', ()), *scope.get('contextFactIds', ())]
        identifiers = _unique(fid for fid in identifiers if fid in facts)
        emitted.add(key)
        if identifiers:
            yield chapter, key, name, assessment_template_duty(key), identifiers, compact

    yield from job('healthOverview', 'bodySummary', '基础体格与人体成分判断',
        assessment_template_duty('bodySummary'), _row_fact_ids(clinical['bodyMetrics'], facts), compact=True)
    clinical_rows = [*clinical['bodyMetrics'],
                     *(row for group in clinical['clinicalGroups'] for row in group['rows']),
                     *clinical['specialtyFindings']]
    yield from job('medicalAssessment', 'clinicalLead', '临床检验与专项发现概览',
        '围绕本次实际临床检查写一段有主次的概览：最值得关注的发现、相关系统的共同管理意义，'
        '以及能改变整体判断的稳健基础。保留不同日期、方法和结果性质，'
        '不重复表格明细、章节说明、阅读规则或全部转诊安排。',
        _row_fact_ids(clinical_rows, facts))
    # 1.3 has three program-decided parts. A part without items has no cell;
    # the projection prints the specified sentence (or omits the gaps part).
    for key, name in _PRIORITY_SECTIONS:
        yield from job('healthOverview', 'priority.' + key, name, assessment_template_duty('priority.' + key))
    yield from job('microbiomeAssessment', 'sampleSummary', '样本与检测背景',
        '只依据已绑定的样本元数据说明本次检测背景中有实际解释价值的情况。'
        '采样、收样、报告日期分别按原字段理解，不能相互代替；报告明确的质控只按其结论表述，'
        '不由缺日期、本人回顾或结果异常推断样本质量。原值已在样本信息展示，不重抄字段清单，'
        '没有额外可解释内容可留空，不写通用检测介绍或资料缺失占位。',
        _row_fact_ids(sample_rows, facts), compact=True)
    fermentation_ids = _reported_diet_context(facts)
    if fermentation_ids or 'fermentationSummary' in scopes:
        yield from job('microbiomeAssessment', 'fermentationSummary', '膳食纤维与发酵耐受',
            '围绕本人确实记录的食物、进食后的表现及耐受情境，解释实际发酵/纤维耐受特点与'
            '有帮助的观察重点。没有明确食物后反应时不能诊断不耐受，不能从菌群潜能推定本人症状；'
            '不要写通用适应规则、配方剂量或缺资料占位。', fermentation_ids, compact=True)
    for groups, chapter, prefix, question in (
        (clinical['bodyGroups'], 'healthOverview', 'body.',
         '解释这一组体格测量共同说明什么，保留实际测量情境与管理意义。'),
        (clinical['clinicalGroups'], 'medicalAssessment', 'clinical.',
         '围绕本系统主要问题综合解释，不把检验参考区间与临床筛查界值混为一谈；'
         '有明确联系才使用计划中的背景事实，不把共现写成病因。'),
        (clinical['digestiveContext'], 'microbiomeAssessment', 'digestive.',
         '解释本消化环节的真实记录及其管理意义，分清临床检查与回顾性自述的时间，'
         '不把查体所见改写为未记录的生活习惯。'),
        (microbiome.get('structureGroups', ()), 'microbiomeAssessment', 'structure.',
         '解释本组群落结构及正常基线的实际含义。比值、肠型及群落指标各按检测对象理解，'
         '不转成宿主生理功能或病因的直接测量。'),
        (microbiome.get('organismGroups', ()), 'microbiomeAssessment', 'organisms.',
         '综合本组异常和相关正常基线说明其生理作用及管理价值；丰度零值、组内未见异常、'
         '基因序列检出各按原检测含义表达。保留真实解释，不只复述状态，不推断感染或毒素表达。'),
        (microbiome['functionalGroups'], 'nutritionAssessment', 'function.',
         '综合本组功能潜能谱与计划内确实有关的个人记录，说明最值得关注的意义及可能关联。'
         '每个关联须有适用证据；预测潜能不是血液营养素、体内暴露量或脑内递质浓度。'
         '不能仅凭名称相似、同向或反向升降互证；不复述全表、无关正常项或生活剂量。'),
    ):
        for group in groups:
            rows = _panel_rows(group)
            yield from job(chapter, prefix + group['code'], group['name'], question,
                           _row_fact_ids(rows, facts), compact=prefix in {'body.', 'clinical.', 'structure.'})
            if prefix == 'clinical.':
                for row in group.get('templateRows', ()):
                    yield from job(chapter, 'clinicalRow.' + group['code'] + '.' + row['code'], row['name'],
                        '这是表格本行解读：先说明原始实测的性质，再解释为何值得关注及与实际背景的联系。'
                        '只用同项目明确提供的参考范围判断正常，不补理想值，不扩大阴性体征。'
                        '相关性与确定因果分开；不复述整表或把行动方案挤入本格。',
                        _row_fact_ids(row['rows'], facts), compact=True)
    for code, name, _ in _SYSTEMS:
        key = 'system.' + code
        if key in scopes:
            yield from job('healthOverview', key, name,
                '概括本系统的主要状态、真实优势及关注点，以审核计划绑定的个人事实支持，'
                '不由固定指标字典决定关联，不罗列无关正常结果。', compact=True)
    for row in plan.get('symptoms', ()):
        code, name = row['code'], row['name']
        owners = row.get('factIds', ())
        purpose = row.get('purpose') or ''
        yield from job('medicalAssessment', 'symptom.' + code, name,
            '解释真实主诉、目标或不适的提问语境、时间及观察意义。希望改善不等于已发生症状；'
            '家族成员病史不属于本人诊断。' + purpose, owners, compact=True)
        for kind, title, ids in (
            ('clinical', '临床证据', row.get('clinicalFactIds', ())),
            ('microbiome', '微生态证据', row.get('microbiomeFactIds', ())),
        ):
            if ids:
                yield from job('medicalAssessment', 'symptomEvidence.' + code + '.' + kind, name + title,
                    '单独解释本主诉与本列真实检测的关联。必须同时引用主诉和适用个人检测，'
                    '并以适用的人体或临床证据支持关联；明确相关性、不同测量层级和不同日期。'
                    '不把共现写成个人病因，不为了填格增加无关检查或使用机制假说作确诊。' + purpose,
                    [*owners, *ids], compact=True)
    for row in clusters:
        yield from job('medicalAssessment', 'synthesis.' + row['code'], row['name'],
            '撰写系统失衡集群的真正综合解释：为什么这些个人事实属于同一管理主题、'
            '临床锚点与微生态线索各能说明什么、相互关联的证据层级与个人健康意义。'
            '只纳入能改变判断的事实，不把单个检验项充当综合集群，不把预测潜能当宿主浓度或缺乏。'
            '不复述完整指标表，不虚构症状、暴露、病史或病程。' + (row.get('purpose') or ''),
            row.get('factIds', ()))
        yield from job('comprehensiveConclusion', 'synthesisAction.' + row['code'], row['name'],
            '围绕本主题的真实优先级写连贯行动与复评目的，说明哪些问题可共管、哪些需临床查因。'
            '不要按每个指标重复转诊、警示或生活步骤；生活起步方案集中在6.2。'
            '检测事实不足以指定补充剂、菌株、剂量或疗效时不作此类处方。' + (row.get('purpose') or ''),
            row.get('factIds', ()))
    for row in plan.get('lifestyleFactors', ()):
        yield from job('medicalAssessment', 'lifestyle.' + row['code'], row['name'],
            '按实际问卷或查体记录解释本行为/场景与相应主题的关联、对当前管理的意义。'
            '不把目标当症状，不从频率推数量，不从食物种类推摄入量，不补未记录的生活习惯。'
            '若属于客观口腔等发现，应说明其来源而非改成生活习惯自述。'
            '行动步骤、频次与用量集中在第六章。' + (row.get('purpose') or ''),
            row.get('factIds', ()), compact=True)
    for row in plan.get('recognitions', ()):
        yield from job('medicalAssessment', 'recognition.' + row['code'], row['name'],
            '由审核计划中真实的来源冲突、个人目标或认知差异温和澄清；'
            '说明核实后的正确理解及其管理意义。不得假装引用患者没说过的观点或行为。'
            + (row.get('purpose') or ''), row.get('factIds', ()), compact=True)
    for row in clinical['specialtyFindings']:
        key = 'specialtyRow.' + row['code']
        yield from job('medicalAssessment', key, row['name'], assessment_template_duty(key),
                       _row_fact_ids([row], facts), compact=True)
    all_ids = _unique([*cluster_ids, *behavior_ids])
    for chapter, key, name, purpose, ids, compact in (
        ('medicalAssessment', 'symptomSummary', '综合印证',
         assessment_template_duty('symptomSummary'),
         [fid for row in plan.get('symptoms', ()) for fid in row.get('factIds', ())], False),
        ('medicalAssessment', 'specialtySummary', '专项功能与生理评估',
         '综合实际完成的专项检查，保留筛查性质、分项偏移和总体判定的区别。',
         _row_fact_ids(clinical['specialtyFindings'], facts), False),
        ('medicalAssessment', 'trendSummary', '历年指标演变趋势',
         '只解释来源已确认的不同日期变化，不把同日不同场景或缺日期结果当趋势。',
         _row_fact_ids([point for series in clinical['trends'] for point in series['points']], facts), False),
        ('medicalAssessment', 'baselineSummary', '已确认稳健安全基线',
         assessment_template_duty('baselineSummary'),
         _row_fact_ids([row for group in clinical.get('normalBaselines', ()) for row in group.get('rows', ())],
                       facts), True),
        ('microbiomeAssessment', 'microbiomeLead', '肠道微生态和消化评估概览',
         '概括实际消化道情况与群落构成主线，交代值得关注的组合及保留基础，不重复章节明细。',
         _row_fact_ids([*microbiome['structure'], *(row for group in clinical['digestiveContext']
                                                          for row in group['rows'])], facts), True),
        ('microbiomeAssessment', 'riskSummary', '本章风险小结',
         assessment_template_duty('riskSummary'), _model_fact_ids(microbiome, facts), False),
        ('healthOverview', 'graphSummary', '核心系统失衡态势速览',
         '用简短文字概括已复核集群的主次、共同管理环节和必要边界，不罗列指标或发明固定数量的主题。',
         cluster_ids, True),
        ('medicalAssessment', 'clusterSummary', '系统失衡集群定性概览',
         '统领下文真实主题及其关联：先交代需要优先查因的事项，再解释长期主线和微生态协同。'
         '不把共现写成个人因果，不重复全部集群名或指标。', cluster_ids, False),
        ('medicalAssessment', 'lifestyleSummary', '先天背景与触发事件',
         '只归纳真实且与当前主题相关的本人既往史、家族成员及用药背景，保留原问题角色与时间。'
         '有背景不等于已证明触发原因，不补遗传分型或未记录病史。', behavior_ids, False),
        ('comprehensiveConclusion', 'layer.clinical', _CONCLUSION_LAYERS[0][1],
         '重新撰写临床安全与随访层的综合结论：按优先级概括查因与复评目标、对象和先后，'
         '相同目的只写一次；不得把逐项action复制拼接，不逐个异常追加整段红旗。', all_ids, False),
        ('comprehensiveConclusion', 'layer.core', _CONCLUSION_LAYERS[1][1],
         '综合最值得协同管理的临床、排便或恢复、微生态主题及其对应目标，说明用哪些个人结果复评。'
         '临床指标与群落/功能潜能分层，不承诺逆转、消除毒素或修复感染。', all_ids, False),
        ('comprehensiveConclusion', 'layer.maintenance', _CONCLUSION_LAYERS[2][1],
         '根据真实功能谱、营养摄入与临床背景解释长期营养和生活维护目标。'
         '预测合成潜能低不等于人体缺乏；没有直接缺乏证据也应解释可成立的膳食支持意义。', all_ids, False),
        ('comprehensiveConclusion', 'management.lifestyle', _LSMI_NAME,
         '围绕真实诱因和计划中的主题写连贯、可执行且有主次的起步方案与观察指标，'
         '把饮食、渐进活动、规律恢复及有来源的约束协同起来，不补缺失习惯，不照搬模板剂量。'
         '对需要评估的运动或临床风险在此直接写明，最终投影不会代为删词补句。', all_ids, False),
        ('comprehensiveConclusion', 'management.fiber', _MICROBIOME_ITEMS[1][1],
         '结合真实饮食、排便特点及微生态线索说明食物选择、循序适应和如何观察耐受。'
         '不凭潜能或基因作治病、拦截毒素、修复屏障承诺。', all_ids, False),
        ('comprehensiveConclusion', 'management.probiotic', _MICROBIOME_ITEMS[0][1],
         '解释实际群落异常对微生态支持选择的意义及需要观察的目标。只有适用菌株与用途的人体研究'
         '和个人条件同时支持时才提出相应方向；不能由种水平0值推绝对缺失并直接指定菌株、'
         '配方或抗真菌抗感染作用。仅有粪便相对丰度时，交代该结果对于选菌的实际局限和本人的观察重点。', all_ids, False),
        ('comprehensiveConclusion', 'management.nutrient', _MICROBIOME_ITEMS[2][1],
         '结合实际功能谱、膳食和临床营养线索说明可成立的营养配方方向与必要核实方向。'
         '不把粪便潜能当宿主缺乏，不指定未经证实需要的补充剂、用量或技术配方。', all_ids, False),
    ):
        yield from job(chapter, key, name, purpose, ids, compact=compact)
    for key, scope in scopes.items():
        if key in emitted or key == 'priority.planned':  # 1.3 no longer has a "planned" part.
            continue
        chapter = ('comprehensiveConclusion' if key.startswith(('layer.', 'management.')) else
                   'healthOverview' if key.startswith(('system.', 'body.', 'priority.')) else
                   'nutritionAssessment' if key.startswith('function.') else
                   'microbiomeAssessment' if key.startswith(('digestive.', 'organisms.', 'structure.')) else
                   'medicalAssessment')
        yield from job(chapter, key, scope.get('name') or key,
                       '按审核计划的本格目的解释实际来源，保持观察、关联与临床判断层级。', compact=True)


def _related_problem_sets(data):
    """Propose clinical topics for review, using either a personal link or a common management domain."""
    partitions = []
    for group in getattr(data, '_template_layout', {}).get('groups', ()):
        if (
            group.get('origin') != 'clinical_group'
            or group.get('role') != 'health_problem'
            or group.get('focusLabel') == '优先查因'
            or not group.get('category')
            or not group.get('originalFactIds')
        ):
            continue
        anchors = set(group.get('originalFactIds') or ())
        context = set(group.get('riskContextFactIds') or ())
        shared = next((bucket for bucket in partitions if all(
            member['category'] == group['category']
            and (
                (anchors.intersection(member.get('riskContextFactIds') or ())
                 and context.intersection(member['originalFactIds'])
                 and member.get('certainty') == group.get('certainty'))
                or (member.get('certainty') == group.get('certainty') == 'confirmed'
                    and {member.get('focusLabel'), group.get('focusLabel')} <= _SHARED_MANAGEMENT)
            )
            for member in bucket
        )), None)
        if shared is None:
            partitions.append([group])
        else:
            shared.append(group)
    return [bucket for bucket in partitions if len(bucket) > 1]


def _symptom_observations(item, clinical, microbiome):
    clinical_rows = [
        row for group in clinical['clinicalGroups'] for row in group['rows']
        if row.get('sourceKind') in {'clinical', 'derived'}
    ]
    related = [dict(row, sources=list(row['sources'])) for row in item['clinicalSignals']]
    groups = set()
    for symptom, measurement, panels in _SYMPTOM_OBSERVATIONS:
        if re.search(symptom, item['name']):
            related.extend(dict(row, sources=list(row['sources'])) for row in clinical_rows
                           if re.search(measurement, row['name'], re.I))
            groups.update(panels)
    gut_rows = [
        row for group in [*microbiome['organismGroups'], *microbiome['functionalGroups']]
        if group['code'] in groups
        for row in group['rows']
        if row.get('status') in {'异常', '偏高', '偏低', '较高', '较低', '较弱', '很弱', '较强', '很强', '偏强', '偏弱'}
        and re.fullmatch(r'[+-]?\d+(?:\.\d+)?', str(row.get('value')).strip())
    ]
    return _symptom_signals(item['name'], related)[:8], gut_rows[:12]


def _reported_fermentation_trigger(fact):
    value = str(fact.get('value') or '').strip()
    question, separator, answer = value.partition('；回答：')
    answer = (answer if separator else value).strip()
    if re.match(r'^(?:无|未见|不|没有|否认|否)', answer):
        return False
    return bool(_FERMENTATION_TRIGGER.search(answer) or (
        _FERMENTATION_TRIGGER.search(question) and re.fullmatch(r'是|有|会|偶尔|经常|总是|有时', answer)
    ))


def assessment_editorial_jobs(clinical, microbiome, facts, data):
    """Bind each template paragraph to the records that belong to that section."""
    sample_rows = _sample_metadata_rows(data, facts)
    if (plan := _content_plan(data)) is not None:
        yield from _planned_editorial_jobs(clinical, microbiome, facts, plan, sample_rows=sample_rows, data=data)
        return
    def job(chapter, key, name, question, rows, *, compact=False):
        anchor_ids = _unique(
            source['recordId']
            for row in rows
            for source in row.get('sources', ())
            if source.get('recordId') in facts
        )
        if anchor_ids:
            yield chapter, key, name, question, anchor_ids, compact

    yield from job('medicalAssessment', 'clinicalLead', '临床检验与专项发现概览',
        '围绕实际临床发现作有主次的概览，解释最值得关注的问题、共同管理重点和真正改变判断的稳健基础。'
        '不写下文按系统展示等阅读规则，不重复完整数值表或全部转诊。',
        [*clinical['bodyMetrics'], *(row for group in clinical['clinicalGroups'] for row in group['rows']),
         *clinical['specialtyFindings']])
    yield from job('microbiomeAssessment', 'sampleSummary', '样本与检测背景',
        '仅解释已有来源绑定的样本元数据及原始质控背景。采样、收样、报告日期保持原义，'
        '不从空日期、留样自述或异常结果推断质量；没有额外解释意义可留空，不写通用占位。',
        sample_rows, compact=True)
    yield from job('healthOverview', 'bodySummary', '基础体格与人体成分判断',
        '用两句概括体格特点和管理意义。具体测值和参考区间已在下表。'
        '同日不同场景的结果按各自的用途说明，不写成体格变化。',
        [row for row in clinical['bodyMetrics'] if row.get('sourceKind') in {'clinical', 'derived'}], compact=True)
    for group in clinical['bodyGroups']:
        yield from job('healthOverview', 'body.' + group['code'], group['name'],
            '解释这一行的实测结果共同说明什么。明确各测量情境，只给这行的生理与代谢意义。',
            [row for row in group['rows'] if row.get('sourceKind') in {'clinical', 'derived'}], compact=True)
    trend_rows = [point for series in clinical['trends'] for point in series['points']]
    trend_locations = {source['recordId'] for row in trend_rows for source in row['sources']}
    trend_anchors = [
        row for row in [
            *clinical['bodyMetrics'],
            *(row for group in clinical['clinicalGroups'] for row in group['rows']),
            *clinical['specialtyFindings'],
        ]
        if any(source.get('recordId') in trend_locations for source in row['sources'])
    ]
    comparison_ids = _unique([*_row_fact_ids(trend_anchors, facts), *_reported_comparison_ids(facts)])
    if comparison_ids:
        yield ('medicalAssessment', 'trendSummary', '历次变化解读',
               assessment_template_duty('trendSummary'), comparison_ids, True)
    for group in clinical['clinicalGroups']:
        yield from job('medicalAssessment', 'clinical.' + group['code'], group['name'],
            '用一两句说明本系统的结果合起来最值得关注什么。选择会改变判断的偏离或检查结论，'
            '不重复表中数值、参考范围和逐项正常清单。'
            '参考范围未标注的项目不能合称“在参考范围内”；临床筛查界值与实验室参考分别判断。'
            '设备分项偏离与报告综合判定分别表述。', group['rows'], compact=True)
        for row in group.get('templateRows', ()):
            yield from job('medicalAssessment', 'clinicalRow.' + group['code'] + '.' + row['code'], row['name'],
                '这是表格末列。解释本行检查结果的医学意义，分别使用临床界值和记录参考范围；'
                '先给本行判断，最多用一个必要测值支持。紧扣同一行实测，不自行补理想值。'
                '只有同项目实测带相应参考范围时才可称“在参考范围内”。'
                '阴性体征只按原文明确写出的检查部位、项目和结果陈述；没有记录的体征不进入正文。'
                '例如牙龈无明显红肿只描述红肿，不延伸为牙周袋、牙齿松动、根分叉病变阴性。',
                row['rows'], compact=True)
    yield from job('medicalAssessment', 'specialtySummary', '专项功能医学、遗传易感性与生物年龄评估',
            '写实际完成的专项所见及其作用。筛查概率、设备总体结论和分项偏离分别表述。'
            '无临床对应时不要把设备风险转为个体疾病或治疗建议。',
        clinical['specialtyFindings'])
    for group in clinical['digestiveContext']:
        yield from job('microbiomeAssessment', 'digestive.' + group['code'], group['name'],
            '只写本组检查对该消化道环节的有据判断及其管理意义。'
            '涉及其他器官的独立发现交给对应检查表，口腔、幽门螺杆菌、反流各按自身目的解读。'
            '只以原报告明确记载的结果说明这一环节的实际发现；不写排除疾病的分析过程、未做的检查或用量。'
            '化验日期与未注明日期的回顾性留样自述分开写，不能称为同期或认定两份资料对应同一标本。'
            '牙石或软垢所见不等于个人清洁不足。具体就诊和处置放在第六章。'
            '仅有自述排便症状时，写用户可直接记录的重点；没有新增意义可以留空。',
            group['rows'])
    for key, name, codes in _SYSTEMS:
        rows = _system_rows(key, codes, clinical, microbiome)
        yield from job('healthOverview', 'system.' + key, name,
            '一句话概括本系统主要状态与关注点。只选择真正支持句中判断的少量个人事实，'
            '出处ID会用来生成相邻证据列。常规正常结果不必罗列，其他系统的行动留在对应章节。',
            rows, compact=True)
    for group in microbiome.get('structureGroups', ()):
        if group.get('summary'):
            continue
        yield from job('microbiomeAssessment', 'structure.' + group['code'], group['name'],
            '只写这项分析实际能支持的本样本特征及需要观察的内容。'
            'F/B值、菌门倾向与肠型分别描述，除非原检测明确说明方向一致。'
            '面板总评与具体组成同时出现不能推出某一菌门导致总评异常。'
            '不复述原值、分级定义或通用饮食措施。比值不是人体免疫、屏障功能或定植抗力的直接实测。',
            group['rows'], compact=True)
    for group in microbiome.get('organismGroups', ()):
        rows = _panel_rows(group)
        yield from job('microbiomeAssessment', 'organisms.' + group['code'], group['name'],
            '综合本组异常和正常基线，解释有依据的生理作用和可能的管理价值。只报告已有实测；'
            '保留相对丰度和序列信号的检测含义。组内丰度未见异常不能写成未检出该微生物。'
            '序列检出与临床感染、活性产物是不同结果，不由微生态序列信号单独提出临床病原体检查。',
            rows)
    for group in microbiome['functionalGroups']:
        rows = [*group['rows'], *((group.get('normalBaseline') or {}).get('rows') or ()),
                *group.get('panelStates', ())]
        yield from job('nutritionAssessment', 'function.' + group['code'], group['name'],
            '最后一列用一至两句写本组最重要的有据判断。方向以表内分项为准，正常值不评“储备不足”。'
            '能改变判断时才纳入一项直接相关的临床事实；无此证据只说明本组观察结果的用途。'
            '名称相近的菌群通路和血液化验代表不同测量对象，不能仅按升降称为同向、相反或互相印证。'
            '省去分数清单、机制科普、无关正常值、疾病罗列、关系未定的并列信息及任何生活安排或剂量。',
            rows)
    gut_rows = [*microbiome['structure'], *(row for g in microbiome['functionalGroups'] for row in g['rows'])]
    yield from job('microbiomeAssessment', 'microbiomeLead', '肠道微生态和消化评估概览',
        '用一句话概括此次本人消化道记录和菌群构成中最值得认识的情况。'
        '样本日期和采样信息由其他字段展示，不能由日期、留样情境或缺资料推导标本置信度。',
        [*microbiome['structure'], *(row for group in clinical['digestiveContext'] for row in group['rows'])],
        compact=True)
    if model_ids := _model_fact_ids(microbiome, facts):
        yield ('microbiomeAssessment', 'riskSummary', '菌群疾病关联模型与临床对照',
               assessment_template_duty('riskSummary'), model_ids, True)
    for symptom in clinical['symptoms']:
        objective, gut = _symptom_observations(symptom, clinical, microbiome)
        yield from job('medicalAssessment', 'symptom.' + symptom['code'], symptom['name'],
            '说明本项实际不适的场景，以及对观察或处理最有用的一件事。'
            '只引用确实有关的记录，不从共现现象推断病因，不复述阴性结果或用于排除其他疾病。'
            '有确切时点或同一留样经历时，明确区分临床测量与本人事后回顾。'
            '其他不适与你在本格讨论的不是同一问题时，不要拼成病因。',
            [symptom, *symptom['clinicalSignals']], compact=True)
        if objective:
            yield from job('medicalAssessment', 'symptomEvidence.clinical.' + symptom['code'], symptom['name'],
                '单独审核主诉表临床证据列。用同一人有明确参考价值的检查解释实际主诉或鉴别对照，'
                '指明所用检查并同时绑定主诉和检查事实。没有可靠关联时留空。'
                '无同期凭证时不要描述为同一时点。不要为填格引用无关的系统检查。',
                [symptom, *objective], compact=True)
        if gut:
            yield from job('medicalAssessment', 'symptomEvidence.microbiome.' + symptom['code'], symptom['name'],
                '单独审核主诉表菌群证据列。仅当引用的人体研究支持同一主诉和实际测得的菌或功能时，'
                '绑定同一句的主诉、检测事实与研究引文。用关联线索措辞，避免因果、疾病概率或人体浓度推断。'
                '没有适用研究或本人实测不相符时留空。',
                [symptom, *gut], compact=True)
    yield from job('medicalAssessment', 'symptomSummary', '主诉相关线索小结',
        '仅当已知主诉、生活或检查的实际时间关系能补充整体判断时，用一句话收束。'
        '没有新增可用信息可留空。不要用推测机制连接不同主诉或重复检查项目。',
        clinical['symptoms'], compact=True)
    behavior = [
        {'sources': [{'recordId': fid}]}
        for fid, fact in facts.items() if fact.get('kind') == 'self_reported'
    ]
    clinical_anchors = [{'sources': [{'recordId': fid}]} for fid, fact in facts.items()
                        if fact.get('kind') == 'clinical']
    problem_rows = [*clinical_anchors, *clinical['symptoms']]
    yield from job('healthOverview', 'graphSummary', '核心系统失衡态势速览',
        '用一句话提示现有问题的优先顺序与经核实的共同背景。只谈第五章可支持的实际问题，不复述指标或图中的名称。',
        problem_rows, compact=True)
    yield from job('medicalAssessment', 'clusterSummary', '系统失衡集群概览',
        '统领后文集群，说明当前最重要的共管问题和相互影响。若仅有独立问题，说明其各自的管理重点。'
        '不写流程、数字清单或没有证据的病因。', problem_rows)
    if fermentation_ids := _reported_diet_context(facts):
        yield ('microbiomeAssessment', 'fermentationSummary', '膳食纤维与发酵耐受',
               assessment_template_duty('fermentationSummary'), fermentation_ids, True)
    beverages = [row for row in behavior if any(topic == 'drinks' and re.search(
        pattern, facts[row['sources'][0]['recordId']]['name'],
    ) and not _truncated_drink_answer(facts[row['sources'][0]['recordId']]['value'])
        for topic, _, pattern in _HABIT_QUESTIONS)]
    yield from job('medicalAssessment', 'lifestyle.drinks', '饮水与饮品',
        '只说明实际饮品习惯中与本人某个问题有关的记录及意义。'
        '仅保留有个人依据的关系与观察重点。删去缺失的饮水量、时间或无法评估的项目本身。'
        '饮品种类不能推出频次或饮用时间。合并题“甜食或含糖饮料”无法单独给饮料频次。'
        '饮酒频率和检测前是否饮酒不能确定饮酒量或排除酒精影响。'
        '行动、时点和用量集中放在第六章。',
        beverages, compact=True)
    yield from job('comprehensiveConclusion', 'management.lifestyle', '生活方式管理方向',
        '这是唯一制定生活计划的位置。只围绕本人的实际生活诱因给一个连贯的起步方案和观察指标。'
        '饮水单位或答案截断时只给分次饮水等方向，不还原数值或给定增量。'
        '未经评估的心肺危险信号优先安排安全评估，运动从本人能耐受的活动开始。',
        behavior)
    yield from job('medicalAssessment', 'lifestyleSummary', '先天背景与既往触发事件复盘',
        '只归纳与当前问题直接相关且已有依据的家族史、既往事件或用药。'
        '只写存在的相关背景。不逐项写没有疾病、用药或既往诱因；多选答案分别呈现，不推导各天变化。',
        behavior, compact=True)
    awareness = [row for row in behavior if re.search(
        r'您认为|你认为|您是否认为|您觉得|您如何看待|自评健康|体检印象|回忆上次体检',
        facts[row['sources'][0]['recordId']]['name'],
    )]
    yield from job('medicalAssessment', 'recognition.awareness', '检查结果与个人认知',
        '有明确的本人观点与本次检查结果差异时，只指出这一差异。没有差异则留空。',
        awareness, compact=True)
    existing_management = [row for row in behavior if re.search(
        r'目前.*(?:管理|改善|调整|尝试|补充)|已经.*(?:采取|尝试|运动|饮食)|既往.*(?:管理|干预|补充)',
        facts[row['sources'][0]['recordId']]['name'],
    )]
    yield from job('medicalAssessment', 'recognition.management', '已有健康管理做法',
        '仅根据实际自述指出一项已在执行且值得保持或需要调整的做法，并说明原因。'
        '个人目标、空答和否定回答不能写成已采取的措施。',
        existing_management, compact=True)
    for level, name, purpose in (
        ('gaps', '关键常规检测盲区提示', '只写确实缺失且改变已知问题判断的一项资料。'
         '已有明确的查因或随访安排在第六章列出，这里不再生成同一检查。无独立补充事项可不写。'),
    ):
        yield from job('healthOverview', 'priority.' + level, name, purpose, clinical_anchors)
    for (level, name), purpose in zip(_CONCLUSION_LAYERS[1:], (
        '用两句说明哪些已成立的问题可同步管理，以及用什么个人结果判断成效。执行频次和用量留在下一节。',
        '只给一个可长期维持的生活或营养目标及观察依据。'
        '不复述菌群分数、补充剂限制、普通正常检验或前面的检查方案；不单独制定行动频次和用量。',
    ), strict=True):
        yield from job('comprehensiveConclusion', 'layer.' + level, name, purpose,
            [*clinical_anchors, *gut_rows, *behavior] if level != 'clinical' else clinical_anchors)
    if _has_microbiome_findings(microbiome):
        yield from job('comprehensiveConclusion', 'management.fiber', '益生元与膳食纤维',
            '有明确排便特点、饮食或耐受资料支撑时，写一条食物选择和如何观察耐受。'
            '不要写处方、补充剂或禁止采购的限制。这个子项不另定目标克数；所有行动量由生活方式安排统一。',
            [*gut_rows, *behavior], compact=True)
        probiotic_use = [row for row in behavior if re.search(
            r'益生菌|菌剂|活菌|发酵食品', facts[row['sources'][0]['recordId']]['name'],
        )]
        yield from job('comprehensiveConclusion', 'management.probiotic', '功效菌选择与观察依据',
            '仅在已记录的使用情况及适用的人群研究支持同一目的时，说明是否需要评估现有用法及观察对象。'
            '不从菌群丰度指定菌株、品牌、剂量或配方。不具备依据时留空。',
            [*probiotic_use, *clinical['symptoms']] if probiotic_use else [], compact=True)
        nutrient_context = [row for row in clinical_anchors if re.search(
            r'维生素|叶酸|铁蛋白|转铁蛋白|营养|白蛋白',
            facts[row['sources'][0]['recordId']]['name'],
        ) and facts[row['sources'][0]['recordId']].get('abnormal') is True]
        yield from job('comprehensiveConclusion', 'management.nutrient', '个体营养评估与膳食支持',
            '仅当已有相关临床营养化验、已记录的饮食或明确医嘱支持时，说明具体营养观察及膳食方向。'
            '不从预测通路推断人体缺乏，不制定补充剂剂量、处方或商品配方。',
            nutrient_context, compact=True)
    ranked_groups = sorted(
        (group for group in getattr(data, '_template_layout', {}).get('groups', ())
         if group.get('origin') == 'clinical_group'
         and group.get('role') == 'health_problem'
         and group.get('focusLabel') not in {None, '结合症状随访'}),
        key=lambda group: _PRIORITIES.get(group.get('focusLabel'), 'P3'),
    )
    for group in ranked_groups[:6]:
        anchored = list(group.get('originalFactIds') or ())
        linked = [fid for fid in group.get('riskContextFactIds', ())
                  if fid in facts and facts[fid]['kind'] in {'microbe', 'function'}]
        group_rows = [{'sources': [{'recordId': fid}]} for fid in [*anchored, *linked]]
        yield from job('medicalAssessment', 'impact.' + group['code'], group['name'],
            '写第五章健康影响解读。说明实际发现的性质、重要程度及会改变处理的个人因素。'
            '用一两条关键发现支持判断。症状未问及或检查没有记录的情况，不写成明确没有。'
            '只在同人证据和适用研究支持下写相关的菌群及功能，省去无关发现和否定关联的话。'
            '已选希望改善不等于日常功能受损；影像未分型时不套用单纯性病变结论和大小阈值。'
            '个人事实只选能改变判断或优先级的实际指标；保留多选原意，不推出频率、变化或因果。'
            '问卷明确记录同一结果既往也出现时，写为既往同类发现。没有明确日期对应的回顾与化验分开。'
            '症状只作留样时的自述情境，不归因为筛查异常来源，'
            '不能据此降低需要查明的其他病因。检查总体结论和分项偏离同时存在时分开写。'
            '不复述整表，实施措施、用量与时限统一写在第六章。',
            group_rows)
    for bucket in _related_problem_sets(data):
        name = '、'.join(group['name'] for group in bucket)
        anchors = [{'sources': [{'recordId': fid}]} for group in bucket for fid in group['originalFactIds']]
        key = 'cluster.' + bucket[0]['code']
        yield from job('medicalAssessment', key, name,
            '逐项引用每个问题的本人原始事实，再说明共同影响及可共用的具体管理决策。没有实际共同环节就留空。'
            '相同分类、一般生活建议或菌群模型信号不足以合并。保留不同问题的结论性质，不造合并病名。',
            anchors)
        yield from job('comprehensiveConclusion', 'management.' + key, name,
            '给出确实同时适用于所有列出问题的具体行动及复评结果，引用适用于本人的共管证据，并保留各自的随访。'
            '没有共同决策时不写任何解释。共同行动只写临床复评，日常饮食和运动步骤由6.2承担。',
            anchors)


def _source_ids(row):
    return _unique(ref.record_id for ref in row.sources if ref.record_id)


def _has_microbiome_findings(view):
    return any(view[key] for key in ('structure', 'organismGroups', 'functionalGroups'))


def _signal(fact, *, issue=None):
    from .clinical_report import _problem_source_quote
    from .clinical_report import _source_lesion_measurement_quotes
    from .clinical_report import reported_fact_kind
    from .clinical_report import reported_symptom_fragments

    source_code = 'A' if fact.kind == 'clinical' else 'B' if fact.kind == 'self_reported' else 'C'
    value = fact.value
    observed_at = fact.date
    if fact.kind == 'self_reported':
        if value.startswith('问题：'):
            value = value.split('；回答：', 1)[-1]
        if reported_fact_kind(fact) == 'symptom':
            fragments = reported_symptom_fragments(fact.value, fact.name)
            if fragments:
                value = '、'.join(answer for _, answer in fragments)
        observed_at = None
    elif issue and fact.imaging:
        finding = _problem_source_quote(value, issue)
        if finding != value:
            fragments = [*_source_lesion_measurement_quotes(value, issue), finding]
            value = '；'.join(dict.fromkeys(part.strip('；;。 \n') for part in fragments if part.strip('；;。 \n')))
    return {
        'name': fact.name,
        'value': value,
        'unit': fact.unit,
        'referenceRange': fact.reference_range,
        'status': ('问卷自述病史' if reported_fact_kind(fact) == 'history' else '自述症状')
        if fact.kind == 'self_reported' else fact.status,
        'observedAt': observed_at,
        'sources': [{'sourceCode': source_code, 'recordId': fact.fact_id, 'observedAt': observed_at}],
        'evidenceRefs': [],
    }


def _reviewed_facts(blueprint, accepted):
    paths = {target['id']: tuple(target['path']) for chapter in blueprint.chapters for target in chapter['targets']}
    return {paths[block['targetId']]: set(block.get('factIds', ())) for block in accepted if block['targetId'] in paths}


def _questionnaire_habits(facts):
    """Keep the subject of a questionnaire answer separate from its value."""
    seen = set()
    records = []
    for fact in facts.values():
        if fact.kind != 'self_reported':
            continue
        question, marker, answer = fact.value.partition('；回答：')
        question = question.removeprefix('问题：') if marker else fact.name
        answer = _lifestyle_value(answer if marker else fact.value)
        if not answer or answer in {'未提供', '不详', '未填写'}:
            continue
        for topic, label, pattern in _HABIT_QUESTIONS:
            if not re.search(pattern, question):
                continue
            if label == '每日饮水' and _truncated_drink_answer(answer):
                # The qualifier remains missing; do not manufacture a fluid amount.
                break
            pair = label, answer
            if pair not in seen:
                records.append((topic, label, answer, fact.fact_id))
                seen.add(pair)
            break
    drinking = [answer for _, label, answer, _ in records if label == '饮酒']
    if drinking and all(re.fullmatch(r'(?:从不|不|没有)(?:饮酒|喝酒)?|无|否', answer) for answer in drinking):
        records = [row for row in records if row[1] != '检查前饮酒']
    return records


def _truncated_drink_answer(value):
    return bool(re.search(r'\d\s*(?:毫|m)$', str(value).strip(), re.I))


def _distinct_signals(signals):
    """Merge duplicate observations while retaining independently supplied paths."""
    from .assessment_microbiome_view import first_reported_abundance_assay

    selected = {}
    for row in signals:
        label = str(row['name']).split(' / ', 1)[0]
        assay = first_reported_abundance_assay(row)
        identity = (label, re.sub(r'\s+', '', str(row['value'])), row['unit'], row['observedAt'],
                    assay['id'] if assay else None)
        if identity not in selected:
            selected[identity] = row
        else:
            slot = selected[identity]['sources']
            slot.extend(source for source in row['sources'] if source not in slot)
    return list(selected.values())


def _microbiome_evidence(identifiers, facts, text, refs):
    """Keep a detected result as personal evidence only where the prose uses it."""
    if not text or not refs:
        return []
    passages = re.split(r'[。！？；\n]', text)
    signals = []
    for fid, fact in facts.items():
        if fid not in identifiers or fact.kind not in {'microbe', 'function'}:
            continue
        name = re.split(r' / |[（(]', fact.name, maxsplit=1)[0]
        if len(name) > 1 and any(name in part and not re.search(
            r'不(?:相关|用于|支持|构成|能解释)|无关|无法关联|未建立|未确立|尚需研究', part,
        ) for part in passages):
            signals.append(_signal(fact))
    return _distinct_signals(signals)


def _original_clinical_plan(label, facts, document, source_ids):
    """Select an explicit recommendation bounded to this finding and its source."""
    from .evaluation_builder import _key
    from .evaluation_builder import original_medical_advice_witnesses

    advice = []
    for witness in original_medical_advice_witnesses(document, source_ids):
        owners = sorted(fid for fid, _ in witness.fact_fingerprints if fid in facts)
        if not owners:
            continue
        finding_text = unicodedata.normalize('NFKC', '。'.join(facts[fid].value for fid in owners)).casefold()
        fragments = _scoped_advice_fragments(
            witness.text, 0, len(witness.text), group_code=label, owners=owners,
            objects=[{'code': label, 'recordIds': set(owners), 'labels': {_advice_heading_key(label)}}],
            source_field='sourceAdvice', source_labels=[witness.name], finding_text=finding_text,
        )
        for fragment in fragments:
            if (fragment['scopeStatus'] == 'explicit_object'
                    or fragment['scopeStatus'] == 'source_record_scope' and _key(witness.name) == _key(label)):
                advice.append(fragment['quote'])
    if not advice:
        owner = re.compile(
            rf'(?:^|[。！？；;\n])\s*{re.escape(label)}\s*[：:]\s*'
            r'((?:(?![；;]\s*[^。！？；;\n]{1,25}[：:])[^。！？\n])+)',
        )
        for fact in facts.values():
            if fact.kind != 'clinical' or not re.search(r'主检|总检|体检结论|医嘱|医生建议', fact.name):
                continue
            advice.extend(match[1].strip('；; ') for match in owner.finditer(fact.value)
                          if _clinical_followup(match[1]))
    unique = _unique(part.rstrip('。') + '。' for part in advice if part.strip())
    return unique[0] if len(unique) == 1 else None


def _clinical_plan_details(text):
    """Independent parts of the doctor's plan that a review must preserve."""
    text = re.sub(r'前列腺特异性?抗原', 'PSA', text or '', flags=re.I).replace('B超', '超声')
    text = text.replace('年度', '每年').replace('指检', '指诊')
    return set(re.findall(
        r'[\u4e00-\u9fff]{2,10}科(?=(?:门诊)?(?:咨询|就诊|评估|随诊|随访))'
        r'|每(?:\d+|[一二三四五六七八九十]+)?(?:个)?(?:天|周|月|年)|'
        r'(?:\d+|[一二三四五六七八九十]+)(?:个)?(?:天|周|月|年)(?:内|后)'
        r'|超声|PSA|指诊|验光|洁治|尿常规|血脂|血常规|胸部CT|心电图|甲状腺功能|甲状腺抗体',
        text,
        flags=re.I,
    ))


def _clinical_action(name, facts, reviewed_action, *, document=None, source_ids=(), other_reviewed=None):
    """Publish a plan without losing the original specialist, test, or interval."""
    from .evaluation_presentation import reader_text

    label = re.sub(r'[（(](?:待复核|待核实|用户自述)[）)]$', '', name)
    original = _original_clinical_plan(label, facts, document, source_ids)
    if original:
        details = _clinical_plan_details(original)
        reviewed_action = next((action for action in (reviewed_action, other_reviewed)
                                if action and details and details <= _clinical_plan_details(action)), original)
    if not reviewed_action:
        return None
    if re.search(r'肾.{0,10}囊(?:肿|性)', label):
        clauses = re.split(r'(?<=[；;。！!？?\n])\s*', reviewed_action)
        reviewed_action = ''.join(clause for clause in clauses if not (
            re.search(r'(?:[>＞≥]|超过|大于|达到)\s*\d+(?:\.\d+)?(?:\s*[-—–至~～]\s*\d+(?:\.\d+)?)?'
                      r'\s*(?:cm|mm|厘米|毫米)', clause, re.I)
            and re.search(r'治疗|手术|穿刺|引流|切除|干预', clause)
        )).strip()
        if reviewed_action.endswith(('；', ';')):
            reviewed_action = reviewed_action.rstrip('；;') + '。'
    return reader_text(reviewed_action, prose=True)


def _clinical_finding_anchor(fact):
    """Explicitly normal results stay in their examination context."""
    from .evaluation_builder import _normal_imaging_result

    return not (_normal_examination(fact.name, fact.status, fact.value) or _normal_imaging_result(fact))


def _is_reported_imaging(fact):
    return fact.imaging or bool(re.search(
        r'超声|彩超|声像|影像|X线|\b(?:MRI|CT|TI[- ]?RADS|BI[- ]?RADS)\b',
        f'{fact.name or ""} {fact.value or ""}', re.I,
    ))


def _normal_examination(name, status, value):
    from .evaluation_builder import _status_state

    if _status_state(status) == 'normal' or status == '参考范围内':
        return True
    if not re.search(r'正位片|侧位片|影像|超声|彩超|X线|CT|MRI', name or '', re.I):
        return False
    conclusion = re.split(r'(?:诊断|结论)[：:]', value or '')[-1].strip()
    return bool(re.fullmatch(
        rf'(?:{re.escape(name)})?未见(?:明显)?异常(?:X线(?:征象|改变)|影像(?:表现|改变))?[。.]?',
        conclusion, re.I,
    ))


def _merge_reviewed_clusters(data, candidates, facts, reviewed):
    for groups in _related_problem_sets(data):
        codes = [group['code'] for group in groups]
        if not all(code in candidates for code in codes):
            continue
        key = 'cluster.' + codes[0]
        summary = data._assessment_editorial.get(key)
        action = data._assessment_editorial.get('management.' + key)
        if not (summary and summary.text and summary.evidence_refs and action and action.text and action.evidence_refs):
            continue
        if re.search(r'(?:未形成|没有|无)(?:明确|同时适用|相同|适用|具体|同时适用于所有[^。；]{0,12})*'
                     r'(?:的)?共同(?:干预|管理|处理|决策|行动)', action.text):
            continue
        members = [candidates[code] for code in codes]
        primary = set().union(*(ids for _, ids in members))
        cross = reviewed.get(('_assessment_editorial', key, 'text'), set())
        if not all((ids - set().union(*(other for _, other in members if other is not ids))) & cross
                   for _, ids in members):
            continue
        joint = {
            '_codes': [code for item, _ in members for code in item['_codes']],
            '_actionRefs': list(action.evidence_refs),
            'name': '、'.join(item['name'] for item, _ in members),
            'priority': min(item['priority'] for item, _ in members),
            'interpretation': summary.text,
            'action': action.text,
            'clinicalSignals': _distinct_signals(signal for item, _ in members for signal in item['clinicalSignals']),
            'microbiomeSignals': _microbiome_evidence(cross, facts, summary.text, summary.evidence_refs),
            'evidenceRefs': _unique([*summary.evidence_refs, *action.evidence_refs]),
        }
        candidates[codes[0]] = (joint, primary)
        for code in codes[1:]:
            candidates.pop(code)


def _followup_procedure(action):
    """Identify a named planned examination, excluding its purpose and symptoms."""
    if not action:
        return None
    plan = re.split(r'[。！？\n]', action, maxsplit=1)[0]
    if re.search(r'无需|暂不|不建议|若|如果', plan):
        return None
    if not (match := re.search(
        r'(?:建议(?:行)?|结合|安排|完成|完善|进行|做|行)\s*((?:\d+(?:\.\d+)?\s*[mg]{1,2}\s*)?'
        r'[\u4e00-\u9fff]{2,22}?(?:耐量试验|功能试验|抑制试验|激发试验|负荷试验|内镜检查|超声复查))'
        r'(?=[，,；;。：:、\s]|评估|判读|判断|明确|核实|确定|以|$)', plan,
    )):
        return None
    visits = re.findall(r'(?:由|到|至|前往)([\u4e00-\u9fff]{2,12}科)', plan)
    return re.sub(r'\s+', '', match[1]), tuple(visits)


def _clinical_followup(action):
    """Include an actual clinical follow-up, including one with a stated trigger."""
    return any(
        re.search(
            r'复查|复诊|随诊|随访|就诊|门诊|科(?:评估|咨询)|就医|进一步检查|'
            r'(?:尿|血压)监测|定期检查|验光|(?:行|做|进行)洁治|'
            r'(?:定期|每年|年度|每半年)[^。；;]{0,18}(?:超声|B超|PSA|指检|指诊|检查)',
            clause,
        )
        and not re.search(r'无需|不必|不建议|暂不|不用|无需单独', clause)
        for clause in re.split(r'[。；;，,\n]', action or '')
    )


def _conditional_action(action):
    return re.split(
        r'(?:^|[，,；;。！？\n])\s*(?:若|如果|如有|一旦|除非|复查仍)'
        r'|(?:出现|持续|进一步加重)[^。；;]{0,32}?(?:时|则|者)',
        action or '', maxsplit=1,
    )[0]


def _active_clinical_followup(action):
    """Recognize a current clinical action without using hypothetical warnings."""
    return any(_clinical_followup(_conditional_action(sentence))
               for sentence in re.split(r'[。；;！？\n]', action or ''))


def _action_constraints(action):
    """Keep source-backed conditions and review windows when coalescing actions."""
    return {
        re.sub(r'\s+', '', clause).strip()
        for clause in re.split(r'[。；;，,！？\n]', action)
        if re.search(
            r'若|如果|如有|一旦|除非|复(?:查|测)仍|出现[^。；;]{0,32}(?:时|则)|'
            r'仅在|待[^。；;]{0,18}后|经[^。；;]{0,18}后|'
            r'无需|不必|不建议|暂不|不宜|不应|处方|用药|'
            r'(?:\d+|[一二三四五六七八九十半])\s*(?:天|周|个?月|年)(?:内|后|一次)|'
            r'每(?:一|\d+)?(?:周|个?月|年)(?:复查|复测|随访|一次)',
            clause,
        )
    }


def _clinical_action_owners(data):
    """Identify a finding's original witnesses, before review adds related records."""
    from .evaluation_schema import clinical_problem_projection_codes

    codes = clinical_problem_projection_codes(data)
    owners = {}
    for frame in data._clinical_problem_projection:
        if code := codes.get(frame.problem_id):
            witnesses = [source.fact_id for source in frame.sources
                         if source.source_code == 'A' and source.role == 'problem_witness']
            if witnesses:
                owners.setdefault(code, set()).update(witnesses)
    for group in getattr(data, '_template_layout', {}).get('groups', ()):
        if group.get('originalFactIds') and group.get('role') not in {'normal_screening', 'management_goal'}:
            owners[group['code']] = set(group['originalFactIds'])
    return owners


def _merge_single_measurement_actions(clusters, data, facts, subject, document):
    """One measurement and its verified single-input derivation have one action."""
    from .evaluation_builder import original_medical_advice_witnesses
    from .evaluation_derived_indices import _number
    from .evaluation_derived_indices import derived_index_facts
    from .evaluation_derived_indices import is_derived_index

    owners = _clinical_action_owners(data)

    singles = {}
    for item, anchors in clusters:
        if len(item['_codes']) != 1:
            continue
        bound = owners.get(item['_codes'][0], ())
        if len(bound) == 1 and (fact := facts.get(next(iter(bound)))) and fact.kind == 'clinical':
            singles.setdefault(fact.fact_id, []).append((item, anchors))

    removed = set()
    for fid, rows in singles.items():
        original = facts[fid]
        if (len(rows) != 1 or not rows[0][0]['action'] or not original.date
                or is_derived_index(original.source)):
            continue
        primary, anchors = rows[0]
        for derived in derived_index_facts((original,), subject):
            matches = singles.get(derived.fact_id, ())
            if len(matches) != 1:
                continue
            duplicate, duplicate_anchors = matches[0]
            measured = facts[derived.fact_id]
            if (not is_derived_index(measured.source) or measured.abnormal != original.abnormal
                    or derived.date != measured.date or derived.unit != measured.unit
                    or derived.value != measured.value or _number(original.value) != _number(measured.value)
                    or not duplicate['action'] or original_medical_advice_witnesses(document, (derived.fact_id,))):
                continue
            if (primary['priority'] != duplicate['priority']
                    or _followup_procedure(primary['action']) != _followup_procedure(duplicate['action'])
                    or _action_constraints(primary['action']) != _action_constraints(duplicate['action'])):
                continue
            primary['_codes'].extend(duplicate['_codes'])
            for key in ('clinicalSignals', 'microbiomeSignals'):
                primary[key] = _distinct_signals([*primary[key], *duplicate[key]])
            primary['evidenceRefs'] = _unique([*primary['evidenceRefs'], *duplicate['evidenceRefs']])
            anchors.update(duplicate_anchors)
            removed.add(id(duplicate))
    return [entry for entry in clusters if id(entry[0]) not in removed]


def _consolidate_assessments(candidates, groups):
    """Combine independently reviewed pointers to the same diagnostic workup."""
    registered = {}
    for code in list(candidates):
        item, anchors = candidates[code]
        group = groups.get(code) or {}
        signature = _followup_procedure(item['action'])
        if not (signature and group.get('category') and group.get('focusLabel') == '近期复评'
                and item['priority'] != 'P0' and item['interpretation']):
            continue
        key = group['category'], signature[0]
        if key not in registered:
            registered[key] = code, signature[1]
            continue
        main_code, main_specialties = registered[key]
        if main_specialties and signature[1] and not set(main_specialties).intersection(signature[1]):
            continue
        main, main_anchors = candidates[main_code]
        main['_codes'].extend(item['_codes'])
        main['name'] += '、' + item['name']
        main['interpretation'] = '\n\n'.join(_unique((main['interpretation'], item['interpretation'])))
        for field in ('clinicalSignals', 'microbiomeSignals'):
            main[field] = _distinct_signals([*main[field], *item[field]])
        main['evidenceRefs'] = _unique([*main['evidenceRefs'], *item['evidenceRefs']])
        main['_actionRefs'] = _unique([*main['_actionRefs'], *item['_actionRefs']])
        main_anchors.update(anchors)
        candidates.pop(code)


def _shared_collection_actions(clusters, facts):
    """Include an accepted collection precaution once in the actual retest plan."""
    repeat = r'复[查测]尿常规|尿常规复[查测]'

    def observation_key(item):
        owners = [facts[ref['recordId']] for row in item['clinicalSignals'] for ref in row['sources']
                  if ref['sourceCode'] == 'A' and ref['recordId'] in facts]
        keys = {(fact.source.split('；', 1)[0], fact.date) for fact in owners}
        if len(keys) == 1:
            source, date = next(iter(keys))
            if source and date and source != '临床检查记录':
                return source, date
        return None

    def interference(item):
        return bool(item['clinicalSignals']) and all(
            re.fullmatch(r'尿(?:维生素\s*C|抗坏血酸)(?:[（(][^（）()]+[）)])?', row['name'], re.I)
            for row in item['clinicalSignals']
        )

    def interference_preparation(text):
        return re.search(
            r'(?:在)?(?:采样|留样|取样|复查|(?:留取|采集)(?:尿液|尿样|(?:尿液|尿)?标本))前'
            r'[^。；;，,]{0,8}(?:避免|减少)'
            r'[^。；;，,]{0,32}(?:维生素\s*C|抗坏血酸)[^。；;，,]*'
            r'(?:[，,]\s*(?:以)?减少[^。；;，,]{0,12}干扰)?',
            text or '', re.I,
        )

    def repeats_collection_only(text):
        clauses = [part.strip() for part in re.split(r'[。；;，,\n]', text) if part.strip()]
        routine = (
            r'(?:该项)?(?:无需|无须)(?:(?:为|针对)该项)?(?:单独|特殊)?处理'
            rf'|(?:(?:按(?:常规)?体检(?:节奏|安排|计划)?|在常规体检时|随常规体检)\s*)?(?:{repeat})'
            r'|(?:建议)?(?:留取|采集|取)(?:清洁)?(?:中段晨尿|晨尿中段|中段尿)(?:标本)?'
            r'|(?:以)?(?:减少|避免)(?:标本|样本)污染(?:对结果的影响)?'
            r'|(?:保持|维持)(?:日常|原有|平时)?(?:饮食(?:与|及|和)饮水|饮食|饮水)(?:习惯)?'
        )
        return bool(clauses and re.search(repeat, text) and all(re.fullmatch(routine, clause) for clause in clauses))

    def plan_details(text):
        # The same conditional referral can be written with different labels.
        specialties = set(re.findall(r'肾内科|泌尿外科', text))
        other = {detail for detail in _clinical_plan_details(text) if not re.search(r'肾内科|泌尿外科', detail)}
        return specialties | other

    removed = set()
    for duplicate, duplicate_ids in clusters:
        key = observation_key(duplicate)
        repeated = duplicate['action'] or ''
        preparation = interference_preparation(repeated)
        routine = repeated if not preparation else repeated[:preparation.start()] + repeated[preparation.end():]
        repeat_only = repeats_collection_only(routine)
        if not (key and duplicate['priority'] == 'P3' and interference(duplicate)
                and re.search(repeat, repeated) and (preparation or repeat_only)):
            continue
        contingent = re.search(r'(?:若|如需|如果|一并)[^。；;]{0,14}(?:复查|尿常规)', repeated)
        match = [(primary, primary_ids) for primary, primary_ids in clusters if (
            primary is not duplicate and not interference(primary) and observation_key(primary) == key
            and all(re.match(r'^尿', row['name']) for row in primary['clinicalSignals'])
            and re.search(r'清洁中段(?:晨)?尿', _conditional_action(primary['action']))
            and re.search(repeat, _conditional_action(primary['action']))
            and (contingent or (repeat_only and primary['priority'] != 'P3')
                 or ('中段' in repeated and interference_preparation(primary['action'])))
            and ('晨尿' not in repeated or '晨尿' in primary['action'])
            and plan_details(repeated) <= plan_details(primary['action'])
        )]
        if len(match) != 1:
            continue
        primary, primary_ids = match[0]
        if preparation and not interference_preparation(primary['action']):
            current = primary['action'].strip()
            break_at = re.search(r'[。；;]', current)
            if break_at:
                head, remaining = current[:break_at.start()], current[break_at.end():]
                primary['action'] = f'{head}。{preparation[0]}。{remaining.lstrip()}'
            else:
                primary['action'] = f'{current}。{preparation[0]}。'
            primary['_actionRefs'] = _unique([*primary['_actionRefs'], *duplicate['_actionRefs']])
        primary['_codes'] = _unique([*primary['_codes'], *duplicate['_codes']])
        primary['clinicalSignals'] = _distinct_signals([*primary['clinicalSignals'], *duplicate['clinicalSignals']])
        primary_ids.update(duplicate_ids)
        removed.add(id(duplicate))
    return [entry for entry in clusters if id(entry[0]) not in removed]


def _urine_leukocyte_finding(name):
    return bool(re.fullmatch(r'尿白细胞(?:脂酶|酯酶)?(?:增高|升高|偏高)?(?:[（(][^（）()]+[）)])?', name))


def _verified_urine_retest(document, personal):
    from .evaluation_builder import original_medical_advice_witnesses

    if not personal or not all(fact.kind == 'clinical' and _urine_leukocyte_finding(fact.name) for fact in personal):
        return None
    witnesses = original_medical_advice_witnesses(document, [fact.fact_id for fact in personal])
    plans = _unique(witness.text.strip() for witness in witnesses if _urine_leukocyte_finding(witness.name))
    if len(plans) != 1:
        return None
    text = _conditional_action(plans[0])
    return plans[0] if re.search(r'清洁中段(?:晨)?尿[^。；;]{0,8}复查尿常规', text) else None


def _source_supported_priority(priority, group, personal, action):
    if priority != 'P0' or group.get('attentionLevel') not in {'中风险', '低风险'}:
        return priority, action
    if personal and all(fact.kind == 'self_reported' for fact in personal):
        if not any(re.search(
            r'剧烈|严重|无法(?:活动|工作|进食)|晕厥|便血|黑便|胸痛|呼吸困难',
            fact.value.partition('；回答：')[-1],
        ) for fact in personal):
            # A planned review and future red flags do not establish an urgent
            # current finding. Leave any stated urgent symptom trigger intact.
            start = _conditional_action(action)
            if start:
                action = re.sub(r'(?:立即|紧急|尽快|尽早|优先)(?=安排|就诊|前往|完成|评估)', '', start
                                ) + action[len(start):]
            return 'P2', action
    if personal and all(fact.kind == 'clinical' and _urine_leukocyte_finding(fact.name) for fact in personal):
        # Keep the clinician's actual plan and conditional specialist visit.
        # The question and urine panel alone do not establish current urgency.
        repeat = r'复查尿常规|尿常规复查'
        urgent = r'立即|今天|当日|急诊|尽快'
        plans = [_conditional_action(text) for text in group.get('originalAdvice', ())]
        if (plans and all(re.search(repeat, plan) and not re.search(urgent, plan) for plan in plans)
                and re.search(repeat, _conditional_action(action)) and not re.search(
                    urgent, _conditional_action(action),
                )):
            return 'P1', action
    return priority, action


def _planned_text(data, reviewed, key, allowed):
    """Return the reviewed text and its exact personal-fact witnesses."""
    row = data._assessment_editorial.get(key)
    bound = reviewed.get(('_assessment_editorial', key, 'text'), set()).intersection(allowed)
    if not row or not row.text or not bound:
        return None, [], set()
    return row.text, list(row.evidence_refs), bound


def _same_projection_exam(left, right):
    """Establish an acquisition alias from source identity, never supporting refs."""
    origin = left.get('sourceSha256') or left.get('documentId')
    if not origin or origin != (right.get('sourceSha256') or right.get('documentId')):
        return False
    # A whole PDF is not an examination. A shared original page plus compatible
    # recorded acquisition context is deliberately narrower than document ID.
    if left.get('sourcePage') is None or left.get('sourcePage') != right.get('sourcePage'):
        return False
    for key in ('method', 'sourceReportedAge', 'dataType', 'specimen'):
        if left.get(key) is not None and right.get(key) is not None and left[key] != right[key]:
            return False
    subjects = [context.get('subject') or {} for context in (left, right)]
    if any(value is not None and subjects[1].get(key) is not None and value != subjects[1][key]
           for key, value in subjects[0].items()):
        return False
    dates = [str(context.get('examDate') or '') for context in (left, right)]
    if not all(dates):
        return dates[0] == dates[1]
    if dates[0][:10] != dates[1][:10]:
        return False
    timed = [bool(re.search(r'[T ]\d{1,2}:\d{2}', value)) for value in dates]
    return not all(timed) or dates[0].replace('T', ' ') == dates[1].replace('T', ' ')


def _display_problem_scopes(data, facts=None, projections=None):
    """Bind a reviewed problem to its original, independently verified quotes."""
    from .clinical_source_projection import get_patient_rows
    from .clinical_source_projection import valid_source_span
    from .evaluation_schema import clinical_problem_projection_codes

    def literal(value):
        return ''.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split()).strip('。；;，,')

    frames = getattr(data, '_clinical_problem_projection', ())
    codes = clinical_problem_projection_codes(data) if frames else {}
    scopes = {}
    for group in getattr(data, '_template_layout', {}).get('groups', ()):
        members = set(group.get('memberCodes', ()))
        quotes = {}
        for frame in frames:
            if codes.get(frame.problem_id) not in members:
                continue
            for source in frame.sources:
                if source.quote:
                    quotes.setdefault(source.fact_id, []).append(source.quote)
                if not facts or not projections or source.fact_id not in facts:
                    continue
                original = projections.get(source.fact_id, {})
                # Start with this verified problem's literal atom. A composite
                # abdominal record cannot lend its unrelated organ findings to
                # a fatty-liver problem merely by sharing source references.
                anchors = get_patient_rows(facts[source.fact_id], projection_index=projections,
                                          source_quotes=[source.quote])
                title = literal(frame.title)
                anchors = [row for row in anchors if literal(row.get('name')) in title
                           or literal(row.get('value')) == literal(source.quote)]
                anchor_quotes = [literal(span['quote']) for row in anchors for span in row['sourceSpans']
                                 if valid_source_span(facts[source.fact_id], span)]
                for fid, candidate in projections.items():
                    if fid == source.fact_id or fid not in facts or not _same_projection_exam(
                        original.get('sourceContext') or {}, candidate.get('sourceContext') or {},
                    ):
                        continue
                    for row in get_patient_rows(facts[fid], projection_index=projections):
                        if row.get('ownRecordId') != fid or row.get('role') == 'reported_behavior':
                            continue
                        spans = [span for span in row['sourceSpans'] if valid_source_span(facts[fid], span)]
                        if any(literal(span['quote']) == quote for span in spans for quote in anchor_quotes):
                            quotes.setdefault(fid, []).extend(span['quote'] for span in spans)
        scopes[group['code']] = {fid: _unique(values) for fid, values in quotes.items()}
    return scopes


def _display_source_index(clinical, microbiome, facts, *, reported_roles=None, document=None, data=None):
    """Index vetted observations, separately from the full private source quotes."""
    from .assessment_microbiome_view import source_observation_display_status
    from .assessment_reported_observation import model_reported_observation
    from .clinical_source_projection import ensure_patient_projection_index
    from .clinical_source_projection import reported_temporal_context

    projections = ensure_patient_projection_index(document) if document is not None else {}
    index = {
        '_clinicalProjections': projections,
        '_problemScopes': _display_problem_scopes(data, facts, projections) if data is not None else {},
        '_reportedFacets': {},
        '_reportedTemporal': {},
        '_affirmedSymptoms': set(),
    }
    rows = [*microbiome['structure'], *microbiome.get('_reportedAssessmentRows', ()),
            *(row for group in [*microbiome.get('structureGroups', ()),
                                 *microbiome['organismGroups'], *microbiome['functionalGroups']]
              for row in _panel_rows(group))]
    fields = ('name', 'value', 'unit', 'referenceRange', 'observedAt')
    for row in rows:
        if row.get('value') is None or row.get('value') == '':
            continue
        identifiers = row.get('_semanticOwnerRecordIds', ())
        sources = [dict(source) for source in row.get('sources', ()) if source.get('sourceCode') == 'C'
                   and source.get('recordId') in identifiers]
        signal = {key: row.get(key) for key in fields}
        signal.update(status=source_observation_display_status(row), evidenceRefs=[])
        semantics = row.get('_sourceSemantics') or {}
        if semantics.get('objectScope') == 'reported_assessment':
            # Scope the source result without changing the original value or treating it as a sequence assay.
            signal['name'] = semantics.get('reportedObject') or signal['name']
        for fid in identifiers:
            if fid in facts and facts[fid].kind in {'microbe', 'function', 'context'}:
                source = {'sourceCode': 'C', 'recordId': fid, 'observedAt': row.get('observedAt')}
                owned_sources = sources if any(ref.get('recordId') == fid for ref in sources) else [*sources, source]
                index.setdefault(fid, []).append({**signal, 'sources': owned_sources})
    for fid, fact in facts.items():
        role = (reported_roles or {}).get(fid, {})
        if fact.kind != 'self_reported':
            continue
        question, marker, answer = str(fact.value or '').removeprefix('问题：').partition('；回答：')
        if not marker:
            question, answer = fact.name, fact.value
        question, answer = role.get('question') or question, role.get('answer') or answer
        # This is questionnaire metadata, not a rewrite of a clinical statement.
        question = re.sub(r'^\s*(?:[Qq]\s*)?\d+\s*[、.．:：)）]\s*', '', str(question or ''))
        temporal = reported_temporal_context(fact, reported_role=role)
        index['_reportedTemporal'][fid] = temporal
        observation = model_reported_observation(
            fact.model_dump(mode='json', by_alias=True),
            reported_role=role,
            temporal_context=temporal,
        )
        if observation and observation.get('selectionBasis') == 'literal_answer_facet':
            index['_reportedFacets'][fid] = observation['value']
            if observation.get('assertionRole') == 'reported_symptom':
                index['_affirmedSymptoms'].add(fid)
        # A submission date locates the answer, not the recalled collection
        # episode. The full question carries its actual event/time condition.
        observed_at = temporal.get('eventDate') if temporal.get('kind') == 'retrospective_event' else fact.date
        source_code = 'B' if fact.kind == 'self_reported' else 'A'
        index[fid] = [{
            'name': question or fact.name, 'value': answer,
            'unit': fact.unit, 'referenceRange': fact.reference_range,
            'status': '本人自述', 'observedAt': observed_at,
            'sources': [{'sourceCode': source_code, 'recordId': fid,
                         'observedAt': observed_at}],
            'evidenceRefs': [],
        }]
    return index


def _strip_display_metadata(value):
    """Keep source selection and semantic audit metadata out of public rows."""
    if isinstance(value, dict):
        for key in list(value):
            if key.startswith('_'):
                value.pop(key)
            else:
                _strip_display_metadata(value[key])
    elif isinstance(value, list):
        for item in value:
            _strip_display_metadata(item)


def _source_scope_quotes(display_index, problem_codes):
    quotes = {}
    for code in problem_codes:
        for fid, values in (display_index or {}).get('_problemScopes', {}).get(code, {}).items():
            quotes.setdefault(fid, []).extend(values)
    return {fid: _unique(values) for fid, values in quotes.items()}


def _planned_signals(identifiers, facts, *, kinds, refs=(), display_index=None,
                     source_selections=(), clinical_selections=(), source_quotes=None):
    from .clinical_source_projection import PATIENT_ROLES
    from .clinical_source_projection import get_patient_rows
    from .clinical_source_projection import valid_source_span

    selected_quotes = {}
    for selection in source_selections:
        selected_quotes.setdefault(selection['recordId'], set()).add(selection['quote'])
    clinical_quotes = {}
    for selection in clinical_selections:
        clinical_quotes.setdefault(selection['recordId'], set()).add(selection['quote'])
    signals = []
    for fid in identifiers:
        if fid not in facts or facts[fid].kind not in kinds:
            continue
        if facts[fid].kind == 'clinical':
            rows = get_patient_rows(facts[fid], projection_index=(display_index or {}).get('_clinicalProjections'))
            if fid in selected_quotes:
                rows = get_patient_rows(facts[fid],
                    projection_index=(display_index or {}).get('_clinicalProjections'),
                    source_quotes=selected_quotes[fid])
                rows = [row for row in rows if row.get('role') == 'reported_behavior' and any(
                    span.get('quote') in selected_quotes[fid] and valid_source_span(facts[fid], span)
                    for span in row.get('sourceSpans', ())
                )]
            elif fid in clinical_quotes:
                rows = [row for row in rows if row.get('role') in PATIENT_ROLES
                        and row.get('role') != 'reported_behavior' and any(
                            span.get('quote') in clinical_quotes[fid] and valid_source_span(facts[fid], span)
                            for span in row.get('sourceSpans', ())
                        )]
            elif (source_quotes or {}).get(fid):
                # Aggregate system cells may use already verified problem
                # witnesses. These are exact source spans, never a guess from
                # the parent examination's title or its sole conclusion.
                rows = get_patient_rows(facts[fid],
                    projection_index=(display_index or {}).get('_clinicalProjections'),
                    source_quotes=source_quotes[fid])
            elif len(rows) > 1:
                rows = []  # A compound record needs explicit reviewed atom selections.
            rows = [{key: row.get(key) for key in (
                'name', 'value', 'unit', 'referenceRange', 'status', 'observedAt', 'sources',
            )} for row in rows if row.get('ownRecordId') == fid]
        else:
            rows = (display_index or {}).get(fid, ())
        for row in rows:
            signals.append({**row, 'sources': [dict(source) for source in row.get('sources') or ()],
                            'evidenceRefs': list(refs)})
    return _distinct_signals(signals)


def _planned_cluster_advice(problem_codes, allowed, facts, advice_by_code):
    """Put verified source wording in observations while review fields stay empty."""
    result = []
    for code in problem_codes:
        same_instruction = {}
        for scope in advice_by_code.get(code, ()):
            ids = scope['sourceFactIds']
            if scope.get('displayScope') == 'source_reference':
                continue
            if scope['scopeStatus'] != 'explicit_object' and not (
                scope['scopeStatus'] == 'source_record_scope' and set(ids) <= allowed
            ):
                continue
            labels = scope['objectLabels'] or scope['sourceRecordLabels']
            sources = [{'sourceCode': 'A', 'recordId': fid, 'observedAt': facts[fid].date} for fid in ids]
            quote_key = re.sub(r'\s+', '', scope['quote'])
            if quote_key in same_instruction:
                refs = same_instruction[quote_key]['sources']
                refs.extend(ref for ref in sources if ref not in refs)
                continue
            signal = {
                # ``kind`` identifies recorded examination advice; the status stays a legacy marker.
                'name': '检查建议' + (' · ' + '、'.join(labels) if labels else ''), 'kind': 'advice',
                'value': scope['quote'], 'unit': None, 'referenceRange': None, 'status': _ADVICE_STATUS,
                'observedAt': None, 'sources': sources, 'evidenceRefs': [],
            }
            same_instruction[quote_key] = signal
            result.append(signal)
    return _distinct_signals(result)


def _planned_clusters(data, facts, reviewed, plan, *, display_index=None, reviewed_blocks=None):
    """Project reviewed themes; a quantitative item never creates its own cluster."""
    if plan.get('status') != 'reviewed':
        return []
    advice_by_code = {item['code']: item['sourceAdviceScopes'] for item in _source_clinical_actions(data, facts)}
    reviewed_blocks = reviewed_blocks or {}
    result = []
    for row in plan.get('clusters', ()):
        allowed = {fid for fid in row.get('factIds', ()) if fid in facts}
        interpretation_key = 'synthesis.' + row['code']
        action_key = 'synthesisAction.' + row['code']
        text, refs, used = _planned_text(data, reviewed, interpretation_key, allowed)
        action, action_refs, action_ids = _planned_text(data, reviewed, action_key, allowed)
        # The reviewed source plan owns the theme and its source-only columns.
        # A missing draft must neither remove that theme nor complete its prose.
        reported_ids = {fid for fid in allowed if facts[fid].kind == 'self_reported'}
        source_ids = (set(row.get('clinicalFactIds', ())) | reported_ids | used) & allowed
        clinical_ids = [fid for fid in row.get('factIds', ())
                        if fid in source_ids and facts[fid].kind in {'clinical', 'self_reported'}]
        clinical = _planned_signals(clinical_ids, facts, kinds={'clinical', 'self_reported'},
                                    display_index=display_index,
                                    clinical_selections=row.get('clinicalSelections', ()))
        gut_scope = set(row.get('microbiomeFactIds', ())) & allowed
        if row['priority'] == 'P0':
            # Keep a priority medical workup free of unrelated microbiome
            # context. An accepted joint interpretation must own the link.
            clinical_scope = {fid for fid in clinical_ids if facts[fid].kind == 'clinical'}
            block = reviewed_blocks.get(interpretation_key, {})
            jointly_reviewed = set()
            if text and block.get('completion') == 'complete':
                for paragraph in block.get('sentences', ()):
                    identifiers = set(paragraph.get('factIds', ()))
                    if (paragraph.get('role') == 'interpretation'
                            and paragraph.get('support') in {'clinical_synthesis', 'evidence', 'applicable_guideline'}
                            and identifiers & clinical_scope):
                        jointly_reviewed.update(identifiers & used & gut_scope)
            gut_scope = jointly_reviewed
        gut_ids = [fid for fid in row.get('microbiomeFactIds', ()) if fid in gut_scope]
        advice = _planned_cluster_advice(row.get('problemCodes', ()), allowed, facts, advice_by_code)
        clinical.extend(advice)
        action_block = reviewed_blocks.get(action_key, {})
        if action_block.get('completion') == 'source_only' or action_block.get('analysisGenerated') is False:
            action, action_refs, action_ids = None, [], set()
        if len(advice) == 1 and not action:
            # A documented clinical instruction is actionable in its own
            # right. Preserve the original label and conditions. Publication
            # here does not promote a missing author block to a reviewed one.
            signal = advice[0]
            label = signal['name'].removeprefix('检查建议').strip(' ·')
            quote = signal['value'].strip()
            if label and not quote.startswith(label):
                quote = label + '：' + quote
            action = '检查建议：' + quote
            action_refs = []
            action_ids = {source['recordId'] for signal in advice for source in signal['sources']}
        result.append(({
            '_codes': list(row.get('problemCodes') or [row['code']]),
            '_planCode': row['code'],
            '_actionRefs': action_refs,
            'name': row['name'],
            'priority': row['priority'],
            'interpretation': text,
            'action': action,
            'clinicalSignals': clinical,
            'microbiomeSignals': _planned_signals(gut_ids, facts, kinds={'microbe', 'function', 'context'},
                                                   display_index=display_index),
            'evidenceRefs': _unique([*refs, *action_refs]),
        }, (source_ids - set(row.get('microbiomeFactIds', ()))) | set(gut_ids) | action_ids))
    return sorted(result, key=lambda entry: entry[0]['priority'])


def _planned_symptoms(clinical, data, facts, reviewed, plan, *, display_index=None):
    """Keep the reviewed column association and all selected source observations."""
    if plan.get('status') == 'unavailable':
        clinical['symptoms'] = []
        return
    existing = {row['code']: row for row in clinical['symptoms']}
    result = []
    affirmed = (display_index or {}).get('_affirmedSymptoms', ())
    for row in plan.get('symptoms', ()):
        allowed = {fid for fid in row.get('factIds', ()) if fid in facts}
        old = existing.get(row['code'], {})
        if not allowed.intersection(source.get('recordId') for source in old.get('sources', ())):
            old = {}
        slot_ids = _unique(source['recordId'] for source in old.get('sources', ())
                           if source.get('sourceCode') == 'B' and source.get('recordId') in facts)
        reported_ids = [fid for fid in row.get('factIds', ())
                        if fid in facts and facts[fid].kind == 'self_reported']
        # The validated symptom slot owns the display value. A supporting or
        # contrasting answer in the plan cannot become part of that assertion.
        clinical_record = old.get('sourceKind') == 'clinicalRecord'
        owners = slot_ids if old else [fid for fid in reported_ids if fid in affirmed]
        listed_context = [fid for fid in row.get('lifestyleFactIds', ()) if fid in facts]
        context_ids = [fid for fid in _unique([*listed_context, *reported_ids]) if fid not in owners]
        text, refs, _ = _planned_text(data, reviewed, 'symptom.' + row['code'], allowed | set(context_ids))
        source_signals = _planned_signals(owners, facts, kinds={'self_reported'}, display_index=display_index)
        facets = (display_index or {}).get('_reportedFacets', {})
        selected_values = [str(facets[fid]) for fid in owners] if all(fid in facets for fid in owners) else []
        if not selected_values and not old.get('value'):
            continue
        name = row['name']
        times = (display_index or {}).get('_reportedTemporal', {})
        recalled = _unique(re.sub(r'^(?:这次|此次|当次|那次)', '', times[fid]['scopeQuote']).strip()
                           for fid in owners if times.get(fid, {}).get('kind') == 'retrospective_event')
        if len(recalled) == 1 and recalled[0] and recalled[0] not in name:
            name += f'（回顾{recalled[0]}）'
        context = _planned_signals(context_ids, facts, kinds={'self_reported'}, display_index=display_index)
        if recalled:
            # Keep the other choices from the same recalled episode before
            # unrelated lifestyle context. The selected symptom stays unchanged.
            complete_answers = [signal for signal in source_signals
                                if signal.get('value') and signal['value'] not in selected_values]
            context = complete_answers + context
        item = {
            'code': row['code'], 'name': name,
            # The selected assertion is the symptom; the full answer may also
            # contain goals or other facets and remains in the source index.
            'value': '；'.join(_unique(selected_values)) or old.get('value'),
            'history': '；'.join(_unique(
                f'{signal["name"]}：{signal["value"]}'
                for signal in context
            )) or None,
            'unit': old.get('unit'), 'referenceRange': old.get('referenceRange'),
            'status': old.get('status') or '本人自述', 'observedAt': None,
            'interpretation': text, 'sourceKind': 'selfReported',
            'sources': [source for signal in source_signals for source in signal['sources']] or old.get('sources', []),
            'evidenceRefs': refs, 'clinicalSignals': [], 'microbiomeSignals': [],
            'question': '；'.join(_unique(signal['name'] for signal in source_signals)) or old.get('question'),
        }
        if clinical_record and not owners:
            item.update(sourceKind='clinicalRecord', observedAt=old.get('observedAt'))
        primary = set(owners) or {source.get('recordId') for source in item['sources']}
        for kind, target, identifiers, kinds in (
            ('clinical', 'clinicalSignals', row.get('clinicalFactIds', ()), {'clinical'}),
            ('microbiome', 'microbiomeSignals', row.get('microbiomeFactIds', ()), {'microbe', 'function', 'context'}),
        ):
            column_allowed = allowed | set(identifiers)
            column_text, column_refs, column_used = _planned_text(
                data, reviewed, 'symptomEvidence.' + row['code'] + '.' + kind, column_allowed,
            )
            accepted = column_text and primary.intersection(column_used)
            signal_ids = [fid for fid in identifiers if not accepted or fid in column_used]
            signals = _planned_signals(signal_ids, facts,
                                       kinds=kinds, display_index=display_index,
                                       clinical_selections=row.get('clinicalSelections', ())
                                       if kind == 'clinical' else ())
            if signals:
                if accepted:
                    signals[0].update(interpretation=column_text, evidenceRefs=column_refs)
                item[target] = signals
        result.append(item)
    clinical['symptoms'] = result


def _planned_lifestyle(data, facts, reviewed, clusters, plan, *, display_index=None):
    if plan.get('status') == 'unavailable':
        return []
    by_code = {item['_planCode']: item for item, _ in clusters}
    result = []
    for row in plan.get('lifestyleFactors', ()):
        allowed = {fid for fid in row.get('factIds', ()) if fid in facts}
        text, refs, _ = _planned_text(data, reviewed, 'lifestyle.' + row['code'], allowed)
        source_rows = _planned_signals(row.get('factIds', ()), facts, kinds={'self_reported', 'clinical'},
                                      display_index=display_index,
                                      source_selections=row.get('sourceSelections', ()))
        if not source_rows:
            continue
        result.append({
            'code': row['code'], 'name': row['name'], 'kind': 'habit',
            'value': '；'.join(_unique(
                (f'{signal["observedAt"]} ' if signal.get('observedAt') else '')
                + f'{signal["name"]}：{signal["value"]}' for signal in source_rows
            )),
            'interpretation': text,
            'clusterIds': [by_code[code]['id'] for code in row.get('clusterCodes', ()) if code in by_code],
            'evidenceRefs': refs,
        })
    return result


def _planned_systems(clinical, microbiome, clusters, data, facts, reviewed, plan, *, display_index=None):
    from .assessment_clinical_view import _measurement_state
    from .assessment_clinical_view import select_overview_clinical_anchors
    from .assessment_microbiome_view import select_overview_microbiome_anchors

    scopes = {row['code']: row for row in plan.get('cellScopes', ())}
    result = []
    for code, name, group_codes in _SYSTEMS:
        key = 'system.' + code
        scope = scopes.get(key)
        if scope is None:
            continue
        allowed = set(scope.get('factIds', ())) | set(scope.get('contextFactIds', ()))
        text, refs, used = _planned_text(data, reviewed, key, allowed)
        used = used or set(scope.get('factIds', ()))
        if not used:
            continue
        problem_codes = [group['code'] for group in getattr(data, '_template_layout', {}).get('groups', ())
                         if group.get('origin') == 'clinical_group' and set(
                             group.get('originalFactIds', group.get('sourceFactIds', ())),
                         ).intersection(used)]
        # A reviewed explanation's source permissions are the complete
        # inventory it used, not a request to print every allowed record.
        # Clinical overview vectors reuse selected reader atoms; the full
        # questionnaire and measurement inventory remain elsewhere intact.
        rows = select_overview_clinical_anchors(
            clinical, group_codes, used,
            source_quotes=_source_scope_quotes(display_index, problem_codes),
            decision_ids=[fid for section in ('clusters', 'symptoms') for entry in plan.get(section, ())
                          for fid in entry.get('clinicalFactIds', ()) if fid in used],
            clinical_selections=[selection for section in ('clusters', 'symptoms')
                                 for entry in plan.get(section, ())
                                 for selection in entry.get('clinicalSelections', ())
                                 if selection.get('recordId') in used],
        )
        rows.extend(select_overview_microbiome_anchors(
            microbiome, used, facts, composition_scope=allowed if code == 'microbiome' else (),
        ))
        states = [row.pop('_overviewAssessmentState', None) or _measurement_state(row.get('status')) for row in rows]
        status = None
        if code == 'microbiome':
            status = next((group['status'] for group in microbiome.get('structureGroups', ())
                           if group['code'] == 'overallBalance' and group.get('status')), None)
        else:
            own_rows = [row for group in clinical['clinicalGroups'] if group['code'] in group_codes
                        for row in group.get('templateRows', ())]
            focus = [row['name'] for row in own_rows if str(row.get('status') or '').startswith('重点关注')]
            if focus:
                status = '重点关注：' + '、'.join(focus)
        if not status:
            abnormal_names = _unique(row['name'] for row, state in zip(rows, states) if state == 'abnormal')
            status = ('分项异常：' + '、'.join(abnormal_names) if abnormal_names
                      else '已检项目未见明确异常' if states and all(state == 'normal' for state in states)
                      else '未分级')
        if not rows and not text:
            continue
        result.append({
            'code': code, 'name': name,
            'status': status,
            'summary': text, 'evidenceRefs': refs, 'rows': _distinct_signals(rows),
        })
    return _complete_systems(result, clinical, microbiome)


def _complete_systems(result, clinical, microbiome):
    """Give every fixed system its chart fields (spec 1.2 and 2.7); append only.

    ``level`` is the most severe member level read from chapters three and four,
    ``anchors`` lists at most six abnormal members (cell anchors first) and
    ``moreCount`` the rest. A system without a reviewed cell is still listed so
    that a reader sees it as measured or ``notTested``; it carries no verdict.
    """
    from .assessment_chart_fields import OTHER_SYSTEM_CODE
    from .assessment_chart_fields import OTHER_SYSTEM_NAME
    from .assessment_chart_fields import system_atoms
    from .assessment_chart_fields import system_chart_fields

    by_code = {item['code']: item for item in result}
    complete = []
    for code, name, group_codes in _SYSTEMS:
        item = by_code.get(code) or {
            'code': code, 'name': name, 'status': None, 'summary': None, 'evidenceRefs': [], 'rows': [],
        }
        atoms = system_atoms(code, group_codes, clinical, microbiome)
        item.update(system_chart_fields(atoms, item['rows'], microbiome=code == 'microbiome'))
        complete.append(item)
    # Abnormal results outside the five systems get a sixth card instead of
    # disappearing from the overview (spec 1.2).
    atoms = system_atoms(OTHER_SYSTEM_CODE, (OTHER_SYSTEM_CODE,), clinical, microbiome)
    fields = system_chart_fields(atoms, [], microbiome=False)
    if fields.get('level') in ('focus', 'borderline'):
        complete.append({
            'code': OTHER_SYSTEM_CODE, 'name': OTHER_SYSTEM_NAME, 'status': None,
            'summary': f'另有{len(fields.get("anchors") or ()) + int(fields.get("moreCount") or 0)}项需关注的检查结果，'
                       '详见第三章分系统评估与专项检测。',
            'evidenceRefs': [], 'rows': [], **fields,
        })
    return complete


def _assessment_clusters(data, facts, reviewed, *, subject=None, document=None, display_index=None,
                         reviewed_blocks=None):
    if (plan := _content_plan(data)) is not None:
        return _planned_clusters(data, facts, reviewed, plan, display_index=display_index,
                                 reviewed_blocks=reviewed_blocks)
    """Publish source-backed problems and link only facts used by a reviewed explanation."""
    from .clinical_report import reported_fact_kind
    from .evaluation_builder import _key
    from .evaluation_builder import _normal_clinical_result
    from .evaluation_presentation import reader_text

    findings = {row.code: (index, row) for index, row in enumerate(data.medical_assessment.findings)}
    risk_rows = {row.code: (index, row) for index, row in enumerate(data.disease_risk_assessment.risk_ranking)}
    actions = {row.code: row for row in data.comprehensive_conclusion.next_steps if row.description}
    candidates = {}
    groups = getattr(data, '_template_layout', {}).get('groups', ())
    for group in groups:
        member = findings.get(group.get('code'))
        if member is None or group.get('origin') != 'clinical_group' or group.get('role') in {
            'normal_screening', 'management_goal', 'source_association',
        }:
            continue
        index, row = member
        source_ids = group.get('originalFactIds') or _source_ids(row)
        personal = [facts[fid] for fid in source_ids if fid in facts and (
            facts[fid].kind == 'clinical' and _clinical_finding_anchor(facts[fid])
            or reported_fact_kind(facts[fid]) in {'symptom', 'history'}
        )]
        if not personal or not row.name:
            continue
        specific = [fact for fact in personal if not re.search(r'^(?:体检)?(?:主检|总检|体检结论)', fact.name)]
        if specific:
            personal = specific
        path = ('medical_assessment', 'findings', index)
        explanation = row.explanation if (*path, 'explanation') in reviewed else None
        related = reviewed.get((*path, 'explanation'), set())
        refs = list(row.evidence_refs) if explanation else []
        risk = risk_rows.get(group.get('riskCode'))
        if risk is not None:
            risk_index, risk_row = risk
            risk_path = ('disease_risk_assessment', 'risk_ranking', risk_index, 'explanation')
            if risk_path in reviewed and risk_row.explanation:
                related = related | reviewed[risk_path]
                if not explanation:
                    explanation = risk_row.explanation
                    refs = list(risk_row.evidence_refs)
        joint = data._assessment_editorial.get('impact.' + row.code)
        joint_ids = reviewed.get(('_assessment_editorial', 'impact.' + row.code, 'text'), set())
        if joint and joint.text and joint.evidence_refs and joint_ids.intersection(source_ids):
            explanation = joint.text
            refs = list(joint.evidence_refs)
            related = joint_ids
        step = actions.get(row.code)
        reviewed_advice = row.source_advice if (*path, 'source_advice') in reviewed else None
        action = step.description if step is not None else reviewed_advice
        if step is not None:
            refs.extend(step.evidence_refs)
        elif action:
            refs.extend(row.evidence_refs)
        published_action = _clinical_action(
            row.name, facts, action, document=document, source_ids=source_ids, other_reviewed=reviewed_advice,
        )
        if urine_retest := _verified_urine_retest(document, personal):
            published_action = reader_text(urine_retest, prose=True)
            group = {**group, 'originalAdvice': [urine_retest]}
        if action and published_action == reader_text(action, prose=True):
            action_refs = list(step.evidence_refs) if step is not None else list(row.evidence_refs)
        else:
            action_refs = list(row.evidence_refs) if reviewed_advice and published_action == reader_text(
                reviewed_advice, prose=True,
            ) else []
        action = published_action
        priority = _PRIORITIES.get(group.get('focusLabel'), 'P3')
        priority, action = _source_supported_priority(priority, group, personal, action)
        if group.get('role') == 'secondary_finding' and priority != 'P0':
            priority = 'P3'
        if (group.get('role') == 'health_problem' and priority == 'P3'
                and any(fact.kind == 'clinical' for fact in personal)
                and any(reported_fact_kind(fact) == 'symptom' for fact in personal)
                and _active_clinical_followup(action)):
            # A current, objectively supported complaint with an active
            # workup belongs ahead of symptom-only observation.
            priority = 'P2'
        if priority == 'P1' and any(re.search(
            r'(?:检查结果和建议|检测结果和建议|综合结论|总体结论)[：:]?[^。]{0,35}(?:大致|基本)正常',
            fact.value,
        ) for fact in personal):
            priority = 'P2'
        if priority == 'P1' and any(_is_reported_imaging(fact) for fact in personal) and re.search(
            r'常规(?:复查|随访)|随访观察为主|定期(?:超声随访|超声观察)|无需(?:特殊处理|专门随访)'
            r'|随访价值有限|影响(?:很小|有限)|不改变[^。；]{0,10}优先(?:顺序|程度)|低度可疑'
            r'|(?:体积|尺寸|体积较|尺寸较)小[^。；]{0,12}影像发现|(?:安排|结合|定期)[^。；]{0,18}超声复查',
            '。'.join(filter(None, (explanation, action))),
        ) and not re.search(
            r'优先|紧急|尽快|显著增大|短期进展',
            re.split(r'(?:[；。\n]下?|^|，[若如]?)出现|(?:若|如)有', action or '', maxsplit=1)[0],
        ):
            priority = 'P3'
        if not action and priority == 'P0':
            action = '优先携带相关检查结果就医，核实异常原因。'
        candidates[row.code] = ({
            '_codes': [row.code],
            '_actionRefs': action_refs,
            'name': row.name,
            'priority': priority,
            'interpretation': explanation,
            'action': action,
            'clinicalSignals': _distinct_signals(_signal(fact, issue=row.name) for fact in personal),
            'microbiomeSignals': _microbiome_evidence(related, facts, explanation, refs),
            'evidenceRefs': _unique(refs),
        }, {fact.fact_id for fact in personal})
    _merge_reviewed_clusters(data, candidates, facts, reviewed)
    _consolidate_assessments(candidates, {group.get('code'): group for group in groups})
    clusters = list(candidates.values())
    grouped_codes = {code for item, _ in clusters for code in item['_codes']}
    owners = _clinical_action_owners(data)
    for index, row in enumerate(data.medical_assessment.findings):
        if (not row.name or row.code in grouped_codes
                or row.certainty in {'model_risk', 'source_association', 'self_reported'}):
            continue
        verified = owners.get(row.code)
        names = {_key(row.name), _key(row.code)}
        referenced = set(verified or _source_ids(row))
        personal = [fact for fid, fact in facts.items() if fid in referenced
                    and fact.kind == 'clinical' and _clinical_finding_anchor(fact)
                    and not _normal_clinical_result(fact)
                    and (verified or (fact.abnormal and _key(fact.name) in names))]
        if not personal:
            continue
        path = ('medical_assessment', 'findings', index)
        explanation = row.explanation if (*path, 'explanation') in reviewed else None
        reviewed_advice = row.source_advice if (*path, 'source_advice') in reviewed else None
        source_ids = {fact.fact_id for fact in personal}
        action = _clinical_action(row.name, facts, reviewed_advice, document=document, source_ids=source_ids)
        if not action or any(source_ids <= ids and action == item['action'] for item, ids in clusters):
            continue
        clusters.append(({
            '_codes': [row.code],
            '_actionRefs': list(row.evidence_refs) if reviewed_advice and action == reader_text(
                reviewed_advice, prose=True,
            ) else [],
            'name': row.name,
            'priority': 'P3',
            'interpretation': explanation,
            'action': action,
            'clinicalSignals': [_signal(fact) for fact in personal],
            'microbiomeSignals': [],
            'evidenceRefs': list(row.evidence_refs) if explanation or action else [],
        }, source_ids))
    if document is not None:
        clusters = _merge_single_measurement_actions(clusters, data, facts, subject, document)
    clusters = _shared_collection_actions(clusters, facts)
    clusters.sort(key=lambda entry: (
        entry[0]['priority'],
        not any(facts[fid].kind == 'clinical' for fid in entry[1]),
    ))
    distinct = {}
    for item, anchors in clusters:
        key = re.sub(r'[\W_]+', '', item['name']).casefold()
        if key not in distinct:
            distinct[key] = item, anchors
            continue
        primary, primary_anchors = distinct[key]
        primary['_codes'].extend(code for code in item['_codes'] if code not in primary['_codes'])
        primary_anchors.update(anchors)
        if not primary['action'] and item['action']:
            primary['action'] = item['action']
            primary['_actionRefs'] = item['_actionRefs']
            primary['evidenceRefs'] = _unique([*primary['evidenceRefs'], *item['evidenceRefs']])
        known = {src['recordId'] for signal in primary['clinicalSignals'] for src in signal['sources']}
        primary['clinicalSignals'].extend(signal for signal in item['clinicalSignals']
                                         if any(src['recordId'] not in known for src in signal['sources']))
    return list(distinct.values())



def reviewed_conclusion_fallback(blueprint, accepted, document, subject):
    """Reuse publishable actions and reviewed follow-up goals if summary authoring failed."""
    from .evaluation_presentation import reader_text

    data = blueprint.data
    if _content_plan(data) is not None:
        # An absent synthesized conclusion remains an authored-content gap.
        # Repeated action paragraphs are not a substitute for that judgment.
        return None
    path = ('comprehensive_conclusion', 'overall_conclusion', 'text')
    targets = {target['id']: target for chapter in blueprint.chapters for target in chapter['targets']}
    by_path = {tuple(targets[row['targetId']]['path']): row for row in accepted if row['targetId'] in targets
               and row.get('completion') == 'complete'}
    target = next((target for target in targets.values() if tuple(target['path']) == path), None)
    if not target or path in by_path or data.comprehensive_conclusion.overall_conclusion.text:
        return None
    facts = {fact.fact_id: fact for fact in document.facts}
    clusters = _assessment_clusters(data, facts, _reviewed_facts(blueprint, accepted),
                                    subject=subject, document=document)
    required_actions = {
        data.comprehensive_conclusion.next_steps[entry['path'][2]].code
        for entry in targets.values() if entry.get('required')
        and tuple(entry['path'][:2]) == ('comprehensive_conclusion', 'next_steps')
    }
    if any(not item['action'] and item['priority'] != 'P3' and (
        item['priority'] in {'P0', 'P1'} or required_actions.intersection(item['_codes'])
    ) for item, _ in clusters):
        return None
    items = [(item, ids) for item, ids in clusters if item['priority'] != 'P3' and (
        item['priority'] == 'P0' or item['interpretation'] or item['action']
    )][:6]
    if not items or any(not item['action'] for item, _ in items):
        return None
    accepted_actions = [block for block_path, block in by_path.items()
                        if block_path[-1] in {'description', 'source_advice'}
                        and block_path[0] in {'comprehensive_conclusion', 'medical_assessment'}]
    allowed = set(target['sourceFactIds'])
    clauses = []
    for item, ids in items:
        observed = set(ids).intersection(allowed)
        if not observed:
            return None
        action = item['action']
        reuse = next((block for block in accepted_actions if set(block['factIds']).intersection(observed)
                      and reader_text(block['text'], prose=True) == action), None)
        if reuse:
            source_sentences = [*reuse['sentences']]
        elif (original := _original_clinical_plan(item['name'], facts, document, ids)) and (
            reader_text(original, prose=True) == action
        ):
            source_sentences = [{'text': action, 'role': 'action', 'support': 'source', 'evidence': [],
                                 'factIds': [fid for fid in target['sourceFactIds'] if fid in observed]}]
        else:
            # A generic urgent placeholder or unverified drafted action is not
            # enough to claim that a complete conclusion is available.
            return None
        clauses.append(source_sentences)
    goals = [by_path[('_assessment_editorial', layer, 'text')]['sentences']
             for layer in ('layer.core', 'layer.maintenance') if ('_assessment_editorial', layer, 'text') in by_path]
    limit = target['cellContract']['maxChars']
    sentences = []
    for parts in [*clauses[:2], *goals]:
        candidate = [*sentences, *parts]
        if len('\n\n'.join(part['text'] for part in candidate)) <= limit and all(
            part.get('factIds') and set(part['factIds']) <= allowed for part in parts
        ):
            sentences = candidate
    if not sentences or not any(part in sentences for part in clauses[0]):
        return None
    return target, {'targetId': target['id'], 'completion': 'complete', 'analysisGenerated': False,
                    'sentences': sentences, 'reviewedConclusionReuse': True}


def _habit_text(label, answer):
    if label == '安排运动' and answer in {'没有运动', '不运动', '无运动'}:
        return '未安排运动'
    if label.endswith('时间'):
        answer = answer.removeprefix('时间')
    return f'{label}：{answer}'


def _symptom_signals(name, signals):
    from .assessment_clinical_view import project_symptom_clinical_evidence

    projected = _distinct_signals(
        row for signal in signals if (row := project_symptom_clinical_evidence(name, signal))
    )
    scoped = [row for row in projected if row.get('status') == '相关查体']
    for parent in scoped:
        originals = {ref['recordId'] for ref in parent['sources']}
        clauses = str(parent['value']).split('；')
        for child in projected:
            if (parent is child or child.get('status') == '相关查体'
                    or parent.get('observedAt') != child.get('observedAt')
                    or not any(ref['recordId'] in originals for ref in child['sources'])):
                continue
            matching = str(child['value']).strip()
            if len(matching) >= 4:
                clauses = [clause for clause in clauses if matching not in clause]
        parent['value'] = '；'.join(clauses)
    return [row for row in projected if row.get('value')]


def _reviewed_symptom_column(item, candidates, block, *, refs=None):
    """Select the exact observations reviewed for this symptom and this column."""
    owners = {source['recordId'] for source in item['sources']}
    ids = set()
    for sentence in block.get('sentences', ()):
        used = set(sentence.get('factIds', ()))
        if not owners.intersection(used):
            continue
        if refs is not None and (
            not refs or not sentence.get('evidence') or _UNSUPPORTED_ASSOCIATION.search(sentence['text'])
        ):
            continue
        ids.update(used - owners)
    return [
        {**row, 'evidenceRefs': list(refs)} if refs else row
        for row in candidates
        if ids.intersection(source['recordId'] for source in row['sources'])
    ]


def retain_applicable_symptom_blocks(blueprint, evidence, blocks):
    """Accept observational evidence for a personal sample, distinct from treatment trials."""
    if _content_plan(blueprint.data) is not None:
        # The normal authoring review owns applicability for planned cells;
        # this projection must not silently remove reviewed sentences.
        return blocks
    keys = {
        target['id']
        for chapter in blueprint.chapters for target in chapter['targets']
        if target['path'][0] == '_assessment_editorial'
        and target['path'][1].startswith('symptomEvidence.microbiome.')
    }
    if not keys:
        return blocks
    citations = {reference.number: reference for reference in evidence.references} if evidence is not None else {}
    facts = {fact['factId']: fact for fact in blueprint.facts}

    def observational_results(citation):
        reference = citations.get(citation.get('number'))
        if reference is None:
            return []
        quotes = citation.get('quotes') or ()
        description = ' '.join([reference.title or '', *map(str, reference.study_types or ())])
        if not any(_OBSERVATIONAL_DESIGN.search(quote) for quote in quotes) and (
            _EXPERIMENTAL_DESIGN.search(description) or not _OBSERVATIONAL_DESIGN.search(description)
        ):
            return []
        return [
            excerpt
            for quote in quotes for excerpt in re.split(r'(?<=[.!?。！？；;])\s+|\n+', quote)
            if _ASSOCIATION_RESULT.search(excerpt) and not _EXPERIMENTAL_RESULT.search(excerpt)
        ]

    def applicable(sentence):
        from .evaluation_evidence_recall import _term_in

        measured = [facts[fid]['name'] for fid in sentence.get('factIds', ())
                    if fid in facts and facts[fid]['kind'] in {'microbe', 'function'}]
        citations_used = [observational_results(cite) for cite in sentence.get('evidence', ())]
        if not measured or not citations_used or not all(citations_used):
            return False
        return all(
            any(_term_in(excerpt, name, exact=True) for name in measure.split(' / ') for excerpt in excerpts)
            for measure in measured for excerpts in citations_used
        )

    filtered = {}
    for chapter, authored in blocks.items():
        filtered[chapter] = []
        for block in authored:
            if block['targetId'] not in keys:
                filtered[chapter].append(block)
                continue
            sentences = [sentence for sentence in block.get('sentences', ()) if applicable(sentence)]
            if sentences:
                filtered[chapter].append({**block, 'sentences': sentences})
    return filtered


def _symptom_explanations(clinical, microbiome, data, facts, reviewed, habits, clusters, editorial, cell_blocks):
    from .clinical_report import reported_symptom_fragments
    from .evaluation_presentation import reader_text

    symptoms = clinical['symptoms']
    measures = [*clinical['bodyMetrics'], *(row for group in clinical['clinicalGroups'] for row in group['rows'])]
    for item in symptoms:
        existing = item.get('history')
        related = []
        for source in item['sources']:
            fact = facts.get(source['recordId'])
            if fact is None or fact.kind != 'self_reported':
                continue
            question = fact.value.partition('；回答：')[0].removeprefix('问题：')
            timing = re.search(r'(?:留取|采集|检查前|检查后)[^，,。；？?]{0,22}(?:时|期间)(?=[，,。；？?]|$)', question)
            if timing:
                if timing[0] not in (existing or ''):
                    related.append(timing[0])
                related.extend(name for name, _ in reported_symptom_fragments(fact.value, fact.name)
                               if name != item['name'] and name not in (existing or ''))
        labels = set().union(*(labels for pattern, labels in _SYMPTOM_HABITS if re.search(pattern, item['name'])))
        context = _unique(_habit_text(label, answer) for _, label, answer, _ in habits if label in labels)
        item['history'] = '。'.join(_unique(part.strip('。；; ') for part in (
            existing, *related, *context,
        ) if part and part.strip('。；; '))) or None

    assigned = set()
    for index, row in enumerate(data.medical_assessment.questionnaire_summary):
        if not row.explanation:
            continue
        ids = reviewed.get(('medical_assessment', 'questionnaire_summary', index, 'explanation'), set())
        candidates = [item for item in symptoms if any(
            source.get('recordId') in ids and source.get('recordId') not in assigned for source in item['sources']
        )]
        if not candidates:
            continue
        matched = next((item for item in candidates if item['name'] == row.name), None)
        if matched is None and len(candidates) == 1:
            matched = candidates[0]
        if matched is None:
            continue
        text = reader_text(row.explanation, prose=True)
        if text and re.search(r'疲劳|乏力|困倦', matched['name']):
            text = ''.join(sentence for sentence in re.findall(r'[^。！？\n]+[。！？\n]*', text) if not (
                re.search(r'甲状腺(?:彩超|超声|影像)|甲状腺结构', sentence)
                and re.search(r'未见|正常|排除|未提示|不支持', sentence)
            )).strip() or None
        matched['interpretation'] = text
        matched['question'] = f'哪些因素可能与{matched["name"]}有关？'
        matched['evidenceRefs'] = list(row.evidence_refs)
        signals = [
            *matched.get('clinicalSignals', ()),
            *(_signal(fact) for fid, fact in facts.items() if fid in ids and fact.kind == 'clinical'
              and not re.search(r'^主检|体检结论', fact.name)),
            *(row for row in measures if _mentions_signal(text, row)),
        ]
        # Preserve objective evidence already matched to the complaint, even
        # when the narrative mentions only one finding from the same panel.
        matched['clinicalSignals'] = _symptom_signals(matched['name'], signals)[:3]
        matched['microbiomeSignals'] = _microbiome_evidence(ids, facts, row.explanation, row.evidence_refs)
        assigned.update(source.get('recordId') for source in matched['sources'])

    # The two chapters share the reviewed links for each exact symptom.
    for item in symptoms:
        key = 'symptom.' + item['code']
        if not item.get('interpretation'):
            _put_editorial(editorial, key, item, 'interpretation')
            if text := item.get('interpretation'):
                item['question'] = f'如何看待{item["name"]}？'
                ids = reviewed.get(('_assessment_editorial', key, 'text'), set())
                signals = [
                    *item.get('clinicalSignals', ()),
                    *(_signal(fact) for fid, fact in facts.items() if fid in ids and fact.kind == 'clinical'
                      and not re.search(r'^主检|体检结论', fact.name)),
                    *(row for row in measures if _mentions_signal(text, row)),
                ]
                item['clinicalSignals'] = _symptom_signals(item['name'], signals)[:3]
                item['microbiomeSignals'] = _microbiome_evidence(ids, facts, text, item['evidenceRefs'])
        for cluster, anchors in clusters:
            if not anchors.intersection(ref.get('recordId') for ref in item['sources']):
                continue
            shared = cluster.get('microbiomeSignals', ())
            # A shared cluster can group several symptoms. Keep person-specific
            # associations only if its evidence resolves to one actual symptom.
            members = [other for other in symptoms if anchors.intersection(
                ref.get('recordId') for ref in other['sources']
            )]
            if len(members) == 1:
                item['microbiomeSignals'] = _distinct_signals([*item['microbiomeSignals'], *shared])
        objective, gut = _symptom_observations(item, clinical, microbiome)
        clinical_key = 'symptomEvidence.clinical.' + item['code']
        gut_key = 'symptomEvidence.microbiome.' + item['code']
        clinical_review = _reviewed_symptom_column(item, objective, cell_blocks.get(clinical_key, {}))
        assessment = editorial.get(gut_key)
        gut_review = _reviewed_symptom_column(
            item, gut, cell_blocks.get(gut_key, {}),
            refs=assessment.evidence_refs if assessment else [],
        )
        item['clinicalSignals'] = _symptom_signals(item['name'], [*clinical_review, *item['clinicalSignals']])[:3]
        item['microbiomeSignals'] = _distinct_signals([*gut_review, *item['microbiomeSignals']])[:3]
        if not gut_review:
            continue
        owners = {source['recordId'] for source in item['sources']}
        for cluster, anchors in clusters:
            if owners.intersection(anchors) and not any(
                other is not item and anchors.intersection(source['recordId'] for source in other['sources'])
                for other in symptoms
            ):
                cluster['microbiomeSignals'] = _distinct_signals([*gut_review, *cluster['microbiomeSignals']])[:3]


def _lifestyle(data, reviewed, clusters, facts):
    from .clinical_report import reported_fact_kind

    result = []
    ownership = {}
    for _, anchors in clusters:
        for fid in anchors:
            ownership[fid] = ownership.get(fid, 0) + 1
    for index, row in enumerate(data.medical_assessment.trigger_analysis):
        if not row.observations:
            continue
        related = reviewed.get(('medical_assessment', 'trigger_analysis', index, 'association'), set())
        result.append({
            'code': row.code,
            'name': row.name,
            'value': row.observations,
            '_sourceIds': _source_ids(row),
            'interpretation': row.association if related else None,
            'clusterIds': [item['id'] for item, fact_ids in clusters
                           if any(ownership[fid] == 1 for fid in fact_ids.intersection(related))],
            'evidenceRefs': list(row.evidence_refs) if related else [],
            'kind': 'background' if row.code == 'familyHistory' else 'habit',
        })
    for row in data.medical_assessment.questionnaire_summary:
        if row.code in {'medicalHistory', 'medications'} and row.value and row.sources:
            if any(row.value == item['value'] for item in result):
                continue
            result.append({
                'name': row.name, 'value': row.value, 'interpretation': None, 'kind': 'history',
                'clusterIds': [], 'evidenceRefs': [],
            })
    known = {(item['name'], item['value']) for item in result}
    for fact in facts.values():
        if reported_fact_kind(fact) != 'history':
            continue
        signal = _signal(fact)
        identity = (signal['name'], signal['value'])
        if identity not in known:
            result.append({
                'name': signal['name'], 'value': signal['value'], 'interpretation': None, 'kind': 'history',
                'clusterIds': [item['id'] for item, ids in clusters if fact.fact_id in ids], 'evidenceRefs': [],
            })
            known.add(identity)
    return result


def _exercise_scope(data, habits, clusters):
    """Defer exercise-intensity decisions to an established cardiac workup."""
    exercise = [row for row in data.medical_assessment.trigger_analysis if row.code == 'exercise']
    recorded = '；'.join([*(row.observations or '' for row in exercise),
                        *(answer for topic, _, answer, _ in habits if topic == 'exercise')])
    explicit_intensity = bool(re.search(r'中(?:等)?强度|高强度|低强度|轻度有氧', recorded))
    cardiac = next((item for item, _ in clusters if item['priority'] == 'P0'
        and re.search(r'心内科|心血管(?:内科|专科)', item['action'] or '')
        and any(re.search(r'心电图|心超|超声心动', f'{row["name"]} {item["name"]}')
                and not _normal_examination(row['name'], row.get('status'), row.get('value'))
                for row in item['clinicalSignals'] if row['sources'][0]['sourceCode'] == 'A')), None)

    def keep(sentence):
        if not explicit_intensity and re.search(
            r'(?:达到|满足|超过|完成)[^。；\n]{0,20}(?:150\s*分钟|(?:中等|中高)强度[^。；\n]{0,8}推荐量)',
            sentence,
        ):
            return False
        escalating = re.search(
            r'(?:维持|保持|继续|坚持|增加|提高|加强|开展|照常|按原有)[^。；\n]{0,32}'
            r'(?:运动|快走|跑步|训练|有氧|活动强度)',
            sentence,
        )
        return not (cardiac and escalating and not re.search(
            r'(?:评估|复查|明确|许可)(?:前|后|完成)|遵医嘱|不宜|不建议|暂缓|不增加', sentence,
        ))

    def restrict(row, field):
        if text := getattr(row, field):
            cleaned = ''.join(sentence for sentence in re.findall(r'[^。！？；\n]+[。！？；\n]*', text)
                              if keep(sentence)).strip('； \n')
            setattr(row, field, cleaned or None)

    for row in exercise:
        restrict(row, 'association')
    for key, row in data._assessment_editorial.items():
        if key.startswith(('function.', 'organisms.', 'management.', 'layer.')):
            restrict(row, 'text')
    restrict(data.health_overview.overall_summary, 'text')
    restrict(data.comprehensive_conclusion.overall_conclusion, 'text')
    if cardiac and recorded:
        return '运动强度由心内科评估后确定。', cardiac['_actionRefs']
    return None


def _lifestyle_value(text):
    text = re.sub(r'[。、，,]+\s*(?=[；;]|$)', '', text.strip()).lstrip('、，,').strip()
    return re.sub(r'([：:]\s*)(true|false)(?=\s*(?:[。；;\n]|$))',
                  lambda match: match[1] + ('是' if match[2].lower() == 'true' else '否'), text, flags=re.I)


def _template_lifestyle(rows, habits, editorial, *, minor=False):
    """Use only the five lifestyle topics in the assessment example."""
    buckets = {code: {'code': code, 'name': name, 'entries': []} for code, name in _LIFESTYLE_SECTIONS}
    if minor:
        buckets['drinks']['name'] = '饮水与饮品'
    history = []
    habit_sources = {fid: (topic, label, answer) for topic, label, answer, fid in habits}
    for row in rows:
        row = {**row, 'value': _lifestyle_value(row['value'])}
        linked_habits = [habit_sources[fid] for fid in row.pop('_sourceIds', ()) if fid in habit_sources]
        if row['kind'] in {'background', 'history'}:
            history.append(row)
            continue
        code = _TRIGGER_SECTIONS.get(row.get('code'))
        if row.get('code') == 'exposures':
            assigned = {'drinks': [], 'exposures': []}
            for part in re.split(r'[；;\n]+', row['value']):
                part = _lifestyle_value(part)
                if not part:
                    continue
                matching = _unique(habit for habit in linked_habits if habit[2] == part)
                if len(matching) == 1:
                    topic, label, answer = matching[0]
                    if topic in assigned:
                        assigned[topic].append(f'{label}：{answer}')
                        continue
                beverage = re.search(r'饮酒|饮水|水量|白水|茶饮|饮品|咖啡|含糖饮料|酒精', part)
                other = re.search(r'吸烟|烟草|二手烟|烟龄|戒烟|烟雾|精神压力|环境|口腔', part)
                assigned['drinks' if beverage and not other else 'exposures'].append(part)
            mixed = all(assigned.values())
            for target, values in assigned.items():
                if values:
                    item = {**row, 'value': '；'.join(values)}
                    if mixed:
                        item.update(interpretation=None, clusterIds=[], evidenceRefs=[])
                    buckets[target]['entries'].append(item)
            continue
        if code:
            values = [cleaned for value in re.split(r'[；;\n]+', row['value']) if (cleaned := _lifestyle_value(value))]
            if code == 'diet':
                diet_values = []
                for value in values:
                    matching = _unique(habit for habit in linked_habits if habit[2] == value)
                    if len(matching) == 1:
                        topic, label, answer = matching[0]
                        if topic == 'drinks':
                            continue
                        value = f'{label}：{answer}'
                    if any(topic == 'drinks' for topic, _, _, _ in habits) and re.search(
                        r'^(?:每日|每天)?(?:饮水|喝水)|\d\s*毫$', value,
                    ):
                        continue
                    diet_values.append(value)
                values = diet_values
            if values:
                interpretation = row['interpretation']
                if code == 'sleep' and _has_bedtime_without_sleep_onset(habits):
                    values = [_bedtime_observation(value) for value in values]
                    interpretation = _bedtime_observation(interpretation)
                buckets[code]['entries'].append({**row, 'value': '；'.join(values), 'interpretation': interpretation})
    drinks = _unique(_lifestyle_value(f'{label}：{answer}') for topic, label, answer, _ in habits if topic == 'drinks')
    if drinks:
        linked = next((row for row in buckets['drinks']['entries'] if row.get('interpretation')), {})
        drink_row = {
            'value': '；'.join(drinks), 'interpretation': linked.get('interpretation'),
            'evidenceRefs': linked.get('evidenceRefs', []), 'clusterIds': linked.get('clusterIds', []),
        }
        _put_editorial(editorial, 'lifestyle.drinks', drink_row, 'interpretation')
        if drink_row['interpretation']:
            labels = {label for _, label, _, _ in habits}
            if '每日饮水' not in labels:
                drink_row['interpretation'] = _drinks_without_missing_amount(drink_row['interpretation'])
            if '甜食或含糖饮料' in labels and '含糖饮料频率' not in labels and drink_row['interpretation']:
                drink_row['interpretation'] = re.sub(
                    r'(^|[。；，]\s*)含糖饮料(?:的?(?:饮用|摄入))?(?:频率[：:为]?)?\s*'
                    r'(?:每周|每月|每天)\s*[\d一二三四五六七八九十两]+(?:\s*[至~～—−-]\s*[\d一二三四五六七八九十两]+)?'
                    r'\s*(?:天|次)(?:，(?=是|属于|为))?',
                    r'\1含糖饮料', drink_row['interpretation'],
                )
        buckets['drinks']['entries'] = [drink_row]
    return [
        {
            **bucket, 'kind': 'habit',
            'value': '；'.join(_unique(row['value'] for row in bucket['entries'])),
            'interpretation': '\n\n'.join(_unique(
                row['interpretation'] for row in bucket['entries'] if row['interpretation']
            )),
            'clusterIds': _unique(fid for row in bucket['entries'] for fid in row['clusterIds']),
            'evidenceRefs': _unique(ref for row in bucket['entries'] for ref in row['evidenceRefs']),
        }
        for bucket in buckets.values() if bucket['entries']
    ] + history


def _has_bedtime_without_sleep_onset(habits):
    labels = {label for topic, label, _, _ in habits if topic == 'sleep'}
    return '就寝时间' in labels and '实际入睡时间' not in labels


def _bedtime_observation(text):
    if not text:
        return text
    return re.sub(r'入睡(?:的)?(?:时间|时点)(?=[:：]|在|通常在|常在|为|是|偏晚|较晚|后移且)', '就寝时间', text)


def _drinks_without_missing_amount(text):
    """Keep recorded drink habits when the water amount is unavailable."""
    pieces = re.findall(r'[^，,；;。！？\n]+[，,；;。！？\n]*', text)
    kept = [piece for piece in pieces if not (
        re.search(r'饮水|喝水|液体摄入|喝液体', piece)
        and re.search(r'未确认|未知|未填写|未提供|不明|不详|未记录|不估算|无法(?:评估|判断|确认)|尚不能', piece)
    )]
    result = ''.join(kept).strip()
    return (result.rstrip('，,；;') + '。' if result.endswith(('，', ',', '；', ';')) else result) or None


def _input_timing(request, microbiome):
    sample = microbiome.get('sampleContext') or {}
    dated = [(str(row.check_time or '')[:10], row.model_extra or {}) for row in request.hospital_data
             if row.check_time]
    clinical = max((when for when, _ in dated), default=None)
    institutions = _unique(
        name for when, meta in dated if when == clinical
        if (name := meta.get('hospitalName') or meta.get('institutionName') or meta.get('institution'))
    )
    sampling = sample.get('sampleDate')
    try:
        interval = abs((date.fromisoformat(str(sampling)[:10]) - date.fromisoformat(str(clinical))).days)
    except ValueError:
        interval = None
    return {
        'clinicalDate': clinical, 'clinicalInstitution': institutions[0] if len(institutions) == 1 else None,
        'microbiomeSampleDate': sampling, 'microbiomeReceivedDate': sample.get('receivedDate'),
        'microbiomeReportDate': sample.get('reportDate'), 'daysBetween': interval,
    }


def _oral_exam(row):
    return re.search(r'口腔|牙石|牙周|龋齿|牙龈|软垢|智齿', row.get('name') or '') is not None


def _system_rows(code, group_codes, clinical, microbiome):
    if code == 'microbiome':
        return [*microbiome['structure'], *(
            row for group in microbiome['organismGroups']
            if group['code'] in {'beneficial', 'composition'}
            for row in group['rows'] if row.get('status') in {'偏高', '偏低', '异常'}
        )]
    rows = [row for group in clinical['clinicalGroups'] if group['code'] in group_codes for row in group['rows']
            if code != 'endocrineMusculoskeletal' or not _oral_exam(row)]
    if code == 'cardiometabolic':
        rows.extend(clinical['bodyMetrics'])
    return rows


def _mentions_signal(text, row):
    if not text:
        return False
    name = re.split(r'[（(]| / ', str(row.get('name') or ''), 1)[0]
    chinese = re.sub(r'[^\u4e00-\u9fff]', '', name)
    candidates = [chinese, *re.findall(r'[A-Za-z][A-Za-z0-9/-]{1,}', name)]
    candidates.append(re.sub(r'(?:试验|测定|测量|检查)$', '', chinese))
    value = str(row.get('value') or '')
    if re.search(r'超声|彩超|声像|影像', name):
        conclusion = re.split(r'(?:诊断|结论|提示)[：:]', value)[-1]
        candidates.extend(part.strip() for part in re.split(r'[，。、；;\n]', conclusion)
                          if 4 <= len(part.strip()) <= 13)
    return any(len(part) > 1 and part.casefold() in text.casefold() for part in candidates)


def _systems(clinical, microbiome, clusters, editorial, reviewed):
    result = []
    for code, name, group_codes in _SYSTEMS:
        rows = _system_rows(code, group_codes, clinical, microbiome)
        if not rows:
            continue
        row_ids = {ref.get('recordId') for row in rows for ref in row.get('sources', ())}
        priorities = [item['priority'] for item, ids in clusters if ids & row_ids]
        status = None
        if rows:
            if all(_normal_examination(row['name'], row.get('templateStatus') or row.get('status'), row.get('value'))
                   for row in rows):
                status = '已检项目未见异常'
            elif any(item in {'P0', 'P1'} for item in priorities):
                status = '重点关注'
            elif any((row.get('templateStatus') or row.get('status')) in {
                '偏高', '偏低', '异常', '有异常所见', '阳性', '弱阳性',
            } for row in rows):
                status = '亚临床边界'
            else:
                status = '按项目解读'
        key = 'system.' + code
        item = {'code': code, 'name': name, 'status': status, 'summary': None}
        _put_editorial(editorial, key, item)
        cited = reviewed.get(('_assessment_editorial', key, 'text'), set())
        used = [row for row in rows if any(ref['recordId'] in cited for ref in row['sources'])]
        named = [row for row in [*used, *rows] if _mentions_signal(item['summary'], row)]
        if not used:
            used = [row for row in rows if not _normal_examination(
                row['name'], row.get('templateStatus') or row.get('status'), row.get('value'),
            )]
        item['rows'] = _distinct_signals([*named, *(used or rows)])[:6]
        result.append(item)
    return _complete_systems(result, clinical, microbiome)


def _put_editorial(editorial, key, item, field='summary', refs='evidenceRefs'):
    reviewed = editorial.get(key)
    if reviewed and reviewed.text:
        item[field] = reviewed.text
        item[refs] = list(reviewed.evidence_refs)


def _oral_interpretation(text, rows):
    oral = [row for row in rows if _oral_exam(row)]
    if not oral or not text:
        return text
    examined = '。'.join(str(row.get('value') or '') for row in oral)
    text = re.sub(
        r'牙石(?:和|与|及|、)软垢(?=[^。！？]{0,12}(?:不能|无法)[^。！？]{0,12}(?:刷牙|牙刷))', '牙石', text,
    )
    unrecorded = [pattern for pattern in (
        r'牙周袋', r'(?:牙齿|牙)松动', r'根分叉', r'牙周(?:附着|探诊)',
        r'咀嚼(?:功能|能力|效率|受限|困难)',
    ) if not re.search(pattern, examined)]
    if '牙龈受累' not in examined:
        unrecorded.append(r'牙龈[^。；]{0,5}(?:未|无)受累')
    if not unrecorded:
        return text
    sentences = re.findall(r'[^。！？\n]+[。！？\n]*', text)
    result = []
    for sentence in sentences:
        if not any(re.search(pattern, sentence) for pattern in unrecorded):
            result.append(sentence)
            continue
        clauses = re.split(r'[，；,;]', sentence.rstrip('。！？\n'))
        kept = [clause for clause in clauses if not any(re.search(pattern, clause) for pattern in unrecorded)]
        if kept:
            result.append('，'.join(kept).strip() + '。')
    return ''.join(result).strip() or None


def _hepatobiliary_interpretation(text, rows):
    observed = '。'.join(str(row.get('value') or '') for row in rows)
    sentences = re.findall(r'[^。！？\n]+[。！？\n]*', text)
    return ''.join(sentence for sentence in sentences if not (
        re.search(r'不支持|排除|暂无需', sentence)
        and any(name in sentence and name not in observed for name in ('胆汁淤积', '胆道梗阻'))
    )).strip() or None


def _named_edits(editorial, names, prefix):
    result = []
    for code, name in names:
        item = {'code': code, 'name': name}
        _put_editorial(editorial, prefix + code, item, 'text')
        if item.get('text'):
            result.append(item)
    return result


def _synchronize_actions(data, items, *, reviewed=None, facts=None):
    """Project the published clinical action list to both legacy callback lists."""
    from .evaluation_schema import NextStep
    from .evaluation_schema import PriorityDirection
    from .evaluation_schema import SourceReference

    conclusion = data.comprehensive_conclusion
    previous_steps = list(conclusion.next_steps)
    previous_directions = list(conclusion.priority_directions)
    findings = {row.code: row for row in data.medical_assessment.findings if row.code}
    old_basis = {row.name: row.basis for row in conclusion.priority_directions if row.name and row.basis}
    directions, steps = [], []
    for rank, item in enumerate(items, 1):
        members = [findings[code] for code in item['_codes'] if code in findings]
        action = item['action']
        sources = _unique((
            ref.source_code, ref.record_id, ref.observed_at,
        ) for row in members for ref in row.sources)
        sources = _unique([*sources, *(
            (source['sourceCode'], source['recordId'], source.get('observedAt'))
            for signal in [*item['clinicalSignals'], *item['microbiomeSignals']]
            for source in signal['sources']
        )])
        refs = [SourceReference(source_code=code, record_id=record, observed_at=when)
                for code, record, when in sources]
        evidence = list(item['_actionRefs'])
        basis = '；'.join(_unique(old_basis[row.name] for row in members if row.name in old_basis)) or None
        directions.append(PriorityDirection(rank=rank, name=item['name'], basis=basis,
            followup_action=action, sources=refs, evidence_refs=evidence, availability='available'))
        steps.append(NextStep(code=next(iter(item['_codes']), None), name=item['name'],
            description=action, sources=refs, evidence_refs=evidence, availability='available'))
    if _content_plan(data) is not None:
        covered_codes = {code for item in items for code in item['_codes']}
        covered_names = {row.name for code, row in findings.items() if code in covered_codes}
        covered_names.update(item['name'] for item in items)
        for index, row in enumerate(previous_steps):
            path = ('comprehensive_conclusion', 'next_steps', index, 'description')
            if (row.description and row.sources and (reviewed or {}).get(path)
                    and row.code not in covered_codes and row.name not in covered_names):
                steps.append(row)
        for index, row in enumerate(previous_directions):
            path = ('comprehensive_conclusion', 'priority_directions', index, 'followup_action')
            if (row.followup_action and row.sources and (reviewed or {}).get(path)
                    and row.name not in covered_names):
                row.rank = len(directions) + 1
                directions.append(row)
        routine = data._assessment_editorial.get('priority.routine')
        routine_ids = (reviewed or {}).get(('_assessment_editorial', 'priority.routine', 'text'), ())
        if routine and routine.text and routine_ids and facts:
            sources = [SourceReference(
                source_code='A' if fact.kind == 'clinical' else 'B' if fact.kind == 'self_reported' else 'C',
                record_id=fid, observed_at=fact.date,
            ) for fid, fact in facts.items() if fid in routine_ids]
            if not any(row.description == routine.text for row in steps):
                steps.append(NextStep(code='routine', name='常规随访与健康维护', description=routine.text,
                    sources=sources, evidence_refs=list(routine.evidence_refs), availability='available'))
            if not any(row.followup_action == routine.text for row in directions):
                directions.append(PriorityDirection(rank=len(directions) + 1, name='常规随访与健康维护',
                    followup_action=routine.text, sources=sources, evidence_refs=list(routine.evidence_refs),
                    availability='available'))
    conclusion.priority_directions = directions
    conclusion.next_steps = steps


def _title_terms(title):
    """Two-character windows of a finding title, used to put its own rows first."""
    text = re.sub(r'[^\u4e00-\u9fff]', '', str(title or ''))
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _rank_basis(rows, title, limit):
    terms = _title_terms(title)

    def rank(row):
        status = str(row.get('status') or '')
        shown = f'{row.get("name") or ""}{row.get("value") or ""}'
        return (-sum(term in shown for term in terms),
                2 if _NORMAL_STATE.match(status) else 1 if not status else 0)

    return sorted(rows, key=rank)[:limit]


def _basis_rows(identifiers, clinical, *, title=None, limit=4):
    """Displayed clinical rows behind an action card: its own finding, then abnormal results."""
    wanted = set(identifiers or ())
    if not wanted:
        return []
    rows = [*clinical.get('bodyMetrics', ()),
            *(row for group in clinical.get('clinicalGroups', ()) for row in group.get('rows', ())),
            *clinical.get('specialtyFindings', ()),
            *(row for group in clinical.get('digestiveContext', ()) for row in group.get('rows', ()))]
    selected, seen = [], set()
    for row in rows:
        if not any(source.get('recordId') in wanted for source in row.get('sources') or ()):
            continue
        if _NOT_PERFORMED.search(f'{row.get("name") or ""} {row.get("value") or ""}'):
            continue  # an examination not done is no basis for an action
        key = (row.get('name'), str(row.get('value')))
        if key in seen:
            continue
        seen.add(key)
        selected.append({**{field: row.get(field) for field in _BASIS_FIELDS},
                         'sources': [dict(source) for source in row.get('sources') or ()]})

    return _rank_basis(selected, title, limit)


def _signal_basis(item, *, limit=4):
    """Measured anchors of a cluster; recorded advice and self-reports are shown elsewhere."""
    rows = [{**{field: signal.get(field) for field in _BASIS_FIELDS},
             'sources': [dict(source) for source in signal.get('sources') or ()]}
            for signal in item.get('clinicalSignals') or ()
            if signal.get('status') not in {*_ADVICE_STATUSES, _SELF_REPORTED_STATUS}
            and not _NOT_PERFORMED.search(f'{signal.get("name") or ""} {signal.get("value") or ""}')]
    return _rank_basis(rows, item.get('name'), limit)


def _recorded_advice_quotes(item):
    return _unique(str(signal['value']).strip() for signal in item.get('clinicalSignals') or ()
                   if signal.get('status') in _ADVICE_STATUSES and str(signal.get('value') or '').strip())


def _sentence(text):
    text = str(text or '').strip()
    return text if not text or text[-1] in '。！？；' else text + '。'


def _card_text(section, title, *, period=None, department=None, checks=(), quotes=(), reason=None):
    """One deterministic line from the program-decided fields of an action card."""
    if section == 'gaps':
        return f'{title}：{_sentence(reason)}' if reason else f'建议补查{title}。'
    if quotes and not (period or department or checks):
        return f'{title}：' + ''.join(_sentence(quote) for quote in quotes)
    if section == 'urgent':
        visit = ''.join(filter(None, ((period or {}).get('text'), f'至{department}就诊' if department else '')))
        steps = '，'.join(filter(None, (('建议' + visit) if visit else '',
                                        f'完成{"、".join(checks)}' if checks else '')))
        return f'{title}：{_sentence(steps)}' if steps else f'{title}。'
    interval = (period or {}).get('text') or ''
    review = interval if not interval or interval.endswith(('复查', '随访')) else interval + '复查'
    steps = '，'.join(filter(None, (f'{department}随访' if department else '',
                                    f'复查{"、".join(checks)}' if checks else '', review)))
    return f'{title}：{_sentence(steps)}' if steps else f'{title}。'


def _action_card(section, code, title, *, priority=None, period=None, department=None, checks=(),
                 basis=(), quotes=(), reason=None, rule=None, cluster_id=None):
    card = {
        'code': str(code), 'name': title, 'priority': priority,
        'timeframe': period, 'department': department or None, 'checks': list(checks),
        'basisRows': list(basis), 'text': _card_text(section, title, period=period, department=department,
                                                     checks=checks, quotes=quotes, reason=reason),
    }
    if reason:
        card['reason'] = reason
    if rule:
        card['ruleId'] = str(rule)
    if cluster_id:
        card['clusterId'] = cluster_id
    return card


def _urgent_cards(plan, clusters, clinical):
    """P0 cards from the red-flag items; a P0 cluster without an item keeps its own card."""
    cards, claimed = [], []
    for index, row in enumerate(_urgent_plan_items(plan), 1):
        ids = _plan_fact_ids(row, 'factIds')
        basis = [dict(item) for item in row.get('basisRows') or () if isinstance(item, dict)]
        cards.append(_action_card(
            'urgent', row.get('code') or f'urgent{index}', _item_title(row, 'title', 'name'), priority='P0',
            period=_period(row.get('timeframe')), department=str(row.get('department') or '').strip(),
            checks=_plain_strings(row.get('checks')),
            basis=basis or _basis_rows(ids, clinical, title=_item_title(row, 'title', 'name')),
            rule=row.get('ruleId'),
        ))
        claimed.append((set(ids), {str(row.get('code') or ''), str(row.get('ruleId') or '')} - {''}))
    plan_clusters = {row.get('code'): row for row in (plan or {}).get('clusters', ()) if isinstance(row, dict)}
    for item in clusters:
        if item.get('priority') != 'P0':
            continue
        source = plan_clusters.get(item.get('_planCode'), {})
        ids = set(_plan_fact_ids(source, 'factIds'))
        codes = set(_plain_strings(source.get('priorityItemCodes'))) | set(
            _plain_strings(source.get('priorityWorkupCodes')))
        owner = next((card for card, (item_ids, item_codes) in zip(cards, claimed)
                      if ids & item_ids or codes & item_codes), None)
        if owner is not None:
            owner.setdefault('clusterId', item.get('id'))
            continue
        cards.append(_action_card(
            'urgent', item.get('id') or item.get('_planCode') or 'urgent', item['name'], priority='P0',
            basis=_signal_basis(item), quotes=_recorded_advice_quotes(item), cluster_id=item.get('id'),
        ))
    return cards


def _routine_cards(plan, clinical, data, facts, clusters):
    if 'routineFollowups' in (plan or {}):
        return [_action_card(
            'routine', row.get('code') or f'routine{index}', _item_title(row, 'title', 'name'),
            period=_period(row.get('interval') or row.get('timeframe')),
            department=str(row.get('department') or '').strip(), checks=_plain_strings(row.get('checks')),
            basis=_basis_rows(_plan_fact_ids(row, 'factIds'), clinical, title=_item_title(row, 'title', 'name')),
        ) for index, row in enumerate(_plan_list(plan, 'routineFollowups'), 1) if _item_title(row, 'title', 'name')]
    # Without a planner list, recorded follow-up advice outside every cluster
    # remains a routine follow-up with its exact source wording.
    if data is None:
        return []
    clustered = {code for item in clusters for code in item.get('_codes', ())}
    groups = {group.get('code'): group for group in getattr(data, '_template_layout', {}).get('groups', ())}
    cards = []
    for action in _source_clinical_actions(data, facts):
        group = groups.get(action['code'], {})
        if (action['code'] in clustered or group.get('focusLabel') == '优先查因' or not action.get('originalAdvice')):
            continue
        owners = [*group.get('originalFactIds', group.get('sourceFactIds', ())), *action.get('sourceFactIds', ())]
        cards.append(_action_card(
            'routine', f'routine{len(cards) + 1}', action['name'], quotes=action['originalAdvice'],
            basis=_basis_rows(owners, clinical, title=action['name']),
        ))
    return cards


def _gap_cards(plan, clinical):
    return [_action_card(
        'gaps', row.get('code') or f'gap{index}', _item_title(row, 'test', 'title', 'name'),
        reason=str(row.get('reason') or '').strip() or None, rule=row.get('ruleId'),
        basis=_basis_rows(_plan_fact_ids(row, 'triggeredBy', 'factIds'), clinical,
                          title=_item_title(row, 'test', 'title', 'name')),
    ) for index, row in enumerate(_plan_list(plan, 'testGaps'), 1) if _item_title(row, 'test', 'title', 'name')]


def planned_priority_findings(plan, clusters, editorial, clinical, *, data=None, facts=None):
    """1.3: urgent (P0), routine follow-up and test gaps as program cards plus authored sentences."""
    cards = {
        'urgent': _urgent_cards(plan, clusters, clinical),
        'routine': _routine_cards(plan, clinical, data, facts or {}, clusters),
        'gaps': _gap_cards(plan, clinical),
    }
    result = []
    for code, name in _PRIORITY_SECTIONS:
        items = cards[code]
        if code == 'gaps' and not items:
            continue
        section = {'code': code, 'name': name, 'priority': 'P0' if code == 'urgent' and items else None,
                   'text': None, 'evidenceRefs': [], 'items': items}
        if items:
            _put_editorial(editorial, 'priority.' + code, section, 'text')
        if not section['text']:
            section['text'] = ('\n'.join(card['text'] for card in items) if items
                               else _NO_URGENT if code == 'urgent' else _NO_ROUTINE)
        section['action'] = section['text']
        first = next((card for card in items if card.get('timeframe')), {})
        section['timeframe'] = first.get('timeframe')
        section['department'] = first.get('department')
        result.append(section)
    return result


def _cluster_line(item):
    action = str(item.get('action') or '').split('\n\n', 1)[0].strip()
    quotes = [] if action else _recorded_advice_quotes(item)
    label = f'{item.get("name")}（{item["id"]}）' if item.get('id') else str(item.get('name') or '')
    if action:
        return f'{label}：{_sentence(action)}'
    if quotes:
        return f'{label}：' + ''.join(_sentence(quote) for quote in quotes)
    return _sentence(label)


def planned_conclusion_layers(editorial, clusters, findings):
    """6.1: three fixed layers; an authored layer is kept, a missing one is composed from its items."""
    cards = [card for section in findings for card in section.get('items', ())]
    layers = []
    for code, name in _CONCLUSION_LAYERS:
        members = [] if code == 'clinical' else [
            item for item in clusters if item.get('priority') in _LAYER_PRIORITIES[code]
        ]
        layer = {'code': code, 'name': name, 'text': None, 'evidenceRefs': [],
                 'clusterIds': _unique(card['clusterId'] for card in cards if card.get('clusterId'))
                 if code == 'clinical' else [item['id'] for item in members if item.get('id')]}
        _put_editorial(editorial, 'layer.' + code, layer, 'text')
        if not layer['text']:
            lines = [card['text'] for card in cards] if code == 'clinical' else [
                _cluster_line(item) for item in members
            ]
            layer['text'] = '\n'.join(lines) or _LAYER_EMPTY[code]
        layers.append(layer)
    return layers


def planned_management_directions(editorial, *, has_microbiome, has_urgent, has_lifestyle):
    """6.2: MTPN (three parts) and LSMI; a P0 item puts the first-layer workup first."""
    directions = []
    items = _named_edits(editorial, _MICROBIOME_ITEMS, 'management.') if has_microbiome else []
    notes = [_MTPN_AFTER_P0 if has_urgent else '', '' if has_microbiome else _MTPN_NO_MICROBIOME]
    mtpn = {'name': _MTPN_NAME, 'items': items}
    if any(notes):
        mtpn['text'] = ''.join(notes)
    if items or mtpn.get('text'):
        directions.append(mtpn)
    lifestyle = _named_edits(editorial, (('lifestyle', _LSMI_NAME),), 'management.')
    if not lifestyle and not has_lifestyle:
        lifestyle = [{'code': 'lifestyle', 'name': _LSMI_NAME, 'text': _LSMI_NO_FACTORS}]
    directions.extend(lifestyle)
    return directions


def assessment_projection_issues(presentation, blueprint):
    """Distinguish a lost authored cell from a gap in the original material."""
    from .evaluation_presentation import assessment_reader_text

    required = {target['path'][1] for chapter in blueprint.chapters for target in chapter['targets']
                if target.get('required') and target['path'][0] == '_assessment_editorial'}
    rows = {
        'clinicalRow.' + group['code'] + '.' + row['code']: row
        for group in presentation.clinical.get('clinicalGroups', ())
        for row in group.get('templateRows', ())
    }
    omitted_cells = set(getattr(blueprint.data, '_assessment_reader_omitted_cells', ()))
    missing = [key for key in sorted(required) if key.startswith('clinicalRow.')
               and key not in omitted_cells and not rows.get(key, {}).get('summary')]
    if presentation.overview.get('preface') != ASSESSMENT_PREFACE:
        missing.append('overview.preface')
    guide = [(item.get('name'), item.get('text')) for item in presentation.overview.get('readingGuide') or ()]
    if guide != list(ASSESSMENT_READING_GUIDE):
        missing.append('overview.readingGuide')
    directions = presentation.conclusion.get('managementDirections') or ()
    if 'management.lifestyle' in required and not any(
        row.get('code') == 'lifestyle' and row.get('text') for row in directions
    ):
        missing.append('management.lifestyle')
    if (plan := _content_plan(blueprint.data)) is not None:
        if plan.get('status') == 'unavailable':
            missing.append('contentPlan')
        targets = {tuple(target['path']): target for chapter in blueprint.chapters for target in chapter['targets']}
        for key, path, section, typed in (
            ('overviewSummary', ('health_overview', 'overall_summary', 'text'), presentation.overview,
             blueprint.data.health_overview.overall_summary),
            ('conclusionSummary', ('comprehensive_conclusion', 'overall_conclusion', 'text'), presentation.conclusion,
             blueprint.data.comprehensive_conclusion.overall_conclusion),
            ('structureSummary', ('microbiome_assessment', 'structure_interpretation', 'text'),
             {'summary': presentation.microbiome.get('structureSummary')},
             blueprint.data.microbiome_assessment.structure_interpretation),
        ):
            if key == 'structureSummary' and not (
                presentation.microbiome.get('structure') or presentation.microbiome.get('structureGroups')
            ):
                continue
            if not targets.get(path, {}).get('required'):
                missing.append(key + '.target')
            if not typed.text:
                missing.append(key + '.text')
            if not section.get('summary'):
                missing.append(key + '.projection')
        publication_rows = assessment_publication_rows(presentation.clinical, presentation.microbiome)
        for target in targets.values():
            if len(target['path']) != 4 or target['path'][-1] != 'explanation':
                continue
            slots = [slot for slot in target.get('publicationSlots', ()) if slot.endswith('.interpretation')]
            if not target.get('required') or not slots:
                continue
            value = blueprint.data
            for part in target['path']:
                value = value[part] if isinstance(value, (dict, list)) else getattr(value, part)
            # The projection carries the reader wording of the reviewed text.
            shown = {value, assessment_reader_text(value, prose=True)} if isinstance(value, str) else {value}
            if not value or not any(publication_rows.get(slot, {}).get('interpretation') in shown for slot in slots):
                missing.append('.'.join(str(part) for part in target['path']) + '.projection')
        projected = {}
        for key, groups in (
            ('body.', presentation.clinical.get('bodyGroups', ())),
            ('clinical.', presentation.clinical.get('clinicalGroups', ())),
            ('digestive.', presentation.clinical.get('digestiveContext', ())),
            ('structure.', presentation.microbiome.get('structureGroups', ())),
            ('organisms.', presentation.microbiome.get('organismGroups', ())),
            ('function.', presentation.microbiome.get('functionalGroups', ())),
        ):
            projected.update((key + row['code'], row.get('summary')) for row in groups)
        projected.update(('system.' + row['code'], row.get('summary'))
                         for row in presentation.overview.get('systems', ()))
        projected.update(('specialtyRow.' + row['code'], row.get('interpretation'))
                         for row in presentation.clinical.get('specialtyFindings', ()))
        by_name = {row['name']: row for row in presentation.clusters}
        for row in plan.get('clusters', ()):
            item = by_name.get(row['name']) or by_name.get(assessment_reader_text(row['name']), {})
            projected['synthesis.' + row['code']] = item.get('interpretation')
            projected['synthesisAction.' + row['code']] = item.get('action')
        for row in presentation.clinical.get('symptoms', ()):
            projected['symptom.' + row['code']] = row.get('interpretation')
            for kind, signals in (('clinical', 'clinicalSignals'), ('microbiome', 'microbiomeSignals')):
                projected['symptomEvidence.' + row['code'] + '.' + kind] = any(
                    signal.get('interpretation') for signal in row.get(signals, ())
                )
        projected.update(('lifestyle.' + row['code'], row.get('interpretation'))
                         for row in presentation.lifestyle_factors if row.get('code'))
        projected.update(('recognition.' + row['code'], row.get('text'))
                         for row in presentation.lifestyle_recognition)
        projected.update(('layer.' + row['code'], row.get('text'))
                         for row in presentation.conclusion.get('layers', ()))
        for key, value in (
            ('bodySummary', presentation.overview.get('bodySummary')),
            ('clinicalLead', presentation.clinical.get('summary')),
            ('graphSummary', presentation.overview.get('graphSummary')),
            ('clusterSummary', presentation.cluster_summary),
            ('lifestyleSummary', presentation.lifestyle_summary),
            ('symptomSummary', presentation.clinical.get('symptomSummary')),
            ('specialtySummary', presentation.clinical.get('specialtySummary')),
            ('trendSummary', presentation.clinical.get('trendSummary')),
            ('baselineSummary', presentation.clinical.get('baselineSummary')),
            ('microbiomeLead', presentation.microbiome.get('overviewSummary')),
            ('sampleSummary', presentation.microbiome.get('sampleSummary')),
            ('riskSummary', presentation.microbiome.get('riskSummary')),
            ('fermentationSummary', presentation.microbiome.get('fermentationSummary')),
        ):
            projected[key] = value
        for direction in directions:
            if direction.get('code'):
                projected['management.' + direction['code']] = direction.get('text')
            for item in direction.get('items', ()):
                projected['management.' + item['code']] = item.get('text')
        projected.update(('priority.' + row['code'], row.get('action'))
                         for row in presentation.overview.get('priorityFindings', ()) if row.get('code'))
        planned_prefixes = ('system.', 'body.', 'clinical.', 'specialtyRow.', 'digestive.', 'structure.', 'organisms.',
                            'function.',
                            'synthesis.', 'synthesisAction.', 'symptom.', 'symptomEvidence.', 'lifestyle.',
                            'recognition.', 'layer.', 'management.', 'priority.')
        missing.extend(key for key in sorted(required)
                       if key not in omitted_cells and (key in projected or key.startswith(planned_prefixes))
                       and not projected.get(key))
    return _unique(missing)


def build_assessment_presentation(*, request, parsed, document, data, blueprint, accepted):
    from .assessment_clinical_view import build_clinical_view
    from .assessment_microbiome_view import annotate_microbiome_chart_fields
    from .assessment_microbiome_view import build_microbiome_view

    clinical = build_clinical_view(request, parsed, document, data, reader_plan=True)
    data._assessment_reader_omitted_cells = list(clinical.pop('_readerOmittedCells', ()))
    blueprint.data._assessment_reader_omitted_cells = data._assessment_reader_omitted_cells
    microbiome = annotate_microbiome_chart_fields(build_microbiome_view(parsed, data, source_facts=document.facts))
    editorial = data._assessment_editorial
    reviewed = _reviewed_facts(blueprint, accepted)
    synthesis_targets = {
        target['id']: target['path'][1]
        for chapter in blueprint.chapters for target in chapter['targets']
        if target['path'][0] == '_assessment_editorial'
        and target['path'][1].startswith(('synthesis.', 'synthesisAction.'))
    }
    synthesis_blocks = {synthesis_targets[block['targetId']]: block for block in accepted
                        if block['targetId'] in synthesis_targets}
    symptom_targets = {
        target['id']: target['path'][1]
        for chapter in blueprint.chapters for target in chapter['targets']
        if target['path'][0] == '_assessment_editorial' and target['path'][1].startswith('symptomEvidence.')
    }
    symptom_blocks = {symptom_targets[block['targetId']]: block for block in accepted
                      if block['targetId'] in symptom_targets and block.get('completion') == 'complete'}
    facts = {fact.fact_id: fact for fact in document.facts}
    plan = _content_plan(data)
    display_index = _display_source_index(
        clinical, microbiome, facts,
        reported_roles=getattr(document, '_clinical_reported_source_roles', {}),
        document=document, data=data,
    ) if plan is not None else None
    if plan is not None and plan.get('status') == 'unavailable':
        editorial = {}
    habits = _questionnaire_habits(facts)
    clusters = _assessment_clusters(data, facts, reviewed, subject=parsed.subject, document=document,
                                    display_index=display_index, reviewed_blocks=synthesis_blocks)
    exercise_scope = _exercise_scope(data, habits, clusters) if plan is None else None
    if plan is None:
        _symptom_explanations(clinical, microbiome, data, facts, reviewed, habits, clusters, editorial, symptom_blocks)
    else:
        _planned_symptoms(clinical, data, facts, reviewed, plan, display_index=display_index)
    clinical['symptomsLead'] = None
    clinical['summary'] = None
    clinical['evidenceRefs'] = []
    if clinical['symptoms']:
        names = _unique(row['name'] for row in clinical['symptoms'] if row.get('name'))
        clinical['symptomsLead'] = '本次记录的主诉包括' + '、'.join(names) + '。' if names else None
    if plan is None and clinical['clinicalGroups']:
        clinical['summary'] = (
            '下文按系统对照实际检查结果、原参考范围和有依据的临床关注界值，说明不同发现对后续评估的影响。'
        )
    _put_editorial(editorial, 'clinicalLead', clinical)
    clinical.setdefault('symptomSummary', None)
    clinical.setdefault('symptomSummaryEvidenceRefs', [])
    _put_editorial(editorial, 'symptomSummary', clinical, 'symptomSummary', 'symptomSummaryEvidenceRefs')
    for field in ('specialtySummary', 'trendSummary', 'baselineSummary'):
        clinical.setdefault(field, None)
        clinical.setdefault(field + 'EvidenceRefs', [])
        _put_editorial(editorial, field, clinical, field, field + 'EvidenceRefs')
    if not clinical.get('trends') and not clinical.get('trendSummary'):
        # Spec 3.6: without comparable earlier results the callback still says so.
        clinical['trendSummary'] = NO_TREND_STATEMENT
    for row in clinical['specialtyFindings']:
        _put_editorial(editorial, 'specialtyRow.' + row['code'], row, field='interpretation')
        device_plan = row.pop('_deviceFollowup', None)
        if device_plan and device_plan not in (row.get('interpretation') or ''):
            row['interpretation'] = '\n'.join(filter(None, (
                row.get('interpretation'), f'设备建议：{device_plan}',
            )))
    for field in ('sampleSummary', 'fermentationSummary', 'riskSummary'):
        microbiome.setdefault(field, None)
        microbiome.setdefault(field + 'EvidenceRefs', [])
    _put_editorial(editorial, 'sampleSummary', microbiome, 'sampleSummary', 'sampleSummaryEvidenceRefs')
    _put_editorial(editorial, 'microbiomeLead', microbiome, 'overviewSummary', 'overviewSummaryEvidenceRefs')
    _put_editorial(editorial, 'fermentationSummary', microbiome,
                   'fermentationSummary', 'fermentationSummaryEvidenceRefs')
    _put_editorial(editorial, 'riskSummary', microbiome, 'riskSummary', 'riskSummaryEvidenceRefs')
    if model_results := _reported_model_summary(parsed):
        microbiome['riskSummary'] = '\n\n'.join(filter(None, (microbiome.get('riskSummary'), model_results)))
    for groups, prefix in (
        (clinical['bodyGroups'], 'body.'),
        (clinical['clinicalGroups'], 'clinical.'),
        (clinical['digestiveContext'], 'digestive.'),
        (microbiome.get('structureGroups', ()), 'structure.'),
        (microbiome.get('organismGroups', ()), 'organisms.'),
        (microbiome['functionalGroups'], 'function.'),
    ):
        for group in groups:
            source_summary = group.pop('_sourceSummary', False)
            group.pop('_sourcePanelCounts', None)
            if plan is not None:
                group['summary'] = None
                group['evidenceRefs'] = []
            if plan is not None or prefix in {'organisms.', 'function.'} or (
                not source_summary and (prefix != 'structure.' or not group.get('summary'))
            ):
                _put_editorial(editorial, prefix + group['code'], group)
    for group in clinical['clinicalGroups']:
        for row in group.get('templateRows', ()):
            _put_editorial(editorial, 'clinicalRow.' + group['code'] + '.' + row['code'], row)
    core_clusters = list(clusters) if plan is not None else [
        entry for entry in clusters if entry[0]['priority'] != 'P3' and (
            entry[0]['priority'] == 'P0' or entry[0]['interpretation'] or entry[0]['action']
        )
    ][:6]
    items = [item for item, _ in core_clusters]
    main_actions = [item for item in items if item['action']]
    selected = {id(item) for item in items}
    routine = [item for item, _ in clusters if id(item) not in selected and item['action'] and (
        plan is not None or (_clinical_followup(item['action']) and any(
            ref['sourceCode'] == 'A' for signal in item['clinicalSignals'] for ref in signal['sources']
        ))
    )]
    published = [*main_actions, *routine]
    for index, item in enumerate([*items, *routine], 1):
        item['id'] = f'C{index}'
    priorities = [
        {
            **{key: item[key] for key in ('id', 'name', 'priority', 'action')},
            'evidenceRefs': item['_actionRefs'],
        }
        for item in ([*items, *routine] if plan is not None else published)
        if item['action'] or any(signal.get('status') in _ADVICE_STATUSES for signal in item['clinicalSignals'])
    ]
    _synchronize_actions(data, published, reviewed=reviewed, facts=facts)
    urgent = [item for item in priorities if item['priority'] == 'P0']
    overview = {
        'summary': data.health_overview.overall_summary.text,
        'evidenceRefs': list(data.health_overview.overall_summary.evidence_refs),
        'bodySummary': None,
        'bodySummaryEvidenceRefs': [],
        'graphSummary': None,
        'graphSummaryEvidenceRefs': [],
        'graphHub': None,
        'systems': _systems(clinical, microbiome, clusters, editorial, reviewed) if plan is None else
        _planned_systems(clinical, microbiome, clusters, data, facts, reviewed, plan,
                         display_index=display_index),
        'readingGuide': [{'name': name, 'text': text} for name, text in ASSESSMENT_READING_GUIDE],
        'preface': ASSESSMENT_PREFACE,
        'dataTiming': _input_timing(request, microbiome),
        'priorityFindings': [],
    }
    _put_editorial(editorial, 'bodySummary', overview, 'bodySummary', 'bodySummaryEvidenceRefs')
    if items:
        _put_editorial(editorial, 'graphSummary', overview, 'graphSummary', 'graphSummaryEvidenceRefs')
    overview['graphClusterIds'] = [item['id'] for item in items
                                   if item['microbiomeSignals'] or (plan is not None and item['clinicalSignals'])]
    if overview['graphClusterIds']:
        overview['graphHub'] = '核心健康主题' if plan is not None else '肠道微生态线索'
    if plan is not None:
        # Program cards decide the items; an authored sentence that did not
        # pass review never removes them (a withheld P0 cell used to drop the
        # whole urgent part).
        # An unavailable plan decided nothing; absence sentences would be false.
        overview['priorityFindings'] = planned_priority_findings(
            plan, items, editorial, clinical, data=data, facts=facts,
        ) if plan.get('status') != 'unavailable' else []
        if plan.get('status') == 'reviewed' and not items:
            overview['graphSummary'] = overview['graphSummary'] or _NO_GRAPH
    else:
        overview['priorityFindings'].extend({
            **item, 'code': 'urgent', 'action': item['action'].split('\n\n', 1)[0],
        } for item in urgent)
        for code, title, levels in (
            ('planned', '重点管理与近期复评', {'P1', 'P2'}),
            ('routine', '常规随访与健康维护', {'P3'}),
        ):
            names = [item['name'] for item in published if item['priority'] in levels]
            if names:
                overview['priorityFindings'].append({
                    'code': code, 'name': title,
                    'action': '、'.join(names[:3]) + ('等' if len(names) > 3 else '') + '。具体安排见第六章。',
                })
        for prompt in _named_edits(editorial, (('gaps', '关键常规检测盲区提示'),), 'priority.'):
            if not re.match(
            r'^(?:未发现|没有|无|暂未发现|暂无)(?:需要|需|其他|尚|新的|独立|明确|单独|\s|。)', prompt['text'],
            ):
                overview['priorityFindings'].append({**prompt, 'action': prompt['text']})
    directions = _named_edits(editorial, (('lifestyle', _LSMI_NAME),), 'management.')
    if exercise_scope:
        text, refs = exercise_scope
        if directions:
            direction = directions[0]
            direction['text'] = '\n\n'.join(_unique((text, direction['text'])))
            direction['evidenceRefs'] = _unique([*refs, *direction.get('evidenceRefs', ())])
        else:
            directions.append({
                'code': 'lifestyle', 'name': _LSMI_NAME,
                'text': text, 'evidenceRefs': refs,
            })
    micro_directions = []
    if _has_microbiome_findings(microbiome):
        micro_directions = _named_edits(editorial, _MICROBIOME_ITEMS, 'management.')
    if micro_directions:
        directions.insert(0, {'name': _MTPN_NAME, 'items': micro_directions})
    if plan is not None and plan.get('status') != 'unavailable':
        directions = planned_management_directions(
            editorial, has_microbiome=_has_microbiome_findings(microbiome),
            has_urgent=any(section['code'] == 'urgent' and section['items']
                           for section in overview['priorityFindings']),
            has_lifestyle=bool(_plan_list(plan, 'lifestyleFactors')),
        )
    lifestyle = {}
    _put_editorial(editorial, 'lifestyleSummary', lifestyle)
    cluster_summary = {}
    if items:
        _put_editorial(editorial, 'clusterSummary', cluster_summary)
    elif plan is not None and plan.get('status') == 'reviewed':
        cluster_summary['summary'] = _NO_CLUSTERS
    layers = ([] if plan is None or plan.get('status') == 'unavailable'
              else planned_conclusion_layers(editorial, items, overview['priorityFindings']))
    if plan is None:
        layers = _named_edits(editorial, _CONCLUSION_LAYERS[1:], 'layer.')
    clinical_layers = [] if plan is not None else [{
        'code': code, 'name': name,
        'text': '\n\n'.join(_unique(item['action'] for item in action_items)),
        'evidenceRefs': _unique(ref for item in action_items for ref in item['_actionRefs']),
    } for code, name, action_items in (
        ('clinical', _CONCLUSION_LAYERS[0][1], main_actions),
        ('routine', '其他已确认的复查与随访', routine),
    ) if action_items]
    age = str(getattr(parsed.subject, 'age', '') or '').strip()
    minor = age.isdecimal() and int(age) < 18
    _strip_display_metadata(clinical)
    _strip_display_metadata(microbiome)
    _strip_display_metadata(overview)
    presentation = AssessmentPresentation(
        overview=overview,
        clinical=clinical,
        microbiome=microbiome,
        clusters=[{key: value for key, value in item.items() if not key.startswith('_')} for item in items],
        clusterSummary=cluster_summary.get('summary'),
        clusterSummaryEvidenceRefs=cluster_summary.get('evidenceRefs', []),
        lifestyleFactors=_template_lifestyle(
            _lifestyle(data, reviewed, core_clusters, facts), habits, editorial, minor=minor,
        ) if plan is None else _planned_lifestyle(data, facts, reviewed, clusters, plan,
                                                display_index=display_index),
        lifestyleSummary=lifestyle.get('summary'),
        lifestyleSummaryEvidenceRefs=lifestyle.get('evidenceRefs', []),
        lifestyleRecognition=_named_edits(editorial, tuple(
            (row['code'], row['name']) for row in plan.get('recognitions', ())
        ) if plan is not None else (
            ('management', '已有健康管理做法'),
            ('awareness', '检查结果与个人认知'),
            ('habit', '日常健康习惯'),
        ), 'recognition.'),
        conclusion={
            'summary': data.comprehensive_conclusion.overall_conclusion.text,
            'evidenceRefs': list(data.comprehensive_conclusion.overall_conclusion.evidence_refs),
            'priorities': priorities,
            'layers': [*clinical_layers, *layers],
            'managementDirections': directions,
        },
    )
    # Formal written wording for every displayed string (values, units, dates
    # and source references stay as recorded). No model call is involved.
    from .evaluation_presentation import present_assessment_for_reader

    present_assessment_for_reader(presentation)
    return presentation
