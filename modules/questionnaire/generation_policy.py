"""Questionnaire-owned generation policy, frozen from the efca880 baseline.

The ordered catalogue and validation leaves preserve the existing questionnaire
behavior. Record's conversational flow, mutable templates and lifecycle are not
runtime dependencies. Future changes require another explicit policy revision.
"""

import re
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

GENERATION_POLICY_V1 = {
    'contractVersion': 'questionnaire-generation-policy.v1',
    'implementationRevision': 'questionnaire-generation-policy-v1',
}


class QuestionnairePolicyVersionError(ValueError):
    """An accepted question policy is missing, corrupt or unavailable."""


def generation_policy_selection():
    return deepcopy(GENERATION_POLICY_V1)


def require_generation_policy(value):
    if not isinstance(value, dict) or value != GENERATION_POLICY_V1:
        raise QuestionnairePolicyVersionError('Questionnaire generation policy is unavailable')
    return deepcopy(value)


class QuestionnairePresentationError(ValueError):
    def __init__(self, code: str, hint: str):
        self.code, self.hint = code, hint
        super().__init__(hint)


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, str_strip_whitespace=True)


TOPICS = {
    'diet_structure': {
        'question': '您平时的正餐通常吃到哪些食物？可多选。',
        'questionType': 'checkbox',
        'options': ['米饭', '面条', '杂粮', '蔬菜', '水果', '猪肉', '鱼肉', '鸡蛋', '豆制品'],
    },
    'staple_portion': {
        'question': '您平时一餐的米饭、面食等主食大约吃多少？',
        'questionType': 'radio',
        'options': ['约1拳', '约2拳', '超过2拳', '不吃主食', '不清楚'],
    },
    'sweet_intake': {
        'question': '您平时吃甜食或喝含糖饮料的频率更接近哪一种？',
        'questionType': 'radio',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不摄入', '不清楚'],
    },
    'salt_processed_food': {
        'question': '腌制品、咸味零食或加工肉类，您平时吃得频繁吗？',
        'questionType': 'radio',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不吃', '不清楚'],
    },
    'meal_pattern': {
        'question': '您平时的进餐时间通常规律吗？',
        'questionType': 'radio',
        'options': ['时间固定', '时间不固定', '不清楚'],
    },
    'late_meals': {
        'question': '您平时临睡前吃夜宵的频率更接近哪一种？',
        'questionType': 'radio',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不吃夜宵', '不清楚'],
    },
    'hydration': {
        'question': '您平时主要喝哪些饮品？可多选。',
        'questionType': 'checkbox',
        'options': ['白水', '茶', '咖啡', '含糖饮料'],
    },
    'water_intake': {
        'question': '您平时一天喝水的量大约是多少？',
        'questionType': 'radio',
        'options': ['不足500毫升', '500至1000毫升', '1000至1500毫升', '1500毫升及以上', '不清楚'],
    },
    'sleep': {
        'question': '您平时通常在什么时间准备睡觉？',
        'questionType': 'radio',
        'options': ['22点前', '22点至午夜前', '午夜及以后', '时间不固定'],
    },
    'sleep_duration': {
        'question': '您最近通常每天睡多长时间？',
        'questionType': 'radio',
        'options': ['不足6小时', '6至7小时', '7至9小时', '9小时及以上', '不清楚'],
    },
    'sleep_consistency': {
        'question': '工作日和休息日相比，您的睡起时间变化大吗？',
        'questionType': 'radio',
        'options': ['相差不足1小时', '相差1至2小时', '相差2小时及以上', '时间不固定', '不清楚'],
    },
    'bedtime_habits': {
        'question': '临睡前一小时，您通常会做哪些事？可多选。',
        'questionType': 'checkbox',
        'options': ['看手机', '用电脑', '阅读', '听音乐', '工作', '学习', '洗漱后休息'],
    },
    'activity': {
        'question': '您平时主要通过哪些方式活动身体？可多选。',
        'questionType': 'checkbox',
        'options': ['步行', '跑步', '骑行', '游泳', '力量训练', '打球', '家务劳动', '没有主动活动'],
    },
    'exercise_frequency': {
        'question': '您平时一周大约有几天安排运动？',
        'questionType': 'radio',
        'options': ['没有运动', '每周1～2天', '每周3～4天', '每周5～7天', '不清楚'],
    },
    'exercise_duration': {
        'question': '有安排运动时，您每次通常持续多久？',
        'questionType': 'radio',
        'options': ['不足15分钟', '15至30分钟', '30至60分钟', '60分钟及以上', '没有运动', '不清楚'],
    },
    'sedentary': {
        'question': '您平时连续坐着、不起身的时间通常有多久？',
        'questionType': 'radio',
        'options': ['不足30分钟', '30至60分钟', '1至2小时', '2小时及以上', '不清楚'],
    },
    'activity_breaks': {
        'question': '工作或学习时，您通常多久会起身走动一次？',
        'questionType': 'radio',
        'options': ['不足30分钟', '30至60分钟', '1至2小时', '2小时及以上', '没有固定安排'],
    },
    'smoking': {
        'question': '您目前的吸烟情况更接近哪一种？',
        'questionType': 'radio',
        'options': ['目前吸烟', '已戒烟', '从不吸烟'],
    },
    'passive_smoking': {
        'question': '您平时在家中、工作或社交场合接触二手烟多吗？',
        'questionType': 'radio',
        'options': ['每天接触', '每周接触', '不足每周1天', '不接触', '不清楚'],
    },
    'alcohol': {
        'question': '您平时的饮酒频率更接近哪一种？',
        'questionType': 'radio',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '不饮酒', '不清楚'],
    },
    'alcohol_amount': {
        'question': '喝酒时，您一次通常喝几杯？也可以描述酒的种类和实际容量。',
        'questionType': 'radio',
        'options': ['不足1杯', '1杯', '2杯', '3杯及以上', '不饮酒', '不清楚'],
    },
    'stress_coping': {
        'question': '忙碌或压力较大时，您通常用哪些方式放松？可多选。',
        'questionType': 'checkbox',
        'options': ['散步', '运动', '与亲友交流', '阅读', '听音乐', '休息', '独处'],
    },
    'rest_schedule': {
        'question': '日常生活中，您通常怎样安排属于自己的休息时间？',
        'questionType': 'radio',
        'options': ['每天固定休息', '休息日休息', '有空时休息', '没有专门休息'],
    },
    'work_breaks': {
        'question': '工作或学习忙碌时，您的休息节奏更接近哪一种？',
        'questionType': 'radio',
        'options': ['定时休息', '任务间休息', '忙完才休息', '没有固定节奏'],
    },
}


BRIDGES = {
    'diet_structure': '接着聊聊平时的饮食。',
    'staple_portion': '再聊聊平时一餐的主食份量。',
    'sweet_intake': '接着聊聊平时吃甜食和喝甜饮的习惯。',
    'salt_processed_food': '接着聊聊平时吃咸味加工食品的习惯。',
    'meal_pattern': '再聊聊您平时的吃饭安排。',
    'late_meals': '接着聊聊晚餐之后的进食习惯。',
    'hydration': '接着聊聊您平时常喝的饮品。',
    'water_intake': '再看看平时一天的饮水量。',
    'sleep': '接着聊聊您晚上的作息。',
    'sleep_duration': '再聊聊一晚实际睡觉的时间。',
    'sleep_consistency': '接着看看工作日和休息日的作息安排。',
    'bedtime_habits': '再聊聊临睡前这段时间。',
    'activity': '接着聊聊您平时的活动。',
    'exercise_frequency': '再看看您一周的运动安排。',
    'exercise_duration': '接着聊聊每次运动的时间。',
    'sedentary': '再聊聊您平时坐着的时间。',
    'activity_breaks': '接着看看坐久之后起身活动的习惯。',
    'smoking': '接着聊聊吸烟方面的情况。',
    'passive_smoking': '再聊聊您平时接触二手烟的情况。',
    'alcohol': '接着聊聊您平时的饮酒习惯。',
    'alcohol_amount': '再看看平时一次的饮酒量。',
    'stress_coping': '接着聊聊您平时怎样放松。',
    'rest_schedule': '再聊聊您日常的休息安排。',
    'work_breaks': '接着看看忙碌时的休息节奏。',
}


_CLAUSE_END = r'[^，,。！？!?；;\n]'


_SYSTEM_SUBJECT = r'(?:^|[，,。！？!?；;\n])(?:由于|因为|目前|当前|本轮|本次)?(?:系统|模型|AI)'


_NOTICE = re.compile(
    rf'(?:尚未|还未|暂未|未能|未完成|未成功|无法|不能|未获|没有得到){_CLAUSE_END}{{0,18}}'
    r'(?:(?:可靠|有效)(?:的|地)?(?:分析|回应|答复|回答)|完成分析|完成解析|完成处理)'
    rf'|(?:未能|无法|暂未)(?:完成|进行|完整)?(?:分析|解析|处理){_CLAUSE_END}{{0,6}}(?:您的回答|提交内容|用户输入)'
    rf'|{_SYSTEM_SUBJECT}(?:调用|生成|分析|解析|校验|响应|处理|修复|运行|服务)'
    r'(?:暂时|目前|出现)?(?:失败|超时|尚未完成|未完成)'
    rf'|{_SYSTEM_SUBJECT}(?:暂时|目前|仍|还)?(?:无法|未能)(?:响应|返回|处理|分析|生成)'
    rf'|{_SYSTEM_SUBJECT}(?:出现|发生)(?:错误|故障|异常)'
    r'|(?:分析|解析|生成|校验)(?:过程(?:中)?|期间)(?:出现|发生)(?:错误|异常|故障)'
    r'|(?:本轮|本次|这次|内部|已提交内容|您的回答|回答|问题|资料|报告|附件|文档)(?:的)?'
    r'(?:分析|解析|生成|校验|修复|处理)(?:暂时|目前|出现|发生|仍|尚)?(?:失败|超时|未完成)'
    r'|(?:^|[，,。！？!?；;\n])(?:分析|解析|生成|校验)(?:暂时|目前|出现|发生|仍|尚)?(?:失败|超时|未完成)'
    rf'|(?:原文|原始回答|原始内容){_CLAUSE_END}{{0,12}}(?:保留|保存)'
    rf'|(?:保留|保存){_CLAUSE_END}{{0,15}}(?:原文|原始回答|原始内容|待回应|待回答|待处理)'
    rf'|(?:保留|保存){_CLAUSE_END}{{0,8}}(?:您提交|已提交|已经提交)(?:的)?(?:内容|回答)'
    rf'|(?:已提交|您提交|已经提交){_CLAUSE_END}{{0,10}}(?:保留|保存)'
    rf'|(?:保留|列为|标记|记录|转为){_CLAUSE_END}{{0,10}}待(?:回应|回答|处理)'
    rf'|(?:稍后|后续|等待){_CLAUSE_END}{{0,12}}(?:重新分析|补偿处理|重试)'
)


_DECORATION = re.compile(r'[\s*_`\u200b\ufeff]+')


def contains_internal_notice(text: str) -> bool:
    """Recognize operational notices, not ordinary medical uncertainty."""
    return bool(isinstance(text, str) and _NOTICE.search(_DECORATION.sub('', text)))


def validate_internal_notice(text: str) -> None:
    if contains_internal_notice(text):
        raise QuestionnairePresentationError(
            'internal_processing_notice_forbidden',
            '对话正文不得描述分析、修复或生成失败、可靠回应缺失、原文保留或待处理状态；直接保留有效解释或自然引入当前话题',
        )


NO_MORE_OPTION = '没有了'


_OPTION_INSTRUCTION = re.compile(
    r'\s*(?:[（(]\s*请(?:补充|说明|描述|填写)(?:说明|描述|情况|内容)?\s*[）)]'
    r'|[，,：:]\s*请(?:补充|说明|描述|填写)(?:说明|描述|情况|内容)?)\s*$'
)


_AMBIGUOUS_CHOICE = re.compile(r'其[他它]|\bothers?\b', re.IGNORECASE)


_ENTRY_ONLY_CHOICES = frozenset(
    (
        '有补充',
        '还有补充',
        '需要补充',
        '有需要补充的内容',
        '有日常习惯想补充',
        '补充内容',
        '补充说明',
        '需要更正',
        '点击补充',
        '点击填写',
        '自由填写',
        '自行填写',
        '自行输入',
        '自定义',
    )
)


_QUOTE_OPEN = r'[“「『‘\"\']'


_QUOTE_CLOSE = r'[”」』’\"\']'


_UI_TARGET = r'(?:其[他它][^，,。.!！?？;；“”「」『』‘’\"\']{0,12}|(?:某|该|这个|所选|相应)?(?:快捷)?(?:选项|按钮))'


_INPUT_ACTION = r'(?:输入|填写|填入|补充|说明|描述)'


_TEXT_ENTRY_ACTION = r'(?:输入|填写|填入|补充(?=说明|描述|内容|信息|情况|病情|病名|答案|具体|[，,。.!！?？;；]|$))'


_UI_ENTRY_PATTERNS = (
    re.compile(
        rf'(?:在|到|从|通过){_QUOTE_OPEN}?(?:某|该|这个|所选|相应)?(?:快捷)?(?:选项|按钮){_QUOTE_CLOSE}?(?:里|中|内|处|下)?(?:直接|继续|自行|再|进行)?{_INPUT_ACTION}'
    ),
    re.compile(
        rf'(?:在|到|从|通过){_QUOTE_OPEN}?{_UI_TARGET}{_QUOTE_CLOSE}?(?:选项|按钮)?(?:里|中|内|处|下)?(?:直接|继续|自行|再|进行)?{_TEXT_ENTRY_ACTION}'
    ),
    re.compile(
        rf'(?:在|到){_QUOTE_OPEN}[^，,。.!！?？;；“”「」『』‘’\"\']{{1,24}}{_QUOTE_CLOSE}(?:选项|按钮)(?:里|中|内|处)?(?:直接|继续|自行|再|进行)?{_INPUT_ACTION}'
    ),
    re.compile(
        rf'(?:点击|点选|勾选|选择|选中)(?:{_QUOTE_OPEN}[^，,。.!！?？;；“”「」『』‘’\"\']{{1,24}}{_QUOTE_CLOSE}|{_UI_TARGET})(?:选项|按钮)?(?:后|之后|以后)(?:请|可|可以|再|继续|直接|即可|就能)*{_TEXT_ENTRY_ACTION}'
    ),
    re.compile(
        r'(?:点击|点选|勾选|选择|选中)[^，,。.!！?？;；]{0,30}(?:弹出|打开|出现|展开)[^，,。.!！?？;；]{0,10}(?:输入框|文本框|填写框)'
    ),
)


_NEGATED_ENTRY = re.compile(r'(?:不能|不要|不可|不应|无法|不支持|并非|请勿|不会)(?:直接|继续|再|自行|自动|尝试)?$')


_CHOICE_NUMBER = r'(?:\d+(?:\.\d+)?|[零一二两三四五六七八九十半]+)'


_CHOICE_UNIT = (
    r'(?:毫升|公升|升|毫克|微克|千克|公斤|克|斤|公里|千米|厘米|米|分钟|小时|秒|天|日|周|月|年'
    r'|次|杯|瓶|支|包|份|拳|片|粒|%|[a-zA-Zµμ°]+)'
)


_CHOICE_FREQUENCY = rf'(?:(?:每|隔)(?:{_CHOICE_NUMBER})?(?:小时|天|日|周|月|年|晚))?'


_CHOICE_QUANTITY = (
    rf'{_CHOICE_FREQUENCY}(?:约|不足|少于|超过)?{_CHOICE_NUMBER}'
    rf'(?:(?:至|～|~|－|-)(?:不足)?{_CHOICE_NUMBER})?{_CHOICE_UNIT}(?:[／/]{_CHOICE_UNIT})*'
)


def _combines_choice_answers(label: str) -> bool:
    """List separators cannot hide several answers; quantity syntax is distinct."""
    if not re.search(r'或|、|[／/]', label):
        return False
    # A range tail describes the same quantity, not a second behavior or disease.
    if re.fullmatch(_CHOICE_QUANTITY + r'或(?:更少|更短|更低|更早|更晚|更多|更长|更高|以上|以下|以内)', label):
        return False
    if re.fullmatch(_CHOICE_QUANTITY, label):
        return False
    # A fractional amount still has one unit and one meaning.
    if re.fullmatch(rf'{_CHOICE_FREQUENCY}{_CHOICE_NUMBER}[／/]{_CHOICE_NUMBER}{_CHOICE_UNIT}', label):
        return False
    return True


def validate_choice_labels(options: list[str]) -> None:
    """New shortcuts name one answer; explanations belong in the question."""
    broad_labels = {
        '身体不适或症状',
        '身体不适',
        '健康问题',
        '健康管理',
        '慢性病管理',
        '报告解读',
        '检查结果的意义',
        '最近的身体变化',
        '日常生活如何调整',
        '整体健康情况',
        '日常体力与精力',
        '饮食与体重管理',
        '长期健康维护',
        '睡眠质量',
    }
    for option in options:
        compact = re.sub(r'\s+', '', option)
        if (
            _AMBIGUOUS_CHOICE.search(compact)
            or compact in _ENTRY_ONLY_CHOICES | broad_labels
            or re.search(r'某|[（(\[【]|[）)\]】]', compact)
            or _combines_choice_answers(compact)
            or len(compact) > 24
        ):
            raise QuestionnairePresentationError(
                'ambiguous_shortcut_forbidden',
                '快捷选项须简短、独立且明确；不使用泛化占位、括号说明或拼接不同答案，选项外内容通过文字或语音回答',
            )


def validate_answer_entry_instructions(text: str, *, options: list[str] | None = None) -> None:
    """Guard published components before checking button-input instructions."""
    validate_internal_notice(text)
    compact = re.sub(r'\s+', '', text)
    patterns = list(_UI_ENTRY_PATTERNS)
    if options:
        labels = '|'.join(re.escape(re.sub(r'\s+', '', label)) for label in options if label)
        if labels:
            patterns.append(
                re.compile(
                    rf'(?:在|到){_QUOTE_OPEN}?(?:{labels}){_QUOTE_CLOSE}?(?:(?:选项|按钮)(?:里|中|内|处)?|里|中|内|处)(?:直接|继续|再)?{_TEXT_ENTRY_ACTION}'
                )
            )
    for pattern in patterns:
        for match in pattern.finditer(compact):
            if _NEGATED_ENTRY.search(compact[max(0, match.start() - 8) : match.start()]):
                continue
            if pattern is _UI_ENTRY_PATTERNS[-1]:
                popup = re.search(r'弹出|打开|出现|展开', match.group())
                if popup and _NEGATED_ENTRY.search(match.group()[: popup.start()]):
                    continue
            raise QuestionnairePresentationError(
                'invalid_answer_entry_instruction',
                '选项按钮只能选择，不能填写或弹出输入框；补充内容应通过现有文字或语音入口回答',
            )


def normalize_option(value: str) -> str:
    """Remove known UI instructions, never reinterpret an answer's meaning."""
    value = _OPTION_INSTRUCTION.sub('', value.strip()).strip()
    if '请补充' in value:
        raise QuestionnairePresentationError(
            'option_contains_instruction', '选项不能包含操作提示，请将补充说明放在问题末尾'
        )
    return value


_REFUSAL_SHORTCUTS = (
    '不想回答',
    '不愿回答',
    '不愿意回答',
    '暂不回答',
    '不方便',
    '不便回答',
    '不想补充',
    '不愿补充',
    '不愿意补充',
    '暂不补充',
    '不想说',
    '不愿说',
    '不愿透露',
    '暂时不说',
    '以后再说',
    '跳过',
    '稍后回答',
)


class GeneratedQuestion(PolicyModel):
    question: str = Field(min_length=1, max_length=2000)
    questionType: Literal['text', 'radio', 'checkbox']
    options: dict[str, str] | list[str]

    @field_validator('options')
    @classmethod
    def normalize_options(cls, value):
        return [normalize_option(item) for item in value] if isinstance(value, list) else value

    @model_validator(mode='after')
    def validate_options(self):
        if self.questionType == 'text':
            if not isinstance(self.options, dict) or not self.options:
                raise QuestionnairePresentationError('text_input_required', '文本问题必须提供至少一个输入字段')
        elif not isinstance(self.options, list) or (
            len(self.options) < 2 and not (self.questionType == 'radio' and self.options == [NO_MORE_OPTION])
        ):
            raise QuestionnairePresentationError('choice_options_required', '选择题必须提供至少两个选项')
        elif any(not item for item in self.options) or len(set(self.options)) != len(self.options):
            raise QuestionnairePresentationError('invalid_choice_options', '选择题选项不能为空或重复')
        return self


def validate_new_question(question: GeneratedQuestion, *, action: str) -> None:
    """Frozen ask_intake validation; Record's other actions are not a Q policy."""
    if action != 'ask_intake':
        raise ValueError('Unsupported Questionnaire question action')
    if question.questionType not in ('radio', 'checkbox'):
        raise QuestionnairePresentationError(
            'question_choices_required',
            '每道新问题都必须使用单选或多选快捷选项，同时支持自定义回答',
        )
    if len(question.options) < 2:
        raise QuestionnairePresentationError('ordinary_choice_options_required', '常规选择题必须提供至少两个选项')
    if any(marker in option for option in question.options for marker in _REFUSAL_SHORTCUTS):
        raise QuestionnairePresentationError(
            'refusal_shortcut_forbidden', '快捷选项不能提供拒绝回答或跳过选项；保留用户自由输入'
        )
    validate_choice_labels(question.options)
    validate_answer_entry_instructions(question.question, options=question.options)
