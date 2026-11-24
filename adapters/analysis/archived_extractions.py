"""Assembly of Document's pure archive reader from its existing resources."""

from pymongo import ReadPreference

from adapters.analysis.document_index import MongoDocumentIndexStore
from modules.analysis.archived_extractions import DocumentArchivedExtractionReader


def create_document_archived_extraction_reader(
    *, processing, compatibility=None, managed=None, lifecycle=None, current_source=None
):
    if lifecycle is None and managed is None:
        raise ValueError('an explicit Document source lifecycle reader is required')
    index = processing._document_reuse.index
    return DocumentArchivedExtractionReader(
        index=MongoDocumentIndexStore(index._collection.with_options(read_preference=ReadPreference.PRIMARY)),
        traces=processing._document_trace_store,
        understanding=compatibility.understanding if compatibility is not None else None,
        lifecycle=lifecycle if lifecycle is not None else managed.lifecycle,
        managed=managed,
        current_source=current_source,
    )
