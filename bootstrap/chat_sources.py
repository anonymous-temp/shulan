"""Chat source resources precede Profile; delivery is constructed after public ports exist."""

from dataclasses import dataclass

from adapters.chat.profile_source import ChatProfileSourceRouter
from adapters.chat.profile_source import create_chat_profile_source
from adapters.chat.profile_source import create_chat_source_resources
from modules.chat.profile_publication import ChatSourcePublicationService
from modules.chat.profile_source import ChatProfileDeliveryPolicy


@dataclass(frozen=True, slots=True)
class ChatSourceRuntime:
    router: ChatProfileSourceRouter
    worker: ChatSourcePublicationService


def build_chat_source_resources(*, sessions):
    """Derive one permanent namespace; reuse the existing client without I/O or DDL.

    Register snapshot_reader and fence_reader with the exact public Profile
    contracts before building delivery. Neither reader depends on a worker.
    """
    collection = sessions.database[sessions.name + '_chat_profile_sources']
    return create_chat_source_resources(collection=collection, sessions=sessions)


def create_application_chat_source_resources():
    from db.mongodb import collection_sessions

    return build_chat_source_resources(sessions=collection_sessions)


def build_chat_source_runtime(
    *, resources, inbox, presence, fresh_admission, legacy, policy=ChatProfileDeliveryPolicy(), clock=None
):
    """Install explicitly; return only the new worker to avoid duplicate legacy scans.

    The root injects router into Chat and schedules worker.recover_pending. The
    existing Profile ingestion worker continues to recover the legacy source.
    Construction admits no source, starts no task and performs no storage I/O.
    """
    router = create_chat_profile_source(
        resources=resources,
        inbox=inbox,
        presence=presence,
        fresh_admission=fresh_admission,
        legacy=legacy,
        policy=policy,
        clock=clock,
    )
    return ChatSourceRuntime(router=router, worker=router.publication)
