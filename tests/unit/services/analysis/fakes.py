"""Isolated document provenance stores for shared Analysis/Record tests."""

from tests.unit._support.document_profile_composition.analysis.fakes import (
    FakeDocumentCollection as FakeDocumentCollection,
)
from tests.unit._support.document_profile_composition.analysis.fakes import (
    InMemoryDocumentTraceStore as InMemoryDocumentTraceStore,
)
from tests.unit._support.document_profile_composition.analysis.fakes import (
    MemoryMongoCollection as MemoryMongoCollection,
)
from tests.unit._support.document_profile_composition.analysis.fakes import MemoryMongoCursor as MemoryMongoCursor
