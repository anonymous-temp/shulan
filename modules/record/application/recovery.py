"""Record-owned reply recovery: persistent bounded generation and replayable commits."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass
from uuid import uuid4

from modules.record.agent import LEGACY_RECORD_AGENT_MANIFEST
from utils.log import getLogger

logger = getLogger(__name__)
RECOVERY_VERSION = 'record.reply-recovery.v1'


def pending_submission(session):
    return bool(
        session
        and session.get('type') == 'qa'
        and session.get('status') in ('active', 'awaiting_decision')
        and isinstance(session.get('qaFlow'), dict)
        and session['qaFlow'].get('version') == 2
        and session['qaFlow'].get('pendingSubmission') is not None
    )


def missing_reply(session):
    if not session or session.get('type') != 'qa' or session.get('status') not in ('active', 'awaiting_decision'):
        return None
    flow = session.get('qaFlow')
    if flow is not None and (not isinstance(flow, dict) or flow.get('version') != 2):
        return None
    history = session.get('qaHistory')
    if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
        return None
    last = history[-1]
    question, answer = last.get('question'), last.get('answer')
    if not isinstance(question, dict) or not question.get('questionId') or not question.get('question'):
        return None
    if not isinstance(answer, (str, dict, list)) or not answer or isinstance(answer, str) and not answer.strip():
        return None
    current = session.get('currentQuestion')
    if current and (not isinstance(current, dict) or current.get('questionId') != question['questionId']):
        return None
    return last


def reply_identity(session):
    if pending_submission(session):
        pending = session['qaFlow']['pendingSubmission']
        if not isinstance(pending, dict):
            return None
        value = ['submission', pending.get('question'), pending.get('answerHash')]
    elif (last := missing_reply(session)) is not None:
        value = ['missing_reply', last['question']['questionId'], last['answer']]
    else:
        return None
    encoded = json.dumps([session['userId'], session['sessionId'], value], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def eligible_for_scan(session, now):
    if not isinstance(session, dict) or session.get('type') != 'qa':
        return False
    if isinstance(session.get('qaCommit'), dict):
        return session['qaCommit'].get('status') != 'failed' and session['qaCommit'].get('nextAttemptAt', 0) <= now
    identity = reply_identity(session)
    if identity is None:
        return False
    receipt = session.get('qaRecovery') or {}
    if not isinstance(receipt, dict):
        return False
    if receipt.get('identity') != identity:
        return True
    if receipt.get('version') != RECOVERY_VERSION or receipt.get('status') in {'exhausted', 'failed'}:
        return False
    return receipt.get('nextAttemptAt', 0) <= now


@dataclass(frozen=True)
class ReplyRecoveryPolicy:
    max_attempts: int = 3
    deadline_seconds: int = 120
    retry_seconds: int = 15

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in (self.max_attempts, self.deadline_seconds, self.retry_seconds)):
            raise ValueError('reply recovery budgets must be positive integers')


class RecoveryAttempt:
    def __init__(self, coordinator, mutation):
        self.coordinator = coordinator
        self.mutation = mutation

    async def freeze(self, result, completed):
        current = deepcopy(self.mutation['expected'])
        result = deepcopy(result)
        result.pop('qaRecovery', None)
        current['qaRecovery'].update(
            status='result_ready',
            resultJson=self.coordinator.store.encode_snapshot(result),
            completed=completed,
            commitAttempts=0,
            commitDeadlineAt=self.coordinator.clock() + 120,
        )
        await self.coordinator.manager._compareRecordSession(current)


class ReplyRecoveryCoordinator:
    def __init__(self, manager, store, *, clock=time.time, policy=None):
        self.manager, self.store, self.clock = manager, store, clock
        self.policy = policy or ReplyRecoveryPolicy()
        self._lock = asyncio.Lock()
        self._accepting = True

    def stop_accepting(self):
        self._accepting = False

    async def recover_pending(self, *, limit=20):
        if not self._accepting or self._lock.locked():
            return 0
        count = 0
        async with self._lock:
            for user_id, session_id in await self.store.pending_replies(now=self.clock(), limit=limit):
                if not self._accepting:
                    break
                if await self.recover_one(user_id, session_id):
                    count += 1
        return count

    async def recover_one(self, user_id, session_id):
        from .service import QASessionConflictError

        try:
            async with self.manager._sessionLease(user_id, session_id) as guard:
                session = await self.manager._load_session(user_id, session_id)
                mutation = self.manager._record_mutation.get()
                cached = mutation['expected']
                if cached and cached.get('qaCommit'):
                    await self.manager._publishFrozenCompletion(guard)
                    await self.manager._ensureRecordProfileQueued(user_id, session_id)
                    return True
                if not eligible_for_scan(session, self.clock()):
                    return False
                identity = reply_identity(session)
                receipt = deepcopy(session.get('qaRecovery') or {})
                if receipt.get('identity') != identity:
                    deadline = self.clock() + self.policy.deadline_seconds
                    # Recovery's commit window is separate from the original model
                    # deadline, which _completeSubmission preserves. An expired
                    # submission can still publish its deterministic fallback.
                    source = (session.get('qaFlow') or {}).get('pendingSubmission') or {}
                    source_selection = (
                        source.get('agentSelection', LEGACY_RECORD_AGENT_MANIFEST)
                        if isinstance(source, dict)
                        else LEGACY_RECORD_AGENT_MANIFEST
                    )
                    receipt = {
                        'version': RECOVERY_VERSION,
                        'agentSelection': deepcopy(source_selection),
                        'identity': identity,
                        'attempts': 0,
                        'maxAttempts': self.policy.max_attempts,
                        'deadlineAt': deadline,
                        'retrySeconds': self.policy.retry_seconds,
                        'status': 'pending',
                    }
                if receipt.get('agentSelection', LEGACY_RECORD_AGENT_MANIFEST) != self.manager.record_agent.manifest:
                    receipt.update(status='failed', errorCode='RECORD_AGENT_VERSION_UNAVAILABLE')
                    await self.manager._compareRecordSession({**session, 'qaRecovery': receipt})
                    return True
                if receipt.get('resultJson') is not None:
                    return await self._replay_result(receipt, guard, user_id, session_id)
                if receipt['attempts'] >= receipt['maxAttempts'] or self.clock() >= receipt['deadlineAt']:
                    receipt.update(status='exhausted', errorCode='RECORD_REPLY_RECOVERY_EXHAUSTED')
                    await self.manager._compareRecordSession({**session, 'qaRecovery': receipt})
                    return True
                receipt.update(
                    status='running',
                    attempts=receipt['attempts'] + 1,
                    token=uuid4().hex,
                    nextAttemptAt=self.clock() + receipt['retrySeconds'],
                    errorCode=None,
                )
                claimed = {**deepcopy(session), 'qaRecovery': receipt}
                await self.manager._compareRecordSession(claimed)
                attempt = RecoveryAttempt(self, mutation)
                token = self.manager._record_recovery_attempt.set(attempt)
                try:
                    async with asyncio.timeout(max(0, receipt['deadlineAt'] - self.clock())):
                        await self.manager._recoverReplyUnderLease(claimed, user_id, session_id, guard)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    await self._record_failure(error)
                finally:
                    self.manager._record_recovery_attempt.reset(token)
                return True
        except QASessionConflictError:
            return False
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning('Record reply recovery deferred: error_type=%s', type(error).__name__)
            return False

    async def _replay_result(self, receipt, guard, user_id, session_id):
        current = deepcopy(self.manager._record_mutation.get()['expected'])
        receipt = current['qaRecovery']
        if receipt.get('commitAttempts', 0) >= 8 or self.clock() >= receipt.get(
            'commitDeadlineAt', receipt['deadlineAt']
        ):
            receipt.update(status='failed', errorCode='RECORD_REPLY_COMMIT_EXHAUSTED')
            await self.manager._compareRecordSession(current)
            return True
        receipt.update(
            commitAttempts=receipt.get('commitAttempts', 0) + 1, nextAttemptAt=self.clock() + receipt['retrySeconds']
        )
        await self.manager._compareRecordSession(current)
        try:
            if not await self.manager._save_session(
                user_id,
                session_id,
                self.store.decode_snapshot(receipt['resultJson']),
                completed=receipt['completed'],
                lease_guard=guard,
            ):
                raise RuntimeError('frozen reply commit failed')
        except Exception as error:
            await self._record_failure(error)
            raise
        if receipt['completed']:
            await self.manager._ensureRecordProfileQueued(user_id, session_id)
        return True

    async def _record_failure(self, error):
        current = deepcopy(self.manager._record_mutation.get()['expected'])
        if current is None or current.get('qaCommit'):
            return  # The immutable completion intent is independently retryable.
        receipt = current.get('qaRecovery')
        if receipt is None:
            return
        receipt['nextAttemptAt'] = self.clock() + receipt['retrySeconds']
        if getattr(error, 'code', None) == 'RECORD_AGENT_VERSION_UNAVAILABLE':
            receipt['status'] = 'failed'
        elif receipt.get('resultJson') is not None:
            receipt['status'] = 'result_ready'
        elif receipt['attempts'] >= receipt['maxAttempts'] or self.clock() >= receipt['deadlineAt']:
            receipt['status'] = 'exhausted'
        else:
            receipt['status'] = 'retry_wait'
        receipt['errorCode'] = (
            'RECORD_REPLY_RECOVERY_EXHAUSTED' if receipt['status'] == 'exhausted' else type(error).__name__
        )
        await self.manager._compareRecordSession(current)
