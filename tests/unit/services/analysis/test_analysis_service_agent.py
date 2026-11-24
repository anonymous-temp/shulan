"""HealthReportInterpreter 接入 AgentHub 的无网络测试。"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

import httpx

from adapters.analysis import create_application_document_parsing_client
from adapters.analysis.image_probe import HttpImageFormatProbe
from modules.analysis.application import HealthReportUnsupportedFormatError
from modules.analysis.application import HealthReportUpstreamError
from modules.analysis.ports import ImageProbeError
from modules.analysis.ports import UnsupportedImageFormatError
from services.analysis import HealthReportInterpreter
from services.analysis import HealthReportProcessingService
from tests.unit._support.document_profile_composition.analysis.analysis_service_agent import (
    FakeDocMindClient as FakeDocMindClient,
)
from tests.unit._support.document_profile_composition.analysis.analysis_service_agent import (
    RecordingAnalysisGraph as RecordingAnalysisGraph,
)
from tests.unit._support.document_profile_composition.analysis.analysis_service_agent import (
    _build_runtime as _build_runtime,
)
from tests.unit.services.analysis.fakes import InMemoryDocumentTraceStore


def _build_processing_service(probe=None):
    result = {'reportType': 'lab', 'title': '血常规', 'status': 'normal', 'expansion': []}
    runtime, _ = _build_runtime(json.dumps(result, ensure_ascii=False))
    client = FakeDocMindClient(result={'success': True, 'text': '|项目|结果|\n|---|---|\n|血红蛋白|120|\n'})
    source = SimpleNamespace(
        resolve_report_id=lambda value: value or 'report-1',
        source_fingerprint=lambda **values: values['file_id'],
        get_existing=AsyncMock(return_value=None),
        save=AsyncMock(return_value=SimpleNamespace(record=SimpleNamespace(public_result=lambda: result))),
    )
    service = HealthReportProcessingService(
        interpreter=HealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=client),
        report_source=source,
        health_profile_service=SimpleNamespace(
            ensure_self_profile=AsyncMock(return_value=SimpleNamespace(subject=SimpleNamespace(subject_id='self-1'))),
        ),
        image_format_probe=probe,
        document_trace_store=InMemoryDocumentTraceStore(),
    )
    return service, client, source


class HealthReportInterpreterAgentTest(unittest.IsolatedAsyncioTestCase):
    def test_docmind_adapter_uses_shulan_config(self):
        runtime, _ = _build_runtime('{}')
        config_env = ModuleType('config.env')
        config_env.DOCMIND_ACCESSKEY_ID = 'access-key-id'
        config_env.DOCMIND_ACCESSKEY_SECRET = 'access-key-secret'
        config_env.DOCMIND_ENDPOINT = 'docmind-api.cn-hangzhou.aliyuncs.com'
        config_env.LOG_LEVEL = 'CRITICAL'

        with (
            patch.dict(sys.modules, {'config.env': config_env}),
            patch(
                'adapters.analysis.docmind.BoundedDocMindClient',
            ) as client_class,
        ):
            client = create_application_document_parsing_client()

        client_class.assert_called_once_with(
            access_key_id='access-key-id',
            access_key_secret='access-key-secret',
            endpoint='docmind-api.cn-hangzhou.aliyuncs.com',
        )
        self.assertIs(client, client_class.return_value)

    async def test_analyze_uses_agent_hub_and_preserves_normalized_result(self):
        raw_result = {
            'reportType': 'lab',
            'title': '血常规',
            'reportTime': '2026-08-13',
            'aiSummary': '一项异常',
            'status': 'abnormal',
            'expansion': [
                {
                    'itemName': '血红蛋白',
                    'resultValue': '120',
                    'referenceRange': '130-175',
                    'unit': 'g/L',
                    'flag': '偏低',
                    'kind': 'measurement',
                    'ignored': '不会进入结果',
                }
            ],
        }
        runtime, graph = _build_runtime(
            f'```json\n{json.dumps(raw_result, ensure_ascii=False)}\n```',
        )
        interpreter = HealthReportInterpreter(
            runtime,
            count_input_tokens=len,
            docmind_client=FakeDocMindClient(),
        )

        source_text = (
            '血常规 检测日期2026-08-13\n|项目|结果|参考范围|单位|结果评价|\n'
            '|---|---|---|---|---|\n|血红蛋白|120|130-175|g/L|偏低|\n'
        )
        result = await interpreter.analyze(
            source_text,
            user_id='user-1',
            session_id='analysis-session-1',
        )

        self.assertEqual(result['reportType'], 'lab')
        self.assertEqual(result['status'], 'abnormal')
        from utils.report_content import report_public_content

        # The model JSON is usable without source-reference annotations.
        self.assertNotIn('sourceId', result['expansion'][0])
        self.assertNotIn('evidence', result['expansion'][0])
        self.assertEqual(
            report_public_content(result)['expansion'],
            [
                {
                    'itemName': '血红蛋白',
                    'resultValue': '120',
                    'referenceRange': '130-175',
                    'unit': 'g/L',
                    'flag': '偏低',
                    'kind': 'measurement',
                }
            ],
        )
        call = graph.calls[0]
        self.assertIn('血红蛋白', call['input']['report_text'])
        self.assertLessEqual(len(call['input']['report_text']), interpreter.MAX_ANALYZE_INPUT_TOKENS)
        self.assertEqual(call['context'].user_id, 'user-1')
        self.assertEqual(call['context'].session_id, 'analysis-session-1')

    async def test_analyze_covers_the_entire_document_in_slices(self):
        runtime, graph = _build_runtime(
            '{"reportType":"unknown","status":"pending","observations":[]}',
        )
        interpreter = HealthReportInterpreter(
            runtime,
            count_input_tokens=len,
            docmind_client=FakeDocMindClient(),
        )
        interpreter._count_input_tokens = len
        interpreter.MAX_ANALYZE_INPUT_TOKENS = 1024

        await interpreter.analyze('a' * 700 + '\n' + 'b' * 700)

        self.assertEqual(len(graph.calls), 2)
        texts = [call['input']['report_text'] for call in graph.calls]
        self.assertIn('a' * 700, texts[0])
        self.assertIn('b' * 700, texts[1])
        self.assertTrue(all('中间内容省略' not in text for text in texts))

    async def test_invalid_agent_content_returns_unavailable(self):
        runtime, _ = _build_runtime('不是 JSON')
        interpreter = HealthReportInterpreter(
            runtime,
            count_input_tokens=len,
            docmind_client=FakeDocMindClient(),
        )

        self.assertEqual((await interpreter.analyze('报告正文'))['processing']['status'], 'unavailable')
        self.assertIsNone(await interpreter.analyze('   '))

    def test_extract_text_keeps_docmind_result_contract(self):
        runtime, _ = _build_runtime('{}')
        success_client = FakeDocMindClient()
        interpreter = HealthReportInterpreter(
            runtime,
            count_input_tokens=len,
            docmind_client=success_client,
        )

        self.assertEqual(
            interpreter.extract_text('https://example/report.pdf'),
            '报告正文',
        )
        self.assertEqual(
            success_client.submit_calls,
            [
                (
                    'https://example/report.pdf',
                    'report.pdf',
                    'pdf',
                )
            ],
        )
        self.assertEqual(success_client.result_calls, [('task-1', 1)])

        failed = HealthReportInterpreter(
            runtime,
            count_input_tokens=len,
            docmind_client=FakeDocMindClient(
                result={
                    'success': False,
                    'error': 'parse failed',
                }
            ),
        )
        self.assertIs(
            failed.extract_text('https://example/report.pdf'),
            False,
        )

        errored = HealthReportInterpreter(
            runtime,
            count_input_tokens=len,
            docmind_client=FakeDocMindClient(
                submit_error=RuntimeError('submit failed'),
            ),
        )
        self.assertIsNone(
            errored.extract_text('https://example/report.pdf'),
        )

    def test_extract_text_passes_query_filename_to_docmind(self):
        runtime, _ = _build_runtime('{}')
        client = FakeDocMindClient()
        interpreter = HealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=client)
        file_url = 'https://test.slmc.top/api/v1/file/download?objectName=8e53dd2690f25000382f47915cd33b66.jpg'

        self.assertEqual(interpreter.extract_text(file_url), '报告正文')
        self.assertEqual(
            client.submit_calls,
            [
                (
                    file_url,
                    '8e53dd2690f25000382f47915cd33b66.jpg',
                    'jpg',
                )
            ],
        )

    def test_extract_text_uses_explicit_type_for_extensionless_url(self):
        runtime, _ = _build_runtime('{}')
        client = FakeDocMindClient()
        interpreter = HealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=client)
        file_url = 'https://example.test/files/download/record-1'

        self.assertEqual(
            interpreter.extract_text(file_url, file_type='pdf'),
            '报告正文',
        )
        self.assertEqual(
            client.submit_calls,
            [
                (
                    file_url,
                    'health-report.pdf',
                    'pdf',
                )
            ],
        )

    async def test_processing_service_accepts_type_for_extensionless_url(self):
        raw_result = {
            'reportType': 'lab',
            'title': '血常规',
            'reportTime': '2026-08-18',
            'aiSummary': '未见明显异常',
            'status': 'normal',
            'expansion': [],
        }
        runtime, _ = _build_runtime(json.dumps(raw_result, ensure_ascii=False))
        client = FakeDocMindClient(result={'success': True, 'text': '|项目|结果|\n|---|---|\n|血红蛋白|120|\n'})
        interpreter = HealthReportInterpreter(runtime, count_input_tokens=len, docmind_client=client)

        class ReportRecord:
            def public_result(self):
                return {
                    **raw_result,
                    'reportId': 'qa-report-1',
                    'userId': 'user-1',
                    'subjectId': 'subject-self-1',
                }

        class ReportSource:
            @staticmethod
            def resolve_report_id(requested):
                return requested

            @staticmethod
            def source_fingerprint(**kwargs):
                return 'fingerprint-1'

            @staticmethod
            async def get_existing(**kwargs):
                return None

            @staticmethod
            async def save(**kwargs):
                return SimpleNamespace(record=ReportRecord())

        class ProfileService:
            @staticmethod
            async def ensure_self_profile(user_id):
                return SimpleNamespace(
                    subject=SimpleNamespace(subject_id='subject-self-1'),
                )

        service = HealthReportProcessingService(
            interpreter=interpreter,
            report_source=ReportSource(),
            health_profile_service=ProfileService(),
            document_trace_store=InMemoryDocumentTraceStore(),
        )

        result = await service.process(
            file_id='https://example.test/files/download/record-1',
            file_type='pdf',
            user_id='user-1',
            report_id='qa-report-1',
        )

        self.assertEqual(result['subjectId'], 'subject-self-1')
        self.assertEqual(
            client.submit_calls,
            [
                (
                    'https://example.test/files/download/record-1',
                    'health-report.pdf',
                    'pdf',
                )
            ],
        )


class ImageProcessingTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_image_category_resolves_every_supported_extension(self):
        for extension in ('jpg', 'jpeg', 'png', 'bmp', 'gif'):
            for url in (
                f'https://example.test/report.{extension}',
                f'https://example.test/download?objectName=report.{extension}',
            ):
                with self.subTest(url=url):
                    probe = SimpleNamespace(detect=AsyncMock())
                    service, client, source = _build_processing_service(probe)
                    await service.process(file_id=url, user_id='user-1', file_type='image')
                    self.assertEqual(client.submit_calls, [(url, f'report.{extension}', extension)])
                    self.assertEqual(source.get_existing.call_args.kwargs['source_fingerprint'], url)
                    probe.detect.assert_not_awaited()

    async def test_extensionless_images_use_probed_format_and_legacy_fingerprint(self):
        for extension in ('jpg', 'png', 'bmp', 'gif'):
            with self.subTest(extension=extension):
                probe = SimpleNamespace(detect=AsyncMock(return_value=extension))
                service, client, source = _build_processing_service(probe)
                url = 'https://example.test/download'
                await service.process(file_id=url, user_id='user-1', file_type='image')
                probe.detect.assert_awaited_once_with(url)
                self.assertEqual(client.submit_calls, [(url, f'health-report.{extension}', extension)])
                self.assertEqual(
                    source.get_existing.call_args.kwargs['source_fingerprint'], f'{url}#fileType={extension}'
                )
                if extension == 'jpg':
                    self.assertEqual(
                        source.get_existing.call_args.kwargs['source_fingerprint_aliases'], (f'{url}#fileType=jpeg',)
                    )

    async def test_reused_image_skips_parser_and_model(self):
        probe = SimpleNamespace(detect=AsyncMock(return_value='jpg'))
        service, client, source = _build_processing_service(probe)
        cached = {
            'reportId': 'existing-jpeg',
            'reportType': 'lab',
            'observations': [{'itemName': '血红蛋白', 'resultValue': '120'}],
        }
        source.get_existing.return_value = SimpleNamespace(record=SimpleNamespace(public_result=lambda: cached))
        result = await service.process(file_id='https://example.test/download', user_id='user-1', file_type='image')
        self.assertEqual({key: result[key] for key in cached}, cached)
        self.assertEqual(result['processing']['status'], 'complete')
        self.assertEqual(result['unresolvedItems'], [])
        self.assertEqual(client.submit_calls, [])
        source.save.assert_not_awaited()

    async def test_conflicting_or_unsupported_extensions_fail_before_parsing(self):
        for extension, declared, error in (
            ('pdf', 'image', ValueError),
            ('jpg', 'pdf', ValueError),
            ('webp', 'image', HealthReportUnsupportedFormatError),
            ('docx', None, HealthReportUnsupportedFormatError),
        ):
            with self.subTest(extension=extension, declared=declared):
                service, client, source = _build_processing_service()
                with self.assertRaises(error):
                    await service.process(
                        file_id=f'https://example.test/report.{extension}', user_id='user-1', file_type=declared
                    )
                self.assertEqual(client.submit_calls, [])
                source.save.assert_not_awaited()

    async def test_probe_errors_keep_input_format_and_upstream_failure_categories(self):
        for failure, expected in (
            (ValueError('类型不一致'), ValueError),
            (UnsupportedImageFormatError('格式不支持'), HealthReportUnsupportedFormatError),
            (ImageProbeError('下载失败'), HealthReportUpstreamError),
        ):
            with self.subTest(error=type(failure).__name__):
                probe = SimpleNamespace(detect=AsyncMock(side_effect=failure))
                service, client, source = _build_processing_service(probe)
                with self.assertRaises(expected):
                    await service.process(file_id='https://example.test/download', user_id='user-1', file_type='image')
                self.assertEqual(client.submit_calls, [])
                source.get_existing.assert_not_awaited()

    async def test_legacy_interpretation_still_infers_and_accepts_concrete_types(self):
        for declared in (None, 'jpg', 'jpeg'):
            with self.subTest(declared=declared):
                service, client, _ = _build_processing_service()
                await service.process(file_id='https://example.test/report.jpg', user_id='user-1', file_type=declared)
                self.assertEqual(client.submit_calls[0][2], 'jpg')


class ImageFormatProbeTest(unittest.IsolatedAsyncioTestCase):
    def make_probe(self, handler, **options):
        resolver = options.pop('resolver', AsyncMock(return_value=['93.184.216.34']))
        return HttpImageFormatProbe(transport=httpx.MockTransport(handler), resolver=resolver, **options)

    async def test_signatures_override_misleading_mime_and_preserve_tls_host(self):
        for prefix, extension in (
            (b'\xff\xd8\xff', 'jpg'),
            (b'\x89PNG\r\n\x1a\n', 'png'),
            (b'BM', 'bmp'),
            (b'GIF87a', 'gif'),
            (b'GIF89a', 'gif'),
        ):
            with self.subTest(extension=extension):

                def handler(request):
                    self.assertEqual(request.url.host, '93.184.216.34')
                    self.assertEqual(request.headers['host'], 'files.example.test')
                    self.assertEqual(request.extensions['sni_hostname'], 'files.example.test')
                    self.assertEqual(request.headers['range'], 'bytes=0-31')
                    return httpx.Response(
                        200, content=prefix + b'0' * 40, headers={'Content-Type': 'application/octet-stream'}
                    )

                probe = self.make_probe(handler)
                self.assertEqual(await probe.detect('https://files.example.test/download'), extension)

    async def test_rejects_private_dns_results_urls_and_redirects(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})

        for addresses in (
            ['127.0.0.1'],
            ['10.0.0.1'],
            ['169.254.169.254'],
            ['::1'],
            ['93.184.216.34', '192.168.1.1'],
            ['::ffff:93.184.216.34'],
        ):
            probe = self.make_probe(handler, resolver=AsyncMock(return_value=addresses))
            with self.subTest(addresses=addresses), self.assertRaises(ValueError):
                await probe.detect('https://files.example.test/download')
        for url in (
            'file:///tmp/report',
            'https://user:password@example.test/download',
            'https://example.test/download#fragment',
            'http://127.0.0.1/download',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.make_probe(handler).detect(url)
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(ValueError, '重定向'):
            await self.make_probe(handler).detect('https://files.example.test/download')
        self.assertEqual(len(calls), 1)

    async def test_probe_distinguishes_format_mismatch_and_download_failures(self):
        for response, expected in (
            (httpx.Response(200, content=b'%PDF-1.7'), ValueError),
            (httpx.Response(200, content=b'not an image'), UnsupportedImageFormatError),
            (httpx.Response(200, content=b'RIFF0000WEBP'), UnsupportedImageFormatError),
            (httpx.Response(200, content=b''), ImageProbeError),
            (httpx.Response(503), ImageProbeError),
            (httpx.Response(200, content=b'GIF89a', headers={'content-length': '100000000'}), ImageProbeError),
            (
                httpx.Response(206, content=b'GIF89a', headers={'content-range': 'bytes 0-31/100000000'}),
                ImageProbeError,
            ),
        ):
            with self.subTest(expected=expected.__name__, response=response), self.assertRaises(expected):
                await self.make_probe(lambda request: response).detect('https://example.test/download')

    async def test_probe_bounds_reading_and_closes_stream(self):
        class Stream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'GIF89a' + b'0' * 26
                raise AssertionError('probe must not read the full file')

            async def aclose(self):
                self.closed = True

        stream = Stream()
        probe = self.make_probe(lambda request: httpx.Response(200, stream=stream))
        self.assertEqual(await probe.detect('http://example.test/download'), 'gif')
        self.assertTrue(stream.closed)

    async def test_probe_total_timeout_includes_dns_and_stream(self):
        async def slow_resolver(hostname):
            await asyncio.Event().wait()

        probe = self.make_probe(
            lambda request: self.fail('must not send request'), resolver=slow_resolver, timeout_seconds=0.01
        )
        with self.assertRaises(ImageProbeError):
            await probe.detect('https://example.test/download')

        class SlowStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b'GIF89a'

            async def aclose(self):
                self.closed = True

        stream = SlowStream()
        probe = self.make_probe(lambda request: httpx.Response(200, stream=stream), timeout_seconds=0.05)
        with self.assertRaises(ImageProbeError):
            await probe.detect('https://example.test/download')
        self.assertTrue(stream.closed)

    async def test_compressed_transport_is_rejected_without_reading_body(self):
        class UnreadStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError('must reject compression before decoding')
                yield b''

        probe = self.make_probe(
            lambda request: httpx.Response(
                200,
                stream=UnreadStream(),
                headers={'content-encoding': 'gzip'},
            )
        )
        with self.assertRaisesRegex(ImageProbeError, '压缩传输'):
            await probe.detect('https://example.test/download')


if __name__ == '__main__':
    unittest.main()
