"""Shared write-side entry for consumers of an immutable current preparation."""

from dataclasses import asdict

from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.basic_info_contracts import ANALYSIS_PUBLIC_CONFIRMATION
from modules.analysis.basic_info_managed import DocumentCurrentManagedCommand
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.managed_source_contracts import MAX_MANAGED_TRANSITIONS
from modules.analysis.profile_delivery import has_clinical_delivery
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.publication_recovery import PublicationRecoveryError
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT_V2
from modules.analysis.snapshots import digest


class DocumentCurrentPreparedConsumption:
    def __init__(self, *, admission, source, managed, publisher, ledger, records, evidence_projections=None):
        self.admission = admission
        self.source = source
        self.managed = managed
        self.publisher = publisher
        self.ledger = ledger
        self.records = records
        self.evidence_projections = evidence_projections

    async def consume_prepared(
        self,
        *,
        scope,
        prepared,
        lifecycle_epoch,
        request_id,
        report_projection_ref=None,
        replace_report_contribution_id=None,
        confirmation_basis=None,
    ):
        exact_text(request_id, 'a fixed consumer request identity')
        if type(lifecycle_epoch) is not int or lifecycle_epoch < 1:
            raise ValueError('the original subject lifecycle is required')
        if replace_report_contribution_id is not None:
            exact_text(replace_report_contribution_id, 'fixed Report replacement contribution')
            if report_projection_ref is None:
                raise ValueError('a Report replacement needs a fixed projection')
        if confirmation_basis is not None and (
            confirmation_basis != ANALYSIS_PUBLIC_CONFIRMATION or report_projection_ref is not None
        ):
            raise ValueError('default confirmation is limited to direct current Analysis publication')
        bundle = await self.admission._prepared(scope, prepared)
        material = bundle.material
        command = {
            'requestId': request_id,
            'sourceId': material.source_id,
            'sourceRevision': material.source_revision,
            'lifecycleEpoch': lifecycle_epoch,
            'prepared': prepared.as_dict(),
            'reportProjectionRef': report_projection_ref,
            'replaceReportContributionId': replace_report_contribution_id,
        }
        if confirmation_basis is not None:
            command['confirmationBasis'] = confirmation_basis
        key = digest([material.source_id, request_id])
        report = None
        if report_projection_ref is not None:
            report = await self._report(scope, bundle, report_projection_ref, lifecycle_epoch)
        try:
            await self.records.put('current_consumer_command', scope, key, command)
        except DocumentCompleteSourceUnavailable as error:
            raise PublicationConflictError('the consumer command changed its fixed inputs or lifecycle') from error
        if (saved := await self.records.get('current_consumer_result', scope, key)) is not None:
            return saved
        result = await self._consume(scope, bundle, prepared, command, key, report)
        try:
            await self.records.put('current_consumer_result', scope, key, result)
        except DocumentCompleteSourceUnavailable:
            saved = await self.records.require('current_consumer_result', scope, key)
            if self._result_identity(saved) != self._result_identity(result):
                raise
            return saved
        return result

    async def _report(self, scope, bundle, ref, epoch):
        exact_text(ref, 'fixed Report projection')
        projection = await self.source._report_projection_at(scope, ref, lifecycle_epoch=epoch, memo={})
        primary = projection.payload['dependencies'][0]
        if (
            projection.source_id != bundle.material.source_id
            or projection.source_revision != bundle.material.source_revision
            or primary['materialSnapshotRef'] != bundle.material.snapshot_id
            or primary['factsSnapshotRef'] != bundle.facts.snapshot_id
            or primary['understandingSnapshotRef'] != bundle.understanding.snapshot_id
        ):
            raise PublicationConflictError('the Report projection uses a different fixed primary preparation')
        return projection

    @staticmethod
    def _result_identity(value):
        publication = value['publication']
        return (
            {key: entry for key, entry in value.items() if key != 'publication'},
            None if publication is None else (publication['content_digest'], publication['receipt_ref']),
        )

    @staticmethod
    def _result(command, status, snapshot=None, revision=None, receipt=None):
        return {
            'status': status,
            'sourceId': command['sourceId'],
            'sourceRevision': command['sourceRevision'] if snapshot else None,
            'sourceSnapshotRef': snapshot,
            'publicationRevision': revision,
            'publication': receipt,
        }

    @staticmethod
    def _check_binding(row, command):
        binding = (row or {}).get('currentSourceBinding')
        if binding is None:
            return None
        if (
            binding['sourceId'] != command['sourceId']
            or binding['sourceRevision'] != command['sourceRevision']
            or binding['lifecycleEpoch'] != command['lifecycleEpoch']
        ):
            raise PublicationConflictError('the consumer input differs from the original current source lifecycle')
        return binding

    @staticmethod
    def _current_event(row):
        intent = (row or {}).get('currentIntent') or {}
        event = intent.get('event')
        if (
            not event
            or event['eventKind'] == 'retract'
            or row.get('reservedRetractionRevision')
            or event['snapshotRef']['contractVersion'] != COMPLETE_SOURCE_CONTRACT_V2
        ):
            raise PublicationConflictError('the source has no current publishable predecessor')
        return event

    async def _prepare_analysis_occurrence(self, scope, prepared, command, report):
        if command.get('confirmationBasis') or self.evidence_projections is None:
            return None
        if report is not None and len(report.payload['dependencies']) == 1:
            manifest = report.payload['manifest']
            if (
                manifest['interpretationProjectionRevision']
                == self.evidence_projections.interpretation_projection_revision
            ):
                return report.snapshot_id
        fixed = await self.evidence_projections.prepare(
            scope=scope,
            inputs=(prepared,),
            request_id='consume_original_'
            + digest(
                [
                    prepared.as_dict(),
                    command['lifecycleEpoch'],
                    self.evidence_projections.interpretation_projection_revision,
                ]
            ),
            lifecycle_epoch=command['lifecycleEpoch'],
        )
        return fixed.snapshot_id

    async def _consume(self, scope, bundle, prepared, command, key, report):
        source_id = command['sourceId']
        facts = bundle.facts.payload
        row = await self.ledger.read_source(scope, source_id)
        binding = self._check_binding(row, command)
        if binding is None:
            if command['replaceReportContributionId'] is not None:
                raise PublicationConflictError('a Report replacement requires an existing active source')
            report_has_observations = report is not None and bool(self.source._report_observations(report, memo=None))
            if not (has_clinical_delivery(facts) or facts['basicInfo'] or report_has_observations):
                return self._result(command, 'no_contribution')
            occurrence_ref = None
            if has_clinical_delivery(facts):
                occurrence_ref = await self._prepare_analysis_occurrence(scope, prepared, command, report)
            try:
                await self.admission.prepare_source_plan(
                    scope=scope,
                    prepared=prepared,
                    request_id='consume_initial_' + key,
                    lifecycle_epoch=command['lifecycleEpoch'],
                    report_projection_ref=command['reportProjectionRef'],
                    occurrence_projection_ref=occurrence_ref,
                    confirmation_basis=command.get('confirmationBasis'),
                )
            except PublicationConflictError:
                row = await self.ledger.read_source(scope, source_id)
                if self._check_binding(row, command) is None:
                    raise
            row = await self.ledger.read_source(scope, source_id)
            binding = self._check_binding(row, command)
        if binding is None:
            raise PublicationConflictError('the current admission has not been durably bound')
        if command['replaceReportContributionId'] is not None and bundle.facts.payload['interpretationRound'] > row[
            'acceptedInterpretationRounds'
        ].get(bundle.material.payload['documentId'], 0):
            raise PublicationConflictError(
                'apply the new primary round before explicitly replacing a Report contribution'
            )
        if binding['requestId'] == 'consume_initial_' + key or row['publicationRevision'] == 0:
            capture = await self.source.capture_source(scope=scope, verified_plan_ref=binding['planRef'])
            try:
                receipt = await self.publisher.publish(
                    scope=scope,
                    snapshot_id=capture.source_snapshot_ref,
                    request_id=binding['requestId'],
                    expected_prior_revision=None,
                )
            except PublicationRecoveryError as error:
                raise DocumentBusyError('the initial consumer publication is awaiting delivery') from error
            if binding['requestId'] == 'consume_initial_' + key:
                return self._result(
                    command, 'published', capture.source_snapshot_ref, receipt.publication_revision, asdict(receipt)
                )
            row = await self.ledger.read_source(scope, source_id)
        existing = await self._existing(scope, bundle, prepared, command, key, row, report)
        return self._result(
            command,
            existing['status'],
            existing['sourceSnapshotRef'],
            existing['publicationRevision'],
            existing['publication'],
        )

    async def _existing(self, scope, bundle, prepared, command, key, row, report):
        epoch = command['lifecycleEpoch']
        source_id = command['sourceId']
        if report is None:
            reuse = await self.records.get('current_consumer_reuse', scope, key)
            if reuse is not None:
                return await self._reuse_publication(scope, source_id, reuse)
        # A held earlier Report step resumes its own original managed command,
        # even when another request has since advanced or withdrawn the source.
        if report is not None:
            accepted = await self._read_step(scope, key, 'report', row, accepted_only=True)
            if accepted is not None:
                return await self._execute_managed(accepted)
        held_analysis = await self._read_step(scope, key, 'analysis', row)
        if report is None and held_analysis is not None:
            return await self._execute_managed(held_analysis)
        working = self._current_event(row)
        input_round = bundle.facts.payload['interpretationRound']
        doc_id = bundle.material.payload['documentId']
        current_round = row['acceptedInterpretationRounds'].get(doc_id, 0)
        if held_analysis is not None or input_round > current_round:
            occurrence_ref = None
            if held_analysis is None and has_clinical_delivery(bundle.facts.payload):
                occurrence_ref = await self._prepare_analysis_occurrence(scope, prepared, command, report)
            result = await self._managed_step(
                scope,
                key,
                'analysis',
                source_id,
                epoch,
                row,
                understanding_snapshot_ref=bundle.understanding.snapshot_id,
                occurrence_projection_ref=occurrence_ref,
                confirmation_basis=command.get('confirmationBasis'),
            )
            if report is None:
                return result
            row = await self.ledger.read_source(scope, source_id)
            self._check_binding(row, command)
            self._current_event(row)
            current_round = row['acceptedInterpretationRounds'].get(doc_id, 0)
        if report is not None:
            if input_round != current_round:
                raise PublicationConflictError('the primary interpretation round differs from the accepted input')
            return await self._managed_step(
                scope,
                key,
                'report',
                source_id,
                epoch,
                row,
                report_projection_ref=report.snapshot_id,
                replace_report_contribution_id=command['replaceReportContributionId'],
            )
        if input_round != current_round:
            raise PublicationConflictError('consumer input is from an earlier accepted interpretation round')
        await self.source.read_source_observations(scope=scope, snapshot_id=working['snapshotRef']['snapshotId'])
        try:
            await self.records.put('current_consumer_reuse', scope, key, working)
        except DocumentCompleteSourceUnavailable:
            working = await self.records.require('current_consumer_reuse', scope, key)
        return await self._reuse_publication(scope, source_id, working)

    async def _reuse_publication(self, scope, source_id, working):
        intent = await self.ledger.read_intent(scope, working['eventId'])
        if intent is None:
            raise DocumentBusyError('current consumer publication outbox is not yet available')
        if intent['event'] != working or working['sourceId'] != source_id or working['scope'] != scope.as_dict():
            raise PublicationConflictError('current consumer publication outbox differs from the exact source')
        fixed = intent.get('receipt')
        try:
            receipt = (
                PublicationReceipt(**fixed)
                if fixed is not None
                else await self.publisher.recovery.deliver_intent(scope=scope, intent_id=working['eventId'])
            )
        except PublicationRecoveryError as error:
            raise DocumentBusyError('the current consumer publication is awaiting delivery') from error
        if (
            receipt.source_id != source_id
            or receipt.publication_revision != working['publicationRevision']
            or receipt.content_digest != working['contentDigest']
        ):
            raise PublicationConflictError('current consumer publication receipt differs from its source')
        identity = {
            'sourceSnapshotRef': working['snapshotRef']['snapshotId'],
            'publicationRevision': working['publicationRevision'],
            'status': 'unchanged',
            'publication': asdict(receipt),
        }
        return identity

    async def _read_step(self, scope, key, step, source, *, accepted_only=False):
        first = await self.records.get('current_consumer_step', scope, (key, step))
        if first is None:
            return None
        current = source['publicationRevision']
        command = DocumentCurrentManagedCommand.from_dict(first)
        start = command.prior_publication_revision
        # Once the source revision advances, an unaccepted older attempt
        # cannot win the predecessor CAS. Accepted attempts are still replayable.
        for revision in range(start, min(current, MAX_MANAGED_TRANSITIONS) + 1):
            if revision != start:
                saved = await self.records.get('current_consumer_step', scope, (key, step, revision))
                if saved is None:
                    continue
                command = DocumentCurrentManagedCommand.from_dict(saved)
            if (revision == current and not accepted_only) or await self.ledger.has_current_managed_acceptance(
                scope, command.source_id, command.command_id, source=source
            ):
                return command
        return None

    async def _managed_step(self, scope, key, step, source_id, epoch, row, **changes):
        if (held := await self._read_step(scope, key, step, row)) is not None:
            return await self._execute_managed(held)
        event = self._current_event(row)
        revision = event['publicationRevision']
        if row['appliedRevision'] != revision:
            raise DocumentBusyError('the current source must be applied before a new consumer correction')

        first_key = (key, step)
        identity = (*first_key, revision)
        candidate = DocumentCurrentManagedCommand(
            scope,
            source_id,
            'consume_' + digest(identity),
            epoch,
            revision,
            event['snapshotRef']['snapshotId'],
            **changes,
        ).as_dict()
        first = await self._hold_step(scope, first_key, candidate)
        if first['priorPublicationRevision'] > revision:
            raise DocumentBusyError('the current consumer predecessor advanced; retry the original request')
        stored = (
            first
            if first['priorPublicationRevision'] == revision
            else await self._hold_step(scope, identity, candidate)
        )
        return await self._execute_managed(DocumentCurrentManagedCommand.from_dict(stored))

    async def _hold_step(self, scope, identity, candidate):
        try:
            await self.records.put('current_consumer_step', scope, identity, candidate)
        except DocumentCompleteSourceUnavailable:
            return await self.records.require('current_consumer_step', scope, identity)
        return candidate

    async def _execute_managed(self, command):
        try:
            return await self.managed.execute(command)
        except PublicationRecoveryError as error:
            raise DocumentBusyError('the held consumer correction is awaiting delivery') from error
        except PublicationConflictError as error:
            source = await self.ledger.read_source(command.scope, command.source_id)
            if (
                source is not None
                and source['publicationRevision'] > command.prior_publication_revision
                and source['reservedRetractionRevision'] == 0
                and not await self.ledger.has_current_managed_acceptance(
                    command.scope, command.source_id, command.command_id, source=source
                )
            ):
                raise DocumentBusyError(
                    'the current consumer predecessor advanced; retry the original request'
                ) from error
            raise
