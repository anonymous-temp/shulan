"""Offline Health Analysis browser backend using synthetic dependencies only.

Run ``python -m tests.api.scripts.health_analysis_demo`` and set API Lab's
backend to http://127.0.0.1:9081. The real Handler, application service, decoder,
policy and generator execute; confirmation storage, source readers and stage
model are explicit synthetic implementations. No .env, external LLM, OpenIM,
MongoDB or production dependencies is used. Trace JSON files are saved outside
the repository and retained for inspection. This is not a live quality test.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from modules.health_analysis.contracts import TraceDeliveryError
from modules.health_analysis.contracts import TraceReceipt
from modules.health_analysis.trace import encode_record
from modules.health_analysis.trace import trace_hash
from utils.artifact_paths import external_artifact_path

DEMO_USERS = frozenset({'test-demo-empty', 'test-demo-report', 'test-demo-chat'})
DEMO_GROUP = 'test-demo-group'
_DEMO_ENVIRONMENT = {
    'SHULAN_ENVIRONMENT': 'test',
    'SHULAN_LOG_LEVEL': 'WARNING',
    'SHULAN_LOG_FILE_ENABLED': 'false',
    'SHULAN_LLM_API_KEY': 'unused-offline-demo-key',
    'SHULAN_LLM_PROVIDER': '',
    'SHULAN_LLM_DEFAULT_MODEL': 'offline-synthetic-model',
    'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID': 'unused-offline-demo-id',
    'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET': 'unused-offline-demo-secret',
    'SHULAN_ALIYUN_DOCMIND_ENDPOINT': 'https://offline.invalid',
    'SHULAN_MONGODB_URI': 'mongodb://127.0.0.1:1/?serverSelectionTimeoutMS=10',
    'SHULAN_MONGODB_DATABASE': 'unused-offline-demo',
    'SHULAN_MONGODB_COLL_SESSIONS': 'unused-sessions',
    'SHULAN_MONGODB_COLL_HEALTH_REPORTS': 'unused-reports',
    'SHULAN_REDIS_HOST': '127.0.0.1',
    'SHULAN_REDIS_PORT': '1',
    'SHULAN_REDIS_PASSWORD': '',
    'SHULAN_KNOWLEDGE_API_KEY': '',
    'SHULAN_EVIMED_API_KEY': '',
    'SHULAN_REPORT_CALLBACK_URL': 'https://offline.invalid/callback',
    'SHULAN_REPORT_PRODUCT_URL': 'https://offline.invalid/products',
}


class DemoConfirmation:
    """Process-local synthetic confirmations; deliberately not a durable provider."""

    def __init__(self):
        self.rows = {}

    async def register(self, user_id, operation_id, *, values, confirmed_at, subject_id=None):
        row = {
            'contractVersion': 'profile-basic-info-command.v1',
            'commandId': operation_id,
            'operationId': operation_id,
            'userId': user_id,
            'subjectId': subject_id or 'demo-self:' + user_id,
            'lifecycleEpoch': 1,
            'fieldEpochs': dict.fromkeys(values, 1),
            'payloadHash': 'offline-demo',
            'confirmedAt': confirmed_at.isoformat(),
            'status': 'registered',
            'fields': {},
        }
        self.rows[(user_id, operation_id)] = row
        return deepcopy(row)

    async def apply(self, user_id, command_id):
        row = self.rows[(user_id, command_id)]
        row['status'] = 'applied'
        row['fields'] = {
            field: {'status': 'applied', 'sourceReliability': 'very_high'} for field in ('name', 'age', 'gender')
        }
        return deepcopy(row)


class DemoProfileReader:
    async def read(self, scope):
        materials = []
        if scope.user_id == 'test-demo-report':
            materials.append(
                {
                    'id': 'test-demo-report-round-1',
                    'kind': 'report',
                    'content': {
                        'reportTime': '2026-10-01',
                        'observations': [
                            {'itemName': '合成检查项目', 'resultValue': '0', 'unit': '合成单位', 'flag': '阴性'}
                        ],
                    },
                    'extractionId': 'test-demo-fixed-extraction-1',
                    'interpretationRound': 'test-demo-round-1',
                    'sourceStatus': 'active',
                }
            )
        return {'materials': materials, 'gender': None, 'status': 'available' if materials else 'empty', 'coverage': {}}


class DemoChatReader:
    async def read(self, scope):
        materials = []
        if scope.user_id == 'test-demo-chat':
            materials.append(
                {
                    'id': 'test-demo-message-1',
                    'kind': 'chat',
                    'content': '我最近通常每天睡6至7小时，工作日与休息日相差不足1小时。',
                    'serverMessageId': 'test-demo-message-1',
                    'senderId': scope.user_id + '-synthetic-sender',
                    'role': 'patient',
                    'timestamp': '2026-11-01T10:00:00+08:00',
                }
            )
        return {'materials': materials, 'status': 'available' if materials else 'empty', 'coverage': {}}


class DemoStageModel:
    """Deterministic fixtures, intentionally not a general semantic model."""

    identity = {'provider': 'offline-synthetic', 'model': 'health-analysis-demo-v1'}

    async def run(self, stage, payload, *, deadline_at=None):
        if stage == 'materials':
            output = {
                'items': [
                    {'id': row['id'], 'classification': 'self_statement', 'quote': row['content'], 'reason': '合成自述'}
                    for row in payload['materials']
                ]
            }
        elif stage == 'targets':
            output = {'coveredMaterialIds': [row['id'] for row in payload['materials']], 'targets': []}
        elif stage == 'plan':
            materials = payload.get('materials', payload.get('snapshot', {}).get('personal_materials', []))
            descriptions = []
            for row in materials:
                value = row['content']
                if row['kind'] == 'answer' and isinstance(value, dict):
                    value = value.get('question', {}).get('title', '') + '：' + str(value.get('readableAnswer', ''))
                elif not isinstance(value, str):
                    value = json.dumps(value, ensure_ascii=False)
                descriptions.append(value)
            output = {
                'consideredMaterialIds': [row['id'] for row in materials],
                'healthReview': {
                    'text': '本次已提供以下资料：'
                    + '；'.join(descriptions)
                    + '。结论限于这些资料，不据此确认疾病或因果。',
                    'materialIds': [row['id'] for row in materials],
                    'targetIds': [],
                },
                'suggestedMaterials': [],
                'advancedQuestions': [],
                'lifestyleOrder': [],
                'questions': [],
            }
        elif stage == 'coverage':
            coverage = []
            for target in payload['targets']:
                historical = [
                    row['pointer']
                    for row in payload['askedQuestions']
                    if row['question']['title'] == target['question']['title']
                    or (row['name'] == 'activity_days' and target['id'] == 'exercise_frequency')
                ]
                known = [
                    row['id']
                    for row in payload.get('materials', [])
                    if row['id'] == 'test-demo-message-1' and target['id'] in ('sleep_duration', 'sleep_consistency')
                ]
                coverage.append(
                    {
                        'targetId': target['id'],
                        'status': 'history_covered' if historical else 'material_covered' if known else 'askable',
                        'historyRefs': historical,
                        'materialRefs': known,
                        'reason': '固定合成样例的明确匹配；不代表通用语义模型效果',
                    }
                )
            output = {'coverage': coverage}
            if 'backgroundTargets' in payload:
                background = []
                for target in payload['backgroundTargets']:
                    evidence = [
                        {'evidenceId': row['id'], 'quote': row['content']['readableAnswer']}
                        for row in payload.get('materials', [])
                        if row['kind'] == 'answer' and row['content']['question'] == target['question']
                    ]
                    background.append(
                        {
                            'targetId': target['id'],
                            'state': 'answered' if evidence else 'unanswered',
                            'evidence': evidence,
                        }
                    )
                output['backgroundCoverage'] = background
        elif stage == 'review':
            output = {
                'questions': [
                    {
                        'candidateId': row['candidateId'],
                        'verdict': 'approved',
                        'targetAligned': True,
                        'lifestyleOnly': True,
                        'novel': True,
                        'suitable': True,
                        'reason': '合成目录题发布样例',
                    }
                    for row in payload['questions']
                ],
                'interviews': [],
                'tests': [],
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
            raise AssertionError('Demo does not support this model stage')
        return {'output': output, 'model_identity': deepcopy(self.identity)}


class DemoFileTraceWriter:
    """Confirm actual synthetic trace files, rather than a pretend save call."""

    def __init__(self, directory):
        self.directory = external_artifact_path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    async def write(self, record):
        return await asyncio.to_thread(self._persist, deepcopy(record))

    def _persist(self, record):
        run_id = record['runId']
        filename = str(uuid.UUID(run_id)) + '.json'
        data = encode_record(record)
        digest = trace_hash(record)
        target = self.directory / filename
        with self._lock:
            if target.exists():
                if target.read_bytes() != data:
                    raise TraceDeliveryError('Conflicting demo trace')
            else:
                temporary = self.directory / (filename + '.' + uuid.uuid4().hex + '.tmp')
                try:
                    with temporary.open('xb') as output:
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            if target.read_bytes() != data:
                raise TraceDeliveryError('Demo trace could not be verified')
        return TraceReceipt(run_id, digest, True)


def make_demo_app(trace_dir):
    # Patch before importing the Web/config boundary. No real private config is
    # loaded, even when launching this standalone command outside pytest.
    with patch.dict(os.environ, _DEMO_ENVIRONMENT), patch('loongspears.utils.env.load_env', return_value=None):
        import tornado.web

        from modules.health_analysis.application import HealthAnalysisService
        from modules.health_analysis.generation import HealthAnalysisGenerator
        from web.handlers.base import log_tornado_request
        from web.handlers.health_analysis import HealthAnalysisHandler

        class DemoHealthHandler(tornado.web.RequestHandler):
            def get(self):
                self.set_header('Content-Type', 'application/json')
                self.write({'code': 200, 'message': 'offline demo', 'data': {'mode': 'synthetic'}})

        writer = DemoFileTraceWriter(trace_dir)
        service = HealthAnalysisService(
            DemoProfileReader(),
            DemoChatReader(),
            HealthAnalysisGenerator(DemoStageModel()),
            writer,
            basic_info_sync=DemoConfirmation(),
        )
        application = tornado.web.Application(
            [
                ('/health', DemoHealthHandler),
                (
                    '/api/v1/health-analysis',
                    HealthAnalysisHandler,
                    {'service': service},
                ),
            ],
            log_function=log_tornado_request,
        )
    return application


async def smoke(trace_dir):
    """Exercise the actual HTTP/service/generator chain against local fixtures."""
    from tornado.httpclient import AsyncHTTPClient
    from tornado.httpserver import HTTPServer
    from tornado.netutil import bind_sockets

    from modules.health_analysis.policy import background_questions
    from modules.health_analysis.policy import catalogue

    application = make_demo_app(trace_dir)
    sockets = bind_sockets(0, address='127.0.0.1')
    port = sockets[0].getsockname()[1]
    server = HTTPServer(application)
    server.add_sockets(sockets)
    client = AsyncHTTPClient(force_instance=True)
    targets = catalogue()

    def historical(except_last, *, answered_background=False):
        questions = [deepcopy(row['question']) for row in targets[: len(targets) - except_last]]
        answers = {}
        if answered_background:
            background = background_questions()
            questions = background + questions
            answers = {q['name']: ['none'] for q in background}
        return [
            {
                'schema': {'title': '合成已发生活卷', 'pages': [{'name': 'daily', 'elements': questions}]},
                'answer': answers,
            }
        ]

    cases = [
        ('complete_basic', 'test-demo-empty', [], 13),
        ('history_basic', 'test-demo-empty', historical(23), 20),
        ('seven_lifestyle_remaining', 'test-demo-empty', historical(7), 10),
        ('one_lifestyle_remaining', 'test-demo-empty', historical(1), 4),
        ('exhausted', 'test-demo-empty', historical(0, answered_background=True), 0),
        ('report_material', 'test-demo-report', [], 20),
        ('chat_material', 'test-demo-chat', [], 20),
    ]
    answer_schema = {
        'title': '合成历史活动问卷',
        'pages': [{'name': 'daily', 'elements': [deepcopy(targets[0]['question'])]}],
    }
    answer_question = answer_schema['pages'][0]['elements'][0]
    answer = (
        [answer_question['choices'][0]['value']]
        if answer_question['type'] == 'checkbox'
        else answer_question['choices'][0]['value']
    )
    cases.append(
        (
            'answer_material',
            'test-demo-empty',
            [{'schema': answer_schema, 'answer': {answer_question['name']: answer}}],
            20,
        )
    )
    results = []
    uuids = set()
    try:
        for name, user, surveys, expected_count in cases:
            response = await client.fetch(
                f'http://127.0.0.1:{port}/api/v1/health-analysis',
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=json.dumps(
                    {
                        'userId': user,
                        'name': '合成用户',
                        'age': 42,
                        'gender': '男',
                        'groupId': DEMO_GROUP,
                        'surveyList': surveys,
                    },
                    ensure_ascii=False,
                ),
                raise_error=False,
            )
            payload = json.loads(response.body)
            if response.code != 200:
                raise AssertionError(f'{name}: unexpected HTTP {response.code}: {payload["message"]}')
            data = payload['data']
            if set(data) != {'healthReview', 'suggestedMaterials', 'advancedQuestions', 'advancedSurveySchema'}:
                raise AssertionError('Unexpected response contract')
            schema = data['advancedSurveySchema']
            count = sum(len(page['elements']) for page in schema['pages']) if schema else 0
            if count != expected_count:
                raise AssertionError(f'{name}: expected {expected_count} questions, got {count}')
            uuids.add(payload['uuid'])
            results.append({'case': name, 'status': response.code, 'questions': count})
        for name, body, expected_status in (
            (
                'missing_basic_info',
                json.dumps({'userId': 'test-demo-empty', 'groupId': 'unconfigured-group', 'surveyList': []}),
                400,
            ),
            ('invalid_json', '{', 400),
        ):
            response = await client.fetch(
                f'http://127.0.0.1:{port}/api/v1/health-analysis',
                method='POST',
                headers={'Content-Type': 'application/json'},
                body=body,
                raise_error=False,
            )
            if response.code != expected_status:
                raise AssertionError(f'{name}: unexpected HTTP {response.code}')
            results.append({'case': name, 'status': response.code})
        records = [json.loads(path.read_text(encoding='utf-8')) for path in Path(trace_dir).glob('*.json')]
        confirmed = {record['uuid'] for record in records if record.get('uuid') in uuids}
        if confirmed != uuids:
            raise AssertionError('Successful HTTP results lack confirmed trace files')
        return {'mode': 'offline-synthetic', 'cases': results, 'confirmedTraceRecords': len(confirmed)}
    finally:
        client.close()
        server.stop()
        await server.close_all_connections()


def main():
    parser = argparse.ArgumentParser(description='Synthetic offline Health Analysis browser backend')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=9081)
    parser.add_argument('--trace-dir', type=Path, help='Retain synthetic trace JSON outside the repository')
    parser.add_argument('--smoke', action='store_true', help='Run a local HTTP acceptance matrix and exit')
    options = parser.parse_args()
    directory = options.trace_dir or Path(tempfile.mkdtemp(prefix='shulan-health-analysis-demo-'))
    if options.smoke:
        print(json.dumps(asyncio.run(smoke(directory)), ensure_ascii=False, indent=4))
        return
    application = make_demo_app(directory)
    from tornado.httpserver import HTTPServer
    from tornado.ioloop import IOLoop

    server = HTTPServer(application)
    server.listen(options.port, address=options.host)
    print(f'Synthetic backend: http://{options.host}:{options.port}', flush=True)
    print('Only synthetic confirmation storage, sources, model and database fixtures are used.', flush=True)
    print(f'Synthetic trace files: {external_artifact_path(directory)}', flush=True)
    try:
        IOLoop.current().start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == '__main__':
    main()
