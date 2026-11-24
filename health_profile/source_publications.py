"""Durable Profile source inbox and monotonic publication receipts.

Only single-document compare-and-swap is assumed. Event aliases, publications and
source cursors have durable repair markers; accepting an event never applies facts.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from pydantic import TypeAdapter
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .contracts import ProfilePublicationWatermarkV1
from .contracts import ProfileScope
from .contracts import ProfileSourceCutoverAuthorizationV1
from .contracts import ProfileSourceCutoverStateV1
from .contracts import ProfileSourceEventV1
from .contracts import ProfileSourceReceiptV1
from .contracts import profile_content_digest

MAX_UNSETTLED_SOURCE_REVISIONS = 1000


class ProfileSourceConflictError(RuntimeError):
    pass


class ProfileSourceLeaseError(RuntimeError):
    pass


def source_key(scope, source_type, source_id):
    return profile_content_digest([scope.user_id, scope.subject_id, source_type, source_id])


def publication_id(event):
    return 'publication_' + profile_content_digest(
        [source_key(event.scope, event.source_type, event.source_id), event.publication_revision]
    )


def _fingerprint(event):
    return profile_content_digest(event.model_dump(mode='json', by_alias=True, exclude={'event_id'}))


class MongoProfilePublicationStore:
    """One dedicated collection, discriminated by recordKind; no legacy Job rows."""

    def __init__(self, collection):
        self.collection = collection

    async def ensure_indexes(self):
        await self.collection.create_index(
            [('recordKind', 1), ('status', 1), ('updatedAt', 1)], name='profile_publication_recovery'
        )
        await self.collection.create_index(
            [('recordKind', 1), ('scope.userId', 1), ('scope.subjectId', 1)], name='profile_publication_scope'
        )

    async def get(self, key):
        return _utc_datetimes(await self.collection.find_one({'_id': key}))

    async def insert(self, document):
        try:
            result = await self.collection.insert_one(deepcopy(document))
            if not result.acknowledged:
                raise RuntimeError('publication persistence was not acknowledged')
            return document, True
        except DuplicateKeyError:
            saved = await self.get(document['_id'])
            if saved is None:
                raise
            return saved, False

    async def replace(self, document, revision):
        updated = deepcopy(document)
        updated['revision'] = revision + 1
        saved = await self.collection.find_one_and_replace(
            {'_id': document['_id'], 'revision': revision},
            updated,
            return_document=ReturnDocument.AFTER,
        )
        return _utc_datetimes(saved)

    async def scan(self, query, limit=100):
        rows = await self.collection.find(query).sort([('updatedAt', 1), ('_id', 1)]).to_list(length=limit)
        return _utc_datetimes(rows)


class ProfileSourceInbox:
    def __init__(self, store, *, repository, clock=None, document_source=None, current_document_source=None):
        self.store = store
        self.repository = repository
        self.document_source = document_source
        self.current_document_source = current_document_source
        self.clock = clock or (lambda: datetime.now(UTC))

    def now(self):
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return value

    async def prepare_self_scope(self, user_id, *, display_name=None):
        """Explicit source-acceptance command; pure snapshot reads never call this."""
        from .service import HealthProfileService

        result = await HealthProfileService(self.repository, clock=self.now).ensure_self_profile(
            user_id, display_name=display_name
        )
        return ProfileScope(user_id=user_id, subject_id=result.subject.subject_id)

    @staticmethod
    def _require_cutover_capability(source_type, material_contract_version=None):
        if source_type == 'record' and material_contract_version != 'profile.record-source.v2':
            raise ProfileSourceConflictError(
                'source.record_cutover_not_ready: explicitly select profile.record-source.v2'
            )

    def _document_resolver(self, authorization):
        """Temporary default wiring; no business-version selection or fallback."""
        from .document_source import validate_default_document_declaration
        from .document_source_contracts import DOCUMENT_PROCESSING_REVISION
        from .source_material import ProfileSourceApplicationError

        try:
            validate_default_document_declaration(authorization)
        except ProfileSourceApplicationError as error:
            raise ProfileSourceConflictError('source.document_complete_source_required') from error
        resolver = self.document_source
        if resolver is None or resolver.VERSION != DOCUMENT_PROCESSING_REVISION:
            raise ProfileSourceConflictError('source.document_receiver_not_ready')
        return resolver

    async def _check_document_epoch(self, scope, epoch):
        from .models import SubjectStatus

        subject = await self.repository.get_subject(scope.user_id, scope.subject_id)
        if (
            type(epoch) is not int
            or epoch < 1
            or subject is None
            or subject.status is not SubjectStatus.ACTIVE
            or subject.lifecycle_epoch != epoch
        ):
            raise ProfileSourceConflictError('source.document_lifecycle_mismatch')
        return epoch

    async def authorize_cutover(self, authorization: ProfileSourceCutoverAuthorizationV1, *, scope: ProfileScope):
        """Persist a fence only after an explicit owner attests both legacy paths are stopped.

        The marker is retained if a pending-job check fails. It blocks later legacy
        admission; an operator must settle that old work before retrying authorization.
        """
        authorization = ProfileSourceCutoverAuthorizationV1.model_validate(authorization.model_dump())
        if authorization.scope != scope:
            raise PermissionError('cutover scope differs from server-resolved scope')
        self._require_cutover_capability(authorization.source_type, authorization.material_contract_version)
        if (
            (authorization.material_contract_version or '').startswith('document.')
            or authorization.material_snapshot_ref is not None
            and authorization.material_snapshot_ref.provider == 'document'
        ):
            resolver = self._document_resolver(authorization)
            await resolver.validate_cutover(authorization)
        subject = await self.repository.get_subject(scope.user_id, scope.subject_id)
        if subject is None or subject.user_id != scope.user_id:
            raise PermissionError('cutover subject is outside the requested scope')
        if authorization.source_type != 'health_report':
            if subject.kind.value != 'self':
                raise ProfileSourceConflictError('user evidence requires an existing self subject anchor')
            if authorization.legacy_source_intents_retired is not True:
                raise ProfileSourceConflictError('the source owner must retire its legacy delivery intents')
        key = source_key(scope, authorization.source_type, authorization.source_id)
        now = self.now()
        marker, _ = await self.store.insert(
            {
                '_id': 'cutover_' + key,
                'recordKind': 'cutover',
                'revision': 0,
                'status': 'blocked',
                'scope': scope.model_dump(by_alias=True),
                'authorization': authorization.model_dump(mode='json', by_alias=True),
                'updatedAt': now,
            }
        )
        stored_authorization = ProfileSourceCutoverAuthorizationV1.model_validate(marker['authorization'])
        if stored_authorization.model_dump(mode='json', by_alias=True) != authorization.model_dump(
            mode='json', by_alias=True
        ):
            raise ProfileSourceConflictError('source cutover already has a different authorization')
        if await self.repository.has_unsettled_source_jobs(scope, authorization.source_type, authorization.source_id):
            raise ProfileSourceConflictError('legacy source ingestion must quiesce before publication cutover')
        await self._update(marker['_id'], lambda row: row.update(status='ready', updatedAt=self.now()))
        return await self.get_cutover(scope, authorization.source_type, authorization.source_id)

    async def get_cutover(self, scope, source_type, source_id):
        row = await self.store.get('cutover_' + source_key(scope, source_type, source_id))
        if row is None:
            return None
        if row.get('admissionKind') == 'fresh':
            from .source_admission import ProfileFreshSourceAdmission

            return ProfileFreshSourceAdmission._state(row)
        return ProfileSourceCutoverStateV1(
            authorization=ProfileSourceCutoverAuthorizationV1.model_validate(row['authorization']),
            status=row['status'],
            updated_at=row['updatedAt'],
        )

    async def receive(self, event: ProfileSourceEventV1, *, scope: ProfileScope):
        event = ProfileSourceEventV1.model_validate(event.model_dump(mode='python'))
        if event.scope != scope:
            raise PermissionError('event scope differs from server-resolved scope')
        self._require_cutover_capability(
            event.source_type,
            'profile.record-source.v2' if event.event_kind == 'retract' else event.snapshot_ref.contract_version,
        )
        subject = await self.repository.get_subject(scope.user_id, scope.subject_id)
        if subject is None or subject.user_id != scope.user_id:
            raise PermissionError('event subject is outside the requested scope')
        key = source_key(scope, event.source_type, event.source_id)
        cutover = await self.store.get('cutover_' + key)
        binding = await self.store.get('document_binding_' + key)
        from .document_source_contracts_v2 import DOCUMENT_SOURCE_CONTRACT

        current_document = binding is not None or (
            event.snapshot_ref.provider == 'document'
            and (
                event.snapshot_ref.contract_version == DOCUMENT_SOURCE_CONTRACT
                or event.event_kind == 'retract'
                and cutover is None
            )
        )
        identity = publication_id(event)
        fingerprint = _fingerprint(event)
        alias_id = self.alias_id(scope, event.event_id)
        existing = await self.store.get(identity) or await self.store.get(alias_id)
        selected_processing, document_epoch = None, None
        if current_document:
            selected_processing, document_epoch = await self._bind_current_document(
                event,
                binding,
                existing,
                fingerprint,
            )
        else:
            if event.source_type == 'record' and cutover is not None:
                self._require_cutover_capability('record', cutover['authorization'].get('materialContractVersion'))
            if cutover is None or cutover['status'] != 'ready':
                if cutover is not None and cutover.get('admissionKind') == 'fresh':
                    raise ProfileSourceConflictError('explicit ready source authorization is required')
                raise ProfileSourceConflictError('explicit quiesced source cutover authorization is required')
            authorization = cutover['authorization']
            if cutover.get('admissionKind') == 'fresh':
                if (
                    event.event_kind != 'retract'
                    and event.snapshot_ref.contract_version != authorization['materialContractVersion']
                ):
                    raise ProfileSourceConflictError('source.fresh_material_contract_mismatch')
                if (
                    event.publication_revision == 1
                    and authorization.get('materialSnapshotRef') is not None
                    and (
                        event.snapshot_ref.model_dump(mode='json', by_alias=True)
                        != authorization['materialSnapshotRef']
                        or event.content_digest != authorization.get('materialContentDigest')
                    )
                ):
                    raise ProfileSourceConflictError('source.fresh_initial_material_mismatch')
            if event.snapshot_ref.provider == 'document' or (
                authorization.get('materialContractVersion') or ''
            ).startswith('document.'):
                if event.snapshot_ref.provider != 'document':
                    raise ProfileSourceConflictError('source.document_complete_source_required')
                if existing is None or existing.get('fingerprint') != fingerprint:
                    resolver = self._document_resolver(authorization)
                    if event.publication_revision == 1 and (
                        event.snapshot_ref.model_dump(mode='json', by_alias=True)
                        != authorization.get('materialSnapshotRef')
                        or event.content_digest != authorization.get('materialContentDigest')
                    ):
                        raise ProfileSourceConflictError('source.document_cutover_material_mismatch')
                    material = await resolver.resolve(event)
                    selected_processing = deepcopy(material.processing_selection)
                else:
                    selected_processing = deepcopy(existing.get('processingSelection'))
                if selected_processing is None:
                    raise ProfileSourceConflictError('source.document_processing_selection_missing')
        stored_cursor = await self.store.get('cursor_' + key)
        if stored_cursor is None and not current_document:
            if await self.repository.has_unsettled_source_jobs(scope, event.source_type, event.source_id):
                raise ProfileSourceConflictError('legacy source ingestion must quiesce before publication cutover')
        now = self.now()
        settled = settled_revision(stored_cursor) if stored_cursor else 0
        limit = MAX_UNSETTLED_SOURCE_REVISIONS + int(event.event_kind == 'retract')
        if event.publication_revision - settled > limit:
            # Accepted identities retain their retry semantics across a budget upgrade.
            existing = await self.store.get(identity) or await self.store.get(alias_id)
            if existing is None or existing.get('fingerprint') != fingerprint:
                raise ProfileSourceConflictError('source.unsettled_revision_limit_exceeded')
        await self.store.ensure_indexes()
        alias, _ = await self.store.insert(
            {
                '_id': alias_id,
                'recordKind': 'alias',
                'revision': 0,
                'status': 'staged',
                'scope': scope.model_dump(by_alias=True),
                'event': event.model_dump(mode='json', by_alias=True),
                'publicationId': identity,
                'processingSelection': selected_processing,
                **({'currentDocument': True} if current_document else {}),
                **({'lifecycleEpoch': document_epoch} if document_epoch is not None else {}),
                'fingerprint': fingerprint,
                'receivedAt': now,
                'updatedAt': now,
            }
        )
        if alias['publicationId'] != identity or alias['fingerprint'] != fingerprint:
            raise ProfileSourceConflictError('eventId is already bound to a different publication')
        publication, publication_created = await self.store.insert(
            {
                '_id': identity,
                'recordKind': 'publication',
                'revision': 0,
                'status': 'received',
                'scope': scope.model_dump(by_alias=True),
                'event': event.model_dump(mode='json', by_alias=True),
                'fingerprint': fingerprint,
                'receivedAt': alias['receivedAt'],
                'updatedAt': now,
                'attempts': 0,
                'errorCode': None,
                'nextRetryAt': None,
                'plan': None,
                'processingSelection': deepcopy(alias.get('processingSelection')),
                **({'currentDocument': True} if alias.get('currentDocument') else {}),
                **({'lifecycleEpoch': alias['lifecycleEpoch']} if 'lifecycleEpoch' in alias else {}),
                'extractionCalls': {},
                'extractedCandidates': None,
                'applicationResult': None,
            }
        )
        if publication['fingerprint'] != fingerprint:
            # The alias remains an observable conflict and must not enqueue a different payload.
            await self._update(alias_id, lambda row: row.update(status='conflict', updatedAt=now))
            raise ProfileSourceConflictError('publication revision already has different immutable content')
        key = source_key(scope, event.source_type, event.source_id)
        await self.store.insert(
            {
                '_id': 'cursor_' + key,
                'recordKind': 'cursor',
                'revision': 0,
                'scope': scope.model_dump(by_alias=True),
                'sourceType': event.source_type,
                'sourceId': event.source_id,
                'status': 'idle',
                'acceptedPublicationRevision': 0,
                'appliedPublicationRevision': 0,
                'settledPublicationRevision': 0,
                'acceptedRetractionRevision': 0,
                'appliedRetractionRevision': 0,
                'applyingPublicationRevision': None,
                'leaseToken': None,
                'leaseExpiresAt': None,
                'activeFactIds': [],
                'affectedSubjectIds': [scope.subject_id],
                'updatedAt': now,
            }
        )

        def accept(row):
            if publication_created:
                row.pop('settlementBlockedRevision', None)
            row['acceptedPublicationRevision'] = max(row['acceptedPublicationRevision'], event.publication_revision)
            if event.event_kind == 'retract':
                row['acceptedRetractionRevision'] = max(row['acceptedRetractionRevision'], event.publication_revision)
            row['status'] = 'pending' if row['acceptedPublicationRevision'] > settled_revision(row) else 'idle'
            row['updatedAt'] = now

        cursor = await self._update('cursor_' + key, accept)
        if event.prior_revision and event.prior_revision > settled_revision(cursor):
            await self._update(
                identity,
                lambda row: row.update(
                    status='waiting' if row['status'] == 'received' else row['status'], updatedAt=now
                ),
            )
        await self._update(alias_id, lambda row: row.update(status='linked', updatedAt=now))
        return await self.get_receipt(event.event_id, scope=scope)

    async def _bind_current_document(self, event, binding, existing, fingerprint):
        from .document_source_contracts_v2 import DOCUMENT_PROCESSING_REVISION
        from .document_source_contracts_v2 import DOCUMENT_SOURCE_CONTRACT

        if (
            event.source_type != 'health_report'
            or event.snapshot_ref.provider != 'document'
            or event.snapshot_ref.contract_version not in {DOCUMENT_SOURCE_CONTRACT, 'document.retraction.v1'}
        ):
            raise ProfileSourceConflictError('source.document_complete_source_required')
        if binding is not None:
            self._same_current_binding(binding, event)
            await self._check_document_epoch(event.scope, binding['lifecycleEpoch'])
        if existing is not None and existing.get('fingerprint') == fingerprint:
            if (
                binding is None
                or not existing.get('currentDocument')
                or existing.get('lifecycleEpoch') != binding['lifecycleEpoch']
            ):
                raise ProfileSourceConflictError('source.document_lifecycle_mismatch')
            selection = existing.get('processingSelection') or {}
            if (
                selection.get('implementationRevision') != DOCUMENT_PROCESSING_REVISION
                or selection.get('materialContract') != event.snapshot_ref.contract_version
            ):
                raise ProfileSourceConflictError('source.document_processing_selection_missing')
            return deepcopy(selection), binding['lifecycleEpoch']
        resolver = self.current_document_source
        if resolver is None:
            raise ProfileSourceConflictError('source.document_receiver_not_ready')
        material = await resolver.resolve(
            event, lifecycle_epoch=binding['lifecycleEpoch'] if binding is not None else None
        )
        epoch = await self._check_document_epoch(event.scope, material.lifecycle_epoch)
        if binding is None:
            binding, _ = await self.store.insert(
                {
                    '_id': 'document_binding_' + source_key(event.scope, event.source_type, event.source_id),
                    'recordKind': 'document_source_binding',
                    'revision': 0,
                    'scope': event.scope.model_dump(mode='json', by_alias=True),
                    'sourceType': event.source_type,
                    'sourceId': event.source_id,
                    'lifecycleEpoch': epoch,
                    'materialContract': DOCUMENT_SOURCE_CONTRACT,
                    'processingRevision': DOCUMENT_PROCESSING_REVISION,
                    'firstSnapshotRef': event.snapshot_ref.model_dump(mode='json', by_alias=True),
                    'firstContentDigest': event.content_digest,
                    'createdAt': self.now(),
                    'updatedAt': self.now(),
                }
            )
        self._same_current_binding(binding, event)
        if binding['lifecycleEpoch'] != epoch:
            raise ProfileSourceConflictError('source.document_lifecycle_mismatch')
        return deepcopy(material.processing_selection), epoch

    @staticmethod
    def _same_current_binding(binding, event):
        from .document_source_contracts_v2 import DOCUMENT_PROCESSING_REVISION
        from .document_source_contracts_v2 import DOCUMENT_SOURCE_CONTRACT

        if (
            binding.get('scope') != event.scope.model_dump(mode='json', by_alias=True)
            or binding.get('sourceType') != event.source_type
            or binding.get('sourceId') != event.source_id
            or binding.get('materialContract') != DOCUMENT_SOURCE_CONTRACT
            or binding.get('processingRevision') != DOCUMENT_PROCESSING_REVISION
        ):
            raise ProfileSourceConflictError('source.document_binding_conflict')

    @staticmethod
    def alias_id(scope, event_id):
        return 'alias_' + profile_content_digest([scope.user_id, scope.subject_id, event_id])

    async def _update(self, identity, mutate):
        for _ in range(32):
            row = await self.store.get(identity)
            if row is None:
                raise RuntimeError('publication record is missing')
            result = mutate(row)
            if result is False:
                return None
            saved = await self.store.replace(row, row['revision'])
            if saved is not None:
                return saved
        raise ProfileSourceConflictError('publication state is being updated concurrently')

    async def get_watermark(self, scope, source_type, source_id):
        row = await self.store.get('cursor_' + source_key(scope, source_type, source_id))
        return (
            _watermark(row)
            if row
            else ProfilePublicationWatermarkV1(
                scope=scope,
                source_type=source_type,
                source_id=source_id,
            )
        )

    async def list_watermarks(self, scope):
        rows = await self.store.scan(
            {
                'recordKind': 'cursor',
                'scope.userId': scope.user_id,
                '$or': [
                    {'scope.subjectId': scope.subject_id},
                    {'affectedSubjectIds': {'$in': [scope.subject_id]}},
                ],
            },
            limit=None,
        )
        return tuple(_watermark(row) for row in rows)

    async def get_receipt(self, event_id, *, scope):
        alias = await self.store.get(self.alias_id(scope, event_id))
        if alias is None:
            return None
        if alias['status'] == 'conflict':
            raise ProfileSourceConflictError('event alias conflicts with its publication')
        event = ProfileSourceEventV1.model_validate(alias['event'])
        row = await self.store.get(alias['publicationId'])
        row = row or alias
        watermark = await self.get_watermark(scope, event.source_type, event.source_id)
        result = row.get('applicationResult') or (row.get('partialApplication') or {}).get('result') or {}
        status = row.get('status', 'received')
        if status == 'staged':
            status = 'received'
        state = publication_state(row, watermark.settled_publication_revision)
        status = state['status']
        return ProfileSourceReceiptV1(
            event_id=event_id,
            publication_id=alias['publicationId'],
            scope=scope,
            source_type=event.source_type,
            source_id=event.source_id,
            publication_revision=event.publication_revision,
            status=status,
            received_at=row['receivedAt'],
            updated_at=row['updatedAt'],
            attempts=row.get('attempts', 0),
            error_code=None if status == 'applied' else row.get('errorCode'),
            profile_revision=result.get('profileRevision'),
            profile_revisions=result.get('profileRevisions', {}),
            processing_revision=(row.get('processingSelection') or {}).get('implementationRevision'),
            watermark=watermark,
            application_state=state['applicationState'],
            partial_application=deepcopy(row.get('partialApplication')),
            superseded_by_publication_revision=state.get('supersededByPublicationRevision'),
        )

    async def get_publication_state(self, scope, source_type, source_id, revision):
        key = source_key(scope, source_type, source_id)
        identity = 'publication_' + profile_content_digest([key, revision])
        row = await self.store.get(identity)
        if row is None:
            return {'status': 'missing', 'applicationState': 'not_applied'}
        cursor = await self.store.get('cursor_' + key)
        return publication_state(row, settled_revision(cursor) if cursor else 0)

    async def repair_staged(self, *, limit=100):
        rows = await self.store.scan({'recordKind': 'alias', 'status': 'staged'}, limit)
        for row in rows:
            event = ProfileSourceEventV1.model_validate(row['event'])
            try:
                await self.receive(event, scope=event.scope)
            except ProfileSourceConflictError as error:
                if str(error) == 'source.document_lifecycle_mismatch':
                    await self._fail_staged_document_lifecycle(row)
                continue
            except Exception:

                def failed_alias(current):
                    attempts = current.get('recoveryAttempts', 0) + 1
                    current.update(
                        recoveryAttempts=attempts,
                        status='failed' if attempts >= 5 else 'staged',
                        errorCode='source.intake_recovery_failed',
                        updatedAt=self.now(),
                    )

                await self._update(row['_id'], failed_alias)
        return len(rows)

    async def _fail_staged_document_lifecycle(self, original):
        selection = original.get('processingSelection') or {}
        epoch = original.get('lifecycleEpoch')
        if (
            selection.get('implementationRevision') != 'profile-document-source-v2'
            or type(epoch) is not int
            or epoch < 1
        ):
            return

        def same_binding(row):
            return all(
                row.get(key) == original.get(key) for key in ('fingerprint', 'lifecycleEpoch', 'processingSelection')
            )

        def fail_alias(row):
            if row.get('status') == 'staged' and same_binding(row):
                row.update(status='failed', errorCode='source.document_lifecycle_mismatch', updatedAt=self.now())

        await self._update(original['_id'], fail_alias)
        # Intake may have saved the publication but not its cursor. Keep that
        # durable receipt terminal too; completed/applied work retains its result.
        publication = await self.store.get(original['publicationId'])
        if publication is not None:

            def fail_unapplied(row):
                if (
                    same_binding(row)
                    and row.get('status') in {'received', 'waiting', 'retry_wait'}
                    and row.get('applicationResult') is None
                ):
                    row.update(
                        status='failed',
                        errorCode='source.document_lifecycle_mismatch',
                        nextRetryAt=None,
                        updatedAt=self.now(),
                    )

            await self._update(original['publicationId'], fail_unapplied)

    async def claim(self, cursor, *, lease_seconds=120):
        now = self.now()
        identity = cursor['_id']
        token = uuid4().hex

        def acquire(row):
            if row.get('leaseExpiresAt') and row['leaseExpiresAt'] > now:
                return False
            row.update(
                leaseToken=token,
                leaseExpiresAt=now + timedelta(seconds=lease_seconds),
                leaseDurationSeconds=lease_seconds,
                applyingPublicationRevision=settled_revision(row) + 1,
                updatedAt=now,
            )

        return await self._update(identity, acquire)

    async def check_lease(self, cursor):
        current = await self.store.get(cursor['_id'])
        if (
            current is None
            or current.get('leaseToken') != cursor['leaseToken']
            or current.get('leaseExpiresAt') is None
            or current['leaseExpiresAt'] <= self.now()
        ):
            raise ProfileSourceLeaseError('source application lease is no longer valid')
        duration = current.get('leaseDurationSeconds', 120)
        if current['leaseExpiresAt'] - self.now() <= timedelta(seconds=duration / 2):

            def renew(row):
                now = self.now()
                if (
                    row.get('leaseToken') != cursor['leaseToken']
                    or row.get('leaseExpiresAt') is None
                    or row['leaseExpiresAt'] <= now
                ):
                    raise ProfileSourceLeaseError('source application lease cannot be renewed by a stale worker')
                row.update(leaseExpiresAt=now + timedelta(seconds=duration), updatedAt=now)

            current = await self._update(cursor['_id'], renew)
        return current

    async def release(self, cursor):
        def unlock(row):
            if row.get('leaseToken') != cursor['leaseToken']:
                return False
            row.update(leaseToken=None, leaseExpiresAt=None, applyingPublicationRevision=None, updatedAt=self.now())

        await self._update(cursor['_id'], unlock)

    async def mark_processing(self, identity):
        return await self._update(
            identity,
            lambda row: row.update(
                status='processing',
                attempts=row['attempts'] + 1,
                updatedAt=self.now(),
                errorCode=None,
            ),
        )

    async def freeze_processing(self, cursor, identity, selection):
        await self.check_lease(cursor)

        def freeze(row):
            existing = row.get('processingSelection')
            if existing is not None and existing != selection:
                raise ProfileSourceConflictError('source processing selection is unavailable; never use latest')
            row['processingSelection'] = deepcopy(selection)

        return await self._update(identity, freeze)

    async def save_extraction_call(self, cursor, identity, key, output):
        await self.check_lease(cursor)

        def save(row):
            calls = row.setdefault('extractionCalls', {})
            calls.setdefault(key, deepcopy(output))
            if len(json.dumps(calls, ensure_ascii=False, allow_nan=False).encode()) > 4_000_000:
                raise ProfileSourceConflictError('source extraction output exceeds its durable budget')

        return await self._update(identity, save)

    async def save_extracted_candidates(self, cursor, identity, candidates):
        await self.check_lease(cursor)
        if len(json.dumps(candidates, ensure_ascii=False, allow_nan=False).encode()) > 4_000_000:
            raise ProfileSourceConflictError('source candidates exceed their durable budget')

        def save(row):
            if row.get('extractedCandidates') is None:
                row['extractedCandidates'] = deepcopy(candidates)

        return await self._update(identity, save)

    async def save_basic_candidates(self, cursor, identity, candidates):
        await self.check_lease(cursor)

        def save(row):
            if row.get('basicCandidatesCheckpoint') is None:
                row['basicCandidatesCheckpoint'] = deepcopy(candidates)

        return await self._update(identity, save)

    async def begin_partial(self, cursor, identity, error_code):
        await self.check_lease(cursor)

        def begin(row):
            if (row.get('processingSelection') or {}).get('implementationRevision') != 'profile-record-source-v2':
                raise ProfileSourceConflictError('partial application requires the exact Record v2 processor')
            if row.get('basicCandidatesCheckpoint') is None:
                raise ProfileSourceConflictError('partial application requires durable basic candidates')
            row['extractionFailure'] = error_code
            partial = row.get('partialApplication')
            if partial is None or partial['status'] != 'applied':
                row['partialApplication'] = {'status': 'pending', 'attempts': 0, 'reasonCode': error_code}
            row.update(status='retry_wait', nextRetryAt=None, updatedAt=self.now())

        return await self._update(identity, begin)

    async def claim_partial(self, cursor, identity):
        await self.check_lease(cursor)

        def claim(row):
            partial = row['partialApplication']
            partial['attempts'] += 1
            row.update(status='processing', updatedAt=self.now())

        return await self._update(identity, claim)

    async def complete_partial(self, cursor, publication, result):
        await self.check_lease(cursor)

        def finish(row):
            row['partialApplication'].update(status='applied', result=deepcopy(result))
            row.update(status='partial', errorCode=row['extractionFailure'], nextRetryAt=None, updatedAt=self.now())

        return await self._update(publication['_id'], finish)

    async def fail_partial(self, cursor, identity, error_code, *, max_attempts=5):
        await self.check_lease(cursor)

        def fail(row):
            partial = row['partialApplication']
            terminal = partial['attempts'] >= max_attempts
            partial.update(status='failed' if terminal else 'pending', errorCode=error_code)
            row.update(
                status='failed' if terminal else 'retry_wait',
                errorCode=error_code,
                nextRetryAt=None if terminal else self.now() + timedelta(seconds=2 ** partial['attempts']),
                updatedAt=self.now(),
            )

        return await self._update(identity, fail)

    async def track_subjects(self, cursor, subject_ids):
        await self.check_lease(cursor)

        def track(row):
            if row.get('leaseToken') != cursor['leaseToken'] or row['leaseExpiresAt'] <= self.now():
                raise ProfileSourceLeaseError('a stale owner cannot change affected subjects')
            row['affectedSubjectIds'] = sorted(set(row.get('affectedSubjectIds', [])) | set(subject_ids))

        return await self._update(cursor['_id'], track)

    async def save_plan(self, identity, plan, *, field='plan'):
        def save(row):
            if row.get(field) is None:
                row[field] = deepcopy(plan)
                row['updatedAt'] = self.now()

        return await self._update(identity, save)

    async def save_physical_reservation_failure(self, cursor, publication, code):
        await self.check_lease(cursor)

        def save(row):
            if row['status'] != 'processing' or row['attempts'] != publication['attempts']:
                raise ProfileSourceLeaseError('the physical reservation no longer belongs to this attempt')
            if row.get('physicalReservationFailure') is None:
                row.update(physicalReservationFailure=code, updatedAt=self.now())
            elif row['physicalReservationFailure'] != code:
                raise ProfileSourceConflictError('a physical reservation failure is already fixed')

        await self._update(publication['_id'], save)

    async def save_document_retirement_recovery(self, cursor, publication, plan, recovery):
        """Attach once to a fixed, unsettled action without rewriting the plan."""
        event = ProfileSourceEventV1.model_validate(publication['event'])
        revision = event.publication_revision
        identity = publication['_id']
        adapter = TypeAdapter(dict)
        plan_digest = profile_content_digest(adapter.dump_python(plan, mode='json'))
        for _ in range(32):
            current = await self.check_lease(cursor)
            if current['applyingPublicationRevision'] != revision or settled_revision(current) != event.prior_revision:
                raise ProfileSourceLeaseError('publication is no longer the source application in progress')
            if current['acceptedRetractionRevision'] >= revision:
                return
            await self._check_document_epoch(event.scope, publication['lifecycleEpoch'])
            row = await self.store.get(identity)
            if row is None or (
                row.get('basicInfoApplication') is not None
                or row.get('applicationResult') is not None
                or row.get('supersededByPublicationRevision') is not None
                or row.get('status') != 'processing'
            ):
                raise ProfileSourceLeaseError('the basic action is no longer awaiting recovery')
            for key in ('event', 'fingerprint', 'scope', 'lifecycleEpoch', 'processingSelection', 'currentDocument'):
                if row.get(key) != publication.get(key):
                    raise ProfileSourceConflictError('source.basic_info_plan_conflict')
            if profile_content_digest(adapter.dump_python(row.get('plan'), mode='json')) != plan_digest:
                raise ProfileSourceConflictError('source.basic_info_plan_conflict')
            existing = row.get('basicInfoRetirementRecovery')
            if existing is not None:
                if profile_content_digest(existing) != profile_content_digest(recovery):
                    raise ProfileSourceConflictError('source.basic_info_plan_conflict')
                return
            row['basicInfoRetirementRecovery'] = deepcopy(recovery)
            row['updatedAt'] = self.now()
            if await self.store.replace(row, row['revision']) is not None:
                return
        raise ProfileSourceConflictError('publication state is being updated concurrently')

    async def complete(self, cursor, publication, result):
        event = ProfileSourceEventV1.model_validate(publication['event'])
        await self.check_lease(cursor)
        supersedes = result.get('settlesPublicationIds', [])
        for identity in supersedes:
            await self.check_lease(cursor)
            await self._update(
                identity,
                lambda row: row.update(
                    supersededByPublicationRevision=event.publication_revision, updatedAt=self.now()
                ),
            )
        await self._update(
            publication['_id'],
            lambda row: row.update(
                applicationResult=deepcopy(result),
                updatedAt=self.now(),
            ),
        )

        def advance(row):
            if (
                row.get('leaseToken') != cursor['leaseToken']
                or row['leaseExpiresAt'] <= self.now()
                or settled_revision(row) != result.get('settlementFromRevision', event.prior_revision or 0)
            ):
                raise ProfileSourceLeaseError('source publication cannot advance with a stale lease or prior revision')
            row['appliedPublicationRevision'] = event.publication_revision
            row['settledPublicationRevision'] = event.publication_revision
            if event.event_kind == 'retract':
                row['appliedRetractionRevision'] = event.publication_revision
            row['activeFactIds'] = list(result['activeFactIds'])
            if 'clinicalInventoryDigest' in result:
                row['clinicalInventoryDigest'] = result['clinicalInventoryDigest']
            row['status'] = 'pending' if row['acceptedPublicationRevision'] > event.publication_revision else 'idle'
            row['updatedAt'] = self.now()

        await self._update(cursor['_id'], advance)
        await self._update(publication['_id'], lambda row: row.update(status='applied', updatedAt=self.now()))

    async def fail(self, identity, error_code, *, max_attempts=5):
        def fail_row(row):
            failed = row['attempts'] >= max_attempts
            row.update(
                status='failed' if failed else 'retry_wait',
                errorCode=error_code,
                updatedAt=self.now(),
                nextRetryAt=None if failed else self.now() + timedelta(seconds=min(300, 2 ** row['attempts'])),
            )

        return await self._update(identity, fail_row)

    async def redrive(self, event_id, *, scope):
        alias = await self.store.get(self.alias_id(scope, event_id))
        if alias is None:
            raise LookupError('source event is missing')

        event = ProfileSourceEventV1.model_validate(alias['event'])
        watermark = await self.get_watermark(scope, event.source_type, event.source_id)
        if watermark.accepted_retraction_revision > event.publication_revision or (
            watermark.accepted_retraction_revision == event.publication_revision and event.event_kind != 'retract'
        ):
            raise ProfileSourceConflictError('a retraction barrier forbids redriving its older publication')

        def resume(row):
            if row['status'] not in {'failed', 'partial'} or row.get('supersededByPublicationRevision'):
                raise ProfileSourceConflictError('only unsettled failed or partial publications can be redriven')
            row.pop('extractionFailure', None)
            row.pop('physicalReservationFailure', None)
            if row.get('currentDocument'):
                row['reportOwnershipRedrive'] = True
            row.update(status='received', attempts=0, errorCode=None, nextRetryAt=None, updatedAt=self.now())

        await self._update(alias['publicationId'], resume)
        if event.event_kind == 'retract':
            await self._update(
                'cursor_' + source_key(scope, event.source_type, event.source_id),
                lambda row: row.pop('settlementBlockedRevision', None),
            )
        return await self.get_receipt(event_id, scope=scope)


def settled_revision(row):
    return row.get('settledPublicationRevision', row['appliedPublicationRevision'])


def publication_state(row, settled):
    event = ProfileSourceEventV1.model_validate(row['event'])
    superseded = row.get('supersededByPublicationRevision')
    if superseded is not None and settled >= superseded:
        return {
            'status': 'superseded',
            'applicationState': 'superseded_by_retraction',
            'supersededByPublicationRevision': superseded,
        }
    if row.get('applicationResult') is not None and settled >= event.publication_revision:
        return {'status': 'applied', 'applicationState': 'applied'}
    partial = row.get('partialApplication') or {}
    state = (
        'partial'
        if partial.get('status') == 'applied'
        else 'incomplete'
        if row.get('plan') is not None or row.get('partialPlan') is not None
        else 'not_applied'
    )
    status = row.get('status', 'received')
    return {'status': 'received' if status == 'staged' else status, 'applicationState': state}


def _watermark(row):
    return ProfilePublicationWatermarkV1(
        scope=ProfileScope.model_validate(row['scope']),
        source_type=row['sourceType'],
        source_id=row['sourceId'],
        accepted_publication_revision=row['acceptedPublicationRevision'],
        applied_publication_revision=row['appliedPublicationRevision'],
        settled_publication_revision=settled_revision(row),
        accepted_retraction_revision=row['acceptedRetractionRevision'],
        applied_retraction_revision=row['appliedRetractionRevision'],
        applying_publication_revision=row.get('applyingPublicationRevision'),
        affected_subject_ids=tuple(row.get('affectedSubjectIds', [row['scope']['subjectId']])),
    )


def _utc_datetimes(value):
    """Default Mongo codecs return naive UTC; never infer the machine timezone.

    Public immutable events keep ISO strings to preserve sub-millisecond precision
    and their original offset/fingerprint. Mutable ledger timestamps remain BSON.
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value
    if isinstance(value, dict):
        return {key: _utc_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_utc_datetimes(item) for item in value]
    return value
