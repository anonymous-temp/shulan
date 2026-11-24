"""Raw Chat identity stays immutable while Profile owns legacy preprocessing."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from health_profile.chat_ingestion import ChatMessageCandidateExtractor
from health_profile.chat_source import _record_from_session
from health_profile.source_extraction import ProfileUserSourceResolver
from health_profile.source_publications import publication_id
from modules.health_profile.contracts import ProfileChatSourceSnapshotV2
from modules.health_profile.contracts import parse_profile_source_snapshot
from modules.health_profile.contracts import profile_content_digest

from .test_chat_ingestion import _runtime
from .test_multi_source_publications import candidate
from .test_multi_source_publications import event_for
from .test_multi_source_publications import setup_source
from .test_multi_source_publications import source_facts


async def setup_v2(*, content='  text  ', timestamp='2026-11-09T12:30:00'):
    flow = await setup_source('chat')
    flow.snapshot = ProfileChatSourceSnapshotV2(
        scope=flow.scope,
        source_id=flow.snapshot.source_id,
        source_revision=1,
        session_id='session-1',
        content=content,
        timestamp=timestamp,
    )
    reader = type('Reader', (), {})()
    reader.read_source_snapshot = AsyncMock(side_effect=lambda ref, scope: deepcopy(flow.materials[ref.snapshot_id]))
    flow.applier.resolvers[('chat', flow.snapshot.contract_version)] = ProfileUserSourceResolver(reader)
    return flow


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'timestamp', ['2026-11-09T12:30:00', ' 2026-11-09T12:30:00 ', '2026-11-09T12:30:00Z', '2026-11-09T12:30:00-05:00']
)
async def test_v2_uses_exact_legacy_input_and_preserves_raw_identity(timestamp):
    content = '  ' + candidate()['evidenceText'] + '\n  '
    flow = await setup_v2(content=content, timestamp=timestamp)
    event = event_for(flow)
    raw = deepcopy(flow.materials[event.snapshot_ref.snapshot_id])
    legacy_record = _record_from_session(
        {
            'type': 'chat',
            'userId': 'u',
            'sessionId': 'session-1',
            'messages': [
                {'messageId': flow.snapshot.source_id, 'role': 'user', 'content': content, 'timestamp': timestamp}
            ],
            'chatProfileIngestion': [{'messageId': flow.snapshot.source_id, 'sourceRevision': 1}],
        },
        message_id=flow.snapshot.source_id,
    )
    runtime, gateway = _runtime({'candidates': [candidate()]})
    await ChatMessageCandidateExtractor(runtime.hub).extract(legacy_record, self_subject_id=flow.scope.subject_id)
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert flow.gateway.complete_requests[0].content == gateway.complete_requests[0].content
    assert flow.materials[event.snapshot_ref.snapshot_id] == raw
    assert raw['content'] == content and raw['timestamp'] == timestamp
    assert profile_content_digest(raw) == event.content_digest
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'applied' and receipt.processing_revision == 'profile-chat-source-v2'
    await flow.inbox.receive(event, scope=flow.scope)
    assert await flow.worker.recover_pending() == 0
    assert len(flow.gateway.complete_requests) == 1
    assert len(await source_facts(flow)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('content', 'timestamp'), [('   ', '2026-11-09T12:30:00'), ('text', 'invalid'), ('text', '1234')]
)
async def test_invalid_raw_source_fails_before_any_model_or_fact(content, timestamp):
    flow = await setup_v2(content=content, timestamp=timestamp)
    event = event_for(flow)
    await flow.inbox.receive(event, scope=flow.scope)
    await flow.worker.recover_pending()
    receipt = await flow.inbox.get_receipt(event.event_id, scope=flow.scope)
    assert receipt.status == 'failed' and receipt.error_code == 'source.chat_input_invalid'
    assert not flow.gateway.complete_requests and not await source_facts(flow)


@pytest.mark.asyncio
async def test_v2_raw_text_identity_and_version_remain_distinct_from_v1():
    flow = await setup_v2()
    raw = flow.snapshot.model_dump(mode='json', by_alias=True)
    assert parse_profile_source_snapshot(raw).model_dump(mode='json', by_alias=True) == raw
    assert profile_content_digest(raw) != profile_content_digest({**raw, 'content': raw['content'].strip()})
    original = await setup_source('chat')
    original.snapshot = original.snapshot.model_copy(update={'content': '  ' + candidate()['evidenceText'] + '  '})
    event = event_for(original)
    await original.inbox.receive(event, scope=original.scope)
    await original.worker.recover_pending()
    saved = await original.inbox.store.get(publication_id(event))
    assert saved['processingSelection']['implementationRevision'] == 'profile-chat-source-v1'
    assert '  ' + candidate()['evidenceText'] + '  ' in original.gateway.complete_requests[0].content
