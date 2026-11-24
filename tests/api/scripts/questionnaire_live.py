"""Live acceptance of production whole-questionnaire HTTP endpoints.

Use synthetic test identities and synthetic reports only. This script never
reads environment files or connects to MongoDB/Redis. It prints states and file
paths, not report/answer bodies. --no-submit exports a generated survey for review.
--with-factors also polls the independent factor assessment. HTTP completion is
not a medical evidence review; partial coverage never passes this acceptance.

Example:
    python -m tests.api.scripts.questionnaire_live --backend http://127.0.0.1:8080 \
        --user-id questionnaire-test --session-id questionnaire-test-001 --no-submit
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.request import Request
from urllib.request import urlopen


class LiveCheckError(RuntimeError):
    """Safe diagnostic without upstream response bodies or sensitive URLs."""


def request_json(backend, path, *, body=None, timeout=30):
    method = 'GET' if body is None else 'POST'
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    request = Request(
        backend + path,
        data=data,
        method=method,
        headers={'Accept': 'application/json', 'Content-Type': 'application/json'},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = response.status
            raw = response.read()
    except HTTPError as error:
        raise LiveCheckError(f'{method} {path.split("?")[0]} returned HTTP {error.code}') from None
    except (URLError, TimeoutError, OSError):
        raise LiveCheckError(f'{method} request could not reach the backend within its timeout') from None
    try:
        envelope = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise LiveCheckError('Backend returned invalid JSON') from None
    if status not in (200, 202) or not isinstance(envelope, dict) or envelope.get('code') not in (200, 202):
        raise LiveCheckError('Backend returned an unsuccessful response envelope')
    result = envelope.get('data')
    if not isinstance(result, dict):
        raise LiveCheckError('Backend response data must be an object')
    return result


def _save(directory, name, value):
    path = directory / name
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print(f'Saved {path.resolve()}')


def _identity(result, user_id, session_id, questionnaire_id=None):
    if result.get('userId') != user_id or result.get('sessionId') != session_id:
        raise LiveCheckError('Backend returned a different user/session identity')
    qid = result.get('questionnaireId')
    if not isinstance(qid, str) or not qid:
        raise LiveCheckError('Backend did not return a questionnaire identity')
    if questionnaire_id is not None and qid != questionnaire_id:
        raise LiveCheckError('Questionnaire identity changed during the acceptance run')
    return qid


def _poll(backend, user_id, session_id, initial, *, timeout, request_timeout):
    deadline = time.monotonic() + timeout
    result = initial
    qid = _identity(result, user_id, session_id)
    last_state = None
    path = '/api/v1/record/questionnaires?' + urlencode({'userId': user_id, 'sessionId': session_id})
    while True:
        _identity(result, user_id, session_id, qid)
        state = result.get('status')
        state_label = f'{state}:submission-pending' if result.get('submissionPending') else state
        if state_label != last_state:
            print(f'Questionnaire state: {state_label}')
            last_state = state_label
        if state in ('ready', 'submitted') and not result.get('submissionPending'):
            return result
        retryable = (
            state in ('queued', 'generating')
            or result.get('submissionPending')
            or (state == 'failed' and result.get('retryable') is True)
        )
        if not retryable:
            raise LiveCheckError(f'Questionnaire stopped in state {state!r}; nothing was submitted')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveCheckError('Generation/recovery polling timed out; reuse this session to restore its state')
        delay = result.get('retryAfterSeconds', 3)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            delay = 3
        time.sleep(min(max(1, delay), 15, remaining))
        result = request_json(backend, path, timeout=min(request_timeout, max(1, deadline - time.monotonic())))


def _poll_factors(backend, user_id, session_id, questionnaire_id, *, timeout, request_timeout):
    deadline = time.monotonic() + timeout
    path = (
        '/api/v1/record/questionnaires/'
        + quote(questionnaire_id, safe='')
        + '/factor-assessment?'
        + urlencode({'userId': user_id})
    )
    result = request_json(backend, path, timeout=min(request_timeout, timeout))
    last_state, assessment_id = None, None
    while True:
        if result.get('questionnaireId') != questionnaire_id or result.get('sessionId') != session_id:
            raise LiveCheckError('Factor assessment returned a different questionnaire/session identity')
        if 'userId' in result and result['userId'] != user_id:
            raise LiveCheckError('Factor assessment returned a different user identity')
        if assessment_id is not None and result.get('assessmentId') != assessment_id:
            raise LiveCheckError('Factor assessment identity changed during polling')
        if result.get('assessmentId'):
            assessment_id = result['assessmentId']
        state = result.get('status')
        if state not in ('queued', 'running', 'completed', 'failed', 'unavailable'):
            raise LiveCheckError('Factor assessment returned an unsupported task state')
        if state != last_state:
            print(f'Factor assessment state: {state}')
            last_state = state
        if state in ('completed', 'failed', 'unavailable'):
            if state == 'completed' and not isinstance(result.get('result'), dict):
                raise LiveCheckError('Completed factor assessment did not return a result object')
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveCheckError('Factor assessment polling timed out; reuse this session to restore its state')
        delay = result.get('retryAfterSeconds', 3)
        if not isinstance(delay, (int, float)) or isinstance(delay, bool):
            delay = 3
        time.sleep(min(max(1, delay), 15, remaining))
        result = request_json(backend, path, timeout=min(request_timeout, max(1, deadline - time.monotonic())))


def _factor_artifacts(directory, assessment):
    result = assessment.get('result')
    coverage = result.get('coverage') if isinstance(result, dict) else None
    coverage_status = coverage.get('status') if isinstance(coverage, dict) else None
    _save(directory, 'factor-assessment.json', result)
    _save(
        directory, 'factor-assessment-status.json', {key: value for key, value in assessment.items() if key != 'result'}
    )
    _save(
        directory,
        'factor-assessment-acceptance.json',
        {
            'taskStatus': assessment['status'],
            'coverage': coverage,
            'coverageComplete': coverage_status == 'complete',
            'substantiveEvidenceReview': 'pending',
        },
    )
    safe_status = coverage_status if coverage_status in ('complete', 'partial', 'unavailable') else 'unreported'
    print(f'Factor coverage status: {safe_status}; substantive medical evidence review remains pending.')
    if assessment['status'] != 'completed':
        raise LiveCheckError(f'Factor assessment stopped in state {assessment["status"]!r}; artifacts were saved')
    if coverage_status != 'complete':
        raise LiveCheckError(
            'Factor assessment has incomplete coverage; substantive evidence acceptance has not passed'
        )


def _answer_body(path):
    if not path:
        return {'answers': {}}
    file = Path(path)
    if file.name.lower().startswith('.env'):
        raise LiveCheckError('--answers-file cannot refer to an environment file')
    try:
        value = json.loads(
            file.read_text(encoding='utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
        )
    except (OSError, UnicodeDecodeError, ValueError):
        raise LiveCheckError('--answers-file must be readable standard JSON') from None
    if not isinstance(value, dict) or set(value) != {'answers'} or not isinstance(value['answers'], dict):
        raise LiveCheckError('--answers-file must contain exactly an answers object')
    return value


def run(args):
    with_factors = getattr(args, 'with_factors', False)
    if with_factors and args.no_submit:
        raise LiveCheckError('--with-factors cannot be combined with --no-submit')
    answers = _answer_body(getattr(args, 'answers_file', None)) if not args.no_submit else None
    parsed = urlsplit(args.backend)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.query or parsed.fragment:
        raise LiveCheckError('--backend must be an HTTP(S) origin or base URL without query/fragment')
    if parsed.username or parsed.password:
        raise LiveCheckError('Credentials must not be embedded in --backend')
    if not args.user_id.strip() or not args.session_id.strip():
        raise LiveCheckError('Use non-empty synthetic user and session identities')
    backend = args.backend.rstrip('/')
    files = []
    if args.report_url:
        files.append({'url': args.report_url})
    if args.document_id:
        files.append({'documentId': args.document_id})
    body = {'userId': args.user_id, 'sessionId': args.session_id, 'fileList': files}
    result = request_json(backend, '/api/v1/record/questionnaires', body=body, timeout=args.request_timeout)
    result = _poll(
        backend,
        args.user_id,
        args.session_id,
        result,
        timeout=args.timeout,
        request_timeout=args.request_timeout,
    )
    qid = _identity(result, args.user_id, args.session_id)
    directory = (
        Path(args.output_dir).resolve()
        if args.output_dir
        else (
            Path(__file__).resolve().parents[3]
            / 'outputs'
            / 'questionnaire-live'
            / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        )
    )
    directory.mkdir(parents=True, exist_ok=True)
    _save(directory, 'data.json', result)
    _save(directory, 'questionnaire.json', result.get('questionnaire'))
    _save(directory, 'guidance.json', result.get('guidance', {}))
    if args.no_submit:
        print('Review export complete; this run did not call the answers endpoint.')
        return directory
    _save(directory, 'answers.json', answers)
    path = (
        '/api/v1/record/questionnaires/'
        + quote(qid, safe='')
        + '/answers?'
        + urlencode(
            {'userId': args.user_id},
        )
    )
    submitted = request_json(backend, path, body=answers, timeout=args.request_timeout)
    _identity(submitted, args.user_id, args.session_id, qid)
    if submitted.get('status') != 'submitted' or submitted.get('canChat') is not True:
        raise LiveCheckError('Answer submission was not confirmed complete')
    if not isinstance(submitted.get('answers'), dict):
        raise LiveCheckError('Submitted questionnaire did not return canonical answers')
    if not answers['answers'] and submitted.get('answers') != {}:
        raise LiveCheckError('Unanswered questions unexpectedly became canonical health answers')
    _save(directory, 'submission.json', submitted)
    replay = request_json(backend, path, body=answers, timeout=args.request_timeout)
    if replay != submitted:
        raise LiveCheckError('Identical submission did not return the original receipt')
    _save(directory, 'replay.json', replay)
    restored = request_json(
        backend,
        '/api/v1/record/questionnaires?'
        + urlencode(
            {'userId': args.user_id, 'sessionId': args.session_id},
        ),
        timeout=args.request_timeout,
    )
    _identity(restored, args.user_id, args.session_id, qid)
    if (
        restored.get('status') != 'submitted'
        or restored.get('answers') != submitted['answers']
        or not restored.get('canChat')
    ):
        raise LiveCheckError('Restoring the submitted questionnaire failed')
    _save(directory, 'restored.json', restored)
    answer_label = 'empty-answer submission' if not answers['answers'] else 'answer submission'
    print(f'Passed: generation, {answer_label}, idempotent replay, and submitted restore.')
    if with_factors:
        assessment = _poll_factors(
            backend,
            args.user_id,
            args.session_id,
            qid,
            timeout=args.timeout,
            request_timeout=args.request_timeout,
        )
        _factor_artifacts(directory, assessment)
        print('Factor lifecycle checks completed; clinical evidence review remains pending.')
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backend', required=True, help='Production API backend; do not use the preview server.')
    parser.add_argument('--user-id', required=True, help='Synthetic acceptance-test user.')
    parser.add_argument('--session-id', required=True, help='Dedicated synthetic acceptance-test session.')
    references = parser.add_mutually_exclusive_group()
    references.add_argument('--report-url', help='Optional URL of a synthetic report.')
    references.add_argument('--document-id', help='Optional self-owned synthetic document reference.')
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument('--no-submit', action='store_true', help='Generate and export only; skip answer submission.')
    execution.add_argument(
        '--with-factors', action='store_true', help='After submitting, poll and export factor assessment.'
    )
    parser.add_argument('--answers-file', help='Optional JSON {"answers": {...}}; defaults to all answers unknown.')
    parser.add_argument('--output-dir', help='Artifact directory; contains generated test data, not debug logs.')
    parser.add_argument(
        '--timeout', type=float, default=900, help='Polling budget per generation/factor phase, seconds.'
    )
    parser.add_argument('--request-timeout', type=float, default=30, help='Per-request timeout in seconds.')
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.request_timeout <= 0:
        parser.error('timeouts must be positive')
    try:
        run(args)
    except (LiveCheckError, OSError) as error:
        print(f'Acceptance failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
