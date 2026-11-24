"""Refresh/reanswer through real HTTP handlers, Record graphs and store adapters.

Only the model and storage clients are in-memory substitutes. No deployment
configuration, real patient data or live model is used.
"""

import asyncio
import json
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import tornado.web
from tornado.httpclient import HTTPClientError
from tornado.testing import AsyncHTTPTestCase
from tornado.testing import gen_test

from adapters.persistence import MongoRecordSessionRepository
from adapters.persistence import RedisRecordSessionCache
from adapters.record import AionPromptRenderer
from agents.errors import AionGatewayError
from modules.record.application.recovery import ReplyRecoveryCoordinator
from modules.record.application.service import QASessionManager
from tests.unit.agents.graphs.record.test_background_reply_recovery import CompletionStore
from tests.unit.agents.graphs.record.test_background_reply_recovery import RecoveryStore
from tests.unit.agents.graphs.record.test_dynamic_record import PatientGateway
from tests.unit.agents.graphs.record.test_dynamic_record import assessment
from tests.unit.agents.graphs.record.test_record_agent import FakeCollection
from tests.unit.agents.graphs.record.test_record_agent import FakeRedis
from tests.unit.agents.graphs.record.test_record_agent import _build_runtime
from web.handlers.record import QASessionAnswerHandler
from web.handlers.record import QASessionAnswerStreamHandler
from web.handlers.record import QASessionCreateHandler
from web.handlers.record import QASessionGetHandler
from web.handlers.record import QASessionHistoryHandler


class RecordRefreshReanswerHTTPTest(AsyncHTTPTestCase):
    def get_app(self):
        self.bindings = {}
        self.reset_storage()
        return tornado.web.Application(
            [
                ('/api/v1/qa/create', QASessionCreateHandler, self.bindings),
                ('/api/v1/qa/answer/complete', QASessionAnswerHandler, self.bindings),
                ('/api/v1/qa/answer/stream', QASessionAnswerStreamHandler, self.bindings),
                ('/api/v1/qa/status', QASessionGetHandler, self.bindings),
                ('/api/v1/qa/history', QASessionHistoryHandler, self.bindings),
            ]
        )

    def reset_storage(self):
        self.redis = FakeRedis()
        self.collection = FakeCollection()
        self.rebuild_manager()

    def rebuild_manager(self):
        # A new manager AND runtime cannot recover anything from old Python
        # object state; only the serialized cache/completed document survives.
        self.gateway = PatientGateway()
        self.manager = QASessionManager(
            _build_runtime(self.gateway),
            session_cache=RedisRecordSessionCache(self.redis),
            session_repository=MongoRecordSessionRepository(self.collection),
            prompt_renderer=AionPromptRenderer(),
        )
        self.bindings['service'] = self.manager

    async def recover_without_get_side_effects(self):
        calls, raw = len(self.gateway.requests), self.redis.value
        for endpoint in ('status', 'history'):
            pending = await self.post(endpoint)
            self.assertEqual(pending.code, 409, pending.body)
        self.assertEqual(self.redis.value, raw)
        self.assertEqual(len(self.gateway.requests), calls)
        clients = SimpleNamespace(redis_client=self.redis, collection_sessions=self.collection)
        store = RecoveryStore(clients)
        self.manager._reply_recovery_store = store
        self.manager._completion_store = CompletionStore(clients)
        self.manager._reply_recovery = ReplyRecoveryCoordinator(self.manager, store)
        self.assertEqual(await self.manager.recoverPendingReplies(), 1)

    async def post(self, endpoint, **body):
        return await self.http_client.fetch(
            self.get_url('/api/v1/qa/' + endpoint),
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps({'userId': 'u', 'sessionId': 's', **body}, ensure_ascii=False),
            raise_error=False,
        )

    def data(self, response):
        self.assertEqual(response.code, 200, response.body)
        envelope = json.loads(response.body)
        self.assertEqual(envelope['code'], 200, envelope)
        return envelope['data']

    @staticmethod
    def events(response):
        return [json.loads(line[6:]) for line in response.body.decode().splitlines() if line.startswith('data: ')]

    async def answer(self, question, answer, *, streaming=False):
        response = await self.post(
            'answer/stream' if streaming else 'answer/complete',
            questionId=question['questionId'],
            answer=answer,
            thinking=streaming,
        )
        if not streaming:
            return self.data(response)
        self.assertEqual(response.code, 200, response.body)
        events = self.events(response)
        self.assertEqual(events[-1]['data']['type'], 'finish', events)
        return next(event['data'] for event in events if event['data'].get('status') in ('question', 'finished'))

    async def start_with_history(self):
        first = self.data(await self.post('create', basicInfo={'age': 30}))
        self.gateway.next_assessment = assessment(sufficient=False)
        current = await self.answer(first, '最近餐后偶尔有些不舒服。')
        self.assertEqual(current['interactionCount'], 1)
        self.assertEqual(current['phase'], 'intake')
        return current

    async def enter_supplement(self, current):
        # These HTTP transaction checks resume an existing 4-6 question
        # session; fresh-policy coverage uses the full expanded interview.
        legacy = json.loads(self.redis.value)
        legacy['qaFlow'].pop('interviewPolicy', None)
        self.redis.value = json.dumps(legacy, ensure_ascii=False)
        for _ in range(6):
            if current['phase'] != 'intake':
                break
            self.gateway.next_assessment = assessment(sufficient=True)
            current = await self.answer(current, '已经说明清楚了，没有其他补充。')
        self.assertEqual(current['phase'], 'supplement')
        self.assertGreaterEqual(current['intakeQuestionCount'], 4)
        return current

    async def refresh_without_side_effects(self):
        cache_before = self.redis.value
        document_before = deepcopy(self.collection.document)
        calls = len(self.gateway.requests)
        with (
            patch.object(self.redis, 'setex', wraps=self.redis.setex) as save_cache,
            patch.object(self.redis, 'delete', wraps=self.redis.delete) as delete_cache,
            patch.object(self.collection, 'replace_one', wraps=self.collection.replace_one) as save_document,
        ):
            status_response, history_response = await asyncio.gather(self.post('status'), self.post('history'))
            status, history = self.data(status_response), self.data(history_response)
            save_cache.assert_not_awaited()
            delete_cache.assert_not_awaited()
            save_document.assert_not_awaited()
        self.assertEqual(self.redis.value, cache_before)
        self.assertEqual(self.collection.document, document_before)
        self.assertEqual(len(self.gateway.requests), calls)
        return status, history

    @gen_test(timeout=30)
    async def test_refresh_resumes_legacy_round_gate_and_accepts_existing_completion_option(self):
        await self.start_with_history()
        legacy = json.loads(self.redis.value)
        legacy.pop('qaFlow')
        legacy.update(status='awaiting_decision', currentQuestion=None, plannedTotal=1, round=1)
        self.redis.value = json.dumps(legacy, ensure_ascii=False)
        calls = len(self.gateway.requests)
        await self.recover_without_get_side_effects()
        restored = self.data(await self.post('status'))
        self.assertEqual(restored['phase'], 'supplement')
        self.assertEqual(restored['options'], ['没有了'])
        self.assertEqual(restored['interactionCount'], 1)
        self.assertEqual(len(self.gateway.requests), calls)
        self.assertEqual(json.loads(self.redis.value)['qaHistory'], legacy['qaHistory'])
        finished = await self.answer(restored, '没有了', streaming=True)
        self.assertEqual(finished['status'], 'finished')
        self.assertEqual(finished['interactionCount'], 1)
        self.assertEqual(len(self.data(await self.post('history'))['history']), 2)

    @gen_test(timeout=30)
    async def test_refresh_generation_errors_publish_fallback_without_recounting_saved_answer(self):
        for endpoint in ('status', 'history'):
            for failure in ('gateway', 'validation'):
                with self.subTest(endpoint=endpoint, failure=failure):
                    self.reset_storage()
                    await self.start_with_history()
                    broken = json.loads(self.redis.value)
                    broken['qaHistory'].append(
                        {
                            'question': deepcopy(broken['currentQuestion']),
                            'answer': '最近饭后有些腹胀。',
                            'answeredAt': '2026-01-01T10:00:00',
                            'interactionCount': 2,
                        }
                    )
                    broken['answers'][broken['currentQuestion']['questionId']] = '最近饭后有些腹胀。'
                    broken['currentQuestion'] = None
                    broken['qaFlow'].update(interactionCount=2, intakeCount=2)
                    self.redis.value = json.dumps(broken, ensure_ascii=False)
                    with ExitStack() as failures:
                        if failure == 'gateway':
                            failures.enter_context(
                                patch.object(self.gateway, 'content', side_effect=AionGatewayError('synthetic failure'))
                            )
                        else:
                            self.gateway.invalid_response = 2
                        await self.recover_without_get_side_effects()
                        response = await self.post(endpoint)
                    self.assertEqual(self.data(response)['status'], 'question')
                    saved = json.loads(self.redis.value)
                    self.assertIn(saved['qaFlow']['generationQuality']['mode'], ('fallback', 'partial'))
                    self.assertNotEqual(
                        saved['currentQuestion']['questionId'], broken['qaHistory'][-1]['question']['questionId']
                    )
                    restored, history = await self.refresh_without_side_effects()
                    self.assertEqual(restored['interactionCount'], 2)
                    self.assertEqual(len(history['history']), 2)
                    saved = json.loads(self.redis.value)
                    self.assertEqual(saved['qaHistory'], broken['qaHistory'])
                    self.assertEqual(saved['answers'], broken['answers'])

    @gen_test(timeout=30)
    async def test_http_model_fallback_replays_and_unwritten_receipt_can_be_reanswered(self):
        for streaming in (False, True):
            for failure, code in (('gateway', 500), ('validation', 502), ('save', 500)):
                with self.subTest(streaming=streaming, failure=failure):
                    self.reset_storage()
                    current = await self.start_with_history()
                    before = self.redis.value
                    before_history = self.data(await self.post('history'))
                    with ExitStack() as failures:
                        if failure == 'gateway':
                            failures.enter_context(
                                patch.object(self.gateway, 'content', side_effect=AionGatewayError('synthetic failure'))
                            )
                        elif failure == 'validation':
                            self.gateway.invalid_response = 2
                        else:
                            failures.enter_context(patch.object(self.redis, 'setex', AsyncMock(return_value=False)))
                        response = await self.post(
                            'answer/stream' if streaming else 'answer/complete',
                            questionId=current['questionId'],
                            answer='本次提交的回答应按原文保留。',
                            thinking=streaming,
                        )
                    if failure != 'save':
                        if streaming:
                            events = self.events(response)
                            self.assertEqual(events[0]['data']['type'], 'start')
                            self.assertEqual(events[-1]['data']['type'], 'finish')
                            self.assertTrue(all(event['code'] == 200 for event in events))
                            result = next(event['data'] for event in events if event['data']['type'] == 'question')
                        else:
                            result = self.data(response)
                        self.assertEqual(result['interactionCount'], 2)
                        restored, history = await self.refresh_without_side_effects()
                        self.assertEqual(restored['questionId'], result['questionId'])
                        self.assertEqual(len(history['history']), 2)
                        self.assertEqual(history['history'][-1]['answer'], '本次提交的回答应按原文保留。')
                        calls = len(self.gateway.requests)
                        replay = await self.answer(current, '本次提交的回答应按原文保留。', streaming=streaming)
                        self.assertEqual(replay['questionId'], restored['questionId'])
                        conflict = await self.post(
                            'answer/complete', questionId=current['questionId'], answer='不能替换已保存的旧答案。'
                        )
                        self.assertEqual(conflict.code, 409, conflict.body)
                        self.assertEqual(len(self.gateway.requests), calls)
                        continued = await self.answer(restored, '继续补充：主要是饭后腹胀。', streaming=streaming)
                        self.assertEqual(continued['interactionCount'], 3)
                        self.assertEqual(len(self.data(await self.post('history'))['history']), 3)
                        continue
                    if streaming:
                        self.assertEqual(response.code, 200)
                        events = self.events(response)
                        self.assertEqual(events[0]['data']['type'], 'start')
                        self.assertEqual(events[-1]['data']['type'], 'error')
                        self.assertEqual(events[-1]['code'], code)
                        self.assertFalse(any(event['data']['type'] in ('question', 'finish') for event in events))
                    else:
                        self.assertEqual(response.code, code, response.body)
                        self.assertEqual(json.loads(response.body)['code'], code)
                    self.assertEqual(self.redis.value, before)
                    restored, history = await self.refresh_without_side_effects()
                    self.assertEqual(restored, current)
                    self.assertEqual(history, before_history)
                    result = await self.answer(restored, '重新回答：主要是饭后腹胀。', streaming=streaming)
                    self.assertEqual(result['interactionCount'], 2)
                    self.assertNotEqual(result['questionId'], current['questionId'])
                    history = self.data(await self.post('history'))['history']
                    self.assertEqual(len(history), 2)
                    self.assertEqual(history[-1]['answer'], '重新回答：主要是饭后腹胀。')

    @gen_test(timeout=30)
    async def test_saved_result_survives_actual_http_connection_loss_and_manager_rebuild(self):
        original_write = QASessionAnswerStreamHandler._write_sse_event

        def drop_complete_response(handler, code, message, data=None):
            # The real manager has committed before the HTTP handler writes.
            handler.request.connection.stream.close()

        async def drop_stream_result(handler, payload):
            if payload['data']['type'] in ('question', 'finish'):
                handler.request.connection.stream.close()
                handler._mark_client_disconnected()
                return False
            return await original_write(handler, payload)

        for streaming in (False, True):
            for ending in (False, True):
                with self.subTest(streaming=streaming, ending=ending):
                    self.reset_storage()
                    current = await self.start_with_history()
                    answer = '这次回答用于已保存但响应丢失的场景。'
                    if ending:
                        current = await self.enter_supplement(current)
                        self.assertEqual(current['phase'], 'supplement')
                        answer = '没有了'
                    expected_length = len(json.loads(self.redis.value)['qaHistory']) + 1
                    with patch.object(
                        QASessionAnswerStreamHandler if streaming else QASessionAnswerHandler,
                        '_write_sse_event' if streaming else 'write_response',
                        drop_stream_result if streaming else drop_complete_response,
                    ):
                        with self.assertRaises(HTTPClientError) as disconnected:
                            await self.post(
                                'answer/stream' if streaming else 'answer/complete',
                                questionId=current['questionId'],
                                answer=answer,
                                thinking=streaming,
                            )
                    self.assertEqual(disconnected.exception.code, 599)
                    committed = deepcopy(self.collection.document) if ending else json.loads(self.redis.value)
                    expected_count = current['interactionCount'] + (0 if ending else 1)
                    self.assertEqual(len(committed['qaHistory']), expected_length)
                    self.rebuild_manager()
                    restored, history = await self.refresh_without_side_effects()
                    self.assertEqual(restored['status'], 'finished' if ending else 'question')
                    self.assertEqual(restored['interactionCount'], expected_count)
                    self.assertEqual(len(history['history']), expected_length)
                    self.assertEqual(history['history'][-1]['answer'], answer)
                    if not ending:
                        self.assertNotEqual(restored['questionId'], current['questionId'])
                    self.assertEqual(self.gateway.requests, [])
                    replay = await self.answer(current, answer, streaming=streaming)
                    self.assertEqual(replay['status'], restored['status'])
                    self.assertEqual(replay['interactionCount'], expected_count)
                    self.assertEqual(self.gateway.requests, [])
                    if not ending:
                        self.assertEqual(replay['questionId'], restored['questionId'])
                    conflict = await self.post(
                        'answer/complete', questionId=current['questionId'], answer='已成功提交后不能更换旧答案。'
                    )
                    self.assertEqual(conflict.code, 409, conflict.body)
                    self.assertEqual(self.gateway.requests, [])
                    self.assertEqual(len(self.data(await self.post('history'))['history']), expected_length)

    @gen_test(timeout=30)
    async def test_storage_acknowledgement_loss_does_not_roll_back_a_committed_result(self):
        for streaming in (False, True):
            for ending in (False, True):
                with self.subTest(streaming=streaming, ending=ending):
                    self.reset_storage()
                    current = await self.start_with_history()
                    answer = '饭后有些腹胀。'
                    client, method = self.redis, 'setex'
                    if ending:
                        current = await self.enter_supplement(current)
                        answer = '没有了'
                        client, method = self.collection, 'replace_one'
                    expected_count = current['interactionCount'] + (0 if ending else 1)
                    expected_length = len(json.loads(self.redis.value)['qaHistory']) + 1
                    save = getattr(client, method)
                    writes = 0

                    async def save_then_disconnect(*args, **kwargs):
                        nonlocal writes
                        writes += 1
                        result = await save(*args, **kwargs)
                        # Active answers write a receipt first, then publish.
                        # This test loses acknowledgement of the final result.
                        if not ending and writes == 1:
                            return result
                        raise ConnectionError('synthetic acknowledgement loss after storage committed')

                    with patch.object(client, method, side_effect=save_then_disconnect):
                        response = await self.post(
                            'answer/stream' if streaming else 'answer/complete',
                            questionId=current['questionId'],
                            answer=answer,
                            thinking=streaming,
                        )
                    if streaming:
                        events = self.events(response)
                        self.assertEqual(response.code, 200)
                        self.assertEqual(events[-1]['data']['type'], 'error')
                        self.assertEqual(events[-1]['code'], 500)
                        self.assertFalse(any(event['data']['type'] in ('question', 'finish') for event in events))
                    else:
                        self.assertEqual(response.code, 500)
                    self.rebuild_manager()
                    restored, history = await self.refresh_without_side_effects()
                    self.assertEqual(restored['status'], 'finished' if ending else 'question')
                    self.assertEqual(restored['interactionCount'], expected_count)
                    self.assertEqual(len(history['history']), expected_length)
                    self.assertEqual(history['history'][-1]['answer'], answer)
                    if ending:
                        # MongoDB completed state wins even though the failed
                        # acknowledgement left the older active Redis snapshot.
                        self.assertIsNotNone(self.redis.value)
                        self.assertEqual(json.loads(self.redis.value)['status'], 'active')
                    else:
                        self.assertNotEqual(restored['questionId'], current['questionId'])
                    replay = await self.answer(current, answer, streaming=streaming)
                    self.assertEqual(replay['status'], restored['status'])
                    self.assertEqual(replay['interactionCount'], restored['interactionCount'])
                    if not ending:
                        self.assertEqual(replay['questionId'], restored['questionId'])
                    self.assertEqual(self.gateway.requests, [])

    @gen_test(timeout=15)
    async def test_interrupted_presentation_recovers_original_answer_after_manager_rebuild(self):
        current = await self.start_with_history()
        before = self.redis.value
        presentation_started = asyncio.Event()
        complete = self.gateway.complete

        async def suspended_presentation(request):
            if request.profile.name == 'record.question':
                presentation_started.set()
                await asyncio.Future()
            return await complete(request)

        with patch.object(self.gateway, 'complete', side_effect=suspended_presentation):
            task = asyncio.create_task(
                self.manager.submitAnswer('u', 's', current['questionId'], '服务中断时的回答。', thinking=False)
            )
            try:
                await asyncio.wait_for(presentation_started.wait(), timeout=5)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        interrupted = json.loads(self.redis.value)
        original = json.loads(before)
        self.assertEqual(interrupted['qaHistory'], original['qaHistory'])
        self.assertEqual(interrupted['currentQuestion'], original['currentQuestion'])
        self.assertEqual(interrupted['qaFlow']['pendingSubmission']['answer'], '服务中断时的回答。')
        self.rebuild_manager()
        conflict = await self.post('answer/complete', questionId=current['questionId'], answer='不能替换待恢复的答案。')
        self.assertEqual(conflict.code, 409, conflict.body)
        self.assertEqual(self.gateway.requests, [])
        await self.recover_without_get_side_effects()
        recovered = self.data(await self.post('status'))
        self.assertEqual(recovered['interactionCount'], 2)
        self.assertNotEqual(recovered['questionId'], current['questionId'])
        self.assertEqual(len(self.gateway.requests), 2)
        restored, history = await self.refresh_without_side_effects()
        self.assertEqual(restored, recovered)
        self.assertEqual(len(history['history']), 2)
        self.assertEqual(history['history'][-1]['answer'], '服务中断时的回答。')
        replay = await self.answer(current, '服务中断时的回答。')
        self.assertEqual(replay, recovered)
        self.assertEqual(len(self.gateway.requests), 2)
        result = await self.answer(restored, '重启后继续补充。')
        self.assertEqual(result['interactionCount'], 3)
        history = self.data(await self.post('history'))['history']
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1]['answer'], '重启后继续补充。')

    @gen_test(timeout=30)
    async def test_lost_receipt_acknowledgement_recovers_from_serialized_state_after_rebuild(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                self.reset_storage()
                current = await self.start_with_history()
                before = json.loads(self.redis.value)
                calls = len(self.gateway.requests)
                save = self.redis.setex

                async def save_receipt_then_disconnect(*args, **kwargs):
                    await save(*args, **kwargs)
                    raise ConnectionError('synthetic lost receipt acknowledgement')

                with patch.object(self.redis, 'setex', side_effect=save_receipt_then_disconnect):
                    response = await self.post(
                        'answer/stream' if streaming else 'answer/complete',
                        questionId=current['questionId'],
                        answer='回执已保存但确认响应丢失。',
                    )
                if streaming:
                    events = self.events(response)
                    self.assertEqual(events[-1]['data']['type'], 'error')
                    self.assertEqual(events[-1]['code'], 500)
                    self.assertFalse(any(event['data']['type'] in ('question', 'finish') for event in events))
                else:
                    self.assertEqual(response.code, 500, response.body)
                interrupted = json.loads(self.redis.value)
                self.assertEqual(interrupted['qaHistory'], before['qaHistory'])
                self.assertEqual(interrupted['qaFlow']['pendingSubmission']['answer'], '回执已保存但确认响应丢失。')
                self.assertEqual(len(self.gateway.requests), calls)
                self.rebuild_manager()
                await self.recover_without_get_side_effects()
                recovered = self.data(await self.post('status'))
                self.assertEqual(recovered['interactionCount'], 2)
                self.assertEqual(len(self.gateway.requests), 2)
                restored, history = await self.refresh_without_side_effects()
                self.assertEqual(restored, recovered)
                self.assertEqual(len(history['history']), 2)
                self.assertEqual(history['history'][-1]['answer'], '回执已保存但确认响应丢失。')
                self.assertEqual(await self.answer(current, '回执已保存但确认响应丢失。'), recovered)
                self.assertEqual(len(self.gateway.requests), 2)
