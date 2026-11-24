"""Public Profile adapter bindings, staged acceptance, and assembly have no model IO."""

from dataclasses import replace
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.publication_receiver import ProfileDocumentPublicationReceiver
from adapters.analysis.publication_runtime import create_document_publication_runtime
from health_profile.contracts import ProfilePublicationWatermarkV1
from health_profile.contracts import ProfileScope
from health_profile.contracts import ProfileSourceCutoverAuthorizationV1
from health_profile.contracts import ProfileSourceCutoverStateV1
from health_profile.contracts import ProfileSourceReceiptV1
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationModeError

SCOPE = ProfileScope(user_id='user', subject_id='self')
NOW = datetime.now(timezone.utc)


def event():
    return {
        'scope': SCOPE.model_dump(by_alias=True),
        'sourceType': 'health_report',
        'sourceId': 'report',
        'sourceRevision': 1,
        'publicationRevision': 1,
        'priorRevision': None,
        'eventKind': 'publish',
        'eventId': 'event-1',
        'occurredAt': NOW.isoformat(),
        'contentDigest': 'a' * 64,
        'snapshotRef': {
            'provider': 'document',
            'snapshotId': 'snapshot',
            'contractVersion': 'document.source-observations.v1',
        },
    }


def receipt(**changes):
    return ProfileSourceReceiptV1(
        **{
            'event_id': 'event-1',
            'publication_id': 'publication',
            'scope': SCOPE,
            'source_type': 'health_report',
            'source_id': 'report',
            'publication_revision': 1,
            'status': 'waiting',
            'received_at': NOW,
            'updated_at': NOW,
            'watermark': ProfilePublicationWatermarkV1(
                scope=SCOPE,
                source_type='health_report',
                source_id='report',
                accepted_publication_revision=1,
            ),
            **changes,
        }
    )


def setup():
    authorization = ProfileSourceCutoverAuthorizationV1(
        scope=SCOPE,
        source_type='health_report',
        source_id='report',
        authorization_id='owner-approved',
        legacy_publishers_quiesced=True,
        legacy_workers_quiesced=True,
        legacy_source_intents_retired=True,
        material_contract_version='document.source-observations.v1',
        authorized_at=NOW,
    )
    inbox = SimpleNamespace(
        get_cutover=AsyncMock(
            return_value=ProfileSourceCutoverStateV1(
                authorization=authorization,
                status='ready',
                updated_at=NOW,
            )
        ),
        receive=AsyncMock(return_value=receipt()),
        get_receipt=AsyncMock(return_value=receipt()),
    )
    return ProfileDocumentPublicationReceiver(inbox), inbox


@pytest.mark.asyncio
async def test_lookup_needs_verified_acceptance_binding_and_staged_alias_is_not_accepted():
    adapter, inbox = setup()
    assert await adapter.lookup(event()) is None
    proven = await adapter.deliver(event())
    assert await adapter.lookup(event(), known_receipt=proven) == proven
    with pytest.raises(PublicationConflictError):
        await adapter.lookup(event(), known_receipt=replace(proven, content_digest='b' * 64))
    inbox.get_receipt.return_value = receipt(
        watermark=ProfilePublicationWatermarkV1(
            scope=SCOPE,
            source_type='health_report',
            source_id='report',
        )
    )
    assert await adapter.lookup(event(), known_receipt=proven) is None
    inbox.receive.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changes',
    [
        {'event_id': 'other'},
        {'scope': ProfileScope(user_id='other', subject_id='self')},
        {'source_id': 'other'},
        {'source_type': 'chat'},
        {'publication_revision': 2},
    ],
)
async def test_wrong_public_receipt_binding_is_rejected(changes):
    adapter, inbox = setup()
    inbox.receive.return_value = receipt(**changes)
    with pytest.raises(PublicationConflictError):
        await adapter.deliver(event())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'status,application',
    [
        ('waiting', 'not_applied'),
        ('failed', 'incomplete'),
        ('partial', 'partial'),
    ],
)
async def test_partial_failed_and_superseded_states_are_not_claimed_as_applied(status, application):
    adapter, inbox = setup()
    inbox.receive.return_value = receipt(status=status, application_state=application)
    actual = await adapter.deliver(event())
    assert actual.state == status and actual.application_state == application
    assert actual.applied_revision == actual.settled_revision == 0


@pytest.mark.asyncio
async def test_superseded_receipt_preserves_exact_later_retraction_proof():
    adapter, inbox = setup()
    inbox.receive.return_value = receipt(
        status='superseded',
        application_state='superseded_by_retraction',
        superseded_by_publication_revision=2,
        watermark=ProfilePublicationWatermarkV1(
            scope=SCOPE,
            source_type='health_report',
            source_id='report',
            accepted_publication_revision=2,
            applied_publication_revision=2,
            settled_publication_revision=2,
            accepted_retraction_revision=2,
            applied_retraction_revision=2,
        ),
    )
    actual = await adapter.deliver(event())
    assert actual.state == 'superseded' and actual.superseded_by_revision == 2


@pytest.mark.asyncio
async def test_missing_ready_and_wrong_material_are_rejected_before_receive():
    adapter, inbox = setup()
    bad = event()
    bad['snapshotRef']['contractVersion'] = 'document.facts.v1'
    with pytest.raises(PublicationModeError):
        await adapter.deliver(bad)
    inbox.get_cutover.assert_not_awaited()
    inbox.get_cutover.return_value = None
    with pytest.raises(PublicationModeError):
        await adapter.deliver(event())
    inbox.receive.assert_not_awaited()


def test_factory_uses_existing_majority_primary_collections_without_io_or_worker():
    class Collection:
        def __init__(self):
            self.options = []

        def with_options(self, **options):
            self.options.append(options)
            return self

    sources, intents, observations = Collection(), Collection(), Collection()
    inbox = SimpleNamespace(receive=AsyncMock(), get_cutover=AsyncMock())
    runtime = create_document_publication_runtime(
        source_collection=sources,
        intent_collection=intents,
        observation_collection=observations,
        snapshots=object(),
        profile_inbox=inbox,
        legacy_ingestion_reader=object(),
    )
    assert runtime.recovery.ledger is runtime.ledger and runtime.lifecycle.ledger is runtime.ledger
    assert sources.options[0]['write_concern'].document == {'w': 'majority'}
    assert intents.options == sources.options
    inbox.receive.assert_not_awaited()
    inbox.get_cutover.assert_not_awaited()
