"""Fixed-scope source adapters preserve identity and expected error boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.health_analysis.chat_history import ChatHistoryMaterialsAdapter
from adapters.health_analysis.profile import ProfileMaterialsAdapter
from health_profile.document_archive_contracts import ProfileDocumentArchiveChangedError
from modules.chat_history.contracts import ChatHistoryQueryError
from modules.health_analysis.contracts import AnalysisCapacityError
from modules.health_analysis.contracts import AnalysisSourceScope
from modules.health_analysis.contracts import SourceUnavailableError

SCOPE = AnalysisSourceScope('test-user', 'test-group', patient_sender_ids=('patient',))


def message(identity, content, *, sender='patient', time=1000, kind=101, revoked=False):
    return {
        'chatLog': {
            'serverMsgID': identity,
            'sendID': sender,
            'sendTime': time,
            'contentType': kind,
            'content': content,
        },
        'isRevoked': revoked,
    }


def tool(rows):
    return SimpleNamespace(handler=AsyncMock(return_value={'chatLogs': rows, 'chatLogsNum': 999}))


@pytest.mark.asyncio
async def test_chat_uses_one_verified_group_query_sorts_and_deduplicates_without_claiming_complete_history():
    rows = [message('later', '合成原文乙', time=2000), message('earlier', '合成原文甲'), message('later', '合成原文乙')]
    provider = tool(rows)
    result = await ChatHistoryMaterialsAdapter(provider).read(SCOPE)
    provider.handler.assert_awaited_once_with({'groupId': 'test-group'}, None)
    assert [item['content'] for item in result['materials']] == ['合成原文甲', '合成原文乙']
    assert result['coverage']['completeHistory'] is False
    assert result['coverage']['upstreamReportedCount'] == 999
    assert all(item['metadata']['statementType'] == 'patient_message_not_verified_fact' for item in result['materials'])
    assert result['sourceSnapshot']['messages'] == result['materials']


@pytest.mark.asyncio
async def test_revocations_unknown_senders_system_messages_and_attachments_are_not_personal_text():
    rows = [
        message('revoked', '合成敏感原文'),
        message('revoked', '合成敏感原文', revoked=True),
        message('staff', '合成工作人员建议', sender='staff'),
        message('unknown', '合成未知成员', sender='other'),
        message('system', '合成系统正文', kind=1001),
        message('image', 'https://synthetic.invalid/image', kind=102),
        message('file', 'https://synthetic.invalid/file', kind=105),
    ]
    result = await ChatHistoryMaterialsAdapter(tool(rows)).read(SCOPE)
    assert result['materials'] == []
    assert result['status'] == 'empty'
    assert result['coverage']['ignored'] == {'revoked': 1, 'unverified_sender': 2, 'unsupported_message_type': 1}
    assert len(result['coverage']['attachmentHints']) == 2
    assert '合成敏感原文' not in str(result)


@pytest.mark.asyncio
async def test_absent_mapping_never_defaults_to_user_id_and_conflicting_identity_is_not_adopted():
    scope = AnalysisSourceScope('patient', 'test-group')
    assert not (await ChatHistoryMaterialsAdapter(tool([message('a', '合成正文')])).read(scope))['materials']
    result = await ChatHistoryMaterialsAdapter(tool([message('same', '合成甲'), message('same', '合成乙')])).read(SCOPE)
    assert not result['materials']
    assert result['coverage']['ignored']['conflicting_message_identity'] == 1


@pytest.mark.asyncio
async def test_cross_group_data_and_message_capacity_are_explicit_errors():
    row = message('a', '合成正文')
    row['chatLog']['groupID'] = 'other-group'
    with pytest.raises(PermissionError):
        await ChatHistoryMaterialsAdapter(tool([row])).read(SCOPE)
    with pytest.raises(AnalysisCapacityError):
        await ChatHistoryMaterialsAdapter(tool([message('a', '甲'), message('b', '乙')]), max_messages=1).read(SCOPE)


@pytest.mark.asyncio
async def test_server_and_client_ids_use_distinct_namespaces():
    client = message('same-value', '客户端定位正文')
    client['chatLog']['clientMsgID'] = client['chatLog'].pop('serverMsgID')
    result = await ChatHistoryMaterialsAdapter(tool([message('same-value', '服务端定位正文'), client])).read(SCOPE)
    assert len(result['materials']) == len({row['id'] for row in result['materials']}) == 2


@pytest.mark.asyncio
async def test_expected_chat_dependency_error_is_safe_and_program_permission_cancel_propagate():
    provider = tool([])
    provider.handler.side_effect = ChatHistoryQueryError('timeout')
    with pytest.raises(SourceUnavailableError) as captured:
        await ChatHistoryMaterialsAdapter(provider).read(SCOPE)
    assert captured.value.category == 'chat_timeout'
    for error in (TypeError('synthetic program'), PermissionError('synthetic permission'), asyncio.CancelledError()):
        provider.handler.side_effect = error
        with pytest.raises(type(error)) as captured:
            await ChatHistoryMaterialsAdapter(provider).read(SCOPE)
        assert captured.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'payload',
    [
        None,
        {},
        {'chatLogs': [], 'chatLogsNum': False},
        {'chatLogs': [], 'chatLogsNum': -1},
        {'chatLogs': [None], 'chatLogsNum': 1},
        {'chatLogs': [{'chatLog': []}], 'chatLogsNum': 1},
        {'chatLogs': [{'chatLog': {}, 'isRevoked': 'false'}], 'chatLogsNum': 1},
    ],
)
async def test_malformed_chat_response_is_an_expected_source_failure(payload):
    provider = SimpleNamespace(handler=AsyncMock(return_value=payload))
    with pytest.raises(SourceUnavailableError) as captured:
        await ChatHistoryMaterialsAdapter(provider).read(SCOPE)
    assert captured.value.category == 'chat_invalid_response'


@pytest.mark.asyncio
async def test_profile_translates_public_snapshot_without_resolving_or_creating_private_resources():
    payload = {
        'scope': {'userId': 'test-user', 'subjectId': 'test-self'},
        'materials': [{'id': 'report:1', 'kind': 'report', 'content': {'resultValue': 0}, 'metadata': {}}],
        'gender': 'male',
        'status': 'partial',
        'coverage': {'complete': False},
        'sourceSnapshot': {'archives': [{'result': {'observations': [{'resultValue': 0}]}}]},
    }
    provider = SimpleNamespace(read=AsyncMock(return_value=payload))
    result = await ProfileMaterialsAdapter(provider).read(SCOPE)
    provider.read.assert_awaited_once_with(user_id='test-user', subject_id=None)
    assert result == payload and result is not payload
    result['materials'][0]['content']['resultValue'] = 1
    assert payload['materials'][0]['content']['resultValue'] == 0


@pytest.mark.asyncio
async def test_profile_expected_error_is_unavailable_but_scope_program_cancel_errors_propagate():
    provider = SimpleNamespace(read=AsyncMock(side_effect=ProfileDocumentArchiveChangedError('synthetic changed')))
    with pytest.raises(SourceUnavailableError):
        await ProfileMaterialsAdapter(provider).read(SCOPE)
    for error in (TypeError('synthetic program'), PermissionError('synthetic permission'), asyncio.CancelledError()):
        provider.read.side_effect = error
        with pytest.raises(type(error)) as captured:
            await ProfileMaterialsAdapter(provider).read(SCOPE)
        assert captured.value is error
    provider.read.side_effect = None
    provider.read.return_value = {'scope': {'userId': 'other', 'subjectId': 's'}, 'materials': [], 'status': 'empty'}
    with pytest.raises(PermissionError):
        await ProfileMaterialsAdapter(provider).read(SCOPE)


@pytest.mark.asyncio
async def test_nonempty_profile_material_cannot_omit_existing_self_scope():
    provider = SimpleNamespace(
        read=AsyncMock(
            return_value={
                'scope': None,
                'materials': [{'id': 'report:1', 'kind': 'report', 'content': {'resultValue': 0}}],
                'status': 'available',
            }
        )
    )
    with pytest.raises(TypeError, match='requires an existing self scope'):
        await ProfileMaterialsAdapter(provider).read(SCOPE)
    provider.read.return_value = {'scope': None, 'materials': [], 'status': 'empty'}
    assert (await ProfileMaterialsAdapter(provider).read(SCOPE))['materials'] == []


def report_payload(raw, *, material_extraction='fixed-1', archive_extraction='fixed-1', content=None):
    return {
        'scope': {'userId': 'test-user', 'subjectId': 'test-self'},
        'materials': [
            {
                'id': 'report:context',
                'kind': 'report',
                'content': {'itemName': '合成检测', 'resultValue': 0} if content is None else content,
                'metadata': {'extractionId': material_extraction, 'sourcePointer': '/observations/0'},
            }
        ],
        'gender': None,
        'status': 'available',
        'coverage': {},
        'sourceSnapshot': {
            'archives': [
                {
                    'descriptor': {
                        'user_id': 'test-user',
                        'subject_id': 'test-self',
                        'extraction_id': archive_extraction,
                        'archive_status': 'ready',
                        'source_status': 'active',
                    },
                    'result': raw,
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_fixed_report_age_and_stage_reach_generation_without_identity_or_report_gender():
    from tests.unit._support.health_analysis import setup as application_setup

    raw = {
        'basicInfo': {'age': 42, 'phone': 'synthetic-private-phone'},
        'subject': {'gender': 'female', 'name': 'synthetic-private-name', 'pregnant': False},
        'processing': {'stage': 'running'},
        'observations': [{'itemName': '合成检测', 'resultValue': 0}],
    }
    payload = report_payload(raw)
    payload['scope']['lifecycleEpoch'] = 1
    service, _, _, _, generator, _ = application_setup(has_profile=True)
    service.profile_reader = ProfileMaterialsAdapter(SimpleNamespace(read=AsyncMock(return_value=payload)))
    await service.generate(
        {
            'userId': 'test-user',
            'name': '合成用户',
            'age': 42,
            'gender': '男',
            'groupId': 'test-group',
            'surveyList': [],
        },
        request_uuid='context-run',
    )
    snapshot = generator.generate.call_args.args[0]
    context = snapshot['personal_materials'][0]['content']['reportContext']
    assert context == [
        {'field': 'age', 'value': 42, 'pointer': '/basicInfo/age'},
        {'field': 'pregnant', 'value': False, 'pointer': '/subject/pregnant'},
    ]
    assert snapshot['profile_gender'] == 'male'
    assert snapshot['personal_materials'][0]['content']['resultValue'] == 0
    assert 'synthetic-private-name' not in str(snapshot['personal_materials'])
    assert 'synthetic-private-phone' not in str(snapshot['personal_materials'])
    assert 'reportContext' not in payload['materials'][0]['content']


@pytest.mark.asyncio
async def test_old_contribution_never_borrows_new_archive_background_and_empty_materials_stay_empty():
    payload = report_payload(
        {'basicInfo': {'age': 42}}, material_extraction='fixed-old', archive_extraction='fixed-new'
    )
    payload['materials'][0]['metadata']['origin'] = 'source_contribution'
    provider = SimpleNamespace(read=AsyncMock(return_value=payload))
    result = await ProfileMaterialsAdapter(provider).read(SCOPE)
    assert 'reportContext' not in result['materials'][0]['content']
    assert result['materials'][0]['metadata']['extractionId'] == 'fixed-old'
    payload['materials'] = []
    payload['status'] = 'empty'
    assert (await ProfileMaterialsAdapter(provider).read(SCOPE))['materials'] == []


@pytest.mark.asyncio
async def test_report_context_uses_only_root_and_the_actual_observation_section():
    payload = report_payload(
        {
            'basicInfo': {'ageYears': 42},
            'sections': [
                {'basicInfo': {'ageYears': 12, 'specialStage': '无关分节阶段'}},
                {'patientInfo': {'ageMonths': 3, 'ageUnit': '月', 'lifeStage': '婴儿期'}},
            ],
        }
    )
    payload['materials'][0]['metadata']['sourcePointer'] = '/sections/1/observations/0'
    result = await ProfileMaterialsAdapter(SimpleNamespace(read=AsyncMock(return_value=payload))).read(SCOPE)
    context = result['materials'][0]['content']['reportContext']
    assert context == [
        {'field': 'ageYears', 'value': 42, 'pointer': '/basicInfo/ageYears'},
        {'field': 'ageMonths', 'value': 3, 'pointer': '/sections/1/patientInfo/ageMonths'},
        {'field': 'ageUnit', 'value': '月', 'pointer': '/sections/1/patientInfo/ageUnit'},
        {'field': 'lifeStage', 'value': '婴儿期', 'pointer': '/sections/1/patientInfo/lifeStage'},
    ]
    assert '无关分节阶段' not in str(context)


@pytest.mark.asyncio
async def test_primitive_observation_keeps_original_content_when_explicit_background_is_attached():
    payload = report_payload({'basicInfo': {'ageDays': 0}}, content='合成临床原文')
    result = await ProfileMaterialsAdapter(SimpleNamespace(read=AsyncMock(return_value=payload))).read(SCOPE)
    assert result['materials'][0]['content'] == {
        'observation': '合成临床原文',
        'reportContext': [{'field': 'ageDays', 'value': 0, 'pointer': '/basicInfo/ageDays'}],
    }


@pytest.mark.asyncio
async def test_http_service_scope_uses_independent_sender_mapping_after_confirmation():
    from tests.unit._support.health_analysis import payload as request_payload
    from tests.unit._support.health_analysis import setup

    service, confirmation, _, _, generator, writer = setup()
    mapping = SimpleNamespace(read=AsyncMock(return_value=('mapped-patient',)))
    service.chat_reader = ChatHistoryMaterialsAdapter(
        tool([message('m', '合成本人自述', sender='mapped-patient')]), patient_sender_reader=mapping
    )
    await service.generate(request_payload(), request_uuid='synthetic')
    mapping.read.assert_awaited_once_with(user_id='test-user', group_id='test-group')
    assert confirmation.register.await_count == 1
    assert generator.generate.call_args.args[0]['has_personal_data'] is True
    coverage = writer.records[0]['sourceSnapshots']['chat']['coverage']
    assert coverage['patientSenderIds'] == ['mapped-patient'] and coverage['senderMappingAvailable']


@pytest.mark.asyncio
async def test_invalid_sender_mapping_is_not_silently_replaced_by_user_id():
    mapping = SimpleNamespace(read=AsyncMock(return_value='test-user'))
    with pytest.raises(TypeError):
        await ChatHistoryMaterialsAdapter(tool([]), patient_sender_reader=mapping).read(SCOPE)
