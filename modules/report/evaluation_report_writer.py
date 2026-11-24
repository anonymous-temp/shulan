"""Chapter-level authoring and review for the seven-chapter evaluate report.

The former path wrote each template cell in isolation as one or two sentences
under a long list of prohibitions, then rejected whole cells sentence by
sentence; cells that failed twice were published empty. This module keeps the
same template addresses and the same publication contract (text bound to
source facts, citations copied from server-held original passages) but:

* a batch of related cells is written in one call with the complete clinical
  record, the rows being explained and the candidate evidence passages;
* every cell has a job description and a length range instead of a cap;
* one reviewer pass per batch checks facts, unsupported claims, citations and
  safety, and returns a corrected text instead of only a rejection.

The module never edits a source value. It returns blocks in the shape consumed
by ``evaluation_report.apply_reviewed_report_blocks``.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from copy import deepcopy
from decimal import Decimal
from decimal import InvalidOperation
from difflib import SequenceMatcher
from itertools import zip_longest
from time import monotonic

from pydantic import BaseModel

from utils.log import getLogger

from .assessment_reviewed_synthesis import reviewed_cell_sources
from .assessment_rule_sources import is_rule_handle
from .assessment_rule_sources import model_rule_conclusions
from .assessment_rule_sources import rule_conclusion_map
from .assessment_rule_sources import sentence_rules
from .assessment_source_receipts import add_recall_timing_note
from .assessment_source_receipts import has_number
from .assessment_source_receipts import recall_timing_targets
from .assessment_source_receipts import repair_receipt_findings
from .assessment_source_receipts import repaired_receipt
from .assessment_source_receipts import retained_paragraphs
from .assessment_source_review import GROUNDING_GUIDE
from .assessment_source_review import build_observation_registry
from .assessment_source_review import build_source_frame
from .assessment_source_review import compact_microbiome_observation
from .assessment_source_review import draft_source_findings
from .assessment_source_review import medical_source_contract
from .assessment_source_review import model_source_contract
from .assessment_source_review import paragraph_version
from .assessment_source_review import proof_output_schema
from .assessment_source_review import short_observation_ids
from .assessment_source_review import validate_source_proofs
from .evaluation_evidence_recall import _term_in
from .evaluation_evidence_recall import topic_terms
from .evaluation_presentation import ITEM_TITLE
from .evaluation_presentation import ai_tells
from .evaluation_presentation import reader_text
from .fallback_policy import EVALUATE_MODEL_CONCURRENCY
from .web_source_policy import evidence_grade

logger = getLogger(__name__)

WRITER_REVISION = 'chapter-writer-v17-scoped-cell-review'
_SERIOUS = frozenset({
    'fact_error', 'unsafe_advice', 'unsupported_claim', 'citation_mismatch', 'missing_interpretation', 'off_task',
})
_BATCH_CELLS = 10
_SMALL_SUMMARY_CELLS = 3
_SUMMARY_BATCH_CHARS = 2400
_SOURCE_PROOF_REPAIR_SECONDS = 50.0
_PUBLICATION_CHOICES_PER_ROLE = 4
_SAMPLING_DATE_CLAIM = re.compile(
    r'采样(?:日期|时间|日)?(?:为|是|在|[:：])?\s*(?P<after>\d{4}-\d{2}-\d{2})'
    r'|(?P<before>\d{4}-\d{2}-\d{2})\s*(?:当日|日)?采样'
)
_PROSE = frozenset(
    {
        'text',
        'summary',
        'explanation',
        'association',
        'health_meaning',
        'healthMeaning',
        'description',
        'basis',
        'followup_action',
        'followupAction',
        'attention_basis',
        'attentionBasis',
        'evidence_strength',
        'evidenceStrength',
        'modifiability',
        'source_advice',
        'sourceAdvice',
        'resistance_notice',
        'resistanceNotice',
        'interpretation',
        'handling',
    }
)
_META = frozenset(
    {
        'sources',
        'evidence_refs',
        'evidenceRefs',
        'availability',
        'source_items',
        'sourceItems',
        'reference_literature',
        'referenceLiterature',
        'indicator_standards',
        'indicatorStandards',
    }
)
_PROCESS_WORDS = re.compile(
    r'\b[FTE]\d{2,4}(?:\.\d+)?\b|factId|passageId|检测方关联记录|来源模型|仅有菌群模型记录|本例原判定|源记录|writingGuide'
    r'|recordPresenceIndex|comparisonScope|priorReviewFindings|sourceContract|sourceVerdict|allowedPassageIds'
    r'|用户自述[，, ]*未核验|不作特殊解读|(?:本段|本格)(?:按|通过)(?:审核|校验|入刊)规则|按代码规则'
    r'|不推荐个人.{0,16}(?:配方|采购|剂量)'
    r'|未发现需在(?:本章|此处)另行补充|未形成(?:同时适用的)?共同(?:干预|决策)|向下推进的节点'
)
_EDITORIAL_INSTRUCTIONS = re.compile(r'本格|本单元格|本栏(?:应当|负责|需写)|(?:未经|未予)核验|与无日期单页分别保留')
_POPULATION_SCREENING = re.compile(
    r'(?:患病率|发病率|检出率|检出比例|患病比例)[^。！？\n]{0,30}[0-9](?:\.[0-9]+)?[%％]'
    r'|(?:发病率|患病率)(?:上升|较高|升高)的年龄段|筛查的起始年龄段'
)
_CARE_SUMMARIES = frozenset({'综合评估', '临床评估摘要', '评估意见', '综合结论'})
_LOCAL_ASSESSMENT_SCOPES = frozenset({'digestive', 'structure', 'organisms', 'function'})
_QUANTIFIED_DIET = re.compile(
    r'(?:每天|每日|每周|每餐|每次)[^。！？；\n]{0,30}'
    r'(?:\d+(?:\.\d+)?\s*(?:(?:g|mg|mL|kg)(?![A-Za-z])|克|毫升|次|份|杯)|[一二两三四五六七八九十]+\s*(?:次|份|杯))',
    re.I,
)
_POSITIVE_BE_CLAIM = re.compile(
    r'(?:对过路菌的抑制|定植抵抗|抑制能力|定植能力)[^。！？；\n]{0,8}(?:偏低|较弱|下降|不足|减弱)'
    r'|(?:屏障损伤|屏障受损|感染风险升高|更易感染)'
)
_FUNCTION_SCORE = re.compile(r'(?<![\d.])\d+(?:\.\d+)?\s*(?:分(?!钟|泌|解|析|类|别|布|子)|/\s*100)')
_ABUNDANCE_VALUE = re.compile(r'\d+(?:\.\d+)?\s*[%％]')
_DENTAL_NEGATIVE = re.compile(r'牙龈\s*(?:未见|无|没有)\s*(?:明显)?\s*(红肿|出血|退缩|肿胀|溃疡)')
_PANEL_ATTRIBUTION = re.compile(
    r'(?:面板|组合|菌群|本组|总评|总判)[^。！？；]{0,15}(?:异常|偏离|失衡)'
    r'[^。！？；]{0,10}(?:来自|源于|归因于|由[^。！？；]{1,24}(?:所致|导致|造成|引起))'
    r'|(?:导致|造成|引起)[^。！？；]{0,12}(?:屏障|面板|组合|总评|本组)(?:异常|下降|损伤)'
)
_ONCOLOGY_CONTEXT = re.compile(
    r'肿瘤(?:患者|人群|病人)|癌症(?:患者|人群|病人)|\bcancer patients\b|\bpatients with cancer\b|'
    r'\boncology (?:patients|population)\b|\bcancer-related constipation\b',
    re.I,
)
_VERIFIED_MALIGNANCY = re.compile(
    r'恶性肿瘤|癌(?!前)|白血病|淋巴瘤|骨髓瘤|\b(?:cancer|carcinoma|malignancy|leukemia|lymphoma|myeloma)\b',
    re.I,
)


def _local_assessment_scope(target):
    owner = target.get('owner') or {}
    if owner.get('presentation') == 'assessment.v1' and tuple(target['path'][:1]) == ('_assessment_editorial',):
        code = str(owner.get('code') or '').partition('.')[0]
        if code in _LOCAL_ASSESSMENT_SCOPES:
            return code
    return None


def _target_role(target):
    roles = target.get('allowedRoles') or ['interpretation']
    if target.get('contentDuty') == 'management' and 'action' in roles:
        return 'action'
    return 'interpretation' if 'interpretation' in roles else roles[0]

WRITING_GUIDE = [
    '你是为受检者出具正式书面评估报告的医学团队（消化内科、内分泌代谢、临床营养、微生态医学）。以客观书面语直接陈述实测结果、判断和实际安排；确需指代本人时写“受检者”，简单句可省略主语。安全判断保留证据出处所需的性质，如“本人回顾”“筛查提示”。',
    '正文直接给出事实、判断及用途，少用“根据问卷”“体检显示”等引出语。实测项目保留源测量名称；测得丰度为零且没有原未检出判定时仅写原测值和状态，不能说低于检出限。适用的医生原建议按其事实和条件陈述。',
    '围绕读者需要解决的健康问题组织文字。选出真正改变判断的化验、影像、症状、生活习惯或菌群记录，写清共同支持的管理方向。只在可靠的同向证据出现时写交叉印证。相反线索用于修正判断，涉及处置时再简洁交代。',
    '报告按用途分工。总览写重点，表格写事实，关联解释回答实际问题，生活计划集中于第六章。'
    '不列举无关结果后再声明没有关联；直接写成立的判断。希望改善不等于功能受损。'
    '指定病名问答均无只表示所询病名，无资料证明全病史为空。不能据并列选项推定单一持续时长或不足。'
    '剂量或饮水单位截断时，使用定性建议，禁止以其他资料或常识补全单位。',
    '条目式写作：form为paragraph的格子写成若干条目，每个条目是paragraphs里的一项，由title和text组成。title是2至8个字的名词性小标题，不带标点；text是这一条的正文，不重复title，一条只讲一件事，一般不超过130字，超过就拆成两条。cell.outline给出了该格建议的条目和顺序，按实际内容取舍或增补，可以用具体对象作小标题（具体病灶、指标或检查的名称）。第一条是结论或判断。并列的多项内容（多个病灶、多项指标、多条措施）分条写，不写成一长段。没有outline的短格子写成一段，不加title。',
    '正文长度由资料决定。先写成立的判断，再选会改变判断的关键依据及行动目的。只有数值或幅度确实影响结论时才写进解释，其他实测留在结果列。数值与patient记录一致，计算指标使用已经核验的计算结果。',
    '解释机制和研究结论时使用evidence里给出的原文段落，用自己的话转述并在cite里填写段落编号；没有对应段落的研究结论不要写成“研究表明”。教科书级的常识性机制可以不引用。指南给出的界值、目标和随访间隔必须来自所引段落或patient记录中的医生建议。',
    '人群检出率、筛查起始年龄及研究效应量须与本人适用条件匹配，只有直接改变本人管理选择时才写。'
    '已由专科执行的检查准备细节由专科说明。',
    '已有需查因的异常时优先安排临床评估。指南的质量指标、观察研究的等待分组或研究随访上限，'
    '不能作为个人可以等待的期限。复测和生活调整可同步，不能替代已经有明确依据的查因安排。',
    '解读菌群与功能结果时，区分检测量、来源模型预测、适用的人群研究和受检者临床结果。选择与本段问题有关的角度，解释研究关联如何影响管理。只有菌群资料时也可说明有据的检测意义。临床、生活或菌群资料缺少时，其他来源能各自支撑结论。',
    '参考区间内的单项只交代本项含义。没有与某疾病相匹配的个人发现时，省去用于对照的疾病名。'
    '只有原记录给出参考区间时才写“在参考范围内”。记录只标正常时直接写状态。'
    '同一人按不同指标作出的分类分别准确表达，避免用一个维度的分档概括其他维度。'
    '只用有明确适用标准的程度分级，不自行追加轻中重分档。',
    '有出血、胸痛、晕厥、急性神经系统症状等警示表现时，交代就诊优先级及诊疗目的，紧急情况建议及时急诊。'
    '具体检查方式及安排由临床评估决定，不用自行预约检查代替就诊，也不用继续观察代替需要的查因。',
    '阳性筛查结果（粪便隐血、尿潜血等）和明显升高的肿瘤标志物属于查因线索，不以自述的诱因解释阳性原因，'
    '也不据单次结果推断或排除肿瘤。结节、息肉、囊肿的随访以影像分级和原检查建议为准。'
    '血红蛋白降低先写查明原因，不直接给补铁或补充剂方案。妊娠或哺乳期的目标区间、补充剂和功效菌方向只在明确适用时写。',
    '检验名仅为葡萄糖时写实测葡萄糖。只有同次采样明确为空腹才能套用空腹临床界值；'
    '接受临床筛查界值时仍要区分筛查与确诊。钠摄入量和食盐质量是不同口径。'
    '有心肺异常已安排评估且尚无运动许可时，各章均不建议维持、增加或恢复具体运动量。'
    '心肺评估和运动安排的先后顺序由原医嘱和核实的临床行动决定。记录仅有运动时长、频率或项目而未确认强度时，'
    '不能认定已达到推荐的中等强度活动量。'
    '膳食、饮水和排便安排只用于证据直接支持的健康问题，独立的健康问题分别写对应行动。',
    'patient.clinicalTrends给出日期明确的可比较检查。存在多次记录时如实写多次出现或变化趋势，'
    '不将最新一次误写成仅测过一次。检测条件不明时保留复核安排，不据趋势判定病因。',
    '影像随访间隔和升级处置条件，以明确影像分类、原医嘱和适用于该分类的指南条件为准。'
    '影像未分型时交代随诊目的，不设尺寸触发线、固定周期或自行预估良恶性。'
    '血脂报告参考上限不是治疗目标；治疗目标需有适用于个人的临床分层。'
    '脂蛋白(a)为独立检验，应写全名，不能简称为脂蛋白。',
    '建议要能照着做：检查项目、就诊科室、时间、可量化的目标、需要提前就医的情况；生活方式建议要对应本人的实际习惯，给出频次或替代办法。各部分分工不同：临床问题给诊疗与随访，肠道菌群和营养功能只给与该结果直接相关的调整，综合结论汇总成管理方案。同一件事的处理意见在全报告保持一致。',
    '行动、比较基线和预期改变方向要一致。若后一项活动时间固定，提前前一项会延长两者间隔。'
    '只有已知当前时间安排及适用的管理目标时才说明具体间隔。',
    '确定程度用准确的名词一次说清：筛查阳性、超声提示、与某病相关的菌群特征。不写“不是诊断”“不能替代医生”“仅供参考”“只是关联”一类声明，不给本人陈述的情况加“未经核验”“本人报告”之类的标注。',
    '不得出现：资料中没有的检查结果、症状或病史；处方药名称与剂量、补充剂剂量、疗效承诺（可以写“是否用药由专科医生评估”）；内部编号和工作流程用语；说明数据来路的话（“标注为”“给出的是”“资料没有提供”“中间结果”）；“本类”“七个维度”“被列为提示项目”一类模板用语。指标名称用中文，不加英文缩写括注，国际通用的分级名称（如TI-RADS）可以保留；单位用化验单上的规范符号（mmol/L、cm、kg、kcal），不写成中文单位名。',
    '文风：规范的书面语。句子完整，用词准确、专业而易懂；不用口语、聊天语气和语气词（如“其实”“说白了”“就行”“挺”“咱们”“吧”），不用“记住”“先做三件事”这类当面叮嘱的说法。不用“不是……而是……”式对比，不用破折号，不用“核心”“主线”“抓手”“关键在于”“值得注意的是”“需要说明的是”“换句话说”“也就是说”“总的来说”“综上”等套话，不硬凑三点排比，不复述格子标题。',
    '同一批格子是同一份报告里相邻的内容：表格和图里已经列出的分数和丰度不必逐项复述，挑最突出的写透；同一句限定或建议不要在每一格重复。',
    (
        'patient.recordPresenceIndex是全报告已有条目的名称目录'
        '，只证明有这项记录，不证明检出、阴性、正常或异常；具体结果只读clinicalR'
        'ecords、selfReported、cellRecords和row。本格没有'
        '展示某个值，不代表全报告没有检测。comparisonScope只界定本格同单位'
        '、同参考口径的比较范围，不代表全报告；最高、最低或全部等概括不得超出完整可见范围'
        '。priorReviewFindings列出前文实质审稿意见，仅用于避免重犯，不'
        '是新的事实或证据；引用只用本批patient和evidence的编号，仍需核实实'
        '际记录与适用条件。'
    ),
]

REVIEW_GUIDE = [
    '你是独立的医学审稿人，逐格审核下面的成稿。只找实质问题，不做文字润色。',
    'reviews必须逐一返回cells中所有id的审核结果。即使全部通过，每格也要返回pass。',
    '核对事实：正文里的每个数值、检查结论、症状、病史、用药和家族史都必须能在patient记录、该格row或previouslyWritten里找到；找不到的属于fact_error。把参考范围内的结果写成异常、把本人陈述的症状写成检查确诊的疾病、把家属的病写成本人，同样是fact_error。本人陈述的生活习惯和症状直接陈述即可，不要求交代出处，也不要求另加“未经核验”一类标注。',
    '历次测量以patient.clinicalTrends核对。已有同指标多次记录却写成“单次测得”按fact_error处理。'
    '原记录只标正常未给区间却写“在参考范围内”同样修正。',
    (
        'previouslyWritten和priorReviewFindings用于承'
        '接前文及避免重犯已纠正的问题，不是新的事实或独立医学证据。摘要仍须核对patie'
        'nt原值、主体、时间、确定程度及引用适用条件；前文错误、扩大结论范围或遗漏限制条'
        '件时不能直接沿用。'
    ),
    '核对推断：把筛查阳性写成确诊、把研究关联写成本人病因、把菌群功能预测写成人体缺乏或疾病、把菌群风险等级写成患病概率，属于unsupported_claim。修改时用准确的名词表达确定程度，不要加“不是诊断”一类的声明句。',
    '核对基础条件：葡萄糖记录及同次采样都未确认空腹时，删去“空腹”及依赖空腹条件的分层。'
    '钠与食盐的数值和单位不可互换。已有待评估的心肺异常时，审核各章是否在评估前建议维持或增加特定运动量。'
    '频率和时长不能替代强度核实。检查多项问题并列时每项行动是否直接对应，避免跨问题套用。',
    '影像未分型却补写大小触发线或固定复查间隔，按unsupported_claim删去相应外推。'
    '普通胸片或生命体征未见异常不代表已完成运动安全评估。',
    '功能试验和计算指数只回答其设计用途，不据以判定未测量的病理状态或严重程度。专项筛查整单结论在参考范围时，局部偏离单独解释，不把局部偏离等同相应器官病变。',
    '菌群组内明确检测到上升、下降或其他偏离，不能写成全组无偏离；只发现一项异常仍不能写成只检测了一项。检测到基因序列不能写成检测到活性毒力产物。',
    '核对引用：cite所列段落必须真的支持该段的医学结论，且人群与条件适用于本人；不支持的属于citation_mismatch。写了“指南建议/研究显示”或给出具体界值、目标值、随访间隔却没有cite或医生建议作依据，也属于此类。教科书级的常识没有引用不算问题。',
    '先核对具体指标、研究对象和终点。不同微生态评分之间不能互证；动物和细胞数据不支持个人症状或感染结论。'
    '专病指南只有符合该病的适用条件才用作个体建议。未知检查、病程及选项冲突不能按正常或异常补齐。',
    '与本人处置无关的人群检出率、筛查年龄或研究效应量判为off_task，直接删除。'
    '摘要不承接各问题已有的执行细节，压缩后仍保留全部关键判断即可。',
    '核对安全：出现处方药名称或剂量、补充剂剂量、疗效承诺，或把需要尽快查因的问题写成可以等待观察，属于unsafe_advice。',
    '把研究或筛查项目的等待上限当作本人可等待的时间，或要求先等复测、生活调整见效才安排已需进行的查因，'
    '均属unsafe_advice。保留恰当的就诊优先顺序。',
    '逐项核对行动与目的是否同向，以及引用能否用于本人的情况。时间前移或后移后间隔变化方向写反，'
    '或把一般饮食建议写成用药调整，均属unsafe_advice。没有相关个人线索时，用疾病人群研究进行正常排除属于unsupported_claim。',
    '核对职责：没有回答该格的question、通篇泛泛而谈、与同批其他格子大段重复、出现内部编号或流程用语、写出结果来源方（检测方、原报告、主检、机构、模型）、出现免责声明式的句子、用“您”称呼读者，属于off_task。',
    '分析栏若只有结果复述、换词后的重复结论，或泛用建议而没有说明本问题的意义或处理目的，标为missing_interpretation。保留有据的局部判断；无需补足样例的篇幅、病因链条或固定条数。',
    '逐格核对模板的职责。把无关结果、机制或研究写完才否定其关联，按off_task删去整条；'
    '若删减后无跨项结论，简洁表达此格本身成立的判断。功能预测只解释本组预测特点；'
    '局部临床信息须和同一个对象、同一方向直接相关，菌群功能预测本身不能支持临床判断、定量饮食或处置。'
    '牙科、酶学或其他部位的研究不能解释此处肠道指标。',
    '审核专科研究的人群条件，特定疾病人群的指南不能支持一般人群同名症状的个人处置。'
    '查体“未见明显异常”不等于各项具体阴性表现，具体表现只写记录明示的内容。'
    '查体所见只记为检查所见，不倒推未记录的生活习惯。'
    '警示表现的处理审核就诊顺序：紧急表现保留急诊方向，删去用自行预约检查替代临床评估的建议。'
    '符合既有筛查或专科评估的检查方向可保留。',
    '回顾性自述若无同一日期、同一标本编号或记录明确连接，不与有日期的检查结果写成同期或同一标本。'
    '问卷填写日不等于症状发生日。阳性筛查结果属于查因线索，未定位病变部位，不用自述的诱因解释阳性原因；'
    '明显升高的肿瘤标志物不据单次结果推断或排除肿瘤。结节随访间隔超出影像分级和原检查建议、贫血直接给补充方案、'
    '妊娠或哺乳期套用不适用的目标与补充方向，均需修订。',
    '没有上述问题就判pass。有问题判revise，并在revisedParagraphs里按原顺序给出该格改好后的全部条目（没有问题的条目原样照抄，title保持不变，不能只返回改过的那一条）：只改有问题的地方，保留原稿正确的内容和篇幅；factIds和cite按改后内容填写。',
    (
        '核对资料范围：recordPresenceIndex仅是有记录的目录，不能据名称'
        '推断阳性或正常；当前格没有值也不能写成本次未检测、未细分或未提供。检查目录与实际'
        '记录，禁止把未知当作阴性。comparisonScope的complete仅指该'
        '格记录是否完整可见；最高、最低、全部等结论只能用于完整且同终点、同单位、同参考口'
        '径的比较组，不能从局部组推广为全报告。'
    ),
    (
        '核对临床用途：实验室参考范围、筛查界值、诊断界值、治疗目标与随访条件不可混用。引'
        '用指南的无需随访、暂不处理或条件性建议时，须满足该条全部适用前提；不因一个尺寸、'
        '单项正常或不完整分级而省略其他条件。'
    ),
    '查因类结果只定位到已证实的解剖部位。样本中检出信号时，按检查目的写待评估部位，'
    '不要写成已经定位病灶或出血来源。',
    (
        '核对推演层级：一般生理机制不能定位为本人的病因或薄弱环节。菌群丰度和功能预测不能'
        '反推出本人代谢物流量或去向、体内浓度、实际摄入与吸收利用、器官损伤或解毒负荷；缺'
        '少对应测量或适用证据时删除该个人推断，保留有依据的行动。前体与辅因子的局部变化也'
        '不能概括为整条合成途径的原因。'
    ),
    (
        '核对完整性：按cell.guide、outline和scope核对职责，列出的机'
        '制线、问题类别和正文实际展开应闭合。宣称三条线却只展开两条、结论与同段数字或分项'
        '相反、以正常项冲淡已知异常，均需修订。'
    ),
]

HUMANIZE_GUIDE = [
    '下面是一份健康评估报告里已经过医学审核的正文。你的任务只是改文风：去掉机器写作的痕迹，使它成为规范、正式、不带人称的书面语，读起来是一份对外交付的医学评估报告。医学内容一个字的意思都不能变，信息一条都不能少。',
    '必须原样保留：每一个数值和单位（单位保持mmol/L、cm等原写法）、检查名称、结论、依据、解释、建议的科室和时间、确定程度用词（可能、约、偏高、提示、筛查阳性等）、主体（本人还是家属）。每段开头“小标题：”的部分原样保留。不增加新内容，不删去任何事实、依据或建议，篇幅与原文基本相当。',
    '要改掉的写法：称呼读者的“您”“你”和“我们”（改为省略主语，确需指代时写“受检者”）；交代信息出处的引出语（问卷显示、根据问卷、问卷中、体检显示、菌群检测显示、检测结果提示），直接陈述事实；口语和当面叮嘱的语气（其实、说白了、就行、挺、咱们、记住、先做三件事）；说明数据来路的话（标注为、给出的是、资料没有提供）；“不是……而是……”式对比；破折号；“核心”“主线”“抓手”“一条线”“关键在于”“值得注意的是”“需要说明的是”“换句话说”“也就是说”“总的来说”“综上”等套话；为凑三点而写的排比；“不是诊断”“只是关联”“仅供参考”“未经核验”一类声明；写出检测方、原报告、主检、机构、模型等来源方的说法。',
    'tells列出了程序在该段检测到的痕迹，请逐项处理；没有痕迹且已是规范书面语的段落原样返回。段落数量和顺序保持不变。',
]

_CHAPTER_BRIEF = {
    'medicalAssessment': '本部分逐项回答“这是什么问题、到什么程度、接下来怎么办”。'
                         '“评估意见”负责定性定量和临床意义，“管理建议”负责具体做法，两栏不重复。'
                         '评估时把相关的化验、影像、症状、生活方式和肠道菌群依据合在一起看。',
    'microbiomeAssessment': '本部分评估肠道微生态：每个指标和每个偏离的菌是什么、本次结果意味着什么、'
                           '与本人的健康问题有什么关系。通用的减重、运动建议不在这里写；'
                           '只写与该指标或该菌直接相关的做法（例如哪类膳食纤维促进某类菌）。',
    'diseaseRiskAssessment': '本部分从风险角度看问题，不重复“评估意见”。已有的临床问题写风险层级和需要预防的结局；'
                            '仅有菌群特征相关的项目，写清相关菌群及变化方向、本人有无相应表现和是否需要处理。',
    'nutritionAssessment': '本部分评估肠道菌群对营养物质的合成与分解潜力，要讲清各组的格局，'
                           '并落到具体的食物选择上。体重管理和运动的通用建议不在这里重复。',
    'functionalPathways': '本部分讲功能通路之间、通路与健康问题之间的联系，不重复对功能分数的解读。',
    'healthOverview': '本部分是全报告的评估结论，写在最前面给读者一个完整判断。'
                      '内容来自后面各部分已经写好的结论，不引入新的说法。',
    'comprehensiveConclusion': '本部分把前文的判断收束为接下来一段时间的健康管理方案。每一项都要可执行、可核对。',
}

_SIX_CHAPTER_BRIEF = {
    'microbiomeAssessment': '本部分解释实际检测中最重要的肠道构成与功能发现。'
                           '按检测口径分别说明组成、功能预测和临床评估。饮食与生活方案只在第六章写。',
    'nutritionAssessment': '本部分用检测项目说明营养与代谢相关菌群功能的实际模式。'
                           '不得按名称或数值方向把菌群通路同血液临床检验对应。饮食与生活方案集中在第六章。',
}

_ASSESSMENT_WRITING_GUIDE = [
    '优先引用已确认的具体检查、症状、病史及原医嘱。患者转述的既往诊断写为“既往自述患有”，并保留时间；问卷目标、无阳性资料及合并选项不得升级为临床疾病、确定暴露或测量结果。',
    '总览、第五章和第六章沿用同一问题的性质、处置优先级和实际安排。没有已确认警示症状、明确临床发现或原医嘱支持时，菌群测序、相对丰度及模型等级均不单独触发加急或专科筛查。',
    '图表已列项目、数值和状态时，解读只写会影响判断的共同特征及用途。无相应个人依据就省去无关疾病、研究和否认句。避免“指标含义”“本次结果”“判读口径”等写作过程标题。',
    '群落组成和功能预测的原始术语应准确。百分比是相对丰度；只读明确检出的基因名，分类标题和候选项均不能证明毒素表达、感染或疾病。',
]
_ASSESSMENT_REVIEW_GUIDE = [
    '若段落把病史转述写作已核实诊断、把多个行为选择拆成某单项，或把同期无关结果写作印证，按fact_error或unsupported_claim修正。判断与建议需保留能影响本人的日期、原医嘱及其触发条件。',
    '核查各章节的紧迫程度和行动是否一致。将菌群序列或功能预测写为感染、毒素表达、疾病或专科筛查指征，按unsupported_claim修正。仅保留真实检测的术语和范围。',
    '不同检查方法、人群、标本或终点不得互证。研究结论超出本人的适用年龄、疾病状态或治疗情境时删除该外推。删除不影响判断的规则、重复数据和“列完再排除”的无关内容，保留影响处置的必要条件。',
]

# Formal written style of every reader-visible sentence (wiki/report-assessment-v2.md 1.2).
# Shared by the planned writer and reviewer so that the same words are banned
# when writing and are a must-fix item when reviewing.
ASSESSMENT_BANNED_TERMS = (
    '您', '你', '我们', '原件', '原报告', '主检', '模板', '本人报告', '未形成解读', '部分报告', '按原件记录',
    '问卷显示', '检测方', '检测机构', '仅供参考', '不能替代医生', '未经核验',
)
_ASSESSMENT_STYLE_GUIDE = (
    '文风：正式书面语，客观陈述，多数句子省略主语，确需指代时写“受检者”。不写免责声明、推销句、比喻和夸张'
    '（如“引擎”“全军覆没”“彻底”“极佳”），不写“绝非”“必然”一类绝对化因果。'
    '不出现后台与来源方用语：' + '、'.join(f'“{term}”' for term in ASSESSMENT_BANNED_TERMS)
    + '，以及指生成过程的“模型”。需要说明结果出处时写检查名称或“检验单”“体检结论”“检查建议”。'
    '标题和小标题用名词性短语，不用问句。不写“值得注意的是”“需要说明的是”“综上”等套话，不用破折号。'
)

_PLANNED_WRITING_GUIDE = [
    '“优先排查”“优先就医”“尽快就诊”只用于程序判定事项里的 P0 条目（危险信号规则命中者）；没有 P0 时不得把任何结果'
    '写成需优先排查。菌群检测结果（含毒力基因、致病菌或条件致病菌检出）单独出现时只作菌群线索，写明不等于感染，'
    '由医生结合症状判断，不写成就医指征。',
    '必须为本批 cells 中每个 id 各返回一格。逐格按给定长度写结论，不用长段弥补其他格。'
    'usesReviewedFactsOnly 的小结从本人的已核检查、报告和已审分项提炼，禁止新引人群阈值或提出新的检查路径。',
    _ASSESSMENT_STYLE_GUIDE,
    '先用本格主对象给出个人判断，再写会改变理解的依据和意义。只选有实际联系的背景，不复述相邻表格、'
    '其他章节处置、模型规则或无关研究。有明确阳性或冲突时保留正向解读和来源限制。',
    '篇幅按整格计算。先写一条能单独回答题目的判断，再选改变该判断的一到两个本项依据。'
    '上下文是可选资料；无需逐项解读。一个对象的短格写成一段，标题留空；涉及多个并列对象（多个事项、病灶、'
    '集群或诱因维度）时每个对象一条，title 用2至8字的名词性短语。每条以自己的role、实际F和E单独返回。'
    'primaryRole为interpretation时写判断和意义。allowedRoles没有action时，不写给受检者的指令；'
    '确需行动且允许action时，另起一个短action，原检查建议保留完整的后续条件，不在解释段重复。'
    'requiresPersonalAction要求至少一项实际适用的行动。',
    'contextPurpose列出程序已判定的内容（优先排查事项、常规随访、建议补查项目、体型判定要素等）时，'
    '逐项照用其中的时限、间隔、科室、检查和判定，不增删事项、不改写分级，只负责成句。',
    'ruleConclusions列出本格范围内程序规则给出的结论（R编号），含时限、间隔、科室、需完成的检查、'
    '补查项目及理由或目标区间，并附规则出处。照用这些内容的段落在cite中写对应R（可与E并列），'
    '同时在factIds绑定该R的recordIds所列触发记录；时限、科室、检查、项目和数值照抄R，不改写、不新增。'
    'R不是受检者的事实：发现、测值、日期和症状仍以F为依据；R也不是原检查建议，不写成医生原嘱。'
    '只引用本格列出的R，正文不出现R编号。新增的其他行动另起一段，不与R的安排合写。',
    '需要呈现原检查建议时使用sourceAdviceScopes的quote与sourceFactIds成对绑定。每项建议独立表述，'
    '以实际含建议的F作为依据，cite留空。解释或总览只需概括有据的发现和先后，不必再转述其他格的建议。'
    '新行动另起action段，先核个人依据，再核引文能否在该人群和条件下支持此动作；'
    '医生原文为建议时保留建议力度，不改成必须完成、先复查才就诊或报告自己的分层。',
    'hasSeparateOriginalAdvice表示已核原检查建议会在正文后逐条展示。正文只解释需查明的事实与处理目的，'
    '省略这些建议的项目、科室、日期和其他段落应当展示的动作。',
    'reviewedReadings为同一对象先前审核过的完整原段，只作判断本题的线索。用本格的事实和当前许可的引文重新核实，'
    '不得移用上格证据序号或用原段替代整格。综合行动按当前问题的轻重、目的及原条件归纳，设备科普不列作临床行动。'
    '只引用本格allowedPassageIds及passageRoles许可、且原文直接支持本段对象、终点和用途的研究。'
    '人群筛查、预测或预后论文通常不能为已出现不同征象的人选择处置。没有适用原段时写实际来源所能说明的结论，'
    '不介绍与受检者无关的论文或取舍理由。',
    '读取sourceObservation、referenceContext和typedMeasurementGroups中的对象、原状态及实际范围。'
    '逐项区分检测结果、诊断、设备指数、计算值、本人回答、亲属史、目标、丰度、序列和功能潜能。'
    '同一组中的定性分档及不同方法的结果各自保留。分档以逐项给出的resolvedGrade和原状态为准；'
    '共有端点有已核实的逐项分档时采用该档，只有方向时保留端点两档和组方向。只在同档确立时用该档概括全组。',
    '若row列有comparisonReference，仅把相应比较用于它命名的指标及用途。逐项写明是相对检验单参考区间、'
    '目标表中的目标区间（附文献编号），还是符合适用条件的临床筛查区间。组内指标方向不同时指名各项。'
    '检验单位按本项原记录书写；原表里的不同指数记法不可无说明地互换。',
    '阈值、范围、计算、单位和程度只写当前测项自身有据的信息。空腹、静息或其他前提须由该项本身建立。'
    '引用指南时保留适用对象及比较前提。derivedIndex标出实测分级或计算。实测分级用原值及适用标准。'
    '引用多个计算时按记录逐项写算式和输入，不把不同指标说成由同一套输入得出。',
    '按temporalRelations区分实测日期与回顾条件。日期未注明的留样或暴露仅在有独立意义时另段引用；'
    '时间关系未确认时不能断言两条记录同日、异日或来自同一标本。菌群报告日期只表示出具报告，采样日期只用原记录。',
    '主诉表的菌群关联格只写这些结果对于理解主诉的直接价值。测值已经在同一格上段展示，'
    '有适用证据时写具体联系，预测本身未提供个人主诉的直接证据时短句说明即可。',
    '主诉、饮食频次与量、时间限定、行为与目标按各自原问题写。复合记录只用本格sourceSelections。'
    '阴性对照只限其原题，旧问诊未记录某病不自动和随后检查发现矛盾。设备建议保留原记录的条件与力度。',
    '按assayGroups区分构成图谱、特定面板及独立靶标。相对丰度零值、面板无显著异常、实际序列阴性各有含义。'
    '说明真正有依据的生理作用和适用的人体证据，不由预测或测序推断个人患病、人体缺乏、活性毒素或确定病因。',
    '通用临床护栏：阳性筛查结果（粪便隐血、尿潜血等）和明显升高的肿瘤标志物属于查因线索，'
    '不以自述的诱因解释阳性原因，也不据单次结果推断或排除肿瘤；结节、息肉、囊肿的随访间隔以影像分级和'
    '原检查建议为准，未分级时只写随诊目的；血红蛋白降低先写查明原因，不直接给补铁或补充剂方案；'
    '处于妊娠或哺乳期时，目标区间、补充剂和功效菌方向只在明确适用时写。',
    '生活管理各格按本方向选择一项有依据的做法与观察目标，只有题目要求时再写起步或递进。'
    '药物、制剂、剂量、就诊条件和复查周期须有与本例相符的依据。没有处方需要时仍可写有据的健康支持。'
    '配方方向只写菌群类别、膳食纤维种类和营养素方向，不写产品名、品牌和剂量。',
    'priorDraft只供修复已指出的问题。sourceReview标出上次实际逐段核查结果。保留已支持段的事实范围，'
    '用本次F与E重新定位每段来源，'
    '修订后输出本格完整内容。没有适用E的段落可以只依据F。',
]

_PLANNED_REVIEW_GUIDE = [
    '无 P0 程序事项而正文写“优先排查/优先就医/尽快就诊”，或把单独的菌群检出写成就医指征时，判revise并改为'
    '“菌群线索、由医生结合症状判断”的表述。',
    '逐格核实事实、证据适用性和读者职责。按本格职责保留成立的实质解释。未通过时返回完整修订段落，'
    '保留仍正确的来源与判断。不要以规则、拒答、通用套话或重复长列表替代当前格的答案。',
    '文风必改项：正文出现' + '、'.join(f'“{term}”' for term in ASSESSMENT_BANNED_TERMS)
    + '、指生成过程的“模型”、免责声明、推销句、比喻夸张、绝对化因果或问句标题时，判revise并改为正式书面表述'
    '（来源写检查名称或“检验单”“体检结论”“检查建议”，称谓省略或写“受检者”），医学内容和篇幅不变。',
    '先处理draftSourceFindings中与本稿对应的原值、限定及日期问题。可用较短的独立结论替换冗长草稿。'
    '删去无助于本题的例子或文献，保留属于本行的对象、方向、适用范围和有必要的条件。'
    '每个paragraph分别绑定自身实际使用的事实和引文。一个句子转入另一时点、另一研究或给出行动时，'
    '分开写并分别标注。表内已有资料可直接形成的结论无需为它寻找旁证。',
    '逐格回答question。表格右栏须先给这一行独有、与实测相符的判断，再说明该判断带来的健康意义。'
    '同一段搬到相邻行仍然成立、只复述数值或只列影响因素而没说影响为何，判missing_interpretation。'
    '仅当实际正常项、对照或证据边界会改变判断时保留。引用的跨表事实必须说明它怎样影响本格判断。',
    'contextPurpose列出程序判定的事项时，核对每项是否都已成句，时限、间隔、科室、检查和分级是否照用；'
    '遗漏事项判missing_interpretation，改动或新增事项判unsupported_claim。',
    'ruleConclusions中的R是程序规则结论。照用其时限、间隔、科室、检查、补查项目或目标值的段落须在cite中'
    '引用相应R，并绑定R所列触发记录；与R不一致或超出R的内容判unsupported_claim，缺少R引用时在修订段补上。'
    'R不能支持受检者的发现或测值，也不能写成原检查建议。',
    '章末小结须从本章已复核要点提炼优先判断或共同目标。它应告诉读者该先关心什么、为何或怎样复评，'
    '按question选择重点。逐行复述、单凭泛称下结论、忽略已核的重要矛盾或在小结引入新主张应修订。',
    '主张分别核实到本格实际F。外部引文核对同一人群、对象、检测、方向、研究端点和段落role。'
    '从文献不支持本例的段落移除错误引用及依赖它的临床结论，留下已有原始依据的内容。',
    '核对每一个独立受检者动作。原检查建议只按能授权该动作的原记录呈现，保留原对象、时限、条件与建议力度。'
    '新行动单独成段，不称原医生建议；只使用适用的行动证据。解释段中的机制引文不能支持动作。',
    '核对原范围、分档、条件、完整组、单位及公式输入。从同日面板其他项目不能获得当前测项的空腹等条件。'
    '不同研究阈值或其他专用设备的范围不能混用。来源冲突保留各自日期和性质。',
    '已注明采样、检查、回答或报告时间的结果分别写。未知回顾事件不得接成同一标本、同期化验或当前病程。'
    '正文相邻叙述也须让读者看见未确认的时间关系。复合题、家族成员、药物、行为、目标及否定的作用范围须可追溯。',
    '按sourceObservation和assayGroups核对每个面板、原测量层级及状态。真实阳性、阴性、来源评估、'
    '相对丰度、功能预测和临床诊断不可互替。预测合成低值不作人体营养缺乏或处方依据。',
    '通用临床护栏：阳性筛查结果和明显升高的肿瘤标志物被写成可以观察等待、被自述诱因解释或据此推断肿瘤，'
    '结节随访间隔超出影像分级和原检查建议，贫血直接给补充方案，或在妊娠、哺乳期套用不适用的目标与补充方向，'
    '均需修订。配方方向出现产品名、品牌或剂量判unsafe_advice。',
    '实际未提供与本批未展开分别处理。sourceContractIssues有具体错误时须修正完整输出，'
    '仅pass或修改role不构成修正。previouslyWritten和priorDraft都不是新证据。',
]

# Suggested items of an itemised cell, by cell label.
_OUTLINES = {
    '综合评估': ['总体判断', '优先事项', '共同管理重点'],
    '临床评估摘要': ['结论', '已明确的问题', '需进一步明确的问题', '随访观察的发现', '共同背景'],
    '肠道微生态评估摘要': ['结论', '整体结构', '菌群偏离', '功能特点', '与健康问题的关系'],
    '营养功能评估摘要': ['结论', '偏低的功能', '正常或偏高的功能', '膳食方向'],
    '领域评分说明': ['评分分布', '（分数低于80分的领域各一条，用领域名称作小标题）', '检查项目不全的领域', '评分含义'],
    '影响因素分析': ['现状', '关联的健康问题', '调整建议'],
    '影响因素小结': ['结论', '主要因素', '相互叠加', '优先调整', '不可改变的因素'],
    '管理建议': ['诊疗安排', '管理目标', '生活方式', '需及时就诊的情况'],
    '评估意见': ['性质与程度', '相关结果', '临床意义'],
    '菌群结构评估': ['主要发现', '相关影响', '管理方向'],
    '菌群偏离综合评估': ['结论', '（每条线索用其名称作小标题，两三条）', '需要处理的', '只需知晓的'],
    '短链脂肪酸评估': ['结论', '生理作用', '本次格局', '膳食关联'],
    '指标评估': ['指标含义', '本次结果', '评估意见'],
    '菌群相关风险': ['菌群特征', '临床对照', '评估意见', '需就诊的情况'],
    '风险评估': ['风险分层', '危险因素与保护因素', '需预防的结局', '监测指标与目标'],
    '领域综合评估': ['结论', '问题之间的关系', '共同因素', '处理顺序'],
    '营养功能对照与调整': ['与饮食习惯的对照', '与检查结果的对照', '膳食调整'],
    '氨基酸功能评估': ['结论', '偏低的氨基酸', '生理作用', '膳食来源'],
    '神经递质相关功能评估': ['结论', '代谢途径', '研究证据', '结合本人情况'],
    '功能组评估': ['功能格局', '健康关联', '食物调整'],
    '通路联动': ['通路联系', '薄弱环节', '干预方向'],
    '通路小结': ['结论', '（每条机制线用其名称作小标题，两三条）', '优先干预环节'],
    '随访安排': ['事项', '目的', '完成时间', '达标标准', '需提前就诊的情况'],
    '综合结论': ['优先处理', '持续管理', '复评依据'],
}


def _spec(target):
    """Job description, length range and outline of one template cell."""
    path = tuple(target['path'])
    section, field = path[0], path[-1]
    owner = target.get('owner') or {}
    six_chapter = owner.get('presentation') == 'assessment.v1'
    contract = target.get('cellContract') or {}
    if owner.get('contentPlanVersion') and owner.get('contentScopeCode') in {'overviewSummary', 'conclusionSummary'}:
        overview = owner['contentScopeCode'] == 'overviewSummary'
        return {
            'form': 'paragraph', 'label': '整体健康判断' if overview else '综合评估结论',
            'question': owner['templateDuty'],
            'guide': '用关键个人锚点解释为何优先处理这些问题及共同管理目标，'
                '不要罗列全部异常、拼接既有行动或用内部规则代替判断。',
            'min': 40, 'max': 240, 'careSummary': True, 'sourcePlanned': True,
        }
    if owner.get('contentPlanVersion') and owner.get('templateDuty'):
        # A real published row and an editorial summary follow the same
        # positive cell contract. Old compatibility-field heuristics must not
        # replace that contract after the planner has selected its sources.
        return {
            'form': 'paragraph',
            'label': contract['label'],
            'question': owner['templateDuty'],
            'guide': '完成本格的实际阅读职责，依据主对象及已复核关系形成判断、解释健康意义与本格负责的关注方向。',
            'min': 18 if contract.get('maxChars', 220) <= 110 else 24,
            'max': contract.get('maxChars', 220),
            'careSummary': True,
            'assessmentScope': _local_assessment_scope(target),
            'assessmentCode': owner.get('contentScopeCode'),
            'sourcePlanned': True,
        }
    if section == '_assessment_editorial' and owner.get('contentPlanVersion'):
        return {
            'form': 'paragraph',
            'label': contract['label'],
            'question': contract['question'],
            'guide': '直接完成这一单元格的阅读职责，先作有据判断，再解释意义；不写填表规则或无关事项。'
                     + (owner.get('editorialPurpose') or ''),
            'min': 24,
            'max': max(140, contract['maxChars']),
            'careSummary': True,
            'assessmentScope': _local_assessment_scope(target),
            'assessmentCode': path[1],
            'sourcePlanned': True,
        }
    if section == '_assessment_editorial':
        scope = _local_assessment_scope(target)
        guide = (
            '按question只交付模板对应的这一段或单元格。直接写判断和它对本人的意义；'
            '结论来自绑定的原始事实及适用证据。不要增设栏目、重抄列表、添加未检结论或抄写其他小节。'
        )
        if scope:
            guide += (
                '只保留本格成立的判断，省去先列无关事实或机制、再否定关联的句子。'
                '无跨项结论时写本项有据的局部判断。'
            )
        if scope == 'function':
            guide += (
                '根据表中分项和实际偏离方向概括菌群功能预测的特点。不给饮食份量或频次、疾病分层或就医建议。'
                '其他临床发现只有直接同向支持同一个对象时才写；牙科、酶学或其他部位的研究不适用。'
                '名称相同的微生物代谢通路及血液检验也不能仅凭相同升降认为互证。'
                '分数和逐项升降留给结果列。解读只写有依据的主要模式与意义，不自行把正常分数另划高低档。'
                '保留观察重点，具体生活安排集中于第六章。'
            )
        if path[1] == 'function.neurotransmitter':
            guide += '使用实际检测名称和合成通路。多巴胺、GABA、血清素是相关递质的名称，不把它们作为前体表述。'
        if scope == 'structure':
            guide += (
                'GMWI2原始数值与失调分度分别表示。B/E是双歧杆菌属与肠杆菌科的相对构成比值，'
                '只解释该比值及原参考比较，不推断个人定植抑制能力、黏膜屏障功能或感染风险。'
                'F/B比例、菌门相对占比与肠型不可互相替代。单个菌门的检出或偏离不能解释组合面板异常的成因。'
            )
        if path[1] == 'structure.gutAge':
            guide += '只说明肠龄差体现的是本次样本的相对菌群组成差异。省去模型流程、通用判读规则和空泛的随访提示。'
        if scope == 'organisms':
            guide += (
                '正常菌种及丰度由基线栏承担；同组没有异常时直接写本组状态。'
                '按本次测值解释个人发现，单次样本不与研究人群随病程进展的变化作同向或反向比较。'
                '有不同方向时据实际分项说明，原组合状态与分项分别陈述。'
                '不把特定疾病或特定干预人群的研究作为此人的意义；只剩实测组成时简洁概括该组成。'
            )
        if path[1] == 'clinicalRow.cardiometabolic.glycemia':
            guide += (
                '糖化血红蛋白判断先核对糖代谢筛查指南及适用人群，给出正确的筛查意义。'
                '注明所采用指南、定义框架及用途，区分筛查、特定框架组分与独立疾病诊断，不混用不同定义。'
            )
        if path[1].startswith('clinicalRow.organMetabolism.'):
            guide += 'FIB-4若有记录，仅用于判断进展期肝纤维化的筛查分层，注明对象，避免“肝脏状态正常”等泛化。'
        if path[1] == 'digestive.oralGastric':
            guide += '只写口腔检查明示的具体表现。一般阴性或未见异常的表述不能延伸成牙龈无红肿、出血等具体结果。'
        return {
            'form': 'paragraph',
            'label': contract['label'],
            'question': contract['question'],
            'guide': guide,
            'min': 24,
            'max': contract['maxChars'],
            'careSummary': True,
            'assessmentScope': scope,
            'assessmentCode': path[1],
        }
    if contract.get('allowedValues'):
        return {
            'form': 'choice',
            'label': contract.get('label'),
            'allowed': list(contract['allowedValues']),
            'question': (
                '这个习惯或背景与相关健康问题之间的关联，现有医学证据有多强？'
                if field == 'evidence_strength'
                else '这个因素通过本人的努力可以改变到什么程度？'
            ),
            'min': 1,
            'max': 1,
        }
    spec = None
    plain = False  # a short cell written as one paragraph, without item titles
    if section == 'health_overview':
        if 'key_findings' in path:
            plain = True
            spec = (
                '要点',
                '这个问题一句话的判断、最关键的一条依据和首要措施是什么？',
                '三句以内：判断（含性质）＋最关键的数值或发现＋首要措施及时限。',
                90,
                190,
            )
        elif 'summaries' in path:
            domain = path[2]
            spec = {
                'medical': (
                    '临床评估摘要',
                    '跨系统查看实际检查结果，哪些发现应一起解释，有什么临床意义？',
                    '说明已存在的问题、筛查阳性或需随访的发现各属于什么性质。'
                    '确有多项同向证据时解释它们共同指向什么。单项异常只写该项的临床意义。'
                    '不重复表格数值、成套处置建议或无关的正常项目。',
                    40 if six_chapter else 260,
                    200 if six_chapter else 440,
                ),
                'microbiome': (
                    '肠道微生态评估摘要',
                    '肠道微生态最主要的特点是什么，与本人的健康问题有什么关系？',
                    '概括多样性与整体指数、偏离菌群的特点、功能预测的格局；点明哪些特点与本人的代谢或肠道问题相呼应，哪些目前没有临床对应。不要写成生活建议清单。',
                    260,
                    440,
                ),
                'nutrition': (
                    '营养功能评估摘要',
                    '营养相关功能的整体格局是什么，对饮食意味着什么？',
                    '概括经核实的菌群功能特点。有相关膳食、症状或客观检查时，'
                    '提出实际饮食方向，不用预测分推断本人的营养水平。',
                    240,
                    420,
                ),
            }.get(domain)
        elif 'overall_score' in path:
            spec = (
                '领域评分说明',
                '各健康领域的评分为什么这样分布？',
                'row.dimensions列出了各领域的分数、等级和影响分数的因素。写出分数最低的两个领域的分数，并用已写明的具体健康问题说明原因（用问题名称，不罗列扣分数字）；分数较高但相关检查不全的领域，说明还缺少哪些针对性检查；最后说明评分反映的是各领域需要管理的程度。分数只用row.dimensions里的数值。',
                180,
                360,
            )
        else:
            plain = six_chapter
            spec = (
                '综合评估',
                '通读全部资料，整体判断是什么？',
                '最多三条，直接给整体判断和最先处理的问题。有共同管理环节时再补一条。'
                '单一问题用一条写清。省去正常清单、分项数值及处置细节。让读者一眼读懂当前首要问题。'
                '只有菌群筛查与生活自述时，沿对应检测结果和实际主诉给出管理判断，不写个人疾病分层。',
                35 if six_chapter else 520,
                180 if six_chapter else 860,
            )
    elif section == 'medical_assessment':
        secondary = owner.get('clinicalRole') == 'secondary_finding'
        if 'trigger_analysis' in path:
            plain = six_chapter
            spec = (
                '影响因素分析',
                '这个习惯或背景与哪些已发现的问题有关，最值得调整的是什么？',
                '只写一至两项与本人已有健康问题直接相关的习惯及其意义。表格已展示习惯记录，不复述。'
                '只给解释。执行频次、时长和目标量统一在第六章。没有饮酒量就不能用非每日饮酒判断总量少；'
                '睡眠时长接近推荐下限不能单独解释本人疲劳或已有疾病。' if six_chapter else
                '先写现状及关联，再给一项调整办法。',
                35 if six_chapter else 130,
                125 if six_chapter else 280,
            )
        elif 'trigger_conclusion' in path:
            spec = (
                '影响因素小结',
                '综合所有习惯和背景因素，从哪里改善最见效？',
                '指出哪两三个因素影响面最大、彼此如何叠加，以及从哪一个入手最容易见效；不可改变的因素（如家族史）说明它改变的是筛查强度。',
                220,
                420,
            )
        elif 'questionnaire_summary' in path:
            plain = True
            spec = (
                '主诉解读',
                '什么实际线索对解释这项不适或决定下一步有帮助？',
                '一至两句先写确有的同人线索，再给一个可观察的变化。'
                '最多引用三项会改变判断的记录，factIds只绑真正写到的依据。'
                '不用多项正常结果排除病因，不把生活背景写成已经证明的诱发条件。'
                '需要了解的症状细节写成可记录或问诊的事项，不列缺资料声明或未经适用确认的固定病程门槛。',
                35,
                160,
            )
        elif field == 'source_advice':
            plain = six_chapter or secondary
            spec = (
                '管理建议',
                '针对这个问题，具体怎么处理？',
                '给一项实际管理目的与下一步。医生已有的检查或随访建议按原意保留；'
                '未分型或无个人医嘱时不补固定周期或大小阈值，通用生活计划由第六章写。' if six_chapter else
                '写相应检查、就诊、随访或生活管理建议，保留原医生建议。',
                30 if six_chapter else 90,
                120 if six_chapter else 320,
            )
        else:
            plain = six_chapter or secondary
            spec = (
                '评估意见',
                '这个问题是什么性质、到了什么程度、放在本人全部资料里意味着什么？',
                '先写个人问题的性质与实际意义，区分确诊、筛查阳性待查、影像发现和症状。'
                '仅纳入会改变解释或优先级的相关事实。单项发现可以一两句解释。具体做法放在管理建议。'
                + ('这是次要发现，两三句说清性质和为什么只需常规随访即可。' if secondary else ''),
                45 if six_chapter else 70 if secondary else 170,
                200 if secondary else 400,
            )
    elif section == 'microbiome_assessment':
        if 'taxa' in path:
            plain = True
            spec = (
                '菌种评估',
                '这个菌是什么，本次丰度偏离说明什么？',
                '只写对本次检测有解释价值的菌群特征。有适用区间才解释偏离幅度。'
                '研究人群、疾病、检材或终点和本人情况无关时省去该研究结论。不要为一个菌推断个体病因。',
                25 if six_chapter else 100,
                150 if six_chapter else 260,
            )
        elif path[1] in {'structure_interpretation', 'key_microbial_signals', 'scfa_interpretation'}:
            spec = {
                'structure_interpretation': (
                    '菌群结构评估',
                    '各项结构指标分别说明什么？',
                    '只概括有实测值且影响判断的结构特征。两项确实不一致时说明其指标含义的区别。'
                    '缺少对应检测时略过菌门、肠型等主题。少量结果用一段说明，无相关临床发现时省去疾病名称。',
                    35 if six_chapter else 280,
                    180 if six_chapter else 500,
                ),
                'key_microbial_signals': (
                    '菌群偏离综合评估',
                    '偏离参考范围的菌合在一起看，指向什么？',
                    '把偏离的菌按功能或类群归并成线索（例如同一功能类群整体偏离、某类有益菌丰度为零、条件致病菌或毒力基因检出），逐条说明意义和可信程度，并指出哪些需要处理、哪些只需知晓。',
                    320,
                    560,
                ),
                'scfa_interpretation': (
                    '短链脂肪酸评估',
                    '短链脂肪酸合成能力的结果说明什么？',
                    '说明三种短链脂肪酸各自的生理作用、本次三项的格局，以及与产丁酸菌丰度、膳食纤维摄入和代谢健康的联系。',
                    200,
                    400,
                ),
            }[path[1]]
        elif 'antimicrobial_resistance' in path or field == 'resistance_notice':
            plain = True
            spec = (
                '耐药基因评估',
                '耐药基因的结果对用药有什么实际意义？',
                '说明这类结果反映肠道菌群携带的耐药基因丰度，个人感染时用药仍以药敏结果为准；本次结果处于什么水平；日常用药应注意什么。',
                90,
                220,
            )
        else:
            spec = (
                '指标评估',
                '这个指标衡量什么，本次结果说明什么？',
                '简述有实际价值的指标含义和本次发现。仅在有适用参考范围时比较。'
                '依据与本人情况匹配的研究和客观结果解释关联；结论正常或无相关线索时简要说明即可。'
                + ('组合评定与各分项独立陈述。组合有偏离时，写有代表性的分项所见，省去对组合偏离成因的推断。'
                   if six_chapter else ''),
                25 if six_chapter else 110,
                160 if six_chapter else 300,
            )
    elif section == 'disease_risk_assessment':
        if 'risk_ranking' in path:
            if owner.get('kind') == 'disease_source_model':
                spec = (
                    '菌群相关风险',
                    '肠道菌群中与这个疾病相关的特征是什么，本人有没有相应的临床表现？',
                    '先写相关的风险等级（有数值时写出）和涉及的菌及其变化方向；再写这类菌群特征与该病的研究关联有多强（引用）；然后写本人有无与该病相关的检查异常或症状，据此判断目前是否需要处理；最后写什么情况下需要就诊。本人没有相关表现时如实写明，不渲染风险。不要写“列为关联提示”“未标风险等级”一类说法。',
                    120,
                    280,
                )
            else:
                spec = (
                    '风险评估',
                    '从风险角度看，这个问题处在什么层级，需要预防的是什么？',
                    '已有适用且完整的指南分层条件时写具体分层。若仅有单次影像、筛查所见或无明确分型体系，'
                    '说明实际所见带来的管理意义。保留具体问题和证据；省去没有输入依据的操作阈值、固定周期和泛用排除。'
                    if six_chapter else
                    '不复述检查所见和“评估意见”里已有的描述，第一条直接给出分层结论及其依据的标准。依据指南的分层标准和本人具备的危险因素、保护因素，说明目前的风险层级；不干预时最需要预防的结局；把风险降下来要监测的指标和目标。',
                    150,
                    340,
                )
        elif field == 'attention_basis':
            return {
                'form': 'label',
                'label': '管理主题',
                'question': '用一个短语概括这个领域当前的管理重点。',
                'guide': '不超过12个汉字，名词性短语，具体到该领域的问题与处理，例如“某项异常的查因与管理”。',
                'min': 4,
                'max': 24,
            }
        elif 'overview_matrix' in path:
            return {
                'form': 'paragraph',
                'label': '领域结论',
                'question': '这个领域当前最主要的情况和安排，用一句话怎么说？',
                'guide': '一句话，不加title，包含该领域最主要的问题或“未见相关临床问题”的结论，以及对应安排。',
                'min': 30,
                'max': 110,
            }
        else:
            plain = six_chapter
            spec = (
                '领域综合评估',
                '综合这个领域的全部项目，整体情况和处理顺序是什么？',
                '一至两句总结本领域已经确认的主要个人发现和处理顺序。选与处理有关的、已有评估的事实。'
                '其中只有一次检查或一个主要问题时，一句即可。不同资料的发生时间或关联未经核实就分别陈述，'
                '不要补病例时间、检查场景或因果。' if six_chapter else
                '先给这个领域的总体结论；再写各健康问题之间的关系（谁影响谁、轻重缓急）和共同的危险因素；最后排出处理顺序。各健康问题的检查结果、评估和处理细节在报告里紧随其后逐项列出，这里不逐项复述，说明关系时点到问题名称和最关键的一个数值即可。只有菌群相关特征而没有健康问题时，写明这一点，并给出日常预防和需要就诊的情况，两三条即可。用领域名称指代（如“肠道方面”），不写“本类”。',
                28 if six_chapter else 200,
                150 if six_chapter else 420,
            )
    elif section == 'nutrition_assessment':
        if path[1] in {
            'amino_metabolite_interpretation',
            'neurotransmitter_interpretation',
            'nutrition_interpretation',
        }:
            spec = {
                'nutrition_interpretation': (
                    '营养功能对照与调整',
                    '营养功能的格局与本人的饮食习惯和检查结果是否对得上，膳食上怎样调整？',
                    '以记录在案的饮食习惯、耐受和临床问题为依据安排调整，具体到食物种类或频次。'
                    '只有可比的结果和适用证据支持相互联系时才写功能对照，不将通路预测写成实际摄入或脂质异常的原因。',
                    280,
                    500,
                ),
                'amino_metabolite_interpretation': (
                    '氨基酸功能评估',
                    '氨基酸合成功能的格局说明什么？',
                    '指出菌群功能预测中有意义的偏离。结合已有膳食或临床发现给饮食建议，'
                    '不得把通路信号当作蛋白质摄入不足或补充依据。',
                    220,
                    420,
                ),
                'neurotransmitter_interpretation': (
                    '神经递质相关功能评估',
                    '神经递质合成相关功能偏低说明什么？',
                    '说明肠道菌群参与这些递质或其前体代谢的途径、这类预测与情绪睡眠研究的关系及其证据强度，结合本人的疲劳和睡眠情况给出解读。',
                    220,
                    420,
                ),
            }[path[1]]
        else:
            spec = (
                '功能组评估',
                '这一组功能分数的格局说明什么，饮食上对应怎么做？',
                '本组哪些项偏离、偏离到什么分数，整体格局；这类菌群功能与健康的关系（引用），与本人的饮食习惯和检查结果是呼应还是不一致；与本组直接相关的食物调整，具体到食物和频次。',
                160,
                360,
            )
    elif section == 'functional_pathways':
        if field == 'health_meaning':
            spec = (
                '通路联动',
                '这条通路与其他通路、与本人的健康问题之间是什么关系？',
                '有个人资料和适用证据支持才说明通路与本人的健康问题如何关联以及处理方向。'
                '单有预测结果只解释其直接意义。避免反推本人最薄弱的环节，不复述分数。',
                100,
                270,
            )
        else:
            spec = (
                '通路小结',
                '十条通路合起来，指向哪几个可以干预的环节？',
                '把通路归并成两三条与本人健康问题相关的机制线（如能量与脂质代谢线、肠屏障与炎症线、肠—脑轴线），每条线写清证据强弱和对应的干预环节。',
                320,
                540,
            )
    elif 'priority_directions' in path:
        if field == 'basis':
            return {
                'form': 'label',
                'label': '主要依据',
                'question': '判断这个问题需要管理的最关键的一两项结果是什么？',
                'guide': '一个短语，不超过30个字，写最关键的检查结果及实际数值和单位。不写判断和建议，不写句号。',
                'min': 6,
                'max': 44,
                'reviewed': True,
            }
        return {
            'form': 'label',
            'label': '首要措施',
            'question': '这个问题第一步要做什么、多长时间内完成、去哪个科室，之后怎样复查？',
            'guide': (
                '严格按“时限；首要事项；科室；后续复查”四段书写，用全角分号隔开，每段不超过16'
                '个字，不写句号。四段分别填写完成期限、首要事项、对应科室、后续复查的项目与间隔或'
                '触发条件，不复制格式示例为临床安排。没有对应科室时写“健康管理”，没有固定间隔时'
                '写“按复查结果确定”。各段以本人的实际问题及已核实的安排为准，与该问题“随访安排'
                '”的内容保持一致。'
            ),
            'min': 10,
            'max': 64,
            'reviewed': True,
            'segments': 4,
        }
    else:
        if 'next_steps' in path or field == 'followup_action':
            plain = six_chapter
            spec = (
                '随访安排',
                '这件事接下来具体做什么、为了什么、什么时候做？',
                '写一两句供受检者直接执行的下一步及目的，需要时另给一句真实警示情境。'
                '不要写“随访事项、完成时间、达标、取得检查结果”等工作流标题或过程。'
                '偶发及已报告正常的设备分项只写与实际风险相称的安排，无相关症状或独立证据不自动指向专科与新影像。'
                '仅在原医嘱或适用指南明确时写随访间隔，不把定期复查换成每年。'
                '不写通用饮食、运动频次、饮水增量或补充剂；生活方式统一由第六章专项计划管理。' if six_chapter else
                '写可执行的检查、就诊、复查或生活管理安排及其目标。',
                30 if six_chapter else 170,
                135 if six_chapter else 340,
            )
        else:
            spec = (
                '综合结论',
                '把前文的判断收束为接下来一段时间的健康管理方案。',
                '只收束为已审定的执行先后顺序和复评依据。已有行动表承接科室、时间、频次和具体检查，'
                '这里只给总的处理目的及条件。资料仅支持一个重点时使用一条，无需例行补上饮食、运动和睡眠。',
                35 if six_chapter else 460,
                200 if six_chapter else 720,
            )
    if spec is None:
        spec = (contract.get('label') or '评估', contract.get('question') or '请评估本项。', '', 100, 300)
    label, question, guide, low, high = spec
    result = {'form': 'paragraph', 'label': label, 'question': question, 'guide': guide, 'min': low, 'max': high}
    if six_chapter and label in _CARE_SUMMARIES:
        result['careSummary'] = True
    if not plain and label in _OUTLINES and not (six_chapter and label == '指标评估'):
        result['outline'] = _OUTLINES[label]
    return result


def _plain(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json', by_alias=False)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _clean_row(value):
    """Read-only view of a result row without prose, citations or provenance."""
    if isinstance(value, list):
        return [_clean_row(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        key: _clean_row(item)
        for key, item in value.items()
        if key not in _PROSE | _META and item not in (None, '', [], {})
    }


def _row(blueprint, target):
    node = blueprint.data
    path = list(target['path'][:-1])
    try:
        for part in path:
            node = node[part] if isinstance(part, int) else getattr(node, part)
        row = _clean_row(_plain(node))
        if isinstance(row, dict) and not row and len(path) > 1:
            node = blueprint.data
            for part in path[:-1]:
                node = node[part] if isinstance(part, int) else getattr(node, part)
            row = _clean_row(_plain(node))
    except (AttributeError, IndexError, KeyError, TypeError):
        row = {}
    owner = target.get('owner') or {}
    if isinstance(row, dict):
        if owner.get('originalAdvice'):
            row['originalAdvice'] = list(owner['originalAdvice'])
        if owner.get('certainty'):
            row.setdefault('certainty', owner['certainty'])
    return row


def _score_rows(blueprint):
    """Published dimension scores with their reviewed deduction breakdown."""
    chart = getattr(getattr(blueprint.data, 'health_overview', None), 'assessment_chart', None) or ()
    rows = []
    for item in chart:
        if getattr(item, 'score', None) is None:
            continue
        row = {'name': item.name, 'score': item.score}
        if getattr(item, 'assessment_level', None):
            row['level'] = item.assessment_level
        if getattr(item, 'explanation', None):
            row['basis'] = item.explanation
        rows.append(row)
    return rows


def _fact_view(fact, *, projection_index=None, reported_role=None, temporal_context=None):
    from .evaluation_derived_indices import derived_index_context

    row = {key: fact.get(key) for key in ('name', 'value', 'unit', 'status', 'date') if fact.get(key) not in (None, '')}
    if fact.get('kind') == 'clinical':
        from .clinical_source_projection import get_patient_projection
        from .clinical_source_projection import patient_projection_context
        from .clinical_source_projection import patient_reference_context

        row['referenceContext'] = patient_reference_context(fact)
        projection = get_patient_projection(fact, projection_index=projection_index)
        if projection is not None:
            row.pop('value', None)
            if projection.get('patientValue') not in (None, ''):
                row['value'] = projection['patientValue']
            row['clinicalSourceProjection'] = patient_projection_context(projection)
    if fact.get('kind') == 'self_reported':
        from .clinical_source_projection import reported_temporal_context

        temporal = temporal_context or reported_temporal_context(fact, reported_role=reported_role)
        if temporal:
            row['reportedTemporalContext'] = deepcopy(temporal)
        reported_at = (temporal or {}).get('reportedAt') or row.pop('date', None)
        row.pop('date', None)
        if reported_at is not None:
            row['reportedAt'] = reported_at
    if fact.get('kind') == 'self_reported' and re.search(r'饮水|喝水', str(fact.get('name') or '')) and re.search(
        r'\d\s*(?:毫|m)\s*$', str(fact.get('value') or ''), re.I,
    ):
        row['value'] = '饮水量未确认'
    reference = fact.get('referenceRange', fact.get('reference_range'))
    if reference:
        row['reference'] = str(reference)
    if derivation := derived_index_context(fact):
        row['derivedIndex'] = derivation
    return row


_UNREMARKABLE = frozenset({'正常', '未见异常', '未见明显异常', '阴性', '未检出', '正常范围'})

_ABSENCE_CLAIM = re.compile(r'未检出|低于(?:检出|检测)限|缺失|\babsen[ct]\w*\b|\bnot detected\b', re.I)
_NO_ABNORMALITY = re.compile(r'未检出(?:任何|明显|丰度)?异常|未见(?:明显)?异常')
_NORMAL_CLAIM = re.compile(r'正常(?!参考|范围|区间|值|上限|下限|分档|判定标准)'
                           r'|(?:参考)?(?:范围|区间)内|未见.{0,4}异常')
_CLAIM_BOUNDARY = re.compile(
    r'(?:不能|不得|不应|不宜|不可|不等于|不代表|不意味着|并非|不是|未能|尚未|无证据|没有证据|未提示|无法)'
    r'[^。；，,]{0,26}$'
)
_REFERRAL_SEQUENCE = re.compile(
    r'先[^。；]{0,24}(?:复查|复测|检查)[^。；]{0,24}(?:再|然后|之后|后才|才)'
    r'[^。；]{0,24}(?:就诊|就医|随诊|专科咨询)'
    r'|(?:复查|复测)[^。；]{0,12}(?:仍|持续)[^。；]{0,8}(?:阳性|异常)'
    r'[^。；]{0,12}(?:才|再)[^。；]{0,12}(?:就诊|就医|随诊)'
)


def _claim_clauses(text, *, join_commas=False):
    boundary = r'[。！？；;\n]|但是|然而|但' if join_commas else r'[。！？；;\n，,]|但是|然而|但'
    return [part.strip() for part in re.split(boundary, text) if part.strip()]


def _positive_claims(text, pattern, *, join_commas=False):
    for clause in _claim_clauses(text, join_commas=join_commas):
        for match in pattern.finditer(clause):
            if not _CLAIM_BOUNDARY.search(clause[:match.end()]):
                yield clause, match


def _source_handles(value, dossier):
    """Map source identities only; observation IDs and literal spans stay exact."""
    if isinstance(value, list):
        return [_source_handles(item, dossier) for item in value]
    if not isinstance(value, dict):
        return deepcopy(value)
    result = {}
    for key, item in value.items():
        if key == 'recordId' and isinstance(item, str):
            result[key] = dossier.handles.get(item, item)
        elif key in {'recordIds', 'sourceFactIds', 'relatedRecordIds', 'adviceRecordIds'} and isinstance(item, list):
            result[key] = [dossier.handles.get(fid, fid) for fid in item
                           if fid in dossier.handles or fid in dossier.reverse]
        else:
            result[key] = _source_handles(item, dossier)
    return result


def _source_day(value):
    match = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?:\D|$)', str(value or ''))
    return f'{int(match[1]):04d}-{int(match[2]):02d}-{int(match[3]):02d}' if match else None


def _date_is_in_quote(day, quote, comparison_days=()):
    year, month, date = (int(part) for part in day.split('-'))
    normalized = unicodedata.normalize('NFKC', quote)
    if re.search(rf'{year}[-/]0?{month}[-/]0?{date}(?!\d)', normalized) or re.search(
        rf'{year}年\s*0?{month}月\s*0?{date}日', normalized,
    ):
        return True
    if any(other != day and other[:7] == day[:7] for other in comparison_days):
        return False
    month_words = ('一', '二', '三', '四', '五', '六', '七', '八', '九', '十', '十一', '十二')
    month_term = r'(?<![0-9一二三四五六七八九十])' + (
        rf'(?:0?{month}|{month_words[month - 1]})月' if 1 <= month <= 12 else rf'0?{month}月'
    )
    if re.search(rf'{year}年\s*{month_term}', normalized):
        return True
    return bool(comparison_days and all(other[:4] == day[:4] for other in comparison_days)
                and re.search(month_term, normalized))


def _behavior_scopes(ids, dossier, selections=()):
    scopes = []
    for fid in ids:
        fact = dossier.facts[fid]
        temporal = dossier.reported_temporal.get(fid) or {}
        role = dossier.reported_roles.get(fid) or {}
        if fact.get('kind') == 'self_reported' and fid in dossier.behavior_fact_ids and role.get('role') not in {
            'reported_symptom', 'reported_history', 'qualified_history_context',
        } and temporal.get('kind') not in {'management_goal', 'reported_history'}:
            scopes.append({
                'recordId': dossier.handles[fid], 'name': fact.get('name'), 'value': fact.get('value'),
                'sourceTime': temporal.get('reportedAt') or fact.get('date'),
                'timeRole': 'reported_at_not_event_date', 'scopeQuote': temporal.get('scopeQuote'),
                'temporalContext': deepcopy(temporal),
            })
        projection = dossier.clinical_projections.get(fid) or {}
        rows = projection.get('patientRows', ())
        selected_quotes = [item['quote'] for item in selections
                           if item.get('recordId') == dossier.handles[fid] and item.get('quote')]
        if selected_quotes:
            from .clinical_source_projection import get_patient_rows

            rows = get_patient_rows(fact, projection_index=dossier.clinical_projections,
                                    source_quotes=selected_quotes)
        for row in rows:
            if row.get('role') != 'reported_behavior':
                continue
            scopes.append({
                'recordId': dossier.handles[fid], 'name': row.get('name'), 'value': row.get('value'),
                'sourceTime': row.get('observedAt') or (projection.get('sourceContext') or {}).get('examDate'),
                'timeRole': 'recorded_behavior_at_source_exam',
                'scopeQuotes': [span['quote'] for span in row.get('sourceSpans', ()) if span.get('quote')],
            })
    return scopes


def _reference_groups(ids, dossier):
    selected = set(ids)
    groups = []
    for group in dossier.clinical_observation_groups:
        member_ids = set(group.get('recordIds', ()))
        member_ids.update(rid for member in group.get('members', ()) for rid in member.get('recordIds', ()))
        if selected.intersection(member_ids):
            groups.append(_source_handles(group, dossier))
    return groups


def _literal_object_labels(name):
    if not isinstance(name, str) or not name:
        return []
    return list(dict.fromkeys([name, *re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9.-]+', name)]))


def _mentions_object(text, names):
    compact = unicodedata.normalize('NFKC', text).casefold()
    return any(unicodedata.normalize('NFKC', label).casefold() in compact
               for name in names for label in _literal_object_labels(name))


def _measurement_contract(dossier, fid):
    fact = dossier.facts[fid]
    semantics = dossier.microbiome_semantics.get(fid, ())
    semantics = [semantics] if isinstance(semantics, dict) else list(semantics)
    if fid in dossier.gene_source_semantics:
        semantics.append(dossier.gene_source_semantics[fid])
    relative = any('relative_abundance' in str(item.get('measurementType') or item.get('kind') or '')
                   for item in semantics)
    relative = relative or fact.get('kind') == 'microbe' and fact.get('unit') in {'%', '％'}
    panel = any(item.get('objectScope') == 'panel' for item in semantics)
    states = {str(item.get('stateMeaning') or '') for item in semantics}
    source_state = str(fact.get('status') or fact.get('sourceStatus') or '').strip()
    source_value = str(fact.get('value') if fact.get('value') is not None else '').strip()
    no_abnormality = 'absence_of_abundance_abnormality' in states or bool(
        _NO_ABNORMALITY.fullmatch(source_state) or _NO_ABNORMALITY.fullmatch(source_value)
    )
    no_abnormality = no_abnormality or relative and panel and source_state in _UNREMARKABLE
    explicit_negative = bool(states.intersection({'target_not_detected', 'sequence_not_detected'}))
    explicit_negative = explicit_negative or not (relative and panel or no_abnormality) and bool(
        re.fullmatch(r'未检出|阴性|not_detected|negative|not detected', source_state or source_value, re.I)
    )
    paths = [path for item in semantics for path in [item.get('sourcePanel'), *item.get('sourcePanels', ())] if path]
    paths.extend(assay.get('sourcePath', '') for item in semantics for assay in item.get('assays', ()))
    ranks = sorted({match[1].casefold() for path in paths
                    if (match := re.search(r'_(PHYLUM|CLASS|ORDER|FAMILY|GENUS|SPECIES)(?:$|\[)', path, re.I))})
    source_observation = dossier.microbiome_observations.get(fid) or {}
    typed_observations = source_observation.get('observations') or [source_observation]
    return {
        'recordId': dossier.handles[fid],
        'object': (dossier.microbiome_observations.get(fid) or {}).get('label')
                  or dossier.gene_fact_names.get(fid, fact.get('name')),
        'objectLabels': list(dict.fromkeys(label for label in [
            (dossier.microbiome_observations.get(fid) or {}).get('label'), fact.get('name'),
            *(item.get('targetName') for item in semantics),
            *(item.get('measurandId') for item in semantics),
        ] if isinstance(label, str) and label)),
        'sourceIdentityLabels': list(dict.fromkeys(label for label in [
            fact.get('name'), *(item.get('targetName') for item in semantics),
            *(item.get('measurandId') for item in semantics),
        ] if isinstance(label, str) and label)),
        'candidateGenes': sorted({str(gene) for item in semantics for gene in item.get('candidateGenes', ())}),
        'measurementTypes': sorted({str(item.get('measurementType') or item.get('kind'))
                                    for item in semantics if item.get('measurementType') or item.get('kind')}),
        'relativeAbundance': relative,
        'zeroAbundance': relative and bool(re.fullmatch(r'0+(?:\.0+)?\s*[%％]?', source_value)),
        'explicitNegativeDetection': explicit_negative,
        'noAbundanceAbnormality': no_abnormality,
        'referenceAvailable': bool(fact.get('referenceRange') or fact.get('reference_range') or any(
            row.get('referenceRange') for row in (dossier.clinical_projections.get(fid) or {}).get('patientRows', ())
        )),
        'sourceState': source_state or None, 'taxonomicRanks': ranks,
        'comparisonBases': sorted({str(item['comparisonBasis']) for item in semantics if item.get('comparisonBasis')}),
        'assayIds': sorted({aid for item in semantics for aid in item.get('assayIds', ())}),
        'typedMicrobialRecord': bool(semantics) or fact.get('kind') in {'microbe', 'function'},
        'measurementMeanings': sorted({str(item['measurementMeaning']) for item in typed_observations
                                       if item.get('measurementMeaning')}),
        'kind': fact.get('kind'),
    }


def _planned_source_contract(target, cell, dossier):
    owner = target.get('owner') or {}
    ids = [fid for fid in target.get('sourceFactIds', ()) if fid in dossier.facts]
    measurements = [_measurement_contract(dossier, fid) for fid in ids]
    unknown_events = []
    established_events = [relation for relation in cell.get('temporalRelations', ())
                          if relation.get('eventLinkEstablished') is True and relation.get('eventId')]
    for fid in ids:
        temporal = dossier.reported_temporal.get(fid) or {}
        if temporal.get('kind') == 'retrospective_event':
            handle = dossier.handles[fid]
            links = [relation for relation in established_events if relation.get('recordId') == handle]
            established_ids = {rid for relation in links for rid in relation.get('relatedRecordIds', ())}
            related_ids = [dossier.handles[other] for other in ids
                           if dossier.facts[other].get('kind') == 'clinical'
                           and dossier.handles[other] not in established_ids]
            if links and not related_ids:
                continue
            unknown_events.append({
                'recordId': handle, 'relatedRecordIds': related_ids, 'scopeQuote': temporal.get('scopeQuote'),
                'reportedAt': temporal.get('reportedAt'),
                'eventDate': temporal.get('eventDate'), 'eventId': temporal.get('eventId'),
                'eventLinkEstablished': False,
                'clinicalCooccurrenceConfirmed': temporal.get('clinicalCooccurrenceConfirmed', False),
            })
    actions = [*cell.get('sourceClinicalActions', ()), *((cell.get('actionContext') or {}).get('originalAdvice') or ())]
    preserve_advice = cell.get('noNewPrerequisite') is True or any(
        action.get('noNewPrerequisite') is True for action in actions
    )
    action_context = cell.get('actionContext') or {}
    constraints = action_context.get('clinicalConstraintFactIds') or []
    activity_groups = action_context.get('activityConstraintGroups') or []
    reference_groups = _reference_groups(ids, dossier)
    code = str(owner.get('contentScopeCode') or '')
    behavior_ids = [fid for fid in ids if fid in dossier.behavior_fact_ids or any(
        row.get('role') == 'reported_behavior'
        for row in (dossier.clinical_projections.get(fid) or {}).get('patientRows', ())
    )]
    if cell.get('sourceSelections'):
        behavior_ids.extend(dossier.reverse[item['recordId']] for item in cell['sourceSelections']
                            if item.get('recordId') in dossier.reverse)
    behavior_scopes = _behavior_scopes(list(dict.fromkeys(behavior_ids)), dossier, cell.get('sourceSelections', ()))
    behavior_dates = {}
    for scope in behavior_scopes:
        if day := _source_day(scope.get('sourceTime')):
            behavior_dates.setdefault(day, []).append(scope['recordId'])
    obligations = [
        {'code': 'event:' + row['recordId'], 'kind': 'independent_events',
         'recordIds': [row['recordId']], 'relatedRecordIds': row['relatedRecordIds'],
         'duty': '同段使用时保留原回顾条件，并明确事件与临床记录的时间关联未确立，不能仅声明因果未知。'}
        for row in unknown_events if row['relatedRecordIds']
    ]
    obligations.extend(
        {'code': 'behavior:' + rid, 'kind': 'reported_behavior_scope', 'recordIds': [rid],
         'duty': '只说明实际题目/原行为片段的对象、时点与范围；本人某行为否认不覆盖未问及的其他暴露途径。'}
        for rid in dict.fromkeys(row['recordId'] for row in behavior_scopes)
    )
    if len(behavior_dates) > 1:
        obligations.append({
            'code': 'behavior-times', 'kind': 'separate_behavior_times',
            'recordIds': list(dict.fromkeys(rid for group in behavior_dates.values() for rid in group)),
            'sourceTimeGroups': [{'sourceDate': day, 'recordIds': list(dict.fromkeys(group))}
                                 for day, group in behavior_dates.items()],
            'duty': '不同来源日期的行为/主观评价分别陈述，不合成为同一当前状态或彼此证明。',
        })
    if reference_groups:
        obligations.append({
            'code': 'reference-claims', 'kind': 'reference_coverage',
            'recordIds': list(dict.fromkeys(rid for group in reference_groups
                                           for rid in group.get('recordIds', ()))),
            'duty': '按正文中的每个临床正常/范围命题返回一条回执，groupCode和observationIds指向实际对象；'
                    '同一组可有多个命题。只需说明实际命题，不逐个证明未作正常命题的临床背景组。'
                    '正常命题全属微生态等其他对象时，用no_group_range_claim引用实际临床背景句。'
                    '区分原状态、打印区间和原文所见；部分正常不能支持整组正常。',
        })
    obligations.extend(
        {'code': 'activity:' + group['code'], 'kind': 'activity_constraints',
         'recordIds': group.get('recordIds', []), 'adviceRecordIds': group.get('adviceRecordIds', []),
         'duty': group.get('purpose'), 'phaseResponsibilities': group.get('phaseResponsibilities', [])}
        for group in activity_groups
    )
    required = ['readerDutyAndPositiveInterpretation', 'referenceClaimsSupported',
                'comparisonScopesPreserved', 'claimPolarityPreserved']
    if any(row['typedMicrobialRecord'] for row in measurements):
        required.extend(['typedMeasurementMeaningPreserved', 'assayAndTaxonomicScopesPreserved',
                         'aggregationAndIntervalMeaningPreserved'])
    if preserve_advice:
        required.append('noNewPrerequisite')
    if unknown_events or any(row.get('eventLinkEstablished') is False for row in cell.get('temporalRelations', ())):
        required.append('unknownEventNotJoined')
    if behavior_scopes:
        required.append('behaviorTimeAndExposureScopesPreserved')
    if constraints or activity_groups:
        required.append('clinicalConstraintsInProgression')
    if code.startswith(('function.', 'clinicalRow.', 'structure.', 'organisms.', 'lifestyle.')):
        reading_duty = '解释本格实际模式、健康意义及有据联系，执行频次、剂量和完整起步方案由管理格承担。'
    elif code.startswith('layer.'):
        reading_duty = '归纳本层独有的目标、处理顺序和复评依据，具体执行动作交相应管理格承担。'
    elif code.startswith('management.'):
        reading_duty = '将真实基线、适用临床条件和已有目标转为本方向独有的起步、递进和观察安排。'
    else:
        reading_duty = cell.get('templateDuty') or cell.get('question')
    return {
        'requiredChecks': required,
        'measurementClaims': {
            'typedMeasurementGroups': [
                {
                    'meaning': meaning,
                    'primaryRecordIds': [row['recordId'] for row in measurements
                                         if meaning in row['measurementMeanings']
                                         and row['recordId'] in cell.get('recordIds', ())],
                    'contextRecordIds': [row['recordId'] for row in measurements
                                         if meaning in row['measurementMeanings']
                                         and row['recordId'] in cell.get('contextRecordIds', ())],
                }
                for meaning in sorted({meaning for row in measurements for meaning in row['measurementMeanings']})
            ],
            'relativeAbundanceRecordIds': [row['recordId'] for row in measurements if row['relativeAbundance']],
            'zeroAbundanceRecordIds': [row['recordId'] for row in measurements if row['zeroAbundance']],
            'explicitNegativeDetectionRecordIds': [row['recordId'] for row in measurements
                                                   if row['explicitNegativeDetection']],
            'noAbundanceAbnormalityRecordIds': [row['recordId'] for row in measurements
                                                if row['noAbundanceAbnormality']],
            'referenceUnstatedRecordIds': [row['recordId'] for row in measurements
                                           if row['kind'] == 'clinical' and not row['referenceAvailable']],
            'taxonomicRankGroups': {
                rank: [row['recordId'] for row in measurements if rank in row['taxonomicRanks']]
                for rank in sorted({rank for row in measurements for rank in row['taxonomicRanks']})
            },
            'clinicalReferenceClaims': [
                {'recordId': dossier.handles[fid], **deepcopy(dossier.reference_contexts[fid])}
                for fid in ids if fid in dossier.reference_contexts
            ],
        },
        'unknownEvents': unknown_events, 'establishedEventRelations': established_events,
        'behaviorScopes': behavior_scopes, 'clinicalObservationGroups': reference_groups,
        'nonClinicalObjects': [{'recordId': row['recordId'], 'labels': row['objectLabels']}
                               for row in measurements if row['kind'] != 'clinical'],
        'reviewObligations': obligations,
        'readingDuty': reading_duty,
        'measurementMeaning': {
            'relativeAbundance': '用原相对测值与分档解释样本构成；零值保留为零丰度。',
            'negativeDetection': '明确阴性属于原检测靶标与方法；无异常属于该面板比较状态。',
            'comparison': '整体判定、分项测值、构成图谱和靶标面板各用自己的比较基准解释，可同时成立。',
            'sourceConflict': '同对象、同方法或同一测量、同比较基准且相反断言才构成待核来源冲突。',
            'aggregation': '组合判定与具体成员并列解释各自意义；成员的作用不等于未知总评算法中的贡献。',
            'referenceEndpoint': '原状态和原区间分别保留；严格大于/小于、含等号与重叠端点按源算符理解。',
            'assayScope': '同类生物在靶标面板与独立构成图谱中分别陈述，各自正常/偏离可同时成立。',
        },
        'careAdvice': {'preserveDirectnessAndSourceConditions': preserve_advice,
                      'newPrerequisiteAllowed': False if preserve_advice else None},
        'activity': {'constraintRecordIds': constraints, 'constraintGroups': activity_groups,
                     'phases': (cell.get('actionContext') or {}).get('phaseResponsibilities') or [],
                     'duty': '医学复核选出真正影响活动的既有发现及原医嘱，并核实其如何约束起步、递进与长期目标。'},
        'reviewDuty': '各check评估最终交付或完整修订后的文字。事实正确而本格解释不足时，补足有据的定义、'
                      '生理作用、模式或定性支持；证据筛选过程和转交其他章节不是本格解释。',
    }


def _source_review_obligations(cell, paragraphs):
    """Only actual use activates a scope obligation; no source IDs are added."""
    contract = cell.get('sourceContract') or {}
    used = {rid for part in paragraphs for rid in part.get('factIds', ())}
    required = []
    for obligation in contract.get('reviewObligations', ()):
        own = set(obligation.get('recordIds', ()))
        if not own.intersection(used):
            continue
        kind = obligation['kind']
        if kind == 'independent_events' and not any(
            own.intersection(part.get('factIds', ()))
            and set(obligation['relatedRecordIds']).intersection(part.get('factIds', ())) for part in paragraphs
        ):
            continue
        if kind == 'separate_behavior_times' and sum(
            bool(set(group['recordIds']).intersection(used)) for group in obligation['sourceTimeGroups']
        ) < 2:
            continue
        if kind == 'reference_coverage' and not any(
            own.intersection(part.get('factIds', ()))
            and any(_positive_claims(part['text'], _NORMAL_CLAIM)) for part in paragraphs
        ):
            continue
        required.append(obligation)
    return required


def _source_review_issue(cell, paragraphs, findings):
    """Validate review receipts against sources, not clinical prose synonyms.

    The independent medical reviewer binds each claim to actual observations.
    A source group can support several claims and can coexist with a normal
    claim about another group. Program checks cover receipt identity, source
    authority and full-group coverage; semantic scope is a review duty.
    """
    obligations = _source_review_obligations(cell, paragraphs)
    if not obligations:
        return None
    if not isinstance(findings, list):
        return '医学复核须按实际sourceContract义务返回sourceContractFindings，不能只返回全true。'
    indexed = {}
    for row in findings:
        if not isinstance(row, dict) or not isinstance(row.get('code'), str):
            return 'sourceContractFindings结构不完整。'
        indexed.setdefault(row['code'], []).append(row)
    allowed_outcomes = {
        'independent_events': {'independent_time_scopes'},
        'reported_behavior_scope': {'recorded_facet_only'},
        'separate_behavior_times': {'separate_source_times'},
        'reference_coverage': {'no_group_range_claim', 'named_observations_in_range',
                               'all_group_observations_in_range', 'source_status_only', 'source_finding_only'},
        'activity_constraints': {'applied_to_phases', 'no_additional_restriction_supported'},
    }
    contract = cell['sourceContract']
    claims = [(obligation, row) for obligation in obligations
              for row in indexed.get(obligation['code'], [{}])]
    for obligation, row in claims:
        if row.get('outcome') not in allowed_outcomes[obligation['kind']]:
            return '医学复核未完成来源作用域义务：' + obligation['code']
        quote = row.get('quote')
        cited_ids = row.get('recordIds')
        matching = [part for part in paragraphs if isinstance(quote, str) and quote and quote in part['text']]
        used = {rid for part in matching for rid in part.get('factIds', ())}
        if not matching or not isinstance(cited_ids, list) or not cited_ids \
                or any(not isinstance(rid, str) for rid in cited_ids) or not set(cited_ids).issubset(used):
            return '来源作用域复核须引用最终正文连续原句及该句实际used recordIds：' + obligation['code']
        if not set(cited_ids).intersection(obligation['recordIds']):
            return '作用域复核借用了其他对象的来源：' + obligation['code']
        if obligation['kind'] == 'independent_events' and not set(cited_ids).intersection(
            obligation['relatedRecordIds'],
        ):
            return '时间独立性复核须同时核对自述与实际临床对象：' + obligation['code']
        if obligation['kind'] == 'reported_behavior_scope' and any(_positive_claims(
            quote, re.compile(r'(?:无|没有|不存在|未有|未见)[^。；，,]{0,20}(?:暴露|接触)'),
        )):
            source_scopes = [scope for scope in contract['behaviorScopes']
                             if scope['recordId'] in obligation['recordIds']]
            if not any(re.search(r'暴露|接触', str(scope.get('name') or '') + str(scope.get('scopeQuote') or '')
                                 + ' '.join(scope.get('scopeQuotes', ()))) for scope in source_scopes):
                return '本人所问行为的否认不能升级为所有相关暴露/接触均无；保留原facet对象范围。'
        if obligation['kind'] == 'separate_behavior_times':
            time_quotes = row.get('sourceTimeQuotes')
            if not isinstance(time_quotes, list):
                return '跨时点行为复核须逐时点返回sourceTimeQuotes，不能只返回true或同一现状句。'
            groups = [group for group in obligation['sourceTimeGroups']
                      if set(group['recordIds']).intersection(
                          {rid for part in paragraphs for rid in part.get('factIds', ())},
                      )]
            days = sorted(group['sourceDate'] for group in groups)
            for group in groups:
                statements = [item for item in time_quotes if isinstance(item, dict)
                              and item.get('sourceDate') == group['sourceDate']
                              and isinstance(item.get('recordIds'), list)
                              and all(isinstance(rid, str) for rid in item['recordIds'])
                              and set(item.get('recordIds', ())).intersection(group['recordIds'])]
                supported = False
                for statement in statements:
                    scope_quote = statement.get('quote')
                    matching_parts = [part for part in paragraphs if isinstance(scope_quote, str) and scope_quote
                                      and scope_quote in part['text']]
                    own_ids = {rid for part in matching_parts for rid in part.get('factIds', ())}
                    if not matching_parts or not set(statement.get('recordIds', ())).issubset(own_ids):
                        continue
                    mode = statement.get('scopeMode')
                    supported = mode == 'explicit_source_date' and _date_is_in_quote(
                        group['sourceDate'], scope_quote, days,
                    )
                    supported = supported or mode == 'earlier_source' and group['sourceDate'] < days[-1]
                    supported = supported or mode == 'later_source' and group['sourceDate'] > days[0]
                    if supported:
                        break
                if not supported:
                    return '最终正文未保留某行为facet的源时间/先后作用域：' + group['sourceDate']
        if obligation['kind'] == 'reference_coverage':
            if row['outcome'] == 'no_group_range_claim':
                if row.get('observationIds'):
                    return '未作临床正常命题的回执不能同时声明正常原子：' + obligation['code']
                continue
            group = next((group for group in contract['clinicalObservationGroups']
                          if group['code'] == row.get('groupCode')), None)
            if group is None:
                return '临床正常命题须指明实际clinicalObservationGroups.groupCode。'
            members = group.get('members', ())
            by_id = {member['observationId']: member for member in members}
            selected = row.get('observationIds') or []
            if not isinstance(selected, list) or any(not isinstance(oid, str) for oid in selected):
                return 'observationIds须为实际临床原子编号数组：' + obligation['code']
            if row['outcome'] == 'all_group_observations_in_range':
                if not members or set(selected) != set(by_id):
                    return '整组参考范围结论未覆盖全部真实原子：' + obligation['code']
            if row['outcome'] in {'all_group_observations_in_range', 'named_observations_in_range'}:
                if not selected or any(oid not in by_id for oid in selected):
                    return '参考范围复核缺少实际observationIds：' + obligation['code']
                if any(by_id[oid].get('referenceAssessmentState') != 'within'
                       or by_id[oid].get('statusReferenceConflict') for oid in selected):
                    return '所指原子并非有明确依据的原参考范围内：' + obligation['code']
            if row['outcome'] != 'no_group_range_claim' and (
                not selected or any(oid not in by_id for oid in selected)
            ):
                return '正常命题须绑定本组实际临床原子：' + obligation['code']
            if row['outcome'] != 'no_group_range_claim' and any(
                not set([by_id[oid].get('recordId'), *by_id[oid].get('recordIds', ())]).intersection(cited_ids)
                for oid in selected if oid in by_id
            ):
                return '范围/原状态概括必须绑定每个所指原子的实际来源，不能借其他正常项：' + obligation['code']
            if row['outcome'] == 'source_status_only' and (
                not selected or any(oid not in by_id or by_id[oid].get('sourceStatusState') != 'normal'
                                    for oid in selected)
            ):
                return '原正常状态概括须逐项有明确原状态，不能代替范围判断：' + obligation['code']
            if row['outcome'] == 'source_finding_only' and any(
                by_id[oid].get('sourceFindingState') not in {'normal', 'qualified_normal'} for oid in selected
            ):
                return '正常所见命题须有实际原文正常所见，不能由缺参考数值推导：' + obligation['code']
            if row['outcome'] in {'source_status_only', 'source_finding_only'} and any(
                _positive_claims(quote, re.compile(r'(?:参考)?(?:范围|区间)内'))
            ):
                return '原状态/所见不能冒充打印参考范围：' + obligation['code']
        if obligation['kind'] == 'activity_constraints':
            phases = row.get('phaseQuotes') or {}
            if row['outcome'] == 'applied_to_phases' and (not isinstance(phases, dict) or any(
                not isinstance(phases.get(key), str) or not phases[key]
                or not any(phases[key] in part['text'] for part in paragraphs)
                for key in ('start', 'progression')
            )):
                return '活动约束复核须核对最终起步与递进原句，不能仅点名异常：' + obligation['code']
            if not isinstance(row.get('sourceRationale'), str) or not row['sourceRationale'].strip():
                return '活动约束复核缺实际观察/原医嘱与阶段安排的医学关系：' + obligation['code']
    return None


def _draft_polarity_scopes(paragraphs):
    scopes = []
    boundary = re.compile(r'不等(?:同)?于|不(?:能|应|宜|可)(?:据此|直接|单独)?(?:说明|判断|推断|视为|解释为)'
                          r'|不代表|并非|不用于(?:判断|解释|诊断)')
    for part in paragraphs:
        for clause in re.split(r'[。！？；;\n]', part['text']):
            if boundary.search(clause):
                scopes.append({'quote': clause, 'recordIds': list(part.get('factIds', ())),
                               'claimPolarity': 'negated_inference',
                               'reviewDuty': '这是被否定的推论，不把被否定的结局当成本人的肯定诊断；'
                                             '仍核对该句实际观察、对象和否定范围。'})
    return scopes


def _names_object(record, text):
    return _mentions_object(text, record['objectLabels'])


def _names_gene_target(record, text):
    return any(re.search(r'(?<![A-Za-z0-9_])' + re.escape(symbol) + r'(?![A-Za-z0-9_])', text)
               for symbol in record['candidateGenes'])


def _names_sequence_target(record, text):
    aliases = []
    for label in record['sourceIdentityLabels']:
        normalized = unicodedata.normalize('NFKC', label)
        aliases.extend([normalized, normalized.partition('(')[0].strip()])
        aliases.extend(value for value in re.findall(r'\(([^()]*)\)', normalized)
                       if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', value))
    compact = unicodedata.normalize('NFKC', text).casefold()
    return _names_gene_target(record, text) or any(
        re.search(r'(?<![A-Za-z0-9_])' + re.escape(alias.casefold()) + r'(?![A-Za-z0-9_])', compact)
        if re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', alias) else alias.casefold() in compact
        for alias in aliases if alias
    )


def _planned_semantic_issue(target, cell, paragraphs, dossier):
    contract = cell.get('sourceContract') or {}
    for part in paragraphs:
        records = [_measurement_contract(dossier, dossier.reverse[rid]) for rid in part.get('factIds', ())
                   if rid in dossier.reverse]
        microbial = [record for record in records if record['typedMicrobialRecord']]
        for clause, match in _positive_claims(part['text'], _ABSENCE_CLAIM):
            if _NO_ABNORMALITY.match(clause, match.start()):
                normal = [record for record in microbial if record['noAbundanceAbnormality']
                          or record['sourceState'] in {'正常', '正常范围', '未见明显异常'}]
                other = [record for record in microbial if record not in normal]
                if microbial and (not normal or other and not any(
                    _names_object(record, clause) for record in normal
                )):
                    return '本命题的无异常须由对应对象和面板的原状态支持，不能用另一正常项覆盖实际偏离。'
                continue
            if not microbial:
                continue
            negative = [record for record in microbial if record['explicitNegativeDetection']]
            incompatible = [record for record in microbial if not record['explicitNegativeDetection']]
            sequence_negative = [record for record in negative if any(
                'sequence' in kind for kind in record['measurementTypes']
            )]
            if sequence_negative:
                named_sequence = any(_names_sequence_target(record, clause) for record in sequence_negative)
                molecular_object = re.search(r'基因|序列|靶标|探针|\b(?:DNA|RNA|PCR)\b', clause, re.I) or any(
                    _names_gene_target(record, clause) for record in sequence_negative
                )
                if not named_sequence or not molecular_object:
                    return ('原阴性属于序列检测靶标；请使用来源实际targetName/缩写或候选基因符号，'
                            '并保留分子对象限定，不能仅写宿主生物未检出或无名整类序列阴性。')
            if negative and not incompatible:
                continue
            # A named negative target can coexist with a separate abundance
            # statement. A group claim cannot borrow an unrelated negative.
            named_negative = any(_names_object(row, clause) or _names_gene_target(row, clause) for row in negative)
            named_incompatible = any(_names_object(row, clause) for row in incompatible)
            if named_negative and not named_incompatible:
                continue
            return ('检测语义不符：该肯定的未检出/缺失/低于检出限命题没有明确阴性靶标依据。'
                    '按sourceContract区分相对丰度、零值、无异常与序列阴性；群体表述也逐项核对，'
                    '混合命题分段并绑定各自真实recordIds，保留正确事实。')
        if microbial and any(_positive_claims(part['text'], _PANEL_ATTRIBUTION)):
            return ('组合原判定与成员所见均应保留并解释。现有来源未给组合贡献算法，'
                    '不能将组合异常归因为某个成员；请形成有据的并列综合判断。')
        for clause, _ in _positive_claims(part['text'], re.compile(r'(?:正常|参考(?:范围|区间)?)(?:下限|上限)')):
            for record in records:
                fact = dossier.facts[dossier.reverse[record['recordId']]]
                reference = unicodedata.normalize('NFKC', str(
                    fact.get('referenceRange') or fact.get('reference_range') or '',
                ))
                bound = re.fullmatch(r'\s*([<>])\s*([+-]?\d+(?:\.\d+)?)\s*', reference)
                if not bound or len(records) > 1 and str(record.get('object') or '') not in clause:
                    continue
                try:
                    at_boundary = Decimal(str(fact.get('value'))) == Decimal(bound[2])
                except (InvalidOperation, ValueError, TypeError):
                    continue
                if at_boundary:
                    return ('原参考使用严格大于或小于，实测恰为端点；不能将其写成含等号的正常上/下限。'
                            '保留原状态、实际值和原算符，按不同判定口径解释。')
        clinical = [record for record in records if record['kind'] == 'clinical']
        if clinical and all(not record['referenceAvailable'] for record in clinical) and any(_positive_claims(
            part['text'], re.compile(r'在(?:报告|本项|其)?(?:参考)?(?:范围|区间)内'),
        )):
            return '本段临床项目没有原参考区间。保留实测及明确原判定，不能由零值或其他项目的范围生成本项范围结论。'
        if contract.get('careAdvice', {}).get('preserveDirectnessAndSourceConditions'):
            used = set(part.get('factIds', ()))
            actions = [*cell.get('sourceClinicalActions', ()),
                       *((cell.get('actionContext') or {}).get('originalAdvice') or ())]
            relevant = [action for action in actions if used.intersection(action.get('recordIds', ()))]
            advice = [quote for action in relevant for quote in action.get('originalAdvice', ())]
            if not actions:
                advice.extend(cell.get('originalClinicalAdvice') or ())
            parallel = any(re.search(r'复查|复测|检查', text) and re.search(r'就诊|就医|随诊|咨询', text)
                           and not _REFERRAL_SEQUENCE.search(text) for text in advice)
            existing_sequence = any(_REFERRAL_SEQUENCE.search(text) for text in advice)
            if parallel and not existing_sequence and any(_positive_claims(
                part['text'], _REFERRAL_SEQUENCE, join_commas=True,
            )):
                return ('原医嘱的检查与就诊为直接安排。当前文字新增了先复查后就诊或结果仍异常才就诊的前提；'
                        '按原文保留各项建议及已有条件，由医学复核核对动作关系。')
    return None


class _Dossier:
    """The complete clinical and self-reported record, addressed by short handles."""

    def __init__(self, blueprint, subject):
        from .assessment_source_context import assessment_reference_date
        from .assessment_source_context import model_microbiome_observation
        from .clinical_source_projection import patient_reference_context

        self.assessment_date = assessment_reference_date()
        self.planned = isinstance(getattr(blueprint.data, '_assessment_content_plan', None), dict)
        self.facts = {row.get('factId', row.get('fact_id')): row for row in blueprint.facts}
        self.facts.pop(None, None)
        self.clinical_projections = getattr(blueprint, 'assessment_sources', {}).get('clinicalPatientProjections', {})
        self.reference_contexts = {fid: patient_reference_context(fact) or {} for fid, fact in self.facts.items()
                                   if fact.get('kind') == 'clinical'}
        self.microbiome_semantics = getattr(blueprint, 'assessment_sources', {}).get('microbiomeSourceSemantics', {})
        self.reported_roles = getattr(blueprint, 'assessment_sources', {}).get('reportedSourceRoles', {})
        self.reported_temporal = getattr(blueprint, 'assessment_sources', {}).get('reportedTemporalContexts', {})
        content_plan = getattr(blueprint.data, '_assessment_content_plan', {}) or {}
        self.behavior_fact_ids = {
            fid for row in content_plan.get('lifestyleFactors', ()) for fid in row.get('factIds', ())
            if fid in self.facts
        }
        self.assay_groups = getattr(blueprint, 'assessment_sources', {}).get('assayGroups', ())
        self.clinical_observation_groups = getattr(blueprint, 'assessment_sources', {}).get(
            'clinicalObservationGroups', (),
        )
        self.gene_fact_names, self.gene_source_semantics, self.unverified_gene_labels = {}, {}, set()
        self.unverified_gene_sources = {}
        layout = getattr(getattr(blueprint, 'data', None), '_template_layout', {})
        for gene in layout.get('geneDetections', ()):
            if gene.get('annotationVerified') is True:
                continue
            label = re.sub(r'\s+', '', str(gene.get('reportedLabel') or ''))
            negative = bool(re.fullmatch(r'未检出|阴性|not_detected|negative',
                                         str(gene.get('sourceStatus') or '').strip(), re.I))
            negative = negative or any(
                item.get('measurementType') in {'sequence_target', 'sequence_detection'}
                and item.get('stateMeaning') in {'target_not_detected', 'sequence_not_detected'}
                for fid in gene.get('sourceFactIds', ()) for item in self.microbiome_semantics.get(fid, ())
            )
            if len(label) > 4 and not negative:
                self.unverified_gene_labels.add(label)
                self.unverified_gene_sources[label] = set(gene.get('sourceFactIds', ())).intersection(self.facts)
            symbols = gene.get('claimObjects') or ()
            name = ('、'.join(symbols) + '（基因序列）' if symbols else
                    str(gene.get('reportedLabel') or '基因序列检测面板') if negative else '基因序列检测面板')
            for fid in gene.get('sourceFactIds', ()):
                if fid in self.facts:
                    self.gene_fact_names[fid] = name
                    self.gene_source_semantics[fid] = {
                        'measurementType': 'sequence_target',
                        'targetName': name,
                        'sourceStatus': gene.get('sourceStatus'),
                        'detectedGenes': list(symbols),
                        'candidateGenes': list(gene.get('candidateGenes') or ()),
                        'stateMeaning': 'target_not_detected' if negative else 'detected_gene_sequences',
                    }
        self.microbiome_observations = {}
        self.literal_sources = {}
        for fid, fact in self.facts.items():
            semantics = self.microbiome_semantics.get(fid, ())
            semantics = [semantics] if isinstance(semantics, dict) else list(semantics)
            if not semantics and fid in self.gene_source_semantics:
                semantics = [self.gene_source_semantics[fid]]
            observation = model_microbiome_observation(fact, semantics)
            if observation is not None:
                self.microbiome_observations[fid] = observation
        self.handles = {fid: f'F{index:03d}' for index, fid in enumerate(self.facts, 1)}
        self.reverse = {handle: fid for fid, handle in self.handles.items()}
        self.source_observation_registry = build_observation_registry(
            _source_handles(self.clinical_observation_groups, self),
        )
        person = _plain(subject) if subject is not None else {}
        if not isinstance(person, dict):
            person = {key: getattr(person, key, None) for key in ('name', 'sex', 'gender', 'age')}
        self.person = {
            '姓名': person.get('name'),
            '性别': person.get('sex') or person.get('gender'),
            '年龄': person.get('age'),
        }
        self.clinical = [fid for fid, fact in self.facts.items() if fact.get('kind') == 'clinical']
        self.reported = [fid for fid, fact in self.facts.items() if fact.get('kind') == 'self_reported']
        self.background = [fid for fid, fact in self.facts.items() if fact.get('kind') == 'context']
        self.clinical_trends = [
            {
                'name': series['name'],
                'direction': series['direction'],
                'points': [
                    {key: point[key] for key in ('observedAt', 'value', 'unit', 'status')}
                    for point in series['points']
                ],
            }
            for series in getattr(blueprint, 'clinical_trends', ())
        ]
        self.presence_index = []
        seen = set()
        for fid, fact in self.facts.items():
            label = (self.microbiome_observations.get(fid) or {}).get('label') if self.planned else None
            key = (fact.get('kind'), label or self.gene_fact_names.get(fid, fact.get('name')))
            if key not in seen:
                seen.add(key)
                self.presence_index.append({'id': self.handles[fid], 'name': key[1], 'kind': key[0]})

    def uses_unverified_gene_label(self, text, fact_handles=(), *, source_observation=False):
        ids = [self.reverse.get(handle) for handle in fact_handles]
        if not any(fid in self.gene_fact_names for fid in ids) and not re.search(
            r'菌群|测序|基因|毒力|序列|粪便(?:样本|检测)', text,
        ):
            return False
        compact = re.sub(r'\s+', '', text)
        clinical = [
            fact for fid in ids
            if (fact := self.facts.get(fid)) and fact.get('kind') == 'clinical'
        ]
        independent_labels = {
            re.sub(r'\s+', '', str(fact.get('name') or '')) for fact in clinical
        }
        return any(label in compact and label not in independent_labels
                   and not (source_observation and self.unverified_gene_sources.get(label, set()).intersection(ids))
                   for label in self.unverified_gene_labels)

    def view(self, cell_fact_ids=(), *, limit=240):
        """Complete clinical and self-reported records plus the cells' own rows.

        ``cell_fact_ids`` is one list per cell. The quota is shared round-robin
        so that a cell owning hundreds of rows (all taxa) cannot crowd out the
        records of the cells written in the same batch; rows that deviate from
        their reference come first within each cell.
        """

        from .assessment_source_context import assay_catalog_context
        from .assessment_source_context import model_source_semantics

        def rows(ids):
            result = []
            for fid in ids:
                if fid not in self.facts:
                    continue
                row = {'id': self.handles[fid],
                       **_fact_view(self.facts[fid], projection_index=self.clinical_projections,
                                    reported_role=self.reported_roles.get(fid),
                                    temporal_context=self.reported_temporal.get(fid))}
                if fid in self.gene_fact_names:
                    row['name'] = self.gene_fact_names[fid]
                    row['sourceSemantics'] = deepcopy(self.gene_source_semantics[fid])
                if fid in self.microbiome_semantics:
                    from .assessment_microbiome_view import source_semantic_context

                    row['sourceSemantics'] = [
                        {**model_source_semantics(source_semantic_context(item), self.handles),
                         'recordIds': [self.handles[source_id]
                            for source_id in item.get('recordIds', ()) if source_id in self.handles]}
                        for item in self.microbiome_semantics[fid]
                    ]
                if self.planned and fid in self.microbiome_observations:
                    observation = _source_handles(self.microbiome_observations[fid], self)
                    row['name'] = observation['label']
                    for field in ('value', 'unit', 'status'):
                        row.pop(field, None)
                        if observation.get(field) is not None:
                            row[field] = deepcopy(observation[field])
                    self.literal_sources[fid] = observation.pop('literalSource')
                    row['sourceObservation'] = compact_microbiome_observation(observation)
                    row.pop('sourceSemantics', None)
                elif self.planned and self.facts[fid].get('kind') == 'clinical':
                    from .assessment_clinical_observation import model_clinical_observation

                    observation = model_clinical_observation(
                        self.facts[fid], projection_index=self.clinical_projections,
                    )
                    if observation is not None:
                        observation = short_observation_ids(_source_handles(observation, self),
                                                            self.source_observation_registry)
                        row['name'] = observation['label']
                        for field in ('value', 'unit', 'status'):
                            row.pop(field, None)
                            if observation.get(field) is not None:
                                row[field] = deepcopy(observation[field])
                        self.literal_sources[fid] = observation.pop('literalSource')
                        row['sourceObservation'] = observation
                        row.pop('clinicalSourceProjection', None)
                elif self.planned and self.facts[fid].get('kind') == 'self_reported':
                    from .assessment_reported_observation import model_reported_observation

                    observation = model_reported_observation(self.facts[fid],
                        reported_role=self.reported_roles.get(fid),
                        temporal_context=self.reported_temporal.get(fid))
                    if observation is not None:
                        self.literal_sources[fid] = observation.pop('literalSource')
                        row['sourceObservation'] = _source_handles(observation, self)
                        row['name'], row['value'] = observation['label'], deepcopy(observation['value'])
                result.append(row)
            return result

        selected = set(fid for ids in cell_fact_ids for fid in ids)
        clinical = [fid for fid in self.clinical if not self.planned or fid in selected]
        reported = [fid for fid in self.reported if not self.planned or fid in selected]
        base = set(clinical) | set(reported)

        def deviating(fid):
            fact = self.facts[fid]
            state = str(fact.get('status') or '')
            return 0 if fact.get('abnormal') or (state and state not in _UNREMARKABLE) else 1

        queues = []
        for ids in cell_fact_ids:
            own = [fid for fid in dict.fromkeys(ids) if fid in self.facts and fid not in base]
            queues.append(sorted(own, key=deviating))
        chosen, position = [], 0
        while len(chosen) < limit and any(position < len(queue) for queue in queues):
            for queue in queues:
                if position < len(queue) and queue[position] not in chosen:
                    chosen.append(queue[position])
            position += 1
        patient = {
            'assessmentDate': self.assessment_date,
            'person': self.person,
            'clinicalRecords': rows(clinical),
            'selfReported': rows(reported),
            'cellRecords': rows(chosen[:limit]),
            'recordPresenceIndex': [dict(row) for row in self.presence_index],
        }
        if self.planned and self.assay_groups:
            patient['assayGroups'] = assay_catalog_context(self.assay_groups, self.handles)
        if self.clinical_trends:
            patient['clinicalTrends'] = self.clinical_trends
        return patient

    def numbers(self):
        """Every literal number that appears in the record, for grounding hints."""
        found = set()
        for fact in self.facts.values():
            source_view = _fact_view(fact, projection_index=self.clinical_projections)
            for key in ('value', 'reference', 'name', 'formula'):
                found.update(re.findall(r'\d+(?:\.\d+)?', str(source_view.get(key) or '')))
        for series in self.clinical_trends:
            for point in series['points']:
                found.update(re.findall(r'\d+(?:\.\d+)?', str(point['value'])))
        return found


def _passages(source_text, terms, *, max_chars=2200, unit_chars=650):
    """Exact substrings of the original that name the topic's object."""
    units = []
    for line in re.finditer(r'[^\n]+', source_text):
        text = line.group()
        if len(text.strip()) < 30:
            continue
        if len(text) <= unit_chars:
            units.append((line.start(), line.end()))
            continue
        start = line.start()
        for sentence in re.finditer(r'.+?(?:[。！？；]|[.!?](?=\s)|$)\s*', text, re.S):
            end = line.start() + sentence.end()
            if end - start >= unit_chars or sentence.end() == len(text):
                units.append((start, end))
                start = end
        if start < line.end():
            units.append((start, line.end()))
    scored = []
    for index, (start, end) in enumerate(units):
        text = source_text[start:end]
        hits = sum(_term_in(text, term) for term in terms)
        if hits:
            scored.append(
                (
                    hits
                    + (
                        0.5
                        if re.search(r'recommend|should|suggest|建议|推荐|应当|associated|risk|相关', text, re.I)
                        else 0
                    ),
                    index,
                )
            )
    chosen, length = [], 0
    for _, index in sorted(scored, key=lambda item: (-item[0], item[1])):
        start, end = units[index]
        text = source_text[start:end].strip()
        if length + len(text) > max_chars and chosen:
            continue
        if len(text) > max_chars:
            continue
        chosen.append(index)
        length += len(text)
        if length >= max_chars:
            break
    return [source_text[units[index][0] : units[index][1]].strip() for index in sorted(chosen)]


def _evidence_applicable(summary, reference, target, *, mechanism_background=False):
    owner = target.get('owner') or {}
    if owner.get('presentation') != 'assessment.v1':
        return True
    title = str(summary.get('title') or '')
    url = str(getattr(reference, 'url', None) or summary.get('url') or '')
    study_types = ' '.join(getattr(reference, 'study_types', ()))
    start = str(summary.get('sourceText') or '')[:10000]
    if re.search(r'/action/downloadSupplement(?:[?#]|$)', url, re.I) and not (
        getattr(reference, 'doi', None) or summary.get('doi')
    ):
        return False
    animal = re.search(r'Animal or In Vitro|\bmurine\b|\bmice\b|\bmouse model\b|小鼠|大鼠|体外实验',
                       title + ' ' + study_types, re.I)
    experimental_model = re.search(
        r'\b(?:DSS|dextran sulfate sodium)[-\u2013 ]induced.{0,45}(?:mice|mouse|murine)|'
        r'\b(?:mice|mouse|murine).{0,45}(?:DSS|dextran sulfate sodium)[-\u2013 ]induced',
        start, re.I | re.S,
    )
    includes_humans = re.search(
        r'\b(?:human participants|randomi[sz]ed controlled trial|enrolled (?:human|patients))\b',
        title + ' ' + start[:4000], re.I,
    )
    if (animal or experimental_model) and not includes_humans and not mechanism_background:
        return False
    diagnoses = ' '.join(owner.get('verifiedDiagnoses', ()))
    if re.search(r'guideline|consensus|指南|共识', title, re.I):
        if re.search(r'便秘|\bconstipation\b', title, re.I) and _ONCOLOGY_CONTEXT.search(title) and not (
            _VERIFIED_MALIGNANCY.search(diagnoses)
        ):
            return False
        if re.search(r'(?:in|with|for).{0,30}\btype\s*2\s*diabetes|2\s*型糖尿病', title, re.I) and not re.search(
            r'2\s*型糖尿病|type\s*2\s*diabetes|T2DM', diagnoses, re.I,
        ):
            return False
        if re.search(r'chronic insomnia (?:disorder|in adults)|慢性失眠障碍', title, re.I) and not re.search(
            r'慢性失眠|chronic insomnia', diagnoses, re.I,
        ):
            return False
    target_name = str(owner.get('name') or '')
    if re.search(r'GMWI\s*2', target_name, re.I):
        return not re.search(r'core keystone index|泌尿生殖|urogenital', title, re.I)
    if re.search(r'定植抗力|B/E', target_name, re.I):
        return not re.search(r'泌尿生殖|urogenital|genitourinary|core keystone index', title, re.I)
    return True


class _Library:
    """Candidate original passages for one batch, with citation bookkeeping."""

    def __init__(self, evidence, targets, chapter_code, *, budget=42000):
        references = {ref.number: ref for ref in getattr(evidence, 'references', ())}
        plan = {topic['topicId']: topic for topic in getattr(evidence, 'plan', ())}
        wanted = {target['id'] for target in targets if not (target.get('owner') or {}).get('usesReviewedFactsOnly')}
        self.entries, self.passages, used = [], {}, 0
        self.allowed_targets = {}
        self.allowed_roles = {}
        planned = all((target.get('owner') or {}).get('contentPlanVersion') for target in targets)
        role_choices = {}
        rows = []
        for summary in getattr(evidence, 'summaries', ()):
            links = [link for link in summary.get('topicLinks', ()) if wanted.intersection(link.get('targetIds', ()))]
            if links:
                rows.append((max(link.get('score', 0) for link in links), summary, links))
        rows.sort(key=lambda item: -item[0])
        for _, summary, links in rows:
            number = summary['number']
            reference = references.get(number)
            full = summary.get('sourceText') or ''
            admitted = {}
            for link in links:
                roles = set(link.get('eligibleRoles', ()))
                if 'eligibleRoles' not in link and 'selectedOriginalPassages' not in link:
                    roles = {'interpretation', 'action'}
                permitted = {
                    target['id']: {
                        role for role in set(target.get('allowedRoles') or ('interpretation',)).intersection(roles)
                        if _evidence_applicable(summary, reference, target, mechanism_background=(
                            link.get('candidateUse') == 'general_function' and role == 'interpretation'))
                    }
                    for target in targets if target['id'] in wanted and target['id'] in link.get('targetIds', ())
                }
                permitted = {target_id: values for target_id, values in permitted.items() if values}
                if planned:
                    permitted = {
                        target_id: {role for role in values
                                    if len(role_choices.get((target_id, role), ())) < _PUBLICATION_CHOICES_PER_ROLE}
                        for target_id, values in permitted.items()
                    }
                    permitted = {target_id: values for target_id, values in permitted.items() if values}
                eligible = set(permitted)
                if not eligible:
                    continue
                if 'selectedOriginalPassages' in link:
                    from .evaluation_report_evidence import validate_original_source_unit

                    texts = []
                    stored = link.get('selectedOriginalPassages')
                    for original in stored if isinstance(stored, list) else ():
                        if not isinstance(original, dict):
                            continue
                        text = original.get('text')
                        identity = original.get('id', original.get('passageId'))
                        if (identity not in link.get('selectedPassageIds', ())
                                or text not in link.get('selectedQuotes', ())
                                or not isinstance(text, str)):
                            continue
                        complete = validate_original_source_unit(full, original)
                        if complete is not None:
                            texts.append(text)
                else:
                    # Historical evidence bundles have no selector receipt;
                    # retain literal object extraction for that older format.
                    texts = _passages(full, link.get('objectTerms') or topic_terms(
                        plan.get(link['topicId'], {'aliases': [link.get('objectName')]})))
                for text in texts:
                    row = admitted.setdefault(text, {'targets': set(), 'scope': [], 'roles': {}})
                    row['targets'].update(eligible)
                    for target_id, values in permitted.items():
                        row['roles'].setdefault(target_id, set()).update(values)
                    scope = {key: deepcopy(link[key]) for key in (
                        'objectName', 'objectKind', 'candidateUse', 'eligibleRoles', 'scopeNotes') if key in link}
                    if scope not in row['scope']:
                        row['scope'].append(scope)
            if not admitted:
                continue
            _, grade_label = evidence_grade(
                summary.get('category'), getattr(reference, 'study_types', ()) if reference is not None else ()
            )
            texts = list(admitted)
            cost = sum(map(len, texts))
            if used + cost > budget and self.entries:
                continue
            used += cost
            passages = []
            for index, text in enumerate(texts, 1):
                from .evaluation_report_evidence import _body_source_scope_options

                pid = f'E{number}.{index}'
                self.passages[pid] = (number, text)
                self.allowed_targets[pid] = admitted[text]['targets']
                self.allowed_roles[pid] = admitted[text]['roles']
                if planned:
                    for target_id, roles in admitted[text]['roles'].items():
                        for role in roles:
                            role_choices.setdefault((target_id, role), set()).add(number)
                passages.append({
                    'pid': pid, 'text': text,
                    'sourceScope': _body_source_scope_options(
                        full, summary.get('title') or '', selected_quotes=(text,),
                    ),
                    'admittedUse': admitted[text]['scope'],
                    'targetRoles': {target_id: sorted(values)
                                    for target_id, values in admitted[text]['roles'].items()},
                })
            self.entries.append(
                {
                    'ref': f'E{number}',
                    'type': grade_label,
                    'title': summary.get('title'),
                    'year': summary.get('year'),
                    'source': getattr(reference, 'source', None),
                    'about': list(dict.fromkeys(link.get('objectName') for link in links if link.get('objectName')))[
                        :6
                    ],
                    'passages': passages,
                }
            )

    def citation(self, pids):
        grouped = {}
        for pid in pids:
            if pid in self.passages:
                number, text = self.passages[pid]
                quotes = grouped.setdefault(number, [])
                if text not in quotes:
                    quotes.append(text)
        return [{'number': number, 'quotes': quotes} for number, quotes in grouped.items()]


def _event(audit, event):
    if isinstance(audit, dict):
        audit.setdefault('writer', []).append(event)


# Section labels from the writing instructions that occasionally lead a paragraph.
_LEAKED_LABEL = re.compile(r'^\s*(?:[①②③④⑤⑥]\s*|(?:一句话)?总判断[：:]\s*)')


def _item_title(value):
    """A short noun heading of one item, or '' when the model gave none (or a placeholder)."""
    if not isinstance(value, str):
        return ''
    title = re.sub(r'[\s：:，。；、“”"（）()\[\]【】]+', '', value)
    return title if 2 <= len(title) <= 14 else ''


def _paragraphs(raw, *, clean_prose=True):
    """Paragraphs of one cell; an itemised paragraph reads "小标题：正文"."""
    rows = raw.get('paragraphs') if isinstance(raw, dict) else None
    if isinstance(rows, str):
        rows = [{'text': rows}]
    result = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, str):
            row = {'text': row}
        if not isinstance(row, dict) or not isinstance(row.get('text'), str) or not row['text'].strip():
            continue
        text = re.sub(r'[ \t]*\n+[ \t]*', '', row['text'].strip())
        # Internal record/passage handles (F012, E3.1) are never content; a leaked
        # handle is removed rather than costing the whole cell.
        handle = r'(?<![A-Za-z0-9])(?:F\d{2,4}|E\d{1,4}\.\d+|E\d{2,4}|R\d{2})(?![0-9A-Za-z])'
        text = re.sub(r'[（(\[【]?\s*' + handle + r'(?:\s*[,，、]\s*' + handle + r')*\s*[）)\]】]?', '', text)
        text = _LEAKED_LABEL.sub('', text)
        if clean_prose:
            text = reader_text(text, prose=True)
        text = re.sub(r'\s*[\[【]\s*[ER]\d+(?:\.\d+)?(?:\s*[,，、]\s*[ER]\d+(?:\.\d+)?)*\s*[\]】]', '', text)
        title = _item_title(row.get('title'))
        if title and text and not text.startswith(title + '：'):
            text = title + '：' + text
            if clean_prose:
                text = reader_text(text, prose=True)
        result.append(
            {
                'text': text,
                'factIds': [value for value in row.get('factIds', ()) if isinstance(value, str)]
                if isinstance(row.get('factIds'), list)
                else [],
                'cite': [value for value in row.get('cite', ()) if isinstance(value, str)]
                if isinstance(row.get('cite'), list)
                else [],
                'role': row.get('role') if isinstance(row.get('role'), str) else None,
            }
        )
    return result


_ITEM_CHARS = 190  # an item longer than this is a block of prose, not an item


def _check(spec, paragraphs, *, strict=False):
    """Deterministic form checks; medical content is the reviewer's job.

    ``strict`` is the first draft: the length range is enforced and an
    itemised cell must be itemised (no block of prose). A second attempt is
    published as written unless it is several times the intended length;
    form alone never costs the report a cell.
    """
    text = '\n\n'.join(row['text'] for row in paragraphs)
    if not text:
        return 'empty'
    if spec['form'] == 'choice':
        return None if text in spec['allowed'] else 'value_not_allowed'
    if _PROCESS_WORDS.search(text):
        return 'process_wording'
    if spec.get('sourcePlanned') and _EDITORIAL_INSTRUCTIONS.search(text):
        return '请直接写检查判断、症状或原建议；删去栏目分工、核验状态和资料归档过程。'
    if not spec.get('sourcePlanned'):
        if spec.get('careSummary') and _POPULATION_SCREENING.search(text):
            return '删除人群筛查统计和笼统年龄段表述。直接写本人的发现、查因目的及管理顺序。'
        if spec.get('assessmentScope') == 'function' and _QUANTIFIED_DIET.search(text):
            return '删除这里的定量饮食安排。只概括本组有据的菌群功能预测特点和观察重点。'
        if spec.get('assessmentScope') == 'function' and len(_FUNCTION_SCORE.findall(text)) > 1:
            return '保留结果列的分数。此处直接概括本组最有意义的共同特征。'
        if spec.get('assessmentCode') in {'organisms.bacteria', 'organisms.otherOrganisms'} and len(
            _ABUNDANCE_VALUE.findall(text)
        ) > 2:
            return '丰度留在检测结果和正常基线栏。直接解释影响判断的重点，省去无关正常项目和感染排除套话。'
        if spec.get('assessmentCode') == 'structure.colonizationResistance' and _POSITIVE_BE_CLAIM.search(text):
            return 'B/E仅反映菌群相对组成。删除对本人抑制能力、黏膜屏障或感染风险的推断，直接陈述比值的含义。'
    length = sum(len(row['text']) for row in paragraphs)
    if length > (int(spec['max'] * 1.5) + 30 if strict else spec['max'] * 3 + 60):
        return 'too_long'
    if spec['form'] == 'paragraph' and len(text) < max(12, int(spec['min'] * 0.45)):
        return 'too_short'
    if strict and spec.get('outline') and any(len(row['text']) > _ITEM_CHARS for row in paragraphs):
        return 'not_itemized'
    if strict and spec.get('segments'):
        parts = [part.strip() for part in re.split(r'[；;]', text)]
        if len(parts) != spec['segments'] or not all(parts) or len(parts[0]) > 10:
            return 'bad_format'
    return None


def _merge_revision(draft, revised):
    """The reviewer's correction applied to the whole cell.

    Reviewers sometimes return only the paragraph they changed. Accepting that
    as the cell would silently drop the paragraphs they had no objection to,
    so each returned paragraph replaces the draft paragraph it most resembles
    and the others are kept. When the correction cannot be aligned with the
    draft, ``[]`` is returned and the cell goes back to the writer instead.
    """
    if not revised or len(revised) >= len(draft):
        return revised
    length = sum(len(row['text']) for row in draft)
    if sum(len(row['text']) for row in revised) >= 0.8 * length:
        return revised  # condensed into fewer paragraphs, not truncated
    merged, used = list(draft), set()
    for row in revised:
        scores = [
            (SequenceMatcher(None, row['text'], part['text'], autojunk=False).ratio(), index)
            for index, part in enumerate(draft)
            if index not in used
        ]
        best, index = max(scores, default=(0.0, None))
        if index is None or best < 0.35:
            return []
        merged[index] = row
        used.add(index)
    return merged


# Words whose loss would change the medical meaning of a sentence.
_MEANING_WORDS = (
    '可能',
    '疑似',
    '约',
    '阳性',
    '阴性',
    '未见',
    '没有',
    '不要',
    '避免',
    '立即',
    '尽快',
    '急诊',
    '家属',
    '父亲',
    '母亲',
)


def _faithful_rewrite(original, rewritten):
    """True when a style rewrite kept every number and meaning word and added no tell."""
    if not isinstance(rewritten, str) or not rewritten.strip():
        return False
    numbers = re.compile(r'\d+(?:\.\d+)?')
    if set(numbers.findall(original)) != set(numbers.findall(rewritten)):
        return False
    if len(rewritten) > len(original) * 1.1 + 8 or len(rewritten) < len(original) * 0.88:
        return False
    if any(word in original and word not in rewritten for word in _MEANING_WORDS):
        return False
    before, after = ITEM_TITLE.match(original), ITEM_TITLE.match(rewritten)
    if (before['title'] if before else None) != (after['title'] if after else None):
        return False
    return set(ai_tells(rewritten)) <= set(ai_tells(original))


def _grounding_hints(paragraphs, known_numbers, library, also_known=''):
    """Numbers with a clinical unit that are absent from the record and citations.

    ``also_known`` is further legitimate material of the cell: its own result
    row and the already reviewed text it summarises.
    """
    cited = ' '.join(library.passages[pid][1] for row in paragraphs for pid in row['cite'] if pid in library.passages)
    cited_numbers = set(re.findall(r'\d+(?:\.\d+)?', cited + ' ' + str(also_known)))
    hints = []
    pattern = re.compile(
        r'(\d+(?:\.\d+)?)\s*(mmol/L|μmol/L|umol/L|mIU/L|U/L|ng/m[lL]|pg/m[lL]|g/L|mg/L|mg/dL|kg/m2|kg/m²|kg|cm²|cm2|cm|mmHg|%|分|岁|次/分|kcal)'
    )
    for row in paragraphs:
        for number, unit in pattern.findall(row['text']):
            if number not in known_numbers and number not in cited_numbers:
                hints.append(number + unit)
    return list(dict.fromkeys(hints))[:12]


def _block(target, paragraphs, dossier, library, spec):
    """Shape accepted by apply_reviewed_report_blocks; never touches source values."""
    allowed = set(target.get('sourceFactIds', ()))
    primary = [fid for fid in (target.get('owner') or {}).get('primaryFactIds', ()) if fid in dossier.facts]
    role = _target_role(target)
    sentences, used = [], set()
    for row in paragraphs:
        sentence_role = (row.get('role') or role) if dossier.planned else role
        ids = [dossier.reverse[handle] for handle in row['factIds'] if handle in dossier.reverse]
        ids = [fid for fid in dict.fromkeys(ids) if fid in allowed]
        allowed_targets = getattr(library, 'allowed_targets', {})
        pids = [pid for pid in row['cite'] if pid not in allowed_targets or target['id'] in allowed_targets[pid]]
        citations = library.citation(pids) if spec['form'] == 'paragraph' else []
        rules = sentence_rules(target.get('owner'), row['cite']) if spec['form'] == 'paragraph' else []
        used.update(ids)
        sentences.append(
            {
                'text': row['text'],
                'factIds': ids,
                'role': sentence_role,
                'evidence': citations,
                'support': 'evidence' if citations else 'clinical_synthesis',
                **({'rules': rules} if rules else {}),
            }
        )
    if (not (target.get('owner') or {}).get('contentPlanVersion') and primary
            and not used.intersection(primary) and sentences and not target.get('sharedMicrobiomePanel')):
        # The cell is owned by these rows; ownership is its factual anchor even
        # when the prose does not restate the value shown in the adjacent column.
        sentences[0]['factIds'] = list(dict.fromkeys([*primary[:3], *sentences[0]['factIds']]))
    return {
        'targetId': target['id'],
        'completion': 'complete',
        'sentences': sentences,
        'analysisGenerated': True,
        'writerRevision': WRITER_REVISION,
    }


_SUMMARY_INPUTS = {
    'overviewSummary': ('body.', 'system.', 'priority.'),
    'bodySummary': ('body.',),
    'graphSummary': ('synthesis.', 'priority.'),
    'symptomSummary': ('symptom.', 'symptomEvidence.'),
    'clinicalLead': ('clinical.', 'clinicalRow.', 'specialtyRow.'),
    'specialtySummary': ('specialtyRow.',),
    'microbiomeLead': ('digestive.', 'structure.', 'organisms.'),
    'structureSummary': ('structure.',),
    'riskSummary': ('digestive.', 'clinicalRow.', 'specialtyRow.', 'symptom.'),
    'clusterSummary': ('synthesis.', 'synthesisAction.'),
    'conclusionSummary': ('synthesis.', 'layer.', 'management.'),
}


def _summary_inputs(target):
    owner = target.get('owner') or {}
    if not owner.get('contentPlanVersion'):
        return ()
    code = owner.get('contentScopeCode') or ''
    if code.startswith('clinical.'):
        return ('clinicalRow.' + code.partition('.')[2] + '.',)
    return _SUMMARY_INPUTS.get(code, ())


def _context(previous, blueprint_targets, chapter, batch_ids, *, budget, allowed_facts=None, inputs=None):
    """Already reviewed text that this batch should build on and not repeat."""
    rows = []
    final = chapter['code'] in {'healthOverview', 'comprehensiveConclusion'}
    for code, blocks in (previous or {}).items():
        if inputs is None and not final and code != chapter['code']:
            continue
        for block in blocks:
            target = blueprint_targets.get(block.get('targetId'))
            if target is None or block['targetId'] in batch_ids:
                continue
            if inputs is not None and block['targetId'] not in inputs:
                continue
            if allowed_facts is not None and not any(
                allowed_facts.intersection(sentence.get('factIds', ())) for sentence in block.get('sentences', ())
            ):
                continue
            text = '\n'.join(sentence.get('text', '') for sentence in block.get('sentences', ()))
            if len(text) < 12:
                continue
            owner = target.get('owner') or {}
            rows.append(
                (
                    0 if owner.get('aggregate') else 1,
                    {'chapter': code, 'item': owner.get('name'), 'column': _spec(target).get('label'), 'text': text},
                )
            )
    rows.sort(key=lambda item: item[0])
    result, used = [], 0
    for _, row in rows:
        if used + len(row['text']) > budget:
            row = {**row, 'text': row['text'][: max(0, budget - used)]}
            if len(row['text']) < 40:
                break
        result.append(row)
        used += len(row['text'])
        if used >= budget:
            break
    return result


def _batch_library(evidence, targets, chapter_code, cache):
    key = (chapter_code, tuple(target['id'] for target in targets))
    if key not in cache:
        cache[key] = _Library(evidence, targets, chapter_code)
    return cache[key]


def _keeps_batch_evidence(evidence, original_batches, merged, chapter_code, cache):
    """A merge may not crowd out any previously available publication or passage."""
    if evidence is None or chapter_code is None:
        return True
    library = _batch_library(evidence, merged, chapter_code, cache)
    available = {entry['ref']: [part['text'] for part in entry['passages']] for entry in library.entries}
    for batch in original_batches:
        original = _batch_library(evidence, batch, chapter_code, cache)
        for entry in original.entries:
            passages = available.get(entry['ref'])
            if passages is None or any(
                not any(part['text'] in text for text in passages) for part in entry['passages']
            ):
                return False
    return True


def _batch_patient(dossier, targets, cache):
    key = tuple(target['id'] for target in targets)
    if key not in cache:
        cache[key] = dossier.view([
            list(target.get('sourceFactIds', ())) if dossier.planned
            else list((target.get('owner') or {}).get('primaryFactIds', ()))
            for target in targets
        ])
    return cache[key]


def _uses_other_panel_taxon(text, scope):
    if not scope:
        return False
    outside = set(scope['outside'])
    # Match the longest literal name first so a shorter label within a
    # different member's full name is not interpreted as another taxon.
    aliases = sorted([*scope['inside'], *outside], key=lambda name: (-len(name), name))
    pattern = '|'.join(re.escape(name) for name in aliases)
    return any(hit.group() in outside for hit in re.finditer(pattern, text.casefold()))


def _comparison_scope(target, dossier, patient):
    """Describe visible comparison groups without declaring medical equivalence."""
    owner = target.get('owner') or {}
    ids = list(dict.fromkeys(owner.get('primaryFactIds', ())))
    visible = {row['id'] for key in ('clinicalRecords', 'selfReported', 'cellRecords') for row in patient[key]}
    groups = {}
    complete = bool(ids)
    for fid in ids:
        fact = dossier.facts.get(fid)
        handle = dossier.handles.get(fid)
        if fact is None or handle not in visible:
            complete = False
            continue
        row = _fact_view(fact, projection_index=dossier.clinical_projections)
        key = (row.get('unit'), row.get('reference'))
        groups.setdefault(key, []).append(handle)
    return {
        'scopeName': owner.get('name'),
        'complete': complete,
        'meaning': '仅本格原始记录的可见范围；相同单位和参考口径不等于医学可互比，不代表全报告',
        'groups': [{'unit': key[0], 'reference': key[1], 'recordIds': handles} for key, handles in groups.items()],
    }


def _review_findings(previous, current):
    """Carry substantive corrections, never another batch's local citation handles."""
    rows, seen = [], set()

    for event in [*(previous or ()), *(current or ())]:
        if not isinstance(event, dict) or event.get('stage') != 'review':
            continue
        targets = event.get('targets') or {}
        for handle, issues in (event.get('issues') or {}).items():
            target = targets.get(handle) or {}
            for issue in issues or ():
                if not isinstance(issue, dict) or issue.get('type') not in _SERIOUS | {'citation_mismatch'}:
                    continue
                detail = _portable_review_text(issue.get('detail'))
                key = (target.get('targetId'), event.get('chapter'), issue['type'], detail)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        'chapter': event.get('chapter'),
                        'targetId': target.get('targetId'),
                        'item': target.get('item'),
                        'factIds': list(target.get('primaryFactIds') or ()),
                        'verdict': (event.get('verdicts') or {}).get(handle),
                        'type': issue['type'],
                        'detail': detail,
                        'reviewedText': _portable_review_text(target.get('reviewedText')),
                    }
                )
    return rows


def _portable_review_text(value):
    text = re.sub(
        r'(?<![A-Za-z0-9])(?:[FOK]\d{2,4}|E\d{1,4}(?:\.\d+)?)(?![0-9A-Za-z])', '', str(value or ''),
    )
    return re.sub(r'[ \t]+', ' ', text).strip()


def _keeps_batch_records(dossier, original_batches, merged, cache):
    """The shared cell quota cannot displace a record available before merging."""
    if dossier is None:
        return True
    available = {row['id']: row for row in _batch_patient(dossier, merged, cache)['cellRecords']}
    return all(
        available.get(row['id']) == row
        for batch in original_batches
        for row in _batch_patient(dossier, batch, cache)['cellRecords']
    )


def _batches(targets, *, evidence=None, chapter_code=None, library_cache=None, dossier=None, patient_cache=None,
             repair=False):
    """Cells of the same table stay together; long tables are split."""
    planned = dossier is not None and dossier.planned
    limit = (4 if repair else 6) if planned else _BATCH_CELLS
    groups = {}
    for target in targets:
        path = target['path']
        key = (path[0], path[1] if len(path) > 1 else '')
        if path[0] == '_assessment_editorial':
            key = (path[0], 'interpretation')
            if (target.get('owner') or {}).get('contentPlanVersion'):
                scope = path[1].partition('.')[0]
                if scope in {'body', 'system', 'priority', 'specialtyRow', 'layer', 'management', 'synthesisAction'}:
                    key = (path[0], scope)
        if path[0] == 'medical_assessment' and path[1] == 'findings':
            key = (path[0], 'findings')
        if path[0] == 'comprehensive_conclusion' and path[1] in {'next_steps', 'priority_directions'}:
            key = (path[0], 'plan')
        stage = 0
        if _summary_inputs(target):
            stage = 2 if (target.get('owner') or {}).get('contentScopeCode') == 'clinicalLead' else 1
        groups.setdefault((*key, stage), []).append(target)
    result = []
    for key, rows in groups.items():
        size = limit
        whole_rows = key[1] in {'findings', 'plan'} and key[0] in {'medical_assessment', 'comprehensive_conclusion'}
        summary_rows = all((target.get('owner') or {}).get('aggregate') for target in rows) and all(
            len(target['path']) > 2 and isinstance(target['path'][2], int) for target in rows
        )
        if whole_rows or summary_rows:
            # The cells of one problem are written together: explanation and
            # management of a finding; follow-up, basis and first measure of a
            # priority (so the short table cells agree with the full text).
            by_row = {}
            for target in rows:
                by_row.setdefault(target['path'][2], []).append(target)
            chunk = []
            for cells in by_row.values():
                if len(chunk) + len(cells) > size and chunk:
                    result.append(chunk)
                    chunk = []
                chunk.extend(cells)
            if chunk:
                result.append(chunk)
            continue
        if rows and rows[0]['path'][0] == 'medical_assessment' and rows[0]['path'][1] == 'trigger_analysis':
            size = 18
        for start in range(0, len(rows), size):
            result.append(rows[start : start + size])
    # Calls contain only dependency-ready targets from one chapter. Related
    # short summaries can therefore share the patient/evidence context while
    # keeping every cell's own job, fact handles, citations and review verdict.
    # Detailed tables retain their existing batches and complete rows.
    cache = library_cache if library_cache is not None else {}
    patients = patient_cache if patient_cache is not None else {}
    combined, pending, originals, chars = [], [], [], 0
    for batch in result:
        pending_is_summary = bool(pending and _summary_inputs(pending[0]))
        small_summary = len(batch) <= _SMALL_SUMMARY_CELLS and all(
            (target.get('owner') or {}).get('aggregate') for target in batch
        )
        length = sum(_spec(target)['max'] for target in batch)
        if not small_summary or (
            pending
            and (
                len(pending) + len(batch) > limit
                or chars + length > _SUMMARY_BATCH_CHARS
                or (planned and batch[0]['path'][0] == '_assessment_editorial'
                    and batch[0]['path'][1].partition('.')[0] != pending[0]['path'][1].partition('.')[0])
                or bool(_summary_inputs(batch[0])) != pending_is_summary
                or any((target.get('owner') or {}).get('contentScopeCode') == 'clinicalLead'
                       for target in [*pending, *batch])
                or not _keeps_batch_evidence(evidence, [*originals, batch], [*pending, *batch], chapter_code, cache)
                or not _keeps_batch_records(dossier, [*originals, batch], [*pending, *batch], patients)
            )
        ):
            if pending:
                combined.append(pending)
                pending, originals, chars = [], [], 0
        if small_summary:
            pending.extend(batch)
            originals.append(batch)
            chars += length
        else:
            combined.append(batch)
    if pending:
        combined.append(pending)
    return combined


async def author_report_chapters(
    client,
    *,
    blueprint,
    evidence,
    subject,
    deadline,
    audit=None,
    previous_reviewed=None,
    context_targets=None,
    on_checkpoint=None,
    model_gate=None,
    prior_review_findings=None,
    authoring_scope=None,
    prior_unresolved=(),
    defer_content_repairs=False,
):
    """Write and review every target of ``blueprint.chapters``; returns blocks by chapter.

    ``context_targets`` maps every target of the whole report (not only this
    phase) so that text reviewed in earlier phases can be shown as context.
    A cell the reviewer could not correct is rewritten once with the
    reviewer's findings and reviewed again; only then is it left unresolved.
    Dependency-ready invocations may share ``model_gate``. A synchronous
    ``on_checkpoint`` receives an isolated copy after each reviewed batch so
    the caller can retain completed work while other chapters are running.
    ``prior_review_findings`` carries substantive review events for internal
    correction context, without promoting them to patient facts or citations.
    ``authoring_scope`` identifies this invocation and its immutable startup
    checkpoint in private calls/journals; it changes no public report fields.
    ``defer_content_repairs`` runs one complete review per batch in each
    coordinated public sweep.
    """
    result = deepcopy(previous_reviewed or {})
    journal = audit if isinstance(audit, dict) else {}
    scope = deepcopy(authoring_scope or {})
    journal['authoringScope'] = scope
    journal.setdefault('writerUnresolved', [])
    dossier = _Dossier(blueprint, subject)
    known_numbers = dossier.numbers()
    all_targets = dict(context_targets or {})
    all_targets.update({target['id']: target for chapter in blueprint.chapters for target in chapter['targets']})
    gate = model_gate if model_gate is not None else asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY)
    batch_slots = asyncio.Semaphore(EVALUATE_MODEL_CONCURRENCY) if dossier.planned else None
    lock = asyncio.Lock()
    timings = journal.setdefault('writerTimings', [])
    library_cache = {}
    patient_cache = {}
    startup_reviewed = deepcopy(result)
    earlier_findings = _review_findings(prior_review_findings, None)
    earlier_unresolved = {row['targetId']: row for row in prior_unresolved if row.get('targetId')}
    contributor_ids = {}
    if dossier.planned:
        contributors = [(target['id'], (target.get('owner') or {}).get('contentScopeCode') or '',
                         set(target.get('sourceFactIds', ()))) for target in all_targets.values()]
        for target in all_targets.values():
            scopes = _summary_inputs(target)
            if scopes:
                facts = set(target.get('sourceFactIds', ()))
                contributor_ids[target['id']] = tuple(
                    identifier for identifier, code, own_facts in contributors
                    if identifier != target['id'] and code.startswith(scopes) and facts.intersection(own_facts)
                )
    complete = {}

    async def call(payload, *, chapter, batch, service_limit=None):
        queued = monotonic()
        started = None
        status = 'failed'
        try:
            async with gate:
                started = monotonic()
                remaining = None if deadline is None else deadline - started
                if remaining is not None and remaining <= 0:
                    raise TimeoutError('report_writer_deadline')
                if service_limit is not None:
                    remaining = service_limit if remaining is None else min(remaining, service_limit)
                async with asyncio.timeout(remaining):
                    response = await client.generate(payload)
                status = 'success'
                return response
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        except TimeoutError:
            status = 'timeout'
            raise
        finally:
            finished = monotonic()
            timings.append(
                {
                    'stage': 'model_call',
                    'authoringScope': deepcopy(scope),
                    'task': payload.get('task'),
                    **({'reviewPhase': payload['reviewPhase']} if payload.get('reviewPhase') else {}),
                    'chapter': chapter,
                    'batch': batch,
                    'queuedAtMonotonic': queued,
                    'startedAtMonotonic': started,
                    'finishedAtMonotonic': finished,
                    'gateWaitMs': round(((started if started is not None else finished) - queued) * 1000, 3),
                    'serviceMs': round((finished - started) * 1000, 3) if started is not None else 0,
                    'status': status,
                }
            )

    async def run_batch(chapter, batch, index, ready_at):
        batch_ids = {target['id'] for target in batch}
        inputs = {identifier for target in batch for identifier in contributor_ids.get(target['id'], ())}
        for identifier in inputs - batch_ids:
            if identifier in complete:
                await complete[identifier].wait()
        batch_started = monotonic()
        batch_timing = {
            'stage': 'batch',
            'authoringScope': deepcopy(scope),
            'chapter': chapter['code'],
            'batch': index,
            'cells': len(batch),
            'readyAtMonotonic': ready_at,
            'startedAtMonotonic': batch_started,
            'finishedAtMonotonic': None,
            'status': 'running',
        }
        timings.append(batch_timing)
        try:
            if batch_slots is None:
                await write_batch(chapter, batch, index, batch_timing, inputs)
            else:
                async with batch_slots:
                    batch_timing['admittedAtMonotonic'] = monotonic()
                    await write_batch(chapter, batch, index, batch_timing, inputs)
        except asyncio.CancelledError:
            batch_timing['status'] = 'cancelled'
            raise
        except Exception:
            batch_timing['status'] = 'failed'
            raise
        finally:
            finished = monotonic()
            batch_timing['finishedAtMonotonic'] = finished
            batch_timing['durationMs'] = round((finished - batch_started) * 1000, 3)
            if batch_timing['status'] == 'running':
                batch_timing['status'] = 'completed'
            for identifier in batch_ids:
                complete[identifier].set()

    async def write_batch(chapter, batch, index, batch_timing, inputs):
        async def batch_call(payload, *, service_limit=None):
            return await call(payload, chapter=chapter['code'], batch=index, service_limit=service_limit)

        handles = {target['id']: f'C{position:02d}' for position, target in enumerate(batch, 1)}
        by_handle = {handle: all_targets[tid] for tid, handle in handles.items()}
        specs = {handle: _spec(target) for handle, target in by_handle.items()}
        library = _batch_library(evidence, batch, chapter['code'], library_cache)
        final = chapter['code'] in {'healthOverview', 'comprehensiveConclusion'}
        aggregate = any((target.get('owner') or {}).get('aggregate') for target in batch)
        if dossier.planned:
            # Summaries see only their completed, reviewed contributors.
            # Individual table rows still own their stable original records.
            earlier = result if inputs else startup_reviewed
            context_budget = 2400 if inputs else 0
        else:
            earlier = result
            context_budget = 30000 if final else 16000 if aggregate or chapter['code'] == 'functionalPathways' else 0
        context = _context(
            earlier,
            all_targets,
            chapter,
            set(handles),
            budget=context_budget,
            allowed_facts={fid for target in batch for fid in target.get('sourceFactIds', ())}
            if dossier.planned else None,
            inputs=inputs or None,
        ) if context_budget else []
        relevant_findings = [
            row for row in earlier_findings
            if row.get('targetId') in handles and row.get('targetId') not in earlier_unresolved
        ]
        cells = {}
        for target in batch:
            handle = handles[target['id']]
            spec, owner = specs[handle], target.get('owner') or {}
            cell = {
                'id': handle,
                'item': owner.get('name'),
                'column': spec['label'],
                'question': spec['question'],
                'form': spec['form'],
            }
            if spec['form'] == 'choice':
                cell['allowed'] = spec['allowed']
            else:
                cell['guide'] = spec.get('guide')
                cell['length'] = {'min': spec['min'], 'max': spec['max']}
                if spec.get('outline'):
                    cell['outline'] = spec['outline']
            if source_row := owner.get('sourceRow'):
                cell['row'] = source_row
            elif owner.get('aggregate'):
                # A summary reads the reviewed cells of its scope (previouslyWritten)
                # and the records in cellRecords, not a copy of whole tables.
                reader_scope = {key: owner[key] for key in ('actualPersonalProblems', 'summaryMode') if owner.get(key)}
                if reader_scope:
                    cell['scope'] = reader_scope
            else:
                row = _row(blueprint, target)
                if row:
                    cell['row'] = row
            if tuple(target['path'][:2]) == ('health_overview', 'overall_score'):
                # The explanation of the radar needs the radar itself.
                dimensions = _score_rows(blueprint)
                if dimensions:
                    cell['row'] = {'dimensions': dimensions}
            if owner.get('kind') == 'disease_source_model':
                cell['nature'] = '与该病相关的肠道菌群特征，不是本人已患疾病'
            cell['recordIds'] = [
                dossier.handles[fid] for fid in owner.get('primaryFactIds', ())[:12] if fid in dossier.handles
            ]
            if dossier.planned:
                from .assessment_microbiome_view import source_semantic_context
                from .assessment_source_context import model_source_semantics
                from .assessment_source_context import source_context_handles

                for field in ('templateDuty', 'contextPurpose', 'cellRelations', 'clinicalSelections',
                              'temporalRelations', 'comparisonClaims',
                              'actionSourceDuty', 'actionBasisScope', 'conditionality', 'noNewPrerequisite',
                              'sourceAdviceScopes', 'unscopedAdvice'):
                    if owner.get(field):
                        cell[field] = source_context_handles(owner[field], dossier.handles)
                cell['primaryRole'] = owner.get('requiredRole') or 'interpretation'
                if owner.get('usesReviewedFactsOnly'):
                    cell['usesReviewedFactsOnly'] = True
                if owner.get('microbiomeDates'):
                    cell['microbiomeDates'] = owner['microbiomeDates']
                if owner.get('requiresPersonalAction'):
                    cell['requiresPersonalAction'] = True
                if any(part.get('role') == 'action' for part in target.get('sourceSupplement', ())):
                    cell['hasSeparateOriginalAdvice'] = True
                if 'actionBasisFactIds' in owner:
                    cell['actionBasisRecordIds'] = [dossier.handles[fid] for fid in owner['actionBasisFactIds']
                                                   if fid in dossier.handles]
                cell['recordIds'] = [dossier.handles[fid] for fid in owner.get('primaryFactIds', ())
                                     if fid in dossier.handles]
                cell['contextRecordIds'] = [dossier.handles[fid] for fid in owner.get('contextFactIds', ())
                                            if fid in dossier.handles]
                if reviewed := reviewed_cell_sources(target, result, all_targets, dossier.handles):
                    cell['reviewedReadings'] = reviewed
                cell['requiredRecordGroups'] = [
                    [dossier.handles[fid] for fid in identifiers if fid in dossier.handles]
                    for identifiers in owner.get('requiredFactGroups', ())
                ]
                cell['allowedPassageIds'] = [pid for pid, permitted in library.allowed_targets.items()
                                            if target['id'] in permitted]
                cell['passageRoles'] = {pid: sorted(values[target['id']])
                                       for pid, values in library.allowed_roles.items() if target['id'] in values}
                cell['allowedRoles'] = list(target.get('allowedRoles') or ('interpretation',))
                if owner.get('sourceClinicalActions'):
                    cell['sourceClinicalActions'] = [
                        _source_handles({**{key: deepcopy(action[key]) for key in (
                            'code', 'name', 'focusLabel', 'originalAdvice', 'conditionality', 'noNewPrerequisite',
                            'sourceAdviceScopes', 'unscopedAdvice')
                            if key in action},
                         'recordIds': [dossier.handles[fid] for fid in action.get('sourceFactIds', ())
                                       if fid in dossier.handles]}, dossier)
                        for action in owner['sourceClinicalActions']
                    ]
                if owner.get('originalAdvice'):
                    cell['originalClinicalAdvice'] = list(owner['originalAdvice'])
                if owner.get('ruleConclusions'):
                    cell['ruleConclusions'] = model_rule_conclusions(owner['ruleConclusions'], dossier.handles)
                if owner.get('sourceSemantics'):
                    cell['sourceSemantics'] = [
                        model_source_semantics({**source_semantic_context(row),
                                                'recordIds': row.get('recordIds', [])}, dossier.handles)
                        for row in owner['sourceSemantics']
                    ]
                if owner.get('sourceSelections'):
                    cell['sourceSelections'] = [
                        {'recordId': dossier.handles[item['recordId']], 'quote': item['quote']}
                        for item in owner['sourceSelections'] if item.get('recordId') in dossier.handles
                    ]
                if owner.get('actionContext'):
                    action_context = deepcopy(owner['actionContext'])
                    for field in ('baselineFactIds', 'symptomFactIds', 'clinicalConstraintFactIds'):
                        action_context[field] = [dossier.handles[fid] for fid in action_context.get(field, ())
                                                 if fid in dossier.handles]
                    for action in action_context.get('originalAdvice', ()):
                        action['recordIds'] = [dossier.handles[fid] for fid in action.pop('sourceFactIds', ())
                                               if fid in dossier.handles]
                    if action_context.get('activityConstraintGroups'):
                        action_context['activityConstraintGroups'] = _source_handles(
                            action_context['activityConstraintGroups'], dossier,
                        )
                    cell['actionContext'] = _source_handles(action_context, dossier)
                cell['sourceContract'] = model_source_contract(
                    _planned_source_contract(target, cell, dossier), dossier.source_observation_registry,
                )
                previous_review = earlier_unresolved.get(target['id'], {})
                if previous := previous_review.get('lastCandidate'):
                    allowed_records = set(cell['recordIds']) | set(cell['contextRecordIds'])
                    cell['priorDraft'] = [
                        {
                            'text': part['text'],
                            'role': part.get('role'),
                            'recordIds': [record for record in part.get('factIds', ()) if record in allowed_records],
                            'sourceReview': previous_review.get('sourceParagraphStatus', {}).get(f'P{index:02d}'),
                        }
                        for index, part in enumerate(previous, 1) if part.get('text')
                    ]
            cells[handle] = cell
        six_chapter = any((target.get('owner') or {}).get('presentation') == 'assessment.v1' for target in batch)
        brief = _CHAPTER_BRIEF.get(chapter['code'], chapter.get('purpose'))
        if six_chapter:
            brief = _SIX_CHAPTER_BRIEF.get(chapter['code'], brief)
        planned = six_chapter and dossier.planned
        if planned:
            brief = chapter.get('purpose') or brief
            if final:
                brief = ('按已审来源计划形成全局判断，以核准记录中的共同目标、优先顺序和行动范围为准。'
                         '概览只概述关键结论，管理章节写清行动与复评。' + str(brief or ''))
        base = {
            'authoringScope': deepcopy(scope),
            'chapter': {
                'title': chapter['title'],
                'brief': brief,
            },
            'patient': _batch_patient(dossier, batch, patient_cache),
            'evidence': library.entries,
            'previouslyWritten': context,
        }
        for handle, cell in cells.items():
            cell['comparisonScope'] = _comparison_scope(by_handle[handle], dossier, base['patient'])
        context_text = ' '.join(row['text'] for row in context)

        def draft_records(owner, paragraphs):
            mentioned = (dossier.reverse.get(record_id) for part in paragraphs
                         for record_id in part.get('factIds', ()))
            return [dossier.facts[fid] for fid in dict.fromkeys([*owner.get('primaryFactIds', ()), *mentioned])
                    if fid in dossier.facts]

        def clinically_supported(handle, paragraphs):
            from .clinical_report import retain_supported_clinical_sentences

            owner = by_handle[handle].get('owner') or {}
            if planned:
                return paragraphs
            if owner.get('presentation') != 'assessment.v1':
                return paragraphs
            records = draft_records(owner, paragraphs)
            advice = owner.get('originalAdvice') or ()
            return [{**part, 'text': supported} for part in paragraphs
                    if (supported := retain_supported_clinical_sentences(part['text'], records, source_advice=advice))]

        def check_draft(handle, paragraphs, *, strict=False):
            target = by_handle[handle]
            target_id = target['id']
            owner = target.get('owner') or {}
            if planned:
                if (cells[handle].get('retainedCoverage') and owner.get('requiredRole') == 'interpretation'
                        and not any(part.get('role') == 'interpretation' for part in paragraphs)):
                    return '保留的段落还只有原值或行动。请按本格题目补写由实际来源支持的判断。'
                if owner.get('requiresPersonalAction') and not any(part.get('role') == 'action' for part in paragraphs):
                    return '本格需要一项确实适用于受检者的行动。把行动放在独立action段，绑定实际个人依据。'
                for part in paragraphs:
                    allowed_facts = set(target.get('sourceFactIds', ()))
                    if not part.get('factIds') or any(
                        dossier.reverse.get(identifier) not in allowed_facts for identifier in part['factIds']
                    ):
                        return '每段factIds须来自本格实际recordIds/contextRecordIds，不省略、借用或补造来源编号。'
                    role = part.get('role')
                    if role not in (target.get('allowedRoles') or ('interpretation',)):
                        return 'paragraph.role须显式填写本格allowedRoles之一；观察、解释与建议分别标识。'
                    rules = {row['id']: row for row in cells[handle].get('ruleConclusions', ())}
                    for pid in part['cite']:
                        if is_rule_handle(pid):
                            if pid not in rules:
                                return '本段cite中的规则编号不属于本格ruleConclusions；只引用本格列出的R。'
                            triggers = set(rules[pid].get('recordIds') or ())
                            if triggers and not triggers.intersection(part['factIds']):
                                return ('引用规则结论R的段落须在factIds绑定触发该规则的记录：'
                                        '受检者的发现和测值由F支持，R只支持时限、科室、检查、补查项目或目标值。')
                            continue
                        roles = library.allowed_roles.get(pid, {}).get(target_id, set())
                        if role not in roles:
                            return ('本段引文不支持该对象或用途。核对allowedPassageIds和passageRoles，'
                                    '机制/意义与行动需要不同用途时分段。病例原值及原医嘱可以仅绑定recordIds。')
            elif any(pid in library.allowed_targets and target_id not in library.allowed_targets[pid]
                     for part in paragraphs for pid in part['cite'] if not is_rule_handle(pid)):
                return '引用对象、指标或适用人群不匹配；删去该推断或使用适用的原文证据。'
            prose = ' '.join(part['text'] for part in paragraphs)
            if not planned and any(dossier.uses_unverified_gene_label(
                part['text'], part['factIds'], source_observation=planned and part.get('role') == 'observation',
            ) for part in paragraphs):
                return ('来源类别名仅可在observation中绑定其实际原记录作原报告列项；'
                        '机制或临床解释使用已检出符号及已验证注释，不据类别推断菌株、活性毒素或感染。')
            if planned:
                used = {dossier.reverse[handle] for part in paragraphs for handle in part.get('factIds', ())
                        if handle in dossier.reverse}
                primary = set(owner.get('primaryFactIds', ()))
                if primary and not primary.intersection(used):
                    return '正文必须明确绑定本格主对象的实际recordIds，不能只引用补充背景。'
                if any(not set(group).intersection(used) for group in owner.get('requiredFactGroups', ())):
                    return '按requiredRecordGroups分别绑定本格必需的实际记录，不能用背景替代实测或主诉。'
                code = owner.get('contentScopeCode') or ''
                if (code.startswith('symptomEvidence.') and code.endswith('.microbiome')
                        and len(_FUNCTION_SCORE.findall(prose)) > 1):
                    return '本栏前面已有分数。用简短结论说明菌群预测与本项主诉的实际关系和限定。'
                if code.startswith(('structure.', 'function.', 'organisms.')) and owner.get('microbiomeDates'):
                    actual = owner['microbiomeDates'].get('sampleDate')
                    if any((match['after'] or match['before']) != actual
                           for match in _SAMPLING_DATE_CLAIM.finditer(prose)):
                        return '收样或报告日期不能写成采样日期，采样时间只采用microbiomeDates.sampleDate。'
                return _check(specs[handle], paragraphs, strict=strict)
            if _uses_other_panel_taxon(prose, target.get('organismPanelAliases')):
                return '只说明该面板实际列明的菌群和方向，删去来自其他面板的菌群。'
            owner_ids = owner.get('primaryFactIds', ())
            if mirror := target.get('sharedMicrobiomePanel'):
                panel_handle = dossier.handles.get(mirror['factId'])
                if panel_handle is None or not any(panel_handle in row['factIds'] for row in paragraphs):
                    return '先陈述该组合的实测评定，并为该句选用组合评定本身的事实ID。'
            if owner.get('presentation') == 'assessment.v1':
                from .clinical_report import clinical_prose_issue

                relevant = draft_records(owner, paragraphs)
                if issue := clinical_prose_issue(prose, relevant, source_advice=owner.get('originalAdvice') or ()):
                    return issue
            assessment_code = specs[handle].get('assessmentCode')
            panel_owner = owner.get('code') in {'barrierRelatedBacteria', 'scfaProducerBacteria', 'beneficialBacteria'}
            panel_scope = specs[handle].get('assessmentScope') in {'structure', 'organisms'}
            if (panel_owner or panel_scope) and _PANEL_ATTRIBUTION.search(prose):
                return '按来源分别陈述组合评定和分项所见；不要将组合评定归因为某一分项的结果。'
            if assessment_code == 'function.neurotransmitter' and re.search(
                r'(?:神经)?递质[^。！？；]{0,5}前体', prose,
            ):
                return '按表内项目解释相关递质的菌群合成通路，不将这些检测对象改写为递质前体。'
            if assessment_code in {
                'clinicalRow.organMetabolism.abdominalImaging', 'digestive.hepatobiliaryPancreatic',
            } and re.search(
                r'胆囊[^。！？；]{0,20}手术(?:界值|阈值|标准)|手术(?:界值|阈值|标准)', prose,
            ):
                return '只解释这次胆囊影像实际显示的形态、尺寸及安排，省去不适用于个人的统一手术界值。'
            if owner.get('presentation') == 'assessment.v1' and re.search(
                r'(?:残粒(?:胆固醇|指标)|\bRemnant[- ]?C\b)[^。！？；]{0,10}(?:偏高|升高|超标)',
                prose,
                re.I,
            ) and not any(
                re.search(r'残粒|remnant', str(record.get('name') or ''), re.I)
                and (record.get('referenceRange') or record.get('reference_range'))
                for record in draft_records(owner, paragraphs)
            ):
                return '残粒胆固醇的实测或计算值可作风险评估参考；无已采用且适用的参照时，不将该项直接写成偏高。'
            if assessment_code and (assessment_code == 'microbiomeLead' or assessment_code.startswith('organisms.')):
                facts = draft_records(owner, paragraphs)
                for fact in facts:
                    value = str(fact.get('value') or '')
                    status = str(fact.get('status') or '')
                    name = re.split(r'[（(]| / ', str(fact.get('name') or ''), maxsplit=1)[0]
                    if (len(name) > 1 and fact.get('kind') == 'microbe'
                            and re.fullmatch(r'\s*0+(?:\.0+)?\s*%?\s*', value)
                            and (value.endswith('%') or fact.get('unit') == '%')
                            and not re.search(r'未检出(?!异常)|^阴性$|^not_detected$', status, re.I)
                            and re.search(rf'{re.escape(name)}[^。！？；]{{0,22}}(?:未检出|低于(?:检出|检测)限)',
                                          prose)):
                        return '该菌只记录了零丰度或偏低。保留测值及原有状态，删去未检出或低于检出限的推断。'
            if assessment_code == 'structure.composition' and re.search(r'肠型|菌型', prose) and re.search(
                r'方向一致|相互(?:印证|支持)|指向同一|两者吻合',
                prose,
            ):
                return '按检测所见分别表述菌门比例和肠型。省去互相印证或同方向的推断。'
            if specs[handle].get('assessmentScope') == 'function' and re.search(
                r'(?:血液?|血清|血浆|临床化验|体检|实测)[^。！？；]{0,40}'
                r'(?:方向(?:一致|相同|相反)|相互(?:印证|支持)|相吻合|相互呼应|同向)',
                prose,
            ):
                return '临床化验和本行菌群推测功能的检测对象不同。省去仅凭名称和升降方向建立的关联。'
            if specs[handle].get('assessmentCode') == 'digestive.oralGastric':
                negative_signs = set(_DENTAL_NEGATIVE.findall(prose))
                record = ' '.join(str(dossier.facts.get(fid, {}).get('value') or '') for fid in owner_ids)
                if negative_signs.difference(_DENTAL_NEGATIVE.findall(record)):
                    return '删除口腔检查未明示的具体阴性所见。保留记录中的牙齿或牙周发现及其实际意义。'
            return _check(specs[handle], paragraphs, strict=strict)
        accepted, form_errors, verdicts, pre_review_issues, last_candidates = {}, {}, {}, {}, {}
        last_receipts, last_reviewed_sources = {}, {}
        contract_audits = {}
        grounded_versions = {}
        paragraph_reviews = {}

        async def publish(unresolved):
            blocks = [_block(by_handle[handle], rows, dossier, library, specs[handle])
                      for handle, rows in accepted.items()
                      if not planned or specs[handle]['form'] != 'paragraph' and not specs[handle].get('reviewed')
                      or grounded_versions.get(handle) == paragraph_version(rows)]
            async with lock:
                bucket = {block['targetId']: block for block in result.get(chapter['code'], [])}
                bucket.update({block['targetId']: block for block in blocks})
                result[chapter['code']] = list(bucket.values())
                journal['writerUnresolved'].extend(unresolved)
                journal['reviewedBlocks'] = deepcopy(result)
                if on_checkpoint is not None:
                    on_checkpoint(chapter['code'], deepcopy(result[chapter['code']]), deepcopy(unresolved))

        async def draft(pending, feedback):
            """Use one draft in a public sweep. Compatibility can repair its form immediately."""
            drafts, problems, candidates = {}, {}, {}
            for attempt in range(1 if planned and defer_content_repairs else 2):
                todo = [{**cells[handle], **({'sourceContract': medical_source_contract(
                    cells[handle]['sourceContract'],
                )} if planned else {})} for handle in pending if handle not in drafts]
                if not todo:
                    break
                if attempt:
                    humanized['redrafted'] = humanized.get('redrafted', 0) + len(todo)
                response = await batch_call(
                    {
                        'task': 'write_report_chapter',
                        **base,
                        'cells': todo,
                        'requiredCellIds': [cell['id'] for cell in todo],
                        'writingGuide': (_PLANNED_WRITING_GUIDE if planned else
                            [*WRITING_GUIDE, *_ASSESSMENT_WRITING_GUIDE] if six_chapter else WRITING_GUIDE),
                        'repair': [{'id': handle, 'problem': problem} for handle, problem in problems.items()],
                        'reviewFindings': [
                            {'id': handle, 'issues': issues} for handle, issues in feedback.items() if handle in pending
                        ],
                        'priorReviewFindings': relevant_findings if planned else
                        _review_findings(prior_review_findings, journal.get('writer')),
                        'outputSchema': {
                            'cells': [
                                {
                                    'id': 'C01',
                                    'paragraphs': [
                                        {
                                            'title': '条目小标题，2至8个字；没有outline或form不是paragraph时留空',
                                            'text': '这一条的正文，不重复title；choice只填allowed中的一个值，'
                                                    'label只填短语',
                                            'factIds': ['本段用到的patient记录id，如F012'],
                                            'cite': ['本段引用的evidence段落pid（如E3.1）或本格ruleConclusions的'
                                                     '规则编号（如R01）；没有引用则为空数组'],
                                            **({'role': 'observation|interpretation|action，选本段实际用途'}
                                               if planned else {}),
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
                rows = response.get('cells') if isinstance(response, dict) else None
                for row in rows if isinstance(rows, list) else ():
                    handle = row.get('id') if isinstance(row, dict) else None
                    if handle not in pending or handle in drafts:
                        continue
                    paragraphs = _paragraphs(row, clean_prose=not planned)
                    if paragraphs:
                        candidates[handle] = paragraphs
                        last_candidates[handle] = deepcopy(paragraphs)
                        paragraph_reviews.pop(handle, None)
                    problem = check_draft(handle, paragraphs, strict=attempt == 0)
                    if problem and attempt:
                        supported = clinically_supported(handle, paragraphs)
                        if supported and supported != paragraphs and check_draft(handle, supported) is None:
                            paragraphs, problem = supported, None
                    if problem:
                        length = sum(len(part['text']) for part in paragraphs)
                        problems[handle] = (
                            f'{problem}（当前{length}字，要求{specs[handle]["min"]}–{specs[handle]["max"]}字）'
                            if problem in {'too_long', 'too_short'}
                            else '没有按条目书写：请按outline拆成若干条，每条填写title，每条正文不超过130字，内容不减少'
                            if problem == 'not_itemized'
                            else '格式不符：请严格按guide规定的分段数书写，各段用全角分号隔开，第一段只写时限'
                            if problem == 'bad_format'
                            else problem
                        )
                    else:
                        drafts[handle] = paragraphs
                        problems.pop(handle, None)
                        pre_review_issues.pop(handle, None)
                for handle in pending:
                    if handle not in drafts:
                        problems.setdefault(handle, 'missing')
            # A source-contract problem is substantive review work too. Give
            # the independent reviewer the complete candidate and the exact
            # failed contract, rather than dropping it before medical review.
            # Publication still requires the same strict check afterwards.
            if planned:
                for handle, paragraphs in candidates.items():
                    if handle not in drafts and specs[handle]['form'] == 'paragraph':
                        drafts[handle] = paragraphs
                        pre_review_issues[handle] = problems.get(handle, 'source_contract')
            return drafts, problems

        async def ground_final(medical_final):
            """Ground a whole final batch; format repair never rewrites prose."""
            needed = {handle: rows for handle, rows in medical_final.items()
                      if specs[handle]['form'] == 'paragraph' or specs[handle].get('reviewed')}
            kept = {handle: rows for handle, rows in medical_final.items() if handle not in needed}
            if not needed:
                return kept, {}
            ground_patient = dossier.view([
                [dossier.reverse[rid] for part in rows for rid in part.get('factIds', ())
                 if rid in dossier.reverse] for rows in needed.values()
            ], limit=len(dossier.facts))
            records = [record for key in ('clinicalRecords', 'selfReported', 'cellRecords')
                       for record in ground_patient.get(key, ())]
            clinical_actions = _source_handles(
                getattr(blueprint, 'assessment_sources', {}).get('clinicalActions', ()), dossier,
            )

            def frame_for(paragraph_map):
                return build_source_frame(
                    cells, paragraph_map, registry=dossier.source_observation_registry,
                    records=records, evidence=base['evidence'], subject=dossier.person,
                    clinical_actions=clinical_actions,
                )

            frame, private = frame_for(needed)
            server_repairs = {}
            # A recalled specimen event without the explicit unconfirmed timing
            # relation fails grounding deterministically. Add the fixed sentence
            # before the independent review sees the text.
            for handle, requirements in private['cells'].items():
                if pids := recall_timing_targets(requirements):
                    updated, changed = add_recall_timing_note(needed[handle], pids)
                    if changed:
                        needed[handle] = updated
                        server_repairs[handle] = [{'repair': 'recall_timing', 'paragraphs': changed}]
            if server_repairs:
                frame, private = frame_for(needed)
            pending = dict(needed)
            semantic_rewrite, feedback, previous_proofs = {}, {}, {}
            for handle, requirements in private['cells'].items():
                literal_errors = {
                    pid: issues for pid, issues in requirements.get('literalIssues', {}).items() if issues
                }
                if not literal_errors or len(literal_errors) < len(needed[handle]):
                    continue
                # The same literal error fails validate_source_proofs even
                # with a supported verdict. Send it to the writer directly.
                pending.pop(handle, None)
                last_candidates[handle] = deepcopy(needed[handle])
                paragraph_reviews[handle] = {pid: 'source_error' for pid in literal_errors}
                semantic_rewrite[handle] = [
                    {'type': 'unsupported_claim', 'detail': f'{pid}：{issue}'}
                    for pid, issues in literal_errors.items() for issue in issues
                ]
                verdicts[handle] = {'verdict': 'withheld', 'issues': semantic_rewrite[handle]}
                contract_audits[handle] = {
                    'stage': 'literal_preflight', 'versionHash': requirements['versionHash'],
                    'semanticIssues': deepcopy(semantic_rewrite[handle]),
                }
            for attempt in range(2):
                if not pending:
                    break
                scoped_frame = {**frame, 'cells': [cell for cell in frame['cells'] if cell['id'] in pending]}
                visible_observations = {oid for cell in scoped_frame['cells'] for oid in cell['observationIds']}
                scoped_frame['observations'] = [row for row in frame['observations']
                                                if row['id'] in visible_observations]
                scoped_private = {**private, 'cells': {handle: private['cells'][handle] for handle in pending}}
                payload = {
                    'task': 'review_report_chapter',
                    'reviewPhase': 'source_ground' if attempt == 0 else 'source_proof_repair',
                    'chapter': base['chapter'], 'frame': scoped_frame,
                    'groundingGuide': GROUNDING_GUIDE,
                    'outputSchema': proof_output_schema(),
                    'authoringScope': deepcopy(scope),
                }
                if attempt:
                    payload['proofRepair'] = [
                        {'id': handle, 'formatIssues': feedback[handle],
                         'previousProof': previous_proofs.get(handle)} for handle in pending
                    ]
                try:
                    response = await batch_call(
                        payload, service_limit=_SOURCE_PROOF_REPAIR_SECONDS if attempt else None,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    response = {}
                    feedback = {handle: [{'type': 'source_proof', 'location': 'call',
                                         'detail': type(error).__name__}] for handle in pending}
                checked = validate_source_proofs(scoped_frame, scoped_private, response)
                for handle, issues in list(checked['semanticIssues'].items()):
                    # Receipt-only findings (an action written inside an
                    # interpretation, an uncited action keeping background E,
                    # an unstated recall timing) are repaired by the server and
                    # the repaired text is validated again against the same
                    # reviewer decisions. Any content finding still goes back.
                    if handle in checked['formatIssues'] or handle not in checked['receipts']:
                        continue
                    repaired = repair_receipt_findings(needed[handle], issues, private['cells'][handle])
                    if repaired is None or check_draft(handle, repaired[0]) is not None:
                        continue
                    new_paragraphs, notes, origins = repaired
                    repaired_frame, repaired_private = frame_for({handle: new_paragraphs})
                    receipt = repaired_receipt(
                        checked['receipts'][handle], private['cells'][handle],
                        repaired_private['cells'][handle], origins,
                    )
                    if receipt is None:
                        continue
                    receipt['id'] = handle
                    again = validate_source_proofs(repaired_frame, repaired_private, {'cells': [receipt]})
                    if handle not in again['accepted']:
                        continue
                    needed[handle] = new_paragraphs
                    private['cells'][handle] = repaired_private['cells'][handle]
                    checked['semanticIssues'].pop(handle)
                    checked['accepted'].append(handle)
                    checked['proofs'][handle] = {
                        **again['proofs'][handle], 'serverRepairs': notes,
                        'reviewerReceipt': deepcopy(checked['receipts'][handle]),
                    }
                    checked['paragraphAudit'][handle] = again['paragraphAudit'].get(handle, {})
                    server_repairs.setdefault(handle, []).extend(notes)
                returned = response.get('cells') if isinstance(response, dict) else []
                for proof in returned if isinstance(returned, list) else ():
                    if isinstance(proof, dict) and proof.get('id') in pending:
                        previous_proofs[proof['id']] = deepcopy(proof)
                for handle in pending:
                    last_candidates[handle] = deepcopy(needed[handle])
                    if handle in checked['receipts']:
                        last_receipts[handle] = deepcopy(checked['receipts'][handle])
                        last_reviewed_sources[handle] = private['cells'][handle]
                    if outcomes := checked.get('paragraphAudit', {}).get(handle):
                        paragraph_reviews[handle] = outcomes
                    entry = contract_audits.setdefault(handle, {'stage': 'immutable_source_grounding', 'attempts': []})
                    entry['attempts'].append({
                        'attempt': attempt, 'versionHash': private['cells'][handle]['versionHash'],
                        **({'receiptCorrections': deepcopy(checked['receiptCorrections'][handle])}
                           if handle in checked['receiptCorrections'] else {}),
                        **({'serverRepairs': deepcopy(server_repairs[handle])} if handle in server_repairs else {}),
                        'formatIssues': deepcopy(checked['formatIssues'].get(handle, [])),
                        'semanticIssues': deepcopy(checked['semanticIssues'].get(handle, [])),
                        'paragraphResults': deepcopy(paragraph_reviews.get(handle, {})),
                        'proof': deepcopy(previous_proofs.get(handle)),
                    })
                for handle in checked['accepted']:
                    # Recompute from the server's final paragraphs, not a returned boolean/hash.
                    version = paragraph_version(needed[handle])
                    if version != private['cells'][handle]['versionHash']:
                        checked['formatIssues'][handle] = [{'type': 'source_proof',
                            'location': 'versionHash', 'detail': '医学最终正文在grounding后发生变化。'}]
                        continue
                    kept[handle] = needed[handle]
                    accepted[handle] = needed[handle]
                    grounded_versions[handle] = version
                    contract_audits[handle]['acceptedProof'] = deepcopy(checked['proofs'][handle])
                for handle, issues in checked['semanticIssues'].items():
                    semantic_rewrite[handle] = [
                        {'type': 'unsupported_claim', 'detail': issue['detail']} for issue in issues
                    ]
                    verdicts[handle] = {'verdict': 'withheld', 'issues': semantic_rewrite[handle]}
                feedback = checked['formatIssues']
                pending = {handle: needed[handle] for handle in pending
                           if handle not in kept and handle not in semantic_rewrite}
                _event(audit, {
                    'authoringScope': deepcopy(scope),
                    'stage': 'source_grounding', 'chapter': chapter['code'], 'batch': index,
                    'attempt': attempt, 'accepted': list(checked['accepted']),
                    'formatIssues': deepcopy(checked['formatIssues']),
                    'semanticIssues': deepcopy(checked['semanticIssues']),
                    'sourceReviews': {
                        handle: {
                            'versionHash': private['cells'][handle]['versionHash'],
                            'paragraphs': deepcopy(private['cells'][handle]['paragraphs']),
                            'proof': deepcopy(previous_proofs.get(handle)),
                        }
                        for handle in scoped_private['cells']
                    },
                })
            for handle in pending:
                verdicts[handle] = {
                    'verdict': 'withheld',
                    'issues': [{'type': 'source_proof_incomplete', 'detail': issue['detail']}
                               for issue in feedback.get(handle, ())],
                }
            return kept, semantic_rewrite

        async def review(drafts, *, retry_missing=True):
            """Return accepted paragraphs and the findings for cells that must be rewritten."""
            # Prose is reviewed, and so is a short cell that states a measure or a result.
            prose = {
                handle: rows
                for handle, rows in drafts.items()
                if specs[handle]['form'] == 'paragraph' or specs[handle].get('reviewed')
            }
            kept = {handle: rows for handle, rows in drafts.items() if handle not in prose}
            rewrite = {}
            if not prose:
                return kept, rewrite
            review_cells = []
            visible_records = {
                row['id']: row for key in ('clinicalRecords', 'selfReported', 'cellRecords')
                for row in base['patient'].get(key, ())
            } if planned else {}
            for handle, rows in prose.items():
                cell = cells[handle]
                hints = _grounding_hints(
                    rows, known_numbers, library, json.dumps(cell.get('row', ''), ensure_ascii=False) + context_text
                    + json.dumps(cell.get('ruleConclusions', []), ensure_ascii=False),
                )
                review_cells.append(
                    {
                        **{
                            key: medical_source_contract(cell[key]) if key == 'sourceContract' else cell[key]
                            for key in (
                                'id',
                                'item',
                                'column',
                                'question',
                                'row',
                                'length',
                                'primaryRole',
                                'requiresPersonalAction',
                                'hasSeparateOriginalAdvice',
                                'usesReviewedFactsOnly',
                                'microbiomeDates',
                                'guide',
                                'outline',
                                'scope',
                                'comparisonScope',
                                'recordIds',
                                'contextRecordIds',
                                'requiredRecordGroups',
                                'sourceSemantics',
                                'sourceClinicalActions',
                                'originalClinicalAdvice',
                                'allowedPassageIds',
                                'passageRoles',
                                'allowedRoles',
                                'actionContext',
                                'sourceSelections',
                                'templateDuty',
                                'reviewedReadings',
                                'contextPurpose',
                                'cellRelations',
                                'clinicalSelections',
                                'temporalRelations',
                                'comparisonClaims',
                                'ruleConclusions',
                                'actionBasisRecordIds',
                                'actionSourceDuty',
                                'actionBasisScope',
                                'conditionality',
                                'noNewPrerequisite',
                                'sourceContract',
                            )
                            if key in cell
                        },
                        'draft': rows,
                        **({'retainedCoverage': cells[handle]['retainedCoverage']}
                           if cells[handle].get('retainedCoverage') else {}),
                        **({'draftSourceFindings': findings} if planned and (
                            findings := draft_source_findings(cell, rows, visible_records)
                        ) else {}),
                        **({'sourceContractIssues': [pre_review_issues[handle]]}
                           if handle in pre_review_issues else {}),
                        **({'numbersNotFoundInRecords': hints} if hints else {}),
                    }
                )
            try:
                response = await batch_call(
                    {
                        'task': 'review_report_chapter',
                        'chapter': base['chapter'],
                        'patient': base['patient'],
                        'evidence': base['evidence'],
                        'previouslyWritten': base['previouslyWritten'],
                        'cells': review_cells,
                        'reviewGuide': (_PLANNED_REVIEW_GUIDE if planned else
                            [*REVIEW_GUIDE, *_ASSESSMENT_REVIEW_GUIDE] if six_chapter else REVIEW_GUIDE),
                        'priorReviewFindings': relevant_findings if planned else
                        _review_findings(prior_review_findings, journal.get('writer')),
                        'outputSchema': {
                            'reviews': [
                                {
                                    'id': 'C01',
                                    'verdict': 'pass|revise',
                                    **({'completeReaderDuty': '仅retainedCoverage格必填，布尔值。'
                                       '删段后的成稿或修订的完整正文是否独立满足原题、必要证据及行动条件？'}
                                       if any(cells[h].get('retainedCoverage') for h in prose) else {}),
                                    'issues': [
                                        {
                                            'type': ('fact_error|unsupported_claim|citation_mismatch|unsafe_advice'
                                                     '|missing_interpretation|off_task'),
                                            'detail': '具体指出哪一句、错在哪里',
                                        }
                                    ],
                                    'revisedParagraphs': [
                                        {
                                            'title': '原条目的小标题，保持不变',
                                            'text': '改好的这一条正文，不重复title',
                                            'factIds': ['F012'],
                                            'cite': ['E3.1', 'R01'],
                                            **({'role': 'observation|interpretation|action，保留或修正本段实际用途'}
                                               if planned else {}),
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Review is a required gate: unreviewed prose is not published.
                for handle in prose:
                    verdicts[handle] = {
                        'verdict': 'withheld',
                        'issues': [{'type': 'review_unavailable', 'detail': type(error).__name__}],
                    }
                return kept, rewrite
            rows = response.get('reviews') if isinstance(response, dict) else None
            seen = set()
            for row in rows if isinstance(rows, list) else ():
                handle = row.get('id') if isinstance(row, dict) else None
                if handle not in prose or handle in seen:
                    continue
                seen.add(handle)
                issues = (
                    [issue for issue in row.get('issues', ()) if isinstance(issue, dict)]
                    if isinstance(row.get('issues'), list)
                    else []
                )
                source_problem = check_draft(handle, prose[handle])
                if cells[handle].get('retainedCoverage') and row.get('completeReaderDuty') is not True:
                    rewrite[handle] = [*issues, {'type': 'missing_interpretation',
                        'detail': '保留段不足以完成本格或缺少完整性审核。下次针对未完成的判断、依据或行动补写。'}]
                    verdicts[handle] = {'verdict': 'withheld', 'issues': rewrite[handle]}
                    continue
                if (row.get('verdict') == 'pass' and not issues and source_problem is None
                        and (not planned or handle not in pre_review_issues)):
                    kept[handle] = prose[handle]
                    verdicts[handle] = {'verdict': 'pass'}
                    continue
                if source_problem:
                    issues.append({'type': 'source_contract', 'detail': source_problem})
                elif planned and handle in pre_review_issues:
                    issues.append({'type': 'source_contract', 'detail': pre_review_issues[handle]})
                revised = _paragraphs({'paragraphs': row.get('revisedParagraphs')}, clean_prose=not planned)
                if not planned:
                    revised = _merge_revision(prose[handle], revised)
                if revised and check_draft(handle, revised):
                    revised = clinically_supported(handle, revised)
                revised_problem = check_draft(handle, revised) if revised else None
                if (revised and revised_problem is None
                        and not (planned and handle in pre_review_issues and revised == prose[handle])):
                    kept[handle] = revised
                    verdicts[handle] = {'verdict': 'revised', 'issues': issues}
                elif source_problem or (planned and handle in pre_review_issues) or any(
                    issue.get('type') in _SERIOUS for issue in issues
                ) or (
                    row.get('revisedParagraphs') and not revised
                ):
                    rewrite[handle] = issues
                    if revised_problem and revised_problem != source_problem:
                        issues.append({'type': 'source_contract', 'detail': revised_problem})
                    verdicts[handle] = {'verdict': 'withheld', 'issues': issues}
                else:
                    kept[handle] = prose[handle]
                    verdicts[handle] = {'verdict': 'kept_with_minor_issues', 'issues': issues}
            missing = {handle: rows for handle, rows in prose.items() if handle not in seen}
            if missing and retry_missing:
                retried, remaining = await review(missing, retry_missing=False)
                kept.update(retried)
                rewrite.update(remaining)
            elif missing:
                for handle in missing:
                    verdicts[handle] = {'verdict': 'withheld', 'issues': [{'type': 'review_missing', 'detail': ''}]}
            return kept, rewrite

        async def humanize():
            """One style pass over the reviewed prose; meaning is checked, not trusted."""
            # Only text in which the detector found a pattern is rewritten; text
            # that is already clean keeps its reviewed wording and costs no call.
            prose = {
                handle: rows
                for handle, rows in accepted.items()
                if specs[handle]['form'] == 'paragraph' and any(ai_tells(row['text']) for row in rows)
            }
            if not prose:
                return
            payload_cells = [
                {
                    'id': handle,
                    'item': cells[handle].get('item'),
                    'column': cells[handle].get('column'),
                    'paragraphs': [
                        {'index': index, 'text': row['text'], 'tells': ai_tells(row['text'])}
                        for index, row in enumerate(rows)
                    ],
                }
                for handle, rows in prose.items()
            ]
            try:
                response = await batch_call(
                    {
                        'task': 'humanize_report_chapter',
                        'chapter': base['chapter'],
                        'cells': payload_cells,
                        'humanizeGuide': HUMANIZE_GUIDE,
                        'outputSchema': {
                            'cells': [{'id': 'C01', 'paragraphs': [{'index': 0, 'text': '改写后的完整段落'}]}]
                        },
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:  # style is optional; reviewed text stands
                humanized['failed'] = type(error).__name__
                return
            rows = response.get('cells') if isinstance(response, dict) else None
            for row in rows if isinstance(rows, list) else ():
                handle = row.get('id') if isinstance(row, dict) else None
                if handle not in prose or not isinstance(row.get('paragraphs'), list):
                    continue
                updated = [dict(part) for part in prose[handle]]
                for part in row['paragraphs']:
                    index = part.get('index') if isinstance(part, dict) else None
                    if not isinstance(index, int) or not 0 <= index < len(updated):
                        continue
                    text = reader_text(re.sub(r'[ \t]*\n+[ \t]*', '', str(part.get('text') or '').strip()), prose=True)
                    if text == updated[index]['text']:
                        continue
                    if _faithful_rewrite(updated[index]['text'], text):
                        updated[index]['text'] = text
                        humanized['changed'] += 1
                    else:
                        humanized['rejected'] += 1
                if check_draft(handle, updated) is None:
                    accepted[handle] = updated

        humanized = {'changed': 0, 'rejected': 0}
        try:
            pending = list(cells)
            feedback = {}
            for handle, target in by_handle.items():
                earlier = earlier_unresolved.get(target['id'])
                if earlier is not None:
                    feedback[handle] = [
                        {'detail': detail, 'type': kind}
                        for detail, kind in dict.fromkeys(
                            (_portable_review_text(issue['detail']), issue.get('type'))
                            for issue in earlier.get('issues', ())
                            if isinstance(issue, dict) and issue.get('detail')
                        )
                    ]
            for round_index in range(1 if planned and defer_content_repairs else 2):
                # A held draft with stable patient bindings is still a useful
                # editable manuscript. On the first repair sweep let the
                # medical editor correct that exact text against the failures,
                # then send the revision to the independent grounding review.
                # Rebind passages by their publication and exact quote.
                editable = {}
                if planned and scope.get('phase') in {'public_repair', 'public_final_repair'} and round_index == 0:
                    for handle in pending:
                        previous = earlier_unresolved.get(by_handle[handle]['id'], {})
                        candidate = previous.get('lastCandidate') or []
                        permitted = set(cells[handle]['recordIds']) | set(cells[handle]['contextRecordIds'])
                        supported = [
                            part for pos, part in enumerate(candidate, 1)
                            if previous.get('sourceParagraphStatus', {}).get(f'P{pos:02d}') == 'supported'
                        ]
                        if 0 < len(supported) < len(candidate):
                            rebound = []
                            for part in supported:
                                if not part.get('factIds') or not set(part['factIds']) <= permitted:
                                    continue
                                saved = previous.get('passageAnchors', {})
                                current_rules = rule_conclusion_map(
                                    (by_handle[handle].get('owner') or {}).get('ruleConclusions'))
                                cites = [
                                    (old if old in current_rules else None) if is_rule_handle(old) else
                                    next((pid for pid, value in library.passages.items()
                                          if list(value) == saved.get(old)
                                          and part['role'] in cells[handle]['passageRoles'].get(pid, ())), None)
                                    for old in part.get('cite', ())
                                ]
                                if None not in cites:
                                    rebound.append({**deepcopy(part), 'cite': cites})
                            if rebound:
                                cells[handle]['retainedCoverage'] = (
                                    '已通过逐段医学及来源核查的原文只作候选。以原题和全部必需依据逐项判断，'
                                    '缺少主判断、比较对象、适用条件或必要行动时，在revisedParagraphs补足。'
                                    '无法成立时标明缺什么；只在最终交付整格独立完成职责时填completeReaderDuty=true。'
                                )
                                editable[handle] = rebound
                                last_candidates[handle] = deepcopy(rebound)
                                continue
                        if scope.get('phase') != 'public_repair':
                            continue
                        if not candidate or not all(
                            part.get('text') and part.get('factIds')
                            and set(part.get('cite') or ()) <= set(rule_conclusion_map(
                                (by_handle[handle].get('owner') or {}).get('ruleConclusions')))
                            and set(part['factIds']) <= permitted for part in candidate
                        ):
                            continue
                        editable[handle] = deepcopy(candidate)
                        last_candidates[handle] = deepcopy(candidate)
                        issues = [row['detail'] for row in feedback.get(handle, ()) if row.get('detail')]
                        if current := check_draft(handle, candidate, strict=True):
                            issues.append(current)
                        if issues:
                            pre_review_issues[handle] = '；'.join(dict.fromkeys(issues))
                drafts, problems = await draft([handle for handle in pending if handle not in editable], feedback)
                drafts.update(editable)
                form_errors.update(problems)
                kept, rewrite = await review(drafts)
                for handle in kept:
                    form_errors.pop(handle, None)
                if planned and kept:
                    kept, source_rewrite = await ground_final(kept)
                    rewrite.update(source_rewrite)
                accepted.update(kept)
                _event(
                    audit,
                    {
                        'stage': 'round',
                        'authoringScope': deepcopy(scope),
                        'chapter': chapter['code'],
                        'batch': index,
                        'round': round_index,
                        'cells': len(pending),
                        'drafted': len(drafts),
                        'accepted': len(kept),
                        'rewrite': list(rewrite),
                        'formErrors': dict(problems),
                        'evidenceEntries': len(library.entries),
                    },
                )
                pending, feedback = list(rewrite), rewrite
                if not pending:
                    break
            if not planned:
                # Planned cells are published exactly as medically reviewed.
                # A later stylistic rewrite has no authority to change them.
                await humanize()
            _event(
                audit,
                {
                    'stage': 'review',
                    'authoringScope': deepcopy(scope),
                    'chapter': chapter['code'],
                    'batch': index,
                    'verdicts': {handle: row['verdict'] for handle, row in verdicts.items()},
                    'issues': {handle: row.get('issues') for handle, row in verdicts.items() if row.get('issues')},
                    **({'sourceContractReviews': deepcopy(contract_audits)} if planned else {}),
                    'targets': {
                        handle: {
                            'targetId': by_handle[handle]['id'],
                            'item': cells[handle].get('item'),
                            'primaryFactIds': list((by_handle[handle].get('owner') or {}).get('primaryFactIds', ())),
                            **(
                                {'reviewedText': '\n'.join(part['text'] for part in accepted[handle])}
                                if row.get('issues') and handle in accepted
                                else {}
                            ),
                        }
                        for handle, row in verdicts.items()
                    },
                    'humanized': dict(humanized),
                },
            )
        except asyncio.CancelledError:
            batch_timing['status'] = 'cancelled'
            if accepted:
                # Losing an optional rewrite or style pass must not discard
                # paragraphs already approved by the medical reviewer.
                await publish([])
            raise
        except Exception as error:
            batch_timing['status'] = 'failed'
            _event(
                audit,
                {
                    'stage': 'batch_failed',
                    'authoringScope': deepcopy(scope),
                    'chapter': chapter['code'],
                    'batch': index,
                    'errorType': type(error).__name__,
                    'accepted': len(accepted),
                },
            )
            form_errors.update({handle: type(error).__name__ for handle in cells if handle not in accepted})

        def salvage(handle):
            """Reviewed sentences of an unresolved cell that still pass every deterministic check.

            Used only if no later sweep publishes the cell. The kept text is a
            subset of the medically reviewed and source-checked candidate; it
            is validated again with the reviewer's own decisions.
            """
            candidate, receipt = last_candidates.get(handle), last_receipts.get(handle)
            reviewed = last_reviewed_sources.get(handle)
            if (not planned or specs[handle]['form'] != 'paragraph' or not candidate or receipt is None
                    or reviewed is None or paragraph_version(candidate) != reviewed['versionHash']):
                return None
            kept, removed, origins = retained_paragraphs(
                candidate, paragraph_reviews.get(handle) or {}, verdicts.get(handle, {}).get('issues') or [],
            )
            if not kept or kept == candidate or check_draft(handle, kept) is not None:
                return None
            owner = by_handle[handle].get('owner') or {}
            if any(re.search(r'\d', str(dossier.facts.get(fid, {}).get('value') or ''))
                   for fid in owner.get('primaryFactIds', ())) and not has_number(kept):
                return None
            view = dossier.view([[dossier.reverse[rid] for part in kept for rid in part.get('factIds', ())
                                  if rid in dossier.reverse]], limit=len(dossier.facts))
            retained_frame, retained_private = build_source_frame(
                cells, {handle: kept}, registry=dossier.source_observation_registry,
                records=[record for key in ('clinicalRecords', 'selfReported', 'cellRecords')
                         for record in view.get(key, ())],
                evidence=base['evidence'], subject=dossier.person,
                clinical_actions=_source_handles(
                    getattr(blueprint, 'assessment_sources', {}).get('clinicalActions', ()), dossier,
                ),
            )
            synthesized = repaired_receipt(receipt, reviewed, retained_private['cells'][handle], origins)
            if synthesized is None:
                return None
            synthesized['id'] = handle
            if handle not in validate_source_proofs(retained_frame, retained_private, {'cells': [synthesized]})[
                'accepted'
            ]:
                return None
            return {
                'block': _block(by_handle[handle], kept, dossier, library, specs[handle]),
                'removed': removed,
                'phase': scope.get('phase'),
            }

        salvaged = {}
        for handle in cells:
            if handle in accepted:
                continue
            try:
                if (kept := salvage(handle)) is not None:
                    salvaged[handle] = kept
            except Exception as error:  # retention is optional; the cell stays unresolved
                logger.warning('report sentence retention skipped: error_type=%s', type(error).__name__)
        unresolved = [
            {
                'chapterCode': chapter['code'],
                'targetId': target['id'],
                'item': (target.get('owner') or {}).get('name'),
                'issues': [
                    *verdicts.get(handles[target['id']], {}).get('issues', ()),
                    *([{'type': 'authoring_form',
                        'detail': form_errors[handles[target['id']]]}]
                      if handles[target['id']] in form_errors else []),
                ],
                'reason': (
                    verdicts.get(handles[target['id']], {}).get('verdict')
                    if handles[target['id']] in verdicts and verdicts[handles[target['id']]]['verdict'] == 'withheld'
                    else form_errors.get(handles[target['id']], 'missing')
                ),
                **({'lastCandidate': last_candidates[handles[target['id']]],
                    'passageAnchors': {
                        pid: list(library.passages[pid]) for part in last_candidates[handles[target['id']]]
                        for pid in part.get('cite', ()) if pid in library.passages
                    },
                    'sourceParagraphStatus': paragraph_reviews.get(handles[target['id']], {}),
                    'sourceContract': {
                        field: deepcopy(cells[handles[target['id']]][field])
                        for field in ('recordIds', 'contextRecordIds', 'requiredRecordGroups',
                                      'allowedPassageIds', 'passageRoles', 'allowedRoles')
                        if field in cells[handles[target['id']]]
                    }} if planned and handles[target['id']] in last_candidates else {}),
                **({'salvage': salvaged[handles[target['id']]]} if handles[target['id']] in salvaged else {}),
            }
            for target in batch
            if handles[target['id']] not in accepted
        ]
        logger.info(
            'report chapter batch: chapter=%s batch=%s cells=%s accepted=%s unresolved=%s evidence=%s',
            chapter['code'],
            index,
            len(cells),
            len(accepted),
            len(unresolved),
            len(library.entries),
        )
        await publish(unresolved)

    chapter_batches = []
    for chapter in blueprint.chapters:
        done = {block['targetId'] for block in result.get(chapter['code'], [])}
        targets = [target for target in chapter['targets'] if target['id'] not in done]
        batches = _batches(
            targets,
            evidence=evidence,
            chapter_code=chapter['code'],
            library_cache=library_cache,
            dossier=dossier,
            patient_cache=patient_cache,
            repair='repair' in str(scope.get('phase') or ''),
        )
        if dossier.planned:
            batches.sort(key=lambda batch: min(target.get('authoringPriority', 2) for target in batch))
        chapter_batches.append([
            (chapter, batch, index)
            for index, batch in enumerate(batches, 1)
        ])
    # At most one complete publication chain per batch slot. Interleaving
    # chapters prevents all initial slots going to a single long chapter.
    jobs = [item for row in zip_longest(*chapter_batches) for item in row if item is not None]
    if dossier.planned and len(jobs) > 1:
        # A batch whose reviewed cells feed later summaries takes a slot
        # first; otherwise a long summary chain can start after the slots
        # were spent on cells nothing waits for.
        owner_job = {target['id']: position for position, (_, batch, _) in enumerate(jobs) for target in batch}
        dependents = {position: set() for position in range(len(jobs))}
        for position, (_, batch, _) in enumerate(jobs):
            for target in batch:
                for identifier in contributor_ids.get(target['id'], ()):
                    source = owner_job.get(identifier)
                    if source is not None and source != position:
                        dependents[source].add(position)
        depth = {}

        def chain_depth(position, visiting=()):
            if position not in depth:
                depth[position] = 1 + max(
                    (chain_depth(child, (*visiting, position)) for child in dependents[position]
                     if child not in visiting),
                    default=0,
                )
            return depth[position]

        order = sorted(range(len(jobs)), key=lambda position: (-chain_depth(position), position))
        jobs = [jobs[position] for position in order]
    complete.update((target['id'], asyncio.Event()) for _, batch, _ in jobs for target in batch)
    work = [run_batch(chapter, batch, index, monotonic()) for chapter, batch, index in jobs]
    if work:
        tasks = [asyncio.create_task(batch) for batch in work]
        try:
            await asyncio.gather(*tasks)
        finally:
            # gather propagates a preparation/checkpoint failure without
            # stopping its other children. Retain the reviewed checkpoint,
            # but do not leave model requests or late writes running.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    return result


def mirror_duplicate_taxa(data):
    """Show one taxon's reviewed interpretation on its other panel rows."""
    try:
        taxa = data.microbiome_assessment.taxa
    except AttributeError:
        return 0
    written = {}
    for row in taxa:
        key = (row.scientific_name or row.name or '').casefold()
        if key and row.explanation:
            written.setdefault(key, row)
    copied = 0
    for row in taxa:
        key = (row.scientific_name or row.name or '').casefold()
        origin = written.get(key)
        if origin is not None and origin is not row and not row.explanation and row.value == origin.value:
            row.explanation = origin.explanation
            row.evidence_refs = list(origin.evidence_refs)
            copied += 1
    return copied


__all__ = ['REVIEW_GUIDE', 'WRITER_REVISION', 'WRITING_GUIDE', 'author_report_chapters', 'mirror_duplicate_taxa']
