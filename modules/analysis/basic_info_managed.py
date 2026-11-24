"""Durable current round recovery and explicit contribution retirement, without models."""

import json
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace

from modules.analysis.basic_info_contracts import ANALYSIS_PUBLIC_CONFIRMATION
from modules.analysis.basic_info_contracts import DocumentCurrentSourcePlan
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import DocumentRetiredContribution
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.managed_source import _require
from modules.analysis.managed_source_contracts import MAX_MANAGED_TRANSITIONS
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest

MANAGED_BASIC_INFO_REVISION = 'document-basic-info-managed-v2'


@dataclass(frozen=True, slots=True)
class DocumentCurrentManagedCommand:
    scope: DocumentScope
    source_id: str
    request_id: str
    lifecycle_epoch: int
    prior_publication_revision: int
    prior_source_snapshot_ref: str
    understanding_snapshot_ref: str | None = None
    retire_contribution_ids: tuple[str, ...] = ()
    command_revision: str = MANAGED_BASIC_INFO_REVISION
    report_projection_ref: str | None = None
    replace_report_contribution_id: str | None = None
    occurrence_projection_ref: str | None = None
    confirmation_basis: str | None = None

    def __post_init__(self):
        if not isinstance(self.scope, DocumentScope):
            raise TypeError('an explicit Document scope is required')
        for value in (self.source_id, self.request_id, self.prior_source_snapshot_ref):
            exact_text(value, 'fixed current command identity')
        if type(self.lifecycle_epoch) is not int or self.lifecycle_epoch < 1:
            raise ValueError('the original subject lifecycle is required')
        if type(self.prior_publication_revision) is not int or self.prior_publication_revision < 1:
            raise ValueError('the exact applied prior revision is required')
        if self.command_revision != MANAGED_BASIC_INFO_REVISION:
            raise ValueError('the exact current managed command implementation is required')
        if not isinstance(self.retire_contribution_ids, tuple) or len(self.retire_contribution_ids) > 1000:
            raise ValueError('retirement identities must be a bounded fixed tuple')
        selected = (
            self.understanding_snapshot_ref is not None,
            bool(self.retire_contribution_ids),
            self.report_projection_ref is not None,
        )
        if sum(selected) != 1:
            raise ValueError('select exactly one fixed round, Report projection, or explicit retirement set')
        if self.report_projection_ref is not None:
            exact_text(self.report_projection_ref, 'fixed Report projection reference')
        if self.replace_report_contribution_id is not None:
            if self.report_projection_ref is None:
                raise ValueError('an exact Report replacement requires a fixed Report projection')
            exact_text(self.replace_report_contribution_id, 'fixed Report replacement contribution')
        if self.occurrence_projection_ref is not None:
            exact_text(self.occurrence_projection_ref, 'fixed Analysis occurrence proof')
            if self.understanding_snapshot_ref is None:
                raise ValueError('original occurrence proof requires a fixed Analysis round')
        if self.confirmation_basis is not None and (
            self.confirmation_basis != ANALYSIS_PUBLIC_CONFIRMATION
            or self.understanding_snapshot_ref is None
            or self.occurrence_projection_ref is not None
        ):
            raise ValueError('default confirmation requires a fixed direct Analysis round')
        if self.understanding_snapshot_ref is not None:
            exact_text(self.understanding_snapshot_ref, 'fixed understanding reference')
        for value in self.retire_contribution_ids:
            exact_text(value, 'retired contribution')
        if len(set(self.retire_contribution_ids)) != len(self.retire_contribution_ids):
            raise ValueError('retirement identities must be unique')

    @property
    def command_id(self):
        return 'document_basic_info_managed_' + digest([self.scope.as_dict(), self.source_id, self.request_id])

    def as_dict(self):
        value = {
            'scope': self.scope.as_dict(),
            'sourceId': self.source_id,
            'requestId': self.request_id,
            'lifecycleEpoch': self.lifecycle_epoch,
            'priorPublicationRevision': self.prior_publication_revision,
            'priorSourceSnapshotRef': self.prior_source_snapshot_ref,
            'understandingSnapshotRef': self.understanding_snapshot_ref,
            'retireContributionIds': list(self.retire_contribution_ids),
            'commandRevision': self.command_revision,
        }
        if self.report_projection_ref is not None:
            value['reportProjectionRef'] = self.report_projection_ref
            value['replaceReportContributionId'] = self.replace_report_contribution_id
        if self.occurrence_projection_ref is not None:
            value['occurrenceProjectionRef'] = self.occurrence_projection_ref
        if self.confirmation_basis is not None:
            value['confirmationBasis'] = self.confirmation_basis
        return value

    @classmethod
    def from_dict(cls, raw):
        expected = {
            'scope',
            'sourceId',
            'requestId',
            'lifecycleEpoch',
            'priorPublicationRevision',
            'priorSourceSnapshotRef',
            'understandingSnapshotRef',
            'retireContributionIds',
            'commandRevision',
        }
        report_fields = {'reportProjectionRef', 'replaceReportContributionId'}
        if (
            set(raw)
            not in (
                expected,
                expected | report_fields,
                expected | {'occurrenceProjectionRef'},
                expected | {'confirmationBasis'},
            )
            or ('confirmationBasis' in raw and raw['confirmationBasis'] is None)
            or ('occurrenceProjectionRef' in raw and raw['occurrenceProjectionRef'] is None)
            or ('reportProjectionRef' in raw and raw['reportProjectionRef'] is None)
            or set(raw['scope']) != {'userId', 'subjectId'}
        ):
            raise ValueError('the exact persisted current managed command is required')
        return cls(
            DocumentScope(raw['scope']['userId'], raw['scope']['subjectId']),
            raw['sourceId'],
            raw['requestId'],
            raw['lifecycleEpoch'],
            raw['priorPublicationRevision'],
            raw['priorSourceSnapshotRef'],
            raw['understandingSnapshotRef'],
            tuple(raw['retireContributionIds']),
            raw['commandRevision'],
            raw.get('reportProjectionRef'),
            raw.get('replaceReportContributionId'),
            raw.get('occurrenceProjectionRef'),
            raw.get('confirmationBasis'),
        )


class DocumentCurrentManagedPlanner:
    """One pure transition algorithm, shared by command preparation and proof reads."""

    async def _submitted_rounds(self, command, incoming, *, memo):
        if command.understanding_snapshot_ref is not None:
            bundle = await self.complete._bundle_at(command.scope, command.understanding_snapshot_ref, memo=memo)
            return {bundle.material.payload['documentId']: bundle.facts.payload['interpretationRound']}
        if command.report_projection_ref is not None and incoming:
            return await self.complete.report_input_rounds(
                scope=command.scope,
                projection_snapshot_ref=command.report_projection_ref,
                lifecycle_epoch=command.lifecycle_epoch,
                memo=memo,
            )
        return {}

    @staticmethod
    def _predecessor_proof(command, transition):
        event = transition['priorEvent']
        return {
            'commandId': command.command_id,
            'priorPublicationRevision': command.prior_publication_revision,
            'priorEventId': event['eventId'],
            'priorSourceSnapshotRef': command.prior_source_snapshot_ref,
            'priorContentDigest': event['contentDigest'],
            'interpretationRounds': transition['interpretationRounds'],
        }

    @classmethod
    def _noop_proof(cls, command, transition):
        return {
            **cls._predecessor_proof(command, transition),
            'lifecycleEpoch': command.lifecycle_epoch,
            'commandDigest': digest(transition),
        }

    async def _derive_current(self, command, prior, *, incoming=(), memo=None):
        _require(
            isinstance(prior, DocumentCurrentSourcePlan)
            and prior.lifecycle_epoch == command.lifecycle_epoch
            and prior.scope == command.scope
            and prior.source_id == command.source_id,
            'the original source lifecycle differs',
        )
        if command.understanding_snapshot_ref is not None:
            proposal = await self.complete._derive_replacement(
                scope=command.scope,
                prior=prior,
                understanding_snapshot_id=command.understanding_snapshot_ref,
                incoming=incoming,
                occurrence_projection_ref=command.occurrence_projection_ref,
                confirmation_basis=command.confirmation_basis,
                memo=memo,
            )
            active, retired, evidence = proposal.active, proposal.retired, proposal.evidence
        elif command.report_projection_ref is not None:
            proposal = await self.complete._derive_report_replacement(
                scope=command.scope,
                prior=prior,
                projection_snapshot_ref=command.report_projection_ref,
                replace_report_contribution_id=command.replace_report_contribution_id,
                incoming=incoming,
                memo=memo,
            )
            active, retired, evidence = proposal.active, proposal.retired, proposal.evidence
        else:
            _require(not incoming, 'explicit retirement cannot add contributions')
            selected, active, retired, evidence = set(command.retire_contribution_ids), [], list(prior.retired), []
            for handle in prior.active:
                snapshot, _ = await self.complete._handle(command.scope, handle, memo=memo)
                if snapshot.payload['contributionId'] not in selected:
                    active.append(handle)
                    continue
                selected.remove(snapshot.payload['contributionId'])
                detail = {
                    'kind': 'explicit_contribution_retirement',
                    'commandId': command.command_id,
                    'priorPlanRef': prior.active_set_evidence_ref,
                    'retiredHandle': asdict(handle),
                }
                ref = 'document_retirement_' + digest(detail)
                evidence.append({'evidenceRef': ref, **detail})
                retired.append(DocumentRetiredContribution(handle, 'retracted', ref))
            _require(not selected, 'retirement does not select exact active contributions')
            active_ids = {
                (await self.complete._handle(command.scope, h, memo=memo))[0].payload['contributionId'] for h in active
            }
            retired = [
                replace(row, superseded_by_contribution_id=None)
                if row.superseded_by_contribution_id not in active_ids
                else row
                for row in retired
            ]
            active, retired = tuple(active), tuple(retired)
        if active == prior.active and retired == prior.retired:
            return prior, evidence, True
        plan = DocumentCurrentSourcePlan(
            scope=prior.scope,
            source_id=prior.source_id,
            source_revision=prior.source_revision,
            active=active,
            retired=retired,
            fence_evidence_ref=command.command_id,
            active_set_evidence_ref=command.command_id,
            _fence_json=canonical_json({'admissionKind': 'current', 'evidenceRef': command.command_id}),
            transition_ref=command.command_id,
            lifecycle_epoch=prior.lifecycle_epoch,
        )
        return plan, evidence, False


class DocumentCurrentPlanReader(DocumentCurrentManagedPlanner):
    def __init__(self, *, records, ledger, initial_admission, artifacts):
        self.records, self.ledger, self.initial_admission = records, ledger, initial_admission
        self.complete = artifacts

    async def read_original_source_plan(self, *, scope, source_id):
        source = await self.ledger.read_source(scope, source_id)
        binding = (source or {}).get('currentSourceBinding')
        _require(
            isinstance(binding, dict)
            and binding.get('scope') == scope.as_dict()
            and binding.get('sourceId') == source_id,
            'the original source binding is unavailable',
        )
        plan = await self.initial_admission.read_verified_plan(scope=scope, plan_ref=binding['planRef'])
        _require(
            plan.source_id == source_id and plan.lifecycle_epoch == binding['lifecycleEpoch'],
            'the original source plan identity differs',
        )
        return plan

    async def read_transition_binding(self, *, scope, plan_ref, memo=None):
        if memo is None:
            memo = {}
        await self.read_verified_plan(scope=scope, plan_ref=plan_ref, memo=memo)
        binding = memo.get(('transition', scope, plan_ref))
        return dict(binding) if binding is not None else None

    async def read_verified_plan(self, *, scope, plan_ref, memo=None):
        if memo is None:
            memo = {}
        chain, seen, current = [], set(), plan_ref
        for _ in range(MAX_MANAGED_TRANSITIONS):
            key = ('plan', scope, current)
            if key in memo:
                plan = memo[key]
                break
            row = await self.records.get('basic_info_managed_transition', scope, current)
            if row is None:
                plan = await self.initial_admission.read_verified_plan(scope=scope, plan_ref=current, memo=memo)
                memo[key] = plan
                break
            _require(current not in seen, 'current transition history contains a cycle')
            seen.add(current)
            chain.append((current, row))
            current = row['priorPlanRef']
        else:
            raise PublicationConflictError('current transition history exceeds the supported bound')
        for ref, row in reversed(chain):
            command = DocumentCurrentManagedCommand.from_dict(row['command'])
            _require(command.scope == scope and command.command_id == ref, 'fixed current transition command differs')
            _require(
                await self.records.require('basic_info_managed_command', scope, ref) == row['command'],
                'fixed current manifest differs',
            )
            event = row['priorEvent']
            intent = await self.ledger.read_intent(scope, event['eventId'])
            _require(intent is not None and intent['event'] == event, 'the original prior event is unavailable')
            receipt = PublicationReceipt(**row['priorAppliedReceipt'])
            _require(
                receipt.state == 'applied'
                and receipt.application_state == 'applied'
                and receipt.publication_revision == command.prior_publication_revision
                and receipt.source_id == command.source_id
                and receipt.content_digest == event['contentDigest']
                and event['sourceId'] == command.source_id
                and event['publicationRevision'] == command.prior_publication_revision
                and event['eventKind'] != 'retract'
                and event['snapshotRef']['contractVersion'] == COMPLETE_SOURCE_CONTRACT_V2
                and event['snapshotRef']['snapshotId'] == command.prior_source_snapshot_ref,
                'the original applied publication proof differs',
            )
            source, inventory, proof = await self.complete._source_from_plan(
                scope, row['priorPlanRef'], plan, memo=memo
            )
            _require(
                source.snapshot_id == command.prior_source_snapshot_ref
                and source.content_digest == event['contentDigest'],
                'the prior source binding differs',
            )
            for snapshot in (source, inventory, proof):
                _require(
                    await self.complete._snapshot_at(scope, snapshot.snapshot_id, memo=memo) == snapshot,
                    'the prior fixed source proof differs',
                )
            incoming = tuple(DocumentContributionHandle(**handle) for handle in row['incoming'])
            plan, evidence, no_change = await self._derive_current(command, plan, incoming=incoming, memo=memo)
            _require(
                row['interpretationRounds'] == await self._submitted_rounds(command, incoming, memo=memo),
                'the submitted interpretation rounds differ from the fixed inputs',
            )
            _require(
                digest(asdict(plan)) == digest(row['plan']) and no_change == row['noChange'],
                'the durable current effective inventory differs',
            )
            for expected in evidence:
                _require(
                    await self.records.require('basic_info_retirement', scope, expected['evidenceRef']) == expected,
                    'the original retirement evidence differs',
                )
            memo['plan', scope, ref] = plan
            if not no_change:
                memo['transition', scope, ref] = {
                    **self._predecessor_proof(command, row),
                    'priorPlanRef': row['priorPlanRef'],
                }
        return plan


class DocumentCurrentManagedSource(DocumentCurrentManagedPlanner):
    def __init__(self, *, records, ledger, initial_admission, complete, publisher, plans):
        self.records, self.ledger, self.initial_admission = records, ledger, initial_admission
        self.complete, self.publisher, self.plans = complete, publisher, plans

    async def read_verified_plan(self, *, scope, plan_ref, memo=None):
        return await self.plans.read_verified_plan(scope=scope, plan_ref=plan_ref, memo=memo)

    async def _prior(self, command):
        if command.prior_publication_revision >= MAX_MANAGED_TRANSITIONS:
            raise PublicationConflictError('managed history budget exhausted; source retraction remains available')
        source = await self.ledger.read_source(command.scope, command.source_id)
        if source is None or source['mode'] != 'events':
            raise PublicationConflictError('a managed source with an applied current inventory is required')
        current = source.get('currentIntent')
        if (
            current is None
            or source['publicationRevision'] != command.prior_publication_revision
            or source['appliedRevision'] != command.prior_publication_revision
            or current['event']['eventKind'] == 'retract'
            or source.get('reservedRetractionRevision', 0) >= command.prior_publication_revision
            or current['event']['snapshotRef']['snapshotId'] != command.prior_source_snapshot_ref
        ):
            raise PublicationConflictError('managed transition has a stale, withdrawn or unapplied prior source')
        intent = await self.ledger.read_intent(command.scope, current['_id'])
        receipt = (intent or {}).get('receipt') or {}
        if receipt.get('state') != 'applied' or receipt.get('application_state') != 'applied':
            raise PublicationConflictError('the exact prior publication lacks its own applied acknowledgement')
        snapshot = await self.complete.read_source_observations(
            scope=command.scope, snapshot_id=command.prior_source_snapshot_ref
        )
        if snapshot['sourceId'] != command.source_id or digest(snapshot) != current['event']['contentDigest']:
            raise PublicationConflictError('the prior inventory differs from its applied event')
        inventory = await self.complete.read_source_inventory(
            scope=command.scope, snapshot_id=snapshot['payload']['coverage']['inventorySnapshotRef']['snapshotId']
        )
        return current['event'], inventory['payload']['activeSetEvidenceRef'], receipt

    async def _prepare_transition(self, command):
        scope, key = command.scope, command.command_id
        try:
            event, prior_ref, receipt = await self._prior(command)
        except PublicationConflictError:
            # A concurrent executor may have advanced the predecessor after our
            # first read. Only the same frozen command can reuse its transition.
            transition = await self.records.get('basic_info_managed_transition', scope, key)
            if transition is None:
                raise
            if transition['command'] != command.as_dict():
                raise PublicationConflictError('current request changed its fixed inputs or lifecycle') from None
            return transition
        memo = {}
        prior = await self.read_verified_plan(scope=scope, plan_ref=prior_ref, memo=memo)
        _require(prior.lifecycle_epoch == command.lifecycle_epoch, 'the original source lifecycle differs')
        understanding = command.understanding_snapshot_ref
        if understanding is not None:
            await self.complete._verify_replacement_round(scope, prior, understanding, memo=memo)
        if command.report_projection_ref is None:
            await self.records.put('basic_info_managed_command', scope, key, command.as_dict())
        incoming = ()
        if understanding is not None:
            incoming = await self.complete.capture_contributions(
                scope=scope,
                understanding_snapshot_id=understanding,
                occurrence_projection_ref=command.occurrence_projection_ref,
                confirmation_basis=command.confirmation_basis,
                epoch=command.lifecycle_epoch
                if command.occurrence_projection_ref or command.confirmation_basis
                else None,
                memo=memo,
            )
        if command.report_projection_ref is not None:
            report = await self.complete.capture_managed_report_contribution(
                scope=scope,
                prior=prior,
                projection_snapshot_ref=command.report_projection_ref,
                memo=memo,
            )
            if report is not None:
                incoming = (report,)
        plan, evidence, no_change = await self._derive_current(command, prior, incoming=incoming, memo=memo)
        if command.report_projection_ref is not None:
            await self.complete._source_from_plan(scope, plan.active_set_evidence_ref, plan, memo=memo)
            await self.records.put('basic_info_managed_command', scope, key, command.as_dict())
        for row in evidence:
            await self.records.put('basic_info_retirement', scope, row['evidenceRef'], row)
        transition = {
            'command': command.as_dict(),
            'priorEvent': event,
            'priorAppliedReceipt': receipt,
            'priorPlanRef': prior_ref,
            'incoming': [asdict(handle) for handle in incoming],
            'interpretationRounds': await self._submitted_rounds(command, incoming, memo=memo),
            'plan': asdict(plan),
            'noChange': no_change,
        }
        await self.records.put('basic_info_managed_transition', scope, key, transition)
        return transition

    async def execute(self, command):
        if not isinstance(command, DocumentCurrentManagedCommand):
            raise TypeError('the exact current managed command is required')
        scope, key = command.scope, command.command_id
        existing = await self.records.get('basic_info_managed_command', scope, key)
        if existing is not None and existing != command.as_dict():
            raise PublicationConflictError('current request changed its fixed inputs or lifecycle')
        if await self.records.get('basic_info_managed_result', scope, key) is not None:
            return await self.read_receipt(scope=scope, command_id=key)
        transition = await self.records.get('basic_info_managed_transition', scope, key)
        if transition is None:
            transition = await self._prepare_transition(command)
        if transition['command'] != command.as_dict():
            raise PublicationConflictError('current request changed its fixed inputs or lifecycle')
        if await self.records.get('basic_info_managed_result', scope, key) is not None:
            return await self.read_receipt(scope=scope, command_id=key)
        await self.read_verified_plan(scope=scope, plan_ref=key)
        if transition['noChange']:
            await self.ledger.accept_current_noop(scope, command.source_id, proof=self._noop_proof(command, transition))
            result = {
                'status': 'unchanged',
                'sourceSnapshotRef': command.prior_source_snapshot_ref,
                'publicationRevision': command.prior_publication_revision,
                'publication': None,
            }
        else:
            captured = await self.complete.capture_source(scope=scope, verified_plan_ref=key)
            publication = await self.publisher.publish(
                scope=scope,
                snapshot_id=captured.source_snapshot_ref,
                request_id=key,
                expected_prior_revision=command.prior_publication_revision,
                event_kind='correct',
            )
            result = {
                'status': 'published',
                'sourceSnapshotRef': captured.source_snapshot_ref,
                'publicationRevision': publication.publication_revision,
                'publication': asdict(publication),
            }
        result.update(
            scope=scope.as_dict(),
            sourceId=command.source_id,
            commandId=key,
            commandRevision=command.command_revision,
            lifecycleEpoch=command.lifecycle_epoch,
        )
        try:
            await self.records.put('basic_info_managed_result', scope, key, result)
        except DocumentCompleteSourceUnavailable:
            # Two workers can receive different states for the same event. Keep the
            # first durable acknowledgement and read live delivery from the ledger.
            winner = await self.read_receipt(scope=scope, command_id=key)
            _require(
                self._result_identity(winner) == self._result_identity(result),
                'the concurrent managed publication receipt identifies a different operation',
            )
            return winner
        return await self.read_receipt(scope=scope, command_id=key)

    @staticmethod
    def _result_identity(result):
        publication = result.get('publication')
        return (
            {key: value for key, value in result.items() if key != 'publication'},
            None
            if publication is None
            else tuple(
                publication[key] for key in ('source_id', 'publication_revision', 'content_digest', 'receipt_ref')
            ),
        )

    async def read_receipt(self, *, scope, command_id):
        result = await self.records.require('basic_info_managed_result', scope, command_id)
        command = DocumentCurrentManagedCommand.from_dict(
            await self.records.require('basic_info_managed_command', scope, command_id)
        )
        _require(
            command.command_id == command_id
            and command.scope == scope
            and result['commandId'] == command_id
            and result['scope'] == scope.as_dict()
            and result['sourceId'] == command.source_id
            and result['commandRevision'] == command.command_revision
            and result['lifecycleEpoch'] == command.lifecycle_epoch,
            'the fixed current result binding differs',
        )
        memo = {}
        plan = await self.read_verified_plan(scope=scope, plan_ref=command_id, memo=memo)
        if plan.active_set_evidence_ref != command_id:
            transition = await self.records.require('basic_info_managed_transition', scope, command_id)
            _require(
                await self.ledger.read_current_noop(scope, command.source_id, command_id)
                == self._noop_proof(command, transition),
                'the fixed no-change acceptance is unavailable',
            )
            _require(
                result['status'] == 'unchanged'
                and result['sourceSnapshotRef'] == command.prior_source_snapshot_ref
                and result['publicationRevision'] == command.prior_publication_revision
                and result['publication'] is None,
                'the fixed current no-op result differs',
            )
        else:
            snapshot, _, _ = await self.complete._source_from_plan(scope, command_id, plan, memo=memo)
            publication = PublicationReceipt(**result['publication'])
            reserved = await self.ledger.read_reserved(scope, command.source_id, command_id)
            event = (reserved or {}).get('event') or {}
            durable = (reserved or {}).get('receipt') or {}
            _require(
                result['status'] == 'published'
                and result['sourceSnapshotRef'] == snapshot.snapshot_id
                and result['publicationRevision'] == publication.publication_revision
                and publication.publication_revision == command.prior_publication_revision + 1
                and publication.source_id == command.source_id
                and publication.content_digest == snapshot.content_digest
                and event.get('scope') == scope.as_dict()
                and event.get('sourceId') == command.source_id
                and event.get('sourceRevision') == snapshot.source_revision
                and event.get('publicationRevision') == publication.publication_revision
                and event.get('priorRevision') == command.prior_publication_revision
                and event.get('eventKind') == 'correct'
                and event.get('contentDigest') == publication.content_digest
                and event.get('snapshotRef')
                == {
                    'provider': 'document',
                    'contractVersion': COMPLETE_SOURCE_CONTRACT_V2,
                    'snapshotId': snapshot.snapshot_id,
                }
                and publication.receipt_ref == durable.get('receipt_ref'),
                'the fixed current publication receipt differs',
            )
        return json.loads(canonical_json(result))
