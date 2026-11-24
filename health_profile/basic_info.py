"""Profile-owned demographic selection and durable user confirmations."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from .basic_info_contracts import BasicInfoCommandPageV1
from .basic_info_contracts import BasicInfoCommandReceiptV1
from .basic_info_contracts import BasicInfoCurrentFieldV2
from .basic_info_contracts import BasicInfoEvidenceV1
from .basic_info_contracts import BasicInfoReportRetirement
from .basic_info_contracts import BasicInfoSnapshotV2
from .basic_info_contracts import BasicInfoSourceV2
from .basic_info_contracts import BasicInfoValuesV1
from .basic_info_store import BasicInfoConflict
from .basic_info_store import BasicInfoUnavailable
from .basic_info_store import _bson_time
from .models import AssertionLevel
from .models import EffectivePeriod
from .models import FactCategory
from .models import FactSource
from .models import FactStatus
from .models import FactValueType
from .models import HealthFact
from .models import HealthSubject
from .models import SubjectKind
from .models import SubjectStatus
from .models import VerificationStatus
from .models import fact_in_lifecycle

FIELDS = ('name', 'gender', 'age')
RANK = {'low': 1, 'medium': 2, 'very_high': 3}
ALIASES = {
    'demographic.name': 'name',
    'name': 'name',
    'demographic.sex': 'gender',
    'demographic.gender': 'gender',
    'gender': 'gender',
    'demographic.age': 'age',
    'age': 'age',
}
BIRTH_TYPES = frozenset({'demographic.date_of_birth', 'demographic.birth_date', 'date_of_birth'})
ZONE = timezone(timedelta(hours=8))
TERMINAL = frozenset({'applied', 'no_change', 'superseded', 'blocked', 'cancelled'})


def field_for_fact(fact):
    return ALIASES.get(fact.fact_type.casefold())


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def report_contribution_identity(raw):
    return _digest(raw)


def _report_source_key(user_id, subject_id, epoch, source_id, field, contribution_id):
    return 'report_' + _digest([user_id, subject_id, epoch, source_id, field, contribution_id])


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
        raise ValueError('basic_info.invalid_identifier')
    return value.strip()


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('basic_info.naive_time')
    return value.astimezone(UTC)


def _value(field, value):
    if field == 'name':
        if not isinstance(value, str) or not value.strip():
            raise ValueError('basic_info.invalid_name')
        return value.strip()
    if field == 'gender':
        if isinstance(value, dict):
            value = value.get('code', value.get('value'))
        result = {'男': 'male', '女': 'female', 'male': 'male', 'female': 'female'}.get(value)
        if result is None:
            raise ValueError('basic_info.invalid_gender')
        return result
    if isinstance(value, dict):
        value = value.get('normalizedNumber', value.get('number'))
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 150:
        raise ValueError('basic_info.invalid_age')
    return int(value) if float(value).is_integer() else value


def _reliability(fact):
    if (
        fact.assertion_level is AssertionLevel.USER_CONFIRMED
        or fact.verification_status is VerificationStatus.CONFIRMED
    ):
        return 'very_high'
    if fact.sources and all(source.source_type == 'health_report' for source in fact.sources):
        return 'low'
    return 'unknown'


class ProfileBasicInfoService:
    """Only Profile assigns API reliability; report reliability is supplied by AI.

    The store owns published demographic source facts and selection in one CAS.
    Existing HealthFact rows are the compatible materialization, not an outbox
    promoted to a second authority. Immutable source provenance stays in Profile.
    """

    def __init__(self, repository, store, *, clock=None):
        self.repository = repository
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def _target(self, user_id, subject_id=None):
        subject = (
            await self.repository.get_self_subject(user_id)
            if subject_id is None
            else await self.repository.get_subject(user_id, subject_id)
        )
        if subject is not None:
            if subject.user_id != user_id or subject.status is not SubjectStatus.ACTIVE:
                raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
            return subject.subject_id, getattr(subject, 'lifecycle_epoch', 1), subject
        if subject_id is not None:
            raise BasicInfoConflict('basic_info.subject_missing')
        previous = await self.repository.list_subjects(user_id, include_archived=True)
        if any(item.kind is SubjectKind.SELF for item in previous):
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        key = 'basic_info_anchor_' + _digest(user_id)
        anchor = await self.store.create(
            {
                '_id': key,
                'recordKind': 'basic_info_anchor',
                'revision': 0,
                'userId': user_id,
                'subjectId': 'subject_basic_' + _digest(user_id)[:32],
                'lifecycleEpoch': 1,
            }
        )
        return anchor['subjectId'], anchor['lifecycleEpoch'], None

    async def reserved_subject(self, subject):
        """All existing self-creation paths reuse a reservation, if present."""
        row = await self.store.get('basic_info_anchor_' + _digest(subject.user_id))
        if row is None:
            row = await self.store.create(
                {
                    '_id': 'basic_info_anchor_' + _digest(subject.user_id),
                    'recordKind': 'basic_info_anchor',
                    'revision': 0,
                    'userId': subject.user_id,
                    'subjectId': subject.subject_id,
                    'lifecycleEpoch': 1,
                }
            )
        return subject.model_copy(update={'subject_id': row['subjectId']})

    @staticmethod
    def _scope_key(user_id, subject_id, epoch):
        return 'basic_info_scope_' + _digest([user_id, subject_id, epoch])

    async def _scope(self, user_id, subject_id, epoch, *, persist=True):
        key = self._scope_key(user_id, subject_id, epoch)
        existing = await self.store.get(key)
        if existing is not None:
            return existing
        facts = await self.repository.list_facts(user_id, subject_id, statuses=(FactStatus.ACTIVE,))
        sources, selected = {}, {}
        for fact in sorted(facts, key=lambda item: (item.effective_period.start or item.created_at, item.updated_at)):
            if fact.basic_info_lifecycle_epoch is not None:
                continue  # Managed rows need their own canonical ledger; never migrate a staged/old-epoch row.
            field = field_for_fact(fact)
            if field is None:
                continue
            operation = fact.last_request_id or fact.creation_request_id
            if operation and await self.store.find_mutation_scope(
                user_id, subject_id, _digest([operation, field]), operation
            ):
                continue  # Interrupted pre-marker manual writes already belong to their original ledger.
            try:
                value = _value(field, fact.value)
            except (TypeError, ValueError):
                continue
            key = 'legacy_' + fact.fact_id
            sources[key] = {
                'field': field,
                'value': value,
                'reliability': _reliability(fact),
                'factId': fact.fact_id,
                'active': True,
                'order': len(sources),
                'observedAt': fact.effective_period.start,
                'confirmedAt': None,
                'ageBasis': 'reported_at_report' if field == 'age' and _reliability(fact) == 'low' else None,
                'sourceRef': {'legacyFactId': fact.fact_id},
                'owner': (
                    fact.sources[0].source_id
                    if _reliability(fact) == 'low' and all(item.source_type == 'health_report' for item in fact.sources)
                    else None
                ),
            }
            selected[field] = key
        for field, key in tuple(selected.items()):
            if sources[key]['reliability'] == 'unknown':
                continue  # Protect the existing historical current, not every unrelated old unknown.
            choices = [
                (candidate, row)
                for candidate, row in sources.items()
                if row['field'] == field and row['reliability'] in RANK
            ]
            selected[field] = max(choices, key=lambda pair: (RANK[pair[1]['reliability']], pair[1]['order']))[0]
        subject = await self.repository.get_subject(user_id, subject_id)
        initial = {
            '_id': self._scope_key(user_id, subject_id, epoch),
            'recordKind': 'basic_info_scope',
            'userId': user_id,
            'subjectId': subject_id,
            'lifecycleEpoch': epoch,
            'revision': 0,
            'fieldEpochs': dict.fromkeys(FIELDS, 0),
            'blockedFields': [],
            'sources': sources,
            'selected': selected,
            'commands': {},
            'mutations': {},
            'pendingCount': 0,
            'dataRevision': 1,
            'reportPublications': {},
            'subjectCreated': subject is not None,
            'hasManagedFacts': any(
                fact.basic_info_lifecycle_epoch is not None and field_for_fact(fact) is not None for fact in facts
            ),
        }
        return await self.store.create(initial) if persist else initial

    async def register(self, user_id, operation_id, *, values, confirmed_at, subject_id=None):
        user_id, operation_id = _identifier(user_id), _identifier(operation_id)
        values = BasicInfoValuesV1.model_validate(values)
        confirmed_at = _time(confirmed_at)
        payload = {'values': values.model_dump(), 'confirmedAt': confirmed_at, 'subjectId': subject_id}
        legacy_payload_hash = _digest(payload)
        payload_hash = _digest({**payload, 'confirmedAt': _bson_time(confirmed_at)})
        command_id = 'basic_info_command_' + _digest([user_id, operation_id])
        index = await self.store.get(command_id)
        if index is None:
            target_id, epoch, subject = await self._target(user_id, subject_id)
            if subject is not None and subject.kind is not SubjectKind.SELF:
                raise BasicInfoConflict('basic_info.self_required')
            initial = await self._scope(user_id, target_id, epoch)
            index = await self.store.create(
                {
                    '_id': command_id,
                    'recordKind': 'basic_info_command_index',
                    'revision': 0,
                    'userId': user_id,
                    'operationId': operation_id,
                    'subjectId': target_id,
                    'lifecycleEpoch': epoch,
                    'payloadHash': payload_hash,
                    'baseFieldEpochs': deepcopy(initial['fieldEpochs']),
                }
            )
        if index['userId'] != user_id or (
            index['payloadHash'] != payload_hash
            and not ('baseFieldEpochs' not in index and index['payloadHash'] == legacy_payload_hash)
        ):
            raise BasicInfoConflict('basic_info.operation_conflict')
        for _ in range(32):
            state = await self._scope(user_id, index['subjectId'], index['lifecycleEpoch'])
            if command_id in state['commands']:
                return self._receipt(state, state['commands'][command_id])
            if 'baseFieldEpochs' not in index:
                raise BasicInfoConflict('basic_info.legacy_command_unverifiable')
            after = deepcopy(state)
            epochs, fields = {}, {}
            for field in FIELDS:
                base = index['baseFieldEpochs'][field]
                epochs[field] = base + 1
                if state['fieldEpochs'][field] != base:
                    fields[field] = {'status': 'superseded'}
                else:
                    after['fieldEpochs'][field] = epochs[field]
                    fields[field] = {'status': 'pending'}
            after['commands'][command_id] = {
                'commandId': command_id,
                'operationId': operation_id,
                'payloadHash': payload_hash,
                'confirmedAt': confirmed_at,
                'values': values.model_dump(),
                'fieldEpochs': epochs,
                'fields': fields,
                'status': 'registered'
                if any(item['status'] == 'pending' for item in fields.values())
                else self._status(fields),
                'errorCode': None,
            }
            committed = await self.store.cas(state, after)
            if committed is not None:
                await self._touch(committed)
                return self._receipt(committed, committed['commands'][command_id])
        raise BasicInfoUnavailable('basic_info.concurrent_registration')

    async def get(self, user_id, operation_id):
        user_id, operation_id = _identifier(user_id), _identifier(operation_id)
        command_id = 'basic_info_command_' + _digest([user_id, operation_id])
        return await self._get_command(user_id, command_id)

    async def _get_command(self, user_id, command_id):
        index = await self.store.get(command_id)
        if index is None or index['userId'] != user_id:
            return None
        state = await self.store.get(self._scope_key(user_id, index['subjectId'], index['lifecycleEpoch']))
        command = None if state is None else state['commands'].get(command_id)
        if command is None:
            return None
        view = await self.repository.get_view(user_id, index['subjectId'])
        return self._receipt(state, command).model_copy(
            update={'profile_version': view.profile_version if view else None}
        )

    async def list_pending(self, *, user_id=None, limit=100, cursor=None, operation_prefix=None):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError('basic_info.invalid_limit')
        if operation_prefix is not None:
            operation_prefix = _identifier(operation_prefix)
        after_scope, after_command = '', ''
        binding = [user_id, operation_prefix]
        if cursor is not None:
            try:
                position = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
                after_scope, after_command, selected = position
                if selected != binding or not isinstance(after_scope, str) or not isinstance(after_command, str):
                    raise ValueError('basic_info.invalid_cursor')
            except (TypeError, ValueError) as error:
                raise ValueError('basic_info.invalid_cursor') from error
        rows = await self.store.scan(user_id=user_id, after=after_scope or None, limit=limit + 2)
        result, position = [], None
        for state in rows[: limit + 1]:
            scope_id = state['_id']
            for key, command in sorted(state['commands'].items()):
                if (scope_id, key) <= (after_scope, after_command):
                    continue
                if command['status'] not in TERMINAL and (
                    operation_prefix is None or command['operationId'].startswith(operation_prefix)
                ):
                    if len(result) == limit:
                        break
                    result.append(self._receipt(state, command))
                position = scope_id, key
            else:
                continue
            break
        else:
            if len(rows) <= limit + 1:
                position = None
        encoded = (
            base64.urlsafe_b64encode(json.dumps([*position, binding]).encode()).decode().rstrip('=')
            if position is not None
            else None
        )
        return BasicInfoCommandPageV1(items=tuple(result), next_cursor=encoded)

    async def apply(self, user_id, command_id):
        user_id = _identifier(user_id)
        index = await self.store.get(command_id)
        if index is None or index['userId'] != user_id:
            raise BasicInfoConflict('basic_info.command_missing')
        key = self._scope_key(user_id, index['subjectId'], index['lifecycleEpoch'])
        for field in FIELDS:
            for _ in range(32):
                state = await self.store.get(key)
                command = state['commands'][command_id]
                if command['fields'][field]['status'] in TERMINAL:
                    break
                subject = await self.repository.get_subject(user_id, state['subjectId'])
                if subject is None and not state.get('subjectCreated'):
                    subject = HealthSubject(
                        user_id=user_id,
                        subject_id=state['subjectId'],
                        kind=SubjectKind.SELF,
                        created_at=command['confirmedAt'],
                        updated_at=command['confirmedAt'],
                    )
                    subject, _ = await self.repository.create_self_subject(subject)
                invalid = (
                    subject is None
                    or subject.subject_id != state['subjectId']
                    or subject.status is not SubjectStatus.ACTIVE
                    or getattr(subject, 'lifecycle_epoch', 1) != state['lifecycleEpoch']
                )
                after = deepcopy(state)
                after['subjectCreated'] = subject is not None or state.get('subjectCreated', False)
                item = after['commands'][command_id]
                if invalid:
                    item['fields'][field] = {'status': 'blocked'}
                    item['errorCode'] = 'basic_info.subject_inactive_or_mismatch'
                elif state['fieldEpochs'][field] != command['fieldEpochs'][field]:
                    item['fields'][field] = {'status': 'superseded'}
                else:
                    source_key = command_id + '_' + field
                    selected = state['sources'].get(state['selected'].get(field))
                    value = command['values'][field]
                    same = selected is not None and selected['value'] == value
                    fact_id = selected['factId'] if same else 'fact_basic_' + _digest(source_key)
                    source = self._source(
                        field,
                        value,
                        'very_high',
                        fact_id,
                        {'commandId': command_id, 'operationId': command['operationId']},
                        command['confirmedAt'],
                        confirmed_at=command['confirmedAt'],
                        order=len(state['sources']),
                    )
                    await self._materialize(user_id, subject, source, command_id)
                    latest_subject = await self.repository.get_subject(user_id, state['subjectId'])
                    if (
                        latest_subject is None
                        or latest_subject.status is not SubjectStatus.ACTIVE
                        or latest_subject.lifecycle_epoch != state['lifecycleEpoch']
                    ):
                        item['fields'][field] = {'status': 'blocked'}
                        item['errorCode'] = 'basic_info.subject_inactive_or_mismatch'
                    else:
                        after['sources'][source_key] = source
                        after['selected'][field] = source_key
                        after['blockedFields'] = [name for name in after['blockedFields'] if name != field]
                        item['fields'][field] = {'status': 'no_change' if same else 'applied', 'factId': fact_id}
                item['status'] = 'pending'
                committed = await self.store.cas(state, after)
                if committed is not None:
                    await self._touch(committed)
                    break
            else:
                raise BasicInfoUnavailable('basic_info.concurrent_application')
        for _ in range(32):
            state = await self.store.get(key)
            subject = await self.repository.get_subject(user_id, state['subjectId'])
            if (
                subject is not None
                and subject.status is SubjectStatus.ACTIVE
                and subject.lifecycle_epoch == state['lifecycleEpoch']
            ):
                await self._sync_display(user_id, state['subjectId'])
            await self._touch(state)
            after = deepcopy(state)
            after['commands'][command_id]['status'] = self._status(after['commands'][command_id]['fields'])
            if after == state or await self.store.cas(state, after) is not None:
                return await self._get_command(user_id, command_id)
        raise BasicInfoUnavailable('basic_info.concurrent_completion')

    async def cancel(self, user_id, command_id):
        index = await self.store.get(command_id)
        if index is None or index['userId'] != user_id:
            raise BasicInfoConflict('basic_info.command_missing')
        for _ in range(32):
            state = await self.store.get(self._scope_key(user_id, index['subjectId'], index['lifecycleEpoch']))
            after = deepcopy(state)
            command = after['commands'][command_id]
            for field, progress in command['fields'].items():
                if progress['status'] not in TERMINAL:
                    progress['status'] = 'cancelled'
                    if after['fieldEpochs'][field] == command['fieldEpochs'][field]:
                        after['fieldEpochs'][field] += 1
            command['status'] = self._status(command['fields'])
            row = await self.store.cas(state, after)
            if row is not None:
                await self._touch(row)
                return self._receipt(row, row['commands'][command_id])
        raise BasicInfoUnavailable('basic_info.concurrent_cancellation')

    @staticmethod
    def _source(field, value, reliability, fact_id, source_ref, observed_at, *, confirmed_at=None, order=0, owner=None):
        return {
            'field': field,
            'value': value,
            'reliability': reliability,
            'factId': fact_id,
            'sourceRef': deepcopy(source_ref),
            'observedAt': observed_at,
            'confirmedAt': confirmed_at,
            'active': True,
            'order': order,
            'owner': owner,
            'ageBasis': ('confirmed_at_time' if confirmed_at else 'reported_at_report') if field == 'age' else None,
        }

    async def _was_report_fact_rejected(self, fact, epoch):
        if (
            fact is None
            or fact.status is not FactStatus.RETRACTED
            or fact.basic_info_lifecycle_epoch != epoch
            or not fact.last_request_id
            or not fact.last_request_fingerprint
        ):
            return False
        field = field_for_fact(fact)
        index = await self.store.get('basic_info_mutation_' + _digest([fact.user_id, fact.last_request_id, field]))
        return bool(
            index
            and index.get('recordKind') == 'basic_info_mutation_index'
            and index.get('userId') == fact.user_id
            and index.get('subjectId') == fact.subject_id
            and index.get('lifecycleEpoch') == epoch
            and index.get('field') == field
            and index.get('operationId') == fact.last_request_id
            and index.get('payloadHash')
            == _digest([fact.subject_id, field, True, False, fact.fact_id, fact.last_request_fingerprint])
        )

    async def _materialize(self, user_id, subject, source, operation_id):
        field, value = source['field'], source['value']
        now = _time(self.clock())
        observed_at = source['observedAt']
        if isinstance(observed_at, str):
            observed_at = datetime.fromisoformat(observed_at)
        if isinstance(observed_at, datetime) and observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=ZONE)
        fact_source = FactSource(
            source_type='manual' if source['confirmedAt'] else 'health_report',
            source_id=operation_id if source['confirmedAt'] else source['owner'],
            source_revision=(source['sourceRef']['sourceRevision'] if not source['confirmedAt'] else 1),
            observed_at=observed_at,
        )
        for _ in range(32):
            existing = await self.repository.get_fact(user_id, subject.subject_id, source['factId'])
            if source['owner'] is not None:
                state = await self.store.get(self._scope_key(user_id, subject.subject_id, subject.lifecycle_epoch))
                if (state and source['factId'] in state.get('rejectedFactIds', ())) or (
                    await self._was_report_fact_rejected(existing, subject.lifecycle_epoch)
                ):
                    return None
            if existing is None:
                if await self.repository.get_deleted_fact_tombstone(user_id, subject.subject_id, source['factId']):
                    raise BasicInfoConflict('basic_info.fact_deleted')
                break
            sources = existing.sources
            if fact_source not in sources:
                sources = (*sources, fact_source)
            confirmed = bool(source['confirmedAt'])
            changes = {
                'sources': sources,
                'status': FactStatus.ACTIVE,
                'basic_info_lifecycle_epoch': subject.lifecycle_epoch,
            }
            if confirmed:
                changes.update(
                    assertion_level=AssertionLevel.USER_CONFIRMED, verification_status=VerificationStatus.CONFIRMED
                )
            if all(getattr(existing, key) == item for key, item in changes.items()):
                return existing
            updated = existing.model_copy(update={**changes, 'version': existing.version + 1, 'updated_at': now})
            stored = await self.repository.replace_fact(
                updated, expected_version=existing.version, allowed_statuses=(existing.status,)
            )
            if stored is not None:
                return stored
        else:
            raise BasicInfoUnavailable('basic_info.concurrent_materialization')
        fact = HealthFact(
            fact_id=source['factId'],
            user_id=user_id,
            subject_id=subject.subject_id,
            fact_type={'name': 'demographic.name', 'gender': 'demographic.sex', 'age': 'demographic.age'}[field],
            category=FactCategory.DEMOGRAPHIC,
            display_name={'name': '姓名', 'gender': '性别', 'age': '年龄'}[field],
            value_type=FactValueType.QUANTITY if field == 'age' else FactValueType.TEXT,
            value={'number': value, 'unit': 'years'} if field == 'age' else value,
            assertion_level=(AssertionLevel.USER_CONFIRMED if source['confirmedAt'] else AssertionLevel.OBSERVED),
            verification_status=(
                VerificationStatus.CONFIRMED if source['confirmedAt'] else VerificationStatus.VERIFIED_SOURCE
            ),
            confidence=1.0,
            effective_period=EffectivePeriod(start=observed_at),
            status=FactStatus.ACTIVE,
            sources=(fact_source,),
            created_at=now,
            updated_at=now,
            creation_request_id=operation_id,
            creation_request_slot=field,
            creation_request_fingerprint=_digest(source),
            basic_info_lifecycle_epoch=subject.lifecycle_epoch,
        )
        stored, _ = await self.repository.create_fact(fact)
        if stored.creation_request_fingerprint != fact.creation_request_fingerprint:
            raise BasicInfoConflict('basic_info.fact_identity_conflict')
        return stored

    async def ingest_report(
        self,
        user_id,
        subject_id,
        *,
        source_id,
        source_revision,
        fields,
        publication_revision=None,
        lifecycle_epoch=None,
        explicit_retirements=(),
    ):
        """Consume validated fixed Document v2 demographic contributions."""
        return await self._ingest_report(
            user_id,
            subject_id,
            source_id=source_id,
            source_revision=source_revision,
            fields=fields,
            publication_revision=publication_revision,
            lifecycle_epoch=lifecycle_epoch,
            explicit_retirements=explicit_retirements,
            replay_fixed_plan=False,
        )

    async def _fixed_report_action_is_committed(
        self, user_id, subject_id, *, source_id, source_revision, fields, publication_revision, lifecycle_epoch
    ):
        state = await self.store.get(self._scope_key(user_id, subject_id, lifecycle_epoch))
        prior = (state or {}).get('reportPublications', {}).get(_digest(source_id))
        if prior is None or prior['revision'] < publication_revision:
            return False
        if prior['revision'] == publication_revision and prior['payloadHash'] != _digest(
            [source_revision, tuple(fields)]
        ):
            raise BasicInfoConflict('basic_info.publication_content_conflict')
        return True

    async def _resume_fixed_report_action(
        self,
        user_id,
        subject_id,
        *,
        source_id,
        source_revision,
        fields,
        publication_revision,
        lifecycle_epoch,
        explicit_retirements,
    ):
        return await self._ingest_report(
            user_id,
            subject_id,
            source_id=source_id,
            source_revision=source_revision,
            fields=fields,
            publication_revision=publication_revision,
            lifecycle_epoch=lifecycle_epoch,
            explicit_retirements=explicit_retirements,
            replay_fixed_plan=True,
        )

    async def _ingest_report(
        self,
        user_id,
        subject_id,
        *,
        source_id,
        source_revision,
        fields,
        publication_revision,
        lifecycle_epoch,
        explicit_retirements,
        replay_fixed_plan,
    ):
        user_id, subject_id = _identifier(user_id), _identifier(subject_id)
        source_id = _identifier(source_id)
        if type(source_revision) is not int or source_revision < 1:
            raise ValueError('basic_info.invalid_source_revision')
        if not isinstance(fields, (list, tuple)) or len(fields) > 1000:
            raise ValueError('basic_info.invalid_fields')
        _, epoch, subject = await self._target(user_id, subject_id)
        if lifecycle_epoch is not None and (type(lifecycle_epoch) is not int or lifecycle_epoch != epoch):
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        if publication_revision is not None and (type(publication_revision) is not int or publication_revision < 1):
            raise ValueError('basic_info.invalid_publication_revision')
        fields = tuple(deepcopy(fields))
        content = [source_revision, fields]
        if not isinstance(explicit_retirements, (tuple, list)) or len(explicit_retirements) > 1000:
            raise ValueError('basic_info.invalid_retirements')
        retirements = tuple(BasicInfoReportRetirement.model_validate(row) for row in explicit_retirements)
        if retirements:
            if lifecycle_epoch is None or publication_revision is None or publication_revision < 2:
                raise ValueError('basic_info.retirement_requires_fixed_publication')
            if len({row.contribution_id for row in retirements}) != len(retirements):
                raise ValueError('basic_info.invalid_retirements')
            if not replay_fixed_plan:
                content.append([row.model_dump(mode='json', by_alias=True) for row in retirements])
        payload_hash = _digest(content)
        owner_key = _digest(source_id)
        prepared = []
        for raw in fields:
            raw = dict(raw)
            evidence = BasicInfoEvidenceV1.model_validate(
                {
                    key: raw[key]
                    for key in ('field', 'value', 'sourceReliability', 'observedAt', 'reason', 'evidence')
                    if key in raw
                }
            )
            if raw.get('subjectStatus', 'single') != 'single':
                continue
            value = _value(evidence.field, evidence.value)
            quote = evidence.evidence.get('quote')
            if not isinstance(quote, str) or not quote.strip():
                raise ValueError('basic_info.missing_evidence')
            ref = {key: raw[key] for key in raw if key not in {'value', 'evidence'}}
            ref.update({'sourceId': source_id, 'sourceRevision': source_revision, 'evidence': evidence.evidence})
            identity = raw.get('contributionId') or _digest([source_revision, raw])
            source_key = _report_source_key(user_id, subject_id, epoch, source_id, evidence.field, identity)
            source = self._source(
                evidence.field,
                value,
                evidence.source_reliability,
                'fact_basic_' + _digest(source_key),
                ref,
                evidence.observed_at,
                owner=source_id,
            )
            source['identityHash'] = report_contribution_identity(raw)
            prepared.append((source_key, source))
        retirement_hashes = {
            _report_source_key(user_id, subject_id, epoch, source_id, row.field, row.contribution_id): row.identity_hash
            for row in retirements
        }
        await self._scope(user_id, subject_id, epoch)
        rejected_during_materialization = set()
        for _ in range(32):
            state = await self._scope(user_id, subject_id, epoch)
            prior = state.get('reportPublications', {}).get(owner_key)
            revision = publication_revision or (prior['revision'] + 1 if prior else 1)
            if prior and publication_revision is None and prior['payloadHash'] == payload_hash:
                await self._reconcile_report_facts(state)
                await self._touch(state)
                return await self.read(user_id, subject_id=subject_id)
            if prior and revision <= prior['revision']:
                if revision == prior['revision'] and prior['payloadHash'] != payload_hash:
                    raise BasicInfoConflict('basic_info.publication_content_conflict')
                await self._reconcile_report_facts(state)
                await self._touch(state)
                return await self.read(user_id, subject_id=subject_id)
            after = deepcopy(state)
            incoming = dict(prepared)
            source_order = {key: row['order'] for key, row in after['sources'].items()}
            for key, _ in prepared:
                source_order.setdefault(key, len(source_order))
            prior_keys = set(prior['activeKeys']) if prior else set()
            removed = prior_keys - incoming.keys()
            explicitly_retired = removed.intersection(retirement_hashes)
            if explicitly_retired:
                # Current explicit retirement is a subtraction from the fixed prior inventory.
                # A cumulative historical marker alone must not qualify an ordinary replacement.
                if set(incoming) != prior_keys - explicitly_retired:
                    raise BasicInfoConflict('basic_info.retirement_inventory_conflict')
                if any(
                    state['sources'][key].get('identityHash') != retirement_hashes[key] for key in explicitly_retired
                ):
                    raise BasicInfoConflict('basic_info.retirement_identity_conflict')
            for key, source in prepared:
                previous = state['sources'].get(key)
                if previous is not None and previous.get('identityHash') != source['identityHash']:
                    raise BasicInfoConflict('basic_info.contribution_content_conflict')
            rejected = set(state.get('rejectedFactIds', ())) | rejected_during_materialization
            # A legacy governance action can have deactivated a candidate while
            # its immutable Document contribution is still in the prior inventory.
            rejected.update(
                state['sources'][key]['factId']
                for key in prior_keys
                if not state['sources'][key]['active'] and not state['sources'][key].get('deleted')
            )
            eligible = {}
            for key, source in prepared:
                previous = state['sources'].get(key, source)
                if previous.get('deleted') or source['factId'] in rejected:
                    continue
                if key in state['sources'] and not previous['active']:
                    fact = await self.repository.get_fact(user_id, subject_id, source['factId'])
                    if await self._was_report_fact_rejected(fact, epoch):
                        rejected.add(source['factId'])
                        continue
                eligible[key] = source
            if rejected:
                after['rejectedFactIds'] = sorted(rejected)
            replacements = {}
            for field, selected_key in state['selected'].items():
                if selected_key not in removed or selected_key in explicitly_retired:
                    continue
                old = state['sources'][selected_key]
                choices = [
                    (key, row)
                    for key, row in eligible.items()
                    if row['field'] == field and row['factId'] not in rejected
                ]
                if not choices:
                    continue  # A true field/contribution retirement; reselect valid alternatives.
                best_key, best = max(choices, key=lambda pair: (RANK[pair[1]['reliability']], -source_order[pair[0]]))
                previous_rank = RANK.get(old['reliability'], 4)
                if RANK[best['reliability']] < previous_rank or (
                    RANK[best['reliability']] == previous_rank and best['value'] != old['value']
                ):
                    raise BasicInfoConflict('basic_info.unqualified_source_replacement')
                replacements[field] = best_key
            for key in removed:
                after['sources'][key]['active'] = False
            for key, source in prepared:
                row = after['sources'].get(key, source)
                active = key in eligible and row['factId'] not in rejected
                if active and await self._materialize(user_id, subject, row, key) is None:
                    # A concurrent user rejection wins against the in-flight
                    # materialization; rebuild choices from the latest scope.
                    rejected_during_materialization.add(row['factId'])
                    break
                if key not in after['sources']:
                    row = deepcopy(row)
                    row['order'] = len(after['sources'])
                    after['sources'][key] = row
                row['active'] = active
            if rejected_during_materialization - rejected:
                continue
            for field, key in replacements.items():
                if field not in after['blockedFields']:
                    after['selected'][field] = key
            self._reselect(after)
            for key in sorted(incoming, key=lambda item: (after['sources'][item]['order'], item)):
                source = after['sources'][key]
                if source['active'] and not source.get('deleted'):
                    self._consider(after, key)
            current_subject = await self.repository.get_subject(user_id, subject_id)
            if (
                current_subject is None
                or current_subject.status is not SubjectStatus.ACTIVE
                or current_subject.lifecycle_epoch != epoch
            ):
                raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
            after.setdefault('reportPublications', {})[owner_key] = {
                'revision': revision,
                'payloadHash': payload_hash,
                'activeKeys': list(incoming),
            }
            committed = await self.store.cas(state, after)
            if committed is not None:
                await self._reconcile_report_facts(committed)
                await self._touch(committed)
                return await self.read(user_id, subject_id=subject_id)
        raise BasicInfoUnavailable('basic_info.concurrent_report')

    @staticmethod
    def _consider(state, key):
        incoming = state['sources'][key]
        field = incoming['field']
        if field in state['blockedFields'] or not ProfileBasicInfoService._is_selectable(state, incoming):
            return
        selected = state['sources'].get(state['selected'].get(field))
        if selected is None or not ProfileBasicInfoService._is_selectable(state, selected):
            state['selected'][field] = key
        elif selected['reliability'] != 'unknown' and RANK[incoming['reliability']] > RANK[selected['reliability']]:
            state['selected'][field] = key

    async def withdraw_report(
        self,
        user_id,
        subject_id,
        *,
        source_id,
        contribution_ids=None,
        publication_revision=None,
        lifecycle_epoch=None,
    ):
        _, epoch, _ = await self._target(user_id, subject_id)
        if lifecycle_epoch is not None and (type(lifecycle_epoch) is not int or lifecycle_epoch != epoch):
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        if publication_revision is not None and (type(publication_revision) is not int or publication_revision < 1):
            raise ValueError('basic_info.invalid_publication_revision')
        wanted = None if contribution_ids is None else frozenset(contribution_ids)
        owner_key = _digest(source_id)
        digest = _digest(['withdraw', sorted(wanted) if wanted is not None else None])
        for _ in range(32):
            state = await self._scope(user_id, subject_id, epoch)
            prior = state.get('reportPublications', {}).get(owner_key)
            revision = publication_revision or (prior['revision'] + 1 if prior else 1)
            if prior and revision <= prior['revision']:
                if revision == prior['revision'] and prior['payloadHash'] != digest:
                    raise BasicInfoConflict('basic_info.publication_content_conflict')
                await self._reconcile_report_facts(state)
                await self._touch(state)
                return await self.read(user_id, subject_id=subject_id)
            after = deepcopy(state)
            removed = set()
            for key, source in after['sources'].items():
                if source.get('owner') == source_id and (
                    wanted is None or source['sourceRef'].get('contributionId') in wanted
                ):
                    source['active'] = False
                    removed.add(key)
            self._reselect(after)
            # Inventory membership survives a Profile-only rejection. A partial
            # withdrawal removes its own contributions, including inactive ones.
            prior_keys = (
                prior['activeKeys']
                if prior
                else [
                    key
                    for key, source in state['sources'].items()
                    if source['active'] and source.get('owner') == source_id
                ]
            )
            after.setdefault('reportPublications', {})[owner_key] = {
                'revision': revision,
                'payloadHash': digest,
                'activeKeys': [key for key in prior_keys if key not in removed],
            }
            committed = await self.store.cas(state, after)
            if committed is not None:
                await self._reconcile_report_facts(committed)
                await self._touch(committed)
                return await self.read(user_id, subject_id=subject_id)
        raise BasicInfoUnavailable('basic_info.concurrent_withdrawal')

    async def _reconcile_report_facts(self, state):
        """Retire compatible rows only after canonical withdrawal is committed.

        An independent API confirmation can share a same-valued physical row;
        its active source keeps that row alive. CAS prevents a concurrent manual
        source append from being overwritten by this repair.
        """
        fact_ids = {row['factId'] for row in state['sources'].values() if row.get('owner')}
        for fact_id in fact_ids:
            for _ in range(32):
                fact = await self.repository.get_fact(state['userId'], state['subjectId'], fact_id)
                if fact is None or fact.status is not FactStatus.ACTIVE:
                    break
                if any(row.source_type == 'manual' for row in fact.sources):
                    break
                current = await self.store.get(state['_id'])
                if fact_id in current.get('rejectedFactIds', ()):
                    break  # The fenced user operation owns the physical retraction.
                if any(row['active'] and row['factId'] == fact_id for row in current['sources'].values()):
                    break
                stored = await self.repository.replace_fact(
                    fact.model_copy(
                        update={
                            'status': FactStatus.RETRACTED,
                            'version': fact.version + 1,
                            'updated_at': _time(self.clock()),
                        }
                    ),
                    expected_version=fact.version,
                    allowed_statuses=(FactStatus.ACTIVE,),
                )
                if stored is not None:
                    break
            else:
                raise BasicInfoUnavailable('basic_info.concurrent_source_retirement')

    @staticmethod
    def _is_selectable(state, source):
        return (
            source['active'] and not source.get('deleted') and source['factId'] not in state.get('rejectedFactIds', ())
        )

    @staticmethod
    def _reselect(state):
        for field in FIELDS:
            current = state['sources'].get(state['selected'].get(field))
            if current and ProfileBasicInfoService._is_selectable(state, current):
                continue
            state['selected'].pop(field, None)
            candidates = sorted(
                state['sources'].items(), key=lambda row: (-RANK.get(row[1]['reliability'], 4), row[1]['order'])
            )
            for key, source in candidates:
                if (
                    source['field'] == field
                    and ProfileBasicInfoService._is_selectable(state, source)
                    and field not in state['blockedFields']
                ):
                    state['selected'][field] = key
                    break

    async def guard_mutation(
        self,
        user_id,
        subject_id,
        field,
        operation_id,
        *,
        deleted=False,
        erase=False,
        fact=None,
        request_fingerprint=None,
    ):
        """Bind a manual operation once, before its physical fact CAS.

        The independent index is found before resolving the current lifecycle.
        A lost acknowledgement can therefore never turn an old operation into
        a new confirmation after a subject is archived and reactivated.
        """
        user_id, subject_id = _identifier(user_id), _identifier(subject_id)
        operation_id = _identifier(operation_id)
        if field not in FIELDS:
            raise ValueError('basic_info.invalid_field')
        index_key = 'basic_info_mutation_' + _digest([user_id, operation_id, field])
        index = await self.store.get(index_key)
        payload_hash = _digest(
            [
                subject_id,
                field,
                deleted,
                erase,
                fact.fact_id if fact else None,
                request_fingerprint,
            ]
        )
        if index is None:
            prior_scope = await self.store.find_mutation_scope(
                user_id, subject_id, _digest([operation_id, field]), operation_id
            )
            recovered = fact is not None and fact.last_request_id == operation_id
            if prior_scope is not None:
                if not recovered or (
                    request_fingerprint is not None and fact.last_request_fingerprint != request_fingerprint
                ):
                    raise BasicInfoConflict('basic_info.legacy_mutation_unverifiable')
                epoch = prior_scope['lifecycleEpoch']
                field_epoch = prior_scope['mutations'][_digest([operation_id, field])]['epoch']
                base_epoch = field_epoch - 1
                source = prior_scope['sources'].get('manual_' + _digest([operation_id, field]))
                confirmed_at = source['confirmedAt'] if source else fact.updated_at
            elif recovered:
                # A physical retry without its original acceptance context is
                # not a new confirmation, even if the subject is active again.
                raise BasicInfoConflict('basic_info.legacy_mutation_unverifiable')
            else:
                _, epoch, _ = await self._target(user_id, subject_id)
                initial = await self._scope(user_id, subject_id, epoch)
                base_epoch = initial['fieldEpochs'][field]
                field_epoch = base_epoch + 1
                confirmed_at = _time(self.clock())
            index = await self.store.create(
                {
                    '_id': index_key,
                    'recordKind': 'basic_info_mutation_index',
                    'revision': 0,
                    'userId': user_id,
                    'subjectId': subject_id,
                    'lifecycleEpoch': epoch,
                    'operationId': operation_id,
                    'field': field,
                    'payloadHash': payload_hash,
                    'confirmedAt': confirmed_at,
                    'baseFieldEpoch': base_epoch,
                    'fieldEpoch': field_epoch,
                }
            )
        if index['payloadHash'] != payload_hash or index['subjectId'] != subject_id:
            raise BasicInfoConflict('basic_info.operation_conflict')
        await self._validate_mutation_fence(index)
        key = _digest([operation_id, field])
        for _ in range(32):
            state = await self._scope(user_id, subject_id, index['lifecycleEpoch'])
            if key in state['mutations']:
                return {**index, 'fieldEpoch': state['mutations'][key]['epoch']}
            if 'baseFieldEpoch' not in index or state['fieldEpochs'][field] != index['baseFieldEpoch']:
                raise BasicInfoConflict('basic_info.mutation_superseded')
            after = deepcopy(state)
            after['fieldEpochs'][field] = index['fieldEpoch']
            after['mutations'][key] = {
                'field': field,
                'operationId': operation_id,
                'epoch': after['fieldEpochs'][field],
            }
            if deleted:
                selected = after['sources'].get(after['selected'].get(field))
                if fact is None or selected is None or selected['factId'] == fact.fact_id:
                    after['blockedFields'] = sorted(set([*after['blockedFields'], field]))
                    after['selected'].pop(field, None)
                if fact is not None:
                    if not erase:
                        after['rejectedFactIds'] = sorted({*after.get('rejectedFactIds', ()), fact.fact_id})
                    for source in after['sources'].values():
                        if source['factId'] == fact.fact_id:
                            source['active'] = False
                            if erase:
                                source['deleted'] = True
                                source['value'] = None
                                source['sourceRef'] = {'deletedFactId': fact.fact_id}
            await self._validate_mutation_fence(index)
            committed = await self.store.cas(state, after)
            if committed is not None:
                return {**index, 'fieldEpoch': committed['mutations'][key]['epoch']}
        raise BasicInfoUnavailable('basic_info.concurrent_governance')

    async def _validate_mutation_fence(self, fence):
        subject = await self.repository.get_subject(fence['userId'], fence['subjectId'])
        if (
            subject is None
            or subject.status is not SubjectStatus.ACTIVE
            or subject.lifecycle_epoch != fence['lifecycleEpoch']
        ):
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        return subject

    async def _manual_fence(self, user_id, subject_id, field, operation_id, fence=None):
        saved = await self.store.get('basic_info_mutation_' + _digest([user_id, operation_id, field]))
        if saved is None or saved['subjectId'] != subject_id:
            raise BasicInfoConflict('basic_info.mutation_missing')
        if fence is not None and (
            any(saved[key] != fence[key] for key in ('userId', 'subjectId', 'lifecycleEpoch', 'payloadHash'))
            or _bson_time(saved['confirmedAt']) != _bson_time(fence['confirmedAt'])
        ):
            raise BasicInfoConflict('basic_info.operation_conflict')
        await self._validate_mutation_fence(saved)
        state = await self._scope(user_id, subject_id, saved['lifecycleEpoch'])
        mutation = state['mutations'].get(_digest([operation_id, field]))
        if mutation is None:
            raise BasicInfoConflict('basic_info.mutation_missing')
        return {**saved, 'fieldEpoch': mutation['epoch']}

    async def accept_manual_fact(self, fact, operation_id, *, fence=None):
        field = field_for_fact(fact)
        if field is None:
            return
        fence = await self._manual_fence(fact.user_id, fact.subject_id, field, operation_id, fence)
        if fact.basic_info_lifecycle_epoch != fence['lifecycleEpoch']:
            raise BasicInfoConflict('basic_info.fact_lifecycle_mismatch')
        key = 'manual_' + _digest([operation_id, field])
        for _ in range(32):
            state = await self._scope(fact.user_id, fact.subject_id, fence['lifecycleEpoch'])
            if key in state['sources']:
                return
            if fence['fieldEpoch'] != state['fieldEpochs'][field]:
                return
            after = deepcopy(state)
            source = self._source(
                field,
                _value(field, fact.value),
                'very_high',
                fact.fact_id,
                {'factId': fact.fact_id, 'operationId': operation_id},
                fact.effective_period.start,
                confirmed_at=fence['confirmedAt'],
                order=len(state['sources']),
            )
            after['sources'][key] = source
            after['selected'][field] = key
            after['blockedFields'] = [name for name in after['blockedFields'] if name != field]
            await self._validate_mutation_fence(fence)
            if await self.store.cas(state, after) is not None:
                return
        raise BasicInfoUnavailable('basic_info.concurrent_governance')

    async def accept_manual_name(self, user_id, subject_id, value, operation_id, *, fence=None):
        if value is None:
            return
        fence = await self._manual_fence(user_id, subject_id, 'name', operation_id, fence)
        subject = await self._validate_mutation_fence(fence)
        state = await self._scope(user_id, subject_id, fence['lifecycleEpoch'])
        if fence['fieldEpoch'] != state['fieldEpochs']['name']:
            return
        source = self._source(
            'name',
            _value('name', value),
            'very_high',
            'fact_basic_' + _digest([user_id, subject_id, fence['lifecycleEpoch'], operation_id, 'name']),
            {'operationId': operation_id},
            fence['confirmedAt'],
            confirmed_at=fence['confirmedAt'],
        )
        fact = await self._materialize(user_id, subject, source, operation_id)
        await self.accept_manual_fact(fact, operation_id, fence=fence)

    async def read(self, user_id, *, subject_id=None, as_of=None):
        user_id = _identifier(user_id)
        as_of = _time(as_of or self.clock())
        subject = (
            await self.repository.get_self_subject(user_id)
            if subject_id is None
            else await self.repository.get_subject(user_id, subject_id)
        )
        empty = {field: BasicInfoCurrentFieldV2() for field in FIELDS}
        if subject is None:
            return BasicInfoSnapshotV2(user_id=user_id, as_of=as_of, fields=empty)
        if subject.user_id != user_id or subject.status is not SubjectStatus.ACTIVE:
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        epoch = getattr(subject, 'lifecycle_epoch', 1)
        state = await self.store.get(self._scope_key(user_id, subject.subject_id, epoch))
        persisted = state is not None
        if state is None:
            state = await self._scope(user_id, subject.subject_id, epoch, persist=False)
        for field, key in state['selected'].items():
            source = state['sources'][key]
            if not source['active'] or field in state['blockedFields']:
                continue
            empty[field] = BasicInfoCurrentFieldV2(
                value=source['value'],
                source_reliability=source['reliability'],
                observed_at=source['observedAt'],
                confirmed_at=source['confirmedAt'],
                fact_id=source['factId'],
                source_ref=source['sourceRef'],
                protected=source['reliability'] == 'unknown',
                governance_epoch=state['fieldEpochs'][field],
                age_basis=source.get('ageBasis'),
                as_of=as_of if field == 'age' else None,
            )
        age = empty['age']
        if 'age' not in state['blockedFields']:
            facts = await self.repository.list_facts(user_id, subject.subject_id, statuses=(FactStatus.ACTIVE,))
            for fact in sorted(facts, key=lambda item: item.updated_at, reverse=True):
                if (
                    not fact_in_lifecycle(fact, epoch)
                    or fact.fact_type not in BIRTH_TYPES
                    or _reliability(fact) != 'very_high'
                ):
                    continue
                try:
                    birth = date.fromisoformat(str(fact.value)[:10])
                except ValueError:
                    continue
                if (
                    age.confirmed_at is not None
                    and self._age(birth, age.confirmed_at.astimezone(ZONE).date()) != age.value
                ):
                    empty['age'] = age.model_copy(
                        update={
                            'source_ref': {
                                **(age.source_ref or {}),
                                'birthDateDecision': 'conflict',
                                'birthFactId': fact.fact_id,
                                'birthFactVersion': fact.version,
                                'birthDateComparedAt': age.confirmed_at,
                                'birthDateNotUsedReason': 'age_mismatch_at_confirmation',
                            }
                        }
                    )
                    break
                value = self._age(birth, as_of.astimezone(ZONE).date())
                if value is not None:
                    empty['age'] = age.model_copy(
                        update={
                            'value': value,
                            'age_basis': 'dob_derived',
                            'source_reliability': 'very_high',
                            'fact_id': age.fact_id if age.confirmed_at else fact.fact_id,
                            'as_of': as_of,
                            'source_ref': {
                                **(age.source_ref or {}),
                                'birthFactId': fact.fact_id,
                                'birthFactVersion': fact.version,
                                'birthDateDecision': 'used',
                            },
                        }
                    )
                    break
        return BasicInfoSnapshotV2(
            user_id=user_id,
            subject_id=subject.subject_id,
            lifecycle_epoch=epoch,
            revision=state.get('dataRevision', state['revision']) if persisted or state.get('hasManagedFacts') else 0,
            as_of=as_of,
            fields=empty,
            sources=tuple(
                BasicInfoSourceV2(
                    source_key=key,
                    field=source['field'],
                    value=source['value'],
                    source_reliability=source['reliability'],
                    observed_at=source['observedAt'],
                    confirmed_at=source['confirmedAt'],
                    fact_id=source['factId'],
                    source_ref=source['sourceRef'],
                    protected=source['reliability'] == 'unknown',
                    governance_epoch=state['fieldEpochs'][source['field']],
                    active=source['active'],
                    current=state['selected'].get(source['field']) == key and source['active'],
                    deleted=source.get('deleted', False),
                    age_basis=source.get('ageBasis'),
                    as_of=as_of if source['field'] == 'age' else None,
                )
                for key, source in state['sources'].items()
            ),
        )

    async def select_facts(self, user_id, subject_id, facts, *, as_of=None, snapshot=None):
        """Pure source lifecycle fencing followed by current-demographic selection."""
        snapshot = snapshot or await self.read(user_id, subject_id=subject_id, as_of=as_of)
        facts = tuple(fact for fact in facts if fact_in_lifecycle(fact, snapshot.lifecycle_epoch))
        if not snapshot.revision:
            return facts
        selected = {item.fact_id for item in snapshot.fields.values() if item.fact_id}
        birth = snapshot.fields['age'].source_ref or {}
        if birth.get('birthFactId'):
            selected.add(birth['birthFactId'])
        return tuple(
            fact
            for fact in facts
            if fact.status is not FactStatus.ACTIVE
            or (field_for_fact(fact) is None and fact.fact_type not in BIRTH_TYPES)
            or fact.fact_id in selected
        )

    async def list_current_fact_page(self, user_id, subject_id, *, snapshot, **query):
        """Refill after hiding unselected active demographics; never emit empty continuation pages."""
        target = query['limit']
        result = []
        while len(result) < target:
            batch = await self.repository.list_fact_page(user_id, subject_id, **query)
            result.extend(await self.select_facts(user_id, subject_id, batch, snapshot=snapshot))
            if len(batch) < query['limit']:
                break
            last = batch[-1]
            query['after'] = (last.effective_period.start, last.fact_id)
        return tuple(result[:target])

    async def fact_reliabilities(self, user_id, subject_id):
        subject = await self.repository.get_subject(user_id, subject_id)
        if subject is None:
            return {}
        state = await self.store.get(self._scope_key(user_id, subject_id, getattr(subject, 'lifecycle_epoch', 1)))
        result = {}
        if state:
            for source in state['sources'].values():
                if not source['active']:
                    continue
                current = result.get(source['factId'])
                if current is None or RANK.get(source['reliability'], 4) > RANK.get(current, 4):
                    result[source['factId']] = source['reliability']
        return result

    @staticmethod
    def _age(birth, today):
        return (
            None if birth > today else today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
        )

    async def _sync_display(self, user_id, subject_id):
        # Public subject reads project the canonical selection. A delayed mirror
        # must never overwrite a newer manual label using a freshly read version.
        return None

    async def project_subject(self, subject):
        if subject.status is not SubjectStatus.ACTIVE:
            return subject
        state = await self.store.get(self._scope_key(subject.user_id, subject.subject_id, subject.lifecycle_epoch))
        if state:
            source = state['sources'].get(state['selected'].get('name'))
            if source and source['active'] and source['confirmedAt'] and 'name' not in state['blockedFields']:
                return subject.model_copy(update={'display_name': source['value']})
        return subject

    async def _touch(self, state):
        subject = await self.repository.get_subject(state['userId'], state['subjectId'])
        if (
            subject is None
            or subject.status is not SubjectStatus.ACTIVE
            or subject.lifecycle_epoch != state['lifecycleEpoch']
        ):
            return None
        return await self.repository.synchronize_basic_info_view(
            state['userId'],
            subject,
            state.get('dataRevision', 0),
        )

    async def publish_governance(self, user_id, subject_id):
        subject = await self.repository.get_subject(user_id, subject_id)
        if subject is None:
            return None
        state = await self.store.get(self._scope_key(user_id, subject_id, subject.lifecycle_epoch))
        return None if state is None else await self._touch(state)

    @staticmethod
    def _status(fields):
        statuses = {item['status'] for item in fields.values()}
        if 'pending' in statuses:
            return 'pending'
        for status in ('blocked', 'cancelled', 'superseded'):
            if status in statuses:
                return status
        return 'no_change' if statuses == {'no_change'} else 'applied'

    @staticmethod
    def _receipt(state, command):
        return BasicInfoCommandReceiptV1(
            user_id=state['userId'],
            subject_id=state['subjectId'],
            lifecycle_epoch=state['lifecycleEpoch'],
            command_id=command['commandId'],
            operation_id=command['operationId'],
            field_epochs=command['fieldEpochs'],
            payload_hash=command['payloadHash'],
            confirmed_at=command['confirmedAt'],
            status=command['status'],
            fields=command['fields'],
            profile_version=None,
            error_code=command['errorCode'],
        )
