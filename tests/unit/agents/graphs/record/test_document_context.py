"""Record document consumer contract, independent of shared processor internals."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from adapters.record.documents import LegacyRecordDocumentAdapter
from adapters.record.documents import legacy_record_report_id
from modules.analysis.application.documents import ProcessedHealthReport
from modules.record.document_context import RecordDocumentContextV1
from modules.record.document_context import document_processing_summary

from .test_dynamic_record import setup_manager


@pytest.mark.asyncio
async def test_legacy_adapter_preserves_report_projection_and_reuse_arguments():
    raw = {
        'reportId': 'report-1',
        'documentId': 'document-1',
        'reportType': 'lab',
        'observations': [{'itemName': 'marker', 'resultValue': '1'}],
        'expansion': [{'content': ['source detail']}],
        'processing': {'status': 'partial', 'retryable': True, 'issues': ['provider diagnostic']},
        'otherLegacyField': {'details': ['retained']},
    }
    expected = deepcopy(raw)
    result = ProcessedHealthReport(
        report=raw,
        text='raw provider text',
        document_id='document-1',
        trace_id='trace-1',
        ingestion_status='queued',
        ingestion_job_id=None,
        reused=True,
        source_revision=7,
    )
    processor = SimpleNamespace(
        supports_document_reuse=True,
        process=AsyncMock(),
        process_document=AsyncMock(return_value=result),
    )
    ready = AsyncMock()
    settled = {'document-0'}
    context = await LegacyRecordDocumentAdapter(processor=processor).process_attachment(
        user_id='u',
        session_id='s',
        file_index=2,
        attachment={'documentId': 'document-1'},
        settled_document_ids=settled,
        on_document_ready=ready,
    )
    processor.process_document.assert_awaited_once_with(
        file_id=None,
        document_id='document-1',
        file_type=None,
        user_id='u',
        source_type='record',
        source_id='s',
        session_id='s',
        file_index=2,
        _settled_document_ids=settled,
        on_document_ready=ready,
    )
    processor.process.assert_not_awaited()
    raw['expansion'][0]['content'].append('provider mutation')
    snapshot = context.to_checkpoint()
    assert snapshot == {
        'report': {key: value for key, value in expected.items() if key != 'documentId'},
        'document': {'fileIndex': 2, 'documentId': 'document-1', 'reportId': 'report-1', 'reused': True},
        'processing': {
            'fileIndex': 2,
            'status': 'partial',
            'message': document_processing_summary(2, {'status': 'partial'})['message'],
            'retryable': True,
            'documentId': 'document-1',
            'sourceRevision': 7,
        },
    }
    snapshot['report']['expansion'][0]['content'].append('checkpoint mutation')
    assert context.report['expansion'][0]['content'] == ['source detail']
    assert context.consumer_projection_revision == 'record-document-context.v1'


@pytest.mark.asyncio
async def test_plain_legacy_processor_remains_lazy_and_preserves_source_identity():
    report = {'reportId': 'r', 'documentId': 'legacy-field', 'custom': {'values': [1]}}
    processor = SimpleNamespace(process=AsyncMock(return_value=report))
    factory = Mock(return_value=processor)
    adapter = LegacyRecordDocumentAdapter(processor_factory=factory)
    factory.assert_not_called()
    contexts = []
    for session_id in ('first-session', 'retry-session'):
        contexts.append(
            await adapter.process_attachment(
                user_id='u',
                session_id=session_id,
                file_index=0,
                attachment={'url': 'https://example.test/report.pdf', 'fileType': 'pdf'},
                settled_document_ids=set(),
            )
        )
    factory.assert_called_once_with()
    calls = processor.process.await_args_list
    assert calls[0].kwargs == {
        'file_id': 'https://example.test/report.pdf',
        'file_type': 'pdf',
        'user_id': 'u',
        'source_type': 'record',
        'source_id': 'first-session',
        'session_id': 'first-session',
        'file_index': 0,
        'subject_id': None,
        'report_id': legacy_record_report_id(user_id='u', file_id='https://example.test/report.pdf'),
    }
    assert calls[1].kwargs['report_id'] == calls[0].kwargs['report_id']
    assert calls[1].kwargs['source_id'] == 'retry-session'
    assert contexts[0].document is None
    assert contexts[0].report == report
    report['custom']['values'].append(2)
    assert contexts[0].report['custom']['values'] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize('factory,error', [(None, RuntimeError), (lambda: object(), TypeError)])
async def test_missing_or_invalid_legacy_processor_fails_without_fabricated_context(factory, error):
    with pytest.raises(error):
        await LegacyRecordDocumentAdapter(processor_factory=factory).process_attachment(
            user_id='u',
            session_id='s',
            file_index=0,
            attachment={'url': 'https://example.test/a.pdf'},
            settled_document_ids=set(),
        )


@pytest.mark.asyncio
async def test_legacy_processor_rejects_document_reference_without_processing():
    processor = SimpleNamespace(process=AsyncMock())
    with pytest.raises(ValueError):
        await LegacyRecordDocumentAdapter(processor=processor).process_attachment(
            user_id='u',
            session_id='s',
            file_index=0,
            attachment={'documentId': 'document-1'},
            settled_document_ids=set(),
        )
    processor.process.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_port_preserves_attachment_order_deduplication_and_saved_checkpoint():
    release_first = asyncio.Event()
    calls = []
    contexts = {
        0: RecordDocumentContextV1(
            report={'reportId': 'r0', 'data': ['first']},
            document={'fileIndex': 0, 'documentId': 'shared', 'reportId': 'r0', 'reused': False},
            processing=document_processing_summary(0, None, document_id='shared'),
        ),
        1: RecordDocumentContextV1(
            report={'reportId': 'r0', 'data': ['duplicate']},
            document={'fileIndex': 1, 'documentId': 'shared', 'reportId': 'r0', 'reused': True},
            processing=document_processing_summary(1, {'status': 'partial'}, document_id='shared'),
        ),
    }

    async def process_attachment(**kwargs):
        calls.append(kwargs)
        index = kwargs['file_index']
        if index == 0:
            await release_first.wait()
        else:
            release_first.set()
        return contexts[index]

    port = SimpleNamespace(process_attachment=process_attachment)
    manager, _, _, _ = setup_manager(record_document_port=port)
    saved = {
        'report': {'reportId': 'saved', 'data': ['persisted']},
        'document': {'fileIndex': 2, 'documentId': 'saved-doc', 'reportId': 'saved', 'reused': False},
        'processing': document_processing_summary(2, None, document_id='saved-doc'),
    }
    attempt = SimpleNamespace(
        session={'qaCreation': {}},  # A historical receipt without a consumer revision.
        data={'files': {'2': saved}, 'references': {'0': 'resume-doc'}},
        checkpoint=AsyncMock(),
    )
    documents, processing = [], []
    reports = await manager._processHealthReports(
        user_id='u',
        session_id='s',
        file_list=[{'url': f'https://example.test/{index}.pdf'} for index in range(3)],
        documents=documents,
        document_processing=processing,
        creation_attempt=attempt,
    )
    assert reports == [{'reportId': 'r0', 'data': ['first']}, {'reportId': 'saved', 'data': ['persisted']}]
    assert [item['fileIndex'] for item in documents] == [0, 1, 2]
    assert [item['status'] for item in processing] == ['complete', 'partial', 'complete']
    assert [call['file_index'] for call in calls] == [0, 1]
    assert calls[0]['attachment'] == {'documentId': 'resume-doc'}
    assert calls[0]['settled_document_ids'] == {'saved-doc'}
    await calls[0]['on_document_ready']('resume-doc')
    attempt.checkpoint.assert_any_await('processing_reports', documentReference=(0, 'resume-doc'))
    attempt.checkpoint.assert_any_await('processing_reports', completedFile=(0, contexts[0].to_checkpoint()))
    reports[1]['data'].append('caller mutation')
    assert saved['report']['data'] == ['persisted']
