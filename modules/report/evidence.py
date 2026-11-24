"""EviMed retrieval, deterministic relevance screening, and citation numbering."""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from modules.report.clinical import disease_has_patient_microbiome_signal
from modules.report.condition_extraction import (
    source_derived_conditions,
    source_mentions_taxon,
)
from modules.report.ports import EvidenceGateway, EvidenceServiceError
from modules.report.schemas import (
    ClinicalReportDocument,
    AbnormalMicrobeItem,
    AbnormalMicrobesSection,
    DiseaseRisksSection,
    EvidenceChannelAudit,
    EvidenceCondition,
    EvidenceIntent,
    EvidenceInterpretation,
    EvidenceQueryAudit,
    EvidenceRecord,
    EvidenceReference,
    EvidenceRelationBinding,
    EvidenceRelationType,
    HealthManagementSection,
    HospitalDataInput,
    PatientEvidenceContext,
    RelationCoverageAudit,
    RiskAssociation,
    stable_id,
)
from modules.report.evaluation_originals import evaluation_original_text

_RELATION_TYPE_ORDER = {
    "established_pathogen": 0,
    "opportunistic": 1,
    "epidemiologic_association": 2,
    "colonization": 3,
    "environmental_or_dietary": 4,
}


@dataclass(frozen=True)
class EvidenceQuery:
    query: str
    supports: tuple[str, ...]
    # Every concept must match at least one of its aliases.  A microbe-disease
    # query therefore cannot pass on the disease name alone.
    required_concepts: tuple[tuple[str, ...], ...]
    intent: EvidenceIntent = "microbe_association"
    target: str = ""
    relation_disease_id: str | None = None
    relation_disease_name: str | None = None
    relation_source_taxon: str | None = None
    matched_microbe_ids: tuple[str, ...] = ()
    channels: tuple[str, ...] = ("guide", "literature")
    age_group: str = "unknown"
    query_language: str = "unknown"
    microbe_id: str | None = None
    disease_id: str | None = None
    relation_id: str | None = None
    relation_type: EvidenceRelationType = "epidemiologic_association"
    screen_profile: str = "gut_association"

    @property
    def query_id(self) -> str:
        return stable_id(
            "query",
            "|".join(
                (
                    self.intent,
                    self.target,
                    self.query,
                    ",".join(self.channels),
                    self.microbe_id or "",
                    self.disease_id or "",
                    self.relation_id or "",
                )
            ),
        )


@dataclass
class EvidenceRetrievalResult:
    records: list[EvidenceRecord]
    failed_supports: set[str]
    query_audits: list[EvidenceQueryAudit]
    relation_coverage: list[RelationCoverageAudit]


def merge_retrieval_results(
    *results: EvidenceRetrievalResult,
) -> EvidenceRetrievalResult:
    return EvidenceRetrievalResult(
        records=_dedupe([record for result in results for record in result.records]),
        failed_supports=set().union(*(result.failed_supports for result in results)),
        query_audits=[audit for result in results for audit in result.query_audits],
        relation_coverage=[
            relation for result in results for relation in result.relation_coverage
        ],
    )


def validate_relation_bindings(records: list[EvidenceRecord]) -> None:
    """Freeze the interpretation made for each exact relation query.

    Relation queries intentionally remain separate until this step.  A paper
    returned for both discovery and an exact microbe-condition query therefore
    cannot inherit the discovery interpretation when it is used as relation
    evidence.
    """
    # Validation used to delete a source-explicit binding when the model
    # returned a generic organism role (``targetAlignment=direct``) but omitted
    # the requested disease name.  Once deleted, the later deterministic
    # recovery had no relation metadata left to inspect.  Recover the exact
    # source clause first; validation remains the final public gate.
    recover_exact_relation_interpretations(records)
    for record in records:
        interpretation = record.interpretation
        if interpretation is None:
            continue
        source_sample_alignment = _source_sample_alignment(record)
        if (
            interpretation.sample_alignment == "unclear"
            and source_sample_alignment != "unclear"
        ):
            # Deterministic relation recovery may replace an earlier model
            # interpretation.  That replacement used to reset an explicitly
            # stated stool/mucosal specimen to ``unclear`` and the clinical
            # promotion step then discarded an otherwise valid association.
            # Recover only literal specimen phrases from the retained source;
            # never infer a gut sample merely from a microbiome keyword.
            interpretation.sample_alignment = source_sample_alignment
        retained_bindings = []
        for binding in record.relation_bindings:
            microbe_aliases = tuple(
                dict.fromkeys(
                    (
                        *_taxon_aliases(binding.microbe_name),
                        *binding.microbe_aliases,
                    )
                )
            )
            interpretation_binds_relation = _claim_names_disease(
                interpretation.claim,
                binding.disease_name,
            ) and source_mentions_taxon(
                interpretation.claim,
                binding.microbe_name,
                aliases=microbe_aliases,
            )
            source_derived_claim_binds_relation = bool(
                binding.claim
                and _claim_names_disease(binding.claim, binding.disease_name)
                and source_mentions_taxon(
                    binding.claim,
                    binding.microbe_name,
                    aliases=microbe_aliases,
                )
            )
            if not (
                interpretation_binds_relation or source_derived_claim_binds_relation
            ) or not _source_explicitly_supports_relation(record, binding):
                continue
            if interpretation_binds_relation:
                binding.claim = interpretation.claim
            binding.target_alignment = interpretation.target_alignment
            binding.taxon_alignment = interpretation.taxon_alignment
            binding.sample_alignment = interpretation.sample_alignment
            binding.patient_applicability = interpretation.patient_applicability
            retained_bindings.append(binding)
        record.relation_bindings = retained_bindings
        if retained_bindings:
            # A different specimen site or population changes applicability,
            # not whether an exact EviMed taxon-outcome source addresses the
            # requested relation.  Keep that distinction deterministic.
            interpretation.target_alignment = "direct"
            for binding in retained_bindings:
                binding.target_alignment = "direct"


def _source_sample_alignment(record: EvidenceRecord) -> str:
    """Return an explicit specimen site stated in title/summary text."""

    source = " ".join(
        part.strip().casefold()
        for part in (record.title, record.summary or "", record.evidence_text or "")
        if part and part.strip()
    )
    if any(
        marker in source
        for marker in (
            "mucosal biopsy",
            "mucosal biopsies",
            "colonic biopsy",
            "colonic biopsies",
            "intestinal biopsy",
            "intestinal biopsies",
            "rectal mucosa",
            "colonic mucosa",
            "intestinal mucosa",
            "luminal washing",
            "luminal washings",
            "肠黏膜活检",
            "肠黏膜标本",
            "结肠活检",
            "黏膜活检",
        )
    ):
        return "mucosa"
    if any(
        marker in source
        for marker in (
            "stool sample",
            "stool samples",
            "stool specimen",
            "stool specimens",
            "stool culture",
            "fecal sample",
            "fecal samples",
            "faecal sample",
            "faecal samples",
            "fecal specimen",
            "faecal specimen",
            "fecal culture",
            "faecal culture",
            "fecal microbiome",
            "faecal microbiome",
            "fecal microbiota",
            "faecal microbiota",
            "fecal samples",
            "粪便样本",
            "粪便标本",
            "粪便培养",
            "粪便菌群",
            "大便样本",
            "大便标本",
        )
    ):
        return "stool"
    if any(
        marker in source
        for marker in (
            "respiratory specimen",
            "respiratory sample",
            "sputum sample",
            "sputum culture",
            "bronchoalveolar lavage",
            "lower respiratory tract",
            "呼吸道标本",
            "痰标本",
            "痰培养",
            "支气管肺泡灌洗液",
        )
    ):
        return "respiratory"
    if any(
        marker in source
        for marker in (
            "blood sample",
            "blood samples",
            "blood specimen",
            "blood specimens",
            "blood culture",
            "blood cultures",
            "bacteremia",
            "bacteraemia",
            "bloodstream infection",
            "血液标本",
            "血标本",
            "血培养",
            "菌血症",
            "血流感染",
        )
    ):
        return "blood"
    return "unclear"


def recover_exact_relation_interpretations(
    records: list[EvidenceRecord],
) -> None:
    """Recover an exact source-bound relation when AI interpretation failed.

    Retrieval has already screened these records for the requested taxon and
    outcome.  This fallback still requires both concepts and a relation marker
    in one source clause/title, and excludes animal/preclinical and single-case
    evidence.  It therefore restores source provenance; it never promotes a
    query hint or a loose co-mention into patient-facing evidence.
    """

    for record in records:
        existing = record.interpretation
        existing_is_direct = bool(
            existing is not None
            and existing.target_alignment == "direct"
            and existing.taxon_alignment in {"species", "genus"}
            and existing.patient_applicability in {"direct", "contextual"}
        )
        if not record.relation_bindings or is_likely_single_case(record):
            continue
        supported_bindings = [
            binding
            for binding in record.relation_bindings
            if binding.relation_type
            in {
                "established_pathogen",
                "opportunistic",
                "epidemiologic_association",
            }
            and _source_explicitly_supports_relation(record, binding)
        ]
        if not supported_bindings:
            continue
        source_directions = {
            direction
            for binding in supported_bindings
            if (
                direction := _source_relation_direction(record, binding)
            )
            in {"increased", "decreased"}
        }
        source_bound_human_abundance = bool(
            source_directions
            and any(
                marker
                in f"{record.title} {record.summary or ''}".casefold()
                for marker in _HUMAN_EVIDENCE_MARKERS
            )
            and any(
                binding.relation_type == "epidemiologic_association"
                for binding in supported_bindings
            )
        )
        if is_preclinical_evidence(record) and not source_bound_human_abundance:
            continue
        if existing_is_direct and all(
            _claim_names_disease(existing.claim, binding.disease_name)
            and source_mentions_taxon(
                existing.claim,
                binding.microbe_name,
                aliases=tuple(binding.microbe_aliases),
            )
            for binding in supported_bindings
        ):
            continue
        claims: list[str] = []
        conditions: list[EvidenceCondition] = []
        taxon_alignments: set[str] = set()
        for binding in supported_bindings:
            display_name = next(
                (
                    alias
                    for alias in binding.microbe_aliases
                    if re.search(r"[\u3400-\u9fff]", alias)
                ),
                binding.microbe_name,
            )
            claim = f"{display_name}与{binding.disease_name}相关。"
            binding.claim = claim
            binding.target_alignment = "direct"
            binding.taxon_alignment = (
                "species" if len(binding.microbe_name.split()) >= 2 else "genus"
            )
            binding.patient_applicability = "contextual"
            claims.append(claim)
            taxon_alignments.add(binding.taxon_alignment)
            conditions.append(
                EvidenceCondition(
                    name=binding.disease_name,
                    search_term=binding.disease_name,
                    source_phrase=record.title,
                    relation_type=binding.relation_type,
                )
            )
        record.interpretation = EvidenceInterpretation(
            claim="".join(dict.fromkeys(claims)),
            study_design=(record.study_types[0] if record.study_types else None),
            confidence="moderate",
            target_alignment="direct",
            direction=(
                next(iter(source_directions))
                if len(source_directions) == 1
                else "mixed"
                if len(source_directions) > 1
                else "not_stated"
            ),
            conditions=conditions,
            taxon_alignment=(
                next(iter(taxon_alignments))
                if len(taxon_alignments) == 1
                else "unclear"
            ),
            sample_alignment=_source_sample_alignment(record),
            patient_applicability="contextual",
            limitations=(
                "仅用于说明文献中的菌种-疾病关系；"
                "粪便样本检出不等同于临床感染。"
            ),
        )


def _source_explicitly_supports_relation(
    record: EvidenceRecord,
    binding: EvidenceRelationBinding,
) -> bool:
    """Require the EviMed title/abstract to bind the exact taxon and outcome.

    Global co-occurrence is insufficient: a handbook table of contents can
    name an organism-associated disease and an unrelated disease hundreds of
    characters apart.  A public relation must occur in one sentence/clause or
    a short title-sized window and contain an actual relation expression.
    """

    source = " ".join(
        part.strip() for part in (record.title, record.summary or "") if part.strip()
    )
    if not source:
        return False
    if _source_relation_requires_unmeasured_subtype(record, binding):
        # A species-level stool result cannot inherit a relation demonstrated
        # only for a toxin-producing strain or a toxin gene.  For example,
        # detecting B. fragilis does not establish that enterotoxigenic
        # B. fragilis (ETBF/bft+) was present.
        return False
    if _normalize(binding.microbe_name) == "mycobacteriumtuberculosis" and any(
        marker in source.casefold()
        for marker in (
            "nontuberculous mycobacter",
            "non-tuberculous mycobacter",
            "非结核分枝杆菌",
        )
    ):
        # NTM sources often name M. tuberculosis only to define the excluded
        # taxonomic group; that occurrence cannot support a tuberculosis-
        # organism disease relation.
        return False
    microbe_aliases = tuple(
        dict.fromkeys(
            (
                binding.microbe_name,
                *binding.microbe_aliases,
                *_scientific_terms(binding.microbe_name),
            )
        )
    )
    disease_aliases = tuple(dict.fromkeys(_disease_terms(binding.disease_name)))
    relation_markers = (
        "caused by",
        "causes ",
        "cause of",
        "causes of",
        "due to",
        "causative",
        "etiologic",
        "aetiologic",
        "responsible for",
        "associated with",
        "association between",
        "relationship between",
        "relation to",
        "related to",
        "correlated with",
        " link ",
        "links ",
        "linked to",
        "linked with",
        "pathogenic role",
        "enriched in",
        "increased in",
        "decreased in",
        "depleted",
        "depletion",
        "reduced abundance",
        "lower abundance",
        "lower levels",
        "decreased levels",
        "reduced levels",
        "higher levels",
        "increased levels",
        "risk of",
        "risk for",
        "引起",
        "导致",
        "病原体",
        "致病菌",
        "感染",
        "可致",
        "与",
        "相关",
        "关联",
        "富集",
        "增加",
        "降低",
    )
    # Keep the complete title as one semantic unit.  Sentence splitting on
    # periods otherwise breaks abbreviated taxonomic titles such as
    # ``L. dumoffii sp. nov. ... associated with human pneumonia`` before the
    # organism and outcome can be compared.
    summary_segments = [
        segment
        for segment in re.split(
            r"(?<=[.!?。！？；;])\s*|[\r\n]+",
            record.summary or "",
        )
        if segment.strip()
    ]
    # Period-based splitting breaks abbreviated Latin species names such as
    # ``C. coli`` into separate fragments.  Add small adjacent windows so the
    # taxon and outcome can still be evaluated as one source sentence without
    # falling back to global abstract co-occurrence.
    adjacent_windows: list[str] = []
    for index, segment in enumerate(summary_segments[:-1]):
        if not re.search(r"\b[A-Z]\.$", segment.strip()):
            continue
        parts = [segment]
        cursor = index
        while cursor + 1 < len(summary_segments):
            cursor += 1
            parts.append(summary_segments[cursor])
            if not re.search(r"\b[A-Z]\.$", parts[-1].strip()):
                break
        adjacent_windows.append(" ".join(parts))
    if binding.relation_type == "epidemiologic_association":
        abundance_markers = (
            "depleted",
            "depletion",
            "reduced abundance",
            "decreased abundance",
            "lower abundance",
            "absence",
            "enriched",
            "overabundance",
            "over-abundance",
            "increased abundance",
            "higher abundance",
            "丰度降低",
            "丰度减少",
            "缺乏",
            "丰度增加",
            "丰度升高",
            "富集",
        )
        for width in (2, 3):
            for index in range(max(0, len(summary_segments) - width + 1)):
                window = " ".join(summary_segments[index : index + width])
                if any(marker in window.casefold() for marker in abundance_markers):
                    adjacent_windows.append(window)
    segments = [record.title, *summary_segments, *adjacent_windows]
    for segment in segments:
        if not segment.strip():
            continue
        if re.search(
            r"(?:基本)?目录|第[一二三四五六七八九十百\d]+(?:章|节)",
            segment,
        ):
            continue
        normalized_segment = _normalize(segment)
        microbe_hits = [
            normalized_segment.find(_normalize(alias))
            for alias in microbe_aliases
            if alias and _normalize(alias) in normalized_segment
        ]
        disease_hits = []
        for alias in disease_aliases:
            normalized_alias = _normalize(alias)
            if not normalized_alias or normalized_alias not in normalized_segment:
                continue
            hit = normalized_segment.find(normalized_alias)
            if (
                binding.disease_name == "结核病"
                and normalized_alias == "tuberculosis"
                and normalized_segment[max(0, hit - 20) : hit].endswith("mycobacterium")
            ):
                # ``Mycobacterium tuberculosis`` is another organism name, not
                # by itself a statement that the requested taxon is related to
                # tuberculosis disease.
                continue
            disease_hits.append(hit)
        if not microbe_hits or not disease_hits:
            continue
        lowered = segment.casefold()
        local_distance = min(
            abs(left - right) for left in microbe_hits for right in disease_hits
        )
        abundance_window = (
            binding.relation_type == "epidemiologic_association"
            and any(
                marker in lowered
                for marker in (
                    "depleted",
                    "depletion",
                    "reduced abundance",
                    "decreased abundance",
                    "lower abundance",
                    "absence",
                    "enriched",
                    "overabundance",
                    "over-abundance",
                    "increased abundance",
                    "higher abundance",
                    "丰度降低",
                    "丰度减少",
                    "缺乏",
                    "丰度增加",
                    "丰度升高",
                    "富集",
                )
            )
        )
        if local_distance > (360 if abundance_window else 180):
            continue
        # A curated disease alias can itself be an organism-specific disease
        # entity, for example ``Mycobacterium kansasii pulmonary disease``.
        # Such a phrase is stronger than loose co-occurrence even when the
        # surrounding review sentence has no separate causal verb.  Require
        # the complete alias to contain the exact requested taxon so generic
        # phrases such as ``pulmonary disease`` cannot use this shortcut.
        taxon_specific_disease_entity = any(
            (normalized_disease_alias := _normalize(disease_alias))
            and normalized_disease_alias in normalized_segment
            and any(
                (normalized_microbe_alias := _normalize(microbe_alias))
                and normalized_microbe_alias in normalized_disease_alias
                for microbe_alias in microbe_aliases
            )
            for disease_alias in disease_aliases
        )
        if taxon_specific_disease_entity and binding.relation_type in {
            "established_pathogen",
            "opportunistic",
        }:
            return True
        target_only_comparator = any(
            (match := re.search(marker, lowered)) is not None
            and not any(alias.casefold() in lowered[: match.start()] for alias in microbe_aliases if alias)
            and any(alias.casefold() in lowered[match.end() :] for alias in microbe_aliases if alias)
            for marker in (
                r"higher than",
                r"lower than",
                r"more\s+[a-z-]+(?:\s+[a-z-]+){0,2}\s+than",
                r"compared with",
                r"compared to",
                r"高于",
                r"低于",
                r"相比",
                r"区别于",
            )
        )
        if target_only_comparator:
            continue
        # A concise clinical title such as ``Bacteremic Staphylococcus aureus
        # pneumonia`` or ``铜绿假单胞菌下呼吸道感染诊治专家共识`` names the exact
        # organism-condition entity even without a causal verb.  That shortcut
        # is safe for established/opportunistic pathogens.  An epidemiologic
        # association still needs explicit association wording; otherwise a
        # title that merely lists a taxon and a case population could be
        # promoted into a patient-facing disease relation.
        verb_less_title_blocked = any(
            marker in record.title.casefold()
            for marker in (
                "microbiota",
                "microbiome",
                "metagenom",
                "signature",
                "abundance",
                "enriched",
                "cohort",
                " patients",
                "菌群",
                "丰度",
                "富集",
                "人群",
                "队列",
            )
        )
        if (
            segment == record.title
            and not verb_less_title_blocked
            and binding.relation_type
            in {
                "established_pathogen",
                "opportunistic",
            }
            and min(
                abs(left - right) for left in microbe_hits for right in disease_hits
            )
            <= 80
        ):
            return True
        if any(marker in lowered for marker in relation_markers):
            return True
    return False


_SUBTYPE_ONLY_RELATION_MARKERS = (
    "enterotoxigenic",
    "toxigenic strain",
    "toxigenic strains",
    "toxin-producing",
    "toxin producing",
    "bft gene",
    "bft1",
    "bft2",
    "bacteroides fragilis toxin",
    "产肠毒素",
    "产毒菌株",
    "产毒株",
    "产毒型",
    "毒素基因",
)


def _source_relation_requires_unmeasured_subtype(
    record: EvidenceRecord,
    binding: EvidenceRelationBinding,
) -> bool:
    """Reject subtype-only evidence when the requested target is generic."""

    source = f"{record.title} {record.summary or ''}".casefold()
    if not any(marker in source for marker in _SUBTYPE_ONLY_RELATION_MARKERS):
        return False
    requested = " ".join(
        (
            binding.microbe_name,
            *binding.microbe_aliases,
            *record.targets,
        )
    ).casefold()
    return not any(marker in requested for marker in _SUBTYPE_ONLY_RELATION_MARKERS)


def _source_relation_direction(
    record: EvidenceRecord,
    binding: EvidenceRelationBinding,
) -> str:
    """Read abundance direction from the shortest exact taxon-outcome window."""

    sentences = [
        sentence.strip()
        for sentence in re.split(
            r"(?<=[.!?。！？；;])\s*|[\r\n]+",
            record.summary or "",
        )
        if sentence.strip()
    ]
    windows = [record.title, *sentences]
    for width in (2, 3):
        windows.extend(
            " ".join(sentences[index : index + width])
            for index in range(max(0, len(sentences) - width + 1))
        )
    low_markers = (
        "depleted",
        "depletion",
        "reduced abundance",
        "decreased abundance",
        "lower abundance",
        "lower levels",
        "decreased levels",
        "reduced levels",
        "decreased in",
        "absence",
        "absent",
        "丰度降低",
        "丰度减少",
        "低丰度",
        "缺乏",
        "耗竭",
    )
    high_markers = (
        "enriched",
        "overabundance",
        "over-abundance",
        "increased abundance",
        "higher abundance",
        "higher levels",
        "increased levels",
        "increased in",
        "high abundance",
        "丰度增加",
        "丰度升高",
        "高丰度",
        "富集",
    )
    for window in windows:
        if not source_mentions_taxon(
            window,
            binding.microbe_name,
            aliases=tuple(
                dict.fromkeys(
                    (*binding.microbe_aliases, *_scientific_terms(binding.microbe_name))
                )
            ),
        ) or not _claim_names_disease(window, binding.disease_name):
            continue
        lowered = window.casefold()
        low = any(marker in lowered for marker in low_markers)
        high = any(marker in lowered for marker in high_markers)
        if low != high:
            return "decreased" if low else "increased"
    return "not_stated"


def _source_clause_has_relation_marker(
    record: EvidenceRecord,
    binding: EvidenceRelationBinding,
    *,
    disease_name: str,
    markers: tuple[str, ...],
) -> bool:
    """Match relation wording inside one taxon-outcome clause.

    Looking at the entire abstract can borrow ``引起`` from an unrelated
    outcome and turn a nearby association into a causal claim. Clause-local
    matching preserves causal wording only when all three elements occur
    together.
    """

    for clause in re.split(
        r"[，,；;。！？!?]+|(?<=[A-Za-z])\.(?:\s+|$)",
        f"{record.title}。{record.summary or ''}",
    ):
        if (
            clause.strip()
            and source_mentions_taxon(
                clause,
                binding.microbe_name,
                aliases=tuple(binding.microbe_aliases),
            )
            and _claim_names_disease(clause, disease_name)
            and any(marker in clause.casefold() for marker in markers)
        ):
            return True
    return False


def recover_source_bound_topic_interpretations(
    records: list[EvidenceRecord],
) -> None:
    """Recover exact function/barrier sources after a model omission.

    This creates no condition and no disease relation binding.  It can only
    support a bounded function/barrier explanation for the same report taxon.
    """

    topic_markers = {
        "gut_function": (
            "short chain fatty acid",
            "scfa",
            "butyrate",
            "propionate",
            "acetate",
            "bile acid",
            "短链脂肪酸",
            "丁酸",
            "丙酸",
            "乙酸",
            "胆汁酸",
        ),
        "gut_barrier": (
            "intestinal permeability",
            "tight junction",
            "mucus",
            "zonulin",
            "calprotectin",
            "endotoxin",
            "肠道通透性",
            "紧密连接",
            "黏液层",
            "连蛋白",
            "钙卫蛋白",
            "内毒素",
            "肠道屏障",
        ),
    }
    for record in records:
        if record.interpretation is not None:
            continue
        intent = next(
            (
                value
                for value in ("gut_function", "gut_barrier")
                if value in record.intents
            ),
            None,
        )
        if intent is None or not record.microbe_ids:
            continue
        target_parts = [
            part.strip()
            for target in record.targets
            for part in target.split("/")
            if part.strip()
        ]
        scientific_name = next(
            (
                part
                for part in target_parts
                if re.fullmatch(r"[A-Z][a-z-]+(?:\s+[a-z][a-z-]+)+", part)
            ),
            None,
        )
        if not scientific_name:
            continue
        display_name = target_parts[1] if len(target_parts) >= 2 else scientific_name
        topic_name = target_parts[-1] if target_parts else "肠道功能"
        clauses = re.split(
            r"(?<=[.!?。！？])\s+|[；;]",
            f"{record.title}. {record.summary or ''}",
        )
        matched_clause = next(
            (
                clause.strip()
                for clause in clauses
                if source_mentions_taxon(clause, scientific_name)
                and any(marker in clause.casefold() for marker in topic_markers[intent])
                and any(marker in clause.casefold() for marker in _ASSOCIATION_MARKERS)
            ),
            None,
        )
        if not matched_clause:
            continue
        record.interpretation = EvidenceInterpretation(
            claim=f"人体肠道研究报告了{display_name}与{topic_name}之间的相关性。",
            study_design=_deterministic_study_design(record),
            direction="associated",
            applicability="该证据用于解释菌群相关功能，不用于疾病诊断。",
            limitations="来源仅支持相关性解释。",
            confidence="moderate",
            target_alignment="direct",
            taxon_alignment=(
                "species" if len(scientific_name.split()) >= 2 else "genus"
            ),
            sample_alignment=(
                "stool"
                if any(
                    marker in matched_clause.casefold()
                    for marker in ("stool", "fecal", "faecal", "粪便")
                )
                else "unclear"
            ),
            patient_applicability="contextual",
        )


def _deterministic_study_design(record: EvidenceRecord) -> str | None:
    text = " ".join(
        (record.title, record.summary or "", " ".join(record.study_types))
    ).casefold()
    for markers, label in (
        (("meta-analysis", "meta analysis", "meta分析"), "Meta分析"),
        (("systematic review", "系统综述"), "系统综述"),
        (("randomized", "randomised", "随机对照"), "随机对照试验"),
        (("prospective cohort", "前瞻性队列"), "前瞻性队列研究"),
        (("cohort", "队列"), "队列研究"),
        (("case-control", "case control", "病例对照"), "病例对照研究"),
        (("cross-sectional", "cross sectional", "横断面"), "横断面研究"),
        (("clinical trial", "临床试验"), "临床试验"),
    ):
        if any(marker in text for marker in markers):
            return label
    return "临床指南" if record.category == "guideline" else None


def promote_discovery_conditions_to_bindings(
    records: list[EvidenceRecord],
    microbes: AbnormalMicrobesSection,
) -> None:
    """Use an already interpreted EviMed source as its own relation evidence.

    A discovery result can directly name both the exact taxon and a clinical
    condition in its title/summary.  Requiring a second search to return the
    same paper is unnecessary and previously discarded valid EviMed evidence.
    Promotion remains conservative: only direct human, non-case evidence with
    an exact taxonomic alignment and a claim that names the condition is used.
    """
    microbes_by_id = {item.microbe_id: item for item in microbes.items}
    for record in records:
        if not set(record.intents) & {
            "microbe_discovery",
            "microbe_clinical_significance",
            "microbe_association",
            "microbe_disease_association",
        }:
            continue
        interpretation = record.interpretation
        if (
            interpretation is None
            or interpretation.target_alignment not in {"direct", "indirect"}
            # ``unclear`` means the abstract omitted optional population or
            # specimen detail.  It is a soft limitation: retain the exact
            # candidate so the second pass can resolve it.  Only an explicit
            # mismatch is a hard exclusion.
            or interpretation.patient_applicability == "not_applicable"
            or interpretation.taxon_alignment not in {"species", "genus"}
            or interpretation.study_design == "病例报告"
            or is_likely_single_case(record)
            or is_preclinical_evidence(record)
        ):
            continue
        existing = {binding.relation_id for binding in record.relation_bindings}
        for microbe_id in record.microbe_ids:
            microbe = microbes_by_id.get(microbe_id)
            if microbe is None:
                continue
            if microbe.category in {"beneficial_abnormal", "other_abnormal"} and (
                record.category == "clinical_trial"
                or _is_abundance_intervention_evidence(record)
            ):
                # A probiotic/synbiotic intervention can support an intervention
                # effect, but it cannot prove that this patient's low or high
                # baseline abundance is associated with the studied disease.
                continue
            expected_alignment = (
                "species" if len(microbe.scientific_name.split()) >= 2 else "genus"
            )
            if interpretation.taxon_alignment != expected_alignment:
                continue
            conditions = list(interpretation.conditions)
            # A direct taxon search can already return a title/summary that
            # explicitly names a concrete disease. Prefer those source phrases
            # for every discovery intent, not only for a later exact query.
            # They remain non-public until the ordinary source/taxon/outcome
            # validation below succeeds.
            source_conditions, _source_claim = source_derived_conditions(record)
            source_condition_names = {
                _canonical_condition_name(condition.name)
                for condition in source_conditions
            }
            conditions.extend(source_conditions)
            for condition in conditions:
                source_derived = (
                    _canonical_condition_name(condition.name) in source_condition_names
                )
                interpretation_names_condition = _claim_names_disease(
                    interpretation.claim,
                    condition.name,
                )
                if condition.relation_type not in {
                    "established_pathogen",
                    "opportunistic",
                    "epidemiologic_association",
                } or not (interpretation_names_condition or source_derived):
                    continue
                disease_id = stable_id("evidence-condition", condition.name)
                relation_id = stable_id(
                    "relation",
                    f"source|{record.evidence_key}|{microbe_id}|{disease_id}",
                )
                if relation_id in existing:
                    continue
                existing.add(relation_id)
                record.disease_ids = list(
                    dict.fromkeys([*record.disease_ids, disease_id])
                )
                record.relation_bindings.append(
                    EvidenceRelationBinding(
                        relation_id=relation_id,
                        microbe_id=microbe_id,
                        microbe_name=microbe.scientific_name,
                        microbe_aliases=[microbe.name],
                        disease_id=disease_id,
                        disease_name=condition.name,
                        relation_type=condition.relation_type,
                        claim=(
                            interpretation.claim
                            if interpretation_names_condition
                            else f"{microbe.name}与{condition.name}相关。"
                        ),
                        target_alignment=interpretation.target_alignment,
                        taxon_alignment=interpretation.taxon_alignment,
                        sample_alignment=interpretation.sample_alignment,
                        patient_applicability=interpretation.patient_applicability,
                    )
                )


def _claim_names_disease(claim: str, disease_name: str) -> bool:
    normalized_claim = _normalize(claim)
    for alias in _disease_terms(disease_name):
        compact = re.sub(r"[^A-Za-z0-9]", "", alias)
        if compact.isupper() and len(compact) <= 5:
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(compact)}(?![A-Za-z0-9])",
                claim,
                flags=re.IGNORECASE,
            ):
                return True
            continue
        normalized_alias = _normalize(alias)
        if normalized_alias and normalized_alias in normalized_claim:
            return True
    return False


def _is_abundance_intervention_evidence(record: EvidenceRecord) -> bool:
    title = record.title.casefold()
    return any(marker in title for marker in _ABUNDANCE_INTERVENTION_TITLE_MARKERS)


def consolidate_public_records(records: list[EvidenceRecord]) -> list[EvidenceRecord]:
    """Collapse already-validated duplicate citations without losing bindings."""
    consolidated = _dedupe(records, preserve_relation_scope=False)
    for record in consolidated:
        if record.source_evidence_key:
            record.evidence_key = record.source_evidence_key
            record.source_evidence_key = None
    return consolidated


def citation_numbers_for_semantic_records(
    records: list[EvidenceRecord],
    citation_records: list[EvidenceRecord],
    citation_numbers: dict[str, int],
) -> dict[str, int]:
    """Map intent-specific records onto one deduplicated bibliography number."""
    by_source = {
        (record.category, _normalize(record.title)): citation_numbers[
            record.evidence_key
        ]
        for record in citation_records
        if record.evidence_key in citation_numbers
    }
    return {
        record.evidence_key: number
        for record in records
        if (number := by_source.get((record.category, _normalize(record.title))))
        is not None
    }


def is_preclinical_evidence(record: EvidenceRecord) -> bool:
    interpretation = record.interpretation
    text = " ".join(
        str(value or "")
        for value in (
            record.title,
            record.source,
            record.summary,
            interpretation.claim if interpretation else None,
            interpretation.population if interpretation else None,
            interpretation.clinical_role if interpretation else None,
            interpretation.limitations if interpretation else None,
        )
    ).casefold()
    metadata = " ".join(record.study_types).casefold()
    has_preclinical_marker = any(
        marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS
    ) or any(
        marker in metadata
        for marker in ("动物实验", "体外实验", "animal", "in vitro", "preclinical")
    )
    if not has_preclinical_marker:
        return False
    # A human pathogen guideline/review can state an exact clinical relation
    # and then discuss epithelial-cell or animal mechanisms later in the same
    # abstract.  Do not relabel the complete clinical source as preclinical
    # when its source text itself binds the requested taxon and disease.  This
    # exception is deliberately unavailable to primary laboratory studies.
    is_synthesis = record.category == "guideline" or "review" in metadata
    source_bound_human_abundance = bool(
        interpretation is not None
        and interpretation.direction in {"increased", "decreased"}
        and interpretation.patient_applicability != "not_applicable"
        and any(marker in text for marker in _HUMAN_EVIDENCE_MARKERS)
        and any(
            binding.relation_type == "epidemiologic_association"
            and _source_explicitly_supports_relation(record, binding)
            for binding in record.relation_bindings
        )
    )
    return not (
        source_bound_human_abundance
        or (
            is_synthesis
            and any(
                binding.relation_type in {"established_pathogen", "opportunistic"}
                and _source_explicitly_supports_relation(record, binding)
                for binding in record.relation_bindings
            )
        )
    )


def update_query_audit_counts(
    result: EvidenceRetrievalResult,
    *,
    public_records: list[EvidenceRecord] | None = None,
) -> None:
    public_keys = {record.evidence_key for record in (public_records or [])}
    for audit in result.query_audits:
        matching = [
            record for record in result.records if audit.query_id in record.query_ids
        ]
        audit.interpreted_count = sum(
            record.interpretation is not None for record in matching
        )
        audit.public_count = sum(
            record.evidence_key in public_keys for record in matching
        )
        if audit.retained_count > audit.interpreted_count:
            audit.rejected_reasons["ai_interpretation_failed"] = (
                audit.retained_count - audit.interpreted_count
            )
        if audit.relation_id and audit.interpreted_count > audit.public_count:
            audit.rejected_reasons["relation_not_confirmed"] = (
                audit.interpreted_count - audit.public_count
            )


def is_likely_single_case(record: EvidenceRecord) -> bool:
    text = f"{record.title} {record.summary or ''} {record.source}".casefold()
    study_types = " ".join(record.study_types).casefold()
    return (
        "病例报告" in study_types
        or "case report" in study_types
        or bool(
            re.search(
                r"\bcase report(?:s)?\b|\ba case of\b|\bautopsy case\b|"
                r"\bcase discussion\b|\bcase presentation\b|"
                r"\brare cause of\b|\b(?:in|from) a patient (?:with|who|suffering|diagnosed)\b|"
                r"\bwe describe (?:the case of )?(?:a|one) patient\b|"
                r"\bwe (?:present|report|describe) (?:a|one) (?:case|patient)\b|"
                r"\b\d{1,3}-year-old\b|病例报告|个案|一例|1例|"
                r"本文作者曾遇|现报道如下|突然接诊一位|"
                r"病例讨论|病例分享",
                text,
            )
        )
    )


def _preinterpretation_priority(
    record: EvidenceRecord,
    *,
    query: EvidenceQuery,
) -> tuple[float, ...]:
    """Prefer reusable human evidence before recent single-patient reports."""
    text = f"{record.title} {record.summary or ''}".casefold()
    action_only = any(
        marker in record.title.casefold() for marker in _DISEASE_ACTION_ONLY_MARKERS
    )
    reusable_design = any(
        marker in text
        for marker in (
            "guideline",
            "consensus",
            "systematic review",
            "meta-analysis",
            "meta analysis",
            "cohort",
            "case-control",
            "cross-sectional",
            "review",
            "outbreak",
            "investigation",
            "指南",
            "共识",
            "综述",
            "队列",
            "暴发",
            "流行病学调查",
        )
    )
    relation_query = query.screen_profile in {"human_relation", "pathogen_relation"}
    population_match = not _adult_query_has_pediatric_only_source(
        text,
        age_group=query.age_group,
    )
    title = record.title.casefold()
    normalized_title = _normalize(title)
    target_in_title = bool(
        query.required_concepts
        and any(
            _concept_match_score(title, normalized_title, term) > 0
            for term in query.required_concepts[0]
        )
    )
    metadata = " ".join(record.study_types).casefold()
    metadata_design_score = max(
        (
            score
            for marker, score in (
                ("系统综述/meta分析", 8.0),
                ("meta-analysis", 8.0),
                ("队列研究", 7.0),
                ("病例对照研究", 6.0),
                ("横断面研究", 5.0),
                ("随机对照试验", 4.0),
                ("传统综述", 3.0),
                ("指南/共识", 3.0),
            )
            if marker in metadata
        ),
        default=0.0,
    )
    ecological_priority = ()
    if query.screen_profile == 'evaluate_ecological_association':
        from .evaluation_function_evidence import ecological_association_priority
        ecological_priority = (ecological_association_priority(evaluation_original_text(record),
            record.title, query.required_concepts[0], query.required_concepts[2]),
            int(bool(re.fullmatch(r'(?:pubmed:)?\d+', str(record.source_id or '')))))
    return (*ecological_priority,
        float(not (relation_query and is_likely_single_case(record))),
        float(not (relation_query and action_only)),
        float(population_match),
        # EviMed V2 may return a long text block that mentions the requested
        # disease only incidentally near the end.  Prefer a source whose title
        # names the target before study-design and date ranking are applied.
        float(target_in_title),
        float(reusable_design),
        metadata_design_score,
        record.relevance,
        float(bool(record.core_journals)),
        min(record.impact_factor or 0.0, 100.0),
        float(record.year or 0),
    )


def _record_can_suppress_relation_fallback(record: EvidenceRecord) -> bool:
    interpretation = record.interpretation
    return bool(
        record.relation_bindings
        and interpretation is not None
        and interpretation.target_alignment == "direct"
        and interpretation.taxon_alignment not in {"strain", "toxin_gene", "unclear"}
        and interpretation.patient_applicability in {"direct", "contextual"}
        and interpretation.study_design != "病例报告"
        and not is_likely_single_case(record)
        and not is_preclinical_evidence(record)
    )


async def retrieve_evidence(
    client: EvidenceGateway,
    queries: list[EvidenceQuery],
    *,
    concurrency: int = 8,
) -> EvidenceRetrievalResult:
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run(query: EvidenceQuery):
        async with semaphore:
            if not query.channels:
                channels = [
                    EvidenceChannelAudit(channel="guide", status="not_requested"),
                    EvidenceChannelAudit(channel="literature", status="not_requested"),
                ]
                return query, [], False, channels, {"taxon_unresolved": 1}, 0
            try:
                if hasattr(client, "search_detailed"):
                    search = client.search_detailed
                    if (query.screen_profile == "evaluate_general_function"
                            and query.intent == "gut_function"
                            and hasattr(client, "search_evaluation_function_detailed")):
                        search = client.search_evaluation_function_detailed
                    outcomes = await search(
                        query.query,
                        supports=list(query.supports),
                        channels=query.channels,
                    )
                    records = [
                        record
                        for outcome in outcomes
                        if outcome.status == "success"
                        for record in outcome.records
                    ]
                    channels = [
                        EvidenceChannelAudit(
                            channel=outcome.channel,
                            status=outcome.status,
                            provider=outcome.provider,
                            elapsed_ms=outcome.elapsed_ms,
                            raw_count=len(outcome.records),
                        )
                        for outcome in outcomes
                    ]
                    did_fail = any(outcome.status == "failed" for outcome in outcomes)
                else:
                    records = await client.search(
                        query.query, supports=list(query.supports)
                    )
                    channels = [
                        EvidenceChannelAudit(
                            channel="guide",
                            status=(
                                "success"
                                if "guide" in query.channels
                                else "not_requested"
                            ),
                            raw_count=(
                                sum(
                                    record.category == "guideline" for record in records
                                )
                                if "guide" in query.channels
                                else 0
                            ),
                        ),
                        EvidenceChannelAudit(
                            channel="literature",
                            status=(
                                "success"
                                if "literature" in query.channels
                                else "not_requested"
                            ),
                            raw_count=(
                                sum(
                                    record.category != "guideline" for record in records
                                )
                                if "literature" in query.channels
                                else 0
                            ),
                        ),
                    ]
                    did_fail = False
            except EvidenceServiceError:
                channels = [
                    EvidenceChannelAudit(channel="guide", status="failed"),
                    EvidenceChannelAudit(channel="literature", status="failed"),
                ]
                return query, [], True, channels, {"channel_failure": 1}, 0
            retained: list[EvidenceRecord] = []
            rejected: defaultdict[str, int] = defaultdict(int)
            for source_record in records:
                # Some clients and test doubles reuse model instances across
                # searches.  Never let one query mutate another query's result.
                record = source_record.model_copy(deep=True)
                relevant, reason = _screen(record, query=query)
                if not relevant:
                    rejected[reason] += 1
                    continue
                record.intents = list(dict.fromkeys([*record.intents, query.intent]))
                if query.target:
                    record.targets = list(
                        dict.fromkeys([*record.targets, query.target])
                    )
                record.query_ids = list(
                    dict.fromkeys([*record.query_ids, query.query_id])
                )
                if query.microbe_id:
                    record.microbe_ids = list(
                        dict.fromkeys([*record.microbe_ids, query.microbe_id])
                    )
                if query.disease_id:
                    record.disease_ids = list(
                        dict.fromkeys([*record.disease_ids, query.disease_id])
                    )
                if (
                    query.relation_id
                    and query.microbe_id
                    and query.disease_id
                    and query.relation_disease_name
                ):
                    record.source_evidence_key = (
                        record.source_evidence_key or record.evidence_key
                    )
                    record.evidence_key = stable_id(
                        "evidence-relation",
                        f"{record.source_evidence_key}|{query.relation_id}",
                    )
                    record.relation_bindings.append(
                        EvidenceRelationBinding(
                            relation_id=query.relation_id,
                            microbe_id=query.microbe_id,
                            microbe_name=(query.relation_source_taxon or query.target),
                            microbe_aliases=[
                                component.strip()
                                for component in query.target.split("/")
                                if component.strip()
                                and not _claim_names_disease(
                                    component,
                                    query.relation_disease_name,
                                )
                            ],
                            disease_id=query.disease_id,
                            disease_name=query.relation_disease_name,
                            relation_type=query.relation_type,
                        )
                    )
                else:
                    # One EviMed citation may legitimately serve different
                    # report purposes (for example disease explanation,
                    # diagnostic evaluation and bowel-habit guidance).  Give
                    # each semantic scope its own interpretation key now; the
                    # bibliography is deduplicated by source metadata later.
                    record.source_evidence_key = (
                        record.source_evidence_key or record.evidence_key
                    )
                    record.evidence_key = stable_id(
                        "evidence-semantic",
                        "|".join(
                            (
                                record.source_evidence_key,
                                query.intent,
                                ",".join(sorted(query.supports)),
                                query.target or "",
                            )
                        ),
                    )
                retained.append(record)
            screened_count = len(retained)
            if query.screen_profile in {"clinical_report", "evaluate_general_function", "evaluate_ecological_association"}:
                # Clinical applicability decides which candidates can be used.
                # Do not let the old first-two health cap erase later sources.
                limit = len(retained)
            elif query.intent == "microbe_discovery":
                limit = 3
            elif query.intent in {
                "disease_clinical_significance",
                "diagnostic_guideline",
            }:
                # Leave room for semantic screening after EviMed ranking. The
                # earliest hits are often treatment or special-population
                # papers rather than usable explanation/diagnostic evidence.
                limit = 6
            elif query.intent == "microbe_disease_association":
                # A patient-facing row still exposes at most three risks, but
                # an exact relation query needs enough distinct EviMed records
                # for semantic screening after population and case-report
                # checks.  Two near-duplicate hits previously made valid adult
                # evidence disappear before interpretation.
                limit = 5
            elif query.intent in {"gut_function", "gut_barrier", "intervention"}:
                limit = 3
            else:
                limit = 2
            retained.sort(
                key=lambda item: _preinterpretation_priority(item, query=query),
                reverse=True,
            )
            # EviMed can return the same indexed article more than once (for
            # example, title-casing or journal-source variants).  Deduplicate
            # before applying the small interpretation budget so duplicates
            # cannot crowd out a distinct adult or guideline record.
            retained = _dedupe(retained, preserve_relation_scope=True)
            # Public metadata/full text is optional and expensive. Enrich only
            # candidates that already passed the same deterministic human,
            # site and semantic screen used by the report.
            enrich_screened = getattr(client, "enrich_screened", None)
            if enrich_screened is not None and retained:
                try:
                    enriched = await enrich_screened(
                        retained[:min(limit, 6)] if query.screen_profile in {"clinical_report", "evaluate_general_function", "evaluate_ecological_association"} else retained[:limit],
                        query=query.query,
                        supports=list(query.supports),
                    )
                except EvidenceServiceError:
                    # Abstract-level records already passed the ordinary
                    # screen. Optional metadata/full-text enrichment must not
                    # turn one transport failure into a report-wide failure.
                    rejected["public_enrichment_unavailable"] += 1
                    enriched = []
                for enriched_record in enriched:
                    relevant, reason = _screen(enriched_record, query=query)
                    if relevant:
                        retained.append(enriched_record)
                    else:
                        rejected[reason] += 1
                retained.sort(
                    key=lambda item: _preinterpretation_priority(item, query=query),
                    reverse=True,
                )
                retained = _dedupe(retained, preserve_relation_scope=True)
            if len(retained) > limit:
                rejected["query_result_limit"] += len(retained) - limit
                retained = retained[:limit]
            return (
                query,
                retained,
                did_fail,
                channels,
                dict(rejected),
                screened_count,
            )

    responses = await asyncio.gather(*(run(query) for query in queries))
    records: list[EvidenceRecord] = []
    failed: set[str] = set()
    audits: list[EvidenceQueryAudit] = []
    relations: list[RelationCoverageAudit] = []
    for query, items, did_fail, channels, rejected, screened_count in responses:
        if did_fail:
            failed.update(query.supports)
        records.extend(items)
        audits.append(
            EvidenceQueryAudit(
                query_id=query.query_id,
                query_text=query.query,
                query_language=query.query_language,
                intent=query.intent,
                target=query.target or query.query,
                microbe_id=query.microbe_id,
                disease_id=query.disease_id,
                relation_id=query.relation_id,
                channels=channels,
                raw_count=sum(channel.raw_count for channel in channels),
                screened_count=screened_count,
                retained_count=len(items),
                rejected_reasons=rejected,
            )
        )
        if query.relation_disease_id and query.relation_source_taxon:
            relations.append(
                RelationCoverageAudit(
                    disease_id=query.relation_disease_id,
                    disease_name=query.relation_disease_name or "",
                    source_taxon=query.relation_source_taxon,
                    matched_microbe_ids=list(query.matched_microbe_ids),
                    query_id=query.query_id,
                )
            )
    return EvidenceRetrievalResult(
        records=_dedupe(records, preserve_relation_scope=True),
        failed_supports=failed,
        query_audits=audits,
        relation_coverage=relations,
    )


def make_queries(
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    *,
    age: int | None = None,
    hospital_data: list[HospitalDataInput] | None = None,
) -> list[EvidenceQuery]:
    microbe_queries: list[EvidenceQuery] = []
    disease_queries: list[EvidenceQuery] = []
    health_queries: list[EvidenceQuery] = []
    age_group = (
        "adult"
        if age is not None and age >= 18
        else "pediatric"
        if age is not None
        else "unknown"
    )
    for item in microbes.items:
        if not _microbe_relation_retrieval_applicable(item):
            continue
        all_names = tuple(
            dict.fromkeys((*_taxon_aliases(item.scientific_name), item.name))
        )
        microbe_queries.append(
            EvidenceQuery(
                query=item.scientific_name,
                supports=(item.microbe_id,),
                required_concepts=(all_names,),
                intent="microbe_discovery",
                target=f"{item.scientific_name} / {item.name}",
                channels=("guide", "literature"),
                age_group=age_group,
                query_language=_query_language(item.scientific_name),
                microbe_id=item.microbe_id,
                screen_profile="discovery",
            )
        )
    for item in diseases.items:
        # Keep unresolved provider-template rows for the source audit, but do
        # not spend retrieval/model budget on them or turn a catalogue disease
        # name into a patient risk.  A public row needs an actual model result
        # or a taxon explicitly bound to this patient result.
        if not disease_has_patient_microbiome_signal(item):
            continue
        disease_terms = _disease_terms(item.name)
        preferred_disease = _preferred_english_term(item.name)
        # The patient-facing column explains why a microbiome source listed
        # the condition; it is not a disease encyclopedia. Start with the one
        # human-gut association that can actually populate that column. A
        # general clinical query and a diagnostic-guide query are generated
        # later only for rows still needing them, avoiding three eager queries
        # for every source disease.
        microbiome_query = (
            f"{preferred_disease} human gut microbiome association"
        )
        disease_queries.append(
            EvidenceQuery(
                query=microbiome_query,
                supports=(item.disease_id,),
                required_concepts=(disease_terms,),
                intent="disease_clinical_significance",
                target=item.name,
                channels=("literature",),
                age_group=age_group,
                query_language=_query_language(microbiome_query),
                disease_id=item.disease_id,
                screen_profile="gut_association",
            )
        )
        for related_name in item.related_microbes:
            matched = _match_related_taxon(related_name, microbes)
            relation_matches = matched or [None]
            for matched_microbe in relation_matches:
                # Source tables can append a result status to the Chinese
                # name. Search the matched source identity, not that label.
                search_taxon = (
                    matched_microbe.scientific_name
                    if matched_microbe is not None
                    and re.search(r"[\u4e00-\u9fff]", related_name)
                    else _related_taxon_identity(related_name)
                )
                microbe_id = (
                    matched_microbe.microbe_id if matched_microbe is not None else None
                )
                relation_id = stable_id(
                    "relation",
                    f"source|{microbe_id or related_name}|{item.disease_id}",
                )
                supports = tuple(part for part in (item.disease_id, microbe_id) if part)
                # EviMed's literature channel is materially more reliable for
                # mixed Latin/Chinese medical queries than for an all-English
                # phrase.  Keep both source concepts explicit without adding a
                # mandatory stool/human-gut qualifier.
                query_text = " ".join(
                    dict.fromkeys(
                        (
                            search_taxon,
                            _preferred_english_term(item.name),
                            item.name,
                        )
                    )
                )
                disease_queries.append(
                    EvidenceQuery(
                        query=query_text,
                        supports=supports,
                        required_concepts=(
                            _scientific_terms(search_taxon),
                            disease_terms,
                        ),
                        intent="microbe_disease_association",
                        target=" / ".join(
                            dict.fromkeys(
                                part
                                for part in (
                                    search_taxon,
                                    matched_microbe.name if matched_microbe else None,
                                    item.name,
                                )
                                if part
                            )
                        ),
                        relation_disease_id=item.disease_id,
                        relation_disease_name=item.name,
                        relation_source_taxon=related_name,
                        matched_microbe_ids=(microbe_id,) if microbe_id else (),
                        channels=("literature",) if microbe_id else (),
                        age_group=age_group,
                        query_language=_query_language(query_text),
                        microbe_id=microbe_id,
                        disease_id=item.disease_id,
                        relation_id=relation_id,
                        relation_type="epidemiologic_association",
                        screen_profile=(
                            "gut_association"
                            if matched_microbe is not None
                            and matched_microbe.category
                            in {"beneficial_abnormal", "other_abnormal"}
                            else "human_relation"
                        ),
                    )
                )
    health_context = _priority_health_query_context(hospital_data or [])
    lifestyle_topics: list[
        tuple[
            str,
            str,
            tuple[str, ...],
            str,
            str,
            tuple[tuple[str, ...], ...] | None,
        ]
    ] = [
        (
            "diet",
            "健康成人膳食调护",
            ("diet", "dietary", "nutrition", "膳食", "饮食", "脂肪摄入"),
            "中国居民膳食指南 健康成人 膳食",
            "nutrition adults general population",
            None,
        ),
        (
            "exercise",
            "健康成人运动调护",
            ("exercise", "physical activity", "运动", "生活方式"),
            "成人 身体活动 指南",
            "physical activity adults general population",
            None,
        ),
        (
            "sleep",
            "健康成人睡眠调护",
            ("sleep", "sleep duration", "睡眠", "睡眠时长"),
            "healthy sleep adults consensus",
            "sleep health adults",
            None,
        ),
        (
            "bowel_habits",
            "健康成人排便习惯调护",
            (
                "bowel habit",
                "defecation",
                "stool frequency",
                "constipation",
                "排便习惯",
                "排便",
                "便意",
                "如厕",
            ),
            "bowel habits adults guideline",
            "bowel movement frequency adults",
            None,
        ),
    ]
    if health_context is not None:
        profile, target_label, condition_terms, guide_term, literature_term = (
            health_context
        )
        # Keep the query budget fixed: replace the two generic lifestyle topics
        # with patient-condition-specific ones instead of appending more work.
        # Sleep and bowel-habit evidence remain independent because those
        # recommendations require their own patient-side symptom target later.
        lifestyle_topics[0] = (
            "diet",
            f"{target_label}相关膳食调护",
            ("diet", "dietary", "nutrition", "膳食", "饮食", "营养"),
            f"成人 {guide_term} 膳食 生活方式 指南",
            f"{literature_term} adults dietary lifestyle intervention",
            (condition_terms,),
        )
        lifestyle_topics[1] = (
            "exercise",
            f"{target_label}相关运动调护",
            ("exercise", "physical activity", "运动", "身体活动", "生活方式"),
            f"成人 {guide_term} 运动 生活方式 指南",
            f"{literature_term} adults physical activity intervention",
            (condition_terms,),
        )
    for (
        topic,
        target,
        topic_terms,
        guide_query,
        literature_query,
        target_concepts,
    ) in lifestyle_topics:
        required_concepts = target_concepts or (
            ("healthy", "adult", "adults", "健康", "成人"),
        )
        health_queries.append(
            EvidenceQuery(
                query=guide_query,
                supports=("health-management",),
                required_concepts=(*required_concepts, topic_terms),
                intent="health_management",
                target=target,
                channels=("guide",),
                age_group=age_group,
                query_language=_query_language(guide_query),
                screen_profile="health",
            )
        )
        health_queries.append(
            EvidenceQuery(
                query=literature_query,
                supports=("health-management",),
                required_concepts=(*required_concepts, topic_terms),
                # This is the independent human-intervention evidence line.
                # The patient-facing recommendation validator treats it the
                # same as health-management evidence, but it remains a
                # separate audit intent and can never become a disease risk.
                intent="intervention",
                target=target,
                channels=("literature",),
                age_group=age_group,
                query_language="english",
                screen_profile="health",
            )
        )
    # Interleave the three patient-facing modules. Retrieval executes bounded
    # batches in list order, so the old module-by-module ordering could finish
    # every taxon query before the first disease or health query had started.
    # Round-robin scheduling guarantees early representation without changing
    # query content, eligibility gates or the external callback contract.
    queries: list[EvidenceQuery] = []
    for index in range(
        max(
            (len(microbe_queries), len(disease_queries), len(health_queries)),
            default=0,
        )
    ):
        for group in (microbe_queries, disease_queries, health_queries):
            if index < len(group):
                queries.append(group[index])
    return _dedupe_queries(queries)


def _priority_health_query_context(
    hospital_data: list[HospitalDataInput],
) -> tuple[str, str, tuple[str, ...], str, str] | None:
    """Select one bounded lifestyle context from explicit abnormal results.

    A test name alone is not enough: a normal cholesterol or glucose result
    must not turn a general recommendation into disease-specific advice.  The
    status supplied by the hospital result is therefore the public source of
    truth; numeric inference is intentionally avoided here.
    """

    abnormal_text = " ".join(
        " ".join(
            str(value)
            for value in (
                item.item_name,
                item.result_status_name,
                item.exam_result,
            )
            if value not in (None, "")
        ).casefold()
        for item in hospital_data
        if _hospital_result_is_explicitly_abnormal(item)
    )
    if not abnormal_text:
        return None
    profiles: tuple[
        tuple[str, str, tuple[str, ...], str, str, tuple[str, ...]], ...
    ] = (
        (
            "lipid",
            "血脂异常",
            (
                "dyslipidemia",
                "hypercholesterolemia",
                "cholesterol",
                "low-density lipoprotein",
                "ldl",
                "血脂异常",
                "高胆固醇",
                "胆固醇",
                "低密度脂蛋白",
            ),
            "血脂异常",
            "dyslipidemia",
            (
                "血脂",
                "低密度脂蛋白",
                "总胆固醇",
                "甘油三酯",
                "ldl",
                "cholesterol",
                "triglyceride",
            ),
        ),
        (
            "glucose",
            "血糖异常",
            (
                "hyperglycemia",
                "prediabetes",
                "blood glucose",
                "glycated hemoglobin",
                "hba1c",
                "血糖异常",
                "高血糖",
                "空腹血糖",
                "糖化血红蛋白",
            ),
            "血糖异常",
            "hyperglycemia",
            ("血糖", "糖化血红蛋白", "glucose", "hba1c"),
        ),
        (
            "blood_pressure",
            "血压异常",
            (
                "elevated blood pressure",
                "hypertension",
                "blood pressure",
                "血压异常",
                "高血压",
                "血压升高",
            ),
            "血压异常",
            "elevated blood pressure",
            ("血压", "高血压", "blood pressure", "hypertension"),
        ),
        (
            "weight",
            "体重与体成分异常",
            (
                "overweight",
                "obesity",
                "body mass index",
                "bmi",
                "超重",
                "肥胖",
                "体重异常",
                "体脂异常",
            ),
            "超重肥胖",
            "overweight obesity",
            ("体重", "体脂", "腰围", "bmi", "overweight", "obesity"),
        ),
    )
    for profile, label, concepts, guide_term, literature_term, markers in profiles:
        if any(marker in abnormal_text for marker in markers):
            return profile, label, concepts, guide_term, literature_term
    return None


def _hospital_result_is_explicitly_abnormal(item: HospitalDataInput) -> bool:
    status_name = (item.result_status_name or "").strip().casefold()
    if any(
        marker in status_name
        for marker in ("正常", "未见异常", "阴性", "normal", "negative")
    ):
        return False
    if any(
        marker in status_name
        for marker in (
            "偏高",
            "偏低",
            "升高",
            "降低",
            "超标",
            "异常",
            "阳性",
            "high",
            "low",
            "abnormal",
            "positive",
        )
    ):
        return True
    status = str(item.result_status).strip().casefold()
    return status in {"1", "2", "high", "low", "abnormal", "positive"}


def make_function_barrier_queries(
    parsed: Any,
    microbes: AbnormalMicrobesSection,
    *,
    age: int | None = None,
    expanded: bool = False,
    max_microbes: int = 4,
) -> list[EvidenceQuery]:
    """Build bounded taxon-function and taxon-barrier evidence queries.

    Only function rows actually present in the source report are used.  The
    query result can enrich a microbe's medical interpretation, but it has no
    disease binding and therefore cannot create a Section 2 row.
    """

    topics = _source_function_topics(getattr(parsed, "metrics", []))
    if not topics or not microbes.items:
        return []
    age_group = (
        "adult"
        if age is not None and age >= 18
        else "pediatric"
        if age is not None
        else "unknown"
    )
    ranked_microbes = sorted(
        microbes.items,
        key=lambda item: (
            item.category
            in {
                "pathogenic_signal",
                "opportunistic_signal",
                "fungi_pathogenic_signal",
            },
            any(
                marker in (item.source_finding.status or "")
                for marker in ("高", "低", "缺乏", "超标", "异常", "检出")
            ),
            item.scientific_name,
        ),
        reverse=True,
    )[: max(0, max_microbes)]

    queries: list[EvidenceQuery] = []
    for microbe in ranked_microbes:
        names = tuple(
            dict.fromkeys(
                (*_scientific_terms(microbe.scientific_name), microbe.name)
            )
        )
        for intent, code, display_name, english_terms, chinese_terms in topics:
            topic_terms = tuple(dict.fromkeys((*english_terms, *chinese_terms)))
            search_topic = (
                " ".join(chinese_terms[:2])
                if expanded
                else " ".join(english_terms[:2])
            )
            direction = _source_abundance_direction(microbe.source_finding.status)
            query_text = " ".join(
                part
                for part in (
                    microbe.scientific_name,
                    search_topic,
                    direction,
                    "human gut",
                )
                if part
            )
            topic_id = stable_id("evidence-topic", f"{intent}|{code}")
            queries.append(
                EvidenceQuery(
                    query=query_text,
                    supports=(microbe.microbe_id, topic_id),
                    required_concepts=(names, topic_terms),
                    intent=intent,
                    target=(
                        f"{microbe.scientific_name} / {microbe.name} / "
                        f"{display_name}"
                    ),
                    channels=("literature",),
                    age_group=age_group,
                    query_language=_query_language(query_text),
                    microbe_id=microbe.microbe_id,
                    screen_profile=intent,
                )
            )
    return _dedupe_queries(queries)


def uncovered_function_barrier_queries(
    queries: list[EvidenceQuery],
    records: list[EvidenceRecord],
) -> list[EvidenceQuery]:
    """Return topic queries without a directly interpretable retained source."""

    covered_scopes = {
        (intent, microbe_id, target)
        for record in records
        if record.interpretation is not None
        and record.interpretation.target_alignment == "direct"
        and record.interpretation.patient_applicability != "not_applicable"
        for intent in record.intents
        if intent in {"gut_function", "gut_barrier"}
        for microbe_id in record.microbe_ids
        for target in record.targets
    }
    return [
        query
        for query in queries
        if query.intent in {"gut_function", "gut_barrier"}
        and (query.intent, query.microbe_id or "", query.target)
        not in covered_scopes
    ]


def _source_function_topics(metrics: list[Any]) -> list[tuple[Any, ...]]:
    function_metrics = {
        "acetate",
        "propionate",
        "butyrate",
        "scfa",
        "bile_acid",
        "bile_acids",
    }
    barrier_metrics = {
        "gut_barrier",
        "anti_inflammatory",
        "gut_immunity",
        "calprotectin",
        "zonulin",
        "dao",
        "d_lactate",
        "endotoxin",
    }
    topics: list[tuple[Any, ...]] = []
    seen: set[str] = set()
    for metric in metrics:
        code = str(getattr(metric, "code", "") or "").casefold()
        status = str(getattr(metric, "status", "") or "")
        value = str(getattr(metric, "value", "") or "")
        if not code or code in seen or not _metric_is_functionally_relevant(status, value):
            continue
        if code in function_metrics:
            seen.add(code)
            if code in {"acetate", "propionate", "butyrate", "scfa"}:
                topics.append(
                    (
                        "gut_function",
                        code,
                        getattr(metric, "name", None) or "短链脂肪酸功能",
                        ("short chain fatty acid", "SCFA production", code),
                        ("短链脂肪酸", "合成能力"),
                    )
                )
            else:
                topics.append(
                    (
                        "gut_function",
                        code,
                        getattr(metric, "name", None) or "胆汁酸代谢",
                        ("bile acid metabolism", "bile acids"),
                        ("胆汁酸代谢",),
                    )
                )
        elif code in barrier_metrics:
            seen.add(code)
            if code == "calprotectin":
                english = ("fecal calprotectin", "intestinal inflammation")
                chinese = ("粪钙卫蛋白", "肠道炎症")
            elif code == "zonulin":
                english = ("zonulin", "intestinal permeability")
                chinese = ("连蛋白", "肠道通透性")
            elif code == "gut_immunity":
                english = ("mucosal immunity", "intestinal barrier")
                chinese = ("黏膜免疫", "肠道屏障")
            elif code == "dao":
                english = ("diamine oxidase", "intestinal permeability")
                chinese = ("二胺氧化酶", "肠道通透性")
            elif code == "d_lactate":
                english = ("D-lactate", "intestinal permeability")
                chinese = ("D-乳酸", "肠道通透性")
            elif code == "endotoxin":
                english = ("endotoxin LPS LBP", "intestinal permeability")
                chinese = ("内毒素", "肠道通透性")
            else:
                english = ("intestinal permeability", "tight junction")
                chinese = ("肠道通透性", "紧密连接")
            topics.append(
                (
                    "gut_barrier",
                    code,
                    getattr(metric, "name", None) or "肠道屏障功能",
                    english,
                    chinese,
                )
            )
    # One function and one barrier topic per selected microbe controls query
    # growth while keeping the two evidence lines independent.
    selected: list[tuple[Any, ...]] = []
    for intent in ("gut_function", "gut_barrier"):
        selected.extend([item for item in topics if item[0] == intent][:1])
    return selected


def _metric_is_functionally_relevant(status: str, value: str) -> bool:
    text = f"{status} {value}".strip()
    if not text:
        return False
    if any(marker in text for marker in ("弱", "低", "缺乏", "异常", "高", "超标")):
        return True
    # A source may provide a quantitative function score without a label.
    return bool(re.search(r"\d", text)) and "正常" not in text


def _source_abundance_direction(status: str) -> str:
    if any(marker in (status or "") for marker in ("低", "缺乏", "减少")):
        return "decreased abundance"
    if any(marker in (status or "") for marker in ("高", "超标", "增多")):
        return "increased abundance"
    return ""


_LOW_PATHOGEN_RELATION_SKIP_CATEGORIES = {
    "pathogenic_signal",
    "pathogenic_abnormal",
    "opportunistic_signal",
    "fungi_pathogenic_signal",
}


def _microbe_relation_retrieval_applicable(microbe: Any) -> bool:
    """Whether an exact disease-relation search can support this finding.

    Low abundance of a pathogen/opportunist cannot support the infection risks
    indexed for detection or high abundance. Skipping those relation searches
    both avoids misleading evidence and saves the slowest fallback requests.
    Beneficial taxa and detected/increased pathogens remain eligible.
    """

    direction = _source_abundance_direction(microbe.source_finding.status)
    return not (
        direction == "decreased abundance"
        and microbe.category in _LOW_PATHOGEN_RELATION_SKIP_CATEGORIES
    )


def make_candidate_queries(
    microbes: AbnormalMicrobesSection,
    records: list[EvidenceRecord],
    *,
    diseases: DiseaseRisksSection | None = None,
    age: int | None = None,
) -> list[EvidenceQuery]:
    """Create exact second-pass searches without creating public relations."""
    microbes_by_id = {item.microbe_id: item for item in microbes.items}
    age_group = (
        "adult"
        if age is not None and age >= 18
        else "pediatric"
        if age is not None
        else "unknown"
    )
    queries: list[EvidenceQuery] = []
    per_microbe: defaultdict[str, int] = defaultdict(int)

    def candidate_limit(_microbe: object) -> int:
        # The public report exposes at most three relations per source taxon.
        # Keep the expensive exact-verification budget aligned with that
        # business boundary for every taxon class.
        return 3

    # A discovery binding is only a candidate, not a substitute for the exact
    # second-pass verification query. Always schedule curated exact hints
    # first; later public-fallback code already restricts them to still-empty
    # taxa, while the per-taxon limit and source deduplication bound cost. This
    # also prevents a ``no significant association`` hit from suppressing a
    # more specific EviMed/PubMed search.
    seen_candidates: set[tuple[str, str]] = set()
    # Run exact, well-established disease-name searches before looser outcomes
    # extracted from taxon-only discovery results can consume the three-query
    # budget. A hint remains search input only: no public relation exists until
    # EviMed returns a matching source and the ordinary validation path accepts
    # the exact taxon-condition pair.
    for microbe in microbes.items:
        if not _microbe_relation_retrieval_applicable(microbe):
            continue
        for (
            condition_name,
            search_term,
            relation_type,
        ) in _TAXON_RELATION_SEARCH_HINTS.get(microbe.scientific_name.casefold(), ()):
            marker = (microbe.microbe_id, _normalize(condition_name))
            if marker in seen_candidates or per_microbe[
                microbe.microbe_id
            ] >= candidate_limit(microbe):
                continue
            seen_candidates.add(marker)
            per_microbe[microbe.microbe_id] += 1
            disease_id = stable_id("evidence-condition", condition_name)
            relation_id = stable_id(
                "relation", f"discovered|{microbe.microbe_id}|{disease_id}"
            )
            # When the curated term is already a complete exact-taxon query,
            # send it unchanged. Appending a translated condition materially
            # changes EviMed ranking (for example the useful generic
            # ``Salmonella enterica infection`` results are displaced by
            # serotype-only papers when ``沙门菌病`` is appended).
            # A curated search may intentionally use a historical scientific
            # name because that is how the article was indexed (for example,
            # ``Bacteroides vulgatus`` for ``Phocaeicola vulgatus``). Treat
            # any accepted synonym as a complete taxon query; prefixing both
            # names materially worsens EviMed/PubMed ranking.
            search_taxon_term = next(
                (
                    alias
                    for alias in _scientific_terms(microbe.scientific_name)
                    if _normalize(alias) in _normalize(search_term)
                ),
                None,
            )
            search_is_complete_taxon_query = search_taxon_term is not None
            # Keep an English exact-relation query free of the translated
            # Chinese display label. PubMed treats adjacent terms as AND, so
            # ``Campylobacter coli gastroenteritis 胃肠炎`` frequently
            # returns no result even though the English pair is well indexed.
            # The Chinese label remains in ``required_concepts`` for EviMed
            # screening and in the public relation binding.
            query_text = (
                search_term
                if search_is_complete_taxon_query
                else " ".join(
                    dict.fromkeys(
                        (
                            microbe.scientific_name,
                            search_term,
                            *(
                                ()
                                if re.search(r"[A-Za-z]", search_term)
                                else (condition_name,)
                            ),
                        )
                    )
                )
            )
            relation_search_term = (
                re.sub(
                    re.escape(search_taxon_term or microbe.scientific_name),
                    "",
                    search_term,
                    flags=re.IGNORECASE,
                ).strip()
                if search_is_complete_taxon_query
                else search_term
            )
            queries.append(
                EvidenceQuery(
                    query=query_text,
                    supports=(microbe.microbe_id,),
                    required_concepts=(
                        tuple(
                            dict.fromkeys(
                                (
                                    *_scientific_terms(microbe.scientific_name),
                                    microbe.name,
                                )
                            )
                        ),
                        tuple(
                            dict.fromkeys(
                                (
                                    condition_name,
                                    *_disease_terms(condition_name),
                                    relation_search_term or search_term,
                                )
                            )
                        ),
                    ),
                    intent="microbe_disease_association",
                    target=(
                        f"{microbe.scientific_name} / {microbe.name} / {condition_name}"
                    ),
                    relation_disease_id=disease_id,
                    relation_disease_name=condition_name,
                    relation_source_taxon=microbe.scientific_name,
                    matched_microbe_ids=(microbe.microbe_id,),
                    channels=("guide", "literature"),
                    age_group=age_group,
                    query_language=_query_language(query_text),
                    microbe_id=microbe.microbe_id,
                    disease_id=disease_id,
                    relation_id=relation_id,
                    relation_type=relation_type,
                    screen_profile=(
                        "gut_association"
                        if microbe.category
                        in {"beneficial_abnormal", "other_abnormal"}
                        and _source_abundance_direction(
                            microbe.source_finding.status
                        )
                        else "human_relation"
                    ),
                )
            )
    # Concrete outcome phrases found in EviMed titles/summaries must be
    # considered before generic model phrases such as ``<taxon> infection``
    # consume the bounded exact-query budget.
    ranked_records = sorted(
        records,
        key=lambda record: bool(source_derived_conditions(record)[0]),
        reverse=True,
    )
    for record in ranked_records:
        if not set(record.intents) & {
            "microbe_discovery",
            "microbe_clinical_significance",
            "microbe_association",
        }:
            continue
        interpretation = record.interpretation
        interpreted_conditions = []
        allow_interpreted_conditions = bool(
            interpretation is not None
            and interpretation.taxon_alignment not in {"strain", "toxin_gene"}
            and interpretation.target_alignment != "not_applicable"
            and interpretation.patient_applicability != "not_applicable"
        )
        if allow_interpreted_conditions and interpretation is not None:
            interpreted_conditions = list(interpretation.conditions)
        source_conditions, _source_claim = source_derived_conditions(record)
        candidates = [
            *((condition, True) for condition in source_conditions),
            *((condition, False) for condition in interpreted_conditions),
        ]
        if not candidates:
            continue
        source = f"{record.title} {record.summary or ''}"
        for microbe_id in record.microbe_ids:
            microbe = microbes_by_id.get(microbe_id)
            if microbe is None or not _microbe_relation_retrieval_applicable(
                microbe
            ):
                continue
            is_species = len(microbe.scientific_name.split()) >= 2
            for condition, is_source_seed in candidates:
                if is_source_seed:
                    # A source-derived term is a discovery hint only.  Require
                    # the same EviMed title/summary to name this exact taxon,
                    # then verify the relation through a separate query below.
                    if not source_mentions_taxon(
                        source,
                        microbe.scientific_name,
                        aliases=tuple(
                            dict.fromkeys(
                                (microbe.name, *_taxon_aliases(microbe.scientific_name))
                            )
                        ),
                    ):
                        continue
                elif interpretation is not None and (
                    (interpretation.taxon_alignment == "genus" and is_species)
                    or (interpretation.taxon_alignment == "species" and not is_species)
                ):
                    continue
                if condition.relation_type == "not_applicable":
                    continue
                # Search slots are bounded. Do not let an unmeasured
                # resistance subtype, a generic colonisation label or another
                # non-public condition consume the budget before a concrete
                # disease/infection candidate found later in EviMed results.
                if not _is_public_condition_name(condition.name):
                    continue
                marker = (microbe_id, _normalize(condition.name))
                if marker in seen_candidates or per_microbe[
                    microbe_id
                ] >= candidate_limit(microbe):
                    continue
                seen_candidates.add(marker)
                per_microbe[microbe_id] += 1
                disease_id = stable_id("evidence-condition", condition.name)
                relation_id = stable_id(
                    "relation", f"discovered|{microbe_id}|{disease_id}"
                )
                if condition.relation_type in {
                    "established_pathogen",
                    "opportunistic",
                    "colonization",
                }:
                    channels = ("guide", "literature")
                else:
                    channels = ("literature",)
                query_text = " ".join(
                    dict.fromkeys(
                        (
                            microbe.scientific_name,
                            condition.search_term,
                            condition.name,
                        )
                    )
                ).strip()
                queries.append(
                    EvidenceQuery(
                        query=query_text,
                        supports=(microbe_id,),
                        required_concepts=(
                            tuple(
                                dict.fromkeys(
                                    (
                                        *_scientific_terms(microbe.scientific_name),
                                        microbe.name,
                                    )
                                )
                            ),
                            tuple(
                                dict.fromkeys(
                                    (
                                        condition.name,
                                        *_disease_terms(condition.name),
                                        condition.search_term,
                                    )
                                )
                            ),
                        ),
                        intent="microbe_disease_association",
                        target=(
                            f"{microbe.scientific_name} / {microbe.name} / "
                            f"{condition.name}"
                        ),
                        relation_disease_id=disease_id,
                        relation_disease_name=condition.name,
                        relation_source_taxon=microbe.scientific_name,
                        matched_microbe_ids=(microbe_id,),
                        channels=channels,
                        age_group=age_group,
                        query_language=_query_language(query_text),
                        microbe_id=microbe_id,
                        disease_id=disease_id,
                        relation_id=relation_id,
                        relation_type=condition.relation_type,
                        screen_profile=(
                            "gut_association"
                            if microbe.category
                            in {"beneficial_abnormal", "other_abnormal"}
                            else "human_relation"
                        ),
                    )
                )
    # If a directionally abnormal beneficial/other taxon still has no three
    # evidence-derived candidates, use the highest source-report disease rows
    # as *search hypotheses*.  This does not create a public relation: the
    # exact taxon-disease pair must still be returned by EviMed and survive the
    # ordinary human, gut and semantic validation path.  It closes the prior
    # gap where a direct report containing both ``B. longum low`` and
    # ``constipation`` never issued the exact relation query.
    if diseases is not None:
        ranked_diseases = sorted(
            (
                disease
                for disease in diseases.items
                if disease.source_type != "clinical_context"
                and disease.item_type != "unclear"
                and disease.name
            ),
            key=lambda disease: (
                _source_score_number(disease.model_index),
                disease.name,
            ),
            reverse=True,
        )
        for microbe in microbes.items:
            if microbe.category not in {"beneficial_abnormal", "other_abnormal"}:
                continue
            for disease in ranked_diseases:
                if per_microbe[microbe.microbe_id] >= 3:
                    break
                marker = (microbe.microbe_id, _normalize(disease.name))
                if marker in seen_candidates:
                    continue
                seen_candidates.add(marker)
                per_microbe[microbe.microbe_id] += 1
                disease_id = disease.disease_id
                relation_id = stable_id(
                    "relation", f"source-candidate|{microbe.microbe_id}|{disease_id}"
                )
                disease_term = _preferred_english_term(disease.name)
                query_text = " ".join(
                    part
                    for part in (
                        microbe.scientific_name,
                        disease_term,
                        "abundance",
                        "adult" if age_group == "adult" else None,
                    )
                    if part
                )
                queries.append(
                    EvidenceQuery(
                        query=query_text,
                        supports=(microbe.microbe_id, disease_id),
                        required_concepts=(
                            tuple(
                                dict.fromkeys(
                                    (
                                        *_scientific_terms(microbe.scientific_name),
                                        microbe.name,
                                    )
                                )
                            ),
                            _disease_terms(disease.name),
                        ),
                        intent="microbe_disease_association",
                        target=(
                            f"{microbe.scientific_name} / {microbe.name} / "
                            f"{disease.name}"
                        ),
                        relation_disease_id=disease_id,
                        relation_disease_name=disease.name,
                        relation_source_taxon=microbe.scientific_name,
                        matched_microbe_ids=(microbe.microbe_id,),
                        channels=("literature",),
                        age_group=age_group,
                        query_language=_query_language(query_text),
                        microbe_id=microbe.microbe_id,
                        disease_id=disease_id,
                        relation_id=relation_id,
                        relation_type="epidemiologic_association",
                        screen_profile="gut_association",
                    )
                )
    return _dedupe_queries(queries)


def _source_score_number(value: str | None) -> float:
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else float("-inf")


def make_relation_fallback_queries(
    queries: list[EvidenceQuery],
    records: list[EvidenceRecord],
) -> list[EvidenceQuery]:
    """Retry a still-empty exact relation once without forcing a sample site."""
    covered_relations = {
        binding.relation_id
        for record in records
        if _record_can_suppress_relation_fallback(record)
        for binding in record.relation_bindings
    }
    fallback: list[EvidenceQuery] = []
    seen: set[str] = set()
    for query in queries:
        if (
            not query.relation_id
            or not query.microbe_id
            or query.relation_id in covered_relations
            or query.relation_id in seen
        ):
            continue
        seen.add(query.relation_id)
        target_components = [
            component.strip()
            for component in query.target.split("/")
            if component.strip()
        ]
        chinese_microbe = next(
            (
                component
                for component in target_components[:-1]
                if re.search(r"[\u3400-\u9fff]", component)
            ),
            None,
        )
        query_text = " ".join(
            part
            for part in (
                chinese_microbe or query.relation_source_taxon,
                query.relation_disease_name,
            )
            if part
        )
        fallback.append(
            EvidenceQuery(
                query=query_text,
                supports=query.supports,
                required_concepts=query.required_concepts,
                intent="microbe_disease_association",
                target=query.target,
                relation_disease_id=query.relation_disease_id,
                relation_disease_name=query.relation_disease_name,
                relation_source_taxon=query.relation_source_taxon,
                matched_microbe_ids=query.matched_microbe_ids,
                channels=("literature",),
                age_group=query.age_group,
                query_language=_query_language(query_text),
                microbe_id=query.microbe_id,
                disease_id=query.disease_id,
                relation_id=query.relation_id,
                relation_type=query.relation_type,
                # Preserve the stricter abundance-association profile used by
                # beneficial/other abnormal taxa. Broadening the language must
                # not turn a probiotic intervention paper into baseline-risk
                # evidence on the Chinese retry.
                screen_profile=query.screen_profile,
            )
        )
    return _dedupe_queries(fallback)


def make_contextual_relation_queries(
    queries: list[EvidenceQuery],
    records: list[EvidenceRecord],
    context: PatientEvidenceContext,
    *,
    max_queries: int = 4,
) -> list[EvidenceQuery]:
    """Add one bounded patient-context retry for high-value unresolved pairs.

    The context is used only to improve retrieval/applicability.  It never
    creates a relation, and missing context does not imply normality.  A
    single query combines at most the age band and one directly relevant
    domain term, avoiding a demographic/BMI/medication Cartesian product.
    """

    covered_relations = {
        binding.relation_id
        for record in records
        if _record_can_suppress_relation_fallback(record)
        for binding in record.relation_bindings
    }
    output: list[EvidenceQuery] = []
    seen: set[str] = set()
    for query in queries:
        if (
            len(output) >= max(0, max_queries)
            or query.intent != "microbe_disease_association"
            or not query.relation_id
            or query.relation_id in covered_relations
            or query.relation_id in seen
        ):
            continue
        disease_name = query.relation_disease_name or ""
        high_value = query.relation_type in {
            "established_pathogen",
            "opportunistic",
        } or _clinical_importance_score(disease_name) > 0
        if not high_value:
            continue

        terms: list[str] = []
        age_term = {
            "pediatric": "pediatric",
            "adult_18_44": "adult",
            "adult_45_64": "middle aged adult",
            "older_adult": "older adult",
        }.get(context.age_band)
        if age_term:
            terms.append(age_term)

        disease_key = _normalize(disease_name)
        if context.bmi_group in {"overweight", "obesity"} and any(
            marker in disease_key
            for marker in (
                "obesity",
                "肥胖",
                "diabetes",
                "糖尿病",
                "fattyliver",
                "脂肪肝",
                "cardiovascular",
                "心血管",
                "hyperlip",
                "高血脂",
            )
        ):
            terms.append("BMI")
        elif context.bowel_context and any(
            marker in disease_key
            for marker in (
                "constipation",
                "便秘",
                "diarrhea",
                "diarrhoea",
                "腹泻",
                "irritablebowel",
                "肠易激",
                "inflammatorybowel",
                "炎症性肠病",
            )
        ):
            terms.append("bowel symptoms")
        elif context.medications and any(
            marker in " ".join(context.medications).casefold()
            for marker in ("antibiotic", "抗生素", "抗菌药")
        ):
            terms.append("antibiotic exposure")

        if not terms:
            continue
        seen.add(query.relation_id)
        query_text = " ".join((query.query, *terms[:2]))
        output.append(
            EvidenceQuery(
                query=query_text,
                supports=query.supports,
                required_concepts=query.required_concepts,
                intent=query.intent,
                target=query.target,
                relation_disease_id=query.relation_disease_id,
                relation_disease_name=query.relation_disease_name,
                relation_source_taxon=query.relation_source_taxon,
                matched_microbe_ids=query.matched_microbe_ids,
                channels=("literature",),
                age_group=query.age_group,
                query_language=_query_language(query_text),
                microbe_id=query.microbe_id,
                disease_id=query.disease_id,
                relation_id=query.relation_id,
                relation_type=query.relation_type,
                screen_profile=query.screen_profile,
            )
        )
    return _dedupe_queries(output)


def make_coverage_queries(
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    records: list[EvidenceRecord],
    *,
    age: int | None = None,
    hospital_data: list[HospitalDataInput] | None = None,
) -> list[EvidenceQuery]:
    """Build one bounded retry for report rows left without retained evidence.

    The first pass deliberately uses several exact and contextual forms.  This
    retry is driven by the *retained* EviMed results rather than by a fixed
    second batch, so it runs only for a microbe, disease, or diagnostic path
    that is still empty after screening.  It broadens wording, never the source
    of evidence, and it does not manufacture a conclusion when EviMed remains
    empty.
    """
    age_group = (
        "adult"
        if age is not None and age >= 18
        else "pediatric"
        if age is not None
        else "unknown"
    )
    age_term = (
        " adult"
        if age_group == "adult"
        else " pediatric"
        if age_group == "pediatric"
        else ""
    )
    queries: list[EvidenceQuery] = []
    # A raw or merely retained hit is not coverage.  The retry decision is
    # based on evidence that survived structured interpretation and can
    # actually support a public row.  Otherwise an unrelated first-page hit
    # suppresses the useful Chinese/mixed-language fallback.
    microbes_by_id = {item.microbe_id: item for item in microbes.items}
    microbe_coverage: set[str] = set()
    for record in records:
        interpretation = record.interpretation
        if (
            interpretation is None
            or interpretation.target_alignment == "not_applicable"
            or interpretation.patient_applicability == "not_applicable"
            or interpretation.taxon_alignment not in {"species", "genus"}
            # The patient-facing relation column accepts only evidence from
            # stool or intestinal mucosa.  A blood/respiratory association is
            # still useful as contextual organism evidence, but it must not
            # mark a stool relation as covered and suppress the exact public
            # fallback that may retrieve the applicable human gut study.
            or interpretation.sample_alignment in {"blood", "respiratory", "other"}
            or _source_sample_alignment(record) in {"blood", "respiratory", "other"}
            or not set(record.intents)
            & {
                "microbe_discovery",
                "microbe_clinical_significance",
                "microbe_association",
                "microbe_disease_association",
            }
        ):
            continue
        for binding in record.relation_bindings:
            microbe = microbes_by_id.get(binding.microbe_id)
            if microbe is None or not _is_public_condition_name(binding.disease_name):
                continue
            description = _public_relation_description(
                record,
                microbe_name=microbe.name,
                disease_name=binding.disease_name,
                relation_type=binding.relation_type,
            )
            if description and _abundance_relation_matches_source_direction(
                item=microbe,
                record=record,
                description=description,
            ):
                microbe_coverage.add(binding.microbe_id)
    disease_gut_coverage = {
        disease_id
        for record in records
        if _record_has_human_gut_disease_association(record)
        for disease_id in record.disease_ids
    }
    from modules.report.clinical import record_has_displayable_evaluation

    disease_by_id = {item.disease_id: item for item in diseases.items}
    diagnostic_coverage: set[str] = set()
    for record in records:
        if is_preclinical_evidence(record):
            continue
        for disease_id in record.disease_ids:
            disease = disease_by_id.get(disease_id)
            if disease is not None and record_has_displayable_evaluation(
                disease,
                record,
                subject_age=age,
            ):
                diagnostic_coverage.add(disease_id)

    for item in microbes.items:
        if (
            item.microbe_id in microbe_coverage
            or not _microbe_relation_retrieval_applicable(item)
        ):
            continue
        all_names = tuple(
            dict.fromkeys((*_scientific_terms(item.scientific_name), item.name))
        )
        synonym_queries = [
            alias
            for alias in _scientific_terms(item.scientific_name)
            if alias.casefold() != item.scientific_name.casefold()
            and re.search(r"[A-Za-z]", alias)
        ]
        if item.category in {"beneficial_abnormal", "other_abnormal"}:
            status = item.source_finding.status
            direction_terms = (
                ("low abundance", "depleted")
                if any(marker in status for marker in ("低", "缺乏", "减少"))
                else ("high abundance", "increased")
                if any(marker in status for marker in ("高", "超标", "增多"))
                else ("abundance association",)
            )
            query_specs = [
                (
                    f"{taxon_name} {direction_term}",
                    "microbe_association",
                    "gut_association",
                )
                for taxon_name in dict.fromkeys(
                    (item.scientific_name, *synonym_queries)
                )
                for direction_term in direction_terms
            ]
            chinese_direction_terms = (
                ("低丰度", "缺乏")
                if any(marker in status for marker in ("低", "缺乏", "减少"))
                else ("高丰度", "丰度升高")
                if any(marker in status for marker in ("高", "超标", "增多"))
                else ("丰度关联",)
            )
            query_specs.extend(
                (
                    f"{item.name} {direction_term}",
                    "microbe_association",
                    "gut_association",
                )
                for direction_term in chinese_direction_terms
            )
            # This is a final broad fallback only for a row that still has no
            # validated relation. It is intentionally not appended to every
            # EviMed query: some species are indexed under a general human-
            # health review even when direction-specific wording returns no
            # literature. Public output still requires an exact species match,
            # a human gut association and a direction consistent with the
            # source report.
            query_specs.append(
                (
                    f"{item.scientific_name} human health",
                    "microbe_association",
                    "gut_association",
                )
            )
        else:
            query_specs = [
                (query_text, "microbe_clinical_significance", "discovery")
                for query_text in dict.fromkeys(
                    (
                        f"{item.scientific_name} infection disease",
                        f"{item.scientific_name} clinical infection",
                        f"{item.scientific_name} pathogen",
                        f"{item.name} 感染 疾病",
                        *(
                            f"{synonym} infection disease"
                            for synonym in synonym_queries
                        ),
                    )
                )
            ]
        for query_text, intent, screen_profile in query_specs:
            queries.append(
                EvidenceQuery(
                    query=query_text,
                    supports=(item.microbe_id,),
                    required_concepts=(all_names,),
                    intent=intent,
                    target=f"{item.scientific_name} / {item.name}",
                    channels=(
                        ("literature",)
                        if intent == "microbe_association"
                        else ("guide", "literature")
                    ),
                    age_group=age_group,
                    query_language=_query_language(query_text),
                    microbe_id=item.microbe_id,
                    screen_profile=screen_profile,
                )
            )

    for item in diseases.items:
        disease_terms = _disease_terms(item.name)
        preferred_disease = _preferred_english_term(item.name)
        preferred_chinese = _preferred_chinese_term(item.name)
        gut_association_query = (
            f'"{preferred_disease}"[Title/Abstract] AND '
            '("gut microbiome"[Title/Abstract] OR '
            '"gut microbiota"[Title/Abstract] OR '
            '"intestinal microbiome"[Title/Abstract] OR '
            '"intestinal microbiota"[Title/Abstract])'
        )
        if (
            item.source_type != "clinical_context"
            and item.disease_id not in disease_gut_coverage
        ):
            # This report needs to explain why the microbiome source listed
            # the condition, not repeat a disease encyclopedia entry. Keep one
            # exact human-gut retry and do not spend another network/model pass
            # on a generic overview that cannot support an individual row.
            queries.append(
                EvidenceQuery(
                    query=gut_association_query,
                    supports=(item.disease_id,),
                    required_concepts=(disease_terms,),
                    intent="disease_clinical_significance",
                    target=item.name,
                    channels=("literature",),
                    age_group=age_group,
                    query_language=_query_language(gut_association_query),
                    disease_id=item.disease_id,
                    screen_profile="gut_association",
                )
            )
        if (
            item.item_type != "unclear"
            and (
                item.source_type == "clinical_context"
                or item.disease_id in disease_gut_coverage
                or item.source_type == "evimed_association"
            )
            and item.disease_id not in diagnostic_coverage
        ):
            diagnostic_terms = _diagnostic_search_hints(item.name)
            if item.source_type == "clinical_context":
                # The summary needs a *new* follow-up examination. Keep the
                # same bounded query count, but prefer a diagnostic hint that
                # is not already represented by the uploaded clinical data.
                diagnostic_terms = tuple(
                    term
                    for _index, term in sorted(
                        enumerate(diagnostic_terms),
                        key=lambda pair: (
                            _diagnostic_hint_is_already_available(
                                pair[1],
                                item.existing_results,
                            ),
                            pair[0],
                        ),
                    )
                )
            diagnostic_queries = []
            if diagnostic_terms:
                # Keep each retrieval atomic. Combining three test names into
                # one query made EviMed require the first Chinese phrase while
                # ranking an English text block, causing valid PHQ-9, Bristol
                # and actigraphy guides to be rejected as missing concepts.
                for term in diagnostic_terms[:2]:
                    term_lowered = term.casefold()
                    # Some concrete hints intentionally use a better indexed
                    # disease synonym (for example infectious diarrhea for a
                    # Salmonella-linked food-poisoning row). Do not prepend a
                    # second disease phrase and push the relevant guide block
                    # out of EviMed's first page.
                    term_already_scoped = any(
                        alias.casefold() in term_lowered
                        for alias in disease_terms
                        if alias and alias != item.name
                    )
                    parts = [] if term_already_scoped else [preferred_disease]
                    parts.append(term)
                    if not any(
                        marker in term_lowered
                        for marker in (
                            "guideline",
                            "criteria",
                            "consensus",
                            "biomarker",
                        )
                    ):
                        parts.append("diagnostic guideline")
                    diagnostic_queries.append(" ".join(parts))
            else:
                diagnostic_queries.extend(
                    [
                        f"{preferred_chinese} 诊断 筛查 指南{age_term}",
                        f"{preferred_disease} diagnostic criteria screening guideline{age_term}",
                    ]
                )
            for query_text in dict.fromkeys(diagnostic_queries[:3]):
                queries.append(
                    EvidenceQuery(
                        query=query_text,
                        supports=(item.disease_id,),
                        # The exact test remains in the retrieval query and is
                        # rechecked against the retained source by the strict
                        # source-bound clinical matcher. Requiring its exact
                        # spelling here incorrectly rejected PHQ-9/PHQ9 and
                        # bilingual guide blocks before synonym matching.
                        required_concepts=(disease_terms,),
                        intent="diagnostic_guideline",
                        target=item.name,
                        channels=("guide", "literature"),
                        age_group=age_group,
                        query_language=_query_language(query_text),
                        disease_id=item.disease_id,
                        screen_profile="diagnostic",
                    )
                )
    health_context = _priority_health_query_context(hospital_data or [])
    if health_context is not None:
        _profile, target_label, condition_terms, guide_term, _literature_term = (
            health_context
        )
        directly_covered_health_targets = {
            target
            for record in records
            if record.category == "guideline"
            and record.interpretation is not None
            and record.interpretation.target_alignment == "direct"
            and record.interpretation.patient_applicability != "not_applicable"
            and set(record.intents) & {"health_management", "intervention"}
            for target in record.targets
        }
        for target, topic_terms, query_text in (
            (
                f"{target_label}相关膳食调护",
                ("diet", "dietary", "nutrition", "膳食", "饮食", "营养"),
                f"{guide_term} 饮食 饱和脂肪 生活方式 指南",
            ),
            (
                f"{target_label}相关运动调护",
                ("exercise", "physical activity", "运动", "身体活动", "生活方式"),
                f"{guide_term} 运动 身体活动 生活方式 指南",
            ),
        ):
            if target in directly_covered_health_targets:
                continue
            queries.append(
                EvidenceQuery(
                    query=query_text,
                    supports=("health-management",),
                    required_concepts=(condition_terms, topic_terms),
                    intent="health_management",
                    target=target,
                    channels=("guide",),
                    age_group=age_group,
                    query_language=_query_language(query_text),
                    screen_profile="health",
                )
            )
    if any(item.name == "便秘" for item in diseases.items):
        for query_text, channels in (
            ("constipation lifestyle bowel habits adult guideline", ("guide",)),
            ("成人 便秘 生活方式 排便习惯", ("guide", "literature")),
        ):
            queries.append(
                EvidenceQuery(
                    query=query_text,
                    supports=("health-management",),
                    required_concepts=(
                        ("constipation", "便秘"),
                        (
                            "bowel habit",
                            "defecation",
                            "lifestyle",
                            "排便习惯",
                            "排便",
                            "生活方式",
                        ),
                    ),
                    intent="health_management",
                    target="成人便秘相关排便调护",
                    channels=channels,
                    age_group=age_group,
                    query_language=_query_language(query_text),
                    screen_profile="health",
                )
            )
    return _dedupe_queries(queries)


def _diagnostic_hint_is_already_available(
    hint: str,
    existing_results: list[Any],
) -> bool:
    """Return whether a broad diagnostic hint is already in source data."""

    normalized_hint = re.sub(r"[\s（）()\-_/]", "", hint).casefold()
    existing = " ".join(
        f"{item.item_name or ''} {item.result or ''}" for item in existing_results
    ).casefold()
    existing_compact = re.sub(r"[\s（）()\-_/]", "", existing)
    families = (
        (("fibroscan", "瞬时弹性", "肝脏弹性"), ("fibroscan", "瞬时弹性", "肝脏弹性")),
        (("超声", "彩超", "ultrasound"), ("超声", "彩超", "ultrasound")),
        (("bmi", "体质量指数", "体重指数"), ("bmi", "体质量指数", "体重指数")),
        (("腰围",), ("腰围",)),
        (("血脂",), ("血脂", "胆固醇", "甘油三酯", "ldl", "hdl")),
        (("ldlc", "低密度脂蛋白"), ("ldlc", "低密度脂蛋白")),
        (("肝功能",), ("肝功能", "转氨酶", "alt", "ast")),
    )
    for hint_markers, existing_markers in families:
        if any(marker in normalized_hint for marker in hint_markers):
            return any(marker in existing_compact for marker in existing_markers)
    return bool(normalized_hint and normalized_hint in existing_compact)


def number_evidence(
    records: list[EvidenceRecord],
) -> tuple[list[EvidenceReference], dict[str, int]]:
    category_order = {
        "guideline": 0,
        "literature": 1,
        "drug_label": 2,
        "clinical_trial": 3,
    }
    ordered = sorted(
        records,
        key=lambda item: (
            category_order[item.category],
            -item.relevance,
            -(item.year or 0),
            item.title.casefold(),
            item.evidence_key,
        ),
    )
    numbers: dict[str, int] = {}
    references: list[EvidenceReference] = []
    for number, record in enumerate(ordered, start=1):
        numbers[record.evidence_key] = number
        references.append(
            EvidenceReference(
                number=number,
                provider=record.provider,
                category=record.category,
                title=record.title,
                source=record.source,
                year=record.year,
                summary=record.summary,
                authors=record.authors,
                study_types=record.study_types,
                url=record.url,
                doi=record.doi,
                intents=record.intents,
                interpretation=record.interpretation,
                targets=record.targets,
            )
        )
    return references, numbers


def attach_evidence_refs(
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    records: list[EvidenceRecord],
    numbers: dict[str, int],
) -> None:
    by_support: defaultdict[str, list[EvidenceRecord]] = defaultdict(list)
    for record in records:
        for support in record.supports:
            by_support[support].append(record)

    microbes_by_id = {item.microbe_id: item for item in microbes.items}
    general_microbe_refs: dict[str, list[int]] = {}
    for item in microbes.items:
        own = [
            record
            for record in records
            if item.microbe_id in record.microbe_ids
            or item.microbe_id in record.supports
        ]
        # Discovery hits remain in the audit. The patient-facing evidence cell
        # cites only records that support a displayed disease or adverse
        # outcome, never a paper that merely names the organism.
        item.evidence_refs = []
        item.clinical_role = _evidence_backed_microbe_role(own, microbe=item)
        item.interpretation_evidence_refs = []
        item.association_evidence_refs = []
        if item.clinical_role:
            general_microbe_refs[item.microbe_id] = _record_numbers(
                _evidence_backed_microbe_role_records(own),
                numbers,
            )
            item.interpretation_evidence_refs = general_microbe_refs[
                item.microbe_id
            ]

    for disease in diseases.items:
        relation_records = [
            record
            for record in records
            if any(
                binding.disease_id == disease.disease_id
                for binding in record.relation_bindings
            )
        ]
        # The public column is "why this microbiome result suggests attention",
        # not a disease encyclopedia.  Only a directly applicable human gut
        # association may populate association evidence.  A disease guideline
        # remains eligible for a separately validated examination but cannot
        # make a provider placeholder row look like an individual risk.
        association_records = [
            record
            for record in _dedupe(
                [*by_support[disease.disease_id], *relation_records]
            )
            if _record_has_human_gut_disease_association(record)
            and _public_disease_claim(record) is not None
        ]
        selected_association_records = sorted(
            association_records,
            key=_disease_record_priority_score,
            reverse=True,
        )[:1]
        confirmation_records = [
            record
            for record in by_support[disease.disease_id]
            if "diagnostic_guideline" in record.intents
        ]
        disease.association_evidence_refs = _record_numbers(
            selected_association_records, numbers
        )
        disease.confirmation_evidence_refs = _record_numbers(
            confirmation_records, numbers
        )
        disease.evidence_refs = sorted(
            set(disease.association_evidence_refs + disease.confirmation_evidence_refs)
        )

        def public_claims(source_records: list[EvidenceRecord]) -> list[str]:
            return [
                claim
                for record in sorted(
                    source_records,
                    key=_disease_record_priority_score,
                    reverse=True,
                )
                if (claim := _public_disease_claim(record)) is not None
            ]

        direct_claims = public_claims(selected_association_records)
        if direct_claims:
            disease.medical_interpretation = (
                "；".join(
                    dict.fromkeys(claim.rstrip("。") for claim in direct_claims[:2])
                )
                + "。"
            )
        matched_ids = {
            binding.microbe_id
            for record in relation_records
            for binding in record.relation_bindings
            if binding.disease_id == disease.disease_id
        }
        evidence_bound_microbes = [
            microbes_by_id[microbe_id].scientific_name
            for microbe_id in sorted(matched_ids)
            if microbe_id in microbes_by_id
        ]
        if disease.source_type == "source_report":
            disease.related_microbes = list(
                dict.fromkeys(
                    [*disease.related_microbes, *evidence_bound_microbes]
                )
            )
        else:
            disease.related_microbes = evidence_bound_microbes

    # A taxon table shows only evidence-backed disease/risk associations.  The
    # source taxon remains even when this list is empty.
    for item in microbes.items:
        grouped: defaultdict[str, list[tuple[EvidenceRelationType, EvidenceRecord]]] = (
            defaultdict(list)
        )
        for record in records:
            for binding in record.relation_bindings:
                if (
                    binding.microbe_id == item.microbe_id
                    and not (
                        item.category in {"beneficial_abnormal", "other_abnormal"}
                        and (
                            record.category == "clinical_trial"
                            or _is_abundance_intervention_evidence(record)
                        )
                    )
                    and not _condition_names_another_report_microbe(
                        binding.disease_name,
                        target_microbe_id=item.microbe_id,
                        microbes=tuple(microbes.items),
                    )
                    and binding.relation_type
                    in {
                        "established_pathogen",
                        "opportunistic",
                        "epidemiologic_association",
                    }
                ):
                    grouped[_canonical_condition_name(binding.disease_name)].append(
                        (binding.relation_type, record)
                    )
        risks: list[RiskAssociation] = []
        for disease_name, typed_records in grouped.items():
            if not _is_public_condition_name(disease_name):
                continue
            relation_type = min(
                (relation_type for relation_type, _record in typed_records),
                key=lambda value: _RELATION_TYPE_ORDER[value],
            )
            joint_records = _dedupe(
                [record for _relation_type, record in typed_records]
            )
            valid_descriptions: list[tuple[EvidenceRecord, str]] = []
            for candidate in sorted(
                joint_records,
                key=_record_priority_score,
                reverse=True,
            ):
                matching_condition_names = [
                    binding.disease_name
                    for binding in candidate.relation_bindings
                    if binding.microbe_id == item.microbe_id
                    and _canonical_condition_name(binding.disease_name) == disease_name
                ]
                candidate_description = next(
                    (
                        text
                        for condition_name in (
                            matching_condition_names or [disease_name]
                        )
                        if (
                            text := _public_relation_description(
                                candidate,
                                microbe_name=item.name,
                                disease_name=condition_name,
                                relation_type=relation_type,
                            )
                        )
                    ),
                    "",
                )
                if (
                    candidate_description
                    and _abundance_relation_matches_source_direction(
                        item=item,
                        record=candidate,
                        description=candidate_description,
                    )
                ):
                    valid_descriptions.append((candidate, candidate_description))
            if not valid_descriptions:
                # A name and citation number without a source-bounded medical
                # statement is not a patient-facing interpretation.
                continue
            best_record, description = valid_descriptions[0]
            joint_refs = _record_numbers(
                [record for record, _description in valid_descriptions],
                numbers,
            )
            if not joint_refs:
                continue
            risks.append(
                RiskAssociation(
                    name=disease_name,
                    description=description,
                    relation_type=relation_type,
                    evidence_refs=joint_refs,
                    priority_score=(
                        1000
                        - 35 * _RELATION_TYPE_ORDER[relation_type]
                        + _record_priority_score(best_record)
                        + _clinical_importance_score(disease_name)
                    ),
                    patient_applicability=(
                        best_record.interpretation.patient_applicability
                        if best_record.interpretation
                        else "unclear"
                    ),
                    sample_alignment=(
                        best_record.interpretation.sample_alignment
                        if best_record.interpretation
                        else "unclear"
                    ),
                )
            )
        specific_risks = [
            risk for risk in risks if not _is_generic_microbe_condition(risk.name, item)
        ]
        # Keep a generic organism-specific infection only when EviMed did not
        # yield any concrete disease or outcome. Once a specific result exists,
        # a circular row such as ``铜绿假单胞菌—铜绿假单胞菌感染`` adds no
        # patient-facing information and must not displace it.
        if specific_risks:
            risks = specific_risks
        item.disease_and_risks = sorted(
            risks,
            key=lambda risk: (
                -risk.priority_score,
                risk.name,
            ),
        )[:3]
        relation_refs = sorted(
            {number for risk in item.disease_and_risks for number in risk.evidence_refs}
        )
        item.association_evidence_refs = relation_refs
        item.evidence_refs = (
            relation_refs
            if relation_refs
            else general_microbe_refs.get(item.microbe_id, [])
        )


def _is_generic_microbe_condition(name: str, microbe: object) -> bool:
    """Identify tautological ``taxon infection`` labels for final ranking."""

    normalized = _normalize(name)
    if not normalized:
        return False
    aliases = tuple(
        dict.fromkeys(
            (
                getattr(microbe, "name", ""),
                getattr(microbe, "scientific_name", ""),
                *_taxon_aliases(getattr(microbe, "scientific_name", "")),
            )
        )
    )
    return any(
        normalized
        in {
            f"{_normalize(alias)}感染",
            f"{_normalize(alias)}infection",
        }
        for alias in aliases
        if alias
    )


def _condition_names_another_report_microbe(
    condition_name: str,
    *,
    target_microbe_id: str,
    microbes: tuple,
) -> bool:
    """Reject a condition label that actually names a different report taxon.

    Multi-organism titles can otherwise make a condition such as
    ``难治性铜绿假单胞菌感染`` appear under an unrelated organism
    mentioned in the same abstract.
    """
    normalized_condition = _normalize(condition_name)
    if not normalized_condition:
        return False
    for microbe in microbes:
        if microbe.microbe_id == target_microbe_id:
            continue
        aliases = (
            microbe.name,
            microbe.scientific_name,
            *_taxon_aliases(microbe.scientific_name),
        )
        if any(
            (normalized_alias := _normalize(alias))
            and len(normalized_alias) >= 4
            and normalized_alias in normalized_condition
            for alias in aliases
        ):
            return True
    return False


def _canonical_condition_name(name: str) -> str:
    return {
        "肠热症": "肠热病",
        "伤寒副伤寒": "肠热病",
        "人感染猪链球菌病": "猪链球菌病",
        "杆菌菌血症": "菌血症",
        "志贺菌病": "细菌性痢疾",
        "肺炎链球菌性疾病": "肺炎球菌病",
        "肺炎链球菌感染": "肺炎球菌病",
        "肺炎链球菌性肺炎": "肺炎",
        "细菌性脑膜炎": "脑膜炎",
        "伪膜性肠炎": "假膜性肠炎",
        "大肠癌": "结直肠癌",
        "沙门氏菌病": "沙门菌病",
    }.get(name.strip(), name.strip())


def _is_public_condition_name(name: str) -> bool:
    normalized = name.strip()
    return (
        bool(normalized)
        and normalized
        not in {
            "发热",
            "头痛",
            "多系统感染",
            "急性感染",
            "感染暴发",
            "人类感染",
            "非结核分枝杆菌感染",
            "鲍曼不动杆菌感染",
            "定植",
            "医院获得性感染",
            "院内感染",
            "医院感染",
        }
        and not any(
            marker in normalized
            for marker in (
                "人类病原真菌",
                "潜在人类病原体",
                "病原真菌",
                "病原体类型",
                "机会性感染",
                # The source report measures a taxon, not an antimicrobial-
                # resistance phenotype or virulence subtype. Such a relation may
                # guide a broader retry but must not become the public disease row.
                "耐药",
                "难治性",
                "耐甲氧西林",
                "耐碳青霉烯",
                "产超广谱",
                "复合群",
                # Population, animal-host and acquisition-setting qualifiers
                # are not disease names. They can remain in the citation text
                # but must not become a general patient-facing adverse effect.
                "仔猪",
                "动物疾病",
                "孕产妇",
                "妊娠期",
                "新生儿",
                "婴儿",
                "儿童",
                "老年人",
                "医院获得性",
            )
        )
        and not re.search(
            r"(?<![a-z])(?:dtr|mrsa|crab|cre)(?![a-z])", normalized.casefold()
        )
    )


def _record_priority_score(record: EvidenceRecord) -> float:
    interpretation = record.interpretation
    category = {
        "guideline": 50,
        "literature": 35,
        "clinical_trial": 30,
        "drug_label": 20,
    }.get(record.category, 0)
    confidence = {
        "high": 20,
        "moderate": 12,
        "low": 4,
        "unclear": 0,
    }.get(interpretation.confidence if interpretation else "unclear", 0)
    sample = {
        "stool": 18,
        "mucosa": 10,
        "unclear": 5,
        "other": 2,
        "blood": 1,
        "respiratory": 1,
    }.get(interpretation.sample_alignment if interpretation else "unclear", 0)
    applicability = {
        "direct": 18,
        "contextual": 8,
        "unclear": 2,
        "not_applicable": -100,
    }.get(
        interpretation.patient_applicability if interpretation else "unclear",
        0,
    )
    design = {
        "Meta分析": 30,
        "系统综述": 26,
        "随机对照试验": 22,
        "前瞻性队列研究": 20,
        "队列研究": 16,
        "病例对照研究": 14,
        "横断面研究": 10,
        "临床试验": 8,
        "病例报告": -28,
    }.get(interpretation.study_design if interpretation else None, 0)
    return (
        category + confidence + sample + applicability + design + record.relevance * 10
    )


def _disease_record_priority_score(record: EvidenceRecord) -> float:
    """Prefer a direct human gut association, then a readable definition."""

    claim = _public_disease_claim(record) or ""
    normalized = claim.strip()
    score = _record_priority_score(record)
    if _record_has_human_gut_disease_association(record):
        # A source-report disease row is patient-visible only when its
        # association evidence is relevant to the gut sample.  Rank that
        # evidence above a generic disease definition so retrieval and final
        # rendering apply the same standard.
        score += 260
    if re.search(r"(?:是一种|为一种|属于|主要包括)", normalized):
        score += 120
    if re.search(
        r"(?:临床表现|典型症状|主要表现|表现为|以[^,，]{1,50}为特征)",
        normalized,
    ):
        score += 60
    target_aliases = {
        alias
        for target in record.targets
        for component in target.split("/")
        for alias in _disease_terms(component.strip())
        if alias
    }
    if any(
        normalized.casefold().startswith(alias.casefold()) for alias in target_aliases
    ):
        score += 35
    if re.search(r"\d+(?:\.\d+)?\s*%|第\s*\d|每年约", normalized):
        score -= 45
    return score


def _record_has_human_gut_disease_association(record: EvidenceRecord) -> bool:
    interpretation = record.interpretation
    if interpretation is None or interpretation.target_alignment != "direct":
        return False
    if interpretation.patient_applicability not in {"direct", "contextual"}:
        return False
    # A systematic review can explicitly discuss the human gut microbiome
    # without declaring whether every included sample was stool or mucosa.
    # Treat that as contextual gut evidence; continue rejecting an explicitly
    # blood, respiratory or other-site source.
    if interpretation.sample_alignment in {"blood", "respiratory", "other"}:
        return False
    text = " ".join(
        part
        for part in (
            record.title,
            record.summary,
            interpretation.claim,
        )
        if part
    ).casefold()
    return (
        not is_preclinical_evidence(record)
        and _source_focuses_target_disease(record)
        and any(marker in text for marker in _HUMAN_EVIDENCE_MARKERS)
        and any(marker in text for marker in _MICROBIOME_MARKERS)
        and any(marker in text for marker in _ASSOCIATION_MARKERS)
    )


def _record_has_general_disease_explanation(record: EvidenceRecord) -> bool:
    """Return whether a hit is strong enough to suppress a coverage retry.

    A narrow cohort result can be valid evidence yet still be a poor medical
    explanation for the disease row.  It remains eligible for the audit, but
    it must not prevent a second EviMed/public search for a definition,
    characteristic clinical feature, or generally applicable risk statement.
    """

    claim = (_public_disease_claim(record) or "").strip().rstrip("。！？!?")
    if not claim or claim.endswith(("中", "内", "方面")):
        return False
    return bool(
        re.search(
            r"(?:是一种|为一种|定义为|属于|表现为|主要表现|临床表现|"
            r"以[^，。；]{1,60}为特征|特征包括|症状包括|"
            r"危险因素包括|风险因素|常见|可影响|分为|分类为)",
            claim,
        )
    )


def _clinical_importance_score(name: str) -> float:
    severe_markers = (
        "癌",
        "感染",
        "败血",
        "脑膜炎",
        "肺炎",
        "中毒",
        "肝炎",
        "肾病",
        "糖尿病",
    )
    return 20 if any(marker in name for marker in severe_markers) else 0


def _public_relation_description(
    record: EvidenceRecord,
    *,
    microbe_name: str,
    disease_name: str,
    relation_type: EvidenceRelationType,
) -> str:
    interpretation = record.interpretation
    if interpretation is None:
        return ""
    if _relation_source_has_non_generalizable_context(record):
        return ""
    canonical_disease_name = _canonical_condition_name(disease_name)
    binding_claims = [
        binding.claim
        for binding in record.relation_bindings
        if _canonical_condition_name(binding.disease_name) == canonical_disease_name
        and binding.claim
    ]
    claims = [*binding_claims, interpretation.claim]
    source_context = " ".join(
        part
        for part in (
            record.title,
            record.summary or "",
            *claims,
        )
        if part
    ).casefold()
    if any(
        marker in source_context
        for marker in (
            "false-negative",
            "false negative",
            "negative test",
            "negative aspergillus precipitin",
            "试验阴性",
            "检测阴性",
            "假阴性",
        )
    ):
        # Test performance is a diagnostic-method outcome, not evidence that
        # the detected taxon generally causes the named disease.
        return ""
    if relation_type == "epidemiologic_association":
        protective_relation = any(
            marker in source_context
            for marker in (
                "helping to prevent",
                "reduce the risk",
                "reduction in the risk",
                "lower risk",
                "risk reduction",
                "风险降低",
                "降低风险",
                "有助于预防",
            )
        )
        absent_or_low = any(
            marker in source_context
            for marker in (
                "absence",
                "absent",
                "cannot be detected",
                "low levels",
                "lower levels",
                "decreased levels",
                "reduced abundance",
                "lower abundance",
                "缺乏",
                "未检出",
                "低水平",
                "丰度降低",
            )
        )
        if protective_relation and absent_or_low:
            return f"{microbe_name}丰度降低可能与{canonical_disease_name}风险增加相关。"
    microbe_aliases = tuple(
        dict.fromkeys(
            (
                microbe_name,
                *(
                    binding.microbe_name
                    for binding in record.relation_bindings
                    if _canonical_condition_name(binding.disease_name)
                    == canonical_disease_name
                ),
                *(
                    alias
                    for binding in record.relation_bindings
                    if _canonical_condition_name(binding.disease_name)
                    == canonical_disease_name
                    for alias in _taxon_aliases(binding.microbe_name)
                ),
            )
        )
    )
    if canonical_disease_name == _canonical_condition_name("结核病") and any(
        ("分枝杆菌" in alias or "mycobacterium" in alias.casefold())
        and "结核分枝杆菌" not in alias
        and "mycobacteriumtuberculosis" not in _normalize(alias)
        for alias in microbe_aliases
    ):
        # Non-tuberculous mycobacteria can resemble tuberculosis clinically,
        # but that does not make tuberculosis the disease caused by the
        # detected NTM species.
        return ""
    disallowed_clauses = (
        "建议",
        "治疗",
        "用药",
        "检查",
        "检测",
        "PCR",
        "检验",
        "防控",
        "规程",
        "噬菌体",
        "生物标志物",
        "筛查",
        "诊断",
        "诊治",
        "检索",
        "筛选",
        "规则",
        "模板",
        "AI",
        "JSON",
        "耐药",
        "resistan",
        "抗体水平",
        "启动子",
        "ON方向",
        "特定基因型",
        "编码基因",
        "毒力基因",
        "毒力蛋白",
        "黏附因子",
        "毒素基因",
        "肠毒素",
        "特定菌株",
        "菌株",
        "变种",
        "高毒力",
        "产毒株",
        "产毒型",
        "血清型",
        "亚种",
        "耐甲氧西林",
        "亚型",
        "strain",
        "serovar",
        "virulence gene",
        "toxin gene",
        "cdta",
        "cdtb",
        "cdtc",
        "基因编码",
        "基因",
        "同源基因",
        "酶水解",
        "酯酶",
        "对羟基苯甲酸酯",
        "分离株",
        "预后相似",
        "感染率",
        "患病率",
        "降钙素原",
        "预后",
        "严重程度",
        "病死率",
        "死亡率",
        "首要致病原",
        "最常见病原",
        "超过",
        "试验",
        "阳性",
        "阴性",
        "医疗器械",
        "留置",
        "药物敏感",
        "抗真菌药物",
        "荚膜表达",
        "培养条件",
        "疾病危害",
        "预防",
        "改善代谢",
        "改善炎症",
        "减轻炎症",
        "缓解炎症",
        "增强肠道屏障",
        "维持微生物群",
        "治疗作用",
        "保护作用",
        "干预效果",
        "干预后",
        "逆转",
        "改善",
        "保护",
        "有益作用",
        "调节肠道屏障",
        "降低炎症",
        "改善胰岛素敏感性",
        "内毒素入血",
        "米酵菌酸",
        "benefit",
        "protective effect",
        "粪便中可检出",
        "本摘要",
        "报告两批",
        "分为4群",
        "遗传变异",
        "遗传多样性",
        "genetic diversity",
        "免疫抑制剂",
        "潜伏感染再激活",
    )
    for claim in claims:
        for sentence in re.split(r"(?<=[。！？!?])\s*|[；;]", claim):
            sentence = re.sub(
                r"^(?:该|本)?(?:综述|研究|文献|指南|共识)"
                r"(?:明确)?(?:描述了|指出|表明|发现|强调)",
                "",
                sentence,
            )
            sentence = re.sub(
                r"^.{0,30}(?:诊疗方案|诊疗指南)(?:明确)?",
                "",
                sentence,
            )
            sentence = re.sub(r"^.*》(?:包括|涵盖)", "", sentence)
            sentence = sentence.replace("强调其", microbe_name)
            sentence = sentence.replace("，表明", "，")
            sentence = re.sub(r"^表明", "", sentence)
            sentence = re.sub(r"[，,]并总结了[^。；;]*", "", sentence)
            safe_clauses = [
                " ".join(clause.split()).strip()
                for clause in re.split(r"[，,]", sentence)
                if clause.strip()
                and not re.search(r"(?<![A-Za-z0-9])\d+[A-Za-z]+型", clause)
                and _normalize(clause) != _normalize(disease_name)
                and not any(
                    marker.casefold() in clause.casefold()
                    for marker in disallowed_clauses
                )
            ]
            raw_disease_clauses = [
                clause
                for clause in safe_clauses
                if _claim_names_disease(clause, disease_name)
            ]
            has_safe_microbe_context = any(
                any(
                    (alias_key := _normalize(alias)) and alias_key in _normalize(clause)
                    for alias in microbe_aliases
                )
                for clause in safe_clauses
            )
            disease_clauses = [
                (
                    f"{microbe_name}{clause}"
                    if clause.startswith(("与", "和", "并与", "可致", "可引起"))
                    and has_safe_microbe_context
                    and not any(
                        (alias_key := _normalize(alias))
                        and alias_key in _normalize(clause)
                        for alias in microbe_aliases
                    )
                    else clause
                )
                for clause in raw_disease_clauses
            ]
            context_clauses = [
                clause
                for clause in safe_clauses
                if clause not in raw_disease_clauses
                and (
                    (
                        any(
                            (alias_key := _normalize(alias))
                            and alias_key in _normalize(clause)
                            for alias in microbe_aliases
                        )
                        and any(
                            marker in clause
                            for marker in (
                                "致病性",
                                "机会致病菌",
                                "条件致病菌",
                                "病原属性",
                                "病原相关",
                            )
                        )
                    )
                    or any(
                        marker in clause
                        for marker in (
                            "高水平",
                            "低水平",
                            "丰度增加",
                            "丰度升高",
                            "丰度降低",
                            "丰度减少",
                        )
                    )
                )
            ]
            normalized = "，".join(
                dict.fromkeys([*context_clauses, *disease_clauses])
            ).strip()
            # A comparison can mention the requested organism only as the
            # comparator while the actual claim is about another taxon.  For
            # example, "Mycoplasma pneumoniae is more prevalent than
            # Streptococcus pneumoniae" must not become the latter organism's
            # public pneumonia description.
            if any(
                re.search(
                    rf"(?:高于|低于|相比|区别于)[^\u3002；;]{{0,24}}"
                    rf"{re.escape(alias)}",
                    normalized,
                    flags=re.IGNORECASE,
                )
                for alias in microbe_aliases
                if alias
            ):
                continue
            normalized = normalized.replace("人类类丹毒", "类丹毒")
            condition_only_as_population = bool(disease_clauses) and all(
                not any(
                    (alias_key := _normalize(alias)) and alias_key in _normalize(clause)
                    for alias in microbe_aliases
                )
                and bool(
                    re.match(
                        r"^(?:尤其|特别|主要|多见|常见|在)",
                        clause,
                    )
                )
                for clause in disease_clauses
            )
            if (
                not normalized
                or normalized.startswith(
                    (
                        "包括",
                        "涵盖",
                        "涉及",
                        "该指南",
                        "本指南",
                        "指南",
                        "文献讨论",
                        "证据涉及",
                        "可能导致疾病危害",
                    )
                )
                or condition_only_as_population
                or not re.search(r"[\u3400-\u9fff]", normalized)
                or not _claim_names_disease(normalized, disease_name)
                or not any(
                    (alias_key := _normalize(alias))
                    and alias_key in _normalize(normalized)
                    for alias in microbe_aliases
                )
                or not any(
                    marker in normalized
                    for marker in (
                        "引起",
                        "导致",
                        "所致",
                        "病因",
                        "病原",
                        "致病",
                        "相关",
                        "关联",
                        "风险",
                        "富集",
                        "增加",
                        "减少",
                        "可出现",
                        "感染",
                        "associated with",
                        "association with",
                        "causes",
                        "cause of",
                        "pathogen",
                    )
                )
            ):
                continue
            # Entity co-occurrence plus a relation keyword is not sufficient
            # when the model returned only a trailing locative fragment (for
            # example ``...血流感染中``). The exact source fallback below can
            # still provide cautious wording when the relation is verifiable.
            if normalized.rstrip("。！？!?").endswith(
                ("中", "内", "方面", "患者中", "人群中", "病例中")
            ):
                continue
            condition_scope = {
                _canonical_condition_name(binding.disease_name)
                for binding in record.relation_bindings
                if binding.microbe_id
                and any(
                    (alias_key := _normalize(alias))
                    and alias_key in _normalize(binding.microbe_name)
                    for alias in microbe_aliases
                )
            }
            if len(condition_scope) > 1:
                if any(marker in normalized for marker in ("病原", "致病")):
                    return f"{microbe_name}是{disease_name}的病原体。"
                if any(marker in normalized for marker in ("引起", "导致", "所致")):
                    return f"{microbe_name}可引起{disease_name}。"
                if any(marker in normalized for marker in ("相关", "关联")):
                    return f"{microbe_name}与{disease_name}相关。"
                if any(
                    marker in normalized
                    for marker in (
                        "显著减少",
                        "水平更低",
                        "显著增加",
                        "水平更高",
                    )
                ):
                    direction = (
                        "显著减少"
                        if any(
                            marker in normalized for marker in ("显著减少", "水平更低")
                        )
                        else "显著增加"
                    )
                    return f"{microbe_name}在{disease_name}中丰度{direction}。"
            # The Salmonella umbrella query may be interpreted with its two
            # named clinical forms (enteric fever and gastroenteritis).  When
            # the same EviMed abstract explicitly says ``salmonellosis``, keep
            # the umbrella row but make its text match the displayed disease
            # name instead of repeating the two specific rows underneath it.
            if (
                relation_type == "established_pathogen"
                and canonical_disease_name == "沙门菌病"
                and re.search(
                    r"(?<![a-z])salmonellosis(?![a-z])",
                    f"{record.title} {record.summary or ''}".casefold(),
                )
            ):
                return f"{microbe_name}可引起沙门菌病。"
            if (
                relation_type == "opportunistic"
                and any(marker in normalized for marker in ("可致", "可引起"))
                and "机会致病菌" in normalized
            ):
                return f"{microbe_name}作为机会致病菌，可引起{disease_name}。"
            if relation_type == "epidemiologic_association":
                normalized = re.sub(
                    r"中(显著(?:减少|增加))",
                    r"中丰度\1",
                    normalized,
                )
                if any(
                    marker in normalized
                    for marker in ("病原", "致病", "引起", "导致", "可致")
                ):
                    # An abundance/cohort association cannot be rewritten as
                    # pathogen causation merely because a model used stronger
                    # wording.  The source remains useful, but the public
                    # statement must preserve the relation type.
                    return f"{microbe_name}与{disease_name}相关。"
                normalized = re.sub(
                    r"中水平(更低|更高)",
                    r"中丰度\1",
                    normalized,
                )
            if "等并发症" in normalized and any(
                marker in normalized for marker in ("相关", "关联")
            ):
                return f"{microbe_name}与{disease_name}相关。"
            return normalized.rstrip("。！？!?") + "。"
    # The relationship name and citation remain visible.  If the model could
    # not produce a clean sentence but the EviMed title/abstract itself binds
    # the exact relation, reduce that source phrase to one clinical statement.
    matching_bindings = [
        binding
        for binding in record.relation_bindings
        if _canonical_condition_name(binding.disease_name) == canonical_disease_name
        and _source_explicitly_supports_relation(record, binding)
    ]
    source_text = f"{record.title} {record.summary or ''}".casefold()
    title_text = record.title.casefold()
    hard_source_fallback_blocked = any(
        marker in source_text
        for marker in (
            "免疫抑制剂",
            "潜伏感染再激活",
            "服药依从性",
            "prosthetic valve",
            "sternal wound",
            "after balloon",
            "postoperative",
            "postsurgical",
            "post-procedure",
            "人工瓣膜",
            "伤口感染",
        )
    )
    action_only_title = any(
        marker in title_text
        for marker in (
            "treatment",
            "therapy",
            "management",
            "diagnosis",
            "diagnostic",
            "detection",
            "screening",
            "治疗",
            "诊断",
            "检测",
            "筛查",
        )
    )
    if matching_bindings and not hard_source_fallback_blocked:
        # Never upgrade an association to causation merely because another
        # clause in the same abstract contains ``引起``.  Causal/pathogen
        # wording must occur in a validated binding claim that names this exact
        # taxon and this exact disease; otherwise use the cautious association
        # wording.
        exact_binding_claims = [
            binding.claim
            for binding in matching_bindings
            if binding.claim
            and source_mentions_taxon(
                binding.claim,
                binding.microbe_name,
                aliases=tuple(binding.microbe_aliases),
            )
            and _claim_names_disease(binding.claim, disease_name)
            and not any(
                marker.casefold() in binding.claim.casefold()
                for marker in disallowed_clauses
            )
            and not re.search(
                r"(?<![A-Za-z0-9])\d+[A-Za-z]+型",
                binding.claim,
                flags=re.IGNORECASE,
            )
        ]
        causal_markers = ("caused by", "causes ", "引起", "导致", "可致")
        if relation_type in {"established_pathogen", "opportunistic"} and (
            any(
                marker in claim.casefold()
                for claim in exact_binding_claims
                for marker in causal_markers
            )
            or any(
                _source_clause_has_relation_marker(
                    record,
                    binding,
                    disease_name=disease_name,
                    markers=causal_markers,
                )
                for binding in matching_bindings
            )
        ):
            return f"{microbe_name}可引起{disease_name}。"
        pathogen_markers = ("pathogen", "causative", "病原体", "致病菌")
        if relation_type in {"established_pathogen", "opportunistic"} and (
            any(
                marker in claim.casefold()
                for claim in exact_binding_claims
                for marker in pathogen_markers
            )
            or any(
                _source_clause_has_relation_marker(
                    record,
                    binding,
                    disease_name=disease_name,
                    markers=pathogen_markers,
                )
                for binding in matching_bindings
            )
        ):
            return f"{microbe_name}是{disease_name}的病原体。"
        if any(
            marker in claim.casefold()
            for claim in exact_binding_claims
            for marker in (
                "associated with",
                "association",
                "correlated with",
                "相关",
                "关联",
            )
        ):
            return f"{microbe_name}与{disease_name}相关。"
        title_names_relation = source_mentions_taxon(
            record.title,
            matching_bindings[0].microbe_name,
            aliases=tuple(matching_bindings[0].microbe_aliases),
        ) and _claim_names_disease(record.title, disease_name)
        if title_names_relation and relation_type in {
            "established_pathogen",
            "opportunistic",
        }:
            return f"{microbe_name}与{disease_name}相关。"
        # An organism-specific infection testing/diagnosis consensus can
        # establish the named organism-disease relationship even though its
        # purpose is diagnostic rather than etiologic.  Keep the public wording
        # deliberately associative: such a source does not by itself justify
        # saying that a stool signal caused, or confirms, the disease.  Requiring
        # the exact taxon plus ``感染`` in the title prevents an unrelated
        # diagnostic paper (for example, an NTM antigen used in tuberculosis
        # serology) from inheriting the disease relation.
        organism_specific_infection_title = (
            source_mentions_taxon(
                record.title,
                matching_bindings[0].microbe_name,
                aliases=tuple(matching_bindings[0].microbe_aliases),
            )
            and any(
                marker in record.title.casefold()
                for marker in (" infection", "infection ", "感染")
            )
            and _claim_names_disease(
                f"{record.title} {record.summary or ''}",
                disease_name,
            )
        )
        if organism_specific_infection_title and relation_type in {
            "established_pathogen",
            "opportunistic",
        }:
            return f"{microbe_name}与{disease_name}相关。"
        if action_only_title:
            return ""
        if relation_type == "epidemiologic_association":
            # A source can state a directional abundance comparison without
            # using the literal word "association". Require the validated
            # model direction and exact source taxon/outcome window to agree.
            direction = interpretation.direction
            if direction in {"increased", "decreased"} and any(
                _source_relation_direction(record, binding) == direction
                for binding in matching_bindings
            ):
                change = "降低" if direction == "decreased" else "升高"
                return f"{microbe_name}在{disease_name}人群中丰度{change}。"
        if relation_type in {"established_pathogen", "opportunistic"} and any(
            marker in source_text for marker in ("pathogen", "病原体", "致病菌")
        ):
            return f"{microbe_name}是{disease_name}的病原体。"
        if any(
            marker in source_text
            for marker in (
                "associated with",
                "association",
                "correlated with",
                "相关",
                "关联",
            )
        ):
            return f"{microbe_name}与{disease_name}相关。"
    # Without an explicit source relation, leave the prose empty instead of
    # inserting a fixed medical conclusion.
    return ""


def _relation_source_has_non_generalizable_context(record: EvidenceRecord) -> bool:
    """Reject one-patient and highly procedure-specific organism relations.

    These sources can be valid clinical observations, but they do not justify
    presenting the same outcome as a general consequence of a stool finding.
    """

    if is_likely_single_case(record):
        return True
    text = f"{record.title} {record.summary or ''}".casefold()
    procedure_markers = (
        "免疫抑制剂",
        "潜伏感染再激活",
        "prosthetic valve",
        "sternal wound",
        "after balloon",
        "postoperative",
        "postsurgical",
        "post-procedure",
        "post cardiac surgery",
        "artificial valve",
        "人工瓣膜",
        "胸骨伤口",
        "术后伤口",
        "伤口感染",
        "mdr acinetobacter baumannii",
        "multidrug-resistant acinetobacter baumannii",
        "pneumonia patients in icu",
        "多重耐药鲍曼不动杆菌",
        "hematologic malignan",
        "hematological malignan",
        "hematologic disease",
        "hematological disease",
        "high-risk hematology",
        "high risk hematology",
        "高危血液病患者",
        "血液病患者",
    )
    if any(marker in text for marker in procedure_markers):
        return True
    narrow_taxon_markers = (
        "异烟肼耐药",
        "耐药结核",
        "isoniazid-resistant",
        "isoniazid resistant",
        "hr-tb",
        "h r-t b",
        "对h耐药",
        "对 h耐药",
        "复合醋酸钙不动杆菌",
        "醋酸钙不动杆菌复合体",
        "acinetobacter baumannii/calcoaceticus complex",
        "acinetobacter calcoaceticus var.",
        "formerly acinetobacter calcoaceticus",
    )
    if not any(marker in text for marker in narrow_taxon_markers):
        return False
    # A narrow title does not invalidate a separate abstract sentence that
    # explicitly states the general exact-species relation.  This exception is
    # source bounded: the clean sentence must name both the taxon and outcome
    # and use causal/pathogen wording.  Subtype-only text therefore stays out.
    for sentence in re.split(r"(?<=[.!?。！？])\s*|[；;]", record.summary or ""):
        lowered = sentence.casefold()
        if not sentence or any(marker in lowered for marker in narrow_taxon_markers):
            continue
        if not any(
            source_mentions_taxon(
                sentence,
                binding.microbe_name,
                aliases=tuple(binding.microbe_aliases),
            )
            and _claim_names_disease(sentence, binding.disease_name)
            and any(
                marker in lowered
                for marker in (
                    "causes ",
                    "caused by",
                    "causative",
                    "pathogen",
                    "引起",
                    "导致",
                    "病原体",
                    "致病菌",
                )
            )
            for binding in record.relation_bindings
        ):
            continue
        return False
    return True


def _abundance_relation_matches_source_direction(
    *,
    item,
    record: EvidenceRecord,
    description: str,
) -> bool:
    """Keep abundance associations only when they match this report's direction.

    A paper reporting that a beneficial species is depleted in a disease does
    not explain a result where the same species is above its reference range.
    Pathogen detection is handled through pathogen relations and is therefore
    intentionally outside this abundance-direction rule.
    """
    status = item.source_finding.status.casefold()
    interpretation = record.interpretation
    direction = interpretation.direction if interpretation is not None else "not_stated"
    text = description.casefold()
    low_markers = (
        "丰度降低",
        "丰度减少",
        "低丰度",
        "减少的丰度",
        "缺乏",
        "耗竭",
        "decreased abundance",
        "lower abundance",
        "low abundance",
        "depleted",
        "显著减少",
        "水平更低",
    )
    high_markers = (
        "丰度增加",
        "丰度升高",
        "高丰度",
        "丰度富集",
        "increased abundance",
        "higher abundance",
        "high abundance",
        "enriched",
        "overabundance",
        "over-abundance",
        "高水平",
        "水平升高",
        "显著增加",
        "水平更高",
    )
    source_is_low = any(marker in status for marker in ("低", "缺乏", "减少"))
    source_is_high = any(marker in status for marker in ("高", "超标", "超出", "增多"))
    if item.category not in {"beneficial_abnormal", "other_abnormal"}:
        return not (
            not source_is_high
            and not source_is_low
            and any(marker in text for marker in (*low_markers, *high_markers))
        )
    source_text = " ".join(
        part
        for part in (
            record.title,
            record.summary or "",
            interpretation.claim if interpretation is not None else "",
        )
        if part
    ).casefold()
    source_has_low = any(marker in source_text for marker in low_markers)
    source_has_high = any(marker in source_text for marker in high_markers)
    # The report sentence is intentionally normalized and can omit the exact
    # English direction phrase even though the same retained abstract says
    # ``depleted`` or ``increased abundance``.  Treat the model's structured
    # direction as authoritative when available; otherwise accept a direction
    # only when the source text has one unambiguous side.
    if direction == "decreased":
        relation_is_low, relation_is_high = True, False
    elif direction == "increased":
        relation_is_low, relation_is_high = False, True
    elif direction == "mixed":
        relation_is_low, relation_is_high = False, False
    else:
        relation_is_low = (
            any(marker in text for marker in low_markers)
            or (source_has_low and not source_has_high)
        )
        relation_is_high = (
            any(marker in text for marker in high_markers)
            or (source_has_high and not source_has_low)
        )
    if source_is_high:
        if relation_is_low:
            return False
        return relation_is_high
    if source_is_low:
        if relation_is_high:
            return False
        if relation_is_low:
            return True
        # Some human studies express the same inverse relationship from the
        # protective side: presence/colonisation of the taxon is associated
        # with lower disease risk, while the abstract separately reports that
        # absence or lower levels are the adverse direction. Keep that source-
        # bounded relation for a low source result without rewriting it as
        # causal harm.
        protective_risk = any(
            marker in source_text
            for marker in (
                "reduce the risk",
                "reduction in the risk",
                "lower risk",
                "risk reduction",
                "风险降低",
                "降低风险",
            )
        )
        adverse_absence = any(
            marker in source_text
            for marker in (
                "absence",
                "absent",
                "low levels",
                "lower levels",
                "decreased levels",
                "reduced abundance",
                "lower abundance",
                "缺乏",
                "未检出",
                "低水平",
                "丰度降低",
            )
        )
        return protective_risk and adverse_absence
    return True


def _contains_disease_outcome_marker(value: str) -> bool:
    """Match English outcome words as words, not substrings such as First/risk."""
    lowered = value.casefold()
    return any(
        (
            marker.isascii()
            and bool(
                re.search(
                    rf"(?<![a-z]){re.escape(marker)}(?![a-z])",
                    lowered,
                )
            )
        )
        or (not marker.isascii() and marker in lowered)
        for marker in _DISEASE_OUTCOME_MARKERS
    )


def _public_disease_claim(record: EvidenceRecord) -> str | None:
    interpretation = record.interpretation
    if interpretation is None or interpretation.target_alignment != "direct":
        return None
    if not _source_focuses_target_disease(record):
        return None
    claim = _remove_disease_action_clauses(interpretation.claim.strip())
    target_aliases = {
        _normalize(alias)
        for target in record.targets
        for component in target.split("/")
        for alias in _disease_terms(component.strip())
        if alias.strip()
    }
    # A human gut-association result answers the public column directly even
    # though it is intentionally not a disease definition.  The older
    # `_safe_disease_sentences` gate was built for definitions and discarded
    # these patient-relevant association statements, which then forced every
    # row back to a generic template.
    if _record_has_human_gut_disease_association(record) and claim:
        normalized_claim = _normalize(claim)
        lowered_claim = claim.casefold()
        if (
            (not target_aliases or any(alias in normalized_claim for alias in target_aliases))
            and any(marker in lowered_claim for marker in _MICROBIOME_MARKERS)
            and any(marker in lowered_claim for marker in _ASSOCIATION_MARKERS)
            and not _claim_is_action_or_boundary_text(claim)
        ):
            return claim if claim[-1:] in "。！？!?" else claim + "。"
    if claim:
        normalized_claim = _normalize(claim)
        if not target_aliases or any(
            alias in normalized_claim for alias in target_aliases
        ):
            safe_sentences = _safe_disease_sentences(claim, record=record)
            # A treatment/diagnostic title does not invalidate a separate,
            # source-bounded sentence that clearly describes the target
            # disease.  The sentence itself still passes all public medical
            # gates below; only the raw-source fallback remains blocked for an
            # action-only title.
            if safe_sentences:
                return "".join(safe_sentences[:2])

    # EviMed may return a clinically useful source abstract while the
    # structured model response merely repeats that the paper is a guideline.
    # In that case keep only an exact, Chinese source sentence that names the
    # requested disease and passes the same public medical filters.  This is a
    # source-bounded fallback, not a generated conclusion.
    source_sentences = _safe_disease_sentences(record.summary, record=record)
    for sentence in source_sentences:
        normalized_sentence = _normalize(sentence)
        if not target_aliases or any(
            alias in normalized_sentence for alias in target_aliases
        ):
            return sentence
    return None


def _source_focuses_target_disease(record: EvidenceRecord) -> bool:
    """Require the retrieved source, not only the model claim, to target disease.

    A long block can mention the requested disease incidentally while mainly
    discussing another condition.  A model can then produce fluent target-led
    prose that is not the source's clinical conclusion.  Accept a disease
    explanation only when the target is named in the source title or begins a
    source sentence/paragraph as the grammatical subject.
    """

    aliases = tuple(
        dict.fromkeys(
            alias.casefold()
            for target in record.targets
            for component in target.split("/")
            for alias in _disease_terms(component.strip())
            if alias.strip()
        )
    )
    if not aliases:
        return True
    title = record.title.casefold()
    if any(
        alias in title
        and not _alias_names_hyphenated_subtype(title, alias)
        for alias in aliases
    ):
        return True
    source = "\n".join(
        part for part in (record.summary or "", record.evidence_text or "") if part
    )
    allowed_prefixes = (
        "acute ",
        "chronic ",
        "primary ",
        "systemic ",
        "adult ",
        "pediatric ",
        "paediatric ",
        "急性",
        "慢性",
        "原发性",
        "系统性",
        "成人",
        "儿童",
    )
    for sentence in re.split(r"(?<=[.!?。！？；;])\s*|[\r\n]+", source):
        lowered = sentence.casefold().lstrip(" ，,：:()\uff08\uff09")
        for alias in aliases:
            position = lowered.find(alias)
            if position == 0 and not _alias_names_hyphenated_subtype(
                lowered, alias
            ):
                return True
            if 0 < position <= 12 and lowered[:position].strip().startswith(
                allowed_prefixes
            ) and not _alias_names_hyphenated_subtype(lowered, alias):
                return True
    return False


def _alias_names_hyphenated_subtype(text: str, alias: str) -> bool:
    """Return true when an alias occurrence names only a special subtype.

    Sources headed by phrases such as ``帕金森-高热综合征`` concern a
    narrower complication and cannot, by title co-occurrence alone, support a
    general explanation of Parkinson disease.  A separate non-subtype source
    sentence can still pass ``_source_focuses_target_disease``.
    """

    position = text.find(alias)
    matched = False
    while position >= 0:
        matched = True
        suffix = text[position + len(alias) :].lstrip()
        if re.match(
            r"^[-—–－]\s*(?:an?\s+)?(?:overview|review|guideline|consensus)\b",
            suffix,
            re.IGNORECASE,
        ):
            return False
        if not re.match(
            r"^[-—–－]\s*[a-z\u3400-\u9fff]", suffix, re.IGNORECASE
        ):
            return False
        position = text.find(alias, position + len(alias))
    return matched


def _safe_disease_sentences(value: str | None, *, record: EvidenceRecord) -> list[str]:
    if not value or not value.strip():
        return []
    safe: list[str] = []
    for sentence in re.split(r"(?<=[。！？!?])\s*|[\r\n]+|[；;]", value):
        sentence = _clean_disease_sentence_format(sentence)
        source_bounded = _remove_disease_action_clauses(sentence)
        if not source_bounded:
            continue
        # These phrases describe a document or the existence of a definition,
        # not the disease itself.  Reject the entire sentence before clause
        # cleanup so a generic fragment such as "是一种慢性疾病" cannot survive
        # and outrank a genuinely informative source sentence.
        if any(
            marker in source_bounded.casefold()
            for marker in ("已有明确描述", "实用临床定义")
        ):
            continue
        cleaned = _remove_nonclinical_disease_clauses(
            source_bounded,
            record=record,
        )
        cleaned = _clean_disease_sentence_format(cleaned)
        if (
            cleaned
            and re.search(r"[\u3400-\u9fff]", cleaned)
            and not cleaned.startswith(
                (
                    "包括区分",
                    "以期",
                    "旨在",
                    "强调",
                    "该指南",
                    "本指南",
                    "为推动",
                    "为促进",
                    "为提高",
                    "因此",
                    "尤其是",
                    "而且",
                    "这些反映",
                    "采用Logistic",
                    "结果 ",
                    "结论:",
                    "结论：",
                    "国内外各种权威机构",
                )
            )
            and not _claim_is_action_or_boundary_text(cleaned)
            and _disease_sentence_has_public_clinical_meaning(cleaned, record=record)
            and _disease_claim_has_substance(cleaned, record=record)
            and _disease_claim_is_relevant_to_target(cleaned, record=record)
            and not _disease_claim_uses_other_patient_population(cleaned, record=record)
        ):
            safe.append(cleaned)
    return safe


def _clean_disease_sentence_format(value: str) -> str:
    cleaned = " ".join(value.split()).strip()
    cleaned = re.sub(r"^[*#\s]+", "", cleaned)
    cleaned = re.sub(r"^\d{4}年\d{1,2}月[,，]?\s*", "", cleaned)
    cleaned = re.sub(
        r"^[（(][^()（）]{1,24}[）)]\s*(?:声明|statement)\s*\d*[：:]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\[(?:\d+(?:[-,–]\d+)*)\]", "", cleaned)
    cleaned = re.sub(
        r"^委员会将([^,，。；;]{2,30})定义为",
        r"\1是",
        cleaned,
    )
    if cleaned.count("(") != cleaned.count(")"):
        return ""
    if cleaned.count("（") != cleaned.count("）"):
        return ""
    if cleaned.rstrip("。！？!?").endswith(
        (
            "疾病管",
            "管理的",
            "诊疗的",
            "以及",
            "并且",
            "和",
            "患者中",
            "人群中",
            "受试者中",
        )
    ):
        return ""
    return cleaned.strip()


def _disease_sentence_has_public_clinical_meaning(
    value: str, *, record: EvidenceRecord
) -> bool:
    """Reject research, editorial and narrow-cohort prose from the disease row."""

    lowered = value.casefold()
    disallowed = (
        "erratum",
        "勘误",
        "摘要表明",
        "摘要显示",
        "医务人员",
        "认识不足",
        "忽视或漏诊",
        "忽视",
        "漏诊",
        "专家经过多次讨论",
        "根据各自的经验",
        "科学声明",
        "文章主要包括",
        "本文主要包括",
        "分期处理规约",
        "生存质量有所改善",
        "手术风险",
        "严重危害人民健康",
        "威胁人民健康",
        "诊疗标准",
        "诊疗体系",
        "循证决策",
        "病变分割",
        "分割算法",
        "自动算法",
        "预测价值不如",
        "诊断价值",
        "有作为诊断标志物的潜力",
        "血清irisin",
        "nesfatin-1",
        "ykl-40",
        "mir-",
        "mrna水平",
        "pten",
        "th22",
        "il-22",
        "ncf1c",
        "视觉诱发的癫痫",
        "视觉诱发性",
        "光敏性癫痫",
        "闪光刺激",
        "致痫条纹",
        "免疫性癫痫相关概念",
        "对其进行更一步深入的研究",
        "通过系统检索",
        "形成推荐意见",
        "研究对象分为",
        "临床专家",
        "头脑风暴法",
        "数据库包括",
        "中国每年约",
        "种族差异",
        "我国居民生命健康",
        "升肝健脾汤",
        "联合检测",
        "世界各国专家",
        "临床上缺乏解释症状",
        "改进肝癌风险人群高危预警",
        "提出建议",
        "检测技术",
        "结果解读",
        "发挥其应用效能",
        "临床管理范围",
        "需要管理的疾病",
        "研究认为",
        "多种慢性病",
        "最常见的慢性病之一",
        "免疫抑制剂使用",
        "便秘严重程度量表等工具可用",
        "结肠传输时间和肛门直肠测压",
        "长期幸存者中",
        "以了解长期肿瘤学",
        "足部问题",
        "以指导临床",
        "多基因风险评分",
        "强调了中西医结合",
        "突出临床实用导向",
        "手术切除标本",
        "病理科标本",
        "睡眠状况与焦虑症状相关",
        "t、n、m",
        "t,n,m",
        "淋巴结分站",
        "转移模式的记录",
        "分类依据肿瘤特征",
        "m6a",
        "rna甲基化",
        "调控因子",
        "风险预测模型",
        "与健康对照无显著差异",
        "血清降钙素原",
        "焦虑症患者睡眠状况",
        "焦虑症患者的睡眠状况",
        "生活节奏紧张",
        "威胁身体健康",
        "发展为严重抑郁症",
        "常转移到腹腔",
        "肿瘤致死病因",
        "第2位",
        "第5位",
        "已有明确描述",
        "实用临床定义",
        "对患者的心理",
        "心理需求",
        "内容涉及一般原则",
        "澳大利亚心血管疾病",
        "发病高峰年龄",
        "男女比例",
        "术后并发症",
        "bormann iii",
        "bormann iv",
        "住院时间延长",
        "中国常见的恶性肿瘤",
        "每年约有50%",
        "痰瘀互结证",
        "中医证候",
        "常被低估",
        "心电图异常",
        "患者心理",
        "抑郁、焦虑症状及睡眠问题",
        "无症状平均风险成人",
        "并称“四高”",
        '并称"四高"',
        "中足受累",
        "步行功能残疾",
        "血清a-ccp",
        "rf、crp",
        "水平异常升高",
        # Narrow treatment cohorts, historical rankings and molecular-only
        # findings are valid research results but are not general medical
        # explanations for a patient-facing disease row.
        "住院患者",
        "类固醇应答",
        "激素应答",
        "in-hospital",
        "hospitalized patients",
        "steroid response",
        "20世纪",
        "美国最常见的死亡原因",
        "西方第三大",
        "第三大常见",
        "third most common",
        "commonest cause of death",
        "遗传变异",
        "基因变异",
        "祖先人群",
        "whole-genome",
        "genetic variant",
        "ancestry",
        "这些疾病",
        "上述疾病",
    )
    if any(marker in lowered for marker in disallowed):
        return False
    # A generic mechanism label without any concrete disease feature is not a
    # patient-facing explanation.
    core = value.strip().rstrip("。！？!?")
    if re.match(
        r"^肝硬化腹水",
        core,
    ) or re.match(
        r"^高脂血症在[^,，。；;]{1,30}人群中",
        core,
    ):
        return False
    if re.fullmatch(
        r"[^,，。；;]{2,40}(?:发病机制|病理机制)"
        r"(?:(?:十分)?复杂|不清|未明|尚不明确|尚未明确)",
        core,
    ):
        return False
    return not (
        any(
            _canonical_condition_name(target) == "腹泻型肠易激综合征"
            for target in record.targets
        )
        and "慢性水样腹泻" in value
        and "腹痛" not in value
    )


def _disease_claim_is_relevant_to_target(value: str, *, record: EvidenceRecord) -> bool:
    """Reject editorial, negative and off-target disease prose."""
    normalized = value.casefold()
    if re.search(r"不是[^.。；;]{0,80}风险因素|与[^.。；;]{0,40}无关", normalized):
        return False
    target_aliases = {
        _normalize(alias)
        for target in record.targets
        for component in target.split("/")
        for alias in _disease_terms(component.strip())
        if alias.strip()
    }
    if not _disease_claim_starts_with_target(value, record=record):
        return False
    # A different abnormality used as the grammatical subject is a risk-factor
    # statement about that abnormality, not a medical explanation of the target
    # disease itself (for example thyroid dysfunction -> coronary disease).
    match = re.match(r"^([^,，。；;]{2,45}异常)与", value)
    if match:
        subject = _normalize(match.group(1))
        if not any(alias in subject for alias in target_aliases):
            return False
    target_components = {
        component.strip()
        for target in record.targets
        for component in target.split("/")
    }
    for other_name in _DISEASE_ALIASES:
        other_aliases = {
            _normalize(alias)
            for alias in (other_name, *_DISEASE_ALIASES.get(other_name, ()))
            if alias.strip()
        }
        if (
            other_name not in target_components
            and not (other_aliases & target_aliases)
            and not any(
                target.startswith(other_name) or other_name.startswith(target)
                for target in target_components
            )
            and value.startswith(other_name)
            and any(alias in _normalize(value) for alias in target_aliases)
        ):
            return False
    return True


def _disease_claim_starts_with_target(value: str, *, record: EvidenceRecord) -> bool:
    """Require the report disease, rather than a tests/exposure, as subject."""

    lowered = value.casefold().lstrip(" ，,：:")
    aliases = tuple(
        dict.fromkeys(
            alias
            for target in record.targets
            for component in target.split("/")
            for alias in _disease_terms(component.strip())
            if alias
        )
    )
    if not aliases:
        return True
    allowed_prefixes = (
        "急性",
        "慢性",
        "功能性",
        "原发性",
        "继发性",
        "系统性",
        "广泛性",
        "自身免疫性",
        "成人",
        "儿童",
        "2型",
        "ii型",
        "夜间",
    )
    for alias in aliases:
        alias_lower = alias.casefold()
        position = lowered.find(alias_lower)
        if position == 0:
            return True
        if position < 0 or position > 8:
            continue
        prefix = lowered[:position].strip(" ，,：:()（）")
        if prefix in allowed_prefixes:
            return True
    return "感染与" in lowered and "风险相关" in lowered


def _disease_claim_uses_other_patient_population(
    value: str, *, record: EvidenceRecord
) -> bool:
    targets = {
        component.strip()
        for target in record.targets
        for component in target.split("/")
        if component.strip() and re.search(r"[\u3400-\u9fff]", component)
    }
    target_aliases = {
        _normalize(alias)
        for target in targets
        for alias in _disease_terms(target)
        if alias.strip()
    }
    for match in re.finditer(r"在([^，。；;]{2,30})患者(?:中|的)", value):
        cohort = _normalize(match.group(1))
        if cohort and not any(alias in cohort for alias in target_aliases):
            return True
    for target in targets:
        match = re.search(
            rf"([^，。；;]{{2,36}})患者(?:中|的)"
            rf"[^，。；;]{{0,180}}{re.escape(target)}",
            value,
        )
        if match and target not in match.group(1):
            return True
    return False


def _remove_nonclinical_disease_clauses(
    value: str,
    *,
    record: EvidenceRecord,
) -> str:
    """Remove population, editorial and special-context prose from a row.

    The disease table explains the condition and clinically relevant effects;
    it is not a place for rankings, service statistics or a complication-only
    cohort that happened to include people with the disease.
    """
    value = re.sub(
        r"[,，]?其发生发展与[^。；;]{0,100}(?:社会环境因素|社会因素)[^。；;]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:其)?病理机制(?:的)?认识"
        r"(?:仍)?(?:在)?(?:持续|不断|日益)?深入[^。；;]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:并)?(?:已)?被列为[^，,。；;]{0,100}(?:第四高|四高)"
        r"[^，,。；;]*",
        "",
        value,
    )
    value = re.sub(
        r"[，,](?:并)?与高血压[，,]高血脂[，,]高血糖"
        r"并称[“\"]?四高[”\"]?[^。；;]*",
        "",
        value,
    )
    excluded = (
        "发病率",
        "患病率",
        "死亡率",
        "识别率",
        "全球",
        "世界卫生组织",
        "第二大",
        "十大",
        "排名",
        "经济负担",
        "疾病负担",
        "医疗资源",
        "文化差异",
        "基层医生",
        "早期发现",
        "改善预后",
        "静脉导管",
        "导管相关血流感染",
        "腹部脂肪定量",
        "circrna",
        "差异表达",
        "联合服用维生素d和钙",
        "当前目标",
        "目标疾病",
        "新疆地区",
        "西北燥证",
        "特殊病证",
        "占我国",
        "发病例数",
        "死亡的主要原因",
        "严重危害人民健康",
        "检测质量",
        "了解其机制",
        "该文献摘要支持",
        "本文摘要支持",
        "敌意与",
        "社会支持与",
        "社会支持",
        "网络支持",
        "家族性心脏病高危人群",
        "该文件",
        "官方建议",
        "摘要未包含",
        "摘要未提供",
        "指导临床实践",
        "强调其",
        "不典型临床表现",
        "蛋白质摄入",
        "工作压力",
        "轮班工作",
        "社会隔离",
        "该证据总结为",
        "该研究分析了",
        "本研究分析了",
        "中性粒细胞明胶酶",
        "脂质运载蛋白",
        "ckd273",
        "风险评分系统",
        "妊娠期患者",
        "孕妇",
        "预后分析",
        "covid",
        "阴燃相关恶化",
        "科学声明阐明",
        "阐明了",
        "发作性高血压",
        "检出与影像学方法相关",
        "分类第三版",
        "分类第",
        "螺旋ctap",
        "门脉受侵",
        "高发趋势",
        "死亡和复发风险",
        "不典型的临床表现",
        "西方国家常见",
        "第4位",
        "5年生存率",
        "临床应用",
        "aena",
        "该规约",
        "规约提供",
        "处理的标准系统",
        "家族遗传性胃癌",
        "特定基因变异",
        "分度标准",
        "量化",
        "临床决策",
        "防控工作",
        "精神心理障碍",
        "机体免疫力低下",
        "该评介",
        "重症克罗恩病",
        "预后不佳",
        "人口老龄化",
        "社会经济差异",
        "体重管理",
        "生活方式改变",
        "预防和缓解",
        "社会决定因素",
        "贫困社区",
        "esm-1",
        "myo",
        "不稳定型心绞痛",
        "mace",
        "sudep",
        "突然意外死亡",
        "慢性未控制癫痫",
        "分类标准",
        "新标准",
        "早期特征",
        "晚期甲状腺癌",
        "检测算法",
        "分子标志物",
        "现代生活节奏",
        "心理因素疾病",
        "疑病",
        "发展为抑郁症",
        "自杀",
        "学历",
        "抗肠神经元抗体",
        "维生素d",
        "尚无定论",
        "专家意见",
        "侵入性操作",
        "出血风险分级",
        "女性＞",
        "女性>",
        "并区分无症状",
        "基于滑膜炎",
        "心理因素相关",
        "引发抑郁症",
        "科学声明阐述",
        "运动试验",
        "运动员",
        "高血压反应",
        "致动脉硬化指数",
        "aip",
        "结直肠恶性肿瘤",
        "acr适当性标准",
        "分期和随访",
        "pt1b",
        "淋巴结转移率",
        "脉管癌栓",
        "肿瘤出芽",
        "欧洲和美国",
        "约100万患者",
        "近年来克罗恩病增加",
        "规范管理并发症",
        "中医体质",
        "晚发和极晚发",
        "互为因果",
        "食养方式",
        "辅助预防和控制",
        "γ-谷氨酰",
        "中性粒细胞与淋巴细胞",
        "血小板与淋巴细胞",
        "分类系统",
        "标准化的分期方法",
        "病理机制认识不断深入",
        "肠道菌群",
        "手术切除标本",
        "病理科标本",
        "淋巴结分站",
        "转移模式",
        "m6a",
        "rna甲基化",
        "调控因子",
        "风险预测模型",
        "健康对照无显著差异",
        "降钙素原",
        "微创外科技术",
        "诊疗体系",
        "诊疗标准",
        "循证决策",
        "角色尚未",
        "尚未完全明确",
        "作用尚未明确",
        "已有明确描述",
        "实用临床定义",
        "对患者的心理",
        "心理需求",
        "内容涉及一般原则",
        "澳大利亚心血管疾病",
        "发病高峰年龄",
        "男女比例",
        "术后并发症",
        "bormann iii",
        "bormann iv",
        "住院时间延长",
        "中国常见的恶性肿瘤",
        "每年约有50%",
        "第四高",
        "痰瘀互结证",
        "中医证候",
        "常被低估",
        "心电图异常",
        "血小板计数",
        "患者心理",
        "抑郁、焦虑症状及睡眠问题",
        "无症状平均风险成人",
        "并称“四高”",
        '并称"四高"',
        "中足受累",
        "步行功能残疾",
        "血清a-ccp",
        "rf、crp",
        "水平异常升高",
    )
    allow_human_gut_association = _record_has_human_gut_disease_association(record)
    retained = [
        clause.strip()
        for clause in re.split(r"[，,]", value)
        if clause.strip()
        and not any(
            marker in clause.casefold()
            for marker in excluded
            if not (allow_human_gut_association and marker == "肠道菌群")
        )
        and not clause.strip().startswith(("强调", "突出"))
    ]
    if not retained:
        return ""
    result = "，".join(retained).rstrip("。！？!?")
    if result.startswith(("且", "并", "以及", "同时")):
        return ""
    return result + "。"


def _remove_disease_action_clauses(value: str) -> str:
    """Keep medical meaning while removing care advice in a mixed sentence."""
    retained: list[str] = []
    for clause in re.split(r"[，,\uff1b;]+", value):
        normalized = clause.strip()
        if not normalized or _claim_is_action_or_boundary_text(normalized):
            continue
        if normalized.startswith(("建议", "应当", "应与", "需要", "需")):
            continue
        retained.append(normalized.rstrip("。"))
    return "，".join(retained) + ("。" if retained else "")


def _disease_claim_has_substance(value: str, *, record: EvidenceRecord) -> bool:
    """Reject tautologies and title-review prose from the medical column."""
    normalized = _normalize(value)
    target_aliases = {
        _normalize(alias)
        for raw_target in record.targets
        for component in raw_target.split("/")
        for alias in _disease_terms(component.strip())
        if alias.strip()
    }
    if normalized in target_aliases:
        return False
    residual = value
    for raw_target in record.targets:
        for component in raw_target.split("/"):
            residual = residual.replace(component.strip(), "")
    residual = re.sub(
        r"(?:是|为)?(?:该|本)?(?:证据|文献|指南|共识)"
        r"(?:直接)?(?:讨论|涉及|关于|列出)?(?:的)?(?:疾病|项目)?",
        "",
        residual,
    )
    short_clinical_fact = residual.strip().rstrip("。！？!?") in {
        "多数无症状",
        "通常无症状",
        "可无症状",
        "可能无症状",
    }
    if len(_normalize(residual)) < 6 and not short_clinical_fact:
        return False
    if any(
        marker in residual
        for marker in (
            "多种慢性疾病",
            "多种疾病",
            "病情复杂",
            "多种多样",
            "复杂多样",
        )
    ):
        return False
    return any(
        marker in residual
        for marker in (
            "定义",
            "是一种",
            "为一种",
            "包括",
            "临床表现",
            "症状",
            "无症状",
            "可出现",
            "腹痛",
            "腹胀",
            "排便",
            "功能性",
            "排便困难",
            "排便次数",
            "运动迟缓",
            "震颤",
            "生活质量",
            "功能受损",
            "心境",
            "感觉",
            "思考",
            "行为",
            "病理",
            "病理生理",
            "发病",
            "并发",
            "风险",
            "相关",
            "预测因子",
            "特征",
            "炎症",
            "炎性",
            "自身免疫",
            "受累",
            "异常",
            "阈值",
            "血糖",
            "胆管",
            "代谢",
            "感染",
            "恶性",
            "癌",
            "肿瘤",
        )
    )


def _claim_is_action_or_boundary_text(value: str) -> bool:
    lowered = value.casefold()
    if lowered.startswith(("描述", "本文描述", "该文描述")):
        return True
    if re.search(
        r"^(?:该|本)?(?:文献|证据|摘要).{0,12}支持",
        lowered,
    ) or re.search(r"^支持.{1,40}(?:管理|诊疗|诊断)", lowered):
        return True
    return any(
        marker in lowered
        for marker in (
            "treatment",
            "therapy",
            "therapeutic",
            "anticoagulant",
            "management",
            "管理需",
            "高血糖管理",
            "管理",
            "screening recommendation",
            "recommend against",
            "治疗",
            "药物",
            "生活方式调整",
            "排便调护",
            "健康调护",
            "抗凝药",
            "抗生素",
            "达比加群",
            "阿哌沙班",
            "利伐沙班",
            "依度沙班",
            "诊治",
            "诊疗",
            "诊断",
            "检查",
            "评估",
            "诊断方法",
            "确诊",
            "排除",
            "mri",
            "ct是",
            "ct或",
            "pet-ct",
            "影像学",
            "首选方法",
            "诊断和管理",
            "筛查",
            "早筛",
            "早诊",
            "风险分层",
            "预测计算器",
            "在线预测",
            "可预测",
            "预测因素",
            "预测指标",
            "优势比",
            "粪便潜血",
            "处理差异",
            "管理差异",
            "当前目标",
            "目标疾病",
            "检查用于",
            "用药",
            "用于",
            "管理策略",
            "营养支持",
            "管理的重点",
            "管理提供建议",
            "提供管理建议",
            "减重",
            "体重减轻",
            "血糖控制",
            "进行保护",
            "该指南",
            "本指南",
            "指南",
            "共识",
            "立场声明",
            "指南替代",
            "指南推荐",
            "接种疫苗",
            "疫苗接种",
            "用于诊断",
            "用于排除",
            "诊断或排除",
            "治疗应答",
            "干预",
            "补充",
            "康复",
            "训练程式",
            "复发率",
            "基因突变",
            "阳性细胞",
            "干细胞样",
            "生物标志物",
            "脑电图",
            "痫样放电",
            "中医药",
            "制剂",
            "组成成分",
            "用于调节",
            "用于改善",
            "不推荐",
            "不建议",
            "仅作为",
            "不直接",
            "不等同",
            "研究和了解",
            "研究进展",
            "研究的重要",
            "文献讨论",
            "指南描述",
        )
    )


def retain_referenced_evidence(
    references: list[EvidenceReference],
    *,
    microbes: AbnormalMicrobesSection,
    diseases: DiseaseRisksSection,
    health: HealthManagementSection,
    clinical_document: ClinicalReportDocument | None = None,
) -> list[EvidenceReference]:
    """Keep only cited records and make patient-facing numbering consecutive."""
    used = {number for microbe in microbes.items for number in microbe.evidence_refs}
    used.update(
        number
        for microbe in microbes.items
        for number in microbe.interpretation_evidence_refs
    )
    used.update(
        number
        for microbe in microbes.items
        for number in microbe.association_evidence_refs
    )
    used.update(
        number
        for microbe in microbes.items
        for risk in microbe.disease_and_risks
        for number in risk.evidence_refs
    )
    for disease in diseases.items:
        used.update(disease.association_evidence_refs)
        used.update(disease.confirmation_evidence_refs)
        used.update(disease.evidence_refs)
        for evaluation in disease.suggested_evaluations:
            used.update(evaluation.evidence_refs)
    for recommendation in health.recommendations:
        used.update(recommendation.evidence_refs)

    document_blocks = []
    if clinical_document is not None:
        document_blocks = [*clinical_document.sections]
        for analysis in clinical_document.analyses:
            document_blocks.extend(value for name in ("causes", "mechanism", "outlook", "action", "goal") if (value := getattr(analysis, name)) is not None)
        used.update(number for block in document_blocks for number in block.evidence_refs)

    retained = [reference for reference in references if reference.number in used]
    mapping = {
        reference.number: new_number
        for new_number, reference in enumerate(retained, start=1)
    }

    def remap(values: list[int]) -> list[int]:
        return sorted({mapping[number] for number in values if number in mapping})

    for reference in retained:
        reference.number = mapping[reference.number]
    for microbe in microbes.items:
        for risk in microbe.disease_and_risks:
            risk.evidence_refs = remap(risk.evidence_refs)
        microbe.interpretation_evidence_refs = remap(
            microbe.interpretation_evidence_refs
        )
        microbe.association_evidence_refs = remap(microbe.association_evidence_refs)
        microbe.evidence_refs = remap(microbe.evidence_refs)
    for disease in diseases.items:
        disease.association_evidence_refs = remap(disease.association_evidence_refs)
        disease.confirmation_evidence_refs = remap(disease.confirmation_evidence_refs)
        disease.evidence_refs = remap(disease.evidence_refs)
        for evaluation in disease.suggested_evaluations:
            evaluation.evidence_refs = remap(evaluation.evidence_refs)
    for recommendation in health.recommendations:
        recommendation.evidence_refs = remap(recommendation.evidence_refs)
    for block in document_blocks:
        block.evidence_refs = remap(block.evidence_refs)
        block.evidence_quotes = {str(mapping[int(number)]): quote for number, quote in block.evidence_quotes.items() if number.isdigit() and int(number) in mapping}
        block.evidence_claims = {str(mapping[int(number)]): claim for number, claim in block.evidence_claims.items() if number.isdigit() and int(number) in mapping}
        block.evidence_fact_ids = {str(mapping[int(number)]): ids for number, ids in block.evidence_fact_ids.items() if number.isdigit() and int(number) in mapping}
        path = getattr(block, '_reviewed_path', ())
        meaning_proofs = getattr(block, '_meaning_contributions', None)
        if (isinstance(path, tuple) and path and path[0] in {
                'microbiomeAssessment', 'nutritionAssessment', 'functionalPathways'}
                and isinstance(meaning_proofs, frozenset)):
            # These live evaluate-only proofs refer to the exact same papers
            # as the public claims. Never create a proof or retain just a
            # subset when one of its original papers is not retained.
            block._meaning_contributions = frozenset(
                (row[0], row[1], row[2], frozenset(mapping[number] for number in row[3]))
                for row in meaning_proofs if isinstance(row, tuple) and len(row) == 4
                and isinstance(row[0], str) and isinstance(row[1], str)
                and isinstance(row[2], frozenset) and isinstance(row[3], frozenset)
                and all(type(number) is int and number in mapping for number in row[3]))
        disease_proofs = getattr(block, '_disease_attributions', None)
        if (isinstance(path, tuple) and path[:2] == ('diseaseRiskAssessment', 'categories')
                and isinstance(disease_proofs, frozenset)):
            # Reindex the same live, independently reviewed attribution along
            # with its claims. An old paper number is not a different paper;
            # a removed paper invalidates the complete proof, not just a part.
            block._disease_attributions = frozenset(
                (row[0], row[1], frozenset(mapping[number] for number in row[2]))
                for row in disease_proofs if isinstance(row, tuple) and len(row) == 3
                and isinstance(row[0], str) and isinstance(row[1], frozenset)
                and isinstance(row[2], frozenset)
                and all(type(number) is int and number in mapping for number in row[2]))
    return retained


def _evidence_backed_microbe_role(
    records: list[EvidenceRecord],
    *,
    microbe: AbnormalMicrobeItem | None = None,
) -> str | None:
    relation_types = {
        condition.relation_type
        for record in records
        if record.interpretation is not None
        for condition in record.interpretation.conditions
    }
    selected_records = _evidence_backed_microbe_role_records(records)
    for record in selected_records:
        interpretation = record.interpretation
        role = (
            " ".join((interpretation.clinical_role or "").split())
            if interpretation is not None
            else ""
        )
        normalized_role = _normalize(role)
        target_names = {
            _normalize(component)
            for target in record.targets
            for component in target.split("/")
            if component.strip()
        }
        direct_scoped_role = bool(
            interpretation is not None
            and interpretation.target_alignment == "direct"
            and interpretation.taxon_alignment in {"species", "genus"}
            and (
                any(
                    marker in role.casefold()
                    for marker in (
                        "该菌",
                        "该微生物",
                        "该物种",
                        "this organism",
                        "this species",
                    )
                )
                or len(record.microbe_ids) == 1
            )
        )
        if (
            role
            and len(_normalize(role)) >= 12
            and (
                any(name and name in normalized_role for name in target_names)
                or direct_scoped_role
            )
            and not any(
                marker in role
                for marker in (
                    "治疗",
                    "用药",
                    "手术",
                    "建议",
                    "应当",
                    "需要",
                    "检查",
                    "筛查",
                    "本次",
                    "受检者",
                    "患者本人",
                    "该起事件",
                    "本起事件",
                    "本次暴发",
                    "污染",
                    "发生率",
                    "首要致病原",
                    "超过",
                    "假阳性",
                    "假阴性",
                    "试验",
                    "检测",
                    "icu",
                    "血液病患者",
                    "抗菌药物",
                )
            )
        ):
            return role.rstrip("。！？!?") + "。"
    display_name = microbe.name if microbe is not None else "该微生物"
    if "established_pathogen" in relation_types:
        return f"人体资料支持{display_name}在相应临床情境中具有病原学意义。"
    if "opportunistic" in relation_types:
        return f"人体资料支持{display_name}在特定宿主或部位中可表现机会致病性。"
    if "colonization" in relation_types:
        return f"人体研究记录{display_name}可在特定人体部位定植。"
    if "environmental_or_dietary" in relation_types:
        return f"现有资料记录{display_name}可见于环境或饮食来源。"
    return None


def _evidence_backed_microbe_role_records(
    records: list[EvidenceRecord],
) -> list[EvidenceRecord]:
    """Return the exact records supporting the displayed general role."""

    present = {
        condition.relation_type
        for record in records
        if record.interpretation is not None
        for condition in record.interpretation.conditions
    }
    selected_type = next(
        (
            relation_type
            for relation_type in (
                "established_pathogen",
                "opportunistic",
                "colonization",
                "environmental_or_dietary",
            )
            if relation_type in present
        ),
        None,
    )
    if selected_type is None:
        return []
    matching = [
        record
        for record in records
        if record.interpretation is not None
        and any(
            condition.relation_type == selected_type
            for condition in record.interpretation.conditions
        )
    ]
    return sorted(
        _dedupe(matching),
        key=_record_priority_score,
        reverse=True,
    )[:2]


def _title_explicitly_names_query_relation(
    record: EvidenceRecord,
    query: EvidenceQuery,
) -> bool:
    """Recognize concise exact taxon-condition clinical titles.

    EviMed frequently returns titles such as ``Pseudomonas aeruginosa
    pneumonia`` without a relation verb.  When the query itself is an exact
    relation query and every required concept occurs in that short title, keep
    the record for semantic interpretation.  Publication still requires the
    later relation-binding and applicability validators.
    """

    if not query.relation_id or len(record.title) > 240:
        return False
    title = record.title.casefold()
    normalized_title = _normalize(title)
    return all(
        any(
            _concept_match_score(title, normalized_title, term) > 0
            for term in alternatives
        )
        for alternatives in query.required_concepts
    )


def _adult_query_has_pediatric_only_source(
    text: str,
    *,
    age_group: str,
) -> bool:
    """Reject pediatric-only evidence without discarding mixed-age guidance."""

    return bool(
        age_group == "adult"
        and any(marker in text for marker in _PEDIATRIC_MARKERS)
        and not any(marker in text for marker in _ADULT_MARKERS)
    )


def is_uncompleted_clinical_evidence(title: str) -> bool:
    """Exclude study plans, not clinical practice protocols by name alone.

    Ambiguous protocol titles still need normal content/applicability review;
    passing this title check is not proof that a study has completed.
    """
    return bool(re.search(
        r"研究方案|研究计划|撤稿|\bretract(?:ed|ion)\b"
        r"|\b(?:study|trial|review|meta-analysis|cohort)\s+protocol\b"
        r"|\bprotocol\s+(?:paper\s+)?for\s+(?:(?:an?|the)\s+)?"
        r"(?:(?:randomi[sz]ed|controlled|clinical|systematic|scoping|prospective|multicentre|multicenter|pilot)\s+)*"
        r"(?:study|trial|review|meta-analysis|cohort)\b", title, re.I))


def _screen(record: EvidenceRecord, *, query: EvidenceQuery) -> tuple[bool, str]:
    general_function = query.screen_profile == "evaluate_general_function"
    ecological = query.screen_profile == 'evaluate_ecological_association'
    if general_function and query.intent != "gut_function":
        return False, "profile_intent_mismatch"
    text = f"{record.title} {record.summary or ''}".casefold()
    if general_function or ecological:
        original = evaluation_original_text(record)
        if not original:
            return False, "missing_original_text"
        text = f"{record.title} {original}".casefold()
    elif query.screen_profile == "clinical_report":
        # A brief translated/generated summary may omit the exact concept
        # that is present in the original abstract or guideline passage.
        text = " ".join(filter(None, (record.title, record.abstract,
            record.evidence_text, record.summary))).casefold()
    normalized_text = _normalize(text)
    concept_scores: list[float] = []
    study_types = " ".join(record.study_types).casefold()
    if not general_function and any(
        marker in study_types
        for marker in ("动物实验", "体外实验", "animal", "in vitro", "preclinical")
    ):
        return False, "nonhuman_or_preclinical"
    if query.intent in {
        "microbe_discovery",
        "microbe_clinical_significance",
        "microbe_association",
        "microbe_disease_association",
    } and ("病例报告" in study_types or "case report" in study_types):
        return False, "single_case_evidence"
    # Pediatric evidence can still describe the established clinical role of
    # an exact taxon. It must not drive an adult disease explanation,
    # diagnostic path, or lifestyle recommendation.
    if (
        not general_function and query.intent
        in {
            "disease_clinical_significance",
            "diagnostic_guideline",
            "health_management",
            "gut_function",
            "gut_barrier",
            "intervention",
        }
        and _adult_query_has_pediatric_only_source(
            text,
            age_group=query.age_group,
        )
    ):
        return False, "population_mismatch"
    for alternatives in query.required_concepts:
        best = 0.0
        for term in alternatives:
            best = max(best, _concept_match_score(text, normalized_text, term))
            if query.screen_profile in {"clinical_report", "evaluate_general_function", "evaluate_ecological_association"} and not query.microbe_id:
                best = max(best, _clinical_concept_match_score(text, term))
        if best == 0:
            return False, "missing_required_concept"
        concept_scores.append(best)
    if general_function:
        # A basic bacterial process may be established in biochemical or
        # in-vitro work. This is a candidate for a general function meaning,
        # never human clinical evidence or a patient-specific effect.
        if is_uncompleted_clinical_evidence(record.title):
            return False, "not_completed_evidence"
    elif query.screen_profile in {"clinical_report", "evaluate_ecological_association"}:
        # Clinical assessment does not require a stool specimen. Human and
        # patient applicability checks still apply to the new query profile.
        if is_uncompleted_clinical_evidence(record.title):
            return False, "not_completed_evidence"
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS):
            return False, "nonhuman_or_preclinical"
        if record.category != "guideline" and not any(marker in text for marker in _HUMAN_EVIDENCE_MARKERS):
            return False, "missing_human_evidence"
        if ecological:
            from .evaluation_function_evidence import ecological_association_priority
            if len(query.required_concepts) != 3 or not ecological_association_priority(
                    evaluation_original_text(record),record.title,query.required_concepts[0],query.required_concepts[2]):
                return False, 'missing_ecological_population_association'
    elif query.screen_profile == "diagnostic" or query.intent == "diagnostic_guideline":
        if record.category != "guideline" and not any(
            marker in text for marker in _HUMAN_EVIDENCE_MARKERS
        ):
            return False, "missing_human_evidence"
        title = record.title.casefold()
        if any(
            marker in title
            for marker in (
                "treatment",
                "therapy",
                "therapeutics",
                "management",
                "治疗",
                "管理",
            )
        ) and not any(marker in title for marker in _DIAGNOSTIC_MARKERS):
            return False, "non_diagnostic_guideline"
        if any(marker in text for marker in _NON_DIAGNOSTIC_GUIDELINE_MARKERS):
            return False, "non_diagnostic_guideline"
        target_text = " ".join(query.required_concepts[0]).casefold()
        if any(
            marker in title and marker not in target_text
            for marker in _DIRECT_DIAGNOSTIC_EXCLUSION_MARKERS
        ):
            return False, "restricted_or_comorbid_context"
        if not any(marker in text for marker in _DIAGNOSTIC_MARKERS):
            return False, "missing_diagnostic_intent"
    elif query.screen_profile == "discovery" or query.intent == "microbe_discovery":
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS):
            return False, "nonhuman_or_preclinical"
        if any(
            marker in record.title.casefold()
            for marker in _LABORATORY_METHOD_GUIDELINE_MARKERS
        ):
            return False, "laboratory_method_only"
        if any(marker in text for marker in _NON_CLINICAL_DISCOVERY_MARKERS):
            return False, "non_clinical_target_use"
        if record.category != "guideline" and not any(
            marker in text for marker in _DISCOVERY_CONTEXT_MARKERS
        ):
            return False, "missing_clinical_context"
    elif (
        query.screen_profile == "pathogen"
        or query.intent == "microbe_clinical_significance"
    ):
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS):
            return False, "nonhuman_or_preclinical"
        if record.category != "guideline" and not any(
            marker in text for marker in _HUMAN_EVIDENCE_MARKERS
        ):
            return False, "missing_human_evidence"
        if not any(marker in text for marker in _CLINICAL_SIGNIFICANCE_MARKERS):
            return False, "missing_clinical_significance"
    elif query.screen_profile == "pathogen_relation":
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS):
            return False, "nonhuman_or_preclinical"
        if record.category != "guideline" and not any(
            marker in text for marker in _HUMAN_EVIDENCE_MARKERS
        ):
            return False, "missing_human_evidence"
        if not any(marker in text for marker in _CLINICAL_RELATION_MARKERS):
            return False, "missing_clinical_relationship"
    elif query.screen_profile == "human_relation":
        descriptive_microbiome_only = any(
            marker in text
            for marker in (
                "microbiome analysis",
                "microbiome profiling",
                "microbiota profiling",
                "metagenome sequencing",
                "metagenomic sequencing",
                "微生物组分析",
                "菌群分析",
                "宏基因组测序",
            )
        ) and not any(
            marker in text
            for marker in (
                "caused by",
                "causes ",
                "causative",
                "responsible for",
                "associated with",
                "association between",
                "correlated with",
                "enriched in",
                "increased in",
                "decreased in",
                "predictor",
                "引起",
                "导致",
                "相关",
                "富集",
                "增加",
                "降低",
                "预测",
            )
        )
        if descriptive_microbiome_only:
            return False, "descriptive_microbiome_only"
        explicit_established_relation = query.relation_type in {
            "established_pathogen",
            "opportunistic",
        } and any(
            marker in text
            for marker in (
                "caused by",
                "causes ",
                "causative agent",
                "causative pathogen",
                "etiological agent",
                "etiologic agent",
                "responsible for",
                "pathogenesis of",
                "引起",
                "导致",
                "病原体",
                "致病菌",
            )
        )
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS) and not (
            (record.category == "guideline" or "review" in study_types)
            and explicit_established_relation
        ):
            return False, "nonhuman_or_preclinical"
        exact_relation_title = _title_explicitly_names_query_relation(
            record,
            query,
        )
        if (
            record.category != "guideline"
            and not any(marker in text for marker in _HUMAN_EVIDENCE_MARKERS)
            and not explicit_established_relation
        ):
            return False, "missing_human_evidence"
        if not exact_relation_title and not any(
            marker in text for marker in _CLINICAL_RELATION_MARKERS
        ):
            return False, "missing_clinical_relationship"
    elif query.screen_profile == "disease_context":
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS):
            return False, "nonhuman_or_preclinical"
        if any(marker in text for marker in ("philosoph", "哲学")):
            return False, "non_clinical_disease_context"
        if record.category != "guideline" and not any(
            marker in text for marker in _HUMAN_EVIDENCE_MARKERS
        ):
            return False, "missing_human_evidence"
        target_text = " ".join(query.required_concepts[0]).casefold()
        if any(
            marker in record.title.casefold() and marker not in target_text
            for marker in _DIRECT_DIAGNOSTIC_EXCLUSION_MARKERS
        ):
            return False, "restricted_or_comorbid_context"
        if not any(marker in text for marker in _CLINICAL_RELATION_MARKERS):
            return False, "missing_clinical_relationship"
        title = record.title.casefold()
        if any(marker in title for marker in _DISEASE_ACTION_ONLY_MARKERS) and not (
            _contains_disease_outcome_marker(title)
        ):
            return False, "treatment_or_management_only"
    elif query.screen_profile == "gut_association" or query.intent in {
        "microbe_association",
        "microbe_disease_association",
    }:
        direct_clinical_abundance = _source_has_direct_clinical_abundance_relation(
            record,
            query=query,
        )
        if record.category == "guideline":
            return False, "not_association_study"
        if any(
            marker in record.title.casefold()
            for marker in _ABUNDANCE_INTERVENTION_TITLE_MARKERS
        ) and not direct_clinical_abundance:
            return False, "intervention_not_abundance_association"
        if any(marker in record.title.casefold() for marker in _NON_GUT_SITE_MARKERS):
            return False, "non_gut_sampling_site"
        relation_specific_stool = (
            query.intent in {"microbe_association", "microbe_disease_association"}
            and any(
                marker in text
                for marker in ("stool", "fecal", "faecal", "粪便")
            )
        )
        if (
            not direct_clinical_abundance
            and not relation_specific_stool
            and not any(marker in text for marker in _MICROBIOME_MARKERS)
        ):
            return False, "missing_microbiome_context"
        if (
            any(marker in text for marker in _NON_CLINICAL_EXCLUSION_MARKERS)
            and not direct_clinical_abundance
        ):
            return False, "nonhuman_or_preclinical"
        if (
            not any(marker in text for marker in _HUMAN_EVIDENCE_MARKERS)
            and not direct_clinical_abundance
        ):
            return False, "missing_human_evidence"
        if not any(marker in text for marker in _ASSOCIATION_MARKERS):
            return False, "missing_association_result"
    elif query.screen_profile in {"gut_function", "gut_barrier"} or query.intent in {
        "gut_function",
        "gut_barrier",
    }:
        if any(marker in text for marker in _ANIMAL_PRECLINICAL_MARKERS):
            return False, "nonhuman_or_preclinical"
        if not any(marker in text for marker in _GUT_CONTEXT_MARKERS):
            return False, "missing_microbiome_context"
        if not any(marker in text for marker in _HUMAN_EVIDENCE_MARKERS):
            return False, "missing_human_evidence"
        outcome_markers = (
            (
                "intestinal permeability",
                "tight junction",
                "mucus",
                "zonulin",
                "calprotectin",
                "endotoxin",
                "肠道通透性",
                "紧密连接",
                "黏液层",
                "连蛋白",
                "钙卫蛋白",
                "内毒素",
                "肠道屏障",
            )
            if query.intent == "gut_barrier"
            else (
                "short chain fatty acid",
                "scfa",
                "butyrate",
                "propionate",
                "acetate",
                "bile acid",
                "短链脂肪酸",
                "丁酸",
                "丙酸",
                "乙酸",
                "胆汁酸",
            )
        )
        if not any(marker in text for marker in outcome_markers):
            return False, (
                "missing_barrier_outcome"
                if query.intent == "gut_barrier"
                else "missing_function_outcome"
            )
        if not any(marker in text for marker in _ASSOCIATION_MARKERS):
            return False, "missing_association_result"
    elif query.screen_profile == "health" or query.intent in {
        "health_management",
        "intervention",
    }:
        title = record.title.casefold()
        intended_context = " ".join(
            (
                query.target,
                query.query,
                *(
                    alias
                    for concept in query.required_concepts
                    for alias in concept
                ),
            )
        ).casefold()
        if any(
            marker in title and marker not in intended_context
            for marker in _HEALTH_RESTRICTED_CONTEXT_MARKERS
        ):
            return False, "restricted_health_population"
        if any(marker in title for marker in _HEALTH_PRODUCT_STUDY_MARKERS):
            return False, "product_specific_intervention"
        if not any(marker in text for marker in _HEALTH_INTERVENTION_MARKERS):
            return False, "missing_lifestyle_intervention"
        if record.category != "guideline" and not any(
            marker in text for marker in _HUMAN_EVIDENCE_MARKERS
        ):
            return False, "missing_human_evidence"
    # A general clinical-management query may have no mandatory term groups;
    # it still passes human/source screening and later semantic assessment.
    record.relevance = sum(concept_scores) / len(concept_scores) if concept_scores else 0.0
    return True, "retained"


def _source_has_direct_clinical_abundance_relation(
    record: EvidenceRecord,
    *,
    query: EvidenceQuery,
) -> bool:
    """Recognize one local human/review abundance statement.

    Some microbiome papers start with a human multicohort depletion result and
    then continue with mouse or cell experiments.  The association query may
    use that human result, but must not inherit the later mechanism as human
    evidence.  A short review passage can similarly introduce a taxon, refer
    to ``this depletion`` and name the clinical diseases in the next sentence.
    """

    if len(query.required_concepts) < 2:
        return False
    sentences = [
        sentence.strip()
        for sentence in re.split(
            r"(?<=[.!?。！？；;])\s*|[\r\n]+",
            record.summary or "",
        )
        if sentence.strip()
    ]
    windows = [record.title, *sentences]
    for width in (2, 3):
        windows.extend(
            " ".join(sentences[index : index + width])
            for index in range(max(0, len(sentences) - width + 1))
        )
    study_types = " ".join(record.study_types).casefold()
    is_synthesis = record.category == "guideline" or "review" in study_types
    direction_markers = (
        "depleted",
        "depletion",
        "reduced abundance",
        "decreased abundance",
        "lower abundance",
        "low abundance",
        "absence",
        "absent",
        "enriched",
        "increased abundance",
        "higher abundance",
        "high abundance",
        "丰度降低",
        "丰度减少",
        "低丰度",
        "缺乏",
        "耗竭",
        "丰度增加",
        "丰度升高",
        "高丰度",
        "富集",
    )
    for window in windows:
        lowered = window.casefold()
        normalized = _normalize(lowered)
        if not all(
            any(
                _concept_match_score(lowered, normalized, term) > 0
                for term in alternatives
            )
            for alternatives in query.required_concepts[:2]
        ):
            continue
        if not any(marker in lowered for marker in direction_markers):
            continue
        if not any(marker in lowered for marker in _GUT_CONTEXT_MARKERS):
            continue
        if any(marker in lowered for marker in _ANIMAL_PRECLINICAL_MARKERS):
            continue
        if is_synthesis or any(
            marker in lowered for marker in _HUMAN_EVIDENCE_MARKERS
        ):
            return True
    return False


def _scientific_terms(name: str) -> tuple[str, ...]:
    aliases = _taxon_aliases(name)
    tokens = name.split()
    if len(tokens) >= 2:
        genus, species = tokens[:2]
        aliases = (
            *aliases,
            f"{genus} {species}",
            f"{genus[0]}. {species}",
            f"{genus[0]} {species}",
        )
    return tuple(dict.fromkeys(aliases))


def _disease_terms(name: str) -> tuple[str, ...]:
    if name == "腺瘤":
        # The source item is a colorectal microbiome association. The bare
        # Chinese word also matches thyroid and other adenomas in EviMed.
        return (
            "结直肠腺瘤",
            "肠腺瘤",
            "colorectal adenoma",
            "intestinal adenoma",
        )
    aliases = _DISEASE_ALIASES.get(name, ())
    return tuple(dict.fromkeys((name, *aliases)))


def _preferred_english_term(name: str) -> str:
    aliases = _DISEASE_ALIASES.get(name, ())
    return aliases[0] if aliases else name


def _preferred_chinese_term(name: str) -> str:
    """Use the report-facing Chinese term people actually search for."""
    return _PREFERRED_CHINESE_DISEASE_TERMS.get(name, name)


def _medical_search_hints(name: str) -> tuple[str, ...]:
    """Add only target-specific clinical concepts; never add a conclusion."""
    return _DISEASE_MEDICAL_SEARCH_HINTS.get(name, ())


def _diagnostic_search_hints(name: str) -> tuple[str, ...]:
    """Use concrete assessment names to avoid generic or wrong-path hits."""
    return _DISEASE_DIAGNOSTIC_SEARCH_HINTS.get(name, ())


def _match_related_taxon(related_name: str, microbes: AbnormalMicrobesSection):
    identity = _related_taxon_identity(related_name)
    related_aliases = {_normalize(item) for item in _taxon_aliases(identity)}
    related_tokens = identity.casefold().split()
    matches = []
    for microbe in microbes.items:
        microbe_aliases = {
            _normalize(item)
            for name in (microbe.scientific_name, microbe.name)
            for item in _taxon_aliases(name)
        }
        if related_aliases & microbe_aliases:
            matches.append(microbe)
            continue
        scientific_tokens = microbe.scientific_name.casefold().split()
        # A source relationship may be reported at genus level while the
        # finding table contains species.  Match only a complete genus token;
        # higher ranks remain explicitly unresolved in the audit.
        if (
            len(related_tokens) == 1
            and scientific_tokens
            and related_tokens[0] == scientific_tokens[0]
        ):
            matches.append(microbe)
    return matches


def _related_taxon_identity(name: str) -> str:
    """Strip only a table's trailing abundance label, preserving taxon ranks."""
    return re.sub(
        r"\s*[（(]\s*(?:超标|缺乏|偏高|偏低|丰度高|丰度低|正常|检出|未检出)\s*[）)]\s*$",
        "",
        name,
    ).strip()


def _taxon_aliases(name: str) -> tuple[str, ...]:
    normalized = " ".join(name.split())
    canonical = _TAXON_SYNONYMS.get(normalized.casefold())
    reverse = [
        alias
        for alias, accepted in _TAXON_SYNONYMS.items()
        if accepted.casefold() == normalized.casefold()
    ]
    return tuple(dict.fromkeys((normalized, canonical or normalized, *reverse)))


def _dedupe_queries(queries: list[EvidenceQuery]) -> list[EvidenceQuery]:
    result: list[EvidenceQuery] = []
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...], str, str | None]] = set()
    for query in queries:
        marker = (
            query.query.casefold(),
            query.supports,
            query.channels,
            query.screen_profile,
            query.relation_id,
        )
        if marker in seen:
            continue
        seen.add(marker)
        result.append(query)
    return result


def _record_numbers(
    records: list[EvidenceRecord], numbers: dict[str, int]
) -> list[int]:
    return sorted(
        {
            numbers[record.evidence_key]
            for record in records
            if record.evidence_key in numbers
        }
    )


def _clinical_concept_match_score(text: str, term: str) -> float:
    """Recall-only matching of English clinical phrases, never Latin taxa.

    Retain every query token in order and bounded proximity. Allow regular
    plurals and short intervening phrases, e.g. 'kidney and ureteral stones'.
    Final clinical relevance still requires the independent source review.
    """
    if re.search(r"[\u3400-\u9fff]", term):
        return 0.0
    tokens = re.findall(r"[A-Za-z0-9]+", term)
    if not 2 <= len(tokens) <= 8:
        return 0.0
    patterns = [re.escape(token) + ("s?" if len(token) > 3 and not token.casefold().endswith("s") else "")
        for token in tokens]
    gap = r"(?:[^A-Za-z0-9]+[A-Za-z0-9]+){0,3}[^A-Za-z0-9]+"
    phrase = gap.join(patterns)
    for sentence in re.split(r"[.!?;。！？；\n]", text):
        for match in re.finditer(rf"(?<![A-Za-z0-9]){phrase}(?![A-Za-z0-9])", sentence, re.I):
            if len(match.group()) > max(120, len(term) * 2):
                continue
            prefix = sentence[max(0, match.start() - 4):match.start()].casefold()
            if not prefix.endswith(("non-", "non ")):
                return 0.75
    return 0.0


def _concept_match_score(text: str, normalized_text: str, term: str) -> float:
    """Match one concept without allowing acronym or Latin-name substrings."""
    stripped = term.strip()
    if not stripped:
        return 0.0
    if re.search(r"[\u3400-\u9fff]", stripped):
        for match in re.finditer(re.escape(stripped), text, flags=re.IGNORECASE):
            if match.start() > 0 and text[match.start() - 1] == "非":
                continue
            return 1.0
        return 0.0
    compact = re.sub(r"[^A-Za-z0-9]", "", stripped)
    if compact.isupper() and len(compact) <= 5:
        return (
            1.0
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(compact)}(?![A-Za-z0-9])",
                text,
                flags=re.IGNORECASE,
            )
            else 0.0
        )
    tokens = re.findall(r"[A-Za-z0-9]+", stripped)
    if not tokens:
        return 0.0
    phrase = r"[^A-Za-z0-9]+".join(map(re.escape, tokens))
    for match in re.finditer(
        rf"(?<![A-Za-z0-9]){phrase}(?![A-Za-z0-9])",
        text,
        flags=re.IGNORECASE,
    ):
        prefix = text[max(0, match.start() - 4) : match.start()].casefold()
        if prefix.endswith(("non-", "non ")):
            continue
        return 1.0
    return 0.0


def _dedupe(
    records: list[EvidenceRecord],
    *,
    preserve_relation_scope: bool = True,
) -> list[EvidenceRecord]:
    combined: dict[tuple[Any, ...], EvidenceRecord] = {}
    for record in records:
        relation_scope = (
            tuple(sorted(binding.relation_id for binding in record.relation_bindings))
            if preserve_relation_scope
            else ()
        )
        semantic_scope = (
            (
                tuple(sorted(record.intents)),
                tuple(sorted(record.supports)),
                tuple(sorted(record.targets)),
            )
            if preserve_relation_scope
            else ()
        )
        original = evaluation_original_text(record) if preserve_relation_scope else ""
        if original:
            # Separate retrieval rounds can return different literal originals
            # for the same publication and semantic query. Keep both, without
            # changing their text; repeated whitespace-equivalent originals
            # from the same provenance still coalesce normally.
            block_scope = (
                stable_id("original-block", " ".join(original.split())),
                record.provider, record.source_id, record.parent_guide_id,
                record.source_evidence_key,
            )
        else:
            block_scope = (
                stable_id("guide-block", record.evidence_text)
                if preserve_relation_scope
                and record.evidence_text
                and (record.parent_guide_id or record.provider == "pmc")
                else ""
            )
        key = (
            record.category,
            _normalize(record.title),
            block_scope,
            relation_scope,
            semantic_scope,
        )
        existing = combined.get(key)
        if existing is None:
            combined[key] = record
            continue
        existing.supports = list(dict.fromkeys(existing.supports + record.supports))
        existing.query_ids = list(dict.fromkeys(existing.query_ids + record.query_ids))
        existing.microbe_ids = list(
            dict.fromkeys(existing.microbe_ids + record.microbe_ids)
        )
        existing.disease_ids = list(
            dict.fromkeys(existing.disease_ids + record.disease_ids)
        )
        existing_had_relation = bool(existing.relation_bindings)
        bindings = {
            binding.relation_id: binding
            for binding in [*existing.relation_bindings, *record.relation_bindings]
        }
        existing.relation_bindings = list(bindings.values())
        existing.intents = list(dict.fromkeys(existing.intents + record.intents))
        existing.targets = list(dict.fromkeys(existing.targets + record.targets))
        existing.relevance = max(existing.relevance, record.relevance)
        if existing.interpretation is None and record.interpretation is not None or (
            record.interpretation is not None
            and record.relation_bindings
            and not existing_had_relation
        ):
            existing.interpretation = record.interpretation
        if not existing.source_evidence_key and record.source_evidence_key:
            existing.source_evidence_key = record.source_evidence_key
        if not existing.summary and record.summary:
            existing.summary = record.summary
        if not existing.provider and record.provider:
            existing.provider = record.provider
        if not existing.source_id and record.source_id:
            existing.source_id = record.source_id
        if not existing.parent_guide_id and record.parent_guide_id:
            existing.parent_guide_id = record.parent_guide_id
        if not existing.evidence_text and record.evidence_text:
            existing.evidence_text = record.evidence_text
        if not existing.abstract and record.abstract:
            existing.abstract = record.abstract
        if not existing.ai_summary and record.ai_summary:
            existing.ai_summary = record.ai_summary
        existing.authors = list(dict.fromkeys([*existing.authors, *record.authors]))
        existing.study_types = list(
            dict.fromkeys([*existing.study_types, *record.study_types])
        )
        existing.core_journals = list(
            dict.fromkeys([*existing.core_journals, *record.core_journals])
        )
        existing.impact_factor = (
            max(
                existing.impact_factor or 0.0,
                record.impact_factor or 0.0,
            )
            or None
        )
    return list(combined.values())


def _normalize(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _query_language(value: str) -> str:
    has_cjk = bool(re.search(r"[\u3400-\u9fff]", value))
    has_latin = bool(re.search(r"[A-Za-z]", value))
    if has_cjk and has_latin:
        return "mixed"
    if has_cjk:
        return "chinese"
    if has_latin:
        # Scientific names and English prose use the same alphabet.  Queries
        # containing a binomial without other words are identified as Latin.
        words = re.findall(r"[A-Za-z]+", value)
        if 1 <= len(words) <= 3 and all(word[:1].isalpha() for word in words):
            return "latin"
        return "english"
    return "unknown"


_ANIMAL_PRECLINICAL_MARKERS = (
    "gorilla",
    "mouse model",
    "mice model",
    "murine model",
    "rat model",
    " rat ",
    " rats ",
    " mice ",
    "animal study",
    "animal studies",
    "animal model",
    "animal experiment",
    "preclinical model",
    "in vitro",
    "in vitro study",
    "in-vitro study",
    "in silico",
    "in-silico",
    "drug target identification",
    "cytotoxic diterpenoid",
    "cell line study",
    "cell lines",
    "epithelial cells",
    "epithelial cell",
    "nematode",
    "larval model",
    "larval survival",
    "mandarin fish",
    "aquaculture",
    "poultry",
    "chicken",
    "broiler",
    "avian model",
    "plant pathogen",
    "plant disease",
    "crop disease",
    "wheat rust",
    "wheat disease",
    "agricultural pathogen",
    "natural product screening",
    "计算机模拟",
    "细胞毒性",
    "天然产物筛选",
    "simulated microgravity",
    "canine",
    " dogs",
    "dog ",
    "feline",
    " cats",
    "cat ",
    "veterinary",
    "pig serum",
    "pigs",
    "porcine serum",
    "swine serum",
    "samples from pigs",
    "animal serum",
    "animal cell",
    "animal cells",
    "organoid",
    "polysaccharide effect",
    "wild baboon",
    "wild baboons",
    " baboon ",
    " baboons ",
    "bovine mastitis",
    "calf",
    "calves",
    "bovine",
    "cattle",
    "nonhuman primate",
    "non-human primate",
    "小鼠",
    "大鼠",
    "猪血清",
    "猪样本",
    "动物血清",
    "动物细胞",
    "类器官",
    "多糖对",
    "野生狒狒",
    "非人灵长类",
    "犊牛",
    "牛源",
    "动物模型",
    "动物实验",
    "仔猪",
    "猪病",
    "畜牧",
    "兽医",
    "动物医学",
    "体外研究",
    "体外实验",
    "细胞系",
    "细胞实验",
    "细胞研究",
    "线虫",
    "幼虫模型",
    "鱼类",
    "水产养殖",
    "家禽",
    "禽类",
    "鸡模型",
    "植物病原",
    "植物病害",
    "作物病害",
    "小麦锈病",
)

_DISCOVERY_CONTEXT_MARKERS = (
    "human",
    "patient",
    "clinical",
    "adult",
    "children",
    "participant",
    "cohort",
    "case report",
    "stool",
    "fecal",
    "faecal",
    "gut microbiome",
    "gut microbiota",
    "intestinal microbiome",
    "intestinal microbiota",
    "mycobiome",
    "dietary",
    "foodborne",
    "患者",
    "临床",
    "成人",
    "儿童",
    "粪便",
    "肠道菌群",
    "饮食",
    "食源性",
)

_CLINICAL_SIGNIFICANCE_MARKERS = (
    "infection",
    "infectious",
    "disease",
    "pathogen",
    "pathogenic",
    "toxin",
    "toxigenic",
    "virulence",
    "colonization",
    "colonisation",
    "carriage",
    "diagnosis",
    "clinical significance",
    "opportunistic",
    "感染",
    "疾病",
    "病原",
    "毒素",
    "毒力",
    "定植",
    "携带",
    "诊断",
    "机会致病",
)

_CLINICAL_RELATION_MARKERS = (
    *_CLINICAL_SIGNIFICANCE_MARKERS,
    "caused by",
    "causes ",
    "causative",
    "responsible for",
    "associated",
    "association",
    "related to",
    "increased",
    "decreased",
    "enriched",
    "risk",
    "相关",
    "引起",
    "导致",
    "增加",
    "降低",
    "富集",
    "风险",
)


_LABORATORY_METHOD_GUIDELINE_MARKERS = (
    "antimicrobial susceptibility testing",
    "susceptibility testing method",
    "laboratory standard",
    "reference method for testing",
    "breakpoint standard",
    "differentiation of",
    "identification of",
    "chemometrics",
    "ft-ir",
    "mass spectrometry identification",
    "assay validation",
    "multiplex pcr assay",
    "differential detection",
    "polymerase chain reaction",
    "diagnostic assay",
)

_DISEASE_ACTION_ONLY_MARKERS = (
    "treatment",
    "therapy",
    "therapeutics",
    "pharmacotherap",
    "management",
    "monitoring",
    "screening",
    "intervention",
    "drug therapy",
    "surgery",
    "exercise",
    "physical activity",
    "glycemic control",
    "how to help patients",
    "nursing care",
    "home care",
    "self-care",
    "治疗",
    "疗法",
    "管理",
    "监测",
    "干预",
    "用药",
    "药敏",
    "抗生素",
    "耐药",
    "手术",
    "运动",
    "血糖控制",
    "控制目标",
    "护理",
    "中医",
    "证候",
    "防治",
    "早筛",
    "早诊",
    "早治",
    "疗效",
    "他汀",
    "塞来昔布",
)

_DISEASE_OUTCOME_MARKERS = (
    "risk",
    "association",
    "associated",
    "complication",
    "outcome",
    "prognosis",
    "epidemiology",
    "clinical feature",
    "风险",
    "关联",
    "并发症",
    "结局",
    "预后",
    "流行病学",
    "临床特征",
)

_NON_CLINICAL_DISCOVERY_MARKERS = (
    "asparaginase",
    "enzyme production",
    "enzyme preparation",
    "recombinant expression",
    "expression vector",
    "industrial fermentation",
)


_NON_CLINICAL_EXCLUSION_MARKERS = (
    "gorilla",
    "mouse model",
    "mice model",
    "murine model",
    "rat model",
    "preclinical model",
    "in vitro study",
    "simulated microgravity",
    "lower airway dysbiosis",
    "lung microbiota",
    "canine",
    " dogs",
    "dog ",
    "feline",
    " cats",
    "cat ",
    "veterinary",
)

_MICROBIOME_MARKERS = (
    "gut microbiome",
    "gut microbiota",
    "intestinal microbiome",
    "intestinal microbiota",
    "gastrointestinal microbiome",
    "肠道菌群",
    "肠道微生物组",
    "肠道微生态",
)

_GUT_CONTEXT_MARKERS = (
    *_MICROBIOME_MARKERS,
    "stool",
    "fecal",
    "faecal",
    "intestinal",
    "gastrointestinal",
    "粪便",
    "肠道",
    "肠黏膜",
    "肠粘膜",
)

_PEDIATRIC_MARKERS = (
    "pediatric",
    "paediatric",
    "children",
    "childhood",
    "infant",
    "adolescent",
    "儿童",
    "小儿",
    "婴儿",
    "青少年",
    "患儿",
)

_ADULT_MARKERS = (
    "adult",
    "adults",
    "adult patient",
    "men and women",
    "women and men",
    "18 years",
    "成人",
    "成年人",
)

_HUMAN_EVIDENCE_MARKERS = (
    "human",
    "patient",
    "participant",
    "subject",
    "volunteer",
    "children",
    "child",
    "adult",
    "women",
    "men ",
    "individual",
    "cohort",
    "case-control",
    "cross-sectional",
    "clinical",
    "trial",
    "meta-analysis",
    "meta analysis",
    "systematic review",
    "患者",
    "受试者",
    "人群",
    "儿童",
    "成人",
    "队列",
    "临床",
)

_DIAGNOSTIC_MARKERS = (
    "diagnosis",
    "diagnostic",
    "screening",
    "screen",
    "workup",
    "colonoscopy",
    "endoscopy",
    "criteria",
    "evaluation",
    "assessment",
    "biomarker",
    "诊断",
    "筛查",
    "评估",
)

_NON_DIAGNOSTIC_GUIDELINE_MARKERS = (
    "fecal microbiota transplantation",
    "faecal microbiota transplantation",
    "microbiota transfer therapy",
    "probiotic treatment",
    "venous thromboembolism",
    "opportunistic infection",
    "clinical nutrition",
    "perioperative",
    "chemotherapy",
    "粪菌移植",
    "静脉血栓",
    "机会性感染",
    "临床营养",
    "围手术期",
    "化疗",
    "guideline on the treatment",
    "treatment guideline",
    "management guideline",
    "management guidelines",
    "medical treatment",
    "therapeutics",
    "治疗指南",
    "管理指南",
)

_DIRECT_DIAGNOSTIC_EXCLUSION_MARKERS = (
    "nsclc",
    "non-small cell lung cancer",
    "non small cell lung cancer",
    "lung cancer",
    "肺癌",
    "cystic fibrosis",
    "cancer patients",
    "oncology patients",
    "oncology setting",
    "pregnancy",
    "pregnant",
    "postpartum",
    "perinatal",
    "dialysis patients",
    "hemodialysis",
    "haemodialysis",
    "hiv patients",
    "aids patients",
    "囊性纤维化",
    "肿瘤患者",
    "孕妇",
    "妊娠期",
    "产后",
    "围产期",
    "透析患者",
    "hypertension",
    "高血压",
    "transplant",
    "移植",
    "clostridioides difficile infection",
    "艰难梭菌感染",
    "perianal cancer",
    "肛周癌",
    "scn8a",
    "cardiovascular risk",
    "心血管风险",
    "psychological stress",
    "心理应激",
    "extraintestinal manifestation",
    "肠外表现",
    "polycystic ovary",
    "polycystic ovarian",
    "多囊卵巢",
    "neurodegenerative",
    "dementia",
    "cognitive impairment",
    "stroke",
    "神经退行",
    "痴呆",
    "认知障碍",
    "脑卒中",
    "artificial intelligence",
    "machine learning",
    "wearable",
    "automated seizure",
    "genetic epilepsy",
    "genetic testing",
    "人工智能",
    "机器学习",
    "可穿戴",
    "自动发作检测",
    "遗传性癫痫",
    "遗传学检查",
    "gastroesophageal reflux",
    "reflux esophagitis",
    "反流性食管炎",
    "autoimmune encephalitis",
    "norse",
    "focal epilepsy",
    "brain atrophy",
    "自身免疫性脑炎",
    "局灶性癫痫",
    "脑萎缩",
    "chemotherapy",
    "rectal cancer",
    "化疗",
    "直肠癌",
    "malnutrition",
    "营养不良",
    "gene polymorphism",
    "基因多态性",
    "chronotropic incompetence",
    "变时性功能不全",
    "headache",
    "migraine",
    "thalamic",
    "thalamus",
    "头痛",
    "偏头痛",
    "丘脑",
    "oral contraceptive",
    "口服避孕药",
    "aortic dissection",
    "主动脉夹层",
    "non-alcoholic fatty liver",
    "nonalcoholic fatty liver",
    "非酒精性脂肪性肝病",
    "military",
    "flight personnel",
    "军事",
    "飞行人员",
    "anxious distress",
    "伴焦虑痛苦",
    "stereotactic",
    "stereo-electroencephalography",
    "surgical planning",
    "立体定向",
    "手术规划",
    "cardiovascular disease prevention",
    "心血管疾病的预防",
    "coronary heart disease",
    "冠心病",
    "type 2 diabetes",
    "diabetes",
    "2型糖尿病",
    "ii型糖尿病",
    "骨密度",
    "骨折",
    "bone density",
    "fracture",
    "valproate",
    "antiepileptic",
    "drug exposure",
    "丙戊酸",
    "抗癫痫药物",
    "药物暴露",
    "arrhythmia",
    "心律失常",
    "statin",
    "celecoxib",
    "adenoma prevention trial",
    "他汀",
    "塞来昔布",
    "腺瘤预防试验",
    "cvd prevention",
    "心血管预防",
    "hepatitis b",
    "hbv",
    "乙型肝炎",
    "乙肝",
    "insulin secretagogue",
    "胰岛素促泌剂",
    "促泌剂",
    "fecal microbial transplant",
    "faecal microbial transplant",
    "transplant failure",
    "donor-derived",
    "粪菌移植失败",
    "供者来源",
    "oral-gut",
    "therapeutic implications",
    "口肠轴",
    "治疗意义",
    "postherpetic neuralgia",
    "post-herpetic neuralgia",
    "hyperesthesia",
    "带状疱疹后神经痛",
    "感觉过敏",
    "psychotic symptoms",
    "psychotic depression",
    "精神病性症状",
    "精神病性抑郁",
    "chronic kidney disease",
    "ckd",
    "慢性肾病",
    "慢性肾脏病",
    "hereditary",
    "familial high risk",
    "genetic high risk",
    "遗传高风险",
    "家族高风险",
    "annotation",
    "影像标注",
    "ct和mri标注",
    "disease-modification trial",
    "expert opinion process",
    "早期阶段定义",
    "thalassemia",
    "地中海贫血",
    # A guideline may mention the requested disease while actually covering
    # its assessment only inside another special population or a research
    # protocol.  Such a record is not a routine confirmation pathway for the
    # requested target and must not reach the patient-facing report.
    "epilepsy",
    "癫痫",
    "clinical trials",
    "clinical trial",
    "临床试验",
    "acute coronary syndrome",
    "acute coronary syndromes",
    "急性冠脉综合征",
    "menopausal",
    "postmenopausal",
    "menopause",
    "绝经",
    "cancer patient",
    "cancer patients",
    "oncology patient",
    "癌症患者",
    "肿瘤患者",
    "stricture",
    "stenosis",
    "狭窄",
    "older persons",
    "older adults",
    "elderly",
    "老年",
    "stroke-related",
    "post-stroke",
    "stroke patients",
    "卒中相关",
    "卒中后",
    "sleep-related breathing",
    "sleep disordered breathing",
    "sleep apnea",
    "睡眠呼吸",
    "呼吸障碍",
    "hemodialysis",
    "haemodialysis",
    "dialysis",
    "血液透析",
    "透析患者",
    "hepatitis c",
    "hcv infection",
    "丙型肝炎",
    "丙肝",
    "non-alcoholic fatty liver",
    "nonalcoholic fatty liver",
    "fatty liver disease",
    "hepatic steatosis",
    "mri-pdff",
    "nafld",
    "masld",
    "脂肪肝",
    "肝脂肪",
    "eyelid myoclonia",
    "眼睑肌阵挛",
    "juvenile myoclonic",
    "青少年肌阵挛",
    "pediatric",
    "paediatric",
    "children",
    "childhood",
    "infant",
    "adolescent",
    "儿童",
    "小儿",
    "婴儿",
    "青少年",
    "pregnan",
    "gestational",
    "妊娠",
    "孕妇",
)

_NON_GUT_SITE_MARKERS = (
    "saliva",
    "salivary",
    "oral ",
    "mouth",
    "dental",
    "唾液",
    "口腔",
    "vaginal",
    "阴道",
    "skin ",
    "皮肤",
)

_HEALTH_INTERVENTION_MARKERS = (
    "diet",
    "dietary",
    "nutrition",
    "fiber",
    "exercise",
    "physical activity",
    "sleep",
    "bowel habit",
    "defecation",
    "stool frequency",
    "lifestyle",
    "膳食",
    "饮食",
    "营养",
    "纤维",
    "运动",
    "睡眠",
    "排便习惯",
    "排便",
    "生活方式",
)

_HEALTH_RESTRICTED_CONTEXT_MARKERS = (
    "hypertension",
    "cancer survivor",
    "survivorship",
    "diabetes",
    "obesity",
    "obese",
    "肥胖",
    "pregnant",
    "pregnancy",
    "antenatal",
    "pediatric",
    "children",
    "infant",
    "older adults",
    "sleep apnea",
    "covid",
    "coronavirus",
    "musculoskeletal",
    "高血压",
    "癌症",
    "糖尿病",
    "妊娠",
    "孕妇",
    "儿童",
    "婴儿",
    "老年",
    "睡眠呼吸暂停",
    "睡眠呼吸疾病",
    "新冠",
    "冠状病毒",
    "疫情期间",
    "肌骨",
    "戒毒",
    "物质依赖",
    "substance withdrawal",
    "drug rehabilitation",
)

_HEALTH_PRODUCT_STUDY_MARKERS = (
    "fortified",
    "extract",
    "supplement",
    "probiotic",
    "synbiotic",
    "capsule",
    "bread",
    "提取物",
    "补充剂",
    "益生菌",
    "胶囊",
    "中医",
    "中药",
    "治未病",
)

_ABUNDANCE_INTERVENTION_TITLE_MARKERS = (
    "randomized",
    "randomised",
    "controlled trial",
    "probiotic",
    "synbiotic",
    "supplement",
    "administration",
    "administered",
    "intervention",
    "treatment",
    "therapy",
    "efficacy",
    "alleviat",
    "reliev",
    "capsule",
    "yogurt",
    "益生菌",
    "合生元",
    "合生素",
    "补充",
    "干预",
    "治疗",
    "缓解",
    "改善",
    "胶囊",
    "酸奶",
)

_ASSOCIATION_MARKERS = (
    "associated",
    "association",
    "correlat",
    "enriched",
    "depleted",
    "depletion",
    "abundance",
    "over-represented",
    "risk",
    "prevalence",
    "coloniz",
    "pathogenic role",
    "pathogen",
    "infection",
    "caus",
    "biomarker",
    "linked",
    "identified",
    "检出",
    "相关",
    "富集",
    "丰度",
    "风险",
    "感染",
    "定植",
)


_DISEASE_ALIASES = {
    "肉毒中毒": ("botulism",),
    "军团菌病": ("legionnaires' disease", "legionnaires disease"),
    "细菌性痢疾": ("shigellosis", "bacillary dysentery"),
    "血性腹泻": ("bloody diarrhea", "bloody diarrhoea"),
    "弯曲菌病": ("campylobacteriosis",),
    "类丹毒": ("erysipeloid",),
    "结核病": ("tuberculosis",),
    "非结核分枝杆菌病": (
        "nontuberculous mycobacterial disease",
        "non-tuberculous mycobacterial disease",
        "non-tuberculosis mycobacterial disease",
        "non-tuberculosis mycobacterial (NTM) disease",
        "non tuberculosis mycobacterial disease",
        "NTM disease",
    ),
    "感染性腹泻": (
        "infectious diarrhea",
        "infectious diarrhoea",
        "diarrheal disease",
        "diarrhoeal disease",
        "foodborne diarrheal disease",
        "foodborne diarrhoeal disease",
    ),
    "胃肠炎": ("gastroenteritis",),
    "食物中毒": (
        "food poisoning",
        "foodborne illness",
        "food-borne illness",
        "food borne illness",
        "infectious diarrhea",
        "infectious diarrhoea",
        "acute gastroenteritis",
        "急性胃肠炎",
        "salmonellosis",
    ),
    "气性坏疽": ("gas gangrene",),
    "梭菌性肌坏死": ("clostridial myonecrosis",),
    "血流感染": ("bloodstream infection", "bloodstream infections"),
    "脓毒症": ("sepsis",),
    "念珠菌病": ("candidiasis",),
    "念珠菌血症": ("candidemia", "candidaemia"),
    "曲霉病": ("aspergillosis",),
    "隐球菌病": ("cryptococcosis",),
    "镰刀菌病": ("fusariosis",),
    "猪链球菌病": ("streptococcus suis disease",),
    "睡眠障碍": ("sleep disorder", "sleep disorders"),
    "结直肠癌": ("colorectal cancer", "rectal cancer", "colon cancer"),
    "溃疡性结肠炎": ("ulcerative colitis", "UC"),
    "硬化性胆管炎": ("sclerosing cholangitis",),
    "系统性红斑狼疮": ("systemic lupus erythematosus", "SLE"),
    "慢性肾病": ("chronic kidney disease", "CKD"),
    "II型糖尿病": ("type 2 diabetes", "T2DM", "2型糖尿病"),
    "克罗恩病": ("crohn disease", "crohn's disease"),
    "冠心病": ("coronary heart disease", "coronary artery disease"),
    "糖化血红蛋白": ("hemoglobin a1c", "HbA1c"),
    "腹胀和排气": ("abdominal bloating", "flatulence"),
    "胰岛素抵抗": ("insulin resistance",),
    "癫痫": ("epilepsy",),
    "炎症性肠病": ("inflammatory bowel disease", "IBD"),
    "非酒精性脂肪肝": (
        "nonalcoholic fatty liver disease",
        "non-alcoholic fatty liver disease",
        "NAFLD",
        "metabolic dysfunction-associated steatotic liver disease",
        "MASLD",
        "代谢相关脂肪性肝病",
    ),
    "肝硬化": ("liver cirrhosis", "cirrhosis"),
    "高尿酸血症": ("hyperuricemia", "hyperuricaemia"),
    "阿尔兹海默症": (
        "阿尔茨海默病",
        "阿尔茨海默症",
        "Alzheimer disease",
        "Alzheimer's disease",
    ),
    "帕金森病": ("Parkinson disease", "Parkinson's disease"),
    "多发性硬化症": ("multiple sclerosis",),
    "类风湿性关节炎": ("rheumatoid arthritis",),
    "强直性脊柱炎": ("ankylosing spondylitis",),
    "甲状腺癌": ("thyroid cancer", "thyroid carcinoma"),
    "焦虑症": ("anxiety disorder", "anxiety"),
    "精神分裂症": ("schizophrenia",),
    "腹泻型肠易激综合征": (
        "diarrhea-predominant irritable bowel syndrome",
        "IBS-D",
    ),
    "高脂血症": ("hyperlipidemia", "dyslipidemia"),
    "高血压": ("hypertension", "high blood pressure"),
    "自身免疫性肝炎": ("autoimmune hepatitis",),
    "肾结石": (
        "kidney stone",
        "kidney stones",
        "calcium oxalate stone",
        "calcium oxalate stones",
        "calcium oxalate kidney stone",
        "calcium oxalate kidney stones",
        "nephrolithiasis",
        "urolithiasis",
    ),
    "非结核分枝杆菌肺病": (
        "nontuberculous mycobacterial pulmonary disease",
        "nontuberculous mycobacterial lung disease",
        "NTM pulmonary disease",
        "Mycobacterium kansasii pulmonary disease",
        "M. kansasii pulmonary disease",
    ),
    "胃癌": ("gastric cancer", "stomach cancer"),
    "肝癌": ("liver cancer", "hepatocellular carcinoma"),
    "便秘": ("constipation",),
    "肠热病": ("肠热症", "enteric fever", "typhoid fever"),
    "肠热症": ("肠热病", "enteric fever", "typhoid fever"),
    "沙门菌病": (
        "沙门氏菌病",
        "salmonellosis",
        "肠热病",
        "肠热症",
        "enteric fever",
        "胃肠炎",
        "gastroenteritis",
    ),
    "腹泻": ("diarrhea", "diarrhoea"),
    "肥胖": ("obesity",),
    "抑郁症": ("depression", "depressive disorder"),
    "空腹血糖异常": (
        "impaired fasting glucose",
        "空腹血糖受损",
        "IFG",
    ),
    "腺瘤": ("colorectal adenoma", "intestinal adenoma"),
    "菌血症": ("bacteremia", "bacteraemia", "septicemia", "septicaemia"),
    "肺炎": ("pneumonia",),
    "社区获得性肺炎": ("community-acquired pneumonia", "pneumonia"),
    "心内膜炎": ("endocarditis",),
    "脑膜炎": ("meningitis",),
    "银屑病": ("psoriasis",),
    "银屑病关节炎": ("psoriatic arthritis",),
    # Source-report labels are not always the same clinical spelling used by
    # EviMed/PubMed.  Keep the original display name, but search and screen
    # with the canonical Chinese/English aliases so a valid source row is not
    # left empty merely because the provider used an umbrella term or a
    # spelling variant.
    "炎症性肠道疾病": (
        "inflammatory bowel disease",
        "IBD",
        "炎症性肠病",
    ),
    "肠易激综合症": (
        "irritable bowel syndrome",
        "IBS",
        "肠易激综合征",
    ),
    "肥胖症": ("obesity", "肥胖"),
    "心脑血管疾病": (
        "cardiovascular disease",
        "CVD",
        "cerebrovascular disease",
        "心血管疾病",
    ),
    "甲状腺疾病": ("thyroid disease", "thyroid disorders"),
    "高血脂": (
        "hyperlipidemia",
        "dyslipidemia",
        "hypercholesterolemia",
        "高脂血症",
    ),
    "高尿酸": ("hyperuricemia", "hyperuricaemia", "高尿酸血症"),
    "肾脏疾病": ("kidney disease", "renal disease", "chronic kidney disease"),
    "阿尔茨海默病": (
        "Alzheimer disease",
        "Alzheimer's disease",
        "阿尔兹海默症",
    ),
    "帕金森": ("Parkinson disease", "Parkinson's disease", "帕金森病"),
    "多发性硬化": ("multiple sclerosis", "多发性硬化症"),
    "酒精肝": (
        "alcohol-related liver disease",
        "alcoholic liver disease",
        "酒精性肝病",
    ),
}


_PREFERRED_CHINESE_DISEASE_TERMS = {
    "II型糖尿病": "2型糖尿病",
    "空腹血糖异常": "空腹血糖受损",
    "腹胀和排气": "腹胀 排气",
    "腺瘤": "结直肠腺瘤",
    "炎症性肠道疾病": "炎症性肠病",
    "肠易激综合症": "肠易激综合征",
    "肥胖症": "肥胖",
    "高血脂": "高脂血症",
    "高尿酸": "高尿酸血症",
    "帕金森": "帕金森病",
    "多发性硬化": "多发性硬化症",
}


_DISEASE_MEDICAL_SEARCH_HINTS = {
    "睡眠障碍": ("失眠", "嗜睡", "临床特征"),
    "溃疡性结肠炎": (
        "chronic inflammatory disease",
        "colon",
        "clinical features",
    ),
    "系统性红斑狼疮": (
        "systemic autoimmune multisystem disease",
        "clinical features",
    ),
    "炎症性肠病": ("chronic inflammatory disease", "clinical features"),
    "炎症性肠道疾病": ("chronic inflammatory disease", "clinical features"),
    "II型糖尿病": ("高血糖", "临床表现", "并发症"),
    "糖化血红蛋白": ("average glycemia", "clinical meaning"),
    "腹胀和排气": ("临床特征", "症状", "病因"),
    "胰岛素抵抗": ("病理生理", "代谢性疾病"),
    "癫痫": ("反复发作", "异常放电", "临床特征"),
    "抑郁症": ("心境低落", "兴趣减退", "临床表现"),
    "空腹血糖异常": ("糖尿病风险", "心血管风险"),
    "非酒精性脂肪肝": (
        "MASLD",
        "代谢相关脂肪性肝病",
        "肝脂肪变性",
    ),
    "帕金森病": ("运动迟缓", "静止性震颤", "临床特征"),
}


_DISEASE_DIAGNOSTIC_SEARCH_HINTS = {
    "炎症性肠病": ("colonoscopy", "fecal calprotectin", "biopsy"),
    "炎症性肠道疾病": ("colonoscopy", "fecal calprotectin", "biopsy"),
    "溃疡性结肠炎": (
        (
            "Role of biomarkers for the management of ulcerative colitis "
            "fecal calprotectin"
        ),
        "ACG Clinical Guideline Ulcerative Colitis in Adults",
        "fecal calprotectin guideline",
        "colonoscopy",
    ),
    "克罗恩病": ("CTE", "MRE", "结肠镜"),
    "非酒精性脂肪肝": ("肝脏超声", "FibroScan", "肝功能"),
    "肝硬化": ("肝脏弹性成像", "肝脏超声", "肝功能"),
    "高尿酸血症": ("血尿酸",),
    "阿尔兹海默症": ("MoCA", "MMSE", "神经心理评估"),
    "帕金森病": ("UPDRS", "神经系统检查"),
    "多发性硬化症": ("MRI", "脑脊液寡克隆带"),
    "类风湿性关节炎": ("类风湿因子", "抗CCP抗体", "关节超声"),
    "强直性脊柱炎": ("骶髂关节MRI", "HLA-B27"),
    "结直肠癌": ("colonoscopy", "fecal immunochemical test", "pathology biopsy"),
    "甲状腺癌": ("甲状腺超声", "细针穿刺"),
    "焦虑症": ("GAD-7", "结构化临床访谈"),
    "精神分裂症": ("结构化临床访谈", "PANSS"),
    "腹泻型肠易激综合征": ("罗马IV诊断标准", "布里斯托粪便分型"),
    "肥胖": ("BMI", "腰围"),
    "肥胖症": ("BMI", "腰围"),
    "高脂血症": ("血脂检测", "LDL-C"),
    "高血脂": ("血脂检测", "LDL-C"),
    "高血压": ("动态血压", "家庭血压"),
    "心脑血管疾病": ("血压测量", "血脂检测", "心电图"),
    # Put the routine functional assessment first.  The former generic
    # ``thyroid disease TSH diagnostic guideline`` query was dominated by
    # thyroid-cancer and unrelated guideline blocks in EviMed.  These scoped
    # phrases reliably retrieve sources that explicitly bind suspected
    # thyroid dysfunction to TSH/FT4 testing.  Structural imaging remains a
    # conditional follow-up rather than a blanket recommendation.
    "甲状腺疾病": (
        "thyroid disease 甲状腺功能异常 TSH FT4 诊断 指南",
        "thyroid dysfunction TSH FT4 guideline",
        "thyroid nodule ultrasound guideline",
    ),
    "高尿酸": ("血尿酸",),
    "阿尔茨海默病": ("MoCA", "MMSE", "神经心理评估"),
    "帕金森": ("UPDRS", "神经系统检查"),
    "多发性硬化": ("MRI", "脑脊液寡克隆带"),
    "酒精肝": ("肝脏超声", "肝功能"),
    "自身免疫性肝炎": ("自身抗体", "IgG", "肝活检"),
    "肾结石": (
        "ultrasound guideline",
        "noncontrast CT acute flank pain",
    ),
    "胃肠炎": ("stool culture", "gastrointestinal pathogen PCR"),
    "食物中毒": (
        "infectious diarrhea stool pathogen testing guideline",
        "infectious diarrhea stool culture guideline",
        "stool culture",
    ),
    "菌血症": ("blood cultures",),
    "艰难梭菌感染": ("GDH antigen toxin assay",),
    "胃癌": ("胃镜", "病理活检"),
    "肝癌": ("肝脏超声", "AFP", "增强MRI"),
    "睡眠障碍": ("sleep diary", "actigraphy", "polysomnography"),
    "系统性红斑狼疮": (
        "2019 EULAR ACR classification criteria ANA",
        "anti-dsDNA complement classification criteria",
    ),
    "慢性肾病": ("尿白蛋白", "估算肾小球滤过率", "肌酐"),
    "肾脏疾病": ("尿白蛋白", "估算肾小球滤过率", "肌酐"),
    "II型糖尿病": (
        "fasting plasma glucose",
        "HbA1c",
        "oral glucose tolerance test",
    ),
    "冠心病": ("coronary CT angiography", "exercise ECG"),
    "糖化血红蛋白": ("HbA1c",),
    "腹胀和排气": ("Rome IV", "symptom scale", "breath test"),
    "胰岛素抵抗": ("HOMA-IR", "高胰岛素正常血糖钳夹"),
    "癫痫": ("脑电图", "EEG"),
    "便秘": ("Bristol stool form scale", "colonic transit", "anorectal manometry"),
    "抑郁症": ("PHQ-9", "patient health questionnaire"),
    "空腹血糖异常": ("空腹血糖", "FPG"),
    "硬化性胆管炎": ("MRCP", "肝活检"),
    "腺瘤": ("colonoscopy", "pathology biopsy"),
}


_TAXON_SYNONYMS = {
    "bacteroides dorei": "Phocaeicola dorei",
    "bacteroides vulgatus": "Phocaeicola vulgatus",
    "clostridium bolteae": "Enterocloster bolteae",
    "clostridium hathewayi": "Hungatella hathewayi",
    "clostridium clostridioforme": "Enterocloster clostridioformis",
    "eubacterium hallii": "Anaerobutyricum hallii",
    "lactobacillus casei": "Lacticaseibacillus casei",
    "lactobacillus plantarum": "Lactiplantibacillus plantarum",
    "lactobacillus rhamnosus": "Lacticaseibacillus rhamnosus",
    # EviMed's Chinese corpus uses both current and historical Chinese taxon
    # spellings. These are nomenclature aliases only; they do not add a
    # disease relation or bypass the exact condition check.
    "产气荚膜梭菌": "Clostridium perfringens",
    "产气荚膜梭状芽胞杆菌": "Clostridium perfringens",
    "产气荚膜梭状芽孢杆菌": "Clostridium perfringens",
    "魏氏梭菌": "Clostridium perfringens",
    "福氏志贺菌": "Shigella flexneri",
    "福氏志贺氏菌": "Shigella flexneri",
    "福氏痢疾杆菌": "Shigella flexneri",
    "肠沙门氏菌": "Salmonella enterica",
    "肠沙门菌": "Salmonella enterica",
    "痢疾志贺菌": "Shigella dysenteriae",
    "痢疾志贺氏菌": "Shigella dysenteriae",
}


_TAXON_RELATION_SEARCH_HINTS: dict[
    str, tuple[tuple[str, str, EvidenceRelationType], ...]
] = {
    # These terms are retrieval seeds only.  They address common EviMed/PubMed
    # indexing forms that are not reliably discovered from a taxon-only first
    # pass.  No row is published unless the returned source still binds the
    # exact taxon, outcome, human context and (for abundance findings) the
    # source-report direction through the ordinary validators below.
    "streptococcus thermophilus": (
        (
            "结直肠癌",
            "Streptococcus thermophilus depleted colorectal cancer",
            "epidemiologic_association",
        ),
    ),
    "clostridium butyricum": (
        (
            "炎症性肠病",
            "Clostridium butyricum depleted inflammatory bowel disease",
            "epidemiologic_association",
        ),
    ),
    "bifidobacterium adolescentis": (
        (
            "炎症性肠病",
            "Bifidobacterium adolescentis depleted inflammatory bowel disease",
            "epidemiologic_association",
        ),
    ),
    "oxalobacter formigenes": (
        (
            "肾结石",
            "Oxalobacter formigenes colonization risk calcium oxalate stones adults",
            "epidemiologic_association",
        ),
    ),
    "phocaeicola vulgatus": (
        (
            "溃疡性结肠炎",
            (
                "Multi-omics analyses ulcerative colitis Bacteroides vulgatus "
                "proteases disease severity"
            ),
            "epidemiologic_association",
        ),
    ),
    "veillonella parvula": (
        (
            "结直肠癌",
            "Veillonella parvula colorectal cancer microbiome",
            "epidemiologic_association",
        ),
    ),
    "pseudomonas putida": (
        (
            "菌血症",
            "Pseudomonas putida human infection septicemia",
            "opportunistic",
        ),
    ),
    "salmonella": (
        ("沙门菌病", "Salmonella infection", "established_pathogen"),
        ("胃肠炎", "Salmonella gastroenteritis", "established_pathogen"),
        ("菌血症", "Salmonella bacteremia", "established_pathogen"),
    ),
    "mycobacterium kansasii": (
        (
            "非结核分枝杆菌肺病",
            "Mycobacterium kansasii pulmonary disease",
            "established_pathogen",
        ),
    ),
    "clostridium perfringens": (
        ("食物中毒", "food poisoning", "established_pathogen"),
    ),
    "salmonella enterica": (
        ("沙门菌病", "Salmonella enterica infection", "established_pathogen"),
        ("胃肠炎", "gastroenteritis", "established_pathogen"),
        ("肠热病", "enteric fever", "established_pathogen"),
    ),
    "campylobacter coli": (
        ("感染性腹泻", "diarrhoeal disease", "established_pathogen"),
        ("胃肠炎", "gastroenteritis", "established_pathogen"),
    ),
    "shigella flexneri": (
        ("细菌性痢疾", "bacillary dysentery", "established_pathogen"),
    ),
    "shigella dysenteriae": (
        (
            "细菌性痢疾",
            "Shigella dysenteriae causes shigellosis humans",
            "established_pathogen",
        ),
    ),
    "clostridium botulinum": (("肉毒中毒", "botulism", "established_pathogen"),),
    "plesiomonas shigelloides": (
        ("胃肠炎", "gastroenteritis", "established_pathogen"),
    ),
    "streptococcus suis": (
        ("脑膜炎", "meningitis", "established_pathogen"),
        ("猪链球菌病", "Streptococcus suis disease", "established_pathogen"),
    ),
    "treponema denticola": (("牙周炎", "periodontitis", "epidemiologic_association"),),
    "aeromonas hydrophila": (
        ("胃肠炎", "causes human gastroenteritis adults", "opportunistic"),
    ),
    "aeromonas caviae": (("胃肠炎", "gastroenteritis", "opportunistic"),),
    "enterobacter cloacae": (("血流感染", "bloodstream infection", "opportunistic"),),
    "streptococcus pneumoniae": (
        ("肺炎球菌病", "pneumococcal disease", "established_pathogen"),
    ),
    "clostridioides difficile": (
        ("艰难梭菌感染", "Clostridioides difficile infection", "established_pathogen"),
    ),
    "pseudomonas aeruginosa": (
        ("肺炎", "pneumonia", "opportunistic"),
        ("血流感染", "bloodstream infection", "opportunistic"),
    ),
    "candida albicans": (("念珠菌病", "candidiasis", "opportunistic"),),
    "candida": (("念珠菌病", "candidiasis", "opportunistic"),),
    "aspergillus": (("曲霉病", "aspergillosis", "opportunistic"),),
    "mycobacterium tuberculosis": (
        ("结核病", "tuberculosis", "established_pathogen"),
    ),
    "cryptococcus": (("隐球菌病", "cryptococcosis", "opportunistic"),),
    "fusarium": (("镰刀菌病", "fusariosis", "opportunistic"),),
}
