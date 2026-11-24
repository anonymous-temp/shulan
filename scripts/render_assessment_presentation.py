"""Render the reviewed assessment presentation in six reader-facing chapters."""

from __future__ import annotations

import re
from collections import Counter
from html import escape
from math import ulp
from unicodedata import normalize
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit

from modules.report.assessment_callback_view import ASSESSMENT_SECTIONS
from modules.report.assessment_microbiome_view import _source_key
from modules.report.assessment_microbiome_view import first_reported_abundance_assay
from modules.report.assessment_microbiome_view import organism_assessment_display_group
from modules.report.assessment_microbiome_view import source_reference_display
from modules.report.assessment_reading_labels import clinical_observation_display_name
from modules.report.evaluation_template_input import _directions

ASSESSMENT_CSS = """
.assessment-shell{max-width:960px;margin:0 auto;padding:24px}
.assessment-shell .assessment{
  --ink:#171717;--muted:#555;--line:#e4e3df;--grid:#ecebe8;--wash:#f4f3f0;
  --accent:#254a69;--accent-wash:#eef4f8;
  color:var(--ink);font-family:"Songti SC",SimSun,"Noto Serif CJK SC",serif;line-height:1.62;
  margin:0 auto;padding:52px 64px;
}
.assessment header.cover{margin:0;padding-bottom:16px;border-bottom:1px solid var(--line)}
.assessment header.cover h1{margin:0 0 14px;font-weight:700;letter-spacing:0;text-wrap:balance}
.assessment dl.idcard{
  display:block;padding:0 0 0 13px;border-left:2px solid var(--ink);font-size:inherit;
}
.assessment dl.idcard div{display:inline;margin-right:.5em}
.assessment dl.idcard .dates:not(.dates + .dates)::before{content:'';display:block;margin-top:4px}
.assessment dl.idcard .identity+.identity::before,.assessment dl.idcard .dates+.dates::before{
  content:'｜';margin-right:.4em
}
.assessment dl.idcard .timing{display:block;margin-top:4px}
.assessment dl.idcard dt,.assessment dl.idcard dd{display:inline;margin:0}
.assessment dl.idcard dt{color:inherit;font-weight:700}
.assessment dl.idcard dt::after{content:'：'}
.assessment dl.idcard dd{font-weight:400}
.assessment section.chapter{padding-top:22px}
.assessment section.chapter+section.chapter{border-top:1px solid var(--line);margin-top:24px}
.assessment h2{border-bottom:0;padding-bottom:0;margin-bottom:10px;font-size:23px}
.assessment h3{margin:24px 0 10px;font-size:18px}
.assessment h4{font-size:16px}
.assessment table.tbl th{white-space:normal}
.assessment table.tbl th,.assessment table.tbl td{border:1px solid var(--grid);padding:7px 9px}
.assessment table.tbl th{background:var(--wash);color:inherit}
.assessment table.tbl td{overflow-wrap:anywhere}
.assessment table.tbl ul{margin:0;padding-left:1.2em}
.assessment .as-cell-note{color:var(--muted);font-size:.88em}
.assessment .as-item-references{color:var(--muted);font-size:.88em;margin:3px 0 0;break-inside:avoid}
.assessment .as-item-references .cite{margin-left:.25em}
.assessment .as-reference-tail{break-inside:avoid-page}
.assessment .as-partial-note{padding:8px 12px;background:var(--wash);border-left:2px solid var(--accent)}
.assessment .as-source-time{display:block;margin:4px 0 2px;color:var(--muted);font-size:.88em}
.assessment table.tbl .as-result{font-variant-numeric:tabular-nums}
.assessment .as-callout{margin:14px 0;padding:0 0 0 14px;border-left:2px solid var(--ink)}
.assessment .as-callout h4,.assessment .as-callout p:last-child{margin-bottom:0}
.assessment .as-callout ol,.assessment .as-callout ul{padding-left:1.2em}
.assessment .as-callout li+li{margin-top:9px}
.assessment .as-graph{margin:14px 0;display:grid;align-items:center;gap:18px;
  grid-template-columns:minmax(0,1fr) minmax(0,3fr)}
.assessment .as-graph>p{position:relative;margin:0;font-weight:650;text-align:center;
  background:var(--accent-wash);padding:8px;overflow-wrap:anywhere}
.assessment .as-graph>p::after{content:'';position:absolute;left:100%;top:50%;width:18px;
  border-top:1px solid var(--line)}
.assessment .as-graph .as-root{background:#164867;color:white}
.assessment .as-graph .as-branches{position:relative;display:grid;gap:5px;margin:0;padding:0}
.assessment .as-graph .as-branches::before{content:'';position:absolute;left:-9px;top:12px;bottom:12px;
  border-left:1px solid var(--line)}
.assessment .as-graph .as-branch{position:relative;list-style:none;margin:0;padding:0}
.assessment .as-branch::before{content:'';position:absolute;left:-9px;top:50%;width:9px;
  border-top:1px solid var(--line)}
.assessment .as-graph a{display:block;padding:5px 8px;background:#fff7dc;border:1px solid #997529;
  color:var(--ink);text-decoration:none;overflow-wrap:anywhere}
.assessment .as-graph .as-branch[data-priority="P0"] a{background:#fbe9e9;border-color:#ae5151}
.assessment .as-graph .as-branch[data-priority="P1"] a{background:#fbefdf;border-color:#a36a2b}
.assessment .as-cluster .facts{margin:5px 0;padding-left:1.2em}
.assessment .as-reading li+li,.assessment .as-followup li+li{margin-top:10px}
.assessment .as-score{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:2px;
  width:min(7em,100%);margin:4px 0}
.assessment .as-score i{height:5px;border-radius:2px;background:var(--grid)}
.assessment .as-score i.on{background:var(--accent)}
.assessment .as-cluster{margin:18px 0}
.assessment .as-cluster h4{margin:0 0 8px}
.assessment a.as-cluster-ref{overflow-wrap:anywhere}
.assessment table.as-systems td p{margin:0 0 8px}
.assessment table.as-systems td p:last-child{margin-bottom:0}
.assessment .as-references{margin-top:28px;padding-top:20px;border-top:1px solid var(--line)}
.assessment .as-reference-list{padding-left:2.2em;font-size:.92em}
.assessment .as-reference-list li{margin:0 0 10px;overflow-wrap:anywhere}
.assessment .as-reference-list .as-original{white-space:nowrap}
@media screen and (min-width:721px),print{
  .assessment table.as-systems,.assessment table.as-observations,.assessment table.as-digestive,
  .assessment table.as-symptoms,.assessment table.as-lifestyle{table-layout:fixed}
  .assessment table.as-systems th:nth-child(1){width:19%}
  .assessment table.as-systems th:nth-child(2){width:13%}
  .assessment table.as-systems th:nth-child(3){width:30%}
  .assessment table.as-observations th:nth-child(1){width:21%}
  .assessment table.as-observations th:nth-child(2){width:16%}
  .assessment table.as-observations th:nth-child(3){width:15%}
  .assessment table.as-observations th:nth-child(4){width:12%}
  .assessment table.as-observations th:last-child{width:auto}
  .assessment table.as-clinical th:nth-child(4),.assessment table.as-body th:nth-child(4){width:15%}
  .assessment table.as-microbiome-group th:nth-child(1){width:17%}
  .assessment table.as-microbiome-group th:nth-child(2){width:31%}
  .assessment table.as-microbiome-group th:nth-child(3){width:20%}
  .assessment table.as-microbiome-group th:nth-child(4){width:auto}
  .assessment table.as-digestive th:nth-child(1){width:23%}
  .assessment table.as-digestive th:nth-child(2){width:43%}
  .assessment table.as-symptoms th:nth-child(1){width:20%}
  .assessment table.as-symptoms th:nth-child(2){width:24%}
  .assessment table.as-symptoms th:nth-child(3){width:26%}
  .assessment table.as-lifestyle th:nth-child(1){width:18%}
  .assessment table.as-lifestyle th:nth-child(2){width:26%}
}
@media screen and (max-width:720px){
  .assessment-shell{padding:0}
  .assessment-shell .assessment{padding:24px 14px}
  .assessment dl.idcard div{display:block}
  .assessment dl.idcard div::before{display:none}
  .assessment table.tbl td.as-empty{display:none}
  .assessment .as-graph{grid-template-columns:minmax(0,1fr);gap:6px}
  .assessment .as-graph>p::after,.assessment .as-graph .as-branches::before,
  .assessment .as-branch::before{display:none}
}
@media print{
  .assessment-shell{max-width:none;padding:0;margin:0}
  .assessment-shell .assessment{padding:0}
  .assessment section.chapter{padding-top:13px}
  .assessment section.chapter+section.chapter{margin-top:15px}
  .assessment h2,.assessment h3,.assessment h4{break-after:avoid-page}
  .assessment .as-cluster h4{break-inside:avoid-page}
  .assessment h3+table,.assessment .as-boundary{break-before:avoid-page}
  .assessment p,.assessment li{orphans:2;widows:2}
  .assessment .as-graph{break-inside:avoid-page;margin:8px 0;gap:13px}
  .assessment .as-graph a{padding:3px 6px}
  .assessment .as-graph .as-branches{gap:3px}
  .assessment .as-score i,.assessment .chip,.assessment .as-graph p,.assessment .as-graph a{
    print-color-adjust:exact;-webkit-print-color-adjust:exact}
  .assessment table.tbl thead{display:table-header-group;break-after:avoid-page;break-inside:avoid-page}
  .assessment ul.facts li{break-inside:auto}
  .assessment table.tbl tbody tr{break-inside:avoid-page}
  .assessment table.as-flow tbody tr,.assessment table.as-lifestyle tbody tr{
    break-inside:auto;page-break-inside:auto}
  .assessment table.as-flow ul.facts li{break-inside:avoid-page}
  .assessment table.as-flow .as-reference-tail{break-inside:auto}
  .assessment table.as-flow .as-item-references{break-before:avoid-page}
  .assessment table.as-clinical td:nth-child(4),.assessment table.as-lifestyle td:nth-child(-n+2){
    overflow-wrap:normal}
  .assessment .as-callout>p:first-child,.assessment .as-function-intro{break-after:avoid-page}
  .assessment .as-callout li>strong+ul{break-before:avoid-page}
  .assessment .as-callout li>strong{display:block;break-after:avoid-page}
  .assessment .as-callout li>strong+p{break-before:avoid-page}
  .assessment .as-cluster-subheading{display:block;break-after:avoid-page}
  .assessment .as-cluster-subheading+p{break-before:avoid-page;orphans:2}
  .assessment table.tbl th,.assessment table.tbl td{padding:4px 6px}
  .assessment table.as-observations:not(.as-microbiome-group) th:nth-child(2){width:21%}
  .assessment table.as-observations:not(.as-microbiome-group) th:nth-child(4){width:10%}
  .assessment table.as-observations.as-body th:nth-child(4),
  .assessment table.as-observations.as-clinical th:nth-child(4){width:20%}
  .assessment table.as-clinical th:nth-child(1){width:16%}
  .assessment table.as-clinical th:nth-child(3){width:17%}
  .assessment .as-graph-note{break-after:avoid-page}
  .assessment .as-directions{break-after:avoid-page}
  .assessment .as-directions li>strong{display:block;break-after:avoid-page}
  .assessment .as-followup li>strong{display:block;break-after:avoid-page}
  .assessment .as-followup li>strong+p{break-before:avoid-page}
  .assessment .as-followup-heading{break-after:avoid-page}
  .assessment .as-followup-heading+p,.assessment .as-followup-heading+ul{break-before:avoid-page}
  .assessment .as-references>p{break-after:avoid-page}
  .assessment .as-reference-list li{break-inside:avoid-page}
}
"""

_PREFACE_EMPHASIS = re.compile(r'(第一章|第二章|第三章与第四章|第五章与第六章)')
_PRIORITIES = {
    'P0': ('优先医疗排雷', 'critical'),
    'P1': ('核心重塑', 'serious'),
    'P2': ('协同调理', 'warn'),
    'P3': ('随访巩固', 'neutral'),
}
_SYSTEM_STATUS_CLASS = {
    '重点关注': 'serious',
    '需要关注': 'warn',
    '亚临床边界': 'warn',
    '稳健达标': 'good',
    '受检项目在参考范围内': 'good',
}
_BODY_HEADERS = ('评估指标', '实测数值', '医学参考基准 / 理想目标', '状态定性', '生理与代谢意义校准')
_SYSTEM_HEADERS = (
    '核心生理系统维度',
    '综合状态分级',
    '核心客观异常与亚临床哨兵锚点（实测摘要）',
    '当前阶段核心评估定论',
)
_SYMPTOM_HEADERS = (
    '主诉编号与核心困扰表型',
    '持续周期与日常加重场景（客户自述特征）',
    '临床检验与影像学关联信号',
    '肠道微生态与功能代谢关联证据',
)
_CLINICAL_SECTIONS = (
    (
        'cardiometabolic',
        '3.1 糖脂代谢、胰岛素敏感性与心血管系统评估',
        (
            '检验 / 影像专项名称',
            '您的实测结果',
            '临床参考范围（功能理想目标）',
            '双轨状态',
            '临床心血管风险与亚临床代谢深度解读',
        ),
    ),
    (
        'organMetabolism',
        '3.2 肝胆胰消化、肾脏代谢与营养辅酶状态评估',
        (
            '检验 / 影像专项名称',
            '您的实测结果',
            '临床参考范围（功能关注阈值）',
            '双轨状态',
            '消化吸收动力学与辅酶哨兵深度解读',
        ),
    ),
    (
        'hematologyImmune',
        '3.3 血液学、铁代谢储备与一碳甲基化通路评估',
        (
            '检验项目名称',
            '您的实测结果',
            '临床参考范围（功能理想目标）',
            '双轨状态',
            '氧运输储备、甲基化效率与免疫稳态解读',
        ),
    ),
    (
        'endocrineMusculoskeletal',
        '3.4 内分泌激素、骨骼肌肉与器官影像学评估',
        (
            '评估维度 / 检查项目',
            '您的实测结果与影像学分级',
            '临床参考基准（功能关注标准）',
            '双轨状态',
            '内分泌稳态、结节随访与运动结构边界解读',
        ),
    ),
)
_SPECIALTY_HEADERS = ('专项评估类别 / 检测指标', '您的实测结果 / 基因型', '参考基准 / 理想区间',
                      '状态评价', '个体化功能代谢与临床交叉印证解读')
_TREND_HEADERS = ('追踪指标 / 影像病灶', '既往检测状态', '本次检测状态', '演变方向', '变化的医学评估意义')
_DIGESTIVE_HEADERS = ('消化道生态位环节', '关联客观检查指标与状态', '对微生态定植及营养消化吸收的生理影响解析')
_ECOLOGY_HEADERS = ('宏观微生态评估维度', '您的检测结果', '健康参考基准', '状态评价', '生态学稳态与慢病关联深度解读')
_ORGANISM_HEADERS = (
    '功能菌群与病原谱分类',
    '原报告面板状态 / 菌种丰度 / 基因序列（含丰度方向 ↓ / ↑）',
    '已确认正常基线范围',
    '生理功能影响与靶向调节意义',
)
_PATHWAY_HEADERS = (
    '菌群代谢通路维度',
    '关键异常子项与可视化潜能标尺',
    '通路内其余正常子项',
    '与临床体检指标及躯体症状的跨维度交叉印证',
)
_LIFESTYLE_HEADERS = ('生活方式诱因维度', '您的日常行为特征与场景约束', '相关异常与健康问题')
_DASH = '<span class="muted">—</span>'
_NO_SOURCE_REFERENCE = '原件未列参考'
_NO_SOURCE_JUDGMENT = '原件未列判定'
_NO_INTERPRETATION = '未形成解读'
_PATHWAY_DEVIATION = re.compile(r'↑+|↓+|异常|偏高|偏低|升高|降低|超出参考范围')
_CLINICAL_CONCLUSION = re.compile(
    r'(?:^|(?<=[。；;\n]))\s*(?:'
    r'(?:检查结论|影像诊断|影像印象|诊断意见|结论)(?:\s*[：:]\s*|\s+|(?=[\u4e00-\u9fff]))'
    r'|(?:超声提示|提示)\s*[：:]\s*)'
)
_CLINICAL_IMAGE = re.compile(r'超声|彩超|影像|正位片|侧位片|X线|CT|MRI|磁共振', re.IGNORECASE)
_STATED_NORMAL_IMAGING = {'未见明显异常', '未见异常', '正常'}
_HEPATOBILIARY_ORGANS = re.compile(r'肝|胆囊|胆管|胆道|胆总管|胰腺|胰管|门静脉')
_OTHER_ABDOMINAL_ORGANS = re.compile(r'肾|脾|输尿管|集合系统|膀胱|前列腺|子宫|附件')
_DUPLICATE_LIFESTYLE_HEADING = re.compile(
    r'(?m)^(?:现状[：:]\s*(?=[^\n：:]{0,8}现状[：:])|关联的健康问题[：:]\s*(?=关联问题[：:])|'
    r'调整建议[：:]\s*(?=调整方向[：:]))'
)
_LIFESTYLE_SOURCE = re.compile(
    r'(?:^|(?<=；))\s*(?:(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2}'
    r'(?:[ T]\d{2}:\d{2}(?::\d{2})?)?)\s+)?'
    r'(?P<label>体检生活方式记录原文|您[^：；?？]+[?？](?:可多选。)?|请[^：；]+)[：:]'
)
_LIFESTYLE_PROMPTS = (
    (r'^您平时一周(?:大约)?有几天安排运动[?？]$', '平时运动频率'),
    (r'^您平时连续坐着、不起身的时间(?:通常)?有多久[?？]$', '平时连续久坐时长'),
    (r'^您平时的正餐(?:通常)?吃到哪些食物[?？](?:可多选。)?$', '平时正餐食物'),
    (r'^您平时吃甜食或喝含糖饮料的频率更接近哪一种[?？]$', '平时甜食或含糖饮料频率'),
    (r'^您平时临睡前吃夜宵的频率更接近哪一种[?？]$', '平时临睡前夜宵频率'),
    (r'^您平时的饮酒频率更接近哪一种[?？]$', '平时饮酒频率'),
    (r'^您最近通常每天睡多长时间[?？]$', '最近每日睡眠时长'),
)
_EXAM_ABBREVIATION = re.compile(r'[（(]([A-Za-z][A-Za-z0-9/_-]{1,12})[）)]$')
_DOI = re.compile(r'10\.\d{4,9}/[^\s<>?#]+', re.IGNORECASE)
_TRACKING_KEYS = frozenset(
    {
        'af',
        'casa_token',
        'content',
        'dclid',
        'fbclid',
        'fr',
        'gclid',
        'guestaccesskey',
        'mc_cid',
        'mc_eid',
        'mi',
        'msclkid',
        'previousarticle',
        'ref',
        'referrer',
        'redirect',
        'search',
        'source',
        'target',
        'text1',
        'widget',
    }
)


def _text(value):
    if isinstance(value, (list, tuple)):
        return '、'.join(_text(item) for item in value)
    if value is None:
        return ''
    if isinstance(value, bool):
        return '是' if value else '否'
    return str(value)


def _e(value):
    return escape(_text(value))


def _value(row):
    value = _text(row.get('value')).strip()
    unit = _text(row.get('unit')).strip()
    if not value or not unit:
        return value
    # Some source serializers print a binary float tail after an otherwise
    # short percentage. Suppress only a lost precision of at most one ULP.
    if (unit in {'%', '％'} and re.fullmatch(r'[+-]?\d+\.\d{12,}', value)
            and re.search(r'(?:0{6,}|9{6,})\d{0,3}$', value)):
        number = float(value)
        short = f'{number:.12g}'
        if number and 'e' not in short.lower() and abs(number - float(short)) <= ulp(number):
            value = short
    displayed = re.sub(r'[\s↑↓]+$', '', normalize('NFKC', value))
    if displayed in {'未见', '未检出', '阴性', '阳性', '弱阳性'}:
        return value
    normalized_unit = normalize('NFKC', unit)
    if ''.join(displayed.casefold().split()).endswith(''.join(normalized_unit.casefold().split())):
        return value
    return f'{value} {unit}'


def _clinical_conclusion(value):
    markers = list(_CLINICAL_CONCLUSION.finditer(value))
    if not markers:
        return ''
    original = value[markers[-1].end():]
    conclusion = re.split(r'(?:\n|(?<=[。；]))\s*(?:医生建议|检查建议|处理意见|建议)\s*[：:]', original, 1)[0]
    return conclusion.strip(' ；;') if len(conclusion) < len(value) else ''


def _clinical_display(row):
    value = re.sub(r'中间结果(?:显示|写明|提示|如下)?\s*[：:]?\s*', '', _value(row))
    status = row.get('templateStatus') or row.get('status')
    if (len(value) > 85 or status in _STATED_NORMAL_IMAGING) and (conclusion := _clinical_conclusion(value)):
        if re.search(r'乳腺|双乳', _text(row.get('name'))):
            measurements = re.search(r'(?:测得|测量)[：:]\s*([^。\n]+)', value)
            if measurements and re.search(r'左乳.*右乳|右乳.*左乳', measurements[1]):
                return measurements[1].strip() + '。' + conclusion
        return conclusion
    if len(value) > 85 and status in _STATED_NORMAL_IMAGING and _CLINICAL_IMAGE.search(_text(row.get('name'))):
        return status
    return re.sub(r'；\s*；+', '；', value)


def _digestive_measurements(group):
    rows = group.get('rows') or ()
    code = group.get('code')
    if code == 'oralGastric':
        scoped = []
        for row in rows:
            value = _text(row.get('value'))
            oral = re.search(r'(?:^|\n)口腔科(?:检室)?\s*\n(.+?)(?=\n体检日期|\Z)', value, re.S)
            scoped.append({**row, 'value': oral[1].strip()} if oral else row)
        return scoped
    if code != 'hepatobiliaryPancreatic':
        return rows
    scoped = []
    for row in rows:
        name, value = _text(row.get('name')), _value(row)
        # A projected atom already identifies its organ. Re-parsing its
        # numeric value as a multi-organ narrative would erase measurements.
        if (_HEPATOBILIARY_ORGANS.search(name) and not _OTHER_ABDOMINAL_ORGANS.search(name)
                or re.fullmatch(r'[<>≤≥]?\s*[+-]?\d+(?:\.\d+)?', _text(row.get('value')).strip())):
            scoped.append(row)
            continue
        if not _CLINICAL_IMAGE.search(name) or not (
            re.search(r'腹部|腹腔', name) or _OTHER_ABDOMINAL_ORGANS.search(value)
        ):
            scoped.append(row)
            continue
        # A combined imaging report's final result may describe a different organ.
        excerpts = []
        for part in re.split(r'[。；;\r\n]+', value):
            sentence = _CLINICAL_CONCLUSION.sub('', part).strip()
            if _HEPATOBILIARY_ORGANS.search(sentence) and not _OTHER_ABDOMINAL_ORGANS.search(sentence):
                excerpts.append(sentence)
        scoped.append({**row, 'value': '。'.join(dict.fromkeys(excerpts)), 'unit': None,
                       'status': None, 'templateStatus': None})
    return scoped


def _lifestyle_text(value):
    return _DUPLICATE_LIFESTYLE_HEADING.sub('', _text(value))


def _lifestyle_answer(value):
    """Group repeated source timestamps while preserving the answer and its question scope."""
    text = _lifestyle_text(value)
    sources = list(_LIFESTYLE_SOURCE.finditer(text))
    if not sources or sources[0].start() != 0:
        return _e(text)
    entries, when = [], None
    for index, source in enumerate(sources):
        end = sources[index + 1].start() if index + 1 < len(sources) else len(text)
        answer = text[source.end():end].strip(' ；')
        if not answer:
            return _e(text)
        timestamp, question = source.group('date', 'label')
        if timestamp != when:
            when = timestamp
            if when:
                entries.append(f'<small class="as-source-time">{_e(when)}</small>')
        if question == '体检生活方式记录原文':
            entries.append(f'<div>{_e(answer)}</div>')
            continue
        label = next((label for pattern, label in _LIFESTYLE_PROMPTS if re.fullmatch(pattern, question)), question)
        entries.append(f'<div>{_e(label)}：{_e(answer)}</div>')
    return ''.join(entries)


def _note(value):
    return f'<span class="as-cell-note">{_e(value)}</span>'


def _function_directions(group, rows):
    member_counts = Counter(_source_key(row['name']) for row in rows if row.get('name'))
    directions = {}
    for panel in group.get('panelStates') or ():
        for name, arrow in _directions(panel.get('details')):
            key = _source_key(name)
            if member_counts[key] == 1:
                directions.setdefault(key, set()).add(arrow)
    return {key: next(iter(arrows)) for key, arrows in directions.items() if len(arrows) == 1}


def _recorded_advice(item):
    quotes = {}
    for row in item.get('clinicalSignals') or ():
        if row.get('status') != '原报告医嘱' or not (quote := _text(row.get('value'))):
            continue
        if not any(ref.get('sourceCode') == 'A' and ref.get('recordId') for ref in row.get('sources') or ()):
            continue
        labels = quotes.setdefault(quote, {})
        if label := _text(row.get('name')).partition(' · ')[2]:
            labels[label] = None
    return quotes


def _advice_items(quotes):
    return _items(
        f'{_e("、".join(labels))}　{_e(quote)}' if labels else _e(quote) for quote, labels in quotes.items()
    )


def _table(headers, rows, *, extra_class='', blank=()):
    if not rows:
        return ''
    head = ''.join(f'<th scope="col">{_e(label)}</th>' for label in headers)
    body = []
    for row in rows:
        cells = []
        for position, (label, cell) in enumerate(zip(headers, row, strict=True)):
            fallback = blank[position] if position < len(blank) else None
            empty_class = '' if cell or fallback else ' class="as-empty"'
            content = cell or (_note(fallback) if fallback else _DASH)
            cells.append(f'<td data-l="{_e(label)}"{empty_class}>{content}</td>')
        body.append('<tr>' + ''.join(cells) + '</tr>')
    body = ''.join(body)
    return f'<table class="tbl {extra_class}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'


def _subsection(title, content, *, anchor=None):
    anchor_attr = f' id="{_e(anchor)}"' if anchor else ''
    return f'<h3{anchor_attr}>{_e(title)}</h3>{content}' if content else ''


def _items(rows):
    values = ''.join(f'<li>{row}</li>' for row in rows if row)
    return f'<ul class="facts">{values}</ul>' if values else ''


def _priority(item):
    priority = item.get('priority')
    if priority not in _PRIORITIES:
        return ''
    label, kind = _PRIORITIES[priority]
    return f'<span class="chip {kind}">{_e(priority)} {_e(label)}</span>'


def _cluster_anchor(cluster_id):
    if isinstance(cluster_id, str) and cluster_id.startswith('C') and cluster_id[1:].isascii():
        if cluster_id[1:].isdigit():
            return 'as-cluster-' + cluster_id
    return None


def _reference_link(row):
    doi = _text(row.get('doi')).strip()
    if _DOI.fullmatch(doi):
        return _e(f'https://doi.org/{doi}')
    for link in (doi, row.get('url')):
        if clean := _clean_reference_url(link):
            return _e(clean)
    return ''


def _clean_reference_url(link):
    if not isinstance(link, str):
        return ''
    try:
        parsed = urlsplit(link.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return ''
    if parsed.scheme not in {'http', 'https'} or not host or parsed.username is not None or port not in {None, 80, 443}:
        return ''
    path = parsed.path.rstrip('/')
    if host in {'doi.org', 'dx.doi.org'}:
        doi = path.lstrip('/')
        return f'https://doi.org/{doi}' if _DOI.fullmatch(doi) else ''
    if host == 'vertexaisearch.cloud.google.com' or path.lower().endswith(('/url', '/redirect')):
        return ''
    parameters = parse_qsl(parsed.query, keep_blank_values=True)
    if path.lower() in {'', '/articles', '/pmc', '/pubmed', '/results'} and any(
        key.lower() in {'q', 'query', 'search', 'term'} for key, _ in parameters
    ):
        return ''
    if re.search(r'(?:^|/)search(?:[./]|$)', path.lower()):
        args = dict(parameters)
        if not (
            host == 'www.embase.com'
            and args.get('subaction') == 'viewrecord'
            and re.fullmatch(r'L\d+', args.get('id', ''))
        ):
            return ''
    retained = [
        (key, value)
        for key, value in parameters
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith(('utm_', '_x_output_type_'))
    ]
    return parsed._replace(query=urlencode(retained)).geturl()


def _numbered_refs(data):
    rows = (data.get('appendices') or {}).get('referenceLiterature') or ()
    return {row['number']: row for row in rows if type(row.get('number')) is int and row['number'] > 0}


def _segments(row):
    filled = row.get('filledSegments')
    if type(filled) is not int or not 0 <= filled <= 10:
        return ''
    squares = ''.join('<i class="on"></i>' if index < filled else '<i></i>' for index in range(10))
    return f'<span class="as-score" role="img" aria-label="十格显示，已填充 {filled} 格">{squares}</span>'


class _AssessmentRenderer:
    def __init__(self, data, presentation, *, status=None, partial_reasons=()):
        self.data = presentation
        self.references = _numbered_refs(data)
        self.core_clusters = list(presentation.get('clusters') or ())
        self.cluster_ids = {item.get('id') for item in self.core_clusters}
        self.compacted_reference_ranges = False
        self.partial = status == 'partial_success'
        self.content_incomplete = self.partial and any(
            reason in {'REPORT_CONTENT_INCOMPLETE', 'REPORT_LAYOUT_INCOMPLETE'}
            for reason in partial_reasons
        )

    def source_reference(self, row):
        original = row.get('referenceRange')
        display = source_reference_display(row) or original
        if display != original:
            self.compacted_reference_ranges = True
        return display

    def refs(self, node):
        numbers = {
            number: None
            for number in node.get('evidenceRefs') or ()
            if type(number) is int and number in self.references
        }
        links = ''
        for number in numbers:
            row = self.references[number]
            title = ' · '.join(_text(value) for value in (row.get('title'), row.get('year')) if value)
            title_attr = f' title="{_e(title)}"' if title else ''
            links += f'<a href="#ref-{number}"{title_attr}>[{number}]</a>'
        return f'<span class="cite">{links}</span>' if links else ''

    def item_refs(self, node, *, inline=False):
        if not node or not (refs := self.refs(node)):
            return ''
        tag = 'span' if inline else 'div'
        return f'<{tag} class="as-item-references">本项参考{refs}</{tag}>'

    def reference_appendix(self):
        if not self.references:
            return ''
        rows = []
        for number, row in sorted(self.references.items()):
            fields = [row.get('title'), row.get('year')]
            if row.get('doi'):
                fields.append('DOI：' + _text(row['doi']))
            metadata = '；'.join(_e(value) for value in fields if value is not None and value != '')
            link = _reference_link(row)
            original = (
                f' <a class="as-original" href="{link}" target="_blank" rel="noopener noreferrer">原文链接</a>'
                if link else ''
            )
            rows.append(f'<li id="ref-{number}" value="{number}">{metadata}{original}</li>')
        return (
            '<section class="as-references" id="as-references" aria-labelledby="as-references-title">'
            '<h2 id="as-references-title">附录：参考文献</h2>'
            '<p class="small muted">以下条目来自本次报告实际引用目录，编号与正文引用一致。</p>'
            f'<ol class="as-reference-list">{"".join(rows)}</ol></section>'
        )

    def paragraph(self, value, node=None):
        parts = [part.strip() for part in _text(value).split('\n\n') if part.strip()]
        paragraphs = [f'<p>{_e(part)}</p>' for part in parts]
        if paragraphs and (references := self.item_refs(node)):
            paragraphs[-1] = f'<div class="as-reference-tail">{paragraphs[-1]}{references}</div>'
        return ''.join(paragraphs)

    def cluster_link(self, cluster_id, label):
        anchor = _cluster_anchor(cluster_id)
        if anchor and cluster_id in self.cluster_ids:
            return f'<a class="as-cluster-ref" href="#{anchor}">{_e(label)}</a>'
        return _e(label)

    def named_items(self, rows, *, ordered=False, extra_class=''):
        items = []
        for row in rows or ():
            title = _e(row.get('name'))
            content = self.paragraph(row.get('text'), row)
            children = self.named_items(row.get('items'), ordered=True)
            if content or children:
                label = f'<strong>{title}</strong>' if title else ''
                items.append(f'<li>{label}{content}{children}</li>')
        if not items:
            return ''
        tag = 'ol' if ordered else 'ul'
        return f'<{tag} class="{extra_class}">{"".join(items)}</{tag}>'

    def preface(self):
        overview = self.data.get('overview') or {}
        intro = ''.join(
            f'<p>{_PREFACE_EMPHASIS.sub(r"<strong>\1</strong>", _e(part))}</p>'
            for part in _text(overview.get('preface')).split('\n\n')
            if part
        )
        if self.partial:
            scope = ('部分章节或解读尚未完成。' if self.content_incomplete else '本文呈现当前可用内容。')
            intro = f'<p class="as-partial-note"><strong>部分报告。</strong>{scope}</p>' + intro
        reading = []
        for row in overview.get('readingGuide') or ():
            content = _PREFACE_EMPHASIS.sub(r'<strong>\1</strong>', _e(row.get('text')))
            if content:
                reading.append(f'<li><strong>{_e(row.get("name"))}：</strong> {content}</li>')
        if reading:
            return (
                intro + '<p><strong>本报告阅读指南：</strong></p><ol class="as-reading">' + ''.join(reading) + '</ol>'
            )
        return intro

    def signals(
        self, rows, *, cite=True, show_status=False, show_date=False, show_scale=False, inline=False,
        compact_clinical=False, show_panel=False, shared_date=None, source_directions=None,
    ):
        items = []
        for row in rows or ():
            name = clinical_observation_display_name(row) if compact_clinical else _text(row.get('name'))
            result = _clinical_display(row) if compact_clinical else _value(row)
            if name == result:
                result = ''
            if name or result:
                references = (' ' + marker) if cite and (marker := self.item_refs(row, inline=True)) else ''
                status = _text(row.get('status'))
                if status in {'用户自述，未核验', '用户自述,未核验'}:
                    status = '本人自述'
                state = (
                    f' <small class="muted">（{_e(status)}）</small>'
                    if show_status and status and status not in result
                    else ''
                )
                direction = source_directions.get(_source_key(name)) if source_directions else None
                if direction and direction not in result and direction not in (status if show_status else ''):
                    state += f' <small class="muted">（原报告方向{_e(direction)}）</small>'
                when = row.get('observedAt')
                timing = _text(when)
                if shared_date:
                    timing = timing.removeprefix(shared_date).lstrip(' T')
                date = f' <small class="muted">{_e(timing)}</small>' if show_date and timing else ''
                if show_date and not when and row.get('sourceReportedAge') is not None:
                    date += (f' <small class="muted">原件标注{_e(row["sourceReportedAge"])}岁，'
                             '检查日期未注明</small>')
                scale = _segments(row) if show_scale else ''
                assay = first_reported_abundance_assay(row) if show_panel else None
                source = ''
                if assay:
                    label = '来源图谱' if assay['type'] == 'composition_chart' else '来源面板'
                    source = f' <small class="muted">（{label}：{_e(assay["name"])}）</small>'
                items.append(
                    f'{_e(name)}{" " if name and result else ""}{_e(result)}{state}{date}{source}{references}{scale}'
                )
        if inline:
            return '；'.join(items)
        return _items(items)

    def group_assessments(self, group, *, has_summary=False):
        summary = self.paragraph(group.get('summary'), group)
        if summary:
            return summary
        descriptions = []
        for row in group.get('rows') or ():
            if has_summary and row.get('status') == '正常':
                continue
            if description := self.paragraph(row.get('interpretation'), row):
                descriptions.append(f'<strong>{_e(row.get("name"))}</strong>{description}')
        return _items(descriptions)

    def grouped_measurements(self, groups, headers, *, has_summary=False, clinical_table=False):
        cells = []
        ecology = headers == _ECOLOGY_HEADERS
        body = headers == _BODY_HEADERS
        for group in groups or ():
            rows = group.get('rows') or ()
            if clinical_table and not rows:
                continue
            group_name = _text(group.get('name'))
            label = f'<strong>{_e(group_name)}</strong>'
            age_difference = group.get('gutAgeDifference')
            if group.get('actualAge') is not None and age_difference is not None:
                label += f'<br><small>实足年龄 {_e(group["actualAge"])} 岁</small>'
            row_statuses = [row.get('templateStatus') or row.get('status') for row in rows]
            group_status = group.get('status')
            result_rows = rows
            if body or clinical_table:
                result_rows = [
                    {
                        **row,
                        'name': _text(row.get('name')).removeprefix(f'{group_name} ').strip() if clinical_table
                                else row.get('name'),
                        'status': status if len(rows) > 1 or status != group_status else None,
                    }
                    for row, status in zip(rows, row_statuses, strict=True)
                ]
                status_cells = '<br>'.join(_e(part) for part in _text(group_status).split('；') if part)
                if not group_status:
                    status_cells = _items(f'{_e(row.get("name"))}：{_e(row["status"])}'
                                          for row in result_rows if row.get('status'))
            else:
                states = '、'.join(dict.fromkeys(value for value in row_statuses if value))
                status_cells = _e(group_status or states)
            reference_items = []
            for row in result_rows:
                reference = self.source_reference(row) if ecology else row.get('referenceRange')
                if reference is None or reference == '':
                    continue
                name = _text(row.get('name'))
                abbreviation = _EXAM_ABBREVIATION.search(name) if clinical_table else None
                prefix = '' if len(result_rows) == 1 else _e(abbreviation[1] if abbreviation else name) + ' '
                reference_items.append(f'{prefix}{_e(reference)}')
            references = _items(reference_items)
            recorded_history = rows and all(row.get('sourceKind') == 'recordedHistory' for row in rows)
            if not references and rows:
                references = _note('送检登记值' if recorded_history else _NO_SOURCE_REFERENCE)
            if comparison := group.get('comparisonReference'):
                references += self.paragraph(comparison)
            dates = {_text(row.get('observedAt')) for row in rows}
            shared_date = None
            if len(rows) > 1 and '' not in dates:
                days = {when[:10] for when in dates if re.match(r'^\d{4}-\d{2}-\d{2}(?:[ T]|$)', when)}
                if len(dates) == 1:
                    shared_date = next(iter(dates))
                elif len(days) == 1 and all(when[:10] in days for when in dates):
                    shared_date = next(iter(days))
            if shared_date:
                label += f'<br><small class="muted">{_e(shared_date)}</small>'
            interpretation = self.group_assessments(group, has_summary=has_summary)
            if not interpretation and rows:
                interpretation = _note('仅作送检背景记录' if recorded_history else _NO_INTERPRETATION)
            results = self.signals(result_rows, cite=False, show_date=True, shared_date=shared_date,
                                   show_status=body or clinical_table or ecology, compact_clinical=clinical_table)
            if age_difference is not None:
                results += f'<div class="small">肠龄差 {_e(age_difference)} 岁</div>'
            cells.append((label, results, references, status_cells, interpretation))
        table_style = 'as-observations as-flow' + (' as-clinical' if clinical_table else ' as-body' if body else '')
        return _table(headers, cells, extra_class=table_style)

    def observations(self, rows, headers, *, compact_clinical=False, blank=()):
        cells = []
        for row in rows or ():
            if not row.get('name'):
                continue
            when = row.get('observedAt')
            timestamp = f'<small class="muted">{_e(when)}</small>' if when else ''
            value = _clinical_display(row) if compact_clinical else _value(row)
            result = f'<span class="as-result">{_e(value)}</span>' if value else ''
            if result and row.get('sourceKind') == 'derived' and '计算值' not in row['name']:
                result += ' <small class="muted">计算值</small>'
            if details := row.get('details'):
                result += f'<div class="small muted">{_e(details)}</div>'
            reference = self.source_reference(row) if headers == _ECOLOGY_HEADERS else row.get('referenceRange')
            interpretation = self.paragraph(row.get('interpretation'), row)
            status = _text(row.get('templateStatus')).strip() or _text(row.get('status')).strip()
            status_cell = _e(status)
            if headers == _SPECIALTY_HEADERS and result and status in {'', '-', '--', '—', '/'}:
                status_cell = _note(_NO_SOURCE_JUDGMENT)
            if result and row.get('sourceKind') == 'recordedHistory':
                reference = _e(reference) or _note('送检登记值')
                interpretation = interpretation or _note('仅作送检背景记录')
            else:
                reference = _e(reference) or (_note(_NO_SOURCE_REFERENCE) if result else '')
                interpretation = interpretation or ((_note(_NO_INTERPRETATION) + self.item_refs(row)) if result else '')
            cells.append(
                (
                    f'<b>{_e(row["name"])}</b>{("<br>" + timestamp) if timestamp else ""}',
                    result,
                    reference,
                    status_cell,
                    interpretation or self.item_refs(row),
                )
            )
        return _table(headers, cells, extra_class='as-observations as-flow', blank=blank)

    def priority_findings(self, findings):
        if not findings:
            return ''
        actions = {
            item.get('id'): item.get('action') for item in (self.data.get('conclusion') or {}).get('priorities') or ()
        }
        parts = []
        for item in findings:
            name = item.get('label') or item.get('name')
            label = self.cluster_link(item.get('id'), name)
            details = item.get('action') or item.get('summary') or item.get('text')
            if not details and actions.get(item.get('id')):
                details = _text(actions[item['id']]).split('\n\n', 1)[0]
            if label or details:
                parts.append(f'<li><strong>{label}</strong>{self.paragraph(details, item)}</li>')
        if not parts:
            return ''
        return f'<aside class="as-callout"><p><strong>临床优先排查提示</strong></p><ol>{"".join(parts)}</ol></aside>'

    def problem_graph(self):
        overview = self.data.get('overview') or {}
        nodes = {item.get('id'): item for item in self.core_clusters}
        node_ids = overview.get('graphClusterIds', nodes) or ()
        problems = [nodes[node_id] for node_id in node_ids if node_id in nodes]
        if not problems:
            return ''
        branches = []
        for item in sorted(problems, key=lambda row: _text(row.get('priority')) or 'P9'):
            node_id = item.get('id')
            name = '：'.join(_text(value) for value in (node_id, item.get('name')) if value)
            branches.append(
                f'<li class="as-branch" data-priority="{_e(item.get("priority"))}">'
                f'{self.cluster_link(node_id, name)}</li>'
            )
        note = self.paragraph(overview.get('graphSummary'), {'evidenceRefs': overview.get('graphSummaryEvidenceRefs')})
        if not note:
            note = '<p>主题对应的记录和解读见 <a href="#as-clusters">第五章</a>。</p>'
        return (
            f'<div class="as-graph-note">{note}</div><figure class="as-graph" aria-label="核心系统失衡态势速览">'
            f'<p class="as-root">{_e(overview.get("graphHub") or "全息健康态势")}</p>'
            f'<ol class="as-branches">{"".join(branches)}</ol></figure>'
        )

    def overview_signals(self, rows):
        # The API's reader projection already selects and composes overview
        # observations. Rendering must preserve those values, rather than
        # replace a longer vector with an examination heading or empty status.
        return self.signals(rows, cite=False, show_status=True)

    def overview(self):
        overview = self.data.get('overview') or {}
        body = self.data.get('clinical') or {}
        summary = self.paragraph(overview.get('summary'), overview)
        system_rows = []
        for row in overview.get('systems') or ():
            anchors = row.get('rows') or []
            interpretation = self.paragraph(row.get('summary'), row)
            status = row.get('status')
            chip = f'<span class="chip {_SYSTEM_STATUS_CLASS.get(status, "neutral")}">{_e(status)}</span>'
            system_rows.append((
                _e(row.get('name')),
                chip if status else '',
                self.overview_signals(anchors),
                interpretation or (_note(_NO_INTERPRETATION) if anchors else ''),
            ))
        systems = _table(_SYSTEM_HEADERS, system_rows, extra_class='as-systems as-flow')
        body_summary = self.paragraph(
            overview.get('bodySummary'), {'evidenceRefs': overview.get('bodySummaryEvidenceRefs')}
        )
        measurements = self.grouped_measurements(body.get('bodyGroups'), _BODY_HEADERS) or self.observations(
            body.get('bodyMetrics'), _BODY_HEADERS
        )
        findings = self.priority_findings(overview.get('priorityFindings'))
        return (
            _subsection('1.1 基础体格与人体成分深度校准', body_summary + measurements)
            + _subsection('1.2 多系统健康状态分级总览', summary + systems)
            + _subsection('1.3 临床优先排查项与未检风险提示', findings)
            + _subsection('1.4 核心系统失衡态势速览', self.problem_graph())
        )

    def symptoms(self):
        clinical = self.data.get('clinical') or {}
        symptoms = clinical.get('symptoms') or ()
        rows, interpretation = [], []
        for index, item in enumerate(symptoms, start=1):
            label = '：'.join(text for text in (f'S{index}', _text(item.get('name'))) if text)
            value = _text(item.get('value'))
            history = _text(item.get('history'))
            history = '。'.join(
                dict.fromkeys(text for text in (value, history) if text and text != _text(item.get('name')))
            )
            def evidence_column(signals, *, clinical_column=False):
                explanations = {signal['interpretation']: signal for signal in signals or ()
                                if signal.get('interpretation')}
                meaning = ''.join(self.paragraph(text, row) for text, row in explanations.items())
                return self.signals(signals, cite=False, show_status=True, compact_clinical=clinical_column) + meaning

            rows.append(
                (
                    _e(label),
                    _e(history),
                    evidence_column(item.get('clinicalSignals'), clinical_column=True),
                    evidence_column(item.get('microbiomeSignals')),
                )
            )
            explanation = self.paragraph(item.get('interpretation'), item)
            if explanation:
                interpretation.append(
                    f'<li><strong>{_e(item.get("name") or item.get("question"))}</strong>{explanation}</li>'
                )
        details = f'<ol>{"".join(interpretation)}</ol>' if interpretation else ''
        summary = self.paragraph(
            clinical.get('symptomSummary'), {'evidenceRefs': clinical.get('symptomSummaryEvidenceRefs')}
        )
        if summary:
            details += f'<aside class="as-callout"><p><strong>个体化定制印证</strong></p>{summary}</aside>'
        return _subsection(
            '2.1 主诉症状谱与临床检验信号印证',
            self.paragraph(clinical.get('symptomsLead')) + _table(
                _SYMPTOM_HEADERS, rows, extra_class='as-symptoms',
            ),
        ) + _subsection('2.2 躯体感知背后的微生态与代谢线索', details)

    def clinical(self):
        clinical = self.data.get('clinical') or {}
        groups = {group.get('code'): group for group in clinical.get('clinicalGroups') or ()}
        sections = [self.paragraph(clinical.get('summary'), clinical)]
        for code, title, headers in _CLINICAL_SECTIONS:
            group = groups.get(code) or {}
            table = self.grouped_measurements(group.get('templateRows'), headers, clinical_table=True)
            if not table and 'templateRows' not in group:
                extra_rows = (
                    (groups.get('otherClinical') or {}).get('rows') if code == 'endocrineMusculoskeletal' else ()
                )
                table = self.observations(
                    [*(group.get('rows') or ()), *(extra_rows or ())], headers, compact_clinical=True,
                )
            interpretation = self.paragraph(group.get('summary'), group)
            if interpretation and code == 'cardiometabolic':
                interpretation = f'<aside class="as-callout">{interpretation}</aside>'
            sections.append(_subsection(title, table + interpretation, anchor=f'as-system-{code}'))

        specialty = self.paragraph(
            clinical.get('specialtySummary'), {'evidenceRefs': clinical.get('specialtySummaryEvidenceRefs')}
        )
        specialty_table = self.observations(clinical.get('specialtyFindings'), _SPECIALTY_HEADERS)

        trends = self.paragraph(
            clinical.get('trendSummary'), {'evidenceRefs': clinical.get('trendSummaryEvidenceRefs')}
        )
        trend_rows = []
        for item in clinical.get('trends') or ():
            if len(points := item.get('points') or ()) < 2:
                continue
            trend_rows.append((
                _e(item.get('name')),
                self.signals(points[:-1], cite=False, show_date=True),
                self.signals(points[-1:], cite=False, show_date=True),
                _e(item.get('direction')),
                self.paragraph(item.get('interpretation'), item),
            ))
        trend_table = _table(_TREND_HEADERS, trend_rows, extra_class='as-observations')

        def observation_identity(row):
            return tuple(_text(row.get(key)) for key in ('name', 'value', 'unit', 'observedAt'))

        displayed = {
            observation_identity(row)
            for group in clinical.get('bodyGroups') or () for row in group.get('rows') or ()
        }
        displayed.update(observation_identity(row) for group in groups.values()
                         for panel in group.get('templateRows') or () for row in panel.get('rows') or ())
        displayed.update(observation_identity(row) for row in clinical.get('specialtyFindings') or ())
        baseline_items = []
        for group in clinical.get('normalBaselines') or ():
            supplemental = {observation_identity(row): row for row in group.get('rows') or ()
                            if observation_identity(row) not in displayed}
            if not supplemental:
                continue
            rows = list(supplemental.values())
            dates = {row.get('observedAt') for row in rows}
            shared_date = next(iter(dates)) if len(dates) == 1 else None
            timing = f'（{_e(shared_date)}）' if shared_date else ''
            entries = []
            for row in rows:
                reference = row.get('referenceRange')
                description = ' '.join(filter(None, (_e(row.get('name')), _e(_value(row)))))
                if reference:
                    description += f'（参考 {_e(reference)}）'
                if not shared_date and row.get('observedAt'):
                    description += f'　{_e(row["observedAt"])}'
                entries.append(description)
            baseline_items.append(f'<strong>{_e(group.get("name"))}</strong>{timing}' + _items(entries))
        baselines = _items(baseline_items)
        if baselines:
            baselines = (
                f'<aside class="as-callout"><p><strong>本次检查的基线记录</strong></p>{baselines}</aside>'
            )
        trend_content = trend_table + trends + baselines
        if not trend_content and (groups or specialty_table):
            trend_content = '<p>已记录的检查结果可作为后续同项目比较的基线。</p>'
        return (
            ''.join(sections)
            + _subsection('3.5 专项功能医学、遗传易感性与生物年龄评估', specialty_table + specialty)
            + _subsection('3.6 历年指标演变趋势与已确认稳健安全基线', trend_content)
        )

    def microbiome(self):
        clinical = self.data.get('clinical') or {}
        gut = self.data.get('microbiome') or {}
        digestive = clinical.get('digestiveContext') or ()
        structure = self.grouped_measurements(
            gut.get('structureGroups'), _ECOLOGY_HEADERS, has_summary=bool(gut.get('structureSummary'))
        )
        structure_codes = {row.get('code') for row in gut.get('structure') or ()}
        structure_codes.update(group.get('code') for group in gut.get('structureGroups') or ())
        if structure_codes & {'developmentalMaturity', 'childGrowth'}:
            structure_heading = '4.2 微生态宏观稳态指数与儿童相关菌群评估'
        elif 'gutAge' in structure_codes:
            structure_heading = '4.2 微生态宏观稳态指数与肠道生物学年龄评估'
        else:
            structure_heading = '4.2 微生态宏观稳态指数评估'
        if 'structureGroups' not in gut:
            rows = gut.get('structure') or ()
            if gut.get('structureSummary'):
                rows = [
                    {**row, 'interpretation': None, 'evidenceRefs': []} if row.get('status') == '正常' else row
                    for row in rows
                ]
            structure = self.observations(rows, _ECOLOGY_HEADERS)
        if self.compacted_reference_ranges:
            structure += ('<p class="as-cell-note">参考列保留本次结果对应的原件分档与正常参考；'
                          '完整分档及解释见原检测报告。</p>')
        structure += self.paragraph(
            gut.get('structureSummary'), {'evidenceRefs': gut.get('structureSummaryEvidenceRefs')},
        )
        sample = gut.get('sampleContext') or {}
        sampling = '，'.join(
            f'{label}{_text(sample[key])}'
            for key, label in (
                ('sampleType', '样本'),
                ('sampleDate', '采样日'),
                ('receivedDate', '接收日'),
                ('reportDate', '报告日'),
            )
            if sample.get(key)
        )
        sample_quality = sample.get('qualityControl')
        sample_notes = self.named_items(
            (
                {
                    'name': '样本信息',
                    'text': sampling,
                },
                {
                    'name': '样本质控记录',
                    'text': sample_quality,
                },
                {
                    'name': '样本说明',
                    'text': gut.get('sampleSummary'),
                    'evidenceRefs': gut.get('sampleSummaryEvidenceRefs'),
                },
                {
                    'name': '肠道产气与膳食纤维发酵耐受特征',
                    'text': gut.get('fermentationSummary'),
                    'evidenceRefs': gut.get('fermentationSummaryEvidenceRefs'),
                },
            )
        )
        if sample_notes:
            sample_notes = (
                f'<aside class="as-callout">{sample_notes}</aside>'
            )
        digestive_rows = []
        for group in digestive:
            mapping = _e(group.get('name'))
            context = self.signals(group.get('symptomContext'), cite=False, inline=True)
            if context:
                mapping += f'<br><span class="as-cell-note">本人报告：{context}</span>'
            observations = self.signals(_digestive_measurements(group), cite=False, show_status=True, show_date=True,
                                        compact_clinical=True)
            digestive_rows.append((
                mapping,
                observations,
                self.group_assessments(group) or (_note(_NO_INTERPRETATION) if observations or context else ''),
            ))
        digestive_table = _table(
            _DIGESTIVE_HEADERS,
            digestive_rows,
            extra_class='as-digestive as-flow',
        )
        risk = self.paragraph(gut.get('riskSummary'), {'evidenceRefs': gut.get('riskSummaryEvidenceRefs')})
        function_groups = gut.get('functionalGroups') or ()
        function_intro = ''
        if any(type(row.get('filledSegments')) is int for group in function_groups for row in group.get('rows') or ()):
            function_intro = ('<p class="as-function-intro">'
                              '下表采用 0～100 分制十格标尺，展示检测报告中记录的菌群代谢通路评分。</p>')
        overview = self.paragraph(
            gut.get('overviewSummary'), {'evidenceRefs': gut.get('overviewSummaryEvidenceRefs')}
        ) or self.paragraph(gut.get('summary'), gut)
        return (
            overview
            + sample_notes
            + _subsection('4.1 消化道器质基础与消化液微环境评估', digestive_table)
            + _subsection(structure_heading, structure)
            + _subsection(
                '4.3 核心功能菌群结构、条件致病谱与毒力因子解析',
                self.microbiome_group_table(gut.get('organismGroups'), _ORGANISM_HEADERS),
            )
            + _subsection(
                '4.4 菌群十维营养合成与外源毒素代谢潜能评估',
                function_intro + self.microbiome_group_table(function_groups, _PATHWAY_HEADERS, show_scale=True),
            )
            + _subsection('本章风险小结', risk)
        )

    def microbiome_group_table(self, groups, headers, *, show_scale=False):
        cells = []
        for group in groups or ():
            baseline = group.get('normalBaseline') or {}
            normal_rows = baseline.get('rows') or ()
            normal_names = baseline.get('names') or ()
            count = baseline.get('count')
            normal_count = count if type(count) is int and count > 0 else len(normal_rows) or len(normal_names)
            normal = ''
            if normal_count:
                normal = f'{_e(normal_count)} 项正常'
                include_scale = show_scale and group.get('code') == 'scfa'
                if normal_rows:
                    displayed = normal_rows if show_scale or len(normal_rows) <= 12 else normal_rows[:4]
                    normal += '，摘要举例：' if len(displayed) < normal_count else '。'
                    normal += self.signals(displayed, cite=False, show_scale=include_scale, inline=not include_scale)
                elif normal_names:
                    displayed = normal_names if len(normal_names) <= 12 else normal_names[:4]
                    normal += '，摘要举例：' if len(displayed) < normal_count else '。'
                    normal += _e(displayed)
            if undetected := [row.get('name') for row in baseline.get('undetectedRows') or () if row.get('name')]:
                normal += ('<br>' if normal else '') + f'未检出序列信号的项目为{_e(undetected)}。'
            abnormal = group.get('rows') or ()
            assessments = []
            if not show_scale:
                assessments = [row for row in abnormal if organism_assessment_display_group(row)]
                abnormal = [row for row in abnormal if row not in assessments]
            if show_scale and normal_rows:
                normal_items = {(_text(row.get('name')), _value(row)) for row in normal_rows}
                abnormal = [row for row in abnormal if (_text(row.get('name')), _value(row)) not in normal_items]
            if normal_rows and group.get('code') == 'genes':
                abnormal = [
                    row for row in abnormal
                    if not (row.get('code') == 'antibioticResistanceGenes' and row.get('status') == '未检出异常')
                ]
            if show_scale and not normal:
                baseline_state = baseline.get('state')
                all_abnormal = baseline_state == 'all_abnormal' or (
                    baseline_state is None and abnormal and all(
                        _value(row) and _PATHWAY_DEVIATION.fullmatch(_text(row.get('status')).strip())
                        for row in abnormal
                    )
                )
                if all_abnormal:
                    normal = _note('所列分项均有偏离')
            source_directions = _function_directions(group, [*abnormal, *normal_rows]) if show_scale else None
            findings = self.signals(abnormal, cite=False, show_status=True, show_scale=show_scale,
                                    source_directions=source_directions)
            if assessments:
                findings += self.signals(assessments, cite=False, show_status=True)
            if not findings and normal_count:
                findings = _note('所列分项未见异常')
            interpretation = self.group_assessments(group)
            if not interpretation and (findings or normal):
                interpretation = _note(_NO_INTERPRETATION)
            label = f'<strong>{_e(group.get("name"))}</strong>'
            if show_scale:
                label += _items(f'{_e(panel["sourceName"])}：{_e(panel["status"])}'
                                for panel in group.get('panelStates') or () if panel.get('sourceName')
                                and panel.get('status'))
            cells.append((label, findings, normal, interpretation))
        return _table(headers, cells, extra_class='as-observations as-microbiome-group as-flow')

    def clusters(self):
        items = []
        clusters = self.core_clusters
        for item in clusters:
            anchor = _cluster_anchor(item.get('id'))
            root_attr = f' id="{anchor}"' if anchor else ''
            heading = '：'.join(_text(item.get(field)) for field in ('id', 'name') if item.get(field))
            clinical = item.get('clinicalSignals') or ()
            personal = [row for row in clinical if any(
                source.get('sourceCode') == 'B' for source in row.get('sources') or ()
            )]
            observations = [row for row in clinical if row not in personal and row.get('status') != '原报告医嘱']
            evidence = []
            for label, signals, kind in (
                ('本人报告', personal, 'reported'),
                ('临床与影像发现', observations, 'clinical'),
                ('菌群检测背景' if observations else '菌群检测记录', item.get('microbiomeSignals'), 'microbiome'),
            ):
                if not signals:
                    continue
                line = self.signals(signals, cite=kind == 'microbiome', show_status=kind != 'reported', inline=True,
                                    compact_clinical=kind == 'clinical', show_panel=kind == 'microbiome')
                if line:
                    evidence.append(f'<li><strong>{label}：</strong>{line}</li>')
            evidence_block = f'<li><strong>本次相关记录</strong><ul>{"".join(evidence)}</ul></li>' if evidence else ''
            if original := _advice_items(_recorded_advice(item)):
                evidence_block += f'<li><strong>原报告医嘱</strong>{original}</li>'
            explanation = self.paragraph(item.get('interpretation'), item)
            if explanation:
                explanation = (
                    '<li><strong class="as-cluster-subheading">当前躯体体感与生理健康影响解读</strong>'
                    f'{explanation}</li>'
                )
            elif self.content_incomplete:
                explanation = '<li class="as-cell-note">本主题的综合解读尚未完成。</li>'
            body = f'<ul>{evidence_block}{explanation}</ul>' if evidence or explanation else ''
            items.append(
                f'<article class="as-cluster"{root_attr}><h4>集群 {_e(heading)} ｜ 优先级：{_priority(item)}</h4>'
                f'{body}</article>'
            )
        summary = self.paragraph(
            self.data.get('clusterSummary'), {'evidenceRefs': self.data.get('clusterSummaryEvidenceRefs')}
        )
        # Missing reviewed prose does not establish missing patient data or
        # another chapter's ability to answer this section's clinical duty.
        clustered = summary + ''.join(items)
        return _subsection('5.1 个体化系统失衡集群定性与健康影响解读', clustered) + _subsection(
            '5.2 ATM 健康诱因全景复盘与健康认知校准', self.lifestyle()
        )

    def lifestyle(self):
        factors = self.data.get('lifestyleFactors') or ()
        history = self.paragraph(
            self.data.get('lifestyleSummary'), {'evidenceRefs': self.data.get('lifestyleSummaryEvidenceRefs')}
        )
        if not history:
            history = _items(
                f'<strong>{_e(item.get("name"))}</strong> {_e(item.get("value"))}'
                f'{self.paragraph(item.get("interpretation"), item)}'
                for item in factors
                if item.get('kind') in {'background', 'history', 'trigger'}
            )
        if history:
            history = (
                f'<ul><li><strong>先天背景（Antecedents）与既往触发事件（Triggers）复盘</strong>{history}</li></ul>'
            )
        lifestyle = _table(
            _LIFESTYLE_HEADERS,
            [
                (
                    _e(item.get('name')),
                    _lifestyle_answer(item.get('value')),
                    self.paragraph(item.get('interpretation'), item),
                )
                for item in factors
                if item.get('kind') not in {'background', 'history', 'trigger'}
            ],
            extra_class='as-lifestyle',
        )
        if lifestyle:
            lifestyle = f'<ul><li><strong>日常维持诱因（Mediators）与客观异常映射全景表</strong></li></ul>{lifestyle}'
        recognition = self.named_items(self.data.get('lifestyleRecognition'), ordered=True)
        if recognition:
            recognition = f'<aside class="as-callout"><p><strong>日常健康认知温和校准</strong></p>{recognition}</aside>'
        return history + lifestyle + recognition

    def conclusion(self):
        conclusion = self.data.get('conclusion') or {}
        summary = self.paragraph(conclusion.get('summary'), conclusion)
        layer_rows = conclusion.get('layers') or ()
        directions = self.named_items(conclusion.get('managementDirections'), extra_class='as-directions')
        by_id = {item.get('id'): item for item in self.core_clusters}
        priorities = []
        shown_advice = set()
        for row in sorted(conclusion.get('priorities') or (), key=lambda item: _text(item.get('priority')) or 'P9'):
            identifier = row.get('id')
            title = self.cluster_link(identifier, row.get('name'))
            quotes = _recorded_advice(by_id.get(identifier, {}))
            original = _advice_items(quotes)
            shown_advice.update(f'针对{label}，原报告记录“{quote}”' if label else f'原报告记录“{quote}”'
                                for quote, labels in quotes.items() for label in labels or ('',))
            action = self.paragraph(row.get('action'), row)
            if not (original or action):
                continue
            source = f'<p class="as-followup-heading"><strong>原报告医嘱</strong></p>{original}' if original else ''
            followup = f'<p class="as-followup-heading"><strong>报告列出的下一步</strong></p>{action}' if action else ''
            priorities.append(f'<li><strong>{title}　{_priority(row)}</strong>{source}{followup}</li>')
        priority_list = f'<ul class="as-followup">{"".join(priorities)}</ul>' if priorities else ''
        # Only an exact source paragraph already shown for its owner can be
        # collapsed. A different object, condition or reviewed passage stays.
        display_layers = [{**row, 'text': '\n\n'.join(
            part for part in _text(row.get('text')).split('\n\n') if part.strip() not in shown_advice
        )} for row in layer_rows]
        layers = self.named_items(display_layers, ordered=True, extra_class='as-followup')
        supplied = ''
        if self.content_incomplete and (layers or priority_list) and len(layer_rows) < 3:
            supplied = '<p class="as-cell-note">以下为本次已提供的事项。部分分层解读尚未完成。</p>'
        sections = _subsection('6.1 核心健康风险分层总结', supplied + summary + priority_list + layers) + _subsection(
            '6.2 全周期微生态与生活方式协同管理方向', directions
        )
        compliance = (
            '<p class="small muted as-boundary"><em>医学合规与专业边界声明：本报告用于健康评估与管理参考。'
            '疾病诊断、处方和治疗由执业医师结合临床情况决定。报告提示的专科复查或随访事项，'
            '请按临床安排就诊。</em></p>'
        )
        return sections + compliance if sections else ''


def assessment_chapters(data, presentation, *, status=None, partial_reasons=()):
    renderer = _AssessmentRenderer(data, presentation, status=status, partial_reasons=partial_reasons)
    chapters = []
    for anchor, title, method_name in ASSESSMENT_SECTIONS:
        content = getattr(renderer, method_name)()
        if not content and renderer.content_incomplete and method_name != 'preface':
            content = '<p class="small muted">本章暂无可展示的解读。</p>'
        section = f'<section class="chapter" id="{anchor}"><h2>{_e(title)}</h2>{content}</section>'
        chapters.append((anchor, title, section))
    if chapters and (appendix := renderer.reference_appendix()):
        anchor, title, section = chapters[-1]
        chapters[-1] = (anchor, title, section + appendix)
    return chapters
