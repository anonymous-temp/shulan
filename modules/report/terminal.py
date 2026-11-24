"""Deterministic terminal payloads; recovery never invokes a model."""

from __future__ import annotations

from modules.report.empty_report_factory import empty_followup_report
from modules.report.evaluation_callback import build_frontend_evaluation_callback
from modules.report.schemas import FailureCallback


def failure_payload(report_type: str, job: dict, *, error_code: str, evaluate: dict | None = None) -> dict:
    message = {
        'REPORT_GENERATION_INTERRUPTED': '报告生成因服务中断而未完成，请重新发起生成',
        'REPORT_DEPENDENCY_FAILED': '主报告生成失败，无法生成此类报告',
        'REPORT_GENERATION_FAILED': '报告未能生成有效内容',
    }.get(error_code, '报告生成失败')
    if report_type == 'evaluate':
        body = build_frontend_evaluation_callback(FailureCallback(
            user_id=job['userId'], session_id=job['sessionId'],
            error_code=error_code, message=message,
        ))
    else:
        # Recovery may only have the new public evaluate artifact. Adapt its
        # metadata at this boundary; downstream fallback factories keep their
        # established DTO and field names.
        if (evaluate or {}).get('schemaVersion') == 'evaluate.v2.0':
            evaluate = {
                'subject': dict(evaluate.get('subject') or {}),
                'sample': {'report_date': (evaluate.get('sample') or {}).get('reportDate')},
            }
        body = empty_followup_report(
            report_type, evaluate=evaluate or {},
            user_id=job['userId'], session_id=job['sessionId'],
        )
        body.update(status='failed', error={'error_code': error_code, 'message': message})
    request = job.get('request') or {}
    group_id = request.get('groupId', request.get('group_id', job.get('groupId')))
    if report_type == 'evaluate':
        from copy import deepcopy

        from modules.report.evaluation_request_input import fmt_typing_report_source

        fmt_url, _ = fmt_typing_report_source(request)
        body.update(fmtTypingReportUrl=fmt_url,
            package=deepcopy(request.get('package') if request.get('package') is not None else []))
    return {**body, 'finalReportId': job['finalReportId'], 'groupId': group_id}


def followup_terminal_payload(body: dict, generation_status: str, *, partial: bool = False) -> dict:
    failed = generation_status in {'failed', 'fallback_empty'}
    status = 'failed' if failed else 'partial_success' if partial else 'success'
    return {
        **body, 'status': status,
        'error': {
            'error_code': 'REPORT_GENERATION_FAILED' if failed else None,
            'message': '报告未能生成有效内容' if failed else None,
        },
    }
