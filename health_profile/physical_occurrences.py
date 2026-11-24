"""Materialize exact physical Document occurrences through the Profile fact CAS."""

from datetime import UTC

from pydantic import TypeAdapter
from pymongo.errors import DuplicateKeyError

from utils.report_observation_comparison import observation_label_key
from utils.report_observation_comparison import observation_literal
from utils.report_observation_comparison import observation_unit_key

from .contracts import ProfileScope
from .contracts import profile_content_digest
from .document_source_contracts_v2 import DocumentFixedObservationRef
from .document_source_contracts_v2 import DocumentPhysicalOccurrence
from .indicators import is_report_observation_type
from .models import EffectivePeriod
from .models import FactSource
from .models import FactStatus
from .models import FactValueType
from .models import PhysicalFactOrigin
from .models import PhysicalFactReading
from .models import PhysicalFactReservation
from .models import VerificationStatus
from .models import fact_user_confirmed
from .source_material import ProfileSourceApplicationError
from .source_publications import publication_id
from .source_publications import source_key

_SYSTEM_PREFIXES = ('profile_publication:', 'profile_report_enrich:')


async def physical_keys(repository, material, *, destinations=None):
    """Match fixed aliases without replacing the native physical identity."""
    rows = {}
    keys_by_subject = {}
    for row in material.observations:
        pointer = row['observationRef']
        destination = (destinations or {}).get(pointer, {})
        subject_id = destination.get('subjectId', material.scope.subject_id)
        identity = [
            material.scope.user_id,
            subject_id,
            destination.get('lifecycleEpoch', material.lifecycle_epoch),
        ]
        keys = []
        if raw := row.get('physicalOccurrence'):
            occurrence = DocumentPhysicalOccurrence.model_validate(raw)
            keys.append(
                profile_content_digest(
                    [
                        *identity,
                        occurrence.document_id,
                        occurrence.source_checksum,
                        occurrence.locator_revision,
                        occurrence.locator,
                    ]
                )
            )
        if raw := row.get('fixedAnalysisObservationRef'):
            fixed = DocumentFixedObservationRef.model_validate(raw)
            keys.append(
                profile_content_digest(
                    [
                        *identity,
                        fixed.document_id,
                        'analysis_fixed_observation',
                        fixed.interpretation_round,
                        fixed.observation_ref,
                    ]
                )
            )
        if keys:
            rows[pointer] = keys
            keys_by_subject.setdefault(subject_id, set()).update(keys)

    bindings = {}
    for subject_id, keys in keys_by_subject.items():
        for key, primary in await repository.find_physical_occurrence_keys(material.scope.user_id, subject_id, keys):
            bindings.setdefault(key, set()).add(primary)
    resolved = {}
    for pointer, keys in rows.items():
        known = {primary for key in keys for primary in bindings.get(key, ())}
        if len(known) > 1:
            raise ProfileSourceApplicationError('source.physical_occurrence_identity_conflict')
        primary = next(iter(known), keys[-1])
        aliases = tuple(key for key in keys if key != primary)
        resolved[pointer] = primary, aliases
        for key in keys:
            bindings.setdefault(key, set()).add(primary)
    return resolved


def physical_reading(fact):
    data = {field: getattr(fact, field) for field in PhysicalFactReading.model_fields}
    data['effective_period'] = _normalized_period(fact.effective_period)
    return PhysicalFactReading(**data)


def _quantity_core(value):
    comparator = value.get('comparator')
    return value['number'], value.get('unit'), {'≤': '<=', '≥': '>='}.get(comparator, comparator)


def _millisecond(value):
    if value is None:
        return None
    value = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def _normalized_period(period):
    return EffectivePeriod(start=_millisecond(period.start), end=_millisecond(period.end))


def _automatic_readings(readings):
    return tuple(
        reading
        for reading in readings
        if reading.status is FactStatus.ACTIVE and reading.verification_status is VerificationStatus.CONFIRMED
    )


def _reading_identity(reading, automatic_bridge):
    if automatic_bridge and is_report_observation_type(reading.fact_type):
        return reading.category, reading.value_type, observation_label_key(reading.display_name)
    return reading.category, reading.value_type, reading.fact_type


def _check_compatible_readings(readings):
    automatic_bridge = bool(_automatic_readings(readings))
    expected_identity = _reading_identity(readings[0], automatic_bridge)
    unset = object()
    expected_value = unset
    qualifiers = {}
    start = end = None
    for reading in readings:
        period = _normalized_period(reading.effective_period)
        if (
            _reading_identity(reading, automatic_bridge) != expected_identity
            or (start is not None and period.start is not None and start != period.start)
            or (end is not None and period.end is not None and end != period.end)
        ):
            raise ProfileSourceApplicationError('source.physical_occurrence_conflict')
        start, end = start or period.start, end or period.end
        if reading.value_type is FactValueType.QUANTITY:
            number, unit, comparator = _quantity_core(reading.value)
            value = (number, comparator) if automatic_bridge else (number, unit, comparator)
            supplied = {field: reading.value.get(field) for field in ('specimen', 'method')}
            if automatic_bridge:
                supplied = {field: observation_label_key(value) for field, value in supplied.items()}
                supplied['unit'] = observation_unit_key(unit)
                supplied['kind'] = observation_label_key(reading.value.get('kind'))
            for field, detail in supplied.items():
                if detail and field in qualifiers and qualifiers[field] != detail:
                    raise ProfileSourceApplicationError('source.physical_occurrence_conflict')
                if detail:
                    qualifiers[field] = detail
        else:
            value = observation_literal(reading.value) if automatic_bridge else reading.value
        if expected_value is not unset and value != expected_value:
            raise ProfileSourceApplicationError('source.physical_occurrence_conflict')
        expected_value = value


def _reading_rank(reading):
    # Use one actually supported reading. Prefer accepted evidence and then
    # the reading carrying more original measurement context.
    quantity = reading.value if reading.value_type is FactValueType.QUANTITY else {}
    canonical = reading.model_copy(update={'effective_period': _normalized_period(reading.effective_period)})
    return (
        reading.status is FactStatus.ACTIVE,
        reading.effective_period.start is not None,
        sum(bool(quantity.get(field)) for field in ('specimen', 'method')),
        bool(reading.reference_range),
        profile_content_digest(canonical.model_dump(mode='json')),
    )


def _preferred_reading(readings, *, current=None):
    winner = max(readings, key=_reading_rank)
    automatic = _automatic_readings(readings)
    if not automatic:
        return winner
    trusted = max(automatic, key=_reading_rank)
    updates = {
        'value': trusted.value,
        'assertion_level': trusted.assertion_level,
        'verification_status': VerificationStatus.CONFIRMED,
    }
    if current is not None and is_report_observation_type(current.fact_type):
        updates['fact_type'] = current.fact_type
    if trusted.value_type is FactValueType.QUANTITY:
        updates['value'] = {
            **winner.value,
            **{key: value for key, value in trusted.value.items() if value is not None},
        }
    return winner.model_copy(update=updates)


def _ordered_sources(sources):
    return tuple(sorted(sources, key=lambda source: (source.source_id, source.pointer or '')))


def group_physical_reading(facts, event, key, aliases):
    readings = tuple(physical_reading(fact) for fact in facts)
    _check_compatible_readings(readings)
    reading = _preferred_reading(readings)
    if reading.value_type is FactValueType.QUANTITY:
        # An original can be represented twice in one projection. Preserve
        # complementary verified context from the corresponding source rows.
        context = {
            field: item.value[field] for item in readings for field in ('specimen', 'method') if item.value.get(field)
        }
        reading = reading.model_copy(update={'value': {**reading.value, **context}})
    origin = PhysicalFactOrigin(
        source_id=event.source_id, publication_revision=event.publication_revision, reading=reading
    )
    return facts[0].model_copy(
        update={
            **reading.model_dump(),
            'effective_period': reading.effective_period,
            'sources': _ordered_sources(source for fact in facts for source in fact.sources),
            'physical_occurrence_key': key,
            'physical_occurrence_aliases': tuple(sorted(aliases)),
            'physical_origins': (origin,),
        }
    )


def _is_event_source(source, event):
    return source.source_type == event.source_type and source.source_id == event.source_id


def human_owned(fact):
    return fact_user_confirmed(fact) or bool(
        fact.last_request_id and not fact.last_request_id.startswith(_SYSTEM_PREFIXES)
    )


def _check_identity(stored, planned):
    if (
        stored.physical_occurrence_key != planned.physical_occurrence_key
        or stored.basic_info_lifecycle_epoch != planned.basic_info_lifecycle_epoch
    ):
        raise ProfileSourceApplicationError('source.physical_occurrence_identity_mismatch')


def _check_reading(stored, event, incoming):
    others = [
        origin.reading
        for origin in stored.physical_origins
        if origin.reading is not None and origin.source_id != event.source_id
    ]
    _check_compatible_readings((incoming, *others))


def _reservations_except(fact, identity):
    return tuple(row for row in fact.physical_reservations if row.publication_id != identity)


async def _live_reservations(inbox, fact):
    live = []
    for row in fact.physical_reservations:
        owner = await inbox.store.get(row.publication_id)
        if owner is None or owner['status'] in {'failed', 'applied', 'superseded', 'partial'}:
            continue
        scope = ProfileScope.model_validate(owner['scope'])
        cursor = await inbox.store.get('cursor_' + source_key(scope, 'health_report', row.source_id))
        if cursor and cursor['acceptedRetractionRevision'] >= row.publication_revision:
            continue
        live.append(row)
    return tuple(live)


def _reserved_fact(fact, reservations):
    updated = fact.model_copy(update={'physical_reservations': reservations})
    _check_fact_budget(updated)
    return updated


def _check_fact_budget(fact):
    if len(TypeAdapter(dict).dump_json(fact.to_document(document_id=fact.fact_id))) > 4_000_000:
        raise ProfileSourceApplicationError('source.physical_occurrence_origin_limit')


async def reserve_physical_fact(applier, cursor, event, fact):
    """Hold a compatible reading without exposing it or retiring prior evidence."""
    repository = applier.repository
    identity = publication_id(event)
    reading = physical_reading(fact)
    reservation = PhysicalFactReservation(
        source_id=event.source_id,
        publication_id=identity,
        publication_revision=event.publication_revision,
        reading=reading,
    )
    for _ in range(16):
        current = await applier.inbox.check_lease(cursor)
        if current['acceptedRetractionRevision'] >= event.publication_revision:
            return
        await applier._target_subject(event, fact, require_active=True, lifecycle_epoch=fact.basic_info_lifecycle_epoch)
        stored = await repository.get_fact(fact.user_id, fact.subject_id, fact.fact_id, include_reservations=True)
        if stored is None:
            if await repository.get_deleted_fact_tombstone(fact.user_id, fact.subject_id, fact.fact_id):
                raise ProfileSourceApplicationError('source.manual_governance_requires_review')
            shell = fact.model_copy(
                update={
                    'physical_reservation_only': True,
                    'physical_reservations': (reservation,),
                    'status': FactStatus.RETRACTED,
                    'physical_origins': (),
                }
            )
            _check_fact_budget(shell)
            try:
                _, created = await repository.create_fact(shell)
            except DuplicateKeyError:
                continue
            if created:
                return
            continue
        _check_identity(stored, fact)
        if any(
            origin.source_id == event.source_id
            and origin.publication_revision >= event.publication_revision
            and origin.reading is not None
            for origin in stored.physical_origins
        ):
            return
        if human_owned(stored):
            raise ProfileSourceApplicationError('source.manual_governance_requires_review')
        _check_reading(stored, event, reading)
        live = await _live_reservations(applier.inbox, stored)
        if any(row.publication_id == identity for row in live):
            return
        try:
            _check_compatible_readings((reading, *(row.reading for row in live)))
        except ProfileSourceApplicationError as error:
            raise ProfileSourceApplicationError('source.physical_occurrence_pending', retryable=True) from error
        accepted = {row.source_id for row in stored.physical_origins}
        if len(accepted | {row.source_id for row in live} | {event.source_id}) > 1000:
            raise ProfileSourceApplicationError('source.physical_occurrence_origin_limit')
        updated = _reserved_fact(stored, (*live, reservation))
        if await repository.replace_physical_reservations(
            updated, version=stored.version, expected=stored.physical_reservations
        ):
            return
    raise ProfileSourceApplicationError('source.concurrent_fact_update', retryable=True)


async def release_physical_fact(applier, cursor, event, fact):
    identity = publication_id(event)
    repository = applier.repository
    for _ in range(16):
        await applier.inbox.check_lease(cursor)
        stored = await repository.get_fact(fact.user_id, fact.subject_id, fact.fact_id, include_reservations=True)
        if stored is None:
            return
        _check_identity(stored, fact)
        remaining = _reservations_except(stored, identity)
        if remaining == stored.physical_reservations or await repository.replace_physical_reservations(
            _reserved_fact(stored, remaining), version=stored.version, expected=stored.physical_reservations
        ):
            return
    raise ProfileSourceApplicationError('source.concurrent_fact_update', retryable=True)


def _advance_origin(fact, event, incoming, *, human, now, request_id, compensate):
    origins = {origin.source_id: origin for origin in fact.physical_origins}
    previous = origins.get(event.source_id)
    if compensate and (previous is None or previous.publication_revision != event.publication_revision):
        return None
    if previous is not None and (
        previous.publication_revision > event.publication_revision
        or (previous.publication_revision == event.publication_revision and (previous.reading is None or incoming))
    ):
        return None
    if len(origins) >= 1000 and event.source_id not in origins:
        raise ProfileSourceApplicationError('source.physical_occurrence_origin_limit')
    reading = physical_reading(incoming) if incoming else None
    if reading is not None:
        if human:
            raise ProfileSourceApplicationError('source.manual_governance_requires_review')
        _check_reading(fact, event, reading)
    origins[event.source_id] = PhysicalFactOrigin(
        source_id=event.source_id, publication_revision=event.publication_revision, reading=reading
    )
    active = [origin.reading for origin in origins.values() if origin.reading is not None]
    active_ids = {origin.source_id for origin in origins.values() if origin.reading is not None}
    remaining = tuple(
        source
        for source in fact.sources
        if not _is_event_source(source, event)
        and (source.source_type != 'health_report' or source.source_id in active_ids)
    )
    if incoming is not None:
        remaining = _ordered_sources((*remaining, *incoming.sources))

    if human and fact_user_confirmed(fact) and not any(source.source_type == 'manual' for source in remaining):
        remaining = (
            *remaining,
            FactSource(
                source_type='manual',
                source_id=fact.last_request_id or fact.fact_id,
                source_revision=1,
                pointer=fact.fact_id,
            ),
        )
    updates = {}
    if not human:
        if active:
            winner = _preferred_reading(active, current=fact)
            updates = {**winner.model_dump(), 'effective_period': _normalized_period(winner.effective_period)}
        else:
            updates['status'] = FactStatus.RETRACTED
    aliases = set(fact.physical_occurrence_aliases)
    if incoming is not None:
        aliases.update(incoming.physical_occurrence_aliases)
    if len(aliases) > 1000:
        raise ProfileSourceApplicationError('source.physical_occurrence_origin_limit')
    return fact.model_copy(
        update={
            **updates,
            'physical_occurrence_aliases': tuple(sorted(aliases)),
            'sources': remaining or fact.sources,
            'physical_reservation_only': False,
            'physical_reservations': _reservations_except(fact, publication_id(event)),
            'physical_origins': tuple(origins[key] for key in sorted(origins)),
            'last_request_id': fact.last_request_id if human else request_id,
            'version': fact.version + 1,
            'updated_at': now,
        }
    )


async def apply_physical_fact(applier, cursor, event, fact, *, upsert=False, compensate=False):
    """Update one source generation at a time; replay original publication effects."""
    repository = applier.repository
    request_id = f'profile_publication:{publication_id(event)}:{fact.fact_id}'
    if compensate:
        request_id += ':compensate'
    if await repository.get_event_by_request(event.scope.user_id, request_id):
        return
    for _ in range(16):
        current = await applier.inbox.check_lease(cursor)
        target = await applier._target_subject(
            event, fact, require_active=upsert, lifecycle_epoch=fact.basic_info_lifecycle_epoch
        )
        incoming = fact if upsert and current['acceptedRetractionRevision'] < event.publication_revision else None
        stored = await repository.get_fact(fact.user_id, fact.subject_id, fact.fact_id, include_reservations=True)
        if stored is None:
            if compensate:
                return
            if await repository.get_deleted_fact_tombstone(fact.user_id, fact.subject_id, fact.fact_id):
                if incoming is not None:
                    raise ProfileSourceApplicationError('source.manual_governance_requires_review')
                return
            now = applier.inbox.now()
            if incoming is None:
                initial = fact.model_copy(
                    update={
                        'status': FactStatus.RETRACTED,
                        'physical_origins': (
                            PhysicalFactOrigin(
                                source_id=event.source_id, publication_revision=event.publication_revision, reading=None
                            ),
                        ),
                        'last_request_id': request_id,
                        'updated_at': now,
                    }
                )
            else:
                initial = incoming.model_copy(update={'last_request_id': request_id, 'updated_at': now})
            try:
                stored, created = await repository.create_fact(initial)
            except DuplicateKeyError:
                continue
            if created:
                break
        _check_identity(stored, fact)
        if (
            incoming is not None
            and not any(row.publication_id == publication_id(event) for row in stored.physical_reservations)
            and not any(
                row.source_id == event.source_id and row.publication_revision >= event.publication_revision
                for row in stored.physical_origins
            )
        ):
            raise ProfileSourceApplicationError('source.physical_occurrence_pending', retryable=True)
        changed = _advance_origin(
            stored,
            event,
            incoming,
            human=human_owned(stored),
            now=applier.inbox.now(),
            request_id=request_id,
            compensate=compensate,
        )
        if changed is None:
            await release_physical_fact(applier, cursor, event, fact)
            if compensate:
                return
            break
        _check_fact_budget(changed)
        if saved := await repository.replace_fact(
            changed,
            expected_version=stored.version,
            allowed_statuses=tuple(status for status in FactStatus if status is not FactStatus.DELETED),
            expected_reservations=stored.physical_reservations,
        ):
            stored = saved
            break
    else:
        raise ProfileSourceApplicationError('source.concurrent_fact_update', retryable=True)
    await applier._record_effect(event, target, stored, request_id)
