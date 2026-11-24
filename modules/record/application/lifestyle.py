"""Controlled lifestyle purposes and safe deterministic question drafts."""

from copy import deepcopy


def _topic(label, category, profile_type, question, options, *, multiple=False):
    return {
        'label': label,
        'category': category,
        'profileType': 'lifestyle.' + profile_type,
        'question': question,
        'questionType': 'checkbox' if multiple else 'radio',
        'options': options,
    }


TOPICS = {
    'diet_structure': _topic(
        '饮食结构',
        'nutrition',
        'diet_pattern',
        '您平时的正餐通常吃到哪些食物？可多选。',
        ['米饭', '面条', '杂粮', '蔬菜', '水果', '猪肉', '鱼肉', '鸡蛋', '豆制品'],
        multiple=True,
    ),
    'staple_portion': _topic(
        '主食份量',
        'nutrition',
        'diet_pattern',
        '您平时一餐的米饭、面食等主食大约吃多少？',
        ['约1拳', '约2拳', '超过2拳', '不吃主食', '不清楚'],
    ),
    'sweet_intake': _topic(
        '甜食与甜饮',
        'nutrition',
        'diet_pattern',
        '您平时吃甜食或喝含糖饮料的频率更接近哪一种？',
        ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不摄入', '不清楚'],
    ),
    'salt_processed_food': _topic(
        '咸味加工食品',
        'nutrition',
        'diet_pattern',
        '腌制品、咸味零食或加工肉类，您平时吃得频繁吗？',
        ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不吃', '不清楚'],
    ),
    'meal_pattern': _topic(
        '进食规律',
        'nutrition',
        'meal_regularity',
        '您平时的进餐时间通常规律吗？',
        ['时间固定', '时间不固定', '不清楚'],
    ),
    'late_meals': _topic(
        '夜宵习惯',
        'nutrition',
        'meal_regularity',
        '您平时临睡前吃夜宵的频率更接近哪一种？',
        ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '从不吃夜宵', '不清楚'],
    ),
    'hydration': _topic(
        '饮水情况',
        'nutrition',
        'hydration',
        '您平时主要喝哪些饮品？可多选。',
        ['白水', '茶', '咖啡', '含糖饮料'],
        multiple=True,
    ),
    'water_intake': _topic(
        '饮水量',
        'nutrition',
        'hydration',
        '您平时一天喝水的量大约是多少？',
        ['不足500毫升', '500至1000毫升', '1000至1500毫升', '1500毫升及以上', '不清楚'],
    ),
    'sleep': _topic(
        '睡眠情况',
        'sleep',
        'sleep',
        '您平时通常在什么时间准备睡觉？',
        ['22点前', '22点至午夜前', '午夜及以后', '时间不固定'],
    ),
    'sleep_duration': _topic(
        '睡眠时长',
        'sleep',
        'sleep',
        '您最近通常每天睡多长时间？',
        ['不足6小时', '6至7小时', '7至9小时', '9小时及以上', '不清楚'],
    ),
    'sleep_consistency': _topic(
        '作息规律',
        'sleep',
        'sleep',
        '工作日和休息日相比，您的睡起时间变化大吗？',
        ['相差不足1小时', '相差1至2小时', '相差2小时及以上', '时间不固定', '不清楚'],
    ),
    'bedtime_habits': _topic(
        '睡前习惯',
        'sleep',
        'sleep',
        '临睡前一小时，您通常会做哪些事？可多选。',
        ['看手机', '用电脑', '阅读', '听音乐', '工作', '学习', '洗漱后休息'],
        multiple=True,
    ),
    'activity': _topic(
        '日常活动',
        'activity',
        'physical_activity',
        '您平时主要通过哪些方式活动身体？可多选。',
        ['步行', '跑步', '骑行', '游泳', '力量训练', '打球', '家务劳动', '没有主动活动'],
        multiple=True,
    ),
    'exercise_frequency': _topic(
        '运动频次',
        'activity',
        'physical_activity',
        '您平时一周大约有几天安排运动？',
        ['没有运动', '每周1～2天', '每周3～4天', '每周5～7天', '不清楚'],
    ),
    'exercise_duration': _topic(
        '运动时长',
        'activity',
        'physical_activity',
        '有安排运动时，您每次通常持续多久？',
        ['不足15分钟', '15至30分钟', '30至60分钟', '60分钟及以上', '没有运动', '不清楚'],
    ),
    'sedentary': _topic(
        '久坐情况',
        'activity',
        'sedentary_behavior',
        '您平时连续坐着、不起身的时间通常有多久？',
        ['不足30分钟', '30至60分钟', '1至2小时', '2小时及以上', '不清楚'],
    ),
    'activity_breaks': _topic(
        '起身活动',
        'activity',
        'sedentary_behavior',
        '工作或学习时，您通常多久会起身走动一次？',
        ['不足30分钟', '30至60分钟', '1至2小时', '2小时及以上', '没有固定安排'],
    ),
    'smoking': _topic(
        '吸烟情况',
        'substance_use',
        'smoking_status',
        '您目前的吸烟情况更接近哪一种？',
        ['目前吸烟', '已戒烟', '从不吸烟'],
    ),
    'passive_smoking': _topic(
        '二手烟接触',
        'substance_use',
        'smoking_status',
        '您平时在家中、工作或社交场合接触二手烟多吗？',
        ['每天接触', '每周接触', '不足每周1天', '不接触', '不清楚'],
    ),
    'alcohol': _topic(
        '饮酒情况',
        'substance_use',
        'alcohol_use',
        '您平时的饮酒频率更接近哪一种？',
        ['每天', '每周1～2天', '每周3～6天', '不足每周1天', '不饮酒', '不清楚'],
    ),
    'alcohol_amount': _topic(
        '单次饮酒量',
        'substance_use',
        'alcohol_use',
        '喝酒时，您一次通常喝几杯？也可以描述酒的种类和实际容量。',
        ['不足1杯', '1杯', '2杯', '3杯及以上', '不饮酒', '不清楚'],
    ),
    'stress_coping': _topic(
        '压力应对与放松',
        'stress_coping',
        'stress_coping',
        '忙碌或压力较大时，您通常用哪些方式放松？可多选。',
        ['散步', '运动', '与亲友交流', '阅读', '听音乐', '休息', '独处'],
        multiple=True,
    ),
    'rest_schedule': _topic(
        '日常休息安排',
        'stress_coping',
        'stress_coping',
        '日常生活中，您通常怎样安排属于自己的休息时间？',
        ['每天固定休息', '休息日休息', '有空时休息', '没有专门休息'],
    ),
    'work_breaks': _topic(
        '忙碌时的休息节奏',
        'stress_coping',
        'stress_coping',
        '工作或学习忙碌时，您的休息节奏更接近哪一种？',
        ['定时休息', '任务间休息', '忙完才休息', '没有固定节奏'],
    ),
}

LABELS = {dimension: topic['label'] for dimension, topic in TOPICS.items()}
CATEGORY_BY_DIMENSION = {dimension: topic['category'] for dimension, topic in TOPICS.items()}
CATEGORIES = {
    category: frozenset(dimension for dimension, value in CATEGORY_BY_DIMENSION.items() if value == category)
    for category in dict.fromkeys(CATEGORY_BY_DIMENSION.values())
}
PROFILE_TYPE_BY_DIMENSION = {dimension: topic['profileType'] for dimension, topic in TOPICS.items()}
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
DEFAULT_ORDER = (
    'diet_structure',
    'activity',
    'sleep',
    'stress_coping',
    'smoking',
    'meal_pattern',
    'exercise_frequency',
    'sleep_duration',
    'rest_schedule',
    'alcohol',
    'hydration',
    'sedentary',
    'sleep_consistency',
    'work_breaks',
    'passive_smoking',
    'staple_portion',
    'exercise_duration',
    'bedtime_habits',
    'alcohol_amount',
    'sweet_intake',
    'activity_breaks',
    'salt_processed_food',
    'late_meals',
    'water_intake',
)


def question_fields(dimension: str) -> dict:
    topic = TOPICS[dimension]
    return deepcopy({key: topic[key] for key in ('question', 'questionType', 'options')})


def bridge_text(dimension: str) -> str:
    return BRIDGES[dimension]
