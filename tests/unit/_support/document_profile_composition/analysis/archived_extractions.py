"""Shared synthetic fixture closure extracted from tests.unit.services.analysis.test_archived_extractions."""

from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.analysis.archived_extractions import DocumentArchivedExtractionReader
from tests.unit._support.document_profile_composition.analysis.document_formal_source import advance
from tests.unit._support.document_profile_composition.analysis.document_formal_source import apply
from tests.unit._support.document_profile_composition.analysis.document_formal_source import flow
from tests.unit._support.document_profile_composition.analysis.document_formal_source import request
from tests.unit._support.document_profile_composition.analysis.fakes import MemoryMongoCollection


def reader(env, *, state=None, managed=None):
    collection = env.flow.index_collection
    collection.find = MethodType(MemoryMongoCollection.find, collection)
    lifecycle = SimpleNamespace(
        read=AsyncMock(return_value=state), ledger=SimpleNamespace(read_source=AsyncMock(return_value={}))
    )
    return DocumentArchivedExtractionReader(
        index=env.flow.index,
        traces=env.flow.service._document_trace_store,
        understanding=env.api,
        lifecycle=lifecycle,
        managed=managed,
    )


async def combined_source():
    """Actual two-file Document/Profile publication, reusable by composition tests."""
    from modules.analysis.managed_source_contracts import MANAGED_SOURCE_REVISION
    from modules.analysis.managed_source_contracts import DocumentManagedSourceCommand
    from modules.analysis.understanding import UNDERSTANDING_SELECTION
    from tests.unit._support.document_profile_composition.analysis.document_managed_source import item

    current = await flow()
    second = await current.env.api.prepare_bundle(
        scope=current.scope,
        selection=UNDERSTANDING_SELECTION,
        content=b'%PDF-1.7 second combined input',
        file_name='secondary.pdf',
    )
    for identity, selected in (('primary', current.item), ('secondary', item(second))):
        command = request(current, identity=identity, inputs=(selected,))
        await current.formal.submit(command)
        assert (await advance(current, command))['status'] == 'completed'
    await apply(current)
    state = await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    command = DocumentManagedSourceCommand(
        current.scope,
        state.source_id,
        'combined-A-B',
        state.publication_revision,
        state.active_snapshot_ref,
        'replace_report',
        (current.item, item(second)),
        MANAGED_SOURCE_REVISION,
    )
    published = await current.runtime.commands.execute(command)
    await apply(current)
    env = SimpleNamespace(flow=current.env.flow, api=current.env.api.understanding)
    api = reader(env, managed=current.runtime)
    api.lifecycle = current.runtime.lifecycle
    return current, second, api, command, published
