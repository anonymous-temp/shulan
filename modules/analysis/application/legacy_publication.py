"""The sole compatibility gateway for historical Document-to-Profile publication.

This preserves legacy source identities and hash revisions. It is intentionally
separate from snapshot reads and the new source-wide publication event ledger.
"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from modules.analysis.application.extraction import normalize_document_time
from modules.analysis.snapshots import DocumentScope
from utils.report_content import report_source_content


def _inherit_published_bindings(current, previous):
    """Storage compatibility only: keep a uniquely matching existing row's identifier.

    This runs after JSON acceptance and never rejects or reinterprets model fields.
    """
    published = previous.get('sourceBindings')
    if not isinstance(published, dict) or not published:
        return current

    def rows(node, path=''):
        for index, item in enumerate(node.get('observations', [])):
            yield f'{path}/observations/{index}', item, node.get('reportTime')
        if node.get('reportType') == 'imaging' and isinstance(node.get('expansion'), dict):
            for field, name in (('findings', '影像所见'), ('diagnosis', '影像诊断')):
                if value := node['expansion'].get(field):
                    yield f'{path}/expansion/{field}', {'itemName': name, 'resultValue': value}, node.get('reportTime')
        for index, child in enumerate(node.get('sections', [])):
            yield from rows(child, f'{path}/sections/{index}')

    def key(item, report_time):
        return (
            item.get('itemName'),
            str(item.get('resultValue')),
            normalize_document_time(item.get('observedAt') or report_time),
            *(item.get(field) for field in ('unit', 'method', 'specimen')),
        )

    metadata_fields = ('flag', 'referenceRange', 'kind')

    def physical_rows(result):
        grouped = {}
        sections = result.get('sections', [])
        mirrors = {}
        for index, item in enumerate(result.get('observations', [])):
            section = item.get('sourceSection')
            if type(section) is int and 0 <= section < len(sections):
                identity = key(item, sections[section].get('reportTime'))
                metadata = tuple(item.get(field) for field in metadata_fields)
                mirrors.setdefault((section, identity, metadata), []).append(f'/observations/{index}')
        for pointer, item, report_time in rows(result):
            section = item.get('sourceSection')
            if pointer.startswith('/observations/') and type(section) is int and 0 <= section < len(sections):
                continue
            identity = key(item, report_time)
            aliases = ()
            if pointer.startswith('/sections/') and '/observations/' in pointer:
                section = int(pointer.split('/')[2])
                metadata = tuple(item.get(field) for field in metadata_fields)
                aliases = mirrors.get((section, identity, metadata), ())
            grouped.setdefault(identity, []).append((item, (pointer, *aliases)))
        return grouped

    existing = physical_rows(previous)
    bindings = dict(current.get('sourceBindings') or {})
    used = set(bindings.values())

    def old_bindings(pointers):
        return {
            binding
            for pointer in pointers
            if isinstance(binding := published.get(pointer), str) and binding.startswith('v6:')
        }

    binding_counts = {}
    for candidates in existing.values():
        for _, pointers in candidates:
            for binding in old_bindings(pointers):
                binding_counts[binding] = binding_counts.get(binding, 0) + 1

    for identity, candidates in physical_rows(current).items():
        matches = existing.get(identity, [])
        if len(matches) != 1 or len(candidates) != 1:
            continue
        ((old_item, old_pointers),) = matches
        ((item, pointers),) = candidates
        prior = old_bindings(old_pointers)
        if len(prior) != 1 or any(pointer in bindings for pointer in pointers):
            continue
        binding = next(iter(prior))
        if binding_counts[binding] != 1 or binding in used:
            continue
        if any(
            old_item.get(field) is not None and item.get(field) is not None and old_item[field] != item[field]
            for field in metadata_fields
        ):
            continue
        bindings.update(dict.fromkeys(pointers, binding))
        used.add(binding)
    return {**current, 'sourceBindings': bindings} if bindings else current


class LegacyPublicationGateway:
    def __init__(self, source, ledger=None):
        self.source = source
        self.ledger = ledger

    async def _invoke(self, method, kwargs):
        if self.ledger is None:
            return await getattr(self.source, method)(**kwargs)
        scope = DocumentScope(kwargs['user_id'], kwargs['subject_id'])
        source_id = kwargs.get('canonical_report_id') or kwargs['report_id']
        ticket = uuid4().hex
        await self.ledger.begin_legacy(scope, source_id, ticket)
        # An uncertain external write remains an explicit switch blocker. A
        # timeout is not proof that the old Profile sink rejected that write.
        result = await getattr(self.source, method)(**kwargs)
        await self.ledger.finish_legacy(scope, source_id, ticket)
        return result

    async def save(self, **kwargs):
        return await self._invoke('save', kwargs)

    async def save_revision(self, **kwargs):
        return await self._invoke('save_revision', kwargs)

    async def publish_document(self, document, result):
        if self.ledger is None:
            return await self._publish_document(document, result)
        scope = DocumentScope(document['userId'], document['subjectId'])
        ticket = uuid4().hex
        await self.ledger.begin_legacy(scope, document['canonicalReportId'], ticket)
        saved = await self._publish_document(document, result)
        await self.ledger.finish_legacy(scope, document['canonicalReportId'], ticket)
        return saved

    async def _publish_document(self, document, result):
        """Select the current immutable source revision, never the first snapshot."""
        result = report_source_content(result)
        # Existing gated snapshots stay immutable. Removing the obsolete
        # allowlist creates a supplemental revision for previously omitted rows.
        result.pop('verifiedObservationPointers', None)
        canonical = await self.source.save(
            report_id=document['canonicalReportId'],
            user_id=document['userId'],
            subject_id=document['subjectId'],
            source_fingerprint=document['sourceFingerprint'],
            result=result,
            document_id=document['_id'],
            canonical_report_id=document['canonicalReportId'],
        )
        canonical_result = report_source_content(canonical.record.result)
        canonical_result.pop('ingestionObservations', None)
        result = _inherit_published_bindings(result, canonical_result)
        if canonical_result == result:
            return canonical
        digest = hashlib.sha256(
            json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        ).hexdigest()
        return await self.source.save_revision(
            canonical_report_id=document['canonicalReportId'],
            user_id=document['userId'],
            subject_id=document['subjectId'],
            source_key=f'health_report:{document["_id"]}:{document.get("interpretationVersion", "legacy")}:{digest}',
            result=result,
        )
