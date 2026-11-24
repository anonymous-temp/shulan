"""Manual demographic mutations keep their first lifecycle across recovery."""

import asyncio
from datetime import timedelta

import pytest

from health_profile import HealthProfileConflictError
from health_profile import HealthProfileService
from health_profile.basic_info import ProfileBasicInfoService
from health_profile.models import SubjectStatus
from health_profile.snapshots import ProfileSnapshotReader
from tests.unit.health_profile.test_basic_info import NOW
from tests.unit.health_profile.test_basic_info import composition as composition
from tests.unit.health_profile.test_basic_info import legacy_fact
from tests.unit.health_profile.test_basic_info import values


async def cycle_subject(repo, sid, *, at=NOW):
    for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        subject = await repo.get_subject('u', sid)
        await repo.update_subject(
            subject.model_copy(update={'status': status, 'version': subject.version + 1, 'updated_at': at}),
            expected_version=subject.version,
        )


async def initial(composition):
    repo, basic, profile, _ = composition
    command = await basic.register('u', 'initial', values=values(), confirmed_at=NOW)
    await basic.apply('u', command.command_id)
    return repo, basic, profile, command.subject_id


async def resolve(profile, repo, sid, fact, decision, request_id):
    view = await repo.get_view('u', sid)
    options = {}
    if decision == 'replace':
        options['replacement'] = {
            'valueType': 'quantity',
            'value': {'number': 44, 'unit': 'years'},
            'effectivePeriod': {'start': None},
        }
    return await profile.resolve_fact(
        'u',
        sid,
        fact.fact_id,
        decision,
        request_id=request_id,
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        **options,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['confirm', 'replace'])
async def test_manual_facts_are_tagged_and_not_imported_after_reactivation(composition, decision):
    repo, basic, profile, sid = await initial(composition)
    fact = await legacy_fact(repo, sid, fact_id='unmanaged_age', value=44)
    result = await resolve(profile, repo, sid, fact, decision, 'manual')
    stored = await repo.get_fact('u', sid, result['fact']['factId'])
    assert stored.basic_info_lifecycle_epoch == 1
    assert (await repo.get_fact('u', sid, fact.fact_id)).basic_info_lifecycle_epoch == 1
    await cycle_subject(repo, sid, at=NOW + timedelta(days=1))
    current = await basic.read('u')
    assert current.lifecycle_epoch == 3
    assert all(field.value is None for field in current.fields.values())
    assert (await profile.get_profile('u'))['overview']['age'] is None
    assert not (await profile.list_facts('u'))['items']
    reader = ProfileSnapshotReader(repo, clock=lambda: NOW + timedelta(days=1))
    assert not (await reader.read('u')).facts
    assert not (await reader.read_v2('u')).facts
    assert not (await reader.read_page('u')).facts


@pytest.mark.asyncio
@pytest.mark.parametrize('decision', ['confirm', 'replace'])
async def test_lifecycle_change_between_physical_write_and_publish_blocks_old_mutation(
    composition,
    monkeypatch,
    decision,
):
    repo, basic, profile, sid = await initial(composition)
    fact = await legacy_fact(repo, sid, fact_id='unmanaged_age', value=44)
    replace = repo.replace_fact
    changed = False

    async def cycle_after_write(candidate, **options):
        nonlocal changed
        stored = await replace(candidate, **options)
        if stored is not None and candidate.last_request_id == 'old_manual' and not changed:
            changed = True
            await cycle_subject(repo, sid, at=NOW + timedelta(days=1))
        return stored

    monkeypatch.setattr(repo, 'replace_fact', cycle_after_write)
    with pytest.raises(HealthProfileConflictError):
        await resolve(profile, repo, sid, fact, decision, 'old_manual')
    assert changed
    assert all(field.value is None for field in (await basic.read('u')).fields.values())
    assert not (await profile.list_facts('u'))['items']
    # A retry must find the original operation instead of registering in epoch 3.
    with pytest.raises(HealthProfileConflictError):
        await resolve(profile, repo, sid, fact, decision, 'old_manual')
    assert all(field.value is None for field in (await basic.read('u')).fields.values())


@pytest.mark.asyncio
async def test_replace_ack_loss_restarts_with_original_confirmation_time(composition, monkeypatch):
    repo, basic, profile, sid = await initial(composition)
    age = (await basic.read('u')).fields['age']
    fact = await repo.get_fact('u', sid, age.fact_id)
    view = await repo.get_view('u', sid)
    create = repo.create_fact
    lost = False

    async def lose_ack(candidate):
        nonlocal lost
        result = await create(candidate)
        if candidate.creation_request_id == 'manual' and not lost:
            lost = True
            raise OSError('synthetic lost acknowledgement')
        return result

    monkeypatch.setattr(repo, 'create_fact', lose_ack)
    with pytest.raises(OSError):
        await resolve(profile, repo, sid, fact, 'replace', 'manual')
    monkeypatch.setattr(repo, 'create_fact', create)
    later = NOW + timedelta(days=2)
    restored = ProfileBasicInfoService(repo, basic.store, clock=lambda: later)
    repo.basic_info = restored
    profile = HealthProfileService(repo, clock=lambda: later)
    result = await profile.resolve_fact(
        'u',
        sid,
        fact.fact_id,
        'replace',
        request_id='manual',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
        replacement={
            'valueType': 'quantity',
            'value': {'number': 44, 'unit': 'years'},
            'effectivePeriod': {'start': None},
        },
    )
    current = (await restored.read('u')).fields['age']
    assert current.value == 44 and current.confirmed_at == NOW
    assert current.age_basis == 'confirmed_at_time' and current.observed_at is None
    assert (await repo.get_fact('u', sid, result['fact']['factId'])).basic_info_lifecycle_epoch == 1


@pytest.mark.asyncio
async def test_mutation_fence_survives_restart_and_rejects_reused_payload(composition):
    repo, basic, _, sid = await initial(composition)
    first = await basic.guard_mutation('u', sid, 'age', 'manual', request_fingerprint='original')
    await cycle_subject(repo, sid, at=NOW + timedelta(days=1))
    restored = ProfileBasicInfoService(repo, basic.store, clock=lambda: NOW + timedelta(days=2))
    with pytest.raises(HealthProfileConflictError):
        await restored.guard_mutation('u', sid, 'age', 'manual', request_fingerprint='original')
    with pytest.raises(HealthProfileConflictError):
        await restored.guard_mutation('u', sid, 'age', 'manual', request_fingerprint='changed')
    assert first['lifecycleEpoch'] == 1 and first['confirmedAt'] == NOW


@pytest.mark.asyncio
@pytest.mark.parametrize('cycle', [False, True])
async def test_pre_index_interrupted_manual_operation_uses_original_scope(composition, monkeypatch, cycle):
    repo, basic, profile, sid = await initial(composition)
    fact = await repo.get_fact('u', sid, (await basic.read('u')).fields['age'].fact_id)
    view = await repo.get_view('u', sid)
    accept = basic.accept_manual_fact

    async def fail_publish(*args, **kwargs):
        raise OSError('synthetic interrupted publication')

    monkeypatch.setattr(basic, 'accept_manual_fact', fail_publish)
    with pytest.raises(OSError):
        await resolve(profile, repo, sid, fact, 'replace', 'manual')
    monkeypatch.setattr(basic, 'accept_manual_fact', accept)
    # Emulate a pre-fix interrupted write: the old scope is durable, while
    # there is neither an independent mutation index nor the physical tag.
    collections = composition[3]
    collections[5].documents[:] = [
        row for row in collections[5].documents if row.get('recordKind') != 'basic_info_mutation_index'
    ]
    for row in collections[1].documents:
        if row.get('lastRequestId') == 'manual' or row.get('creationRequestId') == 'manual':
            row.pop('basicInfoLifecycleEpoch', None)
    if cycle:
        await cycle_subject(repo, sid, at=NOW + timedelta(days=1))
    later = NOW + timedelta(days=2)
    restored = ProfileBasicInfoService(repo, basic.store, clock=lambda: later)
    repo.basic_info = restored
    profile = HealthProfileService(repo, clock=lambda: later)
    options = {
        'request_id': 'manual',
        'expected_fact_version': fact.version,
        'expected_profile_version': view.profile_version,
        'replacement': {
            'valueType': 'quantity',
            'value': {'number': 44, 'unit': 'years'},
            'effectivePeriod': {'start': None},
        },
    }
    if cycle:
        with pytest.raises(HealthProfileConflictError):
            await profile.resolve_fact('u', sid, fact.fact_id, 'replace', **options)
        assert (await restored.read('u')).fields['age'].value is None
    else:
        result = await profile.resolve_fact('u', sid, fact.fact_id, 'replace', **options)
        age = (await restored.read('u')).fields['age']
        assert age.value == 44 and age.confirmed_at == NOW
        assert (await repo.get_fact('u', sid, result['fact']['factId'])).basic_info_lifecycle_epoch == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('newer_action', ['confirmation', 'deletion'])
async def test_paused_manual_publish_cannot_override_later_confirmation_or_deletion(
    composition,
    monkeypatch,
    newer_action,
):
    repo, basic, profile, sid = await initial(composition)
    fact = await repo.get_fact('u', sid, (await basic.read('u')).fields['age'].fact_id)
    accept = basic.accept_manual_fact
    paused, release = asyncio.Event(), asyncio.Event()

    async def pause_old(result, operation_id, **options):
        if operation_id == 'old_manual':
            paused.set()
            await release.wait()
        return await accept(result, operation_id, **options)

    monkeypatch.setattr(basic, 'accept_manual_fact', pause_old)
    old = asyncio.create_task(resolve(profile, repo, sid, fact, 'replace', 'old_manual'))
    await asyncio.wait_for(paused.wait(), timeout=5)
    try:
        later = await basic.register(
            'u',
            'later',
            values=values(age=46),
            confirmed_at=NOW + timedelta(hours=1),
        )
        await basic.apply('u', later.command_id)
        if newer_action == 'deletion':
            age = (await basic.read('u')).fields['age']
            selected = await repo.get_fact('u', sid, age.fact_id)
            view = await repo.get_view('u', sid)
            await profile.delete_fact(
                'u',
                sid,
                selected.fact_id,
                delete_scope='fact',
                request_id='later_delete',
                expected_fact_version=selected.version,
                expected_profile_version=view.profile_version,
            )
    finally:
        release.set()
    # A changed view can reject the old response; neither outcome grants it
    # permission to publish over a later accepted field decision.
    try:
        await old
    except HealthProfileConflictError:
        pass
    age = (await basic.read('u')).fields['age']
    assert age.value == (46 if newer_action == 'confirmation' else None)
    assert (await profile.get_profile('u'))['overview']['age'] == age.value


@pytest.mark.asyncio
async def test_mutation_index_ack_loss_keeps_first_time_after_restart(composition, monkeypatch):
    repo, basic, profile, sid = await initial(composition)
    fact = await repo.get_fact('u', sid, (await basic.read('u')).fields['age'].fact_id)
    view = await repo.get_view('u', sid)
    create = basic.store.create
    lost = False

    async def lose_index_ack(document):
        nonlocal lost
        result = await create(document)
        if document.get('recordKind') == 'basic_info_mutation_index' and not lost:
            lost = True
            raise OSError('synthetic lost mutation index acknowledgement')
        return result

    monkeypatch.setattr(basic.store, 'create', lose_index_ack)
    with pytest.raises(OSError):
        await resolve(profile, repo, sid, fact, 'confirm', 'manual')
    monkeypatch.setattr(basic.store, 'create', create)
    restored = ProfileBasicInfoService(repo, basic.store, clock=lambda: NOW + timedelta(days=2))
    repo.basic_info = restored
    profile = HealthProfileService(repo, clock=lambda: NOW + timedelta(days=2))
    await profile.resolve_fact(
        'u',
        sid,
        fact.fact_id,
        'confirm',
        request_id='manual',
        expected_fact_version=fact.version,
        expected_profile_version=view.profile_version,
    )
    assert (await restored.read('u')).fields['age'].confirmed_at == NOW


@pytest.mark.asyncio
async def test_paused_mutation_registration_cannot_take_a_new_field_epoch(composition, monkeypatch):
    repo, basic, profile, sid = await initial(composition)
    fact = await repo.get_fact('u', sid, (await basic.read('u')).fields['age'].fact_id)
    create = basic.store.create
    paused, release = asyncio.Event(), asyncio.Event()

    async def pause_after_index(document):
        saved = await create(document)
        if document.get('recordKind') == 'basic_info_mutation_index' and document['operationId'] == 'old_manual':
            paused.set()
            await release.wait()
        return saved

    monkeypatch.setattr(basic.store, 'create', pause_after_index)
    old = asyncio.create_task(resolve(profile, repo, sid, fact, 'confirm', 'old_manual'))
    await asyncio.wait_for(paused.wait(), timeout=5)
    try:
        later = await basic.register(
            'u',
            'later',
            values=values(age=46),
            confirmed_at=NOW + timedelta(hours=1),
        )
        await basic.apply('u', later.command_id)
    finally:
        release.set()
    with pytest.raises(HealthProfileConflictError):
        await old
    age = (await basic.read('u')).fields['age']
    assert age.value == 46
    assert age.governance_epoch == later.field_epochs['age']


@pytest.mark.asyncio
@pytest.mark.parametrize('newer_action', ['api', 'name'])
async def test_paused_api_registration_preserves_newer_field_decisions(composition, monkeypatch, newer_action):
    repo, basic, profile, sid = await initial(composition)
    create = basic.store.create
    paused, release = asyncio.Event(), asyncio.Event()

    async def pause_old_index(document):
        saved = await create(document)
        if document.get('recordKind') == 'basic_info_command_index' and document['operationId'] == 'old_api':
            paused.set()
            await release.wait()
        return saved

    monkeypatch.setattr(basic.store, 'create', pause_old_index)
    old = asyncio.create_task(basic.register('u', 'old_api', values=values(age=44), confirmed_at=NOW))
    await asyncio.wait_for(paused.wait(), timeout=5)
    try:
        if newer_action == 'api':
            later = await basic.register(
                'u',
                'later',
                values=values(age=46),
                confirmed_at=NOW + timedelta(hours=1),
            )
            await basic.apply('u', later.command_id)
        else:
            subject = await repo.get_subject('u', sid)
            await profile.upsert_subject(
                'u',
                subject_id=sid,
                display_name='Later Name',
                request_id='later_name',
                expected_subject_version=subject.version,
            )
    finally:
        release.set()
    receipt = await old
    await basic.apply('u', receipt.command_id)
    current = await basic.read('u')
    if newer_action == 'api':
        assert current.fields['age'].value == 46
        assert receipt.status == 'superseded'
        assert not (await basic.list_pending()).items
    else:
        assert current.fields['name'].value == 'Later Name'
        assert receipt.fields['name'].status == 'superseded'
        assert current.fields['age'].value == 44


@pytest.mark.asyncio
async def test_manual_fence_identity_survives_actual_bson_microsecond_truncation(composition, monkeypatch):
    from bson import BSON

    from health_profile.basic_info_store import _aware

    repo, basic, profile, sid = await initial(composition)
    at = NOW.replace(microsecond=123456)
    basic.clock = lambda: at
    fact = await repo.get_fact('u', sid, (await basic.read('u')).fields['age'].fact_id)
    get = basic.store.get

    async def bson_read(key):
        document = await get(key)
        if document and document.get('recordKind') == 'basic_info_mutation_index':
            return _aware(BSON.encode(document).decode())
        return document

    monkeypatch.setattr(basic.store, 'get', bson_read)
    await resolve(profile, repo, sid, fact, 'confirm', 'manual')
    age = (await basic.read('u')).fields['age']
    assert age.value == 42 and age.confirmed_at == at.replace(microsecond=123000)
    assert age.observed_at == fact.effective_period.start


@pytest.mark.asyncio
async def test_api_registration_identity_survives_actual_bson_microsecond_truncation(composition):
    from bson import BSON

    from health_profile.basic_info_store import BasicInfoConflict

    _, basic, _, _ = composition
    at = NOW.replace(microsecond=123456)
    first = await basic.register('u', 'bson_api', values=values(), confirmed_at=at)
    persisted = BSON.encode({'at': at}).decode()['at'].replace(tzinfo=at.tzinfo)
    replay = await basic.register('u', 'bson_api', values=values(), confirmed_at=persisted)
    assert replay.command_id == first.command_id and replay.field_epochs == first.field_epochs
    assert replay.payload_hash == first.payload_hash
    with pytest.raises(BasicInfoConflict):
        await basic.register('u', 'bson_api', values=values(), confirmed_at=persisted + timedelta(milliseconds=1))


@pytest.mark.asyncio
async def test_existing_pre_normalization_command_replays_without_rewriting_its_snapshot(composition):
    from health_profile.basic_info import _digest

    _, basic, _, collections = composition
    at = NOW.replace(microsecond=123456)
    first = await basic.register('u', 'legacy_api', values=values(), confirmed_at=at)
    old_hash = _digest({'values': values().model_dump(), 'confirmedAt': at, 'subjectId': None})
    for row in collections[5].documents:
        if row.get('recordKind') == 'basic_info_command_index':
            row['payloadHash'] = old_hash
            row.pop('baseFieldEpochs', None)
        if row.get('recordKind') == 'basic_info_scope':
            row['commands'][first.command_id]['payloadHash'] = old_hash
    before = await basic.get('u', 'legacy_api')
    repeated = await basic.register('u', 'legacy_api', values=values(), confirmed_at=at)
    assert repeated == before and repeated.payload_hash == old_hash
    assert (await basic.apply('u', repeated.command_id)).status == 'applied'
