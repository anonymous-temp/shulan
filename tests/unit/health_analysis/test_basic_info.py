"""Confirmed demographics and durable Profile port, without live dependencies."""

import asyncio
from copy import deepcopy
from datetime import datetime
from datetime import timezone

import pytest

from modules.health_analysis.basic_info import ConfirmedBasicInfo
from modules.health_analysis.basic_info_sync import BasicInfoCompensationWorker
from modules.health_analysis.basic_info_sync import BasicInfoConflictError
from modules.health_analysis.basic_info_sync import BasicInfoDeliveryError
from modules.health_analysis.basic_info_sync import BasicInfoSynchronizer
from modules.health_analysis.basic_info_sync import BasicInfoUnavailableError
from modules.health_analysis.contracts import AnalysisLimits
from modules.health_analysis.contracts import AnalysisTimeoutError
from modules.health_analysis.contracts import InvalidAnalysisInputError
from tests.unit._support.health_analysis import FakeBasicInfoPort
from tests.unit._support.health_analysis import payload
from tests.unit._support.health_analysis import setup


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('name', ''),
        ('name', '  '),
        ('name', None),
        ('name', 1),
        ('age', -1),
        ('age', 151),
        ('age', True),
        ('age', 42.0),
        ('age', '42'),
        ('age', None),
        ('gender', ''),
        ('gender', None),
        ('gender', 'male'),
        ('gender', '未知'),
        ('gender', ' 男 '),
    ],
)
async def test_invalid_confirmed_fields_never_reach_profile_or_sources(field, value):
    service, confirmation, profile, chat, generator, writer = setup()
    body = {**payload(), field: value}
    with pytest.raises(InvalidAnalysisInputError):
        await service.generate(body, request_uuid='synthetic')
    for dependency in (confirmation.register, profile.read, chat.read, generator.generate):
        dependency.assert_not_awaited()
    assert not writer.records


@pytest.mark.asyncio
@pytest.mark.parametrize('age', [0, 150])
async def test_confirmed_values_are_frozen_and_do_not_count_as_clinical_data(age):
    service, confirmation, profile, _, generator, writer = setup()
    profile.read.return_value['gender'] = 'female'
    body = {**payload(), 'name': '  合成用户  ', 'age': age, 'gender': '男', 'surveyList': []}
    result = await service.generate(body, request_uuid='synthetic')
    args = confirmation.register.call_args.kwargs
    assert args['values'] == {'name': '合成用户', 'age': age, 'gender': '男'}
    snapshot = generator.generate.call_args.args[0]
    assert snapshot['effective_demographics']['ageYears'] == age
    assert snapshot['effective_demographics']['gender'] == 'male'
    assert not snapshot['has_personal_data'] and result['healthReview'] == ''
    assert profile.read.call_args.args[0].subject_id == 'test-self'
    trace = writer.records[0]
    assert trace['confirmedBasicInfo']['name'] == '合成用户'
    assert trace['basicInfoSync']['receipt']['status'] == 'applied'
    assert trace['sourceScope']['subjectId'] == 'test-self'
    assert 'authorizedScope' not in trace
    body['age'] = 1
    assert trace['confirmedBasicInfo']['age'] == age


@pytest.mark.asyncio
async def test_application_failure_keeps_registered_receipt_and_uses_request_demographics():
    service, confirmation, _, _, generator, writer = setup()
    confirmation.apply.side_effect = BasicInfoUnavailableError('synthetic')
    await service.generate(payload(), request_uuid='synthetic')
    receipt = writer.records[0]['basicInfoSync']
    assert receipt['receipt']['status'] == 'registered'
    assert receipt['applicationAttempt'] == 'deferred'
    assert generator.generate.call_args.args[0]['effective_demographics']['ageYears'] == 42


@pytest.mark.asyncio
async def test_lost_registration_receipt_queries_same_operation_without_registering_again():
    service, confirmation, *_ = setup()

    async def lost(*args, **kwargs):
        await confirmation._register(*args, **kwargs)
        raise BasicInfoUnavailableError('synthetic lost acknowledgement')

    confirmation.register.side_effect = lost
    await service.generate(payload(), request_uuid='synthetic')
    assert confirmation.register.await_count == 1
    assert confirmation.get.call_args.args == confirmation.register.call_args.args
    assert confirmation.apply.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'mutation',
    [
        {'userId': 'other'},
        {'subjectId': 'other'},
        {'lifecycleEpoch': 2},
        {'fieldEpochs': {'name': 2, 'age': 1, 'gender': 1}},
        {'payloadHash': 'different'},
    ],
)
async def test_changed_command_scope_or_versions_stop_before_analysis(mutation):
    service, confirmation, profile, chat, generator, writer = setup()

    async def changed(*args):
        row = await confirmation._apply(*args)
        return {**row, **mutation}

    confirmation.apply.side_effect = changed
    with pytest.raises(BasicInfoConflictError):
        await service.generate(payload(), request_uuid='synthetic')
    for dependency in (profile.read, chat.read, generator.generate):
        dependency.assert_not_awaited()
    assert not writer.records


@pytest.mark.asyncio
async def test_local_application_timeout_is_deferred_but_total_timeout_is_504():
    service, confirmation, *_ = setup()

    async def wait(*args, **kwargs):
        await asyncio.Event().wait()

    confirmation.apply.side_effect = wait
    service.basic_info_sync.timeout_seconds = 0.005
    await service.generate(payload(), request_uuid='local')
    service.limits = AnalysisLimits(
        total_timeout_seconds=0.01, source_timeout_seconds=0.005, trace_timeout_seconds=0.005
    )
    service.basic_info_sync.timeout_seconds = 1
    with pytest.raises(AnalysisTimeoutError):
        await service.generate(payload(), request_uuid='total')


@pytest.mark.asyncio
async def test_registration_timeout_without_receipt_is_not_deferred_success():
    service, confirmation, profile, *_ = setup()

    async def wait(*args, **kwargs):
        await asyncio.Event().wait()

    confirmation.register.side_effect = wait
    service.basic_info_sync.timeout_seconds = 0.005
    with pytest.raises(BasicInfoDeliveryError):
        await service.generate(payload(), request_uuid='synthetic')
    profile.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_compensation_resumes_only_original_commands_and_does_not_modify_frozen_analysis():
    service, confirmation, _, _, generator, writer = setup()
    confirmation.apply.side_effect = BasicInfoUnavailableError('synthetic')
    await service.generate(payload(), request_uuid='synthetic')
    frozen = deepcopy(writer.records)
    confirmation.apply.side_effect = confirmation._apply
    worker = BasicInfoCompensationWorker(confirmation)
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert confirmation.register.await_count == 1 and generator.generate.await_count == 1
    assert writer.records == frozen
    assert confirmation.apply.call_args.args == ('test-user', frozen[0]['basicInfoSync']['receipt']['commandId'])
    # A process restart enumerates durable pending state rather than using trace copies.
    assert await BasicInfoCompensationWorker(confirmation).run_once() == 0


@pytest.mark.asyncio
async def test_pending_pagination_and_one_local_failure_do_not_drop_other_commands():
    port = FakeBasicInfoPort()
    now = datetime.now(timezone.utc)
    rows = [
        await port.register('test-user', f'health-analysis:{i}', values=payload_info().values(), confirmed_at=now)
        for i in range(2)
    ]
    port.list_pending.side_effect = None
    port.list_pending.return_value = {'items': rows, 'nextCursor': 'page-two'}
    first = True

    async def one_timeout(*args):
        nonlocal first
        if first:
            first = False
            raise TimeoutError('synthetic local dependency timeout')
        return await port._apply(*args)

    port.apply.side_effect = one_timeout
    worker = BasicInfoCompensationWorker(port)
    assert await worker.run_once() == 2
    assert port.apply.await_count == 2
    port.list_pending.return_value = {'items': [], 'nextCursor': None}
    await worker.run_once()
    assert port.list_pending.call_args.kwargs['cursor'] == 'page-two'


@pytest.mark.asyncio
async def test_program_errors_and_cancelled_requests_are_not_dependency_degradation():
    service, confirmation, *_ = setup()
    confirmation.apply.side_effect = TypeError('synthetic programmer error')
    with pytest.raises(TypeError):
        await service.generate(payload(), request_uuid='synthetic')
    confirmation.apply.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await service.generate(payload(), request_uuid='synthetic')


def payload_info():
    return ConfirmedBasicInfo.parse(payload())


@pytest.mark.asyncio
async def test_already_superseded_receipt_is_not_reapplied():
    port = FakeBasicInfoPort()
    receipt = await port.register(
        'test-user', 'old', values=payload_info().values(), confirmed_at=datetime.now(timezone.utc)
    )
    receipt['status'] = 'superseded'
    receipt['fields'] = {field: {'status': 'superseded'} for field in ('name', 'age', 'gender')}
    port.register.side_effect = None
    port.register.return_value = receipt
    sync = BasicInfoSynchronizer(port)
    result = await sync.confirm(
        user_id='test-user',
        operation_id='old',
        info=payload_info(),
        confirmed_at=datetime.now(timezone.utc),
        deadline_at=asyncio.get_running_loop().time() + 1,
    )
    assert result['receipt']['status'] == 'superseded'
    port.apply.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'fields',
    [
        {},
        {'name': {'status': 'pending'}},
        {field: {'status': 'pending' if field == 'age' else 'applied'} for field in ('name', 'age', 'gender')},
    ],
)
async def test_malformed_terminal_receipt_never_claims_completed_sync(fields):
    service, confirmation, profile, *_ = setup()

    async def malformed(*args):
        row = await confirmation._apply(*args)
        row['fields'] = fields
        return row

    confirmation.apply.side_effect = malformed
    with pytest.raises(TypeError):
        await service.generate(payload(), request_uuid='synthetic')
    profile.read.assert_not_awaited()


@pytest.mark.asyncio
async def test_compensation_ignores_foreign_commands_and_continues_past_rejected_results():
    port = FakeBasicInfoPort()
    now = datetime.now(timezone.utc)
    ids = ('other-consumer:one', 'health-analysis:blocked', 'health-analysis:next')
    rows = [
        await port.register('test-user', operation, values=payload_info().values(), confirmed_at=now)
        for operation in ids
    ]

    async def apply(user, command):
        result = await port._apply(user, command)
        if command == rows[1]['commandId']:
            result['status'] = 'blocked'
            result['fields']['name']['status'] = 'blocked'
        return result

    port.apply.side_effect = apply
    worker = BasicInfoCompensationWorker(port)
    assert await worker.run_once() == 2
    assert [call.args[1] for call in port.apply.await_args_list] == [row['commandId'] for row in rows[1:]]
