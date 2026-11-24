"""Bounded pure reads of existing self-profile report material.

This provider returns original saved material and independent source contributions;
it neither creates a subject nor publishes facts, downloads files or runs models.
Health Analysis uses its fixed Profile confirmation subject and lifecycle.
"""

from copy import deepcopy
from dataclasses import asdict

from pymongo.errors import PyMongoError

from .contracts import ProfileScope
from .contracts import profile_content_digest
from .document_archive_contracts import ProfileDocumentArchiveChangedError
from .document_archive_contracts import ProfileDocumentArchiveReadError
from .document_archive_contracts import ProfileReportLifecycleConflict
from .document_views import complete_observations


class ProfileReportMaterialsCapacityError(OverflowError):
    """The configured material read bound was exceeded; no silent truncation."""


class ProfileReportMaterialsReader:
    def __init__(self, *, profiles, archives, max_documents=100, page_size=20, max_restarts=1):
        for name, value in (('max_documents', max_documents), ('page_size', page_size)):
            if type(value) is not int or value < 1:
                raise ValueError(name + ' must be a positive integer')
        if page_size > 100 or type(max_restarts) is not int or not 0 <= max_restarts <= 2:
            raise ValueError('invalid material paging or restart bound')
        self.profiles, self.archives = profiles, archives
        self.max_documents, self.page_size, self.max_restarts = max_documents, page_size, max_restarts

    async def read(self, *, user_id, subject_id=None, expected_lifecycle_epoch=None):
        if expected_lifecycle_epoch is not None and (
            type(expected_lifecycle_epoch) is not int or expected_lifecycle_epoch < 1 or subject_id is None
        ):
            raise ValueError('the fixed confirmation requires a subject and positive lifecycle')
        for attempt in range(self.max_restarts + 1):
            try:
                return await self._read(user_id, subject_id, expected_lifecycle_epoch)
            except ProfileReportLifecycleConflict:
                raise
            except ProfileDocumentArchiveChangedError:
                if attempt == self.max_restarts:
                    raise
            except (PyMongoError, TimeoutError):
                raise ProfileDocumentArchiveReadError('Profile report dependency unavailable') from None

    @staticmethod
    def _empty(reason):
        return {
            'contractVersion': 'profile-report-materials.v1',
            'scope': None,
            'materials': [],
            'gender': None,
            'status': 'empty',
            'coverage': {'reason': reason, 'complete': True},
            'sourceSnapshot': {'archives': []},
        }

    async def _read(self, user_id, subject_id, epoch):
        if epoch is None:
            scope = await self.profiles.resolve_scope(user_id)
        else:
            scope = ProfileScope(user_id=user_id, subject_id=subject_id)
            current = await self.profiles.resolve_existing_self_lifecycle(scope)
            if current is None:
                return self._empty('confirmation_subject_unavailable')
            if current != (subject_id, epoch, 'active'):
                raise ProfileReportLifecycleConflict('Profile subject lifecycle changed after confirmation')
        if scope is None:
            return self._empty('existing_self_missing')
        if scope.user_id != user_id or subject_id is not None and scope.subject_id != subject_id:
            raise PermissionError('Report read is outside the authorized existing self scope')
        owner = {'user_id': scope.user_id, 'subject_id': scope.subject_id}
        gender, gender_status = (None, 'from_confirmation') if epoch is not None else await self._gender(scope)
        snapshots, selections, cursor, seen_cursors = [], [], None, set()
        partial, documents_read = False, 0
        while True:
            remaining = self.max_documents - documents_read
            if remaining <= 0:
                raise ProfileReportMaterialsCapacityError('Report document capacity exceeded')
            page = await self.archives.list_archived(**owner, after_id=cursor, limit=min(self.page_size, remaining))
            if len(page.entries) > min(self.page_size, remaining):
                raise TypeError('Report archive provider exceeded its requested page bound')
            documents_read += len(page.entries)
            for descriptor in page.entries:
                self._check_scope(scope, descriptor)
                if epoch is not None and descriptor.lifecycle_epoch != epoch:
                    partial = True
                    continue
                selections.append(descriptor)
                snapshot = {'descriptor': asdict(descriptor), 'result': None, 'sourceObservations': []}
                snapshots.append(snapshot)
                withdrawn = (
                    descriptor.source_status in {'withdrawn', 'retired'} or descriptor.archive_status == 'withdrawn'
                )
                if descriptor.extraction_id is None:
                    partial |= not withdrawn
                    continue
                if withdrawn and not descriptor.archive_managed_bindings:
                    continue
                try:
                    value = await self.archives.read_fixed(
                        **owner,
                        extraction_id=descriptor.extraction_id,
                        expected_lifecycle_token=descriptor.lifecycle_token,
                    )
                except ProfileDocumentArchiveReadError:
                    partial = True
                    continue
                self._check_scope(scope, value.descriptor)
                if value.descriptor != descriptor:
                    raise ProfileDocumentArchiveChangedError('Report material changed between selection and read')
                snapshot['archiveBindingsVerified'] = True
                if not withdrawn:
                    snapshot.update(
                        result=deepcopy(value.result), sourceObservations=deepcopy(value.source_observations)
                    )
                    partial |= descriptor.archive_status != 'ready' or descriptor.processing.get('status') == 'partial'
            next_cursor = page.next_after_id
            if next_cursor is None:
                break
            if not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                raise TypeError('Report archive cursor did not advance')
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        # A second fixed read closes lifecycle changes occurring between pages.
        for descriptor, snapshot in zip(selections, snapshots):
            if descriptor.extraction_id is not None:
                try:
                    check = await self.archives.read_fixed(
                        **owner,
                        extraction_id=descriptor.extraction_id,
                        expected_lifecycle_token=descriptor.lifecycle_token,
                    )
                except ProfileDocumentArchiveReadError:
                    # Failed revalidation cannot publish this document's old
                    # content, while independent documents remain usable.
                    snapshot.update(
                        result=None, sourceObservations=[], archiveBindingsVerified=False, readStatus='unavailable'
                    )
                    partial = True
                    state = await self.archives.read_source_state(**owner, source_id=descriptor.canonical_report_id)
                    if state.token != descriptor.lifecycle_token:
                        raise ProfileDocumentArchiveChangedError('Unreadable report lifecycle changed')
                    continue
                self._check_scope(scope, check.descriptor)
                if check.descriptor != descriptor:
                    raise ProfileDocumentArchiveChangedError('Report material changed during reading')
            else:
                state = await self.archives.read_source_state(**owner, source_id=descriptor.canonical_report_id)
                if state.token != descriptor.lifecycle_token:
                    raise ProfileDocumentArchiveChangedError('Unavailable report lifecycle changed')
                if descriptor.archive_managed_bindings:
                    snapshot['archiveBindingsVerified'] = True
        if epoch is not None:
            if await self.profiles.resolve_existing_self_lifecycle(scope) != (scope.subject_id, epoch, 'active'):
                raise ProfileReportLifecycleConflict('Profile subject lifecycle changed during the read')
        elif await self.profiles.resolve_scope(user_id) != scope:
            raise ProfileDocumentArchiveChangedError('Existing self scope changed during reading')
        materials, mirrors, empty_observations = self._materials(scope, snapshots)
        return {
            'contractVersion': 'profile-report-materials.v1',
            'scope': {**scope.model_dump(by_alias=True), **({'lifecycleEpoch': epoch} if epoch is not None else {})},
            'materials': materials,
            'gender': gender,
            'status': 'partial' if partial else 'available' if materials else 'empty',
            'coverage': {
                'complete': not partial,
                'documentsRead': documents_read,
                'profileGenderStatus': gender_status,
                'mirrorsRemoved': mirrors,
                'emptyObservationsExcluded': empty_observations,
                'factVerification': 'saved_material_is_not_verified_patient_fact',
            },
            'sourceSnapshot': {'archives': snapshots},
        }

    async def _gender(self, scope):
        try:
            page = await self.profiles.read_page(
                scope.user_id,
                subject_id=scope.subject_id,
                statuses=('active', 'pending', 'disputed'),
                fact_types=('demographic.sex', 'demographic.gender', 'gender'),
                page_size=1000,
            )
        except (PyMongoError, TimeoutError):
            return None, 'unavailable'
        if page.scope is not None and page.scope != scope:
            raise PermissionError('Gender material escaped the existing self scope')
        if page.status != 'ready' or page.truncated or page.stale:
            return None, 'unavailable'
        values = set()
        for fact in page.facts:
            value = str(fact.value).strip().lower()
            normalized = {
                'male': 'male',
                'm': 'male',
                '男': 'male',
                '男性': 'male',
                'female': 'female',
                'f': 'female',
                '女': 'female',
                '女性': 'female',
            }.get(value)
            if normalized is None:
                continue
            if fact.status == 'disputed':
                return None, 'conflicting'
            if fact.status == 'active':
                values.add(normalized)
        return (next(iter(values)), 'explicit') if len(values) == 1 else (None, 'conflicting' if values else 'unknown')

    @staticmethod
    def _check_scope(scope, descriptor):
        if (descriptor.user_id, descriptor.subject_id) != (scope.user_id, scope.subject_id):
            raise PermissionError('Report material escaped the existing self scope')
        if not descriptor.lifecycle_token:
            raise TypeError('Report material is missing a lifecycle identity')

    @staticmethod
    def _has_content(observation):
        def actual(value):
            if value is None:
                return False
            if isinstance(value, str):
                return bool(value.strip())
            if isinstance(value, (list, tuple)):
                return any(actual(item) for item in value)
            if isinstance(value, dict):
                return any(actual(item) for item in value.values())
            return True  # Numeric zero and explicit false are actual results.

        return any(
            actual(observation.get(key))
            for key in (
                'resultValue',
                'rawValue',
                'value',
                'text',
                'rawText',
                'description',
                'findings',
                'diagnosis',
                'flag',
                'rawFlag',
            )
        )

    @staticmethod
    def _materials(scope, snapshots):
        materials, originals, seen_sources, mirrors, empty_observations = [], {}, set(), 0, 0
        physical = {}
        managed = {
            (descriptor['lifecycle_epoch'], *binding)
            for snapshot in snapshots
            if (descriptor := snapshot['descriptor']).get('current_source') and snapshot.get('archiveBindingsVerified')
            for binding in descriptor.get('archive_managed_bindings', ())
        }
        for snapshot in snapshots:
            descriptor, result = snapshot['descriptor'], snapshot['result']
            if descriptor['source_status'] in {'withdrawn', 'retired'} or descriptor['archive_status'] == 'withdrawn':
                continue
            if result is None or descriptor['archive_status'] != 'ready':
                continue
            current = descriptor.get('current_source')
            quality = descriptor['processing'].get('status')
            if current and (
                quality not in {'partial', 'complete'}
                or descriptor.get('archive_lifecycle_epoch') is None
                or descriptor['archive_lifecycle_epoch'] != descriptor['lifecycle_epoch']
                or not descriptor.get('result_digest')
            ):
                continue
            identity_base = (
                descriptor['lifecycle_epoch'],
                descriptor['document_id'],
                descriptor['interpretation_round'],
            )
            source_metadata = {key: val for key, val in descriptor.items() if key != 'archive_managed_bindings'}
            for pointer, observation, unresolved in complete_observations(result):
                if (*identity_base, pointer) in managed:
                    continue
                if not ProfileReportMaterialsReader._has_content(observation):
                    empty_observations += 1
                    continue
                identity = (descriptor['extraction_id'], pointer)
                material = {
                    'id': 'report:' + profile_content_digest([scope.model_dump(), identity]),
                    'kind': 'report',
                    'content': deepcopy(observation),
                    'metadata': {
                        **deepcopy(source_metadata),
                        'origin': 'original_result',
                        'publicationRevision': None,
                        'sourcePointer': pointer,
                        'unresolved': unresolved,
                        'materialQuality': 'partial' if quality == 'partial' else 'complete',
                        'verificationStatus': 'unconfirmed',
                        'mirrorSources': [],
                    },
                }
                if unresolved:
                    # The saved text can inform follow-up, but cannot support a
                    # personal condition in Health Analysis.
                    material['status'] = 'unknown'
                originals[identity] = material
                materials.append(material)
        for snapshot in snapshots:
            descriptor = snapshot['descriptor']
            if descriptor['source_status'] in {'withdrawn', 'retired'} or descriptor['archive_status'] == 'withdrawn':
                continue
            for observation in snapshot['sourceObservations']:
                state = observation.get('sourceStatus', descriptor['source_status'])
                if state not in {'active', 'pending'}:
                    continue
                if not ProfileReportMaterialsReader._has_content(observation):
                    empty_observations += 1
                    continue
                extraction = observation.get('extractionId')
                is_original = observation.get('contributionKind') == 'base' or (
                    descriptor.get('current_source') and not observation.get('reportProjectionRef')
                )
                pointers = [observation.get('observationRef')] if is_original else []
                pointers += [
                    evidence['pointer'].removeprefix('/payload/interpretation')
                    for evidence in observation.get('evidence', [])
                    if isinstance(evidence, dict)
                    and isinstance(evidence.get('pointer'), str)
                    and evidence['pointer'].startswith('/payload/interpretation/')
                ]
                mirror = next(
                    (originals[(extraction, pointer)] for pointer in pointers if (extraction, pointer) in originals),
                    None,
                )
                if mirror is not None:
                    mirror['metadata']['mirrorSources'].append(deepcopy(observation))
                    mirrors += 1
                    continue
                occurrence = observation.get('physicalOccurrence')
                physical_key = (
                    tuple(occurrence[field] for field in ('documentId', 'sourceChecksum', 'locatorRevision', 'locator'))
                    if occurrence is not None
                    else None
                )
                if physical_key is not None and physical_key in physical:
                    physical[physical_key]['metadata']['mirrorSources'].append(deepcopy(observation))
                    mirrors += 1
                    continue
                binding = (
                    observation.get('contributionSnapshotRef'),
                    observation.get('sourcePointer'),
                    observation.get('sourceRevision'),
                    observation.get('publicationRevision'),
                    descriptor['canonical_report_id'],
                )
                if binding in seen_sources:
                    mirrors += 1
                    continue
                seen_sources.add(binding)
                metadata = {
                    'origin': 'source_contribution',
                    'canonicalReportId': descriptor['canonical_report_id'],
                    'lifecycleToken': descriptor['lifecycle_token'],
                    'sourceStatus': state,
                    'verificationStatus': 'unconfirmed',
                    'extractionId': descriptor['extraction_id'],
                    'archiveDocumentId': descriptor['document_id'],
                    'mirrorSources': [],
                    **deepcopy(observation),
                }
                pointer = observation.get('observationRef', '')
                if descriptor.get('current_source') and not pointer.startswith('/reportSource/'):
                    metadata['interpretationPointer'] = pointer
                    metadata['unresolved'] = '/unresolvedItems/' in pointer
                    quality = descriptor['processing'].get('status')
                    metadata['materialQuality'] = 'partial' if quality == 'partial' else 'complete'
                material = {
                    'id': 'report:' + profile_content_digest([scope.model_dump(), physical_key or binding]),
                    'kind': 'report',
                    'content': deepcopy(observation),
                    'metadata': metadata,
                }
                if metadata.get('unresolved') or observation.get('verificationStatus') in {'unconfirmed', 'conflicted'}:
                    material['status'] = 'unknown'
                if physical_key is not None:
                    physical[physical_key] = material
                materials.append(material)
        return materials, mirrors, empty_observations
