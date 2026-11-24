"""Deterministic, loss-preserving projection of actual inputs into evaluate v2.

This module performs no retrieval or model calls. Source observations are kept
in the internal inventory even when a template category is unknown. Display standards do
not become patient measurements, diagnoses, scores, or inferred dietary intake.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Iterable

from modules.report import evaluation_schema as public
from modules.report import evaluation_catalog as catalog
from modules.report.evaluation_state import populate_state_assessments
from modules.report.evaluation_result_rules import has_risk_index
from modules.report.schemas import stable_id


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python", by_alias=True, exclude_none=False)
    return {}


def _get(value: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        if isinstance(value, dict) and key in value:
            return value[key]
        if not isinstance(value, dict) and hasattr(value, key):
            return getattr(value, key)
    return default


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None and v != ""), None)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _original_medical_advice(data: dict) -> str | None:
    """Read explicit advice fields; findings/conclusions are not advice."""
    for key in ("sourceAdvice", "advice", "suggestion", "doctorAdvice", "examAdvice",
            "checkAdvice", "medicalAdvice", "recommendation"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


@dataclass(frozen=True)
class _OriginalMedicalAdviceWitness:
    """A request-derived advice record; public prose cannot recreate it."""
    name: str
    text: str
    fact_fingerprints: frozenset[tuple[str, str]]


def _advice_source_fingerprints(ids, facts):
    return frozenset((fid, hashlib.sha256(json.dumps(
        facts[fid].model_dump(mode='json', by_alias=True), ensure_ascii=False,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        for fid in ids if fid in facts)


def original_medical_advice_witnesses(document, fact_ids=()):
    """Read unchanged private source advice, never generated sourceAdvice."""
    facts = {fact.fact_id: fact for fact in getattr(document, 'facts', ())}
    allowed = set(fact_ids)
    result = []
    for witness in getattr(document, '_original_medical_advice_inventory', ()):
        if not isinstance(witness, _OriginalMedicalAdviceWitness) or not witness.text.strip():
            continue
        ids = {fid for fid, _ in witness.fact_fingerprints}
        if (ids and ids <= allowed and ids <= facts.keys()
                and witness.fact_fingerprints == _advice_source_fingerprints(ids, facts)):
            result.append(witness)
    return tuple(result)


def _seal_original_medical_advice(rows, document):
    if document is None:
        return
    facts = {fact.fact_id: fact for fact in document.facts}
    witnesses = []
    for row in rows:
        if (row.kind != 'clinical' or not row.original_medical_advice
                or not isinstance(row.description, str) or not row.description.strip()):
            continue
        ids = {source.record_id for source in row.sources if source.record_id in facts}
        if ids:
            witnesses.append(_OriginalMedicalAdviceWitness(row.name, row.description,
                _advice_source_fingerprints(ids, facts)))
    document._original_medical_advice_inventory = tuple(witnesses)


def _clinical_background_dimension(data):
    """Recognize explicit A history/intake fields, never test-name substrings.

    The observation remains clinical and keeps A references. A hospital's
    history/lifestyle section and an exact intake subject establish its role;
    exercise tests, antimicrobial susceptibility and sleep diagnoses do not.
    """
    name = _key(_first(data.get('itemName'), data.get('item_name')))
    scope = ' '.join(str(data.get(key) or '') for key in
        ('departmentName', 'department_name', 'sourceSection', 'source_section',
            'sectionTitle', 'sourceCategory'))
    if not re.search(r'病史|生活(?:方式|信息)?|问诊|个人史|健康(?:信息|访谈)|history|lifestyle|intake|clinicalReportedBackground', scope, re.I):
        return None
    for domain, labels in (
        ('medicalHistory', ('既往史', '既往病史', '既往疾病史', '疾病史', 'medical history')),
        ('familyHistory', ('家族史', '家族病史', '家族疾病史', 'family history')),
        ('medications', ('长期用药史', '用药史', '用药情况', '当前用药', '药物使用情况', '药物及补充剂使用情况', 'medication history', 'current medications')),
        ('exercise', ('运动锻炼情况', '运动习惯', '运动情况', '锻炼情况', '体力活动情况', '久坐情况', 'physical activity', 'exercise habits')),
        ('sleep', ('睡眠充足程度', '睡眠质量', '睡眠情况', '作息情况', '睡眠习惯', 'sleep habits', 'sleep quality')),
        ('exposures', ('目前的吸烟状态', '吸烟情况', '吸烟史', '当前饮酒情况', '饮酒情况', '饮酒史', 'smoking status', 'alcohol use')),
        ('diet', ('饮食习惯', '饮食情况', '饮水情况', 'dietary habits')),
        ('stress', ('心理压力', '压力情况', '情绪情况', 'stress level')),
        ('allergies', ('过敏史', '药物过敏史', 'allergy history')),
        ('foodIntolerance', ('食物不耐受史', '食物不耐受', 'food intolerance')),
    ):
        if name in {_key(label) for label in labels}:
            return domain
    return None


def _is_imaging_examination(name: str, section: str | None = None) -> bool:
    """EXAM includes vital signs and physical exams as well as imaging."""
    text = unicodedata.normalize("NFKC", str(name or ""))
    if re.search(r"(?<![A-Za-z])(?:CT|MRI|MR|DR)(?![A-Za-z])|影像|放射(?:检查|科|报告)|磁共振|核磁|X线|正侧位|超声|彩超|钼靶|骨密度", text, re.I):
        return True
    return bool(section and re.search(r"影像|放射|超声", section)
        and not re.search(r"身高|体重|腰围|血压|体格|触诊|叩诊|听诊|心电|内镜|胃镜|肠镜", text))


def _key(value: Any) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", _text(value) or "")).casefold()


def _scalar(value: Any) -> Any:
    return value if value is None or type(value) in (str, int, float, bool) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:20]


@dataclass
class _Observation:
    observation_id: str
    code: str | None
    name: str
    value: Any = None
    unit: str | None = None
    reference_range: str | None = None
    source_status: Any = None
    kind: str = "context"
    category: str | None = None
    scientific_name: str | None = None
    detection_status: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    observed_at: str | None = None
    description: str | None = None
    original_medical_advice: bool = False
    source_sections: list[str] = field(default_factory=list)
    antibiotic_classes: list[str] = field(default_factory=list)
    related_taxa: list[str] = field(default_factory=list)
    taxon_links: list[tuple[str | None, public.TaxonSignal]] = field(default_factory=list)
    cross_references: list[str] = field(default_factory=list)
    explicit_pathways: list[str] = field(default_factory=list)
    sources: list[public.SourceReference] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)
    abnormal: bool = False
    imaging: bool = False
    core_code: str | None = None
    clinical_data_type: str | None = None
    specimen: str | None = None
    source_page: int | None = None
    catalog_family: str | None = None
    source_projection_only: bool = False

    @property
    def source_code(self) -> str:
        profile = str(self.code or '').startswith('profile:') or any(
            str(fid).startswith('profile:') for fid in self.fact_ids)
        return "B" if profile or self.kind == "self_reported" else "A" if self.kind == "clinical" else "C"

    @property
    def availability(self) -> str:
        return "available" if self.value is not None else "partial" if self.source_status is not None else "unavailable"

    def reference(self, record_id: str, observed_at: str | None = None) -> None:
        candidate = public.SourceReference(source_code=self.source_code, record_id=record_id, observed_at=observed_at if observed_at is not None else self.observed_at)
        if candidate not in self.sources:
            self.sources.append(candidate)

    def metric(self, cls=public.MetricAssessment):
        # Boolean measurements have an explicit text display; the original JSON
        # boolean remains unchanged in SourceObservation.value/sourceStatus.
        value = _text(self.value) if isinstance(self.value, bool) else self.value
        native = _text(self.source_status)
        if self.source_projection_only:
            grade = native if native and native.strip() else _source_legend_grade(self)
        else:
            grade = native if native and native.strip() else (
                (_source_legend_grade(self) or _scfa_score_grade(self) or _nutrition_score_grade(self))
                if self.catalog_family in {'GM', 'G1'} else
                (_scfa_score_grade(self) or _nutrition_score_grade(self) or _source_legend_grade(self)))
        metric = cls(code=self.code or self.observation_id, name=self.name, value=value,
            unit=self.unit, reference_range=self.reference_range,
            source_status=_text(self.source_status), sources=copy.deepcopy(self.sources),
            assessment_level=grade,
            availability=self.availability)
        if grade is not None and self.source_projection_only:
            metric._assessment_origin = "source" if native and native.strip() else "reference"
        elif grade is not None:
            metric._assessment_origin = ("source" if native and native.strip()
                else "reference" if self.catalog_family in {'GM', 'G1'} and grade == _source_legend_grade(self) else "report" if (_nutrition_score_grade(self) is not None or
                grade == catalog.template_indicator_grade("shortChainFattyAcidSynthesis", self.value)
                and _alias(self, _SCFA_ALIASES)) else
                "source" if grade == _text(self.source_status)
                else "reference" if grade == _source_legend_grade(self) else "report")
        return metric

    def inventory(self) -> public.SourceObservation:
        return public.SourceObservation(observation_id=self.observation_id, code=self.code,
            name=self.name, scientific_name=self.scientific_name, value=self.value,
            unit=self.unit, reference_range=self.reference_range, source_status=self.source_status,
            kind=self.kind, category=self.category, source_type=self.source_type,
            source_name=self.source_name, observed_at=self.observed_at, description=self.description,
            related_taxa=self.related_taxa, sources=copy.deepcopy(self.sources), availability=self.availability)


def _legacy_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _source_locator(value: Any, default_identity: str | None = None) -> str | None:
    """Use the existing source-reference contract for a literal page origin."""
    from .evaluation_template_input import EvaluationTemplateHealthMetric
    from .evaluation_template_pdf import EvaluationTemplatePdfFunctionMetric

    if isinstance(value, EvaluationTemplatePdfFunctionMetric):
        path = value.source_path
        checksum = value.source_checksum
        page = value.source_page
        if (isinstance(path, str) and re.fullmatch(r'yingyang2_[A-Za-z0-9_]+\[\d+\]\.score', path)
                and isinstance(checksum, str) and re.fullmatch(r'[a-fA-F0-9]{64}', checksum)
                and type(page) is int and page > 0
                and (default_identity is None or checksum == default_identity)):
            return f'source:{checksum}:page:{page}:template:{path}'

    if isinstance(value, EvaluationTemplateHealthMetric):
        if value.source_path and value.source_checksum:
            return f"source:{value.source_checksum}:json:{value.source_path}"
        return None
    # Clinical snapshot selection legitimately rewrites private record IDs to
    # their array positions. Native JSON/PDF module provenance therefore must
    # be recovered from the mapper's retained typed-extra source metadata,
    # rather than depending on a private attribute surviving every stage.
    if isinstance(value, dict):
        path = _get(value, 'sourcePath', 'source_path', 'sourcePanelPath')
        checksum = _get(value, 'sourceChecksum', 'source_checksum')
        if (isinstance(path, str) and re.fullmatch(r'(?:jun|jkj|yingyang2)_[A-Za-z0-9_]+(?:\[\d+\](?:[.](?:score|metabolism_type))?)?', path)
                and isinstance(checksum, str) and checksum
                and (default_identity is None or checksum == default_identity)):
            page = _get(value, 'sourcePage', 'source_page')
            scope = f'page:{page}:template:' if type(page) is int and page > 0 else 'json:'
            return f'source:{checksum}:{scope}{path}'
    page = _get(value, "sourcePage", "source_page")
    if type(page) is not int or page < 1:
        return None
    identity = _first(_get(value, "sourceChecksum", "source_checksum"),
        _get(value, "sourceFile", "source_file"), default_identity)
    return f"source:{identity}:page:{page}" if identity else None


def _same_source_number(left: Any, right: Any) -> bool:
    left_number, right_number = _number(left), _number(right)
    return left_number == right_number if left_number is not None and right_number is not None else left == right


def _retain_taxon_occurrences(row: _Observation, parsed: Any) -> None:
    """Bind each printed status to its own occurrences, never the worst status."""
    occurrences = _get(parsed, "source_taxon_occurrences", "sourceTaxonOccurrences", default=[]) or []
    for index, occurrence in enumerate(occurrences):
        names = {_key(_get(occurrence, *keys)) for keys in (
            ("latinName", "latin_name"), ("chineseName", "chinese_name"), ("name",))} - {""}
        if not names & {_key(row.name), _key(row.scientific_name)}:
            continue
        status = _get(occurrence, "sourceStatus", "source_status", "status")
        if not _same_source_number(row.value, _get(occurrence, "value")) or _key(status) != _key(row.source_status):
            continue
        reference = _get(occurrence, "referenceRange", "reference_range")
        if reference and row.reference_range and _key(reference) != _key(row.reference_range):
            continue
        row.reference(f"parsed.sourceTaxonOccurrences[{index}]")
        # The native mapper owns these logical source paths. Retain the exact
        # panel membership beside the scalar taxon occurrence; identical taxa
        # in another panel are not proof of membership in this one.
        path = _get(occurrence, 'sourcePath', 'source_path', 'sourcePanelPath')
        checksum = _get(parsed, 'source_checksum', 'sourceChecksum')
        if (isinstance(path, str) and re.fullmatch(r'(?:jun|jkj)_[A-Za-z0-9_]+\[\d+\]', path)
                and checksum and _get(occurrence, 'sourceChecksum', 'source_checksum') == checksum
                and _get(occurrence, 'sourceFile', 'source_file') == _get(parsed, 'source_name', 'sourceName')):
            page = _get(occurrence, 'sourcePage', 'source_page')
            prefix = f'page:{page}:template:' if type(page) is int and page > 0 else 'json:'
            row.reference(f'source:{checksum}:{prefix}{path}')
        if locator := _source_locator(occurrence,
                _get(parsed, "source_checksum", "sourceChecksum") or _get(parsed, "source_name", "sourceName")):
            row.reference(locator)


def _gene_details(value: Any) -> str | None:
    parts = []
    for keys, label in ((("detectedGenes", "detected_genes"), "检出基因"),
                        (("candidateGenes", "candidate_genes"), "检测面板候选基因")):
        genes = _get(value, *keys, default=[]) or []
        if isinstance(genes, list) and genes:
            parts.append(label + "：" + "、".join(str(gene) for gene in genes))
    return "；".join(parts) or None


def _existing_source_annotation_alias(fact, rows):
    """Recognize an exact author-facing alias of an existing literal note.

    The fact bank retains the alias. It is not another measured observation
    and cannot change the original inventory merely because a blueprint is
    built again. Prefixes alone never remove an unrecognized source record.
    """
    fid = _get(fact, 'fact_id', 'factId')
    if (_get(fact, 'kind') != 'context' or not isinstance(fid, str)
            or not fid.startswith('source-annotation-')
            or any(_get(fact, key) is not None for key in ('unit', 'reference_range', 'status'))):
        return False
    for row in rows:
        if row.source_code != 'C' or not isinstance(row.description, str) or not row.description:
            continue
        if (_get(fact, 'name') != (row.name or '源面板') + '原始方向注解'
                or _get(fact, 'value') != row.description):
            continue
        for source in row.sources:
            if not source.record_id or source.source_code != 'C':
                continue
            expected = 'source-annotation-' + hashlib.sha256(json.dumps(
                [source.record_id, source.observed_at, row.description],
                ensure_ascii=False).encode()).hexdigest()[:24]
            if (fid == expected and _get(fact, 'date') == (source.observed_at or row.observed_at)
                    and _get(fact, 'source') == f'{row.source_name or "C"}:{source.record_id}#description'):
                return True
    return False


def _source_observations(request: Any, parsed: Any, clinical_document: Any, *, source_only=False) -> list[_Observation]:
    rows: list[_Observation] = []
    source_name = _get(parsed, "source_name", "sourceName")
    source_type = _get(parsed, "source_type", "sourceType")
    sample = _get(parsed, "sample", default={})
    source_date = _get(sample, "report_date", "reportDate")

    for kind, collection, name_fields, value_fields in (
        ("metric", "metrics", ("name",), ("value",)),
        ("taxon", "taxa", ("chinese_name", "chineseName", "latin_name", "latinName"), ("value",)),
        ("disease", "disease_results", ("name",), ("source_score", "sourceScore")),
    ):
        values = _get(parsed, collection, "diseaseResults" if collection == "disease_results" else collection, default=[]) or []
        for index, item in enumerate(values):
            name = _first(*(_get(item, key) for key in name_fields))
            if name is None:
                continue
            code = _get(item, "code", "taxon_id", "taxonId", "disease_id", "diseaseId")
            value = _first(*(_get(item, key) for key in value_fields))
            status = _first(_get(item, "source_level", "sourceLevel"), _get(item, "source_status", "sourceStatus"), _get(item, "status"))
            row = _Observation(observation_id=f"{collection}:{index}:{_hash([code, name, value])}",
                code=code, name=str(name), value=_scalar(value), kind=kind,
                scientific_name=_get(item, "latin_name", "latinName"),
                category=_get(item, "category") or _get(item, "system"), detection_status=_get(item, "detection_status", "detectionStatus"),
                unit=_get(item, "unit"), reference_range=_get(item, "reference_range", "referenceRange"),
                source_status=status, source_type=source_type, source_name=source_name,
                observed_at=source_date, description=_get(item, "source_description", "sourceDescription"),
                related_taxa=list(_get(item, "related_taxa", "relatedTaxa", default=[]) or []),
                abnormal=_get(item, "explicitly_abnormal", "explicitlyAbnormal", default=False) is True)
            row.reference(f"parsed.{collection}[{index}]")
            from .evaluation_template_input import EvaluationTemplateFunctionMetric
            from .evaluation_template_input import EvaluationTemplateStructureMetric
            from .evaluation_template_pdf import EvaluationTemplatePdfFunctionMetric, EvaluationTemplatePdfStructureMetric
            if isinstance(item, (EvaluationTemplateStructureMetric, EvaluationTemplatePdfStructureMetric)):
                if locator := _source_locator(item):
                    row.reference(locator)
                if item.reference_source:
                    if item.reference_source.startswith('template:'):
                        row.sources.append(public.SourceReference(source_code='C', record_id=item.reference_source))
                    else:
                        row.reference(item.reference_source if item.reference_source.startswith('source:')
                            else f'source:{item.source_checksum}:json:{item.reference_source}')
            if isinstance(item, (EvaluationTemplateFunctionMetric, EvaluationTemplatePdfFunctionMetric)):
                row.source_sections = [item.source_section]
                row.explicit_pathways = [item.pathway_code]
                if locator := _source_locator(item):
                    row.reference(locator)
            if kind == "taxon":
                _retain_taxon_occurrences(row, parsed)
            if kind in {"taxon", "disease"} and code:
                row.fact_ids.append(code)
            elif kind == "metric":
                row.fact_ids.append(stable_id("function-fact", "|".join(map(_legacy_text, (code, value, row.unit, source_date)))))
            rows.append(row)

    # Only evaluate consumes these original category-heading ratios. They do
    # not enter the shared scoring/legacy metric lane or become detected counts.
    panel_summaries = _get(parsed, "evaluation_panel_summaries", "evaluationPanelSummaries", default=[]) or []
    panel_codes = {"panel_title_beneficial", "panel_title_pathogenic",
        "panel_title_opportunistic", "panel_title_other"}
    grouped_panels = {}
    for index, item in enumerate(panel_summaries):
        code = _get(item, "code")
        if code in panel_codes:
            grouped_panels.setdefault(code, []).append((index, item))
    for code, candidates in grouped_panels.items():
        identities = {(_get(item, "name"), _get(item, "value"), _get(item, "unit"),
            _get(item, "status", "source_status", "sourceStatus"), _get(item, "reference_range", "referenceRange"))
            for _, item in candidates}
        if len(identities) != 1:
            continue  # Conflicting same-code headings are never selected by order.
        index, item = candidates[0]
        row = _Observation(observation_id=f"evaluationPanelSummaries:{code}", code=code,
            name=_get(item, "name"), value=_get(item, "value"), kind="metric",
            unit=_get(item, "unit"), reference_range=_get(item, "reference_range", "referenceRange"),
            source_status=_get(item, "status", "source_status", "sourceStatus"), source_type=source_type,
            source_name=source_name, observed_at=source_date)
        for index, _ in candidates:
            row.reference(f"parsed.evaluationPanelSummaries[{index}]")
        rows.append(row)

    # Literal overview result cells are distinct from panel-heading ratios.
    # Keep disagreeing printed statuses independent and merge identical cells
    # only while retaining every actual page locator. No shared scalar lane
    # or legacy clinical fact is changed by this evaluate-only recovery.
    from modules.report.evaluation_source_recovery import HEALTH_SUMMARY_CODES, EvaluationHealthSummaryMetric
    from .evaluation_template_input import EvaluationTemplateHealthMetric
    health_summaries = {}
    for index, item in enumerate(panel_summaries):
        code = _get(item, "code")
        if (code not in HEALTH_SUMMARY_CODES or not isinstance(item, (EvaluationHealthSummaryMetric, EvaluationTemplateHealthMetric))
                or not _source_locator(item)):
            continue
        identity = (code, _get(item, "value"), _get(item, "status"),
            _get(item, "unit"), _get(item, "reference_range", "referenceRange"),
            _get(item, "observed_at", "observedAt") or source_date,
            item.source_file, item.source_checksum)
        health_summaries.setdefault(identity, []).append((index, item))
    for identity, candidates in health_summaries.items():
        code, value, status, unit, reference, date, file_name, checksum = identity
        row = _Observation(observation_id="evaluationHealthSummary:" + _hash(identity),
            code=code, name=HEALTH_SUMMARY_CODES[code], value=value, kind="context",
            unit=unit, reference_range=reference, source_status=status,
            source_type=source_type, source_name=file_name, observed_at=date)
        for index, item in candidates:
            row.reference(f"parsed.evaluationPanelSummaries[{index}]")
            if locator := _source_locator(item):
                row.reference(locator)
        rows.append(row)

    # The pipeline already appends supplementary rows to request.hospitalData.
    # Merge only repetitions across these two projections. Identical input rows
    # inside the same array remain separate observations with separate locators.
    # Use the exact provenance/term classifier used to create clinical facts.
    # In particular, genus-only and source terminology rows are context, not
    # nutritional functions, and must bind to their existing context fact IDs.
    from modules.report.clinical_report import _hospital_row_classifier
    classify_hospital = _hospital_row_classifier(parsed)
    request_signatures: dict[str, list[_Observation]] = {}
    for origin, values in (
        ("request.hospitalData", _get(request, "hospital_data", "hospitalData", default=[]) or []),
        ("parsed.supplementaryHospitalData", _get(parsed, "supplementary_hospital_data", "supplementaryHospitalData", default=[]) or []),
    ):
        matched: set[str] = set()
        for index, item in enumerate(values):
            path = getattr(item, "_evaluation_record_id", None) or f"{origin}[{index}]"
            data = _mapping(item)
            name = _get(item, "item_name", "itemName")
            if name is None:
                continue
            is_exam = _get(item, "data_type", "dataType") == "EXAM"
            section = _first(data.get("sourceSection"), data.get("source_section"))
            imaging = is_exam and _is_imaging_examination(str(name), _text(section))
            value = _first(_get(item, "exam_result", "examResult") if is_exam else _get(item, "test_result_value", "testResultValue"), _get(item, "exam_result", "examResult"))
            unit = _first(data.get("unit"), data.get("testResultUnit"))
            date = _first(_get(item, "report_time", "reportTime"), _get(item, "check_time", "checkTime"))
            status = _first(_get(item, "result_status_name", "resultStatusName"), _get(item, "result_status", "resultStatus"))
            file_name = _first(data.get("sourceFile"), data.get("source_file"), data.get("sourceName"))
            report_type = _first(data.get("sourceReportType"), data.get("source_report_type"))
            kind = classify_hospital(item)
            background = (_clinical_background_dimension(data) if kind == 'clinical'
                and value is not None and (not isinstance(value, str) or value.strip()) else None)
            row = _Observation(observation_id=f"hospital:{path}:{_hash(data)}", code=str(name),
                name=str(name), value=_scalar(value), unit=_text(unit),
                reference_range=_get(item, "reference_range", "referenceRange"), source_status=status,
                category=('clinicalReportedBackground:' + background if background else
                    _text(_first(data.get("category"), data.get("sourceCategory"), data.get("assessmentType")))),
                kind=kind, source_name=_text(file_name) or "院内临床记录" if kind == "clinical" else _text(file_name) or source_name,
                source_type=_text(report_type) or "hospital_data", observed_at=_text(date),
                description=(_text(data.get('sourceDescription')) if kind == 'context'
                    else _original_medical_advice(data)),
                original_medical_advice=bool(kind == 'clinical' and _original_medical_advice(data)),
                source_sections=[str(section)] if (section := _first(data.get("sourceSection"), data.get("source_section"))) else [],
                clinical_data_type=_get(item, "data_type", "dataType"),
                specimen=_text(_first(*(data.get(key) for key in
                    ("specimen", "specimenType", "sampleType", "sampleName", "specimen_type", "sample_type")))),
                source_page=data.get("sourcePage") if type(data.get("sourcePage")) is int else None,
                imaging=imaging, abnormal=_status_state(status) == "abnormal")
            signature = _hash([_get(item, "data_type", "dataType"), row.name, row.value, row.unit,
                row.reference_range, row.source_status, _get(item, "report_time", "reportTime"),
                _get(item, "check_time", "checkTime"), row.source_name, row.source_type, row.kind, row.specimen,
                row.category, row.source_page])
            if origin.startswith("parsed."):
                existing = next((r for r in request_signatures.get(signature, []) if r.observation_id not in matched), None)
                if existing is not None:
                    matched.add(existing.observation_id)
                    existing.reference(path)
                    for duplicate_path in getattr(item, "_evaluation_duplicate_record_ids", []) or []:
                        existing.reference(duplicate_path)
                    existing.source_sections = list(dict.fromkeys([*existing.source_sections, *row.source_sections]))
                    if row.description and not existing.description:
                        existing.description = row.description
                    _retain_function_links(existing, data, path)
                    continue
            row.reference(path)
            for duplicate_path in getattr(item, "_evaluation_duplicate_record_ids", []) or []:
                row.reference(duplicate_path)
            if locator := _source_locator(data, _get(parsed, 'source_checksum', 'sourceChecksum')):
                row.reference(locator)
            if row.category == "virulence":
                row.description = _gene_details(data)
            _retain_function_links(row, data, path)
            check_time = _get(item, "check_time", "checkTime")
            if check_time is not None and check_time != date:
                row.reference(path + ".checkTime", _text(check_time))
            fact_key = stable_id("clinical-fact", "|".join(map(_legacy_text, (name, value, unit, date, row.reference_range, status))))
            row.fact_ids.append(fact_key if kind == "clinical" else stable_id("auxiliary-fact", f"{kind}|{fact_key}"))
            rows.append(row)
            if origin.startswith("request."):
                request_signatures.setdefault(signature, []).append(row)

    # Compatibility input sections not covered by the current Parsed DTO.
    # Canonical metrics/taxa/diseaseRisks are deliberately NOT parsed twice.
    payload = _get(request, "microbiome_data", "microbiomeData") or {}
    for key in ("nutrition", "nutritionData", "nutrients", "functionalData", "functionalMetrics", "functionMetrics", "metabolism", "antimicrobialResistance", "antibioticResistance", "resistanceGenes"):
        if isinstance(payload, dict) and key in payload:
            _extra_observations(payload[key], f"request.microbiomeData.{key}", key, rows, source_name, source_date)
    # Canonical parsing intentionally has no relationship fields. Reconcile
    # only explicitly supplied metadata, without parsing the metric array twice.
    if isinstance(payload, dict) and isinstance(payload.get("metrics"), list):
        for index, item in enumerate(payload["metrics"]):
            if isinstance(item, dict) and any(key in item for key in _FUNCTION_LINK_KEYS):
                _extra_observations(item, f"request.microbiomeData.metrics[{index}]", "metrics", rows, source_name, source_date)
    for key in ("healthSurvey", "questionnaire", "survey"):
        supplied = _get(request, key)
        if supplied not in (None, {}, [], ""):
            _extra_observations(supplied, f"request.{key}", key, rows, "用户填写问卷", None,
                questionnaire=True, canonical_path=f"request.{key}")
    reported = _get(_get(clinical_document, "background", default={}), "patientReported", default={})
    if isinstance(reported, dict):
        for key in ("answers", "answer", "qaHistory", "symptoms", "symptom", "lifestyle", "diet", "exercise", "sleep", "bowel"):
            if key in reported:
                _extra_observations(reported[key], f"clinicalDocument.background.patientReported.{key}", key,
                    rows, "用户问答记录", None, questionnaire=True,
                    request_survey=_get(request, "healthSurvey"))

    # Bind facts after all compatibility sections have been collected. Their
    # original paths can also be model-visible fact IDs; never project them twice.
    for index, fact in enumerate(_get(clinical_document, "facts", default=[]) or []):
        fact_id = _get(fact, "fact_id", "factId")
        if (_get(fact, 'kind') == 'context' and isinstance(fact_id, str)
                and fact_id.startswith('fmt-typing-context-')):
            # A matching plan remains available to downstream planning, but
            # does not establish a microbiome test or a patient observation.
            continue
        if (_get(fact, "kind") in {"microbe", "context"} and isinstance(fact_id, str)
                and re.fullmatch(r"parsed\.evaluationPanelSummaries\[\d+\]\.taxa\[\d+\](?:\.association)?", fact_id)):
            # These measured rows and their raw +/- signs belong to a single
            # disease card. Keep the facts for that card's local evidence, not
            # as another organism occurrence in the global health panels.
            continue
        if source_only and _existing_source_annotation_alias(fact, rows):
            continue
        matches = [row for row in rows if fact_id and (fact_id in row.fact_ids
            or any(source.record_id == fact_id for source in row.sources))]
        if len(matches) > 1:
            # Duplicate named disease rows can share a legacy ID. A single
            # model-visible fact cannot prove a different index under that ID.
            exact = [row for row in matches if _text(row.value) == _text(_get(fact, "value"))]
            if exact:
                matches = exact
        if matches:
            for row in matches:
                if fact_id not in row.fact_ids:
                    row.fact_ids.append(fact_id)
                row.reference(fact_id)
                row.reference(f"clinicalDocument.facts[{index}]")
                # The old collector derives abnormality using a substring
                # search, which also catches '未见异常'. Keep the actual label.
                complete_negative = (row.source_status is None or isinstance(row.source_status, str)
                    and not row.source_status.strip()) and _normal_clinical_result(row)
                if _status_state(row.source_status) != "normal" and not _normal_imaging_result(row) and not complete_negative:
                    row.abnormal = row.abnormal or _get(fact, "abnormal", default=False) is True
            continue
        kind = _get(fact, "kind", default="context")
        status = _get(fact, "status")
        row = _Observation(observation_id=f"fact:{index}:{fact_id}", code=fact_id,
            name=_get(fact, "name", default=""), value=_get(fact, "value"), kind=kind,
            unit=_get(fact, "unit"), reference_range=_get(fact, "reference_range", "referenceRange"),
            source_status=status, source_name=_get(fact, "source"),
            source_type="clinical_document", observed_at=_get(fact, "date"),
            abnormal=_get(fact, "abnormal", default=False) is True and _status_state(status) != "normal",
            imaging=_get(fact, "imaging", default=False) is True)
        row.reference(fact_id or f"clinicalDocument.facts[{index}]")
        rows.append(row)
    _apply_source_semantics(rows)
    return _merge_source_health_summaries(rows)


def _merge_source_health_summaries(rows: list[_Observation]) -> list[_Observation]:
    """Attach verified overview origins without replacing existing results.

    A categorical row with the same literal result already expresses that
    overview state. A numeric core row may express it through its own printed
    status (e.g. 73 / 正常). Keep every original value, status, unit and date;
    the overview contributes provenance only in those two exact cases.
    """
    from modules.report.evaluation_source_recovery import HEALTH_SUMMARY_CODES
    summary_rows = [row for row in rows if row.code in HEALTH_SUMMARY_CODES
        and row.observation_id.startswith("evaluationHealthSummary:")]
    retired = set()
    for summary in summary_rows:
        core = _alias(summary, _HEALTH_ALIASES)
        if not core:
            continue
        summary_hashes = {match[1] for source in summary.sources if (match := re.fullmatch(
            r"source:([a-fA-F0-9]{64}):page:\d+", source.record_id or ""))}
        matches = []
        for row in rows:
            if (row in summary_rows or row.kind not in {"metric", "function", "context"}
                    or _alias(row, _HEALTH_ALIASES) != core or row.source_name != summary.source_name
                    or _is_amr(row)):
                continue
            if row.observed_at and summary.observed_at and row.observed_at[:10] != summary.observed_at[:10]:
                continue
            hashes = {match[1] for source in row.sources if (match := re.fullmatch(
                r"source:([a-fA-F0-9]{64}):page:\d+", source.record_id or ""))}
            if hashes and summary_hashes and hashes != summary_hashes:
                continue
            literal = (_text(row.value) == _text(summary.value) and
                (row.source_status is None or _text(row.source_status) == _text(summary.source_status)))
            numeric_status = (not isinstance(row.value, bool) and _number(row.value) is not None
                and _text(row.source_status) == _text(summary.value))
            if literal or numeric_status:
                matches.append(row)
        if matches:
            for row in matches:
                row.sources.extend(source.model_copy(deep=True) for source in summary.sources if source not in row.sources)
            retired.add(summary.observation_id)
    return [row for row in rows if row.observation_id not in retired]


_FUNCTION_LINK_KEYS = {"relatedTaxa", "related_taxa", "crossReferences", "cross_references",
    "pathwayCode", "pathway_code", "pathwayCodes", "pathway_codes"}


def _retain_function_links(row: _Observation, value: dict, path: str) -> None:
    """Retain explicit associations; names alone never imply a function link."""
    explicit_class = _first(value.get("antibioticClass"), value.get("antibiotic_class"))
    if isinstance(explicit_class, str) and explicit_class.strip() and explicit_class not in row.antibiotic_classes:
        row.antibiotic_classes.append(explicit_class)
    for key in ("relatedTaxa", "related_taxa"):
        supplied = value.get(key)
        items = supplied if isinstance(supplied, list) else [supplied]
        for index, item in enumerate(items):
            taxon_id = None
            if isinstance(item, str) and item.strip():
                signal = public.TaxonSignal(name=item)
            elif isinstance(item, dict):
                taxon_id = _text(_first(item.get("taxonId"), item.get("taxon_id"), item.get("code"), item.get("id")))
                scientific = _text(_first(item.get("scientificName"), item.get("scientific_name"), item.get("latinName"), item.get("latin_name")))
                name = _text(_first(item.get("name"), item.get("chineseName"), item.get("chinese_name"), scientific, taxon_id))
                if not name:
                    continue
                measurement = item.get("value")
                # Related signals use the public scalar display contract; do
                # not stringify arbitrary nested objects into measurements.
                measurement = _text(measurement) if isinstance(measurement, bool) else measurement
                if measurement is not None and type(measurement) not in (str, int, float):
                    measurement = None
                signal = public.TaxonSignal(name=name, scientific_name=scientific, value=measurement,
                    unit=_text(item.get("unit")), source_status=_text(_first(item.get("sourceStatus"), item.get("source_status"), item.get("status"), item.get("direction"))))
            else:
                continue
            record_id = f"{path}.{key}[{index}]" if isinstance(supplied, list) else f"{path}.{key}"
            signal.sources = [public.SourceReference(source_code=row.source_code, record_id=record_id, observed_at=row.observed_at)]
            if signal.name not in row.related_taxa:
                row.related_taxa.append(signal.name)
            if (taxon_id, signal) not in row.taxon_links:
                row.taxon_links.append((taxon_id, signal))
    for key in ("crossReferences", "cross_references"):
        supplied = value.get(key)
        for item in supplied if isinstance(supplied, list) else [supplied]:
            if isinstance(item, str) and item.strip() and item not in row.cross_references:
                row.cross_references.append(item)
    for key in ("pathwayCode", "pathway_code", "pathwayCodes", "pathway_codes"):
        supplied = value.get(key)
        for item in supplied if isinstance(supplied, list) else [supplied]:
            if isinstance(item, str) and re.fullmatch(r"P(?:[1-9]|10)", item, re.IGNORECASE):
                code = item.upper()
                if code not in row.explicit_pathways:
                    row.explicit_pathways.append(code)


def _project_function_links(target: public.FunctionalPathway, row: _Observation, rows: list[_Observation]) -> None:
    for taxon_id, supplied in row.taxon_links:
        names = {_key(name) for name in (supplied.name, supplied.scientific_name) if name}
        matching = [taxon for taxon in rows if taxon.kind in {"taxon", "microbe"}
            and (taxon.source_name, taxon.observed_at) == (row.source_name, row.observed_at)
            and (taxon.code == taxon_id if taxon_id else bool(names & {_key(taxon.name), _key(taxon.scientific_name)}))
            and (not taxon_id or supplied.name == taxon_id or bool(names & {_key(taxon.name), _key(taxon.scientific_name)}))]
        signals = []
        for taxon in matching:
            actual = public.TaxonSignal(name=taxon.name, scientific_name=taxon.scientific_name,
                value=_text(taxon.value) if isinstance(taxon.value, bool) else taxon.value,
                unit=taxon.unit, source_status=_text(taxon.source_status), sources=copy.deepcopy(taxon.sources))
            # An explicit link can itself carry a distinct measurement. Keep
            # that value rather than silently overwriting it with Parsed data.
            status_conflict = (supplied.source_status is not None and supplied.source_status != actual.source_status
                and not (_taxon_direction(supplied.source_status)
                    and _taxon_direction(supplied.source_status) == _taxon_direction(actual.source_status)))
            if any(provided is not None and provided != measured for provided, measured in (
                    (supplied.value, actual.value), (supplied.unit, actual.unit))) or status_conflict:
                continue
            actual.sources.extend(s.model_copy(deep=True) for s in supplied.sources if s not in actual.sources)
            signals.append(actual)
        for signal in signals or [supplied.model_copy(deep=True)]:
            _merge_taxon_signal(target.related_taxa, signal)
    target.cross_references.extend(ref for ref in row.cross_references if ref not in target.cross_references)


def _same_source_value(first, second):
    """Exact mirrored input equality, including JSON false versus numeric zero."""
    if type(first) is not type(second):
        return False
    if isinstance(first, dict):
        return first.keys() == second.keys() and all(_same_source_value(first[key], second[key]) for key in first)
    if isinstance(first, list):
        return len(first) == len(second) and all(_same_source_value(a, b) for a, b in zip(first, second))
    return first == second


def _extra_observations(value, path, name, rows, source_name, source_date, *, questionnaire=False,
                        canonical_path=None, request_survey=None):
    if questionnaire and value is None:
        # An absent container is not an unanswered, explicitly named question.
        return
    if (questionnaire and canonical_path is None
            and path.startswith("clinicalDocument.background.patientReported.answers[") and isinstance(value, dict)
            and set(value) == {"healthSurvey"} and request_survey not in (None, {}, [], "")
            and _same_source_value(value["healthSurvey"], request_survey)):
        # The pipeline copies this exact request object into clinical answers.
        # Other conversation records, even same-valued QAs, stay independent.
        canonical_path = "request"
    if isinstance(value, list):
        for index, item in enumerate(value):
            _extra_observations(item, f"{path}[{index}]", name, rows, source_name, source_date,
                questionnaire=questionnaire,
                canonical_path=f"{canonical_path}[{index}]" if canonical_path is not None else None,
                request_survey=request_survey)
        return
    if isinstance(value, dict):
        if questionnaire:
            source_date = _text(_first(value.get("submittedAt"), value.get("submitTime"), value.get("submissionTime"), source_date))
        value_keys = ("answer", "answerValue", "value", "result", "resultValue", "score") if questionnaire else ("value", "result", "resultValue", "score")
        supplied_key = next((key for key in value_keys if key in value), None)
        if supplied_key is None and questionnaire and any(key in value for key in ("question", "questionId", "questionText")):
            # An unanswered question is an unknown answer, not a patient claim.
            value = {**value, "answer": None}
            supplied_key = "answer"
        if supplied_key is None and not questionnaire and any(key in value for key in _FUNCTION_LINK_KEYS):
            # A source may provide a pathway relationship without a measured
            # score. Preserve that relationship with an unknown measurement.
            value = {**value, "value": None}
            supplied_key = "value"
        if supplied_key is None:
            for key, item in value.items():
                if key not in {"userId", "sessionId", "name", "age", "sex", "gender", "submittedAt", "submitTime", "version", "questionCount", "answeredCount"}:
                    _extra_observations(item, f"{path}.{key}", key, rows, source_name, source_date,
                        questionnaire=questionnaire,
                        canonical_path=f"{canonical_path}.{key}" if canonical_path is not None else None,
                        request_survey=request_survey)
            return
        name = _first(value.get("name"), value.get("itemName"), value.get("question"), value.get("questionText"), value.get("title"), value.get("code"), name)
        scalar = value[supplied_key]
        code = _first(value.get("code"), value.get("id"), value.get("questionId"), name)
        status = _first(value.get("status"), value.get("sourceStatus"))
        unit, reference = value.get("unit"), value.get("referenceRange")
        category = _first(value.get("category"), value.get("sourceCategory"), value.get("assessmentType"))
    else:
        scalar, code, status, unit, reference = value, name, None, None, None
        category = None
    scalar = _scalar(scalar)
    if questionnaire and canonical_path is not None and canonical_path != path:
        mirrored = next((row for row in rows if row.kind == "self_reported"
            and any(source.record_id == canonical_path for source in row.sources)
            and all(_same_source_value(a, b) for a, b in zip(
                (row.code, row.name, row.value, row.observed_at, row.source_status, row.unit, row.reference_range, row.category),
                (_text(code), _text(name) or path, scalar, source_date, status, _text(unit), _text(reference), _text(category))))), None)
        if mirrored is not None:
            mirrored.reference(path)
            if path not in mirrored.fact_ids:
                mirrored.fact_ids.append(path)
            return
    existing = next((row for row in rows if not questionnaire and row.kind == "metric"
        and _key(row.code or row.name) == _key(code) and _text(row.value) == _text(scalar)
        and (row.unit, row.reference_range, row.source_status, row.observed_at, row.source_name)
            == (_text(unit), _text(reference), status, source_date, source_name)
        and row.category == _text(category)), None)
    if existing:
        existing.reference(path)
        existing.fact_ids.append(path)
        if isinstance(value, dict) and not questionnaire:
            _retain_function_links(existing, value, path)
        return
    row = _Observation(observation_id=f"extra:{_hash(path)}", code=_text(code), name=_text(name) or path,
        value=scalar, source_status=status, unit=_text(unit), reference_range=_text(reference),
        category=_text(category),
        kind="self_reported" if questionnaire else "function", source_type="questionnaire" if questionnaire else "microbiome_data",
        source_name=source_name, observed_at=source_date)
    row.reference(path)
    row.fact_ids.append(path)
    if isinstance(value, dict) and not questionnaire:
        _retain_function_links(row, value, path)
    rows.append(row)


def _put(target: list, row: _Observation, code: str | None = None, *, observations: dict | None = None) -> None:
    metric = row.metric()
    scfa_grade = None if row.source_projection_only or metric.source_status and metric.source_status.strip() else _scfa_score_grade(row)
    if scfa_grade is not None:
        metric.assessment_level = scfa_grade
        metric._assessment_origin = ("source" if scfa_grade == _text(row.source_status)
            else "reference" if scfa_grade == _source_legend_grade(row) else "report")
    if code is not None:
        metric.code = code
        report_grade = (None if row.source_projection_only or row.catalog_family in {'GM', 'G1'}
            else catalog.template_indicator_grade(code, row.value))
        if report_grade is not None and not (metric.source_status and metric.source_status.strip()):
            # The callback retains the literal source label and range. This
            # separate assessment follows the approved report standard.
            if code == "gmwi2" and report_grade == "不健康倾向/不健康" and row.source_status in {"不健康", "不健康倾向"}:
                report_grade = row.source_status
            metric.assessment_level = report_grade
            metric._assessment_origin = "report"
        if not row.source_projection_only and metric.explanation is None:
            metric.explanation = catalog.OVERALL_STRUCTURE_EXPLANATIONS.get(code)
        # Canonical Parsed metrics and their supplemental table projections
        # can be the same observation. Merge only an exact source/date/value
        # fingerprint, retaining the complete independent appendix rows.
        identity = (row.source_name, row.observed_at, _text(row.value), row.unit,
            row.source_status, re.sub(r"\s+", "", unicodedata.normalize("NFKC", row.reference_range or "")))
        key = (id(target), code, identity)
        existing = observations.get(key) if observations is not None else None
        if existing is None and observations is not None and code in {'beRatio', 'fbRatio'}:
            # A supplier summary repeats the same scalar without its range;
            # its detailed table supplies that range. The source/date/value/
            # unit/grade must all agree before sharing one fixed display row.
            # Conflicting references remain distinct original observations.
            existing = next((item for (target_id, item_code, original), item in observations.items()
                if target_id == id(target) and item_code == code and original[:5] == identity[:5]
                and (not original[5] or not identity[5])), None)
            if existing is not None and not existing.reference_range and row.reference_range:
                existing.reference_range = row.reference_range
        if existing is not None:
            for source in row.sources:
                if source not in existing.sources:
                    existing.sources.append(source.model_copy(deep=True))
            if observations is not None:
                observations[key] = existing
            return
        slot = next((m for m in target if m.code == code and not m.sources), None)
        if slot is not None:
            for field_name in ("value", "unit", "reference_range", "source_status", "assessment_level", "explanation", "sources", "availability"):
                setattr(slot, field_name, getattr(metric, field_name))
            slot._assessment_origin = metric._assessment_origin
            if observations is not None:
                observations[key] = slot
            return
    target.append(metric)
    if code is not None and observations is not None:
        observations[key] = metric


_OVERALL_ALIASES = {
    "gmwi2": ("gmwi2", "肠道微生物组健康指数gmwi2"),
    "gmhi": ("gmhi", "肠道微生物健康指数", "肠道菌群健康指数"),
    "gutHealthIndex": ("gut_health_index", "gutHealthIndex", "肠菌健康指数"),
    "gutHealthIndexScore": ("gut_health_index_score", "肠道微生物组健康指数评分"),
    "gutAge": ("gut_age", "gutAge", "肠道年龄"),
    "microbiomeHealthScore": ("microbiome_health_score", "microbiomeHealthScore", "gut_overall_score", "肠道微生态总评分", "肠道总评分", "肠道菌群健康度评分"),
    "speciesCount": ("species_count", "speciesCount", "菌群种类数", "肠道微生物种类", "肠道菌群种类", "肠道菌群种类数", "菌群种类", "菌种数"),
    "diversity": ("diversity", "shannon", "香农指数", "菌群多样性", "肠道菌群多样性", "肠道微生物多样性", "多样性指数", "菌群多样性指数", "香农多样性指数"),
    "beRatio": ("be_ratio", "beratio", "be比值", "肠道定植抗力", "肠道微生物定植抗力"),
    "fbRatio": ("fb_ratio", "fbratio", "fb比值", "厚壁菌门拟杆菌门比值", "厚壁菌门拟杆菌门", "肠道菌群热量代谢fb比值"),
    "beBalanceScore": ("be_score", "beBalanceScore", "肠道菌群平衡状态评分"),
    "fbEnergyMetabolismScore": ("fb_score", "fbEnergyMetabolismScore", "肠道菌群热量代谢评分"),
    "enterotype": ("enterotype", "肠型", "肠道菌群分型", "菌群分型", "肠道菌群肠型"),
    "balanceStatus": ("balance_status", "balanceStatus", "平衡状态", "菌群平衡状态", "肠道菌群平衡状态"),
    "pathogenicPotential": ("pathogenic_potential", "pathogenicPotential", "肠道菌群致病潜力", "致病潜力"),
}
_HEALTH_ALIASES = {
    "beneficialBacteria": ("panel_beneficial", "panel_title_beneficial", "有益菌"),
    "scfaProducerBacteria": ("panel_scfa_producers", "scfa_producer_bacteria", "产短链脂肪酸的菌"),
    "pathogenicBacteria": ("panel_pathogenic", "panel_title_pathogenic", "致病菌"),
    "opportunisticBacteria": ("panel_opportunistic", "panel_title_opportunistic", "条件致病菌"),
    "pathogenicFungi": ("pathogenic_fungi_summary", "致病性真菌"),
    "pathogenicViruses": ("pathogenic_viruses_summary", "致病性病毒"),
    "pathogenicParasites": ("pathogenic_parasites_summary", "致病性寄生虫"),
    "virulenceGenes": ("virulence_genes_summary", "毒力基因"),
    "antibioticResistanceGenes": ("antibiotic_resistance_genes_summary", "抗生素耐药基因"),
    "otherHealthBacteria": ("panel_title_other", "其他健康相关菌", "部分与健康相关的其他菌种"),
    "antiInflammatoryCapacity": ("anti_inflammatory", "anti_inflammatory_capacity", "肠道抗炎能力", "肠道菌群抗炎能力", "抗炎能力"),
    "gutImmunity": ("gut_immunity", "肠道免疫力"),
    "dietaryFiberNeed": ("dietary_fiber_need", "肠道膳食纤维需求", "膳食纤维需求"),
    "biofilmCapacity": ("biofilm_capacity", "肠道菌群生物膜合成能力", "菌群生物膜合成能力"),
    "oxidativeStressTolerance": ("oxidative_stress_tolerance", "肠道菌群氧化胁迫耐受", "肠道菌群氧化耐受胁迫", "氧化胁迫耐受"),
    "pathogenicPotential": _OVERALL_ALIASES["pathogenicPotential"],
}
_SCFA_ALIASES = {"formate": ("formate", "甲酸", "甲酸合成能力"), "acetate": ("acetate", "乙酸", "乙酸合成能力"),
    "propionate": ("propionate", "丙酸", "丙酸合成能力"), "butyrate": ("butyrate", "丁酸", "丁酸合成能力"), "valerate": ("valerate", "戊酸", "戊酸合成能力")}
_MAJOR_ALIASES = {"carbohydrate": ("carbohydrate_metabolism", "碳水化合物", "碳水化合物代谢", "碳水化合物代谢能力"),
    "fat": ("fat_metabolism", "脂肪", "脂肪代谢", "脂肪代谢能力"), "protein": ("protein_metabolism", "蛋白质", "蛋白质代谢", "蛋白质代谢能力"), "minerals": ("mineral_metabolism", "矿物质", "矿物质代谢", "矿物质代谢能力")}
_NEURO_ALIASES = {"serotonin": ("serotonin", "5ht", "5羟色胺", "血清素", "5羟色胺合成能力"), "dopamine": ("dopamine", "多巴胺", "多巴胺合成能力"),
    "acetylcholine": ("acetylcholine", "乙酰胆碱", "乙酰胆碱合成能力"), "gaba": ("gaba", "γ氨基丁酸", "伽马氨基丁酸", "γ氨基丁酸合成能力"), "histamine": ("histamine", "组胺", "组胺合成能力"),
    "norepinephrine": ("norepinephrine", "noradrenaline", "去甲肾上腺素", "去甲肾上腺素合成能力"), "endorphins": ("endorphin", "内啡肽", "内啡肽合成能力")}


def _alias(row: _Observation, definitions: dict) -> str | None:
    if row.core_code and (definitions is _OVERALL_ALIASES or definitions is _HEALTH_ALIASES) and row.core_code in definitions:
        return row.core_code
    def forms(value):
        label = _key(value)
        return {label, label[:-2]} if label.endswith("评估") else {label}
    keys = forms(row.code) | forms(row.name)
    return next((code for code, names in definitions.items() if any(forms(name) & keys for name in names)), None)


_DISEASE_SYNONYMS = (
    ("非酒精性脂肪肝", "非酒精性脂肪性肝病", "非酒精性脂肪肝病", "NAFLD", "nonalcoholic fatty liver disease"),
    ("炎症性肠病", "炎症性肠病（总体）", "IBD", "inflammatory bowel disease"),
    ("II型糖尿病", "Ⅱ型糖尿病", "2型糖尿病", "type 2 diabetes", "type 2 diabetes mellitus", "T2DM"),
)
_AMR_CATEGORIES = {"amr", "antimicrobialresistance", "antibioticresistance", "resistancegenes",
    "抗生素耐药基因", "抗生素耐药基因评估", "抗生素耐药基因检出", "抗生素耐药", "抗菌药物耐药", "耐药基因"}
_ANTIBIOTIC_CLASSES = (
    "β内酰胺", "贝塔内酰胺", "青霉素", "头孢菌素", "头孢", "碳青霉烯", "单环内酰胺",
    "氨基糖苷", "喹诺酮", "氟喹诺酮", "大环内酯", "四环素", "磺胺", "磺酰胺",
    "甲氧苄氨嘧啶", "甲氧苄啶", "万古霉素", "糖肽", "脂肽", "氯霉素", "林可酰胺",
    "链阳菌素", "噁唑烷酮", "恶唑烷酮", "利福霉素", "利福平", "新生霉素",
    "硝基咪唑", "硝基呋喃", "多黏菌素", "多粘菌素",
    "beta lactam", "beta lactams", "penicillin", "cephalosporin", "carbapenem", "monobactam",
    "aminoglycoside", "quinolone", "fluoroquinolone", "macrolide", "tetracycline", "sulfonamide",
    "trimethoprim", "vancomycin", "glycopeptide", "lipopeptide", "chloramphenicol", "phenicol",
    "lincosamide", "streptogramin", "oxazolidinone", "rifamycin", "rifampicin", "novobiocin",
    "nitroimidazole", "nitrofuran", "polymyxin",
)
_AMINO_ACIDS = catalog.AMINO_ACID_ALIASES

_PIGMENTS = {"carotenoids": ("类胡萝卜素", "胡萝卜素", "β胡萝卜素", "carotenoid", "carotenoids", "beta carotene"),
    "flavonoids": ("类黄酮", "flavonoid", "flavonoids"), "anthocyanins": ("花青素", "anthocyanin", "anthocyanins"),
    "lycopene": ("番茄红素", "lycopene"), "lutein": ("叶黄素", "lutein"),
    "isoflavones": ("异黄酮", "异黄酮素", "isoflavone", "isoflavones"), "chlorophyll": ("叶绿素", "chlorophyll")}
_NUCLEOTIDES = {"nucleotides": ("核苷酸", "nucleotide", "nucleotides"),
    "purines": ("嘌呤", "purine", "purines"), "pyrimidines": ("嘧啶", "pyrimidine", "pyrimidines")}
_OTHER_METABOLITES = {"bileAcids": ("胆汁酸", "bile acid", "bile acids"),
    "glutathione": ("谷胱甘肽", "谷胱甘肽（三肽）", "glutathione"),
    "lipoicAcid": ("硫辛酸", "lipoic acid"), "coenzymeQ": ("辅酶Q", "coenzyme Q", "ubiquinone"),
    "homocysteine": ("同型半胱氨酸", "homocysteine"), "lipopolysaccharide": ("脂多糖", "lipopolysaccharide")}


def _standard_term(row: _Observation, definitions: dict) -> str | None:
    def base(value):
        label = _key(value)
        previous = None
        while previous != label:
            previous = label
            for suffix in ("synthesisability", "synthesisscore", "synthesis", "metabolismscore", "metabolism",
                    "合成能力", "合成评分", "代谢能力", "代谢评分", "合成", "代谢", "assessment", "评估", "score", "评分"):
                if label.endswith(suffix):
                    label = label[:-len(suffix)]
                    break
        for prefix in ("肠道菌群", "菌群", "来源", "普通"):
            if label.startswith(prefix):
                label = label[len(prefix):]
                break
        return label
    keys = {base(row.code), base(row.name)}
    return next((code for code, aliases in definitions.items()
        if any(_key(alias) in keys or "l" + _key(alias) in keys for alias in aliases)), None)


def _nutrient_group(row: _Observation) -> str | None:
    if _is_amr(row) or row.kind not in {"metric", "function"} or _alias(row, _OVERALL_ALIASES) or _alias(row, _HEALTH_ALIASES) or _alias(row, _MAJOR_ALIASES):
        return None
    if _standard_term(row, _PIGMENTS):
        return "pigments"
    label = _key((row.code or "") + row.name)
    return next((code for code, pattern in (("vitamins", r"vitamin|维生素|叶酸|folate|biotin|烟酸|生物素"),
        ("traceElements", r"mineral|微量元素|矿物质|selenium|zinc|iron|硒|锌|铁|钙|镁"),
        ("pigments", r"pigment|carotenoid|flavonoid|anthocyanin|色素|胡萝卜素|类黄酮|花青素")) if re.search(pattern, label)), None)


def _is_amr(row: _Observation) -> bool:
    """Only explicit C-source AMR context or complete antibiotic class terms."""
    if row.kind not in {"metric", "function"}:
        return False
    if _key(row.category) in _AMR_CATEGORIES:
        return True
    if any(re.match(r"^request\.microbiomeData\.(?:antimicrobialResistance|antibioticResistance|resistanceGenes)(?:\[|\.|$)", source.record_id or "")
            for source in row.sources):
        return True
    keys = {_key(row.code), _key(row.name)}
    if any(re.search(r"抗生素耐药|抗菌药物耐药|耐药基因|antimicrobialresistance|antibioticresistance|resistancegenes?", key) for key in keys):
        return True
    suffix = r"(?:类|抗生素类|抗生素|耐药基因|耐药性|耐药|抗性基因|抗性|s|class|antibiotics|antibiotic|resistancegenes?|resistance)?"
    return any(re.fullmatch(re.escape(_key(term)) + suffix, key)
        for term in _ANTIBIOTIC_CLASSES for key in keys)


def _antibiotic_class_name(row: _Observation, parsed: Any) -> str | None:
    """Use the selected original table's verified class, never a drug-name guess."""
    checksum = _get(parsed, "source_checksum", "sourceChecksum")
    filename = _get(parsed, "source_name", "sourceName")
    compact = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", _text(value) or ""))
    day = lambda value: (_text(value) or "")[:10]
    matches = []
    candidates = (_get(parsed, "evaluation_amr_classes", "evaluationAmrClasses", default=[]) or []) if (
        checksum and filename and row.source_name == filename) else []
    for item in candidates:
        value = _mapping(item)
        if value.get("sourceChecksum") != checksum or value.get("sourceFile") != filename:
            continue
        if (compact(value.get("itemName")), compact(value.get("value")), compact(value.get("unit")),
                compact(value.get("status")), day(value.get("reportTime"))) != (
                compact(row.name), compact(row.value), compact(row.unit), compact(row.source_status), day(row.observed_at)):
            continue
        if value.get("antibioticClass"):
            matches.append(value["antibioticClass"])
    if matches:
        return matches[0] if len(set(matches)) == 1 else None
    if row.antibiotic_classes:
        return row.antibiotic_classes[0] if len(set(row.antibiotic_classes)) == 1 else None
    # An input already named as a complete drug class can retain that literal
    # name. A specific drug (for example novobiocin) cannot supply its class.
    if _key(row.name) in {_key("新生霉素"), "novobiocin"}:
        return None
    suffix = r"(?:类|抗生素类|抗生素|s|class|antibiotics|antibiotic)?"
    return row.name if any(re.fullmatch(re.escape(_key(term)) + suffix, _key(row.name))
        for term in _ANTIBIOTIC_CLASSES) else None


def _disease_name_keys(name: str) -> set[str]:
    key = _key(name)
    return next(({_key(alias) for alias in group} for group in _DISEASE_SYNONYMS
        if key in {_key(alias) for alias in group}), {key})


def _unclassified_microbiome_number(row: _Observation) -> bool:
    """A generic extracted number is not affirmative functional evidence."""
    if row.kind != "function" or row.source_type != "microbiome" or not any(
            re.match(r"(?:request\.hospitalData|parsed\.supplementaryHospitalData)\[", source.record_id or "")
            for source in row.sources):
        return False
    if not (type(row.value) in (int, float, bool) or isinstance(row.value, str)
            and re.fullmatch(r"[<>≤≥]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s*[%％])?", row.value.strip())):
        return False
    if (_pathways(row) or row.explicit_pathways or row.related_taxa or row.taxon_links
            or re.fullmatch(r"P(?:[1-9]|10)", row.code or "", re.I)):
        return False
    known = any(_alias(row, aliases) for aliases in (
        _OVERALL_ALIASES, _HEALTH_ALIASES, _SCFA_ALIASES, _MAJOR_ALIASES, _NEURO_ALIASES))
    if known or _nutrient_group(row) or any(_standard_term(row, aliases)
            for aliases in (_AMINO_ACIDS, _OTHER_METABOLITES, _NUCLEOTIDES, _PIGMENTS)):
        return False
    # The section/category or original functional legend must say what the
    # number represents. A source filename, percentile sign or number alone
    # cannot classify a chart remainder as nutrient synthesis.
    functional = re.compile(r"合成|代谢|分解|降解|营养|功能|synthesis|metaboli|degrad|nutrition|functional|pathway", re.I)
    semantics = " ".join(str(value) for value in (
        row.name, row.category, row.reference_range, row.description, *row.source_sections) if value)
    return not bool(functional.search(semantics))


def _apply_source_semantics(rows: list[_Observation]) -> None:
    """Separate disease-model/table semantics before any nutrient-name matching.

    This runs after binding the shared collector's original fact IDs, so a
    semantic boundary never creates a second observation or loses its source.
    """
    # A table mirror may omit the canonical code (e.g. the plain B/E balance
    # label). Its exact name is interpretable only when this same source
    # snapshot contains an explicitly coded metric with the same value.
    # Keep the original row code and all origins in the appendix.
    canonical_core = [(row, _alias(row, _OVERALL_ALIASES) or _alias(row, _HEALTH_ALIASES))
        for row in rows if row.kind == "metric" and any(
            re.match(r"parsed\.metrics\[", source.record_id or "") for source in row.sources)]
    for row in rows:
        if row.kind not in {"metric", "function", "context"}:
            continue
        codes = {code for canonical, code in canonical_core if code and row.source_name is not None
            and (row.source_name, row.observed_at, _text(row.value), _key(row.name)) == (
                canonical.source_name, canonical.observed_at, _text(canonical.value), _key(canonical.name))}
        if len(codes) == 1:
            row.core_code = codes.pop()
    diseases = [row for row in rows if row.kind == "disease"]
    for row in rows:
        if _unclassified_microbiome_number(row):
            row.kind = "context"
            row.category = row.category or "unclassified"
        if row.kind == "context" and row.category in {"sourceReportedBackground", "healthAssociation", "virulence", "structuralAssessment"}:
            continue
        if _is_amr(row):
            row.category = row.category or "antimicrobialResistance"
            continue
        if row.kind not in {"metric", "function", "context"}:
            continue
        if (row.kind in {"metric","function"} and not row.explicit_pathways and not row.source_sections
                and not row.related_taxa and not re.fullmatch(r"P(?:[1-9]|10)",row.code or '',re.I)):
            known = any(_alias(row, aliases) for aliases in (
                _OVERALL_ALIASES, _HEALTH_ALIASES, _SCFA_ALIASES, _MAJOR_ALIASES, _NEURO_ALIASES))
            known = known or bool(_nutrient_group(row)) or any(_standard_term(row, aliases)
                for aliases in (_AMINO_ACIDS,_OTHER_METABOLITES,_NUCLEOTIDES,_PIGMENTS))
            age_context = re.fullmatch(r"(?:您的)?(?:实际年龄|肠道年龄差|您的肠道年龄)", row.name)
            # Unscored source headings, qualitative overview summaries and
            # the source patient's age are context, not nutrient synthesis
            # measurements. Keep each original row in the source inventory.
            if not known and (row.value is None or age_context or isinstance(row.value,str)
                    and not re.fullmatch(r"[<>≤≥]?\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:\s*[%％])?",row.value.strip())):
                row.kind = "context"
        matching = [disease for disease in diseases
            if _disease_name_keys(row.name) & _disease_name_keys(disease.name)]
        if not matching:
            continue
        row.kind = "diseaseModel"
        for disease in matching:
            if (row.source_name, row.observed_at, _text(row.value)) != (
                    disease.source_name, disease.observed_at, _text(disease.value)):
                continue
            if row.source_status is not None and disease.source_status is not None and _key(row.source_status) != _key(disease.source_status):
                continue
            for source in row.sources:
                if source not in disease.sources:
                    disease.sources.append(source.model_copy(deep=True))
            if disease.code:
                row.reference(disease.code)


def _pathways(row: _Observation) -> list[str]:
    if _is_amr(row):
        return ["P10"]
    if row.kind not in {"metric", "function"}:
        return []
    core = _alias(row, _OVERALL_ALIASES)
    if core:
        return ["P9"] if core == "pathogenicPotential" else []
    explicit = list(row.explicit_pathways)
    # An explicitly retained source subsection supplies the original table's
    # grouping, not an inferred microbe/function or disease relationship.
    source_sections = {
        "碳水化合物（糖类）分解代谢": "P1", "碳水化合物(糖类)分解代谢": "P1",
        "脂质合成和分解代谢": "P2", "短链脂肪酸合成代谢": "P3",
        "氨基酸合成代谢": "P4", "维生素合成代谢": "P5", "神经递质合成代谢": "P6",
        "核苷酸合成代谢": "P7", "天然色素合成代谢": "P8",
        "有害代谢物合成能力": "P9", "毒性物质分解能力": "P10",
        "有害代谢物合成代谢": "P9", "毒性物质分解代谢": "P10",
    }
    for section in row.source_sections:
        code = next((code for name, code in source_sections.items() if _key(section) == _key(name)), None)
        if code and code not in explicit:
            explicit.append(code)
    if re.fullmatch(r"P(?:[1-9]|10)", row.code or "", re.IGNORECASE) and row.code.upper() not in explicit:
        explicit.append(row.code.upper())
    if explicit:
        return explicit
    text = _key((row.code or "") + " " + row.name)
    result = []
    if _standard_term(row, _AMINO_ACIDS):
        result.append("P4")
    if _standard_term(row, _PIGMENTS):
        result.append("P8")
    if _standard_term(row, _NUCLEOTIDES):
        result.append("P7")
    # Complete SCFA names are required: the "丁酸"/"butyrate" substrings
    # also occur in neurotransmitters and other chemically distinct acids.
    if _standard_term(row, _SCFA_ALIASES) or _standard_term(row,
            {"scfa": ("scfa", "短链脂肪酸", "short chain fatty acid", "short chain fatty acids")}):
        result.append("P3")
    for code, pattern in (
        ("P1", r"carbohydrate|starch|sugar|fiber|pyruvate|pyruvic|glycolysis|碳水|淀粉|单糖|多糖|膳食纤维|乳糖|乳制品|丙酮酸|糖酵解|磷酸戊糖|果糖|甘露糖|半乳糖|蔗糖|氨基糖|核苷酸糖"),
        ("P2", r"lipid|fat|bile|sphingolipid|脂肪|脂质|胆汁酸|甘油|亚麻酸|花生四烯酸|鞘脂"),
        ("P4", r"aminoacid|protein|氨基酸|蛋白质|色氨酸|赖氨酸|谷氨酸|组氨酸|精氨酸"),
        ("P5", r"vitamin|维生素|叶酸|folate|biotin|thiamin|riboflavin|烟酸|生物素"),
        ("P6", r"neurotransmitter|serotonin|dopamine|acetylcholine|gaba|histamine|norepinephrine|endorphin|神经递质|多巴胺|羟色胺|乙酰胆碱|氨基丁酸|组胺|内啡肽|去甲肾上腺素"),
        ("P8", r"carotenoid|flavonoid|anthocyanin|pigment|色素|类胡萝卜素|类黄酮|花青素|番茄红素"),
        ("P9", r"pathogenicpotential|virulence|lipopolysaccharide|homocysteine|致病潜力|毒力|脂多糖|同型半胱氨酸|有害代谢物"),
        ("P10", r"resistance|biofilm|oxidative|detox|耐药|耐受|生物膜|毒性物质降解|毒性物质分解"),
    ):
        if re.search(pattern, text) and code not in result:
            result.append(code)
    return result


def _disease_category(name: str, *, code=None, category=None) -> str:
    return catalog.disease_category(name, code=code, category=category)


def _taxon_direction(value: Any) -> str | None:
    text = _text(value) or ""
    if re.search(r"↑|偏高|增多|升高|超标|\bhigh\b|increas", text, re.I):
        return "up"
    if re.search(r"↓|偏低|减少|降低|缺乏|\blow\b|decreas", text, re.I):
        return "down"
    return None


def _association_signals(row: _Observation, rows: list[_Observation]) -> list[public.TaxonSignal]:
    """Bind an explicitly named source relation to the same measured taxon.

    Different printed grades for one taxon remain distinct source occurrences.
    An upward relation must not borrow a normal/downward occurrence, a genus
    must not borrow a species, and another report cannot supply the value.
    """
    result = []
    for taxon_id, supplied in row.taxon_links:
        names = {_key(supplied.name), _key(supplied.scientific_name)} - {""}
        direction = _taxon_direction(supplied.source_status)
        matches = [taxon for taxon in rows if taxon.kind in {"taxon", "microbe"}
            and (taxon.source_name, taxon.observed_at) == (row.source_name, row.observed_at)
            and names & {_key(taxon.name), _key(taxon.scientific_name)}
            and (not taxon_id or taxon.code == taxon_id)
            and (not direction or _taxon_direction(taxon.source_status) == direction)
            and all(provided is None or provided == actual for provided, actual in
                ((supplied.value, taxon.value), (supplied.unit, taxon.unit)))]
        for taxon in matches:
            signal = public.TaxonSignal(name=taxon.name, scientific_name=taxon.scientific_name,
                value=_text(taxon.value) if isinstance(taxon.value, bool) else taxon.value,
                unit=taxon.unit, source_status=_text(taxon.source_status),
                sources=copy.deepcopy([*taxon.sources, *supplied.sources, *row.sources]))
            _merge_taxon_signal(result, signal)
    return result


def _merge_taxon_signal(target, signal) -> None:
    identity = signal.model_dump(exclude={"sources"})
    metadata = ('_source_correlation', '_source_reference_range')
    existing = next((other for other in target if other.model_dump(exclude={"sources"}) == identity
        and all(getattr(other, key) is None or getattr(signal, key) is None
            or getattr(other, key) == getattr(signal, key) for key in metadata)), None)
    if existing is None:
        target.append(signal)
    else:
        for key in metadata:
            if getattr(existing, key) is None:
                setattr(existing, key, getattr(signal, key))
        existing.sources.extend(source.model_copy(deep=True) for source in signal.sources
            if source not in existing.sources)


def _disease_taxon_label(value):
    """Separate a source's explicit trailing flag from its organism name."""
    text = str(value or '').strip()
    match = re.fullmatch(r'(.+?)\s*[（(](超标|偏高|缺乏|偏低|正常)[）)]', text)
    return (match[1].strip(), match[2]) if match else (text, None)


def _disease_summary_taxa(row, rows):
    """Resolve only the selected report's explicitly listed organisms."""
    result = []
    for label in row.related_taxa:
        name, status = _disease_taxon_label(label)
        direction = _taxon_direction(status)
        matching = [taxon for taxon in rows if taxon.kind in {'taxon', 'microbe'}
            and (taxon.source_name, taxon.observed_at) == (row.source_name, row.observed_at)
            and _key(name) in {_key(taxon.name), _key(taxon.scientific_name)}
            and (not direction or _taxon_direction(taxon.source_status) == direction)]
        for taxon in matching:
            _merge_taxon_signal(result, public.TaxonSignal(name=taxon.name,
                scientific_name=taxon.scientific_name, value=_text(taxon.value) if isinstance(taxon.value, bool) else taxon.value,
                unit=taxon.unit, source_status=_text(taxon.source_status),
                sources=copy.deepcopy([*taxon.sources, *row.sources])))
        if not matching:
            _merge_taxon_signal(result, public.TaxonSignal(name=name,
                source_status=status, sources=copy.deepcopy(row.sources)))
    return result


def _project_disease_source_evidence(data, parsed, rows):
    """Link a verified card's own table without inferring any disease risk."""
    from modules.report.document_source import EvaluationDiseaseEvidenceMetric

    filename = _get(parsed, 'source_name', 'sourceName')
    checksum = _get(parsed, 'source_checksum', 'sourceChecksum')
    candidates = _get(parsed, 'evaluation_panel_summaries', 'evaluationPanelSummaries', default=[]) or []
    targets = [item for category in data.disease_risk_assessment.categories for item in category.items]
    targets.extend(data.disease_risk_assessment.risk_ranking)
    for index, card in enumerate(candidates):
        if (not isinstance(card, EvaluationDiseaseEvidenceMetric) or not checksum
                or (card.source_file, card.source_checksum) != (filename, checksum)):
            continue
        models = [row for row in rows if row.kind == 'disease'
            and row.source_name == filename and row.observed_at == card.report_date
            and _disease_name_keys(row.name) & _disease_name_keys(card.disease_name)
            and _same_source_number(row.value, card.disease_score)
            and _key(row.source_status) == _key(card.disease_level)]
        origins = {source.record_id for row in models for source in row.sources
            if (source.record_id or '').startswith('parsed.disease_results[')}
        if not origins:
            continue
        for target in targets:
            if (not any(source.record_id in origins for source in target.sources)
                    or not _disease_name_keys(target.name) & _disease_name_keys(card.disease_name)
                    or not _same_source_number(target.value, card.disease_score)
                    or _key(target.source_status) != _key(card.disease_level)):
                continue
            for taxon_index, actual in enumerate(card.taxa):
                path = actual.get('sourcePath')
                if card.source_page == 0:
                    # Canonical JSON has an exact row path, not a fictitious
                    # PDF page. This typed card was bound to the parsed file
                    # and matched disease observation above.
                    if not isinstance(path, str) or not re.fullmatch(
                            r'diseaseRisks\[\d+\]\.relatedMicrobeDetails\[\d+\]', path):
                        continue
                    locator = f'source:{checksum}:json:{path}'
                elif card.source_page > 0:
                    locator = f'source:{checksum}:page:{card.source_page}'
                else:
                    continue
                signal = public.TaxonSignal(name=actual['name'], scientific_name=actual['scientificName'],
                    value=actual['value'], unit=actual['unit'], source_status=actual['sourceStatus'],
                    sources=[public.SourceReference(source_code='C',
                        record_id=f'parsed.evaluationPanelSummaries[{index}].taxa[{taxon_index}]', observed_at=card.report_date),
                        public.SourceReference(source_code='C',
                            record_id=locator, observed_at=card.report_date)])
                signal._source_correlation = _text(actual.get('correlation'))
                signal._source_reference_range = _text(actual.get('referenceRange'))
                # Replace only an unmeasured mirror of the same explicit label;
                # no other taxon's value, flag, or source occurrence is lost.
                target.related_taxa[:] = [existing for existing in target.related_taxa
                    if not (existing.value is None
                        and _key(_disease_taxon_label(existing.name)[0]) in {_key(signal.name), _key(signal.scientific_name)}
                        and _taxon_direction(existing.source_status) in {None, _taxon_direction(signal.source_status)})]
                _merge_taxon_signal(target.related_taxa, signal)
            if card.source_notice and not card.taxa and not target.related_taxa and target.explanation is None:
                target.explanation = card.source_notice
                for record_id in (f'parsed.evaluationPanelSummaries[{index}].sourceNotice',
                        f'source:{checksum}:page:{card.source_page}'):
                    source = public.SourceReference(source_code='C', record_id=record_id, observed_at=card.report_date)
                    if source not in target.sources:
                        target.sources.append(source)


def _project_health_associations(data, rows) -> None:
    """Organise real qualitative associations without creating model indices."""
    from .evaluation_disease_identity import same_source_disease
    categories = {category.code: category for category in data.disease_risk_assessment.categories}
    for row in rows:
        if row.kind != "context" or row.category != "healthAssociation":
            continue
        category = categories.get(_disease_category(row.name, code=row.code, category=row.category))
        if category is None:
            continue
        matching = [model for model in rows if model.kind == "disease"
            and (model.source_name, model.observed_at) == (row.source_name, row.observed_at)
            and same_source_disease(model.name, row.name)]
        model_origins = {source.record_id for model in matching for source in model.sources
            if (source.record_id or "").startswith("parsed.disease_results[")}
        targets = [item for item in [*category.items, *data.disease_risk_assessment.risk_ranking]
            if any(source.record_id in model_origins for source in item.sources)]
        if not targets:
            # An association signal has no disease-model index. Preserve it in
            # source context, never manufacture a model-risk result from it.
            continue
        signals = _association_signals(row, rows)
        for item in targets:
            for signal in signals:
                _merge_taxon_signal(item.related_taxa, signal.model_copy(deep=True))
        category.sources.extend(source.model_copy(deep=True) for source in row.sources
            if source not in category.sources)
        category.availability = "partial"


_PANEL_SECTIONS = {
    "有益菌": ("beneficial", "有益菌"),
    "产短链脂肪酸的菌": ("scfaProducers", "产短链脂肪酸的菌"),
    "致病菌": ("pathogenic", "致病菌"),
    "条件致病菌": ("opportunistic", "条件致病菌"),
    "致病性真菌": ("fungi_pathogenic", "致病性真菌"),
    "病毒": ("virus", "病毒"),
    "致病性寄生虫": ("parasites", "致病性寄生虫"),
}


def _provided_detection(row: _Observation) -> bool | None:
    number = _number(row.value)
    number = number if number is not None and math.isfinite(number) and number >= 0 else None
    if row.detection_status in {"detected", "trace"}:
        return True
    if row.detection_status == "not_detected":
        return None if number is not None and number > 0 else False
    return number > 0 if number is not None else None


def _provided_panel_rows(parsed, rows):
    """Use original source headings before taxonomy names or word heuristics."""
    grouped = {}
    occurrences = _get(parsed, "source_taxon_occurrences", "sourceTaxonOccurrences", default=[]) or []
    for index, item in enumerate(occurrences):
        section = _text(_get(item, "sourceSection", "source_section")) or ""
        title = re.sub(r"^\s*\d+(?:\.\d+)*[.、．]?\s*", "", section).strip()
        group = _PANEL_SECTIONS.get(title)
        if group is None:
            continue
        name = _first(_get(item, "chineseName", "chinese_name"), _get(item, "latinName", "latin_name"))
        if not name:
            continue
        locator = f"parsed.sourceTaxonOccurrences[{index}]"
        matching = [row for row in rows if row.kind in {"taxon", "microbe"}
            and any(source.record_id == locator for source in row.sources)]
        row = _Observation(observation_id=locator, code=_get(item, "taxonId", "taxon_id"), name=str(name),
            value=_get(item, "value"), unit=_get(item, "unit"),
            reference_range=_get(item, "referenceRange", "reference_range"),
            source_status=_get(item, "sourceStatus", "source_status"), kind="taxon", category=group[0],
            scientific_name=_get(item, "latinName", "latin_name"),
            detection_status=_get(item, "detectionStatus", "detection_status"),
            source_name=_get(parsed, "source_name", "sourceName"),
            observed_at=_get(_get(parsed, "sample", default={}), "report_date", "reportDate"))
        row.reference(locator)
        page = _source_locator(item, _get(parsed, "source_checksum", "sourceChecksum"))
        if page:
            row.reference(page)
        for actual in matching:
            row.sources.extend(source.model_copy(deep=True) for source in actual.sources if source not in row.sources
                and (not (source.record_id or "").startswith("parsed.sourceTaxonOccurrences[") or source.record_id == locator)
                and (not (source.record_id or "").startswith("source:") or source.record_id == page))
        grouped.setdefault(group, []).append(row)
    # Structured inputs may have explicit original categories but no page
    # occurrences. They may supply these four groups, never inferred viruses,
    # parasites or SCFA producers based on a name substring.
    for code, name in _PANEL_SECTIONS.values():
        if code not in {"beneficial", "pathogenic", "opportunistic", "fungi_pathogenic"} or (code, name) in grouped:
            continue
        actual = [row for row in rows if row.kind in {"taxon", "microbe"} and row.category == code]
        if actual:
            grouped[code, name] = actual
    return grouped


def _populate_provided_panel_results(data, parsed, rows) -> None:
    """Expose compact source inventory without inventing supplier panel totals."""
    for (code, name), actual in _provided_panel_rows(parsed, rows).items():
        grades = {}
        for row in actual:
            grade = _text(row.source_status)
            grades[grade] = grades.get(grade, 0) + 1
        distribution = "、".join(("原标" + grade if grade else "未提供原分级") + str(count) + "项"
            for grade, count in grades.items())
        detections = [_provided_detection(row) for row in actual]
        detected, unknown = detections.count(True), detections.count(None)
        value = f"来源列示{len(actual)}项；{distribution}"
        if len(actual) != unknown:
            value += f"；明确非零丰度/检出记录{detected}项"
        if unknown:
            value += f"；检出状态未定{unknown}项"
        data.microbiome_assessment.health_metrics.append(public.MetricAssessment(
            code="panelSummary:" + code, name=name, value=value,
            sources=_combine_sources(actual), availability="partial" if unknown else "available"))
        # Small pathogen panels retain their real normal and zero records.
        # Larger bacterial groups retain every original deviation; a small
        # number of normal representatives supplies context without a raw dump.
        if code in {"fungi_pathogenic", "virus", "parasites"}:
            selected = actual
        else:
            deviations = [row for row in actual if _status_state(row.source_status) == "abnormal"
                or _taxon_direction(row.source_status)]
            nonzero = [row for row in actual if row not in deviations and _provided_detection(row) is True]
            selected = [*deviations, *(nonzero if code == "pathogenic" else nonzero[:1])]
        for row in selected:
            item = row.metric()
            item.code = "panelResult:" + code + ":" + _hash([row.observation_id, row.name, row.value, row.source_status])
            item.explanation = row.scientific_name
            data.microbiome_assessment.health_metrics.append(item)


def _complete_health_summary_details(data, parsed, rows):
    """Recover the printed detail cell, rather than inventing panel members.

    The native overview's third column is authoritative for membership. Exact
    original panel occurrences can add their measured abundance and locators.
    An absent beneficial organism remains a real low finding.
    """
    from modules.report.evaluation_source_recovery import EvaluationHealthSummaryMetric
    from .evaluation_template_input import EvaluationTemplateHealthMetric
    summaries = _get(parsed, "evaluation_panel_summaries", "evaluationPanelSummaries", default=[]) or []
    panel_rows = _provided_panel_rows(parsed, rows)
    groups = {"beneficialBacteria": "beneficial", "scfaProducerBacteria": "scfaProducers",
        "pathogenicBacteria": "pathogenic", "opportunisticBacteria": "opportunistic"}
    completed = []
    for original in data.microbiome_assessment.health_metrics:
        item = public.HealthMetricAssessment.model_validate(original.model_dump())
        item._assessment_origin = original._assessment_origin
        cells = []
        scopes = set()
        for summary in summaries:
            if not isinstance(summary, (EvaluationHealthSummaryMetric, EvaluationTemplateHealthMetric)):
                continue
            if _alias(_Observation("summary", summary.code, summary.name), _HEALTH_ALIASES) != item.code:
                continue
            if not any(source.record_id == _source_locator(summary) for source in item.sources):
                continue
            parts = (getattr(summary, "source_text", None) or "").split("|", 2)
            if len(parts) == 3 and (detail := parts[2].strip()) not in {"", "—", "-", "/", "／", "未检出", "正常"}:
                if detail not in cells:
                    cells.append(detail)
                scopes.add((summary.source_file, summary.observed_at))
        native_detail = "；".join(cells)
        # Printed complete names define membership. A genus substring cannot
        # expand to every species or borrow a member from another report.
        labels = {}
        for token in re.split(r'[、；;，,\n]', native_detail):
            token = token.strip()
            name, status = _disease_taxon_label(re.sub(r'\s*[↑↓↗↘]+\s*$', '', token))
            name = re.sub(r'\s*(?:超标|偏高|缺乏|偏低|正常)\s*$', '', name)
            if _key(name):
                labels[_key(name)] = _taxon_direction(token) or _taxon_direction(status)
        measured = []
        for (group, _), members in panel_rows.items():
            if group != groups.get(item.code):
                continue
            for member in members:
                aliases = {_key(member.name), _key(member.scientific_name)} - {""}
                matches = aliases & labels.keys()
                if (not matches or (member.source_name, member.observed_at) not in scopes
                        or all(labels[name] and _taxon_direction(member.source_status)
                            and labels[name] != _taxon_direction(member.source_status) for name in matches)):
                    continue
                # A member is selected by the printed overview, not by a name
                # heuristic or an invented detection threshold.
                label = member.name
                if member.value is not None:
                    label += " " + str(member.value) + (member.unit or "")
                if member.source_status:
                    label += "（" + str(member.source_status) + "）"
                if label not in measured:
                    measured.append(label)
                    item.sources.extend(source.model_copy(deep=True) for source in member.sources if source not in item.sources)
        item.details = "；".join(measured) or native_detail or getattr(original, "details", None)
        if item.details is None and item.code not in groups and isinstance(item.value, str) and re.search(r"[;；]", item.value):
            item.details = re.split(r"[;；]", item.value, maxsplit=1)[1].strip() or None
        completed.append(item)
    # A detection heading and the same examination's abnormal-member overview
    # are complementary, not competing dated measurements. Prefer the latter
    # only when the exact document hash and nonempty report date agree.
    def native_scope(item):
        documents = {s.record_id.rsplit(":page:", 1)[0] for s in item.sources
            if (s.record_id or "").startswith("source:") and ":page:" in s.record_id}
        dates = {s.observed_at for s in item.sources if s.observed_at}
        return documents, dates
    kept = []
    for item in completed:
        replacement = None
        if item.value == "检出" and item.code in groups and not item.details:
            own_documents, own_dates = native_scope(item)
            replacement = next((other for other in completed if other is not item and other.code == item.code
                and other.value == "异常" and other.details and own_documents and own_dates
                and native_scope(other) == (own_documents, own_dates)), None)
        if replacement is not None:
            replacement.sources.extend(s.model_copy(deep=True) for s in item.sources if s not in replacement.sources)
        else:
            kept.append(item)
    data.microbiome_assessment.health_metrics = kept


def _combine_sources(rows: Iterable[_Observation]) -> list[public.SourceReference]:
    result = []
    for row in rows:
        for source in row.sources:
            if source not in result:
                result.append(source.model_copy(deep=True))
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", str(value).strip()):
        return None
    return float(value)


def _literal_unit(value: Any) -> str:
    """Normalise unit typography without erasing meaningful symbols."""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", _text(value) or "")).casefold()


def _clinical_number(value: Any, unit: Any) -> float | None:
    """Read a single number, optionally followed by its exact supplied unit."""
    number = _number(value)
    if number is not None:
        return number if math.isfinite(number) else None
    if not isinstance(value, str) or not _literal_unit(unit):
        return None
    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.+?)\s*",
        unicodedata.normalize("NFKC", value))
    if not match or _literal_unit(match[2]) != _literal_unit(unit):
        return None
    number = float(match[1])
    return number if math.isfinite(number) else None


def is_ungraded_medical_record(row: Any) -> bool:
    """Identify a small set of neutral raw records, without assigning a grade.

    Both a public camelCase dictionary and a MedicalFinding are supported.
    Unknown projects, real ranges, advice and source/clinical classifications
    remain in the main finding list rather than being hidden by this helper.
    """
    neutral = {"", "本次测量", "测量值", "实测值", "未分级"}
    if any((_text(_get(row, *keys)) or "").strip() not in neutral
            for keys in (("source_status", "sourceStatus"),
                ("assessment_level", "assessmentLevel"))):
        return False
    if any(_text(_get(row, *keys)) and _text(_get(row, *keys)).strip()
            for keys in (("certainty",), ("source_advice", "sourceAdvice"))):
        return False
    reference = unicodedata.normalize("NFKC", _text(_get(row,
        "reference_range", "referenceRange")) or "").strip()
    if reference not in {"", "-", "--", "—", "–", "−"}:
        return False
    name = (_text(_get(row, "name")) or "").strip()
    value = _get(row, "value")
    if name in {"身高", "体重"}:
        return _clinical_number(value, _get(row, "unit")) is not None
    return name in {"性状", "粪便性状"} and value == "软便"


def _status_state(status: Any) -> str | None:
    """Recognise complete source labels; 'abnormal' is never 'normal'."""
    if not isinstance(status, str):
        return None
    text = unicodedata.normalize("NFKC", status).strip().casefold()
    if re.fullmatch(r"正常(?:范围(?:内)?)?|(?:未见|未发现|未查见)(?:明显)?异常|无异常|阴性|未查见|未检出(?:异常)?|normal|negative|neg|not\s+detected|within\s+(?:the\s+)?(?:normal|reference)\s+(?:range|limits)", text):
        return "normal"
    if re.search(r"不正常|非正常|异常|偏高|偏低|升高|降低|阳性|超标|超出|↑|↓|\b(?:abnormal|not\s+normal|positive|high|low|out\s+of\s+range)\b", text):
        return "abnormal"
    return None


def _qualitative_polarity(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    if re.fullmatch(r"[-−]|阴性(?:\s*\(.*\))?|negative|neg|未检出|未查见", text):
        return "negative"
    if re.fullmatch(r"(?:[1-4]\s*)?\+{1,4}|(?:弱|强)?阳性(?:\s*\(.*\))?|positive|pos", text):
        return "positive"
    return None


def _printed_interval_match(reference: str, numeric: str, suffix: str = "") -> re.Match[str] | None:
    """Separate an ordinary signed interval from a printed double-hyphen separator."""
    ordinary = re.fullmatch(rf"\s*({numeric})\s*[-–—~～至]\s*({numeric})\s*{suffix}", reference)
    if ordinary is not None and float(ordinary[1]) <= float(ordinary[2]):
        return ordinary
    # The signed form is preferred when it already produces an ordered range
    # (for example -4--2). A second reading is limited to nonnegative endpoints.
    doubled = re.fullmatch(rf"\s*({numeric})\s*--\s*({numeric})\s*{suffix}", reference)
    if doubled is not None and 0 <= float(doubled[1]) <= float(doubled[2]):
        return doubled
    return None


def _printed_reference_relation(row: _Observation, *, allow_exact_match: bool = False) -> str | None:
    """Compare only the printed reference, without conversions or guessed cutoffs.

    Comparison-string measurements (e.g. >3.0), sex-specific/multiple ranges,
    unmatched units, and reference prose are deliberately not exact numbers.
    """
    reference = unicodedata.normalize("NFKC", row.reference_range or "").strip()
    if row.kind == "clinical" and row.source_type in {"checkup", "lab"}:
        # Some clinical tables print a categorical baseline followed by a
        # trailing placeholder dash. Compare this exact source layout without
        # rewriting the raw reference, result or original source status.
        value = unicodedata.normalize("NFKC", str(row.value or "")).strip()
        if reference == "阴性-":
            if value in {"阴性", "未检出", "未查见"}:
                return "within"
            if _qualitative_polarity(value) == "positive":
                return "outside"
        elif reference == "正常-":
            if value == "正常":
                return "within"
            if value == "异常" or _qualitative_polarity(value) == "positive":
                return "outside"
    polarity = _qualitative_polarity(row.value)
    expected = _qualitative_polarity(reference)
    if polarity and expected:
        return "within" if polarity == expected else "outside"
    number = _clinical_number(row.value, row.unit) if row.kind == "clinical" else _number(row.value)
    if number is None or not reference:
        return None
    numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    if (
        allow_exact_match
        and row.kind == "clinical"
        and re.fullmatch(numeric, reference)
        and number == float(reference)
    ):
        # A printed scalar supports this exact match only. It does not say
        # whether a different result would be outside a clinical interval.
        return "within"
    match = _printed_interval_match(reference, numeric, r"([^\d;；]*)")
    if match:
        low, high, printed_unit = float(match[1]), float(match[2]), match[3].strip()
        unit_key = _literal_unit if row.kind == "clinical" else _key
        if printed_unit and unit_key(printed_unit) != unit_key(row.unit):
            return None
        return "within" if low <= number <= high else "outside"
    match = re.fullmatch(rf"\s*(<=|>=|<|>|≤|≥)\s*({numeric})\s*([^\d;；]*)", reference)
    if match:
        operator, boundary, printed_unit = match[1], float(match[2]), match[3].strip()
        unit_key = _literal_unit if row.kind == "clinical" else _key
        if printed_unit and unit_key(printed_unit) != unit_key(row.unit):
            return None
        comparisons = {"<": number < boundary, "<=": number <= boundary, "≤": number <= boundary,
            ">": number > boundary, ">=": number >= boundary, "≥": number >= boundary}
        return "within" if comparisons[operator] else "outside"
    return None


def _is_score_unit(unit: str | None) -> bool:
    # Keep symbols: the general alias normalizer erases '%' into an empty
    # string, but a percentage measurement is not an omitted scoring unit.
    return unicodedata.normalize("NFKC", unit or "").strip().casefold() in {
        "", "score", "scores", "point", "points", "分", "评分", "得分"}


def _source_legend_grade(row: _Observation) -> str | None:
    """Compare a function score with its own complete, literal source legend.

    No global cutoffs, endpoint ownership, percent conversion or missing band
    is supplied. Shared written endpoints therefore produce no unique grade.
    """
    if row.kind not in {"metric", "function"} or row.source_status is not None or not _is_score_unit(row.unit):
        return None
    number = _number(row.value)
    if number is None or not row.reference_range:
        return None
    text = unicodedata.normalize("NFKC", row.reference_range)
    numeric = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
    label = "|".join(sorted(map(re.escape, (*catalog.NUTRITION_DISPLAY_LEVELS, "强", "合成能力强", "合成能力弱")), key=len, reverse=True))
    bands = list(dict.fromkeys((match[1], float(match[2]), float(match[3])) for match in re.finditer(
        rf"({label})\s*[:：]\s*\(?\s*({numeric})\s*(?:-|–|—|~|～|至)\s*({numeric})\s*\)?", text)))
    if len(bands) < 2 or any(low >= high for _, low, high in bands):
        return None
    matching = [label for label, low, high in bands if low <= number <= high]
    return matching[0] if len(matching) == 1 else None


def _nutrition_score_grade(row: _Observation) -> str | None:
    """Use the approved report standard without rewriting the source grade."""
    if _alias(row, _OVERALL_ALIASES) or _alias(row, _HEALTH_ALIASES):
        return None  # Core structure scores have their own source methodology.
    paths = set(_pathways(row))
    # P9 describes harmful generation/virulence, not a beneficial nutrient
    # supply. A small score must never become a deficiency to be increased.
    # Its source grade/legend remains available through the P9 projection.
    if "P9" in paths:
        return None
    if row.kind not in {"metric", "function"} or _is_amr(row) or not (
            paths & {"P1", "P2", "P4", "P5", "P6", "P7", "P8"}
            or _alias(row, _MAJOR_ALIASES) or _nutrient_group(row) or _standard_term(row, _OTHER_METABOLITES)):
        return None
    # Nutrient/functional scores usually omit units. Physical units and percent
    # measurements are not interchangeable with a method's capability score.
    if not _is_score_unit(row.unit):
        return None
    if re.search(r"dietaryfiberneed|膳食纤维需求", _key((row.code or "") + row.name)):
        return None
    if row.source_status is not None and str(row.source_status).strip():
        return str(row.source_status)
    if row.catalog_family in {"GM", "G1"}:
        return _source_legend_grade(row)
    return catalog.template_nutrition_grade(row.value)


def _scfa_score_grade(row: _Observation) -> str | None:
    if row.kind not in {"metric", "function"} or not _alias(row, _SCFA_ALIASES):
        return None
    if not _is_score_unit(row.unit):
        return None
    if row.source_status is not None and str(row.source_status).strip():
        return str(row.source_status)
    if row.catalog_family in {"GM", "G1"}:
        return _source_legend_grade(row)
    number = _number(row.value)
    if number is not None and 60 < number <= 100:
        return catalog.template_indicator_grade("shortChainFattyAcidSynthesis", row.value)
    if row.source_status is not None:
        return None  # The printed label remains in sourceStatus, not a new calculation.
    source_grade = _source_legend_grade(row)
    if source_grade is not None:
        return source_grade
    if row.source_status is not None or row.reference_range:
        return None
    # An explicit synthesis label, a canonical Parsed function metric, or a
    # microbiome function/table projection establishes a score interpretation.
    score_source = row.source_type in {"microbiome", "microbiome_data"} or bool(re.search(
        r"合成|synthesis|score", _key((row.code or "") + row.name))) or any(
        re.match(r"parsed\.metrics\[|request\.microbiomeData\.(?:nutrition|nutrients|functional|function|metabolism)", source.record_id or "")
        for source in row.sources)
    number = _number(row.value)
    return "合成能力强" if score_source and number is not None and 60 < number <= 100 else None


def _pathway_state(row: _Observation, path: str) -> tuple[str | None, str | None]:
    status = _text(row.source_status)
    if status is not None and status.strip():
        return status, "来源标记"
    source_grade = _source_legend_grade(row)
    if source_grade is not None:
        return source_grade, "原报告区间对照"
    if path == "P3" and _scfa_score_grade(row):
        return _scfa_score_grade(row), "短链脂肪酸合成标尺"
    relation = _printed_reference_relation(row)
    if relation:
        return ("所印参考范围内" if relation == "within" else "所印参考范围外"), "来源参考范围对照"
    if path in {"P1", "P2", "P4", "P5", "P6", "P7", "P8"}:
        grade = _nutrition_score_grade(row)
        if grade:
            return grade, "营养合成/代谢能力标尺"
    return None, None


def _summarize_pathways(pathways, rows) -> None:
    for path, target in pathways.items():
        actual = [row for row in rows if row.kind in {"metric", "function"} and path in _pathways(row)]
        if not actual:
            continue
        for row, item in zip(actual, target.related_metrics):
            label, basis = _pathway_state(row, path)
            if label is not None:
                # The summary may accurately report the source's own label,
                # but that label must not overwrite an independently derived
                # method grade already displayed for this same observation.
                if item.assessment_level is None:
                    item.assessment_level = label
                    item._assessment_origin = {
                        "来源标记": "source",
                        "原报告区间对照": "reference",
                        "来源参考范围对照": "reference",
                        "短链脂肪酸合成标尺": "report",
                        "营养合成/代谢能力标尺": "report",
                    }.get(basis)
        # Component counts are evidence, not a pathway assessment. Preserve a
        # native overall label where supplied; otherwise the same report
        # composer fills a source-bound AI level without inventing a measured
        # pathway value or changing any component's original result.
        target.assessment_level = target.source_status


def _project_harmful_generation_aggregate(pathways, rows):
    """Bind the supplier's group result without grading its child scores."""
    target = pathways['P9']
    components = [row for row in rows if row.kind in {'metric', 'function'} and 'P9' in _pathways(row)]
    scopes = {(row.source_name, row.observed_at) for row in components}
    names = {'有害代谢物合成能力', '有害代谢物生成能力', '有害代谢物合成能力汇总', '有害代谢物生成能力汇总'}
    aggregates = [row for row in rows if row.kind == 'context' and row.category == 'structuralAssessment'
        and row.name in names and row.value is not None
        and (not scopes or (row.source_name, row.observed_at) in scopes)]
    for row in aggregates:
        item = row.metric()
        # Attach the literal source state only to this aggregate observation;
        # a missing original status stays missing even when value is text.
        status = _text(row.source_status)
        item.source_status = status
        item.assessment_level = status
        item._assessment_origin = 'source' if status is not None else None
        target.related_metrics.append(item)
        target.sources.extend(source.model_copy(deep=True) for source in row.sources if source not in target.sources)
    states = {item.source_status for item in target.related_metrics
        if item.name in names and item.source_status is not None}
    # A harmful-metabolite aggregate owns its own molecules, not separately
    # assayed virulence panels that widen this pathway's actual scope.
    virulence = any(row.kind == 'context' and row.category == 'virulence'
        and row.value is not None and row.sources for row in rows)
    if len(states) == 1 and not virulence:
        target.source_status = target.assessment_level = states.pop()
        target._assessment_origin = 'source'
    if aggregates:
        target.availability = 'available'


def _link_projected_pathways(data: public.EvaluationData) -> None:
    """Link only report sections containing the same actual source observations."""
    micro, nutrition = data.microbiome_assessment, data.nutrition_assessment
    collections = [
        ("microbiomeAssessment.overallStructure", micro.overall_structure),
        ("microbiomeAssessment.healthMetrics", micro.health_metrics),
        ("microbiomeAssessment.shortChainFattyAcids", micro.short_chain_fatty_acids),
        ("microbiomeAssessment.antimicrobialResistance", micro.antimicrobial_resistance),
        ("nutritionAssessment.majorMetabolism", nutrition.major_metabolism),
        ("nutritionAssessment.aminoAcidsAndMetabolites", nutrition.amino_acids_and_metabolites),
        ("nutritionAssessment.neurotransmitters", nutrition.neurotransmitters),
        ("nutritionAssessment.weakSynthesisChart", nutrition.weak_synthesis_chart),
    ]
    collections.extend(("nutritionAssessment.vitaminsAndMinerals", group.items)
        for group in nutrition.vitamins_and_minerals)
    collections.extend(("nutritionAssessment.detailedMetabolism", group.items)
        for group in nutrition.detailed_metabolism)

    def identities(items):
        result = set()
        for item in items:
            original = [source for source in item.sources if source.record_id
                and source.record_id.startswith(("parsed.", "request."))]
            # A derived fallback must never override distinct known input
            # positions, even when their names and literal values coincide.
            for source in original or item.sources:
                if not source.record_id:
                    continue
                locator = (source.source_code, source.record_id, source.observed_at)
                if original:
                    # These locators identify a literal input position. A
                    # derived function-fact ID can collide when two projects
                    # share a code/value, so it is not sufficient by itself.
                    result.add(("input", *locator))
                else:
                    result.add(("typed", *locator, item.name, type(item.value).__name__,
                        item.value, item.unit, item.reference_range, item.source_status))
        return result

    for pathway in data.functional_pathways.pathways:
        actual = identities(pathway.related_metrics)
        if not actual:
            continue
        for reference, items in collections:
            if reference not in pathway.cross_references and actual & identities(items):
                pathway.cross_references.append(reference)


def _normal_imaging_result(row: _Observation) -> bool:
    if not row.imaging or not isinstance(row.value, str):
        return False
    if _status_state(row.value) == "normal":
        if _qualitative_polarity(row.value) == "negative" or row.value.strip() == "未检出":
            return bool(re.search(r"影像|CT|MRI|MR\b|X线|超声|正侧位|正位片|侧位片", row.name, re.IGNORECASE))
        return True
    text = unicodedata.normalize("NFKC", row.value).strip()
    match = re.fullmatch(r"(.+?)未见(?:明显)?异常(?:X线改变|影像表现|CT表现|MR信号)?[。.!！]?", text, re.IGNORECASE)
    if not match:
        return False
    # Accept an explicit whole-result normal statement prefixed by this same
    # examination's name. Remove only modality/position words, not findings or
    # anatomical words; mixed prose such as "见结石，肝未见异常" cannot match.
    def examination_key(value):
        return re.sub(r"三维重建|x线|ct|mri|mr|dr|平扫|正侧位|正位|侧位|椎体|片|检查|结果|显示", "", _key(value))
    return bool(examination_key(row.name)) and examination_key(match[1]) == examination_key(row.name)


def _urine_microscopy_not_seen(name, value, unit=None) -> bool:
    """Recognize an explicit urine microscopy absence without making it numeric zero."""
    label = unicodedata.normalize("NFKC", str(name or "")).strip()
    result = unicodedata.normalize("NFKC", str(value or "")).strip()
    measure_unit = unicodedata.normalize("NFKC", str(unit or "")).strip()
    field = r"(?:个|细胞)?\s*/\s*(?:hpf?|高倍(?:镜)?视野)"
    return bool(
        re.fullmatch(r"尿(?:沉渣|液)?(?:红|白)细胞(?:\s*\((?:显微镜|镜检)\))?", label)
        and re.fullmatch(rf"(?:未见|未查见)(?:\s*{field})?", result, re.I)
        and (re.search(field, result, re.I) or re.fullmatch(field, measure_unit, re.I))
    )


def _normal_clinical_result(row: _Observation) -> bool:
    """Recognise complete negative source results, never a negative substring."""
    if row.kind != "clinical" or not isinstance(row.value, str):
        return False
    text = unicodedata.normalize("NFKC", row.value).strip().casefold()
    if re.fullmatch(r"(?:无明显异常|未见(?:明显)?异常|未发现异常|无异常)[。.!！]?", text):
        return True
    name = row.name.strip()
    result = text.rstrip("。.!！")
    if _urine_microscopy_not_seen(name, row.value, row.unit):
        return True
    if result == "无殊":
        return name in {"肺部听诊", "外耳", "外耳道", "鼓膜", "鼻腔", "鼻中隔", "扁桃体", "耳鼻咽喉科其它"}
    if result == "未查见":
        return name in {"蛔虫卵", "钩虫卵", "鞭虫卵", "吞噬细胞", "油滴", "夏雷登氏", "不消化食物"}
    if re.fullmatch(r"阴性|未检出|negative|neg|not\s+detected", result):
        return bool(re.fullmatch(r"(?:大便|粪便|尿)?(?:隐血|潜血)(?:试验)?|尿蛋白|尿糖|尿酮体", name))
    return (name, result) in {("肝脏触诊", "肋下未及"), ("肾脏叩诊", "无叩击痛")}


def _source_bound_problem_title(problem, source_values=()):
    """Recognise a workflow suffix only while retaining literal source text."""
    title = _text(_get(problem, "title")) or ""
    quotes = [_text(_get(source, "quote")) or ""
        for source in _get(problem, "sources", default=[]) or []]
    normalized = lambda text: re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    if any(normalized(title) in normalized(_text(value)) for value in [*quotes, *source_values]):
        return title
    # The organiser adds this exact suffix after validating a literal title.
    # Recognise it only for a suspected problem whose unmodified base remains
    # verbatim in its supplied quote; unrelated/invented titles gain no anchor.
    suffix = "（待核实）"
    if title.endswith(suffix):
        from modules.report.clinical_report import _denied_title
        base = title[:-len(suffix)]
        return base if (_get(problem, "certainty") == "suspected" and base
            and any(base in quote and not _denied_title(normalized(base), normalized(quote))
                for quote in quotes)) else title
    cleaned = re.sub(r"[，,；;、:：\s]*待(?:复核|核实|核对|核)$", "", title).rstrip("，,；;、:： ")
    return cleaned or title


def _apply_verified_imaging_problems(rows, clinical_document):
    """Classify source-bound image findings without upgrading their diagnosis.

    The organiser has already validated its problems. Repeat the literal
    source binding here because a source image may have no separate flag:
    a suspected mass is still an abnormal finding, rather than a normal test.
    Source facts, printed status, quotations and diagnostic certainty stay as
    supplied. A negated or full-normal finding cannot establish abnormality.
    """
    from modules.report.clinical_report import _denied_title
    normalized = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", _text(value) or ""))
    facts = {_get(fact, "fact_id", "factId"): fact
        for fact in _get(clinical_document, "facts", default=[]) or []}
    for problem in _get(clinical_document, "problems", default=[]) or []:
        if _get(problem, "certainty") not in {"suspected", "confirmed", "source_finding"}:
            continue
        title = _text(_get(problem, "title")) or ""
        for source in _get(problem, "sources", default=[]) or []:
            fact_id, quote = _get(source, "fact_id", "factId"), _text(_get(source, "quote")) or ""
            fact = facts.get(fact_id)
            bound_title = (_source_bound_problem_title(problem, [_get(fact, "value")])
                if title.endswith("（待核实）") else title)
            if (fact is None or _get(fact, "kind") != "clinical" or not quote or not title
                    or normalized(quote) not in normalized(_get(fact, "value"))
                    or normalized(bound_title) not in normalized(quote)
                    or _denied_title(normalized(bound_title), normalized(quote))):
                continue
            for row in rows:
                bound = fact_id in row.fact_ids or any(reference.record_id == fact_id for reference in row.sources)
                if (row.kind == "clinical" and row.imaging and _medical_system(row) == "影像科" and bound
                        and normalized(quote) in normalized(row.value) and not _normal_imaging_result(row)):
                    row.abnormal = True


def _medical_source_consistency(data: public.EvaluationData, row: _Observation) -> bool:
    """Return whether a row may enter the source-normal list, retaining conflicts."""
    status_state = _status_state(row.source_status)
    # A complete source result such as "未见明显异常" is an explicit normal
    # statement even when no separate status cell was supplied. Mixed imaging
    # prose and unknown text do not pass this whole-string classification.
    normal_imaging = _normal_imaging_result(row)
    relation = _printed_reference_relation(row)
    # Semi-quantitative occult-blood/urine screening results are explicit source
    # positives. Do not silently call 3+ normal merely because the status says so.
    positive_screen = _qualitative_polarity(row.value) == "positive" and bool(re.search(
        r"隐血|潜血|尿蛋白|尿糖|尿酮体|occultblood", _key(row.name)))
    reason = None
    if status_state == "normal" and (relation == "outside" or positive_screen):
        reason = "原值超出所附参考范围或呈明确阳性，本报告按实际结果判定异常；原正常/阴性标记同时保留为来源记录。"
    elif status_state == "abnormal" and relation == "within":
        reason = "数值处于所附参考范围内，但原报告另有异常标记；保留两项原始记录，结合已有异常信息列入关注。"
    if row.imaging and status_state == "normal" and not normal_imaging and reason is None:
        reason = "影像文字并非完整的正常或阴性结论，按实际影像所见进行评估；原正常标记保留，不概括为整个检查正常。"
    elif status_state == "abnormal" and normal_imaging:
        reason = "影像结论记为未见异常，另有原始异常标记；保留结论与标记，结合本次其他检查结果确定关注重点。"
    if relation == "outside" or positive_screen or status_state == "abnormal":
        row.abnormal = True
    if reason:
        first = row.sources[0] if row.sources else public.SourceReference(source_code="A")
        base = first.record_id or row.observation_id
        data.medical_assessment.consistency_notes.append(public.ConsistencyNote(
            code=row.observation_id, name=row.name, handling=reason,
            source_values=[public.SourceValue(source_code="A", record_id=base + ".value", observed_at=row.observed_at, value=row.value),
                public.SourceValue(source_code="A", record_id=base + ".sourceStatus", observed_at=row.observed_at, value=row.source_status),
                public.SourceValue(source_code="A", record_id=base + ".referenceRange", observed_at=row.observed_at, value=row.reference_range)],
            sources=copy.deepcopy(row.sources), availability="available"))
    missing_status = row.source_status is None or isinstance(row.source_status, str) and not row.source_status.strip()
    source_normal = normal_imaging or _normal_clinical_result(row) or relation == "within"
    return (status_state == "normal" or missing_status and source_normal) and not row.abnormal and reason is None


def _reference_range_consistency(data: public.EvaluationData, rows: list[_Observation]) -> None:
    """Keep disagreeing C-source ranges without choosing one source version."""
    def range_text(value):
        normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))
        normalized = re.sub(r"[–—−]", "-", normalized)
        return None if normalized in {"", "-", "--", "无", "暂无", "未提供"} else normalized

    groups = {}
    for row in rows:
        if (row.kind not in {"metric", "function", "context"} or _is_amr(row)
                or not row.source_name or not row.observed_at or row.value is None
                or range_text(row.reference_range) is None):
            continue
        canonical = next(((section, code) for section, aliases in (
            ("overall", _OVERALL_ALIASES), ("health", _HEALTH_ALIASES), ("scfa", _SCFA_ALIASES),
            ("major", _MAJOR_ALIASES), ("neurotransmitters", _NEURO_ALIASES))
            if (code := _alias(row, aliases)) is not None), None)
        if canonical is None:
            continue
        identity = (canonical, row.source_name, row.observed_at, _text(row.value),
            unicodedata.normalize("NFKC", row.unit or "").strip().casefold())
        groups.setdefault(identity, []).append(row)
    for identity, actual in groups.items():
        if len({range_text(row.reference_range) for row in actual}) < 2:
            continue
        data.medical_assessment.consistency_notes.append(public.ConsistencyNote(
            code="source-reference-" + _hash(identity), name=actual[0].name,
            handling="同一来源、日期与结果的参考范围记录不一致；保留各条原始范围及出处，需核对原始报告，未择一覆盖或据此新增分级。",
            source_values=[public.SourceValue(source_code="C",
                record_id=(row.sources[0].record_id if row.sources else row.observation_id) + ".referenceRange",
                observed_at=row.observed_at, value=row.reference_range) for row in actual],
            sources=copy.deepcopy(_combine_sources(actual)), availability="available"))


def _source_risk_grade(row: _Observation) -> str | None:
    """Use only a complete five-level legend printed with this observation."""
    number = _number(row.value)
    if number is None or not math.isfinite(number) or not row.reference_range or not _is_score_unit(row.unit):
        return None
    labels = [item['name'] for item in catalog.RISK_INDEX_STANDARDS]
    label = '|'.join(sorted(map(re.escape, labels), key=len, reverse=True))
    numeric = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'
    text = unicodedata.normalize('NFKC', row.reference_range)
    pattern = rf'(?P<label>{label})\s*[:：]\s*(?P<left>\[|\()?\s*(?P<low>{numeric})\s*(?:-|–|—|~|～|至|,)\s*(?P<high>{numeric})\s*(?P<right>\]|\))?'
    bands = list(dict.fromkeys((m['label'], float(m['low']), float(m['high']), m['left'], m['right'])
        for m in re.finditer(pattern, text)))
    if len(bands) != len(labels) or {band[0] for band in bands} != set(labels):
        return None
    ordered = sorted(bands, key=lambda band: band[1])
    if any(low >= high for _, low, high, _, _ in ordered) or any(
            before[2] != after[1] for before, after in zip(ordered, ordered[1:])):
        return None
    matches = [name for name, low, high, left, right in ordered
        if (number > low if left == '(' else number >= low)
        and (number < high if right == ')' else number <= high)]
    return matches[0] if len(matches) == 1 else None


def _risk_grade(row: _Observation) -> tuple[str | None, int]:
    """Preserve source labels; unknown scales never adopt template cutoffs."""
    reference = unicodedata.normalize("NFKC", row.reference_range or "")
    full_scale = 100 if (_literal_unit(row.unit) in {"%", "百分数", "百分制"}
        or re.search(r"(?:0\s*(?:-|–|—|~|～|至)\s*100|百分制|满分\s*100)", reference)) else None
    report_grade = catalog.template_risk_grade(row.value, full_scale=full_scale)
    if report_grade is not None:
        return report_grade, next(i for i, item in enumerate(catalog.RISK_INDEX_STANDARDS)
            if item['name'] == report_grade)
    status = _text(row.source_status)
    if status:
        normalized = _key(status)
        for labels, index in (({"中高风险", "中高", "moderatelyhigh", "moderatehigh"}, 3),
                ({"中低风险", "中低", "moderatelylow", "moderatelow"}, 1),
                ({"高风险", "高", "high", "highrisk"}, 4),
                ({"中风险", "中", "moderate", "medium", "mediumrisk"}, 2),
                ({"低风险", "低", "low", "lowrisk", "正常"}, 0)):
            if normalized in labels:
                return catalog.RISK_INDEX_STANDARDS[index]["name"], index
        return status, -1
    grade = _source_risk_grade(row)
    return (grade, next(i for i, item in enumerate(catalog.RISK_INDEX_STANDARDS)
        if item['name'] == grade)) if grade else (None, -1)


def _populate_risk_context(data, rows):
    """Bind actual context, leaving integrated category levels to the AI.

    Original disease model levels remain in their own rows. No model maximum
    is published or supplied to the writer as a clinical integrated judgement.
    """
    clinical_patterns = {
        "intestinal": r"胃肠|消化道|结肠|肠镜|胃镜|便血|(?:大便|粪便)隐血|溃疡性结肠炎|克罗恩",
        "metabolic": r"血压|血糖|糖化血红蛋白|胆固醇|甘油三酯|尿酸|体重指数|脂肪肝|肾结石|肾.*结石",
        "autoimmune": r"风湿|抗核抗体|自身免疫|过敏|哮喘|湿疹|免疫|多发性硬化|1型糖尿病|呼吸道感染",
        "aging": r"骨密度|骨质疏松|衰弱|肌少|动脉粥样硬化|认知下降",
        "mental": r"焦虑|抑郁|精神分裂|双相|孤独症|自闭症|睡眠呼吸暂停|睡眠障碍|失眠",
        "neurological": r"帕金森|阿尔兹|阿尔茨|癫痫|轻度认知障碍|神经",
        "tumor": r"肿瘤|恶性|癌|carcinoma|cancer|tumou?r|neoplasm",
    }
    for category in data.disease_risk_assessment.categories:
        models = [row for row in rows if row.kind == "disease" and _disease_category(row.name, code=row.code, category=row.category) == category.code]
        associations = [row for row in rows if row.kind == "context" and row.category == "healthAssociation"
            and _disease_category(row.name, code=row.code, category=row.category) == category.code]
        if not models:
            continue
        medical = [row for row in rows if row.kind == "clinical" and row.abnormal
            and re.search(clinical_patterns.get(category.code, r"(?!)"), row.name + " " + str(row.value or ""), re.I)]
        category.attention_level = category.attention_basis = None
        category.sources = _combine_sources([*models, *associations, *medical])
        category.availability = "partial"
        matrix = next(item for item in data.disease_risk_assessment.overview_matrix if item.code == category.code)
        matrix.attention_level = matrix.basis = None
        matrix.sources = copy.deepcopy(category.sources)
        matrix.availability = "partial"


def _source_group(row: _Observation) -> tuple[str | None, str | None, str | None]:
    return row.source_name, row.source_type, row.observed_at


def _observation_summary(row: _Observation, *, grade: str | None = None) -> str:
    value = _text(row.value)
    measurement = "" if value is None else "：" + value + (" " + row.unit if row.unit else "")
    status = grade if grade is not None else _text(row.source_status)
    label = "（" + status + "）" if status is not None and status.strip() else ""
    return row.name + measurement + label


def _medical_direction(rows: Iterable[_Observation]) -> str:
    actual = list(rows)
    advice = list(dict.fromkeys(row.description for row in actual
        if row.kind == "clinical" and row.description is not None and row.description.strip()))
    if advice:
        return "；".join(advice)
    names = "、".join(dict.fromkeys(row.name for row in actual))
    if any(row.imaging for row in actual):
        return f"按{names}的影像结论安排相应专科随诊，结合症状确定后续检查。"
    return f"优先关注{names}异常的持续性，在后续就诊时结合相关检查评估并随访变化。"


def _clinical_focus_summary(name, rows, action, *, certainty=None, source_quotes=()):
    """Explain an established clinical finding, reusing its existing action."""
    actual = [row for row in rows if row.kind == "clinical"]
    if any(_qualitative_polarity(row.value) == "positive" and re.search(
            r"隐血|潜血|尿蛋白|尿糖|尿酮体|occultblood", _key(row.name)) for row in actual):
        judgment = "属于明确阳性筛查线索，应优先评估其临床影响。"
    elif any(_printed_reference_relation(row) == "outside" for row in actual):
        outside = [row for row in actual if _printed_reference_relation(row) == "outside"]
        if all((row.clinical_data_type or '').upper() == 'INSPECTION'
                or _medical_system(row) == '检验科' for row in outside):
            kind = '检验'
        elif all(re.fullmatch(r'身高|体重|腰围|臀围|胸围|血压|脉搏|心率|体温|BMI|体质指数',
                row.name, re.I) for row in outside):
            kind = '测量'
        else:
            kind = '检查'
        judgment = f"已超出该项所附参考区间，属于明确的{kind}异常线索。"
    elif any(row.imaging and row.abnormal and _medical_system(row) == "影像科"
            and not _normal_imaging_result(row) for row in actual):
        if certainty == "suspected":
            qualifiers = list(dict.fromkeys(clause.strip() for quote in source_quotes
                for clause in re.split(r"[，,。；;\r\n]", quote)
                if clause.strip() and clause.strip() not in name
                and re.search(r"可能|疑似|考虑|待排|待查|不除外|不排除|不能排除", clause)))
            if qualifiers:
                name += "（" + "；".join(qualifiers[:2]) + "）"
        judgment = ("为已有影像中的异常发现，相关疾病性质仍保留疑似判断，临床关注优先于一般检验偏离。"
            if certainty == "suspected" else "为已有影像中的异常发现，需要纳入相应专科问题的持续管理。")
    elif any(row.abnormal for row in actual):
        judgment = "是已有医学检查中的异常发现，应纳入后续临床关注。"
    else:
        judgment = "已被医学资料记载，应结合后续症状与诊疗变化持续关注。"
    return name + "：" + judgment + action


def _nutrition_editorial_group(row: _Observation) -> tuple[str, str]:
    if _alias(row, _NEURO_ALIASES):
        return "P6", "神经递质合成"
    nutrient = _nutrient_group(row)
    if nutrient:
        return nutrient, dict(catalog.NUTRIENT_GROUP_DEFINITIONS)[nutrient]
    major = _alias(row, _MAJOR_ALIASES)
    if major:
        path = {"carbohydrate": "P1", "fat": "P2", "protein": "P4", "minerals": "minerals"}[major]
        return path, dict(catalog.PATHWAY_DEFINITIONS).get(path, "矿物质相关功能")
    path = next((code for code in _pathways(row) if code in {"P1", "P2", "P4", "P5", "P6", "P7", "P8"}), None)
    return (path, dict(catalog.PATHWAY_DEFINITIONS)[path]) if path else ("other", "其他营养功能")


def _taxon_editorial_group(row: _Observation) -> tuple[str, str]:
    names = {"beneficial": "有益菌", "pathogenic": "致病菌", "opportunistic": "条件致病菌", "other": "其他菌群"}
    category = row.category or "unclassified"
    # Group by the source's own direction, not a presumed biological role.
    status = _text(row.source_status) or ""
    state = "偏低或缺乏" if status in {"缺乏", "偏低", "低于参考", "low"} else (
        "偏高或超标" if status in {"超标", "偏高", "高于参考", "high"} else "异常标记")
    return category + ":" + state, names.get(category, "来源分类为" + category if row.category else "未分类菌群") + "来源" + state + "项"


def _clinical_display_title(problem, actual):
    """Remove generated workflow suffixes while preserving literal uncertainty."""
    return _source_bound_problem_title(problem, [row.value for row in actual])


def _focus_identity(row, grade):
    if row.kind in {"taxon", "microbe"} and row.scientific_name:
        return (_key(row.scientific_name), grade(row), _text(row.value), row.unit,
            row.reference_range, _source_group(row),
            None if row.source_name else row.observation_id)
    return (_key(row.name), grade(row))


def _focus_representatives(rows, grade):
    """Select the same two source-specific representatives for prose reuse."""
    selected = {}
    for row in rows:
        selected.setdefault(_focus_identity(row, grade), row)
    return list(selected.values())[:2]


def _focus_labels(rows, grade):
    """Name at most two real representatives, keeping their literal grades."""
    return "、".join(row.name + (f"（{grade(row)}）" if grade(row) else "")
        for row in _focus_representatives(rows, grade))


def _nutrition_focus_summary(actual, peers, group_name):
    low = _focus_labels(actual, _nutrition_score_grade)
    normal = _focus_labels([row for row in peers if _nutrition_score_grade(row) == "正常"], _nutrition_score_grade)
    text = group_name + "相关菌群功能的较低项为" + low + "；"
    text += ("同组" + normal + "处于正常档，关注重点可收敛于前述低项。" if normal
        else "关注重点集中在上述已记录的较低功能项。")
    return text


def _taxon_focus_summary(actual, peers, *, include_comparison=True):
    category = {"beneficial": "有益菌", "pathogenic": "致病菌", "opportunistic": "条件致病菌", "other": "其他菌群"}.get(
        actual[0].category, "来源菌群分类")
    direction = _taxon_editorial_group(actual[0])[0].partition(":")[2]
    opposite = {"偏低或缺乏": "偏高或超标", "偏高或超标": "偏低或缺乏"}.get(direction)
    opposite_rows = [row for row in peers if opposite and _taxon_editorial_group(row)[0].endswith(":" + opposite)]
    normal_rows = [row for row in peers if _status_state(row.source_status) == "normal"]
    label = lambda row: _text(row.source_status)
    text = category + ("异常标记的代表为" if direction == "异常标记" else "已标记" + direction + "的代表为") + _focus_labels(actual, label) + "；"
    if not include_comparison:
        return text[:-1] + "。"
    if opposite_rows:
        text += "同分类还记录" + _focus_labels(opposite_rows, label) + "，呈现高低并存，应按菌项分别观察。"
    elif normal_rows:
        text += "同分类" + _focus_labels(normal_rows, label) + "处于正常档，关注应集中在已标记偏离的菌项。"
    else:
        text += "本组关注集中在这些已有异常标记的菌项。"
    return text


def _taxon_direction_basis(actual, rows):
    """Keep each category's first comparison and only new later-direction facts.

    Core rows, measurements and source relations stay separate. Deduplication
    here applies only to the representative names already used in this basis.
    """
    scopes = {}
    for row in actual:
        scope = (_source_group(row), row.category)
        scopes.setdefault(scope, {}).setdefault(_taxon_editorial_group(row)[0], []).append(row)
    texts = []
    grade = lambda row: _text(row.source_status)
    identity = lambda row: _focus_identity(row, grade)
    for (source, category), groups in scopes.items():
        primary, *later = groups.values()
        peers = [row for row in rows if row.kind in {"taxon", "microbe"}
            and _source_group(row) == source and row.category == category]
        texts.append(_taxon_focus_summary(primary, peers))
        represented = {identity(row) for row in _focus_representatives(primary, grade)}
        direction = _taxon_editorial_group(primary[0])[0].partition(":")[2]
        opposite = {"偏低或缺乏": "偏高或超标", "偏高或超标": "偏低或缺乏"}.get(direction)
        comparison = [row for row in peers if opposite and _taxon_editorial_group(row)[0].endswith(":" + opposite)]
        if not comparison:
            comparison = [row for row in peers if _status_state(row.source_status) == "normal"]
        represented.update(identity(row) for row in _focus_representatives(comparison, grade))
        for group in later:
            new = [row for row in _focus_representatives(group, grade) if identity(row) not in represented]
            if new:
                texts.append(_taxon_focus_summary(new, peers, include_comparison=False))
                represented.update(identity(row) for row in new)
    return "\n".join(texts)


def _reported_attention(row):
    """Goals, negatives and explicitly settled adaptation are background."""
    if row.kind != 'self_reported':
        return True
    from .evaluation_fixed_assessment import _answer, _questionnaire_question, _goal_only_question
    text = _answer(row)
    if isinstance(row.value, bool) and not row.value or re.fullmatch(r'false|否|无|没有|0', text, re.I):
        return False
    if re.search(r'家族|血亲', _questionnaire_question(row)) or _goal_only_question(row):
        return False
    if re.search(r'分离不安|入园.{0,5}不安', text) and re.search(r'可被安抚|可安抚|能安抚', text):
        return False
    return True


_NASAL_EXAMS = r'鼻部|鼻腔|鼻中隔|鼻黏膜|鼻粘膜|鼻甲'
_NASAL_SYMPTOMS = r'鼻塞|打喷嚏|喷嚏|流鼻涕|清涕|鼻痒|鼻部不适'
_NASAL_DIRECTION = '鼻部表现关注'
_BOWEL_DIRECTION = '排便困难与便秘相关不适'
_BOWEL_SYMPTOMS = r'便秘|排便|粪便|大便|硬便|硬块便'


def _hard_stool_source(row):
    from .evaluation_fixed_assessment import _affirmative
    return bool(row.kind == 'clinical'
        and re.fullmatch(r'(?:粪便|大便)?(?:性状|形态)', row.name)
        and (row.name.startswith(('粪便', '大便')) or '便' in (_clinical_specimen_scope(row) or ''))
        and _status_state(row.source_status) != 'normal'
        and re.search(r'偏硬|硬块|硬便|干硬', _text(row.value) or '')
        and _affirmative(row, pattern=r'偏硬|硬块|硬便|干硬'))


def _direction_support_rows(key, actual, rows):
    """Link adopted facts for one attention goal, never the whole survey.

    The organiser may omit an already measured supporting stool record from
    its problem list. Keep that record available to the same bowel goal;
    nasal follow-up may likewise use only affirmative nasal B statements.
    Original records, values and classifications are never combined here.
    """
    from .evaluation_fixed_assessment import _answer, _affirmative, _goal_clause, _questionnaire_question
    bowel = key == ('reportedDigestive', 'bowel')
    nasal = any(row.kind == 'clinical' and re.search(_NASAL_EXAMS, row.name)
        for row in actual)
    nasal = nasal or key == ('medicalNasal', 'nasal')
    if not (bowel or nasal):
        return actual
    pattern = (r'便秘|排便费力|排便困难|硬便|硬块便|粪便偏硬|排便.{0,5}用力|每周(?:排便)?约[012一二两]次'
        if bowel else _NASAL_SYMPTOMS)
    result = list(actual)
    seen = {row.observation_id for row in actual}
    for row in rows:
        if row.observation_id in seen:
            continue
        measured_stool = bowel and _hard_stool_source(row)
        question = _questionnaire_question(row)
        topic_supported = (not bowel or re.search(r'排便|大便|便秘', question)
            or re.search(r'便秘|排便费力|排便困难|硬便|硬块便|粪便偏硬|排便.{0,5}用力', _answer(row)))
        reported = (row.kind == 'self_reported' and _reported_attention(row)
            and not re.search(r'家族|血亲|亲属|家人|父亲|母亲|爸爸|妈妈|父母|family.?history', question, re.I)
            and topic_supported
            and _affirmative(row, pattern=pattern)
            and (row.value is True and re.search(pattern, question)
                or any(re.search(pattern, part) and not _goal_clause(part)
                    for part in re.split(r'[，,。；;\n]', _answer(row)))))
        if measured_stool or reported:
            seen.add(row.observation_id)
            result.append(row)
    return result


def direction_clinical_scope(direction, document):
    """Return exact source excerpts for one action, without new facts.

    A compound questionnaire answer remains unchanged in the document. Only
    its literal topic clauses are authorised for this specific action; a
    shared fact ID cannot authorise an unrelated symptom in the same answer.
    """
    topic = {_NASAL_DIRECTION: 'nasal', '鼻塞、喷嚏与鼻黏膜异常': 'nasal',
        _BOWEL_DIRECTION: 'bowel'}.get(_get(direction, 'name'))
    if topic is None:
        return None
    from .evaluation_fixed_assessment import (
        _answer, _affirmative, _goal_clause, _goal_only_question, _questionnaire_question,
    )
    symptoms = _NASAL_SYMPTOMS if topic == 'nasal' else _BOWEL_SYMPTOMS
    unrelated = (r'排便|便秘|粪便|硬便|反酸|腹胀|腹泻' if topic == 'nasal'
        else _NASAL_EXAMS + '|' + _NASAL_SYMPTOMS + r'|反酸|烧心|呕吐')
    known = {_get(fact, 'fact_id', 'factId'): fact
        for fact in _get(document, 'facts', default=[]) or []}
    verified = {}
    for problem in _get(document, 'problems', default=[]) or []:
        for source in _get(problem, 'sources', default=[]) or []:
            fid, quote = _get(source, 'fact_id', 'factId'), _text(_get(source, 'quote')) or ''
            if fid in known and quote and quote in (_text(_get(known[fid], 'value')) or ''):
                verified.setdefault(fid, []).append(quote)
    entries, seen = [], set()
    for source in _get(direction, 'sources', default=[]) or []:
        fid = _get(source, 'record_id', 'recordId')
        if not fid or fid in seen or fid not in known:
            continue
        seen.add(fid)
        fact = known[fid]
        kind, name = _get(fact, 'kind'), _text(_get(fact, 'name')) or ''
        value = _text(_get(fact, 'value')) or ''
        nasal_exam = topic == 'nasal' and (re.search(_NASAL_EXAMS, name)
            or any(re.search(_NASAL_EXAMS, quote) for quote in verified.get(fid, [])))
        hard_stool = (topic == 'bowel' and re.fullmatch(r'(?:粪便|大便)(?:性状|形态)', name)
            and _status_state(_get(fact, 'status')) != 'normal'
            and re.search(r'偏硬|硬块|硬便|干硬', value))
        if kind == 'clinical' and (nasal_exam or hard_stool):
            original = verified.get(fid, []) or [value]
            parts = [part.strip() for quote in original for part in re.split(r'[，,。；;\n]', quote) if part.strip()]
            quotes = [part for part in parts if re.search(
                _NASAL_EXAMS if topic == 'nasal' else r'偏硬|硬块|硬便|干硬', part)] or [value]
        elif kind == 'self_reported':
            question = _questionnaire_question(fact)
            if (_goal_only_question(fact)
                    or re.search(r'家族|血亲|亲属|家人|父亲|母亲|爸爸|妈妈|父母|family.?history', question, re.I)):
                continue
            quotes = [part.strip() for part in re.split(r'[，,。；;\n]', _answer(fact))
                if part.strip() and re.search(symptoms, part) and not _goal_clause(part)
                and _affirmative({'name': name, 'value': part}, pattern=symptoms)]
        else:
            continue
        quotes = list(dict.fromkeys(quote for quote in quotes if quote and quote in value
            and not re.search(unrelated, quote)))
        if quotes:
            entries.append({'factId': fid, 'quotes': quotes})
    if not entries:
        return None
    return {'semanticType': 'clinicalDirectionScope', 'topic': topic,
        'factIds': [entry['factId'] for entry in entries], 'allowedSourceQuotes': entries}


def _ordered_findings(data, rows, clinical_document):
    """Prioritize real problems, grouping editorial focus without losing detail."""
    candidates = []
    covered_ids = set()
    by_fact = {}
    problems = _get(clinical_document, "problems", default=[]) or []
    def add(sort, name, module, level, summary, actual, direction_key, direction_name, action):
        nasal = (module == '医学评估' and not re.search(r'便秘|排便|粪便|硬便', name)
            and (re.search(_NASAL_EXAMS + '|' + _NASAL_SYMPTOMS, name)
                or any(row.kind == 'clinical' and re.search(_NASAL_EXAMS, row.name) for row in actual)))
        if nasal:
            direction_key, direction_name = ('medicalNasal', 'nasal'), _NASAL_DIRECTION
            if not any(row.description for row in actual):
                action = '关注已记录鼻部表现的变化，记录持续或加重情况，作为后续专业沟通的依据。'
        candidates.append(dict(sort=sort, name=name, module=module, level=level,
            summary=summary, actual=actual, direction_key=direction_key,
            direction_name=direction_name, action=action))

    for row in rows:
        for fact_id in row.fact_ids:
            by_fact.setdefault(fact_id, []).append(row)
        for source in row.sources:
            if source.record_id:
                by_fact.setdefault(source.record_id, []).append(row)
    for order, problem in enumerate(problems):
        problem_sources = _get(problem, "sources", default=[]) or []
        fact_ids = [_get(s, "fact_id", "factId") for s in problem_sources]
        actual = list({r.observation_id: r for fid in fact_ids for r in by_fact.get(fid, [])}.values())
        actual = [row for row in actual if row.kind != "clinical" or (
            not is_ungraded_medical_record(row)
            and not (_status_state(row.source_status) == "normal" and not row.abnormal))]
        actual = [row for row in actual if _reported_attention(row)]
        if not actual:
            continue
        covered_ids.update(r.observation_id for r in actual)
        certainty = _get(problem, "certainty")
        for item in (*data.medical_assessment.findings, *data.medical_assessment.normal_findings):
            if any(source.record_id in fact_ids for source in item.sources):
                if certainty == 'self_reported' and any(source.source_code == 'A' for source in item.sources):
                    continue  # An organizer label cannot relabel a measured A result as questionnaire data.
                # One examination may contain several distinct problems.
                # Keep its conservative suspected state even when another
                # problem on the same record is only a source finding.
                # Individual problem certainties remain untouched.
                if certainty == "suspected" or item.certainty != "suspected":
                    item.certainty = certainty
        quotes = list(dict.fromkeys(_get(s, "quote") for s in problem_sources if _get(s, "quote") is not None))
        priority = _get(problem, "priority", default="review")
        name = _clinical_display_title(problem, actual)
        action = _medical_direction(actual)
        summary = (_clinical_focus_summary(name, actual, action, certainty=certainty, source_quotes=quotes)
            if any(row.kind == "clinical" for row in actual)
            else "；".join(quotes) or "；".join(_observation_summary(row) for row in actual))
        direction_key = ("medical", tuple(sorted(r.observation_id for r in actual)))
        direction_name = name
        clinical = [row for row in actual if row.kind == 'clinical']
        stool_shape = bool(clinical) and all(_hard_stool_source(row) for row in clinical)
        if (all(row.kind == 'self_reported' for row in actual) and re.search(
                r'便秘|排便|硬便|硬块便|粪便偏硬', name + ' ' + ' '.join(quotes))) or stool_shape:
            direction_key, direction_name = ('reportedDigestive', 'bowel'), '排便困难与便秘相关不适'
            if not any(row.description for row in actual):
                action = '围绕排便频率、便质及用力情况关注变化，结合已提供的饮食、饮水与活动线索开展后续健康管理。'
        add(({"priority": 0, "review": 1, "routine": 2}.get(priority, 2), order), name, "医学评估",
            {"priority": "优先关注", "review": "需要关注", "routine": "常规关注"}.get(priority, "需要关注"),
            summary, actual, direction_key, direction_name, action)
    grouped = {}
    for order, row in enumerate(rows):
        if row.observation_id in covered_ids:
            continue
        if row.kind == "clinical" and row.abnormal:
            key = ("medical", _key(row.name), _source_group(row))
        elif row.kind in {"taxon", "microbe"} and (row.abnormal or _status_state(row.source_status) == "abnormal"
                or _status_state(row.source_status) != "normal" and "缺乏" in (_text(row.source_status) or "")):
            key = ("microbiome", _taxon_editorial_group(row)[0], _source_group(row))
        elif _is_amr(row) and row.value is not None and re.search(r'耐药|注意|异常|resistan', _text(row.source_status) or '', re.I):
            key = ("resistance", "sourceResistance", _source_group(row))
        elif row.kind == "disease":
            _, grade = _risk_grade(row)
            # Low/moderately-low model rows remain in the full disease table;
            # they do not each become a report-wide priority.
            if grade < 2:
                continue
            key = ("disease", _disease_category(row.name, code=row.code, category=row.category) or "source:" + _key(row.name), _source_group(row))
        elif _nutrition_score_grade(row) in catalog.LOW_FUNCTION_LEVELS:
            key = ("nutrition", _nutrition_editorial_group(row)[0], _source_group(row))
        else:
            continue
        grouped.setdefault(key, []).append((order, row))

    compared_taxon_scopes = set()
    for (kind, group, source), members in grouped.items():
        actual = [row for _, row in members]
        first_order = members[0][0]
        names = "、".join(dict.fromkeys(row.name for row in actual))
        if kind == "medical":
            name = actual[0].name
            level = "异常"
            action = _medical_direction(actual)
            add((1, len(problems) + first_order), name, "医学评估", level,
                _clinical_focus_summary(name, actual, action), actual,
                (kind, group, source), name + "异常随访", action)
        elif kind == "microbiome":
            name = actual[0].name if len(actual) == 1 else _taxon_editorial_group(actual[0])[1]
            statuses = list(dict.fromkeys(_text(row.source_status) for row in actual if row.source_status is not None))
            action = "持续观察已标记的菌群偏离方向及肠道状态变化，对照后续同类检测结果。"
            peers = [row for row in rows if row.kind in {"taxon", "microbe"}
                and row.category == actual[0].category and _source_group(row) == source]
            scope = (source, actual[0].category)
            summary = _taxon_focus_summary(actual, peers, include_comparison=scope not in compared_taxon_scopes)
            compared_taxon_scopes.add(scope)
            add((4, first_order), name, "肠道菌群评估", "、".join(statuses) or None,
                summary, actual, (kind, source), "菌群结构与肠道状态改善", action)
        elif kind == "disease":
            actual.sort(key=lambda row: (-_risk_grade(row)[1], -(_number(row.value) or 0)))
            category = dict(catalog.RISK_CATEGORY_DEFINITIONS).get(group)
            name = actual[0].name if len(actual) == 1 else (category or "来源模型") + "风险提示"
            level = _risk_grade(actual[0])[0]
            if len(actual) > 1:
                level = "最高来源模型分级：" + level
            action = f"持续关注{names}来源模型风险提示及其后续变化。"
            summary = ("来源模型分级：" + _focus_labels(actual, lambda row: _risk_grade(row)[0])
                + "，关注重点集中在上述模型项。")
            add((5, -_risk_grade(actual[0])[1], first_order), name, "疾病风险评估", level,
                summary, actual, (kind, group, source), (category or names) + "风险管理",
                action)
        elif kind == "resistance":
            add((7, first_order), "耐药基因背景", "肠道菌群评估", None,
                "来源耐药基因记录：" + "；".join(_observation_summary(row) for row in actual), actual,
                (kind, source), "耐药基因背景",
                "后续接受抗感染诊疗时，向医生提供原始耐药基因报告。")
        else:
            _, group_name = _nutrition_editorial_group(actual[0])
            name = actual[0].name if len(actual) == 1 else group_name + "能力弱项"
            grades = list(dict.fromkeys(_nutrition_score_grade(row) for row in actual))
            action = f"持续关注{group_name}中已列较低功能项的变化，对照后续同类结果。"
            peers = [row for row in rows if _source_group(row) == source and _nutrition_editorial_group(row)[0] == group
                and _nutrition_score_grade(row) is not None]
            summary = _nutrition_focus_summary(actual, peers, group_name)
            numeric = [value for row in actual if (value := _number(row.value)) is not None]
            # A source may supply both a qualitative abnormal group and
            # numerical weak members. Missing numbers sort after measured
            # values; they never acquire an invented score or suppress zero.
            add((6, min(numeric) if numeric else float('inf'), first_order), name, "营养功能评估", "、".join(grades),
                summary, actual, (kind, "nutrition"), "营养与代谢功能关注", action)

    directions = {}
    conflict_ids = {source.record_id for note in data.medical_assessment.consistency_notes for source in note.sources}
    for rank, candidate in enumerate(sorted(candidates, key=lambda item: item["sort"]), 1):
        sources = _combine_sources(candidate["actual"])
        scoped = [row for row in candidate["actual"]
            if row.kind == "clinical" and _has_distinct_medical_scope(row, rows)]
        if scoped:
            details = list(dict.fromkeys(row.name + "（" + (_clinical_specimen_scope(row) or "标本未注明")
                + "） " + str(row.value) + (" " + row.unit if row.unit else "") for row in scoped))
            candidate["summary"] = "；".join(details) + "。" + candidate["summary"]
        data.health_overview.key_findings.append(public.OverviewFinding(rank=rank, name=candidate["name"], module=candidate["module"],
            assessment_level=candidate["level"], summary=candidate["summary"], sources=copy.deepcopy(sources), availability="available"))
        direction_key = candidate["direction_key"]
        actual = candidate["actual"]
        if (candidate["module"] == "医学评估" and actual
                and len({_key(row.specimen) for row in actual}) == 1
                and all(row.kind == "clinical" and row.clinical_data_type == "INSPECTION"
                    and not row.imaging and not row.description
                    and _medical_system(row) == "检验科"
                    and not re.search(r"^尿(?:比重|蛋白|液|常规|沉渣)|粪便|大便|血(?:液|清|浆)|全血", row.name)
                    and not any(source.record_id in conflict_ids for source in row.sources)
                    and (_number(row.value) is not None or _qualitative_polarity(row.value) == "positive")
                    for row in actual)):
            # This combines an editorial follow-up goal, never the distinct
            # measurements, dates, priorities or original advice themselves.
            direction_key = ("medicalLaboratory", _key(actual[0].specimen))
        direction = directions.setdefault(direction_key, dict(name=candidate["direction_name"], summaries=[], actual=[], actions=[], priority=False))
        direction["priority"] = (direction["priority"] or candidate["level"] == "优先关注"
            or str(candidate["action"] or '').startswith("优先关注"))
        basis_summary = candidate["summary"]
        if candidate["action"] and basis_summary.endswith(candidate["action"]):
            basis_summary = basis_summary[:-len(candidate["action"])]
        direction["summaries"].append(basis_summary)
        direction["actual"].extend(candidate["actual"])
        direction["actions"].append(candidate["action"])
    for rank, (key, direction) in enumerate(directions.items(), 1):
        direction['actual'] = _direction_support_rows(key, direction['actual'], rows)
        action = "；".join(dict.fromkeys(direction["actions"]))
        basis = "；".join(dict.fromkeys(direction["summaries"]))
        if key == ("nutrition", "nutrition"):
            themes = "、".join(dict.fromkeys(_nutrition_editorial_group(row)[1] for row in direction["actual"]))
            basis = themes + "存在较低分档的合成或代谢项目，应作为营养与功能改善的优先方向。"
            action = "持续关注" + themes + "中已列较低合成或代谢项目的变化，并与后续同类结果对照。"
        elif key[0] == "microbiome":
            basis = _taxon_direction_basis(direction["actual"], rows)
        elif key[0] == "medicalLaboratory" and len(direction["summaries"]) > 1:
            actual = list({row.observation_id: row for row in direction["actual"]}.values())
            names = "、".join(dict.fromkeys(row.name for row in actual))
            direction["name"] = names + "检验异常随访"
            clauses = []
            outside = [row for row in actual if _printed_reference_relation(row) == "outside"]
            positive = [row for row in actual if _qualitative_polarity(row.value) == "positive"]
            other = [row for row in actual if row not in outside and row not in positive]
            for matched, meaning in ((outside, "超出各自所附参考区间"),
                    (positive, "呈明确阳性"), (other, "存在已列异常表现")):
                if matched:
                    clauses.append("、".join(dict.fromkeys(row.name for row in matched)) + meaning)
            basis = "已有检验记录中，" + "；".join(clauses) + "。"
            action = (("优先关注" + names + "异常的持续性，在后续就诊时结合相关检查评估并随访变化。")
                if direction["priority"] else "后续对照各项原值与参考范围观察复查结果，评估这些异常是否持续。")
        data.comprehensive_conclusion.priority_directions.append(public.PriorityDirection(rank=rank,
            name=direction["name"], basis=basis, followup_action=action,
            sources=_combine_sources(direction["actual"]), availability="available"))
    if candidates:
        data.comprehensive_conclusion.availability = "partial"


def _trigger_answer_result(row):
    """Keep the answer, without rendering the source's question as a result."""
    answer = _text(row.value)
    if answer is None:
        return None
    if (row.kind == "context" and _get(row, "category") == "sourceReportedBackground"
            or row.kind == 'clinical' and str(_get(row, 'category') or '').startswith('clinicalReportedBackground:')):
        # Original-report history is not a questionnaire prompt. Its label
        # can be the only place naming the drug, symptom or time period.
        return row.name + "：" + answer
    # A derived self-report fact may wrap the complete original Q/A. Only
    # remove this exact wrapper; punctuation within the answer is preserved.
    wrapped = re.fullmatch(r"问题[:：].*?[；;]回答[:：](.*)", answer, re.S)
    if wrapped:
        answer = wrapped[1]
    from .evaluation_fixed_assessment import _questionnaire_display, _questionnaire_question
    if (type(row.value) in (int, float) and re.search(r'药物.*种类|用药.*种', row.name)
            or re.search(r'(?:吸烟|抽烟).{0,20}支', _questionnaire_question(row))
            and re.fullmatch(r'[-+]?\d+(?:\.\d+)?\s*支?', answer.strip())):
        return _questionnaire_display(row)
    units = r"毫升|mL|升|L|分钟|min|小时|h|天|次|支|包|杯|瓶|罐|毫克|mg|克|g"
    bare_number = re.fullmatch(r"[-+]?\d+(?:\.\d+)?", answer.strip())
    numeric_answer = re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:\s*(?:" + units + r"))?", answer.strip(), re.I)
    short_measurement = (len(answer) <= 35 and re.search(r"\d.*(?:" + units + r")", answer, re.I))
    if type(row.value) in (bool, int, float) or numeric_answer or short_measurement or re.fullmatch(
            r"(?:是|否|有|无|没有|不详|不清楚|未知|true|false)", answer.strip(), re.I):
        # A bare false/0/unknown answer still needs its actual subject. Take
        # only a subject literally present in the question, never infer one
        # from the whole trigger dimension (e.g. smoking is not all exposure).
        subject = re.search(r"二手烟|吸烟|抽烟|饮酒|喝酒|饮水|喝水|甜食|含糖饮料|夜宵|蔬菜|早餐|晚餐|连续坐着|连续坐|久坐|运动|睡眠|睡觉|睡多长时间|睡多久|每天睡|压力|放松|抗生素|用药|药物|家族史|家族病史|血亲|食物不耐受|食物过敏", row.name)
        if subject:
            # Preserve the question's explicit window and unit for a short
            # numeric/unknown answer. These are literal labels, not inferred
            # frequency, elapsed time, intake, or a converted measurement.
            windows = re.findall(r"(?:体检|检查|检测)前\s*[\d一二三四五六七八九十半两]+\s*(?:个)?(?:年|月|周|天|日)|临睡前|睡前|(?:最近|近|过去|未来)\s*[\d一二三四五六七八九十百半两]+\s*(?:个)?(?:年|月|周|天|日)|"
                r"每(?:年|月|周|天|日|次)|工作日|周末|(?:一|1)周", row.name)
            unit = _get(row, "unit")
            if not unit:
                question_unit = re.search(r"(?:多少|几)\s*(" + units + r")|[（(]\s*(" + units + r")\s*[）)]", row.name, re.I)
                unit = next((value for value in question_unit.groups() if value), None) if question_unit else None
            if bare_number and unit:
                answer += str(unit)
            label = subject[0]
            if re.search(r"甜食|含糖饮料", row.name):
                label = "甜食或含糖饮料" if "甜食" in row.name and "含糖饮料" in row.name else label
            elif re.search(r"睡多长时间|睡多久|每天睡", row.name):
                label = "睡眠时间"
            return "".join(dict.fromkeys(windows)) + label + "：" + answer
        if not re.search(r"[?？]|是否|有无|怎样|如何|多少|几次|几点|您|你", row.name):
            return row.name + "：" + answer
    return answer


def _questionnaire_triggers(data, rows):
    for trigger in data.medical_assessment.trigger_analysis:
        pattern = {
            "diet": r"饮食|外卖|蔬菜|水果|含糖饮料|饮水|喝水|喝多少水|晚餐|早餐|夜宵|乳制品|奶类|diet|foodintake|mealpattern|waterintake|latemeals",
            "exercise": r"运动|久坐|连续坐|exercise|sedentary",
            "sleep": r"睡眠|睡觉|几点睡|睡多长|睡多久|每天睡|熬夜|作息|sleep",
            "stress": r"压力|焦虑|心理|情绪|入园适应|家庭互动|放松|stress",
            "medications": r"用药|药物|抗生素|补充剂|medication|antibiotic",
            "exposures": r"吸烟|饮酒|环境|暴露|smok|alcohol|exposure",
            "foodIntolerance": r"食物不耐受|foodintolerance",
            "familyHistory": r"家族(?:疾病|病)?史|血亲|遗传|familyhistory",
        }[trigger.code]
        actual = [r for r in rows if r.value is not None and (
            r.kind == 'clinical' and r.category == 'clinicalReportedBackground:' + trigger.code
            or (r.kind == "self_reported" or r.kind == "context" and r.category == "sourceReportedBackground")
                and (re.search(pattern, _key((r.code or "") + r.name))
                    or trigger.code == 'foodIntolerance' and re.search(r'奶|乳制品', r.name)
                    and re.search(r'饮奶后|喝奶后|乳制品.{0,8}(?:腹泻|腹胀|不适|耐受)', _text(r.value) or '')))
            and not (trigger.code == 'medications' and re.search(r'过敏|不耐受', r.name))]
        if actual:
            trigger.observations = "；".join(dict.fromkeys(_trigger_answer_result(r) for r in actual))
            trigger.sources = _combine_sources(actual)
            trigger.availability = "available"


def _medical_system(row):
    """Classify an explicitly named examination, without reading its result."""
    name = _key(row.name)
    display_name = unicodedata.normalize("NFKC", row.name)
    if re.search(r"(?<![a-z])(?:ct|mri)(?![a-z])|磁共振|x线|正侧位|超声|彩超|钼靶|骨密度", display_name, re.I):
        return "影像科"
    for pattern, system in (
        (r"裸眼视力|矫正视力|眼压|眼底|视力检查", "眼科"),
        (r"^(?:外耳|外耳道|鼓膜|鼻腔|鼻中隔|鼻部检查|扁桃体|咽|喉)$|耳鼻咽喉科|耳鼻喉科", "耳鼻咽喉科"),
        (r"心电图|动态心电", "心电检查"),
        (r"胃镜|肠镜|内镜", "内镜检查"),
        (r"^(?:体重|身高|体重指数|bmi|腰围|臀围|血压|脉搏|体温)$", "一般检查"),
        (r"体格检查|腹部查体|肺部听诊|心脏听诊|肝脏触诊|肾脏叩诊", "内科查体"),
        (r"神经反射", "神经系统查体"),
        (r"红细胞|白细胞|嗜酸性粒细胞|嗜碱性粒细胞|上皮细胞|脓细胞|血红蛋白|血小板|酵母菌|酮体|浊度|^比重$|^颜色$|胆红素|尿胆原|^ph值$|管型|结晶|白细胞脂酶|^蛋白质$|粘液丝|黏液丝|亚硝酸盐|葡萄糖|尿酸|肌酐|尿素|肾小球滤过率|核糖核酸|隐血|癌胚抗原|糖链抗原|蛔虫卵|钩虫卵|鞭虫卵|吞噬细胞|油滴|夏雷登氏|不消化食物|^性状$|^细菌$|转氨酶|白蛋白|胆固醇|甘油三酯|糖化血红蛋白|胰岛素|c肽|c反应蛋白|促甲状腺激素|促肾上腺皮质激素|乳酸", "检验科"),
    ):
        if re.search(pattern, name, re.I):
            return system
    return row.source_sections[0] if len(row.source_sections) == 1 else None


def _clinical_specimen_scope(row):
    """Use an explicit specimen or examination section, never a result guess."""
    if row.specimen:
        return row.specimen
    from modules.report.evaluation_sources import _section_specimen
    scopes = {scope for section in row.source_sections if (scope := _section_specimen(section))}
    return next(iter(scopes)) if len(scopes) == 1 else None


def _has_distinct_medical_scope(row, rows):
    scopes = {_clinical_specimen_scope(other) for other in rows
        if other.kind == "clinical" and _key(other.name) == _key(row.name)}
    return len(scopes) > 1


def _medical_display_system(row, rows):
    system = _medical_system(row)
    if system and _has_distinct_medical_scope(row, rows):
        return system + "（" + (_clinical_specimen_scope(row) or "标本未注明") + "）"
    return system


def _has_result(item):
    return any(value is not None and (not isinstance(value, str) or value.strip())
        for value in (getattr(item, "value", None), getattr(item, "source_status", None),
            getattr(item, "assessment_level", None)))


def _populate_overview_cards(data):
    """Provide source summaries before explicit state-score projection."""
    slots = {item.code: item for item in data.health_overview.assessment_chart}

    def examples(items):
        chosen = []
        seen = set()
        for item in items:
            grade = item.assessment_level or getattr(item, "source_status", None)
            identity = (item.name, grade)
            if not item.name or identity in seen:
                continue
            seen.add(identity)
            chosen.append(item)
        text = "、".join(item.name + ("（" + (item.assessment_level or getattr(item, "source_status", None)) + "）"
            if item.assessment_level or getattr(item, "source_status", None) else "") for item in chosen[:3])
        return chosen[:3], text + ("等" if len(chosen) > 3 else "")

    clinical = [item for item in (*data.medical_assessment.findings, *data.medical_assessment.normal_findings)
        if _has_result(item) and item.sources]
    if clinical:
        focus = [item for item in data.health_overview.key_findings if item.module == "医学评估" and item.sources]
        selected, text = examples(focus or clinical)
        slot = slots["medical"]
        slot.assessment_level = "单项医学记录"
        slot.explanation = "已提供：" + text + "；未提供医学评估总分，按单项记录呈现。"
        slot.sources, slot.availability = _combine_sources(selected), "available"

    microbiome = data.microbiome_assessment
    slot = slots["microbiome"]
    if slot.score is not None:
        slot.explanation = "菌群健康度总分按来源报告原值呈现。"
    else:
        actual = [item for item in (*microbiome.overall_structure, *microbiome.phylum_composition, *microbiome.taxa)
            if _has_result(item) and item.sources]
        if actual:
            selected, text = examples(actual)
            slot.assessment_level = "已有菌群检测结果"
            slot.explanation = "已提供：" + text + "；未提供菌群健康度总分。"
            slot.sources, slot.availability = _combine_sources(selected), "available"

    nutrition = data.nutrition_assessment
    actual = [item for item in (
        *nutrition.major_metabolism,
        *(item for group in nutrition.detailed_metabolism if group.code != "otherSourceFunctions" for item in group.items),
        *(item for group in nutrition.vitamins_and_minerals for item in group.items),
        *nutrition.amino_acids_and_metabolites, *nutrition.neurotransmitters)
        if _has_result(item) and item.sources]
    if not actual:
        actual = [item for group in nutrition.detailed_metabolism for item in group.items
            if _has_result(item) and item.sources]
    if actual:
        low = [item for item in actual if (item.assessment_level or item.source_status) in catalog.LOW_FUNCTION_LEVELS]
        selected, text = examples(low or actual)
        slot = slots["nutrition"]
        slot.assessment_level = "有来源功能低项" if low else "已有单项功能结果"
        slot.explanation = "来源单项结果：" + text + "；未提供营养与功能评估总分，按来源分级及参考区间呈现。"
        slot.sources, slot.availability = _combine_sources(selected), "available"

    overall = slots.get("overall")
    if overall is not None:
        # This is a separate AI judgement over adopted observations, never an
        # average of the three module scores or a replacement native score.
        observations = [*clinical, *microbiome.overall_structure, *microbiome.health_metrics,
            *actual, *data.medical_assessment.questionnaire_summary]
        adopted = [item for item in observations if _has_result(item) and item.sources]
        if adopted:
            overall.sources = _combine_sources(adopted)
            overall.availability = "partial"


def _source_method_standards(data: public.EvaluationData, rows: list[_Observation], parsed: Any = None, *, source_only: bool = False) -> None:
    """Publish fixed report standards without changing literal source ranges.

    An unknown disease-model scale is never converted to the template scale.
    """
    from .evaluation_disease_catalog import source_catalog_family
    family = source_catalog_family(data)
    native = family in {'GM', 'G1'}
    data.disease_risk_assessment.description = (catalog.NATIVE_DISEASE_RISK_DESCRIPTION
        if native else catalog.DISEASE_RISK_DESCRIPTION)
    data.disease_risk_assessment.risk_index_standards = [
        public.StandardDefinition(**item) for item in (catalog.NATIVE_RISK_INDEX_STANDARDS
            if native else catalog.RISK_INDEX_STANDARDS)]
    if not source_only and not native and not (_text(data.disease_risk_assessment.notice) or "").strip() and any(
            row.kind == "disease" and has_risk_index(row) and _text(row.source_status)
            and _number(row.value) is not None
            and _risk_grade(row)[0] != catalog.template_risk_grade(row.value, full_scale=100)
            for row in rows):
        data.disease_risk_assessment.notice = (
            "本表保留检测资料的原始指数与原分级；0–100为模板标尺，未按该标尺换算或重判已有原分级。")
    data.nutrition_assessment.description = (catalog.NATIVE_NUTRITION_DESCRIPTION
        if native else catalog.NUTRITION_DESCRIPTION)
    data.appendices.indicator_standards = [
        public.StandardDefinition(**item) for item in catalog.source_indicator_standards(family)]


def _source_content(source_code, rows):
    """Summarize only categories actually represented in this source."""
    if source_code == 'A':
        names = list(dict.fromkeys(_medical_system(row) for row in rows))
        labels = [name for name in names if name]
        return ('、'.join(labels) + '记录') if labels else '临床检查记录'
    if source_code == 'B':
        return '已提供的健康问卷回答'
    categories = []
    for label, present in (
        ('菌群组成', any(row.kind in {'taxon', 'microbe'} for row in rows)),
        ('菌群结构与健康指标', any(_alias(row, _OVERALL_ALIASES) or _alias(row, _HEALTH_ALIASES) for row in rows)),
        ('疾病风险模型', any(row.kind == 'disease' for row in rows)),
        ('短链脂肪酸合成功能', any(_alias(row, _SCFA_ALIASES) for row in rows)),
        ('营养与代谢功能', any(row.kind in {'metric', 'function'} and not _is_amr(row)
            and (_pathways(row) or _nutrient_group(row)) for row in rows)),
        ('耐药基因', any(_is_amr(row) for row in rows)),
        ('原报告记载的症状、病史与用药', any(row.category == 'sourceReportedBackground' for row in rows)),
        ('健康主题关联筛查', any(row.category == 'healthAssociation' for row in rows)),
        ('毒力基因检测', any(row.category == 'virulence' for row in rows)),
    ):
        if present:
            categories.append(label)
    return '、'.join(categories) if categories else '来源检测记录'


def _project_literal_native_modules(data, rows):
    """Move supplied native modules without assigning attention or a risk."""
    from .evaluation_native_modules import ASSOCIATION_PREFIX, project_native_modules
    project_native_modules(data, rows)
    categories = {row.code: row for row in data.disease_risk_assessment.categories}
    for item in data.disease_risk_assessment.risk_ranking:
        if not str(item.code or '').startswith(ASSOCIATION_PREFIX):
            continue
        code = str(item.code).removeprefix(ASSOCIATION_PREFIX)
        category = categories.get(_disease_category(item.name, code=code))
        if category is None:
            # Preserve an unclassified source screen in the health table;
            # its missing disease dimension must not be guessed.
            data.microbiome_assessment.health_metrics.append(public.HealthMetricAssessment(
                **item.model_dump(exclude={'related_taxa', 'related_indicators'})))
            continue
        category.items.append(item)
        category.sources.extend(source.model_copy(deep=True) for source in item.sources
            if source not in category.sources)
        category.availability = 'partial'
    data.disease_risk_assessment.risk_ranking = []


def _finish_source_projection(data, rows):
    """Discard legacy editorial defaults and merge only exact model mirrors."""
    editorial = {'text', 'summary', 'explanation', 'association', 'health_meaning',
        'basis', 'followup_action', 'attention_basis', 'evidence_strength',
        'modifiability', 'interpretation', 'handling'}

    def clear(value):
        if isinstance(value, list):
            for child in value:
                clear(child)
        elif hasattr(type(value), 'model_fields'):
            for name in type(value).model_fields:
                if name in {'source_items', 'source_advice', 'observations', 'details'}:
                    continue
                child = getattr(value, name)
                if name in editorial and (child is None or isinstance(child, str)):
                    setattr(value, name, None)
                else:
                    clear(child)
    for name in ('health_overview', 'medical_assessment', 'microbiome_assessment',
            'disease_risk_assessment', 'nutrition_assessment', 'functional_pathways',
            'comprehensive_conclusion'):
        clear(getattr(data, name))
    data.health_overview.key_findings = []
    data.comprehensive_conclusion.priority_directions = []
    data.comprehensive_conclusion.next_steps = []
    data.disease_risk_assessment.risk_ranking = []
    for category in data.disease_risk_assessment.categories:
        seen, kept = {}, []
        for item in category.items:
            origin = getattr(item, '_source_projection_model_identity', None)
            if origin is None or str(item.code or '').startswith('nativeAssociation:'):
                kept.append(item)
                continue
            key = (origin, item.code, item.name, type(item.value).__name__,
                json.dumps(item.value, ensure_ascii=False, sort_keys=True), item.unit,
                item.reference_range, item.source_status, item.assessment_level)
            previous = seen.get(key)
            if previous is None:
                seen[key] = item
                kept.append(item)
            else:
                previous.sources.extend(source.model_copy(deep=True) for source in item.sources
                    if source not in previous.sources)
                for signal in item.related_taxa:
                    _merge_taxon_signal(previous.related_taxa, signal.model_copy(deep=True))
        category.items = kept


def _project_evaluation_data(request, parsed, clinical_document=None, health_assessment=None,
                          evidence_references=None, *, source_only=False) -> public.EvaluationData:
    """Project all available source rows; leave model-authored narrative empty.

    ``health_assessment`` is intentionally not relabelled as the template's
    medical/nutrition totals: the six-system engine is a different construct.
    Legacy clinical sections and intervention prose are never copied.
    """
    data = public.EvaluationData()
    if clinical_document is not None:
        # The same quote-verified problems organize the working assessment
        # and final callback; raw measurements remain a separate inventory.
        public.bind_clinical_problem_projection(data, clinical_document)
    from .evaluation_disease_catalog import initialize_source_catalog
    family = initialize_source_catalog(data, parsed)
    source_definitions = catalog.source_nutrition_definitions(family)
    if family in {"GM", "G1"}:
        data.microbiome_assessment.overall_structure = [public.MetricAssessment(code=code, name=name)
            for code, name in catalog.source_structure_definitions(family)]
        data.microbiome_assessment.health_metrics = [public.HealthMetricAssessment(code=code, name=name)
            for code, name in catalog.source_health_definitions(family)]
        data.microbiome_assessment.short_chain_fatty_acids = [public.MetricAssessment(code=code, name=name)
            for code, name in source_definitions['scfa']]
        data.nutrition_assessment.major_metabolism = []
        data.nutrition_assessment.neurotransmitters = [public.MetricAssessment(code=code, name=name)
            for code, name in source_definitions['neuro']]
        data.nutrition_assessment.vitamins_and_minerals = [public.NutrientGroup(code=code, name=name)
            for code, name in source_definitions['groups']]
    rows = _source_observations(request, parsed, clinical_document, source_only=source_only)
    _seal_original_medical_advice(rows, clinical_document)
    for row in rows:
        if row.source_code == 'C':
            row.catalog_family = family
    explicit_phyla = {f"parsed.sourceTaxonOccurrences[{index}]"
        for index, occurrence in enumerate(_get(parsed, "source_taxon_occurrences", "sourceTaxonOccurrences", default=[]) or [])
        if _get(occurrence, "sourceSection", "source_section") == "菌门构成"}
    if not source_only:
        _apply_verified_imaging_problems(rows, clinical_document)
    for row in rows:
        row.source_projection_only = source_only
    data.appendices.source_items = [row.inventory() for row in rows]
    from .evaluation_pdf_amr import selected_pdf_amr_display_rows
    rows = selected_pdf_amr_display_rows(rows, parsed)
    _source_method_standards(data, rows, parsed, source_only=source_only)
    categories = {item.code: item for item in data.disease_risk_assessment.categories}
    pathways = {item.code: item for item in data.functional_pathways.pathways}
    nutrition = data.nutrition_assessment
    microbiome = data.microbiome_assessment
    projected_observations = {}
    virulence_items = []

    for row in rows:
        if row.kind == "context" and row.category == "sourceReportedBackground":
            name = row.name if row.name.startswith("原报告记载") else "原报告记载的" + row.name
            data.medical_assessment.questionnaire_summary.append(public.QuestionnaireSummaryItem(
                code=row.code, name=name, value=row.value, sources=copy.deepcopy(row.sources), availability=row.availability))
        elif row.kind == "context" and row.category in {"healthAssociation", "virulence"}:
            item = row.metric(public.HealthMetricAssessment)
            # The gene-panel inventory is source detail, not a health meaning.
            # Detected genes remain in value/details and the complete original
            # candidate list remains in the internal source observation.
            item.explanation = row.description if row.category != "virulence" else None
            if row.category == "virulence" and (row.description or "").startswith("检出基因："):
                item.details = row.description.split("；", 1)[0]
            microbiome.health_metrics.append(item)
            if row.category == "virulence":
                virulence_items.append(item)
        elif row.kind == "clinical":
            item = row.metric(public.MedicalFinding)
            item.source_advice = row.description
            item.system = _medical_display_system(row, rows)
            normal = (_status_state(row.source_status) == "normal" if source_only
                else _medical_source_consistency(data, row))
            if not source_only and (normal or row.abnormal):
                item.assessment_level = "正常" if normal else "异常"
                item._assessment_origin = "report"
            target = data.medical_assessment.normal_findings if normal else data.medical_assessment.findings
            # Neutral measurements stay in the source inventory. They are not
            # abnormal findings simply because no institution grade exists.
            if source_only or normal or not is_ungraded_medical_record(item):
                target.append(item)
        elif row.kind == "self_reported":
            data.medical_assessment.questionnaire_summary.append(public.QuestionnaireSummaryItem(
                code=row.code, name=row.name, value=row.value, sources=row.sources, availability=row.availability))
        elif row.kind in {"taxon", "microbe"}:
            item = row.metric(public.TaxonAssessment)
            item.scientific_name, item.category, item.detection_status = row.scientific_name, row.category, row.detection_status
            microbiome.taxa.append(item)
            is_phylum = (any(source.record_id in explicit_phyla for source in row.sources)
                if explicit_phyla else bool(re.search(r"菌门$", row.name) or _key(row.scientific_name) in {
                    "bacteroidota", "bacteroidetes", "firmicutes", "bacillota", "proteobacteria", "actinobacteria", "verrucomicrobiota"}))
            if is_phylum:
                microbiome.phylum_composition.append(row.metric())
        elif row.kind == "disease":
            if not has_risk_index(row) and not (source_only and row.source_status is not None):
                continue
            item = row.metric(public.DiseaseRiskItem)
            item.assessment_level = (_text(row.source_status) or _source_risk_grade(row)) if source_only else _risk_grade(row)[0]
            if item.assessment_level is not None:
                item._assessment_origin = 'source' if row.source_status is not None else 'reference'
            if source_only and (row.source_name or _get(parsed, 'source_checksum', 'sourceChecksum')):
                object.__setattr__(item, '_source_projection_model_identity', (row.source_name,
                    row.source_type, row.observed_at, _get(parsed, 'source_checksum', 'sourceChecksum')
                    if row.source_name == _get(parsed, 'source_name', 'sourceName') else None))
            item.related_taxa = _disease_summary_taxa(row, rows)
            if not source_only:
                data.disease_risk_assessment.risk_ranking.append(item)
            category = _disease_category(row.name, code=row.code, category=row.category)
            if category:
                categories[category].items.append(item.model_copy(deep=True))
                categories[category].sources = _combine_sources([r for r in rows if r.kind == "disease" and _disease_category(r.name, code=r.code, category=r.category) == category])
                categories[category].availability = "available"
        elif row.kind in {"metric", "function", "context"}:
            if _is_amr(row):
                item = row.metric(public.AntimicrobialResistance)
                item.antibiotic_class = _antibiotic_class_name(row, parsed)
                microbiome.antimicrobial_resistance.append(item)
                pathways["P10"].related_metrics.append(row.metric())
                _project_function_links(pathways["P10"], row, rows)
                pathways["P10"].sources = _combine_sources([r for r in rows
                    if r.kind in {"metric", "function"} and "P10" in _pathways(r)])
                pathways["P10"].availability = "available"
                continue
            assigned = False
            for aliases, target in ((_OVERALL_ALIASES, microbiome.overall_structure), (_HEALTH_ALIASES, microbiome.health_metrics), (_SCFA_ALIASES, microbiome.short_chain_fatty_acids), (_MAJOR_ALIASES, nutrition.major_metabolism), (_NEURO_ALIASES, nutrition.neurotransmitters)):
                code = _alias(row, aliases)
                if code:
                    if code in {'beRatio', 'fbRatio'} and row.value is not None and _number(row.value) is None:
                        # "定植抗力：正常" is a source summary, not a
                        # second numerical B/E observation. Its unchanged
                        # inventory fact remains available with its source.
                        continue
                    _put(target, row, code, observations=projected_observations)
                    assigned = True
            if row.kind == "context":
                # Only exact known indicator aliases get a display slot.
                # Other context (taxon headings, genus rows, legends) remains
                # source context and is never a nutrient or disease model.
                continue
            core = _alias(row, _OVERALL_ALIASES) or _alias(row, _HEALTH_ALIASES)
            nutrient_group = _nutrient_group(row)
            if nutrient_group:
                group = next((group for group in nutrition.vitamins_and_minerals if group.code == nutrient_group), None)
                if group is None:
                    group = public.NutrientGroup(code=nutrient_group,
                        name=dict(catalog.NUTRIENT_GROUP_DEFINITIONS).get(nutrient_group))
                    nutrition.vitamins_and_minerals.append(group)
                group.items.append(row.metric())
                group.availability = "available"
                assigned = True
            paths = _pathways(row)
            for path in paths:
                pathways[path].related_metrics.append(row.metric())
                _project_function_links(pathways[path], row, rows)
                pathways[path].sources = _combine_sources([r for r in rows if r.kind in {"metric", "function"} and path in _pathways(r)])
                pathways[path].availability = "available"
            amino_member = ('P4' in paths if family in {'GM', 'G1'} else
                _standard_term(row, _AMINO_ACIDS) or _standard_term(row, _OTHER_METABOLITES) or 'P4' in paths)
            if not core and amino_member and not _alias(row, _MAJOR_ALIASES):
                nutrition.amino_acids_and_metabolites.append(row.metric())
                assigned = True
            detailed_paths = [("P1", "carbohydrateMetabolism", "糖类代谢"),
                    ("P2", "lipidMetabolism", "脂类代谢"), ("P7", "nucleotideMetabolism", "核苷酸代谢")]
            if family in {'GM', 'G1'}:
                detailed_paths.extend((("P9", "harmfulMetabolites", "有害代谢物合成能力"),
                    ("P10", "toxicSubstanceDegradation", "毒性物质分解能力")))
            for path, code, name in detailed_paths:
                if path in paths and not core and not _alias(row, _MAJOR_ALIASES):
                    group = next((g for g in nutrition.detailed_metabolism if g.code == code), None)
                    if group is None:
                        group = public.NutrientGroup(code=code, name=name)
                        nutrition.detailed_metabolism.append(group)
                    group.items.append(row.metric())
                    group.availability = "available"
                    assigned = True
            if not assigned:
                # Preserve unclassified functional observations in a visible
                # dynamic group as well as in the complete appendix inventory.
                code = "otherSourceFunctions"
                group = next((g for g in nutrition.detailed_metabolism if g.code == code), None)
                if group is None:
                    group = public.NutrientGroup(code=code, name="其他来源功能项目")
                    nutrition.detailed_metabolism.append(group)
                group.items.append(row.metric())
                group.availability = "available"

    if not source_only:
        _project_health_associations(data, rows)
    else:
        _project_literal_native_modules(data, rows)
    _project_disease_source_evidence(data, parsed, rows)
    disease_order = {r.observation_id: r for r in rows if r.kind == "disease"}
    if not source_only:
        grades = {_hash([(s.record_id, s.observed_at) for s in r.sources]): _risk_grade(r)[1] for r in disease_order.values()}
        data.disease_risk_assessment.risk_ranking.sort(key=lambda item: (
            -grades.get(_hash([(s.record_id, s.observed_at) for s in item.sources]), -1),
            -(_number(item.value) if _number(item.value) is not None else -1)))
    for category in categories.values():
        matched = [r for r in disease_order.values() if _disease_category(r.name, code=r.code, category=r.category) == category.code]
        # Initialise categories before applying the explicit attention rule.
        # Unknown model scales remain ungraded; their literal rows stay intact.
        category.attention_level = None
        category.attention_basis = None
        if matched:
            category.availability = "partial"
        matrix = next(m for m in data.disease_risk_assessment.overview_matrix if m.code == category.code)
        matrix.attention_level, matrix.basis, matrix.sources = category.attention_level, category.attention_basis, copy.deepcopy(category.sources)
        matrix.availability = category.availability
    if not source_only:
        _populate_risk_context(data, rows)
        _summarize_pathways(pathways, rows)
    _project_harmful_generation_aggregate(pathways, rows)
    # Gene panels are qualitative source context, not scalar function scores.
    # Append after scalar summaries so positional grade matching cannot assign
    # a metabolite's grade to a preceding gene panel.
    for item in virulence_items:
        pathways["P9"].related_metrics.append(item.model_copy(deep=True))
        pathways["P9"].sources.extend(source.model_copy(deep=True)
            for source in item.sources if source not in pathways["P9"].sources)
        pathways["P9"].availability = "available"
    for row in rows:
        grade = row.metric().assessment_level if source_only else _nutrition_score_grade(row)
        if grade in catalog.LOW_FUNCTION_LEVELS:
            item = row.metric()
            item.assessment_level = grade
            nutrition.weak_synthesis_chart.append(item)
    _link_projected_pathways(data)
    if not source_only:
        _reference_range_consistency(data, rows)
        _ordered_findings(data, rows, clinical_document)
    _questionnaire_triggers(data, rows)

    enterotype = _get(parsed, "enterotype")
    if enterotype is not None and not any(item.code == "enterotype" and item.sources for item in microbiome.overall_structure):
        row = _Observation(observation_id="parsed:enterotype", code="enterotype", name="肠型", value=enterotype,
            kind="metric", source_name=_get(parsed, "source_name", "sourceName"), source_type=_get(parsed, "source_type", "sourceType"),
            observed_at=_get(_get(parsed, "sample", default={}), "report_date", "reportDate"),
            source_projection_only=source_only)
        row.reference("parsed.enterotype")
        _put(microbiome.overall_structure, row, "enterotype", observations=projected_observations)
        data.appendices.source_items.append(row.inventory())

    from modules.report.evaluation_enterotype import classify_explicit_enterotypes
    if not source_only:
        classify_explicit_enterotypes(microbiome.overall_structure)

    for index, panel in enumerate(_get(parsed, "panel_counts", "panelCounts", default=[]) or []):
        microbiome.panel_counts.append(public.PanelCount(code=_get(panel, "category"), name=_get(panel, "panel_name", "panelName"),
            detected_count=_get(panel, "detected_count", "detectedCount"), tested_count=_get(panel, "row_count", "rowCount"),
            sources=[public.SourceReference(source_code="C", record_id=f"parsed.panelCounts[{index}]")], availability="available"))
    if not source_only:
        _populate_provided_panel_results(data, parsed, rows)
    _complete_health_summary_details(data, parsed, rows)

    for source in data.introduction.sources:
        supplied = [r for r in rows if r.source_code == source.source_code]
        source.available = bool(supplied)
        source.availability = "available" if supplied else "unavailable"
        if supplied:
            source.content = _source_content(source.source_code, supplied)
            dates = list(dict.fromkeys(r.observed_at for r in supplied if r.observed_at))
            source.observed_at = dates[0] if len(dates) == 1 else None
        if source.source_code == "B":
            survey = next((_get(request, key) for key in ("healthSurvey", "questionnaire", "survey") if isinstance(_get(request, key), dict)), {})
            source.version = _text(survey.get("version"))
            source.question_count = survey.get("questionCount") if type(survey.get("questionCount")) is int else None
            source.answered_count = survey.get("answeredCount") if type(survey.get("answeredCount")) is int else None
    from .evaluation_disease_catalog import initialize_source_catalog
    initialize_source_catalog(data, parsed)
    data.introduction.availability = "available" if rows else "unavailable"
    if _get(parsed, "source_errors", "sourceErrors", default=[]):
        data.introduction.source_notices = ["部分来源资料未完整解析；以下保留已取得的来源记录。"]
        data.introduction.availability = "partial"
    data.medical_assessment.availability = "available" if any(r.kind in {"clinical", "self_reported"}
        or r.kind == "context" and r.category == "sourceReportedBackground" for r in rows) else "unavailable"
    microbiome.availability = "available" if any(r.kind in {"metric", "function", "taxon", "microbe"} for r in rows) else "unavailable"
    data.disease_risk_assessment.availability = "available" if any(category.items for category in categories.values()) else "unavailable"
    nutrition.availability = "available" if any(r.kind in {"metric", "function"} and not _is_amr(r) and (
        _pathways(r) or _alias(r, _MAJOR_ALIASES) or _nutrient_group(r) or _standard_term(r, _OTHER_METABOLITES)) for r in rows) else "unavailable"
    data.functional_pathways.availability = "available" if any(p.related_metrics for p in pathways.values()) else "unavailable"
    if not source_only:
        score = next((m for m in microbiome.overall_structure if m.code == "microbiomeHealthScore" and m.value is not None), None)
        if score is not None:
            try:
                numeric = float(score.value)
            except (ValueError, TypeError):
                numeric = None
            if numeric is not None and 0 <= numeric <= 100:
                slot = next(s for s in data.health_overview.assessment_chart if s.code == "microbiome")
                slot.score, slot.sources, slot.availability = numeric, score.sources, "available"
        _populate_overview_cards(data)
        populate_state_assessments(data)
        from modules.report.evaluation_scores import synchronize_disease_index_levels
        synchronize_disease_index_levels(data)
    data.health_overview.availability = "partial" if rows else "unavailable"
    for ref in evidence_references or []:
        value = _mapping(ref)
        number = value.get("number")
        if type(number) is not int:
            continue
        data.appendices.reference_literature.append(public.ReferenceLiterature(number=number,
            title=value.get("title"), authors=value.get("authors") or [], year=_text(value.get("year")),
            journal=_first(value.get("journal"), value.get("source")), url=value.get("url"), doi=value.get("doi"),
            # EviMed's AI studyTypes are retrieval metadata, not verified
            # publication design. Keep bibliography identity without presenting
            # that classification as an assessed study design.
            study_type=None if value.get("provider") == "evimed_v2" else _first(
                value.get("studyType"), "、".join(value.get("publicationTypes") or []) or None,
                "、".join(value.get("studyTypes") or []) or None)))
    from modules.report.evaluation_introduction import project_evaluation_introduction
    project_evaluation_introduction(data, request)
    from modules.report.evaluation_fixed_assessment import project_fixed_assessment
    if not source_only:
        data = project_fixed_assessment(data, clinical_document)
    from .evaluation_schema import compact_native_nutrition_mirrors
    data.nutrition_assessment.major_metabolism = compact_native_nutrition_mirrors(
        data.nutrition_assessment.major_metabolism)
    # Materialize the same source-bound problem owners before authorship and
    # final serialization; a late callback-only row has never been narrated.
    materialize = getattr(public, 'materialize_clinical_problem_findings', None)
    if callable(materialize):
        materialize(data)
    if source_only:
        _finish_source_projection(data, rows)
    return data


def project_evaluation_source_data(request, parsed, clinical_document=None, health_assessment=None,
                                   evidence_references=None) -> public.EvaluationData:
    """Adapt literal sources without legacy scoring, attention or prose.

    Only a supplied original status or this observation's own literal legend
    may provide a grade. All original observations remain in source_items.
    """
    return _project_evaluation_data(request, parsed, clinical_document, health_assessment,
        evidence_references, source_only=True)


def build_evaluation_data(request, parsed, clinical_document=None, health_assessment=None,
                          evidence_references=None) -> public.EvaluationData:
    """Keep the established legacy builder behavior for existing callers."""
    return _project_evaluation_data(request, parsed, clinical_document, health_assessment,
        evidence_references, source_only=False)
