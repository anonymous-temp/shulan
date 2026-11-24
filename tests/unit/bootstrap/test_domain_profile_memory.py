"""Default composition separates domain renderers over the same public facts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from bootstrap.factory import build_container
from health_profile.document_observations import MongoDocumentObservationStore
from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from modules.chat.profile_memory import ChatProfileMemory
from modules.health_profile import ProfileLegacyIngestionStatusReader
from modules.record.profile_memory import RecordProfileMemory
from tests.unit.bootstrap.test_factory import _dependencies
from tests.unit.bootstrap.test_factory import _RecordingManager
from tests.unit.health_profile.test_fact_pages import environment
from tests.unit.health_profile.test_fact_pages import stored
from tests.unit.services.analysis.test_document_observation_reader import Collection
from tests.unit.services.analysis.test_document_observation_reader import row


def build(**options):
    dependencies = _dependencies()
    dependencies.pop('health_profile_memory_provider')
    return build_container(
        object(),
        **dependencies,
        **options,
        record_document_port=object(),
        report_pipeline=object(),
        report_followup_service=object(),
        gut_barrier_service=object(),
        qa_manager_type=_RecordingManager,
        chat_manager_type=_RecordingManager,
    )


@pytest.mark.asyncio
async def test_default_root_connects_both_real_domain_adapters_and_public_reads_without_writes(monkeypatch):
    flow = environment(count=305)
    collection = Collection([row(userId='u', subjectId='self-u')])
    legacy = RepositoryHealthProfileChatMemoryProvider(
        flow.repository, document_observation_store=MongoDocumentObservationStore(collection, flow.collections['jobs'])
    )
    expected = await legacy.build_context('u')
    before = stored(flow)
    original_index_calls = collection.create_index.await_count
    collection.find_calls.clear()
    contracts = SimpleNamespace(
        reader=flow.reader, legacy_ingestion=ProfileLegacyIngestionStatusReader(flow.repository)
    )
    create = Mock(return_value=contracts)
    monkeypatch.setattr('modules.health_profile.application.create_application_profile_public_contracts', create)
    monkeypatch.setattr('db.mongodb.collection_document_observations', collection)
    monkeypatch.setattr(
        'modules.health_profile.application.create_application_health_profile_chat_memory_provider',
        Mock(side_effect=AssertionError('default domains must not construct a shared renderer')),
    )
    flow.reader.read_page = AsyncMock(wraps=flow.reader.read_page)
    container = build()
    record = container.record_service.dependencies['health_profile_memory_provider']
    chat = container.chat_service.dependencies['health_profile_memory_provider']
    assert isinstance(record, RecordProfileMemory) and isinstance(chat, ChatProfileMemory)
    assert record is not chat
    create.assert_called_once_with()
    flow.reader.read_page.assert_not_awaited()
    assert collection.find_calls == [] and stored(flow) == before
    assert await record.build_context('u') == expected
    assert await chat.build_context('u') == expected
    assert flow.reader.read_page.await_count == 4  # Both domains read both complete pages.
    assert collection.create_index.await_count == original_index_calls and stored(flow) == before
    assert container.record_profile_memory_provider is record
    assert container.chat_profile_memory_provider is chat
    assert container.compatibility_settings()['health_profile_memory_provider'] is chat
    assert container.compatibility_settings()['record_profile_memory_provider'] is record


def test_explicit_historical_provider_preserves_both_domains_without_new_public_resources(monkeypatch):
    create = Mock(side_effect=AssertionError('explicit old provider needs no new resource'))
    monkeypatch.setattr('modules.health_profile.application.create_application_profile_public_contracts', create)
    legacy = object()
    container = build(health_profile_memory_provider=legacy)
    assert container.record_service.dependencies['health_profile_memory_provider'] is legacy
    assert container.chat_service.dependencies['health_profile_memory_provider'] is legacy
    assert container.health_profile_memory_provider is legacy
    create.assert_not_called()


def test_domain_overrides_are_independent_and_take_priority_over_legacy_injection(monkeypatch):
    create = Mock(side_effect=AssertionError('all domain providers already supplied'))
    monkeypatch.setattr('modules.health_profile.application.create_application_profile_public_contracts', create)
    record, chat, legacy = object(), object(), object()
    container = build(
        record_profile_memory_provider=record, chat_profile_memory_provider=chat, health_profile_memory_provider=legacy
    )
    assert container.record_service.dependencies['health_profile_memory_provider'] is record
    assert container.chat_service.dependencies['health_profile_memory_provider'] is chat
    assert container.health_profile_memory_provider is legacy
    create.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_public_readers_can_supply_only_the_missing_domain(monkeypatch):
    create = Mock(side_effect=AssertionError('public read resources already supplied'))
    monkeypatch.setattr('modules.health_profile.application.create_application_profile_public_contracts', create)
    flow = environment()
    observations = SimpleNamespace(read=AsyncMock(side_effect=OSError('optional supplement unavailable')))
    record = object()
    container = build(
        record_profile_memory_provider=record,
        profile_snapshot_reader=flow.reader,
        document_observation_reader=observations,
    )
    assert container.record_service.dependencies['health_profile_memory_provider'] is record
    chat = container.chat_service.dependencies['health_profile_memory_provider']
    assert isinstance(chat, ChatProfileMemory)
    expected = await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u')
    assert await chat.build_context('u') == expected
    create.assert_not_called()
