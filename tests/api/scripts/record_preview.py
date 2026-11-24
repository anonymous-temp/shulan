"""Temporary, stateless Record preview server. See tests/api/record-preview.md."""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import ipaddress
import logging
import os
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tornado.ioloop
import tornado.web

from modules.record.application import lifestyle
from tests.api.scripts.record_preview_questions import report_evidence
from tests.api.scripts.record_preview_questions import report_question_copy
from utils.report_files import REPORT_EXTENSIONS

PAGES = Path(__file__).resolve().parents[1] / 'pages'
MAX_FILES = 5
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
LIFESTYLE = (
    'diet_structure',
    'meal_pattern',
    'exercise_frequency',
    'sleep_duration',
    'smoking',
    'alcohol',
    'stress_coping',
)
GOALS = ('便秘', '反酸', '入睡困难', '容易疲劳', '减轻体重', '增加肌肉', '肩颈疼痛', '缓解压力', '不清楚')
HISTORY = {
    'medical_history': ('高血压', '糖尿病', '血脂异常', '冠心病', '脑卒中', '慢性肾病', '脂肪肝', '不清楚'),
    'family_history': ('高血压', '糖尿病', '冠心病', '脑卒中', '肺癌', '结直肠癌', '地中海贫血', '血友病', '不清楚'),
}
FILE_SIGNATURES = {
    'jpeg': (b'\xff\xd8\xff',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'gif': (b'GIF87a', b'GIF89a'),
    'bmp': (b'BM',),
}


def choice_question(name, title, options, *, multiple=False, none_text=None):
    question = {
        'name': name,
        'title': title,
        'type': 'checkbox' if multiple else 'radiogroup',
        'choices': [{'value': label, 'text': label} for label in options],
    }
    if none_text is not None:
        question.update(showNoneItem=True, noneText=none_text)
    return question


def preview_questionnaire(reports=(), plan=None, *, evidence=None):
    plan = plan if isinstance(plan, dict) else {}
    evidence = report_evidence(reports) if evidence is None else evidence
    opening, followups, bridges, guidance = report_question_copy(evidence, plan)
    background = [
        choice_question(
            'health_goals',
            '您最近有哪些具体不适，或希望改善哪些方面？',
            GOALS,
            multiple=True,
            none_text='暂无上述需求',
        ),
    ]
    background[0]['description'] = opening if reports else '先从您目前最关心的健康问题聊起。'
    for name, title in (
        ('medical_history', '医生曾明确诊断过您有以下哪些疾病？'),
        ('family_history', '您的血亲中有人患过以下哪些疾病？'),
    ):
        question = choice_question(name, title, HISTORY[name], multiple=True, none_text='以上均无')
        question['description'] = bridges.get(name) or (
            '再了解一下您以往经医生确诊的情况。' if name == 'medical_history' else '接下来了解您的血亲家族健康背景。'
        )
        background.append(question)
    selected = plan.get('lifestyleKeys')
    selected = selected if isinstance(selected, list) else []
    keys = [key for key in selected if isinstance(key, str) and key in lifestyle.TOPICS]
    keys = list(dict.fromkeys([*keys, *LIFESTYLE]))[: len(LIFESTYLE)]
    daily = [
        dict(
            choice_question(key, topic['question'], topic['options'], multiple=topic['questionType'] == 'checkbox'),
            description=bridges.get(key) or lifestyle.BRIDGES[key],
        )
        for key in keys
        for topic in (lifestyle.TOPICS[key],)
    ]
    pages = [{'name': 'health_background', 'elements': background}, {'name': 'daily_habits', 'elements': daily}]
    if followups:
        pages.insert(
            1,
            {
                'name': 'report_follow_up',
                'elements': [
                    dict(
                        choice_question(f'report_follow_up_{index}', title, item['choices'], multiple=item['multiple']),
                        description=item['description'],
                    )
                    for index, (title, item) in enumerate(followups, 1)
                ],
            },
        )
    return {
        'questionnaire': {'title': '健康补充问卷' if reports else '健康问卷', 'pages': pages},
        'guidance': guidance,
    }


@dataclass(frozen=True)
class Upload:
    filename: str
    content: bytes
    file_type: str


def validate_uploads(files):
    if not 1 <= len(files) <= MAX_FILES:
        raise ValueError('请选择 1 至 5 份体检报告。')
    if sum(len(file.body) for file in files) > MAX_UPLOAD_BYTES:
        raise ValueError('报告总大小不能超过 50 MB。')
    uploads = []
    for file in files:
        filename = file.filename.replace('\\', '/').rsplit('/', 1)[-1]
        suffix = Path(filename).suffix.lower().lstrip('.')
        if suffix not in REPORT_EXTENSIONS or not file.body or len(file.body) > MAX_FILE_BYTES:
            raise ValueError('仅支持非空的 PDF、JPG、PNG、GIF、BMP 文件，每份最多 15 MB。')
        kind = 'jpeg' if suffix == 'jpg' else suffix
        valid = (
            file.body[:1024].lstrip().startswith(b'%PDF-')
            if kind == 'pdf'
            else file.body.startswith(FILE_SIGNATURES[kind])
        )
        if not valid:
            raise ValueError('文件内容和扩展名不一致，请确认后重试。')
        uploads.append(Upload(filename, file.body, suffix))
    return uploads


Interpreter = Callable[[Upload], Awaitable[dict]]


class PreviewHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header('Cache-Control', 'no-store')
        self.set_header('X-Content-Type-Options', 'nosniff')
        self.set_header('Referrer-Policy', 'no-referrer')
        self.set_header(
            'Content-Security-Policy',
            "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        )

    def prepare(self):
        password = self.settings['preview_password']
        if not password:
            return
        header = self.request.headers.get('Authorization', '')
        try:
            scheme, encoded = header.split()
            actual = base64.b64decode(encoded, validate=True) if scheme.lower() == 'basic' else b''
        except (ValueError, binascii.Error):
            actual = b''
        if not hmac.compare_digest(actual, f'preview:{password}'.encode()):
            self.set_status(401)
            self.set_header('WWW-Authenticate', 'Basic realm="Record Preview"')
            self.finish({'error': '请输入访问凭据。'})

    def write_error(self, status_code, **kwargs):
        self.finish(
            {
                'error': {
                    403: '请从预览页面上传报告。',
                    404: '未找到该页面。',
                    405: '不支持此操作。',
                }.get(status_code, '请求未能完成，请重试。')
            }
        )


class AssetHandler(PreviewHandler):
    def initialize(self, filename, content_type):
        self.filename = filename
        self.content_type = content_type

    def get(self):
        self.set_header('Content-Type', self.content_type)
        self.finish((PAGES / self.filename).read_bytes())


class QuestionnaireHandler(PreviewHandler):
    def get(self):
        self.finish(preview_questionnaire()['questionnaire'])


class ReportPreviewHandler(PreviewHandler):
    def get(self):
        self.finish({'reports': [], **preview_questionnaire()})

    async def post(self):
        if self.request.headers.get('X-Record-Preview') != '1':
            raise tornado.web.HTTPError(403)
        try:
            uploads = validate_uploads(self.request.files.get('reports', []))
        except ValueError as error:
            self.set_status(400)
            self.finish({'error': str(error)})
            return
        if self.settings['preview_busy']:
            self.set_status(429)
            self.finish({'error': '正在处理另一批报告，请稍后重试。'})
            return
        interpreter = self.settings['preview_interpreter']
        planner = self.settings.get('preview_question_planner')
        if interpreter is None:
            from tests.api.scripts.record_preview_live import PreviewConfigurationError
            from tests.api.scripts.record_preview_live import create_interpreter

            try:
                interpreter, planner = create_interpreter()
            except PreviewConfigurationError as error:
                self.set_status(503)
                self.finish({'error': str(error)})
                return
            self.settings['preview_interpreter'] = interpreter
            self.settings['preview_question_planner'] = planner
        self.settings['preview_busy'] = True
        reports = []
        usable = []
        try:
            for upload in uploads:
                try:
                    interpretation = await interpreter(upload)
                except Exception:
                    reports.append({'filename': upload.filename, 'error': '报告解读未完成，可重试或继续查看基础问卷。'})
                    continue
                reports.append({'filename': upload.filename, 'interpretation': interpretation})
                if interpretation.get('processing', {}).get('status') in ('complete', 'partial'):
                    usable.append(dict(interpretation, _preview_report_number=len(reports)))
            evidence = report_evidence(usable)
            plan = await planner(evidence) if planner is not None and evidence else None
            self.finish({'reports': reports, **preview_questionnaire(usable, plan, evidence=evidence)})
        finally:
            self.settings['preview_busy'] = False


def preview_routes(*, include_index=False):
    page = dict(filename='record-preview.html', content_type='text/html; charset=utf-8')
    routes = [
        (r'/record-preview\.html', AssetHandler, page),
        (
            r'/record-preview\.js',
            AssetHandler,
            dict(filename='record-preview.js', content_type='text/javascript; charset=utf-8'),
        ),
        (r'/questionnaire', QuestionnaireHandler),
        (r'/preview', ReportPreviewHandler),
    ]
    if include_index:
        routes.insert(0, (r'/', AssetHandler, page))
    return routes


def create_app(interpreter: Interpreter, *, password='', planner=None):
    return tornado.web.Application(
        preview_routes(include_index=True),
        preview_interpreter=interpreter,
        preview_question_planner=planner,
        preview_password=password,
        preview_busy=False,
        log_function=lambda request: None,
    )


def valid_preview_password(password):
    return len(password) >= 16 and password.isascii() and password.isprintable()


async def demo_interpretation(_upload):
    return {
        'title': '本地演示报告（合成数据）',
        'reportTime': '2026-01-01',
        'aiSummary': '以下检查值为示例，不来自上传文件。',
        'observations': [
            {'itemName': '空腹血糖', 'resultValue': '6.3', 'unit': 'mmol/L', 'referenceRange': '3.9–6.1', 'flag': 'H'},
        ],
        'processing': {'status': 'complete'},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1', help='IP address to bind')
    parser.add_argument('--port', type=int, default=9081)
    parser.add_argument('--demo', action='store_true', help='local synthetic interpretation; no external calls')
    args = parser.parse_args()
    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        parser.error('--host must be an IP address')
    password = os.environ.get('RECORD_PREVIEW_PASSWORD', '')
    if not loopback and (args.demo or not password):
        parser.error('non-loopback access requires live mode and RECORD_PREVIEW_PASSWORD')
    if password and not valid_preview_password(password):
        parser.error('RECORD_PREVIEW_PASSWORD must contain at least 16 printable ASCII characters')
    logging.disable(logging.CRITICAL)
    interpreter = demo_interpretation
    planner = None
    if not args.demo:
        from tests.api.scripts.record_preview_live import create_interpreter

        try:
            interpreter, planner = create_interpreter()
        except ValueError as error:
            parser.error(str(error))
    server = create_app(interpreter, password=password, planner=planner).listen(
        args.port,
        address=args.host,
        max_body_size=MAX_BODY_BYTES,
        body_timeout=60,
    )
    address = f'[{args.host}]' if ':' in args.host else args.host
    print(f'Record preview: http://{address}:{args.port}/' + (' (demo)' if args.demo else ''), flush=True)
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == '__main__':
    main()
