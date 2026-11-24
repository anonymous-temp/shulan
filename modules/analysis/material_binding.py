"""Small provenance frozen by the actual failed execution, before later recovery."""

from modules.analysis.preparation import document_metadata
from modules.analysis.preparation import payload_digest


def failure_material_binding(document, parsed):
    if (
        not isinstance(parsed, dict)
        or parsed.get('success') is not True
        or not isinstance(parsed.get('text'), str)
        or not parsed['text'].strip()
        or not all(document.get('payloadRefs', {}).get(name) for name in ('original_file', 'docmind_result'))
        or not (
            document.get('status') == 'failed'
            or (document.get('status') == 'completed' and document.get('processingStatus') == 'unavailable')
        )
    ):
        return None
    binding = {
        'scope': {'userId': document['userId'], 'subjectId': document['subjectId']},
        'documentMetadata': document_metadata(document),
        'originalArtifactRef': document['payloadRefs']['original_file'],
        'parseArtifactRef': document['payloadRefs']['docmind_result'],
        'parseContentDigest': payload_digest(parsed),
        'interpretationRevision': document.get('interpretationVersion'),
        'validationRevision': document.get('interpretationValidatedVersion'),
        'interpretationRound': document.get('interpretationRound', 0),
        'interpretationResultRound': document.get('interpretationResultRound'),
        'processingStatus': document.get('processingStatus'),
        'errorType': document.get('errorType'),
    }
    return {**binding, 'bindingDigest': payload_digest(binding)}
