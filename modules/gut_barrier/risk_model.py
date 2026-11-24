"""Versioned disease directory, deliberately not a disease prediction model.

The supplied report defines a display catalog and discusses possible mechanisms,
but its example indices do not define coefficients, calibration or a questionnaire.
Nothing in this module derives a patient score or imports an upstream NGS index.
"""

from __future__ import annotations

from copy import deepcopy


DISEASE_CATALOG_VERSION = "gut-barrier-disease-catalog-v2.7.20261009"
DISEASE_CATALOG_SOURCE = {
    "name": "张学斌肠道屏障功能与慢性炎症综合评估报告(1).docx",
    "sha256": "8c593463f065739de1eeac0f7c6c1d347f8a4f4fee2146a552da40c331a58e69",
    "location": "第三章表4–11：病种与机制内容，不采用示例指数",
    "status": "provided_reference_not_validated_prediction_model",
}

# General mechanisms, not statements that a pathway was measured in this person.
# Primary study scope is recorded below; source example indices are never read.
_SYSTEMS = (
    ("digestive", "消化系统", 4, (
        ("nafld", "非酒精性脂肪性肝病", "肠道吸收的细菌产物可经门静脉进入肝脏。实验研究中，LPS–TLR4 信号促进肝内炎症反应，并增强肝星状细胞对 TGF-β 的纤维化反应；这一肠–肝轴过程可与脂质代谢异常相互影响。"),
        ("ibs", "肠易激综合征", "部分腹泻型肠易激综合征患者存在黏膜通透性增加，并与黏膜肥大细胞增多相关。黏膜免疫激活与内脏敏感性变化，为腹痛、腹胀和排便习惯改变提供机制解释。"),
        ("ibd", "炎症性肠病", "黏液层和上皮连接保护减弱，可增加肠腔微生物与黏膜免疫系统的接触；免疫调节异常又可反向损伤屏障。在易感背景下，两者共同参与持续或反复的肠道炎症。"),
        ("cirrhosis", "肝硬化", "在慢性肝损伤背景下，细菌产物相关的 TLR4–MyD88–NF-κB 信号可增强 TGF-β 促纤维化作用，涉及肝星状细胞激活及胶原沉积。它说明炎症与纤维化的联系，不等同于本次已发现肝硬化。"),
        ("intestinal_barrier_followup", "肠道炎症与功能紊乱", "肠道黏膜屏障功能改变与局部炎症及消化道功能紊乱相关。"),
    )),
    ("metabolic", "代谢系统", 5, (
        ("type2_diabetes", "2型糖尿病", "LPS 相关先天免疫信号可促使脂肪组织释放 TNF-α、IL-6 等炎症介质，与胰岛素敏感性下降相联系。动物实验和人体脂肪组织研究支持代谢性炎症这一环节，个体糖代谢状态仍由血糖等资料评价。"),
        ("obesity", "肥胖", "脂肪组织不仅储存能量，也参与免疫反应。LPS 相关信号可增加脂肪组织炎症介质释放；动物研究中伴随脂肪组织免疫细胞聚集及体重增加，形成代谢负荷与炎症相互影响的机制线索。"),
        ("hyperlipidemia", "高脂血症", "炎症反应可改变脂肪酸动员及富含甘油三酯脂蛋白的代谢；人体低剂量内毒素刺激研究观察到相关脂质变化。实际 LDL-C、总胆固醇和甘油三酯结果，应与这一通用机制分开判断。"),
        ("hyperuricemia", "高尿酸血症", "尿酸除经肾脏排泄外，也经肠道排泄；ABCG2 等转运功能变化可影响肾外尿酸清除。炎症与代谢异常可同时存在，但屏障三项升高并不能直接证明尿酸生成过多或排泄障碍。"),
    )),
    ("cardiovascular", "心脑血管系统", 6, (
        ("atherosclerosis", "动脉粥样硬化", "动物研究中，TLR4/MyD88 免疫信号与血管壁炎症及斑块形成相关。"),
        ("coronary_heart_disease", "冠心病", "血管壁炎症与脂质负荷共同参与动脉粥样硬化，局部免疫细胞活化与斑块进展相关。"),
        ("hypertension", "高血压", "炎症介质与血管内皮功能及重构相联系。实验研究中，血管紧张素Ⅱ诱导的 IL-6 信号可促进内皮素相关表达及肾脏纤维化；这一过程与血压调节有关，但不替代规范血压测量。"),
        ("stroke", "脑卒中", "部分缺血性卒中与动脉粥样硬化及血栓形成相关；血管壁炎症是斑块形成机制中的一个环节。"),
    )),
    ("neurological", "神经系统", 7, (
        ("alzheimers", "阿尔茨海默病", "肠–脑轴研究关注菌群、外周炎症与中枢淀粉样蛋白相关改变之间的联系。认知受损老年人研究观察到特定菌群及外周炎症标志物与脑淀粉样沉积相关，但尚不能由屏障三指标预测个体认知疾病。"),
        ("parkinsons", "帕金森病", "在 α-突触核蛋白过表达小鼠中，菌群及其代谢物可影响小胶质细胞活化、神经炎症和运动表现。这为肠道信号参与神经退行过程提供实验线索，不等于已在人群中建立相同的风险计算关系。"),
    )),
    ("psychiatric", "精神系统", 8, (
        ("depression", "抑郁症", "部分抑郁或焦虑人群研究观察到血浆 LPS、zonulin 与 FABP2 升高，并与菌群改变相关，提示屏障与外周炎症是肠–脑轴的研究环节。这类关联不等同于情绪障碍已被检测确诊。"),
        ("anxiety", "焦虑症", "肠道感觉、免疫信号及微生物代谢与脑功能相互联系。将部分肠易激综合征患者菌群移植至无菌小鼠后，研究观察到屏障改变、先天免疫激活及焦虑样行为；该实验不能直接推定受检者患焦虑症。"),
    )),
    ("immune", "免疫系统", 9, (
        ("rheumatoid_arthritis", "类风湿性关节炎", "类风湿性关节炎患者研究观察到结肠紧密连接蛋白 ZO-1 减少，以及 LBP、sCD14 等标志物与疾病活动度关联。屏障与全身炎症可能相互影响，免疫耐受及关节病变仍需专项临床评价。"),
        ("systemic_lupus", "系统性红斑狼疮", "遗传易感动物中，特定肠道共生菌移位可激活适应性免疫和自身抗体反应；部分自身免疫患者组织亦发现相关细菌线索。这支持“易感性—微生物接触—免疫激活”的研究路径，而非普遍适用的单一病因。"),
    )),
    ("urinary", "泌尿系统", 10, (
        ("chronic_kidney_disease", "慢性肾脏病", "肾功能下降可改变肠道内环境并增加肠源性物质清除负担。尿毒症患者血浆处理肠上皮细胞的实验显示紧密连接蛋白减少与通透性改变，为肠–肾轴的双向作用提供线索。"),
    )),
    ("oncology", "肿瘤", 11, (
        ("colorectal_cancer", "结直肠癌", "在肠腺瘤实验模型中，局部屏障缺陷使微生物产物更易接触免疫细胞，激活 IL-23/IL-17 等促炎通路并支持肿瘤生长。该局部微环境机制不等同于屏障检测具有结直肠癌筛查能力。"),
        ("liver_cancer", "肝癌", "慢性肝损伤、炎症和纤维化共同构成部分肝癌的发生背景。动物研究中，肠道菌群及 TLR4 信号可促进已受损肝脏的肿瘤进展；个体风险还需结合病毒性肝炎、肝硬化等明确危险因素。"),
        ("lung_cancer", "肺癌", "IL-1β 等炎症通路与肿瘤微环境相关。既往心肌梗死且炎症升高人群的探索性试验分析发现抗炎干预与肺癌事件变化有关；这一特定人群结果并未建立肠道屏障损伤导致肺癌的因果关系。"),
    )),
)


# Internal verification ledger. "primary_abstract_or_article" is deliberately
# not called a full-text systematic review or model validation. No source here
# supplies a personal risk coefficient or a risk-score approval.
_MECHANISM_STUDIES = {
    "liver_tlr4": ("https://pubmed.ncbi.nlm.nih.gov/17952090/", "小鼠和细胞实验", "TLR4、肝星状细胞及 TGF-β 促纤维化信号"),
    "ibs_permeability": ("https://pubmed.ncbi.nlm.nih.gov/23667756/", "腹泻型肠易激综合征患者观察研究", "肥大细胞与通透性的关联"),
    "ibd_barrier": ("https://pmc.ncbi.nlm.nih.gov/articles/PMC9869054/", "IL-10 缺失小鼠", "黏液、菌群及屏障变化与肠炎"),
    "metabolic_lps": ("https://pubmed.ncbi.nlm.nih.gov/17456850/", "小鼠实验", "LPS、炎症及代谢表型"),
    "human_adipose": ("https://pubmed.ncbi.nlm.nih.gov/17090751/", "人体脂肪组织、细胞实验和病例比较", "LPS、先天免疫通路与脂肪组织细胞因子"),
    "lipids": ("https://pmc.ncbi.nlm.nih.gov/articles/PMC10123374/", "健康年轻男性内毒素刺激实验", "急性炎症刺激与富甘油三酯脂蛋白变化；不等同慢性血脂病因"),
    "urate": ("https://pubmed.ncbi.nlm.nih.gov/24940678/", "日本男性高尿酸门诊患者", "ABCG2 功能和肾外尿酸排泄；非屏障标志物预测"),
    "atherosclerosis_tlr4": ("https://pmc.ncbi.nlm.nih.gov/articles/PMC489994/", "ApoE 缺失小鼠", "TLR4/MyD88 与斑块及炎症表型"),
    "hypertension_il6": ("https://pubmed.ncbi.nlm.nih.gov/22068875/", "患者组织及血管紧张素Ⅱ小鼠实验", "IL-6、内皮素表达、血压及肾纤维化"),
    "alzheimers": ("https://pubmed.ncbi.nlm.nih.gov/27776263/", "认知受损老年人观察研究", "菌群、外周炎症与脑淀粉样沉积相关；非因果预测"),
    "parkinsons": ("https://pubmed.ncbi.nlm.nih.gov/27912057/", "α-突触核蛋白过表达小鼠及患者菌群移植实验", "菌群、小胶质细胞及运动表型；非人体风险算法"),
    "mood_biomarkers": ("https://pmc.ncbi.nlm.nih.gov/articles/PMC5851874/", "抑郁或焦虑人群观察研究", "LPS、zonulin、FABP2 及菌群关联"),
    "anxiety_mouse": ("https://pubmed.ncbi.nlm.nih.gov/28251905/", "患者菌群移植至无菌小鼠", "屏障、免疫及焦虑样行为；非焦虑症诊断"),
    "rheumatoid_arthritis": ("https://academic.oup.com/rheumatology/article/62/3/1264/6659537", "类风湿关节炎患者活检及纵向标志物研究", "ZO-1、LBP、sCD14 与疾病活动度"),
    "lupus": ("https://pubmed.ncbi.nlm.nih.gov/29590047/", "自身免疫易感小鼠及患者组织/细胞研究", "特定菌移位与自身免疫激活；不是所有人的统一病因"),
    "kidney_barrier": ("https://pubmed.ncbi.nlm.nih.gov/23128155/", "尿毒症患者血浆与肠上皮细胞实验", "尿毒症环境、紧密连接与屏障功能"),
    "colorectal": ("https://pmc.ncbi.nlm.nih.gov/articles/PMC3601659/", "小鼠肿瘤模型及人类肿瘤组织", "腺瘤局部屏障缺陷与 IL-23/IL-17 通路"),
    "liver_cancer": ("https://pubmed.ncbi.nlm.nih.gov/22516259/", "慢性肝损伤小鼠肿瘤模型", "菌群/TLR4 参与肿瘤促进，不能等同起始致癌"),
    "lung_cancer": ("https://pubmed.ncbi.nlm.nih.gov/28855077/", "既往心肌梗死且炎症升高人群随机试验的探索性分析", "IL-1β 抑制与肺癌事件；非肠道因果或风险计分模型"),
}
_MECHANISM_EVIDENCE = {
    "intestinal_barrier_followup": ("ibs_permeability", "ibd_barrier"),
    "nafld": ("liver_tlr4", "metabolic_lps"), "ibs": ("ibs_permeability", "anxiety_mouse"),
    "ibd": ("ibd_barrier",), "cirrhosis": ("liver_tlr4",),
    "type2_diabetes": ("metabolic_lps", "human_adipose"), "obesity": ("metabolic_lps", "human_adipose"),
    "hyperlipidemia": ("lipids",), "hyperuricemia": ("urate",),
    "atherosclerosis": ("atherosclerosis_tlr4",), "coronary_heart_disease": ("atherosclerosis_tlr4",),
    "hypertension": ("hypertension_il6",), "stroke": ("atherosclerosis_tlr4",),
    "alzheimers": ("alzheimers",), "parkinsons": ("parkinsons",),
    "depression": ("mood_biomarkers",), "anxiety": ("anxiety_mouse", "mood_biomarkers"),
    "rheumatoid_arthritis": ("rheumatoid_arthritis",), "systemic_lupus": ("lupus",),
    "chronic_kidney_disease": ("kidney_barrier",), "colorectal_cancer": ("colorectal",),
    "liver_cancer": ("liver_cancer",), "lung_cancer": ("lung_cancer",),
}

# Bibliographic fields confirmed from the linked primary records. Unknown fields
# remain None; neither a journal nor a DOI is reconstructed from a title.
_STUDY_BIBLIOGRAPHY = {
    "liver_tlr4": ("TLR4 enhances TGF-beta signaling and hepatic fibrosis", 2007, "Nature Medicine", "10.1038/nm1663"),
    "ibs_permeability": ("Mucosal mast cell count is associated with intestinal permeability in patients with diarrhea predominant irritable bowel syndrome", 2013, "Journal of Neurogastroenterology and Motility", "10.5056/jnm.2013.19.2.244"),
    "ibd_barrier": ("Akkermansia deficiency and mucin depletion are implicated in intestinal barrier dysfunction as earlier event in the development of inflammation in interleukin-10-deficient mice", 2023, None, None),
    "metabolic_lps": ("Metabolic endotoxemia initiates obesity and insulin resistance", 2007, "Diabetes", "10.2337/db06-1491"),
    "human_adipose": ("Lipopolysaccharide activates an innate immune system response in human adipose tissue in obesity and type 2 diabetes", 2007, "American Journal of Physiology-Endocrinology and Metabolism", "10.1152/ajpendo.00302.2006"),
    "lipids": ("Effect of omega-3 ethyl esters on the triglyceride-rich lipoprotein response to endotoxin challenge in healthy young men", 2023, None, None),
    "urate": ("ABCG2 dysfunction increases the risk of renal overload hyperuricemia", 2014, None, "10.1080/15257770.2013.866679"),
    "atherosclerosis_tlr4": ("Lack of Toll-like receptor 4 or myeloid differentiation factor 88 reduces atherosclerosis and alters plaque phenotype in mice deficient in apolipoprotein E", 2004, None, None),
    "hypertension_il6": ("Interleukin 6 underlies angiotensin II-induced hypertension and chronic renal damage", 2011, "Hypertension", "10.1161/HYPERTENSIONAHA.111.173328"),
    "alzheimers": ("Association of brain amyloidosis with pro-inflammatory gut bacterial taxa and peripheral inflammation markers in cognitively impaired elderly", 2017, "Neurobiology of Aging", "10.1016/j.neurobiolaging.2016.08.019"),
    "parkinsons": ("Gut Microbiota Regulate Motor Deficits and Neuroinflammation in a Model of Parkinson's Disease", 2016, "Cell", "10.1016/j.cell.2016.11.018"),
    "mood_biomarkers": ("Increased human intestinal barrier permeability plasma biomarkers zonulin and FABP2 correlated with plasma LPS and altered gut microbiome in anxiety or depression", 2018, "Gut", "10.1136/gutjnl-2017-314759"),
    "anxiety_mouse": ("Transplantation of fecal microbiota from patients with irritable bowel syndrome alters gut function and behavior in recipient mice", 2017, "Science Translational Medicine", "10.1126/scitranslmed.aaf6397"),
    "rheumatoid_arthritis": ("Rheumatoid arthritis is associated with increased gut permeability and bacterial translocation that are reversed by inflammation control", 2023, "Rheumatology", None),
    "lupus": ("Translocation of a gut pathobiont drives autoimmunity in mice and humans", 2018, "Science", "10.1126/science.aar7201"),
    "kidney_barrier": ("Uremic plasma impairs barrier function and depletes the tight junction protein constituents of intestinal epithelium", 2012, None, None),
    "colorectal": ("Adenoma-linked barrier defects and microbial products drive IL-23/IL-17-mediated tumour growth", 2012, "Nature", "10.1038/nature11465"),
    "liver_cancer": ("Promotion of hepatocellular carcinoma by the intestinal microbiota and TLR4", 2012, "Cancer Cell", None),
    "lung_cancer": ("Effect of interleukin-1β inhibition with canakinumab on incident lung cancer in patients with atherosclerosis: exploratory results from a randomised, double-blind, placebo-controlled trial", 2017, "The Lancet", "10.1016/S0140-6736(17)32247-X"),
}


def mechanism_evidence() -> list[dict]:
    """Internal, per-disease claim/source ledger; not a model-approval record."""
    return [
        {"code": code, "reviewScope": "primary_abstract_or_article", "reviewDate": "2026-09-24",
         "status": "mechanism_only_not_prediction_model", "studies": [
             {"id": key, "url": _MECHANISM_STUDIES[key][0], "population": _MECHANISM_STUDIES[key][1],
              "scope": _MECHANISM_STUDIES[key][2], "title": _STUDY_BIBLIOGRAPHY[key][0],
              "year": _STUDY_BIBLIOGRAPHY[key][1], "publisher": _STUDY_BIBLIOGRAPHY[key][2],
              "doi": _STUDY_BIBLIOGRAPHY[key][3]} for key in study_ids]}
        for code, study_ids in _MECHANISM_EVIDENCE.items()
    ]


def build_disease_risks() -> tuple[list[dict], dict]:
    """Return the fixed catalog with explicitly unevaluated personal indices.

    This deliberately takes no model/approval/score arguments. The separate
    server-registered adapter cannot be selected or approved by extra JSON.
    """
    rows = []
    evidence = {item["code"]: item for item in mechanism_evidence()}
    for system_code, system_name, table, diseases in _SYSTEMS:
        for index, (code, name, mechanism) in enumerate(diseases, 2):
            source = deepcopy(DISEASE_CATALOG_SOURCE)
            source["location"] = f"第三章表{table}第{index}行；机制参考，不采用示例指数"
            if code == "intestinal_barrier_followup":
                source.update(name="肠道屏障评估逻辑_v2.7_完整版.md", sha256=None,
                              location="第四节：固定评分目录；沿用肠道炎症与功能紊乱机制内容")
            # Keep claim provenance with the catalog; full bibliographic fields
            # are obtained separately through mechanism_evidence() so the same
            # journal record is not duplicated throughout the assessment.
            source["mechanismEvidence"] = {
                key: deepcopy(value) for key, value in evidence[code].items() if key != "studies"
            }
            source["mechanismEvidence"]["studies"] = [
                {key: deepcopy(study[key]) for key in ("id", "url", "population", "scope")}
                for study in evidence[code]["studies"]
            ]
            rows.append({
                "code": code, "name": name, "systemCode": system_code, "systemName": system_name,
                "riskIndex": None, "riskLevel": None, "probability": None, "status": "not_evaluated",
                "mechanism": mechanism,
                "mechanismStatus": "provided_reference_not_individual_diagnosis",
                "source": source,
                "reasons": ["disease_risk_model_not_configured", "mechanism_not_individual_risk_estimate"],
            })
    order = {code: position for position, code in enumerate(SCORING_CODES)}
    rows.sort(key=lambda row: order[row["code"]])
    return rows, {
        "status": "not_configured", "catalogVersion": DISEASE_CATALOG_VERSION,
        "modelId": None, "modelVersion": None, "scale": None,
        "reasons": ["no_validated_disease_risk_model", "no_approved_questionnaire_or_coefficients"],
    }


# The v2.7 document's order is also the deterministic tie breaker for grading.
SCORING_CODES = (
    "ibd", "intestinal_barrier_followup", "nafld", "ibs", "cirrhosis",
    "type2_diabetes", "obesity", "hyperlipidemia", "hyperuricemia",
    "atherosclerosis", "coronary_heart_disease", "stroke", "hypertension",
    "alzheimers", "parkinsons", "depression", "anxiety", "rheumatoid_arthritis",
    "systemic_lupus", "chronic_kidney_disease", "liver_cancer", "colorectal_cancer", "lung_cancer",
)
