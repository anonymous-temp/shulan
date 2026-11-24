"""Full-text extraction, durable extensions, and original-observation integration."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.document_index import MongoDocumentIndexStore
from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from health_profile.report_ingestion import HealthReportCandidateMapper
from health_profile.report_ingestion import HealthReportProfileProcessor
from health_profile.service import HealthProfileService
from modules.analysis.application.output_profiles import DocumentOutputCoordinator
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.record.application import QASessionManager
from tests.unit._support.document_profile_composition.analysis.document_workflow import extended_flow as extended_flow
from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture
from tests.unit.services.analysis.test_document_provenance import attach_source_evidence
from tests.unit.services.analysis.test_document_provenance import report_text
from tests.unit.services.analysis.test_document_reuse import request
from tests.unit.services.analysis.test_document_reuse import reuse_flow


def lab(value='468', date='2026-09-01', name='尿酸'):
    return {
        'reportType': 'lab',
        'reportTime': date,
        'status': 'abnormal',
        'aiSummary': '原文指标偏高',
        'observations': [{'itemName': name, 'resultValue': value, 'unit': 'umol/L', 'kind': 'measurement'}],
    }


def install_graph(flow, respond):
    async def invoke(input, config=None, *, context=None, **kwargs):
        flow.graph.calls.append({'input': deepcopy(input), 'context': context})
        result = respond(input['report_text'])
        return {
            **input,
            'content': json.dumps(attach_source_evidence(result, input['report_text']), ensure_ascii=False)
            if isinstance(result, dict)
            else result,
            'status': 'completed',
        }

    flow.graph.ainvoke = invoke


def use_report_sections(flow, reports):
    texts = [report_text(item) + '\n本次报告记录。' * 12 for item in reports]
    width = max(len(text) for text in texts)
    texts = [text.ljust(width) for text in texts]
    flow.client.result = {
        'success': True,
        'text': '\n'.join(texts),
        'data': {'layouts': [{'type': 'text', 'text': text} for text in texts]},
    }
    flow.interpreter._count_input_tokens = len
    flow.interpreter.MAX_ANALYZE_INPUT_TOKENS = width + 1


@pytest.mark.asyncio
async def test_middle_of_long_document_is_extracted_at_every_entrypoint():
    flow = reuse_flow()
    text = '开头\n' + 'a' * 14000 + '\n' + report_text(lab()) + '\n' + 'b' * 14000
    flow.client.result = {'success': True, 'text': text}
    install_graph(
        flow,
        lambda chunk: (
            lab()
            if '本次检测结果：尿酸' in chunk
            else {'reportType': 'unknown', 'status': 'pending', 'observations': []}
        ),
    )
    outcomes = [
        await flow.service.process_document(**request(source_type=name)) for name in ('analysis', 'record', 'report')
    ]
    assert len(flow.client.submit_calls) == 1
    assert all(item.text == text for item in outcomes)
    assert outcomes[0].report['observations'][0]['itemName'] == '尿酸'
    assert len(flow.graph.calls) == 1
    assert flow.graph.calls[0]['input']['report_text'] == text
    ranges = sorted(
        (payload['source']['start'], payload['source']['end'])
        for (_, name), payload in flow.traces.payloads.items()
        if name.startswith('analysis_input')
    )
    metrics = next(payload for (_, name), payload in flow.traces.payloads.items() if name == 'analysis_metrics')
    assert ranges[0][0] == 0 and ranges[-1][1] == metrics['preparedChars']
    assert all(a[1] == b[0] for a, b in zip(ranges, ranges[1:]))


@pytest.mark.asyncio
async def test_long_table_is_sent_as_raw_slices_and_model_returns_the_later_row():
    flow = reuse_flow()
    flow.interpreter._count_input_tokens = len
    flow.interpreter.MAX_ANALYZE_INPUT_TOKENS = 400
    header = '| 项目 | 结果 | 单位 |'
    text = header + '\n|---|---|---|\n' + '| 普通项目 | 1 | mmol/L |\n' * 15 + '| 跨段指标 | 4.2 | mmol/L |\n'
    flow.client.result = {'success': True, 'text': text}

    def respond(chunk):
        import re

        items = [
            {'itemName': name.strip(), 'resultValue': value, 'unit': unit.strip()}
            for name, value, unit in re.findall(r'^\| ([^|]+) \| ([\d.]+) \| ([^|]+) \|', chunk, re.MULTILINE)
        ]
        return {'reportType': 'lab', 'observations': items}

    install_graph(flow, respond)
    outcome = await flow.service.process_document(**request())
    assert any(item['itemName'] == '跨段指标' for item in outcome.report['observations'])
    assert ''.join(call['input']['report_text'] for call in flow.graph.calls) == text
    assert outcome.text == text


@pytest.mark.asyncio
async def test_mixed_dates_and_imaging_survive_merge_and_profile_mapping():
    flow = reuse_flow()
    image = {
        'reportType': 'imaging',
        'reportTime': '2026-09-05',
        'status': 'abnormal',
        'expansion': {'findings': '肝脏回声增强', 'diagnosis': '脂肪肝'},
    }
    use_report_sections(flow, [lab(), lab('420', '2026-09-03'), image])

    def respond(text):
        if '尿酸 420' in text:
            return lab('420', '2026-09-03')
        if '影像所见' in text:
            return image
        return lab()

    install_graph(flow, respond)
    outcome = await flow.service.process_document(**request())
    record = await flow.reports.get_report(outcome.report['reportId'])
    assert record.result['reportTime'] is None
    assert [item['observedAt'] for item in record.result['observations']] == ['2026-09-01', '2026-09-03']
    mapping = HealthReportCandidateMapper().map(record)
    assert len(mapping.candidates) == 4
    assert {candidate.fact_type for candidate in mapping.candidates} >= {'condition.finding', 'condition.diagnosis'}
    assert {candidate.source.observed_at.day for candidate in mapping.candidates} == {1, 3, 5}


@pytest.mark.asyncio
async def test_invalid_model_date_preserves_verified_result_without_assigning_an_invalid_date():
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': report_text(lab())}
    install_graph(flow, lambda _: lab(date=['invalid', 'date']))
    outcome = await flow.service.process_document(**request())
    assert outcome.report['observations'][0]['resultValue'] == '468'
    assert outcome.report['reportTime'] is None
    assert outcome.report['processing']['status'] == 'partial'
    assert flow.jobs.jobs


@pytest.mark.asyncio
async def test_undated_subreports_never_inherit_another_subreports_date():
    flow = extended_flow()
    image = {'reportType': 'imaging', 'expansion': {'findings': '无日期影像所见'}}
    use_report_sections(flow, [lab(), lab('420', None, '无日期指标'), image])

    def respond(text):
        if '无日期指标' in text:
            return lab('420', None, '无日期指标')
        if '影像所见' in text:
            return image
        return lab()

    install_graph(flow, respond)
    result = await flow.service.process_document(**request())
    record = await flow.reports.get_report(result.canonical_report_id)
    assert record.result['reportTime'] == '2026-09-01'
    mapping = HealthReportCandidateMapper().map(record)
    assert len(mapping.candidates) == 1
    rows = (await flow.observations.list_observations('user-1', 'subject-self'))['items']
    assert sum(row['dateStatus'] == 'unknown' for row in rows) == 2


@pytest.mark.asyncio
async def test_supplemental_rows_deduplicate_and_keep_the_first_source_pointer_on_retry():
    flow = extended_flow()
    base = await flow.service.process_document(**request())
    record = await flow.reports.get_report(base.canonical_report_id)
    a, b = {'name': '评分A', 'value': '68'}, {'name': '评分B', 'value': '70'}
    record = replace(
        record,
        source_revision=25,
        result={
            'reportTime': '2026-09-01',
            'reportSource': {'metrics': [a, b, a]},
        },
    )
    first = await flow.observations.capture(record)
    again = await flow.observations.capture(
        replace(
            record,
            result={
                **record.result,
                'reportSource': {'metrics': [b, a, a]},
            },
        )
    )
    assert len(first) == len(again) == 2
    assert {item['itemName']: item['sourcePointer'] for item in first} == {
        item['itemName']: item['sourcePointer'] for item in again
    }
    assert all(item['observedAt'] is None for item in first)
    mapping = HealthReportCandidateMapper().map(
        replace(
            record,
            result={
                **record.result,
                'ingestionObservations': first,
            },
        )
    )
    assert not mapping.candidates


@pytest.mark.asyncio
async def test_failed_chunk_resumes_without_repeating_successful_chunks_or_docmind():
    flow = extended_flow()
    use_report_sections(flow, [lab(), lab('420', '2026-09-03')])
    failed = True
    install_graph(
        flow,
        lambda text: (
            'invalid' if failed and '尿酸 420' in text else (lab('420', '2026-09-03') if '尿酸 420' in text else lab())
        ),
    )
    partial = await flow.service.process_document(**request())
    assert partial.report['processing']['status'] == 'partial'
    calls_after_partial = len(flow.graph.calls)
    failed = False
    recovered = await flow.service.process_document(**request())
    assert recovered.report['processing']['status'] == 'complete'
    assert len(flow.client.submit_calls) == 1
    assert len(flow.graph.calls) == calls_after_partial + 1
    refs = next(iter(flow.index_collection.documents.values()))['payloadRefs']
    assert 'analysis_part_00000' in refs and 'analysis_part_00001' in refs
    assert refs['analysis_part_00000'] != refs['analysis_part_00001']


@pytest.mark.asyncio
async def test_file_bytes_without_url_are_archived_hashed_and_reusable():
    flow = reuse_flow()
    first = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7\nlocal', file_name='本地报告.pdf')
    assert first.file_name == '本地报告.pdf'
    assert flow.downloader.download.await_count == 0
    second = await flow.service.process_document(user_id='user-1', document_id=first.document_id)
    assert second.text == first.text and second.content_sha256 == first.content_sha256
    assert len(flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_report_output_is_cached_and_queued_under_one_canonical_source():
    flow = extended_flow()
    flow.client.result = {
        'success': True,
        'text': (
            '报告日期：2026-09-01\n项目|检测结果|参考范围|结果评价\n'
            '菌群多样性|68|70-100|偏低\n'
            '报告日期：2024-11-02\n项目|检查结果|单位\n'
            '低密度脂蛋白胆固醇|3.48|mmol/L'
        ),
    }
    base = await flow.service.process_document(**request())
    first, second = await asyncio.gather(
        *(
            flow.service.process_document(
                user_id='user-1', document_id=base.document_id, output_profile='report_source'
            )
            for _ in range(2)
        )
    )
    assert first.structured_data == second.structured_data
    assert first.structured_data['sourceChecksum'] == base.content_sha256
    assert first.structured_data['sourceName'] == base.file_name
    assert first.structured_data['supplementaryHospitalData'][0]['testResultValue'] == '3.48 mmol/L'
    assert first.report == base.report
    assert flow.extractor.await_count == len(flow.client.submit_calls) == len(flow.graph.calls) == 1
    assert len({job.source_id for job in flow.jobs.jobs.values()}) == 1
    assert len(flow.jobs.jobs) == 2
    version = await flow.reports.get_revision(base.canonical_report_id, first.source_revision)
    assert {item['itemName'] for item in version.result['ingestionObservations']} == {
        '菌群多样性',
        '低密度脂蛋白胆固醇',
        'Faecalibacterium prausnitzii',
    }
    assert first.structured_data['taxa'][0]['latinName'] == 'Faecalibacterium prausnitzii'
    assert version.result['ingestionObservations'][-1]['unit'] == 'mmol/L'
    assert (await flow.reports.get_report(base.canonical_report_id)).source_revision == 1
    repository = FakeProfileRepository(ReportIngestionFixture.subject())
    processor = HealthReportProfileProcessor(store=flow.reports, repository=repository)
    for job in flow.jobs.jobs.values():
        await processor.process(job)
    assert any(fact.display_name == '菌群多样性' for fact in repository.facts)
    assert any(fact.display_name == '低密度脂蛋白胆固醇' for fact in repository.facts)


@pytest.mark.parametrize('legacy_first', [True, False])
@pytest.mark.parametrize('allowed_count, materialized', [(0, True), (0, False), (1, False)])
@pytest.mark.asyncio
async def test_reused_gated_report_backfills_omitted_rows_without_ocr_or_model(
    legacy_first,
    allowed_count,
    materialized,
):
    flow = extended_flow()
    flow.source._observation_store = None
    first = await flow.service.process_document(**request())
    flow.source._observation_store = flow.observations
    # Seed the publication and archive produced by the old release.
    canonical = next(row for row in flow.collection.documents if row['reportId'] == first.canonical_report_id)
    pointers = ['/expansion/0'] if allowed_count else []
    canonical['verifiedObservationPointers'] = pointers
    if materialized:
        canonical['ingestionObservations'] = []
    archive = flow.traces.payloads[(first.archive_trace_id, 'interpretation')]
    archive['verifiedObservationPointers'] = pointers
    repository = FakeProfileRepository(ReportIngestionFixture.subject())
    processor = HealthReportProfileProcessor(store=flow.reports, repository=repository)
    old_job = next(iter(flow.jobs.jobs.values()))
    if legacy_first:
        assert (await processor.process(old_job)).fact_count == allowed_count

    kwargs = {'user_id': 'user-1', 'document_id': first.document_id}
    second = await flow.service.process_document(**kwargs)
    third = await flow.service.process_document(**kwargs)
    assert second.source_revision == third.source_revision != first.source_revision
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == 1
    revision = await flow.reports.get_revision(first.canonical_report_id, second.source_revision)
    assert len(revision.result['ingestionObservations']) == 2 - allowed_count
    assert canonical.get('ingestionObservations') == ([] if materialized else None)
    job = next(job for job in flow.jobs.jobs.values() if job.source_revision == second.source_revision)
    assert (await processor.process(job)).fact_count == 2 - allowed_count
    if not legacy_first:
        assert (await processor.process(old_job)).fact_count == allowed_count
    assert (await processor.process(job)).fact_count == 0
    assert (await processor.process(old_job)).fact_count == 0
    assert len(repository.facts) == 2
    assert len({fact.display_name for fact in repository.facts}) == 2
    raw_rows = (await flow.observations.list_observations('user-1', 'subject-self'))['items']
    assert len(raw_rows) == 2


@pytest.mark.asyncio
async def test_new_extractor_version_does_not_duplicate_source_observations():
    flow = extended_flow()
    first = await flow.service.process_document(**request(output_profile='report_source'))
    registry = DocumentOutputProfiles()
    registry.register_report_source(flow.extractor, version='test-report-source-v2')
    flow.service.output_profiles = registry
    flow.service._document_outputs = DocumentOutputCoordinator(flow.service, flow.output_index, registry)
    second = await flow.service.process_document(
        user_id='user-1', document_id=first.document_id, output_profile='report_source'
    )
    assert first.source_revision != second.source_revision
    revision = await flow.reports.get_revision(second.canonical_report_id, second.source_revision)
    assert revision.result['ingestionObservations'] == []
    assert len(flow.client.submit_calls) == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_profile_queue_outage_retries_only_delivery():
    flow = extended_flow()
    flow.jobs.fail_create = True
    first = await flow.service.process_document(**request(output_profile='report_source'))
    assert first.ingestion_status == 'pending'
    flow.jobs.fail_create = False
    await flow.source.recover_pending()
    second = await flow.service.process_document(
        user_id='user-1', document_id=first.document_id, output_profile='report_source'
    )
    assert second.ingestion_status == 'queued'
    assert len(flow.jobs.jobs) == 2
    assert flow.extractor.await_count == len(flow.client.submit_calls) == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_internal_type_hint_is_versioned_and_does_not_replace_default_snapshot():
    flow = extended_flow()
    original = await flow.service.process_document(**request())
    hinted = await flow.service.process_document(
        user_id='user-1', document_id=original.document_id, report_type_hint='lab'
    )
    await flow.service.process_document(user_id='user-1', document_id=original.document_id, report_type_hint='lab')
    assert '报告类型提示：lab' in flow.graph.calls[-1]['input']['report_text']
    assert len(flow.graph.calls) == 2
    assert hinted.report == original.report
    assert hinted.source_revision > 1


@pytest.mark.asyncio
async def test_ordered_groups_reuse_raw_documents_and_keep_all_input_references():
    flow = extended_flow()
    a = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7\na', file_name='a.pdf')
    b = await flow.service.process_document(user_id='user-1', content=b'%PDF-1.7\nb', file_name='b.pdf')
    first = await flow.service.process_documents(
        [{'documentId': a.document_id}, {'documentId': b.document_id}], user_id='user-1'
    )
    again = await flow.service.process_documents(
        [{'documentId': a.document_id}, {'documentId': b.document_id}], user_id='user-1'
    )
    swapped = await flow.service.process_documents(
        [{'documentId': b.document_id}, {'documentId': a.document_id}], user_id='user-1'
    )
    assert first.extraction_id == again.extraction_id != swapped.extraction_id
    assert len(first.documents) == 2
    assert first.structured_data['sourceChecksum'] is None
    assert len(first.structured_data['sourceDocuments']) == 2
    assert flow.extractor.await_count == 2 and len(flow.client.submit_calls) == 2


@pytest.mark.asyncio
async def test_undated_values_are_queryable_with_owner_bound_pagination_and_memory():
    flow = extended_flow()
    flow.client.result = {'success': True, 'text': report_text(lab(date=None))}
    install_graph(flow, lambda _: lab(date=None))
    first = await flow.service.process_document(**request())
    base = await flow.reports.get_report(first.canonical_report_id)
    await flow.observations.capture(replace(base, result=lab('999', None, '另一项目')))
    page = await flow.observations.list_observations('user-1', 'subject-self', limit=1)
    assert page['nextCursor']
    second = await flow.observations.list_observations('user-1', 'subject-self', limit=1, cursor=page['nextCursor'])
    assert second['items'][0]['observationId'] != page['items'][0]['observationId']
    with pytest.raises(ValueError):
        await flow.observations.list_observations('other', 'subject-self', cursor=page['nextCursor'])
    assert not (await flow.observations.list_observations('other', 'subject-self'))['items']
    assert all(row['dateStatus'] == 'unknown' and row['observedAt'] is None for row in page['items'])
    repository = FakeProfileRepository(ReportIngestionFixture.subject())
    service = HealthProfileService(repository, document_observation_store=flow.observations)
    result = await service.get_profile('user-1', subject_id='subject-self')
    assert result['documentObservations']['items'] and not result['indicatorGroups']
    memory = RepositoryHealthProfileChatMemoryProvider(repository, document_observation_store=flow.observations)
    repository.get_self_subject = AsyncMock(return_value=ReportIngestionFixture.subject())
    repository.list_subjects = AsyncMock(return_value=[])
    assert 'unknown' in await memory.build_context('user-1')


def test_large_first_attachment_cannot_hide_later_attachments_from_qa_context():
    first = {
        'title': '首份报告',
        'status': 'normal',
        'observations': [{'itemName': f'项目{i}', 'resultValue': '10', 'flag': '正常'} for i in range(500)],
    }
    first['expansion'] = first['observations']
    second = {'title': '第二份影像', 'status': 'abnormal', 'expansion': {'findings': '肝脏回声增强'}}
    context, status = QASessionManager._formatHealthReports([first, second])
    assert '首份报告' in context and '第二份影像' in context and '肝脏回声增强' in context
    assert status == 'abnormal'
    assert len(first['observations']) == 500


@pytest.mark.asyncio
async def test_subreports_in_one_chunk_keep_their_dates_and_do_not_duplicate_parent_items():
    flow = reuse_flow()
    child = lab()
    raw = {
        **lab(date='2026-09-01'),
        'reportType': 'checkup',
        'sections': [
            child,
            {
                'reportType': 'imaging',
                'reportTime': '2026-09-03',
                'status': 'abnormal',
                'expansion': {'findings': '肝脏回声增强', 'diagnosis': '脂肪肝'},
            },
        ],
    }
    texts = [report_text(section) for section in raw['sections']]
    flow.client.result = {
        'success': True,
        'text': '\n'.join(texts),
        'data': {'layouts': [{'type': 'text', 'text': text} for text in texts]},
    }
    install_graph(flow, lambda _: raw)
    result = await flow.service.process_document(**request())
    assert len(result.report['observations']) == 1
    assert result.report['observations'][0]['observedAt'] == '2026-09-01'
    record = await flow.reports.get_report(result.canonical_report_id)
    mapping = HealthReportCandidateMapper().map(record)
    assert len(mapping.candidates) == 3
    assert {candidate.source.observed_at.day for candidate in mapping.candidates} == {1, 3}


@pytest.mark.asyncio
async def test_extension_save_failure_reuses_the_completed_extractor_output():
    flow = extended_flow()
    original = flow.source.save_revision
    flow.source.save_revision = AsyncMock(side_effect=RuntimeError('storage unavailable'))
    with pytest.raises(RuntimeError):
        await flow.service.process_document(**request(output_profile='report_source'))
    flow.source.save_revision = original
    result = await flow.service.process_document(**request(output_profile='report_source'))
    assert result.structured_data['metrics']
    assert flow.extractor.await_count == len(flow.client.submit_calls) == len(flow.graph.calls) == 1


@pytest.mark.asyncio
async def test_separate_output_coordinators_share_the_durable_result_after_restart():
    flow = extended_flow()
    base = await flow.service.process_document(**request())
    other_registry = DocumentOutputProfiles()
    other = AsyncMock(return_value=deepcopy(flow.extractor.return_value))
    other_registry.register_report_source(other, version='test-report-source-v1')
    restarted = DocumentOutputCoordinator(
        flow.service,
        MongoDocumentIndexStore(flow.output_index._collection, identity_key='extractionKey'),
        other_registry,
    )
    restarted.POLL_SECONDS = 0.005
    outcomes = await asyncio.gather(
        flow.service._document_outputs.process(
            [base], user_id='user-1', subject_id='subject-self', output_profile='report_source'
        ),
        restarted.process([base], user_id='user-1', subject_id='subject-self', output_profile='report_source'),
    )
    assert outcomes[0].extraction_id == outcomes[1].extraction_id
    assert flow.extractor.await_count + other.await_count == 1
    await restarted.process([base], user_id='user-1', subject_id='subject-self', output_profile='report_source')
    assert flow.extractor.await_count + other.await_count == 1


@pytest.mark.asyncio
async def test_cancelled_extension_waiter_does_not_cancel_processing_or_publish_an_incomplete_result():
    entered, release = asyncio.Event(), asyncio.Event()

    async def extractor(snapshots):
        entered.set()
        await release.wait()
        return {'metrics': [{'name': '原报告评分', 'value': '68'}]}

    flow = extended_flow(extractor)
    base = await flow.service.process_document(**request())
    waiter = asyncio.create_task(
        flow.service.process_document(
            user_id='user-1',
            document_id=base.document_id,
            output_profile='report_source',
        )
    )
    await entered.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert next(iter(flow.output_index._collection.documents.values()))['status'] == 'running'
    release.set()
    await asyncio.gather(*tuple(flow.service._document_outputs.tasks))
    cached = await flow.service.process_document(
        user_id='user-1', document_id=base.document_id, output_profile='report_source'
    )
    assert cached.output_reused
    reference = next(iter(flow.output_index._collection.documents.values()))['payloadRefs']['output_result']
    assert flow.traces.documents[reference]['status'] == 'completed'
