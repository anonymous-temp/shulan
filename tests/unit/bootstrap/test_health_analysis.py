"""Default new-domain wiring and the actual public Profile/Document composition."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.knowledge.protocol import DisabledKnowledgeGateway
from bootstrap.health_analysis import build_health_analysis_service
from modules.health_analysis.basic_info_sync import BasicInfoDeliveryError
from modules.health_analysis.policy import background_questions
from tests.unit._support.health_analysis import FakeBasicInfoPort
from tests.unit._support.health_analysis import StageModel
from tests.unit._support.health_analysis import Writer
from tests.unit._support.health_analysis import payload


class Gateway:
    def __init__(self):
        self.model = StageModel()

    async def complete(self, request):
        payload = json.loads(request.content.splitlines()[-1])
        stage = request.profile.name.rsplit('.', 1)[1]
        value = await self.model.run(stage, payload)
        return SimpleNamespace(
            content=json.dumps(value['output'], ensure_ascii=False), provider='offline', model='fake'
        )


def runtime():
    return SimpleNamespace(llm_gateway=Gateway(), knowledge_gateway=DisabledKnowledgeGateway())


@pytest.mark.asyncio
async def test_missing_confirmation_provider_is_explicit_and_call_slots_are_shared_by_model_and_knowledge():
    profile, chat, writer = AsyncMock(), AsyncMock(), Writer()
    service = build_health_analysis_service(runtime(), profile_reader=profile, chat_reader=chat, trace_writer=writer)
    core = service.generator.generator
    assert core.model.semaphore is core.factors.semaphore
    with pytest.raises(BasicInfoDeliveryError):
        await service.generate(payload(), request_uuid='test')
    profile.read.assert_not_awaited()
    chat.read.assert_not_awaited()
    assert not writer.records


@pytest.mark.asyncio
@pytest.mark.parametrize(('has_history', 'verify_report'), [(False, True), (True, True), (False, False)])
async def test_actual_profile_facts_and_gateway_compose_without_document_processing(
    monkeypatch, has_history, verify_report
):
    from copy import deepcopy

    from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
    from tests.unit._support.current_document import UPLOAD
    from tests.unit._support.current_document import model_report
    from tests.unit._support.current_document_profile import install_profile
    from tests.unit._support.current_document_profile import settle
    from tests.unit._support.current_occurrence import readers_for
    from tests.unit._support.current_report_projection import composition

    flow = await composition(monkeypatch, [model_report()], png=verify_report)
    upload = dict(UPLOAD)
    if verify_report:
        flow.readers = readers_for(flow)
        install_profile(flow)
        flow.runtime = create_document_basic_info_profile_runtime(
            readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
        )
        upload.update(file_id=UPLOAD['file_id'].replace('.pdf', '.png'), file_type='png')
    monkeypatch.setattr(
        flow.upstream.index_collection, 'with_options', lambda **kwargs: flow.upstream.index_collection, raising=False
    )
    prepared = await flow.prepared.prepare_bundle(scope=flow.scope, **upload)
    await flow.runtime.consumption.consume_prepared(
        scope=flow.scope, prepared=flow.prepared.input(prepared), lifecycle_epoch=flow.epoch, request_id='seed-analysis'
    )
    await settle(flow)
    confirmation = FakeBasicInfoPort(subject_id=flow.scope.subject_id)
    chat = AsyncMock()
    chat.read.return_value = {'materials': [], 'status': 'empty'}
    writer = Writer()
    service = build_health_analysis_service(
        runtime(),
        basic_info_sync=confirmation,
        profiles=flow.profile.reader,
        chat_reader=chat,
        trace_writer=writer,
    )
    before = deepcopy({name: coll.documents for name, coll in flow.cols.items()})
    calls = len(flow.gateway.complete_requests), len(flow.upstream.client.submit_calls)
    request = {**payload(), 'userId': flow.scope.user_id}
    if not has_history:
        request['surveyList'] = []
    result = await service.generate(request, request_uuid='test-combined')
    questions = result['advancedSurveySchema']['pages'][0]['elements']
    initial = background_questions()
    assert questions[: len(initial)] == initial
    if verify_report:
        assert len(questions) == 20
        assert all(q['name'].startswith('ha_') for q in questions[len(initial) :])
        assert result['healthReview']
    else:
        assert len(questions) == 13
        assert all(result[key] == '' for key in ('healthReview', 'suggestedMaterials', 'advancedQuestions'))
    saved = writer.records[0]
    assert saved['sourceScope']['lifecycleEpoch'] == flow.epoch
    assert saved['sourceSnapshots']['profile']['sourceSnapshot']['facts']
    assert saved['sourceSnapshots']['profile']['materials'][0]['metadata']['sources'][0]['pointer']
    assert saved['analysis']['effectiveHasPersonalData'] is verify_report
    model_calls = saved['modelIdentity']['calls']
    assert bool(model_calls) is verify_report
    assert all(row['promptHash'] for row in model_calls)
    assert {name: coll.documents for name, coll in flow.cols.items()} == before
    assert (len(flow.gateway.complete_requests), len(flow.upstream.client.submit_calls)) == calls


@pytest.mark.asyncio
async def test_real_profile_confirmation_creates_self_and_preserves_newest_after_restart(monkeypatch):
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    from pymongo.errors import AutoReconnect

    from adapters.health_analysis.trace import MongoRunTraceWriter
    from bootstrap.health_analysis import build_health_analysis_confirmation_port
    from modules.health_analysis.basic_info_sync import BasicInfoCompensationWorker
    from modules.health_profile.application import create_application_profile_public_contracts
    from tests.unit._support.current_document_profile import profile_scope
    from tests.unit.report.durable_fakes import Collection

    cols, _, _ = await profile_scope(monkeypatch)

    class TraceCollection(Collection):
        def with_options(self, **kwargs):
            return self

    traces = TraceCollection()
    profile = create_application_profile_public_contracts(basic_info_collection=cols['basic_info'])
    provider = profile.basic_info
    read_sources = AsyncMock()
    read_sources.read.return_value = {'materials': [], 'status': 'empty'}
    selected_runtime = runtime()
    service = build_health_analysis_service(
        selected_runtime,
        basic_info_sync=build_health_analysis_confirmation_port(provider),
        profile_reader=read_sources,
        chat_reader=read_sources,
        trace_writer=MongoRunTraceWriter(traces),
    )
    user = 'synthetic-profile-first-run'
    first_values = {**payload(), 'userId': user}
    confirmed = await service.generate(first_values, request_uuid='synthetic-first')
    assert set(confirmed) == {'healthReview', 'suggestedMaterials', 'advancedQuestions', 'advancedSurveySchema'}
    assert confirmed['healthReview'] == ''
    assert confirmed['suggestedMaterials'] == ''
    field_view = await provider.read(user)
    assert field_view.fields['name'].value == first_values['name']
    assert field_view.fields['age'].value == 42
    assert all(field.source_reliability == 'very_high' for field in field_view.fields.values())
    assert field_view.fields['age'].age_basis == 'confirmed_at_time'
    assert read_sources.read.await_args.args[0].lifecycle_epoch == field_view.lifecycle_epoch

    real_register = provider.register

    async def acknowledged_registration_but_no_response(*args, **kwargs):
        await real_register(*args, **kwargs)
        raise AutoReconnect('synthetic lost registration response')

    async def unreachable_application(*args, **kwargs):
        raise AutoReconnect('synthetic lost Profile connection')

    with monkeypatch.context() as outage:
        outage.setattr(provider, 'register', acknowledged_registration_but_no_response)
        outage.setattr(provider, 'apply', unreachable_application)
        deferred = await service.generate({**first_values, 'age': 43}, request_uuid='synthetic-deferred')
    assert deferred == confirmed
    deferred_trace = next(row for row in traces.documents.values() if row['record']['uuid'] == 'synthetic-deferred')
    snapshot = deferred_trace['record']
    pending = snapshot['basicInfoSync']['receipt']
    assert snapshot['basicInfoSync']['applicationAttempt'] == 'deferred'
    assert snapshot['confirmedBasicInfo']['age'] == 43
    assert snapshot['snapshot']['effective_demographics']['ageYears'] == 43
    assert (await provider.read(user)).fields['age'].value == 42

    latest = await service.generate({**first_values, 'age': 44}, request_uuid='synthetic-latest')
    assert latest == confirmed
    assert (await provider.read(user)).fields['age'].value == 44
    unrelated = await real_register(
        'synthetic-other-consumer',
        'other-consumer:fixed-confirmation',
        values={'name': 'Synthetic Other', 'gender': 'female', 'age': 35},
        confirmed_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert unrelated.status == 'registered'
    from copy import deepcopy

    initial_traces = deepcopy(traces.documents)
    source_calls = read_sources.read.await_count
    model_calls = len(selected_runtime.llm_gateway.model.calls)
    restarted = create_application_profile_public_contracts(basic_info_collection=cols['basic_info']).basic_info
    worker = BasicInfoCompensationWorker(build_health_analysis_confirmation_port(restarted))
    assert await worker.run_once() == 1
    assert await worker.run_once() == 0
    assert (await restarted.get(user, pending['operationId'])).status == 'superseded'
    assert (await restarted.get(unrelated.user_id, unrelated.operation_id)).status == 'registered'
    assert (await restarted.read(user)).fields['age'].value == 44
    assert traces.documents == initial_traces
    assert read_sources.read.await_count == source_calls
    assert len(selected_runtime.llm_gateway.model.calls) == model_calls


@pytest.mark.asyncio
async def test_real_confirmation_never_crosses_an_archived_self_lifecycle(monkeypatch):
    from datetime import UTC
    from datetime import datetime

    from bootstrap.health_analysis import build_health_analysis_confirmation_port
    from health_profile.models import SubjectStatus
    from modules.health_analysis.basic_info_sync import BasicInfoCompensationWorker
    from modules.health_profile.application import create_application_profile_public_contracts
    from tests.unit._support.current_document_profile import profile_scope

    cols, repository, scope = await profile_scope(monkeypatch)
    provider = create_application_profile_public_contracts(basic_info_collection=cols['basic_info']).basic_info
    initial = await provider.register(
        scope.user_id,
        'health-analysis:old-lifecycle',
        values={'name': 'Synthetic Old', 'gender': 'female', 'age': 19},
        confirmed_at=datetime.now(UTC),
    )
    for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        subject = await repository.get_subject(scope.user_id, scope.subject_id)
        await repository.update_subject(
            subject.model_copy(
                update={'status': status, 'version': subject.version + 1, 'updated_at': datetime.now(UTC)}
            ),
            expected_version=subject.version,
        )
    newest = await provider.register(
        scope.user_id,
        'health-analysis:current-lifecycle',
        values={'name': 'Synthetic Current', 'gender': 'male', 'age': 29},
        confirmed_at=datetime.now(UTC),
    )
    assert newest.lifecycle_epoch > initial.lifecycle_epoch
    worker = BasicInfoCompensationWorker(build_health_analysis_confirmation_port(provider))
    assert await worker.run_once() == 2
    assert await worker.run_once() == 0
    assert (await provider.get(initial.user_id, initial.operation_id)).status == 'blocked'
    assert (await provider.get(newest.user_id, newest.operation_id)).status == 'applied'
    current = await provider.read(scope.user_id)
    assert current.lifecycle_epoch == newest.lifecycle_epoch
    assert current.fields['name'].value == 'Synthetic Current'
    assert current.fields['age'].value == 29
