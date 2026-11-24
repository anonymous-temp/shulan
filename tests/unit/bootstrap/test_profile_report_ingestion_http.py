"""HTTP upload through real Analysis, Document and Profile, with offline providers."""

import json

import pytest
from tornado.httpclient import AsyncHTTPClient
from tornado.httpserver import HTTPServer
from tornado.netutil import bind_sockets
from tornado.web import Application

from adapters.analysis.analysis_current import create_analysis_current_processing_service
from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
from bootstrap.document_sources import _read_fixed_subject
from health_profile.report_fact_materials import ProfileReportFactsReader
from health_profile.service import HealthProfileService
from tests.unit._support.current_document import UPLOAD
from tests.unit._support.current_document import report
from tests.unit._support.current_document_profile import install_profile
from tests.unit._support.current_document_profile import settle
from tests.unit._support.current_occurrence import readers_for
from tests.unit._support.current_report_projection import composition
from tests.unit._support.document_profile_composition.analysis.document_formal_source_store import Collection
from web.handlers.analysis import HealthReportHandler
from web.handlers.health_profile import HealthProfileFactListHandler
from web.handlers.health_profile import HealthProfileGetHandler


@pytest.mark.asyncio
@pytest.mark.parametrize(('archive_table', 'missing_date'), [(False, False), (True, False), (False, True)])
async def test_public_analysis_confirms_resolved_report_observations_without_original_matching(
    monkeypatch, archive_table, missing_date
):
    raw = report(name=None, gender=None, age=None)
    raw['reportTime'] = None if missing_date else '2026-11-12'
    raw['observations'] = [{'itemName': 'ALT', 'resultValue': '12', 'unit': 'U/L', 'kind': 'measurement'}]
    raw['unresolvedItems'] = [
        {'itemName': 'AST', 'resultValue': None},
        {'itemName': 'CRP', 'resultValue': None},
    ]
    if archive_table or missing_date:
        raw['observations'][0].update(resultValue='51', flag='偏高', referenceRange='0-40')
    flow = await composition(monkeypatch, [raw, raw], png=True)
    if archive_table:
        flow.upstream.client.result = {
            'success': True,
            'text': '体检报告\n建议\nALT 40 U/L\n项目名称检查结果参考值单位ALT41 0-40 U/L',
            'data': {
                'layouts': [
                    {'type': 'text', 'pageNum': [0], 'text': '体检日期：2026-11-12'},
                    {
                        'type': 'table',
                        'pageNum': [0],
                        'numRow': 4,
                        'numCol': 4,
                        'cells': [
                            {
                                'xsc': index % 4,
                                'xec': index % 4,
                                'ysc': 0 if index < 4 else 3,
                                'yec': 2 if index < 4 else 3,
                                'layouts': [{'text': value}],
                            }
                            for index, value in enumerate(
                                ('项目名称', '检查结果', '参考值', '单位', 'ALT', '41', '0-40', 'U/L')
                            )
                        ],
                    },
                ],
            },
        }
    flow.readers = readers_for(flow)
    install_profile(flow)
    flow.runtime = create_document_basic_info_profile_runtime(
        readers=flow.readers, profile_inbox=flow.profile.inbox, clock=lambda: flow.now[0]
    )

    async def require_no_verification(**_kwargs):
        raise AssertionError('the public Analysis publication must skip original verification')

    monkeypatch.setattr(flow.runtime.consumption.evidence_projections, 'prepare', require_no_verification)
    commands = Collection()
    commands.with_options = lambda **options: commands

    def recreate():
        return create_analysis_current_processing_service(
            processing=flow.analysis,
            prepared=flow.prepared,
            consumption=flow.runtime.consumption,
            resolve_subject=lambda scope: _read_fixed_subject(flow.profile.reader, scope),
            command_records=commands,
        )

    current = recreate()
    profile = HealthProfileService(flow.repository, clock=lambda: flow.now[0])
    application = Application(
        [
            (r'/api/v1/interpret/health-report', HealthReportHandler, {'processing_service': current.service}),
            (r'/api/v1/health-profile/get', HealthProfileGetHandler, {'service': profile}),
            (r'/api/v1/health-profile/facts/list', HealthProfileFactListHandler, {'service': profile}),
        ]
    )
    sockets = bind_sockets(0, address='127.0.0.1')
    server = HTTPServer(application)
    server.add_sockets(sockets)
    client = AsyncHTTPClient(force_instance=True)
    origin = 'http://127.0.0.1:' + str(sockets[0].getsockname()[1])

    async def post(path, **fields):
        response = await client.fetch(
            origin + path,
            method='POST',
            headers={'Content-Type': 'application/json'},
            body=json.dumps({'userId': flow.scope.user_id, **fields}),
        )
        assert response.code == 200
        return json.loads(response.body)['data']

    try:
        profile_url = '/api/v1/health-profile/get'
        assert (await post(profile_url))['factGroups'] == []
        upload = await post('/api/v1/interpret/health-report', fileId=UPLOAD['file_id'].replace('.pdf', '.png'))
        assert upload['processing']['status'] == 'partial'
        assert upload['subjectId'] == flow.scope.subject_id
        assert await recreate().delivery.recover_pending() == 1
        await settle(flow)

        facts = (await post('/api/v1/health-profile/facts/list', statuses=['active', 'pending']))['items']
        by_name = {row['displayName']: row for row in facts}
        assert by_name['ALT']['verificationStatus'] == 'confirmed'
        assert by_name['ALT']['status'] == 'active'
        assert by_name['ALT']['assertionLevel'] == 'observed'
        assert by_name['ALT']['sources'][0]['confirmationBasis'] == 'analysis_public'
        if missing_date:
            assert by_name['ALT']['effectivePeriod']['start'] is None
            assert by_name['ALT']['sources'][0]['recordedAt']
        else:
            assert by_name['ALT']['effectivePeriod']['start'] is not None
        if archive_table or missing_date:
            assert by_name['ALT']['interpretation'] == 'high'
            assert (await post(profile_url))['alerts']
        for name in ('AST', 'CRP'):
            assert by_name[name]['status'] == 'pending'
            assert by_name[name]['verificationStatus'] == 'unconfirmed'
            assert by_name[name]['value'] is None
        assert (await post(profile_url))['overview']['pendingConfirmationCount'] >= 2
        materials = await ProfileReportFactsReader(flow.profile.reader).read(
            user_id=flow.scope.user_id, subject_id=flow.scope.subject_id, expected_lifecycle_epoch=flow.epoch
        )
        by_name = {row['content']['itemName']: row for row in materials['materials']}
        assert by_name['ALT']['content']['resultValue'] == (51 if archive_table or missing_date else 12)
        assert by_name['ALT'].get('status') != 'unknown'
        if missing_date:
            assert by_name['ALT']['content']['measuredAt'] is None
            assert by_name['ALT']['content']['recordedAt']
        assert by_name['AST']['status'] == by_name['CRP']['status'] == 'unknown'
    finally:
        client.close()
        server.stop()
        await server.close_all_connections()
