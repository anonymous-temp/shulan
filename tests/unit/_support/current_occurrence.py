"""Fixture helpers for current fixed original occurrence integration."""

from adapters.analysis.basic_info import create_document_basic_info_source_readers
from adapters.analysis.materials import create_document_material_reader
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_report_projection import SAME_VALUE_ROWS
from tests.unit._support.current_report_projection import page_bytes


def readers_for(flow):
    return create_document_basic_info_source_readers(
        prepared=flow.prepared,
        ledger=flow.ledger,
        admission_records=flow.report_admissions,
        material_reader=create_document_material_reader(
            snapshots=flow.snapshots,
            snapshot_chunks=flow.snapshot_chunks,
            traces=flow.traces,
            trace_chunks=flow.trace_chunks,
        ),
        verify_original_occurrences=True,
    )


async def page(flow, name):
    if name != 'A':
        flow.upstream.downloader.download.return_value = page_bytes(f'Synthetic page {name}', png=True)
        flow.upstream.client.result = {
            'success': True,
            'text': '续页 检测结果\n' + REPORT_TEXT + SAME_VALUE_ROWS,
        }
    return await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id=f'https://example.test/occurrence-{name}.png', file_type='png'
    )
