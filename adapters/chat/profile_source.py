"""Explicit Chat legacy/new source routing; default production wiring lives in bootstrap."""

from datetime import UTC
from datetime import datetime

from modules.chat.profile_source import CHAT_PROFILE_DELIVERY_MODE
from modules.chat.profile_source import ChatProfileDeliveryPolicy
from modules.chat.profile_source import prepare_user_message_source


class ChatProfileSourceRouter:
    def __init__(self, *, publication, owner, legacy, policy=ChatProfileDeliveryPolicy(), clock=None):
        self.publication, self.owner, self.legacy, self.policy = publication, owner, legacy, policy
        self.clock = clock or (lambda: datetime.now(UTC))

    def prepare_user_message(self, session, message):
        message_id = message.get('messageId')
        legacy_id = message_id.strip() if isinstance(message_id, str) else message_id
        existing = [
            item
            for item in session.get('chatProfileIngestion', [])
            if isinstance(item, dict) and item.get('messageId') in (message_id, legacy_id)
        ]
        if len(existing) > 1:
            raise ValueError('duplicate Chat source markers')
        if existing:
            if existing[0].get('deliveryMode') in (None, 'legacy'):
                return self.legacy.prepare_user_message(session, message)
            if existing[0].get('messageId') != message_id:
                raise ValueError('an accepted Chat source identity cannot be changed')
        if not existing and self._requires_legacy_input(session, message):
            self.legacy.prepare_user_message(session, message)
            # The old implementation normalizes its marker ID. Match its actual
            # acknowledgement without changing the saved raw business message.
            states = [
                item
                for item in session['chatProfileIngestion']
                if isinstance(item, dict) and item.get('messageId') == legacy_id
            ]
            if len(states) != 1:
                raise ValueError('legacy Chat source did not return one matching marker')
            states[0].update(deliveryMode='legacy', consumerRevision='chat-profile-legacy-v1')
            return None
        return prepare_user_message_source(session, message, policy=self.policy, now=self.clock())

    @staticmethod
    def _requires_legacy_input(session, message):
        # Existing HTTP/business inputs do not share the narrower public source
        # DTO bounds. Keep their raw identities/content, without trimming them.
        fields = (
            (session.get('userId'), 256),
            (session.get('sessionId'), 256),
            (message.get('messageId'), 256),
            (message.get('content'), 128 * 1024),
            (message.get('timestamp'), 128),
        )
        return any(isinstance(value, str) and len(value) > bound for value, bound in fields)

    async def ensure_queued(self, *, user_id, message_id):
        try:
            session = await self.owner.read_session(user_id=user_id, message_id=message_id)
            if session is None:
                return None
            states = [
                item
                for item in session.get('chatProfileIngestion', [])
                if isinstance(item, dict) and item.get('messageId') == message_id
            ]
            if len(states) == 1 and states[0].get('deliveryMode') == CHAT_PROFILE_DELIVERY_MODE:
                return await self.publication.ensure_queued(user_id=user_id, message_id=message_id)
            if not states or len(states) == 1 and states[0].get('deliveryMode') in (None, 'legacy'):
                return await self.legacy.ensure_queued(user_id=user_id, message_id=message_id)
            return None  # Unknown/duplicate modes never enter legacy or new delivery.
        except Exception:
            return None  # Business save already succeeded; a background scan can retry.

    async def recover_pending(self, *, limit=100):
        count = 0
        try:
            count = await self.publication.recover_pending(limit=limit)
        finally:
            await self.legacy.recover_pending(limit=limit)
        return count


def create_chat_source_resources(*, collection, sessions):
    """Pure resources first: register both readers with Profile before building delivery."""
    from types import SimpleNamespace

    from adapters.chat.source_admission import ChatOwnerFenceReader
    from adapters.chat.source_material import ChatSourceSnapshotReader
    from adapters.chat.source_material import MongoChatSourceMaterialStore
    from adapters.chat.source_store import MongoChatSavedSourceReader
    from adapters.chat.source_store import MongoChatSourceStore
    from adapters.chat.source_store import primary_session_collection
    from adapters.chat.source_store import primary_source_collection

    owned = primary_source_collection(collection)
    store = MongoChatSourceStore(owned)
    owner = MongoChatSavedSourceReader(primary_session_collection(sessions))
    materials = MongoChatSourceMaterialStore(owned)
    return SimpleNamespace(
        store=store,
        owner=owner,
        materials=materials,
        snapshot_reader=ChatSourceSnapshotReader(materials),
        fence_reader=ChatOwnerFenceReader(store=store, owner=owner),
    )


def create_chat_profile_source(
    *, resources, inbox, presence, fresh_admission, legacy, policy=ChatProfileDeliveryPolicy(), clock=None
):
    """Explicit opt-in composition; no I/O, background task or source migration at construction."""
    from adapters.chat.source_admission import ChatMaterialPreparer
    from adapters.chat.source_admission import ChatProfileSourceReceiver
    from adapters.chat.source_admission import ChatSourceAdmissionAdapter
    from modules.chat.profile_publication import ChatSourcePublicationService

    worker = ChatSourcePublicationService(
        store=resources.store,
        owner=resources.owner,
        materials=ChatMaterialPreparer(owner=resources.owner, materials=resources.materials),
        admission=ChatSourceAdmissionAdapter(
            store=resources.store,
            owner=resources.owner,
            inbox=inbox,
            presence=presence,
            fresh_admission=fresh_admission,
        ),
        receiver=ChatProfileSourceReceiver(inbox),
        **({'clock': clock} if clock is not None else {}),
    )
    return ChatProfileSourceRouter(
        publication=worker,
        owner=resources.owner,
        legacy=legacy,
        policy=policy,
        clock=(lambda: datetime.fromtimestamp(clock(), UTC)) if clock is not None else None,
    )
