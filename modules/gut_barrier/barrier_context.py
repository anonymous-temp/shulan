"""Accepted barrier observations and bounded, patient-facing mechanisms.

Study associations support mechanisms, not personal probabilities or weights.
The 3/4/5 strengths are an operational attention policy for the project's
existing mild/moderate/severe marker categories, not a clinical risk model.
"""
from __future__ import annotations

from copy import deepcopy
from math import isfinite

from .risk_model import mechanism_evidence


BARRIER_REFERENCES = {
    "Q32": {
        "citation": "Diagnostic and Clinical Significance of Serum Levels of D-Lactate and Diamine Oxidase in Patients with Crohn's Disease",
        "year": 2019, "publisher": "Gastroenterology Research and Practice",
        "doi": "10.1155/2019/8536952", "url": "https://pubmed.ncbi.nlm.nih.gov/31531016/",
        "location": "59例已诊断克罗恩病患者与28例对照的观察研究：D-乳酸、DAO与疾病活动相关；不用于普通人群确诊、病因归属或数值风险权重",
    },
    "Q33": {
        "citation": "Mucosal mast cell count is associated with intestinal permeability in patients with diarrhea predominant irritable bowel syndrome",
        "year": 2013, "publisher": "Journal of Neurogastroenterology and Motility",
        "doi": "10.5056/jnm.2013.19.2.244", "url": "https://pubmed.ncbi.nlm.nih.gov/23667756/",
        "location": "16例IBS-D患者与7例对照的黏膜观察研究：通透性与肥大细胞数关联；不从屏障血液指标确诊IBS或计算概率",
    },
    "Q34": {
        "citation": "Lipopolysaccharide activates an innate immune system response in human adipose tissue in obesity and type 2 diabetes",
        "year": 2007, "publisher": "American Journal of Physiology-Endocrinology and Metabolism",
        "doi": "10.1152/ajpendo.00302.2006", "url": "https://pubmed.ncbi.nlm.nih.gov/17090751/",
        "location": "人体脂肪组织、细胞实验与病例比较：LPS相关先天免疫信号及细胞因子；不是普通人群前瞻性因果预测或本报告评分权重",
    },
    "Q35": {
        "citation": "Increased intestinal permeability and tight junction alterations in nonalcoholic fatty liver disease",
        "year": 2009, "publisher": "Hepatology", "doi": "10.1002/hep.22848",
        "url": "https://onlinelibrary.wiley.com/doi/10.1002/hep.22848",
        "location": "35例活检证实NAFLD患者、27例乳糜泻及24例健康对照：通透性与脂肪变关联；采用尿51Cr-EDTA及组织检查，不可替代D-乳酸/DAO/LPS个体疾病概率验证",
    },
}

BARRIER_LINKED_CODES = {
    "intestinal_barrier_followup": ("Q32", "Q33"),
    "metabolic_barrier_followup": ("Q34", "Q35"),
    "ibs": ("Q33",), "ibd": ("Q32",), "nafld": ("Q35",),
    "type2_diabetes": ("Q34",), "obesity": ("Q34",),
}

# Catalog-only sentences (notably documented diagnosis follow-up) must cite
# their mechanism studies, not merely the clinical management guideline.
_CATALOG_EVIDENCE = mechanism_evidence()
_STUDIES = {study["id"]: study for row in _CATALOG_EVIDENCE for study in row["studies"]}
_STUDY_IDS = {key: f"Q{100 + index}" for index, key in enumerate(_STUDIES)}
CATALOG_MECHANISM_REFERENCES = {
    _STUDY_IDS[key]: {
        "citation": study["title"], "year": study["year"], "publisher": study["publisher"],
        "doi": study["doi"], "url": study["url"],
        "location": study["population"] + "；" + study["scope"] + "；仅支持机制，不提供个体概率或数值权重",
    } for key, study in _STUDIES.items()
}


def barrier_features(assessment):
    """Individual exact abnormalities survive an unresolved aggregate endpoint.

    Do not reconstruct disputed scores, assign a degree to comparison values,
    or treat three related markers as three independent scoring domains.
    """
    accepted = []
    for marker in assessment.get("markers", []):
        if marker.get("code") not in {"D_LACTATE", "DAO", "LPS"}:
            continue
        value = marker.get("value")
        if (marker.get("status") != "evaluated" or marker.get("abnormal") is not True
                or marker.get("operator") not in {None, "="}
                or isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) or value < 0
                or marker.get("level") not in {"轻度", "中度", "重度"}
                or marker.get("reasons")):
            continue
        accepted.append(marker)
    if not accepted:
        return {"barrierFact": None, "barrierMarkers": [], "barrierSummary": ""}
    strength = max({"轻度": 3, "中度": 4, "重度": 5}[row["level"]] for row in accepted)
    summary = "、".join(f"{row['name']} {row['value']:g} {row['unit']}（{row['level']}升高）" for row in accepted)
    return {
        "barrierFact": {"domain": "intestinal_barrier", "strength": strength,
                        "longitudinal": 0, "sourceType": "objective_current",
                        "location": "markers accepted current exact D_LACTATE/DAO/LPS",
                        "markers": [{key: deepcopy(row.get(key)) for key in
                                     ("code", "name", "value", "unit", "level", "source")} for row in accepted]},
        "barrierMarkers": [row["code"] for row in accepted], "barrierSummary": summary,
    }


_MECHANISMS = {
    "obesity": "体脂积聚与能量代谢失衡相互影响，并可伴随脂肪组织炎症和胰岛素敏感性下降。",
    "nafld": "体重相关的代谢负荷可促进脂肪在肝脏积聚，并与胰岛素抵抗相互影响。",
    "type2_diabetes": "体重相关代谢负荷可降低胰岛素敏感性，增加糖代谢异常的可能性。",
    "hyperlipidemia": "脂质生成、转运及清除失衡可造成血脂升高，并增加血管壁脂质沉积。",
    "hypertension": "体重相关的交感神经及激素调节变化可增加血管阻力，影响血压调节。",
    "atherosclerosis": "脂质积聚与血管壁炎症相互作用，参与动脉粥样硬化斑块形成。",
    "ibs": "肠道感觉和动力调节异常可引起腹痛与排便改变，并可能伴随黏膜通透性及免疫反应变化。",
    "ibd": "肠黏膜免疫调节异常与屏障损伤相互影响，可参与持续或反复的肠道炎症。",
    "sleep_apnea": "颈部及上气道周围脂肪积聚可增加睡眠时气道塌陷的可能性，造成反复呼吸受阻。",
    "gastritis_followup": "胃黏膜炎症及黏膜防御受损可引起局部损伤，与上腹不适等表现相关。",
    "kidney_function_evaluation": "肾小球滤过或屏障功能异常，可表现为滤过率下降或尿白蛋白增加。",
    "intestinal_barrier_followup": "肠道黏膜屏障功能改变与局部炎症及消化道功能紊乱相关。",
    "metabolic_barrier_followup": "肠道屏障改变可能增加肠源性产物与机体的接触，并通过炎症反应影响胰岛素敏感性和糖脂代谢。",
    "digestive_lesion_followup": "消化道局灶性病变可表现为壁内或腔内结节，不同病变的组织来源与生物学行为存在差异。",
    "urinary_stone_followup": "尿液中成石物质过饱和及结晶沉积参与结石形成，结石可刺激尿路黏膜或影响尿液引流。",
}

_LINKED_MECHANISMS = {
    "obesity": "肠道屏障变化、肠源性产物相关炎症与体脂积聚及胰岛素敏感性下降可能相互关联。",
    "nafld": "肠道屏障通透性变化与肝脏脂质积聚和代谢负荷相关，涉及肠肝轴的相互作用。",
    "type2_diabetes": "肠道屏障变化及肠源性产物相关炎症可能参与胰岛素敏感性下降和糖代谢异常。",
    "ibs": "黏膜屏障通透性与免疫反应变化可能参与腹痛及排便习惯改变。",
    "ibd": "肠道屏障损伤与黏膜免疫反应可能相互影响，并参与肠道炎症。",
}

# Preserve these catalog claims and their original mechanism references while
# removing research-purpose or assessment-language openings from the cell.
_CATALOG_MECHANISMS = {
    "alzheimers": "认知受损老年人研究中，特定菌群及外周炎症变化与脑淀粉样蛋白沉积相关。",
    "depression": "部分抑郁或焦虑人群研究观察到血浆 LPS、zonulin 与 FABP2 升高并与菌群改变相关，可能涉及屏障、外周炎症及肠脑轴的相互作用。",
    "systemic_lupus": "遗传易感动物中，特定肠道共生菌移位可激活适应性免疫和自身抗体反应，部分自身免疫患者组织中也检出相关细菌。",
}


def patient_mechanism(code, *, barrier_linked=False, catalog_mechanism=None):
    if barrier_linked and code in _LINKED_MECHANISMS:
        return _LINKED_MECHANISMS[code]
    if code in _MECHANISMS:
        return _MECHANISMS[code]
    if code in _CATALOG_MECHANISMS:
        return _CATALOG_MECHANISMS[code]
    # Documented diagnosis follow-up retains the catalog's first mechanistic
    # sentence, rather than replacing it with internal evaluation instructions.
    if catalog_mechanism:
        return catalog_mechanism.split("。")[0].strip() + "。"
    return "疾病相关的组织损伤与功能变化，可共同影响症状表现和长期健康状态。"


def catalog_mechanism_reference_ids(code):
    if code in _MECHANISMS:
        return []
    return [_STUDY_IDS[study["id"]] for row in _CATALOG_EVIDENCE if row["code"] == code
            for study in row["studies"]]
