"""Conservative condition discovery from EviMed titles and summaries.

The results produced here are search seeds only.  They must never be exposed
as public evidence until a separate exact taxon-condition query has returned
and passed the normal relation validation pipeline.
"""

from __future__ import annotations

import re

from modules.report.schemas import EvidenceCondition, EvidenceRecord

_CONDITION_TERMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("肉毒中毒", ("botulism", "肉毒中毒")),
    ("军团菌病", ("legionnaires' disease", "legionnaires disease", "军团菌病")),
    ("细菌性痢疾", ("shigellosis", "bacillary dysentery", "细菌性痢疾")),
    ("血性腹泻", ("bloody diarrhea", "bloody diarrhoea", "血性腹泻")),
    ("弯曲菌病", ("campylobacteriosis", "弯曲菌病")),
    (
        "非结核分枝杆菌病",
        (
            "nontuberculous mycobacterial disease",
            "non-tuberculous mycobacterial disease",
            "non-tuberculosis mycobacterial disease",
            "non-tuberculosis mycobacterial (NTM) disease",
            "non tuberculosis mycobacterial disease",
            "NTM disease",
            "非结核分枝杆菌病",
        ),
    ),
    ("结核病", ("tuberculosis", "结核病")),
    ("感染性腹泻", ("infectious diarrhea", "infectious diarrhoea", "感染性腹泻")),
    ("胃肠炎", ("gastroenteritis", "胃肠炎")),
    (
        "食物中毒",
        (
            "food poisoning",
            "foodborne illness",
            "food-borne illness",
            "food borne illness",
            "食物中毒",
        ),
    ),
    ("气性坏疽", ("gas gangrene", "气性坏疽")),
    ("梭菌性肌坏死", ("clostridial myonecrosis", "梭菌性肌坏死")),
    ("菌血症", ("bacteremia", "bacteraemia", "菌血症")),
    ("血流感染", ("bloodstream infection", "bloodstream infections", "血流感染")),
    ("心内膜炎", ("endocarditis", "心内膜炎")),
    ("脓毒症", ("sepsis", "脓毒症")),
    ("脑膜炎", ("meningitis", "脑膜炎")),
    (
        "下呼吸道感染",
        (
            "lower respiratory tract infection",
            "lower respiratory infection",
            "下呼吸道感染",
        ),
    ),
    (
        "皮肤和软组织感染",
        (
            "skin and soft tissue infection",
            "skin and soft tissue infections",
            "皮肤和软组织感染",
        ),
    ),
    ("脓疱病", ("impetigo", "脓疱病", "脓疱疮")),
    ("脓肿", ("abscess", "abscesses", "脓肿")),
    ("类丹毒", ("erysipeloid", "类丹毒")),
    ("肺炎", ("pneumonia", "肺炎")),
    (
        "艰难梭菌感染",
        (
            "clostridioides difficile infection",
            "clostridium difficile infection",
            "c. difficile infection",
            "艰难梭菌感染",
        ),
    ),
    ("念珠菌病", ("candidiasis", "念珠菌病")),
    ("念珠菌血症", ("candidemia", "candidaemia", "念珠菌血症")),
    ("曲霉病", ("aspergillosis", "曲霉病")),
    ("隐球菌病", ("cryptococcosis", "隐球菌病")),
    ("镰刀菌病", ("fusariosis", "镰刀菌病")),
    ("猪链球菌病", ("streptococcus suis disease", "猪链球菌病")),
    (
        "结直肠癌",
        ("colorectal cancer", "colon cancer", "rectal cancer", "结直肠癌"),
    ),
    (
        "炎症性肠病",
        ("inflammatory bowel disease", "IBD", "炎症性肠病"),
    ),
    ("溃疡性结肠炎", ("ulcerative colitis", "溃疡性结肠炎")),
    ("克罗恩病", ("crohn disease", "crohn's disease", "克罗恩病")),
    ("牙周炎", ("periodontitis", "periodontal disease", "牙周炎")),
)

_CAUSAL_MARKERS = (
    "causative agent",
    "causative pathogen",
    "caused by",
    "cause of",
    "causing ",
    "causes ",
    "due to",
    "responsible for",
    "toxin produced by",
    "neurotoxin produced by",
    "pathogen",
    "pathogenic",
    "引起",
    "导致",
    "病原",
    "致病",
)

_ASSOCIATION_MARKERS = (
    "associated with",
    "association with",
    "associated",
    "association",
    "相关",
    "关联",
)


def source_mentions_taxon(
    source: str,
    scientific_name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> bool:
    """Return whether source text explicitly names the requested taxon."""
    scientific_name = " ".join(scientific_name.split()).strip()
    if not scientific_name:
        return False
    for alias in dict.fromkeys((scientific_name, *aliases)):
        alias = " ".join(alias.split()).strip()
        if alias and re.search(re.escape(alias), source, flags=re.IGNORECASE):
            return True
    parts = scientific_name.split()
    if len(parts) >= 2:
        genus, species = parts[0], parts[1]
        return bool(
            re.search(
                rf"(?<![A-Za-z]){re.escape(genus[0])}\.?\s*{re.escape(species)}(?![A-Za-z])",
                source,
                flags=re.IGNORECASE,
            )
        )
    return bool(
        re.search(
            rf"(?<![A-Za-z]){re.escape(scientific_name)}(?![A-Za-z])",
            source,
            flags=re.IGNORECASE,
        )
    )


def source_derived_conditions(
    record: EvidenceRecord,
) -> tuple[list[EvidenceCondition], str | None]:
    """Extract explicit outcomes for exact-query seeding, never publication."""
    source = f"{record.title} {record.summary or ''}".strip()
    lowered = source.casefold()
    # "pathogenesis" describes disease mechanisms, not a pathogen claim.
    # Match English causal words rather than arbitrary word substrings.
    causal = any(
        bool(re.search(
            r"(?<![a-z])" + (
                r"pathogens?" if marker == "pathogen" else re.escape(marker.strip())
            ) + r"(?![a-z])",
            lowered,
        )) if marker.isascii() else marker in lowered
        for marker in _CAUSAL_MARKERS
    )
    associated = any(marker in lowered for marker in _ASSOCIATION_MARKERS)

    target = ""
    target_aliases: tuple[str, ...] = ()
    for item in record.targets:
        components = tuple(
            component.strip() for component in item.split("/") if component.strip()
        )
        latin_target = next(
            (
                component
                for component in components
                if re.fullmatch(
                    r"[A-Z][A-Za-z.-]+(?:\s+[a-z][A-Za-z.-]+)?",
                    component,
                )
            ),
            "",
        )
        if latin_target and source_mentions_taxon(
            source,
            latin_target,
            aliases=tuple(
                component for component in components if component != latin_target
            ),
        ):
            target = latin_target
            target_aliases = tuple(
                component for component in components if component != latin_target
            )
            break
    if not target:
        return [], None

    # A consensus specifically titled for ``<exact taxon> infection`` can
    # establish a cautious organism-condition association even when the
    # abstract is written entirely around diagnostic use and therefore lacks
    # a causal verb.  This is intentionally narrow: the taxon must be directly
    # followed by ``infection/感染`` in the title and the title must describe a
    # clinical diagnostic/testing document.  An unrelated organism antigen
    # used in another disease's serology will not satisfy this gate.
    normalized_title = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", record.title.casefold())
    organism_infection_title = any(
        (normalized_alias := re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", alias.casefold()))
        and any(
            f"{normalized_alias}{suffix}" in normalized_title
            for suffix in ("infection", "感染")
        )
        for alias in (target, *target_aliases)
    ) and any(
        marker in record.title.casefold()
        for marker in ("diagnos", "test", "detect", "诊断", "检测", "检验")
    )
    # A short clinical title that contains the exact requested taxon and a
    # concrete disease is useful as a *search seed* even when it omits a verb,
    # for example ``Pseudomonas aeruginosa pneumonia``.  It is not public
    # evidence until a separate exact relation query passes all later gates.
    non_relation_research_title = any(
        marker in record.title.casefold()
        for marker in (
            " antigen ",
            " serology",
            "serological",
            " assay",
            " primer",
            " biomarker",
            "抗原",
            "血清学",
            "检测方法",
        )
    )
    concise_title_relation = bool(
        len(record.title) <= 240
        and not non_relation_research_title
        and source_mentions_taxon(
            record.title,
            target,
            aliases=target_aliases,
        )
        and any(
            re.search(re.escape(alias), record.title, flags=re.IGNORECASE)
            for _name, aliases in _CONDITION_TERMS
            for alias in aliases
        )
    )
    if (
        not causal
        and not associated
        and not organism_infection_title
        and not concise_title_relation
    ):
        return [], None
    associated = associated or organism_infection_title

    relation_type = "established_pathogen" if causal else "epidemiologic_association"
    results: list[EvidenceCondition] = []
    for chinese_name, aliases in _CONDITION_TERMS:
        matched = next(
            (
                match.group(0)
                for alias in aliases
                if (match := re.search(re.escape(alias), source, flags=re.IGNORECASE))
            ),
            None,
        )
        if not matched:
            continue
        results.append(
            EvidenceCondition(
                name=chinese_name,
                search_term=matched,
                source_phrase=matched,
                relation_type=relation_type,
            )
        )
        if len(results) >= 3:
            break
    if not results:
        return [], None
    names = "、".join(condition.name for condition in results)
    if causal:
        return results, f"文献明确将{target}描述为{names}的相关病原菌。"
    return results, f"文献报道{target}与{names}存在相关性。"
