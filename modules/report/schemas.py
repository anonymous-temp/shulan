"""Request, internal fact, and callback schemas for the report API.

The public models deliberately separate source facts from narrative fields.  The
pipeline may enrich prose, but it cannot mutate source findings or citations.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_serializer, field_validator, model_validator

from modules.report.evaluation_schema import EvaluationData
from modules.report.evaluation_risk_assessment import RiskRegistry


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="forbid",
    )


class FlexibleInputModel(ApiModel):
    # Existing callers have added source metadata over time.  Accept it without
    # making any new key mandatory and preserve the documented request shape.
    model_config = ConfigDict(
        alias_generator=_to_camel,
        populate_by_name=True,
        extra="allow",
    )


class RemoteFileInput(FlexibleInputModel):
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    file_type: Literal["pdf", "image"] | None = None
    document_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_source(self) -> RemoteFileInput:
        if self.document_id is not None:
            self.document_id = self.document_id.strip()
            if not self.document_id or any(ord(c) < 32 for c in self.document_id):
                raise ValueError("documentId must be a nonempty identifier")
            if self.url is not None or self.file_type is not None:
                raise ValueError("documentId cannot be combined with url/fileType")
        elif self.url is None or self.file_type is None:
            raise ValueError("fileList requires documentId or url + fileType")
        return self

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme.casefold() != "https" or not parsed.hostname:
            raise ValueError("fileList URL must use HTTPS")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("fileList URL must not contain credentials or fragments")
        return normalized


class TestDataInput(FlexibleInputModel):
    result_detail_url: str = Field(min_length=1, max_length=4096)
    result_summary_url: str | None = Field(default=None, max_length=4096)

    @field_validator("result_detail_url", "result_summary_url")
    @classmethod
    def validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        parsed = urlsplit(normalized)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            raise ValueError("testData URL must use HTTP or HTTPS")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("testData URL must not contain credentials or fragments")
        return normalized


class HospitalDataInput(FlexibleInputModel):
    _evaluation_record_id: str | None = PrivateAttr(default=None)
    _evaluation_duplicate_record_ids: list[str] = PrivateAttr(default_factory=list)
    check_time: str | None = None
    data_type: Literal["EXAM", "INSPECTION"]
    item_name: str = Field(min_length=1, max_length=512)
    report_time: str | None = None
    exam_result: str | None = Field(default=None, max_length=20000)
    reference_range: str | None = Field(default=None, max_length=512)
    result_status: int | str | None = None
    result_status_name: str | None = Field(default=None, max_length=128)
    test_result_value: str | int | float | None = None


class MicrobiomeReportDataInput(FlexibleInputModel):
    """Optional dedicated evaluate source; a usable JSON precedes its PDF."""

    original_report_url: str | None = Field(default=None, max_length=4096)
    data_info_url: str | None = Field(default=None, max_length=4096)
    template_number: str | None = Field(default=None, max_length=128)
    fmt_typing_report_url: str | None = Field(default=None, max_length=4096)

    @field_validator("original_report_url", "data_info_url", "fmt_typing_report_url", mode="before")
    @classmethod
    def validate_url(cls, value):
        if value is None or isinstance(value, str) and not value.strip():
            return None
        return RemoteFileInput.validate_url(value)


class TriggerSurveyInput(FlexibleInputModel):
    """Keep each questionnaire and its unmodified answer mapping together."""

    survey_schema: dict[str, Any] = Field(alias="schema")
    answer: dict[str, Any] | None = Field(default_factory=dict)


class ReportRequest(FlexibleInputModel):
    user_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    group_id: str | None = Field(default=None, min_length=1, max_length=128)
    init_survey_schema: dict[str, Any] | None = None
    init_survey_answer: dict[str, Any] | None = None
    advanced_survey_schema: dict[str, Any] | None = None
    advanced_survey_answer: dict[str, Any] | None = None
    survey_list: list[TriggerSurveyInput] | None = None
    name: str | None = Field(default=None, max_length=128)
    age: int | str | None = None
    sex: str | None = Field(default=None, max_length=32)
    gender: str | None = Field(default=None, max_length=32)
    file_list: list[RemoteFileInput] = Field(default_factory=list, max_length=10)
    test_data: list[TestDataInput] = Field(default_factory=list, max_length=10)
    hospital_data: list[HospitalDataInput] = Field(
        default_factory=list, max_length=1000
    )
    microbiome_data: dict[str, Any] | None = None
    microbiome_report_data: MicrobiomeReportDataInput | None = None
    fmt_typing_report_url: str | None = Field(default=None, max_length=4096)
    package: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def normalize_trigger_input(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        from modules.report.evaluation_request_input import trigger_survey_subject, validate_trigger_surveys

        data = dict(value)
        gender = data.get("gender")
        sex = data.get("sex")
        if sex is None or isinstance(sex, str) and not sex.strip():
            if gender is not None:
                data["sex"] = gender
        # New callers identify an IM group, not an existing QA session. This
        # deterministic internal trace does not imply that the two IDs match.
        group = data.get("groupId", data.get("group_id"))
        session = data.get("sessionId", data.get("session_id"))
        if isinstance(group, str) and group.strip() and (session is None
                or isinstance(session, str) and not session.strip()):
            data["sessionId"] = "report-group-" + hashlib.sha256(group.strip().encode()).hexdigest()[:32]
        for camel, snake in (
            ("initSurveySchema", "init_survey_schema"),
            ("initSurveyAnswer", "init_survey_answer"),
            ("advancedSurveySchema", "advanced_survey_schema"),
            ("advancedSurveyAnswer", "advanced_survey_answer"),
        ):
            for key in (camel, snake):
                if isinstance(data.get(key), str) and not data[key].strip():
                    data[key] = None
        validate_trigger_surveys(data)
        for key, subject_value in trigger_survey_subject(data).items():
            current = data.get(key)
            if current is None or isinstance(current, str) and not current.strip():
                data[key] = subject_value
        return data

    @field_validator("user_id", "session_id")
    @classmethod
    def normalize_identifiers(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(ord(char) < 32 for char in normalized):
            raise ValueError("identifier contains control characters")
        return normalized

    @field_validator("group_id")
    @classmethod
    def normalize_group_id(cls, value: str | None) -> str | None:
        return None if value is None else cls.normalize_identifiers(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @field_validator("age", mode="before")
    @classmethod
    def normalize_age(cls, value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            raise ValueError("age must be an integer")  # noqa: TRY004 - Pydantic validator.
        try:
            age = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("age must be an integer") from exc
        if not 0 <= age <= 150:
            raise ValueError("age must be between 0 and 150")
        return age

    @field_validator("fmt_typing_report_url", mode="before")
    @classmethod
    def validate_fmt_typing_report_url(cls, value):
        return MicrobiomeReportDataInput.validate_url(value)

    @model_validator(mode="after")
    def validate_gender_compatibility(self) -> ReportRequest:
        if self.sex is not None and self.gender is not None and self.sex != self.gender:
            raise ValueError("gender conflicts with sex")
        return self

    @field_validator("sex", "gender")
    @classmethod
    def normalize_sex(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        mapping = {
            "男": "男",
            "male": "男",
            "m": "男",
            "女": "女",
            "female": "女",
            "f": "女",
            "未知": "未知",
            "unknown": "未知",
            "u": "未知",
        }
        normalized = mapping.get(value.strip().casefold())
        if not normalized:
            raise ValueError("sex must be 男, 女, or 未知")
        return normalized


class SubjectInfo(ApiModel):
    name: str | None = None
    age: int | None = None
    sex: str | None = None


class PatientEvidenceContext(ApiModel):
    """De-identified facts used only to rank evidence applicability.

    The context is assembled from the existing request, parsed source,
    hospital data and optional session.  Missing values stay missing and never
    become assumptions or patient-facing findings.
    """

    age: int | None = None
    age_band: Literal[
        "pediatric",
        "adult_18_44",
        "adult_45_64",
        "older_adult",
        "unknown",
    ] = "unknown"
    sex: Literal["男", "女", "未知"] | None = None
    height_cm: float | None = None
    weight_kg: float | None = None
    bmi: float | None = None
    bmi_group: Literal["underweight", "normal", "overweight", "obesity"] | None = None
    waist_cm: float | None = None
    symptoms: list[str] = Field(default_factory=list)
    bowel_context: list[str] = Field(default_factory=list)
    medications: list[str] = Field(default_factory=list)
    dietary_context: list[str] = Field(default_factory=list)
    confirmed_conditions: list[str] = Field(default_factory=list)
    sample_type: str | None = None
    sample_date: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    # 长期画像分区。三者互不混淆：背景是本人已确认事实，家族史只来自
    # 血亲且不能当作本人疾病，未确认线索不作为确定事实。
    profile_background: list[str] = Field(default_factory=list)
    profile_family_history: list[str] = Field(default_factory=list)
    profile_unconfirmed: list[str] = Field(default_factory=list)
    profile_read_at: str | None = None
    profile_version: int | None = None
    # 有预算的快照不能被当作完整病史，截断必须如实标注。
    profile_truncated: bool = False
    profile_stale: bool = False


class SampleInfo(ApiModel):
    sample_id: str | None = None
    sample_type: str | None = None
    test_item: str | None = None
    sample_date: str | None = None
    report_date: str | None = None
    sequencing_platform: str | None = None
    method: str | None = None
    effective_sequences: int | None = None
    quality_control: str | None = None


class SourceMetric(ApiModel):
    code: str
    name: str
    value: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    status: str | None = None


TaxonCategory = Literal[
    "beneficial",
    "pathogenic",
    "opportunistic",
    "fungi_pathogenic",
    "other",
]


class ParsedTaxon(ApiModel):
    taxon_id: str
    category: TaxonCategory
    latin_name: str
    chinese_name: str | None = None
    value: str | None = None
    unit: str | None = "%"
    reference_range: str | None = None
    source_status: str | None = None
    explicitly_abnormal: bool = False
    detection_status: Literal["detected", "trace", "not_detected", "unknown"] = (
        "unknown"
    )


class ParsedDisease(ApiModel):
    disease_id: str
    name: str
    source_score: str | None = None
    source_level: str | None = None
    source_description: str | None = None
    related_taxa: list[str] = Field(default_factory=list)


class PanelCountAudit(ApiModel):
    category: TaxonCategory
    panel_name: str
    row_count: int
    detected_count: int
    trace_count: int
    source_summary_count: int | None = None
    matches_source_summary: bool | None = None


class SourceSnapshotAudit(ApiModel):
    """Internal provenance for every independently parsed report source."""

    source_type: Literal["microbiome_data", "test_data", "pdf", "image"]
    source_name: str
    source_checksum: str | None = None
    sample: SampleInfo = Field(default_factory=SampleInfo)
    # Kept internal so historical same-user metrics can inform score trends
    # without leaking old report rows into the patient-facing current report.
    metrics: list[SourceMetric] = Field(default_factory=list, exclude=True)
    taxon_count: int = 0
    disease_count: int = 0
    selected: bool = False
    date_unresolved: bool = False


class ParsedMicrobiomeReport(ApiModel):
    _evaluation_source_audit: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _evaluation_clinical_history: list[HospitalDataInput] = PrivateAttr(default_factory=list)
    source_type: Literal["microbiome_data", "test_data", "pdf", "image", "pending"]
    source_name: str
    source_checksum: str | None = None
    subject: SubjectInfo
    sample: SampleInfo = Field(default_factory=SampleInfo)
    metrics: list[SourceMetric] = Field(default_factory=list)
    taxa: list[ParsedTaxon] = Field(default_factory=list)
    disease_results: list[ParsedDisease] = Field(default_factory=list)
    panel_counts: list[PanelCountAudit] = Field(default_factory=list)
    # Source heading ratios have no confirmed detected/tested-count definition.
    # Only the evaluate projection consumes these; legacy reports and model
    # inputs must keep their existing serialized source facts unchanged.
    evaluation_panel_summaries: list[SourceMetric] = Field(
        default_factory=list, exclude=True,
    )
    # Original AMR table class cells are evaluate-only metadata. They never
    # modify the legacy scalar/clinical lanes or enter serialized model input.
    evaluation_amr_classes: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    # Per-occurrence provenance stays internal; identical taxa can carry
    # different printed statuses in distinct source panels.
    source_taxon_occurrences: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    enterotype: str | None = None
    source_summary: str | None = None
    # Historical reports are used only for source reconciliation and retrieval
    # reuse.  They must never leak into the patient-facing callback or model
    # narrative as current findings.
    source_history: list[SourceSnapshotAudit] = Field(
        default_factory=list,
        exclude=True,
    )
    source_errors: list[str] = Field(default_factory=list, exclude=True)
    source_documents: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    # Matching/plan text is background only, never a patient measurement.
    fmt_typing_context: list[dict[str, Any]] = Field(default_factory=list, exclude=True)
    # Terminology only from all actual microbiome sources. This prevents
    # duplicated generic table rows being reclassified as clinical findings;
    # it does not merge historical abundance values into the current report.
    source_microbiome_terms: list[str] = Field(default_factory=list, exclude=True)
    # General check-up PDFs/images supplied alongside (or before) the
    # microbiome result are normalized into the existing hospital-data shape.
    # They are internal pipeline facts and never add fields to the callback.
    supplementary_hospital_data: list[HospitalDataInput] = Field(
        default_factory=list,
        exclude=True,
    )

    @model_validator(mode="after")
    def require_microbiome_facts(self) -> ParsedMicrobiomeReport:
        if self.source_type == "pending":
            return self
        if not self.metrics and not self.taxa and not self.disease_results:
            # Dedicated evaluate JSON may legitimately contain only an AMR
            # or virulence result. Accept its source-bound cells without
            # inventing a generic measurement or admitting arbitrary history.
            def present(value):
                return value is not None and not isinstance(value, (bool, dict, list)) and (
                    not isinstance(value, str) or bool(value.strip()))

            dedicated = (self.source_type == "microbiome_data"
                and self.source_name == "microbiomeReportData.json" and bool(self.source_checksum))
            original_summary = dedicated and any(present(row.value)
                and getattr(row, "source_file", None) == self.source_name
                and getattr(row, "source_checksum", None) == self.source_checksum
                and bool(getattr(row, "source_path", None))
                for row in self.evaluation_panel_summaries)
            original_class = dedicated and any(present(row.get("value")) and row.get("itemName")
                and row.get("sourceFile") == self.source_name
                and row.get("sourceChecksum") == self.source_checksum
                for row in self.evaluation_amr_classes)
            original_gene = dedicated and any(
                row.model_extra.get("sourceReportType") == "microbiome"
                and row.model_extra.get("sourceCategory") in {"virulence", "antimicrobialResistance"}
                and row.model_extra.get("sourceFile") == self.source_name
                and row.model_extra.get("sourceChecksum") == self.source_checksum
                and row.model_extra.get("sourcePath")
                and (present(row.exam_result) or present(row.test_result_value))
                for row in self.supplementary_hospital_data)
            if not (original_summary or original_class or original_gene):
                raise ValueError("source does not contain usable microbiome findings")
        return self


class SourceFinding(ApiModel):
    status: str
    value: str | None = None
    unit: str | None = None
    reference_range: str | None = None


class RiskAssociation(ApiModel):
    name: str
    description: str
    relation_type: Literal[
        "established_pathogen",
        "opportunistic",
        "epidemiologic_association",
        "colonization",
        "environmental_or_dietary",
    ] = "epidemiologic_association"
    evidence_refs: list[int] = Field(default_factory=list)
    priority_score: float = Field(default=0.0, exclude=True)
    patient_applicability: Literal[
        "direct", "contextual", "not_applicable", "unclear"
    ] = "unclear"
    sample_alignment: Literal[
        "stool", "mucosa", "blood", "respiratory", "other", "unclear"
    ] = "unclear"


class AbnormalMicrobeItem(ApiModel):
    microbe_id: str
    name: str
    scientific_name: str
    category: Literal[
        "beneficial_abnormal",
        "pathogenic_signal",
        "opportunistic_signal",
        "fungi_pathogenic_signal",
        "other_abnormal",
    ]
    source_finding: SourceFinding
    clinical_role: str | None = None
    conclusion: str | None = None
    patient_interpretation: str | None = None
    clinical_confirmation: str | None = None
    medical_interpretation: str | None = None
    # Kept internal: these claims are merged into ``medical_interpretation``
    # and its ordinary citation list before the fixed callback is built.
    # They must never become disease rows merely because a function or barrier
    # query was successful.
    functional_interpretation: str | None = Field(default=None, exclude=True)
    functional_evidence_refs: list[int] = Field(default_factory=list, exclude=True)
    disease_and_risks: list[RiskAssociation] = Field(default_factory=list)
    interpretation_evidence_refs: list[int] = Field(default_factory=list)
    association_evidence_refs: list[int] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    # Internal audit marker.  It distinguishes a validated model-authored
    # interpretation from the safe source-fact fallback without changing the
    # external callback contract.
    # Included only in the internal report passed to the fixed callback mapper.
    # FixedReportCallback has no such field, so it never reaches the caller.
    model_interpretation_generated: bool = False
    # Internal workload marker. Only rows that can pass the patient-facing
    # evidence gate are required to receive a model-authored interpretation.
    # Keeping this separate from ``model_interpretation_generated`` prevents
    # non-public low-abundance pathogen rows from making the whole narrative
    # stage look incomplete.
    model_interpretation_required: bool = Field(default=False, exclude=True)


class MicrobePanelFinding(ApiModel):
    category: TaxonCategory
    name: str
    value: str | None = None
    unit: str | None = None
    status: str | None = None


class AbnormalMicrobesSection(ApiModel):
    items: list[AbnormalMicrobeItem] = Field(default_factory=list)
    panel_findings: list[MicrobePanelFinding] = Field(default_factory=list)
    summary: str | None = None


class ClinicalResult(ApiModel):
    item_name: str
    result: str
    result_time: str | None = None
    status: str | None = None


class SuggestedEvaluation(ApiModel):
    name: str
    rationale: str
    purpose: Literal["筛查", "确认", "评估"] = "确认"
    already_available: bool = False
    evidence_refs: list[int] = Field(default_factory=list)
    # Internal provenance; the fixed callback does not expose this field.
    recommendation_source: Literal["evidence", "model"] = "evidence"


class DiseaseRiskItem(ApiModel):
    disease_id: str
    name: str
    display_name: str | None = None
    # ``clinical_context`` is an internal-only row used to retrieve a
    # source-bound follow-up examination for the whole-report health summary.
    # It must never become a public microbiome disease-risk row.
    source_type: Literal[
        "source_report", "evimed_association", "clinical_context"
    ]
    item_type: Literal["disease", "symptom", "biomarker", "phenotype", "unclear"] = (
        "disease"
    )
    source_result: str | None = None
    model_index: str | None = None
    source_description: str | None = Field(default=None, exclude=True)
    related_content: str | None = None
    medical_interpretation: str | None = None
    # Internal narrative field consumed by the fixed callback mapper.  The
    # public contract continues to expose it through tips.special_reminder.
    patient_suggestion: str | None = None
    related_microbes: list[str] = Field(default_factory=list)
    association_evidence_refs: list[int] = Field(default_factory=list)
    confirmation_evidence_refs: list[int] = Field(default_factory=list)
    existing_results: list[ClinicalResult] = Field(default_factory=list)
    suggested_evaluations: list[SuggestedEvaluation] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)


class DiseaseRisksSection(ApiModel):
    items: list[DiseaseRiskItem] = Field(default_factory=list)


class EnterotypeResult(ApiModel):
    name: str
    source: Literal["source_report", "derived"]
    basis: str | None = None


class HealthLevel(ApiModel):
    level: Literal["较低关注", "中度关注", "重点关注"]
    summary: str
    basis: list[str] = Field(default_factory=list)


class HealthRecommendation(ApiModel):
    category: Literal["diet", "exercise", "sleep", "bowel_habits", "general"]
    topic: str | None = None
    priority: Literal["重点关注", "建议关注"] | None = None
    content: str
    evidence_refs: list[int] = Field(default_factory=list)


class HealthRecommendedExamination(ApiModel):
    """One evidence-backed examination selected for the report as a whole."""

    name: str
    purpose: Literal["筛查", "确认", "评估"]
    reason: str
    evidence_refs: list[int] = Field(default_factory=list)


class HealthManagementSection(ApiModel):
    enterotype: EnterotypeResult | None = None
    health_level: HealthLevel | None = None
    health_profile: str | None = None
    recommendations: list[HealthRecommendation] = Field(default_factory=list)
    recommended_examinations: list[HealthRecommendedExamination] = Field(
        default_factory=list
    )


AssessmentLevel = Literal[
    "good",
    "attention",
    "priority_attention",
    "insufficient_data",
    "rule_not_applicable",
]


class AssessmentEvidenceUsed(ApiModel):
    indicator_code: str
    item: str
    value: str | None = None
    unit: str | None = None
    date: str | None = None
    source: str
    source_type: Literal[
        "hospital_data",
        "microbiome_data",
        "pdf_or_image",
        "session_context",
        "derived",
    ]
    status: str | None = None
    reference_range: str | None = None


class AssessmentRuleBasis(ApiModel):
    rule_id: str
    indicator: str
    source: str
    version: str
    boundary: str


class AssessmentScoreCap(ApiModel):
    applied: bool = False
    maximum: int = Field(default=100, ge=0, le=100)
    reason: str | None = None


class HealthAssessmentRelatedDisease(ApiModel):
    name: str
    risk_level: Literal["high", "medium", "low", "pending"]
    risk_label: Literal["高风险", "中风险", "低风险", "风险待评估"]
    source: Literal["临床资料", "菌群报告"]
    primary: bool = True


class HealthAssessmentDimension(ApiModel):
    code: Literal[
        # Legacy component codes remain valid for internal deterministic
        # builders. HealthAssessment itself accepts only the six active
        # system codes below.
        "gut_microecology",
        "gut_barrier",
        "glucose_lipid_metabolism",
        "cardiovascular_risk",
        "weight_body_composition",
        "kidney_uric_acid",
        "mental_system",
        "immune_system",
        "tumor_risk",
        "urinary_system",
        "nervous_system",
        "cardiovascular_system",
        "digestive_system",
        "metabolic_system",
    ]
    name: str
    weight: float = Field(gt=0, le=1)
    score: int = Field(default=0, ge=0, le=100)
    score_available: bool = False
    display_score: str = "--"
    level: AssessmentLevel = "insufficient_data"
    level_label: str = "暂不评分"
    confidence: Literal["high", "moderate", "low", "unavailable"] = "unavailable"
    evidence_used: list[AssessmentEvidenceUsed] = Field(default_factory=list)
    rule_basis: list[AssessmentRuleBasis] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    trend_adjustment: int = Field(default=0, ge=-10, le=0)
    score_cap: AssessmentScoreCap = Field(default_factory=AssessmentScoreCap)
    related_diseases: list[HealthAssessmentRelatedDisease] = Field(
        default_factory=list
    )
    explanation: str


class HealthAssessmentAbnormalIndicator(ApiModel):
    """One source-backed abnormal input shown immediately after the score."""

    dimension_code: Literal[
        "mental_system",
        "immune_system",
        "tumor_risk",
        "urinary_system",
        "nervous_system",
        "cardiovascular_system",
        "digestive_system",
        "metabolic_system",
    ]
    dimension_name: str
    indicator_code: str
    item: str
    value: str | None = None
    unit: str | None = None
    reference_range: str | None = None
    status: str | None = None
    severity: Literal["attention", "priority_attention"]
    date: str | None = None
    clinical_significance: str | None = Field(default=None, max_length=800)


_ASSESSMENT_DIMENSION_DEFINITIONS = (
    ("immune_system", "免疫系统", 1 / 6),
    ("urinary_system", "泌尿系统", 1 / 6),
    ("nervous_system", "神经系统", 1 / 6),
    ("cardiovascular_system", "心脑血管系统", 1 / 6),
    ("digestive_system", "消化系统", 1 / 6),
    ("metabolic_system", "代谢系统", 1 / 6),
)

_ASSESSMENT_DEFAULT_CODES = (
    "cardiovascular_system",
    "metabolic_system",
    "digestive_system",
    "immune_system",
    "urinary_system",
    "nervous_system",
)


def empty_health_assessment_dimensions() -> list[HealthAssessmentDimension]:
    definitions = {
        code: (name, weight)
        for code, name, weight in _ASSESSMENT_DIMENSION_DEFINITIONS
    }
    return [
        HealthAssessmentDimension(
            code=code,
            name=definitions[code][0],
            weight=definitions[code][1],
            missing_evidence=["当前评分所需的有效数据"],
            explanation=f"当前资料不足，暂不能评估{definitions[code][0]}。",
        )
        for code in _ASSESSMENT_DEFAULT_CODES
    ]


class HealthAssessment(ApiModel):
    name: str = "健康管理评估指数"
    rule_version: str = "2.1.0"
    overall_score: int = Field(default=0, ge=0, le=100)
    overall_score_available: bool = False
    display_overall_score: str = "--"
    level: AssessmentLevel = "insufficient_data"
    level_label: str = "暂不评分"
    summary: str = "部分维度证据不足，暂不能计算综合评分。"
    dimensions: list[HealthAssessmentDimension] = Field(
        default_factory=empty_health_assessment_dimensions,
        min_length=6,
        max_length=6,
    )
    abnormal_indicators: list[HealthAssessmentAbnormalIndicator] = Field(
        default_factory=list
    )

    @field_serializer("dimensions", mode="wrap")
    def serialize_fixed_dimension_order(self, dimensions, handler):
        # Presentation-only ordering; do not alter internal relevance order
        # used for scoring evidence ownership or abnormal-indicator priority.
        ranks = {code: index for index, code in enumerate(_ASSESSMENT_DEFAULT_CODES)}
        return handler(sorted(dimensions, key=lambda item: ranks.get(item.code, len(ranks))))

    @model_validator(mode="after")
    def validate_fixed_assessment_contract(self) -> HealthAssessment:
        definitions = {
            code: (name, weight)
            for code, name, weight in _ASSESSMENT_DIMENSION_DEFINITIONS
        }
        codes = [item.code for item in self.dimensions]
        if len(codes) != len(set(codes)):
            raise ValueError("healthAssessment dimensions must be unique")
        if any(
            item.code not in definitions
            or item.name != definitions[item.code][0]
            or not math.isclose(item.weight, definitions[item.code][1])
            for item in self.dimensions
        ):
            raise ValueError(
                "healthAssessment dimensions must use fixed names and weights"
            )

        for item in self.dimensions:
            if item.score_available:
                if item.score == 0:
                    raise ValueError("available dimension score must be positive")
                if item.display_score != str(item.score):
                    raise ValueError("scoreAvailable dimension must display its score")
                expected_level = (
                    "good"
                    if item.score >= 75
                    else "attention"
                    if item.score >= 55
                    else "priority_attention"
                )
                if item.level != expected_level:
                    raise ValueError("dimension level does not match score")
                if item.missing_evidence:
                    raise ValueError(
                        "scoreAvailable dimension cannot have missingEvidence"
                    )
            elif item.score != 0 or item.display_score != "--":
                raise ValueError(
                    "unavailable dimension must use score=0 and displayScore=--"
                )
            elif not item.missing_evidence:
                raise ValueError("unavailable dimension must describe missingEvidence")

        all_available = all(item.score_available for item in self.dimensions)
        if all_available:
            expected_score = round(
                sum(item.score * item.weight for item in self.dimensions)
            )
            if not self.overall_score_available:
                raise ValueError(
                    "complete dimensions require an available overall score"
                )
            if self.overall_score != expected_score:
                raise ValueError(
                    "overallScore does not match weighted dimension scores"
                )
            if self.display_overall_score != str(expected_score):
                raise ValueError("displayOverallScore does not match overallScore")
            expected_level = (
                "good"
                if expected_score >= 75
                else "attention"
                if expected_score >= 55
                else "priority_attention"
            )
            if self.level != expected_level:
                raise ValueError("overall level does not match overallScore")
        elif (
            self.overall_score_available
            or self.overall_score != 0
            or self.display_overall_score != "--"
        ):
            raise ValueError(
                "incomplete dimensions require an unavailable overall score"
            )
        return self


EvidenceCategory = Literal["guideline", "literature", "drug_label", "clinical_trial"]
EvidenceIntent = Literal[
    "microbe_discovery",
    "microbe_clinical_significance",
    "microbe_association",
    "microbe_disease_association",
    "disease_clinical_significance",
    "diagnostic_guideline",
    "gut_function",
    "gut_barrier",
    "intervention",
    "health_management",
]

EvidenceRelationType = Literal[
    "established_pathogen",
    "opportunistic",
    "epidemiologic_association",
    "colonization",
    "environmental_or_dietary",
    "not_applicable",
]


class EvidenceCondition(ApiModel):
    name: str
    search_term: str
    source_phrase: str
    relation_type: EvidenceRelationType


class EvidenceRelationBinding(ApiModel):
    relation_id: str
    microbe_id: str
    microbe_name: str
    microbe_aliases: list[str] = Field(default_factory=list)
    disease_id: str
    disease_name: str
    relation_type: EvidenceRelationType = "epidemiologic_association"
    claim: str | None = None
    target_alignment: Literal["direct", "indirect", "not_applicable", "unclear"] = (
        "unclear"
    )
    taxon_alignment: Literal["species", "genus", "strain", "toxin_gene", "unclear"] = (
        "unclear"
    )
    sample_alignment: Literal[
        "stool", "mucosa", "blood", "respiratory", "other", "unclear"
    ] = "unclear"
    patient_applicability: Literal[
        "direct", "contextual", "not_applicable", "unclear"
    ] = "unclear"


class EvidenceInterpretation(ApiModel):
    claim: str
    population: str | None = None
    study_design: str | None = None
    direction: Literal[
        "increased", "decreased", "associated", "mixed", "not_stated"
    ] = "not_stated"
    effect: str | None = None
    applicability: str | None = None
    limitations: str | None = None
    confidence: Literal["high", "moderate", "low", "unclear"] = "unclear"
    target_alignment: Literal["direct", "indirect", "not_applicable", "unclear"] = (
        "unclear"
    )
    recommended_evaluations: list[EvidenceRecommendedEvaluation] = Field(
        default_factory=list
    )
    clinical_role: str | None = None
    conditions: list[EvidenceCondition] = Field(default_factory=list)
    taxon_alignment: Literal["species", "genus", "strain", "toxin_gene", "unclear"] = (
        "unclear"
    )
    sample_alignment: Literal[
        "stool", "mucosa", "blood", "respiratory", "other", "unclear"
    ] = "unclear"
    patient_applicability: Literal[
        "direct", "contextual", "not_applicable", "unclear"
    ] = "unclear"


class EvidenceRecommendedEvaluation(ApiModel):
    name: str
    rationale: str
    source_phrase: str
    purpose: Literal["confirm_target", "screen_target", "characterize_target"]


class EvidenceRecord(ApiModel):
    evidence_key: str
    source_evidence_key: str | None = None
    provider: str | None = None
    source_id: str | None = None
    parent_guide_id: str | None = None
    category: EvidenceCategory
    title: str
    source: str
    year: int | None = None
    summary: str | None = None
    abstract: str | None = None
    ai_summary: str | None = None
    evidence_text: str | None = None
    authors: list[str] = Field(default_factory=list)
    study_types: list[str] = Field(default_factory=list)
    impact_factor: float | None = None
    core_journals: list[str] = Field(default_factory=list)
    language: str | None = None
    url: str | None = None
    doi: str | None = None
    relevance: float = 0.0
    supports: list[str] = Field(default_factory=list)
    query_ids: list[str] = Field(default_factory=list)
    microbe_ids: list[str] = Field(default_factory=list)
    disease_ids: list[str] = Field(default_factory=list)
    relation_bindings: list[EvidenceRelationBinding] = Field(default_factory=list)
    intents: list[EvidenceIntent] = Field(default_factory=list)
    interpretation: EvidenceInterpretation | None = None
    targets: list[str] = Field(default_factory=list)


class EvidenceReference(ApiModel):
    number: int
    provider: str | None = None
    category: EvidenceCategory
    title: str
    source: str
    year: int | None = None
    summary: str | None = None
    authors: list[str] = Field(default_factory=list)
    study_types: list[str] = Field(default_factory=list)
    url: str | None = None
    doi: str | None = None
    intents: list[EvidenceIntent] = Field(default_factory=list)
    interpretation: EvidenceInterpretation | None = None
    targets: list[str] = Field(default_factory=list)


class EvidenceChannelAudit(ApiModel):
    channel: Literal["guide", "literature"]
    status: Literal["success", "failed", "not_requested"]
    provider: str | None = None
    elapsed_ms: float = 0.0
    raw_count: int = 0


class EvidenceQueryAudit(ApiModel):
    query_id: str
    query_text: str
    query_language: Literal["latin", "chinese", "english", "mixed", "unknown"] = (
        "unknown"
    )
    intent: EvidenceIntent
    target: str
    microbe_id: str | None = None
    disease_id: str | None = None
    relation_id: str | None = None
    channels: list[EvidenceChannelAudit] = Field(default_factory=list)
    raw_count: int = 0
    screened_count: int = 0
    retained_count: int = 0
    interpreted_count: int = 0
    public_count: int = 0
    rejected_reasons: dict[str, int] = Field(default_factory=dict)


class RelationCoverageAudit(ApiModel):
    disease_id: str
    disease_name: str
    source_taxon: str
    matched_microbe_ids: list[str] = Field(default_factory=list)
    query_id: str


class WithheldDiseaseAudit(ApiModel):
    disease_id: str
    name: str
    reason: str


class ReportPerformanceAudit(ApiModel):
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)
    provider_query_counts: dict[str, int] = Field(default_factory=dict)
    provider_elapsed_ms: dict[str, float] = Field(default_factory=dict)
    runtime_counters: dict[str, int] = Field(default_factory=dict)
    runtime_elapsed_ms: dict[str, float] = Field(default_factory=dict)
    raw_hit_count: int = 0
    screened_hit_count: int = 0
    interpreted_hit_count: int = 0
    public_evidence_count: int = 0
    source_snapshot_count: int = 0
    source_error_count: int = 0


class EvidenceCoverageIssue(ApiModel):
    target_type: Literal["source", "microbe", "disease", "diagnostic"]
    target_id: str
    reason: str


class EvidenceStageAudit(ApiModel):
    """Target-level degradation trail kept in the internal report audit."""

    target_type: Literal[
        "report",
        "microbe",
        "disease",
        "diagnostic",
        "function",
        "barrier",
        "intervention",
        "health_management",
    ]
    target_id: str
    target: str
    stage: str
    result: Literal[
        "success",
        "partial_success",
        "degraded_success",
        "insufficient_data",
    ]
    attempts: list[str] = Field(default_factory=list)
    raw_result_count: int = 0
    retained_result_count: int = 0
    interpreted_result_count: int = 0
    public_result_count: int = 0
    reasons: list[str] = Field(default_factory=list)


class ReportAudit(ApiModel):
    source_count_reconciliation: list[PanelCountAudit] = Field(default_factory=list)
    evidence_queries: list[EvidenceQueryAudit] = Field(default_factory=list)
    relation_coverage: list[RelationCoverageAudit] = Field(default_factory=list)
    withheld_disease_items: list[WithheldDiseaseAudit] = Field(default_factory=list)
    coverage_issues: list[EvidenceCoverageIssue] = Field(default_factory=list)
    stage_outcomes: list[EvidenceStageAudit] = Field(default_factory=list)
    performance: ReportPerformanceAudit = Field(default_factory=ReportPerformanceAudit)


class ReportSourceFact(ApiModel):
    """An immutable source observation, independent of scoring/disease dictionaries."""

    model_config = ConfigDict(frozen=True)
    fact_id: str
    kind: Literal["clinical", "self_reported", "microbe", "function", "context"]
    name: str
    value: str
    unit: str | None = None
    reference_range: str | None = None
    status: str | None = None
    date: str | None = None
    source: str
    abnormal: bool = False
    imaging: bool = False
    reported_assertion_kind: Literal["symptom", "history"] | None = None


class ClinicalProblemSource(ApiModel):
    fact_id: str
    quote: str


class ClinicalReportProblem(ApiModel):
    problem_id: str
    title: str
    certainty: Literal["source_finding", "suspected", "confirmed", "self_reported"] = "source_finding"
    priority: Literal["priority", "review", "routine"] = "review"
    sources: list[ClinicalProblemSource] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)


class ClinicalReportBlock(ApiModel):
    """Internal typed section; never an unversioned public callback extension."""

    code: str
    text: str = Field(max_length=4000)
    state: Literal["ready", "insufficient", "not_applicable", "unavailable"] = "ready"
    fact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    # Verbatim excerpts from supplied, applicable evidence. These are internal
    # validation anchors, not another public bibliography.
    evidence_quotes: dict[str, str] = Field(default_factory=dict)
    evidence_claims: dict[str, str] = Field(default_factory=dict)
    # Program-bound, per-reference facts; never accept a paragraph-wide union
    # as support for every cited paper.
    evidence_fact_ids: dict[str, list[str]] = Field(default_factory=dict)


class ClinicalProblemAnalysis(ApiModel):
    problem_id: str
    causes: ClinicalReportBlock | None = None
    mechanism: ClinicalReportBlock | None = None
    outlook: ClinicalReportBlock | None = None
    action: ClinicalReportBlock | None = None
    goal: ClinicalReportBlock | None = None
    significance: ClinicalReportBlock | None = None


class ClinicalReportDocument(ApiModel):
    """Clinical-first report work product; public serialization remains fixed."""

    version: Literal["clinical-v1"] = "clinical-v1"
    facts: list[ReportSourceFact] = Field(default_factory=list)
    risk_registry: RiskRegistry = Field(default_factory=RiskRegistry)
    _evaluation_evidence_plan: Any = PrivateAttr(default=None)
    _dimension_score_receipt: Any = PrivateAttr(default=None)
    # Internal per-run background snapshot, not new source measurements or
    # a public callback extension.
    background: dict[str, Any] = Field(default_factory=dict)
    # Accepted evidence key -> problem ID -> quoted analysis roles. This is
    # only a retrieval coverage checkpoint, never a public medical assertion.
    evidence_analysis_roles: dict[str, dict[str, list[str]]] = Field(default_factory=dict, exclude=True)
    problems: list[ClinicalReportProblem] = Field(default_factory=list)
    analyses: list[ClinicalProblemAnalysis] = Field(default_factory=list)
    sections: list[ClinicalReportBlock] = Field(default_factory=list)
    incomplete: list[str] = Field(default_factory=list)


class ReportContent(ApiModel):
    title: str = "肠道菌群医学辅助解读报告"
    subject: SubjectInfo
    sample: SampleInfo
    health_assessment: HealthAssessment = Field(default_factory=HealthAssessment)
    executive_summary: str | None = None
    abnormal_microbes: AbnormalMicrobesSection
    disease_risks: DiseaseRisksSection
    health_management: HealthManagementSection
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    clinical_document: ClinicalReportDocument | None = None
    evaluation_data: EvaluationData | None = None


class SuccessCallback(ApiModel):
    user_id: str
    session_id: str
    status: Literal["success", "partial_success"]
    generated_at: datetime
    report: ReportContent
    partial_reasons: list[str] = Field(default_factory=list)
    audit: ReportAudit = Field(default_factory=ReportAudit)


class FailureCallback(ApiModel):
    user_id: str
    session_id: str
    status: Literal["failed"] = "failed"
    error_code: str
    message: str


CallbackPayload = SuccessCallback | FailureCallback


class FixedOutputModel(BaseModel):
    """Full validated mapping; clinical delivery applies an HTML projection."""

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
    )


class FixedSubject(FixedOutputModel):
    name: str | None = None
    gender: str | None = None
    age: str | None = None
    report_date: str | None = None


class FixedSample(FixedOutputModel):
    sample_id: str | None = None
    sample_type: str | None = None
    test_item: str | None = None
    sample_date: str | None = None
    report_date: str | None = None
    sequencing_platform: str | None = None


class FixedDiagnosisSummary(FixedOutputModel):
    conclusion: str | None = None
    clinical_context: dict[str, str] = Field(default_factory=dict)
    risk: Literal["低风险", "中等风险", "高风险"] | None = None
    evidence_refs: list[int] = Field(default_factory=list)


class FixedIndicator(FixedOutputModel):
    name: str | None = None
    value: str | None = None
    status: str | None = None


class FixedTips(FixedOutputModel):
    special_reminder: str | None = None
    practical_goal: str | None = None


class FixedRiskAssociation(FixedOutputModel):
    """One evidence-bound disease or adverse-effect association."""

    name: str | None = None
    description: str | None = None
    relation_type: (
        Literal[
            "established_pathogen",
            "opportunistic",
            "epidemiologic_association",
            "colonization",
            "environmental_or_dietary",
        ]
        | None
    ) = None
    evidence_refs: list[int] = Field(default_factory=list)


class FixedAbnormalFinding(FixedOutputModel):
    finding_type: Literal["microbe", "disease"] | None = None
    item_id: str | None = None
    problem: str | None = None
    priority: str | None = None
    indicators: list[FixedIndicator] = Field(default_factory=list)
    related_diseases_or_adverse_effects: list[FixedRiskAssociation] = Field(
        default_factory=list
    )
    biological_mechanism: str | None = None
    recommendations: list[str] = Field(default_factory=list)
    tips: FixedTips = Field(default_factory=FixedTips)
    interpretation_evidence_refs: list[int] = Field(default_factory=list)
    recommendation_evidence_refs: list[int] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)
    evidence_level: str | None = None


class FixedDiagnosticBasis(FixedOutputModel):
    abnormal_findings: list[FixedAbnormalFinding] = Field(default_factory=list)
    evidence_refs: list[int] = Field(default_factory=list)


class FixedPlan(FixedOutputModel):
    future_1_2_weeks: str | None = Field(default=None, alias="未来1-2周")
    future_1_month: str | None = Field(default=None, alias="未来1个月")
    future_2_3_months: str | None = Field(default=None, alias="未来2-3个月")
    future_6_12_months: str | None = Field(default=None, alias="未来6-12个月")


class FixedHealthRecommendation(FixedOutputModel):
    category: Literal["diet", "exercise", "sleep", "bowel_habits", "general"]
    topic: str | None = None
    priority: Literal["重点关注", "建议关注"] | None = None
    content: str | None = None
    evidence_refs: list[int] = Field(default_factory=list)


class FixedRecommendedExamination(FixedOutputModel):
    name: str | None = None
    purpose: Literal["筛查", "确认", "评估"] | None = None
    reason: str | None = None
    evidence_refs: list[int] = Field(default_factory=list)


class FixedDiagnosticAdvice(FixedOutputModel):
    report_interpretation: list[str] = Field(default_factory=list)
    plan: FixedPlan = Field(default_factory=FixedPlan, alias="Plan")
    health_recommendations: list[FixedHealthRecommendation] = Field(
        default_factory=list
    )
    recommended_examinations: list[FixedRecommendedExamination] = Field(
        default_factory=list
    )
    not_recommended_behaviors: dict[str, str] = Field(default_factory=dict)
    medical_alerts: list[str] = Field(default_factory=list)


class FixedEvidence(FixedOutputModel):
    number: int
    evidence_type: str | None = None
    topic: str | None = None
    title: str | None = None
    source: str | None = None
    publication_date: str | None = None
    url: str | None = None
    evidence_level: str | None = None
    provider: str | None = None
    summary: str | None = None
    conclusion: str | None = None
    key_evidence: str | None = None
    patient_related_facts: list[str] = Field(default_factory=list)
    evidence_grade: Literal["强", "中", "弱"] | None = None
    study_type: str | None = None
    authors: list[str] = Field(default_factory=list)


class FixedCallbackError(FixedOutputModel):
    error_code: str | None = None
    message: str | None = None


class FixedReportCallback(FixedOutputModel):
    """Full legacy/follow-up DTO, before clinical public field compaction."""

    user_id: str = Field(alias="userId")
    session_id: str = Field(alias="sessionId")
    # 生成批次引用；五类产物共享同一批次，用于查询、展示与后续咨询。
    # 旧调用方忽略该字段即可，不影响既有解析。
    final_report_id: str | None = Field(default=None, alias="finalReportId")
    report_type: Literal["evaluate"] = Field(default="evaluate", alias="type")
    status: Literal["success", "partial_success", "failed"]
    generated_at: date | None = None
    subject: FixedSubject = Field(default_factory=FixedSubject)
    sample: FixedSample = Field(default_factory=FixedSample)
    health_assessment: HealthAssessment = Field(
        default_factory=HealthAssessment,
        alias="healthAssessment",
    )
    diagnosis_summary: FixedDiagnosisSummary = Field(
        default_factory=FixedDiagnosisSummary
    )
    diagnostic_basis: FixedDiagnosticBasis = Field(default_factory=FixedDiagnosticBasis)
    diagnostic_advice: FixedDiagnosticAdvice = Field(
        default_factory=FixedDiagnosticAdvice
    )
    evidence: list[FixedEvidence] = Field(default_factory=list)
    partial_reasons: list[str] = Field(default_factory=list)
    error: FixedCallbackError = Field(default_factory=FixedCallbackError)


def stable_id(prefix: str, value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        slug = value.encode("utf-8").hex()[:32]
    # Keep IDs readable while guaranteeing that long queries with the same
    # prefix (for example colorectal cancer and colorectal adenoma searches)
    # remain distinct in the evidence audit.
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{slug[:64]}-{digest}"
