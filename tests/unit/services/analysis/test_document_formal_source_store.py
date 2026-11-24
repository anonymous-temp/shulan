"""Fixed command ownership, durable acceptance and stale worker fencing."""

from copy import deepcopy
from dataclasses import asdict
from dataclasses import replace

import pytest

from adapters.analysis.formal_source_store import MongoDocumentFormalSourceStore
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.formal_source_contracts import FORMAL_SOURCE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from modules.analysis.formal_source_contracts import DocumentFormalSourcePolicy
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from modules.analysis.understanding import DocumentPreparationReceipt
from tests.unit._support.document_profile_composition.analysis.document_formal_source_store import (
    Collection as Collection,
)
from tests.unit._support.document_profile_composition.analysis.document_formal_source_store import Cursor as Cursor


def command():
    return DocumentFormalSourceCommand(
        DocumentScope('user', 'self'),
        'accepted-command',
        'base',
        (
            DocumentPreparedPublicationInput(
                'material-fixed',
                'facts-fixed',
                'understanding-fixed',
                UNDERSTANDING_SELECTION,
                DocumentPreparationReceipt('document', 'trace', False),
            ),
        ),
        FORMAL_SOURCE_REVISION,
    )


def store():
    collection = Collection()
    options = []
    collection.with_options = lambda **kw: options.append(kw) or collection
    return MongoDocumentFormalSourceStore(collection), options


def row(identity='one', *, due=1.0, status='pending'):
    request = command()
    return {
        '_id': identity,
        'revision': 1,
        'command': request.as_dict(),
        'status': status,
        'nextAttemptAt': due,
        'policy': asdict(DocumentFormalSourcePolicy()),
    }


def test_fixed_commands_roundtrip_and_same_identity_rejects_implicit_new_versions():
    request = command()
    assert DocumentFormalSourceCommand.from_dict(request.as_dict()) == request
    changed = replace(request, operation='report')
    assert changed.command_id == request.command_id  # the acceptor must reject changed content under this identity
    assert changed.as_dict() != request.as_dict()
    with pytest.raises(UnsupportedOutputVersionError):
        replace(request, command_revision='latest')
    with pytest.raises(ValueError):
        replace(request, operation='base', inputs=request.inputs * 2)
    with pytest.raises(TypeError):
        replace(request, inputs=('raw-unverified',))


@pytest.mark.asyncio
async def test_acceptance_is_detached_and_duplicate_returns_original_for_binding_validation():
    persistence, options = store()
    assert options[0]['read_preference'].name == 'Primary'
    assert options[0]['write_concern'].document == {'wtimeout': 5000, 'j': True, 'w': 'majority'}
    original = row()
    await persistence.insert(original)
    original['command']['requestId'] = 'mutated'
    assert (await persistence.get('one'))['command'] == command().as_dict()
    duplicate = await persistence.insert({**row(), 'command': {'different': True}})
    assert duplicate['command'] == command().as_dict()


@pytest.mark.asyncio
async def test_only_one_live_claim_and_stale_worker_cannot_save_or_unlock_successor():
    persistence, _ = store()
    await persistence.insert(row())
    first = await persistence.claim('one', owner='worker-a', now=10.0)
    assert first is not None
    assert await persistence.claim('one', owner='worker-b', now=11.0) is None
    next_claim = await persistence.claim('one', owner='worker-b', now=131.0)
    assert next_claim['leaseGeneration'] == first['leaseGeneration'] + 1
    stale = {**deepcopy(first), 'revision': first['revision'] + 1, 'leaseOwner': None}
    assert not await persistence.replace(first, stale, now=132.0)
    assert await persistence.get('one') == next_claim
    finished = {**next_claim, 'revision': next_claim['revision'] + 1, 'status': 'completed', 'leaseOwner': None}
    assert await persistence.replace(next_claim, finished, now=132.0)
    assert await persistence.claim('one', owner='worker-c', now=500.0) is None


@pytest.mark.asyncio
async def test_expired_claim_cannot_advance_even_when_no_successor_has_arrived():
    persistence, _ = store()
    await persistence.insert(row())
    claimed = await persistence.claim('one', owner='worker', now=10.0)
    assert not await persistence.replace(claimed, {**claimed, 'revision': claimed['revision'] + 1}, now=130.0)


@pytest.mark.asyncio
async def test_pending_orders_due_work_and_excludes_terminal_future_or_leased_rows_before_limit():
    persistence, _ = store()
    for value in [
        row('a', due=20.0),
        row('b', due=10.0),
        row('old', due=0.0, status='failed'),
        row('future', due=99.0),
    ]:
        await persistence.insert(value)
    await persistence.insert(row('leased', due=0.0))
    await persistence.claim('leased', owner='worker', now=1.0)
    assert await persistence.pending(now=30.0, limit=1) == ['b']
    assert await persistence.pending(now=30.0, limit=2) == ['b', 'a']
    with pytest.raises(ValueError):
        await persistence.pending(now=30.0, limit=True)
