"""Fixed-template projections from adopted records, never invented measurements.

Disease model indices remain C observations. A/B records can supply qualitative
attention and a medical summary, but cannot supply a missing microbiome index.
All original records remain in the private source inventory.
"""
from __future__ import annotations

from copy import deepcopy
import re
import unicodedata

from . import evaluation_catalog as catalog
from . import evaluation_schema as public


def _get(row, name, default=None):
    return row.get(name, default) if isinstance(row, dict) else getattr(row, name, default)


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


def _answer(row):
    raw = _get(row, 'value')
    value = ('' if raw is None else str(raw)).strip()
    match = re.fullmatch(r'问题：.*?；回答：(.*)', value, re.S)
    return match[1].strip() if match else value


def _questionnaire_question(row):
    value = str(_get(row, 'value') or '')
    match = re.fullmatch(r'问题：(.*?)；回答：.*', value, re.S)
    return match[1] if match else str(_get(row, 'name') or '')


def _goal_only_question(row):
    question = _questionnaire_question(row)
    return bool(re.search(r'健康目标|健康管理目标|目标排序|希望改善和观察', question)
        and not re.search(r'主诉|主要不适|当前不适|身体不适|症状|健康困扰', question))


def _goal_clause(text):
    return bool(re.search(r'希望|想(?:知道|了解|改善|调整)|目标|优先改善', text)
        or re.match(r'\s*(?:规律排便|减少|增加|建立|保持|持续记录|并持续记录|管理体重)', text))


def _sources(rows):
    result = []
    for row in rows:
        for source in _get(row, 'sources', []) or []:
            if isinstance(source, dict):
                source = public.SourceReference.model_validate(source)
            if source not in result:
                result.append(source.model_copy(deep=True))
    return result


def _names(row):
    return str(_get(row, 'name') or '') + ' ' + _answer(row)


def _affirmative(row, *, family=False, pattern=None):
    text = _answer(row)
    if not text or re.fullmatch(r'(?i:false)|不清楚.*|不详|记不清.*|未知|无|否|没有|未见|阴性|未检出|未患|'
            r'没有明确诊断的疾病[。.]?|无明确诊断.*', text):
        return False
    family_record = bool(re.search(r'家族|父亲|母亲|血亲|亲属|兄弟|姐妹|子女', _names(row)))
    if family_record != family:
        return False
    if pattern:
        clauses = [part.strip() for part in re.split(r'[，,。；;\n]', text) if part.strip()]
        matching = [part for part in clauses if re.search(pattern, part, re.I)]
        if matching:
            for part in matching:
                for match in re.finditer(pattern, part, re.I):
                    before, after = part[:match.start()], part[match.end():]
                    if re.search(r'(?:否认|未见|没有|未患|未确诊|未诊断|排除|不考虑|无)[^，,。；;]{0,10}$', before):
                        continue
                    if re.match(r'\s*[：:]?\s*(?:无|否|未见|没有|阴性|未检出|已排除)(?:$|[。；;])', after):
                        continue
                    return True
            return False
        if re.fullmatch(r'否认.*|未患.*|未确诊.*|未诊断.*|未见.*|无.*|没有.*', text):
            return False
    elif re.match(r'否认|未患|未确诊|未诊断|未见|不清楚|未知', text):
        return False
    return True


_CLINICAL = {
    'ulcerativeColitis': r'溃疡性结肠炎', 'inflammatoryBowelDisease': r'炎症性肠病|\bIBD\b',
    'crohnDisease': r'克罗恩', 'constipation': r'便秘|排便费力|排便困难',
    'ibsDiarrhea': r'腹泻型肠易激', 'type2Diabetes': r'糖尿病|糖化血红蛋白|HbA1c|血糖|胰岛素|C肽',
    'irritableBowelSyndrome': r'肠易激综合[征症]|(?<![A-Za-z0-9])IBS(?![A-Za-z0-9])',
    'autismSpectrumDisorder': r'孤独症|自闭症(?:谱系障碍)?',
    'kidneyStones': r'肾结石|肾.{0,4}结石', 'coronaryHeartDisease': r'冠心病|冠状动脉',
    'nonalcoholicFattyLiver': r'脂肪肝|脂肪性肝病',
    'hyperlipidemia': r'血脂|胆固醇|甘油三酯|LDL|HDL|apoA|apoB',
    'obesity': r'肥胖|体重指数|\bBMI\b|腰围|内脏脂肪|体脂',
    'hypertension': r'高血压|血压', 'hyperuricemia': r'尿酸|高尿酸', 'liverCirrhosis': r'肝硬化',
    'systemicLupusErythematosus': r'红斑狼疮|抗核抗体|抗dsDNA',
    'rheumatoidArthritis': r'类风湿|抗CCP', 'ankylosingSpondylitis': r'强直性脊柱炎|HLA.B27',
    'autoimmuneHepatitis': r'自身免疫性肝炎', 'anxiety': r'焦虑|GAD',
    'depressiveTendency': r'抑郁|PHQ', 'schizophrenia': r'精神分裂', 'bipolarDisorder': r'双相',
    'parkinsonsDisease': r'帕金森', 'epilepsy': r'癫痫', 'multipleSclerosis': r'多发性硬化',
    'alzheimersDisease': r'阿尔[兹茨]海默|Alzheimer',
    'recurrentClostridioidesDifficileInfection': r'复发(?:性)?艰难梭(?:菌|状芽孢杆菌)感染|recurrent\s+Clostridi(?:oides|um)\s+difficile\s+infection|\brCDI\b',
    'type1Diabetes': r'(?:1|I|Ⅰ)型糖尿病|type\s*1\s*diabetes|\bT1DM?\b',
    'metabolicAssociatedFattyLiverDisease': r'代谢(?:功能障碍)?相关脂肪性肝病|\bMASLD\b',
    'majorDepressiveDisorder': r'重性抑郁障碍',
    'mildCognitiveImpairment': r'轻度认知(?:功能)?障碍|mild\s+cognitive\s+impairment|\bMCI\b',
    'frailty': r'衰弱|frailty', 'sarcopenia': r'肌少症|肌肉减少症|sarcopenia',
    'osteoporosis': r'骨质疏松|骨密度|osteoporosis',
    'atherosclerosis': r'动脉粥样硬化|atherosclerosis',
    'cognitiveDecline': r'认知(?:功能)?下降|cognitive\s+decline',
    'thyroidCancer': r'甲状腺.{0,8}(?:癌|结节)|TI.RADS',
    'colorectalCancer': r'结直肠癌|结肠癌|直肠癌|(?:大便|粪便|便)隐血|结肠.{0,8}(?:结节|占位|肿块)',
    'gastricCancer': r'胃癌|胃.{0,6}恶性', 'liverCancer': r'肝癌|肝.{0,6}恶性',
}


# Source-native headings have the same A/B duties as numeric model questions.
# These are theme locators, not new diagnoses or microbial model features.
_NATIVE_CLINICAL = {
    'jkj_ganran': r'胃肠道感染|感染性腹泻|急性胃肠炎',
    'jkj_futong': r'腹痛|腹部疼痛', 'jkj_chronic_diarrhea': r'腹泻|稀便',
    'jkj_fuzhangpaiqi': r'腹胀|排气多|胀气', 'jkj_weizhang': r'胃胀|餐后饱胀|饭后腹胀',
    'jkj_HF': r'冠心病|冠状动脉|高血压|血压|胆固醇|甘油三酯|LDL|HDL',
    'jkj_allergy': r'过敏|荨麻疹|湿疹|鼻炎|鼻黏膜|嗜酸性粒细胞|(?<![A-Za-z])IgE(?![A-Za-z])',
    'jkj_hepatopathy': r'脂肪肝|脂肪性肝病|肝脏.{0,6}回声|丙氨酸氨基转移酶|(?<![A-Za-z])ALT(?![A-Za-z])',
    'jkj_osteoporosis': r'骨质疏松|骨密度|脆性骨折',
    'jkj_yiyu': r'情绪低落|心情低落|抑郁|焦虑|情绪困扰',
    'jkj_shuimian': r'入睡困难|失眠|睡眠不足|睡眠障碍|夜间易醒|睡眠质量差',
    'jkj_shuimianhuxizanting': r'睡眠呼吸暂停|打鼾|憋醒|憋气|呼吸暂停',
    'jkj_aud': r'饮酒|喝酒|酒精|酗酒',
    'jkj_children_CDD': r'慢性腹泻|长期.{0,6}腹泻|反复.{0,6}腹泻',
    'jkj_children_CeD': r'乳糜泻|麸质.{0,6}(?:过敏|不耐受)|抗.{0,6}(?:tTG|肌内膜)',
    'jkj_children_FD': r'功能性消化不良|消化不良|(?:餐后|饭后).{0,6}(?:饱胀|腹胀|胃胀)',
    'jkj_children_AD': r'湿疹|特应性皮炎|反复.{0,6}皮疹',
    'jkj_children_AR': r'鼻塞|喷嚏|流涕|鼻炎|鼻黏膜|嗜酸性粒细胞',
    'jkj_children_FA': r'食物过敏|(?:奶|蛋|大豆).{0,6}过敏|食用.{0,6}(?:皮疹|荨麻疹)',
    'jkj_children_HSP': r'过敏性紫癜|IgA.{0,6}血管炎|皮肤紫癜',
    'jkj_children_asthma': r'哮喘|喘息|喘鸣',
    'jkj_children_RRTI': r'反复呼吸道感染|反复感冒|反复肺炎|反复支气管炎',
    'jkj_children_IDA': r'缺铁|贫血|血红蛋白|血清铁|铁蛋白|转铁蛋白',
    'jkj_children_innutrition': r'营养不良|体重不增|体重下降|体格发育迟缓|生长迟缓',
    'jkj_children_ADHD': r'多动|注意力不集中|注意缺陷',
    'jkj_children_TD': r'抽动',
}


def _clinical_pattern(code):
    if not str(code).startswith('nativeAssociation:'):
        return _CLINICAL.get(code)
    panel = code.split(':', 1)[1]
    if panel in _NATIVE_CLINICAL:
        return _NATIVE_CLINICAL[panel]
    # Only a complete source-title equivalence may reuse an older disease
    # pattern. No parent IBD -> Crohn/UC or mood -> depression substitution.
    title = next((name for rows in catalog.SOURCE_ASSOCIATION_DIRECTORY.values()
        for _, key, name, _ in rows if key == panel), None)
    return _CLINICAL.get(catalog.fixed_disease_code(title)) if title else None


_SYSTEM = {
    'intestinal': '消化系统/消化内科', 'metabolic': '代谢/内分泌科',
    'autoimmune': '免疫系统/风湿免疫科', 'aging': '老年健康/相关专科',
    'mental': '心理精神/精神心理科', 'neurological': '神经系统/神经内科',
    'tumor': '肿瘤相关专科',
}


def _clinical_matches(code, rows, *, abnormal_only=False):
    pattern = _clinical_pattern(code)
    if not pattern:
        return []
    found = []
    for row in rows:
        kind = _get(row, 'kind')
        if kind not in {'clinical', 'self_reported'} or _goal_only_question(row) or not re.search(pattern, _names(row), re.I):
            continue
        # A specific source diagnosis cannot silently fill a different
        # subtype/older diagnostic term's empty disease slot.
        distinct = {'type2Diabetes': 'type1Diabetes',
            'nonalcoholicFattyLiver': 'metabolicAssociatedFattyLiverDisease',
            'depressiveTendency': 'majorDepressiveDisorder'}
        if code in distinct and re.search(_CLINICAL[distinct[code]], _names(row), re.I):
            continue
        if not _affirmative(row, pattern=pattern):
            continue
        if abnormal_only and kind == 'clinical' and not _get(row, 'abnormal', False):
            status = str(_get(row, 'source_status') or '')
            if not re.search(r'异常|升高|降低|偏高|偏低|阳性|超标|↑|↓', status):
                # A literal disease/imaging finding is usable without a lab flag.
                if not re.search(r'诊断|已患|患有|病史|结石|结节|占位|脂肪肝|肥胖|高脂血症', _names(row)):
                    continue
        found.append(row)
    return found


def _same_source_rows(rows):
    """Remove exact mirrored records, never independent times/specimens."""
    result = []; seen = set()
    for row in rows:
        identity = (_get(row, 'kind'), _get(row, 'name'), _answer(row), _get(row, 'unit'),
            _get(row, 'reference_range'), _get(row, 'source_status'), _get(row, 'observed_at'),
            _get(row, 'source_name'), tuple((s.source_code, s.record_id, s.observed_at) for s in _sources([row])))
        if identity not in seen:
            seen.add(identity); result.append(row)
    return result


def _literal(row):
    value = _answer(row)
    if _get(row, 'kind') == 'self_reported':
        return value
    unit = str(_get(row, 'unit') or '')
    return str(_get(row, 'name') or '') + '：' + value + (' ' + unit if unit and unit not in value else '')


def _clinical_literal(code, row):
    """A concise exact-clause display; never rewrite the underlying source."""
    if _get(row, 'kind') != 'self_reported':
        return _literal(row)
    pattern = _clinical_pattern(code)
    parts = re.split(r'[，,、；;。\r\n]', _answer(row))
    matched = [part.strip() for part in parts if part.strip() and pattern and re.search(pattern, part, re.I)]
    return '、'.join(matched) if matched else _literal(row)


def category_clinical_attention(data, category_code):
    """Read the same adopted A/B clues used by the fixed category projection."""
    rows = data.appendices.source_items
    from .evaluation_disease_catalog import source_catalog_family
    definitions = dict([*catalog.fixed_disease_definitions(category_code, family=source_catalog_family(data)),
        *catalog.fixed_disease_definitions(category_code)])
    return _same_source_rows([row for code in definitions
        for row in _clinical_matches(code, rows, abnormal_only=True)])


def _reported_disease_items(rows):
    """Retain explicitly named A/B diseases outside the configured directory.

    A diagnosis/history question supplies context only; its answer must still
    name a disease literally. Lab measurements, generic symptoms, goals,
    uncertainty, denial and family history cannot create a disease entry.
    """
    from .schemas import stable_id
    result = []
    disease_title = r'[\w\u4e00-\u9fff（）()·\-]{1,60}(?:病|症|炎|癌|瘤|障碍|感染|综合征|综合症)'
    for row in rows:
        kind = _get(row, 'kind')
        if kind not in {'clinical', 'self_reported'} or _goal_only_question(row):
            continue
        name = str(_get(row, 'name') or '').strip()
        explicit = bool(re.search(r'诊断|已患|患有|既往.*疾?病|疾病|病史', name)
            or str(_get(row, 'category') or '').lower() in {'diagnosis', 'disease', 'history', 'medicalhistory'})
        candidates = re.split(r'[，,、；;。\r\n]', _answer(row)) if explicit else []
        if kind == 'clinical' and re.fullmatch(disease_title, name) and re.search(r'确诊|诊断|已患|患有|病史', _answer(row)):
            candidates.append(name)
        for candidate in candidates:
            title = re.sub(r'^(?:本人|既往|曾经|曾|已确诊为|已确诊|确诊为|诊断为|患有|患|有)+[：:\s]*', '', candidate.strip())
            if (not re.fullmatch(disease_title, title) or catalog.fixed_disease_code(title)
                    or re.match(r'^(?:无|未|否认|没有|排除|不考虑)', title)
                    or re.search(r'疑似|可疑|可能|待排|未确诊|未诊断|不清楚|未知|风险|倾向', title)
                    or not _affirmative(row, pattern=re.escape(title))):
                continue
            code = (_get(row, 'code') if title == name else None) or stable_id('reportedDisease', title)
            item = public.DiseaseRiskItem(code=code, name=title, value=None,
                assessment_level='注意', explanation='风险线索：' + _literal(row),
                sources=_sources([row]), availability='partial')
            item._assessment_origin = 'report'
            if not any((old.code, old.name, old.sources) == (item.code, item.name, item.sources) for old in result):
                result.append(item)
    return result


def _complete_diseases(data, rows):
    """Fixed rows can be empty, qualitative A/B, or measured C; never fake 0."""
    from .evaluation_disease_catalog import source_catalog_family
    family = source_catalog_family(data)
    if family in {'GM', 'G1'}:
        data.introduction.description = data.introduction.description.replace('七大类疾病风险', '疾病与健康风险')
        data.disease_risk_assessment.description = catalog.source_disease_risk_description(family)
    current = {category.code: category for category in data.disease_risk_assessment.categories}
    matrices = {row.code: row for row in data.disease_risk_assessment.overview_matrix}
    data.disease_risk_assessment.categories = [current.get(code)
        or public.DiseaseRiskCategory(code=code, name=title) for code, title in catalog.RISK_CATEGORY_DEFINITIONS]
    data.disease_risk_assessment.overview_matrix = [matrices.get(code)
        or public.RiskMatrixRow(code=code, name=title) for code, title in catalog.RISK_CATEGORY_DEFINITIONS]
    categories = {category.code: category for category in data.disease_risk_assessment.categories}
    originals = [item for group in current.values() for item in group.items]
    originals.extend(data.disease_risk_assessment.risk_ranking)
    reported = _reported_disease_items(rows)
    originals.extend(reported)
    def item_stamp(item):
        return repr((item.code, item.name, type(item.value).__name__, item.value, item.unit,
            item.source_status, tuple(sorted((source.source_code or '', source.record_id or '',
                source.observed_at or '') for source in item.sources))))
    original_groups = {item_stamp(item): group.code for group in current.values() for item in group.items}
    for item in reported:
        source_row = next((row for row in rows if _sources([row]) == item.sources), None)
        original_groups.setdefault(item_stamp(item), catalog.disease_category(item.name, code=item.code,
            category=_get(source_row, 'category'), system=_get(source_row, 'system')))
    original, seen = [], set()
    for item in originals:
        # Ranking/category copies are mirrors; independent source/date/value
        # occurrences remain separate, including source-owned unknown codes.
        stamp = item_stamp(item)
        if stamp not in seen:
            seen.add(stamp)
            original.append(item)
    by_code = {}
    for item in original:
        if str(item.code or '').startswith('nativeAssociation:'):
            continue
        code = catalog.fixed_disease_code(item.code) or catalog.fixed_disease_code(item.name)
        if code and code not in by_code and (item.value is not None or item.sources):
            by_code[code] = item
    native = {item.code: item for item in original
        if str(item.code or '').startswith('nativeAssociation:')}
    for category in categories.values():
        category.name = dict(catalog.RISK_CATEGORY_DEFINITIONS)[category.code]
        reviewed_attention = public._reviewed_disease_attention_matches(data, category)
        items = []
        related = []
        clinical_labels = []
        definitions = list(catalog.fixed_disease_definitions(category.code, family=family))
        existing_codes = {identity for code, name in definitions
            for identity in (code, catalog.fixed_disease_code(name)) if identity}
        # A/B disease clues stay usable even when C's assay has no corresponding
        # model question. Candidate identities alone supply no result or score.
        definitions.extend((code, name) for code, name in catalog.fixed_disease_definitions(category.code)
            if code not in existing_codes and _clinical_matches(code, rows, abnormal_only=True))
        for code, name in definitions:
            item = native.get(code) if code.startswith('nativeAssociation:') else by_code.get(code)
            if item is None:
                item = public.DiseaseRiskItem(code=code, name=name)
            else:
                item = item.model_copy(deep=True)
            ab = _clinical_matches(code, rows, abnormal_only=True)
            related.extend(ab)
            clinical_labels.extend(_clinical_literal(code, row) for row in ab)
            # A raw C index and its origin are immutable. Only an empty fixed
            # slot can carry a distinct qualitative A/B attention judgement.
            if item.value is None and not item.sources and ab:
                item.sources = _sources(ab)
                item.assessment_level = '注意'
                item._assessment_origin = 'report'
                item.explanation = '风险线索：' + '；'.join(dict.fromkeys(_clinical_literal(code, row) for row in ab))
                item.availability = 'partial'
            from .evaluation_disease_context import attach_exact_association, attach_native_member_evidence
            if code.startswith('nativeAssociation:'):
                # Preserve the complete source scalar during composition;
                # the public callback projects the screening-result meaning.
                attach_native_member_evidence(item, data, code.split(':', 1)[1])
            else:
                attach_exact_association(item, data, code)
            items.append(item)
        for item in original:
            if not (item.value is not None or item.sources or item.source_status is not None):
                continue
            if catalog.disease_category(item.name, code=item.code,
                    category=original_groups.get(item_stamp(item))) != category.code:
                continue
            if any((item.code, item.name, item.value, item.unit, item.source_status, item.sources)
                    == (old.code, old.name, old.value, old.unit, old.source_status, old.sources)
                    for old in items):
                continue
            items.append(item.model_copy(deep=True))
        category.items = items
        for source in _sources(related):
            if source not in category.sources:
                category.sources.append(source)
        if related and not reviewed_attention:
            # Clinical/symptom attention is a separate qualitative layer;
            # a low C model index cannot erase the adopted A/B finding.
            if category.attention_level in (None, '正常', '低风险', '中低风险'):
                category.attention_level = '注意'
                category.attention_basis = '关注线索：' + '；'.join(dict.fromkeys(clinical_labels))
        actual = [item for item in items if item.value is not None or item.sources]
        from .evaluation_disease_context import native_category_basis
        native_level, native_basis, native_sources = native_category_basis(data, category.code)
        # A real association without a model index remains qualitative source
        # context. Neither an empty fixed catalog nor this later projection
        # may erase its basis, provenance or availability.
        for source in [*(source for item in actual for source in item.sources), *native_sources]:
            if source not in category.sources:
                category.sources.append(source.model_copy(deep=True))
        if native_basis and not reviewed_attention:
            if not category.attention_basis:
                category.attention_basis = native_basis
            elif native_basis not in category.attention_basis:
                category.attention_basis += '；' + native_basis
            if category.attention_level in (None, '正常', '低风险', '中低风险'):
                if native_level == '注意' or not actual and not related:
                    category.attention_level = native_level
        if actual or related or native_sources:
            category.availability = 'partial'
        else:
            category.availability = 'unavailable'
        matrix = next((item for item in data.disease_risk_assessment.overview_matrix if item.code == category.code), None)
        if matrix is not None:
            matrix.attention_level, matrix.basis = category.attention_level, category.attention_basis
            matrix.sources, matrix.availability = deepcopy(category.sources), category.availability
    aging = categories.get('aging')
    if aging is not None:
        # This template has a fixed aging assessment, not invented diseases or
        # a microbial disease-model score. Bind actual supplied dimensions.
        dimensions = [row for row in rows if _get(row, 'value') is not None
            and _get(row, 'category') != 'healthAssociation'
            and re.search(r'肠道年龄|骨密度|骨质疏松|衰老|跌倒|握力|起立|爬.{0,3}楼|记忆|认知|'
                r'体能|体力|步速|步行|自理|活动能力|\bI?ADL\b|aging|gut.?age',
                _names(row) + ' ' + str(_get(row, 'code') or ''), re.I)]
        for source in _sources(dimensions):
            if source not in aging.sources:
                aging.sources.append(source)
        if dimensions:
            aging.availability = 'partial'
        matrix = next((item for item in data.disease_risk_assessment.overview_matrix if item.code == 'aging'), None)
        if matrix is not None:
            matrix.sources, matrix.availability = deepcopy(aging.sources), aging.availability
    data.disease_risk_assessment.availability = 'partial' if any(
        group.sources or any(item.value is not None or item.sources for item in group.items)
        for group in categories.values()) else 'unavailable'


_MEDICAL_GROUPS = (
    ('obesity', '肥胖与体成分相关风险', '代谢/内分泌科'),
    ('hyperlipidemia', '血脂异常相关风险', '心血管/代谢'),
    ('nonalcoholicFattyLiver', '脂肪性肝病相关问题', '消化/肝胆'),
    ('type2Diabetes', '糖代谢相关风险', '代谢/内分泌科'),
    ('thyroidCancer', '甲状腺相关发现', '内分泌/甲状腺专科'),
    ('kidneyStones', '泌尿系结石相关发现', '泌尿系统/泌尿外科'),
    ('colorectalCancer', '消化道相关异常', '消化系统/消化内科'),
)


def medical_finding_display_name(name):
    """Clean the patient label; keep clinical qualifiers and all source facts.

    Display aliases and generated source-role parentheses are removable. A
    degree, stage, measurement or suspected nature remains outside brackets.
    The caller applies this only after original-name source binding finishes.
    """
    if not isinstance(name, str):
        return name
    def replace(match):
        content = match[1].strip()
        preserve = (re.search(r'疑似|可疑|可能|待排|待定|不除外|未确诊|已确诊|既往|病史|历史|术后|已治愈|已缓解|家族史|轻度|中度|重度|'
            r'[ⅠⅡⅢⅣIVXivx一二三四五六七八九十0-9]+[度级期]|'
            r'\d+(?:[.]\d+)?\s*(?:mm|cm|毫米|厘米|度)|TI.?RADS|BI.?RADS', content, re.I))
        return '，' + content if preserve else ''
    text = name
    while re.search(r'[（(]([^（）()]*)[）)]', text):
        text = re.sub(r'[（(]([^（）()]*)[）)]', replace, text)
    return re.sub(r'，{2,}', '，', text).strip(' ，').replace('相关相关菌群', '相关菌群')


def medical_finding_specialty(row):
    """Choose an assessment specialty from the specific item, not its lab.

    A broad disease category supplies no clinical department. In particular
    celiac disease is not routed to rheumatology simply because it is immune
    related, and an eosinophil result is not referred to its producing lab.
    """
    name = str(_get(row, 'name') or '')
    advice = str(_get(row, 'source_advice') or _get(row, 'sourceAdvice') or '')
    explicit = re.search(r'(?:儿童|小儿)(?:消化|神经|呼吸|血液|内分泌)科|'
        r'消化内科|消化科|心血管内科|心内科|内分泌科|耳鼻喉科|血液科|'
        r'精神心理科|精神科|心理科|神经内科|神经科|呼吸内科|呼吸科|'
        r'肾内科|泌尿外科|风湿免疫科|变态反应科|临床营养科|皮肤科|'
        r'儿科|眼科|骨科|妇科|产科|肿瘤科|全科门诊|综合内科|睡眠门诊', advice)
    if explicit:
        return explicit[0]
    routes = (
        (r'乳糜泻|麸质|消化不良|克罗恩|结肠炎|炎症性肠病|肠易激|便秘|排便|粪便|大便|'
            r'腹痛|腹泻|腹胀|反酸|烧心|胃胀|胃癌|结直肠|肝硬化|肝病|脂肪肝|脂肪性肝|全腹|腹部', '消化科'),
        (r'鼻部|鼻塞|喷嚏|流涕|鼻炎|鼻痒|咽|喉', '耳鼻喉科'),
        (r'嗜酸性粒细胞|缺铁性贫血|贫血|血红蛋白|血小板|血细胞', '血液科'),
        (r'甘油三酯|胆固醇|血脂|腰围|肥胖|体重指数|血糖|糖化血红蛋白|糖尿病|甲状腺|骨质疏松', '内分泌科'),
        (r'心血管|心脑血管|冠心|冠状动脉|心脏', '心血管内科'),
        (r'哮喘|呼吸道|肺|咳嗽|睡眠呼吸暂停', '呼吸科'),
        (r'精神分裂|抑郁|情绪障碍|双相|酒精使用', '精神心理科'),
        (r'孤独症|自闭症|多动症|抽动|阿尔[兹茨]海默|帕金森|认知|癫痫', '神经科'),
        (r'睡眠障碍|失眠', '睡眠门诊'),
        (r'过敏性紫癜|红斑狼疮|类风湿|强直性脊柱炎', '风湿免疫科'),
        (r'食物过敏|过敏症|过敏|湿疹', '变态反应科'),
        (r'营养不良|营养缺乏', '临床营养科'),
        (r'尿路|肾结石|泌尿|肾脏结石', '泌尿外科'),
        (r'尿蛋白|尿红细胞|尿潜血|尿白细胞|尿比重|肾功能|肌酐', '肾内科'),
        (r'视力|眼部|眼底|角膜|晶状体', '眼科'),
    )
    for pattern, specialty in routes:
        if re.search(pattern, name, re.I):
            return specialty
    # Preserve an explicit existing clinical specialty for unknown projects;
    # a source producer (lab, imaging, pathology) is not a referral destination.
    system = str(_get(row, 'system') or '')
    for specialty in re.split(r'[/／、]', system):
        if re.fullmatch(r'[^/／、]*(?:内科|外科|儿科|眼科|骨科|皮肤科|口腔科|妇科|产科|'
                r'耳鼻喉科|精神心理科|风湿免疫科|肿瘤科|康复科|门诊)', specialty) \
                and not re.search(r'检验|化验|实验|影像|放射|病理|检查', specialty):
            return specialty
    return '综合内科'


def _group_medical(data, rows):
    """Assign specialties without merging distinct abnormal measurements.

    Source values, units, ranges and original advice belong to the individual
    examination. A grouped editorial risk must not reduce the finding count.
    """
    for code, _title, system in _MEDICAL_GROUPS:
        evidence = _clinical_matches(code, rows, abnormal_only=True)
        ids = {source.record_id for source in _sources(evidence)}
        for item in data.medical_assessment.findings:
            if ids & {source.record_id for source in item.sources}:
                item.system = system
    for item in data.medical_assessment.findings:
        if item.system:
            continue
        for category, pattern in (('intestinal', r'肠|胃|便|消化'), ('metabolic', r'血脂|血糖|胰|代谢|血压'),
                ('autoimmune', r'风湿|免疫'), ('neurological', r'神经|认知|癫痫'), ('tumor', r'肿瘤|恶性|癌')):
            if re.search(pattern, str(item.name or '')):
                item.system = _SYSTEM[category]; break
    _symptom_findings(data, rows)


def _disease_attention_findings(data):
    """Show adopted positive risk results without calling them diagnoses.

    Quantitative medium/high models and positive qualitative source screens
    are distinct result lanes. The source model, range and original level
    are copied verbatim; a screen never supplies a numeric model index.
    """
    from .evaluation_disease_identity import same_source_disease
    from .evaluation_result_rules import has_risk_index
    from .evaluation_native_modules import ASSOCIATION_PREFIX, native_association_positive
    generated = ('modelRiskFinding:', 'associationFinding:')
    previous = {item.code: item for item in data.medical_assessment.findings
        if str(item.code or '').startswith(generated)}
    data.medical_assessment.findings = [item for item in data.medical_assessment.findings
        if not str(item.code or '').startswith(generated)]
    ranking = data.disease_risk_assessment.risk_ranking
    models = [item for item in ranking if has_risk_index(item) and item.sources
        and (item.source_status or item.assessment_level) in {'中风险', '中高风险', '高风险'}]
    signals = [item for item in ranking if str(item.code or '').startswith(ASSOCIATION_PREFIX)
        and item.sources and native_association_positive(item)]
    for item, certainty, prefix in [*((row, 'model_risk', generated[0]) for row in models),
            *((row, 'source_association', generated[1]) for row in signals
                if not any(same_source_disease(row.name, model.name) for model in models))]:
        code = prefix + str(item.code or item.name)
        category = next((group.code for group in data.disease_risk_assessment.categories
            if any(same_source_disease(row.name, item.name) for row in group.items)), None)
        refs = list(item.sources)
        if certainty == 'model_risk':
            for signal in signals:
                if same_source_disease(signal.name, item.name):
                    refs.extend(ref for ref in signal.sources if ref not in refs)
        label = ('（菌群模型风险提示）' if certainty == 'model_risk' else '相关菌群异常（筛查信号）')
        finding = public.MedicalFinding(code=code, name=str(item.name) + label,
            value=item.value, unit=item.unit, reference_range=item.reference_range,
            source_status=item.source_status, assessment_level=item.assessment_level,
            system={**_SYSTEM, 'allergy': '变态反应/相关专科',
                'respiratory': '呼吸系统/呼吸科', 'nutrition': '营养/临床营养科'}.get(category)
                or '相关专科', certainty=certainty,
            sources=[ref.model_copy(deep=True) for ref in refs], availability=item.availability)
        # A source projection never supplies a new AI advice or approval.
        # Retain already-composed prose only for the exact same source row.
        old = previous.get(code)
        if old is not None and all(getattr(old, field) == getattr(finding, field)
                for field in ('value', 'unit', 'reference_range', 'source_status', 'assessment_level', 'sources')):
            finding.explanation = old.explanation
            finding.evidence_refs = list(old.evidence_refs)
        data.medical_assessment.findings.append(finding)


def _symptom_findings(data, rows):
    """Actual B symptoms are clinical attention clues, never a new diagnosis."""
    for code, pattern, name in (
        ('constipation', r'便秘|排便费力|排便困难|硬便|硬块便|粪便偏硬|排便.{0,5}用力|每周排便约[012一二两]次', '排便困难/便秘症状（自述）'),
        ('reflux', r'反酸|烧心|胃食管反流', '反酸/烧心症状（自述）'),
        ('nasal', r'鼻塞|喷嚏|流涕|流鼻涕|鼻痒', '鼻塞/喷嚏等鼻部症状（自述）')):
        actual = [row for row in rows if _get(row, 'kind') == 'self_reported'
            and not _goal_only_question(row)
            and re.search(pattern, _answer(row)) and _affirmative(row, pattern=pattern)]
        if not actual:
            continue
        fid = 'reportedSymptom:' + code
        if any(item.code == fid for item in data.medical_assessment.findings):
            continue
        # Use only the symptom clauses; other lifestyle or history statements
        # from a compound answer do not become clinical findings.
        values = list(dict.fromkeys(part.strip() for row in actual
            for part in re.split(r'[，,。；;\n]', _answer(row))
            if re.search(pattern, part) and part.strip()
            and not _goal_clause(part)))
        if not values:
            continue
        data.medical_assessment.findings.append(public.MedicalFinding(code=fid, name=name,
            value='；'.join(values), source_status=None, assessment_level='注意',
            system='耳鼻喉科' if code == 'nasal' else '消化系统/消化内科',
            certainty='self_reported', sources=_sources(actual),
            availability='partial'))


_QUESTIONNAIRE = {
    'chiefComplaint': r'主诉|症状|不适|希望|改善|健康目标|chief.?complaint',
    'redFlags': r'红旗|便血|黑便|胸痛|呼吸困难|意识|不明原因体重|非主动体重|明显体重下降|吞咽困难|(?:持续|反复)?发热|持续腹泻|咯血|打鼾|日间嗜睡|red.?flags',
    'reportedDiseases': r'明确诊断|确诊|医生诊断|自报疾病|基础疾病|reported.?diseases',
    'medicalHistory': r'病史|疾病史|手术|住院|检查史|既往|胃肠镜|胃镜|肠镜|medical.?history',
    'familyHistory': r'家族|血亲|遗传|亲属|父亲|母亲|兄弟姐妹|family.?history',
    'allergies': r'过敏|不耐受|allerg|intolerance',
    'medications': r'药物|用药|服药|补充剂|抗生素|处方药|益生菌|保健品|medications?',
    'psychologicalScreening': r'心理|压力|情绪|入园适应|家庭互动|PHQ|GAD|抑郁|焦虑|psychological.?screening',
    'aging': r'衰老|跌倒|握力|起立|记忆|爬楼|认知|白发|aging',
    'healthGoals': r'健康目标|健康管理目标|目标排序|希望改善和观察|优先改善|health.?goals',
}


def _questionnaire_modules(row):
    """Match question semantics first, then an explicit structured dimension.

    A question about a relative is never the patient's diagnosis/history. An
    opaque question label can still use its supplied intake dimension without
    interpreting the answer as a new clinical finding.
    """
    label = _questionnaire_question(row) + ' ' + str(_get(row, 'code') or '')
    matched = {code for code, pattern in _QUESTIONNAIRE.items() if re.search(pattern, label, re.I)}
    if 'familyHistory' in matched:
        matched -= {'reportedDiseases', 'medicalHistory', 'chiefComplaint'}
    if re.search(r'过敏|不耐受', label):
        matched.discard('medications')
    if re.search(r'诊断|病史|血亲|家族', str(_get(row, 'name') or '')):
        matched.discard('chiefComplaint')
    if _goal_only_question(row):
        matched.discard('chiefComplaint')
    if not matched:
        category = str(_get(row, 'category') or '')
        matched = {code for code in _QUESTIONNAIRE if _key(code) == _key(category)}
    return matched


def _questionnaire_windows(question):
    return re.findall(r'(?:最近|近|过去|未来)\s*[\d一二三四五六七八九十百半两]+\s*(?:个)?(?:年|月|周|天|日)', question)


def _questionnaire_display(row):
    """Add the literal question subject to short typed answers for readers.

    The source value stays typed in the inventory and, for one short answer,
    in the summary value. This text is a display explanation, not a diagnosis.
    """
    raw = _get(row, 'value')
    value = _answer(row)
    question = _questionnaire_question(row).strip().rstrip('？?')
    short_answer = value.rstrip('。.!！').strip()
    affirmative = short_answer in {'是', '有', '以上均有'}
    negative = short_answer in {'否', '无', '没有', '以上均无', '均无', '都没有'}
    if isinstance(raw, bool) or re.fullmatch(r'true|false', value, re.I) or affirmative or negative:
        positive = raw is True or value.lower() == 'true' or affirmative
        windows = _questionnaire_windows(question)
        subject = question
        for window in windows:
            subject = subject.replace(window, '', 1)
        prefix = r'^(?:您|你|本人|患者|该儿童|儿童|孩子|目前|当前|现在|最近|近期|本次|是否|有无|有|出现|存在|已被)'
        while re.search(prefix, subject):
            subject = re.sub(prefix, '', subject, count=1)
        subject = subject.replace('是否', '').rstrip('？?')
        if re.fullmatch(r'吸烟|抽烟|smoking', subject, re.I):
            result = '吸烟' if positive else '未吸烟'
        elif subject in {'使用过抗生素', '使用抗生素'}:
            result = '使用过抗生素' if positive else '未使用抗生素'
        elif subject:
            result = ('报告有' if positive else '未报告') + (subject or '该项情况')
        else:
            # A short answer without its original question has no inferred
            # subject. Keep its exact meaning rather than guessing one.
            return value
        return ''.join(windows) + result
    smoking = re.search(r'(?:吸烟|抽烟)', question)
    if (smoking and re.search(r'支', question[smoking.end():])
            and re.fullmatch(r'[-+]?\d+(?:\.\d+)?\s*支?', value)):
        # A child's household count is not the child's own tobacco use.
        # Retain only the subject/frequency literally preceding this count.
        subject = question[:smoking.end()]
        subject = re.sub(r'^(?:(?:您|你)(?:的)?|目前|当前|现在|请问)+', '', subject)
        subject = subject.replace('是否', '').replace('有无', '')
        return subject + '：' + (value if value.endswith('支') else value + '支')
    if re.fullmatch(r'\d+(?:\.\d+)?', value) and re.search(r'药物.*种类|用药.*种', question):
        # The time/unit comes from this actual count question, not from
        # another zero-valued answer such as cigarettes or antibiotic use.
        prefix = '每日用药' if re.search(r'每天|每日', question) else '用药种类'
        return prefix + '：' + value + '种'
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        from .evaluation_builder import _trigger_answer_result
        return _trigger_answer_result(row)
    return value


def _summarize_questionnaire(data, rows):
    supplied = [row for row in rows if (_get(row, 'kind') == 'self_reported'
        or _get(row, 'kind') == 'context' and str(_get(row, 'code') or '').startswith('profile:'))
        and _answer(row)]
    result = []
    for code, name in catalog.QUESTIONNAIRE_SUMMARY_DEFINITIONS:
        actual = [row for row in supplied if code in _questionnaire_modules(row)]
        if code == 'healthGoals':
            actual = _same_source_rows([*actual, *[row for row in supplied
                if re.search(_QUESTIONNAIRE['chiefComplaint'], str(_get(row, 'name') or ''), re.I)
                and re.search(r'想(?:知道|了解|改善|调整)|希望|目标|优先改善', _answer(row))]])
        values = []
        display = []
        for row in actual:
            raw = _get(row, 'value')
            value = _answer(row)
            if isinstance(raw, (bool, int, float)):
                value = raw
            elif code in {'healthGoals', 'chiefComplaint'}:
                clauses = [part.strip() for part in re.split(r'[，,。；;\n]', value) if part.strip()]
                goal = _goal_clause
                if code == 'healthGoals':
                    value = '；'.join(part for part in clauses if goal(part)) or value
                elif any(not goal(part) for part in clauses):
                    value = '；'.join(part for part in clauses if not goal(part))
            if not any(type(value) is type(existing) and value == existing for existing in values):
                values.append(value)
            readable = _questionnaire_display(row)
            if code in {'healthGoals', 'chiefComplaint'} and isinstance(value, str):
                readable = value
            if readable and readable not in display:
                display.append(readable)
        # Different questions can all answer "否". Their display subjects
        # remain separate, while a single typed 0/false remains typed.
        joined = '；'.join(value.rstrip('。；; \n') for value in display)
        single_typed = len(actual) == 1 and isinstance(_get(actual[0], 'value'), (bool, int, float))
        semantic = any(_questionnaire_display(row) != _answer(row) for row in actual)
        result.append(public.QuestionnaireSummaryItem(code=code, name=name,
            value=values[0] if single_typed else joined or None,
            explanation=joined if single_typed and semantic else None,
            sources=_sources(actual),
            availability='available' if values else 'unavailable'))
    for code, name, pattern in (
        ('birthAndFeeding', '出生与早期喂养', r'出生孕周|出生方式|出生体重|喂养方式|纯母乳|添加辅食|断母乳|断乳'),
        ('growthAndDevelopment', '生长与发育', r'生长|身高体重变化|语言交流|游戏互动|发育')):
        actual = [row for row in supplied if re.search(pattern, str(_get(row, 'name') or ''))
            and not re.search(r'慢性疾病或发育障碍|家族|血亲', str(_get(row, 'name') or ''))]
        if actual:
            values = []
            for row in actual:
                text = _questionnaire_display(row)
                if code == 'birthAndFeeding':
                    topic = next((title for pattern, title in (
                        (r'出生孕周', '出生孕周'), (r'出生方式|出生体重', '出生方式与体重'),
                        (r'添加辅食', '辅食添加'), (r'持续时间|断母乳|断乳', '母乳喂养与断乳'),
                        (r'喂养方式', '早期喂养')) if re.search(pattern, _questionnaire_question(row))), None)
                    if topic:
                        text = topic + '：' + text
                elif code == 'growthAndDevelopment' and re.search(r'身高|体重', _questionnaire_question(row)):
                    windows = _questionnaire_windows(_questionnaire_question(row))
                    answer_window = re.search(r'[\d一二三四五六七八九十百半两]+\s*(?:个)?(?:年|月|周|天|日)|'
                        r'本月|上个月|上月|今年|去年|(?:19|20)\d{2}-\d{1,2}', text)
                    if len(windows) == 1 and not answer_window:
                        text = windows[0] + '：' + text
                if text not in values:
                    values.append(text)
            result.append(public.QuestionnaireSummaryItem(code=code, name=name,
                value='；'.join(values),
                sources=_sources(actual), availability='available'))
    # Explicit C-reported history has its own source attribution and is not
    # silently reclassified as this patient's questionnaire.
    for row in rows:
        if (_get(row, 'kind') == 'context' and _get(row, 'category') == 'sourceReportedBackground'
                or _get(row, 'kind') == 'clinical' and str(_get(row, 'category') or '').startswith('clinicalReportedBackground:')):
            result.append(public.QuestionnaireSummaryItem(code=_get(row, 'code'), name=_get(row, 'name'),
                value=_get(row, 'value'), sources=_sources([row]), availability='available'))
    data.medical_assessment.questionnaire_summary = result


def _compact_amr(data):
    rows = data.microbiome_assessment.antimicrobial_resistance
    normal = [row for row in rows if row.source_status in {'正常', '未检出', '阴性'} and row.value is not None
        and row.antibiotic_class and row.sources and row.code != 'otherNormalClasses']
    if not normal:
        return
    # Classification is native. No range or criterion for unknown rows is inferred.
    remaining = [row for row in rows if row not in normal and row.code != 'otherNormalClasses']
    names = list(dict.fromkeys(row.antibiotic_class or row.name for row in normal if row.antibiotic_class or row.name))
    states = list(dict.fromkeys(row.source_status for row in normal))
    state = '、'.join(states)
    item = public.AntimicrobialResistance(code='otherNormalClasses', name='其余类别',
        antibiotic_class='其余类别（' + '、'.join(names) + '）',
        value='；'.join((row.antibiotic_class or row.name or '') + '：' + str(row.value)
            + (' ' + row.unit if row.unit else '') for row in normal),
        source_status=state, assessment_level=state, sources=_sources(normal),
        interpretation='\n'.join(dict.fromkeys(row.interpretation for row in normal if row.interpretation)) or None,
        explanation='\n'.join(dict.fromkeys(row.explanation for row in normal if row.explanation)) or None,
        evidence_refs=list(dict.fromkeys(ref for row in normal for ref in row.evidence_refs)), availability='available')
    item._assessment_origin = 'source'
    data.microbiome_assessment.antimicrobial_resistance = [*remaining, item]


def _priority_theme(item):
    name = str(item.name or '')
    module = str(item.module or '')
    if '诱因' in module or '生活方式' in name:
        return 'behavior', '生活方式相关诱因'
    if '营养' in module or '功能' in module:
        return ('neurotransmitter', '神经递质相关功能关注') if re.search(r'神经递质|多巴胺|血清素|羟色胺|氨基丁酸', name) else ('nutrition', '营养合成与代谢功能关注')
    if '疾病风险' in module:
        return 'disease', '疾病风险重点'
    if '菌群' in module:
        return 'microbiome', '菌群结构与健康状态'
    for code, pattern, title in (
        ('occultBlood', r'隐血|便血|消化道', '消化道相关异常关注'),
        ('obesity', r'肥胖|体成分|腰围|体重指数|BMI', '肥胖与体成分相关风险'),
        ('lipids', r'血脂|胆固醇|甘油三酯', '血脂异常相关风险'),
        ('hepaticMetabolic', r'脂肪肝|糖代谢|C肽|胰岛|血糖', '肝脏与糖代谢相关风险'),
        ('thyroid', r'甲状腺', '甲状腺相关风险')):
        if re.search(pattern, name, re.I):
            return code, title
    # Important real medical problems outside the example patient's ten
    # themes are retained rather than forced into an unrelated disease.
    return 'medical:' + _key(name), name


def _symptom_value(item):
    """A short literal symptom phrase, without questionnaire scaffolding."""
    return re.sub(r'^(?:最近|近期|目前|现在|当前|还|另外|并且|而且)+', '',
        str(item.value or '').strip()).rstrip('。；;')


def _priorities(data, rows, clinical_document=None):
    previous = list(data.health_overview.key_findings)
    symptoms = [item for item in data.medical_assessment.findings
        if str(item.code or '').startswith('reportedSymptom:') and item.value is not None
        and item.certainty == 'self_reported' and item.sources
        and all(source.source_code == 'B' for source in item.sources)]
    symptom_ids = {source.record_id for source in _sources(symptoms)}
    grouped = {}
    for item in previous:
        if item.module in {'肠道菌群评估', '疾病风险评估', '营养功能评估', '诱因分析'}:
            continue
        key, name = _priority_theme(item)
        if (item.module == '医学评估' and item.sources
                and all(source.source_code == 'B' for source in item.sources)
                and symptom_ids & {source.record_id for source in item.sources}
                and re.search(r'便秘|排便|反酸|烧心|上消化道|胃肠道症状', str(item.name or ''))):
            key, name = 'digestiveSymptoms', '胃肠道症状相关关注'
        grouped.setdefault(key, {'name': name, 'items': []})['items'].append(item)
    result = []
    for group in grouped.values():
        items = group['items']
        meanings = list(dict.fromkeys(item.summary for item in items if item.summary))
        if group['name'] == '胃肠道症状相关关注' and symptoms:
            meanings = ['用户自述：' + '；'.join(dict.fromkeys(_symptom_value(item) for item in symptoms)) + '。']
        # Keep source-bound summaries, but remove generated report-audit
        # prefixes. No biomedical clause is cut out of a cited sentence.
        meanings = [text.replace('来源模型分级：', '疾病风险分级：')
            .replace('上述模型项', '上述疾病风险') for text in meanings]
        levels = list(dict.fromkeys(item.assessment_level for item in items if item.assessment_level))
        result.append(public.OverviewFinding(rank=len(result)+1, name=group['name'],
            module=items[0].module, assessment_level='；'.join(levels) or None,
            summary=' '.join(meanings) or None, sources=_sources(items),
            availability='available' if all(item.availability=='available' for item in items) else 'partial'))
    if not any(item.module == '诱因分析' for item in result):
        triggers = [row for row in data.medical_assessment.trigger_analysis if row.observations and row.sources
            and row.code in {'diet', 'exercise', 'sleep', 'stress', 'exposures'}]
        if triggers:
            result.append(public.OverviewFinding(rank=len(result)+1, name='生活方式相关诱因', module='诱因分析',
                summary='已采用的生活方式线索涉及' + '、'.join(row.name for row in triggers) + '，结合主诉及本次重点疾病风险分析其可调整方向。',
                sources=_sources(triggers), availability='partial'))
    # The same questionnaire answer may support several organized symptom
    # problems. They share one attention theme, never repeated full QA text.
    if symptoms and not any(item.name == '胃肠道症状相关关注' for item in result):
        result.append(public.OverviewFinding(rank=len(result)+1, name='胃肠道症状相关关注', module='医学评估',
            assessment_level='注意', summary='用户自述：'+'；'.join(dict.fromkeys(_symptom_value(item)for item in symptoms))+'。',
            sources=_sources(symptoms), availability='partial'))
    data.health_overview.key_findings = result
    from .evaluation_priority_focus import project_priority_focus
    project_priority_focus(data, previous=previous,
        facts={fact.fact_id: fact for fact in clinical_document.facts} if clinical_document is not None else None)


def project_fixed_assessment(data, clinical_document=None):
    """Idempotent final projection; original records remain in source_items.

    Run in the builder and after composed blocks are applied. The latter
    prevents compatibility questionnaire mirrors from restoring raw QA rows.
    """
    rows = data.appendices.source_items
    from .evaluation_native_modules import project_native_modules
    project_native_modules(data, rows)
    _complete_diseases(data, rows)
    _group_medical(data, rows)
    _disease_attention_findings(data)
    _summarize_questionnaire(data, rows)
    _compact_amr(data)
    _priorities(data, rows, clinical_document)
    _clear_unbound_default_clinical_review(data)
    return data


def _clear_unbound_default_clinical_review(data):
    """A template action is not evidence that this patient needs rechecking.

    Keep the declared step and types. Explicit plans or bound source advice
    remain unchanged; only the unavailable, unbound static default is empty.
    """
    default = next(description for code, _, description in catalog.NEXT_STEP_DEFINITIONS
        if code == 'clinicalReview')
    for step in data.comprehensive_conclusion.next_steps:
        if (step.code == 'clinicalReview' and step.availability == 'unavailable'
                and not step.sources and step.description == default):
            step.description = None
