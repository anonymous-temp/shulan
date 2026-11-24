"""Profile-owned source-wide governance and recoverable publication application."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from pydantic import TypeAdapter
from pymongo.errors import DuplicateKeyError

from utils.log import getLogger

from .basic_info import field_for_fact
from .basic_info_store import BasicInfoConflict
from .basic_info_store import BasicInfoUnavailable
from .contracts import ProfileSourceEventV1
from .contracts import profile_content_digest
from .document_source_contracts_v2 import DOCUMENT_SOURCE_CONTRACT
from .fact_writer import HealthProfileFactWriter
from .ingestion import HealthFactPolicy
from .ingestion import PolicyDisposition
from .models import FactSource
from .models import FactStatus
from .models import HealthFact
from .models import MemoryCandidate
from .models import ProfileActor
from .models import ProfileEvent
from .models import SubjectKind
from .models import SubjectStatus
from .models import fact_user_confirmed
from .physical_occurrences import apply_physical_fact
from .physical_occurrences import group_physical_reading
from .physical_occurrences import human_owned as _human_owned
from .physical_occurrences import physical_keys
from .physical_occurrences import release_physical_fact
from .physical_occurrences import reserve_physical_fact
from .record_ingestion import RecordSubjectResolver
from .report_ownership import ReportOwnership
from .report_ownership import report_target
from .service import HealthProfileService
from .source_material import ProfileSourceApplicationError
from .source_material import ProfileSourceMaterialMapper
from .source_publications import MAX_UNSETTLED_SOURCE_REVISIONS
from .source_publications import ProfileSourceConflictError
from .source_publications import ProfileSourceLeaseError
from .source_publications import _fingerprint
from .source_publications import publication_id
from .source_publications import settled_revision

logger = getLogger(__name__)

_READ_STATUSES = tuple(status for status in FactStatus if status is not FactStatus.DELETED)


class ProfileSourceApplier:
    """Persist a bounded plan before facts; replay creates no duplicate contribution.

    Only verified original positions assert a shared cross-revision identity.
    Human changes with unresolved correspondence require explicit review.
    """

    def __init__(
        self, inbox, *, resolvers, mapper=None, policy=None, extractor=None, subject_resolver=None, basic_info=None
    ):
        self.inbox = inbox
        self.repository = inbox.repository
        self.resolvers = dict(resolvers)
        self.mapper = mapper or ProfileSourceMaterialMapper()
        self.policy = policy or HealthFactPolicy()
        self.extractor = extractor
        self.subject_resolver = subject_resolver or RecordSubjectResolver(
            self.repository,
            HealthProfileService(self.repository, clock=inbox.now),
        )
        self.writer = HealthProfileFactWriter(self.repository, clock=inbox.now, basic_info=basic_info)
        self.basic_info_bridge = self.writer.basic_info_bridge
        self.report_ownership = ReportOwnership(self.repository, basic_info)

    async def apply(self, cursor, publication, *, partial=False):
        try:
            return await self._apply(cursor, publication, partial=partial)
        except BasicInfoConflict as error:
            raise ProfileSourceApplicationError(str(error), retryable=False) from error
        except BasicInfoUnavailable as error:
            raise ProfileSourceApplicationError(str(error), retryable=True) from error

    async def _apply(self, cursor, publication, *, partial=False):
        event = ProfileSourceEventV1.model_validate(publication['event'])
        subject = await self.repository.get_subject(event.scope.user_id, event.scope.subject_id)
        if subject is None or subject.status is not SubjectStatus.ACTIVE:
            raise ProfileSourceApplicationError('subject.inactive_or_missing')
        if event.source_type != 'health_report' and subject.kind is not SubjectKind.SELF:
            raise ProfileSourceApplicationError('source.self_anchor_required')
        await self.inbox.check_lease(cursor)
        await self._check_material_revision(event, publication)
        if publication.get('reportOwnershipRedrive'):
            epoch = publication['lifecycleEpoch']
            self._document_epoch(subject, publication, epoch)
            await self.report_ownership.finish(event.scope, epoch, publication['_id'], failed=True)
        field = 'partialPlan' if partial else 'plan'
        plan = publication.get(field)
        if plan is None:
            plan = await self._plan(event, subject, cursor, publication, partial=partial)
            publication = (
                await self.inbox.save_plan(publication['_id'], plan, field=field)
                if partial
                else await self.inbox.save_plan(publication['_id'], plan)
            )
            plan = publication[field]
        await self.inbox.track_subjects(cursor, plan.get('subjectIds', [subject.subject_id]))
        if 'lifecycleEpoch' in plan:
            self._document_epoch(subject, publication, plan['lifecycleEpoch'])
        if fixed := plan.get('documentOwnership'):
            current = await self.inbox.check_lease(cursor)
            if current['acceptedRetractionRevision'] < event.publication_revision:
                await self.report_ownership.validate_plan(
                    event.scope, plan['lifecycleEpoch'], fixed, publication['_id']
                )
        await self._reserve_physical_facts(cursor, publication, event, plan)
        if 'documentBasicActions' in plan:
            basic_receipt = await self._apply_routed_basic_info(cursor, publication, plan, event)
        else:
            basic_receipt = await self._apply_document_basic_info(cursor, publication, plan)
        if 'documentSubjectEpochs' in plan:
            await self.inbox.check_lease(cursor)
            await self.report_ownership.finish(event.scope, plan['lifecycleEpoch'], publication['_id'])
        if 'lifecycleEpoch' in plan:
            fresh_subject = await self.repository.get_subject(event.scope.user_id, event.scope.subject_id)
            self._document_epoch(fresh_subject, publication, plan['lifecycleEpoch'])
        try:
            await self._check_plan_human_decisions(event, plan)
            for document in plan['withdraws']:
                await self.inbox.check_lease(cursor)
                fact = self._planned_fact(document, plan)
                if 'documentSubjectEpochs' in plan and fact.subject_id != event.scope.subject_id:
                    previous_target = await self.repository.get_subject(event.scope.user_id, fact.subject_id)
                    if previous_target is None or previous_target.lifecycle_epoch != fact.basic_info_lifecycle_epoch:
                        continue  # The former target lifecycle is already invisible.
                if fact.physical_occurrence_key:
                    await apply_physical_fact(self, cursor, event, fact)
                else:
                    target = await self._target_subject(event, fact, lifecycle_epoch=fact.basic_info_lifecycle_epoch)
                    await self._withdraw(event, target, fact, admitted_at=publication.get('receivedAt'))
            await self._check_plan_human_decisions(event, plan)
            for document in plan['creates']:
                fact = self._planned_fact(document, plan)
                if fact.physical_occurrence_key:
                    await apply_physical_fact(self, cursor, event, fact, upsert=True)
                else:
                    current = await self.inbox.check_lease(cursor)
                    target = await self._target_subject(
                        event, fact, require_active=True, lifecycle_epoch=fact.basic_info_lifecycle_epoch
                    )
                    await self._create(
                        event,
                        target,
                        fact,
                        suppressed=current['acceptedRetractionRevision'] >= event.publication_revision,
                        admitted_at=publication.get('receivedAt'),
                    )
            await self._check_plan_human_decisions(event, plan)
        except ProfileSourceApplicationError as error:
            if error.code == 'source.manual_governance_requires_review':
                for document in plan['creates']:
                    await self.inbox.check_lease(cursor)
                    fact = self._planned_fact(document, plan)
                    if fact.physical_occurrence_key:
                        await apply_physical_fact(self, cursor, event, fact, compensate=True)
                    else:
                        target = await self._target_subject(
                            event, fact, lifecycle_epoch=fact.basic_info_lifecycle_epoch
                        )
                        await self._withdraw(
                            event,
                            target,
                            fact,
                            request_suffix=':compensate',
                            admitted_at=publication.get('receivedAt'),
                        )
            raise
        await self.inbox.check_lease(cursor)
        if 'lifecycleEpoch' in plan:
            fresh_subject = await self.repository.get_subject(event.scope.user_id, event.scope.subject_id)
            self._document_epoch(fresh_subject, publication, plan['lifecycleEpoch'])
        view = await self.repository.get_view(event.scope.user_id, event.scope.subject_id)
        revisions = {}
        for subject_id in plan.get('subjectIds', [subject.subject_id]):
            target_view = await self.repository.get_view(event.scope.user_id, subject_id)
            revisions[subject_id] = target_view.profile_version if target_view else None
        return {
            'profileRevision': view.profile_version if view else None,
            'profileRevisions': revisions,
            'activeFactIds': plan['activeFactIds'],
            'reasonCodes': plan['reasonCodes'],
            **(
                {'clinicalInventoryDigest': plan['clinicalInventoryDigest']}
                if 'clinicalInventoryDigest' in plan
                else {}
            ),
            **({'basicInfoApplication': basic_receipt} if basic_receipt is not None else {}),
            **(
                {key: plan[key] for key in ('settlesPublicationIds', 'settlementFromRevision')}
                if 'settlesPublicationIds' in plan
                else {}
            ),
        }

    async def _reserve_physical_facts(self, cursor, publication, event, plan):
        facts = [self._planned_fact(row, plan) for row in plan['creates'] if row.get('physical_occurrence_key')]
        if not facts:
            return
        failure = publication.get('physicalReservationFailure')
        if failure is None:
            try:
                for fact in sorted(facts, key=lambda item: item.fact_id):
                    await reserve_physical_fact(self, cursor, event, fact)
            except ProfileSourceApplicationError as error:
                if error.retryable:
                    raise
                failure = error.code
                await self.inbox.save_physical_reservation_failure(cursor, publication, failure)
        if failure is not None:
            for fact in facts:
                await release_physical_fact(self, cursor, event, fact)
            raise ProfileSourceApplicationError(failure)

    async def _check_material_revision(self, event, publication):
        """Validate every predecessor before planning or replaying any application effect."""
        predecessors = set(publication.get('settlesPublicationIds', ()))
        required = {
            publication_id(event.model_copy(update={'publication_revision': revision}))
            for revision in (event.prior_revision, publication.get('settlementFromRevision'))
            if revision
        }
        for identity in predecessors | required:
            previous = await self.inbox.store.get(identity)
            if previous is None:
                if identity in required:
                    raise ProfileSourceApplicationError('source.snapshot_missing', retryable=True)
                continue
            prior = ProfileSourceEventV1.model_validate(previous['event'])
            if (prior.scope, prior.source_type, prior.source_id) != (event.scope, event.source_type, event.source_id):
                raise ProfileSourceApplicationError('source.plan_scope_mismatch')
            if prior.source_revision > event.source_revision:
                raise ProfileSourceApplicationError('source.material_revision_regressed')

    @staticmethod
    def _planned_fact(raw, plan):
        fact = HealthFact.model_validate(raw)
        epoch = plan.get('documentSubjectEpochs', {}).get(fact.subject_id, plan.get('lifecycleEpoch'))
        if epoch is None:
            return fact
        if fact.basic_info_lifecycle_epoch not in (None, epoch):
            raise ProfileSourceApplicationError('source.document_fact_lifecycle_mismatch')
        # Only the original durable plan may repair an older untagged plan row.
        # Never obtain a new epoch from the then-current subject on replay.
        return fact.model_copy(update={'basic_info_lifecycle_epoch': epoch})

    async def _target_subject(self, event, fact, *, require_active=False, lifecycle_epoch=None):
        if fact.user_id != event.scope.user_id:
            raise ProfileSourceApplicationError('source.plan_scope_mismatch')
        subject = await self.repository.get_subject(event.scope.user_id, fact.subject_id)
        if subject is None or subject.user_id != event.scope.user_id:
            raise ProfileSourceApplicationError('source.plan_subject_missing')
        if require_active and subject.status is not SubjectStatus.ACTIVE:
            raise ProfileSourceApplicationError('source.plan_subject_inactive')
        if lifecycle_epoch is not None and subject.lifecycle_epoch != lifecycle_epoch:
            raise ProfileSourceApplicationError('source.document_lifecycle_mismatch')
        return subject

    async def _plan(self, event, subject, cursor, publication, *, partial=False):
        resolver = (
            self.inbox.current_document_source
            if publication.get('currentDocument')
            else self.resolvers.get((event.snapshot_ref.provider, event.snapshot_ref.contract_version))
        )
        if resolver is None:
            raise ProfileSourceApplicationError('source.resolver_unsupported')
        if hasattr(resolver, 'resolve_fixed'):
            material = deepcopy(
                await resolver.resolve_fixed(
                    event, publication.get('processingSelection'), lifecycle_epoch=publication.get('lifecycleEpoch')
                )
            )
        else:
            material = deepcopy(await resolver.resolve(event))
        self._check_material_identity(event, material)
        if material.processing_selection is not None:
            try:
                await self.inbox.freeze_processing(cursor, publication['_id'], material.processing_selection)
            except ProfileSourceConflictError as error:
                raise ProfileSourceApplicationError('source.processing_version_unavailable') from error
        if material.lifecycle_epoch is not None:
            self._document_epoch(subject, publication, material.lifecycle_epoch)
        document = event.source_type == 'health_report' and event.snapshot_ref.provider == 'document'
        report_targets, prior_document_plan = await self._report_targets(event, subject, cursor, publication, material)
        routed = report_targets is not None
        prior_epochs = prior_document_plan.get('documentSubjectEpochs', {})
        target_epochs = {**prior_epochs, subject.subject_id: material.lifecycle_epoch}
        if routed:
            target_epochs.update({row['subjectId']: row['lifecycleEpoch'] for row in report_targets.values()})
        clinical_digest = material.clinical_inventory_digest
        preserve_clinical = (
            document
            and event.event_kind != 'retract'
            and (
                not material.clinical_available
                or clinical_digest is not None
                and cursor.get('clinicalInventoryDigest') == clinical_digest
            )
        )
        if preserve_clinical:
            plan = {
                'creates': [],
                'withdraws': [],
                'activeFactIds': list(cursor.get('activeFactIds', [])),
                'reasonCodes': [],
                'subjectIds': sorted(target_epochs) if routed else [subject.subject_id],
            }
        else:
            plan = await self._plan_material(
                event,
                subject,
                cursor,
                publication,
                material,
                partial=partial,
                report_targets=report_targets,
                document_epochs=target_epochs if routed else None,
            )
        if material.lifecycle_epoch is not None:
            plan['lifecycleEpoch'] = material.lifecycle_epoch
        if document and (clinical_digest is not None or 'clinicalInventoryDigest' in cursor):
            plan['clinicalInventoryDigest'] = (
                cursor.get('clinicalInventoryDigest', clinical_digest) if preserve_clinical else clinical_digest
            )
        if routed:
            plan['documentSubjectEpochs'] = target_epochs
            if report_targets:
                plan['documentOwnership'] = [
                    {'documentId': document, 'patientSlot': slot} for document, slot in sorted(report_targets)
                ]
            plan['subjectIds'] = sorted(set(plan['subjectIds']) | set(target_epochs))
        if routed and set(target_epochs) != {subject.subject_id}:
            plan['documentBasicActions'] = self._routed_basic_actions(
                event, material, report_targets, prior_document_plan
            )
        elif material.basic_info_available or (event.event_kind == 'retract' and material.lifecycle_epoch is not None):
            plan['documentBasicInfo'] = self._document_basic_action(event, material)
            if publication.get('currentDocument') and event.event_kind == 'correct':
                plan['documentBasicInfo']['retiredBasicContributions'] = self._retirement_rows(material)
        if len(TypeAdapter(dict).dump_json(plan)) > 4_000_000:
            raise ProfileSourceApplicationError('source.application_plan_too_large')
        return plan

    async def _report_targets(self, event, subject, cursor, publication, material):
        if not publication.get('currentDocument'):
            return None, {}
        previous = await self._prior_plans(event, publication)
        prior = next((plan for plan in previous if 'documentSubjectEpochs' in plan), {})
        if event.event_kind == 'retract':
            return ({} if prior else None), prior
        if not material.routing_documents:
            return None, prior
        current = await self.inbox.check_lease(cursor)
        if current['acceptedRetractionRevision'] >= event.publication_revision:
            return {}, prior
        targets = await self.report_ownership.reserve(subject, material, publication['_id'])
        await self.inbox.check_lease(cursor)
        fresh = await self.repository.get_subject(subject.user_id, subject.subject_id)
        self._document_epoch(fresh, publication, material.lifecycle_epoch)
        await self.report_ownership.materialize(subject, targets, self.inbox.now)
        return targets, prior

    async def _prior_plans(self, event, publication):
        if not event.prior_revision:
            return []
        previous = event.model_copy(update={'publication_revision': event.prior_revision})
        identities = [publication_id(previous), *publication.get('settlesPublicationIds', ())]
        settled = publication.get('settlementFromRevision', 0)
        if settled and settled != event.prior_revision:
            identities.append(publication_id(event.model_copy(update={'publication_revision': settled})))
        plans = []
        for identity in dict.fromkeys(identities):
            row = await self.inbox.store.get(identity)
            if row:
                plans.extend(row[field] for field in ('plan', 'partialPlan') if row.get(field) is not None)
        return plans

    def _routed_basic_actions(self, event, material, targets, previous):
        previous_actions = {row['subjectId']: row for row in previous.get('documentBasicActions', ())}
        if legacy_action := previous.get('documentBasicInfo'):
            previous_actions[event.scope.subject_id] = legacy_action
        if not previous_actions and not material.basic_info_available:
            return []
        by_target = {}
        for row in material.basic_info:
            target = report_target(row, material, targets)
            if target is not None:
                by_target.setdefault(target['subjectId'], (target['lifecycleEpoch'], []))[1].append(row)
        for subject_id, prior in previous_actions.items():
            by_target.setdefault(subject_id, (prior['lifecycleEpoch'], []))
        retirements = self._retirement_rows(material)
        actions = []
        for subject_id, (epoch, fields) in sorted(by_target.items()):
            prior_ids = {row.get('contributionId') for row in previous_actions.get(subject_id, {}).get('fields', ())}
            actions.append(
                {
                    **self._document_basic_action(event, material),
                    'subjectId': subject_id,
                    'lifecycleEpoch': epoch,
                    'fields': fields,
                    'retiredBasicContributions': [row for row in retirements if row['contributionId'] in prior_ids],
                }
            )
        return actions

    async def _apply_routed_basic_info(self, cursor, publication, plan, event):
        actions = plan['documentBasicActions']
        existing = publication.get('basicInfoApplication')
        digest = profile_content_digest(actions)
        if existing is not None:
            if existing['actionDigest'] != digest:
                raise ProfileSourceApplicationError('source.basic_info_plan_conflict')
            return existing
        basic = self.report_ownership.basic
        if basic is None:
            raise ProfileSourceApplicationError('source.basic_info_provider_unavailable', retryable=True)
        revision = event.publication_revision
        receipts = {}
        for action in actions:
            current = await self.inbox.check_lease(cursor)
            barrier = current['acceptedRetractionRevision']
            withdrawing = action['retract'] or barrier >= revision
            target = await self.repository.get_subject(event.scope.user_id, action['subjectId'])
            epoch = action['lifecycleEpoch']
            if target is None or target.status is not SubjectStatus.ACTIVE or target.lifecycle_epoch != epoch:
                if withdrawing:
                    continue  # The fixed former lifecycle is already invisible.
                raise ProfileSourceApplicationError('source.document_lifecycle_mismatch')
            kwargs = {
                'source_id': action['sourceId'],
                'publication_revision': max(revision, barrier) if withdrawing else revision,
                'lifecycle_epoch': epoch,
            }
            if withdrawing:
                await basic.withdraw_report(event.scope.user_id, target.subject_id, **kwargs)
            else:
                await basic.ingest_report(
                    event.scope.user_id,
                    target.subject_id,
                    source_revision=action['sourceRevision'],
                    fields=action['fields'],
                    explicit_retirements=action['retiredBasicContributions'],
                    **kwargs,
                )
            receipts[target.subject_id] = 'withdraw' if withdrawing else 'upsert'
        await self.inbox.check_lease(cursor)
        receipt = {
            'actionDigest': digest,
            'lifecycleEpoch': plan['lifecycleEpoch'],
            'publicationRevision': revision,
            'subjectEffects': receipts,
        }

        def record(row):
            previous = row.get('basicInfoApplication')
            if previous is not None and previous != receipt:
                raise ProfileSourceConflictError('source.basic_info_receipt_conflict')
            row['basicInfoApplication'] = receipt

        await self.inbox._update(publication['_id'], record)
        return receipt

    @staticmethod
    def _document_basic_action(event, material):
        return {
            'sourceId': event.source_id,
            'sourceRevision': event.source_revision,
            'publicationRevision': event.publication_revision,
            'lifecycleEpoch': material.lifecycle_epoch,
            'fields': list(material.basic_info),
            'retract': event.event_kind == 'retract',
        }

    @staticmethod
    def _retirement_rows(material):
        return [row.model_dump(mode='json', by_alias=True) for row in material.basic_info_retirements]

    @staticmethod
    def _check_material_identity(event, material):
        if (
            material.scope != event.scope
            or material.source_type != event.source_type
            or material.source_id != event.source_id
            or material.source_revision != event.source_revision
        ):
            raise ProfileSourceApplicationError('source.resolved_scope_mismatch')
        if profile_content_digest(material.snapshot) != event.content_digest:
            raise ProfileSourceApplicationError('source.content_digest_mismatch')

    @staticmethod
    def _document_epoch(subject, publication, epoch):
        if (
            type(epoch) is not int
            or publication.get('lifecycleEpoch') != epoch
            or subject is None
            or subject.status is not SubjectStatus.ACTIVE
            or subject.lifecycle_epoch != epoch
        ):
            raise ProfileSourceApplicationError('source.document_lifecycle_mismatch')

    async def _recover_document_retirements(self, cursor, publication, plan, event, digest, basic):
        action = plan['documentBasicInfo']
        epoch = publication.get('lifecycleEpoch')
        self._check_legacy_basic_action(publication, plan, event)
        scope = event.scope
        if await basic._fixed_report_action_is_committed(
            scope.user_id,
            scope.subject_id,
            source_id=event.source_id,
            source_revision=event.source_revision,
            fields=action['fields'],
            publication_revision=event.publication_revision,
            lifecycle_epoch=epoch,
        ):
            return ()

        binding = {
            'eventFingerprint': publication['fingerprint'],
            'processingSelection': publication.get('processingSelection'),
            'lifecycleEpoch': epoch,
            'actionDigest': digest,
        }
        saved = publication.get('basicInfoRetirementRecovery')
        if saved is not None:
            if (
                not isinstance(saved, dict)
                or set(saved) != {'binding', 'retirements', 'proofDigest'}
                or saved['binding'] != binding
                or saved['proofDigest'] != profile_content_digest([binding, saved['retirements']])
            ):
                raise ProfileSourceApplicationError('source.basic_info_plan_conflict')
            return saved['retirements']

        resolver = self.inbox.current_document_source
        if resolver is None:
            raise ProfileSourceApplicationError('source.resolver_unsupported')
        material = await resolver.resolve_fixed(event, binding['processingSelection'], lifecycle_epoch=epoch)
        self._check_material_identity(event, material)
        if (
            not material.basic_info_available
            or profile_content_digest(self._document_basic_action(event, material)) != digest
        ):
            raise ProfileSourceApplicationError('source.basic_info_plan_conflict')
        subject = await self.repository.get_subject(scope.user_id, scope.subject_id)
        self._document_epoch(subject, publication, material.lifecycle_epoch)
        retirements = self._retirement_rows(material)
        recovery = {
            'binding': binding,
            'retirements': retirements,
            'proofDigest': profile_content_digest([binding, retirements]),
        }
        if len(TypeAdapter(dict).dump_json({'plan': plan, 'basicInfoRetirementRecovery': recovery})) > 4_000_000:
            raise ProfileSourceApplicationError('source.application_plan_too_large')
        try:
            await self.inbox.save_document_retirement_recovery(cursor, publication, plan, recovery)
        except ProfileSourceConflictError as error:
            if str(error) in {'source.basic_info_plan_conflict', 'source.document_lifecycle_mismatch'}:
                raise ProfileSourceApplicationError(str(error)) from error
            raise
        return retirements

    @staticmethod
    def _check_legacy_basic_action(publication, plan, event):
        action = plan['documentBasicInfo']
        epoch = publication.get('lifecycleEpoch')
        if (
            publication.get('fingerprint') != _fingerprint(event)
            or publication.get('scope') != event.scope.model_dump(by_alias=True)
            or type(epoch) is not int
            or epoch < 1
            or plan.get('lifecycleEpoch') != epoch
            or set(action)
            != {'sourceId', 'sourceRevision', 'publicationRevision', 'lifecycleEpoch', 'fields', 'retract'}
            or action['sourceId'] != event.source_id
            or type(action['sourceRevision']) is not int
            or action['sourceRevision'] != event.source_revision
            or type(action['publicationRevision']) is not int
            or action['publicationRevision'] != event.publication_revision
            or type(action['lifecycleEpoch']) is not int
            or action['lifecycleEpoch'] != epoch
            or action['retract'] is not False
            or not isinstance(action['fields'], list)
        ):
            raise ProfileSourceApplicationError('source.basic_info_plan_conflict')

    async def _apply_document_basic_info(self, cursor, publication, plan):
        action = plan.get('documentBasicInfo')
        if action is None:
            return None
        digest = profile_content_digest(action)
        existing = publication.get('basicInfoApplication')
        if existing is not None:
            if existing['actionDigest'] != digest:
                raise ProfileSourceApplicationError('source.basic_info_plan_conflict')
            return existing
        basic = getattr(self.repository, 'basic_info', None)
        if basic is None:
            raise ProfileSourceApplicationError('source.basic_info_provider_unavailable', retryable=True)
        event = ProfileSourceEventV1.model_validate(publication['event'])
        current = await self.inbox.check_lease(cursor)
        suppressed = current['acceptedRetractionRevision'] >= event.publication_revision and not action['retract']
        retirements = action.get('retiredBasicContributions', ())
        recovered = (
            not suppressed
            and publication.get('currentDocument') is True
            and event.source_type == 'health_report'
            and event.snapshot_ref.provider == 'document'
            and event.snapshot_ref.contract_version == DOCUMENT_SOURCE_CONTRACT
            and event.event_kind == 'correct'
            and 'retiredBasicContributions' not in action
        )
        if recovered:
            retirements = await self._recover_document_retirements(cursor, publication, plan, event, digest, basic)
            current = await self.inbox.check_lease(cursor)
            suppressed = current['acceptedRetractionRevision'] >= event.publication_revision
        kwargs = {
            'source_id': action['sourceId'],
            'publication_revision': current['acceptedRetractionRevision']
            if suppressed
            else action['publicationRevision'],
            'lifecycle_epoch': action['lifecycleEpoch'],
        }
        if action['retract'] or suppressed:
            await basic.withdraw_report(event.scope.user_id, event.scope.subject_id, **kwargs)
        else:
            apply_report = basic._resume_fixed_report_action if recovered and retirements else basic.ingest_report
            await apply_report(
                event.scope.user_id,
                event.scope.subject_id,
                source_revision=action['sourceRevision'],
                fields=action['fields'],
                explicit_retirements=retirements,
                **kwargs,
            )
        receipt = {
            'actionDigest': digest,
            'lifecycleEpoch': action['lifecycleEpoch'],
            'publicationRevision': kwargs['publication_revision'],
            'effect': 'withdraw' if action['retract'] or suppressed else 'upsert',
        }
        await self.inbox.check_lease(cursor)

        def record(row):
            previous = row.get('basicInfoApplication')
            if previous is not None and previous != receipt:
                raise ProfileSourceConflictError('source.basic_info_receipt_conflict')
            row['basicInfoApplication'] = receipt

        await self.inbox._update(publication['_id'], record)
        return receipt

    async def _plan_material(
        self, event, subject, cursor, publication, material, *, partial=False, report_targets=None, document_epochs=None
    ):
        if len(material.observations) > 1000:
            raise ProfileSourceApplicationError('source.observation_limit_exceeded')
        if document_epochs is not None:
            subjects = [
                current
                for subject_id, epoch in document_epochs.items()
                if (current := await self.repository.get_subject(event.scope.user_id, subject_id)) is not None
                and current.lifecycle_epoch == epoch
            ]
        elif event.source_type == 'health_report':
            subjects = (subject,)
        else:
            subjects = await self.repository.list_subjects(event.scope.user_id, include_archived=True)
        if len(subjects) > 1000:
            raise ProfileSourceApplicationError('source.subject_limit_exceeded')
        owned = {}
        for target in subjects:
            for fact in await self.repository.list_facts(
                event.scope.user_id, target.subject_id, statuses=_READ_STATUSES
            ):
                if _has_source(fact, event) and not (material.lifecycle_epoch is not None and field_for_fact(fact)):
                    owned[fact.fact_id] = fact
        for previous_plan in await self._prior_plans(event, publication):
            for raw in previous_plan['creates']:
                seed = HealthFact.model_validate(raw)
                if material.lifecycle_epoch is None or field_for_fact(seed) is None:
                    owned.setdefault(seed.fact_id, seed)
        # A full redrive reuses its own partial creates; never withdraw them and
        # then rely on an idempotent create to revive the withdrawn value.
        if not partial and event.event_kind != 'retract':
            for raw in (publication.get('partialPlan') or {}).get('creates', []):
                seed = HealthFact.model_validate(raw)
                existing = owned.get(seed.fact_id)
                if existing is not None:
                    if existing.version != seed.version or existing.value != seed.value:
                        raise ProfileSourceApplicationError('source.manual_governance_requires_review')
                    owned.pop(seed.fact_id)
        if len(owned) > 1000:
            raise ProfileSourceApplicationError('source.contribution_limit_exceeded')
        if event.event_kind != 'retract':
            if any(_human_owned(fact) for fact in owned.values()):
                raise ProfileSourceApplicationError('source.manual_governance_requires_review')
            for target in subjects:
                await self._check_deleted_source_facts(event, subject_id=target.subject_id)
        creates, reasons = [], []
        shared = {}
        shared_aliases = {}
        previous_facts = owned.copy()
        destinations = (
            {item['observationRef']: report_target(item, material, report_targets) for item in material.observations}
            if report_targets
            else {}
        )
        occurrences = {}
        if publication.get('currentDocument'):
            occurrences = await physical_keys(self.repository, material, destinations=destinations)
        subjects_by_id = {target.subject_id: target for target in subjects}
        subject_ids = {event.scope.subject_id, *(fact.subject_id for fact in owned.values())}
        if event.event_kind != 'retract' and (report_targets is None or report_targets):
            if event.source_type == 'health_report':
                recorded_at = None
                if publication.get('currentDocument'):
                    now = self.inbox.now()
                    recorded_at = now.replace(microsecond=now.microsecond // 1000 * 1000)
                candidates, mapping = self.mapper.map(event, material, analysis_recorded_at=recorded_at)
                reasons.extend(mapping.reason_codes)
            else:
                if self.extractor is None:
                    raise ProfileSourceApplicationError('source.extractor_unavailable', retryable=True)
                if partial:
                    from .source_contracts import parse_profile_source_snapshot

                    selected = self.extractor.processing_manifest(parse_profile_source_snapshot(material.snapshot))
                    if selected != publication.get('processingSelection'):
                        raise ProfileSourceApplicationError('source.processing_version_unavailable')
                    if publication.get('basicCandidatesCheckpoint') is None:
                        raise ProfileSourceApplicationError('source.partial_checkpoint_missing')
                    candidates = tuple(
                        MemoryCandidate.model_validate(row) for row in publication['basicCandidatesCheckpoint']
                    )
                    reasons.append(publication['extractionFailure'])
                else:
                    candidates = await self.extractor.extract(self.inbox, cursor, publication, event, material)
            for candidate in candidates:
                if material.lifecycle_epoch is not None and field_for_fact(candidate) is not None:
                    continue
                target = subject
                if event.source_type != 'health_report':
                    resolution = await self.subject_resolver.resolve(
                        candidate,
                        user_id=event.scope.user_id,
                        self_subject=subject,
                    )
                    candidate, target = resolution.candidate, resolution.subject
                    subject_kind = resolution.subject_kind
                else:
                    if report_targets is not None:
                        destination = destinations.get(candidate.source.pointer)
                        if destination is None:
                            raise ProfileSourceApplicationError('source.document_patient_observation_mismatch')
                        target = subjects_by_id.get(destination['subjectId'])
                        if target is None or target.status is not SubjectStatus.ACTIVE:
                            raise ProfileSourceApplicationError('source.document_lifecycle_mismatch')
                    subject_kind = target.kind
                decision = self.policy.evaluate(candidate, subject_kind=subject_kind)
                if decision.disposition in {PolicyDisposition.REJECTED, PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION}:
                    if (
                        event.source_type == 'health_report'
                        and decision.disposition is PolicyDisposition.NEEDS_SUBJECT_CONFIRMATION
                    ):
                        raise ProfileSourceApplicationError('subject.unresolved')
                    reasons.append(decision.reason_code)
                    continue
                if target is None or target.user_id != event.scope.user_id:
                    raise ProfileSourceApplicationError('subject.unresolved')
                if event.source_type != 'health_report' and (
                    candidate.source.source_type != event.source_type
                    or candidate.source.source_id != event.source_id
                    or candidate.source.source_revision != event.source_revision
                ):
                    raise ProfileSourceApplicationError('source.candidate_source_mismatch')
                identity = [publication_id(event), candidate.candidate_id]
                if event.source_type != 'health_report' or report_targets is not None:
                    identity.append(target.subject_id)
                key, aliases = occurrences.get(candidate.source.pointer, (None, ()))
                fact_id = 'fact_physical_' + key if key else 'fact_publication_' + profile_content_digest(identity)
                source = candidate.source
                if source.recorded_at is not None and (earlier := previous_facts.get(fact_id)) is not None:
                    recording = next(
                        (
                            previous.recorded_at
                            for previous in earlier.sources
                            if _same_source(previous, event)
                            and previous.pointer == source.pointer
                            and previous.excerpt_hash == source.excerpt_hash
                            and previous.recorded_at is not None
                        ),
                        None,
                    )
                    if recording is not None:
                        source = source.model_copy(update={'recorded_at': recording})
                        candidate = candidate.model_copy(update={'source': source})
                now = self.inbox.now()
                fact = HealthFact(
                    fact_id=fact_id,
                    user_id=event.scope.user_id,
                    subject_id=target.subject_id,
                    fact_type=candidate.fact_type,
                    category=decision.definition.category,
                    display_name=candidate.display_name,
                    value_type=candidate.value_type,
                    value=deepcopy(candidate.value),
                    interpretation=candidate.interpretation,
                    reference_range=deepcopy(candidate.reference_range),
                    assertion_level=candidate.assertion_level,
                    verification_status=decision.verification_status,
                    confidence=candidate.confidence,
                    effective_period=candidate.effective_period,
                    status=decision.fact_status,
                    sources=(candidate.source,),
                    created_at=now,
                    updated_at=now,
                    creation_request_id=fact_id,
                    creation_request_slot='fact',
                    creation_request_fingerprint=profile_content_digest(candidate.model_dump(mode='json')),
                    basic_info_lifecycle_epoch=(
                        target.lifecycle_epoch if report_targets is not None else material.lifecycle_epoch
                    ),
                )
                subject_ids.add(target.subject_id)
                if key:
                    shared.setdefault(key, []).append(fact)
                    shared_aliases.setdefault(key, set()).update(aliases)
                    owned.pop(fact_id, None)
                    continue
                if self.basic_info_bridge is not None:
                    fence = await self.basic_info_bridge.prepare(
                        fact,
                        target,
                        _request_id(event, fact.fact_id),
                        admitted_at=publication.get('receivedAt'),
                        source_type=event.source_type,
                        source_id=event.source_id,
                        publication_revision=event.publication_revision,
                    )
                    fact = self.basic_info_bridge.tag_fact(fact, fence)
                creates.append(fact.model_dump(mode='python'))
        creates.extend(
            group_physical_reading(facts, event, key, shared_aliases[key]).model_dump(mode='python')
            for key, facts in shared.items()
        )
        if self.basic_info_bridge is not None and not partial:
            for fact in owned.values():
                target = await self._target_subject(event, fact)
                await self.basic_info_bridge.prepare(
                    fact,
                    target,
                    _request_id(event, fact.fact_id),
                    admitted_at=publication.get('receivedAt'),
                    effect='withdraw',
                    source_type=event.source_type,
                    source_id=event.source_id,
                    publication_revision=event.publication_revision,
                )
        plan = {
            'creates': creates,
            'withdraws': [] if partial else [fact.model_dump(mode='python') for fact in owned.values()],
            'activeFactIds': [fact['fact_id'] for fact in creates],
            'reasonCodes': list(dict.fromkeys(reasons)),
            'subjectIds': sorted(subject_ids),
        }
        if publication.get('settlesPublicationIds'):
            plan['settlesPublicationIds'] = list(publication['settlesPublicationIds'])
            plan['settlementFromRevision'] = publication['settlementFromRevision']
        if len(TypeAdapter(dict).dump_json(plan)) > 4_000_000:
            raise ProfileSourceApplicationError('source.application_plan_too_large')
        return plan

    async def _check_plan_human_decisions(self, event, plan):
        if event.event_kind == 'retract':
            return
        for raw in plan['withdraws']:
            fact = await self.repository.get_fact(event.scope.user_id, raw['subject_id'], raw['fact_id'])
            if fact is not None and _human_owned(fact):
                raise ProfileSourceApplicationError('source.manual_governance_requires_review')
            if (
                fact is None
                and await self.repository.get_deleted_fact_tombstone(
                    event.scope.user_id,
                    raw['subject_id'],
                    raw['fact_id'],
                )
                is not None
            ):
                raise ProfileSourceApplicationError('source.manual_governance_requires_review')

    async def _check_deleted_source_facts(self, event, *, subject_id):
        before = None
        for _ in range(100):
            page = await self.repository.list_event_page(
                event.scope.user_id,
                subject_id,
                event_types=('fact_ingested', 'fact_enriched', 'source_publication_applied'),
                before_sequence=before,
                limit=100,
            )
            for change in page:
                ref = change.source_ref or {}
                if (
                    ref.get('sourceType') == event.source_type
                    and ref.get('sourceId') == event.source_id
                    and change.fact_id
                    and await self.repository.get_deleted_fact_tombstone(
                        event.scope.user_id, subject_id, change.fact_id
                    )
                    is not None
                ):
                    raise ProfileSourceApplicationError('source.manual_governance_requires_review')
            if len(page) < 100:
                return
            next_before = min(change.sequence for change in page)
            if before is not None and next_before >= before:
                break
            before = next_before
        raise ProfileSourceApplicationError('source.governance_history_limit_exceeded')

    async def _create(self, event, subject, requested, *, suppressed, admitted_at=None):
        request_id = _request_id(event, requested.fact_id)
        if await self.repository.get_event_by_request(event.scope.user_id, request_id):
            if self.basic_info_bridge is not None:
                existing = await self.repository.get_fact(event.scope.user_id, subject.subject_id, requested.fact_id)
                if existing is not None:
                    await self.basic_info_bridge.repair(
                        existing,
                        subject,
                        request_id,
                        proof_epoch=requested.basic_info_lifecycle_epoch,
                        source_type=event.source_type,
                        source_id=event.source_id,
                        publication_revision=event.publication_revision,
                    )
            return
        fence = None
        if self.basic_info_bridge is not None:
            fence = await self.basic_info_bridge.prepare(
                requested,
                subject,
                request_id,
                admitted_at=admitted_at,
                source_type=event.source_type,
                source_id=event.source_id,
                publication_revision=event.publication_revision,
            )
            requested = self.basic_info_bridge.tag_fact(requested, fence)
        stored = await self.repository.get_fact(event.scope.user_id, subject.subject_id, requested.fact_id)
        if stored is None:
            if (
                await self.repository.get_deleted_fact_tombstone(
                    event.scope.user_id, subject.subject_id, requested.fact_id
                )
                is not None
            ):
                return
            if suppressed:
                requested = requested.model_copy(
                    update={
                        'status': FactStatus.RETRACTED,
                        'last_request_id': request_id,
                    }
                )
            try:
                stored, _ = await self.repository.create_fact(requested)
            except DuplicateKeyError:
                if (
                    await self.repository.get_deleted_fact_tombstone(
                        event.scope.user_id, subject.subject_id, requested.fact_id
                    )
                    is not None
                ):
                    return
                raise
        # Existing rows, including a later retraction or human edit, are never
        # overwritten by an older source create.
        if self.basic_info_bridge is not None:
            stored = await self.basic_info_bridge.ensure_tag(fence, stored)
            await self.basic_info_bridge.publish(fence, stored)
        await self._record_effect(event, subject, stored, request_id)

    async def _withdraw(self, event, subject, seed, *, request_suffix='', admitted_at=None):
        request_id = _request_id(event, seed.fact_id) + request_suffix
        if await self.repository.get_event_by_request(event.scope.user_id, request_id):
            if self.basic_info_bridge is not None:
                existing = await self.repository.get_fact(event.scope.user_id, subject.subject_id, seed.fact_id)
                if existing is not None:
                    await self.basic_info_bridge.repair(
                        existing,
                        subject,
                        request_id,
                        proof_epoch=seed.basic_info_lifecycle_epoch,
                        source_type=event.source_type,
                        source_id=event.source_id,
                        publication_revision=event.publication_revision,
                    )
            return
        fence = None
        if self.basic_info_bridge is not None:
            fence = await self.basic_info_bridge.prepare(
                seed,
                subject,
                request_id,
                admitted_at=admitted_at,
                effect='withdraw',
                source_type=event.source_type,
                source_id=event.source_id,
                publication_revision=event.publication_revision,
            )
            seed = self.basic_info_bridge.tag_fact(seed, fence)
        for _ in range(16):
            stored = await self.repository.get_fact(event.scope.user_id, subject.subject_id, seed.fact_id)
            if stored is None:
                if (
                    await self.repository.get_deleted_fact_tombstone(
                        event.scope.user_id, subject.subject_id, seed.fact_id
                    )
                    is not None
                ):
                    return
                tombstone = seed.model_copy(
                    update={
                        'status': FactStatus.RETRACTED,
                        'last_request_id': request_id,
                        'updated_at': self.inbox.now(),
                    }
                )
                try:
                    stored, created = await self.repository.create_fact(tombstone)
                except DuplicateKeyError:
                    continue
                if created:
                    break
            if not _has_source(stored, event):
                break
            remaining = tuple(source for source in stored.sources if not _same_source(source, event))
            human = _human_owned(stored)
            if (
                human
                and fact_user_confirmed(stored)
                and not any(source.source_type == 'manual' for source in remaining)
            ):
                remaining = (
                    *remaining,
                    FactSource(
                        source_type='manual',
                        source_id=stored.last_request_id or stored.fact_id,
                        source_revision=1,
                        pointer=stored.fact_id,
                    ),
                )
            target_status = stored.status if remaining or human else FactStatus.RETRACTED
            if not remaining:
                remaining = stored.sources  # Retired facts retain their historical provenance.
            if stored.status == target_status and stored.sources == remaining:
                break
            updated = stored.model_copy(
                update={
                    'sources': remaining,
                    'status': target_status,
                    **({'basic_info_lifecycle_epoch': fence['lifecycleEpoch']} if fence is not None else {}),
                    'last_request_id': stored.last_request_id if human else request_id,
                    'version': stored.version + 1,
                    'updated_at': self.inbox.now(),
                }
            )
            result = await self.repository.replace_fact(
                updated,
                expected_version=stored.version,
                allowed_statuses=_READ_STATUSES,
            )
            if result is not None:
                stored = result
                break
        else:
            raise ProfileSourceApplicationError('source.concurrent_fact_update', retryable=True)
        if self.basic_info_bridge is not None:
            stored = await self.basic_info_bridge.ensure_tag(fence, stored)
            await self.basic_info_bridge.publish(fence, stored)
        await self._record_effect(event, subject, stored, request_id)

    async def _record_effect(self, event, subject, fact, request_id):
        view = await self.writer._advance_view(
            user_id=event.scope.user_id,
            subject=subject,
            request_id=request_id,
            updated_at=self.inbox.now(),
        )
        if self.writer._basic_info is not None:
            view = await self.writer._basic_info.publish_governance(event.scope.user_id, subject.subject_id) or view
        await self.repository.append_event(
            ProfileEvent(
                event_id='event_' + profile_content_digest(request_id),
                user_id=event.scope.user_id,
                subject_id=subject.subject_id,
                sequence=view.profile_version,
                event_type='source_publication_applied',
                fact_id=fact.fact_id,
                new_fact_version=fact.version,
                reason_code='source_' + event.event_kind,
                actor=ProfileActor(type='system'),
                request_id=request_id,
                source_ref={
                    'sourceType': event.source_type,
                    'sourceId': event.source_id,
                    'sourceRevision': event.source_revision,
                    'publicationRevision': event.publication_revision,
                },
                operation_result={'profileVersion': view.profile_version, 'status': fact.status.value},
                created_at=self.inbox.now(),
            )
        )


class ProfilePublicationWorker:
    def __init__(self, inbox, applier, *, max_attempts=5, lease_seconds=120, waiting_timeout=timedelta(days=1)):
        self.inbox, self.applier = inbox, applier
        self.max_attempts, self.lease_seconds = max_attempts, lease_seconds
        if max_attempts < 1 or lease_seconds < 1 or waiting_timeout <= timedelta(0):
            raise ValueError('publication recovery budgets must be positive')
        self.waiting_timeout = waiting_timeout

    async def recover_pending(self, *, limit=100):
        await self.inbox.repair_staged(limit=limit)
        rows = await self.inbox.store.scan({'recordKind': 'cursor', 'status': 'pending'}, limit)
        applied = 0
        for row in rows:
            cursor = await self.inbox.claim(row, lease_seconds=self.lease_seconds)
            if cursor is None:
                continue
            try:
                identity = self._publication_id(cursor, settled_revision(cursor) + 1)
                publication = await self.inbox.store.get(identity)
                if publication is None:
                    await self._expire_waiting(cursor)
                    continue
                if publication['status'] in {'failed', 'partial'}:
                    await self._finish_failed_ownership(cursor, publication)
                    publication = await self._terminal_retraction(cursor)
                    if publication is None:
                        await self._expire_waiting(cursor)
                        continue
                    identity = publication['_id']
                if publication.get('nextRetryAt') and publication['nextRetryAt'] > self.inbox.now():
                    continue
                await self.inbox.check_lease(cursor)
                if (
                    publication.get('extractionFailure')
                    and (publication.get('partialApplication') or {}).get('status') == 'pending'
                ):
                    await self._apply_partial(cursor, publication)
                    continue
                if publication['attempts'] >= self.max_attempts:
                    publication = await self._fail(cursor, identity, 'source.attempts_exhausted', max_attempts=1)
                    await self._start_partial(cursor, publication, 'source.attempts_exhausted')
                    continue
                settlement = {
                    key: publication[key]
                    for key in ('settlesPublicationIds', 'settlementFromRevision')
                    if key in publication
                }
                publication = await self.inbox.mark_processing(identity)
                publication.update(settlement)
                try:
                    result = await self.applier.apply(cursor, publication)
                    await self.inbox.complete(cursor, publication, result)
                    applied += 1
                except ProfileSourceLeaseError:
                    continue
                except ProfileSourceApplicationError as error:
                    await self.inbox.check_lease(cursor)
                    failed = await self._fail(
                        cursor, identity, error.code, max_attempts=self.max_attempts if error.retryable else 1
                    )
                    if failed['status'] == 'failed':
                        await self._start_partial(cursor, failed, error.code)
                except Exception:
                    current = await self.inbox.check_lease(cursor)
                    event = ProfileSourceEventV1.model_validate(publication['event'])
                    if (
                        settled_revision(current) >= event.publication_revision
                        and current['appliedPublicationRevision'] == event.publication_revision
                    ):
                        await self.inbox._update(
                            identity,
                            lambda record: record.update(status='applied', errorCode=None, updatedAt=self.inbox.now()),
                        )
                    else:
                        await self._fail(cursor, identity, 'source.application_failed', max_attempts=self.max_attempts)
            except ProfileSourceLeaseError:
                continue
            except Exception as error:
                # A durable intent may already exist when its ACK is lost. Leave
                # it recoverable; an individual source must not stop this batch.
                logger.warning('Profile source recovery deferred: error_type=%s', type(error).__name__)
            finally:
                await self.inbox.release(cursor)
        return applied

    async def _fail(self, cursor, identity, code, *, max_attempts):
        publication = await self.inbox.fail(identity, code, max_attempts=max_attempts)
        if publication['status'] == 'failed':
            await self._finish_failed_ownership(cursor, publication)
        return publication

    async def _finish_failed_ownership(self, cursor, publication):
        epoch = publication.get('lifecycleEpoch')
        if publication.get('currentDocument') and epoch is not None:
            await self.inbox.check_lease(cursor)
            event = ProfileSourceEventV1.model_validate(publication['event'])
            await self.applier.report_ownership.finish(event.scope, epoch, publication['_id'], failed=True)

    @staticmethod
    def _publication_id(cursor, revision):
        return 'publication_' + profile_content_digest([cursor['_id'].removeprefix('cursor_'), revision])

    async def _terminal_retraction(self, cursor):
        """Skip only a contiguous accepted chain, and only into an explicit retraction.

        No facts are changed here. The applier freezes all predecessor plan seeds,
        withdraws them, then complete() recoverably seals the predecessor receipts.
        """
        start = settled_revision(cursor)
        if cursor.get('settlementBlockedRevision') == cursor['acceptedRetractionRevision']:
            return None
        prior = await self.inbox.store.get(self._publication_id(cursor, start)) if start else None
        material_revision = ProfileSourceEventV1.model_validate(prior['event']).source_revision if prior else 0
        supersedes = []
        scan_end = min(cursor['acceptedRetractionRevision'], start + MAX_UNSETTLED_SOURCE_REVISIONS + 1)
        for revision in range(start + 1, scan_end + 1):
            await self.inbox.check_lease(cursor)
            row = await self.inbox.store.get(self._publication_id(cursor, revision))
            if row is None:
                return None
            event = ProfileSourceEventV1.model_validate(row['event'])
            if event.source_revision < material_revision:
                return None
            material_revision = event.source_revision
            if event.event_kind == 'retract':
                if row['status'] == 'failed':
                    supersedes.append(row['_id'])
                    continue
                row['settlesPublicationIds'] = supersedes
                row['settlementFromRevision'] = start
                return row
            supersedes.append(row['_id'])
        if cursor['acceptedRetractionRevision'] > scan_end:
            identity = self._publication_id(cursor, cursor['acceptedRetractionRevision'])
            if await self.inbox.store.get(identity) is not None:
                await self.inbox.check_lease(cursor)
                await self.inbox.fail(identity, 'source.settlement_revision_limit_exceeded', max_attempts=0)

                def block(row):
                    if row.get('leaseToken') != cursor['leaseToken'] or row['leaseExpiresAt'] <= self.inbox.now():
                        raise ProfileSourceLeaseError('stale worker cannot block settlement')
                    row['settlementBlockedRevision'] = cursor['acceptedRetractionRevision']

                await self.inbox._update(cursor['_id'], block)
        return None

    async def _start_partial(self, cursor, publication, error_code):
        if not (
            error_code.startswith('extractor.')
            or error_code in {'source.attempts_exhausted', 'source.extraction_version_unavailable'}
        ):
            return
        if (publication.get('processingSelection') or {}).get('implementationRevision') != 'profile-record-source-v2':
            return
        if publication.get('basicCandidatesCheckpoint') is None or publication.get('extractedCandidates') is not None:
            return
        publication = await self.inbox.begin_partial(cursor, publication['_id'], error_code)
        await self._apply_partial(cursor, publication)

    async def _apply_partial(self, cursor, publication):
        partial = publication['partialApplication']
        if partial['status'] == 'applied':
            await self.inbox.complete_partial(cursor, publication, partial['result'])
            return
        if partial['attempts'] >= self.max_attempts:
            await self.inbox.fail_partial(
                cursor, publication['_id'], 'source.partial_attempts_exhausted', max_attempts=1
            )
            return
        publication = await self.inbox.claim_partial(cursor, publication['_id'])
        try:
            result = await self.applier.apply(cursor, publication, partial=True)
            await self.inbox.complete_partial(cursor, publication, result)
        except ProfileSourceLeaseError:
            raise
        except ProfileSourceApplicationError as error:
            await self.inbox.fail_partial(
                cursor, publication['_id'], error.code, max_attempts=self.max_attempts if error.retryable else 1
            )
        except Exception:
            await self.inbox.fail_partial(
                cursor, publication['_id'], 'source.partial_application_failed', max_attempts=self.max_attempts
            )

    async def _expire_waiting(self, cursor):
        await self.inbox.check_lease(cursor)
        rows = await self.inbox.store.scan(
            {
                'recordKind': 'publication',
                'status': 'waiting',
                'scope.userId': cursor['scope']['userId'],
                'scope.subjectId': cursor['scope']['subjectId'],
                'event.sourceType': cursor['sourceType'],
                'event.sourceId': cursor['sourceId'],
            },
            limit=1000,
        )
        for row in rows:
            if row['receivedAt'] + self.waiting_timeout <= self.inbox.now():
                await self.inbox._update(
                    row['_id'],
                    lambda current: current.update(
                        status='failed',
                        errorCode='source.prior_revision_timeout',
                        updatedAt=self.inbox.now(),
                    ),
                )


class CompositeProfilePendingSource:
    def __init__(self, *sources):
        self.sources = sources

    async def recover_pending(self, *, limit=100):
        return sum([await source.recover_pending(limit=limit) for source in self.sources])


def _same_source(source, event):
    return source.source_type == event.source_type and source.source_id == event.source_id


def _has_source(fact, event):
    if fact.physical_occurrence_key:
        return event.source_type == 'health_report' and any(
            origin.source_id == event.source_id and origin.reading is not None for origin in fact.physical_origins
        )
    return any(_same_source(source, event) for source in fact.sources)


def _request_id(event, fact_id):
    return f'profile_publication:{publication_id(event)}:{fact_id}'
