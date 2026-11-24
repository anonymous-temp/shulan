"""Safe, deterministic catalog parsing and seven-pattern strategy metadata."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


CATALOG_SCHEMA_VERSION = "gut-barrier-product-catalog-v2"


_ABNORMAL_PATTERNS = {"+--", "-+-", "--+", "++-", "+-+", "-++", "+++"}
_APPROVAL_STATUSES = {"approved", "核准", "已核准", "批准", "已批准"}


_KNOWN_STRAINS = {
    "瑞士乳杆菌R0052": ("瑞士乳杆菌", "R0052"),
    "长双歧杆菌长亚种R0175": ("长双歧杆菌长亚种", "R0175"),
    "嗜酸联合乳杆菌L101": ("嗜酸联合乳杆菌", "L101"),
    "副干酪乳杆菌L26": ("副干酪乳杆菌", "L26"),
    "乳酸乳球菌乳亚种R1058": ("乳酸乳球菌乳亚种", "R1058"),
    "短双歧杆菌HA129": ("短双歧杆菌", "HA129"),
    "长双歧杆菌婴儿亚种": ("长双歧杆菌婴儿亚种", None),
    "发酵粘液乳杆菌HA179": ("发酵粘液乳杆菌", "HA179"),
    "植物乳植杆菌R1012": ("植物乳植杆菌", "R1012"),
    "嗜酸乳杆菌NCFM": ("嗜酸乳杆菌", "NCFM"),
    "动物双歧杆菌乳亚种B1-04": ("动物双歧杆菌乳亚种", "B1-04"),
    "动物双歧杆菌乳亚种Bi-07": ("动物双歧杆菌乳亚种", "Bi-07"),
    "副干酪乳杆菌HN001": ("副干酪乳杆菌", "HN001"),
    "短双歧杆菌M-16V": ("短双歧杆菌", "M-16V"),
    "两歧双歧杆菌R0071": ("两歧双歧杆菌", "R0071"),
}


RESEARCH_CANDIDATES = {
    "DS0384_CFS": {
        "id": "DS0384_CFS",
        "name": "Limosilactobacillus reuteri DS0384 无细胞上清",
        "strain": "DS0384",
        "form": "cell_free_supernatant",
        "evidenceLevel": "preclinical_in_provided_summary",
        "status": "research_only_not_product_matched",
        "reason": "项目资料将其与上皮修复机制联系，但该形式不在当前 MDF 目录中",
        "targetTags": ["epithelial_injury"],
        "references": ["GB-SOURCE-REPAIR-2026-V1"],
    },
    "L21_POSTBIOTIC": {
        "id": "L21_POSTBIOTIC",
        "name": "Lacticaseibacillus paracasei L21 后生元",
        "strain": "L21",
        "form": "postbiotic",
        "evidenceLevel": "preclinical_in_provided_summary",
        "status": "research_only_not_product_matched",
        "reason": "项目资料将其与紧密连接/炎症通路联系；目录 L26 不是 L21，不可替代",
        "targetTags": ["tight_junction", "inflammation"],
        "references": ["GB-SOURCE-REPAIR-2026-V1", "GB-ISAPP-POSTBIOTIC-2021-V1"],
    },
    "LGG_HEAT": {
        "id": "LGG_HEAT",
        "name": "热灭活 Lacticaseibacillus rhamnosus GG",
        "strain": "GG",
        "form": "heat_inactivated_postbiotic",
        "evidenceLevel": "preclinical_in_provided_summary",
        "status": "research_only_not_product_matched",
        "reason": "灭活形式的证据不能转用到任意活菌产品",
        "targetTags": ["tight_junction"],
        "references": ["GB-SOURCE-REPAIR-2026-V1", "GB-ISAPP-POSTBIOTIC-2021-V1"],
    },
    "CCFM1359": {
        "id": "CCFM1359",
        "name": "Bifidobacterium bifidum CCFM1359",
        "strain": "CCFM1359",
        "form": "research_strain",
        "evidenceLevel": "preclinical_in_provided_summary",
        "status": "research_only_not_product_matched",
        "reason": "项目资料仅提供氧化应激相关研究线索；目录 R0071 不是 CCFM1359",
        "targetTags": ["multi_link", "inflammation"],
        "references": ["GB-SOURCE-REPAIR-2026-V1"],
    },
}


# Marker order is D-lactate, DAO, LPS.  These are functional strategies, not
# seven independently validated diagnoses or seven clinically proven formulas.
PATTERN_PLANS = (
    {
        "pattern": "+--", "patternCode": "100", "name": "D-乳酸单项异常组合",
        "markerFocus": ["D_LACTATE"],
        "functionalTargets": ["核对通透性相关原因", "优先通过食物基质支持短链脂肪酸代谢", "观察腹胀和大便耐受"],
        "nutritionModules": ["fiber_total", "zinc_if_needed", "vitamin_d_if_needed", "glutamine_specialist_review"],
        "microbiomeStrategy": "若考虑菌群干预，先区分活菌与后生元；仅展示精确菌株/制剂形式对得上的证据",
        "researchCandidateIds": ["L21_POSTBIOTIC", "LGG_HEAT"],
        "limitations": ["单一 D-乳酸异常不能确诊某一疾病"],
    },
    {
        "pattern": "-+-", "patternCode": "010", "name": "DAO单项异常组合",
        "markerFocus": ["DAO"],
        "functionalTargets": ["评估上皮损伤可能原因", "保证能量和蛋白质摄入", "避免未核对的高剂量补充"],
        "nutritionModules": ["protein_adequacy", "zinc_if_needed", "vitamin_d_if_needed", "glutamine_specialist_review"],
        "microbiomeStrategy": "将上皮修复机制作为研究线索，不将上清液研究等同于现售活菌制剂",
        "researchCandidateIds": ["DS0384_CFS"],
        "limitations": ["DAO 受检测方法和临床情境影响，需与症状/病史并读"],
    },
    {
        "pattern": "--+", "patternCode": "001", "name": "LPS单项异常组合",
        "markerFocus": ["LPS"],
        "functionalTargets": ["复核标本、方法与非肠道来源", "评估饮食结构和炎症背景", "不将单项 LPS 写成已确证菌群移位"],
        "nutritionModules": ["fiber_total", "fish_food", "zinc_if_needed", "vitamin_d_if_needed"],
        "microbiomeStrategy": "当前产品目录没有已核对的“LPS 单项型”精确菌株复方，优先复核来源",
        "researchCandidateIds": [],
        "limitations": ["相关指标异常不能单独确定肠源性"],
    },
    {
        "pattern": "++-", "patternCode": "110", "name": "D-乳酸＋DAO异常组合",
        "markerFocus": ["D_LACTATE", "DAO"],
        "functionalTargets": ["同时关注通透性与上皮损伤", "先做营养与症状耐受管理", "在复评后再判断是否需菌群产品"],
        "nutritionModules": ["fiber_total", "protein_adequacy", "zinc_if_needed", "vitamin_d_if_needed", "glutamine_specialist_review"],
        "microbiomeStrategy": "可审查紧密连接与上皮修复的两类研究线索，但当前没有经验证的复方一一映射",
        "researchCandidateIds": ["L21_POSTBIOTIC", "LGG_HEAT", "DS0384_CFS"],
        "limitations": ["联合型仅描述三项指标组合，不是病因诊断"],
    },
    {
        "pattern": "+-+", "patternCode": "101", "name": "D-乳酸＋LPS异常组合",
        "markerFocus": ["D_LACTATE", "LPS"],
        "functionalTargets": ["关注通透性与系统暴露信号", "核对急性感染/检测干扰", "以饮食可耐受性为前提增加纤维"],
        "nutritionModules": ["fiber_total", "fish_food", "zinc_if_needed", "vitamin_d_if_needed", "glutamine_specialist_review"],
        "microbiomeStrategy": "先完成 LPS 来源审查；后生元紧密连接线索仅供研究审查",
        "researchCandidateIds": ["L21_POSTBIOTIC", "LGG_HEAT"],
        "limitations": ["无法由该组合证明 LPS 一定来自肠道"],
    },
    {
        "pattern": "-++", "patternCode": "011", "name": "DAO＋LPS异常组合",
        "markerFocus": ["DAO", "LPS"],
        "functionalTargets": ["同时复核上皮损伤与 LPS 来源", "维持能量和蛋白质供给", "监测消化道症状与炎症变化"],
        "nutritionModules": ["fiber_total", "protein_adequacy", "fish_food", "zinc_if_needed", "vitamin_d_if_needed", "glutamine_specialist_review"],
        "microbiomeStrategy": "上皮修复研究线索与 LPS 来源评估分开处理，不根据组合自动选择活菌",
        "researchCandidateIds": ["DS0384_CFS"],
        "limitations": ["缺少与当前目录精确菌株一致的分型临床证据"],
    },
    {
        "pattern": "+++", "patternCode": "111", "name": "混合型受损",
        "markerFocus": ["D_LACTATE", "DAO", "LPS"],
        "functionalTargets": ["同时复核紧密连接、上皮损伤和 LPS 来源", "按严重程度决定是否需医疗营养支持", "将产品匹配与药物/营养安全审查分开"],
        "nutritionModules": ["fiber_total", "protein_adequacy", "fish_food", "zinc_if_needed", "vitamin_d_if_needed", "glutamine_specialist_review", "enteral_nutrition_review"],
        "microbiomeStrategy": "多环节异常不等于“菌越多越好”；当前仅能列出分机制研究候选并等待审核",
        "researchCandidateIds": ["L21_POSTBIOTIC", "LGG_HEAT", "DS0384_CFS", "CCFM1359"],
        "limitations": ["没有获得与七型一一对应的已验证商品复方"],
    },
)


_PATIENT_STRATEGIES = {
    "+--": [
        "先核对本次 D-乳酸结果、样本与检测方法，并结合腹泻、腹胀和近期饮食解释。",
        "饮食支持从可耐受的食物多样性和纤维来源开始；补充剂或菌株需要另有适用依据。",
    ],
    "-+-": [
        "先排查近期胃肠炎症、黏膜损伤、进食不足及影响结果的临床背景。",
        "优先评估能量、蛋白质和总体营养状态，不根据 DAO 单项结果直接使用谷氨酰胺或菌群产品。",
    ],
    "--+": [
        "先复核 LPS 的样本、方法、急性感染背景及肝脏清除等非肠道影响因素。",
        "未明确来源前，以一般饮食和心代谢危险因素管理为主，不把结果表述成已发生菌群移位。",
    ],
    "++-": [
        "同步评估通透性相关信号、黏膜损伤背景、饮食摄入和胃肠症状。",
        "先完成食物耐受与营养评估，再决定是否需要任何特定补充剂或菌群制剂。",
    ],
    "+-+": [
        "分别核对通透性相关信号和 LPS 来源，避免用一个指标解释另一个指标。",
        "在排除急性因素后，以可耐受饮食和随访趋势决定下一步，不自动匹配菌株。",
    ],
    "-++": [
        "分别评估黏膜损伤背景、营养摄入和 LPS 的肠内外来源。",
        "先处理明确的临床问题和营养不足；研究菌株不等同于本次已选产品。",
    ],
    "+++": [
        "三项均异常时按紧密连接相关信号、上皮损伤背景及 LPS 来源三条线并行复核。",
        "严重度较高仍优先评估经口/肠内营养；静脉营养、MDF 与 FMT 均需各自适应证和安全审核。",
    ],
}


def _strain(label: Any, *, role: str) -> dict:
    if isinstance(label, dict):
        name = str(label.get("name") or label.get("species") or "").strip()
        strain_value = label.get("strain") or label.get("strainId")
        strain = str(strain_value).strip() if strain_value is not None else None
        strain = strain or None
        text = str(label.get("label") or " ".join(filter(None, (name, strain)))).strip()
        return {
            "label": text,
            "name": name or text,
            "strain": strain,
            "form": str(label.get("form") or "catalog_unspecified").strip(),
            "source": "product_catalog",
            "role": role,
            "status": "catalog_fixed_component" if role == "required" else "catalog_option_unreviewed",
        }
    text = str(label).strip()
    name, strain = _KNOWN_STRAINS.get(text, (text, None))
    return {
        "label": text,
        "name": name,
        "strain": strain,
        "form": "live_or_catalog_unspecified",
        "source": "product_catalog",
        "role": role,
        "status": "catalog_fixed_component" if role == "required" else "catalog_option_unreviewed",
    }


def _text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and item.strip()]


def _valid_strain_item(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        name = value.get("name") or value.get("species") or value.get("label")
        strain = value.get("strain") or value.get("strainId")
        return isinstance(name, str) and bool(name.strip()) and isinstance(strain, str) and bool(strain.strip())
    return False


def _product(row: Any, *, kind: str) -> dict | None:
    if not isinstance(row, dict) or not row.get("productCode"):
        return None
    product = {
        "kind": kind,
        "code": str(row["productCode"]),
        "name": str(row.get("productName") or row["productCode"]),
        "status": row.get("productStatus"),
        "specification": row.get("productSpecification"),
        "usageInstructions": row.get("usageInstructions"),
        "requiredStrains": [],
        "optionalStrains": [],
        "components": row.get("compositeEfficacyComponents"),
    }
    if kind == "mdf":
        product["requiredStrains"] = [_strain(item, role="required") for item in row.get("requiredStrains", [])]
        product["optionalStrains"] = [_strain(item, role="optional") for item in row.get("optionalStrains", [])]
        approval = row.get("formulationApproval") if isinstance(row.get("formulationApproval"), dict) else {}
        raw_patterns = (
            approval.get("patterns")
            if approval.get("patterns") is not None
            else row.get("approvedPatterns", row.get("applicablePatterns"))
        )
        product.update({
            "approvedPatterns": _text_list(raw_patterns),
            "formulationApprovalStatus": approval.get("status", row.get("formulationApprovalStatus")),
            "formulationApprovalVersion": approval.get("version", row.get("formulationApprovalVersion")),
            "preparationForm": approval.get("preparationForm", row.get("preparationForm")),
        })
    else:
        product.update({
            "applicableEnterotype": row.get("applicableEnterotype"),
            "applicableEnterotypeName": row.get("applicableEnterotypeName"),
            "applicableGender": row.get("applicableGender"),
            "strainSource": row.get("strainSource"),
        })
    return product


def normalize_product_catalog(product_catalog: Any) -> dict:
    """Normalize only the fields needed by the report; never expose price/CFU."""
    result = {"schemaVersion": CATALOG_SCHEMA_VERSION, "mdfProducts": [], "pfmtProducts": [], "issues": []}
    if not isinstance(product_catalog, dict):
        result["issues"].append("product_catalog_missing_or_invalid")
        return result
    payload = product_catalog
    # The catalog adapter has existed in both ``results`` and
    # ``data.results``/``data`` response envelopes.  Unwrap only dictionaries
    # and keep the product collections themselves untouched.
    for _ in range(3):
        if isinstance(payload.get("data"), dict):
            payload = payload["data"]
            continue
        if isinstance(payload.get("results"), dict):
            payload = payload["results"]
            continue
        break
    if not isinstance(payload, dict):
        result["issues"].append("product_catalog_results_invalid")
        return result
    mdf_rows = payload.get("microecologicalTargetedRegulator", [])
    if not isinstance(mdf_rows, list):
        result["issues"].append("mdf_catalog_rows_invalid")
        mdf_rows = []
    pfmt_rows = payload.get("fecalMicrobiotaTransplantationCapsule", [])
    if not isinstance(pfmt_rows, list):
        result["issues"].append("pfmt_catalog_rows_invalid")
        pfmt_rows = []
    for row in mdf_rows:
        if not isinstance(row, dict):
            result["issues"].append("mdf_catalog_record_invalid")
            continue
        row = dict(row)
        for field, issue in (("requiredStrains", "mdf_required_strains_invalid"),
                             ("optionalStrains", "mdf_optional_strains_invalid")):
            strains = row.get(field)
            if not isinstance(strains, list):
                result["issues"].append(issue)
                row[field] = []
                continue
            valid = [item for item in strains if _valid_strain_item(item)]
            if len(valid) != len(strains):
                result["issues"].append(issue)
            row[field] = valid
        approval = row.get("formulationApproval") if isinstance(row.get("formulationApproval"), dict) else {}
        raw_patterns = (
            approval.get("patterns")
            if approval.get("patterns") is not None
            else row.get("approvedPatterns", row.get("applicablePatterns"))
        )
        if raw_patterns is not None and not isinstance(raw_patterns, list):
            result["issues"].append("mdf_approved_patterns_invalid")
        elif isinstance(raw_patterns, list) and any(
            not isinstance(item, str) or item.strip() not in _ABNORMAL_PATTERNS for item in raw_patterns
        ):
            result["issues"].append("mdf_approved_patterns_invalid")
        product = _product(row, kind="mdf")
        if product:
            result["mdfProducts"].append(product)
    for row in pfmt_rows:
        product = _product(row, kind="pfmt")
        if product:
            result["pfmtProducts"].append(product)
    if not result["mdfProducts"]:
        result["issues"].append("mdf_catalog_empty")
    if not result["pfmtProducts"]:
        result["issues"].append("pfmt_catalog_empty")
    for product in result["mdfProducts"]:
        if product.get("specification") == "30 粒/盒" and "2粒" in (product.get("usageInstructions") or "") and "30天" in (product.get("usageInstructions") or ""):
            result["issues"].append("mdf_package_course_quantity_requires_reconciliation")
        if any(item["strain"] is None for item in product["requiredStrains"]):
            result["issues"].append("mdf_required_component_missing_strain_identifier")
    return result


def match_mdf_products(catalog: dict, *, pattern: Any) -> list[dict]:
    """Return only explicitly approved, exact-pattern MDF formulations.

    An enabled catalog row is not enough.  Precision matching requires an
    explicit pattern mapping, approval status and version, a stated
    preparation form, and identifiers for every fixed strain.  Missing any of
    these fields yields no match rather than a best-effort product choice.
    """
    if not isinstance(pattern, str) or pattern not in _ABNORMAL_PATTERNS:
        return []
    matches: list[dict] = []
    for product in catalog.get("mdfProducts", []):
        approval_status = str(product.get("formulationApprovalStatus") or "").strip().casefold()
        version = product.get("formulationApprovalVersion")
        form = product.get("preparationForm")
        fixed = product.get("requiredStrains")
        exact_strains = isinstance(fixed, list) and bool(fixed) and all(
            isinstance(item, dict) and isinstance(item.get("strain"), str) and item["strain"].strip()
            for item in fixed
        )
        if (
            product.get("status") == "启用"
            and pattern in product.get("approvedPatterns", [])
            and approval_status in {item.casefold() for item in _APPROVAL_STATUSES}
            and isinstance(version, str) and version.strip()
            and isinstance(form, str) and form.strip()
            and exact_strains
        ):
            matches.append(deepcopy(product))
    return matches


def match_pfmt_products(catalog: dict, *, sex: Any, enterotype: Any) -> list[dict]:
    """Match only exact catalog labels/codes; never infer I/II/III mappings."""
    if not isinstance(sex, str) or not isinstance(enterotype, str):
        return []
    sex_text = sex.strip().casefold()
    enterotype_text = enterotype.strip().casefold()
    sex_aliases = {"男": {"男", "male", "m"}, "女": {"女", "female", "f"}}
    normalized_sex = next((label for label, aliases in sex_aliases.items() if sex_text in aliases), None)
    if normalized_sex is None:
        return []
    matches = []
    for product in catalog.get("pfmtProducts", []):
        accepted = {
            str(product.get("applicableEnterotype") or "").strip().casefold(),
            str(product.get("applicableEnterotypeName") or "").strip().casefold(),
        }
        if (product.get("status") == "启用" and
                product.get("applicableGender") == normalized_sex and enterotype_text in accepted):
            matches.append(deepcopy(product))
    return matches


def pattern_plans(current_pattern: str | None) -> list[dict]:
    plans = []
    for source in PATTERN_PLANS:
        plan = deepcopy(source)
        plan["active"] = source["pattern"] == current_pattern
        plan["nameStatus"] = (
            "source_confirmed" if source["pattern"] == "+++" else "neutral_descriptor_pending_provider_confirmation"
        )
        plan["approvedFormulaStatus"] = "not_supplied"
        plan["patientStrategy"] = deepcopy(_PATIENT_STRATEGIES[source["pattern"]])
        plan["researchCandidates"] = [deepcopy(RESEARCH_CANDIDATES[item]) for item in source["researchCandidateIds"]]
        del plan["researchCandidateIds"]
        plan["references"] = ["GB-SOURCE-GRADING-2026-V1", "GB-SOURCE-REPAIR-2026-V1"]
        plans.append(plan)
    return plans
