"""Real Report and Document projection support for offline Profile integration."""

import pymupdf

from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from adapters.analysis.basic_info import create_document_basic_info_source_readers
from adapters.analysis.materials import create_document_material_reader
from modules.analysis.basic_info_managed import DocumentCurrentManagedCommand
from modules.report.document_projection import REPORT_SOURCE_SELECTION
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document_profile import actual_analysis_composition
from tests.unit._support.current_document_profile import install_profile
from tests.unit._support.current_document_profile import settle

SAME_VALUE_ROWS = """
| 检验项目 | 检验结果 | 标本 | 方法 | 检验日期 |
| --- | --- | --- | --- | --- |
| pH | 7.1 | 粪便 | 试纸法 | 2026-11-12 |
| pH | 7.1 | 尿液 | 电极法 | 2026-11-12 |
"""


def page_bytes(title, *, png=False):
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), title)
        return page.get_pixmap().tobytes('png') if png else pdf.tobytes()


def wire_projection(flow):
    flow.readers = create_document_basic_info_source_readers(
        prepared=flow.prepared,
        ledger=flow.ledger,
        admission_records=flow.report_admissions,
        material_reader=create_document_material_reader(
            snapshots=flow.snapshots,
            snapshot_chunks=flow.snapshot_chunks,
            traces=flow.traces,
            trace_chunks=flow.trace_chunks,
        ),
    )
    install_profile(flow)
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )


async def composition(monkeypatch, models, *, png=False):
    text = REPORT_TEXT + SAME_VALUE_ROWS
    if png:
        text = '肠道菌群检测报告\n' + text
    flow = await actual_analysis_composition(
        monkeypatch, models, text=text, native_content=page_bytes('Synthetic primary', png=png)
    )
    # Keep the same durable Document collections when replacing runtime objects.
    flow.report_admissions = flow.readers.records.collection
    wire_projection(flow)
    return flow


async def projection(flow, request, *bundles):
    return await flow.readers.report_projections.prepare(
        scope=flow.scope,
        inputs=tuple(flow.prepared.input(bundle) for bundle in bundles),
        request_id=request,
        lifecycle_epoch=flow.epoch,
    )


async def admit(flow, primary, report=None):
    plan = await flow.runtime.admission.prepare_source_plan(
        scope=flow.scope,
        prepared=flow.prepared.input(primary),
        request_id='initial',
        lifecycle_epoch=flow.epoch,
        report_projection_ref=report.snapshot_id if report else None,
    )
    flow.capture = await flow.runtime.source.capture_source(scope=flow.scope, verified_plan_ref=plan)
    return flow.capture.source_snapshot_ref


async def source(flow, snapshot_id):
    return await flow.readers.source.read_source_observations(scope=flow.scope, snapshot_id=snapshot_id)


def report_contributions(source_data):
    return tuple(
        row
        for row in source_data['payload']['clinicalContributions']
        if row['implementationSelection']['implementationRevision'] == REPORT_SOURCE_SELECTION.implementation_revision
    )


def command(flow, primary, source_ref, revision, request, **changes):
    return DocumentCurrentManagedCommand(
        flow.scope, primary.material.source_id, request, flow.epoch, revision, source_ref, **changes
    )


async def apply(flow, request):
    result = await flow.runtime.managed.execute(request)
    await settle(flow)
    state = await flow.ledger.read_source(flow.scope, request.source_id)
    assert state['publicationRevision'] == state['appliedRevision'] == result['publicationRevision']
    return await source(flow, result['sourceSnapshotRef']), result


async def next_round(flow, bundle):
    # Treat this fixture's interpretation as retryable, preserving the earlier
    # archive and using the real Analysis recovery path for the next response.
    document_id = bundle.receipt.document_id
    flow.upstream.index_collection.documents[document_id].update(processingStatus='partial', processingRetryable=True)
    updated = await flow.prepared.prepare_bundle(scope=flow.scope, document_id=document_id)
    assert updated.receipt.document_id == document_id
    assert updated.facts.payload['interpretationRound'] == bundle.facts.payload['interpretationRound'] + 1
    return updated
