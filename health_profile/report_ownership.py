"""Fix a report's patient destinations before applying any of its contributions."""

from copy import deepcopy
from unicodedata import normalize

from .basic_info import _value
from .contracts import profile_content_digest
from .models import SubjectKind
from .models import SubjectStatus
from .service import HealthProfileService
from .source_material import ProfileSourceApplicationError

PERSON_FIELDS = ('name', 'gender')
UNASSIGNED = 'unassigned'


def _document_id(row, documents):
    document = row.get('sourceDocumentId') or (row.get('physicalOccurrence') or {}).get('documentId')
    if document is None and len(documents) == 1:
        document = documents[0]
    if document not in documents:
        raise ProfileSourceApplicationError('source.document_patient_material_mismatch')
    return document


def patient_groups(material):
    by_document = {}
    for row in (*material.basic_info, *material.observations):
        document = _document_id(row, material.routing_documents)
        by_document.setdefault(document, []).append(row)
    for row in material.routing_identity:
        if rows := by_document.get(_document_id(row, material.routing_documents)):
            rows.append(row)
    result = {}
    for document, rows in by_document.items():
        multiple = any(row.get('patientSlot') for row in rows)
        slots = {}
        for row in rows:
            slot = row.get('patientSlot') or (UNASSIGNED if multiple else '')
            fields = slots.setdefault(slot, [])
            if row.get('field') in PERSON_FIELDS:
                fields.append(row)
        result[document] = slots
    return result


def report_target(row, material, targets):
    document = _document_id(row, material.routing_documents)
    slot = row.get('patientSlot') or ('' if (document, '') in targets else UNASSIGNED)
    return targets.get((document, slot))


def normalized_person_value(field, value):
    try:
        canonical = _value(field, value)
    except (TypeError, ValueError):
        return ''
    return ' '.join(normalize('NFKC', canonical).casefold().split())


def _incoming_identity(fields):
    values = {field: set() for field in PERSON_FIELDS}
    for row in fields:
        value = normalized_person_value(row['field'], row.get('value'))
        if value:
            values[row['field']].add(value)
    return {field: next(iter(items)) for field, items in values.items() if len(items) == 1}


def _base_slot(base, identities, multiple):
    if not multiple:
        incoming = identities.get('', {})
        both_differ = all(
            field in base and field in incoming and base[field] != incoming[field] for field in PERSON_FIELDS
        )
        return None if both_differ else ''
    matches = []
    for slot, identity in identities.items():
        comparable = base.keys() & identity.keys()
        if slot != UNASSIGNED and comparable and all(base[field] == identity[field] for field in comparable):
            matches.append((len(comparable), slot))
    if not matches:
        return None
    best = max(rank for rank, _ in matches)
    possible = [slot for rank, slot in matches if rank == best]
    return possible[0] if len(possible) == 1 else None


def _route_key(scope, document):
    return 'profile_report_owners_' + profile_content_digest([*scope, document])


def _route_rows(archived, staged):
    return {**(staged or {}).get('slots', {}), **(archived or {}).get('slots', {})}


class ReportOwnership:
    def __init__(self, repository, basic=None):
        self.repository = repository
        self.basic = basic if basic is not None else getattr(repository, 'basic_info', None)

    async def routes(self, scope, epoch, document_id):
        if self.basic is None:
            return {}
        owner = (scope.user_id, scope.subject_id, epoch)
        key = _route_key(owner, document_id)
        state = await self.basic.store.get(self.basic._scope_key(*owner))
        fixed = await self.basic.store.get(key)
        staged = (state or {}).get('reportRoutes', {}).get(key)
        return {slot: row['target'] for slot, row in _route_rows(fixed, staged).items() if not row.get('abandoned')}

    async def lookup(self, scope, epoch, document_id, patient_slot=None):
        return (await self.routes(scope, epoch, document_id)).get(patient_slot or '')

    @staticmethod
    def _selection(state):
        blocked = set(state['blockedFields'])
        selected = {}
        for field in PERSON_FIELDS:
            key = state['selected'].get(field)
            if key and field not in blocked:
                source = state['sources'][key]
                if source['active'] and (value := normalized_person_value(field, source['value'])):
                    selected[field] = value
        for provisional in state.get('reportRoutingPending', {}).values():
            for field, value in provisional.items():
                if field not in blocked:
                    selected.setdefault(field, value)
        return selected

    @staticmethod
    def _reserve_identity(state, current, publication_id, identity):
        tentative = {field: value for field, value in identity.items() if field not in current}
        if tentative:
            pending = state.setdefault('reportRoutingPending', {}).setdefault(publication_id, {})
            pending.update(tentative)
            current.update(tentative)

    def _resume_slot(self, state, current, key, document, slot, archived, publication_id):
        identities = archived['identities']
        if archived['publicationId'] != publication_id or _base_slot(current, identities, '' not in identities) != slot:
            raise ProfileSourceApplicationError('source.document_patient_ownership_conflict')
        self._reserve_identity(state, current, publication_id, identities[slot])
        grouped = state.setdefault('reportRoutes', {}).setdefault(key, {'documentId': document, 'slots': {}})
        grouped['slots'][slot] = {
            'target': archived['target'],
            'publicationId': publication_id,
            'identities': identities,
            'resuming': True,
        }

    async def reserve(self, subject, material, publication_id):
        if self.basic is None:
            raise ProfileSourceApplicationError('source.basic_info_provider_unavailable', retryable=True)
        groups = patient_groups(material)
        if not groups:
            return {}
        identities = {
            document: {slot: _incoming_identity(fields) for slot, fields in slots.items()}
            for document, slots in groups.items()
        }
        scope = (subject.user_id, subject.subject_id, subject.lifecycle_epoch)
        for _ in range(32):
            state = await self.basic._scope(*scope)
            after = deepcopy(state)
            routes = after.setdefault('reportRoutes', {})
            current = self._selection(state)
            targets = {}
            for document in material.routing_documents:
                if document not in identities:
                    continue
                slots = identities[document]
                key = _route_key(scope, document)
                fixed = _route_rows(await self.basic.store.get(key), routes.get(key))
                multiple = '' not in slots
                if fixed and ('' in fixed) == multiple:
                    raise ProfileSourceApplicationError('source.document_patient_ownership_conflict')
                base_slot = _base_slot(current, slots, multiple)
                base_used = any(
                    row['target']['subjectId'] == subject.subject_id
                    for row in fixed.values()
                    if not row.get('abandoned')
                )
                for slot, identity_fields in slots.items():
                    existing = fixed.get(slot)
                    if existing is not None:
                        if existing.get('abandoned'):
                            self._resume_slot(after, current, key, document, slot, existing, publication_id)
                        target = existing['target']
                    elif fixed and slot != UNASSIGNED:
                        raise ProfileSourceApplicationError('source.document_patient_ownership_conflict')
                    elif slot == base_slot and not base_used:
                        target = {'subjectId': subject.subject_id, 'lifecycleEpoch': subject.lifecycle_epoch}
                        self._reserve_identity(after, current, publication_id, identity_fields)
                        base_used = True
                    else:
                        identity = profile_content_digest([*scope, document, slot])
                        target = {'subjectId': 'subject_report_' + identity, 'lifecycleEpoch': 1, 'requestId': identity}
                    targets[document, slot] = target
                    if existing is None:
                        grouped = routes.setdefault(key, {'documentId': document, 'slots': {}})
                        pending = {'target': target, 'publicationId': publication_id}
                        if target['subjectId'] == subject.subject_id:
                            pending['identities'] = slots
                        grouped['slots'][slot] = pending
            if after == state or await self.basic.store.cas(state, after) is not None:
                return targets
        raise ProfileSourceApplicationError('source.concurrent_patient_ownership', retryable=True)

    async def materialize(self, subject, targets, clock):
        for target in targets.values():
            if 'requestId' not in target:
                continue
            request_id = target['requestId']
            service = HealthProfileService(
                self.repository,
                clock=clock,
                id_factory=lambda prefix: target['subjectId'] if prefix == 'subject' else 'event_report_' + request_id,
            )
            await service.upsert_subject(
                subject.user_id,
                'report-patient:' + request_id,
                kind=SubjectKind.OTHER,
                relationship={'relationType': 'pending', 'isBloodRelative': False},
            )
        for target in targets.values():
            live = await self.repository.get_subject(subject.user_id, target['subjectId'])
            if (
                live is None
                or live.status is not SubjectStatus.ACTIVE
                or live.lifecycle_epoch != target['lifecycleEpoch']
            ):
                raise ProfileSourceApplicationError('source.document_lifecycle_mismatch')

    async def validate_plan(self, scope, epoch, bindings, publication_id):
        if self.basic is None:
            raise ProfileSourceApplicationError('source.basic_info_provider_unavailable', retryable=True)
        owner = (scope.user_id, scope.subject_id, epoch)
        for _ in range(32):
            state = await self.basic._scope(*owner)
            after = deepcopy(state)
            current = self._selection(state)
            for binding in bindings:
                document, slot = binding['documentId'], binding['patientSlot']
                key = _route_key(owner, document)
                fixed = await self.basic.store.get(key)
                if fixed is not None and (archived := fixed['slots'].get(slot)) and archived.get('abandoned'):
                    self._resume_slot(after, current, key, document, slot, archived, publication_id)
            if after == state or await self.basic.store.cas(state, after) is not None:
                return
        raise ProfileSourceApplicationError('source.concurrent_patient_ownership', retryable=True)

    async def _archive(self, key, document_id, slots):
        for _ in range(32):
            fixed = await self.basic.store.get(key)
            if fixed is None:
                fixed = await self.basic.store.create(
                    {
                        '_id': key,
                        'recordKind': 'report_patient_routes',
                        'revision': 0,
                        'documentId': document_id,
                        'slots': slots,
                    }
                )
            existing = fixed['slots']
            if any(existing[slot]['target'] != row['target'] for slot, row in slots.items() if slot in existing):
                raise ProfileSourceApplicationError('source.document_patient_ownership_conflict')
            merged = dict(existing)
            for slot, row in slots.items():
                previous = existing.get(slot)
                if previous is None or (
                    previous['abandoned']
                    and not row['abandoned']
                    and previous['publicationId'] == row.get('publicationId')
                ):
                    merged[slot] = row
            if merged == existing:
                return
            after = {**fixed, 'slots': merged}
            if await self.basic.store.cas(fixed, after) is not None:
                return
        raise ProfileSourceApplicationError('source.concurrent_patient_ownership', retryable=True)

    async def finish(self, scope, epoch, publication_id, *, failed=False):
        if self.basic is None:
            return
        key = self.basic._scope_key(scope.user_id, scope.subject_id, epoch)
        for _ in range(32):
            state = await self.basic.store.get(key)
            if state is None:
                return
            after = deepcopy(state)
            provisional = after.get('reportRoutingPending', {}).pop(publication_id, None)
            routes = after.get('reportRoutes', {})
            for route_key, route in list(routes.items()):
                slots = {}
                for slot, row in list(route['slots'].items()):
                    if row['publicationId'] == publication_id:
                        target = row['target']
                        abandoned = bool(
                            failed and (row.get('resuming') or provisional and target['subjectId'] == scope.subject_id)
                        )
                        slots[slot] = {'target': target, 'abandoned': abandoned, 'publicationId': publication_id}
                        if abandoned:
                            slots[slot]['identities'] = row['identities']
                        del route['slots'][slot]
                if not route['slots']:
                    del routes[route_key]
                if slots:
                    await self._archive(route_key, route['documentId'], slots)
            if after == state or await self.basic.store.cas(state, after) is not None:
                return
        raise ProfileSourceApplicationError('source.concurrent_patient_ownership', retryable=True)
