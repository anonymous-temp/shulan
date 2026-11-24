"""Executable acceptance must be bounded and independent of external persistence."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.unit._support.harnesses import record_dialogue_acceptance as harness


def arguments(tmp_path, **kwargs):
    values = dict(
        live=False,
        output_dir=str(tmp_path),
        max_model_calls=140,
        timeout_seconds=30,
        scenarios=list(harness.SCENARIOS),
    )
    return SimpleNamespace(**{**values, **kwargs})


def test_cli_defaults_to_bounded_offline_and_rejects_repository_artifacts(tmp_path):
    args = harness.parse_args(['--output-dir', str(tmp_path)])
    assert not args.live
    assert args.max_model_calls == 140
    assert args.timeout_seconds == 600
    with pytest.raises(SystemExit):
        harness.parse_args(['--output-dir', str(harness.ROOT / 'unwanted-traces')])
    with pytest.raises(SystemExit):
        harness.parse_args(['--output-dir', str(tmp_path), '--live'])
    for flag, value in [('--max-model-calls', '0'), ('--timeout-seconds', '601')]:
        with pytest.raises(SystemExit):
            harness.parse_args(['--output-dir', str(tmp_path), flag, value])


def test_selects_only_llm_settings_without_docmind_or_database(tmp_path):
    config = tmp_path / 'synthetic.env'
    config.write_text(
        'SHULAN_LLM_PROVIDER="synthetic-provider"\n'
        'SHULAN_LLM_API_KEY="synthetic-key"\n'
        'SHULAN_LLM_DEFAULT_MODEL="synthetic-model"\n'
        'SHULAN_MONGODB_URI="mongodb://do-not-use.invalid"\n'
        'SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET="do-not-use"\n',
        encoding='utf-8',
    )
    assert harness.settings_from_file(config) == {
        'SHULAN_LLM_PROVIDER': 'synthetic-provider',
        'SHULAN_LLM_API_KEY': 'synthetic-key',
        'SHULAN_LLM_DEFAULT_MODEL': 'synthetic-model',
    }


def test_all_three_smoke_trajectories_use_actual_graph_and_memory(tmp_path, capsys):
    with patch('aion.create_chat_client', side_effect=AssertionError('offline must not call a provider')):
        result = asyncio.run(harness.acceptance(arguments(tmp_path), {}))
    assert result == 0
    printed = capsys.readouterr().out
    events = [json.loads(line) for line in printed.splitlines()]
    progress = [event for event in events if event.get('event') == 'turn_completed']
    assert progress and all('modelLogicalCalls' in event and 'elapsedMs' in event for event in progress)
    assert {event['scenario'] for event in progress} == set(harness.SCENARIOS)
    assert '腹胀' not in printed and 'API_KEY' not in printed
    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert summary['automaticChecksPassed']
    assert summary['modelLogicalCalls'] <= 140
    assert summary['externalServices'] == []
    assert not summary['productionWrites']
    assert summary['docmindCalls'] == summary['profileJobs'] == summary['callbacksSent'] == 0
    assert summary['qualityVerdict'] == 'offline_flow_only'
    assert summary['coverage']['extension']['status'] == 'observed'
    traces = {name: json.loads((tmp_path / (name + '.json')).read_text(encoding='utf-8')) for name in harness.SCENARIOS}
    for name, trace in traces.items():
        assert all(item['passed'] for item in trace['checks']), trace['checks']
        assert trace['turns'][-1]['result']['status'] == 'finished'
        assert trace['replayUnchanged'] and trace['replayExtraCalls'] == 0
        assert trace['readbackPreservesStoredState']
        assert set(trace['publicReadback']) == {'status', 'history', 'replay'}
        assert (tmp_path / (name + '.md')).is_file()
    multiple = traces['multiple_complaints']['finalState']['qaFlow']['intake']
    assert len(multiple['complaints']) == 2
    assert multiple['preferredId'] is None
    extended = traces['extended_intake']
    assert extended['finalState']['qaFlow']['intake']['preferredId'] is None
    assert extended['finalState']['qaFlow']['intakeQuestionCount'] == 15
    assert any(
        turn['result']['phase'] == 'supplement' and turn['result']['intakeQuestionCount'] == 15
        for turn in extended['turns']
    )
    assert '反酸' in ' '.join(str(turn['answer']) for turn in extended['turns'])
    final = traces['final_turn']
    assert final['seedInteractionCount'] == 9
    assert len(final['calls']) == 2
    assert final['turns'][-1]['result']['qaRoundCount'] == 5
    final_concerns = final['finalState']['qaFlow']['concerns']
    assert len(final_concerns) == 3
    assert {item['status'] for item in final_concerns} == {'answered', 'limited'}
    assert next(item for item in final_concerns if item['id'] == harness.SEEDED_CONCERN_ID)['status'] == 'limited'
    final_assessment = json.loads(final['calls'][0]['content'])
    assert harness.SEEDED_CONCERN_ID in {item['id'] for item in final_assessment['concerns']}
    assert len(final['finalState']['qaHistory']) == 10
    assert final['finalState']['qaHistory'][-1]['answer'] == harness.FINAL_ANSWER
    # Parseable results still fail the acceptance when flow semantics regress.
    corrupted = deepcopy(traces['multiple_complaints'])
    corrupted['finalState']['qaFlow']['intake']['complaints'] = multiple['complaints'][:1]
    failures = {item['name'] for item in harness.evaluate_trace(corrupted) if not item['passed']}
    assert 'both_complaints_retained' in failures
    missing_old = deepcopy(final)
    missing_old['finalState']['qaFlow']['concerns'] = [
        item for item in final_concerns if item['id'] != harness.SEEDED_CONCERN_ID
    ]
    failures = {item['name'] for item in harness.evaluate_trace(missing_old) if not item['passed']}
    assert 'old_and_new_final_concerns_resolved' in failures


@pytest.mark.parametrize(
    'notice',
    [
        '部分已提交内容尚未完成可靠分析，已按原文保留。',
        '部分内容尚未完成可靠分析，您提交的原文会保留。',
        '本轮生成失败，请稍后重试。',
        '原始回答已保存。',
        '您的问题已标记为待回应。',
    ],
)
@pytest.mark.parametrize('field', ['assistantMessage', 'question'])
def test_acceptance_rejects_internal_notices_in_public_ai_fields(notice, field):
    trace = {
        'scenario': 'single_goal_dialogue',
        'turns': [{'result': {'status': 'question', field: '接着聊聊睡前安排。' + notice}}],
        'finalState': {},
        'decisions': [],
    }
    rejected = next(
        item for item in harness.evaluate_trace(trace) if item['name'] == 'internal_processing_notices_absent'
    )
    assert not rejected['passed']
    assert rejected['locations'] == [f'turns[0].result.{field}']


def test_acceptance_checks_history_and_replay_without_scanning_patient_or_diagnostics():
    notice = '部分内容尚未完成可靠分析，已按原文保留。'
    result = {
        'status': 'finished',
        'assistantMessage': '日常规律进餐有助于保持血糖稳定。',
        'reasoning': notice,
        'documentProcessing': [{'status': 'partial', 'message': notice}],
        'history': [
            {'answer': notice, 'question': '您平时如何安排晚间休息？', 'assistantMessage': '接着聊聊晚间安排。'}
        ],
    }
    trace = {
        'turns': [{'answer': notice, 'result': result}],
        'finalState': {'qaFlow': {'finalMessage': notice}},
        'calls': [{'content': notice, 'reasoning': notice}],
        'publicReadback': {
            'history': deepcopy(result),
            'replay': deepcopy(result),
            'failed': {'status': 'failed', 'message': notice},
        },
    }
    original = deepcopy(trace)
    assert harness.internal_notice_locations(trace) == []
    assert trace == original
    trace['publicReadback']['history']['history'][0]['assistantMessage'] = notice
    trace['publicReadback']['replay']['assistantMessage'] = notice
    assert harness.internal_notice_locations(trace) == [
        'publicReadback.history.history[0].assistantMessage',
        'publicReadback.replay.assistantMessage',
    ]


def test_focus_cohorts_are_available_for_offline_and_future_live_review(tmp_path):
    with patch('aion.create_chat_client', side_effect=AssertionError('offline must not call a provider')):
        assert asyncio.run(harness.acceptance(arguments(tmp_path, scenarios=list(harness.FOCUS_SCENARIOS)), {})) == 0
    traces = {
        name: json.loads((tmp_path / (name + '.json')).read_text(encoding='utf-8')) for name in harness.FOCUS_SCENARIOS
    }
    wellness = traces['wellness_goals']['finalState']['qaFlow']['intake']
    assert wellness['asked'][:3] == [
        'general:goal',
        'general:medical_history',
        'general:family_history',
    ]
    assert len(wellness['asked']) == len(set(wellness['asked'])) == 15
    assert wellness['complaints'] == []
    report = traces['non_digestive_report']
    assert report['finalState']['parsedHealthReports'] == [harness.EYE_REPORT]
    assert 'general:medical_history' in report['finalState']['qaFlow']['intake']['asked']
    history_task = next(
        item for item in report['decisions'] if (item.get('target') or {}).get('dimension') == 'medical_history'
    )
    assert history_task['target']['supplementation'] and history_task['target']['knownFacts']
    declined = traces['background_decline']
    assert 'invite_background' not in [item['action'] for item in declined['decisions']]
    assert any(item['status'] == 'declined' for item in declined['finalState']['qaFlow']['intake']['facts'].values())
    assert 10 <= declined['finalState']['qaFlow']['intakeQuestionCount'] <= 15
    corrupted = deepcopy(traces['wellness_goals'])
    corrupted['turns'][1]['result'].update(questionType='text', options={'content': ''})
    assert 'every_new_question_has_choices_and_free_input' in {
        item['name'] for item in harness.evaluate_trace(corrupted) if not item['passed']
    }


def test_coverage_uses_accepted_intake_count_and_allows_natural_early_closure():
    def trace(name, count):
        return {
            'scenario': name,
            'turns': [],
            'finalState': {'qaFlow': {'intakeCount': count}},
            'decisions': [{'action': 'invite_supplement', 'target': None, 'responses': []}],
        }

    # Fifteen accepted answers can lead straight to supplement, without a sixteenth question.
    extended = trace('multiple_complaints', 15)
    early = trace('extended_intake', 10)
    assert harness.trace_coverage(extended)['extension']['status'] == 'observed'
    assert harness.trace_coverage(early)['extension']['status'] == 'not_exercised'
    assert harness.trace_coverage(early)['earlyClosure']['status'] == 'observed'
    assert not any(item['name'].startswith('intake_continues') for item in harness.evaluate_trace(early))
    coverage = harness.suite_coverage([extended, early], harness.SCENARIOS)
    assert coverage['complete']
    assert coverage['extension']['scenarios'] == ['multiple_complaints']
    assert coverage['earlyClosure']['scenarios'] == ['extended_intake']
    assert not harness.suite_coverage([early], ['extended_intake'])['complete']
    # A synthetic seed with seven historical intake answers cannot satisfy live extension coverage.
    final = trace('final_turn', 7)
    assert harness.trace_coverage(final)['extension']['status'] == 'not_exercised'
    assert harness.suite_coverage([final], ['final_turn'])['complete']


def test_synthetic_answers_follow_stable_complaint_targets():
    state = {
        'qaFlow': {
            'focus': {'complaintIds': ['reflux']},
            'intake': {
                'complaints': [
                    {'id': 'bloating', 'text': '晚饭后腹胀'},
                    {'id': 'reflux', 'text': '反酸和烧心'},
                ]
            },
        }
    }
    targets = harness.focused_complaint_names(state)
    reply = harness.patient_answer(
        'extended_intake', step=5, phase='intake', dimension='frequency', target_names=targets
    )
    assert '反酸每周一两次' in reply and '腹胀' not in reply
    state['qaFlow']['focus']['complaintIds'] = ['bloating', 'reflux']
    reply = harness.patient_answer(
        'extended_intake',
        step=5,
        phase='intake',
        dimension='onset',
        target_names=harness.focused_complaint_names(state),
    )
    assert '腹胀持续了两个月' in reply and '反酸是最近两周' in reply


def test_turn_limit_allows_control_closure_but_does_not_fake_intake_completion():
    async def run(limit):
        gateway = harness.ObservedGateway(harness.SmokeGateway(), 40)
        manager, cache, _ = harness.build_manager(gateway)
        return await harness.run_trajectory(manager, cache, gateway, 'extended_intake', max_user_turns=limit)

    # This fixture naturally reaches supplement after its fifteenth actual answer.
    closed = asyncio.run(run(15))
    assert closed['trajectoryFinished'] and closed['finalStatus'] == 'finished'
    assert closed['controlCloseExtraCalls'] == 0
    assert closed['turns'][-1]['controlOnly']
    assert closed['turns'][-1]['result']['interactionCount'] == closed['turns'][-2]['result']['interactionCount']
    unfinished = asyncio.run(run(1))
    assert unfinished['userTurnLimitReached'] and not unfinished['trajectoryFinished']
    assert unfinished['finalStatus'] == 'question'
    assert unfinished['turns'][-1]['result']['phase'] == 'intake'
    assert all(item['name'] != 'trajectory_completed_without_generation_error' for item in unfinished['checks'])


def test_each_trajectory_is_saved_before_next_one_starts(tmp_path, capsys):
    original = harness.run_trajectory
    visited = []

    async def inspect_before_next(*args, **kwargs):
        if visited:
            assert (tmp_path / (visited[-1] + '.json')).is_file()
            assert (tmp_path / (visited[-1] + '.md')).is_file()
            assert not (tmp_path / 'summary.json').exists()
        visited.append(args[3])
        return await original(*args, **kwargs)

    with patch.object(harness, 'run_trajectory', inspect_before_next):
        assert asyncio.run(harness.acceptance(arguments(tmp_path), {})) == 0
    completed = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if json.loads(line).get('event') == 'scenario_completed'
    ]
    assert len(completed) == 3
    assert all(row['errorType'] is None and row['trajectoryFinished'] for row in completed)


def test_model_budget_stops_repairs_and_saves_failure_artifacts(tmp_path):
    result = asyncio.run(harness.acceptance(arguments(tmp_path, max_model_calls=1), {}))
    assert result == 1
    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    calls = json.loads((tmp_path / 'calls.json').read_text(encoding='utf-8'))
    assert summary['modelLogicalCalls'] == len(calls) == 1
    assert not summary['automaticChecksPassed']
    assert (tmp_path / 'multiple_complaints.json').is_file()


def test_total_timeout_preserves_in_progress_call_and_trace(tmp_path):
    async def delayed(self, request):
        await asyncio.sleep(10)

    with patch.object(harness.SmokeGateway, 'complete', delayed):
        result = asyncio.run(harness.acceptance(arguments(tmp_path, timeout_seconds=0.5), {}))
    assert result == 1
    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert summary['errorType'] == 'TimeoutError'
    trace = json.loads((tmp_path / 'multiple_complaints.json').read_text(encoding='utf-8'))
    assert trace['errorType'] == 'CancelledError'
    assert trace['calls'][0]['errorType'] == 'CancelledError'
    assert trace['calls'][0]['elapsedMs'] >= 0


def test_default_main_never_reads_supplied_live_config(tmp_path):
    with patch.object(harness, 'settings_from_file', side_effect=AssertionError('no config read in smoke')):
        result = harness.main(
            ['--config', str(Path('not-read.env')), '--output-dir', str(tmp_path), '--scenarios', 'final_turn']
        )
    assert result == 0


def test_live_results_require_prose_review_even_if_graph_checks_pass(tmp_path):
    # Exercise reporting policy with an inert delegate; this is not live verification.
    with patch.object(harness, 'build_delegate', return_value=harness.SmokeGateway()):
        result = asyncio.run(
            harness.acceptance(
                arguments(tmp_path, live=True, scenarios=['final_turn']),
                {
                    'SHULAN_LLM_PROVIDER': 'synthetic-provider',
                    'SHULAN_LLM_DEFAULT_MODEL': 'synthetic-model',
                    'SHULAN_LLM_API_KEY': 'never-persist-this-key',
                },
            )
        )
    assert result == 0
    summary = json.loads((tmp_path / 'summary.json').read_text(encoding='utf-8'))
    assert summary['automaticChecksPassed']
    assert summary['manualReviewRequired']
    assert summary['qualityVerdict'] == 'requires_prose_review'
    assert summary['reviewRubric']
    assert summary['provider'] == 'synthetic-provider'
    assert summary['defaultModel'] == 'synthetic-model'
    assert 'never-persist-this-key' not in (tmp_path / 'summary.json').read_text(encoding='utf-8')
    assert harness.public_setting_label('https://name:secret@example.test/private-secret?key=hidden') == (
        'https://example.test'
    )
