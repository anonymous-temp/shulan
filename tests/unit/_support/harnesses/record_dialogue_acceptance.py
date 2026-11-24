"""Isolated Record dialogue acceptance, with synthetic patients and memory storage.

Default mode is offline. Example (run from the repository root)::

    python -m tests.unit.tools.record_dialogue_acceptance --output-dir C:/Temp/record-smoke
    python -m tests.api.scripts.record_dialogue_acceptance --live --config .env \
        --output-dir C:/Temp/record-live

Live mode calls only the configured LLM. It does not create document, database,
Redis, profile, retrieval, worker or callback dependencies. JSON validity alone
is not an acceptance result: traces include flow assertions and a prose review
rubric. Live prose still needs a human review of the saved synthetic dialogues.
Exit codes: 0 means trajectories finished and flow checks plus suite coverage
passed; 1 means a flow/runtime check failed; 2 means flow checks passed but a
trajectory is unfinished or extension coverage was not exercised. Natural early
closure alone is not a failed flow check.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import time
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[4]
SCENARIOS = ('multiple_complaints', 'extended_intake', 'final_turn')
FOCUS_SCENARIOS = ('wellness_goals', 'non_digestive_report', 'background_decline')
DIALOGUE_SCENARIOS = ('report_focus_rotation', 'single_goal_dialogue', 'no_goal_dialogue', 'free_text_expansion')
METABOLIC_REPORTS = [
    {
        'reportType': 'checkup',
        'title': '合成血糖检查报告',
        'status': 'abnormal',
        'reportTime': '2026-10-01',
        'aiSummary': '空腹血糖6.4 mmol/L，高于本报告参考范围3.9～6.1 mmol/L。',
        'expansion': [
            {
                'itemName': '空腹血糖',
                'resultValue': '6.4',
                'unit': 'mmol/L',
                'referenceRange': '3.9～6.1',
                'flag': 'high',
            }
        ],
    },
    {
        'reportType': 'checkup',
        'title': '合成血脂检查报告',
        'status': 'abnormal',
        'reportTime': '2026-10-01',
        'aiSummary': '甘油三酯2.1 mmol/L，高于本报告参考范围0.3～1.7 mmol/L。',
        'expansion': [
            {
                'itemName': '甘油三酯',
                'resultValue': '2.1',
                'unit': 'mmol/L',
                'referenceRange': '0.3～1.7',
                'flag': 'high',
            }
        ],
    },
]
EYE_REPORT = {
    'reportType': 'checkup',
    'title': '合成眼科复查报告',
    'status': 'normal',
    'reportTime': '2026-09-01',
    'aiSummary': '既往近视，已经配镜；建议按原就诊安排复查。',
    'expansion': [],
}
LLM_KEYS = ('SHULAN_LLM_PROVIDER', 'SHULAN_LLM_API_KEY', 'SHULAN_LLM_DEFAULT_MODEL')
DIMENSIONS = ('goal', 'medical_history', 'family_history', 'diet_structure', 'activity', 'sleep')
ANSWERS = {
    'priority': '腹胀和排便偏干都想了解，没有哪个更重要，不需要分先后。',
    'character': '腹胀像肚子鼓起来，大便偏干但没有明显腹痛。',
    'onset': '腹胀持续了两个月，排便偏干大约一个月。',
    'frequency': '腹胀每周三四次，通常在晚饭后；排便大约每两天一次。',
    'impact': '有时需要松一下腰带，没有影响工作或睡眠。',
    'associated_symptoms': '没有发热、呕吐、便血或明显体重下降。',
    'habits': '最近经常吃得快、蔬菜少，喝水大约一升，饭后很少活动。',
    'medical_history': '没有确诊的消化系统疾病，也没有做过腹部手术。',
    'medication': '近期没有服用药物、益生菌或膳食补充剂。',
    'family_history': '不清楚家族里的消化系统病史，暂时无法确认。',
    'goal': '想了解这些不适与饮食生活习惯的关系，不要求确定诊断。',
    'other_symptoms': '目前没有其他想补充的不适。',
    'diet_structure': '平时蔬菜、主食和肉类都有，午饭多在外面吃。',
    'meal_pattern': '工作日三餐时间大致固定，周末午饭有时较晚。',
    'hydration': '主要喝白水，上午会喝一杯不加糖的咖啡。',
    'activity': '上下班步行，周末会散步，暂时没有固定的运动安排。',
    'sleep': '大多能睡七小时，最近没有明显变化。',
    'weight_change': '近半年体重大致稳定，希望养成便于坚持的管理习惯。',
}
OPENING_ANSWER = {
    'multiple_complaints': '晚饭后腹胀和排便偏干都困扰我，两项都想了解，没有哪个更重要。',
    'extended_intake': '最近晚饭后肚子发胀，想把这件事的情况说清楚。',
    'wellness_goals': '目前没有不适，主要关注体重管理，想先记录平时的饮食和活动。',
    'non_digestive_report': '这次主要记录看屏幕后的眼睛疲劳，已有的近视和配镜情况报告里写了。',
    'background_decline': '目前没有其他不适，这次只想记录整体健康情况。',
    'report_focus_rotation': '我主要想改善入睡困难，也想了解报告里的血糖和血脂情况。',
    'single_goal_dialogue': '最近入睡困难，想改善入睡。',
    'no_goal_dialogue': '目前没有不适，也没有想改善的具体问题。',
    'free_text_expansion': (
        '最近入睡困难。我没有确诊疾病，父亲有糖尿病；我每周跑步三次，每次三十分钟，主要想先解决入睡的问题。'
    ),
}
FINAL_ANSWER = '饭后仍有腹胀。想知道饭后散步是否合适，也担心这是不是肠道肿瘤。'
SEEDED_CONCERN_ID = 'synthetic-pending-probiotic'
SEEDED_CONCERN_TEXT = '每天吃益生菌可以改善腹胀吗？'
REVIEW_RUBRIC = [
    '多困扰均被保留，未凭空替用户选主诉；优先级至多确认一次，拒绝排序后继续了解共同背景。',
    '已知、明确否认、说不清及不愿回答的信息不换一种措辞重复追问。',
    '承接通常一至两句，围绕用户的困扰主动交流，让人感到受到关注；不介绍采集流程或泛泛讲主题重要性。',
    '较长采集仍在补充相关信息，不能为达到轮数机械提问；自然充分时可停止。',
    '终轮分别回应所有实际疑问，证据不足的疾病担忧保留不确定性，不重新提问。',
    '未将合成资料、内部阶段、计数、调度信息或模型 JSON 当作患者事实写入正文。',
    '先承接本人关注及报告，无症状也能建档；非消化报告不默认引出排便或虚构肠道关联。',
    '饮食、活动等目的分别记录，已有报告答案不重复询问；独立背景先邀请，拒绝后停止转向。',
    '正常、修复、兜底的问题都有匹配快捷选项及自由回答入口；不以每次措辞不同作为质量目标。',
    '个人疾病史主要承接主诉或目标；家族史自然转入完整血亲背景，不被某个报告指标限定。',
    '生活习惯关联真实主诉或报告，关联相当时关注点交替；不为轮换改变问题顺序或制造疾病线索。',
    '单一主诉从不同生活角度展开；没有具体主诉时自然交流，不预设患病，不增加症状澄清。',
    '不反复复述用户行为、整段病史、已解释结论，也不机械重复为了更好地帮助您。',
    '连续问题可以借助上下文自然延续，不要求每轮重提主诉、帮助目的或关注引用；不强制套用关注点加话题切换。',
    '逐段人工审阅完整对话；连续三轮或更多沿用同一表达骨架、只替换名词时，表达验收未通过，换同义词不算改进。',
    '可以表达帮助分析困扰、了解改善方向的意图，但不承诺疗效、确定病因或虚构个性化依据。',
    '直接主动了解用户，不用我们一起看看、和您一起了解等空泛陪伴话术；把两种情况放在一起考虑等实际合并语义可以保留。',
    '减少没有递进含义的也，不反复也想、也一起、也看看，不机械替换成一并、共同，也不让每句都以我想开头。',
    '上述措辞逐段人工审阅，不按一起或也做关键词判定；题干固定自由回答提示、QA正文、用户原话和已发布历史不清理词语。',
    '自然延续不等于我知道了、心里有数了等上题回执，不反复安慰记不清，不以主题很重要或泛关联说明代替交流。',
    '自由回答中提前提供的病史、家族史、运动频次与时长得到保留，后续不换措辞重复采集。',
    '普通提问不夹带科普、建议、诊断或方法解释；正式QA仍直接逐项解释，不强调信息不足。',
    '公开AI正文不出现分析失败、未完成可靠分析、原文保留或待处理提示；正常、修复、兜底及历史恢复均检查。',
    '仅投影AI正文，用户原话、reasoning、存储快照、错误响应及独立附件状态保持原义；不能把无提示理解为分析完整成功。',
]

# Independent acceptance examples deliberately do not call the production
# sanitizer: a broken or missing public projection must fail this check.
_INTERNAL_NOTICE = re.compile(
    r'(?:尚未|未能|暂未|无法|未完成)[^。！？!?；;\n]{0,25}(?:可靠分析|可靠回应|可靠答复|完成分析|完成处理)'
    r'|(?:分析|解析|生成|模型|系统|校验|修复|处理)[^。！？!?；;\n]{0,12}(?:失败|超时|未完成)'
    r'|(?:原文|原始回答|原始内容)[^。！？!?；;\n]{0,12}(?:保留|保存)'
    r'|(?:保留|保存)[^。！？!?；;\n]{0,15}(?:原文|原始回答|原始内容|待回应|待回答|待处理)'
    r'|(?:列为|标记|记录|转为)[^。！？!?；;\n]{0,10}待(?:回应|回答|处理)'
)


def internal_notice_locations(trace):
    """Inspect only outward AI fields, never raw state, answers or reasoning."""
    locations = []

    def inspect(result, prefix):
        if not isinstance(result, dict) or result.get('status') not in ('question', 'finished'):
            return
        rows = [(prefix, result)]
        rows.extend((f'{prefix}.history[{index}]', item) for index, item in enumerate(result.get('history') or []))
        for path, row in rows:
            for field in ('assistantMessage', 'question'):
                value = row.get(field)
                if isinstance(value, str) and _INTERNAL_NOTICE.search(re.sub(r'[\s*_`]+', '', value)):
                    locations.append(f'{path}.{field}')

    for index, turn in enumerate(trace.get('turns') or []):
        inspect(turn.get('result'), f'turns[{index}].result')
    for name, result in (trace.get('publicReadback') or {}).items():
        inspect(result, f'publicReadback.{name}')
    return locations


def settings_from_file(path):
    """Read only the three LLM keys, without importing deployment configuration."""
    from dotenv import dotenv_values

    selected = []
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        if match and match[1] in LLM_KEYS:
            selected.append(line)
    values = dotenv_values(stream=io.StringIO('\n'.join(selected)), interpolate=False)
    if not all(values.get(key) for key in LLM_KEYS):
        raise ValueError('The three explicit LLM settings are required')
    return {key: values[key] for key in LLM_KEYS}


def isolated_environment():
    # Reuse only the pure inert values, never the upload harness dependencies.
    from tests.unit._support.harnesses.qa_document_acceptance import inert_environment

    return inert_environment()


class MemorySessions:
    def __init__(self):
        self.rows = {}

    async def get(self, user_id, session_id):
        return deepcopy(self.rows.get((user_id, session_id)))

    async def save(self, user_id, session_id, session, *, ttl_seconds=None):
        self.rows[user_id, session_id] = deepcopy(session)
        return True

    async def delete(self, user_id, session_id):
        self.rows.pop((user_id, session_id), None)
        return True


class MemoryLeases:
    def __init__(self):
        self.owners = {}

    async def try_acquire(self, thread_id, owner_id):
        if thread_id in self.owners:
            return None
        self.owners[thread_id] = owner_id
        return SimpleNamespace(key=thread_id, owner_id=owner_id)

    async def renew(self, lease):
        return self.owners.get(lease.key) == lease.owner_id

    async def release(self, lease):
        if self.owners.get(lease.key) == lease.owner_id:
            self.owners.pop(lease.key)
        return True


def request_payload(request):
    content = request.content
    return json.JSONDecoder().raw_decode(content[content.index('{') :])[0]


class CallBudgetExceeded(RuntimeError):
    pass


class ObservedGateway:
    """Count every logical call, including application repair attempts."""

    def __init__(self, delegate, maximum):
        self.delegate = delegate
        self.maximum = maximum
        self.calls = []
        self.scenario = ''

    async def complete(self, request):
        if len(self.calls) >= self.maximum:
            raise CallBudgetExceeded('acceptance model-call budget exhausted')
        row = {
            'index': len(self.calls) + 1,
            'scenario': self.scenario,
            'profile': request.profile.name,
            'request': request_payload(request),
        }
        self.calls.append(row)
        started = time.perf_counter()
        try:
            result = await self.delegate.complete(request)
            row.update(
                content=result.content,
                reasoning=result.reasoning_content,
                transportRetries=result.transport_retries,
                usage=result.usage.to_dict() if result.usage else None,
            )
            return result
        except BaseException as error:
            # Exception messages can include provider credentials/URLs. Keep type only.
            row['errorType'] = type(error).__name__
            raise
        finally:
            row['elapsedMs'] = round((time.perf_counter() - started) * 1000, 2)

    async def stream(self, request):
        # This acceptance exercises the ordinary submission path. API tests cover SSE.
        raise AssertionError('unexpected streaming call in dialogue acceptance')
        yield  # pragma: no cover


class SmokeGateway:
    """Deterministic model double; production graph and state machine remain real."""

    scenario = ''

    def assessment(self, patient):
        if self.scenario in DIALOGUE_SCENARIOS:
            return self.dialogue_assessment(patient)
        if self.scenario in FOCUS_SCENARIOS:
            return self.focus_assessment(patient)
        answer = patient['本次回答']
        turn = len(patient['历史问答'])
        texts = ['晚饭后腹胀', '排便偏干'] if self.scenario == 'multiple_complaints' else ['晚饭后腹胀']
        known = patient.get('已识别的困扰', [])
        complaints = []
        if answer is not None:
            for text in texts:
                old = next((item for item in known if item['困扰'] == text), {})
                item = {
                    'id': old.get('编号'),
                    'text': text,
                    'evidence': [{'source': '本次回答', 'quote': str(answer)}],
                }
                complaints.append(item)
        understanding = {
            'complaints': complaints,
            'priorityClarificationNeeded': False,
            'facts': [],
            # One identified complaint is not an explicit preference expressed by the user.
            'preferredIndex': None,
        }
        limit = 15
        gaps = []
        if answer is not None and turn + 1 < min(limit, len(DIMENSIONS)):
            dimension = DIMENSIONS[turn + 1]
            general = True
            gap = {
                'topic': '消化与排便',
                'dimension': dimension,
                'scope': 'general' if general else 'complaint',
                'complaintIndexes': [] if general else list(range(len(complaints))),
                'informationNeeded': '了解' + dimension,
                'reason': '这些信息有助于了解用户已经提出的不适。',
                'essential': True,
            }
            gaps.append(gap)
        if answer is not None and turn < len(DIMENSIONS):
            from modules.record.application.intake import DIMENSION_LABELS

            previous = next(key for key, value in DIMENSION_LABELS.items() if value == patient['已问过的内容'][-1])
            general = True
            fact = {
                'dimension': previous,
                'scope': 'general' if general else 'complaint',
                'complaintIndexes': [] if general else list(range(len(complaints))),
                'status': 'known',
                'summary': str(answer),
                'evidence': [{'source': '本次回答', 'quote': str(answer)}],
            }
            understanding['facts'].append(fact)
        concerns = []
        if self.scenario == 'final_turn':
            gaps = []
            pending_ids = set(patient['尚未回应的疑问编号'])
            for previous in patient['已有疑问']:
                if previous['编号'] in pending_ids:
                    concerns.append(
                        {
                            'id': previous['编号'],
                            'text': previous['问题'],
                            'evidence': previous['用户原话'],
                            'readyToAnswer': False,
                            'informationNeeded': '缺少具体益生菌产品和使用情况',
                        }
                    )
            for text, ready in [('饭后散步是否合适', True), ('是否为肠道肿瘤', False)]:
                concerns.append(
                    {
                        'id': None,
                        'text': text,
                        'evidence': FINAL_ANSWER,
                        'readyToAnswer': ready,
                        'informationNeeded': '' if ready else '缺少医生查体和必要检查',
                    }
                )
        return {
            'intent': 'answer',
            'substantive': answer is not None,
            'intakeSufficient': turn >= limit - 1,
            'summary': '目前仅能结合用户陈述了解消化相关不适，不能据此确定疾病。',
            'intake': understanding,
            'concerns': concerns,
            'gaps': gaps,
        }

    def focus_assessment(self, patient):
        """Synthetic replies exercise flow, never stand in for live clinical quality."""
        answer, history = patient['本次回答'], patient['历史问答']
        turn = len(history)
        report_case = self.scenario == 'non_digestive_report'
        sequence = ('goal', 'medical_history', 'family_history', 'diet_structure', 'activity')
        complaints, facts, gaps = [], [], []
        if report_case and not history:
            facts.append(
                {
                    'dimension': 'medical_history',
                    'scope': 'general',
                    'status': 'known',
                    'summary': EYE_REPORT['aiSummary'],
                    'evidence': [{'source': '本次报告', 'quote': EYE_REPORT['aiSummary']}],
                }
            )
        if answer is not None:
            if report_case:
                known = patient['已识别的困扰']
                complaints = [
                    {
                        'id': known[0]['编号'] if known else None,
                        'text': '看屏幕后的眼睛疲劳',
                        'evidence': [
                            {'source': '历史回答' if history else '本次回答', 'quote': OPENING_ANSWER[self.scenario]}
                        ],
                    }
                ]
            from modules.record.application.intake import DIMENSION_LABELS

            previous = next(key for key, value in DIMENSION_LABELS.items() if value == patient['已问过的内容'][-1])
            facts.append(
                {
                    'dimension': previous,
                    'scope': 'general',
                    'complaintIndexes': [],
                    'status': 'declined' if self.scenario == 'background_decline' and turn >= 3 else 'known',
                    'summary': str(answer),
                    'evidence': [{'source': '本次回答', 'quote': str(answer)}],
                }
            )
            if turn + 1 < len(sequence):
                optional = False
                gaps = [
                    {
                        'topic': '日常健康背景' if not report_case else '眼睛疲劳',
                        'dimension': sequence[turn + 1],
                        'scope': 'general',
                        'complaintIndexes': [],
                        'informationNeeded': '尚未记录的相关情况',
                        'reason': '补充独立的健康背景' if optional else '了解用户本次关注的情况',
                        'optionalBackground': optional,
                    }
                ]
        return {
            'intent': 'answer',
            'substantive': answer is not None,
            'intakeSufficient': answer is not None and turn >= len(sequence) - 1,
            'summary': '先按本次目标记录已有情况，再补充尚缺的信息。',
            'intake': {'complaints': complaints, 'facts': facts},
            'concerns': [],
            'gaps': gaps,
        }

    def dialogue_assessment(self, patient):
        """Known synthetic facts, without predicting a live model's interpretation."""
        from modules.record.application.intake import DIMENSION_LABELS

        answer, history = patient['本次回答'], patient['历史问答']
        facts, complaints = [], []
        if answer is not None:
            source = '历史回答' if history else '本次回答'
            original = OPENING_ANSWER[self.scenario]
            if self.scenario != 'no_goal_dialogue':
                previous = patient['已识别的困扰']
                complaints = [
                    {
                        'id': previous[0]['编号'] if previous else None,
                        'text': '入睡困难',
                        'evidence': [{'source': source, 'quote': original}],
                    }
                ]
            dimension = next(key for key, value in DIMENSION_LABELS.items() if value == patient['已问过的内容'][-1])
            facts.append(
                {
                    'dimension': dimension,
                    'scope': 'general',
                    'status': 'known',
                    'summary': str(answer),
                    'evidence': [{'source': '本次回答', 'quote': str(answer)}],
                }
            )
            if self.scenario == 'free_text_expansion' and not history:
                for covered, statement in (
                    ('medical_history', '我没有确诊疾病'),
                    ('family_history', '父亲有糖尿病'),
                    ('exercise_frequency', '我每周跑步三次'),
                    ('exercise_duration', '每次三十分钟'),
                ):
                    facts.append(
                        {
                            'dimension': covered,
                            'scope': 'general',
                            'status': 'known',
                            'summary': statement,
                            'evidence': [{'source': '本次回答', 'quote': statement}],
                        }
                    )
        return {
            'intent': 'answer',
            'substantive': answer is not None,
            'intakeSufficient': len(history) >= 14,
            'summary': '按真实陈述保留主诉、疾病背景与生活行为。',
            'intake': {'complaints': complaints, 'facts': facts},
            'concerns': [],
            'gaps': [],
        }

    def presentation(self, payload):
        from modules.record.application.fallback import _gap_question
        from modules.record.application.interview import DEFAULT_GOAL_OPTIONS
        from modules.record.application.interview import GOAL_STATUS_OPTIONS
        from modules.record.application.presentation import supplement_question

        patient, task = payload['patient'], payload['responseTask']
        action = task['action']
        acknowledgement = '接着聊聊您平时的生活。'
        rationale = ''
        if action == 'open_intake':
            acknowledgement = '您好，我们从您这次最想解决的问题聊起。'
            question = {
                'question': '这次您最希望进一步了解哪方面？',
                'questionType': 'checkbox',
                'options': list(DEFAULT_GOAL_OPTIONS) + list(GOAL_STATUS_OPTIONS),
            }
        elif action == 'invite_background':
            rationale = ''
            question = None
        elif action in {'invite_supplement', 'clarify_supplement'}:
            acknowledgement = '接下来聊聊您还关心的问题。'
            question = supplement_question(clarify=action == 'clarify_supplement')
        elif action == 'finish':
            rationale = ''
            acknowledgement = '日常安排可以从容易坚持的小调整开始。'
            question = None
        else:
            target = task.get('target') or {}
            question = _gap_question(target)
            if target.get('dimension') == 'family_history':
                acknowledgement = '接着聊聊家里人的健康情况。'
            elif target.get('dimension') == 'medical_history':
                acknowledgement = '我先了解一下您以往的疾病情况。'
        evidence = [] if patient['本次回答'] is None else [{'source': '本次回答', 'quote': str(patient['本次回答'])}]
        response_texts = {}
        for item in task['responses']:
            if item['concernId'] == SEEDED_CONCERN_ID:
                response_texts[item['concernId']] = (
                    '益生菌的作用因菌株和用途而异。日常可以先关注饮食和活动安排，不宜把产品当作所有腹胀的通用处理。'
                )
            elif '散步' in item['text']:
                response_texts[item['concernId']] = '可结合耐受情况尝试轻松散步；如活动后不适加重，应暂停并咨询医生。'
            else:
                response_texts[item['concernId']] = (
                    '腹胀可能与进食速度、食物搭配有关，并不等于肠道肿瘤。持续加重时应就医评估。'
                )
        return {
            'assistantMessage': {'acknowledgement': acknowledgement, 'rationale': rationale, 'evidence': evidence},
            'responses': [
                {
                    'concernId': item['concernId'],
                    'text': response_texts[item['concernId']],
                }
                for item in task['responses']
            ],
            'question': question,
        }

    async def complete(self, request):
        from agents.llm import LLMResult

        payload = request_payload(request)
        output = self.assessment(payload) if request.profile.name == 'record.plan' else self.presentation(payload)
        return LLMResult(content=json.dumps(output, ensure_ascii=False), reasoning_content='结合患者陈述了解相关情况。')

    async def stream(self, request):
        raise AssertionError('smoke uses ordinary submissions')
        yield  # pragma: no cover


def build_manager(gateway):
    from langgraph.checkpoint.memory import InMemorySaver

    from adapters.record import AionPromptRenderer
    from agents import AgentHub
    from agents import AgentRuntime
    from agents import GraphRegistry
    from agents.graphs.record import register_record_graphs
    from agents.llm import ModelProfileRegistry
    from modules.record.application.service import QASessionManager

    registry = GraphRegistry()
    runtime = AgentRuntime(
        hub=AgentHub(registry),
        registry=registry,
        profiles=ModelProfileRegistry(),
        llm_gateway=gateway,
        context_repository=object(),
        lease_manager=MemoryLeases(),
        checkpointer=InMemorySaver(),
        _close_callback=lambda: None,
    )
    register_record_graphs(
        runtime,
        question_topics=json.loads((ROOT / 'config/question_topics.json').read_text(encoding='utf-8')),
    )
    cache, repository = MemorySessions(), MemorySessions()
    manager = QASessionManager(
        runtime,
        session_repository=repository,
        session_cache=cache,
        prompt_renderer=AionPromptRenderer(),
    )
    return manager, cache, repository


def final_seed(user_id, session_id):
    from modules.record.application.flow import new_flow

    flow = new_flow()
    flow.update(
        phase='qa', interactionCount=9, intakeCount=5, intakeQuestionCount=5, qaRoundCount=4, preQaAnswered=True
    )
    flow['concerns'] = [
        {
            'id': SEEDED_CONCERN_ID,
            'text': SEEDED_CONCERN_TEXT,
            'evidence': SEEDED_CONCERN_TEXT,
            'sourceQuestionId': 'seed-8',
            'originPhase': 'qa',
            'handledCount': 0,
            'status': 'pending',
            'response': '',
            'readyToAnswer': False,
            'informationNeeded': '缺少具体益生菌产品和使用情况',
        }
    ]
    history = [
        {
            'question': {
                'questionId': f'seed-{i}',
                'question': '请补充相关的实际情况。',
                'questionType': 'text',
                'options': {'content': ''},
                'phase': 'intake' if i < 5 else 'qa',
            },
            'answer': list(ANSWERS.values())[i % len(ANSWERS)],
            'interactionCount': i + 1,
            'answeredAt': '2026-10-01T08:00:00+00:00',
        }
        for i in range(9)
    ]
    history[-1]['answer'] += ' ' + SEEDED_CONCERN_TEXT
    return {
        'userId': user_id,
        'sessionId': session_id,
        'type': 'qa',
        'status': 'active',
        'createdAt': '2026-10-01T07:00:00+00:00',
        'qaFlow': flow,
        'qaHistory': history,
        'round': 1,
        'healthData': {},
        'extractedInfo': {},
        'parsedHealthReports': [],
        'hasHealthReport': False,
        'healthProfileContext': None,
        'documents': [],
        'documentProcessing': [],
        'currentQuestion': {
            'questionId': 'seed-final',
            'question': '您提到想了解益生菌，具体是什么产品、目前有没有使用？也可以补充其他情况。',
            'questionType': 'text',
            'options': {'content': '实际情况或疑问'},
            'phase': 'qa',
            'concernId': SEEDED_CONCERN_ID,
            'assistantMessage': '您想了解益生菌是否能改善腹胀，目前还缺少产品和使用情况。',
            'reasoning': '',
        },
    }


def check(name, condition, **details):
    return {'name': name, 'passed': bool(condition), **details}


def evaluate_trace(trace):
    """Check actual state and published turns, not only parseable model replies."""
    turns, state = trace['turns'], trace.get('finalState') or {}
    decisions = trace.get('decisions', [])
    intake = (state.get('qaFlow') or {}).get('intake') or {}
    priorities = [d for d in decisions if (d.get('target') or {}).get('dimension') == 'priority']
    purposes = [
        (
            d['target'].get('dimension'),
            d['target'].get('scope'),
            tuple(d['target'].get('complaintIds') or d['target'].get('complaintIndexes') or []),
        )
        for d in decisions
        if d['action'] == 'ask_intake' and d.get('target')
    ]
    # Expression repair repeats the same response task; only published question
    # IDs count as user-facing questions. Keep all raw decisions in the trace
    # for repair review, but do not mistake an unpublished retry for repetition.
    published = {}
    for question in [
        *(item.get('question') for item in state.get('qaHistory') or []),
        state.get('currentQuestion'),
    ]:
        if isinstance(question, dict) and question.get('questionId') and question.get('intakeDimension'):
            published[question['questionId']] = question
    if published:
        priorities = [question for question in published.values() if question['intakeDimension'] == 'priority']
        purposes = [
            (question['intakeDimension'],)
            for question in published.values()
            if question.get('phase') == 'intake' and question['intakeDimension'] != 'goal'
        ]
    checks = [
        check('no_generation_error', 'errorType' not in trace),
        check('published_messages_nonempty', bool(turns) and all(t['result'].get('assistantMessage') for t in turns)),
        check(
            'internal_processing_notices_absent',
            not internal_notice_locations(trace),
            locations=internal_notice_locations(trace),
        ),
        check('priority_not_repeated', len(priorities) <= 1, actual=len(priorities)),
        check('question_purposes_not_repeated', len(purposes) == len(set(purposes)), purposes=purposes),
        check('submission_replay_is_idempotent', trace.get('replayUnchanged') is True),
        check('replay_makes_no_model_calls', trace.get('replayExtraCalls') == 0),
        check('no_creation_recovery_work', trace.get('recoveryCount') == 0),
        check(
            'every_new_question_has_choices_and_free_input',
            all(
                item['result'].get('questionType') in ('radio', 'checkbox')
                and isinstance(item['result'].get('options'), list)
                and len(item['result']['options']) >= (1 if item['result'].get('phase') == 'supplement' else 2)
                and '文字或语音' in item['result'].get('question', '')
                for item in turns
                if item['result'].get('status') == 'question'
            ),
        ),
    ]
    if 'readbackPreservesStoredState' in trace:
        checks.append(check('public_readback_preserves_storage', trace['readbackPreservesStoredState']))
    if trace['scenario'] in FOCUS_SCENARIOS:
        first = turns[0]['result'] if turns else {}
        checks.append(
            check(
                'opening_does_not_default_to_stool', '排便' not in first.get('question', '') + str(first.get('options'))
            )
        )
        if trace['scenario'] == 'background_decline':
            invitation_indexes = [
                index for index, item in enumerate(decisions) if item['action'] == 'invite_background'
            ]
            checks.append(
                check(
                    'no_independent_pivot_after_refusal',
                    not invitation_indexes
                    or (
                        intake.get('backgroundConsent') in ('declined', 'deferred')
                        and all(
                            not (item.get('target') or {}).get('optionalBackground')
                            for item in decisions[invitation_indexes[0] + 1 :]
                        )
                    ),
                )
            )
    if trace['scenario'] in DIALOGUE_SCENARIOS:
        checks.append(
            check(
                'dialogue_metadata_not_public',
                all(
                    not {'focusRef', 'dialogueFocusRef', '对话衔接参考'}.intersection(turn['result']) for turn in turns
                ),
            )
        )
        if trace['scenario'] == 'free_text_expansion':
            dimensions = {(item.get('target') or {}).get('dimension') for item in decisions}
            checks.append(
                check(
                    'expanded_activity_facts_not_reasked', not dimensions & {'exercise_frequency', 'exercise_duration'}
                )
            )
        if trace['scenario'] == 'no_goal_dialogue':
            checks.append(check('no_goal_does_not_invent_complaints', not intake.get('complaints')))
    if trace['scenario'] == 'multiple_complaints':
        complaints = intake.get('complaints') or []
        texts = ' '.join(item['text'] for item in complaints)
        checks.extend(
            [
                check('both_complaints_retained', len(complaints) >= 2 and '胀' in texts and ('便' in texts)),
                check('no_invented_preference', intake.get('preferredId', intake.get('primaryId')) is None),
                check(
                    'focus_references_retained_complaints',
                    set(intake.get('focusIds') or []).issubset({item['id'] for item in complaints}),
                ),
            ]
        )
    elif trace['scenario'] == 'final_turn':
        result = turns[-1]['result'] if turns else {}
        concerns = (state.get('qaFlow') or {}).get('concerns') or []
        by_id = {item['id']: item for item in concerns}
        seeded_ids = set(trace.get('seedPendingConcernIds') or [])
        new_concerns = [item for item in concerns if item['id'] not in seeded_ids]
        responded_ids = {item['concernId'] for decision in decisions for item in decision['responses']}
        history = state.get('qaHistory') or []
        checks.extend(
            [
                check(
                    'fifth_qa_reply_finishes',
                    result.get('status') == 'finished'
                    and result.get('qaRoundCount') == 5
                    and result.get('endReason') == 'qa_round_limit',
                ),
                check('no_question_after_final_turn', not state.get('currentQuestion') and not result.get('question')),
                check(
                    'old_and_new_final_concerns_resolved',
                    bool(seeded_ids)
                    and seeded_ids.issubset(by_id)
                    and len(new_concerns) >= 2
                    and all(c.get('status') in {'answered', 'limited'} and c.get('response') for c in concerns),
                ),
                check(
                    'all_final_answers_published',
                    bool(concerns)
                    and responded_ids == set(by_id)
                    and all(
                        item.get('response') and item['response'] in result.get('assistantMessage', '')
                        for item in concerns
                    ),
                ),
                check(
                    'completed_snapshot_preserves_final_exchange',
                    state.get('status') == 'completed'
                    and len(history) == 10
                    and history[-1]['question']['questionId'] == 'seed-final'
                    and history[-1]['answer'] == FINAL_ANSWER
                    and history[-1]['interactionCount'] == 10,
                ),
            ]
        )
    return checks


def trace_coverage(trace):
    """Coverage is observed behavior, not a requirement to ask a fixed number of questions."""
    flow = (trace.get('finalState') or {}).get('qaFlow') or {}
    count = flow.get('intakeQuestionCount', flow.get('intakeCount', 0))
    applicable = trace['scenario'] != 'final_turn'
    invited = any(item['action'] == 'invite_supplement' for item in trace.get('decisions', []))
    return {
        'extension': {
            'status': 'observed' if applicable and count > 10 else 'not_exercised',
            'intakeCount': count if applicable else None,
            'reason': 'synthetic final seed excluded' if not applicable else 'count of accepted intake answers',
        },
        'earlyClosure': {
            'status': 'observed' if applicable and 0 < count <= 10 and invited else 'not_exercised',
            'intakeCount': count if applicable else None,
            'reason': 'supplement invitation within ten accepted intake answers; wording still needs review',
        },
    }


def suite_coverage(traces, requested_scenarios):
    extension = [trace['scenario'] for trace in traces if trace_coverage(trace)['extension']['status'] == 'observed']
    early = [trace['scenario'] for trace in traces if trace_coverage(trace)['earlyClosure']['status'] == 'observed']
    required = any(name in ('multiple_complaints', 'extended_intake') for name in requested_scenarios)
    return {
        'extension': {
            'status': 'observed' if extension else 'not_exercised',
            'required': required,
            'scenarios': extension,
        },
        'earlyClosure': {'status': 'observed' if early else 'not_exercised', 'scenarios': early},
        'complete': not required or bool(extension),
    }


def focused_complaint_names(state):
    flow = state.get('qaFlow') or {}
    ids = set((flow.get('focus') or {}).get('complaintIds') or [])
    return [item['text'] for item in (flow.get('intake') or {}).get('complaints', []) if item['id'] in ids]


def complaint_answer(name, dimension):
    # Names come from server-resolved stable IDs, never a guess about question prose.
    if '反酸' in name or '烧心' in name:
        facts = {
            'character': '反酸时感觉酸水上涌，有一点烧心。',
            'onset': '反酸是最近两周出现的。',
            'frequency': '反酸每周一两次，多在晚餐后。',
            'impact': '反酸偶尔让我暂停进食，没有影响睡眠。',
            'associated_symptoms': '反酸时没有吞咽困难、呕血或明显体重下降。',
        }
    elif '胀' in name:
        facts = {
            'character': '腹胀像肚子鼓起来，没有明显腹痛。',
            'onset': '腹胀持续了两个月。',
            'frequency': '腹胀每周三四次，通常在晚饭后。',
            'impact': '腹胀时有时需要松一下腰带，没有影响工作或睡眠。',
            'associated_symptoms': '腹胀时没有发热、呕吐、便血或明显体重下降。',
        }
    elif '便' in name:
        facts = {
            'character': '大便偏干，有时需要用力。',
            'onset': '排便偏干大约一个月。',
            'frequency': '排便大约每两天一次。',
            'impact': '排便有时费力，但没有影响工作。',
            'associated_symptoms': '排便时没有便血或明显腹痛。',
        }
    else:
        facts = {}
    return facts.get(dimension, '关于' + name + '的这项情况，我暂时说不清。')


def patient_answer(scenario, *, step, phase, dimension, target_names=(), background_invited=False):
    if scenario == 'final_turn':
        return FINAL_ANSWER
    if phase == 'supplement':
        from modules.record.application.presentation import NO_MORE_OPTION

        return NO_MORE_OPTION
    if step == 0:
        return OPENING_ANSWER[scenario]
    if background_invited:
        from modules.record.application.intake import BACKGROUND_ACCEPT
        from modules.record.application.intake import BACKGROUND_DECLINE

        return BACKGROUND_DECLINE if scenario == 'background_decline' else BACKGROUND_ACCEPT
    if scenario == 'background_decline' and dimension in {'diet_structure', 'activity', 'sleep'}:
        return '我不想继续补充这方面的生活情况。'
    if scenario == 'non_digestive_report':
        return {
            'impact': '看屏幕久了需要休息一下，没有影响其他日常安排。',
            'onset': '大约两周前开始留意到这种疲劳。',
            'character': '长时间看屏幕后眼睛觉得酸累。',
            'frequency': '工作日看屏幕较久时会出现。',
            'medical_history': '报告里已经记录了近视和配镜，没有新的就诊情况。',
        }.get(dimension, '这方面暂时没有更多信息。')
    if scenario == 'wellness_goals' and dimension == 'medical_history':
        return '没有被医生确诊的疾病。'
    if scenario in DIALOGUE_SCENARIOS:
        return {
            'medical_history': '没有被医生确诊的疾病。',
            'family_history': '父亲有糖尿病，其他血亲的病史不清楚。',
            'sleep': '通常晚上十一点半准备睡觉，早上七点起床。',
            'exercise_frequency': '每周跑步三次。',
            'exercise_duration': '每次三十分钟。',
        }.get(dimension, ANSWERS.get(dimension, '目前记不清。'))
    answer = ANSWERS.get(dimension, '我暂时说不清具体情况，不能确认。')
    if dimension in {'character', 'onset', 'frequency', 'impact', 'associated_symptoms'}:
        answer = ' '.join(complaint_answer(name, dimension) for name in target_names) or (
            '我暂时说不清具体是哪一项不适的情况，不能确认。'
        )
    if scenario == 'extended_intake':
        if dimension == 'priority':
            answer = '这些不适都想了解，没有先后之分。'
        if step == 4 and not any('反酸' in name or '烧心' in name for name in target_names):
            answer += '另外最近新增了反酸的感受，反酸的频率和影响我还没有讲清楚。'
    return answer


def emit_turn_progress(scenario, result, gateway, started):
    print(
        json.dumps(
            {
                'event': 'turn_completed',
                'scenario': scenario,
                'interactionCount': result.get('interactionCount'),
                'status': result.get('status'),
                'phase': result.get('phase'),
                'modelLogicalCalls': len(gateway.calls),
                'elapsedMs': round((time.perf_counter() - started) * 1000),
            }
        ),
        flush=True,
    )


async def run_trajectory(manager, cache, gateway, scenario, *, max_user_turns=19, trace=None):
    user_id, session_id = 'synthetic-dialogue-user', 'synthetic-' + scenario
    gateway.scenario = scenario
    if isinstance(gateway.delegate, SmokeGateway):
        gateway.delegate.scenario = scenario
    if trace is None:
        trace = {}
    trace.update(scenario=scenario, synthetic=True, turns=[], decisions=[], maxUserTurns=max_user_turns)
    first_call = len(gateway.calls)
    started = time.perf_counter()
    last_submission = None
    substantive_turns = 0
    try:
        if scenario == 'final_turn':
            seed = final_seed(user_id, session_id)
            await cache.save(user_id, session_id, seed)
            current = await manager.getSession(user_id, session_id)
            trace['seedInteractionCount'] = seed['qaFlow']['interactionCount']
            trace['seedPendingConcernIds'] = [item['id'] for item in seed['qaFlow']['concerns']]
        else:
            tick = time.perf_counter()
            if scenario in {'non_digestive_report', 'report_focus_rotation'}:
                reports = [EYE_REPORT] if scenario == 'non_digestive_report' else METABOLIC_REPORTS

                async def synthetic_report(**kwargs):
                    kwargs['document_processing'].extend(
                        {'fileIndex': index, 'status': 'complete', 'retryable': False} for index in range(len(reports))
                    )
                    return deepcopy(reports)

                # Supply normalized synthetic report data; never fetch or parse a file.
                with patch.object(manager, '_processHealthReports', side_effect=synthetic_report):
                    current = await manager.createSession(
                        user_id,
                        session_id,
                        basic_info={'age': 38},
                        file_list=[
                            {'url': f'https://synthetic.invalid/report-{index}.pdf'} for index in range(len(reports))
                        ],
                    )
                if scenario == 'non_digestive_report':
                    trace['syntheticReport'] = deepcopy(EYE_REPORT)
                else:
                    trace['syntheticReports'] = deepcopy(reports)
            else:
                current = await manager.createSession(user_id, session_id, basic_info={'age': 38})
            trace['turns'].append(
                {
                    'answer': None,
                    'result': current,
                    'elapsedMs': round((time.perf_counter() - tick) * 1000),
                }
            )
            emit_turn_progress(scenario, current, gateway, started)
        for step in range(max_user_turns):
            if current['status'] != 'question':
                break
            state = await manager._load_session(user_id, session_id)
            dimension = (state['qaFlow'].get('focus') or {}).get('dimension')
            answer = patient_answer(
                scenario,
                step=step,
                phase=current['phase'],
                dimension=dimension,
                target_names=focused_complaint_names(state),
                background_invited=state['qaFlow']['intake'].get('backgroundConsent') == 'invited',
            )
            if current['phase'] != 'supplement':
                substantive_turns += 1
            last_submission = (current['questionId'], answer)
            tick = time.perf_counter()
            current = await manager.submitAnswer(user_id, session_id, *last_submission)
            trace['turns'].append(
                {
                    'answer': answer,
                    'result': current,
                    'elapsedMs': round((time.perf_counter() - tick) * 1000),
                }
            )
            emit_turn_progress(scenario, current, gateway, started)
            if scenario == 'final_turn':
                break
        if current['status'] == 'question' and current['phase'] == 'supplement':
            from modules.record.application.presentation import NO_MORE_OPTION

            state = await manager._load_session(user_id, session_id)
            if not any(item['status'] == 'pending' for item in state['qaFlow']['concerns']):
                last_submission = (current['questionId'], NO_MORE_OPTION)
                before = len(gateway.calls)
                tick = time.perf_counter()
                current = await manager.submitAnswer(user_id, session_id, *last_submission)
                trace['controlCloseExtraCalls'] = len(gateway.calls) - before
                trace['turns'].append(
                    {
                        'answer': NO_MORE_OPTION,
                        'result': current,
                        'controlOnly': True,
                        'elapsedMs': round((time.perf_counter() - tick) * 1000),
                    }
                )
                emit_turn_progress(scenario, current, gateway, started)
        if last_submission:
            before = len(gateway.calls)
            repeated = await manager.submitAnswer(user_id, session_id, *last_submission)
            trace.update(replayUnchanged=repeated == current, replayExtraCalls=len(gateway.calls) - before)
            trace.setdefault('publicReadback', {})['replay'] = repeated
        trace['recoveryCount'] = await manager.recoverPendingCreations()
        before_readback = await manager._load_session(user_id, session_id)
        trace.setdefault('publicReadback', {}).update(
            status=await manager.getSession(user_id, session_id),
            history=await manager.getHistory(user_id, session_id),
        )
        trace['readbackPreservesStoredState'] = before_readback == await manager._load_session(user_id, session_id)
    except BaseException as error:
        trace['errorType'] = type(error).__name__
        if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
    finally:
        trace['finalState'] = await manager._load_session(user_id, session_id)
        trace['calls'] = deepcopy(gateway.calls[first_call:])
        trace['decisions'] = [
            row['request']['responseTask'] for row in trace['calls'] if row['profile'] == 'record.question'
        ]
        trace['elapsedMs'] = round((time.perf_counter() - started) * 1000)
        final_result = trace['turns'][-1]['result'] if trace['turns'] else {}
        trace['finalStatus'] = final_result.get('status')
        trace['trajectoryFinished'] = trace['finalStatus'] == 'finished'
        trace['userTurnsUsed'] = substantive_turns
        trace['userTurnLimitReached'] = substantive_turns >= max_user_turns and not trace['trajectoryFinished']
        trace['checks'] = evaluate_trace(trace)
        if 'controlCloseExtraCalls' in trace:
            trace['checks'].append(check('control_closure_has_no_model_calls', trace['controlCloseExtraCalls'] == 0))
        trace['coverage'] = trace_coverage(trace)
    return trace


def build_delegate(settings, clients):
    if not settings:
        return SmokeGateway()
    raise ValueError('Use python -m tests.api.scripts.record_dialogue_acceptance --live')


def public_setting_label(value):
    """Expose model/provider identifiers, never URL userinfo, paths or query secrets."""
    if value is None:
        return None
    text = str(value).strip()
    if '://' in text:
        parsed = urlsplit(text)
        return f'{parsed.scheme}://{parsed.hostname}' if parsed.hostname else '(configured URL)'
    return text if re.fullmatch(r'[A-Za-z0-9_./:-]{1,200}', text) else '(nonstandard identifier omitted)'


def dialogue_markdown(trace):
    lines = [
        f'# Synthetic dialogue: {trace["scenario"]}',
        '',
        'Review the complete dialogue against summary.json reviewRubric; automated checks do not grade naturalness.',
        '',
        'Manual style failure: three or more consecutive introductions reuse one skeleton, changing only nouns.',
        '',
    ]
    if trace.get('seedInteractionCount'):
        lines.extend([f'Starts from synthetic interaction {trace["seedInteractionCount"]}.', ''])
    for index, turn in enumerate(trace['turns']):
        result = turn['result']
        lines.extend([f'## Exchange {index}', ''])
        if turn['answer'] is not None:
            lines.extend(['User: ' + str(turn['answer']), ''])
        lines.extend(['Assistant: ' + result.get('assistantMessage', ''), ''])
        if result.get('question'):
            lines.extend(
                [
                    'Question: ' + result['question'],
                    '',
                    'Options: ' + json.dumps(result.get('options'), ensure_ascii=False),
                    '',
                ]
            )
        lines.extend(
            [
                f'Status: {result["status"]}; interactions: {result.get("interactionCount")}; '
                f'wall time: {turn["elapsedMs"]} ms',
                '',
            ]
        )
    return '\n'.join(lines)


def persist_trace(output_dir, trace):
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / (trace['scenario'] + '.json')).write_text(
        json.dumps(trace, ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    (output / (trace['scenario'] + '.md')).write_text(dialogue_markdown(trace), encoding='utf-8')


async def acceptance(args, settings, *, delegate_factory=None):
    clients, traces = [], []
    gateway = ObservedGateway((delegate_factory or build_delegate)(settings, clients), args.max_model_calls)
    manager, cache, _ = build_manager(gateway)
    started = time.perf_counter()
    summary = {
        'mode': 'live' if args.live else 'smoke',
        'provider': public_setting_label(settings.get('SHULAN_LLM_PROVIDER')) if args.live else None,
        'defaultModel': public_setting_label(settings.get('SHULAN_LLM_DEFAULT_MODEL')) if args.live else None,
        'syntheticData': True,
        'storage': 'memory',
        'externalServices': ['LLM'] if args.live else [],
        'productionWrites': False,
        'docmindCalls': 0,
        'profileJobs': 0,
        'callbacksSent': 0,
        'maxModelCalls': args.max_model_calls,
        'timeoutSeconds': args.timeout_seconds,
        'manualReviewRequired': bool(args.live),
        'reviewRubric': REVIEW_RUBRIC,
        'timingMeaning': 'elapsedMs is wall clock; modelElapsedMs sums individual logical call durations.',
    }
    try:
        async with asyncio.timeout(args.timeout_seconds):
            for scenario in args.scenarios:
                trace = {}
                traces.append(trace)
                print(
                    json.dumps(
                        {'event': 'scenario_started', 'scenario': scenario, 'modelLogicalCalls': len(gateway.calls)}
                    ),
                    flush=True,
                )
                await run_trajectory(manager, cache, gateway, scenario, trace=trace)
                persist_trace(args.output_dir, trace)
                print(
                    json.dumps(
                        {
                            'event': 'scenario_completed',
                            'scenario': scenario,
                            'errorType': trace.get('errorType'),
                            'finalStatus': trace['finalStatus'],
                            'trajectoryFinished': trace['trajectoryFinished'],
                            'userTurnLimitReached': trace['userTurnLimitReached'],
                            'modelLogicalCalls': len(gateway.calls),
                            'elapsedMs': trace['elapsedMs'],
                        }
                    ),
                    flush=True,
                )
                if len(gateway.calls) >= args.max_model_calls:
                    break
    except Exception as error:
        summary['errorType'] = type(error).__name__
    finally:
        for client in clients:
            try:
                await client.async_client.close()
            except Exception:
                pass
        summary.update(
            elapsedMs=round((time.perf_counter() - started) * 1000),
            modelLogicalCalls=len(gateway.calls),
            modelElapsedMs=round(sum(row['elapsedMs'] for row in gateway.calls)),
            requestedScenarios=list(args.scenarios),
            scenarios=[
                {
                    'scenario': trace['scenario'],
                    'elapsedMs': trace['elapsedMs'],
                    'checks': trace['checks'],
                    'coverage': trace['coverage'],
                    'finalStatus': trace['finalStatus'],
                    'trajectoryFinished': trace['trajectoryFinished'],
                    'userTurnLimitReached': trace['userTurnLimitReached'],
                    'userTurnsUsed': trace['userTurnsUsed'],
                    'errorType': trace.get('errorType'),
                }
                for trace in traces
            ],
        )
        summary['coverage'] = suite_coverage(traces, args.scenarios)
        summary['executionComplete'] = len(traces) == len(args.scenarios) and all(
            trace['trajectoryFinished'] for trace in traces
        )
        summary['automaticChecksPassed'] = (
            'errorType' not in summary
            and len(traces) == len(args.scenarios)
            and all(item['passed'] for trace in traces for item in trace['checks'])
        )
        summary['qualityVerdict'] = 'requires_prose_review' if args.live else 'offline_flow_only'
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        for trace in traces:
            persist_trace(output, trace)
        (output / 'calls.json').write_text(json.dumps(gateway.calls, ensure_ascii=False, indent=2), encoding='utf-8')
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print(
            json.dumps(
                {
                    key: summary[key]
                    for key in (
                        'mode',
                        'elapsedMs',
                        'modelLogicalCalls',
                        'automaticChecksPassed',
                        'executionComplete',
                        'qualityVerdict',
                        'coverage',
                    )
                }
            )
        )
    if not summary['automaticChecksPassed']:
        return 1
    # Missing a desired trajectory is different from a rejected/incorrect turn.
    return 0 if summary['coverage']['complete'] and summary['executionComplete'] else 2


def parse_args(argv=None, *, online=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--live', action='store_true')
    mode.add_argument('--smoke', action='store_true', help='Default: offline synthetic model replies')
    parser.add_argument('--config', help='Read only three LLM settings; required for --live')
    parser.add_argument('--output-dir', required=True, help='New external directory for synthetic traces')
    parser.add_argument(
        '--scenarios', nargs='+', choices=SCENARIOS + FOCUS_SCENARIOS + DIALOGUE_SCENARIOS, default=list(SCENARIOS)
    )
    parser.add_argument('--max-model-calls', type=int, default=140, help='Logical call limit, including model repairs')
    parser.add_argument('--timeout-seconds', type=float, default=600)
    args = parser.parse_args(argv)
    if args.live and not args.config:
        parser.error('--live requires --config')
    if args.max_model_calls < 1 or not 0 < args.timeout_seconds <= 600:
        parser.error('model-call budget must be positive; timeout must be in (0, 600] seconds')
    if Path(args.output_dir).resolve().is_relative_to(ROOT):
        parser.error('acceptance results must be saved outside the repository')
    if len(args.scenarios) != len(set(args.scenarios)):
        parser.error('scenarios must be unique')
    if online is not None and args.live != online:
        command = (
            'tests.api.scripts.record_dialogue_acceptance --live'
            if args.live
            else 'tests.unit.tools.record_dialogue_acceptance'
        )
        parser.error(f'Use python -m {command} with the same arguments')
    return args


def main(argv=None, *, online=False, delegate_factory=None):
    args = parse_args(argv, online=online)
    settings = settings_from_file(args.config) if args.live else {}
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, {**isolated_environment(), **settings}))
        stack.enter_context(patch('loongspears.utils.env.load_env', return_value=None))
        stack.enter_context(patch('database.mongodb.Client.create_async', side_effect=AssertionError('no database')))
        stack.enter_context(patch('database.redis.Client.connect', side_effect=AssertionError('no Redis')))
        return asyncio.run(acceptance(args, settings, delegate_factory=delegate_factory))
