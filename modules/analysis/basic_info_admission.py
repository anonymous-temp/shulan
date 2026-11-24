"""Current source admission: immutable input plan and the existing publication CAS."""

from dataclasses import asdict

from modules.analysis.basic_info_contracts import ANALYSIS_PUBLIC_CONFIRMATION
from modules.analysis.basic_info_contracts import DocumentCurrentSourcePlan
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import DocumentContributionHandle
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.current_understanding import DocumentPreparedInput
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.snapshots import digest

PLAN_REVISION = 'document-current-source-plan'


def _require(value, message):
    if not value:
        raise DocumentCompleteSourceUnavailable(message)


class DocumentCurrentAdmission:
    def __init__(self, *, prepared, contributions, ledger, records):
        self.prepared, self.contributions, self.ledger, self.records = prepared, contributions, ledger, records

    async def _prepared(self, scope, prepared, *, memo=None):
        _require(
            isinstance(scope, DocumentScope) and isinstance(prepared, DocumentPreparedInput),
            'the exact scope and actual preparation DTO are required',
        )
        if memo is None:
            bundle = await self.prepared.validate_prepared(scope=scope, prepared=prepared)
        else:
            bundle = await self.contributions._bundle_at(scope, prepared.understanding_snapshot_ref, memo=memo)
            await self.prepared._validate_prepared_bundle(scope, prepared, bundle)
        _require(
            bundle.material.snapshot_id == prepared.material_snapshot_ref
            and bundle.facts.snapshot_id == prepared.facts_snapshot_ref,
            'fixed preparation refs differ',
        )
        return bundle

    async def prepare_source_plan(
        self,
        *,
        scope,
        prepared,
        request_id,
        lifecycle_epoch,
        report_projection_ref=None,
        occurrence_projection_ref=None,
        confirmation_basis=None,
    ):
        _require(
            isinstance(request_id, str) and request_id and request_id == request_id.strip(),
            'a fixed command identity is required',
        )
        _require(type(lifecycle_epoch) is int and lifecycle_epoch > 0, 'the original subject lifecycle is required')
        bundle = await self._prepared(scope, prepared)
        manifest = {'scope': scope.as_dict(), 'prepared': prepared.as_dict(), 'lifecycleEpoch': lifecycle_epoch}
        if report_projection_ref is not None:
            exact_text(report_projection_ref, 'fixed Report projection reference')
            manifest['reportProjectionRef'] = report_projection_ref
        if occurrence_projection_ref is not None:
            exact_text(occurrence_projection_ref, 'fixed Analysis occurrence projection')
            manifest['occurrenceProjectionRef'] = occurrence_projection_ref
        if confirmation_basis is not None:
            _require(
                confirmation_basis == ANALYSIS_PUBLIC_CONFIRMATION
                and occurrence_projection_ref is None
                and report_projection_ref is None,
                'default confirmation is limited to direct current Analysis publication',
            )
            manifest['confirmationBasis'] = confirmation_basis
        manifest_ref = 'document_prepared_manifest_' + digest(manifest)
        request_ref = 'document_current_request_' + digest([scope.as_dict(), bundle.material.source_id, request_id])
        saved = await self.records.get('current_request', scope, request_ref)
        if saved is not None:
            _require(saved['preparedManifestRef'] == manifest_ref, 'the command changed its fixed inputs or lifecycle')
            await self.read_verified_plan(scope=scope, plan_ref=saved['planRef'])
            return saved['planRef']
        handles = await self.contributions.capture_contributions(
            scope=scope,
            understanding_snapshot_id=prepared.understanding_snapshot_ref,
            occurrence_projection_ref=occurrence_projection_ref,
            confirmation_basis=confirmation_basis,
            epoch=lifecycle_epoch if occurrence_projection_ref or confirmation_basis else None,
        )
        rounds = {bundle.material.payload['documentId']: bundle.facts.payload['interpretationRound']}
        if report_projection_ref is not None:
            report = await self.contributions.capture_report_contribution(
                scope=scope,
                projection_snapshot_ref=report_projection_ref,
                primary_bundle=bundle,
                lifecycle_epoch=lifecycle_epoch,
            )
            if report is not None:
                handles = (*handles, report)
                rounds.update(
                    await self.contributions.report_input_rounds(
                        scope=scope,
                        projection_snapshot_ref=report_projection_ref,
                        lifecycle_epoch=lifecycle_epoch,
                    )
                )
        _require(bool(handles), 'no admissible contribution; retain the archive for recovery')
        inputs = {
            'revision': PLAN_REVISION,
            'preparedManifestRef': manifest_ref,
            'requestId': request_id,
            'active': [asdict(handle) for handle in handles],
            'lifecycleEpoch': lifecycle_epoch,
            'interpretationRounds': rounds,
        }
        source_id = bundle.material.source_id
        plan_ref = 'document_source_plan_' + digest([scope.as_dict(), source_id, inputs])
        plan = DocumentCurrentSourcePlan(
            scope,
            source_id,
            bundle.material.source_revision,
            handles,
            (),
            plan_ref,
            plan_ref,
            canonical_json({'admissionKind': 'current', 'evidenceRef': plan_ref}),
            plan_ref,
            lifecycle_epoch,
        )
        await self.contributions.verify_complete_plan(scope, plan_ref, plan)
        await self.records.put('current_prepared_manifest', scope, manifest_ref, manifest)
        await self.records.put('current_verified_plan', scope, plan_ref, {'input': inputs, 'plan': asdict(plan)})
        await self.ledger.admit_current_source(
            scope,
            source_id,
            binding=self._binding(plan, manifest_ref, request_id),
            interpretation_rounds=rounds,
        )
        await self.records.put(
            'current_request', scope, request_ref, {'preparedManifestRef': manifest_ref, 'planRef': plan_ref}
        )
        return plan_ref

    @staticmethod
    def _binding(plan, manifest_ref, request_id):
        return {
            'scope': plan.scope.as_dict(),
            'sourceId': plan.source_id,
            'sourceRevision': plan.source_revision,
            'lifecycleEpoch': plan.lifecycle_epoch,
            'preparedManifestRef': manifest_ref,
            'planRef': plan.active_set_evidence_ref,
            'requestId': request_id,
        }

    async def read_verified_plan(self, *, scope, plan_ref, memo=None):
        stored = await self.records.require('current_verified_plan', scope, plan_ref)
        raw, inputs = stored['plan'], stored['input']
        plan = DocumentCurrentSourcePlan(
            **{
                **raw,
                'scope': DocumentScope(**raw['scope']),
                'active': tuple(DocumentContributionHandle(**row) for row in raw['active']),
                'retired': (),
            }
        )
        _require(
            inputs['revision'] == PLAN_REVISION
            and not raw['retired']
            and bool(plan.active)
            and plan.scope == scope
            and plan.active_set_evidence_ref == plan_ref
            and plan.lifecycle_epoch == inputs['lifecycleEpoch']
            and plan_ref == 'document_source_plan_' + digest([scope.as_dict(), plan.source_id, inputs]),
            'the fixed current plan identity differs',
        )
        manifest_ref = inputs['preparedManifestRef']
        manifest = await self.records.require('current_prepared_manifest', scope, manifest_ref)
        _require(
            manifest_ref == 'document_prepared_manifest_' + digest(manifest)
            and manifest['scope'] == scope.as_dict()
            and manifest['lifecycleEpoch'] == plan.lifecycle_epoch,
            'the original preparation manifest differs',
        )
        prepared = DocumentPreparedInput.from_dict(manifest['prepared'])
        bundle = await self._prepared(scope, prepared, memo=memo)
        expected = tuple(
            DocumentContributionHandle(snapshot.snapshot_id, capture.snapshot_id)
            for snapshot, capture in await self.contributions._contributions(
                scope,
                prepared.understanding_snapshot_ref,
                occurrence_projection_ref=manifest.get('occurrenceProjectionRef'),
                confirmation_basis=manifest.get('confirmationBasis'),
                epoch=plan.lifecycle_epoch
                if manifest.get('occurrenceProjectionRef') or manifest.get('confirmationBasis')
                else None,
                memo=memo,
            )
        )
        rounds = {bundle.material.payload['documentId']: bundle.facts.payload['interpretationRound']}
        if report_ref := manifest.get('reportProjectionRef'):
            pieces = await self.contributions._report_contribution(
                scope,
                report_ref,
                lifecycle_epoch=plan.lifecycle_epoch,
                primary_bundle=bundle,
                memo=memo,
            )
            if pieces is not None:
                snapshot, capture = pieces
                expected = (*expected, DocumentContributionHandle(snapshot.snapshot_id, capture.snapshot_id))
                rounds.update(
                    await self.contributions.report_input_rounds(
                        scope=scope,
                        projection_snapshot_ref=report_ref,
                        lifecycle_epoch=plan.lifecycle_epoch,
                        memo=memo,
                    )
                )
        _require(
            inputs['interpretationRounds'] == rounds
            and plan.active == expected
            and plan.active == tuple(DocumentContributionHandle(**row) for row in inputs['active']),
            'the complete inventory differs from its fixed extraction',
        )
        source = await self.ledger.read_source(scope, plan.source_id)
        _require(
            source is not None
            and source.get('currentSourceBinding') == self._binding(plan, manifest_ref, inputs['requestId'])
            and source.get('initialInterpretationRoundsDigest') == digest(rounds),
            'the durable source admission differs',
        )
        return plan
