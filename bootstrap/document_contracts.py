"""Compose Document public contracts without opening an extra client or owning it."""

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.compatibility import create_document_compatibility_service


def build_document_compatibility_service(
    *, processing_service, profile_service, document_collection, publication_ledger=None
):
    """Keep all compatibility writers on the same explicit legacy publication gate.

    Collections are derived from the configured Document collection, so deployment
    namespaces stay isolated. The built-in unique _id indexes are sufficient. No
    TTL or DDL is introduced and construction performs no database/model I/O.
    """
    options = {
        'write_concern': WriteConcern(w='majority', j=True, wtimeout=5000),
        'read_preference': ReadPreference.PRIMARY,
    }
    database = document_collection.database
    prefix = document_collection.name
    return create_document_compatibility_service(
        processing_service=processing_service,
        profile_service=profile_service,
        snapshots=database[prefix + '_snapshots'].with_options(**options),
        snapshot_chunks=database[prefix + '_snapshot_chunks'].with_options(**options),
        compatibility_records=database[prefix + '_compatibility'].with_options(**options),
        publication_ledger=publication_ledger,
    )


def create_application_document_compatibility_service(*, processing_service, profile_service, publication_ledger=None):
    from db.mongodb import collection_document_index

    return build_document_compatibility_service(
        processing_service=processing_service,
        profile_service=profile_service,
        document_collection=collection_document_index,
        publication_ledger=publication_ledger,
    )


def build_document_material_reader(*, document_collection, traces, trace_chunks):
    from adapters.analysis.materials import create_document_material_reader

    options = {
        'write_concern': WriteConcern(w='majority', j=True, wtimeout=5000),
        'read_preference': ReadPreference.PRIMARY,
    }
    database, prefix = document_collection.database, document_collection.name
    return create_document_material_reader(
        snapshots=database[prefix + '_snapshots'].with_options(**options),
        snapshot_chunks=database[prefix + '_snapshot_chunks'].with_options(**options),
        traces=traces.with_options(**options),
        trace_chunks=trace_chunks.with_options(**options),
    )


def create_application_document_material_reader():
    from db.mongodb import collection_document_index
    from db.mongodb import collection_document_trace_chunks
    from db.mongodb import collection_document_traces

    return build_document_material_reader(
        document_collection=collection_document_index,
        traces=collection_document_traces,
        trace_chunks=collection_document_trace_chunks,
    )


def build_document_observation_reader(*, collection, profile_legacy_ingestion_reader):
    from adapters.analysis.observation_reader import create_document_observation_reader
    from adapters.analysis.profile_ingestion import ProfileDocumentObservationIngestionReader

    return create_document_observation_reader(
        collection=collection,
        ingestion_reader=ProfileDocumentObservationIngestionReader(profile_legacy_ingestion_reader),
    )


def create_application_document_observation_reader(*, profile_legacy_ingestion_reader):
    from db.mongodb import collection_document_observations

    return build_document_observation_reader(
        collection=collection_document_observations,
        profile_legacy_ingestion_reader=profile_legacy_ingestion_reader,
    )
