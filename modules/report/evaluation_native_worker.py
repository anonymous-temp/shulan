"""Evaluate-only process boundary for PyMuPDF's native, non-thread-safe work."""
from __future__ import annotations

import base64
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY


@dataclass
class _NativeScope:
    cancelled: threading.Event = field(default_factory=threading.Event)
    processes: set = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cancel(self):
        self.cancelled.set()
        with self.lock:
            for process in self.processes:
                if process.poll() is None:
                    process.kill()


_SCOPE: ContextVar[_NativeScope | None] = ContextVar('evaluate_native_process_scope', default=None)
_SLOTS = threading.BoundedSemaphore(max(1, DEFAULT_REPORT_FALLBACK_POLICY.source_parse_concurrency))
_TIMEOUT_SECONDS = 300.0


@contextmanager
def evaluate_native_scope(enabled):
    scope = _NativeScope() if enabled else None
    token = _SCOPE.set(scope) if enabled else None
    try:
        yield
    except BaseException:
        if scope is not None:
            scope.cancel()
        raise
    finally:
        if token is not None:
            _SCOPE.reset(token)


def native_process_enabled():
    return _SCOPE.get() is not None


def extract_in_native_process(filename, content, *, source_type, ocr=True, prove_result_rows=False):
    """Bound only native CPU work; do not reduce report/model concurrency."""
    from .parsing import ExtractedDocument, SourceParseError, _NativeSourceMetric
    from pydantic import TypeAdapter

    scope = _SCOPE.get()
    started = time.monotonic()
    while not _SLOTS.acquire(timeout=0.1):
        if scope is not None and scope.cancelled.is_set():
            raise SourceParseError('native source extraction cancelled')
        if time.monotonic() - started >= _TIMEOUT_SECONDS:
            raise SourceParseError('native source extraction deadline exceeded')
    process = None
    try:
        if scope is not None and scope.cancelled.is_set():
            raise SourceParseError('native source extraction cancelled')
        payload = json.dumps({
            'filename': filename,
            'sourceType': source_type,
            'ocr': bool(ocr),
            'proveResultRows': prove_result_rows,
            'content': base64.b64encode(content).decode('ascii'),
        }, ensure_ascii=False).encode()
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parents[2])
        env['PYTHONPATH'] = root + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        process = subprocess.Popen([sys.executable, '-m', 'modules.report.evaluation_native_worker'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if scope is not None:
            with scope.lock:
                scope.processes.add(process)
                if scope.cancelled.is_set():
                    process.kill()
        try:
            output, _stderr = process.communicate(input=payload,
                timeout=max(0.001, _TIMEOUT_SECONDS - (time.monotonic() - started)))
        except subprocess.TimeoutExpired as error:
            raise SourceParseError('native source extraction deadline exceeded') from error
        if scope is not None and scope.cancelled.is_set():
            raise SourceParseError('native source extraction cancelled')
        if process.returncode != 0:
            raise SourceParseError(f'native source extraction worker exited {process.returncode}')
        result = json.loads(output)
        if result.get('ok') is not True:
            raise SourceParseError('native source extraction failed')
        document = TypeAdapter(ExtractedDocument).validate_python(result['document'])
        locators = result['nativeMetricLocators']
        if len(locators) != len(document.native_metrics):
            raise SourceParseError('native source metric locators are incomplete')
        document.native_metrics = [_NativeSourceMetric(**metric.model_dump(),
            source_page=locator['sourcePage'], source_pages=locator['sourcePages'])
            if locator['positioned'] else metric for metric, locator in zip(document.native_metrics, locators, strict=True)]
        return document
    except SourceParseError:
        raise
    except Exception as error:
        raise SourceParseError('native source extraction worker response is invalid') from error
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate()
        if scope is not None and process is not None:
            with scope.lock:
                scope.processes.discard(process)
        _SLOTS.release()


def _worker_main():
    # Preserve a dedicated protocol pipe; native/Python console output must
    # never corrupt it. This fixed -m entry does not import the app's main.
    protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'wb')
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    try:
        from pydantic import TypeAdapter
        from .parsing import ExtractedDocument, _extract_pdf, _extract_image, _NativeSourceMetric
        payload = json.loads(sys.stdin.buffer.read())
        content = base64.b64decode(payload['content'], validate=True)
        if payload['sourceType'] == 'pdf':
            document = _extract_pdf(
                payload['filename'], content, ocr=payload['ocr'], prove_result_rows=payload['proveResultRows'],
            )
        elif payload['sourceType'] == 'image':
            document = _extract_image(payload['filename'], content)
        else:
            raise ValueError('unsupported native source type')
        result = {'ok': True, 'document': TypeAdapter(ExtractedDocument).dump_python(document, mode='json'),
            'nativeMetricLocators': [{'positioned': isinstance(metric, _NativeSourceMetric),
                'sourcePage': getattr(metric, 'source_page', None),
                'sourcePages': getattr(metric, 'source_pages', [])} for metric in document.native_metrics]}
        protocol.write(json.dumps(result, ensure_ascii=False).encode())
        protocol.flush()
        return 0
    except Exception:
        protocol.write(b'{"ok":false}')
        protocol.flush()
        return 2
    finally:
        protocol.close()


if __name__ == '__main__':
    raise SystemExit(_worker_main())
