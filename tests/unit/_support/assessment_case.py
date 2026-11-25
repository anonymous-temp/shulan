"""Small synthetic whole-case fixtures shared by the ``assessment_case_*`` tests (offline, no patient data)."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from modules.report.assessment_case_sheet import CaseSheet
from modules.report.assessment_case_sheet import build_case_sheet
from modules.report.schemas import ReportSourceFact

SURVEY_SCHEMA = {
    'title': '健康问卷',
    'pages': [
        {
            'elements': [
                {
                    'name': 'q_sleep',
                    'title': '睡眠情况',
                    'choices': [{'value': 'a', 'text': '睡眠充足'}, {'value': 'b', 'text': '入睡困难'}],
                },
                {'name': 'q_symptom', 'title': '近期不适', 'choices': ['乏力', '腹胀', '便秘']},
                {'name': 'q_family', 'title': '家族史', 'choices': [{'value': 'x', 'text': '父亲糖尿病'}]},
                {'name': 'q_empty', 'title': '未答题'},
            ]
        }
    ],
}
SURVEY_ANSWER = {'q_sleep': 'b', 'q_symptom': ['乏力', '腹胀'], 'q_family': 'x', 'q_empty': ''}


def fact(
    fact_id,
    name,
    value,
    *,
    kind='clinical',
    unit=None,
    reference_range=None,
    status=None,
    date='2026-06-30',
    section='一般检查',
    source=None,
    abnormal=False,
    imaging=False,
    reported_assertion_kind=None,
):
    return ReportSourceFact(
        fact_id=fact_id,
        kind=kind,
        name=name,
        value=value,
        unit=unit,
        reference_range=reference_range,
        status=status,
        date=date,
        source=source if source is not None else f'检查类别：{section}；样例',
        abnormal=abnormal,
        imaging=imaging,
        reported_assertion_kind=reported_assertion_kind,
    )


def view_row(
    code,
    name,
    value,
    *,
    unit=None,
    reference=None,
    status=None,
    level='normal',
    direction=None,
    observed='2026-06-30',
    **extra,
):
    return {
        'code': code,
        'name': name,
        'value': value,
        'unit': unit,
        'referenceRange': reference,
        'status': status,
        'level': level,
        'direction': direction,
        'observedAt': observed,
        **extra,
    }


def survey(answer=None, schema=None, submitted=None):
    return SimpleNamespace(
        schema_=deepcopy(schema or SURVEY_SCHEMA), answer=deepcopy(answer or SURVEY_ANSWER), submitted_at=submitted
    )


def request(*, surveys=None, no_survey_attrs=False, sex=None, age=None):
    if no_survey_attrs:
        return SimpleNamespace(sex=sex, age=age)
    return SimpleNamespace(survey_list=surveys, sex=sex, age=age)


def parsed(sex='男', age=48):
    return SimpleNamespace(subject=SimpleNamespace(sex=sex, age=age), disease_results=[])


def document(*facts):
    return SimpleNamespace(facts=list(facts))


def default_facts():
    return [
        fact('fact-bmi', '体重指数(BMI)', '29.3', unit='kg/m2', section='一般检查'),
        fact('fact-glu', '空腹葡萄糖(Glu)', '5.74', unit='mmol/L', reference_range='3.89-6.11', section='糖代谢'),
        fact(
            'fact-tg',
            '甘油三酯(TG)',
            '3.34',
            unit='mmol/L',
            reference_range='0.00-1.70',
            status='偏高',
            section='血脂7项',
            abnormal=True,
        ),
        fact('fact-fobt', '粪便隐血', '弱阳性', section='便常规', abnormal=True),
        fact('fact-us', '腹部超声', '脂肪肝；胆囊息肉 5mm；肾囊肿', section='超声', imaging=True, abnormal=True),
        fact(
            'derived-fact-tyg',
            'TyG 指数',
            '8.9',
            reference_range='<8.5',
            status='偏高',
            source='ln(TG×Glu/2)',
            abnormal=True,
        ),
        fact('fact-adv', '主检结论：建议复查血脂', '三个月后复查', section='主检'),
    ]


def default_clinical():
    glu = view_row(
        'fact-glu',
        '空腹葡萄糖(Glu)',
        '5.74',
        unit='mmol/L',
        reference='3.89-6.11',
        level='normal',
        target={
            'text': '<5.6 mmol/L',
            'sources': [{'title': 'ADA 糖尿病诊疗标准', 'doi': '10.0/ada'}],
            'evidenceLevel': '指南',
        },
    )
    tg = view_row(
        'fact-tg',
        '甘油三酯(TG)',
        '3.34',
        unit='mmol/L',
        reference='0.00-1.70',
        status='偏高',
        level='focus',
        direction='up',
    )
    fobt = view_row('fact-fobt', '粪便隐血', '弱阳性', level='focus')
    spans = [
        view_row('fact-us:span:a:b', '腹部超声 · 肝脏', '脂肪肝', level='borderline', direction='up'),
        view_row('fact-us:span:c:d', '腹部超声 · 胆囊', '胆囊息肉 5mm', level='borderline'),
        view_row('fact-us:span:e:f', '腹部超声 · 肾脏', '肾囊肿', level='borderline'),
    ]
    tyg = view_row('derived-fact-tyg', 'TyG 指数', '8.9', reference='<8.5', status='偏高', level='borderline')
    return {
        'bodyMetrics': [view_row('fact-bmi', '体重指数(BMI)', '29.3', unit='kg/m2', level='borderline')],
        'clinicalGroups': [
            {'code': 'cardiometabolic', 'name': '糖脂代谢', 'rows': [glu, tg, tyg]},
            {'code': 'organMetabolism', 'name': '肝胆胰', 'rows': [fobt, *spans]},
        ],
        'specialtyFindings': [],
        'normalBaselines': [],
        'digestiveContext': [],
    }


def default_microbiome():
    low = view_row(
        'm-akk', '阿克曼菌', '0.1', unit='%', reference='0.5-3', status='偏低', level='focus', direction='down'
    )
    div = view_row('m-shannon', '香农多样性', '3.2', reference='2.5-4', level='normal')
    path = view_row('m-scfa', '短链脂肪酸合成', '42', unit='分', reference='60-100', status='偏低', level='borderline')
    return {
        'structureGroups': [{'code': 'diversity', 'name': '多样性', 'rows': [div], 'panelStates': []}],
        'organismGroups': [
            {
                'code': 'beneficial',
                'name': '有益菌',
                'rows': [low],
                'panelStates': [],
                'normalBaseline': {'rows': [view_row('m-bifi', '双歧杆菌', '2.0', unit='%')], 'count': 1},
            }
        ],
        'functionalGroups': [{'code': 'scfa', 'name': '短链脂肪酸', 'rows': [path], 'panelStates': []}],
        'structure': [],
        'sampleContext': {'sampleType': '粪便', 'sampleDate': '2026-11-02'},
    }


def build_sheet(*, facts=None, clinical=None, microbiome=None, req=None, timing=None, disease_models=None) -> CaseSheet:
    return build_case_sheet(
        request=req if req is not None else request(surveys=[survey()]),
        parsed=parsed(),
        document=document(*(default_facts() if facts is None else facts)),
        clinical=default_clinical() if clinical is None else clinical,
        microbiome=default_microbiome() if microbiome is None else microbiome,
        disease_models=disease_models,
        timing=timing,
    )


def handle_of(sheet: CaseSheet, fact_id: str) -> str:
    return next(handle for handle, entry in sheet.entries.items() if entry.get('factId') == fact_id)


def handle_named(sheet: CaseSheet, name: str, kind: str | None = None) -> str:
    return next(
        handle
        for handle, entry in sheet.entries.items()
        if entry['name'] == name and (kind is None or entry['kind'] == kind)
    )


def raw_analysis(sheet: CaseSheet) -> dict:
    """A model answer of a realistic shape, built from the handles of ``build_sheet()``."""
    glu, tg, fobt, us = (handle_of(sheet, key) for key in ('fact-glu', 'fact-tg', 'fact-fobt', 'fact-us'))
    bmi, tyg = handle_of(sheet, 'fact-bmi'), handle_of(sheet, 'derived-fact-tyg')
    symptom = handle_named(sheet, '近期不适', 'reported')
    sleep = handle_named(sheet, '睡眠情况', 'reported')
    family = handle_named(sheet, '家族史', 'reported')
    micro = handle_named(sheet, '阿克曼菌', 'microbiome')
    return {
        'phenotype': {'label': '代谢性脂肪肝倾向', 'handles': [tg, us]},
        'dataNotes': [{'kind': 'calibration', 'title': '读数校准', 'note': '以空腹读数为准', 'handles': [glu]}],
        'bodyRows': [{'title': '体重指数', 'handles': [bmi], 'ideal': '<24', 'status': '高于理想值'}],
        'systems': [
            {
                'code': 'cardiometabolic',
                'status': '重点关注',
                'keyHandles': [tg],
                'rows': [
                    {'title': '血脂与代谢', 'handles': [tg, tyg], 'ideal': '<1.7', 'status': '重点关注'},
                    {'title': '血糖', 'handles': [glu], 'status': '稳健达标'},
                ],
            },
            {
                'code': 'digestiveRenal',
                'status': '重点关注',
                'keyHandles': [fobt],
                'rows': [
                    {'title': '粪便隐血', 'handles': [fobt], 'status': '重点关注'},
                    {'title': '肝脏', 'handles': [us], 'status': '亚临床边界'},
                ],
            },
            {'code': 'microbiome', 'status': '亚临床边界', 'keyHandles': [micro], 'rows': []},
        ],
        'symptoms': [{'name': '乏力腹胀', 'handles': [symptom], 'clinicalHandles': [tg], 'microbiomeHandles': [micro]}],
        'clusters': [
            {
                'name': '糖脂代谢—脂肪肝',
                'priority': 'P1',
                'clinicalHandles': [tg, us],
                'microbiomeHandles': [micro],
                'reportedHandles': [sleep],
            },
            {'name': '隐血—消化道', 'priority': 'P2', 'clinicalHandles': [fobt]},
        ],
        'priorityItems': [
            {
                'title': '粪便隐血查因',
                'timeframe': '1 个月内',
                'days': 30,
                'department': '消化内科',
                'checks': ['结肠镜'],
                'handles': [fobt],
                'cluster': 2,
            }
        ],
        'followups': [
            {
                'title': '胆囊息肉',
                'department': '肝胆外科',
                'interval': '每 6 个月',
                'days': 180,
                'checks': ['超声'],
                'handles': [us],
            }
        ],
        'testGaps': [{'test': '糖耐量试验', 'because': '空腹血糖接近上限', 'handles': [glu]}],
        'background': {'handles': [family]},
        'lifestyle': [{'dimension': 'sleep', 'handles': [sleep], 'clusters': [1]}],
        'protective': {'handles': []},
        'recognitions': [{'title': '关于血糖的校准', 'handles': [glu]}],
    }


class FakeClient:
    """``generate(payload)`` answers by ``payload['task']``; handlers may be dicts, callables or exceptions."""

    def __init__(self, **handlers):
        self.handlers = handlers
        self.calls: list[dict] = []

    async def generate(self, payload):
        self.calls.append(payload)
        handler = self.handlers[payload['task']]
        if callable(handler):
            handler = handler(payload)
        if isinstance(handler, BaseException):
            raise handler
        return handler

    def tasks(self, task):
        return [payload for payload in self.calls if payload['task'] == task]
