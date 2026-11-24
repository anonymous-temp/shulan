"""Real Document capture -> public Profile resolver/Inbox; admission remains synthetic.

These checks prove the shared material/event contracts and revision semantics.
Fresh-source owner fencing/absence proof has its own real-provider acceptance tests.
"""

from dataclasses import replace
from datetime import timedelta

import pytest

from adapters.analysis.complete_source import ProfileDocumentCompleteSourceReader
from adapters.analysis.publication_receiver import ProfileDocumentPublicationReceiver
from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceSnapshotRefV1
from health_profile.document_source import ProfileDocumentSourceResolver
from modules.analysis.complete_source_publication import DocumentCompleteSourcePublisher
from modules.analysis.publication import DocumentPublisher
from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
from modules.analysis.publication_recovery import DocumentPublicationRecovery
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.report.document_source import extract_intrinsic_report_source
from tests.unit._support.current_document import SyntheticAdmission
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_public_contracts import visible
from tests.unit.services.analysis.test_document_complete_source import change_interpretation
from tests.unit.services.analysis.test_document_complete_source import plan
from tests.unit.services.analysis.test_document_complete_source import service
from tests.unit.services.analysis.test_document_publication import evidence
from tests.unit.services.analysis.test_document_report_publication import environment
from tests.unit.services.analysis.test_document_report_publication import prepare
from tests.unit.services.analysis.test_document_report_publication import prohibit_reprocessing
from tests.unit.services.analysis.test_document_report_publication import publish
from tests.unit.services.analysis.test_document_snapshots import SCOPE


def public_ref(captured):
    return ProfileSourceSnapshotRefV1(
        provider='document', contract_version=COMPLETE_SOURCE_CONTRACT, snapshot_id=captured.source_snapshot_ref
    )


@pytest.mark.asyncio
async def test_real_base_and_v16_neutral_captures_match_public_profile_resolver():
    env = environment(extractor=extract_intrinsic_report_source)
    bundle, item = await prepare(env)
    published = await publish(env, item)
    admission = SyntheticAdmission()
    api = service(env, admission)
    base = await api.capture_base_contribution(scope=SCOPE, understanding_snapshot_id=bundle.understanding.snapshot_id)
    supplement = await api.capture_legacy_report_contribution(
        scope=SCOPE, publication_receipt_ref=published.receipt_ref
    )
    admission.plans['verified-plan'] = plan(bundle, [base, supplement], legacy=published.legacy_source_revision)
    captured = await api.capture_source(scope=SCOPE, verified_plan_ref='verified-plan')
    prohibit_reprocessing(env)
    resolver = ProfileDocumentSourceResolver(ProfileDocumentCompleteSourceReader(api))
    ready = await resolver.inspect(
        public_ref(captured),
        scope=ProfileScope(user_id=SCOPE.user_id, subject_id=SCOPE.subject_id),
        source_id=bundle.material.source_id,
        content_digest=captured.source_content_digest,
    )
    assert ready.legacy_source_revision == published.legacy_source_revision
    assert len(ready.contribution_ids) == 2


@pytest.mark.asyncio
async def test_real_bundle_correction_same_material_revision_new_digest_and_publication_revision():
    profile = await setup_flow(authorize=False)
    scope = DocumentScope(profile.scope.user_id, profile.scope.subject_id)
    env = environment()
    first_bundle = await env.api.prepare_bundle(
        scope=scope, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/source.pdf'
    )
    second_bundle = await change_interpretation(env, first_bundle, scope=scope, value='479')
    admission = SyntheticAdmission()
    api = service(env, admission)
    captures = []
    for number, bundle in enumerate((first_bundle, second_bundle), 1):
        handle = await api.capture_base_contribution(
            scope=scope, understanding_snapshot_id=bundle.understanding.snapshot_id
        )
        key = f'plan-{number}'
        admission.plans[key] = replace(plan(bundle, [handle], ref=key), scope=scope)
        captures.append(await api.capture_source(scope=scope, verified_plan_ref=key))
    first, second = captures
    assert first_bundle.material.snapshot_id == second_bundle.material.snapshot_id
    assert first.source_content_digest != second.source_content_digest
    resolver = ProfileDocumentSourceResolver(ProfileDocumentCompleteSourceReader(api))
    profile.inbox.document_source = resolver
    profile.applier.resolvers[('document', COMPLETE_SOURCE_CONTRACT)] = resolver
    profile.applier.resolvers[('document', RETRACTION_CONTRACT)] = resolver
    source_id = first_bundle.material.source_id
    authorization = profile.authorization.model_copy(
        update={
            'source_id': source_id,
            'material_contract_version': COMPLETE_SOURCE_CONTRACT,
            'material_snapshot_ref': public_ref(first),
            'material_content_digest': first.source_content_digest,
            'processing_revision': resolver.VERSION,
            'legacy_source_intents_retired': True,
            'legacy_quiescence_evidence_ref': 'actual-fence',
        }
    )
    await profile.inbox.authorize_cutover(authorization, scope=profile.scope)
    await env.ledger.switch_mode(scope, source_id, target='events', evidence=evidence())
    recovery = DocumentPublicationRecovery(
        ledger=env.ledger, receiver=ProfileDocumentPublicationReceiver(profile.inbox), clock=lambda: profile.now[0]
    )
    publisher = DocumentCompleteSourcePublisher(
        source_reader=api, snapshots=api.snapshots, ledger=env.ledger, recovery=recovery
    )
    lifecycle = DocumentPublicationLifecycleReader(ledger=env.ledger, snapshots=api.snapshots)
    prohibit_reprocessing(env)
    old = DocumentPublisher(snapshots=api.snapshots, ledger=env.ledger, receiver=recovery.receiver)
    with pytest.raises(ValueError, match='fixed facts'):
        await old.publish(
            scope=scope, snapshot_id=first.source_snapshot_ref, request_id='wrong-port', expected_prior_revision=None
        )
    for number, capture in enumerate(captures, 1):
        received = await publisher.publish(
            scope=scope,
            snapshot_id=capture.source_snapshot_ref,
            request_id=f'revision-{number}',
            expected_prior_revision=number - 1 or None,
            event_kind='publish' if number == 1 else 'correct',
        )
        assert received.publication_revision == number
        assert received.state != 'applied'
        assert await profile.worker.recover_pending() == 1
        profile.now[0] += timedelta(minutes=1)
        await recovery.recover_pending()
        active = await lifecycle.read(scope=scope, source_id=source_id)
        assert active.status == 'active' and active.active_material_revision == 1
        assert active.publication_revision == number and active.active_content_digest == capture.source_content_digest
    assert any(fact.value.get('number') == 479 for fact in await visible(profile))
    current = await env.ledger.read_source(scope, source_id)
    assert current['currentIntent']['event']['snapshotRef']['contractVersion'] == COMPLETE_SOURCE_CONTRACT
    assert current['currentIntent']['event']['sourceRevision'] == 1
    withdrawn = await publisher.retract(
        scope=scope, source_id=source_id, source_revision=1, request_id='withdraw', expected_prior_revision=2
    )
    assert withdrawn.publication_revision == 3
    assert (await lifecycle.read(scope=scope, source_id=source_id)).status == 'retracting'
    assert await profile.worker.recover_pending() == 1
    profile.now[0] += timedelta(minutes=1)
    await recovery.recover_pending()
    assert not await visible(profile)
    assert (await lifecycle.read(scope=scope, source_id=source_id)).status == 'withdrawn'
