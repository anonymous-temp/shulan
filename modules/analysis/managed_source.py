"""Source-wide managed transitions, with exact prior CAS and immutable retirement audit."""

from dataclasses import asdict
from dataclasses import replace

from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import DocumentRetiredContribution
from modules.analysis.managed_source_contracts import MANAGED_REPORT_REVISION
from modules.analysis.managed_source_contracts import MAX_MANAGED_TRANSITIONS
from modules.analysis.managed_source_contracts import DocumentManagedSourceCommand
from modules.analysis.managed_source_contracts import DocumentManagedSourcePlan
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import digest


def _require(ok, message):
    if not ok:
        raise DocumentCompleteSourceUnavailable(message)


class DocumentManagedSourceService:
    def __init__(self, *, records, ledger, initial_admission, complete, reports, archive, publisher):
        self.records, self.ledger, self.initial_admission = records, ledger, initial_admission
        self.complete, self.reports, self.archive, self.publisher = complete, reports, archive, publisher

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

    async def _origin(self, scope, plan_ref):
        for _ in range(MAX_MANAGED_TRANSITIONS + 1):
            row = await self.records.get('managed_transition', scope, plan_ref)
            if row is None:
                plan = await self.initial_admission.read_verified_plan(scope=scope, plan_ref=plan_ref)
                _require(
                    plan.fence.get('admissionKind') == 'fresh', 'only an actually admitted fresh origin is supported'
                )
                _require(len(plan.active) == 1, 'the initial fresh base must be unambiguous')
                _, proof = await self.complete._handle(scope, plan.active[0])
                _require(proof.payload['kind'] == 'base', 'the original fixed base proof is required')
                return await self.complete._bundle(scope, proof.payload['understandingSnapshotRef'])
            plan_ref = row['priorPlanRef']
        raise DocumentCompleteSourceUnavailable('managed transition history exceeds the supported bound')

    async def _dependencies(self, scope, proof):
        value = proof.payload
        refs = value['materialSnapshotRefs']
        if value['kind'] in {'base', 'managed_base'}:
            understandings = {refs[0]['documentId']: value['understandingSnapshotRef']}
        elif value['kind'] == 'legacy_report_source':
            receipt = await self.complete.report_publications.read_publication(
                scope=scope, receipt_ref=value['publicationReceiptRef']
            )
            understandings = {
                x.preparation_receipt.document_id: x.understanding_snapshot_ref for x in receipt.manifest.inputs
            }
        else:
            understandings = {x['documentId']: x['snapshotRef'] for x in value['understandingSnapshotRefs']}
        return {x['documentId']: {'material': x, 'understanding': understandings[x['documentId']]} for x in refs}

    async def _derive(self, command, prior, handle, archive_revision, plan_ref):
        withdrawing = command.operation == 'retire_report'
        replacement, replacement_proof = await self.complete._handle(command.scope, handle)
        _require(replacement.source_id == command.source_id, 'the replacement belongs to another source')
        _require(
            replacement.source_revision == prior.source_revision, 'mixed material revisions need a supported contract'
        )
        old = [(h, *(await self.complete._handle(command.scope, h))) for h in prior.active]
        new_refs = await self._dependencies(command.scope, replacement_proof)
        targets, retained = [], []
        replacement_docs = list(new_refs)
        for old_handle, snapshot, proof in old:
            kind = snapshot.payload['kind']
            refs = await self._dependencies(command.scope, proof)
            if command.operation == 'replace_report' and kind == 'base':
                _require(
                    all(key not in new_refs or value == new_refs[key] for key, value in refs.items()),
                    'Report inputs differ from the current active base interpretation',
                )
            target = (
                kind == 'base'
                if command.operation == 'replace_base'
                else (kind == 'legacy_report_source' and list(refs) == replacement_docs)
            )
            invalidated = (
                command.operation in {'replace_base', 'replace_report'}
                and kind == 'legacy_report_source'
                and any(key in new_refs and new_refs[key] != value for key, value in refs.items())
            )
            if target or invalidated:
                reason = 'explicit_withdrawal' if withdrawing else ('replaced' if target else 'fixed_input_superseded')
                targets.append((old_handle, snapshot, proof, reason))
            else:
                retained.append(old_handle)
        _require(
            command.operation != 'replace_base' or any(x[1].payload['kind'] == 'base' for x in targets),
            'active base is missing',
        )
        _require(
            not withdrawing or len(targets) == 1 and targets[0][0] == handle,
            'the exact active Report contribution must be selected for retirement',
        )
        # Equal fixed content is a true no-op, even if the new HTTP preparation
        # has a different request-local trace or this command has another ID.
        for _, snapshot, proof, reason in targets:
            old_payload = {
                k: v for k, v in snapshot.payload.items() if k not in {'contributionId', 'legacySourceRevision'}
            }
            new_payload = {
                k: v for k, v in replacement.payload.items() if k not in {'contributionId', 'legacySourceRevision'}
            }
            if (
                reason == 'replaced'
                and old_payload == new_payload
                and await self._dependencies(command.scope, proof) == new_refs
            ):
                return prior, [], True
        # Retain original order; the new base remains first. Report slots replace
        # their first matching slot or append when that set of inputs is new.
        active, inserted = [], False
        removed = {x[0] for x in targets}
        for old_handle in prior.active:
            if old_handle in removed:
                if not inserted and not withdrawing:
                    active.append(handle)
                    inserted = True
            else:
                active.append(old_handle)
        if not inserted and not withdrawing:
            active.append(handle)
        retirements, retired = [], list(prior.retired)
        replacement_id = replacement.payload['contributionId']
        for old_handle, snapshot, proof, reason in targets:
            evidence = {
                'commandId': command.command_id,
                'priorPublicationRevision': command.prior_publication_revision,
                'priorSourceSnapshotRef': command.prior_source_snapshot_ref,
                'retiredHandle': asdict(old_handle),
                'replacementHandle': None if withdrawing else asdict(handle),
                'retirementKind': 'retracted' if withdrawing else 'corrected',
                'reason': reason,
                'oldMaterialRefs': proof.payload['materialSnapshotRefs'],
                'newMaterialRefs': [] if withdrawing else replacement_proof.payload['materialSnapshotRefs'],
                'oldInputBindings': await self._dependencies(command.scope, proof),
                'newInputBindings': {} if withdrawing else new_refs,
            }
            ref = 'document_retirement_' + digest(evidence)
            retirements.append((ref, evidence))
            retired.append(
                DocumentRetiredContribution(
                    old_handle,
                    'retracted' if withdrawing else 'corrected',
                    ref,
                    replacement_id if reason == 'replaced' else None,
                )
            )
        active_ids = {(await self.complete._handle(command.scope, h))[0].payload['contributionId'] for h in active}
        # A→B remains proven by its immutable evidence after B→C. The optional
        # inventory pointer only names a currently active successor; do not invent A→C.
        retired = [
            replace(row, superseded_by_contribution_id=None)
            if row.superseded_by_contribution_id not in active_ids
            else row
            for row in retired
        ]
        return (
            DocumentManagedSourcePlan(
                command.scope,
                command.source_id,
                prior.source_revision,
                tuple(active),
                tuple(retired),
                'captured',
                archive_revision,
                prior.fence_evidence_ref,
                plan_ref,
                prior._fence_json,
                plan_ref,
            ),
            retirements,
            False,
        )

    async def execute(self, command):
        if not isinstance(command, DocumentManagedSourceCommand):
            raise TypeError('an explicit versioned managed source command is required')
        scope, key = command.scope, command.command_id
        existing = await self.records.get('managed_command', scope, key)
        if existing is not None and existing != command.as_dict():
            raise PublicationConflictError('managed request id was reused with changed input')
        saved = await self.records.get('managed_result', scope, key)
        if saved is not None:
            return await self.read_receipt(scope=scope, command_id=key)
        transition = await self.records.get('managed_transition', scope, key)
        if transition is None:
            prior_event, prior_ref, prior_receipt = await self._prior(command)
            prior = await self.read_verified_plan(scope=scope, plan_ref=prior_ref)
            bundles = await self.reports.inputs(scope=scope, inputs=command.inputs)
            _require(bundles[0].material.source_id == command.source_id, 'the first fixed input must own this source')
            if command.operation == 'replace_report':
                for handle in prior.active:
                    base, proof = await self.complete._handle(scope, handle)
                    if base.payload['kind'] == 'base':
                        dependencies = await self._dependencies(scope, proof)
                        for fixed in command.inputs:
                            doc_id = fixed.preparation_receipt.document_id
                            if doc_id in dependencies:
                                _require(
                                    dependencies[doc_id]['understanding'] == fixed.understanding_snapshot_ref,
                                    'Report inputs differ from the current active base interpretation',
                                )
            # Validate base quality before either compatibility archive or source writes.
            candidate = await self.complete.capture_managed_base_contribution(
                scope=scope, understanding_snapshot_id=command.inputs[0].understanding_snapshot_ref, transition_ref=key
            )
            projection = None
            if command.operation == 'replace_report':
                projection = await self.reports.prepare(
                    scope=scope, inputs=command.inputs, revision=MANAGED_REPORT_REVISION
                )
                _require(
                    projection.payload['processingStatus'] == 'complete',
                    'incomplete preparation remains available but cannot replace a source',
                )
            await self.records.put('managed_command', scope, key, command.as_dict())
            if command.operation == 'retire_report':
                expected = {x.preparation_receipt.document_id: x.understanding_snapshot_ref for x in command.inputs}
                matches = []
                for handle in prior.active:
                    snapshot, proof = await self.complete._handle(scope, handle)
                    refs = await self._dependencies(scope, proof)
                    if (
                        snapshot.payload['kind'] == 'legacy_report_source'
                        and {key: value['understanding'] for key, value in refs.items()} == expected
                        and list(refs) == list(expected)
                    ):
                        matches.append(handle)
                _require(len(matches) == 1, 'retirement requires one exact current Report input slot')
                candidate = matches[0]
                archive_ref = base_archive_ref = None
                archive_revision = prior.legacy_source_revision
            else:
                origin = await self._origin(scope, prior_ref)
                base_archive_ref, base_archive = await self.archive.base(scope=scope, origin=origin, current=bundles[0])
                archive_ref, archived = base_archive_ref, base_archive
                if projection is not None:
                    archive_ref, archived = await self.reports.archive_prepared(
                        scope=scope,
                        projection_ref=projection.snapshot_id,
                        primary_bundle=bundles[0],
                        transition_ref=key,
                    )
                    candidate = await self.complete.capture_managed_report_contribution(
                        scope=scope, archive_ref=archive_ref
                    )
                archive_revision = archived['receipt']['legacySourceRevision']
            plan, retirements, no_change = await self._derive(command, prior, candidate, archive_revision, key)
            transition = {
                'command': command.as_dict(),
                'priorEvent': prior_event,
                'priorAppliedReceipt': prior_receipt,
                'priorPlanRef': prior_ref,
                'replacement': asdict(candidate),
                'archiveRef': archive_ref,
                'baseArchiveRef': base_archive_ref,
                'legacySourceRevision': archive_revision,
                'projectionRef': projection.snapshot_id if projection else None,
                'plan': asdict(plan),
                'noChange': no_change,
            }
            for ref, evidence in retirements:
                await self.records.put('managed_retirement', scope, ref, evidence)
            await self.records.put('managed_transition', scope, key, transition)
        plan = await self.read_verified_plan(scope=scope, plan_ref=key)
        if transition['noChange']:
            # An old no-op cannot confirm a source has remained active after a
            # concurrent correction/retraction; freeze its result only after CAS-equivalent recheck.
            await self._prior(command)
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
            projectionRef=transition['projectionRef'],
            archiveRef=transition['archiveRef'],
            baseArchiveRef=transition['baseArchiveRef'],
            legacySourceRevision=transition['legacySourceRevision'],
        )
        await self.records.put('managed_result', scope, key, result)
        return await self.read_receipt(scope=scope, command_id=key)

    async def read_verified_plan(self, *, scope, plan_ref):
        chain, current = [], plan_ref
        for _ in range(MAX_MANAGED_TRANSITIONS):
            row = await self.records.get('managed_transition', scope, current)
            if row is None:
                plan = await self.initial_admission.read_verified_plan(scope=scope, plan_ref=current)
                break
            if current in {ref for ref, _ in chain}:
                raise DocumentCompleteSourceUnavailable('managed transition cycle')
            chain.append((current, row))
            current = row['priorPlanRef']
        else:
            raise DocumentCompleteSourceUnavailable('managed transition history exceeds the supported bound')
        for ref, row in reversed(chain):
            command = DocumentManagedSourceCommand.from_dict(row['command'])
            _require(
                command.scope == scope and command.command_id == ref, 'managed transition command identity differs'
            )
            _require(
                await self.records.require('managed_command', scope, ref) == row['command'], 'managed manifest differs'
            )
            event = row['priorEvent']
            intent = await self.ledger.read_intent(scope, event['eventId'])
            _require(intent is not None and intent['event'] == event, 'the actual prior event proof is unavailable')
            acknowledged = PublicationReceipt(**row['priorAppliedReceipt'])
            _require(
                acknowledged.state == 'applied'
                and acknowledged.application_state == 'applied'
                and acknowledged.publication_revision == event['publicationRevision']
                and acknowledged.source_id == event['sourceId']
                and acknowledged.content_digest == event['contentDigest'],
                'the transition lacks a fixed applied prior acknowledgement',
            )
            _require(
                event['publicationRevision'] == command.prior_publication_revision
                and event['sourceId'] == command.source_id
                and event['snapshotRef']['contractVersion'] == COMPLETE_SOURCE_CONTRACT
                and event['snapshotRef']['snapshotId'] == command.prior_source_snapshot_ref,
                'prior event binding differs',
            )
            previous = await self.complete.snapshots.get(scope, command.prior_source_snapshot_ref)
            _require(previous.content_digest == event['contentDigest'], 'prior source digest differs')
            inventory = await self.complete.snapshots.get(
                scope, previous.payload['coverage']['inventorySnapshotRef']['snapshotId']
            )
            _require(inventory.payload['activeSetEvidenceRef'] == row['priorPlanRef'], 'prior inventory plan differs')
            replacement = DocumentContributionHandle(**row['replacement'])
            plan, retirements, no_change = await self._derive(
                command, plan, replacement, row['legacySourceRevision'], ref
            )
            _require(
                digest(asdict(plan)) == digest(row['plan']) and no_change == row['noChange'],
                'managed effective set differs',
            )
            for evidence_ref, expected in retirements:
                _require(
                    await self.records.require('managed_retirement', scope, evidence_ref) == expected,
                    'retirement evidence differs',
                )
            if command.operation == 'retire_report':
                _require(
                    row['archiveRef'] is None and row['baseArchiveRef'] is None,
                    'retirement must not archive new material',
                )
                continue
            base = await self.archive.read(scope=scope, archive_ref=row['baseArchiveRef'])
            if command.operation == 'replace_base':
                _require(
                    base['receipt']['legacySourceRevision'] == row['legacySourceRevision'],
                    'base archive binding differs',
                )
            else:
                snapshot, _ = await self.reports.build_contribution(scope=scope, archive_ref=row['archiveRef'])
                _require(snapshot.snapshot_id == replacement.snapshot_ref, 'Report archive binding differs')
        return plan

    async def read_receipt(self, *, scope, command_id):
        result = await self.records.require('managed_result', scope, command_id)
        command = DocumentManagedSourceCommand.from_dict(
            await self.records.require('managed_command', scope, command_id)
        )
        _require(command.scope == scope and command.command_id == command_id, 'managed receipt scope differs')
        await self.read_verified_plan(scope=scope, plan_ref=command_id)
        transition = await self.records.require('managed_transition', scope, command_id)
        _require(
            result['commandId'] == command_id
            and result['commandRevision'] == command.command_revision
            and result['scope'] == scope.as_dict()
            and result['sourceId'] == command.source_id
            and all(
                result[key] == transition[key]
                for key in ('projectionRef', 'archiveRef', 'baseArchiveRef', 'legacySourceRevision')
            ),
            'managed receipt command differs',
        )
        if transition['noChange']:
            _require(
                result['status'] == 'unchanged'
                and result['publication'] is None
                and result['sourceSnapshotRef'] == command.prior_source_snapshot_ref
                and result['publicationRevision'] == command.prior_publication_revision,
                'no-op receipt differs',
            )
        else:
            publication = PublicationReceipt(**result['publication'])
            snapshot = await self.complete.snapshots.get(scope, result['sourceSnapshotRef'])
            _require(
                result['status'] == 'published'
                and publication.publication_revision == command.prior_publication_revision + 1
                and result['publicationRevision'] == publication.publication_revision
                and publication.source_id == command.source_id
                and publication.content_digest == snapshot.content_digest,
                'managed publication receipt differs',
            )
        return result
