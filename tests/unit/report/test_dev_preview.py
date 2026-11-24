import copy
from pathlib import Path

import pytest

from adapters.report.dev_preview import external_preview_path, render_report_html


def test_priority_table_uses_plain_meaning_not_the_management_goal():
    from adapters.report.dev_preview import _clinical_priority_table
    item = {'item_id': 'p', 'problem': '来源问题', 'priority': '建议复核',
        'tips': {'practical_goal': '明确情况以判断后续管理。'}}
    context = {'report.problem.p.significance': '原检查发现了局部变化，先了解其是否持续。'}
    html = _clinical_priority_table([item], context)
    assert context['report.problem.p.significance'] in html
    assert '明确情况以判断' not in html
    assert item['tips']['practical_goal'] in _clinical_priority_table([item])


def test_patient_preview_hides_audit_columns_and_function_catalog_without_mutation():
    context = {"report.version": "clinical-v1", "report.section.gaps": "审核专用缺项",
        "report.observation.version": "1", "report.auxiliary.version": "1"}
    for key, value in {"kind": "indicator", "name": "异常指标甲", "value": "3", "unit": "U/L",
        "reference": "0–2", "status": "偏高", "date": "2001-01-01", "source": "审核来源", "problems": "内部关联问题"}.items():
        context['report.observation.a.' + key] = value
    for fid, kind, name in (("m", "microbe", "原始菌群甲"), ("f", "function", "辅助功能甲")):
        for key, value in {"kind": kind, "name": name, "value": "0", "unit": "%", "status": "检出", "date": "2001-01-01", "source": "审核来源"}.items():
            context[f'report.auxiliary.{fid}.' + key] = value
    body = {"diagnosis_summary": {"clinical_context": context}}
    original = copy.deepcopy(body)
    html = render_report_html(body)
    for value in ("当前资料与待核实事项", "审核专用缺项", "功能检测参考资料", "辅助功能甲", "审核来源", "内部关联问题", "日期与来源"):
        assert value not in html
    assert '<th>指标</th><th>本次结果</th><th>参考范围</th><th>判定</th>' in html
    assert '原始菌群甲' in html and '0%' in html
    assert body == original


def test_nutrition_table_is_exact_callback_projection_and_refs_numbered_once():
    row = "营养素：测试营养素｜每日剂量：暂不定量｜剂型/理由：一般说明｜服用方式：食物优先｜注意事项：核对适用条件"
    body = {"diagnosis_summary": {"clinical_context": {"report.version": "clinical-v1", "report.section.nutrition": row}},
        "evidence": [{"number": 7, "title": "真实标题", "conclusion": row}]}
    original = copy.deepcopy(body)
    html = render_report_html(body)
    assert '<th>营养素</th><th>每日剂量</th><th>剂型 / 理由</th><th>服用方式</th><th>循证依据</th><th>注意事项</th>' in html
    assert '<td>测试营养素</td>' in html and '<td>暂不定量</td>' in html
    assert '<ol class="ref-list"' not in html
    assert html.count('<li id="ref-7"><span>[7]</span>') == 1
    assert body == original


def test_clinical_preview_retains_unfinished_phase_from_callback_context():
    body = {"diagnosis_summary": {"clinical_context": {"report.version": "clinical-v1",
        "report.section.phase_3": "两至三个月的复评暂未形成可用的个体化说明。"}},
        "diagnostic_advice": {"Plan": {"未来6-12个月": "长期安排。", "未来1-2周": "近期安排。",
            "未来1个月": "一个月内安排。", "未来2-3个月": None}}}
    original = copy.deepcopy(body)
    html = render_report_html(body)
    labels = ("未来1-2周", "未来1个月", "未来2-3个月", "未来6-12个月")
    positions = [html.index(f'<h4>{label}</h4>') for label in labels]
    assert positions == sorted(positions)
    assert '两至三个月的复评暂未形成可用的个体化说明。' in html
    assert body == original


def test_preview_output_must_stay_outside_project_tree(tmp_path):
    project_root = Path(__file__).resolve().parents[3]

    with pytest.raises(ValueError, match="outside the project directory"):
        external_preview_path(project_root / "debug_outputs" / "report.html")

    external = tmp_path / "report.html"
    assert external_preview_path(external) == external.resolve()


def test_preview_accepts_fixed_frontend_callback_contract():
    payload = {
        "userId": "u1",
        "sessionId": "s1",
        "type": "evaluate",
        "status": "partial_success",
        "generated_at": "2026-08-10T12:00:00Z",
        "subject": {
            "name": "患者",
            "gender": "男",
            "age": "28岁",
            "report_date": "2026-04-10",
        },
        "sample": {
            "sample_id": "3718407",
            "sample_type": "粪便",
            "test_item": "肠道菌群检测",
            "sample_date": "2026-04-09",
            "report_date": "2026-04-10",
            "sequencing_platform": None,
        },
        "diagnosis_summary": {
            "conclusion": "固定字段报告总结。",
            "clinical_context": {
                "异常菌群概况": "异常菌群概况。",
                "肠型": "肠型I",
                "健康关注说明": "需要持续关注。",
                "综合健康画像": "综合健康画像。",
            },
            "risk": "中等风险",
            "evidence_refs": [1],
        },
        "diagnostic_basis": {
            "abnormal_findings": [
                {
                    "finding_type": "microbe",
                    "item_id": "taxon-1",
                    "problem": "肠沙门菌 (Salmonella enterica)",
                    "priority": "重点关注",
                    "indicators": [
                        {"name": "肠沙门菌", "value": "0.01%", "status": "超标"}
                    ],
                    "related_diseases_or_adverse_effects": [
                        {
                            "name": "沙门菌病",
                            "description": "与沙门菌病相关。",
                            "relation_type": "epidemiologic_association",
                            "evidence_refs": [1],
                        }
                    ],
                    "biological_mechanism": "经证据校验的医学解释。",
                    "recommendations": [],
                    "tips": {
                        "special_reminder": None,
                        "practical_goal": None,
                    },
                    "interpretation_evidence_refs": [1],
                    "recommendation_evidence_refs": [],
                    "evidence_refs": [1],
                    "evidence_level": "人体文献证据",
                },
                {
                    "finding_type": "disease",
                    "item_id": "disease-crc",
                    "problem": "结直肠癌",
                    "priority": "原报告关联项目",
                    "indicators": [
                        {
                            "name": "原报告模型指数",
                            "value": "1.2",
                            "status": "检测方原值",
                        },
                        {
                            "name": "无痛肠镜检查",
                            "value": "结直肠黏膜大致正常",
                            "status": "已完成",
                        },
                    ],
                    "related_diseases_or_adverse_effects": [],
                    "biological_mechanism": "疾病医学解释。",
                    "recommendations": [
                        "已有检查·结肠镜检查（确认）：用于辅助诊断"
                    ],
                    "tips": {
                        "special_reminder": None,
                        "practical_goal": None,
                    },
                    "interpretation_evidence_refs": [1],
                    "recommendation_evidence_refs": [1],
                    "evidence_refs": [1],
                    "evidence_level": "指南/共识",
                }
            ],
            "evidence_refs": [1],
        },
        "diagnostic_advice": {
            "report_interpretation": ["整体理解。"],
            "Plan": {
                "未来1-2周": None,
                "未来1个月": None,
                "未来2-3个月": None,
                "未来6-12个月": None,
            },
            "health_recommendations": [
                {
                    "category": "general",
                    "topic": "心血管与代谢健康",
                    "priority": "重点关注",
                    "content": "持续异常可能增加长期心血管风险。",
                    "evidence_refs": [1],
                }
            ],
            "recommended_examinations": [
                {
                    "name": "糖化血红蛋白测定",
                    "purpose": "评估",
                    "reason": "用于了解近期血糖的整体情况。",
                    "evidence_refs": [1],
                }
            ],
            "not_recommended_behaviors": {},
            "medical_alerts": [],
        },
        "evidence": [
            {
                "number": 1,
                "evidence_type": "literature",
                "topic": "菌种与疾病关联",
                "title": "证据标题",
                "source": "期刊",
                "publication_date": "2025",
                "url": None,
                "evidence_level": "peer_reviewed_literature",
                "provider": None,
                "summary": None,
                "conclusion": "人体研究观察到相关健康方向。",
                "key_evidence": "研究类型：队列研究",
                "patient_related_facts": ["肠沙门菌：超标，0.01%"],
                "evidence_grade": "中",
                "study_type": None,
                "authors": [],
            }
        ],
        "partial_reasons": [],
        "error": {"error_code": None, "message": None},
    }
    html = render_report_html(payload)
    assert "2026-08-10" in html
    assert "2026-08-10T12:00:00Z" not in html
    assert "固定字段报告总结" not in html
    assert "肠沙门菌" in html
    assert "沙门菌病" not in html
    assert "相关疾病或不良影响" not in html
    assert payload["diagnostic_basis"]["abnormal_findings"][0][
        "related_diseases_or_adverse_effects"
    ][0]["name"] == "沙门菌病"
    assert "用户相关信息" not in html
    assert "肠沙门菌：超标，0.01%" in html
    assert "研究类型：队列研究" not in html
    assert "肠型I" not in html
    assert "健康总结" in html
    assert "健康调护" not in html
    assert "建议关注方向" in html
    assert "心血管与代谢健康" in html
    assert "建议补充检查" in html
    assert "<strong>糖化血红蛋白测定</strong>（评估）" in html
    assert "用于了解近期血糖的整体情况" in html
    assert "健康关注说明" not in html
    assert "综合健康画像。" in html
    assert "证据标题" in html
    assert "证据等级：中" in html
    assert "结论：" in html
    assert "证据：" in html
    assert "参考：" in html
    assert "人体研究观察到相关健康方向。" in html
    assert "3718407" not in html
    assert "2026-04-09" in html
    assert "菌群报告日期" in html
    assert "本次评估生成时间" in html
    assert "无痛肠镜检查" not in html
    assert "已有检查·结肠镜检查" in html
    assert "医学解读依据：[1]" in html
    assert "检查建议依据：[1]" in html


def test_preview_does_not_mutate_callback_json_and_groups_rule_sources():
    payload = {
        "subject": {},
        "sample": {},
        "healthAssessment": {
            "overallScoreAvailable": True,
            "displayOverallScore": "70",
            "levelLabel": "需要关注",
            "summary": "综合评估需要关注。",
            "dimensions": [
                {
                    "name": "代谢系统",
                    "score": 70,
                    "scoreAvailable": True,
                    "displayScore": "70",
                    "levelLabel": "需要关注",
                    "explanation": "代谢系统的长期健康表现需要关注。",
                    "relatedDiseases": [
                        {
                            "name": "2型糖尿病",
                            "riskLevel": "high",
                            "riskLabel": "高风险",
                            "source": "临床资料",
                            "primary": True,
                        },
                        {
                            "name": "高脂血症",
                            "riskLevel": "medium",
                            "riskLabel": "中风险",
                            "source": "菌群报告",
                            "primary": True,
                        },
                        {
                            "name": "高尿酸血症",
                            "riskLevel": "pending",
                            "riskLabel": "风险待评估",
                            "source": "临床资料",
                            "primary": True,
                        },
                    ],
                    "evidenceUsed": [],
                    "ruleBasis": [
                        {
                            "ruleId": "ldl",
                            "indicator": "LDL-C",
                            "source": "健康管理规则",
                            "version": "1.0.0",
                            "boundary": "边界A",
                        },
                        {
                            "ruleId": "tc",
                            "indicator": "总胆固醇",
                            "source": "健康管理规则",
                            "version": "1.0.0",
                            "boundary": "边界B",
                        },
                    ],
                }
            ],
            "abnormalIndicators": [],
        },
        "diagnosis_summary": {"conclusion": "综合结论。"},
        "diagnostic_basis": {"abnormal_findings": []},
        "diagnostic_advice": {"health_recommendations": []},
        "evidence": [],
    }
    original = copy.deepcopy(payload)

    html = render_report_html(payload)

    assert payload == original
    assert "综合结论。" not in html
    assert html.count("健康管理规则；1.0.0") == 1
    assert "LDL-C：边界A" in html
    assert "总胆固醇：边界B" in html
    assert "2型糖尿病" in html and "高风险" in html
    assert "高脂血症" in html and "中风险" in html
    assert "高尿酸血症" in html and "风险待评估" in html
    assert 'class="risk-dot high"' in html
    assert 'class="risk-dot medium"' in html
    assert 'class="risk-dot pending"' in html


def test_preview_omits_microbe_relation_column_without_mutating_payload():
    payload = {
        "subject": {},
        "sample": {},
        "healthAssessment": {"dimensions": [], "abnormalIndicators": []},
        "diagnosis_summary": {"clinical_context": {}},
        "diagnostic_basis": {
            "abnormal_findings": [
                {
                    "finding_type": "microbe",
                    "problem": "示例菌",
                    "indicators": [{"name": None, "value": "0.1%", "status": "丰度低"}],
                    "related_diseases_or_adverse_effects": [],
                    "biological_mechanism": "本次结果的专业解读。",
                    "tips": {
                        "special_reminder": "低丰度本身不提示相关感染或不良影响。",
                        "practical_goal": "证据不足",
                    },
                    "interpretation_evidence_refs": [],
                    "evidence_refs": [],
                },
                {
                    "finding_type": "disease",
                    "problem": "肾结石",
                    "biological_mechanism": "关联风险解释。",
                    "recommendations": [],
                    "tips": {
                        "special_reminder": "关注泌尿系统症状。",
                        "practical_goal": None,
                    },
                    "interpretation_evidence_refs": [1],
                    "recommendation_evidence_refs": [],
                    "evidence_refs": [1],
                },
            ]
        },
        "diagnostic_advice": {"health_recommendations": []},
        "evidence": [],
    }

    original = copy.deepcopy(payload)
    html = render_report_html(payload)
    assert payload == original
    assert "固定字段报告总结" not in html
    assert "示例菌" in html
    assert "肾结石" in html
    assert "本次结果的专业解读。" in html
    assert "相关疾病或不良影响" not in html
    assert "低丰度本身不提示相关感染或不良影响" not in html
    assert "证据不足" in html
    assert "当前无符合循证要求的新增医院检查建议" not in html
