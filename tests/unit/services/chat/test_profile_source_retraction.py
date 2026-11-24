"""A later, applied retraction is required before stopping Chat source recovery."""

from copy import deepcopy

import pytest

from modules.health_profile.contracts import ProfileSourceReceiptV1
from tests.unit.services.chat.test_profile_source_publication import environment
from tests.unit.services.chat.test_profile_source_publication import save
from tests.unit.services.chat.test_profile_source_publication import task


async def accepted_message(*, poll):
    env = environment()
    await save(env)
    if poll:
        await env.service.recover_one(env.identity)
        assert (await task(env))['status'] == 'waiting_receipt'
        env.now[0] += 31
    return env


def retraction_receipt(env, *, revision, settled, applied):
    original = env.receiver.receipt

    def receipt(event):
        value = original(event)
        value.update(
            status='superseded',
            applicationState='superseded_by_retraction',
            supersededByPublicationRevision=revision,
        )
        value['watermark'].update(
            acceptedPublicationRevision=3,
            acceptedRetractionRevision=3,
            appliedRetractionRevision=applied,
            settledPublicationRevision=settled,
        )
        return value

    env.receiver.receipt = receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('poll', [False, True], ids=['initial_ack', 'stored_ack_poll'])
@pytest.mark.parametrize(
    'revision,settled,applied',
    [(None, 1, 0), (1, 1, 1), (True, 3, 3), ('3', 3, 3), (3.0, 3, 3), (3, 2, 3), (3, 3, 2)],
)
async def test_incomplete_retraction_proof_never_retires_the_saved_message(poll, revision, settled, applied):
    env = await accepted_message(poll=poll)
    saved_business = deepcopy(env.sessions.documents)
    prior_receipt = deepcopy((await task(env)).get('receipt'))
    retraction_receipt(env, revision=revision, settled=settled, applied=applied)

    await env.service.recover_one(env.identity)

    current = await task(env)
    assert current['status'] == 'failed'
    assert current['errorCode'] == 'chat_source_ChatSourceConflict'
    assert current.get('receipt') == prior_receipt
    assert env.sessions.documents == saved_business
    assert len(env.receiver.calls) == 1
    assert len(env.receiver.lookups) == int(poll)


@pytest.mark.asyncio
@pytest.mark.parametrize('poll', [False, True], ids=['initial_ack', 'stored_ack_poll'])
async def test_complete_public_retraction_receipt_is_persisted_and_stops_further_delivery(poll):
    env = await accepted_message(poll=poll)
    saved_business = deepcopy(env.sessions.documents)
    retraction_receipt(env, revision=3, settled=3, applied=3)

    await env.service.recover_one(env.identity)

    current = await task(env)
    assert current['status'] == 'superseded'
    receipt = ProfileSourceReceiptV1.model_validate(current['receipt'])
    assert receipt.superseded_by_publication_revision == 3
    assert receipt.watermark.applied_retraction_revision == receipt.watermark.settled_publication_revision == 3
    assert env.sessions.documents == saved_business
    env.now[0] += 31
    await env.service.recover_pending()
    assert len(env.receiver.calls) == 1
    assert len(env.receiver.lookups) == int(poll)
    assert (await task(env))['receipt'] == current['receipt']
