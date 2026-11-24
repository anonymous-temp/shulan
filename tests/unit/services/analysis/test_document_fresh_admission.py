"""Real Document gate/proof persistence with an explicitly synthetic Profile port."""

import asyncio
from copy import deepcopy
from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.source_admission import MongoDocumentAdmissionStore
from adapters.analysis.source_admission import MongoDocumentFormalHistoryReader
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationModeError
from modules.analysis.snapshots import digest
from modules.analysis.source_admission import PRESENCE_SECTIONS
from modules.analysis.source_admission import DocumentFreshSourceAdmission
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_complete_source import service
from tests.unit.services.analysis.test_document_material_reader import BsonCollection
from tests.unit.services.analysis.test_document_publication import evidence
from tests.unit.services.analysis.test_document_report_publication import environment
from tests.unit.services.analysis.test_document_report_publication import prepare
from tests.unit.services.analysis.test_document_report_publication import prohibit_reprocessing
from tests.unit.services.analysis.test_document_report_publication import publish
from tests.unit.services.analysis.test_document_snapshots import SCOPE


def synthetic_presence(source_id):
    canonical = {
        'contractVersion': 'profile-source-presence.v1',
        'scope': SCOPE.as_dict(),
        'sourceType': 'health_report',
        'sourceId': source_id,
        'status': 'absent',
        'sections': [
            {'kind': key, 'observedCount': 0, 'truncated': False, 'evidenceDigest': digest([])}
            for key in sorted(PRESENCE_SECTIONS)
        ],
    }
    return {**canonical, 'proofRef': 'profile_presence_' + digest(canonical), 'contentDigest': digest(canonical)}


def setup(env, bundle):
    # This older BSON fixture's cursor only serves material lookups. The history
    # query additionally needs Mongo's ordinary sort/to_list interface.
    env.commands.find = MethodType(MemoryMongoCollection.find, env.commands)
    records = MongoDocumentAdmissionStore(BsonCollection())
    presence = SimpleNamespace(read_absence=AsyncMock(return_value=synthetic_presence(bundle.material.source_id)))
    history = MongoDocumentFormalHistoryReader(
        compatibility_records=env.records,
        report_commands=env.commands,
        observations=env.flow.observations.collection,
        ledger=env.ledger,
        snapshots=env.api.understanding.snapshots,
    )
    admission = DocumentFreshSourceAdmission(
        compatibility=env.api,
        contributions=service(env),
        ledger=env.ledger,
        records=records,
        history=history,
        profile_presence=presence,
    )
    return admission, service(env, admission)


@pytest.mark.asyncio
async def test_real_fresh_gate_and_bson_plan_capture_then_pure_historical_read():
    env = environment()
    bundle, item = await prepare(env)
    admission, api = setup(env, bundle)
    plan_ref = await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    captured = await api.capture_source(scope=SCOPE, verified_plan_ref=plan_ref)
    plan = await admission.read_verified_plan(scope=SCOPE, plan_ref=plan_ref)
    owner_ref = plan.fence['ownerFenceRef']['snapshotId']
    owner = await admission.read_admission_fence(scope=SCOPE, snapshot_id=owner_ref)
    assert owner['preparedManifestRef'].startswith('document_prepared_manifest_')
    assert owner['activeLegacyWriters'] == owner['activeLegacyWorkers'] == 0
    assert 'publishersQuiesced' not in owner
    assert await admission.is_admission_fence_held(scope=SCOPE, snapshot_id=owner_ref, content_digest=digest(owner))
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    assert plan.fence['admissionKind'] == 'fresh'
    with pytest.raises(PublicationModeError):
        await env.ledger.switch_mode(SCOPE, bundle.material.source_id, target='legacy', evidence=evidence())
    prohibit_reprocessing(env)
    admission.history.read_absence = AsyncMock(side_effect=AssertionError('historical read must not rescan'))
    admission.profile_presence.read_absence.side_effect = AssertionError('historical read must not rescan Profile')
    env.ledger.read_source = AsyncMock(side_effect=AssertionError('historical read must not require current fence'))
    assert await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh') == plan_ref
    assert (await api.read_source_observations(scope=SCOPE, snapshot_id=captured.source_snapshot_ref))['payload'][
        'quality'
    ]['status'] == 'accepted'
    assert await api.read_source_inventory(scope=SCOPE, snapshot_id=captured.inventory_snapshot_ref)


@pytest.mark.asyncio
async def test_preexisting_ticket_rejects_fresh_and_old_computation_cannot_publish_after_fence():
    env = environment()
    bundle, item = await prepare(env)
    admission, _ = setup(env, bundle)
    await env.ledger.begin_legacy(SCOPE, bundle.material.source_id, 'old-writer')
    with pytest.raises(PublicationModeError, match='in flight'):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    assert (await env.ledger.read_source(SCOPE, bundle.material.source_id))['mode'] == 'legacy'
    admission.profile_presence.read_absence.assert_awaited_once()
    await env.ledger.finish_legacy(SCOPE, bundle.material.source_id, 'old-writer')
    computed, resume = asyncio.Event(), asyncio.Event()

    async def old_computation():
        computed.set()
        await resume.wait()
        return await env.api.publish_prepared(
            scope=SCOPE,
            understanding_snapshot_id=item.understanding_snapshot_ref,
            selection=item.selection,
            preparation_receipt=item.preparation_receipt,
        )

    old = asyncio.create_task(old_computation())
    await computed.wait()
    await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    resume.set()
    with pytest.raises(PublicationModeError, match='disabled'):
        await old
    assert not env.flow.collection.documents and not env.flow.jobs.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'history', ['legacy_receipt', 'report_command', 'observation', 'event', 'truncated', 'unknown']
)
async def test_history_or_unknown_never_manufactures_fresh_proof(history):
    env = environment()
    bundle, item = await prepare(env)
    admission, _ = setup(env, bundle)
    if history == 'legacy_receipt':
        await env.api.publish_prepared(
            scope=SCOPE,
            understanding_snapshot_id=item.understanding_snapshot_ref,
            selection=item.selection,
            preparation_receipt=item.preparation_receipt,
        )
    elif history == 'report_command':
        await publish(env, item)
    elif history == 'observation':
        await env.flow.observations.collection.insert_one(
            {'_id': 'orphan', 'canonicalReportId': bundle.material.source_id}
        )
    elif history == 'event':
        await env.ledger.intents.insert_one({'_id': 'orphan-intent', 'event': {'sourceId': bundle.material.source_id}})
    else:
        await env.records.insert_one({'_id': 'unknown', **SCOPE.as_dict(), 'kind': 'unknown', 'payload': {}})
    prior = await env.ledger.read_source(SCOPE, bundle.material.source_id)
    snapshot_count = len(env.snapshots.documents)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='absence is not proven'):
        await admission.prepare_fresh_plan(
            scope=SCOPE, prepared=item, request_id='fresh', limit=1 if history == 'truncated' else 1000
        )
    assert await env.ledger.read_source(SCOPE, bundle.material.source_id) == prior
    assert len(env.snapshots.documents) == snapshot_count
    admission.profile_presence.read_absence.assert_not_awaited()
    assert all(row['kind'] != 'owner_fence' for row in admission.records.collection.documents.values())


@pytest.mark.asyncio
async def test_concurrent_and_lost_ack_replays_share_one_fixed_owner_proof(monkeypatch):
    env = environment()
    bundle, item = await prepare(env)
    admission, api = setup(env, bundle)
    insert = admission.records.collection.insert_one
    failed = False

    async def lost_ack(row):
        nonlocal failed
        result = await insert(row)
        if row['kind'] == 'verified_plan' and not failed:
            failed = True
            raise RuntimeError('offline lost plan ACK')
        return result

    monkeypatch.setattr(admission.records.collection, 'insert_one', lost_ack)
    with pytest.raises(RuntimeError, match='lost plan ACK'):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    refs = await asyncio.gather(
        *(admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh') for _ in range(4))
    )
    assert len(set(refs)) == 1
    captured = await api.capture_source(scope=SCOPE, verified_plan_ref=refs[0])
    assert captured.plan_ref == refs[0]
    assert sum(row['kind'] == 'owner_fence' for row in admission.records.collection.documents.values()) == 1
    with pytest.raises(PublicationConflictError, match='another fresh admission'):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='different')


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['scope', 'sections', 'present', 'truncated', 'digest'])
async def test_invalid_profile_absence_precheck_does_not_change_legacy_mode(change):
    env = environment()
    bundle, item = await prepare(env)
    admission, _ = setup(env, bundle)
    raw = deepcopy(admission.profile_presence.read_absence.return_value)
    if change == 'scope':
        raw['scope']['userId'] = 'other'
    elif change == 'sections':
        raw['sections'].pop()
    elif change == 'present':
        raw['status'] = 'present'
    elif change == 'truncated':
        raw['sections'][0]['truncated'] = True
    else:
        raw['contentDigest'] = '0' * 64
    admission.profile_presence.read_absence.return_value = raw
    with pytest.raises(DocumentCompleteSourceUnavailable):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    assert await env.ledger.read_source(SCOPE, bundle.material.source_id) is None
    assert not any(row['kind'] == 'verified_plan' for row in admission.records.collection.documents.values())


@pytest.mark.asyncio
async def test_real_legacy_source_stays_usable_after_fresh_rejection():
    env = environment()
    bundle, item = await prepare(env)
    first = await publish(env, item)
    admission, _ = setup(env, bundle)
    prior = await env.ledger.read_source(SCOPE, bundle.material.source_id)
    with pytest.raises(DocumentCompleteSourceUnavailable, match='absence is not proven'):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='probe-old')
    assert await env.ledger.read_source(SCOPE, bundle.material.source_id) == prior
    replay = await publish(env, item)
    assert replay.legacy_source_revision == first.legacy_source_revision
    await env.ledger.begin_legacy(SCOPE, bundle.material.source_id, 'later-write')
    await env.ledger.finish_legacy(SCOPE, bundle.material.source_id, 'later-write')


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['history', 'presence', 'cancel'])
async def test_race_before_owner_proof_rolls_back_only_its_unissued_fence(failure):
    env = environment()
    bundle, item = await prepare(env)
    admission, _ = setup(env, bundle)
    original = admission.history.read_absence
    calls = 0

    async def changed(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert (await env.ledger.read_source(SCOPE, bundle.material.source_id))['mode'] == 'admission'
            if failure == 'cancel':
                raise asyncio.CancelledError
            if failure == 'history':
                return {'status': 'present'}
            admission.profile_presence.read_absence.return_value['status'] = 'present'
        return await original(**kwargs)

    admission.history.read_absence = changed
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else DocumentCompleteSourceUnavailable):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    status = await admission.read_status(scope=SCOPE, source_id=bundle.material.source_id)
    assert status['mode'] == 'legacy' and status['ownerProofIssued'] is False
    assert status['lastAborted']['fence']['fenceId'].startswith('document_fresh_fence_')
    assert all(row['kind'] != 'owner_fence' for row in admission.records.collection.documents.values())
    await env.ledger.begin_legacy(SCOPE, bundle.material.source_id, 'safe-legacy')


@pytest.mark.asyncio
async def test_owner_proof_ack_unknown_cannot_release_a_possible_profile_authorization(monkeypatch):
    env = environment()
    bundle, item = await prepare(env)
    admission, _ = setup(env, bundle)
    put = admission.records.put

    async def lost_ack(kind, *args):
        await put(kind, *args)
        if kind == 'owner_fence':
            raise RuntimeError('lost owner proof ACK')

    monkeypatch.setattr(admission.records, 'put', lost_ack)
    with pytest.raises(RuntimeError, match='lost owner proof'):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    status = await admission.read_status(scope=SCOPE, source_id=bundle.material.source_id)
    assert status['mode'] == 'admission' and status['ownerProofIssued'] is True
    assert await admission.recover_unissued(scope=SCOPE, prepared=item, request_id='fresh') is False
    with pytest.raises(PublicationModeError):
        await env.ledger.begin_legacy(SCOPE, bundle.material.source_id, 'must-fail')


@pytest.mark.asyncio
async def test_explicit_recovery_of_lost_gate_ack_requires_exact_request_and_unissued_state(monkeypatch):
    env = environment()
    bundle, item = await prepare(env)
    admission, _ = setup(env, bundle)
    update = env.ledger.sources.update_one

    async def lost_ack(query, value):
        result = await update(query, value)
        if value['$set'].get('mode') == 'admission':
            raise RuntimeError('lost gate ACK')
        return result

    monkeypatch.setattr(env.ledger.sources, 'update_one', lost_ack)
    with pytest.raises(RuntimeError, match='lost gate ACK'):
        await admission.prepare_fresh_plan(scope=SCOPE, prepared=item, request_id='fresh')
    with pytest.raises(DocumentCompleteSourceUnavailable, match='held fresh request'):
        await admission.recover_unissued(scope=SCOPE, prepared=item, request_id='wrong')
    assert await admission.recover_unissued(scope=SCOPE, prepared=item, request_id='fresh') is True
    assert (await admission.read_status(scope=SCOPE, source_id=bundle.material.source_id))['mode'] == 'legacy'
