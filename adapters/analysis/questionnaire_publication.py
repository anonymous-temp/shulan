"""Document-owned durable Q commands; all cross-domain access uses public ports."""

import json

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.source_admission import MongoDocumentAdmissionStore
from modules.analysis.application.legacy_publication import LegacyPublicationGateway
from modules.analysis.questionnaire_publication import DocumentQuestionnaireAttachmentPublisher
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationConflict
from modules.analysis.snapshots import canonical_json


class MongoQuestionnairePublicationStore(MongoDocumentAdmissionStore):
    async def get(self, kind, scope, identity):
        value = await super().get(kind, scope, identity)
        return None if value is None else json.loads(canonical_json(value))

    async def put_acknowledgement(self, scope, identity, receipt):
        stored = await self._insert('questionnaire_attachment', scope, identity, receipt)
        # Queue state may advance between concurrent saves. Preserve the first
        # real ACK; neither created nor current queue state defines source identity.
        fields = ('artifactId', 'reportId', 'legacySourceRevision', 'inputInterpretationDigest', 'storedResultDigest')
        if any(stored.get(key) != receipt[key] for key in fields):
            raise QuestionnairePublicationConflict('the fixed legacy attachment acknowledgement conflicts')
        return json.loads(canonical_json(stored))


def create_document_questionnaire_attachment_publisher(
    *, submitted_reader, source_reader, profile_inbox, health_report_source, publication_ledger, command_records
):
    """Zero I/O construction, explicit providers only; production defaults are untouched.

    submitted_reader is Q's public submitted-material v1 reader. source_reader is
    Q's public ProfileSourceSnapshotReaderPort for the fixed v2 envelope; no Q
    private archive/store is inspected here. profile_inbox supplies get_cutover.
    health_report_source is the public existing legacy service, preserving its
    formal queue and recovery semantics, wrapped by the shared Document gate.

    command_records is a dedicated Q-attachment collection/namespace; do not mix
    it with canonical Document fresh-admission history. It requires PRIMARY,
    majority, unique _id and no TTL. It stores
    only immutable refs/manifests/ACKs, never large report bodies. Deployment owns
    indexes/resources. Q's independent worker owns admission, retry budget and
    repeated publish; constructing this provider starts no worker or model call.
    """
    if publication_ledger is None:
        raise ValueError('Questionnaire attachments require the shared Document publication ledger')
    records = MongoQuestionnairePublicationStore(
        command_records.with_options(read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority'))
    )
    return DocumentQuestionnaireAttachmentPublisher(
        submitted_reader=submitted_reader,
        source_reader=source_reader,
        ready_reader=profile_inbox,
        legacy_publication=LegacyPublicationGateway(health_report_source, publication_ledger),
        records=records,
    )
