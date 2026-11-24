"""Durable dispatch accounting: contention, parent cancellation and non-resumable leases."""

import asyncio
from dataclasses import replace

import pytest

from agents.tools import ToolBudget
from agents.tools import ToolRunContext
from agents.tools.contracts import ToolRunConflict
from agents.tools.contracts import ToolRunStopped
from agents.tools.ledger import ToolRunLedger
from tests.unit._support.agents.tool_runs import MemoryToolRunRepository


def setup():
    clock = [10.0]
    ledger = ToolRunLedger(MemoryToolRunRepository(), clock=lambda: clock[0], lease_seconds=10)
    context = ToolRunContext('user', 'root', 'root', 'report')
    return ledger, context, ToolBudget(5, 5, 100, 1000, 100), clock


@pytest.mark.asyncio
async def test_competing_child_allocations_cannot_oversubscribe_parent():
    ledger, root, budget, clock = setup()
    await ledger.admit(root, budget, 'root-input')
    await ledger.claim(root)
    children = [replace(root, run_id=str(i), parent_run_id='root', role='research') for i in range(2)]
    child_budget = ToolBudget(3, 3, 90, 600, 60)
    results = await asyncio.gather(
        *(ledger.admit(child, child_budget, 'child') for child in children), return_exceptions=True
    )
    assert sum(isinstance(result, ToolRunStopped) for result in results) == 1
    child = children[next(i for i, result in enumerate(results) if isinstance(result, dict))]
    child_token = await ledger.claim(child)
    with pytest.raises(ToolRunStopped):
        await ledger.admit(replace(root, run_id='third', parent_run_id='root'), child_budget, 'third')
    await ledger.reserve(child, child_token, 'model:1', kind='model', identity={})
    await ledger.settle(
        child, child_token, 'model:1', usage={'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
    )
    await ledger.finish(child, child_token, status='completed', result={'content': 'done'})
    parent = await ledger.snapshot(root)
    assert (parent['reserved_model'], parent['used_model'], parent['child_usage']['total_tokens']) == (0, 1, 3)
    await ledger.admit(replace(root, run_id='third', parent_run_id='root'), child_budget, 'third')


@pytest.mark.asyncio
async def test_unknown_child_keeps_allocation_and_parent_cannot_publish_complete():
    ledger, root, budget, clock = setup()
    await ledger.admit(root, budget, 'root')
    root_token = await ledger.claim(root)
    child = replace(root, run_id='child', parent_run_id='root')
    await ledger.admit(child, ToolBudget(2, 2, 90), 'child')
    token = await ledger.claim(child)
    await ledger.reserve(child, token, 'model:1', kind='model', identity={})
    await ledger.settle(child, token, 'model:1', usage={'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    await ledger.reserve(child, token, 'tool:1', kind='tool', identity={'idempotency_key': 'stable'})
    await ledger.cancel(child)
    assert (await ledger.finish(child, token, status='cancelled'))['status'] == 'interrupted'
    assert (await ledger.snapshot(root))['reserved_tool'] == 2
    assert (await ledger.snapshot(root))['observed_usage']['total_tokens'] == 3
    assert (await ledger.finish(root, root_token, status='completed'))['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_parent_cancel_or_expired_lease_stops_children_and_late_publication():
    ledger, root, budget, clock = setup()
    await ledger.admit(root, budget, 'root')
    await ledger.claim(root)
    child = replace(root, run_id='child', parent_run_id='root')
    await ledger.admit(child, ToolBudget(1, 1, 90), 'child')
    token = await ledger.claim(child)
    await ledger.cancel(root)
    with pytest.raises(ToolRunStopped):
        await ledger.reserve(child, token, 'model:1', kind='model', identity={})
    with pytest.raises(ToolRunStopped):
        await ledger.finish(child, token, status='completed')
    clock[0] = 21
    assert await ledger.claim(child) is None
    assert (await ledger.snapshot(child))['status'] == 'interrupted'


@pytest.mark.asyncio
async def test_restart_returns_unknown_not_an_execution_claim_and_pins_input():
    ledger, context, budget, clock = setup()
    await ledger.admit(context, budget, 'original')
    token = await ledger.claim(context)
    await ledger.reserve(context, token, 'model:1', kind='model', identity={})
    with pytest.raises(ToolRunConflict):
        await ledger.admit(context, budget, 'changed')
    clock[0] = 21
    restarted = ToolRunLedger(ledger.repository, clock=lambda: clock[0])
    assert await restarted.claim(context) is None
    assert await restarted.claim(context) is None
    node = await restarted.snapshot(context)
    assert node['status'] == 'interrupted'
    assert node['used_model'] == 1
    assert next(iter(node['calls'].values()))['status'] == 'unknown'
    with pytest.raises(ToolRunStopped):
        await ledger.settle(context, token, 'model:1')
    with pytest.raises(ToolRunConflict):
        await restarted.snapshot(replace(context, user_id='other'))


@pytest.mark.asyncio
async def test_cancellation_before_claim_and_deadline_do_not_dispatch():
    ledger, context, budget, clock = setup()
    await ledger.admit(context, budget, 'input')
    await ledger.cancel(context)
    assert await ledger.claim(context) is None
    assert (await ledger.snapshot(context))['status'] == 'cancelled'
    assert (await ledger.snapshot(context))['used_model'] == 0
