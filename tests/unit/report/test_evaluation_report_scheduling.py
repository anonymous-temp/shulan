import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from modules.report.evaluation_report import _author_chapters


@dataclass
class Plan:
    chapters: list


def chapter(code, number):
    return {
        'code': code,
        'number': number,
        'targets': [
            {'id': code + ':detail', 'path': [code, 'detail'], 'owner': {}},
            {'id': code + ':summary', 'path': [code, 'summary'], 'owner': {'aggregate': True}},
        ],
    }


def state_for(*chapters):
    return SimpleNamespace(
        blueprint=Plan(list(chapters)), evidence=None, approved_blocks={}, audit={}, stage_timings={}
    )


def block(target):
    return {'targetId': target['id'], 'sentences': [{'text': 'reviewed'}]}


@pytest.mark.asyncio
async def test_chapter_summary_waits_for_own_reviewed_details_not_unrelated_chapter(monkeypatch):
    slow_entered = asyncio.Event()
    release_slow = asyncio.Event()
    fast_summary = asyncio.Event()
    seen = []
    gates = []
    state = state_for(chapter('medical', 2), chapter('microbiome', 3), chapter('overview', 1))

    async def author(client, *, blueprint, audit, previous_reviewed, model_gate, on_checkpoint, **kwargs):
        gates.append(model_gate)
        for current in blueprint.chapters:
            code = current['code']
            is_summary = current['targets'][0]['owner'].get('aggregate', False)
            seen.append((code, is_summary))
            if code == 'microbiome' and not is_summary:
                slow_entered.set()
                await release_slow.wait()
            if code == 'medical' and is_summary:
                assert [row['targetId'] for row in previous_reviewed[code]] == ['medical:detail']
                assert not release_slow.is_set()
                fast_summary.set()
            if code == 'overview':
                for body in ('medical', 'microbiome'):
                    assert {row['targetId'] for row in previous_reviewed[body]} == {body + ':detail', body + ':summary'}
            blocks = [block(target) for target in current['targets']]
            audit.setdefault('reviewedBlocks', {})[code] = blocks
            on_checkpoint(code, blocks, [])
        return audit['reviewedBlocks']

    monkeypatch.setattr('modules.report.evaluation_report_writer.author_report_chapters', author)
    task = asyncio.create_task(_author_chapters(state, None, None, None))
    try:
        await asyncio.wait_for(slow_entered.wait(), 2)
        await asyncio.wait_for(fast_summary.wait(), 2)
        release_slow.set()
        await asyncio.wait_for(task, 2)
    finally:
        release_slow.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert seen[-1] == ('overview', False)
    assert len({id(gate) for gate in gates}) == 1
    assert {row['targetId'] for row in state.audit['reviewedBlocks']['medical']} == {
        'medical:detail',
        'medical:summary',
    }


@pytest.mark.asyncio
async def test_functional_details_can_start_before_unrelated_body_details_finish(monkeypatch):
    release = asyncio.Event()
    functional_started = asyncio.Event()
    state = state_for(chapter('medical', 2), chapter('functional', 6))
    state.blueprint.chapters[1]['targets'][0]['path'] = ['functional_pathways', 'items', 0]

    async def author(client, *, blueprint, audit, on_checkpoint, **kwargs):
        for current in blueprint.chapters:
            if not current['targets'][0]['owner'].get('aggregate'):
                if current['code'] == 'medical':
                    await release.wait()
                else:
                    assert not release.is_set()
                    functional_started.set()
            blocks = [block(target) for target in current['targets']]
            audit.setdefault('reviewedBlocks', {})[current['code']] = blocks
            on_checkpoint(current['code'], blocks, [])
        return audit['reviewedBlocks']

    monkeypatch.setattr('modules.report.evaluation_report_writer.author_report_chapters', author)
    task = asyncio.create_task(_author_chapters(state, None, None, None))
    try:
        await asyncio.wait_for(functional_started.wait(), 2)
        release.set()
        await asyncio.wait_for(task, 2)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_joins_children_and_preserves_all_completed_reviewed_checkpoints(monkeypatch):
    slow_entered = asyncio.Event()
    fast_done = asyncio.Event()
    slow_cancelled = asyncio.Event()
    changes = []
    state = state_for(chapter('medical', 2), chapter('microbiome', 3), chapter('overview', 1))

    async def author(client, *, blueprint, audit, on_checkpoint, **kwargs):
        for current in blueprint.chapters:
            code = current['code']
            if code == 'microbiome':
                slow_entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    slow_cancelled.set()
            assert code != 'overview'
            blocks = [block(target) for target in current['targets']]
            audit.setdefault('reviewedBlocks', {})[code] = blocks
            on_checkpoint(code, blocks, [])
            changes.append(current['targets'][0]['id'])
            if current['targets'][0]['owner'].get('aggregate'):
                fast_done.set()
        return audit['reviewedBlocks']

    monkeypatch.setattr('modules.report.evaluation_report_writer.author_report_chapters', author)
    task = asyncio.create_task(_author_chapters(state, None, None, None))
    await asyncio.wait_for(slow_entered.wait(), 2)
    await asyncio.wait_for(fast_done.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow_cancelled.is_set()
    assert changes == ['medical:detail', 'medical:summary']
    assert {row['targetId'] for row in state.audit['reviewedBlocks']['medical']} == set(changes)
    assert all(row['status'] in {'completed', 'interrupted'} for row in state.audit['authoringSchedule'])
    assert not any(row['chapters'] == ['overview'] for row in state.audit['authoringSchedule'])


@pytest.mark.asyncio
async def test_writer_failure_preserves_completed_peer_and_does_not_start_overview(monkeypatch):
    completed = asyncio.Event()
    state = state_for(chapter('medical', 2), chapter('microbiome', 3), chapter('overview', 1))

    async def author(client, *, blueprint, audit, on_checkpoint, **kwargs):
        for current in blueprint.chapters:
            code = current['code']
            if code == 'microbiome':
                await completed.wait()
                raise RuntimeError('simulated writer failure')
            assert code != 'overview'
            blocks = [block(target) for target in current['targets']]
            audit.setdefault('reviewedBlocks', {})[code] = blocks
            on_checkpoint(code, blocks, [])
            if current['targets'][0]['owner'].get('aggregate'):
                completed.set()
        return audit['reviewedBlocks']

    monkeypatch.setattr('modules.report.evaluation_report_writer.author_report_chapters', author)
    with pytest.raises(RuntimeError, match='simulated writer failure'):
        await asyncio.wait_for(_author_chapters(state, None, None, None), 2)
    assert {row['targetId'] for row in state.audit['reviewedBlocks']['medical']} == {
        'medical:detail',
        'medical:summary',
    }


@pytest.mark.asyncio
async def test_chapter_summary_and_final_synthesis_receive_prior_medical_review_findings(monkeypatch):
    state = state_for(chapter('medical', 2), chapter('overview', 1))
    correction = {
        'stage': 'review',
        'chapter': 'medical',
        'issues': {'C01': [{'type': 'unsafe_advice', 'detail': 'A screening finding requires timely assessment.'}]},
    }

    async def author(client, *, blueprint, audit, on_checkpoint, prior_review_findings, **kwargs):
        for current in blueprint.chapters:
            code = current['code']
            is_summary = current['targets'][0]['owner'].get('aggregate')
            if code == 'medical' and not is_summary:
                audit['writer'] = [correction]
            else:
                assert correction in prior_review_findings
            blocks = [block(target) for target in current['targets']]
            audit.setdefault('reviewedBlocks', {})[code] = blocks
            on_checkpoint(code, blocks, [])
        return audit['reviewedBlocks']

    monkeypatch.setattr('modules.report.evaluation_report_writer.author_report_chapters', author)
    await _author_chapters(state, None, None, None)
    assert state.audit['writer'].count(correction) == 1
