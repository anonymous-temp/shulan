"""Current preparation and recovery through the production BSON trace adapter."""

from copy import deepcopy

import pytest

from adapters.analysis.document_trace import MongoDocumentTraceStore
from health_profile.document_source_contracts_v2 import DocumentBasicInfo
from modules.analysis.application.current_interpreter import CurrentHealthReportInterpreter
from modules.analysis.application.documents import DocumentInterpretationError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.service import HealthReportProcessingService
from modules.analysis.preparation import payload_digest
from tests.unit._support.current_document import report
from tests.unit.services.analysis.fakes import FakeDocumentCollection
from tests.unit.services.analysis.test_basic_info_current import TEXT
from tests.unit.services.analysis.test_current_preparation import current_flow
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_workflow import install_graph
from utils.report_content import report_source_content


def assert_no_basic_merge_marker(*documents):
    pending = list(documents)
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            assert '_basicInfoMerged' not in value
            pending.extend(value.values())
        elif isinstance(value, (list, tuple)):
            pending.extend(value)


def current_mongo_flow(respond):
    flow = current_flow()
    flow.metadata, flow.chunks = FakeDocumentCollection(), FakeDocumentCollection()
    flow.traces = MongoDocumentTraceStore(flow.metadata, flow.chunks)
    flow.service._document_trace_store = flow.traces
    flow.service._document_reuse.POLL_SECONDS = 0.005
    install_graph(flow, respond)
    return flow


def restarted_service(flow):
    previous = flow.service
    flow.traces = MongoDocumentTraceStore(flow.metadata, flow.chunks)
    flow.current = CurrentHealthReportInterpreter(
        flow.current._agent_runtime, count_input_tokens=len, docmind_client=flow.client
    )
    flow.service = HealthReportProcessingService(
        interpreter=flow.interpreter,
        current_interpreter=flow.current,
        report_source=flow.source,
        health_profile_service=previous._health_profile_service,
        document_trace_store=flow.traces,
        document_index=flow.index,
        document_downloader=flow.downloader,
        document_output_index=flow.output_index,
        output_profiles=previous.output_profiles,
    )
    flow.service._document_reuse.POLL_SECONDS = 0.005
    return flow.service


async def read_checkpoint(flow, name):
    document = next(iter(flow.index_collection.documents.values()))
    return await flow.traces.read_payload(
        document['payloadRefs'][name], document['userId'], document['subjectId'], name
    )


def phase_reply(*, repair=False, check=False):
    def respond(text):
        if '第二阶段仅核查' in text:
            return report()
        raw = report(name=None if check else '测试甲')
        if repair and '请仅修复输出' not in text:
            raw['title'] = 123
        return raw

    return respond


def profile_age_contribution(fixed):
    age = fixed.interpretation['basicInfoMeta']['age']
    snapshot = fixed.preparation_binding.input_snapshot
    return DocumentBasicInfo(
        field='age',
        value=fixed.interpretation['basicInfo']['age'],
        source_reliability=age['sourceReliability'],
        observed_at=age['observedAt'],
        reason=age['reason'],
        evidence=age['evidence'],
        subject_status=age['subjectStatus'],
        subject_ref=age['subjectRef'],
        subject_evidence=age['subjectEvidence'],
        contribution_id='synthetic-age',
        understanding_snapshot_ref='document_snapshot_' + 'a' * 64,
        facts_snapshot_ref='document_snapshot_' + 'b' * 64,
        interpretation_round=snapshot.interpretation_round,
        interpretation_revision=snapshot.interpretation_revision,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('repair,check', [(False, False), (True, False), (False, True), (True, True)])
async def test_current_prepare_archives_each_required_phase_with_the_real_trace_store(repair, check):
    flow = current_mongo_flow(phase_reply(repair=repair, check=check))
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    assert prepared.report['basicInfo'] == {'name': '测试甲', 'gender': '女', 'age': 42}
    assert [item['resultValue'] for item in prepared.report['observations']] == ['3']
    assert prepared.preparation_binding.input_snapshot.interpretation_revision == flow.current.VERSION
    assert prepared.output_selection.implementation_revision == 'analysis-health-report-v13'
    assert not flow.collection.documents and not flow.jobs.jobs
    assert len(flow.graph.calls) == 1 + int(repair) + int(check)
    assert len(flow.client.submit_calls) == 1
    first = await read_checkpoint(flow, 'analysis_part_00000')
    assert first['firstPhaseDone'] and first['firstPhaseCalls'] == 1 + int(repair)
    assert first['identity']['version'] == flow.current.VERSION
    phases = [('first', '')]
    if repair:
        phases.append(('repair', '_repair'))
    if check:
        phases.append(('basic_check', '_0'))
    for phase, suffix in phases:
        request_payload = await read_checkpoint(flow, f'analysis_input_00000{suffix}')
        reply_payload = await read_checkpoint(flow, f'analysis_output_00000{suffix}')
        assert request_payload['phase'] == reply_payload['phase'] == phase
        assert request_payload['source'] == reply_payload['source'] == first['identity']
        assert request_payload['source']['start'] == 0
        assert request_payload['source']['end'] == len(TEXT)
        assert TEXT in request_payload['text']
        assert reply_payload['content']
    if check:
        checked = await read_checkpoint(flow, 'analysis_part_00000_0')
        assert checked['started'] and checked['done']
        assert checked['result']['basicInfo']['name'] == '测试甲'
        assert checked['identity'] == first['identity']
    else:
        document = next(iter(flow.index_collection.documents.values()))
        assert 'analysis_part_00000_0' not in document['payloadRefs']
    archived = await read_checkpoint(flow, 'interpretation')
    assert payload_digest(archived) == prepared.preparation_binding.interpretation_digest
    assert report_source_content(archived) == prepared.report
    fixed = await flow.service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=prepared.preparation_binding.input_snapshot.interpretation_round,
    )
    assert fixed.interpretation == archived
    assert_no_basic_merge_marker(prepared.report, archived, fixed.interpretation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'observed_at,expected',
    [
        ('20260901T091506+0800', '2026-09-01T09:15:06+08:00'),
        ('20260901T0915+0800', '2026-09-01T09:15+08:00'),
        ('20260901T091506.1234567+0800', '2026-09-01T09:15:06.1234567+08:00'),
        ('20260901T091506+0860', None),
        ('2026-09-01T09:15:06+08:60', None),
    ],
)
async def test_current_preparation_archives_normalized_demographic_time_without_dropping_evidence(
    observed_at, expected
):
    def respond(_):
        raw = report(reliability='medium')
        raw['basicInfoMeta']['age']['observedAt'] = observed_at
        raw['basicInfoMeta']['name']['observedAt'] = '2026-09-01X09:15:06'
        return raw

    flow = current_mongo_flow(respond)
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    binding = prepared.preparation_binding
    fixed = await flow.service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=binding.input_snapshot.interpretation_round,
    )
    assert payload_digest(fixed.interpretation) == binding.interpretation_digest
    assert fixed.interpretation['basicInfo'] == {'name': '测试甲', 'gender': '女', 'age': 42}
    metadata = fixed.interpretation['basicInfoMeta']
    assert metadata == prepared.report['basicInfoMeta']
    assert metadata['age']['observedAt'] == profile_age_contribution(fixed).observed_at == expected
    assert metadata['name']['observedAt'] is None
    for field, quote in [('age', '年龄：42岁'), ('name', '姓名：测试甲')]:
        item = metadata[field]
        assert item['status'] == 'extracted' and item['sourceReliability'] == 'medium'
        proof = item['evidence']
        assert fixed.parsed_document['text'][proof['start'] : proof['end']] == proof['quote'] == quote
    assert [item['resultValue'] for item in fixed.interpretation['observations']] == ['3']
    assert len(flow.graph.calls) == len(flow.client.submit_calls) == 1
    assert not flow.collection.documents and not flow.jobs.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'failure,expected_calls,expected_name',
    [
        ('first_input', 1, '测试甲'),
        ('first_output', 2, '测试甲'),
        ('basic_input', 1, None),
        ('basic_output', 2, None),
        ('basic_checkpoint_before_write', 2, None),
        ('basic_checkpoint_ack_lost', 2, '测试甲'),
    ],
)
async def test_current_archive_io_recovery_rebuilds_services_without_resetting_phase_budget(
    failure, expected_calls, expected_name
):
    checking = failure.startswith('basic_')
    flow = current_mongo_flow(phase_reply(check=checking))
    original_write = flow.traces.write_payload
    failed = False
    names = {
        'first_input': 'analysis_input_00000',
        'first_output': 'analysis_output_00000',
        'basic_input': 'analysis_input_00000_0',
        'basic_output': 'analysis_output_00000_0',
        'basic_checkpoint_before_write': 'analysis_part_00000_0',
        'basic_checkpoint_ack_lost': 'analysis_part_00000_0',
    }

    async def interrupt(trace_id, user_id, name, payload):
        nonlocal failed
        checkpoint = failure.startswith('basic_checkpoint')
        matches = name == names[failure] and (not checkpoint or payload.get('done') is True)
        if matches and not failed:
            failed = True
            if failure == 'basic_checkpoint_ack_lost':
                await original_write(trace_id, user_id, name, payload)
            raise RuntimeError('synthetic required archive interruption')
        return await original_write(trace_id, user_id, name, payload)

    flow.traces.write_payload = interrupt
    with pytest.raises(RuntimeError, match='synthetic required archive interruption'):
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    assert failed
    document = next(iter(flow.index_collection.documents.values()))
    assert document['status'] == 'failed'
    old_round = document['interpretationRound']
    service = restarted_service(flow)
    prepared = await service.prepare_document(user_id='user-1', subject_id='subject-self', document_id=document['_id'])
    assert prepared.report['basicInfo']['name'] == expected_name
    assert [item['resultValue'] for item in prepared.report['observations']] == ['3']
    assert len(flow.graph.calls) == expected_calls
    assert len(flow.client.submit_calls) == 1
    assert flow.downloader.download.await_count == 1
    assert prepared.preparation_binding.input_snapshot.interpretation_round == old_round
    assert prepared.output_selection.implementation_revision == flow.current.VERSION
    assert not flow.collection.documents and not flow.jobs.jobs
    first = await read_checkpoint(flow, 'analysis_part_00000')
    assert first['firstPhaseDone'] and first['firstPhaseCalls'] <= 2
    if checking:
        checked = await read_checkpoint(flow, 'analysis_part_00000_0')
        assert checked['started']
        assert bool(checked.get('done')) is (failure == 'basic_checkpoint_ack_lost')
    fixed = await service.read_prepared_round(
        user_id='user-1', subject_id='subject-self', document_id=prepared.document_id, interpretation_round=old_round
    )
    assert fixed.interpretation == await read_checkpoint(flow, 'interpretation')
    assert payload_digest(fixed.interpretation) == prepared.preparation_binding.interpretation_digest
    assert report_source_content(fixed.interpretation) == prepared.report
    assert_no_basic_merge_marker(prepared.report, fixed.interpretation)
    assert len(flow.graph.calls) == expected_calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'interruption',
    ['first_checkpoint', 'basic_checkpoint', 'validated_interpretation', 'fixed_round_ack_lost'],
)
async def test_current_recovery_normalizes_demographic_time_only_before_first_archive(interruption):
    compact = '20260901T091506.1234567+0800'
    canonical = '2026-09-01T09:15:06.1234567+08:00'
    checking = interruption == 'basic_checkpoint'
    respond = phase_reply(check=checking)

    def timed_reply(text):
        raw = respond(text)
        raw['basicInfoMeta']['age']['observedAt'] = canonical
        return raw

    flow = current_mongo_flow(timed_reply)
    write = flow.traces.write_payload
    put_archive = flow.index.put_archived_round
    checkpoint_name = {
        'first_checkpoint': 'analysis_part_00000',
        'basic_checkpoint': 'analysis_part_00000_0',
    }.get(interruption)
    prior_interpretation = None

    def prior_time(payload):
        payload['basicInfoMeta']['age']['observedAt'] = compact

    async def write_prior_payload(trace_id, user_id, name, payload):
        nonlocal prior_interpretation
        if name == checkpoint_name and (payload.get('firstPhaseDone') or payload.get('done')):
            payload = deepcopy(payload)
            prior_time(payload['result'])
            prior_time(payload.get('rawData') or payload['result']['_verifiedData'])
        if name == 'interpretation':
            prior_interpretation = deepcopy(payload)
            prior_time(prior_interpretation)
            if checkpoint_name is not None:
                raise RuntimeError('synthetic archive interruption')
            payload = prior_interpretation
        return await write(trace_id, user_id, name, payload)

    async def interrupt_archive(document, token, now, archived):
        if interruption == 'fixed_round_ack_lost':
            archived = {**archived, 'interpretationDigest': payload_digest(prior_interpretation)}
            await put_archive(document, token, now, archived)
        raise RuntimeError('synthetic archive interruption')

    flow.traces.write_payload = write_prior_payload
    flow.index.put_archived_round = interrupt_archive
    with pytest.raises(RuntimeError, match='synthetic archive interruption'):
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    flow.index.put_archived_round = put_archive
    document = next(iter(flow.index_collection.documents.values()))
    old_round = document['interpretationRound']
    original_archive = await flow.index.get_archived_round(document['_id'], 'user-1', 'subject-self', old_round)
    fixed_before_retry = interruption == 'fixed_round_ack_lost'
    assert (original_archive is not None) is fixed_before_retry
    if checkpoint_name is not None:
        assert 'interpretation' not in document['payloadRefs']
        saved = await read_checkpoint(flow, checkpoint_name)
        assert saved['result']['basicInfoMeta']['age']['observedAt'] == compact
    else:
        assert document['interpretationValidatedVersion'] == flow.current.VERSION
        assert document['interpretationResultRound'] == old_round
        assert await read_checkpoint(flow, 'interpretation') == prior_interpretation
    chunks_before_retry = deepcopy(flow.chunks.documents)
    service = restarted_service(flow)
    scope = dict(user_id='user-1', subject_id='subject-self', document_id=document['_id'])
    before_fixed_read = deepcopy((flow.metadata.documents, flow.chunks.documents, flow.index_collection.documents))
    if fixed_before_retry:
        fixed_before = await service.read_prepared_round(**scope, interpretation_round=old_round)
        assert fixed_before.interpretation == prior_interpretation
        assert (flow.metadata.documents, flow.chunks.documents, flow.index_collection.documents) == before_fixed_read
    prepared = await service.prepare_document(**scope)
    assert prepared.preparation_binding.input_snapshot.interpretation_round == old_round
    fixed = await service.read_prepared_round(**scope, interpretation_round=old_round)
    assert prepared.preparation_binding == fixed.preparation_binding
    assert payload_digest(fixed.interpretation) == fixed.preparation_binding.interpretation_digest
    assert report_source_content(fixed.interpretation) == prepared.report
    assert await read_checkpoint(flow, 'interpretation') == fixed.interpretation
    if fixed_before_retry:
        assert fixed == fixed_before
        assert (
            await flow.index.get_archived_round(document['_id'], 'user-1', 'subject-self', old_round)
            == original_archive
        )
        assert flow.chunks.documents == chunks_before_retry
    else:
        expected = deepcopy(prior_interpretation)
        expected['basicInfoMeta']['age']['observedAt'] = canonical
        assert fixed.interpretation == expected
        assert profile_age_contribution(fixed).observed_at == canonical
    assert len(flow.graph.calls) == 1 + int(checking)
    assert len(flow.client.submit_calls) == len(flow.client.result_calls) == flow.downloader.download.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('parser_drift', [False, True])
async def test_empty_current_round_recovery_preserves_fixed_payload_when_archive_ack_is_lost(parser_drift):
    flow = current_mongo_flow(phase_reply())
    flow.client.result = {'success': True, 'text': ''}
    put_archive = flow.index.put_archived_round

    async def lose_ack(*args):
        await put_archive(*args)
        raise RuntimeError('synthetic fixed archive acknowledgement loss')

    flow.index.put_archived_round = lose_ack
    with pytest.raises(RuntimeError, match='synthetic fixed archive acknowledgement loss'):
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    flow.index.put_archived_round = put_archive
    document = next(iter(flow.index_collection.documents.values()))
    old_round = document['interpretationRound']
    scope = dict(user_id='user-1', subject_id='subject-self', document_id=document['_id'])
    service = restarted_service(flow)
    fixed = await service.read_prepared_round(**scope, interpretation_round=old_round)
    if parser_drift:
        document['payloadRefs'].pop('docmind_result')
    chunks = deepcopy(flow.chunks.documents)
    if parser_drift:
        with pytest.raises(RuntimeError, match='fixed document round lacks its validated artifacts'):
            await service.prepare_document(**scope)
        assert document['status'] == 'failed'
    else:
        with pytest.raises(DocumentInterpretationError) as error:
            await service.prepare_document(**scope)
        assert error.value.content_outcome == 'no_readable_text'
        assert error.value.result == fixed.interpretation
        assert document['status'] == 'completed'
    assert await service.read_prepared_round(**scope, interpretation_round=old_round) == fixed
    assert flow.chunks.documents == chunks
    assert len(flow.client.submit_calls) == len(flow.client.result_calls) == flow.downloader.download.await_count == 1
    assert not flow.graph.calls


@pytest.mark.asyncio
async def test_completed_current_execution_and_fixed_reads_do_not_repeat_processing_or_rewrite_artifacts():
    flow = current_mongo_flow(phase_reply(check=True))
    prepared = await flow.service.prepare_document(**request(subject_id='subject-self'))
    before_chunks = deepcopy(flow.chunks.documents)
    before_index = deepcopy(flow.index_collection.documents)
    counters = len(flow.graph.calls), len(flow.client.submit_calls), flow.downloader.download.await_count
    service = restarted_service(flow)
    repeated = await service.prepare_document(
        user_id='user-1', subject_id='subject-self', document_id=prepared.document_id
    )
    assert repeated.reused and repeated.preparation_binding == prepared.preparation_binding
    assert repeated.report == prepared.report
    assert flow.chunks.documents == before_chunks
    assert flow.index_collection.documents == before_index
    before_read = deepcopy((flow.metadata.documents, flow.chunks.documents, flow.index_collection.documents))
    args = dict(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=prepared.preparation_binding.input_snapshot.interpretation_round,
    )
    fixed = await service.read_prepared_round(**args)
    assert fixed.interpretation == await read_checkpoint(flow, 'interpretation')
    assert payload_digest(fixed.interpretation) == prepared.preparation_binding.interpretation_digest
    assert report_source_content(fixed.interpretation) == prepared.report
    assert_no_basic_merge_marker(prepared.report, fixed.interpretation)
    assert fixed.parsed_document['text'] == TEXT
    for user, subject in [('other-user', 'subject-self'), ('user-1', 'other-subject')]:
        with pytest.raises(DocumentNotFoundError):
            await service.read_prepared_round(**{**args, 'user_id': user, 'subject_id': subject})
    assert (flow.metadata.documents, flow.chunks.documents, flow.index_collection.documents) == before_read
    assert (len(flow.graph.calls), len(flow.client.submit_calls), flow.downloader.download.await_count) == counters
    assert not flow.collection.documents and not flow.jobs.jobs


@pytest.mark.asyncio
async def test_retryable_current_round_keeps_both_real_archives_without_internal_merge_markers():
    available = False

    def respond(_):
        if not available:
            return '{'
        container = report(name=None, gender=None, age=None)
        container.update(reportType='checkup', observations=[], sections=[report()])
        return container

    flow = current_mongo_flow(respond)
    with pytest.raises(DocumentInterpretationError) as failure:
        await flow.service.prepare_document(**request(subject_id='subject-self'))
    document = next(iter(flow.index_collection.documents.values()))
    first_round = document['interpretationRound']
    first_ref = document['payloadRefs']['interpretation']
    first_archive = await read_checkpoint(flow, 'interpretation')
    assert first_archive['processing']['retryable'] is True
    first_fixed = await flow.service.read_prepared_round(
        user_id='user-1', subject_id='subject-self', document_id=document['_id'], interpretation_round=first_round
    )
    assert first_fixed.interpretation == first_archive
    assert_no_basic_merge_marker(failure.value.result, first_archive, first_fixed.interpretation)
    first_calls = len(flow.graph.calls)
    assert first_calls == 3
    available = True
    service = restarted_service(flow)
    prepared = await service.prepare_document(user_id='user-1', subject_id='subject-self', document_id=document['_id'])
    assert prepared.preparation_binding.input_snapshot.interpretation_round == first_round + 1
    assert prepared.report['basicInfo'] == {'name': '测试甲', 'gender': '女', 'age': 42}
    assert len(flow.graph.calls) == first_calls + 1
    assert flow.downloader.download.await_count == len(flow.client.submit_calls) == 1
    second_archive = await read_checkpoint(flow, 'interpretation')
    assert payload_digest(second_archive) == prepared.preparation_binding.interpretation_digest
    assert report_source_content(second_archive) == prepared.report
    second_fixed = await service.read_prepared_round(
        user_id='user-1',
        subject_id='subject-self',
        document_id=prepared.document_id,
        interpretation_round=first_round + 1,
    )
    assert second_fixed.interpretation == second_archive
    old_fixed = await service.read_prepared_round(
        user_id='user-1', subject_id='subject-self', document_id=document['_id'], interpretation_round=first_round
    )
    assert old_fixed == first_fixed
    assert await flow.traces.read_payload(first_ref, 'user-1', 'subject-self', 'interpretation') == first_archive
    assert_no_basic_merge_marker(prepared.report, second_archive, second_fixed.interpretation, old_fixed.interpretation)
    assert len(flow.graph.calls) == first_calls + 1
    assert not flow.collection.documents and not flow.jobs.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'name',
    [
        'analysis_input_100000',
        'analysis_part_100000_0',
        'analysis_part_-0001_0',
        'analysis_input_00000_0_extra',
        'analysis_part_00000_0.unsafe',
        '$analysis_input_00000',
        'analysis_input_00000_basic_check',
        'analysis_basic_check_00000',
    ],
)
async def test_current_trace_contract_does_not_accept_unknown_or_out_of_range_payload_names(name):
    metadata, chunks = FakeDocumentCollection(), FakeDocumentCollection()
    store = MongoDocumentTraceStore(metadata, chunks)
    with pytest.raises(ValueError, match='未知文档留痕内容类型'):
        await store.write_payload('test-trace', 'test-user', name, {})
    assert not metadata.documents and not chunks.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing', 'checksum'])
async def test_current_checkpoint_bson_chunks_remain_scoped_and_integrity_checked(damage):
    metadata, chunks = FakeDocumentCollection(), FakeDocumentCollection()
    store = MongoDocumentTraceStore(metadata, chunks)
    store.CHUNK_BYTES = 64
    await store.create({'_id': 'test-trace', 'userId': 'test-user', 'subjectId': 'test-subject'})
    payload = {'done': True, 'rawData': {'basicInfo': {'name': '测试甲'}, 'text': TEXT * 8}}
    name = 'analysis_part_00000_0'
    await store.write_payload('test-trace', 'test-user', name, payload)
    reference = metadata.documents['test-trace']['payloads'][name]
    assert reference['chunks'] > 1
    rebuilt = MongoDocumentTraceStore(metadata, chunks)
    assert await rebuilt.read_payload('test-trace', 'test-user', 'test-subject', name) == payload
    for user, subject in [('other-user', 'test-subject'), ('test-user', 'other-subject')]:
        with pytest.raises(LookupError):
            await rebuilt.read_payload('test-trace', user, subject, name)
    chunk_id = f'test-trace:{reference["payloadId"]}:0'
    if damage == 'missing':
        del chunks.documents[chunk_id]
    else:
        chunks.documents[chunk_id]['data'] = b'corrupted'
    with pytest.raises(RuntimeError, match='分块缺失|校验失败'):
        await rebuilt.read_payload('test-trace', 'test-user', 'test-subject', name)
