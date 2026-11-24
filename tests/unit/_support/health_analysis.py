"""Shared Health Analysis fakes; no collected test modules or live dependencies."""

from copy import deepcopy
from unittest.mock import AsyncMock

from modules.health_analysis.application import HealthAnalysisService
from modules.health_analysis.contracts import TraceReceipt
from modules.health_analysis.policy import background_questions
from modules.health_analysis.policy import build_schema
from modules.health_analysis.trace import trace_hash


def schema():
    return {
        'title': '合成卷',
        'pages': [
            {
                'name': 'p',
                'elements': [
                    {
                        'name': 'q',
                        'type': 'radiogroup',
                        'title': '合成问题',
                        'isRequired': False,
                        'choices': [{'value': 'unknown', 'text': '不清楚'}],
                    }
                ],
            }
        ],
    }


def payload(with_answer=False):
    return {
        'userId': 'test-user',
        'groupId': 'test-group',
        'name': '合成用户',
        'age': 42,
        'gender': '男',
        'surveyList': [{'schema': schema(), 'answer': {'q': 'unknown'} if with_answer else {}}],
    }


class FakeBasicInfoPort:
    """In-memory public protocol double; never used by production assembly."""

    def __init__(self, subject_id='test-self'):
        self.subject_id = subject_id
        self.rows = {}
        self.register = AsyncMock(side_effect=self._register)
        self.apply = AsyncMock(side_effect=self._apply)
        self.get = AsyncMock(side_effect=self._get)
        self.list_pending = AsyncMock(side_effect=self._list_pending)

    async def _register(self, user_id, operation_id, *, values, confirmed_at, subject_id=None):
        key = (user_id, operation_id)
        if key not in self.rows:
            self.rows[key] = {
                'contractVersion': 'profile-basic-info-command.v1',
                'commandId': 'command:' + operation_id,
                'operationId': operation_id,
                'userId': user_id,
                'subjectId': subject_id or self.subject_id,
                'lifecycleEpoch': 1,
                'fieldEpochs': dict.fromkeys(values, 1),
                'payloadHash': 'synthetic-fixed-hash',
                'confirmedAt': confirmed_at.isoformat(),
                'status': 'registered',
                'fields': {},
                'profileVersion': None,
                'errorCode': None,
            }
        return deepcopy(self.rows[key])

    async def _apply(self, user_id, command_id):
        row = next(row for row in self.rows.values() if row['userId'] == user_id and row['commandId'] == command_id)
        row['status'] = 'applied'
        row['fields'] = {
            field: {'status': 'applied', 'factId': 'fact:' + field, 'sourceReliability': 'very_high'}
            for field in ('name', 'age', 'gender')
        }
        return deepcopy(row)

    async def _get(self, user_id, operation_id):
        return deepcopy(self.rows.get((user_id, operation_id)))

    async def _list_pending(self, *, user_id=None, limit=100, cursor=None):
        rows = [
            row
            for row in self.rows.values()
            if row['status'] in ('registered', 'pending') and (user_id is None or row['userId'] == user_id)
        ]
        return {'items': deepcopy(rows[:limit]), 'nextCursor': None}


class Writer:
    def __init__(self):
        self.records = []

    async def write(self, record):
        self.records.append(deepcopy(record))
        return TraceReceipt(record['runId'], trace_hash(record), True)


def setup(*, limits=None, has_profile=False, has_chat=False):
    confirmation = FakeBasicInfoPort()
    profile, chat = AsyncMock(), AsyncMock()
    profile.read.return_value = {
        'materials': [{'id': 'report:1', 'kind': 'report', 'content': {'value': 0}}] if has_profile else [],
        'gender': 'male',
        'status': 'available',
    }
    chat.read.return_value = {
        'materials': [{'id': 'chat:1', 'kind': 'chat', 'content': '合成本人陈述'}] if has_chat else [],
        'status': 'available',
    }
    generator = AsyncMock()

    async def generate(snapshot):
        return {
            'result': {
                'healthReview': '合成评价' if snapshot['has_personal_data'] else '',
                'suggestedMaterials': '',
                'advancedQuestions': '',
                'advancedSurveySchema': build_schema(background_questions()[:1]),
            },
            'trace': {'coverage': 'synthetic'},
            'model_identity': {'model': 'synthetic'},
        }

    generator.generate.side_effect = generate
    writer = Writer()
    service = HealthAnalysisService(profile, chat, generator, writer, basic_info_sync=confirmation, limits=limits)
    return service, confirmation, profile, chat, generator, writer


class StageModel:
    def __init__(self, *, states=None, rejected=None, plan=None, classification='self_statement', background=None):
        self.calls = []
        self.states, self.rejected = states or {}, rejected or set()
        self.plan, self.classification = plan, classification
        self.background = background or {}

    async def run(self, stage, payload):
        self.calls.append((stage, deepcopy(payload)))
        materials = payload.get('materials', [])
        if stage == 'materials':
            output = {
                'items': [
                    {
                        'id': row['id'],
                        'classification': self.classification,
                        'quote': row['content'],
                        'reason': '合成场景预设分类',
                    }
                    for row in materials
                ]
            }
        elif stage == 'targets':
            output = {'coveredMaterialIds': [m['id'] for m in materials], 'targets': []}
        elif stage == 'plan':
            output = (
                deepcopy(self.plan)
                if self.plan is not None
                else {
                    'consideredMaterialIds': [m['id'] for m in materials],
                    'healthReview': {
                        'text': '本次仅了解合成资料中的情况，具体判断仍有资料限制。',
                        'materialIds': [m['id'] for m in materials],
                        'targetIds': [],
                    },
                    'suggestedMaterials': [],
                    'advancedQuestions': [],
                    'questions': [],
                    'lifestyleOrder': [],
                }
            )
        elif stage == 'coverage':
            output = {'coverage': []}
            for target in payload['targets']:
                state = self.states.get(target['id'], 'askable')
                output['coverage'].append(
                    {
                        'targetId': target['id'],
                        'status': state,
                        'historyRefs': [payload['askedQuestions'][0]['pointer']] if state == 'history_covered' else [],
                        'materialRefs': [materials[0]['id']] if state in {'material_covered', 'inapplicable'} else [],
                        'reason': '显式合成语义裁决，非通用相似度算法',
                    }
                )
            if 'backgroundTargets' in payload:
                output['backgroundCoverage'] = [
                    {
                        'targetId': target['id'],
                        'state': 'answered' if self.background.get(target['id']) else 'unanswered',
                        'evidence': self.background.get(target['id'], []),
                    }
                    for target in payload['backgroundTargets']
                ]
        elif stage == 'review':
            output = {
                kind: [
                    {
                        'candidateId': row['candidateId'],
                        'verdict': 'invalid' if row.get('targetId') in self.rejected else 'approved',
                        'targetAligned': True,
                        'lifestyleOnly': True,
                        'novel': True,
                        'suitable': True,
                        'reason': '合成候选发布裁决',
                    }
                    for row in payload[kind]
                ]
                for kind in ('questions', 'interviews', 'tests')
            }
            if 'survey' in payload:
                survey = payload['survey']
                output['draftVersion'] = survey['draftVersion']
                output['surveyFindings'] = [
                    {'ruleId': rule['ruleId'], 'status': 'passed', 'reason': '合成卷级裁决', 'questionIds': []}
                    for rule in survey['rules']
                    if rule['scope'] == 'survey'
                ]
                for row in output['questions']:
                    row['issues'] = []
        else:
            raise AssertionError('Unexpected synthetic stage')
        return {'output': output, 'model_identity': {'provider': 'offline', 'model': 'synthetic-v1'}}
