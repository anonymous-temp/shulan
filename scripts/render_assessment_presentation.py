"""Render the six-chapter assessment (``assessment.v2``) as the printable sample.

This is the reference rendering given to the frontend team. Its layout follows
the client's example report: each section is a plain table in which one row
answers one clinical question, with the client's fixed columns, callout boxes
and reading order. The only additions are small inline charts inside the
cells (reference range bars, grade bands, log-abundance bars and 0-100 score
bars), the cluster relation graph and the three-stage roadmap.

It adds no medical judgement. Statuses, levels, bands and directions come from
the callback; when a field is absent the same deterministic parser that
produced it is applied to the row's own strings
(``modules.report.assessment_chart_fields``). Every list may be empty and every
text may be null; an empty part is skipped or replaced by the statement the
callback supplies.
"""

from __future__ import annotations

import math
import re
from copy import deepcopy
from datetime import date
from html import escape
from math import ulp
from unicodedata import normalize
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit

from modules.report import assessment_chart_fields as charts
from modules.report.assessment_callback_view import ASSESSMENT_SECTIONS
from modules.report.assessment_microbiome_view import annotate_microbiome_chart_fields
from modules.report.assessment_presentation import _SYSTEMS
from modules.report.assessment_reading_labels import clinical_observation_display_name

ASSESSMENT_CSS = """
.assessment-shell{max-width:940px;margin:0 auto;padding:28px 18px 64px}
main.assessment{
  --ink:#1b1f1d;--ink-2:#3d433f;--muted:#5b615d;--line:#d3d8d4;--grid:#e6e9e6;--wash:#f4f5f3;--paper:#fff;
  --accent:#0d5f52;--accent-wash:#e7f0ed;--band:#cfe6d8;--band-edge:#6fa688;
  --focus:#ec835a;--focus-ink:#8a3413;--focus-wash:#fbe7dc;
  --borderline:#fab219;--borderline-ink:#6a4500;--borderline-wash:#fdf2d6;
  --above:#d9822b;--above-ink:#7a4108;--below:#3f7fc9;--below-ink:#1d4a85;
  --normal:#0ca30c;--normal-ink:#1c5c1c;--normal-wash:#e6f3e4;
  --untested:#9aa09c;--untested-ink:#3f423f;--untested-wash:#eceeec;
  --p0:#d03b3b;--p0-ink:#9a2121;--p0-wash:#f9e1e0;
  --serif:"Noto Serif CJK SC","Source Han Serif SC","Songti SC",SimSun,serif;
  background:var(--paper);color:var(--ink);font:15px/1.72 var(--sans);padding:52px 56px 60px;
  border:1px solid var(--line)
}
.assessment ::selection{background:#cfe3dd}
.assessment h1,.assessment h2,.assessment h3{font-family:var(--serif);font-weight:700;letter-spacing:.01em;
  text-wrap:balance}
.assessment h2{font-size:23px;line-height:1.35;margin:0 0 12px;padding:0 0 8px;border-bottom:1.5px solid var(--ink)}
.assessment h3{font-size:17.5px;line-height:1.45;margin:24px 0 8px}
.assessment h4{font-size:15px;line-height:1.5;margin:14px 0 6px;font-weight:650}
.assessment p{margin:0 0 8px}
.assessment section.chapter{padding-top:30px}
.assessment .as-lead{margin:0 0 10px}
.assessment .as-muted{color:var(--muted)}
.assessment .as-small{font-size:13px;line-height:1.65}
.assessment .as-missing{color:var(--ink-2);padding:8px 12px;background:var(--wash);border-radius:6px;margin:6px 0 10px}
.assessment .as-dash{color:var(--muted)}
.assessment .cite{font-size:10.5px;vertical-align:super;line-height:0;margin-left:1px;white-space:nowrap}
.assessment .cite a{text-decoration:none;padding:0 1px;color:var(--accent)}
/* cover */
.assessment header.as-cover{padding:0 0 14px;margin:0 0 4px;border-bottom:3px double var(--ink)}
.assessment header.as-cover h1{font-size:27px;line-height:1.3;margin:0 0 12px}
.assessment .as-ident{border-left:3px solid var(--accent);padding:2px 0 2px 14px;font-size:14px;line-height:1.8}
.assessment .as-ident p{margin:0}
.assessment .as-ident b{font-weight:650}
.assessment .as-ident .sep{color:var(--line);margin:0 8px}
/* status, priority and fact chips */
.assessment .as-lv,.assessment .as-pri{display:inline-flex;align-items:center;gap:4px;padding:0 7px 0 5px;
  border-radius:3px;font-size:11.5px;line-height:1.75;font-weight:600;white-space:nowrap;vertical-align:middle}
.assessment .as-lv svg,.assessment .as-pri svg{width:10px;height:10px;flex:none}
.assessment .lv-focus{background:var(--focus-wash);color:var(--focus-ink)}
.assessment .lv-borderline{background:var(--borderline-wash);color:var(--borderline-ink)}
.assessment .lv-above{background:var(--paper);color:var(--above-ink);box-shadow:inset 0 0 0 1px var(--above)}
.assessment .lv-below{background:var(--paper);color:var(--below-ink);box-shadow:inset 0 0 0 1px var(--below)}
.assessment .lv-normal{background:var(--normal-wash);color:var(--normal-ink)}
.assessment .lv-notTested,.assessment .lv-none{background:var(--untested-wash);color:var(--untested-ink)}
.assessment .as-lvnote{display:block;font-size:11.5px;color:var(--ink-2);margin-top:3px;line-height:1.45}
.assessment .pri-p0{background:var(--p0-ink);color:#fff}
.assessment .pri-p1{background:var(--ink-2);color:#fff}
.assessment .pri-p2{background:var(--paper);color:var(--ink-2);box-shadow:inset 0 0 0 1px #8f9692}
.assessment .pri-p3{background:var(--paper);color:var(--muted);box-shadow:inset 0 0 0 1px var(--line)}
.assessment .as-cid{display:inline-block;font-size:11.5px;font-weight:700;padding:0 6px;border-radius:3px;
  background:var(--ink-2);color:#fff;line-height:1.7;margin-right:4px;text-decoration:none;white-space:nowrap}
.assessment .as-cid.pri-p0{background:var(--p0-ink)}
.assessment .as-cid.pri-p2,.assessment .as-cid.pri-p3{background:#8f9692}
.assessment .as-facts{display:flex;flex-wrap:wrap;gap:4px 6px;margin:4px 0 6px}
.assessment .as-fact{display:inline-block;font-size:12.5px;line-height:1.65;padding:0 7px;border-radius:3px;
  background:var(--paper);box-shadow:inset 0 0 0 1px var(--line);overflow-wrap:anywhere}
.assessment .as-fact small{color:var(--muted);font-size:11.5px;margin-right:5px}
/* tags carry a measured value; the dot carries the level */
.assessment .as-tags{display:flex;flex-wrap:wrap;gap:4px;margin:2px 0}
.assessment .as-tag{display:inline-flex;align-items:baseline;gap:4px;padding:1px 7px 1px 6px;border-radius:3px;
  background:var(--wash);font-size:12px;line-height:1.55;color:var(--ink)}
.assessment .as-tag i{width:6px;height:6px;border-radius:50%;background:var(--untested);flex:none;align-self:center}
.assessment .as-tag b{font-weight:650}
.assessment .as-tag small{color:var(--muted);font-size:11px}
.assessment .as-tag.lv-focus{background:var(--focus-wash)}.assessment .as-tag.lv-focus i{background:var(--focus)}
.assessment .as-tag.lv-borderline{background:var(--borderline-wash)}
.assessment .as-tag.lv-borderline i{background:var(--borderline)}
.assessment .as-tag.lv-normal{background:var(--normal-wash)}.assessment .as-tag.lv-normal i{background:var(--normal)}
.assessment .as-tag.lv-focus,.assessment .as-tag.lv-borderline,.assessment .as-tag.lv-normal{color:var(--ink)}
.assessment .as-tag.lv-none{background:transparent;box-shadow:inset 0 0 0 1px var(--line)}
.assessment .as-tag.lv-none i{background:#fff;box-shadow:inset 0 0 0 1.5px #7d837f}
.assessment .as-dir{display:inline-block;width:7px;height:9px;fill:currentColor;vertical-align:-1px;margin-left:1px}
.assessment .as-more{font-size:12px;color:var(--muted);align-self:center}
/* tables */
.assessment table.as-table{width:100%;border-collapse:collapse;table-layout:fixed;font-size:12.5px;line-height:1.6;
  margin:6px 0 12px}
.assessment table.as-table th{text-align:left;font-weight:650;font-size:12px;color:var(--ink-2);background:var(--wash);
  padding:6px 7px;border:1px solid var(--line);vertical-align:bottom}
.assessment table.as-table td{padding:6px 7px;border:1px solid var(--line);vertical-align:top;overflow-wrap:anywhere}
.assessment table.as-table td.k{font-weight:650;color:var(--ink)}
.assessment table.as-table td.k small{display:block;font-weight:400;color:var(--muted);font-size:11.5px}
.assessment table.as-table td p{margin:0 0 4px}
.assessment table.as-table td p:last-child{margin:0}
.assessment table.as-table td.st{text-align:left}
.assessment .as-v{margin:0 0 5px;line-height:1.5}
.assessment .as-v:last-child{margin-bottom:0}
.assessment .as-v .nm{color:var(--ink-2)}
.assessment .as-v b{font-weight:650;font-variant-numeric:tabular-nums}
.assessment .as-v b.lv-focus{background:none;color:var(--focus-ink)}
.assessment .as-v b.lv-borderline{background:none;color:var(--borderline-ink)}
.assessment .as-v small{color:var(--muted);font-size:11px}
/* inline-block, not block: Chrome will not fragment a table cell that holds a block-level auto-height SVG */
.assessment .as-v svg:not(.as-dir){display:inline-block;vertical-align:top;width:100%;max-width:190px;height:auto;
  margin:2px 0 0;overflow:visible}
.assessment .as-v .tx{font-weight:400;color:var(--ink)}
.assessment .as-tx{display:grid;grid-template-columns:minmax(0,1fr) 40%;gap:2px 8px;align-items:center;margin:0 0 3px;
  line-height:1.45}
.assessment .as-tx b{font-weight:650;font-variant-numeric:tabular-nums}
.assessment .as-tx b.lv-focus{background:none;color:var(--focus-ink)}
.assessment .as-tx b.lv-borderline{background:none;color:var(--borderline-ink)}
.assessment .as-tx small{color:var(--muted);font-size:10.5px}
.assessment .as-tx svg:not(.as-dir){display:block;width:100%;height:auto;overflow:visible}
.assessment .as-v.wide svg:not(.as-dir){max-width:none}
.assessment .as-ref>div{margin:0 0 3px;line-height:1.5}
.assessment .as-ref .ideal{color:var(--accent)}
.assessment .as-plain{list-style:none;margin:0;padding:0}
.assessment .as-plain li{margin:0 0 3px}
/* callouts */
.assessment .as-callout{border-left:3px solid var(--accent);background:var(--accent-wash);padding:9px 14px 9px 13px;
  margin:10px 0 12px;border-radius:0 6px 6px 0}
.assessment .as-callout>h4:first-child{margin:0 0 5px;font-size:14px}
.assessment .as-callout>p:last-child,.assessment .as-callout>ol:last-child,.assessment .as-callout>div:last-child{
  margin-bottom:0}
.assessment .as-callout.urgent{border-left-color:var(--p0-ink);background:var(--p0-wash)}
.assessment .as-callout ol{margin:4px 0 0;padding-left:1.4em}
.assessment .as-callout li{margin:0 0 8px}
.assessment .as-callout li>p{margin:2px 0 0}
.assessment .as-urgent{padding:4px 0 2px}
.assessment .as-urgent+.as-urgent{border-top:1px solid rgba(154,33,33,.18);margin-top:6px;padding-top:8px}
.assessment .as-urgent h5{font-size:14.5px;margin:0 0 2px;font-weight:700}
/* scales (inline SVG; numbers are always printed beside them) */
.assessment svg text{font-family:var(--sans)}
.assessment svg .trk{stroke:#dde1de;stroke-width:2;stroke-linecap:round}
.assessment svg .band{fill:var(--band);stroke:var(--band-edge);stroke-width:1}
.assessment svg .tgt{fill:var(--accent)}
.assessment svg .tk{font-size:10px;fill:var(--muted)}
.assessment svg .tkl{stroke:#c9cec9;stroke-width:1}
.assessment svg .dot{stroke:#fff;stroke-width:1.6}
.assessment svg .ring{fill:none;stroke:rgba(27,31,29,.5);stroke-width:.8}
.assessment svg .dot.lv-focus{fill:var(--focus)}.assessment svg .dot.lv-borderline{fill:var(--borderline)}
.assessment svg .dot.lv-normal{fill:var(--normal)}.assessment svg .dot.lv-notTested{fill:#7d837f}
.assessment svg .dot.lv-none{fill:#fff;stroke:#6b716d;stroke-width:1.6}
.assessment svg .hollow{fill:#fff;stroke:#5b615d;stroke-width:1.4}
.assessment svg .over{fill:#5b615d}
.assessment svg .bn{fill:var(--normal-wash);stroke:#a9d3a4;stroke-width:.8}
.assessment svg .blo{fill:#e5eefa;stroke:#a8c4e6;stroke-width:.8}
.assessment svg .blx{fill:#cfe0f5;stroke:#86abd9;stroke-width:.8}
.assessment svg .bhi{fill:#fdf0d8;stroke:#ebcf8f;stroke-width:.8}
.assessment svg .bhx{fill:#fbe0d2;stroke:#e6ad93;stroke-width:.8}
.assessment svg .bo{fill:#f1efe9;stroke:#cfcac0;stroke-width:.8}
.assessment svg .bt{font-size:9px;fill:var(--ink-2)}
.assessment .as-srow{display:grid;grid-template-columns:minmax(0,5.6em) minmax(0,1fr) 2.9em;align-items:center;gap:6px;
  padding:1px 0;font-size:12px;line-height:1.5}
.assessment .as-srow>svg{display:block;width:100%;height:auto;overflow:visible}
.assessment .as-srow .v{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
/* graph */
.assessment figure.as-graph{margin:8px 0 6px}
.assessment figure.as-graph svg{display:block;width:100%;height:auto}
.assessment figure.as-graph .hub{fill:var(--accent)}
.assessment figure.as-graph .hub-t{fill:#fff;font-size:15px;font-weight:700}
.assessment figure.as-graph .hub-s{fill:#d9ebe5;font-size:12px}
.assessment figure.as-graph .edge{fill:none;stroke:#a9b1ac;stroke-width:1.5}
.assessment figure.as-graph .node{fill:#fff;stroke-width:1.2}
.assessment figure.as-graph .node.pri-p0{stroke:var(--p0-ink);stroke-width:2}
.assessment figure.as-graph .node.pri-p1{stroke:var(--ink-2);stroke-width:1.6}
.assessment figure.as-graph .node.pri-p2{stroke:#8f9692}
.assessment figure.as-graph .node.pri-p3{stroke:var(--line)}
.assessment figure.as-graph .badge.pri-p0{fill:var(--p0-ink)}
.assessment figure.as-graph .badge.pri-p1{fill:var(--ink-2)}
.assessment figure.as-graph .badge.pri-p2,.assessment figure.as-graph .badge.pri-p3{fill:#8f9692}
.assessment figure.as-graph .bid{font-size:12px;font-weight:700;fill:#fff}
.assessment figure.as-graph .ns{font-size:11.5px;fill:var(--ink-2)}
.assessment figure.as-graph .nt{font-size:13px;fill:var(--ink)}
.assessment ul.as-clines{list-style:none;margin:4px 0 8px;padding:0;font-size:13.5px}
.assessment ul.as-clines li{margin:0 0 3px}
.assessment .as-v .r{white-space:nowrap}
.assessment ul.as-clines .as-pri{margin-left:6px}
/* clusters */
.assessment .as-cluster{margin:14px 0 10px}
.assessment .as-cluster>h4{font-family:var(--serif);font-size:15.5px;margin:0 0 6px;line-height:1.55}
.assessment .as-cluster>h4 .as-pri{margin-left:8px}
.assessment .as-cluster>div{margin:0 0 6px;padding-left:12px;border-left:2px solid var(--grid)}
.assessment .as-cluster>div>h5{font-size:13px;font-weight:650;color:var(--ink-2);margin:0 0 1px}
.assessment .as-cluster>div p{margin:0 0 3px}
/* roadmap */
.assessment .as-roadmap{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:10px 0 12px}
.assessment .as-stage{border:1px solid var(--line);border-radius:8px;overflow:hidden}
.assessment .as-stage>header{padding:8px 12px;border-bottom:1px solid var(--grid);background:var(--wash)}
.assessment .as-stage>header .t{display:flex;align-items:baseline;gap:8px}
.assessment .as-stage>header .t b{font-family:var(--serif);font-size:18px}
.assessment .as-stage>header .t span{font-size:12px;color:var(--muted)}
.assessment .as-stage>header h4{font-size:13px;color:var(--ink-2);font-weight:600;margin:2px 0 0}
.assessment .as-stage .ids{padding:8px 12px 0}
.assessment .as-stage p{padding:6px 12px 2px;margin:0 0 6px;font-size:13px;line-height:1.65}
.assessment .as-direction{margin:10px 0}
.assessment .as-direction h4{margin:0 0 4px}
.assessment .as-direction ol{margin:4px 0 0;padding-left:1.4em}
.assessment .as-direction li{margin:0 0 6px}
.assessment .as-direction li>p{margin:2px 0 0}
/* trends */
.assessment .as-trends{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:10px;margin:8px 0}
.assessment .as-trend{border:1px solid var(--line);border-radius:8px;padding:8px 10px}
.assessment .as-trend svg{display:block;width:100%;height:auto}
.assessment .as-trend .line{fill:none;stroke:var(--accent);stroke-width:2;stroke-linejoin:round}
.assessment .as-trend .pt{fill:var(--accent);stroke:#fff;stroke-width:1.5}
/* references */
.assessment .as-references{margin-top:24px;padding-top:14px;border-top:1px solid var(--line)}
.assessment .as-reference-list{padding-left:2.2em;font-size:12px;color:var(--ink-2)}
.assessment .as-reference-list li{margin:0 0 6px;overflow-wrap:anywhere}
.assessment .as-reference-list a{white-space:nowrap}
@media screen and (max-width:720px){
  .assessment-shell{padding:0}
  main.assessment{padding:24px 14px;border:none}
  .assessment .as-roadmap{grid-template-columns:minmax(0,1fr)}
  .assessment table.as-table{table-layout:auto}
}
@media print{
  .assessment-shell{max-width:none;padding:0;margin:0}
  main.assessment{padding:0;border:none;font-size:10pt;line-height:1.62}
  .assessment *{print-color-adjust:exact;-webkit-print-color-adjust:exact}
  .assessment section.chapter{break-before:auto;padding-top:16pt}
  .assessment h2,.assessment h3,.assessment h4,.assessment h5,.assessment .as-cluster>h4{break-after:avoid-page}
  .assessment table.as-table thead{display:table-header-group}
  .assessment table.as-table tr:not(.long){break-inside:avoid}
  .assessment .as-urgent,.assessment .as-stage,.assessment figure.as-graph,.assessment .as-trend,
  .assessment .as-ident,.assessment .as-tx,.assessment .as-v,.assessment .as-srow,.assessment ul.as-clines{
    break-inside:avoid}
  .assessment .as-cluster>div>h5{break-after:avoid-page}
  .assessment p,.assessment li{orphans:2;widows:2}
  .assessment .as-reference-list li{break-inside:avoid}
  .assessment a{color:inherit;text-decoration:none}
}
"""

_CLINICAL_CONCLUSION = re.compile(
    r'(?:^|(?<=[。；;\n]))\s*(?:'
    r'(?:检查结论|影像诊断|影像印象|诊断意见|结论)(?:\s*[：:]\s*|\s+|(?=[一-鿿]))'
    r'|(?:超声提示|提示)\s*[：:]\s*)'
)
_CLINICAL_IMAGE = re.compile(r'超声|彩超|影像|正位片|侧位片|X线|CT|MRI|磁共振', re.IGNORECASE)
_STATED_NORMAL_IMAGING = {'未见明显异常', '未见异常', '正常'}
_HEPATOBILIARY_ORGANS = re.compile(r'肝|胆囊|胆管|胆道|胆总管|胰腺|胰管|门静脉')
_OTHER_ABDOMINAL_ORGANS = re.compile(r'肾|脾|输尿管|集合系统|膀胱|前列腺|子宫|附件')
_LIFESTYLE_PROMPTS = (
    (r'^您?平时一周(?:大约)?有几天安排运动[?？]$', '平时运动频率'),
    (r'^您?平时连续坐着、不起身的时间(?:通常)?有多久[?？]$', '平时连续久坐时长'),
    (r'^您?平时的正餐(?:通常)?吃到哪些食物[?？](?:可多选。)?$', '平时正餐食物'),
    (r'^您?平时吃甜食或喝含糖饮料的频率更接近哪一种[?？]$', '平时甜食或含糖饮料频率'),
    (r'^您?平时临睡前吃夜宵的频率更接近哪一种[?？]$', '平时临睡前夜宵频率'),
    (r'^您?平时的饮酒频率更接近哪一种[?？]$', '平时饮酒频率'),
    (r'^您?最近通常每天睡多长时间[?？]$', '最近每日睡眠时长'),
)
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
    return f'{value} {_unit(unit)}'


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


def _is_advice(row):
    """Recorded examination advice: the ``kind`` field, or the legacy status marker of older callbacks."""
    return row.get('kind') == 'advice' or row.get('status') in {'原报告医嘱', '检查建议'}


def _is_personal(row):
    return any(source.get('sourceCode') == 'B' for source in row.get('sources') or ())


def _recorded_advice(item):
    quotes = {}
    for row in item.get('clinicalSignals') or ():
        if not _is_advice(row) or not (quote := _text(row.get('value'))):
            continue
        if not any(ref.get('sourceCode') == 'A' and ref.get('recordId') for ref in row.get('sources') or ()):
            continue
        quotes.setdefault(quote, None)
    return list(quotes)


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


# ---------- vocabulary ----------

_ICONS = {
    'focus': '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="5" fill="currentColor"/>'
    '<rect x="5.2" y="2.6" width="1.6" height="4.4" rx=".8" fill="#fff"/><circle cx="6" cy="8.9" r=".9" fill="#fff"/>'
    '</svg>',
    'borderline': '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.6" fill="none" '
    'stroke="currentColor" stroke-width="1.8"/><circle cx="6" cy="6" r="1.6" fill="currentColor"/></svg>',
    'above': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5 10.5 9.5H1.5Z" fill="currentColor"/></svg>',
    'below': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 10.5 10.5 2.5H1.5Z" fill="currentColor"/></svg>',
    'normal': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.2 6.3 4.9 9 9.8 3.4" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>',
    'notTested': '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2.4" y="5.2" width="7.2" height="1.6" rx=".8" '
    'fill="currentColor"/></svg>',
    'P0': '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1 11 10.5H1Z" fill="currentColor"/></svg>',
}
# The five reader statuses (spec: body groups, systems, clinical groups, template rows) and their chips.
_STATUS_CHIPS = {
    '重点关注': 'focus',
    '亚临床边界': 'borderline',
    '高于理想值': 'above',
    '低于理想值': 'below',
    '稳健达标': 'normal',
    '未检测': 'notTested',
}
_PRIORITIES = {
    'P0': ('优先医疗排查', 'p0'),
    'P1': ('核心干预', 'p1'),
    'P2': ('协同调理', 'p2'),
    'P3': ('随访巩固', 'p3'),
}
# Chapter three follows the client's order of systems; other groups keep the callback order after them.
_CLINICAL_ORDER = ('cardiometabolic', 'organMetabolism', 'hematologyImmune', 'endocrineMusculoskeletal')
_CLINICAL_TITLES = {
    'cardiometabolic': '糖脂代谢、胰岛素敏感性与心血管系统评估',
    'organMetabolism': '肝胆胰消化、肾脏代谢与营养辅酶状态评估',
    'hematologyImmune': '血液学、铁代谢、免疫与一碳甲基化评估',
    'endocrineMusculoskeletal': '内分泌激素、骨骼肌肉与器官影像学评估',
    'otherClinical': '其他器官与专项检查',
}
_OTHER_SYSTEM = ('otherClinical', '其他器官与专项检查')
_STAGES = (
    ('clinical', '第一层级', '首月', '临床专科排查与随访', '本次未发现需在首月内完成的专科排查或随访事项。'),
    ('core', '第二层级', '90 天', '核心代谢与肠道微生态干预', '本次未形成需在 90 天内集中干预的代谢或微生态方向。'),
    ('maintenance', '第三层级', '持续', '营养储备与长期健康维护', '保持现有健康习惯，按常规周期复查。'),
)
_LAYER_STAGES = {'clinical': 'clinical', 'routine': 'clinical', 'core': 'core', 'maintenance': 'maintenance'}
_PRIORITY_STAGES = {'P0': 'clinical', 'P1': 'core', 'P2': 'maintenance', 'P3': 'maintenance'}
_LAYER_NAME = re.compile(r'^\s*(?P<level>第.层级)\s*[：:]\s*(?P<title>.+?)\s*(?:[（(](?P<when>[^（）()]+)[）)])?\s*$')
_BACKGROUND_KINDS = {'background', 'history', 'trigger'}
_QA = re.compile(
    r'^\s*(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\s+)?'
    r'(?P<label>[^；;：:\n]{1,80}?)\s*(?:：|(?<!\d):(?!\d))\s*(?P<answer>.*)$',
    re.S,
)
_LABEL_NOISE = (
    (re.compile(r'（原报告菌群关联评估）|\(原报告菌群关联评估\)'), '（菌群关联评估）'),
    (re.compile(r'原报告|原件|主检'), ''),
)
_MICROBIOME_INDEX_NOTE = '菌群功能评分为基因预测的 0–100 分相对评分，反映代谢潜力，不等同于体内营养素水平。'
_DASH = '<span class="as-dash">—</span>'


def _label(value):
    """A program-supplied label without back-office wording; prose is never rewritten here."""
    text = _text(value)
    for pattern, replacement in _LABEL_NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()


def _prompt(question):
    """A questionnaire prompt as a short, impersonal label."""
    text = _text(question).strip()
    for pattern, label in _LIFESTYLE_PROMPTS:
        if re.fullmatch(pattern, text):
            return label
    text = re.sub(r'您的|您', '', text)
    text = re.sub(r'(?:可多选。?)$', '', text).strip()
    return text.rstrip('？?。').strip()


def _qa_pairs(value):
    """(label, answer) pairs of "题目：答案；题目：答案"; a piece without a label continues the previous answer."""
    text = _text(value).strip()
    if not text:
        return []
    pairs = []
    for piece in re.split(r'[；;\n]', text):
        piece = piece.strip()
        if not piece:
            continue
        match = _QA.match(piece)
        if match and match['answer'].strip():
            label = match['label'].strip()
            pairs.append(['' if label == '体检生活方式记录原文' else _prompt(label), match['answer'].strip()])
        elif pairs:
            pairs[-1][1] += '；' + piece
        else:
            pairs.append(['', piece])
    return [tuple(pair) for pair in pairs]


def _answers_html(value):
    """Questionnaire answers ("题目：答案；…") one per line; written prose stays one paragraph."""
    pairs = _qa_pairs(value)
    if len(pairs) < 2 or not all(label for label, _ in pairs):
        text = re.sub(r'您的|您', '', _text(value)).strip()
        return f'<p>{_e(text)}</p>' if text else ''
    return '<ul class="as-plain">' + ''.join(f'<li>{_e(label)}：{_e(answer)}</li>' for label, answer in pairs) + '</ul>'


def _fmt(number):
    if number is None:
        return ''
    if number == 0:
        return '0'
    magnitude = abs(number)
    if magnitude >= 1000:
        text = f'{number:.0f}'
    elif magnitude >= 100:
        text = f'{number:.1f}'
    elif magnitude >= 1:
        text = f'{number:.2f}'
    else:
        text = f'{number:.{max(2, 2 - math.floor(math.log10(magnitude)))}f}'
    return text.rstrip('0').rstrip('.') if '.' in text else text


_SUPERSCRIPT = str.maketrans('0123456789', '⁰¹²³⁴⁵⁶⁷⁸⁹')


def _unit(unit):
    """Printed form of a recorded unit: kg/m2 as kg/m², 10E9/L and 10^9/L as ×10⁹/L."""
    text = re.sub(r'(?<![A-Za-z])(kg/m|cm|m)2(?![0-9A-Za-z])', r'\1²', _text(unit))
    return re.sub(
        r'^\s*[×xX*]?\s*10\s*(?:[Ee]|\^)\s*(\d{1,2})(?=\s*/)',
        lambda match: '×10' + match[1].translate(_SUPERSCRIPT),
        text,
    )


def _level_class(level):
    return level if level in charts.LEVEL_LABELS else 'none'


def _status_chip(status=None, level=None):
    """The reader status as a chip: one of the five labels, otherwise the level's label with the status below it."""
    label = _label(status)
    # Older callbacks wrote "重点关注：项目、项目"; the items are already listed in the row.
    label = re.sub(rf'^({"|".join(_STATUS_CHIPS)})\s*[：:].*$', r'\1', label, flags=re.S)
    css = _STATUS_CHIPS.get(label)
    note = ''
    if css is None:
        if level not in charts.LEVEL_LABELS:
            return f'<span class="as-lvnote">{_e(label)}</span>' if label else ''
        css = level
        if label and label != charts.LEVEL_LABELS[level]:
            note = f'<span class="as-lvnote">{_e(label)}</span>'
        label = charts.LEVEL_LABELS[level]
    return f'<span class="as-lv lv-{css}">{_ICONS[css]}{_e(label)}</span>{note}'


def _priority_chip(priority):
    if priority not in _PRIORITIES:
        return ''
    label, css = _PRIORITIES[priority]
    icon = _ICONS['P0'] if priority == 'P0' else ''
    return f'<span class="as-pri pri-{css}">{icon}{_e(priority)} {_e(label)}</span>'


def _arrow(direction):
    if direction == 'up':
        return (
            '<svg class="as-dir" viewBox="0 0 8 10" role="img" aria-label="偏高">'
            '<path d="M4 0 8 5.2H5.1V10H2.9V5.2H0Z"/></svg>'
        )
    if direction == 'down':
        return (
            '<svg class="as-dir" viewBox="0 0 8 10" role="img" aria-label="偏低">'
            '<path d="M4 10 8 4.8H5.1V0H2.9V4.8H0Z"/></svg>'
        )
    return ''


def _note(text):
    return f'<p class="as-missing">{_e(text)}</p>' if _text(text).strip() else ''


def _subsection(title, content, *, anchor=None):
    anchor_attr = f' id="{_e(anchor)}"' if anchor else ''
    return f'<h3{anchor_attr}>{_e(title)}</h3>{content}' if content else ''


def _table(heads, widths, rows, *, css=''):
    """A plain table: fixed columns, a header repeated on every printed page, one row per clinical question."""
    if not rows:
        return ''
    cols = ''.join(f'<col style="width:{width}%">' for width in widths)
    head = ''.join(f'<th>{_e(title)}</th>' for title in heads)
    klass = f'as-table {css}'.strip()
    return (
        f'<table class="{klass}"><colgroup>{cols}</colgroup><thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    )


def _tr(cells, *, long=False):
    """A table row; a row taller than about a third of a page may break across pages, a shorter one stays whole."""
    return f'<tr{" class=long" if long else ""}>{"".join(cells)}</tr>'


def _is_long(count, *texts):
    return count >= 5 or max((len(_text(text)) for text in texts), default=0) > 220


_ABBREVIATION = re.compile(r'[（(]([A-Za-z][A-Za-z0-9\-/.\u2160-\u217f ]*)[）)]\s*$')


def _short_name(name):
    """The abbreviation in a name's closing brackets, as the client's reference column writes it."""
    match = _ABBREVIATION.search(_text(name))
    return match[1].strip() if match else _text(name)


def _callout(title, body, *, css=''):
    if not body:
        return ''
    klass = f'as-callout {css}'.strip()
    return f'<div class="{klass}"><h4>{_e(title)}</h4>{body}</div>'


def _days_between(first, second):
    try:
        return abs((date.fromisoformat(str(second)[:10]) - date.fromisoformat(str(first)[:10])).days)
    except ValueError:
        return None


_FULL_WIDTH_MARKS = frozenset('—…“”‘’')


def _advance(char, size):
    """Advance of one glyph: CJK glyphs and the dashes and quotes set with them take one em."""
    return size if ord(char) > 0x2E80 or char in _FULL_WIDTH_MARKS else size * 0.58


def _text_width(text, size):
    return sum(_advance(char, size) for char in _text(text))


def _wrap(text, size, width, lines=2):
    """Lines of at most ``width`` user units at ``size``; CJK glyphs count as one em."""
    result, current, used = [], '', 0.0
    for char in _text(text):
        advance = _advance(char, size)
        if used + advance > width and current:
            result.append(current)
            current, used = '', 0.0
            if len(result) == lines:
                break
        current += char
        used += advance
    else:
        if current:
            result.append(current)
        return result
    result[-1] = result[-1][:-1] + '…'
    return result


def _row_label(row):
    """(short name, shown result) of a clinical row, without repeating the name inside the result."""
    shown = _clinical_display(row).strip()
    name = clinical_observation_display_name(row)
    name = _label(charts.display_name(name)) if name else ''
    if name and shown == name:
        shown = ''
    elif name and shown.startswith(name):
        shown = shown[len(name):].lstrip(' ：:；;，,')
    return name, shown


# ---------- scales (inline SVG; numbers are always printed beside them) ----------

_SW = 200.0
_X0 = 8.0
_X1 = _SW - 8.0


def _x(value, low, high):
    return _X0 + (value - low) / ((high - low) or 1.0) * (_X1 - _X0)


def _range_svg(number, low, high, kind, level, label, target=None):
    """A small clinical reference band, optional target bar and the measured point (no labels; text sits beside)."""
    target = target if isinstance(target, dict) else {}
    t_low, t_high = target.get('low'), target.get('high')
    values = [value for value in (number, low, high, t_low, t_high) if isinstance(value, (int, float))]
    if kind == 'range':
        span = (high - low) or abs(high) or 1.0
        lo, hi = low - 0.55 * span, high + 0.55 * span
        band = (low, high)
    elif kind == 'upper':
        span = abs(high) or 1.0
        lo, hi = (0.0 if high > 0 else high - span), high + 0.6 * span
        band = (None, high)
    else:
        span = abs(low) or 1.0
        lo, hi = low - 0.6 * span, low + 0.9 * span
        band = (low, None)
    lo, hi = min(lo, *values), max(hi, *values)
    anchor_low = low if low is not None else high
    anchor_high = high if high is not None else low
    overflow = ''
    if number is not None and number > anchor_high + 4 * span:
        hi, overflow = anchor_high + 3 * span, 'right'
    elif number is not None and number < anchor_low - 4 * span:
        lo, overflow = anchor_low - 3 * span, 'left'
    if all(value >= 0 for value in values):
        lo = max(lo, 0.0)
    pad = (hi - lo) * 0.04
    lo, hi = (lo if lo == 0 else lo - pad), hi + pad
    b0 = _x(band[0], lo, hi) if band[0] is not None else _X0
    b1 = _x(band[1], lo, hi) if band[1] is not None else _X1
    parts = [
        f'<svg viewBox="0 0 {_SW:.0f} 20" role="img" aria-label="{_e(label)}">',
        f'<line class="trk" x1="{_X0}" y1="9" x2="{_X1}" y2="9"/>',
        f'<rect class="band" x="{b0:.1f}" y="4" width="{max(b1 - b0, 2):.1f}" height="10" rx="3"/>',
    ]
    if t_low is not None or t_high is not None:
        g0 = _x(max(t_low, lo), lo, hi) if t_low is not None else _X0
        g1 = _x(min(t_high, hi), lo, hi) if t_high is not None else _X1
        parts.append(f'<rect class="tgt" x="{g0:.1f}" y="16" width="{max(g1 - g0, 2):.1f}" height="3" rx="1.5"/>')
    if number is not None:
        cx = min(max(_x(number, lo, hi), _X0), _X1)
        parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="9" r="6.6"/>')
        parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="9" r="5.6"/>')
        if overflow == 'right':
            parts.append(f'<path class="over" d="M{_X1 + 3:.1f} 6 {_X1 + 7:.1f} 9 {_X1 + 3:.1f} 12Z"/>')
        elif overflow == 'left':
            parts.append(f'<path class="over" d="M{_X0 - 3:.1f} 6 {_X0 - 7:.1f} 9 {_X0 - 3:.1f} 12Z"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _band_class(label):
    """Normal bands are green; low bands blue and high bands warm, deeper at the extremes."""
    text = _text(label)
    if charts.is_normal_label(text):
        return 'bn'
    if re.search(r'极低|严重偏低', text):
        return 'blx'
    if re.search(r'极高|严重偏高', text):
        return 'bhx'
    if re.search(r'低|少|弱', text):
        return 'blo'
    if re.search(r'高|多|强', text):
        return 'bhi'
    return 'bo'


def _bands_svg(number, bands, level, label):
    """Grade bands of the source report as the background, the measured point on top."""
    finite = [value for band in bands for value in (band.get('low'), band.get('high')) if value is not None]
    lo, hi = min(finite), max(finite)
    span = (hi - lo) or 1.0
    if any(band.get('low') is None for band in bands):
        lo -= 0.25 * span
    if any(band.get('high') is None for band in bands):
        hi += 0.25 * span
    if number is not None:
        lo, hi = min(lo, number), max(hi, number)
    parts = [f'<svg viewBox="0 0 {_SW:.0f} 30" role="img" aria-label="{_e(label)}">']
    edges = set()
    for band in bands:
        x0 = _x(band['low'], lo, hi) if band.get('low') is not None else _X0
        x1 = _x(band['high'], lo, hi) if band.get('high') is not None else _X1
        x0, x1 = max(x0, _X0), min(x1, _X1)
        if x1 - x0 < 0.5:
            continue
        parts.append(
            f'<rect class="{_band_class(band["label"])}" x="{x0 + 0.5:.1f}" y="5" width="{max(x1 - x0 - 1, 1):.1f}" '
            'height="13" rx="2"/>'
        )
        if len(band['label']) * 9.2 + 4 < x1 - x0:
            middle = (x0 + x1) / 2
            parts.append(f'<text class="bt" x="{middle:.1f}" y="14.8" text-anchor="middle">{_e(band["label"])}</text>')
        for value in (band.get('low'), band.get('high')):
            if value is not None and lo < value < hi:
                edges.add(value)
    for value in sorted(edges):
        x = _x(value, lo, hi)
        parts.append(f'<text class="tk" x="{x:.1f}" y="29" text-anchor="middle">{_e(_fmt(value))}</text>')
    if number is not None:
        cx = min(max(_x(number, lo, hi), _X0), _X1)
        parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="11.5" r="6.6"/>')
        parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="11.5" r="5.6"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _axis_svg(number, lo, hi, ticks, level, label, *, marker=None):
    """A plain axis with labelled ticks, an optional reference marker and the measured point."""
    parts = [
        f'<svg viewBox="0 0 {_SW:.0f} 30" role="img" aria-label="{_e(label)}">',
        f'<line class="trk" x1="{_X0}" y1="11.5" x2="{_X1}" y2="11.5"/>',
    ]
    for value, text in ticks:
        x = _x(value, lo, hi)
        parts.append(f'<line class="tkl" x1="{x:.1f}" y1="7.5" x2="{x:.1f}" y2="15.5"/>')
        parts.append(f'<text class="tk" x="{x:.1f}" y="29" text-anchor="middle">{_e(text)}</text>')
    if marker is not None:
        x = _x(marker, lo, hi)
        parts.append(f'<rect class="tgt" x="{x - 1:.1f}" y="4" width="2" height="15" rx="1"/>')
    if number is not None:
        cx = min(max(_x(number, lo, hi), _X0), _X1)
        parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="11.5" r="6.6"/>')
        parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="11.5" r="5.6"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _score_svg(score, bands, level, label):
    """A 0–100 score over its grade bands; the normal band is named in the next column."""
    width = 200.0
    parts = [f'<svg viewBox="0 0 {width:.0f} 16" role="img" aria-label="{_e(label)}">']
    for band in bands:
        low = 0.0 if band.get('low') is None else max(band['low'], 0.0)
        high = 100.0 if band.get('high') is None else min(band['high'], 100.0)
        x0, x1 = 4 + low / 100 * (width - 8), 4 + high / 100 * (width - 8)
        if x1 - x0 >= 1:
            parts.append(
                f'<rect class="{_band_class(band["label"])}" x="{x0 + 0.5:.1f}" y="3" width="{x1 - x0 - 1:.1f}" '
                'height="10" rx="2"/>'
            )
    cx = 4 + min(max(score, 0.0), 100.0) / 100 * (width - 8)
    parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="8" r="6.2"/>')
    parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="8" r="5.2"/>')
    parts.append('</svg>')
    return ''.join(parts)


_LOG_LOW, _LOG_HIGH = -4.0, 2.0  # 0.0001 % to 100 %, one shared axis for every abundance row


def _log_x(percent, width=_SW):
    value = min(max(percent, 10**_LOG_LOW), 10**_LOG_HIGH)
    return 6 + (math.log10(value) - _LOG_LOW) / (_LOG_HIGH - _LOG_LOW) * (width - 12)


def _abundance_svg(abundance, low, high, level, label, *, width=_SW):
    """Relative abundance on a log axis against the reported reference band; 0 is drawn hollow at the origin."""
    parts = [
        f'<svg viewBox="0 0 {width:.0f} 18" role="img" aria-label="{_e(label)}">',
        f'<line class="trk" x1="6" y1="9" x2="{width - 6:.0f}" y2="9"/>',
    ]
    for exponent in range(int(_LOG_LOW), int(_LOG_HIGH) + 1):
        x = _log_x(10.0**exponent, width)
        parts.append(f'<line class="tkl" x1="{x:.1f}" y1="6" x2="{x:.1f}" y2="12"/>')
    if high is not None:
        b0 = _log_x(low, width) if low else 6
        b1 = _log_x(high, width)
        parts.append(f'<rect class="band" x="{b0:.1f}" y="4" width="{max(b1 - b0, 2):.1f}" height="10" rx="3"/>')
    if abundance is not None:
        if abundance <= 0:
            parts.append('<circle class="hollow" cx="6" cy="9" r="4.4"/>')
        else:
            cx = _log_x(abundance, width)
            parts.append(f'<circle class="ring" cx="{cx:.1f}" cy="9" r="6.2"/>')
            parts.append(f'<circle class="dot lv-{_level_class(level)}" cx="{cx:.1f}" cy="9" r="5.2"/>')
    parts.append('</svg>')
    return ''.join(parts)


def _trend_svg(points, label):
    values = [(row, charts.parse_number(row.get('value'))) for row in points]
    values = [(row, number) for row, number in values if number is not None]
    if len(values) < 2:
        return ''
    numbers = [number for _, number in values]
    lo, hi = min(numbers), max(numbers)
    span = (hi - lo) or abs(hi) or 1.0
    lo, hi = lo - 0.2 * span, hi + 0.2 * span
    width, height = 240.0, 64.0
    step = (width - 40) / (len(values) - 1)
    coords = [(20 + index * step, 44 - (number - lo) / (hi - lo) * 30) for index, (_, number) in enumerate(values)]
    path = ' '.join(f'{x:.1f},{y:.1f}' for x, y in coords)
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_e(label)}">',
             f'<polyline class="line" points="{path}"/>']
    for index, ((row, number), (x, y)) in enumerate(zip(values, coords, strict=True)):
        parts.append(f'<circle class="pt" cx="{x:.1f}" cy="{y:.1f}" r="3.4"/>')
        if index in {0, len(values) - 1}:
            anchor = 'start' if index == 0 else 'end'
            parts.append(
                f'<text class="tk" x="{x:.1f}" y="{y - 7:.1f}" text-anchor="{anchor}">{_e(_fmt(number))}</text>'
            )
            parts.append(
                f'<text class="tk" x="{x:.1f}" y="60" text-anchor="{anchor}">{_e(_text(row.get("date"))[:10])}</text>'
            )
    parts.append('</svg>')
    return ''.join(parts)


def _graph_svg(hub, hub_lines, nodes):
    """Centre (key indicators) to cluster nodes; names wrap to at most three lines, text is 11.5–15 px."""
    width, gap, node_x = 680.0, 6.0, 300.0
    text_width = width - node_x - 6 - 56
    laid = []
    for identifier, name, priority, anchors in nodes:
        lines = _wrap(name, 13, text_width, lines=3)
        sub = f'{priority} {_PRIORITIES[priority][0]}' if priority in _PRIORITIES else ''
        for anchor in anchors:  # an anchor that does not fit is left out, never cut
            candidate = f'{sub} · {anchor}' if sub else anchor
            if _text_width(candidate, 11.5) <= text_width:
                sub = candidate
        laid.append((identifier, lines, priority, [sub] if sub else [], 16 + 17 * len(lines) + (15 if sub else 0)))
    total = sum(item[4] for item in laid) + gap * (len(laid) - 1)
    hub_h = 40 + 20 * len(hub_lines)
    height = max(total + 12, hub_h + 20)
    hub_y = (height - hub_h) / 2
    parts = [f'<svg viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="{_e(hub)}与失衡集群关系">']
    y = (height - total) / 2
    boxes = []
    for item in laid:
        boxes.append((y, item))
        cy = y + item[4] / 2
        parts.append(
            f'<path class="edge" d="M230 {height / 2:.1f} C265 {height / 2:.1f} {node_x - 35:.1f} {cy:.1f} '
            f'{node_x:.1f} {cy:.1f}"/>'
        )
        y += item[4] + gap
    parts.append(f'<rect class="hub" x="8" y="{hub_y:.1f}" width="222" height="{hub_h:.1f}" rx="8"/>')
    parts.append(f'<text class="hub-t" x="22" y="{hub_y + 26:.1f}">{_e(_wrap(hub, 15, 196, lines=1)[0])}</text>')
    for index, line in enumerate(hub_lines):
        text = _wrap(line, 12, 196, lines=1)[0]
        parts.append(f'<text class="hub-s" x="22" y="{hub_y + 48 + index * 20:.1f}">{_e(text)}</text>')
    for top, (identifier, lines, priority, sub, box_h) in boxes:
        css = _PRIORITIES.get(priority, ('', 'p3'))[1]
        parts.append(
            f'<rect class="node pri-{css}" x="{node_x:.1f}" y="{top:.1f}" width="{width - node_x - 6:.0f}" '
            f'height="{box_h:.0f}" rx="7"/>'
        )
        parts.append(f'<rect class="badge pri-{css}" x="{node_x + 10:.1f}" y="{top + 6:.1f}" width="32" height="18" '
                     'rx="3"/>')
        parts.append(f'<text class="bid" x="{node_x + 26:.1f}" y="{top + 19.5:.1f}" text-anchor="middle">'
                     f'{_e(identifier)}</text>')
        for index, line in enumerate(lines):
            parts.append(f'<text class="nt" x="{node_x + 50:.1f}" y="{top + 20 + index * 17:.1f}">{_e(line)}</text>')
        if sub:
            parts.append(f'<text class="ns" x="{node_x + 50:.1f}" y="{top + 19 + len(lines) * 17:.1f}">'
                         f'{_e(sub[0])}</text>')
    parts.append('</svg>')
    return ''.join(parts)


def _cited_numbers(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, list) and (key == 'evidenceRefs' or key.endswith('EvidenceRefs')):
                yield from (number for number in value if type(number) is int)
            else:
                yield from _cited_numbers(value)
    elif isinstance(node, list):
        for item in node:
            yield from _cited_numbers(item)


def _anchor_tag(anchor, level):
    """A system anchor (spec 2.7 ``anchors[]``) as a tag."""
    value = f' <b>{_e(anchor["value"])}</b>' if anchor.get('value') else ''
    unit = f'<small>{_e(_unit(anchor["unit"]))}</small>' if anchor.get('unit') else ''
    css = _level_class(anchor.get('level') or level)
    return (
        f'<span class="as-tag lv-{css}"><i></i>{_e(_label(anchor.get("name")))}{value}{unit}'
        f'{_arrow(anchor.get("direction"))}</span>'
    )


def _band_text(band):
    low, high = band.get('low'), band.get('high')
    if low is None:
        return f'<{_fmt(high)}'
    if high is None:
        return f'>{_fmt(low)}'
    return f'{_fmt(low)}–{_fmt(high)}'


def _strip_lead(text, name):
    """A line without the item name it repeats at its start."""
    text = _text(text).strip()
    if name and text.startswith(name):
        return text[len(name):].lstrip('：:，,；; ')
    return text


class _AssessmentRenderer:
    def __init__(self, data, presentation, *, status=None, partial_reasons=()):
        # Completeness travels in the callback status; the page shows no banner.
        self.data = presentation
        cited = set(_cited_numbers(presentation))
        self.references = {number: row for number, row in _numbered_refs(data).items() if number in cited}
        # The appendix lists the works that a printed citation mark points to, in order of first mention.
        self.shown_refs = {}
        self.core_clusters = [item for item in presentation.get('clusters') or () if isinstance(item, dict)]
        self.cluster_ids = {item.get('id') for item in self.core_clusters}
        self.cluster_priority = {item.get('id'): item.get('priority') for item in self.core_clusters}
        self.overview_data = presentation.get('overview') or {}
        self.clinical_data = presentation.get('clinical') or {}
        # Older callbacks carry no chart fields; the same parser fills them on a copy.
        self.microbiome_data = annotate_microbiome_chart_fields(deepcopy(presentation.get('microbiome') or {}))
        self.conclusion_data = presentation.get('conclusion') or {}

    # ----- shared pieces -----

    def refs(self, node):
        numbers = {
            number: None
            for number in (node or {}).get('evidenceRefs') or ()
            if type(number) is int and number in self.references
        }
        links = ''
        for number in numbers:
            self.shown_refs.setdefault(number, None)
            row = self.references[number]
            title = ' · '.join(_text(value) for value in (row.get('title'), row.get('year')) if value)
            title_attr = f' title="{_e(title)}"' if title else ''
            links += f'<a href="#ref-{number}"{title_attr}>[{number}]</a>'
        return f'<span class="cite">{links}</span>' if links else ''

    def paragraph(self, value, node=None, *, css=''):
        parts = [part.strip() for part in _text(value).split('\n\n') if part.strip()]
        if not parts:
            return ''
        tail = self.refs(node) if node is not None else ''
        klass = f' class="{css}"' if css else ''
        return ''.join(
            f'<p{klass}>{_e(part)}{tail if index == len(parts) - 1 else ""}</p>' for index, part in enumerate(parts)
        )

    def cluster_chip(self, cluster_id, label=None):
        css = _PRIORITIES.get(self.cluster_priority.get(cluster_id), ('', 'p1'))[1]
        anchor = _cluster_anchor(cluster_id)
        text = _e(label or cluster_id)
        if anchor and cluster_id in self.cluster_ids:
            return f'<a class="as-cid pri-{css}" href="#{anchor}">{text}</a>'
        return f'<span class="as-cid pri-{css}">{text}</span>'

    def tag(self, row, *, microbiome=False):
        """One measured value as a tag: name, value, unit, direction; the dot carries the level."""
        fields = charts.microbiome_chart_fields(row) if microbiome else charts.clinical_chart_fields(row)
        name = _label(charts.display_name(clinical_observation_display_name(row) or row.get('name')))
        number = charts.parse_number(row.get('value'), row.get('unit'))
        value = ''
        if number is not None:
            value = f'<b>{_e(_text(row.get("value")).strip())}</b>'
            if row.get('unit'):
                value += f'<small>{_e(_unit(row["unit"]))}</small>'
        else:
            shown = _clinical_display(row) if not microbiome else _value(row)
            if shown and shown != name and len(shown) <= 18 and shown not in name:
                value = f'<b>{_e(shown)}</b>'
        if not name and not value:
            return ''
        return (
            f'<span class="as-tag lv-{_level_class(fields.get("level"))}"><i></i>{_e(name)}{" " if value else ""}'
            f'{value}{_arrow(fields.get("direction"))}</span>'
        )

    def tags(self, rows, *, microbiome=False):
        items = [self.tag(row, microbiome=microbiome) for row in rows or () if isinstance(row, dict)]
        unique = list(dict.fromkeys(item for item in items if item))
        return f'<div class="as-tags">{"".join(unique)}</div>' if unique else ''

    def value_line(self, row, *, named=True):
        """A clinical result: name, value, unit and direction, with a small reference bar under a number."""
        fields = charts.clinical_chart_fields(row)
        level, number = fields.get('level'), fields.get('valueNumber')
        name, shown = _row_label(row)
        css = f' class="lv-{level}"' if level in {charts.FOCUS, charts.BORDERLINE} else ''
        value = _text(row.get('value')).strip()
        if number is not None and re.fullmatch(r'[<>≤≥]?\s*[+-]?\d+(?:\.\d+)?\s*[↑↓]*', value):
            unit = _unit(row.get('unit'))
            result = (f'<span class="r"><b{css}>{_e(value)}</b>{" " + _e(unit) if unit else ""}'
                      f'{_arrow(fields.get("direction"))}</span>')
        elif shown and len(shown) > 14:
            result = f'<span class="tx">{_e(shown)}</span>{_arrow(fields.get("direction"))}'
        elif shown:
            result = f'<b{css}>{_e(shown)}</b>{_arrow(fields.get("direction"))}'
        else:
            result = _arrow(fields.get('direction'))
        if not named and not result:
            result = f'<b{css}>{_e(name)}</b>'
        label = f'<span class="nm">{_e(name)}</span> ' if named and name else ''
        if not label and not result:
            return ''
        bar = ''
        if isinstance(number, (int, float)):
            target = fields.get('target') if isinstance(fields.get('target'), dict) else None
            kind, low, high = fields.get('referenceKind'), fields.get('referenceLow'), fields.get('referenceHigh')
            aria = f'{name} {value}{_unit(row.get("unit"))}'
            if kind in {'range', 'upper', 'lower'} and (low is not None or high is not None):
                bar = _range_svg(number, low, high, kind, level, aria, target)
            elif target and (target.get('low') is not None or target.get('high') is not None):
                t_low, t_high = target.get('low'), target.get('high')
                t_kind = 'range' if t_low is not None and t_high is not None else (
                    'upper' if t_high is not None else 'lower')
                bar = _range_svg(number, t_low, t_high, t_kind, level, aria)
        return f'<div class="as-v">{label}{result}{bar}</div>'

    def values_cell(self, rows):
        rows = [row for row in rows or () if isinstance(row, dict) and (row.get('name') or row.get('value'))]
        lines = ''.join(self.value_line(row, named=len(rows) > 1 or not row.get('value')) for row in rows)
        return lines or _DASH

    def reference_cell(self, rows, group):
        """Each row's clinical reference range, then the group's ideal target as "理想：…".

        Callbacks without ``comparisonReference`` show the rows' own targets instead.
        """
        rows = [row for row in rows or () if isinstance(row, dict)]
        lines = []
        ranged = [row for row in rows if _text(row.get('referenceRange')).strip()]
        for row in ranged:
            name = _short_name(_row_label(row)[0]) if len(rows) > 1 else ''
            lines.append(f'<div>{_e(name + " " if name else "")}{_e(_label(row.get("referenceRange")))}</div>')
        ideal = _text(group.get('comparisonReference')).strip()
        if ideal:
            lines.append(f'<div class="ideal">理想：{_e(ideal)}</div>')
        elif 'comparisonReference' not in group:
            for row in rows:
                target = row.get('target')
                if not isinstance(target, dict):
                    continue
                text = _text(target.get('text')) or '–'.join(
                    _fmt(value) for value in (target.get('low'), target.get('high')) if value is not None
                )
                if text:
                    name = _short_name(_row_label(row)[0]) if len(rows) > 1 else ''
                    lines.append(f'<div class="ideal">理想：{_e(name + " " if name else "")}{_e(text)}</div>')
        return f'<div class="as-ref">{"".join(lines)}</div>' if lines else _DASH

    def group_status(self, group, rows=(), *, microbiome=False):
        level = group.get('level')
        if level not in charts.LEVEL_LABELS:
            fields = charts.microbiome_chart_fields if microbiome else charts.clinical_chart_fields
            level = charts.worst_level(fields(row).get('level') for row in rows if isinstance(row, dict))
        return _status_chip(group.get('status'), level) or _DASH

    # ----- front matter -----

    def preface(self):
        overview = self.overview_data
        intro = self.paragraph(overview.get('preface'))
        guide = ''.join(
            f'<li><strong>{_e(row.get("name"))}：</strong>{_e(row.get("text"))}</li>'
            for row in overview.get('readingGuide') or ()
            if isinstance(row, dict) and row.get('text')
        )
        if guide:
            guide = f'<p><strong>本报告阅读指南：</strong></p><ol class="as-small">{guide}</ol>'
        return intro + guide

    # ----- chapter one -----

    def body_measurements(self):
        clinical = self.clinical_data
        groups = [group for group in clinical.get('bodyGroups') or () if isinstance(group, dict)]
        if not groups:
            groups = [{'name': charts.display_name(row.get('name')), 'rows': [row]}
                      for row in clinical.get('bodyMetrics') or () if isinstance(row, dict) and row.get('name')]
        rows = []
        for group in groups:
            members = [row for row in group.get('rows') or () if isinstance(row, dict) and row.get('name')]
            reading = self.paragraph(group.get('summary'), group)
            if not members and not reading:
                continue
            rows.append(_tr((
                f'<td class="k">{_e(_label(group.get("name")))}</td><td>{self.values_cell(members)}</td>',
                f'<td>{self.reference_cell(members, group)}</td>',
                f'<td class="st">{self.group_status(group, members)}</td><td>{reading or _DASH}</td>',
            ), long=_is_long(len(members), group.get('summary'))))
        overview = self.overview_data
        summary = _text(overview.get('bodySummary')).strip()
        phenotype = _text(overview.get('phenotype')).strip()
        lead = ''
        if summary:
            parts = [part.strip() for part in summary.split('\n\n') if part.strip()]
            refs = self.refs({'evidenceRefs': overview.get('bodySummaryEvidenceRefs')})
            prefix = f'<strong>{_e(phenotype)}。</strong>' if phenotype and phenotype not in summary else ''
            lead = ''.join(
                f'<p class="as-lead">{prefix if index == 0 else ""}{_e(part)}{refs if index == len(parts) - 1 else ""}'
                '</p>'
                for index, part in enumerate(parts)
            )
        elif phenotype:
            lead = f'<p class="as-lead"><strong>{_e(phenotype)}</strong></p>'
        table = _table(
            ('评估指标', '实测数值', '参考基准 / 理想目标', '状态', '生理与代谢意义'), (15, 23, 19, 15, 28), rows,
        )
        if not table:
            return lead or _note('本次资料未包含体格测量。')
        return lead + table

    def system_rows(self):
        """(name, level, status, findings html, summary node) of each system; older callbacks are completed here."""
        provided = [row for row in self.overview_data.get('systems') or () if isinstance(row, dict)]
        if provided:
            items = [((row.get('code'), row.get('name'), None), row) for row in provided]
        else:
            definitions = list(_SYSTEMS)
            other_rows = [
                row for group in self.clinical_data.get('clinicalGroups') or ()
                if group.get('code') == _OTHER_SYSTEM[0] for row in group.get('rows') or ()
            ]
            if other_rows:
                definitions.append((_OTHER_SYSTEM[0], _OTHER_SYSTEM[1], (_OTHER_SYSTEM[0],)))
            items = [(definition, {}) for definition in definitions]
        result = []
        for (code, name, group_codes), row in items:
            fields = {key: row.get(key) for key in ('level', 'anchors', 'moreCount')}
            if fields['level'] is None and fields['anchors'] is None:
                codes = group_codes
                if codes is None:
                    codes = next((codes for known, _, codes in _SYSTEMS if known == code), (code,))
                atoms = charts.system_atoms(code, codes, self.clinical_data, self.microbiome_data)
                fields = charts.system_chart_fields(atoms, row.get('rows') or (), microbiome=code == 'microbiome')
            level = fields.get('level') or (charts.NOT_TESTED if not row else None)
            if not row and code == _OTHER_SYSTEM[0] and level == charts.NORMAL:
                continue
            name = row.get('name') or name or next((known for key, known, _ in _SYSTEMS if key == code), '')
            result.append((name, level, row, fields))
        return result

    def systems(self):
        rows = []
        for name, level, row, fields in self.system_rows():
            if level == charts.NOT_TESTED or _text(row.get('status')).strip() == '未检测':
                rows.append(f'<tr><td class="k">{_e(name)}</td><td class="st">{_status_chip("未检测")}</td>'
                            f'<td>{_DASH}</td><td>{_DASH}</td></tr>')
                continue
            findings = _text(row.get('keyFindings')).strip()
            if findings:
                findings = f'<p>{_e(findings)}</p>'
            else:
                anchors = ''.join(_anchor_tag(anchor, level) for anchor in fields.get('anchors') or ()
                                  if isinstance(anchor, dict) and anchor.get('name'))
                more = fields.get('moreCount') or 0
                if more:
                    where = '第四章' if row.get('code') == 'microbiome' else '第三章'
                    anchors += f'<span class="as-more">另有 {more} 项，见{where}</span>'
                findings = f'<div class="as-tags">{anchors}</div>' if anchors else ''
            verdict = self.paragraph(row.get('summary'), row)
            if not findings and not verdict and level == charts.NORMAL:
                verdict = '<p>已检项目均在参考范围内。</p>'
            chip = _status_chip(row.get('status'), level) or _DASH
            rows.append(
                f'<tr><td class="k">{_e(name)}</td><td class="st">{chip}</td>'
                f'<td>{findings or _DASH}</td><td>{verdict or _DASH}</td></tr>'
            )
        summary = self.paragraph(self.overview_data.get('summary'), self.overview_data, css='as-lead')
        table = _table(('系统', '综合状态', '核心异常与关键实测值', '当前阶段评估结论'), (17, 14, 38, 31), rows)
        return summary + table

    def item_facts(self, item):
        facts = []
        timeframe = (item.get('timeframe') or {}).get('text') if isinstance(item.get('timeframe'), dict) else None
        if timeframe:
            facts.append(('时限', timeframe))
        if item.get('department'):
            facts.append(('科室', item['department']))
        checks = [_text(check) for check in item.get('checks') or () if _text(check).strip()]
        if checks:
            facts.append(('需完成', '、'.join(checks)))
        if not facts:
            return ''
        return '<div class="as-facts">' + ''.join(
            f'<span class="as-fact"><small>{_e(label)}</small>{_e(value)}</span>' for label, value in facts
        ) + '</div>'

    def priorities(self):
        """1.3 in three parts: an urgent callout, a routine follow-up table and a table of tests to add."""
        findings = [row for row in self.overview_data.get('priorityFindings') or () if isinstance(row, dict)]
        sections = {row.get('code'): row for row in findings}
        urgent = sections.get('urgent') or {}
        items = [item for item in urgent.get('items') or () if isinstance(item, dict)]
        if items:
            blocks = ''.join(
                f'<div class="as-urgent"><h5>{_e(_label(item.get("name") or item.get("title")))}'
                f'{self.cluster_ref(item.get("clusterId"))}</h5>{self.item_facts(item)}'
                f'{self.paragraph(item.get("text"), item)}</div>'
                for item in items
            )
            html = f'<div class="as-callout urgent"><h4>临床优先排查</h4>{blocks}</div>'
        elif urgent.get('text') and 'items' not in urgent:
            # An older callback keeps the urgent finding as one paragraph.
            html = _callout('临床优先排查', self.paragraph(urgent.get('text'), urgent), css='urgent')
        else:
            html = _note(_text(urgent.get('text')) or '本次未发现需优先就医排查的结果。')
        routine = sections.get('routine') or {}
        items = [item for item in routine.get('items') or () if isinstance(item, dict)]
        if items:
            rows = []
            for item in items:
                name = _label(item.get('name') or item.get('title'))
                timeframe = item.get('timeframe') if isinstance(item.get('timeframe'), dict) else {}
                rows.append(
                    f'<tr><td class="k">{_e(name)}</td><td>{_e(item.get("department")) or _DASH}</td>'
                    f'<td>{_e(timeframe.get("text")) or _DASH}</td>'
                    f'<td>{self.paragraph(_strip_lead(item.get("text"), name), item) or _DASH}</td></tr>'
                )
            html += '<h4>常规专科随访</h4>' + _table(('发现', '科室', '复查间隔', '说明'), (24, 13, 15, 48), rows)
        elif routine.get('text'):
            html += '<h4>常规专科随访</h4>' + self.paragraph(routine.get('text'), routine)
        gaps = sections.get('gaps') or {}
        items = [item for item in gaps.get('items') or () if isinstance(item, dict)]
        if items:
            rows = []
            for item in items:
                name = _label(item.get('name') or item.get('title') or item.get('test'))
                reason = _strip_lead(item.get('text') or item.get('reason'), name)
                rows.append(f'<tr><td class="k">{_e(name)}</td><td>{self.paragraph(reason, item) or _DASH}</td></tr>')
            html += '<h4>建议补查</h4>' + _table(('建议补查', '依据'), (26, 74), rows)
        elif gaps.get('text'):
            html += '<h4>建议补查</h4>' + self.paragraph(gaps.get('text'), gaps)
        for row in findings:  # older callbacks may carry other finding codes
            if row.get('code') not in {'urgent', 'routine', 'gaps'} and (row.get('text') or row.get('action')):
                html += f'<h4>{_e(_label(row.get("name")))}</h4>' + self.paragraph(row.get('text') or row.get('action'),
                                                                                    row)
        return html

    def cluster_ref(self, cluster_id):
        if cluster_id and cluster_id in self.cluster_ids:
            return ' ' + self.cluster_chip(cluster_id, f'见集群 {cluster_id}')
        return ''

    def hub_lines(self):
        lines = []
        body_rows = [row for group in self.clinical_data.get('bodyGroups') or () for row in group.get('rows') or ()]
        body_rows = body_rows or list(self.clinical_data.get('bodyMetrics') or ())
        for pattern in (r'BMI|体重指数', r'内脏脂肪'):
            row = next((row for row in body_rows if re.search(pattern, _text(row.get('name')))), None)
            if row and charts.parse_number(row.get('value')) is not None:
                text = f'{charts.display_name(row["name"])} {_text(row["value"])} {_unit(row.get("unit"))}'
                lines.append(text.strip())
        for group in self.microbiome_data.get('structureGroups') or ():
            if group.get('code') != 'overallBalance':
                continue
            score = next((row for row in group.get('rows') or () if row.get('code') == 'microbiomeHealthScore'), None)
            grade = next((row for row in group.get('rows') or () if row.get('code') == 'balanceStatus'), None)
            if score:
                grade_text = f'（{_text(grade.get("value"))}）' if grade else ''
                lines.append(f'菌群总分 {_text(score.get("value"))}{grade_text}')
        return lines[:3]

    def node_anchors(self, cluster):
        """Up to two abnormal measured signals of a cluster, as short text for its graph node."""
        texts = []
        for row, microbiome in [*((row, False) for row in cluster.get('clinicalSignals') or ()),
                                *((row, True) for row in cluster.get('microbiomeSignals') or ())]:
            if _is_advice(row) or _is_personal(row):
                continue
            fields = charts.microbiome_chart_fields(row) if microbiome else charts.clinical_chart_fields(row)
            if fields.get('level') not in {charts.FOCUS, charts.BORDERLINE}:
                continue
            name = charts.display_name(clinical_observation_display_name(row) or row.get('name'))
            value = _text(row.get('value')).strip()
            text = f'{name} {value}' if value and len(value) <= 10 and value not in name else name
            if text not in texts:
                texts.append(text)
            if len(texts) == 2:
                break
        return texts

    def problem_graph(self):
        ids = self.overview_data.get('graphClusterIds')
        clusters = [item for item in self.core_clusters if ids is None or item.get('id') in ids] or self.core_clusters
        if not clusters:
            return _note('本次各系统未形成需集中干预的失衡方向。')
        nodes = [
            (_text(item.get('id')), _label(item.get('name')), item.get('priority'), self.node_anchors(item))
            for item in clusters
        ]
        hub = _text(self.overview_data.get('graphHub')) or '核心失衡态势'
        figure = f'<figure class="as-graph">{_graph_svg(hub, self.hub_lines(), nodes)}</figure>'
        lines = ''.join(
            f'<li>{self.cluster_chip(item.get("id"))}{_e(_label(item.get("name")))}'
            f'{_priority_chip(item.get("priority"))}</li>'
            for item in clusters
        )
        summary = self.paragraph(
            self.overview_data.get('graphSummary'), {'evidenceRefs': self.overview_data.get('graphSummaryEvidenceRefs')}
        )
        return figure + f'<ul class="as-clines">{lines}</ul>' + summary

    def overview(self):
        return (
            _subsection('1.1 基础体格与人体成分深度校准', self.body_measurements())
            + _subsection('1.2 多系统健康状态分级总览', self.systems())
            + _subsection('1.3 临床优先排查项与未检风险提示', self.priorities())
            + _subsection('1.4 核心系统失衡态势速览', self.problem_graph())
        )

    # ----- chapter two -----

    def symptoms(self):
        clinical = self.clinical_data
        symptoms = [item for item in clinical.get('symptoms') or ()
                    if isinstance(item, dict) and (item.get('name') or item.get('value'))]
        lead = self.paragraph(clinical.get('symptomsLead'))
        summary = self.paragraph(
            clinical.get('symptomSummary'), {'evidenceRefs': clinical.get('symptomSummaryEvidenceRefs')}
        )
        if not symptoms:
            return _subsection(
                '2.1 主诉症状谱与临床检验信号印证', lead + summary or _note('本次未记录主诉症状。')
            )
        rows, readings = [], []
        # The microbiome column is dropped only when the request carried no microbiome report at all.
        gut = self.microbiome_data
        with_microbiome = bool(gut.get('structureGroups') or gut.get('organismGroups')) or any(
            item.get('microbiomeEvidence') or item.get('microbiomeSignals') for item in symptoms
        )
        for index, item in enumerate(symptoms, start=1):
            code = _text(item.get('code')) or f'S{index}'
            name = _text(item.get('name') or item.get('value'))
            context = _answers_html(item.get('history'))
            if item.get('value') and _text(item.get('value')) not in name:
                context = f'<p>表现：{_e(item["value"])}</p>' + context
            signals = [row for row in item.get('clinicalSignals') or () if isinstance(row, dict)]
            personal = [row for row in signals if _is_personal(row)]
            context += ''.join(
                f'<p>{_e(_prompt(row.get("name")))}：{_e(row.get("value"))}</p>'
                for row in personal if row.get('value') and _text(row.get('value')) not in context
            )
            observed = [row for row in signals if row not in personal and not _is_advice(row)]
            clinical_text = self.paragraph(item.get('clinicalEvidence')) or self.tags(observed)
            microbiome_text = (self.paragraph(item.get('microbiomeEvidence'))
                               or self.tags(item.get('microbiomeSignals'), microbiome=True))
            last = f'<td>{microbiome_text or _DASH}</td>' if with_microbiome else ''
            rows.append(
                f'<tr><td class="k"><span class="as-cid">{_e(code)}</span>{_e(name)}</td><td>{context or _DASH}</td>'
                f'<td>{clinical_text or _DASH}</td>{last}</tr>'
            )
            body = self.paragraph(item.get('interpretation'), item)
            if body:
                title = _text(item.get('interpretationTitle')).strip() or name
                readings.append(f'<li><strong>{_e(title)}</strong>{body}</li>')
        heads = ('主诉', '持续时间与加重情境', '临床检验与影像关联信号', '菌群与功能代谢关联证据')
        table = _table(heads, (15, 25, 30, 30), rows) if with_microbiome else _table(heads[:3], (18, 34, 48), rows)
        details = f'<ol class="as-readings">{"".join(readings)}</ol>' if readings else ''
        details += _callout('综合印证', summary)
        return (
            _subsection('2.1 主诉症状谱与临床检验信号印证', lead + table)
            + _subsection('2.2 躯体感知背后的微生态与代谢线索', details)
        )

    # ----- chapter three -----

    def clinical_rows(self, group):
        """Table rows of one system: a template row per clinical question, otherwise one per recorded result."""
        rows = []
        templates = [row for row in group.get('templateRows') or () if isinstance(row, dict)]
        if not any(template.get('rows') or template.get('summary') for template in templates):
            templates = [{'name': _row_label(row)[0] or row.get('name'), 'rows': [row],
                          'summary': row.get('interpretation')}
                         for row in group.get('rows') or () if isinstance(row, dict) and row.get('name')]
        for template in templates:
            members = [row for row in template.get('rows') or ()
                       if isinstance(row, dict) and (row.get('name') or row.get('value'))]
            reading = self.paragraph(template.get('summary'), template)
            if not members and not reading:
                continue
            texts = [template.get('summary'), *(row.get('value') for row in members)]
            rows.append(_tr((
                f'<td class="k">{_e(_label(template.get("name")))}</td><td>{self.values_cell(members)}</td>',
                f'<td>{self.reference_cell(members, template)}</td>',
                f'<td class="st">{self.group_status(template, members)}</td><td>{reading or _DASH}</td>',
            ), long=_is_long(len(members), *texts)))
        return rows

    def clinical(self):
        clinical = self.clinical_data
        groups = [group for group in clinical.get('clinicalGroups') or () if isinstance(group, dict)]
        order = {code: index for index, code in enumerate(_CLINICAL_ORDER)}
        groups.sort(key=lambda group: order.get(group.get('code'), len(order)))
        sections = []
        for group in groups:
            rows = self.clinical_rows(group)
            table = _table(
                ('检验 / 影像项目', '实测结果', '临床参考范围（理想目标）', '状态', '解读'), (15, 23, 19, 15, 28), rows,
            )
            note = _callout('数据校准说明', self.paragraph(group.get('calibrationNote')))
            reading = self.paragraph(group.get('summary'), group)
            if not (table or reading):
                continue
            title = _label(group.get('name')) or _CLINICAL_TITLES.get(group.get('code'), '临床检查')
            sections.append((title, group.get('code'), table + note + reading))
        html = self.paragraph(clinical.get('summary'), clinical, css='as-lead')
        for number, (title, code, content) in enumerate(sections, start=1):
            html += _subsection(f'3.{number} {title}', content, anchor=f'as-system-{code}' if code else None)
        number = len(sections)
        html += _subsection(f'3.{number + 1} 本次未纳入的专项检测', self.specialty())
        html += _subsection(f'3.{number + 2} 历年指标演变趋势与已确认稳健安全基线', self.trends())
        return html

    def specialty(self):
        clinical = self.clinical_data
        content = self.paragraph(clinical.get('specialtySummary'), {
            'evidenceRefs': clinical.get('specialtySummaryEvidenceRefs')
        })
        items = []
        for row in clinical.get('specialtyFindings') or ():
            if not isinstance(row, dict):
                continue
            name, shown = _row_label(row)
            parent = _text(row.get('name')).partition(' · ')[0] if ' · ' in _text(row.get('name')) else ''
            if parent and parent != name:
                name = f'{parent}：{name}' if name else parent
            text = f'{name}：{shown}' if name and shown else name or shown
            if text:
                items.append(f'<li>{_e(text)}</li>')
        if items:
            content += f'<h4>其他专项记录</h4><ul class="as-plain as-small">{"".join(items)}</ul>'
        return content or _note('本次资料未包含设备专项检测与筛查项目。')

    def trends(self):
        clinical = self.clinical_data
        cards = []
        for item in clinical.get('trends') or ():
            if not isinstance(item, dict):
                continue
            chart = _trend_svg(item.get('points') or (), _text(item.get('name')))
            if not chart:
                continue
            reading = self.paragraph(item.get('interpretation'), item)
            cards.append(
                f'<div class="as-trend"><h4>{_e(item.get("name"))} <small class="as-muted">{_e(item.get("direction"))}'
                f'</small></h4>{chart}{reading}</div>'
            )
        trend = f'<div class="as-trends">{"".join(cards)}</div>' if cards else ''
        trend += self.paragraph(
            clinical.get('trendSummary'), {'evidenceRefs': clinical.get('trendSummaryEvidenceRefs')}
        )
        if not trend:
            trend = _note('本次资料未包含可比较的历次结果。')
        baselines = self.paragraph(clinical.get('baselineSummary'), {
            'evidenceRefs': clinical.get('baselineSummaryEvidenceRefs')
        })
        for group in clinical.get('normalBaselines') or ():
            if not isinstance(group, dict):
                continue
            items = []
            for row in group.get('rows') or ():
                name, shown = _row_label(row)
                text = f'{name} {shown}' if name and shown and len(shown) <= 24 else name or shown
                if text:
                    items.append(text)
            if items:
                baselines += f'<p class="as-small"><strong>{_e(_label(group.get("name")))}：</strong>' \
                             f'{_e("；".join(dict.fromkeys(items)))}</p>'
        return trend + _callout('已确认的稳健基线', baselines)

    # ----- chapter four -----

    def microbiome(self):
        gut = self.microbiome_data
        has_data = any(gut.get(key) for key in ('structureGroups', 'structure', 'organismGroups', 'functionalGroups'))
        lead = self.paragraph(gut.get('overviewSummary'), {'evidenceRefs': gut.get('overviewSummaryEvidenceRefs')})
        if not has_data:
            # Without a microbiome report the chapter is one statement; the digestive
            # examinations are shown here only when the payload still lists them.
            groups = self.clinical_data.get('digestiveContext') or gut.get('digestiveContext')
            digestive = _subsection('4.1 消化道器质基础与消化液微环境', self.digestive()) if groups else ''
            return (lead or _note('本次资料未包含肠道菌群检测。')) + digestive
        digestive = _subsection('4.1 消化道器质基础与消化液微环境', self.digestive())
        notes = ''
        sample = self.paragraph(gut.get('sampleSummary'), {'evidenceRefs': gut.get('sampleSummaryEvidenceRefs')})
        if sample:
            notes += sample.replace('<p>', '<p><strong>采样基线：</strong>', 1)
        fermentation = self.paragraph(gut.get('fermentationSummary'), {
            'evidenceRefs': gut.get('fermentationSummaryEvidenceRefs')
        })
        if fermentation:
            notes += fermentation.replace('<p>', '<p><strong>发酵耐受：</strong>', 1)
        risk = self.paragraph(gut.get('riskSummary'), {'evidenceRefs': gut.get('riskSummaryEvidenceRefs')})
        return (
            lead
            + _callout('样本基线与发酵耐受', notes)
            + digestive
            + _subsection(self.structure_heading(), self.structure())
            + _subsection('4.3 核心功能菌群结构、条件致病谱与毒力因子', self.organisms())
            + _subsection('4.4 菌群营养合成与外源毒素代谢潜能', self.functions() + risk)
        )

    def structure_heading(self):
        codes = {group.get('code') for group in self.microbiome_data.get('structureGroups') or ()}
        if codes & {'developmentalMaturity', 'childGrowth'}:
            return '4.2 微生态宏观稳态指数与儿童相关菌群'
        if 'gutAge' in codes:
            return '4.2 微生态宏观稳态指数与肠道生物学年龄'
        return '4.2 微生态宏观稳态指数'

    def digestive(self):
        rows = []
        groups = self.clinical_data.get('digestiveContext') or self.microbiome_data.get('digestiveContext') or ()
        for group in groups:
            if not isinstance(group, dict):
                continue
            members = [row for row in _digestive_measurements(group) if isinstance(row, dict) and row.get('name')]
            results = ''.join(self.value_line(row) for row in members)
            context = '；'.join(
                f'{_prompt(row.get("name"))}：{_text(row.get("value"))}' for row in group.get('symptomContext') or ()
                if isinstance(row, dict) and row.get('value')
            )
            if context:
                results += f'<p class="as-small as-muted">自述：{_e(context)}</p>'
            reading = self.paragraph(group.get('summary'), group)
            if not results and not reading:
                continue
            rows.append(f'<tr><td class="k">{_e(_label(group.get("name")))}</td><td>{results or _DASH}</td>'
                        f'<td>{reading or _DASH}</td></tr>')
        table = _table(('消化道环节', '相关检查结果', '对菌群定植与消化吸收的影响'), (20, 34, 46), rows)
        return table or _note('本次资料未包含口腔、胃肠与肝胆胰的相关检查。')

    def index_line(self, row, group, *, named):
        """One macro index: value with direction, and its grade bands, reference band or plain axis."""
        fields = charts.microbiome_chart_fields(row)
        level = fields.get('level')
        number = fields.get('valueNumber')
        if number is None:
            number = fields.get('abundance')
        if number is None:
            number = charts.parse_number(row.get('value'))
        name = _label(charts.display_name(row.get('name')))
        value = _text(row.get('value')).strip()
        label = f'{name} {value}'
        bands = fields.get('bands') or []
        reference = ''
        chart = ''
        if row.get('code') == 'gutAge' and number is not None:
            actual = row.get('actualAge', group.get('actualAge'))
            if not isinstance(actual, (int, float)):
                actual = charts.parse_number(re.sub(r'[^\d.]+', ' ', _text(row.get('referenceRange'))).strip() or None)
            difference = row.get('ageDifference', group.get('gutAgeDifference'))
            if isinstance(actual, (int, float)):
                lo, hi = min(number, actual) - 10, max(number, actual) + 10
                chart = _axis_svg(number, lo, hi, [(actual, f'实足 {_fmt(actual)} 岁')], level, label, marker=actual)
                reference = f'实足年龄 {_fmt(actual)} 岁'
                if isinstance(difference, (int, float)):
                    reference += f'（相差 {"+" if difference > 0 else ""}{_fmt(difference)} 岁）'
        elif bands and number is not None:
            chart = _bands_svg(number, bands, level, label)
            reference = '；'.join(f'{band["label"]} {_band_text(band)}' for band in bands)
        elif charts.microbiome_kind(row) == 'abundance' and number is not None:
            chart = _abundance_svg(number, fields.get('referenceLow'), fields.get('referenceHigh'), level, label)
            reference = _label(row.get('referenceRange'))
        elif number is not None and fields.get('referenceKind') in {'range', 'upper', 'lower'}:
            chart = _range_svg(number, fields['referenceLow'], fields['referenceHigh'], fields['referenceKind'], level,
                               label)
            reference = _label(row.get('referenceRange'))
        elif number is not None and 0 <= number <= 100 and re.search(r'评分|得分|总分', _text(row.get('name'))):
            chart = _axis_svg(number, 0, 100, [(0, '0'), (50, '50'), (100, '100')], level, label)
        else:
            reference = _label(row.get('referenceRange'))
        css = f' class="lv-{level}"' if level in {charts.FOCUS, charts.BORDERLINE} else ''
        unit = _unit(row.get('unit'))
        shown = f'<b{css}>{_e(value)}</b>{" " + _e(unit) if unit and number is not None else ""}'
        shown += _arrow(fields.get('direction'))
        prefix = f'<span class="nm">{_e(name)}</span> ' if named else ''
        line = f'<div class="as-v wide">{prefix}{shown}{chart}</div>'
        ref = f'<div>{_e(name + " " if named else "")}{_e(reference)}</div>' if reference else ''
        return line, ref

    def structure(self):
        gut = self.microbiome_data
        rows = []
        for group in gut.get('structureGroups') or ():
            if not isinstance(group, dict):
                continue
            members = [row for row in group.get('rows') or () if isinstance(row, dict) and row.get('name')]
            lines = [self.index_line(row, group, named=len(members) > 1) for row in members]
            reading = self.paragraph(group.get('summary'), group)
            if not lines and not reading:
                continue
            results = ''.join(line for line, _ in lines) or _DASH
            references = ''.join(ref for _, ref in lines)
            status = self.group_status({'status': group.get('status')}, members, microbiome=True)
            rows.append(
                f'<tr><td class="k">{_e(_label(group.get("name")))}</td><td>{results}</td>'
                f'<td>{f"<div class=as-ref>{references}</div>" if references else _DASH}</td>'
                f'<td class="st">{status}</td><td>{reading or _DASH}</td></tr>'
            )
        table = _table(('评估维度', '检测结果', '参考基准', '状态', '解读'), (15, 28, 16, 15, 26), rows)
        summary = self.paragraph(gut.get('structureSummary'), {'evidenceRefs': gut.get('structureSummaryEvidenceRefs')})
        return table + summary or _note('本次菌群检测未包含宏观稳态指数。')

    def taxon_line(self, row):
        fields = charts.microbiome_chart_fields(row)
        level = fields.get('level')
        name = _label(charts.display_name(row.get('name')))
        abundance = fields.get('abundance')
        value = _text(row.get('value')).strip()
        css = f' class="lv-{level}"' if level in {charts.FOCUS, charts.BORDERLINE} else ''
        if abundance is not None and abundance <= 0:
            shown = f'<b{css}>0</b> %<small>（未检出）</small>'
        else:
            shown = f'<b{css}>{_e(value)}</b> {_e(row.get("unit"))}'
        shown += _arrow(fields.get('direction'))
        reference = _text(row.get('referenceRange')).strip()
        if reference:
            shown += f' <small>参考 {_e(reference)}</small>'
        chart = _abundance_svg(abundance, fields.get('referenceLow'), fields.get('referenceHigh'), level,
                               f'{name} {value}%，参考 {reference}', width=110)
        return f'<div class="as-tx"><span><span class="nm">{_e(name)}</span> {shown}</span>{chart}</div>'

    def organisms(self):
        rows = []
        for group in self.microbiome_data.get('organismGroups') or ():
            if not isinstance(group, dict):
                continue
            members = [row for row in group.get('rows') or () if isinstance(row, dict) and row.get('name')]
            taxa = [row for row in members if charts.microbiome_kind(row) == 'abundance']
            panels = [row for row in members if row not in taxa]
            abnormal = self.tags(panels, microbiome=True) + ''.join(self.taxon_line(row) for row in taxa)
            baseline = group.get('normalBaseline') or {}
            normal_rows = [row for row in baseline.get('rows') or () if isinstance(row, dict)]
            names = baseline.get('names') or [row.get('name') for row in normal_rows]
            count = baseline.get('count') if type(baseline.get('count')) is int else len(names)
            normal = ''
            if count:
                examples = [f'{_label(row.get("name"))} {_value(row)}'.strip() for row in normal_rows[:4]]
                examples = examples or [_label(name) for name in names[:4]]
                more = '等' if count > len(examples) else ''
                normal = f'<p><strong>{count} 项在参考范围内</strong>：{_e("、".join(examples))}{more}</p>'
            undetected = [row.get('name') for row in baseline.get('undetectedRows') or () if row.get('name')]
            if undetected:
                normal += f'<p>未检出序列信号：{_e("、".join(undetected))}</p>'
            reading = self.paragraph(group.get('summary'), group)
            if not abnormal and not normal and not reading:
                continue
            rows.append(_tr((
                f'<td class="k">{_e(_label(group.get("name")))}</td><td>{abnormal or _DASH}</td>',
                f'<td>{normal or _DASH}</td><td>{reading or _DASH}</td>',
            ), long=_is_long(len(members), group.get('summary'))))
        table = _table(
            ('类别', '异常菌种 / 基因及方向', '已确认正常的部分', '功能影响与调节意义'), (15, 35, 21, 29), rows,
        )
        return table or _note('本次菌群检测未包含功能菌群与病原谱。')

    def functions(self):
        rows = []
        for group in self.microbiome_data.get('functionalGroups') or ():
            if not isinstance(group, dict):
                continue
            baseline = group.get('normalBaseline') or {}
            seen, scored = set(), []
            for row in [*(group.get('rows') or ()), *(baseline.get('rows') or ())]:
                if not isinstance(row, dict):
                    continue
                identity = (_text(row.get('name')), _text(row.get('value')))
                if identity in seen:
                    continue
                seen.add(identity)
                fields = charts.microbiome_chart_fields(row)
                if fields.get('score') is not None:
                    scored.append((row, fields))
            deviating = [(row, fields) for row, fields in scored if fields.get('level') in {charts.FOCUS,
                                                                                            charts.BORDERLINE}]
            normal = [(row, fields) for row, fields in scored if (row, fields) not in deviating]
            abnormal = ''.join(self.score_row(row, fields) for row, fields in deviating)
            normal_text = '、'.join(f'{_label(row.get("name"))} {_fmt(fields["score"])}' for row, fields in normal)
            if not normal_text:
                normal_text = '、'.join(_label(name) for name in baseline.get('names') or ())
            basis = next((band for _, fields in scored for band in fields.get('bands') or ()
                          if charts.is_normal_label(band.get('label'))), None)
            normal_html = f'<p>{_e(normal_text)}</p>' if normal_text else ''
            if basis:
                normal_html += f'<p class="as-muted">基准 {_e(_band_text(basis))}</p>'
            reading = self.paragraph(group.get('summary'), group)
            if not abnormal and not normal_html and not reading:
                continue
            title = re.sub(r'^\d+\.\s*', '', _text(group.get('name')))
            rows.append(
                f'<tr><td class="k">{_e(title)}</td><td>{abnormal or "<p>无偏离子项</p>"}</td>'
                f'<td>{normal_html or _DASH}</td><td>{reading or _DASH}</td></tr>'
            )
        table = _table(
            ('代谢通路', '异常子项及评分', '通路内正常子项', '与临床指标及症状的交叉印证'), (15, 33, 20, 32), rows,
        )
        if not table:
            return _note('本次菌群检测未包含营养合成与代谢潜能评分。')
        return f'<p>{_e(_MICROBIOME_INDEX_NOTE)}</p>' + table

    def score_row(self, row, fields):
        level = fields.get('level')
        name = _label(row.get('name'))
        score = fields['score']
        bands = fields.get('bands') or [{'label': '正常', 'low': 0.0, 'high': 100.0}]
        chart = _score_svg(score, bands, level, f'{name} {_fmt(score)} 分')
        return (
            f'<div class="as-srow"><span class="nm">{_e(name)}</span>{chart}'
            f'<span class="v"><b>{_e(_fmt(score))}</b>{_arrow(fields.get("direction"))}</span></div>'
        )

    # ----- chapter five -----

    def cluster_block(self, item):
        identifier = _text(item.get('id'))
        anchor = _cluster_anchor(item.get('id'))
        clinical = [row for row in item.get('clinicalSignals') or () if isinstance(row, dict)]
        observations = [row for row in clinical if not _is_personal(row) and not _is_advice(row)]
        clinical_html = self.paragraph(item.get('clinicalEvidence'))
        if not clinical_html:
            clinical_html = self.tags(observations)
            advice = _recorded_advice(item)
            if advice:
                clinical_html += ''.join(f'<p>检查建议：{_e(quote)}</p>' for quote in advice)
        microbiome_html = (self.paragraph(item.get('microbiomeEvidence'))
                           or self.tags(item.get('microbiomeSignals'), microbiome=True))
        action = item.get('action') or next(
            (row.get('action') for row in self.conclusion_data.get('priorities') or ()
             if isinstance(row, dict) and row.get('id') == item.get('id') and row.get('action')), None,
        )
        parts = (
            ('临床与影像证据', clinical_html),
            ('菌群与代谢证据', microbiome_html),
            ('对身体的影响与走向', self.paragraph(item.get('interpretation'), item)),
            ('处理方向', self.paragraph(action)),
        )
        body = ''.join(f'<div><h5>{_e(title)}</h5>{content}</div>' for title, content in parts if content)
        root = f' id="{anchor}"' if anchor else ''
        heading = f'集群 {identifier}：' if identifier else ''
        return (
            f'<article class="as-cluster"{root}><h4>{_e(heading)}{_e(_label(item.get("name")))}'
            f'{_priority_chip(item.get("priority"))}</h4>{body}</article>'
        )

    def clusters(self):
        summary = self.paragraph(
            self.data.get('clusterSummary'), {'evidenceRefs': self.data.get('clusterSummaryEvidenceRefs')}
        )
        blocks = ''.join(self.cluster_block(item) for item in self.core_clusters)
        if not blocks:
            blocks = _note('未形成需集中干预的失衡集群。')
        return (
            _subsection('5.1 个体化系统失衡集群定性与健康影响解读', summary + blocks)
            + _subsection('5.2 ATM 健康诱因全景复盘与健康认知校准', self.lifestyle())
        )

    def lifestyle(self):
        factors = [factor for factor in self.data.get('lifestyleFactors') or () if isinstance(factor, dict)]
        background = [factor for factor in factors if factor.get('kind') in _BACKGROUND_KINDS]
        habits = [factor for factor in factors if factor not in background]
        history = _text(self.data.get('lifestyleSummary')).strip()
        content = ''
        if history:
            refs = self.refs({'evidenceRefs': self.data.get('lifestyleSummaryEvidenceRefs')})
            content += f'<p><strong>先天背景与既往事件：</strong>{_e(history)}{refs}</p>'
        for factor in background:
            content += f'<p><strong>{_e(factor.get("name"))}：</strong>{_e(factor.get("value"))}</p>'
            content += self.paragraph(factor.get('interpretation'), factor)
        rows = []
        for factor in habits:
            behaviour = _answers_html(factor.get('value'))
            ids = ''.join(self.cluster_chip(identifier) for identifier in factor.get('clusterIds') or ())
            effect = self.paragraph(factor.get('interpretation'), factor)
            if ids:
                effect += f'<div class="as-facts">{ids}</div>'
            if not behaviour and not effect:
                continue
            rows.append(f'<tr><td class="k">{_e(factor.get("name"))}</td><td>{behaviour or _DASH}</td>'
                        f'<td>{effect or _DASH}</td></tr>')
        table = _table(('生活方式维度', '行为特征', '正在维持的异常与对应集群'), (17, 33, 50), rows)
        content += table or _note('本次未记录可对照的日常生活方式信息。')
        recognition = ''.join(
            f'<li><strong>{_e(row.get("name"))}</strong>{self.paragraph(row.get("text"), row)}</li>'
            for row in self.data.get('lifestyleRecognition') or () if isinstance(row, dict) and row.get('text')
        )
        if recognition:
            content += _callout('健康认知校准', f'<ol>{recognition}</ol>')
        return content

    # ----- chapter six -----

    def stage_columns(self):
        layers = {code: [] for code, *_ in _STAGES}
        for layer in self.conclusion_data.get('layers') or ():
            if isinstance(layer, dict):
                layers[_LAYER_STAGES.get(layer.get('code'), 'maintenance')].append(layer)
        placed = {
            identifier for group in layers.values() for layer in group for identifier in layer.get('clusterIds') or ()
        }
        extra = {code: [] for code in layers}
        for cluster in self.core_clusters:  # older callbacks without layers place a cluster by its priority
            if cluster.get('id') not in placed:
                extra[_PRIORITY_STAGES.get(cluster.get('priority'), 'maintenance')].append(cluster.get('id'))
        columns = []
        for code, level_name, timeframe, title, empty in _STAGES:
            ids = [identifier for layer in layers[code] for identifier in layer.get('clusterIds') or ()] + extra[code]
            for layer in layers[code]:
                if match := _LAYER_NAME.match(_text(layer.get('name'))):
                    level_name, title = match['level'], match['title']
                    break
            chips = ''.join(self.cluster_chip(identifier) for identifier in dict.fromkeys(ids) if identifier)
            text = ''.join(self.paragraph(layer.get('text'), layer) for layer in layers[code])
            columns.append(
                f'<section class="as-stage {code}"><header><div class="t"><b>{_e(timeframe)}</b>'
                f'<span>{_e(level_name)}</span></div><h4>{_e(title)}</h4></header>'
                f'{f"<div class=ids>{chips}</div>" if chips else ""}{text or f"<p>{_e(empty)}</p>"}</section>'
            )
        return f'<div class="as-roadmap" aria-label="三阶段干预路线">{"".join(columns)}</div>'

    def conclusion(self):
        conclusion = self.conclusion_data
        summary = self.paragraph(conclusion.get('summary'), conclusion, css='as-lead')
        directions = []
        urgent = any(item.get('priority') == 'P0' for item in self.core_clusters) or any(
            isinstance(item, dict) and item.get('priority') == 'P0'
            for section in self.overview_data.get('priorityFindings') or () for item in section.get('items') or ()
        )
        for row in conclusion.get('managementDirections') or ():
            if not isinstance(row, dict):
                continue
            children = ''.join(
                f'<li><strong>{_e(child.get("name"))}</strong>{self.paragraph(child.get("text"), child)}</li>'
                for child in row.get('items') or () if isinstance(child, dict) and child.get('text')
            )
            text = self.paragraph(row.get('text'), row)
            title = _text(row.get('name'))
            if row.get('items') is not None and not title.startswith('MTPN'):
                title = f'MTPN 个性化功能靶向配方方向（{title}）' if title else 'MTPN 个性化功能靶向配方方向'
            elif row.get('code') == 'lifestyle' and not title.startswith('LSMI'):
                title = f'LSMI 去诱因生活方式干预方向（{title}）' if title else 'LSMI 去诱因生活方式干预方向'
            if children or text:
                listed = f'<ol>{children}</ol>' if children else ''
                directions.append(f'<div class="as-direction"><h4>{_e(title)}</h4>{text}{listed}</div>')
        management = ''.join(directions)
        if urgent and '第一层级' not in management:
            management = '<p class="as-missing">先完成第一层级的专科排查，再开始配方与生活方式干预。</p>' + management
        if not directions:
            management += _note('本次资料不足以形成配方与生活方式干预方向。')
        return (
            _subsection('6.1 核心健康风险分层总结', summary + self.stage_columns())
            + _subsection('6.2 全周期微生态与生活方式协同管理方向', management)
        )

    # ----- appendix -----

    def reference_appendix(self):
        # Works behind a printed citation mark, and the sources of the ideal targets
        # (carried on the result rows, shown without a mark), in one continuous list.
        if not self.references:
            return ''
        note = '正文方括号中的编号与下列文献对应。'
        if set(self.references) - set(self.shown_refs):
            note += '未在正文中标注编号的文献是相应指标理想目标的依据。'
        rows = []
        for number in sorted(self.references):
            row = self.references[number]
            fields = [row.get('title'), row.get('year')]
            if row.get('doi'):
                fields.append('DOI：' + _text(row['doi']))
            metadata = '；'.join(_e(value) for value in fields if value is not None and value != '')
            link = _reference_link(row)
            original = (
                f' <a href="{link}" target="_blank" rel="noopener noreferrer">原文链接</a>' if link else ''
            )
            rows.append(f'<li id="ref-{number}" value="{number}">{metadata}{original}</li>')
        return (
            '<section class="as-references" id="as-references" aria-labelledby="as-references-title">'
            '<h2 id="as-references-title">附录：参考文献</h2>'
            f'<p class="as-small as-muted">{note}</p>'
            f'<ol class="as-reference-list">{"".join(rows)}</ol></section>'
        )


def assessment_cover(body, presentation):
    """Header: title, subject line, the two source dates and one line on their interval."""
    subject = body.get('subject') or {}
    sample = body.get('sample') or {}
    timing = (presentation.get('overview') or {}).get('dataTiming') or {}
    context = (presentation.get('microbiome') or {}).get('sampleContext') or {}
    age = subject.get('age')
    clinical_date = _text(timing.get('clinicalDate'))
    institution = _text(timing.get('clinicalInstitution'))
    sampled = _text(timing.get('microbiomeSampleDate') or sample.get('sampleDate') or context.get('sampleDate'))
    received = _text(timing.get('microbiomeReceivedDate') or context.get('receivedDate'))
    reported = _text(timing.get('microbiomeReportDate') or sample.get('reportDate') or context.get('reportDate'))
    gut_date, gut_label = (sampled, '肠道菌群采样日') if sampled else (
        (received, '肠道菌群收样日') if received else (reported, '肠道菌群报告日'))
    identity = [
        ('受检者', subject.get('name')),
        ('性别 / 年龄', ' · '.join(_text(value) for value in (subject.get('gender'),
                                                              f'{age} 岁' if age not in (None, '') else '') if value)),
        ('评估档案编码', sample.get('sampleId')),
    ]
    sep = '<span class="sep">|</span>'
    lines = [sep.join(f'<b>{_e(label)}：</b>{_e(value)}' for label, value in identity if _text(value).strip())]
    dates = []
    if clinical_date:
        dates.append(f'<b>临床体检基准日：</b>{_e(clinical_date)}{f"（{_e(institution)}）" if institution else ""}')
    if gut_date:
        method = _text(sample.get('method'))
        dates.append(f'<b>{_e(gut_label)}：</b>{_e(gut_date)}{f"（{_e(method)}）" if method else ""}')
    lines.append(sep.join(dates))
    days = _days_between(clinical_date, gut_date) if clinical_date and gut_date else None
    if days is not None:
        months = days / 30.44
        basis = '' if sampled else '（按收样日期计算）' if gut_date == received else '（按报告日期计算）'
        if months <= 6:
            verdict = '两份资料属同一健康周期，可交叉印证'
        else:
            verdict = '两份资料间隔超过 6 个月，结论以较新的资料为准'
        lines.append(f'<b>资料时效：</b>临床体检与肠道菌群检测间隔约 {_fmt(round(months, 1))} 个月{_e(basis)}，'
                     f'{_e(verdict)}。')
    body_html = ''.join(f'<p>{line}</p>' for line in lines if line)
    title = presentation.get('title') or '个体化全息健康与肠道微生态深度评估报告'
    ident = f'<div class="as-ident">{body_html}</div>' if body_html else ''
    return f'<header class="as-cover"><h1>{_e(title)}</h1>{ident}</header>'


def assessment_chapters(data, presentation, *, status=None, partial_reasons=()):
    renderer = _AssessmentRenderer(data, presentation, status=status, partial_reasons=partial_reasons)
    chapters = []
    for anchor, title, method_name in ASSESSMENT_SECTIONS:
        content = getattr(renderer, method_name)()
        section = f'<section class="chapter" id="{anchor}"><h2>{_e(title)}</h2>{content}</section>'
        chapters.append((anchor, title, section))
    if chapters and (appendix := renderer.reference_appendix()):
        anchor, title, section = chapters[-1]
        chapters[-1] = (anchor, title, section + appendix)
    return chapters
