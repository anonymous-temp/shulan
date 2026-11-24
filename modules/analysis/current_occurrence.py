"""Bind verified Report original locations to each fixed Analysis observation."""

from modules.analysis.complete_source import _require

# The storage projection can change independently of existing physical locators.
OCCURRENCE_REVISION = 'report-understanding-current.v4'
_LOCATOR_REVISION = 'report-understanding-current.v3'
OCCURRENCE_PROJECTION_REVISIONS = frozenset({OCCURRENCE_REVISION, _LOCATOR_REVISION})


def physical_occurrences(projection, bundles):
    """Use only archived occurrence proof, scoped and validated by the caller."""
    fixed = projection.payload
    if fixed['manifest']['interpretationProjectionRevision'] not in OCCURRENCE_PROJECTION_REVISIONS:
        return {}, {}
    materials = {
        bundle.material.payload['documentId']: (
            bundle.material.payload['contentSha256'],
            bundle.material.payload['parseContentDigest'],
            {row['observationRef'] for row in bundle.facts.payload['observations']},
        )
        for bundle in bundles
    }
    positions, aliases = {}, {}
    for index, row in enumerate(fixed['projection'].get('supplementaryHospitalData', ())):
        pointer = f'/reportSource/supplementaryHospitalData/{index}'
        locator = row.get('sourceOccurrence')
        refs = row.get('analysisObservationRefs')
        if locator is None and refs is None:
            continue
        doc, checksum = row.get('sourceDocumentId'), row.get('sourceChecksum')
        _require(isinstance(doc, str) and doc in materials, 'Report original occurrence document is not fixed')
        original_hash, parse_hash, eligible = materials[doc]
        _require(
            checksum == original_hash
            and isinstance(locator, str)
            and 0 < len(locator) <= 512
            and locator.strip() == locator,
            'Report original occurrence differs from archived material',
        )
        proof = {
            'documentId': doc,
            'sourceChecksum': checksum,
            'locatorRevision': _LOCATOR_REVISION + ':' + parse_hash,
            'locator': locator,
            'proofSnapshotRef': projection.snapshot_id,
        }
        positions[pointer] = proof
        if refs is None:
            continue
        _require(
            isinstance(refs, list)
            and 0 < len(refs) <= 1000
            and all(isinstance(ref, str) and ref in eligible for ref in refs)
            and len(set(refs)) == len(refs),
            'Report original occurrence has an invalid fixed Analysis pointer',
        )
        for ref in refs:
            alias = doc, ref
            _require(alias not in aliases, 'one fixed Analysis pointer names multiple original occurrences')
            aliases[alias] = pointer, proof
    return positions, aliases
