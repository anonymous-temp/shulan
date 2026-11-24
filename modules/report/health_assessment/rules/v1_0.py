"""Audited constants for health-management assessment rule version 1.0.0.

These values are loaded locally at report-generation time.  The assessment
engine must never retrieve a threshold from EviMed, the public internet or a
language model.
"""

from __future__ import annotations

RULE_VERSION = "1.0.0"

DIMENSIONS = (
    ("gut_microecology", "肠道微生态", 0.20),
    ("gut_barrier", "肠道屏障", 0.20),
    ("glucose_lipid_metabolism", "糖脂代谢", 0.15),
    ("cardiovascular_risk", "心血管风险", 0.15),
    ("weight_body_composition", "体重与体成分", 0.15),
    ("kidney_uric_acid", "肾脏与尿酸", 0.15),
)

LEVEL_BANDS = (
    (75, "good", "状态良好"),
    (55, "attention", "需要关注"),
    (0, "priority_attention", "重点关注"),
)

STATUS_SCORE_PATTERNS = (
    (("轻度肥胖",), 70),
    (("不健康倾向",), 50),
    (
        (
            "重度异常",
            "重度缺乏",
            "重度超标",
            "重度失衡",
            "不健康",
            "严重",
            "显著异常",
            "肥胖",
        ),
        30,
    ),
    (("中度异常", "中度缺乏", "中度超标", "中度失衡", "中等异常", "需求高"), 50),
    (
        (
            "轻度异常",
            "轻度缺乏",
            "轻度超标",
            "轻度失衡",
            "轻度偏高",
            "轻度偏低",
            "亚健康",
        ),
        70,
    ),
    (("失衡",), 50),
    (("优秀", "优良", "很强"), 95),
    (("健康倾向",), 85),
    (("正常", "达标", "适宜"), 85),
    (("偏高", "偏低", "超标", "不足", "缺乏", "丰度低"), 70),
    (("弱", "较弱", "异常", "阳性"), 50),
    (("强", "较强"), 95),
)

RULE_SOURCES = {
    "level_mapping": (
        "assessment.level-mapping.v1",
        "健康管理评估指数等级映射",
        RULE_VERSION,
    ),
    "glycemia": (
        "nhc.diabetes.2023",
        "国家卫生健委《成人糖尿病食养指南（2023年版）》",
        "2023",
    ),
    "lipid": (
        "assessment.lipid.v1",
        "健康管理评估指数血脂分层规则",
        RULE_VERSION,
    ),
    "blood_pressure": (
        "assessment.blood-pressure.v1",
        "健康管理评估指数血压分层规则",
        RULE_VERSION,
    ),
    "body_composition": (
        "nhc.obesity.2024",
        "国家卫生健委《肥胖症诊疗指南（2024年版）》",
        "2024",
    ),
    "kidney": (
        "kdigo.ckd.2024",
        "KDIGO 2024 Clinical Practice Guideline for CKD",
        "2024",
    ),
    "uric_acid": (
        "nhc.hyperuricemia.ws-t560-2017",
        "国家卫生健行业标准 WS/T 560—2017",
        "2017",
    ),
    "microbiome_provider": (
        "provider.microbiome-grade",
        "菌群检测报告中的指标等级或参考范围",
        "检测报告",
    ),
}
