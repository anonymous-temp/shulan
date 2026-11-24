"""Renderer-independent synthetic report fixtures; no clinical calibration."""
from modules.gut_barrier.presentation import build_report

CHAPTER_IDS = ("overview", "disease_risks", "repair_plan", "education", "appendix")
LEGACY_CHAPTER_IDS = ("overview", "education", "disease_risks", "repair_plan", "appendix")
MARKER_COLUMNS = ("检测指标", "检测结果", "参考范围", "结果评价", "单项评分")
RISK_COLUMNS = ("疾病", "风险指数", "风险等级", "机制关联")

def assessment(*, pattern="+++", band="high", hs_status="evaluated"):
    values = [19, 13, 24] if pattern != "---" else [9, 5, 10]
    scores = [2, 2.5, 3] if pattern != "---" else [0, 0, 0]
    markers = [
        {"code": code, "name": name, "value": value, "unit": unit, "status": "evaluated",
         "score": score, "level": "轻度" if sign == "+" else "正常", "abnormal": sign == "+", "source": []}
        for code, name, value, unit, score, sign in zip(
            ["D_LACTATE", "DAO", "LPS"], ["D-乳酸", "二胺氧化酶", "细菌脂多糖"],
            values, ["mg/L", "U/L", "U/L"], scores, pattern,
        )
    ]
    crp = {"low": 0.4, "intermediate": 2.1, "high": 4.2, "requires_clinical_review": 12}.get(band)
    markers.append({"code": "HS_CRP", "name": "超敏C反应蛋白", "value": crp, "unit": "mg/L", "status": hs_status, "score": None, "level": None, "abnormal": None})
    return {
        "subject": {"name": "真实输入姓名", "sex": "female", "age": 46, "context": {}},
        "markers": markers,
        "barrier": {"score": sum(scores), "grade": "IV" if pattern != "---" else "normal", "pattern": pattern, "type": "混合型" if pattern != "---" else "三项未见异常", "status": "evaluated"},
        "inflammation": {"value": crp, "unit": "mg/L", "band": band, "status": hs_status, "sourceAttribution": "undetermined", "chronicity": "unconfirmed",
                         "project": {"band": {"low": "ideal", "intermediate": "moderate", "high": "marked"}.get(band), "status": hs_status}},
        "risks": [], "missingData": [], "provenance": {"observations": []}, "ruleVersion": "test-five-grade-v1",
    }

def interventions():
    return {
        "barrierCare": {"name": "营养支持", "status": "conditional", "started": False, "condition": ["专科确认适应条件"], "actions": ["先评估实际摄入"], "dose": {"sourceProgramRange": "来源方案范围", "officialMaximum": "需按标签核对", "route": "静脉", "setting": "医疗机构"}},
        "microbiome": {
            "mdf": {"name": "微生态候选", "status": "pending_review", "product": {"code": "TEST-01", "name": "测试目录制剂"}, "catalogUsage": {"directions": "目录用法A", "course": "目录周期A"}, "requiredStrains": [{"name": "研究菌株", "strain": "NCFM", "form": "活菌"}], "selectedStrains": [], "condition": ["药师核对标签"], "monitoring": ["症状及耐受"]},
            "pfmt": {"name": "移植备选", "status": "not_started", "started": False, "condition": ["尚未确认适应证"]},
            "sevenPatternPlans": [
                {"pattern": pattern, "patternCode": pattern.replace("+", "1").replace("-", "0"), "name": f"组合{index}", "functionalTargets": ["对应功能目标"], "nutritionModules": ["饮食支持"], "microbiomeStrategy": "按症状评估", "researchCandidates": [{"name": "候选株", "strain": f"STRAIN-{index}", "status": "candidate"}], "limitations": "研究候选，需审核"}
                for index, pattern in enumerate(["+--", "-+-", "--+", "++-", "+-+", "-++", "+++"], 1)
            ],
        },
        "nutrition": [{"name": "营养项目A", "status": "conditional", "reason": "实际摄入评估", "condition": ["确认需要"], "actions": ["食物优先"], "dose": {"amount": "核对后确定", "unit": "mg"}, "duration": "复评后决定", "monitoring": ["耐受"]}],
        "lifestyle": [{"name": "规律作息", "status": "routine", "actions": ["按个人生活安排固定作息"]}],
        "followup": [{"timepoint": "临床状态稳定后", "actions": ["核对复查结果"], "monitoring": ["症状趋势"]}],
        "references": [{"id": "source-1", "title": "测试参考资料", "url": "https://example.org/evidence"}], "reviewItems": ["internal_rule_check"],
    }

def document(**kwargs):
    return build_report(assessment(**kwargs), interventions(), report_id="ACTUAL-REPORT-01")

def five_chapter_fixture(*, score=7.5, crp=">3.0 mg/L", risk_index=None):
    """Public contract example, not a patient or a medical scoring fixture."""
    return {"schemaVersion": "gut-barrier-public.v1", "meta": {"title": "五章格式测试", "reportId": "TEMPLATE-ONLY"},
            "sections": [
                {"id": "overview", "title": "第一章 检测综述", "blocks": [
                    {"kind": "table", "title": "四项检测指标", "columns": list(MARKER_COLUMNS), "rows": [
                        ["D-乳酸", "19 mg/L", "＜14 mg/L", "原有评价A", 2],
                        ["DAO", "13 U/L", "＜8.5 U/L", "原有评价B", 2.5],
                        ["LPS", "24 U/L", "＜18 U/L", "原有评价C", 3],
                        ["hs-CRP", crp, "≤0.5 mg/L", "范围待核实", None]]},
                    {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
                        {"label": "IBFC评分", "value": score, "detail": "满分15分"},
                        {"label": "损伤等级", "value": "原评估等级，未重新计算", "detail": None}]}]},
                {"id": "education", "title": "第二章 科普", "blocks": [
                    {"kind": "paragraph", "title": f"2.{i} 固定科普小节", "text": f"经审核正文{i}"} for i in range(1, 7)]},
                {"id": "disease_risks", "title": "第三章 疾病风险", "blocks": [
                    {"kind": "table", "title": f"3.{i} 系统{i}疾病风险评估", "columns": list(RISK_COLUMNS),
                     "rows": [[f"疾病{i}-{j}", risk_index, "未评估" if risk_index is None else "原评估等级", "既有机制"]
                              for j in range(1, 4 if i <= 6 else 3)]} for i in range(1, 9)]},
                {"id": "repair_plan", "title": "第四章 修复方案", "blocks": [
                    {"kind": "list", "title": "4.1 修复总原则：五原则框架", "items": ["去除", "改善", "重建", "修复", "平衡"]}]},
                {"id": "appendix", "title": "第五章 附录", "blocks": [
                    {"kind": "references", "items": [{"citation": "教学用文献格式", "url": None}]}]},
            ]}

def reordered_chapter_fixture():
    report = five_chapter_fixture()
    by_id = {section["id"]: section for section in report["sections"]}
    overview = by_id["overview"]["blocks"]
    overview[0]["title"] = "1.2 检测指标分项阐述"
    overview.insert(0, {"kind": "table", "title": "1.1 检测综合评估结论", "columns": ["项目", "结论"], "rows": [["本次", "已核实结论"]]})
    overview.insert(2, {"kind": "paragraph", "title": "1.3 肠道屏障功能评分（IBFC）与损伤分级", "text": "原评估分数"})
    overview.extend([
        {"kind": "paragraph", "title": "1.4 肠道屏障受损类型与受损状况", "text": "原受损类型"},
        {"kind": "paragraph", "title": "1.5 慢性低度炎症评估", "text": "原炎症结果"},
    ])
    by_id["disease_risks"]["title"] = "第二章 相关疾病风险评估"
    systems = ["消化系统疾病", "代谢系统疾病", "心脑血管系统疾病", "精神系统疾病", "神经系统疾病", "免疫系统疾病", "肿瘤"]
    by_id["disease_risks"]["blocks"] = [
        {"kind": "table", "title": f"2.{i} {system}风险评估", "columns": list(RISK_COLUMNS),
         "rows": [[f"疾病{i}-{j}", None, "未评估", "既有机制"] for j in range(1, 4)]}
        for i, system in enumerate(systems, 1)
    ]
    by_id["repair_plan"]["title"] = "第三章 分层修复方案"
    by_id["repair_plan"]["blocks"][0]["title"] = "3.1 肠道屏障修复 5 原则总览"
    by_id["repair_plan"]["blocks"].extend({"kind": "paragraph", "title": f"3.{i} 固定修复说明", "text": "固定正文"} for i in range(2, 7))
    by_id["education"]["title"] = "第四章 肠道屏障与慢性炎症"
    for block in by_id["education"]["blocks"]:
        block["title"] = block["title"].replace("2.", "4.", 1)
    by_id["appendix"]["blocks"] = [
        {"kind": "table", "title": "5.1 质量控制校验", "columns": ["要求", "说明"], "rows": [["质控", "通用要求"]]},
        {"kind": "table", "title": "5.2 检测方法学介绍", "columns": ["方法", "说明"], "rows": [["方法", "通用说明"]]},
        {"kind": "paragraph", "title": "5.3 风险指数标尺说明", "text": "风险指数不是患病概率"},
        {"kind": "list", "title": "5.4 免责声明", "items": ["说明"]},
        {"kind": "references", "title": "5.5 参考文献", "items": [{"citation": "教学用文献格式", "url": None}]},
    ]
    report["sections"] = [by_id[identifier] for identifier in CHAPTER_IDS]
    return report

