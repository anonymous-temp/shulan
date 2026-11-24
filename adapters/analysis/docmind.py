"""Bounded DocMind I/O; retries are owned by the durable document coordinator."""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

from aion.aliyun import docmind
from alibabacloud_docmind_api20220711 import models
from alibabacloud_tea_util import models as util_models

from modules.analysis.application.documents import DocumentSubmissionRejectedError
from modules.analysis.ports import DocumentParsingClient


class BoundedDocMindClient(docmind.DocMindClient):
    POLL_TIMEOUT_SECONDS = 180
    CONNECT_TIMEOUT_MS = 10000
    READ_TIMEOUT_MS = 30000

    def _runtime(self):
        return util_models.RuntimeOptions(
            autoretry=False,
            max_attempts=1,
            connect_timeout=self.CONNECT_TIMEOUT_MS,
            read_timeout=self.READ_TIMEOUT_MS,
        )

    @staticmethod
    def _submitted(call):
        try:
            return call().body.data.id
        except Exception as error:
            status = getattr(error, 'status_code', None)
            code = getattr(error, 'code', None)
            if status in (400, 401, 403, 404, 413, 415, 422) or code == 'InvalidCredentials':
                raise DocumentSubmissionRejectedError(
                    '文档解析服务拒绝了提交，请检查文件或服务配置', retryable=False
                ) from error
            raise

    def submit_local_file(self, file_path, *, file_name=None, file_type=None):
        # Opening a local file fails before any SDK call can accept a task.
        try:
            stream = open(file_path, 'rb')
        except OSError as error:
            raise DocumentSubmissionRejectedError('无法读取待解析文件') from error
        with stream:
            request = models.SubmitDocStructureJobAdvanceRequest(
                file_url_object=stream,
                file_name=file_name or Path(file_path).name,
                file_name_extension=file_type or Path(file_path).suffix.lstrip('.'),
            )
            return self._submitted(lambda: self.client.submit_doc_structure_job_advance(request, self._runtime()))

    def submit_online_file(self, file_url, *, file_name=None, file_type=None):
        path = Path(urlparse(file_url).path)
        request = models.SubmitDocStructureJobRequest(
            file_url=file_url,
            file_name=file_name or path.name,
            file_name_extension=file_type or path.suffix.lstrip('.'),
        )
        return self._submitted(lambda: self.client.submit_doc_structure_job_with_options(request, self._runtime()))

    def get_result(self, task_id, *, poll_interval=1):
        deadline = time.monotonic() + self.POLL_TIMEOUT_SECONDS
        request = models.GetDocStructureResultRequest(id=task_id)
        while time.monotonic() < deadline:
            response = self.client.get_doc_structure_result_with_options(request, self._runtime())
            body = response.body
            if body.completed:
                data = body.data
                if str(body.status).lower() != 'success':
                    return {'success': False, 'data': data, 'error': f'解析任务失败，状态: {body.status}'}
                try:
                    text = '\n'.join(layout['text'] for layout in data['layouts'])
                    return {'success': True, 'data': data, 'text': text}
                except (KeyError, TypeError):
                    return {'success': False, 'data': data, 'error': '解析任务缺少有效正文'}
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(max(poll_interval, 0.1), remaining))
        raise TimeoutError('文档解析任务本次查询超时，保留任务 ID 供恢复')


def create_application_document_parsing_client() -> DocumentParsingClient:
    from config.env import DOCMIND_ACCESSKEY_ID
    from config.env import DOCMIND_ACCESSKEY_SECRET
    from config.env import DOCMIND_ENDPOINT

    return BoundedDocMindClient(
        access_key_id=DOCMIND_ACCESSKEY_ID,
        access_key_secret=DOCMIND_ACCESSKEY_SECRET,
        endpoint=DOCMIND_ENDPOINT,
    )


__all__ = ['BoundedDocMindClient', 'create_application_document_parsing_client']
