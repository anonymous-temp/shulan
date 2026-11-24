"""Project authorized Profile facts into report materials for Health Analysis."""

from pymongo.errors import PyMongoError

from .contracts import ProfileScope
from .document_archive_contracts import ProfileDocumentArchiveChangedError
from .document_archive_contracts import ProfileDocumentArchiveReadError
from .document_archive_contracts import ProfileReportLifecycleConflict
from .observation_dates import recorded_report_source

_REPORT_CATEGORIES = (
    'anthropometric',
    'laboratory',
    'assessment',
    'report_observation',
    'condition',
    'goal',
    'symptom',
    'family_history',
)
_USABLE_VERIFICATION = {'verified_source', 'confirmed'}


class ProfileReportFactsReader:
    """Read the fixed self scope without falling back to the upload custody scope."""

    def __init__(self, profiles, *, max_facts=1000, page_size=200):
        if type(max_facts) is not int or max_facts < 1 or type(page_size) is not int or not 1 <= page_size <= 1000:
            raise ValueError('invalid Profile report material bounds')
        self.profiles, self.max_facts, self.page_size = profiles, max_facts, page_size

    async def read(self, *, user_id, subject_id=None, expected_lifecycle_epoch=None):
        if expected_lifecycle_epoch is not None and (
            type(expected_lifecycle_epoch) is not int or expected_lifecycle_epoch < 1 or subject_id is None
        ):
            raise ValueError('the fixed confirmation requires a subject and positive lifecycle')
        for attempt in range(2):
            try:
                return await self._read(user_id, subject_id, expected_lifecycle_epoch)
            except ProfileReportLifecycleConflict:
                raise
            except ProfileDocumentArchiveChangedError:
                if attempt:
                    raise
            except (PyMongoError, TimeoutError):
                raise ProfileDocumentArchiveReadError('Profile report facts unavailable') from None

    async def _read(self, user_id, subject_id, epoch):
        if epoch is None:
            scope = await self.profiles.resolve_scope(user_id)
            if scope is None:
                return _result(None, None)
            if scope.user_id != user_id or (subject_id is not None and scope.subject_id != subject_id):
                raise PermissionError('Profile report facts escaped the requested self scope')
        else:
            scope = ProfileScope(user_id=user_id, subject_id=subject_id)
            if not await self._check_lifecycle(scope, epoch):
                return _result(None, None)
        materials, identities, snapshot = [], set(), []
        cursor, revision, count, pending, seen_cursors = None, None, 0, False, set()
        while True:
            page = await self.profiles.read_page(
                scope.user_id,
                subject_id=scope.subject_id,
                page_size=self.page_size,
                categories=_REPORT_CATEGORIES,
                cursor=cursor,
            )
            if page.status == 'restart_required':
                raise ProfileDocumentArchiveChangedError('Profile facts changed during the read')
            if page.status != 'ready':
                if epoch is not None and page.status == 'missing':
                    await self._check_lifecycle(scope, epoch)
                raise ProfileDocumentArchiveReadError('Profile report facts unavailable')
            if page.scope != scope or page.subject is None or page.subject.kind != 'self':
                raise PermissionError('Profile report facts escaped the fixed self scope')
            if page.coverage.get('publicationApplying') or page.coverage.get('revisionStable') is not True:
                raise ProfileDocumentArchiveChangedError('Profile facts changed during the read')
            if revision is not None and revision != page.profile_revision:
                raise ProfileDocumentArchiveChangedError('Profile facts changed during the read')
            revision = page.profile_revision
            pending |= page.stale
            count += len(page.facts)
            if count > self.max_facts or (count == self.max_facts and page.next_cursor):
                raise OverflowError('Profile report fact capacity exceeded')
            for fact in page.facts:
                if fact.fact_id in identities:
                    raise ProfileDocumentArchiveChangedError('Profile fact repeated during the read')
                identities.add(fact.fact_id)
                if material := _report_material(fact):
                    materials.append(material)
                    snapshot.append({'factId': fact.fact_id, 'version': fact.version, **material['metadata']})
            cursor = page.next_cursor
            if cursor is None:
                break
            if not page.facts or cursor in seen_cursors:
                raise ProfileDocumentArchiveChangedError('Profile fact pagination did not advance')
            seen_cursors.add(cursor)
        if epoch is not None and not await self._check_lifecycle(scope, epoch):
            raise ProfileReportLifecycleConflict('Profile subject lifecycle changed after confirmation')
        return _result(scope, epoch, materials=materials, revision=revision, facts=snapshot, pending=pending)

    async def _check_lifecycle(self, scope, epoch):
        binding = await self.profiles.resolve_existing_self_lifecycle(scope)
        if binding is None:
            return False
        if binding != (scope.subject_id, epoch, 'active'):
            raise ProfileReportLifecycleConflict('Profile subject lifecycle changed after confirmation')
        return True


def _report_material(fact):
    sources = [
        source.model_dump(mode='json', by_alias=True)
        for source in fact.sources
        if source.source_type == 'health_report'
    ]
    if not sources:
        return None
    checked = fact.status == 'active' and fact.verification_status in _USABLE_VERIFICATION
    value = fact.value
    if fact.value_type == 'quantity' and isinstance(value, dict):
        number, raw, comparator = value.get('number'), value.get('raw'), value.get('comparator')
        result = number if number is not None else raw
        if comparator and number is not None:
            result = raw or str(comparator) + str(number)
        content = {
            'resultValue': result,
            'rawValue': raw,
            **{key: value[key] for key in ('unit', 'specimen', 'method') if key in value},
        }
    else:
        content = {'observation': value}
    observed_at = fact.effective_period.get('start')
    content.update(
        itemName=fact.display_name,
        factType=fact.fact_type,
        measuredAt=observed_at.isoformat() if observed_at is not None else None,
        flag=fact.interpretation,
        referenceRange=fact.reference_range,
    )
    if observed_at is None and (source := recorded_report_source(fact.sources)) is not None:
        content['recordedAt'] = source.recorded_at.isoformat()
    return {
        'id': 'report:' + fact.fact_id,
        'kind': 'report',
        **({'backgroundScope': 'family_history'} if fact.fact_type.startswith('family_history.') else {}),
        **({} if checked else {'status': 'unknown'}),
        'content': content,
        'metadata': {'factStatus': fact.status, 'verificationStatus': fact.verification_status, 'sources': sources},
    }


def _result(scope, epoch, *, materials=(), revision=None, facts=(), pending=False):
    return {
        'contractVersion': 'profile-report-materials.v1',
        'scope': (
            {**scope.model_dump(by_alias=True), **({'lifecycleEpoch': epoch} if epoch is not None else {})}
            if scope is not None
            else None
        ),
        'materials': list(materials),
        'gender': None,
        'status': 'partial' if pending else 'available' if materials else 'empty',
        'coverage': {'complete': not pending, 'reason': 'existing_self_missing' if scope is None else None},
        'sourceSnapshot': {'profileRevision': revision, 'facts': list(facts)},
    }
