"""Project measured and reviewed microbiome findings into the six-chapter view."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from copy import deepcopy
from decimal import ROUND_HALF_UP
from decimal import Decimal
from decimal import InvalidOperation

from .microbiome_source_semantics import assay_groups as assay_groups
from .microbiome_source_semantics import assay_identity
from .microbiome_source_semantics import comparison_semantics
from .microbiome_source_semantics import function_cell_duties
from .microbiome_source_semantics import source_assessment_semantics
from .microbiome_source_semantics import source_path_from_record
from .microbiome_source_semantics import typed_measurement_observation

_STRUCTURE_GROUPS = (
    ('overallBalance', '微生态综合评分 & 失衡分度', ('microbiomeHealthScore', 'balanceStatus')),
    ('diversity', '物种多样性指数 & 检出物种数', ('diversity', 'speciesCount')),
    ('gmwi2', '微生物组健康指数 (GMWI2)', ('gmwi2',)),
    ('gutAge', '肠道生物学年龄（实足年龄校准）', ('gutAge',)),
    ('colonizationResistance', '肠道定植抗力 (B/E 比值)', ('beRatio',)),
    ('composition', '菌门构成、F/B 比值与肠型', ('fbRatio', 'enterotype')),
    (
        'barrier',
        '黏膜屏障损伤菌 / 抗炎与免疫力评分',
        ('barrierRelatedBacteria', 'antiInflammatoryCapacity', 'gutImmunity'),
    ),
    ('developmentalMaturity', '肠道菌群发育成熟度相关菌群', ('developmentalMaturity',)),
    ('childGrowth', '儿童生长指标相关肠道菌群', ('childGrowth',)),
)
_CHILD_STRUCTURE_PANELS = {
    'developmentalMaturity': 'jun_children_fayu',
    'childGrowth': 'jun_children_shengzhang',
}
_STRUCTURE_ORDER = tuple(key for _, _, keys in _STRUCTURE_GROUPS for key in keys)
_CATEGORICAL_STRUCTURE_CODES = {'balanceStatus', 'enterotype'}
_ORGANISM_GROUPS = (
    ('beneficial', '核心有益菌群双向审计'),
    ('scfaProducers', '产短链脂肪酸 (SCFA) 功能菌'),
    ('composition', '菌群组成与丰度偏移'),
    ('bacteria', '细菌性致病菌与条件致病菌'),
    ('otherOrganisms', '真菌、病毒与寄生虫谱系'),
    ('genes', '毒力因子基因 & 抗生素耐药基因'),
)
_ORGANISM_ASSESSMENT_GROUPS = {'jkj_ganran': 'bacteria'}
_PANEL_GROUPS = {
    'beneficialBacteria': 'beneficial',
    'scfaProducerBacteria': 'scfaProducers',
    'pathogenicBacteria': 'bacteria',
    'opportunisticBacteria': 'bacteria',
    'pathogenicFungi': 'otherOrganisms',
    'pathogenicViruses': 'otherOrganisms',
    'pathogenicParasites': 'otherOrganisms',
    'virulenceGenes': 'genes',
    'antibioticResistanceGenes': 'genes',
}
_ABUNDANCE_PANEL_SOURCES = {
    'pathogenicBacteria': 'health_summary_pathogenic',
    'opportunisticBacteria': 'health_summary_opportunistic',
    'pathogenicFungi': 'health_summary_pathogenic_fungi',
    'pathogenicViruses': 'health_summary_pathogenic_viruses',
    'pathogenicParasites': 'health_summary_pathogenic_parasites',
    'antibioticResistanceGenes': 'health_summary_antibiotic_resistance_genes',
}
_TAXON_GROUPS = {
    'beneficial': 'beneficial',
    'pathogenic': 'bacteria',
    'opportunistic': 'bacteria',
    'fungi_pathogenic': 'otherOrganisms',
}
_SOURCE_PANEL_GROUPS = {
    'jun_yyj': 'beneficial',
    'jun_children_yyj': 'beneficial',
    'jun_zbj': 'bacteria',
    'jun_children_zbj': 'bacteria',
    'jun_tjzbj': 'bacteria',
    'jun_children_tjzbj': 'bacteria',
    'jun_zj': 'otherOrganisms',
    'jun_children_zj': 'otherOrganisms',
    'jun_ds': 'scfaProducers',
    'jun_children_ds': 'scfaProducers',
    'jun_hxjs': 'composition',
    'jun_children_hxjs': 'composition',
    'jun_top_adult_SPECIES': 'composition',
    'jun_top_children_SPECIES': 'composition',
    'jun_top_yichang_species': 'composition',
    'jun_children_top_yichang_species': 'composition',
    'jun_bd': 'otherOrganisms',
    'jun_children_bd': 'otherOrganisms',
    'jun_jsc': 'otherOrganisms',
    'jun_children_jsc': 'otherOrganisms',
    'jun_top5_fungi_species': 'otherOrganisms',
    'jun_children_top5_fungi_species': 'otherOrganisms',
    'jun_top5_virus_species': 'otherOrganisms',
    'jun_children_top5_virus_species': 'otherOrganisms',
    'jun_top5_parasite_species': 'otherOrganisms',
    'jun_children_top5_parasite_species': 'otherOrganisms',
}
for _population in ('adult', 'children'):
    for _rank in ('PHYLUM', 'CLASS', 'ORDER', 'FAMILY', 'GENUS'):
        _SOURCE_PANEL_GROUPS[f'jun_top_{_population}_{_rank}'] = 'composition'
_FUNCTION_GROUPS = (
    ('carbohydrate', '碳水化合物发酵与分解', ('P1',)),
    ('scfa', '短链脂肪酸 (SCFA) 合成', ('P3',)),
    ('lipid', '脂质与类固醇代谢通路', ('P2',)),
    ('neurotransmitter', '肠-脑轴神经递质合成', ('P6',)),
    ('aminoAcid', '必需与功能氨基酸合成', ('P4',)),
    ('vitamin', '肠源性维生素内源合成', ('P5',)),
    ('nucleotidePlant', '核苷酸合成与天然色素合成代谢', ('P7', 'P8')),
    ('harmfulMetabolism', '内源有害物 & 外源毒素降解', ('P9', 'P10')),
)
_MISSING = {
    '',
    '-',
    '--',
    '/',
    '—',
    '未知',
    '待检',
    '未测',
    '未检测',
    '未报告',
    '未覆盖',
    '未评估',
    '未提供',
    '未采集',
    '不详',
    '不适用',
    '无可用结果',
    '无数据',
    'nan',
    'n/a',
}
_DETECTION_LABELS = {'detected': '检出', 'not_detected': '未检出', 'trace': '低丰度信号'}
_ABNORMAL_MARKS = re.compile(r'偏高|偏低|超标|异常|升高|降低|[↑↓]')
_NORMAL_STATES = {'正常', '未见异常', '未见明显异常', '正常范围内', '均衡'}
_NORMAL_PANEL_STATES = _NORMAL_STATES | {'阴性', '未检出', '未检出异常'}
_ABSENT_ABNORMALITY = re.compile(r'(?:未(?:发现|检出|见)|无)(?:明显)?(?:异常|偏离)')
_SCORE_UNITS = {'分', '评分', '得分', 'score', '/100'}
_HUNDRED_SCALE = re.compile(r'(?:0\s*(?:-|–|—|~|～|至)\s*100(?!\d)|满分\s*100(?!\d)|百分制)')
_TAXON_OCCURRENCE = re.compile(r'parsed\.sourceTaxonOccurrences\[(\d+)\]')
_SOURCE_SNAPSHOT = re.compile(r'^source:([a-fA-F0-9]{64}):(?:json:|page:\d+:template:)')
_DECIMAL_LITERAL = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)'
_REFERENCE_BAND = re.compile(rf'({_DECIMAL_LITERAL})\s*(?:-|~|～|至|–|—)\s*({_DECIMAL_LITERAL})')
_EXCLUSIVE_REFERENCE = re.compile(rf'[<>]\s*({_DECIMAL_LITERAL})')
_GENE_DETECTION = {'检出': 'detected', '未检出': 'not_detected'}
_GRADE_BAND = re.compile(
    rf'(?:^|[;；\n])\s*(?P<label>[^;；\n:：]{{1,30}}?)\s*[:：]\s*[\[(（]?\s*'
    rf'(?P<range>(?:[<>≤≥]=?\s*{_DECIMAL_LITERAL}|'
    rf'{_DECIMAL_LITERAL}\s*(?:-|~|～|至|–|—)\s*{_DECIMAL_LITERAL}))'
)


def _present(value):
    return value is not None and str(value).strip().casefold() not in _MISSING


def _source_key(value):
    return ''.join(unicodedata.normalize('NFKC', str(value)).casefold().split())


def _source_state(item):
    if _present(item.source_status):
        return item.source_status
    if getattr(item, '_assessment_origin', None) in {'source', 'reference'} and _present(item.assessment_level):
        return item.assessment_level
    return None


def _source_is_abnormal(status):
    return bool(
        isinstance(status, str)
        and status
        and status not in _NORMAL_PANEL_STATES
        and not _ABSENT_ABNORMALITY.search(status)
        and _ABNORMAL_MARKS.search(status)
    )


def _source_grade_bands(reference):
    return [
        {'label': match['label'].strip(), 'range': match['range'].strip()}
        for match in _GRADE_BAND.finditer(reference or '')
    ]


def _matching_source_bands(value, bands):
    """Return all printed matches; shared endpoints have no implicit priority."""
    number = _number_key(value)
    if not isinstance(number, Decimal):
        return []
    result = []
    for band in bands:
        interval = unicodedata.normalize('NFKC', band['range']).strip()
        bounds = _REFERENCE_BAND.fullmatch(interval)
        limit = re.fullmatch(rf'([<>]=?|[≤≥])\s*({_DECIMAL_LITERAL})', interval)
        if bounds:
            matches = Decimal(bounds[1]) <= number <= Decimal(bounds[2])
        elif limit:
            operator, bound = limit[1], Decimal(limit[2])
            matches = {
                '>': number > bound,
                '>=': number >= bound,
                '≥': number >= bound,
                '<': number < bound,
                '<=': number <= bound,
                '≤': number <= bound,
            }[operator]
        else:
            continue
        if matches:
            result.append(band)
    return result


def source_reference_display(row: dict) -> str | None:
    """Format current and normal source bands without changing the JSON range.

    This is also callable by a renderer working only from the public row.
    A literal source grade wins; otherwise every matching endpoint band is
    displayed together, without choosing the most adverse one.
    """
    reference = row.get('referenceRange')
    bands = _source_grade_bands(reference)
    if len(bands) < 2:
        return reference
    reported = [band for band in bands if _source_key(band['label']) == _source_key(row.get('status'))]
    matched = _matching_source_bands(row.get('value'), bands)
    current = reported if len(reported) == 1 else matched
    if not current or len(bands) == 2:
        return reference
    chosen = [*current, *(band for band in bands if band['label'] in _NORMAL_STATES and band not in current)]
    return '；'.join(f'{band["label"]}: {band["range"]}' for band in chosen)


def _grade_semantics(row, *, reported_status, authority, kind='reported_grade'):
    bands = _source_grade_bands(row.get('referenceRange'))
    matched = _matching_source_bands(row.get('value'), bands)
    reported = [band for band in bands if _source_key(band['label']) == _source_key(reported_status)]
    if (
        kind == 'graded_function_score'
        and len(reported) == 1
        and isinstance(_number_key(row['value']), Decimal)
        and reported[0] not in matched
    ):
        resolved, resolution = None, 'source_reference_conflict'
    elif len(reported) == 1:
        resolved, resolution = reported[0]['label'], 'source_reported'
    elif len(matched) == 1:
        resolved, resolution = matched[0]['label'], 'unique_source_band'
    else:
        resolved, resolution = None, 'unresolved'
    return {
        'kind': kind,
        'measurementType': 'functional_score' if kind == 'graded_function_score' else 'source_graded_index',
        'objectScope': 'measurement',
        'reportedObject': row['name'],
        'originalValue': row['value'],
        'originalStatus': reported_status,
        'displayStatus': row.get('status'),
        'referenceText': row.get('referenceRange'),
        'referenceBands': bands,
        'matchingBands': matched,
        'boundaryAmbiguous': len(matched) > 1 and (kind != 'graded_function_score' or resolved is None),
        'resolvedGrade': resolved,
        'gradeResolution': resolution,
        'statusAuthority': authority,
        'referenceRole': 'labelled_grade_bands_or_reported_reference',
        'referenceDisplay': source_reference_display(row),
    }


def _row(item):
    from .assessment_reviewed_fields import project_reviewed_field

    state = _source_state(item)
    detection = getattr(item, 'detection_status', None)
    if (
        not _present(item.name)
        or not item.sources
        or not (_present(item.value) or _present(state) or detection in _DETECTION_LABELS)
    ):
        return None
    dates = {source.observed_at for source in item.sources if source.source_code == 'C' and source.observed_at}
    details = (
        getattr(item, 'details', None)
        if item.code in {'virulenceGenes', 'antibioticResistanceGenes', *_CHILD_STRUCTURE_PANELS}
        else None
    )
    row = {
        'code': item.code,
        'name': item.name,
        'value': item.value if _present(item.value) else None,
        'unit': item.unit if _present(item.value) else None,
        'referenceRange': item.reference_range,
        'status': state or _DETECTION_LABELS.get(detection),
        'observedAt': next(iter(dates)) if len(dates) == 1 else None,
        'interpretation': None,
        'sources': [source.model_dump(mode='json', by_alias=True) for source in item.sources],
        'evidenceRefs': list(item.evidence_refs),
        **({'detectionStatus': detection} if detection in _DETECTION_LABELS else {}),
        **({'details': details} if _present(details) else {}),
    }
    if (
        item.code in _STRUCTURE_ORDER
        and (_present(state) or _source_grade_bands(row['referenceRange']))
        and isinstance(_number_key(row['value']), Decimal)
    ):
        row['_sourceSemantics'] = _grade_semantics(row, reported_status=state, authority='source_reported')
    project_reviewed_field(item, row)
    return row


def _rows(items):
    rows = []
    for item in items:
        row = _row(item)
        if row and not any(
            {key: value for key, value in current.items() if key != '_assessmentReviewedInterpretation'}
            == {key: value for key, value in row.items() if key != '_assessmentReviewedInterpretation'}
            for current in rows
        ):
            rows.append(row)
    return rows


def _group_source_semantics(rows):
    """Carry measurement identity to authors without adding public fields."""
    result = []
    for row in rows:
        semantics = row.get('_sourceSemantics')
        if not semantics:
            continue
        item = {
            **semantics,
            'recordIds': row.get(
                '_semanticOwnerRecordIds',
                list(dict.fromkeys(source['recordId'] for source in row['sources'] if source['recordId'])),
            ),
        }
        if item not in result:
            result.append(item)
    return result


def _abundance_semantics(row, occurrences):
    panels = list(
        dict.fromkeys(_occurrence_path(item).partition('[')[0] for item in _row_occurrences(row, occurrences))
    )
    return {
        'kind': 'relative_abundance',
        'measurementType': 'relative_abundance',
        'objectScope': 'measurement',
        'reportedObject': row['name'],
        'originalValue': row['value'],
        'originalStatus': row['status'],
        'displayStatus': row['status'],
        'stateMeaning': (
            'abundance_within_reference' if row['status'] in _NORMAL_STATES else 'reported_abundance_status'
        ),
        'referenceText': row['referenceRange'],
        'sourcePanels': panels,
        'sourcePanelRoles': list(
            dict.fromkeys(
                'composition_chart'
                if panel.startswith(('jun_top_', 'jun_top5_', 'jun_children_top5_'))
                else 'source_panel'
                for panel in panels
            )
        ),
    }


def source_semantics_by_fact_id(view: dict) -> dict[str, list[dict]]:
    """Index authoritative source meanings independently of a selected cell."""
    result = defaultdict(list)

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        semantics = value.get('_sourceSemantics')
        if isinstance(semantics, dict):
            entries = [
                {
                    **semantics,
                    'recordIds': value.get(
                        '_semanticOwnerRecordIds', [source['recordId'] for source in value.get('sources', ())]
                    ),
                }
            ]
        else:
            entries = semantics or []
        for entry in entries:
            for fact_id in entry.get('recordIds', ()):
                if not fact_id:
                    continue
                # A displayed observation may coalesce identical values from
                # several panels. Its literal occurrence still owns just its
                # own assay; a canonical fact referencing both retains both.
                assays = [assay for assay in entry.get('assays', ()) if fact_id in assay['recordIds']]
                scoped = {**entry, **_assay_scope(assays)} if assays else entry
                if locators := entry.get('observationLocatorBindings', {}).get(fact_id):
                    scoped = {**scoped, 'observationLocators': list(locators)}
                    observation_row = value if 'value' in value else {
                        **entry.get('observedValue', {}),
                        'status': entry.get('displayStatus', entry.get('originalStatus')),
                        'referenceRange': entry.get('referenceText'),
                    }
                    scoped.update(typed_measurement_observation(observation_row, scoped))
                if scoped not in result[fact_id]:
                    result[fact_id].append(scoped)
        for key, item in value.items():
            if not key.startswith('_'):
                visit(item)

    visit(view)
    visit(view.get('_reportedAssessmentRows', []))
    return dict(result)


def source_semantic_context(entry: dict) -> dict:
    """Model-facing identity; values and complete references stay on their record."""
    fields = (
        'kind',
        'measurementType',
        'objectScope',
        'stateMeaning',
        'targetName',
        'originalStatus',
        'displayStatus',
        'statusAuthority',
        'detectedGenes',
        'candidateGenes',
        'referenceBands',
        'matchingBands',
        'boundaryAmbiguous',
        'resolvedGrade',
        'gradeResolution',
        'reportedDirections',
        'sourcePanelRoles',
        'measurandId',
        'measurandDefinition',
        'panelDefinition',
        'assessmentDefinition',
        'statusRole',
        'comparisonBasis',
        'referenceBasis',
        'statusReferenceComparable',
        'sourceConflictCriterion',
        'assayIds',
        'sourcePanel',
        'sourcePanels',
        'assays',
        'organismKinds',
        'sameKindAssayIds',
        'counterpartAssays',
        'referenceText',
        'observationLocators',
        'typedObservationProtocol',
        'measurementMeaning',
        'observationId',
        'observationIds',
        'observedValue',
        'referenceComparison',
        'detection',
        'analyticalLimits',
    )
    result = {key: entry[key] for key in fields if entry.get(key) not in (None, '', [], {})}
    if entry.get('objectScope') == 'reported_assessment' and entry.get('reportedObject'):
        result['reportedObject'] = entry['reportedObject']
    return result


def first_reported_abundance_assay(row):
    """Identify the first recorded source panel from its explicit locator."""
    for source in row.get('sources') or ():
        if source.get('sourceCode') != 'C':
            continue
        record_id = source.get('recordId') or ''
        if not (path := source_path_from_record(record_id)):
            continue
        assay = assay_identity(path, checksum=record_id.split(':', 2)[1])
        if assay['type'] in {'relative_abundance_panel', 'association_abundance_panel', 'composition_chart'}:
            return assay
    return None


def organism_assessment_display_group(row):
    """Route registered classifications independently of their displayed status."""
    for source in row.get('sources') or ():
        if source.get('sourceCode') != 'C':
            continue
        path = source_path_from_record(source.get('recordId') or '')
        if path and (definition := source_assessment_semantics(path, category='healthAssociation')):
            if group := _ORGANISM_ASSESSMENT_GROUPS.get(definition['assessmentDefinition']['sourcePanel']):
                return group
    return None


def _assay_scope(assays):
    panels = list(dict.fromkeys(assay['sourcePath'] for assay in assays))
    return {
        'assays': assays,
        'assayIds': [assay['id'] for assay in assays],
        'sourcePanel': panels[0] if len(panels) == 1 else None,
        'sourcePanels': panels,
        'sourcePanelRoles': list(dict.fromkeys(assay['type'] for assay in assays)),
        'organismKinds': list(dict.fromkeys(assay['organismKind'] for assay in assays if assay.get('organismKind'))),
    }


def _source_location(parsed, path, page=None):
    prefix = f'page:{page}:template:' if type(page) is int and page > 0 else 'json:'
    return f'source:{parsed.source_checksum}:{prefix}{path}' if parsed.source_checksum else None


def _assay_catalogue(parsed):
    """Keep every original member under its own source-defined assay."""
    catalogue, bindings = {}, defaultdict(list)

    def add(path, member, record_ids, *, name=None, measurement_type=None):
        if not path:
            return
        descriptor = assay_identity(
            path,
            checksum=parsed.source_checksum,
            source_name=parsed.source_name,
            name=name,
            measurement_type=measurement_type,
        )
        assay = catalogue.setdefault(descriptor['id'], {**descriptor, 'recordIds': [], 'members': []})
        for record_id in record_ids:
            if not record_id:
                continue
            if record_id not in assay['recordIds']:
                assay['recordIds'].append(record_id)
            if descriptor['id'] not in bindings[record_id]:
                bindings[record_id].append(descriptor['id'])
        if member is not None:
            existing = next((entry for entry in assay['members'] if entry['sourcePath'] == path), None)
            if existing is None:
                assay['members'].append({**member, 'sourcePath': path, 'recordIds': list(filter(None, record_ids))})
            else:
                existing['recordIds'].extend(
                    record_id for record_id in record_ids if record_id and record_id not in existing['recordIds']
                )

    for index, item in _source_occurrences(parsed).items():
        path = _occurrence_path(item)
        add(
            path,
            {
                'name': item.get('chineseName') or item.get('latinName'),
                'latinName': item.get('latinName'),
                'value': item.get('value'),
                'unit': item.get('unit'),
                'referenceRange': item.get('referenceRange'),
                'status': item.get('sourceStatus'),
                'detectionStatus': item.get('detectionStatus'),
                'sourcePage': item.get('sourcePage'),
                'objectScope': 'measurement',
            },
            [f'parsed.sourceTaxonOccurrences[{index}]', _source_location(parsed, path, item.get('sourcePage'))],
            name=item.get('sourceSection'),
            measurement_type='relative_abundance',
        )
    for container, items in (
        ('metrics', parsed.metrics),
        ('evaluationPanelSummaries', parsed.evaluation_panel_summaries),
    ):
        for index, item in enumerate(items):
            path = getattr(item, 'source_path', None)
            if not path or (getattr(item, 'source_checksum', None), getattr(item, 'source_file', None)) != (
                parsed.source_checksum,
                parsed.source_name,
            ):
                continue
            add(
                path,
                {
                    'name': item.name,
                    'value': item.value,
                    'unit': item.unit,
                    'referenceRange': item.reference_range,
                    'status': item.status,
                    'objectScope': 'panel' if container == 'evaluationPanelSummaries' else 'measurement',
                },
                [f'parsed.{container}[{index}]', _source_location(parsed, path)],
                name=getattr(item, 'source_section', None) or item.name,
            )
    for item in _selected_source_rows(parsed):
        path = _source_path(item)
        category_member = path.partition('[')[0] in {'virulence_gene_result', 'naiyao_gene_result'} and '[' in path
        extra = item.model_extra or {}
        assessment = source_assessment_semantics(path, category=extra.get('sourceCategory'))
        add(
            path,
            {
                'name': item.item_name,
                'value': item.test_result_value,
                'unit': getattr(item, 'unit', None),
                'referenceRange': item.reference_range,
                'status': item.result_status_name,
                'objectScope': 'reported_assessment' if assessment else 'measurement' if category_member else 'panel',
                **(
                    {key: extra[key] for key in ('detectedGenes', 'candidateGenes') if key in extra}
                    if category_member
                    else {}
                ),
            },
            [item._evaluation_record_id, _source_location(parsed, path)],
            name=item.item_name,
        )
    return catalogue, bindings, add


def _microbiome_rows(value):
    if isinstance(value, list):
        for child in value:
            yield from _microbiome_rows(child)
    elif isinstance(value, dict):
        if isinstance(value.get('sources'), list) and 'name' in value and 'value' in value:
            yield value
        else:
            for key, child in value.items():
                if not key.startswith('_'):
                    yield from _microbiome_rows(child)


def _semantic_names(value):
    text = re.sub(r'（(?:构成图谱|靶标面板|基因序列|序列面板)）$', '', str(value or ''))
    return {_source_key(part) for part in re.split(r'\s+[/|]\s+', text) if _present(part)}


def _semantic_owner_matches(row, semantics, record, *, literal=False):
    """A supporting citation does not own the displayed value or status."""
    if (row.get('_sourceAssessmentFile') and record.get('source')
            and row['_sourceAssessmentFile'] != record['source']):
        return False
    values = (row.get('value'), semantics.get('originalValue'))
    source_value = record.get('value')
    if not any(_present(value) and _number_key(value) == _number_key(source_value) for value in values):
        return False
    if record.get('unit') and row.get('unit') and _source_key(record['unit']) != _source_key(row['unit']):
        return False
    date = record.get('date')
    if date and row.get('observedAt') and not _same_date(date, row['observedAt']):
        return False
    if record.get('factId', record.get('fact_id')) == row.get('code'):
        return True
    source_names = _semantic_names(record.get('name')) | _semantic_names(record.get('latinName'))
    row_names = set().union(
        *(
            _semantic_names(name)
            for name in (
                row.get('name'),
                semantics.get('reportedObject'),
                semantics.get('originalName'),
                semantics.get('targetName'),
            )
        )
    )
    if source_names & row_names:
        return True
    # A literal scalar/panel field locates its own value even when the view
    # uses a canonical display label. Taxon occurrences still require their
    # identity: equal zeros in different organisms are not interchangeable.
    return literal and 'latinName' not in record and bool(record.get('sourcePath'))


def _semantic_owners(row, semantics, facts, literals):
    owners = []
    for source in row['sources']:
        record_id = source.get('recordId')
        if record_id in facts:
            matches = _semantic_owner_matches(row, semantics, facts[record_id])
        elif record_id in literals:
            matches = _semantic_owner_matches(row, semantics, literals[record_id], literal=True)
        else:
            matches = False
        if matches and record_id not in owners:
            owners.append(record_id)
    return owners


def _same_literal_comparison(row, record):
    """Exact source comparison, independent of a display name or alias."""
    status = record.get('status', record.get('sourceStatus'))
    return (
        _present(row.get('value')) and _present(record.get('value'))
        and _number_key(row['value']) == _number_key(record['value'])
        and _source_key(row.get('unit')) == _source_key(record.get('unit'))
        and _reference_key(row.get('referenceRange', row.get('reference_range')))
        == _reference_key(record.get('referenceRange', record.get('reference_range')))
        and _source_key(row.get('status')) == _source_key(status)
    )


def _own_occurrence_aliases(data, facts, literals):
    """Use the builder's own physical source edges, never taxon-name guessing.

    An authored field can add supporting facts to sources. When a field has
    an assembler receipt, its pre-application native source set supplies the
    alias edges; added supporting citations cannot acquire value ownership.
    """
    microbiome = data.microbiome_assessment
    candidates = [*microbiome.overall_structure, *microbiome.health_metrics, *microbiome.taxa,
        *microbiome.antimicrobial_resistance, *data.appendices.source_items,
        *(item for pathway in data.functional_pathways.pathways for item in pathway.related_metrics)]
    result = defaultdict(set)
    for item in candidates:
        row = _row(item)
        if row is None:
            continue
        native_sets = [set(ref[1] for ref in receipt.native_sources if ref[0] == 'C')
            for receipt in (getattr(item, '_assessment_reviewed_fields', {}) or {}).values()
            if hasattr(receipt, 'native_sources')]
        native = set.intersection(*native_sets) if native_sets else {
            source['recordId'] for source in row['sources'] if source['sourceCode'] == 'C'
        }
        anchors = [record_id for record_id in native if record_id in literals
            and literals[record_id].get('objectScope') == 'measurement'
            and _same_literal_comparison(row, literals[record_id])]
        if not anchors:
            continue
        for record_id in native:
            fact = facts.get(record_id)
            if fact is None or not _same_literal_comparison(row, fact):
                continue
            if fact.get('date') and row.get('observedAt') and not _same_date(fact['date'], row['observedAt']):
                continue
            result[record_id].update(anchor for anchor in anchors
                if _same_literal_comparison(fact, literals[anchor]))
    return result


def _reported_assessment_aliases(row, data):
    """Bridge only native appendix aliases of this same literal assessment."""
    anchors = {source['recordId'] for source in row['sources']}
    for item in data.appendices.source_items:
        if item.source_type != 'microbiome':
            continue
        candidate = _row(item)
        if candidate is None or not _same_literal_comparison(row, candidate):
            continue
        native_sets = [set(ref[1] for ref in receipt.native_sources if ref[0] == 'C')
            for receipt in (getattr(item, '_assessment_reviewed_fields', {}) or {}).values()
            if hasattr(receipt, 'native_sources')]
        native = set.intersection(*native_sets) if native_sets else {
            source['recordId'] for source in candidate['sources'] if source['sourceCode'] == 'C'
        }
        if not anchors.intersection(native):
            continue
        row['sources'].extend(source for source in candidate['sources']
            if source['recordId'] in native and source not in row['sources'])


def _reported_assessment_rows(parsed, data):
    """Cover registered source assessments even when no public cell displays them."""
    if not parsed.source_checksum:
        return []
    result = []
    for item in _selected_source_rows(parsed):
        path = _source_path(item)
        category = (item.model_extra or {}).get('sourceCategory')
        semantics = source_assessment_semantics(path, category=category)
        if semantics is None or not _present(item.test_result_value):
            continue
        row = {
            'code': item._evaluation_record_id,
            'name': item.item_name,
            'value': item.test_result_value,
            'unit': getattr(item, 'unit', None),
            'referenceRange': item.reference_range,
            'status': item.result_status_name,
            'observedAt': item.report_time,
            'sources': [_supplement_reference(item)],
            '_sourceAssessmentPath': path,
            '_sourceAssessmentFile': parsed.source_name,
            '_sourceSemantics': {
                **semantics,
                'reportedObject': semantics['assessmentDefinition']['label'] + '（菌群关联评估）',
                'originalValue': item.test_result_value,
                'originalStatus': item.result_status_name,
                'displayStatus': item.result_status_name,
            },
        }
        if locator := _source_location(parsed, path):
            source = {'sourceCode': 'C', 'recordId': locator, 'observedAt': item.report_time}
            if source not in row['sources']:
                row['sources'].append(source)
        _reported_assessment_aliases(row, data)
        result.append(row)
    for audit in getattr(parsed, '_evaluation_source_audit', ()):
        if (audit.get('sourceChecksum'), audit.get('sourceFile')) != (parsed.source_checksum, parsed.source_name):
            continue
        for mapping in audit.get('fieldMap', ()):
            index = mapping.get('index')
            if (mapping.get('lane') != 'diseaseResults' or type(index) is not int
                    or not 0 <= index < len(parsed.disease_results)):
                continue
            path = mapping.get('sourcePath') or ''
            semantics = source_assessment_semantics(path, category='diseaseResults', family=audit.get('family'))
            item = parsed.disease_results[index]
            if semantics is None or not _present(item.source_score):
                continue
            date = parsed.sample.report_date
            sources = [{'sourceCode': 'C', 'recordId': item.disease_id, 'observedAt': date}]
            if locator := _source_location(parsed, path):
                sources.append({'sourceCode': 'C', 'recordId': locator, 'observedAt': date})
            result.append({
                'code': item.disease_id,
                'name': item.name,
                'value': item.source_score,
                'unit': None,
                'referenceRange': None,
                'status': item.source_level,
                'observedAt': date,
                'sources': sources,
                '_sourceAssessmentPath': path,
                '_sourceAssessmentFile': parsed.source_name,
                '_sourceSemantics': {
                    **semantics,
                    'reportedObject': item.name + '（菌群模型评估）',
                    'originalValue': item.source_score,
                    'originalStatus': item.source_level,
                    'displayStatus': item.source_level,
                },
            })
    return result


def _annotate_source_assays(view, parsed, source_facts, *, data):
    catalogue, bindings, add = _assay_catalogue(parsed)
    facts = {}
    for source in source_facts:
        fact = source if isinstance(source, dict) else source.model_dump(mode='json', by_alias=True)
        if record_id := fact.get('factId', fact.get('fact_id')):
            facts[record_id] = fact
    assessments = _reported_assessment_rows(parsed, data)
    for row in assessments:
        add(row['_sourceAssessmentPath'], {
            key: row[key] for key in ('name', 'value', 'unit', 'referenceRange', 'status')
        } | {'objectScope': 'reported_assessment'}, [source['recordId'] for source in row['sources']],
            name=row['name'], measurement_type=row['_sourceSemantics']['measurementType'])
    literals = {
        record_id: member
        for assay in catalogue.values()
        for member in assay['members']
        for record_id in member['recordIds']
    }
    aliases = _own_occurrence_aliases(data, facts, literals)
    rows = [*list(_microbiome_rows(view)), *assessments]
    for row in rows:
        existing = row.get('_sourceSemantics') or {}
        owners = _semantic_owners(row, existing, facts, literals)
        owned_literals = set(owners).intersection(literals)
        for record_id, anchors in aliases.items():
            if (anchors.intersection(owned_literals) and _same_literal_comparison(row, facts[record_id])
                    and record_id not in owners):
                owners.append(record_id)
        row['_semanticOwnerRecordIds'] = owners
        for record_id in owners:
            if record_id and record_id not in bindings and (path := source_path_from_record(record_id)):
                add(path, None, [record_id])
        assay_ids = list(dict.fromkeys(assay_id for record_id in owners for assay_id in bindings.get(record_id, ())))
        if not assay_ids:
            path = _ABUNDANCE_PANEL_SOURCES.get(row.get('code'))
            if (
                row.get('code') == 'virulenceGenes'
                or row.get('_sourceSemantics', {}).get('kind') == 'gene_sequence_target'
            ):
                path = 'virulence_gene_result'
            if path:
                add(path, None, owners, name=row['name'])
                assay_ids = list(
                    dict.fromkeys(assay_id for record_id in owners for assay_id in bindings.get(record_id, ()))
                )
        assays = []
        for assay_id in assay_ids:
            assay = catalogue[assay_id]
            # Generic fact aliases own all panels cited by that fact. A
            # concrete occurrence/path alias belongs only to its actual panel.
            owned = [
                record_id
                for record_id in owners
                if (
                    assay_id in bindings.get(record_id, ())
                    or not (_TAXON_OCCURRENCE.fullmatch(record_id) or source_path_from_record(record_id))
                )
            ]
            assay['recordIds'].extend(record_id for record_id in owned if record_id not in assay['recordIds'])
            assays.append(
                {key: value for key, value in assay.items() if key not in {'members', 'recordIds'}}
                | {
                    'recordIds': owned,
                }
            )
        abundance = any(assay['measurementBasis'] == 'within_sample_relative_abundance' for assay in assays)
        panel_owner = any(
            literals[record_id]['objectScope'] == 'panel' for record_id in owners if record_id in literals
        )
        semantics = {
            'kind': 'relative_abundance_panel'
            if abundance and panel_owner
            else 'relative_abundance'
            if abundance
            else 'source_measurement',
            'measurementType': 'relative_abundance' if abundance else 'source_measurement',
            'objectScope': 'panel' if panel_owner else 'measurement',
            'reportedObject': row['name'],
            'originalValue': row['value'],
            'originalStatus': row.get('status'),
            'displayStatus': row.get('status'),
            **existing,
            **_assay_scope(assays),
        }
        locator_bindings = {}
        for record_id in owners:
            anchors = ({record_id} if record_id in literals else aliases.get(record_id, set()))
            locators = []
            for anchor in anchors.intersection(owned_literals):
                member = literals[anchor]
                path = member.get('sourcePath')
                locator = _source_location(parsed, path, member.get('sourcePage')) if path else None
                locator = locator or anchor
                if locator not in locators:
                    locators.append(locator)
            if locators:
                locator_bindings[record_id] = sorted(locators)
        semantics['observationLocatorBindings'] = locator_bindings
        semantics['observationLocators'] = sorted({
            locator for values in locator_bindings.values() for locator in values
        })
        semantics = comparison_semantics(row, semantics)
        row['_sourceSemantics'] = {**semantics, **typed_measurement_observation(row, semantics)}
    for assay in catalogue.values():
        measurements = [member for member in assay['members'] if member['objectScope'] == 'measurement']
        values = [_number_key(member.get('value')) for member in measurements]
        assay['count'] = len(measurements)
        assay['zeroCount'] = sum(value == 0 for value in values if isinstance(value, Decimal))
        assay['nonzeroCount'] = sum(value != 0 for value in values if isinstance(value, Decimal))
        assay['unquantifiedCount'] = sum(not isinstance(value, Decimal) for value in values)
    for row in rows:
        semantics = row['_sourceSemantics']
        kinds = set(semantics.get('organismKinds', ()))
        owned_types = {assay['type'] for assay in semantics.get('assays', ())}
        same_kind = [assay for assay in catalogue.values() if assay.get('organismKind') in kinds]
        semantics['sameKindAssayIds'] = [assay['id'] for assay in same_kind]
        semantics['counterpartAssays'] = [
            {
                key: assay[key]
                for key in (
                    'id',
                    'name',
                    'type',
                    'organismKind',
                    'measurementBasis',
                    'sourcePath',
                    'count',
                    'zeroCount',
                    'nonzeroCount',
                    'unquantifiedCount',
                    'recordIds',
                )
            }
            for assay in same_kind
            if assay['type'] not in owned_types
        ]
    organism_groups = {group['code']: group for group in view['organismGroups']}
    for row in assessments:
        if group := organism_groups.get(organism_assessment_display_group(row)):
            group['rows'].append({
                **row,
                'name': row['_sourceSemantics']['reportedObject'],
                'interpretation': None,
                'evidenceRefs': [],
            })
    # Group copies must follow the per-record enrichment rather than retain
    # the earlier, lossy semantic snapshots assembled for display.
    for groups in ('structureGroups', 'organismGroups', 'functionalGroups'):
        for group in view[groups]:
            group['_sourceSemantics'] = _group_source_semantics(list(_microbiome_rows(group)))
            group['_sourceAssayIds'] = list(
                dict.fromkeys(assay_id for entry in group['_sourceSemantics'] for assay_id in entry.get('assayIds', ()))
            )
    view['_assayGroups'] = list(catalogue.values())
    view['_reportedAssessmentRows'] = assessments


def _reference_display_rows(rows):
    return [
        {
            'name': row['name'],
            'text': source_reference_display(row),
            'recordIds': [source['recordId'] for source in row['sources'] if source['recordId']],
        }
        for row in rows
        if len(_source_grade_bands(row.get('referenceRange'))) > 1
    ]


def _structure_rows(microbiome):
    candidates = [
        *microbiome.overall_structure,
        *(row for row in microbiome.health_metrics if row.code in _STRUCTURE_ORDER),
    ]
    positions = {code: index for index, code in enumerate(_STRUCTURE_ORDER)}
    rows = _rows(sorted(candidates, key=lambda item: positions.get(item.code, len(positions))))
    return _unique_categorical_measurements(rows)


def _unique_categorical_measurements(rows):
    """Coalesce matching summary/detail cells from one dated observation."""
    result = []
    seen = {}
    for row in rows:
        if row['code'] not in _CATEGORICAL_STRUCTURE_CODES or row['value'] is None:
            result.append(row)
            continue
        identity = row['code'], _source_key(row['value']), row['observedAt']
        if identity not in seen:
            seen[identity] = len(result)
            result.append(row)
            continue
        index = seen[identity]
        first, second = result[index], row
        if any(ref['recordId'].startswith('parsed.metrics[') for ref in second['sources'] if ref['recordId']):
            first, second = second, first
        result[index] = {
            **first,
            'status': first['status'] or second['status'],
            'interpretation': first['interpretation'] or second['interpretation'],
            'evidenceRefs': first['evidenceRefs'] if first['interpretation'] else second['evidenceRefs'],
            'sources': [*first['sources'], *(ref for ref in second['sources'] if ref not in first['sources'])],
        }
    return result


def _barrier_summary(rows):
    """Keep a recorded panel judgement independent from its constituent results."""
    axis_names = {
        'barrierRelatedBacteria': '屏障相关菌群组合',
        'antiInflammatoryCapacity': '菌群抗炎能力评分',
        'gutImmunity': '肠道免疫力评分',
    }
    states = defaultdict(set)
    deviations = []
    for row in rows:
        code, status = row['code'], row['status']
        if code == 'barrierRelatedBacteria' and not _present(status):
            status = row['value']
        if code in axis_names and isinstance(status, str) and _present(status):
            states[code].add(status)
        elif code not in axis_names and _source_key(row['unit']) == '%' and _source_is_abnormal(status):
            deviations.append(f'{row["name"]}相对丰度{status}')

    parts = []
    panel_states = states['barrierRelatedBacteria']
    if len(panel_states) == 1:
        status = next(iter(panel_states))
        if status == '异常':
            parts.append('屏障相关菌群组合有所偏离。')
        elif status in _NORMAL_PANEL_STATES:
            parts.append('屏障相关菌群组合未见异常。')
        elif _source_is_abnormal(status):
            parts.append(f'屏障相关菌群组合为{status}。')
    if deviations:
        distinct = list(dict.fromkeys(deviations))
        parts.append('、'.join(distinct) + '。' if len(distinct) <= 2 else '多项相关菌群的相对丰度出现偏离。')
    scores = [
        f'{axis_names[code]}为{next(iter(states[code]))}'
        for code in ('antiInflammatoryCapacity', 'gutImmunity')
        if len(states[code]) == 1
    ]
    if scores:
        parts.append('，'.join(scores) + '。')
    return ''.join(parts) or None


def _composition_summary(rows):
    """Describe reference comparison, original F/B assessment and enterotype separately."""
    ratios = {
        (_number_key(row['value']), _reference_key(row['referenceRange']), row['status'])
        for row in rows
        if row['code'] == 'fbRatio' and _present(row['value'])
    }
    types = {str(row['value']) for row in rows if row['code'] == 'enterotype' and _present(row['value'])}
    parts = []
    if len(ratios) == 1:
        value, reference, status = next(iter(ratios))
        if isinstance(value, Decimal) and isinstance(reference, tuple) and reference[0] <= reference[1]:
            relation = '在参考范围内' if reference[0] <= value <= reference[1] else '在参考范围外'
            parts.append(f'F/B比值{relation}。')
        if _present(status):
            parts.append(f'F/B分项判定为{status}。')
    if len(types) == 1:
        parts.append(f'肠型为{next(iter(types))}。')
    return ''.join(parts) or None


def _structure_groups(rows, parsed, microbiome):
    gmwi_degrees = [
        item
        for item in _selected_source_rows(parsed)
        if _source_path(item) == 'jun_gmwi2_degree' and _present(item.test_result_value)
    ]
    groups = []
    for code, name, keys in _STRUCTURE_GROUPS:
        measurements = [row for row in rows if row['code'] in keys]
        panel = _CHILD_STRUCTURE_PANELS.get(code)
        panel_states = (
            {
                str(row['status'] if _present(row['status']) else row['value']).strip()
                for row in measurements
                if _present(row['status']) or _present(row['value'])
            }
            if panel
            else set()
        )
        if panel:
            measurements.extend(_child_panel_members(parsed, panel))
        if code == 'composition':
            measurements.extend(
                _rows(item for item in microbiome.phylum_composition if _source_is_abnormal(_source_state(item)))
            )
        if code == 'barrier':
            measurements.extend(
                _organism_rows(
                    item
                    for item in microbiome.taxa
                    if _source_is_abnormal(_source_state(item))
                    and _taxon_panels(item, parsed.source_taxon_occurrences) & {'jun_pzss', 'jun_children_pzss'}
                )
            )
        if not measurements:
            continue
        group = {'code': code, 'name': name, 'rows': measurements}
        if panel:
            if len(panel_states) == 1:
                group['status'] = next(iter(panel_states))
        if code == 'overallBalance':
            states = {
                str(row['value']).strip()
                for row in measurements
                if row['code'] == 'balanceStatus' and _present(row['value'])
            }
            if len(states) == 1:
                group['status'] = next(iter(states))
        if code == 'gmwi2' and len(measurements) == 1:
            row = measurements[0]
            degrees = [item for item in gmwi_degrees if _same_date(item.report_time, row['observedAt'])]
            values = {str(item.test_result_value).strip() for item in degrees}
            if len(values) == 1:
                degree = next(iter(values))
                status = row['status']
                if not _present(status) or _source_key(degree) not in _source_key(status):
                    group['status'] = f'{status}（{degree}）' if _present(status) else degree
                sources = list(row['sources'])
                for item in degrees:
                    if item._evaluation_record_id and (source := _supplement_reference(item)) not in sources:
                        sources.append(source)
                measurements[0] = {**row, 'sources': sources}
        if code == 'gutAge' and len(measurements) == 1:
            # Report the age difference only for the subject age read from
            # this same source, without importing ages from other reports.
            age = parsed.subject.age
            raw = str(measurements[0]['value']).strip()
            measured_age = re.fullmatch(r'(\d+(?:\.\d+)?)\s*岁?', raw)
            if type(age) is int and age >= 0 and measured_age:
                difference = Decimal(measured_age[1]) - age
                group['actualAge'] = age
                group['gutAgeDifference'] = float(difference)
                group['status'] = (
                    '高于实足年龄' if difference > 0 else '低于实足年龄' if difference < 0 else '与实足年龄相同'
                )
                row = measurements[0]
                if not _present(row.get('referenceRange')):
                    measurements[0] = {**row, 'referenceRange': f'个人对照年龄 {age} 岁'}
        if code == 'barrier':
            for index, row in enumerate(measurements):
                if (
                    row['code'] not in {'antiInflammatoryCapacity', 'gutImmunity'}
                    or row['status'] not in _NORMAL_STATES
                ):
                    continue
                bound = _EXCLUSIVE_REFERENCE.fullmatch(
                    unicodedata.normalize('NFKC', row['referenceRange'] or '').strip()
                )
                if bound and _number_key(row['value']) == Decimal(bound[1]):
                    measurements[index] = {**row, 'templateStatus': (
                        f'{row["name"]}报告判为{row["status"]}；{row["value"]}处于分界值，未达到严格界值{row["referenceRange"]}'
                    )}
        if semantics := _group_source_semantics(measurements):
            group['_sourceSemantics'] = semantics
        if references := _reference_display_rows(measurements):
            group['_referenceDisplay'] = references
        groups.append(group)
    return groups


def _child_panel_members(parsed, panel):
    """Project each supplier panel occurrence with its own label and grade."""
    rows = []
    for index, occurrence in enumerate(parsed.source_taxon_occurrences):
        path = _occurrence_path(occurrence)
        if not re.fullmatch(rf'{re.escape(panel)}\[\d+\]', path):
            continue
        if (occurrence.get('sourceChecksum'), occurrence.get('sourceFile')) != (
            parsed.source_checksum,
            parsed.source_name,
        ):
            continue
        name = occurrence.get('chineseName') or occurrence.get('latinName')
        if not (_present(name) and _present(occurrence.get('value'))):
            continue
        observed_at = parsed.sample.report_date
        location = f'parsed.sourceTaxonOccurrences[{index}]'
        sources = [{'sourceCode': 'C', 'recordId': location, 'observedAt': observed_at}]
        if parsed.source_checksum:
            page = occurrence.get('sourcePage')
            prefix = f'page:{page}:template:' if type(page) is int and page > 0 else 'json:'
            sources.append(
                {
                    'sourceCode': 'C',
                    'recordId': f'source:{parsed.source_checksum}:{prefix}{path}',
                    'observedAt': observed_at,
                }
            )
        detection = occurrence.get('detectionStatus')
        rows.append(
            {
                'code': occurrence.get('taxonId') or location,
                'name': name,
                'value': occurrence['value'],
                'unit': occurrence.get('unit'),
                'referenceRange': occurrence.get('referenceRange'),
                'status': occurrence.get('sourceStatus') or _DETECTION_LABELS.get(detection),
                'observedAt': observed_at,
                'interpretation': None,
                'sources': sources,
                'evidenceRefs': [],
                **({'detectionStatus': detection} if detection in _DETECTION_LABELS else {}),
            }
        )
    return rows


def _source_path(item):
    extra = item.model_extra or {}
    return extra.get('sourcePanelPath') or extra.get('sourcePath') or ''


def _selected_source_rows(parsed):
    if not parsed.source_checksum:
        return
    for item in parsed.supplementary_hospital_data:
        extra = item.model_extra or {}
        if extra.get('sourceChecksum') == parsed.source_checksum and extra.get('sourceFile') == parsed.source_name:
            yield item


def _same_date(left, right):
    return bool(left and right and str(left)[:10] == str(right)[:10])


def _supplement_reference(item):
    return {'sourceCode': 'C', 'recordId': item._evaluation_record_id, 'observedAt': item.report_time}


def _sample_context(parsed):
    if parsed.source_type == 'pending':
        return {}
    sample = parsed.sample
    receipt_dates = set()
    for row in _selected_source_rows(parsed):
        if _source_path(row) == 'signDate' and _present(row.test_result_value):
            receipt_dates.add(str(row.test_result_value))
    results = {
        'sampleType': sample.sample_type,
        'sampleDate': sample.sample_date,
        'receivedDate': next(iter(receipt_dates)) if len(receipt_dates) == 1 else None,
        'reportDate': sample.report_date,
        'qualityControl': sample.quality_control,
    }
    registration = {
        str(name).strip() for audit in getattr(parsed, '_evaluation_source_audit', ())
        if (audit.get('sourceChecksum'), audit.get('sourceFile')) == (parsed.source_checksum, parsed.source_name)
        if (name := (audit.get('sourcePersonalInformation') or {}).get('name'))
    }
    if len(registration) == 1:
        label = registration.pop()
        if re.fullmatch(r'(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+', label):
            results['reportedSubjectLabel'] = label
    return {key: value for key, value in results.items() if _present(value)}


def _with_normal_baseline(group):
    """Keep confirmed baseline separate from notable or unclassified results."""
    rows = group['rows']
    normal = [row for row in rows if row['status'] in _NORMAL_STATES and row['value'] is not None]
    # Rows already coalesce identical observations. A name alone is not an
    # observation identity: different reference bands or original grades are
    # still distinct source records and must retain their own count.
    names = list(dict.fromkeys(row['name'] for row in normal))
    return {
        **group,
        'rows': [row for row in rows if row not in normal],
        'normalBaseline': {'count': len(normal), 'names': names, 'rows': normal},
    }


def _occurrence_path(occurrence):
    return occurrence.get('sourcePanelPath') or occurrence.get('sourcePath') or occurrence.get('source_path') or ''


def _taxon_panels(item, occurrences):
    panels = set()
    for source in item.sources:
        if match := _TAXON_OCCURRENCE.fullmatch(source.record_id or ''):
            index = int(match[1])
            if index < len(occurrences):
                panel = _occurrence_path(occurrences[index]).partition('[')[0]
                if panel:
                    panels.add(panel)
    return panels


def _taxon_groups(item, occurrences):
    panels = _taxon_panels(item, occurrences)
    groups = {_SOURCE_PANEL_GROUPS[panel] for panel in panels if panel in _SOURCE_PANEL_GROUPS}
    if category := _TAXON_GROUPS.get(item.category):
        groups.add(category)
    # Classification comes from the original assay/chart, never a guessed
    # kingdom inferred from the displayed Chinese or Latin organism name.
    if groups - {'composition'}:
        groups.discard('composition')
    # Rows unique to an association or child structure panel have their own
    # scope and do not acquire a community-composition classification.
    scoped_only = panels and all(panel in _CHILD_STRUCTURE_PANELS.values() for panel in panels)
    if not groups and not scoped_only and _source_is_abnormal(_source_state(item)):
        groups.add('composition')
    return groups


def _number_key(value):
    normalized = _source_key(value).replace('−', '-') if _present(value) else None
    if normalized and re.fullmatch(_DECIMAL_LITERAL, normalized):
        return Decimal(normalized)
    return normalized


def _reference_key(value):
    normalized = _source_key(value).replace('−', '-') if _present(value) else None
    if normalized and (match := _REFERENCE_BAND.fullmatch(normalized)):
        return Decimal(match[1]), Decimal(match[2])
    return normalized


def _observation_anchors(row):
    anchors = set()
    for ref in row['sources']:
        if ref['sourceCode'] != 'C' or not ref['recordId']:
            continue
        record_id = ref['recordId']
        if _TAXON_OCCURRENCE.fullmatch(record_id):
            anchors.add(record_id)
        elif row['observedAt'] and (snapshot := _SOURCE_SNAPSHOT.match(record_id)):
            # Printed panels from the same dated report may repeat the exact
            # same result. Distinct snapshots are not interchangeable.
            anchors.add(f'snapshot:{snapshot[1].casefold()}')
    return anchors


def _taxon_measurement(item, row):
    name = unicodedata.normalize('NFKC', getattr(item, 'scientific_name', None) or row['name']).casefold()
    return (
        ' '.join(name.split()),
        _number_key(row['value']),
        _source_key(row['unit']),
        _reference_key(row['referenceRange']),
        row['observedAt'],
    )


def _taxon_membership(items, occurrences):
    membership = {id(item): _taxon_groups(item, occurrences) for item in items}
    explicit = defaultdict(list)
    composition = []
    for item in items:
        groups = membership[id(item)]
        if not groups or not (row := _row(item)):
            continue
        identity = _taxon_measurement(item, row)
        anchors = _observation_anchors(row)
        if groups == {'composition'}:
            composition.append((item, identity, anchors))
        else:
            explicit[identity].append((groups, anchors))
    for item, identity, anchors in composition:
        matched_groups = (groups for groups, locations in explicit[identity] if anchors & locations)
        if confirmed := set().union(*matched_groups):
            membership[id(item)] = confirmed
    return membership


def _organism_rows(items):
    """Merge projections of one physical observation within a display group."""
    rows = []
    measurements = defaultdict(list)
    for item in items:
        row = _row(item)
        if not row:
            continue
        identity = (
            *_taxon_measurement(item, row),
            _source_key(row['status']),
            row.get('detectionStatus'),
        )
        anchors = _observation_anchors(row)
        duplicate = next(
            ((current, locations) for current, locations in measurements[identity] if anchors & locations), None
        )
        if duplicate is None:
            if row in rows:
                continue
            measurements[identity].append((row, anchors))
            rows.append(row)
            continue
        matching, locations = duplicate
        locations.update(anchors)
        matching['sources'].extend(ref for ref in row['sources'] if ref not in matching['sources'])
        if not matching['interpretation']:
            matching['interpretation'] = row['interpretation']
            matching['evidenceRefs'] = row['evidenceRefs']
        elif matching['interpretation'] == row['interpretation']:
            matching['evidenceRefs'] = list(dict.fromkeys([*matching['evidenceRefs'], *row['evidenceRefs']]))
    return rows


def _amr_rows(data):
    rows = data.microbiome_assessment.antimicrobial_resistance
    aggregate_references = {
        (ref.record_id, ref.observed_at)
        for row in rows
        if row.code == 'otherResistanceCategories'
        for ref in row.sources
    }
    if not aggregate_references:
        return rows
    originals = [
        row
        for row in data.appendices.source_items
        if row.kind == 'template_original_antimicrobial_resistance'
        and row.source_status in _NORMAL_STATES
        and _present(row.value)
        and any((ref.record_id, ref.observed_at) in aggregate_references for ref in row.sources)
    ]
    references = {(ref.record_id, ref.observed_at) for row in originals for ref in row.sources}
    if not aggregate_references <= references:
        return rows
    return [*(row for row in rows if row.code != 'otherResistanceCategories'), *originals]


def _gene_category_rows(data):
    """Publish literal detected targets separately from the laboratory's panel label."""
    from .evaluation_template_layout import _literal_gene_symbols

    positive, negative = [], []
    for item in data.appendices.source_items:
        if item.category != 'virulence' or item.source_type == 'template_layout_original':
            continue
        row = _row(item)
        if not row or not any(source['sourceCode'] == 'C' for source in row['sources']):
            continue
        states = {
            _GENE_DETECTION[_source_key(value)]
            for value in (row['value'], row['status'])
            if _source_key(value) in _GENE_DETECTION
        }
        if len(states) != 1:
            continue
        detection = states.pop()
        detected = _literal_gene_symbols(item.description, '检出基因')
        candidates = _literal_gene_symbols(item.description, '检测面板候选基因')
        row['_sourceSemantics'] = {
            'kind': 'gene_sequence_target',
            'measurementType': 'sequence_detection',
            'objectScope': 'target',
            'reportedObject': row['name'],
            'targetName': row['name'],
            'targetIdentitySource': 'source_reported_label',
            'originalValue': row['value'],
            'originalStatus': row['status'],
            'detectionStatus': detection,
            'stateMeaning': 'sequence_detected' if detection == 'detected' else 'sequence_not_detected',
            'detectedGenes': detected,
            'candidateGenes': candidates,
        }
        row['detectionStatus'] = detection
        if detection == 'detected':
            if detected:
                row['name'] = '、'.join(detected) + '（基因序列）'
                row['status'] = '检出序列信号'
            else:
                row['name'] += '（序列面板）'
                row['status'] = '面板检出'
        else:
            row['name'] += '（序列面板）'
            row['status'] = '未检出'
        row['value'] = row['status']
        row['_sourceSemantics']['displayStatus'] = row['status']
        target = positive if detection == 'detected' else negative
        if row not in target:
            target.append(row)
    return positive, negative


def _source_occurrences(parsed):
    """Use only occurrences belonging to the selected microbiome snapshot."""
    return {
        index: occurrence
        for index, occurrence in enumerate(parsed.source_taxon_occurrences)
        if (occurrence.get('sourceChecksum'), occurrence.get('sourceFile'))
        == (parsed.source_checksum, parsed.source_name)
    }


def _occurrence_identity(occurrence):
    return _source_key(occurrence.get('latinName') or occurrence.get('chineseName') or '')


def _occurrence_measurement(occurrence):
    return (
        _occurrence_identity(occurrence),
        _number_key(occurrence.get('value')),
        _source_key(occurrence.get('unit')),
        _reference_key(occurrence.get('referenceRange')),
        _source_key(occurrence.get('sourceStatus')),
    )


def _row_occurrences(row, occurrences):
    return [
        occurrences[int(match[1])]
        for source in row['sources']
        if (match := _TAXON_OCCURRENCE.fullmatch(source.get('recordId') or '')) and int(match[1]) in occurrences
    ]


def _raw_group_members(parsed, data):
    """Recover every literal panel member before choosing display examples.

    Canonical taxa can merge repeated panels or retain only selected normal
    representatives. Original occurrences keep each panel's own reference,
    grade and scope, including deviations unique to an association panel.
    """
    from .assessment_reviewed_fields import has_reviewed_interpretation
    from .assessment_reviewed_fields import transfer_reviewed_interpretation

    occurrences = _source_occurrences(parsed)
    explicit = defaultdict(set)
    for occurrence in occurrences.values():
        panel = _occurrence_path(occurrence).partition('[')[0]
        if (group := _SOURCE_PANEL_GROUPS.get(panel)) and group != 'composition':
            explicit[_occurrence_measurement(occurrence)].add(group)
    candidates = [*data.microbiome_assessment.taxa, *data.appendices.source_items]
    reviewed = defaultdict(list)
    for item in candidates:
        if row := _row(item):
            for source in row['sources']:
                if _TAXON_OCCURRENCE.fullmatch(source.get('recordId') or ''):
                    reviewed[source['recordId']].append(row)
    groups = defaultdict(list)
    panels = dict.fromkeys(_occurrence_path(item).partition('[')[0] for item in occurrences.values())
    for panel in panels:
        if panel in _CHILD_STRUCTURE_PANELS.values():
            continue
        for row in _child_panel_members(parsed, panel):
            members = _row_occurrences(row, occurrences)
            if len(members) != 1:
                continue
            occurrence = members[0]
            owned = _SOURCE_PANEL_GROUPS.get(panel)
            scopes = {owned} if owned else set()
            if owned == 'composition':
                scopes = explicit[_occurrence_measurement(occurrence)] or scopes
            if not owned and panel.startswith('jkj_') and _source_is_abnormal(row['status']):
                scopes = explicit[_occurrence_measurement(occurrence)] or {'composition'}
            if not scopes:
                continue
            locator = row['sources'][0]['recordId']
            matches = [
                item
                for item in reviewed[locator]
                if _number_key(item['value']) == _number_key(row['value'])
                and _source_key(item['unit']) == _source_key(row['unit'])
                and _reference_key(item['referenceRange']) == _reference_key(row['referenceRange'])
                and _source_key(item['status']) == _source_key(row['status'])
            ]
            interpreted = next((item for item in matches if has_reviewed_interpretation(item)), None)
            if interpreted:
                transfer_reviewed_interpretation(interpreted, row, occurrence_id=locator)
            for code in scopes:
                groups[code].append(row)
    return groups, occurrences


def _organism_assay_roles(row, occurrences):
    return frozenset(
        assay_identity(path)['type']
        for item in _row_occurrences(row, occurrences)
        if (path := _occurrence_path(item))
    )


def _merge_observation_rows(rows, occurrences, *, separate_assay_roles=False):
    """Coalesce repeated observations, keeping every contributing source path."""
    result, indexed = [], defaultdict(list)
    for original in rows:
        row = {**original, 'sources': list(original['sources']), 'evidenceRefs': list(original['evidenceRefs'])}
        members = _row_occurrences(row, occurrences)
        names = {_occurrence_identity(item) for item in members}
        identity = (
            next(iter(names)) if len(names) == 1 else _source_key(row['name']),
            _number_key(row['value']),
            _source_key(row['unit']),
            _reference_key(row['referenceRange']),
            _source_key(row['status']),
            row['observedAt'],
            _organism_assay_roles(row, occurrences) if separate_assay_roles else None,
        )
        anchors = _observation_anchors(row)
        existing = next((item for item in indexed[identity] if anchors & _observation_anchors(item)), None)
        if existing is None:
            result.append(row)
            indexed[identity].append(row)
            continue
        existing['sources'].extend(source for source in row['sources'] if source not in existing['sources'])
        if not existing['interpretation'] and row['interpretation']:
            existing['interpretation'] = row['interpretation']
            existing['evidenceRefs'] = row['evidenceRefs']
    return result


def _source_panel_counts(rows, occurrences, reported, code):
    """Retain visible member counts separately from supplier aggregate counts."""
    panels = defaultdict(dict)
    for row in rows:
        for member in _row_occurrences(row, occurrences):
            path = _occurrence_path(member)
            panels[path.partition('[')[0]][path] = member
    counts = []
    for panel, members in panels.items():
        source_members = [item for item in occurrences.values() if _occurrence_path(item).partition('[')[0] == panel]
        statuses = [member.get('sourceStatus') for member in source_members]
        normal = sum(status in _NORMAL_STATES for status in statuses)
        abnormal = sum(_source_is_abnormal(status) for status in statuses)
        counts.append(
            {
                'sourcePanel': panel,
                'visibleCount': len(source_members),
                'includedCount': len(members),
                'normalCount': normal,
                'abnormalCount': abnormal,
                'unclassifiedCount': len(source_members) - normal - abnormal,
            }
        )
    sections = {
        member.get('sourceSection')
        for members in panels.values()
        for member in members.values()
        if member.get('sourceSection')
    }
    count_groups = {**_TAXON_GROUPS, 'virus': 'otherOrganisms', 'parasites': 'otherOrganisms'}
    supplier = [
        item.model_dump(mode='json', by_alias=True)
        for item in reported
        if count_groups.get(item.category, item.category) == code or item.panel_name in sections
    ]
    return {'visiblePanels': counts, 'reportedCounts': supplier}


def _normal_representatives(group, occurrences, context):
    """Order full baselines by relevance and assay coverage, without truncation."""
    baseline = group['normalBaseline']
    rows = list(baseline['rows'])
    memberships = defaultdict(set)
    for occurrence in occurrences.values():
        memberships[_occurrence_identity(occurrence)].add(_occurrence_path(occurrence).partition('[')[0])

    def score(row):
        members = _row_occurrences(row, occurrences)
        related = set().union(*(memberships[_occurrence_identity(item)] for item in members)) if members else set()
        functional = {
            panel
            for panel in related
            if _SOURCE_PANEL_GROUPS.get(panel) not in {None, 'composition'}
            or panel in {'jun_pzss', 'jun_children_pzss'}
        }
        value, band = _number_key(row['value']), _reference_key(row['referenceRange'])
        upper_fraction = (
            (value - band[0]) / (band[1] - band[0])
            if isinstance(value, Decimal) and isinstance(band, tuple) and band[0] <= value <= band[1]
            and band[0] < band[1]
            else Decimal('-1')
        )
        named_context = len(_source_key(row['name'])) >= 4 and _source_key(row['name']) in context
        return named_context, upper_fraction, len(functional), bool(row['evidenceRefs']), value is not None

    ranked = sorted(rows, key=score, reverse=True)
    ordered, covered = [], set()
    # If the source combines fungal, viral or parasite assays, each real
    # assay gets a representative before another assay fills the list.
    for row in ranked:
        panels = {
            _occurrence_path(item).partition('[')[0]
            for item in _row_occurrences(row, occurrences)
            if _SOURCE_PANEL_GROUPS.get(_occurrence_path(item).partition('[')[0]) == group['code']
        }
        if panels - covered:
            ordered.append(row)
            covered.update(panels)
    ordered.extend(row for row in ranked if row not in ordered)
    baseline['rows'] = ordered
    baseline['names'] = list(dict.fromkeys(row['name'] for row in ordered))


def _organism_groups(parsed, data):
    microbiome = data.microbiome_assessment
    panels = [row for row in microbiome.health_metrics if row.code in _PANEL_GROUPS]
    # The existing layout can hide a literal negative panel from its legacy
    # view. The original archive restores that measured panel state only.
    panels.extend(
        row
        for row in data.appendices.source_items
        if row.kind == 'template_original_health_metric'
        and row.code in _PANEL_GROUPS
        and row.source_status in _NORMAL_PANEL_STATES
        and not any(
            row.code == current.code and row.value == current.value and row.source_status == current.source_status
            for current in panels
        )
    )
    membership = _taxon_membership(microbiome.taxa, parsed.source_taxon_occurrences)
    original_groups, occurrences = _raw_group_members(parsed, data)
    source_panels = {row.code for row in parsed.evaluation_panel_summaries}
    context = _source_key(
        ' '.join(
            str(item.name or '') + ' ' + str(getattr(item, 'details', None) or '')
            for item in [
                *panels,
                *(item for item in data.appendices.source_items if item.category == 'virulence'),
            ]
        )
    )
    result = []
    for code, name in _ORGANISM_GROUPS:
        group_panels = [row for row in panels if _PANEL_GROUPS[row.code] == code]
        items = [item for item in microbiome.taxa if code in membership[id(item)]]
        amr_items = _amr_rows(data) if code == 'genes' else []
        items.extend(amr_items)
        rows = _rows(group_panels)
        for row in rows:
            _assay_panel_row(row, source_panels)
        # The grouped cell has its own review scope. A compatibility panel's
        # prose is not a fallback for a wider group of measurements.
        for row in rows:
            row['interpretation'] = None
            row['evidenceRefs'] = []
        separate_assay_roles = code == 'otherOrganisms'
        original_rows = original_groups[code]
        compatibility_rows = _organism_rows(items)
        if separate_assay_roles:
            original_anchors = {
                record_id for row in original_rows for record_id in _observation_anchors(row)
                if _TAXON_OCCURRENCE.fullmatch(record_id)
            }
            compatibility_rows = [
                row for row in compatibility_rows
                if len(_organism_assay_roles(row, occurrences)) <= 1
                or not {record_id for record_id in _observation_anchors(row)
                        if _TAXON_OCCURRENCE.fullmatch(record_id)} <= original_anchors
            ]
        rows.extend(_merge_observation_rows(
            [*original_rows, *compatibility_rows], occurrences, separate_assay_roles=separate_assay_roles,
        ))
        amr_sources = {(ref.record_id, ref.observed_at) for item in amr_items for ref in item.sources}
        for row in rows:
            amr_measurement = any(
                (source['recordId'], source['observedAt']) in amr_sources for source in row['sources']
            )
            taxon_abundance = code in {'bacteria', 'otherOrganisms'} and (
                _source_key(row['unit']) == '%' or _source_key(row['value']).endswith('%')
            )
            if row['code'] in _PANEL_GROUPS or not (amr_measurement or taxon_abundance):
                continue
            row['_sourceSemantics'] = _abundance_semantics(row, occurrences)
        if separate_assay_roles:
            for row in rows:
                roles = _organism_assay_roles(row, occurrences)
                if roles == {'composition_chart'}:
                    row['name'] += '（构成图谱）'
                elif roles == {'relative_abundance_panel'} or row['code'] in _ABUNDANCE_PANEL_SOURCES:
                    row['name'] += '（靶标面板）'
        detected, undetected = _gene_category_rows(data) if code == 'genes' else ([], [])
        rows.extend(detected)
        if rows or undetected:
            group = {'code': code, 'name': name, 'rows': rows, 'summary': None}
            group['_sourcePanelCounts'] = _source_panel_counts(rows, occurrences, parsed.panel_counts, code)
            group = _with_normal_baseline(group)
            _normal_representatives(group, occurrences, context)
            if undetected:
                group['normalBaseline']['undetectedRows'] = undetected
            if semantics := _group_source_semantics([*rows, *undetected]):
                group['_sourceSemantics'] = semantics
            result.append(group)
    return result


def _assay_panel_row(row, source_panels):
    code = row['code']
    original_name = row['name']
    if code == 'virulenceGenes':
        kind = 'gene_sequence_panel'
        row['name'] = '毒力相关基因序列'
        display = {'检出': '检出序列信号'}
    elif _ABUNDANCE_PANEL_SOURCES.get(code) in source_panels:
        kind = 'relative_abundance_panel'
        display = {'未检出': '未检出异常', '检出': '检出丰度异常'}
    else:
        return
    row['_sourceSemantics'] = {
        'kind': kind,
        'measurementType': 'sequence_detection' if kind == 'gene_sequence_panel' else 'relative_abundance',
        'objectScope': 'panel',
        'reportedObject': original_name,
        'originalName': original_name,
        'originalValue': row['value'],
        'originalStatus': row['status'],
    }
    if kind == 'relative_abundance_panel':
        # A summary above numeric abundance rows is not a sequence-negative
        # assay. Keep its literal label privately while displaying its scope.
        row.pop('detectionStatus', None)
        if code == 'antibioticResistanceGenes' and '相对丰度' not in row['name']:
            row['name'] += '（相对丰度）'
    for field in ('value', 'status'):
        if isinstance(row[field], str):
            row[field] = display.get(row[field], row[field])
    semantics = row['_sourceSemantics']
    semantics['displayStatus'] = row['status'] or row['value']
    if kind == 'relative_abundance_panel':
        semantics['stateMeaning'] = (
            'absence_of_abundance_abnormality'
            if semantics['displayStatus'] in _NORMAL_STATES
            or _ABSENT_ABNORMALITY.search(semantics['displayStatus'] or '')
            else 'abundance_abnormality'
            if _source_is_abnormal(semantics['displayStatus'])
            else 'reported_abundance_panel_status'
        )
    else:
        detection = _GENE_DETECTION.get(_source_key(semantics['originalStatus'])) or _GENE_DETECTION.get(
            _source_key(semantics['originalValue'])
        )
        if detection:
            semantics['detectionStatus'] = detection
            semantics['stateMeaning'] = 'sequence_detected' if detection == 'detected' else 'sequence_not_detected'


def _unconfirmed_zero_abundance(row):
    unit = _source_key(row['unit'])
    value = _source_key(row['value'])
    return (
        (unit == '%' or value.endswith('%'))
        and _number_key(value.removesuffix('%')) == 0
        and row.get('detectionStatus') != 'not_detected'
        and row['status'] not in {'未检出', '阴性'}
    )


def _zero_abundance_summary(rows):
    observations = {row['name']: row for row in rows if _unconfirmed_zero_abundance(row)}
    if not observations:
        return None
    states = {row['status'] for row in observations.values()}
    if states == {'偏低'}:
        return f'本组{len(observations)}项相对丰度偏低的菌群均记录为0%。'
    return f'本组{len(observations)}项菌群的相对丰度记录为0%。'


def _abnormal_bacterial_abundance(rows, summary=''):
    findings = []
    for row in rows:
        value = _number_key(row['value'])
        if (
            row['code'] in _PANEL_GROUPS
            or not _source_is_abnormal(row['status'])
            or _source_key(row['unit']) != '%'
            or not isinstance(value, Decimal)
            or value <= 0
            or row['name'] in summary
        ):
            continue
        finding = f'{row["name"]}（{row["value"]}%）{row["status"]}'
        if finding not in findings:
            findings.append(finding)
    return f'本次相对丰度结果中，{"、".join(findings)}。' if findings else ''


def _organism_summary(summary, group):
    """Keep authored interpretation separate from the literal panel-state cells."""
    return summary or None


def _scfa_producer_observation(group):
    """Summarize measured group directions, with the rows that support the prose."""
    rows = [*group['rows'], *(group.get('normalBaseline') or {}).get('rows', ())]
    panels = [row for row in rows if row['code'] == 'scfaProducerBacteria']
    panel = panels[0] if len(panels) == 1 and _present(panels[0]['status']) else None
    directions = {}
    for row in rows:
        if (
            row['code'] in _PANEL_GROUPS
            or _source_key(row['unit']) != '%'
            or not isinstance(_number_key(row['value']), Decimal)
            or not row['status']
        ):
            continue
        direction = (
            '偏高'
            if re.search(r'偏高|升高|超标|↑', row['status'])
            else '偏低'
            if re.search(r'偏低|降低|↓', row['status'])
            else None
        )
        if direction and _source_is_abnormal(row['status']):
            directions.setdefault(direction, row)
    parts = [f'产短链脂肪酸相关菌的组合评定为{panel["status"]}'] if panel else []
    if directions:
        parts.append(
            '组内相对丰度既有升高也有降低' if len(directions) == 2 else f'组内有相对丰度{next(iter(directions))}的菌群'
        )
    observations = ([panel] if panel else []) + list(directions.values())
    return ('，'.join(parts) + '。' if parts else None), observations


def _organism_fallback(group):
    """Use source observations alone when the group's interpretation is withheld."""
    if group['code'] == 'scfaProducers':
        return _scfa_producer_observation(group)[0]
    if group['code'] == 'bacteria':
        return _abnormal_bacterial_abundance(group['rows']) or None
    row = next(
        (
            row
            for row in group['rows']
            if row['code'] not in _PANEL_GROUPS
            and _source_is_abnormal(row['status'])
            and _source_key(row['unit']) == '%'
            and isinstance(_number_key(row['value']), Decimal)
        ),
        None,
    )
    if row is None:
        return None
    status = row['status']
    direction = (
        '偏高' if re.search(r'偏高|升高|超标|↑', status) else ('偏低' if re.search(r'偏低|降低|↓', status) else '异常')
    )
    return f'{row["name"]}的相对丰度在本次检测中呈{direction}。'


def _score_number(value):
    try:
        score = Decimal(str(value))
    except InvalidOperation:
        return None
    return score if score.is_finite() and 0 <= score <= 100 else None


def _filled_segments(item, score_codes):
    # A relative abundance, laboratory concentration or detection label does
    # not share the capability scale. Preserve the source number separately.
    unit = (item.unit or '').strip().casefold()
    confirmed_scale = (
        unit == '/100' or item.code in score_codes or bool(_HUNDRED_SCALE.search(item.reference_range or ''))
    )
    if not confirmed_scale or (unit and unit not in _SCORE_UNITS):
        return None
    score = _score_number(item.value)
    if score is None:
        return None
    return int((score / 10).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def _function_panel_states(parsed):
    """Bind the printout's panel judgement to its verified member series."""
    from .evaluation_template_input import NUTRITION_PANELS

    states = defaultdict(list)
    for item in _selected_source_rows(parsed):
        meta = item.model_extra or {}
        path = _source_path(item)
        if not path.endswith('_all_result'):
            continue
        panel = NUTRITION_PANELS.get(path.removesuffix('_all_result'))
        status = item.result_status_name
        if panel and _present(status):
            name, code = panel
            states[code].append(
                {
                    'sourceName': name,
                    'status': status,
                    'details': meta.get('sourceDescription'),
                    'sources': [_supplement_reference(item)],
                }
            )
    return states


def _function_summary(summary, states):
    """Source states remain in their own cells, never rewrite reviewed prose."""
    return summary or None


def _native_function_status(row, native, states):
    """Use the explicit member label; a group arrow does not decide a shared band."""
    from .evaluation_template_input import _directions

    if _present(native.status):
        return native.status
    directions = [entry for state in states for entry in _directions(state['details'])]
    direction = {mark for label, mark in directions if _source_key(label) == _source_key(row['name'])}
    if direction:
        return next(iter(direction)) if len(direction) == 1 else None
    bands = _source_grade_bands(native.reference_range)
    matched = _matching_source_bands(row['value'], bands)
    if len(matched) > 1:
        return None
    if len(matched) == 1:
        return matched[0]['label']
    return None


def _native_function_semantics(row, native, states):
    from .evaluation_template_input import _directions

    directions = {
        mark
        for state in states
        for label, mark in _directions(state['details'])
        if _source_key(label) == _source_key(row['name'])
    }
    reported = native.status if _present(native.status) else next(iter(directions)) if len(directions) == 1 else None
    semantics = _grade_semantics(
        row,
        reported_status=reported,
        authority=(
            'source_reported'
            if _present(native.status)
            else 'source_member_direction'
            if directions
            else 'source_bands'
        ),
        kind='graded_function_score',
    )
    semantics['reportedDirections'] = sorted(directions)
    return semantics


def _scfa_summary(rows):
    """Summarize recorded component statuses without regrading boundary values."""
    states = defaultdict(set)
    for row in rows:
        if not _present(row['value']) or not _present(row['status']):
            continue
        state = str(row['status']).strip()
        states[row['name']].add({'↑': '偏高', '↓': '偏低'}.get(state, state))
    deviations, normals = [], []
    for name, outcomes in states.items():
        if len(outcomes) != 1:
            continue
        state = next(iter(outcomes))
        if _source_is_abnormal(state) or state in {'偏强', '偏弱', '极高', '极低'}:
            deviations.append(f'{name}{state}')
        elif state in _NORMAL_STATES:
            normals.append(name)
    if not deviations:
        return None
    finding = '、'.join(deviations)
    baseline = '，' + '、'.join(normals) + '处于正常范围' if normals else ''
    return f'菌群短链脂肪酸合成潜能评分中，{finding}{baseline}。'


def _functional_groups(parsed, data):
    from .evaluation_template_input import EvaluationTemplateFunctionMetric
    from .evaluation_template_pdf import EvaluationTemplatePdfFunctionMetric

    native_metrics = {
        item.code: item
        for item in parsed.metrics
        if isinstance(item, (EvaluationTemplateFunctionMetric, EvaluationTemplatePdfFunctionMetric))
    }
    pathways = {path.code: path for path in data.functional_pathways.pathways}
    panel_states = _function_panel_states(parsed)
    microbiome = data.microbiome_assessment
    gene_rows = [
        *microbiome.antimicrobial_resistance,
        *(row for row in microbiome.health_metrics if _PANEL_GROUPS.get(row.code) == 'genes'),
        *(row for row in data.appendices.source_items if row.category in {'virulence', 'antimicrobialResistance'}),
    ]
    gene_refs = {source.record_id for row in gene_rows for source in row.sources if source.record_id}
    result = []
    for ordinal, (code, name, pathway_codes) in enumerate(_FUNCTION_GROUPS, start=1):
        blocks = [pathways[path] for path in pathway_codes if path in pathways]
        rows = []
        for block in blocks:
            panel_refs = {
                source['recordId']
                for panel in panel_states[block.code]
                for source in panel['sources']
                if source['recordId']
            }
            for item in block.related_metrics:
                if any(source.record_id in panel_refs for source in item.sources):
                    continue
                if block.code in {'P9', 'P10'}:
                    if item.code == 'pathogenicPotential' or any(
                        source.record_id in gene_refs for source in item.sources
                    ):
                        continue
                    if (
                        block.code == 'P10'
                        and item.code not in native_metrics
                        and not re.search(r'分解|降解|toxin:', f'{item.name or ""} {item.code or ""}')
                    ):
                        continue
                row = _row(item)
                if row:
                    row['filledSegments'] = _filled_segments(item, native_metrics)
                    if native := native_metrics.get(item.code):
                        row['value'] = native.value
                        row['unit'] = native.unit
                        row['referenceRange'] = native.reference_range
                        row['status'] = _native_function_status(row, native, panel_states[block.code])
                        row['_sourceSemantics'] = _native_function_semantics(row, native, panel_states[block.code])
                    if row not in rows:
                        rows.append(row)
        states = [state for path in pathway_codes for state in panel_states[path]]
        if rows or states:
            interpreted = [block for block in blocks if block.health_meaning]
            summary = '\n\n'.join(dict.fromkeys(block.health_meaning for block in interpreted))
            if not rows and not summary:
                summary = '。'.join(f'{state["sourceName"]}：{state["status"]}' for state in states) + '。'
            if rows and code in {'aminoAcid', 'vitamin'}:
                count = len({row['name'] for row in rows})
                name = f'{name} ({count}种)'
            group = {
                'code': code,
                'name': f'{ordinal}. {name}',
                'rows': rows,
                'summary': _function_summary(summary, states) if len(pathway_codes) == 1 else summary or None,
                'panelStates': states,
                'evidenceRefs': list(dict.fromkeys(ref for block in interpreted for ref in block.evidence_refs)),
                '_cellDuty': function_cell_duties()[f'function.{code}'],
            }
            group = _with_normal_baseline(group)
            if semantics := _group_source_semantics(rows):
                group['_sourceSemantics'] = semantics
            if references := _reference_display_rows(rows):
                group['_referenceDisplay'] = references
            if not group['normalBaseline']['rows']:
                group['normalBaseline']['state'] = (
                    'all_abnormal'
                    if rows and all(_source_is_abnormal(row['status']) and _present(row['value']) for row in rows)
                    else 'no_confirmed_normal'
                )
            result.append(group)
    return result


def build_microbiome_view(parsed, data, *, source_facts=()) -> dict:
    """Reuse validated factual rows and authored assessments; invent no missing result."""
    microbiome = data.microbiome_assessment
    rows = _structure_rows(microbiome)
    view = {
        'sampleContext': _sample_context(parsed),
        'structure': rows,
        'structureGroups': _structure_groups(rows, parsed, microbiome),
        'structureSummary': microbiome.structure_interpretation.text,
        'structureSummaryEvidenceRefs': list(microbiome.structure_interpretation.evidence_refs),
        'organismGroups': _organism_groups(parsed, data),
        'functionalGroups': _functional_groups(parsed, data),
    }
    _annotate_source_assays(view, parsed, source_facts, data=data)
    view['_functionCellDuties'] = function_cell_duties()
    view['_sourceSemanticsByFactId'] = source_semantics_by_fact_id(view)
    return view


def _displayed_group_rows(group):
    baseline = group.get('normalBaseline') or {}
    yield from group.get('rows') or ()
    yield from baseline.get('rows') or ()
    yield from baseline.get('undetectedRows') or ()


def annotate_microbiome_chart_fields(view: dict) -> dict:
    """Append the spec 2.7 chart fields to every displayed microbiome row, in place.

    Score rows gain ``score``/``bands``, abundance rows ``abundance``/
    ``referenceLow``/``referenceHigh``, macro indices ``valueNumber``, reference
    bounds and ``bands``; every row gains ``direction`` and ``level``. Original
    fields are neither renamed nor removed, and unparsed values stay ``None``.
    """
    from .assessment_chart_fields import BORDERLINE
    from .assessment_chart_fields import NORMAL
    from .assessment_chart_fields import microbiome_chart_fields

    for row in view.get('structure') or ():
        row.update(microbiome_chart_fields(row))
    for group in view.get('structureGroups') or ():
        rows = list(_displayed_group_rows(group))
        for row in rows:
            row.update(microbiome_chart_fields(row))
        if group.get('code') == 'overallBalance':
            grade = next(
                (row['level'] for row in rows if row.get('code') == 'balanceStatus' and row.get('level')), None,
            )
            for row in rows:
                if row.get('code') == 'microbiomeHealthScore' and row.get('level') is None:
                    row['level'] = grade
        difference = group.get('gutAgeDifference')
        if group.get('code') == 'gutAge' and isinstance(difference, (int, float)):
            for row in rows:
                if row.get('code') == 'gutAge':
                    row['actualAge'] = group.get('actualAge')
                    row['ageDifference'] = difference
                    row['direction'] = 'up' if difference > 0 else 'down' if difference < 0 else None
                    row['level'] = BORDERLINE if difference > 0 else NORMAL
    for group in [*(view.get('organismGroups') or ()), *(view.get('functionalGroups') or ())]:
        for row in _displayed_group_rows(group):
            row.update(microbiome_chart_fields(row))
    return view


def source_observation_display_status(row):
    """Keep a source direction visible alongside all matching score bands."""
    state = row.get('templateStatus') or row.get('status')
    semantics = row.get('_sourceSemantics') or {}
    if semantics.get('boundaryAmbiguous'):
        labels = list(dict.fromkeys(band['label'] for band in semantics.get('matchingBands', ())))
        if len(labels) > 1:
            boundary = '/'.join(labels) + '交界'
            return f'{state}；{boundary}' if _present(state) else boundary
    return state


def _overview_microbiome_observation(row, semantics):
    from .assessment_source_context import model_microbiome_observation

    observation = model_microbiome_observation(row, semantics)
    if observation is None or observation['measurementType'] == 'multiple_source_observations':
        return dict(row)
    displayed = {
        **row,
        'name': observation['label'],
        'value': observation['value'],
        'unit': observation['unit'],
        'status': observation['status'],
    }
    if observation['measurementType'] == 'sequence_detection' or (
        observation['measurementType'] == 'relative_abundance' and observation['objectScope'] == 'panel'
    ):
        # A legacy template label cannot replace the shared typed result.
        displayed.pop('templateStatus', None)
    return displayed


def _overview_microbiome_identity(row):
    semantics = row.get('_sourceSemantics') or {}
    return (
        semantics.get('measurementType'),
        semantics.get('objectScope'),
        _number_key(row.get('value')),
        _source_key(row.get('unit')),
        _reference_key(row.get('referenceRange')),
        _source_key(source_observation_display_status(row)),
        row.get('observedAt'),
    )


def _overview_microbiome_vector(label, entries):
    rows = [entry['row'] for entry in entries]
    sources = []
    for entry in entries:
        sources.extend(source for source in entry['sources'] if source not in sources)
    if len(rows) == 1:
        return {
            **{
                key: deepcopy(rows[0].get(key))
                for key in (
                    'name',
                    'value',
                    'unit',
                    'referenceRange',
                    'observedAt',
                )
            },
            'status': source_observation_display_status(rows[0]),
            'sources': sources,
            'evidenceRefs': [],
        }
    readings = []
    for row in rows:
        semantics = row.get('_sourceSemantics') or {}
        state = source_observation_display_status(row)
        key = (
            str(row.get('value')),
            row.get('unit'),
            state,
            semantics.get('measurementType'),
            semantics.get('objectScope'),
        )
        bucket = next((item for item in readings if item['key'] == key), None)
        if bucket is None:
            bucket = {'key': key, 'names': [], 'row': row}
            readings.append(bucket)
        if row['name'] not in bucket['names']:
            bucket['names'].append(row['name'])
    values, states = [], []
    for reading in readings:
        row = reading['row']
        name = '、'.join(reading['names'])
        value = str(row['value']) if row['value'] is not None else ''
        if row.get('unit'):
            value += ' ' + row['unit']
        state = source_observation_display_status(row)
        if _present(state):
            if state not in states:
                states.append(state)
            if _source_key(state) != _source_key(row['value']):
                value += f'（{state}）'
        values.append(f'{name}：{value}')
    return {
        'name': label,
        'value': '；'.join(values),
        'unit': None,
        'referenceRange': None,
        'status': '；'.join(states) or None,
        'observedAt': rows[0].get('observedAt'),
        'sources': sources,
        'evidenceRefs': [],
    }


def select_overview_microbiome_anchors(view, identifiers, facts, *, composition_scope=()):
    """Group actually used, owned observations without truncating source values.

    Display groups supply editorial domains, not permission to include their
    other members. Supporting references cannot grant membership. Repeated
    panel/member observations stay parallel in one vector; their contribution
    to the panel classification is not inferred here.
    """
    selected = set(identifiers)
    composition_rows = [
        row
        for group in view.get('structureGroups', ()) if group['code'] == 'composition'
        for row in group.get('rows', ()) if row.get('code') in {'fbRatio', 'enterotype'}
    ]
    permitted = set(composition_scope)
    for row in composition_rows:
        selected.update(permitted.intersection(row.get('_semanticOwnerRecordIds', ())))
    source_facts = {
        fid: fact if isinstance(fact, dict) else fact.model_dump(mode='json', by_alias=True)
        for fid, fact in facts.items()
        if fid in selected
    }
    selected = {
        fid
        for fid in selected
        if fid in source_facts and source_facts[fid].get('kind') in {'microbe', 'function', 'context'}
    }
    structure_groups = view.get('structureGroups', ())
    groups = [*structure_groups, *view.get('organismGroups', ()), *view.get('functionalGroups', ())]
    candidates = []
    for position, group in enumerate(groups):
        entries = []
        composition = position < len(structure_groups) and group['code'] == 'composition'
        for row in _microbiome_rows(group):
            owners = selected.intersection(row.get('_semanticOwnerRecordIds', ()))
            if not owners or not _present(row.get('value')):
                continue
            sources = [deepcopy(source) for source in row.get('sources', ()) if source.get('recordId') in owners]
            semantics = row.get('_sourceSemantics') or {}
            snapshots = tuple(
                sorted(
                    {
                        assay.get('sourceChecksum')
                        for assay in semantics.get('assays', ())
                        if assay.get('sourceChecksum')
                    }
                )
            )
            acquisition = (row.get('observedAt'), snapshots)
            # Unknown acquisition identity cannot merge independent readings.
            if not snapshots:
                acquisition += (tuple(sorted(owners)),)
            displayed = _overview_microbiome_observation(row, [semantics])
            label = group['name']
            if composition:
                label = 'F/B 比值与肠型' if row.get('code') in {'fbRatio', 'enterotype'} else '菌门相对丰度'
            entries.append({'row': displayed, 'owners': owners, 'sources': sources,
                            'acquisition': acquisition, 'group': position, 'label': label})
        # Prefer the real domain with the most actually selected members when
        # one identical observation occurs in several source-defined groups.
        panel = any((entry['row'].get('_sourceSemantics') or {}).get('objectScope') == 'panel' for entry in entries)
        member_count = sum(
            (entry['row'].get('_sourceSemantics') or {}).get('objectScope') != 'panel' for entry in entries
        )
        for entry in entries:
            entry['priority'] = (panel, member_count)
            candidates.append(entry)
    observations = []
    for entry in candidates:
        match = next(
            (
                prior
                for prior in observations
                if prior['owners'] & entry['owners']
                and _overview_microbiome_identity(prior['row']) == _overview_microbiome_identity(entry['row'])
            ),
            None,
        )
        if match is None:
            observations.append({**entry, 'owners': set(entry['owners']), 'sources': list(entry['sources'])})
            continue
        match['owners'].update(entry['owners'])
        match['sources'].extend(source for source in entry['sources'] if source not in match['sources'])
        if entry['priority'] > match['priority']:
            for key in ('row', 'group', 'acquisition', 'priority', 'label'):
                match[key] = entry[key]
    buckets = defaultdict(list)
    covered = set()
    for entry in observations:
        buckets[(entry['group'], entry['label'], entry['acquisition'])].append(entry)
        covered.update(entry['owners'])
    result = [
        _overview_microbiome_vector(label, entries)
        for (_, label, _), entries in sorted(buckets.items(), key=lambda item: item[0][0])
    ]
    # A used fact lacking sufficient row ownership remains a literal single
    # anchor. Do not expand a panel or silently discard it to reduce length.
    for fid, fact in source_facts.items():
        if fid not in selected or fid in covered:
            continue
        displayed = _overview_microbiome_observation(fact, view.get('_sourceSemanticsByFactId', {}).get(fid, []))
        result.append(
            {
                'name': displayed.get('name'),
                'value': deepcopy(displayed.get('value')),
                'unit': displayed.get('unit'),
                'referenceRange': fact.get('referenceRange', fact.get('reference_range')),
                'status': displayed.get('status'),
                'observedAt': fact.get('date'),
                'sources': [{'sourceCode': 'C', 'recordId': fid, 'observedAt': fact.get('date')}],
                'evidenceRefs': [],
            }
        )
    return result
