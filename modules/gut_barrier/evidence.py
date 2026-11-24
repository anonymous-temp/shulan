"""Versioned evidence records used by the gut-barrier intervention builder.

The two supplied Word documents are provider materials rather than independent
clinical guidelines.  Keeping that distinction in the data prevents the report
layer from silently upgrading a local protocol or a preclinical finding into a
patient-specific prescription.
"""

from __future__ import annotations

from copy import deepcopy


EVIDENCE_VERSION = "gut-barrier-intervention-evidence-v2.20260924"


_REFERENCES = {
    "GB-SOURCE-GRADING-2026-V1": {
        "id": "GB-SOURCE-GRADING-2026-V1",
        "title": "肠道屏障功能损伤分级标准及分层调控方案（2026修订版）",
        "type": "provided_protocol",
        "version": "sha256:40db0af47c74e87a3461c91104475009723bccbd570b43d081b49e2a0aaa2682",
        "source": "user_provided_docx",
        "year": 2026,
        "url": None,
        "scope": "五级分层、营养支持范围与复评周期的项目内方案",
        "limitations": [
            "属用户提供的项目方案，不等同于独立临床指南",
            "部分分值边界在原文中存在冲突，边界病例需人工确认",
        ],
    },
    "GB-SOURCE-REPAIR-2026-V1": {
        "id": "GB-SOURCE-REPAIR-2026-V1",
        "title": "肠道屏障功能修复方案",
        "type": "provided_evidence_summary",
        "version": "sha256:dd5273d39fb7d8887ece84f3abfb8f6fc1274c360cb874a1303fb859bcd01be4",
        "source": "user_provided_docx",
        "year": 2026,
        "url": None,
        "scope": "修复机制、营养模块与菌株/后生元研究线索汇总",
        "limitations": [
            "汇总内容混合了临床、动物和细胞证据",
            "研究菌株、制剂形式与目录产品不可直接互换",
        ],
    },
    "GB-PRODUCT-CATALOG-20260919-V1": {
        "id": "GB-PRODUCT-CATALOG-20260919-V1",
        "title": "MDF 与 FMT 产品目录快照",
        "type": "product_catalog",
        "version": "sha256:0e708073edd3b60a21ad53d80dca96f11ff11df2b1472c6ae6ba3d6c7453a7b6",
        "source": "local_catalog_snapshot",
        "year": 2026,
        "url": None,
        "scope": "产品名称、目录菌株、包装与用法元数据",
        "limitations": [
            "产品目录只能证明配方与用法声明，不能证明对某种屏障组合的临床疗效",
            "MDF 包装数与 30 天目录用量需运营方再核对",
            "FMT 目录活菌总数与单粒数乘包装数的结果不一致，本模块不对患者展示该数值",
        ],
    },
    "GB-FK-DIPEPTIVEN-PI-V1": {
        "id": "GB-FK-DIPEPTIVEN-PI-V1",
        "title": "Dipeptiven prescribing information",
        "type": "official_product_information",
        "version": "accessed-2026-09-23",
        "source": "Fresenius Kabi",
        "year": None,
        "url": "https://www.fresenius-kabi.com/content/dam/fresenius-kabi/gb/products/product-documents/parenteral-nutrition/aminoven-5-10-15/Macronutrient%20Prescribing%20Information.pdf.coredownload.inline.pdf",
        "scope": "丙氨酰谷氨酰胺静脉营养标签上限、联合氨基酸需求和最长疗程",
        "limitations": ["处方信息不构成对某一检测分型的自动用药指征"],
    },
    "GB-NIH-ZINC-ODS-V1": {
        "id": "GB-NIH-ZINC-ODS-V1",
        "title": "Zinc: Fact Sheet for Health Professionals",
        "type": "official_fact_sheet",
        "version": "accessed-2026-09-23",
        "source": "NIH Office of Dietary Supplements",
        "year": None,
        "url": "https://ods.od.nih.gov/factsheets/Zinc-HealthProfessional/",
        "scope": "成人推荐摄入量、可耐受最高摄入量、长期高剂量风险和药物相互作用",
        "limitations": ["常规营养信息，不是肠道屏障特异疗效证据"],
    },
    "GB-NIH-VITD-ODS-V1": {
        "id": "GB-NIH-VITD-ODS-V1",
        "title": "Vitamin D: Fact Sheet for Health Professionals",
        "type": "official_fact_sheet",
        "version": "accessed-2026-09-23",
        "source": "NIH Office of Dietary Supplements",
        "year": None,
        "url": "https://ods.od.nih.gov/factsheets/VitaminD-HealthProfessional/",
        "scope": "维生素D营养评估与成人可耐受最高摄入量",
        "limitations": ["不支持仅根据屏障指标盲目补充维生素D"],
    },
    "GB-RCT-GLUTAMINE-30108163-V1": {
        "id": "GB-RCT-GLUTAMINE-30108163-V1",
        "title": "Randomised placebo-controlled trial of dietary glutamine supplements for postinfectious IBS-D",
        "type": "randomized_controlled_trial",
        "version": "PMID:30108163",
        "source": "Gut",
        "year": 2019,
        "url": "https://pubmed.ncbi.nlm.nih.gov/30108163/",
        "scope": "感染后 IBS-D 且乳果糖/甘露醇通透性增高人群中，5 g、每日三次、8周的研究方案",
        "limitations": [
            "适用人群窄，不能仅凭 D-乳酸/DAO/LPS 外推到一般人群",
            "研究剂量不是当前用户的处方",
        ],
    },
    "GB-RCT-NCFM-BI07-21436726-V1": {
        "id": "GB-RCT-NCFM-BI07-21436726-V1",
        "title": "Lactobacillus acidophilus NCFM and Bifidobacterium lactis Bi-07 in non-constipation functional bowel disorders",
        "type": "randomized_controlled_trial",
        "version": "PMID:21436726",
        "source": "Journal of Clinical Gastroenterology",
        "year": 2011,
        "url": "https://pubmed.ncbi.nlm.nih.gov/21436726/",
        "scope": "非便秘型功能性肠病成人中 NCFM+Bi-07 、2×10^11 CFU/日、8周的症状研究",
        "limitations": [
            "全局缓解/满意度主要结局未显示组间优势，腹胀改善属次要结果",
            "研究复方与目录多菌株 MDF 不是同一制剂，研究剂量不得写为产品用量",
        ],
    },
    "GB-AGA-FMT-2024-V1": {
        "id": "GB-AGA-FMT-2024-V1",
        "title": "AGA Clinical Practice Guideline on Fecal Microbiota-Based Therapies",
        "type": "professional_guideline",
        "version": "accessed-2026-09-23",
        "source": "American Gastroenterological Association",
        "year": 2024,
        "url": "https://gastro.org/clinical-guidance/fecal-microbiota-based-therapies-for-select-gastrointestinal-diseases/",
        "scope": "粪菌群疗法用于选择性复发性艰难梭菌感染等情境的临床边界；IBD、储袋炎和IBS限临床试验，重度免疫抑制者不建议常规使用",
        "limitations": ["地区法规、产品定义和患者筛查仍须由当地专科团队确认", "屏障指标或肠型不构成指南适应证"],
    },
    "GB-FDA-FMT-SAFETY-2020-V1": {
        "id": "GB-FDA-FMT-SAFETY-2020-V1",
        "title": "Safety Alert Regarding Use of Fecal Microbiota for Transplantation and Risk of Serious Adverse Events",
        "type": "regulatory_safety_alert",
        "version": "accessed-2026-09-24",
        "source": "US Food and Drug Administration",
        "year": 2020,
        "url": "https://www.fda.gov/vaccines-blood-biologics/safety-availability-biologics/safety-alert-regarding-use-fecal-microbiota-transplantation-and-risk-serious-adverse-events-likely",
        "scope": "FMT供者和病原筛查、知情同意及病原体传播导致严重感染的安全风险",
        "limitations": ["美国监管安全警示不能替代当地法规、机构流程或个体专科评估"],
    },
    "GB-ISAPP-POSTBIOTIC-2021-V1": {
        "id": "GB-ISAPP-POSTBIOTIC-2021-V1",
        "title": "ISAPP consensus statement on the definition and scope of postbiotics",
        "type": "expert_consensus",
        "version": "PMID:33948025",
        "source": "Nature Reviews Gastroenterology & Hepatology",
        "year": 2021,
        "url": "https://pubmed.ncbi.nlm.nih.gov/33948025/",
        "scope": "后生元的定义及制剂特异性",
        "limitations": ["定义性共识不证明任意灭活菌/代谢物都有临床疗效"],
    },
    "GB-AHA-FISH-V1": {
        "id": "GB-AHA-FISH-V1",
        "title": "Fish and Omega-3 Fatty Acids",
        "type": "professional_public_guidance",
        "version": "accessed-2026-09-23",
        "source": "American Heart Association",
        "year": None,
        "url": "https://www.heart.org/en/healthy-living/healthy-eating/eat-smart/fats/fish-and-omega-3-fatty-acids",
        "scope": "普通成人优先通过鱼类摄入 omega-3 的食物建议",
        "limitations": ["属一般心血管营养建议，不能表述为降低本次 LPS 的个体疗效"],
    },
    "GB-WHO-ACTIVITY-2020-V1": {
        "id": "GB-WHO-ACTIVITY-2020-V1",
        "title": "WHO guidelines on physical activity and sedentary behaviour",
        "type": "official_guideline",
        "version": "ISBN:9789240015128",
        "source": "World Health Organization",
        "year": 2020,
        "url": "https://iris.who.int/bitstream/handle/10665/336656/9789240015128-eng.pdf",
        "scope": "成人每周 150–300 分钟中等强度有氧活动与每周至少 2 天肌力活动",
        "limitations": ["需根据年龄、心肺状态、慢病及功能限制调整"],
    },
    "GB-CDC-SLEEP-V1": {
        "id": "GB-CDC-SLEEP-V1",
        "title": "About Sleep",
        "type": "official_public_guidance",
        "version": "accessed-2026-09-23",
        "source": "US Centers for Disease Control and Prevention",
        "year": None,
        "url": "https://www.cdc.gov/sleep/about/",
        "scope": "成人睡眠时长建议和基本睡眠卫生",
        "limitations": ["按年龄分层的一般建议，不是肠道屏障特异治疗"],
    },
}


def reference(reference_id: str) -> dict:
    """Return an isolated reference record and fail loudly on unknown IDs."""
    return deepcopy(_REFERENCES[reference_id])


def references_for(reference_ids) -> list[dict]:
    """Return unique references in caller order."""
    seen: set[str] = set()
    result: list[dict] = []
    for reference_id in reference_ids:
        if reference_id in seen:
            continue
        seen.add(reference_id)
        result.append(reference(reference_id))
    return result
