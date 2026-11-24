"""Source-bound neutral meanings for otherwise unexplained evaluate items.

Existing explanation slots receive finite term definitions where useful.
Generic limits for taxon/resistance detail rows belong to their shared chapter
text, not a repeated explanation for every observation. This is deterministic
display completion, not a result interpretation, diagnosis, or evidence claim.
"""

from __future__ import annotations

import re
import hashlib
import json
import unicodedata
from copy import deepcopy

from modules.report import evaluation_catalog as catalog
from modules.report.schemas import ReportSourceFact
from modules.report.evaluation_prose_roles import measurement_scope_text_allowed
from modules.report.evaluation_distribution import build_source_distribution, render_source_distribution
from modules.report.evaluation_distribution import (
    _SourceStateAuthority, _mint_source_state_authority, _source_state_sealed,
)


def _key(value: str | None) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value or "")).casefold()


_ALIASES = {
    "gmwi2": ("GMWI2", "肠道微生物组健康指数GMWI2", "肠道菌群健康指数GMWI2"),
    "gmhi": ("GMHI", "肠道微生物健康指数", "肠道菌群健康指数"),
    "gutHealthIndex": ("gut_health_index", "肠菌健康指数"),
    "gutAge": ("gut_age", "肠道年龄"),
    "microbiomeHealthScore": ("microbiome_health_score", "肠道菌群健康度评分"),
    "speciesCount": ("species_count", "肠道微生物种类", "肠道菌群种类", "肠道菌群种类数", "菌群种类", "菌种数", "肠道菌群种类评估", "肠道微生物种类评估"),
    "diversity": ("shannon", "香农指数", "肠道菌群多样性", "肠道微生物多样性", "多样性指数", "菌群多样性指数", "香农多样性指数"),
    "beRatio": ("be_ratio", "B/E比值", "肠道定植抗力", "肠道微生物定植抗力"),
    "fbRatio": ("fb_ratio", "F/B比值", "厚壁菌门拟杆菌门比值", "厚壁菌门拟杆菌门", "肠道菌群热量代谢F/B比值",
        "F/B 比值（厚壁菌门/拟杆菌门）", "厚壁菌门/拟杆菌门（F/B）"),
    "beBalanceScore": ("be_score", "B/E评分", "肠道菌群平衡状态评估"),
    "fbEnergyMetabolismScore": ("fb_score", "F/B评分", "肠道菌群热量代谢评估"),
    "beneficialBacteria": ("panel_beneficial",),
    "pathogenicBacteria": ("panel_pathogenic",),
    "opportunisticBacteria": ("panel_opportunistic",),
    "antiInflammatoryCapacity": ("anti_inflammatory", "anti_inflammatory_capacity", "肠道菌群抗炎能力", "抗炎能力"),
    "gutImmunity": ("gut_immunity", "肠道免疫力"),
    "dietaryFiberNeed": ("dietary_fiber_need", "肠道膳食纤维需求"),
    "biofilmCapacity": ("biofilm_capacity", "肠道菌群生物膜合成能力", "肠道菌群生物膜合成能力评估"),
    "oxidativeStressTolerance": ("oxidative_stress_tolerance", "肠道菌群氧化胁迫耐受", "肠道菌群氧化耐受胁迫"),
    "pathogenicPotential": ("pathogenic_potential",),
    "keystoneBacteria": ("核心菌属构成",),
    "barrierRelatedBacteria": ("肠道屏障功能损伤相关菌", "肠道屏障损伤相关菌评估"),
    "developmentalMaturity": ("肠道菌群发育成熟度相关菌群",),
    "childGrowth": ("影响儿童生长指标相关肠道菌群", "影响儿童生长指标相关肠道菌群评估"),
    "formate": ("formate", "甲酸"), "acetate": ("acetate", "乙酸"),
    "propionate": ("propionate", "丙酸"), "butyrate": ("butyrate", "丁酸"),
    "valerate": ("valerate", "戊酸"),
    "carbohydrate": ("carbohydrate_metabolism", "碳水化合物代谢", "碳水化合物代谢能力"),
    "fat": ("fat_metabolism", "脂肪代谢", "脂肪代谢能力"),
    "protein": ("protein_metabolism", "蛋白质代谢", "蛋白质代谢能力"),
    "minerals": ("mineral_metabolism", "矿物质代谢", "矿物质代谢能力"),
    "serotonin": ("5HT", "血清素", "5羟色胺"), "dopamine": ("dopamine",),
    "acetylcholine": ("acetylcholine",), "gaba": ("GABA", "伽马氨基丁酸"),
    "histamine": ("histamine",), "norepinephrine": ("norepinephrine", "noradrenaline"),
    "endorphins": ("endorphin",),
}
for _definitions in (catalog.OVERALL_STRUCTURE_DEFINITIONS, catalog.HEALTH_METRIC_DEFINITIONS,
        catalog.SCFA_DEFINITIONS, catalog.MAJOR_METABOLISM_DEFINITIONS,
        catalog.NEUROTRANSMITTER_DEFINITIONS):
    for _code, _name in _definitions:
        _ALIASES[_code] = (*_ALIASES.get(_code, ()), _name)
_CANONICAL = {_key(alias): code for code, aliases in _ALIASES.items() for alias in (code, *aliases)}


# Basic substance/pathway definitions, independent of the patient's score.
# NIH ODS glossary: https://ods.od.nih.gov/HealthInformation/dictionary/
# Amino acids: https://medlineplus.gov/ency/article/002222.htm
# Metabolism: https://www.ncbi.nlm.nih.gov/books/NBK26882/
# Pentose pathway: https://www.ncbi.nlm.nih.gov/books/NBK551687/
# Signalling: https://www.ncbi.nlm.nih.gov/books/NBK9924/
# Purines: https://www.cancer.gov/publications/dictionaries/cancer-terms/def/purine
# Lipids: https://www.ncbi.nlm.nih.gov/books/NBK234932/
# Sugars: https://www.ncbi.nlm.nih.gov/books/NBK453043/
# CoQ: https://www.ncbi.nlm.nih.gov/mesh/68014451
# Biofilms: https://wwwnc.cdc.gov/eid/article/7/2/70-0277_article
# RNA testing: https://medlineplus.gov/ency/article/007769.htm
# These definitions do not assert that a patient's microbes provide the named
# substance, or that the score measures its concentration or physiological effect.
_TERM_MEANINGS = {
    "碳水化合物": "碳水化合物是糖、淀粉和膳食纤维等物质的总称。",
    "脂肪": "脂肪是一类营养物质，属于脂质。",
    "矿物质": "矿物质是钙、铁等无机营养元素的统称。",
    "蛋白质": "蛋白质由氨基酸连接而成，氨基酸可理解为构成蛋白质的基本单元。",
    "丙酮酸": "丙酮酸是葡萄糖等物质分解过程中形成的一种中间产物。",
    "葡萄糖 (糖酵解途径)": "糖酵解是细胞将葡萄糖逐步分解为丙酮酸的一组反应。",
    "葡萄糖 (磷酸戊糖途径)": "磷酸戊糖途径是葡萄糖代谢的一条分支，可形成核酸组成所需的糖和参与氧化还原反应的物质。",
    "果糖和甘露糖": "果糖和甘露糖均为单糖，即结构较简单的糖类。",
    "半乳糖": "半乳糖是一种单糖，也是乳糖的组成成分之一。",
    "淀粉和蔗糖": "淀粉是许多葡萄糖单元连接而成的糖类，蔗糖由葡萄糖和果糖组成。",
    "氨基糖和核苷酸糖": "氨基糖是含氨基的糖类；核苷酸糖是糖与核苷酸连接形成的活化形式。",
    "甘油磷脂": "甘油磷脂是一类含甘油和磷酸基团的脂质，是细胞膜的组成成分。",
    "甘油酯": "甘油酯是甘油与脂肪酸结合形成的一类脂质。",
    "鞘脂类": "鞘脂类是以鞘氨醇等长链结构为骨架的一类脂质，也是细胞膜的组成成分。",
    "脂肪酸": "脂肪酸是脂肪等脂质的组成单元，包括饱和和不饱和脂肪酸。",
    "花生四烯酸": "花生四烯酸是一种多不饱和脂肪酸。",
    "亚麻酸": "亚麻酸是一类不饱和脂肪酸名称，具体异构体以原项目为准。",
    "胆汁酸": "胆汁酸是一类由胆固醇转化形成、参与脂肪消化吸收的物质。",
    "嘌呤代谢": "嘌呤是构成DNA、RNA等分子的化学成分之一；嘌呤代谢指其合成、转化和分解过程。",
    "叶酸": "叶酸又称维生素B9，是B族维生素之一。",
    "类黄酮": "类黄酮是植物中存在的一大类天然化合物。",
    "叶黄素": "叶黄素是一种类胡萝卜素，属于天然色素。",
    "花青素": "花青素是一类可使植物呈现红、紫、蓝等颜色的天然色素。",
    "异黄酮素": "异黄酮是一类天然植物化合物，属于黄酮类物质。",
    "叶绿素": "叶绿素是植物等生物进行光合作用时利用的绿色色素。",
    "类胡萝卜素": "类胡萝卜素是一组天然色素，包括叶黄素、番茄红素等物质。",
    "番茄红素": "番茄红素是一种红色色素，属于类胡萝卜素。",
    "牛磺酸": "牛磺酸是一种含硫的氨基磺酸，不是构成蛋白质的常规氨基酸。",
    "谷胱甘肽": "谷胱甘肽是由谷氨酸、半胱氨酸和甘氨酸三个单元组成的小分子肽。",
    "辅酶Q": "辅酶Q是一组参与细胞电子传递的物质；原项目未明确时不等同特定的辅酶Q10。",
    "硫辛酸": "硫辛酸是一种含硫化合物，可参与细胞内的酶促代谢反应。",
}
for _name in ("赖氨酸", "亮氨酸", "苯丙氨酸", "组氨酸", "酪氨酸", "脯氨酸", "谷氨酸", "精氨酸",
        "甘氨酸", "丝氨酸", "色氨酸", "缬氨酸", "半胱氨酸", "丙氨酸", "苏氨酸", "异亮氨酸",
        "蛋氨酸", "天冬氨酸", "天冬酰胺", "谷氨酰胺"):
    _TERM_MEANINGS[_name] = f"{_name}是一种氨基酸，属于构成蛋白质的基本单元。"
for _number, _common in (("B1", "硫胺素"), ("B2", "核黄素"), ("B3", "烟酸类物质"),
        ("B5", "泛酸"), ("B6", "吡哆醇等相关物质"), ("B7", "生物素"), ("B12", "钴胺素类物质")):
    _TERM_MEANINGS[f"维生素{_number}"] = f"维生素{_number}（{_common}）是B族维生素之一。"
for _name, _meaning in (("A", "一组脂溶性维生素"), ("C", "又称抗坏血酸，是水溶性维生素"),
        ("E", "一组脂溶性维生素"), ("K2", "维生素K中的甲萘醌类物质，属于脂溶性维生素")):
    _TERM_MEANINGS[f"维生素{_name}"] = f"维生素{_name}是{_meaning}。" if _name != "C" else f"维生素C{_meaning}。"
for _name in ("硒", "铁", "锌", "钙", "镁"):
    _TERM_MEANINGS[_name] = f"{_name}是一种矿物质元素，属于人体所需的无机营养元素。"
_TERM_MEANINGS = {_key(name): text for name, text in _TERM_MEANINGS.items()}

# SCFA names retain the source's panel membership. In particular, formate is
# defined as a one-carbon organic acid rather than silently reclassified.
_SCFA_MEANINGS = {
    "formate": "甲酸是一种含一个碳原子的有机酸。",
    "acetate": "乙酸是一种含两个碳原子的有机酸，属于常见短链脂肪酸。",
    "propionate": "丙酸是一种含三个碳原子的有机酸，属于短链脂肪酸。",
    "butyrate": "丁酸是一种含四个碳原子的有机酸，属于短链脂肪酸。",
    "valerate": "戊酸是一种含五个碳原子的有机酸，属于短链脂肪酸。",
}
_NEURO_MEANINGS = {
    "serotonin": "5-羟色胺又称血清素，是参与细胞间信号传递的一种神经递质。",
    "dopamine": "多巴胺是参与神经细胞间信号传递的一种神经递质。",
    "acetylcholine": "乙酰胆碱是神经细胞向其他神经细胞或肌肉等细胞传递信号的一种物质。",
    "gaba": "γ-氨基丁酸（GABA）是一种氨基酸类神经递质，是参与神经信号传递的物质。",
    "histamine": "组胺是参与细胞间信号传递的一种胺类物质，也可作为神经递质。",
    "norepinephrine": "去甲肾上腺素是一种神经递质，也可作为激素参与信号传递。",
    "endorphins": "内啡肽是一组由氨基酸连接而成的肽类信号物质。",
}
_CORE_TERM_MEANINGS = {
    "keystoneBacteria": "核心菌群构成描述核心成员的含量及组成分布。",
    "barrierRelatedBacteria": "本项汇总肠道屏障相关菌群的检测状态，关注其组成及相对参考范围的变化。",
    "developmentalMaturity": "发育成熟度相关菌群评估观察儿童肠道菌群的构成和发育相关特征。",
    "childGrowth": "生长相关菌群评估观察与儿童生长相关的肠道菌群特征。",
    "dietaryFiberNeed": "膳食纤维是人体消化酶难以消化的一类食物成分。",
    "biofilmCapacity": "生物膜是微生物聚集并被其产生的细胞外基质包裹的群体结构。",
    "oxidativeStressTolerance": "氧化胁迫指活性氧等物质增多、超过相应清除能力时形成的应激环境；此处耐受指菌群对这类环境的应对。",
    "pathogenicPotential": "致病潜力评估微生物的致病相关特征；毒力是微生物引起损伤等效应的能力。",
}

# Finite disease-name glossary; definitions describe terms, never this patient.
# NIDDK: https://www.niddk.nih.gov/health-information/digestive-diseases/crohns-disease/definition-facts
# https://www.niddk.nih.gov/health-information/digestive-diseases/ulcerative-colitis/definition-facts
# https://www.niddk.nih.gov/health-information/digestive-diseases/irritable-bowel-syndrome/definition-facts
# https://www.niddk.nih.gov/health-information/liver-disease/autoimmune-hepatitis/definition-facts
# https://www.niddk.nih.gov/health-information/liver-disease/nafld-nash/definition-facts
# https://www.niddk.nih.gov/health-information/liver-disease/cirrhosis/definition-facts
# https://www.niddk.nih.gov/health-information/diabetes/overview/what-is-diabetes/type-2-diabetes
# NIAMS: https://www.niams.nih.gov/health-topics/lupus
# https://www.niams.nih.gov/health-topics/rheumatoid-arthritis
# https://www.niams.nih.gov/health-topics/ankylosing-spondylitis
# NIMH: https://www.nimh.nih.gov/health/topics/schizophrenia
# https://www.nimh.nih.gov/health/topics/bipolar-disorder
# MedlinePlus: https://medlineplus.gov/multiplesclerosis.html
# https://medlineplus.gov/epilepsy.html
# https://medlineplus.gov/parkinsonsdisease.html
# https://medlineplus.gov/alzheimersdisease.html
# NHLBI: https://www.nhlbi.nih.gov/health/coronary-heart-disease
# These sources support vocabulary only, not the source risk model or its score.
# Deliberately no spelling correction or expansion of ambiguous source names.
_DISEASE_MEANINGS = {
    "炎症性肠病": ("炎症性肠病是一组使消化道发生慢性炎症的疾病，包括克罗恩病和溃疡性结肠炎等。", r"(?:消化道|肠道|肠).{0,12}(?:慢性|长期|持续|反复).{0,6}(?:炎症|发炎)|克罗恩病.{0,8}溃疡性结肠炎"),
    "溃疡性结肠炎": ("溃疡性结肠炎是一种使大肠内壁发生慢性炎症和溃疡的疾病。", r"(?:大肠|结肠|肠道).{0,15}(?:炎症|发炎).{0,8}溃疡|(?:大肠|结肠).{0,10}慢性炎症"),
    "克罗恩病": ("克罗恩病是一种可累及消化道不同部位的慢性炎症性疾病。", r"(?:消化道|肠道).{0,16}(?:慢性|长期|反复).{0,6}(?:炎症|发炎)|慢性.{0,6}炎症性肠病"),
    "肠易激综合征": ("肠易激综合征是一种肠道与大脑相互作用相关的功能性肠道疾病，表现为反复腹痛并伴排便习惯改变。", r"功能性.{0,6}(?:肠道|胃肠)|肠.{0,8}脑.{0,8}(?:互动|相互作用)|反复腹痛.{0,12}排便"),
    "腹泻型肠易激综合征": ("腹泻型肠易激综合征是以腹泻为主要排便变化的肠易激综合征，属于肠道与大脑相互作用相关的功能性肠道疾病。", r"功能性.{0,6}(?:肠道|胃肠)|肠.{0,8}脑.{0,8}(?:互动|相互作用)|以腹泻.{0,10}(?:主要|为主)"),
    "系统性红斑狼疮": ("系统性红斑狼疮是一种免疫系统攻击自身组织、可影响身体多个部位的慢性疾病。", r"(?:免疫系统|免疫).{0,12}(?:攻击|损伤).{0,8}(?:自身|自己)|自身免疫性?疾病"),
    "多发性硬化症": ("多发性硬化症是一种影响脑和脊髓的疾病，会损伤神经纤维外的保护层（髓鞘）。", r"(?:损伤|破坏|受损|脱失).{0,16}(?:髓鞘|保护层)|(?:髓鞘|保护层).{0,8}(?:损伤|破坏|受损|脱失)"),
    "类风湿性关节炎": ("类风湿性关节炎是一种免疫系统攻击自身组织、主要影响关节的慢性疾病。", r"(?:免疫系统|免疫).{0,12}(?:攻击|损伤).{0,8}(?:自身|自己)|自身免疫性?疾病"),
    "强直性脊柱炎": ("强直性脊柱炎是一种主要使脊柱关节和韧带发生炎症的关节炎。", r"(?:脊柱|脊椎).{0,12}(?:关节|韧带|炎症|发炎)"),
    "自身免疫性肝炎": ("自身免疫性肝炎是免疫系统攻击肝脏而引起肝脏炎症的慢性疾病。", r"(?:免疫系统|免疫).{0,12}(?:攻击|损伤).{0,8}肝|自身免疫性?肝脏疾病"),
    "精神分裂症": ("精神分裂症是一种影响思维、感受和行为的精神疾病。", r"(?:思维|思考|感知).{0,10}(?:感受|情感|行为|现实)"),
    "癫痫": ("癫痫是一种脑部疾病，其反复发作与脑内神经细胞异常发送信号有关。", r"脑.{0,12}(?:异常放电|异常.{0,6}信号)|反复.{0,6}(?:癫痫发作|痫性发作)"),
    "帕金森病": ("帕金森病是一种逐渐进展、主要影响运动控制的神经系统疾病。", r"(?:运动|动作).{0,8}(?:控制|障碍)|神经退行性|神经系统疾病"),
    "阿尔兹海默症": ("阿尔兹海默症是一种逐渐损害记忆、思考及日常活动能力的脑部疾病。", r"记忆.{0,12}(?:思考|思维|认知)|神经退行性|脑部疾病"),
    "双相情感障碍": ("双相情感障碍是一种出现明显情绪、精力和活动水平变化的精神疾病，可有躁狂或轻躁狂及抑郁发作。", r"躁狂|轻躁狂|情绪.{0,8}(?:精力|活动水平)"),
    "非酒精性脂肪肝": ("非酒精性脂肪肝指并非由大量饮酒引起的肝脏脂肪过多积聚。", r"肝.{0,10}脂肪.{0,8}(?:积聚|堆积|积累|过多)"),
    "II型糖尿病": ("II型糖尿病是身体不能有效利用胰岛素或胰岛素分泌不足、使血糖升高的一类糖尿病。", r"胰岛素.{0,12}(?:抵抗|不足|分泌)|(?:不能|无法).{0,8}(?:有效利用|正常使用).{0,6}胰岛素"),
    "冠心病": ("冠心病是向心肌供血的冠状动脉发生病变、影响心脏血液供应的疾病。", r"冠状动脉|心肌.{0,8}(?:供血|血液供应)"),
    "肝硬化": ("肝硬化是肝脏中的健康组织被瘢痕组织替代、影响肝脏正常工作的疾病。", r"(?:瘢痕|疤痕|纤维化).{0,8}组织|肝.{0,10}纤维化"),
}
_DISEASE_MEANINGS = {_key(name): pair for name, pair in _DISEASE_MEANINGS.items()}


def _disease_definition(row: dict) -> tuple[str, str] | None:
    for name in (row.get("name"), row.get("scientificName")):
        for key in _names(name):
            if key in _DISEASE_MEANINGS:
                return _DISEASE_MEANINGS[key]
    return None


def _complete_disease_text(row: dict, existing: str) -> str:
    pair = _disease_definition(row)
    if pair is None:
        return existing
    definition, signal = pair
    if definition in existing:
        return existing
    for clause in re.split(r"[，,。；;！？\n]", existing):
        if re.search(r"不能|不可|不应|无法|不代表|不等同|不等于|不构成|不说明|不意味着|不是|并非|是否|没有|缺乏|未(?:提供|明确|说明|检查)|尚无", clause):
            continue
        if re.search(signal, clause, re.I) and re.search(r"是|指|属于|影响|损伤|攻击|累及|替代|导致", clause):
            return existing
    return existing + ("\n" if existing else "") + definition


def _bound_disease_fact(row: dict, facts: dict[str, ReportSourceFact]) -> ReportSourceFact | None:
    fact = _bound_fact(row, facts, "disease")
    if fact is None:
        return None
    status = row.get("sourceStatus")
    if status is not None:
        for source in row.get("sources") or []:
            own = facts.get(source.get("recordId")) if isinstance(source, dict) else None
            if (own is not None and own.status is not None
                    and _bound_fact(row, {own.fact_id: own}, "disease") is not None
                    and _key(status) != _key(own.status)):
                return None
    return fact


def _identity(name: str) -> str | None:
    key = _key(name)
    if key in _CANONICAL:
        return _CANONICAL[key]
    for prefix in ("肠道菌群", "菌群", "来源"):
        if key.startswith(prefix):
            key = key[len(prefix):]
            break
    if key in _CANONICAL:
        return _CANONICAL[key]
    # Only approved, exact function names may lose a display suffix. A word
    # containing an acid name or a generic score is not an alias for that acid.
    base = key
    for suffix in ("合成能力评估", "合成能力", "合成评分", "synthesisscore", "synthesisability",
            "合成", "synthesis", "代谢能力评估", "代谢能力", "代谢评分", "metabolismscore", "代谢", "metabolism", "评估"):
        if key.endswith(suffix):
            base = key[:-len(suffix)]
            break
    return _CANONICAL.get(base)


def _names(name: str | None) -> set[str]:
    if not isinstance(name, str) or not name.strip():
        return set()
    parts = re.split(r"\s+/\s+", name)
    result = set()
    for part in parts:
        part = re.sub(r"[（(]来源疾病模型指数[）)]$", "", part).strip()
        result.add(_key(part))
        if identity := _identity(part):
            result.add("identity:" + identity)
    return result - {""}


def _value(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip()
    if text.casefold() in {'true', 'false'}:
        return text.casefold()
    if text in {"未提供", "未提供数值", "未提供指数", "无数据", "待提供", "--", "—", "N/A"}:
        return None
    return text or None


def _bound_fact(row: dict, facts: dict[str, ReportSourceFact], kind: str) -> ReportSourceFact | None:
    value = _value(row.get("value"))
    if value is None:
        return None
    names = _names(row.get("name")) | _names(row.get("scientificName"))
    if not names:
        return None
    expected_kinds = {"clinical"} if kind == "clinical" else {"microbe"} if kind in {"taxa", "phylum"} else {"function", "context"}
    expected_source = "A" if kind == "clinical" else "C"
    for source in row.get("sources") or []:
        if not isinstance(source, dict) or source.get("sourceCode") not in {None, expected_source}:
            continue
        fact = facts.get(source.get("recordId"))
        if fact is None or fact.kind not in expected_kinds:
            continue
        actual_value = _value(fact.value)
        if actual_value != value:
            continue
        fact_names = _names(fact.name)
        # This exact source heading is a B/E ratio only when the typed
        # projection already identifies it as that ratio. The separate
        # balance-score heading and other unqualified records stay distinct.
        if row.get("code") == "beRatio" and _key(fact.name) == _key("肠道菌群平衡状态"):
            fact_names.add("identity:beRatio")
        # Clinical extraction may explicitly qualify a generic source name
        # with its supplied specimen; this does not infer that specimen here.
        if kind == "clinical" and re.fullmatch(r"尿(?:红细胞(?:总数)?|比重)", fact.name):
            fact_names |= _names(fact.name[1:])
        if not names & fact_names:
            continue
        unit, fact_unit = row.get("unit"), fact.unit
        normalize_unit = lambda unit: re.sub(r"\s+", "", unicodedata.normalize("NFKC", unit or "")).casefold()
        if normalize_unit(unit) != normalize_unit(fact_unit):
            continue
        if source.get("observedAt") is not None and fact.date is not None and source["observedAt"] != fact.date:
            continue
        return fact
    return None


_HEALTH_MEMBER_PANELS = {
    'beneficialBacteria': 'beneficial', 'scfaProducerBacteria': 'scfaProducers',
    'pathogenicBacteria': 'pathogenic', 'opportunisticBacteria': 'opportunistic',
    'pathogenicFungi': 'fungi_pathogenic', 'pathogenicViruses': 'virus',
    'pathogenicParasites': 'parasites',
}
_GENE_MARKER_GROUPS = frozenset(_key(name) for name in (
    '肠致病性大肠杆菌（EPEC）', '艰难梭菌毒素基因',
    '产志贺毒素大肠埃希氏菌（STEC）', '肠产毒素阴沟肠杆菌', '志贺肠毒素基因'))


def health_state_source_context(row: dict, all_health_rows: list[dict],
                                facts: dict[str, ReportSourceFact], *, member_rows=()) -> dict:
    """Separate an original panel result from its measured member states.

    This private author/reviewer frame contains source data only. It never
    writes a patient explanation, grades a result, or converts zero/normal
    into a negative detection. Gene group labels retain their original names
    while their object type stays a marker, rather than an organism diagnosis.
    """
    def bound_ids(item, kind):
        result = []
        for source in item.get('sources') or []:
            if not isinstance(source, dict) or source.get('sourceCode') not in {None, 'C'}:
                continue
            fid = source.get('recordId')
            fact = facts.get(fid)
            if fact is None or _bound_fact(item, {fid: fact}, kind) is None:
                continue
            if (item.get('sourceStatus') is not None and fact.status is not None
                    and _key(str(item['sourceStatus'])) != _key(fact.status)):
                continue
            if fid not in result:
                result.append(fid)
        return result

    def checksums(item):
        return {match[1].lower() for source in item.get('sources') or []
            if isinstance(source, dict) and (match := re.match(
                r'^source:([a-fA-F0-9]{64}):', str(source.get('recordId') or '')))}

    own_ids = bound_ids(row, 'health')
    own_checksums = checksums(row)

    def same_report(item, ids):
        if not own_ids or not ids:
            return False
        candidate_checksums = checksums(item)
        if own_checksums and candidate_checksums and candidate_checksums != own_checksums:
            return False
        return any(facts[owner].source and facts[owner].source == facts[fid].source
            and (facts[owner].date is None or facts[fid].date is None
                or facts[owner].date == facts[fid].date)
            for owner in own_ids for fid in ids)

    def snapshot(item, ids, object_type):
        if not ids:
            return None
        result = {key: deepcopy(item.get(key)) for key in (
            'code', 'name', 'value', 'unit', 'referenceRange', 'sourceStatus', 'assessmentLevel')}
        result.update(factIds=list(ids), objectType=object_type)
        return result

    marker = _key(str(row.get('name') or '')) in _GENE_MARKER_GROUPS
    aggregate = snapshot(row, own_ids, 'geneMarker' if marker else 'panelResult')
    members = []
    panel = _HEALTH_MEMBER_PANELS.get(row.get('code'))
    candidates = [item for item in member_rows if isinstance(item, dict)]
    if panel:
        candidates.extend(item for item in all_health_rows if isinstance(item, dict)
            and str(item.get('code') or '').startswith('panelResult:' + panel + ':'))
    for item in candidates:
        ids = bound_ids(item, 'taxa')
        if not same_report(item, ids):
            continue
        state = snapshot(item, ids, 'measuredMember')
        # Fold only identical mirrors carrying the same source fact. Different
        # dates, values, original grades and references remain distinct.
        previous = next((member for member in members
            if set(member['factIds']).intersection(ids)
            and all(member.get(key) == state.get(key) for key in (
                'name', 'value', 'unit', 'referenceRange', 'sourceStatus', 'assessmentLevel'))), None)
        if previous is None:
            members.append(state)
        else:
            previous['factIds'] = list(dict.fromkeys([*previous['factIds'], *ids]))

    genes = []
    if row.get('code') == 'virulenceGenes' or marker:
        from .evaluation_function_evidence import _detected_gene_symbols
        for item in ([row] if marker else all_health_rows):
            if not isinstance(item, dict) or _key(str(item.get('name') or '')) not in _GENE_MARKER_GROUPS:
                continue
            ids = bound_ids(item, 'health')
            if not same_report(item, ids):
                continue
            state = snapshot(item, ids, 'geneMarker')
            state['detectedGeneSymbols'] = _detected_gene_symbols([
                {'semanticType': 'healthSummary', 'details': item.get('details')}])
            genes.append(state)
    return dict(aggregateState=aggregate, memberStates=members, geneGroupStates=genes)


def source_state_projections(data, document) -> list[dict]:
    """Project literal, verified C states; never manufacture a review or science.

    These short source paragraphs are separate from the independently reviewed
    scientific explanation.  They neither calculate member grades nor turn a
    normal/zero measurement into a negative detection result.  A changed row,
    date, source label or source identity supplies no projection authority.
    """
    from modules.report.evaluation_synthesis import native_function_source_context
    from modules.report.evaluation_template_input import NUTRITION_PANELS

    plain = lambda value: value.model_dump(mode='json', by_alias=True) if hasattr(value, 'model_dump') else deepcopy(value)
    if hasattr(data, 'model_dump'):
        from modules.report.evaluation_narrative import _assessment_projection
        raw = _assessment_projection(data)
    else:
        raw = plain(data)
    if not isinstance(raw, dict):
        return []
    raw = raw.get('data', raw)
    if not isinstance(raw, dict):
        return []
    source_document = plain(document)
    if not isinstance(source_document, dict):
        return []
    facts = {}
    for item in source_document.get('facts', []):
        if not isinstance(item, dict):
            continue
        try:
            fact = ReportSourceFact.model_validate({key: value for key, value in item.items()
                if key not in {'quoteText', 'useAs'}})
        except (TypeError, ValueError):
            continue
        facts[fact.fact_id] = fact

    exact = lambda value: re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value))) if value is not None else None

    def checksum(row):
        return {match[1].lower() for ref in row.get('sources') or []
            if isinstance(ref, dict) and (match := re.match(
                r'^source:([a-fA-F0-9]{64}):', str(ref.get('recordId') or '')))}

    def verified(row, kind='health'):
        if not isinstance(row, dict) or len(checksum(row)) > 1:
            return None
        ids = []
        refs = []
        for ref in row.get('sources') or []:
            if not isinstance(ref, dict) or ref.get('sourceCode') not in {None, 'C'}:
                continue
            fact = facts.get(ref.get('recordId'))
            if (fact is None or _bound_fact(row, {fact.fact_id: fact}, kind) is None
                    or ref.get('observedAt') is not None and fact.date is not None
                        and ref.get('observedAt') != fact.date
                    or exact(row.get('sourceStatus')) != exact(fact.status)
                    or exact(row.get('referenceRange')) != exact(fact.reference_range)):
                continue
            ids.append(fact.fact_id)
            refs.append(deepcopy(ref))
        if not ids:
            return None
        # Keep the typed physical locator for same-document checks, but it
        # never becomes a fact ID or substitutes for a verified source row.
        refs.extend(deepcopy(ref) for ref in row.get('sources') or [] if isinstance(ref, dict)
            and ref.get('sourceCode') == 'C' and re.match(
                r'^source:[a-fA-F0-9]{64}:', str(ref.get('recordId') or '')))
        return {**deepcopy(row), 'sources': refs, '_stateFactIds': list(dict.fromkeys(ids))}

    def same_source(owner, candidate):
        left, right = checksum(owner), checksum(candidate)
        if left and right and left != right:
            return False
        return any(facts[a].source and facts[a].source == facts[b].source
            and facts[a].date == facts[b].date
            and (bool(left and right) or facts[a].date is not None)
            for a in owner['_stateFactIds'] for b in candidate['_stateFactIds'])

    def literal_status(row):
        status = _value(row.get('sourceStatus'))
        if status is not None:
            return status
        # Only the actual categorical value owns detection semantics.  Zero,
        # false and an absent label retain their literal measurement meaning.
        value = _value(row.get('value'))
        return value if value in {'检出', '未检出', '阴性', '阳性', '正常', '异常'} else None

    def state_text(row):
        name = str(row.get('name') or '').strip()
        status = literal_status(row)
        if not name:
            return None
        if status is not None:
            return f'{name}为{status}。'
        value = _value(row.get('value'))
        if value is not None:
            return f'{name}为{value}{row.get("unit") or ""}。'
        return None

    micro = raw.get('microbiomeAssessment') or {}
    original_items = (raw.get('appendices') or {}).get('sourceItems') or []
    original_health = micro.get('healthMetrics') or []
    health = [value for row in original_health if (value := verified(row)) is not None]
    health_states = {}
    output = []
    for index, row in enumerate(original_health):
        owner = verified(row)
        if owner is None:
            continue
        panel = _HEALTH_MEMBER_PANELS.get(owner.get('code'))
        members = [value for member in micro.get('taxa') or []
            if isinstance(member, dict) and member.get('category') == panel
            and (value := verified(member, 'taxa')) is not None and same_source(owner, value)] if panel else []
        if owner.get('code') == 'scfaProducerBacteria':
            # Native producer panels retain their original member rows in
            # healthMetrics. They are microbial observations, not function
            # aggregates, and must pass the same exact source checks.
            members.extend(value for member in original_health
                if isinstance(member, dict) and str(member.get('code') or '').startswith(
                    'panelResult:scfaProducers:')
                and (value := verified(member, 'taxa')) is not None and same_source(owner, value))
        compatible = [item for item in health if same_source(owner, item)]
        frame = health_state_source_context(owner, compatible, facts, member_rows=members)
        aggregate = frame.get('aggregateState')
        if aggregate is None:
            continue
        text = state_text(owner)
        ids = list(owner['_stateFactIds'])
        own_members = [item for item in members if item.get('_stateFactIds')]
        scoped_owner = deepcopy(owner)
        scoped_names = {'antiInflammatoryCapacity': '菌群抗炎相关评分',
            'gutImmunity': '菌群免疫相关评分', 'intestinalBarrier': '屏障相关菌群指标'}
        if owner.get('code') in scoped_names:
            scoped_owner['name'] = scoped_names[owner['code']]
        if aggregate.get('objectType') == 'geneMarker':
            scoped_owner['_distributionObjectType'] = '原报告基因标记条目'
            scoped_owner['name'] = '“' + str(owner.get('name') or '') + '”'
        distribution = build_source_distribution(own_members or [scoped_owner], facts,
            group_name=owner.get('name'), measurement_kind='microbialHealth')
        if own_members:
            distribution['aggregates'] = [{'name': scoped_owner['name'],
                'sourceLabel': literal_status(owner), 'factIds': list(owner['_stateFactIds'])}]
            distribution['contextFactIds'] = list(dict.fromkeys([
                *distribution['contextFactIds'], *owner['_stateFactIds']]))
            distribution['factIds'] = list(dict.fromkeys([
                *distribution['factIds'], *owner['_stateFactIds']]))
        text = render_source_distribution(distribution, include_minimum=False)
        ids = distribution['factIds']
        if aggregate.get('objectType') == 'geneMarker':
            label = '原报告基因标记条目“' + str(owner.get('name') or '') + '”'
            text = f'{label}标记为{literal_status(owner)}。' if literal_status(owner) is not None else text
        if owner.get('code') == 'virulenceGenes':
            gene_rows = [value for item in original_items if isinstance(item, dict)
                and item.get('category') == 'virulence'
                and (value := verified(item, 'function')) is not None
                and not set(value['_stateFactIds']).intersection(owner['_stateFactIds'])
                and same_source(owner, value) and literal_status(value) in {'检出', '未检出', '阳性', '阴性'}]
            if gene_rows:
                gene_rows = [{**item, '_distributionObjectType': '原报告基因标记条目'} for item in gene_rows]
                distribution = build_source_distribution(gene_rows, facts,
                    group_name=owner.get('name'), measurement_kind='geneMarker')
                distribution['aggregates'] = [{'name': '基因标记条目汇总',
                    'sourceLabel': literal_status(owner), 'factIds': list(owner['_stateFactIds'])}]
                distribution['contextFactIds'] = list(dict.fromkeys([
                    *distribution['contextFactIds'], *owner['_stateFactIds']]))
                distribution['factIds'] = list(dict.fromkeys([
                    *distribution['factIds'], *owner['_stateFactIds']]))
                text = render_source_distribution(distribution, include_minimum=False)
                ids = distribution['factIds']
        if text:
            projection = {'path': ('microbiomeAssessment', 'healthMetrics', index, 'explanation'),
                'text': text, 'factIds': list(dict.fromkeys(ids)), 'distribution': distribution}
            output.append(projection)
            health_states[owner.get('code')] = (owner, projection)

    native_raw = deepcopy(raw)
    source_items = [value for row in original_items
        if isinstance(row, dict)
        and (value := verified(row, 'taxa' if row.get('kind') == 'microbe' else 'function')) is not None]
    native_raw.setdefault('appendices', {})['sourceItems'] = source_items
    groups = native_function_source_context(native_raw, facts)
    def project(path, rows, caption, kind='function', *, aggregate_context=()):
        supplied = [row for row in rows or [] if isinstance(row, dict)
            and _value(row.get('value')) is not None]
        measured = []
        for row in supplied:
            item = verified(row, kind) or deepcopy(row)
            ids = set(item.get('_stateFactIds') or ())
            source_categories = {original.get('category') for original in source_items
                if ids.intersection(original.get('_stateFactIds') or ())}
            if 'antimicrobialResistance' in source_categories or kind == 'resistance':
                item['_distributionObjectType'] = '耐药基因检测'
            elif 'virulence' in source_categories:
                item['_distributionObjectType'] = '原报告基因标记条目'
            else:
                item['_distributionObjectType'] = '菌群功能测量'
            measured.append(item)
        frame = build_source_distribution(measured, facts,
            native_context=aggregate_context, group_name=caption, measurement_kind=kind)
        text = render_source_distribution(frame)
        if text:
            result = {'path': tuple(path), 'text': text,
                'factIds': frame['factIds'], 'distribution': frame}
            output.append(result)
            return result
        return None

    # Enumerate actual measured groups rather than requiring a particular
    # native panel or complete expected template. Each member carries its own
    # binding, label, grade and direction; one unresolved member cannot erase
    # the other usable observations.
    for index, pathway in enumerate((raw.get('functionalPathways') or {}).get('pathways') or []):
        rows = pathway.get('relatedMetrics') or []
        project(('functionalPathways', 'pathways', index, 'healthMeaning'), rows,
            pathway.get('name'), aggregate_context=groups)

    scfa = micro.get('scfaInterpretation')
    if isinstance(scfa, dict) and 'text' in scfa:
        project(('microbiomeAssessment', 'scfaInterpretation', 'text'),
            micro.get('shortChainFattyAcids'), '菌群短链脂肪酸相关功能评分',
            aggregate_context=groups)

    # Structure rows are measurements of different objects, not a common
    # score scale. Their complete state is preserved without a cross-object
    # minimum or a conversion from richness to total bacterial quantity.
    structure = micro.get('overallStructure') or []
    structure_rows = []
    names = {'speciesCount': '菌种丰富度', 'diversity': '菌群多样性', 'beRatio': '菌群B/E比值',
        'gutAge': '菌群年龄模型估计值', 'barrierRelatedBacteria': '屏障相关菌群指标',
        'developmentalMaturity': '菌群发育成熟度指标', 'childGrowth': '生长相关菌群指标'}
    for index, row in enumerate(structure):
        if not isinstance(row, dict):
            continue
        scoped = {**row, 'name': names.get(row.get('code'), row.get('name'))}
        bound = verified(row, 'structure')
        if bound is not None:
            scoped['_stateFactIds'] = bound['_stateFactIds']
            # The renamed subject is a finite measurement type, not a new
            # alias used for source binding.
            structure_rows.append(scoped)
            project_path = ('microbiomeAssessment', 'overallStructure', index, 'explanation')
            frame = build_source_distribution([scoped], facts, measurement_kind='structure')
            text = render_source_distribution(frame, include_minimum=False)
            if text:
                output.append({'path': project_path, 'text': text,
                    'factIds': frame['factIds'], 'distribution': frame})
    if isinstance(micro.get('structureInterpretation'), dict):
        frame = build_source_distribution(structure_rows, facts,
            group_name='菌群结构测量', measurement_kind='structure')
        text = render_source_distribution(frame, include_minimum=False)
        if text:
            output.append({'path': ('microbiomeAssessment', 'structureInterpretation', 'text'),
                'text': text, 'factIds': frame['factIds'], 'distribution': frame})

    for index, row in enumerate(micro.get('antimicrobialResistance') or []):
        project(('microbiomeAssessment', 'antimicrobialResistance', index, 'explanation'),
            [row], row.get('name'), 'resistance')

    nutrition = raw.get('nutritionAssessment') or {}
    for field in ('detailedMetabolism', 'vitaminsAndMinerals'):
        for index, group in enumerate(nutrition.get(field) or []):
            if not isinstance(group, dict) or not isinstance(group.get('explanation'), dict):
                continue
            project(('nutritionAssessment', field, index, 'explanation', 'text'),
                group.get('items'), group.get('name'), aggregate_context=groups)
    for rows_key, text_key, caption in (
            ('neurotransmitters', 'neurotransmitterInterpretation', '菌群神经递质相关功能评分'),
            ('aminoAcidsAndMetabolites', 'aminoMetaboliteInterpretation', '菌群氨基酸相关功能评分')):
        if isinstance(nutrition.get(text_key), dict):
            project(('nutritionAssessment', text_key, 'text'), nutrition.get(rows_key),
                caption, aggregate_context=groups)

    return output


_SOURCE_STATE_FIELDS = ('code', 'name', 'scientificName', 'value', 'unit', 'referenceRange',
    'sourceStatus', 'assessmentLevel', 'sources', 'kind', 'category', 'sourceType',
    'sourceName', 'observedAt', 'description', 'details', '_assessmentOrigin')


def _source_state_input_signature(data, document):
    """Bind authority to this live typed input, excluding only authored prose."""
    from .evaluation_schema import EvaluationData
    from .schemas import ClinicalReportDocument
    if not isinstance(data, EvaluationData) or not isinstance(document, ClinicalReportDocument):
        return None
    from .evaluation_narrative import _assessment_projection
    raw = _assessment_projection(data)
    observations = []

    def visit(value, path=()):
        if isinstance(value, dict):
            if 'value' in value and ('sources' in value or 'code' in value):
                observations.append((path, {key: value.get(key) for key in _SOURCE_STATE_FIELDS
                    if key in value}))
            if any(key in value for key in ('items', 'relatedMetrics')):
                observations.append((path, {key: value.get(key) for key in ('code', 'name')}))
            for key, child in value.items():
                if key not in {'_clinicalProblemFrames'}:
                    visit(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, index))
    visit(raw)
    facts = [fact.model_dump(mode='json', by_alias=True) for fact in document.facts]
    return hashlib.sha256(json.dumps({'observations': observations, 'facts': facts},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_state_authorities(data, document) -> tuple[_SourceStateAuthority, ...]:
    """Export current verified states; no serialized flag supplies authority."""
    signature = _source_state_input_signature(data, document)
    if signature is None:
        return ()
    known = {fact.fact_id for fact in document.facts}
    result = []
    for state in source_state_projections(data, document):
        if (not state.get('text') or not state.get('factIds')
                or not set(state['factIds']) <= known or not state.get('distribution')):
            continue
        result.append(_mint_source_state_authority(path=state['path'], text=state['text'],
            fact_ids=state['factIds'], distribution=state['distribution'],
            input_signature=signature, data=data, document=document))
    return tuple(result)


def matching_source_state_authorities(authorities, data, document):
    """Check one live source signature once for a batch of typed exports."""
    signature = _source_state_input_signature(data, document)
    if signature is None:
        return {}
    return {authority.path: authority for authority in authorities
        if _source_state_sealed(authority) and authority.input_signature == signature
        and authority.data_identity == id(data) and authority.document_identity == id(document)}


def source_state_authority_matches(authority, data, document):
    return bool(matching_source_state_authorities((authority,), data, document))


_CORE_MEANINGS = {
    "gmwi2": "GMWI2是根据菌群组成计算的菌群健康指数。",
    "gmhi": "GMHI是根据菌群组成计算的菌群健康指数。",
    "gutHealthIndex": "肠菌健康指数是描述菌群整体健康特征的综合指标。",
    "gutAge": "肠道年龄是根据菌群特征估计的年龄相关指标。",
    "microbiomeHealthScore": "菌群健康度评分描述菌群的整体健康特征。",
    "speciesCount": "菌种数量描述样本的菌种丰富度。",
    "diversity": "菌群多样性描述菌群种类及其分布的多样程度。",
    "beRatio": "B/E比值为双歧杆菌属丰度与肠杆菌科丰度之比。",
    "fbRatio": "F/B比值为厚壁菌门丰度与拟杆菌门丰度之比。",
    "beBalanceScore": "B/E相关菌群平衡评分描述双歧杆菌属与肠杆菌科的组成状态。",
    "fbEnergyMetabolismScore": "F/B相关评分描述厚壁菌门与拟杆菌门的组成状态。",
    "enterotype": "肠型是按菌群组成特征划分的结构类型。",
}

# Finite definitions of the named substance, cell, structure, or examination.
# These sources describe terms, not the patient's specimen or current state:
# https://medlineplus.gov/lab-tests/uric-acid-test/
# https://medlineplus.gov/lab-tests/creatinine-test/
# https://medlineplus.gov/lab-tests/bun-blood-urea-nitrogen/
# https://medlineplus.gov/lab-tests/estimated-glomerular-filtration-rate-egfr-and-gfr-tests/
# https://medlineplus.gov/lab-tests/ketones-in-urine/
# https://medlineplus.gov/lab-tests/bilirubin-in-urine/
# https://medlineplus.gov/lab-tests/urobilinogen-in-urine/
# https://medlineplus.gov/lab-tests/epithelial-cells-in-urine/
# https://medlineplus.gov/lab-tests/mucus-in-urine/
# https://medlineplus.gov/ency/article/003586.htm
# https://medlineplus.gov/abscess.html
# https://medlineplus.gov/ency/article/002274.htm
# https://medlineplus.gov/ctscans.html
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/red-blood-cell
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/white-blood-cell
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/phagocyte
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/yeast
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/bacteria
# https://medlineplus.gov/lab-tests/cea-test/
# https://medlineplus.gov/lab-tests/ca-19-9-blood-test-pancreatic-cancer/
# https://www.cdc.gov/dpdx/ascariasis/index.html
# https://www.cdc.gov/dpdx/hookworm/index.html
# https://www.cdc.gov/dpdx/trichuriasis/index.html
# https://www.nidcd.nih.gov/health/how-do-we-hear
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/nasal-cavity
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/tonsil
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/pharynx
# Pharyngitis = throat inflammation; chronic qualifies the duration only:
# https://medlineplus.gov/medwords/Medical_Words_Reference.pdf (p11)
# https://www.cancer.gov/publications/dictionaries/cancer-terms/def/chronic-disease
_CLINICAL_MEANINGS = {
    "身高": "身高是身体高度的测量记录。", "体重": "体重是身体重量的测量记录。",
    "体重指数": "体重指数是体重相对于身高的指标，属于筛查信息。",
    "红细胞": "红细胞是含血红蛋白、可携带氧的细胞；该项记录送检标本中的红细胞相关结果。",
    "红细胞总数": "红细胞是含血红蛋白、可携带氧的细胞；该项记录送检标本中的红细胞数量。",
    "红细胞计数": "红细胞是含血红蛋白、可携带氧的细胞；该项记录送检标本中的红细胞数量。",
    "白细胞": "白细胞是参与免疫反应的一类细胞；该项记录送检标本中的白细胞相关结果。",
    "白细胞总数": "白细胞是参与免疫反应的一类细胞；该项记录送检标本中的白细胞数量。",
    "白细胞计数": "白细胞是参与免疫反应的一类细胞；该项记录送检标本中的白细胞数量。",
    "比重": "比重是标本密度与水密度的比值，用于描述标本的物理性质。",
    "隐血": "隐血检测用于寻找肉眼难以察觉的血红蛋白等相关成分信号。",
    "隐血试验": "隐血检测用于寻找肉眼难以察觉的血红蛋白等相关成分信号。",
    "PH值": "该项记录送检标本的酸碱度。", "pH": "该项记录送检标本的酸碱度。",
    "浊度": "该项记录送检标本的浑浊程度。", "颜色": "该项记录送检标本的外观颜色。",
    "性状": "性状指送检标本的外观、形态和稠度等性质。",
    "蛋白质": "蛋白质是由氨基酸连接而成的一类物质；该项记录送检标本中的蛋白质检测结果。",
    "葡萄糖": "葡萄糖是一种单糖，即结构较简单的糖；该项记录送检标本中的葡萄糖检测结果。",
    "酮体": "酮体是脂肪分解代谢过程中形成的一组小分子物质。",
    "胆红素": "胆红素是红细胞分解过程中形成的一种黄色物质。",
    "尿胆原": "尿胆原是胆红素分解转化形成的一种物质。",
    "亚硝酸盐": "亚硝酸盐是一类含亚硝酸根的化合物；该项记录送检标本中的相关检测结果。",
    "肌酐": "肌酐是肌肉代谢过程中形成的一种代谢产物。",
    "尿素": "尿素是蛋白质分解后含氮物质转化形成的一种代谢产物。",
    "尿酸": "尿酸是嘌呤分解后形成的一种代谢产物；嘌呤是细胞等所含的一类化学成分。",
    "肾小球滤过率": "肾小球是肾脏中的微小过滤结构；肾小球滤过率描述其单位时间内滤出的液体量，本项是来源按其方法提供的估计结果。",
    "癌胚抗原": "癌胚抗原（CEA）是一种蛋白质，可用作肿瘤标志物。",
    "糖链抗原CA19-9": "糖链抗原CA19-9是一种可用作肿瘤标志物的物质。",
    "酵母菌": "酵母菌是一类真菌；该项记录送检标本中的相关检出或观察结果。",
    "非鳞状上皮细胞": "上皮细胞是覆盖身体内外表面的细胞；非鳞状上皮细胞指未归入扁平鳞状形态一类的上皮细胞。",
    "鳞状上皮细胞": "鳞状上皮细胞是一类呈扁平形态的上皮细胞，上皮细胞覆盖身体内外表面。",
    "脓细胞": "脓细胞是镜检中对脓液相关白细胞的称呼，包含发生变性或破坏的白细胞。",
    "透明管型": "透明管型是主要由蛋白质组成、呈透明管状的微小结构。",
    "病理管型": "管型是可含蛋白质或细胞等成分的微小管状结构；病理管型是来源的合并分类，具体类型以原报告为准。",
    "尿酸结晶": "尿酸结晶是尿酸形成的固体晶体。",
    "草酸钙结晶": "草酸钙结晶是草酸与钙结合形成的固体晶体。",
    "粘液丝": "粘液丝是黏液呈丝状时的形态，黏液是一种黏稠的分泌物。",
    "细菌": "细菌是一大类单细胞微生物；该项记录送检标本中的相关检出或观察结果。",
    "蛔虫卵": "蛔虫卵是蛔虫产生的虫卵，蛔虫是一类寄生性线虫。",
    "钩虫卵": "钩虫卵是钩虫产生的虫卵，钩虫是一类寄生性线虫。",
    "鞭虫卵": "鞭虫卵是鞭虫产生的虫卵，鞭虫是一类寄生性线虫。",
    "吞噬细胞": "吞噬细胞是能够包围并吞入微生物、异物或死亡细胞的一类免疫细胞。",
    "油滴": "油滴指镜检中呈小滴状的脂肪类物质。",
    "不消化食物": "“不消化食物”指标本中仍可辨认的、未充分分解的食物残渣。",
    "白细胞酯酶": "白细胞酯酶是白细胞所含的一类酶；酶是可促进特定化学反应的物质。",
    "肺部听诊": "肺部听诊是用听诊器听取呼吸相关声音的体格检查。",
    "肝脏触诊": "肝脏触诊是用手触摸肝脏所在区域、检查可触及情况等的体格检查。",
    "肾脏叩诊": "肾脏叩诊是叩击肾脏所在体表区域、观察相关反应的体格检查。",
    "神经反射": "神经反射指神经系统对刺激产生的自动反应；该项记录相关体格检查所见。",
    "裸眼视力": "裸眼视力是在不使用眼镜或隐形眼镜矫正时辨认细节的能力，眼别和测量条件以原记录为准。",
    "外耳": "外耳是耳的外部部分，包括耳廓和外耳道；该项记录相关检查所见。",
    "外耳道": "外耳道是从外耳开口通向鼓膜的通道；该项记录相关检查所见。",
    "鼓膜": "鼓膜是外耳道深处的一层薄膜，可随声音振动；该项记录相关检查所见。",
    "鼻腔": "鼻腔是鼻内部的空腔；该项记录相关检查所见。",
    "鼻中隔": "鼻中隔是把鼻腔分为左右两侧的组织；该项记录相关检查所见。",
    "扁桃体": "扁桃体是咽部的淋巴组织；该项记录相关检查所见。",
    "咽": "咽是鼻腔和口腔后方的通道；该项记录相关检查所见。",
    "慢性咽炎": "慢性咽炎指咽部的慢性炎症。",
    "左膝关节正侧位": "正侧位指从正面和侧面获取同一部位的影像；本项部位为来源标明的左膝关节。",
    "CT颈椎椎体平扫＋三维重建": "CT利用X射线和计算机形成断层影像；三维重建是把断层影像重新组合为空间图像，本项部位为来源标明的颈椎。",
    "CT全腹平扫+增强": "CT利用X射线和计算机形成断层影像；平扫与增强是使用对比剂前及使用后获取影像的检查方式，本项部位为来源标明的腹部。",
}


_CLINICAL_HISTORICAL_LIMIT = "仅代表来源记录当时的结果，须结合原始检查语境理解，不能据单项判定当前状态或病因。"

_AMBIGUOUS_CLINICAL_TERMS = frozenset({"白细胞脂酶", "夏雷登氏"})
_AMBIGUOUS_DEFINITION_TARGETS = {
    "白细胞脂酶": (r"白细胞酯酶|leukocyte\s+esterase|白细胞(?:所含|含有|产生|分泌|中.{0,4}的).{0,8}酶",),
    "夏雷登氏": (r"结晶|晶体|嗜酸性|Charcot(?:[\s-]*Leyden)?|夏科", r"寄生虫|虫体|虫卵|原虫"),
}
_COVID_RNA_MEANING = "该项检测送检样本中的新型冠状病毒核糖核酸（RNA），RNA是该病毒的遗传物质。"
_CLINICAL_EVENT_CATEGORIES = {
    "体格检查": r"体格检查|查体",
    "血液": r"血液|血常规|血检|血标本|血样|血清|血浆",
    "尿液": r"尿液|尿常规|尿检|尿标本|尿样",
    "生化": r"生化(?:检查|检验|检测|指标|项目)?",
    "粪便": r"粪便|粪检|便常规|大便检查",
}


def _unsupported_ambiguous_categories(text: str, fact: ReportSourceFact) -> list[str]:
    if fact.kind != "clinical" or fact.name not in _AMBIGUOUS_CLINICAL_TERMS:
        return []
    supplied = " ".join((fact.name, fact.value, fact.source))
    failures = []
    for category, pattern in _CLINICAL_EVENT_CATEGORIES.items():
        if re.search(pattern, supplied):
            continue
        for match in re.finditer(pattern, text):
            # Negation/uncertainty applies only to the clause containing the
            # category. A later 'cannot diagnose' does not undo an earlier
            # affirmative 'is a physical examination'.
            prefix = re.split(r"[，,。；;！？\n]", text[:match.start()])[-1]
            suffix = text[match.end():]
            prefix = re.split(r"但是|然而|不过|而是|但|却", prefix)[-1]
            if re.search(r"是否|不是|并非|不属于|(?:不能|无法|不宜|不应|未能|无证据).{0,16}(?:确定|判断|明确|证实|认定|归|属|为|是)|(?:未明确|未确定|不确定|不清楚).{0,12}(?:是|为|属于|归)", prefix):
                continue
            suffix_clause = re.split(r"[，,。；;！？\n]", suffix)[0]
            if (re.match(r"(?:还是|抑或|吗|么|？|\?)", suffix)
                    or re.search(r"(?:尚|仍)?(?:有待|待|需|须).{0,5}(?:核实|核对|确认)", suffix_clause)):
                continue
            if (re.search(r"属于|归为|归入|来自|取自|是|为|作为|在|中的?|可能|或许|也许|疑似|似乎", prefix)
                    or re.match(r"(?:信息|记录|所见|结果|项目|检查|检验|检测|标本|样本|中)", suffix)):
                failures.append(category)
                break
    return failures


def _unsupported_ambiguous_definitions(text: str, fact: ReportSourceFact) -> bool:
    if fact.kind != "clinical" or fact.name not in _AMBIGUOUS_CLINICAL_TERMS:
        return False
    supplied = " ".join((fact.name, fact.value, fact.source))
    def limited(prefix, suffix):
        return bool(re.search(r"是否|并非|不是|不属于|不等同|不一定|(?:不能|无法|不应|未能|无证据).{0,20}(?:认定|确定|判断|说明|证明|称|等同|是|为)|未(?:证实|确认|确定|明确)", prefix)
            or re.match(r"(?:还是|抑或|吗|么|？|\?)", suffix)
            or re.search(r"(?:有待|待|需|须).{0,5}(?:核实|核对|确认)", suffix))
    for target in _AMBIGUOUS_DEFINITION_TARGETS[fact.name]:
        # Support for one possible meaning does not establish a different one.
        supplied_support = False
        for match in re.finditer(target, supplied, re.I):
            prefix = re.split(r"[，,。；;！？\n]", supplied[:match.start()])[-1]
            suffix = re.split(r"[，,。；;！？\n]", supplied[match.end():])[0]
            if not limited(prefix, suffix) and not re.search(r"可能|疑似|或许|也许|不确定", prefix):
                supplied_support = True
        if supplied_support:
            continue
        for match in re.finditer(target, text, re.I):
            prefix = re.split(r"[，,。；;！？\n]", text[:match.start()])[-1]
            prefix = re.split(r"但是|然而|不过|而是|但|却", prefix)[-1]
            suffix = re.split(r"[，,。；;！？\n]", text[match.end():])[0]
            refers_back = re.search(r"该(?:类|种)?$", prefix) and not re.search(r"另(?:一个|一项)|其他", prefix)
            if fact.name not in prefix and not refers_back and not re.search(
                    r"(?:该|这|此)(?:项|词|原词|术语|记录|项目)|它", prefix):
                continue
            if limited(prefix, suffix):
                continue
            # A negative observation still asserts the identity of its object:
            # "did not see this parasite" does not mean its category is unknown.
            if re.search(r"就是|又称|也叫|亦称|等同|指的是|是指|可理解为|属于|是|为|指|即|未(?:查见|见|检出|发现)", prefix):
                return True
    return False


def ambiguous_clinical_meaning_failures(text: str, facts) -> list[str]:
    """Reject unsupported categories or guessed definitions of ambiguous terms."""
    if not isinstance(text, str) or not facts:
        return []
    bound = list(facts.values() if isinstance(facts, dict) else facts)
    clinical_names = sorted({f.name for f in bound if f.kind == "clinical" and f.name}, key=len, reverse=True)
    failures = []
    for fact in bound:
        if fact.kind != "clinical" or fact.name not in _AMBIGUOUS_CLINICAL_TERMS:
            continue
        for sentence in re.split(r"[。！？\n]", text):
            if fact.name not in sentence and len(bound) != 1:
                continue
            scopes = [sentence]
            if len(clinical_names) > 1:
                subjects = list(re.finditer("|".join(re.escape(name) for name in clinical_names), sentence))
                scopes = [sentence[match.start() if index else 0:
                    subjects[index + 1].start() if index + 1 < len(subjects) else len(sentence)]
                    for index, match in enumerate(subjects) if match.group() == fact.name]
            for scope in scopes:
                failures.extend(f"{fact.name}的{category}分类缺少同源事实支持"
                    for category in _unsupported_ambiguous_categories(scope, fact))
                if _unsupported_ambiguous_definitions(scope, fact):
                    failures.append(f"{fact.name}的释义或同义改名缺少同源事实支持")
    return list(dict.fromkeys(failures))


def _clinical_term(name: str) -> str:
    base = re.split(r"[（(]", name)[0].strip()
    if base.startswith("尿") and base[1:] in {"红细胞", "红细胞总数", "比重"}:
        base = base[1:]
    return base


def _explicit_covid_rna(name: str) -> bool:
    # A full, explicit viral target is required. A bare PCR/RNA/NAAT or an
    # ambiguous abbreviation must not be expanded into a named infection.
    return bool(re.search(r"新型冠状病毒(?:核糖核酸|核酸)|SARS[\s-]*CoV[\s-]*2.*(?:RNA|核糖核酸|核酸)", name, re.I)
        and not re.search(r"抗体|抗原|antibod|antigen|\bIg[MG]\b", name, re.I))


def _clinical_definition(name: str, value=None) -> tuple[str, str | None]:
    if _explicit_covid_rna(name):
        return "covidRNA", _COVID_RNA_MEANING
    base = _clinical_term(name)
    # Only the explicitly supplied source result unlocks a result-term meaning.
    if base == "咽" and _value(value) == "慢性咽炎":
        base = "慢性咽炎"
    return base, _CLINICAL_MEANINGS.get(base)


def _complete_clinical_text(row: dict, existing: str, fact: ReportSourceFact) -> str:
    name = row.get("name") or row.get("scientificName")
    if _clinical_term(name) in _AMBIGUOUS_CLINICAL_TERMS:
        # Remove the unsupported category clause only; preserve usable source
        # result/limitation clauses, while the actual value stays in its field.
        parts = re.split(r"([，,。；;！？\n])", existing)
        text = ""
        removed = False
        for index in range(0, len(parts), 2):
            clause = parts[index]
            if (_unsupported_ambiguous_categories(clause, fact)
                    or _unsupported_ambiguous_definitions(clause, fact)):
                removed = True
                continue
            text += clause + (parts[index + 1] if index + 1 < len(parts) else "")
        if removed:
            text = re.sub(r"[，,；;]+\s*$", "。", text)
        if name not in text or not re.search(r"(?:原词|术语|含义|项目).{0,16}(?:核对|待核|不明|未明|不明确)|(?:核对|待核).{0,16}(?:原词|术语|含义|项目)", text):
            text += ("\n" if text else "") + f"该项保留来源原词“{name}”，具体术语含义需核对原项目说明。"
        return text
    # Nonempty source-bound explanations already carry their useful meaning.
    # Do not append a second dictionary definition after a patient assessment.
    return existing


def _clinical_meaning(name: str, value=None) -> str | None:
    base, definition = _clinical_definition(name, value)
    meaning = definition
    if meaning is None:
        if base in _AMBIGUOUS_CLINICAL_TERMS:
            meaning = f"该项保留来源原词“{name}”，具体术语含义需核对原项目说明。"
    return meaning


def _meaning(row: dict, kind: str, group: str | None = None) -> str | None:
    name = row.get("name") or row.get("scientificName")
    identity = _identity(name)
    # This is a finite glossary, not fuzzy clinical inference. Preserve the
    # existing context/limits and add only a known term's basic definition.
    definition = None
    if kind == "disease" and (pair := _disease_definition(row)) is not None:
        definition = pair[0]
    if kind not in {"clinical", "disease", "taxa", "phylum", "resistance"}:
        definition = (_TERM_MEANINGS.get(_key(name))
            or _SCFA_MEANINGS.get(identity) or _NEURO_MEANINGS.get(identity)
            or _CORE_TERM_MEANINGS.get(identity))
        if definition is None and identity in {"carbohydrate", "fat", "protein", "minerals"}:
            definition = _TERM_MEANINGS.get(_key(dict(catalog.MAJOR_METABOLISM_DEFINITIONS)[identity]))
    # A finite term definition adds information. Repeating a source-type label
    # and a generic clinical limitation for every row does not.
    return definition or _context_meaning(row, kind, group)


def _context_meaning(row: dict, kind: str, group: str | None = None) -> str | None:
    name = row.get("name") or row.get("scientificName")
    identity = _identity(name)
    if kind == "clinical":
        return _clinical_meaning(name, row.get("value"))
    if kind == "resistance":
        return f"{name}条目记录相应类别的耐药基因检测结果。"
    if identity in _CORE_MEANINGS:
        return _CORE_MEANINGS[identity]
    return None


_TAXON_DETAIL_LIMIT = "检出、未检出或丰度均不能单独确定感染或对本人的具体功能。"


_PROSE_KEYS = frozenset({"text", "explanation", "interpretation", "healthMeaning",
    "summary", "description", "clinicalCorrelation", "basis", "attentionBasis",
    "followupAction", "association", "handling"})
_SPECIFIC_CONTEXT = re.compile(r"未注明|未明确|待核|需核对|冲突|矛盾|疑似|可能大")
_SHARED_LIMIT = re.compile(
    r"不等同|不代表|不构成|不能(?:据此|单独|直接|由该条目|等同|替代|互相验证|称为相互验证)|不是(?:脑内|本人|人体|血液)|不作为.{0,12}(?:用药|诊断)")
_LIMIT_TOPIC = re.compile(r"人体|本人|营养|血液|血清|脑内|疾病|临床|药敏|摄入|补充|药物|抗生素|生理|诊断|机制|病因|当前状态|相互验证")
_GENERIC_SUBJECT = re.compile(r"^(?:这(?:些|类|一|两项|两类)?|该(?:项|功能|模型|评估|检测|分值|记录|标记|条目|结果)|本段|功能估计|分值本身|数值|菌群模型|模型评分|来源(?:模型|分值)|偏低项目|不等同|不代表|不构成|不能据此)")


def _body_boundary_sentence(sentence: str) -> bool:
    """A repeated method boundary is not a patient assessment sentence.

    Drop the whole sentence, never just a qualifier on a medical proposition.
    Concrete specimen gaps, contradictory records and literal source quotes
    remain available in their dedicated source fields and consistency note.
    """
    if re.search(r'不能把[^。；;！？\n]{0,25}(?:合成|评分|分档|档位)[^。；;！？\n]{0,25}'
            r'(?:当作|视为|等同)[^。；;！？\n]{0,12}(?:健康|优势)|'
            r'(?:评分|分档|档位)[^。；;！？\n]{0,20}(?:来源方向|原始方向|来源标记)'
            r'[^。；;！？\n]{0,20}(?:不一致|不相同|冲突|矛盾)', sentence):
        return True
    if (re.search(r'评分|分档|档位|原始方向|来源方向', sentence)
            and re.search(r'分别看待|分开看待|分开理解|区别对待|不能因[^。；;！？\n]{0,20}忽略', sentence)):
        return True
    if re.search(r'^(?:来源|原始?|既往)(?:检查)?记录为(?!.*(?:菌群功能|来源模型|功能估计))', sentence):
        return False
    if re.search(r'标本(?:类型)?(?:待|未|不明)|冲突|矛盾|结果.{0,12}(?:标记|状态).{0,8}不一致|原检查记录[：:]|原资料建议[：:]', sentence):
        return False
    if re.search(r'(?:尚|仍)?未(?:曾|经|经过)?(?:医生|医师|临床|医学|医疗)?(?:核验|核实)|'
            r'(?:尚|仍)?未(?:曾|经|经过|获(?:得)?|得到|通过)?(?:临床|医学|医疗|医生|医师)'
            r'(?:诊断|检查|评估)?(?:确认|证实|核定)|'
            r'(?:并非|不是)[^，,。；;！？\n]{0,12}(?:相互|互相)印证', sentence):
        return True
    if re.search(r'(?:避免|防止|切勿|不要)(?:把|将)[^。；;！？\n]{0,45}(?:误读|误认|读作)|'
            r'(?:需|应|要)[^。；;！？\n]{0,12}同时保留[^。；;！？\n]{0,20}(?:两层信息|两种信息)', sentence):
        return True
    if (re.search(r'评分|模型|指数|本报告|本次评估', sentence)
            and re.search(r'(?:不因|不凭|不以|不能|不宜|不应)[^。；;！？\n]{0,30}'
                r'(?:推导|推断|引申|生成)[^。；;！？\n]{0,24}(?:药物|补剂|补充剂|检查|方案)', sentence)):
        return True
    # Compare measurement meaning with an actual human amount/outcome, not
    # ordinary biological subjects such as microbial versus host synthesis.
    measurement_boundary = re.search(r'(?:而非|不同于|并非|并不是|并不代表)[^，,。；;！？\n]{0,35}(?:'
        r'(?:人体|本人|体内|血液|血清|脑内)[^，,。；;！？\n]{0,24}'
        r'(?:实际(?:暴露|含量|浓度|水平)|暴露水平|血药浓度|实测含量|浓度|含量|水平)|'
        r'临床(?:结局|实测结果)|膳食摄入)', sentence)
    boundary_text = re.sub(r'(?:暂)?不构成(?:本次|当前)?(?:具体|候选|优先)?(?:关注方向|关注因素|关注线索|重点诱因)', '', sentence)
    boundary = re.search(r'不等同|(?:不能|不应|不宜)[^，,。；;！？\n]{0,8}等同(?:于)?|不(?:直接)?代表|不构成|不是本轮问卷|不能(?:据此|直接确诊|相互|互相)|'
        r'(?:不能|不应|不宜|不可)[^，,。；;！？\n]{0,10}(?:视为|视作|看作|当作|解释为|理解为|推定为|认定为|代表)|'
        r'不能理解为|不应仅凭|不表示|不能.{0,8}(?:替代|印证)|不据此(?:自行|直接|单独)?(?:推断|判断|诊断|处理)|'
        r'不是(?:本人|个人).{0,8}(?:患病|已患该病|患有|诊断)|不能说明(?:个体|本人|本例)|不能相互替代|'
        r'(?:不能|无法|不宜|不应)(?:直接|据此|单独|简单)?(?:说明|证明|推知|得出|外推)'
        r'[^，,。；;！？\n]{0,25}(?:本人|本例|该患者|您)|'
        r'需要把.{0,45}(?:分开看待|分开理解)|不同.{0,12}不能相互|而非据此推断(?:已)?发生', boundary_text)
    return bool((boundary or measurement_boundary) and re.search(r'菌群|来源|模型|检测|研究|本例|本人|个体|患者|人群|人体|体内|代谢物|健康|营养|功能|评分|分档|临床|血液|摄入|问卷|病史|症状|确诊|疾病', sentence))


def _prose_sentence_units(text: str) -> list[str]:
    """Keep embedded source quotations whole during sentence-level cleanup.

    Questions, full stops and line breaks inside a quotation are source
    content. Splitting/deduplicating those fragments can remove a closing
    quote or an answer shared by two different source assertions.
    """
    units, closing = [], []
    start = 0
    pairs = {'“': '”', '‘': '’'}
    for index, character in enumerate(text):
        if character in pairs:
            closing.append(pairs[character])
        elif character == '"':
            if closing and closing[-1] == '"':
                closing.pop()
            else:
                closing.append('"')
        elif closing and character == closing[-1]:
            closing.pop()
        if character in '。！？\n' and not closing:
            if text[start:index + 1].strip():
                units.append(text[start:index + 1])
            start = index + 1
    if text[start:].strip():
        units.append(text[start:])
    return units


def _readable_text(text: str, *, path=(), facts=()) -> str | None:
    """Retire shared templates, never a negation of an actual source finding.

    Restrict this to generated prose. Original values/statuses, bibliographies
    and introduction declarations never enter here. Generated prose can quote
    a source record; that complete quotation is one indivisible text unit.
    Removing whole generic sentences avoids turning a qualified proposition
    into an unqualified medical claim.
    """
    original = text
    text = text.replace(_CLINICAL_HISTORICAL_LIMIT, "")
    # These clauses already identify the concrete information gap. Express it
    # directly without expanding the record into specimen-specific reasoning.
    text = re.sub(r'[，,](?:但)?记录未注明标本类型[，,](?:因此)?不能据此[^。！？\n]*',
        '，标本类型待核实', text)
    text = text.replace('，这属于当时体检所记录的结论，不等同于本次新确诊', '')
    if re.search(r'既往|历史', text):
        text = re.sub(r'[，,]本次仅保留该历史记录[，,]不能据此推断当前[^。！？\n]*', '', text)
    for suffix in ("；单项结果不能独立确诊或排除肿瘤。", "，不能单独判断个人整体健康状态。",
            "，其单项结果不能独立确诊或排除肿瘤，需要结合临床语境理解。"):
        text = text.replace(suffix, "。")
    kept = []
    pieces = _prose_sentence_units(text)
    for piece in pieces:
        sentence = piece.strip()
        if not sentence:
            continue
        measurement_scope = measurement_scope_text_allowed(sentence, path=path, facts=facts)
        if _body_boundary_sentence(sentence) and not measurement_scope:
            continue
        if sentence in {"该项为来源功能记录，具体意义依原方法解释。",
                "该项为来源中的功能记录，具体意义依原方法解释。"}:
            continue
        if re.match(r"^该项(?:呈现|为来源中的|展示来源菌群功能分值)", sentence) and re.search(
                r"功能记录|合成或代谢功能|相关结果|营养或代谢项目|功能评估", sentence):
            continue
        if sentence.endswith(_TAXON_DETAIL_LIMIT) and "是来源菌群检测列示的分类单元" in sentence:
            continue
        if re.match(r'^“[^”]+”条目展示来源菌群疾病风险模型的输出或标记', sentence):
            continue
        if re.match(r'^该报告中的膳食纤维需求是独立来源项目，不能由', sentence):
            continue
        if sentence == "菌群的" + _TAXON_DETAIL_LIMIT:
            continue
        if measurement_scope:
            kept.append(sentence)
            continue
        limited = _SHARED_LIMIT.search(sentence)
        prefix = sentence[:limited.start()] if limited else ""
        # Retain a concrete prior result or a source conflict even if its
        # interpretation contains a caution. It is not a shared disclaimer.
        actual_result = re.search(r"\d|偏低|极低|偏高|升高|降低|阴性|未检出|为正常|均正常", prefix)
        if (limited and _LIMIT_TOPIC.search(sentence) and _GENERIC_SUBJECT.search(sentence)
                and not _SPECIFIC_CONTEXT.search(sentence)
                and (not actual_result or sentence.startswith("偏低项目"))):
            continue
        kept.append(sentence)
    if text == original and len(kept) == len(pieces) and len(set(kept)) == len(kept):
        return original or None
    return "\n".join(dict.fromkeys(kept)) or None


def refine_evaluation_prose(raw: dict, *, facts=None, bound_fact_ids=()) -> None:
    """Clean only public narrative slots; preserve every fact and field name."""
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        return

    known = facts if isinstance(facts, dict) else {fact.fact_id: fact for fact in facts or ()}
    def walk(value, path=()):
        if isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, index))
        elif isinstance(value, dict):
            removed = False
            ids = [source.get('recordId') for source in value.get('sources', [])
                if isinstance(source, dict)] or list(bound_fact_ids)
            linked = [known[fid] for fid in ids if fid in known]
            for key, item in value.items():
                if key in {"sources", "sourceItems", "referenceLiterature", "sourceValues",
                        "indicatorStandards", "riskIndexStandards", "sourceAdvice"}:
                    continue
                if key in _PROSE_KEYS and isinstance(item, str):
                    cleaned = _readable_text(item, path=(*path, key), facts=linked)
                    value[key] = (cleaned or "") if key == "description" else cleaned
                    removed |= bool(item) and value[key] is None
                elif isinstance(item, (dict, list)):
                    walk(item, (*path, key))
            if (removed and "evidenceRefs" in value and not value.get("relatedTaxa")
                    and not any(value.get(key) for key in _PROSE_KEYS)):
                value["evidenceRefs"] = []

    for chapter, value in data.items():
        if chapter != "introduction":
            walk(value, (chapter,))



def _normal_category_summary(category: str) -> str:
    if category == '影像科':
        return '所列正常影像记录未见对应检查部位的明确异常表现，可与本次影像异常发现分别对照。'
    if category == '神经系统查体':
        return '所列神经系统查体正常项未记录相应的阳性体征，可作为本次神经系统评估的正常查体依据。'
    if category == '检验科':
        return '所列正常或阴性检验项目未提示各自所检测的异常信号，可作为与本次异常检验项对照的正常信息。'
    return '所列' + category + '正常项目未记录相应的异常发现，可作为与本次异常项目对照的检查信息。'


def complete_normal_findings(raw: dict, facts: dict[str, ReportSourceFact]) -> None:
    """Keep one source-bound generic explanation per examination class.

    Specific source findings, literal advice and reviewed explanations remain
    on their own rows. The measured rows and their original source relations
    are never merged to remove repeated generated category prose.
    """
    data = raw.get('data', raw)
    if not isinstance(data, dict):
        return
    rows = (data.get('medicalAssessment') or {}).get('normalFindings') or []
    represented = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        fact = _bound_fact(row, facts, 'clinical')
        if (fact is None or fact.abnormal or row.get('assessmentLevel') == '异常'
                or row.get('sourceStatus') not in {'正常', '阴性', '未见异常'}
                    and row.get('assessmentLevel') != '正常'
                or re.fullmatch(r'\s*(?:\d*\s*\+{1,4}|阳性)\s*', fact.value)):
            continue
        category = (row.get('system') or '').strip()
        if not category:
            continue  # No invented examination class from a familiar analyte.
        existing = row.get('explanation')
        if existing is not None and not isinstance(existing, str):
            continue
        summary = _normal_category_summary(category)
        lines = (existing or '').splitlines()
        generic = any(line.strip() == summary for line in lines)
        if row.get('evidenceRefs'):
            if generic:
                represented.add(category)
            continue  # A generated category cleanup cannot remove reviewed citations.
        if generic:
            literal = any(summary in str(value or '') for value in (fact.value, row.get('sourceAdvice')))
            if category in represented and not literal:
                row['explanation'] = '\n'.join(line for line in lines if line.strip() != summary) or None
            represented.add(category)
            continue
        definition = _clinical_meaning(row.get('name'), row.get('value'))
        if existing and (not definition or definition not in existing):
            continue  # Preserve a meaningful source or reviewed explanation.
        remaining = [line for line in (existing or '').splitlines()
            if line.strip() and line.strip() != definition and line.strip() != '既往检查记录。']
        if remaining:
            row['explanation'] = '\n'.join(remaining)
            continue
        row['explanation'] = summary if category not in represented else None
        represented.add(category)

def complete_metric_meanings(raw: dict, facts: dict[str, ReportSourceFact], *, fill_empty_clinical: bool = True) -> None:
    """Complete source-bound meanings with an explicit evaluate prose policy.

    Formal evaluate projection supplies False: clinical state/category/action
    and disease meanings are completed in their reviewed paths, never by a
    medical glossary. The default retains standalone glossary compatibility.
    """
    data = raw.get("data", raw)
    if not isinstance(data, dict):
        return
    refine_evaluation_prose(data, facts=facts)

    def fill(rows, kind, group=None, *, generic_detail=False):
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            if kind == "disease" and not fill_empty_clinical:
                # The formal report's disease body is source text or reviewed
                # medical meaning. A later glossary must not append a new,
                # uncited definition after those reviews have completed.
                continue
            existing = row.get("explanation")
            if not fill_empty_clinical and kind not in {"clinical", "disease", "taxa", "resistance", "phylum"}:
                # Formal interpretation slots receive source states or whole
                # reviewed prose. A finite glossary is not a personal result.
                if (isinstance(existing, str) and not row.get("evidenceRefs")
                        and existing == _meaning(row, kind, group)
                        and _bound_fact(row, facts, kind) is not None):
                    row["explanation"] = None
                continue
            if generic_detail:
                # A name inserted into the old fallback adds no row-specific
                # information. Retire only that exact, uncited, source-bound
                # template; reviewed prose, definitions and citations survive.
                # Keep the confirmed explanation key, including its null value.
                if (isinstance(existing, str) and existing == _context_meaning(row, kind)
                        and not row.get("evidenceRefs")
                        and _bound_fact(row, facts, kind) is not None):
                    row["explanation"] = None
                continue
            if existing is not None and (not isinstance(existing, str) or existing.strip()):
                if kind == "clinical" and isinstance(existing, str):
                    if (fact := _bound_fact(row, facts, kind)) is not None:
                        row["explanation"] = _complete_clinical_text(row, existing, fact)
                elif kind == "disease" and isinstance(existing, str):
                    if _bound_disease_fact(row, facts) is not None:
                        row["explanation"] = _complete_disease_text(row, existing)
                continue
            if kind == "clinical" and not fill_empty_clinical:
                continue
            if (_bound_disease_fact(row, facts) if kind == "disease" else _bound_fact(row, facts, kind)) is not None:
                row["explanation"] = _meaning(row, kind, group)

    medical = data.get("medicalAssessment") or {}
    for key in ("findings", "normalFindings"):
        fill(medical.get(key), "clinical")
    complete_normal_findings(data, facts)
    micro = data.get("microbiomeAssessment") or {}
    for key, kind in (("overallStructure", "structure"), ("healthMetrics", "health"),
            ("shortChainFattyAcids", "scfa"), ("taxa", "taxa"),
            ("phylumComposition", "phylum"), ("antimicrobialResistance", "resistance")):
        fill(micro.get(key), kind, generic_detail=kind in {"taxa", "resistance"})
    disease = data.get("diseaseRiskAssessment") or {}
    fill(disease.get("riskRanking"), "disease")
    for category in disease.get("categories") or []:
        if isinstance(category, dict):
            fill(category.get("items"), "disease")
    nutrition = data.get("nutritionAssessment") or {}
    for key, kind in (("majorMetabolism", "major"), ("neurotransmitters", "neuro"),
            ("aminoAcidsAndMetabolites", "amino"), ("weakSynthesisChart", "weak")):
        fill(nutrition.get(key), kind)
    for key, kind in (("detailedMetabolism", "metabolism"), ("vitaminsAndMinerals", "nutrient")):
        for group in nutrition.get(key) or []:
            if isinstance(group, dict):
                fill(group.get("items"), kind, group.get("code"))
    # Repeated AMR observations retain the parent's measurement type. Matching
    # only a drug name or an equal value is insufficient: both rows must bind
    # to the same source fact with the existing unit/date/kind checks intact.
    resistance_sources = []
    for row in micro.get("antimicrobialResistance") or []:
        if isinstance(row, dict) and (fact := _bound_fact(row, facts, "resistance")) is not None:
            resistance_sources.append((_key(row.get("name")), fact))
    for pathway in (data.get("functionalPathways") or {}).get("pathways") or []:
        if isinstance(pathway, dict):
            for row in pathway.get("relatedMetrics") or []:
                if not isinstance(row, dict):
                    continue
                resistance = any(_key(row.get("name")) == name
                    and _bound_fact(row, {fact.fact_id: fact}, "resistance") is not None
                    for name, fact in resistance_sources)
                fill([row], "resistance" if resistance else "functional")
    # Keep the existing amino-acid glossary once in the main nutrient group.
    # Repeated row names do not make the same elementary definition a new
    # personal interpretation. Exact source values, grades, unique prose and
    # all cited explanations retain their original fields and provenance.
    amino_rows = list(nutrition.get("aminoAcidsAndMetabolites") or [])
    amino_rows.extend(nutrition.get("weakSynthesisChart") or [])
    amino_rows.extend(metric for pathway in (data.get("functionalPathways") or {}).get("pathways") or []
        if isinstance(pathway, dict) for metric in pathway.get("relatedMetrics") or [])
    generic_seen = False
    for row in amino_rows:
        if not isinstance(row, dict) or row.get("evidenceRefs"):
            continue
        name = row.get("name")
        if (not isinstance(name, str)
                or row.get("explanation") != f"{name}是一种氨基酸，属于构成蛋白质的基本单元。"
                or _TERM_MEANINGS.get(_key(name)) != row["explanation"]
                or _bound_fact(row, facts, "amino") is None):
            continue
        if generic_seen:
            row["explanation"] = None
        generic_seen = True
