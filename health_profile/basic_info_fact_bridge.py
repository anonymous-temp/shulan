"""Publish existing Profile source facts without turning replay into user governance."""

from copy import deepcopy
from datetime import UTC

from .basic_info import _digest
from .basic_info import _reliability
from .basic_info import _value
from .basic_info import field_for_fact
from .basic_info_store import BasicInfoConflict
from .basic_info_store import BasicInfoUnavailable
from .basic_info_store import _bson_time
from .models import AssertionLevel
from .models import FactStatus
from .models import SubjectStatus
from .models import VerificationStatus


def _utc(value):
    if value is None:
        return None
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).astimezone(UTC)


class ProfileBasicInfoFactBridge:
    """A private provider adapter for legacy writer and formal source effects.

    Source intent is persisted before materialization. Neither admission nor
    replay advances a manual field epoch. Physical facts retain real provenance.
    """

    def __init__(self, basic_info):
        self.basic_info = basic_info
        self.repository = basic_info.repository
        self.store = basic_info.store

    @staticmethod
    def _key(user_id, operation_id, field):
        return 'basic_info_fact_effect_' + _digest([user_id, operation_id, field])

    async def prepare(
        self,
        fact,
        subject,
        operation_id,
        *,
        admitted_at,
        effect='upsert',
        source_type=None,
        source_id=None,
        publication_revision=None,
        allow_new=True,
        recovering=False,
    ):
        field = field_for_fact(fact)
        if field is None or (effect == 'upsert' and fact.status is not FactStatus.ACTIVE):
            return None
        key = self._key(fact.user_id, operation_id, field)
        fingerprint = _digest(
            {
                'subjectId': fact.subject_id,
                'field': field,
                'value': fact.value,
                'effectivePeriod': {
                    'start': _bson_time(fact.effective_period.start),
                    'end': _bson_time(fact.effective_period.end),
                },
                'assertionLevel': fact.assertion_level.value,
                'verificationStatus': fact.verification_status.value,
                'sources': [
                    {**item.model_dump(mode='python'), 'observed_at': _bson_time(item.observed_at)}
                    for item in fact.sources
                ],
                'effect': effect,
                'sourceType': source_type,
                'sourceId': source_id,
                'publicationRevision': publication_revision,
            }
        )
        previous = await self.store.get(key)
        if previous is not None:
            if previous['payloadHash'] != fingerprint:
                raise BasicInfoConflict('basic_info.source_effect_conflict')
            return previous
        if not allow_new:
            return None  # A completed historical event is not a new confirmation.
        if fact.user_id != subject.user_id or fact.subject_id != subject.subject_id:
            raise BasicInfoConflict('basic_info.source_scope_mismatch')
        epoch = fact.basic_info_lifecycle_epoch or subject.lifecycle_epoch
        current = await self.repository.get_subject(fact.user_id, fact.subject_id)
        if current is None or current.status is not SubjectStatus.ACTIVE:
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        if fact.basic_info_lifecycle_epoch is None and epoch > 1:
            started = _utc(getattr(current, 'lifecycle_started_at', None))
            if started is None or admitted_at is None or _utc(admitted_at) < started:
                raise BasicInfoConflict('basic_info.source_epoch_unproven')
        if current.lifecycle_epoch != epoch and fact.basic_info_lifecycle_epoch is None:
            raise BasicInfoConflict('basic_info.subject_inactive_or_mismatch')
        state = await self.basic_info._scope(fact.user_id, fact.subject_id, epoch)
        sources = tuple(fact.sources)
        origin = next(
            (
                item
                for item in sources
                if (source_type is None or item.source_type == source_type)
                and (source_id is None or item.source_id == source_id)
            ),
            sources[0],
        )
        confirmed = (
            fact.assertion_level is AssertionLevel.USER_CONFIRMED
            or fact.verification_status is VerificationStatus.CONFIRMED
        )
        confirmed_at = (
            _utc(origin.observed_at or fact.effective_period.start)
            if confirmed and origin.source_type == 'record'
            else _utc(origin.observed_at)
            if confirmed and origin.source_type == 'manual'
            else None
        )
        source_key = 'existing_fact_' + _digest(
            [
                fact.user_id,
                fact.subject_id,
                epoch,
                fact.fact_id,
                source_type or origin.source_type,
                source_id or origin.source_id,
            ]
        )
        row = {
            '_id': key,
            'recordKind': 'basic_info_fact_effect',
            'revision': 0,
            'userId': fact.user_id,
            'subjectId': fact.subject_id,
            'lifecycleEpoch': epoch,
            'operationId': operation_id,
            'payloadHash': fingerprint,
            'field': field,
            'fieldEpoch': 0 if recovering else state['fieldEpochs'][field],
            'factId': fact.fact_id,
            'effect': effect,
            'sourceKey': source_key,
            'publicationRevision': publication_revision,
            'sourceType': source_type or origin.source_type,
            'sourceId': source_id or origin.source_id,
            'value': _value(field, fact.value),
            'reliability': _reliability(fact),
            'observedAt': _utc(fact.effective_period.start),
            'confirmedAt': confirmed_at,
            'sourceRef': {
                'factId': fact.fact_id,
                'operationId': operation_id,
                'sourceType': source_type or origin.source_type,
                'sourceId': source_id or origin.source_id,
                'sources': [item.model_dump(mode='json') for item in sources],
                **({'publicationRevision': publication_revision} if publication_revision is not None else {}),
            },
            'status': 'prepared',
        }
        saved = await self.store.create(row)
        if saved['payloadHash'] != fingerprint:
            raise BasicInfoConflict('basic_info.source_effect_conflict')
        return saved

    @staticmethod
    def tag_fact(fact, fence):
        return (
            fact
            if fence is None
            else fact.model_copy(
                update={
                    'fact_id': fence['factId'],
                    'basic_info_lifecycle_epoch': fence['lifecycleEpoch'],
                }
            )
        )

    async def ensure_tag(self, fence, fact):
        if fence is None or fact.basic_info_lifecycle_epoch is not None:
            return fact
        for _ in range(32):
            updated = fact.model_copy(
                update={
                    'basic_info_lifecycle_epoch': fence['lifecycleEpoch'],
                }
            )
            stored = await self.repository.replace_fact(
                updated, expected_version=fact.version, allowed_statuses=(fact.status,)
            )
            if stored is not None:
                return stored
            fact = await self.repository.get_fact(fact.user_id, fact.subject_id, fact.fact_id)
            if fact is None or fact.basic_info_lifecycle_epoch is not None:
                return fact
        raise BasicInfoUnavailable('basic_info.concurrent_source_fact_tag')

    async def repair(
        self,
        fact,
        subject,
        operation_id,
        *,
        proof_epoch=None,
        source_type=None,
        source_id=None,
        publication_revision=None,
    ):
        field = field_for_fact(fact)
        if field is None:
            return
        fence = await self.store.get(self._key(fact.user_id, operation_id, field))
        if fence is None:
            epoch = fact.basic_info_lifecycle_epoch or proof_epoch
            if epoch is None:
                return
            if source_type is not None and not any(
                row.source_type == source_type and row.source_id == source_id for row in fact.sources
            ):
                return  # The old source has already been retired; remaining manual sources are independent.
            proven = fact.model_copy(update={'basic_info_lifecycle_epoch': epoch})
            fence = await self.prepare(
                proven,
                subject,
                operation_id,
                admitted_at=None,
                source_type=source_type,
                source_id=source_id,
                publication_revision=publication_revision,
                recovering=True,
            )
        fact = await self.ensure_tag(fence, fact)
        await self.publish(fence, fact)
        await self.basic_info.publish_governance(fact.user_id, fact.subject_id)

    async def publish(self, fence, fact):
        if fence is None:
            return
        for _ in range(32):
            subject = await self.repository.get_subject(fence['userId'], fence['subjectId'])
            if (
                subject is None
                or subject.status is not SubjectStatus.ACTIVE
                or subject.lifecycle_epoch != fence['lifecycleEpoch']
            ):
                await self._finish(fence, 'blocked')
                return
            stored = await self.repository.get_fact(fence['userId'], fence['subjectId'], fence['factId'])
            state = await self.basic_info._scope(fence['userId'], fence['subjectId'], fence['lifecycleEpoch'])
            after = deepcopy(state)
            field = fence['field']
            key = fence['sourceKey']
            old = after['sources'].get(key)
            was_current = state['selected'].get(field) == key
            previous_revision = (old or {}).get('sourceRef', {}).get('publicationRevision')
            if previous_revision is not None and (
                fence['publicationRevision'] is None or previous_revision > fence['publicationRevision']
            ):
                await self._finish(fence, 'superseded')
                return
            eligible = state['fieldEpochs'][field] == fence['fieldEpoch'] and field not in state['blockedFields']
            active = (
                eligible
                and stored is not None
                and stored.status is FactStatus.ACTIVE
                and stored.basic_info_lifecycle_epoch == fence['lifecycleEpoch']
                and any(
                    item.source_type == fence['sourceType'] and item.source_id == fence['sourceId']
                    for item in stored.sources
                )
                and _value(field, stored.value) == fence['value']
                and fence['effect'] == 'upsert'
            )
            row = {
                'field': field,
                'value': fence['value'],
                'reliability': fence['reliability'],
                'factId': fence['factId'],
                'active': active,
                'order': old['order'] if old else len(after['sources']),
                'observedAt': fence['observedAt'],
                'confirmedAt': fence['confirmedAt'],
                'ageBasis': (
                    'confirmed_at_time'
                    if fence['confirmedAt']
                    else 'reported_at_report'
                    if fence['sourceType'] == 'health_report'
                    else None
                )
                if field == 'age'
                else None,
                'sourceRef': deepcopy(fence['sourceRef']),
                'owner': None,
            }
            if not eligible:
                row['value'] = None
                row['sourceRef'] = {
                    'factId': fence['factId'],
                    'operationId': fence['operationId'],
                    'sourceType': fence['sourceType'],
                    'sourceId': fence['sourceId'],
                    'governanceDisposition': 'superseded',
                }
            if stored is None:
                row.update(value=None, deleted=True, sourceRef={'deletedFactId': fence['factId']})
            after['sources'][key] = row
            if fence['effect'] == 'withdraw':
                for source in after['sources'].values():
                    if source.get('sourceRef', {}).get('legacyFactId') == fence['factId']:
                        source['active'] = False
                if stored is not None and stored.status is FactStatus.ACTIVE and eligible:
                    for remaining in stored.sources:
                        if remaining.source_type == fence['sourceType'] and remaining.source_id == fence['sourceId']:
                            continue
                        remaining_key = 'existing_fact_' + _digest(
                            [
                                fence['userId'],
                                fence['subjectId'],
                                fence['lifecycleEpoch'],
                                stored.fact_id,
                                remaining.source_type,
                                remaining.source_id,
                            ]
                        )
                        if remaining_key in after['sources']:
                            continue  # A different owner's retirement/fence is never undone by this event.
                        retained = deepcopy(row)
                        confirmed_at = _utc(remaining.observed_at) if remaining.source_type == 'manual' else None
                        independent = any(
                            item['active']
                            and item['factId'] == stored.fact_id
                            and item.get('confirmedAt') is not None
                            and item.get('owner') is None
                            and item.get('sourceRef', {}).get('sourceType')
                            not in {'record', 'chat', 'health_report', 'questionnaire'}
                            and (
                                item.get('sourceRef', {}).get('commandId')
                                or item.get('sourceRef', {}).get('operationId')
                            )
                            for item in after['sources'].values()
                        )
                        if remaining.source_type == 'manual' and independent:
                            continue  # The existing explicit confirmation already owns its exact time/basis.
                        if (
                            remaining.source_type == 'manual'
                            and confirmed_at is None
                            and fence['sourceType'] == 'record'
                        ):
                            confirmed_at = fence['confirmedAt']
                        retained.update(
                            active=True,
                            value=_value(field, stored.value),
                            reliability=_reliability(stored),
                            confirmedAt=confirmed_at,
                            ageBasis=(
                                'confirmed_at_time'
                                if confirmed_at
                                else 'reported_at_report'
                                if remaining.source_type == 'health_report'
                                else None
                            )
                            if field == 'age'
                            else None,
                            sourceRef={
                                'factId': stored.fact_id,
                                'sourceType': remaining.source_type,
                                'sourceId': remaining.source_id,
                                'sources': [remaining.model_dump(mode='json')],
                            },
                        )
                        after['sources'][remaining_key] = retained
                        if was_current and remaining.source_type == 'manual':
                            after['selected'][field] = remaining_key
            elif active:
                self.basic_info._consider(after, key)
            self.basic_info._reselect(after)
            latest = await self.repository.get_subject(fence['userId'], fence['subjectId'])
            if (
                latest is None
                or latest.status is not SubjectStatus.ACTIVE
                or latest.lifecycle_epoch != fence['lifecycleEpoch']
            ):
                await self._finish(fence, 'blocked')
                return
            committed = await self.store.cas(state, after)
            if committed is not None:
                await self._finish(fence, 'applied' if eligible else 'superseded')
                return
        raise BasicInfoUnavailable('basic_info.concurrent_source_effect')

    async def _finish(self, fence, status):
        for _ in range(32):
            row = await self.store.get(fence['_id'])
            if row['status'] == status:
                return
            after = deepcopy(row)
            after['status'] = status
            if await self.store.cas(row, after) is not None:
                return
        raise BasicInfoUnavailable('basic_info.concurrent_source_effect_receipt')
