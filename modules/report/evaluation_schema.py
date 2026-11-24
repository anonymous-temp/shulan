"""Fixed public evaluate.v2.0 DTO, independent of the legacy follow-up DTO.

All defaults are explicit empty states or approved static display text. No
example patient score, measurement, diagnosis or questionnaire answer is used.
Serialize with ``model_dump(mode='json', by_alias=True, exclude_none=False)``.
The working EvaluationData retains source inventory for validation/composition;
the final callback includes assessed taxon rows but omits private raw inventory.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
from datetime import date
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
import hashlib
import json
import math
import re

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator, model_serializer, model_validator

from modules.report import evaluation_catalog as catalog
from modules.report.assessment_presentation import AssessmentPresentation


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


Availability = Literal["available", "partial", "unavailable", "notApplicable"]
MetricValue = StrictStr | StrictInt | StrictFloat | None
AnswerValue = StrictStr | StrictInt | StrictFloat | StrictBool | None


class EvaluationModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True, extra="forbid")


class SourceReference(EvaluationModel):
    source_code: Literal["A", "B", "C"] | None = None
    record_id: str | None = None
    observed_at: str | None = None


class TextAssessment(EvaluationModel):
    text: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


# Shared name for the new narrative gateway; the public shape is identical.
NarrativeAssessment = TextAssessment


class MetricAssessment(EvaluationModel):
    _assessment_origin: str | None = PrivateAttr(default=None)

    code: str | None = None
    name: str | None = None
    value: MetricValue = None
    unit: str | None = None
    reference_range: str | None = None
    source_status: str | None = None
    assessment_level: str | None = None
    explanation: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


def _metrics(definitions: tuple) -> list[MetricAssessment]:
    return [MetricAssessment(code=code, name=name,
        explanation=catalog.OVERALL_STRUCTURE_EXPLANATIONS.get(code)) for code, name in definitions]


class StandardDefinition(EvaluationModel):
    code: str
    name: str
    meaning: str
    reference_range: str


class SourceSummary(EvaluationModel):
    source_code: Literal["A", "B", "C"]
    name: str | None = None
    role: str | None = None
    content: str | None = None
    organization: str | None = None
    record_id: str | None = None
    observed_at: str | None = None
    version: str | None = None
    question_count: int | None = None
    answered_count: int | None = None
    available: StrictBool = False
    availability: Availability = "unavailable"


class IntroductionSection(EvaluationModel):
    title: str = catalog.REPORT_TITLE
    subtitle: str = catalog.REPORT_SUBTITLE
    assessment_nature: str = catalog.ASSESSMENT_NATURE
    description: str = catalog.INTRODUCTION_TEXT
    disclaimer: str = catalog.COVER_DISCLAIMER
    important_declarations: list[str] = Field(default_factory=lambda: list(catalog.IMPORTANT_DECLARATIONS))
    sources: list[SourceSummary] = Field(default_factory=lambda: [
        SourceSummary(source_code=code, name=name, role=role) for code, name, role in catalog.SOURCE_DEFINITIONS
    ])
    source_notices: list[str] = Field(default_factory=list)
    availability: Availability = "unavailable"


class AssessmentScore(EvaluationModel):
    code: str | None = None
    name: str | None = None
    score: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=100)
    assessment_level: str | None = None
    explanation: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class OverviewFinding(EvaluationModel):
    rank: int | None = None
    name: str | None = None
    module: str | None = None
    assessment_level: str | None = None
    summary: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class AssessmentSummaries(EvaluationModel):
    medical: TextAssessment = Field(default_factory=TextAssessment)
    microbiome: TextAssessment = Field(default_factory=TextAssessment)
    nutrition: TextAssessment = Field(default_factory=TextAssessment)


class HealthOverviewSection(EvaluationModel):
    overall_summary: TextAssessment = Field(default_factory=TextAssessment)
    overall_score: AssessmentScore = Field(default_factory=lambda: AssessmentScore(
        code='overall', name='健康管理评估指数'))
    assessment_chart: list[AssessmentScore] = Field(default_factory=lambda: [
        AssessmentScore(code=code, name=name) for code, name in catalog.OVERVIEW_SCORE_DEFINITIONS
    ])
    key_findings: list[OverviewFinding] = Field(default_factory=list)
    summaries: AssessmentSummaries = Field(default_factory=AssessmentSummaries)
    availability: Availability = "unavailable"


class MedicalFinding(MetricAssessment):
    system: str | None = None
    source_advice: str | None = None
    certainty: str | None = None


class QuestionnaireSummaryItem(EvaluationModel):
    code: str | None = None
    name: str | None = None
    value: AnswerValue = None
    explanation: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class SourceValue(SourceReference):
    value: AnswerValue = None


class ConsistencyNote(EvaluationModel):
    code: str | None = None
    name: str | None = None
    source_values: list[SourceValue] = Field(default_factory=list)
    handling: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    availability: Availability = "unavailable"


class TriggerAnalysisRow(EvaluationModel):
    code: str | None = None
    name: str | None = None
    observations: str | None = None
    association: str | None = None
    evidence_strength: str | None = None
    modifiability: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class MedicalAssessmentSection(EvaluationModel):
    description: str = catalog.MEDICAL_DESCRIPTION
    findings: list[MedicalFinding] = Field(default_factory=list)
    normal_findings: list[MedicalFinding] = Field(default_factory=list)
    consistency_notes: list[ConsistencyNote] = Field(default_factory=list)
    questionnaire_summary: list[QuestionnaireSummaryItem] = Field(default_factory=lambda: [
        QuestionnaireSummaryItem(code=code, name=name) for code, name in catalog.QUESTIONNAIRE_SUMMARY_DEFINITIONS
    ])
    trigger_analysis: list[TriggerAnalysisRow] = Field(default_factory=lambda: [
        TriggerAnalysisRow(code=code, name=name) for code, name in catalog.TRIGGER_DEFINITIONS
    ])
    trigger_conclusion: TextAssessment = Field(default_factory=TextAssessment)
    availability: Availability = "unavailable"


class TaxonAssessment(MetricAssessment):
    scientific_name: str | None = None
    category: str | None = None
    detection_status: str | None = None


class PanelCount(EvaluationModel):
    code: str | None = None
    name: str | None = None
    detected_count: int | None = None
    tested_count: int | None = None
    abnormal_count: int | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    availability: Availability = "unavailable"


class AntimicrobialResistance(MetricAssessment):
    antibiotic_class: str | None = None
    interpretation: str | None = None


class HealthMetricAssessment(MetricAssessment):
    """A compact, source-bound detail column for the health summary table."""
    details: str | None = None


class MicrobiomeAssessmentSection(EvaluationModel):
    description: str = catalog.MICROBIOME_DESCRIPTION
    overall_structure: list[MetricAssessment] = Field(default_factory=lambda: _metrics(catalog.OVERALL_STRUCTURE_DEFINITIONS))
    phylum_composition: list[MetricAssessment] = Field(default_factory=list)
    structure_interpretation: TextAssessment = Field(default_factory=TextAssessment)
    health_metrics: list[HealthMetricAssessment] = Field(default_factory=lambda: [
        HealthMetricAssessment(code=code, name=name) for code, name in catalog.HEALTH_METRIC_DEFINITIONS])
    panel_counts: list[PanelCount] = Field(default_factory=list)
    taxa: list[TaxonAssessment] = Field(default_factory=list)
    key_microbial_signals: TextAssessment = Field(default_factory=TextAssessment)
    short_chain_fatty_acids: list[MetricAssessment] = Field(default_factory=lambda: _metrics(catalog.SCFA_DEFINITIONS))
    scfa_interpretation: TextAssessment = Field(default_factory=TextAssessment)
    antimicrobial_resistance: list[AntimicrobialResistance] = Field(default_factory=list)
    resistance_notice: str = catalog.RESISTANCE_NOTICE
    availability: Availability = "unavailable"


class TaxonSignal(EvaluationModel):
    # Source association metadata belongs to the interpretation context. Keep
    # it through internal model copies without expanding the callback DTO.
    _source_correlation: str | None = PrivateAttr(default=None)
    _source_reference_range: str | None = PrivateAttr(default=None)

    name: str | None = None
    scientific_name: str | None = None
    value: MetricValue = None
    unit: str | None = None
    source_status: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)


class DiseaseRiskItem(MetricAssessment):
    related_taxa: list[TaxonSignal] = Field(default_factory=list)
    related_indicators: list[MetricAssessment] = Field(default_factory=list)


@dataclass(frozen=True)
class _ReviewedDiseaseAttention:
    level: str
    basis: str
    fact_ids: tuple[str, ...]
    scope: str


class DiseaseRiskCategory(EvaluationModel):
    _reviewed_attention: _ReviewedDiseaseAttention | None = PrivateAttr(default=None)

    code: str
    name: str | None = None
    items: list[DiseaseRiskItem] = Field(default_factory=list)
    clinical_correlation: TextAssessment = Field(default_factory=TextAssessment)
    attention_level: str | None = None
    attention_basis: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


def _disease_attention_scope(data, category, fact_ids):
    """Fingerprint adopted measurements, never generated prose or public flags."""
    def measurement(row):
        keys = ('code', 'name', 'value', 'unit', 'reference_range', 'source_status', 'assessment_level')
        result = {key: getattr(row, key, None) for key in keys}
        result['sources'] = sorted((source.source_code or '', source.record_id or '', source.observed_at or '')
            for source in row.sources)
        return result
    def canonical(rows):
        return sorted({json.dumps(measurement(row), ensure_ascii=False, sort_keys=True,
            allow_nan=False) for row in rows})
    bound = set(fact_ids)
    selected = [row for row in [*data.appendices.source_items,
        *data.medical_assessment.findings, *data.medical_assessment.normal_findings,
        *data.medical_assessment.questionnaire_summary]
        if bound.intersection(source.record_id for source in row.sources)]
    actual = [item for item in category.items if item.value is not None or item.sources]
    payload = {'category': category.code, 'items': canonical(actual),
        'taxa': canonical(taxon for item in actual for taxon in item.related_taxa),
        'selected': canonical(selected), 'factIds': sorted(bound)}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
        allow_nan=False).encode()).hexdigest()


def _reviewed_disease_attention_matches(data, category):
    proof = category._reviewed_attention
    return (isinstance(proof, _ReviewedDiseaseAttention)
        and (category.attention_level, category.attention_basis) == (proof.level, proof.basis)
        and proof.scope == _disease_attention_scope(data, category, proof.fact_ids))


class RiskMatrixRow(EvaluationModel):
    code: str
    name: str | None = None
    score: StrictInt | StrictFloat | None = Field(default=None, ge=0, le=100)
    attention_level: str | None = None
    basis: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


def _ordered_rows(rows, definitions, row_type):
    """Backfill fixed display slots, reject unknown/duplicate identifiers."""
    names = dict(definitions)
    indexed = {}
    for row in rows:
        if row.code not in names or row.code in indexed:
            raise ValueError("unknown or duplicate fixed display code")
        indexed[row.code] = row.model_copy(update={"name": names[row.code]})
    return [indexed.get(code) or row_type(code=code, name=name) for code, name in definitions]


def _ordered_source_risk_rows(rows, row_type):
    """Repartition source items; a changed partition needs a new judgement."""
    if len({row.code for row in rows}) != len(rows):
        raise ValueError('duplicate source disease category code')
    partitions = {code: [] for code, _ in catalog.RISK_CATEGORY_DEFINITIONS}
    for row in rows:
        # A retired empty display slot contributes no patient information
        # and cannot invalidate an unchanged, independently reviewed group.
        if row.code not in partitions and not row.model_dump(
                exclude={'code', 'name'}, exclude_defaults=True):
            continue
        groups = {}
        if isinstance(row, DiseaseRiskCategory) and row.items:
            for item in row.items:
                code = catalog.disease_category(item.name, code=item.code, category=row.code)
                groups.setdefault(code, []).append(item)
        else:
            groups[catalog.disease_category(row.name, category=row.code)] = []
        for code, items in groups.items():
            partitions[code].append((row, items, len(groups) == 1))
    result = []
    for code, name in catalog.RISK_CATEGORY_DEFINITIONS:
        members = partitions[code]
        if (len(members) == 1 and members[0][0].code == code and members[0][2]):
            # Preserve exact in-process review provenance only for the same
            # complete partition. A public DTO still has no private proof.
            result.append(members[0][0].model_copy(deep=True, update={'name': name}))
            continue
        target = row_type(code=code, name=name)
        if isinstance(target, DiseaseRiskCategory):
            for old, items, _ in members:
                for item in items:
                    if item not in target.items:
                        target.items.append(item.model_copy(deep=True))
                    for source in item.sources:
                        if source not in target.sources:
                            target.sources.append(source.model_copy(deep=True))
            if any(item.value is not None or item.sources for item in target.items):
                target.availability = 'partial'
        # Never copy a tumour-wide or merged allergy/immune judgement into
        # a different new group. Items retain all original values/grades;
        # aggregate levels, prose and references are independently recomputed.
        result.append(target)
    return result


class DiseaseRiskAssessmentSection(EvaluationModel):
    description: str = catalog.DISEASE_RISK_DESCRIPTION
    risk_index_standards: list[StandardDefinition] = Field(default_factory=lambda: [StandardDefinition(**x) for x in catalog.RISK_INDEX_STANDARDS])
    matrix_levels: list[str] = Field(default_factory=lambda: list(catalog.PRESENTATION_RISK_LEVELS))
    categories: list[DiseaseRiskCategory] = Field(default_factory=lambda: [
        DiseaseRiskCategory(code=code, name=name) for code, name in catalog.RISK_CATEGORY_DEFINITIONS
    ])
    overview_matrix: list[RiskMatrixRow] = Field(default_factory=lambda: [
        RiskMatrixRow(code=code, name=name) for code, name in catalog.RISK_CATEGORY_DEFINITIONS
    ])
    risk_ranking: list[DiseaseRiskItem] = Field(default_factory=list)
    notice: str = catalog.DISEASE_RISK_NOTICE
    availability: Availability = "unavailable"

    @field_validator("categories")
    @classmethod
    def ordered_categories(cls, rows):
        return _ordered_source_risk_rows(rows, DiseaseRiskCategory)

    @field_validator("overview_matrix")
    @classmethod
    def ordered_matrix(cls, rows):
        return _ordered_source_risk_rows(rows, RiskMatrixRow)

    @field_validator("risk_index_standards")
    @classmethod
    def fixed_risk_standards(cls, rows):
        if not rows:
            return []
        if [(r.code, r.name) for r in rows] not in [
            [(r["code"], r["name"]) for r in standards] for standards in (
                catalog.RISK_INDEX_STANDARDS, catalog.NATIVE_RISK_INDEX_STANDARDS,
                catalog.INTEGRATED_RISK_INDEX_STANDARDS, catalog.PRESENTATION_RISK_INDEX_STANDARDS)]:
            raise ValueError("source risk display standard identities and order are fixed")
        return rows

    @field_validator("matrix_levels")
    @classmethod
    def fixed_matrix_levels(cls, rows):
        if rows not in (list(catalog.ATTENTION_LEVELS), list(catalog.PRESENTATION_RISK_LEVELS)):
            raise ValueError("attention matrix levels are fixed")
        return rows


class NutrientGroup(EvaluationModel):
    code: str | None = None
    name: str | None = None
    items: list[MetricAssessment] = Field(default_factory=list)
    explanation: TextAssessment = Field(default_factory=TextAssessment)
    availability: Availability = "unavailable"


class NutritionAssessmentSection(EvaluationModel):
    description: str = catalog.NUTRITION_DESCRIPTION
    major_metabolism: list[MetricAssessment] = Field(default_factory=lambda: _metrics(catalog.MAJOR_METABOLISM_DEFINITIONS))
    detailed_metabolism: list[NutrientGroup] = Field(default_factory=list)
    vitamins_and_minerals: list[NutrientGroup] = Field(default_factory=lambda: [
        NutrientGroup(code=code, name=name) for code, name in catalog.NUTRIENT_GROUP_DEFINITIONS
    ])
    weak_synthesis_chart: list[MetricAssessment] = Field(default_factory=list)
    amino_acids_and_metabolites: list[MetricAssessment] = Field(default_factory=list)
    amino_metabolite_interpretation: TextAssessment = Field(default_factory=TextAssessment)
    neurotransmitters: list[MetricAssessment] = Field(default_factory=lambda: _metrics(catalog.NEUROTRANSMITTER_DEFINITIONS))
    nutrition_interpretation: TextAssessment = Field(default_factory=TextAssessment)
    neurotransmitter_interpretation: TextAssessment = Field(default_factory=TextAssessment)
    availability: Availability = "unavailable"


class FunctionalPathway(MetricAssessment):
    related_metrics: list[MetricAssessment] = Field(default_factory=list)
    related_taxa: list[TaxonSignal] = Field(default_factory=list)
    health_meaning: str | None = None
    cross_references: list[str] = Field(default_factory=list)


class FunctionalPathwaysSection(EvaluationModel):
    description: str = catalog.PATHWAY_DESCRIPTION
    pathways: list[FunctionalPathway] = Field(default_factory=lambda: [
        FunctionalPathway(code=code, name=name) for code, name in catalog.PATHWAY_DEFINITIONS
    ])
    summary: TextAssessment = Field(default_factory=TextAssessment)
    availability: Availability = "unavailable"

    @field_validator("pathways")
    @classmethod
    def ordered_pathways(cls, rows):
        return _ordered_rows(rows, catalog.PATHWAY_DEFINITIONS, FunctionalPathway)


class PriorityDirection(EvaluationModel):
    rank: int | None = None
    name: str | None = None
    basis: str | None = None
    followup_action: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class NextStep(EvaluationModel):
    code: str | None = None
    name: str | None = None
    description: str | None = None
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class ComprehensiveConclusionSection(EvaluationModel):
    overall_conclusion: TextAssessment = Field(default_factory=TextAssessment)
    favorable_factors: TextAssessment = Field(default_factory=TextAssessment)
    priority_directions: list[PriorityDirection] = Field(default_factory=list)
    next_steps: list[NextStep] = Field(default_factory=lambda: [
        NextStep(code=code, name=name, description=description)
        for code, name, description in catalog.NEXT_STEP_DEFINITIONS
    ])
    availability: Availability = "unavailable"


class ReferenceLiterature(EvaluationModel):
    number: int
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: str | None = None
    journal: str | None = None
    url: str | None = None
    doi: str | None = None
    study_type: str | None = None
    # Grade of the source type (A guideline/systematic review, B trial or
    # cohort, C review/reference); not a GRADE certainty of a recommendation.
    evidence_grade: str | None = None
    supporting_sections: list[str] = Field(default_factory=list)


class SourceObservation(EvaluationModel):
    """Typed original-source inventory, including facts outside display slots.

    These values are observations or source model results, never newly inferred
    clinical findings. ``sources`` retains original row paths and source dates.
    """

    observation_id: str | None = None
    code: str | None = None
    name: str | None = None
    scientific_name: str | None = None
    value: AnswerValue = None
    unit: str | None = None
    reference_range: str | None = None
    source_status: AnswerValue = None
    kind: str | None = None
    category: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    observed_at: str | None = None
    description: str | None = None
    related_taxa: list[str] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    availability: Availability = "unavailable"


class AppendicesSection(EvaluationModel):
    indicator_standards: list[StandardDefinition] = Field(default_factory=lambda: [StandardDefinition(**x) for x in catalog.APPENDIX_A_ROWS])
    reference_description: str = catalog.APPENDIX_B_DESCRIPTION
    reference_literature: list[ReferenceLiterature] = Field(default_factory=list)
    source_items: list[SourceObservation] = Field(default_factory=list)

    @field_validator("indicator_standards")
    @classmethod
    def fixed_indicator_standards(cls, rows):
        expected = catalog.APPENDIX_A_ROWS
        if ([row.code for row in rows] != [item['code'] for item in expected]
                or any(row.name != item['name'] and not (
                    row.code == 'diseaseRiskIndex' and row.name in {'疾病与健康风险综合关注指数', '疾病与健康风险综合评分'})
                    for row, item in zip(rows, expected, strict=True))):
            raise ValueError("appendix A identities and order are fixed")
        return rows


@dataclass(frozen=True)
class _ClinicalProblemQuote:
    fact_id: str
    quote: str
    name: str
    unit: str | None
    reference_range: str | None
    status: str | None
    date: str | None
    source_code: str
    role: str = 'problem_witness'


@dataclass(frozen=True)
class _ClinicalProblemProjection:
    problem_id: str
    title: str
    certainty: str
    priority: str
    sources: tuple[_ClinicalProblemQuote, ...]

class EvaluationData(EvaluationModel):
    _clinical_problem_projection: tuple[_ClinicalProblemProjection, ...] = PrivateAttr(default=())
    _assessment_editorial: dict[str, TextAssessment] = PrivateAttr(default_factory=dict)
    introduction: IntroductionSection = Field(default_factory=IntroductionSection)
    health_overview: HealthOverviewSection = Field(default_factory=HealthOverviewSection)
    medical_assessment: MedicalAssessmentSection = Field(default_factory=MedicalAssessmentSection)
    microbiome_assessment: MicrobiomeAssessmentSection = Field(default_factory=MicrobiomeAssessmentSection)
    disease_risk_assessment: DiseaseRiskAssessmentSection = Field(default_factory=DiseaseRiskAssessmentSection)
    nutrition_assessment: NutritionAssessmentSection = Field(default_factory=NutritionAssessmentSection)
    functional_pathways: FunctionalPathwaysSection = Field(default_factory=FunctionalPathwaysSection)
    comprehensive_conclusion: ComprehensiveConclusionSection = Field(default_factory=ComprehensiveConclusionSection)
    appendices: AppendicesSection = Field(default_factory=AppendicesSection)
    assessment_presentation: AssessmentPresentation | None = None


def bind_clinical_problem_projection(data, document):
    """Bind organizer-verified original quotations for delivery, not analysis."""
    facts = {fact.fact_id: fact for fact in document.facts}
    from .evaluation_risk_projection import (
        _compact_published_reported_findings, _reported_finding_is_covered,
    )
    _compact_published_reported_findings(data, facts)
    delivered = [row for row in data.medical_assessment.findings
        if str(row.code or '').startswith('integratedRisk:')]
    from .clinical_report import (_source_lesion_measurement_quotes,
        _reported_problem_quote_parts, _reported_answer_problem_witness)
    result = []
    for problem in document.problems:
        sources = []
        for source in problem.sources:
            fact = facts.get(source.fact_id)
            if fact is None or fact.kind not in {'clinical', 'self_reported'} \
                    or not source.quote.strip() or source.quote not in fact.value:
                break
            quote = source.quote
            if fact.kind == 'self_reported':
                if not _reported_answer_problem_witness(fact.value, problem.title):
                    break
                quote, contexts = _reported_problem_quote_parts(fact.value, problem.title)
                for context in contexts:
                    witness = _ClinicalProblemQuote(fact.fact_id, context, fact.name,
                        fact.unit, fact.reference_range, fact.status, fact.date,
                        'B', 'question_context')
                    if witness not in sources:
                        sources.append(witness)
            witness = _ClinicalProblemQuote(fact.fact_id, quote, fact.name,
                fact.unit, fact.reference_range, fact.status, fact.date,
                'A' if fact.kind == 'clinical' else 'B')
            if witness not in sources:
                sources.append(witness)
            if fact.kind == 'clinical' and fact.imaging:
                for quote in _source_lesion_measurement_quotes(fact.value, problem.title):
                    if quote not in source.quote:
                        sources.append(_ClinicalProblemQuote(fact.fact_id, quote, fact.name,
                            fact.unit, fact.reference_range, fact.status, fact.date, 'A'))
        else:
            if sources:
                if problem.certainty == 'self_reported':
                    observed = MedicalFinding(name=problem.title, certainty=problem.certainty,
                        value='；'.join(source.quote for source in sources),
                        sources=[SourceReference(source_code=source.source_code,
                            record_id=source.fact_id, observed_at=source.date) for source in sources])
                    if _reported_finding_is_covered(observed, delivered, facts):
                        continue
                result.append(_ClinicalProblemProjection(problem.problem_id, problem.title,
                    problem.certainty, problem.priority, tuple(sources)))
    data._clinical_problem_projection = tuple(result)


def _association_source_identity(row, key, prefix):
    """Locate one positive native panel in its original PDF/page and date."""
    code = str(row.get('code') or '')
    if not code.startswith(prefix):
        return None
    status = row.get(key('source_status'))
    if not isinstance(status, str) or status not in {'检出', '阳性', '异常', '检出相关菌群异常'}:
        return None
    refs = row.get('sources') or []
    if not refs or any(ref.get(key('source_code')) != 'C' for ref in refs):
        return None
    dates = {ref.get(key('observed_at')) for ref in refs}
    if None in dates or len(dates) != 1:
        return None
    anchors = set()
    panel = code.removeprefix(prefix)
    for ref in refs:
        record = str(ref.get(key('record_id')) or '')
        if not record.startswith('source:'):
            continue
        match = re.fullmatch(r'source:([a-f0-9]{64}):page:(\d+)(?::template:([^:]+))?', record)
        if match is None or match[3] not in (None, panel + '_all_result'):
            return None
        anchors.add((match[1], match[2]))
    origin = ('pdf', tuple(sorted(anchors))) if anchors else ('exact', tuple(sorted(
        (str(ref.get(key('record_id')) or ''), ref.get(key('observed_at'))) for ref in refs)))
    name = catalog.fixed_disease_code(row.get('name')) or row.get('name')
    return (code, name, status, row.get('unit'), row.get(key('reference_range')),
        tuple(dates), origin)


def _coalesce_association_findings(rows, key):
    """Join two views of one source panel, never distinct measurements.

    Native PDF panels may have both a template-cell locator and a page
    locator. Both must identify the same original PDF/page, panel, date and
    result. Raw measurements remain in the working/source inventories.
    """
    def identity(row):
        if row.get('certainty') != 'source_association':
            return None
        return _association_source_identity(row, key, 'associationFinding:nativeAssociation:')

    result, positions, removed = [], {}, []
    for row in rows:
        stamp = identity(row)
        candidates = positions.get(stamp, []) if stamp is not None else []
        for index in candidates:
            old = result[index]
            values = {json.dumps(value, ensure_ascii=False, sort_keys=True) for value in
                (old.get('value'), row.get('value')) if value is not None}
            levels = {value for value in (old.get(key('assessment_level')), row.get(key('assessment_level'))) if value is not None}
            if len(values) > 1 or len(levels) > 1:
                continue
            # A missing display value may share its original detection flag;
            # it must not borrow an unrelated numerical measurement.
            if values and next(iter(values)) != json.dumps(row.get(key('source_status')), ensure_ascii=False):
                continue
            preferred, other = (row, old) if row.get(key('assessment_level')) is not None else (old, row)
            merged = dict(preferred)
            merged['value'] = old.get('value') if old.get('value') is not None else row.get('value')
            merged[key('assessment_level')] = next(iter(levels), None)
            if not merged.get('explanation'):
                merged['explanation'] = other.get('explanation')
                merged[key('evidence_refs')] = list(other.get(key('evidence_refs')) or [])
            if not merged.get(key('source_advice')):
                merged[key('source_advice')] = other.get(key('source_advice'))
            merged['sources'] = list(old.get('sources') or [])
            merged['sources'].extend(ref for ref in row.get('sources') or [] if ref not in merged['sources'])
            result[index] = merged
            removed.append(other)
            break
        else:
            if stamp is not None:
                positions.setdefault(stamp, []).append(len(result))
            result.append(row)
    return result, removed


def _publish_clinical_problem_findings(data, working):
    """Publish problem-bound evidence without changing the working inventory."""
    camel = 'medicalAssessment' in data
    key = lambda value: _camel(value) if camel else value
    section = data.get(key('medical_assessment'), {})
    rows = section.get('findings', [])
    frames = [frame for frame in working._clinical_problem_projection
        if isinstance(frame, _ClinicalProblemProjection)]
    for row in rows:
        if str(row.get('code') or '').startswith('integratedRisk:'):
            continue
        if row.get('certainty') in {'model_risk', 'source_association'}:
            from .evaluation_fixed_assessment import medical_finding_display_name, medical_finding_specialty
            name = medical_finding_display_name(str(row.get('name') or ''))
            if str(row.get('code') or '').startswith('associationFinding:'):
                name = re.sub(r'相关菌群异常$', '', name)
            row['name'] = name if re.search(r'风险|易感', name) else name + '相关风险'
            row['system'] = medical_finding_specialty(row)
    rows, coalesced = _coalesce_association_findings(rows, key)
    section['findings'] = rows
    # A disease assessment and a source observation have separate duties.
    # Only the exact coverage checks below may retire an observation; the
    # presence of any live disease projection does not cover every symptom
    # or every finding from a multi-problem image.
    if not frames:
        return coalesced
    from .evaluation_builder import _source_bound_problem_title, _status_state, _clinical_number, _qualitative_polarity
    from .evaluation_fixed_assessment import medical_finding_specialty
    from .evaluation_narrative import _outside_printed_reference
    from .evaluation_risk_projection import integrated_clinical_support_ids

    clinical_support = integrated_clinical_support_ids(working)

    def ids(row):
        return {ref.get(key('record_id')) for ref in row.get('sources', [])}
    def reference_matched_categorical(value, name, reference):
        text = str(value or '').strip(' 。.')
        expected = str(reference or '').strip(' 。.')
        return bool(text == expected and (text in {'未查见', '未见', '未检出', '未检测到', '阴性'}
            or text == '软便' and re.fullmatch(r'(?:大便|粪便)?性状', str(name or ''))))
    def normal_value(value, name=None, reference=None):
        text = str(value or '').strip()
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict) and isinstance(obj.get('conclusion'), str):
            text = obj['conclusion'].strip()
        # Descriptive colour/clarity values are normal only in their own
        # assay or when the printed categorical reference agrees exactly.
        expected = str(reference or '').strip()
        if reference_matched_categorical(text, name, expected):
            return True  # Exact categorical assay/reference agreement, not a new diagnosis.
        ordinary_colour = text in {'黄色', '淡黄色'} and bool(re.fullmatch(
            r'(?:尿液?|粪便|大便)?(?:颜色|色泽)', str(name or '')))
        ordinary_clarity = text == '透明' and bool(re.fullmatch(
            r'(?:尿液?)?(?:浊度|透明度)', str(name or '')))
        if ordinary_clarity or ordinary_colour and expected == text:
            return True
        clauses = [re.sub(r'^[^：:]+[：:]', '', part).strip(' 。.')
            for part in re.split(r'[；;\n]', text) if part.strip(' 。.')]
        normal_parts = {'无殊', '未见明显异常', '无明显异常', '未见异常', '齐', '肋下未及', '无叩击痛',
            '对称引出，无亢进或减弱', '下肢血管堵塞状况正常', '心脑血管风险度在同龄标准范围内'}
        return bool(clauses and all(part in normal_parts for part in clauses)
            or re.fullmatch(r'(?:本次肿瘤标志物均正常范围|窦性心律\s*正常范围心电图|'
            r'[（(]?尿液[）)]?未见肿瘤细胞[。.]?|未见(?:明显)?异常[。.]?|正常(?:范围(?:内)?)?|无殊|无)', text)
            or re.fullmatch(r'阴性\s*Hp\s*\(-\)', text, re.I)
            or '正常范围' in text and '风险较低' in text)

    grouped = {}
    canonical_codes = {row.get('code') for row in rows
        if str(row.get('code') or '').startswith('clinicalProblem:')}
    frame_codes = clinical_problem_projection_codes(working)
    for frame in frames:
        # A reviewed integrated problem already carries these scalar or
        # self-reported observations as its supporting remarks. Broad images
        # remain independent because one image can contain other problems.
        own = {source.fact_id for source in frame.sources}
        integrated = [row for row in rows if str(row.get('code') or '').startswith('integratedRisk:')
            and row.get('certainty') in {'confirmed', 'current_problem', 'self_reported'}
            and own <= ids(row)]
        def displayed_quote(source):
            quote = re.sub(r'^\s*回答[：:]\s*', '', source.quote)
            return re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', quote).strip('；; 。')
        already_materialized = frame_codes.get(frame.problem_id) in canonical_codes
        if not already_materialized and integrated and all((frame.certainty == 'self_reported'
                or _clinical_number(source.quote, source.unit) is not None
                or re.fullmatch(r'\s*[<>≤≥]?[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*'
                    r'[A-Za-zμµ%/⋅·^0-9⁹²³*.-]{1,24}\s*', source.quote))
                and any(displayed_quote(source) in str(row.get('value') or '') for row in integrated)
                for source in frame.sources):
            continue
        # A potential disease can carry an abnormal scalar as a clinical
        # remark only through the same live, reviewed outcome link. Sharing
        # a source ID alone does not cover a diagnosis or a wider image.
        if not already_materialized and frame.certainty == 'source_finding' and own and all(
                source.source_code == 'A'
                and _clinical_number(source.quote, source.unit) is not None
                for source in frame.sources):
            potential = [row for row in rows
                if str(row.get('code') or '').startswith('integratedRisk:')
                and row.get('certainty') == 'potential_risk'
                and own <= clinical_support.get(row.get('code'), set())
                and own <= ids(row)]
            if any(all(re.search(r'(?<![\d.])' + re.escape(displayed_quote(source))
                    + r'(?![\d.])', str(row.get('value') or ''))
                    for source in frame.sources) for row in potential):
                continue
        title = _source_bound_problem_title(frame, [source.quote for source in frame.sources])
        if frame.certainty == 'self_reported':
            title = re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', title).strip()
        dates = tuple(sorted({source.date[:10] for source in frame.sources if source.date}))
        group = grouped.setdefault((title, frame.certainty, dates), {'frames': [], 'quotes': []})
        group['frames'].append(frame)
        for quote in frame.sources:
            if quote not in group['quotes']:
                group['quotes'].append(quote)
    occurrence = {}
    for group in grouped.values():
        for fid in {quote.fact_id for quote in group['quotes']}:
            occurrence[fid] = occurrence.get(fid, 0) + 1
    projected, covered = [], set()
    for (title, certainty, _dates), group in grouped.items():
        quotes = group['quotes']
        own = {quote.fact_id for quote in quotes}
        problem_code = _clinical_problem_code(title, certainty, _dates, quotes)
        exact = [(index, row) for index, row in enumerate(rows)
            if row.get('code') == problem_code]
        if len(exact) == 1:
            # Materialization happens before authoring.  The final serializer
            # transports that same owner rather than regrouping reviewed
            # prose under a new array position or a shared imaging fact.
            index, row = exact[0]
            covered.add(index)
            projected.append((min({'priority': 0, 'review': 1, 'routine': 2}.get(frame.priority, 2)
                for frame in group['frames']), row))
            continue
        matched = [(index, row) for index, row in enumerate(rows) if ids(row) & own
            and row.get('certainty') not in {'model_risk', 'source_association'}
            and not str(row.get('code') or '').startswith('integratedRisk:')]
        covered.update(index for index, _ in matched)
        values, advice, explanations, refs, sources, systems = [], [], [], [], [], []
        for quote in quotes:
            corresponding = [row for _, row in matched if quote.fact_id in ids(row)]
            value = displayed_quote(quote) if quote.source_code == 'B' else quote.name + '：' + quote.quote
            if quote.unit and quote.unit not in quote.quote:
                value += ' ' + quote.unit
            labels = ['参考' + quote.reference_range] if quote.reference_range else []
            labels.extend(dict.fromkeys([str(quote.status)] if quote.status and quote.source_code != 'B' else []))
            # A working assessment of an image is not a printed source
            # grade. Only the immutable original status qualifies the quote.
            if labels:
                value += '（' + '；'.join(labels) + '）'
            if value not in values:
                values.append(value)
            source = SourceReference(source_code=quote.source_code, record_id=quote.fact_id,
                observed_at=quote.date).model_dump(mode='json', by_alias=camel)
            if source not in sources:
                sources.append(source)
            for row in corresponding:
                for source in row.get('sources', []):
                    if source not in sources:
                        sources.append(source)
                # A shared image's wider explanation/advice does not become
                # an explanation of each individual problem in that image.
                same_problem = title == str(row.get('name') or '').removeprefix('主检结论：') \
                    or title == str(row.get('value') or '').rstrip('。')
                if (title == row.get('name') and certainty == row.get('certainty')
                        and row.get('system') and row['system'] not in systems):
                    # Narrowing the literal answer does not change the
                    # existing source owner's organizational display label.
                    systems.append(row['system'])
                same_scalar = (occurrence[quote.fact_id] == 1 and len(own) == 1
                    and ids(row) == own and _clinical_number(str(row.get('value') or ''),
                        row.get('unit')) is not None)
                if occurrence[quote.fact_id] == 1 and (same_problem or same_scalar):
                    original_advice = row.get(key('source_advice'))
                    if original_advice and original_advice not in advice:
                        advice.append(original_advice)
                # A newly materialized owner has no reviewed explanation.
                # Only the exact-code branch above transports authored text;
                # a matching title or shared scalar cannot recreate proof
                # after the source value/date/qualification has changed.
        primary_source = 'B' if certainty == 'self_reported' else 'A'
        original_statuses = {quote.status for quote in quotes
            if quote.status and quote.source_code == primary_source}
        row = MedicalFinding(code=problem_code, name=title,
            value='；'.join(values), source_advice='；'.join(advice) or None,
            source_status=next(iter(original_statuses)) if len(original_statuses) == 1 else None,
            explanation='；'.join(explanations) or None, certainty=certainty,
            sources=[SourceReference.model_validate(source) for source in sources],
            evidence_refs=sorted(set(refs)), availability='partial').model_dump(mode='json', by_alias=camel)
        row['system'] = systems[0] if len(systems) == 1 else medical_finding_specialty({'name': title,
            'sourceAdvice': row.get(key('source_advice'))})
        if explicit := re.search(r'肝胆外科|甲状腺外科|泌尿科', str(row.get(key('source_advice')) or '')):
            row['system'] = explicit[0]
        projected.append((min({'priority': 0, 'review': 1, 'routine': 2}.get(frame.priority, 2)
            for frame in group['frames']), row))
    background_ids = {source.record_id for item in working.appendices.source_items
        if item.kind == 'clinical' and str(item.category or '').startswith('clinicalReportedBackground:')
        for source in item.sources if source.source_code == 'A' and source.record_id}
    represented_background_ids = {source.record_id
        for item in working.medical_assessment.questionnaire_summary
        if item.value is not None for source in item.sources
        if source.source_code == 'A' and source.record_id} & background_ids
    removed = list(coalesced)
    for index, row in enumerate(rows):
        if index in covered:
            continue
        if str(row.get('code') or '').startswith('integratedRisk:'):
            projected.append((-1, row))
            continue
        compatible = {_camel(k): v for k, v in row.items()} if not camel else row
        neutral = {'', '-', '--', '—', '–', '/', '／', '本次测量', '测量值', '实测值', '未分级'}
        source_signal = row.get('certainty') in {'confirmed', 'suspected', 'self_reported', 'model_risk', 'source_association'} \
            or any(str(compatible.get(field) or '').strip() not in neutral
                and _status_state(str(compatible[field])) != 'normal'
                for field in ('sourceStatus', 'assessmentLevel'))
        if not source_signal and not compatible.get('sourceAdvice') and ids(row) & represented_background_ids:
            # The exact A background is already present in section 3.2; it
            # does not become another current disease/abnormal examination.
            removed.append(row)
            continue
        ungraded_limit = bool(re.fullmatch(r'\s*(?:<|<=|≤)\s*\d+(?:\.\d+)?\s*', str(row.get('value') or ''))
            and re.search(r'\d', str(compatible.get('referenceRange') or '')) and not compatible.get('certainty')
            and not compatible.get('sourceAdvice') and not compatible.get('sourceStatus')
            and not compatible.get('assessmentLevel') and not _outside_printed_reference(
                compatible['value'], compatible['referenceRange'], compatible.get('unit')))
        value = '' if row.get('value') is None else str(row['value']).strip()
        outside = _outside_printed_reference(value, compatible.get('referenceRange'), compatible.get('unit'))
        categorical_mismatch = (_qualitative_polarity(value) in {'positive', 'negative'}
            and _qualitative_polarity(str(compatible.get('referenceRange') or '')) in {'positive', 'negative'}
            and _qualitative_polarity(value) != _qualitative_polarity(str(compatible.get('referenceRange') or '')))
        unknown_qualitative = bool(value and _clinical_number(value, compatible.get('unit')) is None
            and not re.fullmatch(r'[+-]?\d+(?:\.\d+)?\s*/\s*[+-]?\d+(?:\.\d+)?', value)
            and _qualitative_polarity(value) != 'negative')
        if source_signal or (not normal_value(row.get('value'), row.get('name'), compatible.get('referenceRange')) and not ungraded_limit \
                and (outside or categorical_mismatch or _qualitative_polarity(value) == 'positive' or unknown_qualitative)):
            projected.append((3, row))
        else:
            if not source_signal and reference_matched_categorical(
                    row.get('value'), row.get('name'), compatible.get('referenceRange')):
                normals = section.setdefault(key('normal_findings'), [])
                if row not in normals:
                    normals.append(row)  # Keep the exact original result, grade and source references.
            removed.append(row)
    section['findings'] = [row for _, row in sorted(projected, key=lambda item: item[0])]
    return removed


def _clinical_problem_code(title, certainty, dates, quotes):
    """Identity of one source problem, including every literal witness.

    A shared image, a changed date or a differently qualified finding must
    not inherit another problem's reviewed explanation or action.
    """
    witnesses = sorted((quote.fact_id, quote.quote, quote.name, quote.unit or '',
        quote.reference_range or '', quote.status or '', quote.date or '', quote.source_code)
        for quote in quotes)
    digest = hashlib.sha256(json.dumps([title, certainty, list(dates), witnesses],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    return 'clinicalProblem:' + digest


def clinical_problem_projection_code(frame):
    """Return the canonical working/public owner for one source frame."""
    from .evaluation_builder import _source_bound_problem_title
    title = _source_bound_problem_title(frame, [source.quote for source in frame.sources])
    if frame.certainty == 'self_reported':
        title = re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', title).strip()
    dates = tuple(sorted({source.date[:10] for source in frame.sources if source.date}))
    return _clinical_problem_code(title, frame.certainty, dates, frame.sources)


def clinical_problem_projection_codes(data):
    """Map exact organizer IDs to their canonical grouped source owners."""
    from .evaluation_builder import _source_bound_problem_title
    groups = {}
    for frame in data._clinical_problem_projection:
        title = _source_bound_problem_title(frame, [source.quote for source in frame.sources])
        if frame.certainty == 'self_reported':
            title = re.sub(r'[（(]用户自述(?:，未核验)?[）)]', '', title).strip()
        dates = tuple(sorted({source.date[:10] for source in frame.sources if source.date}))
        group = groups.setdefault((title, frame.certainty, dates), {'ids': [], 'quotes': []})
        group['ids'].append(frame.problem_id)
        group['quotes'].extend(source for source in frame.sources if source not in group['quotes'])
    return {problem_id: _clinical_problem_code(title, certainty, dates, group['quotes'])
        for (title, certainty, dates), group in groups.items() for problem_id in group['ids']}


def materialize_clinical_problem_findings(data):
    """Create source-bound problem owners before narrative composition.

    This is the same delivery projection, applied to the working medical
    section.  It grants no interpretation, grade or action permission and
    leaves the complete source inventory untouched.
    """
    payload = {'medicalAssessment': data.medical_assessment.model_dump(
        mode='json', by_alias=True)}
    _publish_clinical_problem_findings(payload, data)
    section = MedicalAssessmentSection.model_validate(payload['medicalAssessment'])
    data.medical_assessment.findings = section.findings
    data.medical_assessment.normal_findings = section.normal_findings
    return data


class EvaluationSubject(EvaluationModel):
    name: str | None = None
    gender: str | None = None
    age: StrictInt | StrictStr | None = None


class EvaluationSample(EvaluationModel):
    sample_id: str | None = None
    sample_type: str | None = None
    test_item: str | None = None
    sample_date: str | None = None
    report_date: str | None = None
    sequencing_platform: str | None = None
    method: str | None = None
    effective_sequences: int | None = None
    quality_control: str | None = None


class EvaluationError(EvaluationModel):
    error_code: str | None = None
    message: str | None = None


def _coalesce_public_metric_rows(rows):
    """Coalesce proved duplicate display results, retaining every citation.

    Only the three fixed ratio/index aliases may omit an otherwise identical
    reference range. Two different nonempty ranges never select one another.
    """
    fixed_codes = {code for code, _ in (*catalog.OVERALL_STRUCTURE_DEFINITIONS, *catalog.HEALTH_METRIC_DEFINITIONS)}
    optional_range_aliases = {"gmwi2", "beRatio", "fbRatio"}

    def field(row, camel, snake=None):
        return row[camel] if camel in row else row.get(snake or camel)

    def typed(value):
        return type(value).__name__, value

    def has_range(row):
        value = field(row, "referenceRange", "reference_range")
        return value is not None and (not isinstance(value, str) or bool(value.strip()))

    groups = {}
    for index, row in enumerate(rows):
        code = row.get("code")
        sources = row.get("sources") or []
        if code not in fixed_codes or not sources or any(field(source, "sourceCode", "source_code") is None for source in sources):
            continue
        scope = frozenset((field(source, "sourceCode", "source_code"), field(source, "observedAt", "observed_at")) for source in sources)
        key = (code, *(typed(field(row, camel, snake)) for camel, snake in (
            ("value", "value"), ("unit", "unit"), ("sourceStatus", "source_status"),
            ("assessmentLevel", "assessment_level"), ("explanation", "explanation"),
            ("availability", "availability"))), scope)
        groups.setdefault(key, []).append((index, row))
    replacements, removed = {}, set()
    for key, members in groups.items():
        if key[0] in optional_range_aliases:
            references = {typed(field(row, "referenceRange", "reference_range")) for _, row in members if has_range(row)}
            candidates = [members] if len(references) <= 1 else []
        else:
            identical = {}
            for index, row in members:
                identical.setdefault(typed(field(row, "referenceRange", "reference_range")), []).append((index, row))
            candidates = list(identical.values())
        for duplicates in candidates:
            if len(duplicates) < 2:
                continue
            representative = next((row for _, row in duplicates if has_range(row)), duplicates[0][1])
            merged = dict(representative)
            evidence_key = "evidenceRefs" if "evidenceRefs" in merged else "evidence_refs"
            merged["sources"], merged[evidence_key] = [], []
            for index, row in duplicates:
                removed.add(index)
                for source in row.get("sources") or []:
                    if source not in merged["sources"]:
                        merged["sources"].append(source)
                for number in field(row, "evidenceRefs", "evidence_refs") or []:
                    if number not in merged[evidence_key]:
                        merged[evidence_key].append(number)
            replacements[duplicates[0][0]] = merged
    return [replacements[index] if index in replacements else row
        for index, row in enumerate(rows) if index not in removed or index in replacements]


def _coalesce_phylum_rows(rows):
    """Merge the same printed result after decimal-only range formatting.

    The original range string of the first row is retained. Source checksum,
    dates, units, grades and values must agree; other source rows stay separate.
    """
    number = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)'

    def interval(value):
        match = re.fullmatch(r'\s*(' + number + r')\s*([~～])\s*(' + number + r')\s*', value or '')
        return ('decimal_interval', Decimal(match[1]), Decimal(match[3])) if match else ('literal', value)

    result, seen = [], {}
    for row in rows:
        sources = row.get('sources') or []
        origins = frozenset(match[1] for source in sources
            if (match := re.match(r'^source:([a-fA-F0-9]{64}):', source.get('recordId', source.get('record_id')) or '')))
        if not row.get('code') or not origins:
            result.append(row)
            continue
        key = (row['code'], row.get('name'), origins,
            frozenset((source.get('sourceCode', source.get('source_code')),
                source.get('observedAt', source.get('observed_at'))) for source in sources),
            *( (type(value).__name__, value) for value in (
                row.get('value'), row.get('unit'), row.get('sourceStatus', row.get('source_status')),
                row.get('assessmentLevel', row.get('assessment_level')), row.get('explanation'), row.get('availability'))),
            interval(row.get('referenceRange', row.get('reference_range'))))
        if key not in seen:
            merged = {**row, 'sources': list(sources)}
            refs_key = 'evidenceRefs' if 'evidenceRefs' in row else 'evidence_refs'
            merged[refs_key] = list(row.get(refs_key) or [])
            seen[key] = merged
            result.append(merged)
            continue
        merged = seen[key]
        refs_key = 'evidenceRefs' if 'evidenceRefs' in row else 'evidence_refs'
        for source in sources:
            if source not in merged['sources']:
                merged['sources'].append(source)
        for reference in row.get(refs_key) or []:
            if reference not in merged[refs_key]:
                merged[refs_key].append(reference)
    return result


def _restore_encoded_author(value):
    """Recover reversible UTF-8 mojibake only in bibliographic author names."""
    if not any(marker in value for marker in ('√', 'Ã', 'Â', 'â€')):
        return value
    for encoding in ('mac_roman', 'latin1'):
        try:
            recovered = value.encode(encoding).decode('utf-8')
            if recovered.encode('utf-8').decode(encoding) == value and not any(
                    marker in recovered for marker in ('√', 'Ã', 'Â', 'â€', '\ufffd')):
                return recovered
        except (UnicodeError, LookupError):
            continue
    return value


def _has_public_result(row):
    """A declared slot is not a result, and zero is not a missing result."""
    return any(row.get(key) is not None and (not isinstance(row.get(key), str) or row[key].strip())
        for key in ('value', 'sourceStatus', 'source_status', 'assessmentLevel', 'assessment_level'))


def _declares_integrated_health_scale(data):
    disease = data.get('diseaseRiskAssessment', data.get('disease_risk_assessment', {}))
    return [(row.get('code'), row.get('referenceRange', row.get('reference_range')))
        for row in disease.get('riskIndexStandards', disease.get('risk_index_standards', []))] == [
            (row['code'], row['referenceRange']) for row in catalog.PRESENTATION_RISK_INDEX_STANDARDS]


def _pending_integrated_health_disease(row, health_scale):
    """Keep an existing typed disease row, without certifying its analysis."""
    code = str(row.get('code') or '').split(':', 2)
    return bool(health_scale and not _has_public_result(row) and row.get('score') is None
        and len(code) == 3 and code[0] == 'integratedRisk'
        and code[1] in dict(catalog.RISK_CATEGORY_DEFINITIONS) and code[2].strip()
        and isinstance(row.get('name'), str) and row['name'].strip()
        and any(source.get('sourceCode', source.get('source_code')) in {'A', 'B', 'C'}
            and isinstance(source.get('recordId', source.get('record_id')), str)
            and source.get('recordId', source.get('record_id')).strip()
            for source in row.get('sources', [])))


def _fixed_public_metric_rows(rows, definitions, row_type=MetricAssessment, *, by_alias=True,
                              preserve_extra=True, identity=None):
    """Backfill template identities without inventing observations or grades.

    Existing source names, literal values and source relations remain exact.
    Only truly empty duplicate placeholders may be replaced. Extra actual
    source observations stay available unless this is the fixed disease table.
    """
    identity = identity or (lambda row: catalog.fixed_metric_code(row.get('code'), definitions)
        or catalog.fixed_metric_code(row.get('name'), definitions))
    grouped, extras = {}, []
    for row in rows or []:
        code = identity(row)
        if code in dict(definitions):
            grouped.setdefault(code, []).append(row)
        elif preserve_extra and _has_public_result(row):
            extras.append(row)

    def selection_key(row):
        observed = [str(source.get('observedAt', source.get('observed_at')) or '')
            for source in row.get('sources', [])]
        numeric = row.get('value') is not None and not isinstance(row.get('value'), bool)
        return (_has_public_result(row), numeric, max(observed, default=''))

    fixed = []
    for code, name in definitions:
        candidates = grouped.get(code, [])
        if not candidates:
            fixed.append(row_type(code=code, name=name).model_dump(mode='json', by_alias=by_alias))
            continue
        primary = max(candidates, key=selection_key)
        fixed.append(primary)
        if preserve_extra:
            extras.extend(row for row in candidates if row is not primary and _has_public_result(row)
                and row not in extras and row != primary)
    return [*fixed, *extras]


def compact_native_nutrition_mirrors(rows):
    """Drop only a valueless legacy view of the same literal native panel.

    A numeric assessment, different observation, date, grade or unit is not
    a mirror. The native row keeps its actual title/value and all provenance.
    """
    mapping = {'carbohydrate': 'yingyang2_tanshui2', 'fat': 'yingyang2_zhizhi2'}
    def get(row, name):
        return row.get(_camel(name), row.get(name)) if isinstance(row, dict) else getattr(row, name, None)
    def sources(row):
        return {repr((get(source, 'source_code'), get(source, 'record_id'), get(source, 'observed_at')))
            for source in get(row, 'sources') or []}
    result = []
    for row in rows:
        panel = mapping.get(get(row, 'code'))
        mirror = panel and get(row, 'value') is None and get(row, 'unit') is None and sources(row) and any(
            get(native, 'code') == 'nativeNutrition:' + panel
            and sources(native) == sources(row)
            and get(native, 'value') == get(row, 'source_status') == get(native, 'source_status')
            and get(native, 'assessment_level') == get(row, 'assessment_level') for native in rows)
        if not mirror:
            result.append(row)
    return result


def _complete_public_fixed_template(data):
    """The callback and preview share fixed slots even for absent input data."""
    camel = 'microbiomeAssessment' in data
    key = lambda a, b: a if camel else b
    from .evaluation_disease_catalog import source_catalog_family
    family = source_catalog_family(data)
    source_nutrition = catalog.source_nutrition_definitions(family)
    micro = data.get(key('microbiomeAssessment', 'microbiome_assessment'), {})
    for name, snake, definitions, row_type in (
        ('overallStructure', 'overall_structure', catalog.source_structure_definitions(family), MetricAssessment),
        ('healthMetrics', 'health_metrics', catalog.source_health_definitions(family), HealthMetricAssessment),
        ('shortChainFattyAcids', 'short_chain_fatty_acids', source_nutrition['scfa'], MetricAssessment)):
        name = key(name, snake)
        rows = micro.get(name, [])
        # Retired count summaries may share a real heading's name. They
        # cannot win its fixed slot and displace the measured source result.
        # Keep them until the existing compaction/citation cleanup below.
        retired = [row for row in rows if name == key('healthMetrics', 'health_metrics')
            and str(row.get('code') or '').startswith(('panelSummary:', 'panelResult:'))]
        micro[name] = [*_fixed_public_metric_rows([row for row in rows if row not in retired],
            definitions, row_type, by_alias=camel), *retired]

    disease = data.get(key('diseaseRiskAssessment', 'disease_risk_assessment'), {})
    integrated = any(str(row.get('code') or '').startswith('integratedRisk:')
        for row in disease.get(key('riskRanking', 'risk_ranking'), []))
    # A reviewed all-normal report has no active ranked rows. Its declared
    # composite scale must survive serialization instead of reverting to an
    # unrelated source-model scale merely because that list is empty.
    declared = [(row.get('code'), row.get(key('referenceRange', 'reference_range')))
        for row in disease.get(key('riskIndexStandards', 'risk_index_standards'), [])]
    declared_standards = next((standards for standards in (
            catalog.INTEGRATED_RISK_INDEX_STANDARDS, catalog.PRESENTATION_RISK_INDEX_STANDARDS,
            catalog.LEGACY_ATTENTION_PRESENTATION_STANDARDS) if declared == [
            (row['code'], row['referenceRange']) for row in standards]
        ), None)
    integrated = integrated or declared_standards is not None
    if integrated:
        disease[key('riskIndexStandards', 'risk_index_standards')] = [
            StandardDefinition(**item).model_dump(mode='json', by_alias=camel)
            for item in (declared_standards or catalog.INTEGRATED_RISK_INDEX_STANDARDS)]
    if family in {'GM', 'G1'}:
        if not integrated:
            disease[key('riskIndexStandards', 'risk_index_standards')] = [
                StandardDefinition(**item).model_dump(mode='json', by_alias=camel)
                for item in catalog.NATIVE_RISK_INDEX_STANDARDS]
        data.get('appendices', {})[key('indicatorStandards', 'indicator_standards')] = [
            StandardDefinition(**item).model_dump(mode='json', by_alias=camel)
            for item in catalog.source_indicator_standards(family)]
    appendix = data.get('appendices', {})
    standards_key = key('indicatorStandards', 'indicator_standards')
    appendix[standards_key] = [StandardDefinition(**item).model_dump(mode='json', by_alias=camel)
        for item in catalog.current_indicator_standards(appendix.get(standards_key, []),
            micro.get(key('overallStructure', 'overall_structure'), []), integrated=integrated,
            health_scale=declared_standards == catalog.PRESENTATION_RISK_INDEX_STANDARDS)]
    for category in disease.get('categories', []):
        definitions = catalog.fixed_disease_definitions(category.get('code'), family=family)
        category['items'] = _fixed_public_metric_rows(category.get('items', []), definitions,
            DiseaseRiskItem, by_alias=camel, preserve_extra=True,
            identity=lambda row: (row.get('code') if str(row.get('code') or '').startswith('nativeAssociation:')
                else catalog.fixed_disease_code(row.get('code')) or catalog.fixed_disease_code(row.get('name'))))

    nutrition = data.get(key('nutritionAssessment', 'nutrition_assessment'), {})
    for name, snake, definitions in (
        ('majorMetabolism', 'major_metabolism', source_nutrition['major']),
        ('neurotransmitters', 'neurotransmitters', source_nutrition['neuro']),
        ('aminoAcidsAndMetabolites', 'amino_acids_and_metabolites',
            source_nutrition['amino'])):
        name = key(name, snake)
        actual = nutrition.get(name, [])
        if snake == 'major_metabolism':
            panels = {row.get('code') for row in actual if row.get('value') is not None}
            definitions = tuple((code, title) for code, title in definitions if not (
                code in {'carbohydrate', 'fat'} and any(native in panels for native in (
                    'nativeNutrition:' + {'carbohydrate': 'yingyang2_tanshui2', 'fat': 'yingyang2_zhizhi2'}[code],))
                and not any(row.get('code') == code and row.get('value') is not None for row in actual)))
        nutrition[name] = _fixed_public_metric_rows(actual, definitions, by_alias=camel)
        if snake == 'major_metabolism':
            nutrition[name] = compact_native_nutrition_mirrors(nutrition[name])

    for name, snake, groups, items in (
        ('vitaminsAndMinerals', 'vitamins_and_minerals', source_nutrition['groups'],
            source_nutrition['group_items']),
        ('detailedMetabolism', 'detailed_metabolism', source_nutrition['detailed'],
            source_nutrition['detail_items'])):
        name = key(name, snake)
        existing = nutrition.get(name, [])
        by_code = {row.get('code'): row for row in existing}
        fixed = []
        for code, title in groups:
            row = by_code.get(code)
            if row is None:
                row = NutrientGroup(code=code, name=title).model_dump(mode='json', by_alias=camel)
            row['items'] = _fixed_public_metric_rows(row.get('items', []), items[code], by_alias=camel)
            fixed.append(row)
        nutrition[name] = [*fixed, *(row for row in existing if row.get('code') not in dict(groups))]


def _source_total_score_value(value):
    """Remove binary arithmetic noise without rounding a report's real score.

    A template JSON can carry 37.029999999999994 for the printed 37.03.
    Only snap a decimal within two binary ULPs; genuinely precise scores and
    non-numeric source text retain their original value and scalar type.
    """
    if type(value) not in (str, int, float):
        return value
    try:
        original = Decimal(str(value))
    except InvalidOperation:
        return value
    if not original.is_finite() or not 0 <= original <= 100:
        return value
    candidate = original.quantize(Decimal("0.0000000001"))
    tolerance = Decimal(str(math.ulp(float(original)))) * 2
    if abs(candidate - original) > tolerance:
        return value
    if isinstance(value, str):
        return format(candidate, "f").rstrip("0").rstrip(".") or "0"
    return int(candidate) if candidate == candidate.to_integral_value() else float(candidate)


def _publish_source_total_score(data):
    """Use the same source score precision in the overview and structure table."""
    overview = data.get("healthOverview", data.get("health_overview", {}))
    for row in overview.get("assessmentChart", overview.get("assessment_chart", [])):
        if row.get("code") == "microbiome":
            row["score"] = _source_total_score_value(row.get("score"))
    microbiome = data.get("microbiomeAssessment", data.get("microbiome_assessment", {}))
    for row in microbiome.get("overallStructure", microbiome.get("overall_structure", [])):
        if row.get("code") == "microbiomeHealthScore":
            row["value"] = _source_total_score_value(row.get("value"))


def _publish_independent_risk_levels(data):
    """Project three bands only for independent decisions, not raw results."""
    def section(owner, camel, snake):
        return owner.get(camel, owner.get(snake, {}))

    def field(owner, camel, snake):
        return camel if camel in owner else snake

    disease = section(data, 'diseaseRiskAssessment', 'disease_risk_assessment')
    standards_key = field(disease, 'riskIndexStandards', 'risk_index_standards')
    health_scale = [(row.get('code'), row.get(field(row, 'referenceRange', 'reference_range')))
        for row in disease.get(standards_key, [])] == [
            (row['code'], row['referenceRange']) for row in catalog.PRESENTATION_RISK_INDEX_STANDARDS]

    def grade(row, *, score=None, camel='assessmentLevel', snake='assessment_level'):
        name = field(row, camel, snake)
        value = ((catalog.presentation_health_level(score) if health_scale
            else catalog.presentation_risk_level(score)) if score is not None else None)
        row[name] = value or catalog.presentation_risk_label(row.get(name))

    overview = section(data, 'healthOverview', 'health_overview')
    for row in section(overview, 'assessmentChart', 'assessment_chart'):
        if row.get('code') in dict(catalog.RISK_CATEGORY_DEFINITIONS):
            grade(row, score=row.get('score'))
    grade(section(overview, 'overallScore', 'overall_score'),
        score=section(overview, 'overallScore', 'overall_score').get('score'))
    for row in section(overview, 'keyFindings', 'key_findings'):
        grade(row)
    medical = section(data, 'medicalAssessment', 'medical_assessment')
    for row in medical.get('findings', []):
        if str(row.get('code') or '').startswith('integratedRisk:'):
            grade(row)
    ranking = section(disease, 'riskRanking', 'risk_ranking')
    for row in ranking:
        if str(row.get('code') or '').startswith('integratedRisk:'):
            grade(row, score=row.get('value'))
    matrices = section(disease, 'overviewMatrix', 'overview_matrix')
    for row in matrices:
        grade(row, score=row.get('score'), camel='attentionLevel', snake='attention_level')
    levels = {row['code']: row.get(field(row, 'attentionLevel', 'attention_level')) for row in matrices}
    for category in disease.get('categories', []):
        name = field(category, 'attentionLevel', 'attention_level')
        category[name] = levels.get(category['code']) or catalog.presentation_risk_label(category.get(name))
        for row in category.get('items', []):
            if str(row.get('code') or '').startswith('integratedRisk:'):
                grade(row, score=row.get('value'))
    disease[field(disease, 'matrixLevels', 'matrix_levels')] = list(catalog.PRESENTATION_RISK_LEVELS)
    integrated = (any(str(row.get('code') or '').startswith('integratedRisk:') for row in ranking)
        or [(row.get('code'), row.get(field(row, 'referenceRange', 'reference_range')))
            for row in disease.get(standards_key, [])] in [
                [(row['code'], row['referenceRange']) for row in standard]
                for standard in (catalog.INTEGRATED_RISK_INDEX_STANDARDS, catalog.PRESENTATION_RISK_INDEX_STANDARDS,
                    catalog.LEGACY_ATTENTION_PRESENTATION_STANDARDS)])
    if integrated:
        disease[standards_key] = [StandardDefinition(**row).model_dump(mode='json',
            by_alias=standards_key == 'riskIndexStandards') for row in (
                catalog.PRESENTATION_RISK_INDEX_STANDARDS if health_scale
                else catalog.LEGACY_ATTENTION_PRESENTATION_STANDARDS)]


def _retire_nutrition_scalar_explanations(data):
    """Remove per-item definitions, retaining values and group-level science."""
    camel = 'nutritionAssessment' in data
    nutrition = data.get('nutritionAssessment', data.get('nutrition_assessment', {}))
    rows = [*nutrition.get('aminoAcidsAndMetabolites', nutrition.get('amino_acids_and_metabolites', [])),
        *nutrition.get('neurotransmitters', [])]
    for left, right in (('detailedMetabolism', 'detailed_metabolism'),
            ('vitaminsAndMinerals', 'vitamins_and_minerals')):
        rows.extend(row for group in nutrition.get(left, nutrition.get(right, []))
            for row in group.get('items', []))
    removed = []
    for row in rows:
        refs_key = 'evidenceRefs' if ('evidenceRefs' in row or camel and 'evidence_refs' not in row) else 'evidence_refs'
        if row.get(refs_key):
            removed.append({refs_key: list(row[refs_key])})
        row['explanation'], row[refs_key] = None, []
    weak_key = ('weakSynthesisChart' if ('weakSynthesisChart' in nutrition
        or camel and 'weak_synthesis_chart' not in nutrition) else 'weak_synthesis_chart')
    removed.extend(nutrition.get(weak_key, []))
    nutrition[weak_key] = []
    return removed


def _evaluation_report_date(generated_at):
    """Use the generation instant in Beijing, independently of sample dates."""
    if isinstance(generated_at, datetime):
        instant = generated_at
    elif isinstance(generated_at, date):
        return generated_at
    elif isinstance(generated_at, str):
        try:
            instant = datetime.fromisoformat(generated_at.replace('Z', '+00:00'))
        except ValueError:
            instant = None
    else:
        instant = None
    zone = ZoneInfo('Asia/Shanghai')
    if instant is None:
        return datetime.now(zone).date()
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return instant.astimezone(zone).date()


class EvaluationCallback(EvaluationModel):
    schema_version: Literal["evaluate.v2.0"] = catalog.SCHEMA_VERSION
    user_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    group_id: str | None = None
    fmt_typing_report_url: str | None = None
    package: list[Any] = Field(default_factory=list)
    final_report_id: str | None = None
    type: Literal["evaluate"] = "evaluate"
    status: Literal["success", "partial_success", "failed"]
    generated_at: str | None = None
    report_date: date
    partial_reasons: list[str] = Field(default_factory=list)
    subject: EvaluationSubject = Field(default_factory=EvaluationSubject)
    sample: EvaluationSample = Field(default_factory=EvaluationSample)
    data: EvaluationData = Field(default_factory=EvaluationData)
    error: EvaluationError = Field(default_factory=EvaluationError)

    @model_validator(mode='before')
    @classmethod
    def supply_report_date(cls, value):
        if not isinstance(value, Mapping):
            return value
        key = 'reportDate' if 'reportDate' in value else 'report_date'
        if value.get(key) is not None:
            return value
        generated_at = value.get('generatedAt', value.get('generated_at'))
        return {**value, key: _evaluation_report_date(generated_at)}

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """Document only callback fields, without changing the working DTO."""
        schema = handler(core_schema)
        public_schema = handler.resolve_ref_schema(schema)
        data_schema = handler.resolve_ref_schema(public_schema["properties"]["data"])
        for parent_names, removed_names in (
            (("microbiomeAssessment", "microbiome_assessment"), ("panelCounts", "panel_counts")),
            (("appendices",), ("sourceItems", "source_items")),
        ):
            parent = next((data_schema["properties"][name] for name in parent_names
                if name in data_schema["properties"]), None)
            if parent is None:
                continue
            section = handler.resolve_ref_schema(parent)
            for name in removed_names:
                section.get("properties", {}).pop(name, None)
            if "required" in section:
                section["required"] = [name for name in section["required"] if name not in removed_names]
        return schema

    @model_serializer(mode="wrap")
    def public_document(self, handler):
        """Serialize the already composed template, without re-authoring rows.

        Grouping, disease selection and cell writing belong to the report
        planner and reviewer. Delivery is a type/transport boundary, so it
        cannot reconstruct clinical rows, change source grades, erase pending
        disease cells or overwrite published model prose.
        """
        document = handler(self)
        data = document.get('data', {})
        for key in ('assessmentPresentation', 'assessment_presentation'):
            if data.get(key) is None:
                data.pop(key, None)
        appendices = data.get('appendices', {})
        for key in ('sourceItems', 'source_items'):
            appendices.pop(key, None)
        microbiome = data.get('microbiomeAssessment', data.get('microbiome_assessment', {}))
        for key in ('panelCounts', 'panel_counts'):
            microbiome.pop(key, None)
        # Both confirmed collection fields expose the same reviewed rows.
        # Vendor panels are private input, not an additional patient diagnosis
        # list. Keep category consumers populated without reviving raw rows.
        disease = data.get('diseaseRiskAssessment', data.get('disease_risk_assessment', {}))
        ranking = disease.get('riskRanking', disease.get('risk_ranking', []))
        if any(str(row.get('code') or '').startswith('templateRisk:') for row in ranking):
            for category in disease.get('categories', []):
                prefix = 'templateRisk:' + str(category.get('code') or '') + ':'
                category['items'] = [dict(row) for row in ranking
                    if str(row.get('code') or '').startswith(prefix)]
        standards = 'riskIndexStandards' if 'riskIndexStandards' in disease else 'risk_index_standards'
        if standards in disease:
            disease[standards] = []
        if 'notice' in disease:
            disease['notice'] = ''
        nutrition = data.get('nutritionAssessment', data.get('nutrition_assessment', {}))
        for name in ('majorMetabolism', 'major_metabolism', 'aminoAcidsAndMetabolites',
                'amino_acids_and_metabolites', 'neurotransmitters'):
            for row in nutrition.get(name, []):
                row['explanation'] = None
        for name in ('detailedMetabolism', 'detailed_metabolism', 'vitaminsAndMinerals', 'vitamins_and_minerals'):
            for group in nutrition.get(name, []):
                for row in group.get('items', []):
                    row['explanation'] = None
        pathways = data.get('functionalPathways', data.get('functional_pathways', {}))
        for row in pathways.get('pathways', []):
            row['explanation'] = None
        return document


def _compact_disease_taxon_evidence(disease):
    """Publish one supplied organism name and state in disease evidence.

    Abundances and the second-language label remain in the working source
    inventory. Retain the confirmed optional field names with empty values
    here, so JSON consumers and the preview receive the same concise view.
    This formatting step changes neither a risk decision nor its approval.
    """
    rows = [*disease.get('riskRanking', disease.get('risk_ranking', [])),
        *(row for category in disease.get('categories', []) for row in category.get('items', []))]
    for row in rows:
        for taxon in row.get('relatedTaxa', row.get('related_taxa', [])):
            scientific_key = 'scientificName' if 'scientificName' in taxon else 'scientific_name'
            scientific = taxon.get(scientific_key)
            if not taxon.get('name') and scientific:
                taxon['name'] = scientific
            taxon[scientific_key] = None
            taxon['value'] = None
            taxon['unit'] = None


def _publish_native_association_values(data):
    """Display a panel's own result without assessing its clinical risk.

    A source model and an association panel remain separate observations.
    Serialization never borrows a model score or adds a missing attention
    grade, including when revalidating an already delivered callback.
    """
    disease = data.get('diseaseRiskAssessment', data.get('disease_risk_assessment', {}))
    rows = [item for category in disease.get('categories', []) for item in category.get('items', [])]
    rows.extend(disease.get('riskRanking', disease.get('risk_ranking', [])))
    medical = data.get('medicalAssessment', data.get('medical_assessment', {}))
    rows.extend(row for row in medical.get('findings', [])
        if row.get('certainty') == 'source_association')
    for row in rows:
        code = str(row.get('code') or '').removeprefix('associationFinding:')
        if not code.startswith('nativeAssociation:'):
            continue
        status = row.get('sourceStatus', row.get('source_status'))
        if row.get('value') is None and isinstance(status, str) and status.strip():
            row['value'] = status


def _coalesce_native_risk_rows(data, rows):
    """Publish one same-code/page/date panel, retaining its canonical meaning.

    This only reconciles two views of the same already published observation;
    it neither reviews text nor merges different reports or disease models.
    """
    camel = 'diseaseRiskAssessment' in data
    key = lambda name: _camel(name) if camel else name
    disease = data.get(key('disease_risk_assessment'), {})
    categories = [row for category in disease.get('categories', []) for row in category.get('items', [])]
    canonical = {}
    for row in categories:
        stamp = _association_source_identity(row, key, 'nativeAssociation:')
        if stamp is not None:
            canonical.setdefault(stamp, []).append(row)
    originals = data.get('appendices', {}).get(key('source_items'), [])
    source_descriptions = {str(row.get('description')).strip() for row in originals
        if row.get('kind') == 'context' and row.get('category') == 'healthAssociation' and row.get('description')}
    def markers(row):
        value = str(row.get('value') or '')
        match = re.search(r'[；;]\s*相关菌[：:]\s*(.+)', value)
        text = match[1] if match else str(row.get('explanation') or '').strip()
        if not match and text not in source_descriptions:
            return None
        return tuple(sorted(part for part in re.split(r'[,，、;；]', re.sub(r'\s+', '', text)) if part))

    result, positions, removed, measured_markers = [], {}, [], {}
    for row in rows:
        stamp = _association_source_identity(row, key, 'nativeAssociation:')
        if stamp is None:
            result.append(row)
            continue
        peers = canonical.get(stamp, [])
        candidate = dict(row)
        # Category prose is the canonical finalized view. A matching source
        # locator is a synchronization key, never independent prose approval.
        if len(peers) == 1:
            for field in ('explanation', key('evidence_refs'), 'availability'):
                candidate[field] = peers[0].get(field)
        if stamp not in positions:
            positions[stamp] = len(result)
            measured_markers[len(result)] = markers(row)
            result.append(candidate)
            continue
        index, previous = positions[stamp], result[positions[stamp]]
        levels = {level for level in (previous.get(key('assessment_level')),
            candidate.get(key('assessment_level'))) if level is not None}
        original_markers, next_markers = measured_markers.get(index), markers(row)
        if len(levels) > 1 or original_markers and next_markers and original_markers != next_markers:
            # Conflicting independent judgements are not duplicate views.
            result.append(candidate)
            continue
        preferred = candidate if candidate.get('explanation') else previous
        merged = dict(preferred)
        if len(peers) != 1 and str(merged.get('explanation') or '').strip() in source_descriptions:
            merged['explanation'], merged[key('evidence_refs')] = None, []
            merged['availability'] = 'partial'
        merged['sources'] = list(previous.get('sources') or [])
        merged['sources'].extend(ref for ref in candidate.get('sources') or [] if ref not in merged['sources'])
        taxa = key('related_taxa')
        merged[taxa] = list(previous.get(taxa) or [])
        merged[taxa].extend(taxon for taxon in candidate.get(taxa) or [] if taxon not in merged[taxa])
        result[index] = merged
        measured_markers[index] = original_markers or next_markers
        removed.append(row)
    return result, removed


def _native_association_result(row):
    """A positive native risk item is usable without a numeric model index."""
    from .evaluation_native_modules import native_association_positive
    if not native_association_positive(row):
        return False
    grade = row.get('assessmentLevel', row.get('assessment_level'))
    if grade in {'正常', '注意', '低风险', '中低风险', '中风险', '中高风险', '高风险', '极高风险'}:
        return True
    return row.get('value') in {'检出', '阳性', '异常', '检出相关菌群异常'}


def _compact_trigger_grade_columns(data):
    """Publish template grade labels while preserving the reviewed work DTO."""
    medical = data.get('medicalAssessment', data.get('medical_assessment', {}))
    rows = medical.get('triggerAnalysis', medical.get('trigger_analysis', []))
    for row in rows:
        for camel, snake, labels in (
            ('evidenceStrength', 'evidence_strength', ('不足', '强', '中', '弱', '—')),
            ('modifiability', 'modifiability', ('维持', '高', '中', '低', '—')),
        ):
            key = camel if camel in row else snake
            value = row.get(key)
            if (not isinstance(value, str) or not value.strip()
                    or value.strip() in {'未提供', '未评估', '未知', '不详'}):
                row[key] = None
                continue
            match = re.match(r'^(' + '|'.join(map(re.escape, labels)) + r')(?=$|[\s：:（(、，,。.;；])', value.strip())
            grade = match[1] if match else None
            if camel == 'modifiability':
                row[key] = grade if grade in {'高', '中', '低'} else '低'
            else:
                row[key] = grade if grade in {'强', '中', '弱'} else '弱'
    # Reorder only the publication copy. Live narrative targets keep their
    # original paths until all source and evidence reviews have completed.
    if _declares_integrated_health_scale(data):
        rows.sort(key=lambda row: {'强': 0, '中': 1, '弱': 2}.get(
            row.get('evidenceStrength', row.get('evidence_strength')), 3))


def _deduplicate_public_virulence_meaning(microbiome):
    """Keep an exact shared parent/child explanation once in public delivery.

    Only the serialized dictionary changes. A specific child's complete
    science and citations survive, as do every parent observation and source.
    """
    rows = microbiome.get("healthMetrics", microbiome.get("health_metrics", []))
    for parent in rows:
        text, details = parent.get("explanation"), parent.get("details")
        if (parent.get("code") != "virulenceGenes" or not isinstance(text, str)
                or not text.strip() or not isinstance(details, str)):
            continue
        names = {name.strip() for name in re.split(r"[,，、；;\r\n]+", details) if name.strip()}
        parent_refs = "evidenceRefs" if "evidenceRefs" in parent else "evidence_refs"
        if parent_refs not in parent:
            continue
        for child in rows:
            child_refs = "evidenceRefs" if "evidenceRefs" in child else "evidence_refs"
            if (child is not parent and child.get("code") != "virulenceGenes"
                    and child.get("name") in names and child.get("explanation") == text
                    and child_refs in child and child[child_refs] == parent[parent_refs]):
                parent["explanation"] = None
                parent[parent_refs] = []
                break


def _compact_template_health_rows(data, microbiome):
    """Remove the explicitly retired inventory and duplicate topic section.

    Canonical disease-category rows, clinical evidence, source inventories in
    the working DTO, and template overview health results are independent.
    """
    key = "healthMetrics" if "healthMetrics" in microbiome else "health_metrics"
    names = set(catalog.HEALTH_ASSOCIATION_CATEGORIES) | {"过敏症", "睡眠呼吸暂停综合征"}
    disease = data.get("diseaseRiskAssessment", data.get("disease_risk_assessment", {}))
    names.update(row.get("name") for category in disease.get("categories", [])
        for row in category.get("items", []) if row.get("name"))
    kept, removed = [], []
    for row in microbiome.get(key, []):
        retired = (str(row.get("code") or "").startswith(("panelSummary:", "panelResult:"))
            or row.get("name") in names or re.search(r"相关菌[：:]", str(row.get("value") or "")))
        (removed if retired else kept).append(row)
    if key in microbiome:
        microbiome[key] = kept
    return removed



def _only_negative_medication_observations(value):
    """A row of explicit non-use answers is not a medication-related cause.

    Every clause must say non-use. An unknown answer, side-effect statement,
    irregular adherence or any positive medicine history keeps the row.
    Original questionnaire/history fields are unaffected.
    """
    clauses = [part.strip() for part in re.split(r'[；;。\n]+', value) if part.strip()]
    for clause in clauses:
        label, colon, answer = clause.replace('：', ':').rpartition(':')
        intake = (colon and re.search(r'用药|服药|处方药|抗生素|补充剂|保健品', label)
            and not re.search(r'反应|过敏|不良|副作用|规律|依从', label))
        if intake and re.fullmatch(r'(?:否|无|没有|未使用|未服用|不使用|不服用|0(?:种|次|天)?)', answer.strip()):
            continue
        if re.fullmatch(r'(?:本人|用户)?(?:目前|当前|近期|现在|'
                r'(?:过去|最近|近)(?:\d+|[一二三四五六七八九十]+)(?:个)?(?:天|周|月|年)(?:内|期间)?)?'
                r'(?:无|没有|未使用|未服用|不使用|不服用)'
                r'(?:长期|常规|日常|任何|固定)?(?:处方药|药物|药品|抗生素|营养补充剂|补充剂|保健品|用药|服药)'
                r'(?:使用|服用|史|记录)?', clause):
            continue
        return False
    return bool(clauses)


def _omit_undetected_observations(data):
    """Remove empty display slots while retaining actual negative results.

    A reported negative is a result, including a control or a whole-panel
    statement. It is not an empty slot or a clinical disease exclusion. Keep
    its original value and scope through repeated callback serialization.
    """
    observation_arrays = {
        'overallStructure', 'overall_structure', 'healthMetrics', 'health_metrics',
        'shortChainFattyAcids', 'short_chain_fatty_acids', 'phylumComposition', 'phylum_composition',
        'antimicrobialResistance', 'antimicrobial_resistance', 'relatedTaxa', 'related_taxa',
        'relatedMetrics', 'related_metrics', 'majorMetabolism', 'major_metabolism',
        'aminoAcidsAndMetabolites', 'amino_acids_and_metabolites', 'neurotransmitters',
        'weakSynthesisChart', 'weak_synthesis_chart', 'riskRanking', 'risk_ranking', 'items',
    }
    removed = []
    health_scale = _declares_integrated_health_scale(data)
    def walk(node):
        if isinstance(node, dict):
            for key, values in list(node.items()):
                if key in observation_arrays and isinstance(values, list):
                    kept = []
                    for row in values:
                        if (key in {'riskRanking', 'risk_ranking'} and isinstance(row, dict)
                                and _pending_integrated_health_disease(row, health_scale)):
                            row['availability'] = 'partial'
                            kept.append(row)
                            continue
                        observation = isinstance(row, dict) and any(field in row for field in (
                            'value', 'score', 'sourceStatus', 'source_status', 'assessmentLevel', 'assessment_level'))
                        empty = observation and not _has_public_result(row) and row.get('score') is None
                        if observation and empty:
                            removed.append(row)
                        else:
                            kept.append(row)
                    node[key] = kept
                walk(node[key])
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(data)
    medical = data.get('medicalAssessment', data.get('medical_assessment', {}))
    trigger_key = 'triggerAnalysis' if 'triggerAnalysis' in medical else 'trigger_analysis'
    if trigger_key in medical:
        complete = []
        for row in medical[trigger_key]:
            association = row.get('association')
            source_bound = any(source.get('sourceCode', source.get('source_code')) in {'A', 'B', 'C'}
                and isinstance(source.get('recordId', source.get('record_id')), str)
                and source.get('recordId', source.get('record_id')).strip()
                for source in row.get('sources', []))
            # The actual answer is useful independently of an optional
            # scientific association. Preserve its existing field and source
            # scope without inventing a relationship on serialization.
            observations = row.get('observations')
            meaningful = lambda value: (isinstance(value, str) and value.strip()
                and value.strip() not in {'未提供', '未填写', '不详', '未知', '—', '-', '--'})
            if source_bound and (meaningful(observations) or meaningful(association)):
                complete.append(row)
            else:
                removed.append(row)
        medical[trigger_key] = complete
    return removed

def _table_paragraphs(value):
    """Normalize display labels without flattening authored paragraphs."""
    generated = {"text", "explanation", "association", "evidenceStrength", "evidence_strength",
        "modifiability", "healthMeaning", "health_meaning", "attentionBasis", "attention_basis"}
    if isinstance(value, dict):
        for key, item in value.items():
            if key in generated and isinstance(item, str):
                paragraph = re.sub(r"[ \t]*\r?\n[ \t]*", "\n", item).strip()
                # The score marker is private restoration metadata, not
                # patient-facing grading instructions. Retain its judgement.
                paragraph = paragraph.removeprefix('AI综合评估分（0–100，越高表示本次综合状态越好）：')
                value[key] = re.sub(r'^AI综合(?:评估|评级)[:：]\s*', '', paragraph)
            else:
                _table_paragraphs(item)
    elif isinstance(value, list):
        for item in value:
            _table_paragraphs(item)


def verified_native_association_notice(row, data):
    """Prove a fixed disease's qualitative notice against its native panel.

    The literal source panel must survive in the same document, have an exact
    disease-concept match, and carry the same provenance. A phrase containing
    '未检出' or a generic page citation alone cannot satisfy this proof.
    """
    from modules.report.evaluation_native_modules import native_disease_associations, native_association_result
    code = catalog.fixed_disease_code(row.get('code')) or catalog.fixed_disease_code(row.get('name'))
    if not code or not isinstance(row.get('explanation'), str):
        return False
    def identity(source):
        return (source.get('sourceCode', source.get('source_code')),
            source.get('recordId', source.get('record_id')),
            source.get('observedAt', source.get('observed_at')))
    actual_sources = {identity(source) for source in row.get('sources', [])}
    clauses = []
    for entry in native_disease_associations(data, code):
        native = entry['row']
        if not _native_association_result(native):
            continue
        source_ids = {identity(source) for source in native.get('sources', [])}
        if source_ids and source_ids <= actual_sources:
            clauses.append(str(native.get('name') or '') + '相关菌群筛查：' + str(native.get('sourceStatus') or native.get('value')))
            if meaning := native_association_result(native):
                clauses.append(str(native.get('name') or '') + '：' + meaning)
    supplied = [part.strip().rstrip('。') for part in row['explanation'].split('；') if part.strip()]
    return bool(supplied and clauses and all(part in clauses for part in supplied))


def _source_bound_qualitative_risk(row):
    """Retain the fixed risk builder's explicit source-bound A/B assessment."""
    return bool(row.get('value') is None
        and (catalog.fixed_disease_code(row.get('code')) or catalog.fixed_disease_code(row.get('name')))
        and isinstance(row.get('explanation'), str) and row['explanation'].startswith('风险线索：')
        and row['explanation'][len('风险线索：'):].strip()
        and any(source.get('sourceCode', source.get('source_code')) in {'A', 'B'}
            and source.get('recordId', source.get('record_id')) for source in row.get('sources', [])))


def _verified_disease_source_notice(row):
    """Keep the source card result; generic AI disease definitions stay retired."""
    text = row.get('explanation')
    if (row.get('relatedTaxa', row.get('related_taxa')) or not isinstance(text, str)
            or not re.fullmatch(r'本次检测未检出与该疾病相关的正、负相关菌异常[。.]?', text.strip())):
        return False
    ids = [source.get('recordId', source.get('record_id')) or ''
        for source in row.get('sources', [])
        if source.get('sourceCode', source.get('source_code')) == 'C']
    return (any(re.fullmatch(r'parsed\.evaluationPanelSummaries\[\d+\]\.sourceNotice', value) for value in ids)
        and any(re.fullmatch(r'source:[a-fA-F0-9]{64}:page:\d+', value) for value in ids))


def _retain_public_literature(data):
    """After removing retired sections, keep a consecutive used bibliography."""
    refs_key = "referenceLiterature" if "referenceLiterature" in data.get("appendices", {}) else "reference_literature"
    library = data.get("appendices", {}).get(refs_key, [])
    used = {}
    def collect(value, chapter):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"evidenceRefs", "evidence_refs"}:
                    for number in item:
                        used.setdefault(number, set()).add(chapter)
                else:
                    collect(item, chapter)
        elif isinstance(value, list):
            for item in value:
                collect(item, chapter)
    for chapter, value in data.items():
        collect(value, chapter)
    available = {row["number"] for row in library}
    if len(available) != len(library) or set(used) - available:
        raise ValueError("evaluate public bibliography must uniquely support every retained citation")
    retained = [row for row in library if row["number"] in used]
    mapping = {row["number"]: index for index, row in enumerate(retained, 1)}
    def remap(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"evidenceRefs", "evidence_refs"}:
                    value[key] = sorted({mapping[number] for number in item})
                else:
                    remap(item)
        elif isinstance(value, list):
            for item in value:
                remap(item)
    remap(data)
    for row in retained:
        old = row["number"]
        section_key = "supportingSections" if "supportingSections" in row else "supporting_sections"
        row[section_key] = sorted(used[old])
        row["number"] = mapping[old]
    data["appendices"][refs_key] = retained
