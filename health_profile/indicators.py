"""共享检测项目字典；标准项目、报告评估及未标准化结果保持独立语义。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from .models import FactCategory


@dataclass(frozen=True, slots=True)
class IndicatorDefinition:
    fact_type: str
    aliases: tuple[str, ...]
    category: FactCategory = FactCategory.LABORATORY
    group: str = 'laboratory'


def _lab(code: str, *aliases: str) -> IndicatorDefinition:
    return IndicatorDefinition(f'laboratory.{code}', aliases)


INDICATORS = (
    IndicatorDefinition('anthropometric.height', ('身高',), FactCategory.ANTHROPOMETRIC, 'anthropometric'),
    IndicatorDefinition('anthropometric.weight', ('体重',), FactCategory.ANTHROPOMETRIC, 'anthropometric'),
    IndicatorDefinition(
        'anthropometric.bmi', ('体质指数', '身体质量指数', 'BMI'), FactCategory.ANTHROPOMETRIC, 'anthropometric'
    ),
    _lab('hemoglobin', '血红蛋白', '血红蛋白浓度', 'hgb', 'hb', 'hemoglobin'),
    _lab('glucose', '葡萄糖', '血糖', '空腹血糖', 'glu', 'glucose'),
    _lab('alt', '丙氨酸氨基转移酶', '谷丙转氨酶', 'alt'),
    _lab('ast', '天门冬氨酸氨基转移酶', '谷草转氨酶', 'ast'),
    _lab('uric_acid', '尿酸', '血尿酸', 'ua', 'uricacid'),
    _lab('total_cholesterol', '总胆固醇', '胆固醇', 'tc', 'totalcholesterol'),
    _lab('triglyceride', '甘油三酯', '甘油三脂', 'tg', 'triglyceride'),
    _lab('hdl_cholesterol', '高密度脂蛋白胆固醇', '高密度脂蛋白', 'hdl-c', 'hdlc'),
    _lab('ldl_cholesterol', '低密度脂蛋白胆固醇', '低密度脂蛋白', 'ldl-c', 'ldlc'),
    _lab('creatinine', '肌酐', '血肌酐', 'crea', 'cr', 'creatinine'),
    _lab('white_blood_cell_count', '白细胞计数', '白细胞', 'wbc'),
    _lab('red_blood_cell_count', '红细胞计数', '红细胞', 'rbc'),
    _lab('platelet_count', '血小板计数', '血小板', 'plt'),
    _lab('neutrophil_percentage', '中性细胞比率', '中性粒细胞百分比', '中性粒细胞比率', 'neut%'),
    _lab('lymphocyte_percentage', '淋巴细胞比率', '淋巴细胞百分比', 'lym%'),
    _lab('monocyte_percentage', '单核细胞比率', '单核细胞百分比', 'mono%'),
    _lab('eosinophil_percentage', '嗜酸性粒细胞比率', '嗜酸性粒细胞百分比', 'eos%'),
    _lab('basophil_percentage', '嗜碱性粒细胞比率', '嗜碱性粒细胞百分比', 'baso%'),
    _lab('neutrophil_count', '中性细胞数', '中性粒细胞计数', '中性粒细胞绝对值', 'neut#'),
    _lab('lymphocyte_count', '淋巴细胞数', '淋巴细胞计数', 'lym#'),
    _lab('monocyte_count', '单核细胞', '单核细胞数', '单核细胞计数', 'mono#'),
    _lab('eosinophil_count', '嗜酸性粒细胞', '嗜酸性粒细胞数', '嗜酸性粒细胞计数', 'eos#'),
    _lab('basophil_count', '嗜碱性粒细胞', '嗜碱性粒细胞数', '嗜碱性粒细胞计数', 'baso#'),
    _lab('hematocrit', '红细胞压积', '红细胞比容', 'hct'),
    _lab('mean_corpuscular_volume', '红细胞平均体积', '平均红细胞体积', 'mcv'),
    _lab('mean_corpuscular_hemoglobin', '平均血红蛋白量', '平均红细胞血红蛋白量', 'mch'),
    _lab('mean_corpuscular_hemoglobin_concentration', '平均血红蛋白浓度', '平均红细胞血红蛋白浓度', 'mchc'),
    _lab('red_cell_distribution_width_sd', '红细胞分布宽度', '红细胞分布宽度SD', 'rdw-sd'),
    _lab('red_cell_distribution_width_cv', '红细胞分布宽度CV', 'rdw-cv'),
    _lab('platelet_distribution_width', '血小板分布宽度', 'pdw'),
    _lab('mean_platelet_volume', '平均血小板体积', 'mpv'),
    _lab('plateletcrit', '血小板压积', '血小板比容'),
    _lab('platelet_large_cell_ratio', '大型血小板比率', '大型血小板比例', 'p-lcr'),
    _lab('albumin', '白蛋白', '血清白蛋白', 'alb'),
    _lab('total_protein', '总蛋白', '血清总蛋白', 'tp'),
    _lab('total_bilirubin', '总胆红素', 'tbil'),
    _lab('direct_bilirubin', '直接胆红素', 'dbil'),
    _lab('indirect_bilirubin', '间接胆红素', 'ibil'),
    _lab('ggt', '谷氨酰转肽酶', 'γ-谷氨酰转肽酶', 'ggt'),
    _lab('alkaline_phosphatase', '碱性磷酸酶', 'alp'),
    _lab('urea', '尿素'),
    _lab('urea_nitrogen', '尿素氮', 'bun'),
    _lab('hba1c', '糖化血红蛋白', 'hba1c'),
    _lab('c_reactive_protein', 'C反应蛋白', 'crp'),
    _lab('potassium', '血钾', '钾'),
    _lab('sodium', '血钠', '钠'),
    _lab('calcium', '血钙', '钙'),
    _lab('tsh', '促甲状腺激素', 'tsh'),
    _lab('free_t3', '游离三碘甲状腺原氨酸', 'ft3'),
    _lab('free_t4', '游离甲状腺素', 'ft4'),
    _lab('urine_protein', '尿蛋白', '尿蛋白定性'),
    _lab('urine_occult_blood', '尿潜血', '尿隐血'),
    _lab('fecal_occult_blood', '粪便隐血', '便潜血', '大便隐血'),
    IndicatorDefinition('microbiome.diversity', ('菌群多样性', '肠道菌群多样性'), group='microbiome'),
    IndicatorDefinition('microbiome.beneficial_abundance', ('有益菌', '有益菌丰度'), group='microbiome'),
    IndicatorDefinition('microbiome.harmful_abundance', ('有害菌', '有害菌丰度'), group='microbiome'),
    IndicatorDefinition('assessment.microbiome_age', ('肠道年龄',), FactCategory.ASSESSMENT, 'assessment'),
    IndicatorDefinition(
        'assessment.microbiome_health_index',
        ('肠道微生物健康指数', '肠道健康指数'),
        FactCategory.ASSESSMENT,
        'assessment',
    ),
)


def normalize_indicator_name(value: str) -> str:
    return re.sub(r'[\s()（）_\-—–·:：/]+', '', value).casefold()


BY_TYPE = {definition.fact_type: definition for definition in INDICATORS}
BY_ALIAS: dict[str, IndicatorDefinition] = {}
for _definition in INDICATORS:
    for _alias in _definition.aliases:
        _key = normalize_indicator_name(_alias)
        if _key in BY_ALIAS and BY_ALIAS[_key] != _definition:
            raise ValueError(f'duplicate indicator alias: {_alias}')
        BY_ALIAS[_key] = _definition


def report_observation_type(item: dict) -> str:
    """保留未知项目的独立身份；不将不同单位、方法或标本合并成同一指标。"""
    identity = [
        normalize_indicator_name(str(item.get('itemName', ''))),
        *(str(item.get(key) or '').strip().casefold() for key in ('unit', 'method', 'specimen', 'kind')),
    ]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
    return f'report_observation.{digest}'


def is_report_observation_type(fact_type: str) -> bool:
    return re.fullmatch(r'report_observation\.[0-9a-f]{24}', fact_type) is not None


def supports_clinical_projection(fact_type: str) -> bool:
    """报告自带评分和未标准化项目不自动驱动临床提醒或循证。"""
    return not fact_type.startswith(('assessment.', 'microbiome.', 'report_observation.'))
