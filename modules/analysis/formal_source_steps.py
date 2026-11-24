"""Versioned Document-owned routing; consumers never coordinate Profile internals."""

from copy import deepcopy
from dataclasses import asdict

from modules.analysis.formal_source import DocumentFormalSourceWait
from modules.analysis.formal_source_contracts import FORMAL_REPORT_REUSE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.managed_archive import archive_result
from modules.analysis.managed_source_contracts import MANAGED_REPORT_REVISION
from modules.analysis.managed_source_contracts import MANAGED_SOURCE_REVISION
from modules.analysis.managed_source_contracts import DocumentManagedSourceCommand
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.understanding import UNDERSTANDING_SELECTION


class DocumentFormalSourceSteps:
    def __init__(self, *, compatibility, admission, managed, profile_admission, legacy_reports):
        self.compatibility, self.admission, self.managed = compatibility, admission, managed
        self.profile_admission, self.legacy_reports = profile_admission, legacy_reports

    async def _bundle(self, scope, item):
        bundle = await self.compatibility.validate_prepared(
            scope=scope,
            understanding_snapshot_id=item.understanding_snapshot_ref,
            selection=item.selection,
            preparation_receipt=item.preparation_receipt,
        )
        if (bundle.material.snapshot_id, bundle.facts.snapshot_id, bundle.understanding.snapshot_id) != (
            item.material_snapshot_ref,
            item.facts_snapshot_ref,
            item.understanding_snapshot_ref,
        ):
            raise DocumentFormalSourceConflict('formal input selects inconsistent fixed Document references')
        return bundle

    async def prepare_report(self, *, scope, inputs):
        # A business projection is independent of whether Profile is available or
        # a previously published base is already applied. Preparation never publishes.
        return await self.managed.reports.prepare(scope=scope, inputs=inputs, revision=MANAGED_REPORT_REVISION)

    async def read_report_projection(self, *, scope, projection_ref):
        return await self.managed.reports.read(scope=scope, snapshot_id=projection_ref)

    async def _active_base(self, scope, lifecycle):
        active = await self.managed.lifecycle.read_active_material(
            scope=scope, source_id=lifecycle.source_id, expected=lifecycle
        )
        inventory = await self.managed.source_reader.read_source_inventory(
            scope=scope, snapshot_id=active.payload['coverage']['inventorySnapshotRef']['snapshotId']
        )
        plan = await self.managed.commands.read_verified_plan(
            scope=scope, plan_ref=inventory['payload']['activeSetEvidenceRef']
        )
        for handle in plan.active:
            contribution, proof = await self.managed.source_reader._handle(scope, handle)
            if contribution.payload['kind'] == 'base':
                return await self.compatibility.read_bundle(
                    scope=scope,
                    understanding_snapshot_id=proof.payload['understandingSnapshotRef'],
                    selection=UNDERSTANDING_SELECTION,
                )
        raise DocumentFormalSourceConflict('the active source has no proven base interpretation')

    async def _older_round(self, command, source, item, lifecycle, active):
        bundle = await self._bundle(command.scope, item)
        if (
            bundle.material.payload['documentId'] != active.material.payload['documentId']
            or bundle.understanding.payload['interpretationRound']
            >= active.understanding.payload['interpretationRound']
        ):
            return None
        reference, archived = await self.managed.archive.save(
            scope=command.scope,
            bundle=bundle,
            result=archive_result(bundle),
            source_key='document_fixed_round:' + bundle.understanding.snapshot_id,
        )
        if await self.managed.lifecycle.read(scope=command.scope, source_id=lifecycle.source_id) != lifecycle:
            raise DocumentFormalSourceWait()
        source.update(
            phase='done',
            result={
                'kind': 'archive',
                'sourceId': lifecycle.source_id,
                'canonicalReportId': archived['receipt']['canonicalReportId'],
                'legacySourceRevision': archived['receipt']['legacySourceRevision'],
                'ingestionStatus': 'not_submitted',
                'jobId': None,
                'archiveRef': reference,
                'publication': None,
                'publicationRevision': lifecycle.publication_revision,
                'sourceSnapshotRef': lifecycle.active_snapshot_ref,
                'historicalOnly': True,
            },
        )
        return source

    async def step(self, command, row):
        progress = deepcopy(row['progress'])
        if row['phase'] == 'prepare':
            sources, seen = [], {}
            for item in command.inputs:
                bundle = await self._bundle(command.scope, item)
                source_id = bundle.material.source_id
                content = (item.material_snapshot_ref, item.facts_snapshot_ref, item.understanding_snapshot_ref)
                if source_id in seen and seen[source_id] != content:
                    raise DocumentFormalSourceConflict('one source has conflicting accepted inputs')
                if source_id not in seen:
                    sources.append(
                        {
                            'sourceId': source_id,
                            'input': item.as_dict(),
                            'phase': 'route',
                            'quality': bundle.understanding.payload.get('quality', {}).get('status'),
                            'inputVersionStatus': bundle.understanding.payload.get('inputVersionStatus'),
                            'interpretationRevision': bundle.understanding.payload.get('interpretationRevision'),
                        }
                    )
                    seen[source_id] = content
            progress = {'sources': sources}
            if command.operation == 'report':
                output = await self.prepare_report(scope=command.scope, inputs=command.inputs)
                progress['projectionRef'] = output.snapshot_id
                progress['processingStatus'] = output.payload['processingStatus']
            return {'phase': 'sources', 'progress': progress}
        if row['phase'] == 'sources':
            for index, source in enumerate(progress['sources']):
                if source['phase'] == 'done':
                    continue
                progress['sources'][index] = await self._source_step(command, row, source, index)
                return {'progress': progress}
            return {'phase': 'report' if command.operation == 'report' else 'finish'}
        if row['phase'] == 'report':
            return await self._report_step(command, progress)
        if row['phase'] == 'finish':
            rejected = (
                any(item.get('result', {}).get('kind') == 'not_published' for item in progress['sources'])
                or progress.get('reportPublication', {}).get('kind') == 'not_published'
            )
            return {
                'status': 'not_published' if rejected else 'completed',
                'result': {
                    'basePublications': [item['result'] for item in progress['sources']],
                    'projectionRef': progress.get('projectionRef'),
                    'processingStatus': progress.get('processingStatus'),
                    'reportPublication': progress.get('reportPublication'),
                },
            }
        raise DocumentFormalSourceConflict('unknown persisted formal phase')

    async def _source_step(self, command, row, source, index):
        source = deepcopy(source)
        item = DocumentPreparedPublicationInput.from_dict(source['input'])
        scope, phase = command.scope, source['phase']
        source_id = source['sourceId']
        request_id = command.command_id + ':' + str(index)
        complete = (
            source['quality'] == 'complete'
            and source['inputVersionStatus'] == 'known'
            and source['interpretationRevision'] == 'analysis-health-report-v12'
        )
        if phase in {'route', 'wait_prior'}:
            lifecycle = await self.managed.lifecycle.read(scope=scope, source_id=source_id)
            if lifecycle.mode == 'legacy':
                if phase == 'wait_prior':
                    raise DocumentFormalSourceConflict('a selected managed source cannot fall back to legacy')
                if not self.profile_admission.supports_scope(scope):
                    source.update(route='legacy', phase='legacy', routeReason='scope-v1-legacy-compat')
                    return source
                # A compatibility decision is fixed BEFORE any formal write. An
                # unavailable/truncated history never supplies fresh permission.
                history = await self.admission.history.read_absence(scope=scope, source_id=source_id, limit=1000)
                presence = await self.admission.profile_presence.read_absence(
                    scope=scope, source_id=source_id, limit=1000
                )
                fresh = complete and history.get('status') == 'absent' and presence.get('status') == 'absent'
                source.update(route='fresh' if fresh else 'legacy', phase='fresh_plan' if fresh else 'legacy')
                return source
            if lifecycle.status in {'withdrawn', 'retracting'} or not complete:
                source.update(
                    phase='done',
                    result={
                        'kind': 'not_published',
                        'sourceId': source_id,
                        'reason': lifecycle.status if complete else 'incomplete_fixed_input',
                    },
                )
                return source
            if lifecycle.status != 'active':
                if 'awaitedRevision' not in source:
                    source.update(phase='wait_prior', awaitedRevision=max(1, lifecycle.publication_revision))
                    return source
                raise DocumentFormalSourceWait()
            active = await self._active_base(scope, lifecycle)
            historical = await self._older_round(command, source, item, lifecycle, active)
            if historical is not None:
                return historical
            if source.get('awaitedRevision', lifecycle.publication_revision) != lifecycle.publication_revision:
                raise DocumentFormalSourceConflict('the selected prior publication advanced')
            selected = DocumentManagedSourceCommand(
                scope,
                source_id,
                request_id,
                lifecycle.publication_revision,
                lifecycle.active_snapshot_ref,
                'replace_base',
                (item,),
                MANAGED_SOURCE_REVISION,
            )
            source.update(route='managed', phase='managed', managedCommand=selected.as_dict())
            return source
        if phase == 'legacy':
            receipt = await self.compatibility.publish_prepared(
                scope=scope,
                understanding_snapshot_id=item.understanding_snapshot_ref,
                selection=item.selection,
                preparation_receipt=item.preparation_receipt,
            )
            source.update(
                phase='done',
                result={
                    'kind': 'legacy',
                    'sourceId': source_id,
                    'receipt': asdict(receipt),
                    'canonicalReportId': receipt.canonical_report_id,
                    'legacySourceRevision': receipt.legacy_source_revision,
                    'ingestionStatus': receipt.ingestion_status,
                    'jobId': receipt.ingestion_job_id,
                },
            )
            return source
        if phase == 'managed':
            selected = DocumentManagedSourceCommand.from_dict(source['managedCommand'])
            lifecycle = await self.managed.lifecycle.read(scope=scope, source_id=source_id)
            if lifecycle.status == 'active' and lifecycle.publication_revision > selected.prior_publication_revision:
                active = await self._active_base(scope, lifecycle)
                historical = await self._older_round(command, source, item, lifecycle, active)
                if historical is not None:
                    return historical
            receipt = await self.managed.commands.execute(selected)
            archived = await self.managed.archive.read(scope=scope, archive_ref=receipt['baseArchiveRef'])
            source.update(
                phase='done',
                result={
                    'kind': 'archive',
                    'sourceId': source_id,
                    'canonicalReportId': archived['receipt']['canonicalReportId'],
                    'legacySourceRevision': archived['receipt']['legacySourceRevision'],
                    'ingestionStatus': 'source_managed',
                    'jobId': None,
                    'archiveRef': receipt['baseArchiveRef'],
                    'managedCommandId': receipt['commandId'],
                    'publication': receipt['publication'],
                    'publicationRevision': receipt['publicationRevision'],
                    'sourceSnapshotRef': receipt['sourceSnapshotRef'],
                },
            )
            return source
        if phase == 'fresh_plan':
            lifecycle = await self.managed.lifecycle.read(scope=scope, source_id=source_id)
            if lifecycle.mode == 'events':
                if lifecycle.status in {'withdrawn', 'retracting'}:
                    source.update(
                        phase='done',
                        result={'kind': 'not_published', 'sourceId': source_id, 'reason': lifecycle.status},
                    )
                    return source
                if lifecycle.status != 'active':
                    raise DocumentFormalSourceWait()
                active = await self._active_base(scope, lifecycle)
                historical = await self._older_round(command, source, item, lifecycle, active)
                if historical is not None:
                    return historical
                matched = (
                    active.understanding.snapshot_id == item.understanding_snapshot_ref
                    and active.material.snapshot_id == item.material_snapshot_ref
                )
                if not matched:
                    raise DocumentFormalSourceConflict('a competing fresh source published different fixed material')
                # Reuse through the ordinary exact-prior no-op path; no new fresh permission is granted.
                selected = DocumentManagedSourceCommand(
                    scope,
                    source_id,
                    request_id,
                    lifecycle.publication_revision,
                    lifecycle.active_snapshot_ref,
                    'replace_base',
                    (item,),
                    MANAGED_SOURCE_REVISION,
                )
                source.update(route='managed', phase='managed', managedCommand=selected.as_dict())
                return source
            try:
                plan_ref = await self.admission.prepare_fresh_plan(scope=scope, prepared=item, request_id=request_id)
            except RuntimeError:
                if (await self.managed.lifecycle.read(scope=scope, source_id=source_id)).mode != 'legacy':
                    raise DocumentFormalSourceWait() from None
                raise
            source.update(phase='fresh_capture', planRef=plan_ref)
            return source
        if phase == 'fresh_capture':
            captured = await self.managed.source_reader.capture_source(scope=scope, verified_plan_ref=source['planRef'])
            plan = await self.admission.read_verified_plan(scope=scope, plan_ref=source['planRef'])
            authorization = self.profile_admission.authorization(
                command_id=request_id,
                scope=scope,
                source_id=source_id,
                captured=asdict(captured),
                fence=plan.fence,
                accepted_at=row['createdAt'],
            )
            source.update(phase='fresh_reserve', captured=asdict(captured), authorization=authorization)
            return source
        if phase == 'fresh_reserve':
            await self.profile_admission.reserve(source['authorization'])
            source['phase'] = 'fresh_authorize'
            return source
        if phase == 'fresh_authorize':
            await self.profile_admission.authorize(source['authorization'])
            source['phase'] = 'fresh_archive'
            return source
        if phase == 'fresh_archive':
            bundle = await self._bundle(scope, item)
            archive_ref, saved = await self.managed.archive.base(scope=scope, origin=bundle, current=bundle)
            source.update(phase='fresh_activate', archiveRef=archive_ref, archiveReceipt=saved['receipt'])
            return source
        if phase == 'fresh_activate':
            await self.admission.activate(
                scope=scope,
                plan_ref=source['planRef'],
                source_reader=self.managed.source_reader,
                source_snapshot_id=source['captured']['source_snapshot_ref'],
            )
            source['phase'] = 'fresh_publish'
            return source
        if phase == 'fresh_publish':
            receipt = await self.managed.publisher.publish(
                scope=scope,
                snapshot_id=source['captured']['source_snapshot_ref'],
                request_id=request_id,
                expected_prior_revision=None,
            )
            source.update(
                phase='done',
                result={
                    'kind': 'archive',
                    'sourceId': source_id,
                    'archiveRef': source['archiveRef'],
                    'canonicalReportId': source['archiveReceipt']['canonicalReportId'],
                    'legacySourceRevision': source['archiveReceipt']['legacySourceRevision'],
                    'ingestionStatus': 'source_managed',
                    'jobId': None,
                    'publication': asdict(receipt),
                    'publicationRevision': receipt.publication_revision,
                    'sourceSnapshotRef': source['captured']['source_snapshot_ref'],
                },
            )
            return source
        raise DocumentFormalSourceConflict('unknown fixed source publication phase')

    async def _report_step(self, command, progress):
        if any(item['result'].get('historicalOnly') for item in progress['sources']):
            progress['reportPublication'] = {'kind': 'not_published', 'reason': 'superseded_fixed_input'}
            return {'phase': 'finish', 'progress': progress}
        if progress['processingStatus'] != 'complete' or any(
            item['result']['kind'] == 'not_published' for item in progress['sources']
        ):
            progress['reportPublication'] = {'kind': 'not_published', 'reason': 'incomplete_fixed_input'}
            return {'phase': 'finish', 'progress': progress}
        first = progress['sources'][0]
        if first['result']['kind'] == 'legacy':
            bases = tuple(item['result'] for item in progress['sources'])
            if command.command_revision == FORMAL_REPORT_REUSE_REVISION:
                receipt = await self.legacy_reports.publish_prepared(
                    command=command, bases=bases, projection_ref=progress['projectionRef']
                )
            else:
                receipt = await self.legacy_reports.publish(command=command, bases=bases)
            progress['reportPublication'] = receipt
            return {'phase': 'finish', 'progress': progress}
        if 'reportCommand' not in progress:
            lifecycle = await self.managed.lifecycle.read(scope=command.scope, source_id=first['sourceId'])
            expected = first['result']['publicationRevision']
            if lifecycle.status in {'withdrawn', 'retracting'} or lifecycle.publication_revision != expected:
                raise DocumentFormalSourceConflict('the Report base changed before its fixed supplement')
            if lifecycle.status != 'active':
                raise DocumentFormalSourceWait()
            if lifecycle.active_snapshot_ref != first['result']['sourceSnapshotRef']:
                raise DocumentFormalSourceConflict('the Report base no longer matches its fixed source')
            selected = DocumentManagedSourceCommand(
                command.scope,
                first['sourceId'],
                command.command_id + ':report',
                expected,
                lifecycle.active_snapshot_ref,
                'replace_report',
                command.inputs,
                MANAGED_SOURCE_REVISION,
            )
            progress['reportCommand'] = selected.as_dict()
            return {'progress': progress}
        receipt = await self.managed.commands.execute(DocumentManagedSourceCommand.from_dict(progress['reportCommand']))
        if receipt['projectionRef'] != progress['projectionRef']:
            raise DocumentFormalSourceConflict('formal Report publication differs from the prepared business output')
        progress['reportPublication'] = receipt
        return {'phase': 'finish', 'progress': progress}
