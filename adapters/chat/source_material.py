"""Chat's immutable source snapshots implement only public Profile contracts."""

from copy import deepcopy
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from modules.chat.profile_source import CHAT_PROFILE_CONSUMER
from modules.chat.profile_source import CHAT_PROFILE_DELIVERY_MODE
from modules.chat.profile_source import CHAT_PROFILE_DRAFT
from modules.chat.profile_source import ChatProfileDeliveryPolicy
from modules.chat.profile_source import verified_saved_source
from modules.health_profile.contracts import ProfileChatSourceSnapshotV2
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.health_profile.contracts import profile_content_digest


def material_from_saved_session(session, message_id, *, scope):
    """Caller supplies the Chat repository's persisted document, never a request or model result."""
    scope = ProfileScope.model_validate(scope)
    state = verified_saved_source(session, message_id)
    if state is None:
        raise ValueError('legacy Chat messages require their fixed legacy consumer')
    draft = state['draft']
    if draft['userId'] != scope.user_id:
        raise PermissionError('Chat source scope differs from its saved owner')
    snapshot = ProfileChatSourceSnapshotV2(
        scope=scope,
        source_id=message_id,
        source_revision=draft['sourceRevision'],
        session_id=draft['sessionId'],
        content=draft['content'],
        timestamp=draft['timestamp'],
    ).model_dump(mode='json', by_alias=True)
    digest = profile_content_digest(snapshot)
    ref = ProfileSourceSnapshotRefV1(
        provider='chat',
        contract_version='profile.chat-source.v2',
        snapshot_id='chat_source_' + digest,
    )
    event = ProfileSourceEventV1(
        event_id='chat_source_event_'
        + profile_content_digest([scope.model_dump(mode='json', by_alias=True), message_id, 1, digest]),
        scope=scope,
        source_type='chat',
        source_id=message_id,
        source_revision=1,
        publication_revision=1,
        event_kind='publish',
        occurred_at=datetime.fromisoformat(state['createdAt']),
        content_digest=digest,
        snapshot_ref=ref,
    )
    return {
        'consumerRevision': CHAT_PROFILE_CONSUMER,
        'snapshot': snapshot,
        'contentDigest': digest,
        'event': event.model_dump(mode='json', by_alias=True),
        'draftDigest': state['draftDigest'],
        'deliveryManifestDigest': state['deliveryManifestDigest'],
        'deliveryPolicy': state['deliveryPolicy'],
        'createdAt': state['createdAt'],
    }


def validate_material(value):
    value = deepcopy(value)
    event = ProfileSourceEventV1.model_validate(value['event'])
    snapshot = ProfileChatSourceSnapshotV2.model_validate(value['snapshot'])
    canonical = snapshot.model_dump(mode='json', by_alias=True)
    digest = profile_content_digest(canonical)
    expected = ProfileSourceSnapshotRefV1(
        provider='chat',
        contract_version='profile.chat-source.v2',
        snapshot_id='chat_source_' + digest,
    )
    if (
        canonical != value['snapshot']
        or value.get('consumerRevision') != CHAT_PROFILE_CONSUMER
        or value.get('contentDigest') != digest
        or event.content_digest != digest
        or event.snapshot_ref != expected
        or event.scope != snapshot.scope
        or event.source_type != 'chat'
        or event.source_id != snapshot.source_id
        or event.source_revision != snapshot.source_revision
        or event.publication_revision != 1
        or event.event_kind != 'publish'
    ):
        raise ValueError('Chat immutable source material binding differs')
    raw_draft = {
        'contractVersion': CHAT_PROFILE_DRAFT,
        'consumerRevision': CHAT_PROFILE_CONSUMER,
        'userId': snapshot.scope.user_id,
        'sessionId': snapshot.session_id,
        'sourceId': snapshot.source_id,
        'sourceRevision': snapshot.source_revision,
        'role': snapshot.role,
        'content': snapshot.content,
        'timestamp': snapshot.timestamp,
    }
    ChatProfileDeliveryPolicy(**value['deliveryPolicy'])
    binding = {
        'messageId': snapshot.source_id,
        'sourceRevision': snapshot.source_revision,
        'publicationRevision': event.publication_revision,
        'deliveryMode': CHAT_PROFILE_DELIVERY_MODE,
        'consumerRevision': CHAT_PROFILE_CONSUMER,
        'draftDigest': value['draftDigest'],
        'deliveryPolicy': value['deliveryPolicy'],
        'createdAt': value['createdAt'],
    }
    created = datetime.fromisoformat(value['createdAt'])
    expected_event_id = 'chat_source_event_' + profile_content_digest(
        [
            event.scope.model_dump(mode='json', by_alias=True),
            event.source_id,
            1,
            digest,
        ]
    )
    if (
        event.source_revision != 1
        or value['draftDigest'] != profile_content_digest(raw_draft)
        or value['deliveryManifestDigest'] != profile_content_digest(binding)
        or created.tzinfo is None
        or created.utcoffset() is None
        or event.occurred_at != created
        or event.event_id != expected_event_id
    ):
        raise ValueError('Chat source delivery policy or event identity differs from the saved draft')
    return value, event


class MongoChatSourceMaterialStore:
    """Dedicated immutable Chat collection; construction/reads do not ensure indexes or publish."""

    def __init__(self, collection):
        self.collection = collection

    @staticmethod
    def key(scope, snapshot_id):
        return 'chat_material_' + profile_content_digest([scope.model_dump(mode='json', by_alias=True), snapshot_id])

    async def put_once(self, value):
        value, event = validate_material(value)
        key = self.key(event.scope, event.snapshot_ref.snapshot_id)
        try:
            result = await self.collection.insert_one({'_id': key, **value})
            if not result.acknowledged:
                raise RuntimeError('Chat source material was not durably acknowledged')
        except DuplicateKeyError:
            existing = await self.get(event.snapshot_ref.snapshot_id, scope=event.scope)
            if existing != value:
                raise ValueError('one Chat source material identity has conflicting content')
        return deepcopy(value)

    async def get(self, snapshot_id, *, scope):
        row = await self.collection.find_one({'_id': self.key(scope, snapshot_id)})
        if row is None:
            return None
        row = deepcopy(row)
        row.pop('_id', None)
        value, event = validate_material(row)
        if event.scope != scope or event.snapshot_ref.snapshot_id != snapshot_id:
            raise PermissionError('stored Chat material has another owner or snapshot identity')
        return value


class ChatSourceSnapshotReader:
    """Public Profile reader; it cannot read mutable sessions, normalize text or start a task."""

    def __init__(self, store):
        self.store = store

    async def read_source_snapshot(self, snapshot_ref, *, scope):
        if snapshot_ref.provider != 'chat' or snapshot_ref.contract_version != 'profile.chat-source.v2':
            raise ValueError('unsupported Chat source snapshot selection')
        value = await self.store.get(snapshot_ref.snapshot_id, scope=scope)
        if value is None:
            raise FileNotFoundError('the exact Chat source snapshot is unavailable')
        value, event = validate_material(value)
        if event.scope != scope or event.snapshot_ref != snapshot_ref:
            raise PermissionError('Chat source snapshot scope or selection differs')
        return deepcopy(value['snapshot'])
