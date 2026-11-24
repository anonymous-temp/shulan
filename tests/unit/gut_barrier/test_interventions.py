from __future__ import annotations

import unittest
from copy import deepcopy

from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.catalog import normalize_product_catalog


def _assessment(*, pattern="+++", grade="IV", age=42, sex="女", inflammation="high", context=None):
    subject = {"userId": "u1", "sessionId": "s1", "sex": sex, "context": dict(context or {})}
    if age is not None:
        subject["age"] = age
    return {
        "subject": subject,
        "context": dict(context or {}),
        "markers": [],
        "barrier": {
            "score": 7.5 if grade == "IV" else 0,
            "grade": grade,
            "pattern": pattern,
            "patternCode": pattern.replace("+", "1").replace("-", "0") if pattern else None,
            "type": "test",
            "status": "evaluated" if pattern else "incomplete",
            "reasons": [],
        },
        "inflammation": {
            "value": 4.2 if inflammation == "high" else 0.5,
            "unit": "mg/L",
            "band": inflammation,
            "status": "evaluated",
            "sourceAttribution": "undetermined",
            "chronicity": "unconfirmed",
            "reasons": [],
        },
        "risks": [],
        "missingData": [],
        "provenance": {},
        "ruleVersion": "test-v1",
    }


def _catalog():
    return {
        "data": {
            "results": {
                "microecologicalTargetedRegulator": [{
                    "productCode": "F8783247",
                    "productName": "肠道微生态靶向调节剂(MDF)",
                    "productStatus": "启用",
                    "productSpecification": "30 粒/盒",
                    "compositeEfficacyComponents": "后生元\n抗性膳食纤维",
                    "requiredStrains": ["瑞士乳杆菌R0052", "长双歧杆菌婴儿亚种"],
                    "optionalStrains": ["嗜酸乳杆菌NCFM", "动物双歧杆菌乳亚种Bi-07", "副干酪乳杆菌HN001"],
                    "usageInstructions": "1.每日2粒 2.单疗程30天",
                }],
                "fecalMicrobiotaTransplantationCapsule": [
                    {
                        "productCode": "T-ACTIVE", "productName": "女-ET-B-PFMT", "productStatus": "启用",
                        "productSpecification": "30 粒/盒", "usageInstructions": "每日10粒，连续3天",
                        "applicableEnterotype": "ET-B", "applicableEnterotypeName": "拟杆菌型", "applicableGender": "女",
                    },
                    {
                        "productCode": "T-DISABLED", "productName": "女-ET-P-PFMT", "productStatus": "停用",
                        "productSpecification": "30 粒/盒", "usageInstructions": "每日10粒，连续3天",
                        "applicableEnterotype": "ET-P", "applicableEnterotypeName": "普雷沃氏菌型", "applicableGender": "女",
                    },
                ],
            }
        }
    }


def _approved_catalog():
    catalog = _catalog()
    product = catalog["data"]["results"]["microecologicalTargetedRegulator"][0]
    product["requiredStrains"] = ["瑞士乳杆菌R0052", "长双歧杆菌长亚种R0175"]
    product["formulationApproval"] = {
        "status": "approved",
        "version": "FORMULA-111-V1",
        "patterns": ["+++"],
        "preparationForm": "live_and_postbiotic_catalog_formula",
    }
    return catalog


def _values_for_key(value, target):
    values = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == target:
                values.append(item)
            else:
                values.extend(_values_for_key(item, target))
    elif isinstance(value, list):
        for item in value:
            values.extend(_values_for_key(item, target))
    return values


def _named(rows, name):
    return next(item for item in rows if item.get("name") == name)


class GutBarrierInterventionTest(unittest.TestCase):
    def test_contract_has_all_seven_patterns_and_one_active_pattern(self):
        result = build_interventions(_assessment(), product_catalog=_catalog())

        self.assertEqual(
            set(result),
            {"barrierCare", "microbiome", "nutrition", "lifestyle", "followup", "references", "reviewItems"},
        )
        plans = result["microbiome"]["sevenPatternPlans"]
        self.assertEqual({item["pattern"] for item in plans}, {"+--", "-+-", "--+", "++-", "+-+", "-++", "+++"})
        self.assertEqual([item["pattern"] for item in plans if item["active"]], ["+++"])
        self.assertTrue(all(item["patientStrategy"] for item in plans))
        self.assertTrue(all(item["approvedFormulaStatus"] == "not_supplied" for item in plans))
        self.assertEqual(next(item for item in plans if item["pattern"] == "+++")["name"], "混合型受损")

    def test_each_abnormal_pattern_keeps_unapproved_catalog_unmatched(self):
        for pattern in ("+--", "-+-", "--+", "++-", "+-+", "-++", "+++"):
            with self.subTest(pattern=pattern):
                result = build_interventions(_assessment(pattern=pattern), product_catalog=_catalog())
                mdf = result["microbiome"]["mdf"]
                self.assertEqual(mdf["status"], "no_approved_formula_match")
                self.assertIsNone(mdf["product"])
                self.assertEqual(mdf["selectedStrains"], [])

    def test_normal_pattern_does_not_start_iv_mdf_or_pfmt(self):
        result = build_interventions(_assessment(pattern="---", grade="normal", inflammation="low"))

        self.assertEqual(result["barrierCare"]["status"], "not_indicated")
        self.assertEqual(result["microbiome"]["mdf"]["status"], "not_indicated")
        self.assertEqual(result["microbiome"]["pfmt"]["status"], "not_indicated")
        self.assertFalse(result["barrierCare"]["started"])
        self.assertFalse(any(item["active"] for item in result["microbiome"]["sevenPatternPlans"]))

    def test_grade_iv_is_review_only_and_never_started(self):
        result = build_interventions(_assessment(), product_catalog=_catalog())

        care = result["barrierCare"]
        self.assertEqual(care["status"], "conditional_specialist_review")
        self.assertFalse(care["started"])
        self.assertEqual(care["dose"]["officialMaximum"], "0.5 g/kg/day（产品处方信息上限）")
        self.assertIn("不超过3周", care["dose"]["duration"])

    def test_enabled_mdf_without_approved_pattern_formula_stays_unmatched(self):
        result = build_interventions(_assessment(), product_catalog=_catalog())

        mdf = result["microbiome"]["mdf"]
        self.assertEqual(mdf["status"], "no_approved_formula_match")
        self.assertIsNone(mdf["product"])
        self.assertIsNone(mdf["catalogUsage"])
        self.assertEqual(mdf["requiredStrains"], [])
        self.assertEqual(mdf["selectedStrains"], [])
        self.assertEqual(mdf["approvedFormulaMatch"]["status"], "no_exact_approved_match")
        self.assertTrue(any("30粒/盒" in item for item in result["reviewItems"]))

    def test_exact_approved_mdf_match_can_expose_separate_rct_dose_but_not_selection(self):
        result = build_interventions(
            _assessment(context={"symptoms": ["腹胀"]}), product_catalog=_approved_catalog(),
        )

        mdf = result["microbiome"]["mdf"]
        self.assertEqual(mdf["status"], "approved_formula_match_requires_review")
        self.assertEqual(mdf["product"]["code"], "F8783247")
        self.assertEqual(mdf["approvedFormulaMatch"]["approvalVersion"], "FORMULA-111-V1")
        self.assertIsNotNone(mdf["researchDose"])
        self.assertIn("2×10^11", mdf["researchDose"]["amount"])
        self.assertNotIn("2×10^11", mdf["catalogUsage"]["directions"])
        self.assertEqual(mdf["selectedStrains"], [])
        reviewed = {row["strain"] for row in mdf["optionalCandidates"] if row["status"] == "eligible_pair_for_human_review"}
        self.assertEqual(reviewed, {"NCFM", "Bi-07"})

    def test_negated_or_source_only_bloating_does_not_trigger_rct_candidate(self):
        for context in (
            {"symptoms": ["无腹胀"]},
            {"microbiomeSources": [{"summaryText": "腹胀研究与NCFM"}]},
            {"symptoms": {"bloating": False}},
        ):
            with self.subTest(context=context):
                result = build_interventions(_assessment(context=context), product_catalog=_approved_catalog())
                self.assertIsNone(result["microbiome"]["mdf"]["researchDose"])

    def test_pfmt_matches_only_exact_enabled_catalog_metadata(self):
        exact = build_interventions(
            _assessment(context={"enterotype": "ET-B"}), product_catalog=_catalog(),
        )["microbiome"]["pfmt"]
        disabled = build_interventions(
            _assessment(context={"enterotype": "ET-P"}), product_catalog=_catalog(),
        )["microbiome"]["pfmt"]
        inferred = build_interventions(
            _assessment(context={"enterotype": "肠型I"}), product_catalog=_catalog(),
        )["microbiome"]["pfmt"]

        self.assertEqual(exact["status"], "no_guideline_indication_identified")
        self.assertIsNone(exact["product"])
        self.assertEqual(exact["catalogMatch"]["candidate"]["code"], "T-ACTIVE")
        self.assertEqual(disabled["status"], "no_guideline_indication_identified")
        self.assertIsNone(disabled["product"])
        self.assertEqual(disabled["catalogMatch"]["status"], "no_exact_match")
        self.assertEqual(inferred["status"], "no_guideline_indication_identified")
        self.assertEqual(inferred["catalogMatch"]["status"], "no_exact_match")

    def test_source_only_recurrent_cdiff_text_does_not_trigger_condition(self):
        result = build_interventions(
            _assessment(context={"microbiomeSources": [{"summaryText": "recurrent C. difficile"}]}),
            product_catalog=_catalog(),
        )
        self.assertFalse(any("上下文提到复发性" in item for item in result["reviewItems"]))

    def test_recurrent_cdiff_signal_still_requires_specialist_and_never_exposes_catalog_usage(self):
        result = build_interventions(
            _assessment(context={"conditions": ["复发性艰难梭菌感染"], "enterotype": "ET-B", "immuneStatus": "正常"}),
            product_catalog=_catalog(),
        )

        pfmt = result["microbiome"]["pfmt"]
        self.assertEqual(pfmt["status"], "specialist_review_only")
        self.assertEqual(pfmt["clinicalEligibility"]["status"], "possible_guideline_context_needs_confirmation")
        self.assertEqual(pfmt["product"]["code"], "T-ACTIVE")
        self.assertIsNone(pfmt["catalogUsage"])
        self.assertFalse(pfmt["started"])

    def test_severe_immunocompromise_suppresses_mdf_and_pfmt_even_with_catalog_matches(self):
        result = build_interventions(
            _assessment(context={
                "conditions": ["复发性艰难梭菌感染"],
                "enterotype": "ET-B",
                "immuneStatus": "重度免疫抑制",
            }),
            product_catalog=_approved_catalog(),
        )

        mdf = result["microbiome"]["mdf"]
        pfmt = result["microbiome"]["pfmt"]
        self.assertEqual(mdf["status"], "safety_hold_immunocompromised")
        self.assertIsNone(mdf["product"])
        self.assertIsNone(mdf["catalogUsage"])
        self.assertEqual(pfmt["status"], "safety_hold_severe_immunocompromise")
        self.assertEqual(pfmt["clinicalEligibility"]["status"], "guideline_suggests_against")
        self.assertIsNone(pfmt["product"])
        self.assertIsNone(pfmt["catalogUsage"])

    def test_fish_allergy_suppresses_fish_and_fish_oil_targets(self):
        result = build_interventions(
            _assessment(context={"allergies": ["鱼类、海鲜过敏"]}), product_catalog=_catalog(),
        )

        omega = _named(result["nutrition"], "Omega-3 食物来源")
        self.assertEqual(omega["status"], "suppressed_due_to_allergy")
        self.assertIsNone(omega["dose"])
        self.assertIn("documented_fish_or_seafood_allergy", omega["suppressedReasons"])
        self.assertTrue(any("鱼类和鱼油相关方案已被抑制" in item for item in result["reviewItems"]))

    def test_liver_or_kidney_problem_suppresses_glutamine_and_supplement_doses(self):
        for context in (
            {"liverAndKidneyFunction": "肾功能不全", "nutrientStatus": {"zinc": "deficient", "vitaminD": "deficient"}},
            {"liverAndKidneyFunction": "肝功能不全", "nutrientStatus": {"zinc": "deficient", "vitaminD": "deficient"}},
        ):
            with self.subTest(context=context):
                result = build_interventions(_assessment(context=context), product_catalog=_catalog())
                self.assertEqual(result["barrierCare"]["status"], "safety_hold_liver_kidney")
                self.assertIsNone(result["barrierCare"]["dose"])
                for name in ("锌（仅缺乏/摄入不足时）", "维生素D", "口服谷氨酰胺"):
                    item = _named(result["nutrition"], name)
                    self.assertEqual(item["status"], "safety_hold_liver_kidney")
                    self.assertIsNone(item["dose"])

    def test_nutrient_status_controls_supplement_candidate_doses(self):
        unknown = build_interventions(_assessment(), product_catalog=_catalog())
        adequate = build_interventions(
            _assessment(context={"nutrientStatus": {"zinc": "正常", "vitaminD": "充足"}}),
            product_catalog=_catalog(),
        )
        deficient = build_interventions(
            _assessment(context={"nutrientStatus": {"zinc": "缺乏", "vitaminD": "不足"}}),
            product_catalog=_catalog(),
        )

        for name in ("锌（仅缺乏/摄入不足时）", "维生素D"):
            self.assertEqual(_named(unknown["nutrition"], name)["status"], "nutrient_status_required")
            self.assertIsNone(_named(unknown["nutrition"], name)["dose"])
            self.assertEqual(_named(adequate["nutrition"], name)["status"], "not_indicated_by_nutrient_status")
            self.assertIsNone(_named(adequate["nutrition"], name)["dose"])
            self.assertEqual(_named(deficient["nutrition"], name)["status"], "documented_deficiency_needs_clinician_review")
            self.assertIsNotNone(_named(deficient["nutrition"], name)["dose"])

    def test_each_nutrient_is_evaluated_without_cross_contaminating_other_statuses(self):
        for status, zinc_deficient, vitamin_d_deficient in (
            ({"zinc": "deficient", "vitaminD": "normal"}, True, False),
            ({"zinc": "normal", "vitaminD": "deficient"}, False, True),
            ("锌正常；维生素D缺乏", False, True),
            ("锌缺乏；维生素D正常", True, False),
            ([{"name": "zinc", "status": "deficient"}, {"nutrient": "vitaminD", "status": "normal"}], True, False),
        ):
            with self.subTest(status=status):
                result = build_interventions(_assessment(context={"nutrientStatus": status}))
                for name, deficient in (("锌（仅缺乏/摄入不足时）", zinc_deficient), ("维生素D", vitamin_d_deficient)):
                    row = _named(result["nutrition"], name)
                    self.assertEqual(row["status"], "documented_deficiency_needs_clinician_review" if deficient else "not_indicated_by_nutrient_status")
                    self.assertEqual(row["dose"] is not None, deficient)

    def test_ambiguous_or_conflicting_nutrient_records_do_not_generate_doses(self):
        for status in (
            "锌正常且维生素D缺乏",  # The adjective scope is not independently parsed.
            "锌可能缺乏；维生素D状态未知",
            {"zinc": "deficient but normal", "vitaminD": {"status": "normal", "assessment": "deficient"}},
            [{"zinc": "deficient", "vitaminD": "deficient"}, {"zinc": "normal", "vitaminD": "normal"}],
            {"zinc": 8, "vitaminD": 20},  # No method-specific threshold inference.
        ):
            with self.subTest(status=status):
                result = build_interventions(_assessment(context={"nutrientStatus": status}))
                for name in ("锌（仅缺乏/摄入不足时）", "维生素D"):
                    row = _named(result["nutrition"], name)
                    self.assertEqual(row["status"], "nutrient_status_required")
                    self.assertIsNone(row["dose"])

    def test_negated_nutrient_deficiency_is_not_treated_as_deficient(self):
        result = build_interventions(
            _assessment(context={"nutrientStatus": {"zinc": "无缺乏", "vitaminD": "not deficient"}}),
            product_catalog=_catalog(),
        )

        for name in ("锌（仅缺乏/摄入不足时）", "维生素D"):
            item = _named(result["nutrition"], name)
            self.assertEqual(item["status"], "not_indicated_by_nutrient_status")
            self.assertIsNone(item["dose"])

    def test_unconfirmed_or_undetected_zinc_deficiency_does_not_generate_dose(self):
        for status in ("锌缺乏未证实", "未发现锌缺乏", "未发现明显锌缺乏", "锌不缺乏"):
            with self.subTest(status=status):
                result = build_interventions(_assessment(context={"nutrientStatus": status}))
                zinc = _named(result["nutrition"], "锌（仅缺乏/摄入不足时）")
                self.assertNotEqual(zinc["status"], "documented_deficiency_needs_clinician_review")
                self.assertIsNone(zinc["dose"])

    def test_zinc_interacting_medication_withholds_zinc_candidate_dose(self):
        result = build_interventions(
            _assessment(context={"nutrientStatus": {"zinc": "缺乏"}, "medications": ["喹诺酮类抗生素"]}),
            product_catalog=_catalog(),
        )

        zinc = _named(result["nutrition"], "锌（仅缺乏/摄入不足时）")
        self.assertEqual(zinc["status"], "interaction_review_required")
        self.assertIsNone(zinc["dose"])

    def test_bowel_obstruction_context_suppresses_fiber_target(self):
        result = build_interventions(
            _assessment(context={"conditions": ["肠梗阻"]}), product_catalog=_catalog(),
        )

        fiber = _named(result["nutrition"], "总膳食纤维")
        self.assertEqual(fiber["status"], "safety_hold")
        self.assertIsNone(fiber["dose"])

    def test_multiple_exact_mdf_matches_do_not_select_first_product(self):
        catalog = _approved_catalog()
        duplicate = deepcopy(catalog["data"]["results"]["microecologicalTargetedRegulator"][0])
        duplicate["productCode"] = "F-SECOND"
        catalog["data"]["results"]["microecologicalTargetedRegulator"].append(duplicate)

        mdf = build_interventions(_assessment(), product_catalog=catalog)["microbiome"]["mdf"]

        self.assertEqual(mdf["status"], "multiple_approved_matches_need_review")
        self.assertIsNone(mdf["product"])
        self.assertIsNone(mdf["catalogUsage"])
        self.assertEqual(mdf["approvedFormulaMatch"]["matchCount"], 2)

    def test_explicit_structured_strain_ids_can_form_an_exact_match_without_guessing(self):
        catalog = _approved_catalog()
        product = catalog["data"]["results"]["microecologicalTargetedRegulator"][0]
        product["requiredStrains"] = [
            {"name": "示例乳杆菌", "strain": "EXACT-001", "form": "live"},
            {"name": "示例双歧杆菌", "strainId": "EXACT-002", "form": "heat_inactivated"},
        ]

        mdf = build_interventions(_assessment(), product_catalog=catalog)["microbiome"]["mdf"]

        self.assertEqual(mdf["status"], "approved_formula_match_requires_review")
        self.assertEqual([item["strain"] for item in mdf["requiredStrains"]], ["EXACT-001", "EXACT-002"])

    def test_minor_has_no_dose_catalog_usage_or_research_dose_anywhere(self):
        result = build_interventions(
            _assessment(age=17, context={"symptoms": ["腹胀"], "enterotype": "ET-B"}),
            product_catalog=_catalog(),
        )

        for key in ("dose", "catalogUsage", "researchDose"):
            self.assertTrue(_values_for_key(result, key))
            self.assertTrue(all(value is None for value in _values_for_key(result, key)))
        self.assertTrue(any("未满18岁" in item for item in result["reviewItems"]))

    def test_unknown_age_has_no_dose_catalog_usage_or_research_dose_anywhere(self):
        result = build_interventions(
            _assessment(age=None, context={"symptoms": ["腹胀"], "enterotype": "ET-B"}),
            product_catalog=_catalog(),
        )

        for key in ("dose", "catalogUsage", "researchDose"):
            self.assertTrue(all(value is None for value in _values_for_key(result, key)))
        self.assertTrue(any("年龄未确认" in item for item in result["reviewItems"]))

    def test_input_objects_are_not_mutated_and_reference_ids_are_unique(self):
        assessment, catalog = _assessment(), _catalog()
        assessment_before, catalog_before = deepcopy(assessment), deepcopy(catalog)

        result = build_interventions(assessment, product_catalog=catalog)

        self.assertEqual(assessment, assessment_before)
        self.assertEqual(catalog, catalog_before)
        ids = [item["id"] for item in result["references"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("GB-PRODUCT-CATALOG-20260919-V1", ids)
        self.assertNotIn("GB-ACC-INFLAMMATION-2025-V1", ids)

    def test_malformed_strain_fields_are_ignored_not_split_into_characters(self):
        catalog = _catalog()
        product = catalog["data"]["results"]["microecologicalTargetedRegulator"][0]
        product["requiredStrains"] = None
        product["optionalStrains"] = "嗜酸乳杆菌NCFM"

        normalized = normalize_product_catalog(catalog)
        result = build_interventions(_assessment(), product_catalog=catalog)

        self.assertEqual(normalized["mdfProducts"][0]["requiredStrains"], [])
        self.assertEqual(normalized["mdfProducts"][0]["optionalStrains"], [])
        self.assertIn("mdf_required_strains_invalid", normalized["issues"])
        self.assertIn("mdf_optional_strains_invalid", normalized["issues"])
        self.assertEqual(result["microbiome"]["mdf"]["requiredStrains"], [])
        self.assertEqual(result["microbiome"]["mdf"]["optionalCandidates"], [])

    def test_malformed_product_collections_are_ignored_without_report_failure(self):
        catalog = {
            "data": {
                "microecologicalTargetedRegulator": {},
                "fecalMicrobiotaTransplantationCapsule": "not-a-list",
            }
        }

        normalized = normalize_product_catalog(catalog)
        result = build_interventions(_assessment(), product_catalog=catalog)

        self.assertEqual(normalized["mdfProducts"], [])
        self.assertEqual(normalized["pfmtProducts"], [])
        self.assertIn("mdf_catalog_rows_invalid", normalized["issues"])
        self.assertIn("pfmt_catalog_rows_invalid", normalized["issues"])
        self.assertEqual(result["microbiome"]["mdf"]["status"], "catalog_unavailable")
        self.assertIsNone(result["microbiome"]["pfmt"]["product"])


if __name__ == "__main__":
    unittest.main()


def test_retired_inflammation_values_do_not_trigger_nutrition_followup_or_review():
    import json
    baseline = _assessment(pattern="---", grade="normal", inflammation="low")
    baseline.pop("inflammation")
    expected = build_interventions(baseline)
    for band in ("low", "intermediate", "high", "requires_clinical_review"):
        candidate = deepcopy(baseline)
        candidate["inflammation"] = {"value": 50, "band": band, "status": "needs_review"}
        assert build_interventions(candidate) == expected
    assert "crp" not in json.dumps(expected, ensure_ascii=False).lower()
    assert not any(row["name"] == "Omega-3 食物来源" for row in expected["nutrition"])


def test_documented_acute_context_keeps_clinical_followup_without_retired_biomarker():
    import json
    baseline = _assessment(pattern="---", grade="normal", inflammation="low",
                           context={"acuteInfection": True})
    result = build_interventions(baseline)
    review = _named(result["followup"], "炎症状态优先复核")
    assert review["status"] == "priority_review"
    assert "急性炎症背景" in review["reason"]
    assert "crp" not in json.dumps(result, ensure_ascii=False).lower()
