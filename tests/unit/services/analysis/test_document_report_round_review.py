"""R1 ordering and recovery through the actual current Document/Report factories."""

import asyncio

import pytest

from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.publication import PublicationConflictError
from tests.unit._support.current_document import REPORT_TEXT
from tests.unit._support.current_document import model_report
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_document_profile import settle
from tests.unit._support.current_report_projection import admit
from tests.unit._support.current_report_projection import command
from tests.unit._support.current_report_projection import composition
from tests.unit._support.current_report_projection import next_round
from tests.unit._support.current_report_projection import page_bytes
from tests.unit._support.current_report_projection import projection
from tests.unit._support.current_report_projection import report_contributions
from tests.unit._support.current_report_projection import source
from tests.unit._support.current_report_projection import wire_projection


async def rounds_fixture(monkeypatch):
    changed = model_report()
    changed['observations'][0]['resultValue'] = '15'
    flow = await composition(monkeypatch, [model_report(), model_report(), changed, model_report()], png=True)
    primary = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/first.png', file_type='png'
    )
    flow.upstream.downloader.download.return_value = page_bytes('Synthetic next page', png=True)
    flow.upstream.client.result = {'success': True, 'text': '\u68c0\u6d4b\u7ed3\u679c\n' + REPORT_TEXT + 'AST 15 U/L\n'}
    first = await flow.prepared.prepare_bundle(
        scope=flow.scope, file_id='https://example.test/second.png', file_type='png'
    )
    one = await projection(flow, 'report-one', primary, first)
    ref = await admit(flow, primary, one)
    await publish(flow)
    second = await next_round(flow, first)
    third = await next_round(flow, second)
    digests = [bundle.facts.payload['clinicalContentDigest'] for bundle in (first, second, third)]
    assert digests[0] == digests[2] != digests[1]
    two = await projection(flow, 'report-two', primary, second)
    three = await projection(flow, 'report-three', primary, third)
    (report,) = report_contributions(await source(flow, ref))
    identity = report['contributionId']
    late = command(
        flow,
        primary,
        ref,
        1,
        'round-two',
        report_projection_ref=two.snapshot_id,
        replace_report_contribution_id=identity,
    )
    unchanged = command(
        flow,
        primary,
        ref,
        1,
        'round-three',
        report_projection_ref=three.snapshot_id,
        replace_report_contribution_id=identity,
    )
    return flow, primary, third, ref, identity, late, unchanged


@pytest.mark.asyncio
async def test_accepted_unchanged_round_survives_lost_ack_restart_and_later_publication(monkeypatch):
    flow, primary, third, ref, identity, late, unchanged = await rounds_fixture(monkeypatch)
    accept = flow.ledger.accept_current_noop

    async def lose_ack(*args, **kwargs):
        await accept(*args, **kwargs)
        raise OSError('synthetic no-change CAS acknowledgement loss')

    with monkeypatch.context() as fault:
        fault.setattr(flow.ledger, 'accept_current_noop', lose_ack)
        with pytest.raises(OSError, match='acknowledgement loss'):
            await flow.runtime.managed.execute(unchanged)
    flow.ledger = MongoDocumentPublicationLedger(sources=flow.ledger.sources, intents=flow.ledger.intents)
    wire_projection(flow)
    with pytest.raises(PublicationConflictError):
        await flow.runtime.managed.execute(late)
    assert await flow.ledger.read_reserved(flow.scope, late.source_id, late.command_id) is None
    ledger = await flow.ledger.read_source(flow.scope, late.source_id)
    assert ledger['publicationRevision'] == 1
    assert ledger['acceptedInterpretationRounds'][third.receipt.document_id] == 3

    retire = command(flow, primary, ref, 1, 'retire-at-head', retire_contribution_ids=(identity,))
    assert (await flow.runtime.managed.execute(retire))['publicationRevision'] == 2
    await settle(flow)
    rescued = await flow.runtime.managed.execute(unchanged)
    assert rescued['status'] == 'unchanged'
    assert rescued['publicationRevision'] == 1
    assert rescued['sourceSnapshotRef'] == ref
    assert await flow.runtime.managed.execute(unchanged) == rescued
    assert (await flow.ledger.read_source(flow.scope, late.source_id))['publicationRevision'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('unchanged_first', [False, True])
async def test_live_publication_and_unchanged_compete_in_one_source_cas(monkeypatch, unchanged_first):
    flow, _, third, _, _, late, unchanged = await rounds_fixture(monkeypatch)
    real_cas = flow.ledger._cas
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause_loser(row, fields):
        is_noop = 'currentNoopAcknowledgement' in fields
        is_publish = fields.get('publicationRevision') == 2
        if not entered.is_set() and (is_publish if unchanged_first else is_noop):
            entered.set()
            await release.wait()
        return await real_cas(row, fields)

    monkeypatch.setattr(flow.ledger, '_cas', pause_loser)
    winner, loser = (unchanged, late) if unchanged_first else (late, unchanged)
    pending = asyncio.create_task(flow.runtime.managed.execute(loser))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        result = await flow.runtime.managed.execute(winner)
    finally:
        release.set()
    with pytest.raises(PublicationConflictError):
        await pending
    assert await flow.ledger.read_reserved(flow.scope, loser.source_id, loser.command_id) is None
    assert await flow.ledger.read_current_noop(flow.scope, loser.source_id, loser.command_id) is None
    assert result['status'] == ('unchanged' if unchanged_first else 'published')
    ledger = await flow.ledger.read_source(flow.scope, loser.source_id)
    assert ledger['publicationRevision'] == (1 if unchanged_first else 2)
    assert ledger['acceptedInterpretationRounds'][third.receipt.document_id] == (3 if unchanged_first else 2)
