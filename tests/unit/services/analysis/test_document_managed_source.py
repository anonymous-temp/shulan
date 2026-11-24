"""Actual managed Document commands and public Profile application, using BSON fakes."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.managed_source import create_document_managed_source_readers
from health_profile.document_source import ProfileDocumentSourceResolver
from health_profile.report_source import HealthReportSourceRecord
from health_profile.report_source import MongoHealthReportStore
from health_profile.source_archive import ProfileSourceMaterialArchive
from modules.analysis.application.output_profiles import DocumentSnapshot as ExtractorInput
from modules.analysis.application.output_profiles import normalize_report_source
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.managed_content import managed_public_content_v1
from modules.analysis.managed_source_contracts import MANAGED_REPORT_REVISION
from modules.analysis.managed_source_contracts import MANAGED_SOURCE_REVISION
from modules.analysis.managed_source_contracts import DocumentManagedSourceCommand
from modules.analysis.publication import PublicationConflictError
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.report.document_source import extract_intrinsic_report_source
from tests.unit._support.document_profile_composition.analysis.document_managed_source import item as item
from tests.unit._support.document_profile_composition.analysis.document_managed_source import restart as restart
from tests.unit.health_profile.test_public_contracts import visible
from tests.unit.services.analysis.test_document_complete_source import change_interpretation
from tests.unit.services.analysis.test_document_fresh_profile_composition import capture
from tests.unit.services.analysis.test_document_fresh_profile_composition import composition
from tests.unit.services.analysis.test_document_fresh_profile_composition import options
from tests.unit.services.analysis.test_document_material_reader import BsonCollection


async def applied(flow):
    assert await flow.profile.worker.recover_pending() == 1
    flow.profile.now[0] += timedelta(minutes=1)
    await flow.recovery.recover_pending()


async def ready():
    flow = await composition()
    plan_ref, captured, authorization = await capture(flow)
    await flow.fresh.authorize_fresh_source(authorization, scope=flow.profile.scope)
    await flow.admission.activate(
        scope=flow.scope, plan_ref=plan_ref, source_reader=flow.api, source_snapshot_id=captured.source_snapshot_ref
    )
    await flow.publisher.publish(
        scope=flow.scope, snapshot_id=captured.source_snapshot_ref, request_id='first', expected_prior_revision=None
    )
    await applied(flow)
    flow.command_records = options(BsonCollection())
    flow.archive = ProfileSourceMaterialArchive(
        report_store=MongoHealthReportStore(flow.collections['reports'], flow.collections['revisions']),
        publication_store=flow.profile.inbox.store,
        clock=lambda: flow.profile.now[0],
    )
    flow.runtime = restart(flow)
    flow.current_ref = captured.source_snapshot_ref
    return flow


def command(flow, bundle, *, request='update', prior=1, ref=None, operation='replace_base'):
    return DocumentManagedSourceCommand(
        flow.scope,
        flow.bundle.material.source_id,
        request,
        prior,
        ref or flow.current_ref,
        operation,
        (item(bundle),),
        MANAGED_SOURCE_REVISION,
    )


@pytest.mark.asyncio
async def test_base_correction_and_historical_receipt_survive_real_profile_retraction():
    flow = await ready()
    changed = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    selected = command(flow, changed)
    receipt = await flow.runtime.commands.execute(selected)
    assert receipt['publicationRevision'] == 2 and receipt['legacySourceRevision'] > 1
    assert receipt['publication']['state'] != 'applied'
    await applied(flow)
    assert await visible(flow.profile)
    assert not flow.env.flow.jobs.jobs  # No legacy queue or observation publication.
    assert flow.collections['reports'].documents and flow.collections['revisions'].documents
    flow.runtime = restart(flow)
    assert await flow.runtime.commands.execute(selected) == receipt
    await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=selected.source_id,
        source_revision=1,
        request_id='withdraw',
        expected_prior_revision=2,
    )
    await applied(flow)
    assert not await visible(flow.profile)
    assert await flow.runtime.commands.read_receipt(scope=flow.scope, command_id=selected.command_id) == receipt
    with pytest.raises(PublicationConflictError, match='withdrawn'):
        await flow.runtime.commands.execute(
            command(flow, changed, request='late', prior=3, ref=receipt['sourceSnapshotRef'])
        )


@pytest.mark.asyncio
async def test_repeated_same_base_is_noop_without_dropping_other_contributions():
    flow = await ready()
    supplement = await flow.runtime.commands.execute(command(flow, flow.bundle, operation='replace_report'))
    await applied(flow)
    repeated = await flow.env.api.prepare_bundle(
        scope=flow.scope, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/fresh.pdf'
    )
    assert repeated.understanding.snapshot_id == flow.bundle.understanding.snapshot_id
    assert repeated.receipt.trace_id != flow.bundle.receipt.trace_id
    before = await flow.runtime.lifecycle.read(scope=flow.scope, source_id=flow.bundle.material.source_id)
    result = await flow.runtime.commands.execute(
        command(flow, repeated, request='same-base', prior=2, ref=supplement['sourceSnapshotRef'])
    )
    assert result['status'] == 'unchanged' and result['publicationRevision'] == 2
    assert await flow.runtime.lifecycle.read(scope=flow.scope, source_id=flow.bundle.material.source_id) == before
    source = await flow.runtime.source_reader.read_source_observations(
        scope=flow.scope, snapshot_id=before.active_snapshot_ref
    )
    assert len(source['payload']['contributions']) == 2


@pytest.mark.asyncio
async def test_real_v16_preparation_is_reused_and_old_understanding_dependency_is_retired():
    flow = await ready()
    profile = flow.env.publisher.coordinator.profiles.get('report_source')
    flow.env.publisher.coordinator.profiles._profiles[profile.selection] = replace(
        profile, extractor=extract_intrinsic_report_source
    )
    prepared = await flow.runtime.reports.prepare(
        scope=flow.scope, inputs=(flow.item,), revision=MANAGED_REPORT_REVISION
    )
    assert prepared.payload['projection']['supplementaryHospitalData']
    assert not flow.collections['reports'].documents
    receipt = await flow.runtime.commands.execute(command(flow, flow.bundle, operation='replace_report'))
    assert receipt['projectionRef'] == prepared.snapshot_id
    await applied(flow)
    changed = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    assert changed.material.snapshot_id == flow.bundle.material.snapshot_id
    corrected = await flow.runtime.commands.execute(
        command(flow, changed, request='base', prior=2, ref=receipt['sourceSnapshotRef'])
    )
    await applied(flow)
    source = await flow.runtime.source_reader.read_source_observations(
        scope=flow.scope, snapshot_id=corrected['sourceSnapshotRef']
    )
    assert len(source['payload']['contributions']) == 1
    plan = await flow.runtime.commands.read_verified_plan(scope=flow.scope, plan_ref=corrected['commandId'])
    assert len(plan.retired) == 2
    evidence = [
        await flow.runtime.commands.records.require('managed_retirement', flow.scope, x.retirement_evidence_ref)
        for x in plan.retired
    ]
    assert any(x['reason'] == 'fixed_input_superseded' for x in evidence)


@pytest.mark.asyncio
async def test_three_corrections_keep_direct_retirement_history_and_allow_returning_to_old_content():
    flow = await ready()
    bundles = [
        await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value=value) for value in ('479', '480')
    ]
    prior_ref, commands, receipts = flow.current_ref, [], []
    for number, bundle in enumerate([*bundles, flow.bundle], 1):
        selected = command(flow, bundle, request=f'correction-{number}', prior=number, ref=prior_ref)
        receipt = await flow.runtime.commands.execute(selected)
        await applied(flow)
        prior_ref = receipt['sourceSnapshotRef']
        commands.append(selected)
        receipts.append(receipt)
    plan = await flow.runtime.commands.read_verified_plan(scope=flow.scope, plan_ref=commands[-1].command_id)
    assert len(plan.active) == 1 and len(plan.retired) == 3
    assert plan.retired[0].superseded_by_contribution_id is None
    assert plan.retired[1].superseded_by_contribution_id is None
    evidence = await flow.runtime.commands.records.require(
        'managed_retirement', flow.scope, plan.retired[0].retirement_evidence_ref
    )
    assert evidence['commandId'] == commands[0].command_id
    assert evidence['replacementHandle']['snapshot_ref'] == plan.retired[1].handle.snapshot_ref
    for selected, receipt in zip(commands, receipts):
        assert await flow.runtime.commands.execute(selected) == receipt


@pytest.mark.asyncio
async def test_explicit_report_retirement_does_not_reappear_on_base_correction(monkeypatch):
    flow = await ready()
    supplement = await flow.runtime.commands.execute(command(flow, flow.bundle, operation='replace_report'))
    await applied(flow)
    monkeypatch.setattr(
        flow.runtime.reports, 'prepare', AsyncMock(side_effect=AssertionError('retirement is not extraction'))
    )
    retired = await flow.runtime.commands.execute(
        command(
            flow, flow.bundle, request='retire', prior=2, ref=supplement['sourceSnapshotRef'], operation='retire_report'
        )
    )
    await applied(flow)
    changed = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    corrected = await flow.runtime.commands.execute(
        command(flow, changed, request='correct', prior=3, ref=retired['sourceSnapshotRef'])
    )
    await applied(flow)
    plan = await flow.runtime.commands.read_verified_plan(scope=flow.scope, plan_ref=corrected['commandId'])
    assert len(plan.active) == 1 and any(x.retirement_kind == 'retracted' for x in plan.retired)
    assert retired['archiveRef'] is None and retired['legacySourceRevision'] == supplement['legacySourceRevision']


@pytest.mark.asyncio
async def test_concurrent_corrections_use_one_publication_cas(monkeypatch):
    flow = await ready()
    one = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    two = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='480')
    reserve, entered, barrier = flow.env.ledger.reserve, [], asyncio.Event()

    async def synchronized(*args, **kwargs):
        entered.append(1)
        if len(entered) == 2:
            barrier.set()
        await barrier.wait()
        return await reserve(*args, **kwargs)

    monkeypatch.setattr(flow.env.ledger, 'reserve', synchronized)
    results = await asyncio.gather(
        flow.runtime.commands.execute(command(flow, one, request='one')),
        flow.runtime.commands.execute(command(flow, two, request='two')),
        return_exceptions=True,
    )
    assert sum(isinstance(x, dict) for x in results) == 1
    assert sum(isinstance(x, PublicationConflictError) for x in results) == 1
    state = await flow.env.ledger.read_source(flow.scope, flow.bundle.material.source_id)
    assert state['publicationRevision'] == 2
    await applied(flow)


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_at', ['archive', 'event', 'result'])
async def test_acknowledgement_loss_replays_fixed_history_and_event_once(monkeypatch, lost_at):
    flow = await ready()
    changed = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    selected = command(flow, changed)
    lost = False
    if lost_at == 'archive':
        actual = flow.archive.archive_source_material

        async def save(**kwargs):
            nonlocal lost
            result = await actual(**kwargs)
            if not lost:
                lost = True
                raise RuntimeError('lost acknowledgement')
            return result

        monkeypatch.setattr(flow.archive, 'archive_source_material', save)
    elif lost_at == 'event':
        actual = flow.runtime.publisher.publish

        async def publish(**kwargs):
            nonlocal lost
            result = await actual(**kwargs)
            if not lost:
                lost = True
                raise RuntimeError('lost acknowledgement')
            return result

        monkeypatch.setattr(flow.runtime.publisher, 'publish', publish)
    else:
        actual = flow.runtime.commands.records.put

        async def put(kind, *args):
            nonlocal lost
            result = await actual(kind, *args)
            if kind == 'managed_result' and not lost:
                lost = True
                raise RuntimeError('lost acknowledgement')
            return result

        monkeypatch.setattr(flow.runtime.commands.records, 'put', put)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        await flow.runtime.commands.execute(selected)
    flow.runtime = restart(flow)
    result = await flow.runtime.commands.execute(selected)
    assert result['publicationRevision'] == 2
    assert len(flow.collections['reports'].documents) == 1 and len(flow.collections['revisions'].documents) == 1
    assert not flow.env.flow.jobs.jobs
    assert (await flow.env.ledger.read_source(flow.scope, selected.source_id))['publicationRevision'] == 2


@pytest.mark.asyncio
async def test_changed_request_or_selection_is_rejected_and_pure_read_never_reextracts(monkeypatch):
    flow = await ready()
    selected = command(flow, flow.bundle, operation='replace_report')
    receipt = await flow.runtime.commands.execute(selected)
    await applied(flow)
    with pytest.raises(UnsupportedOutputVersionError):
        replace(selected, command_revision='latest')
    with pytest.raises(PublicationConflictError, match='changed input'):
        await flow.runtime.commands.execute(replace(selected, operation='replace_base'))
    monkeypatch.setattr(
        flow.runtime.reports.profiles, 'get', lambda *a: (_ for _ in ()).throw(AssertionError('no extractor'))
    )
    monkeypatch.setattr(
        flow.env.materials, 'read_original', AsyncMock(side_effect=AssertionError('no mutable original read'))
    )
    monkeypatch.setattr(
        flow.archive, 'archive_source_material', AsyncMock(side_effect=AssertionError('no archive write'))
    )
    assert await flow.runtime.commands.read_receipt(scope=flow.scope, command_id=selected.command_id) == receipt
    assert (
        await flow.runtime.reports.read(scope=flow.scope, snapshot_id=receipt['projectionRef'])
    ).snapshot_id == receipt['projectionRef']


@pytest.mark.asyncio
async def test_partial_report_preparation_is_available_but_never_archived_or_published():
    flow = await ready()
    partial = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, status='partial')
    prepared = await flow.runtime.reports.prepare(
        scope=flow.scope, inputs=(item(partial),), revision=MANAGED_REPORT_REVISION
    )
    assert prepared.payload['processingStatus'] == 'partial'
    with pytest.raises(DocumentCompleteSourceUnavailable, match='current active base'):
        await flow.runtime.commands.execute(command(flow, partial, operation='replace_report'))
    with pytest.raises(DocumentCompleteSourceUnavailable, match='partial or unavailable'):
        await flow.runtime.reports.archive_prepared(
            scope=flow.scope, projection_ref=prepared.snapshot_id, primary_bundle=partial, transition_ref='rejected'
        )
    assert not flow.collections['reports'].documents and not flow.collections['revisions'].documents
    assert (await flow.env.ledger.read_source(flow.scope, flow.bundle.material.source_id))['publicationRevision'] == 1


@pytest.mark.asyncio
async def test_real_v16_uses_frozen_public_observations_and_retains_private_archive_evidence(monkeypatch):
    flow = await ready()
    document = flow.env.flow.index_collection.documents[flow.bundle.receipt.document_id]
    trace = flow.env.flow.service._document_trace_store
    raw = await trace.read_payload(
        document['payloadRefs']['interpretation'], flow.scope.user_id, flow.scope.subject_id, 'interpretation'
    )
    raw.update(
        reportType='imaging',
        observations=[
            {
                'itemName': '影像结论',
                'resultValue': '肝脏脂肪浸润',
                'kind': 'assessment',
                'evidence': {'quote': '肝脏回声增强，临床结论已确认'},
            }
        ],
    )
    await trace.create({'_id': 'approved-assessment', **flow.scope.as_dict()})
    await trace.write_payload('approved-assessment', flow.scope.user_id, 'interpretation', raw)
    document['payloadRefs']['interpretation'] = 'approved-assessment'
    bundle = await flow.env.api.prepare_bundle(
        scope=flow.scope,
        selection=UNDERSTANDING_SELECTION,
        document_id=flow.bundle.receipt.document_id,
        resume_incomplete=False,
    )
    profile = flow.env.publisher.coordinator.profiles.get('report_source')
    flow.env.publisher.coordinator.profiles._profiles[profile.selection] = replace(
        profile, extractor=extract_intrinsic_report_source
    )
    metadata = bundle.material.payload['documentMetadata']
    legacy = HealthReportSourceRecord(
        bundle.material.source_id,
        flow.scope.user_id,
        flow.scope.subject_id,
        1,
        metadata['sourceFingerprint'],
        raw,
        'source_managed',
        None,
        flow.profile.now[0],
        flow.profile.now[0],
        metadata['documentId'],
        bundle.material.source_id,
    )
    public = legacy.public_result()
    for key in ('reportId', 'userId', 'subjectId', 'documentId'):
        public.pop(key, None)
    original = await flow.env.materials.read_original(
        scope=flow.scope, material_snapshot_ref=bundle.material.snapshot_id
    )
    expected_input = ExtractorInput(
        metadata['documentId'],
        public,
        bundle.material.payload['parsedDocument']['text'],
        metadata['fileName'],
        metadata['fileType'],
        metadata['contentSha256'],
        original,
    )
    expected = normalize_report_source(extract_intrinsic_report_source((expected_input,)), (expected_input,))
    import utils.report_content as changing_utility

    monkeypatch.setattr(changing_utility, 'report_public_content', lambda value: {'changed': True})
    monkeypatch.setattr(changing_utility, 'report_source_content', lambda value: {'changed': True})
    prepared = await flow.runtime.reports.prepare(
        scope=flow.scope, inputs=(item(bundle),), revision=MANAGED_REPORT_REVISION
    )
    assert prepared.payload['projection'] == expected
    assert prepared.payload['projection']['supplementaryHospitalData'][0]['examResult'] == '肝脏脂肪浸润'
    assert 'evidence' in bundle.understanding.payload['interpretation']['observations'][0]
    assert prepared.payload['manifest']['interpretationProjectionRevision'] == 'document-understanding-public.v1'


def test_frozen_public_processing_messages_do_not_follow_current_templates(monkeypatch):
    from modules.analysis.application import content_results

    raw = {'processing': {'status': 'partial', 'contentOutcome': 'interpretation_incomplete'}}
    expected = managed_public_content_v1(raw)
    monkeypatch.setitem(content_results.CONTENT_MESSAGES, 'interpretation_incomplete', 'future template')
    assert managed_public_content_v1(raw) == expected
    assert expected['processing']['warnings'] == ['本次解读未完成，已保留可确认的信息，可稍后重试。']


@pytest.mark.asyncio
async def test_duplicate_inputs_share_one_extraction_input_but_conflicting_fixed_inputs_are_rejected(monkeypatch):
    flow = await ready()
    profile = flow.env.publisher.coordinator.profiles.get('report_source')
    calls = []

    def extractor(inputs):
        calls.append(inputs)
        return extract_intrinsic_report_source(inputs)

    flow.env.publisher.coordinator.profiles._profiles[profile.selection] = replace(profile, extractor=extractor)
    prepared = await flow.runtime.reports.prepare(
        scope=flow.scope, inputs=(flow.item, flow.item), revision=MANAGED_REPORT_REVISION
    )
    assert len(calls) == 1 and len(calls[0]) == 1
    assert len(prepared.payload['manifest']['inputs']) == 2
    changed = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    with pytest.raises(DocumentCompleteSourceUnavailable, match='conflicting fixed inputs'):
        await flow.runtime.reports.prepare(
            scope=flow.scope, inputs=(flow.item, item(changed)), revision=MANAGED_REPORT_REVISION
        )
    assert len(calls) == 1 and not flow.collections['reports'].documents


@pytest.mark.asyncio
async def test_retraction_wins_while_correction_prepares_and_historical_archive_cannot_revive_it(monkeypatch):
    flow = await ready()
    changed = await change_interpretation(flow.env, flow.bundle, scope=flow.scope, value='479')
    publish = flow.runtime.publisher.publish

    async def withdrawn_before_publish(**kwargs):
        await flow.runtime.publisher.retract(
            scope=flow.scope,
            source_id=flow.bundle.material.source_id,
            source_revision=1,
            request_id='rescue',
            expected_prior_revision=1,
        )
        return await publish(**kwargs)

    monkeypatch.setattr(flow.runtime.publisher, 'publish', withdrawn_before_publish)
    with pytest.raises(PublicationConflictError):
        await flow.runtime.commands.execute(command(flow, changed))
    await applied(flow)
    state = await flow.runtime.lifecycle.read(scope=flow.scope, source_id=flow.bundle.material.source_id)
    assert state.status == 'withdrawn' and state.active_snapshot_ref is None
    assert not await visible(flow.profile)
    assert flow.collections['reports'].documents  # History is not an active contribution.


@pytest.mark.asyncio
async def test_managed_factory_has_no_io_and_enforces_shared_gate(monkeypatch):
    flow = await ready()
    for owner, methods in (
        (flow.env.ledger, ('read_source', 'reserve')),
        (flow.archive, ('archive_source_material',)),
        (flow.command_records, ('find_one', 'insert_one')),
    ):
        for name in methods:
            monkeypatch.setattr(owner, name, AsyncMock(side_effect=AssertionError('construction must be pure')))
    assert restart(flow).lifecycle is not None
    old = flow.env.api.legacy_publication.ledger
    monkeypatch.setattr(flow.env.api.legacy_publication, 'ledger', object())
    with pytest.raises(ValueError, match='share one source gate'):
        restart(flow)
    monkeypatch.setattr(flow.env.api.legacy_publication, 'ledger', old)


@pytest.mark.asyncio
async def test_readers_need_no_profile_commands_and_resolve_initial_and_later_sources(monkeypatch):
    flow = await ready()
    # The public constructor accepts no Profile provider and invokes no storage.
    with monkeypatch.context() as patch:
        for collection in (flow.command_records, flow.admission.records.collection):
            for name in ('find_one', 'insert_one'):
                patch.setattr(collection, name, AsyncMock(side_effect=AssertionError('pure reader construction')))
        readers = create_document_managed_source_readers(
            compatibility=flow.env.api,
            report_publications=flow.env.publisher,
            ledger=flow.env.ledger,
            admission_records=flow.admission.records.collection,
            command_records=flow.command_records,
        )
    assert not hasattr(readers.source_reader, 'execute') and not hasattr(readers.owner_fence_reader, 'activate')
    initial = await readers.source_reader.read_source_observations(
        scope=flow.profile.scope, snapshot_id=flow.current_ref
    )
    inventory = await readers.source_reader.read_source_inventory(
        scope=flow.profile.scope, snapshot_id=initial['payload']['coverage']['inventorySnapshotRef']['snapshotId']
    )
    fence = inventory['payload']['fence']
    fixed = await readers.owner_fence_reader.read_admission_fence(
        scope=flow.profile.scope, snapshot_ref=ProfileSourceSnapshotRefV1.model_validate(fence['ownerFenceRef'])
    )
    assert fixed['sourceId'] == flow.bundle.material.source_id
    # These exact read-only instances stay registered when commands are constructed
    # later. No post-construction resolver replacement is necessary.
    resolver = ProfileDocumentSourceResolver(readers.source_reader)
    flow.profile.inbox.document_source = resolver
    flow.profile.applier.resolvers[('document', COMPLETE_SOURCE_CONTRACT)] = resolver
    flow.profile.applier.resolvers[('document', RETRACTION_CONTRACT)] = resolver
    receipt = await flow.runtime.commands.execute(command(flow, flow.bundle, operation='replace_report'))
    await applied(flow)
    source = await readers.source_reader.read_source_observations(
        scope=flow.profile.scope, snapshot_id=receipt['sourceSnapshotRef']
    )
    assert len(source['payload']['contributions']) == 2
    await flow.runtime.publisher.retract(
        scope=flow.scope,
        source_id=flow.bundle.material.source_id,
        source_revision=1,
        request_id='withdraw-with-pure-reader',
        expected_prior_revision=2,
    )
    await applied(flow)
    assert not await visible(flow.profile)
    assert (
        await readers.source_reader.read_source_observations(scope=flow.profile.scope, snapshot_id=flow.current_ref)
        == initial
    )
