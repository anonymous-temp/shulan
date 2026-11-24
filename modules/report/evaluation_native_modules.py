"""Literal source modules on existing evaluate arrays, without example slots.

The three overview panels project the adopted report's actual modules. Native
health-association findings are a distinct lane from disease model indices and
medical diagnoses. Missing assays never become negative findings.
"""
from __future__ import annotations

import re
import unicodedata

from . import evaluation_schema as public
from . import evaluation_catalog as catalog
from .evaluation_disease_identity import same_source_disease
from .evaluation_template_input import HEALTH_ASSOCIATION_PANELS, NUTRITION_PANELS


ASSOCIATION_PREFIX = 'nativeAssociation:'
NUTRITION_PREFIX = 'nativeNutrition:'

# Exact concept equivalences only. Generic IBD/IBS/liver disease are not their
# individual diagnoses/subtypes. A negative group screen cannot exclude them.
ASSOCIATION_DISEASE_CODES = {
    'jkj_IBD': ('inflammatoryBowelDisease',),
    'jkj_IBS': ('irritableBowelSyndrome',),
    'jkj_constipation': ('constipation',),
    'jkj_fat': ('obesity',),
    'jkj_T2D': ('type2Diabetes',),
    'jkj_yiyu': ('depressiveTendency',),
    'jkj_jingfen': ('schizophrenia',),
    'jkj_AD': ('alzheimersDisease',),
    'jkj_PD': ('parkinsonsDisease',),
    'jkj_children_ASD': ('autismSpectrumDisorder',),
    'jkj_children_yiyu': ('depressiveTendency',),
    'jkj_children_FC': ('constipation',),
    'jkj_children_obesity': ('obesity',),
}

ASSOCIATION_ALIASES = {
    'jkj_ganran': ('胃肠道感染', '胃肠道感染相关菌群'),
    'jkj_IBD': ('炎症性肠病', '炎症性肠病IBD', 'IBD'),
    'jkj_IBS': ('肠易激综合征', '肠易激综合征IBS', 'IBS'),
    'jkj_futong': ('腹痛',), 'jkj_chronic_diarrhea': ('腹泻', '慢性腹泻'),
    'jkj_fuzhangpaiqi': ('腹胀排气',), 'jkj_constipation': ('便秘', '功能性便秘'),
    'jkj_HF': ('心血管相关', '心血管疾病'), 'jkj_fat': ('肥胖', '肥胖症'),
    'jkj_T2D': ('Ⅱ型糖尿病', 'II型糖尿病', '2型糖尿病', '二型糖尿病'),
    'jkj_allergy': ('过敏', '过敏症'), 'jkj_osteoporosis': ('骨质疏松', '骨质疏松症'),
    'jkj_yiyu': ('情绪障碍', '抑郁症', '抑郁倾向'), 'jkj_jingfen': ('精神分裂症',),
    'jkj_shuimianhuxizanting': ('睡眠呼吸暂停', '睡眠呼吸暂停综合征'),
    'jkj_aud': ('酒精使用障碍',), 'jkj_AD': ('阿尔兹海默症', '阿尔茨海默病', '阿尔茨海默症'),
    'jkj_PD': ('帕金森综合征', '帕金森病'), 'jkj_hepatopathy': ('肝病',), 'jkj_weizhang': ('胃胀',),
}


def _get(row, name, default=None):
    if isinstance(row, dict):
        head, *tail = name.split('_')
        camel = head + ''.join(token.title() for token in tail)
        return row.get(name, row.get(camel, default))
    return getattr(row, name, default)


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _sources(row):
    return [source.model_copy(deep=True) if isinstance(source, public.SourceReference)
        else public.SourceReference.model_validate(source) for source in (_get(row, 'sources', []) or [])]


def _source_c(row):
    sources = _sources(row)
    return bool(sources) and all(source.source_code == 'C' for source in sources)


def native_association_result(row):
    """Project a source health-panel state, never a detected disease.

    These provider tables summarise abnormal *associated organisms*.  Their
    sourceStatus is retained verbatim.  The public result adds that missing
    object, without inferring a disease, numeric risk, or non-detection of all
    organisms.  A field definition or member abundance cannot supply a state.
    """
    code = str(_get(row, 'code') or '')
    if not code.startswith(ASSOCIATION_PREFIX) or code[len(ASSOCIATION_PREFIX):] not in HEALTH_ASSOCIATION_PANELS:
        return None
    if not _source_c(row) or not any(source.record_id for source in _sources(row)):
        return None
    status = _get(row, 'source_status')
    original = status if status is not None else _get(row, 'value')
    if not isinstance(original, str):
        return None
    original = original.strip().casefold()
    if original in {'检出', '阳性', '异常', '检出相关菌群异常', 'detected', 'positive', 'abnormal'}:
        return '检出相关菌群异常'
    if original in {'未检出', '阴性', '正常', '未见相关菌群异常', 'not_detected', 'not detected', 'negative', 'normal'}:
        return '未见相关菌群异常'
    return None


def native_association_source_row(row):
    """A raw-state candidate for existing exact fact binding, never a proof.

    Working rows already carry the complete literal source value and are
    copied unchanged. Only a previously published display label can restore
    its retained original sourceStatus. The caller must still bind every original name/value/unit/status/date and
    source locator through the normal fact validator. This does not approve
    a paragraph or infer a source state from a displayed label.
    """
    label = native_association_result(row)
    status = _get(row, 'source_status')
    value = _get(row, 'value')
    if label is None:
        return None
    published_label = value in {'检出相关菌群异常', '未见相关菌群异常'}
    if published_label and (value != label or not isinstance(status, str)):
        return None
    if isinstance(row, dict):
        from copy import deepcopy
        result = deepcopy(row)
    else:
        result = row.model_dump(mode='json', by_alias=True)
    if published_label:
        result['value'] = status
    return result


def native_association_positive(row):
    """A positive abnormal-associated-organism panel is not disease presence."""
    return native_association_result(row) == '检出相关菌群异常'


def _association_inventory_same_source(data, association, model):
    """Recover the original parsed-model link through exact source inventory.

    Some legacy JSON projections have a model's parsed locator but only the
    assay's JSON checksum locator. Their original inventory must prove the
    numeric value/grade, actual locator and one unambiguous C document.
    """
    if data is None:
        return False
    inventory = _get(_get(data, 'appendices', {}), 'source_items', [])
    def ids(row):
        return {source.record_id for source in _sources(row) if source.source_code == 'C' and source.record_id}
    def compatible_dates(first, second):
        a = {_get(first, 'observed_at')} - {None, ''}
        b = {_get(second, 'observed_at')} - {None, ''}
        return not a or not b or bool(a.intersection(b))
    model_ids = {record for record in ids(model)
        if re.match(r'^parsed\.(?:disease_results|diseaseResults)\[\d+\]$', record)}
    if not model_ids:
        return False
    model_rows = [row for row in inventory if _get(row, 'kind') == 'disease'
        and _source_c(row) and model_ids.intersection(ids(row))
        and same_source_disease(_get(row, 'name'), _get(model, 'name'))
        and all(_get(row, name) == _get(model, name) for name in ('value', 'unit', 'source_status'))]
    panel_rows = [row for row in inventory if _get(row, 'kind') == 'context'
        and _get(row, 'category') == 'healthAssociation' and _source_c(row)
        and ids(row).intersection(ids(association))
        and same_source_disease(_get(row, 'name'), _get(association, 'name'))
        and all(_get(row, name) == _get(association, name) for name in ('value', 'unit', 'source_status'))]
    for original in model_rows:
        for panel in panel_rows:
            filename = _get(original, 'source_name')
            if (not filename or filename != _get(panel, 'source_name')
                    or not compatible_dates(original, panel)):
                continue
            panel_roots = {match[1] for record in ids(panel)
                if (match := re.fullmatch(r'source:([^:]+):(?:json:|page:\d+:template:)'
                    r'jkj_[A-Za-z0-9_]+_all_result', record))}
            if len(panel_roots) != 1:
                continue
            document_roots = {match[1] for row in inventory
                if _get(row, 'source_name') == filename and compatible_dates(row, panel)
                for record in ids(row) if (match := re.match(r'^source:([^:]+):', record))}
            if document_roots == panel_roots:
                return True
    return False


def _association_model_same_source(association, model, data=None):
    """Match one adopted C report, without borrowing another report's index."""
    native_refs, model_refs = _sources(association), _sources(model)
    native_dates = {source.observed_at for source in native_refs if source.observed_at}
    model_dates = {source.observed_at for source in model_refs if source.observed_at}
    if native_dates and model_dates and not native_dates.intersection(model_dates):
        return False

    def roots(refs, *, model_origin=False):
        result, scored = [], []
        for source in refs:
            locator = str(source.record_id or '')
            match = re.match(r'^source:([^:]+):', locator)
            if not match:
                continue
            if re.search(r'(?:json:|template:)disease_[A-Za-z0-9_]+_(?:score|label)$', locator):
                scored.append(match[1])
            # Attached association/organism evidence does not establish the
            # report owning a numeric disease-model observation.
            if model_origin and re.search(r'(?:json:|template:)(?:jkj_|jun_|yingyang)', locator):
                continue
            result.append(match[1])
        return set(scored) if scored else set(result[:1]) if model_origin else set(result)

    native_roots, model_roots = roots(native_refs), roots(model_refs, model_origin=True)
    if native_roots and model_roots:
        return bool(native_roots.intersection(model_roots))
    # Legacy projections retain the one adopted parsed-C namespace instead
    # of a field checksum. Require the actual model locator, not a generic
    # C label or an attached source association pretending to be its index.
    parsed_link = (any(re.match(r'^parsed\.(?:disease_results|diseaseResults)\[', str(source.record_id or ''))
            for source in model_refs)
        and any(str(source.record_id or '').startswith('parsed.') for source in native_refs))
    return parsed_link or _association_inventory_same_source(data, association, model)


def native_association_risk_assessment(data, row):
    """A positive source panel has attention; only its own model has an index.

    This returns presentation metadata, leaving all working observations
    unchanged. A matching original model wins even when its grade is low.
    An association without that original model has no invented number.
    """
    if not native_association_positive(row):
        return None
    from .evaluation_result_rules import has_risk_index
    section = _get(data, 'disease_risk_assessment', {})
    candidates = [*_get(section, 'risk_ranking', [])]
    candidates.extend(item for group in _get(section, 'categories', []) for item in _get(group, 'items', []))
    models = [model for model in candidates
        if not str(_get(model, 'code') or '').startswith(ASSOCIATION_PREFIX)
        and has_risk_index(model) and _source_c(model)
        and same_source_disease(_get(row, 'name'), _get(model, 'name'))
        and _association_model_same_source(row, model, data)]
    # Mirrored category/ranking rows are one original observation. If genuinely
    # different adopted model results remain, do not choose an arbitrary index.
    signatures = {(str(_get(model, 'value')), _get(model, 'unit'), _get(model, 'source_status')) for model in models}
    model = models[0] if len(signatures) == 1 else None
    sources = _sources(row)
    if model is not None:
        sources.extend(source for source in _sources(model) if source not in sources)
    grade = _get(model, 'source_status') if model is not None else None
    risk_levels = {'正常', '注意', '低风险', '中低风险', '中风险', '中高风险', '高风险', '极高风险'}
    return {'value': _get(model, 'value') if model is not None else None,
        'assessmentLevel': grade if grade in risk_levels else '注意',
        'sources': [source.model_dump(mode='json', by_alias=True) for source in sources],
        'sourceModelCode': _get(model, 'code') if model is not None else None,
        'origin': 'source_model' if model is not None else 'qualitative_association'}


def _literal(row):
    return _get(row, 'value') is not None or _get(row, 'source_status') is not None


def _native_key(row, definitions):
    for source in _sources(row):
        locator = str(source.record_id or '')
        for key in definitions:
            if re.search(r'(?:json:|template:)' + re.escape(key) + r'_all_result(?:$|[^A-Za-z0-9_])', locator):
                return key
    # A verified native PDF source can have only page locators; its literal
    # table heading still identifies a known source module exactly.
    name = _key(_get(row, 'name'))
    if definitions is HEALTH_ASSOCIATION_PANELS:
        return next((key for key, title in definitions.items()
            if name in {_key(alias) for alias in (title, *ASSOCIATION_ALIASES.get(key, ()))}), None)
    def native_titles(definition):
        title = definition[0] if isinstance(definition, tuple) else definition
        # The two original overview headings say 代谢 while their chapter
        # headings/field dictionary say 能力; neither changes the measured group.
        return {_key(title), _key(title.replace('合成能力', '合成代谢').replace('分解能力', '分解代谢'))}
    return next((key for key, definition in definitions.items() if name in native_titles(definition)), None)


def _signal_direction(value):
    value = str(value or '')
    return 'up' if re.search(r'偏高|升高|↑', value) else 'down' if re.search(r'偏低|降低|缺乏|↓', value) else None


def _association_signals(row, rows):
    """Use this summary's explicit abnormal list and matching source taxa."""
    result = []
    pairs = []
    for original in (_get(row, 'description'), _get(row, 'value')):
        for clause in re.split(r'[,，、;；\n]+', str(original or '')):
            clause = re.sub(r'^\s*(?:相关菌(?:群)?|异常指标|异常菌(?:群)?|相关微生物)\s*[：:]\s*', '', clause)
            if match := re.fullmatch(r'\s*(.+?)\s*([↑↓])\s*', clause):
                pairs.append((match[1].strip(), 'up' if match[2] == '↑' else 'down'))
        if pairs:
            # A declared native abnormal list is authoritative. The legacy
            # value is read only when no such list has survived projection.
            break
    if not pairs:
        pairs = [(name, None) for name in (_get(row, 'related_taxa', []) or [])]
    for name, direction in pairs:
        matches = [taxon for taxon in rows if _get(taxon, 'kind') in {'taxon', 'microbe'}
            and (_get(taxon, 'source_name'), _get(taxon, 'observed_at')) ==
                (_get(row, 'source_name'), _get(row, 'observed_at'))
            and _key(name) in {_key(_get(taxon, 'name')), _key(_get(taxon, 'scientific_name'))}
            and (not direction or _signal_direction(_get(taxon, 'source_status')) == direction)]
        if not direction and len({(_get(taxon, 'value'), _get(taxon, 'unit'),
                _get(taxon, 'source_status')) for taxon in matches}) > 1:
            # A bare organism label cannot choose between the same source's
            # conflicting original normal/low occurrences. Retain the source
            # association label without borrowing either measured row.
            matches = []
        for taxon in matches or [None]:
            signal = public.TaxonSignal(name=name,
                scientific_name=_get(taxon, 'scientific_name') if taxon is not None else None,
                value=_get(taxon, 'value') if taxon is not None else None,
                unit=_get(taxon, 'unit') if taxon is not None else None,
                source_status=_get(taxon, 'source_status') if taxon is not None else
                    ('↑' if direction == 'up' else '↓' if direction == 'down' else None),
                sources=[*_sources(row), *(_sources(taxon) if taxon is not None else [])])
            duplicate = next((item for item in result if item.model_dump(exclude={'sources'}) ==
                signal.model_dump(exclude={'sources'})), None)
            if duplicate is None:
                result.append(signal)
            else:
                duplicate.sources.extend(source for source in signal.sources if source not in duplicate.sources)
    return result


def _native_panel_member_sources(summary, rows, panel):
    """Bind exact native array occurrences, including normal/zero members.

    A title/name match is insufficient. Both the original panel locator and
    its document checksum must match the summary's source, in the same file
    and report date. PDF mappings keep their actual page and logical path.
    """
    checksums = set()
    for source in _sources(summary):
        if match := re.fullmatch(r'source:([^:]+):(?:json:|page:\d+:template:)'
                + re.escape(panel) + r'_all_result', str(source.record_id or '')):
            checksums.add(match[1])
    if not checksums:
        return []
    result = []
    for member in rows:
        if (_get(member, 'kind') not in {'taxon', 'microbe'} or _get(member, 'value') is None
                or (_get(member, 'source_name'), _get(member, 'observed_at')) !=
                    (_get(summary, 'source_name'), _get(summary, 'observed_at'))):
            continue
        member_sources = _sources(member)
        proven = [source for source in member_sources if any(re.fullmatch(
            r'source:' + re.escape(checksum) + r':(?:json:|page:\d+:template:)'
            + re.escape(panel) + r'\[\d+\]', str(source.record_id or '')) for checksum in checksums)]
        if not proven:
            continue
        for source in [*proven, *member_sources]:
            # Other panel locators are unnecessary for this relationship.
            # Keep the original scalar/fact aliases so evidence composition
            # can resolve the measured organism rather than guessing a name.
            record = str(source.record_id or '')
            if (record.startswith('parsed.sourceTaxonOccurrences[')
                    or re.match(r'source:[^:]+:(?:json:|page:\d+:template:)', record)
                    and source not in proven):
                continue
            if source not in result:
                result.append(source.model_copy(deep=True))
    return result


def native_panel_semantics(row):
    """Internal source semantics; never add a callback notice or new field."""
    code = _get(row, 'code')
    if code not in {'barrierRelatedBacteria', 'keystoneBacteria'}:
        return {}
    return dict(sourcePanel='jun_pzss' if code == 'barrierRelatedBacteria' else 'jun_hxjs',
        resultScope='abnormal_signals',
        resultDescription='原栏目结果为异常关联信号汇总，未检出表示未检出异常关联项；不表示所有受检成员均未检出或丰度为零。',
        memberInterpretation='成员丰度与原分级逐项保留；正常且非零的成员仍为实测存在，不从汇总词生成新的菌种未检出标记。')


def _merge_same_native_results(items):
    """Fold proved mirrors from one PDF, retaining every original locator.

    A native table can supply an undated field and the shared adapter a dated
    mirror of that same file. A missing date is compatible; two explicit
    different dates, measured cells, original grades or references are not.
    """
    def scope(item):
        sources = _sources(item)
        if not sources or any(source.source_code != 'C' for source in sources):
            return None
        checksums = {match[1].lower() for source in sources if (match := re.match(
            r'^source:([0-9a-fA-F]{64}):', str(source.record_id or '')))}
        if len(checksums) != 1:
            return None
        dates = {source.observed_at for source in sources if source.observed_at}
        return next(iter(checksums)), dates

    def typed(value):
        return type(value).__name__, value

    def compatible(first, second):
        if not first.code or first.code != second.code:
            return False
        a, b = scope(first), scope(second)
        if a is None or b is None or a[0] != b[0] or a[1] and b[1] and a[1] != b[1]:
            return False
        if any(typed(getattr(first, field)) != typed(getattr(second, field)) for field in
                ('value', 'unit', 'reference_range', 'source_status', 'assessment_level')):
            return False
        return not any(getattr(first, field, None) and getattr(second, field, None)
            and getattr(first, field) != getattr(second, field) for field in
                ('explanation', 'details'))

    result = []
    for item in items:
        previous = next((row for row in result if compatible(row, item)), None)
        if previous is None:
            result.append(item)
            continue
        for source in item.sources:
            if source not in previous.sources:
                previous.sources.append(source.model_copy(deep=True))
        for field in ('evidence_refs', 'related_taxa'):
            if hasattr(previous, field) and hasattr(item, field):
                values = getattr(previous, field)
                values.extend(value for value in getattr(item, field) if value not in values)
        for field in ('explanation', 'details'):
            if hasattr(previous, field) and not getattr(previous, field) and getattr(item, field, None):
                setattr(previous, field, getattr(item, field))
        if item.availability == 'available':
            previous.availability = 'available'
    return result


def project_native_modules(data, rows=None):
    """Idempotently retain supplied module summaries on confirmed fields."""
    rows = data.appendices.source_items if rows is None else rows
    from .evaluation_disease_catalog import source_catalog_family
    family = source_catalog_family(data)
    native_family = family in {'GM', 'G1'}
    associations, nutrients, health, structure = [], [], [], []
    structural_panels = {
        'jun_hxjs': ('keystoneBacteria', '肠道基石菌构成'),
        'jun_children_hxjs': ('keystoneBacteria', '核心菌属构成'),
        'jun_pzss': ('barrierRelatedBacteria', '肠道屏障损伤相关菌评估'),
        'jun_children_pzss': ('barrierRelatedBacteria', '肠道屏障损伤相关菌评估'),
        'jun_children_fayu': ('developmentalMaturity', '肠道菌群发育成熟度相关菌群'),
        'jun_children_shengzhang': ('childGrowth', '影响儿童生长指标相关肠道菌群'),
    }
    for row in rows:
        if not _source_c(row) or not _literal(row):
            continue
        if _get(row, 'kind') == 'context' and _get(row, 'category') == 'healthAssociation':
            key = _native_key(row, HEALTH_ASSOCIATION_PANELS)
            if not key:
                continue
            item = public.DiseaseRiskItem(code=ASSOCIATION_PREFIX + key,
                name=_get(row, 'name'), value=_get(row, 'value'), source_status=_get(row, 'source_status'),
                assessment_level=None, explanation=_get(row, 'description'),
                sources=_sources(row), availability=_get(row, 'availability', 'available'),
                related_taxa=_association_signals(row, rows))
            label = native_association_result(item)
            if label is None:
                # A field dictionary/unknown flag is not a measured screen.
                # Its original context remains private; fixed empty slots do
                # not acquire a normal state or a numeric disease index.
                continue
            # Keep the full original scalar for evidence binding. The
            # callback serializer adds the explicit object only at publish.
            associations.append(item)
        elif _get(row, 'kind') == 'context' and _get(row, 'category') == 'structuralAssessment':
            key = _native_key(row, NUTRITION_PANELS)
            if key:
                nutrients.append(public.MetricAssessment(code=NUTRITION_PREFIX + key,
                    name=_get(row, 'name'), value=_get(row, 'value'), source_status=_get(row, 'source_status'),
                    assessment_level=_get(row, 'source_status'), explanation=_get(row, 'description'),
                    sources=_sources(row), availability=_get(row, 'availability', 'available')))
            else:
                panel = next((key for key in structural_panels if any(re.search(
                    r'(?:json:|template:)' + re.escape(key) + r'_all_result$',
                    str(source.record_id or '')) for source in _sources(row))), None)
                if panel is None:
                    panel = next((key for key, (_, title) in structural_panels.items()
                        if _get(row, 'name') == title), None)
                if panel is None:
                    continue
                code = structural_panels[panel][0]
                bound_sources = _sources(row)
                bound_sources.extend(source for source in _native_panel_member_sources(row, rows, panel)
                    if source not in bound_sources)
                target = (data.microbiome_assessment.overall_structure if native_family
                    else data.microbiome_assessment.health_metrics)
                previous = next((item for item in target
                    if item.code == code and item.value == _get(row, 'value') and item.source_status == _get(row, 'source_status')), None)
                item = public.HealthMetricAssessment(code=code, name=_get(row, 'name'),
                    value=_get(row, 'value'), source_status=_get(row, 'source_status'),
                    assessment_level=_get(row, 'source_status'), details=_get(row, 'description'),
                    explanation=previous.explanation if previous else None,
                    evidence_refs=list(previous.evidence_refs) if previous else [],
                    sources=bound_sources, availability=_get(row, 'availability', 'available'))
                (structure if native_family else health).append(item)

    def unique(items):
        result, seen = [], set()
        for item in items:
            identity = (item.code, item.name, item.value, item.source_status,
                tuple((s.record_id, s.observed_at) for s in item.sources))
            if identity not in seen:
                seen.add(identity); result.append(item)
        return _merge_same_native_results(result) if native_family else result

    ranking = data.disease_risk_assessment.risk_ranking
    # The old alias matcher accidentally placed four disease association rows
    # in the health table. Their full source modules now share one correct lane.
    association_names = {_key(name) for names in ASSOCIATION_ALIASES.values() for name in names}
    association_names.update(_key(name) for name in HEALTH_ASSOCIATION_PANELS.values())
    data.microbiome_assessment.health_metrics = [item for item in data.microbiome_assessment.health_metrics
        if _key(item.name) not in association_names and item.code not in {'keystoneBacteria', 'barrierRelatedBacteria'}]
    data.microbiome_assessment.health_metrics.extend(unique(health))
    if native_family:
        codes = {code for code, _ in structural_panels.values()}
        data.microbiome_assessment.overall_structure = [item for item in data.microbiome_assessment.overall_structure
            if item.code not in codes or item.value is None and item.source_status is None]
        for item in unique(structure):
            empty = next((row for row in data.microbiome_assessment.overall_structure
                if row.code == item.code and row.value is None and row.source_status is None), None)
            if empty is not None:
                data.microbiome_assessment.overall_structure.remove(empty)
            data.microbiome_assessment.overall_structure.append(item)
    ranking[:] = [item for item in ranking if not str(item.code or '').startswith(ASSOCIATION_PREFIX)]
    ranking.extend(unique(associations))
    major = data.nutrition_assessment.major_metabolism
    major[:] = [item for item in major if not str(item.code or '').startswith(NUTRITION_PREFIX)]
    major.extend(unique(nutrients))
    if native_family:
        by_code = {item.code: item for item in major if str(item.code or '').startswith(NUTRITION_PREFIX)}
        major[:] = [by_code.get(code) or public.MetricAssessment(code=code, name=name)
            for code, name in catalog.NATIVE_NUTRITION_MODULES] + [item for item in major
                if not str(item.code or '').startswith(NUTRITION_PREFIX) and _literal(item)]
    # A named source-wide qualitative result is an exact concept match for
    # these two macro headings, but is not a score. Amino-acid synthesis is not
    # whole protein metabolism, and no mineral score is inferred from it.
    for native_key, target_code in (('yingyang2_tanshui2', 'carbohydrate'), ('yingyang2_zhizhi2', 'fat')):
        original = next((item for item in nutrients if item.code == NUTRITION_PREFIX + native_key), None)
        target = next((item for item in major if item.code == target_code), None)
        if original is not None and target is not None and target.value is None and target.source_status is None:
            target.source_status = original.source_status
            target.assessment_level = original.source_status
            target._assessment_origin = 'source'
            target.sources = [source.model_copy(deep=True) for source in original.sources]
            target.explanation = original.explanation
            target.availability = 'partial'
    if native_family:
        data.microbiome_assessment.overall_structure = _merge_same_native_results(
            data.microbiome_assessment.overall_structure)
        data.microbiome_assessment.health_metrics = _merge_same_native_results(
            data.microbiome_assessment.health_metrics)
    return data


def native_disease_associations(data, code=None):
    """Read source findings matched to an exact disease concept, not diagnosis.

    Each entry contains ``key``, ``codes``, ``row`` and an actual callback JSON
    ``path``. Passing no code returns all actual source associations, including
    symptom/generic groups which must not populate a narrower fixed disease.
    """
    disease = _get(data, 'disease_risk_assessment', {})
    result = []
    for i, row in enumerate(_get(disease, 'risk_ranking', []) or []):
        identity = str(_get(row, 'code') or '')
        if not identity.startswith(ASSOCIATION_PREFIX) or not _source_c(row) or not _literal(row):
            continue
        key = identity[len(ASSOCIATION_PREFIX):]
        # The supplier's key (for example jkj_yiyu) is not proof that a wider
        # source title such as 情绪障碍 names the narrower depression disease.
        titles = {disease_code: title for definitions in (
            catalog.FIXED_DISEASE_DEFINITIONS, *catalog.SOURCE_DISEASE_CATALOGS.values())
            for _, disease_code, title in definitions}
        codes = tuple(disease_code for disease_code in ASSOCIATION_DISEASE_CODES.get(key, ())
            if same_source_disease(_get(row, 'name'), titles.get(disease_code)))
        if code is None or code in codes:
            result.append(dict(key=key, codes=codes, row=row,
                path=f'/data/diseaseRiskAssessment/riskRanking/{i}'))
    return result
