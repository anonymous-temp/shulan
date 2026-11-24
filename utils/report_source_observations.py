"""Source pointers for structured Report extensions."""

import re


def report_source_items(result):
    """Translate source-document output, never the generated medical report prose."""
    sample = result.get('sample') if isinstance(result.get('sample'), dict) else {}
    timestamp = sample.get('reportDate') or sample.get('sampleDate')
    entries = []
    for key, kind in (('metrics', 'assessment'), ('taxa', 'measurement'), ('panelCounts', 'assessment')):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            name = row.get('name') or row.get('latinName') or row.get('chineseName') or row.get('label')
            value = row.get('value', row.get('count'))
            entries.append(
                (
                    f'/{key}/{index}',
                    {
                        'itemName': name,
                        'resultValue': value,
                        'unit': row.get('unit'),
                        'referenceRange': row.get('referenceRange'),
                        'flag': row.get('sourceStatus', row.get('status')),
                        'kind': kind,
                        'observedAt': row.get('observedAt') or timestamp,
                        'sourceCategory': key,
                    },
                )
            )
    for index, row in enumerate(result.get('diseaseResults') or []):
        if isinstance(row, dict):
            entries.append(
                (
                    f'/diseaseResults/{index}',
                    {
                        'itemName': f'{row.get("name") or "未命名"}（原报告风险）',
                        'resultValue': row.get('sourceScore')
                        if row.get('sourceScore') is not None
                        else row.get('sourceLevel'),
                        'kind': 'assessment',
                        'observedAt': timestamp,
                        'sourceCategory': 'diseaseResults',
                    },
                )
            )
    if result.get('enterotype'):
        entries.append(
            (
                '/enterotype',
                {
                    'itemName': '肠型',
                    'resultValue': result['enterotype'],
                    'kind': 'assessment',
                    'observedAt': timestamp,
                    'sourceCategory': 'enterotype',
                },
            )
        )
    for index, row in enumerate(result.get('supplementaryHospitalData') or []):
        if not isinstance(row, dict):
            continue
        exam = row.get('dataType') == 'EXAM'
        value = row.get('examResult') if exam else row.get('testResultValue')
        unit = row.get('unit')
        if not exam and isinstance(value, str) and unit is None:
            match = re.fullmatch(
                r'\s*([<>≤≥]?\s*[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-zμµ%℃°/][A-Za-z0-9μµ%℃°/^*().· -]*)\s*',
                value,
            )
            if match:
                value, unit = match.group(1).strip(), match.group(2).strip()
        entries.append(
            (
                f'/supplementaryHospitalData/{index}',
                {
                    'itemName': row.get('itemName'),
                    'resultValue': value,
                    'referenceRange': row.get('referenceRange'),
                    'flag': row.get('resultStatusName'),
                    'unit': unit,
                    'observedAt': row.get('reportTime'),
                    'kind': 'narrative' if exam else 'measurement',
                    'sourceCategory': 'supplementaryHospitalData',
                },
            )
        )
    return entries
