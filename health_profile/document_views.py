"""Profile's standard and complete projections of fixed document material."""

from copy import deepcopy
from dataclasses import replace

from pymongo.errors import PyMongoError

from .contracts import ProfileScope
from .contracts import profile_content_digest
from .document_archive_contracts import ProfileDocumentArchiveChangedError
from .document_archive_contracts import ProfileDocumentArchiveReadError
from .models import FactStatus
from .pagination import decode_cursor
from .pagination import encode_cursor
from .report_ingestion import _observation_time
from .report_ingestion import _parse_report_time
from .report_ingestion import _report_observations
from .report_ingestion import _sha256_json

_CURSOR_KIND = 'profile-document-view'
_SOURCE_STATUSES = {'pending', 'active', 'not_published', 'withdrawn', 'unavailable'}
_ARCHIVE_STATUSES = {'ready', 'pending', 'unavailable', 'withdrawn'}


def validate_document_view(view, cursor, limit):
    if view not in ('standard', 'complete'):
        raise ValueError('view must be standard or complete')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit must be between 1 and 100')
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        raise ValueError('cursor must be a nonempty string')


class ProfileDocumentObservationViews:
    """Keep storage reads scoped and fixed; all display decisions stay here."""

    SCAN_BUDGET = 1000

    def __init__(self, *, archives, repository, legacy=None, require_profile_ownership=False, publications=None):
        self.archives, self.repository, self.legacy = archives, repository, legacy
        self.require_profile_ownership = require_profile_ownership
        self.publications = publications

    async def list_observations(
        self,
        user_id,
        subject_id,
        *,
        document_id=None,
        view='standard',
        cursor=None,
        limit=20,
    ):
        validate_document_view(view, cursor, limit)
        scope = {'userId': user_id, 'subjectId': subject_id, 'documentId': document_id, 'view': view}
        custody, ownership = await self._archive_access(user_id, subject_id)
        if ownership is not None:
            scope['profileOwnership'] = ownership
        state = self._decode(scope, cursor)
        if state.get('custody', 0) >= len(custody):
            raise ValueError('cursor archive position is invalid')
        items, documents, sources, fixed, missing_tokens = [], {}, {}, {}, {}
        scanned = 0
        incomplete = False
        while len(items) + sum(not value['_hasItems'] for value in documents.values()) < limit:
            if scanned >= self.SCAN_BUDGET:
                break
            if state['phase'] == 'legacy':
                if self.legacy is None:
                    state = {'phase': 'archives', 'after': None, 'current': None, 'custody': 0}
                    continue
                page = await self.legacy.list_observations(
                    user_id,
                    subject_id,
                    document_id=document_id,
                    cursor=state['after'],
                    limit=1,
                )
                rows = page['items']
                if len(rows) > 1:
                    raise ProfileDocumentArchiveReadError('legacy reader exceeded its bound')
                scanned += 1
                for original in rows:
                    source_id = original['canonicalReportId']
                    selected = await self.archives.read_source_state(
                        user_id=user_id,
                        subject_id=subject_id,
                        source_id=source_id,
                    )
                    if source_id in sources and sources[source_id] != selected:
                        raise ProfileDocumentArchiveChangedError('source changed during the page')
                    sources[source_id] = selected
                    if not selected.legacy_visible:
                        continue
                    if view == 'complete' and await self._has_archive(scope, original):
                        continue
                    item = deepcopy(original)
                    if view == 'complete':
                        source_status = (
                            'active'
                            if item.get('ingestionStatus') == 'completed'
                            else 'unavailable'
                            if item.get('ingestionStatus') == 'dead_letter'
                            else 'pending'
                        )
                        item.update(
                            extractionId=None,
                            resolutionStatus='resolved',
                            verificationStatus='unconfirmed',
                            factStatus=None,
                            sourceStatus=source_status,
                            publicationRevision=None,
                            _factPointer=item.get('sourcePointer'),
                            _factRevision=item.get('sourceRevision'),
                        )
                        key = ('document', original.get('documentId'), source_id)
                        documents[key] = {
                            'documentId': original.get('documentId'),
                            'documentIds': deepcopy(original.get('documentIds', [])),
                            'canonicalReportId': source_id,
                            'extractionId': None,
                            'archiveStatus': 'unavailable',
                            'sourceStatus': source_status,
                            'reason': 'legacy_complete_archive_unavailable',
                            '_hasItems': True,
                        }
                        incomplete = True
                    items.append(item)
                state['after'] = page.get('nextCursor')
                if state['after'] is None:
                    state = {'phase': 'archives', 'after': None, 'current': None, 'custody': 0}
                continue
            custody_subject, custody_sources = custody[state['custody']]
            custody_scope = {**scope, 'subjectId': custody_subject}
            current = state['current']
            if current is None:
                page = await self.archives.list_archived(
                    user_id=user_id,
                    subject_id=custody_subject,
                    document_id=document_id,
                    after_id=state['after'],
                    limit=1,
                )
                if len(page.entries) > 1:
                    raise ProfileDocumentArchiveReadError('archive reader exceeded its bound')
                if page.next_after_id is not None and page.next_after_id == state['after']:
                    raise ProfileDocumentArchiveReadError('archive cursor did not advance')
                descriptor = page.entries[0] if page.entries else None
                if descriptor is not None:
                    self._check_descriptor(custody_scope, descriptor)
                if descriptor is None or not self._authorized_archive(descriptor, custody_sources):
                    state = self._archive_position(state, page.next_after_id, len(custody))
                    if state is None:
                        break
                    scanned += 1
                    continue
                if descriptor.extraction_id is None:
                    scanned += 1
                    if view == 'complete' and not self.require_profile_ownership:
                        key = ('document', descriptor.document_id, descriptor.canonical_report_id)
                        documents.setdefault(key, {**self._document(descriptor), '_hasItems': False})
                        previous = missing_tokens.get(descriptor.canonical_report_id)
                        if previous is not None and previous != descriptor.lifecycle_token:
                            raise ProfileDocumentArchiveChangedError('source changed between missing archives')
                        missing_tokens[descriptor.canonical_report_id] = descriptor.lifecycle_token
                        incomplete = True
                    state = self._archive_position(state, page.next_after_id, len(custody))
                    if state is None:
                        break
                    continue
                current = {
                    'extraction': descriptor.extraction_id,
                    'token': descriptor.lifecycle_token,
                    'next': page.next_after_id,
                    'offset': 0,
                }
                state['current'] = current
            extraction = await self.archives.read_fixed(
                user_id=user_id,
                subject_id=custody_subject,
                extraction_id=current['extraction'],
                expected_lifecycle_token=current['token'],
                document_id=document_id,
            )
            descriptor = extraction.descriptor
            self._check_descriptor(custody_scope, descriptor)
            if not self._authorized_archive(descriptor, custody_sources):
                raise ProfileDocumentArchiveChangedError('archive subject ownership changed')
            if descriptor.extraction_id != current['extraction'] or descriptor.lifecycle_token != current['token']:
                raise ProfileDocumentArchiveChangedError('fixed archive changed')
            archive_key = (custody_subject, descriptor.extraction_id)
            fixed[archive_key] = descriptor.lifecycle_token
            scanned += 1
            owned_view = self.require_profile_ownership and descriptor.current_source
            if (
                not owned_view
                and view == 'complete'
                and current['offset'] > 0
                and descriptor.archive_status in {'pending', 'unavailable'}
            ):
                # Keep the unread position while this fixed archive is unavailable.
                documents[archive_key] = {**self._document(descriptor), '_hasItems': False}
                incomplete = True
                break
            rows = self._archive_items(extraction, view, document_id=document_id)
            unfiltered_count = len(rows)
            if owned_view:
                rows = await self._owned_rows(user_id, subject_id, ownership[0], descriptor, rows)
            if current['offset'] > len(rows):
                raise ValueError('cursor item offset is invalid')
            available = limit - len(items) - sum(not value['_hasItems'] for value in documents.values())
            selected_rows = rows[current['offset'] : current['offset'] + available]
            items.extend(selected_rows)
            if view == 'complete' and (rows or not owned_view):
                metadata = self._document(descriptor)
                if owned_view:
                    visible_documents = list(dict.fromkeys(doc for row in rows for doc in row['documentIds']))
                    metadata['documentIds'] = visible_documents
                    if descriptor.document_id not in visible_documents:
                        metadata['documentId'] = visible_documents[0] if len(visible_documents) == 1 else None
                    if custody_sources is not None or len(rows) < unfiltered_count:
                        metadata.update(title=None, reportTime=None)
                metadata['_hasItems'] = bool(rows)
                documents[archive_key] = metadata
                incomplete |= descriptor.archive_status in {'pending', 'unavailable'}
            current['offset'] += len(selected_rows)
            if current['offset'] < len(rows):
                break
            state = self._archive_position(state, current['next'], len(custody))
            if state is None:
                break
        if view == 'complete':
            try:
                await self._associate_facts(user_id, subject_id, items)
            except Exception:
                # Reading material does not depend on a fact lookup succeeding.
                # Unknown associations cannot inherit verification from a guess.
                for item in items:
                    item.pop('_factPointer', None)
                    item.pop('_factRevision', None)
                incomplete = True
        for source_id, before in sources.items():
            after = await self.archives.read_source_state(user_id=user_id, subject_id=subject_id, source_id=source_id)
            if before != after:
                raise ProfileDocumentArchiveChangedError('source changed during the page')
        for source_id, token in missing_tokens.items():
            current_source = await self.archives.read_source_state(
                user_id=user_id,
                subject_id=subject_id,
                source_id=source_id,
            )
            if current_source.token != token:
                raise ProfileDocumentArchiveChangedError('source changed while reading missing archive metadata')
        for (custody_subject, extraction_id), token in fixed.items():
            check = await self.archives.read_fixed(
                user_id=user_id,
                subject_id=custody_subject,
                extraction_id=extraction_id,
                expected_lifecycle_token=token,
                document_id=document_id,
            )
            self._check_descriptor({**scope, 'subjectId': custody_subject}, check.descriptor)
            if check.descriptor.extraction_id != extraction_id or check.descriptor.lifecycle_token != token:
                raise ProfileDocumentArchiveChangedError('fixed archive changed during the page')
        if ownership is not None:
            after_custody, after_ownership = await self._archive_access(user_id, subject_id)
            if (custody, ownership) != (after_custody, after_ownership):
                raise ProfileDocumentArchiveChangedError('Profile ownership changed during the page')
        output = {
            'items': items,
            'nextCursor': encode_cursor(_CURSOR_KIND, {'scope': scope, **state}) if state else None,
        }
        if view == 'complete':
            output['documents'] = [
                {key: value for key, value in document.items() if key != '_hasItems'} for document in documents.values()
            ]
            readable = any(document.get('archiveStatus') == 'ready' for document in documents.values()) or bool(items)
            output['status'] = 'partial' if incomplete and readable else 'unavailable' if incomplete else 'ready'
        return output

    @staticmethod
    def _archive_position(state, next_cursor, custody_count):
        index = state['custody']
        if next_cursor is None:
            index += 1
        if index >= custody_count:
            return None
        return {'phase': 'archives', 'after': next_cursor, 'current': None, 'custody': index}

    @staticmethod
    def _authorized_archive(descriptor, sources):
        if sources is None:
            return True
        revision = sources.get(descriptor.canonical_report_id)
        return revision is not None and descriptor.current_source and descriptor.publication_revision == revision

    @staticmethod
    def _decode(scope, cursor):
        if cursor is None:
            return {'phase': 'legacy', 'after': None, 'current': None}
        try:
            raw = decode_cursor(_CURSOR_KIND, cursor)
        except ValueError:
            # Published standard cursors still resume the legacy row scan.
            legacy = decode_cursor('document-observations', cursor)
            expected = {key: value for key, value in scope.items() if key != 'view'}
            if scope['view'] != 'standard' or legacy.get('scope') != expected:
                raise ValueError('cursor does not match this query') from None
            return {'phase': 'legacy', 'after': cursor, 'current': None}
        previous_scope = raw.get('scope')
        if (
            isinstance(previous_scope, dict)
            and 'profileOwnership' in scope
            and {key: value for key, value in previous_scope.items() if key != 'profileOwnership'}
            == {key: value for key, value in scope.items() if key != 'profileOwnership'}
            and previous_scope.get('profileOwnership') != scope['profileOwnership']
        ):
            raise ProfileDocumentArchiveChangedError('Profile ownership changed after the cursor was issued')
        if previous_scope != scope or raw.get('phase') not in {'legacy', 'archives'}:
            raise ValueError('cursor does not match this query')
        if raw.get('after') is not None and not isinstance(raw['after'], str):
            raise ValueError('cursor scan position is invalid')
        current = raw.get('current')
        if current is not None and (
            raw['phase'] != 'archives'
            or not isinstance(current, dict)
            or set(current) != {'extraction', 'token', 'next', 'offset'}
            or not all(isinstance(current[key], str) and current[key] for key in ('extraction', 'token'))
            or current['next'] is not None
            and not isinstance(current['next'], str)
            or type(current['offset']) is not int
            or current['offset'] < 0
        ):
            raise ValueError('cursor fixed archive position is invalid')
        custody = raw.get('custody', 0)
        if type(custody) is not int or custody < 0 or (raw['phase'] == 'legacy' and custody != 0):
            raise ValueError('cursor archive position is invalid')
        return {'phase': raw['phase'], 'after': raw.get('after'), 'current': current, 'custody': custody}

    async def _has_archive(self, scope, item):
        document_id = item.get('documentId')
        if not document_id:
            return False
        page = await self.archives.list_archived(
            user_id=scope['userId'],
            subject_id=scope['subjectId'],
            document_id=document_id,
            limit=1,
        )
        for descriptor in page.entries:
            self._check_descriptor({**scope, 'documentId': document_id}, descriptor)
            if descriptor.archive_status in {'ready', 'withdrawn'}:
                return True
        return False

    @staticmethod
    def _check_descriptor(scope, descriptor):
        if (descriptor.user_id, descriptor.subject_id) != (scope['userId'], scope['subjectId']):
            raise ProfileDocumentArchiveReadError('archive escaped the requested scope')
        if scope['documentId'] and scope['documentId'] not in (descriptor.document_id, *descriptor.document_ids):
            raise ProfileDocumentArchiveReadError('archive escaped the document filter')
        if not descriptor.lifecycle_token:
            raise ProfileDocumentArchiveReadError('archive identity is missing')
        if descriptor.extraction_id is None:
            if descriptor.archive_status != 'unavailable':
                raise ProfileDocumentArchiveReadError('unproved archive must be unavailable')
        elif not isinstance(descriptor.extraction_id, str) or not descriptor.extraction_id:
            raise ProfileDocumentArchiveReadError('archive identity is invalid')
        if descriptor.source_status not in _SOURCE_STATUSES or descriptor.archive_status not in _ARCHIVE_STATUSES:
            raise ProfileDocumentArchiveReadError('archive status is unsupported')

    @staticmethod
    def _document(descriptor):
        return {
            'documentId': descriptor.document_id,
            'documentIds': list(descriptor.document_ids or (descriptor.document_id,)),
            'canonicalReportId': descriptor.canonical_report_id,
            'extractionId': descriptor.extraction_id,
            'title': descriptor.title,
            'reportTime': descriptor.report_time,
            'processing': deepcopy(descriptor.processing),
            'sourceStatus': descriptor.source_status,
            'sourceRevision': descriptor.source_revision,
            'publicationRevision': descriptor.publication_revision,
            'archiveStatus': descriptor.archive_status,
            'reason': descriptor.reason,
        }

    def _archive_items(self, extraction, view, *, document_id=None):
        descriptor = extraction.descriptor
        if descriptor.archive_status == 'withdrawn' or descriptor.source_status == 'withdrawn':
            return []
        source_document = document_id
        if source_document is None and self.require_profile_ownership and descriptor.current_source:
            source_document = descriptor.document_id
        scoped_sources = [
            original
            for original in extraction.source_observations
            if source_document is None or 'documentIds' not in original or source_document in original['documentIds']
        ]
        if view == 'standard':
            rows = []
            for original in scoped_sources:
                if original.get('sourceStatus', descriptor.source_status) != 'active':
                    continue
                item = self._source_item(descriptor, original, source_status='active')
                if self.require_profile_ownership and descriptor.current_source:
                    item.update(
                        _factPointer=item['sourcePointer'],
                        _factRevision=original.get('materialSourceRevision', item['sourceRevision']),
                    )
                rows.append(item)
            return rows
        if descriptor.archive_status != 'ready':
            return []
        if extraction.result is None and not extraction.source_observations:
            raise ProfileDocumentArchiveReadError('ready archive has no result')
        selected_sources = [
            original
            for original in scoped_sources
            if original.get('extractionId', descriptor.extraction_id) == descriptor.extraction_id
        ]
        source_map = {}
        for original in selected_sources:
            analysis_base = original.get('contributionKind') == 'base' or (
                descriptor.current_source and not original.get('reportProjectionRef')
            )
            if analysis_base and isinstance(original.get('observationRef'), str):
                source_map.setdefault(original['observationRef'], []).append(original)
            for evidence in original.get('evidence', []):
                pointer = evidence.get('pointer')
                if isinstance(pointer, str) and pointer.startswith('/payload/interpretation/'):
                    rows = source_map.setdefault(pointer.removeprefix('/payload/interpretation'), [])
                    if original not in rows:
                        rows.append(original)
        items = []
        base_descriptor = replace(descriptor, document_ids=(descriptor.document_id,))
        for pointer, observation, unresolved in complete_observations(extraction.result or {}):
            associated = source_map.get(pointer, [])
            original = associated[0] if len(associated) == 1 else None
            item = self._item(base_descriptor, pointer, observation, unresolved=unresolved)
            if original is not None:
                self._set_source_documents(item, original)
                item['_factPointer'] = original.get('sourcePointer')
                item['_factRevision'] = original.get(
                    'materialSourceRevision',
                    original.get('sourceRevision', descriptor.source_revision),
                )
            items.append(item)
        for original in selected_sources:
            if (
                original.get('contributionKind') == 'legacy_report_source'
                or original.get('reportProjectionRef')
                or extraction.result is None
            ):
                item = self._source_item(descriptor, original)
                item.update(
                    extractionId=descriptor.extraction_id,
                    resolutionStatus='resolved',
                    verificationStatus='unconfirmed',
                    factStatus=None,
                    sourceStatus=descriptor.source_status,
                    publicationRevision=descriptor.publication_revision,
                    _factPointer=original['sourcePointer'],
                    _factRevision=original.get('materialSourceRevision', item['sourceRevision']),
                )
                items.append(item)
        return items

    def _source_item(self, descriptor, original, *, source_status=None):
        pointer = original.get('sourcePointer')
        revision = original.get('sourceRevision', descriptor.source_revision)
        if not isinstance(pointer, str) or not pointer or type(revision) is not int or revision < 1:
            raise ProfileDocumentArchiveReadError('source observation identity is missing')
        observation = {
            target: deepcopy(original[key])
            for key, target in (
                ('label', 'itemName'),
                ('rawValue', 'resultValue'),
                ('unit', 'unit'),
                ('referenceRange', 'referenceRange'),
                ('rawFlag', 'flag'),
                ('flag', 'flag'),
                ('observedAt', 'observedAt'),
                ('kind', 'kind'),
                ('method', 'method'),
                ('specimen', 'specimen'),
                ('narrativeType', 'narrativeType'),
                ('sourceCategory', 'sourceCategory'),
            )
            if original.get(key) is not None
        }
        effective = replace(descriptor, source_status=source_status) if source_status is not None else descriptor
        item = self._item(effective, pointer, observation)
        item['observationId'] = 'document_observation_' + _sha256_json([descriptor.canonical_report_id, pointer])
        self._set_source_documents(item, original)
        item['sourceRevision'] = revision
        for key in (
            'extractionId',
            'resolutionStatus',
            'verificationStatus',
            'factStatus',
            'sourceStatus',
            'publicationRevision',
        ):
            item.pop(key, None)
        return item

    @staticmethod
    def _set_source_documents(item, original):
        if 'documentIds' in original:
            item['documentIds'] = list(original['documentIds'])
            item['documentId'] = (
                original.get('documentId', item['documentIds'][0]) if len(item['documentIds']) == 1 else None
            )
        elif len(item['documentIds']) > 1:
            item['documentId'] = None

    @staticmethod
    def _item(descriptor, pointer, observation, *, unresolved=False):
        observed = _parse_report_time(observation.get('observedAt'))
        eligibility = (
            'unresolved'
            if unresolved
            else 'date_unknown'
            if observed is None
            else 'source_not_published'
            if descriptor.source_status != 'active'
            else 'subject_to_fact_policy'
        )
        return {
            'observationId': 'document_observation_' + _sha256_json([descriptor.extraction_id, pointer]),
            'documentId': descriptor.document_id,
            'documentIds': list(descriptor.document_ids or (descriptor.document_id,)),
            'canonicalReportId': descriptor.canonical_report_id,
            'sourceRevision': descriptor.source_revision,
            'sourcePointer': pointer,
            'observation': deepcopy(observation),
            'extractionId': descriptor.extraction_id,
            'resolutionStatus': 'unresolved' if unresolved else 'resolved',
            'observedAt': observed.isoformat() if observed else None,
            'dateStatus': 'known' if observed else 'unknown',
            'ingestionStatus': None,
            'factEligibility': eligibility,
            'sourceStatus': descriptor.source_status,
            'publicationRevision': descriptor.publication_revision,
            'verificationStatus': 'unconfirmed',
            'factStatus': None,
            **(
                {'missingFields': deepcopy(observation.get('missingFields', [])), 'reason': observation.get('reason')}
                if unresolved
                else {}
            ),
        }

    async def _archive_access(self, user_id, subject_id):
        custody = [(subject_id, None)]
        if not self.require_profile_ownership:
            return custody, None
        try:
            subject = await self.repository.get_subject(user_id, subject_id)
            view = await self.repository.get_view(user_id, subject_id)
        except (PyMongoError, TimeoutError):
            raise ProfileDocumentArchiveReadError('Profile ownership is unavailable') from None
        if subject is None or subject.status != 'active' or view is None:
            raise ProfileDocumentArchiveReadError('the selected Profile subject is unavailable')
        if self.publications is not None:
            scope = ProfileScope(user_id=user_id, subject_id=subject_id)
            try:
                watermarks = await self.publications.list_watermarks(scope)
            except (PyMongoError, TimeoutError):
                raise ProfileDocumentArchiveReadError('Profile source ownership is unavailable') from None
            by_subject = {}
            for row in watermarks:
                if (
                    row.source_type != 'health_report'
                    or row.scope.user_id != user_id
                    or row.scope.subject_id == subject_id
                    or subject_id not in row.affected_subject_ids
                ):
                    continue
                revision = row.applied_publication_revision
                if (
                    row.applied_retraction_revision < revision == row.accepted_publication_revision
                    and revision == row.settled_publication_revision
                    and row.applying_publication_revision is None
                ):
                    by_subject.setdefault(row.scope.subject_id, {})[row.source_id] = revision
            custody.extend(sorted(by_subject.items()))
        return custody, [subject.lifecycle_epoch, view.profile_version, profile_content_digest(custody)]

    async def _owned_rows(self, user_id, subject_id, lifecycle_epoch, descriptor, rows):
        # Upload custody may differ from the selected patient's final ownership.
        # Wait for settlement and require the exact authorized fact binding.
        if descriptor.source_status != 'active':
            return []
        try:
            await self._associate_facts(user_id, subject_id, rows, lifecycle_epoch=lifecycle_epoch)
        except (PyMongoError, TimeoutError):
            raise ProfileDocumentArchiveReadError('Profile source fact lookup is unavailable') from None
        return [item for item in rows if item.get('factStatus') in ('active', 'pending', 'disputed')]

    async def _associate_facts(self, user_id, subject_id, items, *, lifecycle_epoch=None):
        # Match exact source identity and pointer only, never names or values.
        requests = {
            (item['canonicalReportId'], item.get('_factRevision'), item.get('_factPointer'))
            for item in items
            if item.get('_factPointer') and item.get('_factRevision') is not None
        }
        matched = {}
        if requests:
            # A bounded indexed scan can establish uniqueness only when it ends.
            # Large or unreadable profiles stay unconfirmed instead of guessing.
            facts, after, remaining = [], None, self.SCAN_BUDGET
            while remaining:
                width = min(300, remaining)
                page = await self.repository.list_fact_page(
                    user_id,
                    subject_id,
                    categories=(),
                    statuses=(
                        (FactStatus.ACTIVE, FactStatus.PENDING, FactStatus.DISPUTED)
                        if lifecycle_epoch is not None
                        else tuple(status for status in FactStatus if status is not FactStatus.DELETED)
                    ),
                    fact_types=(),
                    effective_from=None,
                    effective_to=None,
                    after=after,
                    limit=width + 1,
                )
                facts.extend(page[:width])
                if len(page) <= width:
                    break
                remaining -= width
                if not remaining:
                    facts = []
                    break
                last = page[width - 1]
                after = (last.effective_period.start, last.fact_id)
            for fact in facts:
                if lifecycle_epoch is not None and (
                    fact.basic_info_lifecycle_epoch != lifecycle_epoch
                    or fact.status not in (FactStatus.ACTIVE, FactStatus.PENDING, FactStatus.DISPUTED)
                ):
                    continue
                for source in fact.sources:
                    key = (source.source_id, source.source_revision, source.pointer)
                    if source.source_type == 'health_report' and key in requests:
                        matched.setdefault(key, {})[fact.fact_id] = fact
        for item in items:
            key = (item['canonicalReportId'], item.pop('_factRevision', None), item.pop('_factPointer', None))
            candidates = matched.get(key, {})
            if len(candidates) == 1:
                fact = next(iter(candidates.values()))
                item.update(verificationStatus=fact.verification_status.value, factStatus=fact.status.value)


def complete_observations(result):
    """Preserve actual positions; remove only provable compatibility mirrors."""
    entries = []
    bindings = result.get('sourceBindings') if isinstance(result.get('sourceBindings'), dict) else {}
    seen_bindings = set()
    sections = result.get('sections')
    targets = [node for section in (sections or []) if isinstance(section, dict) for node in _projection_nodes(section)]
    if isinstance(sections, list):
        for index, section in enumerate(sections):
            if not isinstance(section, dict):
                continue
            for pointer, observation, unresolved in complete_observations(section):
                absolute = f'/sections/{index}' + pointer
                entries.append((absolute, observation, unresolved))
                if bindings.get(absolute):
                    seen_bindings.add(bindings[absolute])
    root = deepcopy(result)
    root.pop('sections', None)
    if isinstance(root.get('expansion'), dict):
        for field in ('findings', 'diagnosis'):
            if _joined_narrative(root, field, targets):
                root['expansion'][field] = None
    for pointer, original in _display_entries(root):
        if not isinstance(original, dict) or _section_projection(original, targets, 'observations'):
            continue
        binding = bindings.get(pointer)
        if binding and binding in seen_bindings:
            continue
        observation = deepcopy(original)
        observed = _observation_time(observation, result.get('reportTime'))
        observation['observedAt'] = observed.isoformat() if observed else None
        if pointer.endswith(('/findings', '/diagnosis')):
            observation.update(
                kind='narrative', narrativeType='diagnosis' if pointer.endswith('/diagnosis') else 'finding'
            )
        entries.append((pointer, observation, False))
        if binding:
            seen_bindings.add(binding)
    for index, original in enumerate(result.get('unresolvedItems') or []):
        if isinstance(original, dict) and not _section_projection(original, targets, 'unresolvedItems'):
            observation = deepcopy(original)
            observed = _observation_time(observation, result.get('reportTime'))
            observation['observedAt'] = observed.isoformat() if observed else None
            entries.append((f'/unresolvedItems/{index}', observation, True))
    return entries


def _display_entries(node):
    # This historical allowlist governs fact publication, not saved material.
    node = {key: value for key, value in node.items() if key != 'verifiedObservationPointers'}
    expansion = node.get('expansion')
    path = '/expansion'
    if isinstance(expansion, dict) and isinstance(expansion.get('observations'), list):
        expansion, path = expansion['observations'], '/expansion/observations'
    observations = node.get('observations')
    if not isinstance(expansion, list) or not isinstance(observations, list):
        return _report_observations(node, for_ingestion=False)
    # The canonical observation position is also used by neutral source facts.
    # Equality across the explicit compatibility arrays proves a mirror; equal
    # rows within one array keep their original occurrence positions.
    root = {**node, 'expansion': None}
    entries = _report_observations(root, for_ingestion=False)
    remaining = list(observations)
    for index, item in enumerate(expansion):
        if item in remaining:
            remaining.remove(item)
            continue
        pointer = f'{path}/{index}'
        entries.append((pointer, item))
    return entries


def _section_projection(item, sections, field):
    index = item.get('sourceSection')
    if type(index) is not int or not 0 <= index < len(sections):
        return False
    section = sections[index]
    candidate = {key: value for key, value in item.items() if key != 'sourceSection'}
    for original in section.get(field) or []:
        if not isinstance(original, dict):
            continue
        projected = {key: value for key, value in original.items() if key != 'sourceSection'}
        if candidate == projected:
            return True
        if field == 'observations':
            expected = _observation_time(original, section.get('reportTime'))
            actual = _parse_report_time(candidate.get('observedAt'))
            if expected is not None and actual == expected:
                projected.pop('observedAt', None)
                without_date = {key: value for key, value in candidate.items() if key != 'observedAt'}
                if without_date == projected:
                    return True
    return False


def _joined_narrative(node, field, sections):
    if not sections or node.get('expansionEvidence') or node.get('reportType') != 'imaging':
        return False
    if not all(type(section.get('sectionIndex')) is int for section in sections):
        return False
    values = [
        section['expansion'][field].strip()
        for section in sections
        if isinstance(section.get('expansion'), dict)
        and isinstance(section['expansion'].get(field), str)
        and section['expansion'][field].strip()
    ]
    return (node.get('expansion') or {}).get(field) == ('\n'.join(dict.fromkeys(values)) or None)


def _projection_nodes(node):
    children = node.get('sections') or []
    if not children:
        return [node]
    descendants = [target for child in children if isinstance(child, dict) for target in _projection_nodes(child)]
    own = any(
        not _section_projection(item, descendants, field)
        for field in ('observations', 'unresolvedItems')
        for item in node.get(field) or []
        if isinstance(item, dict)
    )
    expansion = node.get('expansion')
    if node.get('reportType') == 'imaging' and isinstance(expansion, dict):
        own |= any(
            expansion.get(field) and not _joined_narrative(node, field, descendants)
            for field in ('findings', 'diagnosis')
        )
    return ([node] if own else []) + descendants
