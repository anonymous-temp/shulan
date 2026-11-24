"""Evaluate-only projection of the supplied adult/child template JSON.

These are field meanings, not invented patient results.  The supplier's AI
HTML, organism descriptions, food lists and intervention suggestions are not
patient measurements and never enter the clinical fact lanes.  JSON and PDF
adapters share this projection; neither can complete the other's patient data.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any

from pydantic import Field

from .schemas import (
    HospitalDataInput, ParsedDisease, ParsedMicrobiomeReport, ParsedTaxon,
    ReportRequest, SampleInfo, SourceMetric, SubjectInfo, stable_id,
)


# The tuple carries an actual field identity, display name, unit and optional
# original-result field.  A numeric zero is present; a missing field is absent.
STRUCTURE_FIELDS = {
    "score_sum_calculate": ("gut_overall_score", "肠道微生态总评分", None, None),
    "part1_species_num": ("species_count", "肠道菌群种类", "种", "part1_species_num_result"),
    "part1_Shannon_data1": ("diversity", "肠道菌群多样性", None, "part1_alpha_txt"),
    "pre_gutage_result": ("gut_age", "肠道年龄", "岁", None),
    "jun_gmwi2_result_score": ("gmwi2", "肠道微生物组健康指数（GMWI2）", None, "jun_gmwi2_state"),
    "jun_fb_Ratio": ("fb_ratio", "厚壁菌门/拟杆菌门（F/B）", None, "jun_result_fb"),
    "jun_be_Ratio": ("be_ratio", "肠道菌群定植抗力", None, "jun_result_be"),
    "jun_sample_result_ET": ("enterotype", "肠道菌群分型", None, None),
    "kynl": ("anti_inflammatory", "肠道抗炎能力", None, "kynl_re"),
    "myl": ("gut_immunity", "肠道免疫力", None, "myl_re"),
    # Present only when a source explicitly supplies it.  The total score is
    # not used to manufacture a missing supplier balance label.
    "balance_status": ("balance_status", "平衡状态", None, None),
}

# Registered from the four supplied field dictionaries. These identify the
# disease directory, not a sequencing platform or a patient's age.
TEMPLATE_DIRECTORY_FAMILIES = {
    "T2505211751627": "GM",
    "T250610885856": "G1",
    "T250627161148": "GS.meta",
    "T250725145643": "G1",
}

# category, original panel title, literal aggregate identity (when supplied).
TAXON_PANELS = {
    "jun_yyj": ("beneficial", "有益菌", "health_summary_beneficial"),
    "jun_zbj": ("pathogenic", "致病菌", "health_summary_pathogenic"),
    "jun_tjzbj": ("opportunistic", "条件致病菌", "health_summary_opportunistic"),
    "jun_zj": ("fungi_pathogenic", "致病性真菌", "health_summary_pathogenic_fungi"),
    "jun_bd": ("other", "病毒", "health_summary_pathogenic_viruses"),
    "jun_jsc": ("other", "致病性寄生虫", "health_summary_pathogenic_parasites"),
    "jun_ds": ("other", "产短链脂肪酸的菌", "health_summary_scfa_producers"),
    "jun_hxjs": ("other", "肠道基石菌构成", None),
    "jun_pzss": ("other", "肠道屏障损伤相关菌评估", None),
    "jun_top_adult_PHYLUM": ("other", "菌门构成", None),
    "jun_top_adult_CLASS": ("other", "菌纲构成", None),
    "jun_top_adult_ORDER": ("other", "菌目构成", None),
    "jun_top_adult_FAMILY": ("other", "菌科构成", None),
    "jun_top_adult_GENUS": ("other", "菌属构成", None),
    "jun_top_adult_SPECIES": ("other", "菌种构成", None),
    "jun_top_yichang_species": ("other", "来源异常菌种", None),
}
TAXON_PANELS.update({
    "jun_children_" + key: value for key, value in {
        "yyj": ("beneficial", "有益菌", "health_summary_beneficial"),
        "zbj": ("pathogenic", "致病菌", "health_summary_pathogenic"),
        "tjzbj": ("opportunistic", "条件致病菌", "health_summary_opportunistic"),
        "zj": ("fungi_pathogenic", "致病性真菌", "health_summary_pathogenic_fungi"),
        "bd": ("other", "致病性病毒", "health_summary_pathogenic_viruses"),
        "jsc": ("other", "致病性寄生虫", "health_summary_pathogenic_parasites"),
        "ds": ("other", "产短链脂肪酸的菌", "health_summary_scfa_producers"),
        "hxjs": ("other", "核心菌属构成", None),
        "pzss": ("other", "肠道屏障损伤相关菌评估", None),
        "fayu": ("other", "肠道菌群发育成熟度相关菌群", None),
        "shengzhang": ("other", "影响儿童生长指标相关肠道菌群", None),
    }.items()
})
for _rank, _title in (("PHYLUM", "菌门构成"), ("CLASS", "菌纲构成"),
        ("ORDER", "菌目构成"), ("FAMILY", "菌科构成"),
        ("GENUS", "菌属构成"), ("SPECIES", "菌种构成")):
    TAXON_PANELS["jun_top_children_" + _rank] = ("other", _title, None)
for _prefix in ("jun_", "jun_children_"):
    for _suffix, _title in (("top5_fungi_species", "真菌种水平构成"),
            ("top5_virus_species", "病毒构成"), ("top5_parasite_species", "寄生虫构成")):
        TAXON_PANELS[_prefix + _suffix] = ("other", _title, None)
TAXON_PANELS["jun_children_top_yichang_species"] = ("other", "来源异常菌种", None)

HEALTH_ASSOCIATION_PANELS = {
    "jkj_ganran": "胃肠道感染", "jkj_IBD": "炎症性肠病",
    "jkj_IBS": "肠易激综合征", "jkj_futong": "腹痛",
    "jkj_chronic_diarrhea": "腹泻", "jkj_fuzhangpaiqi": "腹胀排气",
    "jkj_constipation": "功能性便秘", "jkj_HF": "心血管相关",
    "jkj_fat": "肥胖症", "jkj_T2D": "Ⅱ型糖尿病", "jkj_allergy": "过敏症",
    "jkj_osteoporosis": "骨质疏松症", "jkj_yiyu": "情绪障碍",
    "jkj_jingfen": "精神分裂症", "jkj_shuimianhuxizanting": "睡眠呼吸暂停综合征",
    "jkj_aud": "酒精使用障碍", "jkj_AD": "阿尔兹海默症", "jkj_PD": "帕金森综合征",
    # These source summaries exist without an organism detail array.
    "jkj_hepatopathy": "代谢相关脂肪性肝病", "jkj_weizhang": "胃胀",
    "jkj_shuimian": "睡眠障碍",
}
HEALTH_ASSOCIATION_PANELS.update({
    "jkj_children_" + key: title for key, title in {
        "AD": "湿疹", "ADHD": "多动症", "AR": "过敏性鼻炎", "ASD": "孤独症",
        "CDD": "慢性腹泻", "CeD": "乳糜泻（麸质过敏症）", "FA": "食物过敏",
        "FC": "功能性便秘", "FD": "功能性消化不良", "HSP": "过敏性紫癜",
        "IDA": "缺铁性贫血", "RRTI": "反复呼吸道感染", "TD": "抽动障碍",
        "asthma": "哮喘", "innutrition": "营养不良", "obesity": "肥胖",
        "yiyu": "抑郁障碍",
    }.items()
})

NUTRITION_PANELS = {
    "yingyang2_tanshui2": ("碳水化合物（糖类）分解代谢", "P1"),
    "yingyang2_zhizhi2": ("脂质合成和分解代谢", "P2"),
    "yingyang2_dlzfs2": ("短链脂肪酸合成代谢", "P3"),
    "yingyang2_ajs2": ("氨基酸合成代谢", "P4"),
    "yingyang2_wss2": ("维生素合成代谢", "P5"),
    "yingyang2_sjdz2": ("神经递质合成代谢", "P6"),
    "yingyang2_hgs2": ("核苷酸合成代谢", "P7"),
    "yingyang2_trss2": ("天然色素合成代谢", "P8"),
    "yingyang2_yhdxw2": ("有害代谢物合成能力", "P9"),
    "yingyang2_dxwz2": ("毒性物质分解能力", "P10"),
}

# Literal chart components are context. They must not become a second taxon
# abundance with the chart's rounded value replacing a precise table result.
CONTEXT_FIELDS = {
    "jun_Firmicutes_dfdata": ("F/B计算所用厚壁菌门丰度", "%", None),
    "jun_Bacteroidetes_dfdata": ("F/B计算所用拟杆菌门丰度", "%", None),
    "jun_bifido_data": ("B/E计算所用双歧杆菌属丰度", "%", None),
    "jun_enter_data": ("B/E计算所用肠杆菌科丰度", "%", None),
    "zhanbi_yyj_percent": ("有益菌占比", "%", "zhanbi_yyj_result"),
    "zhanbi_yhj_percent": ("有害菌占比", "%", "zhanbi_yhj_result"),
    "zhanbi_zxj_percent": ("中性菌占比", "%", None),
}

# Explicit template meanings. In particular "sc" is gastric cancer here,
# "scz" is schizophrenia; neither abbreviation is inferred from a substring.
DISEASE_FIELDS = {
    "disease_uc": ("溃疡性结肠炎", "jkj_IBD"),
    "disease_crc": ("结直肠癌", None),
    "disease_ibs": ("肠易激综合征", "jkj_IBS"),
    "disease_sc": ("胃癌", None),
    "disease_scz": ("精神分裂症", "jkj_jingfen"),
    "disease_mdd": ("抑郁症", "jkj_yiyu"),
    "disease_dm": ("Ⅱ型糖尿病", "jkj_T2D"),
    "disease_cirrhosis": ("肝硬化", None),
}
_ADULT_EXTENDED_DISEASE_FIELDS = {
    **DISEASE_FIELDS, "disease_cd": ("克罗恩病", None),
    "disease_nafld": ("非酒精性脂肪肝", None),
}
_CHILD_DISEASE_FIELDS = {
    "disease_autism": ("孤独症", "jkj_children_ASD"),
    "disease_mdd": ("抑郁障碍", "jkj_children_yiyu"),
    "disease_cd": ("克罗恩病", None),
    "disease_uc": ("溃疡性结肠炎", None),
    "disease_ibs": ("肠易激综合征", None),
}

_BACKGROUND_FIELDS = {
    "disease": ("history", "原报告记载的病史"),
    "drug": ("medication", "原报告记载的用药"),
    "gutHealth": ("symptoms", "原报告记载的症状"),
    "shit": ("symptoms", "原报告记载的排便情况"),
    "allergyHistory": ("allergy", "原报告记载的过敏史"),
    "familyHistory": ("familyHistory", "原报告记载的家族史"),
    "diet": ("diet", "原报告记载的饮食状况"),
    "birthInfo": ("birth", "原报告记载的出生方式及体重"),
    "gestationalAge": ("birth", "原报告记载的出生孕周"),
    "feedMethod06": ("feeding", "原报告记载的0–6个月喂养方式"),
    "breastMilkDate": ("feeding", "原报告记载的纯母乳喂养时间"),
    "ablactationDate": ("feeding", "原报告记载的断母乳时间"),
    "complementaryFoodDate": ("feeding", "原报告记载的辅食添加时间"),
}


def template_directory_family(payload, template_number=None):
    if template_number in {"GM", "G1", "GS.meta"}:
        return template_number
    if template_number in TEMPLATE_DIRECTORY_FAMILIES:
        return TEMPLATE_DIRECTORY_FAMILIES[template_number]
    if any(str(key).startswith(("jun_children_", "jkj_children_", "jun_top_children_"))
            for key in payload) or "disease_autism_score" in payload:
        return "G1"
    if any(key in payload for key in ("disease_cd_score", "disease_nafld_score")):
        return "GM"
    return "GS.meta"


class EvaluationTemplateHealthMetric(SourceMetric):
    """Literal JSON result cells in the existing excluded evaluate lane."""

    source_path: str = Field(exclude=True)
    source_file: str = Field(exclude=True)
    source_checksum: str = Field(exclude=True)
    observed_at: str | None = Field(default=None, exclude=True)
    source_text: str = Field(exclude=True)


class EvaluationTemplateFunctionMetric(EvaluationTemplateHealthMetric):
    """A measured score plus its source-declared functional grouping."""

    source_section: str = Field(exclude=True)
    pathway_code: str = Field(exclude=True)


class EvaluationTemplateStructureMetric(EvaluationTemplateHealthMetric):
    """A literal structure value and separately identified method standard."""

    reference_source: str | None = Field(default=None, exclude=True)


# Static methods verified in the supplier GS.meta T250627161148 report.
# These are not patient values, and never apply to an unrecognised template.
# Page numbers are physical PDF pages in the audited 114-page source.
TEMPLATE_STRUCTURE_STANDARDS = {
    "part1_species_num": ("913–2812", 14),
    "part1_Shannon_data1": ("3.5–4", 14),
    "jun_gmwi2_result_score": (">0", 20),
    "jun_be_Ratio": (">1", 21),
    "jun_fb_Ratio": ("1.38–5.99", 22),
    "kynl": (">60", 25),
    "myl": (">60", 25),
}


def _structure_reference(payload, key, template_number):
    """Prefer an explicit result reference; otherwise use this method only."""
    for suffix in ("_referenceRange", "_reference_range", "_fanwei"):
        if value := _scalar(payload.get(key + suffix)):
            return value, key + suffix
    ranges = payload.get("referenceRanges")
    if isinstance(ranges, dict) and (value := _scalar(ranges.get(key))):
        return value, "referenceRanges." + key
    unit = str(payload.get(key + '_unit') or '').strip().casefold()
    accepted_units = ({'', '种', 'species', 'count'} if key == 'part1_species_num'
        else {'', '分', 'score'} if key in {'kynl', 'myl'} else {'', 'ratio', '比值', '无量纲'})
    methods = [str(payload.get(name) or '').strip() for name in
        ('testTechnology', 'sequencingMethod', 'detectionMethod') if payload.get(name)]
    method_matches = all(re.search(r'宏基因|metagenom|GS[.]?meta', value, re.I)
        and not re.search(r'16S', value, re.I) for value in methods)
    if (template_number == "T250627161148" and key in TEMPLATE_STRUCTURE_STANDARDS
            and unit in accepted_units and method_matches):
        value, page = TEMPLATE_STRUCTURE_STANDARDS[key]
        return value, f"template:T250627161148:standard:{key}:page:{page}"
    if (template_number in TEMPLATE_DIRECTORY_FAMILIES and key in {"kynl", "myl"}
            and unit in accepted_units):
        return ">60", f"template:{template_number}:field-meaning:{key}"
    return None, None


_EMPTY = frozenset({"", "/", "／", "--", "—", "N/A", "null", "None"})


def _scalar(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, tuple, bool)):
        return None
    if isinstance(value, str):
        return value.strip() if value.strip() not in _EMPTY else None
    if isinstance(value, (int, float, Decimal)):
        try:
            return str(value) if Decimal(str(value)).is_finite() else None
        except InvalidOperation:
            return None
    return None


def _rows(value: Any):
    return [(i, row) for i, row in enumerate(value) if isinstance(row, dict)] if isinstance(value, list) else []


def _row_scalar(row, *keys):
    return next((value for key in keys if (value := _scalar(row.get(key))) is not None), None)


def _context_scalar(value):
    return ("true" if value else "false") if isinstance(value, bool) else _scalar(value)


def _gene_list(value):
    if isinstance(value, list):
        return [text for item in value if (text := _scalar(item)) is not None]
    text = _scalar(value)
    return [item.strip() for item in re.split(r"[,，、;；]", text or "") if item.strip()]


def _negative(value: str | None) -> bool:
    return bool(value and re.fullmatch(r"未检出|未发现|阴性|未见异常", value.strip()))


def _abnormal(value: str | None) -> bool:
    return bool(value and not _negative(value) and re.search(r"异常|偏高|偏低|较低|缺乏|超标|不足|↑|↓", value))


def is_template_payload(payload: Any) -> bool:
    """Recognise named data modules, never an AI HTML-only object."""
    if not isinstance(payload, dict):
        return False
    if any(_scalar(payload.get(key)) is not None for key in STRUCTURE_FIELDS):
        return True
    if any(_scalar(payload.get(prefix + "_score")) is not None
            for prefix in {*_ADULT_EXTENDED_DISEASE_FIELDS, *_CHILD_DISEASE_FIELDS}):
        return True
    if any(any(_row_scalar(row, "chineseName", "name", "名称")
            and _row_scalar(row, "score", "value", "评分") is not None
            for _, row in _rows(payload.get(key))) for key in NUTRITION_PANELS):
        return True
    if any(any((_row_scalar(row, "taxonomy", "latinName", "拉丁学名")
                or _row_scalar(row, "chineseName", "name", "中文名", "菌名"))
            and _row_scalar(row, "abundance", "value", "resultValue", "样本含量") is not None
            for _, row in _rows(payload.get(key))) for key in (*TAXON_PANELS, *HEALTH_ASSOCIATION_PANELS)):
        return True
    if isinstance(payload.get("summary"), dict) and _scalar(payload["summary"].get("k_alpha_score")) is not None:
        return True
    return (any(_scalar(payload.get(key)) is not None for key in ("nyjy_all_result", "virulence_gene"))
        or any(_rows(payload.get(key)) for key in ("naiyao_gene_result", "virulence_gene_result")))


def _directions(value: Any) -> list[tuple[str, str]]:
    text = _scalar(value)
    result = []
    for token in re.split(r"[,，、;；\n]+", text or ""):
        if match := re.fullmatch(r"\s*(.+?)\s*([↑↓])\s*", token):
            result.append((match[1].strip(), match[2]))
    return result


def _source_sample(payload):
    """Read only existing sample fields, without substituting receipt dates."""
    nested = payload.get('sample')
    nested = nested if isinstance(nested, dict) else {}
    aliases = {
        'sample_id': 'sampleId', 'sample_type': 'sampleType', 'test_item': 'testItem',
        'sample_date': 'sampleDate', 'report_date': 'reportDate',
        'sequencing_platform': 'sequencingPlatform', 'method': 'method',
        'effective_sequences': 'effectiveSequences', 'quality_control': 'qualityControl',
    }
    values = {}
    for field, alias in aliases.items():
        value = _row_scalar(nested, alias, field)
        if field == 'report_date':
            value = _scalar(payload.get('reportDate')) or value
        elif field == 'sample_date':
            value = _scalar(payload.get('samplingDate')) or value
        if value is None:
            continue
        if field == 'effective_sequences':
            if not re.fullmatch(r'\d+', value):
                continue
            value = int(value)
        values[field] = value
    return SampleInfo(**values)


def parse_template_json(request: ReportRequest, payload: dict[str, Any], *,
                        template_number: str | None = None, checksum: str | None = None,
                        source_name: str = "microbiome-template.json") -> ParsedMicrobiomeReport:
    """Adapt actual fields into the same source DTO used by PDF evaluation.

    Field shape proves this supplier family; an unknown template number never
    causes guessed fields or a guessed patient grade. Original keys remain in
    provenance so a consumer can inspect coverage without copying raw AI text.
    """
    if not is_template_payload(payload):
        raise ValueError("template JSON does not contain supported measured modules")
    from .parsing import detection_status
    from .evaluation_source_recovery import HEALTH_SUMMARY_CODES

    checksum = checksum or hashlib.sha256(json.dumps(payload, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    source_sample = _source_sample(payload)
    report_date = source_sample.report_date
    family = template_directory_family(payload, template_number)
    disease_fields = (_CHILD_DISEASE_FIELDS if family == "G1" else
        _ADULT_EXTENDED_DISEASE_FIELDS if family == "GM" else DISEASE_FIELDS)
    if family == "GM":
        disease_fields = {**disease_fields, "disease_dm": ("2型糖尿病", "jkj_T2D")}
    metrics, taxa, summaries, supplementary, occurrences = [], [], [], [], []
    field_map: list[dict[str, Any]] = []
    used: set[str] = set()

    def used_field(path: str, lane: str, index: int):
        used.add(path.split("[", 1)[0].split(".", 1)[0])
        field_map.append({"sourcePath": path, "lane": lane, "index": index})

    def supplemental(name, value, *, path, category, section, status=None,
                     reference=None, unit=None, related=None, detail=None, **extra):
        row = HospitalDataInput(data_type="INSPECTION", item_name=name,
            test_result_value=value, report_time=report_date, reference_range=reference,
            result_status_name=status, unit=unit, sourceReportType="microbiome",
            sourceCategory=category, sourceSection=section, sourceFile=source_name,
            sourceChecksum=checksum, sourcePath=path,
            **({"sourceDescription": detail} if detail else {}),
            **({"relatedTaxa": related} if related else {}), **extra)
        row._evaluation_record_id = f"source:{checksum}:json:{path}"
        used_field(path, "supplementaryHospitalData", len(supplementary))
        supplementary.append(row)

    for key, (code, name, unit, status_key) in STRUCTURE_FIELDS.items():
        actual_key = ("pre_gutage_result_update" if key == "pre_gutage_result"
            and _scalar(payload.get("pre_gutage_result_update")) is not None else key)
        value = _scalar(payload.get(actual_key))
        if value is None:
            continue
        status = _scalar(payload.get(status_key)) if status_key else None
        if status_key and status_key in payload:
            used.add(status_key)
        if supplied_unit := _scalar(payload.get(key + '_unit')):
            unit = supplied_unit
            used.add(key + '_unit')
        used_field(actual_key, "metrics", len(metrics))
        reference, reference_source = _structure_reference(payload, key, template_number)
        if reference_source and not reference_source.startswith("template:"):
            used.add(reference_source.split(".", 1)[0])
        metrics.append(EvaluationTemplateStructureMetric(code=code, name=name, value=value, unit=unit,
            status=status, reference_range=reference, reference_source=reference_source,
            source_path=actual_key, source_file=source_name, source_checksum=checksum,
            observed_at=report_date, source_text=" | ".join((name, value, status or "/"))))

    if isinstance(payload.get("summary"), dict):
        summary = payload["summary"]
        if (value := _scalar(summary.get("k_alpha_score"))) is not None:
            status = _scalar(summary.get("k_alpha_label"))
            used_field("summary.k_alpha_score", "metrics", len(metrics))
            metrics.append(EvaluationTemplateStructureMetric(code="gut_health_index_score",
                name="肠道微生物组健康指数评分", value=value, status=status,
                source_path="summary.k_alpha_score", source_file=source_name,
                source_checksum=checksum, observed_at=report_date,
                source_text=" | ".join(("summary.k_alpha_score", value, status or "/"))))

    for key, (kind, name) in _BACKGROUND_FIELDS.items():
        if (value := _context_scalar(payload.get(key))) is not None:
            supplemental(name, value, path=key, category="sourceReportedBackground",
                section="原报告受检人资料", sourceBackgroundKind=kind)
    for key, originals, name in (
        ("mergeCustomDesc", ("diet", "drug"), "原报告记载的饮食及用药合并说明"),
        ("mergeHistoryDesc", ("allergyHistory", "familyHistory"), "原报告记载的过敏及家族史合并说明"),
        ("mergeOtherDesc", ("familyHistory", "disease"), "原报告记载的家族史及其他情况合并说明"),
    ):
        if (all(_context_scalar(payload.get(original)) is None for original in originals)
                and (value := _context_scalar(payload.get(key))) is not None):
            supplemental(name, value, path=key, category="sourceReportedBackground",
                section="原报告受检人资料", sourceBackgroundKind="combined")
    for key, name in (("ageMonths", "原报告记载的月龄"), ("height", "原报告身高"),
            ("weight", "原报告体重"), ("bmi", "原报告BMI"),
            ("d_archaea_abnormal_size", "古生菌异常检出数量"),
            ("toBeSentOutDate", "来源送样日期"), ("samplingDate", "来源采样日期"),
            ("pre_gutage_result", "肠道年龄原始评估值")):
        # Preserve a replaced raw age only when a distinct corrected value is
        # actually adopted. No undocumented vendor conversion is executed.
        if key == "pre_gutage_result" and _scalar(payload.get("pre_gutage_result_update")) is None:
            continue
        if (value := _context_scalar(payload.get(key))) is not None:
            supplemental(name, value, path=key, category="structuralAssessment", section="原报告资料")

    # Degree and original-subject age are source metadata, never a second
    # patient or an invented chronology. signDate means receipt, not sampling.
    for key, name in (("jun_gmwi2_degree", "GMWI2分度"), ("signDate", "来源收样日期")):
        if (value := _scalar(payload.get(key))) is not None:
            supplemental(name, value, path=key, category="structuralAssessment", section="肠道菌群总体评估")
    for key, (name, unit, status_key) in CONTEXT_FIELDS.items():
        if (value := _scalar(payload.get(key))) is not None:
            supplemental(name, value, path=key, category="structuralAssessment", section="肠道菌群结构图",
                unit=unit, status=_scalar(payload.get(status_key)) if status_key else None)
            if status_key and status_key in payload:
                used.add(status_key)

    taxon_index = {}
    panel_rows = {}
    for key, (category, section, summary_code) in TAXON_PANELS.items():
        if key == "jun_hxjs" and family in {"GM", "G1"}:
            section = "核心菌属构成"
        if key == "jun_top_children_ORDER" and template_number == "T250725145643":
            section = "菌群分类构成（原始层级未限定）"
        panel_rows[key] = _rows(payload.get(key))
        status = _scalar(payload.get(key + "_all_result"))
        detail = _scalar(payload.get(key + "_all"))
        used.update(k for k in (key + "_all_result", key + "_all") if k in payload)
        if status is not None:
            if summary_code:
                used_field(key + "_all_result", "evaluationPanelSummaries", len(summaries))
                summaries.append(EvaluationTemplateHealthMetric(code=summary_code, name=HEALTH_SUMMARY_CODES[summary_code],
                    value=status, status=status, source_path=key + "_all_result", source_file=source_name,
                    source_checksum=checksum, observed_at=report_date,
                    source_text=" | ".join((section, status, detail or "/"))))
            else:
                supplemental(section, status, path=key + "_all_result", category="structuralAssessment",
                    section=section, status=status, detail=detail)

    # Every panel occurrence retains its own reference/status. Identical rows
    # may share a source observation, but opposite statuses are never merged.
    all_panels = [(key, category, section) for key, (category, section, _) in TAXON_PANELS.items()]
    all_panels += [(key, "other", section) for key, section in HEALTH_ASSOCIATION_PANELS.items()]
    for key, category, section in all_panels:
        if key == "jun_hxjs" and family in {"GM", "G1"}:
            section = "核心菌属构成"
        if key == "jun_top_children_ORDER" and template_number == "T250725145643":
            section = "菌群分类构成（原始层级未限定）"
        panel_rows.setdefault(key, _rows(payload.get(key)))
        for index, row in panel_rows[key]:
            latin = _row_scalar(row, "taxonomy", "latinName", "拉丁学名")
            name = _row_scalar(row, "chineseName", "name", "中文名", "菌名")
            value = _row_scalar(row, "abundance", "value", "resultValue", "样本含量")
            if not (latin or name) or value is None:
                continue
            status = _row_scalar(row, "status", "sourceStatus", "resultStatusName", "结果", "检测结果")
            reference = _row_scalar(row, "fanwei", "referenceRange", "参考范围")
            unit = _row_scalar(row, "unit", "单位") or "%"
            path = f"{key}[{index}]"
            signature = (category, latin, name, value, reference, status) + ((unit,) if unit != "%" else ())
            if signature not in taxon_index:
                taxon_index[signature] = len(taxa)
                taxa.append(ParsedTaxon(taxon_id=stable_id("template-taxon", json.dumps(signature, ensure_ascii=False)),
                    category=category, latin_name=latin or "", chinese_name=name, value=value,
                    unit=unit,
                    reference_range=reference, source_status=status,
                    explicitly_abnormal=_abnormal(status), detection_status=detection_status(value, status=status)))
            target = taxon_index[signature]
            used_field(path, "taxa", target)
            occurrences.append({"taxonId": taxa[target].taxon_id, "latinName": latin, "chineseName": name,
                "value": value, "unit": unit, "referenceRange": reference, "sourceStatus": status,
                "detectionStatus": taxa[target].detection_status, "sourceSection": section,
                "sourcePath": path, "sourceFile": source_name, "sourceChecksum": checksum})

    for key, name in HEALTH_ASSOCIATION_PANELS.items():
        status = _scalar(payload.get(key + "_all_result"))
        if status is None:
            continue
        used.update(k for k in (key + "_all_result", key + "_all") if k in payload)
        # Association membership follows the actual abnormal-list field. A
        # screened organism's descriptive disease text supplies no association.
        related = []
        for label, direction in _directions(payload.get(key + "_all")):
            matches = [row for _, row in panel_rows.get(key, [])
                if _row_scalar(row, "chineseName", "name", "中文名", "菌名") == label
                and _row_scalar(row, "abundance", "value", "resultValue", "样本含量") is not None]
            for row in matches:
                related.append({"name": label, "scientificName": _row_scalar(row, "taxonomy", "latinName", "拉丁学名"),
                    "value": _row_scalar(row, "abundance", "value", "resultValue", "样本含量"),
                    "unit": _row_scalar(row, "unit", "单位") or "%",
                    "sourceStatus": _row_scalar(row, "status", "sourceStatus", "结果", "检测结果") or direction})
        supplemental(name, status, path=key + "_all_result", category="healthAssociation",
            section="影响健康/疾病相关菌群评估", status=status, related=related,
            detail=_scalar(payload.get(key + "_all")))

    for key, (section, pathway) in NUTRITION_PANELS.items():
        for index, row in _rows(payload.get(key)):
            name = _row_scalar(row, "chineseName", "name", "名称")
            value = _row_scalar(row, "score", "value", "评分")
            if name is None or value is None:
                continue
            path = f"{key}[{index}].score"
            status = _row_scalar(row, "status", "sourceStatus", "结果", "检测结果")
            used_field(path, "metrics", len(metrics))
            metrics.append(EvaluationTemplateFunctionMetric(code="template_function:" + path,
                name=name, value=value, unit=_row_scalar(row, "unit", "单位") or "分", status=status,
                reference_range=_row_scalar(row, "fanwei", "referenceRange", "参考范围"),
                source_path=path, source_file=source_name, source_checksum=checksum,
                observed_at=report_date, source_text=" | ".join((name, value, status or "/")),
                source_section=section, pathway_code=pathway))
            # The measured process belongs to this exact array occurrence.
            # Keep it in the existing private C context lane so snapshot
            # serialization preserves it without adding a public table row.
            if metabolism_type := _scalar(row.get('metabolism_type')):
                supplemental(name + '代谢类型', metabolism_type,
                    path=f'{key}[{index}].metabolism_type', category='structuralAssessment', section=section)
        # Aggregate nutrition statements remain context, not an extra molecule
        # or an invented group score copied onto each member.
        status = _scalar(payload.get(key + "_all_result"))
        if status is not None:
            supplemental(section, status, path=key + "_all_result", category="structuralAssessment", section=section,
                status=status, detail=_scalar(payload.get(key + "_all")))
            used.update(k for k in (key + "_all_result", key + "_all") if k in payload)

    for key, name, code, detail_key in (
        ("nyjy_all_result", "抗生素耐药基因", "health_summary_antibiotic_resistance_genes", "nyjy_all"),
        ("virulence_gene", "毒力基因", "health_summary_virulence_genes", "virulence_gene_txt_result"),
    ):
        if (status := _scalar(payload.get(key))) is None:
            continue
        used_field(key, "evaluationPanelSummaries", len(summaries))
        summaries.append(EvaluationTemplateHealthMetric(code=code, name=name, value=status, status=status,
            source_path=key, source_file=source_name, source_checksum=checksum, observed_at=report_date,
            source_text=" | ".join((name, status, _scalar(payload.get(detail_key)) or "/"))))
        if detail_key in payload:
            used.add(detail_key)

    amr_classes = []
    for index, row in _rows(payload.get("naiyao_gene_result")):
        name = _row_scalar(row, "chineseName", "name", "itemName", "中文名", "抗生素类别")
        value = _row_scalar(row, "abundance", "value", "resultValue", "样本含量")
        if name is None or value is None:
            continue
        path = f"naiyao_gene_result[{index}]"
        status = _row_scalar(row, "status", "sourceStatus", "resultStatusName", "结果", "检测结果")
        reference = _row_scalar(row, "fanwei", "referenceRange", "参考范围")
        unit = _row_scalar(row, "unit", "单位")
        classification = _row_scalar(row, "antibioticClass", "抗生素类别")
        supplemental(name, value, path=path, category="antimicrobialResistance",
            section="抗生素耐药基因", status=status, reference=reference, unit=unit,
            **({"antibioticClass": classification} if classification else {}))
        amr_classes.append(dict(itemName=name, antibioticClass=classification, value=value,
            unit=unit, status=status, reportTime=report_date,
            sourceFile=source_name, sourceChecksum=checksum))

    for index, row in _rows(payload.get("virulence_gene_result")):
        name = _row_scalar(row, "name", "chineseName", "itemName", "名称", "中文名")
        status = _row_scalar(row, "status", "sourceStatus", "result", "检测结果", "结果")
        if name is None or status is None:
            continue
        path = f"virulence_gene_result[{index}]"
        # Only explicitly detected-gene fields can supply detected genes. The
        # panel's candidate list is retained separately and never promoted.
        detected = _gene_list(next((row[key] for key in ("detectedGenes", "detected_genes", "检出毒力基因")
            if key in row), None))
        candidates = _gene_list(next((row[key] for key in ("candidateGenes", "candidate_genes", "geneList", "毒力基因列表", "list")
            if key in row), None))
        supplemental(name, status, path=path, category="virulence", section="重要毒力基因评估",
            status=status, detectedGenes=detected, candidateGenes=candidates)

    diseases = []
    from .evaluation_disease_identity import same_source_disease
    for prefix, (name, association_key) in disease_fields.items():
        value, status = _scalar(payload.get(prefix + "_score")), _scalar(payload.get(prefix + "_label"))
        if value is None:
            continue
        related = []
        if association_key and not same_source_disease(HEALTH_ASSOCIATION_PANELS.get(association_key), name):
            association_key = None
        # The source's explicit associated abnormal organisms may accompany the
        # matched disease; they never alter its model index or original grade.
        for label, direction in _directions(payload.get((association_key or "") + "_all")):
            matching = [row for _, row in panel_rows.get(association_key, [])
                if _row_scalar(row, "chineseName", "name", "中文名", "菌名") == label
                and _row_scalar(row, "abundance", "value", "resultValue", "样本含量") is not None]
            for row in matching:
                original = _row_scalar(row, "status", "sourceStatus", "结果", "检测结果")
                related.append(label + (f"（{original}）" if original else direction))
        diseases.append(ParsedDisease(disease_id=stable_id("disease", name), name=name,
            source_score=value, source_level=status, related_taxa=list(dict.fromkeys(related))))
        used_field(prefix + "_score", "diseaseResults", len(diseases) - 1)
        if prefix + "_label" in payload:
            used.add(prefix + "_label")

    # Explicit top-level request personal fields retain their existing priority.
    source_age = payload.get("age")
    source_age = (int(str(source_age).strip()) if not isinstance(source_age, bool)
        and re.fullmatch(r"\d{1,3}", str(source_age).strip()) and 0 <= int(source_age) <= 150 else None)
    report = ParsedMicrobiomeReport(source_type="microbiome_data", source_name=source_name,
        source_checksum=checksum, subject=SubjectInfo(name=request.name or _scalar(payload.get("userName")),
            age=request.age if request.age is not None else source_age, sex=request.sex or _scalar(payload.get("gender"))),
        sample=source_sample,
        metrics=metrics, taxa=taxa, disease_results=diseases,
        enterotype=_scalar(payload.get("jun_sample_result_ET")), evaluation_panel_summaries=summaries,
        evaluation_amr_classes=amr_classes,
        source_taxon_occurrences=occurrences, supplementary_hospital_data=supplementary,
        source_microbiome_terms=list(dict.fromkeys(name for row in taxa for name in (row.latin_name, row.chinese_name) if name)))
    used.update(key for key in ("userName", "age", "gender", "reportDate", "samplingDate") if key in payload)
    if isinstance(payload.get('sample'), dict) and any(value is not None for value in source_sample.model_dump().values()):
        used.add('sample')
    report._evaluation_source_audit = [{"kind": "template_json_field_mapping",
        "templateNumber": template_number, "family": family,
        "recognizedShape": "gs-meta-jun-jkj-yingyang2" if family == "GS.meta" else "template-jun-jkj-yingyang2",
        "sourceChecksum": checksum, "sourceFile": source_name, "fieldMap": field_map,
        "sourcePersonalInformation": {"name": _scalar(payload.get("userName")), "age": source_age,
            "gender": _scalar(payload.get("gender")), "ageMonths": payload.get("ageMonths")},
        "usedTopLevelFields": sorted(used), "unmappedTopLevelFields": sorted(set(payload) - used),
        "omittedNonMeasurementFields": [key for key in (
            "aiResultContent", "boolean_use_sign_img", "tenant_sign_img", "cusHighRiskOld",
            "jun_gmwi2_advice", "jun_k_be_anno", "jun_k_fb_anno", "jun_sample_result_ET_anno",
            "naiyao_gene_result_anno", "hiddenAge", "mergeCustom", "mergeHistory", "mergeOther",
            "mergeCustomDesc", "mergeHistoryDesc", "mergeOtherDesc", "report_interpretation_model",
            "report_interpretation_arr01", "report_interpretation_arr02", "report_interpretation_arr03", "report_interpretation_arr04",
        ) if key in payload and key not in used]}]
    return report
