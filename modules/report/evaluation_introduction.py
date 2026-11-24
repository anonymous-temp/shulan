"""Fixed report explanations and a factual outline of the questionnaire.

This projection describes the material, not the patient's results. It never
copies the example patient's question count, abnormality or examination advice.
"""
from __future__ import annotations

import re

from modules.report import evaluation_catalog as catalog


_QUESTION_TOPICS = (
    ("基本信息", r"姓名|性别|年龄|身高|体重|基本信息"),
    ("主诉", r"主诉|身体不适|不舒服|症状|健康困扰"),
    ("疾病史", r"疾病史|诊断|既往|手术|住院|病史"),
    ("家族史", r"家族|血亲|亲属"),
    ("过敏史", r"过敏|不耐受"),
    ("药物与补充剂", r"药物|用药|服药|抗生素|益生菌|补充剂"),
    ("饮食", r"饮食|晚餐|早餐|午餐|夜宵|饮水|喝水|蔬菜|蔬果|吃饭|食物"),
    ("运动/久坐", r"运动|锻炼|走路|步行|散步|久坐|坐着|连续坐"),
    ("睡眠/节律", r"睡眠|睡觉|熬夜|作息|入睡|几点睡|睡多(?:长|久)|睡.{0,8}(?:小时|时长)"),
    ("心理/压力", r"心理|压力|情绪|焦虑|放松|减压"),
    ("烟酒/环境", r"吸烟|抽烟|饮酒|喝酒|酒精|环境|暴露"),
    ("肠道线索", r"排便|大便|便秘|腹泻|腹胀|反酸|肠道"),
    ("衰老维度", r"记忆|认知|握力|跌倒|衰老|爬楼|起立|白发"),
    ("健康目标", r"目标|希望|改善|调整|最关心|诉求"),
)


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _text(value):
    return str(value).strip() if value is not None else ""


def _questionnaire_material(data, request=None):
    """Prefer original questions, then source inventory, with stable ordering."""
    if request is not None:
        from .evaluation_request_input import trigger_survey_answers
        native = trigger_survey_answers(request)
        if native:
            return [_text(row['question']) for row in native], len(native)
    extra = _get(request, "model_extra", {}) or {}
    if isinstance(request, dict):
        extra = request
    survey = next((extra.get(key) for key in ("healthSurvey", "questionnaire", "survey")
        if isinstance(extra.get(key), dict)), {})
    answers = survey.get("answers")
    if isinstance(answers, list):
        rows = [row for row in answers if isinstance(row, dict)
            and row.get("answer") is not None and row.get("answer") != ""]
        texts = [_text(row.get("question")) + " " + _text(row.get("intakeDimension"))
            for row in rows]
        if any(not re.search(r"家族|血亲|亲属|父亲|母亲", _text(row.get("question")))
                and re.search(r"反酸|烧心|便秘|腹泻|腹胀|腹痛|疲倦|乏力|不适|失眠", _text(row.get("answer")))
                for row in rows):
            texts.append("主诉")
        return texts, len(rows)
    # A final projection may run without the request; the internal observation
    # inventory still carries the original QA, including duplicated mirrors.
    inventory = _get(data.appendices, "source_items", []) or []
    texts, locators = [], set()
    for row in inventory:
        if not any(_get(source, "source_code") == "B" for source in row.sources):
            continue
        texts.append(_text(row.name))
        for source in row.sources:
            match = re.fullmatch(r"request\.healthSurvey\.answers\[(\d+)\]", _text(source.record_id))
            if match:
                locators.add(match[1])
    return texts, len(locators) if locators else None


def _questionnaire_dates(request):
    """Keep a single supplied collection date; never choose among conflicts."""
    if request is None:
        return None
    surveys = _get(request, 'survey_list', _get(request, 'surveyList'))
    if isinstance(surveys, list):
        candidates = surveys
    else:
        candidates = [_get(request, key) for key in ('healthSurvey', 'questionnaire', 'survey')]
    dates = []
    for survey in candidates:
        if survey is None:
            continue
        # A timestamp describes this actual questionnaire, not the patient's
        # clinical event time. Future/historical dates do not discard answers.
        date = next((_get(survey, key) for key in ('submittedAt', 'submitted_at',
            'submitTime', 'submissionTime') if _get(survey, key) not in (None, '')), None)
        if isinstance(date, str) and date.strip() and date not in dates:
            dates.append(date)
    return dates


def project_evaluation_introduction(data, request=None):
    """Use fixed explanations and describe only actual questionnaire domains."""
    data.introduction.description = catalog.INTRODUCTION_TEXT
    data.introduction.disclaimer = catalog.COVER_DISCLAIMER
    data.introduction.assessment_nature = catalog.ASSESSMENT_NATURE
    data.introduction.important_declarations = list(catalog.IMPORTANT_DECLARATIONS)
    source_b = next((source for source in data.introduction.sources if source.source_code == "B"), None)
    if source_b is None or not source_b.available:
        return data
    dates = _questionnaire_dates(request)
    if dates:
        source_b.observed_at = dates[0] if len(dates) == 1 else None
    texts, count = _questionnaire_material(data, request)
    domains = []
    for name, pattern in _QUESTION_TOPICS:
        if any(re.search(pattern, text, re.I) for text in texts):
            # The family-history question alone does not imply personal disease
            # history was collected; the family domain has its own template slot.
            if name == "疾病史" and not any(re.search(pattern, text) and not re.search(r"家族|血亲|亲属", text)
                    for text in texts):
                continue
            domains.append(name)
    if count is not None:
        source_b.answered_count = count
    count_text = f"（已回答{count}题）" if count is not None else ""
    if domains:
        source_b.content = "个性化健康评估问卷" + count_text + "：" + "、".join(domains)
    elif count is not None:
        source_b.content = "个性化健康评估问卷" + count_text
    source_b.role = next(role for code, _, role in catalog.SOURCE_DEFINITIONS if code == "B")
    return data


_LEGACY_MICROBIOME_METADATA_PREFIX = (
    "本章基于肠道菌群检测资料，评估菌群总体结构、菌群健康及短链脂肪酸合成与抗生素耐药基因。"
)


def project_fixed_report_copy(data):
    """Keep fixed copy identical in new callbacks and stored-body revalidation.

    Only report-level explanations change. Source contents, personal
    conclusions, observations and grading definitions remain independent.
    """
    from .evaluation_disease_catalog import source_catalog_family
    family = source_catalog_family(data)

    def section(camel, snake):
        return data.get(camel, data.get(snake))

    introduction = data.get('introduction')
    if isinstance(introduction, dict):
        camel = 'importantDeclarations' in introduction
        introduction.update(description=catalog.INTRODUCTION_TEXT,
            disclaimer=catalog.COVER_DISCLAIMER)
        introduction['importantDeclarations' if camel else 'important_declarations'] = list(catalog.IMPORTANT_DECLARATIONS)
        introduction['assessmentNature' if camel else 'assessment_nature'] = catalog.ASSESSMENT_NATURE
        roles = {code: role for code, _, role in catalog.SOURCE_DEFINITIONS}
        for source in introduction.get('sources', []):
            code = source.get('sourceCode', source.get('source_code'))
            if code in roles:
                source['role'] = roles[code]

    for camel, snake, text in (
        ('medicalAssessment', 'medical_assessment', catalog.MEDICAL_DESCRIPTION),
        ('diseaseRiskAssessment', 'disease_risk_assessment',
            catalog.NATIVE_DISEASE_RISK_DESCRIPTION if family else catalog.DISEASE_RISK_DESCRIPTION),
        ('functionalPathways', 'functional_pathways', catalog.PATHWAY_DESCRIPTION),
    ):
        node = section(camel, snake)
        if isinstance(node, dict) and (camel != 'medicalAssessment'
                or node.get('description') in (None, '', catalog.MEDICAL_DESCRIPTION)):
            node['description'] = text

    microbiome = section('microbiomeAssessment', 'microbiome_assessment')
    if isinstance(microbiome, dict):
        # Do not reattach old free-form method/rule suffixes at serialization.
        microbiome['description'] = catalog.MICROBIOME_DESCRIPTION

    nutrition = section('nutritionAssessment', 'nutrition_assessment')
    if isinstance(nutrition, dict):
        if family in {'GM', 'G1'}:
            nutrition['description'] = catalog.NATIVE_NUTRITION_DESCRIPTION
        elif family:
            nutrition['description'] = catalog.SOURCE_NUTRITION_DESCRIPTION
        elif nutrition.get('description') in {catalog.NEUTRAL_NUTRITION_DESCRIPTION,
                '本章用于汇总已提供的营养与菌群合成、代谢功能记录。'}:
            nutrition['description'] = catalog.NEUTRAL_NUTRITION_DESCRIPTION
        else:
            nutrition['description'] = catalog.NUTRITION_DESCRIPTION
    appendix = data.get('appendices')
    if isinstance(appendix, dict):
        key = 'referenceDescription' if 'referenceDescription' in appendix else 'reference_description'
        appendix[key] = catalog.APPENDIX_B_DESCRIPTION
    _project_result_section_descriptions(data)


def _project_result_section_descriptions(data):
    """Describe present result domains, without generating medical judgments.

    Only measured/assessed rows count. Empty fixed slots, prose and the old
    report's wording cannot declare a domain present. This is deterministic
    on the public data, so stored callbacks reproduce the same descriptions.
    """
    def section(camel, snake):
        return data.get(camel, data.get(snake, {}))

    def rows(owner, camel, snake=None):
        return owner.get(camel, owner.get(snake or camel, [])) or []

    def supplied(value):
        if isinstance(value, list):
            return any(supplied(item) for item in value)
        if not isinstance(value, dict):
            return False
        if any(value.get(key) is not None and (not isinstance(value[key], str) or value[key].strip())
                for key in ('value', 'sourceStatus', 'source_status', 'assessmentLevel', 'assessment_level')):
            return True
        return any(supplied(value.get(key)) for key in ('items', 'relatedMetrics', 'related_metrics'))

    micro = section('microbiomeAssessment', 'microbiome_assessment')
    if isinstance(micro, dict):
        domains = [title for camel, snake, title in (
            ('overallStructure', 'overall_structure', '菌群结构'),
            ('phylumComposition', 'phylum_composition', '菌群组成'),
            ('healthMetrics', 'health_metrics', '肠道相关功能'),
            ('shortChainFattyAcids', 'short_chain_fatty_acids', '短链脂肪酸相关功能'),
            ('antimicrobialResistance', 'antimicrobial_resistance', '耐药基因'))
            if supplied(rows(micro, camel, snake))]
        if domains:
            micro['description'] = '本章呈现' + '、'.join(domains) + '的结果，分别说明各项状态与主要差异。'

    nutrition = section('nutritionAssessment', 'nutrition_assessment')
    if isinstance(nutrition, dict):
        domains = []
        known_groups = dict((*catalog.DETAILED_METABOLISM_DEFINITIONS, *catalog.NUTRIENT_GROUP_DEFINITIONS))
        for camel, snake in (('detailedMetabolism', 'detailed_metabolism'),
                ('vitaminsAndMinerals', 'vitamins_and_minerals')):
            for group in rows(nutrition, camel, snake):
                if isinstance(group, dict) and supplied(group.get('items')):
                    title = group.get('name') or known_groups.get(group.get('code'), '其他营养相关功能')
                    if title not in domains:
                        domains.append(title)
        for camel, snake, title in (
                ('majorMetabolism', 'major_metabolism', '营养代谢'),
                ('aminoAcidsAndMetabolites', 'amino_acids_and_metabolites', '氨基酸及代谢物相关功能'),
                ('neurotransmitters', 'neurotransmitters', '神经递质相关功能')):
            if supplied(rows(nutrition, camel, snake)) and title not in domains:
                domains.append(title)
        if domains:
            nutrition['description'] = '本章按' + '、'.join(domains) + '呈现相关菌群功能结果，说明各类项目的状态与差异。'

    functional = section('functionalPathways', 'functional_pathways')
    if isinstance(functional, dict):
        actual = [row for row in functional.get('pathways', []) if supplied(row)]
        if actual:
            functional['description'] = f'本章呈现{len(actual)}条功能通路的相关结果，分别说明通路内各项目的状态与差异。'
