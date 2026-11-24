"""Document-owned admission storage/history plus public Profile-only bridges."""

import asyncio
import json

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.compatibility import MongoDocumentCompatibilityStore
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.complete_source import DocumentCompleteSourceService
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.report_publication_contracts import DocumentReportPublicationManifest
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import canonical_json
from modules.analysis.source_admission import OWNER_FENCE_CONTRACT
from modules.analysis.source_admission import DocumentFreshSourceAdmission
from modules.health_profile.contracts import ProfileScope


class MongoDocumentAdmissionStore(MongoDocumentCompatibilityStore):
    async def get(self, kind, scope, identity):
        row = await self._get(kind, scope, identity)
        return None if row is None else row['payload']

    async def require(self, kind, scope, identity):
        value = await self.get(kind, scope, identity)
        if value is None:
            raise DocumentNotFoundError('fixed source admission proof is unavailable in this scope')
        return value

    async def put(self, kind, scope, identity, value):
        # BSON arrays read back as lists; timestamps in these immutable proof
        # payloads are ISO strings so precision and timezone never drift.
        value = json.loads(canonical_json(value))
        if await self._insert(kind, scope, identity, value) != value:
            raise DocumentCompleteSourceUnavailable('immutable source admission proof conflicts')


class MongoDocumentFormalHistoryReader:
    """Bounded all-status history; no repair, cleanup, indexes or Profile tables.

    Unknown legacy data and an exhausted scope scan cannot be called absent.
    Fixed unpublished preparations are allowed. Formal Report commands are not:
    a command may still attempt publication after an interrupted executor.
    """

    def __init__(self, *, compatibility_records, report_commands, observations, ledger, snapshots):
        self.compatibility_records = compatibility_records
        self.report_commands = report_commands
        self.observations = observations
        self.ledger = ledger
        self.snapshots = snapshots

    async def read_absence(self, *, scope, source_id, limit):
        if not isinstance(scope, DocumentScope) or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('explicit scope and bounded history scan are required')
        try:
            return await asyncio.wait_for(self._read_absence(scope=scope, source_id=source_id, limit=limit), timeout=10)
        except TimeoutError:
            return {'status': 'unavailable', 'reason': 'formal_history_deadline'}

    async def _read_absence(self, *, scope, source_id, limit):
        try:
            # Null/mismatched subject legacy rows with this canonical source must
            # also block fresh admission, not disappear behind a scope filter.
            observations = (
                await self.observations.find({'canonicalReportId': source_id}).sort([('_id', 1)]).to_list(length=1)
            )
            if observations:
                return {'status': 'present', 'reason': 'legacy_observations'}
            intents = await self.ledger.intents.find({'event.sourceId': source_id}).sort([('_id', 1)]).to_list(length=1)
            if intents:
                return {'status': 'present', 'reason': 'event_history'}
            rows = (
                await self.compatibility_records.find({'userId': scope.user_id})
                .sort([('_id', 1)])
                .to_list(length=limit + 1)
            )
            if len(rows) > limit:
                return {'status': 'unavailable', 'reason': 'compatibility_scan_truncated'}
            for row in rows:
                payload = row['payload']
                if row['kind'] == 'preparation':
                    continue
                if row['kind'] != 'legacy_receipt':
                    return {'status': 'unavailable', 'reason': 'unknown_compatibility_history'}
                if payload['canonical_report_id'] == source_id:
                    return {'status': 'present', 'reason': 'legacy_receipt'}
            commands = (
                await self.report_commands.find({'userId': scope.user_id}).sort([('_id', 1)]).to_list(length=limit + 1)
            )
            if len(commands) > limit:
                return {'status': 'unavailable', 'reason': 'report_command_scan_truncated'}
            remaining_materials = limit
            for row in commands:
                payload = row['payload']
                row_scope = DocumentScope(row['userId'], row['subjectId'])
                if row['kind'] == 'report_output_trace':
                    execution = await self.snapshots.get(row_scope, payload['binding']['executionSnapshotRef'])
                    raw = execution.payload['manifest']
                elif row['kind'] == 'report_command':
                    raw = payload
                elif row['kind'] in {'report_execution', 'report_receipt'}:
                    raw = payload['manifest']
                else:
                    return {'status': 'unavailable', 'reason': 'unknown_report_history'}
                manifest = DocumentReportPublicationManifest.from_dict(raw)
                if manifest.scope != row_scope:
                    return {'status': 'unavailable', 'reason': 'report_history_scope_ambiguous'}
                for prepared in manifest.inputs:
                    if remaining_materials == 0:
                        return {'status': 'unavailable', 'reason': 'report_material_scan_truncated'}
                    remaining_materials -= 1
                    material = await self.snapshots.get(row_scope, prepared.material_snapshot_ref)
                    if material.source_id == source_id:
                        return {'status': 'present', 'reason': 'report_command'}
            return {
                'status': 'absent',
                'checked': ['legacy_observations', 'event_history', 'legacy_receipts', 'report_commands'],
            }
        except Exception:
            return {'status': 'unavailable', 'reason': 'formal_history_unavailable'}


class ProfileDocumentFreshAdmissionReader:
    """Only public Profile presence/admission methods; never direct table access."""

    def __init__(self, *, presence, admission):
        self.presence = presence
        self.admission = admission

    @staticmethod
    def _scope(scope):
        return ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id)

    async def read_absence(self, *, scope, source_id, limit):
        value = await self.presence.read_legacy_presence(
            self._scope(scope), source_type='health_report', source_id=source_id, limit=limit
        )
        return value.model_dump(mode='json', by_alias=True)

    async def read_ready(self, *, scope, source_id):
        value = await self.admission.get_cutover(self._scope(scope), 'health_report', source_id)
        return None if value is None else value.model_dump(mode='json', by_alias=True)


class ProfileDocumentOwnerFenceReader:
    def __init__(self, admission):
        self.admission = admission

    @staticmethod
    def _identity(scope, snapshot_ref):
        if (
            not isinstance(scope, ProfileScope)
            or snapshot_ref.provider != 'document'
            or snapshot_ref.contract_version != OWNER_FENCE_CONTRACT
        ):
            raise DocumentCompleteSourceUnavailable('explicit owner fence scope and contract are required')
        return DocumentScope(scope.user_id, scope.subject_id), snapshot_ref.snapshot_id

    async def read_admission_fence(self, *, scope, snapshot_ref):
        scope, ref = self._identity(scope, snapshot_ref)
        return await self.admission.read_admission_fence(scope=scope, snapshot_id=ref)

    async def is_admission_fence_held(self, *, scope, snapshot_ref, content_digest):
        scope, ref = self._identity(scope, snapshot_ref)
        return await self.admission.is_admission_fence_held(scope=scope, snapshot_id=ref, content_digest=content_digest)


def create_document_fresh_source_admission(
    *,
    compatibility,
    report_publications,
    ledger,
    admission_records,
    observation_collection,
    profile_presence,
    profile_inbox,
):
    """Use existing shared resources; construction performs zero IO.

    This new immutable admission collection requires unique _id, PRIMARY reads,
    majority writes and no TTL. History scans require userId/_id indexes on
    compatibility and Report commands, canonicalReportId/_id on observations,
    and event.sourceId/_id on intents. Deployment owns indexes/collections.
    The injected ledger MUST be the exact shared legacy gateway ledger. Neither
    factory construction nor successful preparation changes production defaults.
    """
    if compatibility.legacy_publication.ledger is not ledger:
        raise ValueError('fresh admission and every legacy publisher must share the same ledger')
    if report_publications.compatibility is not compatibility:
        raise ValueError('fresh history must use the actual Report compatibility publisher')
    options = {'read_preference': ReadPreference.PRIMARY, 'write_concern': WriteConcern(w='majority')}
    snapshots = compatibility.understanding.snapshots
    profile = ProfileDocumentFreshAdmissionReader(presence=profile_presence, admission=profile_inbox)
    return DocumentFreshSourceAdmission(
        compatibility=compatibility,
        contributions=DocumentCompleteSourceService(
            snapshots=snapshots,
            understanding=compatibility.understanding,
            report_publications=report_publications,
            admission=None,
        ),
        ledger=ledger,
        records=MongoDocumentAdmissionStore(admission_records.with_options(**options)),
        history=MongoDocumentFormalHistoryReader(
            compatibility_records=compatibility.publications.collection.with_options(
                read_preference=ReadPreference.PRIMARY
            ),
            report_commands=report_publications.commands.collection.with_options(
                read_preference=ReadPreference.PRIMARY
            ),
            observations=observation_collection.with_options(read_preference=ReadPreference.PRIMARY),
            ledger=ledger,
            snapshots=snapshots,
        ),
        profile_presence=profile,
        profile_ready=profile,
    )
