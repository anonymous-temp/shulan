"""Health Analysis-owned collection policy, frozen independently of legacy tasks."""

from __future__ import annotations

import re
import unicodedata
from copy import deepcopy

from modules.health_analysis.surveys import InvalidSurveyError
from modules.health_analysis.surveys import decode_surveys

POLICY_VERSION = 'health-analysis-policy-v5'
CATALOGUE_VERSION = 'health-analysis-lifestyle-catalogue-v3'
MAX_QUESTIONS = 20
BACKGROUND_UNKNOWN_TEXT = '不清楚'
SURVEY_TITLE = '健康情况补充问卷'
SURVEY_DESCRIPTION = '请根据题目范围选择符合实际情况的选项；可以保留未回答的问题。'
SURVEY_PAGE_NAME = 'health_supplement'
LIFESTYLE_TARGETS = {
    'diet_structure': {
        'question': '您平时的正餐通常包括哪些类食物？可多选。',
        'questionType': 'checkbox',
        'options': ['谷薯类主食', '蔬菜类', '水果类', '畜禽肉类', '水产类', '蛋类', '豆类及豆制品'],
        'optionValues': (
            'staple',
            '4',
            '5',
            'meat_poultry',
            'aquatic_foods',
            'eggs',
            'beans_soy_products',
        ),
    },
    'staple_portion': {
        'question': '您平时一餐的米饭、面食等主食大约吃多少？',
        'questionType': 'radiogroup',
        'options': ['约1拳', '约2拳', '超过2拳', '不吃主食'],
    },
    'sweet_intake': {
        'question': '您平时吃甜食或喝含糖饮料的频率更接近哪一种？',
        'questionType': 'radiogroup',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不摄入'],
    },
    'salt_processed_food': {
        'question': '腌制品、咸味零食或加工肉类，您平时吃得频繁吗？',
        'questionType': 'radiogroup',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不吃'],
    },
    'meal_pattern': {
        'question': '您平时的进餐时间通常规律吗？',
        'questionType': 'radiogroup',
        'options': ['时间固定', '时间不固定'],
    },
    'late_meals': {
        'question': '您平时临睡前吃夜宵的频率更接近哪一种？',
        'questionType': 'radiogroup',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不吃夜宵'],
    },
    'hydration': {
        'question': '您平时主要喝哪些饮品？可多选。',
        'questionType': 'checkbox',
        'options': ['白水', '茶', '咖啡', '含糖饮料'],
    },
    'water_intake': {
        'question': '您平时一天喝水的量大约是多少？',
        'questionType': 'radiogroup',
        'options': ['不足500毫升', '500至不足1000毫升', '1000至不足1500毫升', '1500毫升及以上'],
    },
    'sleep': {
        'question': '您平时通常几点开始准备睡觉？',
        'questionType': 'radiogroup',
        'options': ['晚上22点前', '22点到次日0点前', '次日0点及以后', '时间不固定'],
    },
    'sleep_duration': {
        'question': '您最近平均每天实际睡了多久？',
        'description': '包括午睡等白天睡着的时间，只计算实际睡着的时间。',
        'questionType': 'radiogroup',
        'options': ['不足6小时', '6至不足7小时', '7至不足9小时', '9小时及以上'],
    },
    'sleep_consistency': {
        'question': '工作日和休息日相比，您的睡起时间变化大吗？',
        'questionType': 'radiogroup',
        'options': ['相差不足1小时', '相差1小时至不足2小时', '相差2小时及以上', '时间不固定'],
    },
    'bedtime_habits': {
        'question': '临睡前一小时，您通常会做哪些事？可多选。',
        'questionType': 'checkbox',
        'options': ['看手机', '用电脑', '阅读', '听音乐', '工作', '学习', '洗漱后休息'],
    },
    'activity': {
        'question': '您平时主要通过哪些方式活动身体？可多选。',
        'questionType': 'checkbox',
        'options': ['步行', '跑步', '骑行', '游泳', '力量训练', '打球', '家务劳动'],
        'noneText': '以上均无',
    },
    'exercise_frequency': {
        'question': '您平时一周大约有几天安排运动？',
        'questionType': 'radiogroup',
        'options': ['没有运动', '每周1～2天', '每周3～4天', '每周5～7天'],
    },
    'exercise_duration': {
        'question': '有安排运动时，您每次通常持续多久？',
        'questionType': 'radiogroup',
        'options': ['不足15分钟', '15至不足30分钟', '30至不足60分钟', '60分钟及以上', '没有运动'],
    },
    'sedentary': {
        'question': '您平时连续坐着、不起身的时间通常有多久？',
        'questionType': 'radiogroup',
        'options': ['不足30分钟', '30至不足60分钟', '1小时至不足2小时', '2小时及以上'],
    },
    'activity_breaks': {
        'question': '工作或学习时，您通常多久会起身走动一次？',
        'questionType': 'radiogroup',
        'options': ['不足30分钟', '30至不足60分钟', '1小时至不足2小时', '2小时及以上', '没有固定安排'],
    },
    'smoking': {
        'question': '您目前的吸烟情况更接近哪一种？',
        'questionType': 'radiogroup',
        'options': ['目前吸烟', '已戒烟', '从不吸烟'],
    },
    'passive_smoking': {
        'question': '您平时在家中、工作或社交场合接触二手烟多吗？',
        'questionType': 'radiogroup',
        'options': ['每天接触', '每周接触1～6天', '不足每周1天', '不接触'],
    },
    'alcohol': {
        'question': '您平时的饮酒频率更接近哪一种？',
        'questionType': 'radiogroup',
        'options': ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '不饮酒'],
    },
    'alcohol_amount': {
        'question': '喝酒时，您一次的饮酒量通常相当于多少杯？',
        'questionType': 'radiogroup',
        'options': ['不足1杯', '1杯', '2杯', '3杯及以上', '不饮酒'],
    },
    'stress_coping': {
        'question': '忙碌或压力较大时，您通常用哪些方式放松？可多选。',
        'questionType': 'checkbox',
        'options': ['散步', '运动', '与亲友交流', '阅读', '听音乐', '休息', '独处'],
    },
    'rest_schedule': {
        'question': '在工作、学习或家务之外，您平时通常怎样留时间休息或放松？',
        'questionType': 'radiogroup',
        'options': [
            '平日也有固定的休息时间',
            '只有休息日才固定安排',
            '没有固定时间，有空时休息',
            '通常不专门留时间休息',
        ],
        'optionValues': ('fixed_regular_days', 'fixed_days_off', '3', '4'),
    },
    'work_breaks': {
        'question': '工作或学习忙碌时，您的休息节奏更接近哪一种？',
        'questionType': 'radiogroup',
        'options': ['定时休息', '任务间休息', '忙完才休息', '没有固定节奏'],
    },
}

# Published templates for the lifestyle targets whose titles changed.
LEGACY_LIFESTYLE_QUESTIONS = {
    'diet_structure': (
        '您平时的正餐通常吃到哪些食物？可多选。',
        'checkbox',
        ('米饭', '面条', '杂粮', '蔬菜', '水果', '猪肉', '鱼肉', '鸡蛋', '豆制品'),
    ),
    'sleep': (
        '您平时通常在什么时间准备睡觉？',
        'radiogroup',
        ('22点前', '22点至午夜前', '午夜及以后', '时间不固定'),
    ),
    'sleep_duration': (
        '您最近通常每天睡多长时间？',
        'radiogroup',
        ('不足6小时', '6至7小时', '7至9小时', '9小时及以上', '不清楚'),
    ),
    'rest_schedule': (
        '日常生活中，您通常怎样安排属于自己的休息时间？',
        'radiogroup',
        ('每天固定休息', '休息日休息', '有空时休息', '没有专门休息'),
    ),
}

_BACKGROUND = (
    (
        'health_goals',
        '您最近有哪些身体不适，或希望改善哪些方面？',
        ('便秘', '反酸', '入睡困难', '容易疲劳', '减轻体重', '增加肌肉', '肩颈疼痛', '缓解压力'),
        '暂无明显问题',
    ),
    (
        'medical_history',
        '医生曾经确诊过您患有以下哪些疾病？',
        ('高血压', '糖尿病', '血脂异常', '冠心病', '脑卒中', '慢性肾病', '脂肪肝'),
        '以上均无',
    ),
    (
        'family_history',
        '有血缘关系的亲属患过以下哪些疾病？',
        ('高血压', '糖尿病', '冠心病', '脑卒中', '肺癌', '结直肠癌', '地中海贫血', '血友病'),
        '以上均无',
    ),
)
_GROUPS = (
    ('diet_structure', 'sleep', 'activity', 'smoking', 'stress_coping'),
    ('staple_portion', 'sleep_duration', 'exercise_frequency', 'passive_smoking', 'rest_schedule'),
    ('sweet_intake', 'sleep_consistency', 'exercise_duration', 'alcohol', 'work_breaks'),
    ('salt_processed_food', 'bedtime_habits', 'sedentary', 'alcohol_amount'),
    ('meal_pattern', 'activity_breaks', 'late_meals', 'hydration', 'water_intake'),
)
DEFAULT_ORDER = tuple(key for row in _GROUPS for key in row)
_FORBIDDEN = re.compile(
    r'性生活|性行为|性功能|性欲|性传播|性征|勃起|精液|睾丸|阴茎|阴道|外阴'
    r'|后台处理|模型失败|解析降级|系统故障'
)
_ENTRY = re.compile(r'其他.{0,5}(?:填写|输入)|请(?:填写|输入|上传|描述)|点击.{0,5}输入|拒答|拒绝回答|暂不透露')


_UNKNOWN_OPTION = re.compile(
    r'不(?:太|很|大)?清楚|不知道|不确定|记不(?:太清|清|住|得)|不记得|不详|不了解|没印象'
    r'|无法(?:确定|判断|回忆|回答)|说不清'
    r'|^(?:unknown|notknown|unsure|notsure|dontknow|idontknow|uncertain)$'
)


_SURVEY_RULES = (
    (
        'required_background_topics',
        'survey',
        '核对本次缺答基础主题及顺序，与应问清单一致。基础题保留「不清楚」选项及原编码。',
    ),
    ('survey_composition', 'survey', '核对标题、说明与作答方式，题目组合只收集本次适用且尚未覆盖的信息。'),
    ('survey_duplicates', 'survey', '逐题比较采集目标、实际问法和选项，识别卷内重复。'),
    ('question_applicability', 'question', '问题适合本次用户，有明确不适用依据的生活题可以取消。'),
    (
        'question_wording',
        'question',
        '用自然、可直接回答的题干表达一个采集目标。需要交代场景的生活题写清具体情境。',
    ),
    (
        'options_match_question',
        'option',
        '选项同层级、可区分并覆盖题干范围。时点用具体时刻，时长说清统计范围，单选区间无重叠。',
    ),
    (
        'unknown_option',
        'option',
        '生活题已有完备具体选项时移除不清楚、记不清等未知占位项，无法判断可留空。保留时间不固定、没有运动、从不饮酒等真实情况。',
    ),
)
_TARGET_RULES = (
    (
        'bedtime_clock',
        'question',
        'sleep',
        '单选，问开始准备睡觉的时刻。用晚上和次日的具体时刻分档，22点和次日0点须各归唯一一档，保留时间不固定。',
    ),
    (
        'sleep_duration_scope',
        'question',
        'sleep_duration',
        '单选，问每天实际睡眠总时长，包括午睡等白天睡着的时间，只计算睡着的时间。',
    ),
    (
        'rest_schedule_scene',
        'question',
        'rest_schedule',
        '单选，用工作、学习或家务之外的场景询问休息安排，区分固定安排、临时休息和通常不安排。',
    ),
    (
        'diet_option_categories',
        'option',
        'diet_structure',
        '多选，正餐构成的可选项用同层级的食物大类。具体食物可放在括号内解释类别。',
    ),
)
_FIXED_TYPE_TARGETS = frozenset({'sleep', 'sleep_duration', 'rest_schedule', 'diet_structure'})
_COMPLETE_OPTION_TARGETS = frozenset(
    {
        'meal_pattern',
        'sweet_intake',
        'salt_processed_food',
        'late_meals',
        'water_intake',
        'sleep',
        'sleep_duration',
        'sleep_consistency',
        'exercise_frequency',
        'exercise_duration',
        'sedentary',
        'activity_breaks',
        'smoking',
        'passive_smoking',
        'alcohol',
        'alcohol_amount',
        'rest_schedule',
    }
)


def survey_rules() -> list[dict]:
    rules = [{'ruleId': key, 'scope': scope, 'criterion': criterion} for key, scope, criterion in _SURVEY_RULES]
    rules.extend(
        {'ruleId': key, 'scope': scope, 'targetIds': [target], 'criterion': criterion}
        for key, scope, target, criterion in _TARGET_RULES
    )
    return rules


def _is_unknown_label(label: str) -> bool:
    normalized = unicodedata.normalize('NFKC', label).casefold()
    return bool(_UNKNOWN_OPTION.search(''.join(char for char in normalized if char.isalnum())))


def has_unknown_option(question: dict) -> bool:
    """Check displayed options in a structurally valid new question."""
    labels = [choice.get('text', choice['value']) for choice in question.get('choices', ())]
    return any(_is_unknown_label(label) for label in [*labels, question.get('noneText', '')])


def remove_covered_unknown_options(target_id: str, question: dict) -> dict:
    """Prune placeholders when a validated new question has the catalogue's complete set."""
    if target_id not in _COMPLETE_OPTION_TARGETS or question['type'] != 'radiogroup':
        return question
    choices = question['choices']
    concrete = [choice for choice in choices if not _is_unknown_label(choice.get('text', choice['value']))]
    if len(concrete) == len(choices):
        return question

    labels = {choice.get('text', choice['value']).strip() for choice in concrete}
    if labels == set(LIFESTYLE_TARGETS[target_id]['options']):
        return {**question, 'choices': concrete}
    return question


def catalogue() -> list[dict]:
    return [{'id': key, 'parentId': None, 'question': template_question(key)} for key in DEFAULT_ORDER]


def template_question(key: str) -> dict:
    row = LIFESTYLE_TARGETS[key]
    labels = row['options']
    values = row.get('optionValues') or tuple(str(index) for index in range(1, len(labels) + 1))
    question = {
        'name': key,
        'type': row['questionType'],
        'title': row['question'],
        'isRequired': False,
        'choices': [{'value': value, 'text': label} for value, label in zip(values, labels, strict=True)],
    }
    if 'description' in row:
        question['description'] = row['description']
    if 'noneText' in row:
        question.update(showNoneItem=True, noneValue='none', noneText=row['noneText'])
    return question


def background_questions() -> list[dict]:
    return [
        {
            'name': name,
            'type': 'checkbox',
            'title': title,
            'isRequired': False,
            'choices': [
                {'value': str(index), 'text': text}
                for index, text in enumerate((*options, BACKGROUND_UNKNOWN_TEXT), start=1)
            ],
            'showNoneItem': True,
            'noneValue': 'none',
            'noneText': none_text,
        }
        for name, title, options, none_text in _BACKGROUND
    ]


def suitable_text(value: str, gender: str | None) -> bool:
    if _FORBIDDEN.search(value):
        return False
    if gender == 'male' and any(word in value for word in ('子宫', '宫颈')):
        return False
    if gender == 'female' and any(word in value for word in ('前列腺', '睾丸')):
        return False
    return True


def validate_question(question: dict, gender: str | None, *, target_id: str | None = None) -> dict | None:
    if (
        not isinstance(question, dict)
        or not isinstance(question.get('type'), str)
        or question['type'] not in {'radiogroup', 'checkbox'}
    ):
        return None
    if target_id in _FIXED_TYPE_TARGETS and question['type'] != LIFESTYLE_TARGETS[target_id]['questionType']:
        return None
    try:
        decode_surveys(
            [{'schema': {'title': '发布校验', 'pages': [{'name': 'p', 'elements': [question]}]}, 'answer': {}}]
        )
    except InvalidSurveyError:
        return None
    choices = question.get('choices', [])
    labels = [choice.get('text', choice['value']) for choice in choices]
    texts = [question['title'], question.get('description', ''), question.get('noneText', ''), *labels]
    if (
        not 2 <= len(choices) <= 10
        or len(question['title']) > 180
        or len(question.get('description', '')) > 400
        or any(len(label) > 70 or not label.strip() for label in labels)
        or len(set(labels)) != len(labels)
        or any(not suitable_text(text, gender) for text in texts)
        or any(_ENTRY.search(text) for text in texts)
    ):
        return None
    result = deepcopy(question)
    result['isRequired'] = False
    return result


def build_schema(questions: list[dict]) -> dict | None:
    if not questions:
        return None
    if len(questions) > MAX_QUESTIONS:
        raise ValueError('Health Analysis question count exceeds policy')
    return {
        'title': SURVEY_TITLE,
        'description': SURVEY_DESCRIPTION,
        'pages': [{'name': SURVEY_PAGE_NAME, 'elements': deepcopy(questions)}],
    }
