"""Explicit content outcomes, separate from transport and persistence failures."""

CONTENT_MESSAGES = {
    'no_readable_text': '未识别到可读的报告正文，请检查文件内容或清晰度。',
    'no_verified_results': '暂未获得可确认的报告结果，请检查文件内容或清晰度。',
    'interpretation_incomplete': '本次解读未完成，已保留可确认的信息，可稍后重试。',
}

TECHNICAL_TIMEOUT_ISSUES = frozenset({'model_call_timeout', 'analysis_deadline'})

_OUTPUT_ISSUES = {
    'invalid_json_schema',
    'json',
    'truncated',
    'model_input_rejected',
    'input_budget',
    'pending_split',
    'object_required',
    'report_type_required',
    'invalid_field_type',
    'invalid_status',
    'invalid_sections',
    'observations_required',
    'observation_object_required',
    'invalid_source_id',
    'invalid_item_field_type',
    'invalid_kind',
}
_SOURCE_ISSUES = {
    'source_evidence_required',
    'unbound_source',
    'background_is_not_a_result',
    'source_quote_mismatch',
    'source_evidence_mismatch',
    'source_name_or_value_mismatch',
    'result_attribution_missing',
    'reference_population_is_not_a_result',
    'optional_evidence_missing',
    'source_cell_required',
    'source_cell_mismatch',
    'source_span_mismatch',
    'source_scope_mismatch',
    'invalid_source_decision',
    'conflicting_source_decision',
    'report_date_evidence_missing',
    'source_value_mismatch',
    'imaging_findings_required',
    'item_name_required',
    'result_value_required',
    'result_value_is_unit',
    'source_name_mismatch',
    'source_metadata_mismatch',
    'conflicting_source_occurrences',
    'missing_source',
    'invalid_source_date',
    'report_date_conflict',
    'missing_result_region',
    'ambiguous_source_metadata',
    'source_input_too_long',
    'unresolved_source',
    'unresolved_table_group',
    'incomplete_source_row',
    'title_evidence_missing',
    'report_title_evidence_missing',
    'unresolved_source_row',
}


def classify_outcome(result):
    """Only known settled content problems qualify; unknown failures remain errors."""
    if not isinstance(result, dict) or not isinstance(result.get('processing'), dict):
        return None
    processing = result['processing']
    if processing.get('status') == 'complete':
        return None
    issues = processing.get('issues')
    if not isinstance(issues, list):
        return None
    codes = {item.get('code') for item in issues if isinstance(item, dict)}
    if len([item for item in issues if isinstance(item, dict)]) != len(issues):
        return None
    if not all(isinstance(code, str) for code in codes):
        return None
    if codes == {'no_readable_text'}:
        return 'no_readable_text'
    if codes - _OUTPUT_ISSUES - _SOURCE_ISSUES:
        return None
    return 'interpretation_incomplete' if codes & _OUTPUT_ISSUES else 'no_verified_results'


def empty_content_result():
    """A successful OCR task with empty text has no clinical source to publish."""
    return {
        'reportType': 'unknown',
        'title': None,
        'reportTime': None,
        'aiSummary': None,
        'status': 'pending',
        'observations': [],
        'expansion': None,
        'unresolvedItems': [],
        'processing': {
            'status': 'unavailable',
            'retryable': False,
            'issueCount': 1,
            'issues': [{'code': 'no_readable_text', 'sourceIds': [], 'recoverable': False}],
            'contentOutcome': 'no_readable_text',
            'failureKind': 'content',
        },
    }
