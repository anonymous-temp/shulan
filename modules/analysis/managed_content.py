"""Frozen Document public/source content v1, copied at 95d4b624.

The managed v1 extraction/archive boundary must not follow later utility changes.
New semantics require a new managed implementation revision.
"""

from copy import deepcopy

CONTENT_MESSAGES_V1 = {
    'no_readable_text': '未识别到可读的报告正文，请检查文件内容或清晰度。',
    'no_verified_results': '暂未获得可确认的报告结果，请检查文件内容或清晰度。',
    'interpretation_incomplete': '本次解读未完成，已保留可确认的信息，可稍后重试。',
}


def managed_source_content_v1(result):
    content = deepcopy(result)
    content.pop('analysisEvidence', None)
    for section in content.get('sections', []) if isinstance(content.get('sections'), list) else []:
        if isinstance(section, dict):
            section.pop('analysisEvidence', None)
    return content


def managed_public_content_v1(result):
    content = managed_source_content_v1(result)
    content.pop('sourceBindings', None)
    content.pop('ingestionObservations', None)
    content.pop('verifiedObservationPointers', None)
    content.pop('metadataEvidence', None)
    content.pop('expansionEvidence', None)
    for field in ('observations', 'expansion'):
        for item in content.get(field, []) if isinstance(content.get(field), list) else []:
            if isinstance(item, dict):
                for key in ('sourceId', 'evidence', 'rawFlag', 'metadataEvidence'):
                    item.pop(key, None)
    if isinstance(content.get('unresolvedItems'), list):
        fields = (
            'itemName',
            'resultValue',
            'referenceRange',
            'unit',
            'flag',
            'kind',
            'observedAt',
            'method',
            'specimen',
        )
        unresolved = []
        for original in content['unresolvedItems']:
            if not isinstance(original, dict):
                continue
            item = {field: deepcopy(original.get(field)) for field in fields}
            missing = [field for field in ('itemName', 'resultValue') if item[field] is None]
            item['missingFields'] = missing
            item['reason'] = (
                '已识别项目名称，检测结果尚未确认。' if item['itemName'] else '已识别结果值，所属检测项目尚未确认。'
            )
            if type(original.get('sourceSection')) is int:
                item['sourceSection'] = original['sourceSection']
            unresolved.append(item)
        content['unresolvedItems'] = unresolved
    processing = content.get('processing')
    if isinstance(processing, dict):
        summary = {
            key: deepcopy(processing[key])
            for key in (
                'status',
                'retryable',
                'issueCount',
                'version',
                'sourceRevision',
            )
            if key in processing
        }
        summary['warnings'] = (
            ['部分内容尚未确认，已保留可确认的结果。']
            if processing.get('status') == 'partial'
            else ['暂未获得可用的个人检测结果。']
            if processing.get('status') == 'unavailable'
            else []
        )
        if processing.get('failureKind') == 'technical' and processing.get('status') == 'partial':
            summary['warnings'] = ['部分处理因服务异常未完成，已保留可确认的信息，可稍后重试。']
        outcome = processing.get('contentOutcome')
        if isinstance(outcome, str):
            # Stored classification selects a safe explanation; exception text
            # and internal diagnostic codes never become user-facing prose.
            if (
                outcome in CONTENT_MESSAGES_V1
                and processing.get('status') != 'complete'
                and (outcome != 'no_verified_results' or processing.get('status') == 'unavailable')
            ):
                summary['warnings'] = [CONTENT_MESSAGES_V1[outcome]]
        content['processing'] = summary
    for section in content.get('sections', []) if isinstance(content.get('sections'), list) else []:
        if isinstance(section, dict):
            sanitized = managed_public_content_v1(section)
            section.clear()
            section.update(sanitized)
    return content
