"""Reviewed supplied literature for matched mechanisms, not score validation.

Only verified bibliographic data is deployed. The source-file ledger is
internal provenance; generation never opens an external literature directory.
Other diseases keep their existing scoped sources and clinical guidelines.
"""
from __future__ import annotations

from copy import deepcopy


VERSION = "gut-barrier-reference-priority-v1.20261007"

_SOURCES = {
    "ibs_primer": {
        "citation": "Enck P, Aziz Q, Barbara G, et al. Irritable bowel syndrome.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/27159638/",
        "year": 2016,
        "publisher": "Nature Reviews Disease Primers",
        "doi": "10.1038/nrdp.2016.14",
        "location": "2:16014",
        "evidenceType": "clinical_narrative_review",
        "providedFile": "肠漏和英文文献/英文文献/Irritable Bowel Syndrome. .pdf",
        "sha256": "848de682460509bdd8980126c175a2f67a27c26d076a4c2508c3bc59f8b52456",
        "reviewedPages": [5, 6],
        "scope": "部分IBS患者的上皮通透性、黏膜免疫与内脏敏感性；不是三项血液指标诊断模型",
    },
    "ibd_human": {
        "citation": "Chang J, Leong RW, Wasinger VC, et al. Impaired Intestinal Permeability Contributes to Ongoing Bowel Symptoms in Patients With Inflammatory Bowel Disease and Mucosal Healing.",
        "url": "https://pubmed.ncbi.nlm.nih.gov/28601482/",
        "year": 2017,
        "publisher": "Gastroenterology",
        "doi": "10.1053/j.gastro.2017.05.056",
        "location": "153(3):723-731.e1",
        "evidenceType": "human_prospective_observational_study",
        "providedFile": "肠漏和英文文献/英文文献/Impaired Intestinal Permeability Contributes to Ongoing Bowel Symptoms in Patients With Inflammatory Bowel Disease and Mucosal Healing.pdf",
        "sha256": "3a803c86b6e005e869dc5e5324b829685db48dedb6cdb119d0635d08a78f2369",
        "reviewedPages": [1, 2, 7],
        "scope": "110名成人（88名既有IBD、22名对照）；不验证新发IBD预测或本报告评分权重",
    },
    "barrier_review": {
        "citation": "Fukui H. Increased Intestinal Permeability and Decreased Barrier Function: Does It Really Influence the Risk of Inflammation?",
        "url": "https://pubmed.ncbi.nlm.nih.gov/29922669/",
        "year": 2016,
        "publisher": "Inflammatory Intestinal Diseases",
        "doi": "10.1159/000447252",
        "location": "1(3):135-145",
        "evidenceType": "clinical_and_experimental_narrative_review",
        "providedFile": "肠漏和英文文献/英文文献/Increased Intestinal Permeability and Decreased Barrier Function Does It Really Influence the Risk of Inflammation.pdf",
        "sha256": "fc716976171837803c9738f9d42532cb09893a1e9df3375f9e8c666f5c3f2dab",
        "reviewedPages": [1, 3, 5, 8],
        "scope": "IBD与脂肪性肝病的屏障及炎症机制；不证明统一因果关系、个体患病概率或评分系数",
    },
}

_MECHANISMS = {
    "ibs": {
        "text": "部分肠易激综合征患者的肠黏膜通透性增加，与黏膜免疫激活及内脏敏感性变化相关，可参与腹痛、腹泻等症状的形成。",
        "sources": ("ibs_primer",),
        "additionalEvidenceIds": ("Q33",),
    },
    "ibd": {
        "text": "部分炎症性肠病患者存在肠道通透性增加及黏膜免疫变化，屏障功能异常也可能与黏膜愈合后持续的肠道症状相关。",
        "sources": ("barrier_review", "ibd_human"),
        "additionalEvidenceIds": ("Q32",),
    },
    "nafld": {
        "text": "肠道屏障功能减弱可能增加细菌产物进入肝脏的机会，并通过TLR4等炎症信号参与脂肪性肝病相关炎症。",
        "sources": ("barrier_review",),
        "additionalEvidenceIds": ("Q35",),
    },
}


def preferred_mechanism(code):
    """Return an isolated, scope-matched selection; never infer risk grades."""
    selected = _MECHANISMS.get(code)
    if selected is None:
        return None
    return {
        "text": selected["text"],
        "sources": [deepcopy(_SOURCES[key]) for key in selected["sources"]],
        "additionalEvidenceIds": list(selected["additionalEvidenceIds"]),
    }


def reference_selection_audit():
    """Private provenance only; not a public report or callback field."""
    return {
        "version": VERSION,
        "reviewDate": "2026-10-07",
        "mode": "matched_mechanism_only_not_score_validation",
        "sources": deepcopy(_SOURCES),
        "diseaseCodes": list(_MECHANISMS),
    }
