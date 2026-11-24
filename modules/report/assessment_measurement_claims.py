"""Source-bound candidates for focused review of ambiguous clinical claims.

Lexical matches identify work for the existing immutable source review; they
do not decide negation, anaphora or a sentence's clinical speech act. The
server checks the classified claim against its actual source authority.
No function in this module edits report text or patient measurements.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from decimal import InvalidOperation

from .assessment_clinical_observation import _CONDITION
from .assessment_clinical_view import _reference_direction

_ANALYTES = (
    (r'(?:血清|血浆)?(?:葡萄糖|血糖)|glucose|glu|fpg', ('葡萄糖', '血糖', 'glucose', 'GLU'), 'FPG'),
    (r'(?:血清)?胰岛素|insulin|ins|fins', ('胰岛素', 'insulin', 'INS'), 'FINS'),
    (r'(?:血清)?c[- ]?肽|c[- ]?peptide', ('C肽', 'C-肽', 'C-peptide', 'C peptide'), None),
    (r'(?:血清|血浆)?甘油三[酯脂](?:tg)?|triglycerides?|tg', ('甘油三酯', '甘油三脂', 'triglycerides', 'TG'), None),
)
_NUMBER = re.compile(r'(?<![A-Za-z\d.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?', re.I)
_ACTIONS = (
    r'复查|复核|复评|复测|复诊|核对|重测|随[访诊]|就诊|就医|转诊|咨询|安排|完善|检查|检测|化验|评估|筛查|'
    r'完成|决定|观察|记录|开展|监测|比较|结肠镜|ogtt|增加|减少|减轻|减重|控制|戒酒|限酒|戒烟|步行|运动|'
    r'补充|服用|摄入|调整|起步|起身|避免|维持'
)
_DIRECTIVE = r'建议|推荐|需(?:要)?(?!求)|应(?:当|予)?|宜|不宜|请|务必|必须|(?:可|先)(?:先|考虑|结合|由)?'
_ADVICE_SUBJECT = r'按(?:照)?(?:原|医)|原(?:报告|医嘱|医生|建议)|(?:临床|原检|体检)医生'
_ATTRIBUTION = re.compile(
    r'按(?:照)?(?:原(?:建议|医嘱|报告)|医嘱)|原(?:医嘱|建议)|'
    r'(?:原(?:报告|医生|医师)|主检|原检|设备|体检报告)(?:原文|报告|医师)?(?:原)?(?:建议|要求|提出)',
    re.I,
)
_NEW_ACTION = re.compile(r'(?:本报告|本次评估)(?:另|也|还)?(?:建议|提出)|(?:另|另行|额外)建议')
_ADVICE_ORDER = re.compile(
    r'(?:按|依|依据)[^。；;]{0,12}(?:原建议|原医嘱|医生建议)[^。；;]{0,10}'
    r'(?:划分|分为|分成|分)(?P<levels>[一二两三四五六七八九十\d]+[层组级])',
)
_ACTION = re.compile(rf'(?:{_DIRECTIVE}|{_ADVICE_SUBJECT})[^。！？；;\n]{{0,42}}(?:{_ACTIONS})', re.I)
_IMPERATIVE = re.compile(
    rf'^\s*(?:应|先|再|仍|优先|逐步|适度|及时|尽[早快]|定期|继续|必要时|结合原件)*\s*'
    rf'(?:{_ACTIONS}|查(?=血|尿|便|肝|甲|超声|b超)|至[^，,、。；;]{{1,24}}(?:科|门诊))',
    re.I,
)
_SOURCE_TIME = re.compile(
    r'(?P<prefix>每隔|间隔|每|满|至少间隔|[第隔])?\s*(?P<n>[0-9]+|[一二两三四五六七八九十半]+)\s*'
    r'(?P<u>个?月|年|周|天|小时)\s*(?P<suffix>内|后)?(?![\d日])'
)
_TIME_ACTION = re.compile(rf'{_SOURCE_TIME.pattern}[^，,、。；;\n]{{0,20}}(?:复查|复测|复诊|随[访诊])', re.I)
_NAMED_FOLLOWUP = re.compile(r'(?:定期|按期|如期|按时)\s*(?:复查|复测|复诊|随[访诊])')
_OBJECT_FIRST_RECHECK = re.compile(r'^[^，,、。；;\n]{1,24}(?:复查|复测|重测|复诊|随[访诊])\s*$')
_DELEGATED_ACTION = re.compile(
    r'(?:由|至|请到|前往)[^，,、。；;\n]{1,30}(?:门诊|专科|内科|外科|营养科|康复科)'
    r'|(?:内科|外科|门诊|专科|营养科|康复科)\s*(?:复核|核对|随[访诊]|咨询|就诊|评估|决定)',
    re.I,
)
_ACTION_SEPARATOR = re.compile(
    r'[，,、](?=\s*[^，,、。；;])|(?:并(?:且)?|以及|及|同时|随后|另外|另行|进而)(?=[^，,、。；;\n])',
)
_CONDITION_START = re.compile(r'^\s*(?:在[^，,]+时|若|如果|如(?=[^何])|一旦|当|必要时|存在.{1,14}时)')
_CONDITION_SCOPE = re.compile(r'(?:若|如果|如(?=[^何])|一旦|当|仅在|必要时)[^。；;]{0,38}')
_FOLLOWUP_GATE = re.compile(r'(?:复查|复测|复评|检查|观察)[^。；;]{0,32}(?:后再|之后再|仍.{0,8}再|才|方可)')
_RESULT_ACTION = re.compile(
    r'(?:复查|复测|复检|复评)(?:结果|后)?[^,，。；;]{0,5}?(?P<result>阳性|阴性|异常|升高|降低|偏高|偏低)'
    r'(?:时|者|后)?\s*[,，]?\s*(?:才|则|方可|再|请|建议|需|可|应|进一步|进行)*'
    r'(?P<action>查因|查明(?:病因|原因)|检查(?:病因|原因)|排查(?:病因|原因)|'
    r'(?:到|前往|至)?[^,，。；;]{0,12}?(?:就诊|就医|转诊|复诊))'
)
_DIRECTNESS_NOTE = re.compile(
    r'(?:不(?:以|用)|不(?:可|能|宜)用|无需用).{1,24}(?:替代|代替)(?P<outcome>.+)|'
    r'不(?:把|将)(?P<target>.{1,16}?)(?:改为|设为|限定为).{1,32}(?:条件|门槛|前提)',
)
_ADVICE_STRENGTH = re.compile(r'(?:完成|务必|必做|必须|需要进行|要求完成)')
_STATEMENT = re.compile(r'[。！？；;\n]+|\.(?=\s|$)')
_ACTION_HEADING = re.compile(r'^\s*(?P<heading>[^：:\n]{2,36})[：:]\s*(?P<rest>.*)$', re.S)
_SPECIALTIES = (
    r'(?:心血管(?:内|专)?|心内)科',
    r'神经内科',
    r'消化(?:内|专)?科',
    r'内分泌(?:内|专)?科',
    r'肝病科|脂肪肝专科',
    r'肝胆外科',
    r'甲状腺外科',
    r'呼吸(?:内)?科',
    r'康复科',
    r'骨科',
    r'泌尿外科',
    r'眼科',
    r'口腔科',
    r'营养(?:专)?科|临床营养(?:门诊)?',
    r'肛肠(?:外)?科',
)
_PROCEDURES = (
    r'大便常规|粪便常规',
    r'(?:粪便|大便)?隐血|(?<![a-z])(?:ob|fobt|fit)(?![a-z])',
    r'口服葡萄糖耐量(?:试验|检查)?|(?<![a-z])ogtt(?![a-z])',
    r'(?:电子|无痛)?(?:结肠镜|肠镜)',
    r'(?:电子|无痛)?胃镜',
    r'(?:胃肠镜|胃肠内镜)',
    r'肝(?:脏)?(?:彩超|超声|b超)',
    r'胆囊(?:彩超|超声|b超)',
    r'肝胆(?:彩超|超声|b超)',
    r'肝(?:脏)?功能|肝功',
    r'甲状腺(?:彩超|超声|b超)',
    r'甲状腺功能|甲功',
    r'(?<![a-z])ezscan(?![a-z])',
    r'血清孕酮|孕酮',
    r'直肠指检|肛检',
)
_PROCEDURAL_ACTIONS = (
    r'复查|复测|复检|复验|重测',
    r'复核|核对|统一.{0,8}(?:摘要|结果|小结)',
    r'随[访诊]|复诊',
    r'就[诊医]|转诊|咨询|会诊|前往|至[^，。；;]{1,20}(?:科|门诊)',
    r'进行|安排|完成|开展|接受',
)
_EXACT_ADVICE_ALIASES = (*_PROCEDURES, *_SPECIALTIES, r'随访|随诊', r'复查|复检')
_CRITERION_BOUNDARY = re.compile(r'[。！？；;\n]+|\.(?=\s|$)|[，,]\s*(?=但|而|相较|相比|与此不同)')
_DEFINITE_COLLECTION = re.compile(
    r'(?:该次|这次|此次|当次|同次|同一|当日|当时)[^，,。！？；;\n]{0,16}'
    r'(?:标本|样本|采样|留样|留取|采集)|'
    r'(?:该|此|这|上述|前述)(?:份|次|一|个)?\s*(?:标本|样本|留样|采样|留取|采集)'
)
_COLLECTION_SCOPE = re.compile(r'标本|样本|采样|留样|留取|采集')
_RECALLED_COLLECTION = re.compile(
    r'(?:回顾|回忆|自述|问卷|(?:本人|受检者|患者)(?:称|表示|报告))'
    r'[^，,。！？；;\n]{0,28}(?:采样|留样|留取|采集|取样|送检)|'
    r'(?:采样|留样|留取|采集|取样|送检)[^，,。！？；;\n]{0,17}?(?:时|期间)'
    r'[^，,。！？；;\n]{0,10}(?:自述|回顾|回忆|本人(?:称|报告)|问卷(?:记载|记录))',
)
_BOUND_RECALL = re.compile(
    r'(?:回顾|回忆|自述|问卷|(?:本人|受检者|患者)(?:称|表示|报告))[^，,。！？；;\n]{0,30}'
    r'(?:留取|留样|采集|取样)\s*(?:该|此|这|上述)(?:次|份)?(?:标本|样本)|'
    r'(?:该|此|这|上述)(?:次|份)?(?:标本|样本)[^，,。！？；;\n]{0,13}'
    r'(?:采样|留样|留取|采集|取样|送检)时[^，,。！？；;\n]{0,8}(?:回顾|回忆|自述|问卷)',
)
_SAMPLE_EVENT = re.compile(r'采样|留样|留取|采集|取样|送检')
_AS_SAMPLED = re.compile(r'(?:采样|留样|留取|采集|取样)(?:于|前|后|当时|时)')
_EVENT_CLAUSE = re.compile(r'[，,。！？；;\n]+')
_INDEPENDENT_EVENT = re.compile(
    r'(?:日期|时间|时点|发生日)[^，,。；;]{0,8}(?:不明|未知|未明|未记录|未注明|未提供|不详)|'
    r'(?:未注明|未记录|未提供|未交代)(?:具体|确切|准确)?(?:日期|时间|时点)|'
    r'(?:无从|不能|无法|未能|未可|尚难)[^，,。；;]{0,8}(?:确定|确认|认定|对应|建立)'
    r'[^，,。；;]{0,14}(?:同次|同一|同份|同日|标本对应|关系|关联|日期|时间|时点)|'
    r'(?:关系|关联|同次|同一|是否同份|对应)[^，,。；;]{0,16}(?:不明|未知|未确立|未确定|未建立|未确认)'
)
_ASSERTED_DIFFERENT_EVENT = re.compile(
    r'(?:各属|分别属于|属于|属|为|并非同次而是)\s*(?:另一次|另一回|另一份|不同(?:的)?'
    r'(?:日期|时点|事件|标本|样本|时间段|采集))|'
    r'(?:日期|时点|时间|标本|样本)(?:和[^，,。；;]{0,8})?(?:均|各|确实|实际)?(?:不同|不一致)'
)
_TEMPORAL_JOIN = re.compile(r'(?:同日|当日|当天|同期|同时期|同一时期|同一时间|同次|同一个事件)')
_DATE = re.compile(r'(?<!\d)(?:(?P<year>\d{4})[-/.年])?(?P<month>\d{1,2})(?P<sep>[-/.月])(?P<day>\d{1,2})(?:日)?(?!\d)')
_QUANTITY = (
    rf'(?:\s*(?:测得|结果|评分|得分|分值|为|达|是)?\s*[:：]?\s*\(?\s*(?P<value>{_NUMBER.pattern})'
    r'\s*(?:分(?:\s*[/／]\s*100)?|%|[a-zμ/][a-zμ/\d.]*|[↑↓])?\s*\)?)?'
)
_MEASUREMENT_LIST = re.compile(rf'{_QUANTITY}\s*(?:、|及|和|与|,|，)\s*$')
_GRADE = re.compile(
    rf'{_QUANTITY}\s*(?:\(?\s*(?:分值|评分|均|仍|皆|都|亦|也|等|属于|处于|处在|属|为|在|呈|分别)\s*)*'
    r'(?P<grade>极低|极高|偏低|偏高|正常)(?![值人范参对上下降最]|与|和|或|交界|边界|端点)'
)
_FUNCTION_PANEL_SUFFIX = re.compile(r'(?:合成和分解|合成|分解)?(?:代谢|能力)$')
_FUNCTION_SUBSET = r'(?:其中|部分|多种|多项|若干|数种|数项|一些|个别|少数)'
_FUNCTION_LIST = re.compile(rf'{_QUANTITY}\s*(?:、|及|和|与|,|，)\s*(?:{_FUNCTION_SUBSET}\s*)?$')
_FUNCTION_GRADE = re.compile(
    r'^\s*(?:的|内源|合成|分解|代谢|能力|潜能|功能|预测|分项|所列|全组|全部|各项|均|皆|都|组|\s){0,18}'
    r'(?:评分|得分|分值)\s*(?:均|皆|都|为|属|处于|落在|呈|\s){0,9}'
    r'(?P<grade>极低|极高|偏低|偏高|正常)(?![值人范参对上下降最]|与|和|或|交界|边界|端点)',
)
_DIRECTION = {'↓': '偏低', '↑': '偏高', '↓↓': '极低', '↑↑': '极高'}
_QUOTED_ASSESSMENT = re.compile(r'(?:原报告|主检|小结|摘要|原文|箭头)[^。；;]{0,24}(?:称|写|标|记|判)')
_ACTUAL_GRADE_NEGATION = re.compile(r'(?:不能|不应|不能称|不等于|并非|不代表|不表示|未能|没有)[^。；;，,]{0,15}$')
_REFERENCE_CLAIM = re.compile(
    rf'{_QUANTITY}\s*(?:均|皆|都|仍|各项|各自|分别|原|本项|打印|结果|数值|阴性|或|在|处于|落在|为|属|\s)*'
    r'(?:参考(?:值)?范围|参考区间)(?:内|之内|中)',
)
_REFERENCE_GROUP_CLAIM = re.compile(
    r'^[\s，,：:]*(?:的)?(?:各项|全部项目|全部|各项目|所有项目|全组|全项|均|皆|都|无一例外)'
    r'(?:的|检测|检查|测定|所查|项目|结果|值|均|皆|都|分别|为|属于|在|处于|落在|原|各自|阴性|正常|或|\s)*'
    r'(?:参考(?:值)?范围|参考区间)(?:内|之内|中|(?=$|[，,。；;]))',
)
_LIMITED_FINDING = re.compile(
    r'(?:未见|未发现|未显示|未提示|无)(?P<scale>明显|明确|确切)?'
    r'(?P<object>[\u4e00-\u9fff]{2,27}?)(?P<limit>影像学?征象|影像学?表现|影像提示|征象|迹象|表现)?'
    r'(?=[，,。！？；;、\s]|$)',
)
_FINDING_PREDICATE = re.compile(r'肿大|扩张|狭窄|增厚|增粗|损伤|破坏|中断|变形|受限')
_CALCULATED_ATTRIBUTION = re.compile(
    rf'{_QUANTITY}\s*[（(]?\s*(?:为\s*)?(?:计算值|推算值|计算所得|推算所得|由[^，,。；;]{{1,24}}计算得出)',
)
_BMI_LABEL = r'(?:体重指数(?:\s*\(?\s*bmi\s*\)?)?|(?<![a-z0-9])bmi(?![a-z0-9]))'
_MEASURED_BMI = re.compile(
    rf'(?:实测|实际测得)(?:的)?\s*{_BMI_LABEL}'
    rf'|{_BMI_LABEL}\s*\(?\s*(?:为\s*)?(?:直接\s*)?实测(?=\s*(?:值|所得|得出|为|[:)]|{_NUMBER.pattern}))'
)
_DERIVATION_SOURCE = r'(?:由|根据|依据|按(?:照)?|用|以|基于)'
_DERIVATION = re.compile(
    rf'(?P<shared>均|都|皆)?{_DERIVATION_SOURCE}(?P<inputs>[^。；;]{{2,120}}?)(?:推算|计算|换算|相减|相除)',
)
_FORMULA_RESULT_START = re.compile(r'\s*(?:可?得(?:出|到)?|出|的)?\s*[,，:：]?\s*')
_FORMULA_VALUE = re.compile(rf'{_QUANTITY}\s*')
_SEPARATE_DERIVATION = re.compile(rf'\s*(?:则|单独|分别|另(?:行)?)?\s*(?:{_DERIVATION_SOURCE}|=)')
_FORMULA_OPERANDS = {
    'tc': r'总胆固醇|(?<![a-z])tc(?![a-z])|\btotal cholesterol\b',
    'tg': r'甘油三[酯脂]|(?<![a-z])tg(?![a-z])|\btriglycerides?\b',
    'hdl': r'(?<![a-z-])hdl[- ]?c?(?![a-z])|高密度脂蛋白(?:胆固醇)?',
    'ldl': r'(?<![a-z-])ldl[- ]?c?(?![a-z])|低密度脂蛋白(?:胆固醇)?',
    'glucose': r'血糖|葡萄糖|\b(?:fpg|glu|glucose)\b',
    'insulin': r'胰岛素|\b(?:fins|insulin)\b',
    'height': r'身高|\bheight\b',
    'weight': r'体重|\bweight\b',
    'waist': r'腰围|\bwaist\b',
    'ast': r'谷草转氨酶|天门冬氨酸?氨基转移酶|\bast\b',
    'alt': r'谷丙转氨酶|丙氨酸氨基转移酶|\balt\b',
    'platelets': r'血小板|\b(?:plt|platelet count)\b',
    'age': r'年龄|\bage\b',
    'sbp': r'收缩压|\bsbp\b',
    'dbp': r'舒张压|\bdbp\b',
}
_ASSAY_RELATIVE_TERMS = re.compile(r'(?:相对丰度|样本内(?:占比|比例))')
_SEQUENCE_TERMS = re.compile(r'(?:序列(?:信号|靶标|检出|阳性|阴性|未检出)|测到序列|(?:检出|检测到)\s*序列)')
_PREVIOUS_RESULTS = re.compile(r'^\s*(?:这些|上述|上列|前述|上述所列|所列|该等|此类|所有)')
_DEFINITION_CLAIM = re.compile(r'(?:均|都是|是|仅|只|属于|属|显示|表示|指|为|代表|意味着)')
_ZERO_INFERENCE = re.compile(
    r'(?:未检出|未检测到|未测到|没有检出|未检测出)(?!异常)|'
    r'(?:低于|在|处于)检测限(?:以下)?|检测限以下|'
    r'体内(?:没有|缺失|不存在)|在体内(?:缺失|不存在|消失)',
)
_NONASSERTION = re.compile(
    r'(?:不能|不应|无法|不得|不等于|并非|不代表|不表示|不支持|不能理解|不作|不能说明|尚不能)'
    r'[^。；;，,]{0,20}$',
)
_HOST_CONCLUSION = re.compile(
    r'(?:提示|反映|表明|说明|显示|印证|可见|支持)[^。；;]{0,8}(?:宿主|人体|个人|受检者)'
    r'[^。；;]{0,16}(?:炎症|免疫)[^。；;]{0,12}(?:同步|一致|状况|状态|背景|功能|正常|异常|良好|减弱|增强)',
)
_CRITERION = re.compile(
    rf'^[ \t]*(?:(?:水平为|值|水平|浓度|levels?|values?|concentrations?|of)[ \t]*)?\|?[ \t]*'
    rf'(?:[≤≥<>]|不低于|不高于|超过|低于|至少|至多|at least|at most)[ \t]*(?:=[ \t]*)?(?P<value>{_NUMBER.pattern})',
    re.I,
)
_CRITERION_USE = re.compile(r'指南|共识|标准|阈值|界值|组分|切点|criteria|threshold|cut[- ]?off', re.I)
_CRITERION_LINK = re.compile(r'达到|符合|满足|超过|高于|低于|低下|未达|纳入|计入|属于|用于判定|meet|above|below', re.I)
_CONDITION_LABELS = {'fasting': '空腹', 'non_fasting': '非空腹', 'postprandial': '餐后'}

CLAIM_USES = {
    'acquisition_condition': ('observed_measurement', 'proposed_test', 'denied_or_general'),
    'external_threshold': ('applied_threshold', 'not_applied'),
    'patient_action': ('patient_management', 'reported_advice', 'rule_conclusion', 'mixed_action', 'not_action'),
    'event_relation': ('established_link', 'unestablished_or_other_event'),
}


def _normal(text):
    return unicodedata.normalize('NFKC', '' if text is None else str(text)).casefold()


def _decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(_normal(value).strip().rstrip('%').strip())
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _aliases(observation):
    label = _normal(observation.get('name') or observation.get('sourceLabel'))
    base = re.sub(r'\([^)]*\)', '', _CONDITION.sub('', label)).strip()
    specimen = _normal((observation.get('acquisitionContext') or {}).get('specimen'))
    if not re.search(r'尿|urine', label + specimen):
        for pattern, names, abbreviation in _ANALYTES:
            if re.fullmatch(pattern, re.sub(r'\s+', '', base), re.I) or (
                abbreviation and label == abbreviation.casefold()
            ):
                return tuple(_normal(name) for name in names), abbreviation
    return ((base,), None) if len(base) >= 2 else ((), None)


def _observation_ids(observation):
    return set(observation.get('recordIds') or [observation.get('recordId')]) - {None}


def _spans(text, boundary):
    start = 0
    for match in boundary.finditer(text):
        if start < match.start():
            yield start, text[start : match.start()]
        start = match.end()
    if start < len(text):
        yield start, text[start:]


def _action_signal(text, role):
    return (
        _ACTION.search(text)
        or _IMPERATIVE.match(text)
        or _DELEGATED_ACTION.search(text)
        or _TIME_ACTION.search(text)
        or (
            role == 'action'
            and (
                _NAMED_FOLLOWUP.search(text)
                or _OBJECT_FIRST_RECHECK.match(text)
                or re.search(r'\b(?:ogtt|ezscan)\b|不作|随[访诊]|酌情|逐渐|加量', text, re.I)
            )
        )
    )


def _preserves_directness(term, instruction):
    """A negative restatement of the same advice is not a second action."""
    if not _ATTRIBUTION.search(instruction) or not (note := _DIRECTNESS_NOTE.fullmatch(term.strip())):
        return False
    target = note['outcome'] or note['target']
    if note['outcome']:
        modifier = term[: note.start('outcome')]
    else:
        modifier = term[note.end('target') :]
    if not re.search(r'观察|复查|复测|复检|检查|结果|阳性|异常|等待', modifier):
        return False
    named = _named_action_objects(target, _PROCEDURAL_ACTIONS)
    return bool(named and named.issubset(_named_action_objects(instruction, _PROCEDURAL_ACTIONS)))


def _action_spans(text, role):
    """Split independent actions, preserving lists of tests and conditional scope."""
    advice_heading = ''
    previous_instruction = ''
    for offset, statement in _spans(text, _STATEMENT):
        statement = statement.strip()
        if not statement:
            continue
        if _preserves_directness(statement, previous_instruction):
            continue
        focus = statement
        if heading := _ACTION_HEADING.match(statement):
            if _ATTRIBUTION.search(heading['heading']):
                advice_heading = heading['heading']
            elif _NEW_ACTION.search(heading['heading']):
                advice_heading = ''
            if not heading['rest']:
                continue
            focus = heading['rest'] or statement
        elif _ATTRIBUTION.fullmatch(statement):
            advice_heading = statement
            continue
        if _NEW_ACTION.match(focus):
            advice_heading = ''
        for separator in re.finditer(r'[，,]', focus):
            leading = focus[: separator.start()]
            preceding = advice_heading + statement[: statement.index(focus)] + leading
            if _preserves_directness(focus[separator.end() :], preceding):
                statement = statement[: statement.index(focus) + separator.start()]
                focus = leading
                break
        if not _action_signal(focus, role):
            previous_instruction = ''
            continue
        context = f'{advice_heading}：{statement}' if advice_heading and advice_heading not in statement else statement
        previous_instruction = context
        start = 0
        for separator in _ACTION_SEPARATOR.finditer(focus):
            left, right = focus[start : separator.start()], focus[separator.end() :]
            if not _action_signal(right, role) or _CONDITION_START.match(left) and not _ACTION.search(left):
                continue
            if not _action_signal(left, role):
                continue
            # Simple references to the next evaluation authority are still
            # attached to their preceding condition, even across a comma.
            if _CONDITION_START.match(right) and not _DELEGATED_ACTION.search(right):
                continue
            yield offset + statement.index(focus) + start, left.strip(), context
            start = separator.end()
        if remaining := focus[start:].strip():
            yield offset + statement.index(focus) + start, remaining, context


def _numeric_interval(text):
    chinese = dict(zip('一二三四五六七八九', '123456789', strict=True)) | {'两': '2', '半': '0.5'}
    intervals = set()
    for match in _SOURCE_TIME.finditer(text):
        if not (match['prefix'] or match['suffix'] or _IMPERATIVE.match(text[match.end() :])):
            continue
        number = match['n']
        if '十' in number:
            tens, units = number.split('十', 1)
            number = str(int(chinese.get(tens, '1')) * 10 + int(chinese.get(units, '0')))
        elif not number.isascii():
            number = chinese.get(number)
        value = _decimal(number)
        if value is None:
            continue
        unit = {'年': '月', '个月': '月', '周': '天'}.get(match['u'], match['u'])
        multiplier = {'年': 12, '周': 7}.get(match['u'], 1)
        intervals.add((value * multiplier, unit))
    return intervals


def _named_action_objects(text, patterns):
    return {index for index, pattern in enumerate(patterns) if re.search(pattern, text, re.I)}


def action_terms(text):
    """Recognized specialties, procedures and explicit time limits named in ``text``.

    Returns ``(specialties, procedures, intervals)``; the first two are indexes
    into the shared vocabularies, intervals are ``(amount, unit)`` pairs.
    """
    normal = _normal(text)
    return (
        _named_action_objects(normal, _SPECIALTIES),
        _named_action_objects(normal, _PROCEDURES),
        _numeric_interval(normal),
    )


def _advice_instruction(term, item):
    """Separate a named source from the procedure ordered by that source."""
    term = _normal(term)
    if marker := _ATTRIBUTION.search(term):
        subject = term[: marker.start()].strip(' ：:，,')
        subject = re.sub(r'^(?:仍|继续|还应|也应|应)\s*', '', subject)
        labels = [
            label
            for scope in item.get('sourceAdviceScopes') or ()
            for label in [*(scope.get('sourceRecordLabels') or ()), *(scope.get('objectLabels') or ())]
        ]
        if not subject or any(subject in _normal(label) for label in [*labels, item.get('name')]):
            return term[marker.end() :].strip(' ：:，,')
    return term


def _advice_action_match(term, source, statement):
    """Check literal, demonstrably different restrictions on a single advice."""
    named_specialty = _named_action_objects(term, _SPECIALTIES)
    if not named_specialty.issubset(_named_action_objects(source, _SPECIALTIES)):
        return '转述的专科并非所选A原文明确的专科。'
    procedures = _named_action_objects(term, _PROCEDURES)
    if not procedures.issubset(_named_action_objects(source, _PROCEDURES)):
        return '转述的具体检查项目并非所选A给该处理对象的项目。'
    actions = _named_action_objects(term, _PROCEDURAL_ACTIONS)
    source_actions = _named_action_objects(source, _PROCEDURAL_ACTIONS)
    if actions and source_actions and not actions.issubset(source_actions):
        return '转述改变了所选A的处理动作，需保留复查、复核或就诊的原职责。'
    if _ADVICE_STRENGTH.search(term) and not _ADVICE_STRENGTH.search(source):
        return '原件仅建议该事项，不能转述为原医嘱要求完成。'
    intervals, source_intervals = _numeric_interval(term), _numeric_interval(source)
    if intervals and not intervals.issubset(source_intervals):
        return '转述新增或改变了所选A的明确复查间隔。'
    if source_intervals and not intervals and re.search(r'定期|按时|按期', term):
        return '所选A给出明确时距，不能改写为没有时距的一般定期复查。'
    if any(match['suffix'] == '后' for match in _SOURCE_TIME.finditer(term)) and any(
        (match['prefix'] or '').startswith('每') for match in _SOURCE_TIME.finditer(source)
    ):
        return '原医嘱的重复随访周期不能改成一次性等待后复查。'
    result_action = _RESULT_ACTION.search(term)
    original_result = _RESULT_ACTION.search(source)
    if result_action:
        def outcome(match):
            return ('visit' if re.search(r'就诊|就医|转诊|复诊', match['action']) else 'etiology',
                    match['result'])
        if not original_result or outcome(result_action) != outcome(original_result):
            return '复查结果对应的后续动作或条件与所选原医嘱不同。'
    elif _FOLLOWUP_GATE.search(term) and not _FOLLOWUP_GATE.search(source):
        return '原医嘱未规定此项复查结果是就诊或进一步处理的先决条件。'
    for cue in _CONDITION_SCOPE.finditer(source):
        scope = cue.group()
        shared_action = bool(_CONDITION_START.match(source) and named_specialty) or any(
            action in scope and action in term for action in ('戒酒', '就诊', '就医', '复查', '复诊', '治疗')
        )
        if shared_action:
            condition = _CONDITION_SCOPE.search(statement)
            if condition is None and not result_action:
                return '原医嘱对此动作有明确适用条件，转述不能省略。'
            drinking = re.compile(r'饮酒|喝酒|酒精')
            if drinking.search(scope) and (not condition or not drinking.search(condition.group())):
                return '原医嘱所限为有饮酒情况的人，不能移为另一条件。'
    return None


def advice_authority_issue(candidate, advice, *, inventory=()):
    term, statement = candidate['term'], candidate.get('statement') or candidate['term']
    if ordered := _ADVICE_ORDER.search(term):
        if not any(ordered['levels'] in _normal(item.get('sourceQuote')) for item in advice):
            return '原记录未说明此报告分层；可保留原医嘱项目，另说明本报告组织顺序。'
    known = {
        _normal(label)
        for item in inventory
        for scope in item.get('sourceAdviceScopes') or ()
        for label in scope.get('objectLabels') or ()
        if label
    }
    referenced = {label for label in known if len(label) > 2 and label in _normal(term)}
    covered = {
        _normal(label)
        for item in advice
        for scope in item.get('sourceAdviceScopes') or ()
        for label in scope.get('objectLabels') or ()
        if label
    }
    if referenced and covered and not referenced.issubset(covered):
        return '转述把所选A的原医嘱对象移到同一检查的其他对象；各对象范围应分别核对。'
    if re.search(r'(?:医生|医师|医嘱)', _normal(statement)) and all(
        any(
            re.search(r'设备.{0,24}建议', str(label))
            for scope in item.get('sourceAdviceScopes') or ()
            for label in scope.get('sourceRecordLabels') or ()
        )
        for item in advice
    ):
        return '所选A明确为设备建议，不能转述为临床医师原医嘱。'
    # Advice fragments can include several siblings. A single action may not
    # borrow a specialty, specific test, or interval from another sibling.
    failures = []
    unconditional, complete = [], []
    for item in advice:
        quote = _normal(item.get('sourceQuote'))
        if not quote:
            continue
        spans = list(_action_spans(quote, 'action'))
        for _, source, _ in spans or [(0, quote, quote)]:
            issue = _advice_action_match(_advice_instruction(term, item), source, _normal(statement))
            if issue is None:
                return None
            failures.append(issue)
        unconditional.extend(source for _, source, _ in spans if not _CONDITION_START.match(source))
        complete.append(quote)
    # One retelling may join sibling actions of the selected orders ("复查大便常规
    #及隐血，并至消化内科就诊"). Every named specialty, test, action and interval
    # must still come from those orders, and their conditions still apply.
    for source in ('，'.join(unconditional), '；'.join(complete)):
        if source and len(failures) > 1 and all(
            _advice_action_match(_advice_instruction(term, item), source, _normal(statement)) is None
            for item in advice
            if item.get('sourceQuote')
        ):
            return None
    return failures[0] if failures else None


def _advice_surface(term):
    """Normalize typography and interchangeable labels, leaving all restrictions."""
    term = re.sub(r'^\s*\(?\d+[)）.]\s*', '', _normal(term))
    term = re.sub(r'^\s*(?:建议|请(?:您)?|行)(?!为)', '', term)
    for index, pattern in enumerate(_EXACT_ADVICE_ALIASES):
        term = re.sub(pattern, f'@{index}@', term, flags=re.I)
    return re.sub(r'[ ，、,:："“”‘’（）()\s+；;。]|和|与|及', '', term)


def exact_advice_support(candidate, item, *, inventory=()):
    """Admit automatic A correction only for a uniquely verifiable retelling."""
    scopes = item.get('sourceAdviceScopes') or ()
    admitted = {'explicit_object', 'source_record_scope'}
    if not scopes or any(scope.get('scopeStatus') not in admitted for scope in scopes):
        return False
    if advice_authority_issue(candidate, [item], inventory=inventory):
        return False
    term = _advice_surface(_advice_instruction(candidate['term'], item))
    if not term:
        return False
    quote = item.get('sourceQuote') or ''
    for _, clause, _ in _action_spans(quote, 'action'):
        original = _advice_surface(clause)
        if term == original:
            return True
        # Object-first and action-first are interchangeable only with one
        # recognized test and one check, without any remaining conditions.
        if pair := re.fullmatch(r'@(\d+)@@(\d+)@', term):
            tokens = [int(value) for value in pair.groups()]
            if (
                min(tokens) < len(_PROCEDURES)
                and max(tokens) == len(_EXACT_ADVICE_ALIASES) - 1
                and (original == f'@{tokens[1]}@@{tokens[0]}@')
            ):
                return True
    return False


def _observed_days(value):
    days = set()
    for match in _DATE.finditer(_normal(value)):
        if match['sep'] == '.' and not match['year']:
            continue
        month, day = int(match['month']), int(match['day'])
        if 1 <= month <= 12 and 1 <= day <= 31:
            days.add((int(match['year'] or 0), month, day))
    return days


def _days_overlap(left, right):
    return any(
        day_left[1:] == day_right[1:] and (not day_left[0] or not day_right[0] or day_left[0] == day_right[0])
        for day_left in left
        for day_right in right
    )


def _quantitative_rows(observations, records, used):
    units = []
    for item in observations:
        if not _observation_ids(item).intersection(used) or item.get('sourceRole') != 'direct_measurement':
            continue
        if (value := _decimal(item.get('value'))) is None or item.get('statusReferenceConflict') is True:
            continue
        expected = _reference_direction(item['value'], item.get('referenceRange'))
        if item.get('referenceAssessmentState') == 'within':
            expected = '正常'
        if not item.get('referenceRange'):
            status = _normal(item.get('status'))
            expected = expected or _DIRECTION.get(status, status)
        units.append(
            {
                'labels': _aliases(item)[0],
                'value': value,
                'expected': expected,
                'boundary': (),
                'date': item.get('observedAt'),
                'panels': (),
                'kind': 'direct_measurement',
                'source': item,
            }
        )
    for record in records:
        observation = record.get('sourceObservation') or {}
        members = observation.get('observations') or [observation]
        for item in members:
            if (
                item.get('measurementType')
                not in {
                    'functional_score',
                    'source_graded_index',
                    'source_measurement',
                    'relative_abundance',
                }
                or (value := _decimal(item.get('value'))) is None
            ):
                continue
            labels = {_normal(item.get(key)) for key in ('label', 'memberLabel', 'reportedObject', 'targetName')}
            labels.update(label[3:] for label in tuple(labels) if re.fullmatch(r'维生素b\d+', label))
            functional = item.get('measurementMeaning') == 'predicted_functional_potential'
            boundary = (
                tuple(dict.fromkeys(band['label'] for band in item.get('matchingBands', ()) if band.get('label')))
                if item.get('boundaryAmbiguous')
                else ()
            )
            units.append(
                {
                    'labels': tuple(label for label in labels if label),
                    'value': value,
                    'expected': item.get('resolvedGrade')
                    or (None if functional else _DIRECTION.get(_normal(item.get('status')))),
                    'date': record.get('date'),
                    'panels': item.get('sourcePanelLabels') or (),
                    'boundary': boundary,
                    'kind': item.get('measurementMeaning'),
                    'source': item,
                }
            )
    return units


def _literal_label_pattern(label, *, flexible_space=True):
    name = re.escape(label)
    if flexible_space:
        name = name.replace(r'\ ', r'\s*')
    left = r'(?<![a-z0-9])' if re.match(r'[a-z0-9]', label) else ''
    right = r'(?![a-z0-9])' if re.search(r'[a-z0-9]$', label) else ''
    return left + name + right


def _quantities(rows):
    by_label = {}
    for row in rows:
        for label in row['labels']:
            if len(label) >= 2 and row not in by_label.setdefault(label, []):
                by_label[label].append(row)
    if not by_label:
        return None, by_label
    labels = sorted(by_label, key=lambda label: (-len(label), label))
    names = '|'.join(_literal_label_pattern(label) for label in labels)
    return re.compile(names), by_label


def _label_rows(match, by_label):
    return by_label.get(match.group()) or next(
        (rows for label, rows in by_label.items() if re.fullmatch(_literal_label_pattern(label), match.group())), []
    )


def _grade_rows(options, asserted_value, clause):
    if asserted_value is not None:
        options = [item for item in options if item['value'] == asserted_value]
    if days := _observed_days(clause):
        dated = [item for item in options if _days_overlap(_observed_days(item['date']), days)]
        if dated:
            return dated
    return options


_GRADE_AUTHORITY = re.compile(r'(?:模板|对照)(?:参考)?(?:目标|窗口|目标区间|范围|关注线)|'
                              r'原化验|本室|实验室|化验单|原检验|原报告参考')


def _grade_issues(text, matches, by_label, comparison_claims=()):
    issues, accumulated = [], []
    for index, match in enumerate(matches):
        following = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        suffix = text[match.end() : following]
        if list_item := _MEASUREMENT_LIST.fullmatch(suffix):
            accumulated.append((match, _decimal(list_item['value'])))
            continue
        if grade := _GRADE.match(suffix):
            accumulated.append((match, _decimal(grade['value'])))
            for item, value in accumulated:
                if _ACTUAL_GRADE_NEGATION.search(text[max(0, item.start() - 22) : item.start()]):
                    continue
                options = _grade_rows(_label_rows(item, by_label), value, text)
                # A named template target applies only to its own observation.
                # The same result may be normal in the laboratory interval.
                prior = text[:item.start()]
                authorities = list(_GRADE_AUTHORITY.finditer(prior))
                if authorities and re.match(r'模板|对照', authorities[-1].group()) and not re.search(
                    r'[。；;]|但|而|另按', prior[authorities[-1].end():]
                ):
                    owned = set().union(*(_observation_ids(row['source']) for row in options))
                    grades = {claim['grade'] for claim in comparison_claims
                              if claim.get('authority') == 'template' and claim.get('grade')
                              and owned.intersection(claim.get('recordIds', ()))}
                    if grades:
                        if grade['grade'] not in grades:
                            issues.append(f'「{item.group()}」的模板目标判定为{"、".join(sorted(grades))}。')
                        continue
                if not options or not all(row['boundary'] or row['expected'] for row in options):
                    continue
                stated = grade['grade']
                allowed = {row['expected'] for row in options if not row['boundary']}
                boundary = {label for row in options for label in row['boundary']}
                if boundary and not allowed:
                    labels = '、'.join(sorted(boundary))
                    issues.append(f'「{item.group()}」处于原分档共有端点（{labels}），应保留边界及原状态。')
                elif stated not in allowed | boundary:
                    labels = '、'.join(sorted(allowed | boundary))
                    issues.append(f'「{item.group()}」的实际原参考判定为{labels}，不能写成{stated}。')
        accumulated.clear()
    return issues


def _functional_group_labels(rows):
    """Map the literal panel or member names to bound functional score rows."""
    names, panels = {}, {}
    for row in rows:
        if row['kind'] != 'predicted_functional_potential':
            continue
        for label in row['labels']:
            names.setdefault(label, []).append(row)
        definition = row['source'].get('panelDefinition') or {}
        for panel in (*row['panels'], definition.get('label')):
            label = _normal(panel).strip()
            if not label:
                continue
            base = _FUNCTION_PANEL_SUFFIX.sub('', re.sub(r'\([^)]*\)', '', label)).strip()
            for alias in {label, base} - {''}:
                panels.setdefault(alias, []).append(row)
    if not panels:
        return None
    labels = sorted(set(names) | set(panels), key=lambda value: (-len(value), value))
    pattern = re.compile('|'.join(_literal_label_pattern(label, flexible_space=False) for label in labels))
    return pattern, names, panels


def _functional_group_grade_issues(text, inventory):
    pattern, names, panels = inventory
    matches = list(pattern.finditer(text))
    listed, issues = [], []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        suffix = text[match.end() : end]
        if connector := _FUNCTION_LIST.fullmatch(suffix):
            listed.append((match, _decimal(connector['value'])))
            continue
        grade = _FUNCTION_GRADE.match(suffix)
        candidates = [*listed, (match, None)]
        if grade and any(item.group() in panels for item, _ in candidates):
            for item, value in candidates:
                label = item.group()
                prefix = text[max(item.start() - 22, 0) : item.start()]
                if _ACTUAL_GRADE_NEGATION.search(prefix) or (
                    label in panels and re.search(rf'{_FUNCTION_SUBSET}\s*$', prefix)
                ):
                    continue
                options = _grade_rows(panels.get(label) or names[label], value, text)
                differing = {
                    row['source'].get('memberLabel') or row['source'].get('reportedObject') or label
                    for row in options
                    if row['boundary'] or (row['expected'] and row['expected'] != grade['grade'])
                }
                if differing:
                    examples = '、'.join(sorted(differing)[:4])
                    issues.append(
                        f'「{label}」不能统一写为{grade["grade"]}（如{examples}）。'
                        '应按每项原分档或尚未定档的共有端点分别陈述。'
                    )
        listed.clear()
    return issues


def _temporal_issue(text, matches, by_label):
    for relation in _TEMPORAL_JOIN.finditer(text):
        if _ACTUAL_GRADE_NEGATION.search(text[max(0, relation.start() - 22) : relation.start()]):
            continue
        before = next((m for m in reversed(matches) if m.end() <= relation.start()), None)
        after = next((m for m in matches if m.start() >= relation.end()), None)
        if before is None or after is None:
            continue
        if (len(_STATEMENT.findall(text[before.end():relation.start()])) > 1
                or len(_STATEMENT.findall(text[relation.end():after.start()])) > 1):
            continue
        sides = [{day for row in _label_rows(m, by_label) for day in _observed_days(row['date'])}
                 for m in (before, after)]
        if all(len(side) == 1 for side in sides) and not _days_overlap(*sides):
            return f'「{relation.group()}」所连的观测来源日期不同，应分别陈述各时点。'
    return None


def _stated_quantity(text, match):
    quantity = re.match(_QUANTITY, text[match.end() :])
    return _decimal(quantity['value']), match.end() + quantity.end()


def _reference_supported(source, negative_allowed):
    if source.get('referenceRange') and source.get('referenceAssessmentState') == 'within':
        return True
    if not negative_allowed or source.get('statusReferenceConflict') is True:
        return False
    return any(
        _normal(source.get(key)).strip() in {'阴性', 'negative', 'neg', '(-)', '-'} for key in ('value', 'status')
    )


def _reference_issues(text, matches, by_label, clinical_groups, used):
    issues, listed = [], []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        suffix = text[match.end() : end]
        if item := _MEASUREMENT_LIST.fullmatch(suffix):
            listed.append((match, _decimal(item['value'])))
            continue
        if (assertion := _REFERENCE_CLAIM.match(suffix)) and not _NONASSERTION.search(text[: match.start()]):
            listed.append((match, _decimal(assertion['value'])))
            for measurement, value in listed:
                candidates = _grade_rows(_label_rows(measurement, by_label), value, text)
                sources = [row['source'] for row in candidates if row['kind'] == 'direct_measurement']
                if (
                    sources
                    and len(sources) == len(candidates)
                    and not any(_reference_supported(source, '阴性或' in assertion.group()) for source in sources)
                ):
                    issues.append(f'「{measurement.group()}」的原记录不足以判为处于参考范围内。')
        listed.clear()

    for group in clinical_groups:
        label = _normal(group.get('name'))
        base = re.sub(r'(?:检查|检测|评估)$', '', label)
        if not base or len(base) < 3:
            continue
        group_pattern = re.sub(r'[与及和]', '[与及和]', re.escape(base))
        for name in re.finditer(group_pattern + r'(?:检查|检测|评估)?', text):
            claim = _REFERENCE_GROUP_CLAIM.match(text[name.end() :])
            if not claim or _NONASSERTION.search(text[: name.start()]):
                continue
            members = [item for item in group['members'] if item.get('sourceRole') == 'direct_measurement']
            bound = [item for item in members if _observation_ids(item).intersection(used)]
            if not bound:
                continue
            days = _observed_days(text) or {day for item in bound for day in _observed_days(item.get('observedAt'))}
            if days:
                members = [
                    item
                    for item in members
                    if not _observed_days(item.get('observedAt'))
                    or _days_overlap(_observed_days(item.get('observedAt')), days)
                ]
            unsupported = sorted(
                {
                    str(item.get('name') or item.get('sourceLabel'))
                    for item in members
                    if (item.get('name') or item.get('sourceLabel'))
                    and not _reference_supported(item, '阴性' in claim.group())
                }
            )
            if unsupported:
                examples = '、'.join(unsupported[:3])
                issues.append(f'「{label}」含缺少支持整组判定的项目（如{examples}），应逐项保留原值与原参考。')
    return issues


def _finding_limit_issues(text, observations, used):
    quotes = set()
    for item in observations:
        if item.get('sourceRole') not in {'exam_finding', 'physical_finding'} or not _observation_ids(item) & used:
            continue
        if item.get('sourceFindingQuote'):
            quotes.add(_normal(item['sourceFindingQuote']))
        quotes.update(
            _normal(span['quote'])
            for span in item.get('sourceSpans') or ()
            if span.get('recordId') in used and span.get('role') == item.get('sourceRole') and span.get('quote')
        )
    if not quotes:
        return []
    findings, predicate_objects = {}, {}
    for quote in quotes:
        for finding in _LIMITED_FINDING.finditer(quote):
            term = finding['object']
            qualifiers = (bool(finding['scale']), bool(finding['limit']))
            findings.setdefault(term, set()).add(qualifiers)
            if (predicate := _FINDING_PREDICATE.match(term)) and len(term) >= predicate.end() + 2:
                target = term[predicate.end() :]
                predicate_objects.setdefault(predicate.group(), {}).setdefault(target, set()).add(qualifiers)
    issues = []
    for claim in _LIMITED_FINDING.finditer(text):
        if _NONASSERTION.search(text[: claim.start()]):
            continue
        term = claim['object']
        witnesses = findings.get(term, set())
        anatomy = _EVENT_CLAUSE.split(text[: claim.start()])[-1].strip()
        if term in predicate_objects and anatomy:
            # Invert a source phrase such as an enlarged structure only
            # when the exact structure also appears as this assertion's subject.
            matched = [bounds for name, bounds in predicate_objects[term].items() if anatomy.endswith(name)]
            if matched:
                witnesses = set().union(*matched)
        removed_scale = not claim['scale'] and witnesses and all(scale for scale, _ in witnesses)
        removed_limit = not claim['limit'] and witnesses and all(limit for _, limit in witnesses)
        if removed_scale or removed_limit:
            issues.append(f'「{claim.group()}」删掉了原所见的影像表现或程度限定，应保留原观察范围。')
    return issues


def _formula_operand_keys(text):
    return {key for key, pattern in _FORMULA_OPERANDS.items() if re.search(pattern, _normal(text))}


def _formula_issues(text, records):
    mentions = []
    for record in records:
        context = record.get('derivedIndex') or {}
        if context.get('valueKind') != 'calculated':
            continue
        formula = _normal(context.get('formula'))
        label = re.sub(r'\(计算值\)$', '', _normal(record.get('name') or record.get('label'))).strip()
        labels = {label}
        if '=' in formula:
            labels.add(formula.partition('=')[0].strip())
        if '残余胆固醇' in labels:
            labels.add('残粒胆固醇')
        if '比值' in label and '/' in label:
            labels.add(label.split('比值', 1)[0].strip())
            ratio = re.match(r'([a-z][a-z0-9-]*)\s*÷\s*([a-z][a-z0-9-]*)', formula)
            if ratio:
                abbreviation = '/'.join(ratio.groups())
                labels.update((abbreviation, abbreviation + ' 比值'))
        allowed = _formula_operand_keys(' '.join([formula, *(context.get('inputs') or ())]))
        if allowed:
            patterns = [
                _literal_label_pattern(alias).replace('/', r'\s*/\s*')
                for alias in sorted(labels, key=len, reverse=True)
                if alias
            ]
            if not patterns:
                continue
            mentions.extend(
                (match.start(), match.end(), label, allowed) for match in re.finditer('|'.join(patterns), text)
            )
    if not mentions:
        return []
    mentions.sort(key=lambda item: (item[0], -item[1]))
    issues = []
    for claim in _DERIVATION.finditer(text):
        prefix = text[: claim.start()]
        if _NONASSERTION.search(prefix) or re.search(r'(?:分别|各自|非)\s*$', prefix):
            continue
        specified = _formula_operand_keys(claim['inputs'])
        if not specified:
            continue
        nearby = [mention for mention in mentions if 0 <= claim.start() - mention[1] <= 90]
        targets = nearby if claim['shared'] else nearby[-1:]
        # A fronted input list can govern a coordinated list of outcomes after the calculation verb.
        if not targets or not _FORMULA_VALUE.fullmatch(text[targets[-1][1] : claim.start()]):
            results = []
            for mention in (item for item in mentions if item[0] >= claim.end()):
                start, end, _, _ = mention
                if results:
                    if not _MEASUREMENT_LIST.fullmatch(text[results[-1][1] : start]):
                        break
                elif not _FORMULA_RESULT_START.fullmatch(text[claim.end() : start]):
                    break
                value = _FORMULA_VALUE.match(text, end)
                if not value or not value['value'] or _SEPARATE_DERIVATION.match(text, value.end()):
                    break
                results.append(mention)
            targets = results or targets
        for _, _, label, allowed in targets:
            if specified - allowed:
                issues.append(f'「{label}」的计算使用了该项原公式不含的输入，应分别按各自来源公式陈述。')
    return issues


def _measured_calculation_issues(text, matches, by_label):
    issues = [
        'BMI是按实测身高和体重计算的指数，应分别说明实测输入和计算结果。'
        for claim in _MEASURED_BMI.finditer(text)
        if not _NONASSERTION.search(text[: claim.start()])
    ]
    for match in matches:
        claim = _CALCULATED_ATTRIBUTION.match(text[match.end() :])
        if not claim or _NONASSERTION.search(text[: match.start()]):
            continue
        value = _decimal(claim['value'])
        rows = _grade_rows(_label_rows(match, by_label), value, text)
        if value is not None and rows and all(row['kind'] == 'direct_measurement' for row in rows):
            issues.append(f'「{match.group()}」引用的是实测原值，应将实测与按标准分级分开陈述。')
    return issues


def _panel_issues(text, matches, by_label, panel_labels):
    if not panel_labels:
        return []
    labels = {_normal(label): _normal(label) for label in panel_labels}
    for label in tuple(labels):
        if label.endswith('构成'):
            labels[label + '图谱'] = label
    pattern = '|'.join(re.escape(label) for label in sorted(labels, key=lambda label: (-len(label), label)))
    introduced_scope = re.compile(
        rf'(?:在|于|属于|来自|归于|来源于)\s*(?P<panel>{pattern})(?:面板)?(?:中|内)?'
        rf'|(?P<list>{pattern})(?:面板)?(?:汇总|小结|结果|比较)(?:[^。！？;；\n:，,]{{0,12}})[:：]'
    )
    stop = re.compile(
        r'[。！？\n]+|(?:但|另见|此外|另列|另一方面|同时另|至于|相比)'
        r'|(?:构成图谱|其他面板|另一面板|肠道菌群构成)(?:中|内)|\.(?=\s|$)'
    )
    issues = []
    scopes = list(introduced_scope.finditer(text))
    for index, scope in enumerate(scopes):
        prefix = text[: scope.start()]
        if _NONASSERTION.search(prefix):
            continue
        list_scope = scope['list'] is not None
        label = labels[scope['list'] or scope['panel']]
        end = scopes[index + 1].start() if index + 1 < len(scopes) else len(text)
        if (boundary := stop.search(text, scope.end(), end)) is not None:
            end = boundary.start()
        if not list_scope:
            end = min(end, scope.end() + 18)
        for match in matches:
            if not scope.end() <= match.start() < end:
                continue
            gap = text[scope.end() : match.start()]
            direct_item = re.fullmatch(r'[，,:：\s]*(?:显示|记录|该项|最明显的是|其中|其)?[，,:：\s]*', gap)
            if not list_scope and not direct_item:
                continue
            value, _ = _stated_quantity(text, match)
            if value is None:
                continue
            options = [row for row in _label_rows(match, by_label) if row['value'] == value]
            if options and all(row['panels'] and label not in {_normal(p) for p in row['panels']} for row in options):
                issues.append(f'「{match.group()}」的该报告值不属于正文所指「{label}」面板。')
    return issues


def _stated_measurements(text, matches, by_label):
    resolved = []
    for match in matches:
        value, end = _stated_quantity(text, match)
        rows = _grade_rows(_label_rows(match, by_label), value, text)
        if value is not None and rows:
            resolved.append((match, end, rows))
    return resolved


def _sequence_authority(records):
    return [
        row
        for record in records
        for observation in [record.get('sourceObservation') or {}]
        for row in observation.get('observations') or [observation]
        if row.get('measurementMeaning') == 'target_sequence_signal'
        and (row.get('detection') or {}).get('status') in {'detected', 'not_detected'}
    ]


def _assay_meaning_issues(text, values, previous, sequences):
    issues = []
    owners = {row['kind'] for _, _, rows in values for row in rows if row['kind']}
    for match, end, rows in values:
        if not all(row['kind'] == 'sample_relative_abundance' for row in rows):
            continue
        boundary = _EVENT_CLAUSE.search(text, end)
        tail = text[end : boundary.start() if boundary else len(text)]
        if (
            any(row['value'] != 0 for row in rows)
            or not (inference := _ZERO_INFERENCE.search(tail))
            or _NONASSERTION.search(tail[: inference.start()])
        ):
            continue
        label = match.group()
        if any(
            _normal(sequence.get(key)) == label and (sequence.get('detection') or {}).get('status') == 'not_detected'
            for sequence in sequences
            for key in ('targetName', 'reportedObject')
        ) and re.search(r'(?:序列|靶标)', tail):
            continue
        if re.search(r'检测限', inference.group()) and any(
            (limit.get('status') == 'reported')
            for row in rows
            for limit in (row['source'].get('analyticalLimits') or {}).values()
        ):
            continue
        issues.append(f'「{label}」的0为样本报告份额，不能据此推断未检出、检测限或体内缺失。')

    if (
        (subject := _PREVIOUS_RESULTS.match(text))
        and (sequence := _SEQUENCE_TERMS.search(text, subject.end()))
        and not _NONASSERTION.search(text[: sequence.start()])
        and _DEFINITION_CLAIM.search(text, sequence.end())
        and _ASSAY_RELATIVE_TERMS.search(text, sequence.end())
        and (owners or previous) == {'sample_relative_abundance'}
    ):
        issues.append('所指项目是样本相对丰度，不能称为靶标序列检出或低丰度序列信号。')
    if (
        owners & {'source_index', 'predicted_functional_potential'}
        and (host := _HOST_CONCLUSION.search(text))
        and not _NONASSERTION.search(text[: host.start()])
        and not re.search(r'(?:不能|不能够|无法|尚不足以|不能用来)', host.group())
    ):
        issues.append('菌群功能预测或源指数与临床测项须分别解释，不能合并判断宿主炎症或免疫状态。')
    return issues, owners


def paragraph_literal_issues(part, observations, records, *, panel_labels, clinical_groups=(), comparison_claims=(),
                             previous=None, microbiome_dates=None):
    """Check strong factual claims against the bound observation rather than another measured object."""
    used = set(part.get('recordIds') or ())
    rows = _quantitative_rows(observations, records, used)
    pattern, by_label = _quantities(rows)
    functional_groups = _functional_group_labels(rows)
    text = _normal((part.get('title') + '：' if part.get('title') else '') + part['text'])
    matches = list(pattern.finditer(text)) if pattern else []
    issues = _panel_issues(text, matches, by_label, panel_labels)
    if _TEMPORAL_JOIN.search(text):
        dates = microbiome_dates or {}
        previous = previous or {}
        prior = _normal(previous.get('text', '')).rstrip('。；; ')
        prior = _STATEMENT.split(prior)[-1]
        temporal_text = prior + '。' + text if prior else text
        date_rows = list(rows)
        if prior:
            extra = previous.get('records') or ()
            date_rows += _quantitative_rows(observations, extra, {row['id'] for row in extra})
        for row in date_rows:
            if not row['date'] and row['kind'] == 'predicted_functional_potential':
                row['date'] = dates.get('sampleDate')
        temporal_pattern, dated_labels = _quantities(date_rows)
        temporal_matches = list(temporal_pattern.finditer(temporal_text)) if temporal_pattern else []
        if issue := _temporal_issue(temporal_text, temporal_matches, dated_labels):
            issues.append(issue)
    sequences = _sequence_authority(records)
    previous = set()
    for _, statement in _spans(text, _STATEMENT):
        matches = list(pattern.finditer(statement)) if pattern else []
        if not _QUOTED_ASSESSMENT.search(statement):
            issues.extend(_grade_issues(statement, matches, by_label, comparison_claims))
            if functional_groups:
                issues.extend(_functional_group_grade_issues(statement, functional_groups))
        issues.extend(_reference_issues(statement, matches, by_label, clinical_groups, used))
        issues.extend(_finding_limit_issues(statement, observations, used))
        issues.extend(_formula_issues(statement, records))
        issues.extend(_measured_calculation_issues(statement, matches, by_label))
        assay_issues, owners = _assay_meaning_issues(
            statement, _stated_measurements(statement, matches, by_label), previous, sequences
        )
        issues.extend(assay_issues)
        previous = owners
    return list(dict.fromkeys(issues))


def _analysis_groups(observations):
    groups = {}
    for observation in observations:
        if observation.get('sourceRole') != 'direct_measurement':
            continue
        names, abbreviation = _aliases(observation)
        if names:
            groups.setdefault((names, abbreviation), {})[observation['id']] = observation
    return groups


def _conditioned_analytes(names):
    analyte = '|'.join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    qualified = re.compile(
        rf'(?:{_CONDITION.pattern})\s*(?:(?:血浆|血清|plasma|serum)\s*)?(?:{analyte})(?![a-z])',
        re.I,
    )
    named = re.compile(rf'(?<![a-z])(?:{analyte})(?![a-z])', re.I)
    return qualified, named


def _matched_values(observations, numbers):
    return [source['id'] for source in observations if _decimal(source.get('value')) in numbers]


def _clause_occurrences(bound, clause):
    dates = _observed_days(clause)
    if len(dates) == 1:
        dated = [source for source in bound if _days_overlap(_observed_days(source.get('observedAt')), dates)]
        if dated or all(_observed_days(source.get('observedAt')) for source in bound):
            return dated
    return bound


def _threshold_candidates(normalized, qualified, named, abbreviation, bound, passages):
    """A cited criterion keeps its own analyte and specimen conditions."""
    if not bound or not passages or not _CRITERION_USE.search(normalized):
        return []
    spans = [
        (start, clause)
        for start, clause in _spans(normalized, _CRITERION_BOUNDARY)
        if _CRITERION_USE.search(clause) and _CRITERION_LINK.search(clause) and named.search(clause)
    ]
    if not spans:
        return []
    candidates = {}
    for pid, snippets in passages.items():
        # Each snippet is an independent literal scope. Do not join a title
        # or adjacent sentence's qualifier to a different threshold here.
        for origin in snippets:
            for item in _qualified_criteria(origin, qualified, abbreviation, bound, spans):
                signature = (item['normalizedStart'], item['condition'], item['criterion'])
                combined = candidates.setdefault(signature, {**item, 'passageIds': []})
                if pid not in combined['passageIds']:
                    combined['passageIds'].append(pid)
    return list(candidates.values())


def _qualified_criteria(origin, qualified, abbreviation, bound, spans):
    text = _normal(origin)
    mentions = list(qualified.finditer(text))
    if abbreviation:
        mentions.extend(re.finditer(rf'(?<![a-z]){re.escape(abbreviation)}(?![a-z])', text, re.I))
    for mention in mentions:
        cut = _CRITERION.match(text[mention.end() :])
        if not cut:
            continue
        condition = next((name for name in _CONDITION_LABELS if mention.groupdict().get(name)), 'fasting')
        if all(
            any(item.get('condition') == condition for item in source.get('acquisitionConditions', ()))
            for source in bound
        ):
            continue  # The occurrences already meet the basic named condition.
        cutoff = _decimal(cut['value'])
        criterion = text[mention.start() : mention.end() + cut.end()]
        for start, clause in spans:
            numbers = {_decimal(number.group()) for number in _NUMBER.finditer(clause)} - {None}
            occurrences = _clause_occurrences(bound, clause)
            matched = _matched_values(occurrences, numbers - {cutoff}) or _matched_values(occurrences, numbers)
            if cutoff not in numbers and not matched:
                continue  # Require the actual criterion or this measurement, not mere topic proximity.
            yield {
                'kind': 'external_threshold',
                'term': clause.strip(),
                'normalizedStart': start,
                'condition': condition,
                'criterion': criterion,
                'observationIds': [source['id'] for source in occurrences],
                'matchingValueObservationIds': matched,
            }


def _recalled_event_mentions(text):
    """An assay referring to its sample does not identify the reported collection."""
    mentions = {}
    for offset, clause in _spans(text, _EVENT_CLAUSE):
        local = list(_RECALLED_COLLECTION.finditer(clause))
        for match in _DEFINITE_COLLECTION.finditer(clause):
            vicinity = clause[max(0, match.start() - 16) : match.end() + 16]
            if (
                _SAMPLE_EVENT.search(match.group())
                or _AS_SAMPLED.search(vicinity)
                or (local and _SAMPLE_EVENT.search(vicinity))
            ):
                if not any(other.start() <= match.start() < other.end() for other in local):
                    local.append(match)
        for match in local:
            mentions[offset + match.start()] = match.group()
    return sorted(mentions.items())


def paragraph_claim_candidates(part, observations, source_contract, *, cell_record_ids, passages=None):
    """Bind review candidates to exact claims in immutable P text."""
    text = (part.get('title') + '：' if part.get('title') else '') + part['text']
    normalized = _normal(text)
    used = set(part.get('recordIds') or ())
    candidates = []
    for (names, abbreviation), by_id in _analysis_groups(observations).items():
        pattern, named = _conditioned_analytes(names)
        mentions = [
            (match.start(), match.end(), next(name for name in _CONDITION_LABELS if match.groupdict().get(name)))
            for match in pattern.finditer(normalized)
        ]
        if abbreviation:
            mentions.extend(
                (match.start(), match.end(), 'fasting')
                for match in re.finditer(
                    rf'(?<![a-z]){re.escape(abbreviation)}(?![a-z])',
                    normalized,
                    re.I,
                )
            )
        bound = [source for source in by_id.values() if _observation_ids(source).intersection(used)]
        for start, end, condition in sorted(set(mentions)):
            prefix = re.split(r'[。！？；;\n]', normalized[:start])[-1]
            suffix = re.split(r'[，,。！？；;\n]', normalized[end:], maxsplit=1)[0]
            number = _NUMBER.search(suffix[:48])
            value = _decimal(number.group()) if number else None
            actual_clause = prefix + normalized[start:end] + suffix
            available = bound if _QUOTED_ASSESSMENT.search(prefix) else _clause_occurrences(bound, actual_clause)
            value_sources = [
                source['id'] for source in available if value is not None and _decimal(source.get('value')) == value
            ]
            candidates.append(
                {
                    'kind': 'acquisition_condition',
                    'term': normalized[start:end],
                    'normalizedStart': start,
                    'condition': condition,
                    'observationIds': [source['id'] for source in available],
                    'matchingValueObservationIds': value_sources,
                }
            )
        candidates.extend(_threshold_candidates(normalized, pattern, named, abbreviation, bound, passages))
    action_spans = list(_action_spans(text, part.get('role')))
    if part.get('role') == 'action' and not action_spans:
        # An editor may propose an action without a listed verb. The
        # independent reviewer must still decide whether each clause really
        # is a personal action and check its sources and any citations.
        action_spans = [(offset, sentence, sentence) for offset, sentence in _spans(text, _STATEMENT)
                        if sentence.strip()]
    action_spans.extend((match.start(), match.group(), match.group()) for match in _ADVICE_ORDER.finditer(text))
    for start, clause, statement in action_spans:
        item = {'kind': 'patient_action', 'term': clause, 'start': start}
        if _ATTRIBUTION.search(statement) and not _NEW_ACTION.search(clause):
            item['sourceAttribution'] = True
        if statement != clause and (
            _FOLLOWUP_GATE.search(statement) or _CONDITION_SCOPE.search(statement) or _ATTRIBUTION.search(statement)
        ):
            item['statement'] = statement
        candidates.append(item)
    combined_events = {}
    bound_recall = _BOUND_RECALL.search(text)
    for event in source_contract.get('unknownEvents', ()):
        if event.get('recordId') not in used or not _COLLECTION_SCOPE.search(str(event.get('scopeQuote') or '')):
            continue
        related = set(event.get('relatedRecordIds', ())).intersection(used)
        if bound_recall and not related:
            # A definite reference can point to the recorded sample in a
            # preceding paragraph. Merely presenting two independent records
            # in one cell does not transfer the clinical sample identity.
            related = set(event.get('relatedRecordIds', ())).intersection(cell_record_ids)
        if related:
            scope = (str(event.get('scopeQuote')), tuple(sorted(related)))
            combined_events.setdefault(scope, []).append(event)
    if combined_events:
        mentions = _recalled_event_mentions(text)
        if bound_recall and not mentions:
            mentions = [(bound_recall.start(), bound_recall.group())]
        affirmed = bound_recall and not _INDEPENDENT_EVENT.search(text[: bound_recall.start()])
        different = _ASSERTED_DIFFERENT_EVENT.search(text)
        if different and _NONASSERTION.search(text[: different.start()]):
            different = None
        for (_, related), events in combined_events.items():
            for start, term in mentions:
                candidates.append(
                    {
                        'kind': 'event_relation',
                        'term': term,
                        'start': start,
                        'recordIds': [event['recordId'] for event in events],
                        'relatedRecordIds': list(related),
                        'eventIdentity': 'not_established_by_sources',
                        'timingExplicit': bool(_INDEPENDENT_EVENT.search(text)),
                        'affirmedSameSpecimen': bool(affirmed),
                        'affirmedDifferentEvent': bool(
                            different and all(not event.get('eventDate') for event in events)
                        ),
                    }
                )
    return [{'id': f'K{index:02d}', **candidate} for index, candidate in enumerate(candidates, 1)]


def claim_authority_issue(candidate, decision, paragraph, observations, *, passage_roles):
    """Check the decision against server-held, occurrence-level rights."""
    use = decision['use']
    kind = candidate['kind']
    if use in {'observed_measurement', 'applied_threshold'}:
        selected = decision.get('observationIds')
        allowed = set(candidate['observationIds'])
        if (
            not isinstance(selected, list)
            or not selected
            or not all(isinstance(oid, str) for oid in selected)
            or not set(selected).issubset(allowed)
        ):
            return '测量限定或标准应用须定位本P的同一实测记录，不能借背景或别项检查。'
        matching = set(candidate['matchingValueObservationIds'])
        if matching and not set(selected).issubset(matching):
            return '限定词旁已有来源实测值，须核该值实际来源；同名或同值不授权合并日期、方法或事件。'
        condition = candidate['condition']
        if any(
            not any(
                item.get('condition') == condition
                for item in observations[oid].get(
                    'acquisitionConditions',
                    (),
                )
            )
            for oid in selected
        ):
            criterion = f'。引文条件为「{candidate["criterion"]}」' if kind == 'external_threshold' else ''
            return (
                f'本P所引该项来源没有{_CONDITION_LABELS[condition]}采集条件{criterion}。'
                '保留原测值及其自身参考结论，另列未来检查建议，不应用该指南界值。'
            )
    if kind == 'patient_action' and use == 'mixed_action':
        return '本句含原医嘱及本报告新增行动，请分P分别核对象、条件、role和引文用途。'
    if kind == 'patient_action' and use != 'reported_advice' and _ADVICE_ORDER.search(candidate['term']):
        return '归为原医嘱的分层须有精确A依据；报告自己的分层应明确是本报告组织。'
    if kind == 'patient_action' and use == 'rule_conclusion' and paragraph.get('role') != 'action':
        return '本P实际提出患者管理行动，应与事实解释分段并标action；规则结论支持的安排同样单列行动段。'
    if kind == 'patient_action' and use == 'patient_management':
        if paragraph.get('role') != 'action':
            return '本P实际提出患者管理行动，应与事实解释分段并标action；不能借interpretation标签让背景引文给行动背书。'
        if any('action' not in passage_roles.get(pid, ()) for pid in decision.get('passageIds', ())):
            return '本命题采用的每条E均须获action用途许可；无适用E的新建议可仅绑真实F并接受医学审查。'
        if candidate.get('sourceAttribution'):
            return '实际新增安排不能写作按原医嘱或原医生建议，须另写真实的报告建议。'
    if kind == 'event_relation' and (use == 'established_link' or candidate.get('affirmedSameSpecimen')):
        return (
            '来源未建立回顾留样与所引临床检查的共同事件/标本身份；'
            '不能用确定指代先作同次绑定，再以后句关系未知撤销。独立保留两份来源事实。'
        )
    if kind == 'event_relation' and candidate.get('affirmedDifferentEvent'):
        return '回顾日期及标本与检查的对应未明确，不能据此断言为两个不同的时点或标本。'
    if kind == 'event_relation' and not candidate['timingExplicit']:
        return '同段同时采用日期确定的检查和日期不明的留样回顾，须明确回顾时间关系未建立，不能只否定因果。'
    return None
