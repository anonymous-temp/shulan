"""Literal PDF reader for the same evaluate-only metagenomic template.

The PDF and JSON readers converge at the template mapping, not at the patient's
data. This module only reads explicit result cells in the supplied document;
it never opens a JSON companion, copies a clinical narrative, or calls a model.
Unrecognised PDFs remain the responsibility of the existing document service.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import replace
from decimal import Decimal

from pydantic import Field

from .evaluation_source_recovery import EvaluationHealthSummaryMetric
from .schemas import ReportRequest
from .schemas import SampleInfo
from .schemas import SubjectInfo


class EvaluationTemplatePdfFunctionMetric(EvaluationHealthSummaryMetric):
    """PDF function cells retain real page provenance and pathway membership."""

    pathway_code: str = Field(exclude=True)
    source_path: str | None = Field(default=None, exclude=True)


class EvaluationTemplatePdfStructureMetric(EvaluationHealthSummaryMetric):
    reference_source: str | None = Field(default=None, exclude=True)


_SECTIONS = {
    '2.1': ('jun_yyj', '有益菌'),
    '2.2': ('jun_zbj', '致病菌'),
    '2.2.1': ('jun_zbj', '致病菌'),
    '2.2.2': ('jun_tjzbj', '条件致病菌'),
    '2.2.3': ('jun_zj', '致病性真菌'),
    '2.2.4': ('jun_bd', '病毒'),
    '2.2.5': ('jun_jsc', '致病性寄生虫'),
    '2.3': ('jun_ds', '产短链脂肪酸的菌'),
    '2.4': ('jun_hxjs', '肠道基石菌构成'),
    '2.5': ('jun_pzss', '肠道屏障损伤相关菌评估'),
    '3.1': ('jkj_ganran', '胃肠道感染'),
    '3.2': ('jkj_IBD', '炎症性肠病'),
    '3.3': ('jkj_IBS', '肠易激综合征'),
    '3.4': ('jkj_futong', '腹痛'),
    '3.5': ('jkj_chronic_diarrhea', '腹泻'),
    '3.6': ('jkj_fuzhangpaiqi', '腹胀排气'),
    '3.7': ('jkj_constipation', '功能性便秘'),
    '3.8': ('jkj_HF', '心血管疾病'),
    '3.9': ('jkj_fat', '肥胖症'),
    '3.10': ('jkj_T2D', 'Ⅱ型糖尿病'),
    '3.11': ('jkj_allergy', '过敏症'),
    '3.12': ('jkj_osteoporosis', '骨质疏松症'),
    '3.13': ('jkj_yiyu', '情绪障碍'),
    '3.14': ('jkj_jingfen', '精神分裂症'),
    '3.15': ('jkj_shuimianhuxizanting', '睡眠呼吸暂停综合征'),
    '3.16': ('jkj_aud', '酒精使用障碍'),
    '3.17': ('jkj_AD', '阿尔兹海默症'),
    '3.18': ('jkj_PD', '帕金森综合征'),
}
_NUTRITION = {
    '4.1': ('yingyang2_tanshui2', '碳水化合物（糖类）分解代谢'),
    '4.2': ('yingyang2_zhizhi2', '脂质合成和分解代谢'),
    '4.3': ('yingyang2_dlzfs2', '短链脂肪酸合成代谢'),
    '4.4': ('yingyang2_sjdz2', '神经递质合成代谢'),
    '4.5': ('yingyang2_ajs2', '氨基酸合成代谢'),
    '4.6': ('yingyang2_wss2', '维生素合成代谢'),
    '4.7': ('yingyang2_yhdxw2', '有害代谢物合成能力'),
}
_STRUCTURE = {
    '肠道微生态总评分': ('score_sum_calculate', None),
    '肠道菌群种类': ('part1_species_num', 'part1_species_num_result'),
    '肠道菌群多样性': ('part1_Shannon_data1', 'part1_alpha_txt'),
    '肠道年龄评估': ('pre_gutage_result', None),
    '肠道微生物组健康指数(GMWI2)': ('jun_gmwi2_result_score', 'jun_gmwi2_state'),
    '肠道菌群定植抗力': ('jun_be_Ratio', 'jun_result_be'),
    '肠道微生物定植抗力': ('jun_be_Ratio', 'jun_result_be'),
    '厚壁菌门/拟杆菌门(F/B)': ('jun_fb_Ratio', 'jun_result_fb'),
    '肠道抗炎能力': ('kynl', 'kynl_re'),
    '肠道免疫力': ('myl', 'myl_re'),
    '肠道免疫力评估': ('myl', 'myl_re'),
}
_SUMMARY = {
    '有益菌': 'jun_yyj',
    '致病菌': 'jun_zbj',
    '条件致病菌': 'jun_tjzbj',
    '致病性真菌': 'jun_zj',
    '致病性病毒': 'jun_bd',
    '致病性寄生虫': 'jun_jsc',
    '产短链脂肪酸的菌': 'jun_ds',
    '肠道基石菌构成': 'jun_hxjs',
    '肠道屏障功能损伤相关菌': 'jun_pzss',
    **{name: key for key, name in _SECTIONS.values() if key.startswith('jkj_')},
    **{name: key for key, name in _NUTRITION.values()},
}
_SUMMARY = {unicodedata.normalize('NFKC', key): value for key, value in _SUMMARY.items()}
_SUMMARY.update({'心血管相关': 'jkj_HF', '有害代谢物合成代谢': 'yingyang2_yhdxw2'})
_DISEASES = {
    '溃疡性结肠炎': 'uc',
    '结直肠癌': 'crc',
    '肠易激综合征': 'ibs',
    '胃癌': 'sc',
    '精神分裂症': 'scz',
    '抑郁症': 'mdd',
    'Ⅱ型糖尿病': 'dm',
    'II型糖尿病': 'dm',
    '肝硬化': 'cirrhosis',
}
_GM_ASSOCIATIONS = (
    ('ganran', '胃肠道感染'),
    ('IBD', '炎症性肠病'),
    ('IBS', '肠易激综合征'),
    ('futong', '腹痛'),
    ('chronic_diarrhea', '腹泻'),
    ('fuzhangpaiqi', '腹胀排气'),
    ('weizhang', '胃胀'),
    ('constipation', '功能性便秘'),
    ('HF', '心血管疾病'),
    ('fat', '肥胖症'),
    ('T2D', '2型糖尿病'),
    ('allergy', '过敏症'),
    ('hepatopathy', '代谢相关脂肪性肝病'),
    ('osteoporosis', '骨质疏松症'),
    ('yiyu', '情绪障碍'),
    ('shuimian', '睡眠障碍'),
    ('jingfen', '精神分裂症'),
    ('shuimianhuxizanting', '睡眠呼吸暂停综合征'),
    ('aud', '酒精使用障碍'),
    ('AD', '阿尔兹海默症'),
    ('PD', '帕金森综合征'),
)
_G1_ASSOCIATIONS = (
    ('innutrition', '营养不良'),
    ('IDA', '缺铁性贫血'),
    ('obesity', '肥胖'),
    ('AD', '湿疹'),
    ('AR', '过敏性鼻炎'),
    ('asthma', '哮喘'),
    ('RRTI', '反复呼吸道感染'),
    ('CDD', '慢性腹泻'),
    ('FC', '功能性便秘'),
    ('CeD', '乳糜泻（麸质过敏症）'),
    ('HSP', '过敏性紫癜'),
    ('FA', '食物过敏'),
    ('FD', '功能性消化不良'),
    ('TD', '抽动障碍'),
    ('ADHD', '多动症'),
    ('ASD', '孤独症'),
    ('yiyu', '抑郁障碍'),
)


def _definitions(family):
    """The supplier's numbered measured sections differ across three layouts."""
    if family not in {'GM', 'G1'}:
        return _SECTIONS, _NUTRITION, _SUMMARY, _DISEASES
    prefix = 'jun_children_' if family == 'G1' else 'jun_'
    sections = {
        code: (prefix + key, title)
        for code, key, title in (
            ('2.1', 'yyj', '有益菌'),
            ('2.2', 'ds', '产短链脂肪酸的菌'),
            ('2.3', 'zbj', '致病菌'),
            ('2.3.1', 'zbj', '致病菌'),
            ('2.3.2', 'tjzbj', '条件致病菌'),
            ('2.3.3', 'zj', '致病性真菌'),
            ('2.3.4', 'bd', '病毒'),
            ('2.3.5', 'jsc', '致病性寄生虫'),
        )
    }
    sections['1.7' if family == 'G1' else '1.8'] = (prefix + 'pzss', '肠道屏障损伤相关菌评估')
    if family == 'G1':
        sections.update(
            {
                '1.8': (prefix + 'fayu', '肠道菌群发育成熟度相关菌群'),
                '1.9': (prefix + 'shengzhang', '影响儿童生长指标相关肠道菌群评估'),
            }
        )
    associations = _G1_ASSOCIATIONS if family == 'G1' else _GM_ASSOCIATIONS
    health_prefix = 'jkj_children_' if family == 'G1' else 'jkj_'
    sections.update({f'3.{i}': (health_prefix + key, title) for i, (key, title) in enumerate(associations, 1)})
    nutrition = {
        **{code: value for code, value in _NUTRITION.items() if code != '4.7'},
        '4.7': ('yingyang2_hgs2', '核苷酸合成代谢'),
        '4.8': ('yingyang2_trss2', '天然色素合成代谢'),
        '4.9': ('yingyang2_yhdxw2', '有害代谢物合成能力'),
        '4.10': ('yingyang2_dxwz2', '毒性物质分解能力'),
    }
    summary = {name: key for key, name in (*sections.values(), *nutrition.values())}
    summary.update(
        {
            '致病性病毒': prefix + 'bd',
            '肠道屏障功能损伤相关菌': prefix + 'pzss',
            '核心菌属构成': prefix + 'hxjs',
            '抗生素耐药基因': 'nyjy',
            '重要毒力基因评估': 'virulence_gene',
            '毒力基因': 'virulence_gene',
            '有害代谢物合成代谢': 'yingyang2_yhdxw2',
            '毒性物质分解代谢': 'yingyang2_dxwz2',
        }
    )
    if family == 'G1':
        summary['抑郁障碍'] = health_prefix + 'yiyu'
        summary['抑郁症'] = health_prefix + 'yiyu'
    else:
        summary['心血管相关'] = health_prefix + 'HF'
    diseases = (
        {'孤独症': 'autism', '抑郁障碍': 'mdd', '克罗恩病': 'cd', '溃疡性结肠炎': 'uc', '肠易激综合征': 'ibs'}
        if family == 'G1'
        else {
            **_DISEASES,
            '克罗恩病': 'cd',
            '代谢相关脂肪性肝病': 'nafld',
            '非酒精性脂肪肝': 'nafld',
            '2型糖尿病': 'dm',
        }
    )
    return (
        sections,
        nutrition,
        {_label(name): key for name, key in summary.items()},
        {_label(name): key for name, key in diseases.items()},
    )


_NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
_RANGE = re.compile(rf'^(?:[<>≤≥]=?\s*{_NUMBER}|{_NUMBER}\s*[~～–—-]\s*{_NUMBER})$')
_LATIN_NAME = re.compile(r'(?:\[[A-Za-z][A-Za-z0-9_. -]*\]|[A-Za-z][A-Za-z0-9_. -]*)(?:\s+[A-Za-z0-9_.-]+)*')
_TAXON_RANKS = {'种': 'SPECIES', '门': 'PHYLUM', '纲': 'CLASS', '目': 'ORDER', '科': 'FAMILY', '属': 'GENUS'}


def _clean(value):
    # Normalise layout whitespace only. Roman degree labels such as Ⅱ and
    # the supplier's original status vocabulary remain source values.
    return re.sub(r'\s+', ' ', str(value or '')).strip()


def _label(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


def _numeric(value):
    return bool(re.fullmatch(_NUMBER, _clean(value)))


def _pages(text):
    # Both the parser's native markers and human audit markers are supported.
    parts = re.split(r'(?:={3,}\s*PAGE\s+(\d+)\s*={3,})', text or '', flags=re.I)
    return [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts) - 1, 2)]


def _section(context, definitions):
    found = []
    text = unicodedata.normalize('NFKC', context or '')
    text = text.replace('II型糖尿病', '2型糖尿病')
    for code, (key, title) in definitions.items():
        title = unicodedata.normalize('NFKC', title).replace('II型糖尿病', '2型糖尿病')
        pattern = r'(?m)(?<![\d.])' + re.escape(code) + r'\s+' + re.escape(title)
        for match in re.finditer(pattern, text):
            found.append((match.start(), len(code), key))
    return max(found)[2] if found else None


def _taxon_name(cell):
    text = _clean(cell).replace('（', '(').replace('）', ')')
    # Chinese aliases may contain their own brackets, even a printed
    # unclosed bracket. Only the final Latin-only parenthesised cell is the
    # scientific name; its preceding Chinese label is retained literally.
    match = re.search(r'\(([^()]*)\)$', text)
    if match is None or not _LATIN_NAME.fullmatch(match[1].strip()):
        return None
    chinese = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', text[: match.start()].strip())
    return chinese or None, match[1].strip()


def _composition_panel(context, *, family):
    """Bind a composition table to its last explicit domain/rank heading.

    The supplier's bacterial rank fields and its fungal/viral/parasite
    charts are separate inventories. Top-N labels describe the printed
    table; neither a target count nor an organism's name supplies scope.
    """
    text = unicodedata.normalize('NFKC', context or '')
    candidates = []
    heading = re.compile(
        r'(细菌|真菌|病毒|寄生虫)\s*(?:[-－]?\s*([种门纲目科属])\s*(?:级)?水平)?'
        r'[^\n]*?(?:详细结构|结果总览表)'
    )
    prefix = 'jun_children_' if family == 'G1' else 'jun_'
    rank_prefix = 'jun_top_children_' if family == 'G1' else 'jun_top_adult_'
    domain_headings = list(heading.finditer(text))
    for match in domain_headings:
        domain, rank = match.group(1, 2)
        if domain == '细菌':
            if rank:
                candidates.append((match.start(), rank_prefix + _TAXON_RANKS[rank]))
        else:
            chart = {'真菌': 'fungi', '病毒': 'virus', '寄生虫': 'parasite'}[domain]
            candidates.append((match.start(), prefix + f'top5_{chart}_species'))
    # Older five-column tables state just their rank in the table title.
    for match in re.finditer(r'([纲目科属门种])级水平结果总览表', text):
        if any(domain.start() <= match.start() < domain.end() for domain in domain_headings):
            continue
        candidates.append((match.start(), rank_prefix + _TAXON_RANKS[match[1]]))
    return max(candidates)[1] if candidates else None


def _taxa(document, payload, origins, *, sections=None, family='GS.meta'):
    sections = sections or _SECTIONS
    by_page = defaultdict(list)
    for table in document.tables:
        if not table.native_clinical_columns:
            by_page[table.page_number].append(table)
    page_texts = dict(_pages(document.text))
    active = None
    pending = None
    previous = None
    for page in sorted(by_page):
        for table in by_page[page]:
            rows = table.rows
            widths = {len(row) for row in rows}
            if not rows or not (widths == {4} or family == 'G1' and widths == {5}):
                continue
            header = [_label(cell) for cell in rows[0]]
            has_header = header == ['菌名', '样本含量', '参考范围', '结果'] or header == [
                '菌名',
                '样本含量',
                '参考范围',
                '检测结果',
                '相关性',
            ]
            core_header = header == ['菌名', '检测结果', '正常范围(%)', '说明']
            has_header = has_header or core_header
            explicit = _section(table.context, sections)
            composition = _composition_panel(table.context, family=family)
            if core_header:
                active = ('jun_children_' if family == 'G1' else 'jun_') + 'hxjs'
                pending = None
            elif explicit:
                active = explicit
                pending = None
            elif composition:
                active = composition
                pending = None
            elif has_header and pending:
                active, pending = pending, None
            if not active:
                continue
            for row in rows[1:] if has_header else rows:
                cell, value, reference, status = map(_clean, row[:4])
                identity = _taxon_name(cell)
                if identity and not value and not reference and not status and previous:
                    # A single physical cell can continue on the next page.
                    if not previous.get('taxonomy'):
                        previous['taxonomy'] = identity[1]
                    continue
                if (
                    not identity
                    or not _numeric(value)
                    or not _RANGE.fullmatch(reference)
                    or status not in {'正常', '偏低', '偏高', '异常', '超标', '未检出'}
                ):
                    # Preserve a split name for the next page, but never infer
                    # a Latin name from a Chinese label or an adjacent record.
                    if (
                        cell
                        and '(' not in cell
                        and _numeric(value)
                        and _RANGE.fullmatch(reference)
                        and status in {'正常', '偏低', '偏高'}
                    ):
                        previous = {'chineseName': cell, 'abundance': value, 'fanwei': reference, 'status': status}
                        payload.setdefault(active, []).append(previous)
                        origins[f'{active}[{len(payload[active]) - 1}]'] = (page, ' | '.join(row))
                    continue
                previous = {
                    'chineseName': identity[0],
                    'taxonomy': identity[1],
                    'abundance': value,
                    'fanwei': reference,
                    'status': status,
                }
                payload.setdefault(active, []).append(previous)
                origins[f'{active}[{len(payload[active]) - 1}]'] = (page, ' | '.join(row))
        footer = _section(page_texts.get(page, ''), sections)
        # A row divided exactly at a physical page break can lie below the
        # final detected table border. Retain its four literal cells only
        # after an explicit table header; its Latin name must then continue
        # as the first name-only cell on the next page.
        native_text = page_texts.get(page, '')
        last_header = native_text.rfind('菌名\n样本含量\n参考范围\n结果')
        if active and last_header >= 0:
            stub = re.compile(
                rf'(?m)^([^\n()（）]{{1,40}})\n({_NUMBER})\n'
                rf'({_NUMBER}[~～–—-]{_NUMBER})\n(正常|偏低|偏高)\s*$'
            )
            for match in stub.finditer(native_text, last_header):
                name, value, reference, status = match.groups()
                if not re.search(r'[\u3400-\u9fff]', name):
                    continue
                found = next(
                    (
                        r
                        for r in payload.get(active, [])
                        if r.get('chineseName') == _clean(name)
                        and r.get('abundance') == value
                        and r.get('fanwei') == reference
                    ),
                    None,
                )
                if found is not None:
                    previous = found
                    continue
                previous = {'chineseName': _clean(name), 'abundance': value, 'fanwei': reference, 'status': status}
                payload.setdefault(active, []).append(previous)
                origins[f'{active}[{len(payload[active]) - 1}]'] = (page, match[0])
        if footer and footer != active:
            pending = footer
    for key, rows in tuple(payload.items()):
        if isinstance(rows, list):
            payload[key] = [row for row in rows if row.get('taxonomy')]


def _nutrition(document, payload, origins, *, definitions=None):
    definitions = definitions or _NUTRITION
    # G1 draws numbered section headings over two physical text lines. Join
    # only that heading in this evaluate copy; the shared document is intact.
    from .document_source import strict_report_source_records

    normalized = replace(document, text=re.sub(r'(?m)^(\s*4\.\d+)[ \t]*\n[ \t]*(?=\S)', r'\1 ', document.text))
    source_records = strict_report_source_records(normalized)
    chart_rows = [row for row in source_records if row.get('referenceRange') and '表示' in row['referenceRange']]
    if chart_rows:
        for row in chart_rows:
            key = next(
                (key for key, title in definitions.values() if _label(title) == _label(row.get('sourceSection'))), None
            )
            if not key:
                continue
            item = {
                'chineseName': row['itemName'],
                'score': row['value'],
                'fanwei': row['referenceRange'],
                'status': row.get('status'),
            }
            index = len(payload.setdefault(key, []))
            payload[key].append(item)
            origins[f'{key}[{index}]'] = (row['sourcePage'], row['sourceText'])
        return
    for page, text in _pages(document.text):
        key = _section(text, definitions)
        if not key:
            continue
        legend = re.search(r'(?m)^\s*(?:极低|偏低|正常)\s*[:：]\s*[（(]', text)
        if not legend:
            continue
        # Only the score chart before its own printed legend. Dietary advice,
        # food examples, reference bounds and page numbers are outside it.
        lines = [_clean(line) for line in text[: legend.start()].splitlines() if _clean(line)]
        rows = []
        for index in range(len(lines) - 1):
            name, score = lines[index : index + 2]
            if not _numeric(score) or _numeric(name) or len(name) > 25 or not re.search(r'[\u3400-\u9fff]', name):
                continue
            if not 0 <= Decimal(score) <= 100:
                continue
            rows.append({'chineseName': name, 'score': score})
            origins[f'{key}[{len(rows) - 1}]'] = (page, name + ' ' + score)
        if rows:
            payload[key] = rows


def _rank_taxa(document, payload, origins, *, family='GS.meta'):
    """Read scientific-name-separated rank tables without crossing inventories."""
    active = None
    last_page = None
    expected = ['中文名', '拉丁学名', '样本含量', '参考范围', '结果']
    for table in sorted(document.tables, key=lambda item: item.page_number):
        if table.native_clinical_columns or not table.rows or not all(len(row) == 5 for row in table.rows):
            active = None
            continue
        header = [_label(cell) for cell in table.rows[0]]
        has_header = header[:4] == expected[:4] and header[4] in {'结果', '检测结果'}
        heading = _composition_panel(table.context, family=family)
        if has_header:
            active = heading
        elif any(cell in expected for cell in header) or (last_page is not None and table.page_number > last_page + 1):
            active = None
        if not active:
            continue
        last_page = table.page_number
        for row in table.rows[1:] if has_header else table.rows:
            chinese, latin, value, reference, status = map(_clean, row)
            if (
                not _LATIN_NAME.fullmatch(latin)
                or not _numeric(value)
                or not _RANGE.fullmatch(reference)
                or status not in {'正常', '偏低', '偏高', '异常', '超标'}
            ):
                continue
            payload.setdefault(active, []).append(
                {'chineseName': chinese, 'taxonomy': latin, 'abundance': value, 'fanwei': reference, 'status': status}
            )
            origins[f'{active}[{len(payload[active]) - 1}]'] = (table.page_number, ' | '.join(row))


def _gene_payload(document, payload, origins):
    """Reuse ownership-checked measured gene cells in both input lanes.

    The PDF DTO and a mechanical JSON export must contain the same original
    classes and detected/candidate columns. A summary never supplies a zero,
    and a candidate list never supplies a detected gene.
    """
    from .evaluation_pdf_amr import original_pdf_amr_rows
    from .source_context import source_context_records

    for row in original_pdf_amr_rows(document):
        key = 'naiyao_gene_result'
        item = dict(
            chineseName=row['itemName'],
            abundance=row['value'],
            fanwei=row.get('referenceRange'),
            status=row.get('status'),
        )
        if row.get('unit') is not None:
            item['unit'] = row['unit']
        if row.get('antibioticClass') is not None:
            item['antibioticClass'] = row['antibioticClass']
        index = len(payload.setdefault(key, []))
        payload[key].append(item)
        origins[f'{key}[{index}]'] = (row['sourcePage'], row['sourceText'])

    for row in source_context_records(document):
        if row.get('sourceCategory') != 'virulence':
            continue
        key = 'virulence_gene_result'
        item = dict(
            name=row['itemName'],
            status=row.get('sourceStatus'),
            detectedGenes=list(row.get('detectedGenes') or []),
            candidateGenes=list(row.get('candidateGenes') or []),
        )
        index = len(payload.setdefault(key, []))
        payload[key].append(item)
        origins[f'{key}[{index}]'] = (row['sourcePage'], row['sourceText'])


def _source_metadata(document, payload, origins, subject, sample):
    """Read labelled personal/sample cells from this original's metadata page.

    A blank sampling date stays blank. Receipt, report and generation dates
    have distinct meanings; a neighbouring label cannot supply their value.
    """
    fields = {
        '姓名': ('subject', 'name'),
        '受检人姓名': ('subject', 'name'),
        '年龄': ('subject', 'age'),
        '性别': ('subject', 'sex'),
        'BMI指数': ('payload', 'bmi'),
        'BMI': ('payload', 'bmi'),
        '肠道健康问题': ('payload', 'gutHealth'),
        '用药情况': ('payload', 'drug'),
        '样本编号': ('sample', 'sample_id'),
        '样本类型': ('sample', 'sample_type'),
        '检测项目': ('sample', 'test_item'),
        '检测技术': ('sample', 'method'),
        '检测方法': ('sample', 'method'),
        '测序平台': ('sample', 'sequencing_platform'),
        '采样日期': ('sample', 'sample_date'),
        '报告日期': ('sample', 'report_date'),
        '收样日期': ('payload', 'signDate'),
    }
    candidates = defaultdict(list)
    pages = dict(_pages(document.text))
    metadata_pages = {
        page for page, text in pages.items() if re.search('受检人信息|基本信息|样本信息|检测项目信息', text)
    }
    empty = {'', '/', '\\', '—', '-'}

    def labelled(value):
        match = re.fullmatch(r'([^:：]{1,20})[:：]\s*(.*)', _clean(value))
        return (match[1], match[2]) if match and match[1] in fields else None

    def keep(label, value, page, quote):
        value = _clean(value)
        if value not in empty and labelled(value) is None and value not in fields:
            candidates[fields[label]].append((value, page, quote))

    for table in document.tables:
        if table.native_clinical_columns or table.page_number not in metadata_pages:
            continue
        for cells in table.rows:
            for index, cell in enumerate(cells):
                entry = labelled(cell)
                if entry is None:
                    continue
                label, value = entry
                if not value and index + 1 < len(cells):
                    value = cells[index + 1]
                keep(label, value, table.page_number, ' | '.join(cells))
    for page in metadata_pages:
        lines = [line.strip() for line in pages[page].splitlines() if line.strip()]
        for index, line in enumerate(lines):
            entry = labelled(line)
            if entry is None and line in fields:
                entry = (line, '')
            if entry is None:
                continue
            label, value = entry
            if not value and index + 1 < len(lines):
                value = lines[index + 1]
            keep(label, value, page, line + ('\n' + value if value else ''))
    for (lane, field), entries in candidates.items():
        if len({value for value, _, _ in entries}) != 1:
            continue
        value, page, quote = entries[0]
        if lane == 'subject' and field == 'age':
            match = re.fullmatch(r'(\d{1,3})\s*(?:岁)?', value)
            if match is None:
                continue
            value = int(match[1])
        if lane == 'payload' and field == 'bmi' and not re.fullmatch(r'\d{1,3}(?:\.\d{1,3})?', value):
            continue
        if lane == 'subject':
            subject[field] = value
        elif lane == 'sample':
            sample[field] = value
        else:
            payload[field] = value
        origins[(field if lane == 'payload' else lane + '.' + field)] = (page, quote)
    # Graphic G1 covers print the actual sequencing technology as an isolated
    # bracketed title even where page 2 has no separate technical field.
    cover = pages.get(1, '')
    if not sample.get('method') and (match := re.search(r'【\s*(宏基因组测序)\s*】', cover)):
        sample['method'] = match[1]
        origins['sample.method'] = (1, match[0])


def _source_method_references(document, payload, origins):
    """Keep this source's literal grade bands, never only its normal band.

    A richer grade is not an abnormal result merely because it is outside
    the interval labelled normal. Preserve the whole labelled table without
    assigning a new grade from its numeric boundaries.
    """
    headers = {'多样性指数范围': 'part1_Shannon_data1', '肠道菌群种类': 'part1_species_num'}
    for table in document.tables:
        if table.native_clinical_columns or not table.rows:
            continue
        header = [_label(cell) for cell in table.rows[0]]
        if len(header) != 3 or header[0] not in headers or header[1:] != ['结果评估', '指标范围含义']:
            continue
        key = headers[header[0]]
        bands = [row for row in table.rows[1:] if len(row) == 3 and _clean(row[1]) and _RANGE.fullmatch(_clean(row[0]))]
        if key not in payload or not bands or key + '_referenceRange' in payload:
            continue
        path = key + '_referenceRange'
        payload[path] = '；'.join(f'{_clean(row[1])}: {_clean(row[0])}' for row in bands)
        origins[path] = (table.page_number, '\n'.join(' | '.join(row) for row in bands))
    candidates = defaultdict(list)
    for page, text in _pages(document.text):
        text = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text))
        if 'score_sum_calculate' in payload and (bands := re.search(r'评估肠道菌群失衡状态[:：]([^。]{1,160})', text)):
            labels = {
                label: interval
                for interval, label in re.findall(
                    r'(\d+(?:\.\d+)?[-~～–—]\d+(?:\.\d+)?)分?为(健康|[IV]+度失调)(?=[,，、]|$)', bands[1]
                )
            }
            status = _label(payload.get('balance_status')).removesuffix('状态')
            if healthy := labels.get('健康'):
                reference = f'{healthy} 分（健康）'
                if current := labels.get(status):
                    reference += f' / {current} 分（{payload["balance_status"]}）'
                candidates['score_sum_calculate'].append((reference, page, bands[1]))
        if 'jun_gmwi2_result_score' in payload and (
            threshold := re.search(
                rf'GMWI2值(?P<range>>=?{_NUMBER})(?:时)?[，,]?代表(?:着)?健康',
                text,
                re.I,
            )
        ):
            reference, quote = threshold['range'], threshold[0]
            sequence = re.search(
                r'分为([^。；]{1,140}?)[,，]其对应分值区间分别为([^。；]{1,120})', text,
            )
            if sequence:
                states = re.split(r'[、,，]', sequence[1])
                states = [re.sub(r'\([-+]?\d+度\)$', '', state) for state in states]
                ranges = re.split(r'[、,，]', sequence[2])
                current = payload.get('jun_gmwi2_state')
                if (len(states) == len(ranges) and states.count(current) == 1
                        and all(_RANGE.fullmatch(band) for band in ranges)):
                    reference = f'原报告健康方向: {reference}；{current}: {ranges[states.index(current)]}'
                    quote += '。' + sequence[0]
            candidates['jun_gmwi2_result_score'].append((reference, page, quote))
    for key, rows in candidates.items():
        path = key + '_referenceRange'
        if len({value for value, _, _ in rows}) == 1:
            value, page, quote = rows[0]
            prior = payload.get(path)
            if not prior or (key == 'jun_gmwi2_result_score' and value.startswith(f'原报告健康方向: {prior}；')):
                payload[path] = value
                origins[path] = (page, quote)


def template_pdf_payload(document):
    """Extract independent, literal template cells plus their physical origin."""
    text = document.text or ''
    from .evaluation_disease_catalog import _source_family

    family = _source_family(text)
    if family is None and (
        '肠道微生态总评分' in text and '高通量宏基因组测序' in text and '肠道菌群健康基因检测' in text
    ):
        family = 'GS.meta'
    if family is None:
        return None
    sections, nutrition, summary, diseases = _definitions(family)
    payload, origins = {}, {}
    sample = {}
    subject = {}
    nutrient_header_page = None
    association_header_page = None
    for table in document.tables:
        if table.native_clinical_columns:
            continue
        header = [_label(v) for v in (table.rows[0] if table.rows else [])]
        if header[:3] == ['健康项目', '检测结果', '异常指标']:
            association_header_page = table.page_number
        elif (
            association_header_page is not None
            and table.page_number == association_header_page + 1
            and table.rows
            and all(
                len(row) == 3
                and summary.get(_label(row[0]), '').startswith('jkj_')
                and _clean(row[1]) in {'检出', '未检出'}
                for row in table.rows
            )
        ):
            # A literal continuation under the immediately preceding health
            # result header; unnamed placeholders and teaching prose fail it.
            header = ['健康项目', '检测结果', '异常指标']
        if header[:3] == ['营养项目', '检测结果', '异常指标']:
            nutrient_header_page = table.page_number
        elif (
            nutrient_header_page is not None
            and table.page_number == nutrient_header_page + 1
            and table.rows
            and [_label(v) for v in table.rows[0][:2]] == ['', '']
            and all(len(row) == 3 for row in table.rows)
        ):
            # An explicit immediately preceding nutrition table continues with
            # a split detail cell, then its remaining named result rows.
            header = ['营养项目', '检测结果', '异常指标']
            if payload.get('yingyang2_zhizhi2_all') is not None:
                payload['yingyang2_zhizhi2_all'] += _clean(table.rows[0][2])
        for row in table.rows:
            cells = [_clean(value) for value in row]
            if len(cells) < 2:
                continue
            label, value = _label(cells[0]), cells[1]
            for offset in (0, 2):
                if len(cells) <= offset + 1:
                    continue
                field, entry = _label(cells[offset]), cells[offset + 1]
                for original, target in [('受检人姓名:', 'name'), ('年龄:', 'age'), ('性别:', 'sex')]:
                    if field == original and entry:
                        subject[target] = (
                            int(re.search(r'\d+', entry)[0]) if target == 'age' and re.search(r'\d+', entry) else entry
                        )
                for original, target in [
                    ('报告日期:', 'report_date'),
                    ('采样日期:', 'sample_date'),
                    ('样本类型:', 'sample_type'),
                    ('检测项目:', 'test_item'),
                    ('检测技术:', 'method'),
                ]:
                    if field == original and entry:
                        sample[target] = entry
                if field == '收样日期:' and entry:
                    payload['signDate'] = entry
                    origins['signDate'] = (table.page_number, ' | '.join(cells))
            if label in _STRUCTURE and _numeric(value) and header and header[0] in {'检测项目', '项目'}:
                key, status_key = _STRUCTURE[label]
                payload[key] = value
                origins[key] = (table.page_number, ' | '.join(cells))
                reference_index = next(
                    (i for i, title in enumerate(header) if title in {'参考范围', '正常范围', '参考区间'}), None
                )
                if (
                    reference_index is not None
                    and reference_index < len(cells)
                    and _RANGE.fullmatch(cells[reference_index])
                ):
                    payload[key + '_referenceRange'] = cells[reference_index]
                    origins[key + '_referenceRange'] = origins[key]
                if status_key and len(cells) > 2:
                    status = cells[3] if len(cells) > 3 and header[2] == '参考范围' else cells[2]
                    if status and status != '/' and not status.startswith('详细结果'):
                        payload[status_key] = status
                        origins[status_key] = origins[key]
            if label == '平衡状态' and value:
                payload['balance_status'] = value
                origins['balance_status'] = (table.page_number, ' | '.join(cells))
            if (
                label in {'肠型', '肠道菌群分型'}
                and value
                and not value.startswith('详细')
                and header
                and header[0] in {'检测项目', '项目'}
            ):
                payload['jun_sample_result_ET'] = value
                origins['jun_sample_result_ET'] = (table.page_number, ' | '.join(cells))
            if (
                label in summary
                and value in {'正常', '异常', '未检出', '检出', '很弱', '较弱', '强', '弱'}
                and header
                and header[0] in {'检测项目', '健康项目', '营养项目'}
            ):
                key = summary[label]
                status_key = key if key == 'virulence_gene' else key + '_all_result'
                payload[status_key] = value
                origins[status_key] = (table.page_number, ' | '.join(cells))
                if len(cells) > 2:
                    detail_key = 'virulence_gene_txt_result' if key == 'virulence_gene' else key + '_all'
                    payload[detail_key] = re.sub(r'(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])', '', cells[2])
                    origins[detail_key] = origins[status_key]
            if (
                header[:3] == ['疾病类型', '风险值', '检测结果']
                and label in diseases
                and _numeric(value)
                and len(cells) > 2
            ):
                prefix = 'disease_' + diseases[label]
                payload[prefix + '_score'], payload[prefix + '_label'] = value, cells[2]
                origins[prefix + '_score'] = origins[prefix + '_label'] = (table.page_number, ' | '.join(cells))
            if header[:3] == ['您的肠道年龄', '您的实际年龄', '肠道年龄差'] and all(_numeric(c) for c in cells[:3]):
                payload['pre_gutage_result'] = cells[0]
                subject['age'] = int(Decimal(cells[1]))
                origins['pre_gutage_result'] = (table.page_number, ' | '.join(cells))
            if (
                len(cells) >= 4
                and label == '肠道微生物组健康指数(GMWI2)'
                and header[2:] == ['肠道健康状态', '肠道菌群状态']
            ):
                payload['jun_gmwi2_degree'] = cells[3]
                origins['jun_gmwi2_degree'] = (table.page_number, ' | '.join(cells))
    if not any(key in payload for key in ('score_sum_calculate', 'part1_Shannon_data1', 'jun_gmwi2_result_score')):
        return None
    _taxa(document, payload, origins, sections=sections, family=family)
    _rank_taxa(document, payload, origins, family=family)
    _nutrition(document, payload, origins, definitions=nutrition)
    _gene_payload(document, payload, origins)
    _source_metadata(document, payload, origins, subject, sample)
    _source_method_references(document, payload, origins)
    if sample.get('report_date'):
        payload['reportDate'] = sample['report_date']
    if subject.get('age') is not None:
        payload['age'] = subject['age']
    if subject.get('name'):
        payload['userName'] = subject['name']
    if subject.get('sex'):
        payload['gender'] = subject['sex']
    identities = set(re.findall(r'样本编号\s*[:：]\s*([A-Za-z0-9_-]+)', text))
    if not sample.get('sample_id') and len(identities) == 1:
        sample['sample_id'] = next(iter(identities))
    if sample:
        payload['sample'] = SampleInfo(**sample).model_dump(mode='json', by_alias=True, exclude_none=True)
    return payload, origins, subject, sample


def parse_template_pdf_document(document, request=None):
    """Return the evaluate DTO only for recognised, nonempty original cells."""
    extracted = template_pdf_payload(document)
    if extracted is None:
        return None
    payload, origins, subject, sample = extracted
    from .evaluation_disease_catalog import _source_family

    family = _source_family(document.text) or 'GS.meta'
    from .evaluation_template_input import parse_template_json

    request = request or ReportRequest(user_id='pdf-source-reader', session_id='pdf-source-reader')
    version = 'T250627161148' if family == 'GS.meta' else family
    parsed = parse_template_json(
        request,
        payload,
        template_number=version,
        checksum=document.metadata.get('content_sha256'),
        source_name=document.filename,
    )
    # A directory alias selects a module, never rewrites its printed diagnosis
    # name (for example 非酒精性脂肪肝 versus a newer classification term).
    from .schemas import stable_id

    for audit in parsed._evaluation_source_audit:
        for mapping in audit.get('fieldMap', []):
            if mapping.get('lane') != 'diseaseResults':
                continue
            origin = origins.get(mapping['sourcePath'])
            if origin is not None:
                raw_name = origin[1].split(' | ', 1)[0]
                disease = parsed.disease_results[mapping['index']]
                disease.name = raw_name
                disease.disease_id = stable_id('disease', raw_name)
    parsed.source_type = 'pdf'
    parsed.subject = SubjectInfo(**subject)
    parsed.sample = SampleInfo(**sample)
    parsed.source_summary = None
    _bind_pdf_origins(parsed, origins, document)
    from .schemas import HospitalDataInput
    from .source_context import source_context_records

    projected_associations = any(str(key).startswith('jkj_') and str(key).endswith('_all_result') for key in payload)
    for row in source_context_records(document):
        if row.get('sourceCategory') == 'virulence' and payload.get('virulence_gene_result'):
            continue
        # The same "健康项目 | 检测结果 | 异常指标" table has already been
        # projected through the template mapping above. Appending the context
        # copy made every health-association topic appear twice in the report.
        if row.get('sourceCategory') == 'healthAssociation' and projected_associations:
            continue
        value = HospitalDataInput.model_validate(row)
        value._evaluation_record_id = (
            f'source:{parsed.source_checksum}:page:{row["sourcePage"]}'
            f':source-context:{len(parsed.supplementary_hospital_data)}'
        )
        parsed.supplementary_hospital_data.append(value)
    return parsed


def _bind_pdf_origins(parsed, origins, document):
    """Replace the mapper's JSON locators with actual PDF physical origins."""
    from .evaluation_template_input import EvaluationTemplateFunctionMetric
    from .evaluation_template_input import EvaluationTemplateStructureMetric

    function_metrics = []
    for item in parsed.metrics:
        if not isinstance(item, (EvaluationTemplateFunctionMetric, EvaluationTemplateStructureMetric)):
            function_metrics.append(item)
            continue
        origin = origins.get(item.source_path) or origins.get(item.source_path.removesuffix('.score'))
        if origin is None:
            continue
        page, quote = origin
        row = item.model_dump(mode='json', by_alias=True)
        row.update(
            sourcePage=page,
            sourceFile=document.filename,
            sourceChecksum=document.metadata.get('content_sha256'),
            observedAt=parsed.sample.report_date,
            sourceText=quote,
        )
        if isinstance(item, EvaluationTemplateFunctionMetric):
            row.update(sourceSection=item.source_section, pathway_code=item.pathway_code, source_path=item.source_path)
            function_metrics.append(EvaluationTemplatePdfFunctionMetric.model_validate(row))
        else:
            reference = item.reference_source
            if reference and not reference.startswith('template:'):
                reference_origin = origins.get(reference)
                reference_page = reference_origin[0] if reference_origin else page
                reference = f'source:{parsed.source_checksum}:page:{reference_page}'
            row.update(sourceSection='肠道菌群组成结构评估', reference_source=reference)
            function_metrics.append(EvaluationTemplatePdfStructureMetric.model_validate(row))
    parsed.metrics = function_metrics
    mapped = []
    for item in parsed.evaluation_panel_summaries:
        path = getattr(item, 'source_path', None)
        origin = origins.get(path)
        if origin is None:
            # A whole-panel result and its _all source have the same scope.
            origin = origins.get(str(path).removesuffix('_all'))
        if origin is None:
            continue
        page, quote = origin
        row = item.model_dump(mode='json', by_alias=True)
        row.update(
            sourcePage=page,
            sourceFile=document.filename,
            sourceChecksum=document.metadata.get('content_sha256'),
            observedAt=parsed.sample.report_date,
            sourceSection='宏基因组检测原始结果',
            sourceText=quote,
        )
        mapped.append(EvaluationHealthSummaryMetric.model_validate(row))
    parsed.evaluation_panel_summaries = mapped
    for row in parsed.supplementary_hospital_data:
        extra = row.__pydantic_extra__ or {}
        path = extra.pop('sourcePath', None)
        origin = origins.get(path) or origins.get(str(path).removesuffix('.score'))
        if origin is not None:
            page, quote = origin
            extra.update(
                sourcePage=page,
                sourceText=quote,
                sourceFile=document.filename,
                sourceChecksum=document.metadata.get('content_sha256'),
                sourcePanelPath=path,
            )
            row._evaluation_record_id = f'source:{parsed.source_checksum}:page:{page}:template:{path}'
    for occurrence in parsed.source_taxon_occurrences:
        path = occurrence.pop('sourcePath', None) or occurrence.pop('source_path', None)
        if path in origins:
            page, quote = origins[path]
            occurrence.update(
                sourcePage=page,
                sourceText=quote,
                sourceFile=document.filename,
                sourceChecksum=document.metadata.get('content_sha256'),
                sourcePanelPath=path,
            )
    family = next(
        (
            row.get('family')
            for row in parsed._evaluation_source_audit
            if row.get('kind') == 'template_json_field_mapping'
        ),
        'GS.meta',
    )
    parsed._evaluation_source_audit = [
        {
            'kind': 'template_pdf_mapping',
            'family': family,
            'templateNumber': 'T250627161148' if family == 'GS.meta' else None,
            'sourceFile': document.filename,
            'sourceChecksum': document.metadata.get('content_sha256'),
            'mappedFields': {
                path: {'sourcePage': page, 'sourceText': quote} for path, (page, quote) in origins.items()
            },
        }
    ]
