"""Explicit compatibility-history archive through the public Profile port only."""

import hashlib
import json
from copy import deepcopy

from modules.analysis.application.legacy_publication import _inherit_published_bindings
from modules.analysis.compatibility import _legacy_revision
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.managed_content import managed_public_content_v1 as report_public_content
from modules.analysis.managed_content import managed_source_content_v1 as report_source_content
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest


def archive_result(bundle):
    result = report_source_content(bundle.understanding.payload['interpretation'])
    result.pop('verifiedObservationPointers', None)
    result.pop('ingestionObservations', None)
    return result


class DocumentManagedArchive:
    def __init__(self, *, records, profile_archive):
        self.records, self.profile_archive = records, profile_archive

    async def save(self, *, scope, bundle, result, source_key=None):
        metadata = bundle.material.payload['documentMetadata']
        request = {
            'scope': scope.as_dict(),
            'canonicalReportId': bundle.material.source_id,
            'reportId': bundle.material.source_id,
            'sourceFingerprint': metadata['sourceFingerprint'],
            'documentId': metadata['documentId'],
            'result': report_source_content(deepcopy(result)),
            'sourceKey': source_key,
        }
        if len(canonical_json(request).encode('utf-8')) > 4_000_000:
            raise DocumentCompleteSourceUnavailable('compatibility archive exceeds the supported byte bound')
        ref = 'document_managed_archive_' + digest(request)
        stored = await self.records.get('managed_archive', scope, ref)
        if stored is None:
            receipt = await self.profile_archive.archive(scope=scope, request=request)
            stored = {'request': request, 'receipt': receipt}
            self._verify(stored, scope, ref)
            await self.records.put('managed_archive', scope, ref, stored)
        return ref, await self.read(scope=scope, archive_ref=ref)

    @staticmethod
    def _verify(saved, scope, ref):
        request, receipt = saved['request'], saved['receipt']
        if request['scope'] != scope.as_dict() or ref != 'document_managed_archive_' + digest(request):
            raise DocumentCompleteSourceUnavailable('the fixed compatibility archive scope or request changed')
        revision = _legacy_revision(receipt['legacySourceRevision'])
        expected_revision = (
            1
            if request['sourceKey'] is None
            else 2 + int(hashlib.sha256(request['sourceKey'].encode('utf-8')).hexdigest()[:12], 16)
        )
        identity = {
            key: request[key]
            for key in ('scope', 'reportId', 'canonicalReportId', 'documentId', 'sourceFingerprint', 'result')
        }
        identity['legacySourceRevision'] = revision
        if (
            revision != expected_revision
            or receipt['contentDigest'] != digest(identity)
            or any(receipt[key] != identity[key] for key in identity if key != 'result')
        ):
            raise DocumentCompleteSourceUnavailable('the actual Profile archive acknowledgement differs')

    async def read(self, *, scope, archive_ref):
        saved = await self.records.require('managed_archive', scope, archive_ref)
        self._verify(saved, scope, archive_ref)
        return saved

    async def base(self, *, scope, origin, current):
        if origin.material.source_id != current.material.source_id:
            raise DocumentCompleteSourceUnavailable('base history belongs to another source')
        initial = archive_result(origin)
        first_ref, first = await self.save(scope=scope, bundle=origin, result=initial)
        current_result = _inherit_published_bindings(archive_result(current), initial)
        if current_result == initial:
            return first_ref, first
        content_hash = hashlib.sha256(
            json.dumps(current_result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        metadata = current.material.payload['documentMetadata']
        key = f'health_report:{metadata["documentId"]}:{metadata["legacyPublicationVersion"]}:{content_hash}'
        return await self.save(scope=scope, bundle=current, result=current_result, source_key=key)

    @staticmethod
    def public_result(saved):
        request, receipt = saved['request'], saved['receipt']
        result = report_public_content(request['result'])
        result.pop('ingestionObservations', None)
        result.update(reportId=request['reportId'], **request['scope'])
        if 'processing' in result or receipt['legacySourceRevision'] > 1:
            result['sourceRevision'] = receipt['legacySourceRevision']
        # Detach nested values from both the immutable stored acknowledgement and
        # the caller. This is historical content, not a current ingestion status.
        return json.loads(canonical_json(result))
