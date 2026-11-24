"""Fixed provenance captured at the unpublished processing boundary.

Kept separate from DocumentInputSnapshot: adding these digests must not change
existing output-profile cache identities.
"""

import hashlib
import json
from dataclasses import dataclass

from modules.analysis.contracts import DocumentInputSnapshot


def payload_digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def document_metadata(document):
    return {
        'documentId': document['_id'],
        'canonicalReportId': document['canonicalReportId'],
        # Preserve the old publisher's exact identity, including an absent
        # interpretationVersion versus an explicitly stored null value.
        'sourceFingerprint': document.get('sourceFingerprint'),
        'legacyPublicationVersion': document.get('interpretationVersion', 'legacy'),
        'contentSha256': document['contentSha256'],
        'fileName': document['fileName'],
        'fileType': document['fileType'],
        'firstFileUrl': document.get('firstFileUrl'),
        'byteSize': document.get('byteSize'),
        'taskId': document.get('taskId'),
        'status': document.get('status'),
        'parserRevision': document.get('parserVersion'),
        'originalArtifactRef': document.get('payloadRefs', {}).get('original_file'),
        'legacySourceRevision': document.get('sourceRevision'),
        'requestedInterpretationRound': document.get('interpretationRound', 0),
    }


@dataclass(frozen=True, slots=True)
class DocumentPreparationBinding:
    input_snapshot: DocumentInputSnapshot
    parse_digest: str
    interpretation_digest: str
    _metadata_json: str

    @property
    def metadata(self):
        return json.loads(self._metadata_json)

    @classmethod
    def capture(cls, document, parsed, interpretation):
        return cls(
            DocumentInputSnapshot(
                parse_artifact_ref=document.get('payloadRefs', {}).get('docmind_result'),
                interpretation_artifact_ref=document.get('payloadRefs', {}).get('interpretation'),
                interpretation_revision=document.get('interpretationVersion'),
                validation_revision=document.get('interpretationValidatedVersion'),
                interpretation_round=document.get('interpretationResultRound', document.get('interpretationRound', 0)),
            ),
            payload_digest(parsed),
            payload_digest(interpretation),
            json.dumps(document_metadata(document), ensure_ascii=False, sort_keys=True, separators=(',', ':')),
        )
