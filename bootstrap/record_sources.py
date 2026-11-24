"""Record-owned resources precede Profile; publication depends on its public ports."""

from dataclasses import dataclass

from adapters.record.profile_source import PublicRecordSourceReceiver
from adapters.record.profile_source import create_record_profile_source_bundle
from adapters.record.profile_source import create_record_source_material_reader
from adapters.record.profile_source_store import create_record_source_store
from adapters.record.source_admission import create_public_record_source_admission
from adapters.record.source_admission import create_record_source_admission_owner
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION


@dataclass(frozen=True, slots=True)
class RecordSourceResources:
    store: object
    material_reader: object
    owner: object


@dataclass(frozen=True, slots=True)
class RecordSourceRuntime:
    router: object
    completion_store: object
    worker: object


def build_record_source_resources(*, completion_collection, session_cache, lease_manager, clock=None):
    """Construct immutable readers and an owner from configured permanent namespaces.

    All handles share the supplied completion collection's existing database and
    client. Public adapters select PRIMARY and majority+j for source/admission
    writes. Construction performs no I/O, DDL or TTL setup. Register material_reader
    and owner with Profile before constructing the delivery runtime. The optional
    owner clock returns an aware datetime, matching its public admission contract.
    """
    if session_cache is None or lease_manager is None:
        raise ValueError('Record source ownership requires the actual session cache and lease manager')
    database, prefix = completion_collection.database, completion_collection.name
    store = create_record_source_store(database[prefix + '_record_profile_sources'])
    owner = create_record_source_admission_owner(
        admission_collection=database[prefix + '_record_source_admissions'],
        completion_collection=completion_collection,
        source_store=store,
        session_cache=session_cache,
        lease_manager=lease_manager,
        clock=clock,
    )
    return RecordSourceResources(
        store=store, material_reader=create_record_source_material_reader(store=store), owner=owner
    )


def create_application_record_source_resources(*, session_cache, lease_manager, clock=None):
    from db.mongodb import collection_sessions

    return build_record_source_resources(
        completion_collection=collection_sessions, session_cache=session_cache, lease_manager=lease_manager, clock=clock
    )


def build_record_source_runtime(
    *, resources, legacy, completion_store, inbox, presence, fresh_admission, policy=None, clock=None
):
    """Return a paired router/completion bridge and only the new background worker.

    This explicitly selects the new consumer for new eligible Record sessions;
    it neither installs a default nor starts workers. Historical selections remain
    owned by the public router. Root must inject both returned business adapters,
    schedule worker.recover_pending, and keep legacy Profile recovery separate.
    The optional delivery clock returns timestamp seconds, as its public API does.
    """
    if not isinstance(resources, RecordSourceResources):
        raise TypeError('the fixed Record source resources are required')
    admission = create_public_record_source_admission(
        owner=resources.owner, inbox=inbox, presence=presence, admission=fresh_admission
    )
    bundle = create_record_profile_source_bundle(
        legacy=legacy,
        completion_store=completion_store,
        store=resources.store,
        admission=admission,
        receiver=PublicRecordSourceReceiver(inbox),
        completion_evidence=resources.owner.evidence,
        default_revision=RECORD_PROFILE_SOURCE_REVISION,
        policy=policy,
        clock=clock,
    )
    return RecordSourceRuntime(router=bundle.source, completion_store=bundle.completion_store, worker=bundle.worker)
