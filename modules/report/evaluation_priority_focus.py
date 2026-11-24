"""Source-bound patient problems, separate from individual source grades.

Public rows carry stable composite names and actual provenance. Missing AI
attention levels stay empty; no maximum, count or concatenated native grades
is presented as a composite judgement.
"""
from __future__ import annotations

import re
import hashlib
import json
from dataclasses import dataclass

from . import evaluation_schema as public
from .evaluation_result_rules import has_risk_index


PRIORITY_THEMES = (
    ('medical','医学健康问题综合关注','医学评估'),
    ('microbiomeStructure','菌群结构与健康综合状态','肠道菌群评估'),
    ('beneficialBalance','有益菌群综合状态','肠道菌群评估'),
    ('pathogenSignals','致病及条件致病菌综合风险','肠道菌群评估'),
    ('diseaseModelRisk','疾病模型综合风险','疾病风险评估'),
    ('vitaminFunction','维生素合成功能综合状态','营养功能评估'),
    ('lipidFunction','脂质与胆汁酸代谢综合状态','营养功能评估'),
    ('aminoAcidFunction','氨基酸合成功能综合状态','营养功能评估'),
    ('neurotransmitterFunction','神经递质合成功能综合状态','营养功能评估'),
    ('modifiableFactors','生活方式相关因素综合关注','诱因分析'),
)
PRIORITY_LEVELS = ('高','中','低','注意')
_OTHER_FUNCTION_THEME = ('otherFunction','其他功能综合关注','营养功能评估')


@dataclass(frozen=True)
class _ReviewedPriorityProjection:
    code: str
    scope: str
    level: str
    summary: str


def priority_identity(row):
    identity = getattr(row, '_priority_topic_identity', None)
    proof = getattr(row, '_reviewed_priority_projection', None)
    if isinstance(identity, str) and identity.startswith('priority:'):
        return identity
    if isinstance(proof, _ReviewedPriorityProjection):
        return proof.code
    return next(('priority:'+code for code,name,_ in (*PRIORITY_THEMES,_OTHER_FUNCTION_THEME)
        if row.name==name),None)


def _level(row):
    return str(getattr(row,'source_status',None) or getattr(row,'assessment_level',None) or '')


def _abnormal(row):
    labels = [str(getattr(row, key, None) or '').strip()
        for key in ('source_status', 'assessment_level')]
    labels = [label for label in labels if label and not re.fullmatch(
        r'正常|未见异常|无异常|未检出|阴性|normal|not detected', label, re.I)]
    return any(re.search(r'亚健康|失衡|失调|异常|极低|偏低|较低|偏高|升高|下降|降低|缺乏|不足|超标|弱|'
        r'中风险|中高风险|高风险|(?:需要|优先|需)关注|注意|↑|↓|\babnormal\b|\bnot normal\b', label, re.I)
        for label in labels)


def _actual(row):
    return bool(row.sources and (getattr(row,'value',None) is not None
        or getattr(row,'observations',None) is not None or _level(row)))


def _sources(rows):
    result, seen = [], set()
    for row in rows:
        for source in row.sources:
            identity = tuple(getattr(source, name) for name in type(source).model_fields)
            if identity not in seen:
                seen.add(identity)
                result.append(source.model_copy(deep=True))
    return result


def _same_source_observation(first, second, facts=None):
    """A shared page locates evidence; it never identifies one measurement.

    Only actual fact IDs or explicit source-row locators can prove mirrors.
    Different dates, values, source grades or reference cells stay separate,
    even when a wrapper carries an otherwise matching provenance alias.
    """
    fields = ('name', 'value', 'unit', 'reference_range', 'source_status', 'assessment_level')
    if any((type(getattr(first, key, None)), getattr(first, key, None)) !=
            (type(getattr(second, key, None)), getattr(second, key, None)) for key in fields):
        return False
    def identity(row):
        sources = list(getattr(row, 'sources', None) or [])
        dates = {source.observed_at for source in sources if source.observed_at}
        ids = {source.record_id for source in sources if source.record_id}
        checksums = {match[1] for record in ids if (match := re.match(
            r'^source:([0-9a-fA-F]{64}):', record))}
        proven = {record for record in ids if facts and record in facts}
        proven.update(record for record in ids if re.fullmatch(
            r'(?:parsed\.(?:metrics|taxa|disease_results|supplementary_hospital_data)\[\d+\]|'
            r'clinicalDocument\.facts\[\d+\]|'
            r'request\.(?:hospitalData\[\d+\]|microbiomeData\.(?:nutrition|nutritionData|nutrients|'
            r'functionalData|functionalMetrics|functionMetrics|metabolism|antimicrobialResistance|'
            r'antibioticResistance|resistanceGenes|metrics)\[\d+\])|'
            r'source:[^:]+:(?:json:|page:\d+:template:)[A-Za-z][A-Za-z0-9_]*\[\d+\](?:\.[A-Za-z_]+)?)', record))
        return dates, checksums, proven
    dates_a, checksums_a, ids_a = identity(first)
    dates_b, checksums_b, ids_b = identity(second)
    return (bool(ids_a & ids_b) and not (dates_a and dates_b and dates_a != dates_b)
        and not (checksums_a and checksums_b and not checksums_a.intersection(checksums_b)))


def _record_ids(row):
    return {source.record_id for source in row.sources if source.record_id}


def _clinical_focus(row, facts=None):
    return (_abnormal(row) or row.certainty in {'confirmed', 'suspected'}
        or row.certainty == 'self_reported' and str(row.code or '').startswith('reportedSymptom:')
        or any(facts[source.record_id].abnormal for source in row.sources
            if facts and source.record_id in facts))


def _occult_blood(row):
    return bool(re.search(r'隐血|潜血|\b(?:FOB|FOBT|FIT|occult[ -]*blood)\b',
        str(row.name or ''), re.I))


def _clinical_result_title(row):
    name = str(row.name or '')
    value = re.sub(r'\s+', '', str(row.value or ''))
    if re.fullmatch(r'(?:弱|强)?阳性|\+{1,4}|[1-4]\+', value) and value not in name:
        return name + value
    return name


def _clinical_frame_date(value):
    text = str(value or '').strip()
    match = re.match(r'^(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:[T ]|$)', text)
    return ('%04d-%02d-%02d' % tuple(map(int, match.groups()))) if match else text


def _clinical_frame_ids(ids, facts):
    """Only a fact or exact row, never a shared page, identifies a result."""
    return {fid for fid in ids if (facts and fid in facts
        and facts[fid].kind in {'clinical', 'self_reported'}) or re.fullmatch(
            r'(?:request\.hospitalData|parsed\.supplementaryHospitalData|clinicalDocument\.facts)\[\d+\]', fid)}


def _clinical_frame_parts(frame, facts):
    name, ids, *retained_dates = frame
    dates = set(retained_dates[0]) if retained_dates else set()
    dates.update(_clinical_frame_date(facts[fid].date) for fid in ids
        if facts and fid in facts and facts[fid].date)
    return name, tuple(sorted(set(ids))), tuple(sorted(date for date in dates if date))


def _compatible_clinical_frames(first, second, facts):
    name_a, ids_a, dates_a = first
    name_b, ids_b, dates_b = second
    if (name_a != name_b or not _clinical_frame_ids(ids_a, facts).intersection(
            _clinical_frame_ids(ids_b, facts)) or dates_a and dates_b and dates_a != dates_b):
        return False
    hashes = lambda ids: {match[1] for fid in ids if (match := re.match(
        r'^source:([0-9a-fA-F]{64}):', fid))}
    hashes_a, hashes_b = hashes(ids_a), hashes(ids_b)
    return not (hashes_a and hashes_b and not hashes_a.intersection(hashes_b))


def _bound_clinical_frames(data, facts):
    """Use quote-verified problems, not auto-labelled laboratory rosters."""
    from .evaluation_builder import _source_bound_problem_title
    result = []
    for problem in getattr(data, '_clinical_problem_projection', ()):
        # Recovery of a genuine out-of-range value does not itself establish
        # a new disease or make that isolated value a report-wide priority.
        if re.search(r'检查异常(?:待(?:复核|核实|核对))?$', problem.title):
            continue
        title = _source_bound_problem_title(problem,
            [source.quote for source in problem.sources])
        if not title:
            continue
        frame = _clinical_frame_parts((title,
            tuple(source.fact_id for source in problem.sources),
            tuple(_clinical_frame_date(source.date) for source in problem.sources if source.date)), facts)
        if frame not in result:
            result.append(frame)
    # A literal waist/body-composition finding remains in chapter 3, while
    # its same-date evidence belongs under an already adopted obesity issue.
    retained = []
    from .evaluation_fixed_assessment import _clinical_pattern
    for frame in result:
        if _problem_code(frame[0]) is None and any(
                (code := _problem_code(parent[0]))
                and (pattern := _clinical_pattern(code))
                and re.search(pattern, frame[0], re.I)
                and frame[2] == parent[2] for parent in result if parent != frame):
            continue
        retained.append(frame)
    return retained


def _problem_code(name):
    from .evaluation_catalog import fixed_disease_code
    code = fixed_disease_code(name)
    if code:
        return code
    # A literal fatty-liver finding can supply background to that microbial
    # theme without relabelling the patient's aetiology as non-alcoholic.
    if re.fullmatch(r'脂肪肝|脂肪性肝病', str(name or '').strip()):
        return 'nonalcoholicFattyLiver'
    return None


def _problem_supports(name, dates, rows, facts):
    """Attach same-disease, same-date measures to an adopted problem."""
    from .evaluation_fixed_assessment import _clinical_pattern
    code = _problem_code(name)
    pattern = _clinical_pattern(code) if code else None
    if code == 'obesity':
        pattern += r'|身体质量指数|体质指数|腰臀比'
    if not pattern:
        return []
    result = []
    for row in rows:
        # The row name locates an assay. A whole abdominal ultrasound or a
        # broad clinical answer must not make every disease in it equivalent.
        names = [str(row.name or '')]
        names.extend(facts[fid].name for fid in _record_ids(row)
            if facts and fid in facts and facts[fid].kind in {'clinical', 'self_reported'})
        if _occult_blood(row) or not any(re.search(pattern, name, re.I) for name in names):
            continue
        row_dates = _clinical_frame_parts(('', tuple(_record_ids(row)),
            tuple(_clinical_frame_date(source.observed_at) for source in row.sources if source.observed_at)), facts)[2]
        if dates and row_dates and dates != row_dates:
            continue
        result.append(row)
    return result


def _disease_clinical_context(name, rows, facts):
    candidates = _problem_supports(name, (), rows, facts)
    dated = [(row, _clinical_frame_parts(('', tuple(_record_ids(row)),
        tuple(_clinical_frame_date(source.observed_at) for source in row.sources if source.observed_at)), facts)[2])
        for row in candidates]
    dates = [date for _, own in dated for date in own]
    if not dates:
        return candidates
    latest = max(dates)
    return [row for row, own in dated if own == (latest,)]


def _remember_clinical_frames(data, previous, facts=None):
    """Retain the organizer's problem titles, never its grades or prose.

    The initial builder has separately bound clinical problems. Later fixed
    projections must not collapse those problems into one medical theme.
    Private frames survive working copies; a public round trip can recover
    the same source bindings from its concrete overview rows.
    """
    authoritative = bool(getattr(data, '_clinical_problem_projection', ()))
    frames = _bound_clinical_frames(data, facts) if authoritative else [
        _clinical_frame_parts(frame, facts)
        for frame in getattr(data, '_priority_clinical_frames', ())]
    generic = {name for _, name, _ in PRIORITY_THEMES} | {
        '消化道相关异常关注', '肥胖与体成分相关风险', '血脂异常相关风险',
        '肝脏与糖代谢相关风险', '甲状腺相关风险', '胃肠道症状相关关注'}
    for row in previous:
        if authoritative:
            break
        if (row.module != '医学评估' or not row.name or row.name in generic
                or priority_identity(row) is not None or not row.sources):
            continue
        frame = _clinical_frame_parts((row.name, tuple(sorted(_record_ids(row))),
            tuple(_clinical_frame_date(source.observed_at) for source in row.sources if source.observed_at)), facts)
        if frame not in frames:
            frames.append(frame)
    merged = []
    for frame in frames:
        matches = [index for index, old in enumerate(merged)
            if _compatible_clinical_frames(old, frame, facts)]
        if matches:
            position = matches[0]
            for index in reversed(matches):
                old = merged.pop(index)
                frame = (frame[0], tuple(sorted(set(frame[1]) | set(old[1]))),
                    tuple(sorted(set(frame[2]) | set(old[2]))))
            merged.insert(position, frame)
        else:
            merged.append(frame)
    object.__setattr__(data, '_priority_clinical_frames', tuple(merged))


def _reported_problem_background(name, data, facts):
    """Read an explicit same-topic C history as history, never as a new exam."""
    from .evaluation_fixed_assessment import _clinical_pattern
    pattern = _clinical_pattern(_problem_code(name))
    if not pattern or not facts:
        return []
    result = []
    for row in data.appendices.source_items:
        if (row.kind != 'context' or row.category != 'sourceReportedBackground'
                or row.source_type != 'microbiome' or not isinstance(row.value, str)):
            continue
        affirmative = any(re.search(pattern, clause, re.I) and not re.search(
            r'(?:无|未|没有|否认)[^，,。；;、]{0,12}(?:' + pattern + ')', clause, re.I)
            for clause in re.split(r'[，,。；;、]', row.value))
        if affirmative and any(source.source_code == 'C' and source.record_id in facts
                and facts[source.record_id].kind == 'context'
                and facts[source.record_id].value == row.value
                and (not source.observed_at or source.observed_at == facts[source.record_id].date)
                for source in row.sources):
            result.append(row)
    return result


def _ibd_priority_groups(groups, facts):
    """One management theme for an IBD parent and its same-report subtypes.

    The three original models remain distinct in chapter 5. No score or grade
    is combined here; the complete grouped inventory receives its own review.
    """
    family = {'inflammatoryBowelDisease', 'ulcerativeColitis', 'crohnDisease'}
    grouped, positions = [], {}
    for name, rows in groups:
        model = rows[0]
        if _problem_code(model.name) not in family:
            grouped.append((name, rows)); continue
        ids = _record_ids(model)
        dates = tuple(sorted({source.observed_at for source in model.sources if source.observed_at}))
        files = tuple(sorted({facts[fid].source for fid in ids if facts and fid in facts and facts[fid].source}))
        hashes = tuple(sorted({match[1] for fid in ids if (match := re.match(r'^source:([0-9a-fA-F]{64}):', fid))}))
        if not dates or not (files or hashes):
            grouped.append((name, rows)); continue
        origin = (dates, files, hashes)
        if origin not in positions:
            positions[origin] = len(grouped)
            grouped.append((name, rows)); continue
        position = positions[origin]
        retained = grouped[position][1]
        grouped[position] = ('炎症性肠病相关风险', list({id(row): row for row in [*retained, *rows]}.values()))
    return grouped


def _specific_topics(data, topics, facts):
    """Split clinical problems and disease results within their evidence domain.

    No clinical diagnosis is inferred from laboratory thresholds here. A
    clinical title comes from the already adopted organizer or its literal
    finding. Occult-blood examinations always retain a separate problem,
    including an untyped or urinary result; they are not IBD evidence.
    """
    result = []
    for topic in topics:
        domain = topic['code'].split(':', 1)[-1]
        groups = []
        if domain == 'medical':
            rows = [row for row in topic['rows'] if _clinical_focus(row, facts)]
            support_rows = [row for row in topic['rows']
                if getattr(row, 'certainty', None) not in {'model_risk', 'source_association'}]
            covered = set()
            for frame in getattr(data, '_priority_clinical_frames', ()):
                name, ids, dates = _clinical_frame_parts(frame, facts)
                occult = bool(re.search(r'隐血|潜血|\b(?:FOB|FOBT|FIT)\b', name, re.I))
                strong_ids = _clinical_frame_ids(ids, facts)
                actual = [row for row in rows
                    if (strong_ids.intersection(_clinical_frame_ids(_record_ids(row), facts))
                        if strong_ids else _record_ids(row).intersection(ids))
                    and _occult_blood(row) == occult
                    and (not dates or not (row_dates := _clinical_frame_parts(
                        ('', tuple(_record_ids(row)), tuple(_clinical_frame_date(source.observed_at)
                            for source in row.sources if source.observed_at)), facts)[2]) or dates == row_dates)]
                if not actual:
                    continue
                # Two occult results with a shared wrapper still represent
                # distinct examinations, not one patient-level blood value.
                if occult:
                    for row in actual:
                        if id(row) not in covered:
                            groups.append((_clinical_result_title(row), [row]))
                            covered.add(id(row))
                    continue
                signature = frozenset(id(row) for row in actual)
                # One examination can substantiate two separately adopted
                # clinical problems. Shared fact IDs do not merge diagnoses.
                if getattr(data, '_clinical_problem_projection', ()):
                    actual = list({id(row): row for row in [*actual,
                        *_problem_supports(name, dates, support_rows, facts)]}.values())
                    code = _problem_code(name)
                    for model in data.disease_risk_assessment.risk_ranking:
                        if code and code == _problem_code(model.name) and _actual(model):
                            actual.extend([model, *[signal for signal in model.related_taxa if _actual(signal)]])
                actual = list({id(row): row for row in actual}.values())
                groups.append((name, actual))
                signature = frozenset(id(row) for row in actual)
                covered.update(signature)
            for row in rows:
                if id(row) not in covered and not getattr(data, '_clinical_problem_projection', ()):
                    groups.append((_clinical_result_title(row), [row]))
                    covered.add(id(row))
        elif domain == 'diseaseModelRisk':
            clinical = [item for item in [*data.medical_assessment.findings,
                    *data.medical_assessment.normal_findings]
                if _actual(item) and item.certainty not in {'model_risk', 'source_association'}]
            for row in topic['rows']:
                code = _problem_code(row.name)
                matching = [frame for frame in getattr(data, '_priority_clinical_frames', ())
                    if code and _problem_code(frame[0]) == code]
                if matching:
                    # The adopted disease already has one personal problem
                    # with this model and its local taxa attached as evidence.
                    continue
                if _level(row) not in {'中风险', '中高风险', '高风险'} and not matching:
                    continue
                actual = [row, *[signal for signal in row.related_taxa if _actual(signal)]]
                actual.extend(_disease_clinical_context(row.name, clinical, facts))
                actual.extend(_reported_problem_background(row.name, data, facts))
                groups.append((row.name if re.search(r'风险|易感', row.name or '') else
                    (row.name or '') + '相关风险', list({id(item): item for item in actual}.values())))
            groups = _ibd_priority_groups(groups, facts)
        if not groups:
            if domain not in {'medical', 'diseaseModelRisk'} or domain == 'medical' \
                    and not getattr(data, '_clinical_problem_projection', ()):
                result.append(topic)
            continue
        for index, (name, rows) in enumerate(groups):
            fingerprint = hashlib.sha256(json.dumps({'name': name,
                'sources': sorted({source.record_id for row in rows for source in row.sources
                    if source.record_id})}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
            result.append(dict(topic, code=topic['code'] if index == 0 else topic['code'] + ':' + fingerprint,
                name=name, rows=rows, concrete=True, kind='medical' if domain == 'medical' else 'disease',
                hint=('围绕' + str(name or '') + '的实际发现确定当前关注重点。')))
    return result


def priority_topics(data, *, facts=None):
    """Return complete actual source groups, never a representative-only grade."""
    micro,nutrition=data.microbiome_assessment,data.nutrition_assessment
    medical=[row for row in data.medical_assessment.findings if _actual(row)
        and row.certainty not in {'model_risk', 'source_association'}]
    medical_focus=[row for row in medical if _clinical_focus(row, facts)]
    other=[row for row in micro.taxa if row.category=='other'and _actual(row)]
    structure=[row for row in [*micro.overall_structure,*micro.health_metrics]
        if row.code in {'gmwi2','microbiomeHealthScore','speciesCount','diversity','balanceStatus',
            'gutImmunity','antiInflammatoryCapacity','beRatio','fbRatio','pathogenicPotential'}and _actual(row)]
    beneficial=[row for row in micro.taxa if row.category=='beneficial'and _actual(row)]
    pathogen=[row for row in micro.taxa if row.category in {'pathogenic','opportunistic'}and _actual(row)]
    models=[row for row in data.disease_risk_assessment.risk_ranking if has_risk_index(row)and _actual(row)]
    groups=[*nutrition.vitamins_and_minerals,*nutrition.detailed_metabolism]
    vitamins=[row for group in groups if re.search('维生素',group.name or '')
        for row in group.items if _actual(row)]
    lipids=[row for group in groups if re.search('脂|胆汁酸',group.name or '')
        for row in group.items if _actual(row)]
    amino=[row for row in nutrition.amino_acids_and_metabolites if _actual(row)
        and not re.search('同型半胱氨酸|脂多糖',row.name or '')]
    neurotransmitters=[row for row in nutrition.neurotransmitters if _actual(row)]
    represented=[*vitamins,*lipids,*amino,*neurotransmitters,*micro.short_chain_fatty_acids]
    other_functions=[]
    candidates=[*[row for group in groups if group.code!='otherSourceFunctions'for row in group.items],
        *[row for row in nutrition.major_metabolism
            if row.value is not None and not str(row.code or '').startswith('nativeNutrition:')],
        *[row for pathway in data.functional_pathways.pathways for row in pathway.related_metrics]]
    for row in candidates:
        if (_actual(row)and not any(_same_source_observation(row, old, facts) for old in represented)
                and not any(_same_source_observation(row, old, facts) for old in other_functions)):
            other_functions.append(row)
    triggers=[row for row in data.medical_assessment.trigger_analysis
        if row.observations and row.sources and row.evidence_strength in {'强', '中', '弱'}
        and row.modifiability in {'高', '中', '低', '维持'}]
    retained_normals=[row for row in data.medical_assessment.normal_findings if _actual(row)]
    selected={
        'medical':(bool(medical_focus),[*medical,*retained_normals],
            '当前健康管理首先回应已经采用的医学发现与本人自述不适。'),
        'microbiomeStructure':(any(_abnormal(row)for row in [*structure,*other]),
            [*structure,*other,*[row for row in micro.short_chain_fatty_acids if _actual(row)]],
            '菌群整体状态的关注在于结构偏离与功能差异，应综合观察这些维度。'),
        'beneficialBalance':(any(_abnormal(row)for row in beneficial),beneficial,
            '有益菌的组成分布差异是本组关注重点，应结合完整菌群状态判断。'),
        'pathogenSignals':(any(_abnormal(row)for row in pathogen),pathogen,
            '致病及条件致病菌的异常信号需要结合其集中程度与实际不适综合评估。'),
        'diseaseModelRisk':(any(_level(row)in {'中风险','中高风险','高风险'}for row in models),models,
            '疾病模型中较突出的方向构成本组关注重点，应结合已有医学资料判断。'),
        'vitaminFunction':(any(_abnormal(row)for row in vitamins),vitamins,
            '维生素合成的薄弱方向与保留功能应共同纳入本组综合评估。'),
        'lipidFunction':(any(_abnormal(row)for row in lipids),lipids,
            '脂质合成与分解的差异是本组重点，综合关注应落在实际薄弱方向。'),
        'aminoAcidFunction':(any(_abnormal(row)for row in amino),amino,
            '氨基酸合成应兼顾薄弱环节与保留能力，形成明确的局部关注。'),
        'neurotransmitterFunction':(any(_abnormal(row)for row in neurotransmitters),neurotransmitters,
            '神经递质合成中实际偏弱的环节是本组综合关注重点。'),
        'modifiableFactors':(bool(triggers and _behavior_source_title(triggers, facts=facts)
            != '日常饮食与活动习惯'),triggers,
            '已有生活方式线索应结合当前不适判断其关联和可调整方向。'),
        'otherFunction':(any(_abnormal(row)for row in other_functions),other_functions,
            '其他已测功能中的薄弱方向与保留状态需要结合各自功能共同关注。'),
    }
    result=[]
    for code,name,module in (*PRIORITY_THEMES,_OTHER_FUNCTION_THEME):
        eligible,rows,hint=selected[code]
        if eligible and rows:
            result.append(dict(code='priority:'+code,name=name,module=module,rows=rows,hint=hint))
    return _specific_topics(data, result, facts)



def _literal_names(rows, limit=3):
    names = list(dict.fromkeys(row.name for row in rows if row.name))
    return '、'.join(names[:limit]) + ('等' if len(names) > limit else '')


def _concrete_topic_name(topic, *, facts=None):
    """Use actual result nouns and literal source states in patient titles.

    This is an editorial title projection, not a new clinical assessment.
    The independent composite level remains owned by reviewed metadata.
    """
    if topic.get('concrete'):
        return topic['name']
    code = topic['code'].split(':', 1)[-1]
    rows = topic['rows']
    abnormal = [row for row in rows if _abnormal(row)]
    if code == 'medical':
        actual = [row for row in rows if getattr(row, 'certainty', None) not in
            {'model_risk', 'source_association'} and (_abnormal(row)
                or getattr(row, 'certainty', None) in {'confirmed', 'suspected', 'self_reported'})]
        return _literal_names(actual, 4) or topic['name']
    if code == 'microbiomeStructure':
        balance = next((row for row in rows if row.code == 'balanceStatus' and (_level(row)
            or isinstance(row.value, str) and re.fullmatch(r'(?:[ⅠⅡⅢⅣIViv一二三四1-4]+度)?失调|正常|平衡|失衡', re.sub(r'\s+', '', row.value)))), None)
        immune = next((row for row in rows if row.code == 'gutImmunity' and _abnormal(row)), None)
        phrases = [('菌群' + (_level(balance) or re.sub(r'\s+', '', balance.value)))] if balance else []
        if immune:
            phrases.append('肠道免疫力' + _level(immune))
        return '、'.join(phrases) or '菌群结构异常'
    if code in {'beneficialBalance', 'pathogenSignals'}:
        actual = abnormal or rows
        down = [row for row in actual if re.search(r'偏低|降低|缺乏|不足|↓', _level(row))]
        up = [row for row in actual if re.search(r'偏高|升高|超标|↑', _level(row))]
        if down and not up:
            return _literal_names(down, 2) + '偏低'
        if up and not down:
            return _literal_names(up, 2) + '偏高'
        if down and up:
            group = '有益菌' if code == 'beneficialBalance' else '致病及条件致病菌'
            return group + '组成异常'
        return _literal_names(actual, 2) + '异常'
    if code == 'diseaseModelRisk':
        elevated = [row for row in rows if _level(row) in {'中风险', '中高风险', '高风险'}]
        return '、'.join(row.name + '菌群模型' + _level(row) for row in elevated) or topic['name']
    names = {
        'vitaminFunction': '维生素合成能力偏弱',
        'lipidFunction': '脂质与胆汁酸代谢能力偏弱',
        'aminoAcidFunction': '氨基酸合成能力偏弱',
        'neurotransmitterFunction': '神经递质合成能力偏弱',
        'otherFunction': '天然色素、核苷酸与毒性物质代谢异常',
    }
    if code in names:
        if code == 'otherFunction':
            themes = [label for pattern, label in ((r'色素|胡萝卜|黄酮|萜|植醇', '天然色素'),
                (r'核苷|嘌呤|嘧啶', '核苷酸'), (r'降解|分解|苯|萘|己内酰', '毒性物质'))
                if any(re.search(pattern, str(row.name or '')) for row in abnormal)]
            return '、'.join(themes) + '代谢异常' if themes else _literal_names(abnormal, 2) + '功能异常'
        if code == 'aminoAcidFunction':
            # This inventory also contains amino-acid-derived metabolites.
            # One weak glutathione result does not regrade normal amino acids.
            return _literal_names(abnormal, 2) + '相关功能需关注'
        weak = any(re.search(r'低|弱|缺乏|不足', _level(row)) for row in abnormal)
        return names[code] if weak else names[code].replace('能力偏弱', '异常')
    if code == 'modifiableFactors':
        return _behavior_source_title(rows, facts=facts)
    return topic['name']


def _behavior_source_title(rows, *, facts=None):
    """Name affirmed behaviour results, never a question's denied keyword."""
    from .evaluation_fixed_assessment import _answer
    answers = []
    known = facts if isinstance(facts, dict) else {}
    for row in rows:
        own = [known[ref.record_id] for ref in row.sources if ref.record_id in known
            and getattr(known[ref.record_id], 'kind', None) == 'self_reported']
        texts = [_answer(fact) for fact in own] or [str(row.observations or '')]
        for text in texts:
            for clause in re.split(r'[，,。；;\n]', text):
                clause = clause.strip()
                if not clause or re.search(r'不清楚|不知道|未知|不详|未提供|未回答', clause):
                    continue
                answers.append(clause)
    terms = []
    for pattern, label in ((r'夜宵', '夜宵'), (r'外卖', '外卖摄入'),
            (r'很少(?:锻炼|运动)|缺乏(?:锻炼|运动)|运动(?:偏|较)?少', '日常活动不足'),
            (r'久坐|坐着(?:约|通常)?[0-9一二两三四五六七八九十]+小时', '连续坐位'),
            (r'蔬菜[^，,。；;]{0,15}(?:两三口|[0-9一二两三四五六七八九十]+口)', '蔬菜每餐摄入少'),
            (r'蔬(?:果|菜)(?:摄入|量)?(?:偏|较)?少|蔬菜量不固定', '蔬菜摄入不足或不固定'),
            (r'很少吃全谷物|全谷物(?:摄入)?(?:很|较|偏)?少', '全谷物摄入少'),
            (r'饮水(?:偏|较)?少', '饮水偏少'), (r'睡眠不足', '睡眠不足')):
        if any(re.search(pattern, text) and not re.search(
                r'(?:不|无|没有|从不|未|否认)[^，,。；;]{0,5}(?:' + pattern + ')', text) for text in answers):
            terms.append(label)
    # A positive smoking count or an explicit affirmed smoking statement is
    # required. “每日吸烟：0支” and “家庭无室内吸烟” are protective source facts.
    if any((re.search(r'(?:每日|每天)?吸烟[：:]?\s*(?:[1-9]\d*(?:[.]\d+)?)\s*支', text)
            or re.search(r'目前(?:仍)?吸烟|现在吸烟|经常吸烟', text))
            and not re.search(r'不吸烟|无[^，,。；;]{0,8}吸烟|没有[^，,。；;]{0,8}吸烟|'
                r'未[^，,。；;]{0,8}吸烟|否认[^，,。；;]{0,8}吸烟', text) for text in answers):
        terms.append('吸烟')
    return '、'.join(terms[:4]) or '日常饮食与活动习惯'


def _direction_taxa(direction, data):
    own = {source.record_id for source in direction.sources if source.record_id}
    return [row for row in data.microbiome_assessment.taxa if _abnormal(row)
        and own.intersection(source.record_id for source in row.sources)]


def _compact_source_basis(direction):
    text = str(direction.basis or '')
    text = re.sub(r'(^|[；;\n])\s*问题[:：][^；;\n]*[；;]\s*回答[:：]', r'\1', text)
    text = re.sub(r'(^|[；;\n])\s*回答[:：]', r'\1', text)
    text = re.sub(r'[；;]{2,}', '；', text).strip('；; \n')
    direction.basis = text or None


def project_concrete_priority_names(data, *, facts=None):
    """Keep chapter 8 names tied to actual conditions rather than broad goals."""
    topics = {topic['code']: topic for topic in priority_topics(data, facts=facts)}
    for direction in data.comprehensive_conclusion.priority_directions:
        name = str(direction.name or '')
        bound = {ref.record_id for ref in direction.sources if ref.record_id}
        models = [row for row in data.disease_risk_assessment.risk_ranking
            if has_risk_index(row) and _level(row) in {'中风险', '中高风险', '高风险'}
            and bound.intersection(ref.record_id for ref in row.sources)]
        if models and re.search(r'风险管理|疾病风险|孤独症|精神相关', name):
            direction.name = '、'.join(row.name + '菌群模型' + _level(row) for row in models)
        elif name in {'菌群结构与肠道状态改善', '菌群结构与肠道状态关注', '有益菌及致病菌组成异常'} \
                or getattr(direction, '_concrete_taxa_direction', False):
            actual = _direction_taxa(direction, data)
            if actual:
                representatives, groups = [], set()
                for row in actual:
                    if row.category not in groups:
                        representatives.append(row)
                        groups.add(row.category)
                    if len(representatives) == 3:
                        break
                for row in actual:
                    if len(representatives) == 3:
                        break
                    if row not in representatives:
                        representatives.append(row)
                direction.name = _literal_names(representatives, 2) + '组成异常'
                object.__setattr__(direction, '_concrete_taxa_direction', True)
                direction.basis = '菌群组成存在偏离，主要表现为' + '、'.join(
                    row.name + '（' + _level(row) + '）' for row in representatives) + ('等' if len(actual) > 3 else '') + '。'
        elif name == '营养与代谢功能关注':
            actual = [topic for code, topic in topics.items() if code in {
                'priority:vitaminFunction', 'priority:lipidFunction', 'priority:aminoAcidFunction',
                'priority:neurotransmitterFunction', 'priority:otherFunction'}]
            phrases = list(dict.fromkeys(_concrete_topic_name(topic, facts=facts).replace('能力偏弱', '') for topic in actual))
            direction.name = ('、'.join(phrases[:3]) + ('等' if len(phrases) > 3 else '') + '功能弱项') if actual else name
        elif name == '鼻部表现关注':
            direction.name = '鼻塞、喷嚏与鼻黏膜异常'
        else:
            direction.name = re.sub(r'检查异常$|检验异常随访$|异常随访$', '异常', name)
        _compact_source_basis(direction)
    return data


def _topic_scope(data, topic, facts):
    if not facts:
        return None
    from .evaluation_synthesis import native_function_source_context
    ids={s.record_id for row in topic['rows']for s in row.sources if s.record_id in facts}
    if not ids:
        return None
    groups=[group for group in native_function_source_context(
        data.model_dump(mode='json',by_alias=True),facts)
        if ids.intersection(fid for member in group['members']for fid in member['factIds'])]
    ids.update(fid for group in groups for fid in group['factIds']if fid in facts)
    rows=[]
    for row in topic['rows']:
        item={key:getattr(row,key,None)for key in ('code','name','value','unit',
            'reference_range','source_status','assessment_level','observations','certainty')}
        clinical_names=sorted({facts[source.record_id].name for source in row.sources
            if source.record_id in facts and facts[source.record_id].kind in {'clinical','self_reported'}})
        if clinical_names:
            # Display-only abbreviation cleanup does not change the exact
            # bound examination. Its full original name and text are also
            # included in the fact digest below; values/grades stay literal.
            item['name']=clinical_names
        rows.append(item)
    payload={'code':topic['code'],'name':topic['name'],'kind':topic.get('kind'),
        'rows':rows,'nativeGroups':groups,
        'facts':[facts[fid].model_dump(mode='json',by_alias=True)for fid in sorted(ids)]}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True,
        allow_nan=False).encode()).hexdigest()


def install_reviewed_priority(data, metadata, *, facts):
    """Internal caller must first verify its independent metadata review."""
    code=metadata['code']
    topic=next((topic for topic in priority_topics(data,facts=facts)if topic['code']==code),None)
    row=next((row for row in data.health_overview.key_findings if priority_identity(row)==code),None)
    scope=_topic_scope(data,topic,facts)if topic else None
    summary=metadata.get('judgment')
    if (row is None or scope is None or metadata.get('assessmentLevel')not in PRIORITY_LEVELS
            or not isinstance(summary,str)or not summary.strip()):
        return False
    row.assessment_level=metadata['assessmentLevel']
    row.summary=summary.strip()
    row.availability='available'
    object.__setattr__(row,'_reviewed_priority_projection',_ReviewedPriorityProjection(
        code,scope,row.assessment_level,row.summary))
    _sort_priority_findings(data)
    return True


def _sort_priority_findings(data):
    """Order reviewed personal attention without regrading source metrics."""
    levels = {'高': 0, '中': 1, '注意': 2, '低': 3}
    def key(row):
        proof = getattr(row, '_reviewed_priority_projection', None)
        level = levels.get(proof.level, 4) if isinstance(proof, _ReviewedPriorityProjection) else 4
        return (level, not getattr(row, '_priority_concrete', False))
    data.health_overview.key_findings.sort(key=key)
    for rank, row in enumerate(data.health_overview.key_findings, 1):
        row.rank = rank


def _internal_priority_text(value):
    text = str(value or '').strip()
    return not text or bool(re.search(
        r'围绕.{0,60}的实际发现确定当前关注重点|按(?:上述|本项|本次)?规则|'
        r'原(?:报告|模型).{0,24}(?:记录保留|仅作参考)|'
        r'未给(?:出)?权重|不作为.{0,16}风险证据|source_fact|factIds|'
        r'评分(?:方法|规则)|来源模型风险提示', text, re.I))


def _project_topic_directions(data, topics):
    """Keep one actual action slot per adopted problem, never a lab specimen."""
    previous = list(data.comprehensive_conclusion.priority_directions)
    result = []
    for topic in topics:
        name = _concrete_topic_name(topic)
        ids = {source.record_id for row in topic['rows'] for source in row.sources if source.record_id}
        sources = _sources(topic['rows'])
        matches = [row for row in previous if (
            getattr(row, '_priority_topic_identity', None) == topic['code']
            or row.name == name and ids.intersection(source.record_id for source in row.sources))]
        row = matches[0].model_copy(deep=True) if matches else public.PriorityDirection()
        row.rank, row.name, row.sources = len(result) + 1, name, sources
        if _internal_priority_text(row.basis):
            row.basis = None
        if _internal_priority_text(row.followup_action):
            row.followup_action = None
        # A source's specific recommendation may be retained for its own
        # problem. Do not concatenate unrelated tests' generic advice.
        if row.followup_action is None and topic.get('kind') == 'medical':
            advice = list(dict.fromkeys(str(item.source_advice).strip()
                for item in topic['rows'] if isinstance(item, public.MedicalFinding)
                and item.source_advice and not _internal_priority_text(item.source_advice)))
            if len(advice) == 1:
                row.followup_action = advice[0]
        row.availability = 'available' if row.followup_action else 'partial'
        object.__setattr__(row, '_priority_topic_identity', topic['code'])
        result.append(row)
    data.comprehensive_conclusion.priority_directions = result


def reviewed_priority_identity(data, row, *, facts):
    """Return an identity only for an unchanged, live reviewed judgement."""
    proof = getattr(row, '_reviewed_priority_projection', None)
    if (not isinstance(proof, _ReviewedPriorityProjection)
            or row.assessment_level != proof.level or row.summary != proof.summary):
        return None
    topic = next((topic for topic in priority_topics(data, facts=facts)
        if topic['code'] == proof.code), None)
    if (topic is None or row.name != _concrete_topic_name(topic, facts=facts)
            or row.module != topic['module'] or row.sources != _sources(topic['rows'])
            or _topic_scope(data, topic, facts) != proof.scope):
        return None
    return proof.code


def carry_reviewed_priority_state(previous, result, *, facts):
    """Restore live review provenance across validation, never from a DTO.

    The caller first binds ``result`` to the real clinical document. A
    matching name or serialized label alone cannot recreate review approval;
    both exact public row contents and the complete current source scope
    must still match the private proof on the previous typed assessment.
    """
    if not isinstance(previous, public.EvaluationData) or not isinstance(result, public.EvaluationData):
        return result
    _remember_clinical_frames(result, result.health_overview.key_findings, facts)
    old_topics = {topic['code']: topic for topic in priority_topics(previous, facts=facts)}
    new_topics = {topic['code']: topic for topic in priority_topics(result, facts=facts)}

    def signature(row, fields):
        return json.dumps({**{field: getattr(row, field, None) for field in fields},
            'sources': [source.model_dump(mode='json', by_alias=True) for source in row.sources]},
            ensure_ascii=False, sort_keys=True, allow_nan=False)

    fields = ('name', 'module', 'assessment_level', 'summary')
    old_rows, new_rows = {}, {}
    for row in previous.health_overview.key_findings:
        old_rows.setdefault(signature(row, fields), []).append(row)
    for row in result.health_overview.key_findings:
        new_rows.setdefault(signature(row, fields), []).append(row)
    for identity, matches in new_rows.items():
        candidates = old_rows.get(identity, [])
        if len(matches) != 1 or len(candidates) != 1:
            continue
        old, row = candidates[0], matches[0]
        proof = getattr(old, '_reviewed_priority_projection', None)
        if (not isinstance(proof, _ReviewedPriorityProjection)
                or old.assessment_level != proof.level or old.summary != proof.summary):
            continue
        old_topic, topic = old_topics.get(proof.code), new_topics.get(proof.code)
        if (old_topic is None or topic is None
                or _topic_scope(previous, old_topic, facts) != proof.scope
                or _topic_scope(result, topic, facts) != proof.scope):
            continue
        object.__setattr__(row, '_priority_topic_identity', proof.code)
        object.__setattr__(row, '_priority_concrete', bool(topic.get('concrete')))
        object.__setattr__(row, '_reviewed_priority_projection', proof)

    # An identity only locates an action's problem; it does not approve new
    # action prose. That prose retains the independent narrative review gate.
    old_directions, new_directions = {}, {}
    for row in previous.comprehensive_conclusion.priority_directions:
        old_directions.setdefault(signature(row, ('name',)), []).append(row)
    for row in result.comprehensive_conclusion.priority_directions:
        new_directions.setdefault(signature(row, ('name',)), []).append(row)
    for identity, matches in new_directions.items():
        candidates = old_directions.get(identity, [])
        if len(matches) != 1 or len(candidates) != 1:
            continue
        code = getattr(candidates[0], '_priority_topic_identity', None)
        old_topic, topic = old_topics.get(code), new_topics.get(code)
        if (not isinstance(code, str) or old_topic is None or topic is None
                or _topic_scope(previous, old_topic, facts) != _topic_scope(result, topic, facts)):
            continue
        object.__setattr__(matches[0], '_priority_topic_identity', code)
    return result


def finalize_priority_focus(data, *, facts=None):
    """Publish bounded, reviewed priorities; retain all original chapter rows.

    Call once after all narrative and qualitative reviews, before reference
    numbering. Keeping full candidates earlier lets every real problem have
    its own independent assessment instead of being lost to an initial cap.
    """
    from .evaluation_risk_projection import has_integrated_risk_projection
    if has_integrated_risk_projection(data):
        return data
    _sort_priority_findings(data)
    reviewed = []
    topics = {topic['code']: topic for topic in priority_topics(data, facts=facts)}
    for row in data.health_overview.key_findings:
        proof = getattr(row, '_reviewed_priority_projection', None)
        if (not isinstance(proof, _ReviewedPriorityProjection)
                or row.assessment_level != proof.level or row.summary != proof.summary
                or _internal_priority_text(row.summary)):
            continue
        topic = topics.get(proof.code)
        if (topic is None or facts and (reviewed_priority_identity(data, row, facts=facts) != proof.code
                or _topic_scope(data, topic, facts) != proof.scope)):
            continue
        reviewed.append(row)
    data.health_overview.key_findings = reviewed[:8]
    for rank, row in enumerate(data.health_overview.key_findings, 1):
        row.rank = rank
    by_code = {getattr(row, '_priority_topic_identity', None): row
        for row in data.comprehensive_conclusion.priority_directions}
    actions = []
    for finding in data.health_overview.key_findings:
        code = priority_identity(finding)
        direction = by_code.get(code)
        if direction is None:
            direction = public.PriorityDirection(sources=[source.model_copy(deep=True) for source in finding.sources])
            object.__setattr__(direction, '_priority_topic_identity', code)
        if _internal_priority_text(direction.followup_action):
            direction.followup_action = None
        direction.name = finding.name
        direction.basis = finding.summary
        direction.rank = len(actions) + 1
        direction.availability = 'available' if direction.followup_action else 'partial'
        actions.append(direction)
    data.comprehensive_conclusion.priority_directions = actions
    focus = [row for row in data.health_overview.key_findings if row.assessment_level == '高']
    focus = (focus or data.health_overview.key_findings)[:3]
    data.comprehensive_conclusion.next_steps = [public.NextStep(
        code='priority_observation', name='后续观察重点',
        description='优先跟进' + '、'.join(row.name for row in focus) + '对应的健康表现及后续评估结果。',
        sources=_sources(focus), evidence_refs=list(dict.fromkeys(number
            for row in focus for number in row.evidence_refs)), availability='available')] if focus else []
    return data


def project_priority_focus(data, *, previous=(), facts=None):
    """Project concrete source problems; independent metadata owns the level.

    Caller restores approved priority metadata after this source projection.
    A raw DTO's label, old concatenated source grades or a previous name is
    not evidence of an independently reviewed composite classification.
    """
    previous=[*previous,*data.health_overview.key_findings]
    _remember_clinical_frames(data, previous, facts)
    result=[]
    for index,topic in enumerate(priority_topics(data,facts=facts),1):
        row=public.OverviewFinding(rank=index,name=_concrete_topic_name(topic, facts=facts),module=topic['module'],
            summary=None,assessment_level=None,sources=_sources(topic['rows']),availability='partial')
        object.__setattr__(row, '_priority_topic_identity', topic['code'])
        object.__setattr__(row, '_priority_concrete', bool(topic.get('concrete')))
        scope=None
        for old in previous:
            proof=getattr(old,'_reviewed_priority_projection',None)
            if (not isinstance(proof,_ReviewedPriorityProjection)or proof.code!=topic['code']
                    or old.assessment_level!=proof.level or old.summary!=proof.summary):
                continue
            scope=scope or _topic_scope(data,topic,facts)
            if scope==proof.scope:
                row.assessment_level,row.summary,row.availability=proof.level,proof.summary,'available'
                object.__setattr__(row,'_reviewed_priority_projection',proof)
                break
        result.append(row)
    data.health_overview.key_findings=result
    _sort_priority_findings(data)
    project_concrete_priority_names(data, facts=facts)
    _project_topic_directions(data, priority_topics(data, facts=facts))
    return data
