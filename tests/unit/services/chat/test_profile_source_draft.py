"""Only a saved raw user message with its same-commit marker may be published."""

from copy import deepcopy
from datetime import datetime
from datetime import timezone

import pytest

from modules.chat.profile_source import ChatProfileDeliveryPolicy
from modules.chat.profile_source import prepare_user_message_source
from modules.chat.profile_source import verified_saved_source


def source():
    message = {
        'messageId': 'm',
        'messageRevision': 1,
        'role': 'user',
        'content': '  original\ntext  ',
        'timestamp': '2026-11-10T12:00:00',
    }
    return {'userId': 'u', 'sessionId': 's', 'messages': [message]}, message


def test_raw_text_and_naive_business_time_stay_exact_with_same_commit_intent():
    session, message = source()
    state = prepare_user_message_source(session, message, now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert state['draft']['content'] == message['content']
    assert state['draft']['timestamp'] == message['timestamp']
    assert verified_saved_source(session, 'm') == state
    state['draft']['content'] = 'changed'
    assert verified_saved_source(session, 'm')['draft']['content'] == message['content']


def test_repeated_prepare_keeps_first_policy_and_publication_identity():
    session, message = source()
    first = prepare_user_message_source(session, message)
    again = prepare_user_message_source(session, message, policy=ChatProfileDeliveryPolicy(max_attempts=99))
    assert first == again and len(session['chatProfileIngestion']) == 1


@pytest.mark.parametrize(
    'field,value',
    [
        ('content', 'changed'),
        ('timestamp', 'changed'),
        ('role', 'assistant'),
        ('messageRevision', 2),
        ('messageRevision', True),
    ],
)
def test_saved_message_changes_cannot_be_accepted_as_the_frozen_source(field, value):
    session, message = source()
    prepare_user_message_source(session, message)
    message[field] = value
    with pytest.raises(ValueError):
        verified_saved_source(session, 'm')


@pytest.mark.parametrize(
    'change',
    [
        'missing_message',
        'duplicate_message',
        'duplicate_marker',
        'wrong_user',
        'unknown_consumer',
        'unknown_mode',
        'changed_draft',
    ],
)
def test_invalid_owner_commit_or_version_never_becomes_source(change):
    session, message = source()
    prepare_user_message_source(session, message)
    state = session['chatProfileIngestion'][0]
    if change == 'missing_message':
        session['messages'] = []
    elif change == 'duplicate_message':
        session['messages'].append(deepcopy(message))
    elif change == 'duplicate_marker':
        session['chatProfileIngestion'].append(deepcopy(state))
    elif change == 'wrong_user':
        session['userId'] = 'other'
    elif change == 'unknown_consumer':
        state['consumerRevision'] = 'future'
    elif change == 'unknown_mode':
        state['deliveryMode'] = 'future'
    else:
        state['draft']['content'] = 'other'
    with pytest.raises(ValueError):
        verified_saved_source(session, 'm')


def test_legacy_markers_are_not_reinterpreted_as_new_sources():
    session, message = source()
    session['chatProfileIngestion'] = [{'messageId': 'm', 'sourceRevision': 1, 'status': 'pending'}]
    assert verified_saved_source(session, 'm') is None
    with pytest.raises(ValueError):
        prepare_user_message_source(session, message)


@pytest.mark.parametrize('value', [0, -1, True, 1.5])
def test_delivery_policy_is_bounded_by_strict_positive_integers(value):
    with pytest.raises(ValueError):
        ChatProfileDeliveryPolicy(max_attempts=value)


@pytest.mark.parametrize('change', ['policy', 'created_at', 'boolean_revision', 'duplicate_legacy'])
def test_delivery_policy_and_first_legacy_marker_cannot_hide_conflicting_new_state(change):
    session, message = source()
    prepare_user_message_source(session, message)
    state = session['chatProfileIngestion'][0]
    if change == 'policy':
        state['deliveryPolicy']['max_attempts'] += 1
    elif change == 'created_at':
        state['createdAt'] = '2026-11-10T00:00:00+00:00'
    elif change == 'boolean_revision':
        state['publicationRevision'] = True
    else:
        session['chatProfileIngestion'].insert(0, {'messageId': 'm'})
    with pytest.raises(ValueError):
        verified_saved_source(session, 'm')
