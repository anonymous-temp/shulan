"""Acceptance script contracts with fake HTTP, never a live backend."""

import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.api.scripts import questionnaire_live


def arguments(tmp_path, **overrides):
    values = {
        'backend': 'https://example.invalid',
        'user_id': 'synthetic-user',
        'session_id': 'synthetic-session',
        'report_url': None,
        'document_id': None,
        'output_dir': str(tmp_path),
        'no_submit': False,
        'with_factors': False,
        'answers_file': None,
        'timeout': 30,
        'request_timeout': 5,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def snapshot(status):
    return {
        'userId': 'synthetic-user',
        'sessionId': 'synthetic-session',
        'questionnaireId': 'qid',
        'status': status,
        'canChat': status == 'submitted',
        'questionnaire': {'title': 'Synthetic-only content', 'pages': []},
        'guidance': {'privateSyntheticText': 'Do not print report bodies'},
        **({'answers': {}} if status == 'submitted' else {}),
    }


def test_http_only_end_to_end_empty_answer_acceptance(monkeypatch, tmp_path, capsys):
    results = [
        snapshot('queued'),
        snapshot('ready'),
        snapshot('submitted'),
        snapshot('submitted'),
        snapshot('submitted'),
    ]
    calls = []

    def request(backend, path, **kwargs):
        calls.append((backend, path, deepcopy(kwargs)))
        return results.pop(0)

    monkeypatch.setattr(questionnaire_live, 'request_json', request)
    monkeypatch.setattr(questionnaire_live.time, 'sleep', lambda delay: None)
    questionnaire_live.run(arguments(tmp_path))
    assert len(calls) == 5
    assert calls[0][1] == '/api/v1/record/questionnaires'
    assert calls[0][2]['body']['fileList'] == []
    assert calls[1][1].startswith('/api/v1/record/questionnaires?')
    assert calls[2][1].startswith('/api/v1/record/questionnaires/qid/answers?')
    assert calls[2][2]['body'] == calls[3][2]['body'] == {'answers': {}}
    assert calls[4][1].startswith('/api/v1/record/questionnaires?')
    assert (tmp_path / 'questionnaire.json').exists()
    assert (tmp_path / 'replay.json').exists()
    assert (tmp_path / 'restored.json').exists()
    output = capsys.readouterr().out
    assert 'Do not print report bodies' not in output
    assert 'Synthetic-only content' not in output


def test_no_submit_exports_without_calling_answer_endpoint(monkeypatch, tmp_path):
    calls = []

    def request(backend, path, **kwargs):
        calls.append((path, kwargs))
        return snapshot('ready')

    monkeypatch.setattr(questionnaire_live, 'request_json', request)
    questionnaire_live.run(arguments(tmp_path, no_submit=True, document_id='owned-synthetic-doc'))
    assert len(calls) == 1
    assert calls[0][1]['body']['fileList'] == [{'documentId': 'owned-synthetic-doc'}]
    assert (tmp_path / 'data.json').exists()
    assert not (tmp_path / 'submission.json').exists()
    assert not (tmp_path / 'answers.json').exists()


def test_terminal_failure_never_submits_or_exports_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(questionnaire_live, 'request_json', lambda *args, **kwargs: snapshot('expired'))
    with pytest.raises(questionnaire_live.LiveCheckError, match='expired'):
        questionnaire_live.run(arguments(tmp_path))
    assert not (tmp_path / 'answers.json').exists()


def test_identity_mismatch_stops_acceptance(monkeypatch, tmp_path):
    other = dict(snapshot('ready'), userId='other-user')
    monkeypatch.setattr(questionnaire_live, 'request_json', lambda *args, **kwargs: other)
    with pytest.raises(questionnaire_live.LiveCheckError, match='different user/session'):
        questionnaire_live.run(arguments(tmp_path))
    assert not (tmp_path / 'data.json').exists()


def factor_snapshot(status, *, coverage='complete'):
    return {
        'questionnaireId': 'qid',
        'sessionId': 'synthetic-session',
        'assessmentId': 'assessment-1',
        'status': status,
        'result': {
            'version': 'synthetic-result',
            'targets': [{'label': 'Synthetic private target; never print'}],
            'coverage': {'status': coverage, 'gaps': []},
        }
        if status == 'completed'
        else None,
    }


def fake_requests(monkeypatch, results):
    calls = []

    def request(backend, path, **kwargs):
        calls.append((backend, path, deepcopy(kwargs)))
        return deepcopy(results.pop(0))

    monkeypatch.setattr(questionnaire_live, 'request_json', request)
    monkeypatch.setattr(questionnaire_live.time, 'sleep', lambda delay: None)
    return calls


def submission_responses():
    return [snapshot('ready'), snapshot('submitted'), snapshot('submitted'), snapshot('submitted')]


def test_with_factors_polls_get_and_saves_unwrapped_result_and_coverage(monkeypatch, tmp_path, capsys):
    completed = factor_snapshot('completed')
    calls = fake_requests(
        monkeypatch,
        submission_responses() + [factor_snapshot('queued'), factor_snapshot('running'), completed],
    )
    assert questionnaire_live.run(arguments(tmp_path, with_factors=True)) == tmp_path
    factor_calls = calls[4:]
    assert len(factor_calls) == 3
    assert all(
        call[1] == '/api/v1/record/questionnaires/qid/factor-assessment?userId=synthetic-user' for call in factor_calls
    )
    assert all('body' not in call[2] for call in factor_calls)
    saved = json.loads((tmp_path / 'factor-assessment.json').read_text(encoding='utf-8'))
    assert saved == completed['result'] and 'data' not in saved and 'result' not in saved
    metadata = json.loads((tmp_path / 'factor-assessment-status.json').read_text(encoding='utf-8'))
    assert metadata['status'] == 'completed' and 'result' not in metadata
    acceptance = json.loads((tmp_path / 'factor-assessment-acceptance.json').read_text(encoding='utf-8'))
    assert acceptance['coverage'] == completed['result']['coverage']
    assert acceptance['coverageComplete'] is True and acceptance['substantiveEvidenceReview'] == 'pending'
    output = capsys.readouterr().out
    assert 'Synthetic private target' not in output
    assert 'clinical evidence review remains pending' in output


@pytest.mark.parametrize('state', ['failed', 'unavailable'])
def test_terminal_factor_failure_is_saved_then_returns_nonzero(monkeypatch, tmp_path, state):
    calls = fake_requests(monkeypatch, submission_responses() + [factor_snapshot(state)])
    code = questionnaire_live.main(
        [
            '--backend',
            'https://example.invalid',
            '--user-id',
            'synthetic-user',
            '--session-id',
            'synthetic-session',
            '--output-dir',
            str(tmp_path),
            '--with-factors',
        ]
    )
    assert code == 1 and len(calls) == 5
    assert json.loads((tmp_path / 'factor-assessment.json').read_text(encoding='utf-8')) is None
    acceptance = json.loads((tmp_path / 'factor-assessment-acceptance.json').read_text(encoding='utf-8'))
    assert acceptance['taskStatus'] == state and acceptance['coverageComplete'] is False
    assert acceptance['substantiveEvidenceReview'] == 'pending'


@pytest.mark.parametrize('coverage', ['partial', 'unavailable', None])
def test_incomplete_coverage_never_passes_substantive_acceptance(monkeypatch, tmp_path, capsys, coverage):
    completed = factor_snapshot('completed', coverage=coverage)
    fake_requests(monkeypatch, submission_responses() + [completed])
    with pytest.raises(questionnaire_live.LiveCheckError, match='incomplete coverage'):
        questionnaire_live.run(arguments(tmp_path, with_factors=True))
    acceptance = json.loads((tmp_path / 'factor-assessment-acceptance.json').read_text(encoding='utf-8'))
    assert acceptance['coverageComplete'] is False
    assert acceptance['coverage'] == completed['result']['coverage']
    assert 'Factor lifecycle checks completed' not in capsys.readouterr().out


def test_with_factors_and_no_submit_rejected_before_http_or_files(monkeypatch, tmp_path):
    client = Mock(side_effect=AssertionError('must not make HTTP requests'))
    monkeypatch.setattr(questionnaire_live, 'request_json', client)
    with pytest.raises(questionnaire_live.LiveCheckError, match='cannot be combined'):
        questionnaire_live.run(arguments(tmp_path, with_factors=True, no_submit=True))
    with pytest.raises(SystemExit) as raised:
        questionnaire_live.main(
            [
                '--backend',
                'https://example.invalid',
                '--user-id',
                'synthetic-user',
                '--session-id',
                'synthetic-session',
                '--no-submit',
                '--with-factors',
            ]
        )
    assert raised.value.code == 2
    client.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_custom_answer_file_is_reused_for_submit_replay_and_factor_assessment(monkeypatch, tmp_path):
    body = {'answers': {'note': '  synthetic custom answer  '}}
    answer_path = tmp_path / 'input-answers.json'
    answer_path.write_text(json.dumps(body), encoding='utf-8')
    submitted = dict(snapshot('submitted'), answers={'note': 'synthetic custom answer'})
    calls = fake_requests(
        monkeypatch,
        [snapshot('ready'), submitted, submitted, submitted, factor_snapshot('completed')],
    )
    questionnaire_live.run(arguments(tmp_path, with_factors=True, answers_file=str(answer_path)))
    assert calls[1][2]['body'] == calls[2][2]['body'] == body
    assert json.loads((tmp_path / 'answers.json').read_text(encoding='utf-8')) == body
    assert (tmp_path / 'factor-assessment.json').exists()


@pytest.mark.parametrize('content', ['[]', '{"answers":NaN}', '{"answers":{},"extra":1}', '{"note":"bare"}'])
def test_bad_answer_files_fail_before_http(monkeypatch, tmp_path, content):
    path = tmp_path / 'answers-input.json'
    path.write_text(content, encoding='utf-8')
    client = Mock(side_effect=AssertionError('must not make HTTP requests'))
    monkeypatch.setattr(questionnaire_live, 'request_json', client)
    with pytest.raises(questionnaire_live.LiveCheckError, match='--answers-file'):
        questionnaire_live.run(arguments(tmp_path, answers_file=str(path)))
    client.assert_not_called()


def test_answer_file_never_reads_environment_files(monkeypatch, tmp_path):
    monkeypatch.setattr(questionnaire_live.Path, 'read_text', Mock(side_effect=AssertionError('must not read')))
    with pytest.raises(questionnaire_live.LiveCheckError, match='environment file'):
        questionnaire_live._answer_body(tmp_path / '.env.synthetic')


def test_factor_polling_has_separate_bounded_timeout(monkeypatch):
    calls = fake_requests(monkeypatch, [factor_snapshot('queued'), factor_snapshot('running')])
    clock = iter([0, 1, 1, 3])
    monkeypatch.setattr(questionnaire_live.time, 'monotonic', lambda: next(clock))
    with pytest.raises(questionnaire_live.LiveCheckError, match='Factor assessment polling timed out'):
        questionnaire_live._poll_factors(
            'https://example.invalid',
            'synthetic-user',
            'synthetic-session',
            'qid',
            timeout=2,
            request_timeout=5,
        )
    assert len(calls) == 2 and calls[0][2]['timeout'] == 2


@pytest.mark.parametrize('field,value', [('questionnaireId', 'other'), ('sessionId', 'other'), ('userId', 'other')])
def test_factor_ownership_is_checked_before_export(monkeypatch, tmp_path, field, value):
    mismatch = dict(factor_snapshot('completed'), **{field: value})
    fake_requests(monkeypatch, submission_responses() + [mismatch])
    with pytest.raises(questionnaire_live.LiveCheckError, match='different'):
        questionnaire_live.run(arguments(tmp_path, with_factors=True))
    assert not (tmp_path / 'factor-assessment.json').exists()


def test_factor_version_cannot_change_while_polling(monkeypatch, tmp_path):
    changed = dict(factor_snapshot('completed'), assessmentId='different-assessment')
    fake_requests(monkeypatch, submission_responses() + [factor_snapshot('queued'), changed])
    with pytest.raises(questionnaire_live.LiveCheckError, match='identity changed'):
        questionnaire_live.run(arguments(tmp_path, with_factors=True))


def test_completed_factor_response_requires_actual_result(monkeypatch, tmp_path):
    malformed = dict(factor_snapshot('completed'), result=None)
    fake_requests(monkeypatch, submission_responses() + [malformed])
    with pytest.raises(questionnaire_live.LiveCheckError, match='result object'):
        questionnaire_live.run(arguments(tmp_path, with_factors=True))
