"""Inspect or recover one failed QA creation or uncertain document submission.

Default is a read-only preview. Configuration is loaded only by an explicit CLI run.
Use --apply with --reason after reviewing the owner, state and proposed action.
"""

import argparse
import asyncio
import json
import math
import time
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from uuid import uuid4


async def recover_creation(cache, store, *, user_id, session_id, apply=False, reason='', now=None):
    from modules.record.application.creation import is_creation

    now = time.time() if now is None else now
    current = await cache.get(user_id, session_id)
    if not is_creation(current):
        raise ValueError('没有可恢复的临时创建记录；已过期或已进入问答的记录不能重置')
    creation = current['qaCreation']
    result = {
        'userId': user_id,
        'sessionId': session_id,
        'status': current['status'],
        'stage': creation['stage'],
        'attempts': creation['attempts'],
        'documentReferences': creation['checkpoint'].get('references', {}),
        'expiresAt': creation['expiresAt'],
        'action': 'resume_creation',
        'applied': False,
    }
    if not apply:
        return result
    if not reason.strip():
        raise ValueError('恢复操作必须填写原因')
    if current['status'] != 'create_failed' or creation['expiresAt'] <= now:
        raise ValueError('仅可恢复尚未过期且已明确失败的创建记录')
    draft = deepcopy(current)
    draft['status'] = 'creating'
    updated = draft['qaCreation']
    updated.setdefault('manualRecoveryHistory', []).append(
        {
            'at': now,
            'reason': reason,
            'previousAttempts': creation['attempts'],
            'previousErrorCode': (creation.get('error') or {}).get('code'),
        }
    )
    updated.update(attempts=0, error=None, nextAttemptAt=now, revision=uuid4().hex)
    saved = await store.compare_save(
        user_id,
        session_id,
        creation['revision'],
        draft,
        ttl_seconds=max(1, math.ceil(creation['expiresAt'] - now)),
    )
    if not saved:
        raise RuntimeError('创建记录已变化或过期，请重新查看')
    result['applied'] = True
    return result


async def recover_document(
    index, *, user_id, subject_id, document_id, task_id=None, allow_resubmit=False, apply=False, reason=''
):
    document = await index.get(document_id, user_id, subject_id)
    if document is None:
        raise ValueError('文件不存在或不属于指定用户及主体')
    result = {
        'documentId': document_id,
        'status': document['status'],
        'taskId': document.get('taskId'),
        'resubmissionCount': document.get('resubmissionCount', 0),
        'submissionRetryAfter': document.get('submissionRetryAfter'),
        'action': 'bind_task' if task_id else ('allow_one_resubmission' if allow_resubmit else 'inspect'),
        'proposedTaskId': task_id,
        'applied': False,
    }
    if not apply:
        return result
    if not reason.strip():
        raise ValueError('恢复操作必须填写原因')
    updated = await index.recover_submission(
        document,
        datetime.now(timezone.utc),
        task_id=task_id,
        allow_resubmit=allow_resubmit,
        reason=reason,
    )
    if updated is None:
        raise RuntimeError('仅可恢复未被占用的不确定提交或尚未提交的失败；记录已变化时请重新查看')
    result['applied'] = True
    return result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user-id', required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--session-id')
    target.add_argument('--document-id')
    parser.add_argument('--subject-id')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--task-id', help='工作人员已核实属于该文件的上游任务 ID')
    action.add_argument('--allow-resubmit', action='store_true', help='额外授权一次重提，可能产生重复远端任务')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reason', default='')
    args = parser.parse_args(argv)
    if args.document_id and not args.subject_id:
        parser.error('文件核查必须提供 --subject-id')
    if args.session_id and (args.subject_id or args.task_id or args.allow_resubmit):
        parser.error('会话恢复不能携带文件恢复参数')
    if args.apply and not args.reason.strip():
        parser.error('--apply 必须填写 --reason')
    if args.document_id and args.apply and not (args.task_id or args.allow_resubmit):
        parser.error('文件恢复必须指定 --task-id 或 --allow-resubmit')
    return args


async def main(args):
    if args.document_id:
        from adapters.analysis.document_index import create_application_document_index_store

        result = await recover_document(
            create_application_document_index_store(),
            user_id=args.user_id,
            subject_id=args.subject_id,
            document_id=args.document_id,
            task_id=args.task_id,
            allow_resubmit=args.allow_resubmit,
            apply=args.apply,
            reason=args.reason,
        )
    else:
        from adapters.persistence.record_creation import create_application_record_creation_store
        from adapters.persistence.sessions import create_application_record_session_cache

        result = await recover_creation(
            create_application_record_session_cache(),
            create_application_record_creation_store(),
            user_id=args.user_id,
            session_id=args.session_id,
            apply=args.apply,
            reason=args.reason,
        )
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    asyncio.run(main(parse_args()))
