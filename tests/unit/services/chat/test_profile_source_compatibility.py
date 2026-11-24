"""Pin known legacy input compatibility before accepting a new Chat source."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.chat.profile_source import ChatProfileSourceRouter
from health_profile.chat_source import ChatProfileSourceService
from modules.chat.profile_source import CHAT_PROFILE_DELIVERY_MODE


def fixture():
    message = {
        'messageId': 'message',
        'messageRevision': 1,
        'role': 'user',
        'content': ' raw content ',
        'timestamp': '2026-11-10T00:00:00+00:00',
    }
    session = {'userId': 'user', 'sessionId': 'session', 'messages': [message]}
    legacy = ChatProfileSourceService.__new__(ChatProfileSourceService)
    from datetime import UTC
    from datetime import datetime

    legacy._now = lambda: datetime(2026, 11, 10, tzinfo=UTC)
    legacy.ensure_queued = AsyncMock()
    publication = SimpleNamespace(ensure_queued=AsyncMock())
    owner = SimpleNamespace(read_session=AsyncMock(return_value=session))
    return ChatProfileSourceRouter(publication=publication, owner=owner, legacy=legacy), session, message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'where,key,bound',
    [
        ('session', 'userId', 256),
        ('session', 'sessionId', 256),
        ('message', 'messageId', 256),
        ('message', 'content', 128 * 1024),
        ('message', 'timestamp', 128),
    ],
)
async def test_new_over_limit_input_pins_real_legacy_without_changing_raw_input(where, key, bound):
    router, session, message = fixture()
    target = session if where == 'session' else message
    target[key] = 'x' * (bound + 1)
    original = deepcopy(session)
    router.prepare_user_message(session, message)
    state = session['chatProfileIngestion'][0]
    assert state['deliveryMode'] == 'legacy' and state['consumerRevision'] == 'chat-profile-legacy-v1'
    assert {key: value for key, value in session.items() if key != 'chatProfileIngestion'} == original
    assert 'draft' not in state
    router.prepare_user_message(session, message)
    assert len(session['chatProfileIngestion']) == 1
    await router.ensure_queued(user_id=session['userId'], message_id=message['messageId'])
    router.legacy.ensure_queued.assert_awaited_once()
    router.publication.ensure_queued.assert_not_awaited()


@pytest.mark.parametrize(
    'where,key,bound',
    [
        ('session', 'userId', 256),
        ('session', 'sessionId', 256),
        ('message', 'messageId', 256),
        ('message', 'content', 128 * 1024),
        ('message', 'timestamp', 128),
    ],
)
def test_boundary_input_still_uses_new_fixed_source(where, key, bound):
    router, session, message = fixture()
    (session if where == 'session' else message)[key] = 'x' * bound
    router.prepare_user_message(session, message)
    assert session['chatProfileIngestion'][0]['deliveryMode'] == CHAT_PROFILE_DELIVERY_MODE


def test_accepted_new_source_never_downgrades_when_input_is_later_changed():
    router, session, message = fixture()
    router.prepare_user_message(session, message)
    before = deepcopy(session['chatProfileIngestion'])
    session['userId'] = 'u' * 257
    with pytest.raises(ValueError):
        router.prepare_user_message(session, message)
    assert session['chatProfileIngestion'] == before


@pytest.mark.parametrize('mode', ['unknown', CHAT_PROFILE_DELIVERY_MODE])
def test_unknown_or_new_existing_mode_does_not_fall_back_for_long_input(mode):
    router, session, message = fixture()
    session['userId'] = 'u' * 257
    session['chatProfileIngestion'] = [{'messageId': message['messageId'], 'deliveryMode': mode}]
    before = deepcopy(session['chatProfileIngestion'])
    with pytest.raises(ValueError):
        router.prepare_user_message(session, message)
    assert session['chatProfileIngestion'] == before


def test_legacy_normalized_marker_keeps_raw_padded_message_id_and_replays_once():
    router, session, message = fixture()
    message['messageId'] = ' padded '
    message['content'] = 'x' * (128 * 1024 + 1)
    original = deepcopy(message)
    router.prepare_user_message(session, message)
    state = session['chatProfileIngestion'][0]
    assert state['messageId'] == 'padded' and state['deliveryMode'] == 'legacy'
    assert message == original
    router.prepare_user_message(session, message)
    assert session['chatProfileIngestion'] == [state] and message == original


@pytest.mark.parametrize('mode', ['unknown', CHAT_PROFILE_DELIVERY_MODE])
def test_normalized_existing_new_or_unknown_marker_cannot_enable_legacy_fallback(mode):
    router, session, message = fixture()
    message['messageId'] = ' padded '
    message['content'] = 'x' * (128 * 1024 + 1)
    session['chatProfileIngestion'] = [{'messageId': 'padded', 'deliveryMode': mode}]
    before = deepcopy(session)
    with pytest.raises(ValueError):
        router.prepare_user_message(session, message)
    assert session == before


def test_duplicate_raw_and_normalized_markers_stop_before_legacy_mutation():
    router, session, message = fixture()
    message['messageId'] = ' padded '
    message['content'] = 'x' * (128 * 1024 + 1)
    session['chatProfileIngestion'] = [
        {'messageId': value, 'deliveryMode': 'legacy'} for value in ['padded', ' padded ']
    ]
    before = deepcopy(session)
    with pytest.raises(ValueError, match='duplicate'):
        router.prepare_user_message(session, message)
    assert session == before


def test_unrelated_non_dictionary_legacy_markers_do_not_block_known_compatibility():
    router, session, message = fixture()
    session['userId'] = 'u' * 257
    session['chatProfileIngestion'] = [None, 'historical-invalid-entry']
    router.prepare_user_message(session, message)
    assert session['chatProfileIngestion'][:2] == [None, 'historical-invalid-entry']
    assert session['chatProfileIngestion'][2]['deliveryMode'] == 'legacy'
    router.prepare_user_message(session, message)
    assert len(session['chatProfileIngestion']) == 3
