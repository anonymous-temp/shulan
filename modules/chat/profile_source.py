"""Chat owns raw user-message publication drafts; Profile owns normalization and facts."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

CHAT_PROFILE_CONSUMER = 'chat-profile-source.v1'
CHAT_PROFILE_DELIVERY_MODE = 'profile-source-event.v1'
CHAT_PROFILE_DRAFT = 'chat.profile-source-draft.v1'


def source_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()


def _text(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'invalid Chat source {name}')
    return value


@dataclass(frozen=True)
class ChatProfileDeliveryPolicy:
    max_attempts: int = 24
    retry_seconds: int = 30
    max_age_seconds: int = 86400
    lease_seconds: int = 120

    def __post_init__(self):
        for value in asdict(self).values():
            if type(value) is not int or value < 1:
                raise ValueError('Chat source delivery policy requires positive integer bounds')


def _delivery_binding(state):
    return {
        key: state[key]
        for key in (
            'messageId',
            'sourceRevision',
            'publicationRevision',
            'deliveryMode',
            'consumerRevision',
            'draftDigest',
            'deliveryPolicy',
            'createdAt',
        )
    }


def prepare_user_message_source(session, message, *, policy=ChatProfileDeliveryPolicy(), now=None):
    """Mutate only the to-be-saved Chat state, synchronously and before any model/I/O.

    The existing message save commits this marker and the raw message together.
    No source is eligible for publication until the owner repository verifies that
    persisted pair. Model failure or a failed save must therefore publish nothing.
    """
    if not isinstance(session, dict) or not isinstance(message, dict):
        raise TypeError('Chat source draft requires dictionaries')
    if (
        message.get('role') != 'user'
        or type(message.get('messageRevision')) is not int
        or message['messageRevision'] != 1
    ):
        raise ValueError('only supported raw user messages may become Profile sources')
    user_id, session_id = _text(session.get('userId'), 'user'), _text(session.get('sessionId'), 'session')
    message_id = _text(message.get('messageId'), 'message')
    draft = {
        'contractVersion': CHAT_PROFILE_DRAFT,
        'consumerRevision': CHAT_PROFILE_CONSUMER,
        'userId': user_id,
        'sessionId': session_id,
        'sourceId': message_id,
        'sourceRevision': 1,
        'role': 'user',
        'content': _text(message.get('content'), 'content', 128 * 1024),
        'timestamp': _text(message.get('timestamp'), 'timestamp', 128),
    }
    digest = source_digest(draft)
    states = session.setdefault('chatProfileIngestion', [])
    if not isinstance(states, list):
        raise ValueError('chatProfileIngestion must be a list')
    existing = [item for item in states if isinstance(item, dict) and item.get('messageId') == message_id]
    if existing:
        if (
            len(existing) != 1
            or existing[0].get('deliveryMode') != CHAT_PROFILE_DELIVERY_MODE
            or existing[0].get('draftDigest') != digest
            or existing[0].get('draft') != draft
        ):
            raise ValueError('the persisted Chat source identity cannot be replaced')
        return deepcopy(existing[0])
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('Chat publication policy time must be timezone-aware')
    state = {
        'messageId': message_id,
        'sourceRevision': 1,
        'publicationRevision': 1,
        'deliveryMode': CHAT_PROFILE_DELIVERY_MODE,
        'consumerRevision': CHAT_PROFILE_CONSUMER,
        'status': 'pending',
        'jobId': None,
        'draft': draft,
        'draftDigest': digest,
        'deliveryPolicy': asdict(policy),
        'createdAt': now.isoformat(),
        'updatedAt': now,
    }
    state['deliveryManifestDigest'] = source_digest(_delivery_binding(state))
    states.append(deepcopy(state))
    return deepcopy(state)


def verified_saved_source(session, message_id):
    """Return a detached raw draft only when an owner-persisted user message proves it."""
    if not isinstance(session, dict):
        return None
    states = [
        item
        for item in session.get('chatProfileIngestion', [])
        if isinstance(item, dict) and item.get('messageId') == message_id
    ]
    if not states:
        return None
    if len(states) != 1:
        raise ValueError('duplicate Chat source markers')
    if states[0].get('deliveryMode') in (None, 'legacy'):
        return None
    state = deepcopy(states[0])
    if (
        state.get('deliveryMode') != CHAT_PROFILE_DELIVERY_MODE
        or state.get('consumerRevision') != CHAT_PROFILE_CONSUMER
    ):
        raise ValueError('unsupported Chat source consumer revision')
    messages = [
        item for item in session.get('messages', []) if isinstance(item, dict) and item.get('messageId') == message_id
    ]
    if len(messages) != 1:
        raise ValueError('Chat source needs exactly one saved user message')
    check = {'userId': session.get('userId'), 'sessionId': session.get('sessionId'), 'chatProfileIngestion': [state]}
    prepare_user_message_source(check, messages[0])
    ChatProfileDeliveryPolicy(**state['deliveryPolicy'])
    if any(type(state.get(key)) is not int or state[key] != 1 for key in ('sourceRevision', 'publicationRevision')):
        raise ValueError('unsupported Chat source publication revision')
    if state.get('deliveryManifestDigest') != source_digest(_delivery_binding(state)):
        raise ValueError('Chat source delivery manifest changed')
    created = datetime.fromisoformat(state['createdAt'])
    if created.tzinfo is None or created.utcoffset() is None:
        raise ValueError('Chat source policy time is not fixed')
    return state
